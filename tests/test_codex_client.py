import pytest

from codex_api_service.codex_client import (
    CodexUnexpectedResponseError,
    _codex_headers,
    _non_sse_response_event,
    _sse_body_events,
)


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
