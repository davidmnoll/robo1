#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[sim-entrypoint] $*" >&2
}

# Check for GUI mode
GUI_MODE=false
for arg in "$@"; do
  if [[ "$arg" == "--gui" ]]; then
    GUI_MODE=true
  fi
done

: "${ROS_DISTRO:=humble}"
if [[ ! -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
  log "ROS distribution ${ROS_DISTRO} is not installed in /opt/ros."
  exit 1
fi

set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u

# Use FastRTPS for Discovery Server support
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
log "RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION}"

# Connect to Discovery Server if specified
if [[ -n "${ROS_DISCOVERY_SERVER:-}" ]]; then
  log "Using Discovery Server: ${ROS_DISCOVERY_SERVER}"
  export ROS_DISCOVERY_SERVER
fi

# Make sure Python can find ROS2 packages
export PYTHONPATH="/opt/ros/${ROS_DISTRO}/lib/python3.10/site-packages:${PYTHONPATH:-}"

SIM_WORKSPACE_ROOT="${SIM_WORKSPACE_ROOT:-/workspace}"
DEFAULT_WORLD="${SIM_WORKSPACE_ROOT}/worlds/turtlebot_apartment.wbt"
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

log "Sim logs will be written to ${LOG_PATH} and stdout"
log "Launching Webots world ${WORLD_PATH}"

if [[ "$GUI_MODE" == "true" ]]; then
  log "Running in GUI mode"
  # GUI mode - use host display
  webots --stdout --stderr --mode=realtime "${WORLD_PATH}" \
    2>&1 | tee -a "${LOG_PATH}"
else
  log "Running in headless mode"
  # Headless mode - use virtual framebuffer
  # Note: WEBOTS_DISABLE_GRAPHICS would break camera rendering, so we don't set it
  export QT_QPA_PLATFORM=offscreen
  export LIBGL_ALWAYS_SOFTWARE=1

  xvfb-run --auto-servernum --server-args='-screen 0 1280x720x24' \
    webots --stdout --stderr --batch --mode=fast "${WORLD_PATH}" \
    2>&1 | tee -a "${LOG_PATH}"
fi
