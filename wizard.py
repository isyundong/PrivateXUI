"""Chinese interactive flow; imports deployment functions lazily."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

from preferred import DEFAULT_MODE, MODE_LABELS

from environment import inspect_server, print_report, require_supported, validate_ipv4


def ask(label, validator=lambda value: value, *, default='', allow_empty=False):
    while True:
        suffix = ' [回车=%s]' % default if default else ''
        raw = input(label + suffix + ': ').strip() or default
        if not raw and allow_empty:
            return None
        if not raw:
            print('此项不能为空。')
            continue
        try:
            return validator(raw)
        except (ValueError, OSError, KeyError) as exc:
            print(str(exc))


def confirm(label, *, default=False):
    choice = input(label + (' [Y/n]: ' if default else ' [y/N]: ')).strip().lower()
    return choice in ('y', 'yes') or (not choice and default)


def recovery_hint(state_path):
    """Base recovery advice on durable state, not on every arbitrary exception."""
    if not Path(state_path).exists():
        return '没有本工具的节点/Cloudflare 部署状态，无需执行卸载；请修正错误后重试。'
    try:
        state = json.loads(Path(state_path).read_text())
        status = state.get('status')
    except (OSError, ValueError, AttributeError):
        return '恢复状态暂时无法读取，请保留状态文件并检查文件内容/权限，不要直接删除。'
    if status in ('installing', 'incomplete', 'cleanup-needed'):
        return '检测到未完成部署，请选择“卸载”重试清理；恢复状态会保留。'
    if status == 'update-pending':
        return '有待完成的 Worker 更新，请选择“更新订阅服务”重试，将复用待更新令牌；无需卸载。'
    if status == 'ready':
        return '原部署状态仍为已完成；请先修正本次错误，不要仅因凭据错误而卸载已有节点。'
    return '请保留状态文件，先核对部署状态和本次错误。'


def panel_information():
    import xui_backend as xui
    path = Path(xui.PANEL_INFO_PATH)
    if not path.exists():
        print('未找到本工具保存的面板信息。已有面板可在服务器执行 x-ui 管理。')
        return
    info = json.loads(path.read_text())
    print('面板用户名: %s' % info.get('username', ''))
    print('面板密码: %s' % info.get('password', ''))
    print('服务器本机地址: %s' % info.get('access_url_local', ''))
    print('面板默认仅监听本机，请通过 SSH 隧道访问。')
    print('电脑终端执行: ssh -L 2053:127.0.0.1:%s root@服务器公网IP' % info.get('port', 2053))
    print('电脑浏览器打开: http://127.0.0.1:2053%s' % info.get('web_base_path', '/'))


def protocol_selection(value):
    import manage as m
    try:
        return ','.join(m.xui.parse_protocol_selection(value))
    except SystemExit:
        raise ValueError('请输入 1、2、3，用逗号分隔；例如 1,2,3。') from None


def distinct_subscription(node):
    import manage as m
    def validate(value):
        value = m.hostname(value)
        if value == node:
            raise ValueError('订阅域名必须与节点域名不同，例如 sub.example.com。')
        return value
    return validate


def install_args(state_path, address, node, subscription, protocols, installed, name='Private XUI'):
    return argparse.Namespace(command='install', state=state_path, node_domain=node, sub_domain=subscription,
                              ipv4=address, protocols=protocols, preferred=None, preferred_mode=DEFAULT_MODE,
                              subscription_name=name, fresh=not installed, quiet_links=True)


def install_questions(state_path, report, installed):
    """Compact line-mode fallback for terminals without curses."""
    import manage as m
    if Path(state_path).exists():
        print('已有部署或待清理状态。请先查看订阅或到“维护”处理，不会覆盖。')
        return None
    print('\n安装向导（按 Ctrl+C 返回）')
    address = ask('服务器公网入站 IPv4', validate_ipv4, default=report['ipv4'] or '')
    node = ask('节点域名，如 node.example.com', m.hostname)
    subscription = ask('订阅域名，如 sub.example.com', distinct_subscription(node))
    protocols = ask('协议：1=VLESS，2=Trojan，3=VMess，逗号多选', protocol_selection, default='1,2,3')
    print('\n服务器 %s ｜ 节点 %s ｜ 订阅 %s' % (address, node, subscription))
    print('协议 %s ｜ 自动优选：公开域名池 + 动态 IP，客户端测延迟' % protocols)
    print('只请求公开地址池，不向地址源发送节点凭据；公开候选未在你的网络测速。')
    print('面板：' + ('复用已安装的 3x-ui' if installed else '自动安装 3x-ui，仅监听本机'))
    if not confirm('开始部署', default=True):
        print('已取消。')
        return None
    return install_args(state_path, address, node, subscription, protocols, installed)


def subscription_count(state):
    """Count the unfiltered subscription's unique entry/protocol combinations."""
    if state.get('preferred_mode') == 'auto':
        return None
    addresses = {state.get('domain', '')}
    for item in state.get('preferred', []):
        if item.get('address'):
            addresses.add(item['address'])
    addresses.discard('')
    return len(state.get('routes', [])) * len(addresses)


