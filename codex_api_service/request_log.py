"""记录本地 API 请求元数据，供控制台日志页查看。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .usage_log import extract_usage


@dataclass(frozen=True)
class RequestLogEntry:
    """单条 API 请求日志，禁止保存 prompt、response 正文或密钥。"""

    id: str
    timestamp: str
    method: str
    path: str
    model: str | None
    status_code: int
    duration_ms: int
    usage: dict[str, int] | None = None
    request_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换成 JSON 响应可序列化的 dict。"""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "usage": self.usage,
            "request_id": self.request_id,
            "error": self.error,
        }


class RequestLogStore:
    """内存环形请求日志，仅用于本地控制台观察最近请求。"""

    def __init__(self, *, max_entries: int = 200) -> None:
        """初始化固定容量日志队列。"""
        self._items: deque[RequestLogEntry] = deque(maxlen=max_entries)

    def record(
        self,
        *,
        method: str,
        path: str,
        model: str | None,
        status_code: int,
        duration_ms: int,
        usage: dict[str, Any] | None = None,
        request_id: str | None = None,
        error: str | None = None,
    ) -> RequestLogEntry:
        """追加一条请求元数据日志。"""
        # usage 统一映射到 codex-usage 的字段，方便前端直接显示。
        normalized_usage = extract_usage(usage)
        entry = RequestLogEntry(
            id=f"req_{uuid4().hex}",
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            method=method,
            path=path,
            model=model,
            status_code=status_code,
            duration_ms=max(0, int(duration_ms)),
            usage=normalized_usage,
            request_id=request_id,
            error=error,
        )
        self._items.appendleft(entry)
        return entry

    def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """按时间倒序返回最近请求。"""
        # limit 防止管理接口一次返回过多数据。
        safe_limit = max(1, min(int(limit), 500))
        return [item.to_dict() for item in list(self._items)[:safe_limit]]
