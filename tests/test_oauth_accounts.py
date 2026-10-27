import base64
import json
import stat
from pathlib import Path

import pytest

from codex_api_service.auth import CodexCredentials
from codex_api_service.oauth_accounts import OAuthAccountStore


def credentials(account_id: str, access: str, *, id_token: str | None = None) -> CodexCredentials:
    """构造带稳定账号标识的测试凭据。"""
    return CodexCredentials(
        access=access,
        refresh=f"refresh-{access}",
        expires=4_102_444_800_000,
        account_id=account_id,
        id_token=id_token,
    )


def unsigned_id_token(payload: dict[str, str]) -> str:
    """构造只用于解析声明的测试 JWT，不包含真实签名。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{claims}.signature"


@pytest.mark.asyncio
async def test_import_credentials_uses_email_as_default_alias(tmp_path: Path) -> None:
    """验证新账号优先使用 ID Token 邮箱作为可读名称。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")

    record = await store.import_credentials(
        credentials(
            "acct-email",
            "access-email",
            id_token=unsigned_id_token({"email": "owner@example.com", "name": "示例用户"}),
        ),
        source="web-oauth",
    )

    assert record.alias == "owner@example.com"


@pytest.mark.asyncio
async def test_import_credentials_backfills_only_generated_alias(tmp_path: Path) -> None:
    """验证再次同步仅补全自动别名，不覆盖用户手动重命名。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    generated = await store.import_credentials(credentials("acct-a", "access-a"), source="web-oauth")

    backfilled = await store.import_credentials(
        credentials("acct-a", "access-b", id_token=unsigned_id_token({"name": "张三"})),
        source="codex-cli",
    )
    assert generated.alias.startswith("账号 ")
    assert backfilled.alias == "张三"

    await store.update(backfilled.key, alias="主力账号")
    preserved = await store.import_credentials(
        credentials("acct-a", "access-c", id_token=unsigned_id_token({"email": "new@example.com"})),
        source="codex-cli",
    )
    assert preserved.alias == "主力账号"


@pytest.mark.asyncio
async def test_loading_registry_backfills_generated_alias_from_saved_id_token(tmp_path: Path) -> None:
    """验证旧账号无需再次登录，也能从自己的已保存凭据补全邮箱。"""
    root = tmp_path / ".codex-oauth"
    store = OAuthAccountStore(root)
    record = await store.import_credentials(credentials("acct-old", "access-old"), source="web-oauth")
    auth_payload = json.loads(store.auth_path(record.key).read_text(encoding="utf-8"))
    auth_payload["tokens"]["id_token"] = unsigned_id_token({"email": "old@example.com"})
    store.auth_path(record.key).write_text(json.dumps(auth_payload), encoding="utf-8")

    reloaded = OAuthAccountStore(root)

    reloaded_record = reloaded.get(record.key)
    assert reloaded_record is not None
    assert reloaded_record.alias == "old@example.com"


@pytest.mark.asyncio
async def test_import_credentials_updates_the_real_account_only(tmp_path: Path) -> None:
    """验证同步 B 的凭据不会覆盖触发同步的 A。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    account_a = await store.import_credentials(credentials("acct-a", "access-a"), source="web")
    account_b = await store.import_credentials(credentials("acct-b", "access-b-old"), source="web")

    updated = await store.import_credentials(credentials("acct-b", "access-b-new"), source="codex-cli")

    assert updated.key == account_b.key
    assert json.loads(store.auth_path(account_a.key).read_text())["tokens"]["access_token"] == "access-a"
    assert json.loads(store.auth_path(account_b.key).read_text())["tokens"]["access_token"] == "access-b-new"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.auth_path(account_b.key).stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_account_registry_supports_management_and_unlimited_concurrency(tmp_path: Path) -> None:
    """验证账号注册信息支持别名、启停和默认无限并发。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    record = await store.import_credentials(credentials("acct-a", "access-a"), source="codex-cli")

    assert record.max_concurrency is None
    renamed = await store.update(record.key, alias="主账号", max_concurrency=3)

    assert renamed.alias == "主账号"
    assert renamed.enabled is True
    assert renamed.max_concurrency == 3
    assert store.get(record.key) == renamed


@pytest.mark.asyncio
async def test_dispatch_defaults_to_multi_and_persists_single_selection(tmp_path: Path) -> None:
    """验证默认多账户模式以及单账户选择会和启停状态一起持久化。"""
    root = tmp_path / ".codex-oauth"
    store = OAuthAccountStore(root)
    account_a = await store.import_credentials(credentials("acct-a", "access-a"), source="web")
    account_b = await store.import_credentials(credentials("acct-b", "access-b"), source="web")

    assert store.dispatch_mode == "multi"
    assert store.single_account_key is None

    await store.set_dispatch(mode="single", single_account_key=account_b.key)
    reloaded = OAuthAccountStore(root)

    assert reloaded.dispatch_mode == "single"
    assert reloaded.single_account_key == account_b.key
    assert reloaded.get(account_a.key) is not None and reloaded.get(account_a.key).enabled is False
    assert reloaded.get(account_b.key) is not None and reloaded.get(account_b.key).enabled is True


@pytest.mark.asyncio
async def test_multi_dispatch_requires_at_least_one_enabled_account(tmp_path: Path) -> None:
    """验证多账户批量设置不会保存空选择或部分状态。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    account = await store.import_credentials(credentials("acct-a", "access-a"), source="web")

    with pytest.raises(ValueError, match="at least one"):
        await store.set_dispatch(mode="multi", enabled_account_keys=set())

    assert store.dispatch_mode == "multi"
    assert store.get(account.key) is not None and store.get(account.key).enabled is True


