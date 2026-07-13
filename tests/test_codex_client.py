import asyncio

import pytest

from codex_api_service.auth import CodexCredentials
from codex_api_service.codex_client import (
    CodexClient,
    CodexUnexpectedResponseError,
    _codex_headers,
    _non_sse_response_event,
    _sse_body_events,
)
from codex_api_service.config import CodexConfig


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_line", "expected_events"),
    [
        ("data: [DONE]", []),
        (
            'data: {"type":"response.completed","response":{"id":"resp_done"}}',
            [{"type": "response.completed", "response": {"id": "resp_done"}}],
        ),
    ],
)
async def test_stream_response_stops_on_protocol_terminal_without_waiting_for_eof(
    monkeypatch: pytest.MonkeyPatch,
    terminal_line: str,
    expected_events: list[dict],
) -> None:
    """验证协议已完成时不再依赖上游主动关闭 TCP 连接。"""

    class StaticAuth:
        """提供固定有效凭据。"""

        def ensure_credentials(self) -> CodexCredentials:
            """返回测试凭据。"""
            return CodexCredentials(access="access", refresh="refresh", expires=4_102_444_800_000)

    class HangingStreamResponse:
        """发送终止行后永久保持连接。"""

        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self) -> "HangingStreamResponse":
            """进入响应上下文。"""
            return self

        async def __aexit__(self, *_args: object) -> None:
            """退出响应上下文。"""
            return None

        async def aiter_lines(self):
            """产出终止信号后模拟不发送 EOF 的上游。"""
            yield terminal_line
            await asyncio.Event().wait()

    class FakeAsyncClient:
        """返回保持连接的测试响应。"""

        received_timeouts: list[object] = []

        def __init__(self, *_args: object, **kwargs: object) -> None:
            """记录客户端读取超时配置。"""
            self.received_timeouts.append(kwargs.get("timeout"))

        async def __aenter__(self) -> "FakeAsyncClient":
            """进入客户端上下文。"""
            return self

        async def __aexit__(self, *_args: object) -> None:
            """退出客户端上下文。"""
            return None

        def stream(self, *_args: object, **_kwargs: object) -> HangingStreamResponse:
            """返回测试流。"""
            return HangingStreamResponse()

    monkeypatch.setattr("codex_api_service.codex_client.httpx.AsyncClient", FakeAsyncClient)
    client = CodexClient(auth=StaticAuth(), config=CodexConfig(responses_url="https://example.test/codex"))

    async def collect_events() -> list[dict]:
        """收集客户端产出的全部事件。"""
        return [event async for event in client.stream_response({"model": "gpt-5.5"})]

    events = await asyncio.wait_for(collect_events(), timeout=0.1)

    assert events == expected_events
    assert FakeAsyncClient.received_timeouts[0].read == 300


def test_codex_headers_do_not_advertise_service_package_version_as_codex_version() -> None:
    """验证 Codex backend 版本头不会使用本服务的 0.1.0 包版本。"""
    # 上游会按 version 判断 Codex 客户端能力，不能发送本服务自身版本。
    headers = _codex_headers("access-token")

    # Authorization 仍然必须使用 OAuth access token。
    assert headers["Authorization"] == "Bearer access-token"

    # version 应来自本机 Codex 客户端或安全 fallback，而不是 codex-api-service 版本。
    assert headers["version"] != "0.1.0"
    assert headers["User-Agent"] != "codex-api-service/0.1.0"


def test_non_sse_non_json_response_raises_readable_error() -> None:
    """验证上游返回非 SSE 且非 JSON 时不会泄漏 JSONDecodeError。"""
    # 真实服务可能返回 HTML、纯文本或空 body，这里应转换成可诊断错误。
    with pytest.raises(CodexUnexpectedResponseError) as caught:
        _non_sse_response_event(status_code=200, content_type="text/html", body="<html>bad gateway</html>")

    # 错误信息要包含状态、类型和响应片段，方便从 UI 或日志定位。
    message = str(caught.value)
    assert "Unexpected Codex response" in message
    assert "HTTP 200" in message
    assert "text/html" in message
    assert "bad gateway" in message


