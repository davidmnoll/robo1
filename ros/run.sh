#!/usr/bin/env bash
set -eo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${ROS_SETUP_SCRIPT:-}" ]]; then
  SELECTED_SETUP="${ROS_SETUP_SCRIPT}"
else
  if [[ -n "${ROS_DISTRO:-}" && -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
    SELECTED_SETUP="/opt/ros/${ROS_DISTRO}/setup.bash"
  else
    for candidate in humble jazzy rolling galactic foxy; do
      if [[ -f "/opt/ros/${candidate}/setup.bash" ]]; then
        SELECTED_SETUP="/opt/ros/${candidate}/setup.bash"
        break
      fi
    done
  fi
fi

ROS_SETUP_SCRIPT="${SELECTED_SETUP:-}"
ROSBRIDGE_PORT="${ROS_BRIDGE_PORT:-9090}"
ROS_VENV="${ROS_VENV:-${ROOT_DIR}/ros/.venv}"

if [[ -z "${ROS_SETUP_SCRIPT}" || ! -f "${ROS_SETUP_SCRIPT}" ]]; then
  echo "Could not locate ROS 2 setup.bash. Set ROS_SETUP_SCRIPT or ROS_DISTRO." >&2
  exit 1
fi

set +u
unset AMENT_TRACE_SETUP_FILES
# Default ROS 2 logging to WARN for less noisy console output (can be overridden upstream).
export RCUTILS_LOGGING_MIN_SEVERITY=${RCUTILS_LOGGING_MIN_SEVERITY:-WARN}
export AMENT_PYTHON_EXECUTABLE=${AMENT_PYTHON_EXECUTABLE:-python3}
source "${ROS_SETUP_SCRIPT}" >/dev/null 2>&1
set -u

ros2 run rosbridge_server rosbridge_websocket \
  --port "${ROSBRIDGE_PORT}" \
  --ros-args --log-level warn \
  -p default_call_service_timeout:=5.0 \
  -p call_services_in_new_thread:=true \
  -p send_action_goals_in_new_thread:=true &
ROS_PID=$!

cleanup() {
  if ps -p "${ROS_PID}" >/dev/null 2>&1; then
    kill "${ROS_PID}"
  fi
}
trap cleanup EXIT

cd "${ROOT_DIR}/ros"

source "${ROS_VENV}/bin/activate" 2>/dev/null || true

PYTHON_BIN="${ROS_VENV}/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "python executable not found. Install python3 or create ${ROS_VENV}." >&2
    exit 1
  fi
fi

echo "[ros-core] rosbridge running on port ${ROSBRIDGE_PORT}"
echo "[ros-core] launching robot bridge (auto-discovers camera topics)"
exec "${PYTHON_BIN}" "${ROOT_DIR}/ros/camera_forwarder.py"
