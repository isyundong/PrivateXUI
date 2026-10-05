import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manage as m
from cloudflare_api import APIError, Cloudflare


class FakeCloudflare:
    def __init__(self):
        self.calls = []
        self.records = []
        self.domains = []
        self.rules = {
            'http_request_origin': [{'ref': 'business-origin', 'id': 'business-origin'}],
            'http_config_settings': [{'ref': 'business-config', 'id': 'business-config'}],
        }
        self.worker = None
        self.fail_add = False
        self.fail_cleanup = False
        self.fail_upload = False

    def listing(self, path):
        if path == '/zones':
            return [{'id': 'zone', 'name': 'example.com', 'account': {'id': 'account'}}]
        return copy.deepcopy(self.domains)

    def dns(self, zone, name):
        return [copy.deepcopy(item) for item in self.records if item['name'] == name]

    def ruleset(self, zone, phase):
        return {'id': phase, 'rules': copy.deepcopy(self.rules[phase])}

    def add_rule(self, zone, phase, rule):
        if self.fail_add:
            raise APIError('POST', '/rules', 503)
        self.rules[phase].append({**rule, 'id': rule['ref']})

    def remove_rules(self, zone, phase, refs):
        if self.fail_cleanup:
            raise APIError('DELETE', '/rules', 503)
        self.rules[phase] = [item for item in self.rules[phase] if item['ref'] not in refs]

    def upload_worker(self, account, name, source, config):
        self.worker = copy.deepcopy(config)
        if self.fail_upload:
            raise APIError('PUT', '/worker', 'network')

    def call(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == 'GET' and path.endswith('/settings'):
            return None
        if method == 'POST' and path.endswith('/dns_records'):
            record = {**data, 'id': 'managed-dns'}
            self.records.append(record)
            return record
        if method == 'DELETE' and '/dns_records/' in path:
            self.records = [item for item in self.records if item['id'] != path.rsplit('/', 1)[1]]
        if method == 'PUT' and path.endswith('/workers/domains'):
            record = {**data, 'id': 'managed-domain'}
            self.domains.append(record)
            return record
        if method == 'DELETE' and '/workers/domains/' in path:
            self.domains = [item for item in self.domains if item['id'] != path.rsplit('/', 1)[1]]
        if method == 'DELETE' and '/workers/scripts/' in path:
            self.worker = None
        return {}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'x-ui.db'
        self.path = self.root / 'state.json'
        self.cf = FakeCloudflare()
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE inbounds (id INTEGER PRIMARY KEY, user_id INTEGER, tag TEXT, remark TEXT, protocol TEXT, port INTEGER, settings TEXT, expiry_time INTEGER)')
            c.execute("INSERT INTO inbounds VALUES (1,1,'business','business','vless',10000,'{}',1)")
        self.args = argparse.Namespace(state=str(self.path), node_domain='node.example.com', sub_domain='sub.example.com',
            protocols='vless,trojan,vmess', ipv4='192.0.2.10', preferred=None, fresh=False)
        for obj, name, value in [(m.xui, 'DB_PATH', str(self.db)), (m.xui, 'restart_xui_service', Mock()),
                                  (m.xui, 'is_xui_installed', lambda: True), (m, 'panel_for', lambda backend=None: ('db', None))]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        for obj, name in [(m.xui.request, 'urlopen'), (m.xui.subprocess, 'run')]:
            p = patch.object(obj, name, side_effect=AssertionError('Real external call forbidden'))
            p.start()
            self.addCleanup(p.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT id,tag,expiry_time FROM inbounds ORDER BY id').fetchall()

    def test_full_install_show_and_uninstall_preserves_business_resources(self):
        m.install(self.cf, self.args)
        state = m.load(self.path)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(all(row[2] == 0 for row in self.rows()[1:]))
        self.assertEqual(state['subscription_token'].__len__(), 43)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.print_links(state)
        self.assertNotIn(state['uuid'], out.getvalue())
        self.assertIn('https://sub.example.com/s/', out.getvalue())
        self.assertNotIn('subscription_token', self.cf.worker)
        self.assertEqual(self.cf.worker['token_sha256'], hashlib.sha256(state['subscription_token'].encode()).hexdigest())
        self.assertFalse(any('/settings/ssl' in path for _, path, _ in self.cf.calls))
        self.assertEqual(m.cleanup(self.cf, state, self.path), [])
        self.assertEqual(self.rows(), [(1, 'business', 1)])
        self.assertEqual([rule['ref'] for rule in self.cf.rules['http_request_origin']], ['business-origin'])
        self.assertEqual([rule['ref'] for rule in self.cf.rules['http_config_settings']], ['business-config'])
        self.assertFalse(self.cf.records)
        self.assertFalse(self.cf.domains)
        self.assertIsNone(self.cf.worker)
        self.assertFalse(self.path.exists())

    def test_failed_install_automatically_cleans_created_resources(self):
        self.cf.fail_add = True
        with self.assertRaises(APIError):
            m.install(self.cf, self.args)
        self.assertEqual(self.rows(), [(1, 'business', 1)])
        self.assertEqual(self.cf.records, [])
        self.assertFalse(self.path.exists())

    def test_failed_cleanup_preserves_retryable_journal(self):
        self.cf.fail_add = self.cf.fail_cleanup = True
        with self.assertRaises(APIError):
            m.install(self.cf, self.args)
        state = m.load(self.path)
        self.assertEqual(state['status'], 'cleanup-needed')
        self.cf.fail_cleanup = False
        self.assertEqual(m.cleanup(self.cf, state, self.path), [])
        self.assertFalse(self.path.exists())

    def test_state_written_before_first_node_create_and_ambiguous_api_add_recovered(self):
        create = m.xui.create_inbounds
        def ambiguous(*args, **kwargs):
            self.assertEqual(m.load(self.path)['status'], 'installing')
            create(*args, **kwargs)
            raise RuntimeError('Response lost after successful create')
        with patch.object(m.xui, 'create_inbounds', ambiguous), self.assertRaises(RuntimeError):
            m.install(self.cf, self.args)
        self.assertEqual(self.rows(), [(1, 'business', 1)])
        self.assertFalse(self.path.exists())

    def test_existing_dns_refused_before_any_nodes_or_cloud_writes(self):
        self.cf.records = [{'id': 'existing', 'name': 'node.example.com', 'type': 'CNAME', 'content': 'business.example.com'}]
        with self.assertRaises(ValueError):
            m.install(self.cf, self.args)
        self.assertEqual(self.rows(), [(1, 'business', 1)])
        self.assertEqual(self.cf.calls, [])
        self.assertFalse(self.path.exists())

    def test_failed_rule_read_stops_preflight(self):
        with patch.object(self.cf, 'ruleset', side_effect=APIError('GET', '/rules', 503)), self.assertRaises(APIError):
            m.install(self.cf, self.args)
        self.assertEqual(self.rows(), [(1, 'business', 1)])
        self.assertFalse(self.path.exists())

    def test_same_node_and_subdomain_refused(self):
        self.args.sub_domain = self.args.node_domain
        with self.assertRaises(ValueError):
            m.install(self.cf, self.args)
        self.assertEqual(self.cf.calls, [])

    def test_rotation_ambiguous_upload_reuses_pending_token_on_retry(self):
        m.install(self.cf, self.args)
        state = m.load(self.path)
        old = state['subscription_token']
        self.cf.fail_upload = True
        with self.assertRaises(APIError):
            m.update_worker(self.cf, state, self.path, rotate=True)
        pending = m.load(self.path)
        self.assertEqual(pending['status'], 'update-pending')
        new = pending['pending_token']
        self.assertNotEqual(new, old)
        self.cf.fail_upload = False
        m.update_worker(self.cf, pending, self.path, rotate=True)
        final = m.load(self.path)
        self.assertEqual(final['subscription_token'], new)
        self.assertEqual(final['status'], 'ready')
        self.assertEqual(self.cf.worker['token_sha256'], hashlib.sha256(new.encode()).hexdigest())

    def test_panel_credentials_are_private_at_umask_022(self):
        record, snapshot = self.root / 'panel.json', self.root / 'panel.txt'
        with patch.multiple(m.xui, PANEL_INFO_PATH=str(record), PANEL_INFO_SNAPSHOT=str(snapshot)):
            previous = os.umask(0o022)
            try:
                m.xui.save_panel_access_info({'password': 'fake-only', 'api_token': 'fake-token'})
            finally:
                os.umask(previous)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_manually_changed_inbound_is_not_deleted_and_state_is_retained(self):
        m.install(self.cf, self.args)
        state = m.load(self.path)
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE inbounds SET port=65000 WHERE id=?', (state['inbound_ids'][0],))
        self.assertIn('3x-ui 入站', m.cleanup(self.cf, state, self.path))
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.rows()), 4)

    def test_installer_checksum_failure_never_executes_download(self):
        with patch.object(m.xui.request, 'urlopen', return_value=io.BytesIO(b'wrong content')):
            with self.assertRaises(SystemExit):
                m.xui.run_xui_install_script()
        m.xui.subprocess.run.assert_not_called()

    def test_pinned_installer_is_patched_to_loopback_and_pins_release(self):
        script = b'#!/bin/bash\nbind_local="n"\n'
        def run(argv, **kwargs):
            if argv[0] == 'bash':
                self.assertEqual(argv[2], 'v3.9.0')
                self.assertIn('bind_local="y"', Path(argv[1]).read_text())
                self.assertEqual(kwargs['env']['XUI_NONINTERACTIVE'], '1')
                self.assertEqual(kwargs['env']['XUI_SSL_MODE'], 'none')
                return argparse.Namespace(returncode=0, stdout='Username: fake-user\nPassword: fake-pass\n', stderr='')
            return argparse.Namespace(returncode=0, stdout='active', stderr='')
        with patch.object(m.xui.request, 'urlopen', return_value=io.BytesIO(script)), patch.object(m.xui, 'XUI_INSTALL_SHA256', hashlib.sha256(script).hexdigest()), patch.object(m.xui.subprocess, 'run', side_effect=run):
            self.assertEqual(m.xui.run_xui_install_script(), ('fake-user', 'fake-pass'))


