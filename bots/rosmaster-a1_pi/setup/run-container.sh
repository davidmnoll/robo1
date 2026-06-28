#!/bin/bash
# Start the ROS Humble Docker container for Rosmaster A1
#
# This script starts the container with proper device access.
# The container runs in background - use start-robot.sh to launch ROS nodes.

set -e

IMAGE="${ROS_IMAGE:-192.168.2.51:5000/ros-humble-a1:2.3.0}"
CONTAINER_NAME="${CONTAINER_NAME:-rosmaster}"

# Stop existing container if running
if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping existing container..."
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
fi
docker rm "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting container $CONTAINER_NAME..."

# Allow X11 forwarding
xhost + 2>/dev/null || true

docker run -d \
    --name "$CONTAINER_NAME" \
    --net=host \
    --privileged \
    --env="DISPLAY" \
    --env="QT_X11_NO_MITSHM=1" \
    --security-opt apparmor:unconfined \
    -e PULSE_SERVER=unix:/run/user/1000/pulse/native \
    -e ALSA_CARD=0 \
    -v /run/user/1000/pulse:/run/user/1000/pulse:ro \
    -v ~/.config/pulse:/root/.config/pulse:ro \
    -v /etc/localtime:/etc/localtime:ro \
    -v /etc/timezone:/etc/timezone:ro \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v ~/share:/root/share \
    -v /dev:/dev \
    -v /dev/input:/dev/input \
    "$IMAGE" tail -f /dev/null

echo "Container started. Use 'make start' or start-robot.sh to launch ROS nodes."
