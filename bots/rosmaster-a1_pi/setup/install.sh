#!/bin/bash
# Install Rosmaster A1 configuration on Raspberry Pi
#
# Run this script on the Pi after syncing the code:
#   cd ~/share/rosmaster/setup && sudo ./install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Rosmaster A1 configuration..."

# Install udev rules
echo "Installing udev rules..."
cp "$SCRIPT_DIR/99-rosmaster-serial.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger

# Install container run script
echo "Installing container run script..."
cp "$SCRIPT_DIR/run-container.sh" /home/pi/
chmod +x /home/pi/run-container.sh
chown pi:pi /home/pi/run-container.sh

# Verify udev rules
echo ""
echo "Verifying serial devices..."
for dev in /dev/myserial /dev/myspeech /dev/rplidar; do
    if [ -e "$dev" ]; then
        target=$(readlink -f "$dev")
        echo "  $dev -> $target"
    else
        echo "  $dev: NOT FOUND (device may not be connected)"
    fi
done

echo ""
echo "Installation complete!"
echo ""
echo "To start the robot:"
echo "  1. Run: /home/pi/run-container.sh"
echo "  2. Run: cd ~/share/rosmaster && ./start-robot.sh"
echo ""
echo "Or use systemd service:"
echo "  sudo systemctl enable rosmaster"
echo "  sudo systemctl start rosmaster"
