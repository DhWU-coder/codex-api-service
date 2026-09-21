"""多 OAuth 账号客户端池与统一请求执行入口。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import CodexAuth, OAuthLoginRequired, _parse_auth_file, credentials_expired_or_near
from .codex_client import CodexClient, CodexHTTPStatusError, _codex_client_version
from .codex_usage import CodexUsageSession, fetch_codex_usage_snapshot
from .config import AppConfig
from .oauth_accounts import OAuthAccountRecord, OAuthAccountStore
from .oauth_login import OAuthLoginManager
from .oauth_scheduler import AccountCandidate, BoundAccountUnavailable, NoAvailableOAuthAccounts, OAuthScheduler
from .oauth_sync import OAuthSyncService
from .response_bindings import ResponseBindingStore


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
    # 指纹仅用于检测凭据变更，不包含明文令牌，也不出现在管理接口或日志中。
    credentials_fingerprint: str | None = field(default=None, repr=False)
    rejected_credentials_fingerprint: str | None = field(default=None, repr=False)
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
                asyncio.create_task(self.refresh_account(key))

    async def close(self) -> None:
        """停止后台任务、额度会话和本地状态库。"""
        if self._background_task is not None:
            self._background_task.cancel()
            await asyncio.gather(self._background_task, return_exceptions=True)
        await self.login.close()
        sessions = list(self._usage_sessions.values())
        self._usage_sessions.clear()
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
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
                response = await runtime.client.create_response(upstream_payload)
                self._remember_response(response, runtime.record.key)
                return response
            except OAuthLoginRequired as error:
                last_error = error
                self._mark_login_required(runtime)
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
                async for event in runtime.client.stream_response(upstream_payload):
                    emitted = True
                    response = event.get("response")
                    if event.get("type") == "response.completed" and isinstance(response, dict):
                        self._remember_response(response, runtime.record.key)
                    yield event
                return
            except OAuthLoginRequired as error:
                last_error = error
                self._mark_login_required(runtime)
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
                await session.close()
        if not credentials_ready:
            runtime.next_refresh_at = time.time() + 30
            return None
        if not self._refresh_is_current(runtime, generation):
            return None
        session = self._usage_sessions.get(key)
        if session is None:
            session = CodexUsageSession(account_home=self.store.auth_path(key).parent)
            self._usage_sessions[key] = session
        try:
            usage = await fetch_codex_usage_snapshot(
                client_version=_codex_client_version(),
                app_server_rpc=session.read_rate_limits,
            )
        except Exception:
            if not self._refresh_is_current(runtime, generation):
                return None
            runtime.last_error = "额度读取失败"
            # 保留最后一次成功快照，同时安排退避重试，避免失败时每五秒重启 app-server。
            runtime.next_refresh_at = time.time() + 30
            return None
        # 调用失败或重新登录可能发生在额度请求期间，旧结果不能覆盖新的认证状态。
        if not self._refresh_is_current(runtime, generation):
            return None
        runtime.usage = usage
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
            await session.close()
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
                    "source": record.source,
                    "maxConcurrency": record.max_concurrency,
                    "currentConcurrency": account_active.get(record.key, 0),
                    "status": runtime.status if runtime else "login_required",
                    "lastError": runtime.last_error if runtime else None,
                    "usage": runtime.usage if runtime else None,
                    "weight": weights.get(record.key, 0.0),
                    "estimatedShare": weights.get(record.key, 0.0) / weight_total,
                    "nextRefreshAt": runtime.next_refresh_at if runtime else 0,
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
            auth = CodexAuth(
                auth_path=self.store.auth_path(key),
                import_auth_path=self.store.auth_path(key),
                allow_import=False,
                allow_interactive_login=False,
            )
            self.runtimes[key] = AccountRuntime(
                record=record,
                client=CodexClient(auth=auth, config=self.config.codex),
                rejected_credentials_fingerprint=self.store.rejected_credentials_fingerprint(key),
            )
            self._check_account_credentials(self.runtimes[key])
        self._update_scheduler()

    def _check_account_credentials(self, runtime: AccountRuntime) -> bool:
        """检查隔离凭据，失效账号只有换成新的未过期凭据才允许重新入池。"""
        credentials = _parse_auth_file(self.store.auth_path(runtime.record.key))
        if credentials is None:
            if runtime.status != "login_required" or runtime.credentials_fingerprint is not None:
                self._mark_login_required(runtime)
            return False
        fingerprint = self._credentials_fingerprint(credentials.access, credentials.refresh)
        blocked = runtime.status in {"login_required", "auth_error"}
        if fingerprint == runtime.rejected_credentials_fingerprint:
            if not blocked:
                self._mark_login_required(runtime)
            return False
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

    @staticmethod
    def _credentials_fingerprint(access: str, refresh: str) -> str:
        """比较令牌内容而非文件时间，避免同一失效凭据被周期同步后误恢复。"""
        return hashlib.sha256(f"{access}\0{refresh}".encode()).hexdigest()

    def _mark_login_required(self, runtime: AccountRuntime) -> None:
        """立即隔离认证失败账号，保留启用设置供重新登录后恢复。"""
        credentials = _parse_auth_file(self.store.auth_path(runtime.record.key))
        fingerprint = (
            self._credentials_fingerprint(credentials.access, credentials.refresh) if credentials else None
        )
        # 自动刷新失败可能已删除认证文件，仍记住最后一次被拒绝的凭据，避免旧文件同步后复活。
        runtime.rejected_credentials_fingerprint = (
            fingerprint or runtime.credentials_fingerprint or runtime.rejected_credentials_fingerprint
        )
        runtime.credentials_fingerprint = fingerprint
        runtime.status = "login_required"
        runtime.last_error = "OAuth 凭据缺失或失效，请重新登录"
        runtime.auth_generation += 1
        runtime.reset_usage_session = True
        runtime.next_refresh_at = time.time() + 30
        self._update_scheduler()
        if runtime.rejected_credentials_fingerprint:
            try:
                self.store.reject_credentials(runtime.record.key, runtime.rejected_credentials_fingerprint)
            except OSError:
                # 状态持久化失败不能中断健康账号回退；内存隔离仍然生效并在界面明确提示。
                runtime.last_error += "；认证失败状态保存失败"

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
            self._mark_login_required(runtime)
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
                await asyncio.sleep(5)
                force_sync = time.monotonic() - last_forced_sync >= 300
                record = await self.sync.sync_once(force=force_sync)
                if force_sync:
                    last_forced_sync = time.monotonic()
                if record is not None:
                    self._rebuild_runtimes()
                    await self.refresh_account(record.key)
                now = time.time()
                due = [key for key, runtime in self.runtimes.items() if runtime.next_refresh_at <= now]
                if due:
                    await asyncio.gather(*(self.refresh_account(key) for key in due), return_exceptions=True)
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
