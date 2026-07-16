from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from codex_api_service.codex_client import CodexHTTPStatusError
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
