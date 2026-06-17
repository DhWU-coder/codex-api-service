"""记录本地 API 请求元数据，供控制台日志页查看。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
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

    def __init__(self, *, max_entries: int = 200, path: Path | None = None, usage_path: Path | None = None) -> None:
        """初始化固定容量日志队列。"""
        # path 存在时同时做 JSONL 持久化；usage_path 用于兼容旧版只写 usage 的历史。
        self.path = path
        self.usage_path = usage_path
        self._items: deque[RequestLogEntry] = deque(maxlen=max_entries)
        self._load_existing_items()

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
        self._append_persisted_entry(entry)
        return entry

    def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """按时间倒序返回最近请求。"""
        # limit 防止管理接口一次返回过多数据。
        safe_limit = max(1, min(int(limit), 500))
        return [item.to_dict() for item in list(self._items)[:safe_limit]]

    def _load_existing_items(self) -> None:
        """从 JSONL 文件加载历史请求元数据。"""
        loaded: list[RequestLogEntry] = []

        # 新版请求日志优先加载，里面包含接口、状态和耗时等更完整的元数据。
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = _entry_from_dict(item)
                if entry is not None:
                    loaded.append(entry)

        # 旧版只写 codex-usage 日志；看板统计仍应能回收这些 token 历史。
        existing_request_ids = {entry.request_id for entry in loaded if entry.request_id}
        loaded.extend(_entries_from_usage_log(self.usage_path, existing_request_ids=existing_request_ids))

        if not loaded:
            return

        # 按时间排序后只保留最近 maxlen 条，避免不同来源加载顺序影响展示。
        loaded.sort(key=lambda entry: entry.timestamp)
        # 文件按旧到新追加，内存按新到旧展示。
        for entry in loaded[-self._items.maxlen :]:
            self._items.appendleft(entry)

    def _append_persisted_entry(self, entry: RequestLogEntry) -> None:
        """把单条请求元数据追加写入 JSONL。"""
        # 持久化内容仍然只包含元数据，不包含 prompt、completion 或密钥。
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")


def _entry_from_dict(item: dict[str, Any]) -> RequestLogEntry | None:
    """把持久化 JSON object 恢复成 RequestLogEntry。"""
    # 历史文件可能被手动编辑或损坏，字段不完整时跳过该行。
    try:
        return RequestLogEntry(
            id=str(item["id"]),
            timestamp=str(item["timestamp"]),
            method=str(item["method"]),
            path=str(item["path"]),
            model=item["model"] if isinstance(item.get("model"), str) else None,
            status_code=int(item["status_code"]),
            duration_ms=int(item["duration_ms"]),
            usage=item["usage"] if isinstance(item.get("usage"), dict) else None,
            request_id=item["request_id"] if isinstance(item.get("request_id"), str) else None,
            error=item["error"] if isinstance(item.get("error"), str) else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _entries_from_usage_log(usage_path: Path | None, *, existing_request_ids: set[str]) -> list[RequestLogEntry]:
    """把旧版 codex-usage JSONL 转换成看板可用的请求元数据。"""
    # 没有 usage 文件时不做兼容加载，保持纯请求日志行为。
    if usage_path is None or not usage_path.exists():
        return []

    entries: list[RequestLogEntry] = []
    for index, line in enumerate(usage_path.read_text(encoding="utf-8").splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry = _entry_from_usage_event(item, index=index, existing_request_ids=existing_request_ids)
        if entry is not None:
            entries.append(entry)
    return entries


def _entry_from_usage_event(
    item: dict[str, Any],
    *,
    index: int,
    existing_request_ids: set[str],
) -> RequestLogEntry | None:
    """把单条 codex-usage 事件恢复成只读历史记录。"""
    # usage 历史没有原始接口和耗时，使用明确的 synthetic path 避免误导为实时请求。
    try:
        usage = extract_usage(item.get("usage"))
        if usage is None:
            return None
        request_id = item.get("request_id") if isinstance(item.get("request_id"), str) else None
        if request_id is not None and request_id in existing_request_ids:
            return None
        entry_id = f"req_usage_{request_id}" if request_id else f"req_usage_{index}"
        return RequestLogEntry(
            id=entry_id,
            timestamp=str(item["timestamp"]),
            method="POST",
            path="/usage/history",
            model=item["model"] if isinstance(item.get("model"), str) else None,
            status_code=200,
            duration_ms=0,
            usage=usage,
            request_id=request_id,
            error=None,
        )
    except (KeyError, TypeError, ValueError):
        return None
