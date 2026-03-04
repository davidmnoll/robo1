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
ROS_PROXY_PORT="${ROS_PROXY_PORT:-8080}"
ROSBRIDGE_PORT="${ROS_BRIDGE_PORT:-9090}"
ROS_VENV="${ROS_VENV:-${ROOT_DIR}/ros/.venv}"

if [[ -z "${ROS_SETUP_SCRIPT}" || ! -f "${ROS_SETUP_SCRIPT}" ]]; then
  echo "Could not locate ROS 2 setup.bash. Set ROS_SETUP_SCRIPT or ROS_DISTRO." >&2
  exit 1
fi

set +u
# Leave AMENT_TRACE_SETUP_FILES defined and force it to 0 to keep setup quiet even if the host sets it.
export AMENT_TRACE_SETUP_FILES=0
# Default ROS 2 logging to WARN for less noisy console output (can be overridden upstream).
export RCUTILS_LOGGING_MIN_SEVERITY=${RCUTILS_LOGGING_MIN_SEVERITY:-WARN}
export AMENT_PYTHON_EXECUTABLE=${AMENT_PYTHON_EXECUTABLE:-python3}
source "${ROS_SETUP_SCRIPT}"
set -u

ros2 run rosbridge_server rosbridge_websocket --port "${ROSBRIDGE_PORT}" --ros-args --log-level warn &
ROS_PID=$!

cleanup() {
  if ps -p "${ROS_PID}" >/dev/null 2>&1; then
    kill "${ROS_PID}"
  fi
}
trap cleanup EXIT

cd "${ROOT_DIR}/ros"

source "${ROS_VENV}/bin/activate" 2>/dev/null || true

echo "[ros-core] rosbridge running on port ${ROSBRIDGE_PORT}"
exec uvicorn proxy.app:app --host 0.0.0.0 --port "${ROS_PROXY_PORT}"
