#!/bin/bash

CONTAINER_ID=$(docker ps -q)

if [ -z "$CONTAINER_ID" ]; then
    echo "No Docker container running"
    exit 0
fi

echo "Stopping robot services..."
docker exec "$CONTAINER_ID" bash -c "pkill -f rosmaster_robot || true; pkill -f usb_cam || true"
echo "Stopped"
