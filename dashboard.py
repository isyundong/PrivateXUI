"""Local telemetry storage for the terminal dashboard. No network listener."""
import contextlib
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

ROOT = Path('/var/lib/private-xui-dashboard')
CONFIG = ROOT / 'config.json'
INTERVAL = 30
MAX_EVENTS = 100000

def connect(path):
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA max_page_count=32768')
    return db


def init_store(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.closing(connect(path)) as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA journal_size_limit=1048576;
        PRAGMA max_page_count=32768;
        PRAGMA auto_vacuum=INCREMENTAL;
        CREATE TABLE IF NOT EXISTS counters(tag TEXT PRIMARY KEY,protocol TEXT,up INTEGER,down INTEGER,ts INTEGER);
        CREATE TABLE IF NOT EXISTS traffic(minute INTEGER,tag TEXT,protocol TEXT,up INTEGER,down INTEGER,
            PRIMARY KEY(minute,tag));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts INTEGER,protocol TEXT,target TEXT,kind TEXT,port INTEGER,outcome TEXT);
        CREATE INDEX IF NOT EXISTS events_time ON events(ts);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        ''')
        db.commit()
    os.chmod(path, 0o600)


def owned_counters(db_path, state):
    """Read only owned inbound IDs AND tags. Never reset 3x-ui counters."""
    ids, tags = set(state.get('inbound_ids', [])), set(state.get('tags', []))
    if not ids or not tags:
        raise ValueError('本项目没有已记录的入站节点')
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT id,tag,protocol,up,down FROM inbounds').fetchall()
    result = []
    for row in rows:
        if row['id'] in ids and row['tag'] in tags:
            result.append(dict(row))
    if not result:
        raise ValueError('未找到本项目的入站节点；可能已卸载或配置已改变')
    return result


def sample(db, rows, now):
    gaps = 0
    if rows:
        db.execute('DELETE FROM counters WHERE tag NOT IN (' + ','.join('?' for _ in rows) + ')', [r['tag'] for r in rows])
    for row in rows:
        tag, proto = row['tag'], row['protocol']
        up, down = max(0, int(row['up'])), max(0, int(row['down']))
        old = db.execute('SELECT * FROM counters WHERE tag=?', (tag,)).fetchone()
        if old and 0 < now - old['ts'] <= INTERVAL * 5:
            # A decreasing counter denotes a reset. Only count the new counter.
            du = up - old['up'] if up >= old['up'] else up
            dd = down - old['down'] if down >= old['down'] else down
            db.execute('''INSERT INTO traffic VALUES(?,?,?,?,?) ON CONFLICT(minute,tag)
                DO UPDATE SET up=up+excluded.up,down=down+excluded.down''', (now // 60 * 60, tag, proto, du, dd))
        elif old and now - old['ts'] > INTERVAL * 5:
            gaps += 1  # Do not assign an unknown downtime interval to today's traffic.
        db.execute('INSERT OR REPLACE INTO counters VALUES(?,?,?,?,?)', (tag, proto, up, down, now))
    if gaps:
        previous = db.execute("SELECT value FROM meta WHERE key='gaps'").fetchone()
        db.execute("INSERT OR REPLACE INTO meta VALUES('gaps',?)", (str(int(previous[0]) + gaps if previous else gaps),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('sample_at',?)", (str(now),))


LOG_LINE = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+.*?\b(accepted|rejected)\s+(?:tcp:|udp:)?(\S+)\s+\[([^\s\]]+)\s*(?:->|>>)\s*[^\]]+\]')


def parse_access(line, tags, now):
    if len(line) > 8192:
        return None
    match = LOG_LINE.search(line)
    if not match or match[4] not in tags:
        return None
    stamp, outcome, destination, tag = match.groups()
    host, sep, port = destination.rpartition(':')
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    host = host.strip('[]').rstrip('.').lower()
    try:
        host, kind = str(ipaddress.ip_address(host)), 'ip'
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            return None
        if not 1 <= len(host) <= 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
            return None
        kind = 'domain'
    try:
        stamp = int(dt.datetime.strptime(stamp, '%Y/%m/%d %H:%M:%S').timestamp())
    except ValueError:
        return None
    if stamp > now + 300:
        return None
    return stamp, tags[tag], host, kind, int(port), outcome


def ingest(db, log_path, tags, now, retention_days, start_from_end=True):
    path = Path(log_path)
    with path.open('rb') as file:
        info = os.fstat(file.fileno())
        identity = f'{path}:{info.st_dev}:{info.st_ino}'
        previous = db.execute("SELECT value FROM meta WHERE key='log_cursor'").fetchone()
        cursor = json.loads(previous[0]) if previous else None
        offset = cursor['offset'] if cursor and cursor['identity'] == identity and cursor['offset'] <= info.st_size else 0
        if cursor is None and start_from_end:
            offset = info.st_size  # Existing files do not retroactively become dashboard history.
        file.seek(offset)
        count = 0
        # Bound work per polling cycle. Save offset in the same transaction as events.
        while file.tell() - offset < 1024 * 1024:
            start = file.tell()
            raw = file.readline(8193)
            if not raw:
                break
            if not raw.endswith(b'\n'):
                if len(raw) <= 8192:
                    file.seek(start)
                    break
                # Ignore oversized records, including all continuation chunks.
                while raw and not raw.endswith(b'\n'):
                    raw = file.readline(8193)
                continue
            parsed = parse_access(raw.decode('utf-8', 'replace'), tags, now)
            if parsed and parsed[0] >= now - retention_days * 86400:
                db.execute('INSERT INTO events(ts,protocol,target,kind,port,outcome) VALUES(?,?,?,?,?,?)', parsed)
                count += 1
        db.execute("INSERT OR REPLACE INTO meta VALUES('log_cursor',?)", (json.dumps({'identity': identity, 'offset': file.tell()}),))
        return count


def prune(db, now, retention_days):
    cutoff = now - retention_days * 86400
    db.execute('DELETE FROM traffic WHERE minute<?', (cutoff,))
    db.execute('DELETE FROM events WHERE ts<?', (cutoff,))
    db.execute('DELETE FROM events WHERE id <= COALESCE((SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?),0)', (MAX_EVENTS,))
    db.execute('PRAGMA incremental_vacuum(64)')


class Collector:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.stop = threading.Event()
        self.status = {'traffic': '等待首次采样', 'history': '等待访问日志', 'last_check': None}
        self.guard = threading.Lock()

    def once(self, now=None):
        now = int(time.time() if now is None else now)
        state = json.loads(Path(self.config['state']).read_text())
        status = {'traffic': '正常', 'history': '正常', 'last_check': now}
        with contextlib.closing(connect(self.store)) as db, db:
            try:
                rows = owned_counters(self.config['xui_db'], state)
                sample(db, rows, now)
            except (sqlite3.Error, ValueError, OSError, KeyError):
                rows = []
                status['traffic'] = '无法读取本项目流量；请检查 3x-ui SQLite 和节点状态'
            tags = {r['tag']: r['protocol'] for r in rows}
            if self.config.get('access_log') and tags:
                try:
                    db.execute('SAVEPOINT log_batch')
                    ingest(db, self.config['access_log'], tags, now, self.config['retention_days'],
                           start_from_end=not self.config.get('owned_log', False))
                    db.execute('RELEASE log_batch')
                except (OSError, ValueError):
                    db.execute('ROLLBACK TO log_batch')
                    db.execute('RELEASE log_batch')
                    status['history'] = '访问日志不可读；请检查 Xray 日志路径及权限'
            else:
                status['history'] = '未配置可读访问日志，或当前没有可验证的本项目节点'
            prune(db, now, self.config['retention_days'])
            db.execute("INSERT OR REPLACE INTO meta VALUES('collector_status',?)", (json.dumps(status, ensure_ascii=False),))
        with self.guard:
            self.status = status

    def run(self):
        while not self.stop.is_set():
            try:
                self.once()
            except Exception:
                # Never log exceptions containing source DB contents or credentials.
                with self.guard:
                    self.status = {'traffic': '采集失败，请检查服务与磁盘空间', 'history': '采集暂停', 'last_check': int(time.time())}
                with contextlib.suppress(sqlite3.Error, OSError):
                    with contextlib.closing(connect(self.store)) as db, db:
                        db.execute("INSERT OR REPLACE INTO meta VALUES('collector_status',?)", (json.dumps(self.status, ensure_ascii=False),))
            self.stop.wait(INTERVAL)

    def snapshot(self):
        with self.guard:
            return dict(self.status)


def overview(store, days=1, query='', now=None):
    now = int(time.time() if now is None else now)
    days = days if days in (1, 7, 30) else 1
    since = now - days * 86400
    with contextlib.closing(connect(store)) as db:
        counters = [dict(r) for r in db.execute('SELECT protocol,up,down,ts FROM counters ORDER BY protocol')]
        total = db.execute('SELECT COALESCE(SUM(up),0),COALESCE(SUM(down),0) FROM traffic WHERE minute>=?', (since,)).fetchone()
        bucket = 300 if days == 1 else 3600 if days == 7 else 21600
        points = [dict(r) for r in db.execute('''SELECT (minute / ?) * ? AS ts,SUM(up) AS up,SUM(down) AS down
            FROM traffic WHERE minute>=? GROUP BY ts ORDER BY ts''', (bucket, bucket, since))]
        top = [dict(r) for r in db.execute('''SELECT target,COUNT(*) AS connections FROM events
            WHERE ts>=? AND kind='domain' GROUP BY target ORDER BY connections DESC,target LIMIT 15''', (since,))]
        clause, values = 'ts>=?', [since]
        if query:
            clause += ' AND instr(target,?)>0'
            values.append(query[:253].lower())
        history = [dict(r) for r in db.execute('SELECT ts,protocol,target,kind,port,outcome FROM events WHERE ' + clause + ' ORDER BY ts DESC,id DESC LIMIT 100', values)]
        count = db.execute('SELECT COUNT(*) FROM events WHERE ts>=?', (since,)).fetchone()[0]
        ip_count = db.execute("SELECT COUNT(*) FROM events WHERE ts>=? AND kind='ip'", (since,)).fetchone()[0]
        recent = db.execute('SELECT COALESCE(SUM(up),0),COALESCE(SUM(down),0) FROM traffic WHERE minute>=?', (now // 60 * 60 - 60,)).fetchone()
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('sample_at','gaps','started_at')"))
    return {'now': now, 'days': days, 'totals': {'up': total[0], 'down': total[1], 'connections': count},
            'rates': {'up': recent[0] / 120, 'down': recent[1] / 120}, 'points': points, 'bucket_seconds': bucket,
            'top': top, 'history': history, 'counters': counters, 'ip_only': ip_count, 'meta': meta}



def read_snapshot(days=1, query='', config_path=None):
    config_path = Path(config_path or CONFIG)
    if not config_path.exists():
        raise ValueError('尚未启用采集，请返回 Dashboard 菜单选择“启用 / 更新采集”。')
    config = json.loads(config_path.read_text())
    store = config_path.parent / 'history.sqlite3'
    if not store.is_file():
        raise ValueError('统计数据库尚未创建，请先启用采集。')
    data = overview(store, days, query)
    with contextlib.closing(connect(store)) as db:
        row = db.execute("SELECT value FROM meta WHERE key='collector_status'").fetchone()
    data['status'] = json.loads(row[0]) if row else {'traffic': '等待首次采样', 'history': '等待访问日志', 'last_check': None}
    data['retention_days'] = config['retention_days']
    data['max_events'] = MAX_EVENTS
    return data


def collect(config_path=None):
    config_path = Path(config_path or CONFIG)
    config = json.loads(config_path.read_text())
    store = config_path.parent / 'history.sqlite3'
    init_store(store)
    with contextlib.closing(connect(store)) as db, db:
        db.execute("INSERT OR IGNORE INTO meta VALUES('started_at',?)", (str(int(time.time())),))
    collector = Collector(config, store)
    try:
        collector.run()
    except KeyboardInterrupt:
        collector.stop.set()
