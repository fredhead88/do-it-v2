# How DO-IT works

Generated from repository code and config by `doit howto`; regenerate when sources change.

## Roles

Source: `agents/*.md` and `machine/models.toml`.
- builder: codex
- charter-reviewer: seat
- executor: pane
- grader: seat
- owed-sweeper: seat (default)
- owed-verifier: seat (default)
- plan-auditor: seat
- planner: pane
- probe: codex
- relay: seat (default)
- research: codex
- reuse-scout: seat
- reviewer: seat
- spec-auditor: seat
- spec-writer: codex
- thinker: pane

SD10 records `tokens = {vendor, input, cached_input, output, reasoning}` on terminal events.
Vendors are `codex` or `claude`; input includes cached input; absent usage is unknown.
`doit health [--json]`: finishing, intake, usage percentages, disk and tokens_per_live_spec.
Seven-day shipped mean: uncached input + output; unmeasured_specs excluded; spawn-stale unknown.
Authors: Codex; judges: Claude seats. Executor: claude-sonnet-5-5; judgment: claude-opus-5-5 seat.
Sonnet trial: log tokens_per_live_spec.claude via doit health --json at start and 14 days later;
record both readings and judgment outcomes in lesson lines (axis=K); a measured rise ends it.

## Lane rules

Source: `lane_rules.toml`. Name | action | actor | limit_min.
- build | dispatch-builder | tick | 30; - grade | dispatch-grader | tick | 15
- review | dispatch-reviewer | tick | 15; - gate | doit-gate | tick | 30
- enqueue | queue-add | tick | 60; - merge | window-run | executor | 120; - idle | none | none | 60
- rebuild | dispatch-builder | tick | 15; - regrade | dispatch-grader | tick | 15
- close | spec-killed | tick | 60; - wave-held | none | none | 10
- dependency-held | none | none | 10; - sweep-check | sweep-check | tick | 120

