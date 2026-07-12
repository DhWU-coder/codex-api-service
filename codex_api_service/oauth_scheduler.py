"""多 OAuth 账号的额度权重、并发限制与等待队列。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


PLAN_FACTORS = {"pro": 1.0, "plus": 0.05, "business": 0.05}


@dataclass(frozen=True)
class AccountCandidate:
    """描述调度器需要的账号非敏感状态。"""

    key: str
    plan_type: str = "unknown"
    remaining_percent: float = 100.0
    resets_at: float = 0.0
    enabled: bool = True
    status: str = "available"
    max_concurrency: int | None = None


class BoundAccountUnavailable(RuntimeError):
    """表示响应链绑定账号当前不能继续请求。"""

    status_code = 409
    error_code = "previous_response_account_unavailable"


class QueueWaitTimeout(RuntimeError):
    """表示请求等待并发槽位超时。"""


def account_dispatch_weight(account: AccountCandidate, *, now: float | None = None) -> float:
    """按套餐、5 小时剩余比例和重置分钟数计算账号权重。"""
    current = time.time() if now is None else now
    plan_factor = PLAN_FACTORS.get(account.plan_type.strip().lower(), 0.05)
    remaining_ratio = max(float(account.remaining_percent) / 100.0, 0.001)
    reset_minutes = max((float(account.resets_at) - current) / 60.0, 1.0)
    return plan_factor * remaining_ratio / reset_minutes


class AccountLease:
    """表示一次请求占用的全局和账号并发槽位。"""

    def __init__(self, scheduler: "OAuthScheduler", account_key: str) -> None:
        self._scheduler = scheduler
        self.account_key = account_key
        self._released = False

    async def release(self) -> None:
        """幂等释放当前请求占用的槽位。"""
        if self._released:
            return
        self._released = True
        await self._scheduler._release(self.account_key)

    async def __aenter__(self) -> "AccountLease":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()


class OAuthScheduler:
    """使用平滑加权轮询分配账号，并原子执行两级并发限制。"""

    def __init__(
        self,
        *,
        global_max: int | None,
        queue_timeout_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.global_max = global_max
        self.queue_timeout_seconds = queue_timeout_seconds
        self.clock = clock
        self._accounts: dict[str, AccountCandidate] = {}
        self._account_active: dict[str, int] = {}
        self._global_active = 0
        self._smooth_current: dict[str, float] = {}
        self._condition = asyncio.Condition()
        self._waiters: deque[object] = deque()

    def update_accounts(self, accounts: list[AccountCandidate]) -> None:
        """替换调度账号快照，同时保留仍存在账号的累计权重和计数。"""
        self._accounts = {item.key: item for item in accounts}
        self._account_active = {key: self._account_active.get(key, 0) for key in self._accounts}
        self._smooth_current = {key: self._smooth_current.get(key, 0.0) for key in self._accounts}

    def active_snapshot(self) -> tuple[int, dict[str, int], int]:
        """返回当前全局并发、账号并发和等待队列计数。"""
        return self._global_active, dict(self._account_active), len(self._waiters)

    async def acquire(
        self,
        *,
        bound_account_key: str | None = None,
        excluded_account_keys: set[str] | None = None,
    ) -> AccountLease:
        """按 FIFO 等待并原子获取全局与账号槽位。"""
        token = object()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.queue_timeout_seconds
        async with self._condition:
            self._waiters.append(token)
            try:
                while True:
                    if self._waiters[0] is token:
                        account_key = self._choose_available(bound_account_key, excluded_account_keys or set())
                        if account_key is not None:
                            self._waiters.popleft()
                            self._global_active += 1
                            self._account_active[account_key] = self._account_active.get(account_key, 0) + 1
                            self._condition.notify_all()
                            return AccountLease(self, account_key)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise QueueWaitTimeout("OAuth account concurrency queue timed out")
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise QueueWaitTimeout("OAuth account concurrency queue timed out") from error
            finally:
                if token in self._waiters:
                    self._waiters.remove(token)
                    self._condition.notify_all()

    def _choose_available(self, bound_account_key: str | None, excluded_account_keys: set[str]) -> str | None:
        """在当前槽位状态下选择绑定账号或普通加权账号。"""
        if self.global_max is not None and self._global_active >= self.global_max:
            return None
        if bound_account_key is not None:
            account = self._accounts.get(bound_account_key)
            if account is None or not account.enabled or account.status != "available":
                raise BoundAccountUnavailable(
                    "previous_response_id 所属 OAuth 账户已停用，请不带 previous_response_id 发起新会话"
                )
            return bound_account_key if self._has_account_slot(account) else None
        candidates = [
            item
            for item in self._accounts.values()
            if item.key not in excluded_account_keys
            and item.enabled
            and item.status == "available"
            and self._has_account_slot(item)
        ]
        if not candidates:
            return None
        weighted = [(item, account_dispatch_weight(item, now=self.clock())) for item in candidates]
        total = sum(weight for _, weight in weighted)
        for item, weight in weighted:
            self._smooth_current[item.key] = self._smooth_current.get(item.key, 0.0) + weight
        selected = max(weighted, key=lambda pair: (self._smooth_current[pair[0].key], pair[0].key))[0]
        self._smooth_current[selected.key] -= total
        return selected.key

    def _has_account_slot(self, account: AccountCandidate) -> bool:
        """判断账号是否仍有独立并发槽位。"""
        return account.max_concurrency is None or self._account_active.get(account.key, 0) < account.max_concurrency

    async def _release(self, account_key: str) -> None:
        """释放一次全局和账号并发占用，并唤醒队首。"""
        async with self._condition:
            self._global_active = max(0, self._global_active - 1)
            self._account_active[account_key] = max(0, self._account_active.get(account_key, 0) - 1)
            self._condition.notify_all()
