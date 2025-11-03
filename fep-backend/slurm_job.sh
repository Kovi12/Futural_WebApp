#!/bin/bash
# Usage: ./slurm_job.sh <model_name> [target_node_suffix_or_fullname]
set -euo pipefail

MODEL_NAME="${1:-}"
TARGET_NODE="${2:-}"

if [[ -z "$MODEL_NAME" ]]; then
  echo "Usage: $0 <model_name> [target_node]" >&2
  exit 1
fi

BASE_DIR="${WEBAPP_BASE_DIR:-$HOME/Futural_WebApp}"

# Keep sbatch outputs in a stable folder that always exists
LOG_ROOT="${BASE_DIR}/logs/sbatch/${MODEL_NAME}"
STATUS_DIR="${BASE_DIR}/status"
mkdir -p "${LOG_ROOT}" "${STATUS_DIR}"

JOB_NAME="${MODEL_NAME}"
OUT_FILE="${LOG_ROOT}/${JOB_NAME}_%j.out"
ERR_FILE="${LOG_ROOT}/${JOB_NAME}_%j.err"
ID_FILE="${STATUS_DIR}/${JOB_NAME}.id"

SBATCH_ARGS=(
  --parsable
  --job-name="${JOB_NAME}"
  --output="${OUT_FILE}"
  --error="${ERR_FILE}"
  --mem=40G
  --partition=dgxa100
  --ntasks=1
  --time=12:00:00
  --gres=gpu:tesla_a100:1
  --signal=B:USR1@300
)

# Optional node pin
if [[ -n "$TARGET_NODE" ]]; then
  if [[ "$TARGET_NODE" == dgxa100-ncit-* ]]; then
    SBATCH_ARGS+=(--nodelist="$TARGET_NODE")
  else
    SBATCH_ARGS+=(--nodelist="dgxa100-ncit-${TARGET_NODE}")
  fi
fi

CMD=(sbatch "${SBATCH_ARGS[@]}" run_model_job.sh "${MODEL_NAME}")
echo "[INFO] Submitting: ${CMD[*]}"

JOB_LINE="$("${CMD[@]}")"
JOB_ID="${JOB_LINE%%.*}"

if [[ -n "$JOB_ID" ]]; then
  echo "$JOB_ID" > "$ID_FILE"

  # --- NEW: unify logs under logs/<model>/<job_id>/ ---
  JOB_DIR="${BASE_DIR}/logs/${MODEL_NAME}/${JOB_ID}"
  mkdir -p "${JOB_DIR}"

  # move stdout/stderr to unified dir (takes effect for subsequent lines)
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
