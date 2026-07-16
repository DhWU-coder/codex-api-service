"""统一管理跨平台后台服务的命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .app import _detect_lan_host, _print_config, _startup_urls
from .config import AppConfig, load_config
from .service_manager import BackgroundServiceManager, ServiceManagerError


COMMAND_ALIASES = {
    "start": "start",
    "run": "start",
    "stop": "stop",
    "end": "stop",
    "restart": "restart",
    "uninstall": "uninstall",
}


def _build_parser() -> argparse.ArgumentParser:
    """创建统一后台服务命令解析器。"""
    parser = argparse.ArgumentParser(description="管理 Codex API Service 后台服务。")
    parser.add_argument("--config", help="指定 config.yaml 路径。")
    parser.add_argument("--print-config", action="store_true", help="打印不含密钥的有效配置。")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    subparsers.add_parser("start", aliases=["run"], help="注册并启动后台服务。")
    subparsers.add_parser("stop", aliases=["end"], help="停止后台服务并保留注册。")
    subparsers.add_parser("restart", help="重启已注册的后台服务。")
    subparsers.add_parser("uninstall", help="停止并删除后台服务注册。")
    return parser


def _print_access_urls(
    config: AppConfig,
    *,
    lan_host_detector: Callable[[], str | None],
) -> None:
    """打印当前配置对应的本机和外部访问地址。"""
    lan_host = lan_host_detector() if config.server.host == "0.0.0.0" else None
    urls = _startup_urls(config, lan_host=lan_host)
    print(f"Local API:        {urls['api']}")
    print(f"Local Console:    {urls['console']}")
    if "lan_api" in urls:
        print(f"External API:     {urls['lan_api']}")
        print(f"External Console: {urls['lan_console']}")
    elif "lan_note" in urls:
        print(f"External address: {urls['lan_note']}")
    print(f"Health:           {urls['health']}")
    if config.server.host == "0.0.0.0" and not config.api.local_api_key:
        print("WARNING: 当前允许外部访问但未配置 API key，任何可连接该端口的设备都能调用服务。")


def _default_project_root() -> Path:
    """从可编辑安装的包路径定位项目根目录。"""
    return Path(__file__).resolve().parents[1]


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path | None = None,
    manager: Any | None = None,
    lan_host_detector: Callable[[], str | None] | None = None,
) -> int:
    """解析命令并执行后台服务操作。"""
    parser = _build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.command is None and not args.print_config:
        parser.print_help()
        return 0

    root = (project_root or _default_project_root()).resolve()
    config_path = Path(args.config).expanduser() if args.config else root / "config.yaml"
    config = load_config(project_root=root, config_path=config_path)
    if args.print_config:
        _print_config(config)
        return 0

    if manager is None:
        manager = BackgroundServiceManager(project_root=root)

    action = COMMAND_ALIASES[args.command]
    try:
        result = getattr(manager, action)()
    except ServiceManagerError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(result.message)
    if action in {"start", "restart"}:
        _print_access_urls(config, lan_host_detector=lan_host_detector or _detect_lan_host)
    return 0
