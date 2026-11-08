#!/bin/bash
# preflight.sh <base-sha> L-spec-N [L-spec-N...]
# Cheap local checks; the full CI rehearsal still gates the exact merged tree.
set -u; export LC_COLLATE=C LANG=C.UTF-8
BASE=$1; shift; SPECS=("$@")
L=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
R=${PREFLIGHT_REPO:-/opt/albert-scott}
INTEG=${PREFLIGHT_INTEG:-/home/albert/.do-it/repos/albert-scott}
P=${PREFLIGHT_PY:-/opt/albert-scott/.venv/bin/python}
SRC=${PREFLIGHT_SRC:-$L/../src}
export PREFLIGHT_CONTENT=${PREFLIGHT_CONTENT:-/home/albert/.do-it/content}
OUTDIR=${PREFLIGHT_OUT:-/var/tmp/push}
WTPARENT=${PREFLIGHT_WT:-/var/tmp}
# Inspect variable names only; never inspect or print database connection values.
for preflight_env in $(compgen -e); do
  case "$preflight_env" in
    PG*|SUPABASE_DB_URL*|AS_LOCAL_PG_*|DATA_MCP_DSN*|DATABASE_URL|GITHUB_ENV|GITHUB_PATH|PYTEST_ADDOPTS|PYTEST_PLUGINS|PYTHONPATH) unset "$preflight_env" ;;
  esac
