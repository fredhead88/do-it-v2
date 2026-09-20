# Planner handover — L-planner-0018 → next Planner context (2026-09-20 ~05:30Z)

## State: L-charter-0024 is **L1-complete**

Seven units, seven specs, **84 acceptance criteria**, every one written, audited once and reworked once — D24 spent on all
seven. R1–R8 and R11 are covered by specs; **R9 and R10 are `decision` events**, because the charter
assigns both to the Planner explicitly. All seven sweep questions are closed. No escalation on this
charter is open. **Nothing is owed from the operator.**

| Spec | Unit | Wave | Delivers | ACs | owed |
|---|---|---|---|---|---|
| L-spec-0107 | the-run-bill-is-a-command-not-a-claim | 1 | R11 | 9 | 0 |
| L-spec-0108 | a-green-that-skipped-its-tests-is-not-a-green | 1 | R1 | 9 | 0 |
| L-spec-0116 | one-master-push-fires-one-run-that-always-runs | 2 | R2, R5 | 15 | 2 |
| L-spec-0117 | the-suite-runs-on-the-sha-that-deploys-and-the-gate-waits | 3 | R3, R4 | 19 | 2 |
| L-spec-0118 | the-skip-shape-and-the-cancel-gap-cannot-come-back | 3 | R6 | 13 | 0 |
| L-spec-0119 | pytest-finishes-before-the-deploy-does | 3 | R7 | 11 | 2 |
| L-spec-0120 | the-month-reads-under-forty | 3 | R8 | 8 | 1 |

The Plan is **one document**: `content/plan-L-charter-0024.md`. `content/cut-L-charter-0024.md` is a
**symlink** to it. `plan-written` was re-appended many times — **read the newest**. ADRs
`L-adr-0104`…`L-adr-0115`; the round-one slot files are kept at `content/slot-L-spec-01NN.md`
because `doit packet spec-writer` needs them to build a rework packet.

Final mechanical pre-pass: undefined seams **none**, shared names **none**, acquisition trail
**none**, requirement coverage complete but for R9/R10 (decisions, expected). Unit size is
`undetermined` for two units **because their files do not exist yet** — those units create them.
That is structural, not an unmeasured input, and it is the one place "undetermined" here is not a
finding.

## THE CHARTER WILL NOT CLOSE AT MERGE, and that is correct

**7 owed ACs against `DOIT_K=1`.** `fold.l2_complete` requires `count(shipped-owed-evidence) <= K`,
so the close row cannot fire early. Every owed AC is structurally unobservable pre-merge: whether
the consolidated workflow's check-run names report `success` rather than `skipped`/`cancelled`
needs a real post-merge master push; the caching and `cron-lint` savings need a full day of live
traffic. **A CI-cost charter cannot prove a cost reduction before it runs.** The criteria carry
`wake_at` stamps; L-spec-0117's were re-deferred to 2026-10-15/17 as *floors, not predictions*.
Do not raise `DOIT_K` to make this close. The specs merge and deploy on the normal path; only the
charter close waits.

## Measurements that override the charter's text — every one is a command

1. **The bill, measured against GitHub's own API** (`/users/fredhead88/settings/billing/usage`),
   not inferred: **rate is exactly $0.006000/min** in all three months. Full-month **net**: July
   **$7.71** (4,303.00 min), August **$85.00** (17,174.67 min), September MTD **$98.64** (19,534→
   19,570 min over 20 days, **~977/day**), projecting **~$160/month**. Trend is **+59% min/day
   Aug→Sep**, driven by this pilot's own CI volume.
2. **The allowance is NOT a flat 3,000 min / $18.00.** Observed $18.108 / $18.048 / $18.564 —
   the discount is applied per line-item and the roundings accumulate. `max(0, min-3000)*0.006`
   **overstates** net by up to ~$0.60/mo and must be labelled an approximation.
3. **The $85 target is CALIBRATED, not derived.** August is an exact natural experiment:
   **17,174.67 billed minutes = $85.00 net**. Budget = **≤17,175 min/month ≈ 565/day** against a
   current 977/day — **a 42% cut**. Never re-derive `85/0.006+3000 = 17,167`; the measured month
   falsifies it. **Never hard-grep a September figure** — the month is open and drifted 36 minutes
   inside one hour.
4. **Master DOES have branch protection** — `required_pull_request_reviews`, `required_signatures`,
   `allow_force_pushes: false`, `allow_deletions: false` are all live. Only `required_status_checks`
   is absent. Writing the arrays **adds a status check to an already-protected branch**. Still:
   write the arrays, do **not** run `--apply`.
5. **`gh api --paginate` silently stops at 1,000 items** (`total_count: 1306`, no error). L-adr-0114
   binds every run listing to day-slicing plus a `total_count` reconciliation. `event=push` is
   equally load-bearing. **This Planner's first measurement was truncated by it.**
6. **The `timing` API is useless here**: `billable.UBUNTU.total_ms: 0` while `run_duration_ms` is
   non-zero. Billed minutes come from per-job `started_at`/`completed_at`, ceilinged, skipping
   `conclusion: skipped` jobs.
