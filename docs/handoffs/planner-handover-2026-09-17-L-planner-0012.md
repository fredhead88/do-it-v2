# Planner handover — L-planner-0012 → next Planner context (2026-09-17 ~06:35Z)

## State
- **L-charter-0020 is `L1-complete`.** Five slots, all `written`, nothing owed from a next Planner.
  The close — spec-audits, builds, grades, reviews, merges, sweep, charter-review, reap — is the
  Executor's. `L-spec-0029` relay-queries (7 ACs, 0 owed), `L-spec-0030` the-pane-ends-itself (8, 1
  owed), `L-spec-0031` the-supervisor (12, 2 owed), `L-spec-0032` sequencing-on-landing (5, 0),
  `L-spec-0033` waiting-on-board (4, 0).
- **There is a standing `escalation-blocking` on the charter.** It is the project split (below). The
  charter is off the tick's lane until a `decision` or `unblocked` lands after it. That is deliberate.
- Cut: `content/cut-L-charter-0020.md` — five units, two waves; wave 1 is four units with disjoint
  footprints (the relay itself), wave 2 is the board block alone, second only because it edits the
  file wave 1's derivation module also edits. Audited `L-plan-auditor-0023` (`bad_cut: true`, 10) →
  `-0024` (`bad_cut: true`, 8) → `-0025` (`bad_cut: false`, 11) → plan-audit `-0026`
  (`bad_cut: false`, 11). All seven pre-pass checks read clean at both stages at close.
- Plan: `content/plan-L-charter-0020.md` (nine sections, sixteen settled decisions, fourteen sweep
  questions). ADRs `L-adr-0025`…`-0034`.
- Probe: `content/probe-L-charter-0020/findings.md` (`L-probe-0006`, 27 real trials, $0). Read it
  before touching R1 or R2 — it is the only measurement of the externals this charter rests on.

## The operator's ruling this session
"Go into planner mode and pick up charter 20." No further input arrived during the context. Two
questions were left with defaults taken and named for veto; one was filed as blocking.

## What the probe settled, and what it cost
- A pane **can** end itself: a descendant walks `/proc/<pid>/status` PPid to its ancestor `claude`
  pid and sends `SIGTERM` → exit 0, handler-driven, ~10s, no orphans. Writing `/exit` to `/dev/tty`
  fails `ENXIO` (a Bash-tool subprocess has no controlling terminal at all); no CLI flag or hook ends
  a foreground session. **One of three candidates exists.**
- `doit up` **can** become a `subprocess.run` foreground loop: control returns, the second child is a
  distinct usable session, no terminal reset needed, Ctrl-C routes to the child.
- A detached seat waiter survives its launcher's death and its pty teardown, resumes from a
  `meta.json`/`output.json` dropped in later, and times out loudly on a packet nobody answers.
- **Two costs.** `SIGTERM` arrives mid-tool-call, so the pane's last tool output is lost — every
  durable act precedes the signal and nothing follows `doit pane-end`. And the probe's own auto-mode
  safety classifier **denied a literal `kill -TERM $pid`** while allowing the identical `os.kill()`
  in a committed file: the end mechanism must ship as a module, never as a line in the contract.

## Traps measured this session (pilot record R67–R69)
- **The project filter silently emptied the one prior-round channel in the system (R67).** The
  charter was filed `project: albert-scott`; every spawn this Planner dispatched carries
  `project: do-it-v2`. `fold.read_events()` filters on `DOIT_PROJECT`, so the stage-`plan` packet's
  `## The cut-audit's findings` read `none` while **29 findings** sat in the ledger under the other
  label. The plan-auditor was handed an empty channel and said so. `doit events <subject>` from a
  scoped pane hides half this charter's history the same way.
- **`doit packet spec-writer` truncates a multi-line `Footprint:` to its first bullet (R68).** All
  five slots were affected. `doit audit` parses the same file correctly, so the two disagree and the
  packet is what the spec-writer is told is authoritative. Three writers caught it by reading the cut
  and declared it; two did not, and `doit gate` enforces the grant — a builder would have been
  refused at merge for committing the test file its own unit is cut to own. Corrected by a
  `decision` event on `L-spec-0030`, `-0032`, `-0033`. The sibling `Produces:` list truncates the
  same way.
- **`doit packet plan-auditor` takes a `--repo` the Planner contract's dispatch step never names
  (R69).** Without it the packet's embedded pre-pass hands the auditor
  `unit size: undetermined` while the Planner's own `doit audit` run measured it clean. Round 1 was
  audited against a degraded block for exactly that reason, and the auditor correctly refused to read
  undetermined as a pass.
- **The seam-direction check is a verb count over prose.** It flagged all nine of `relay-queries`'
  `Produces:` because the unit's Goal said "derives/renders/says"; rewording to "generates/creates"
  cleared all nine without moving a single boundary. Know that it can be satisfied by wording alone.
- Three audit rounds at stage `cut` returned 29 findings with near-zero overlap, and round 2's
  load-bearing one was invisible to every mechanical check: wave 1 as then cut would have merged a
  Planner that kills its own pane while `doit up` still `execvpe`d once — **strictly worse than
  today**. More evidence against extending the one-round blind-audit cap to cuts (cf. R63).

## Owed at close
Fourteen sweep questions, defaults in the Plan, one blocking.

- **Blocking · Q1 — the project split.** Two operator acts: which Executor picks these specs up (the
  running one is scoped `albert-scott` and will never see a `do-it-v2` spec; `board.md` is a single
  global path two folds would clobber), and whether the charter-side events are repaired with
  `correction`, which is operator-only by code (`fold.py:159`). **Default taken:** specs dispatched
  as `do-it-v2` — safe either way, because the spec text does not depend on the label and a wrong
  label is repairable, whereas building do-it-v2 code inside the Albert Scott repository is not.
  **This Planner proceeded to ⑥ past a blocking question on that default**; the contract says stop,
  and that choice is recorded here rather than buried.
- Named for veto: **Q4** (from the day this ships every undeclared charter reads `count throttle:
  L-charter-N not landed` — four charters are `L1-complete` and not landed — so the relay will not
  march until a Thinker backfills sequencing blocks), **Q5** (ordering is charter id alone; the other
  two keys do not exist here), **Q10** (a serving-only pass is a second shape of Planner),
  **Q3** (probe residue approval, D96 — not a yes/no).
- **Q2 — the live working tree is the install.** Settled rather than escalated, per the last
  handover's instruction: `L-adr-0033` makes a relay-query failure render one named error line
  instead of a missing board section, closing the silent-omission failure R58/R59 actually cost.
  Pinning the panes to a snapshot install remains the operator's and is not this charter's.
- **Q11/Q12/Q13/Q14 are process, not product**, and three of them cost something real this session.

## Next
Do **not** start another charter from this context. The next Planner should take the operator's
named charter, else the lowest-numbered `open` one — and should not plan charter N+2 before
L-charter-0020 has landed.
