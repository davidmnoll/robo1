SHELL := /bin/bash
ROS_SETUP := source /opt/ros/jazzy/setup.bash
export TURTLEBOT3_MODEL := burger

.PHONY: help sim bridge web controller all stop clean tmux-stack attach all

attach:
	tmux attach -t arena

help:
	@echo "Arena stack commands"
	@echo ""
	@echo "  make tmux-stack - Launch ros-core, sim, api, web in tmux (preferred)"
	@echo "  make all        - Alias for make tmux-stack"
	@echo "  make attach     - Attach to tmux session (arena)"
	@echo "  make db-shell   - Open psql shell inside the db container"
	@echo ""
	@echo "Legacy TurtleBot3 helpers (not wired into the arena stack):"
	@echo "  make sim / bridge / web / controller"
	@echo "  make stop       - Stop legacy background services"
	@echo "  make topics/nodes/echo-* etc."

# Core services
sim:
	$(ROS_SETUP) && ros2 launch webots_ros2_turtlebot robot_launch.py

bridge:
	$(ROS_SETUP) && ros2 launch rosbridge_server rosbridge_websocket_launch.xml

web:
	@echo "Starting Vite dev server at http://localhost:4173"
	cd web && ./run.sh

controller:
	$(ROS_SETUP) && python3 src/simple_controller.py

# Start arena stack in tmux
all: tmux-stack
	$(MAKE) attach

stop:
	@echo "Stopping tmux session 'arena'..."
	@tmux kill-session -t arena >/dev/null 2>&1 || echo "No tmux session named 'arena'"
	@echo "Killing leftover processes (rosbridge/webots/uvicorn/http.server)..."
	@pkill -f rosbridge_websocket >/dev/null 2>&1 || true
	@pkill -f webots >/dev/null 2>&1 || true
	@pkill -f "uvicorn app.main:app" >/dev/null 2>&1 || true
	@pkill -f "python3 -m http.server" >/dev/null 2>&1 || true
	@echo "Done"

# Debugging tools
topics:
	$(ROS_SETUP) && ros2 topic list

nodes:
	$(ROS_SETUP) && ros2 node list

echo-scan:
	$(ROS_SETUP) && ros2 topic echo /scan

echo-odom:
	$(ROS_SETUP) && ros2 topic echo /odom

echo-cmd:
	$(ROS_SETUP) && ros2 topic echo /cmd_vel

teleop:
	$(ROS_SETUP) && ros2 run teleop_twist_keyboard teleop_twist_keyboard

# Launch new tmux-based dev stack (ros-core, sim, api, web)
tmux-stack:
	./scripts/run_stack_tmux.sh

db-shell:
	docker compose exec db psql -U robot -d robotarena

# Cleanup
clean:
	rm -rf logs .pid_*
