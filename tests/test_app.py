import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from codex_api_service.app import create_app
from codex_api_service.codex_client import CodexHTTPStatusError, CodexUnexpectedResponseError
from codex_api_service.config import AppConfig, ApiConfig, AuthConfig, CodexConfig, ServerConfig, UsageConfig
from codex_api_service.model_catalog import ModelCatalogEntry, ModelCatalogSnapshot


class FakeCodexClient:
    """为 API 路由测试提供不访问网络的 Codex client。"""

    def __init__(self) -> None:
        """记录收到的 payload，方便断言路由转换逻辑。"""
        self.payloads: list[dict[str, Any]] = []

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """返回固定 Codex 响应，模拟非流式 Codex backend。"""
        self.payloads.append(payload)
        return {
            "id": "resp_fake",
            "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回固定增量事件，模拟流式 Codex backend。"""
        self.payloads.append(payload)
        yield {"type": "response.output_text.delta", "delta": "hel"}
        yield {"type": "response.output_text.delta", "delta": "lo"}
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_fake",
                "output_text": "hello",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        }


class FailingStreamCodexClient:
    """模拟 Codex backend 在流式请求开始后返回 HTTP 错误。"""

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """非流式测试不会使用这个方法。"""
        raise AssertionError("create_response should not be called")

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """抛出上游 HTTP 错误，复现浏览器 network error 的根因。"""
        raise CodexHTTPStatusError(
            400,
            '{"detail":"The gpt-5.5 model requires a newer version of Codex."}',
        )
        yield {}


