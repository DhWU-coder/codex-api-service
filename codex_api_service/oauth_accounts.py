"""项目内多 OAuth 账号的安全存储与管理。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .auth import CodexCredentials, _parse_auth_file


def _identity_alias(id_token: str | None) -> str | None:
    """从 ID Token 的公开身份声明提取展示名，不记录或验证敏感凭据。"""
    if not id_token:
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    for field_name in ("email", "name"):
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class OAuthAccountRecord:
    """描述一个不包含敏感凭据的 OAuth 账号。"""

    key: str
    alias: str
    enabled: bool = True
    source: str = "unknown"
    max_concurrency: int | None = None


class OAuthAccountStore:
    """把多个 OAuth 凭据按真实账号身份隔离保存。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.accounts_dir = self.root / "accounts"
        self.registry_path = self.root / "registry.json"
        self._registry_lock = asyncio.Lock()
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._records: dict[str, OAuthAccountRecord] = {}
        self.dispatch_mode = "multi"
        self.single_account_key: str | None = None
        self._prepare_directories()
        self._load_registry()

    def list(self) -> list[OAuthAccountRecord]:
        """返回按别名排序的账号注册快照。"""
        return sorted(self._records.values(), key=lambda item: (item.alias.lower(), item.key))

    def get(self, key: str) -> OAuthAccountRecord | None:
        """读取单个账号注册信息。"""
        return self._records.get(key)

    def auth_path(self, key: str) -> Path:
        """返回指定账号的认证文件路径。"""
        return self.accounts_dir / key / "auth.json"

    async def import_credentials(self, credentials: CodexCredentials, *, source: str) -> OAuthAccountRecord:
        """根据凭据中的真实账号标识更新或新增账号。"""
        payload = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {
                **({"id_token": credentials.id_token} if credentials.id_token else {}),
                "access_token": credentials.access,
                "refresh_token": credentials.refresh,
                "account_id": credentials.account_id,
            },
            "expires": credentials.expires,
        }
        return await self._import(credentials, payload=payload, source=source)

    async def import_auth_file(self, path: Path | str, *, source: str) -> OAuthAccountRecord:
        """按真实账号归档 Codex 原生 auth.json，并保留顶层元数据。"""
        source_path = Path(path)
        credentials = _parse_auth_file(source_path)
        if credentials is None:
            raise ValueError("OAuth auth file is invalid")
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("OAuth auth file is invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("OAuth auth file is invalid")
        return await self._import(credentials, payload=payload, source=source)

    async def _import(
        self,
        credentials: CodexCredentials,
        *,
        payload: dict[str, Any],
        source: str,
    ) -> OAuthAccountRecord:
        """在识别账号后保存指定认证内容和注册信息。"""
        account_id = (credentials.account_id or "").strip()
        if not account_id:
            raise ValueError("OAuth credentials missing account id")
        key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
        lock = self._account_locks.setdefault(key, asyncio.Lock())
        async with lock:
            account_dir = self.accounts_dir / key
            account_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(account_dir, 0o700)
            self._atomic_json_write(self.auth_path(key), payload, mode=0o600)
            async with self._registry_lock:
                existing = self._records.get(key)
                generated_alias = f"账号 {key[-6:]}"
                identity_alias = _identity_alias(credentials.id_token)
                # 只补全系统生成的旧别名，用户手动名称永远优先保留。
                if existing and existing.alias != generated_alias:
                    alias = existing.alias
                else:
                    alias = identity_alias or generated_alias
                record = OAuthAccountRecord(
                    key=key,
                    alias=alias,
                    enabled=existing.enabled if existing else self.dispatch_mode == "multi",
                    source=source,
                    max_concurrency=existing.max_concurrency if existing else None,
                )
                self._records[key] = record
                self._save_registry()
            return record

    async def update(
        self,
        key: str,
        *,
        alias: str | None = None,
        enabled: bool | None = None,
        max_concurrency: int | None | object = ...,
    ) -> OAuthAccountRecord:
        """更新账号的非敏感管理字段。"""
        async with self._registry_lock:
            current = self._records.get(key)
            if current is None:
                raise KeyError(key)
            next_limit = current.max_concurrency if max_concurrency is ... else max_concurrency
            if next_limit is not None and (not isinstance(next_limit, int) or isinstance(next_limit, bool) or next_limit <= 0):
                raise ValueError("max_concurrency must be empty or a positive integer")
            if enabled is not None:
                if self.dispatch_mode == "single" and enabled:
                    # 单账户模式启用另一账号等同于切换唯一调度账号。
                    self.single_account_key = key
                    self._records = {
                        record_key: OAuthAccountRecord(
                            key=record.key,
                            alias=record.alias,
                            enabled=record_key == key,
                            source=record.source,
                            max_concurrency=record.max_concurrency,
                        )
                        for record_key, record in self._records.items()
                    }
                    current = self._records[key]
                elif self.dispatch_mode == "single" and key == self.single_account_key and not enabled:
                    raise ValueError("single account mode must keep the selected account enabled")
                elif self.dispatch_mode == "multi" and not enabled:
                    enabled_count = sum(1 for item in self._records.values() if item.enabled)
                    if current.enabled and enabled_count <= 1:
                        raise ValueError("multi account mode requires at least one enabled account")
            record = OAuthAccountRecord(
                key=current.key,
                alias=(alias.strip() if alias is not None else current.alias) or current.alias,
                enabled=current.enabled if enabled is None else enabled,
                source=current.source,
                max_concurrency=next_limit,
            )
            self._records[key] = record
            self._save_registry()
            return record

    async def set_dispatch(
        self,
        *,
        mode: str,
        single_account_key: str | None = None,
        enabled_account_keys: set[str] | None = None,
    ) -> None:
        """原子保存调度模式，并同步所有账号启停状态。"""
        normalized_mode = mode.strip().lower()
        async with self._registry_lock:
            if normalized_mode == "single":
                if not single_account_key or single_account_key not in self._records:
                    raise ValueError("single account mode requires a valid account")
                selected_keys = {single_account_key}
            elif normalized_mode == "multi":
                selected_keys = set(enabled_account_keys or set())
                if not selected_keys:
                    raise ValueError("multi account mode requires at least one enabled account")
                if not selected_keys.issubset(self._records):
                    raise ValueError("multi account mode contains an unknown account")
            else:
                raise ValueError("dispatch mode must be single or multi")

            self.dispatch_mode = normalized_mode
            self.single_account_key = single_account_key if normalized_mode == "single" else None
            self._records = {
                key: OAuthAccountRecord(
                    key=record.key,
                    alias=record.alias,
                    enabled=key in selected_keys,
                    source=record.source,
                    max_concurrency=record.max_concurrency,
                )
                for key, record in self._records.items()
            }
            self._save_registry()

    async def delete(self, key: str) -> None:
        """删除账号注册信息及其项目内凭据。"""
        lock = self._account_locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self._registry_lock:
                if key not in self._records:
                    raise KeyError(key)
                if self.dispatch_mode == "single" and key == self.single_account_key:
                    raise ValueError("select another single account before deleting this account")
                self._records.pop(key)
                self._save_registry()
            shutil.rmtree(self.accounts_dir / key, ignore_errors=True)

    def _prepare_directories(self) -> None:
        """创建账号库目录并收紧权限。"""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.accounts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.accounts_dir, 0o700)

    def _load_registry(self) -> None:
        """读取非敏感账号注册信息。"""
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        entries = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return
        mode = raw.get("dispatch_mode")
        if mode in {"single", "multi"}:
            self.dispatch_mode = mode
        single_key = raw.get("single_account_key")
        self.single_account_key = single_key if self.dispatch_mode == "single" and isinstance(single_key, str) else None
        registry_changed = False
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            try:
                record = OAuthAccountRecord(
                    key=item["key"],
                    alias=str(item.get("alias") or item["key"]),
                    enabled=bool(item.get("enabled", True)),
                    source=str(item.get("source") or "unknown"),
                    max_concurrency=item.get("max_concurrency"),
                )
            except (TypeError, ValueError):
                continue
            generated_alias = f"账号 {record.key[-6:]}"
            if record.alias == generated_alias:
                # 旧账号直接读取自己的本地凭据补全名称，不依赖它再次成为当前 CLI 登录。
                credentials = _parse_auth_file(self.auth_path(record.key))
                identity_alias = _identity_alias(credentials.id_token) if credentials else None
                if identity_alias:
                    record = OAuthAccountRecord(
                        key=record.key,
                        alias=identity_alias,
                        enabled=record.enabled,
                        source=record.source,
                        max_concurrency=record.max_concurrency,
                    )
                    registry_changed = True
            self._records[record.key] = record
        if registry_changed:
            self._save_registry()

    def _save_registry(self) -> None:
        """原子保存账号注册表。"""
        self._atomic_json_write(
            self.registry_path,
            {
                "dispatch_mode": self.dispatch_mode,
                "single_account_key": self.single_account_key,
                "accounts": [asdict(item) for item in self.list()],
            },
            mode=0o600,
        )

    @staticmethod
    def _atomic_json_write(path: Path, payload: dict[str, Any], *, mode: int) -> None:
        """在同目录写临时文件后原子替换目标文件。"""
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, mode)
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
