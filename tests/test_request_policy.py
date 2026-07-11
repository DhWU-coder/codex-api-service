"""测试按模型默认请求策略和调用方显式覆盖。"""

from codex_api_service.config import CodexConfig, ModelRequestDefaults
from codex_api_service.request_policy import resolve_request_policy


def configured_codex(*, legacy: bool = False) -> CodexConfig:
    """构造包含 gpt-5.4 默认策略的测试配置。"""
    return CodexConfig(
        reasoning_effort="xhigh",
        fast_mode=True,
        model_request_defaults={
            "gpt-5.4": ModelRequestDefaults(reasoning_effort="high", fast_mode=True)
        },
        uses_legacy_request_defaults=legacy,
    )


def test_policy_uses_model_defaults_without_explicit_overrides() -> None:
    """验证调用方未传参数时使用真实模型配置。"""
    policy = resolve_request_policy({}, configured_codex(), "gpt-5.4")

    assert policy.reasoning == {"effort": "high", "summary": "auto"}
    assert policy.service_tier == "priority"


def test_policy_uses_fixed_defaults_for_unconfigured_model() -> None:
    """验证新模式未配置模型固定使用 medium 和标准模式。"""
    policy = resolve_request_policy({}, configured_codex(), "gpt-5.5")

    assert policy.reasoning == {"effort": "medium", "summary": "auto"}
    assert policy.service_tier is None


def test_policy_uses_legacy_globals_before_migration() -> None:
    """验证旧配置在首次迁移前继续沿用全局默认值。"""
    policy = resolve_request_policy({}, configured_codex(legacy=True), "gpt-5.5")

    assert policy.reasoning["effort"] == "xhigh"
    assert policy.service_tier == "priority"


def test_policy_explicit_reasoning_effort_and_summary_merge() -> None:
    """验证 reasoning.effort 优先，只有 summary 时保留模型默认 effort。"""
    explicit = resolve_request_policy(
        {"reasoning": {"effort": "low", "summary": "detailed"}, "reasoning_effort": "medium"},
        configured_codex(),
        "gpt-5.4",
    )
    summary_only = resolve_request_policy(
        {"reasoning": {"summary": "detailed"}},
        configured_codex(),
        "gpt-5.4",
    )

    assert explicit.reasoning == {"effort": "low", "summary": "detailed"}
    assert summary_only.reasoning == {"effort": "high", "summary": "detailed"}


def test_policy_explicit_fast_mode_and_service_tier_override_defaults() -> None:
    """验证 fast_mode 优先于 service_tier，service_tier 也能单独覆盖模型默认值。"""
    fast_mode = resolve_request_policy(
        {"fast_mode": False, "service_tier": "fast"}, configured_codex(), "gpt-5.4"
    )
    standard = resolve_request_policy({"service_tier": "standard"}, configured_codex(), "gpt-5.4")
    fast = resolve_request_policy({"service_tier": "fast"}, configured_codex(), "gpt-5.5")

    assert fast_mode.service_tier is None
    assert standard.service_tier is None
    assert fast.service_tier == "priority"
