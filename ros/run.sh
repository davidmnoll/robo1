#!/usr/bin/env bash
set -euo pipefail

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
source "${ROS_SETUP_SCRIPT}"
set -u

ros2 run rosbridge_server rosbridge_websocket --port "${ROSBRIDGE_PORT}" &
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
