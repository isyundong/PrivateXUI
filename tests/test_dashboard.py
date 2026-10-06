import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import dashboard as d
import dashboard_service as service


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'history.db'
        d.init_store(self.store)
        self.source = self.root / 'x-ui.db'
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            db.executescript('CREATE TABLE inbounds(id INTEGER,tag TEXT,protocol TEXT,up INTEGER,down INTEGER); CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);')
            db.execute('INSERT INTO inbounds VALUES(1,?,?,?,?)', ('owned-vless','vless',100,200))
            db.execute('INSERT INTO inbounds VALUES(2,?,?,?,?)', ('other-vless','vless',9000,9000))
        self.state = {'inbound_ids':[1], 'tags':['owned-vless']}

    def test_only_owned_ids_and_tags_are_read(self):
        rows = d.owned_counters(self.source, self.state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['up'], 100)
        with self.assertRaises(ValueError):
            d.owned_counters(self.source, {'inbound_ids':[1], 'tags':['other-vless']})

    def test_read_only_source_never_created(self):
        path = self.root / 'absent.db'
        with self.assertRaises(sqlite3.Error):
            d.owned_counters(path, self.state)
        self.assertFalse(path.exists())

    def test_baseline_deltas_resets_and_missing_intervals(self):
        row = {'tag':'owned-vless','protocol':'vless','up':100,'down':200}
        with contextlib.closing(d.connect(self.store)) as db, db:
            d.sample(db,[row],1000)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM traffic').fetchone()[0],0)
            row.update(up=140,down=300);d.sample(db,[row],1030)
            row.update(up=5,down=8);d.sample(db,[row],1060)
            row.update(up=1000,down=1000);d.sample(db,[row],2000)
        result = d.overview(self.store,now=2000)
        self.assertEqual(result['totals']['up'],45)
        self.assertEqual(result['totals']['down'],108)
        self.assertEqual(result['meta']['gaps'],'1')

    def line(self, target='example.com:443', tag='owned-vless', now=None):
        stamp = dt.datetime.fromtimestamp(now or time.time()).strftime('%Y/%m/%d %H:%M:%S')
        return f'{stamp}.123456 from tcp:192.0.2.10:12345 accepted tcp:{target} [{tag} -> direct] email: secret-user\n'

    def test_logs_exclude_source_identity_and_unmanaged_inbounds(self):
        now=int(time.time())
        parsed=d.parse_access(self.line(now=now),{'owned-vless':'vless'},now)
        self.assertEqual(parsed[1:],('vless','example.com','domain',443,'accepted'))
        self.assertIsNone(d.parse_access(self.line(tag='other-vless'),{'owned-vless':'vless'},now))
        self.assertIsNone(d.parse_access(self.line(target='<script>:443'),{'owned-vless':'vless'},now))
        ipv6=d.parse_access(self.line(target='[2606:4700::1]:443'),{'owned-vless':'vless'},now)
        self.assertEqual(ipv6[3],'ip')

    def test_log_cursor_restart_rotation_and_partial_record(self):
        now=int(time.time());log=self.root/'access.log';line=self.line(now=now)
        log.write_text(line+line[:20])
        with contextlib.closing(d.connect(self.store)) as db,db:
            self.assertEqual(d.ingest(db,log,{'owned-vless':'vless'},now,30,False),1)
        with log.open('a') as f:f.write(line[20:])
        with contextlib.closing(d.connect(self.store)) as db,db:
            self.assertEqual(d.ingest(db,log,{'owned-vless':'vless'},now,30,False),1)
            self.assertEqual(d.ingest(db,log,{'owned-vless':'vless'},now,30,False),0)
        log.rename(self.root/'access.log.1');log.write_text(line)
        with contextlib.closing(d.connect(self.store)) as db,db:
            self.assertEqual(d.ingest(db,log,{'owned-vless':'vless'},now,30,False),1)
        self.assertEqual(d.overview(self.store,now=now)['totals']['connections'],3)

    def test_existing_logs_start_at_end_and_old_records_expire(self):
        now=int(time.time());log=self.root/'existing.log';log.write_text(self.line(now=now))
        with contextlib.closing(d.connect(self.store)) as db,db:
            self.assertEqual(d.ingest(db,log,{'owned-vless':'vless'},now,30,True),0)
            db.execute('INSERT INTO events(ts,protocol,target,kind,port,outcome) VALUES(?,?,?,?,?,?)',(now-31*86400,'vless','old.test','domain',443,'accepted'))
            d.prune(db,now,30)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_capacity_limit_keeps_latest_records(self):
        now=int(time.time())
        with contextlib.closing(d.connect(self.store)) as db,db:
            for i in range(8):db.execute('INSERT INTO events(ts,protocol,target,kind,port,outcome) VALUES(?,?,?,?,?,?)',(now,'vless',str(i)+'.test','domain',443,'accepted'))
            with patch.object(d,'MAX_EVENTS',3):d.prune(db,now,30)
        history=d.overview(self.store,now=now)['history']
        self.assertEqual([r['target'] for r in history],['7.test','6.test','5.test'])

    def test_search_is_literal_not_sql(self):
        result=d.overview(self.store,query="' OR 1=1 --")
        self.assertEqual(result['history'],[])

    def test_enable_and_restore_only_log_field_preserves_later_user_edits(self):
        template={'log':{'access':'none','loglevel':'warning'},'outbounds':[{'tag':'unchanged'}]}
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:db.execute('INSERT INTO settings VALUES(?,?)',('xrayTemplateConfig',json.dumps(template)))
        config={'xui_db':str(self.source)};path=self.root/'config.json'
        self.assertTrue(service.set_access_log(self.source,'/var/log/x-ui/private-xui-access.log',config,path))
        self.assertTrue(config['log_change_pending'])
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            changed=json.loads(db.execute('SELECT value FROM settings').fetchone()[0]);changed['log']['loglevel']='debug';changed['routing']={'new':'user edit'}
            db.execute('UPDATE settings SET value=?',(json.dumps(changed),))
        self.assertTrue(service.restore_access_log(config))
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:restored=json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
        self.assertEqual(restored['log'],{'access':'none','loglevel':'debug'})
        self.assertEqual(restored['routing'],{'new':'user edit'})
        self.assertEqual(restored['outbounds'],template['outbounds'])

    def test_existing_access_log_not_reconfigured(self):
        original={'log':{'access':'/var/log/x-ui/existing.log'}}
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:db.execute('INSERT INTO settings VALUES(?,?)',('xrayTemplateConfig',json.dumps(original)))
        config={}
        self.assertFalse(service.set_access_log(self.source,'/new.log',config,self.root/'config.json'))
        self.assertFalse(config['owned_log'])
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:self.assertEqual(json.loads(db.execute('SELECT value FROM settings').fetchone()[0]),original)

    def test_uninstall_does_not_overwrite_user_replacement_log(self):
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:db.execute('INSERT INTO settings VALUES(?,?)',('xrayTemplateConfig',json.dumps({'log':{'access':'/user/new.log'}})))
        self.assertFalse(service.restore_access_log({'xui_db':str(self.source),'owned_log':True,'managed_log':'/our.log','previous_access':{'present':True,'value':'none'}}))

    def test_collector_records_errors_separately_from_zero(self):
        state=self.root/'state.json';state.write_text(json.dumps(self.state))
        collector=d.Collector({'state':str(state),'xui_db':str(self.source),'retention_days':30,'access_log':str(self.root/'missing.log')},self.store)
        collector.once()
        self.assertEqual(collector.snapshot()['traffic'],'正常')
        self.assertIn('不可读',collector.snapshot()['history'])

    def test_systemd_quote_rejects_injection(self):
        self.assertEqual(service.quote('/a%name'), '"/a%%name"')
        with self.assertRaises(ValueError):service.quote('/a\nExecStart=/evil')



