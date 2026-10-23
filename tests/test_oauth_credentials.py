"""验证来源隔离、优先级、刷新落盘和持久化回退边界。"""

import json
import os
import stat
from pathlib import Path

import pytest

from codex_api_service.auth import CodexAuth, CodexCredentials, OAuthLoginRequired, _parse_auth_file
from codex_api_service.oauth_accounts import OAuthAccountRecord, OAuthAccountStore
from codex_api_service.oauth_credentials import AccountPoolAuth, credentials_fingerprint


def credential(access: str, *, expires: int = 4_102_444_800_000) -> CodexCredentials:
    """构造同一账号的无敏感信息测试凭据。"""
    return CodexCredentials(access, f"refresh-{access}", expires, "same-account")


@pytest.mark.asyncio
async def test_sources_remain_separate_and_repeated_sync_does_not_promote_old_cli(tmp_path: Path) -> None:
    """验证最新优先但重复导入不改变顺序，目录和文件均限制权限。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("cli"), source="codex-cli")
    await store.import_credentials(credential("web"), source="web-oauth")
    authorizations = store.authorizations(record.key)
    before = authorizations.snapshot()
    await store.import_credentials(credential("cli"), source="codex-cli")

    assert len(store.list()) == 1
    assert authorizations.select().source == "web-oauth"
    assert before == authorizations.snapshot()
    assert _parse_auth_file(authorizations.profile_path("codex-cli")).access == "cli"
    assert _parse_auth_file(store.auth_path(record.key)).access == "web"
    for path in (authorizations.state_path, authorizations.profile_path("web-oauth"), authorizations.profile_path("codex-cli")):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((authorizations.home / "authorizations").stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_rejection_and_fallback_survive_restart_and_unchanged_cli_import(tmp_path: Path) -> None:
    """验证失效来源跨重启保持隔离，重复同步不能复活旧授权。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("web"), source="web-oauth")
    await store.import_credentials(credential("cli"), source="codex-cli")
    authorizations = store.authorizations(record.key)
    authorizations.reject(authorizations.select().fingerprint)
    restarted = OAuthAccountStore(tmp_path)
    await restarted.import_credentials(credential("cli"), source="codex-cli")
    assert restarted.authorizations(record.key).select().source == "web-oauth"
    states = restarted.authorizations(record.key).snapshot()
    assert {item["source"]: item["status"] for item in states} == {"web-oauth": "available", "codex-cli": "login_required"}
    await restarted.import_credentials(credential("cli-new-login"), source="codex-cli")
    assert restarted.authorizations(record.key).select().source == "codex-cli"


@pytest.mark.asyncio
async def test_identical_tokens_are_never_used_as_each_others_backup(tmp_path: Path) -> None:
    """验证两个来源实为同一凭据时一次拒绝即全部隔离。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("same"), source="web-oauth")
    await store.import_credentials(credential("same"), source="codex-cli")
    authorizations = store.authorizations(record.key)
    authorizations.reject(authorizations.select().fingerprint)
    assert authorizations.select() is None
    assert all(item["status"] == "login_required" for item in authorizations.snapshot())


@pytest.mark.asyncio
async def test_refresh_is_saved_in_original_slot_and_old_global_file_cannot_overwrite_it(tmp_path: Path) -> None:
    """验证刷新后仍归属原来源，重复同步旧全局文件不覆盖旋转后的刷新令牌。"""
    store = OAuthAccountStore(tmp_path)
    expired = credential("cli-expired", expires=1)
    record = await store.import_credentials(expired, source="codex-cli")
    authorizations = store.authorizations(record.key)
    auth = AccountPoolAuth(authorizations)
    calls = []

    def refresh(refresh_token: str) -> CodexCredentials:
        """替换为可控的新一代凭据。"""
        calls.append(refresh_token)
        return credential("cli-refreshed")

    auth.refresh_func = refresh
    assert auth.ensure_credentials().access == "cli-refreshed"
    assert calls == [expired.refresh]
    await store.import_credentials(expired, source="codex-cli")
    restarted = OAuthAccountStore(tmp_path)
    selected = restarted.authorizations(record.key).select()
    assert selected.credentials.access == "cli-refreshed"
    assert selected.source == "codex-cli"
    assert _parse_auth_file(authorizations.profile_path("codex-cli")).access == "cli-refreshed"


@pytest.mark.asyncio
async def test_late_refresh_and_old_request_rejection_cannot_replace_new_login(tmp_path: Path) -> None:
    """验证旧刷新按指纹条件写入，旧请求失败只拒绝旧令牌。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("web-old"), source="web-oauth")
    authorizations = store.authorizations(record.key)
    old = authorizations.select()
    await store.import_credentials(credential("web-new-login"), source="web-oauth")
    authorizations.save_refreshed(old, credential("web-late-refresh"))
    authorizations.reject(old.fingerprint)
    assert authorizations.select().credentials.access == "web-new-login"
    assert _parse_auth_file(authorizations.profile_path("web-oauth")).access == "web-new-login"


