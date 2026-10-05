"""Zipapp entry point: give useful errors before importing platform modules."""
import platform
import sys
import importlib

if sys.version_info < (3, 9):
    sys.exit('需要 Python 3.9+，请升级 Python 后重新运行。')
if platform.system() != 'Linux':
    sys.exit('请在 Linux VPS 上运行。电脑终端先执行 ssh root@服务器IP，再按 README 下载运行。')
for module in ('sqlite3', 'ssl', 'fcntl'):
    try:
        importlib.import_module(module)
    except ImportError:
        sys.exit('Python 缺少 %s 模块。请安装发行版提供的完整 python3 软件包后重试。' % module)

from manage import main

main()