class ToolStreamCodexClient:
    """模拟 Codex backend 流式返回工具调用。"""

    def __init__(self) -> None:
        """记录收到的 payload，方便断言路由转换逻辑。"""
        self.payloads: list[dict[str, Any]] = []

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """流式测试不会使用这个方法。"""
        raise AssertionError("create_response should not be called")

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回 function_call 事件和最终 completed 响应。"""
        self.payloads.append(payload)
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city":"杭州"}',
            },
        }
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_tool",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "get_weather",
                        "arguments": '{"city":"杭州"}',
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
            },
        }


class FailingCreateCodexClient:
    """模拟非流式聚合阶段收到不可解析的上游响应。"""

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """抛出清晰的上游响应错误，复现之前的 JSONDecodeError 路径。"""
        raise CodexUnexpectedResponseError(
            upstream_status_code=200,
            content_type="text/html",
            body="<html>upstream unavailable</html>",
        )

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """非流式测试不会使用这个方法。"""
        raise AssertionError("stream_response should not be called")
        yield {}


class FakeModelCatalog:
    """为模型发现路由提供可控目录快照。"""

    def __init__(self, snapshot: ModelCatalogSnapshot) -> None:
        """记录 refresh 参数，方便断言强制刷新语义。"""
        self.snapshot_value = snapshot
        self.refreshes: list[bool] = []

    async def snapshot(self, *, refresh: bool = False) -> ModelCatalogSnapshot:
        """返回固定快照，不访问真实 Codex CLI。"""
        self.refreshes.append(refresh)
        return self.snapshot_value


def model_snapshot(*models: str, default_model: str | None = None) -> ModelCatalogSnapshot:
    """构造模型发现测试用快照。"""
    entries = tuple(
        ModelCatalogEntry(
            id=model,
            display_name=model,
            default_reasoning_effort="medium",
            supported_reasoning_efforts=("low", "medium", "high"),
            order=index,
        )
        for index, model in enumerate(models)
    )
    return ModelCatalogSnapshot(
        models=entries,
        effective_default_model=default_model or (models[0] if models else ""),
        source="cli",
        cache_state="fresh",
    )


def make_test_config(tmp_path: Path, api_key: str | None = None) -> AppConfig:
    """构造测试配置，确保日志写入临时目录。"""
    return AppConfig(
        project_root=tmp_path,
        server=ServerConfig(host="127.0.0.1", port=1219),
        api=ApiConfig(local_api_key=api_key),
        codex=CodexConfig(default_model="gpt-5.5"),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
    )


def make_standard_test_config(tmp_path: Path) -> AppConfig:
    """构造默认关闭快速模式的测试配置。"""
    return AppConfig(
        project_root=tmp_path,
        server=ServerConfig(host="127.0.0.1", port=1219),
        api=ApiConfig(local_api_key=None),
        codex=CodexConfig(default_model="gpt-5.5", fast_mode=False),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
    )


def write_oauth_file(path: Path, *, access: str, refresh: str, expires: int) -> None:
    """写入测试用 OAuth 文件，避免依赖真实本机登录。"""
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "account_id": "acct_test",
                },
                "expires": expires,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_models_route_returns_openai_list_shape(tmp_path: Path) -> None:
    """验证 /v1/models 返回 OpenAI list 结构。"""
    # 使用 fake client 创建应用，避免真实 Codex 依赖。
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=FakeCodexClient(),
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.6-sol", "gpt-5.6-mini")),
    )

    # 通过 ASGITransport 直接调用应用，不启动真实端口。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models")

    # 模型列表来自动态目录，而不是静态 available_models。
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [item["id"] for item in body["data"]] == ["gpt-5.6-sol", "gpt-5.6-mini"]
    assert body["data"][0]["object"] == "model"


@pytest.mark.asyncio
async def test_admin_models_route_returns_catalog_and_honors_refresh(tmp_path: Path) -> None:
    """验证 /admin/models 返回动态目录，并透传 refresh=true。"""
    catalog = FakeModelCatalog(model_snapshot("gpt-5.6-sol", default_model="gpt-5.6-sol"))
    app = create_app(config=make_test_config(tmp_path, api_key="local-secret"), codex_client=FakeCodexClient(), model_catalog=catalog)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/admin/models")
        allowed = await client.get("/admin/models?refresh=true", headers={"Authorization": "Bearer local-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    body = allowed.json()
    assert catalog.refreshes == [True]
    assert body["effective_default_model"] == "gpt-5.6-sol"
    assert body["cache_state"] == "fresh"
    assert body["models"][0]["id"] == "gpt-5.6-sol"
    assert body["models"][0]["supported_reasoning_efforts"] == ["low", "medium", "high"]


@pytest.mark.asyncio
async def test_model_discovery_routes_return_503_when_catalog_empty(tmp_path: Path) -> None:
    """验证所有模型发现接口在目录为空时返回 503。"""
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=FakeCodexClient(),
        model_catalog=FakeModelCatalog(model_snapshot()),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_response = await client.get("/admin/models")
        openai_response = await client.get("/v1/models")
        anthropic_response = await client.get("/anthropic/v1/models")

    assert admin_response.status_code == 503
    assert openai_response.status_code == 503
    assert anthropic_response.status_code == 503


@pytest.mark.asyncio
async def test_chat_completions_route_returns_openai_completion_and_logs_usage(tmp_path: Path) -> None:
    """验证 /v1/chat/completions 非流式响应和 usage 日志。"""
    # fake client 固定返回文本和 usage，便于断言路由行为。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    # 发起 OpenAI Chat Completions 风格请求。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    # 响应应为 ChatCompletion 结构。
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["usage"]["total_tokens"] == 15

    # 转给 Codex 的 payload 使用 Responses input 结构。
    assert fake_client.payloads[0]["input"][0]["content"][0]["text"] == "hello"
    assert fake_client.payloads[0]["service_tier"] == "priority"

    # 成功返回后必须写入 codex-usage 可导入的 JSONL。
    log_lines = (tmp_path / ".codex-usage" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["usage"]["total"] == 15


@pytest.mark.asyncio
async def test_anthropic_messages_route_returns_message_and_logs_usage(tmp_path: Path) -> None:
    """验证 /anthropic/messages 非流式响应兼容 Anthropic Messages。"""
    # Anthropic SDK 通常使用 x-api-key，服务应复用本地访问密钥校验。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path, api_key="local-secret"), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/messages",
            headers={"x-api-key": "local-secret", "anthropic-version": "2023-06-01"},
            json={
                "model": "gpt-5.5",
                "system": "保持简洁",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert fake_client.payloads[0]["input"][0]["role"] == "system"
    assert fake_client.payloads[0]["input"][1]["content"][0]["text"] == "hello"
    assert "max_tokens" not in fake_client.payloads[0]

    log_lines = (tmp_path / ".codex-usage" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log_lines[0])["usage"]["total"] == 15


@pytest.mark.asyncio
async def test_anthropic_messages_route_maps_discovery_alias_to_real_model(tmp_path: Path) -> None:
    """验证 Anthropic 发现别名会映射回真实 Codex 模型。"""
    # 网关发现别名必须通过 Claude 的 Anthropic 模型过滤，但发给 Codex backend 的模型名仍必须是原始值。
    fake_client = FakeCodexClient()
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=fake_client,
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.6-sol")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/messages",
            json={
                "model": "claude-sonnet-5-6-sol",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert fake_client.payloads[0]["model"] == "gpt-5.6-sol"
    assert response.json()["model"] == "claude-sonnet-5-6-sol"


@pytest.mark.asyncio
async def test_anthropic_v1_messages_route_matches_gateway_inference_path(tmp_path: Path) -> None:
    """验证 Claude-3p 网关硬拼的 /v1/messages 推理路径可用。"""
    # Claude-3p 使用 base URL + /v1/messages，因此 /anthropic base URL 需要这个兼容入口。
    fake_client = FakeCodexClient()
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=fake_client,
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.6-sol")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-sonnet-5-6-sol",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert fake_client.payloads[0]["model"] == "gpt-5.6-sol"
    assert response.json()["model"] == "claude-sonnet-5-6-sol"


@pytest.mark.asyncio
async def test_anthropic_messages_route_maps_legacy_and_tier_aliases_to_real_model(tmp_path: Path) -> None:
    """验证旧发现别名和裸 tier 别名也会映射回真实 Codex 模型。"""
    # 兼容用户已经保存过的旧模型名，同时支持 Claude 可能直接提交的 sonnet tier。
    fake_client = FakeCodexClient()
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=fake_client,
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.6-sol")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for model in ("claude-gpt-5-6-sol", "sonnet"):
            response = await client.post(
                "/anthropic/messages",
                json={
                    "model": model,
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

            assert response.status_code == 200

    assert [payload["model"] for payload in fake_client.payloads] == ["gpt-5.6-sol", "gpt-5.6-sol"]


@pytest.mark.asyncio
async def test_anthropic_messages_route_forwards_tools_and_images(tmp_path: Path) -> None:
    """验证 /anthropic/messages 会转发 Anthropic 工具定义和图片输入。"""
    # tools 与 image 都应转换成 Codex/Responses 风格字段，不能静默丢弃。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/messages",
            json={
                "model": "gpt-5.5",
                "max_tokens": 128,
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "查询天气",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
                            },
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = fake_client.payloads[0]
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["parameters"]["required"] == ["city"]
    assert payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc123",
    }


@pytest.mark.asyncio
async def test_anthropic_messages_route_streams_named_events(tmp_path: Path) -> None:
    """验证 /anthropic/messages 流式响应兼容 Anthropic SSE。"""
    # fake client 会输出 hel + lo，路由应包装成 Anthropic 命名事件。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/messages",
            json={
                "model": "gpt-5.5",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    text = response.text
    assert "event: message_start" in text
    assert "event: content_block_start" in text
    assert 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}' in text
    assert "event: message_delta" in text
    assert "event: message_stop" in text


@pytest.mark.asyncio
async def test_anthropic_messages_route_streams_tool_use_events(tmp_path: Path) -> None:
    """验证 /anthropic/messages 流式工具调用会输出 Anthropic tool_use 块。"""
    # 上游 function_call 应转成 content_block_start + input_json_delta + content_block_stop。
    fake_client = ToolStreamCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/anthropic/messages",
            json={
                "model": "gpt-5.5",
                "stream": True,
                "max_tokens": 128,
                "tools": [
                    {
                        "name": "get_weather",
                        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                    }
                ],
                "messages": [{"role": "user", "content": "查天气"}],
            },
        )

    assert response.status_code == 200
    text = response.text
    assert 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_1","name":"get_weather","input":{}}}' in text
    assert 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":\\"杭州\\"}"}}' in text
    assert 'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":3}}' in text


@pytest.mark.asyncio
async def test_anthropic_model_discovery_route_returns_model_list(tmp_path: Path) -> None:
    """验证 /anthropic/v1/models 兼容会硬拼 /v1/models 的网关探测。"""
    # Claude-3p 会过滤包含 gpt/codex 的模型名，因此发现别名必须像 Anthropic tier 模型。
    app = create_app(
        config=make_test_config(tmp_path, api_key="local-secret"),
        codex_client=FakeCodexClient(),
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.6-sol", "codex-5.6-sol")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/anthropic/v1/models", headers={"x-api-key": "local-secret"})

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body).lower()
    assert body["has_more"] is False
    assert "gpt" not in serialized
    assert "codex" not in serialized
    assert body["first_id"] == "claude-sonnet-5-6-sol"
    assert body["last_id"] == "claude-sonnet-5-6-sol-2"
    assert body["data"][0]["id"] == "claude-sonnet-5-6-sol"
    assert body["data"][0]["type"] == "model"
    assert body["data"][0]["display_name"] == "claude-sonnet-5-6-sol"
    assert body["data"][0]["anthropic_family_tier"] == "sonnet"
    assert body["data"][0]["is_family_default"] is True
    assert body["data"][0]["created_at"].endswith("Z")


@pytest.mark.asyncio
async def test_v2_messages_route_is_removed(tmp_path: Path) -> None:
    """验证旧 /v2/messages 入口已移除，避免继续误导客户端配置。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=FakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v2/messages",
            json={"model": "gpt-5.5", "max_tokens": 128, "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_chat_completions_does_not_forward_unsupported_openai_compat_fields(tmp_path: Path) -> None:
    """验证常见 OpenAI SDK 参数不会原样透传给 Codex backend。"""
    # fake client 记录 payload，便于确认兼容参数被本地服务消化掉。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    # 这些参数经常由 OpenAI-compatible 客户端带上，但 Codex OAuth backend 不支持它们。
    unsupported_fields = {
        "temperature": 0.2,
        "top_p": 0.8,
        "max_output_tokens": 128,
        "max_tokens": 128,
        "max_completion_tokens": 128,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "logit_bias": {},
        "logprobs": False,
        "top_logprobs": 0,
        "response_format": {"type": "text"},
        "seed": 1234,
        "stop": ["END"],
        "n": 1,
        "user": "local-user",
        "metadata": {"trace": "abc"},
        "tools": [{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    # 请求应仍然成功，服务只把 Codex backend 真正支持的字段发给上游。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                **unsupported_fields,
            },
        )

    # 本地 API 仍应兼容接收这些字段，但发给 Codex 的 payload 不能包含上游不支持的名字。
    assert response.status_code == 200
    for field_name in unsupported_fields:
        assert field_name not in fake_client.payloads[0]


@pytest.mark.asyncio
async def test_chat_completions_fast_mode_request_overrides_default(tmp_path: Path) -> None:
    """验证请求体 fast_mode 可以临时覆盖配置默认值。"""
    # 默认配置会开启 fast，请求显式 false 时应走标准模式。
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "fast_mode": False,
            },
        )

    # fast_mode=false 不向 Codex backend 发送 service_tier，等价于 CLI 的 /fast off。
    assert response.status_code == 200
    assert "service_tier" not in fake_client.payloads[0]


