#!/usr/bin/env bash
HERE=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
ROOT=${DOIT_ROOT:-$HOME/.do-it}
umask 077
# flow.sh: build (or re-attach) the DO-IT v2 'flow' tmux session on Albert-Scott-Cloud.
#   window 0 "peacock":  spare shell
#   window 1 "codex":    Codex (resumes CODEX_ID), in ~/.do-it/worktrees/codex-tech-audit
#   window 2 "do-it":    Thinker (2.0) | Planner `doit up` (2.1) / Relay `doit relay` (2.2) | Executor (2.3)
# Session ids come from ~/.do-it/state/flow-sessions.env, which flow-snapshot.sh rewrites
# every 5 min, so after a crash or reboot every role resumes where it was.
#   ./flow.sh              build if missing, then attach
#   ./flow.sh --no-attach  build if missing, don't attach (used by the @reboot cron)
# The crons address these panes as flow:2.0 / 2.2 / 2.3, so keep this layout.
# The old v1 cockpit layout is saved as ~/flow-v1.sh.bak. Don't use it.
S=${FLOW_SESSION:-flow}
ATTACH=1; [ "${1:-}" = "--no-attach" ] && ATTACH=0
if ! tmux has-session -t "$S" 2>/dev/null; then
  . "$ROOT/state/flow-sessions.env" 2>/dev/null || true
  CL=${FLOW_CL:-"/home/albert/.local/bin/claude --dangerously-skip-permissions"}
  RESUME_T=${THINKER_ID:+--resume $THINKER_ID}
  RESUME_E=${EXECUTOR_ID:+--resume $EXECUTOR_ID}
  tmux new-session -d -s "$S" -n peacock -c /opt/albert-scott
  tmux new-window -t "$S:1" -n codex -c "$HOME/.do-it/worktrees/codex-tech-audit"
  if [ -n "${CODEX_ID:-}" ]; then
    tmux send-keys -t "$S:1.0" "codex resume $CODEX_ID -m gpt-6.1-sol --dangerously-bypass-approvals-and-sandbox" Enter
  fi
  tmux new-window -t "$S:2" -n do-it -c /opt/albert-scott "bash -lc '$CL $RESUME_T; exec bash'"
  tmux split-window -h -t "$S:2.0" -c /home/albert/do-it-v2 "bash -lc '/home/albert/do-it-v2/doit up; exec bash'"
  tmux split-window -h -t "$S:2.1" -c /opt/albert-scott "bash -lc '$CL $RESUME_E; exec bash'"
  tmux split-window -v -t "$S:2.1" -c /home/albert/do-it-v2 "bash -lc '/home/albert/do-it-v2/doit relay; exec bash'"
  W=$(tmux display -p -t "$S:2" '#{window_width}'); C=$(( (W-2)/3 ))
  tmux resize-pane -t "$S:2.0" -x "$C"; tmux resize-pane -t "$S:2.1" -x "$C"
  tmux set -g mouse on
  tmux select-window -t "$S:2"; tmux select-pane -t "$S:2.0"
fi
# Keep pane identity and no-prefix navigation durable across reattachment.
"$HERE/remember-thinker-pane.sh" "$S:2.0" || exit $?
tmux bind-key -n S-Right select-pane -t :.+
tmux bind-key -n S-Left select-pane -t :.-
[ "$ATTACH" = 1 ] && exec tmux attach -d -t "$S"
exit 0
