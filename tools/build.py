#!/usr/bin/env python3
"""Build a deterministic single-file distribution, with no third-party runtime deps."""
import argparse
import hashlib
import io
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ('__main__.py', 'manage.py', 'wizard.py', 'environment.py', 'cloudflare_api.py',
         'storage.py', 'progress.py', 'preferred.py', 'connectivity.py', 'tui.py', 'xui_backend.py', 'worker.mjs')


def build():
    content = io.BytesIO()
    content.write(b'#!/usr/bin/env python3\n')
    with zipfile.ZipFile(content, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in FILES:
            item = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, (ROOT / name).read_bytes())
    return content.getvalue()


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--check', action='store_true')
    options = args.parse_args()
    program = build()
    digest = hashlib.sha256(program).hexdigest() + '  private-xui.pyz\n'
    dest = ROOT / 'dist'
    if options.check:
        if not (dest / 'private-xui.pyz').exists() or (dest / 'private-xui.pyz').read_bytes() != program:
            raise SystemExit('dist 已过期，请执行 python3 tools/build.py')
        if (dest / 'private-xui.pyz.sha256').read_text() != digest:
            raise SystemExit('SHA-256 校验文件不一致')
        print('单文件程序与源码一致，SHA-256 校验通过。')
    else:
        dest.mkdir(exist_ok=True)
        (dest / 'private-xui.pyz').write_bytes(program)
        (dest / 'private-xui.pyz.sha256').write_text(digest)
        print('已生成 dist/private-xui.pyz 和校验文件。')


if __name__ == '__main__':
    main()
