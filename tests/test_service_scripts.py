"""验证跨平台后台服务脚本和文档入口。"""

from pathlib import Path


# 项目根目录用于读取脚本和 README 文档。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_project_file(relative_path: str) -> str:
    """读取项目内文本文件，方便断言脚本内容。"""
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_unix_runner_prefers_venv_and_falls_back_to_global_python() -> None:
    """验证 Unix 启动脚本会优先使用虚拟环境，缺失时警告并回退到全局 Python。"""
    # run_service.sh 同时被 macOS launchd 和 Ubuntu systemd 复用。
    script = _read_project_file("scripts/run_service.sh")

    assert ".venv/bin/python" in script
    assert "command -v python3" in script
    assert "command -v python" in script
    assert "PYTHON_BIN" in script
    assert "WARNING" in script


def test_ubuntu_systemd_user_scripts_exist_and_use_user_scope() -> None:
    """验证 Ubuntu systemd 用户服务脚本存在，并使用无 sudo 的用户服务。"""
    # 安装脚本应创建 systemd user service，卸载脚本应移除同一个服务。
    install_script = _read_project_file("scripts/install_systemd_user_service.sh")
    uninstall_script = _read_project_file("scripts/uninstall_systemd_user_service.sh")

    assert "systemctl --user daemon-reload" in install_script
    assert "systemctl --user enable" in install_script
    assert "codex-api-service.service" in install_script
    assert "journalctl --user -u codex-api-service -f" in install_script
    assert "systemctl --user disable" in uninstall_script
    assert "codex-api-service.service" in uninstall_script


def test_windows_task_scripts_exist_and_use_python_fallback() -> None:
    """验证 Windows 脚本存在，并按虚拟环境优先、警告后全局 Python 兜底启动。"""
    # PowerShell runner 负责选择 Python，计划任务脚本负责登录后自动启动。
    runner = _read_project_file("scripts/run_service.ps1")
    install_script = _read_project_file("scripts/install_windows_task.ps1")
    uninstall_script = _read_project_file("scripts/uninstall_windows_task.ps1")

    assert ".venv\\Scripts\\python.exe" in runner
    assert "Get-Command python" in runner
    assert "WARNING" in runner
    assert "Start-Process" in runner
    assert "CodexApiService" in install_script
    assert "Register-ScheduledTask" in install_script
    assert "Unregister-ScheduledTask" in uninstall_script


def test_readmes_document_macos_ubuntu_and_windows_service_modes() -> None:
    """验证中英文 README 都说明 macOS、Ubuntu 和 Windows 后台运行方式。"""
    # 公开仓库首页需要让不同系统用户都能找到对应安装入口。
    chinese_readme = _read_project_file("README.md")
    english_readme = _read_project_file("README_en.md")

    assert "macOS" in chinese_readme
    assert "Ubuntu" in chinese_readme
    assert "Windows" in chinese_readme
    assert "install_systemd_user_service.sh" in chinese_readme
    assert "install_windows_task.ps1" in chinese_readme
    assert "macOS" in english_readme
    assert "Ubuntu" in english_readme
    assert "Windows" in english_readme
    assert "install_systemd_user_service.sh" in english_readme
    assert "install_windows_task.ps1" in english_readme
