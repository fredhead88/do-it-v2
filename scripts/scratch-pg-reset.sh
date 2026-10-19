#!/bin/bash
# scratch-pg-reset.sh <scratch-dir> — clear the governed roles' leftover ownership on a LOCAL private-pg cluster.
# (L-spec-0682 R3.) A leftover scratch database (e.g. as_l0447_parity) that holds privileges on portal_reader,
# anon, authenticated or service_role leaves pg_shdepend entries that make the next scratch suite refuse to
# drop or recreate those roles. This drops every such leftover database, and, in the cluster's primary
# `scratch` database, drops the governed roles' owned privileges instead (the database itself stays).
# A database with no governed-role dependency is never touched.
#
# Refuses (exit 2, no psql call made) when SUPABASE_DB_URL is set, or when <scratch-dir> is not a unix-socket
# cluster under the scratch root (src/scratch.py's root()). Never reads or prints a credential.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
refuse() { echo "scratch-pg-reset: refused: $*" >&2; exit 2; }
D="${1:-}"; [ -n "$D" ] || refuse "usage: scratch-pg-reset.sh <scratch-dir>"
[ -z "${SUPABASE_DB_URL:-}" ] || refuse "SUPABASE_DB_URL is set — it may name a real database; unset it"
ROOT="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import scratch; print(scratch.root())' "$HERE/../src" 2>/dev/null)" \
  || refuse "no valid scratch root (src/scratch.py root() refused)"
[ -d "$D" ] || refuse "$D is not a directory"
REAL="$(cd "$D" && pwd -P)"
case "$REAL" in "$ROOT"/*) ;; *) refuse "$REAL is not under the scratch root $ROOT" ;; esac
[ -f "$REAL/data/PG_VERSION" ] || refuse "$REAL holds no private-pg cluster (no data/PG_VERSION)"
SOCK=""; for s in "$REAL"/.s.PGSQL.*; do [ -S "$s" ] && SOCK="$s"; done
[ -n "$SOCK" ] || refuse "$REAL has no unix socket — not a running socket-only cluster"
PORT="${SOCK##*.}"
B=/usr/lib/postgresql/17/bin
export PGHOST="$REAL" PGPORT="$PORT" PGUSER=albert PGCONNECT_TIMEOUT=10
unset PGDATABASE PGSERVICE PGPASSWORD PGPASSFILE DATABASE_URL
ROLES="'portal_reader','anon','authenticated','service_role'"
q() { "$B/psql" -X -q -At -v ON_ERROR_STOP=1 -d "$1" -c "$2"; }
mapfile -t DBS < <(q postgres "SELECT DISTINCT d.datname FROM pg_shdepend s JOIN pg_database d ON d.oid = s.dbid JOIN pg_roles r ON r.oid = s.refobjid WHERE s.refclassid = 'pg_authid'::regclass AND r.rolname IN ($ROLES) AND d.datname NOT IN ('postgres','template0','template1') ORDER BY 1")
for db in "${DBS[@]}"; do
  [ -n "$db" ] || continue
  if [ "$db" = "scratch" ]; then
    q scratch "DO \$\$ DECLARE r text; BEGIN FOREACH r IN ARRAY ARRAY[$ROLES] LOOP IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN EXECUTE format('REASSIGN OWNED BY %I TO albert', r); EXECUTE format('DROP OWNED BY %I', r); END IF; END LOOP; END \$\$" >/dev/null
    echo "scratch-pg-reset: cleared governed-role privileges in scratch"
  else
    echo 'DROP DATABASE :"db" WITH (FORCE);' | "$B/psql" -X -q -v ON_ERROR_STOP=1 -v "db=$db" -d postgres >/dev/null
    echo "scratch-pg-reset: dropped leftover database $db"
  fi
done
echo "scratch-pg-reset: ${#DBS[@]} leftover database(s) handled"
