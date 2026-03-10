#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[ros-core] $*" >&2
}

# Source ROS2
export RCUTILS_LOGGING_MIN_SEVERITY=${RCUTILS_LOGGING_MIN_SEVERITY:-WARN}
set +u
unset AMENT_TRACE_SETUP_FILES
source /opt/ros/${ROS_DISTRO}/setup.bash >/dev/null 2>&1
set -u

# Use FastRTPS for Discovery Server support
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

# Track PIDs for cleanup
PIDS=()

cleanup() {
  log "Shutting down..."
  for pid in "${PIDS[@]}"; do
    if ps -p "${pid}" > /dev/null 2>&1; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  # Bring down WireGuard if it was up
  if ip link show wg0 &>/dev/null; then
    wg-quick down wg0 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- WireGuard VPN Server (optional) ---
if [[ -f /etc/wireguard/wg0.conf ]]; then
  log "Starting WireGuard VPN..."
  wg-quick up wg0
  log "WireGuard VPN started on 10.10.0.1/24"
else
  log "No WireGuard config found at /etc/wireguard/wg0.conf - skipping VPN"
  log "To enable VPN, mount a wg0.conf file"
fi

# --- ROS2 Discovery Server ---
log "Starting ROS2 Discovery Server on port 11811..."
fastdds discovery --server-id 0 --udp-address 0.0.0.0 --udp-port 11811 &
DISCOVERY_PID=$!
PIDS+=("$DISCOVERY_PID")
sleep 1

# Set this container to use the local discovery server
export ROS_DISCOVERY_SERVER=127.0.0.1:11811

# --- Rosbridge (for backwards compatibility with WebSocket clients) ---
log "Starting rosbridge WebSocket server on port 9090..."
ros2 run rosbridge_server rosbridge_websocket \
  --ros-args --log-level warn \
  -p default_call_service_timeout:=5.0 \
  -p call_services_in_new_thread:=true \
  -p send_action_goals_in_new_thread:=true &
ROSBRIDGE_PID=$!
PIDS+=("$ROSBRIDGE_PID")

# --- Camera Forwarder ---
log "Starting camera forwarder..."
python3 /workspace/camera_forwarder.py &
FORWARDER_PID=$!
PIDS+=("$FORWARDER_PID")

log "All services started. Waiting..."
wait "${FORWARDER_PID}"
