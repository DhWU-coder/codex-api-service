"""测试 Codex CLI 模型目录解析、缓存和兼容响应。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import asyncio

import pytest

from codex_api_service.config import AppConfig, CodexConfig
from codex_api_service.model_catalog import (
    CodexModelCatalog,
    anthropic_model_list_response,
    build_anthropic_aliases,
    model_from_anthropic_alias,
    model_list_response,
    parse_cli_models,
)


def make_config(
    tmp_path: Path,
    *,
    default_model: str = "gpt-5.5",
    available_models: tuple[str, ...] | None = None,
) -> AppConfig:
    """构造模型目录测试使用的最小配置。"""
    # 测试里直接构造 CodexConfig 时，手动模拟 load_config 的模型列表归一化。
    kwargs: dict[str, Any] = {"default_model": default_model, "available_models": available_models or ((default_model,) if default_model else ())}
    return AppConfig(project_root=tmp_path, codex=CodexConfig(**kwargs))


def cli_output(*models: dict[str, Any]) -> str:
    """构造 codex debug models 的 JSON 输出。"""
    return json.dumps({"models": list(models)})


def test_parse_cli_models_normalizes_entries() -> None:
    """验证标准 CLI JSON 输出会被规范化为不可变目录快照。"""
    # 使用代表性 codex debug models 输出，先锁定调用方需要的字段。
    raw_output = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT 5.6 SOL",
                    "default_reasoning_effort": "medium",
                    "supported_reasoning_efforts": ["low", "medium", "high"],
                }
            ]
        }
    )

    snapshot = parse_cli_models(raw_output)

    assert snapshot.models[0].id == "gpt-5.6-sol"
    assert snapshot.models[0].display_name == "GPT 5.6 SOL"
    assert snapshot.models[0].default_reasoning_effort == "medium"
    assert snapshot.models[0].supported_reasoning_efforts == ("low", "medium", "high")


def test_parse_cli_models_accepts_current_codex_debug_schema() -> None:
    """验证当前 Codex CLI 的对象型 effort 和 visibility 字段可以被正确解析。"""
    raw_output = cli_output(
        {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT-5.6-SOL",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "轻量推理"},
                {"effort": "medium", "description": "标准推理"},
                {"effort": "high", "description": "深度推理"},
            ],
            "visibility": "list",
        },
        {
            "slug": "codex-auto-review",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium"}],
            "visibility": "hide",
        },
    )

    snapshot = parse_cli_models(raw_output)

    visible, hidden = snapshot.models
    assert visible.visible is True
    assert visible.default_reasoning_effort == "medium"
    assert visible.supported_reasoning_efforts == ("low", "medium", "high")
    assert hidden.visible is False


def test_parse_cli_models_accepts_list_root_and_id_candidates() -> None:
    """验证 CLI 根节点为列表时也能读取 slug/id/model 候选字段。"""
    raw_output = json.dumps(
        [
            {"id": " gpt-5.6-alpha ", "name": "Alpha"},
            {"model": "gpt-5.6-beta", "display_name": "Beta"},
        ]
    )

    snapshot = parse_cli_models(raw_output)

    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-alpha", "gpt-5.6-beta"]
    assert [entry.display_name for entry in snapshot.models] == ["Alpha", "Beta"]


def test_parse_cli_models_skips_malformed_and_blank_ids() -> None:
    """验证非对象、空 ID 和空白 ID 会被忽略。"""
    raw_output = json.dumps(
        {
            "models": [
                None,
                "gpt-5.6-string",
                {"slug": ""},
                {"id": "   "},
                {"model": "gpt-5.6-valid"},
            ]
        }
    )

    snapshot = parse_cli_models(raw_output)

    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-valid"]


def test_parse_cli_models_defaults_sparse_display_and_efforts() -> None:
    """验证缺失名称和 sparse effort 字段会使用安全默认值。"""
    raw_output = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.6-sparse", "display_name": "  "},
                {
                    "slug": "gpt-5.6-custom",
                    "default_reasoning_level": "extreme",
                    "supported_reasoning_levels": ["low", " ", "high"],
                },
                {
                    "slug": "gpt-5.6-blank-default",
                    "default_reasoning_effort": " ",
                    "reasoning_efforts": ["low"],
                },
            ]
        }
    )

    snapshot = parse_cli_models(raw_output)

    sparse, custom, blank_default = snapshot.models
    assert sparse.display_name == "gpt-5.6-sparse"
    assert sparse.default_reasoning_effort == "medium"
    assert sparse.supported_reasoning_efforts == ("low", "medium", "high", "xhigh")
    assert custom.default_reasoning_effort == "low"
    assert custom.supported_reasoning_efforts == ("low", "high")
    assert blank_default.default_reasoning_effort == "low"
    assert blank_default.supported_reasoning_efforts == ("low",)


def test_parse_cli_models_preserves_visibility_priority_and_cli_order() -> None:
    """验证解析层保留 CLI 元数据和输入顺序，不提前排序。"""
    raw_output = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.6-first", "visible": False, "priority": 20},
                {"slug": "gpt-5.6-second", "visible": True, "priority": 10},
                {"slug": "gpt-5.6-third"},
            ]
        }
    )

    snapshot = parse_cli_models(raw_output)

    assert [entry.id for entry in snapshot.models] == [
        "gpt-5.6-first",
        "gpt-5.6-second",
        "gpt-5.6-third",
    ]
    assert snapshot.models[0].visible is False
    assert snapshot.models[0].priority == 20
    assert snapshot.models[1].visible is True
    assert snapshot.models[1].priority == 10


def test_parse_cli_models_returns_empty_snapshot_for_empty_or_unknown_shapes() -> None:
    """验证空目录或未知 JSON 结构会返回空快照，留给路由层决定是否 503。"""
    assert parse_cli_models(json.dumps({"models": []})).models == ()
    assert parse_cli_models(json.dumps({"unexpected": []})).models == ()
    assert parse_cli_models(json.dumps({"models": {"slug": "gpt-5.6"}})).models == ()


@pytest.mark.asyncio
async def test_catalog_first_request_waits_for_cli_and_uses_fresh_cache(tmp_path: Path) -> None:
    """验证首次读取会等待 CLI，并在 TTL 内复用 fresh 缓存。"""
    calls = 0
    now = 100.0

    async def runner() -> str:
        nonlocal calls
        calls += 1
        return cli_output({"slug": "gpt-5.6-sol"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-sol"),
        runner=runner,
        clock=lambda: now,
    )

    first = await catalog.snapshot()
    second = await catalog.snapshot()

    assert calls == 1
    assert first.cache_state == "fresh"
    assert second.cache_state == "fresh"
    assert [entry.id for entry in second.models] == ["gpt-5.6-sol"]


@pytest.mark.asyncio
async def test_catalog_expired_request_refreshes_cli_cache(tmp_path: Path) -> None:
    """验证超过 fresh TTL 的普通读取会重新执行 CLI。"""
    calls = 0
    now = 100.0

    async def runner() -> str:
        nonlocal calls
        calls += 1
        return cli_output({"slug": f"gpt-5.6-{calls}"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-1"),
        runner=runner,
        clock=lambda: now,
    )

    first = await catalog.snapshot()
    now = 161.0
    second = await catalog.snapshot()

    assert calls == 2
    assert [entry.id for entry in first.models] == ["gpt-5.6-1"]
    assert [entry.id for entry in second.models] == ["gpt-5.6-1", "gpt-5.6-2"]


@pytest.mark.asyncio
async def test_catalog_cli_failure_with_old_cache_returns_stale(tmp_path: Path) -> None:
    """验证 CLI 失败但已有旧缓存时返回 stale 目录。"""
    calls = 0
    now = 100.0

    async def runner() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cli_output({"slug": "gpt-5.6-old"})
        raise RuntimeError("cli unavailable")

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-old"),
        runner=runner,
        clock=lambda: now,
    )

    await catalog.snapshot()
    now = 161.0
    snapshot = await catalog.snapshot()

    assert calls == 2
    assert snapshot.cache_state == "stale"
    assert snapshot.source == "cli"
    assert snapshot.warning is not None
    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-old"]


@pytest.mark.asyncio
async def test_catalog_cli_failure_without_cache_falls_back_to_config(tmp_path: Path) -> None:
    """验证无 CLI 缓存时回退到 default_model 和 available_models。"""

    async def runner() -> str:
        raise RuntimeError("cli unavailable")

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(
            tmp_path,
            default_model="gpt-5.6-default",
            available_models=("gpt-5.6-extra", "gpt-5.6-default"),
        ),
        runner=runner,
        clock=lambda: 100.0,
    )

    snapshot = await catalog.snapshot()

    assert snapshot.cache_state == "fallback"
    assert snapshot.source == "config"
    assert snapshot.effective_default_model == "gpt-5.6-default"
    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-default", "gpt-5.6-extra"]
    assert {entry.source for entry in snapshot.models} == {"config"}


@pytest.mark.asyncio
async def test_catalog_hides_invisible_cli_models_unless_configured(tmp_path: Path) -> None:
    """验证 CLI 标记不可见的模型不会暴露，除非它是当前配置值。"""

    async def runner() -> str:
        return cli_output(
            {"slug": "gpt-5.6-hidden", "visible": False},
            {"slug": "gpt-5.6-visible", "visible": True},
        )

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-visible"),
        runner=runner,
        clock=lambda: 100.0,
    )
    configured_catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-hidden"),
        runner=runner,
        clock=lambda: 100.0,
    )

    snapshot = await catalog.snapshot()
    configured_snapshot = await configured_catalog.snapshot()

    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-visible"]
    assert [entry.id for entry in configured_snapshot.models] == ["gpt-5.6-hidden", "gpt-5.6-visible"]
    assert configured_snapshot.models[0].source == "compatibility"


@pytest.mark.asyncio
async def test_catalog_exposes_only_seven_list_models_from_current_cli_schema(tmp_path: Path) -> None:
    """验证当前 CLI 返回的隐藏模型不会进入 Web 下拉列表。"""
    listed_models = (
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    )

    async def runner() -> str:
        models = [
            {
                "slug": model_id,
                "visibility": "list",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
            }
            for model_id in listed_models
        ]
        models.append(
            {
                "slug": "codex-auto-review",
                "visibility": "hide",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "medium"}],
            }
        )
        return cli_output(*models)

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-sol"),
        runner=runner,
        clock=lambda: 100.0,
    )

    snapshot = await catalog.snapshot()

    assert len(snapshot.models) == 7
    assert {entry.id for entry in snapshot.models} == set(listed_models)


@pytest.mark.asyncio
async def test_catalog_empty_config_fallback_returns_empty_snapshot(tmp_path: Path) -> None:
    """验证配置兜底也没有模型时返回空 fallback 快照。"""

    async def runner() -> str:
        raise RuntimeError("cli unavailable")

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="", available_models=()),
        runner=runner,
        clock=lambda: 100.0,
    )

    snapshot = await catalog.snapshot()

    assert snapshot.cache_state == "fallback"
    assert snapshot.source == "config"
    assert snapshot.models == ()


@pytest.mark.asyncio
async def test_catalog_forced_refresh_failure_controls_cooldown(tmp_path: Path) -> None:
    """验证强刷失败会立即标记 stale，并在冷却期后允许普通请求重试。"""
    calls = 0
    now = 100.0

    async def runner() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cli_output({"slug": "gpt-5.6-old"})
        if calls == 2:
            raise RuntimeError("cli unavailable")
        return cli_output({"slug": "gpt-5.6-new"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-old"),
        runner=runner,
        clock=lambda: now,
    )

    await catalog.snapshot()
    now = 110.0
    stale = await catalog.snapshot(refresh=True)
    inside_cooldown = await catalog.snapshot()
    now = 141.0
    refreshed = await catalog.snapshot()

    assert calls == 3
    assert stale.cache_state == "stale"
    assert inside_cooldown.cache_state == "stale"
    assert [entry.id for entry in inside_cooldown.models] == ["gpt-5.6-old"]
    assert refreshed.cache_state == "fresh"
    assert [entry.id for entry in refreshed.models] == ["gpt-5.6-old", "gpt-5.6-new"]


@pytest.mark.asyncio
async def test_catalog_concurrent_requests_share_refresh(tmp_path: Path) -> None:
    """验证无历史并发请求会合并为一次 CLI 调用。"""
    calls = 0
    release = asyncio.Event()

    async def runner() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return cli_output({"slug": "gpt-5.6-shared"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-shared"),
        runner=runner,
        clock=lambda: 100.0,
    )
    tasks = [asyncio.create_task(catalog.snapshot()) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    snapshots = await asyncio.gather(*tasks)

    assert calls == 1
    assert [snapshot.cache_state for snapshot in snapshots] == ["fresh", "fresh", "fresh"]


@pytest.mark.asyncio
async def test_catalog_cancelled_waiter_does_not_cancel_shared_refresh(tmp_path: Path) -> None:
    """验证取消单个等待请求不会取消共享 CLI 刷新任务。"""
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return cli_output({"slug": "gpt-5.6-shared"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-shared"),
        runner=runner,
        clock=lambda: 100.0,
    )
    cancelled_task = asyncio.create_task(catalog.snapshot())
    surviving_task = asyncio.create_task(catalog.snapshot())
    await started.wait()
    cancelled_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    snapshot = await surviving_task

    assert calls == 1
    assert [entry.id for entry in snapshot.models] == ["gpt-5.6-shared"]


@pytest.mark.asyncio
async def test_catalog_concurrent_forced_refresh_requests_share_refresh(tmp_path: Path) -> None:
    """验证多个强制刷新请求也会合并为一次 CLI 调用。"""
    calls = 0
    release = asyncio.Event()

    async def runner() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return cli_output({"slug": "gpt-5.6-forced"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-forced"),
        runner=runner,
        clock=lambda: 100.0,
    )
    tasks = [asyncio.create_task(catalog.snapshot(refresh=True)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    snapshots = await asyncio.gather(*tasks)

    assert calls == 1
    assert [entry.id for entry in snapshots[0].models] == ["gpt-5.6-forced"]


@pytest.mark.asyncio
async def test_catalog_fresh_request_returns_while_forced_refresh_runs(tmp_path: Path) -> None:
    """验证 fresh 普通请求不会被正在执行的强制刷新阻塞。"""
    calls = 0
    now = 100.0
    release = asyncio.Event()

    async def runner() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cli_output({"slug": "gpt-5.6-old"})
        await release.wait()
        return cli_output({"slug": "gpt-5.6-new"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="gpt-5.6-old"),
        runner=runner,
        clock=lambda: now,
    )

    await catalog.snapshot()
    forced_task = asyncio.create_task(catalog.snapshot(refresh=True))
    await asyncio.sleep(0)
    ordinary = await catalog.snapshot()
    release.set()
    forced = await forced_task

    assert calls == 2
    assert [entry.id for entry in ordinary.models] == ["gpt-5.6-old"]
    assert [entry.id for entry in forced.models] == ["gpt-5.6-old", "gpt-5.6-new"]


def test_model_list_response_returns_openai_shape() -> None:
    """验证 OpenAI 模型发现响应使用真实模型 ID。"""
    snapshot = parse_cli_models(cli_output({"slug": "gpt-5.6-sol"}))

    body = model_list_response(snapshot)

    assert body["object"] == "list"
    assert body["data"][0]["id"] == "gpt-5.6-sol"
    assert body["data"][0]["object"] == "model"


def test_anthropic_aliases_are_safe_and_reversible() -> None:
    """验证 Anthropic 公开别名安全，且能映射回真实 Codex 模型。"""
    snapshot = parse_cli_models(
        cli_output(
            {"slug": "gpt-5.6-sol"},
            {"slug": "codex-5.6-sol"},
            {"slug": "claude-sonnet-real"},
        )
    )

    aliases = build_anthropic_aliases(snapshot)
    response = anthropic_model_list_response(snapshot)
    serialized = json.dumps(response).lower()

    assert aliases.public_by_real["gpt-5.6-sol"] == "claude-sonnet-5-6-sol"
    assert aliases.public_by_real["codex-5.6-sol"] == "claude-sonnet-5-6-sol-2"
    assert aliases.public_by_real["claude-sonnet-real"] == "claude-sonnet-real"
    assert "gpt" not in serialized
    assert "codex" not in serialized
    assert response["first_id"] == "claude-sonnet-5-6-sol"
    assert response["data"][0]["display_name"] == "claude-sonnet-5-6-sol"
    assert model_from_anthropic_alias("claude-sonnet-5-6-sol", snapshot) == "gpt-5.6-sol"
    assert model_from_anthropic_alias("claude-gpt-5-6-sol", snapshot) == "gpt-5.6-sol"


def test_anthropic_aliases_reserve_sonnet_for_default_model() -> None:
    """验证裸 sonnet 永远指向 effective default，不被真实同名模型抢占。"""
    snapshot = parse_cli_models(cli_output({"slug": "sonnet"}, {"slug": "gpt-5.6-sol"}))
    snapshot = type(snapshot)(
        models=snapshot.models,
        effective_default_model="gpt-5.6-sol",
        source=snapshot.source,
        cache_state=snapshot.cache_state,
        warning=snapshot.warning,
    )

    aliases = build_anthropic_aliases(snapshot)

    assert aliases.reverse["sonnet"] == "gpt-5.6-sol"
    assert aliases.public_by_real["sonnet"] != "sonnet"
    assert model_from_anthropic_alias(aliases.public_by_real["sonnet"], snapshot) == "sonnet"


def test_anthropic_aliases_remove_forbidden_substrings() -> None:
    """验证 gpt/codex/openai 子串不会出现在 Anthropic 公开响应里。"""
    snapshot = parse_cli_models(
        cli_output(
            {"slug": "claude-gpt5-safeish"},
            {"slug": "codex2-model"},
            {"slug": "openaiish-model"},
        )
    )

    response = anthropic_model_list_response(snapshot)
    serialized = json.dumps(response).lower()

    assert "gpt" not in serialized
    assert "codex" not in serialized
    assert "openai" not in serialized


@pytest.mark.asyncio
async def test_catalog_uses_first_cli_model_as_default_when_config_is_blank(tmp_path: Path) -> None:
    """验证配置空白但 CLI 有模型时，effective default 使用第一个 CLI 模型。"""

    async def runner() -> str:
        return cli_output({"slug": "gpt-5.6-first"}, {"slug": "gpt-5.6-second"})

    catalog = CodexModelCatalog(
        config_getter=lambda: make_config(tmp_path, default_model="", available_models=()),
        runner=runner,
        clock=lambda: 100.0,
    )

    snapshot = await catalog.snapshot()

    assert snapshot.effective_default_model == "gpt-5.6-first"
    assert model_from_anthropic_alias("sonnet", snapshot) == "gpt-5.6-first"
