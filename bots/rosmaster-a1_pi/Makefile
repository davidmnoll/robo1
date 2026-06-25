# Rosmaster A1 Pi Robot - ROS2 Package
PI_HOST ?= pi@raspberrypi.local
REMOTE_DIR ?= /home/pi/share/rosmaster
ROBOT_ID ?= rosmaster1
ROS_DOMAIN_ID ?= 61

.PHONY: help deploy restart stop logs status ssh shell

help:
	@echo "Rosmaster A1 Pi Robot"
	@echo ""
	@echo "Commands (run from dev machine):"
	@echo "  make deploy   - Push code, build, and restart robot"
	@echo "  make restart  - Restart robot (no code sync)"
	@echo "  make stop     - Stop robot"
	@echo "  make logs     - Tail robot logs"
	@echo "  make status   - Check robot status"
	@echo "  make ssh      - SSH to Pi"
	@echo "  make shell    - Shell into container"
	@echo ""
	@echo "Config: PI_HOST=$(PI_HOST) ROBOT_ID=$(ROBOT_ID)"

deploy:
	@echo "==> Syncing code..."
	@rsync -avz --filter=':- .gitignore' ./ $(PI_HOST):$(REMOTE_DIR)/
	@echo ""
	@echo "==> Setting up udev rules..."
	@ssh $(PI_HOST) "sudo cp $(REMOTE_DIR)/setup/99-rosmaster-serial.rules /etc/udev/rules.d/ 2>/dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger" || true
	@echo ""
	@echo "==> Setting up container..."
	@ssh $(PI_HOST) "chmod +x $(REMOTE_DIR)/setup/run-container.sh $(REMOTE_DIR)/start-robot.sh $(REMOTE_DIR)/stop-robot.sh"
	@ssh $(PI_HOST) "cp $(REMOTE_DIR)/setup/run-container.sh /home/pi/"
	@ssh $(PI_HOST) "/home/pi/run-container.sh"
	@echo ""
	@echo "==> Building ROS package..."
	@ssh $(PI_HOST) "docker exec rosmaster bash -c '\
		source /opt/ros/humble/setup.bash && \
		cd /root/share && colcon build --packages-select rosmaster_robot --symlink-install'"
	@echo ""
	@echo "==> Installing service..."
	@ssh $(PI_HOST) "sudo cp $(REMOTE_DIR)/rosmaster.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable rosmaster.service"
	@echo ""
	@echo "==> Starting robot..."
	@ssh $(PI_HOST) "sudo systemctl restart rosmaster.service"
	@sleep 3
	@$(MAKE) status --no-print-directory

restart:
	@echo "Restarting robot..."
	@ssh $(PI_HOST) "/home/pi/run-container.sh 2>/dev/null" || true
	@ssh $(PI_HOST) "sudo systemctl restart rosmaster.service"
	@sleep 2
	@$(MAKE) status --no-print-directory

stop:
	@ssh $(PI_HOST) "sudo systemctl stop rosmaster.service"
	@echo "Robot stopped."

logs:
	ssh $(PI_HOST) "sudo journalctl -u rosmaster.service -f"

status:
	@ssh $(PI_HOST) "sudo systemctl status rosmaster.service --no-pager -l 2>/dev/null" | head -15

ssh:
	ssh $(PI_HOST)

shell:
	ssh -t $(PI_HOST) "docker exec -it rosmaster bash"
