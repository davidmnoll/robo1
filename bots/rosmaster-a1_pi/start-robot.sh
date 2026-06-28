#!/bin/bash
set -e

# Load environment
source /home/pi/share/rosmaster/.env 2>/dev/null || true

ROBOT_ID="${ROBOT_ID:-rosmaster1}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-61}"
CONTAINER_DIR="/root/share/rosmaster"

# Use named container
CONTAINER_NAME="${CONTAINER_NAME:-rosmaster}"

if ! docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "ERROR: Container '$CONTAINER_NAME' not running"
    exit 1
fi

echo "Starting robot services in container $CONTAINER_NAME..."
echo "ROBOT_ID=$ROBOT_ID ROS_DOMAIN_ID=$ROS_DOMAIN_ID"

# Clean up any stale processes before starting
echo "Cleaning up any existing robot processes..."
docker exec "$CONTAINER_NAME" bash -c "
    pkill -9 -f rosmaster_robot || true
    pkill -9 -f usb_cam || true
    pkill -9 -f ydlidar || true
    pkill -9 -f audio_play || true
    pkill -9 -f audio_stream || true
    pkill -9 -f static_transform_publisher || true
    pkill -9 -f slam_toolbox || true
" 2>/dev/null || true
sleep 1

# Run the robot launch in the container
exec docker exec -e ROS_DOMAIN_ID="$ROS_DOMAIN_ID" -e ROBOT_ID="$ROBOT_ID" "$CONTAINER_NAME" bash -c "
    source /opt/ros/humble/setup.bash
    source /root/share/install/setup.bash 2>/dev/null || true
    source /root/ydlidar_humble/install/setup.bash 2>/dev/null || true
    cd $CONTAINER_DIR
    ros2 launch rosmaster_robot robot.launch.py robot_id:=$ROBOT_ID
"
