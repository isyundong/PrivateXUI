import base64
import copy
import hashlib
import io
import contextlib
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connectivity as c

ROUTE = {'protocol': 'vless', 'port': 17001, 'path': '/sample-vl'}
STATE = {'domain': 'node.example.com', 'routes': [ROUTE], 'uuid': 'must-not-leave-state', 'subscription_token': 'also-private'}


class ConnectivityTests(unittest.TestCase):
    def connection(self, status=101, cloudflare=True, valid_key=True):
        conn = Mock()
        response = Mock(status=status)
        def getresponse():
            key = conn.request.call_args.kwargs['headers']['Sec-WebSocket-Key']
            accept = base64.b64encode(hashlib.sha1((key + c.WS_GUID).encode()).digest()).decode()
            headers = {'Upgrade': 'websocket', 'Sec-WebSocket-Accept': accept if valid_key else 'bad'}
            if cloudflare: headers['CF-Ray'] = 'sample-ray'
            response.getheader.side_effect = lambda name, default='': headers.get(name, default)
            return response
        conn.getresponse.side_effect = getresponse
        return conn, response

    def test_realistic_cloudflare_handshake_passes_without_credentials(self):
        conn, response = self.connection()
        with patch.object(c.http.client, 'HTTPSConnection', return_value=conn) as factory:
            result = c.probe_route(STATE['domain'], ROUTE)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(factory.call_args.args, ('node.example.com', 443))
        context = factory.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertNotIn(STATE['uuid'], str(conn.request.call_args))
        self.assertNotIn(STATE['subscription_token'], str(conn.request.call_args))
        conn.close.assert_called_once()
        response.close.assert_called_once()

    def test_101_without_cloudflare_or_valid_handshake_is_not_passed(self):
        for cloudflare, key in [(False, True), (True, False)]:
            conn, _ = self.connection(cloudflare=cloudflare, valid_key=key)
            with patch.object(c.http.client, 'HTTPSConnection', return_value=conn):
                self.assertEqual(c.probe_route(STATE['domain'], ROUTE)['status'], 'unverified')

    def test_http_failure_is_not_assumed_to_be_a_closed_firewall(self):
        conn, _ = self.connection(status=403)
        with patch.object(c.http.client, 'HTTPSConnection', return_value=conn):
            result = c.probe_route(STATE['domain'], ROUTE)
        self.assertIn('HTTP 403', result['reason'])
        self.assertNotIn('防火墙', result['reason'])

    def test_dns_tls_and_timeout_remain_unverified(self):
        for failure, description in [(socket.gaierror(), 'DNS'), (ssl.SSLError(), 'TLS'), (TimeoutError(), '超时')]:
            with patch.object(c.http.client, 'HTTPSConnection', side_effect=failure):
                result = c.probe_route(STATE['domain'], ROUTE)
            self.assertEqual(result['status'], 'unverified')
            self.assertIn(description, result['reason'])

    def report(self):
        return {'checked_at': 100, 'signature': c.signature(STATE), 'results': [{'protocol': 'vless', 'port': 17001, 'status': 'passed', 'reason': 'ok'}]}

    def test_cached_result_expires_and_invalidates_when_routes_change(self):
        state = dict(STATE, connectivity=self.report())
        self.assertIsNotNone(c.cached_report(state, now=101))
        self.assertIsNone(c.cached_report(state, now=401))
        altered = copy.deepcopy(state)
        altered['routes'][0]['port'] = 17002
        self.assertIsNone(c.cached_report(altered, now=101))

    def test_success_suppresses_firewall_opening_reminders(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): c.print_report(STATE, self.report())
        self.assertIn('1/1 通过', out.getvalue())
        self.assertNotIn('防火墙', out.getvalue())
        self.assertNotIn('放行', out.getvalue())


if __name__ == '__main__': unittest.main()
