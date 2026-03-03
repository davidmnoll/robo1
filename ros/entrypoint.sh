#!/usr/bin/env bash
set -euo pipefail

source /opt/ros/${ROS_DISTRO}/setup.bash

ros2 run rosbridge_server rosbridge_websocket &
ROSBRIDGE_PID=$!

cleanup() {
  if ps -p "${ROSBRIDGE_PID}" > /dev/null 2>&1; then
    kill "${ROSBRIDGE_PID}"
  fi
}
trap cleanup EXIT

exec uvicorn proxy.app:app --host 0.0.0.0 --port 8080
