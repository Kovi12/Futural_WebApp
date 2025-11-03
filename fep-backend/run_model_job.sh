#!/bin/bash
# Usage: run_model_job.sh <model_name>
set -euo pipefail

MODEL_NAME="${1:-}"
if [[ -z "$MODEL_NAME" ]]; then
  echo "Usage: $0 <model_name>" >&2
  exit 1
fi

# ---- paths ----
BASE_DIR="${WEBAPP_BASE_DIR:-$HOME/Futural_WebApp}"
JOB_ID_SAFE="${SLURM_JOB_ID:-local}"
JOB_LOG_DIR="${BASE_DIR}/logs/${MODEL_NAME}/${JOB_ID_SAFE}"
mkdir -p "${JOB_LOG_DIR}"

# expose to the app
export WEBAPP_BASE_DIR="${BASE_DIR}"
export JOB_LOG_DIR
export MODEL_NAME
export CONV_DB="${CONV_DB:-${BASE_DIR}/data/conversations.sqlite}"


SBATCH_LOG_ROOT="${BASE_DIR}/logs/sbatch/${MODEL_NAME}"
OUT_SRC="${SBATCH_LOG_ROOT}/${MODEL_NAME}_${JOB_ID_SAFE}.out"
ERR_SRC="${SBATCH_LOG_ROOT}/${MODEL_NAME}_${JOB_ID_SAFE}.err"
ln -sf "${OUT_SRC}" "${JOB_LOG_DIR}/slurm.out"
ln -sf "${ERR_SRC}" "${JOB_LOG_DIR}/slurm.err"

# MCP env
export MCP_TRANSPORT="${MCP_TRANSPORT:-stdio}"
export MCP_BASE_DIR="${MCP_BASE_DIR:-${BASE_DIR}/MCP-main}"
export MCP_SERVER_SCRIPT="${MCP_SERVER_SCRIPT:-${MCP_BASE_DIR}/run_server.py}"

# model -> fixed cocos port
case "$MODEL_NAME" in
  compression) COCOS_PORT=23459 ;;
  meteo)       COCOS_PORT=23457 ;;
  durangaldea) COCOS_PORT=23456 ;;
  *) echo "Unknown model name: $MODEL_NAME" >&2; exit 1 ;;
esac

SUMMARY_LOG="${JOB_LOG_DIR}/model_debug.log"
AUTOSSH_LOG="${JOB_LOG_DIR}/autossh.log"
log(){ printf '[%(%F %T%z)T] %s\n' -1 "$*" | tee -a "${SUMMARY_LOG}"; }

# ---- traps & diagnostics ----
diag() {
  local rc="${1:-$?}"
  log "========== DIAGNOSTICS (rc=${rc}) =========="
  date
  sacct -j "${SLURM_JOB_ID:-0},${SLURM_JOB_ID:-0}.batch,${SLURM_JOB_ID:-0}.0" \
    --format=JobID,JobName%18,State,ExitCode,DerivedExitCode,Elapsed,Timelimit,ReqTRES,AllocTRES,NodeList%20,End,Reason%30,Comment 2>&1 | tee -a "${SUMMARY_LOG}" || true
  scontrol show jobid -dd "${SLURM_JOB_ID:-0}" 2>&1 | tee -a "${SUMMARY_LOG}" || true
  echo "-- ps (uvicorn srun) --" | tee -a "${SUMMARY_LOG}"
  ps -o pid,ppid,pgid,etime,stat,cmd -p "${UV_SRUN_PID:-0}" | tee -a "${SUMMARY_LOG}" || true
  echo "-- autossh tail --" | tee -a "${SUMMARY_LOG}"
  tail -n 80 "${AUTOSSH_LOG}" 2>/dev/null | tee -a "${SUMMARY_LOG}" || true
  log "========== END DIAGNOSTICS =========="
}
on_term(){ log "[DIAG] SIGTERM"; diag 143; [[ -n "${AUTOSSH_WRAPPER_PID:-}" ]] && kill "${AUTOSSH_WRAPPER_PID}" 2>/dev/null || true; [[ -n "${UV_SRUN_PID:-}" ]] && kill "${UV_SRUN_PID}" 2>/dev/null || true; exit 143; }
on_usr1(){ log "[DIAG] SIGUSR1 (notice)"; }
trap on_term TERM INT
trap on_usr1 USR1
trap 'diag $?' EXIT

# pick free local port for uvicorn
pick_free_port() {
  for _ in $(seq 1 50); do
    cand=$(shuf -i 20000-39999 -n 1)
    if ! ss -ltn | awk '{print $4}' | grep -q ":${cand}\$"; then
      echo "$cand"; return 0
    fi
  done
  return 1
}
PORT="$(pick_free_port || true)"
if [[ -z "${PORT}" ]]; then
  log "[ERROR] Could not find a free local port"
  exit 1
fi

# optional conda
if [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
  # shellcheck source=/dev/null
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate mcp-poc || true
fi

log "[INFO] Starting model server on ${PORT} for ${MODEL_NAME}"
log "[INFO] which python: $(command -v python || true)"
python --version || true
log "[INFO] which uvicorn: $(command -v uvicorn || true)"
log "[INFO] key env: MCP_TRANSPORT=${MCP_TRANSPORT} MCP_BASE_DIR=${MCP_BASE_DIR}"
log "[INFO] SLURM_JOB_GPUS=${SLURM_JOB_GPUS:-unset} CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"

# start uvicorn bound to srun step
set -x
srun --ntasks=1 --cpu-bind=none --gres=gpu:1 --gpu-bind=single:1 \
  bash -lc 'PYTHONUNBUFFERED=1 uvicorn http_model_server:app --host 0.0.0.0 --port '"${PORT}"' --log-level info' &
set +x
UV_SRUN_PID=$!
log "[INFO] Uvicorn (srun step) PID: ${UV_SRUN_PID}"

# wait for health
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-400}"
START_TS=$(date +%s)
until curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; do
  if ! kill -0 "${UV_SRUN_PID}" 2>/dev/null; then
    log "[ERROR] srun/uvicorn died during startup."
    exit 1
  fi
  if (( $(date +%s) - START_TS > HEALTH_TIMEOUT )); then
    log "[ERROR] Health check timed out."
    exit 1
  fi
  log "[INFO] Waiting for model server health..."
  sleep 2
done
log "[INFO] Health OK."

# reverse tunnel to cocos
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
      -v -R 0.0.0.0:${COCOS_PORT}:127.0.0.1:${PORT} \
      cocos >> "${AUTOSSH_LOG}" 2>&1
    echo "[WARN] autossh exited. Retrying in 3s..." >> "${AUTOSSH_LOG}"
    sleep 3
  done
}
: > "${AUTOSSH_LOG}"
start_autossh_loop & AUTOSSH_WRAPPER_PID=$!
log "[INFO] autossh wrapper PID: ${AUTOSSH_WRAPPER_PID}; log: ${AUTOSSH_LOG}"

# warm-up
WARMUP_JSON='{"text":"ping","token":"warmup","session_id":"warmup","client_ip":"127.0.0.1"}'
if curl -sS -m 15 -H "Content-Type: application/json" \
  -d "${WARMUP_JSON}" "http://127.0.0.1:${PORT}/query" >/dev/null; then
  log "[INFO] Warm-up query sent."
else
  log "[WARN] Warm-up query failed (continuing)."
fi

wait "${UV_SRUN_PID}"
