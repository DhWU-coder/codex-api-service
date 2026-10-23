"""为同一账号隔离保存授权来源，并按真实凭据更新顺序选择和回退。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth import (
    CodexAuth,
    CodexCredentials,
    OAuthLoginRequired,
    _credentials_from_auth_payload,
    _is_refresh_token_invalidated,
    _parse_auth_file,
    credentials_expired_or_near,
)


def credentials_fingerprint(credentials: CodexCredentials) -> str:
    """只比较不可逆摘要，禁止将令牌正文用于状态展示。"""
    return hashlib.sha256(f"{credentials.access}\0{credentials.refresh}".encode()).hexdigest()


def atomic_json_write(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    """在目标目录内原子替换文件，权限从创建时即限制为当前用户。"""
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


def _read_json(path: Path) -> dict[str, Any]:
    """对缺失或损坏的 JSON 返回空对象，不泄露原文件内容。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_name(source: str) -> str:
    """只使用固定来源名构造目录，未知历史来源保存在兼容槽。"""
    if source in {"web", "web-oauth"}:
        return "web-oauth"
    return "codex-cli" if source == "codex-cli" else "legacy"


@dataclass(frozen=True)
class AuthorizationSelection:
    """描述当前选中授权，敏感数据只在服务内部传递。"""

    source: str
    credentials: CodexCredentials
    fingerprint: str
    auth_path: Path


