#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8081}"
VENV_PATH="${API_VENV:-${ROOT_DIR}/api/.venv}"

if [[ ! -x "${VENV_PATH}/bin/uvicorn" ]]; then
  echo "[api] Virtualenv missing at ${VENV_PATH}. Create it (e.g., uv venv) and install requirements." >&2
  exit 1
fi

cd "${ROOT_DIR}/api"

source "${VENV_PATH}/bin/activate"
echo "[api] Starting FastAPI gateway on port ${API_PORT} using ${VENV_PATH}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}"
