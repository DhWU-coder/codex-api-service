#!/usr/bin/env bash
# 这个脚本作为 launchd 的 ProgramArguments 入口，负责进入项目目录并启动 API 服务。

set -euo pipefail

# 计算项目根目录，确保从任意工作目录启动都能读到项目内的 config.yaml。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 切到项目根目录，服务会默认读取这里的 config.yaml。
cd "${PROJECT_ROOT}"

# 使用项目自带虚拟环境启动服务，避免依赖系统 Python 环境。
exec "${PROJECT_ROOT}/.venv/bin/python" -m codex_api_service.app
