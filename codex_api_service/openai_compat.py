"""OpenAI-compatible 请求和响应转换工具。"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Iterator


def chat_messages_to_codex_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 Chat Completions messages 转成 Codex Responses input。"""
    codex_messages: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "tool":
            tool_output = _chat_tool_message_to_codex_output(message)
            if tool_output is not None:
                codex_messages.append(tool_output)
            continue

        # Codex backend 接受 user/system/developer 这类 Responses input role。
        content = message.get("content")
        if content is not None:
            blocks = _content_to_text_blocks(content, role=role)
            if blocks:
                codex_messages.append({"role": role, "content": blocks})
        if role == "assistant":
            codex_messages.extend(_chat_tool_calls_to_codex_items(message.get("tool_calls")))
    return codex_messages


def openai_tools_to_codex_tools(tools: Any) -> list[dict[str, Any]]:
    """把 OpenAI Chat function tools 转成 Responses function tools。"""
    if not isinstance(tools, list):
        return []
    codex_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        codex_tool: dict[str, Any] = {
            "type": "function",
            "name": function["name"],
            "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {},
        }
        if isinstance(function.get("description"), str):
            codex_tool["description"] = function["description"]
        if isinstance(function.get("strict"), bool):
            codex_tool["strict"] = function["strict"]
        codex_tools.append(codex_tool)
    return codex_tools


def openai_tool_choice_to_codex_tool_choice(tool_choice: Any) -> Any:
    """把 OpenAI Chat tool_choice 转成 Responses tool_choice。"""
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None
    function = tool_choice.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        return None
    return {"type": "function", "name": function["name"]}


def normalize_responses_input(value: Any) -> Any:
    """把 /v1/responses input 规范化为 Codex backend 可接受的 input。"""
    # 字符串 input 是 Responses API 的常见用法，按 user 消息处理。
    if isinstance(value, str):
        return [{"role": "user", "content": [{"type": "input_text", "text": value}]}]
    if isinstance(value, list):
        return [_normalize_response_input_item(item) for item in value]
    return value


def extract_output_text(response: dict[str, Any]) -> str:
    """从 Codex/Responses 响应中提取文本输出。"""
    # output_text 是 Codex backend 和 Responses API 最便捷的文本字段。
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text

    # 如果没有 output_text，就遍历 output message content blocks 拼接文本。
    texts: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "".join(texts)


def build_chat_completion(codex_response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """把 Codex response 转成 OpenAI ChatCompletion 响应体。"""
    # 使用上游 id 派生 chat completion id，便于日志和客户端排查问题。
    response_id = str(codex_response.get("id") or _new_response_id())
    text = extract_output_text(codex_response)
    tool_calls = _codex_response_to_openai_tool_calls(codex_response)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text if text or not tool_calls else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{response_id}",
        "object": "chat.completion",
        "created": _created_at(codex_response),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else _finish_reason(codex_response),
            }
        ],
        "usage": _chat_usage(codex_response.get("usage")),
    }


