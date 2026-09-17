# Planner handover — L-planner-0014 → next Planner context (2026-09-17 ~09:10Z)

## State
- **L-charter-0021 is `L1-complete`.** Ten slots, ten specs, every requirement R1–R16 covered.
  Nothing is owed from a next Planner. The close — spec-audits, builds, grades, reviews, merges,
  sweep, charter-review, reap — is the Executor's.
- Cut: `content/cut-L-charter-0021.md` — **ten units, two waves.** Wave 1 is the three units every
  other unit reads (`the-fold-and-the-board`, `pane-identity`, `carry-over`); wave 2 is the seven
  consumers, each on a footprint of its own. Zero same-wave footprint overlap; all seven pre-pass
  checks read `none` at both stages at close.
- Plan: `content/plan-L-charter-0021.md` (nine sections + a tenth holding the lost cut-audit
  findings verbatim). ADRs `L-adr-0035`…`L-adr-0045`.
- Probe: `content/probe-L-charter-0021/findings.md` (`L-probe-0007`, **`complete: false`**).
  Read it before touching R1 or R13 — see "what the probe could not reach".

| Spec | Unit | Wave | ACs | Owed |
|---|---|---|---|---|
| L-spec-0039 | the-fold-and-the-board | 1 | 10 | 0 |
| L-spec-0040 | pane-identity | 1 | 9 | 1 |
| L-spec-0041 | carry-over | 1 | 9 | 1 |
| L-spec-0045 | the-executor-supervises-itself | 2 | 9 | 2 |
| L-spec-0046 | the-lane-without-a-wait | 2 | 10 | 0 |
| L-spec-0047 | conflict-is-a-re-dispatch | 2 | 9 | 0 |
| L-spec-0048 | the-one-document-audit | 2 | 7 | 0 |
| L-spec-0049 | the-planner-owns-the-audit | 2 | 9 | 1 |
| L-spec-0050 | concurrent-work-is-not-a-failed-spawn | 2 | 5 | 0 |
| L-spec-0051 | the-contracts | 2 | 20 | 0 |

## Read this before you trust `doit events L-charter-0021`

**Every one of this charter's ten spec-writer spawns, and its cut-audit, was recorded
`spawn-failed` by the wrapper's repo-status check** — `why: "repo status changed across a
non-builder spawn"` — because L-charter-0020's build was moving the shared working tree the whole
time. The work was fine: every output card validated VALID and every spec body is on disk.

With no `spec-written` event the fold derives each spec **`void`** and drops it out of the charter
(`fold.py:580`). So the Planner appended `spec-written` itself, per spec, with every field copied
verbatim from that spawn's own validated output card and a `why` naming the failed spawn.
**`spec-written` is absent from the fold's `EMITS` table, so it is open to any actor (§2.5)** —
this is a recorded act by `planner`, not a forgery of `spec-writer`. The terminal `spawn-failed`
events stand beside it; both are true, and `doit spend` will show ten failed spawns that produced
ten good specs. `correction` is operator-only, so nothing can tidy this.

**`L-spec-0050` is the fix**, and it was added to the cut by this incident rather than by an
auditor: R7 says overlap never blocks a dispatch, and a wrapper that fails a *reader* because a
*writer* was working is that rule missed. Three older `decision` events in the ledger already
hand-repair the same pattern. Pilot R70 has the measurement.

## What the probe could not reach, and what that cost
- **Measured for real:** `-n, --name` exists (R12). `claude agents --json` and
  `~/.claude/sessions/<pid>.json` carry name, `nameSource`, contract, cwd, busy/idle status, tmux
  coordinates and `messagingSocketPath` — seven live entries at run time, including two operator
  panes it never touched. **This changed the cut**: `live_panes` reads those files joined to the
  ledger, because the ledger cannot know a pane is alive.
- **Not reached, 0 of 3 and 0 of 4 trials:** the `Stop`-hook turn loop (R1) and live message
  delivery (R13). Six real denials of ambient-credentialed `claude` launches from the nested
  probe context, including a no-hooks control — so the trigger is ambient credentials, not hooks.
  `charter-gap` declared. **The prior probe (`L-probe-0006`) did not hit this wall from what
  should have been an equivalent context, and nobody can explain the difference.**
