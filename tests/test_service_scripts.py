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


def test_unix_runner_exposes_common_codex_cli_paths() -> None:
    """验证短 PATH 的后台服务也能找到 Homebrew 安装的 Codex CLI 和 Node。"""
    script = _read_project_file("scripts/run_service.sh")

    assert "/opt/homebrew/bin" in script
    assert "/usr/local/bin" in script
    assert "export PATH" in script


def test_windows_runner_uses_python_fallback() -> None:
    """验证 Windows 内部 runner 按虚拟环境优先并支持全局 Python 兜底。"""
    runner = _read_project_file("scripts/run_service.ps1")

    assert ".venv\\Scripts\\python.exe" in runner
    assert "Get-Command python" in runner
    assert "WARNING" in runner
    assert "Start-Process" in runner


def test_legacy_service_entry_scripts_are_removed() -> None:
    """验证公开的旧安装和卸载脚本已经删除，内部 runner 继续保留。"""
    legacy_paths = [
        "scripts/install_launchd_service.sh",
        "scripts/uninstall_launchd_service.sh",
        "scripts/install_systemd_user_service.sh",
        "scripts/uninstall_systemd_user_service.sh",
        "scripts/install_windows_task.ps1",
        "scripts/uninstall_windows_task.ps1",
    ]

    for relative_path in legacy_paths:
        assert not (PROJECT_ROOT / relative_path).exists()
    assert (PROJECT_ROOT / "scripts/run_service.sh").exists()
    assert (PROJECT_ROOT / "scripts/run_service.ps1").exists()


def test_readmes_only_document_unified_service_commands() -> None:
    """验证中英文 README 只公开统一后台服务命令。"""
    chinese_readme = _read_project_file("README.md")
    english_readme = _read_project_file("README_en.md")

    for readme in (chinese_readme, english_readme):
        assert "codex-api-service start" in readme
        assert "codex-api-service run" in readme
        assert "codex-api-service stop" in readme
        assert "codex-api-service end" in readme
        assert "codex-api-service restart" in readme
        assert "codex-api-service uninstall" in readme
        assert "install_launchd_service.sh" not in readme
        assert "install_systemd_user_service.sh" not in readme
        assert "install_windows_task.ps1" not in readme
        assert "launchctl kickstart" not in readme
        assert "systemctl --user restart" not in readme


def test_setuptools_only_discovers_service_package() -> None:
    """验证 editable 安装不会把日志和前端目录误识别为 Python 包。"""
    pyproject = _read_project_file("pyproject.toml")

    assert "[tool.setuptools.packages.find]" in pyproject
    assert 'include = ["codex_api_service*"]' in pyproject
