#!/usr/bin/env bash
# Run inside the job namespace after provisioning a fresh project venv.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workers=$(python3 "$HERE/cpu-budget.py")
export CI_WORKERS="$workers" PYTEST_XDIST_AUTO_NUM_WORKERS="$workers"
# A project interpreter is explicit; bare python3 remains the clean system one.
exec "${CI_PROJECT_PYTHON:-/work/.venv/bin/python}" -m pytest -n "$workers" "$@"
