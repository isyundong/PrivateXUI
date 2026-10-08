"""Optional real-core regressions for ordinary subscriptions, loopback only.

PRIVATE_XUI_MIHOMO=/path/to/mihomo python3 -m unittest discover -s tests -p test_mihomo_runtime.py -v
Without PRIVATE_XUI_MIHOMO the normal suite skips these checks. Node and the
repository's YAML dependency render the real Worker using one fake identity.
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
PREFERRED = [{'address': '104.16.0.1', 'name': 'IPv4 fixture'}]
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
  token_sha256: createHash('sha256').update(token).digest('hex'),
  routes: [{protocol: 'vless', path: '/fixture'}],
  preferred: JSON.parse(process.argv[1]),
};
const response = await worker.fetch(
  new Request(`https://sub.example.test/s/${token}`),
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


class MockEntry(socketserver.BaseRequestHandler):
    """Local SOCKS wire transport replacing each ordinary node in the fixture."""
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
            # Never open an upstream connection or resolve a destination name.
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
        self.origin = self.server(MockEntry)
        self.origin.marker = b'ORIGIN'
        self.ip_entry = self.server(MockEntry)
        self.ip_entry.marker = b'IP-ENTRY'
        for entry in (self.origin, self.ip_entry):
            entry.expected_target = self.destination.server_address
        self.addCleanup(self.stop_core)
        self.controller_port, self.mixed_port = self.free_port(), self.free_port()
        self.generated = self.local_config(PREFERRED)
        self.origin_name, self.ip_name = [node['name'] for node in self.generated['proxies']]
        self.current_path = self.directory / 'config.json'

    def local_config(self, preferred):
        rendered = subprocess.run(['node', '--input-type=module', '-e', RENDER,
                                   json.dumps(preferred)], cwd=ROOT,
                                  capture_output=True, text=True, check=True, timeout=10)
        config = json.loads(rendered.stdout)
        self.assertEqual(len(config['proxies']), 1 + len(preferred))
        self.assertTrue(all(node['uuid'] == '00000000-0000-4000-8000-000000000000'
                            for node in config['proxies']))
        self.assertEqual([g for g in config['proxy-groups'] if g['name'] == 'GLOBAL'],
                         [{'name': 'GLOBAL', 'type': 'select',
                           'proxies': ['PROXY'] + [node['name'] for node in config['proxies']]}])
        # Keep the generated selector graph and names; replace only wire transports.
        config['proxies'] = [
            {'name': node['name'], 'type': 'socks5', 'server': '127.0.0.1',
             'port': (self.origin if node['server'] == 'node.example.test'
                      else self.ip_entry).server_address[1]}
            for node in config['proxies']
        ]
        config.update({'mode': 'global', 'mixed-port': self.mixed_port,
                       'external-controller': '127.0.0.1:%d' % self.controller_port,
                       'log-level': 'silent', 'tun': {'enable': False},
                       'dns': {'enable': False}, 'profile': {'store-selected': True}})
        for group in config['proxy-groups']:
            if group['type'] == 'url-test':
                # Empty URLs/zero intervals receive parser defaults. Whitespace
                # survives parsing but is trimmed by health checks, skipping I/O.
                group['url'], group['interval'] = ' ', 0
        return config

    def server(self, handler):
        server = Server(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
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
                # The API can expose groups before the tunnel is ready to
                # handle mixed-port traffic. This local HTTP probe does not
                # run a URL health check or populate node delay history.
                if self.api('/proxies/GLOBAL')[0] == 200:
                    self.webpage()
                    return
            except (OSError, http.client.HTTPException, AssertionError):
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

    def assert_proxy_route(self, config=None, marker=b'IP-ENTRY'):
        config = self.generated if config is None else config
        status, group = self.api('/proxies/GLOBAL')
        self.assertEqual(status, 200)
        self.assertEqual(group['all'], ['PROXY'] + [node['name'] for node in config['proxies']])
        self.assertEqual(group['now'], 'PROXY')
        self.assertEqual(self.webpage(), marker)

    def test_global_replaces_cached_direct_on_reload_and_restart(self):
        old = copy.deepcopy(self.generated)
        old['proxy-groups'] = [group for group in old['proxy-groups'] if group['name'] != 'GLOBAL']
        self.start_core(old)
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': 'DIRECT'})[0], 204)
        self.assertEqual(self.webpage(), b'LOCAL-DIRECT')
        self.assertTrue((self.directory / 'cache.db').is_file())
        self.current_path.write_text(json.dumps(self.generated), encoding='utf-8')
        self.assertEqual(self.api('/configs?force=true', 'PUT', {'path': str(self.current_path)})[0], 204)
        self.assert_proxy_route()
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': 'DIRECT'})[0], 400)
        self.stop_core()
        self.start_core(self.generated)
        self.assert_proxy_route()

    def test_global_manual_nodes_persist_and_removed_candidates_are_revoked(self):
        self.start_core(self.generated)
        self.assert_proxy_route()
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': self.origin_name})[0], 204)
        self.assertEqual(self.webpage(), b'ORIGIN')
        self.assertEqual(self.api('/proxies/PROXY')[1]['now'], '自动选择')
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': self.ip_name})[0], 204)
        self.assertEqual(self.webpage(), b'IP-ENTRY')
        self.stop_core()
        self.start_core(self.generated)
        self.assertEqual(self.api('/proxies/GLOBAL')[1]['now'], self.ip_name)
        self.assertEqual(self.webpage(), b'IP-ENTRY')
        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': 'PROXY'})[0], 204)
        self.assert_proxy_route()

        self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': self.ip_name})[0], 204)
        restricted = self.local_config([])
        self.current_path.write_text(json.dumps(restricted), encoding='utf-8')
        self.assertEqual(self.api('/configs?force=true', 'PUT', {'path': str(self.current_path)})[0], 204)
        self.assert_proxy_route(restricted, b'ORIGIN')
        for name in (self.ip_name, 'DIRECT'):
            self.assertEqual(self.api('/proxies/GLOBAL', 'PUT', {'name': name})[0], 400)

    def test_automatic_group_starts_with_ipv4_before_health_checks(self):
        config = copy.deepcopy(self.generated)
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            config['proxies'][0]['port'] = unavailable.getsockname()[1]
            self.start_core(config)
            _, response = self.api('/proxies')
            proxies = response['proxies']
            self.assertEqual(proxies['自动选择']['now'], self.ip_name)
            for node in config['proxies']:
                self.assertEqual(proxies[node['name']]['history'], [])
                self.assertEqual(proxies[node['name']]['extra'], {})
            self.assert_proxy_route()


if __name__ == '__main__':
    unittest.main()
