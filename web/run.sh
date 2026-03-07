#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_PORT="${WEB_PORT:-4173}"

cd "${ROOT_DIR}/web"
export VITE_API_BASE_URL="${VITE_API_BASE_URL:-http://localhost:8081/api}"
if [[ ! -d "node_modules" ]]; then
  echo "[web] Installing dashboard dependencies..."
  npm install
fi
echo "[web] Starting Vite dev server on http://localhost:${WEB_PORT}"
exec npm run dev -- --host 0.0.0.0 --port "${WEB_PORT}"