def panel_status(installed):
    if not installed:
        return '未安装'
    try:
        result = subprocess.run(['systemctl', 'is-active', 'x-ui'], capture_output=True, text=True, timeout=2)
        running = result.stdout.strip() == 'active'
        return '已安装 · ' + ('运行中' if running else '服务未运行')
    except (OSError, subprocess.TimeoutExpired):
        return '已安装 · 运行状态待检查'


def domain_only(state):
    return state.get('preferred_mode') != 'auto' and not state.get('preferred')


def dashboard_state(state_path, report, installed):
    """A credential-free view model; green deployment badges mean local state only."""
    import manage as m
    panel = panel_status(installed)
    panel_tone = ('good' if '运行中' in panel else 'danger' if '服务未运行' in panel else 'warning')
    model = {
        'server': {'title': '服务器 / 3x-ui', 'badge': (panel, panel_tone), 'rows': [
            ('系统', report['system'], ''), ('IPv4', report.get('ipv4') or '待填写', ''),
            ('架构', report.get('architecture', '待检查'), 'muted'),
        ]},
        'deployment': {'title': '节点 / 订阅', 'badge': ('尚未部署', 'warning'), 'rows': [
            ('节点', '尚未配置', 'muted'), ('订阅', '尚未配置', 'muted'), ('入口', '尚未配置', 'muted'),
        ]},
        'notice': {'title': '下一步 · 首次部署', 'tone': 'warning', 'lines': [
            '配置你的节点域名和订阅域名，启用自动优选。',
            '选择「部署」开始；输入完成后才会执行安装。',
        ]},
        'focus': 'install',
    }
    if not Path(state_path).exists():
        return model
    try:
        state = m.load(state_path)
    except (OSError, ValueError, AttributeError):
        model['deployment']['badge'] = ('状态文件读取失败', 'danger')
        model['notice'] = {'title': '需要处理 · 恢复记录异常', 'tone': 'danger', 'lines': [
            '请保留状态文件，暂时不要重复安装或删除配置。', '在「维护」检查环境，修复状态后继续。',
        ]}
        model['focus'] = 'maintenance'
        return model
    status = state.get('status')
    labels = {'ready': '已部署 · 本机记录', 'installing': '安装未完成', 'incomplete': '安装未完成',
              'cleanup-needed': '清理未完成', 'update-pending': '更新待恢复'}
    model['deployment']['badge'] = (labels.get(status, '状态待检查'), 'good' if status == 'ready' else 'warning')
    if state.get('preferred_mode') == 'auto':
        entry, entry_tone = '自动优选 · 动态生成', 'good'
    elif domain_only(state):
        entry, entry_tone = '仅域名 · 未启用优选', 'warning'
    else:
        entry = ('固定候选' if state.get('preferred_mode') == 'builtin' else '自有入口') + ' · %s 条配置' % subscription_count(state)
        entry_tone = 'accent'
    model['deployment']['rows'] = [
        ('节点', state.get('domain') or '未配置', ''),
        ('订阅', state.get('subscription_domain') or '未配置', ''),
        ('入口', entry, entry_tone),
    ]
    ports = ' / '.join(str(route.get('port', '')) for route in state.get('routes', []))
    if ports:
        model['server']['rows'][-1] = ('回源', ports, 'muted')
    model['focus'] = 'subscription'
    if status != 'ready':
        task = '维护 → 更新订阅服务' if status == 'update-pending' else '维护 → 卸载 / 清理未完成部署'
        model['notice'] = {'title': '需要处理 · ' + labels.get(status, '部署待检查'), 'tone': 'warning', 'lines': [
            '已保留恢复记录，请先处理当前状态。', task,
        ]}
        model['focus'] = 'maintenance'
    elif domain_only(state):
        model['notice'] = {'title': '尚未启用自动优选', 'tone': 'warning', 'lines': [
            '当前仅域名入口；不会自动扩展优选节点。',
            '按 A 打开设置，或 维护 → 订阅设置 → 自动优选',
        ], 'compact_lines': [
            '当前仅域名入口，自动优选未启用。',
            '按 A 打开自动优选设置（确认后生效）',
        ]}
        model['focus'] = 'maintenance'
        model['shortcut'] = 'auto-settings'
    elif state.get('preferred_mode') == 'auto':
        model['notice'] = {'title': '自动优选已配置', 'tone': 'good', 'lines': [
            '公开域名池 + 动态 IP；数量以客户端更新后的订阅为准。',
            '选择「订阅」复制地址；实际连通性可在「维护」检查。',
        ]}
    else:
        model['notice'] = {'title': '订阅已配置', 'tone': 'accent', 'lines': [
            '当前使用自选入口；切换自动优选需在设置中确认。',
            '选择「订阅」复制地址，或到「维护」更改入口。',
        ]}
    if installed and panel_tone == 'danger':
        model['notice'] = {'title': '需要处理 · 3x-ui 服务未运行', 'tone': 'danger', 'lines': [
            '节点进程可能不可用，请先检查服务状态。',
            '在服务器执行 systemctl status x-ui 查看原因。',
        ]}
        model['focus'] = 'maintenance'
    return model


