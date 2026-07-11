"""Codex CLI 模型目录解析、缓存和兼容响应工具。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, CodexConfig

DEFAULT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_REASONING_EFFORT = "medium"
FRESH_TTL_SECONDS = 60.0
FAILURE_COOLDOWN_SECONDS = 30.0


@dataclass(frozen=True)
class ModelCatalogEntry:
    """描述一个可用模型目录项。"""

    id: str
    display_name: str
    default_reasoning_effort: str = DEFAULT_REASONING_EFFORT
    supported_reasoning_efforts: tuple[str, ...] = DEFAULT_REASONING_EFFORTS
    visible: bool | None = None
    priority: int | float | None = None
    source: str = "cli"
    order: int = 0


@dataclass(frozen=True)
class ModelCatalogSnapshot:
    """描述一次模型目录读取结果。"""

    models: tuple[ModelCatalogEntry, ...]
    effective_default_model: str = ""
    source: str = "cli"
    cache_state: str = "fresh"
    warning: str | None = None


@dataclass(frozen=True)
class AnthropicAliasMap:
    """保存 Anthropic 公开别名和真实 Codex 模型之间的映射。"""

    public_by_real: dict[str, str]
    reverse: dict[str, str]


class CodexModelCatalog:
    """读取和缓存 Codex CLI 模型目录。"""

    def __init__(
        self,
        *,
        config_getter: Callable[[], AppConfig],
        runner: Callable[[], Awaitable[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化目录服务，runner 仅在测试中注入。"""
        self._config_getter = config_getter
        self._runner = runner or _run_codex_debug_models
        self._clock = clock
        self._cached_entries: tuple[ModelCatalogEntry, ...] | None = None
        self._cached_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error: str | None = None
        self._refresh_task: asyncio.Task[bool] | None = None
        self._refresh_lock = asyncio.Lock()

    async def snapshot(self, *, refresh: bool = False) -> ModelCatalogSnapshot:
        """返回叠加最新配置后的目录快照。"""
        now = self._clock()
        if not refresh and self._has_fresh_cache(now):
            return self._snapshot_from_cache(cache_state="fresh")
        if not refresh and self._in_failure_cooldown(now):
            return self._snapshot_after_failure()

        await self._ensure_refresh()
        if self._cached_entries is not None:
            cache_state = (
                "fresh"
                if self._last_failure_at is None or self._cached_at >= self._last_failure_at
                else "stale"
            )
            return self._snapshot_from_cache(cache_state=cache_state)
        return self._snapshot_from_config()

    def _has_fresh_cache(self, now: float) -> bool:
        """判断当前缓存是否仍可被普通请求直接使用。"""
        if self._cached_entries is None or self._cached_at is None:
            return False
        if self._last_failure_at is not None and self._last_failure_at > self._cached_at:
            return False
        return now - self._cached_at < FRESH_TTL_SECONDS

    def _in_failure_cooldown(self, now: float) -> bool:
        """判断最近失败是否仍在冷却期内。"""
        return self._last_failure_at is not None and now - self._last_failure_at < FAILURE_COOLDOWN_SECONDS

    async def _ensure_refresh(self) -> None:
        """合并并发刷新请求，保证同一时刻最多一个 CLI 调用。"""
        async with self._refresh_lock:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh())
            task = self._refresh_task
        await asyncio.shield(task)

    async def _refresh(self) -> bool:
        """执行 CLI 刷新，并记录成功或失败状态。"""
        try:
            raw_output = await self._runner()
            parsed = parse_cli_models(raw_output)
        except Exception as error:  # noqa: BLE001 - CLI 失败需要统一降级为 stale/fallback。
            self._last_failure_at = self._clock()
            self._last_error = str(error) or error.__class__.__name__
            return False
        self._cached_entries = parsed.models
        self._cached_at = self._clock()
        self._last_failure_at = None
        self._last_error = None
        return True

    def _snapshot_from_cache(self, *, cache_state: str) -> ModelCatalogSnapshot:
        """把 CLI 缓存和最新配置合成调用方可用快照。"""
        warning = self._last_error if cache_state == "stale" else None
        return _overlay_config(
            self._cached_entries or (),
            self._config_getter(),
            source="cli",
            cache_state=cache_state,
            warning=warning,
        )

    def _snapshot_after_failure(self) -> ModelCatalogSnapshot:
        """在失败冷却期内返回 stale 缓存或 config fallback。"""
        if self._cached_entries is not None:
            return self._snapshot_from_cache(cache_state="stale")
        return self._snapshot_from_config()

    def _snapshot_from_config(self) -> ModelCatalogSnapshot:
        """CLI 不可用且没有缓存时，从配置构造 fallback 快照。"""
        return _overlay_config(
            (),
            self._config_getter(),
            source="config",
            cache_state="fallback",
            warning=self._last_error,
        )


