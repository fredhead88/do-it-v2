#!/bin/bash
# executor-wake.sh — overnight stopgap (Thinker L-thinker-0004, 2026-09-26 ~20:55Z, operator asleep).
# Problem: the Executor pane (tmux flow:2.3) goes idle at its prompt when its background `doit wait` ends
# without re-invoking it (measured 19:41Z-20:41Z, 60 min idle with a finished master-fix build waiting).
# The old per-role nudge crons have been PAUSED since 2026-09-14.
# Rule: if the pane shows no running turn on two consecutive runs (5 min apart), type ONE wake message.
# Never kills or restarts anything. Log: ~/.do-it/logs/executor-wake.log. Replaced by L-charter-0036/0038 work.
set -u
R="${DOIT_ROOT:-$HOME/.do-it}"
TODAY="${EXECUTOR_WAKE_TODAY:-$(date -u +%F)}"
PANE=flow:2.3
STATE=$R/state/executor-wake.idle
LOG=$R/logs/executor-wake.log
mkdir -p "$R/state" "$R/logs"
now=$(date -u +%FT%TZ)
tmux has-session -t flow 2>/dev/null || { echo "$now no-tmux" >> "$LOG"; exit 0; }
if [ "$(tmux display -p -t "$PANE" '#{pane_dead}' 2>/dev/null)" != "0" ]; then echo "$now pane-dead-or-missing (babysitter restores)" >> "$LOG"; exit 0; fi

# Dates are read from the headers so expiry cannot drift from documentation.
active() {
  local name="$1" expiry
  expiry=$(awk -v name="$name" '$1=="#" && $2=="STOPGAP" && $3==name {print $5}' "$0")
  if [[ ! "$expiry" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || [[ "$TODAY" > "$expiry" ]]; then
    echo "$now expired $name" >> "$LOG"
    return 1
  fi
}

# --- Relay
# STOPGAP relay-nudge expires 2026-11-26 (relay lifecycle of L-spec-1497)
if active relay-nudge; then
RPANE=$(tmux list-panes -a -F "#{pane_id} #{pane_current_command} #{pane_start_command}" | awk '/doit relay|up.py --relay/{print $1; exit}'); RPANE=${RPANE:-flow:2.2}
SEAT=$R/seat
unclaimed=0
for c in $(find "$SEAT" -maxdepth 1 -name '*.cmd.json' -mmin +1 -mmin -720 2>/dev/null); do
  b=${c%.cmd.json}
  python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get(\"cmd\",[\"\"])[0]==\"codex\" else 1)" "$c" 2>/dev/null && continue  # Codex packets run directly via dispatch; the relay never serves them
  [ -f "$b.claimed" ] || [ -f "$b.output.json" ] || unclaimed=$((unclaimed+1))
done
# Thinker 2026-10-24: only when a Claude relay session (L-relay-NNNN) is showing. Since L-spec-1497 flow:2.2 runs the Python relay loop (doit relay), which starts its own
# Claude relay sessions; typing wake text into it does nothing. The count also included Codex builder packets the relay never serves.
if [ "$unclaimed" -gt 0 ] && [ "$(tmux display -p -t "$RPANE" '#{pane_dead}' 2>/dev/null)" = "0" ] && tmux capture-pane -p -t "$RPANE" | grep -q "L-relay-"; then
  rs=$(tmux capture-pane -p -t "$RPANE" | tail -14)
  if ! echo "$rs" | grep -qE 'esc to interrupt|Compacting|tokens · thinking'; then
    m="Wake (executor-wake.sh, $now): $unclaimed seat packet(s) unclaimed for 5+ min. Re-scan $R/seat now and serve them (graders and reviewers first, then builders, about 5 at a time); do not wait on older background agents before serving new packets."
    "$R/bin/pane-say.sh" "$RPANE" "$m"
    echo "$now relay-woke unclaimed=$unclaimed" >> "$LOG"
  fi
fi
fi

# --- Planner
# STOPGAP planner-idle expires 2026-11-10 (held-planner-end and the pane-watchdog heartbeat)
if active planner-idle; then
PPANE=flow:2.1; PSTATE=$R/state/planner-wake.idle
if [ "$(tmux display -p -t "$PPANE" '#{pane_dead}' 2>/dev/null)" = "0" ]; then
  ps_=$(tmux capture-pane -p -t "$PPANE" | tail -14)
  if echo "$ps_" | grep -qE 'esc to interrupt|Compacting|tokens · thinking|background agent|◯ |· ↓'; then rm -f "$PSTATE"
  elif echo "$ps_" | grep -qE 'done [0-9]+:[0-9]+ (AM|PM)'; then
    # Thinker 2026-10-27: no longer types /exit. It fired while the Planner's own background wait loops ran, which
    # froze the pane on Claude's "Background work is running" dialog (~8 times 10-25..10-27). Planners end via
    # `doit pane-end`; this only logs an idle Planner now.
    if [ -f "$PSTATE" ]; then rm -f "$PSTATE"; echo "$now planner-idle (log only)" >> "$LOG"
    else echo "$now" > "$PSTATE"; fi
  fi
fi
fi

# --- Executor
# STOPGAP executor-wake expires 2026-11-26 (the Executor own doit wait supervision)
if active executor-wake; then
screen=$(tmux capture-pane -p -t "$PANE" | tail -12)
# A running turn shows "esc to interrupt" or a spinner line with "(Ns · ↓"; compaction shows "Compacting".
if echo "$screen" | grep -qE 'esc to interrupt|· ↓|Compacting|tokens · thinking'; then
  rm -f "$STATE"; exit 0
fi
if [ ! -f "$STATE" ]; then echo "$now" > "$STATE"; echo "$now idle-1" >> "$LOG"; exit 0; fi
msg="Wake (executor-wake.sh, $now): you have been idle at the prompt for 5+ min. Re-read the ledger now (doit board) and take the next durable action on every actionable lane item: grade/review/merge finished builds first, then dispatch builders only for shelf specs whose earlier-wave siblings are settled (do not try wave-held ones; the gate refuses them), then owed checks. Keep a doit wait running and re-check at least every 5 minutes."
"$R/bin/pane-say.sh" "$PANE" "$msg"  # Thinker 2026-10-24: a single Enter was swallowed as paste; pane-say re-submits
rm -f "$STATE"
echo "$now woke" >> "$LOG"
fi
