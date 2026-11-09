#!/bin/bash
# redalone.sh — remember specs that were red when rehearsed alone, so the lane does not re-pick them
# until their branch tip changes (a new build). Sourced by rloop.sh; also a CLI:
#   lane/redalone.sh clear L-spec-<n>
# State: $Q/red-alone.txt  "<spec> <sha> <UTC time> <first failing id>"; log: $Q/red-alone.log;
# dedupe marker for skip logging: $Q/red-alone.logged ("<spec> <sha>").
: "${Q:=/var/tmp/push/rq}"
_ra_spec(){ echo "${1#L-spec-}"; }
redalone_file(){ echo "$Q/red-alone.txt"; }
redalone_add(){  # redalone_add <spec> <sha> <ids>
  local s; s=$(_ra_spec "$1"); mkdir -p "$Q"
  echo "$s $2 $(date -u +%Y-%m-%dT%H:%M:%SZ) $(printf '%s\n' "$3" | grep -v '^$' | head -1 | awk '{print $1}')" >> "$(redalone_file)"; }
redalone_blocked(){  # 0 only when the same spec and the same sha are recorded
  local s f; s=$(_ra_spec "$1"); f=$(redalone_file)
  [ -n "$2" ] && [ -f "$f" ] && awk -v s="$s" -v h="$2" '$1==s && $2==h {f=1} END{exit f?0:1}' "$f"; }
redalone_clear(){  # redalone_clear <spec>
  local s f; s=$(_ra_spec "$1")
  for f in "$Q/red-alone.txt" "$Q/red-alone.logged"; do
    [ -f "$f" ] && { awk -v s="$s" '$1!=s' "$f" > "$f.tmp.$$"; mv "$f.tmp.$$" "$f"; }
  done
  echo "CLEARED L-spec-$s $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$Q/red-alone.log"; }
redalone_note(){  # redalone_note <nspecs> <spec> <sha> <base> <tip> <ids>: record one-spec red on the master tip
  local n=$1 s; s=$(_ra_spec "$2")
  [ "$n" -eq 1 ] || return 0
  [ -n "$6" ] || return 0
  case "$6" in *RUN-BROKEN*) return 0;; esac
  [ -n "$3" ] && [ "$4" = "$5" ] || return 0
  redalone_add "$s" "$3" "$6"
  echo "RED-ALONE L-spec-$s ${3:0:12} $(printf '%s\n' "$6" | grep -v '^$' | tr '\n' ' ' | sed 's/ $//') (clear: lane/redalone.sh clear L-spec-$s)" >> "$Q/red-alone.log"; }
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in clear) [ -n "${2:-}" ] || { echo "usage: redalone.sh clear L-spec-<n>" >&2; exit 2; }; redalone_clear "$2";;
    *) echo "usage: redalone.sh clear L-spec-<n>" >&2; exit 2;; esac
fi
