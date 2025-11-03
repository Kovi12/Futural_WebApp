#!/bin/bash
set -euo pipefail

LOG_DIR="/var/log/llm-backend"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/startup_$(date +'%Y%m%d_%H%M%S').log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "[INFO] === Starting backend container setup ==="
echo "[INFO] Log file: $LOG_FILE"

COCOS_HOST="host.docker.internal"
DEFAULT_PORT=23459

echo "[INFO] Testing reachability on ${COCOS_HOST}:${DEFAULT_PORT}..."
curl -s --max-time 3 "http://${COCOS_HOST}:${DEFAULT_PORT}/health" || \
  echo "[WARN] Could not reach ${COCOS_HOST}:${DEFAULT_PORT}"

echo "[INFO] Starting backend Flask server..."
exec python3 app.py
