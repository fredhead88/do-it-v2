#!/usr/bin/env bash
# Retired (L-spec-0186, ADR-0028-6): gutted in place, never deleted — a
# `Writes:` grant is not a licence to delete outside it, and a file removal is
# a `rework` trigger at the merge gate. Superseded by src/backup.py (push,
# watch, status, cron) and src/restore.py (the restore drill and board_diff).
set -euo pipefail
echo "backup.sh: retired — use 'doit backup' (src/backup.py) and 'doit restore' (src/restore.py)" >&2
exit 1
