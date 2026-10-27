import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from codex_api_service.app import create_app
from codex_api_service.auth import CodexAuth, CodexCredentials
from codex_api_service.codex_client import CodexClient, CodexHTTPStatusError, CodexUnexpectedResponseError
from codex_api_service.config import (
    AppConfig,
    ApiConfig,
    AuthConfig,
    CodexConfig,
    ModelRequestDefaults,
    ServerConfig,
    UsageConfig,
)
from codex_api_service.model_catalog import ModelCatalogEntry, ModelCatalogSnapshot
from codex_api_service.oauth_login import OAuthLoginSession
from codex_api_service.oauth_scheduler import BoundAccountUnavailable


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

    def request_account_metadata(self) -> dict[str, str]:
        """返回测试请求使用的安全账户元数据。"""
        return {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
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

    def request_account_metadata(self) -> dict[str, str]:
        """返回流式失败前已经选中的测试账户。"""
        return {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
        }


class UnavailableBoundAccountClient:
    """模拟 previous_response_id 所属账号已停用。"""

    async def create_response(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """非流式请求返回稳定的绑定账号不可用错误。"""
        raise BoundAccountUnavailable("previous_response_id 所属 OAuth 账户已停用，请不带 previous_response_id 发起新会话")

    async def stream_response(self, _payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """流式测试保持接口完整。"""
        raise BoundAccountUnavailable("previous_response_id 所属 OAuth 账户已停用，请不带 previous_response_id 发起新会话")
        yield {}


class ToolStreamCodexClient:
    """模拟 Codex backend 流式返回工具调用。"""

    def __init__(self) -> None:
        """记录收到的 payload，方便断言路由转换逻辑。"""
        self.payloads: list[dict[str, Any]] = []

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """返回固定 function_call，供非流式工具响应测试复用。"""
        self.payloads.append(payload)
        return {
            "id": "resp_tool",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"杭州"}',
                    "status": "completed",
                }
            ],
            "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
        }

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


class ToolEventOnlyStreamCodexClient(ToolStreamCodexClient):
    """模拟 completed 响应不再重复携带已发送工具项的情况。"""

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """先发送完整工具事件，再发送不含 output 的完成事件。"""
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
                "output": [],
                "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
            },
        }


class ResponsesToolStreamCodexClient(ToolStreamCodexClient):
    """模拟包含调用开始、参数增量和完成事件的 Responses 工具流。"""

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """复用非流式工具响应，生成可供客户端按调用标识聚合的 SSE 事件。"""
        response = await self.create_response(payload)
        item = response["output"][0]
        yield {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": "", "status": "in_progress"},
        }
        for delta in ['{"city":', '"杭州"}']:
            yield {
                "type": "response.function_call_arguments.delta",
                "item_id": item["id"],
                "output_index": 0,
                "delta": delta,
            }
        yield {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": 0,
            "arguments": item["arguments"],
        }
        yield {"type": "response.output_item.done", "output_index": 0, "item": item}
        yield {"type": "response.completed", "response": response}


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

    def request_account_metadata(self) -> dict[str, str]:
        """返回非流式失败前已经选中的测试账户。"""
        return {
            "account_key": "account-a",
            "account_alias": "owner@example.com",
        }


class OverloadedCreateCodexClient(FailingCreateCodexClient):
    """模拟非流式上游返回标准 503 overload 错误。"""

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """抛出包含 OpenAI type/code 的上游错误。"""
        raise CodexHTTPStatusError(
            503,
            json.dumps(
                {
                    "error": {
                        "message": "Our servers are currently overloaded. Please try again later.",
                        "type": "service_unavailable_error",
                        "code": "server_is_overloaded",
                    }
                }
            ),
        )


