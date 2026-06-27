SHELL := /bin/bash
-include .env
ROS_DISTRO ?= jazzy
ROS_DOMAIN_ID ?= 61
ROS_SETUP := source /opt/ros/$(ROS_DISTRO)/setup.bash
PROJECT_ID ?= robo1-489405
export TURTLEBOT3_MODEL := burger

.PHONY: help sim bridge web controller all stop clean tmux-stack attach all sim-gui-bare

attach:
	tmux attach -t arena

help:
	@echo "Arena stack commands"
	@echo ""
	@echo "  make dev        - Launch ros-bridge + web pointing to DEV servers"
	@echo "  make dev-local  - Launch local api + ros-bridge + web (fully local)"
	@echo "  make tmux-stack - Launch ros-core, sim, api, web in tmux"
	@echo "  make cloud-ros  - Run ros-bridge connected to PROD cloud API"
	@echo "  make cloud-web  - Run local dashboard against PROD cloud API"
	@echo "  make all        - Alias for make tmux-stack"
	@echo "  make attach     - Attach to tmux session (arena)"
	@echo "  make db-shell   - Open psql shell inside the db container"
	@echo ""
	@echo "Robot commands (Rosmaster A1 Pi):"
	@echo "  make robot-push    - Push code to robot, build, and restart"
	@echo "  make robot-pull    - Pull code/configs from robot"
	@echo "  make robot-restart - Restart robot service"
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

# Run local dashboard against cloud API
cloud-web:
	@echo "Starting Vite dev server pointing to cloud API..."
	VITE_API_BASE_URL=https://34.42.43.54.sslip.io/api ./web/run.sh

controller:
	$(ROS_SETUP) && python3 bots/turtlebot3/simple_controller.py

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

# Run ros-bridge + web pointing to DEV cloud servers
dev:
	@if ! command -v tmux >/dev/null 2>&1; then \
		echo "tmux required. Install with: sudo apt install tmux"; exit 1; \
	fi
	@DEV_IP=$$($(MAKE) -s dev-ip); \
	if [ -z "$$DEV_IP" ] || [ "$$DEV_IP" = "Dev VM not found. Run deployment first." ]; then \
		echo "Error: Could not get dev VM IP. Is the dev environment deployed?"; \
		exit 1; \
	fi; \
	tmux kill-session -t dev 2>/dev/null || true; \
	tmux new-session -d -s dev -n ros-bridge "cd $(CURDIR) && $(MAKE) dev-cloud-ros; read"; \
	tmux new-window -t dev -n web "cd $(CURDIR) && VITE_API_BASE_URL=https://$$DEV_IP.sslip.io/api ./web/run.sh; read"; \
	echo "Started tmux session 'dev' with [ros-bridge -> dev] [web -> dev]"; \
	echo "Dev API: https://$$DEV_IP.sslip.io/api"; \
	tmux attach -t dev

# Run local api + ros-bridge + web (fully local stack)
dev-local:
	@if ! command -v tmux >/dev/null 2>&1; then \
		echo "tmux required. Install with: sudo apt install tmux"; exit 1; \
	fi
	@tmux kill-session -t dev 2>/dev/null || true
	@tmux new-session -d -s dev -n api "cd $(CURDIR) && ./api/run.sh; read"
	@tmux new-window -t dev -n ros-bridge "cd $(CURDIR) && $(MAKE) dev-ros; read"
	@tmux new-window -t dev -n web "cd $(CURDIR) && ./web/run.sh; read"
	@echo "Started tmux session 'dev' with [api] [ros-bridge] [web] (all local)"
	@tmux attach -t dev

# Build ros-bridge Humble container
dev-ros-build:
	docker build -t ros-bridge-humble -f $(CURDIR)/ros-bridge/Dockerfile.humble $(CURDIR)/ros-bridge

# Run ros-bridge in Humble container (for connecting to Humble robots)
dev-ros:
	@if ! docker image inspect ros-bridge-humble >/dev/null 2>&1; then \
		echo "Building ros-bridge-humble image..."; \
		$(MAKE) dev-ros-build; \
	fi
	@echo "Starting ros-bridge in Humble container on port 9090..."
	docker run --rm --net=host \
		-v $(CURDIR)/ros-bridge:/ros-bridge \
		-e ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) \
		-e API_BASE_URL=http://localhost:8080/api \
		-e LOBBY_KEY=local-dev-key \
		ros-bridge-humble

# Run ros-bridge connected to cloud API
cloud-ros:
	@if ! docker image inspect ros-bridge-humble >/dev/null 2>&1; then \
		echo "Building ros-bridge-humble image..."; \
		$(MAKE) dev-ros-build; \
	fi
	@echo "Starting ros-bridge connected to cloud API..."
	docker run --rm --net=host \
		-v $(CURDIR)/ros-bridge:/ros-bridge \
		-e ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) \
		-e API_BASE_URL=https://34.42.43.54.sslip.io/api \
		-e LOBBY_KEY=G-lV6Jrg0DW-m78AClMySQ \
		ros-bridge-humble

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
		ros2 launch $(CURDIR)/ros-bridge/launch/turtlebot_drivers.launch.py; read"
	@echo "Started tmux session 'ros2sim'. Attaching..."
	@tmux attach -t ros2sim

