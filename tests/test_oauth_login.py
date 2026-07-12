import asyncio

import pytest

from codex_api_service.oauth_accounts import OAuthAccountStore
from codex_api_service.oauth_login import OAuthLoginManager, OAuthLoginSession


@pytest.mark.asyncio
async def test_completed_login_session_is_removed_after_retention(tmp_path) -> None:
    """验证已结束登录会话不会永久驻留在管理器内存中。"""
    manager = OAuthLoginManager(store=OAuthAccountStore(tmp_path / ".codex-oauth"))
    manager.completed_session_retention_seconds = 0.01
    session = OAuthLoginSession(id="finished", device_auth=False, status="success")
    manager.sessions[session.id] = session

    await manager._finish(session)
    await asyncio.sleep(0.03)

    assert manager.status(session.id) is None