class SemanticErrorStreamCodexClient(FailingStreamCodexClient):
    """模拟 HTTP 200 SSE 中出现 error 语义事件。"""

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回标准 Responses error 事件。"""
        yield {
            "type": "error",
            "error": {
                "message": "Our servers are currently overloaded. Please try again later.",
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
            },
        }


class IncompleteStreamCodexClient(FailingStreamCodexClient):
    """模拟 SSE 只返回部分增量后提前结束。"""

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """返回没有 response.completed 的事件序列。"""
        yield {"type": "response.output_text.delta", "delta": "partial"}


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
    path.parent.mkdir(parents=True, exist_ok=True)
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
async def test_all_api_routes_log_direct_client_ip(tmp_path: Path) -> None:
    """验证模型发现和三类推理接口都记录 TCP 直连客户端 IP。"""
    app = create_app(
        config=make_test_config(tmp_path),
        codex_client=FakeCodexClient(),
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.5")),
    )
    transport = ASGITransport(app=app, client=("203.0.113.8", 54321))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/v1/models")
        await client.get("/anthropic/v1/models")
        await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )
        await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello"},
        )
        await client.post(
            "/anthropic/v1/messages",
            json={
                "model": "gpt-5.5",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        request_logs = await client.get("/admin/requests")

    logs_by_path = {item["path"]: item for item in request_logs.json()["items"]}
    expected_paths = {
        "/v1/models",
        "/anthropic/v1/models",
        "/v1/chat/completions",
        "/v1/responses",
        "/anthropic/v1/messages",
    }
    assert expected_paths.issubset(logs_by_path)
    assert {logs_by_path[path]["client_ip"] for path in expected_paths} == {"203.0.113.8"}


@pytest.mark.asyncio
async def test_stream_request_log_records_direct_client_ip(tmp_path: Path) -> None:
    """验证流式请求完成后仍记录同一连接的 IPv6 地址。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=FakeCodexClient())
    transport = ASGITransport(app=app, client=("2001:db8::8", 54321))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello", "stream": True},
        )
        request_logs = await client.get("/admin/requests")

    assert response.status_code == 200
    assert request_logs.json()["items"][0]["client_ip"] == "2001:db8::8"