sim-ros2-stop:
	@tmux kill-session -t ros2sim 2>/dev/null || true
	@docker compose down ros-core 2>/dev/null || true

sim-gui-bare:
ifeq ($(OS),Windows_NT)
	MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-24.04 -e bash -c "\
		export LOBBY_KEY=LPy6XgmZ_RayuekaA6CPsA; \
		export API_BASE_URL=https://34.42.43.54.sslip.io/api; \
		export ROS_VENV=/home/dmn/robo1-ros-venv; \
		cd /mnt/c/Users/dmn32/main/code/project/robo1; \
		python3 scripts/run_sim_gui_bare.py"
else
	LOBBY_KEY=LPy6XgmZ_RayuekaA6CPsA \
	API_BASE_URL=https://34.42.43.54.sslip.io/api \
	python3 scripts/run_sim_gui_bare.py
endif

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


mic-to-bot:
	source /opt/ros/$(ROS_DISTRO)/setup.bash && export ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) && python3 scripts/mic_to_robot.py

bot-to-speaker:
	source /opt/ros/$(ROS_DISTRO)/setup.bash && export ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) && python3 scripts/robot_to_speaker.py

# =============================================================================
# Dev Environment Deployment Commands
# =============================================================================
.PHONY: dev-deploy dev-logs dev-ssh dev-status dev-url dev-ip

DEV_VM_NAME ?= robot-gateway-api-dev
DEV_PROJECT_ID ?= robo1-489405
DEV_ZONE ?= us-central1-a
DEV_FRONTEND_BUCKET ?= robo1-489405-dev-frontend

dev-deploy:
	@echo "Pushing to dev branch to trigger deployment..."
	git push origin dev

dev-ip:
	@gcloud compute instances describe $(DEV_VM_NAME) \
		--project=$(DEV_PROJECT_ID) \
		--zone=$(DEV_ZONE) \
		--format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || \
		echo "Dev VM not found. Run deployment first."

dev-logs:
	gcloud compute ssh $(DEV_VM_NAME) \
		--project=$(DEV_PROJECT_ID) \
		--zone=$(DEV_ZONE) \
		--tunnel-through-iap \
		--command="sudo docker logs robot-gateway -f --tail=100"

dev-ssh:
	gcloud compute ssh $(DEV_VM_NAME) \
		--project=$(DEV_PROJECT_ID) \
		--zone=$(DEV_ZONE) \
		--tunnel-through-iap

dev-status:
	@echo "=== Dev VM Status ==="
	@gcloud compute ssh $(DEV_VM_NAME) \
		--project=$(DEV_PROJECT_ID) \
		--zone=$(DEV_ZONE) \
		--tunnel-through-iap \
		--command="echo '--- Docker containers ---' && sudo docker ps && echo '' && echo '--- API Health ---' && curl -s localhost:8080/api/health | head -c 200"

dev-url:
	@echo "https://storage.googleapis.com/$(DEV_FRONTEND_BUCKET)/index.html"

# Run local dashboard against dev API
dev-cloud-web:
	@DEV_IP=$$($(MAKE) -s dev-ip); \
	if [ -z "$$DEV_IP" ] || [ "$$DEV_IP" = "Dev VM not found. Run deployment first." ]; then \
		echo "Error: Could not get dev VM IP. Is the dev environment deployed?"; \
		exit 1; \
	fi; \
	echo "Starting Vite dev server pointing to dev API at https://$$DEV_IP.sslip.io..."; \
	VITE_API_BASE_URL="https://$$DEV_IP.sslip.io/api" ./web/run.sh

# Run ros-bridge connected to dev API
dev-cloud-ros:
	@DEV_IP=$$($(MAKE) -s dev-ip); \
	if [ -z "$$DEV_IP" ] || [ "$$DEV_IP" = "Dev VM not found. Run deployment first." ]; then \
		echo "Error: Could not get dev VM IP. Is the dev environment deployed?"; \
		exit 1; \
	fi; \
	if ! docker image inspect ros-bridge-humble >/dev/null 2>&1; then \
		echo "Building ros-bridge-humble image..."; \
		$(MAKE) dev-ros-build; \
	fi; \
	echo "Starting ros-bridge connected to dev API at https://$$DEV_IP.sslip.io..."; \
	docker run --rm --net=host \
		-v $(CURDIR)/ros-bridge:/ros-bridge \
		-e ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) \
		-e API_BASE_URL="https://$$DEV_IP.sslip.io/api" \
		-e LOBBY_KEY=G-lV6Jrg0DW-m78AClMySQ \
		ros-bridge-humble

# =============================================================================
# Robot Commands (Rosmaster A1 Pi)
# =============================================================================
.PHONY: robot-push robot-pull robot-restart

robot-push:
	$(MAKE) -C bots/rosmaster-a1_pi deploy

robot-pull:
	@echo "Pulling code/configs from robot..."
	rsync -avz --filter=':- .gitignore' pi@raspberrypi.local:/home/pi/share/rosmaster/ bots/rosmaster-a1_pi/

robot-restart:
	$(MAKE) -C bots/rosmaster-a1_pi restart