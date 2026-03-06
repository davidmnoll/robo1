#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[sim-entrypoint] $*" >&2
}

: "${ROS_DISTRO:=humble}"
if [[ ! -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
  log "ROS distribution ${ROS_DISTRO} is not installed in /opt/ros."
  exit 1
fi

set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u

SIM_WORKSPACE_ROOT="${SIM_WORKSPACE_ROOT:-/workspace}"
DEFAULT_WORLD="${SIM_WORKSPACE_ROOT}/worlds/battle_arena.wbt"
WORLD_PATH="${WEBOTS_WORLD:-${DEFAULT_WORLD}}"
if [[ ! -f "${WORLD_PATH}" ]]; then
  log "Webots world ${WORLD_PATH} is missing. Mount or copy a valid world file."
  exit 1
fi

LOG_DIR="${SIM_LOG_DIR:-/tmp/sim_logs}"
mkdir -p "${LOG_DIR}"
LOG_PATH="${LOG_DIR}/webots.log"
BOT_LOG_DIR="${BOT_LOG_DIR:-/tmp/bot_logs}"
mkdir -p "${BOT_LOG_DIR}"
export BOT_LOG_DIR

export WEBOTS_DISABLE_SAVE_WORLD=1
export WEBOTS_DISABLE_SAVE_SCREEN=1
export WEBOTS_PROJECT_PATH="${WEBOTS_PROJECT_PATH:-${SIM_WORKSPACE_ROOT}}"
export WEBOTS_DISABLE_GRAPHICS=1
export QT_QPA_PLATFORM=offscreen
export QT_QPA_PLATFORM_PLUGIN_PATH="${QT_QPA_PLATFORM_PLUGIN_PATH:-/snap/webots/current/usr/lib/webots/plugins/platforms}"
export LIBGL_ALWAYS_SOFTWARE=1

log "Sim logs will be written to ${LOG_PATH} and stdout"
log "Launching Webots world ${WORLD_PATH}"

xvfb-run --auto-servernum --server-args='-screen 0 1280x720x24' \
  webots --stdout --stderr --batch --mode=fast --no-rendering "${WORLD_PATH}" \
  2>&1 | tee -a "${LOG_PATH}"
