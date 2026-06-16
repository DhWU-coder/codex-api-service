"""Codex OAuth 凭据读取、刷新和登录。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

# 这些 OAuth 参数来自 Codex OAuth/参考实现使用的 ChatGPT 登录流程。
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
LOCAL_CALLBACK_WAIT_SECONDS = 15


@dataclass(frozen=True)
class CodexCredentials:
    """Codex backend 请求所需的 OAuth token。"""

    access: str
    refresh: str
    expires: int
    account_id: str | None = None
    id_token: str | None = None


def default_auth_path() -> Path:
    """返回本服务默认保存 OAuth 凭据的位置。"""
    # OPENAI_CODEX_HOME 兼容参考实现；否则使用用户目录，避免把 token 放进项目源码。
    base = os.environ.get("OPENAI_CODEX_HOME")
    if base:
        return Path(base).expanduser().resolve() / "auth.json"
    return Path.home() / ".codex-api-service" / "auth.json"


def default_import_auth_path() -> Path:
    """返回默认的已有 Codex OAuth 凭据导入位置。"""
    # CODEX_HOME 是 Codex 常见配置根目录；未设置时回落到 ~/.codex。
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser().resolve() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def refresh_openai_codex_token(refresh_token: str) -> CodexCredentials:
    """使用 refresh token 刷新 Codex OAuth token。"""
    payload = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    return _credentials_from_token_response(payload)


class CodexAuth:
    """管理 Codex OAuth 登录、读取和刷新。"""

    def __init__(
        self,
        *,
        auth_path: Path | str | None = None,
        import_auth_path: Path | str | None = None,
        codex_cli_auth_path: Path | str | None = None,
        open_browser: Callable[[str], bool] | None = None,
        input_func: Callable[[str], str] | None = None,
        refresh_func: Callable[[str], CodexCredentials] | None = None,
    ) -> None:
        """初始化认证管理器。"""
        self.auth_path = Path(auth_path) if auth_path is not None else default_auth_path()
        # codex_cli_auth_path 是旧参数名，保留只是为了兼容已有调用方。
        fallback_import_path = import_auth_path if import_auth_path is not None else codex_cli_auth_path
        self.import_auth_path = (
            Path(fallback_import_path) if fallback_import_path is not None else default_import_auth_path()
        )
        self.open_browser = open_browser or webbrowser.open
        self.input_func = input_func or input
        self.refresh_func = refresh_func or refresh_openai_codex_token

    def ensure_credentials(self) -> CodexCredentials:
        """确保存在未过期的 OAuth 凭据。"""
        credentials = self.load()
        if credentials is None:
            credentials = self._login()
            self.save(credentials)
            return credentials
        if credentials.expires and credentials.expires <= _now_ms() + 60_000:
            credentials = self.refresh_func(credentials.refresh)
            self.save(credentials)
        return credentials

    def load(self) -> CodexCredentials | None:
        """优先读取本服务凭据，缺失时导入已有 Codex OAuth 凭据。"""
        local = _parse_auth_file(self.auth_path)
        if local is not None:
            return local
        imported = _parse_auth_file(self.import_auth_path)
        if imported is not None:
            self.save(imported)
            return imported
        return None

    def save(self, credentials: CodexCredentials) -> None:
        """保存 OAuth 凭据到本服务 auth 文件。"""
        _write_auth_file(self.auth_path, credentials)

    def logout(self) -> None:
        """删除本服务 OAuth 凭据文件。"""
        try:
            self.auth_path.unlink()
        except FileNotFoundError:
            return

    def _login(self) -> CodexCredentials:
        """执行浏览器 PKCE OAuth 登录流程。"""
        verifier, challenge = _pkce_pair()
        state = secrets.token_hex(16)
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex-api-service",
        }
        auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        # 本地回调服务可自动接收浏览器跳转；端口占用时回落到手动粘贴 code。
        _CallbackHandler.code = None
        _CallbackHandler.expected_state = state
        server: HTTPServer | None = None
        try:
            server = HTTPServer(("127.0.0.1", 1455), _CallbackHandler)
            server.timeout = 1
        except OSError:
            server = None

        self.open_browser(auth_url)
        print(f"Open this URL to sign in if the browser did not open:\n{auth_url}")

        code = None
        if server is not None:
            deadline = time.time() + LOCAL_CALLBACK_WAIT_SECONDS
            while time.time() < deadline and not _CallbackHandler.code:
                server.handle_request()
            code = _CallbackHandler.code
            server.server_close()

        if not code:
            raw = self.input_func("Paste the authorization code or full redirect URL: ")
            code, returned_state = _parse_authorization_input(raw)
            if returned_state and returned_state != state:
                raise RuntimeError("State mismatch")
        if not code:
            raise RuntimeError("Missing authorization code")

        payload = _post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
        )
        return _credentials_from_token_response(payload)


class _CallbackHandler(BaseHTTPRequestHandler):
    """接收 OAuth redirect_uri 的本地 HTTP handler。"""

    code: str | None = None
    expected_state: str = ""

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭 http.server 默认访问日志，避免干扰 CLI 输出。"""
        return

    def do_GET(self) -> None:
        """处理 OAuth callback 请求。"""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("state", [None])[0] != self.expected_state:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch")
            return
        code = query.get("code", [None])[0]
        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code")
            return
        type(self).code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication successful. Return to the terminal to continue.")


