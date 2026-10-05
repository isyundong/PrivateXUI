"""Preferred-source modes and offline fallback candidates.

The addresses below lie within Cloudflare's published 104.16.0.0/13 range:
https://www.cloudflare.com/ips-v4/ (checked 2026-10-06).
Range ownership does not guarantee an individual IP works for every domain or
network. The generated Clash url-test group checks the full route on the client;
the ordinary node domain always remains available as an entry point.
"""

DEFAULT_MODE = 'auto'
MODE_LABELS = {
    'auto': '自动优选（公开域名池 + 动态 IP）',
    'builtin': '内置 Cloudflare 候选（客户端自动测延迟）',
    'direct': '仅域名入口',
    'custom': '导入自有地址文件',
}
BUILTIN_NETWORK = '104.16.0.0/13'
BUILTIN_ADDRESSES = (
    '104.16.0.1', '104.17.0.1', '104.18.0.1',
    '104.19.0.1', '104.20.0.1', '104.21.0.1',
)


def builtin_candidates():
    """Return fresh candidate records suitable for state['preferred']."""
    return [{'address': address, 'name': f'CF 候选 {index:02d}'}
            for index, address in enumerate(BUILTIN_ADDRESSES, 1)]
