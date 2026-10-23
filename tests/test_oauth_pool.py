import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from codex_api_service.codex_client import CodexHTTPStatusError
import codex_api_service.oauth_pool as oauth_pool_module
from codex_api_service.auth import CodexAuth, CodexCredentials, OAuthLoginRequired
from codex_api_service.config import AppConfig, AuthConfig
from codex_api_service.oauth_accounts import OAuthAccountRecord
from codex_api_service.oauth_pool import AccountRuntime, OAuthAccountPool
from codex_api_service.oauth_scheduler import BoundAccountUnavailable, NoAvailableOAuthAccounts


class SuccessfulAccountClient:
    """模拟由指定 OAuth 账户成功完成请求的上游客户端。"""

    async def create_response(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """返回固定非流式响应。"""
        return {"id": "resp_account", "output_text": "ok"}

    async def stream_response(self, _payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回带完整响应的固定流式事件。"""
        yield {
            "type": "response.completed",
            "response": {"id": "resp_account", "output_text": "ok"},
        }


class FailingAccountClient(SuccessfulAccountClient):
    """模拟账户已被选中、但上游请求最终失败的客户端。"""

    async def create_response(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """返回不可切换的上游错误，便于验证最后尝试账户。"""
        raise CodexHTTPStatusError(400, "bad request")


class FakeUsageSession:
    """记录账号池对持久额度会话的读取和关闭。"""

    def __init__(self, usage: dict[str, Any]) -> None:
        self.usage = usage
        self.read_count = 0
        self.close_count = 0

    async def read_rate_limits(self, _client_version: str, _timeout: float) -> dict[str, Any]:
        """返回已脱敏测试额度快照。"""
        self.read_count += 1
        return self.usage

    async def close(self) -> None:
        """记录会话被账号池关闭。"""
        self.close_count += 1


class AuthResultClient(SuccessfulAccountClient):
    """记录请求次数，并按需在首个事件前后抛出认证错误。"""

    def __init__(self, error: Exception | None = None, *, emit_first: bool = False) -> None:
        """保存本地测试结果，不触及真实 OAuth 服务。"""
        self.error = error
        self.emit_first = emit_first
        self.calls = 0

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """让非流式测试观察是否真正尝试了健康账号。"""
        self.calls += 1
        if self.error:
            raise self.error
        return await super().create_response(payload)

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """首个事件前可切换账号，事件发出后必须直接报错。"""
        self.calls += 1
        if self.emit_first:
            yield {"type": "response.output_text.delta", "delta": "partial"}
        if self.error:
            raise self.error
        async for event in super().stream_response(payload):
            yield event


def usage_snapshot(remaining: float) -> dict[str, Any]:
    """构造账号池刷新逻辑需要的稳定额度摘要。"""
    return {
        "planType": "pro",
        "rateLimit": {
            "allowed": True,
            "windows": [
                {
                    "kind": "primary",
                    "label": "5h",
                    "remainingPercent": remaining,
                    "limitWindowSeconds": 18_000,
                    "resetAt": 4_102_444_800,
                }
            ],
        },
        "additionalRateLimits": [],
        "credits": {
            "hasCredits": False,
            "unlimited": False,
            "overageLimitReached": False,
            "balance": "0",
        },
    }


async def add_pool_account(pool: OAuthAccountPool, account_id: str = "acct-usage") -> OAuthAccountRecord:
    """向账号池持久化一个无需真实登录的测试账号。"""
    record = await pool.store.import_credentials(
        CodexCredentials(
            access=f"access-{account_id}",
            refresh=f"refresh-{account_id}",
            expires=4_102_444_800_000,
            account_id=account_id,
        ),
        source="test",
    )
    pool._rebuild_runtimes()
    return record


def make_account_pool(tmp_path: Path, client: SuccessfulAccountClient) -> OAuthAccountPool:
    """构造已初始化且只包含一个安全测试账户的账户池。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    record = OAuthAccountRecord(
        key="account-a",
        alias="owner@example.com",
        source="test",
    )
    # 模拟已注册且拥有独立凭据的账号，避免测试跳过真实的入池检查。
    pool.store._records[record.key] = record
    CodexAuth(auth_path=pool.store.auth_path(record.key)).save(
        CodexCredentials("test-access", "test-refresh", 4_102_444_800_000, "test-account")
    )
    pool.runtimes[record.key] = AccountRuntime(record=record, client=client)  # type: ignore[arg-type]
    pool._update_scheduler()
    pool._initialized = True
    return pool


@pytest.mark.asyncio
async def test_create_response_exposes_selected_account_metadata(tmp_path: Path) -> None:
    """验证非流式请求完成后可读取实际处理账户的安全元数据。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    try:
        await pool.create_response({"model": "gpt-5.5", "input": "hello"})

        assert pool.request_account_metadata() == {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
        }
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_stream_response_exposes_selected_account_metadata(tmp_path: Path) -> None:
    """验证流式请求结束后仍可读取实际处理账户。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    try:
        events = [event async for event in pool.stream_response({"model": "gpt-5.5", "input": "hello"})]

        assert events[-1]["type"] == "response.completed"
        assert pool.request_account_metadata() == {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
        }
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_failed_response_keeps_last_attempted_account_metadata(tmp_path: Path) -> None:
    """验证上游失败时保留最后一次实际尝试的账户。"""
    pool = make_account_pool(tmp_path, FailingAccountClient())
    try:
        with pytest.raises(CodexHTTPStatusError):
            await pool.create_response({"model": "gpt-5.5", "input": "hello"})

        assert pool.request_account_metadata() == {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
        }
    finally:
        await pool.close()


async def add_healthy_fallback(pool: OAuthAccountPool) -> tuple[OAuthAccountRecord, AuthResultClient]:
    """增加健康账号并固定首选失效账号，确保回退测试不依赖随机时间权重。"""
    record = await add_pool_account(pool, "healthy")
    client = AuthResultClient()
    pool.runtimes[record.key].client = client
    pool.scheduler._smooth_current["account-a"] = 100
    return record, client


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_status", [None, 401, 403])
async def test_auth_failure_is_quarantined_and_request_uses_healthy_account(
    tmp_path: Path, stream: bool, error_status: int | None
) -> None:
    """验证本地登录失败和上游认证错误都会隔离账号，并在未输出时自动回退。"""
    error = OAuthLoginRequired("requires login") if error_status is None else CodexHTTPStatusError(error_status, "denied")
    broken = AuthResultClient(error)
    pool = make_account_pool(tmp_path, broken)
    healthy_record, healthy = await add_healthy_fallback(pool)
    try:
        if stream:
            events = [event async for event in pool.stream_response({"model": "test"})]
            assert events[-1]["type"] == "response.completed"
        else:
            assert (await pool.create_response({"model": "test"}))["output_text"] == "ok"
        assert broken.calls == healthy.calls == 1
        assert pool.runtimes["account-a"].status == "login_required"
        assert "account-a" not in pool._available_keys()
        assert pool.request_account_metadata()["account_key"] == healthy_record.key
        item = next(item for item in pool.snapshot()["accounts"] if item["key"] == "account-a")
        assert item["enabled"] is True
        assert item["weight"] == item["estimatedShare"] == 0
        assert "重新登录" in item["lastError"]
        await pool.create_response({"model": "test"})
        assert broken.calls == 1
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_bound_auth_failure_marks_account_but_never_switches(tmp_path: Path, stream: bool) -> None:
    """验证绑定会话首次发现认证失效时也隔离账号，并明确返回 409。"""
    broken = AuthResultClient(OAuthLoginRequired("requires login"))
    pool = make_account_pool(tmp_path, broken)
    _, healthy = await add_healthy_fallback(pool)
    pool.bindings.bind("resp_bound", "account-a")
    try:
        with pytest.raises(BoundAccountUnavailable) as caught:
            if stream:
                _ = [event async for event in pool.stream_response({"previous_response_id": "resp_bound"})]
            else:
                await pool.create_response({"previous_response_id": "resp_bound"})
        assert caught.value.status_code == 409
        assert healthy.calls == 0
        assert pool.runtimes["account-a"].status == "login_required"
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("http_error", [False, True])
async def test_auth_failure_after_stream_output_never_switches(tmp_path: Path, http_error: bool) -> None:
    """验证流式已输出时只终止本次请求，但仍禁止失效账号继续参与后续轮换。"""
    error = CodexHTTPStatusError(401, "denied") if http_error else OAuthLoginRequired("requires login")
    pool = make_account_pool(tmp_path, AuthResultClient(error, emit_first=True))
    _, healthy = await add_healthy_fallback(pool)
    try:
        stream = pool.stream_response({"model": "test"})
        assert (await anext(stream))["delta"] == "partial"
        with pytest.raises(type(error)):
            await anext(stream)
        assert healthy.calls == 0
        assert pool.runtimes["account-a"].status == "login_required"
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_all_accounts_require_login_returns_503_without_leaking_slots(tmp_path: Path, stream: bool) -> None:
    """验证没有健康账号时及时返回明确的服务不可用错误，并释放并发槽。"""
    broken = AuthResultClient(OAuthLoginRequired("requires login"))
    pool = make_account_pool(tmp_path, broken)
    try:
        for _ in range(2):
            with pytest.raises(NoAvailableOAuthAccounts) as caught:
                if stream:
                    _ = [event async for event in pool.stream_response({"model": "test"})]
                else:
                    await pool.create_response({"model": "test"})
            assert caught.value.status_code == 503
            assert caught.value.error_code == "oauth_accounts_unavailable"
        assert broken.calls == 1
        assert pool.scheduler.active_snapshot()[0] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_invalid_credentials_never_enter_rotation(tmp_path: Path, missing: bool) -> None:
    """验证注册表中的账号在凭据丢失或格式无效时，首次请求之前就退出调度。"""
    pool = make_account_pool(tmp_path, AuthResultClient())
    path = pool.store.auth_path("account-a")
    if missing:
        path.unlink()
    else:
        path.write_text("[]", encoding="utf-8")
    try:
        snapshot = pool.snapshot()
        assert snapshot["accounts"][0]["status"] == "login_required"
        assert snapshot["accounts"][0]["weight"] == 0
        assert pool._available_keys() == set()
        with pytest.raises(NoAvailableOAuthAccounts):
            await pool.create_response({"model": "test"})
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_auth_failure_only_recovers_after_credentials_change(tmp_path: Path) -> None:
    """验证重复同步旧令牌或修改别名不会解封，新的未过期凭据可恢复同一账号。"""
    pool = make_account_pool(tmp_path, AuthResultClient(OAuthLoginRequired("requires login")))
    auth = CodexAuth(auth_path=pool.store.auth_path("account-a"))
    old_credentials = auth.load()
    try:
        with pytest.raises(NoAvailableOAuthAccounts):
            await pool.create_response({"model": "test"})
        # 模拟失效凭据被清除后，全局同步又写回同一份旧文件。
        pool.store.auth_path("account-a").unlink()
        pool._rebuild_runtimes()
        auth.save(old_credentials)
        await pool.store.update("account-a", alias="新别名")
        pool._rebuild_runtimes()
        assert pool.runtimes["account-a"].status == "login_required"
        assert pool.runtimes["account-a"].last_error
        # 重启读取同一份注册表和认证文件，也不能重新启用已被拒绝的旧令牌。
        restarted = OAuthAccountPool(config=pool.config)
        try:
            restarted._rebuild_runtimes()
            assert restarted.runtimes["account-a"].status == "login_required"
            assert "account-a" not in restarted._available_keys()
        finally:
            await restarted.close()
        # 新文件如果仍已过期，也不能被当作一次成功的重新登录。
        auth.save(CodexCredentials("new-expired", "new-refresh", 1, "test-account"))
        pool._rebuild_runtimes()
        assert pool.runtimes["account-a"].status == "login_required"
        auth.save(CodexCredentials("new-access", "new-refresh", 4_102_444_800_000, "test-account"))
        pool._rebuild_runtimes()
        assert pool.runtimes["account-a"].status == "available"
        assert pool.runtimes["account-a"].last_error is None
        assert pool.runtimes["account-a"].next_refresh_at == 0
        assert "account-a" in pool._available_keys()
        restarted = OAuthAccountPool(config=pool.config)
        try:
            restarted._rebuild_runtimes()
            assert restarted.runtimes["account-a"].status == "available"
        finally:
            await restarted.close()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_blocked_account_does_not_poll_stale_usage_session(tmp_path: Path) -> None:
    """验证失效账号关闭旧额度会话，不会被该会话缓存的成功结果重新放入池中。"""
    pool = make_account_pool(tmp_path, AuthResultClient(OAuthLoginRequired("requires login")))
    session = FakeUsageSession(usage_snapshot(50))
    pool._usage_sessions["account-a"] = session
    try:
        with pytest.raises(NoAvailableOAuthAccounts):
            await pool.create_response({"model": "test"})
        assert await pool.refresh_account("account-a") is None
        assert session.read_count == 0
        assert session.close_count == 1
        assert "account-a" not in pool._usage_sessions
        assert pool.runtimes["account-a"].status == "login_required"
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_fails", [False, True])
async def test_inflight_quota_refresh_cannot_overwrite_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage_fails: bool
) -> None:
    """验证认证失败发生在额度请求期间时，迟到的成功或失败结果都不能覆盖认证状态。"""
    pool = make_account_pool(tmp_path, AuthResultClient(OAuthLoginRequired("requires login")))
    pool._rebuild_runtimes()
    pool._usage_sessions["account-a"] = FakeUsageSession(usage_snapshot(50))
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed_usage(**_kwargs: Any) -> dict[str, Any]:
        """暂停额度结果直到模型请求已经标记认证失败。"""
        started.set()
        await finish.wait()
        if usage_fails:
            raise RuntimeError("temporary quota failure")
        return usage_snapshot(100)

    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", delayed_usage)
    task = asyncio.create_task(pool.refresh_account("account-a"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(NoAvailableOAuthAccounts):
            await pool.create_response({"model": "test"})
        finish.set()
        assert await task is None
        assert pool.runtimes["account-a"].status == "login_required"
        assert "重新登录" in pool.runtimes["account-a"].last_error
        assert "account-a" not in pool._available_keys()
    finally:
        finish.set()
        await task
        await pool.close()


@pytest.mark.asyncio
async def test_refresh_account_reuses_usage_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证同一账号连续刷新通过同一个持久 app-server 会话读取。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    record = await add_pool_account(pool)
    session = FakeUsageSession(usage_snapshot(50))
    pool._usage_sessions[record.key] = session

    async def fake_fetch_usage(
        *,
        client_version: str,
        app_server_rpc: Any = None,
        account_home: str | None = None,
    ) -> dict[str, Any]:
        """调用账号池传入的真实会话入口，隔离外部 Codex 进程。"""
        assert client_version
        assert account_home is None
        assert app_server_rpc is not None
        return await app_server_rpc(client_version, 1.0)

    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", fake_fetch_usage)

    await pool.refresh_account(record.key)
    await pool.refresh_account(record.key)

    assert session.read_count == 2
    assert pool._usage_sessions[record.key] is session
    await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining", "interval"),
    [(4, 30), (5, 60), (9, 60), (10, 300), (50, 300), (80, 300)],
)
async def test_refresh_account_keeps_existing_refresh_intervals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining: float,
    interval: int,
) -> None:
    """验证低额度高频刷新不变，50% 以上仍为每五分钟。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    record = await add_pool_account(pool, account_id=f"acct-{remaining}")
    session = FakeUsageSession(usage_snapshot(remaining))
    pool._usage_sessions[record.key] = session
    now = 1_800_000_000.0

    async def fake_fetch_usage(*, client_version: str, app_server_rpc: Any, **_kwargs: Any) -> dict[str, Any]:
        """通过当前账号会话返回指定剩余额度。"""
        return await app_server_rpc(client_version, 1.0)

    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", fake_fetch_usage)
    monkeypatch.setattr(oauth_pool_module.time, "time", lambda: now)

    await pool.refresh_account(record.key)

    assert pool.runtimes[record.key].next_refresh_at == now + interval
    await pool.close()


def test_dispatch_window_prefers_five_hours_and_falls_back_to_shortest() -> None:
    """验证调度按真实时长选择 5 小时窗口，而不是盲目选择 primary。"""
    usage = usage_snapshot(80)
    usage["rateLimit"]["windows"] = [
        {
            "kind": "primary",
            "label": "Weekly",
            "remainingPercent": 80,
            "limitWindowSeconds": 604_800,
            "resetAt": 4_102_444_800,
        },
        {
            "kind": "secondary",
            "label": "5h",
            "remainingPercent": 40,
            "limitWindowSeconds": 18_000,
            "resetAt": 4_102_000_000,
        },
    ]

    selected = OAuthAccountPool._dispatch_window(usage)

    assert selected["label"] == "5h"
    usage["rateLimit"]["windows"] = [
        usage["rateLimit"]["windows"][0],
        {
            "kind": "secondary",
            "label": "1d",
            "remainingPercent": 60,
            "limitWindowSeconds": 86_400,
            "resetAt": 4_102_100_000,
        },
    ]
    assert OAuthAccountPool._dispatch_window(usage)["label"] == "1d"


@pytest.mark.asyncio
async def test_refresh_account_uses_retry_backoff_and_keeps_last_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证额度读取失败后保留旧快照，并避免后台每五秒持续重试。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    record = await add_pool_account(pool, account_id="acct-retry")
    previous_usage = usage_snapshot(50)
    pool.runtimes[record.key].usage = previous_usage
    now = 1_800_000_000.0

    async def failing_fetch_usage(**_kwargs: Any) -> dict[str, Any]:
        """模拟 app-server 额度接口暂时不可用。"""
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", failing_fetch_usage)
    monkeypatch.setattr(oauth_pool_module.time, "time", lambda: now)

    result = await pool.refresh_account(record.key)

    assert result is None
    assert pool.runtimes[record.key].usage is previous_usage
    assert pool.runtimes[record.key].status == "available"
    assert pool.runtimes[record.key].last_error == "额度读取失败"
    assert pool.runtimes[record.key].next_refresh_at == now + 30
    await pool.close()


@pytest.mark.asyncio
async def test_snapshot_share_excludes_accounts_that_cannot_be_dispatched(tmp_path: Path) -> None:
    """验证停用或限流账号不会占用预计调度占比分母。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    available = await add_pool_account(pool, account_id="acct-available")
    disabled = await add_pool_account(pool, account_id="acct-disabled")
    limited = await add_pool_account(pool, account_id="acct-limited")
    await pool.store.update(disabled.key, enabled=False)
    pool._rebuild_runtimes()
    pool.runtimes[available.key].usage = usage_snapshot(50)
    pool.runtimes[disabled.key].usage = usage_snapshot(50)
    pool.runtimes[limited.key].usage = usage_snapshot(50)
    pool.runtimes[limited.key].status = "rate_limited"

    items = {item["key"]: item for item in pool.snapshot()["accounts"]}

    assert items[available.key]["estimatedShare"] == pytest.approx(1.0)
    assert items[disabled.key]["weight"] == 0.0
    assert items[disabled.key]["estimatedShare"] == 0.0
    assert items[limited.key]["weight"] == 0.0
    assert items[limited.key]["estimatedShare"] == 0.0
    await pool.close()


@pytest.mark.asyncio
async def test_pool_close_closes_all_usage_sessions(tmp_path: Path) -> None:
    """验证服务关闭会释放登录任务和所有账号的 app-server 会话。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    first = FakeUsageSession(usage_snapshot(50))
    second = FakeUsageSession(usage_snapshot(50))
    pool._usage_sessions = {"account-a": first, "account-b": second}
    login_close_count = 0

    async def close_login_manager() -> None:
        """记录账号池是否联动关闭 OAuth 登录管理器。"""
        nonlocal login_close_count
        login_close_count += 1

    pool.login.close = close_login_manager  # type: ignore[method-assign]

    await pool.close()

    assert login_close_count == 1
    assert first.close_count == 1
    assert second.close_count == 1
    assert pool._usage_sessions == {}


@pytest.mark.asyncio
async def test_pool_restart_preserves_oauth_credentials(tmp_path: Path) -> None:
    """验证关闭临时运行层后，新账号池仍加载原 OAuth 凭据。"""
    config = AppConfig(
        project_root=tmp_path,
        auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
    )
    first_pool = OAuthAccountPool(config=config)
    record = await add_pool_account(first_pool, account_id="acct-persist")
    auth_path = first_pool.store.auth_path(record.key)
    original_auth = auth_path.read_text(encoding="utf-8")
    session = FakeUsageSession(usage_snapshot(50))
    first_pool._usage_sessions[record.key] = session

    await first_pool.close()
    second_pool = OAuthAccountPool(config=config)

    assert session.close_count == 1
    assert second_pool.store.get(record.key) is not None
    assert auth_path.exists()
    assert auth_path.read_text(encoding="utf-8") == original_auth
    assert json.loads(original_auth)["tokens"]["refresh_token"] == "refresh-acct-persist"
    await second_pool.close()


@pytest.mark.asyncio
async def test_delete_account_closes_only_its_usage_session(tmp_path: Path) -> None:
    """验证删除账号会释放对应 app-server，不影响其他账号会话。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    first_record = await add_pool_account(pool, account_id="acct-delete")
    second_record = await add_pool_account(pool, account_id="acct-keep")
    first_session = FakeUsageSession(usage_snapshot(50))
    second_session = FakeUsageSession(usage_snapshot(50))
    pool._usage_sessions = {
        first_record.key: first_session,
        second_record.key: second_session,
    }

    await pool.delete_account(first_record.key)

    assert first_session.close_count == 1
    assert second_session.close_count == 0
    assert pool.store.get(first_record.key) is None
    assert pool.store.get(second_record.key) is not None
    assert first_record.key not in pool._usage_sessions
    assert pool._usage_sessions[second_record.key] is second_session
    await pool.close()


@pytest.mark.asyncio
async def test_delete_account_stops_app_server_before_removing_auth_home(tmp_path: Path) -> None:
    """验证删除账号前先停止仍以该目录作为 CODEX_HOME 的进程。"""
    pool = OAuthAccountPool(
        config=AppConfig(
            project_root=tmp_path,
            auth=AuthConfig(account_store_path=tmp_path / ".codex-oauth"),
        )
    )
    record = await add_pool_account(pool, account_id="acct-close-order")
    auth_path = pool.store.auth_path(record.key)

    class InspectingUsageSession(FakeUsageSession):
        """关闭时记录永久认证目录是否仍然存在。"""

        def __init__(self) -> None:
            super().__init__(usage_snapshot(50))
            self.auth_existed_during_close = False

        async def close(self) -> None:
            """在模拟进程退出前检查它依赖的认证目录。"""
            self.auth_existed_during_close = auth_path.exists()
            await super().close()

    session = InspectingUsageSession()
    pool._usage_sessions[record.key] = session

    await pool.delete_account(record.key)

    assert session.auth_existed_during_close is True
    assert not auth_path.exists()
    await pool.close()


@pytest.mark.asyncio
async def test_refresh_timeout_isolates_bad_account_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证单账号读取及关闭均阻塞时，健康账号更新、批量返回且下次可恢复。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    good = await add_pool_account(pool, account_id="acct-healthy")
    previous = usage_snapshot(4)
    pool.runtimes["account-a"].usage = previous
    pool.runtimes["account-a"].last_successful_refresh_at = 1
    healthy_updated = asyncio.Event()

    class StuckSession(FakeUsageSession):
        """模拟永久等待的旧会话，包括异常后关闭也无法完成。"""

        async def read_rate_limits(self, _version: str, _timeout: float) -> dict[str, Any]:
            """读取永不返回，必须由账号总超时取消。"""
            await asyncio.Event().wait()
            return self.usage

        async def close(self) -> None:
            """清理永不返回，必须由清理超时取消。"""
            self.close_count += 1
            await asyncio.Event().wait()

    bad_session = StuckSession(previous)
    good_session = FakeUsageSession(usage_snapshot(75))
    pool._usage_sessions.update({"account-a": bad_session, good.key: good_session})

    async def fetch_usage(*, app_server_rpc: Any, **_kwargs: Any) -> dict[str, Any]:
        """健康账号返回后标记事件，以验证没有等待坏账号。"""
        usage = await app_server_rpc("test", 0.1)
        healthy_updated.set()
        return usage

    monkeypatch.setattr(oauth_pool_module, "ACCOUNT_REFRESH_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(oauth_pool_module, "SESSION_CLEANUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(oauth_pool_module, "_codex_client_version", lambda: "test")
    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", fetch_usage)
    try:
        batch = asyncio.create_task(pool.refresh_enabled_accounts())
        await asyncio.wait_for(healthy_updated.wait(), timeout=0.2)
        assert not batch.done()
        snapshot = await asyncio.wait_for(batch, timeout=0.3)
        accounts = {item["key"]: item for item in snapshot["accounts"]}
        assert pool.runtimes["account-a"].usage is previous
        assert accounts["account-a"]["usageStale"]
        assert "超时" in accounts["account-a"]["lastError"]
        assert accounts[good.key]["lastSuccessfulRefreshAt"] > 0
        assert not accounts[good.key]["usageStale"]
        assert "account-a" not in pool._usage_sessions
        assert bad_session.close_count == 1

        recovered = FakeUsageSession(usage_snapshot(98))
        monkeypatch.setattr(oauth_pool_module, "CodexUsageSession", lambda **_kwargs: recovered)
        await pool.refresh_account("account-a")
        account = next(item for item in pool.snapshot()["accounts"] if item["key"] == "account-a")
        assert account["usage"]["rateLimit"]["windows"][0]["remainingPercent"] == 98
        assert account["lastError"] is None
        assert not account["usageStale"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_refresh_shares_task_and_survives_caller_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证同时刷新只读一次，某个 HTTP 调用方退出不会取消共享刷新。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def delayed_fetch(**_kwargs: Any) -> dict[str, Any]:
        """等待测试释放后再返回实际额度。"""
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return usage_snapshot(98)

    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", delayed_fetch)
    monkeypatch.setattr(oauth_pool_module, "_codex_client_version", lambda: "test")
    first = asyncio.create_task(pool.refresh_account("account-a"))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        shared = pool._refresh_tasks["account-a"]
        assert pool._start_account_refresh("account-a") is shared
        assert pool.snapshot()["accounts"][0]["usageRefreshing"]
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert not shared.cancelled()
        finish.set()
        assert await pool.refresh_account("account-a") == usage_snapshot(98)
        assert calls == 1
    finally:
        finish.set()
        await pool.close()


@pytest.mark.asyncio
async def test_background_survives_sync_error_and_refreshes_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """验证全局同步抛错后循环仍继续，慢账号不挡住健康账号的后续调度。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    good = await add_pool_account(pool, account_id="acct-independent")
    healthy_done, second_sync = asyncio.Event(), asyncio.Event()
    sync_calls = 0

    async def failing_sync(**_kwargs: Any) -> None:
        """首轮模拟同步失败，后续记录循环仍在运行。"""
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise RuntimeError("private upstream content")
        second_sync.set()

    async def fetch_usage(*, app_server_rpc: Any, **_kwargs: Any) -> dict[str, Any]:
        """只让健康账号返回；坏账号需在停服时取消。"""
        if app_server_rpc.__self__.usage["rateLimit"]["windows"][0]["remainingPercent"] == 4:
            await asyncio.Event().wait()
        healthy_done.set()
        return usage_snapshot(75)

    pool._usage_sessions["account-a"] = FakeUsageSession(usage_snapshot(4))
    pool._usage_sessions[good.key] = FakeUsageSession(usage_snapshot(75))
    monkeypatch.setattr(pool.sync, "sync_once", failing_sync)
    monkeypatch.setattr(oauth_pool_module, "BACKGROUND_POLL_SECONDS", 0.01)
    monkeypatch.setattr(oauth_pool_module, "fetch_codex_usage_snapshot", fetch_usage)
    monkeypatch.setattr(oauth_pool_module, "_codex_client_version", lambda: "test")
    pool._background_task = asyncio.create_task(pool._background_loop())
    try:
        await asyncio.wait_for(healthy_done.wait(), timeout=0.3)
        await asyncio.wait_for(second_sync.wait(), timeout=0.3)
        assert not pool._background_task.done()
        assert not pool._refresh_tasks["account-a"].done()
        assert pool.runtimes[good.key].last_successful_refresh_at > 0
        assert "private upstream content" not in caplog.text
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_snapshot_marks_old_successful_usage_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证后台停滞时，旧快照会明确过期而不是永远看起来正常。"""
    pool = make_account_pool(tmp_path, SuccessfulAccountClient())
    runtime = pool.runtimes["account-a"]
    pool._rebuild_runtimes()
    runtime.usage = usage_snapshot(4)
    runtime.last_successful_refresh_at = 100
    runtime.next_refresh_at = 400
    monkeypatch.setattr(oauth_pool_module.time, "time", lambda: 431)
    try:
        item = pool.snapshot()["accounts"][0]
        assert item["usageStale"]
        assert item["lastSuccessfulRefreshAt"] == 100
    finally:
        await pool.close()
