#!/usr/bin/env bash
# Bootstrap DO-IT on this machine. Idempotent — safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${DOIT_ROOT:-$HOME/.do-it}"

# git merge-tree --write-tree arrived in 2.38. The merge guard is the reason this
# is checked at install rather than discovered at a merge (D113).
need=2.38
have=$(git --version | awk '{print $3}')
if [ "$(printf '%s\n%s\n' "$need" "$have" | sort -V | head -1)" != "$need" ]; then
  echo "ERROR: git $have found, $need or newer required (git merge-tree --write-tree)." >&2
  exit 1
fi
command -v python3 >/dev/null || { echo "ERROR: python3 not found." >&2; exit 1; }
echo "git:     $have"

mkdir -p "$ROOT/events" "$ROOT/content"
echo "ledger:  $ROOT"

# The checks run BEFORE anything is put on PATH. An install that ships a red
# suite is how a guard becomes a guard that is not running.
( cd "$HERE/src" && python3 test_fold.py && python3 test_merge_gate.py && python3 test_dispatch.py && python3 test_tick.py )

BIN="${DOIT_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sf "$HERE/doit" "$BIN/doit"
echo "command: $BIN/doit -> $HERE/doit"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. Add:  export PATH=\"$BIN:\$PATH\"" ;;
esac

# The ten contracts are agent files. `--agent <name>` resolves them from
# ~/.claude/agents/ whatever the cwd (D116); symlinked so the repo stays the
# source of truth (§9.5).
AGENTS="$HOME/.claude/agents"
mkdir -p "$AGENTS"
for f in "$HERE"/agents/*.md; do ln -sf "$f" "$AGENTS/$(basename "$f")"; done
echo "agents:  $AGENTS/<name>.md -> $HERE/agents/"

if ! command -v restic >/dev/null 2>&1; then
  echo "NOTE: restic not installed and DOIT_RESTIC_REPO unset, so the ledger has"
  echo "      no copy. The board will say so on every render until you fix it."
fi
echo
echo "next:  doit            # render the board"
echo "       doit help"
