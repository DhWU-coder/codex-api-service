"""集中解析按模型默认值和调用方显式请求参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import (
    DEFAULT_MODEL_FAST_MODE,
    DEFAULT_MODEL_REASONING_EFFORT,
    CodexConfig,
    ModelRequestDefaults,
)

CODEX_FAST_SERVICE_TIER = "priority"


@dataclass(frozen=True)
class ResolvedRequestPolicy:
    """描述合并默认值和显式参数后的上游请求策略。"""

    reasoning: dict[str, Any]
    service_tier: str | None


def resolve_request_policy(
    body: dict[str, Any],
    config: CodexConfig,
    model: str,
) -> ResolvedRequestPolicy:
    """按显式参数、模型默认值和固定兜底顺序解析请求策略。"""
    defaults = _defaults_for_model(config, model)
    reasoning: dict[str, Any] = {"effort": defaults.reasoning_effort, "summary": "auto"}
    request_reasoning = body.get("reasoning")
    if isinstance(request_reasoning, dict):
        reasoning.update(request_reasoning)
        effort = request_reasoning.get("effort")
        if not isinstance(effort, str) or not effort.strip():
            request_effort = body.get("reasoning_effort")
            reasoning["effort"] = (
                request_effort.strip()
                if isinstance(request_effort, str) and request_effort.strip()
                else defaults.reasoning_effort
            )
    elif isinstance(body.get("reasoning_effort"), str) and body["reasoning_effort"].strip():
        reasoning["effort"] = body["reasoning_effort"].strip()

    service_tier = _resolve_service_tier(body, defaults.fast_mode)
    return ResolvedRequestPolicy(reasoning=reasoning, service_tier=service_tier)


def _defaults_for_model(config: CodexConfig, model: str) -> ModelRequestDefaults:
    """返回真实模型的有效默认值，并兼容尚未迁移的旧配置。"""
    if config.uses_legacy_request_defaults:
        return ModelRequestDefaults(
            reasoning_effort=config.reasoning_effort,
            fast_mode=config.fast_mode,
        )
    return config.model_request_defaults.get(
        model,
        ModelRequestDefaults(
            reasoning_effort=DEFAULT_MODEL_REASONING_EFFORT,
            fast_mode=DEFAULT_MODEL_FAST_MODE,
        ),
    )


def _resolve_service_tier(body: dict[str, Any], default_fast_mode: bool) -> str | None:
    """按 fast_mode、service_tier 和模型默认值解析上游服务层。"""
    if "fast_mode" in body:
        return CODEX_FAST_SERVICE_TIER if _request_bool(body["fast_mode"]) else None
    if "service_tier" in body:
        tier = str(body["service_tier"]).strip().lower()
        if tier in {"fast", CODEX_FAST_SERVICE_TIER}:
            return CODEX_FAST_SERVICE_TIER
        if tier in {"", "auto", "default", "standard"}:
            return None
        return tier
    return CODEX_FAST_SERVICE_TIER if default_fast_mode else None


def _request_bool(value: Any) -> bool:
    """规范化请求体布尔值，兼容命令行常见字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "fast"}
    return bool(value)
