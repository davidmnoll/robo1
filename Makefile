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
	echo "[Cloud Run services]"; \
	gcloud run services list; \
	echo ""; \
	echo "[Cloud SQL instances]"; \
	gcloud sql instances list; \
	echo ""; \
	echo "[Artifact Registry images]"; \
	gcloud artifacts repositories list || true

cloud-sim:
	@bash -c 'set -euo pipefail; \
	API_URL="${CLOUD_RUN_API_URL}"; \
	if [ -z "$${API_URL}" ] && [ -d terraform ]; then \
	  API_URL=$$(cd terraform && terraform output -raw cloud_run_url 2>/dev/null || true); \
	  if [ -n "$${API_URL}" ]; then \
	    API_URL="$${API_URL%/}/api"; \
	  fi; \
	fi; \
	if [ -z "$${API_URL}" ]; then \
	  echo "Set CLOUD_RUN_API_URL to your Cloud Run API base (e.g. https://robot-gateway-.../api) or run terraform output first." >&2; exit 1; \
	fi; \
	export CLOUD_RUN_API_URL="$${API_URL%/}/api"; \
	if [ -z "${CLOUD_RUN_LOBBY_KEY:-}" ]; then \
	  echo "Set CLOUD_RUN_LOBBY_KEY to the lobby access key" >&2; exit 1; \
	fi; \
	echo "Starting ROS bridge + Webots sim against $$CLOUD_RUN_API_URL"; \
	docker compose -f docker-compose.yaml -f docker-compose.cloud.yml up ros-core sim'

# Cleanup
clean:
	rm -rf logs .pid_*
