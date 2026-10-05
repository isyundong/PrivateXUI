"""Check the configured Cloudflare -> origin WS transport, without node credentials."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import re
import secrets
import socket
import ssl
import time

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
CACHE_SECONDS = 300


def signature(state):
    config = {'domain': state['domain'], 'routes': state['routes']}
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def probe_route(domain, route, timeout=8):
    result = {'protocol': route['protocol'], 'port': route['port'], 'status': 'unverified'}
    connection = None
    try:
        path = route['path']
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', domain) or not path.startswith('/') or path.startswith('//') or any(ord(c) < 32 for c in path):
            raise ValueError('invalid configured endpoint')
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        connection = http.client.HTTPSConnection(domain, 443, timeout=timeout, context=ssl.create_default_context())
        connection.request('GET', path, headers={
            'Upgrade': 'websocket', 'Connection': 'Upgrade',
            'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': key,
            'User-Agent': 'Private-XUI-Connectivity-Check',
        })
        response = connection.getresponse()
        try:
            if response.status != 101:
                result['reason'] = f'HTTP {response.status}，未建立 WebSocket 连接'
            elif (response.getheader('Upgrade', '').lower() != 'websocket'
                  or response.getheader('Sec-WebSocket-Accept', '') != expected):
                result['reason'] = 'WebSocket 握手响应不匹配'
            elif not response.getheader('CF-Ray', ''):
                result['reason'] = '握手成功，但未确认经过 Cloudflare'
            else:
                result.update(status='passed', reason='Cloudflare WebSocket 回源握手通过')
        finally:
            response.close()
    except ssl.SSLError:
        result['reason'] = 'TLS/证书检查失败，证书可能尚未生效'
    except socket.gaierror:
        result['reason'] = 'DNS 解析失败，记录可能尚未生效'
    except TimeoutError:
        result['reason'] = '连接超时，尚不能确认回源连通'
    except (OSError, http.client.HTTPException):
        result['reason'] = '连接未建立，需检查网络与服务'
    except (ValueError, TypeError):
        result['reason'] = '节点域名或路径配置无效'
    finally:
        if connection:
            connection.close()
    return result


def probe_all(state):
    routes = state['routes']
    if not routes:
        raise ValueError('没有可检查的节点')
    with ThreadPoolExecutor(max_workers=min(3, len(routes))) as executor:
        results = list(executor.map(lambda route: probe_route(state['domain'], route), routes))
    return {'checked_at': time.time(), 'signature': signature(state), 'results': results}


def cached_report(state, now=None):
    report = state.get('connectivity')
    if not isinstance(report, dict):
        return None
    try:
        age = (time.time() if now is None else now) - report['checked_at']
        if not 0 <= age <= CACHE_SECONDS or report['signature'] != signature(state):
            return None
        expected = {(r['protocol'], r['port']) for r in state['routes']}
        rows = report['results']
        if len(rows) != len(expected) or {(r['protocol'], r['port']) for r in rows} != expected:
            return None
        if any(r['status'] not in ('passed', 'unverified') for r in rows):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return report


def print_report(state, report=None):
    report = report or cached_report(state)
    if report is None:
        print('回源连通性：尚未检查或检查已过期，可在维护中执行“连接检查”。')
        print('当前记录的节点 TCP 端口: ' + '、'.join(f'{r["protocol"].upper()}={r["port"]}' for r in state['routes']))
        return
    results = report['results']
    passed = sum(r['status'] == 'passed' for r in results)
    print(f'Cloudflare 回源检查：{passed}/{len(results)} 通过（WebSocket 握手）。')
    if passed == len(results):
        return  # Do not repeat firewall-opening prompts after verified success.
    for result in results:
        if result['status'] != 'passed':
            print(f'  {result["protocol"].upper()} / TCP {result["port"]}: {result["reason"]}')
    print('尚未通过不等于端口未放行。请先核对 DNS/证书、回源规则和 Xray 监听，再检查系统防火墙与云安全组。')
