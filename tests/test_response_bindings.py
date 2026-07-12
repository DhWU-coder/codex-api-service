import sqlite3
from pathlib import Path

from codex_api_service.response_bindings import ResponseBindingStore


def test_response_binding_survives_restart_without_storing_raw_id(tmp_path: Path) -> None:
    """验证响应链绑定可持久化，数据库不保存原始 response ID。"""
    path = tmp_path / "state.sqlite3"
    store = ResponseBindingStore(path, ttl_seconds=60, clock=lambda: 100)
    store.bind("resp_sensitive", "account-a")
    store.close()

    reopened = ResponseBindingStore(path, ttl_seconds=60, clock=lambda: 120)
    assert reopened.lookup("resp_sensitive") == "account-a"
    reopened.close()

    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT response_hash FROM response_bindings").fetchall()
    connection.close()
    assert rows and rows[0][0] != "resp_sensitive"


def test_expired_response_binding_is_removed(tmp_path: Path) -> None:
    """验证过期响应链不会继续绑定旧账号。"""
    now = [100.0]
    store = ResponseBindingStore(tmp_path / "state.sqlite3", ttl_seconds=10, clock=lambda: now[0])
    store.bind("resp_old", "account-a")
    now[0] = 111

    assert store.lookup("resp_old") is None
    assert store.cleanup() == 0
    store.close()
