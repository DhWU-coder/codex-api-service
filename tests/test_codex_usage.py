import json

import pytest

import codex_api_service.codex_usage as codex_usage
from codex_api_service.codex_usage import (
    CodexUsageFetchError,
    fetch_codex_usage_snapshot,
    summarize_codex_usage,
)


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
