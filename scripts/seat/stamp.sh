#!/usr/bin/env bash
# stamp.sh <spawn> <model> <session> <tool_uses> <duration_ms> <subagent_tokens> — the pane's completion stamp (S13)
set -euo pipefail
R="${DOIT_ROOT:-$HOME/.do-it}"
[ -f "$R/seat/$1.output.json" ] || { echo "no output for $1" >&2; exit 1; }
/home/albert/do-it-v2/doit validate "$(echo "$1" | sed -E 's/^L-(.*)-[0-9]+$/\1/')" "$R/seat/$1.output.json" >/dev/null
printf '{"model":"%s","session":"%s","turns":%s,"duration_ms":%s,"usage":{"subagent_tokens":%s},"served_as":"general-purpose+contract"}\n' "$2" "$3" "$4" "$5" "$6" > "$R/seat/$1.meta.json"
echo "stamped $1"; sleep 4; tail -2 "$R/events/$1.jsonl" | cut -c1-220
