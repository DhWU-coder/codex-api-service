from pathlib import Path

import codex_api_service.app as app_module
from codex_api_service.app import _startup_urls
from codex_api_service.config import AppConfig, ServerConfig


def test_startup_urls_include_local_console_api_and_health(tmp_path: Path) -> None:
    """验证启动提示会包含本机控制台、API base 和健康检查地址。"""
    # 构造一个本机监听配置，确保普通本地启动不会出现局域网提示。
    config = AppConfig(project_root=tmp_path, server=ServerConfig(host="127.0.0.1", port=1888))

    # 本机监听时，所有可复制地址都应继续使用配置里的回环地址。
    urls = _startup_urls(config)

    # 三个入口都应出现，用户启动时不用再猜 /ui 路径。
    assert urls == {
        "server": "http://127.0.0.1:1888",
        "api": "http://127.0.0.1:1888/v1",
        "console": "http://127.0.0.1:1888/ui",
        "health": "http://127.0.0.1:1888/health",
    }


def test_startup_urls_include_lan_addresses_for_wildcard_host(tmp_path: Path) -> None:
    """验证 0.0.0.0 监听时会展示局域网访问地址。"""
    # 构造一个非默认端口，确保地址来自实际配置而不是硬编码 1219。
    config = AppConfig(project_root=tmp_path, server=ServerConfig(host="0.0.0.0", port=1888))

    # 传入检测到的局域网 IP，避免测试依赖当前机器网络环境。
    urls = _startup_urls(config, lan_host="192.168.1.23")

    # 0.0.0.0 仍作为监听地址展示，局域网入口单独给出可复制 URL。
    assert urls == {
        "server": "http://0.0.0.0:1888",
        "api": "http://127.0.0.1:1888/v1",
        "console": "http://127.0.0.1:1888/ui",
        "health": "http://127.0.0.1:1888/health",
        "lan_api": "http://192.168.1.23:1888/v1",
        "lan_console": "http://192.168.1.23:1888/ui",
    }


def test_startup_urls_include_lan_fallback_when_detection_fails(tmp_path: Path) -> None:
    """验证无法检测局域网 IP 时启动提示不会静默缺失。"""
    # 0.0.0.0 已经允许局域网访问，但 IP 检测可能在离线环境失败。
    config = AppConfig(project_root=tmp_path, server=ServerConfig(host="0.0.0.0", port=1888))

    # lan_host 为 None 表示检测不到局域网 IP。
    urls = _startup_urls(config, lan_host=None)

    # fallback 文案提醒用户手动使用本机局域网 IP。
    assert urls["lan_note"] == "not detected, use your machine LAN IP"


def test_startup_banner_prints_lan_console_when_available(tmp_path: Path, monkeypatch, capsys) -> None:
    """验证前台启动横幅会实际打印 LAN 控制台地址。"""
    # 让检测函数返回固定地址，测试启动横幅而不依赖真实网卡。
    monkeypatch.setattr(app_module, "_detect_lan_host", lambda: "192.168.1.23")
    config = AppConfig(project_root=tmp_path, server=ServerConfig(host="0.0.0.0", port=1888))

    app_module._print_startup_banner(config)

    # 用户运行 python -m codex_api_service.app 时，应能直接复制 LAN 控制台地址。
    output = capsys.readouterr().out
    assert "LAN Console: http://192.168.1.23:1888/ui" in output
