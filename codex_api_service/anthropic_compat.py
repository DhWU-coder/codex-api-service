"""Anthropic Messages-compatible 请求和响应转换工具。"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .openai_compat import extract_output_text


def anthropic_messages_to_codex_input(
    messages: list[dict[str, Any]],
    *,
    system: Any = None,
) -> list[dict[str, Any]]:
    """把 Anthropic Messages 请求转换成 Codex Responses input。"""
    codex_items: list[dict[str, Any]] = []
    system_blocks = _anthropic_content_to_message_blocks(system, role="system")
    if system_blocks:
        codex_items.append({"role": "system", "content": system_blocks})
    for message in messages:
        codex_items.extend(_anthropic_message_to_codex_items(message))
    return codex_items


def anthropic_tools_to_codex_tools(tools: Any) -> list[dict[str, Any]]:
    """把 Anthropic tools 转成 Responses function tools。"""
    if not isinstance(tools, list):
        return []
    codex_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        codex_tool: dict[str, Any] = {
            "type": "function",
            "name": tool["name"],
            "parameters": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {},
        }
        if isinstance(tool.get("description"), str):
            codex_tool["description"] = tool["description"]
        codex_tools.append(codex_tool)
    return codex_tools


def anthropic_tool_choice_to_codex_tool_choice(tool_choice: Any) -> Any:
    """把 Anthropic tool_choice 转成 Responses tool_choice。"""
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "tool" and isinstance(tool_choice.get("name"), str):
        return {"type": "function", "name": tool_choice["name"]}
    if isinstance(choice_type, str):
        return choice_type
    return None


def build_anthropic_message(codex_response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """把 Codex response 转成 Anthropic Message 响应体。"""
    response_id = str(codex_response.get("id") or "resp_local")
    content = _codex_response_to_anthropic_content(codex_response)
    return {
        "id": _message_id(response_id),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _anthropic_stop_reason(codex_response),
        "stop_sequence": None,
        "usage": _anthropic_usage(codex_response.get("usage")),
    }


def build_anthropic_stream_events(
    *,
    message_id: str,
    model: str,
    deltas: Iterable[str],
    usage: dict[str, Any] | None = None,
) -> Iterator[str]:
    """把文本增量序列编码成 Anthropic Messages SSE。"""
    yield anthropic_message_start_sse(message_id=message_id, model=model, usage=usage)
    yield anthropic_content_block_start_sse()
    for delta in deltas:
        yield anthropic_text_delta_sse(delta)
    yield anthropic_content_block_stop_sse()
    yield anthropic_message_delta_sse(usage=usage)
    yield anthropic_message_stop_sse()


def anthropic_message_start_sse(
    *,
    message_id: str,
    model: str,
    usage: dict[str, Any] | None = None,
) -> str:
    """构造 Anthropic message_start 事件。"""
    event_usage = {"input_tokens": 0, "output_tokens": 0}
    normalized_usage = _anthropic_usage(usage)
    if normalized_usage is not None:
        event_usage["input_tokens"] = normalized_usage["input_tokens"]
    return _encode_anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": event_usage,
            },
        },
    )


def anthropic_content_block_start_sse(*, index: int = 0) -> str:
    """构造 Anthropic content_block_start 事件。"""
    return _encode_anthropic_sse(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
    )


def anthropic_text_delta_sse(delta: str, *, index: int = 0) -> str:
    """构造 Anthropic content_block_delta 文本事件。"""
    return _encode_anthropic_sse(
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": delta}},
    )


def anthropic_content_block_stop_sse(*, index: int = 0) -> str:
    """构造 Anthropic content_block_stop 事件。"""
    return _encode_anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": index})


def anthropic_tool_use_start_sse(function_call: dict[str, Any], *, index: int = 0) -> str:
    """构造 Anthropic tool_use content_block_start 事件。"""
    return _encode_anthropic_sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": _function_call_id(function_call),
                "name": function_call.get("name") if isinstance(function_call.get("name"), str) else "tool",
                "input": {},
            },
        },
    )


def anthropic_tool_use_delta_sse(function_call: dict[str, Any], *, index: int = 0) -> str:
    """构造 Anthropic tool_use input_json_delta 事件。"""
    partial_json = function_call.get("arguments") if isinstance(function_call.get("arguments"), str) else "{}"
    return _encode_anthropic_sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    )


def anthropic_message_delta_sse(*, usage: dict[str, Any] | None = None, stop_reason: str = "end_turn") -> str:
    """构造 Anthropic message_delta 事件。"""
    event: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
    }
    normalized_usage = _anthropic_usage(usage)
    if normalized_usage is not None:
        event["usage"] = {"output_tokens": normalized_usage["output_tokens"]}
    return _encode_anthropic_sse("message_delta", event)


def anthropic_message_stop_sse() -> str:
    """构造 Anthropic message_stop 事件。"""
    return _encode_anthropic_sse("message_stop", {"type": "message_stop"})


def anthropic_error_sse(*, message: str, error_type: str = "api_error") -> str:
    """构造 Anthropic 流式错误事件。"""
    return _encode_anthropic_sse(
        "error",
        {"type": "error", "error": {"type": error_type, "message": message}},
    )


def anthropic_message_id_from_response(response: dict[str, Any], *, fallback: str = "resp_stream") -> str:
    """从 Codex 响应中派生 Anthropic message id。"""
    return _message_id(str(response.get("id") or fallback))


def codex_event_function_call(event: dict[str, Any]) -> dict[str, Any] | None:
    """从 Codex SSE 事件中提取完成的 function_call。"""
    item = event.get("item")
    if event.get("type") == "response.output_item.done" and isinstance(item, dict):
        if item.get("type") == "function_call":
            return item
    if event.get("type") == "function_call":
        return event
    return None


def _anthropic_message_to_codex_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """把单条 Anthropic message 转成一个或多个 Responses input item。"""
    role = str(message.get("role") or "user")
    codex_role = role if role in {"user", "assistant", "system", "developer"} else "user"
    items: list[dict[str, Any]] = []
    message_blocks: list[dict[str, Any]] = []

    for part in _anthropic_content_parts(message.get("content", "")):
        special_item = _anthropic_part_to_special_item(part)
        if special_item is not None:
            if message_blocks:
                items.append({"role": codex_role, "content": message_blocks})
                message_blocks = []
            items.append(special_item)
            continue
        block = _anthropic_part_to_message_block(part, role=codex_role)
        if block is not None:
            message_blocks.append(block)

    if message_blocks:
        items.append({"role": codex_role, "content": message_blocks})
    return items


def _anthropic_content_to_message_blocks(content: Any, *, role: str) -> list[dict[str, Any]]:
    """把 Anthropic content 转成单条 Responses message 的 content blocks。"""
    blocks: list[dict[str, Any]] = []
    for part in _anthropic_content_parts(content):
        block = _anthropic_part_to_message_block(part, role=role)
        if block is not None:
            blocks.append(block)
    return blocks


def _anthropic_content_parts(content: Any) -> list[Any]:
    """把 Anthropic content 统一拆成 content parts。"""
    if content is None:
        return []
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _anthropic_part_to_message_block(part: Any, *, role: str) -> dict[str, Any] | None:
    """把普通 Anthropic content part 转成 Responses message block。"""
    block_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(part, str):
        return {"type": block_type, "text": part}
    if not isinstance(part, dict):
        return {"type": block_type, "text": str(part)}
    part_type = part.get("type")
    if part_type == "text" and isinstance(part.get("text"), str):
        return {"type": block_type, "text": part["text"]}
    if part_type == "image" and role != "assistant":
        image_url = _anthropic_image_url(part)
        if image_url:
            return {"type": "input_image", "image_url": image_url}
    return None


def _anthropic_part_to_special_item(part: Any) -> dict[str, Any] | None:
    """把工具相关 content part 转成 Responses 顶层 input item。"""
    if not isinstance(part, dict):
        return None
    if part.get("type") == "tool_use":
        return _tool_use_to_function_call(part)
    if part.get("type") == "tool_result":
        return _tool_result_to_function_output(part)
    return None


def _anthropic_image_url(part: dict[str, Any]) -> str | None:
    """把 Anthropic image source 转成 Responses input_image 的 image_url。"""
    source = part.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64" and isinstance(source.get("data"), str):
        media_type = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
        return f"data:{media_type};base64,{source['data']}"
    if source_type == "url" and isinstance(source.get("url"), str):
        return source["url"]
    return None


def _anthropic_content_to_texts(content: Any) -> list[str]:
    """提取 Anthropic 文本内容，用于工具结果 output。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                texts.extend(_anthropic_part_to_texts(part))
        return texts
    return [str(content)]