def _parse_auth_file(path: Path) -> CodexCredentials | None:
    """从 Codex auth.json 中读取 ChatGPT OAuth token。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if data.get("auth_mode") != "chatgpt":
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        return None
    if not isinstance(refresh, str) or not refresh.strip():
        return None
    expires = data.get("expires")
    if not isinstance(expires, int):
        expires = _decode_jwt_expiry_ms(access) or 0
    account_id = tokens.get("account_id")
    id_token = tokens.get("id_token")
    return CodexCredentials(
        access=access.strip(),
        refresh=refresh.strip(),
        expires=expires,
        account_id=account_id if isinstance(account_id, str) and account_id else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def _write_auth_file(path: Path, credentials: CodexCredentials) -> None:
    """以 0600 权限写入 auth.json。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        "auth_mode": "chatgpt",
        "tokens": {
            **({"id_token": credentials.id_token} if credentials.id_token else {}),
            "access_token": credentials.access,
            "refresh_token": credentials.refresh,
            **({"account_id": credentials.account_id} if credentials.account_id else {}),
        },
        "expires": credentials.expires,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _credentials_from_token_response(payload: dict) -> CodexCredentials:
    """把 OAuth token endpoint 响应转换成 CodexCredentials。"""
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access, str) or not isinstance(refresh, str) or not isinstance(expires_in, int):
        raise RuntimeError("OAuth token response missing required fields")
    account_id = _account_id_from_access_token(access)
    if not account_id:
        raise RuntimeError("Failed to extract accountId from token")
    id_token = payload.get("id_token")
    return CodexCredentials(
        access=access,
        refresh=refresh,
        expires=_now_ms() + expires_in * 1000,
        account_id=account_id,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def _post_form(url: str, values: dict[str, str], timeout: int = 60) -> dict:
    """向 OAuth endpoint 提交表单并返回 JSON。"""
    data = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OAuth request failed: HTTP {error.code}: {body}") from error


def _parse_authorization_input(value: str) -> tuple[str | None, str | None]:
    """兼容 code、redirect URL、code#state 等手动输入格式。"""
    value = value.strip()
    if not value:
        return None, None
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.query:
            query = urllib.parse.parse_qs(parsed.query)
            return query.get("code", [None])[0], query.get("state", [None])[0]
    except Exception:
        pass
    if "#" in value:
        code, state = value.split("#", 1)
        return code, state
    if "code=" in value:
        query = urllib.parse.parse_qs(value)
        return query.get("code", [None])[0], query.get("state", [None])[0]
    return value, None


def _pkce_pair() -> tuple[str, str]:
    """生成 OAuth PKCE verifier/challenge。"""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _account_id_from_access_token(access_token: str) -> str | None:
    """从 access token JWT payload 中读取 ChatGPT account id。"""
    auth = _decode_jwt_payload(access_token).get(JWT_CLAIM_PATH)
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def _decode_jwt_expiry_ms(token: str) -> int | None:
    """读取 JWT exp 并转换为毫秒时间戳。"""
    exp = _decode_jwt_payload(token).get("exp")
    if isinstance(exp, (int, float)):
        return int(exp * 1000)
    return None


def _decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload；失败时返回空 dict。"""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _b64url(data: bytes) -> str:
    """生成不带 padding 的 base64url 字符串。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)
