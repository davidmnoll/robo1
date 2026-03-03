#!/usr/bin/env bash
set -euo pipefail

source /opt/ros/${ROS_DISTRO}/setup.bash

WORLD_PATH="${WEBOTS_WORLD:-/workspace/worlds/battle_arena.wbt}"

if [[ ! -f "${WORLD_PATH}" ]]; then
  echo "Webots world ${WORLD_PATH} is missing. Mount or copy a valid world file." >&2
  exit 1
fi

# xvfb-run keeps Webots happy even when no GPU/display is present.
exec xvfb-run --auto-servernum --server-args='-screen 0 1280x720x24' \
  webots --stdout --stderr --batch --mode=fast "${WORLD_PATH}"
