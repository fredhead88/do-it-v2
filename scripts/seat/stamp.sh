#!/usr/bin/env bash
# stamp.sh <spawn> <model> <session> <turns> <duration_ms> <subagent_tokens> — the
# pane's completion stamp (S13). Validates seat/<spawn>.output.json, then calls
# src/usage.py to resolve <session>'s OWN sub-agent transcript
# (~/.claude/projects/*/*/subagents/agent-<id>.jsonl, newest match) and write
# <spawn>.meta.json: the four-way token split (deduped by message id) alongside
# the hand-supplied blended <subagent_tokens>, plus model_observed — the model
# the transcript's assistant lines actually ran on, not the <model> arg above
# (which is only the pane operator's typed claim). No transcript resolves -> the
# four fields stay null (unmeasured is never zero) and this warns on stderr, but
# still stamps: a missing transcript must not block the completion signal.
# <turns> may now be the literal "-" to mean "omitted": usage.py then derives it
# from the transcript's own distinct-assistant-id count (itself possibly null).
# A supplied <turns> that differs from that derived count by more than 10% is
# overridden with the derived value, and the override is recorded on the meta
# as turns_supplied/stamp_corrected: "turns". <duration_ms> and
# <subagent_tokens> stay exactly as documented above — hand-typed, required,
# never compared against anything, never corrected.
set -euo pipefail
R="${DOIT_ROOT:-$HOME/.do-it}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$R/seat/$1.output.json" ] || { echo "no output for $1" >&2; exit 1; }
/home/albert/do-it-v2/doit validate "$(echo "$1" | sed -E 's/^L-(.*)-[0-9]+$/\1/')" "$R/seat/$1.output.json" >/dev/null
python3 "$HERE/../../src/usage.py" stamp "$1" "$2" "$3" "$4" "$5" "$6"
echo "stamped $1"; sleep 4; tail -2 "$R/events/$1.jsonl" | cut -c1-220
