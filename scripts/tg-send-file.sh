#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 1 ]; then printf '000\n'; exit 2; fi
path=$1; shift
exec "$(dirname -- "${BASH_SOURCE[0]}")/tg-send.sh" --document "$path" "${1:-}"
