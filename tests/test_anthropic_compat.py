import json

from codex_api_service.anthropic_compat import (
    anthropic_messages_to_codex_input,
    anthropic_tools_to_codex_tools,
    build_anthropic_message,
    build_anthropic_stream_events,
)


def test_anthropic_messages_to_codex_input_merges_system_and_text_blocks() -> None:
    """验证 Anthropic Messages 请求能转换为 Codex Responses input。"""
    # system 是 Anthropic 顶层字段；消息 content 同时覆盖字符串和 text block。
    codex_input = anthropic_messages_to_codex_input(
        messages=[
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": [{"type": "text", "text": "你好，有什么可以帮你？"}]},
            {"role": "user", "content": [{"type": "text", "text": "写一句短句"}]},
        ],
        system="保持简洁",
    )

    assert codex_input == [
        {"role": "system", "content": [{"type": "input_text", "text": "保持简洁"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "你好"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "你好，有什么可以帮你？"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "写一句短句"}]},
    ]


def test_anthropic_messages_to_codex_input_maps_images_and_tool_results() -> None:
    """验证图片和工具结果会转换为 Responses 风格输入项。"""
    # Anthropic 的图片块应变成 input_image，工具调用历史应保留为 function_call/function_call_output。
    codex_input = anthropic_messages_to_codex_input(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "杭州"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "晴天"}
                ],
            },
        ]
    )

    assert codex_input == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,abc123"},
            ],
        },
        {
            "type": "function_call",
            "call_id": "toolu_1",
            "name": "get_weather",
            "arguments": '{"city":"杭州"}',
        },
        {"type": "function_call_output", "call_id": "toolu_1", "output": "晴天"},
    ]


def test_anthropic_tools_to_codex_tools_maps_function_schemas() -> None:
    """验证 Anthropic tools 会转换成 Responses function tools。"""
    # input_schema 是 Anthropic 工具参数定义，对应 Responses function parameters。
    tools = anthropic_tools_to_codex_tools(
        [
            {
                "name": "get_weather",
                "description": "查询天气",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]
    )

    assert tools == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]


def test_build_anthropic_message_maps_codex_response_shape() -> None:
    """验证 Codex 非流式响应能转换为 Anthropic message。"""
    # Codex response 包含文本和 Responses 风格 usage。
    message = build_anthropic_message(
        {
            "id": "resp_abc",
            "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
        model="gpt-5.5",
    )

    assert message == {
        "id": "msg_resp_abc",
        "type": "message",
        "role": "assistant",
        "model": "gpt-5.5",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_build_anthropic_message_maps_function_call_to_tool_use() -> None:
    """验证 Codex function_call 输出会转换为 Anthropic tool_use。"""
    # Responses 风格 function_call 是工具调用的结构化输出。
    message = build_anthropic_message(
        {
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
        model="gpt-5.5",
    )

    assert message["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "杭州"}}
    ]
    assert message["stop_reason"] == "tool_use"


def test_build_anthropic_stream_events_emits_named_sse_events() -> None:
    """验证 Anthropic 流式响应使用命名 SSE 事件。"""
    # 两段文本增量和最终 usage 应组成 Anthropic Messages 事件序列。
    events = list(
        build_anthropic_stream_events(
            message_id="msg_resp_abc",
            model="gpt-5.5",
            deltas=["hel", "lo"],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    )

    assert events[0].startswith("event: message_start\n")
    assert json.loads(events[0].split("data: ", 1)[1])["type"] == "message_start"
    assert json.loads(events[1].split("data: ", 1)[1])["type"] == "content_block_start"
    assert json.loads(events[2].split("data: ", 1)[1])["delta"] == {"type": "text_delta", "text": "hel"}
    assert json.loads(events[3].split("data: ", 1)[1])["delta"] == {"type": "text_delta", "text": "lo"}
    assert json.loads(events[-2].split("data: ", 1)[1])["usage"] == {"output_tokens": 5}
    assert json.loads(events[-1].split("data: ", 1)[1])["type"] == "message_stop"