- Consequences, both recorded as ADRs and taken as sweep defaults: **L-adr-0036** ships R1 on the
  measured end-and-restart channel, so no spec depends on a hook; **L-adr-0040** makes every
  criterion needing a live pane an **owed** criterion with a top-level observation. Four owed
  criteria across three specs, all of that shape.

## Traps measured this session (pilot record R70, R71)
- **R70** — the concurrent-build spawn failure above.
- **R71** — `stamp.sh` needs `<turns> <duration_ms> <subagent_tokens>`, and the only source is the
  Agent hand-back, which can arrive *after* the detached waiter's cap expires. Stamping late loses
  the spawn; stamping early writes zeros. Several spawns here carry `turns=0, subagent_tokens=0`
  for that reason, and `doit spend` will read them as free.
- **`doit test` is red at HEAD only because `DOIT_PROJECT` is set.** `test_fold.py:31` builds a
  scratch ledger whose events carry no `project`, and `fold.read_events()` filters on the
  environment variable at import — so the suite asserts `set()` and dies. With `DOIT_PROJECT`
  unset it passes 104 checks. One spec-writer (`L-spec-0050`) measured this as a red baseline and
  narrowed its own Verification section because of it. **A grader or builder that runs the suite
  in a scoped shell will see a false red.** This is not this charter's to fix; it is worth a brief.
- **`doit packet spec-writer` still truncates sibling `Produces:` lists to the first line** (R68's
  second half; single-line `Footprint:` avoids the first half). Every round-one packet here carries
  a hand-appended **Complete seam inventory** block, `content/seam-inventory-L-charter-0021.md`,
  named in each packet as the authority. Do the same until R68 is fixed.
- **Two audit rounds, 23 findings, no overlap, both `bad_cut: false`.** The cut was rewritten twice
  — once before the Plan against the cut-audit, once after against the plan-audit. More evidence
  for §3.6's two audits and against extending the one-round cap to cuts (cf. R63).

## Cross-charter, unresolved by design (L-adr-0044)
`src/up.py`, `src/think.py`, `src/fold.py`, `agents/planner.md` and `doit` are in both this
charter's and L-charter-0020's footprints. Every unit branches off `master` at dispatch; conflicts
are builder re-dispatches, never waits. **If `L-spec-0047`/`L-spec-0045` have not landed when the
first conflict happens, it is resolved by hand once, by whoever integrates, and that is worth
recording rather than hiding.** One live contradiction to reconcile at merge: `L-spec-0031`
(charter 20) names the Planner pane by **charter id**; R12 and `L-spec-0045` name it by **ledger
stem**. Two spec-writers flagged it independently.

## Owed at close
Two questions, both on the ledger as `question` events with a default, a deadline
(2026-09-18T06:00Z) and a revert. **Neither is an `escalation-blocking`, deliberately**: by §8.7's
own test neither changes the cut and neither is irreversible, and an escalation would take the
whole charter off the tick's lane to settle a one-line env change and a D96 approval.

- **Q1 — which Executor builds these.** The running Executor is scoped `DOIT_PROJECT=albert-scott`
  and the fold filters on it, so a `do-it-v2` spec is invisible to it. The same question has stood
  open on L-charter-0020 since ~06:35Z. **Default: dispatch as `do-it-v2` AND unset `DOIT_PROJECT`
  for the Executor pane** — the charter's own constraint says the fold renders all projects when it
  is unset. The previous default ("dispatch as do-it-v2") was *not* a default; it was the condition
  that hides the specs, and the plan-audit said so.
- **Q2 — D96 approval of the probe residue.** Defaults are L-adr-0036 and L-adr-0040 above.
- Twelve further owed questions with defaults are in the Plan's sweep section, named for veto.

## Next
Do **not** start another charter from this context. L-charter-0020 and L-charter-0021 are both
`L1-complete` and neither has landed; **do not plan charter N+2 against fiction.** The next Planner
should take the operator's named charter, else the lowest-numbered `open` one — 0015 through 0019
are open and none has been read by a Planner.