def parse_cli_models(raw_output: str) -> ModelCatalogSnapshot:
    """解析 codex debug models 输出为目录快照。"""
    data = json.loads(raw_output)
    raw_models = data.get("models", []) if isinstance(data, dict) else data
    entries: list[ModelCatalogEntry] = []
    if isinstance(raw_models, list):
        for index, item in enumerate(raw_models):
            if not isinstance(item, dict):
                continue
            model_id = _first_nonblank(item, ("slug", "id", "model"))
            if not model_id:
                continue
            display_name = _first_nonblank(item, ("display_name", "name")) or model_id
            supported_efforts = _supported_reasoning_efforts(item)
            default_effort = _default_reasoning_effort(
                item,
                supported_efforts,
                has_explicit_efforts=_has_explicit_reasoning_efforts(item),
            )
            supported_efforts = _ensure_effort_supported(supported_efforts, default_effort)
            entries.append(
                ModelCatalogEntry(
                    id=model_id,
                    display_name=display_name,
                    default_reasoning_effort=default_effort,
                    supported_reasoning_efforts=supported_efforts,
                    visible=_model_visibility(item),
                    priority=item.get("priority") if isinstance(item.get("priority"), (int, float)) else None,
                    order=index,
                )
            )
    return ModelCatalogSnapshot(
        models=tuple(entries),
        effective_default_model=entries[0].id if entries else "",
        source="cli",
        cache_state="fresh",
    )


def model_list_response(snapshot: ModelCatalogSnapshot) -> dict[str, Any]:
    """构造 OpenAI-compatible 模型发现响应。"""
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": entry.id, "object": "model", "created": created, "owned_by": "codex-oauth"}
            for entry in snapshot.models
        ],
    }


def anthropic_model_list_response(snapshot: ModelCatalogSnapshot) -> dict[str, Any]:
    """构造不泄漏真实 Codex 模型名的 Anthropic 模型发现响应。"""
    aliases = build_anthropic_aliases(snapshot)
    created_at = "2026-07-02T00:00:00Z"
    models = []
    for entry in snapshot.models:
        alias = aliases.public_by_real[entry.id]
        models.append(
            {
                "type": "model",
                "id": alias,
                "display_name": alias,
                "created_at": created_at,
                "anthropic_family_tier": "sonnet",
                "is_family_default": entry.id == snapshot.effective_default_model,
            }
        )
    first_id = models[0]["id"] if models else None
    last_id = models[-1]["id"] if models else None
    return {"data": models, "has_more": False, "first_id": first_id, "last_id": last_id}


def build_anthropic_aliases(snapshot: ModelCatalogSnapshot) -> AnthropicAliasMap:
    """为目录快照生成 Anthropic 安全别名。"""
    occupied = {entry.id for entry in snapshot.models}
    occupied.add("sonnet")
    public_by_real: dict[str, str] = {}
    reverse: dict[str, str] = {}

    for entry in snapshot.models:
        candidate = (
            entry.id
            if _looks_like_safe_claude_model(entry.id) and entry.id not in public_by_real.values()
            else ""
        )
        if not candidate or candidate in occupied and candidate != entry.id:
            candidate = f"claude-sonnet-{_safe_anthropic_slug(entry.id) or 'model'}"
        alias = _allocate_alias(candidate, occupied, entry.id)
        public_by_real[entry.id] = alias
        reverse[alias] = entry.id
        occupied.add(alias)

    if snapshot.effective_default_model:
        reverse["sonnet"] = snapshot.effective_default_model
    for entry in snapshot.models:
        reverse.setdefault(_legacy_anthropic_alias(entry.id), entry.id)
        if _looks_like_safe_claude_model(entry.id) and entry.id != "sonnet":
            reverse.setdefault(entry.id, entry.id)
    return AnthropicAliasMap(public_by_real=public_by_real, reverse=reverse)


