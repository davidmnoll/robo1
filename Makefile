SHELL := /bin/bash
-include .env
ROS_DISTRO ?= jazzy
ROS_SETUP := source /opt/ros/$(ROS_DISTRO)/setup.bash
PROJECT_ID ?= robo1-489405
export TURTLEBOT3_MODEL := burger

.PHONY: help sim bridge web controller all stop clean tmux-stack attach all sim-gui-bare

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
	@echo "Simulation commands:"
	@echo "  make cloud-sim  - Headless container-based simulation"
	@echo "  make sim-gui    - Container-based simulation with GUI (X11)"
	@echo "  make sim-gui-stop - Stop container-based simulation"
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

cloud-sql-shell:
	@bash -c 'set -euo pipefail; \
	INSTANCE="${CLOUD_SQL_INSTANCE:-robo1-489405:us-central1:robotarena}"; \
	SQL_USER="${CLOUD_SQL_USER:-arena_app}"; \
	DB_NAME="${CLOUD_SQL_DB:-robotarena}"; \
	echo "Connecting to $$INSTANCE as $$SQL_USER (database $$DB_NAME)"; \
	exec gcloud sql connect "$$INSTANCE" --user="$$SQL_USER" --database="$$DB_NAME"'

gcloud-resources:
	@echo "Project: ${PROJECT_ID}"; \
	set -euo pipefail; \
	gcloud config set project "${PROJECT_ID}" >/dev/null; \
	echo ""; \
	echo "[App Engine services]"; \
	gcloud app services list || true; \
	echo ""; \
	echo "[App Engine versions]"; \
	gcloud app versions list || true; \
	echo ""; \
	echo "[Cloud SQL instances]"; \
	gcloud sql instances list; \
	echo ""; \
	echo "[Artifact Registry images]"; \
	gcloud artifacts repositories list || true

gcloud-app-logs:
	@set -euo pipefail; \
	if [ -z "${PROJECT_ID}" ]; then \
	  echo "PROJECT_ID environment variable is required"; \
	  exit 1; \
	fi; \
	gcloud config set project "${PROJECT_ID}" >/dev/null; \
	if [ -n "${LIMIT:-}" ]; then \
	  LIMIT_VALUE="${LIMIT}"; \
	else \
	  LIMIT_VALUE=200; \
	fi; \
	FILTER="resource.type=\"gae_app\" AND resource.labels.version_id=\"flex-v2\""; \
	if [ -n "${APP_SERVICE}" ]; then \
	  FILTER="${FILTER} AND resource.labels.module_id=\"${APP_SERVICE}\""; \
	fi; \
	gcloud logging read "$${FILTER}" --limit="$${LIMIT_VALUE}" --format='value(timestamp,textPayload)'

cloud-sim:
	echo "Starting ROS bridge + Webots sim against $$CLOUD_RUN_API_URL"; \
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml up --build ros-core sim

sim-gui:
	@echo "Starting container-based Webots simulation with GUI..."
	@echo "Allowing Docker X11 access..."
	@xhost +local:docker 2>/dev/null || true
	@xhost +SI:localuser:root 2>/dev/null || true
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml -f docker-compose.gui.yml up --build ros-core sim

sim-gui-native:
	@echo "Starting ros-core container (VPN + Discovery Server + camera_forwarder)..."
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml up -d --build ros-core
	@echo "Waiting for Discovery Server to be ready..."
	@sleep 3
	@echo "Launching Webots with native ROS2..."
	@echo "Make sure ROS2 is sourced: source /opt/ros/humble/setup.bash"
	ROS_DISCOVERY_SERVER=localhost:11811 \
	webots sim/worlds/turtlebot_apartment.wbt
	@echo "Webots closed. Stopping ros-core..."
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml down ros-core

# Native ROS2 simulation (runs everything locally)
sim-ros2:
	@echo "=== Native ROS2 Simulation ==="
	@echo "This runs in 3 terminals. Starting tmux session..."
	@tmux new-session -d -s ros2sim -n ros-core "docker compose up ros-core; read" || true
	@sleep 3
	@tmux new-window -t ros2sim -n webots "source /opt/ros/$(ROS_DISTRO)/setup.bash && \
		export RMW_IMPLEMENTATION=rmw_fastrtps_cpp && \
		export ROS_DISCOVERY_SERVER=127.0.0.1:11811 && \
		webots sim/worlds/turtlebot_apartment.wbt; read"
	@sleep 2
	@tmux new-window -t ros2sim -n drivers "cd $(CURDIR) && source /opt/ros/$(ROS_DISTRO)/setup.bash && \
		export RMW_IMPLEMENTATION=rmw_fastrtps_cpp && \
		export ROS_DISCOVERY_SERVER=127.0.0.1:11811 && \
		ros2 launch $(CURDIR)/ros/launch/turtlebot_drivers.launch.py; read"
	@echo "Started tmux session 'ros2sim'. Attaching..."
	@tmux attach -t ros2sim

sim-ros2-stop:
	@tmux kill-session -t ros2sim 2>/dev/null || true
	@docker compose down ros-core 2>/dev/null || true

sim-gui-bare:
	LOBBY_KEY=LPy6XgmZ_RayuekaA6CPsA \
	API_BASE_URL=https://34.42.43.54.sslip.io/api \
	uv run scripts/run_sim_gui_bare.py

sim-gui-stop:
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml down

# VPN setup for remote robot connections
vpn-setup:
	@./scripts/setup-vpn.sh

vpn-setup-cloud:
	@./scripts/setup-vpn.sh $(shell terraform -chdir=terraform output -raw api_vm_ip 2>/dev/null || echo "YOUR_SERVER_IP")
# Cleanup
clean:
	rm -rf logs .pid_*