@pytest.mark.asyncio
async def test_failed_request_log_records_direct_client_ip(tmp_path: Path) -> None:
    """验证上游失败日志也保留调用方直连 IP。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=FailingCreateCodexClient())
    transport = ASGITransport(app=app, client=("198.51.100.24", 54321))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )
        request_logs = await client.get("/admin/requests")

    assert response.status_code == 502
    assert request_logs.json()["items"][0]["client_ip"] == "198.51.100.24"


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
    assert "service_tier" not in fake_client.payloads[0]

    # 成功返回后必须写入 codex-usage 可导入的 JSONL。
    log_lines = (tmp_path / ".codex-usage" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["usage"]["total"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("image_only", [False, True])
async def test_chat_completions_forwards_images_in_both_stream_modes(tmp_path: Path, stream: bool, image_only: bool) -> None:
    """验证流式和非流式均转发多图、纯图片及图文混排，并且日志不记录图片内容。"""
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.com/private-image.png?token=demo", "detail": "high"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8=", "detail": "auto"}},
    ]
    expected = [
        {"type": "input_image", "image_url": "https://example.com/private-image.png?token=demo", "detail": "high"},
        {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8=", "detail": "auto"},
    ]
    if not image_only:
        content.insert(1, {"type": "text", "text": "请对比图片"})
        expected.insert(1, {"type": "input_text", "text": "请对比图片"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json={
            "model": "gpt-5.5", "stream": stream, "messages": [{"role": "user", "content": content}],
        })

    assert response.status_code == 200
    assert len(fake_client.payloads) == 1
    assert fake_client.payloads[0]["input"] == [{"role": "user", "content": expected}]
    if stream:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "data: [DONE]" in response.text
    else:
        assert response.json()["choices"][0]["message"]["content"] == "hello"
    logs = (tmp_path / "logs" / "requests.jsonl").read_text(encoding="utf-8")
    assert "private-image" not in logs
    assert "aGVsbG8=" not in logs


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("image_url", [
    None, "https://example.com/private-image.png", [], {}, {"url": None}, {"url": 123},
    {"url": ""}, {"url": "   "},
    {"url": "https://example.com/private-image.png", "detail": "invalid"},
    {"url": "data:image/png;base64,aGVsbG8=", "detail": []},
    {"url": "https://example.com/private-image.png", "detail": None},
])
async def test_chat_completions_rejects_invalid_images_before_upstream(tmp_path: Path, stream: bool, image_url: Any) -> None:
    """验证错误图片参数在建立流之前返回 400，不调用上游，也不回显图片地址。"""
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)
    image_part = {"type": "image_url", "image_url": image_url}
    if image_url is None:
        # 缺失整个 image_url 字段也必须被明确拒绝。
        image_part.pop("image_url")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json={
            "model": "gpt-5.5", "stream": stream, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "不要丢弃图片后只发送文字"}, image_part,
            ]}],
        })

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["code"] == "invalid_image"
    assert "image_url" in response.json()["error"]["message"]
    assert "private-image" not in response.text
    assert "aGVsbG8=" not in response.text
    assert fake_client.payloads == []


@pytest.mark.asyncio
async def test_chat_completions_route_returns_non_stream_tool_calls(tmp_path: Path) -> None:
    """验证非流式 Chat 路由返回标准 OpenAI tool_calls。"""
    fake_client = ToolStreamCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "查天气"}]},
        )

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"杭州"}'},
            }
        ],
    }
    assert choice["finish_reason"] == "tool_calls"


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
async def test_chat_completions_streams_openai_tool_call_chunks(tmp_path: Path) -> None:
    """验证 Chat 流式响应会输出 Claudish 可解析的 tool_calls。"""
    fake_client = ToolEventOnlyStreamCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "stream": True,
                "messages": [{"role": "user", "content": "查天气"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    tool_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0]["delta"].get("tool_calls")
    ]
    assert tool_chunks[0]["choices"][0]["delta"]["tool_calls"] == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"杭州"}'},
        }
    ]
    finish_reasons = [chunk["choices"][0]["finish_reason"] for chunk in chunks if chunk.get("choices")]
    assert "tool_calls" in finish_reasons
    assert response.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_chat_completions_forwards_openai_tools_to_codex_payload(tmp_path: Path) -> None:
    """验证 Chat 工具配置会转换并进入 Codex Responses payload。"""
    fake_client = FakeCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "读取文件"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "description": "读取文件",
                            "parameters": {
                                "type": "object",
                                "properties": {"file_path": {"type": "string"}},
                                "required": ["file_path"],
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "Read"}},
                "parallel_tool_calls": False,
            },
        )

    assert response.status_code == 200
    payload = fake_client.payloads[0]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "Read",
            "description": "读取文件",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "Read"}
    assert payload["parallel_tool_calls"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("chat_style", [False, True])
async def test_responses_forwards_function_tools_and_returns_calls(
    tmp_path: Path, stream: bool, chat_style: bool
) -> None:
    """验证两种函数格式均能传入上游，并在流式和非流式返回中保留完整调用。"""
    fake_client = ResponsesToolStreamCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)
    function = {
        "name": "get_weather",
        "description": "查询天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    tool = {"type": "function", "function": function} if chat_style else {"type": "function", **function}
    choice = (
        {"type": "function", "function": {"name": "get_weather"}}
        if chat_style else {"type": "function", "name": "get_weather"}
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": "杭州天气如何？",
                "tools": [tool],
                "tool_choice": choice,
                "parallel_tool_calls": False,
                "stream": stream,
            },
        )
        request_logs = await client.get("/admin/requests")

    assert response.status_code == 200
    assert len(fake_client.payloads) == 1
    payload = fake_client.payloads[0]
    assert payload["tools"] == [{"type": "function", **function}]
    assert payload["tool_choice"] == {"type": "function", "name": "get_weather"}
    assert payload["parallel_tool_calls"] is False
    expected_call = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"杭州"}',
        "status": "completed",
    }
    if stream:
        assert "text/event-stream" in response.headers["content-type"]
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines() if line.startswith("data: {")
        ]
        assert [event["type"] for event in events] == [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert events[0]["item"] == {**expected_call, "arguments": "", "status": "in_progress"}
        assert events[1]["item_id"] == events[2]["item_id"] == "fc_1"
        assert events[1]["output_index"] == events[2]["output_index"] == 0
        assert events[1]["delta"] + events[2]["delta"] == expected_call["arguments"]
        assert events[3]["arguments"] == expected_call["arguments"]
        assert events[4]["item"] == expected_call
        assert events[5]["response"]["output"] == [expected_call]
        assert response.text.rstrip().endswith("data: [DONE]")
    else:
        assert response.json()["output"] == [expected_call]
        assert response.json()["output_text"] == ""
        assert response.json()["usage"] == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    assert request_logs.json()["items"][0]["status_code"] == 200
    assert request_logs.json()["items"][0]["request_id"] == "resp_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions"])
async def test_non_stream_tool_calls_survive_real_sse_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """回放完成事件为空的工具流，验证真实客户端聚合与接口转换不会丢失调用。"""
    event_client = ToolEventOnlyStreamCodexClient()
    real_client = CodexClient(auth=CodexAuth(auth_path=tmp_path / "auth.json"), config=CodexConfig())
    # 仅替换网络事件来源，让 create_response 使用生产环境的聚合逻辑。
    monkeypatch.setattr(real_client, "stream_response", event_client.stream_response)
    app = create_app(config=make_test_config(tmp_path), codex_client=real_client)
    body = {
        "model": "gpt-5.6-terra",
        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "get_weather"},
    }
    if path == "/v1/responses":
        body["input"] = "查天气"
    else:
        body["messages"] = [{"role": "user", "content": "查天气"}]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)

    assert response.status_code == 200
    if path == "/v1/responses":
        assert response.json()["output"] == [{
            "type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": '{"city":"杭州"}'
        }]
        assert response.json()["output_text"] == ""
    else:
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"] == [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"杭州"}'},
        }]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_previous_response", [False, True])
async def test_responses_preserves_tool_result_round_trip(tmp_path: Path, use_previous_response: bool) -> None:
    """验证返回的调用和函数执行结果可作为下一轮 input 原样传给上游。"""
    fake_client = ToolStreamCodexClient()
    app = create_app(config=make_test_config(tmp_path), codex_client=fake_client)
    tools = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/responses", json={"input": "查天气", "tools": tools})
        assert first.status_code == 200
        call = first.json()["output"][0]
        assert call["type"] == "function_call"
        tool_result = {"type": "function_call_output", "call_id": call["call_id"], "output": "晴，25°C"}
        # 响应链模式只补工具结果；手动历史模式回传原消息、调用和结果。
        history = [tool_result] if use_previous_response else [{"role": "user", "content": "查天气"}, call, tool_result]
        continuation = {"previous_response_id": first.json()["id"]} if use_previous_response else {}
        second = await client.post(
            "/v1/responses",
            json={
                "input": history,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": True,
                **continuation,
            },
        )

    assert second.status_code == 200
    if use_previous_response:
        assert fake_client.payloads[1]["input"] == [tool_result]
        assert fake_client.payloads[1]["previous_response_id"] == "resp_tool"
    else:
        assert fake_client.payloads[1]["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "查天气"}]}, call, tool_result
        ]
        assert "previous_response_id" not in fake_client.payloads[1]
    assert fake_client.payloads[1]["tools"] == tools
    assert fake_client.payloads[1]["tool_choice"] == "auto"
    assert fake_client.payloads[1]["parallel_tool_calls"] is True


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
async def test_routes_apply_real_model_defaults_and_explicit_overrides(tmp_path: Path) -> None:
    """验证 Chat、Responses 和 Anthropic 共享真实模型请求配置。"""
    config = AppConfig(
        project_root=tmp_path,
        codex=CodexConfig(
            default_model="gpt-5.4",
            model_request_defaults={
                "gpt-5.4": ModelRequestDefaults(reasoning_effort="high", fast_mode=True)
            },
        ),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
    )
    fake_client = FakeCodexClient()
    app = create_app(
        config=config,
        codex_client=fake_client,
        model_catalog=FakeModelCatalog(model_snapshot("gpt-5.4")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chat = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "chat"}]},
        )
        responses = await client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "input": "response",
                "reasoning": {"summary": "detailed"},
                "reasoning_effort": "low",
                "fast_mode": False,
            },
        )
        anthropic = await client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-sonnet-5-4",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "anthropic"}],
            },
        )
        request_logs = await client.get("/admin/requests")

    assert chat.status_code == 200
    assert responses.status_code == 200
    assert anthropic.status_code == 200
    assert fake_client.payloads[0]["model"] == "gpt-5.4"
    assert fake_client.payloads[0]["reasoning"]["effort"] == "high"
    assert fake_client.payloads[0]["service_tier"] == "priority"
    assert fake_client.payloads[1]["reasoning"] == {"effort": "low", "summary": "detailed"}
    assert "service_tier" not in fake_client.payloads[1]
    assert fake_client.payloads[2]["model"] == "gpt-5.4"
    assert fake_client.payloads[2]["reasoning"]["effort"] == "high"
    assert anthropic.json()["model"] == "claude-sonnet-5-4"
    logs_by_path = {item["path"]: item for item in request_logs.json()["items"]}
    assert logs_by_path["/v1/chat/completions"]["stream"] is False
    assert logs_by_path["/v1/chat/completions"]["reasoning_effort"] == "high"
    assert logs_by_path["/v1/chat/completions"]["fast_mode"] is True
    assert logs_by_path["/v1/chat/completions"]["service_tier"] == "priority"
    assert logs_by_path["/v1/chat/completions"]["account_key"] == "account-a"
    assert logs_by_path["/v1/chat/completions"]["account_alias"] == "owner@example.com"
    assert logs_by_path["/v1/responses"]["stream"] is False
    assert logs_by_path["/v1/responses"]["reasoning_effort"] == "low"
    assert logs_by_path["/v1/responses"]["fast_mode"] is False
    assert logs_by_path["/v1/responses"]["service_tier"] is None
    assert logs_by_path["/v1/responses"]["account_key"] == "account-a"
    assert logs_by_path["/v1/responses"]["account_alias"] == "owner@example.com"
    assert logs_by_path["/anthropic/v1/messages"]["reasoning_effort"] == "high"
    assert logs_by_path["/anthropic/v1/messages"]["fast_mode"] is True
    assert logs_by_path["/anthropic/v1/messages"]["account_key"] == "account-a"
    assert logs_by_path["/anthropic/v1/messages"]["account_alias"] == "owner@example.com"


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
async def test_responses_forwards_previous_response_id_for_account_stickiness(tmp_path: Path) -> None:
    """验证响应链标识会进入账号池和上游 payload。"""
    fake_client = FakeCodexClient()
    app = create_app(config=make_standard_test_config(tmp_path), codex_client=fake_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "continue", "previous_response_id": "resp_previous"},
        )

    assert response.status_code == 200
    assert fake_client.payloads[0]["previous_response_id"] == "resp_previous"


@pytest.mark.asyncio
async def test_unavailable_previous_response_account_returns_409_and_is_logged(tmp_path: Path) -> None:
    """验证绑定账号停用会返回稳定错误，并出现在请求日志中。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=UnavailableBoundAccountClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "continue", "previous_response_id": "resp_old"},
        )
        logs = await client.get("/admin/requests")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "previous_response_account_unavailable"
    assert "请不带 previous_response_id 发起新会话" in response.json()["error"]["message"]
    assert logs.json()["items"][0]["status_code"] == 409
    assert "previous_response_account_unavailable" in logs.json()["items"][0]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_empty_oauth_pool_returns_explicit_503_and_logs_failure(tmp_path: Path, stream: bool) -> None:
    """验证无可用账号通过实际账号池返回稳定错误，不误报本地 API key 错误或未知 500。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    # 测试使用空的隔离账号库，禁止导入开发机的全局登录。
    pool._initialized = True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/responses", json={"input": "hello", "stream": stream})
            logs = (await client.get("/admin/requests")).json()["items"]
        if stream:
            assert '"code":"oauth_accounts_unavailable"' in response.text
            assert '"type":"service_unavailable_error"' in response.text
            assert "data: [DONE]" in response.text
        else:
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "oauth_accounts_unavailable"
            assert response.json()["error"]["type"] == "service_unavailable_error"
        assert logs[0]["status_code"] == 503
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_admin_oauth_dispatch_updates_mode_and_snapshot(tmp_path: Path) -> None:
    """验证管理接口原子切换单账户模式并返回负载统计字段。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    account_a = await pool.store.import_credentials(
        CodexCredentials("access-a", "refresh-a", 4_102_444_800_000, "acct-a"),
        source="web",
    )
    account_b = await pool.store.import_credentials(
        CodexCredentials("access-b", "refresh-b", 4_102_444_800_000, "acct-b"),
        source="web",
    )
    pool._rebuild_runtimes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/admin/oauth/dispatch",
            json={"mode": "single", "singleAccountKey": account_b.key},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["dispatchMode"] == "single"
    assert body["singleAccountKey"] == account_b.key
    assert body["waitingQueueSize"] == 0
    assert "globalMaxConcurrency" in body
    enabled = {item["key"] for item in body["accounts"] if item["enabled"]}
    assert enabled == {account_b.key}
    assert account_a.key not in enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplier", [0.000001, 0.3, 1.25, 7.5, 1_000_000])
