import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from codex_api_service.codex_client import CodexHTTPStatusError
import codex_api_service.oauth_pool as oauth_pool_module
from codex_api_service.auth import CodexCredentials
from codex_api_service.config import AppConfig, AuthConfig
from codex_api_service.oauth_accounts import OAuthAccountRecord
from codex_api_service.oauth_pool import AccountRuntime, OAuthAccountPool


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


def usage_snapshot(remaining: float) -> dict[str, Any]:
    """构造账号池刷新逻辑需要的稳定额度摘要。"""
    return {
        "planType": "pro",
        "rateLimit": {
            "allowed": True,
            "windows": [
                {
                    "kind": "primary",
                    "remainingPercent": remaining,
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
