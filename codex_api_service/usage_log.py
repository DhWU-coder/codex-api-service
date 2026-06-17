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

# 早期版本误把认证方式写入来源字段；迁移时只替换这组旧默认值。
LEGACY_SOURCE = "codex-oauth"
LEGACY_CHANNEL = "Codex OAuth"


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
        # 初始化时顺手修正旧 JSONL 元数据，让历史用量也能按项目正确分组。
        if self.usage_config.enabled:
            self._migrate_existing_log_metadata()

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
            "provider": self.usage_config.provider,
            "auth": self.usage_config.auth,
            "api_surface": self.usage_config.api_surface,
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

    def _migrate_existing_log_metadata(self) -> None:
        """把旧日志里的来源字段迁移到 codex-usage 新语义。"""
        path = self.usage_config.path
        # 首次启动时日志文件可能不存在，此时没有历史需要迁移。
        if not path.exists() or not path.is_file():
            return

        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            # 日志迁移失败不应该阻止 API 服务启动，后续写入仍会走正常错误路径。
            return

        migrated_lines: list[str] = []
        changed = False
        for line in raw_lines:
            migrated_line, line_changed = self._migrate_log_line(line)
            migrated_lines.append(migrated_line)
            changed = changed or line_changed

        if not changed:
            return

        # 先写临时文件再 replace，避免迁移中断时留下半截 JSONL。
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text("\n".join(migrated_lines) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def _migrate_log_line(self, line: str) -> tuple[str, bool]:
        """迁移单行 JSONL；无法解析的行原样保留。"""
        if not line.strip():
            return line, False
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 保留异常行，避免迁移器因为一条坏数据丢弃历史文件。
            return line, False
        if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION:
            return line, False

        changed = self._apply_metadata_migration(event)
        if not changed:
            return line, False
        return json.dumps(event, ensure_ascii=False, separators=(",", ":")), True

    def _apply_metadata_migration(self, event: dict[str, Any]) -> bool:
        """就地补齐或修正 codex-usage 元数据，返回是否发生变化。"""
        changed = False

        # 只把旧默认来源迁移成当前项目来源，避免覆盖用户自定义 source。
        if _blank_or_legacy(event.get("source"), LEGACY_SOURCE):
            event["source"] = self.usage_config.source
            changed = True
        if _blank_or_legacy(event.get("channel"), LEGACY_CHANNEL):
            event["channel"] = self.usage_config.channel
            changed = True

        # provider/auth/api_surface 是新拆出的语义字段，旧日志缺失时补齐。
        for field_name, value in (
            ("provider", self.usage_config.provider),
            ("auth", self.usage_config.auth),
            ("api_surface", self.usage_config.api_surface),
        ):
            if _blank(event.get(field_name)):
                event[field_name] = value
                changed = True

        return changed


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


def _blank(value: Any) -> bool:
    """判断日志元数据字段是否为空。"""
    return value is None or str(value).strip() == ""


def _blank_or_legacy(value: Any, legacy_value: str) -> bool:
    """判断字段是否缺失或仍是旧默认值。"""
    return _blank(value) or str(value) == legacy_value
