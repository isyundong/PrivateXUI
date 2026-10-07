"""Optional real-core checks, loopback only.

PRIVATE_XUI_XRAY=/path/to/xray python3 -m unittest discover -s tests -p test_egress_runtime.py -v
The normal suite skips these when no independently verified Xray binary is set.
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

import egress
import xui_backend as xui


def receive(stream, size):
    result = b''
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise OSError('connection closed')
        result += chunk
    return result


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Destination(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        if self.request.recv(4096):
            self.server.hits += 1
            self.request.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 8\r\n\r\nORIGINAL')


class Upstream(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            version, count = receive(self.request, 2)
            methods = receive(self.request, count)
            if version != 5 or 2 not in methods:
                self.request.sendall(b'\x05\xff')
                return
            self.request.sendall(b'\x05\x02')
            auth_version, length = receive(self.request, 2)
            user = receive(self.request, length)
            password = receive(self.request, receive(self.request, 1)[0])
            if auth_version != 1 or user != b'test-user' or password != b'test-pass' or self.server.deny:
                self.request.sendall(b'\x01\x01')
                return
            self.request.sendall(b'\x01\x00')
            version, command, _, kind = receive(self.request, 4)
            if kind == 1:
                host = socket.inet_ntop(socket.AF_INET, receive(self.request, 4))
            elif kind == 3:
                host = receive(self.request, receive(self.request, 1)[0]).decode()
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, receive(self.request, 16))
            else:
                return
            port = struct.unpack('!H', receive(self.request, 2))[0]
            self.server.requests.append((command, host, port))
            self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            if self.request.recv(4096):
                self.request.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nSOCKS')
        except (OSError, ValueError):
            pass


@unittest.skipUnless(os.environ.get('PRIVATE_XUI_XRAY'), 'Set PRIVATE_XUI_XRAY for isolated real-core checks')
class RealXrayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = os.environ['PRIVATE_XUI_XRAY']
        self.processes = []
        self.addCleanup(self.stop_processes)
        self.destination = self.server(Destination)
        self.destination.hits = 0
        self.upstream = self.server(Upstream)
        self.upstream.requests, self.upstream.deny = [], False
        self.base_uuid = str(uuid.uuid4())
        self.rows = [{'id': i + 1, 'tag': 'owned-' + p, 'protocol': p} for i, p in enumerate(['vless', 'trojan', 'vmess'])]
        self.bundle = egress.bundle_for({'deployment_id': 'a' * 32, 'uuid': self.base_uuid},
            {'address': '127.0.0.1', 'port': self.upstream.server_address[1], 'username': 'test-user', 'password': 'test-pass'}, self.rows)
        self.ports = {r['protocol']: self.port() for r in self.rows}
        self.config = {'log': {'loglevel': 'none'}, 'inbounds': [],
                       'outbounds': [{'tag': 'original', 'protocol': 'freedom'}, self.bundle['outbound']],
                       'routing': {'rules': [self.bundle['rule']]}}
        for row, post in zip(self.rows, self.bundle['clients']):
            settings = xui.protocol_settings(row['protocol'], self.base_uuid, 'base-' + row['protocol'])
            settings['clients'].append(post['entry'])
            self.config['inbounds'].append({'tag': row['tag'], 'listen': '127.0.0.1', 'port': self.ports[row['protocol']],
                'protocol': row['protocol'], 'settings': settings, 'streamSettings': xui.ws_stream_settings('/test-' + row['protocol'])})
        self.core = self.start(self.config, self.ports['vless'])

    def server(self, handler):
        server = Server(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def port(self):
        with socket.socket() as stream:
            stream.bind(('127.0.0.1', 0))
            return stream.getsockname()[1]

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def start(self, config, port):
        path = self.root / (str(len(self.processes)) + '.json')
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        test = subprocess.run([self.binary, 'run', '-test', '-config', str(path)], capture_output=True, timeout=10)
        self.assertEqual(test.returncode, 0, 'Xray config rejected: ' + test.stdout.decode(errors='replace'))
        process = subprocess.Popen([self.binary, 'run', '-config', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                self.fail('Xray exited during startup')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.05):
                    return process
            except OSError:
                time.sleep(.02)
        self.fail('Xray did not start listening')

    def client(self, protocol, credential):
        port = self.port()
        target = {'address': '127.0.0.1', 'port': self.ports[protocol]}
        if protocol == 'trojan':
            settings = {'servers': [{**target, 'password': credential}]}
        else:
            settings = {'vnext': [{**target, 'users': [{'id': credential, 'encryption': 'none'} if protocol == 'vless' else {'id': credential, 'alterId': 0, 'security': 'auto'}]}]}
        config = {'log': {'loglevel': 'none'}, 'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks', 'settings': {'auth': 'noauth'}}],
                  'outbounds': [{'protocol': protocol, 'settings': settings, 'streamSettings': xui.ws_stream_settings('/test-' + protocol)}]}
        process = self.start(config, port)
        return port, process

    def request(self, port, host='127.0.0.1'):
        with socket.create_connection(('127.0.0.1', port), timeout=3) as stream:
            stream.sendall(b'\x05\x01\x00')
            self.assertEqual(receive(stream, 2), b'\x05\x00')
            encoded = host.encode()
            stream.sendall(b'\x05\x01\x00\x03' + bytes((len(encoded),)) + encoded + struct.pack('!H', self.destination.server_address[1]))
            answer = receive(stream, 4)
            if answer[1] != 0:
                return b''
            if answer[3] == 1: receive(stream, 4)
            elif answer[3] == 4: receive(stream, 16)
            else: receive(stream, receive(stream, 1)[0])
            receive(stream, 2)
            stream.sendall(b'GET / HTTP/1.0\r\nHost: test\r\n\r\n')
            result = b''
            while True:
                chunk = stream.recv(4096)
                if not chunk: return result
                result += chunk

    def failed_request(self, port):
        try:
            return self.request(port)
        except OSError:
            return b''

    def test_three_protocols_distinguish_no_egress_and_socks_and_never_fallback(self):
        for protocol in self.ports:
            with self.subTest(protocol=protocol):
                direct, direct_process = self.client(protocol, self.base_uuid)
                post, post_process = self.client(protocol, self.bundle['client_uuid'])
                self.assertIn(b'ORIGINAL', self.request(direct))
                hits = self.destination.hits
                self.assertIn(b'SOCKS', self.request(post))
                self.assertEqual(self.destination.hits, hits)
                self.assertIn(b'SOCKS', self.request(post, 'unresolved.example.invalid'))
                self.assertEqual(self.upstream.requests[-1][1], 'unresolved.example.invalid')
                self.upstream.deny = True
                self.assertNotIn(b'ORIGINAL', self.failed_request(post))
                self.assertEqual(self.destination.hits, hits)
                self.upstream.deny = False
                direct_process.terminate(); direct_process.wait(timeout=5)
                post_process.terminate(); post_process.wait(timeout=5)
        direct, _ = self.client('vless', self.base_uuid)
        post, _ = self.client('vless', self.bundle['client_uuid'])
        self.upstream.shutdown()
        self.upstream.server_close()
        hits = self.destination.hits
        self.assertNotIn(b'ORIGINAL', self.failed_request(post))
        self.assertEqual(self.destination.hits, hits)
        self.assertIn(b'ORIGINAL', self.request(direct))

    def test_revoking_post_identity_invalidates_cached_post_nodes_only(self):
        direct, _ = self.client('vless', self.base_uuid)
        post, _ = self.client('vless', self.bundle['client_uuid'])
        self.assertIn(b'SOCKS', self.request(post))
        self.core.terminate(); self.core.wait(timeout=5)
        for inbound in self.config['inbounds']:
            inbound['settings']['clients'] = [inbound['settings']['clients'][0]]
        self.config['outbounds'] = [self.config['outbounds'][0]]
        self.config['routing']['rules'] = []
        self.core = self.start(self.config, self.ports['vless'])
        hits = self.destination.hits
        self.assertEqual(self.failed_request(post), b'')
        self.assertEqual(self.destination.hits, hits)
        self.assertIn(b'ORIGINAL', self.request(direct))
