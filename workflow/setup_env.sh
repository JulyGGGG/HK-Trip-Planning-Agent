#!/usr/bin/env bash
# Run only inside a tcloud CPU job. Never installs into the public environment.
set -euo pipefail
umask 077
: "${TACC_WORKDIR:?Submit through tcloud}"
: "${TACC_USERDIR:?Submit through tcloud}"
: "${SLURM_JOB_ID:?Submit through tcloud}"
cd "$TACC_WORKDIR"
jobs="$TACC_USERDIR/${TACC_PROJECT_SESSION:-hk-trip-planner}/jobs"
mkdir -p "$jobs"
printf '{"job_id":"%s","kind":"setup"}\n' "$SLURM_JOB_ID" > "$jobs/$SLURM_JOB_ID.json"
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
env_root="$TACC_USERDIR/agent-envs/storm-e2e-v1"
mkdir -p "$env_root"
# Keep setup and application jobs from observing a half-installed prefix.
exec 9>"$env_root/setup.lock"
flock -w 1200 9
export STORM_ENV_ROOT="$env_root"
export QWEN_CACHE_HOME="$env_root/runtime-cache"
if [ -f "$env_root/ready.json" ]; then
    if qwen-run "$env_root/venv/bin/python" -u "$TACC_WORKDIR/verify_env.py"; then
        if [[ "${TACC_PROJECT_WORKFLOW:-}" == hk_trip ]]; then
            qwen-run "$env_root/venv/bin/python" -c 'from knowledge_storm.interface import Retriever; import trip_job'
        fi
        printf 'STORM_ENV_READY=%s\n' "$env_root"
        exit 0
    fi
    rm -f -- "$env_root/ready.json"
fi
test -f "$TACC_WORKDIR/storm-src/setup.py"
test -f "$TACC_WORKDIR/storm-src/knowledge_storm/lm.py"
if [ ! -x "$env_root/venv/bin/python" ]; then
    qwen-python -m venv --system-site-packages --copies "$env_root/venv"
fi
py="$env_root/venv/bin/python"
# A nested venv does not necessarily inherit the parent venv's packages.
# Append the read-only course site-packages after this client's own packages.
public_site=$(qwen-python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
private_site=$(qwen-run "$py" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
case "$public_site" in "$QWEN_PROJECT_ROOT"/vllm-env/*/site-packages) ;; *) echo 'Unexpected public package path' >&2; exit 1 ;; esac
case "$private_site" in "$env_root"/venv/*/site-packages) ;; *) echo 'Unexpected private package path' >&2; exit 1 ;; esac
printf '%s\n' "$public_site" > "$private_site/course-public.pth"
qwen-run "$py" -c 'import os, torch; assert torch.__file__.startswith(os.environ["QWEN_PROJECT_ROOT"] + "/vllm-env/"); print("READ_ONLY_TORCH=" + torch.__file__)'
wheelhouse="${STORM_WHEELHOUSE:-$TACC_WORKDIR/wheelhouse}"
if [[ -n "${STORM_WHEELHOUSE:-}" || -d "$wheelhouse" ]]; then
    test -d "$wheelhouse"
    pip_args=(--no-index --find-links "$wheelhouse")
else
    pip_args=(--index-url "${STORM_PIP_INDEX_URL:-https://pypi.org/simple}")
fi
qwen-run "$py" -m pip install --disable-pip-version-check --timeout 60 --retries 2 \
    "${pip_args[@]}" -r "$TACC_WORKDIR/requirements-client.txt"
qwen-run "$py" -m pip install --no-deps --no-build-isolation "$TACC_WORKDIR/storm-src"
qwen-run "$py" -m pip freeze > "$env_root/requirements-resolved.txt"
qwen-run "$py" -u "$TACC_WORKDIR/verify_env.py" --prepare-embedding
if [[ "${TACC_PROJECT_WORKFLOW:-}" == hk_trip ]]; then
    qwen-run "$py" -c 'from knowledge_storm.interface import Retriever; import trip_job'
fi
printf 'STORM_ENV_READY=%s\n' "$env_root"
