#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${TACC_WORKDIR:?Submit through tcloud}"
: "${TACC_USERDIR:?Submit through tcloud}"
: "${SLURM_JOB_ID:?Submit through tcloud}"
cd "$TACC_WORKDIR"
session="${TACC_PROJECT_SESSION:-hk-trip-planner}"
jobs="$TACC_USERDIR/$session/jobs"
mkdir -p "$jobs"
if [[ "${TACC_PROJECT_WORKFLOW:-}" != hk_trip ]]; then
    printf '{"job_id":"%s","kind":"workflow"}\n' "$SLURM_JOB_ID" > "$jobs/$SLURM_JOB_ID.json"
fi
/usr/bin/python3 native_diagnostics.py
source "${TACC_COURSE_ROOT:-/mnt/home/public/course-envs/CSIT6000V}/project/activate.sh"
cleanup_runtime() {
    local runtime_path
    runtime_path=$(readlink -f -- "${QWEN_RUN_DIR:-/nonexistent}")
    case "$runtime_path" in
        /tmp/*|"$TACC_USERDIR"/*)
            if [ -d "$runtime_path/rootfs" ]; then rm -rf -- "$runtime_path"; fi ;;
    esac
}
trap cleanup_runtime EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
export CUDA_VISIBLE_DEVICES=""
export TOKENIZERS_PARALLELISM=false
export STORM_ENV_ROOT="$TACC_USERDIR/agent-envs/storm-e2e-v1"
export QWEN_CACHE_HOME="$STORM_ENV_ROOT/runtime-cache"
test -f "$STORM_ENV_ROOT/ready.json"
test -x "$STORM_ENV_ROOT/venv/bin/python"
export TACC_PROJECT_RUNS="${TACC_PROJECT_RUNS:-$session/runs}"
qwen-run "$STORM_ENV_ROOT/venv/bin/python" -u trip_job.py --request request.json --model-config model-config.json
