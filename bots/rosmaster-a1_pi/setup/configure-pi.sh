#!/bin/bash
# Configure Raspberry Pi for Rosmaster robot
# Run this once after initial setup, or via deploy

set -e

echo "==> Configuring Pi for Rosmaster..."

# Disable Yahboom's default autostart
YAHBOOM_AUTOSTART="$HOME/.config/autostart/start_handle.desktop"
if [ -f "$YAHBOOM_AUTOSTART" ]; then
    echo "Disabling Yahboom autostart..."
    mv "$YAHBOOM_AUTOSTART" "$YAHBOOM_AUTOSTART.disabled"
fi

# Clean up old unnamed containers (Yahboom leftovers)
echo "Cleaning up old containers..."
docker ps -a --format '{{.Names}}' | grep -v '^rosmaster$' | while read name; do
    if [ -n "$name" ]; then
        echo "  Removing: $name"
        docker stop "$name" 2>/dev/null || true
        docker rm "$name" 2>/dev/null || true
    fi
done

# Ensure our service is enabled
echo "Enabling rosmaster service..."
sudo systemctl enable rosmaster.service 2>/dev/null || true

echo "==> Pi configuration complete"
