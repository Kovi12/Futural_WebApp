#!/bin/bash
set -euo pipefail

LOG_DIR="/var/log/llm-backend-dual"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/startup_$(date +'%Y%m%d_%H%M%S').log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "[INFO] === Starting dual backend container setup ==="
echo "[INFO] Log file: $LOG_FILE"

export DUAL_BACKEND_PORT="${DUAL_BACKEND_PORT:-8010}"
COCOS_HOST="host.docker.internal"
PORTS=(23459 23456)

for P in "${PORTS[@]}"; do
  echo "[INFO] Checking reachability ${COCOS_HOST}:${P}/health ..."
  curl -s --max-time 3 "http://${COCOS_HOST}:${P}/health" || \
    echo "[WARN] Could not reach ${COCOS_HOST}:${P}"
done

echo "[INFO] Starting dual backend Flask server on port ${DUAL_BACKEND_PORT} ..."
exec python3 app_dual.py
