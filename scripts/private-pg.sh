#!/bin/bash
# private-pg.sh <dir> — start a throwaway PostgreSQL 17 cluster for live_db tests, socket-only, no network.
# Prints: export lines for SUPABASE_DB_URL / DATABASE_URL pointing at it. Stop with: pg_ctl -D <dir>/data stop
# All prod DSNs must be unset by the caller (unset SUPABASE_DB_URL SUPABASE_DB_URL_DIRECT SUPABASE_DB_URL_RO SUPABASE_DB_URL_MIGRATIONS).
#
# AC15/L-spec-0481: brought into the repo, functionally identical to the
# operator's existing $R/bin/private-pg.sh (same usage line, same export
# output names) — `grading_env` calls THIS copy, by its repo path, never
# `$R/bin`.
set -euo pipefail
D=${1:?usage: private-pg.sh <dir>}; B=/usr/lib/postgresql/17/bin; mkdir -p "$D"; chmod 700 "$D"
PORT=$(( 20000 + RANDOM % 20000 ))
[ -f "$D/data/PG_VERSION" ] || "$B/initdb" -D "$D/data" -U albert --auth=trust -E UTF8 >/dev/null
"$B/pg_ctl" -w -D "$D/data" -o "-c listen_addresses= -c unix_socket_directories=$D -c port=$PORT" -l "$D/pg.log" start >/dev/null
"$B/createdb" -h "$D" -p $PORT -U albert scratch
DSN="postgresql://albert@/scratch?host=$D&port=$PORT"
echo "export SUPABASE_DB_URL='$DSN' DATABASE_URL='$DSN' PGHOST='$D' PGPORT=$PORT PGUSER=albert PGDATABASE=scratch"
