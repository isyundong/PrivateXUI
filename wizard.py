"""Chinese interactive flow; imports deployment functions lazily."""
import argparse
import json
from pathlib import Path
import sys

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


def install_questions(state_path, report, installed):
    import manage as m
    if Path(state_path).exists():
        print('已有部署或待清理状态。请先选择“查看订阅”或“卸载”，不会覆盖。')
        return None
    print('\n安装向导（输入过程中不会修改配置；按 Ctrl+C 返回菜单）')
    address = ask('1. 服务器公网入站 IPv4（请确认不是 VPN/代理出口）', validate_ipv4, default=report['ipv4'] or '')
    node = ask('2. 节点域名，如 node.example.com', m.hostname)
    def distinct(value):
        value = m.hostname(value)
        if value == node:
            raise ValueError('订阅域名必须与节点域名不同，例如 sub.example.com。')
        return value
    subscription = ask('3. 订阅域名，如 sub.example.com', distinct)
    def select_protocols(value):
        try:
            return ','.join(m.xui.parse_protocol_selection(value))
        except SystemExit:
            raise ValueError('请输入 1、2、3，用逗号分隔；例如 1,2,3。') from None
    protocols = ask('4. 协议：1=VLESS，2=Trojan，3=VMess，可用逗号多选', select_protocols, default='1,2,3')
    preferred = ask('5. 优选地址 JSON 文件路径（可选，回车跳过）',
                    lambda value: (m.preferred_from_file(value), value)[1], allow_empty=True)
    print('\n请核对：')
    print('  服务器: %s；节点: %s；订阅: %s' % (address, node, subscription))
    print('  协议: %s；输出: Clash/Mihomo YAML' % protocols)
    print('  面板: ' + ('复用已安装的 3x-ui' if installed else '自动安装 3x-ui，管理面板仅监听本机'))
    print('  节点回源使用 HTTP WebSocket；SSL 设置仅作用于这个节点域名。')
    if not confirm('开始部署', default=True):
        print('已取消。')
        return None
    print('\n接下来输入 Cloudflare API Token（隐藏输入，不保存到本地）。')
    print('权限：Workers Scripts 编辑；Zone 读取、DNS / Workers Routes / Origin Rules / Config Settings 编辑。')
    return argparse.Namespace(command='install', state=state_path, node_domain=node, sub_domain=subscription,
                              ipv4=address, protocols=protocols, preferred=preferred, fresh=not installed)


def menu(state_path):
    import manage as m
    if not sys.stdin.isatty():
        raise ValueError('交互菜单需要终端。请先下载再运行，不要使用 curl | python；脚本模式请指定子命令。')
    report = inspect_server()
    print_report(report)
    require_supported(report)
    while True:
        try:
            installed = m.xui.is_xui_installed()
            print('\nPrivate XUI · 自有域名 Clash 订阅')
            print('  1. ' + ('安装节点与订阅（已有 3x-ui）' if installed else '全新安装（3x-ui + 节点 + 订阅）'))
            print('  2. 卸载本项目配置')
            print('  3. 查看 Clash 订阅')
            print('  4. 查看面板访问信息')
            print('  5. 更新订阅服务 / 优选列表')
            print('  6. 更换订阅令牌')
            print('  7. 重新检查环境和 IP')
            print('  8. 查看 x-ui 管理命令')
            print('  0. 退出')
            choice = input('请选择 [0-8]: ').strip()
            if choice == '0':
                return
            if choice == '1':
                args = install_questions(state_path, report, installed)
                if args:
                    m.run_command(args)
            elif choice == '2':
                state = m.load(state_path)
                print('将清理本项目节点与订阅：%s / %s；保留 3x-ui 面板及其他节点。' % (state['domain'], state['subscription_domain']))
                if confirm('确认卸载'):
                    m.run_command(argparse.Namespace(command='uninstall', state=state_path))
            elif choice == '3':
                m.print_links(m.load(state_path))
            elif choice == '4':
                panel_information()
            elif choice == '5':
                m.load(state_path)
                preferred = ask('优选 JSON 文件（回车保留；空数组文件可清空）',
                                lambda value: (m.preferred_from_file(value), value)[1], allow_empty=True)
                m.run_command(argparse.Namespace(command='update-worker', state=state_path, preferred=preferred))
            elif choice == '6':
                m.load(state_path)
                if confirm('旧订阅 URL 将失效，需要重新导入客户端；确认更换令牌'):
                    m.run_command(argparse.Namespace(command='rotate-token', state=state_path))
            elif choice == '7':
                report = inspect_server()
                print_report(report)
                require_supported(report)
            elif choice == '8':
                print('服务器执行 x-ui 可进入面板管理菜单；执行 private-xui 可返回本工具。')
            else:
                print('请输入 0 到 8。')
        except FileNotFoundError:
            print('未找到部署状态或文件，请先安装，或检查输入的文件路径。')
        except KeyboardInterrupt:
            print('\n已取消当前操作，返回菜单。')
        except EOFError:
            return
        except (ValueError, OSError, RuntimeError, KeyError, SystemExit) as exc:
            print('操作未完成：%s' % exc)
            print('如有失败部署，请选择“卸载”重试清理；恢复状态会保留。')