def _codex_response_to_openai_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Codex response 中的 function_call 输出转换成 OpenAI tool_calls。"""
    output = response.get("output")
    if not isinstance(output, list):
        return []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call":
            tool_calls.append(_codex_function_call_to_openai_tool_call(item))
    return tool_calls


def _codex_function_call_to_openai_tool_call(function_call: dict[str, Any]) -> dict[str, Any]:
    """把单个 Responses function_call 转成 ChatCompletion tool call。"""
    call_id = function_call.get("call_id") or function_call.get("id")
    name = function_call.get("name")
    arguments = function_call.get("arguments")
    return {
        "id": call_id if isinstance(call_id, str) else "call_local",
        "type": "function",
        "function": {
            "name": name if isinstance(name, str) else "tool",
            "arguments": arguments if isinstance(arguments, str) else _json_text(arguments or {}),
        },
    }


def build_response_object(codex_response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """把 Codex response 转成 OpenAI Responses API 响应体。"""
    # 如果上游没有 id，就生成一个本地 response id。
    response_id = str(codex_response.get("id") or _new_response_id())
    text = extract_output_text(codex_response)
    return {
        "id": response_id,
        "object": "response",
        "created_at": _created_at(codex_response),
        "status": codex_response.get("status") or "completed",
        "model": model,
        "output": _response_output(response_id, text),
        "output_text": text,
        "usage": _responses_usage(codex_response.get("usage")),
    }


def build_chat_stream_events(
    *,
    response_id: str,
    model: str,
    deltas: Iterable[str],
    usage: dict[str, Any] | None = None,
) -> Iterator[str]:
    """把文本增量序列编码为 Chat Completions SSE。"""
    chat_id = f"chatcmpl-{response_id}"
    created = int(time.time())
    for delta in deltas:
        # 每个文本片段用 choices[0].delta.content 推给 OpenAI SDK。
        yield encode_sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                "usage": None,
            }
        )

    # Chat Completions 的 include_usage 约定是在 DONE 前发送空 choices usage chunk。
    if usage is not None:
        yield encode_sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": _chat_usage(usage),
            }
        )
    yield "data: [DONE]\n\n"


def build_chat_delta_sse(*, response_id: str, model: str, delta: str, created: int | None = None) -> str:
    """构造单条 Chat Completions 文本增量 SSE。"""
    # 独立函数供 FastAPI 流式路由边读边写。
    return encode_sse(
        {
            "id": f"chatcmpl-{response_id}",
            "object": "chat.completion.chunk",
            "created": created or int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            "usage": None,
        }
    )


def build_chat_tool_call_sse(
    *,
    response_id: str,
    model: str,
    function_call: dict[str, Any],
    index: int,
    created: int | None = None,
) -> str:
    """构造单条 Chat Completions 工具调用增量 SSE。"""
    tool_call = _codex_function_call_to_openai_tool_call(function_call)
    tool_call["index"] = index
    return encode_sse(
        {
            "id": f"chatcmpl-{response_id}",
            "object": "chat.completion.chunk",
            "created": created or int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [tool_call]},
                    "finish_reason": None,
                }
            ],
            "usage": None,
        }
    )


def build_chat_finish_sse(
    *,
    response_id: str,
    model: str,
    finish_reason: str,
    created: int | None = None,
) -> str:
    """构造 Chat Completions 流式结束原因 SSE。"""
    return encode_sse(
        {
            "id": f"chatcmpl-{response_id}",
            "object": "chat.completion.chunk",
            "created": created or int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": None,
        }
    )


def build_chat_usage_sse(*, response_id: str, model: str, usage: dict[str, Any], created: int | None = None) -> str:
    """构造 Chat Completions 流式结尾 usage SSE。"""
    # OpenAI 的 usage chunk 约定 choices 为空数组。
    return encode_sse(
        {
            "id": f"chatcmpl-{response_id}",
            "object": "chat.completion.chunk",
            "created": created or int(time.time()),
            "model": model,
            "choices": [],
            "usage": _chat_usage(usage),
        }
    )


def encode_sse(data: dict[str, Any]) -> str:
    """把 JSON 数据编码成 OpenAI-compatible SSE data 事件。"""
    # separators 去掉多余空格，便于测试和客户端解析。
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def codex_event_text_delta(event: dict[str, Any]) -> str | None:
    """从 Codex SSE 事件中提取文本增量。"""
    # 当前参考实现观察到的文本增量事件类型为 response.output_text.delta。
    if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
        return event["delta"]
    return None


def codex_event_completed_response(event: dict[str, Any]) -> dict[str, Any] | None:
    """从 Codex SSE completed 事件中提取完整响应。"""
    response = event.get("response")
    if event.get("type") == "response.completed" and isinstance(response, dict):
        return response
    return None


def response_stream_event(event: dict[str, Any]) -> str:
    """把 Codex Responses SSE 事件转成 /v1/responses SSE。"""
    # Codex backend 已经使用 Responses 风格事件名，所以这里只做安全 JSON 编码。
    return encode_sse(event)


def _chat_tool_calls_to_codex_items(tool_calls: Any) -> list[dict[str, Any]]:
    """把 assistant tool_calls 历史转换成 Responses function_call items。"""
    if not isinstance(tool_calls, list):
        return []
    items: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or tool_call.get("type") != "function":
            continue
        function = tool_call.get("function")
        call_id = tool_call.get("id")
        if not isinstance(function, dict) or not isinstance(call_id, str):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = _json_text(arguments if arguments is not None else {})
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return items


def _chat_tool_message_to_codex_output(message: dict[str, Any]) -> dict[str, Any] | None:
    """把 role=tool 消息转换成 Responses function_call_output。"""
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str):
        return None
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": _chat_tool_output_text(message.get("content")),
    }


def _chat_tool_output_text(content: Any) -> str:
    """把 Chat 工具结果内容转换成 Codex 可消费的字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        return "\n".join(texts)
    if content is None:
        return ""
    return _json_text(content)


