#!/usr/bin/env bash
# 安装并启动 macOS launchd 用户服务，让 codex-api-service 登录后自动运行。

set -euo pipefail

# 固定服务名，后续查看、重启、卸载都用同一个 label。
LABEL="com.codex-api-service.local"

# 计算项目根目录和 launchd plist 目标路径。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${PROJECT_ROOT}/logs"

# 确保日志目录和 LaunchAgents 目录存在。
mkdir -p "${LOG_DIR}"
mkdir -p "${HOME}/Library/LaunchAgents"

# 如果当前用户服务已经加载，先卸载旧定义，避免 bootstrap 报 already bootstrapped。
launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true

# 写入 launchd 配置。KeepAlive 会在服务异常退出时自动拉起。
cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!-- codex-api-service 的 macOS launchd 用户服务配置。 -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <!-- 服务唯一名称。 -->
  <key>Label</key>
  <string>${LABEL}</string>

  <!-- 通过项目脚本启动，脚本内会进入项目根目录并使用 .venv Python。 -->
  <key>ProgramArguments</key>
  <array>
    <string>${PROJECT_ROOT}/scripts/run_service.sh</string>
  </array>

  <!-- 用户登录后自动启动。 -->
  <key>RunAtLoad</key>
  <true/>

  <!-- 服务异常退出时自动重启。 -->
  <key>KeepAlive</key>
  <true/>

  <!-- 工作目录设为项目根目录，确保相对路径配置稳定。 -->
  <key>WorkingDirectory</key>
  <string>${PROJECT_ROOT}</string>

  <!-- 标准输出和错误日志写入项目 logs 目录，便于排查启动问题。 -->
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
</dict>
</plist>
PLIST

# 给启动脚本添加执行权限，launchd 才能直接运行它。
chmod +x "${PROJECT_ROOT}/scripts/run_service.sh"

# 加载服务；RunAtLoad=true 会立即启动，避免 bootstrap 后再 kickstart 造成重复进程。
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"

# 输出后续管理命令，方便用户复制。
echo "Installed and started ${LABEL}"
echo "Console: http://127.0.0.1:1219/ui"
echo "API base: http://127.0.0.1:1219/v1"
echo "Health check: curl http://127.0.0.1:1219/admin/health"
echo "Logs: ${LOG_DIR}/launchd.out.log and ${LOG_DIR}/launchd.err.log"