class AccountAuthorizations:
    """管理一个真实账号的 Web、CLI 和迁移兼容授权。"""

    def __init__(self, account_home: Path, *, legacy_source: str) -> None:
        self.home = account_home
        self.auth_path = account_home / "auth.json"
        self.state_path = account_home / "authorizations.json"
        self.legacy_source = _source_name(legacy_source)
        state = _read_json(self.state_path)
        self.profiles: dict[str, dict[str, Any]] = {
            source: metadata
            for source, metadata in (state.get("profiles") or {}).items()
            if source in {"web-oauth", "codex-cli", "legacy"} and isinstance(metadata, dict)
        } if isinstance(state.get("profiles"), dict) else {}
        rejected = state.get("rejected")
        self.rejected = {item for item in rejected if isinstance(item, str)} if isinstance(rejected, list) else set()
        self.active_source: str | None = state.get("activeSource")
        self.account_id: str | None = state.get("accountId")
        self._migrated = bool(state)

    def profile_path(self, source: str) -> Path:
        """返回固定授权槽的原生认证文件。"""
        return self.home / "authorizations" / _source_name(source) / "auth.json"

    def _save(self) -> None:
        """保存脱敏状态，拒绝摘要也跨服务重启保留。"""
        atomic_json_write(self.state_path, {
            "profiles": self.profiles,
            "rejected": sorted(self.rejected),
            "activeSource": self.active_source,
            "accountId": self.account_id,
        })

    def _write_profile(self, source: str, payload: dict[str, Any]) -> None:
        """逐级限制授权目录权限，不能只依赖父账号目录的隔离。"""
        directory = self.home / "authorizations"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        target = self.profile_path(source)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target.parent, 0o700)
        atomic_json_write(target, payload)

    def _migrate(self) -> None:
        """只迁移仍存在的旧凭据，不捏造已被覆盖的另一种授权。"""
        if self._migrated:
            return
        credentials = _parse_auth_file(self.auth_path)
        if credentials is not None:
            fingerprint = credentials_fingerprint(credentials)
            self.account_id = credentials.account_id
            self._write_profile(self.legacy_source, _read_json(self.auth_path))
            self.profiles[self.legacy_source] = {
                "fingerprint": fingerprint,
                "importedFingerprint": fingerprint,
                "updatedAt": self.auth_path.stat().st_mtime,
            }
            self.active_source = self.legacy_source
        rejection = _read_json(self.home / "auth-rejection.json").get("fingerprint")
        if isinstance(rejection, str):
            self.rejected.add(rejection)
        if credentials is not None or self.rejected:
            self._save()
        self._migrated = True

    def import_payload(self, payload: dict[str, Any], *, source: str, updated_at: float) -> None:
        """更新指定来源；重复导入不能覆盖已轮换的令牌或抬高优先级。"""
        self._migrate()
        source = _source_name(source)
        tokens = payload["tokens"]
        if self.account_id is not None and tokens.get("account_id") != self.account_id:
            raise ValueError("OAuth account identity changed")
        self.account_id = tokens.get("account_id")
        credentials = _credentials_from_auth_payload(payload)
        if credentials is None:
            raise ValueError("OAuth auth file is invalid")
        fingerprint = credentials_fingerprint(credentials)
        previous = self.profiles.get(source, {})
        if previous.get("importedFingerprint") == fingerprint:
            self.select()
            return
        current = _parse_auth_file(self.profile_path(source))
        if fingerprint in self.rejected and current is not None and credentials_fingerprint(current) not in self.rejected:
            # 全局文件意外回滚到已拒绝版本时，不能销毁本槽已经刷新的健康凭据。
            previous["importedFingerprint"] = fingerprint
            self._save()
            self.select()
            return
        self._write_profile(source, payload)
        self.profiles[source] = {
            "fingerprint": fingerprint,
            "importedFingerprint": fingerprint,
            "updatedAt": updated_at,
        }
        self._save()
        self.select()

    def select(self) -> AuthorizationSelection | None:
        """选取最新且未明确失效的授权，并维护旧路径的兼容镜像。"""
        self._migrate()
        self._sync_legacy_mirror()
        choices: list[tuple[float, AuthorizationSelection]] = []
        changed = False
        for source, metadata in self.profiles.items():
            path = self.profile_path(source)
            credentials = _parse_auth_file(path)
            if credentials is None or credentials.account_id != self.account_id:
                continue
            fingerprint = credentials_fingerprint(credentials)
            if metadata.get("fingerprint") != fingerprint:
                # app-server 可能自己刷新令牌；只更新它所属的授权槽，不接收其他账号。
                metadata["fingerprint"] = fingerprint
                metadata["updatedAt"] = path.stat().st_mtime
                changed = True
            if fingerprint not in self.rejected:
                choices.append((float(metadata.get("updatedAt", 0)), AuthorizationSelection(source, credentials, fingerprint, path)))
        selection = max(choices, key=lambda item: (item[0], item[1].source == self.active_source), default=None)
        selected = selection[1] if selection else None
        next_source = selected.source if selected else None
        if next_source != self.active_source:
            self.active_source = next_source
            changed = True
        if selected is not None:
            current = _parse_auth_file(self.auth_path)
            if current is None or credentials_fingerprint(current) != selected.fingerprint:
                atomic_json_write(self.auth_path, _read_json(selected.auth_path))
        if changed:
            self._save()
        return selected

    def _sync_legacy_mirror(self) -> None:
        """兼容旧单文件调用方写入的新凭据，不能用旧镜像复活拒绝记录。"""
        if self.profiles and "legacy" not in self.profiles:
            return
        credentials = _parse_auth_file(self.auth_path)
        if credentials is None:
            return
        if self.account_id is not None and credentials.account_id != self.account_id:
            return
        self.account_id = credentials.account_id
        fingerprint = credentials_fingerprint(credentials)
        known = {item.get("fingerprint") for item in self.profiles.values()}
        if fingerprint in known or fingerprint in self.rejected:
            return
        self._write_profile("legacy", _read_json(self.auth_path))
        self.profiles["legacy"] = {
            "fingerprint": fingerprint,
            "importedFingerprint": fingerprint,
            "updatedAt": self.auth_path.stat().st_mtime,
        }
        self._save()

    def reject(self, fingerprint: str) -> None:
        """仅拒绝本次实际使用的凭据，相同内容的其他来源也不重复尝试。"""
        self._migrate()
        self.rejected.add(fingerprint)
        self._save()
        self.select()

    def save_refreshed(self, selected: AuthorizationSelection, credentials: CodexCredentials) -> None:
        """按原指纹条件落盘，旧刷新不能覆盖并发写入的新登录。"""
        current = _parse_auth_file(selected.auth_path)
        if current is None or credentials_fingerprint(current) != selected.fingerprint:
            return
        if credentials.account_id != selected.credentials.account_id:
            raise OAuthLoginRequired("OAuth account identity changed", credentials_fingerprint=selected.fingerprint)
        payload = _read_json(selected.auth_path)
        payload.update({
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": credentials.access,
                "refresh_token": credentials.refresh,
                "account_id": credentials.account_id,
                **({"id_token": credentials.id_token} if credentials.id_token else {}),
            },
            "expires": credentials.expires,
            "last_refresh": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        })
        self._write_profile(selected.source, payload)
        self.profiles[selected.source]["fingerprint"] = credentials_fingerprint(credentials)
        self.profiles[selected.source]["updatedAt"] = time.time()
        self._save()
        self.select()

    def snapshot(self) -> list[dict[str, Any]]:
        """仅公开来源、状态和更新时间，不公开令牌及其摘要。"""
        selected = self.select()
        sources = ["web-oauth", "codex-cli"] + (["legacy"] if "legacy" in self.profiles else [])
        result = []
        for source in sources:
            credentials = _parse_auth_file(self.profile_path(source))
            status = "missing" if credentials is None else "login_required" if credentials.account_id != self.account_id or credentials_fingerprint(credentials) in self.rejected else "available"
            result.append({
                "source": source,
                "status": status,
                "active": selected is not None and selected.source == source,
                "updatedAt": self.profiles.get(source, {}).get("updatedAt", 0),
            })
        return result


class AccountPoolAuth(CodexAuth):
    """为账号池提供原槽刷新，认证失效由池按实际指纹执行回退。"""

    def __init__(self, authorizations: AccountAuthorizations) -> None:
        super().__init__(auth_path=authorizations.auth_path, allow_import=False, allow_interactive_login=False)
        self.authorizations = authorizations

    def ensure_credentials(self) -> CodexCredentials:
        """优先刷新当前授权，不删除备用来源，也不在本层重复请求。"""
        selected = self.authorizations.select()
        if selected is None:
            raise OAuthLoginRequired("OAuth account requires login")
        credentials = selected.credentials
        if credentials_expired_or_near(credentials):
            try:
                refreshed = self.refresh_func(credentials.refresh)
            except RuntimeError as error:
                if not _is_refresh_token_invalidated(error):
                    raise
                raise OAuthLoginRequired("OAuth account requires login", credentials_fingerprint=selected.fingerprint) from error
            self.authorizations.save_refreshed(selected, refreshed)
            # 条件更新失败意味着有新登录，下一次读取应使用新授权而非旧刷新结果。
            current = self.authorizations.select()
            if current is None:
                raise OAuthLoginRequired("OAuth account requires login", credentials_fingerprint=selected.fingerprint)
            return current.credentials
        return credentials
