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
# Optional admin endpoint: a role-clean, authenticated loopback cluster.
if [ "${2:-}" = loopback ]; then
  PW=$(/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(16))')
  PWFILE=$(mktemp "$D/password.XXXXXX")
  admin_cleanup() {
    rm -f "$PWFILE"
    if [ "${ADMIN_STARTED:-0}" != 1 ]; then
      "$B/pg_ctl" -w -D "$D/data" -m fast stop >/dev/null 2>&1 || true
    fi
  }
  trap admin_cleanup EXIT
  printf '%s\n' "$PW" > "$PWFILE"
  "$B/initdb" -D "$D/data" -U albert --auth-local=scram-sha-256 --auth-host=scram-sha-256 --pwfile="$PWFILE" -E UTF8 >/dev/null
  rm -f "$PWFILE"
  # Only the view's socket and IPv4 loopback accept authenticated connections.
  printf 'local all all scram-sha-256\nhost all all 127.0.0.1/32 scram-sha-256\n' > "$D/data/pg_hba.conf"
  USED_PORTS=",${PRIVATE_PG_AVOID_PORT:-},"
  for ATTEMPT in 1 2 3 4 5; do
    while :; do
      PORT=$(( 20000 + RANDOM % 20000 ))
      case "$USED_PORTS" in *,$PORT,*) continue ;; esac
      USED_PORTS="$USED_PORTS$PORT,"
      break
    done
    : > "$D/pg.log"
    if "$B/pg_ctl" -w -t 5 -D "$D/data" -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=$D -c port=$PORT" -l "$D/pg.log" start >/dev/null 2>&1; then
      ADMIN_STARTED=1
      printf "ADMIN_DSN='postgresql://albert:%s@127.0.0.1:%s/postgres'\n" "$PW" "$PORT"
      exit 0
    fi
    "$B/pg_ctl" -w -D "$D/data" -m fast stop >/dev/null 2>&1 || true
    if ! grep -Eq 'could not bind|Address already in use' "$D/pg.log"; then
      echo 'admin cluster failed to start' >&2
      exit 1
    fi
  done
  echo 'admin cluster could not bind after 5 port attempts' >&2
  exit 1
fi
PORT=$(( 20000 + RANDOM % 20000 ))
[ -f "$D/data/PG_VERSION" ] || "$B/initdb" -D "$D/data" -U albert --auth=trust -E UTF8 >/dev/null
"$B/pg_ctl" -w -D "$D/data" -o "-c listen_addresses= -c unix_socket_directories=$D -c port=$PORT" -l "$D/pg.log" start >/dev/null
"$B/createdb" -h "$D" -p $PORT -U albert scratch
DSN="postgresql://albert@/scratch?host=$D&port=$PORT"
echo "export SUPABASE_DB_URL='$DSN' DATABASE_URL='$DSN' PGHOST='$D' PGPORT=$PORT PGUSER=albert PGDATABASE=scratch"
