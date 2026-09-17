# Planner handover — L-planner-0016 → next Planner context (2026-09-17 ~15:15Z)

## State
- **L-charter-0022 is `L1-complete`.** Seven slots, seven specs, R1–R17 all covered. Each spec was
  written, audited once, and reworked once (D24 spent on all seven). Nothing is owed from a next
  Planner. The close — builds, grades, reviews, merges, sweep, charter-review, reap — is the
  Executor's.
- **One document, not two:** `content/plan-L-charter-0022.md` holds the unit blocks *and* the
  shared decisions. A `cut-written` event points at that same file (R73 workaround); anything
  pattern-matching filenames for a `cut-*.md` will be wrong.
- ADRs `L-adr-0065`…`L-adr-0086`. Measurement sidecars:
  `content/measurement-L-charter-0022-anchor-legs.md`, `content/seam-inventory-L-charter-0022.md`.
- **`l1-complete` is on the ledger TWICE** (15:13:59Z and 15:14:05Z), same content. A false-positive
  error check caused a needless retry. Harmless — but if anything downstream counts events rather
  than checking presence, that is where the 2 comes from.

| Spec | Unit | Wave | Delivers |
|---|---|---|---|
| L-spec-0064 | the-job-records-its-own-end | 1 | R1, R3, R4, R6 |
| L-spec-0065 | one-sign-rule-one-header-definition | 1 | R12, R14, R15 |
| L-spec-0066 | an-anchor-is-a-source-ingestion-id | 1 | R7, R8 |
| L-spec-0072 | the-envelope-and-the-atlas-store | 2 | R2, R5 |
| L-spec-0073 | the-lines-get-their-anchor-back | 2 | R9, R10 |
| L-spec-0074 | the-deposit-equals-the-header | 2 | R13, R16, R17 |
| L-spec-0075 | unanchored-growth-is-a-finding | 2 | R3, R11 |

Final mechanical audit: requirement coverage `none`, unit size `none`, undefined seams `none`,
shared names `none`, acquisition trail `none`. Two same-wave overlaps, both declared and
intentional: `cash_basis_etl.py` (wave 1) and `ops_backup_manifest.txt` (the two data-writing
units, per L-adr-0084).

## READ THIS BEFORE THE BUILD — four invariants that override spec text

1. **L-adr-0086 — Verification runs inside the builder's worktree.** On this box
   `/opt/albert-scott` is the **shared master checkout** (`~/.do-it/repos/albert-scott` symlinks to
   it); production is the droplet, a different machine — do **not** repeat this as a
   "tests ran against prod" finding. A chain that hardcodes `cd /opt/albert-scott` or
   `PYTHONPATH=/opt/albert-scott` escapes the worktree and grades master, not the commit: it can
   pass on code the builder never wrote and two concurrent builders race one tree (the R70
   cross-talk). Everything relative — `PYTHONPATH=.` / `.:api`. An absolute *interpreter* path is
   fine; it selects a toolchain, not a source tree.
   **Known carriers this overrides: `L-spec-0064` lines 305–306, `L-spec-0074` line 240.**
   0064 had spent its D24 round, so it was deliberately not re-dispatched.
2. **L-adr-0083 — no criterion or Verification step writes to a DSN it did not itself create.**
   `pg_engine_ready` → `pg_parity_dsn` → `resolve_live_db_dsn(prefer='parity')` falls back to
   `SUPABASE_DB_URL` — the live money DB — when `AS_LIVE_DB_TARGET`/`PG_PARITY_DB_URL` are unset.
   One spec had a non-dry-run CREATE+UPDATE graded through exactly that, while its prose claimed the
   fixture was scratch. No `alembic upgrade head`, no `--restore`, no migration in a Verification
   block, whatever comment sits beside it.
3. **L-adr-0085 — a new `pg_scratch` test file must be enrolled in the CI workflow step.** The
   default job runs `-m "not pg_scratch"`; scratch tests run in a step with a hand-maintained file
   list. An unenrolled marked file **runs in no job at all** and looks green. Self-detecting:
   `tests/test_818_guc_pinning.py:1478` AST-parses the workflow and goes red.
