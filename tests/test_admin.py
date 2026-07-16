import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from codex_api_service.app import create_app
from codex_api_service.config import (
    AppConfig,
    ApiConfig,
    AuthConfig,
    CodexConfig,
    ModelRequestDefaults,
    ServerConfig,
    UsageConfig,
)
from codex_api_service.request_log import RequestLogStore


class AdminFakeCodexClient:
    """为管理台测试提供不会访问网络的 Codex client。"""

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """返回固定响应，帮助测试请求日志记录。"""
        return {
            "id": "resp_admin_fake",
            "output_text": "hello",
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回固定流式响应，保持接口完整。"""
        yield {"type": "response.output_text.delta", "delta": "hello"}
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_admin_fake",
                "output_text": "hello",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        }


def make_admin_config(tmp_path: Path, api_key: str | None = None) -> AppConfig:
    """构造测试用配置，并把日志写入临时目录。"""
    return AppConfig(
        project_root=tmp_path,
        server=ServerConfig(host="127.0.0.1", port=1219),
        api=ApiConfig(local_api_key=api_key),
        codex=CodexConfig(default_model="gpt-5.5", reasoning_effort="medium"),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
        auth=AuthConfig(
            import_auth_path=tmp_path / "missing-auth.json",
            account_store_path=tmp_path / ".codex-oauth",
        ),
    )


def write_admin_auth_file(path: Path, *, expires: int) -> None:
    """写入管理台 health 测试用 OAuth 文件，不包含真实 token。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "account_id": "acct_test",
                },
                "expires": expires,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_admin_config_returns_safe_snapshot_without_secret(tmp_path: Path) -> None:
    """验证管理配置接口不泄露本地 API key 明文。"""
    # 配置 API key 后，接口响应只应该说明已配置，不返回 secret。
    app = create_app(config=make_admin_config(tmp_path, api_key="local-secret"), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/config", headers={"Authorization": "Bearer local-secret"})

    # 返回安全快照，包含 UI 需要展示和编辑的字段。
    assert response.status_code == 200
    body = response.json()
    assert body["api"]["local_api_key_configured"] is True
    assert "local-secret" not in json.dumps(body)
    assert body["codex"]["default_model"] == "gpt-5.5"
    assert body["codex"]["reasoning_effort"] == "medium"
    assert body["codex"]["fast_mode"] is True
    assert body["usage"]["enabled"] is True
    assert "auth_path" not in body["auth"]


@pytest.mark.asyncio
async def test_admin_config_returns_and_migrates_model_request_defaults(tmp_path: Path) -> None:
    """验证管理接口返回按模型配置，保存时删除旧全局字段。"""
    (tmp_path / "config.yaml").write_text(
        "codex:\n  default_model: gpt-5.5\n  reasoning_effort: high\n  fast_mode: true\n",
        encoding="utf-8",
    )
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/admin/config",
            json={
                "codex": {
                    "model_request_defaults": {
                        "gpt-5.4": {"reasoning_effort": "high", "fast_mode": True},
                        "gpt-5.5": {"reasoning_effort": "medium", "fast_mode": False},
                    }
                }
            },
        )
        snapshot = await client.get("/admin/config")

    assert response.status_code == 200
    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "model_request_defaults:" in saved
    assert "gpt-5.4:" in saved
    assert "reasoning_effort: high" in saved
    assert "gpt-5.5:" not in saved
    assert "\n  fast_mode:" not in saved
    assert snapshot.json()["codex"]["model_request_defaults"] == {
        "gpt-5.4": {"reasoning_effort": "high", "fast_mode": True}
    }
    assert snapshot.json()["codex"]["uses_legacy_request_defaults"] is False


@pytest.mark.asyncio
async def test_admin_config_rejects_invalid_model_request_defaults(tmp_path: Path) -> None:
    """验证管理接口把非法按模型配置转换为 400。"""
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/admin/config",
            json={"codex": {"model_request_defaults": []}},
        )

    assert response.status_code == 400
    assert "model_request_defaults" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_admin_config_patch_writes_config_yaml(tmp_path: Path) -> None:
    """验证配置保存接口会写入 config.yaml 并提示重启。"""
    # 准备一个应用，配置文件应写到临时项目根目录。
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())

    # PATCH 只更新白名单字段，避免 UI 误写复杂配置。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/admin/config",
            json={
                "api": {"local_api_key": "new-key"},
                "codex": {"default_model": "gpt-5.5-mini", "reasoning_effort": "high", "fast_mode": False},
                "usage": {"enabled": False},
                "auth": {"import_auth_path": "~/.codex/auth.json"},
            },
        )

    # 响应提示需要重启服务，文件内容应包含新值。
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "local_api_key: new-key" in saved
    assert "default_model: gpt-5.5-mini" in saved
    assert "reasoning_effort: high" in saved
    assert "fast_mode: false" in saved
    assert "enabled: false" in saved
    assert "import_auth_path: ~/.codex/auth.json" in saved


