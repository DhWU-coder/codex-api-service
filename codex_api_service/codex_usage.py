"""读取并脱敏 Codex 账号额度状态。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

AppServerRpc = Callable[[str, float], Awaitable[dict[str, Any]]]

APP_SERVER_TIMEOUT_SECONDS = 20.0
APP_SERVER_CLOSE_TIMEOUT_SECONDS = 5.0
PROCESS_EXIT_TIMEOUT_SECONDS = 2.0


class CodexUsageFetchError(RuntimeError):
    """表示 Codex 额度状态不可用。"""


class CodexUsageSession:
    """复用单个账号的 Codex app-server 额度读取会话。"""

    def __init__(self, *, account_home: str | Path | None) -> None:
        """保存永久账号目录，运行时 SQLite 将使用独立临时目录。"""
        self.account_home = Path(account_home).resolve() if account_home is not None else None
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._sqlite_home: tempfile.TemporaryDirectory[str] | None = None
        self._next_request_id = 1
        self._closed = False

    async def read_rate_limits(self, client_version: str, timeout: float) -> dict[str, Any]:
        """限制整个读取过程，锁等待、初始化和管道写入也计入超时。"""
        try:
            async with asyncio.timeout(timeout):
                return await self._read_rate_limits(client_version, timeout)
        except TimeoutError as error:
            raise CodexUsageFetchError("Codex usage status unavailable") from error

    async def _read_rate_limits(self, client_version: str, timeout: float) -> dict[str, Any]:
        """在串行会话内读取，失效时最多重启一次。"""
        async with self._lock:
            if self._closed:
                raise CodexUsageFetchError("Codex usage status unavailable")
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    await self._start_locked(client_version, timeout)
                    process = self._process
                    if process is None:
                        raise CodexUsageFetchError("Codex usage status unavailable")
                    request_id = self._take_request_id()
                    await _send_rpc(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "account/rateLimits/read",
                            "params": None,
                        },
                    )
                    return await _read_rpc_result(process, request_id, timeout)
                except asyncio.CancelledError:
                    # 总超时或停服取消后回收当前进程，下一次请求不能复用半截 RPC。
                    await self._stop_locked()
                    raise
                except Exception as error:
                    last_error = error
                    await self._stop_locked()
            raise CodexUsageFetchError("Codex usage status unavailable") from last_error

    async def close(self) -> None:
        """有界关闭会话，锁被卡住时直接终止该会话持有的进程。"""
        if self._closed:
            return
        self._closed = True
        try:
            async with asyncio.timeout(APP_SERVER_CLOSE_TIMEOUT_SECONDS):
                async with self._lock:
                    await self._stop_locked()
        except TimeoutError:
            await self._stop_locked()

    async def _start_locked(self, client_version: str, timeout: float) -> None:
        """按需启动并初始化 app-server；调用方必须持有会话锁。"""
        if self._process is not None and self._process.returncode is None:
            return
        await self._stop_locked()
        sqlite_home = tempfile.TemporaryDirectory(prefix="codex-api-service-sqlite-")
        environment = os.environ.copy()
        if self.account_home is not None:
            environment["CODEX_HOME"] = str(self.account_home)
        environment["CODEX_SQLITE_HOME"] = sqlite_home.name
        self._sqlite_home = sqlite_home
        try:
            self._process = await asyncio.create_subprocess_exec(
                "codex",
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # 独立进程组让 Node 启动器和原生子进程能够一起退出并关闭管道。
                start_new_session=os.name == "posix",
                env=environment,
            )
            request_id = self._take_request_id()
            await _send_rpc(
                self._process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "codex-api-service", "version": client_version},
                        "capabilities": None,
                    },
                },
            )
            await _read_rpc_result(self._process, request_id, timeout)
        except BaseException:
            await self._stop_locked()
            raise

    async def _stop_locked(self) -> None:
        """释放子进程和临时目录；调用方必须持有会话锁。"""
        process = self._process
        self._process = None
        sqlite_home = self._sqlite_home
        self._sqlite_home = None
        try:
            if process is not None:
                await _terminate_process(process)
        finally:
            if sqlite_home is not None:
                try:
                    sqlite_home.cleanup()
                except OSError:
                    pass

    def _take_request_id(self) -> int:
        """分配当前会话内单调递增的 JSON-RPC 请求 id。"""
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id


async def fetch_codex_usage_snapshot(
    *,
    client_version: str,
    app_server_rpc: AppServerRpc | None = None,
    account_home: str | None = None,
) -> dict[str, Any]:
    """通过 Codex app-server 读取当前账号额度，并返回脱敏摘要。"""
    try:
        if app_server_rpc is not None:
            payload = await app_server_rpc(client_version, APP_SERVER_TIMEOUT_SECONDS)
        else:
            payload = await _read_rate_limits_from_app_server(
                client_version,
                APP_SERVER_TIMEOUT_SECONDS,
                account_home=account_home,
            )
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


async def _read_rate_limits_from_app_server(
    client_version: str,
    timeout: float,
    *,
    account_home: str | None = None,
) -> dict[str, Any]:
    """使用一次性隔离会话，通过官方 RPC 读取额度快照。"""
    session = CodexUsageSession(account_home=account_home)
    try:
        return await session.read_rate_limits(client_version, timeout)
    finally:
        await session.close()


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
    """有界终止整组 app-server，避免子进程持有管道使 wait 永久等待。"""
    process_group = os.name == "posix" and getattr(process, "pid", None) is not None
    # 启动器即使已经退出，仍可能有持有管道的原生子进程留在该独立组内。
    if process.returncode is not None and not process_group:
        return
    for force in (False, True):
        try:
            if process_group:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            return
        except TimeoutError:
            # 强制退出后的等待同样有上限，不能继续拖住账号刷新。
            continue


def _rate_limit(value: Any) -> dict[str, Any]:
    """转换单组额度窗口。"""
    raw = value if isinstance(value, dict) else {}
    limit_reached = bool(raw.get("rateLimitReachedType"))
    windows = [
        _window(kind, item)
        for kind, item in (("primary", raw.get("primary")), ("secondary", raw.get("secondary")))
        if isinstance(item, dict)
    ]
    return {
        "allowed": not limit_reached,
        "limitReached": limit_reached,
        "windows": windows,
    }


def _window(kind: str, value: Any) -> dict[str, Any]:
    """转换实际存在的额度窗口，并限制百分比范围。"""
    raw = value if isinstance(value, dict) else {}
    used_percent = _bounded_percent(raw.get("usedPercent"))
    window_minutes = _integer(raw.get("windowDurationMins"))
    window_seconds = window_minutes * 60
    reset_at = _integer(raw.get("resetsAt"))
    return {
        "label": _window_label(window_minutes),
        "kind": kind,
        "usedPercent": used_percent,
        "remainingPercent": max(0, min(100, 100 - used_percent)),
        "limitWindowSeconds": window_seconds,
        "resetAfterSeconds": max(0, reset_at - int(time.time())) if reset_at else 0,
        "resetAt": reset_at,
    }


def _window_label(window_minutes: int) -> str:
    """根据窗口分钟数生成不会误导用户的显示标签。"""
    if window_minutes == 300:
        return "5h"
    if window_minutes == 10080:
        return "Weekly"
    if window_minutes > 0 and window_minutes % (24 * 60) == 0:
        return f"{window_minutes // (24 * 60)}d"
    if window_minutes > 0 and window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    if window_minutes > 0:
        return f"{window_minutes}m"
    return "额度窗口"


def _bounded_percent(value: Any) -> int:
    return max(0, min(100, _integer(value)))


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
