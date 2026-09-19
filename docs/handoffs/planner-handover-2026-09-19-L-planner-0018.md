# Planner handover — L-planner-0018 → next Planner context (2026-09-19 ~20:20Z)

## State: L-charter-0024 is **NOT** L1-complete, and stopping was the correct outcome

Seven units cut, one document, one fable plan audit passed (`bad_cut: false`). **Wave 1's two
specs are written, audited once and reworked once — D24 spent on both.** Waves 2 and 3 are
**held on two `escalation-blocking` events**, both with a default, a revert and a deadline of
**2026-09-20T12:00Z**. `l1-complete` is deliberately absent: five of seven slots have no spec,
and appending it would be a lie the Executor's close row would act on.

| Slot | Spec | Wave | Delivers | State |
|---|---|---|---|---|
| the-run-bill-is-a-command-not-a-claim | **L-spec-0107** | 1 | R11 | written · audited · reworked · 9 ACs, 0 owed, 1 unknown |
| a-green-that-skipped-its-tests-is-not-a-green | **L-spec-0108** | 1 | R1 | written · audited · reworked · 9 ACs, 0 owed, 0 unknowns |
| one-master-push-fires-one-run-that-always-runs | — | 2 | R2, R5 | **held on Q1** |
| the-suite-runs-on-the-sha-that-deploys-and-the-gate-waits | — | 3 | R3, R4 | not commissioned (wave 2 first) |
| the-skip-shape-and-the-cancel-gap-cannot-come-back | — | 3 | R6 | not commissioned |
| pytest-finishes-before-the-deploy-does | — | 3 | R7 | **held on Q5** |
| the-month-reads-under-forty | — | 3 | R8 | not commissioned |

R9 and R10 are delivered by `decision` events on the charter, not by specs — the charter assigns
both to the Planner explicitly. Both are on the ledger (R10 at 19:16Z, R9 at 19:30Z).

**The Plan is one document**: `content/plan-L-charter-0024.md`. `content/cut-L-charter-0024.md`
is a **symlink** to it so the by-convention packet builders resolve; a `cut-written` event points
at the plan file. `plan-written` was re-appended after the audit round — **read the newest**.
ADRs `L-adr-0104`…`L-adr-0114`. The round-one slot files are kept at
`content/slot-L-spec-0107.md` and `content/slot-L-spec-0108.md` because `doit packet spec-writer`
needs them to build a rework packet.

## The two questions that hold this charter

**Q1 — the run-count criterion cannot be satisfied as written.** Twelve workflows fire on a master
push that the deploy gate does not consume. Measured 2026-09-17 they add 0.8 runs per push and 33
of 418 billed minutes (8%). The charter's review path says *a master push fires at most two runs*,
and a reviewer counts runs, not consumed runs. **No default satisfies the charter**: either fold
the twelve into the consolidation — which grows that unit from a seven-file footprint to nineteen,
a different unit — or amend the done-condition. The recorded default is leave-them-alone **and
record the clause as knowingly unmet**, so the charter-review reads a declared miss.

**Q5 — R7's 20-minute target is not reachable on the runner R9 defaults to.** Probe `L-probe-0009`
measured `pytest-xdist` on 18.6% of the suite: **`-n 2` = 1.10×**, **`-n 4` = 0.88× — slower than
serial**. Serial `Run tests` is 40.4 min inside a 44.6-min job, so 20 minutes needs ~2.6×. The
probe's own diagnosis: `-n 2`'s shortfall is per-worker import cost (+72 s of CPU for the same
tests) which amortises on a 40-minute run, while `-n 4`'s regression is contention on the build
box (load average 4.43 on 4 pinned cores) and proves nothing about a clean runner. Its honest
answer is **20–30 minutes best case, straddling the target**. Three ways out: a larger
GitHub-hosted runner, a self-hosted VPS at ~$7.50/month (which **adds an installer unit** to this
Plan), or relax the target and let the builder report the number. Default: relax.

## READ THIS BEFORE COMMISSIONING WAVE 2 — measurements that override the charter's text

Every one of these is a command, not a reading. Four contradict the charter directly.

1. **Five live `needs_run` sites, not six.** `dashboard-pr-quality.yml`'s three occurrences are
   comments; spec 1344 R2 already removed its in-job gate. The five are `api-security-check.yml`,
   `migration-lint.yml`, `python-tests.yml`, `repo-lint-guards.yml`, `verification-loop-tests.yml`.
