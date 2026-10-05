"""Read-only server checks, kept independent of deployment and credentials."""
import importlib
import ipaddress
import os
from pathlib import Path
import platform
import shutil
import sys
from urllib import request


def os_release(path='/etc/os-release'):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                values[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def validate_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        raise ValueError('请输入公网 IPv4；当前部署方式不支持纯 IPv6 服务器') from None
    if not address.is_global:
        raise ValueError('需要公网 IPv4，不能使用内网、回环或文档示例地址')
    return str(address)


def public_ipv4():
    for url in ('https://api.ipify.org', 'https://ipv4.icanhazip.com'):
        try:
            with request.urlopen(url, timeout=5) as response:
                return validate_ipv4(response.read(128).decode().strip())
        except (OSError, ValueError, UnicodeError):
            continue
    return None


def inspect_server(*, lookup_ip=True):
    system, architecture = platform.system(), platform.machine().lower()
    release = os_release() if system == 'Linux' else {}
    issues = []
    if system != 'Linux':
        issues.append('自动部署需要 Linux VPS。请在电脑终端先用 ssh root@服务器IP 登录服务器，再下载运行。')
    else:
        distro = release.get('ID', '').lower()
        try:
            major = int(release.get('VERSION_ID', '').split('.')[0])
        except ValueError:
            major = 0
        if not ((distro == 'debian' and major >= 11) or (distro == 'ubuntu' and major >= 22)):
            issues.append('当前自动部署支持 Debian 11+、Ubuntu 22.04+；此系统暂不支持，请更换受支持的服务器镜像。')
        if not Path('/run/systemd/system').is_dir() or not shutil.which('systemctl'):
            issues.append('未检测到运行中的 systemd。请在完整 VPS 系统中运行，不支持普通 Docker 容器或未启用 systemd 的环境。')
    if architecture not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        issues.append('当前支持 x86_64 / ARM64，服务器架构 %s 暂不支持。' % architecture)
    if sys.version_info < (3, 9):
        issues.append('需要 Python 3.9+；请升级 Python 或使用 Debian 11+ / Ubuntu 22.04+。')
    for module in ('sqlite3', 'ssl', 'fcntl'):
        try:
            importlib.import_module(module)
        except ImportError:
            issues.append('Python 缺少 %s 模块，请安装发行版提供的完整 python3 软件包。' % module)
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        issues.append('部署需要 root 权限，请使用 sudo private-xui，或切换为 root 后运行。')
    address = public_ipv4() if lookup_ip and system == 'Linux' and not issues else None
    return {
        'system': release.get('PRETTY_NAME', system), 'architecture': architecture,
        'python': platform.python_version(), 'ipv4': address, 'issues': issues,
        'supported': not issues, 'ip_checked': lookup_ip and not issues,
    }


def print_report(report):
    print('\n服务器检查')
    print('  系统: %s' % report['system'])
    print('  架构: %s' % report['architecture'])
    print('  Python: %s' % report['python'])
    if report['ipv4']:
        print('  检测到的公网出口 IPv4: %s' % report['ipv4'])
    elif report['ip_checked']:
        print('  公网 IPv4: 自动查询失败，安装时可手动输入。')
    for issue in report['issues']:
        print('  [不支持] ' + issue)
    if report['supported']:
        print('  [通过] 系统环境支持部署。安装时还会确认公网入站 IP。')


def require_supported(report):
    if not report['supported']:
        raise ValueError('环境检查未通过，未进行部署。请按上面的说明处理后重试。')
