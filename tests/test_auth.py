import json
from pathlib import Path

from codex_api_service.auth import CodexAuth, CodexCredentials


def write_auth_file(path: Path, *, access: str, refresh: str, expires: int) -> None:
    """写入测试用 OAuth 文件，避免依赖真实本机凭据。"""
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "account_id": "acct_test",
                },
                "expires": expires,
            }
        ),
        encoding="utf-8",
    )


def test_ensure_credentials_imports_fresh_auth_when_local_refresh_token_invalidated(tmp_path: Path) -> None:
    """验证本服务旧 refresh token 失效时会回退导入仍有效的 Codex 登录凭据。"""
    local_auth = tmp_path / "service-auth.json"
    import_auth = tmp_path / "codex-auth.json"
    write_auth_file(local_auth, access="old-access", refresh="old-refresh", expires=1)
    write_auth_file(import_auth, access="fresh-access", refresh="fresh-refresh", expires=4_102_444_800_000)
    refresh_tokens: list[str] = []

    def refresh_with_invalidated_error(refresh_token: str) -> CodexCredentials:
        """模拟 OAuth 服务端返回 refresh token 已失效。"""
        refresh_tokens.append(refresh_token)
        raise RuntimeError(
            'OAuth request failed: HTTP 401: {"error":{"code":"refresh_token_invalidated"}}'
        )

    auth = CodexAuth(
        auth_path=local_auth,
        import_auth_path=import_auth,
        refresh_func=refresh_with_invalidated_error,
        open_browser=lambda _url: False,
        input_func=lambda _prompt: "",
    )

    credentials = auth.ensure_credentials()

    assert credentials.access == "fresh-access"
    assert credentials.refresh == "fresh-refresh"
    assert refresh_tokens == ["old-refresh"]
    saved = json.loads(local_auth.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "fresh-access"