def status_lines(state_path, report, installed):
    """Never include credentials in the persistent dashboard."""
    import manage as m
    lines = ['服务器   %s · %s' % (report['system'], report['ipv4'] or 'IP 待填写'),
             '3x-ui    ' + panel_status(installed)]
    if not Path(state_path).exists():
        return lines + ['本项目   未部署', '订阅     尚未配置']
    try:
        state = m.load(state_path)
    except (OSError, ValueError, AttributeError):
        return lines + ['本项目   状态读取失败 · 请保留恢复文件', '恢复     在“维护”检查后重试']
    labels = {'ready': '已部署', 'installing': '安装未完成', 'incomplete': '安装未完成',
              'cleanup-needed': '清理未完成', 'update-pending': '订阅更新待恢复'}
    lines.append('本项目   %s（本机记录）' % labels.get(state.get('status'), '状态待检查'))
    count = subscription_count(state)
    quantity = ('动态优选 · 数量以订阅为准' if count is None else
                '仅域名入口 · 尚未启用自动优选' if domain_only(state) else '%s 条节点配置' % count)
    lines.append('订阅     %s · %s' % (state.get('subscription_domain', '未配置'), quantity))
    protocols = ' / '.join(str(route.get('protocol', '')).upper() for route in state.get('routes', []))
    lines.append('节点     %s · %s' % (state.get('domain', '未配置'), protocols or '未创建'))
    if domain_only(state):
        lines.append('下一步   维护 → 订阅设置 → 自动优选')
    return lines


def show_subscription(state_path):
    import manage as m
    state = m.load(state_path)
    if state.get('status') != 'ready':
        print('部署尚未完成，请先在“维护”中恢复；当前订阅可能不可用。')
    print('%s · Clash / Mihomo' % state.get('subscription_name', 'Private XUI'))
    count = subscription_count(state)
    if count is None:
        print('自动优选：节点数量随公开地址池变化，以客户端更新后的订阅为准。')
    elif domain_only(state):
        print('当前仅域名入口，共 %s 种协议；尚未启用自动优选。' % len(state.get('routes', [])))
        print('启用路径：维护 → 订阅设置 → 自动优选。')
    else:
        print('共 %s 条节点配置；不同 CF 入口共用同一台 VPS。' % count)
    print('\n' + m.subscription_url(state))
    print('\n复制上方完整地址导入客户端；此地址包含访问令牌，请勿公开。')


def operation(callback, state_path):
    try:
        callback()
    except FileNotFoundError:
        print('未找到部署状态或文件，请先部署，或检查文件路径。')
    except KeyboardInterrupt:
        print('\n已取消当前操作。')
        print(recovery_hint(state_path))
    except EOFError:
        print('未收到输入，已返回。')
    except (ValueError, OSError, RuntimeError, KeyError, SystemExit) as exc:
        print('操作未完成：%s' % exc)
        print(recovery_hint(state_path))