@pytest.mark.asyncio
async def test_admin_config_patch_hot_applies_runtime_fields(tmp_path: Path) -> None:
    """验证不需要重启的配置会立即更新当前服务运行状态。"""
    # 当前应用启动时没有 API key，PATCH 后应立即要求新 key。
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        patch_response = await client.patch(
            "/admin/config",
            json={
                "api": {"local_api_key": "new-key"},
                "codex": {"default_model": "gpt-5.5-mini", "reasoning_effort": "high", "fast_mode": False},
                "usage": {"enabled": False},
            },
        )
        denied = await client.get("/v1/models")
        allowed = await client.get("/v1/models", headers={"Authorization": "Bearer new-key"})
        snapshot = await client.get("/admin/config", headers={"Authorization": "Bearer new-key"})

    # 这些字段不需要重启，响应应明确说明已立即生效。
    assert patch_response.status_code == 200
    assert patch_response.json()["restart_required"] is False
    assert patch_response.json()["applied"] is True
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["data"][0]["id"] == "gpt-5.5-mini"
    assert snapshot.json()["codex"]["reasoning_effort"] == "high"
    assert snapshot.json()["usage"]["enabled"] is False


@pytest.mark.asyncio
async def test_admin_health_reports_runtime_status_without_secrets(tmp_path: Path) -> None:
    """验证管理台 health 接口返回运行状态且不泄露密钥。"""
    # health 只检查状态，不应触发浏览器登录，也不能返回 token。
    app = create_app(config=make_admin_config(tmp_path, api_key="local-secret"), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/health", headers={"Authorization": "Bearer local-secret"})

    # 返回控制台可展示的健康信息。
    assert response.status_code == 200
    body = response.json()
    assert body["server"]["console"].endswith("/ui")
    assert body["oauth"]["available"] is False
    assert body["usage"]["enabled"] is True
    assert body["usage"]["writable"] is True
    assert body["ui"]["built"] in {True, False}
    assert "local-secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_admin_health_reports_expired_oauth_file(tmp_path: Path) -> None:
    """验证 health 能区分 OAuth 文件存在和 token 已过期。"""
    # 过期的本服务 auth 文件不能再被视为完全可用。
    config = make_admin_config(tmp_path, api_key="local-secret")
    write_admin_auth_file((config.auth.account_store_path or tmp_path / ".codex-oauth") / "single-account-auth.json", expires=1)
    app = create_app(config=config, codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/health", headers={"Authorization": "Bearer local-secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["oauth"]["available"] is True
    assert body["oauth"]["expired"] is True
    assert "test-access" not in json.dumps(body)
    assert "test-refresh" not in json.dumps(body)


@pytest.mark.asyncio
async def test_admin_codex_usage_returns_safe_limit_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证管理台额度接口返回脱敏后的 Codex usage 摘要。"""
    config = make_admin_config(tmp_path, api_key="local-secret")
    write_admin_auth_file(
        (config.auth.account_store_path or tmp_path / ".codex-oauth") / "single-account-auth.json",
        expires=4_102_444_800_000,
    )

    async def fake_fetch_usage(*, client_version: str) -> dict[str, Any]:
        assert client_version
        return {
            "planType": "pro",
            "rateLimit": {
                "allowed": True,
                "limitReached": False,
                "windows": [
                    {
                        "label": "5h",
                        "kind": "primary",
                        "usedPercent": 33,
                        "remainingPercent": 67,
                        "limitWindowSeconds": 18000,
                        "resetAfterSeconds": 1200,
                        "resetAt": 1783814968,
                    },
                    {
                        "label": "Weekly",
                        "kind": "secondary",
                        "usedPercent": 21,
                        "remainingPercent": 79,
                        "limitWindowSeconds": 604800,
                        "resetAfterSeconds": 580000,
                        "resetAt": 1784370805,
                    },
                ],
            },
            "additionalRateLimits": [],
            "credits": {"hasCredits": False, "unlimited": False, "overageLimitReached": False, "balance": "0"},
        }

    monkeypatch.setattr("codex_api_service.app.fetch_codex_usage_snapshot", fake_fetch_usage, raising=False)
    app = create_app(config=config, codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/admin/codex/usage")
        response = await client.get("/admin/codex/usage", headers={"Authorization": "Bearer local-secret"})

    assert denied.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["planType"] == "pro"
    assert body["rateLimit"]["windows"][0]["remainingPercent"] == 67
    assert "test-access" not in json.dumps(body)


@pytest.mark.asyncio
async def test_admin_codex_usage_reports_safe_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证额度接口上游失败时不把敏感内容透传给前端。"""
    config = make_admin_config(tmp_path, api_key="local-secret")
    write_admin_auth_file(
        (config.auth.account_store_path or tmp_path / ".codex-oauth") / "single-account-auth.json",
        expires=4_102_444_800_000,
    )

    async def fake_fetch_usage(*, client_version: str) -> dict[str, Any]:
        raise RuntimeError("upstream failed with test-access")

    monkeypatch.setattr("codex_api_service.app.fetch_codex_usage_snapshot", fake_fetch_usage, raising=False)
    app = create_app(config=config, codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/codex/usage", headers={"Authorization": "Bearer local-secret"})

    assert response.status_code == 502
    serialized = json.dumps(response.json())
    assert "test-access" not in serialized


@pytest.mark.asyncio
async def test_admin_requests_lists_recent_api_calls(tmp_path: Path) -> None:
    """验证请求日志接口能展示最近 API 调用元数据。"""
    # 先调用一次 chat completion，让服务记录请求日志。
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )
        response = await client.get("/admin/requests")

    # 请求日志只包含元数据和 usage，不包含 prompt 或 response 正文。
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["path"] == "/v1/chat/completions"
    assert body["items"][0]["model"] == "gpt-5.5"
    assert body["items"][0]["status_code"] == 200
    assert body["items"][0]["usage"]["total"] == 5
    serialized = json.dumps(body, ensure_ascii=False)
    assert "hello" not in serialized
    assert "Authorization" not in serialized


def test_request_log_persists_safe_policy_metadata(tmp_path: Path) -> None:
    """验证安全策略元数据会写入 JSONL，并能在重新加载后恢复。"""
    path = tmp_path / "logs" / "requests.jsonl"
    store = RequestLogStore(path=path)

    store.record(
        method="POST",
        path="/v1/responses",
        model="gpt-5.5",
        status_code=200,
        duration_ms=321,
        stream=True,
        reasoning_effort="high",
        fast_mode=True,
        service_tier="priority",
        account_key="account-a",
        account_alias="owner@example.com",
    )

    item = RequestLogStore(path=path).list_recent()[0]
    assert item["stream"] is True
    assert item["reasoning_effort"] == "high"
    assert item["fast_mode"] is True
    assert item["service_tier"] == "priority"
    assert item["account_key"] == "account-a"
    assert item["account_alias"] == "owner@example.com"


def test_request_log_loads_legacy_entry_without_policy_metadata(tmp_path: Path) -> None:
    """验证旧日志缺少策略字段时仍可读取，并用空值明确表示未记录。"""
    path = tmp_path / "logs" / "requests.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": "req_legacy",
                "timestamp": "2026-07-16T10:00:00.000Z",
                "method": "POST",
                "path": "/v1/chat/completions",
                "model": "gpt-5.5",
                "status_code": 200,
                "duration_ms": 100,
                "usage": None,
                "request_id": "resp_legacy",
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    item = RequestLogStore(path=path).list_recent()[0]
    assert item["stream"] is None
    assert item["reasoning_effort"] is None
    assert item["fast_mode"] is None
    assert item["service_tier"] is None
    assert item["account_key"] is None
    assert item["account_alias"] is None


@pytest.mark.asyncio
async def test_admin_requests_survive_app_restart(tmp_path: Path) -> None:
    """验证请求日志会持久化，应用重启后仍能展示最近请求元数据。"""
    # 第一个应用实例记录一条请求日志。
    first_app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=first_app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    # 第二个应用实例模拟重启后重新加载同一项目目录。
    second_app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=second_app), base_url="http://test") as client:
        response = await client.get("/admin/requests")

    # 重启后仍能看到最近请求，但不能包含 prompt 内容。
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["path"] == "/v1/chat/completions"
    assert body["items"][0]["usage"]["total"] == 5
    assert "hello" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_admin_requests_can_list_all_persisted_entries(tmp_path: Path) -> None:
    """验证请求日志接口支持读取超过内存窗口的全部持久化记录。"""
    # 直接写入超过 200 条历史，模拟本地服务长期运行后的请求日志文件。
    requests_path = tmp_path / "logs" / "requests.jsonl"
    requests_path.parent.mkdir(parents=True)
    lines = []
    for index in range(1200):
        lines.append(
            json.dumps(
                {
                    "id": f"req_{index:03d}",
                    "timestamp": f"2026-07-10T10:{index // 60:02d}:{index % 60:02d}.000Z",
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "model": "gpt-5.5",
                    "status_code": 200,
                    "duration_ms": 100 + index,
                    "usage": {"total": index, "input": index, "cached": 0, "output": 0, "reasoning": 0},
                    "request_id": f"resp_{index:03d}",
                    "error": None,
                },
                ensure_ascii=False,
            )
        )
    requests_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        limited = await client.get("/admin/requests")
        thousand_entries = await client.get("/admin/requests?limit=1000")
        five_thousand_entries = await client.get("/admin/requests?limit=5000")
        all_entries = await client.get("/admin/requests?limit=all")

    # 默认接口仍保持最近窗口；limit=all 返回完整持久化历史，且按新到旧排序。
    assert limited.status_code == 200
    assert thousand_entries.status_code == 200
    assert five_thousand_entries.status_code == 200
    assert all_entries.status_code == 200
    assert len(limited.json()["items"]) == 100
    assert len(thousand_entries.json()["items"]) == 1000
    assert len(five_thousand_entries.json()["items"]) == 1200
    body = all_entries.json()
    assert len(body["items"]) == 1200
    assert body["items"][0]["id"] == "req_1199"
    assert body["items"][-1]["id"] == "req_000"


