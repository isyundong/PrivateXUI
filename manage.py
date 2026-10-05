#!/usr/bin/env python3
"""One entry point for 3x-ui nodes + a subscription Worker in your account."""
import argparse
import contextlib
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import uuid
from getpass import getpass

from cloudflare_api import Cloudflare
from environment import inspect_server, print_report, require_supported, validate_ipv4
import xui_backend as xui

DEFAULT_STATE = '/etc/x-ui/private-cf/state.json'
WORKER_SOURCE = Path(__file__).with_name('worker.mjs')


from storage import atomic_private_write


def save(path, state):
    atomic_private_write(path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def load(path):
    state = json.loads(Path(path).read_text())
    if state.get('version') != 1 or not re.fullmatch('[0-9a-f]{32}', state.get('deployment_id', '')):
        raise ValueError('不是此合并版的状态文件；不能直接导入旧部署器状态')
    return state


@contextlib.contextmanager
def lock(path):
    if fcntl is None:
        raise ValueError('请在支持 fcntl 的 Linux VPS 上运行部署命令')
    target = Path(path).with_suffix('.lock')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def hostname(value):
    name = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
    if len(name) > 253 or '.' not in name:
        raise ValueError('请输入完整域名，例如 node.example.com')
    for label in name.split('.'):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label):
            raise ValueError('域名格式不正确；不要包含协议、端口或路径')
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    raise ValueError('此处需要域名，不能填写 IP')


def preferred_from_file(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list) or len(data) > 50:
        raise ValueError('优选列表必须为最多 50 项的 JSON 数组')
    result = []
    for item in data:
        address = str(item['address']).strip()
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = hostname(address)
        label = str(item.get('name') or address)
        if len(label) > 100 or any(ord(c) < 32 for c in label):
            raise ValueError('优选地址名称不可包含控制字符，长度不超过 100')
        result.append({'address': address, 'name': label})
    return result


def best_zone(zones, domain):
    matching = [zone for zone in zones if domain == zone['name'] or domain.endswith('.' + zone['name'])]
    if not matching:
        raise ValueError(f'Cloudflare Token 无权访问域名所在 Zone: {domain}')
    return max(matching, key=lambda zone: len(zone['name']))


def cf_client():
    token = os.environ.get('CF_API_TOKEN', '').strip()
    if token:
        print('使用环境变量 CF_API_TOKEN；如需重新输入，请先执行 unset CF_API_TOKEN。')
    else:
        token = getpass('Cloudflare API Token（只粘贴令牌本身，隐藏输入）: ').strip()
    return Cloudflare(token)


def check_cloudflare(domain=None):
    if domain:
        domain = hostname(domain)
    cf = cf_client()
    print('正在只读检查 Cloudflare 域名列表访问权限……')
    zones = cf.listing('/zones')
    print(f'域名列表读取成功，可访问 {len(zones)} 个 Zone。')
    if domain:
        zone = best_zone(zones, domain)
        print(f'{domain} 匹配到授权 Zone：{zone["name"]}')
    print('此检查没有修改节点、DNS、Worker 或部署状态；读取成功不代表所有写入权限已通过。')


def panel_for(backend=None):
    selected, runtime, _ = xui.resolve_backend({'backend': backend} if backend else None)
    panel = xui.setup_panel_client(runtime) if selected == 'api' else None
    return selected, panel


def worker_config(state, token=None):
    return {
        'subscription_domain': state['subscription_domain'], 'domain': state['domain'],
        'uuid': state['uuid'], 'routes': state['routes'], 'preferred': state['preferred'],
        'token_sha256': hashlib.sha256((token or state['subscription_token']).encode()).hexdigest(),
    }


def worker_source():
    if WORKER_SOURCE.is_file():
        return WORKER_SOURCE.read_text()
    # zipimporter reads the asset embedded in the single-file .pyz distribution.
    return __loader__.get_data(str(WORKER_SOURCE)).decode('utf-8')