def _anthropic_part_to_texts(part: dict[str, Any]) -> list[str]:
    """把单个 Anthropic content block 转成文本。"""
    part_type = part.get("type")
    if part_type == "text" and isinstance(part.get("text"), str):
        return [part["text"]]
    return []


def _tool_use_to_function_call(part: dict[str, Any]) -> dict[str, Any] | None:
    """把 Anthropic tool_use 历史转成 Responses function_call。"""
    tool_use_id = part.get("id")
    name = part.get("name")
    if not isinstance(name, str):
        return None
    return {
        "type": "function_call",
        "call_id": tool_use_id if isinstance(tool_use_id, str) else f"call_{name}",
        "name": name,
        "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False, separators=(",", ":")),
    }


def _tool_result_to_function_output(part: dict[str, Any]) -> dict[str, Any] | None:
    """把 Anthropic tool_result 转成 Responses function_call_output。"""
    tool_use_id = part.get("tool_use_id")
    if not isinstance(tool_use_id, str):
        return None
    output = "\n".join(_anthropic_content_to_texts(part.get("content")))
    return {"type": "function_call_output", "call_id": tool_use_id, "output": output}


def _codex_response_to_anthropic_content(response: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Codex output 列表转换成 Anthropic content blocks。"""
    content_blocks: list[dict[str, Any]] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content_blocks.extend(_codex_output_item_to_anthropic_blocks(item))
    if content_blocks:
        return content_blocks
    return [{"type": "text", "text": extract_output_text(response)}]


def _codex_output_item_to_anthropic_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
    """把单个 Codex output item 转成 Anthropic content blocks。"""
    item_type = item.get("type")
    if item_type == "function_call":
        return [_function_call_to_tool_use(item)]
    if item_type == "message" and isinstance(item.get("content"), list):
        text_blocks: list[dict[str, Any]] = []
        for block in item["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text_blocks.append({"type": "text", "text": block["text"]})
        return text_blocks
    return []


def _function_call_to_tool_use(item: dict[str, Any]) -> dict[str, Any]:
    """把 Responses function_call 转成 Anthropic tool_use。"""
    arguments = item.get("arguments")
    tool_input: Any = {}
    if isinstance(arguments, str) and arguments:
        try:
            tool_input = json.loads(arguments)
        except json.JSONDecodeError:
            tool_input = {"arguments": arguments}
    return {
        "type": "tool_use",
        "id": _function_call_id(item),
        "name": item.get("name") if isinstance(item.get("name"), str) else "tool",
        "input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
    }


def _function_call_id(item: dict[str, Any]) -> str:
    """读取 function_call 的稳定调用 id。"""
    call_id = item.get("call_id") or item.get("id")
    return call_id if isinstance(call_id, str) else "call_local"


def _anthropic_usage(usage: Any) -> dict[str, int] | None:
    """把 Responses/Codex usage 转成 Anthropic usage。"""
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_field(usage, "input_tokens", "prompt_tokens")
    output_tokens = _int_field(usage, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    result = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    cache_creation = _int_field(usage, "cache_creation_input_tokens")
    cache_read = _int_field(usage, "cache_read_input_tokens")
    if cache_creation is not None:
        result["cache_creation_input_tokens"] = cache_creation
    if cache_read is not None:
        result["cache_read_input_tokens"] = cache_read
    return result


def _anthropic_stop_reason(response: dict[str, Any]) -> str:
    """把上游结束原因转换成 Anthropic stop_reason。"""
    finish_reason = response.get("finish_reason") or response.get("stop_reason")
    output = response.get("output")
    if isinstance(output, list) and any(isinstance(item, dict) and item.get("type") == "function_call" for item in output):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if finish_reason == "stop_sequence":
        return "stop_sequence"
    return "end_turn"


def _message_id(response_id: str) -> str:
    """把 Codex response id 转成 Anthropic message id。"""
    return response_id if response_id.startswith("msg_") else f"msg_{response_id}"


def _encode_anthropic_sse(event_name: str, data: dict[str, Any]) -> str:
    """按 Anthropic 约定编码命名 SSE 事件。"""
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {encoded}\n\n"


def _int_field(data: dict[str, Any], *names: str) -> int | None:
    """按候选字段名读取整数字段。"""
    for name in names:
        value = data.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None
