"""Install/uninstall the optional Dashboard without changing proxy routes or ports."""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import io
import time

import dashboard as dash
from storage import atomic_private_write

UNIT = Path('/etc/systemd/system/private-xui-dashboard.service')
TIMER = Path('/etc/systemd/system/private-xui-dashboard-logrotate.timer')
ROTATE_UNIT = Path('/etc/systemd/system/private-xui-dashboard-logrotate.service')
ROTATE_CONFIG = Path('/etc/logrotate.d/private-xui-dashboard')
MARKER = '# Private XUI dashboard managed file\n'
PROGRAM = '/usr/local/lib/private-xui/private-xui.pyz'


def systemctl(*args):
    result = subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ValueError('服务操作失败：systemctl ' + ' '.join(args) + '；请检查 journalctl -u private-xui-dashboard')
    return result.stdout


def quote(value):
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('路径不能包含控制字符')
    return json.dumps(str(value).replace('%', '%%'))


def managed_write(path, text):
    if path.exists() and not path.read_text().startswith(MARKER):
        raise ValueError(str(path) + ' 已存在且不属于本项目')
    atomic_private_write(path, MARKER + text)
    path.chmod(0o644)


def template_from_panel():
    import xui_backend as xui
    # Legacy adapter exits on API errors. Suppress its output and turn failures
    # into a generic diagnostic, never printing panel responses or credentials.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            runtime = xui.detect_xui_environment()
            client = xui.XuiPanelClient(runtime['panel_url'], token=runtime.get('api_token'), insecure_tls=runtime.get('insecure_tls', False))
            if not runtime.get('api_token'):
                user, password = xui.read_panel_user_from_db()
                client.login(user, password)
            for route in ('panel/api/xray/', 'panel/xray/'):
                try:
                    obj = client._request('POST', route).get('obj')
                    obj = json.loads(obj) if isinstance(obj, str) else obj
                    template = obj.get('xraySetting')
                    template = json.loads(template) if isinstance(template, str) else template
                    if isinstance(template, dict) and ('outbounds' in template or 'routing' in template):
                        return template
                except (Exception, SystemExit):
                    continue
        except (Exception, SystemExit):
            pass
    raise ValueError('无法读取 3x-ui 的默认 Xray 模板。请在面板保存一次 Xray 设置后重试，或用 --access-log 指定已启用的日志。')


def read_template(db_path, fallback=None):
    with contextlib.closing(sqlite3.connect(str(db_path), timeout=5)) as db:
        row = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
    value = json.loads(row[0]) if row else (fallback or template_from_panel)()
    if not isinstance(value, dict) or 'xraySetting' in value:
        raise ValueError('Xray 模板格式不支持，请先在 3x-ui 保存一次 Xray 设置')
    return value, row[0] if row else None


