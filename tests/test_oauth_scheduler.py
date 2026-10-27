import asyncio
import time
from collections import Counter
from dataclasses import replace

import pytest

from codex_api_service.oauth_scheduler import AccountCandidate, NoAvailableOAuthAccounts, OAuthScheduler, account_dispatch_weight


def candidate(
    key: str,
    *,
    plan: str = "pro",
    remaining: float = 50,
    reset_minutes: float = 60,
    max_concurrency: int | None = None,
    weight_multiplier: float = 1,
) -> AccountCandidate:
    """构造可调度账号。"""
    now = 1_000_000.0
    return AccountCandidate(
        key=key,
        plan_type=plan,
        remaining_percent=remaining,
        resets_at=now + reset_minutes * 60,
        max_concurrency=max_concurrency,
        weight_multiplier=weight_multiplier,
    )


def test_dispatch_weight_uses_plan_remaining_and_reset_minutes() -> None:
    """验证套餐系数、剩余比例和分钟重置时间共同决定权重。"""
    pro = account_dispatch_weight(candidate("pro"), now=1_000_000)
    plus = account_dispatch_weight(candidate("plus", plan="plus"), now=1_000_000)
    business = account_dispatch_weight(candidate("business", plan="business"), now=1_000_000)
    unknown = account_dispatch_weight(candidate("unknown", plan="team"), now=1_000_000)

    assert pro == pytest.approx(0.5 / 60)
    assert plus == pytest.approx(pro * 0.05)
    assert business == pytest.approx(pro * 0.05)
    assert unknown == pytest.approx(pro * 0.05)
    assert account_dispatch_weight(candidate("zero", remaining=0, reset_minutes=0), now=1_000_000) == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_scheduler_uses_other_account_when_one_reaches_concurrency_limit() -> None:
    """验证账号满并发时普通请求会选择其他可用账号。"""
    scheduler = OAuthScheduler(global_max=2, queue_timeout_seconds=1, clock=lambda: 1_000_000)
    scheduler.update_accounts([candidate("a", max_concurrency=1), candidate("b", max_concurrency=1)])

    first = await scheduler.acquire(bound_account_key="a")
    second = await scheduler.acquire()

    assert first.account_key == "a"
    assert second.account_key == "b"
    await second.release()
    await first.release()


@pytest.mark.asyncio
async def test_bound_request_waits_for_its_original_account() -> None:
    """验证响应链请求不会因其他账号空闲而改变账号。"""
    scheduler = OAuthScheduler(global_max=None, queue_timeout_seconds=1, clock=time.time)
    scheduler.update_accounts([candidate("a", max_concurrency=1), candidate("b", max_concurrency=1)])
    first = await scheduler.acquire(bound_account_key="a")
    waiting = asyncio.create_task(scheduler.acquire(bound_account_key="a"))
    await asyncio.sleep(0)

    assert not waiting.done()
    await first.release()
    second = await waiting
    assert second.account_key == "a"
    await second.release()


@pytest.mark.asyncio
async def test_retry_can_exclude_already_attempted_account() -> None:
    """验证认证或限流切换不会在同一请求重复选择旧账号。"""
    scheduler = OAuthScheduler(global_max=None, queue_timeout_seconds=1, clock=lambda: 1_000_000)
    scheduler.update_accounts([candidate("a"), candidate("b")])

    lease = await scheduler.acquire(excluded_account_keys={"b"})

    assert lease.account_key == "a"
    await lease.release()


@pytest.mark.asyncio
async def test_active_snapshot_includes_waiting_queue_size() -> None:
    """验证并发槽满时快照会实时反映 FIFO 等待任务数。"""
    scheduler = OAuthScheduler(global_max=1, queue_timeout_seconds=1, clock=lambda: 1_000_000)
    scheduler.update_accounts([candidate("a")])
    first = await scheduler.acquire()
    waiting = asyncio.create_task(scheduler.acquire())
    await asyncio.sleep(0)

    global_active, account_active, waiting_count = scheduler.active_snapshot()

    assert global_active == 1
    assert account_active == {"a": 1}
    assert waiting_count == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await first.release()
    assert scheduler.active_snapshot()[2] == 0


