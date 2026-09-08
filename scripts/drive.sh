#!/usr/bin/env bash
# drive — build DO-IT v2 to completion unattended: one fresh session per Next-Steps
# item in the handoff, the same shape as the tick (D117) applied to building the
# system itself. The handoff is the only state; a step that does not commit did not
# happen; a red suite or a dirty tree stops the loop. `touch $DOIT_ROOT/drive.stop`
# stops it between steps.
#
#   scripts/drive.sh                    run until Next Steps is empty or a step blocks
#   DRIVE_STEP_USD=10 DRIVE_MODEL=claude-opus-5 DRIVE_MAX_STEPS=24   (defaults; USD is the
#   CLI's list-price size cap — the seat is not metered in dollars, D116)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
H=docs/handoffs/prompt-tier-contracts.md
R="${DOIT_ROOT:-$HOME/.do-it}"
mkdir -p "$R/logs"
LOG="$R/logs/drive.log"
MAX=${DRIVE_MAX_STEPS:-24}
USD=${DRIVE_STEP_USD:-10}
MODEL=${DRIVE_MODEL:-claude-opus-5}
# §10.5's RETIRE list, denied by name in every DO-IT driver context (D119).
RETIRE="Skill(subagent-driven-development),Skill(executing-plans),Skill(writing-plans),Skill(requesting-code-review),Skill(receiving-code-review),Skill(finishing-a-development-branch),Skill(dispatching-parallel-agents)"
say() { echo "$(date -u +%FT%TZ) drive: $*" | tee -a "$LOG"; }

for i in $(seq 1 "$MAX"); do
  [ -e "$R/drive.stop" ] && { say "stop file present; exiting"; exit 0; }
  if grep -qE '^- \[!\]' "$H"; then say "BLOCKED on the operator: $(grep -m1 -E '^- \[!\]' "$H")"; exit 2; fi
  next=$(grep -m1 -E '^- \[[ ~]\]' "$H" || true)
  [ -z "$next" ] && { say "Next Steps is empty — done"; exit 0; }
  git status --porcelain | grep -q . && { say "tree dirty before step $i; refusing to start"; exit 3; }
  before=$(git rev-parse HEAD)
  say "step $i: $next"
  out=$(env -u ANTHROPIC_API_KEY claude -p --model "$MODEL" --dangerously-skip-permissions \
        --strict-mcp-config --disallowedTools "$RETIRE" --max-budget-usd "$USD" \
        --output-format json < docs/handoffs/driver-prompt.md 2>>"$LOG")
  say "step $i returned: $(printf '%s' "$out" | python3 -c 'import sys,json
d=json.load(sys.stdin); print("cost", d.get("total_cost_usd"), "is_error", d.get("is_error"), "turns", d.get("num_turns"))' 2>/dev/null || echo 'no JSON')"
  # The seat window ran dry (D94): that is a clock, not a failure. Wait for it.
  if printf '%s' "$out" | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get("is_error") and any(w in str(d.get("result","")).lower() for w in ("usage limit","rate limit","resets","overloaded")) else 1)' 2>/dev/null; then
    say "seat window exhausted after step $i; sleeping 30m, then retrying the same item"; sleep 1800; continue
  fi
  # The step's claim is checked here, never believed (§4.4).
  if git status --porcelain | grep -q .; then say "step $i left the tree dirty; stopping"; exit 4; fi
  if [ "$(git rev-parse HEAD)" = "$before" ]; then say "step $i committed nothing; stopping"; exit 5; fi
  if ! ./doit test >>"$LOG" 2>&1; then say "step $i left the suite red at $(git rev-parse --short HEAD); stopping (not pushed)"; exit 6; fi
  git push -q 2>>"$LOG" && say "step $i pushed $(git rev-parse --short HEAD)" || say "push failed after step $i; continuing"
done
say "max steps ($MAX) reached"
