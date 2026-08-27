#!/usr/bin/env bash
# One-way replication of ~/.do-it/ plus the restore drill (§9.9, D85).
#
#   backup.sh push    replicate. One writer, push-only. The fold NEVER reads the
#                     destination — that is a property of this script, not of
#                     anyone's intention, because an automatic restore direction
#                     is bidirectional sync wearing another name.
#   backup.sh drill   restore the latest snapshot to scratch, fold it, and assert
#                     the derived terminal states match the live fold's.
#
# restic is the reference implementation, named as an example, not the contract.
# DOIT_RESTIC_REPO is per-installation: the design states the properties (off the
# machine, push-only, encrypted, incremental, verifiable) and never the host.
set -euo pipefail
DOIT_ROOT="${DOIT_ROOT:-$HOME/.do-it}"
HERE="$(cd "$(dirname "$0")" && pwd)"

die() { echo "backup: $*" >&2; exit 1; }
[ -n "${DOIT_RESTIC_REPO:-}" ] || die "DOIT_RESTIC_REPO unset — the ledger is NOT backed up.
  This is loud on purpose: with no destination the drill fails from day one,
  rather than 'we have backups' being a claim nobody ever checked (§9.9)."
export RESTIC_REPOSITORY="$DOIT_RESTIC_REPO"

case "${1:-drill}" in
push)
  restic backup "$DOIT_ROOT" --exclude "$DOIT_ROOT/board.md"   # board is a disposable cache
  ;;
drill)
  scratch="$(mktemp -d)"; trap 'rm -rf "$scratch"' EXIT
  restic restore latest --target "$scratch"
  snap="$(restic snapshots latest --json | python3 -c 'import json,sys;print(json.load(sys.stdin)[-1]["short_id"])')"
  restored="$scratch$DOIT_ROOT"
  [ -d "$restored/events" ] || die "snapshot $snap holds no events/ — the backup is empty"

  # The comparison is §12.2's strangler check pointed at a backup: fold both
  # sides and diff the DERIVED states. Never compare files; files are not state.
  DOIT_ROOT="$restored" python3 "$HERE/fold.py" states > "$scratch/restored.states"
  DOIT_ROOT="$DOIT_ROOT" python3 "$HERE/fold.py" states > "$scratch/live.states"
  # The restored side legitimately LAGS the live one by whatever was appended
  # since the snapshot, so "any difference" is the wrong test — it fails on every
  # normal day and the drill becomes noise. The real failure is a state going
  # BACKWARDS: live ranking below restored means the live ledger lost events the
  # backup still has, which is the disaster this drill exists to catch.
  DOIT_ROOT="$restored" python3 "$HERE/fold.py" states > "$scratch/restored.states"
  DOIT_ROOT="$DOIT_ROOT" python3 "$HERE/fold.py" states > "$scratch/live.states"
  if ! python3 - "$scratch/restored.states" "$scratch/live.states" <<'PY'
import sys
RANK = {"unknown": 0, "open": 0, "written": 1, "L1-complete": 1, "building": 2,
        "graded": 3, "reviewing": 4, "shipped": 5, "shipped-owed-evidence": 6,
        "accepted": 7, "dropped": 7, "L2-complete": 2, "retracted": 2}
load = lambda p: dict(l.split("\t") for l in open(p).read().splitlines())
r, live = load(sys.argv[1]), load(sys.argv[2])
bad = {k: (v, live.get(k, "ABSENT")) for k, v in r.items()
       if RANK.get(live.get(k), -1) < RANK.get(v, 0)}
if not r:
    print("  restored snapshot folds to NO states at all"); sys.exit(1)
for k, (a, b) in bad.items():
    print(f"  {k}: restored={a} live={b}")
sys.exit(1 if bad else 0)
PY
  then die "restore drill FAILED on snapshot $snap — restored states contradict live ones"; fi

  n=$(cat "$restored"/events/*.jsonl 2>/dev/null | grep -c . || true)
  DOIT_LEDGER_FILE=L-drill-local.jsonl python3 "$HERE/fold.py" append \
    restore-verified - "snapshot_id=$snap" "event_count=$n" > /dev/null
  echo "restore drill OK · snapshot $snap · $n events"
  ;;
*) die "usage: backup.sh push|drill" ;;
esac