@pytest.mark.asyncio
async def test_waiting_request_fails_when_last_account_loses_authorization() -> None:
    """验证排队期间唯一账号失效后立即报 503，不会继续等待默认十分钟。"""
    scheduler = OAuthScheduler(global_max=1, queue_timeout_seconds=600)
    scheduler.update_accounts([candidate("a")])
    lease = await scheduler.acquire()
    waiting = asyncio.create_task(scheduler.acquire())
    await asyncio.sleep(0)
    scheduler.update_accounts([AccountCandidate(key="a", status="login_required")])
    await lease.release()
    with pytest.raises(NoAvailableOAuthAccounts):
        await asyncio.wait_for(waiting, timeout=1)
    assert scheduler.active_snapshot()[0] == scheduler.active_snapshot()[2] == 0


@pytest.mark.parametrize("multiplier", [0.000001, 0.3, 1, 1.25, 7.5, 1_000_000])
def test_dispatch_weight_applies_custom_multiplier(multiplier: float) -> None:
    """验证倍率只乘在原公式上，默认 1 倍完全兼容。"""
    original = account_dispatch_weight(candidate("original"), now=1_000_000)
    adjusted = account_dispatch_weight(candidate("adjusted", weight_multiplier=multiplier), now=1_000_000)
    assert adjusted == pytest.approx(original * multiplier, rel=1e-12, abs=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("multiplier", "requests", "expected"), [(0.3, 130, 30), (1.25, 90, 50), (7.5, 170, 150)])
async def test_custom_multiplier_changes_actual_dispatch_proportions(multiplier: float, requests: int, expected: int) -> None:
    """验证实际轮询比例随自定义小数变化，而不是只改页面数字。"""
    scheduler = OAuthScheduler(global_max=None, clock=lambda: 1_000_000)
    scheduler.update_accounts([candidate("custom", weight_multiplier=multiplier), candidate("default")])
    counts: Counter[str] = Counter()
    for _ in range(requests):
        lease = await scheduler.acquire()
        counts[lease.account_key] += 1
        await lease.release()
    assert counts["custom"] == expected
    assert counts["default"] == requests - expected
    assert scheduler.active_snapshot()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["login_required", "auth_error", "rate_limited", "disabled"])
async def test_high_multiplier_never_bypasses_unavailable_account(status: str) -> None:
    """验证高倍率不绕过失效、停用或限流状态。"""
    scheduler = OAuthScheduler(global_max=None, clock=lambda: 1_000_000)
    blocked = replace(candidate("blocked", weight_multiplier=1_000_000),
                      status="available" if status == "disabled" else status, enabled=status != "disabled")
    scheduler.update_accounts([blocked, candidate("healthy", weight_multiplier=0.000001)])
    lease = await scheduler.acquire()
    assert lease.account_key == "healthy"
    await lease.release()


@pytest.mark.asyncio
async def test_multiplier_change_resets_scores_but_keeps_active_slots_and_binding() -> None:
    """验证倍率修改立即清除旧累积分数，但不改变在途计数及绑定账号。"""
    scheduler = OAuthScheduler(global_max=2, clock=lambda: 1_000_000)
    first, second = candidate("a"), candidate("b", weight_multiplier=1_000_000)
    scheduler.update_accounts([first, second])
    lease = await scheduler.acquire(bound_account_key="a")
    scheduler._smooth_current = {"a": 500, "b": -500}
    scheduler.update_accounts([replace(first, weight_multiplier=0.000001), second])
    assert scheduler._smooth_current == {"a": 0, "b": 0}
    assert scheduler.active_snapshot()[0] == 1
    bound = await scheduler.acquire(bound_account_key="a")
    assert bound.account_key == "a"
    await bound.release()
    await lease.release()
    assert scheduler.active_snapshot()[0] == 0
