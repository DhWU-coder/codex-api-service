from pathlib import Path

from codex_api_service.app import _startup_urls
from codex_api_service.config import AppConfig, ServerConfig


def test_startup_urls_include_console_api_and_health(tmp_path: Path) -> None:
    """验证启动提示会包含控制台、API base 和健康检查地址。"""
    # 构造一个非默认端口，确保地址来自实际配置而不是硬编码 1219。
    config = AppConfig(project_root=tmp_path, server=ServerConfig(host="0.0.0.0", port=1888))

    # 0.0.0.0 适合监听，但给用户看的本机访问地址应转换成 127.0.0.1。
    urls = _startup_urls(config)

    # 三个入口都应出现，用户启动时不用再猜 /ui 路径。
    assert urls == {
        "server": "http://0.0.0.0:1888",
        "api": "http://127.0.0.1:1888/v1",
        "console": "http://127.0.0.1:1888/ui",
        "health": "http://127.0.0.1:1888/health",
    }
