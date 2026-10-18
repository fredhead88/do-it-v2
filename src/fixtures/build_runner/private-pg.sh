#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$1/data"
printf 'start %s\n' "$1" >> "$FIXTURE_LOG"
if [ "${FIXTURE_FAIL:-}" = pg ]; then exit 1; fi
if [ "${FIXTURE_FAIL:-}" = base-pg ] && [ -f "$FIXTURE_LOG.base" ]; then exit 1; fi
if [ "${2:-}" = loopback ]; then
  printf "ADMIN_DSN='private-admin-fixture'\n"
  touch "$FIXTURE_LOG.base"
else
  printf "export SUPABASE_DB_URL='private-db-fixture' DATABASE_URL='private-db-fixture' PGHOST='%s' PGPORT=23456 PGUSER=albert PGDATABASE=scratch\n" "$1"
fi
