#!/bin/bash
# rloop.sh — the one merge driver for the push: only rehearsed batches go in.
#   ready list : /var/tmp/push/ready-pass.txt   (spec numbers, priority order; Thinker edits)
#   queue      : /var/tmp/push/rq/NNN.batch      ("<base-sha> <spec> <spec> ...") rehearsed clean
#   holds      : /var/tmp/push/rq/attention.txt  (spec + reason; rehearsal found a real failure)
#   stop       : touch /var/tmp/push/rq/stop     (expires by itself at $UNTIL)
# Loop: when the chain is free, launch the head batch if its base is still master's tip,
# otherwise re-rehearse it on the tip first. While a window runs, rehearse the next
# batch on top of that window's candidate, so it is ready the moment the window lands.
set -u; export LC_COLLATE=C LANG=C.UTF-8
L=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
Q=/var/tmp/push/rq; mkdir -p $Q; touch $Q/attention.txt
READY=/var/tmp/push/ready-pass.txt; R=/opt/albert-scott; D=/home/albert/do-it-v2
UNTIL=$(date -u -d '2026-11-12 23:30' +%s)   # extended 2026-11-04 16:55Z by the Thinker (operator: keep pushing); was 11-04 23:30   # extended 2026-11-03 19:30Z: the push continues overnight
log(){ echo "$(date -u +%H:%M:%S) $*" | tee -a $Q/rloop.log; }
tg(){ k=$(printf %s "$*" | md5sum | cut -c1-12); if grep -qx $k $Q/tg-seen 2>/dev/null; then log "TG(dup, not sent): $*"; return; fi; echo $k >> $Q/tg-seen; /home/albert/.do-it/telegram/tg-send.sh "Merge push: $*" >/dev/null 2>&1; log "TG: $*"; }  # operator 11:57Z: same message three times
names(){ printf "%s, " "$@" | sed "s/, $//"; }
tip(){ git -C /home/albert/.do-it/repos/albert-scott rev-parse refs/heads/master; }
IR=/home/albert/.do-it/repos/albert-scott
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
sync(){ git -C $R -c gc.auto=0 -c maintenance.auto=false fetch -q /home/albert/.do-it/repos/albert-scott "+refs/heads/l-spec-*:refs/integ/l-spec-*" "+refs/heads/master:refs/integ/master"; }
busy(){ pgrep -f "window_gate.py win-" >/dev/null; }
inflight_cand(){  # candidate sha of the window now running, if any
  w=$(pgrep -af "window_gate.py win-" | head -1 | grep -o 'win-[0-9]*-[a-z0-9]*'); [ -z "$w" ] && return
  grep -h '"window-candidate"' ~/.do-it/events/*.jsonl | grep "\"$w\"" | tail -1 | grep -o '"sha": "[0-9a-f]*"' | cut -d'"' -f4; }
done_or_queued(){  # shipped/dropped, already queued, or held for attention
  s=L-spec-$1
  grep -qhE "\"subject\": \"$s\".*\"type\": \"(shipped|dropped)\"|\"type\": \"(shipped|dropped)\".*\"subject\": \"$s\"" ~/.do-it/events/*.jsonl && return 0
  cat $Q/*.batch 2>/dev/null | cut -d" " -f2- | grep -qw "$1" && return 0   # Thinker 21:12Z: batch files hold bare numbers (2033/2041 rehearsed twice)
  grep -qw "^$1" $Q/attention.txt && return 0
  pgrep -af "window_gate.py" | grep -qw "$s" && return 0; return 1; }
is_mig(){ git -C $R diff --name-only $1...refs/integ/l-spec-$2 | grep -q '^api/alembic_supabase/versions/'; }
pick(){  sync  # next 4 from ready list: no shared files, at most one migration
  base=$1; got=(); files=""; mig=0
  files=$(git -C $R diff --name-only refs/integ/master...$base 2>/dev/null)   # files already changed by the stacked candidate: a spec touching them conflicts (1841 vs 1933, 14:36)
  git -C $R cat-file -e $base 2>/dev/null || git -C $R -c gc.auto=0 -c maintenance.auto=false fetch -q /home/albert/.do-it/repos/albert-scott $base
  for n in $(grep -oE '^[0-9]+' $READY); do
    done_or_queued $n && continue
    python3 /var/tmp/push/clean.py $n || continue
    git -C $R rev-parse -q --verify refs/integ/l-spec-$n >/dev/null || continue
    if ! git -C $R merge-tree --write-tree $base refs/integ/l-spec-$n >/dev/null 2>&1; then   # Thinker 2026-11-04 16:20Z: conflict precheck before a batch is formed
      grep -q "^$n " $Q/attention.txt 2>/dev/null || { echo "$n $(date -u +%H:%M) CONFLICT with ${base:0:12} (pick precheck; Thinker files the rework)|do not auto-release|" >> $Q/attention.txt; log "precheck: $n conflicts with ${base:0:12}; parked" >&2; }
      continue; fi
    f=$(git -C $R diff --name-only $base...refs/integ/l-spec-$n)
    [ -n "$(comm -12 <(echo "$files"|sort -u) <(echo "$f"|sort -u)|grep -v '^$')" ] && continue
    if is_mig $base $n; then [ $mig -eq 1 ] && continue; mig=1; fi
    [ ${#got[@]} -ge 4 ] && ! preclean $n && continue   # slots 5-6 only for specs the pre-check passed alone on this master
    got+=($n); files=$(printf '%s\n%s' "$files" "$f")
    if [ ${#got[@]} -ge 4 ]; then allclean=1; for g in "${got[@]}"; do preclean $g || allclean=0; done
      [ $allclean -eq 1 ] && [ ${#got[@]} -lt 6 ] && continue   # operator 2026-11-04 05:29 "Yes on all": up to 6 when every member is pre-checked clean
      break; fi
  done; echo "${got[@]}"; }
preclean(){  # spec $1 has a CLEAN row in the pre-check sweep that ran on the current master tree
  grep -qE "^$1 CLEAN" $Q/preguard.txt 2>/dev/null || return 1
  [ "$(tac $Q/preguard.log 2>/dev/null | grep -m1 'sweep on' | awk '{print $4}')" = "$(git -C $R rev-parse refs/integ/master | cut -c1-12)" ]; }
rehearse(){  # rehearse(base, specs...) -> writes queue file or moves culprits to attention
  base=$1; shift; specs=("$@"); args=$(printf 'L-spec-%s ' "${specs[@]}")
  log "rehearse on ${base:0:12}: ${specs[*]}"
  # Operator 2026-11-04 17:31Z "(a)": the full-CI rehearsal starts NOW, alongside the pre-flight (Yitzy #1272: CI idle 45%).
  # A red pre-flight cancels it; a green one just lets it run. The batch file exists from the start so the loop polls it.
  bf=$Q/$(date -u +%s).batch; echo "$base ${specs[*]}" > $bf
  sp=$(printf 'L-spec-%s,' "${specs[@]}"); sp=${sp%,}; cid=rh-$(date -u +%m%d-%H%M%S)
  (cd $D && DOIT_ACTOR=thinker DOIT_WINDOW_MAX_MERGES=${#specs[@]} PYTHONUNBUFFERED=1 nice ./doit gate-window $cid --rehearse --on $base --project albert-scott --specs $sp > $bf.ci 2>&1; echo "exit=$?" >> $bf.ci) &
  cipid=$!; echo $cipid > $bf.pid; log "full-CI rehearsal $cid started for ${specs[*]} (in parallel with the pre-flight)"
  out=$("$L/preflight.sh" $base $args 2>&1 | tail -40)
  tag=$(printf "%s\n" "${specs[@]}" | paste -sd-); rep=/var/tmp/push/rehearse-$tag.txt
  bad=$(grep -E '^(CONFLICT|OVERLAP|ALEMBIC-HEADS)' $rep)
  new=$(sed -n '/^NEW-FAILURES:/,/^DONE/p' $rep | grep -v -e NEW-FAILURES -e DONE)
  if [ -z "$bad" ] && [ -z "$new" ] && grep -q NO-NEW-FAILURES $rep; then
    log "CLEAN -> queued: ${specs[*]} (CI rehearsal $cid already running)"
    tg "pre-flight passed for $(names "${specs[@]}"); the full CI rehearsal has been running since the batch started. The window reuses it and lands in minutes."; return 0; fi
  log "NOT CLEAN: $(echo "$bad $new" | tr '\n' ' ' | cut -c1-400)"
  # cancel the parallel CI rehearsal: its python child, its GitHub runs (branch ends in -win<pid>), then the poller subshell
  pypid=$(pgrep -P $cipid -f window_gate.py | head -1)
  if [ -n "$pypid" ]; then kill $pypid 2>/dev/null; sleep 2
    (cd /home/albert/.do-it/repos/albert-scott && gh run list --limit 20 --json databaseId,headBranch --jq ".[] | select(.headBranch|endswith(\"-win$pypid\")) | .databaseId" 2>/dev/null | while read -r rid; do gh run cancel $rid >/dev/null 2>&1; done); fi
  kill $cipid 2>/dev/null; rm -f $bf $bf.ci $bf.cand; log "cancelled the parallel CI rehearsal $cid (pre-flight red)"
  if echo "$new" | grep -q RUN-BROKEN; then [ -n "$(find $Q/broken -mmin -60 2>/dev/null)" ] && { log "run broken again; quiet 10 min"; sleep 600; return 1; }; touch $Q/broken; tg "rehearsal of $(names "${specs[@]}") could not run its tests ($(echo "$new" | cut -c1-150)). Not blaming any spec; Thinker is checking."; sleep 600; return 1; fi
  if [ "$base" != "$(tip)" ]; then touch $Q/nostack-${base:0:12}; log "stacked rehearsal on ${base:0:12} not clean; no attribution, no more stacking on it; retry on master"; return 1; fi
  # attribute fast: run the failing ids on master and on master+each member in PARALLEL worktrees
  # (no PG lane needed); members that add failures are set aside, the rest ride on. Only when this
  # names nobody (e.g. PG-only failures) fall back to the serial alone-rehearsals below.
  if [ -n "$new" ] && ! echo "$new" | grep -q RUN-BROKEN; then
    ATTR_TESTS="$(echo "$new" | grep -v '^$' | sed 's/\[.*//' | sort -u | tr '\n' ' ')" timeout 1500 "$L/attr.sh" "${specs[@]}" >/dev/null 2>&1
    named=$(awk '($2=="NEW:" && $3!="none") || $2=="CONFLICT" {print $1}' /var/tmp/push/attr.txt 2>/dev/null)
    if [ -n "$named" ]; then
      for s in $named; do echo "$s $(date -u +%H:%M) attr: $(grep "^$s " /var/tmp/push/attr.txt | head -1 | cut -c1-160)" >> $Q/attention.txt; done
      log "attributed by parallel alone-runs: $(echo $named | tr '\n' ' ')"; new=""
    else log "parallel attribution named nobody; falling back to serial alone-rehearsals"; fi
  fi
  # attribute: a conflict names its spec; test failures -> rehearse each spec alone on base
  for s in "${specs[@]}"; do
    if echo "$bad" | grep -qE "^(CONFLICT|OVERLAP|ALEMBIC-HEADS) L-spec-$s\b"; then echo "$s $(date -u +%H:%M) $(echo "$bad"|grep -E "^(CONFLICT|OVERLAP|ALEMBIC-HEADS) L-spec-$s\b"|head -1)" >> $Q/attention.txt; continue; fi
    if [ -n "$new" ]; then
      [ -z "$(echo "$new" | grep -v '^$')" ] && continue   # every batch failure already reproduced by an earlier spec alone
      "$L/preflight.sh" $base L-spec-$s >/dev/null 2>&1
      grep -q NO-NEW-FAILURES /var/tmp/push/rehearse-$s.txt || echo "$s $(date -u +%H:%M) alone: $(sed -n '/^NEW-FAILURES:/,/^DONE/p' /var/tmp/push/rehearse-$s.txt | sed -n 2p)" >> $Q/attention.txt
      new=$(echo "$new" | grep -vxF -f <(sed -n '/^NEW-FAILURES:/,/^DONE/p' /var/tmp/push/rehearse-$s.txt | grep -v -e NEW-FAILURES -e DONE) || true)
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
log "rloop start (until $(date -u -d @$UNTIL +%FT%TZ))"
echo $$ > $Q/main.pid   # Thinker 2026-11-04: pid-only restarts keep CI pollers alive
while [ $(date +%s) -lt $UNTIL ] && [ ! -f $Q/stop ]; do
  derive_exit
  [ -f $Q/rehearsing ] && ! kill -0 "$(cat $Q/rehearsing 2>/dev/null)" 2>/dev/null && rm -f $Q/rehearsing   # stale marker after a crash
  if busy; then
    c=$(inflight_cand); [ -n "$c" ] && [ ! -f $Q/nostack-${c:0:12} ] && fill $c
    # a queued batch rehearsed on a different base than the in-flight candidate is stale
    sleep 15; continue; fi
  t=$(tip)
  if [ "$t" != "$(cat $Q/pushed 2>/dev/null)" ]; then  # a window advanced master: push it to GitHub
    r=$(cd $D/src && timeout 250 env -u DOIT_LEDGER_FILE python3 -c "import fold,push_origin; print(push_origin.run(fold.read_events()))" 2>&1 | tail -c 80)
    log "push ${t:0:12}: $r"; { echo "$r" | grep -q albert || [ "$(git -C /home/albert/.do-it/repos/albert-scott ls-remote origin refs/heads/master | cut -f1)" = "$t" ]; } && echo $t > $Q/pushed; fi
  if [ -f $Q/current ]; then cw=$(cat $Q/current); rm -f $Q/current
    res=$(grep -h "gate-window:" $Q/$cw.log | tail -1 | cut -c1-300)
    case "$res" in *"advanced master"*) tg "window $cw merged. $(echo "$res" | grep -o "shipped=.*" | tr -d "[]'\"" | sed 's/L-spec-//g')";;
      *) tg "window $cw did not merge: $(tail -2 $Q/$cw.log | tr '\n' ' ' | cut -c1-250)"
         # refused before CI ran (chain held, cap, base not releasable): the rehearsal still stands
         grep -qE "chain held|exceeds the window cap|base-not-releasable|lock" $Q/$cw.log && [ -f $Q/current.bat ] && { mv $Q/current.bat $Q/0000000000.batch; log "requeued rehearsed batch after refusal"; };; esac
    rm -f $Q/current.bat; fi
  h=$(ls $Q/*.batch 2>/dev/null | head -1)
  if [ -z "$h" ]; then fill $t; continue; fi
  read -r base specs < $h
  if ! same_tree $base $t; then log "head batch base ${base:0:12} tree != master ${t:0:12}; re-rehearse"; [ -s $h.cand ] && cat $h.cand >> $Q/cand-seen; cancel_ci $h; rm -f $h $h.ci $h.cand
    rehearse $t $specs; continue; fi
  if [ -f $h.ci ] && ! grep -q '^exit=' $h.ci; then   # full-CI rehearsal still running
    if [ $(ls $Q/*.batch 2>/dev/null | wc -l) -lt 2 ] && [ ! -f $Q/rehearsing ]; then c=$(find_cand $h)   # cap 2->3 (Thinker 2026-11-04 17:50Z, Yitzy note #1272: lane idle 45%)
      [ -n "$c" ] && [ ! -f $Q/nostack-${c:0:12} ] && { log "stacking next batch on ${c:0:12} (candidate of $(cut -d' ' -f2- $h))"; p=$(pick $c); [ -n "$p" ] && bgr $c $p; }; fi
    sleep 15; continue; fi
  if [ -f $h.ci ] && ! grep -q '^exit=0' $h.ci; then
    log "full-CI rehearsal not green: $(grep -E 'gate-window|FLAKE|reproduc|exit=' $h.ci | tail -4 | tr '\n' ' ' | cut -c1-400)"
    tg "full CI rehearsal of $specs was not green: $(grep -E 'reproduc|FLAKE|exit=' $h.ci | tail -3 | tr '\n' ' ' | cut -c1-300). Holding that batch; Thinker is checking."
    att=$(grep -oE '^(ATTRIBUTED|FALLBACK) L-spec-[0-9]+' $h.ci | grep -oE '[0-9]+$' | sort -u)
    # exit=143 = we cancelled it ourselves after a red pre-flight; the local attribution already parked the culprit
    # (Thinker 2026-11-05 06:30Z: this branch parked 1901/1993/2003 as "unattributed" although only 1772 failed)
    grep -q '^exit=143' $h.ci && [ -z "$att" ] && specs=""
    for s in $specs; do if echo "$att" | grep -qx $s; then echo "$s $(date -u +%H:%M) CI rehearsal attributed failure" >> $Q/attention.txt
      elif [ -z "$att" ]; then echo "$s $(date -u +%H:%M) CI rehearsal red, unattributed: Thinker releases clean ones" >> $Q/attention.txt; fi; done
    mv $h $h.red; mv $h.ci $h.red.ci; [ -s $h.cand ] && cat $h.cand >> $Q/cand-seen; rm -f $h.cand; continue; fi
  id=win-$(date -u +%Y%m%d)-r$(date -u +%H%M); sp=$(printf 'L-spec-%s,' $specs); sp=${sp%,}
  log "LAUNCH $id: $specs"; cp $h $Q/current.bat; rm -f $h $h.ci $h.cand; echo $id > $Q/current; tg "window $id started: $specs. CI takes about 40 minutes."
  (cd $D && DOIT_ACTOR=thinker DOIT_WINDOW_MAX_MERGES=$(echo "$sp" | tr "," "\n" | grep -c .) PYTHONUNBUFFERED=1 nice ./doit gate-window $id --project albert-scott --specs $sp > $Q/$id.log 2>&1; echo "exit=$?" >> $Q/$id.log) &
  sleep 30
  if ! busy; then log "$id did not hold the chain: $(tail -2 $Q/$id.log | tr '\n' ' ')"; sleep 30; fi
done
log "rloop end"