4. **L-adr-0080 — no `FILE_SIZE_GUARD_OVERRIDE`.** It is **commit-global**
   (`file_size_guard.sh:23` exits 0 before building any file set), so using it to land a three-line
   registration un-caps every other file in the same commit. Affected units instead hold
   `cross_validation.py` (2,494), `ingestion/pipeline.py` (1,684) and `runner.py` (2,829) at **net
   zero or fewer lines than HEAD**.

## The two measurements that decide how this charter builds

- **The nightly kill is external, and it signals first.** `_TIER2_WATCHDOG_ENVELOPE_MINUTES` kills
  nothing — it feeds `_unit_window()` for concurrency planning. The killer is a *different
  process*, `stuck_killer.py`, at `max(_REAP_THRESHOLDS_HOURS['amazon_runner_tier2']=8h,
  _CLIENT_SUBPROCESS_TIMEOUT_S=4h) + _KILL_GRACE_HOURS=0.5h = 8.5h` on a 30-min tick. Two unrelated
  constants, same number — an earlier `decision` event named the wrong one and is superseded on the
  ledger. **It signals SIGTERM with a 30-second grace before SIGKILL**, so the job *can* record its
  own end on the nights it dies. A terminal phase covering only clean exits does not deliver R1.
- **R14 is deliverable; the escalation branch is NOT triggered.** A spec-writer concluded "no single
  global sign rule can tie all five clients". The measurement under it reproduces
  (`refund_fee_reversal` positive on all 45 qur_life settlements, negative on all 93 of the other
  four) but the conclusion is wrong. The decisive test is the tie to Amazon's header:
  **goya 75/75, mouthwatchers 6/6, sable_rosenfeld 6/6, supersmile 6/6 — 93 of 93 tie. qur_life
  ties 0 of 44, and 44 of 44 break by exactly 2 × |refund_fee_reversal|.** A break of precisely
  twice the line amount is the signature of that one line carrying the wrong sign. So the rule is
  the one four clients already satisfy and qur_life's stored history is the lone outlier.
  **Consequence: do not wire a run-gate that refuses unless a rule ties all five clients as they
  stand** — qur_life cannot tie until repaired, so that gate deadlocks and the first `--execute`
  refuses forever. The repair is determinate and is gated by L-adr-0071's whole-client pre-image
  and operator-visible SQL.

## Open with the operator
- **Blocking, unanswered: R4's scope** — narrow (default, what the specs were written against) vs
  wide. `escalation-blocking` on the ledger, deadline 2026-09-18T12:00Z. The charter was rewritten
  under a running Planner; `doit audit` cannot see a requirement *widening* because coverage still
  reads `none`.
- **Owed Q1–Q6** in the Plan's sweep section, each with a default. **Q3 now has a measured answer**
  favouring "leave" (zero duplicate groups on all three static clients).
- **Q7 — the `ops_backup` manifest `origin_spec` is numeric** and no DO-IT v2 id can satisfy it.
  Default (amended): each unit uses **its lineage number** — `255` for the anchor backfill, `1178`
  for the header restore — since every committed row names the spec that created the table.
- **Q8 — DO-IT v2 ids vs host-project numeric guards.** Q7 is the first collision, not the last.
  Wants a brief.
- The disk escalation is **addressed** (box at 62%); recorded, not retracted.

## What this charter taught the pilot (R87–R89, pushed)
**The per-spec blind audit is the Plan's only *measured* check.** Of ~36 wave-2 findings, **four
were defects in the Planner's own Plan and ADRs**, and all four had survived the fable plan-audit
clean — because the plan-auditor reads documents against a charter while the spec-auditor measures
against HEAD. A fifth (the worktree escape) and a sixth (the CI enrollment hole) came from
*spec-writers reading their own drafts*, from no auditor at all.
**Read every finding asking "is this mine?" before handing it to the writer.**

## Next
Do **not** start another charter from this context. Take the operator's named charter, else the
lowest-numbered `open` one.
