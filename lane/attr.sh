#!/bin/bash
# attr.sh — run the named failing tests on master and on master+each spec alone; print who adds failures.
export LC_ALL=C; R=/opt/albert-scott; P=/opt/albert-scott/.venv/bin/python; OUT=/var/tmp/push/attr.txt; : > $OUT
TESTS="${ATTR_TESTS:-tests/ci/test_path_scope_coverage.py}"   # failing ids from the batch rehearsal (brackets stripped)
# Thinker 2026-11-05 07:00Z: run whole FILES, not ids. Rehearsal output can truncate an id ("...cache_k"); one
# unknown id makes pytest collect nothing, so every member read "NEW: none" and 2034 escaped attribution.
TESTS=$(for t in $TESTS; do echo "${t%%::*}"; done | sort -u | tr '\n' ' ')
git -C $R -c gc.auto=0 fetch -q /home/albert/.do-it/repos/albert-scott "+refs/heads/l-spec-*:refs/integ/l-spec-*" "+refs/heads/master:refs/integ/master"
run(){ (cd $1 && T=$(for f in $TESTS; do [ -e "$f" ] && echo "$f"; done) && [ -n "$T" ] && PYTHONPATH=api:. timeout 900 $P -m pytest -q -p no:cacheprovider --no-header -rfE $T 2>&1 | grep -E '^(FAILED|ERROR) ' | awk '{print $2}' | sort -u); }
W=/var/tmp/attr-base; git -C $R worktree add -q --detach $W refs/integ/master && base=$(run $W) && git -C $R worktree remove --force $W
echo "BASE fails: $(echo "$base" | tr '\n' ' ')" >> $OUT
one(){ s=$1; W=/var/tmp/attr-$s; git -C $R worktree add -q --detach $W refs/integ/master || { echo "$s WORKTREE-FAILED" >> $OUT; return; }
  if git -C $W merge -q --no-edit refs/integ/l-spec-$s >/dev/null 2>&1; then
    f=$(run $W); new=$(comm -23 <(echo "$f") <(echo "$base") | grep -v '^$' | tr '\n' ' ')
    echo "$s NEW: ${new:-none}" >> $OUT
  else echo "$s CONFLICT" >> $OUT; fi
  git -C $R worktree remove --force $W; }
for s in "$@"; do one $s & done; wait   # members in parallel: these runs use no PG lane
echo DONE >> $OUT
