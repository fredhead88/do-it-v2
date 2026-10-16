#!/usr/bin/env bash
HERE=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
ROOT=${DOIT_ROOT:-$HOME/.do-it}
umask 077
# flow-snapshot.sh: record which Claude/Codex session each flow pane is running,
# so flow.sh can resume the same sessions after a reboot or crash. Runs every 5
# min from cron and on demand before a planned shutdown. Writes
# ~/.do-it/state/flow-sessions.env and never touches the panes.
set -u
OUT=$ROOT/state/flow-sessions.env
UUID='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
S=${FLOW_SESSION:-flow}
tmux has-session -t "$S" 2>/dev/null || exit 0
"$HERE/remember-thinker-pane.sh" "$S:2.0" || exit $?
mkdir -p -- "$ROOT/state"
sid_of_pane() {  # $1 = target; print the resume id of the claude/codex under that pane
  local pid; pid=$(tmux display -p -t "$1" '#{pane_pid}' 2>/dev/null) || return
  local all="$pid $(pgrep -P "$pid" | tr '\n' ' ')"
  for p in $all $(for c in $all; do pgrep -P "$c"; done); do
    tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -oE "resume $UUID" | head -1 | cut -d' ' -f2
  done | head -1
}
T=$(sid_of_pane "$S":2.0); E=$(sid_of_pane "$S":2.3); C=$(sid_of_pane "$S":1.0)
[ -f "$OUT" ] && . "$OUT"
{
  echo "# written $(date -u +%FT%TZ) by flow-snapshot.sh"
  echo "THINKER_ID=${T:-${THINKER_ID:-}}"
  echo "EXECUTOR_ID=${E:-${EXECUTOR_ID:-}}"
  echo "CODEX_ID=${C:-${CODEX_ID:-}}"
} > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
