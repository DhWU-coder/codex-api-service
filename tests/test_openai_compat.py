import json

import codex_api_service.openai_compat as openai_compat
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


def test_chat_messages_to_codex_input_preserves_tool_call_history() -> None:
    """验证 Chat 工具调用历史会转换成 Responses 顶层工具 item。"""
    # Claudish 会把 Claude tool_use/tool_result 转换成 assistant tool_calls 和 role=tool。
    messages = [
        {
            "role": "assistant",
            "content": "我来读取",
            "tool_calls": [
                {
                    "id": "call_read_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"file_path":"/tmp/demo"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_read_1", "content": "README content"},
    ]

    codex_input = chat_messages_to_codex_input(messages)

    assert codex_input == [
        {"role": "assistant", "content": [{"type": "output_text", "text": "我来读取"}]},
        {
            "type": "function_call",
            "call_id": "call_read_1",
            "name": "Read",
            "arguments": '{"file_path":"/tmp/demo"}',
        },
        {"type": "function_call_output", "call_id": "call_read_1", "output": "README content"},
    ]


def test_openai_tools_to_codex_tools_flattens_function_schema() -> None:
    """验证 Chat function tools 会转换成 Responses function tools。"""
    tools = [
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
                "strict": True,
            },
        },
        {"type": "custom", "name": "ignored"},
    ]

    converted = openai_compat.openai_tools_to_codex_tools(tools)

    assert converted == [
        {
            "type": "function",
            "name": "Read",
            "description": "读取文件",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            "strict": True,
        }
    ]


def test_openai_tool_choice_to_codex_tool_choice_flattens_forced_function() -> None:
    """验证 Chat 强制函数选择会转换成 Responses tool_choice。"""
    forced = {"type": "function", "function": {"name": "Read"}}

    assert openai_compat.openai_tool_choice_to_codex_tool_choice("auto") == "auto"
    assert openai_compat.openai_tool_choice_to_codex_tool_choice("required") == "required"
    assert openai_compat.openai_tool_choice_to_codex_tool_choice(forced) == {
        "type": "function",
        "name": "Read",
    }


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


def test_build_chat_completion_maps_function_calls_to_openai_tool_calls() -> None:
    """验证非流式 Codex function_call 会变成 ChatCompletion tool_calls。"""
    completion = build_chat_completion(
        codex_response={
            "id": "resp_tool",
            "output": [
                {
                    "id": "fc_read_1",
                    "type": "function_call",
                    "call_id": "call_read_1",
                    "name": "Read",
                    "arguments": '{"file_path":"/tmp/demo"}',
                    "status": "completed",
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
        },
        model="gpt-5.5",
    )

    choice = completion["choices"][0]
    assert choice["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_read_1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path":"/tmp/demo"}'},
            }
        ],
    }
    assert choice["finish_reason"] == "tool_calls"


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


def test_build_chat_tool_call_sse_emits_openai_delta_shape() -> None:
    """验证完整 Codex function_call 会编码成 OpenAI 流式 tool_calls delta。"""
    event = openai_compat.build_chat_tool_call_sse(
        response_id="resp_tool",
        model="gpt-5.5",
        function_call={
            "call_id": "call_read_1",
            "name": "Read",
            "arguments": '{"file_path":"/tmp/demo"}',
        },
        index=0,
        created=123,
    )

    payload = json.loads(event.removeprefix("data: "))
    assert payload["choices"] == [
        {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_read_1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": '{"file_path":"/tmp/demo"}'},
                    }
                ]
            },
            "finish_reason": None,
        }
    ]


def test_build_chat_finish_sse_emits_tool_calls_reason() -> None:
    """验证 Chat 流会在 usage 前发送明确的工具调用结束原因。"""
    event = openai_compat.build_chat_finish_sse(
        response_id="resp_tool",
        model="gpt-5.5",
        finish_reason="tool_calls",
        created=123,
    )

    payload = json.loads(event.removeprefix("data: "))
    assert payload["choices"] == [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]


def test_encode_sse_serializes_data_events() -> None:
    """验证 SSE 编码使用 OpenAI-compatible 的 data 行格式。"""
    # SSE 每个事件都应该以空行结束，便于客户端逐条解析。
    assert encode_sse({"type": "response.output_text.delta", "delta": "hi"}) == (
        'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    )
