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
    stream: bool | None = None
    reasoning_effort: str | None = None
    fast_mode: bool | None = None
    service_tier: str | None = None
    account_key: str | None = None
    account_alias: str | None = None
    client_ip: str | None = None

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
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "fast_mode": self.fast_mode,
            "service_tier": self.service_tier,
            "account_key": self.account_key,
            "account_alias": self.account_alias,
            "client_ip": self.client_ip,
        }


class RequestLogStore:
    """内存环形请求日志，仅用于本地控制台观察最近请求。"""

    def __init__(self, *, max_entries: int = 200, path: Path | None = None, usage_path: Path | None = None) -> None:
        """初始化固定容量日志队列。"""
        # path 存在时同时做 JSONL 持久化；usage_path 用于兼容旧版只写 usage 的历史。
        self.path = path
        self.usage_path = usage_path
        self._items: deque[RequestLogEntry] = deque(maxlen=max_entries)
        self._history_cache: list[RequestLogEntry] | None = None
        self._history_signature: tuple[tuple[int, int] | None, ...] | None = None
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
        stream: bool | None = None,
        reasoning_effort: str | None = None,
        fast_mode: bool | None = None,
        service_tier: str | None = None,
        account_key: str | None = None,
        account_alias: str | None = None,
        client_ip: str | None = None,
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
            stream=stream,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
            service_tier=service_tier,
            account_key=account_key,
            account_alias=account_alias,
            client_ip=client_ip,
        )
        self._items.appendleft(entry)
        self._append_persisted_entry(entry)
        # 新记录已经落盘，下一次读取历史时需要重建缓存。
        self._history_cache = None
        return entry

    def list_recent(self, *, limit: int | str = 100) -> list[dict[str, Any]]:
        """按时间倒序返回最近请求。"""
        # limit=all 用于本地看板全量统计，数据来自持久化 JSONL。
        if isinstance(limit, str) and limit.lower() == "all":
            return [item.to_dict() for item in self._all_entries()]

        # 数字限制覆盖日志页预设档位；更大的历史读取使用显式 all。
        try:
            safe_limit = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            safe_limit = 100
        memory_items = list(self._items)
        if safe_limit <= len(memory_items):
            return [item.to_dict() for item in memory_items[:safe_limit]]
        return [item.to_dict() for item in self._all_entries()[:safe_limit]]

    def _load_existing_items(self) -> None:
        """从 JSONL 文件加载历史请求元数据。"""
        loaded = self._all_entries()

        if not loaded:
            return

        # 历史缓存按新到旧；反向插入 deque 后仍保持新记录在最前。
        for entry in reversed(loaded[: self._items.maxlen]):
            self._items.appendleft(entry)

    def _all_entries(self) -> list[RequestLogEntry]:
        """读取全部持久化请求日志，并按时间倒序返回。"""
        signature = self._persisted_signature()
        if self._history_cache is not None and signature == self._history_signature:
            return self._history_cache

        loaded = self._read_persisted_entries()
        if not loaded:
            return list(self._items)
        self._history_cache = sorted(loaded, key=lambda entry: entry.timestamp, reverse=True)
        self._history_signature = signature
        return self._history_cache

    def _persisted_signature(self) -> tuple[tuple[int, int] | None, ...]:
        """用文件大小和纳秒修改时间判断历史缓存是否仍有效。"""

        def file_signature(path: Path | None) -> tuple[int, int] | None:
            if path is None or not path.exists():
                return None
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns

        return file_signature(self.path), file_signature(self.usage_path)

    def _read_persisted_entries(self) -> list[RequestLogEntry]:
        """从新版请求日志和旧版 usage 日志读取全部可用历史。"""
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
        return loaded

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
            stream=item["stream"] if isinstance(item.get("stream"), bool) else None,
            reasoning_effort=(
                item["reasoning_effort"] if isinstance(item.get("reasoning_effort"), str) else None
            ),
            fast_mode=item["fast_mode"] if isinstance(item.get("fast_mode"), bool) else None,
            service_tier=item["service_tier"] if isinstance(item.get("service_tier"), str) else None,
            account_key=item["account_key"] if isinstance(item.get("account_key"), str) else None,
            account_alias=item["account_alias"] if isinstance(item.get("account_alias"), str) else None,
            client_ip=item["client_ip"] if isinstance(item.get("client_ip"), str) else None,
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
