#!/usr/bin/env bash
# 这个脚本作为 Unix 后台服务入口，负责进入项目目录并启动 API 服务。

set -euo pipefail

# 计算项目根目录，确保从任意工作目录启动都能读到项目内的 config.yaml。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
VENV_SITE_PACKAGES=""

# 切到项目根目录，服务会默认读取这里的 config.yaml。
cd "${PROJECT_ROOT}"

# 优先使用项目虚拟环境；如果不存在，打印 warning 后回退到全局 Python。
if [[ -x "${VENV_PYTHON}" ]]; then
  PYTHON_BIN="${VENV_PYTHON}"
  # 记录虚拟环境 site-packages，macOS Framework Python 直启时需要显式加入依赖路径。
  PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  VENV_SITE_PACKAGES="${PROJECT_ROOT}/.venv/lib/python${PYTHON_VERSION}/site-packages"

  # Python.org Framework 版本会经由 stub 拉起 Python.app，launchd 会误判原进程退出并反复重启。
  FRAMEWORK_PYTHON="$("${PYTHON_BIN}" - <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
print(candidate if candidate.exists() else "")
PY
)"
  if [[ -x "${FRAMEWORK_PYTHON}" ]]; then
    # 直接执行 Python.app 里的真实二进制，让 launchd 跟踪常驻服务进程。
    PYTHON_BIN="${FRAMEWORK_PYTHON}"
    export PYTHONPATH="${PROJECT_ROOT}:${VENV_SITE_PACKAGES}${PYTHONPATH:+:${PYTHONPATH}}"
  fi
else
  echo "WARNING: ${VENV_PYTHON} not found; falling back to global Python." >&2
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "ERROR: no Python interpreter found. Create .venv or install python3/python." >&2
    exit 1
  fi
fi

# 使用选中的 Python 启动服务，当前进程交给 uvicorn 生命周期管理。
export PYTHONUNBUFFERED=1
exec "${PYTHON_BIN}" -m codex_api_service.app
