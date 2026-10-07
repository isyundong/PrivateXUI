"""Project-scoped SOCKS5 egress. Credentials never enter the subscription Worker.

Only the local 3x-ui SQLite template is edited. A private write-ahead journal
survives interruption; retries merge owned objects into the current template,
never restore an old global configuration or silently fall back to direct.
"""
import contextlib
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from storage import atomic_private_write


def address(value):
    if not isinstance(value, str):
        raise ValueError('SOCKS5 主机应为 IP 或域名')
    value = value.strip()
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]
    try:
        ip = ipaddress.ip_address(value)
        if ip.is_unspecified or ip.is_multicast or '%' in value:
            raise ValueError()
        return str(ip)
    except ValueError:
        try:
            value = value.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            value = ''
        if not 1 <= len(value) <= 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.')):
            raise ValueError('SOCKS5 主机格式不正确；仅填 IP 或域名，不含协议、账号、端口或路径') from None
        # Invalid numeric IPs should not masquerade as DNS names.
        if re.fullmatch(r'[0-9.]+', value):
            raise ValueError('SOCKS5 IP 地址无效')
        return value


def port(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,5}', str(value)) or not 1 <= int(value) <= 65535:
        raise ValueError('SOCKS5 端口必须为 1–65535 的整数')
    return int(value)


def credential(value):
    if not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 255 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('认证字段应为 1–255 个 UTF-8 字节，且不含控制字符')
    return value  # Spaces and case are significant in SOCKS credentials.


def normalize(data):
    if not isinstance(data, dict) or set(data) - {'address', 'port', 'username', 'password'}:
        raise ValueError('后置出口配置应包含 address、port 和可选 username、password')
    result = {'address': address(data.get('address')), 'port': port(data.get('port'))}
    user, password = data.get('username', ''), data.get('password', '')
    if user or password:
        result.update(username=credential(user), password=credential(password))
    elif not isinstance(user, str) or not isinstance(password, str):
        raise ValueError('SOCKS5 认证字段必须为字符串')
    return result


