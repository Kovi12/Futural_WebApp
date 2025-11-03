#!/bin/bash
# Usage: ./slurm_job_dual.sh <llama_dual|deepseek_dual> [target_node_suffix_or_fullname]
set -euo pipefail

MODEL_NAME="${1:-}"
TARGET_NODE="${2:-}"

if [[ -z "$MODEL_NAME" ]]; then
  echo "Usage: $0 <llama_dual|deepseek_dual> [target_node]" >&2
  exit 1
fi
if [[ "$MODEL_NAME" != "llama_dual" && "$MODEL_NAME" != "deepseek_dual" ]]; then
  echo "Error: MODEL_NAME must be one of: llama_dual, deepseek_dual" >&2
  exit 1
fi

BASE_DIR="${WEBAPP_BASE_DIR:-$HOME/Futural_WebApp}"

# Keep sbatch outputs in a stable folder (created immediately by Slurm)
LOG_ROOT="${BASE_DIR}/logs/sbatch/${MODEL_NAME}"
STATUS_DIR="${BASE_DIR}/status"
mkdir -p "${LOG_ROOT}" "${STATUS_DIR}"

JOB_NAME="${MODEL_NAME}"
OUT_FILE="${LOG_ROOT}/${JOB_NAME}_%j.out"
ERR_FILE="${LOG_ROOT}/${JOB_NAME}_%j.err"
ID_FILE="${STATUS_DIR}/${JOB_NAME}.id"

# --- sbatch args (1 GPU, 32G default mem unless you want 40G like before) ---
SBATCH_ARGS=(
  --parsable
  --job-name="${JOB_NAME}"
  --output="${OUT_FILE}"
  --error="${ERR_FILE}"
  --mem=32G
  --partition=dgxa100
  --ntasks=1
  --time=12:00:00
  --gres=gpu:tesla_a100:1
  --signal=B:USR1@300
)

# Optional node pin (accepts "wn04" or full "dgxa100-ncit-wn04")
if [[ -n "$TARGET_NODE" ]]; then
  if [[ "$TARGET_NODE" == dgxa100-ncit-* ]]; then
    SBATCH_ARGS+=(--nodelist="$TARGET_NODE")
  else
    SBATCH_ARGS+=(--nodelist="dgxa100-ncit-${TARGET_NODE}")
  fi
fi

# Submit and pass MODEL_NAME to the runtime script
CMD=(sbatch "${SBATCH_ARGS[@]}" run_model_job_dual.sh "${MODEL_NAME}")
echo "[INFO] Submitting: ${CMD[*]}"

JOB_LINE="$("${CMD[@]}")"          # --parsable returns "<jobid>"
JOB_ID="${JOB_LINE%%.*}"           # strip any suffix just in case

if [[ -n "$JOB_ID" ]]; then
  echo "$JOB_ID" > "$ID_FILE"

  # Create unified dir and repoint stdout/err there *for future lines*
  JOB_DIR="${BASE_DIR}/logs/${MODEL_NAME}/${JOB_ID}"
  mkdir -p "${JOB_DIR}"

  # Move slurm streams into the unified dir
  scontrol update JobId="${JOB_ID}" \
    StdOut="${JOB_DIR}/${MODEL_NAME}.out" \
    StdErr="${JOB_DIR}/${MODEL_NAME}.err" >/dev/null

  echo "[INFO] Job submitted with ID: ${JOB_ID}"
  echo "[INFO] Unified log dir: ${JOB_DIR}"
  echo "[INFO]   stdout -> ${JOB_DIR}/${MODEL_NAME}.out"
  echo "[INFO]   stderr -> ${JOB_DIR}/${MODEL_NAME}.err"
else
  echo "[ERROR] Failed to parse job id. Raw output: ${JOB_LINE}" >&2
  exit 1
fi
