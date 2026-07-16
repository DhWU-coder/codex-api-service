"""验证跨平台后台服务生命周期管理。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from codex_api_service.service_manager import (
    BackgroundServiceManager,
    ServiceCommandError,
    ServiceNotInstalledError,
    UnsupportedPlatformError,
)


class FakeCommandRunner:
    """记录系统命令并按测试场景返回模拟结果。"""

    def __init__(self, responder: Callable[[list[str]], tuple[int, str, str]]) -> None:
        self.responder = responder
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """返回与 subprocess.run 相同形状的结果。"""
        self.calls.append(command)
        return_code, stdout, stderr = self.responder(command)
        return subprocess.CompletedProcess(command, return_code, stdout=stdout, stderr=stderr)


def _success(_command: list[str]) -> tuple[int, str, str]:
    """为不关心状态的命令返回成功结果。"""
    return 0, "", ""


def _manager(
    tmp_path: Path,
    *,
    platform_name: str,
    runner: FakeCommandRunner,
) -> BackgroundServiceManager:
    """构造路径完全隔离的后台服务管理器。"""
    project_root = tmp_path / "project"
    (project_root / "scripts").mkdir(parents=True)
    (project_root / "scripts" / "run_service.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (project_root / "scripts" / "run_service.ps1").write_text("# 内部启动脚本\n", encoding="utf-8")
    return BackgroundServiceManager(
        project_root=project_root,
        platform_name=platform_name,
        home=tmp_path / "home",
        command_runner=runner,
        user_id=501,
    )


def test_macos_start_keeps_existing_running_service_untouched(tmp_path: Path) -> None:
    """验证 macOS 已运行服务不会被 start 重写或重启。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="darwin", runner=runner)
    manager.macos_plist_path.parent.mkdir(parents=True)
    manager.macos_plist_path.write_text("legacy plist", encoding="utf-8")

    result = manager.start()

    assert result.changed is False
    assert "已在运行" in result.message
    assert manager.macos_plist_path.read_text(encoding="utf-8") == "legacy plist"
    assert runner.calls == [["launchctl", "print", "gui/501/com.codex-api-service.local"]]


def test_macos_start_registers_missing_service(tmp_path: Path) -> None:
    """验证 macOS 未加载时创建 plist 并启动服务。"""

    def responder(command: list[str]) -> tuple[int, str, str]:
        if command[:2] == ["launchctl", "print"]:
            return 113, "", "Could not find service"
        return 0, "", ""

    runner = FakeCommandRunner(responder)
    manager = _manager(tmp_path, platform_name="darwin", runner=runner)

    result = manager.start()

    assert result.changed is True
    assert manager.macos_plist_path.exists()
    plist = manager.macos_plist_path.read_text(encoding="utf-8")
    assert "com.codex-api-service.local" in plist
    assert str(manager.project_root / "scripts" / "run_service.sh") in plist
    assert runner.calls[-1] == [
        "launchctl",
        "bootstrap",
        "gui/501",
        str(manager.macos_plist_path),
    ]


def test_macos_stop_keeps_registration_file(tmp_path: Path) -> None:
    """验证 macOS stop 卸载当前进程但保留 plist。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="darwin", runner=runner)
    manager.macos_plist_path.parent.mkdir(parents=True)
    manager.macos_plist_path.write_text("registered", encoding="utf-8")

    result = manager.stop()

    assert result.changed is True
    assert manager.macos_plist_path.exists()
    assert runner.calls[-1] == [
        "launchctl",
        "bootout",
        "gui/501",
        str(manager.macos_plist_path),
    ]


def test_macos_restart_requires_registration(tmp_path: Path) -> None:
    """验证 macOS 未注册时 restart 明确失败。"""
    manager = _manager(tmp_path, platform_name="darwin", runner=FakeCommandRunner(_success))

    with pytest.raises(ServiceNotInstalledError, match="start"):
        manager.restart()


def test_macos_restart_loaded_service_uses_kickstart(tmp_path: Path) -> None:
    """验证 macOS 已加载服务使用 kickstart 原地重启。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="darwin", runner=runner)
    manager.macos_plist_path.parent.mkdir(parents=True)
    manager.macos_plist_path.write_text("registered", encoding="utf-8")

    result = manager.restart()

    assert result.changed is True
    assert runner.calls[-1] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.codex-api-service.local",
    ]


def test_macos_uninstall_removes_registration(tmp_path: Path) -> None:
    """验证 macOS uninstall 停止并删除 plist。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="darwin", runner=runner)
    manager.macos_plist_path.parent.mkdir(parents=True)
    manager.macos_plist_path.write_text("registered", encoding="utf-8")

    result = manager.uninstall()

    assert result.changed is True
    assert not manager.macos_plist_path.exists()
    assert any(command[:2] == ["launchctl", "bootout"] for command in runner.calls)


def test_linux_start_active_service_does_not_restart_or_rewrite(tmp_path: Path) -> None:
    """验证 Linux 已运行服务保持现有进程和 unit 不变。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="linux", runner=runner)
    manager.linux_unit_path.parent.mkdir(parents=True)
    manager.linux_unit_path.write_text("legacy unit", encoding="utf-8")

    result = manager.start()

    assert result.changed is False
    assert manager.linux_unit_path.read_text(encoding="utf-8") == "legacy unit"
    assert runner.calls == [["systemctl", "--user", "is-active", "--quiet", "codex-api-service.service"]]


