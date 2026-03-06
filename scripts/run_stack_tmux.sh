#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION_NAME="${TMUX_SESSION_NAME:-arena}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed. Install it (e.g., sudo apt install tmux) to use this workflow." >&2
  exit 1
fi

set +e
tmux has-session -t "${SESSION_NAME}" >/dev/null 2>&1
HAS_SESSION=$?
set -e
if [[ ${HAS_SESSION} -eq 0 ]]; then
  echo "tmux session '${SESSION_NAME}' already exists. Attach via: tmux attach -t ${SESSION_NAME}" >&2
  exit 1
fi

tmux new-session -d -s "${SESSION_NAME}" -n ros-core "cd ${ROOT_DIR} && ./ros/run.sh; echo; echo '[ros-core] exited. Press Enter to close this pane.'; read"
tmux new-window -t "${SESSION_NAME}" -n sim "cd ${ROOT_DIR} && ./sim/run.sh; echo; echo '[sim] exited. Press Enter to close this pane.'; read"
tmux new-window -t "${SESSION_NAME}" -n api "cd ${ROOT_DIR} && ./api/run.sh; echo; echo '[api] exited. Press Enter to close this pane.'; read"
tmux new-window -t "${SESSION_NAME}" -n web "cd ${ROOT_DIR} && ./web/run.sh; echo; echo '[web] exited. Press Enter to close this pane.'; read"

tmux set-option -t "${SESSION_NAME}" remain-on-exit on

tmux select-window -t "${SESSION_NAME}":ros-core

cat <<EOF
Started tmux session '${SESSION_NAME}' with windows:
  [ros-core] rosbridge + camera_forwarder
  [sim]      Webots arena
  [api]      FastAPI gateway
  [web]      Static dashboard server

Attach with:
  tmux attach -t ${SESSION_NAME}

Use Ctrl+b then n/p to switch panes, and Ctrl+b then d to detach.
EOF
