import json

from codex_api_service.openai_compat import (
    build_chat_completion,
    build_chat_stream_events,
    build_response_object,
    chat_messages_to_codex_input,
    encode_sse,
    extract_output_text,
    normalize_responses_input,
)


def test_chat_messages_to_codex_input_uses_role_specific_text_block_types() -> None:
    """验证 Chat Completions messages 能转换为 Codex Responses input。"""
    # 输入同时覆盖字符串 content、OpenAI content parts 和历史 assistant 消息。
    messages = [
        {"role": "system", "content": "follow rules"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": "hi"},
    ]

    # assistant 历史消息在 Responses input 中必须使用 output_text，否则上游会拒绝 input_text。
    codex_input = chat_messages_to_codex_input(messages)

    assert codex_input == [
        {"role": "system", "content": [{"type": "input_text", "text": "follow rules"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]


def test_normalize_responses_input_uses_assistant_output_text_blocks() -> None:
    """验证 Responses input 中的 assistant 文本会规范化为 output_text。"""
    # 外部客户端可能传 text 或 input_text，本地兼容层都要按 role 纠正。
    codex_input = normalize_responses_input(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "input_text", "text": "hi"}]},
        ]
    )

    assert codex_input == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]


def test_extract_output_text_reads_output_text_or_output_blocks() -> None:
    """验证文本提取兼容 output_text 和 output content blocks。"""
    # output_text 是最直接的响应字段。
    assert extract_output_text({"output_text": "hello"}) == "hello"

    # 参考 Responses API 的 output content 结构也要支持。
    assert (
        extract_output_text(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "hel"},
                            {"type": "output_text", "text": "lo"},
                        ],
                    }
                ]
            }
        )
        == "hello"
    )


def test_build_chat_completion_maps_codex_response_to_openai_shape() -> None:
    """验证非流式 Codex 响应会变成 OpenAI ChatCompletion。"""
    # Codex response 中包含文本和真实 usage。
    completion = build_chat_completion(
        codex_response={
            "id": "resp_abc",
            "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
        model="gpt-5.5",
    )

    # 返回体遵循 Chat Completions 基本字段。
    assert completion["id"] == "chatcmpl-resp_abc"
    assert completion["object"] == "chat.completion"
    assert completion["model"] == "gpt-5.5"
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert completion["choices"][0]["finish_reason"] == "stop"
    assert completion["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_build_response_object_keeps_responses_shape() -> None:
    """验证 /v1/responses 非流式结果保持 Responses API 风格。"""
    # Codex response 的 id 和 usage 应被保留或规范化。
    response = build_response_object(
        codex_response={
            "id": "resp_abc",
            "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
        model="gpt-5.5",
    )

    # 响应包含 output_text 便于 SDK 直接读取文本。
    assert response["id"] == "resp_abc"
    assert response["object"] == "response"
    assert response["model"] == "gpt-5.5"
    assert response["output_text"] == "hello"
    assert response["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_build_chat_stream_events_emits_delta_usage_and_done() -> None:
    """验证 Chat Completions 流式输出包含 delta、usage chunk 和 DONE。"""
    # 两段文本增量和一段最终 usage 模拟 Codex SSE 转换后的数据。
    events = list(
        build_chat_stream_events(
            response_id="resp_abc",
            model="gpt-5.5",
            deltas=["hel", "lo"],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    )

    # 前两段为 delta chunk，倒数第二段为 usage chunk，最后为 [DONE]。
    assert json.loads(events[0][len("data: ") :].strip())["choices"][0]["delta"]["content"] == "hel"
    assert json.loads(events[1][len("data: ") :].strip())["choices"][0]["delta"]["content"] == "lo"
    assert json.loads(events[2][len("data: ") :].strip())["choices"] == []
    assert json.loads(events[2][len("data: ") :].strip())["usage"]["total_tokens"] == 15
    assert events[3] == "data: [DONE]\n\n"


def test_encode_sse_serializes_data_events() -> None:
    """验证 SSE 编码使用 OpenAI-compatible 的 data 行格式。"""
    # SSE 每个事件都应该以空行结束，便于客户端逐条解析。
    assert encode_sse({"type": "response.output_text.delta", "delta": "hi"}) == (
        'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    )
