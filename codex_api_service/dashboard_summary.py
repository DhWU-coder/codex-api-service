"""在服务端聚合请求日志，避免浏览器下载完整历史。"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable


DASHBOARD_RANGES = {"today", "week", "month", "all", "recent"}


def summarize_request_logs(
    items: list[dict[str, Any]],
    *,
    range_preset: str,
    recent_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按前端现有口径返回完整看板摘要。"""
    if range_preset not in DASHBOARD_RANGES:
        raise ValueError(f"unsupported dashboard range: {range_preset}")

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    filtered = _filter_by_range(items, range_preset, recent_days, current)
    request_count = len(filtered)
    success_count = sum(1 for item in filtered if 200 <= _integer(item.get("status_code")) < 400)
    total_duration = sum(_integer(item.get("duration_ms")) for item in filtered)
    token_breakdown = {
        "input": sum(_usage_value(item, "input") for item in filtered),
        "cached": sum(_usage_value(item, "cached") for item in filtered),
        "output": sum(_usage_value(item, "output") for item in filtered),
        "reasoning": sum(_usage_value(item, "reasoning") for item in filtered),
    }
    total_tokens = sum(_usage_value(item, "total") for item in filtered)
    bucket = "hour" if range_preset == "today" else "day"
    model_distribution = _distribution(filtered, lambda item: str(item.get("model") or "未知模型"))

    return {
        "requestCount": request_count,
        "successCount": success_count,
        "errorCount": request_count - success_count,
        "successRate": round(success_count / request_count * 100) if request_count else 0,
        "averageDurationMs": round(total_duration / request_count) if request_count else 0,
        "totalTokens": total_tokens,
        "tokenBreakdown": token_breakdown,
        "topModel": model_distribution[0]["name"] if model_distribution else "-",
        "trend": _recent_trend(filtered),
        "timeline": _timeline(filtered, bucket, current),
        "modelDistribution": model_distribution,
        "statusDistribution": _status_distribution(filtered),
        "endpointDistribution": _distribution(filtered, lambda item: str(item.get("path") or "-")),
        "recentFailures": _newest_first(
            [item for item in filtered if _integer(item.get("status_code")) >= 400]
        )[:5],
        "slowRequests": sorted(filtered, key=lambda item: _integer(item.get("duration_ms")), reverse=True)[:5],
        "rangeLabel": _range_label(range_preset, recent_days),
        "bucketLabel": "按小时" if bucket == "hour" else "按天",
        "lastUpdated": _last_updated(filtered),
    }


def _filter_by_range(
    items: list[dict[str, Any]],
    range_preset: str,
    recent_days: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """在服务本地时区过滤看板范围。"""
    if range_preset == "all":
        return list(items)

    local_now = now.astimezone()
    if range_preset == "today":
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_preset == "week":
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start - timedelta(days=day_start.weekday())
    elif range_preset == "month":
        start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = local_now - timedelta(days=max(1, recent_days))

    filtered: list[dict[str, Any]] = []
    for item in items:
        timestamp = _timestamp(item)
        if timestamp is None:
            continue
        local_timestamp = timestamp.astimezone(local_now.tzinfo)
        if start <= local_timestamp <= local_now:
            filtered.append(item)
    return filtered


def _distribution(
    items: list[dict[str, Any]],
    key_getter: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    """按 token 总量、请求数降序生成通用分布。"""
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {"totalTokens": 0, "requestCount": 0})
    for item in items:
        key = key_getter(item) or "-"
        groups[key]["totalTokens"] += _usage_value(item, "total")
        groups[key]["requestCount"] += 1
    result = [{"name": name, **values} for name, values in groups.items()]
    return sorted(result, key=lambda row: (-row["totalTokens"], -row["requestCount"], row["name"]))


def _status_distribution(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成成功与失败两组固定状态分布。"""
    successful = [item for item in items if _integer(item.get("status_code")) < 400]
    failed = [item for item in items if _integer(item.get("status_code")) >= 400]
    return [
        {
            "name": "成功",
            "totalTokens": sum(_usage_value(item, "total") for item in successful),
            "requestCount": len(successful),
        },
        {
            "name": "失败",
            "totalTokens": sum(_usage_value(item, "total") for item in failed),
            "requestCount": len(failed),
        },
    ]


def _timeline(items: list[dict[str, Any]], bucket: str, now: datetime) -> list[dict[str, Any]]:
    """把成功且带 usage 的请求聚合到小时或日期桶。"""
    groups: dict[str, dict[str, Any]] = {}
    for item in _oldest_first(items):
        if _integer(item.get("status_code")) >= 400 or not isinstance(item.get("usage"), dict):
            continue
        timestamp = _timestamp(item)
        if timestamp is None:
            continue
        local = timestamp.astimezone(now.astimezone().tzinfo)
        day_key = local.strftime("%Y-%m-%d")
        key = f"{day_key} {local:%H}" if bucket == "hour" else day_key
        label = local.strftime("%H:00") if bucket == "hour" else local.strftime("%m/%d")
        row = groups.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "totalTokens": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "requestCount": 0,
            },
        )
        row["totalTokens"] += _usage_value(item, "total")
        row["inputTokens"] += _usage_value(item, "input")
        row["outputTokens"] += _usage_value(item, "output")
        row["requestCount"] += 1
    return list(groups.values())


def _recent_trend(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最近二十条趋势按旧到新返回。"""
    recent = items[:20]
    return [
        {
            "id": str(item.get("id") or ""),
            "timestamp": str(item.get("timestamp") or ""),
            "totalTokens": _usage_value(item, "total"),
            "statusCode": _integer(item.get("status_code")),
        }
        for item in reversed(recent)
    ]


def _last_updated(items: list[dict[str, Any]]) -> str:
    """返回最新有效时间戳的原始字符串。"""
    ordered = _newest_first(items)
    return str(ordered[0].get("timestamp") or "-") if ordered else "-"


def _newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: _timestamp(item) or datetime.min.replace(tzinfo=UTC), reverse=True)


def _oldest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: _timestamp(item) or datetime.max.replace(tzinfo=UTC))


def _timestamp(item: dict[str, Any]) -> datetime | None:
    value = item.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _usage_value(item: dict[str, Any], field: str) -> int:
    usage = item.get("usage")
    return _integer(usage.get(field)) if isinstance(usage, dict) else 0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _range_label(range_preset: str, recent_days: int) -> str:
    labels = {"today": "今日", "week": "本周", "month": "本月", "all": "全部"}
    return f"最近 {recent_days} 天" if range_preset == "recent" else labels[range_preset]
