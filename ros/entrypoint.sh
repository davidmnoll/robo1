#!/usr/bin/env bash
set -euo pipefail

# Drop ROS logging down unless explicitly overridden for quieter boots.
export RCUTILS_LOGGING_MIN_SEVERITY=${RCUTILS_LOGGING_MIN_SEVERITY:-WARN}
set +u
unset AMENT_TRACE_SETUP_FILES
source /opt/ros/${ROS_DISTRO}/setup.bash >/dev/null 2>&1
set -u

ros2 run rosbridge_server rosbridge_websocket \
  --ros-args --log-level warn \
  -p default_call_service_timeout:=5.0 \
  -p call_services_in_new_thread:=true \
  -p send_action_goals_in_new_thread:=true &
ROSBRIDGE_PID=$!

python3 /workspace/camera_forwarder.py &
FORWARDER_PID=$!

cleanup() {
  for pid in "${ROSBRIDGE_PID}" "${FORWARDER_PID}"; do
    if ps -p "${pid}" > /dev/null 2>&1; then
      kill "${pid}"
    fi
  done
}
trap cleanup EXIT

wait "${FORWARDER_PID}"