def upload(cf, state, token=None):
    cf.upload_worker(state['account_id'], state['worker_name'], worker_source(), worker_config(state, token))
    # Custom domain only; explicitly disable workers.dev and preview URLs.
    cf.call('POST', f'/accounts/{state["account_id"]}/workers/scripts/{state["worker_name"]}/subdomain',
            {'enabled': False, 'previews_enabled': False})


def print_links(state):
    base = f'https://{state["subscription_domain"]}/s/{state["subscription_token"]}'
    print(f'状态: {state["status"]}')
    if state['status'] != 'ready':
        print('部署未完成或更新待恢复，请先按 README 处理；以下配置可能尚未生效。')
    print(f'Clash/Mihomo 订阅: {base}')
    print('将上面地址添加到客户端的“订阅 / 配置”，更新后即可选择节点。')
    print(f'其他客户端（Base64，可选）: {base}?format=base64')
    for route in state['routes']:
        print(f'仅 {route["protocol"].upper()} 的 Clash 订阅: {base}?protocol={route["protocol"]}')
    ports = '、'.join(f'{route["protocol"].upper()}={route["port"]}' for route in state['routes'])
    print(f'VPS 需要放行的节点回源 TCP 端口: {ports}')
    print('另保留实际 SSH 端口（默认 22）；通过 SSH 隧道访问面板时，无需额外开放面板端口。')
    print('请核对系统防火墙和云安全组；本工具不会自动修改防火墙。')


def preflight(cf, args):
    domain = hostname(args.node_domain or input('节点域名（如 node.example.com）: '))
    sub_domain = hostname(args.sub_domain or input('订阅域名（如 sub.example.com）: '))
    if domain == sub_domain:
        raise ValueError('节点域名与订阅域名必须不同，以免 Worker 拦截节点流量')
    zones = cf.listing('/zones')
    node_zone, sub_zone = best_zone(zones, domain), best_zone(zones, sub_domain)
    if node_zone['account']['id'] != sub_zone['account']['id']:
        raise ValueError('两个域名必须位于同一 Cloudflare 账号')
    for zone, name in [(node_zone, domain), (sub_zone, sub_domain)]:
        if cf.dns(zone['id'], name):
            raise ValueError(f'{name} 已有 DNS 记录；请使用空闲子域名，工具不会覆盖它')
    account = node_zone['account']['id']
    domains = cf.listing(f'/accounts/{account}/workers/domains')
    if any(item['hostname'] in (domain, sub_domain) for item in domains):
        raise ValueError('目标域名已经绑定 Worker；请使用空闲子域名')
    # Failed reads must stop before any changes, never become an empty ruleset.
    for phase in ('http_request_origin', 'http_config_settings'):
        cf.ruleset(node_zone['id'], phase)
    return domain, sub_domain, node_zone, sub_zone


