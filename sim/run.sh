#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM_ROOT="${SIM_WORKSPACE_ROOT:-${SCRIPT_DIR}}"
export SIM_WORKSPACE_ROOT="${SIM_ROOT}"

if [[ -z "${WEBOTS_WORLD:-}" ]]; then
  export WEBOTS_WORLD="${SIM_WORKSPACE_ROOT}/worlds/battle_arena.wbt"
fi

exec "${SCRIPT_DIR}/entrypoint.sh" "$@"