class APIClientTests(unittest.TestCase):
    def test_missing_ruleset_differs_from_server_or_auth_error(self):
        cf = Cloudflare('fake-secret')
        for status in (403, 429, 500):
            cf.opener.open = Mock(side_effect=HTTPError('https://example.com', status, 'failure', {}, io.BytesIO(b'{"success":false}')))
            with self.assertRaises(APIError) as caught:
                cf.ruleset('zone', 'http_request_origin')
            self.assertNotIn('fake-secret', str(caught.exception))
        cf.opener.open = Mock(side_effect=HTTPError('https://example.com', 404, 'missing', {}, io.BytesIO(b'')))
        self.assertIsNone(cf.ruleset('zone', 'http_request_origin'))

    def test_malformed_successful_read_is_not_treated_as_empty(self):
        cf = Cloudflare('fake')
        with patch.object(cf, 'envelope', return_value={'success': True, 'result': None}), self.assertRaises(APIError):
            cf.ruleset('zone', 'http_request_origin')

    def test_rule_add_is_incremental_not_full_list_replacement(self):
        cf = Cloudflare('fake')
        with patch.object(cf, 'ruleset', return_value={'id': 'ruleset', 'rules': [{'ref': 'business'}]}), patch.object(cf, 'call') as call:
            cf.add_rule('zone', 'http_request_origin', {'ref': 'ours', 'action': 'route'})
        self.assertEqual(call.call_args.args[:2], ('POST', '/zones/zone/rulesets/ruleset/rules'))

    def test_upload_uses_secret_binding_disables_logs_and_preserves_unicode(self):
        cf = Cloudflare('fake')
        with patch.object(cf, 'call') as call:
            cf.upload_worker('account', 'worker', 'export default {};', {'uuid': 'fake-uuid', 'name': '测试'})
        payload = call.call_args.kwargs['raw'].decode()
        self.assertIn('"type": "secret_text"', payload)
        self.assertIn('"logpush": false', payload)
        self.assertIn('"observability": {"enabled": false}', payload)
        self.assertNotIn('yx-auto', payload)
        self.assertIn('application/javascript+module', payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
