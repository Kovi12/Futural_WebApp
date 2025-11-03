#!/bin/bash
# Usage: run_model_job_dual.sh <llama_dual|deepseek_dual>
set -euo pipefail

MODEL_NAME="${1:-}"
if [[ -z "$MODEL_NAME" ]]; then
  echo "Usage: $0 <llama_dual|deepseek_dual>" >&2
  exit 1
fi
if [[ "$MODEL_NAME" != "llama_dual" && "$MODEL_NAME" != "deepseek_dual" ]]; then
  echo "Error: MODEL_NAME must be one of: llama_dual, deepseek_dual" >&2
  exit 1
fi

# ---- paths ----
BASE_DIR="${WEBAPP_BASE_DIR:-$HOME/Futural_WebApp}"
JOB_ID_SAFE="${SLURM_JOB_ID:-local}"
JOB_LOG_DIR="${BASE_DIR}/logs/${MODEL_NAME}/${JOB_ID_SAFE}"
mkdir -p "${JOB_LOG_DIR}"

# Export for the app
export WEBAPP_BASE_DIR="${BASE_DIR}"
export JOB_LOG_DIR
export MODEL_NAME
export CONV_DB="${CONV_DB:-${BASE_DIR}/data/conversations.sqlite}"

# Link slurm’s sbatch streams (created by slurm_job_dual.sh)
SBATCH_LOG_ROOT="${BASE_DIR}/logs/sbatch/${MODEL_NAME}"
OUT_SRC="${SBATCH_LOG_ROOT}/${MODEL_NAME}_${JOB_ID_SAFE}.out"
ERR_SRC="${SBATCH_LOG_ROOT}/${MODEL_NAME}_${JOB_ID_SAFE}.err"
ln -sf "${OUT_SRC}" "${JOB_LOG_DIR}/slurm.out"  || true
ln -sf "${ERR_SRC}" "${JOB_LOG_DIR}/slurm.err"  || true

SUMMARY_LOG="${JOB_LOG_DIR}/model_debug.log"
# Use /tmp for autossh log to avoid any race/permission/path issues
AUTOSSH_LOG="/tmp/autossh_${MODEL_NAME}_${JOB_ID_SAFE}.log"
: > "${AUTOSSH_LOG}" || true

log(){ printf '[%(%F %T%z)T] %s\n' -1 "$*" | tee -a "${SUMMARY_LOG}"; }

# ---- reverse port map (aligns with cocos public ports) ----
case "$MODEL_NAME" in
  llama_dual)    COCOS_PORT=23459 ;;  # llama dual (meteo+compression)
  deepseek_dual) COCOS_PORT=23456 ;;  # durangaldea (deepseek)
esac

# ---- traps & diagnostics ----
diag() {
  local rc="${1:-$?}"
  log "========== DIAGNOSTICS (rc=${rc}) =========="
  date | tee -a "${SUMMARY_LOG}" || true
  sacct -j "${SLURM_JOB_ID:-0},${SLURM_JOB_ID:-0}.batch,${SLURM_JOB_ID:-0}.0" \
    --format=JobID,JobName%18,State,ExitCode,DerivedExitCode,Elapsed,Timelimit,ReqTRES,AllocTRES,NodeList%20,End,Reason%30,Comment 2>&1 | tee -a "${SUMMARY_LOG}" || true
  scontrol show jobid -dd "${SLURM_JOB_ID:-0}" 2>&1 | tee -a "${SUMMARY_LOG}" || true
  echo "-- ps (uvicorn srun) --" | tee -a "${SUMMARY_LOG}"
  if [[ -n "${UV_SRUN_PID:-}" ]]; then
    ps -o pid,ppid,pgid,etime,stat,cmd -p "${UV_SRUN_PID}" | tee -a "${SUMMARY_LOG}" || true
  else
    echo "(no UV_SRUN_PID yet)" | tee -a "${SUMMARY_LOG}"
  fi
  echo "-- autossh tail --" | tee -a "${SUMMARY_LOG}"
  tail -n 200 "${AUTOSSH_LOG}" 2>/dev/null | tee -a "${SUMMARY_LOG}" || true
  log "========== END DIAGNOSTICS =========="
}
on_term(){ log "[DIAG] SIGTERM"; diag 143; [[ -n "${AUTOSSH_WRAPPER_PID:-}" ]] && kill "${AUTOSSH_WRAPPER_PID}" 2>/dev/null || true; [[ -n "${UV_SRUN_PID:-}" ]] && kill "${UV_SRUN_PID}" 2>/dev/null || true; exit 143; }
on_usr1(){ log "[DIAG] SIGUSR1 (notice)"; }
trap on_term TERM INT
trap on_usr1 USR1
trap 'diag $?' EXIT

# ---- pick free local port for uvicorn ----
pick_free_port() {
  for _ in $(seq 1 50); do
    local cand
    cand="$(shuf -i 20000-39999 -n 1)"
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

# ---- choose uvicorn app ----
case "$MODEL_NAME" in
  llama_dual)    UVICORN_APP="http_model_server_llama_dual:app" ;;
  deepseek_dual) UVICORN_APP="http_model_server_deepseek_dual:app" ;;
esac

# ---- locate env python / conda run / conda.sh ----
PY_BIN=""
CONDA_RUN=""
CONDA_SH=""

