#!/usr/bin/env bash
# scripts/seat/claim.sh <spawn> — R6/L-spec-0189: the ONLY writer of a seat's
# claim file. O_EXCL semantics (bash's `noclobber`), so two servers reading the
# same board can never both claim the same packet: the first exits 0 and leaves
# `<spawn>.claimed` on disk; every later call, for as long as that file exists,
# exits non-zero and touches nothing. `dispatch.py` itself never writes this file
# (ADR-0028-2: "the wrapper never creates it") — this script is the whole
# mechanism. Run it BEFORE anything else in the serving pattern
# (scripts/seat/README.md, agents/relay.md) — a claim run
# after work has already started defeats the point of the claim.
set -euo pipefail

R="${DOIT_ROOT:-$HOME/.do-it}"
spawn="${1:?usage: claim.sh <spawn>}"
if [[ ! "$spawn" =~ ^L-[a-z-]+-[0-9]+$ ]]; then
    echo "claim.sh: invalid spawn id" >&2
    exit 1
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
seat="$R/seat"
packet="$seat/$spawn.packet.md"
claimed="$seat/$spawn.claimed"

if [[ ! -f "$packet" ]]; then
    echo "claim.sh: no packet for $spawn at $packet — nothing to claim" >&2
    exit 1
fi

mkdir -p "$seat"
if ( set -o noclobber; : > "$claimed" ) 2>/dev/null; then
    if [[ "${DOIT_LEDGER_FILE:-}" =~ ^L-relay-[0-9]+\.jsonl$ ]]; then
        "$HERE/../../doit" append seat-claimed "$spawn" "spawn=$spawn" server=relay "ts=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))')" ||
            echo "claim.sh: seat-claimed append failed" >&2
    fi
    exit 0
fi
echo "claim.sh: $spawn is already claimed ($claimed exists) — someone else has this seat" >&2
exit 1
