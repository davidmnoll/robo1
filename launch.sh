#!/bin/bash
# Launch TurtleBot3 simulation with web control

source /opt/ros/jazzy/setup.bash
export TURTLEBOT3_MODEL=burger

echo "Starting TurtleBot3 simulation..."
echo ""
echo "Controls:"
echo "  - Web interface: http://localhost:8000"
echo "  - Press Ctrl+C to stop everything"
echo ""

# Start rosbridge (WebSocket server for browser)
ros2 launch rosbridge_server rosbridge_websocket_launch.xml &
ROSBRIDGE_PID=$!

# Start web server for the control page
cd "$(dirname "$0")/web"
python3 -m http.server 8000 &
WEBSERVER_PID=$!

# Start the simulation
ros2 launch webots_ros2_turtlebot robot_launch.py &
SIM_PID=$!

# Wait and cleanup on Ctrl+C
cleanup() {
    echo ""
    echo "Shutting down..."
    kill $ROSBRIDGE_PID $WEBSERVER_PID $SIM_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT

wait