@pytest.mark.asyncio
async def test_single_mode_enable_switches_account_and_new_account_stays_disabled(tmp_path: Path) -> None:
    """验证单账户模式在账号页启用其他账号时切换选择，新增账号保持停用。"""
    store = OAuthAccountStore(tmp_path / ".codex-oauth")
    account_a = await store.import_credentials(credentials("acct-a", "access-a"), source="web")
    account_b = await store.import_credentials(credentials("acct-b", "access-b"), source="web")
    await store.set_dispatch(mode="single", single_account_key=account_a.key)

    await store.update(account_b.key, enabled=True)
    account_c = await store.import_credentials(credentials("acct-c", "access-c"), source="web")

    assert store.single_account_key == account_b.key
    assert store.get(account_a.key) is not None and store.get(account_a.key).enabled is False
    assert store.get(account_b.key) is not None and store.get(account_b.key).enabled is True
    assert store.get(account_c.key) is not None and store.get(account_c.key).enabled is False
    with pytest.raises(ValueError, match="single account"):
        await store.update(account_b.key, enabled=False)


@pytest.mark.asyncio
async def test_import_auth_file_preserves_codex_native_metadata(tmp_path: Path) -> None:
    """验证同步时保留 app-server 判断登录状态所需的原生字段。"""
    source = tmp_path / "codex-auth.json"
    source.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": "2026-07-12T02:44:43Z",
                "tokens": {
                    "access_token": "access-a",
                    "refresh_token": "refresh-a",
                    "account_id": "acct-a",
                    "id_token": "id-a",
                },
            }
        ),
        encoding="utf-8",
    )
    store = OAuthAccountStore(tmp_path / ".codex-oauth")

    record = await store.import_auth_file(source, source="codex-cli")
    saved = json.loads(store.auth_path(record.key).read_text(encoding="utf-8"))

    assert saved["last_refresh"] == "2026-07-12T02:44:43Z"
    assert "OPENAI_API_KEY" in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplier", [0.000001, 0.3, 1.25, 7.5, 1_000_000])
async def test_custom_weight_multiplier_survives_restart_and_account_updates(tmp_path: Path, multiplier: float) -> None:
    """验证自定义小数及边界值保存后可重启恢复，管理和授权更新不清空倍率。"""
    store = OAuthAccountStore(tmp_path)
    account = await store.import_credentials(credentials("weighted", "web-old"), source="web-oauth")
    other = await store.import_credentials(credentials("other", "other-web"), source="web-oauth")
    assert account.weight_multiplier == 1
    await store.update(account.key, weight_multiplier=multiplier)
    await store.update(account.key, alias="自定义账号", max_concurrency=3)
    await store.import_credentials(credentials("weighted", "cli-new"), source="codex-cli")
    await store.set_dispatch(mode="single", single_account_key=other.key)
    await store.update(account.key, enabled=True)
    await store.set_dispatch(mode="multi", enabled_account_keys={account.key, other.key})
    await store.update(account.key, enabled=False)
    await store.update(account.key, enabled=True)
    restarted = OAuthAccountStore(tmp_path)
    restored = restarted.get(account.key)
    assert restored.weight_multiplier == multiplier
    assert restored.alias == "自定义账号"
    assert restored.max_concurrency == 3
    assert restored.enabled
    await restarted.update(account.key, weight_multiplier=1)
    assert OAuthAccountStore(tmp_path).get(account.key).weight_multiplier == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, True, False, "2", 0, -1, 0.0000001, 1_000_001, float("nan"), float("inf"), [], {}, 10 ** 1000])
async def test_invalid_multiplier_does_not_partially_change_dispatch(tmp_path: Path, invalid: object) -> None:
    """验证非法输入不会让单账号启停状态或原倍率发生部分变更。"""
    store = OAuthAccountStore(tmp_path)
    first = await store.import_credentials(credentials("first", "first-web"), source="web-oauth")
    second = await store.import_credentials(credentials("second", "second-web"), source="web-oauth")
    await store.set_dispatch(mode="single", single_account_key=first.key)
    with pytest.raises(ValueError, match="权重倍率"):
        await store.update(second.key, enabled=True, weight_multiplier=invalid)
    assert store.single_account_key == first.key
    assert store.get(first.key).enabled
    assert not store.get(second.key).enabled
    assert store.get(second.key).weight_multiplier == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, False, "oops", -1, 0, 0.0000001, 1_000_001])
async def test_old_or_invalid_registry_multiplier_keeps_account_with_default(tmp_path: Path, stored: object) -> None:
    """验证旧字段缺失及手工写坏的倍率不会删除账号或改变默认调度。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credentials("legacy", "legacy-web"), source="web-oauth")
    payload = json.loads(store.registry_path.read_text())
    if stored is None:
        payload["accounts"][0].pop("weight_multiplier")
    else:
        payload["accounts"][0]["weight_multiplier"] = stored
    store.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    assert OAuthAccountStore(tmp_path).get(record.key).weight_multiplier == 1


@pytest.mark.asyncio
async def test_generated_alias_backfill_keeps_weight_multiplier(tmp_path: Path) -> None:
    """验证加载时旧账号补全邮箱不会丢失用户倍率。"""
    store = OAuthAccountStore(tmp_path)
    record = await store.import_credentials(credentials("backfill", "old-web"), source="web-oauth")
    await store.update(record.key, weight_multiplier=1.25)
    payload = json.loads(store.auth_path(record.key).read_text())
    payload["tokens"]["id_token"] = unsigned_id_token({"email": "owner@example.com"})
    store.auth_path(record.key).write_text(json.dumps(payload), encoding="utf-8")
    restored = OAuthAccountStore(tmp_path).get(record.key)
    assert restored.alias == "owner@example.com"
    assert restored.weight_multiplier == 1.25