## Cron manifest
Source: `crons.toml`. Name | schedule | where | path, then command.
- tick | */{tick_min} * * * * | user | {doit}  DOIT_ROOT={root} {doit} cron-run tick -- {doit} tick
  >> {root}/logs/tick.log 2>&1; look | 33 * * * * | user | {doit}  {doit} cron-run look -- {doit}
  look >> {root}/logs/look.log 2>&1; tmp-reaper | */15 * * * * | user | {doit}  {doit} cron-run
  tmp-reaper -- {doit} reap-tmp
  lessons-digest | 7 * * * * | user | {repo}/scripts/lessons_digest.py  {doit} cron-run
  lessons-digest -- {repo}/scripts/lessons_digest.py >> {root}/lessons-digest.log 2>&1
  backup-watch | */15 * * * * | user | {doit}  {doit} cron-run backup-watch -- {doit} backup watch
  --for 60 >> {root}/backup.log 2>&1; ledger-repack | 47 4 * * * | user | {doit}  {doit} cron-run
  ledger-repack -- {doit} backup maintain >> {root}/logs/ledger-repack.log 2>&1
  ledger-snapshot | 2 * * * * | user | {repo}/scripts/snapshot.sh  {doit} cron-run ledger-snapshot
  -- {repo}/scripts/snapshot.sh >/dev/null 2>&1; sweep-owed | 11 0,6,12,18 * * * | user | {doit}
  {doit} cron-run sweep-owed -- {doit} sweep-owed >> {root}/logs/sweep-owed.log 2>&1
  verify-owed | 8,23,38,53 * * * * | user | {doit}  {doit} cron-run verify-owed -- {doit}
  verify-owed >> {root}/logs/verify-owed.log 2>&1
  auto-deploy | */5 * * * * | cron.d | /opt/albert-scott/venv/bin/python  albert {doit} cron-run
  auto-deploy -- /opt/albert-scott/venv/bin/python /opt/albert-scott/scripts/ops/auto_deploy.py
  --once >> /var/log/albert-scott/auto-deploy-run.log 2>&1; owed-digest | 0 6 * * * | user | {doit}
  {doit} cron-run owed-digest -- {doit} owed-digest --alert >> {root}/logs/owed-digest.log 2>&1
  owed-digest-json | 17 * * * * | user | {doit}  {doit} cron-run owed-digest-json -- {doit}
  owed-digest --json-only >> {root}/logs/owed-digest-json.log 2>&1
  portal-latency-daily | 30 6 * * * | cron.d |
  /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh (paused)  albert {doit} cron-run
  portal-latency-daily -- /bin/bash /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh
  suite-daily | 40 5 * * * | user | /usr/bin/python3  {doit} cron-run suite-daily --
  /usr/bin/python3 {repo}/src/suite_run.py >> {root}/logs/suite-daily.log 2>&1
  health-alarms | 6,36 * * * * | user | {doit}  {doit} cron-run health-alarms -- {doit}
  health-alarms >> {root}/logs/health-alarms.log 2>&1; ci-shadow | */10 * * * * | user | {doit}
  {doit} cron-run ci-shadow -- {doit} ci-shadow >> {root}/logs/ci-shadow.log 2>&1
  balance | 8 * * * * | user | {doit}  {doit} cron-run balance -- {doit} balance >>
  {root}/logs/balance.log 2>&1; stuck-digest | 15 6 * * * | user | {doit}  {doit} cron-run
  stuck-digest -- {doit} stuck-digest --send >> {root}/logs/stuck-digest.log 2>&1
  gh-bill | 15 6 * * * | user | {doit}  {doit} cron-run gh-bill -- {doit} gh-bill >>
  {root}/logs/gh-bill.log 2>&1; seat-usage | 12,27,42,57 * * * * | user | {doit}  {doit} cron-run
  seat-usage -- {doit} seat-usage >> {root}/logs/seat-usage.log 2>&1
  base-red-nightly | 20 3 * * * | user | /usr/bin/python3  {doit} cron-run base-red-nightly --
  /usr/bin/python3 {repo}/src/base_red.py nightly albert-scott >> {root}/logs/base-red-nightly.log
  2>&1; base-red-nightly-collect | 40 * * * * | user | /usr/bin/python3  {doit} cron-run
  base-red-nightly-collect -- /usr/bin/python3 {repo}/src/base_red.py nightly-collect albert-scott
  >> {root}/logs/base-red-nightly-collect.log 2>&1
  doit-base-red-nightly | 20 2 * * * | user | /usr/bin/python3  {doit} cron-run
  doit-base-red-nightly -- /usr/bin/python3 {repo}/src/base_red.py nightly do-it-v2 >>
  {root}/logs/doit-base-red-nightly.log 2>&1
  pane-watchdog | */5 * * * * | user | {repo}/src/pane_watchdog.py  DOIT_ROOT={root} {doit} cron-run
  pane-watchdog -- {repo}/src/pane_watchdog.py
  executor-wake | */5 * * * * | user | {repo}/scripts/executor-wake.sh  DOIT_ROOT={root} {doit}
  cron-run executor-wake -- {repo}/scripts/executor-wake.sh
  pace-sample | 8,18,28,38,48,58 * * * * | user | /usr/bin/python3  {doit} cron-run pace-sample --
  /usr/bin/python3 {repo}/src/pace.py sample >> {root}/logs/pace-sample.log 2>&1
  stall-sample | 3 * * * * | user | /usr/bin/python3  {doit} cron-run stall-sample --
  /usr/bin/python3 {repo}/src/stall_sample.py sample >> {root}/logs/stall-sample.log 2>&1
  spec-preflight | 4,9,14,19,24,29,34,39,44,49,54,59 * * * * | user | /usr/bin/python3  {doit}
  cron-run spec-preflight -- /usr/bin/python3 {repo}/src/spec_preflight.py tick >>
  {root}/logs/spec-preflight.log 2>&1; ledger-rollup | 20 4 * * * | user | {doit}  {doit} cron-run
  ledger-rollup -- nice -n 19 {doit} ledger-rollup >> {root}/logs/ledger-rollup.log 2>&1
  flow-health-daily | 30 0 * * * | user | /usr/bin/python3  {doit} cron-run flow-health-daily --
  /usr/bin/python3 {repo}/src/flow_health.py >> {root}/logs/flow-health.log 2>&1
  scratch-db-reaper | 17 * * * * | user | {doit}  {doit} cron-run scratch-db-reaper -- {doit}
  reap-scratch-db; worktree-reaper | */5 * * * * | user | {doit}  {doit} cron-run worktree-reaper --
  {doit} reap-worktrees >> {root}/logs/worktree-reaper.log 2>&1
  claude-autoswitch | */5 * * * * | user | /usr/bin/python3  DOIT_ROOT={root} {doit} cron-run
  claude-autoswitch -- /usr/bin/python3 {repo}/scripts/claude-autoswitch.py >/dev/null 2>&1
  master-full | */5 * * * * | user | /usr/bin/python3  {doit} cron-run master-full --
  /usr/bin/python3 {repo}/src/master_full.py albert-scott >> {root}/logs/master-full.log 2>&1
  merge-news | */1 * * * * | user | {doit}  DOIT_ROOT={root} {doit} cron-run merge-news -- {doit}
  merge-news flush >> {root}/logs/merge-news.log 2>&1
  thinker-inbox | */5 * * * * | user | /usr/bin/python3  DOIT_ROOT={root} {doit} cron-run
  thinker-inbox -- /usr/bin/python3 {repo}/src/thinker_inbox.py

## Tick event types
Source: literal `dispatch.emit`/`look.emit_once` calls in `src/tick.py`, `lane_rules.toml` actions.
- autodispatch-failed; autodispatch-skipped; autodispatched; charter-closed; charter-proving;
  charter-reopened; cron-missing; dispatch-builder; dispatch-grader; dispatch-reviewer; doit-gate;
  escalation-blocking; look-stale; next-step-held; none; proving-check-failed; proving-overdue;
  queue-add; spawn-failed; spawn-stale; spec-killed; sweep-check; tick; window-run; worktree-reaped

## Member verdicts
Source: `look.toml` `[merge_queue]` and `src/window_members.py`.
- After a red window with unnamed owners, each remaining member is verified alone on the window
  base, at most 4 CI runs at once (member_verify_parallel).
- Each run appends `member-verdict {spec, window, tree, state, url, nodes}`. Drop red members with
  their failing nodes; green and undetermined members stay eligible for the next window.
- Green member-verdict or window-tree-verified for required checks on the exact tree is reused for
  24 hours (verdict_fresh_hours). Reject undetermined, malformed, stale or future-dated evidence.

## Branch refresh
Before rework: isolated rebase, Verification/guards, adoption, bundle, re-gate; max 3/spec/day.
- `src/direct_merge.py`: merge_one handles one do-it-v2 queue row: refuse overlapping dirty paths,
  gate, require unchanged main/branch shas, merge --no-ff, then emit shipped. A moved ref requires
  re-gating; a failed merge is aborted. cli_gate uses doit gate. Queue selection and tick wiring
  belong to the caller.
