#!/bin/bash
set -e

# Load environment
source /home/pi/share/rosmaster/.env 2>/dev/null || true

ROBOT_ID="${ROBOT_ID:-rosmaster1}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-61}"
CONTAINER_DIR="/root/share/rosmaster"

# Get running container ID
CONTAINER_ID=$(docker ps -q)

if [ -z "$CONTAINER_ID" ]; then
    echo "ERROR: No Docker container running"
    exit 1
fi

echo "Starting robot services in container $CONTAINER_ID..."
echo "ROBOT_ID=$ROBOT_ID ROS_DOMAIN_ID=$ROS_DOMAIN_ID"

# Run the robot launch in the container
exec docker exec -e ROS_DOMAIN_ID="$ROS_DOMAIN_ID" -e ROBOT_ID="$ROBOT_ID" "$CONTAINER_ID" bash -c "
    source /opt/ros/humble/setup.bash
    source /root/share/install/setup.bash 2>/dev/null || true
    cd $CONTAINER_DIR
    ros2 launch rosmaster_robot robot.launch.py robot_id:=$ROBOT_ID
"
