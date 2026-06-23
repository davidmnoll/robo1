#!/bin/bash
set -e

source /opt/ros/humble/setup.bash

# Start rosbridge in background
ros2 run rosbridge_server rosbridge_websocket --port 9090 &
sleep 2

echo "[ros-bridge] Running rosbridge on port 9090 (Humble container)"

# Run camera forwarder
exec python3 /ros-bridge/camera_forwarder.py
