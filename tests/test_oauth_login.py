import asyncio
from pathlib import Path
from typing import Any

import pytest

from codex_api_service.oauth_accounts import OAuthAccountStore
from codex_api_service.oauth_login import OAuthLoginManager, OAuthLoginSession


class WaitingProcess:
    """模拟一直等待、直到管理器主动终止的 Codex 登录进程。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        """等待测试触发终止并返回退出码。"""
        await self._finished.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        """记录终止动作并唤醒等待者。"""
        self.terminated = True
        self.returncode = -15
        self._finished.set()


class EmptyStream:
    """提供立即结束的异步输出流。"""

    async def readline(self) -> bytes:
        """返回 EOF，避免测试生成无关 CLI 输出。"""
        return b""


@pytest.mark.asyncio
async def test_start_reuses_existing_waiting_session(tmp_path: Path) -> None:
    """验证重复点击登录会接管当前会话，不再返回冲突。"""
    manager = OAuthLoginManager(store=OAuthAccountStore(tmp_path / ".codex-oauth"))
    session = OAuthLoginSession(id="active", device_auth=False)
    manager.sessions[session.id] = session
    manager._active_id = session.id

    result = await manager.start(device_auth=True)

    assert result["id"] == "active"
    assert result["deviceAuth"] is False
    assert list(manager.sessions) == ["active"]


def test_active_returns_only_waiting_session(tmp_path: Path) -> None:
    """验证前端可以恢复活动会话，结束态不会被误报为活动。"""
    manager = OAuthLoginManager(store=OAuthAccountStore(tmp_path / ".codex-oauth"))
    session = OAuthLoginSession(id="active", device_auth=False)
    manager.sessions[session.id] = session
    manager._active_id = session.id

    assert manager.active() == session.snapshot()

    session.status = "success"

    assert manager.active() is None


def test_manager_startup_removes_only_stale_pending_content(tmp_path: Path) -> None:
    """验证服务重启清理临时登录目录，但保留正式账号凭据。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    stale_dir = store.root / "pending" / "stale-session"
    stale_dir.mkdir(parents=True)
    (stale_dir / "auth.json").write_text("stale", encoding="utf-8")
    account_dir = store.root / "accounts" / "saved-account"
    account_dir.mkdir(parents=True)
    saved_auth = account_dir / "auth.json"
    saved_auth.write_text("saved", encoding="utf-8")

    OAuthLoginManager(store=store)

    assert not stale_dir.exists()
    assert saved_auth.read_text(encoding="utf-8") == "saved"


@pytest.mark.asyncio
async def test_cancel_waits_for_login_task_to_finish(tmp_path: Path) -> None:
    """验证取消返回前后台任务已经退出，避免残留活动登录。"""
    manager = OAuthLoginManager(store=OAuthAccountStore(tmp_path / ".codex-oauth"))
    cancelled = asyncio.Event()

    async def wait_forever() -> None:
        """模拟需要接收取消信号才能退出的登录任务。"""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    session = OAuthLoginSession(id="active", device_auth=False)
    session.task = asyncio.create_task(wait_forever())
    manager.sessions[session.id] = session
    manager._active_id = session.id
    await asyncio.sleep(0)

    result = await manager.cancel(session.id)

    assert result is not None
    assert result["status"] == "cancelled"
    assert cancelled.is_set()
    assert session.task.done()
    assert manager.active() is None
    await manager.close()


@pytest.mark.asyncio
async def test_login_timeout_terminates_process_and_cleans_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证无人完成登录时后端会超时终止进程并清理临时目录。"""
    manager = OAuthLoginManager(store=OAuthAccountStore(tmp_path / ".codex-oauth"))
    manager.login_timeout_seconds = 0.01
    manager.completed_session_retention_seconds = 0
    process = WaitingProcess()
    process.stdout = EmptyStream()  # type: ignore[attr-defined]
    process.stderr = EmptyStream()  # type: ignore[attr-defined]

    async def create_process(*_args: Any, **_kwargs: Any) -> WaitingProcess:
        """返回可观测的等待进程。"""
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    snapshot = await manager.start()
    session = manager.sessions[snapshot["id"]]
    assert session.task is not None
    await asyncio.wait_for(session.task, timeout=0.2)

    assert process.terminated is True
    assert session.status == "failed"
    assert session.message == "OAuth 登录超时，请重试"
    assert not (manager.pending_root / session.id).exists()
    assert manager.active() is None


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
