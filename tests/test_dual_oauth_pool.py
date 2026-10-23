"""通过真实池和客户端验证双授权回退，不访问真实上游服务。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import codex_api_service.codex_client as client_module
import codex_api_service.oauth_pool as pool_module
from codex_api_service.auth import CodexAuth, CodexCredentials
from codex_api_service.codex_client import CodexHTTPStatusError
from codex_api_service.codex_usage import CodexUsageAuthError
from codex_api_service.config import AppConfig, AuthConfig
from codex_api_service.oauth_pool import OAuthAccountPool
from codex_api_service.oauth_scheduler import BoundAccountUnavailable, NoAvailableOAuthAccounts


def credential(access: str, *, expires: int = 4_102_444_800_000) -> CodexCredentials:
    """为同一身份构造两份完全本地的测试凭据。"""
    return CodexCredentials(access, f"refresh-{access}", expires, "same-account")


async def dual_pool(tmp_path: Path) -> tuple[OAuthAccountPool, str]:
    """跳过全局导入及后台网络请求，只注册一个双授权账号。"""
    pool = OAuthAccountPool(config=AppConfig(
        project_root=tmp_path, auth=AuthConfig(account_store_path=tmp_path / "accounts"),
    ))
    record = await pool.store.import_credentials(credential("cli-backup"), source="codex-cli")
    await pool.store.import_credentials(credential("web-latest"), source="web-oauth")
    pool._initialized = True
    pool._rebuild_runtimes()
    return pool, record.key


def mock_upstream(monkeypatch: pytest.MonkeyPatch, handler: Callable) -> None:
    """把客户端连接替换为内存传输层，同时保留真实认证和解析流程。"""
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("status", [401, 403])
async def test_request_retries_backup_within_same_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool, bound: bool, status: int,
) -> None:
    """验证普通请求和绑定链均先在原账号内部切换授权。"""
    pool, key = await dual_pool(tmp_path)
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        """最新 Web 失效但旧 CLI 有效。"""
        token = request.headers["authorization"]
        used.append(token)
        if token == "Bearer web-latest":
            return httpx.Response(status, json={"error": {"code": "invalid_token", "message": "denied"}})
        return httpx.Response(200, json={"id": "resp_ok", "output_text": "ok"})

    mock_upstream(monkeypatch, handler)
    payload: dict[str, Any] = {"model": "test", "input": "hello"}
    if bound:
        pool.bindings.bind("resp_previous", key)
        payload["previous_response_id"] = "resp_previous"
    try:
        if stream:
            events = [event async for event in pool.stream_response(payload)]
            assert events[-1]["response"]["id"] == "resp_ok"
        else:
            assert (await pool.create_response(payload))["id"] == "resp_ok"
        assert used == ["Bearer web-latest", "Bearer cli-backup"]
        assert pool.request_account_metadata()["account_key"] == key
        assert pool.bindings.lookup("resp_ok") == key
        assert pool.snapshot()["accounts"][0]["source"] == "codex-cli"
        assert pool.runtimes[key].status == "available"
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_expired_access_token_refresh_failure_uses_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证先刷新过期授权，明确刷新失效后才回退另一来源。"""
    pool, key = await dual_pool(tmp_path)
    await pool.store.import_credentials(credential("web-expired", expires=1), source="web-oauth")
    used = []

    def invalid_refresh(refresh_token: str) -> CodexCredentials:
        """模拟原来源刷新令牌已失效。"""
        assert refresh_token == "refresh-web-expired"
        raise RuntimeError("refresh_token_invalidated")

    def handler(request: httpx.Request) -> httpx.Response:
        """只有备用授权会真正发出模型请求。"""
        used.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": "resp_backup"})

    pool.runtimes[key].client.auth.refresh_func = invalid_refresh
    mock_upstream(monkeypatch, handler)
    try:
        assert (await pool.create_response({"model": "test"}))["id"] == "resp_backup"
        assert used == ["Bearer cli-backup"]
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_both_invalid_are_persisted_and_not_retried_on_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: bool,
) -> None:
    """验证最多尝试两份凭据，全部失效后保持隔离并释放并发槽。"""
    pool, key = await dual_pool(tmp_path)
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        """拒绝两个来源。"""
        used.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": {"code": "invalid_token"}})

    mock_upstream(monkeypatch, handler)
    pool.bindings.bind("resp_bound", key)
    payload = {"previous_response_id": "resp_bound"} if bound else {}
    try:
        with pytest.raises(BoundAccountUnavailable if bound else NoAvailableOAuthAccounts):
            await pool.create_response(payload)
        assert len(used) == 2
        assert pool.runtimes[key].status == "login_required"
        with pytest.raises(NoAvailableOAuthAccounts):
            await pool.create_response({})
        assert len(used) == 2
        assert pool.scheduler.active_snapshot()[0] == 0
        restarted = OAuthAccountPool(config=pool.config)
        try:
            restarted._rebuild_runtimes()
            assert restarted.runtimes[key].status == "login_required"
        finally:
            await restarted.close()
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 503])
async def test_non_auth_errors_never_switch_authorizations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    """验证额度不足、服务过载及参数错误不消耗备用授权。"""
    pool, key = await dual_pool(tmp_path)
    used = []

    async def no_sleep(_delay: float) -> None:
        """跳过上游临时重试退避以缩短测试。"""
        return None

    async def no_usage(_key: str) -> None:
        """隔离 429 触发的额度读取，不启动外部进程。"""
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        """所有请求均返回同一非认证错误。"""
        used.append(request.headers["authorization"])
        return httpx.Response(status, json={"error": {"code": "temporary_error"}})

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(pool, "refresh_account", no_usage)
    mock_upstream(monkeypatch, handler)
    try:
        with pytest.raises(CodexHTTPStatusError):
            await pool.create_response({})
        assert set(used) == {"Bearer web-latest"}
        assert not pool.store.authorizations(key).rejected
        assert pool.store.authorizations(key).select().source == "web-oauth"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_old_inflight_401_does_not_reject_new_web_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证旧请求迟到的拒绝仍携带实际令牌指纹，不误伤当前来源的新登录。"""
    pool, key = await dual_pool(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    used = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """暂停旧请求直到新登录已经保存。"""
        token = request.headers["authorization"]
        used.append(token)
        if token == "Bearer web-latest":
            started.set()
            await finish.wait()
            return httpx.Response(401, json={"error": {"code": "token_revoked"}})
        return httpx.Response(200, json={"id": "resp_new_login"})

    mock_upstream(monkeypatch, handler)
    task = asyncio.create_task(pool.create_response({}))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await pool.store.import_credentials(credential("web-new-login"), source="web-oauth")
        pool._rebuild_runtimes()
        finish.set()
        assert (await asyncio.wait_for(task, timeout=1))["id"] == "resp_new_login"
        assert used == ["Bearer web-latest", "Bearer web-new-login"]
        assert pool.store.authorizations(key).select().source == "web-oauth"
        assert pool.runtimes[key].status == "available"
    finally:
        finish.set()
        await task
        await pool.close()


@pytest.mark.asyncio
async def test_auth_error_after_stream_output_changes_future_source_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证部分输出后只报错，保留备用授权供下一次请求使用。"""
    pool, key = await dual_pool(tmp_path)
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        """在流式正文中先输出事件，再返回语义认证失败。"""
        used.append(request.headers["authorization"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            'data: {"type":"error","error":{"type":"authentication_error","code":"invalid_api_key","message":"denied"}}\n\n'
        ))

    mock_upstream(monkeypatch, handler)
    try:
        stream = pool.stream_response({})
        assert (await anext(stream))["delta"] == "partial"
        with pytest.raises(CodexHTTPStatusError):
            await anext(stream)
        assert used == ["Bearer web-latest"]
        assert pool.store.authorizations(key).select().source == "codex-cli"
        assert pool.runtimes[key].status == "available"
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_quota_auth_failure_falls_back_and_reads_backup_profile_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证额度会话使用来源独立目录，明确认证失败后关闭旧会话并回退。"""
    pool, key = await dual_pool(tmp_path)
    homes = []
    closed = []

    class UsageSession:
        """按授权来源返回认证失败或有效额度。"""

        def __init__(self, *, account_home: Path) -> None:
            """记录额度进程实际使用的目录。"""
            self.home = account_home
            homes.append(account_home)

        async def read_rate_limits(self, _version: str, _timeout: float) -> dict:
            """Web 明确失效，CLI 可读取。"""
            if self.home.parent.name == "authorizations" and self.home.name == "web-oauth":
                raise CodexUsageAuthError("requires login")
            return {"rateLimits": {"planType": "pro", "primary": {"usedPercent": 2, "windowDurationMins": 300}}}

        async def close(self) -> None:
            """记录旧会话被及时回收。"""
            closed.append(self.home)

    monkeypatch.setattr(pool_module, "CodexUsageSession", UsageSession)
    try:
        usage = await pool.refresh_account(key)
        assert usage["rateLimit"]["windows"][0]["remainingPercent"] == 98
        assert [home.name for home in homes] == ["web-oauth", "codex-cli"]
        assert [home.name for home in closed] == ["web-oauth"]
        assert pool.runtimes[key].status == "available"
        public = json.dumps(pool.snapshot())
        assert "web-latest" not in public and "cli-backup" not in public
        assert "fingerprint" not in public.lower()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_quota_refresh_during_native_token_rotation_returns_fresh_data_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证额度进程自己旋转令牌后，本次刷新直接重读新代次，不留下旧缓存。"""
    pool, key = await dual_pool(tmp_path)
    calls = []

    class UsageSession:
        """首次读取模拟原生刷新，第二次使用新令牌返回稳定额度。"""

        def __init__(self, *, account_home: Path) -> None:
            """保存与原生读取器相同的凭据路径。"""
            self.auth_path = account_home / "auth.json"

        async def read_rate_limits(self, _version: str, _timeout: float) -> dict:
            """首次落盘新凭据，并对两次读取返回不同额度以识别旧结果。"""
            calls.append(self.auth_path)
            if len(calls) == 1:
                CodexAuth(auth_path=self.auth_path).save(credential("web-native-refreshed"))
            return {"rateLimits": {"planType": "pro", "primary": {
                "usedPercent": 70 if len(calls) == 1 else 3, "windowDurationMins": 300,
            }}}

        async def close(self) -> None:
            """测试不启动真实进程。"""
            return None

    monkeypatch.setattr(pool_module, "CodexUsageSession", UsageSession)
    try:
        usage = await pool.refresh_account(key)
        assert len(calls) == 2
        assert usage["rateLimit"]["windows"][0]["remainingPercent"] == 97
        assert pool.store.authorizations(key).select().credentials.access == "web-native-refreshed"
    finally:
        await pool.close()
