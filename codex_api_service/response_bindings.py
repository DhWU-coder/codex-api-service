"""持久化 Responses API 响应链与 OAuth 账号绑定。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Callable


class ResponseBindingStore:
    """使用 response ID 哈希保存可跨重启恢复的账号绑定。"""

    def __init__(
        self,
        path: Path | str,
        *,
        ttl_seconds: int = 30 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS response_bindings (
                response_hash TEXT PRIMARY KEY,
                account_key TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def bind(self, response_id: str, account_key: str) -> None:
        """新增或更新响应链绑定。"""
        self._connection.execute(
            "INSERT OR REPLACE INTO response_bindings(response_hash, account_key, expires_at) VALUES (?, ?, ?)",
            (self._hash(response_id), account_key, self.clock() + self.ttl_seconds),
        )
        self._connection.commit()

    def lookup(self, response_id: str) -> str | None:
        """读取未过期绑定，过期数据会立即清理。"""
        key = self._hash(response_id)
        row = self._connection.execute(
            "SELECT account_key, expires_at FROM response_bindings WHERE response_hash = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if float(row[1]) <= self.clock():
            self._connection.execute("DELETE FROM response_bindings WHERE response_hash = ?", (key,))
            self._connection.commit()
            return None
        return str(row[0])

    def cleanup(self) -> int:
        """删除全部过期绑定并返回删除数量。"""
        cursor = self._connection.execute(
            "DELETE FROM response_bindings WHERE expires_at <= ?",
            (self.clock(),),
        )
        self._connection.commit()
        return cursor.rowcount

    def close(self) -> None:
        """关闭本地 SQLite 连接。"""
        self._connection.close()

    @staticmethod
    def _hash(response_id: str) -> str:
        """生成不可逆的响应 ID 索引。"""
        return hashlib.sha256(response_id.encode("utf-8")).hexdigest()
