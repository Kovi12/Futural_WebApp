#!/bin/bash
# Launches the DeepSeek(+adapter) server and exposes it to COCOS via autossh.
# Usage: ./slurm_job_deepseek.sh [target_node_suffix_or_fullname]
set -euo pipefail

TARGET_NODE="${1:-}"

BASE_DIR="${WEBAPP_BASE_DIR:-$HOME/Futural_WebApp}"
APP_FILE="${BASE_DIR}/http_model_server_deepseek.py"
MODEL_NAME="deepseek_durangaldea"
JOB_ID_SAFE="${SLURM_JOB_ID:-local}"

JOB_DIR="${BASE_DIR}/logs/${MODEL_NAME}/${JOB_ID_SAFE}"
SBATCH_DIR="${BASE_DIR}/logs/sbatch/${MODEL_NAME}"
STATUS_DIR="${BASE_DIR}/status"
mkdir -p "${JOB_DIR}" "${SBATCH_DIR}" "${STATUS_DIR}"

SUMMARY_LOG="${JOB_DIR}/model_debug.log"
AUTOSSH_LOG="${JOB_DIR}/autossh.log"

# Make these visible to the app
export WEBAPP_BASE_DIR="${BASE_DIR}"
export JOB_LOG_DIR="${JOB_DIR}"

# Distinct COCOS port for this DeepSeek service
export COCOS_PORT_DEEPSEEK="${COCOS_PORT_DEEPSEEK:-23512}"

log(){ printf '[%(%F %T%z)T] %s\n' -1 "$*" | tee -a "${SUMMARY_LOG}"; }

pick_free_port() {
  for _ in $(seq 1 80); do
    cand=$(shuf -i 20000-39999 -n 1)
    if ! ss -ltn | awk '{print $4}' | grep -q ":${cand}\$"; then
      echo "$cand"; return 0
    fi
  done
  return 1
}
LOCAL_PORT="$(pick_free_port || true)"
if [[ -z "${LOCAL_PORT}" ]]; then
  echo "[FATAL] Could not find a free local port" | tee -a "${SUMMARY_LOG}"
  exit 1
fi

diag() {
  local rc="${1:-$?}"
  log "========== DIAGNOSTICS (rc=${rc}) =========="
  date
  sacct -j "${SLURM_JOB_ID:-0},${SLURM_JOB_ID:-0}.batch,${SLURM_JOB_ID:-0}.0" \
    --format=JobID,JobName%18,State,ExitCode,DerivedExitCode,Elapsed,Timelimit,ReqTRES,AllocTRES,NodeList%20,End,Reason%30,Comment 2>&1 | tee -a "${SUMMARY_LOG}" || true
  scontrol show jobid -dd "${SLURM_JOB_ID:-0}" 2>&1 | tee -a "${SUMMARY_LOG}" || true
  echo "-- autossh tail --" | tee -a "${SUMMARY_LOG}"
  tail -n 120 "${AUTOSSH_LOG}" 2>/dev/null | tee -a "${SUMMARY_LOG}" || true
  log "========== END DIAGNOSTICS =========="
}
on_term(){ log "[DIAG] SIGTERM"; [[ -n "${AUTOSSH_WRAPPER_PID:-}" ]] && kill "${AUTOSSH_WRAPPER_PID}" 2>/dev/null || true; [[ -n "${UV_SRUN_PID:-}" ]] && kill "${UV_SRUN_PID}" 2>/dev/null || true; diag 143; exit 143; }
on_usr1(){ log "[DIAG] SIGUSR1 (notice)"; }
trap on_term TERM INT
trap on_usr1 USR1
trap 'diag $?' EXIT

# Optional conda
if [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
  # shellcheck source=/dev/null
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate mcp-poc || true
fi

log "[INFO] Starting DeepSeek server"
log "[INFO] Local PORT=${LOCAL_PORT} (will reverse tunnel to COCOS ${COCOS_PORT_DEEPSEEK})"
python -V || true

set -x
srun --ntasks=1 --cpu-bind=none --gres=gpu:1 --gpu-bind=single:1 \
  bash -lc 'PYTHONUNBUFFERED=1 uvicorn http_model_server_deepseek:app --host 0.0.0.0 --port '"${LOCAL_PORT}"' --log-level info' &
set +x
UV_SRUN_PID=$!
log "[INFO] Uvicorn (srun) PID: ${UV_SRUN_PID}"

# Wait for health
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-400}"
START_TS=$(date +%s)
until curl -sf "http://127.0.0.1:${LOCAL_PORT}/health" >/dev/null; do
  if ! kill -0 "${UV_SRUN_PID}" 2>/dev/null; then
    log "[ERROR] srun/uvicorn died during startup."
    exit 1
  fi
  if (( $(date +%s) - START_TS > HEALTH_TIMEOUT )); then
    log "[ERROR] Health check timed out."
    exit 1
  }
  log "[INFO] Waiting for model server health..."
  sleep 2
done
log "[INFO] Health OK."

# Reverse tunnel to cocos
export AUTOSSH_DEBUG=1
export AUTOSSH_GATETIME=0
export AUTOSSH_LOGLEVEL=7

start_autossh_loop() {
  while true; do
    "${BASE_DIR}/autossh/bin/autossh" \
      -M 0 -T -N -4 \
      -o "ServerAliveInterval=30" \
      -o "ServerAliveCountMax=3" \
      -o "ExitOnForwardFailure=yes" \
      -v -R 0.0.0.0:${COCOS_PORT_DEEPSEEK}:127.0.0.1:${LOCAL_PORT} \
      cocos >> "${AUTOSSH_LOG}" 2>&1
    echo "[WARN] autossh exited. Retrying in 3s..." >> "${AUTOSSH_LOG}"
    sleep 3
  done
}
: > "${AUTOSSH_LOG}"
start_autossh_loop & AUTOSSH_WRAPPER_PID=$!
log "[INFO] autossh wrapper PID: ${AUTOSSH_WRAPPER_PID}; log: ${AUTOSSH_LOG}"

# Warm-up
WARMUP_JSON='{"text":"ping","token":"warmup","session_id":"warmup","client_ip":"127.0.0.1"}'
curl -sS -m 12 -H "Content-Type: application/json" -d "${WARMUP_JSON}" \
  "http://127.0.0.1:${LOCAL_PORT}/query" >/dev/null || log "[WARN] Warm-up failed (continuing)."

wait "${UV_SRUN_PID}"
