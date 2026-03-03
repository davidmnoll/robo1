#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_PORT="${WEB_PORT:-4173}"

cd "${ROOT_DIR}/web/public"

echo "[web] Serving static files from web/public at http://localhost:${WEB_PORT}"
exec python3 -m http.server "${WEB_PORT}"
