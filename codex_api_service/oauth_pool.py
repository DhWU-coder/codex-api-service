"""多 OAuth 账号客户端池与统一请求执行入口。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import OAuthLoginRequired, credentials_expired_or_near
from .codex_client import CodexClient, CodexHTTPStatusError, _codex_client_version
from .codex_usage import CodexUsageAuthError, CodexUsageSession, fetch_codex_usage_snapshot
from .config import AppConfig
from .oauth_accounts import OAuthAccountRecord, OAuthAccountStore
from .oauth_credentials import AccountPoolAuth
from .oauth_login import OAuthLoginManager
from .oauth_scheduler import AccountCandidate, BoundAccountUnavailable, NoAvailableOAuthAccounts, OAuthScheduler
from .oauth_sync import OAuthSyncService
from .response_bindings import ResponseBindingStore

ACCOUNT_REFRESH_TIMEOUT_SECONDS = 30.0
SESSION_CLEANUP_TIMEOUT_SECONDS = 10.0
SYNC_TIMEOUT_SECONDS = 5.0
BACKGROUND_POLL_SECONDS = 5.0
logger = logging.getLogger(__name__)


def _integer(value: Any) -> int:
    """把额度窗口时长安全转换为整数。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _positive_number(value: Any) -> bool:
    """判断额度窗口是否包含可用于调度的正时长。"""
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


@dataclass
class AccountRuntime:
    """保存单个账号客户端与可公开的运行状态。"""

    record: OAuthAccountRecord
    client: CodexClient
    usage: dict[str, Any] | None = None
    status: str = "available"
    last_error: str | None = None
    next_refresh_at: float = 0.0
    last_successful_refresh_at: float = 0.0
    # 指纹仅用于检测凭据变更，不包含明文令牌，也不出现在管理接口或日志中。
    credentials_fingerprint: str | None = field(default=None, repr=False)
    auth_generation: int = 0
    reset_usage_session: bool = False