CANDIDATE_BASES=("$HOME/miniconda3" "$HOME/anaconda3" "$HOME/conda" "$HOME/.conda")
for base in "${CANDIDATE_BASES[@]}"; do
  if [[ -x "$base/envs/mcp-poc/bin/python" ]]; then
    PY_BIN="$base/envs/mcp-poc/bin/python"
    break
  fi
done
for base in "${CANDIDATE_BASES[@]}"; do
  if [[ -x "$base/bin/conda" ]]; then
    CONDA_RUN="$base/bin/conda"
    break
  fi
done
for base in "${CANDIDATE_BASES[@]}"; do
  if [[ -f "$base/etc/profile.d/conda.sh" ]]; then
    CONDA_SH="$base/etc/profile.d/conda.sh"
    break
  fi
done

log "[INFO] Env detect: PY_BIN='${PY_BIN:-}' CONDA_RUN='${CONDA_RUN:-}' CONDA_SH='${CONDA_SH:-}'"

# ---- SRUN: robust env activation in the subshell ----
set -x
srun --ntasks=1 --cpu-bind=none --gres=gpu:1 --gpu-bind=single:1 \
  bash -lc '
    set -euo pipefail
    echo "[SRUN] starting on $(hostname)"

    RUN_CMD=()
    if [[ -n "'"${PY_BIN}"'" ]]; then
      echo "[SRUN] using env python: '"${PY_BIN}"'"
      RUN_CMD=("'"${PY_BIN}"'")
    elif [[ -n "'"${CONDA_RUN}"'" ]]; then
      echo "[SRUN] using conda run"
      RUN_CMD=("'"${CONDA_RUN}"'" run -n mcp-poc python)
    elif [[ -n "'"${CONDA_SH}"'" ]]; then
      echo "[SRUN] sourcing conda.sh and activating mcp-poc"
      source "'"${CONDA_SH}"'"
      conda activate mcp-poc
      RUN_CMD=(python)
    else
      echo "[SRUN][FATAL] No conda env found and no conda.sh to activate. Aborting."
      exit 1
    fi

    echo "[SRUN] which python: $(command -v "${RUN_CMD[0]}" || true)"
    "${RUN_CMD[@]}" --version || true

    # quick sanity import so we fail fast if env is wrong
    "${RUN_CMD[@]}" - <<PY
try:
    import typing_extensions, fastapi, uvicorn
    print("[SRUN] imports OK")
except Exception as e:
    import sys, traceback
    print("[SRUN] import error:", e)
    traceback.print_exc()
    sys.exit(1)
PY

    # NOTE: Unsloth complains if imported after transformers;
    # our http_model_server_* files already import in the correct order.
    PYTHONUNBUFFERED=1 "${RUN_CMD[@]}" -m uvicorn '"${UVICORN_APP}"' --host 0.0.0.0 --port '"${PORT}"' --log-level info
  ' &

set +x
UV_SRUN_PID=$!
log "[INFO] Uvicorn (srun step) PID: ${UV_SRUN_PID}"

# ---- wait for health ----
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-1000}"
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

# ---- reverse tunnel to cocos (autossh) ----
export AUTOSSH_DEBUG=1
export AUTOSSH_GATETIME=0
export AUTOSSH_LOGLEVEL=7

# Prefer bundled autossh, else fallback to system autossh
AUTOSSH_BIN="${BASE_DIR}/autossh/bin/autossh"
if [[ ! -x "${AUTOSSH_BIN}" ]]; then
  AUTOSSH_BIN="$(command -v autossh || true)"
fi
if [[ -z "${AUTOSSH_BIN}" ]]; then
  log "[ERROR] autossh not found (neither ${BASE_DIR}/autossh/bin/autossh nor system autossh)."
  exit 1
fi
log "[INFO] Using autossh at: ${AUTOSSH_BIN}"

if ! ssh -o BatchMode=yes -o ConnectTimeout=5 cocos true 2>>"${AUTOSSH_LOG}"; then
  log "[WARN] SSH to 'cocos' failed in quick probe (this may still work when autossh retries). Check SSH config/keys."
fi

start_autossh_loop() {
  while true; do
    "${AUTOSSH_BIN}" \
      -M 0 -T -N -4 \
      -o "ServerAliveInterval=30" \
      -o "ServerAliveCountMax=3" \
      -o "ExitOnForwardFailure=yes" \
      -v -R 0.0.0.0:"${COCOS_PORT}":127.0.0.1:"${PORT}" \
      cocos >> "${AUTOSSH_LOG}" 2>&1
    echo "[WARN] autossh exited. Retrying in 3s..." >> "${AUTOSSH_LOG}"
    sleep 3
  done
}
start_autossh_loop & AUTOSSH_WRAPPER_PID=$!
log "[INFO] autossh wrapper PID: ${AUTOSSH_WRAPPER_PID}; log: ${AUTOSSH_LOG}"
log "[INFO] Reverse exposed at cocos:${COCOS_PORT}"

# ---- warm-up (non-fatal) ----
WARMUP_JSON='{"text":"ping","token":"warmup","session_id":"warmup","client_ip":"127.0.0.1"}'
if curl -sS -m 15 -H "Content-Type: application/json" \
  -d "${WARMUP_JSON}" "http://127.0.0.1:${PORT}/query" >/dev/null; then
  log "[INFO] Warm-up query sent."
else
  log "[WARN] Warm-up query failed (continuing)."
fi

# ---- wait forever on the srun uvicorn step ----
wait "${UV_SRUN_PID}"
