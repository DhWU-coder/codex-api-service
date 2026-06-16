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

# 这些状态通常是临时上游错误，可以在没有输出任何事件前重试。
RETRYABLE_HTTP_STATUS = {502, 503, 504}


class CodexHTTPStatusError(RuntimeError):
    """携带 HTTP 状态码的 Codex 请求错误。"""

    def __init__(self, status_code: int, body: str) -> None:
        """保存状态码和响应体片段供重试判断与报错。"""
        self.status_code = status_code
        self.body = body
        super().__init__(f"Codex request failed: HTTP {status_code}: {body}")


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
        completed: dict[str, Any] | None = None
        async for event in self.stream_response(stream_payload):
            if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
                text_deltas.append(event["delta"])
            response = event.get("response")
            if event.get("type") == "response.completed" and isinstance(response, dict):
                completed = response

        # completed 事件最完整；如果缺 output_text，则用已收到的 delta 补齐。
        if completed is not None:
            if "output_text" not in completed and text_deltas:
                completed = {**completed, "output_text": "".join(text_deltas)}
            return completed
        return {"id": "resp_local_aggregation", "output_text": "".join(text_deltas)}

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
        # ensure_credentials 会在必要时刷新 token 或触发首次登录。
        credentials = self.auth.ensure_credentials()
        request_payload = dict(payload)
        request_payload["stream"] = True

        # read=None 允许长时间流式输出；connect 仍保留超时避免卡在握手。
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=30.0, read=None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                self.config.responses_url,
                headers=_codex_headers(credentials.access),
                json=request_payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise CodexHTTPStatusError(response.status_code, body)

                # 非 SSE JSON 响应也兼容成 completed 事件，便于后续扩展。
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    buffered_events = _sse_body_events(body)
                    if buffered_events:
                        # 某些上游响应漏掉 content-type，但 body 仍是标准 SSE 行。
                        for event in buffered_events:
                            yield event
                        return
                    yield _non_sse_response_event(
                        status_code=response.status_code,
                        content_type=content_type,
                        body=body,
                    )
                    return

                # SSE 每个 data 行都是一个 JSON event；[DONE] 表示流结束。
                async for line in response.aiter_lines():
                    event = _parse_sse_line(line)
                    if event is None:
                        continue
                    yield event


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
    return {"type": "response.completed", "response": parsed}


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
