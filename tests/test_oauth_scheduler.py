import asyncio
import time

import pytest

from codex_api_service.oauth_scheduler import AccountCandidate, OAuthScheduler, account_dispatch_weight


def candidate(
    key: str,
    *,
    plan: str = "pro",
    remaining: float = 50,
    reset_minutes: float = 60,
    max_concurrency: int | None = None,
) -> AccountCandidate:
    """构造可调度账号。"""
    now = 1_000_000.0
    return AccountCandidate(
        key=key,
        plan_type=plan,
        remaining_percent=remaining,
        resets_at=now + reset_minutes * 60,
        max_concurrency=max_concurrency,
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
