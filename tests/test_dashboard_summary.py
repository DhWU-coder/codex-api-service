"""测试服务端请求日志看板聚合。"""

from datetime import UTC, datetime

import pytest

from codex_api_service.dashboard_summary import summarize_request_logs


def log_item(
    item_id: str,
    timestamp: str,
    *,
    status_code: int,
    duration_ms: int,
    model: str,
    path: str,
    total: int | None,
) -> dict[str, object]:
    """构造不包含请求正文的聚合测试日志。"""
    usage = None
    if total is not None:
        usage = {"total": total, "input": total - 10, "cached": 5, "output": 10, "reasoning": 3}
    return {
        "id": item_id,
        "timestamp": timestamp,
        "method": "POST",
        "path": path,
        "model": model,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "usage": usage,
        "request_id": f"resp_{item_id}",
        "error": "upstream failed" if status_code >= 400 else None,
    }


def sample_logs() -> list[dict[str, object]]:
    """提供覆盖今日、本周和本月边界的三条日志。"""
    return [
        log_item(
            "today",
            "2026-07-11T10:00:00Z",
            status_code=200,
            duration_ms=100,
            model="gpt-5.6-sol",
            path="/v1/chat/completions",
            total=100,
        ),
        log_item(
            "yesterday",
            "2026-07-10T09:00:00Z",
            status_code=500,
            duration_ms=900,
            model="gpt-5.5",
            path="/v1/responses",
            total=None,
        ),
        log_item(
            "month",
            "2026-07-01T08:00:00Z",
            status_code=200,
            duration_ms=500,
            model="gpt-5.5",
            path="/v1/responses",
            total=50,
        ),
    ]


@pytest.mark.parametrize(
    ("range_preset", "recent_days", "expected_count"),
    [("today", 7, 1), ("week", 7, 2), ("month", 7, 3), ("recent", 2, 2), ("all", 7, 3)],
)
def test_dashboard_summary_filters_each_range(
    range_preset: str,
    recent_days: int,
    expected_count: int,
) -> None:
    """验证五种看板范围使用完整历史并返回正确数量。"""
    summary = summarize_request_logs(
        sample_logs(),
        range_preset=range_preset,
        recent_days=recent_days,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert summary["requestCount"] == expected_count


def test_dashboard_summary_calculates_metrics_and_insights() -> None:
    """验证 token、分布、失败和慢请求等完整聚合字段。"""
    summary = summarize_request_logs(
        sample_logs(),
        range_preset="all",
        recent_days=7,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert summary["successCount"] == 2
    assert summary["errorCount"] == 1
    assert summary["successRate"] == 67
    assert summary["averageDurationMs"] == 500
    assert summary["totalTokens"] == 150
    assert summary["tokenBreakdown"] == {"input": 130, "cached": 10, "output": 20, "reasoning": 6}
    assert summary["topModel"] == "gpt-5.6-sol"
    assert summary["recentFailures"][0]["id"] == "yesterday"
    assert summary["slowRequests"][0]["id"] == "yesterday"
    assert summary["modelDistribution"][0] == {
        "name": "gpt-5.6-sol",
        "totalTokens": 100,
        "requestCount": 1,
    }
    assert summary["statusDistribution"][0]["name"] == "成功"
    assert summary["endpointDistribution"][0]["name"] == "/v1/chat/completions"
    assert summary["timeline"][0]["key"] == "2026-07-01"
    assert summary["lastUpdated"] == "2026-07-11T10:00:00Z"


def test_dashboard_summary_rejects_unknown_range() -> None:
    """验证非法范围不会静默退化成全部历史。"""
    with pytest.raises(ValueError, match="unsupported dashboard range"):
        summarize_request_logs(sample_logs(), range_preset="invalid", recent_days=7)