@pytest.mark.asyncio
async def test_admin_requests_imports_existing_usage_history(tmp_path: Path) -> None:
    """验证旧版 usage 历史在没有请求日志文件时也能进入看板统计。"""
    # 旧版本只写 codex-usage 日志，没有 logs/requests.jsonl，请求看板应能读回这类历史。
    usage_path = tmp_path / ".codex-usage" / "usage.jsonl"
    usage_path.parent.mkdir(parents=True)
    usage_path.write_text(
        json.dumps(
            {
                "schema_version": "codex-usage.project-log.v1",
                "timestamp": "2026-06-16T16:31:52.406Z",
                "source": "codex-oauth",
                "channel": "Codex OAuth",
                "project_root": str(tmp_path),
                "cwd": str(tmp_path),
                "session_id": "run-history",
                "model": "gpt-5.5",
                "usage": {"total": 9332, "input": 8798, "cached": 7424, "output": 534, "reasoning": 505},
                "request_id": "resp_history",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/requests")

    # 历史 usage 会被转换为只含元数据的请求记录，不会暴露 prompt 或响应正文。
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == "req_usage_resp_history"
    assert body["items"][0]["path"] == "/usage/history"
    assert body["items"][0]["model"] == "gpt-5.5"
    assert body["items"][0]["status_code"] == 200
    assert body["items"][0]["usage"]["total"] == 9332
    assert "hello" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_admin_dashboard_returns_aggregated_history_and_rejects_invalid_range(tmp_path: Path) -> None:
    """验证看板接口只返回聚合结果，并严格校验范围。"""
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        completion = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )
        dashboard = await client.get("/admin/dashboard?range=all&recent_days=7")
        invalid = await client.get("/admin/dashboard?range=invalid")

    assert completion.status_code == 200
    assert dashboard.status_code == 200
    assert dashboard.json()["requestCount"] == 1
    assert dashboard.json()["totalTokens"] == 5
    assert "items" not in dashboard.json()
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_ui_route_serves_html_shell(tmp_path: Path) -> None:
    """验证 /ui 可以返回前端壳页面。"""
    # 即使前端尚未构建，后端也应返回一个可读的提示页。
    app = create_app(config=make_admin_config(tmp_path), codex_client=AdminFakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ui")

    # UI 路由应返回 HTML，真实构建后会替换为 React 入口。
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Codex API Console" in response.text