done
# Nested pytest gets its own worktree import path below. Inherited -p options,
# plugin names and runner module paths belong only to the dispatch process.
export SUPABASE_DB_URL='' SUPABASE_DB_URL_DIRECT='' SUPABASE_DB_URL_RO=''
export SUPABASE_DB_URL_MIGRATIONS='' DATABASE_URL=''
export NODE_ID_SECRET=ci-pg-parity-node-id-secret CI_BUILD_BOX=false
export PATH="$(dirname "$P"):$PATH"
TAG=$(printf "%s\n" "${SPECS[@]}" | sed "s/L-spec-//" | paste -sd-)
mkdir -p "$OUTDIR" "$WTPARENT"
OUT=$OUTDIR/rehearse-$TAG.txt
: > "$OUT"; log(){ echo "$*" | tee -a "$OUT"; }
WC=$WTPARENT/pf-cand-$TAG; WB=$WTPARENT/pf-base-$TAG
ACTIVE=""
cleanup(){
  if [ -n "$ACTIVE" ]; then
    kill -TERM "$ACTIVE" 2>/dev/null
    wait "$ACTIVE" 2>/dev/null
  fi
  git -C "$R" worktree remove --force "$WC" >/dev/null 2>&1 || true
  git -C "$R" worktree remove --force "$WB" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Remove stale worktrees before creating this batch's candidate.
cleanup
# Wait in the parent shell so its signal traps run immediately. The worker
# forwards termination to timeout, which terminates and reaps its test group.
timed(){
  local child
  timeout "$@" & child=$!
  trap 'kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 143' TERM
  wait "$child"
}
capture(){
  local output=$1; shift
  "$@" > "$output" & ACTIVE=$!
  wait "$ACTIVE"
  ACTIVE=""
}
GUARDS="tests/ci/test_no_remote_ref_tests.py api/tests/profit_v2/test_1069_classification_drift.py api/tests/profit_v2/test_173_legacy_aggregate_exclusion_lint.py tests/amazon/test_ads_source_rule.py tests/test_795_cron_pythonpath_parity.py tests/test_activation_manifest_completeness.py tests/ci/test_977_selfhosted_runner_switch.py tests/test_820_no_abs_test_imports.py tests/test_496_r3_guard.py api/tests/profit_v2/test_read_model_refresh.py::test_ac9_deploy_sh_net_zero tests/deploy/test_protected_stamp.py::test_ac12_deploy_sh_line_count_not_grown tests/deploy/test_ship_snapshot.py::test_ac14_line_counts tests/test_207_monitoring_denoise.py::TestDigestFoldCronStale::test_query_cron_runs_stale_returns_data_trust_when_failed tests/pipelines/amazon/test_retire_product_objection_record.py::test_ac1_no_tracked_reader tests/test_821_empty_ledger_guard.py::test_ac8_every_non_mention_invoker_is_named_in_the_inventory tests/test_soda_scan_retired.py::test_active_execution_paths_have_no_soda_references tests/ci/test_path_scope_coverage.py tests/health/test_portal_route_inventory.py api/tests/test_550_reviewer_live_floor.py tests/ci/test_pg_scratch_file_list_parity.py tests/test_activation_fixture_parity.py tests/ci/test_608_amazon_activations.py tests/test_deploy_activation_assertion.py tests/ci/test_runner_select.py tests/ci/test_shard_layout.py tests/test_activation_row_truth.py tests/pipelines/amazon/test_tier1_orders_window_sizing.py::test_ac8_module_limit_and_untouched_sources tests/test_775_feed_health_expectations.py tests/services/data_mcp/test_core_pages_served.py"
GUARDS=${PREFLIGHT_GUARDS-$GUARDS}
git -C "$R" cat-file -e "$BASE" 2>/dev/null || git -C "$R" -c gc.auto=0 -c maintenance.auto=false fetch -q "$INTEG" "$BASE"
git -C "$R" -c gc.auto=0 -c maintenance.auto=false fetch -q "$INTEG" "+refs/heads/l-spec-*:refs/integ/l-spec-*" "+refs/heads/master:refs/integ/master"
git -C "$R" worktree add -q --detach "$WC" "$BASE" || exit 1
files=""
conflict=""
for s in "${SPECS[@]}"; do
  b=refs/integ/${s,,}
  f=$(git -C $R diff --name-only $BASE...$b)
  dup=$(comm -12 <(echo "$files" | sort -u) <(echo "$f" | sort -u) | grep -v '^$')
  [ -n "$dup" ] && log "OVERLAP $s: $dup"
  files=$(printf '%s\n%s' "$files" "$f")
  git -C $WC merge -q --no-edit $b >/dev/null 2>&1 || { log "CONFLICT $s"; git -C $WC merge --abort; conflict=1; }
done
# 1974 (2026-11-04 02:44): a conflict was reported only after 38 min of tests; fail fast, the driver parks the named spec.
if [ -n "$conflict" ]; then log "NEW-FAILURES:"; log DONE; exit 0; fi
# Plain-shard simulation (static): a changed test that needs a scratch Postgres (short_transactions_scratch /
# ScratchRequired / AS_LOCAL_PG_ADMIN) but carries no pg_scratch marker passes here (lanes have PG) and
# fails in CI's plain shards (1932, 2026-11-03 05:12). Name the spec before any test runs.
pgm=""
for s in "${SPECS[@]}"; do b=refs/integ/${s,,}
  for f in $(git -C $R diff --name-only $BASE...$b | grep -E '^(tests|api/tests)/.*test_[^/]*\.py$'); do
    src=$(git -C $R show $b:$f 2>/dev/null) || continue
    old=$(git -C $R show $BASE:$f 2>/dev/null); if [ -n "$old" ] && echo "$old" | grep -qE "short_transactions_scratch|ScratchRequired|AS_LOCAL_PG_ADMIN" && ! echo "$old" | grep -qE "pg_scratch"; then continue; fi   # pre-existing on the base (1988, 16:11): not this spec's to fix
    if echo "$src" | grep -qE "short_transactions_scratch|ScratchRequired|AS_LOCAL_PG_ADMIN" && ! echo "$src" | grep -qE "pg_scratch"; then pgm="$pgm
OVERLAP $s: $f needs a scratch Postgres but has no pg_scratch marker (fails in CI plain shards)"; fi
  done
done
if [ -n "$pgm" ]; then echo "$pgm" | grep -v '^$' | tee -a $OUT; log "NEW-FAILURES:"; log DONE; exit 0; fi
# Writes-grant overlap: the window refuses two specs that grant the same path (prewindow deferral,
# 23:00 2026-11-02: 1715+1901), so fail the batch here, naming the later spec, before any test runs.
ov=$(cd "$SRC" && /usr/bin/python3 - "${SPECS[@]}" <<'PY'
import os, sys, merge_gate
seen = {}
for s in sys.argv[1:]:
    try: g = merge_gate.grant_from_text(open(os.path.join(os.environ['PREFLIGHT_CONTENT'], f'{s}.md')).read())
    except Exception: continue
    for path in g:
        if path in seen: print(f"OVERLAP {s}: Writes grant {path} also granted by {seen[path]}")
        else: seen[path] = s
PY
)
if [ -n "$ov" ]; then echo "$ov" | tee -a $OUT; log "NEW-FAILURES:"; log DONE; exit 0; fi
if [ -d "$WC/api/alembic_supabase" ]; then
  heads=$(cd "$WC/api" && "$P" -m alembic -c alembic_supabase.ini heads 2>/dev/null | wc -l)
  [ "${heads:-0}" -gt 1 ] && log "ALEMBIC-HEADS $heads"
fi
# Only changed test files are added to the guard list. Explain every exclusion
# in the report consumed by the lane, including the count and the reason.
own=""; excluded=0
while IFS= read -r path; do
  [ -n "$path" ] && [ -f "$WC/$path" ] || continue
  if grep -qE 'pg_scratch|short_transactions_scratch|ScratchRequired|AS_LOCAL_PG_ADMIN' "$WC/$path"; then
    excluded=$((excluded + 1))
    log "excluded test: $path (pg_scratch|short_transactions_scratch|ScratchRequired|AS_LOCAL_PG_ADMIN; full CI covers scratch Postgres)"
  else
    own=$(printf '%s\n%s' "$own" "$path")
  fi
done < <(echo "$files" | grep -E '(^|/)test_[^/]*\.py$' | sort -u)
log "excluded: $excluded changed test files requiring scratch Postgres; full CI covers them"
tgt="$GUARDS $own"
run(){ (
  cd "$1" || exit 1
  targets=()
  for t in $2; do [ ! -e "${t%%::*}" ] || targets+=("$t"); done
  if [ ${#targets[@]} -eq 0 ]; then echo 'no tests ran'; echo 'PYTEST-RC=5'; exit 0; fi
  PYTHONPATH=api:. timed "${PREFLIGHT_PYTEST_TIMEOUT:-600}" nice "$P" -m pytest -q -p no:cacheprovider --no-header -rfE \
    --maxfail=10 \
    --deselect tests/test_795_cron_pythonpath_parity.py::test_ac10_cron_lint_clean_over_every_committed_cron_template \
    --deselect tests/ci/test_impacted_tests.py::test_ac6_real_history \
    --deselect tests/ci/test_private_pg_ci.py \
    --deselect api/tests/profit_v2/test_prod_instruments.py::test_golden_compare_no_leaked_worktree_after_failed_revision \
    --deselect api/tests/profit_v2/test_prod_instruments.py::test_degraded_run_cannot_pass \
    "${targets[@]}" 2>&1
  echo "PYTEST-RC=$?"
); }
failures(){ grep -E '^(FAILED|ERROR) ' "$1" | awk '{print $2}' | sort -u; }
base_tree(){
  [ -d "$WB" ] || git -C "$R" worktree add -q --detach "$WB" "$BASE" || exit 1
}
log "targets: $(echo $tgt)"
capture "$OUT.cand" run "$WC" "$tgt"
log "cand: $(sed '/^PYTEST-RC=/d' "$OUT.cand" | tail -1)"
timed_out=""
if grep -q '^PYTEST-RC=124$' "$OUT.cand"; then
  log PREFLIGHT-TIMEOUT; timed_out=1
fi
failures "$OUT.cand" > "$OUT.cfail"
new=""
if [ -s "$OUT.cfail" ]; then
  base_tree
  failing_files=$(sed 's/::.*//' "$OUT.cfail" | sort -u)
  capture "$OUT.base" run "$WB" "$failing_files"
  failures "$OUT.base" > "$OUT.bfail"
  new=$(comm -23 "$OUT.cfail" "$OUT.bfail")
  log "base: $(sed '/^PYTEST-RC=/d' "$OUT.base" | tail -1)"
fi
broken=""
grep -qE 'Interrupted:|errors? during collection|no tests ran' "$OUT.cand" && broken="candidate test run stopped before running tests"
[ -n "$broken" ] || [ -n "$timed_out" ] || grep -qE '[0-9]+ passed' "$OUT.cand" || broken="candidate ran zero passing tests"

# Dashboard changes run their own and adjacent tests. --no-install prevents npx from
# fetching dependencies; use the already provisioned dashboard dependencies.
vt=$(echo "$files" | grep -E '^dashboard/src/.*\.test\.tsx?$' | while IFS= read -r path; do
  [ ! -f "$WC/$path" ] || echo "${path#dashboard/}"
done | sort -u)
vsrc=$(echo "$files" | grep -E '^dashboard/src/.*\.tsx?$' | grep -vE '\.test\.tsx?$' | sed 's#^dashboard/##' | xargs -r -n1 dirname | sort -u)
for d in $vsrc; do
  ls "$WC/dashboard/$d/"*.test.ts "$WC/dashboard/$d/"*.test.tsx "$WC/dashboard/$d/__tests__/"*.test.ts "$WC/dashboard/$d/__tests__/"*.test.tsx 2>/dev/null | sed "s#$WC/dashboard/##"
done | sort -u > "$OUT.vt.near"
vt=$(printf '%s\n%s\n' "$vt" "$(cat "$OUT.vt.near")" | grep -v '^$' | sort -u)
vcount=$(echo "$vt" | grep -c .)
if [ "$vcount" -gt 40 ]; then
  log "vitest excluded: $((vcount - 40)) target files beyond 40-file cap; full CI covers them"
fi
vt=$(echo "$vt" | head -40)
vnew=""
if [ -n "$vt" ]; then
  vrun(){ (
    cd "$1/dashboard" || exit 1
    targets=()
    for t in $vt; do [ ! -f "$t" ] || targets+=("$t"); done
    if [ ${#targets[@]} -eq 0 ]; then echo 'VITEST-RC=0'; exit 0; fi
    [ -L node_modules ] || [ -e node_modules ] || ln -s "$R/dashboard/node_modules" node_modules
    NO_COLOR=1 timed 900 nice npx --no-install vitest run --reporter=verbose --testTimeout=20000 "${targets[@]}" 2>&1
    echo "VITEST-RC=$?"
  ); }
  vfail(){
    sed -nE 's/^[[:space:]]*×[[:space:]]*//p; s/^[[:space:]]*FAIL[[:space:]]+/file: /p' "$1" |
      sed -E 's/[[:space:]]+[0-9.]+[[:space:]]*m?s$//' | sort -u
  }
  capture "$OUT.vcand" vrun "$WC"
  vfail "$OUT.vcand" > "$OUT.vcfail"
  if ! grep -q '^VITEST-RC=0$' "$OUT.vcand"; then
    # Preserve infrastructure/collection failures even when vitest prints no ids.
    [ -s "$OUT.vcfail" ] || echo 'RUN-BROKEN: vitest did not complete successfully' > "$OUT.vcfail"
    base_tree
    if [ -d "$WB/dashboard" ]; then capture "$OUT.vbase" vrun "$WB"; else echo 'VITEST-RC=0' > "$OUT.vbase"; fi
    vfail "$OUT.vbase" > "$OUT.vbfail"
    if ! grep -q '^VITEST-RC=0$' "$OUT.vbase" && [ ! -s "$OUT.vbfail" ]; then
      echo 'RUN-BROKEN: vitest did not complete successfully' > "$OUT.vbfail"
    fi
    vnew=$(comm -23 "$OUT.vcfail" "$OUT.vbfail" | sed 's/^/vitest: /')
  fi
  log "vitest targets: $(echo $vt | cut -c1-300)"
  log "vitest new failures: $(echo "$vnew" | grep -c .)"
fi
new=$(printf '%s\n%s\n' "$new" "$vnew" | grep -v '^$')
if [ -n "$broken" ]; then
  log "NEW-FAILURES:"
  log "RUN-BROKEN: $broken: $(grep -E '^ERROR .*(collect|\.py -)' "$OUT.cand" | head -3 | tr '\n' ' ')"
elif [ -n "$new" ]; then log "NEW-FAILURES:"; log "$new"
else log "NO-NEW-FAILURES"; fi
log DONE
