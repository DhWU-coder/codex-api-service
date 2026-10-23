"""调用 Codex backend Responses endpoint 的异步 client。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx

from .auth import CodexAuth
from .config import CodexConfig
from .oauth_credentials import credentials_fingerprint

# 这些状态通常是临时上游错误，可以在没有输出任何事件前重试。
RETRYABLE_HTTP_STATUS = {502, 503, 504}


class CodexHTTPStatusError(RuntimeError):
    """携带 HTTP 状态码的 Codex 请求错误。"""

    def __init__(
        self,
        status_code: int,
        body: str,
        *,
        error_type: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        credentials_fingerprint: str | None = None,
    ) -> None:
        """保存状态码、OpenAI 错误字段和响应体供重试判断与报错。"""
        parsed_type, parsed_code, parsed_message = _error_fields_from_body(body)
        self.status_code = status_code
        self.body = body
        self.error_type = error_type or parsed_type or _error_type_for_code(error_code or parsed_code)
        self.error_code = error_code or parsed_code
        self.error_message = error_message or parsed_message
        self.credentials_fingerprint = credentials_fingerprint
        detail = self.error_message or _body_snippet(body)
        super().__init__(f"Codex request failed: HTTP {status_code}: {detail}")


class CodexUnexpectedResponseError(RuntimeError):
    """表示 Codex 返回了成功状态但响应格式不可用。"""

    def __init__(self, *, upstream_status_code: int, content_type: str, body: str) -> None:
        """保存上游状态、内容类型和响应片段，便于 UI 与日志展示。"""
        # 本地服务用 502 表示上游响应协议异常，而不是把解析错误暴露成 500。
        self.status_code = 502
        self.upstream_status_code = upstream_status_code
        self.content_type = content_type or "<missing>"
        self.body = _body_snippet(body)
        super().__init__(
            "Unexpected Codex response: "
            f"HTTP {upstream_status_code}, content-type {self.content_type}, body {self.body}"
        )


class CodexClient:
    """封装 Codex OAuth 和 backend SSE 调用。"""

    def __init__(self, *, auth: CodexAuth, config: CodexConfig) -> None:
        """初始化 client。"""
        self.auth = auth
        self.config = config

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送请求并聚合 Codex SSE 为完整响应。"""
        # Codex backend 以 SSE 返回增量；非流式 API 在服务端聚合后再响应客户端。
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        text_deltas: list[str] = []
        output_items: dict[int, dict[str, Any]] = {}
        completed: dict[str, Any] | None = None
        async for event in self.stream_response(stream_payload):
            event_error = codex_event_error(event)
            if event_error is not None:
                raise event_error
            if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
                text_deltas.append(event["delta"])
            if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict):
                # 完成事件可能不再重复 output，必须保留此前已完成的工具、推理和消息项。
                index = event.get("output_index")
                if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                    # 兼容缺少索引的事件，按接收顺序补充，避免覆盖已有项。
                    index = max(output_items, default=-1) + 1
                output_items[index] = event["item"]
            response = event.get("response")
            if event.get("type") == "response.completed" and isinstance(response, dict):
                completed = response

        # 只在收到 completed 后补齐输出，避免把中途断流误报为成功。
        if completed is not None:
            if not completed.get("output") and output_items:
                completed = {**completed, "output": [output_items[index] for index in sorted(output_items)]}
            if "output_text" not in completed and text_deltas:
                completed = {**completed, "output_text": "".join(text_deltas)}
            return completed
        raise CodexUnexpectedResponseError(
            upstream_status_code=200,
            content_type="text/event-stream",
            body="SSE stream ended before response.completed",
        )

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """发送请求并逐个产出 Codex SSE JSON 事件。"""
        # 只有在尚未产出任何事件前，才安全地重试 transient 错误。
        last_error: Exception | None = None
        for attempt in range(3):
            emitted = False
            try:
                async for event in self._stream_response_once(payload):
                    emitted = True
                    yield event
                return
            except Exception as error:
                last_error = error
                if emitted or attempt >= 2 or not _is_retryable_error(error):
                    raise
                await asyncio.sleep(2**attempt)
        if last_error is not None:
            raise last_error

    async def _stream_response_once(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """执行单次 Codex SSE HTTP 请求。"""
        request_payload = dict(payload)
        request_payload["stream"] = True
        reloaded_after_revocation = False

        while True:
            # ensure_credentials 会在必要时刷新 token 或触发首次登录。
            credentials = self.auth.ensure_credentials()

            # 读取超时只限制连续无数据时间，正常 SSE 事件会持续重置计时。
            timeout = httpx.Timeout(
                self.config.timeout_seconds,
                connect=30.0,
                read=self.config.stream_idle_timeout_seconds,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    self.config.responses_url,
                    headers=_codex_headers(credentials.access),
                    json=request_payload,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if not reloaded_after_revocation and _is_revoked_oauth_token(response.status_code, body):
                            imported = self.auth.reload_import_credentials()
                            if imported is not None:
                                reloaded_after_revocation = True
                                continue
                        raise CodexHTTPStatusError(
                            response.status_code, body, credentials_fingerprint=credentials_fingerprint(credentials),
                        )

                    # 非 SSE JSON 响应也兼容成 completed 事件，便于后续扩展。
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        buffered_events = _sse_body_events(body)
                        if buffered_events:
                            # 某些上游响应漏掉 content-type，但 body 仍是标准 SSE 行。
                            for event in buffered_events:
                                event_error = codex_event_error(event)
                                if event_error is not None:
                                    event_error.credentials_fingerprint = credentials_fingerprint(credentials)
                                    raise event_error
                                yield event
                            return
                        try:
                            event = _non_sse_response_event(
                                status_code=response.status_code, content_type=content_type, body=body,
                            )
                        except CodexHTTPStatusError as error:
                            error.credentials_fingerprint = credentials_fingerprint(credentials)
                            raise
                        yield event
                        return

                    # SSE 每个 data 行都是一个 JSON event；[DONE] 表示流结束。
                    async for line in response.aiter_lines():
                        if _is_sse_done_line(line):
                            return
                        event = _parse_sse_line(line)
                        if event is None:
                            continue
                        event_error = codex_event_error(event)
                        if event_error is not None:
                            event_error.credentials_fingerprint = credentials_fingerprint(credentials)
                            raise event_error
                        yield event
                        if event.get("type") == "response.completed":
                            return
                    return


def _is_revoked_oauth_token(status_code: int, body: str) -> bool:
    """判断上游 401 是否表示当前 access token 已被撤销。"""
    if status_code != 401:
        return False
    lowered = body.lower()
    return any(
        marker in lowered
        for marker in (
            "token_revoked",
            "invalidated oauth token",
            "oauth token revoked",
        )
    )


def _codex_headers(access_token: str) -> dict[str, str]:
    """构造 Codex backend 请求头。"""
    # 上游会根据 version 判断 Codex 客户端能力，所以这里使用本机 Codex CLI 版本。
    client_version = _codex_client_version()
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "originator": "codex-api-service",
        "version": client_version,
        "User-Agent": f"codex-api-service/{client_version}",
    }


@lru_cache(maxsize=1)
def _codex_client_version() -> str:
    """解析本机 Codex CLI 版本，失败时返回不会冒充旧版本的 unknown。"""
    # 环境变量方便用户在特殊安装路径或未来兼容性变化时手动覆盖。
    configured = os.environ.get("CODEX_API_SERVICE_CLIENT_VERSION")
    if configured and configured.strip():
        return configured.strip()

    # launchd 的 PATH 很短，所以不能只依赖 shutil.which。
    codex_command = _resolve_codex_command()
    if not codex_command:
        return "unknown"

    try:
        result = subprocess.run(
            [codex_command, "--version"],
            check=False,
            text=True,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"

    # 典型输出是 "codex-cli 0.136.0"，只取语义版本号给上游。
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    return match.group(1) if match else "unknown"


def _resolve_codex_command() -> str | None:
    """查找本机 Codex CLI 可执行文件。"""
    # 先用当前 PATH，再检查 macOS App 和 Homebrew 的常见安装位置。
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    for candidate in (
        "/Applications/Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析单行 SSE data。"""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    data = stripped[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _is_sse_done_line(line: str) -> bool:
    """判断当前 SSE 行是否为协议结束标记。"""
    stripped = line.strip()
    return stripped.startswith("data:") and stripped[5:].strip() == "[DONE]"


def _sse_body_events(body: str) -> list[dict[str, Any]]:
    """从完整 SSE body 中提取 JSON data 事件。"""
    # 用现有单行解析逻辑处理每一行，保持流式和缓冲解析行为一致。
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        event = _parse_sse_line(line)
        if event is not None:
            events.append(event)
    return events


def _non_sse_response_event(*, status_code: int, content_type: str, body: str) -> dict[str, Any]:
    """把非 SSE 响应转换为 completed 事件，无法解析时抛清晰错误。"""
    # 空 body 没有可聚合内容，通常代表代理或上游协议异常。
    if not body.strip():
        raise CodexUnexpectedResponseError(
            upstream_status_code=status_code,
            content_type=content_type,
            body=body,
        )

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        # 保留原异常链，日志里仍可深挖，但对外展示使用更友好的错误。
        raise CodexUnexpectedResponseError(
            upstream_status_code=status_code,
            content_type=content_type,
            body=body,
        ) from error

    # 只有 JSON object 能安全映射成 Responses completed 事件。
    if not isinstance(parsed, dict):
        raise CodexUnexpectedResponseError(
            upstream_status_code=status_code,
            content_type=content_type,
            body=body,
        )
    event_error = codex_event_error(parsed)
    if event_error is not None:
        raise event_error
    error_payload = _nested_error_payload(parsed)
    if error_payload is not None or parsed.get("status") == "failed":
        raise _codex_error_from_payload(
            error_payload or {},
            body=body,
            default_status_code=status_code if status_code >= 400 else None,
            default_message="Codex backend response failed",
        )
    return {"type": "response.completed", "response": parsed}


def codex_event_error(event: dict[str, Any]) -> CodexHTTPStatusError | None:
    """把 Responses 语义错误事件转换成可重试、可透传字段的异常。"""
    event_type = event.get("type")
    if event_type not in {"error", "response.failed"}:
        return None

    error_payload = event.get("error") if isinstance(event.get("error"), dict) else None
    if error_payload is None and event_type == "response.failed":
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            error_payload = response["error"]
    if error_payload is None:
        error_payload = {
            key: event[key]
            for key in ("message", "code", "param", "status_code")
            if key in event
        }
        if isinstance(event.get("error_type"), str):
            error_payload["type"] = event["error_type"]
    return _codex_error_from_payload(
        error_payload,
        body=json.dumps({"error": error_payload}, ensure_ascii=False),
        default_status_code=None,
        default_message="Codex backend response failed",
    )


def _nested_error_payload(value: dict[str, Any]) -> dict[str, Any] | None:
    """读取 OpenAI 错误 envelope 中的 error object。"""
    error = value.get("error")
    return error if isinstance(error, dict) else None


def _codex_error_from_payload(
    payload: dict[str, Any],
    *,
    body: str,
    default_status_code: int | None,
    default_message: str,
) -> CodexHTTPStatusError:
    """按 OpenAI 错误字段构造本地上游异常。"""
    error_code = payload.get("code") if isinstance(payload.get("code"), str) else None
    error_type = (
        payload.get("type")
        if isinstance(payload.get("type"), str)
        else _error_type_for_code(error_code)
    )
    error_message = payload.get("message") if isinstance(payload.get("message"), str) else None
    explicit_status = payload.get("status_code")
    status_code = (
        explicit_status
        if isinstance(explicit_status, int) and 400 <= explicit_status < 600
        else default_status_code
    )
    if status_code is None:
        status_code = _status_code_for_error(error_type, error_code)
    return CodexHTTPStatusError(
        status_code,
        body,
        error_type=error_type,
        error_code=error_code,
        error_message=error_message or default_message,
    )


def _status_code_for_error(error_type: str | None, error_code: str | None) -> int:
    """把 OpenAI 错误类型或错误码映射成兼容 HTTP 状态。"""
    if error_type == "service_unavailable_error" or error_code == "server_is_overloaded":
        return 503
    if error_type == "rate_limit_error" or error_code in {"rate_limit_exceeded", "requests"}:
        return 429
    if error_type == "authentication_error":
        return 401
    if error_type == "permission_error":
        return 403
    if error_type == "invalid_request_error":
        return 400
    return 502


def _error_type_for_code(error_code: str | None) -> str | None:
    """按常见 OpenAI 错误码补齐缺失的错误类型。"""
    if error_code == "server_is_overloaded":
        return "service_unavailable_error"
    if error_code in {"rate_limit_exceeded", "requests"}:
        return "rate_limit_error"
    return None


def _error_fields_from_body(body: str) -> tuple[str | None, str | None, str | None]:
    """从 JSON 错误响应体提取 type、code 和 message。"""
    try:
        parsed = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None
    error = parsed.get("error")
    candidate = error if isinstance(error, dict) else parsed
    error_type = candidate.get("type") if isinstance(candidate.get("type"), str) else None
    error_code = candidate.get("code") if isinstance(candidate.get("code"), str) else None
    message = candidate.get("message") if isinstance(candidate.get("message"), str) else None
    if message is None and isinstance(parsed.get("detail"), str):
        message = parsed["detail"]
    return error_type, error_code, message


def _body_snippet(body: str, limit: int = 500) -> str:
    """截取响应体片段，避免日志或 UI 展示过长内容。"""
    # 合并空白让 HTML 或多行文本在一行里也能看清关键内容。
    collapsed = " ".join(body.split())
    if not collapsed:
        return "<empty>"
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


def _is_retryable_error(error: Exception) -> bool:
    """判断错误是否适合自动重试。"""
    if isinstance(error, CodexHTTPStatusError):
        return error.status_code in RETRYABLE_HTTP_STATUS
    return isinstance(error, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError))