class GuidedUI:
    def __init__(self, state_path, report, ui):
        self.state_path = state_path
        self.report = report
        self.ui = ui

    def run(self):
        import manage as m
        import tui
        while True:
            installed = m.xui.is_xui_installed()
            selected = self.ui.home(dashboard_state(self.state_path, self.report, installed), [
                ('install', '部署', '首次安装节点和私有订阅；已有部署会保留'),
                ('subscription', '订阅', '查看完整 Clash / Mihomo 订阅地址'),
                ('maintenance', '维护', '更新、订阅设置、连接检查与清理'),
                ('exit', '退出', '退出 Private XUI，已部署的服务继续运行'),
            ])
            if selected in (None, 'exit'):
                return
            try:
                if selected == 'install':
                    self.install(installed)
                elif selected == 'subscription':
                    self.task(lambda: show_subscription(self.state_path))
                elif selected == 'dashboard':
                    self.dashboard_menu()
                elif selected == 'auto-settings':
                    self.settings(suggest_auto=True)
                else:
                    self.maintenance()
            except tui.Cancelled:
                pass
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                self.task(lambda error=exc: (_ for _ in ()).throw(error))

    def task(self, callback):
        self.ui.console(lambda: operation(callback, self.state_path))

    def command(self, command, **options):
        import manage as m
        args = argparse.Namespace(command=command, state=self.state_path, quiet_links=True, **options)
        self.task(lambda: m.run_command(args))

    def install(self, installed):
        import manage as m
        if Path(self.state_path).exists():
            self.task(lambda: print('已有部署或待恢复记录，不会重复安装。\n查看订阅请选择“订阅”；恢复或清理请选择“维护”。'))
            return
        address = self.ui.ask('部署 · 1 / 5', '服务器公网入站 IPv4', validate_ipv4,
                              default=self.report['ipv4'] or '', hint='确认是这台 VPS 的入站地址。')
        node = self.ui.ask('部署 · 2 / 5', '节点域名', m.hostname, hint='例如 node.example.com')
        subscription = self.ui.ask('部署 · 3 / 5', '订阅域名', distinct_subscription(node), hint='例如 sub.example.com')
        protocols = self.ui.ask('部署 · 4 / 5', '协议：1 VLESS / 2 Trojan / 3 VMess', protocol_selection,
                                default='1,2,3', hint='逗号多选；回源端口分别为 17001 / 17002 / 17003。')
        name = self.ui.ask('部署 · 5 / 5', '客户端显示的订阅名称', subscription_name, default='Private XUI')
        if not self.ui.confirm('确认部署', [
            '服务器   ' + address, '节点     ' + node, '订阅     ' + subscription,
            '协议     ' + protocols, '入口     自动优选：公开域名池 + 动态 IP',
            '来源     只取公开地址；不向源发送节点凭据',
            '面板     ' + ('复用现有 3x-ui' if installed else '自动安装，仅监听本机'),
        ]):
            return
        args = install_args(self.state_path, address, node, subscription, protocols, installed, name)
        self.task(lambda: m.run_command(args))

    def maintenance(self):
        import manage as m
        while True:
            selected = self.ui.choose('维护', [
                ('update', '更新订阅服务', '更新 Worker，保留名称、节点和自有优选列表'),
                ('settings', '订阅设置', '修改显示名称与 CF 入口；无需手工编辑 JSON'),
                ('check', '连接与凭据检查', '检查节点回源、Cloudflare 权限或系统环境'),
                ('panel', '查看面板访问', '显示面板账号和 SSH 隧道访问方法'),
                ('rotate', '更换订阅令牌', '使旧订阅地址失效，完成后需重新导入客户端'),
                ('dashboard', 'Dashboard · 流量与历史', '终端查看流量趋势、连接记录与热门域名'),
                ('uninstall', '卸载 / 清理未完成部署', '只清理本项目；保留 3x-ui 和其他节点'),
            ], subtitle='Esc 返回首页')
            if selected is None:
                return
            if selected == 'update':
                self.command('update-worker', preferred=None, preferred_mode=None, subscription_name=None)
            elif selected == 'settings':
                self.settings()
            elif selected == 'dashboard':
                self.dashboard_menu()
            elif selected == 'panel':
                self.task(panel_information)
            elif selected == 'check':
                self.checks()
            elif selected == 'rotate':
                if self.ui.confirm('更换订阅令牌', ['旧订阅链接将失效。', '完成后从首页“订阅”复制新地址并重新导入。'], destructive=True):
                    self.command('rotate-token')
            elif selected == 'uninstall':
                state = m.load(self.state_path)
                if self.ui.confirm('卸载本项目', ['节点   ' + state['domain'], '订阅   ' + state['subscription_domain'],
                                               '清理本项目节点、Worker、DNS、规则及 Dashboard 采集。', '保留 3x-ui 面板及其他节点。'], destructive=True):
                    self.command('uninstall')

    def dashboard_menu(self):
        import dashboard_service as service
        import dashboard as dash
        while True:
            installed = dash.CONFIG.exists()
            action = self.ui.choose('Dashboard · 终端流量与历史', [
                ('open', '打开 Dashboard', '终端内查看概览、热门域名和连接历史'),
                ('install', '启用 / 更新采集', '后台保存统计；没有网页和监听端口'),
                ('status', '查看采集状态', '查看本机采集服务状态和保留时间'),
                ('uninstall', '停止并卸载采集', '保留统计历史；不删除节点和订阅'),
            ], summary=['状态   ' + ('已有本机配置' if installed else '尚未启用'),
                        '默认保留 30 天，连接记录最多 10 万条。'], initial='open' if installed else 'install')
            if action is None:
                return
            if action == 'open':
                self.ui.dashboard(dash.read_snapshot)
                continue
            if action == 'install':
                if not self.ui.confirm('启用后台采集', [
                    '记录本项目流量与目标域名/IP，供 TUI 查看。',
                    '若需开启 Xray 全局访问日志，将重启 x-ui，短暂断线。',
                    'TUI 只统计本项目；短期原始日志可能包含其他入站。',
                    '已有日志复用；新建原始日志配置轮转。']):
                    continue
            if action == 'uninstall' and not self.ui.confirm('卸载采集', ['停止采集，历史数据保留。', '按需恢复自己修改的日志字段；可能短暂重启 x-ui。'], destructive=True):
                continue
            args = argparse.Namespace(state=self.state_path, action=action, retention_days=30, access_log=None, yes=True)
            self.task(lambda: service.command(args))

    def settings(self, *, suggest_auto=False):
        import manage as m
        state = m.load(self.state_path)
        name = state.get('subscription_name') or 'Private XUI'
        if not suggest_auto:
            name = self.ui.ask('订阅设置', '客户端显示的订阅名称', subscription_name, default=name)
        mode = self.ui.choose('订阅入口', [
            ('keep', '保留当前入口', '只修改名称；保留现有自定义地址'),
            ('auto', '自动优选（推荐）', '公开域名池 + 动态 IP；不向公开源发送节点凭据'),
            ('builtin', '内置 Cloudflare 候选', '固定候选 + 域名入口；客户端测延迟，共用同一 VPS'),
            ('direct', '仅节点域名', '每个已安装协议生成 1 条节点配置'),
            ('custom', '导入自有地址文件', '高级选项：使用已有的优选 JSON 文件'),
        ], summary=['候选入口需要客户端测速，不保证每个网络都可用。'],
           initial='auto' if suggest_auto else 'keep')
        if mode is None:
            return
        preferred = None
        if mode == 'custom':
            preferred = self.ui.ask('导入优选', '服务器上的 JSON 文件路径',
                                    lambda value: (m.preferred_from_file(value), value)[1])
        if self.ui.confirm('更新订阅设置', ['名称   ' + name, '入口   ' + ('保留当前' if mode == 'keep' else MODE_LABELS[mode])]):
            self.command('update-worker', preferred=preferred, preferred_mode=None if mode == 'keep' else mode,
                         subscription_name=name)

    def checks(self):
        import manage as m
        selection = self.ui.choose('检查', [
            ('connectivity', '节点连接', '通过 Cloudflare 检查回源握手；成功时不再提醒开放端口'),
            ('cloudflare', 'Cloudflare 凭据', '只读验证 Token 及域名访问范围'),
            ('server', '服务器环境', '重新检查系统和公网出口 IP'),
        ])
        if selection == 'connectivity':
            self.command('check-connectivity')
        elif selection == 'cloudflare':
            domain = self.ui.ask('Cloudflare 检查', '域名（可留空）', m.hostname, allow_empty=True,
                                 hint='验证使用的 Token 会在本次会话中复用。')
            self.task(lambda: m.check_cloudflare(domain))
        elif selection == 'server':
            def check():
                self.report = inspect_server()
                print_report(self.report)
                require_supported(self.report)
            self.task(check)


