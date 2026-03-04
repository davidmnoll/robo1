#!/usr/bin/env bash
set -euo pipefail

# Keep AMENT_TRACE_SETUP_FILES defined (and force it to 0) to avoid setup.bash blowing up under set -u
# and to suppress verbose trace output.
export AMENT_TRACE_SETUP_FILES=0
# Drop ROS logging down unless explicitly overridden for quieter boots.
export RCUTILS_LOGGING_MIN_SEVERITY=${RCUTILS_LOGGING_MIN_SEVERITY:-WARN}
set +u
source /opt/ros/${ROS_DISTRO}/setup.bash
set -u

ros2 run rosbridge_server rosbridge_websocket --ros-args --log-level warn &
ROSBRIDGE_PID=$!

cleanup() {
  if ps -p "${ROSBRIDGE_PID}" > /dev/null 2>&1; then
    kill "${ROSBRIDGE_PID}"
  fi
}
trap cleanup EXIT

exec uvicorn proxy.app:app --host 0.0.0.0 --port 8080