async def test_admin_oauth_custom_weight_updates_snapshot_and_scheduler(tmp_path: Path, multiplier: float) -> None:
    """验证管理接口保存自定义倍率，立即同步真实调度候选及返回值。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    record = await pool.store.import_credentials(
        CodexCredentials("access", "refresh", 4_102_444_800_000, "weighted-account"), source="web-oauth",
    )
    pool._rebuild_runtimes()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(f"/admin/oauth/accounts/{record.key}", json={"weightMultiplier": multiplier})
            snapshot = await client.get("/admin/oauth/accounts")
        assert response.status_code == 200
        assert response.json()["accounts"][0]["weightMultiplier"] == multiplier
        assert snapshot.json()["accounts"][0]["weightMultiplier"] == multiplier
        assert pool.scheduler._accounts[record.key].weight_multiplier == multiplier
        assert pool.store.get(record.key).weight_multiplier == multiplier
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_value", ["null", "true", '"2"', "0", "-1", "0.0000001", "1000001", "NaN", "Infinity"])
async def test_admin_oauth_rejects_invalid_custom_multiplier(tmp_path: Path, raw_value: str) -> None:
    """验证请求中非法倍率返回明确错误，而不是静默忽略或保存非有限值。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    record = await pool.store.import_credentials(
        CodexCredentials("access", "refresh", 4_102_444_800_000, "weighted-account"), source="web-oauth",
    )
    pool._rebuild_runtimes()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                f"/admin/oauth/accounts/{record.key}", content='{"weightMultiplier":' + raw_value + '}',
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 409
        assert "权重倍率" in response.json()["error"]["message"]
        assert pool.store.get(record.key).weight_multiplier == 1
        assert pool.scheduler._accounts[record.key].weight_multiplier == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_admin_oauth_login_active_and_repeated_start_reuse_session(tmp_path: Path) -> None:
    """验证页面可恢复活动登录，重复启动会接管同一后端会话。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    session = OAuthLoginSession(id="login-active", device_auth=False)
    pool.login.sessions[session.id] = session
    pool.login._active_id = session.id

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            active_response = await client.get("/admin/oauth/login/active")
            repeated_response = await client.post(
                "/admin/oauth/login",
                json={"deviceAuth": True},
            )

        assert active_response.status_code == 200
        assert active_response.json() == {"session": session.snapshot()}
        assert repeated_response.status_code == 200
        assert repeated_response.json()["id"] == session.id
        assert repeated_response.json()["deviceAuth"] is False
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_refresh_enabled_accounts_runs_enabled_accounts_concurrently(tmp_path: Path) -> None:
    """验证手动批量刷新只处理启用账号，并且不会串行等待每个账号。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    account_a = await pool.store.import_credentials(
        CodexCredentials("access-a", "refresh-a", 4_102_444_800_000, "acct-a"),
        source="web",
    )
    account_b = await pool.store.import_credentials(
        CodexCredentials("access-b", "refresh-b", 4_102_444_800_000, "acct-b"),
        source="web",
    )
    account_c = await pool.store.import_credentials(
        CodexCredentials("access-c", "refresh-c", 4_102_444_800_000, "acct-c"),
        source="web",
    )
    await pool.store.update(account_c.key, enabled=False)
    pool._rebuild_runtimes()
    started: list[str] = []
    completed: list[str] = []
    all_started = asyncio.Event()

    async def fake_refresh(account_key: str) -> dict[str, str]:
        """等待两个启用账号均开始，用屏障验证批量刷新为并发执行。"""
        started.append(account_key)
        if len(started) == 2:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.2)
        completed.append(account_key)
        return {"account": account_key}

    pool.refresh_account = fake_refresh

    snapshot = await pool.refresh_enabled_accounts()

    assert set(started) == {account_a.key, account_b.key}
    assert set(completed) == {account_a.key, account_b.key}
    assert account_c.key not in started
    assert {item["key"] for item in snapshot["accounts"]} == {account_a.key, account_b.key, account_c.key}