def subscription_name(value):
    import unicodedata
    value = value.strip()
    if not value or len(value) > 80 or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('名称须为 1–80 个可见字符。')
    return value


def text_menu(state_path, report):
    """Accessible fallback for dumb terminals; scripts retain explicit CLI commands."""
    import manage as m
    while True:
        try:
            installed = m.xui.is_xui_installed()
            print('\nPrivate XUI')
            for line in status_lines(state_path, report, installed):
                print('  ' + line)
            print('\n  1 部署    2 订阅    3 维护    0 退出')
            choice = input('选择: ').strip()
            if choice in ('0', 'q'):
                return
            if choice == '1':
                args = install_questions(state_path, report, installed)
                if args:
                    operation(lambda: m.run_command(args), state_path)
            elif choice == '2':
                operation(lambda: show_subscription(state_path), state_path)
            elif choice == '3':
                text_maintenance(state_path)
            else:
                print('请输入 0–3。')
        except KeyboardInterrupt:
            print('\n已取消当前操作。')
        except EOFError:
            return


def text_maintenance(state_path):
    import manage as m
    print('\n维护：1 更新订阅  2 订阅设置  3 连接检查  4 面板  5 更换令牌  6 卸载  7 Dashboard  0 返回')
    choice = input('选择: ').strip()
    options = {'quiet_links': True}
    command = None
    if choice == '1':
        command, options = 'update-worker', dict(options, preferred=None, preferred_mode=None, subscription_name=None)
    elif choice == '2':
        def settings():
            state = m.load(state_path)
            name = ask('订阅名称', subscription_name, default=state.get('subscription_name') or 'Private XUI')
            print('入口：1 保留当前  2 自动优选  3 固定候选  4 仅节点域名  5 自有文件')
            mode = ask('选择', lambda value: {'1': None, '2': 'auto', '3': 'builtin', '4': 'direct', '5': 'custom'}[value], default='1')
            preferred = ask('JSON 文件路径', lambda value: (m.preferred_from_file(value), value)[1]) if mode == 'custom' else None
            m.run_command(argparse.Namespace(command='update-worker', state=state_path, preferred=preferred,
                                             preferred_mode=mode, subscription_name=name, quiet_links=True))
        operation(settings, state_path)
    elif choice == '3':
        command = 'check-connectivity'
    elif choice == '4':
        operation(panel_information, state_path)
    elif choice == '5' and confirm('旧订阅链接将失效，确认更换令牌'):
        command = 'rotate-token'
    elif choice == '7':
        import dashboard as dash
        import dashboard_service as service
        action = ask('Dashboard：1 打开 TUI / 2 启用采集 / 3 停止采集 / 4 状态', lambda v: {'1':'open','2':'install','3':'uninstall','4':'status'}[v], default='1' if dash.CONFIG.exists() else '2')
        operation(lambda: service.command(argparse.Namespace(state=state_path, action=action, retention_days=30, access_log=None, yes=False)), state_path)
    elif choice == '6' and confirm('删除本项目节点和订阅，保留面板及其他节点，确认卸载'):
        command = 'uninstall'
    if command:
        operation(lambda: m.run_command(argparse.Namespace(command=command, state=state_path, **options)), state_path)


def menu(state_path):
    import tui
    if not sys.stdin.isatty():
        raise ValueError('交互界面需要终端。请先下载再运行，不要使用 curl | python；脚本模式请指定子命令。')
    report = inspect_server()
    if not report['supported']:
        print_report(report)
    require_supported(report)
    if tui.available():
        try:
            tui.run(lambda ui: GuidedUI(state_path, report, ui).run())
            return
        except tui.curses.error:
            print('当前终端不支持全屏界面，使用简洁文字模式。')
    else:
        print('当前终端使用简洁文字模式；完整 SSH 终端支持方向键界面。')
    text_menu(state_path, report)