@pytest.mark.asyncio
async def test_migration_preserves_original_native_payload_and_old_rejection(tmp_path: Path) -> None:
    """验证旧单文件按已知来源迁移，并沿用原有失效摘要。"""
    store = OAuthAccountStore(tmp_path)
    key = "legacy-account"
    store._records[key] = OAuthAccountRecord(key=key, alias="旧账号", source="web-oauth")
    credentials = credential("legacy-web")
    CodexAuth(auth_path=store.auth_path(key)).save(credentials)
    original = json.loads(store.auth_path(key).read_text())
    store.reject_credentials(key, credentials_fingerprint(credentials))
    authorizations = store.authorizations(key)
    assert authorizations.select() is None
    assert json.loads(authorizations.profile_path("web-oauth").read_text()) == original
    assert authorizations.snapshot()[1]["status"] == "missing"


@pytest.mark.asyncio
async def test_file_import_uses_original_update_time_instead_of_sync_time(tmp_path: Path) -> None:
    """验证第一次同步旧 CLI 文件时也不挤掉较新的 Web 授权。"""
    store = OAuthAccountStore(tmp_path / "store")
    record = await store.import_credentials(credential("web-new"), source="web-oauth")
    global_path = tmp_path / "global-auth.json"
    CodexAuth(auth_path=global_path).save(credential("cli-old"))
    os.utime(global_path, (1_700_000_000, 1_700_000_000))
    await store.import_auth_file(global_path, source="codex-cli")
    assert store.authorizations(record.key).select().source == "web-oauth"


@pytest.mark.asyncio
async def test_app_server_refresh_updates_its_own_profile_and_compatibility_mirror(tmp_path: Path) -> None:
    """验证原生额度进程刷新后的令牌被收回正确来源，并维持镜像同步。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("web"), source="web-oauth")
    authorizations = store.authorizations(record.key)
    CodexAuth(auth_path=authorizations.profile_path("web-oauth")).save(credential("web-app-refreshed"))
    assert authorizations.select().credentials.access == "web-app-refreshed"
    assert _parse_auth_file(store.auth_path(record.key)).access == "web-app-refreshed"
    assert authorizations.profiles["web-oauth"]["importedFingerprint"] == credentials_fingerprint(credential("web"))


@pytest.mark.asyncio
async def test_foreign_account_in_profile_is_never_selected_even_when_mirror_is_missing(tmp_path: Path) -> None:
    """验证来源文件被意外写成另一账号时不会串用身份。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("web"), source="web-oauth")
    authorizations = store.authorizations(record.key)
    store.auth_path(record.key).unlink()
    CodexAuth(auth_path=authorizations.profile_path("web-oauth")).save(
        CodexCredentials("foreign", "foreign-refresh", 4_102_444_800_000, "other-account")
    )
    assert authorizations.select() is None
    assert authorizations.snapshot()[0]["status"] == "login_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["refresh_token_invalidated", "refresh_token_reused", "refresh_token_expired", "refresh_token_revoked", "invalid_grant"])
async def test_refresh_invalidated_keeps_both_files_and_reports_actual_fingerprint(tmp_path: Path, error_code: str) -> None:
    """验证刷新明确失效只报告原凭据，不删除任何来源文件。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("expired", expires=1), source="web-oauth")
    authorizations = store.authorizations(record.key)
    auth = AccountPoolAuth(authorizations)

    def invalid_refresh(_refresh_token: str) -> CodexCredentials:
        """模拟上游明确拒绝刷新。"""
        raise RuntimeError(error_code)

    auth.refresh_func = invalid_refresh
    with pytest.raises(OAuthLoginRequired) as caught:
        auth.ensure_credentials()
    assert caught.value.credentials_fingerprint == authorizations.select().fingerprint
    assert authorizations.profile_path("web-oauth").exists()
    assert store.auth_path(record.key).exists()


@pytest.mark.asyncio
async def test_reimporting_rejected_old_cli_does_not_destroy_new_cli_slot(tmp_path: Path) -> None:
    """验证曾被拒绝的旧全局文件回流时，保留已更新的同源凭据。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credential("cli-old"), source="codex-cli")
    authorizations = store.authorizations(record.key)
    authorizations.reject(authorizations.select().fingerprint)
    await store.import_credentials(credential("cli-new"), source="codex-cli")
    await store.import_credentials(credential("cli-old"), source="codex-cli")
    assert authorizations.select().credentials.access == "cli-new"
    assert _parse_auth_file(authorizations.profile_path("codex-cli")).access == "cli-new"
