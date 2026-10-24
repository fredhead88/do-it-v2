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
`doit health [--json]` reports finishing, intake, usage percentages, disk and tokens_per_live_spec.
Tokens per live spec average uncached input plus output for measured specs shipped in seven days.
unmeasured_specs counts excluded specs; spawn-stale has no usage and understates spend.
The vendor routing above assigns authors to Codex and judging roles to Claude seats.
The Executor uses claude-sonnet-5-5; judgment calls go to a claude-opus-5-5 seat sub-agent.
Sonnet trial: log tokens_per_live_spec.claude via doit health --json at start and 14 days later;
record both readings and judgment outcomes in lesson lines (axis=K); a measured rise ends it.

## Lane rules

Source: `lane_rules.toml`. Name | action | actor | limit_min.

- build | dispatch-builder | tick | 30
- grade | dispatch-grader | tick | 15
- review | dispatch-reviewer | tick | 15
- gate | doit-gate | tick | 30
- enqueue | queue-add | tick | 60
- merge | window-run | executor | 120
- idle | none | none | 60
- rebuild | dispatch-builder | tick | 15
- regrade | dispatch-grader | tick | 15
- close | spec-killed | tick | 60
- wave-held | none | none | 10
- dependency-held | none | none | 10
- sweep-check | sweep-check | tick | 120

## Cron manifest

Source: `crons.toml`. Name | schedule | where | path; command on following lines.

- tick | */{tick_min} * * * * | user | {doit}
  DOIT_ROOT={root} {doit} cron-run tick -- {doit} tick >> {root}/logs/tick.log 2>&1
- look | */10 * * * * | user | {doit}
  {doit} cron-run look -- {doit} look >> {root}/logs/look.log 2>&1
- tmp-reaper | */5 * * * * | user | {doit}
  {doit} cron-run tmp-reaper -- {doit} reap-tmp
- lessons-digest | 7 * * * * | user | {repo}/scripts/lessons_digest.py
  {doit} cron-run lessons-digest -- {repo}/scripts/lessons_digest.py >> {root}/lessons-digest.log
  2>&1
- backup-watch | * * * * * | user | {doit}
  {doit} cron-run backup-watch -- {doit} backup watch --for 60 >> {root}/backup.log 2>&1
- ledger-repack | 47 4 * * * | user | {doit}
  {doit} cron-run ledger-repack -- {doit} backup maintain >> {root}/logs/ledger-repack.log 2>&1
- ledger-snapshot | */10 * * * * | user | /var/backups/do-it/snapshot.sh
  {doit} cron-run ledger-snapshot -- /var/backups/do-it/snapshot.sh >/dev/null 2>&1
- sweep-owed | */30 * * * * | user | {doit}
  {doit} cron-run sweep-owed -- {doit} sweep-owed >> {root}/logs/sweep-owed.log 2>&1
- auto-deploy | */5 * * * * | cron.d | /opt/albert-scott/venv/bin/python
  albert {doit} cron-run auto-deploy -- /opt/albert-scott/venv/bin/python
  /opt/albert-scott/scripts/ops/auto_deploy.py --once >> /var/log/albert-scott/auto-deploy-run.log
  2>&1
- owed-digest | 0 6 * * * | user | {doit}
  {doit} cron-run owed-digest -- {doit} owed-digest --alert >> {root}/logs/owed-digest.log 2>&1
- owed-digest-json | */30 * * * * | user | {doit}
  {doit} cron-run owed-digest-json -- {doit} owed-digest --json-only >>
  {root}/logs/owed-digest-json.log 2>&1
- portal-latency-daily | 30 6 * * * | cron.d |
  /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh
  albert {doit} cron-run portal-latency-daily -- /bin/bash
  /opt/albert-scott/scripts/deploy/portal_check_daily_wrapper.sh
- suite-daily | 40 5 * * * | user | /usr/bin/python3
  {doit} cron-run suite-daily -- /usr/bin/python3 {repo}/src/suite_run.py >>
  {root}/logs/suite-daily.log 2>&1
- health-alarms | */10 * * * * | user | {doit}
  {doit} cron-run health-alarms -- {doit} health-alarms >> {root}/logs/health-alarms.log 2>&1
- ci-shadow | */10 * * * * | user | {doit}
  {doit} cron-run ci-shadow -- {doit} ci-shadow >> {root}/logs/ci-shadow.log 2>&1
- balance | */10 * * * * | user | {doit}
  {doit} cron-run balance -- {doit} balance >> {root}/logs/balance.log 2>&1
- stuck-digest | 15 6 * * * | user | {doit}
  {doit} cron-run stuck-digest -- {doit} stuck-digest --send >> {root}/logs/stuck-digest.log 2>&1
- gh-bill | 15 6 * * * | user | {doit}
  {doit} cron-run gh-bill -- {doit} gh-bill >> {root}/logs/gh-bill.log 2>&1
- seat-usage | */15 * * * * | user | {doit}
  {doit} cron-run seat-usage -- {doit} seat-usage >> {root}/logs/seat-usage.log 2>&1
- base-red-nightly | 20 3 * * * | user | /usr/bin/python3
  {doit} cron-run base-red-nightly -- /usr/bin/python3 {repo}/src/base_red.py nightly albert-scott
  >> {root}/logs/base-red-nightly.log 2>&1

## Tick event types

Source: literal `dispatch.emit` and `look.emit_once` calls in `src/tick.py`,
plus every action in `lane_rules.toml`.

- autodispatch-failed; autodispatched; charter-closed; charter-proving; charter-reopened;
  cron-missing; dispatch-builder; dispatch-grader; dispatch-reviewer; doit-gate; look-stale; none;
  proving-check-failed; proving-overdue; queue-add; spawn-stale; spec-killed; sweep-check; tick;
  window-run; worktree-reaped
