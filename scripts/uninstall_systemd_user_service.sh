#!/usr/bin/env bash
# 停止并卸载 Ubuntu/Linux systemd 用户服务。

set -euo pipefail

# systemd 用户服务名必须和安装脚本一致。
SERVICE_NAME="codex-api-service.service"

# systemd user unit 存放在当前用户配置目录。
UNIT_PATH="${HOME}/.config/systemd/user/${SERVICE_NAME}"

# 停止并禁用服务；服务不存在时继续执行清理。
systemctl --user stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl --user disable "${SERVICE_NAME}" >/dev/null 2>&1 || true

# 删除 unit 文件并刷新 systemd 用户配置。
rm -f "${UNIT_PATH}"
systemctl --user daemon-reload
systemctl --user reset-failed >/dev/null 2>&1 || true

# 输出确认信息。
echo "Uninstalled ${SERVICE_NAME}"
