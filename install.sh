#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$(uname -s)" != Linux ]; then
  echo '请先在电脑终端执行 ssh root@服务器IP，登录 Linux VPS 后再运行本命令。' >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo '需要 root 权限，请执行 sudo bash private-xui.sh。' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo '缺少 Python 3。Debian/Ubuntu 请先执行：apt-get update && apt-get install -y python3' >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'; then
  echo '需要 Python 3.9+。请升级 Python，或使用 Debian 11+ / Ubuntu 22.04+。' >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo '缺少 curl。Debian/Ubuntu 请先执行：apt-get install -y curl ca-certificates' >&2
  exit 1
fi

px_tmp=$(mktemp -d)
trap 'rm -rf -- "$px_tmp"' EXIT
px_repo='https://api.github.com/repos/isyundong/private-xui/commits/main'
echo '正在从 GitHub 下载 Private XUI…'
curl -fLsS --retry 2 --connect-timeout 10 --max-time 60 "$px_repo" -o "$px_tmp/revision.json"
px_revision=$(python3 -c 'import json,re,sys; value=json.load(open(sys.argv[1])).get("sha", ""); sys.exit("GitHub 没有返回有效版本") if not re.fullmatch("[0-9a-f]{40}", value) else print(value)' "$px_tmp/revision.json")
px_raw="https://raw.githubusercontent.com/isyundong/private-xui/$px_revision/dist"
curl -fLsS --retry 2 --connect-timeout 10 --max-time 120 "$px_raw/private-xui.pyz" -o "$px_tmp/private-xui.pyz"
curl -fLsS --retry 2 --connect-timeout 10 --max-time 60 "$px_raw/private-xui.pyz.sha256" -o "$px_tmp/checksum"
python3 - "$px_tmp/private-xui.pyz" "$px_tmp/checksum" <<'PY'
import hashlib, pathlib, re, sys
expected = pathlib.Path(sys.argv[2]).read_text().split()[0]
actual = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
if not re.fullmatch(r'[0-9a-f]{64}', expected) or actual != expected:
    sys.exit('文件校验失败，未安装。请重试或检查 GitHub 下载连接。')
PY

# Check compatibility before installing the launcher or changing cloud/node resources.
python3 "$px_tmp/private-xui.pyz" check --local-only --quiet
px_dir='/usr/local/lib/private-xui'
px_command='/usr/local/bin/private-xui'
if [ -e "$px_command" ] && ! grep -q '^# private-xui managed launcher$' "$px_command"; then
  echo "$px_command 已存在且不是本工具的快捷命令，请先人工检查；不会覆盖。" >&2
  exit 1
fi
mkdir -p "$px_dir" /usr/local/bin
install -m 0644 "$px_tmp/private-xui.pyz" "$px_dir/private-xui.pyz.new"
mv -f "$px_dir/private-xui.pyz.new" "$px_dir/private-xui.pyz"
cat > "$px_tmp/launcher" <<'SH'
#!/bin/sh
# private-xui managed launcher
exec python3 /usr/local/lib/private-xui/private-xui.pyz "$@"
SH
install -m 0755 "$px_tmp/launcher" "$px_command"
echo '下载校验完成。以后输入 private-xui 即可打开菜单（非 root 用户请加 sudo）。'
python3 "$px_dir/private-xui.pyz" "$@"