def test_linux_start_registers_and_starts_inactive_service(tmp_path: Path) -> None:
    """验证 Linux 未运行时写入 unit 并启用启动。"""

    def responder(command: list[str]) -> tuple[int, str, str]:
        if "is-active" in command:
            return 3, "", ""
        return 0, "", ""

    runner = FakeCommandRunner(responder)
    manager = _manager(tmp_path, platform_name="linux", runner=runner)

    result = manager.start()

    assert result.changed is True
    assert manager.linux_unit_path.exists()
    unit = manager.linux_unit_path.read_text(encoding="utf-8")
    assert str(manager.project_root / "scripts" / "run_service.sh") in unit
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls
    assert ["systemctl", "--user", "enable", "codex-api-service.service"] in runner.calls
    assert ["systemctl", "--user", "start", "codex-api-service.service"] in runner.calls


def test_linux_stop_keeps_unit_and_uninstall_removes_it(tmp_path: Path) -> None:
    """验证 Linux stop 保留 unit，而 uninstall 删除注册。"""
    runner = FakeCommandRunner(_success)
    manager = _manager(tmp_path, platform_name="linux", runner=runner)
    manager.linux_unit_path.parent.mkdir(parents=True)
    manager.linux_unit_path.write_text("registered", encoding="utf-8")

    stop_result = manager.stop()

    assert stop_result.changed is True
    assert manager.linux_unit_path.exists()
    assert ["systemctl", "--user", "stop", "codex-api-service.service"] in runner.calls

    uninstall_result = manager.uninstall()

    assert uninstall_result.changed is True
    assert not manager.linux_unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", "codex-api-service.service"] in runner.calls


def test_windows_start_running_task_is_idempotent(tmp_path: Path) -> None:
    """验证 Windows 任务已运行时不重复注册或启动。"""

    def responder(command: list[str]) -> tuple[int, str, str]:
        return 0, "Running\n", ""

    runner = FakeCommandRunner(responder)
    manager = _manager(tmp_path, platform_name="win32", runner=runner)

    result = manager.start()

    assert result.changed is False
    assert len(runner.calls) == 1
    assert "Get-ScheduledTask" in runner.calls[0][-1]


def test_windows_start_missing_task_registers_and_starts(tmp_path: Path) -> None:
    """验证 Windows 缺少计划任务时完成注册和启动。"""

    def responder(command: list[str]) -> tuple[int, str, str]:
        if "Get-ScheduledTask" in command[-1]:
            return 0, "Missing\n", ""
        return 0, "", ""

    runner = FakeCommandRunner(responder)
    manager = _manager(tmp_path, platform_name="win32", runner=runner)

    result = manager.start()

    scripts = "\n".join(command[-1] for command in runner.calls)
    assert result.changed is True
    assert "Register-ScheduledTask" in scripts
    assert "Start-ScheduledTask" in scripts
    assert str(manager.project_root / "scripts" / "run_service.ps1") in scripts


def test_windows_stop_keeps_task_and_uninstall_unregisters_it(tmp_path: Path) -> None:
    """验证 Windows stop 保留任务，uninstall 注销任务。"""

    def responder(command: list[str]) -> tuple[int, str, str]:
        if "Get-ScheduledTask" in command[-1]:
            return 0, "Running\n", ""
        return 0, "", ""

    runner = FakeCommandRunner(responder)
    manager = _manager(tmp_path, platform_name="win32", runner=runner)

    stop_result = manager.stop()
    stop_scripts = "\n".join(command[-1] for command in runner.calls)

    assert stop_result.changed is True
    assert "Stop-ScheduledTask" in stop_scripts
    assert "Unregister-ScheduledTask" not in stop_scripts

    runner.calls.clear()
    uninstall_result = manager.uninstall()
    uninstall_scripts = "\n".join(command[-1] for command in runner.calls)

    assert uninstall_result.changed is True
    assert "Unregister-ScheduledTask" in uninstall_scripts


def test_unsupported_platform_fails_clearly(tmp_path: Path) -> None:
    """验证未知平台不会静默执行错误的系统命令。"""
    manager = _manager(tmp_path, platform_name="plan9", runner=FakeCommandRunner(_success))

    with pytest.raises(UnsupportedPlatformError, match="plan9"):
        manager.start()


def test_missing_platform_command_is_converted_to_service_error(tmp_path: Path) -> None:
    """验证系统命令无法启动时返回统一错误而不是底层堆栈。"""

    def missing_command(_command: list[str]) -> subprocess.CompletedProcess[str]:
        """模拟 launchctl 或 systemctl 不存在。"""
        raise FileNotFoundError("command not found")

    project_root = tmp_path / "project"
    project_root.mkdir()
    manager = BackgroundServiceManager(
        project_root=project_root,
        platform_name="darwin",
        home=tmp_path / "home",
        command_runner=missing_command,
        user_id=501,
    )

    with pytest.raises(ServiceCommandError, match="无法执行系统命令"):
        manager.start()
