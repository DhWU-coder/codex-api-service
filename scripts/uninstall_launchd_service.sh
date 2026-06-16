#!/usr/bin/env bash
# 停止并卸载 macOS launchd 用户服务。

set -euo pipefail

# 固定服务名必须和安装脚本一致。
LABEL="com.codex-api-service.local"

# launchd plist 存放在当前用户的 LaunchAgents 目录。
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

# 如果服务已加载，先从当前用户 launchd 中卸载。
launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true

# 删除 plist，避免下次登录自动启动。
rm -f "${PLIST_PATH}"

# 输出确认信息。
echo "Uninstalled ${LABEL}"
