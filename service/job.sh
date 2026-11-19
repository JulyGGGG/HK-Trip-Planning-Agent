#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${TACC_WORKDIR:?Submit with tcloud submit}"
: "${TACC_USERDIR:?Submit with tcloud submit}"
: "${SLURM_JOB_ID:?Submit with tcloud submit}"
: "${CONDA_PREFIX:?SDK must activate the course management environment}"
course_root="${TACC_COURSE_ROOT:-/mnt/home/public/course-envs/CSIT6000V}"
expected_prefix="${STORM_BOOTSTRAP_PREFIX:-$course_root/assignment1/mpi-gemm-v1}"
test "$CONDA_PREFIX" = "$expected_prefix"
printf 'SDK_BOOTSTRAP=%s\n' "$CONDA_PREFIX"
printf 'SERVICE_JOB_ID=%s\n' "$SLURM_JOB_ID"
command -v nvidia-smi >/dev/null && nvidia-smi -L || true
scontrol show job "$SLURM_JOB_ID" || true
/usr/bin/python3 "$TACC_WORKDIR/accounting.py" &
ACCOUNTING_PID=$!
source "$course_root/project/activate.sh"
trap 'kill "$ACCOUNTING_PID" 2>/dev/null || true; rm -rf -- "$QWEN_RUN_DIR"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
export QWEN_CACHE_HOME="$TACC_USERDIR/CSIT6000V-project-cache"
mkdir -p -m 700 "$QWEN_CACHE_HOME"
hostname
printf 'SERVICE_BASE_URL=http://%s:8000/v1\n' "$(hostname)"
bash "$QWEN_PROJECT_ROOT/serve.sh"