@pytest.mark.asyncio
async def test_responses_service_tier_request_can_enable_fast_mode(tmp_path: Path) -> None:
    """验证请求体 service_tier=fast 可以临时开启快速模式。"""
    # 配置默认关闭 fast，请求显式 service_tier=fast 时应映射到 Codex backend 支持的 tier。
    fake_client = FakeCodexClient()
    app = create_app(config=make_standard_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello", "service_tier": "fast"},
        )

    # service_tier=fast 贴近文档语义；上游 OAuth backend 当前实际接受 priority。
    assert response.status_code == 200
    assert fake_client.payloads[0]["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_responses_route_streams_sse_and_logs_final_usage(tmp_path: Path) -> None:
    """验证 /v1/responses 流式响应会转发 SSE 并记录最终 usage。"""
    # fake client 的 stream_response 会返回两个 delta 和一个 completed 事件。
    app = create_app(config=make_test_config(tmp_path), codex_client=FakeCodexClient())

    # 请求 stream=true，期望服务返回 text/event-stream。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello", "stream": True},
        )

    # httpx ASGITransport 会聚合 body，但内容仍是 SSE data 行。
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert 'data: {"type":"response.output_text.delta","delta":"hel"}' in response.text
    assert "data: [DONE]" in response.text

    # completed 事件带真实 usage 时，服务应在流结束前写 usage 日志。
    log_lines = (tmp_path / ".codex-usage" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["usage"]["output"] == 5


@pytest.mark.asyncio
async def test_local_api_key_is_required_when_configured(tmp_path: Path) -> None:
    """验证配置 local_api_key 后本地服务会校验 Authorization。"""
    # 配置本地 API key，模拟只允许受控客户端访问。
    app = create_app(config=make_test_config(tmp_path, api_key="local-secret"), codex_client=FakeCodexClient())

    # 缺少 Authorization 时应拒绝请求。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/v1/models")
        allowed = await client.get("/v1/models", headers={"Authorization": "Bearer local-secret"})

    # 未授权返回 401，正确 Bearer token 可以访问。
    assert denied.status_code == 401
    assert denied.json()["error"]["type"] == "authentication_error"
    assert denied.json()["error"]["message"] == "Unauthorized"
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_admin_auth_reload_imports_latest_codex_login(tmp_path: Path) -> None:
    """验证管理接口可以用 Codex CLI/App 的新登录覆盖本服务旧缓存。"""
    local_auth = tmp_path / "service-auth.json"
    import_auth = tmp_path / "codex-auth.json"
    write_oauth_file(local_auth, access="old-access", refresh="old-refresh", expires=4_102_444_800_000)
    write_oauth_file(import_auth, access="fresh-access", refresh="fresh-refresh", expires=4_102_444_800_000)
    config = AppConfig(
        project_root=tmp_path,
        server=ServerConfig(host="127.0.0.1", port=1219),
        codex=CodexConfig(default_model="gpt-5.5"),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
        auth=AuthConfig(auth_path=local_auth, import_auth_path=import_auth),
    )
    app = create_app(config=config, codex_client=FakeCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/auth/reload")

    assert response.status_code == 200
    assert response.json()["oauth"] == {"available": True, "expired": False, "reloaded": True}
    saved = json.loads(local_auth.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "fresh-access"


@pytest.mark.asyncio
async def test_chat_stream_returns_sse_error_when_codex_rejects_request(tmp_path: Path) -> None:
    """验证上游流式错误会变成 SSE 错误事件，而不是断开连接。"""
    # 这个 fake client 复现截图中的上游 400 错误。
    app = create_app(config=make_test_config(tmp_path), codex_client=FailingStreamCodexClient())

    # 调用 stream=true，服务应保持 SSE 协议完整返回。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        requests = await client.get("/admin/requests")

    # 响应不能因为生成器异常让客户端看到 network error。
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"error"' in response.text
    assert "newer version of Codex" in response.text
    assert "data: [DONE]" in response.text

    # 请求日志应记录失败状态，方便 UI 的请求日志页定位。
    item = requests.json()["items"][0]
    assert item["path"] == "/v1/chat/completions"
    assert item["status_code"] == 400
    assert "newer version of Codex" in item["error"]


@pytest.mark.asyncio
async def test_chat_non_stream_returns_json_error_when_codex_response_is_unexpected(tmp_path: Path) -> None:
    """验证非流式上游异常返回 JSON 错误，而不是 FastAPI 500 页面。"""
    # 这个 fake client 复现真实调试中 HTML/文本响应触发 JSONDecodeError 的情况。
    app = create_app(config=make_test_config(tmp_path), codex_client=FailingCreateCodexClient())

    # 非 stream 请求应拿到明确 JSON 错误，客户端和 UI 都能展示 detail。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )
        requests = await client.get("/admin/requests")

    # 服务端用 502 表示上游响应异常，并保留响应片段用于排查。
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["type"] == "api_error"
    assert "Unexpected Codex response" in response.json()["error"]["message"]
    assert "upstream unavailable" in response.json()["error"]["message"]

    # 请求日志同样记录失败，方便控制台日志页查看。
    item = requests.json()["items"][0]
    assert item["path"] == "/v1/chat/completions"
    assert item["status_code"] == 502
    assert "upstream unavailable" in item["error"]
