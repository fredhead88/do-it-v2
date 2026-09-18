# Planner handover — L-planner-0017 → next Planner context (2026-09-18 ~06:00Z)

## State

- **L-charter-0023 is `l1-complete`.** Ten slots, ten specs, R1–R20 all covered, no orphans. Each
  spec was written, audited once and reworked once (D24 spent on all ten). Nothing is owed from a
  next Planner. The close — builds, grades, reviews, merges, sweep, charter-review, reap — is the
  Executor's.
- **One document, not two:** `content/plan-L-charter-0023.md` holds the unit blocks *and* the shared
  decisions. A `cut-written` event points at that same file (R73 workaround); anything
  pattern-matching for a `cut-*.md` will be wrong. `plan-written` was re-appended at 05:17Z after
  the round-two corrections — read the newest.
- ADRs `L-adr-0087`…`L-adr-0103`. **L-adr-0097 is AMENDED (reversed)**: no tsc step is added
  anywhere in this charter. The amendment is in both the ADR file and the Plan's Shared-decisions
  row — it was in the file only for several hours, and that cost a spec (see the trap below).

| Spec | Unit | Wave | Delivers |
|---|---|---|---|
| L-spec-0078 | only-a-gated-deploy-changes-the-box | 1 | R1, R2, R3, R19 |
| L-spec-0079 | every-manifest-row-proves-its-target | 1 | R5, R6, R7, R10 |
| L-spec-0080 | every-guard-test-executes-and-the-suite-finishes | 1 | R12, R13, R15 |
| L-spec-0087 | a-skipped-line-is-a-held-lock | 2 | R20 |
| L-spec-0088 | the-request-health-head-lands-on-one-route | 2 | R8 |
| L-spec-0090 | the-atlas-gate-runs-where-a-dsn-exists | 2 | R16 |
| L-spec-0091 | the-instinct-criteria-get-a-number | 2 | R18 |
| L-spec-0092 | a-type-error-and-a-comment-are-not-lint-reds | 2 | R11, R14 |
| L-spec-0093 | the-installers-leave-nothing-behind | 3 | R9, R10 |
| L-spec-0094 | the-required-set-grows-last | 3 | R4, R14, R17 |

`L-spec-0089` is **`closed-unbuilt`** — the first carve of the lint unit, killed after its audit
returned `bad_cut: true`. `L-spec-0082`…`0086` are **void** (five spawns died on an account weekly
limit, 2026-09-17T18:21Z); their partial files were never read or salvaged, and the work was
recommissioned on fresh ids.

Final mechanical pre-pass: requirement coverage `none`, undefined seams `none`, shared names
`none`, unit size `none`, acquisition trail `none`. Three same-wave footprint overlaps, all declared
and intentional: `deploy.sh` twice (wave 1, wave 2) and `docs/do-it/briefs/` (wave 2).

## READ THIS BEFORE THE BUILD — five things that override spec text

1. **No tsc step, anywhere (L-adr-0097 as amended).**
   `tests/ci/test_t2_next_build_supersedes_tsc.sh` is wired live into `predeploy-gate.yml:69-70`,
   green, and asserts the dashboard quality workflow **no longer runs `tsc --noEmit`** because
   `next build` is a strict superset. Adding the step flips that guard red. It is deliberately **out
   of every footprint here**. R11 is an *evidence* deliverable: a named master CI run and three
   closures (1329 ac2, 1330 ac9, 1334 ac5).
2. **`branch_protection.sh --apply` is out of scope.** Measured live 2026-09-18:
   `gh api repos/:owner/:repo/branches/master/protection` → `required_status_checks: **null**`,
   `enforce_admins: false`. Master has **never** had a required check, so `--apply` is not a 5→6
   growth — it is the first activation of required-status gating on a repo that takes direct pushes.
   Nothing in R4/R11/R14/R17 asks for it. It is **Q14** in the sweep, operator's call, default off.
   The undo, if it ever runs, is `PUT … required_status_checks: null`, **not** re-running `--apply`.
3. **The five `flock -w` sites keep their waits.** `deploy/cron/amazon_tier2_{seller,ads,pricing}.cron`
   (7200s) and `deploy.sh:5468` / `:5479` (1800s) gain `-E 99` and are **never** rewritten to `-n`.
   `-E` applies to a `-w` timeout (`man flock`; measured here: `flock -w 1 -E 99 <held> true` → 99).
   The in-scope ambiguous population is 33 in `deploy.sh` + 27 across 26 `.cron` files of 52.
