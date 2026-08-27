#!/usr/bin/env bash
# Bootstrap DO-IT on this machine. Idempotent — safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${DOIT_ROOT:-$HOME/.do-it}"

mkdir -p "$ROOT/events" "$ROOT/content"
echo "ledger:  $ROOT"

# The checks run BEFORE anything is put on PATH. An install that ships a red
# suite is how a guard becomes a guard that is not running.
( cd "$HERE/src" && python3 test_fold.py && python3 test_merge_gate.py )

BIN="${DOIT_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sf "$HERE/doit" "$BIN/doit"
echo "command: $BIN/doit -> $HERE/doit"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. Add:  export PATH=\"$BIN:\$PATH\"" ;;
esac

if ! command -v restic >/dev/null 2>&1; then
  echo "NOTE: restic not installed and DOIT_RESTIC_REPO unset, so the ledger has"
  echo "      no copy. The board will say so on every render until you fix it."
fi
echo
echo "next:  doit            # render the board"
echo "       doit help"
