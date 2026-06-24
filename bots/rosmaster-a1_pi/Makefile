# Rosmaster A1 Pi Robot - ROS2 Package
PI_HOST ?= pi@raspberrypi.local
REMOTE_DIR ?= /home/pi/share/rosmaster
CONTAINER_DIR ?= /root/share/rosmaster
ROBOT_ID ?= rosmaster1
ROS_DOMAIN_ID ?= 61
# Discovery Server - set to your laptop/server IP
DISCOVERY_SERVER ?= 10.0.0.185:11811

.PHONY: help sync-to sync-from ssh shell start stop logs build run run-controller run-camera

help:
	@echo "Rosmaster A1 Pi Robot"
	@echo ""
	@echo "From dev machine:"
	@echo "  make sync-to        - Push code to Pi"
	@echo "  make sync-from      - Pull code from Pi"
	@echo "  make deploy         - Sync + build + install service"
	@echo "  make ssh            - SSH to the Pi"
	@echo "  make shell          - Shell into Docker container"
	@echo "  make build-remote   - Build package on Pi"
	@echo "  make start          - Start robot (remote, one-time)"
	@echo "  make stop           - Stop robot (remote)"
	@echo "  make logs           - Tail logs (remote)"
	@echo ""
	@echo "Systemd service (auto-start on boot):"
	@echo "  make install-service - Install/update systemd service"
	@echo "  make service-start   - Start the service"
	@echo "  make service-stop    - Stop the service"
	@echo "  make service-restart - Restart the service"
	@echo "  make service-status  - Check service status"
	@echo "  make service-logs    - Tail service logs"
	@echo ""
	@echo "On robot (in container):"
	@echo "  make build          - Build the ROS2 package"
	@echo "  make run            - Launch all services"
	@echo "  make run-controller - Controller only"
	@echo "  make run-camera     - Camera only"
	@echo ""
	@echo "Config: ROBOT_ID=$(ROBOT_ID) ROS_DOMAIN_ID=$(ROS_DOMAIN_ID)"

# ── Remote targets (run from dev machine) ──────────────────────────

sync-to:
	rsync -avz --filter=':- .gitignore' ./ $(PI_HOST):$(REMOTE_DIR)/
	@echo "Synced to $(PI_HOST):$(REMOTE_DIR)"

sync-from:
	rsync -avz --filter=':- .gitignore' $(PI_HOST):$(REMOTE_DIR)/ ./
	@echo "Synced from $(PI_HOST):$(REMOTE_DIR)"

ssh:
	ssh $(PI_HOST)

shell:
	ssh -t $(PI_HOST) "docker exec -it \$$(docker ps -q) bash"

start:
	ssh -t $(PI_HOST) "docker exec \$$(docker ps -q) bash -c '\
		source /opt/ros/humble/setup.bash && \
		source /root/share/install/setup.bash && \
		cd $(CONTAINER_DIR) && \
		export ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) && \
		export ROBOT_ID=$(ROBOT_ID) && \
		ros2 launch rosmaster_robot robot.launch.py robot_id:=$(ROBOT_ID)'"

stop:
	ssh -t $(PI_HOST) "docker exec \$$(docker ps -q) bash -c '\
		pkill -f rosmaster_robot || true && \
		pkill -f usb_cam || true && \
		echo Stopped'"

build-remote:
	@echo "Building ROS2 package on $(PI_HOST)..."
	ssh $(PI_HOST) "docker exec \$$(docker ps -q) bash -c 'cd $(CONTAINER_DIR) && make build'"

deploy: sync-to build-remote install-service
	@echo "Deployed and built on $(PI_HOST)"

install-service:
	@echo "Installing systemd service on $(PI_HOST)..."
	ssh $(PI_HOST) "chmod +x $(REMOTE_DIR)/start-robot.sh $(REMOTE_DIR)/stop-robot.sh"
	ssh $(PI_HOST) "sudo cp $(REMOTE_DIR)/rosmaster.service /etc/systemd/system/"
	ssh $(PI_HOST) "sudo systemctl daemon-reload"
	ssh $(PI_HOST) "sudo systemctl enable rosmaster.service"
	@echo "Service installed. Use 'make service-start' to start."

service-start:
	ssh $(PI_HOST) "sudo systemctl start rosmaster.service"
	@echo "Service started"

service-stop:
	ssh $(PI_HOST) "sudo systemctl stop rosmaster.service"
	@echo "Service stopped"

service-restart:
	ssh $(PI_HOST) "sudo systemctl restart rosmaster.service"
	@echo "Service restarted"

service-status:
	ssh $(PI_HOST) "sudo systemctl status rosmaster.service"

service-logs:
	ssh $(PI_HOST) "sudo journalctl -u rosmaster.service -f"

logs:
	ssh -t $(PI_HOST) "docker exec \$$(docker ps -q) bash -c '\
		journalctl -f -t ros 2>/dev/null || tail -f ~/.ros/log/*/rosout.log 2>/dev/null || echo No logs found'"

# ── Local targets (run ON the robot, inside container) ─────────────

build:
	@echo "Building ROS2 package..."
	bash -c "source /opt/ros/humble/setup.bash && cd .. && colcon build --packages-select rosmaster_robot --symlink-install"
	@echo ""
	@echo "Done! Now run: source ../install/setup.bash"

# Check if package is built and sourced
check-build:
	@if ! ros2 pkg list 2>/dev/null | grep -q rosmaster_robot; then \
		echo "ERROR: Package not found. Run:"; \
		echo "  make build"; \
		echo "  source ../install/setup.bash"; \
		exit 1; \
	fi

run: check-build
	@echo "Launching robot $(ROBOT_ID) on domain $(ROS_DOMAIN_ID)"
	ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) ROBOT_ID=$(ROBOT_ID) \
		ros2 launch rosmaster_robot robot.launch.py robot_id:=$(ROBOT_ID)

run-controller: check-build
	@echo "Starting controller for $(ROBOT_ID)"
	ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) ROBOT_ID=$(ROBOT_ID) \
		ros2 run rosmaster_robot controller

run-camera:
	@echo "Starting camera for $(ROBOT_ID)"
	ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) ros2 run usb_cam usb_cam_node_exe \
		--ros-args \
		-p video_device:=/dev/video0 \
		-p image_width:=640 \
		-p image_height:=480 \
		-p framerate:=15.0 \
		-r image_raw:=/$(ROBOT_ID)/camera/image_raw

run-audio: check-build
	@echo "Starting audio nodes for $(ROBOT_ID)"
	ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) ROBOT_ID=$(ROBOT_ID) \
		ros2 run rosmaster_robot audio_stream &
	ROS_DOMAIN_ID=$(ROS_DOMAIN_ID) ROBOT_ID=$(ROBOT_ID) \
		ros2 run rosmaster_robot audio_play
