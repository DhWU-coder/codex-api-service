import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import codex_api_service.codex_usage as codex_usage
from codex_api_service.codex_usage import (
    CodexUsageFetchError,
    fetch_codex_usage_snapshot,
    summarize_codex_usage,
)


class FakeAppServerStdout:
    """模拟 app-server 按行输出 JSON-RPC 响应。"""

    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        """返回下一行已排队响应。"""
        return await self.lines.get()


class FakeAppServerStdin:
    """解析测试请求并立即生成同 id 响应。"""

    def __init__(self, process: "FakeAppServerProcess") -> None:
        self.process = process

    def write(self, data: bytes) -> None:
        """记录请求，并为初始化或额度读取生成完整响应。"""
        message = json.loads(data.decode("utf-8"))
        self.process.requests.append(message)
        if message["method"] == "account/rateLimits/read" and self.process.fail_rate_limit:
            self.process.stdout.lines.put_nowait(b"")
            return
        result = {} if message["method"] == "initialize" else raw_usage_payload()
        response = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n"
        self.process.stdout.lines.put_nowait(response.encode("utf-8"))

    async def drain(self) -> None:
        """模拟异步缓冲区刷新完成。"""
        return


class FakeAppServerProcess:
    """提供额度会话测试需要的最小异步子进程行为。"""

    def __init__(self, *, fail_rate_limit: bool = False) -> None:
        self.stdout = FakeAppServerStdout()
        self.stdin = FakeAppServerStdin(self)
        self.requests: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self.terminated = False
        self.fail_rate_limit = fail_rate_limit

    def terminate(self) -> None:
        """记录正常终止并设置退出码。"""
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        """记录强制终止并设置退出码。"""
        self.terminated = True
        self.returncode = -9

    async def wait(self) -> int:
        """返回当前退出码。"""
        return self.returncode or 0


def raw_usage_payload() -> dict[str, object]:
    """构造 Codex app-server rateLimits 接口的代表性响应。"""
    return {
        "rateLimits": {
            "limitId": "codex",
            "limitName": None,
            "primary": {"usedPercent": 33, "windowDurationMins": 300, "resetsAt": 1783814968},
            "secondary": {"usedPercent": 21, "windowDurationMins": 10080, "resetsAt": 1784370805},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "pro",
            "rateLimitReachedType": None,
        },
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": None,
                "primary": {"usedPercent": 33, "windowDurationMins": 300, "resetsAt": 1783814968},
                "secondary": {"usedPercent": 21, "windowDurationMins": 10080, "resetsAt": 1784370805},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "planType": "pro",
                "rateLimitReachedType": None,
            },
            "codex_bengalfox": {
                "limitId": "codex_bengalfox",
                "limitName": "GPT-5.3-Codex-Spark",
                "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1783817589},
                "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1784393589},
                "credits": None,
                "planType": "pro",
                "rateLimitReachedType": None,
            },
        },
        "rateLimitResetCredits": None,
    }


def test_summarize_codex_usage_keeps_limit_windows_and_removes_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证额度摘要保留重置时间，但不泄露账号身份字段。"""
    monkeypatch.setattr(codex_usage.time, "time", lambda: 1783813768)

    summary = summarize_codex_usage(raw_usage_payload())

    assert summary["planType"] == "pro"
    assert summary["rateLimit"]["windows"][0] == {
        "label": "5h",
        "kind": "primary",
        "usedPercent": 33,
        "remainingPercent": 67,
        "limitWindowSeconds": 18000,
        "resetAfterSeconds": 1200,
        "resetAt": 1783814968,
    }
    assert summary["rateLimit"]["windows"][1]["label"] == "Weekly"
    assert summary["additionalRateLimits"][0]["limitName"] == "GPT-5.3-Codex-Spark"
    assert summary["additionalRateLimits"][0]["rateLimit"]["windows"][0]["remainingPercent"] == 100
    assert summary["credits"] == {
        "hasCredits": False,
        "unlimited": False,
        "overageLimitReached": False,
        "balance": "0",
    }
    serialized = json.dumps(summary)
    assert "secret@example.com" not in serialized
    assert "acct_secret" not in serialized
    assert "user_secret" not in serialized


def test_summarize_codex_usage_uses_actual_duration_and_omits_missing_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证只有周窗口的账号不会被误报为 5 小时加一个虚假满额窗口。"""
    monkeypatch.setattr(codex_usage.time, "time", lambda: 1_783_813_768)
    payload = raw_usage_payload()
    weekly_only = {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 1_789_057_153},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "pro",
        "rateLimitReachedType": None,
    }
    payload["rateLimits"] = weekly_only
    payload["rateLimitsByLimitId"] = {"codex": weekly_only}

    summary = summarize_codex_usage(payload)

    assert summary["rateLimit"]["windows"] == [
        {
            "label": "Weekly",
            "kind": "primary",
            "usedPercent": 12,
            "remainingPercent": 88,
            "limitWindowSeconds": 604800,
            "resetAfterSeconds": 5_243_385,
            "resetAt": 1_789_057_153,
        }
    ]


@pytest.mark.parametrize(
    ("duration_minutes", "expected_label"),
    [(300, "5h"), (10080, "Weekly"), (1440, "1d"), (120, "2h"), (90, "90m"), (0, "额度窗口")],
)
def test_summarize_codex_usage_formats_other_window_durations(
    duration_minutes: int,
    expected_label: str,
) -> None:
    """验证未知窗口时长仍能生成可读且不误导的标签。"""
    payload = raw_usage_payload()
    primary = payload["rateLimits"]
    assert isinstance(primary, dict)
    primary["primary"] = {
        "usedPercent": 10,
        "windowDurationMins": duration_minutes,
        "resetsAt": 1_789_057_153,
    }
    primary["secondary"] = None
    payload["rateLimitsByLimitId"] = {}

    summary = summarize_codex_usage(payload)

    assert summary["rateLimit"]["windows"][0]["label"] == expected_label