def set_access_log(db_path, desired, config, config_path=None, fallback=None):
    config_path = config_path or dash.CONFIG
    template, original = read_template(db_path, fallback)
    log = template.setdefault('log', {})
    if not isinstance(log, dict) or any(k.lower() == 'access' and k != 'access' for k in log):
        raise ValueError('日志字段格式不支持，请先在 3x-ui 保存标准 log.access 配置')
    current = log.get('access')
    if current not in (None, '', 'none'):
        config['access_log'] = str(Path(current).resolve())
        config['owned_log'] = bool(config.get('owned_log') and config.get('managed_log') == current)
        atomic_private_write(config_path, json.dumps(config))
        return False
    config.update(access_log=desired, managed_log=desired, owned_log=True,
                  previous_access={'present': 'access' in log, 'value': current}, log_change_pending=True)
    # Journal before mutation so interrupted installation can restore the exact field.
    atomic_private_write(config_path, json.dumps(config))
    log['access'] = desired
    with contextlib.closing(sqlite3.connect(str(db_path), timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
        if (row[0] if row else None) != original:
            raise ValueError('Xray 设置刚被修改，停止覆盖，请重试')
        if row:
            db.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig'", (json.dumps(template),))
        else:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('xrayTemplateConfig', json.dumps(template)))
    # Keep pending until x-ui has actually restarted successfully.
    atomic_private_write(config_path, json.dumps(config))
    return True


def restore_access_log(config):
    if not config.get('owned_log') or 'previous_access' not in config or not Path(config['xui_db']).is_file():
        return False
    with contextlib.closing(sqlite3.connect(config['xui_db'], timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
        if not row:
            return False
        template = json.loads(row[0])
        log = template.get('log', {})
        if log.get('access') != config.get('managed_log'):
            return False  # User changed this field later: leave it alone.
        previous = config['previous_access']
        if previous['present']:
            log['access'] = previous['value']
        else:
            log.pop('access', None)
        db.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig'", (json.dumps(template),))
    return True


def log_folder():
    result = subprocess.run(['systemctl', 'show', 'x-ui', '--property=Environment', '--value'], capture_output=True, text=True, timeout=10)
    import shlex
    for value in shlex.split(result.stdout):
        if value.startswith('XUI_LOG_FOLDER='):
            folder = value.split('=', 1)[1]
            if not re.fullmatch(r'/[A-Za-z0-9_./-]+', folder):
                raise ValueError('XUI_LOG_FOLDER 路径不支持自动日志轮转，请用 --access-log 读取现有日志')
            return Path(folder)
    return Path('/var/log/x-ui')


def install(args):
    import manage
    import xui_backend as xui
    state = manage.load(args.state)
    if state.get('status') not in ('ready', 'update-pending'):
        raise ValueError('请先完成 Private XUI 节点部署')
    dash.owned_counters(xui.DB_PATH, state)
    if not Path(PROGRAM).is_file() or not shutil.which('systemctl'):
        raise ValueError('请先使用官方 install.sh 安装工具；Dashboard 需要 systemd 的 Linux VPS')
    if not 1 <= args.retention_days <= 30:
        raise ValueError('保留时间须在 1–30 天')
    existing = json.loads(dash.CONFIG.read_text()) if dash.CONFIG.exists() else None
    config = existing or {'version': 1, 'state': str(Path(args.state).resolve()),
                         'xui_db': xui.DB_PATH, 'retention_days': args.retention_days}
    if config.get('state') != str(Path(args.state).resolve()):
        raise ValueError('Dashboard 已绑定另一部署，不会覆盖')
    for managed in (UNIT, TIMER, ROTATE_UNIT, ROTATE_CONFIG):
        if managed.exists() and not managed.read_text().startswith(MARKER):
            raise ValueError(str(managed) + ' 不属于本项目，停止安装')
    if args.access_log and config.get('owned_log') and str(Path(args.access_log).resolve()) != config.get('access_log'):
        raise ValueError('请先卸载 Dashboard 恢复原日志设置，再切换日志文件')
    if not args.yes:
        print('将启用终端 Dashboard 的后台采集，不启动网页、不监听网络端口。')
        print('记录本项目流量与目标域名/IP，最多保留 %s 天 / 最近 10 万条连接。' % config['retention_days'])
        print('若尚未开启访问日志，将启用 Xray 全局访问日志并重启 x-ui（短暂断线）。')
        print('Dashboard 只保存本项目记录；短期原始日志可能含其他入站记录，会配置轮转。')
        if input('确认启用？输入 yes: ').strip().lower() != 'yes':
            print('已取消。')
            return
    dash.ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    dash.ROOT.chmod(0o700)
    if args.access_log:
        config.update(access_log=str(Path(args.access_log).resolve()), owned_log=False)
        with open(config['access_log'], 'rb'):
            pass
        atomic_private_write(dash.CONFIG, json.dumps(config))
        restart = False
    else:
        # Reusing an existing log never changes its retention policy.
        template, _ = read_template(xui.DB_PATH)
        needs_new_log = template.get('log', {}).get('access') in (None, '', 'none')
        if (needs_new_log or config.get('owned_log')) and not shutil.which('logrotate'):
            raise ValueError('需安装 logrotate 以限制原始日志体积：apt-get install -y logrotate')
        folder = log_folder()
        if needs_new_log:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / 'private-xui-access.log'
            if target.exists() and not config.get('owned_log'):
                raise ValueError('预定日志文件已存在，不会接管；请用 --access-log 指定它或先核对文件')
            target.touch(mode=0o600, exist_ok=True)
        restart = set_access_log(xui.DB_PATH, str(folder / 'private-xui-access.log'), config)
        if not Path(config['access_log']).is_file():
            confined = folder / Path(config['access_log']).name
            if confined.is_file():
                config['access_log'] = str(confined)
                atomic_private_write(dash.CONFIG, json.dumps(config))
    if restart or config.get('log_change_pending'):
        xui.restart_xui_service()
        config['log_change_pending'] = False
        atomic_private_write(dash.CONFIG, json.dumps(config))
    runtime_config = Path('/usr/local/x-ui/bin/config.json')
    if config.get('owned_log') and runtime_config.is_file():
        try:
            actual = json.loads(runtime_config.read_text()).get('log', {}).get('access')
            if actual and Path(actual).name == 'private-xui-access.log' and Path(actual).is_absolute():
                config['access_log'] = actual
                if Path(actual).is_file():
                    Path(actual).chmod(0o600)
                atomic_private_write(dash.CONFIG, json.dumps(config))
        except (OSError, ValueError, TypeError):
            pass
    dash.init_store(dash.ROOT / 'history.sqlite3')
    managed_write(UNIT, '[Unit]\nDescription=Private XUI telemetry collector\nAfter=network.target x-ui.service\n\n[Service]\nType=simple\nUser=root\nUMask=0077\n'
                  + 'ExecStart=' + quote(sys.executable) + ' ' + quote(PROGRAM) + ' dashboard collect\n'
                  + 'Restart=on-failure\nRestartSec=5\nNoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\nReadWritePaths=/var/lib/private-xui-dashboard\nMemoryMax=256M\n\n[Install]\nWantedBy=multi-user.target\n')
    if config.get('owned_log'):
        path = config['access_log']
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path):
            raise ValueError('日志路径不支持自动轮转')
        managed_write(ROTATE_CONFIG, path + ' {\n  size 10M\n  rotate 2\n  compress\n  missingok\n  notifempty\n  copytruncate\n  su root root\n}\n')
        managed_write(ROTATE_UNIT, '[Unit]\nDescription=Rotate Private XUI access log\n\n[Service]\nType=oneshot\nExecStart=' + shutil.which('logrotate') + ' --state /var/lib/private-xui-dashboard/rotation.state /etc/logrotate.d/private-xui-dashboard\n')
        managed_write(TIMER, '[Unit]\nDescription=Check Private XUI log size\n\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec=5min\n\n[Install]\nWantedBy=timers.target\n')
    systemctl('daemon-reload')
    systemctl('enable', '--now', UNIT.name)
    started_at = int(time.time())
    systemctl('restart', UNIT.name)
    if config.get('owned_log'):
        systemctl('enable', '--now', TIMER.name)
    deadline = time.monotonic() + 10
    ready = False
    while time.monotonic() < deadline:
        try:
            with contextlib.closing(dash.connect(dash.ROOT / 'history.sqlite3')) as db:
                row = db.execute("SELECT value FROM meta WHERE key='collector_status'").fetchone()
            ready = bool(row and json.loads(row[0]).get('last_check', 0) >= started_at)
            if ready:
                break
        except (OSError, ValueError, sqlite3.Error):
            pass
        time.sleep(0.25)
    if not ready:
        raise ValueError('采集服务尚未完成首次检查；配置已保留，请查看 journalctl -u private-xui-dashboard 后重试')

    show()


def show():
    if not dash.CONFIG.exists():
        print('Dashboard 尚未启用。运行 private-xui dashboard install。')
        return
    config = json.loads(dash.CONFIG.read_text())
    running = subprocess.run(['systemctl', 'is-active', '--quiet', UNIT.name], capture_output=True).returncode == 0
    print('\nDashboard 采集服务：' + ('运行中' if running else '未运行'))
    print('保留时间：%s 天；连接记录最多 %s 条。' % (config['retention_days'], dash.MAX_EVENTS))
    print('访问方式：SSH 登录服务器，运行 private-xui dashboard。')
    print('数据仅保存在服务器，不提供网页或网络监听端口。')


def uninstall(args):
    import xui_backend as xui
    if not dash.CONFIG.exists():
        print('Dashboard 未安装。')
        return
    config = json.loads(dash.CONFIG.read_text())
    if not args.yes and input('停止并移除 Dashboard 采集服务，保留历史数据？输入 yes: ').strip().lower() != 'yes':
        return
    for path in (UNIT, TIMER):
        if path.exists() and path.read_text().startswith(MARKER):
            systemctl('disable', '--now', path.name)
    if restore_access_log(config):
        xui.restart_xui_service()
    for path in (UNIT, TIMER, ROTATE_UNIT, ROTATE_CONFIG):
        if path.exists() and path.read_text().startswith(MARKER):
            path.unlink()
    systemctl('daemon-reload')
    store = dash.ROOT / 'history.sqlite3'
    if store.is_file():
        with contextlib.closing(dash.connect(store)) as db, db:
            status = {'traffic': '采集已停止', 'history': '只读历史', 'last_check': int(time.time())}
            db.execute("INSERT OR REPLACE INTO meta VALUES('collector_status',?)", (json.dumps(status, ensure_ascii=False),))
    print('Dashboard 已停止，自己的日志设置已按需恢复；历史和配置仍保存在 ' + str(dash.ROOT))
    print('本操作不删除 3x-ui、节点、订阅和原始日志文件。')


def command(args):
    if os.geteuid() != 0:
        raise ValueError('请使用 sudo 运行 Dashboard 命令')
    if args.action == 'collect':
        return dash.collect()
    if args.action == 'status':
        return show()
    if args.action == 'open':
        import tui
        if not tui.available():
            raise ValueError('Dashboard 需要支持 curses 的交互终端。请 SSH 登录服务器后运行。')
        return tui.run(lambda ui: ui.dashboard(dash.read_snapshot))
    import manage
    with manage.lock(args.state):
        if args.action == 'install':
            return install(args)
        if args.action == 'uninstall':
            return uninstall(args)