class DashboardServiceTests(unittest.TestCase):
    setUp = DashboardTests.setUp
    def test_install_failure_retry_and_uninstall_preserve_history(self):
        import argparse
        state_path=self.root/'state.json'
        state={**self.state,'version':1,'deployment_id':'a'*32,'status':'ready','ipv4':'192.0.2.1'}
        state_path.write_text(json.dumps(state))
        original={'log':{'access':'none'},'outbounds':[{'tag':'direct'}]}
        with contextlib.closing(sqlite3.connect(self.source)) as db,db:
            db.execute('INSERT INTO settings VALUES(?,?)',('xrayTemplateConfig',json.dumps(original)))
        root=self.root/'dashboard';logs=self.root/'logs';program=self.root/'program.pyz';program.write_text('fixture')
        unit=self.root/'private-xui-dashboard.service';timer=self.root/'private-xui-dashboard-logrotate.timer';rotate_unit=self.root/'private-xui-dashboard-logrotate.service';rotate_config=self.root/'logrotate-config'
        args=argparse.Namespace(state=str(state_path),retention_days=7,access_log=None,yes=True)
        def fake_systemctl(*args):
            if args[0] == 'restart':
                with contextlib.closing(d.connect(root/'history.sqlite3')) as db,db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('collector_status',?)",(json.dumps({'last_check':int(time.time()),'traffic':'正常','history':'正常'}),))
            return ''
        with contextlib.ExitStack() as stack:
            for name,value in [('UNIT',unit),('TIMER',timer),('ROTATE_UNIT',rotate_unit),('ROTATE_CONFIG',rotate_config),('PROGRAM',str(program))]:stack.enter_context(patch.object(service,name,value))
            stack.enter_context(patch.object(d,'ROOT',root));stack.enter_context(patch.object(d,'CONFIG',root/'config.json'))
            stack.enter_context(patch('xui_backend.DB_PATH',str(self.source)))
            restart=stack.enter_context(patch('xui_backend.restart_xui_service',side_effect=ValueError('temporary restart failure')))
            stack.enter_context(patch.object(service,'systemctl',side_effect=fake_systemctl))
            stack.enter_context(patch.object(service,'log_folder',return_value=logs))
            stack.enter_context(patch.object(service,'show'))
            stack.enter_context(patch.object(service.shutil,'which',side_effect=lambda name:'/usr/bin/'+name))
            stack.enter_context(patch.object(service.subprocess,'run',return_value=argparse.Namespace(returncode=1,stdout='')))
            with self.assertRaises(ValueError):service.install(args)
            pending=json.loads(d.CONFIG.read_text());self.assertTrue(pending['log_change_pending'])
            restart.side_effect=None
            service.install(args)
            config=json.loads(d.CONFIG.read_text());self.assertNotIn('token',config);self.assertFalse(config['log_change_pending'])
            self.assertIn('dashboard collect',unit.read_text());self.assertIn('OnUnitActiveSec=5min',timer.read_text());self.assertTrue((root/'history.sqlite3').is_file())
            service.uninstall(args)
            self.assertFalse(unit.exists());self.assertFalse(timer.exists());self.assertTrue((root/'history.sqlite3').exists())
            with contextlib.closing(sqlite3.connect(self.source)) as db:self.assertEqual(json.loads(db.execute('SELECT value FROM settings').fetchone()[0]),original)


if __name__=='__main__':unittest.main()
