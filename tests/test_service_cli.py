"""验证统一后台服务命令行入口。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_api_service.cli import main
from codex_api_service.service_manager import ServiceNotInstalledError


class FakeServiceManager:
    """记录 CLI 发起的后台服务操作，不接触真实系统服务。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> SimpleNamespace:
        """模拟后台启动。"""
        self.calls.append("start")
        return SimpleNamespace(message="后台服务已启动")

    def stop(self) -> SimpleNamespace:
        """模拟后台停止。"""
        self.calls.append("stop")
        return SimpleNamespace(message="后台服务已停止")

    def restart(self) -> SimpleNamespace:
        """模拟后台重启。"""
        self.calls.append("restart")
        return SimpleNamespace(message="后台服务已重启")

    def uninstall(self) -> SimpleNamespace:
        """模拟后台卸载。"""
        self.calls.append("uninstall")
        return SimpleNamespace(message="后台服务已卸载")


def _write_config(root: Path, *, host: str, api_key: str | None = None) -> None:
    """写入 CLI 测试使用的最小配置。"""
    key_value = api_key or ""
    (root / "config.yaml").write_text(
        f"server:\n  host: {host}\n  port: 1888\napi:\n  local_api_key: {key_value}\n",
        encoding="utf-8",
    )


def test_no_arguments_prints_help_without_touching_service(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """验证无参数调用只显示帮助，不隐式启动后台服务。"""
    manager = FakeServiceManager()

    exit_code = main([], project_root=tmp_path, manager=manager)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "start" in output
    assert "restart" in output
    assert manager.calls == []


@pytest.mark.parametrize(
    ("command", "expected_call"),
    [
        ("start", "start"),
        ("run", "start"),
        ("stop", "stop"),
        ("end", "stop"),
        ("restart", "restart"),
        ("uninstall", "uninstall"),
    ],
)
def test_commands_and_aliases_dispatch_to_service_manager(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    expected_call: str,
) -> None:
    """验证公开命令及别名统一映射到后台服务管理层。"""
    _write_config(tmp_path, host="127.0.0.1")
    manager = FakeServiceManager()

    exit_code = main([command], project_root=tmp_path, manager=manager)

    capsys.readouterr()
    assert exit_code == 0
    assert manager.calls == [expected_call]


def test_start_prints_external_urls_and_auth_warning_for_wildcard_host(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证通配监听启动后打印外部接口和无鉴权警告。"""
    _write_config(tmp_path, host="0.0.0.0")
    manager = FakeServiceManager()

    exit_code = main(
        ["start"],
        project_root=tmp_path,
        manager=manager,
        lan_host_detector=lambda: "192.168.1.23",
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Local API:        http://127.0.0.1:1888/v1" in output
    assert "External API:     http://192.168.1.23:1888/v1" in output
    assert "External Console: http://192.168.1.23:1888/ui" in output
    assert "未配置 API key" in output


def test_stop_does_not_print_access_urls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """验证停止命令只打印操作结果，不展示已停止服务的地址。"""
    _write_config(tmp_path, host="0.0.0.0")
    manager = FakeServiceManager()

    exit_code = main(["stop"], project_root=tmp_path, manager=manager)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "后台服务已停止" in output
    assert "External API" not in output


def test_service_error_is_printed_to_stderr_with_nonzero_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证后台管理失败转换成稳定的 CLI 错误而不是堆栈。"""

    class FailingManager(FakeServiceManager):
        """模拟尚未注册的后台服务。"""

        def restart(self) -> SimpleNamespace:
            """模拟 restart 的未注册错误。"""
            raise ServiceNotInstalledError("请先执行 codex-api-service start。")

    _write_config(tmp_path, host="127.0.0.1")

    exit_code = main(["restart"], project_root=tmp_path, manager=FailingManager())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "请先执行 codex-api-service start" in captured.err


def test_print_config_remains_available_on_unified_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证统一入口继续支持原有的安全配置查看能力。"""
    _write_config(tmp_path, host="0.0.0.0", api_key="secret")

    exit_code = main(["--print-config"], project_root=tmp_path)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"host": "0.0.0.0"' in output
    assert '"local_api_key_configured": true' in output
    assert "secret" not in output