class OAuthAccountPool:
    """按额度权重选择隔离的 OAuth 客户端执行请求。"""

    def __init__(self, *, config: AppConfig) -> None:
        root = config.auth.account_store_path or config.project_root / ".codex-oauth"
        import_path = config.auth.import_auth_path or Path.home() / ".codex" / "auth.json"
        self.config = config
        self.store = OAuthAccountStore(root)
        self.sync = OAuthSyncService(store=self.store, import_path=import_path)
        self.login = OAuthLoginManager(store=self.store)
        self.scheduler = OAuthScheduler(
            global_max=config.concurrency.global_max,
            queue_timeout_seconds=config.concurrency.queue_timeout_seconds,
        )
        self.bindings = ResponseBindingStore(root / "state.sqlite3")
        self.runtimes: dict[str, AccountRuntime] = {}
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._background_task: asyncio.Task[None] | None = None
        self._usage_sessions: dict[str, CodexUsageSession] = {}
        # 同账号自动与手动刷新共享任务，避免在会话锁前堆积无限等待者。
        self._refresh_tasks: dict[str, asyncio.Task[dict[str, Any] | None]] = {}
        # ContextVar 让同一账户池处理并发请求时，各请求读取到自己的账户信息。
        self._request_account: ContextVar[dict[str, str] | None] = ContextVar(
            f"oauth_request_account_{id(self)}",
            default=None,
        )

    async def ensure_initialized(self) -> None:
        """首次请求时同步当前登录并构建账号客户端。"""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.sync.sync_once(force=True)
            self._rebuild_runtimes()
            self._initialized = True
            self._background_task = asyncio.create_task(self._background_loop())
            for key in self.runtimes:
                self._start_account_refresh(key)

    async def close(self) -> None:
        """停止后台任务、额度会话和本地状态库。"""
        if self._background_task is not None:
            self._background_task.cancel()
            await asyncio.gather(self._background_task, return_exceptions=True)
        tasks = list(self._refresh_tasks.values())
        self._refresh_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.login.close()
        sessions = list(self._usage_sessions.values())
        self._usage_sessions.clear()
        if sessions:
            await asyncio.gather(*(self._close_usage_session(session) for session in sessions), return_exceptions=True)
        self.bindings.close()

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """为非流式请求选择账号并聚合响应。"""
        self._request_account.set(None)
        await self.ensure_initialized()
        self._rebuild_runtimes()
        bound = self._bound_account(payload)
        upstream_payload = self._upstream_payload(payload)
        attempted: set[str] = set()
        last_error: Exception | None = None
        while bound is not None or bool(self._available_keys() - attempted):
            lease = await self.scheduler.acquire(
                bound_account_key=bound,
                excluded_account_keys=attempted,
            )
            attempted.add(lease.account_key)
            runtime = self.runtimes[lease.account_key]
            self._set_request_account(runtime.record)
            try:
                response = await self._create_with_authorizations(runtime, upstream_payload)
                self._remember_response(response, runtime.record.key)
                return response
            except OAuthLoginRequired as error:
                last_error = error
                if bound is not None:
                    raise self._bound_auth_error() from error
            except CodexHTTPStatusError as error:
                last_error = error
                if error.status_code not in {401, 403, 429}:
                    raise
                await self._handle_switchable_error(runtime, error)
                if bound is not None:
                    if error.status_code in {401, 403}:
                        raise self._bound_auth_error() from error
                    raise
            finally:
                await lease.release()
        if isinstance(last_error, CodexHTTPStatusError) and last_error.status_code == 429:
            raise last_error
        raise NoAvailableOAuthAccounts("没有可用的 OAuth 账号，请检查账号登录状态、启用状态和额度") from last_error

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """为流式请求选择账号，并在流结束时释放并发槽。"""
        self._request_account.set(None)
        await self.ensure_initialized()
        self._rebuild_runtimes()
        bound = self._bound_account(payload)
        upstream_payload = self._upstream_payload(payload)
        attempted: set[str] = set()
        last_error: Exception | None = None
        while bound is not None or bool(self._available_keys() - attempted):
            lease = await self.scheduler.acquire(bound_account_key=bound, excluded_account_keys=attempted)
            attempted.add(lease.account_key)
            runtime = self.runtimes[lease.account_key]
            self._set_request_account(runtime.record)
            emitted = False
            try:
                async for event in self._stream_with_authorizations(runtime, upstream_payload):
                    emitted = True
                    response = event.get("response")
                    if event.get("type") == "response.completed" and isinstance(response, dict):
                        self._remember_response(response, runtime.record.key)
                    yield event
                return
            except OAuthLoginRequired as error:
                last_error = error
                if bound is not None:
                    raise self._bound_auth_error() from error
                if emitted:
                    raise
            except CodexHTTPStatusError as error:
                last_error = error
                if error.status_code not in {401, 403, 429}:
                    raise
                await self._handle_switchable_error(runtime, error)
                if bound is not None and error.status_code in {401, 403}:
                    raise self._bound_auth_error() from error
                if emitted or bound is not None:
                    raise
            finally:
                await lease.release()
        if isinstance(last_error, CodexHTTPStatusError) and last_error.status_code == 429:
            raise last_error
        raise NoAvailableOAuthAccounts("没有可用的 OAuth 账号，请检查账号登录状态、启用状态和额度") from last_error

    async def _create_with_authorizations(self, runtime: AccountRuntime, payload: dict[str, Any]) -> dict[str, Any]:
        """同账号内先回退授权，保持绑定链、调度槽和请求元数据不变。"""
        attempted: set[str | None] = set()
        for _attempt in range(4):
            fingerprint = runtime.credentials_fingerprint
            attempted.add(fingerprint)
            try:
                return await runtime.client.create_response(payload)
            except (OAuthLoginRequired, CodexHTTPStatusError) as error:
                if not self._is_auth_failure(error):
                    raise
                ready = self._reject_authorization(runtime, error, fingerprint)
                if not ready or runtime.credentials_fingerprint in attempted:
                    raise
        raise OAuthLoginRequired("OAuth authorization changed too frequently")

    async def _stream_with_authorizations(self, runtime: AccountRuntime, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """仅在尚未输出任何事件时回退，已输出的流不能重放。"""
        attempted: set[str | None] = set()
        for _attempt in range(4):
            fingerprint = runtime.credentials_fingerprint
            attempted.add(fingerprint)
            emitted = False
            try:
                async for event in runtime.client.stream_response(payload):
                    emitted = True
                    yield event
                return
            except (OAuthLoginRequired, CodexHTTPStatusError) as error:
                if not self._is_auth_failure(error):
                    raise
                ready = self._reject_authorization(runtime, error, fingerprint)
                if emitted or not ready or runtime.credentials_fingerprint in attempted:
                    raise
        raise OAuthLoginRequired("OAuth authorization changed too frequently")

    @staticmethod
    def _is_auth_failure(error: Exception) -> bool:
        """额度、服务过载和网络异常均不能消耗备用授权。"""
        return isinstance(error, OAuthLoginRequired) or isinstance(error, CodexHTTPStatusError) and error.status_code in {401, 403}

    def _reject_authorization(self, runtime: AccountRuntime, error: Exception, fingerprint: str | None) -> bool:
        """拒绝实际失败来源，迟到的旧错误不能隔离当前新授权。"""
        rejected = getattr(error, "credentials_fingerprint", None) or fingerprint
        try:
            if rejected:
                self.store.authorizations(runtime.record.key).reject(rejected)
        except OSError:
            self._mark_login_required(runtime)
            runtime.last_error += "；认证失败状态保存失败"
            return False
        ready = self._check_account_credentials(runtime)
        self._update_scheduler()
        return ready

    def request_account_metadata(self) -> dict[str, str] | None:
        """返回当前请求最后一次实际选中账户的安全元数据。"""
        metadata = self._request_account.get()
        return dict(metadata) if metadata is not None else None

    async def sync_global(self) -> OAuthAccountRecord | None:
        """强制同步全局 Codex 登录并刷新运行账号集合。"""
        record = await self.sync.sync_once(force=True)
        self._rebuild_runtimes()
        if record is not None:
            await self.refresh_account(record.key)
        return record

    async def refresh_account(self, key: str) -> dict[str, Any] | None:
        """等待该账号的共享刷新，调用方断开不能取消其他调用方等待的任务。"""
        return await asyncio.shield(self._start_account_refresh(key))

    def _start_account_refresh(self, key: str) -> asyncio.Task[dict[str, Any] | None]:
        """启动单账号任务；后台调度只派发，不等待整个批次完成。"""
        task = self._refresh_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_account_bounded(key))
            self._refresh_tasks[key] = task
        return task

    async def _refresh_account_bounded(self, key: str) -> dict[str, Any] | None:
        """给完整刷新设置时限，失败后淘汰会话并保留最后成功数据。"""
        runtime = self.runtimes.get(key)
        if runtime is None:
            return None
        generation = runtime.auth_generation
        try:
            self._check_account_credentials(runtime)
            generation = runtime.auth_generation
            async with asyncio.timeout(ACCOUNT_REFRESH_TIMEOUT_SECONDS):
                for _attempt in range(3):
                    self._check_account_credentials(runtime)
                    fingerprint = runtime.credentials_fingerprint
                    attempt_generation = runtime.auth_generation
                    try:
                        usage = await self._refresh_account_once(key)
                        if usage is None and runtime.status == "available" and runtime.auth_generation != attempt_generation:
                            # 原生会话刷新了凭据或期间发生新登录，立即用新代次重读一次。
                            generation = runtime.auth_generation
                            continue
                        return usage
                    except CodexUsageAuthError as error:
                        # RPC 同样绑定实际开始读取的授权，旧会话错误不能误伤新登录。
                        if not self._reject_authorization(runtime, error, fingerprint):
                            session = self._usage_sessions.pop(key, None)
                            if session is not None:
                                await self._close_usage_session(session)
                            return None
                        generation = runtime.auth_generation
                return None
        except Exception as error:
            if self._refresh_is_current(runtime, generation):
                runtime.last_error = "额度读取超时，稍后自动重试" if isinstance(error, TimeoutError) else "额度读取失败"
                runtime.next_refresh_at = time.time() + 30
            # 不透传原始异常，避免把上游凭据或响应内容写进管理接口。
            session = self._usage_sessions.pop(key, None)
            if session is not None:
                await self._close_usage_session(session)
            return None

    @staticmethod
    async def _close_usage_session(session: CodexUsageSession) -> None:
        """为会话清理设置独立上限，坏会话不能阻塞后续刷新或停服。"""
        try:
            async with asyncio.timeout(SESSION_CLEANUP_TIMEOUT_SECONDS):
                await session.close()
        except Exception:
            logger.warning("额度读取会话清理失败或超时，已停止复用")

    async def _refresh_account_once(self, key: str) -> dict[str, Any] | None:
        """读取指定账号额度并更新调度权重。"""
        runtime = self.runtimes.get(key)
        if runtime is None:
            return None
        credentials_ready = self._check_account_credentials(runtime)
        self._update_scheduler()
        generation = runtime.auth_generation
        if runtime.reset_usage_session:
            session = self._usage_sessions.pop(key, None)
            runtime.reset_usage_session = False
            if session is not None:
                await self._close_usage_session(session)
        if not credentials_ready:
            runtime.next_refresh_at = time.time() + 30
            return None
        if not self._refresh_is_current(runtime, generation):
            return None
        session = self._usage_sessions.get(key)
        if session is None:
            selected = self.store.authorizations(key).select()
            session = CodexUsageSession(account_home=selected.auth_path.parent)
            self._usage_sessions[key] = session
        usage = await fetch_codex_usage_snapshot(
            client_version=_codex_client_version(),
            app_server_rpc=session.read_rate_limits,
        )
        # 调用失败或重新登录可能发生在额度请求期间，旧结果不能覆盖新的认证状态。
        if not self._refresh_is_current(runtime, generation):
            return None
        runtime.usage = usage
        runtime.last_successful_refresh_at = time.time()
        runtime.status = "available" if usage.get("rateLimit", {}).get("allowed", True) else "rate_limited"
        runtime.last_error = None
        primary = self._dispatch_window(usage)
        remaining = float(primary.get("remainingPercent", 100)) if primary else 100.0
        interval = 30 if remaining < 5 else 60 if remaining < 10 else 300
        runtime.next_refresh_at = time.time() + interval
        self._update_scheduler()
        return usage

    async def refresh_enabled_accounts(self) -> dict[str, Any]:
        """并发刷新全部启用账号，并返回最新脱敏快照。"""
        enabled_keys = [key for key, runtime in self.runtimes.items() if runtime.record.enabled]
        await asyncio.gather(*(self.refresh_account(key) for key in enabled_keys), return_exceptions=True)
        return self.snapshot()

    async def delete_account(self, key: str) -> None:
        """删除账号，并关闭该账号持有的额度会话。"""
        if self.store.get(key) is None:
            raise KeyError(key)
        if self.store.dispatch_mode == "single" and key == self.store.single_account_key:
            raise ValueError("select another single account before deleting this account")
        # app-server 仍以账号目录作为 CODEX_HOME，必须先停进程再删除凭据目录。
        session = self._usage_sessions.pop(key, None)
        if session is not None:
            await self._close_usage_session(session)
        await self.store.delete(key)
        self._rebuild_runtimes()

    def snapshot(self) -> dict[str, Any]:
        """返回不包含 token 的多账号管理快照。"""
        self._rebuild_runtimes()
        global_active, account_active, waiting_count = self.scheduler.active_snapshot()
        items = []
        weights: dict[str, float] = {}
        candidates = self._candidates()
        from .oauth_scheduler import account_dispatch_weight

        for candidate in candidates:
            # 预计占比只统计实际会参与调度的账号，避免停用或限流账号稀释分母。
            weights[candidate.key] = (
                account_dispatch_weight(candidate)
                if candidate.enabled and candidate.status == "available"
                else 0.0
            )
        weight_total = sum(weights.values()) or 1.0
        for record in self.store.list():
            runtime = self.runtimes.get(record.key)
            items.append(
                {
                    "key": record.key,
                    "alias": record.alias,
                    "enabled": record.enabled,
                    "source": self.store.authorizations(record.key).active_source or record.source,
                    "authorizations": self.store.authorizations(record.key).snapshot(),
                    "maxConcurrency": record.max_concurrency,
                    "currentConcurrency": account_active.get(record.key, 0),
                    "status": runtime.status if runtime else "login_required",
                    "lastError": runtime.last_error if runtime else None,
                    "usage": runtime.usage if runtime else None,
                    "weight": weights.get(record.key, 0.0),
                    "estimatedShare": weights.get(record.key, 0.0) / weight_total,
                    "nextRefreshAt": runtime.next_refresh_at if runtime else 0,
                    "lastSuccessfulRefreshAt": runtime.last_successful_refresh_at if runtime else 0,
                    "usageStale": (
                        runtime is None
                        or not runtime.last_successful_refresh_at
                        or runtime.last_error is not None
                        or time.time() > runtime.next_refresh_at + 30
                    ),
                    "usageRefreshing": record.key in self._refresh_tasks and not self._refresh_tasks[record.key].done(),
                }
            )
        return {
            "accounts": items,
            "dispatchMode": self.store.dispatch_mode,
            "singleAccountKey": self.store.single_account_key,
            "globalCurrentConcurrency": global_active,
            "globalMaxConcurrency": self.scheduler.global_max,
            "waitingQueueSize": waiting_count,
        }

    async def set_dispatch(
        self,
        *,
        mode: str,
        single_account_key: str | None = None,
        enabled_account_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """原子更新调度策略，并立即刷新运行时和调度候选。"""
        await self.store.set_dispatch(
            mode=mode,
            single_account_key=single_account_key,
            enabled_account_keys=enabled_account_keys,
        )
        self._rebuild_runtimes()
        return self.snapshot()

    def _rebuild_runtimes(self) -> None:
        """根据注册表增量重建账号客户端。"""
        records = {item.key: item for item in self.store.list()}
        for key in list(self.runtimes):
            if key not in records:
                self.runtimes.pop(key)
        for key, record in records.items():
            existing = self.runtimes.get(key)
            if existing is not None:
                existing.record = record
                # 同账号重新登录可能不改变注册信息，仍必须重新核对凭据。
                self._check_account_credentials(existing)
                continue
            auth = AccountPoolAuth(self.store.authorizations(key))
            self.runtimes[key] = AccountRuntime(
                record=record,
                client=CodexClient(auth=auth, config=self.config.codex),
            )
            self._check_account_credentials(self.runtimes[key])
        self._update_scheduler()

    def _check_account_credentials(self, runtime: AccountRuntime) -> bool:
        """检查隔离凭据，失效账号只有换成新的未过期凭据才允许重新入池。"""
        selected = self.store.authorizations(runtime.record.key).select()
        if selected is None:
            if runtime.status != "login_required" or runtime.credentials_fingerprint is not None:
                self._mark_login_required(runtime)
            return False
        credentials = selected.credentials
        fingerprint = selected.fingerprint
        blocked = runtime.status in {"login_required", "auth_error"}
        if blocked and credentials_expired_or_near(credentials):
            return False
        if fingerprint != runtime.credentials_fingerprint:
            runtime.reset_usage_session = runtime.reset_usage_session or blocked or runtime.credentials_fingerprint is not None
            runtime.credentials_fingerprint = fingerprint
            runtime.auth_generation += 1
            runtime.next_refresh_at = 0
            if blocked:
                runtime.status = "available"
                runtime.last_error = None
        return True

    def _mark_login_required(self, runtime: AccountRuntime) -> None:
        """立即隔离认证失败账号，保留启用设置供重新登录后恢复。"""
        runtime.credentials_fingerprint = None
        runtime.status = "login_required"
        runtime.last_error = "OAuth 凭据缺失或失效，请重新登录"
        runtime.auth_generation += 1
        runtime.reset_usage_session = True
        runtime.next_refresh_at = time.time() + 30
        self._update_scheduler()

    def _refresh_is_current(self, runtime: AccountRuntime, generation: int) -> bool:
        """防止旧额度读取结果复活已失效、已删除或刚更换凭据的账号。"""
        if self.runtimes.get(runtime.record.key) is not runtime:
            return False
        ready = self._check_account_credentials(runtime)
        self._update_scheduler()
        return ready and runtime.auth_generation == generation

    @staticmethod
    def _bound_auth_error() -> BoundAccountUnavailable:
        """绑定会话认证失败时明确报错，禁止切换到其他账号继续上下文。"""
        return BoundAccountUnavailable("previous_response_id 所属 OAuth 账户需要重新登录，请重新登录该账号或不带 previous_response_id 发起新会话")

    async def _handle_switchable_error(self, runtime: AccountRuntime, error: CodexHTTPStatusError) -> None:
        """处理认证或额度错误，并让普通请求切换其他账号。"""
        if error.status_code in {401, 403}:
            # 授权回退层已经按实际指纹处理，不能再次拒绝刚切换的备用凭据。
            self._check_account_credentials(runtime)
        else:
            runtime.status = "rate_limited"
            runtime.last_error = "5 小时额度或请求频率受限"
            await self.refresh_account(runtime.record.key)
        self._update_scheduler()

    async def _background_loop(self) -> None:
        """后台同步全局登录，并按每账号自适应时间刷新额度。"""
        last_forced_sync = time.monotonic()
        try:
            while True:
                await asyncio.sleep(BACKGROUND_POLL_SECONDS)
                try:
                    force_sync = time.monotonic() - last_forced_sync >= 300
                    async with asyncio.timeout(SYNC_TIMEOUT_SECONDS):
                        record = await self.sync.sync_once(force=force_sync)
                    if force_sync:
                        last_forced_sync = time.monotonic()
                    if record is not None:
                        self._rebuild_runtimes()
                        self._start_account_refresh(record.key)
                except Exception:
                    # 单次同步失败不能让刷新循环永久退出，也不能暴露原始认证错误。
                    logger.warning("OAuth 登录同步失败，后台额度刷新将继续重试")
                now = time.time()
                due = [key for key, runtime in self.runtimes.items() if runtime.next_refresh_at <= now]
                for key in due:
                    self._start_account_refresh(key)
        except asyncio.CancelledError:
            return

    def _available_keys(self) -> set[str]:
        """返回当前启用且未被暂停的账号键。"""
        return {
            key
            for key, runtime in self.runtimes.items()
            if runtime.record.enabled and runtime.status == "available"
        }

    def _update_scheduler(self) -> None:
        self.scheduler.update_accounts(self._candidates())

    def _candidates(self) -> list[AccountCandidate]:
        """把账号运行状态转换为调度快照。"""
        candidates = []
        for runtime in self.runtimes.values():
            primary = self._dispatch_window(runtime.usage)
            candidates.append(
                AccountCandidate(
                    key=runtime.record.key,
                    plan_type=str((runtime.usage or {}).get("planType") or "unknown"),
                    remaining_percent=float(primary.get("remainingPercent", 100)) if primary else 100,
                    resets_at=float(primary.get("resetAt", time.time() + 300 * 60)) if primary else time.time() + 300 * 60,
                    enabled=runtime.record.enabled,
                    status=runtime.status,
                    max_concurrency=runtime.record.max_concurrency,
                )
            )
        return candidates

    def _bound_account(self, payload: dict[str, Any]) -> str | None:
        previous = payload.get("previous_response_id")
        if not isinstance(previous, str) or not previous:
            return None
        bound = self.bindings.lookup(previous)
        if bound is None:
            raise BoundAccountUnavailable(
                "previous_response_id 所属 OAuth 账户已停用，请不带 previous_response_id 发起新会话"
            )
        return bound

    @staticmethod
    def _upstream_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """移除仅供本地账号粘性使用、Codex backend 不支持的字段。"""
        upstream = dict(payload)
        upstream.pop("previous_response_id", None)
        return upstream

    def _remember_response(self, response: dict[str, Any], account_key: str) -> None:
        response_id = response.get("id")
        if isinstance(response_id, str) and response_id:
            self.bindings.bind(response_id, account_key)

    def _set_request_account(self, record: OAuthAccountRecord) -> None:
        """把已选账户写入当前异步请求上下文。"""
        self._request_account.set(
            {
                "account_key": record.key,
                "account_alias": record.alias,
            }
        )

    @staticmethod
    def _dispatch_window(usage: dict[str, Any] | None) -> dict[str, Any]:
        """优先选择真实 5 小时窗口，没有时回退到最短有效窗口。"""
        if not isinstance(usage, dict):
            return {}
        rate_limit = usage.get("rateLimit")
        windows = rate_limit.get("windows", []) if isinstance(rate_limit, dict) else []
        valid_windows = [
            item
            for item in windows
            if isinstance(item, dict) and _positive_number(item.get("limitWindowSeconds"))
        ]
        five_hour = next(
            (item for item in valid_windows if _integer(item.get("limitWindowSeconds")) == 5 * 60 * 60),
            None,
        )
        if five_hour is not None:
            return five_hour
        return min(valid_windows, key=lambda item: _integer(item.get("limitWindowSeconds")), default={})

    @staticmethod
    def _primary_window(usage: dict[str, Any] | None) -> dict[str, Any]:
        """兼容旧调用方，返回当前用于调度的额度窗口。"""
        return OAuthAccountPool._dispatch_window(usage)
