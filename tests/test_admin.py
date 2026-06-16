import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from codex_api_service.app import create_app
from codex_api_service.config import AppConfig, ApiConfig, AuthConfig, CodexConfig, ServerConfig, UsageConfig


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
        auth=AuthConfig(auth_path=tmp_path / "auth.json", import_auth_path=tmp_path / "missing-auth.json"),
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