7. **GitHub bills per JOB-minute and each preserved check-run name IS a job** with its own runner
   and checkout. Consolidation cuts *runs*, not minutes. 27% of the cheap set is setup/checkout/
   install and 27.5% is ceil-to-the-minute waste — **both paid per push**. Cost scales with
   **pushes (27/day)**, not commits (~200/day).
8. **The builder box and CI resolve DIFFERENT MAJOR pytest versions.** `api/pyproject.toml:39`
   pins `>=8.0.0,<9.0.0` and is the only declaration anywhere; the box's venv has **9.0.3**,
   outside its own repo's pin. Filed as a brief; out of scope here.
9. **Five live `needs_run` sites, not six.** **25 of 29** workflows carry `cancel-in-progress`,
   not 26. **Check 5 selects the OLDEST duplicate check-run** (GitHub returns newest-first;
   `by_name[...] = r` keeps the last) — on `498f0dde4` a 39-second `success` masked a 39-minute
   `failure`. L-adr-0109 fixes it.

## Two defects the specs found that the charter never named — both are the charter's own fix

1. **`pytest-xdist -n 2` never prints `collected N items`** — it prints `N workers [M items]`. The
   job-summary's `grep -oE '^collected [0-9]+ item'` would have reported **every successful
   parallel run as "DID NOT RUN."** A new false signal, manufactured by the change that exists to
   remove false signals. Fixed in-footprint by the R7 unit.
2. **The new nightly run would have been HOLLOW.** On a `schedule` event `github.event.before` is
   absent → `BASE=""` → `git diff` returns 0 files → `needs_run=false` → every expensive step skips
   → the nightly reports `success` having run no tests. The BASE conditional is copy-pasted at
   **three** sites (`:78-96`, `:252-261`, `:438-456`), not one. Fixed; AC16 now asserts the
   `Run tests` step actually executed.

**Probe `L-probe-0009`'s two open items are now ANSWERED by observation** (it rightly refused to
conclude either from code-reading): the fail-fast stop signal **does** propagate across xdist
workers for not-yet-dispatched work but **does not** preempt an in-flight item on a sibling worker —
the probe's code-reading hypothesis was too pessimistic. Codified as a permanent regression test.
`--dist loadfile`/`loadscope` remain unreached and unowed.

## Traps this pane hit, so the next one does not

- **ROOT CAUSE of R81/R92, ten occurrences in (now R109):** `doit packet` strips `#` comments
  **without respecting quotes**, so `grep -q '^# ' file &&` truncates to `grep -q '^` and the
  refusal then says the line "does not end in `&&`" — describing a state the file does not exhibit.
  Any Verification block that greps for a markdown heading trips it.
- **Absolute interpreter paths are a VERIFICATION-BLOCK rule only.** Saying it unscoped in a slot
  packet made a writer offer `/opt/albert-scott/.venv/bin/python` as a guard's runtime mechanism on
  `ubuntu-latest`, which would have shipped a guard that **crashes on every push** — fail-open, in
  the unit whose purpose is fail-closed. An instruction followed correctly that yields a defect is a
  **packet bug**, invisible to the writer by construction.
- **`mkpacket.py` ships the Planner's carve rationale into the blind plan audit and ships the Plan
  twice** (R103). Build the plan-audit packet by hand and assert the absence of the markers.
- **`doit packet spec-writer` needs `content/slot-<spec>.md` to exist** for a rework packet.
- **`Consumes: nothing` creates phantom undefined seams**; `mkslot.py` then crashes on the missing
  label. Build slot packets by hand.
- **A `correction` from a Planner pane is silently dropped** — `fold.EMITS["correction"] ==
  {"operator"}`, and `doit append` still **exits 0** (R107). Use `decision`.
- **Tell every server: do not end your turn before the stamp has run** (R104).

## Open, and not mine

**`L-spec-writer-0194` is a stranded seat packet on L-charter-0023** — `spawn-started` 04:47:49Z,
no terminal event, no `.output.json`, **no live waiter**. Dispatched by another pane, so under the
serving rule it has no server. `relay.pending_packets()` is global and `pane_end` refuses while any
packet is pending, so **it blocks `doit pane-end` for every pane on this box**. Filed as a `brief`
on L-charter-0023. Not served and no terminal event written from here: both would cross the serving
boundary. Recurrence of R90/R97 — still no supported reconcile path.

## Three briefs filed for future charters

- **The VPS** — the operator's named future project. ~$7.50/mo flat replaces a ~$160/mo metered
  bill and is the only lever that also fixes R7's runtime.
- **Dashboard PR Quality** — 163 billed min/day, **51% of the cheap set**, the single largest line,
  and already the only consumed workflow that caches.
- **Push batching** — ~6–8 master pushes/day instead of 27 reaches $40 with **no infrastructure**.
  The highest-leverage item on the list.

## Next

Do **not** start another charter from this context. `L-charter-0025` is pull-throttled behind
`L-charter-0021`. Take the operator's named charter, else the lowest-numbered `open` one.
