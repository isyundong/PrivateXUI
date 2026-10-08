"""Optional real-core regression for generated GLOBAL routing, loopback only.

PRIVATE_XUI_MIHOMO=/path/to/mihomo python3 -m unittest discover -s tests -p test_mihomo_runtime.py -v
Without PRIVATE_XUI_MIHOMO the normal suite skips this check. Node and the
repository's YAML test dependency render the real Worker using fake credentials.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDER = r"""
import {createHash, webcrypto} from 'node:crypto';
import YAML from 'yaml';
import worker from './worker.mjs';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
globalThis.fetch = async () => { throw new Error('External fetch forbidden'); };
const token = 'A'.repeat(43);
const config = {
  subscription_domain: 'sub.example.test', domain: 'node.example.test',
  uuid: '00000000-0000-4000-8000-000000000000',
  egress_profile: {uuid: '11111111-1111-4111-8111-111111111111'},
  token_sha256: createHash('sha256').update(token).digest('hex'),
  routes: [{protocol: 'vless', path: '/fixture'}], preferred: [],
};
const response = await worker.fetch(
  new Request(`https://sub.example.test/s/${token}?egress=all`),
  {SUB_CONFIG: JSON.stringify(config)},
);
if (response.status !== 200) throw new Error('Fixture rendering failed');
process.stdout.write(JSON.stringify(YAML.parse(await response.text())));
"""


def receive(stream, size):
    result = b''
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise OSError('connection closed')
        result += chunk
    return result


def answer_http(stream, marker):
    request = b''
    while b'\r\n\r\n' not in request:
        request += receive(stream, 1)
        if len(request) > 8192:
            raise OSError('request too large')
    stream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '
                   + str(len(marker)).encode() + b'\r\nConnection: close\r\n\r\n' + marker)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DirectDestination(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            answer_http(self.request, b'LOCAL-DIRECT')
        except OSError:
            pass


class SocksDestination(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            version, count = receive(self.request, 2)
            methods = receive(self.request, count)
            if version != 5 or 0 not in methods:
                return
            self.request.sendall(b'\x05\x00')
            version, command, _, kind = receive(self.request, 4)
            if version != 5 or command != 1 or kind != 1:
                return
            address = socket.inet_ntoa(receive(self.request, 4))
            port = int.from_bytes(receive(self.request, 2), 'big')
            # The fixture never opens an upstream connection or resolves DNS.
            if (address, port) != self.server.expected_target:
                return
            self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            answer_http(self.request, self.server.marker)
        except OSError:
            pass


@unittest.skipUnless(os.environ.get('PRIVATE_XUI_MIHOMO'),
                     'Set PRIVATE_XUI_MIHOMO for isolated real-core checks')
class RealMihomoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='private-xui-mihomo-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = os.environ['PRIVATE_XUI_MIHOMO']
        self.process = None
        self.destination = self.server(DirectDestination)
        self.post = self.server(SocksDestination)
        self.post.marker = b'POST'
        self.base = self.server(SocksDestination)
        self.base.marker = b'BASE'
        for upstream in (self.post, self.base):
            upstream.expected_target = self.destination.server_address
        # Stop the core before the servers and temporary directory are removed.
        self.addCleanup(self.stop_core)
        self.controller_port, self.mixed_port = self.free_port(), self.free_port()
        rendered = subprocess.run(['node', '--input-type=module', '-e', RENDER], cwd=ROOT,
                                  capture_output=True, text=True, check=True, timeout=10)
        self.generated = json.loads(rendered.stdout)
        self.assertEqual([g for g in self.generated['proxy-groups'] if g['name'] == 'GLOBAL'],
                         [{'name': 'GLOBAL', 'type': 'select', 'proxies': ['PROXY']}])
        # Keep the generated selector graph; only its wire transports become local fixtures.
        self.generated['proxies'] = [
            {'name': node['name'], 'type': 'socks5', 'server': '127.0.0.1',
             'port': (self.post if node['name'].startswith('[后置 SOCKS5] ')
                      else self.base).server_address[1]}
            for node in self.generated['proxies']
        ]
        self.assertEqual(len(self.generated['proxies']), 2)
        self.generated.update({'mode': 'global', 'mixed-port': self.mixed_port,
                               'external-controller': '127.0.0.1:%d' % self.controller_port,
                               'log-level': 'silent', 'tun': {'enable': False},
                               'dns': {'enable': False}, 'profile': {'store-selected': True}})
        # The fixture has one entry per category, so no periodic health checks exist.
        self.assertTrue(all(group['type'] == 'select' for group in self.generated['proxy-groups']))
        self.current_path = self.directory / 'config.json'

    def server(self, handler):
        server = Server(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def free_port():
        with socket.socket() as stream:
            stream.bind(('127.0.0.1', 0))
            return stream.getsockname()[1]

    def api(self, path, method='GET', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.controller_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or '{}')
        finally:
            connection.close()

    def webpage(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.mixed_port, timeout=3)
        try:
            target = 'http://127.0.0.1:%d/check' % self.destination.server_address[1]
            connection.request('GET', target)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return response.read()
        finally:
            connection.close()

    def start_core(self, config):
        self.current_path.write_text(json.dumps(config), encoding='utf-8')
        self.process = subprocess.Popen([self.binary, '-d', str(self.directory),
                                         '-f', str(self.current_path)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('Mihomo exited before its loopback API became ready')
            try:
                # The controller can answer /version before the initial proxy
                # configuration has finished loading.
                if self.api('/proxies/GLOBAL')[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.025)
        self.fail('Mihomo loopback API did not become ready')

    def stop_core(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process = None

    def assert_post_route(self):
        status, group = self.api('/proxies/GLOBAL')
        self.assertEqual(status, 200)
        self.assertEqual(group['all'], ['PROXY'])
        self.assertEqual(group['now'], 'PROXY')
        self.assertEqual(self.webpage(), b'POST')

    def test_generated_global_replaces_cached_direct_on_reload_and_restart(self):
        old = copy.deepcopy(self.generated)
        old['proxy-groups'] = [group for group in old['proxy-groups'] if group['name'] != 'GLOBAL']
        self.start_core(old)
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': 'DIRECT'})[0], 204)
        self.assertEqual(self.webpage(), b'LOCAL-DIRECT')
        self.assertTrue((self.directory / 'cache.db').is_file())

        self.current_path.write_text(json.dumps(self.generated), encoding='utf-8')
        self.assertEqual(self.api('/configs?force=true', 'PUT',
                                  {'path': str(self.current_path)})[0], 204)
        self.assert_post_route()
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': 'DIRECT'})[0], 400)

        # GLOBAL was never updated to PROXY through the API, so the persisted
        # pre-upgrade DIRECT selection is still exercised after a core restart.
        self.stop_core()
        self.start_core(self.generated)
        self.assert_post_route()


if __name__ == '__main__':
    unittest.main()