2. **Two of the three "laundered shas" already fail today's gate.** `d8f706a62` and `906380474`
   carry `TypeScript · Biome · Tests · File Sizes` as `cancelled`, which `predeploy_gate.sh:751`
   already reads as FAILING. Only `498f0dde4` reads all-five-green.
3. **A defect the charter does not name: check 5 selects the OLDEST run of a duplicated check.**
   On `498f0dde4` there are two `pytest` check-runs — a 39-second `success` and a 39-minute
   `failure`. GitHub returns them newest-first; `by_name[r.get("name")] = r` keeps the last
   element, the oldest. L-adr-0109 fixes it; it survives the skip gate's removal.
4. **Cost scales with pushes, not commits.** 27 distinct master pushes on 2026-09-17 carrying 202
   runs — 7.48 runs per push. The charter's ~200 commits/day is the wrong unit and is unused.
5. **`gh api --paginate` silently stops at 1,000 items.** `created=2026-09-10..2026-09-18&event=push`
   has `total_count: 1306`; one `--paginate` call returns exactly 1000, with no error. **This
   Planner's own first measurement was truncated** — it reported 71 non-master push runs; day-sliced,
   the true figure is **129, all Secret Scan, across 95 branches**. L-adr-0114 binds every run
   listing to day-slicing plus a `total_count` reconciliation. `event=push` is equally load-bearing.
6. **The `timing` API is useless here.** `billable.UBUNTU.total_ms: 0` while `run_duration_ms` is
   non-zero. Billed minutes come from per-job `started_at`/`completed_at`, ceilinged, skipping
   `conclusion: skipped` jobs.
7. **The laundering is live today**: master run `35310846573` (2026-09-18) concluded `pytest`
   `success` in 0.6 min with `Run tests = skipped`.
8. **25 of 29 workflows carry `cancel-in-progress: true`** — not 26; the four without are the three
   deploy workflows and `run-agents.yml`.
9. **Do not add a `tsc` check-run name.** L-adr-0097 was amended during L-charter-0023 to add no
   `tsc` step anywhere, and `tests/ci/test_t2_next_build_supersedes_tsc.sh` is wired live into
   `predeploy-gate.yml:69-70` asserting it. 0023's R11 is an *evidence* deliverable. 0023's R14
   name **is** `"Repo lint guards"`.
10. **`branch_protection.sh` applies its array to `required_status_checks.contexts` (`:104-131`).**
    A deploy-only name there deadlocks every master PR — commit `95567b9ea`'s bug by another
    route. Only `REQUIRED_CHECKS_PUSH` is mirrored into the applied array (L-adr-0104).

## Traps this pane hit, so the next one does not

- **`doit packet plan-auditor --stage plan` is still dead code** (R91). The documented fallback
  `scripts/seat/mkpacket.py` needs a `cut-<charter>.md` that the one-document convention does not
  produce — symlink it — and then emits the Plan **twice** and ships the Planner's carve rationale
  verbatim into a packet whose whole premise is blindness. **Build the plan-audit packet by hand**
  and assert the absence of the justification markers before dispatching. Filed as **R103**.
- **`doit packet spec-auditor` refused both specs' Verification blocks** — the 8th and 9th
  occurrence of R81/R92, still unstated in any contract. The two rules are: an **absolute**
  interpreter path, and **one gated `&&` chain** with each line ending in `&&`, no trailing
  backslash, no `;`, no `||`. Fixed mechanically rather than by re-dispatch, so the D24 round
  stayed available for the audit; recorded as a `decision` with backups.
- **`doit packet spec-writer` needs `content/slot-<spec>.md` to exist** for a rework packet. Round
  one's slot is the Planner's and lives only in its context unless written to that path. Write it
  there at dispatch time.
- **`Consumes: nothing` creates phantom undefined seams** in `doit audit plan`. A unit with no seam
  simply omits the label — but `mkslot.py` then crashes on the missing `Consumes:`. Build slot
  packets by hand.
- **A seat sub-agent that starts background work ends its turn without stamping.** The probe needed
  four nudges. Tell every server explicitly: *do not end your turn before the stamp has run*.
  Filed as **R104**.

## Next

Answer Q1 and Q5, then commission wave 2 (one unit), then wave 3 (four units). Do **not** append
`l1-complete` before all seven slots have a spec. The two wave-1 specs are already the Executor's
— they depend on nothing and can build now.
