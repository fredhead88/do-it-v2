#!/bin/bash
# preflight.sh <base-sha> L-spec-N [L-spec-N...]
# Cheap local checks through the shared 180-second sweep (doit preflight): stack,
# static, guards, changed tests. The full CI rehearsal still gates the exact merged tree.
# Report ($PREFLIGHT_OUT/rehearse-<tag>.txt): NO-NEW-FAILURES | NEW-FAILURES: <class/detail> | NEW-FAILURES:
# RUN-BROKEN: <reason>, then DONE. The guard inventory is lane/guard-tests.txt, read here and by the sweep.
set -u; export LC_COLLATE=C LANG=C.UTF-8
[ $# -ge 2 ] || { echo "usage: preflight.sh <base-sha> L-spec-N [L-spec-N...]" >&2; exit 1; }
BASE=$1; shift; SPECS=("$@")
L=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DOIT=${PREFLIGHT_DOIT:-$L/../doit}
PROJECT=${PREFLIGHT_PROJECT:-albert-scott}
GUARD_FILE=${PREFLIGHT_GUARD_FILE:-$L/guard-tests.txt}
OUTDIR=${PREFLIGHT_OUT:-/var/tmp/push}
WTPARENT=${PREFLIGHT_WT:-/var/tmp}
# Hard outer bound; the sweep holds itself to 180 seconds and reports undetermined first.
OUTER=${PREFLIGHT_OUTER_TIMEOUT:-210}
# Inspect variable names only; never inspect or print database connection values.
for preflight_env in $(compgen -e); do
  case "$preflight_env" in
    PG*|SUPABASE_DB_URL*|AS_LOCAL_PG_*|DATA_MCP_DSN*|DATABASE_URL|GITHUB_ENV|GITHUB_PATH|PYTEST_ADDOPTS|PYTEST_PLUGINS|PYTHONPATH) unset "$preflight_env" ;;
  esac
done
export SUPABASE_DB_URL='' SUPABASE_DB_URL_DIRECT='' SUPABASE_DB_URL_RO=''
export SUPABASE_DB_URL_MIGRATIONS='' DATABASE_URL=''
[ -z "${PREFLIGHT_PY:-}" ] || export PATH="$(dirname "$PREFLIGHT_PY"):$PATH"
TAG=$(printf "%s\n" "${SPECS[@]}" | sed "s/L-spec-//" | paste -sd-)
mkdir -p "$OUTDIR" "$WTPARENT"
OUT=$OUTDIR/rehearse-$TAG.txt
: > "$OUT"; log(){ echo "$*" | tee -a "$OUT"; }
ACTIVE=""
cleanup(){
  if [ -n "$ACTIVE" ]; then
    kill -TERM "$ACTIVE" 2>/dev/null
    wait "$ACTIVE" 2>/dev/null
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
clean(){ tr -cd '[:print:]' <<<"$1" | cut -c1-300; }

[ -r "$GUARD_FILE" ] || { log "NEW-FAILURES:"; log "RUN-BROKEN: guard inventory unreadable"; log DONE; exit 0; }
guards=$(grep -cvE '^[[:space:]]*(#|$)' "$GUARD_FILE")
log "guards: ${guards:-0} from $GUARD_FILE"
export DOIT_PREFLIGHT_GUARDS="$GUARD_FILE" TMPDIR="$WTPARENT"
csv=$(IFS=,; echo "${SPECS[*]}")
# One sweep, one test pass: no second local pass, no attribution, no hosted workflow.
timeout -k 5 "$OUTER" "$DOIT" preflight --project "$PROJECT" --on "$BASE" --specs "$csv" > "$OUT.sweep" 2>&1 & ACTIVE=$!
wait "$ACTIVE"; rc=$?
ACTIVE=""
while IFS= read -r line; do log "$(clean "$line")"; done < <(grep -E '^PREFLIGHT-(RECHAINED|GUARDS|TARGETS|CLEAN) ' "$OUT.sweep")
case "$rc" in
  0) log NO-NEW-FAILURES ;;
  1)
    red=$(grep -m1 '^PREFLIGHT-RED ' "$OUT.sweep")
    read -r _ stage owner cls detail <<<"$red"
    case "$cls" in
      content-conflict|declined) log "CONFLICT $(clean "$owner")" ;;
      overlap|writes|pg-marker) log "OVERLAP $(clean "$owner"): $(clean "$detail")" ;;
      alembic-heads) log "ALEMBIC-HEADS $(clean "$owner"): $(clean "$detail")" ;;
    esac
    log "NEW-FAILURES:"; log "$(clean "${red:-PREFLIGHT-RED unknown}")" ;;
  *)
    why=$(grep -m1 '^PREFLIGHT-UNDETERMINED ' "$OUT.sweep")
    case "$rc" in 124|137) why="PREFLIGHT-UNDETERMINED timeout sweep exceeded its bound" ;; esac
    log "NEW-FAILURES:"; log "RUN-BROKEN: $(clean "${why:-PREFLIGHT-UNDETERMINED no verdict (exit $rc)}")" ;;
esac
log DONE
exit 0