@pytest.mark.asyncio
async def test_fetch_codex_usage_snapshot_reads_app_server_without_leaking_errors() -> None:
    """验证额度读取走 Codex app-server，错误信息不会透传内部细节。"""
    captured: dict[str, object] = {}

    async def fake_rpc(client_version: str, timeout: float) -> dict[str, object]:
        captured["client_version"] = client_version
        captured["timeout"] = timeout
        return raw_usage_payload()

    summary = await fetch_codex_usage_snapshot(client_version="0.144.1", app_server_rpc=fake_rpc)

    assert captured["client_version"] == "0.144.1"
    assert captured["timeout"] == codex_usage.APP_SERVER_TIMEOUT_SECONDS
    assert summary["rateLimit"]["windows"][0]["remainingPercent"] == 67

    async def failing_rpc(client_version: str, timeout: float) -> dict[str, object]:
        raise RuntimeError("access-secret should never surface")

    with pytest.raises(CodexUsageFetchError) as error:
        await fetch_codex_usage_snapshot(client_version="0.144.1", app_server_rpc=failing_rpc)
    assert "access-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_usage_session_reuses_process_and_isolates_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证连续额度读取复用进程，且关闭时只清理临时 SQLite。"""
    account_home = tmp_path / "account"
    account_home.mkdir()
    auth_path = account_home / "auth.json"
    original_auth = '{"auth_mode":"chatgpt","tokens":{"refresh_token":"refresh-test"}}\n'
    auth_path.write_text(original_auth, encoding="utf-8")
    created: list[tuple[tuple[object, ...], dict[str, object], FakeAppServerProcess]] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeAppServerProcess:
        """捕获 app-server 命令和环境，并返回可交互假进程。"""
        process = FakeAppServerProcess()
        created.append((args, kwargs, process))
        return process

    monkeypatch.setattr(codex_usage.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    session = codex_usage.CodexUsageSession(account_home=account_home)

    first = await session.read_rate_limits("0.144.5", 1.0)
    second = await session.read_rate_limits("0.144.5", 1.0)

    assert first["rateLimits"]["planType"] == "pro"
    assert second["rateLimits"]["planType"] == "pro"
    assert len(created) == 1
    args, kwargs, process = created[0]
    assert args == ("codex", "app-server", "--stdio")
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == str(account_home)
    sqlite_home = Path(environment["CODEX_SQLITE_HOME"])
    assert sqlite_home != account_home
    assert sqlite_home.is_dir()

    await session.close()

    assert process.terminated is True
    assert not sqlite_home.exists()
    assert auth_path.read_text(encoding="utf-8") == original_auth


@pytest.mark.asyncio
async def test_usage_session_restarts_once_after_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证额度 RPC 遇到 EOF 后重启一次并完成当前读取。"""
    created = [FakeAppServerProcess(fail_rate_limit=True), FakeAppServerProcess()]
    create_count = 0

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> FakeAppServerProcess:
        """依次返回失败进程和恢复进程。"""
        nonlocal create_count
        process = created[create_count]
        create_count += 1
        return process

    monkeypatch.setattr(codex_usage.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    session = codex_usage.CodexUsageSession(account_home=tmp_path)

    result = await session.read_rate_limits("0.144.5", 1.0)
    await session.close()

    assert result["rateLimits"]["planType"] == "pro"
    assert create_count == 2
    assert created[0].terminated is True


@pytest.mark.asyncio
async def test_usage_session_returns_safe_error_after_second_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证两次进程都失效时只返回稳定安全错误。"""
    created = [
        FakeAppServerProcess(fail_rate_limit=True),
        FakeAppServerProcess(fail_rate_limit=True),
    ]
    create_count = 0

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> FakeAppServerProcess:
        """为两次允许的启动都返回额度读取失败进程。"""
        nonlocal create_count
        process = created[create_count]
        create_count += 1
        return process

    monkeypatch.setattr(codex_usage.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    session = codex_usage.CodexUsageSession(account_home=tmp_path)

    with pytest.raises(CodexUsageFetchError, match="Codex usage status unavailable") as error:
        await session.read_rate_limits("0.144.5", 1.0)
    await session.close()

    assert create_count == 2
    assert "refresh-test-secret" not in str(error.value)
    assert all(process.terminated for process in created)


@pytest.mark.asyncio
async def test_usage_session_serializes_concurrent_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证同账号并发刷新共享进程，并按请求顺序读取响应。"""
    process = FakeAppServerProcess()
    create_count = 0

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> FakeAppServerProcess:
        """始终返回同一个假进程，并记录真实启动次数。"""
        nonlocal create_count
        create_count += 1
        return process

    monkeypatch.setattr(codex_usage.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    session = codex_usage.CodexUsageSession(account_home=tmp_path)

    first, second = await asyncio.gather(
        session.read_rate_limits("0.144.5", 1.0),
        session.read_rate_limits("0.144.5", 1.0),
    )
    await session.close()

    assert first["rateLimits"]["planType"] == "pro"
    assert second["rateLimits"]["planType"] == "pro"
    assert create_count == 1
    assert [request["method"] for request in process.requests] == [
        "initialize",
        "account/rateLimits/read",
        "account/rateLimits/read",
    ]