def install(cf, args):
    if Path(args.state).exists():
        raise ValueError('已有状态文件。请先 show 查看，或 uninstall 清理，不会覆盖原部署。')
    # Validate/cloud preflight before fresh installation or node modifications.
    domain, sub_domain, zone, sub_zone = preflight(cf, args)
    protocols = xui.parse_protocol_selection(args.protocols)
    preferred = preferred_from_file(args.preferred)
    if args.fresh:
        if xui.is_xui_installed():
            raise ValueError('已有 3x-ui，请去掉 --fresh')
        xui.default_ports(protocols, set())
        xui.ensure_xui_for_fresh_setup()
    if not xui.is_xui_installed():
        raise ValueError('未找到本机 3x-ui；请先安装，或在裸机使用 --fresh')
    backend, panel = panel_for()
    if backend == 'api':
        ports = xui.load_existing_ports_api(panel)
    else:
        with contextlib.closing(sqlite3.connect(xui.DB_PATH)) as conn:
            ports = xui.load_existing_ports_db(conn)
    allocated = xui.default_ports(protocols, ports)
    ip = str(ipaddress.IPv4Address(args.ipv4 or xui.get_public_ipv4()))
    deployment_id = uuid.uuid4().hex
    credential = str(uuid.uuid4())
    short_id = credential[:8]
    state = {
        'version': 1, 'deployment_id': deployment_id, 'status': 'installing',
        'domain': domain, 'subscription_domain': sub_domain, 'backend': backend,
        'zone_id': zone['id'], 'sub_zone_id': sub_zone['id'], 'sub_zone_name': sub_zone['name'],
        'account_id': zone['account']['id'], 'worker_name': 'private-xui-' + deployment_id,
        'uuid': credential, 'short_id': short_id, 'subscription_token': secrets.token_urlsafe(32),
        'preferred': preferred, 'ipv4': ip, 'inbound_ids': [], 'worker_attempted': False,
        'routes': [{'protocol': proto, 'port': port, 'path': f'/{uuid.uuid4().hex}-{xui.PROTOCOL_SUFFIX[proto]}'}
                   for proto, port in zip(protocols, allocated)],
        'tags': [f'{short_id}-{proto}' for proto in protocols],
    }
    name, account = state['worker_name'], state['account_id']
    if cf.call('GET', f'/accounts/{account}/workers/scripts/{name}/settings', missing_ok=True) is not None:
        raise ValueError('Worker 名称已存在，停止以防覆盖')
    save(args.state, state)  # journal exists BEFORE the first node/cloud mutation
    try:
        for route in state['routes']:
            ids = xui.create_inbounds(backend, credential, short_id, [route], panel=panel)
            state['inbound_ids'].extend(ids)
            save(args.state, state)
        # Recheck just before creating resources: no reuse/overwrite of DNS records.
        if cf.dns(zone['id'], domain):
            raise ValueError('节点 DNS 在部署期间被创建，停止并回滚本次资源')
        cf.call('POST', f'/zones/{zone["id"]}/dns_records', {
            'type': 'A', 'name': domain, 'content': ip, 'proxied': True, 'ttl': 1,
            'comment': 'private-xui:' + deployment_id,
        })
        for route in state['routes']:
            cf.add_rule(zone['id'], 'http_request_origin', {
                'ref': f'private_xui_{deployment_id}_{route["protocol"]}',
                'description': f'Private XUI {deployment_id} {route["protocol"]}', 'enabled': True,
                'expression': f'(http.host eq "{domain}" and http.request.uri.path eq "{route["path"]}")',
                'action': 'route', 'action_parameters': {'origin': {'port': route['port']}},
            })
        cf.add_rule(zone['id'], 'http_config_settings', {
            'ref': f'private_xui_{deployment_id}_ssl', 'description': f'Private XUI {deployment_id} SSL',
            'enabled': True, 'expression': f'(http.host eq "{domain}")',
            'action': 'set_config', 'action_parameters': {'ssl': 'flexible'},
        })
        state['worker_attempted'] = True
        save(args.state, state)
        upload(cf, state)
        if cf.dns(sub_zone['id'], sub_domain):
            raise ValueError('订阅域名在部署期间出现 DNS 记录，停止以防覆盖')
        if any(item['hostname'] == sub_domain for item in cf.listing(f'/accounts/{account}/workers/domains')):
            raise ValueError('订阅域名在部署期间被其他 Worker 绑定，停止以防覆盖')
        cf.call('PUT', f'/accounts/{account}/workers/domains', {
            'hostname': sub_domain, 'service': name, 'environment': 'production',
            'zone_id': sub_zone['id'], 'zone_name': sub_zone['name'],
        })
        state['status'] = 'ready'
        save(args.state, state)
    except (Exception, SystemExit, KeyboardInterrupt):
        state['status'] = 'incomplete'
        save(args.state, state)
        print('部署中断，已保存恢复状态。正在尝试清理本次创建的资源……')
        failures = cleanup(cf, state, args.state, panel=panel)
        if failures:
            print('部分资源清理失败；保留状态文件，请排除错误后执行 uninstall。')
        raise
    print('配置已提交。自定义域名证书/DNS 生效可能需要等待；尚未做真实节点连通性测试。')
    print_links(state)


