#!/usr/bin/env bash
# 安装并启动 Ubuntu/Linux systemd 用户服务，不需要 sudo 权限。

set -euo pipefail

# systemd 用户服务名，卸载和日志查看都使用同一个名字。
SERVICE_NAME="codex-api-service.service"

# 计算项目根目录和 systemd user unit 路径。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_PATH="${UNIT_DIR}/${SERVICE_NAME}"

# 确保启动脚本可执行，并准备 systemd 用户配置目录。
chmod +x "${PROJECT_ROOT}/scripts/run_service.sh"
mkdir -p "${UNIT_DIR}"

# 写入 systemd unit；日志默认进入 journalctl --user。
cat > "${UNIT_PATH}" <<UNIT
[Unit]
# 本地 Codex OpenAI-compatible API 服务。
Description=Codex API Service
After=network-online.target
Wants=network-online.target

[Service]
# 简单前台进程模式，run_service.sh 负责选择 Python 并启动 uvicorn。
Type=simple
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PROJECT_ROOT}/scripts/run_service.sh
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
# 用户会话启动时自动运行。
WantedBy=default.target
UNIT

# 重新加载用户服务，设置开机登录后自动启动，并立即启动。
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"
systemctl --user restart "${SERVICE_NAME}"

# 输出后续管理命令，方便用户复制。
echo "Installed and started ${SERVICE_NAME}"
echo "Console: http://127.0.0.1:1219/ui"
echo "API base: http://127.0.0.1:1219/v1"
echo "Health check: curl http://127.0.0.1:1219/health"
echo "Status: systemctl --user status ${SERVICE_NAME}"
echo "Logs: journalctl --user -u codex-api-service -f"
echo "Tip: enable lingering with 'loginctl enable-linger ${USER}' if you need it to run without an active login session."
