#!/bin/bash
# rloop.sh — the one merge driver for the push: only rehearsed batches go in.
#   ready list : $RLOOP_READY   (spec numbers, priority order; Thinker edits)
#   queue      : $RLOOP_Q/NNN.batch      ("<base-sha> <spec> <spec> ...") rehearsed clean
#   holds      : $RLOOP_Q/attention.txt  (spec + reason; a real failure or conflict)
#   stop       : touch $RLOOP_Q/stop     (expires by itself at $UNTIL)
# Loop: when the chain is free, launch the head batch if its base is still master's tip,
# otherwise re-rehearse it on the tip first. While a window runs, rehearse the next
# batch on top of that window's candidate, so it is ready the moment the window lands.
# L-spec-2266: ready members enter through one single-spec sweep on master's tip; migrations are
# ordered (doit migration-order), admitted by the resolver (doit rechain-check) onto ONE chain; a
# gate refusal of a multi-migration rehearsal and a CI red fully covered by master's own red are
# holds, never attention. Definitions below are safe to source; state and the loop start in main.
set -u; export LC_COLLATE=C LANG=C.UTF-8
L=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${RLOOP_Q:=/var/tmp/push/rq}" "${RLOOP_READY:=/var/tmp/push/ready-pass.txt}" "${RLOOP_R:=/opt/albert-scott}"
: "${RLOOP_D:=/home/albert/do-it-v2}" "${RLOOP_IR:=/home/albert/.do-it/repos/albert-scott}"
: "${RLOOP_TG_SEND:=/home/albert/.do-it/telegram/tg-send.sh}" "${RLOOP_PROJECT:=albert-scott}"
Q=$RLOOP_Q; READY=$RLOOP_READY; R=$RLOOP_R; D=$RLOOP_D; IR=$RLOOP_IR; PROJECT=$RLOOP_PROJECT
UNTIL=${RLOOP_UNTIL:-$(date -u -d '2026-11-12 23:30' +%s)}   # extended 2026-11-04 16:55Z by the Thinker (operator: keep pushing)
evdir(){ echo "${DOIT_ROOT:-$HOME/.do-it}/events"; }
log(){ local m; m="$(date -u +%H:%M:%S) $*"; printf '%s\n' "$m" >> "$Q/rloop.log"; printf '%s\n' "$m" >&2; }
. "$L/redalone.sh"   # L-spec-2285: specs red alone are not re-picked until their tip changes
tg(){ k=$(printf %s "$*" | md5sum | cut -c1-12); if grep -qx $k $Q/tg-seen 2>/dev/null; then log "TG(dup, not sent): $*"; return; fi; echo $k >> $Q/tg-seen; "$RLOOP_TG_SEND" "Merge push: $*" >/dev/null 2>&1; log "TG: $*"; }  # operator 11:57Z: same message three times
names(){ printf "%s, " "$@" | sed "s/, $//"; }
rloop_init(){ mkdir -p "$Q" && touch "$Q/attention.txt"; }
valid_n(){ [[ ${1:-} =~ ^[0-9]{1,6}$ ]]; }
valid_sha(){ [[ ${1:-} =~ ^[0-9a-f]{40}$ ]]; }
tip(){ git -C "$IR" rev-parse refs/heads/master; }
mtip(){ git -C "$R" rev-parse -q --verify refs/integ/master 2>/dev/null; }
mtree(){ git -C "$R" rev-parse -q --verify 'refs/integ/master^{tree}' 2>/dev/null; }
spec_sha(){ git -C "$R" rev-parse -q --verify "refs/integ/l-spec-$1" 2>/dev/null; }
same_tree(){ [ "$(git -C $IR rev-parse $1^{tree} 2>/dev/null)" = "$(git -C $IR rev-parse $2^{tree} 2>/dev/null)" ]; }
find_cand(){  # find_cand <batchfile>: the CI candidate commit a running rehearsal pushed (ci-verify/*), cached in <batchfile>.cand
  bf=$1; [ -s $bf.cand ] && { cat $bf.cand; return; }
  read -r b sp < $bf
  for c in $(git -C $IR ls-remote origin 'refs/heads/ci-verify/*' 2>/dev/null | cut -f1 | sort -u); do
    grep -qx $c $Q/cand-seen 2>/dev/null && continue
    git -C $IR cat-file -e $c 2>/dev/null || git -C $IR -c gc.auto=0 -c maintenance.auto=false fetch -q origin $c 2>/dev/null || continue
    ok=1; git -C $IR merge-base --is-ancestor $b $c || ok=0
    for s in $sp; do [ $ok = 1 ] && { git -C $IR merge-base --is-ancestor refs/heads/l-spec-$s $c || ok=0; }; done
    [ $ok = 1 ] && [ "$(git -C $IR log --first-parent --format=%s $b..$c | grep -c '^merge(L-spec-')" = "$(echo $sp | wc -w)" ] || ok=0   # exactly these spec merges on the first-parent chain (the gate may add its own quarantine commit on top)
    if [ $ok = 1 ]; then echo $c > $bf.cand; echo $c; return; fi
    echo $c >> $Q/cand-seen
  done; }