def test_sse_body_is_parsed_even_when_content_type_is_missing() -> None:
    """验证上游漏掉 content-type 时仍能按 SSE body 解析事件。"""
    # 真实 Codex backend 可能返回 event/data 行但不带 text/event-stream 头。
    body = "\n".join(
        [
            "event: response.output_text.delta",
            'data: {"type":"response.output_text.delta","delta":"OK"}',
            "",
            "data: [DONE]",
            "",
        ]
    )

    # 只提取 JSON data 事件，[DONE] 和 event 行会被忽略。
    assert _sse_body_events(body) == [{"type": "response.output_text.delta", "delta": "OK"}]


@pytest.mark.asyncio
async def test_stream_response_reloads_imported_credentials_when_access_token_is_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 access token 被上游撤销时会导入新 Codex 登录并重试一次。"""

    class RecoveringAuth:
        """模拟本服务有旧 token，同时 Codex CLI 已经写入新 token。"""

        def __init__(self) -> None:
            """记录导入次数，方便断言恢复路径确实被触发。"""
            self.current = CodexCredentials(access="old-access", refresh="old-refresh", expires=4_102_444_800_000)
            self.reload_count = 0

        def ensure_credentials(self) -> CodexCredentials:
            """返回当前服务缓存的凭据。"""
            return self.current

        def reload_import_credentials(self) -> CodexCredentials | None:
            """模拟从 Codex CLI/App auth.json 导入最新凭据。"""
            self.reload_count += 1
            self.current = CodexCredentials(access="fresh-access", refresh="fresh-refresh", expires=4_102_444_800_000)
            return self.current

    class FakeStreamResponse:
        """提供 httpx stream response 所需的最小异步接口。"""

        def __init__(self, *, status_code: int, body: str = "", lines: list[str] | None = None) -> None:
            """保存 fake 响应状态、错误 body 和 SSE 行。"""
            self.status_code = status_code
            self.body = body
            self.lines = lines or []
            self.headers = {"content-type": "text/event-stream"}

        async def __aenter__(self) -> "FakeStreamResponse":
            """进入异步上下文并返回自身。"""
            return self

        async def __aexit__(self, *_args: object) -> None:
            """退出异步上下文时无需清理资源。"""
            return None

        async def aread(self) -> bytes:
            """返回错误响应体。"""
            return self.body.encode("utf-8")

        async def aiter_lines(self):
            """逐行返回 SSE 内容。"""
            for line in self.lines:
                yield line

    class FakeAsyncClient:
        """替代 httpx.AsyncClient，记录每次请求使用的 Authorization。"""

        responses = [
            FakeStreamResponse(
                status_code=401,
                body='{"error":{"message":"Encountered invalidated oauth token for user","code":"token_revoked"}}',
            ),
            FakeStreamResponse(
                status_code=200,
                lines=['data: {"type":"response.output_text.delta","delta":"OK"}', "data: [DONE]"],
            ),
        ]
        authorizations: list[str] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """忽略 timeout 等构造参数。"""
            return None

        async def __aenter__(self) -> "FakeAsyncClient":
            """进入异步上下文并返回自身。"""
            return self

        async def __aexit__(self, *_args: object) -> None:
            """退出异步上下文时无需清理资源。"""
            return None

        def stream(self, _method: str, _url: str, *, headers: dict[str, str], json: dict) -> FakeStreamResponse:
            """返回预设响应，并记录请求头。"""
            self.authorizations.append(headers["Authorization"])
            return self.responses.pop(0)

    monkeypatch.setattr("codex_api_service.codex_client.httpx.AsyncClient", FakeAsyncClient)
    auth = RecoveringAuth()
    client = CodexClient(auth=auth, config=CodexConfig(responses_url="https://example.test/codex"))

    events = [event async for event in client.stream_response({"model": "gpt-5.5"})]

    assert events == [{"type": "response.output_text.delta", "delta": "OK"}]
    assert FakeAsyncClient.authorizations == ["Bearer old-access", "Bearer fresh-access"]
    assert auth.reload_count == 1
