import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import egress as e
import manage as m
import wizard
import xui_backend as xui
from test_manage import FakeCloudflare

CONFIG = {'address': 'relay.example.net', 'port': 1080, 'username': 'account-secret', 'password': ' password-secret? '}


class EgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'xui.db'
        self.path = self.root / 'state.json'
        self.state = {'version': 1, 'deployment_id': 'a' * 32, 'status': 'ready', 'backend': 'db',
                      'uuid': '00000000-0000-4000-8000-000000000000', 'short_id': '00000000',
                      'inbound_ids': [2, 3, 4], 'tags': ['owned-vless', 'owned-trojan', 'owned-vmess'],
                      'routes': [{'protocol': p, 'port': 17001 + i, 'path': '/path-' + p} for i, p in enumerate(['vless', 'trojan', 'vmess'])],
                      'domain': 'node.example.com', 'subscription_domain': 'sub.example.com', 'subscription_token': 'A' * 43,
                      'preferred': [], 'preferred_mode': 'direct', 'account_id': 'account', 'worker_name': 'worker'}
        self.template = {'log': {'access': '/var/log/existing-access.log'}, 'dns': {'servers': ['1.1.1.1']},
                         'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}, {'tag': 'blocked', 'protocol': 'blackhole'}],
                         'routing': {'domainStrategy': 'IPOnDemand', 'rules': [
                             {'inboundTag': ['api'], 'outboundTag': 'api'},
                             {'domain': ['full:example.com'], 'outboundTag': 'direct'}]}}
        with e.connect(self.db) if self.db.exists() else sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT,remark TEXT,protocol TEXT,port INTEGER,settings TEXT)')
            db.execute('INSERT INTO settings VALUES(?,?)', ('xrayTemplateConfig', json.dumps(self.template)))
            db.execute("INSERT INTO inbounds VALUES(1,'other','other','vless',443,'{\"clients\":[]}')")
            for i, route in enumerate(self.state['routes']):
                settings = xui.protocol_settings(route['protocol'], self.state['uuid'], 'base' + str(i), v3=False)
                db.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?)', (i + 2, self.state['tags'][i], self.state['tags'][i], route['protocol'], route['port'], json.dumps(settings)))
        e.persist(self.path, self.state)
        for target, attr, replacement in [(xui, 'DB_PATH', str(self.db)), (e, 'validate_bundle', Mock()),
                                           (e, 'restart_and_verify', Mock())]:
            mock = patch.object(target, attr, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        self.no_external = patch.object(e.subprocess, 'run', side_effect=AssertionError('Real service calls forbidden'))
        self.no_external.start()
        self.addCleanup(self.no_external.stop)

    def read_template(self):
        return e.read_template(self.db)[0]

    def entries(self, inbound=2):
        with contextlib.closing(e.connect(self.db, True)) as db:
            return json.loads(db.execute('SELECT settings FROM inbounds WHERE id=?', (inbound,)).fetchone()[0])['clients']

    def enable(self):
        e.apply(self.state, self.path, CONFIG)
        return self.state['egress']

    def test_dual_identity_route_and_template_preserve_other_clients_and_settings(self):
        originals = [self.entries(i) for i in [2, 3, 4]]
        bundle = self.enable()
        self.assertNotEqual(bundle['client_uuid'], self.state['uuid'])
        template = self.read_template()
        self.assertEqual(template['outbounds'][:-1], self.template['outbounds'])
        self.assertEqual(template['routing']['rules'][1:], self.template['routing']['rules'])
        self.assertEqual(template['log'], self.template['log'])
        self.assertEqual(template['dns'], self.template['dns'])
        self.assertEqual(bundle['rule']['inboundTag'], self.state['tags'])
        self.assertEqual(len(bundle['rule']['user']), 3)
        self.assertNotIn('network', bundle['rule'])
        for i, original in zip([2, 3, 4], originals):
            self.assertEqual(self.entries(i)[0], original[0])
            self.assertEqual(len(self.entries(i)), 2)
            post = self.entries(i)[1]
            self.assertEqual(post.get('id') or post.get('password'), bundle['client_uuid'])
            self.assertIn(post['email'], bundle['rule']['user'])
        self.assertEqual(self.entries(1), [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('egress_pending', m.load(self.path))

    def test_write_ahead_journal_survives_restart_failure_and_retry_keeps_identity(self):
        e.restart_and_verify.side_effect = ValueError('restart failed')
        with self.assertRaises(ValueError):
            self.enable()
        saved = m.load(self.path)
        self.assertNotIn('egress', saved)
        desired = saved['egress_pending']['after']
        self.assertTrue(e.matches(self.read_template(), desired, e.tag_for(saved)))
        with contextlib.closing(e.connect(self.db, True)) as db:
            self.assertTrue(e.clients_match(db, desired))
        e.restart_and_verify.side_effect = None
        e.apply(saved, self.path, retry=True)
        self.assertEqual(saved['egress']['client_uuid'], desired['client_uuid'])
        self.assertEqual(len(self.entries()), 2)
        self.assertNotIn('egress_pending', saved)

    def test_journal_precedes_sqlite_and_validation_failure_makes_no_change(self):
        e.validate_bundle.side_effect = ValueError('invalid')
        with self.assertRaises(ValueError):
            self.enable()
        self.assertNotIn('egress_pending', m.load(self.path))
        self.assertEqual(self.read_template(), self.template)
        e.validate_bundle.side_effect = None
        original = e.persist
        def inspect(path, state):
            if state.get('egress_pending'):
                self.assertEqual(len(self.entries()), 1)
                self.assertEqual(self.read_template(), self.template)
            original(path, state)
        with patch.object(e, 'persist', side_effect=inspect):
            self.enable()

    def test_change_keeps_post_uuid_and_disable_revokes_only_post_identity(self):
        bundle = self.enable()
        e.apply(self.state, self.path, {'address': '127.0.0.1', 'port': 1081})
        self.assertEqual(self.state['egress']['client_uuid'], bundle['client_uuid'])
        e.apply(self.state, self.path)
        self.assertNotIn('egress', self.state)
        self.assertEqual(self.read_template(), self.template)
        for inbound in [2, 3, 4]:
            self.assertEqual(len(self.entries(inbound)), 1)
            self.assertEqual(self.entries(inbound)[0].get('id') or self.entries(inbound)[0].get('password'), self.state['uuid'])
        self.assertIsNotNone(e.restart_and_verify.call_args.kwargs['revoked'])
        new = self.enable()
        self.assertNotEqual(new['client_uuid'], bundle['client_uuid'])

    def test_manual_client_edit_rolls_back_rule_and_client_deletion_atomically(self):
        bundle = self.enable()
        with contextlib.closing(e.connect(self.db)) as db, db:
            entries = self.entries(3)
            entries[-1]['comment'] = 'user changed'
            db.execute('UPDATE inbounds SET settings=? WHERE id=3', (json.dumps({'clients': entries}),))
        with self.assertRaisesRegex(ValueError, '手动修改'):
            e.apply(self.state, self.path)
        self.assertTrue(e.matches(self.read_template(), bundle, e.tag_for(self.state)))
        self.assertEqual(len(self.entries(2)), 2)  # Earlier row's removal was rolled back.
        self.assertEqual(len(self.entries(3)), 2)

    def test_manual_outbound_or_foreign_reference_is_not_removed(self):
        bundle = self.enable()
        template = self.read_template()
        template['routing']['rules'].append({'inboundTag': ['other'], 'outboundTag': e.tag_for(self.state)})
        with self.assertRaisesRegex(ValueError, '手动修改'):
            e.replace_owned(template, bundle, None, e.tag_for(self.state))
        template = self.read_template()
        template['routing']['balancers'] = [{'selector': ['private-xui-egress-'], 'tag': 'other-balancer'}]
        with self.assertRaisesRegex(ValueError, '引用'):
            e.replace_owned(template, bundle, None, e.tag_for(self.state))

    def test_compare_and_swap_keeps_concurrent_log_changes(self):
        read = e.read_template
        def racing(path):
            template, original = read(path)
            with contextlib.closing(e.connect(path)) as db, db:
                updated = copy.deepcopy(template)
                updated['log']['access'] = '/new/access.log'
                db.execute('UPDATE settings SET value=?', (json.dumps(updated),))
            return template, original
        with patch.object(e, 'read_template', side_effect=racing):
            with self.assertRaisesRegex(ValueError, '其他操作'):
                self.enable()
        self.assertEqual(self.read_template()['log']['access'], '/new/access.log')
        self.assertEqual(len(self.entries()), 1)
        e.apply(self.state, self.path, retry=True)
        self.assertEqual(self.read_template()['log']['access'], '/new/access.log')

    def test_empty_tags_and_wrong_ownership_are_refused_before_writes(self):
        with self.assertRaises(ValueError):
            e.bundle_for(self.state, CONFIG, [])
        with contextlib.closing(e.connect(self.db)) as db, db:
            db.execute("UPDATE inbounds SET settings='{\"clients\":[{\"id\":\"other\"}]}' WHERE id=2")
        with self.assertRaisesRegex(ValueError, '归属'):
            self.enable()
        self.assertNotIn('egress_pending', m.load(self.path))
        self.assertEqual(self.read_template(), self.template)

    def test_whole_uninstall_keeps_egress_until_inbounds_are_gone(self):
        bundle = self.enable()
        with self.assertRaisesRegex(ValueError, '入站仍存在'):
            e.remove_after_inbounds(self.state, self.path)
        self.assertTrue(e.matches(self.read_template(), bundle, e.tag_for(self.state)))
        with contextlib.closing(e.connect(self.db)) as db, db:
            db.execute('DELETE FROM inbounds WHERE id IN (2,3,4)')
        e.remove_after_inbounds(self.state, self.path)
        self.assertEqual(self.read_template(), self.template)
        self.assertEqual(self.entries(1), [])

    def test_worker_payload_and_status_never_contain_upstream_credentials(self):
        bundle = self.enable()
        result = m.worker_config(self.state)
        self.assertEqual(result['egress_profile'], {'uuid': bundle['client_uuid']})
        for secret in CONFIG.values():
            self.assertNotIn(str(secret), json.dumps(result))
        public = e.status(self.state, verify_runtime=False)
        self.assertNotIn(CONFIG['password'], json.dumps(public))
        self.assertNotIn(CONFIG['username'], json.dumps(public))

    def test_publish_failure_has_stable_retry_and_pending_status(self):
        self.enable()
        identity = self.state['egress']['client_uuid']
        cf = FakeCloudflare()
        cf.fail_upload = True
        with self.assertRaises(Exception):
            m.update_worker(cf, self.state, self.path, quiet_links=True)
        self.assertEqual(self.state['status'], 'update-pending')
        self.assertTrue(self.state['egress_subscription_pending'])
        cf.fail_upload = False
        m.update_worker(cf, self.state, self.path, quiet_links=True)
        self.assertEqual(cf.worker['egress_profile']['uuid'], identity)
        self.assertNotIn('egress_subscription_pending', self.state)
        self.assertEqual(self.state['status'], 'ready')

    def test_local_disable_does_not_depend_on_cloudflare_publication_recovery(self):
        self.enable()
        self.state.update(status='update-pending', pending_token='B' * 43)
        e.persist(self.path, self.state)
        e.apply(self.state, self.path)
        self.assertNotIn('egress', self.state)
        self.assertEqual(self.state['pending_token'], 'B' * 43)
        self.assertTrue(self.state['egress_subscription_pending'])
        self.assertEqual(len(self.entries()), 1)

    def test_api_uninstall_may_prune_owned_rule_tags_without_stranding_egress(self):
        self.enable()
        template = self.read_template()
        template['routing']['rules'][0].pop('inboundTag')
        with contextlib.closing(e.connect(self.db)) as db, db:
            db.execute('DELETE FROM inbounds WHERE id IN (2,3,4)')
            db.execute('UPDATE settings SET value=?', (json.dumps(template),))
        e.remove_after_inbounds(self.state, self.path)
        self.assertEqual(self.read_template(), self.template)
        self.assertNotIn('egress', self.state)

    def test_cleanup_refuses_post_identity_copied_to_another_inbound(self):
        self.enable()
        template = self.read_template()
        post = self.entries()[-1]
        with contextlib.closing(e.connect(self.db)) as db, db:
            db.execute('DELETE FROM inbounds WHERE id IN (2,3,4)')
            db.execute('UPDATE inbounds SET settings=? WHERE id=1', (json.dumps({'clients': [post]}),))
        with self.assertRaisesRegex(ValueError, '其他入站'):
            e.remove_after_inbounds(self.state, self.path)
        self.assertEqual(self.read_template(), template)

    def test_egress_command_is_not_wrapped_in_cloudflare_for_status_or_check(self):
        args = argparse.Namespace(command='egress', action='status', state=str(self.path))
        with patch.object(os, 'geteuid', return_value=0), patch.object(m, 'cf_session', side_effect=AssertionError('CF not needed')), contextlib.redirect_stdout(io.StringIO()):
            m.run_command(args)

    def test_subscription_display_and_urls_distinguish_two_classes(self):
        self.enable()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            wizard.show_subscription(self.path)
        text = output.getvalue()
        self.assertIn('?egress=direct', text)
        self.assertIn('?egress=socks', text)
        self.assertIn('默认后置组', text)
        self.assertNotIn(CONFIG['username'], text)
        self.assertNotIn(CONFIG['password'], text)
        self.assertEqual(wizard.subscription_count(self.state), 6)

    def add_v3_schema(self):
        with contextlib.closing(e.connect(self.db)) as db, db:
            db.execute('CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT UNIQUE,sub_id TEXT,uuid TEXT,password TEXT,auth TEXT,flow TEXT,security TEXT,reverse TEXT,limit_ip INTEGER,total_gb INTEGER,expiry_time INTEGER,enable INTEGER,tg_id INTEGER,group_name TEXT,comment TEXT,reset INTEGER,created_at INTEGER,updated_at INTEGER)')
            db.execute('CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER,flow_override TEXT,created_at INTEGER)')
            for i in range(3):
                db.execute('INSERT INTO clients(id,email,uuid,password,enable) VALUES(?,?,?,?,1)', (i + 1, 'base' + str(i), self.state['uuid'], '',))
                db.execute('INSERT INTO client_inbounds VALUES(?,?,?,?)', (i + 1, i + 2, '', 0))

    def test_v3_preserves_original_links_and_removes_only_owned_post_client(self):
        self.add_v3_schema()
        self.enable()
        with contextlib.closing(e.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM client_inbounds').fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 6)
            self.assertTrue(e.clients_match(db, self.state['egress']))
        e.apply(self.state, self.path)
        with contextlib.closing(e.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT client_id,inbound_id FROM client_inbounds').fetchall(), [(1, 2), (2, 3), (3, 4)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 3)

    def test_shared_v3_post_client_is_not_removed_and_transaction_rolls_back(self):
        self.add_v3_schema()
        self.enable()
        with contextlib.closing(e.connect(self.db)) as db, db:
            client = db.execute('SELECT id FROM clients WHERE email=?', (self.state['egress']['clients'][0]['entry']['email'],)).fetchone()[0]
            db.execute('INSERT INTO client_inbounds VALUES(?,?,?,?)', (client, 1, '', 0))
        with self.assertRaisesRegex(ValueError, '其他入站'):
            e.apply(self.state, self.path)
        self.assertEqual(len(self.entries()), 2)

    def test_cleanup_checks_normalized_v3_links_not_only_settings_snapshots(self):
        self.add_v3_schema()
        self.enable()
        before = self.read_template()
        with contextlib.closing(e.connect(self.db)) as db, db:
            client = db.execute('SELECT id FROM clients WHERE email=?', (self.state['egress']['clients'][0]['entry']['email'],)).fetchone()[0]
            db.execute('INSERT INTO client_inbounds VALUES(?,?,?,?)', (client, 1, '', 0))
            db.execute('DELETE FROM inbounds WHERE id IN (2,3,4)')
        with self.assertRaisesRegex(ValueError, '仍关联'):
            e.remove_after_inbounds(self.state, self.path)
        self.assertEqual(self.read_template(), before)

    def test_runtime_verification_requires_user_identity_and_revocation(self):
        bundle = self.enable()
        config = self.read_template()
        config['inbounds'] = [{'tag': c['tag'], 'settings': {'clients': [c['entry']]}} for c in bundle['clients']]
        self.assertTrue(e.matches(config, bundle, e.tag_for(self.state), runtime=True))
        missing = copy.deepcopy(config)
        missing['inbounds'][0]['settings']['clients'] = []
        self.assertFalse(e.matches(missing, bundle, e.tag_for(self.state), runtime=True))
        config['outbounds'] = config['outbounds'][:-1]
        config['routing']['rules'] = config['routing']['rules'][1:]
        self.assertFalse(e.matches(config, None, e.tag_for(self.state), runtime=True, revoked=bundle))

    def test_go_style_nonleader_thread_child_is_discovered(self):
        script = self.root / 'child.py'
        script.write_text('import time\ntime.sleep(10)\n')
        config = self.root / 'runtime.json'
        config.write_text(json.dumps(self.template))
        ready, finish, box = threading.Event(), threading.Event(), []
        def spawn():
            process = subprocess.Popen([sys.executable, str(script), '-config', str(config)])
            box.append(process)
            ready.set()
            finish.wait(8)
        thread = threading.Thread(target=spawn)
        thread.start()
        self.assertTrue(ready.wait(3))
        child = box[0]
        readlink = os.readlink
        def link(path):
            if str(path) == '/proc/%d/exe' % child.pid:
                return '/test/xray-linux-amd64'
            return readlink(path)
        try:
            with patch.object(e.subprocess, 'run', return_value=Mock(returncode=0, stdout=str(os.getpid()))), patch.object(os, 'readlink', side_effect=link):
                actual, identity, binary = e.runtime_config()
            self.assertEqual(actual, self.template)
            self.assertEqual(identity[0], child.pid)
        finally:
            child.terminate(); child.wait(timeout=3)
            finish.set(); thread.join(timeout=3)


class InputAndProbeTests(unittest.TestCase):
    def test_validation_ipv6_idna_and_private_file(self):
        self.assertEqual(e.address('[::1]'), '::1')
        self.assertEqual(e.address('LOCALHOST'), 'localhost')
        self.assertEqual(e.normalize(CONFIG)['password'], CONFIG['password'])
        for value in ['http://relay.test', 'user:pass@relay.test', '0.0.0.0', '::', '1.2.3.999', 'host/path']:
            with self.subTest(value=value), self.assertRaises(ValueError): e.address(value)
        for value in [0, 65536, True, '1.2', None]:
            with self.assertRaises(ValueError): e.port(value)
        for data in [dict(CONFIG, username=''), dict(CONFIG, password=''), dict(CONFIG, password='secret\n'), {'address':'example.com','port':1080,'uri':'credential'}]:
            with self.assertRaises(ValueError): e.normalize(data)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(CONFIG))
            path.chmod(0o644)
            with self.assertRaises(ValueError): e.read_config(path)
            path.chmod(0o600)
            self.assertEqual(e.read_config(path), CONFIG)

    def test_socks_negotiation_requires_correct_version_and_auth_no_downgrade(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=None)
        stream.recv.side_effect = [b'\x05', b'\x02', b'\x01\x00']
        with patch.object(socket, 'create_connection', return_value=stream):
            e.probe(CONFIG)
        self.assertEqual(stream.sendall.call_args_list[0].args[0], b'\x05\x01\x02')
        stream.reset_mock()
        stream.recv.side_effect = [b'\x05\x00']
        with patch.object(socket, 'create_connection', return_value=stream), self.assertRaisesRegex(ValueError, '认证方式'):
            e.probe(CONFIG)
        self.assertEqual(stream.sendall.call_count, 1)  # Never send the password after downgrade.
        stream.recv.side_effect = [b'\x05\x02', b'\x01\x01']
        with patch.object(socket, 'create_connection', return_value=stream), self.assertRaisesRegex(ValueError, '认证失败'):
            e.probe(CONFIG)

    def test_failed_probe_error_does_not_echo_endpoint_or_credentials(self):
        with patch.object(socket, 'create_connection', side_effect=OSError(str(CONFIG))):
            with self.assertRaises(ValueError) as caught: e.probe(CONFIG)
        self.assertNotIn(CONFIG['password'], str(caught.exception))


if __name__ == '__main__':
    unittest.main()
