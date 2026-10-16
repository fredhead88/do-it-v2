#!/usr/bin/env bash
# Persist only a proven live pane. Called on flow creation, attach and snapshot.
set -euo pipefail
ROOT=${DOIT_ROOT:-$HOME/.do-it}
target=${1:-${FLOW_SESSION:-flow}:2.0}
pane=$(tmux display -p -t "$target" '#{pane_id}' 2>/dev/null) || exit 0
[[ "$pane" =~ ^%[0-9]+$ ]] || exit 0
dead=$(tmux display -p -t "$pane" '#{pane_dead}' 2>/dev/null) || exit 0
[ "$dead" = 0 ] || exit 0
mkdir -p -- "$ROOT/state"
temporary=$(mktemp "$ROOT/state/.thinker-pane.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' "$pane" > "$temporary"
mv -f -- "$temporary" "$ROOT/state/thinker-pane"