derive_exit(){  # a poller whose parent driver was restarted never writes exit=; derive it from the gate's REHEARSED line
  for ci in $Q/*.batch.ci; do [ -f "$ci" ] || continue; grep -q '^exit=' "$ci" && continue
    rid=$(grep -oE 'REHEARSED rh-[0-9-]+' "$ci" | head -1 | cut -d' ' -f2); [ -n "$rid" ] || continue
    pgrep -f "window_gate.py $rid" >/dev/null && continue
    if grep -qE "^REHEARSED $rid green" "$ci"; then echo exit=0 >> "$ci"; else echo exit=2 >> "$ci"; fi
    log "derived exit for $(basename $ci) from its REHEARSED line (poller orphaned by a restart)"; done; }
sync(){ git -C $R -c gc.auto=0 -c maintenance.auto=false fetch -q "$IR" "+refs/heads/l-spec-*:refs/integ/l-spec-*" "+refs/heads/master:refs/integ/master"; }
busy(){ pgrep -f "window_gate.py win-" >/dev/null; }
inflight_cand(){  # candidate sha of the window now running, if any
  w=$(pgrep -af "window_gate.py win-" | head -1 | grep -o 'win-[0-9]*-[a-z0-9]*'); [ -z "$w" ] && return
  grep -h '"window-candidate"' "$(evdir)"/*.jsonl | grep "\"$w\"" | tail -1 | grep -o '"sha": "[0-9a-f]*"' | cut -d'"' -f4; }
done_or_queued(){  # shipped/dropped, already queued, or held for attention
  s=L-spec-$1
  grep -qhE "\"subject\": \"$s\".*\"type\": \"(shipped|dropped)\"|\"type\": \"(shipped|dropped)\".*\"subject\": \"$s\"" "$(evdir)"/*.jsonl 2>/dev/null && return 0
  cat $Q/*.batch 2>/dev/null | cut -d" " -f2- | grep -qw "$1" && return 0   # Thinker 21:12Z: batch files hold bare numbers (2033/2041 rehearsed twice)
  grep -qw "^$1" $Q/attention.txt && return 0
  sha=$(git -C $R rev-parse refs/integ/l-spec-$1 2>/dev/null)
  if [ -n "$sha" ] && redalone_blocked $1 "$sha"; then
    grep -qxF "$1 $sha" $Q/red-alone.logged 2>/dev/null || { echo "$1 $sha" >> $Q/red-alone.logged; log "skip $s: red alone at ${sha:0:12}; waits for a new build" >&2; }
    return 0; fi
  pgrep -af "window_gate.py" | grep -qw "$s" && return 0; return 1; }
is_mig(){ git -C $R diff --name-only $1...refs/integ/l-spec-$2 | grep -q '^api/alembic_supabase/versions/'; }
clean_grade(){ python3 /var/tmp/push/clean.py "$1"; }   # unversioned review-clean rule; retained as is
attention_add(){ # attention_add <spec> <reason>: one line, never more than one per spec+reason
  local why; why=$(printf '%s' "$2" | tr -cd '[:print:]' | tr '|' '/' | cut -c1-300)
  grep "^$1 " "$Q/attention.txt" 2>/dev/null | grep -qF -- "$why" && return 0
  echo "$1 $(date -u +%H:%M) $why|do not auto-release|" >> "$Q/attention.txt"; }

# ---- sweep CPU telemetry: read from preflight-swept records, never from CLI output ------------
ev_snap(){ local f; for f in "$(evdir)"/*.jsonl; do [ -f "$f" ] && printf '%s\t%s\n' "$f" "$(stat -c %s "$f")"; done; return 0; }
sweep_cpu(){  # sweep_cpu <snapshot> <base> <spec-number>...: "<sweep-batch> <cpu_s>" or "unknown"
  local snap=$1 base=$2 f off recs="" subs; shift 2
  subs=$(printf 'L-spec-%s\n' "$@" | jq -R . | jq -sc 'sort') || { echo unknown; return; }
  for f in "$(evdir)"/*.jsonl; do [ -f "$f" ] || continue
    off=$(awk -F'\t' -v f="$f" '$1==f{print $2}' <<<"$snap"); off=${off:-0}
    recs+=$(tail -c +$((off+1)) "$f" 2>/dev/null | grep -F '"preflight-swept"' || true)$'\n'
  done
  printf '%s' "$recs" | jq -R 'fromjson? | select(type=="object" and .type=="preflight-swept")' 2>/dev/null | jq -rs --arg base "$base" --argjson subs "$subs" '
    (group_by(.batch) | map(select(.[0].base==$base and ((map(.subject)|unique)==$subs)))) as $g
    | if ($g|length)==1 and ($g[0]|map(.cpu_s)|unique|length)==1 and ($g[0][0].cpu_s|type)=="number"
         and ($g[0][0].batch|type)=="string" and ($g[0][0].batch|test("^[0-9A-Za-z._-]{1,64}$"))
      then "\($g[0][0].batch) \($g[0][0].cpu_s)" else "unknown" end' 2>/dev/null || echo unknown; }
cpu_log(){  # cpu_log <driver-batch> <snapshot> <base> <spec-number>...: the log line, echoes "<sweep> <cpu>" or "- unknown"
  local dbatch=$1 r; shift
  r=$(sweep_cpu "$@"); [ -n "$r" ] || r=unknown
  if [ "$r" = unknown ]; then log "preflight-cpu $dbatch - cpu_s=unknown reason=missing-or-ambiguous-preflight-record"; echo "- unknown"
  else log "preflight-cpu $dbatch ${r% *} cpu_s=${r#* }"; echo "$r"; fi; }

# ---- first-batch eligibility: one clean single-spec sweep on master's tip, keyed by sha + tree ----
elig_file(){ echo "$Q/eligibility/$1.$2.$3"; }
preclean(){  # spec $1 has a valid clean record for its CURRENT branch sha and master tree
  valid_n "$1" || return 1
  local sha tree f st pj ss tt
  sha=$(spec_sha "$1"); tree=$(mtree); valid_sha "$sha" && valid_sha "$tree" || return 1
  f=$(elig_file "$1" "$sha" "$tree"); [ -f "$f" ] || return 1
  read -r st pj ss tt _ < "$f" 2>/dev/null || return 1
  [ "$st" = clean ] && [ "$pj" = "$PROJECT" ] && [ "$ss" = "$sha" ] && [ "$tt" = "$tree" ]; }
ready_entry(){  # 0 only on a clean eligibility record for this key; red parks once; undetermined retries next pass
  local n=$1 sha tree base f rf rc out snap cpu line stage cls detail
  valid_n "$n" || { log "ready-hold $n: invalid-spec-id"; return 1; }
  sha=$(spec_sha "$n"); tree=$(mtree); base=$(mtip)
  valid_sha "$sha" && valid_sha "$tree" && valid_sha "$base" || { log "ready-hold $n: branch-or-master-unresolved"; return 1; }
  preclean "$n" && return 0
  f=$(elig_file "$n" "$sha" "$tree"); rf="$f.red"
  [ -f "$f" ] && { log "ready-hold $n: eligibility-record-malformed"; return 1; }
  [ -f "$rf" ] && return 1   # red at this key: its one attention entry already exists
  mkdir -p "$Q/eligibility" || { log "ready-hold $n: state-unwritable"; return 1; }
  snap=$(ev_snap)
  out=$(timeout -k 5 210 "$D/doit" preflight --project "$PROJECT" --on "$base" --specs "L-spec-$n" 2>&1); rc=$?
  cpu=$(cpu_log "ready-$n" "$snap" "$base" "$n")
  case "$rc" in
    0) printf 'clean %s %s %s sweep=%s cpu_s=%s\n' "$PROJECT" "$sha" "$tree" "${cpu% *}" "${cpu#* }" > "$f.tmp.$$" && mv "$f.tmp.$$" "$f" \
         || { rm -f "$f.tmp.$$"; log "ready-hold $n: eligibility-unwritable"; return 1; }
       preclean "$n" && { log "ready-entry $n: clean on ${base:0:12}"; return 0; }; return 1 ;;
    1) line=$(printf '%s\n' "$out" | grep -m1 '^PREFLIGHT-RED ')
       read -r _ stage _ cls detail <<<"$line"
       [[ $stage =~ ^[a-z-]{1,30}$ ]] || stage=unknown; [[ $cls =~ ^[a-z-]{1,30}$ ]] || cls=unknown
       printf 'red %s %s\n' "$stage" "$cls" > "$rf" || { log "ready-hold $n: red-record-unwritable"; return 1; }
       attention_add "$n" "ready-entry sweep red $stage/$cls: $detail"
       log "ready-entry $n: red $stage/$cls; parked"; return 1 ;;
    *) log "ready-hold $n: sweep-undetermined (exit $rc); retry next pass"; return 1 ;;
  esac; }
elig_ref(){  # elig_ref <driver-batch> <spec>...: carry ready-entry CPU into the batch log without charging it again
  local b=$1 n f line; shift
  for n in "$@"; do f=$(elig_file "$n" "$(spec_sha "$n")" "$(mtree)"); [ -f "$f" ] || continue
    line=$(head -n1 "$f"); log "preflight-cpu-ref $b L-spec-$n $(grep -oE 'sweep=[^ ]+ cpu_s=[^ ]+' <<<"$line") counted=ready-entry"; done; }

# ---- migration order ------------------------------------------------------------------------
ORDERED=(); ORDER_CYCLE=" "
order_migs(){  # order_migs <spec>...: sets ORDERED; returns 1 = hold migration batching for this pass
  ORDERED=(); ORDER_CYCLE=" "
  local ids out rc line n seen=" " cyc="" k v ok=1 words inset
  inset=$(printf ' L-spec-%s' "$@")
  ids=$(printf 'L-spec-%s,' "$@"); ids=${ids%,}
  log "migration-order input: $*"
  out=$("$D/doit" migration-order --project "$PROJECT" --specs "$ids" 2>/dev/null); rc=$?
  if [ $rc -eq 2 ]; then
    while IFS= read -r line; do case "$line" in "CYCLE "*) ;; *) continue ;; esac
      words=${line#CYCLE }; ok=1
      for k in $words; do [[ $k =~ ^L-spec-[0-9]{1,6}$ ]] && [[ "$inset " == *" $k "* ]] || ok=0; done
      [ -n "$words" ] && [ $ok = 1 ] || { log "migration-order-undetermined malformed-cycle-line"; return 1; }
      for k in $words; do ORDER_CYCLE+="${k#L-spec-} "; done
      cyc+="$words;"
      if ! grep -qF "migration-order-cycle $words" "$Q/attention.txt" 2>/dev/null; then
        v=${words%% *}; echo "${v#L-spec-} $(date -u +%H:%M) migration-order-cycle $words|do not auto-release|" >> "$Q/attention.txt"
        log "migration-order-cycle $words"; fi
    done <<<"$out"
    [ -n "$cyc" ] || log "migration-order-undetermined exit 2 without CYCLE lines (unavailable or corrupt project evidence)"
    return 1
  fi
  [ $rc -eq 0 ] || { log "migration-order-undetermined exit $rc"; return 1; }
  while IFS= read -r line; do [ -n "$line" ] || continue
    case "$line" in
      "DEFERRED "*) read -r _ k v <<<"$line"; [[ $k =~ ^L-spec-[0-9]{1,6}$ ]] || { log "migration-order-undetermined malformed-deferred"; ORDERED=(); return 1; }
        [[ $seen == *" ${k#L-spec-} "* ]] && { log "migration-order-undetermined duplicate-id"; ORDERED=(); return 1; }
        seen+="${k#L-spec-} "; log "migration-order-deferred ${k#L-spec-} $v" ;;
      "CYCLE "*|*" "*) log "migration-order-undetermined unexpected-output"; ORDERED=(); return 1 ;;
      *) [[ $line =~ ^L-spec-[0-9]{1,6}$ ]] || { log "migration-order-undetermined malformed-id"; ORDERED=(); return 1; }
        n=${line#L-spec-}; [[ $seen == *" $n "* ]] && { log "migration-order-undetermined duplicate-id"; ORDERED=(); return 1; }
        seen+="$n "; ORDERED+=("$n") ;;
    esac
  done <<<"$out"
  for n in "$@"; do [[ $seen == *" $n "* ]] || { log "migration-order-undetermined missing-id $n"; ORDERED=(); return 1; }; done
  for n in $seen; do [[ " $* " == *" $n "* ]] || { log "migration-order-undetermined unknown-id $n"; ORDERED=(); return 1; }; done
  log "migration-order output: ${ORDERED[*]:-none}"; return 0; }

# ---- stack admission: only the resolver decides a migration's bookkeeping --------------------
shared_paths(){  # the canonical rechain.SHARED, read without importing Python; nonzero when unreadable
  local f="$D/src/rechain.py" names name v out=""
  [ -r "$f" ] || return 1
  names=$(sed -n 's/^SHARED = {\(.*\)}[[:space:]]*$/\1/p' "$f" | head -1 | tr -d ' ' | tr ',' ' ')
  [ -n "$names" ] || return 1
  for name in $names; do [[ $name =~ ^[A-Z_]+$ ]] || return 1
    v=$(sed -n "s/^$name = [\"']\([^\"']*\)[\"'][[:space:]]*\$/\1/p" "$f" | head -1); [ -n "$v" ] || return 1
    out+="$v"$'\n'; done
  printf '%s' "$out"; }
STACK_WHY=""
stack_decide(){  # stack_decide <base> <spec> <earlier-migration-spec>...: 0 admit, 1 park (attention), 2 hold
  local base=$1 n=$2 out rc o line st want="" ids; shift 2
  ids=$(printf 'L-spec-%s,' "$@" "$n"); ids=${ids%,}
  out=$("$D/doit" rechain-check --project "$PROJECT" --on "$base" --specs "$ids" 2>/dev/null); rc=$?
  [ $rc -eq 2 ] && { STACK_WHY="rechain-check undetermined"; return 2; }
  [ $rc -le 1 ] || { STACK_WHY="rechain-check exit $rc"; return 2; }
  for o in "$@" "$n"; do
    line=$(awk -v s="L-spec-$o" '$1==s{print; exit}' <<<"$out"); read -r _ st rest <<<"$line"
    case "$st" in clean|rechained) ;;
      content-conflict|declined) [ "$o" = "$n" ] || { STACK_WHY="earlier member $o $st"; return 2; }
        STACK_WHY="$st ${rest:-}"; return 1 ;;
      *) STACK_WHY="no resolver outcome for $o"; return 2 ;; esac
  done; return 0; }

# ---- one migration chain ---------------------------------------------------------------------
batch_mig(){ local b sp s; read -r b sp < "$1" 2>/dev/null || return 1; for s in $sp; do is_mig "$b" "$s" && return 0; done; return 1; }
CHAIN_WHY=""
chain_check(){  # 0 = a migration batch may be formed on $1
  local base=$1 t bf c preds=() ci
  t=$(mtip); [ -n "$t" ] || { CHAIN_WHY="master-unresolved"; return 1; }
  for bf in $Q/*.batch $Q/current.bat; do [ -f "$bf" ] || continue
    batch_mig "$bf" || continue
    if [ "$bf" = "$Q/current.bat" ]; then busy || continue; c=$(inflight_cand); else c=$(cat "$bf.cand" 2>/dev/null); fi
    [ -n "$c" ] || { CHAIN_WHY="unknown-candidate for $(basename "$bf")"; return 1; }
    [ -f "$Q/nostack-${c:0:12}" ] && { CHAIN_WHY="stale-or-failed-predecessor $(basename "$bf")"; return 1; }
    ci="$bf.ci"; if [ -f "$ci" ] && grep -q '^exit=' "$ci" && ! grep -q '^exit=0' "$ci"; then CHAIN_WHY="failed-predecessor $(basename "$bf")"; return 1; fi
    preds+=("$c"); done
  if [ ${#preds[@]} -eq 0 ]; then
    if [ "$base" != "$t" ] && [ -f "$Q/nostack-${base:0:12}" ]; then CHAIN_WHY="stale-candidate-base"; return 1; fi
    return 0; fi
  [ "$base" = "$t" ] && { CHAIN_WHY="migration-rehearsal-in-flight (no sibling base)"; return 1; }
  for c in "${preds[@]}"; do git -C "$R" merge-base --is-ancestor "$c" "$base" 2>/dev/null || { CHAIN_WHY="base-lacks-earlier-migration-members"; return 1; }; done
  return 0; }
cand_owner_paths(){  # cand_owner_paths <base>: "d|m|c <path>": carried by the base / by a migration member / by a code-only member
  local base=$1 bf c b sp s o
  git -C "$R" diff --name-only refs/integ/master..."$base" 2>/dev/null | sed 's/^/d /'
  for bf in $Q/*.batch $Q/current.bat; do [ -f "$bf" ] || continue
    if [ "$bf" = "$Q/current.bat" ]; then c=$(inflight_cand); else c=$(cat "$bf.cand" 2>/dev/null); fi
    [ -n "$c" ] && git -C "$R" merge-base --is-ancestor "$c" "$base" 2>/dev/null || continue
    read -r b sp < "$bf"
    for s in $sp; do if is_mig "$b" "$s"; then o=m; else o=c; fi
      git -C "$R" diff --name-only "$b"...refs/integ/l-spec-$s | sed "s/^/$o /"; done
  done; }

# ---- held pools: gate capability holds and base-red suppression ------------------------------
gate_rev(){ local f; for f in window_rehearse window_gate window_prewindow merge_queue; do cat "$D/src/$f.py" 2>/dev/null || echo none; done | md5sum | cut -c1-16; }
members_sha(){ local n out=""; for n in "$@"; do out+="$n:$(spec_sha "$n") "; done; printf '%s' "${out% }"; }
valid_hold(){ [[ ${1:-} =~ ^([0-9]{1,6}:[0-9a-f]{40})( [0-9]{1,6}:[0-9a-f]{40})*$ ]]; }
gate_held(){  # 0 while a capability hold covering spec $1 is unchanged (same gate code, same member inputs)
  local f rev ms cur
  for f in "$Q"/gate-hold/*; do [ -f "$f" ] || continue
    [[ " $(sed -n 2p "$f") " == *" $1:"* ]] || continue
    rev=$(sed -n 1p "$f"); ms=$(sed -n 2p "$f")
    valid_hold "$ms" || { log "migration-gate-hold-malformed $(basename "$f"); staying held"; return 0; }
    cur=$(members_sha $(sed 's/:[0-9a-f]*//g' <<<"$ms"))
    if [ "$rev" = "$(gate_rev)" ] && [ "$ms" = "$cur" ]; then return 0; fi
    rm -f "$f"; log "migration-gate-release $(basename "$f") (gate code or member inputs changed)"
  done; return 1; }
