#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_PATH="${WEBOTS_WORLD:-${ROOT_DIR}/sim/worlds/battle_arena.wbt}"
export DISPLAY="${DISPLAY:-:0}"

if ! command -v webots >/dev/null 2>&1; then
  echo "Webots is not installed or not in PATH. Install it or set WEBOTS_HOME." >&2
  exit 1
fi

if [[ ! -f "${WORLD_PATH}" ]]; then
  echo "World file ${WORLD_PATH} not found." >&2
  exit 1
fi

export WEBOTS_DISABLE_SAVE_WORLD=1
export WEBOTS_DISABLE_SAVE_SCREEN=1
export WEBOTS_PROJECT_PATH="${ROOT_DIR}/sim"
export WEBOTS_DISABLE_GRAPHICS=1
export QT_QPA_PLATFORM=offscreen
export QT_QPA_PLATFORM_PLUGIN_PATH="${QT_QPA_PLATFORM_PLUGIN_PATH:-/snap/webots/current/usr/lib/webots/plugins/platforms}"
export LIBGL_ALWAYS_SOFTWARE=1

XVFB_PID=""
if command -v Xvfb >/dev/null 2>&1; then
  export DISPLAY=:99
  Xvfb :99 -screen 0 1280x720x24 >/tmp/xvfb-webots.log 2>&1 &
  XVFB_PID=$!
  cleanup() {
    if [[ -n "${XVFB_PID}" ]] && ps -p "${XVFB_PID}" >/dev/null 2>&1; then
      kill "${XVFB_PID}"
    fi
  }
  trap cleanup EXIT
fi

cd "${ROOT_DIR}/sim"

RUNNER=(webots --stdout --stderr --batch --mode=fast --no-rendering "${WORLD_PATH}")

echo "[sim] Starting Webots world ${WORLD_PATH}"
exec "${RUNNER[@]}"
