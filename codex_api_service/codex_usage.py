"""读取并脱敏 Codex 账号额度状态。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

AppServerRpc = Callable[[str, float], Awaitable[dict[str, Any]]]

APP_SERVER_TIMEOUT_SECONDS = 20.0


class CodexUsageFetchError(RuntimeError):
    """表示 Codex 额度状态不可用。"""


async def fetch_codex_usage_snapshot(
    *,
    client_version: str,
    app_server_rpc: AppServerRpc | None = None,
) -> dict[str, Any]:
    """通过 Codex app-server 读取当前账号额度，并返回脱敏摘要。"""
    rpc = app_server_rpc or _read_rate_limits_from_app_server
    try:
        payload = await rpc(client_version, APP_SERVER_TIMEOUT_SECONDS)
    except Exception as error:  # pragma: no cover - 真实 app-server 异常依赖本机 Codex 安装。
        raise CodexUsageFetchError("Codex usage status unavailable") from error
    return summarize_codex_usage(payload)


def summarize_codex_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """把 Codex app-server rateLimits 响应转换成前端稳定结构。"""
    rate_limits = payload.get("rateLimits") if isinstance(payload.get("rateLimits"), dict) else {}
    by_limit_id = payload.get("rateLimitsByLimitId") if isinstance(payload.get("rateLimitsByLimitId"), dict) else {}
    primary_limit = by_limit_id.get("codex") if isinstance(by_limit_id.get("codex"), dict) else rate_limits

    additional_limits = []
    for limit_id, item in by_limit_id.items():
        if limit_id == "codex" or not isinstance(item, dict):
            continue
        additional_limits.append(
            {
                "limitName": str(item.get("limitName") or limit_id or "额外额度"),
                "meteredFeature": str(limit_id or ""),
                "rateLimit": _rate_limit(item),
            }
        )

    credits = primary_limit.get("credits") if isinstance(primary_limit.get("credits"), dict) else {}
    return {
        "planType": str(primary_limit.get("planType") or "-"),
        "rateLimit": _rate_limit(primary_limit),
        "additionalRateLimits": additional_limits,
        "credits": {
            "hasCredits": bool(credits.get("hasCredits") or credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "overageLimitReached": bool(credits.get("overageLimitReached") or credits.get("overage_limit_reached")),
            "balance": str(credits.get("balance") or "0"),
        },
    }


async def _read_rate_limits_from_app_server(client_version: str, timeout: float) -> dict[str, Any]:
    """短暂启动 Codex app-server，通过官方 RPC 读取额度快照。"""
    process = await asyncio.create_subprocess_exec(
        "codex",
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _send_rpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-api-service", "version": client_version},
                    "capabilities": None,
                },
            },
        )
        await _read_rpc_result(process, 1, timeout)
        await _send_rpc(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
        )
        return await _read_rpc_result(process, 2, timeout)
    finally:
        await _terminate_process(process)


async def _send_rpc(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    """向 app-server 写入单行 JSON-RPC 请求。"""
    if process.stdin is None:
        raise CodexUsageFetchError("Codex usage status unavailable")
    process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    await process.stdin.drain()


async def _read_rpc_result(process: asyncio.subprocess.Process, request_id: int, timeout: float) -> dict[str, Any]:
    """读取指定 JSON-RPC id 的响应，跳过通知消息。"""
    if process.stdout is None:
        raise CodexUsageFetchError("Codex usage status unavailable")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        if not line:
            break
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise CodexUsageFetchError("Codex usage status unavailable")
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexUsageFetchError("Codex usage status unavailable")
        return result
    raise CodexUsageFetchError("Codex usage status unavailable")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """结束临时 app-server 进程，避免后台残留。"""
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def _rate_limit(value: Any) -> dict[str, Any]:
    """转换单组额度窗口。"""
    raw = value if isinstance(value, dict) else {}
    limit_reached = bool(raw.get("rateLimitReachedType"))
    return {
        "allowed": not limit_reached,
        "limitReached": limit_reached,
        "windows": [
            _window("5h", "primary", raw.get("primary")),
            _window("Weekly", "secondary", raw.get("secondary")),
        ],
    }


def _window(label: str, kind: str, value: Any) -> dict[str, Any]:
    """转换 5h 或 weekly 窗口，并限制百分比范围。"""
    raw = value if isinstance(value, dict) else {}
    used_percent = _bounded_percent(raw.get("usedPercent"))
    window_seconds = _integer(raw.get("windowDurationMins")) * 60
    reset_at = _integer(raw.get("resetsAt"))
    return {
        "label": label,
        "kind": kind,
        "usedPercent": used_percent,
        "remainingPercent": max(0, min(100, 100 - used_percent)),
        "limitWindowSeconds": window_seconds,
        "resetAfterSeconds": max(0, reset_at - int(time.time())) if reset_at else 0,
        "resetAt": reset_at,
    }


def _bounded_percent(value: Any) -> int:
    return max(0, min(100, _integer(value)))


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
