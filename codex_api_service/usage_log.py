"""写入 codex-usage 可导入的项目级用量日志。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import UsageConfig

# codex-usage 规范要求的固定 schema 版本。
SCHEMA_VERSION = "codex-usage.project-log.v1"


def extract_usage(raw: dict[str, Any] | None) -> dict[str, int] | None:
    """从 OpenAI/Responses 风格 usage 字段中提取真实 token 用量。"""
    # None 或非 dict 无法提供真实 usage，调用方必须跳过日志。
    if not isinstance(raw, dict):
        return None

    # 调用方可能传入完整响应，也可能直接传入 usage 子对象。
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
    if not isinstance(usage, dict):
        return None

    # 只接受服务端返回的明确数字字段，不做文本估算。
    total = _int_field(usage, "total_tokens", "total")
    input_tokens = _int_field(usage, "input_tokens", "prompt_tokens", "input")
    output_tokens = _int_field(usage, "output_tokens", "completion_tokens", "output")
    if total is None or input_tokens is None or output_tokens is None:
        return None

    # cached 和 reasoning 是可选明细，没有真实值时按规范写 0。
    cached = _int_field(usage, "cached_input_tokens", "cached")
    if cached is None:
        cached = _nested_int_field(usage, "input_tokens_details", "cached_tokens")
    if cached is None:
        cached = _nested_int_field(usage, "prompt_tokens_details", "cached_tokens")

    reasoning = _int_field(usage, "reasoning_output_tokens", "reasoning")
    if reasoning is None:
        reasoning = _nested_int_field(usage, "output_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _nested_int_field(usage, "completion_tokens_details", "reasoning_tokens")

    return {
        "total": total,
        "input": input_tokens,
        "cached": cached or 0,
        "output": output_tokens,
        "reasoning": reasoning or 0,
    }


class UsageLogger:
    """负责把真实 token usage 追加到 JSONL 文件。"""

    def __init__(
        self,
        *,
        project_root: Path,
        usage_config: UsageConfig,
        session_id: str | None = None,
    ) -> None:
        """初始化日志器并生成进程级稳定 session_id。"""
        self.project_root = project_root.resolve()
        self.usage_config = usage_config
        self.session_id = session_id or f"run-{uuid.uuid4().hex}"

    def log(
        self,
        *,
        model: str,
        usage: dict[str, Any] | None,
        request_id: str | None = None,
        cwd: Path | None = None,
    ) -> bool:
        """在 usage 有真实 token 数时追加一行 JSONL。"""
        # 配置关闭或 usage 不完整时直接跳过，严格遵守不估算规则。
        if not self.usage_config.enabled:
            return False
        normalized_usage = extract_usage(usage)
        if normalized_usage is None:
            return False

        # 每行只记录统计元数据，不记录 prompt、completion、token 或响应正文。
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source": self.usage_config.source,
            "channel": self.usage_config.channel,
            "project_root": str(self.project_root),
            "cwd": str((cwd or Path.cwd()).resolve()),
            "session_id": self.session_id,
            "model": model,
            "usage": normalized_usage,
        }
        if request_id:
            event["request_id"] = request_id

        # append 模式天然适合并发追加场景，避免读出整个日志再重写。
        self.usage_config.path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_config.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True


def _int_field(data: dict[str, Any], *names: str) -> int | None:
    """按候选字段名读取整数 token 字段。"""
    for name in names:
        value = data.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _nested_int_field(data: dict[str, Any], object_name: str, field_name: str) -> int | None:
    """从嵌套详情对象中读取整数 token 字段。"""
    nested = data.get(object_name)
    if not isinstance(nested, dict):
        return None
    value = nested.get(field_name)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None
