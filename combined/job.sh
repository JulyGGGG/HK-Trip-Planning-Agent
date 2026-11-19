#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${TACC_WORKDIR:?Submit through tcloud}"
: "${TACC_USERDIR:?Submit through tcloud}"
: "${SLURM_JOB_ID:?Submit through tcloud}"
cd "$TACC_WORKDIR"
if [[ -f ./launch-profile.sh ]]; then source ./launch-profile.sh; fi
exec /usr/bin/python3 -u "$TACC_WORKDIR/supervisor.py"