base_ids(){  # base_ids <sha>: master's complete failing ids, one per line; nonzero when not a complete answer
  local out rc
  out=$("$D/doit" base-red "$PROJECT" "$1" --json 2>/dev/null); rc=$?
  [ $rc -eq 0 ] || return 1
  jq -e --arg sha "$1" 'select(.complete==true and .sha==$sha and ((.undetermined//[])|length)==0)' <<<"$out" >/dev/null 2>&1 || return 1
  jq -r '[.ids[]?[]] | unique | .[]' <<<"$out" 2>/dev/null; }
base_red_held(){  # 0 while a base-red hold covering spec $1 is unchanged; released only by complete changed evidence or a moved member
  local f ms cur ids now i
  for f in "$Q"/base-red-hold/*; do [ -f "$f" ] || continue
    [[ " $(sed -n 1p "$f") " == *" $1:"* ]] || continue
    ms=$(sed -n 1p "$f"); ids=$(sed -n 2p "$f")
    valid_hold "$ms" && [[ $ids =~ ^[][A-Za-z0-9_./:,-]+$ ]] || { log "base-red-hold-malformed $(basename "$f"); staying held"; return 0; }
    cur=$(members_sha $(sed 's/:[0-9a-f]*//g' <<<"$ms"))
    if [ "$ms" != "$cur" ]; then rm -f "$f"; log "base-red-release $(basename "$f") (member branch moved)"; continue; fi
    if now=$(base_ids "$(mtip)") && [ -n "$(mtip)" ]; then
      for i in ${ids//,/ }; do grep -qxF "$i" <<<"$now" || { rm -f "$f"; log "base-red-release $(basename "$f") (master red status changed for $i)"; continue 2; }; done; fi
    return 0
  done; return 1; }
ci_failing_ids(){  # failing ids a red rehearsal named; empty unless the gate printed its red REHEARSED verdict
  grep -qE '^REHEARSED rh-[0-9-]+ red( |$)' "$1" || return 0
  { grep -E '^(UNRESOLVED|ENVIRONMENT) [^ ]+$' "$1" | cut -d' ' -f2
    grep -E '^(ATTRIBUTED|FALLBACK) L-spec-[0-9]+ [^ ]+$' "$1" | cut -d' ' -f3; } | sort -u; }

pick(){  [ -n "${1:-}" ] || return 0; sync  # next members from the ready list: migrations in migration-order, no shared files except resolver-owned bookkeeping
  local base=$1 n p fa mi=0 migset=" " elig=() seq=() migs=() got=() gotm=() mighold=0 owners rc o
  declare -A fowner=()
  git -C $R cat-file -e $base 2>/dev/null || git -C $R -c gc.auto=0 -c maintenance.auto=false fetch -q "$IR" $base
  for n in $(grep -oE '^[0-9]+' $READY 2>/dev/null); do
    done_or_queued $n && continue
    gate_held $n && continue
    base_red_held $n && continue
    clean_grade $n || continue
    git -C $R rev-parse -q --verify refs/integ/l-spec-$n >/dev/null || continue
    ready_entry $n || continue
    elig+=($n); done
  for n in "${elig[@]}"; do is_mig $base $n && { migs+=($n); migset+="$n "; }; done
  if [ ${#migs[@]} -gt 0 ]; then
    if ! chain_check $base; then log "migration-hold: ${migs[*]}: $CHAIN_WHY"; mighold=1
    elif ! order_migs "${migs[@]}"; then mighold=1; fi; fi
  for n in "${elig[@]}"; do
    if [[ $migset == *" $n "* ]]; then
      [ $mighold = 1 ] && continue
      [ $mi -lt ${#ORDERED[@]} ] || continue
      seq+=("${ORDERED[$mi]}"); mi=$((mi+1))
    else seq+=($n); fi; done
  owners=$(cand_owner_paths $base)
  declare -A hasm=() hasc=() hasd=()
  while read -r o p; do [ -n "${p:-}" ] || continue
    case "$o" in m) hasm[$p]=1 ;; c) hasc[$p]=1 ;; *) hasd[$p]=1 ;; esac; done <<<"$owners"
  for p in "${!hasd[@]}" "${!hasm[@]}" "${!hasc[@]}"; do   # a path is migration-owned only when no code-only member touches it
    if [ -n "${hasc[$p]:-}" ] || [ -z "${hasm[$p]:-}" ]; then fowner[$p]=c; else fowner[$p]=m; fi; done
  shared=$(shared_paths) && shared_ok=1 || shared_ok=0
  for n in "${seq[@]}"; do
    mapfile -t fa < <(git -C $R diff --name-only $base...refs/integ/l-spec-$n)
    if [[ $migset == *" $n "* ]]; then
      for p in "${fa[@]}"; do
        [ -z "$p" ] || [ -z "${fowner[$p]:-}" ] && continue
        if [ "${fowner[$p]}" = m ] && [ $shared_ok = 1 ] && grep -qxF -- "$p" <<<"$shared"; then continue; fi
        continue 2; done
      stack_decide $base $n "${gotm[@]}"; rc=$?
      if [ $rc -eq 1 ]; then attention_add "$n" "stack decision: $STACK_WHY"; log "stack: $n parked ($STACK_WHY)"; continue
      elif [ $rc -eq 2 ]; then log "stack-hold: $n: $STACK_WHY"; continue; fi
    else
      if ! git -C $R merge-tree --write-tree $base refs/integ/l-spec-$n >/dev/null 2>&1; then   # Thinker 2026-11-04 16:20Z: conflict precheck for code-only members
        grep -q "^$n " $Q/attention.txt 2>/dev/null || { echo "$n $(date -u +%H:%M) CONFLICT with ${base:0:12} (pick precheck; Thinker files the rework)|do not auto-release|" >> $Q/attention.txt; log "precheck: $n conflicts with ${base:0:12}; parked"; }
        continue; fi
      for p in "${fa[@]}"; do [ -n "$p" ] && [ -n "${fowner[$p]:-}" ] && continue 2; done
    fi
    [ ${#got[@]} -ge 4 ] && ! preclean $n && continue   # slots 5-6 only for specs the pre-check passed alone on this master
    got+=($n); [[ $migset == *" $n "* ]] && gotm+=($n)
    for p in "${fa[@]}"; do [ -n "$p" ] || continue; if [[ $migset == *" $n "* ]]; then fowner[$p]=${fowner[$p]:-m}; else fowner[$p]=c; fi; done
    if [ ${#got[@]} -ge 4 ]; then allclean=1; for g in "${got[@]}"; do preclean $g || allclean=0; done
      [ $allclean -eq 1 ] && [ ${#got[@]} -lt 6 ] && continue   # up to 6 when every member's eligibility key is clean
      break; fi
  done; echo "${got[@]}"; }
rehearse(){  # rehearse(base, specs...) -> writes queue file or moves culprits to attention
  base=$1; shift; specs=("$@"); args=$(printf 'L-spec-%s ' "${specs[@]}")
  log "rehearse on ${base:0:12}: ${specs[*]}"
  # Operator 2026-11-04 17:31Z "(a)": the full-CI rehearsal starts NOW, alongside the pre-flight (Yitzy #1272: CI idle 45%).
  # A red pre-flight cancels it; a green one just lets it run. The batch file exists from the start so the loop polls it.
  bf=$Q/$(date -u +%s).batch; echo "$base ${specs[*]}" > $bf
  sp=$(printf 'L-spec-%s,' "${specs[@]}"); sp=${sp%,}; cid=rh-$(date -u +%m%d-%H%M%S)
  (cd $D && DOIT_ACTOR=thinker DOIT_WINDOW_MAX_MERGES=${#specs[@]} PYTHONUNBUFFERED=1 nice ./doit gate-window $cid --rehearse --on $base --project "$PROJECT" --specs $sp > $bf.ci 2>&1; echo "exit=$?" >> $bf.ci) &
  cipid=$!; echo $cipid > $bf.pid; log "full-CI rehearsal $cid started for ${specs[*]} (in parallel with the pre-flight)"
  elig_ref "$(basename $bf)" "${specs[@]}"
  snap=$(ev_snap)
  out=$("$L/preflight.sh" $base $args 2>&1 | tail -40)
  cpu_log "$(basename $bf)" "$snap" "$base" "${specs[@]}" >/dev/null
  tag=$(printf "%s\n" "${specs[@]}" | paste -sd-); rep=${PREFLIGHT_OUT:-/var/tmp/push}/rehearse-$tag.txt
  bad=$(grep -E '^(CONFLICT|OVERLAP|ALEMBIC-HEADS)' $rep)
  new=$(sed -n '/^NEW-FAILURES:/,/^DONE/p' $rep | grep -v -e NEW-FAILURES -e DONE)
  if [ -z "$bad" ] && [ -z "$new" ] && grep -q NO-NEW-FAILURES $rep; then
    log "CLEAN -> queued: ${specs[*]} (CI rehearsal $cid already running)"
    tg "pre-flight passed for $(names "${specs[@]}"); the full CI rehearsal has been running since the batch started. The window reuses it and lands in minutes."; return 0; fi
  log "NOT CLEAN: $(echo "$bad $new" | tr '\n' ' ' | cut -c1-400)"
  # cancel the parallel CI rehearsal: its python child, its GitHub runs (branch ends in -win<pid>), then the poller subshell
  pypid=$(pgrep -P $cipid -f window_gate.py | head -1)
  if [ -n "$pypid" ]; then kill $pypid 2>/dev/null; sleep 2
    (cd $IR && gh run list --limit 20 --json databaseId,headBranch --jq ".[] | select(.headBranch|endswith(\"-win$pypid\")) | .databaseId" 2>/dev/null | while read -r rid; do gh run cancel $rid >/dev/null 2>&1; done); fi
  kill $cipid 2>/dev/null; rm -f $bf $bf.ci $bf.cand; log "cancelled the parallel CI rehearsal $cid (pre-flight red)"
  if echo "$new" | grep -q RUN-BROKEN; then [ -n "$(find $Q/broken -mmin -60 2>/dev/null)" ] && { log "run broken again; quiet 10 min"; sleep 600; return 1; }; touch $Q/broken; tg "rehearsal of $(names "${specs[@]}") could not run its tests ($(echo "$new" | cut -c1-150)). Not blaming any spec; Thinker is checking."; sleep 600; return 1; fi
  if [ "$base" != "$(tip)" ]; then touch $Q/nostack-${base:0:12}; log "stacked rehearsal on ${base:0:12} not clean; no attribution, no more stacking on it; retry on master"; return 1; fi
  [ ${#specs[@]} -eq 1 ] && redalone_note 1 ${specs[0]} "$(git -C $R rev-parse refs/integ/l-spec-${specs[0]} 2>/dev/null)" "$base" "$(tip)" "$new"   # L-spec-2285
  # attribute fast: run the failing ids on master and on master+each member in PARALLEL worktrees
  # (no PG lane needed); members that add failures are set aside, the rest ride on. Only when this
  # names nobody (e.g. PG-only failures) fall back to the serial alone-rehearsals below.
  if [ -n "$new" ] && ! echo "$new" | grep -q RUN-BROKEN; then
    ATTR_TESTS="$(echo "$new" | grep -v '^$' | sed 's/\[.*//' | sort -u | tr '\n' ' ')" timeout 1500 "$L/attr.sh" "${specs[@]}" >/dev/null 2>&1
    named=$(awk '($2=="NEW:" && $3!="none") || $2=="CONFLICT" {print $1}' "${PREFLIGHT_OUT:-/var/tmp/push}/attr.txt" 2>/dev/null)
    if [ -n "$named" ]; then
      for s in $named; do echo "$s $(date -u +%H:%M) attr: $(grep "^$s " "${PREFLIGHT_OUT:-/var/tmp/push}/attr.txt" | head -1 | cut -c1-160)" >> $Q/attention.txt; done
      log "attributed by parallel alone-runs: $(echo $named | tr '\n' ' ')"; new=""
    else log "parallel attribution named nobody; falling back to serial alone-rehearsals"; fi
  fi
  # attribute: a conflict names its spec; test failures -> rehearse each spec alone on base
  for s in "${specs[@]}"; do
    if echo "$bad" | grep -qE "^(CONFLICT|OVERLAP|ALEMBIC-HEADS) L-spec-$s\b"; then echo "$s $(date -u +%H:%M) $(echo "$bad"|grep -E "^(CONFLICT|OVERLAP|ALEMBIC-HEADS) L-spec-$s\b"|head -1)" >> $Q/attention.txt; continue; fi
    if [ -n "$new" ]; then
      [ -z "$(echo "$new" | grep -v '^$')" ] && continue   # every batch failure already reproduced by an earlier spec alone
      "$L/preflight.sh" $base L-spec-$s >/dev/null 2>&1
      grep -q NO-NEW-FAILURES ${PREFLIGHT_OUT:-/var/tmp/push}/rehearse-$s.txt || echo "$s $(date -u +%H:%M) alone: $(sed -n '/^NEW-FAILURES:/,/^DONE/p' ${PREFLIGHT_OUT:-/var/tmp/push}/rehearse-$s.txt | sed -n 2p)" >> $Q/attention.txt
      new=$(echo "$new" | grep -vxF -f <(sed -n '/^NEW-FAILURES:/,/^DONE/p' ${PREFLIGHT_OUT:-/var/tmp/push}/rehearse-$s.txt | grep -v -e NEW-FAILURES -e DONE) || true)
    fi
  done
  tg "rehearsal of $(names "${specs[@]}") found a problem. Set aside: $(awk -v s=" ${specs[*]} " 'index(s," "$1" ")' $Q/attention.txt | cut -d" " -f1 | sort -u | paste -sd, -). Picking replacements and rehearsing again."
  return 1; }
cancel_ci(){  # Thinker 2026-11-04 22:30Z: a dropped stale batch must free its gate slot (orphan rh-1104-214646 blocked 2033+2038)
  f=$1; [ -f $f.pid ] || return 0; c=$(cat $f.pid); p=$(pgrep -P $c -f window_gate.py | head -1)
  [ -n "$p" ] && { kill $p 2>/dev/null; log "cancelled stale CI rehearsal for $(cut -d' ' -f2- $f 2>/dev/null) (pid $p)"; }
  kill $c 2>/dev/null; rm -f $f.pid; }
bgr(){  # background rehearsal (one at a time): the loop keeps launching/processing windows meanwhile (18:19-18:49 a green window waited on a timed-out rehearsal)
  [ -f $Q/rehearsing ] && return 1; ( rehearse "$@"; rm -f $Q/rehearsing ) & echo $! > $Q/rehearsing; return 0; }
fill(){  # keep one rehearsed batch ready on top of $1
  base=$1; ls $Q/*.batch >/dev/null 2>&1 && return; [ -f $Q/rehearsing ] && return
  p=$(pick $base); [ -z "$p" ] && { log "ready list empty"; sleep 60; return; }; bgr $base $p; }
ci_red(){  # ci_red <batchfile>: a finished-red full-CI rehearsal of the head batch
  local h=$1 base specs ids master cover n att s hold bid
  read -r base specs < "$h"; bid=$(basename "$h")
  sync   # member shas and master's red evidence are read from the freshly fetched refs
  if grep -qiE 'at most one migration spec per rehearsal' "$h.ci"; then   # a capability hold, not a spec fault
    log "migration-gate-hold $bid $(grep -iEm1 'at most one migration spec per rehearsal' "$h.ci" | tr -cd '[:print:]' | cut -c1-200)"
    mkdir -p "$Q/gate-hold" && { printf '%s\n%s\n' "$(gate_rev)" "$(members_sha $specs)" > "$Q/gate-hold/$bid.$$" && mv "$Q/gate-hold/$bid.$$" "$Q/gate-hold/$bid"; } \
      || { log "migration-gate-hold-unwritable $bid; batch kept, nothing launches from it"; return 0; }
    cancel_ci "$h"; [ -s "$h.cand" ] && cat "$h.cand" >> $Q/cand-seen
    rm -f "$h" "$h.ci" "$h.cand" "$h.pid"; return 0; fi
  log "full-CI rehearsal not green: $(grep -E 'gate-window|FLAKE|reproduc|exit=' $h.ci | tail -4 | tr '\n' ' ' | cut -c1-400)"
  ids=$(ci_failing_ids "$h.ci"); master=$(mtip)
  if [ -n "$ids" ] && valid_sha "$master" && cover=$(base_ids "$master") && [ -z "$(comm -23 <(echo "$ids") <(echo "$cover" | sort -u))" ]; then
    mkdir -p "$Q/base-red-hold" && { printf '%s\n%s\n' "$(members_sha $specs)" "$(echo $ids | tr ' ' ',')" > "$Q/base-red-hold/$bid.$$" && mv "$Q/base-red-hold/$bid.$$" "$Q/base-red-hold/$bid"; } \
      || { log "base-red-hold-unwritable $bid; batch kept, nothing launches from it"; return 0; }
    log "base-red-hold $bid $(echo $ids | tr ' ' ',')"
    cancel_ci "$h"; [ -s "$h.cand" ] && cat "$h.cand" >> $Q/cand-seen
    rm -f "$h" "$h.ci" "$h.cand" "$h.pid"; return 0; fi
  tg "full CI rehearsal of $specs was not green: $(grep -E 'reproduc|FLAKE|exit=' $h.ci | tail -3 | tr '\n' ' ' | cut -c1-300). Holding that batch; Thinker is checking."
  att=$(grep -oE '^(ATTRIBUTED|FALLBACK) L-spec-[0-9]+' $h.ci | grep -oE '[0-9]+$' | sort -u)
  # exit=143 = we cancelled it ourselves after a red pre-flight; the local attribution already parked the culprit
  # (Thinker 2026-11-05 06:30Z: this branch parked 1901/1993/2003 as "unattributed" although only 1772 failed)
  grep -q '^exit=143' $h.ci && [ -z "$att" ] && specs=""
  for s in $specs; do if echo "$att" | grep -qx $s; then echo "$s $(date -u +%H:%M) CI rehearsal attributed failure" >> $Q/attention.txt
    elif [ -z "$att" ]; then echo "$s $(date -u +%H:%M) CI rehearsal red, unattributed: Thinker releases clean ones" >> $Q/attention.txt; fi; done
  mv $h $h.red; mv $h.ci $h.red.ci; [ -s $h.cand ] && cat $h.cand >> $Q/cand-seen; rm -f $h.cand; return 0; }
loop_once(){  # one pass of the driver loop
  derive_exit
  [ -f $Q/rehearsing ] && ! kill -0 "$(cat $Q/rehearsing 2>/dev/null)" 2>/dev/null && rm -f $Q/rehearsing   # stale marker after a crash
  if busy; then
    c=$(inflight_cand); [ -n "$c" ] && [ ! -f $Q/nostack-${c:0:12} ] && fill $c
    # a queued batch rehearsed on a different base than the in-flight candidate is stale
    sleep 15; return 0; fi
  t=$(tip)
  if [ "$t" != "$(cat $Q/pushed 2>/dev/null)" ]; then  # a window advanced master: push it to GitHub
    r=$(cd $D/src && timeout 250 env -u DOIT_LEDGER_FILE python3 -c "import fold,push_origin; print(push_origin.run(fold.read_events()))" 2>&1 | tail -c 80)
    log "push ${t:0:12}: $r"; { echo "$r" | grep -q albert || [ "$(git -C $IR ls-remote origin refs/heads/master | cut -f1)" = "$t" ]; } && echo $t > $Q/pushed; fi
  if [ -f $Q/current ]; then cw=$(cat $Q/current); rm -f $Q/current
    res=$(grep -h "gate-window:" $Q/$cw.log | tail -1 | cut -c1-300)
    case "$res" in *"advanced master"*) tg "window $cw merged. $(echo "$res" | grep -o "shipped=.*" | tr -d "[]'\"" | sed 's/L-spec-//g')";;
      *) tg "window $cw did not merge: $(tail -2 $Q/$cw.log | tr '\n' ' ' | cut -c1-250)"
         # refused before CI ran (chain held, cap, base not releasable): the rehearsal still stands
         grep -qE "chain held|exceeds the window cap|base-not-releasable|lock" $Q/$cw.log && [ -f $Q/current.bat ] && { mv $Q/current.bat $Q/0000000000.batch; log "requeued rehearsed batch after refusal"; };; esac
    rm -f $Q/current.bat; fi
  h=$(ls $Q/*.batch 2>/dev/null | head -1)
  if [ -z "$h" ]; then fill $t; return 0; fi
  read -r base specs < $h
  if ! same_tree $base $t; then log "head batch base ${base:0:12} tree != master ${t:0:12}; re-rehearse"; [ -s $h.cand ] && cat $h.cand >> $Q/cand-seen; cancel_ci $h; rm -f $h $h.ci $h.cand
    rehearse $t $specs; return 0; fi
  if [ -f $h.ci ] && ! grep -q '^exit=' $h.ci; then   # full-CI rehearsal still running
    if [ $(ls $Q/*.batch 2>/dev/null | wc -l) -lt 2 ] && [ ! -f $Q/rehearsing ]; then c=$(find_cand $h)   # cap 2->3 (Thinker 2026-11-04 17:50Z, Yitzy note #1272: lane idle 45%)
      [ -n "$c" ] && [ ! -f $Q/nostack-${c:0:12} ] && { log "stacking next batch on ${c:0:12} (candidate of $(cut -d' ' -f2- $h))"; p=$(pick $c); [ -n "$p" ] && bgr $c $p; }; fi
    sleep 15; return 0; fi
  if [ -f $h.ci ] && ! grep -q '^exit=0' $h.ci; then ci_red "$h"; return 0; fi
  id=win-$(date -u +%Y%m%d)-r$(date -u +%H%M); sp=$(printf 'L-spec-%s,' $specs); sp=${sp%,}
  log "LAUNCH $id: $specs"; cp $h $Q/current.bat; rm -f $h $h.ci $h.cand; echo $id > $Q/current; tg "window $id started: $specs. CI takes about 40 minutes."
  (cd $D && DOIT_ACTOR=thinker DOIT_WINDOW_MAX_MERGES=$(echo "$sp" | tr "," "\n" | grep -c .) PYTHONUNBUFFERED=1 nice ./doit gate-window $id --project "$PROJECT" --specs $sp > $Q/$id.log 2>&1; echo "exit=$?" >> $Q/$id.log) &
  sleep 30
  if ! busy; then log "$id did not hold the chain: $(tail -2 $Q/$id.log | tr '\n' ' ')"; sleep 30; fi
  return 0; }
main(){
  rloop_init || { echo "rloop: cannot initialise $Q" >&2; return 1; }
  log "rloop start (until $(date -u -d @$UNTIL +%FT%TZ))"
  echo $$ > $Q/main.pid   # Thinker 2026-11-04: pid-only restarts keep CI pollers alive
  while [ $(date +%s) -lt $UNTIL ] && [ ! -f $Q/stop ]; do loop_once; done
  log "rloop end"; }
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