def _json_text(value: Any) -> str:
    """把兼容输入稳定序列化成紧凑 JSON，失败时回退到字符串。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _content_to_input_blocks(content: Any) -> list[dict[str, Any]]:
    """把 OpenAI message content 转成 input_text blocks。"""
    return _content_to_text_blocks(content, role="user")


def _content_to_text_blocks(content: Any, *, role: str) -> list[dict[str, Any]]:
    """按 role 把 OpenAI message content 转成 Responses text blocks。"""
    block_type = _text_block_type_for_role(role)
    if isinstance(content, str):
        return [{"type": block_type, "text": content}]
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                # refusal 是 assistant 历史消息允许的特殊块类型，不能改写成 output_text。
                if role == "assistant" and part.get("type") == "refusal":
                    blocks.append({"type": "refusal", "text": text})
                else:
                    blocks.append({"type": block_type, "text": text})
        return blocks
    return [{"type": block_type, "text": str(content)}]


def _normalize_response_input_item(item: Any) -> Any:
    """规范化 Responses input 列表中的单个 item。"""
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    role = str(normalized.get("role") or "user")
    content = normalized.get("content")
    if isinstance(content, str):
        normalized["content"] = [{"type": _text_block_type_for_role(role), "text": content}]
    elif isinstance(content, list):
        normalized["content"] = [_normalize_content_block(block, role=role) for block in content]
    return normalized


def _normalize_content_block(block: Any, *, role: str) -> Any:
    """按 role 把 text block 类型兼容到 Codex backend 支持的名称。"""
    if not isinstance(block, dict):
        return block
    normalized = dict(block)
    if normalized.get("type") in {"text", "input_text", "output_text"}:
        normalized["type"] = _text_block_type_for_role(role)
    return normalized


def _text_block_type_for_role(role: str) -> str:
    """返回当前 role 在 Responses input 中应该使用的文本块类型。"""
    # assistant 历史消息表示模型已经输出过的内容，上游要求 output_text。
    return "output_text" if role == "assistant" else "input_text"


def _chat_usage(usage: Any) -> dict[str, int] | None:
    """把 Responses/Codex usage 转成 Chat Completions usage。"""
    if not isinstance(usage, dict):
        return None
    prompt_tokens = _int_field(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _int_field(usage, "completion_tokens", "output_tokens")
    total_tokens = _int_field(usage, "total_tokens")
    if prompt_tokens is None or completion_tokens is None or total_tokens is None:
        return None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _responses_usage(usage: Any) -> dict[str, Any] | None:
    """把 usage 转成 Responses API 字段名。"""
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_field(usage, "input_tokens", "prompt_tokens")
    output_tokens = _int_field(usage, "output_tokens", "completion_tokens")
    total_tokens = _int_field(usage, "total_tokens")
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    result: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    input_details = _details_for(usage, "input_tokens_details", "prompt_tokens_details", "cached_tokens")
    output_details = _details_for(usage, "output_tokens_details", "completion_tokens_details", "reasoning_tokens")
    if input_details:
        result["input_tokens_details"] = input_details
    if output_details:
        result["output_tokens_details"] = output_details
    return result


def _details_for(usage: dict[str, Any], primary_name: str, fallback_name: str, detail_key: str) -> dict[str, int] | None:
    """提取 usage 明细字段并统一为 Responses details 名称。"""
    for name in (primary_name, fallback_name):
        value = usage.get(name)
        if isinstance(value, dict) and isinstance(value.get(detail_key), int):
            return {detail_key: value[detail_key]}
    return None


def _response_output(response_id: str, text: str) -> list[dict[str, Any]]:
    """构造 Responses API 的 output message。"""
    return [
        {
            "id": f"msg_{response_id}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    ]


def _finish_reason(response: dict[str, Any]) -> str:
    """从响应中读取结束原因，缺省为 stop。"""
    finish_reason = response.get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else "stop"


def _created_at(response: dict[str, Any]) -> int:
    """读取或生成 Unix 秒级创建时间。"""
    created_at = response.get("created_at") or response.get("created")
    if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
        return int(created_at)
    return int(time.time())


def _new_response_id() -> str:
    """生成本地 response id。"""
    return f"resp_{uuid.uuid4().hex}"


def _int_field(data: dict[str, Any], *names: str) -> int | None:
    """按候选字段名读取整数字段。"""
    for name in names:
        value = data.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None
