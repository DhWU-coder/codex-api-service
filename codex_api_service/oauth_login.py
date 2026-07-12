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

    def __init__(self, *, store: OAuthAccountStore) -> None:
        self.store = store
        self.pending_root = store.root / "pending"
        self.pending_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.pending_root, 0o700)
        self.sessions: dict[str, OAuthLoginSession] = {}
        self._active_id: str | None = None
        self._lock = asyncio.Lock()

    async def start(self, *, device_auth: bool = False) -> dict[str, Any]:
        """启动一个登录会话；同一时间只允许一个活动会话。"""
        async with self._lock:
            if self._active_id:
                active = self.sessions.get(self._active_id)
                if active and active.status == "waiting":
                    raise RuntimeError("已有 OAuth 登录正在进行")
            session = OAuthLoginSession(id=uuid.uuid4().hex, device_auth=device_auth)
            if device_auth:
                session.message = "等待设备码登录"
            self.sessions[session.id] = session
            self._active_id = session.id
            session.task = asyncio.create_task(self._run(session))
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
        if session.process and session.process.returncode is None:
            session.process.terminate()
        if session.task and not session.task.done():
            session.task.cancel()
        session.status = "cancelled"
        session.message = "登录已取消"
        await self._finish(session)
        return session.snapshot()

    async def _run(self, session: OAuthLoginSession) -> None:
        """执行 CLI 登录并把成功凭据导入正式账号目录。"""
        pending_home = self.pending_root / session.id
        pending_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(pending_home)
        command = ["codex", "login"]
        if session.device_auth:
            command.append("--device-auth")
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
            return_code = await session.process.wait()
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
            session.status = "cancelled"
            session.message = "登录已取消"
        except Exception:
            session.status = "failed"
            session.message = "OAuth 登录失败，请重试"
        finally:
            shutil.rmtree(pending_home, ignore_errors=True)
            await self._finish(session)

    async def _finish(self, session: OAuthLoginSession) -> None:
        """释放全局活动登录标记，并延迟清理完成会话。"""
        async with self._lock:
            if self._active_id == session.id:
                self._active_id = None
        if session.status != "waiting":
            asyncio.create_task(self._remove_after_retention(session.id))

    async def _remove_after_retention(self, session_id: str) -> None:
        """短暂保留完成态供最后一次轮询读取，随后释放内存。"""
        await asyncio.sleep(self.completed_session_retention_seconds)
        self.sessions.pop(session_id, None)

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
