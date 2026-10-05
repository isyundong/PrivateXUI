# Adapted from byJoey/xui-cf-deployer at c7c3d9a; see README.md.
import http.cookiejar


import ipaddress


import json


import os


import random


import re


import shutil


import sqlite3


import ssl


import subprocess


import sys


import time


import unicodedata


import uuid
import hashlib
import socket
import tempfile
from storage import atomic_private_write


from getpass import getpass


from typing import Any, Dict, List, Optional, Set, Tuple


from urllib import error, parse, request


from urllib.request import HTTPCookieProcessor, HTTPSHandler, build_opener


DB_PATH = "/etc/x-ui/x-ui.db"


PANEL_INFO_PATH = "/etc/x-ui/private-cf/panel.json"


PANEL_INFO_SNAPSHOT = "/etc/x-ui/private-cf/panel.txt"


XUI_INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/e897b0957a12c3a106f505e0551c18887b0528d6/install.sh"
XUI_INSTALL_SHA256 = "616d1758c993316d6b72fbe2c18b441d6003c05bf82d3c179e69550106b6369f"


XUI_INSTALL_STDIN = "\nn\n4\n\n"


PORT_MIN = 10000


PORT_MAX = 60000


PROTOCOL_ORDER = ["vless", "trojan", "vmess"]


PROTOCOL_SUFFIX = {"vless": "vl", "trojan": "tr", "vmess": "vm"}


PANEL_API_PREFIX = "panel/api"


BACKEND_DB = "db"


BACKEND_API = "api"


API_MIN_VERSION = (2, 0, 0)


XUI_BINARY_CANDIDATES = ("/usr/local/x-ui/x-ui", "/usr/bin/x-ui")


def exit_error(message: str) -> None:
    print(message)
    sys.exit(1)


def call_json_api(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
    exit_on_http_error: bool = True,
    opener: Optional[Any] = None,
):
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")

    req = request.Request(url=url, data=payload, headers=headers or {}, method=method)

    open_fn = opener.open if opener is not None else request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        if exit_on_http_error:
            print(body)
            sys.exit(1)
        if body:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"success": False, "errors": [{"message": body}]}
        return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}
    except error.URLError as e:
        exit_error(f"网络错误: {e}")

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