4. **A name in `REQUIRED_CHECKS` is a JOB `name:`, never a workflow `name:`.**
   `predeploy-gate.yml:1` is the workflow `Predeploy Gate Tests`; the check-run context is the job at
   `:49`. Enrolling a workflow name creates a required context that never reports — the MISSING
   failure the unit exists to prevent.
5. **L-adr-0100's carried invariants still bind**: verification runs inside the builder's worktree
   with relative **target** paths (never `cd /opt/albert-scott`, never
   `PYTHONPATH=/opt/albert-scott`) — but the **interpreter is absolute**, because the packet builder
   refuses a bare `python3`/`bash`. Two auditors called the absolute interpreter a defect; both were
   rejected. No criterion writes to a DSN it did not create, no `alembic upgrade head` in a
   Verification block, no `FILE_SIZE_GUARD_OVERRIDE` (commit-global).

## Measurements that decide how this charter builds

- **The baseline's four figures.** `scripts/ci/date_casts_baseline.txt`: 98 physical lines, **94**
  entries, **73** unique keys under `LC_ALL=C` (a locale `sort -u` folds them to 69 — that is where
  the Plan's original wrong figure came from), matching **90** hits. Pin `LC_ALL=C` wherever a key
  count is taken.
- **The date-cast linter's comment strip dispatches by file extension**, not by "does it tokenize as
  Python" — 5 of the 7 `.sql` files in the scan set tokenize as Python without raising. The
  `# noqa: TZ001` escape is decided on the **raw** line before any stripping; it has zero live uses
  today, so a naive strip would make it silently inert. A token-exact strip loses none of the 90
  hits (simulated over all 1,583 scan-set files).
- **The Atlas sweep's invoker is known**: `/etc/cron.d/albert-scott-process-health`, `30 5 * * *`,
  under `flock -n -E 99 … cron_attest --job process-health-sweep`. A `crontab -u albert -l` cannot
  show it; `cron.d` files never appear in a user crontab.
- **`cron_attest` INSERTs a `cron_runs` row before it execs the child** (`:240-249`), so any
  criterion that runs an attested wrapper writes to production. The specs verify the wrapped scripts
  directly instead, and force an unresolvable DSN where the wrapper itself must run.
- **Enrolling `Repo lint guards` costs real money.** Removing its push `paths:` filter and the
  in-job `needs_run` gate restores per-push scheduling: ≈ +457 job-minutes/week, order +1,900/month,
  ~$15/mo at list rate on a private repo. Under the $300/yr-asks threshold, and recorded in the
  spec's `cost_path` rather than assumed away.
- **A9, observed and unexplained:** on master sha `d465e51ff` **all ten** check contexts failed in
  3 s with zero steps — a runner/account startup failure, not a code red. Do not misattribute it to
  this charter's enrolment work.

## Open with the operator

- **Blocking, still unanswered: R8's alembic route** — `request_health_buckets` is
  `deployment: prohibited` while `listing_authority_fn_defaults_v1` is `routed`, and the two are on
  disjoint branches (verified `is_ancestor` False both ways). `escalation-blocking` on the ledger,
  deadline **2026-09-19T12:00Z**. `L-spec-0088` is written against the routed-entry-only change and
  must not be widened before the ruling.
- **Answered during this pane:** `spec-closed L-spec-0089` (operator ledger, 05:43Z) — the
  escalation that asked for it is discharged.
- **Q1–Q13 owed** in the Plan's sweep, each with a default. **Q14 (new)** is the branch-protection
  activation question above.

## What this charter taught the pilot (R100–R102, pushed)

- **R100.** An ADR amended in its own file but **not** in the Plan's Shared-decisions table cost a
  whole spec. The packet builder inlines the *table*, never the ADR file, so the writer obeyed the
  stale row and the audit correctly called `bad_cut`. Two copies of a decision is the defect.
- **R101.** A rework packet is `prev_packet + fix_list`, so a Plan correction made *between* rounds
  never reaches the writer. Every rework this session carried a hand-appended
  "## Planner corrections" section; without it three specs would have reworked against figures this
  Plan had already fixed.
- **R102.** `spec-killed` is the spec-writer's *pre-flight* kill and changes no state; only
  `spec-closed` retires a written spec, and it is operator-only. So the Planner is told to
  "recommission the slot" on a `bad_cut` with no instrument to retire the old one.

**R87/R93 holds a third charter running.** Four of this round's findings were defects in the
Planner's own Plan — a cron inventory, a fixture grain, an unfalsifiable AC rule, and the stale ADR
row — and every one survived the fable plan-audit and was caught by a spec-auditor measuring against
HEAD. Every one was a number written down without running the command that produces it.

## Next

Do **not** start another charter from this context. Take the operator's named charter, else the
lowest-numbered `open` one.
