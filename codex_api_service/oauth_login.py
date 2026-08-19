"""管理 Web 发起的隔离 Codex OAuth 登录会话。"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import _parse_auth_file
from .oauth_accounts import OAuthAccountStore


@dataclass
class OAuthLoginSession:
    """保存单次登录的非敏感可观察状态。"""

    id: str
    device_auth: bool
    status: str = "waiting"
    message: str = "等待浏览器完成登录"
    output: list[str] = field(default_factory=list)
    account_key: str | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any]:
        """生成可安全返回前端的登录状态。"""
        return {
            "id": self.id,
            "deviceAuth": self.device_auth,
            "status": self.status,
            "message": self.message,
            "output": self.output[-20:],
            "accountKey": self.account_key,
        }


class OAuthLoginManager:
    """使用临时 CODEX_HOME 启动 Codex CLI 登录并归档账号。"""

    completed_session_retention_seconds = 600.0
    login_timeout_seconds = 600.0

    def __init__(self, *, store: OAuthAccountStore) -> None:
        self.store = store
        self.pending_root = store.root / "pending"
        self.pending_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.pending_root, 0o700)
        self._remove_stale_pending_content()
        self.sessions: dict[str, OAuthLoginSession] = {}
        self._active_id: str | None = None
        self._lock = asyncio.Lock()
        self._retention_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, *, device_auth: bool = False) -> dict[str, Any]:
        """启动登录；已有活动会话时直接返回，供新页面接管。"""
        async with self._lock:
            if self._active_id:
                active = self.sessions.get(self._active_id)
                if active and active.status == "waiting":
                    return active.snapshot()
                self._active_id = None
            session = OAuthLoginSession(id=uuid.uuid4().hex, device_auth=device_auth)
            if device_auth:
                session.message = "等待设备码登录"
            self.sessions[session.id] = session
            self._active_id = session.id
            session.task = asyncio.create_task(self._run(session))
            return session.snapshot()

    def active(self) -> dict[str, Any] | None:
        """读取当前仍在等待的登录会话，供页面刷新后恢复轮询。"""
        if self._active_id is None:
            return None
        session = self.sessions.get(self._active_id)
        if session is None or session.status != "waiting":
            return None
        return session.snapshot()

    def status(self, session_id: str) -> dict[str, Any] | None:
        """读取登录会话状态。"""
        session = self.sessions.get(session_id)
        return session.snapshot() if session else None

    async def cancel(self, session_id: str) -> dict[str, Any] | None:
        """取消活动登录并清理临时认证目录。"""
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if session.status != "waiting":
            return session.snapshot()
        session.status = "cancelled"
        session.message = "登录已取消"
        await self._terminate_process(session.process)
        if session.task and not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        session.status = "cancelled"
        session.message = "登录已取消"
        shutil.rmtree(self.pending_root / session.id, ignore_errors=True)
        await self._finish(session)
        return session.snapshot()

    async def close(self) -> None:
        """服务关闭时终止活动登录，并停止会话保留任务。"""
        waiting_ids = [session.id for session in self.sessions.values() if session.status == "waiting"]
        if waiting_ids:
            await asyncio.gather(*(self.cancel(session_id) for session_id in waiting_ids))
        retention_tasks = list(self._retention_tasks.values())
        for task in retention_tasks:
            task.cancel()
        if retention_tasks:
            await asyncio.gather(*retention_tasks, return_exceptions=True)
        self._retention_tasks.clear()

    async def _run(self, session: OAuthLoginSession) -> None:
        """执行 CLI 登录并把成功凭据导入正式账号目录。"""
        pending_home = self.pending_root / session.id
        pending_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(pending_home)
        command = ["codex", "login"]
        if session.device_auth:
            command.append("--device-auth")
        readers: list[asyncio.Task[None]] = []
        try:
            session.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            readers = [
                asyncio.create_task(self._read_output(session, session.process.stdout)),
                asyncio.create_task(self._read_output(session, session.process.stderr)),
            ]
            try:
                return_code = await asyncio.wait_for(
                    session.process.wait(),
                    timeout=self.login_timeout_seconds,
                )
            except TimeoutError:
                await self._terminate_process(session.process)
                await asyncio.gather(*readers, return_exceptions=True)
                session.status = "failed"
                session.message = "OAuth 登录超时，请重试"
                return
            await asyncio.gather(*readers, return_exceptions=True)
            if return_code != 0:
                raise RuntimeError("Codex OAuth 登录失败")
            credentials = _parse_auth_file(pending_home / "auth.json")
            if credentials is None:
                raise RuntimeError("登录完成但未找到有效 OAuth 凭据")
            record = await self.store.import_auth_file(pending_home / "auth.json", source="web-oauth")
            session.account_key = record.key
            session.status = "success"
            session.message = "OAuth 账号添加成功"
        except asyncio.CancelledError:
            await self._terminate_process(session.process)
            session.status = "cancelled"
            session.message = "登录已取消"
        except Exception:
            session.status = "failed"
            session.message = "OAuth 登录失败，请重试"
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            shutil.rmtree(pending_home, ignore_errors=True)
            await self._finish(session)

    async def _finish(self, session: OAuthLoginSession) -> None:
        """释放全局活动登录标记，并延迟清理完成会话。"""
        async with self._lock:
            if self._active_id == session.id:
                self._active_id = None
            if session.status != "waiting" and session.id not in self._retention_tasks:
                self._retention_tasks[session.id] = asyncio.create_task(
                    self._remove_after_retention(session.id)
                )

    async def _remove_after_retention(self, session_id: str) -> None:
        """短暂保留完成态供最后一次轮询读取，随后释放内存。"""
        try:
            await asyncio.sleep(self.completed_session_retention_seconds)
            self.sessions.pop(session_id, None)
        finally:
            self._retention_tasks.pop(session_id, None)

    def _remove_stale_pending_content(self) -> None:
        """启动时删除上次异常退出遗留的临时登录内容。"""
        for child in self.pending_root.iterdir():
            try:
                if child.is_symlink() or not child.is_dir():
                    child.unlink(missing_ok=True)
                else:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                # 单个异常遗留项不应阻止服务加载已有账号。
                continue

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
        """可靠终止仍在等待的 Codex 登录子进程。"""
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _read_output(
        session: OAuthLoginSession,
        stream: asyncio.StreamReader | None,
    ) -> None:
        """逐行读取登录提示，供设备码页面实时展示。"""
        if stream is None:
            return
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                session.output.append(text[:500])
