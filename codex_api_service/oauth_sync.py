"""同步全局 Codex CLI/App OAuth 文件到项目账号库。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .auth import _parse_auth_file
from .oauth_accounts import OAuthAccountRecord, OAuthAccountStore


class OAuthSyncService:
    """按文件真实账号身份执行幂等 OAuth 同步。"""

    def __init__(self, *, store: OAuthAccountStore, import_path: Path) -> None:
        self.store = store
        self.import_path = import_path
        self._lock = asyncio.Lock()
        self._signature: tuple[int, int] | None = None

    async def sync_once(self, *, force: bool = False) -> OAuthAccountRecord | None:
        """文件变化或强制请求时导入当前全局 Codex 登录。"""
        async with self._lock:
            signature = self._file_signature()
            if not force and signature == self._signature:
                return None
            self._signature = signature
            if _parse_auth_file(self.import_path) is None:
                return None
            return await self.store.import_auth_file(self.import_path, source="codex-cli")

    async def watch(self, *, stop: asyncio.Event, interval_seconds: float = 5) -> None:
        """轮询轻量文件签名，检测 CLI 登录或账号切换。"""
        while not stop.is_set():
            await self.sync_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _file_signature(self) -> tuple[int, int] | None:
        """读取文件修改时间与大小，文件缺失时返回空。"""
        try:
            stat = self.import_path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size