@pytest.mark.asyncio
async def test_admin_oauth_accounts_refresh_returns_latest_snapshot(tmp_path: Path) -> None:
    """验证管理接口调用批量刷新并直接返回最新账号快照。"""
    app = create_app(config=make_test_config(tmp_path))
    pool = app.state.oauth_account_pool
    pool._initialized = True
    called = 0

    async def fake_refresh_enabled_accounts() -> dict[str, Any]:
        """返回带标记的快照，便于确认路由没有读取旧缓存。"""
        nonlocal called
        called += 1
        snapshot = pool.snapshot()
        snapshot["waitingQueueSize"] = 7
        return snapshot

    pool.refresh_enabled_accounts = fake_refresh_enabled_accounts

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/oauth/accounts/refresh")

    assert response.status_code == 200
    assert called == 1
    assert response.json()["waitingQueueSize"] == 7


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
        request_logs = await client.get("/admin/requests")

    # httpx ASGITransport 会聚合 body，但内容仍是 SSE data 行。
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert 'data: {"type":"response.output_text.delta","delta":"hel"}' in response.text
    assert "data: [DONE]" in response.text

    # completed 事件带真实 usage 时，服务应在流结束前写 usage 日志。
    log_lines = (tmp_path / ".codex-usage" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["usage"]["output"] == 5
    assert request_logs.json()["items"][0]["account_key"] == "account-a"
    assert request_logs.json()["items"][0]["account_alias"] == "owner@example.com"


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
    local_auth = tmp_path / ".codex-oauth" / "single-account-auth.json"
    import_auth = tmp_path / "codex-auth.json"
    write_oauth_file(local_auth, access="old-access", refresh="old-refresh", expires=4_102_444_800_000)
    write_oauth_file(import_auth, access="fresh-access", refresh="fresh-refresh", expires=4_102_444_800_000)
    config = AppConfig(
        project_root=tmp_path,
        server=ServerConfig(host="127.0.0.1", port=1219),
        codex=CodexConfig(default_model="gpt-5.5"),
        usage=UsageConfig(path=tmp_path / ".codex-usage" / "usage.jsonl"),
        auth=AuthConfig(import_auth_path=import_auth, account_store_path=tmp_path / ".codex-oauth"),
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
                "reasoning_effort": "low",
                "service_tier": "fast",
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
    assert item["stream"] is True
    assert item["reasoning_effort"] == "low"
    assert item["fast_mode"] is True
    assert item["service_tier"] == "priority"
    assert item["account_key"] == "account-a"
    assert item["account_alias"] == "owner@example.com"


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
    assert item["stream"] is False
    assert item["reasoning_effort"] == "medium"
    assert item["fast_mode"] is False
    assert item["service_tier"] is None
    assert item["account_key"] == "account-a"
    assert item["account_alias"] == "owner@example.com"


@pytest.mark.asyncio
async def test_responses_non_stream_preserves_upstream_overload_error(tmp_path: Path) -> None:
    """验证 Responses 非流式请求会返回上游 503 的 type 和 code。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=OverloadedCreateCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello"},
        )
        requests = await client.get("/admin/requests")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "message": "Our servers are currently overloaded. Please try again later.",
        "type": "service_unavailable_error",
        "param": None,
        "code": "server_is_overloaded",
    }
    item = requests.json()["items"][0]
    assert item["path"] == "/v1/responses"
    assert item["status_code"] == 503
    assert "server_is_overloaded" in item["error"]


@pytest.mark.asyncio
async def test_responses_stream_returns_sse_error_for_semantic_upstream_failure(tmp_path: Path) -> None:
    """验证 Responses 流式 error 事件会被转换成可读的错误 SSE。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=SemanticErrorStreamCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello", "stream": True},
        )
        requests = await client.get("/admin/requests")

    assert response.status_code == 200
    assert '"type":"service_unavailable_error"' in response.text
    assert '"code":"server_is_overloaded"' in response.text
    assert "Our servers are currently overloaded" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert requests.json()["items"][0]["status_code"] == 503


@pytest.mark.asyncio
async def test_responses_stream_returns_sse_error_when_completed_event_is_missing(tmp_path: Path) -> None:
    """验证 Responses 流提前结束时不会只返回 data DONE 伪装成功。"""
    app = create_app(config=make_test_config(tmp_path), codex_client=IncompleteStreamCodexClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.5", "input": "hello", "stream": True},
        )
        requests = await client.get("/admin/requests")

    assert response.status_code == 200
    assert '"status_code":502' in response.text
    assert "response.completed" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert requests.json()["items"][0]["status_code"] == 502
