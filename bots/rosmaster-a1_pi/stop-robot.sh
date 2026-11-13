#!/bin/bash

CONTAINER_ID=$(docker ps -q)

if [ -z "$CONTAINER_ID" ]; then
    echo "No Docker container running"
    exit 0
fi

echo "Stopping robot services..."
docker exec "$CONTAINER_ID" bash -c "
    # Kill all robot-related ROS nodes
    pkill -f rosmaster_robot || true
    pkill -f usb_cam || true
    pkill -f ydlidar || true
    pkill -f audio_play || true
    pkill -f audio_stream || true
    pkill -f static_transform_publisher || true
    pkill -f slam_toolbox || true
    # Give processes time to exit
    sleep 1
    # Force kill any remaining
    pkill -9 -f rosmaster_robot || true
    pkill -9 -f ydlidar || true
"
echo "Stopped"