def read_config(path):
    try:
        with open(path, encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
                raise ValueError('出口配置应为不超过 16 KiB、权限 600 的普通 JSON 文件')
            data = json.load(stream)
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError('无法解析后置出口 JSON；错误内容已隐藏以保护凭据') from None
    return normalize(data)


def endpoint(config):
    host = config['address']
    return ('[' + host + ']' if ':' in host else host) + ':' + str(config['port'])


def tag_for(state):
    value = state.get('deployment_id', '')
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('部署标识无效，不能管理后置出口')
    return 'private-xui-egress-' + value


def config_of(bundle):
    if not bundle:
        return None
    server = bundle['outbound']['settings']['servers'][0]
    config = {'address': server['address'], 'port': server['port']}
    if server.get('users'):
        config.update(username=server['users'][0]['user'], password=server['users'][0]['pass'])
    return normalize(config)


def bundle_for(state, config, rows, client_uuid=None, *, v3=False):
    import xui_backend as xui
    tags = [r['tag'] for r in rows]
    if not tags or any(not isinstance(t, str) or not t or any(ord(c) < 32 for c in t) for t in tags):
        raise ValueError('本项目入站标签为空或无效；拒绝创建全局出口规则')
    config = normalize(config)
    server = {'address': config['address'], 'port': config['port']}
    if 'username' in config:
        server['users'] = [{'user': config['username'], 'pass': config['password']}]
    tag = tag_for(state)
    client_uuid = client_uuid or str(uuid.uuid4())
    if client_uuid == state['uuid']:
        raise ValueError('后置身份必须与无后置身份不同')
    clients = []
    for row in rows:
        email = 'px' + client_uuid.replace('-', '') + xui.PROTOCOL_SUFFIX[row['protocol']]
        clients.append({'inbound_id': row['id'], 'tag': row['tag'], 'protocol': row['protocol'],
                        'entry': xui.inbound_client_entry(row['protocol'], client_uuid, email, v3=v3)})
    return {'outbound': {'tag': tag, 'protocol': 'socks', 'settings': {'servers': [server]}},
            'rule': {'type': 'field', 'inboundTag': tags, 'user': [c['entry']['email'] for c in clients], 'outboundTag': tag},
            'client_uuid': client_uuid, 'clients': clients}


def connect(db_path, readonly=False):
    return sqlite3.connect(Path(db_path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5)


def owned_rows(db_path, state):
    ids = state.get('inbound_ids', [])
    if not ids or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('未找到本项目记录的入站 ID')
    with contextlib.closing(connect(db_path, True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT id,tag,remark,protocol,port,settings FROM inbounds WHERE id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id', ids).fetchall()
    if len(rows) != len(ids):
        raise ValueError('本项目入站已缺失，先检查部署状态')
    tags = []
    for row in rows:
        settings = json.loads(row['settings'])
        credential_matches = any(c.get('id') == state['uuid'] or c.get('password') == state['uuid'] for c in settings.get('clients', []))
        if not credential_matches or not any(row['protocol'] == r['protocol'] and row['port'] == r['port'] for r in state['routes']) or not (row['tag'] in state['tags'] or row['remark'] in state['tags']):
            raise ValueError('入站归属或配置发生变化；后置出口未修改')
        if not row['tag'] or row['tag'] in tags:
            raise ValueError('本项目入站标签为空或重复')
        tags.append(row['tag'])
    # Read the actual persisted tags, including API-created inbound tags.
    return [dict(row) for row in rows]


def owned_tags(db_path, state):
    return [r['tag'] for r in owned_rows(db_path, state)]


def clients_match(db, bundle):
    if not bundle:
        return True
    for client in bundle['clients']:
        row = db.execute('SELECT settings FROM inbounds WHERE id=? AND tag=?', (client['inbound_id'], client['tag'])).fetchone()
        if not row:
            return False
        entries = json.loads(row[0]).get('clients', [])
        if [c for c in entries if c.get('email') == client['entry']['email']] != [client['entry']]:
            return False
        import xui_backend as xui
        if xui.has_v3_client_schema(db):
            row = db.execute('SELECT id,uuid,password,enable FROM clients WHERE email=?', (client['entry']['email'],)).fetchone()
            if not row or row[1:] != (client['entry'].get('id', ''), client['entry'].get('password', ''), 1):
                return False
            if db.execute('SELECT inbound_id FROM client_inbounds WHERE client_id=?', (row[0],)).fetchall() != [(client['inbound_id'],)]:
                return False
    return True


def replace_clients(db, old, desired):
    """Change only held client identities; keep existing clients and traffic intact."""
    import xui_backend as xui
    old_clients = {c['inbound_id']: c for c in (old or {}).get('clients', [])}
    new_clients = {c['inbound_id']: c for c in (desired or {}).get('clients', [])}
    v3 = xui.has_v3_client_schema(db)
    for inbound_id in sorted(old_clients.keys() | new_clients.keys()):
        before, after = old_clients.get(inbound_id), new_clients.get(inbound_id)
        row = db.execute('SELECT tag,settings FROM inbounds WHERE id=?', (inbound_id,)).fetchone()
        if row is None and after is None:
            continue  # Whole-project uninstall already deleted this inbound.
        if not row or row[0] != (before or after)['tag']:
            raise ValueError('后置客户端所属入站发生变化，请核对后重试')
        settings = json.loads(row[1])
        entries = settings.get('clients', [])
        email = (before or after)['entry']['email']
        held = [c for c in entries if c.get('email') == email]
        if held != ([before['entry']] if before else []):
            raise ValueError('后置客户端已被手动修改或存在同名账号，未覆盖')
        if after and any(c.get('id') == desired['client_uuid'] or c.get('password') == desired['client_uuid'] for c in entries if c.get('email') != email):
            raise ValueError('后置客户端凭据与现有账号冲突')
        kept = [c for c in entries if c.get('email') != email]
        settings['clients'] = kept + ([after['entry']] if after else [])
        db.execute('UPDATE inbounds SET settings=? WHERE id=?', (json.dumps(settings, ensure_ascii=False), inbound_id))
        if not v3:
            continue
        record = db.execute('SELECT id,uuid,password FROM clients WHERE email=?', (email,)).fetchone()
        if before:
            identity = before['entry'].get('id', '')
            password = before['entry'].get('password', '')
            if not record or record[1:] != (identity, password):
                raise ValueError('3x-ui 客户端身份表与后置恢复记录不一致')
            links = db.execute('SELECT inbound_id FROM client_inbounds WHERE client_id=?', (record[0],)).fetchall()
            if links != [(inbound_id,)]:
                raise ValueError('后置客户端被其他入站引用，未修改')
            if not after:
                db.execute('DELETE FROM client_inbounds WHERE client_id=? AND inbound_id=?', (record[0], inbound_id))
                db.execute('DELETE FROM clients WHERE id=?', (record[0],))
                if xui.table_exists(db, 'client_traffics'):
                    db.execute('DELETE FROM client_traffics WHERE email=? AND inbound_id=?', (email, inbound_id))
        elif after:
            if record:
                raise ValueError('3x-ui 已存在同名后置账号，未覆盖')
            cursor, stamp = db.cursor(), xui.now_ms()
            client_id = xui.upsert_v3_client_record(cursor, after['protocol'], desired['client_uuid'], email, stamp)
            # Do not use link_v3_client_inbound: it deletes the original client's link.
            db.execute('INSERT INTO client_inbounds(client_id,inbound_id,flow_override,created_at) VALUES(?,?,?,?)', (client_id, inbound_id, '', stamp))
            xui.ensure_v3_client_traffic(cursor, db, inbound_id, email)


def read_template(db_path):
    with contextlib.closing(connect(db_path, True)) as db:
        rows = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchall()
    if len(rows) > 1:
        raise ValueError('Xray 模板存在重复记录，请先在面板修复')
    raw = rows[0][0] if rows else None
    if raw is None:
        from dashboard_service import template_from_panel
        try:
            template = template_from_panel()
        except (ValueError, SystemExit):
            raise ValueError('无法读取 Xray 默认模板；请在 3x-ui 保存一次 Xray 设置后重试') from None
    else:
        try:
            template = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError('Xray 模板不是有效 JSON，未作修改') from None
    return template, raw


def objects(template):
    if not isinstance(template, dict):
        raise ValueError('Xray 模板应为 JSON 对象')
    for key in template:
        if key.lower() in ('routing', 'outbounds') and key not in ('routing', 'outbounds'):
            raise ValueError('请在 3x-ui 保存标准小写 routing / outbounds 配置')
    outbounds = template.get('outbounds')
    routing = template.get('routing', {})
    if not isinstance(outbounds, list) or not outbounds or any(not isinstance(o, dict) for o in outbounds) or not isinstance(routing, dict):
        raise ValueError('Xray 模板缺少有效的原有出站，不能更改默认全局出口')
    if any(k.lower() == 'rules' and k != 'rules' for k in routing):
        raise ValueError('请使用标准 routing.rules 配置')
    rules = routing.get('rules', [])
    if not isinstance(rules, list) or any(not isinstance(r, dict) for r in rules):
        raise ValueError('Xray 路由规则格式不支持')
    return outbounds, rules


def references(value, tag):
    if isinstance(value, dict):
        return any(references(v, tag) or (k == 'selector' and isinstance(v, list) and any(isinstance(p, str) and tag.startswith(p) for p in v)) for k, v in value.items())
    if isinstance(value, list):
        return any(references(v, tag) for v in value)
    return value == tag


def matches(template, bundle, tag, *, runtime=False, revoked=None):
    outbounds, rules = objects(template)
    selected = [o for o in outbounds if o.get('tag') == tag]
    routes = [r for r in rules if r.get('outboundTag') == tag]
    if bundle is None:
        if selected or routes:
            return False
        if runtime and revoked:
            for inbound in template.get('inbounds', []):
                for client in inbound.get('settings', {}).get('clients', []):
                    if client.get('id') == revoked['client_uuid'] or client.get('password') == revoked['client_uuid']:
                        return False
        return True
    if runtime:
        selected = [{k: o.get(k) for k in ('tag', 'protocol', 'settings')} for o in selected]
        for expected in bundle['clients']:
            inbounds = [i for i in template.get('inbounds', []) if i.get('tag') == expected['tag']]
            if len(inbounds) != 1 or not any(c.get('email') == expected['entry']['email'] and
                    (c.get('id') == bundle['client_uuid'] or c.get('password') == bundle['client_uuid'])
                    for c in inbounds[0].get('settings', {}).get('clients', [])):
                return False
    return selected == [bundle['outbound']] and routes == [bundle['rule']] and bool(rules) and rules[0] == bundle['rule']


def replace_owned(template, old, desired, tag):
    result = copy.deepcopy(template)
    outbounds, rules = objects(result)
    selected = [o for o in outbounds if o.get('tag') == tag]
    routes = [r for r in rules if r.get('outboundTag') == tag]
    if selected != ([old['outbound']] if old else []) or routes != ([old['rule']] if old else []):
        raise ValueError('后置出口配置已被手动修改或存在同名对象；已保留，请人工核对')
    kept_out = [o for o in outbounds if o.get('tag') != tag]
    kept_rules = [r for r in rules if r.get('outboundTag') != tag]
    if not kept_out:
        raise ValueError('不能移除唯一的默认出站')
    other = copy.deepcopy(result)
    other['outbounds'] = kept_out
    other.setdefault('routing', {})['rules'] = kept_rules
    if references(other, tag):
        raise ValueError('其他配置引用了本项目出口或其标签前缀；已保留，请人工核对')
    result['outbounds'] = kept_out + ([desired['outbound']] if desired else [])
    result.setdefault('routing', {})['rules'] = ([desired['rule']] if desired else []) + kept_rules
    return result


def runtime_config():
    """Inspect the Xray child of this x-ui service, not unrelated Xray instances."""
    result = subprocess.run(['systemctl', 'show', 'x-ui', '--property=MainPID', '--value'], capture_output=True, text=True, timeout=3)
    if result.returncode or not result.stdout.strip().isdigit() or int(result.stdout.strip()) <= 0:
        raise ValueError('无法确认 x-ui / Xray 运行状态')
    pending, visited = [int(result.stdout.strip())], set()
    while pending and len(visited) < 64:
        pid = pending.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        root = Path('/proc') / str(pid)
        try:
            # Go may spawn Xray from any OS thread. /task/<tid>/children is
            # thread-local; looking only at the leader misses real Xray cores.
            for children in root.glob('task/*/children'):
                with contextlib.suppress(OSError):
                    pending.extend(int(n) for n in children.read_text().split())
            exe = Path(os.readlink(root / 'exe'))
            if not exe.name.startswith('xray'):
                continue
            args = (root / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if '-test' in args:
                continue
            paths = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ('-c', '-config', '--config')]
            paths += [a.split('=', 1)[1] for a in args if a.startswith(('-c=', '-config=', '--config='))]
            if len(paths) != 1 or any(a in ('-confdir', '--confdir') or a.startswith(('-confdir=', '--confdir=')) for a in args):
                continue
            path = Path(paths[0])
            if not path.is_absolute():
                path = Path(os.readlink(root / 'cwd')) / path
            config = json.loads(path.read_text())
            started = (root / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            return config, (pid, started), exe
        except (OSError, ValueError, IndexError):
            continue
    raise ValueError('未找到可核实配置的 Xray 进程；后置出口尚未确认生效')


def validate_bundle(bundle, directory):
    if bundle is None:
        return
    try:
        binary = runtime_config()[2]
    except (ValueError, OSError, subprocess.SubprocessError):
        choices = [p for p in Path('/usr/local/x-ui/bin').glob('xray*') if p.is_file() and os.access(p, os.X_OK) and not p.suffix]
        if len(choices) != 1:
            raise ValueError('无法找到 Xray 核心校验出口配置，请先启动 3x-ui') from None
        binary = choices[0]
    candidate = {'log': {'loglevel': 'none'}, 'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}, bundle['outbound']], 'routing': {'rules': [bundle['rule']]}}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, prefix='.egress-test-', suffix='.json') as file:
        json.dump(candidate, file)
        file.flush()
        try:
            result = subprocess.run([str(binary), 'run', '-test', '-config', file.name], capture_output=True, timeout=20, cwd=binary.parent)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Xray 出口配置校验未完成；错误内容已隐藏以保护凭据') from None
        if result.returncode:
            raise ValueError('Xray 核心不接受此 SOCKS5 配置；未修改模板，错误内容已隐藏')


def restart_and_verify(bundle, tag, revoked=None):
    try:
        previous = runtime_config()[1]
    except (ValueError, OSError, subprocess.SubprocessError):
        previous = None
    result = subprocess.run(['systemctl', 'restart', 'x-ui'], capture_output=True, timeout=45)
    if result.returncode:
        raise ValueError('x-ui 重启失败；配置保留为待应用，请在后置出口中重试')
    deadline, stable = time.monotonic() + 12, None
    while time.monotonic() < deadline:
        try:
            config, identity, _ = runtime_config()
            if identity != previous and matches(config, bundle, tag, runtime=True, revoked=revoked):
                if stable == identity:
                    return
                stable = identity
            else:
                stable = None
        except (ValueError, OSError, subprocess.SubprocessError):
            stable = None
        time.sleep(0.5)
    raise ValueError('未确认新 Xray 进程加载后置出口配置；保留待应用记录，不能视为已生效')


def persist(path, state):
    atomic_private_write(path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def apply(state, path, config=None, *, retry=False, cleanup=False):
    """Caller holds manage.lock. An interrupted write is retried, never rolled back."""
    import xui_backend as xui
    tag = tag_for(state)
    db_path = str(Path(xui.DB_PATH).resolve())
    pending = state.get('egress_pending')
    if pending and not retry:
        raise ValueError('有待完成的后置出口设置，请先选择“重试应用”')
    if retry:
        if not pending or pending.get('db_path') != db_path:
            raise ValueError('没有可重试的出口配置，或 3x-ui 数据库位置已改变')
        old, desired = pending['before'], pending['after']
    else:
        if not cleanup and not (state.get('status') == 'ready' or
                                config is None and state.get('status') == 'update-pending'):
            raise ValueError('请先完成或恢复当前部署，再配置后置出口')
        old = state.get('egress')
        with contextlib.closing(connect(db_path, True)) as db:
            v3 = xui.has_v3_client_schema(db)
        desired = bundle_for(state, config, owned_rows(db_path, state), (old or {}).get('client_uuid'), v3=v3) if config is not None else None
    if desired and owned_tags(db_path, state) != desired['rule']['inboundTag']:
        raise ValueError('入站标签在配置期间已改变，后置出口未应用')
    template, original = read_template(db_path)
    already_applied = retry and matches(template, desired, tag)
    if already_applied:
        with contextlib.closing(connect(db_path, True)) as db:
            if not clients_match(db, desired):
                raise ValueError('出口模板与客户端身份不同步，已保留恢复记录，请核对')
    updated = template if already_applied else replace_owned(template, old, desired, tag)
    validate_bundle(desired, Path(path).parent)
    if not retry:
        state['egress_pending'] = {'before': old, 'after': desired, 'db_path': db_path}
        persist(path, state)  # Persist ownership and desired data BEFORE SQLite commit.
    if not already_applied:
        with contextlib.closing(connect(db_path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchall()
            if len(rows) > 1 or (rows[0][0] if rows else None) != original:
                raise ValueError('Xray 模板刚被其他操作修改；未覆盖，请重试应用')
            if desired and owned_tags(db_path, state) != desired['rule']['inboundTag']:
                raise ValueError('入站归属在应用期间已改变，请核对后重试')
            replace_clients(db, old, desired)
            value = json.dumps(updated, ensure_ascii=False)
            if rows:
                db.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig'", (value,))
            else:
                db.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('xrayTemplateConfig', value))
    try:
        restart_and_verify(desired, tag, revoked=old if desired is None else None)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('服务操作未完成；出口配置仍为待应用，请重试，不会自动改为直连') from None
    state.pop('egress_pending', None)
    if desired:
        state['egress'] = desired
    else:
        state.pop('egress', None)
    if not cleanup:
        state['egress_subscription_pending'] = True
    persist(path, state)


def status(state, *, verify_runtime=True):
    import xui_backend as xui
    if state.get('egress_pending'):
        return {'summary': '待应用 / 待恢复', 'tone': 'warning', 'detail': '尚未确认生效；旧运行配置可能仍在使用，请重试应用。'}
    bundle = state.get('egress')
    if not bundle:
        if state.get('egress_subscription_pending'):
            return {'summary': '后置已撤销 · 订阅待发布', 'tone': 'warning', 'detail': '缓存的后置节点已失效；请重试发布订阅，无后置节点不变。'}
        return {'summary': '未配置 · 沿用原路由', 'tone': 'muted', 'detail': '配置后新增后置节点组；无后置节点继续原有路由。'}
    try:
        label = 'SOCKS5 ' + endpoint(config_of(bundle))
        template, _ = read_template(xui.DB_PATH)
        with contextlib.closing(connect(xui.DB_PATH, True)) as db:
            identities_match = clients_match(db, bundle)
        if not identities_match or owned_tags(xui.DB_PATH, state) != bundle['rule']['inboundTag'] or not matches(template, bundle, tag_for(state)):
            return {'summary': label + ' · 配置漂移', 'tone': 'warning', 'detail': '模板或入站已改变，不能保证后置路由；请核对 3x-ui 设置。'}
        if verify_runtime and not matches(runtime_config()[0], bundle, tag_for(state), runtime=True):
            raise ValueError()
        if state.get('egress_subscription_pending'):
            return {'summary': label + ' · 订阅待发布', 'tone': 'warning', 'detail': '服务端出口已配置；请更新订阅服务以同步两类节点。'}
        return {'summary': label, 'tone': 'accent', 'detail': '运行配置已核实；上游可用性需另行检查。' if verify_runtime else '模板配置一致；未检查运行核心。'}
    except (ValueError, OSError, KeyError, TypeError, IndexError, sqlite3.Error, subprocess.SubprocessError):
        return {'summary': 'SOCKS5 · 运行状态未确认', 'tone': 'warning', 'detail': '配置记录保留，请检查 x-ui 与本项目节点，再重试应用。'}


def remove_after_inbounds(state, path):
    """Remove owned egress only AFTER inbound deletion, including interrupted edits."""
    if not state.get('egress') and not state.get('egress_pending'):
        return
    import xui_backend as xui
    with contextlib.closing(connect(xui.DB_PATH, True)) as db:
        ids = state.get('inbound_ids', [])
        if ids and db.execute('SELECT 1 FROM inbounds WHERE id IN (' + ','.join('?' for _ in ids) + ') LIMIT 1', ids).fetchone():
            raise ValueError('本项目入站仍存在，保留 SOCKS5 路由以避免回落原出口')
    template, _ = read_template(xui.DB_PATH)
    pending = state.get('egress_pending', {})
    choices = [state.get('egress'), pending.get('before'), pending.get('after')]
    tag = tag_for(state)
    # An API deletion may prune inboundTag from routing rules. Accept ONLY that
    # exact, harmless pruning after all owned inbounds have been removed.
    with contextlib.closing(connect(xui.DB_PATH, True)) as db:
        remaining = db.execute('SELECT settings FROM inbounds').fetchall()
        linked = db.execute('SELECT c.uuid,c.password,c.email FROM clients c JOIN client_inbounds ci ON ci.client_id=c.id JOIN inbounds i ON i.id=ci.inbound_id').fetchall() if xui.has_v3_client_schema(db) else []
    revoked_ids = {b['client_uuid'] for b in choices if b}
    revoked_emails = {c['entry']['email'] for b in choices if b for c in b['clients']}
    if any(identity in revoked_ids or password in revoked_ids or email in revoked_emails for identity, password, email in linked):
        raise ValueError('其他入站仍关联后置身份，已保留出口，请人工核对')
    for row in remaining:
        if any(c.get('id') in revoked_ids or c.get('password') in revoked_ids or c.get('email') in revoked_emails for c in json.loads(row[0]).get('clients', [])):
            raise ValueError('其他入站仍引用后置身份，已保留出口，请人工核对')
    current_rules = [r for r in objects(template)[1] if r.get('outboundTag') == tag]
    for old in choices:
        old = copy.deepcopy(old)
        if old and len(current_rules) == 1:
            actual = current_rules[0]
            expected = old['rule']
            tags = actual.get('inboundTag', [])
            if (isinstance(tags, list) and set(tags) <= set(expected.get('inboundTag', [])) and
                    {k: v for k, v in actual.items() if k != 'inboundTag'} ==
                    {k: v for k, v in expected.items() if k != 'inboundTag'}):
                old['rule'] = copy.deepcopy(actual)
        try:
            replace_owned(template, old, None, tag)
        except ValueError:
            continue
        work = copy.deepcopy(state)
        work.pop('egress_pending', None)
        work['egress'] = old
        try:
            apply(work, path, cleanup=True)
        finally:
            state.clear()
            state.update(work)
        return
    raise ValueError('后置出口已被手动修改，已保留其配置和恢复记录')


def command(args):
    try:
        return _command(args)
    except sqlite3.Error:
        raise ValueError('3x-ui 数据库操作未完成；请检查数据库与服务，已保留恢复记录') from None


def _command(args):
    """Called under the deployment lock; deliberately independent of Cloudflare."""
    import manage
    from getpass import getpass
    state = manage.load(args.state)
    action = getattr(args, 'action', 'status')
    if action == 'status':
        info = status(state)
        print('后置出口   ' + info['summary'])
        print(info['detail'])
        return
    if action == 'check':
        if state.get('egress_pending') or not state.get('egress'):
            raise ValueError('请先完成后置出口配置，再检查 SOCKS5')
        probe(config_of(state['egress']))
        print('SOCKS5 协商与认证通过；未验证目标网站、公网出口 IP 或 UDP。')
        return
    config = None
    if action == 'configure':
        data = getattr(args, 'config_data', None)
        if data is not None:
            config = normalize(data)
        elif getattr(args, 'config', None):
            config = read_config(args.config)
        else:
            from wizard import ask
            config = {'address': ask('SOCKS5 主机（IP / 域名）', address),
                      'port': ask('SOCKS5 端口', port, default='1080')}
            user = input('用户名（无认证时留空）: ')
            if user:
                config.update(username=credential(user), password=credential(getpass('SOCKS5 密码（隐藏输入）: ')))
            config = normalize(config)
    if action not in ('configure', 'disable', 'retry'):
        raise ValueError('未知的后置出口操作')
    if action == 'disable' and not state.get('egress') and not state.get('egress_pending'):
        print('尚未配置后置出口，当前沿用 3x-ui 原有路由。')
        return
    if not getattr(args, 'yes', False):
        from wizard import confirm
        desired = (state.get('egress_pending') or {}).get('after') if action == 'retry' else None
        description = ('新增后置节点经 SOCKS5 ' + endpoint(config)) if config else ('重试待完成的出口与订阅设置' if action == 'retry' else '撤销后置节点身份，无后置节点继续原有路由')
        print(description + '；重启 x-ui 并同步订阅 Worker，现有连接可能短暂中断。')
        if not confirm('确认应用后置出口设置'):
            return
    if action != 'retry' or state.get('egress_pending'):
        apply(state, args.state, config, retry=action == 'retry')
    elif not state.get('egress_subscription_pending'):
        raise ValueError('没有待重试的后置出口或订阅更新')
    print('后置出口运行配置已确认。' if state.get('egress') else '后置身份已撤销，缓存的后置节点将无法连接。')
    # Publish node UUIDs only AFTER the corresponding server identities/routes exist.
    # Upstream SOCKS credentials are deliberately absent from worker_config().
    try:
        with manage.cf_session() as cf:
            manage.update_worker(cf, state, args.state, quiet_links=True)
    except (Exception, SystemExit, KeyboardInterrupt):
        print('服务端设置已保存，订阅尚未同步；请在后置出口选择“重试应用 / 发布订阅”。')
        raise


def probe(config):
    """SOCKS5 negotiation/auth only; never claims target/UDP/exit-IP verification."""
    config = normalize(config)
    def receive(stream, count):
        result = b''
        while len(result) < count:
            part = stream.recv(count - len(result))
            if not part:
                raise ValueError('SOCKS5 服务提前关闭连接')
            result += part
        return result
    try:
        with socket.create_connection((config['address'], config['port']), timeout=5) as stream:
            method = 2 if 'username' in config else 0
            stream.sendall(bytes((5, 1, method)))
            if receive(stream, 2) != bytes((5, method)):
                raise ValueError('SOCKS5 不支持所选认证方式或拒绝连接')
            if method == 2:
                user, password = config['username'].encode(), config['password'].encode()
                stream.sendall(bytes((1, len(user))) + user + bytes((len(password),)) + password)
                if receive(stream, 2) != b'\x01\x00':
                    raise ValueError('SOCKS5 用户名或密码认证失败')
    except OSError:
        raise ValueError('SOCKS5 连接失败或超时；请核对主机、端口和网络，未切换出口') from None