def model_from_anthropic_alias(model: str, snapshot: ModelCatalogSnapshot) -> str:
    """把 Anthropic 公开模型名映射回真实 Codex 模型名。"""
    aliases = build_anthropic_aliases(snapshot)
    return aliases.reverse.get(model, model)


async def _run_codex_debug_models() -> str:
    """执行 Codex CLI 模型发现命令。"""
    process = await asyncio.create_subprocess_exec(
        "codex",
        "debug",
        "models",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RuntimeError("codex debug models timed out") from error
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip() or f"codex debug models exited {process.returncode}"
        raise RuntimeError(message)
    return stdout.decode("utf-8", errors="replace")


def _overlay_config(
    entries: tuple[ModelCatalogEntry, ...],
    config: AppConfig,
    *,
    source: str,
    cache_state: str,
    warning: str | None,
) -> ModelCatalogSnapshot:
    """把当前配置叠加到 CLI 目录，保留默认模型兼容项。"""
    configured_models = _configured_models(config)
    sorted_cli_entries = _sort_cli_entries(entries)
    effective_default = _effective_default_model(configured_models, sorted_cli_entries)
    combined: dict[str, ModelCatalogEntry] = {}
    for entry in sorted_cli_entries:
        combined.setdefault(entry.id, entry)
    next_order = len(combined)
    for model_id in configured_models:
        if model_id not in combined:
            combined[model_id] = _compatibility_entry(
                model_id,
                order=next_order,
                source="config" if source == "config" else "compatibility",
            )
            next_order += 1
    ordered = list(combined.values())
    if effective_default:
        ordered.sort(key=lambda entry: (entry.id != effective_default, _entry_sort_key(entry)))
    else:
        ordered.sort(key=_entry_sort_key)
    snapshot_source = source
    return ModelCatalogSnapshot(
        models=tuple(ordered),
        effective_default_model=effective_default,
        source=snapshot_source,
        cache_state=cache_state,
        warning=warning,
    )


def _configured_models(config: AppConfig) -> tuple[str, ...]:
    """读取配置中的默认模型和可用模型，去重并过滤空白。"""
    models: list[str] = []
    default_model = str(config.codex.default_model).strip()
    if default_model:
        models.append(default_model)
    for model in config.codex.available_models:
        model_id = str(model).strip()
        if model_id and model_id not in models:
            models.append(model_id)
    return tuple(models)


def _effective_default_model(configured_models: tuple[str, ...], entries: list[ModelCatalogEntry]) -> str:
    """计算最终默认模型，空配置时使用 CodexConfig 内置默认值。"""
    if configured_models:
        return configured_models[0]
    if entries:
        return entries[0].id
    return str(CodexConfig.default_model).strip()


def _sort_cli_entries(entries: tuple[ModelCatalogEntry, ...]) -> list[ModelCatalogEntry]:
    """按 priority 和 CLI 原始顺序排序模型。"""
    return sorted((entry for entry in entries if entry.visible is not False), key=_entry_sort_key)


def _entry_sort_key(entry: ModelCatalogEntry) -> tuple[int, float, int]:
    """生成稳定排序键，无 priority 的模型排在有 priority 的模型之后。"""
    if entry.priority is None:
        return (1, 0, entry.order)
    return (0, float(entry.priority), entry.order)


def _compatibility_entry(model_id: str, *, order: int, source: str) -> ModelCatalogEntry:
    """为配置里仍在使用但 CLI 未返回的模型构造兼容项。"""
    return ModelCatalogEntry(id=model_id, display_name=model_id, source=source, order=order)


def _first_nonblank(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选字段顺序返回第一个非空白字符串。"""
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _supported_reasoning_efforts(item: dict[str, Any]) -> tuple[str, ...]:
    """规范化模型支持的 reasoning effort 列表。"""
    raw_value: Any = None
    for key in (
        "supported_reasoning_efforts",
        "supported_reasoning_levels",
        "reasoning_efforts",
        "reasoning_levels",
    ):
        if key in item:
            raw_value = item[key]
            break
    if not isinstance(raw_value, list):
        return DEFAULT_REASONING_EFFORTS

    efforts: list[str] = []
    for value in raw_value:
        if isinstance(value, dict):
            effort = _first_nonblank(value, ("effort", "level", "name"))
        else:
            effort = str(value).strip()
        if effort and effort not in efforts:
            efforts.append(effort)
    return tuple(efforts) if efforts else DEFAULT_REASONING_EFFORTS


def _model_visibility(item: dict[str, Any]) -> bool | None:
    """兼容 Codex CLI 新旧模型可见性字段。"""
    visible = item.get("visible")
    if isinstance(visible, bool):
        return visible
    visibility = str(item.get("visibility", "")).strip().lower()
    if visibility == "list":
        return True
    if visibility in {"hide", "hidden"}:
        return False
    return None


def _default_reasoning_effort(
    item: dict[str, Any],
    supported_efforts: tuple[str, ...],
    *,
    has_explicit_efforts: bool,
) -> str:
    """规范化默认 reasoning effort。"""
    default_effort = _first_nonblank(item, ("default_reasoning_effort", "default_reasoning_level"))
    if default_effort and default_effort in supported_efforts:
        return default_effort
    if has_explicit_efforts and supported_efforts:
        return supported_efforts[0]
    return DEFAULT_REASONING_EFFORT


def _has_explicit_reasoning_efforts(item: dict[str, Any]) -> bool:
    """判断 CLI 是否显式声明了支持的 reasoning effort 列表。"""
    for key in (
        "supported_reasoning_efforts",
        "supported_reasoning_levels",
        "reasoning_efforts",
        "reasoning_levels",
    ):
        if isinstance(item.get(key), list):
            return True
    return False


def _ensure_effort_supported(supported_efforts: tuple[str, ...], default_effort: str) -> tuple[str, ...]:
    """确保默认 effort 出现在支持列表中。"""
    if default_effort in supported_efforts:
        return supported_efforts
    return (*supported_efforts, default_effort)


def _looks_like_safe_claude_model(model: str) -> bool:
    """判断模型名是否已经是安全 Claude 风格。"""
    lowered = model.lower()
    return lowered.startswith(("claude", "anthropic")) and not _contains_forbidden_anthropic_text(lowered)


def _safe_anthropic_slug(model: str) -> str:
    """把真实模型名转换成不含禁用词的 Anthropic slug 片段。"""
    slug = _slug_model_name(model)
    for forbidden in ("gpt", "codex", "openai"):
        slug = slug.replace(forbidden, "")
    parts = [
        part
        for part in _slug_model_name(slug).split("-")
        if part not in {"claude", "anthropic"}
    ]
    return "-".join(parts)


def _contains_forbidden_anthropic_text(value: str) -> bool:
    """检查 Anthropic 公开文本是否包含禁用供应商子串。"""
    lowered = value.lower()
    return any(forbidden in lowered for forbidden in ("gpt", "codex", "openai"))


def _slug_model_name(model: str) -> str:
    """把模型名规范化成短横线分隔的片段。"""
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in model).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized


def _allocate_alias(candidate: str, occupied: set[str], own_id: str) -> str:
    """在全局占用集合里分配唯一公开别名。"""
    if candidate == own_id and candidate not in {"sonnet"}:
        return candidate
    alias = candidate
    suffix = 2
    while alias in occupied:
        alias = f"{candidate}-{suffix}"
        suffix += 1
    return alias


def _legacy_anthropic_alias(model: str) -> str:
    """生成旧版 claude-gpt 风格别名，用于兼容已保存配置。"""
    if model.startswith(("claude", "anthropic")):
        return model
    return f"claude-{_slug_model_name(model) or 'model'}"
