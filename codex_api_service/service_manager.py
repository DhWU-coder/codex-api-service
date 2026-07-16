"""封装 macOS、Linux 和 Windows 的后台服务生命周期。"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


MACOS_LABEL = "com.codex-api-service.local"
LINUX_SERVICE_NAME = "codex-api-service.service"
WINDOWS_TASK_NAME = "CodexApiService"

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class ServiceManagerError(RuntimeError):
    """后台服务管理失败。"""


class ServiceNotInstalledError(ServiceManagerError):
    """需要已注册服务的操作无法继续。"""


class UnsupportedPlatformError(ServiceManagerError):
    """当前操作系统没有对应的后台服务实现。"""


class ServiceCommandError(ServiceManagerError):
    """平台系统命令执行失败。"""


@dataclass(frozen=True)
class ServiceOperationResult:
    """描述一次后台服务操作是否改变了系统状态。"""

    message: str
    changed: bool


def _default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    """以无 shell 方式执行平台命令并捕获输出。"""
    return subprocess.run(command, capture_output=True, text=True, check=False)


class BackgroundServiceManager:
    """根据当前平台统一管理项目的用户级后台服务。"""

    def __init__(
        self,
        *,
        project_root: Path,
        platform_name: str | None = None,
        home: Path | None = None,
        command_runner: CommandRunner | None = None,
        user_id: int | None = None,
    ) -> None:
        """保存平台、路径和可替换的系统命令执行器。"""
        self.project_root = project_root.expanduser().resolve()
        self.platform_name = platform_name or sys.platform
        self.home = (home or Path.home()).expanduser().resolve()
        self.command_runner = command_runner or _default_command_runner
        self.user_id = user_id if user_id is not None else os.getuid() if hasattr(os, "getuid") else 0

    @property
    def macos_plist_path(self) -> Path:
        """返回 macOS 用户 LaunchAgent 配置路径。"""
        return self.home / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"

    @property
    def linux_unit_path(self) -> Path:
        """返回 Linux systemd 用户服务配置路径。"""
        return self.home / ".config" / "systemd" / "user" / LINUX_SERVICE_NAME

    def start(self) -> ServiceOperationResult:
        """注册并后台启动服务，已运行时保持现有进程不变。"""
        if self.platform_name == "darwin":
            return self._start_macos()
        if self.platform_name.startswith("linux"):
            return self._start_linux()
        if self.platform_name.startswith("win"):
            return self._start_windows()
        raise UnsupportedPlatformError(f"不支持的平台：{self.platform_name}")

    def stop(self) -> ServiceOperationResult:
        """停止后台服务但保留系统注册。"""
        if self.platform_name == "darwin":
            return self._stop_macos()
        if self.platform_name.startswith("linux"):
            return self._stop_linux()
        if self.platform_name.startswith("win"):
            return self._stop_windows()
        raise UnsupportedPlatformError(f"不支持的平台：{self.platform_name}")

    def restart(self) -> ServiceOperationResult:
        """重启已注册的后台服务。"""
        if self.platform_name == "darwin":
            return self._restart_macos()
        if self.platform_name.startswith("linux"):
            return self._restart_linux()
        if self.platform_name.startswith("win"):
            return self._restart_windows()
        raise UnsupportedPlatformError(f"不支持的平台：{self.platform_name}")

    def uninstall(self) -> ServiceOperationResult:
        """停止后台服务并删除系统注册。"""
        if self.platform_name == "darwin":
            return self._uninstall_macos()
        if self.platform_name.startswith("linux"):
            return self._uninstall_linux()
        if self.platform_name.startswith("win"):
            return self._uninstall_windows()
        raise UnsupportedPlatformError(f"不支持的平台：{self.platform_name}")

    def _run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """执行系统命令，并把失败转换成稳定的管理异常。"""
        try:
            result = self.command_runner(command)
        except OSError as error:
            raise ServiceCommandError(f"无法执行系统命令 {command[0]}：{error}") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "未知错误").strip()
            raise ServiceCommandError(f"命令执行失败：{' '.join(command)}：{detail}")
        return result

    def _ensure_runner(self, path: Path) -> None:
        """确认内部启动脚本存在，并为 Unix 脚本补充执行权限。"""
        if not path.exists():
            raise ServiceManagerError(f"内部启动脚本不存在：{path}")
        if path.suffix == ".sh":
            path.chmod(path.stat().st_mode | 0o111)

    def _write_text_atomically(self, path: Path, content: str) -> None:
        """通过同目录临时文件原子替换服务配置。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(path)

    def _macos_target(self) -> str:
        """返回当前图形用户的 launchd domain 和服务标签。"""
        return f"gui/{self.user_id}/{MACOS_LABEL}"

    def _macos_domain(self) -> str:
        """返回当前图形用户的 launchd domain。"""
        return f"gui/{self.user_id}"

    def _macos_loaded(self) -> bool:
        """查询 launchd 服务是否已经加载。"""
        return self._run(["launchctl", "print", self._macos_target()], check=False).returncode == 0

    def _macos_plist_content(self) -> str:
        """生成继续复用内部 Unix runner 的 LaunchAgent 配置。"""
        runner = escape(str(self.project_root / "scripts" / "run_service.sh"))
        project_root = escape(str(self.project_root))
        log_root = escape(str(self.project_root / "logs"))
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{MACOS_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{runner}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>{project_root}</string>
  <key>StandardOutPath</key>
  <string>{log_root}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>{log_root}/launchd.err.log</string>
</dict>
</plist>
"""

    def _start_macos(self) -> ServiceOperationResult:
        """幂等注册并启动 macOS LaunchAgent。"""
        if self._macos_loaded():
            return ServiceOperationResult("后台服务已在运行。", changed=False)
        runner = self.project_root / "scripts" / "run_service.sh"
        self._ensure_runner(runner)
        (self.project_root / "logs").mkdir(parents=True, exist_ok=True)
        self._write_text_atomically(self.macos_plist_path, self._macos_plist_content())
        self._run(["launchctl", "bootstrap", self._macos_domain(), str(self.macos_plist_path)])
        return ServiceOperationResult("后台服务已启动。", changed=True)

    def _stop_macos(self) -> ServiceOperationResult:
        """停止 macOS LaunchAgent，同时保留 plist。"""
        if not self._macos_loaded():
            return ServiceOperationResult("后台服务已经停止。", changed=False)
        self._run(["launchctl", "bootout", self._macos_domain(), str(self.macos_plist_path)])
        return ServiceOperationResult("后台服务已停止，服务注册已保留。", changed=True)

    def _restart_macos(self) -> ServiceOperationResult:
        """重启已注册的 macOS LaunchAgent。"""
        if not self.macos_plist_path.exists():
            raise ServiceNotInstalledError("后台服务尚未注册，请先执行 codex-api-service start。")
        if self._macos_loaded():
            self._run(["launchctl", "kickstart", "-k", self._macos_target()])
        else:
            self._run(["launchctl", "bootstrap", self._macos_domain(), str(self.macos_plist_path)])
        return ServiceOperationResult("后台服务已重启。", changed=True)

    def _uninstall_macos(self) -> ServiceOperationResult:
        """停止并删除 macOS LaunchAgent 注册。"""
        registered = self.macos_plist_path.exists()
        loaded = self._macos_loaded()
        if loaded:
            self._run(["launchctl", "bootout", self._macos_domain(), str(self.macos_plist_path)])
        if registered:
            self.macos_plist_path.unlink()
        if not loaded and not registered:
            return ServiceOperationResult("后台服务尚未安装。", changed=False)
        return ServiceOperationResult("后台服务已卸载。", changed=True)

    def _linux_active(self) -> bool:
        """查询 systemd 用户服务是否正在运行。"""
        command = ["systemctl", "--user", "is-active", "--quiet", LINUX_SERVICE_NAME]
        return self._run(command, check=False).returncode == 0

    def _linux_unit_content(self) -> str:
        """生成 systemd 用户服务 unit。"""
        runner = self.project_root / "scripts" / "run_service.sh"
        return f"""[Unit]
Description=Codex API Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={self.project_root}
ExecStart={runner}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""

    def _start_linux(self) -> ServiceOperationResult:
        """幂等注册并启动 systemd 用户服务。"""
        if self._linux_active():
            return ServiceOperationResult("后台服务已在运行。", changed=False)
        runner = self.project_root / "scripts" / "run_service.sh"
        self._ensure_runner(runner)
        self._write_text_atomically(self.linux_unit_path, self._linux_unit_content())
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", LINUX_SERVICE_NAME])
        self._run(["systemctl", "--user", "start", LINUX_SERVICE_NAME])
        return ServiceOperationResult("后台服务已启动。", changed=True)

    def _stop_linux(self) -> ServiceOperationResult:
        """停止 systemd 用户服务并保留 unit。"""
        if not self.linux_unit_path.exists():
            return ServiceOperationResult("后台服务尚未安装。", changed=False)
        if not self._linux_active():
            return ServiceOperationResult("后台服务已经停止。", changed=False)
        self._run(["systemctl", "--user", "stop", LINUX_SERVICE_NAME])
        return ServiceOperationResult("后台服务已停止，服务注册已保留。", changed=True)

    def _restart_linux(self) -> ServiceOperationResult:
        """重启已注册的 systemd 用户服务。"""
        if not self.linux_unit_path.exists():
            raise ServiceNotInstalledError("后台服务尚未注册，请先执行 codex-api-service start。")
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "restart", LINUX_SERVICE_NAME])
        return ServiceOperationResult("后台服务已重启。", changed=True)

    def _uninstall_linux(self) -> ServiceOperationResult:
        """停止并删除 systemd 用户服务注册。"""
        if not self.linux_unit_path.exists():
            return ServiceOperationResult("后台服务尚未安装。", changed=False)
        self._run(["systemctl", "--user", "disable", "--now", LINUX_SERVICE_NAME])
        self.linux_unit_path.unlink()
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "reset-failed"], check=False)
        return ServiceOperationResult("后台服务已卸载。", changed=True)

    def _powershell_command(self, script: str) -> list[str]:
        """构造不经过 cmd.exe 的 PowerShell 命令。"""
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]

    def _powershell_quote(self, value: str) -> str:
        """把路径安全放入 PowerShell 单引号字符串。"""
        return "'" + value.replace("'", "''") + "'"

    def _windows_task_state(self) -> str:
        """返回 Windows 计划任务状态或 Missing。"""
        task_name = self._powershell_quote(WINDOWS_TASK_NAME)
        script = (
            f"$task = Get-ScheduledTask -TaskName {task_name} -ErrorAction SilentlyContinue; "
            "if ($null -eq $task) { 'Missing' } else { $task.State }"
        )
        result = self._run(self._powershell_command(script))
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else "Missing"

    def _register_windows_task(self) -> None:
        """创建当前用户登录时自动运行的 Windows 计划任务。"""
        runner = self.project_root / "scripts" / "run_service.ps1"
        self._ensure_runner(runner)
        task_name = self._powershell_quote(WINDOWS_TASK_NAME)
        project_root = self._powershell_quote(str(self.project_root))
        argument = self._powershell_quote(f'-NoProfile -ExecutionPolicy Bypass -File "{runner}"')
        script = (
            f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {argument} "
            f"-WorkingDirectory {project_root}; "
            "$trigger = New-ScheduledTaskTrigger -AtLogOn; "
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
            f"Register-ScheduledTask -TaskName {task_name} -Action $action -Trigger $trigger "
            "-Settings $settings -Description 'Codex API Service' -Force | Out-Null"
        )
        self._run(self._powershell_command(script))

    def _start_windows_task(self) -> None:
        """启动已注册的 Windows 计划任务。"""
        task_name = self._powershell_quote(WINDOWS_TASK_NAME)
        self._run(self._powershell_command(f"Start-ScheduledTask -TaskName {task_name}"))

    def _stop_windows_task(self) -> None:
        """停止已运行的 Windows 计划任务。"""
        task_name = self._powershell_quote(WINDOWS_TASK_NAME)
        self._run(self._powershell_command(f"Stop-ScheduledTask -TaskName {task_name}"))

    def _start_windows(self) -> ServiceOperationResult:
        """幂等注册并启动 Windows 计划任务。"""
        state = self._windows_task_state()
        if state == "Running":
            return ServiceOperationResult("后台服务已在运行。", changed=False)
        if state == "Missing":
            self._register_windows_task()
        self._start_windows_task()
        return ServiceOperationResult("后台服务已启动。", changed=True)

    def _stop_windows(self) -> ServiceOperationResult:
        """停止 Windows 计划任务并保留注册。"""
        state = self._windows_task_state()
        if state == "Missing":
            return ServiceOperationResult("后台服务尚未安装。", changed=False)
        if state != "Running":
            return ServiceOperationResult("后台服务已经停止。", changed=False)
        self._stop_windows_task()
        return ServiceOperationResult("后台服务已停止，服务注册已保留。", changed=True)

    def _restart_windows(self) -> ServiceOperationResult:
        """重启已注册的 Windows 计划任务。"""
        state = self._windows_task_state()
        if state == "Missing":
            raise ServiceNotInstalledError("后台服务尚未注册，请先执行 codex-api-service start。")
        if state == "Running":
            self._stop_windows_task()
        self._start_windows_task()
        return ServiceOperationResult("后台服务已重启。", changed=True)

    def _uninstall_windows(self) -> ServiceOperationResult:
        """停止并注销 Windows 计划任务。"""
        state = self._windows_task_state()
        if state == "Missing":
            return ServiceOperationResult("后台服务尚未安装。", changed=False)
        if state == "Running":
            self._stop_windows_task()
        task_name = self._powershell_quote(WINDOWS_TASK_NAME)
        script = f"Unregister-ScheduledTask -TaskName {task_name} -Confirm:$false"
        self._run(self._powershell_command(script))
        return ServiceOperationResult("后台服务已卸载。", changed=True)