def owned_inbound_ids(state, panel=None):
    if state['backend'] == 'api':
        rows = panel.list_inbounds()
    else:
        with contextlib.closing(sqlite3.connect(xui.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute('SELECT id, tag, remark, protocol, port, settings FROM inbounds')]
    found = []
    for row in rows:
        if not (row.get('tag') in state['tags'] or row.get('remark') in state['tags']):
            continue
        if not any(row.get('protocol') == route['protocol'] and int(row.get('port', 0)) == route['port'] for route in state['routes']):
            raise ValueError('本项目入站的协议/端口已被修改，保留状态，请人工核对')
        settings = row.get('settings') or '{}'
        settings = json.loads(settings) if isinstance(settings, str) else settings
        if any(client.get('id') == state['uuid'] or client.get('password') == state['uuid'] for client in settings.get('clients', [])):
            found.append(int(row['id']))
        else:
            raise ValueError('本项目入站的客户端凭据已被修改，保留状态，请人工核对')
    return found


def cleanup(cf, state, path, panel=None):
    failures = []
    def attempt(label, action):
        try:
            action()
        except (Exception, SystemExit) as exc:
            failures.append(label)
            print(f'{label} 清理失败: {exc}')
    account, name = state['account_id'], state['worker_name']
    def remove_worker():
        if not state['worker_attempted']:
            return
        domains = cf.listing(f'/accounts/{account}/workers/domains')
        if any(item['service'] == name and item['hostname'] != state['subscription_domain'] for item in domains):
            raise ValueError('Worker 绑定了额外域名，需要人工检查；不会删除')
        for item in domains:
            if item['hostname'] == state['subscription_domain'] and item['service'] == name:
                cf.call('DELETE', f'/accounts/{account}/workers/domains/{item["id"]}', missing_ok=True)
        cf.call('DELETE', f'/accounts/{account}/workers/scripts/{name}', missing_ok=True)
    attempt('订阅 Worker', remove_worker)
    prefix = 'private_xui_' + state['deployment_id']
    attempt('回源规则', lambda: cf.remove_rules(state['zone_id'], 'http_request_origin',
            {f'{prefix}_{route["protocol"]}' for route in state['routes']}))
    attempt('节点 SSL 规则', lambda: cf.remove_rules(state['zone_id'], 'http_config_settings', {prefix + '_ssl'}))
    def remove_dns():
        for item in cf.dns(state['zone_id'], state['domain']):
            if item.get('comment') == 'private-xui:' + state['deployment_id']:
                cf.call('DELETE', f'/zones/{state["zone_id"]}/dns_records/{item["id"]}', missing_ok=True)
    attempt('节点 DNS', remove_dns)
    def remove_inbounds():
        nonlocal panel
        if state['backend'] == 'api' and panel is None:
            _, panel = panel_for(state['backend'])
        ids = owned_inbound_ids(state, panel)
        if ids:
            xui.delete_managed_inbounds(state['backend'], ids, [], panel=panel)
        else:
            # A previous delete may have committed before service restart failed.
            if state['backend'] == 'db':
                xui.restart_xui_service()
    attempt('3x-ui 入站', remove_inbounds)
    if failures:
        state['status'] = 'cleanup-needed'
        state['cleanup_failures'] = failures
        save(path, state)
    else:
        Path(path).unlink(missing_ok=True)
    return failures


def update_worker(cf, state, path, *, rotate=False, preferred=None):
    if state['status'] not in ('ready', 'update-pending'):
        raise ValueError('部署未完成，不能更新 Worker；请先清理失败部署')
    if state['status'] == 'update-pending':
        # Retrying an ambiguous upload reuses the exact pending token/config.
        token = state['pending_token']
    else:
        token = secrets.token_urlsafe(32) if rotate else state['subscription_token']
        state['pending_token'] = token
        if preferred is not None:
            state['preferred'] = preferred
        state['status'] = 'update-pending'
        save(path, state)
    upload(cf, state, token)
    state['subscription_token'] = token
    state.pop('pending_token', None)
    state['status'] = 'ready'
    save(path, state)
    print_links(state)


def parser():
    result = argparse.ArgumentParser(description='自有域名的 3x-ui 节点 + 私有订阅 Worker')
    result.add_argument('--state', default=DEFAULT_STATE, help='私有状态文件路径')
    commands = result.add_subparsers(dest='command')
    create = commands.add_parser('install', help='配置节点并部署自己的订阅 Worker')
    create.add_argument('--node-domain')
    create.add_argument('--sub-domain')
    create.add_argument('--ipv4', help='VPS 公网 IPv4；省略时自动查询')
    create.add_argument('--protocols', default='vless,trojan,vmess',
                        help='协议列表；固定回源 TCP 端口：vless=17001，trojan=17002，vmess=17003')
    create.add_argument('--preferred', help='自有优选地址 JSON 文件（可选）')
    create.add_argument('--fresh', action='store_true', help='裸机先安装固定版本来源的 3x-ui 安装器')
    commands.add_parser('show', help='只读显示订阅链接')
    commands.add_parser('uninstall', help='只清理本项目创建的资源；保留 3x-ui 面板')
    commands.add_parser('rotate-token', help='更换订阅令牌，使旧订阅 URL 失效')
    update = commands.add_parser('update-worker', help='更新 Worker 代码/优选列表，或重试中断的更新')
    update.add_argument('--preferred', help='省略则保留当前列表；空数组文件可清空')
    check = commands.add_parser('check', help='检查服务器 IP 和系统环境，不部署')
    check.add_argument('--local-only', action='store_true', help='只检查本机环境，不查询公网 IP')
    check.add_argument('--quiet', action='store_true', help='检查通过时不输出')
    commands.add_parser('panel', help='查看本工具保存的面板访问信息')
    cloud = commands.add_parser('check-cloudflare', help='只读检查 Cloudflare 凭据和域名访问权限')
    cloud.add_argument('--domain', help='可选：检查节点域名所在 Zone 是否在授权范围内')
    return result


def run_command(args):
    if args.command == 'show':
        print_links(load(args.state))
        return
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('请在目标 VPS 上使用 sudo 运行修改命令')
    with lock(args.state):
        cf = cf_client()
        if args.command == 'install':
            install(cf, args)
        else:
            state = load(args.state)
            if args.command == 'uninstall':
                if cleanup(cf, state, args.state):
                    raise ValueError('清理未完成，状态文件保留供重试')
                print('已清理本项目资源；3x-ui 面板和其他节点保留。')
            else:
                preferred = preferred_from_file(args.preferred) if getattr(args, 'preferred', None) else None
                update_worker(cf, state, args.state, rotate=args.command == 'rotate-token', preferred=preferred)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command is None:
            from wizard import menu
            menu(args.state)
            return
        if args.command == 'check':
            report = inspect_server(lookup_ip=not args.local_only)
            if not args.quiet or not report['supported']:
                print_report(report)
            require_supported(report)
            return
        if args.command == 'panel':
            from wizard import panel_information
            panel_information()
            return
        if args.command == 'check-cloudflare':
            check_cloudflare(args.domain)
            return
        if args.command == 'install':
            report = inspect_server(lookup_ip=not args.ipv4)
            print_report(report)
            require_supported(report)
            if args.ipv4:
                args.ipv4 = validate_ipv4(args.ipv4)
            else:
                from wizard import ask
                if not sys.stdin.isatty():
                    raise ValueError('非交互安装必须指定 --ipv4，避免误用 NAT/代理出口 IP')
                args.ipv4 = ask('确认服务器公网入站 IPv4', validate_ipv4, default=report['ipv4'] or '')
        run_command(args)
    except KeyboardInterrupt:
        raise SystemExit('\n已取消。若部署已开始，请查看状态或重试卸载清理。') from None
    except EOFError:
        raise SystemExit('未收到输入，请在交互终端中运行。') from None
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