class XuiPanelClient:
    """3x-ui 面板 REST API 客户端（支持 Session 登录或 Bearer Token）。"""

    def __init__(self, base_url: str, token: Optional[str] = None, insecure_tls: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip() or None
        self.csrf_token: Optional[str] = None
        self.insecure_tls = insecure_tls
        jar = http.cookiejar.CookieJar()
        handlers: List[Any] = [HTTPCookieProcessor(jar)]
        if insecure_tls:
            handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
        self.opener = build_opener(*handlers)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if extra:
            headers.update(extra)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        require_success: bool = True,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        if auth_required and not self.token and not self.csrf_token:
            exit_error("未登录 3x-ui 面板，请先调用 login() 或提供 API Token")

        result = call_json_api(
            method=method,
            url=self._url(path),
            headers=self._headers(),
            data=data,
            opener=self.opener,
        )
        if require_success and not result.get("success", False):
            msg = result.get("msg") or result.get("message") or json.dumps(result, ensure_ascii=False)
            exit_error(f"3x-ui API 失败: {msg}")
        return result

    def fetch_csrf_token(self) -> str:
        result = self._request("GET", "csrf-token", require_success=True, auth_required=False)
        token = result.get("obj")
        if not isinstance(token, str) or not token:
            exit_error("获取 CSRF Token 失败")
        self.csrf_token = token
        return token

    def login(self, username: str, password: str, two_factor_code: str = "") -> None:
        self.fetch_csrf_token()
        payload: Dict[str, Any] = {"username": username, "password": password}
        if two_factor_code.strip():
            payload["twoFactorCode"] = two_factor_code.strip()
        self._request("POST", "login", data=payload, auth_required=False)
        if not self.csrf_token:
            exit_error("3x-ui 登录失败：未获得 CSRF Token")

    def list_inbounds(self) -> List[Dict[str, Any]]:
        result = self._request("GET", f"{PANEL_API_PREFIX}/inbounds/list")
        obj = result.get("obj")
        if isinstance(obj, list):
            return obj
        return []

    def add_inbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._request("POST", f"{PANEL_API_PREFIX}/inbounds/add", data=payload)
        obj = result.get("obj")
        if isinstance(obj, dict):
            return obj
        return {}

    def delete_inbound(self, inbound_id: int) -> None:
        self._request("POST", f"{PANEL_API_PREFIX}/inbounds/del/{inbound_id}")

    def restart_xray(self) -> None:
        self._request("POST", f"{PANEL_API_PREFIX}/server/restartXrayService")


def parse_version(version_text: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for token in re.split(r"[^0-9]+", version_text.strip()):
        if token.isdigit():
            parts.append(int(token))
    return tuple(parts) if parts else (0,)


def version_at_least(version_tuple: Tuple[int, ...], minimum: Tuple[int, ...]) -> bool:
    width = max(len(version_tuple), len(minimum))
    left = version_tuple + (0,) * (width - len(version_tuple))
    right = minimum + (0,) * (width - len(minimum))
    return left >= right


def find_xui_binary() -> Optional[str]:
    candidates: List[str] = []
    which = shutil.which("x-ui")
    if which:
        candidates.append(which)
    candidates.extend(XUI_BINARY_CANDIDATES)

    seen: Set[str] = set()
    for path in candidates:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            result = subprocess.run(
                [path, "-v"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        version = (result.stdout or result.stderr or "").strip().splitlines()
        if version and re.match(r"^\d", version[0]):
            return path
    return None


def read_xui_version(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "-v"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    if not text:
        return None
    return text[0]


def read_setting_from_db(key: str) -> Optional[str]:
    if not os.path.isfile(DB_PATH):
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = cur.fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def detect_panel_url() -> Tuple[str, bool]:
    env_url = os.environ.get("XUI_PANEL_URL", "").strip()
    if env_url:
        return env_url.rstrip("/"), env_url.lower().startswith("https://")

    port = read_setting_from_db("webPort") or "2053"
    base_path = read_setting_from_db("webBasePath") or "/"
    cert = (read_setting_from_db("webCertFile") or "").strip()
    key = (read_setting_from_db("webKeyFile") or "").strip()
    https = bool(cert and key)
    if not base_path.startswith("/"):
        base_path = f"/{base_path}"
    base_path = base_path.rstrip("/") or ""
    return f"{'https' if https else 'http'}://127.0.0.1:{port}{base_path}", https


def read_api_token_from_cli(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "setting", "-getApiToken"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    for line in output.splitlines():
        if line.startswith("apiToken:"):
            token = line.split(":", 1)[1].strip()
            return token or None
    return None


def is_xui_installed() -> bool:
    return os.path.isfile(DB_PATH) and find_xui_binary() is not None


def parse_credentials_from_install_output(output: str) -> Tuple[Optional[str], Optional[str]]:
    username: Optional[str] = None
    password: Optional[str] = None
    for line in output.splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        user_match = re.search(r"Username:\s*(\S+)", clean, re.I)
        if user_match:
            username = user_match.group(1)
        pass_match = re.search(r"Password:\s*(\S+)", clean, re.I)
        if pass_match:
            password = pass_match.group(1)
    return username, password


def is_password_hash(value: str) -> bool:
    return value.startswith(("$2a$", "$2b$", "$2y$"))


def run_xui_install_script() -> Tuple[str, str]:
    print("正在安装 3x-ui v3.9.0（SQLite / 随机端口 / 面板仅监听本机）...")
    with request.urlopen(XUI_INSTALL_URL, timeout=30) as response:
        script = response.read()
    if hashlib.sha256(script).hexdigest() != XUI_INSTALL_SHA256:
        exit_error("安装器校验失败，停止执行")
    # The pinned upstream noninteractive default exposes HTTP on all interfaces.
    # Change its one fixed bind default before executing; refuse unexpected source.
    old = b'bind_local="n"'
    if script.count(old) != 1:
        exit_error("安装器的本机绑定补丁不匹配，停止执行")
    script = script.replace(old, b'bind_local="y"')
    installer_env = dict(os.environ)
    installer_env.update(XUI_NONINTERACTIVE="1", XUI_DB_TYPE="sqlite", XUI_SSL_MODE="none")
    try:
        with tempfile.NamedTemporaryFile(prefix="private-xui-install-", suffix=".sh") as installer:
            installer.write(script)
            installer.flush()
            proc = subprocess.run(
                ["bash", installer.name, "v3.9.0"], input="", capture_output=True,
                text=True, timeout=900, check=False, env=installer_env,
            )
    except subprocess.TimeoutExpired:
        exit_error("3x-ui 安装超时")

    install_output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0:
        atomic_private_write(PANEL_INFO_PATH + ".install.log", install_output)
        exit_error(f"3x-ui 安装失败 (exit {proc.returncode})；详情保存为私有安装日志")

    for _ in range(45):
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "x-ui"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError:
            break
        if result.stdout.strip() == "active":
            print("3x-ui 安装完成，服务已启动")
            username, password = parse_credentials_from_install_output(install_output)
            if not username or not password:
                exit_error("3x-ui 安装成功但未解析到登录凭据，请检查安装输出")
            return username, password
        time.sleep(2)
    exit_error("3x-ui 安装完成但服务未启动，请检查 journalctl -u x-ui")


def shlex_quote(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_/@.+-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def read_panel_user_from_db() -> Tuple[str, str]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT username, password FROM users ORDER BY id LIMIT 1")
            row = cur.fetchone()
    except sqlite3.Error as e:
        exit_error(str(e))
    if not row or not row[0]:
        exit_error("未找到面板登录账号，请先安装 3x-ui")
    return str(row[0]), str(row[1] or "")


def collect_panel_access_info(
    *,
    installed_by_script: bool = False,
    plain_username: Optional[str] = None,
    plain_password: Optional[str] = None,
) -> Dict[str, Any]:
    binary = find_xui_binary()
    db_username, db_password = read_panel_user_from_db()
    username = plain_username or db_username
    password = plain_password or db_password
    if is_password_hash(password):
        exit_error("无法读取面板明文密码，请重新执行模式 4 全新安装")
    port_text = read_setting_from_db("webPort") or "2053"
    base_path = read_setting_from_db("webBasePath") or "/"
    listen_ip = "127.0.0.1" if installed_by_script else (
        read_setting_from_db("webListen") or read_setting_from_db("listenIP") or ""
    ).strip()
    cert = (read_setting_from_db("webCertFile") or "").strip()
    key = (read_setting_from_db("webKeyFile") or "").strip()
    https = bool(cert and key)
    scheme = "https" if https else "http"
    if not base_path.startswith("/"):
        base_path = f"/{base_path}"
    base_path = base_path.rstrip("/") or ""
    path_suffix = base_path if base_path else ""
    try:
        port = int(port_text)
    except ValueError:
        port = 2053
    local_host = "127.0.0.1"
    if listen_ip in ("127.0.0.1", "::1", "localhost"):
        local_host = "127.0.0.1"
    local_url = f"{scheme}://{local_host}:{port}{path_suffix}"
    public_url = ""
    if listen_ip not in ("127.0.0.1", "::1", "localhost"):
        try:
            public_ip = get_public_ipv4()
            public_url = f"{scheme}://{public_ip}:{port}{path_suffix}"
        except SystemExit:
            public_url = ""
    api_token = read_api_token_from_cli(binary) or os.environ.get("XUI_API_TOKEN", "").strip()
    info: Dict[str, Any] = {
        "username": username,
        "password": password,
        "port": port,
        "web_base_path": base_path or "/",
        "listen_ip": listen_ip,
        "access_url_local": local_url,
        "access_url_public": public_url,
        "api_token": api_token,
        "installed_at": int(time.time()),
    }
    if installed_by_script:
        info["installed_by_script"] = True
    return info


def save_panel_access_info(info: Dict[str, Any]) -> None:
    try:
        atomic_private_write(PANEL_INFO_PATH, json.dumps(info, ensure_ascii=False, indent=2))
    except OSError as e:
        exit_error(f"保存面板访问信息失败: {e}")

    lines = [
        "3x-ui 面板访问信息",
        f"用户名: {info.get('username', '')}",
        f"密码: {info.get('password', '')}",
        f"本机地址: {info.get('access_url_local', '')}",
    ]
    public_url = str(info.get("access_url_public") or "").strip()
    if public_url:
        lines.append(f"公网地址: {public_url}")
    api_token = str(info.get("api_token") or "").strip()
    if api_token:
        lines.append(f"API Token: {api_token}")
    lines.append("")
    try:
        atomic_private_write(PANEL_INFO_SNAPSHOT, "\n".join(lines))
    except OSError as e:
        exit_error(f"保存面板快照失败: {e}")


def ensure_xui_for_fresh_setup() -> None:
    if is_xui_installed():
        exit_error("检测到已安装 3x-ui，请使用模式 1 安装节点")
    username, password = run_xui_install_script()
    info = collect_panel_access_info(
        installed_by_script=True,
        plain_username=username,
        plain_password=password,
    )
    save_panel_access_info(info)
    print(f"面板信息已保存到 {PANEL_INFO_SNAPSHOT}")
    print(f"本机地址: {info['access_url_local']}")
    print(f"用户名: {info['username']}")
    print(f"密码: {info['password']}")


def panel_tls_insecure(panel_url: str, panel_https: bool) -> bool:
    if not panel_https:
        return False
    if os.environ.get("XUI_TLS_INSECURE", "").strip().lower() in ("1", "true", "yes", "y"):
        return True
    host = parse.urlparse(panel_url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def probe_panel_api(panel_url: str, api_token: Optional[str], insecure_tls: bool) -> bool:
    client = XuiPanelClient(panel_url, token=api_token, insecure_tls=insecure_tls)
    csrf = call_json_api(
        "GET",
        client._url("csrf-token"),
        headers=client._headers(),
        opener=client.opener,
        exit_on_http_error=False,
        timeout=8,
    )
    if csrf.get("success") and isinstance(csrf.get("obj"), str):
        return True
    if api_token:
        listed = call_json_api(
            "GET",
            client._url(f"{PANEL_API_PREFIX}/inbounds/list"),
            headers=client._headers(),
            opener=client.opener,
            exit_on_http_error=False,
            timeout=8,
        )
        return bool(listed.get("success"))
    return False


def api_auth_available(env: Dict[str, Any]) -> bool:
    return bool((env.get("api_token") or "").strip())


def detect_xui_environment() -> Dict[str, Any]:
    binary = find_xui_binary()
    version = read_xui_version(binary)
    version_tuple = parse_version(version) if version else (0,)
    db_available = os.path.isfile(DB_PATH)
    panel_url, panel_https = detect_panel_url()
    insecure_tls = panel_tls_insecure(panel_url, panel_https)
    api_token = os.environ.get("XUI_API_TOKEN", "").strip() or read_api_token_from_cli(binary)

    api_capable = version_tuple == (0,) or version_at_least(version_tuple, API_MIN_VERSION)
    api_reachable = False
    # Do not require a running panel to select the local DB backend.
    # Authenticated API operations themselves will validate connectivity.

    return {
        "binary": binary,
        "version": version,
        "version_tuple": version_tuple,
        "db_available": db_available,
        "panel_url": panel_url,
        "panel_https": panel_https,
        "insecure_tls": insecure_tls,
        "api_token": api_token,
        "api_capable": api_capable,
        "api_reachable": api_reachable,
    }


def auto_select_backend(
    env: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    explicit = os.environ.get("XUI_BACKEND", "").strip().lower()
    if explicit == BACKEND_DB:
        return BACKEND_DB, "环境变量 XUI_BACKEND=db"
    if explicit == BACKEND_API:
        if not api_auth_available(env):
            exit_error("已强制 API 模式，但未检测到 API Token")
        return BACKEND_API, "环境变量 XUI_BACKEND=api"

    from_state = backend_from_state(state)
    if from_state:
        return from_state, "状态文件记录"

    if api_auth_available(env):
        return BACKEND_API, "检测到 API Token，使用 API"

    if env.get("db_available"):
        return BACKEND_DB, "未检测到 API Token，使用数据库直写"

    exit_error("未检测到 API Token，且不存在本地数据库")


def resolve_backend(
    state: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any], str]:
    runtime = env or detect_xui_environment()
    backend, reason = auto_select_backend(runtime, state)
    return backend, runtime, reason


def setup_panel_client(env: Dict[str, Any], *, interactive: bool = True) -> XuiPanelClient:
    panel_url = os.environ.get("XUI_PANEL_URL", "").strip() or str(env["panel_url"])
    insecure = bool(env.get("insecure_tls"))
    token = os.environ.get("XUI_API_TOKEN", "").strip() or str(env.get("api_token") or "").strip()
    if not token:
        exit_error("API 模式需要 API Token（可通过 x-ui setting -getApiToken 获取）")
    return XuiPanelClient(panel_url, token=token, insecure_tls=insecure)


def backend_from_state(state: Optional[Dict[str, Any]]) -> Optional[str]:
    if not state:
        return None
    backend = str(state.get("backend", "")).strip().lower()
    if backend in (BACKEND_DB, BACKEND_API):
        return backend
    version = state.get("version")
    if version == 2:
        return BACKEND_API
    if version == 1:
        return BACKEND_DB
    return None


def get_public_ipv4() -> str:
    providers = [
        "https://api.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    for url in providers:
        try:
            with request.urlopen(url, timeout=8) as resp:
                ip_text = resp.read().decode("utf-8").strip()
            ipaddress.IPv4Address(ip_text)
            return ip_text
        except error.HTTPError as e:
            print(e.read().decode("utf-8", errors="ignore"))
            sys.exit(1)
        except Exception:
            continue
    exit_error("获取公网 IPv4 失败")


def client_email_for_route(short_id: str, protocol: str) -> str:
    """3x-ui 客户端 email：小写字母数字，无 @，与面板校验一致。"""
    return f"{short_id.lower()}{PROTOCOL_SUFFIX[protocol]}"


def now_ms() -> int:
    return int(time.time() * 1000)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cursor.fetchone() is not None


def has_v3_client_schema(conn: sqlite3.Connection) -> bool:
    return table_exists(conn, "clients") and table_exists(conn, "client_inbounds")


def inbound_client_entry(protocol: str, user_uuid: str, email: str, *, v3: bool = True) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "email": email,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "subId": "",
        "comment": "",
        "reset": 0,
        "flow": "",
        "tgId": 0 if v3 else "",
    }
    if protocol == "vless":
        entry["id"] = user_uuid
    elif protocol == "trojan":
        entry["password"] = user_uuid
    elif protocol == "vmess":
        entry["id"] = user_uuid
        entry["alterId"] = 0
        entry["security"] = "auto"
    else:
        raise ValueError(f"不支持的协议: {protocol}")
    return entry


def protocol_settings(protocol: str, user_uuid: str, email: str, *, v3: bool = True) -> Dict[str, Any]:
    client = inbound_client_entry(protocol, user_uuid, email, v3=v3)
    if protocol == "vless":
        return {
            "clients": [client],
            "decryption": "none",
            "encryption": "none",
            "fallbacks": [],
        }
    if protocol == "trojan":
        return {
            "clients": [client],
            "fallbacks": [],
        }
    if protocol == "vmess":
        return {
            "clients": [client],
        }
    raise ValueError(f"不支持的协议: {protocol}")


def upsert_v3_client_record(
    cursor: sqlite3.Cursor,
    protocol: str,
    user_uuid: str,
    email: str,
    ts_ms: int,
) -> int:
    uuid_val = user_uuid if protocol in ("vless", "vmess") else ""
    password_val = user_uuid if protocol == "trojan" else ""
    security_val = "auto" if protocol == "vmess" else ""

    cursor.execute("SELECT id FROM clients WHERE email = ?", (email,))
    row = cursor.fetchone()
    if row:
        raise ValueError("客户端 email 已存在，停止以防覆盖原客户端")

    cursor.execute(
        """
        INSERT INTO clients (
            email, sub_id, uuid, password, auth, flow, security, reverse,
            limit_ip, total_gb, expiry_time, enable, tg_id, group_name, comment, reset,
            created_at, updated_at
        ) VALUES (?, '', ?, ?, '', '', ?, '', 0, 0, 0, 1, 0, '', '', 0, ?, ?)
        """,
        (email, uuid_val, password_val, security_val, ts_ms, ts_ms),
    )
    return int(cursor.lastrowid)


def link_v3_client_inbound(
    cursor: sqlite3.Cursor,
    client_id: int,
    inbound_id: int,
    ts_ms: int,
    flow: str = "",
) -> None:
    cursor.execute("DELETE FROM client_inbounds WHERE inbound_id = ?", (inbound_id,))
    cursor.execute(
        """
        INSERT INTO client_inbounds (client_id, inbound_id, flow_override, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (client_id, inbound_id, flow, ts_ms),
    )


def ensure_v3_client_traffic(cursor: sqlite3.Cursor, conn: sqlite3.Connection, inbound_id: int, email: str) -> None:
    if not table_exists(conn, "client_traffics"):
        return
    cursor.execute("SELECT 1 FROM client_traffics WHERE email = ? LIMIT 1", (email,))
    if cursor.fetchone():
        cursor.execute(
            """
            UPDATE client_traffics
            SET inbound_id=?, enable=1, total=0, expiry_time=0, reset=0
            WHERE email=?
            """,
            (inbound_id, email),
        )
        return
    cursor.execute(
        """
        INSERT INTO client_traffics (
            inbound_id, enable, email, up, down, expiry_time, total, reset, last_online
        ) VALUES (?, 1, ?, 0, 0, 0, 0, 0, 0)
        """,
        (inbound_id, email),
    )


def sync_v3_client_for_inbound(
    conn: sqlite3.Connection,
    inbound_id: int,
    protocol: str,
    user_uuid: str,
    email: str,
    ts_ms: Optional[int] = None,
) -> None:
    if not has_v3_client_schema(conn):
        return
    ts = ts_ms if ts_ms is not None else now_ms()
    cursor = conn.cursor()
    client_id = upsert_v3_client_record(cursor, protocol, user_uuid, email, ts)
    link_v3_client_inbound(cursor, client_id, inbound_id, ts)
    ensure_v3_client_traffic(cursor, conn, inbound_id, email)


def cleanup_v3_clients_for_inbounds(conn: sqlite3.Connection, inbound_ids: List[int]) -> None:
    if not inbound_ids or not has_v3_client_schema(conn):
        return

    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(inbound_ids))
    cursor.execute(
        f"""
        SELECT DISTINCT c.email
        FROM clients c
        JOIN client_inbounds ci ON ci.client_id = c.id
        WHERE ci.inbound_id IN ({placeholders})
        """,
        inbound_ids,
    )
    emails = [str(row[0]) for row in cursor.fetchall() if row and row[0]]

    cursor.execute(f"DELETE FROM client_inbounds WHERE inbound_id IN ({placeholders})", inbound_ids)

    for email in emails:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM client_inbounds ci
            JOIN clients c ON c.id = ci.client_id
            WHERE c.email = ?
            """,
            (email,),
        )
        if int(cursor.fetchone()[0]) > 0:
            continue
        cursor.execute("DELETE FROM clients WHERE email = ?", (email,))
        if table_exists(conn, "client_traffics"):
            cursor.execute("DELETE FROM client_traffics WHERE email = ?", (email,))


def protocol_settings_legacy(protocol: str, user_uuid: str) -> Dict[str, Any]:
    """旧版 3x-ui：clients 嵌在 settings 内，email 可为空。"""
    if protocol == "vless":
        return {
            "clients": [{"id": user_uuid, "flow": "", "email": ""}],
            "decryption": "none",
            "encryption": "none",
            "fallbacks": [],
        }
    if protocol == "trojan":
        return {
            "clients": [{"password": user_uuid, "flow": "", "email": ""}],
            "fallbacks": [],
        }
    if protocol == "vmess":
        return {
            "clients": [{"id": user_uuid, "alterId": 0, "email": ""}],
        }
    raise ValueError(f"不支持的协议: {protocol}")


def ws_stream_settings(path: str) -> Dict[str, Any]:
    return {
        "network": "ws",
        "security": "none",
        "wsSettings": {"path": path},
    }


def sniffing_settings() -> Dict[str, Any]:
    return {
        "enabled": True,
        "destOverride": ["http", "tls"],
        "metadataOnly": False,
        "routeOnly": False,
    }


def allocate_settings() -> Dict[str, Any]:
    return {"strategy": "always", "refresh": 5, "concurrency": 3}


def build_inbound_payload(protocol: str, user_uuid: str, short_id: str, route: Dict[str, Any]) -> Dict[str, Any]:
    email = client_email_for_route(short_id, protocol)
    return {
        "enable": True,
        "remark": f"{short_id}-{protocol}",
        "listen": "",
        "port": route["port"],
        "protocol": protocol,
        "expiryTime": 0,
        "tag": f"{short_id}-{protocol}",
        "settings": json.dumps(protocol_settings(protocol, user_uuid, email, v3=True), separators=(",", ":")),
        "streamSettings": json.dumps(ws_stream_settings(route["path"]), separators=(",", ":")),
        "sniffing": json.dumps(sniffing_settings(), separators=(",", ":")),
    }


def load_existing_ports_db(conn: sqlite3.Connection) -> Set[int]:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT port FROM inbounds")
    except sqlite3.Error:
        return set()
    ports = set()
    for row in cursor.fetchall():
        try:
            ports.add(int(row[0]))
        except Exception:
            continue
    return ports


def load_existing_ports_api(client: XuiPanelClient) -> Set[int]:
    ports: Set[int] = set()
    for inbound in client.list_inbounds():
        try:
            ports.add(int(inbound.get("port", 0)))
        except (TypeError, ValueError):
            continue
    return ports


def random_ports(count: int, existing: Set[int]) -> List[int]:
    selected = set()
    for _ in range(10000):
        if len(selected) == count:
            return list(selected)
        p = random.randint(PORT_MIN, PORT_MAX)
        if p in existing or p in selected:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("0.0.0.0", p))
        except OSError:
            continue
        selected.add(p)
    raise ValueError("无法找到足够的空闲端口")


def parse_protocol_selection(raw: str) -> List[str]:
    text = raw.strip().lower()
    if not text:
        return list(PROTOCOL_ORDER)

    index_mapping = {"1": "vless", "2": "trojan", "3": "vmess"}
    name_mapping = {"vless": "vless", "trojan": "trojan", "vmess": "vmess"}

    selected: List[str] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        protocol = index_mapping.get(token) or name_mapping.get(token)
        if protocol is None:
            exit_error(f"无效协议选项: {token}")
        if protocol not in selected:
            selected.append(protocol)

    if not selected:
        exit_error("至少选择一个协议")
    return selected


def get_inbounds_schema(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(inbounds)")
    rows = cursor.fetchall()
    schema: List[Dict[str, Any]] = []
    for row in rows:
        schema.append(
            {
                "name": row[1],
                "type": (row[2] or "").upper(),
                "notnull": bool(row[3]),
                "default": row[4],
                "pk": bool(row[5]),
            }
        )
    return schema


def load_template_inbound(conn: sqlite3.Connection) -> Dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM inbounds ORDER BY id LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        return {}
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


def infer_default_value(col_type: str):
    if "INT" in col_type:
        return 0
    if "REAL" in col_type or "FLOA" in col_type or "DOUB" in col_type:
        return 0
    if "BLOB" in col_type:
        return b""
    return ""


def insert_inbounds_db(
    db_path: str,
    user_uuid: str,
    short_id: str,
    routes: List[Dict[str, Any]],
) -> List[int]:
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        exit_error(str(e))

    try:
        schema = get_inbounds_schema(conn)
        if not schema:
            exit_error("未找到 inbounds 表")
        template = load_template_inbound(conn)
        cursor = conn.cursor()
        inserted_ids: List[int] = []
        v3_schema = has_v3_client_schema(conn)
        ts_ms = now_ms()

        for route in routes:
            protocol = route["protocol"]
            email = client_email_for_route(short_id, protocol)
            settings = protocol_settings(protocol, user_uuid, email, v3=v3_schema)
            row_data = dict(template)
            row_data.update(
                {
                    "user_id": 1,
                    "enable": 1,
                    "up": 0,
                    "down": 0,
                    "total": 0,
                    "expiry_time": 0,
                    "remark": f"{short_id}-{protocol}",
                    "listen": "",
                    "port": route["port"],
                    "protocol": protocol,
                    "settings": json.dumps(settings, separators=(",", ":")),
                    "stream_settings": json.dumps(ws_stream_settings(route["path"]), separators=(",", ":")),
                    "sniffing": json.dumps(sniffing_settings(), separators=(",", ":")),
                    "allocate": json.dumps(allocate_settings(), separators=(",", ":")),
                    "tag": f"{short_id}-{protocol}",
                }
            )

            columns: List[str] = []
            values: List[Any] = []
            for col in schema:
                name = col["name"]
                if col["pk"]:
                    continue
                if name in row_data:
                    columns.append(name)
                    values.append(row_data[name])
                    continue
                if col["notnull"] and col["default"] is None:
                    columns.append(name)
                    values.append(infer_default_value(col["type"]))

            placeholders = ",".join(["?"] * len(columns))
            sql = f"INSERT INTO inbounds ({','.join(columns)}) VALUES ({placeholders})"
            cursor.execute(sql, values)
            inbound_id = int(cursor.lastrowid)
            inserted_ids.append(inbound_id)
            if v3_schema:
                sync_v3_client_for_inbound(conn, inbound_id, protocol, user_uuid, email, ts_ms)

        conn.commit()
        return inserted_ids
    except sqlite3.Error as e:
        print(str(e))
        sys.exit(1)
    finally:
        conn.close()


def delete_inbounds_db(db_path: str, inbound_ids: List[int], tags: List[str]) -> None:
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        exit_error(str(e))

    try:
        cursor = conn.cursor()
        if inbound_ids:
            cleanup_v3_clients_for_inbounds(conn, inbound_ids)
            placeholders = ",".join(["?"] * len(inbound_ids))
            cursor.execute(f"DELETE FROM inbounds WHERE id IN ({placeholders})", inbound_ids)
        elif tags:
            cursor.execute(
                f"SELECT id FROM inbounds WHERE tag IN ({','.join(['?'] * len(tags))})",
                tags,
            )
            resolved_ids = [int(row[0]) for row in cursor.fetchall()]
            if resolved_ids:
                cleanup_v3_clients_for_inbounds(conn, resolved_ids)
            placeholders = ",".join(["?"] * len(tags))
            cursor.execute(f"DELETE FROM inbounds WHERE tag IN ({placeholders})", tags)
        conn.commit()
    except sqlite3.Error as e:
        print(str(e))
        sys.exit(1)
    finally:
        conn.close()


def restart_xui_service() -> None:
    try:
        result = subprocess.run(
            ["systemctl", "restart", "x-ui"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stderr.strip():
            print(result.stderr.strip())
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()
        if stderr:
            print(stderr)
        elif stdout:
            print(stdout)
        else:
            print(str(e))
        sys.exit(1)


def create_inbounds_via_api(
    client: XuiPanelClient,
    user_uuid: str,
    short_id: str,
    routes: List[Dict[str, Any]],
) -> List[int]:
    inserted_ids: List[int] = []
    for route in routes:
        protocol = route["protocol"]
        payload = build_inbound_payload(protocol, user_uuid, short_id, route)
        created = client.add_inbound(payload)
        inbound_id = created.get("id")
        if inbound_id is None:
            exit_error(f"创建 {protocol} 入站失败：API 未返回 id")
        inserted_ids.append(int(inbound_id))
    client.restart_xray()
    return inserted_ids


def delete_inbounds_via_api(client: XuiPanelClient, inbound_ids: List[int]) -> None:
    for inbound_id in inbound_ids:
        client.delete_inbound(inbound_id)
    if inbound_ids:
        client.restart_xray()


def create_inbounds(
    backend: str,
    user_uuid: str,
    short_id: str,
    routes: List[Dict[str, Any]],
    panel: Optional[XuiPanelClient] = None,
) -> List[int]:
    if backend == BACKEND_API:
        if panel is None:
            exit_error("API 模式需要已登录的面板客户端")
        return create_inbounds_via_api(panel, user_uuid, short_id, routes)
    inbound_ids = insert_inbounds_db(DB_PATH, user_uuid, short_id, routes)
    restart_xui_service()
    return inbound_ids


def delete_managed_inbounds(
    backend: str,
    inbound_ids: List[int],
    tags: List[str],
    panel: Optional[XuiPanelClient] = None,
) -> None:
    if backend == BACKEND_API:
        if panel is None:
            exit_error("API 模式需要已登录的面板客户端")
        delete_inbounds_via_api(panel, inbound_ids)
        return
    delete_inbounds_db(DB_PATH, inbound_ids, tags)
    restart_xui_service()
