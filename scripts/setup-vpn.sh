#!/bin/bash
# Setup WireGuard VPN for ros-core container
# Usage: ./setup-vpn.sh [SERVER_PUBLIC_IP]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
WG_DIR="$PROJECT_DIR/ros/wireguard"
CLIENTS_DIR="$WG_DIR/clients"

SERVER_IP="${1:-$(curl -s ifconfig.me || echo "YOUR_SERVER_IP")}"
VPN_SUBNET="10.10.0"
SERVER_VPN_IP="$VPN_SUBNET.1"

mkdir -p "$WG_DIR" "$CLIENTS_DIR"

echo "Setting up WireGuard VPN..."
echo "Server public IP: $SERVER_IP"
echo "VPN subnet: $VPN_SUBNET.0/24"
echo ""

# Generate server keys if they don't exist
if [[ ! -f "$WG_DIR/server_private.key" ]]; then
    echo "Generating server keys..."
    wg genkey | tee "$WG_DIR/server_private.key" | wg pubkey > "$WG_DIR/server_public.key"
    chmod 600 "$WG_DIR/server_private.key"
fi

SERVER_PRIVATE_KEY=$(cat "$WG_DIR/server_private.key")
SERVER_PUBLIC_KEY=$(cat "$WG_DIR/server_public.key")

# Create server config
cat > "$WG_DIR/wg0.conf" << EOF
[Interface]
Address = $SERVER_VPN_IP/24
ListenPort = 51820
PrivateKey = $SERVER_PRIVATE_KEY
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

# Clients will be added below
EOF

chmod 600 "$WG_DIR/wg0.conf"

echo "Server config created at: $WG_DIR/wg0.conf"
echo "Server public key: $SERVER_PUBLIC_KEY"
echo ""

# Function to add a client
add_client() {
    local CLIENT_NAME="$1"
    local CLIENT_NUM="$2"
    local CLIENT_IP="$VPN_SUBNET.$CLIENT_NUM"
    local CLIENT_DIR="$CLIENTS_DIR/$CLIENT_NAME"

    mkdir -p "$CLIENT_DIR"

    if [[ ! -f "$CLIENT_DIR/private.key" ]]; then
        wg genkey | tee "$CLIENT_DIR/private.key" | wg pubkey > "$CLIENT_DIR/public.key"
        chmod 600 "$CLIENT_DIR/private.key"
    fi

    CLIENT_PRIVATE_KEY=$(cat "$CLIENT_DIR/private.key")
    CLIENT_PUBLIC_KEY=$(cat "$CLIENT_DIR/public.key")

    # Add client to server config
    cat >> "$WG_DIR/wg0.conf" << EOF

[Peer]
# $CLIENT_NAME
PublicKey = $CLIENT_PUBLIC_KEY
AllowedIPs = $CLIENT_IP/32
EOF

    # Create client config
    cat > "$CLIENT_DIR/wg0.conf" << EOF
[Interface]
Address = $CLIENT_IP/24
PrivateKey = $CLIENT_PRIVATE_KEY
DNS = 8.8.8.8

[Peer]
PublicKey = $SERVER_PUBLIC_KEY
Endpoint = $SERVER_IP:51820
AllowedIPs = $VPN_SUBNET.0/24
PersistentKeepalive = 25
EOF

    chmod 600 "$CLIENT_DIR/wg0.conf"

    echo "Client '$CLIENT_NAME' config created at: $CLIENT_DIR/wg0.conf"
    echo "  VPN IP: $CLIENT_IP"
    echo "  Copy wg0.conf to the client's /etc/wireguard/"
}

# Create default clients
echo "Creating client configs..."
add_client "robot1" 2
add_client "robot2" 3
add_client "laptop" 4

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To use:"
echo "1. Start ros-core: docker compose up ros-core"
echo "2. Copy client configs to robots/laptops: scp $CLIENTS_DIR/robot1/wg0.conf robot1:/etc/wireguard/"
echo "3. Start VPN on client: sudo wg-quick up wg0"
echo "4. Set ROS2 Discovery Server: export ROS_DISCOVERY_SERVER=$SERVER_VPN_IP:11811"
echo ""
echo "Clients can then publish/subscribe to ROS2 topics!"
