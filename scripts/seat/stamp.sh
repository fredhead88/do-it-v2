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
spawn="${1:-}"
if [[ ! "$spawn" =~ ^L-[a-z-]+-[0-9]+$ ]]; then
    echo "stamp.sh: invalid spawn id" >&2
    exit 1
fi
[ -f "$R/seat/$spawn.output.json" ] || { echo "no output for $spawn" >&2; exit 1; }
role="${spawn#L-}"
role="${role%-*}"
status=0
"$HERE/../../doit" validate "$role" "$R/seat/$spawn.output.json" >/dev/null 2>&1 || status=$?
case "$status" in
    0) outcome=ok ;;
    1) outcome=invalid-output ;;
    *) outcome=undetermined ;;
esac
stamp_status=0
python3 "$HERE/../../src/usage.py" stamp "$spawn" "$2" "$3" "$4" "$5" "$6" || stamp_status=$?
if [[ "${DOIT_LEDGER_FILE:-}" =~ ^L-relay-[0-9]+\.jsonl$ ]]; then
    "$HERE/../../doit" append seat-finished "$spawn" "spawn=$spawn" "outcome=$outcome" "ts=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))')" ||
        echo "stamp.sh: seat-finished append failed" >&2
fi
if [[ "$status" -ne 0 ]]; then
    exit "$status"
fi
exit "$stamp_status"
