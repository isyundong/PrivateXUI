"""Small Cloudflare API client. No redirects or credential-bearing error output."""
import json
import ipaddress
import re
import secrets
from urllib import error, parse, request

BASE = 'https://api.cloudflare.com/client/v4'


def safe_error_details(errors):
    """Keep recognized auth diagnostics only; API error bodies can echo secrets."""
    codes, messages = [], []
    allowed = {'invalid access token': 'Invalid access token（Token 无效或不可用）',
               'authentication error': 'Authentication error（认证失败）',
               'unauthorized': 'Unauthorized（未获授权）',
               'forbidden': 'Forbidden（访问被拒绝）'}
    if not isinstance(errors, list):
        return codes, messages
    for item in errors[:20]:
        if not isinstance(item, dict):
            continue
        code = item.get('code')
        if isinstance(code, int) and not isinstance(code, bool):
            codes.append(code)
        message = str(item.get('message') or '').strip().rstrip('.')
        known = allowed.get(message.lower())
        if known:
            messages.append(known)
        prefix = 'cannot use the access token from location:'
        if message.lower().startswith(prefix):
            try:
                address = str(ipaddress.ip_address(message[len(prefix):].strip()))
                messages.append(f'Token 的客户端 IP 限制拒绝了请求来源：{address}')
            except ValueError:
                pass
    return codes, list(dict.fromkeys(messages))


class APIError(RuntimeError):
    def __init__(self, method, path, status, codes=(), messages=()):
        self.method = method
        self.path = path.split('?')[0]
        self.status = status
        self.codes = codes
        message = f'Cloudflare {method} {self.path} 失败: HTTP {status}, codes={codes}'
        if messages:
            message += '\nCloudflare 说明：' + '；'.join(messages)
        if status in (401, 403) or 9109 in codes or 10000 in codes:
            message += ('\n请确认输入的是 API Token 本身，不是 Global API Key、Zone ID 或带 Bearer 的整段命令；'
                        '检查 Token 是否被撤销/过期、是否尚未生效，以及客户端 IP 限制是否允许 VPS 实际出口 IP。')
            if self.path == '/zones':
                message += '\n读取域名列表需要 Zone → Zone → Read，Zone Resources 必须包含目标域名所在 Zone。'
        super().__init__(message)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Cloudflare:
    def __init__(self, token):
        token = token.strip()
        if not token:
            raise ValueError('Cloudflare API Token 不能为空')
        if not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token):
            raise ValueError('请只粘贴 API Token 本身，不要包含引号、空格、换行、Bearer 前缀或 curl 命令。')
        self.headers = {'Authorization': f'Bearer {token}'}
        self.opener = request.build_opener(NoRedirect())

    def envelope(self, method, path, data=None, *, raw=None, content_type=None, missing_ok=False):
        if not path.startswith('/') or '://' in path:
            raise ValueError('Invalid API path')
        headers = dict(self.headers)
        if data is not None:
            raw = json.dumps(data).encode()
            content_type = 'application/json'
        if content_type:
            headers['Content-Type'] = content_type
        req = request.Request(BASE + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=45) as response:
                content = response.read()
        except error.HTTPError as exc:
            # An explicit 404 is different from a failed read or invalid JSON.
            if missing_ok and exc.code == 404:
                exc.close()
                return None
            try:
                codes, messages = safe_error_details(json.loads(exc.read()).get('errors', []))
            except (ValueError, AttributeError, TypeError):
                codes, messages = [], []
            finally:
                exc.close()
            raise APIError(method, path, exc.code, codes, messages) from None
        except (error.URLError, TimeoutError, OSError):
            raise APIError(method, path, 'network') from None
        try:
            body = json.loads(content)
        except ValueError:
            raise APIError(method, path, 'invalid-json') from None
        if not isinstance(body, dict) or body.get('success') is not True:
            codes, messages = safe_error_details(body.get('errors', [])) if isinstance(body, dict) else ([], [])
            raise APIError(method, path, 'api-error', codes, messages)
        return body

    def call(self, method, path, data=None, **kwargs):
        body = self.envelope(method, path, data, **kwargs)
        return None if body is None else body.get('result')

    def listing(self, path):
        result = []
        for page in range(1, 10001):
            separator = '&' if '?' in path else '?'
            body = self.envelope('GET', f'{path}{separator}page={page}&per_page=50')
            rows = body.get('result')
            if not isinstance(rows, list):
                raise APIError('GET', path, 'invalid-list')
            result.extend(rows)
            info = body.get('result_info') or {}
            if page >= int(info.get('total_pages') or 1):
                return result
        raise RuntimeError('API pagination exceeded limit')

    def dns(self, zone_id, hostname):
        return self.listing(f'/zones/{zone_id}/dns_records?' + parse.urlencode({'name': hostname}))

    def ruleset(self, zone_id, phase):
        path = f'/zones/{zone_id}/rulesets/phases/{phase}/entrypoint'
        body = self.envelope('GET', path, missing_ok=True)
        if body is None:
            return None
        value = body.get('result')
        if not isinstance(value, dict) or not value.get('id') or not isinstance(value.get('rules'), list):
            raise APIError('GET', path, 'invalid-ruleset')
        return value

    def add_rule(self, zone_id, phase, rule):
        current = self.ruleset(zone_id, phase)
        if current is None:
            return self.call('POST', f'/zones/{zone_id}/rulesets', {
                'name': f'Private XUI {phase}', 'kind': 'zone', 'phase': phase, 'rules': [rule],
            })
        if any(item.get('ref') == rule['ref'] for item in current.get('rules', [])):
            return current
        return self.call('POST', f'/zones/{zone_id}/rulesets/{current["id"]}/rules', rule)

    def remove_rules(self, zone_id, phase, refs):
        current = self.ruleset(zone_id, phase)
        if current is None:
            return
        for rule in current.get('rules', []):
            if rule.get('ref') in refs:
                self.call('DELETE', f'/zones/{zone_id}/rulesets/{current["id"]}/rules/{rule["id"]}', missing_ok=True)

    def upload_worker(self, account_id, name, source, config):
        metadata = {
            'main_module': 'worker.mjs', 'compatibility_date': '2026-10-01',
            'logpush': False, 'tail_consumers': [], 'observability': {'enabled': False},
            'bindings': [{'type': 'secret_text', 'name': 'SUB_CONFIG', 'text': json.dumps(config)}],
        }
        boundary = 'private-xui-' + secrets.token_hex(16)
        parts = []
        for field, mime, value in [('metadata', 'application/json', json.dumps(metadata)), ('worker.mjs', 'application/javascript+module', source)]:
            filename = '; filename="worker.mjs"' if field == 'worker.mjs' else ''
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"{filename}\r\n'
                          f'Content-Type: {mime}\r\n\r\n{value}\r\n').encode())
        parts.append(f'--{boundary}--\r\n'.encode())
        return self.call('PUT', f'/accounts/{account_id}/workers/scripts/{name}',
                         raw=b''.join(parts), content_type=f'multipart/form-data; boundary={boundary}')
