# Baseline — the first real charter through the contracts (D100, §12.2 step 3)

§12.2 step 3 is the one moment the old path and the new one both exist, and D100
says the baseline is perishable: capture it here or *is this working* becomes
permanently unanswerable. This is the record of `L-charter-0002` — filed by a
real Thinker, cut and planned by a real Planner, audited twice, specced, and
driven by real Executor ticks — on this repository, under the real `~/.do-it`.

**Read every dollar as §4.2 says.** Each `cost_usd` is the CLI's list-price
estimate under seat auth (`costBasis: list`). It is a *size* measure, not money
billed; the real meter is the seat's usage window.

**Status: the forward half and the build half both ran; the charter is
`L1-complete` with the last two ticks — charter-review and reap — outstanding.**
What is here is what actually happened, not a projection.

## The charter

`L-charter-0002 · Spend per project on the board` — the board shows what each
project has drawn, labelled as a list-price estimate, carrying the spawn count
behind it so an unmeasured project cannot read as a free one. Three
requirements, one unit, one wave, `Covers: none` (§12.2's adopted project,
`goal: null`). The Thinker chose R3 because it read the ledger and found zero
`spawn-done` events: the first render would be `$0.00`, and *"nothing has been
measured"* must not read as *"this project cost nothing"*.

## The run

| # | Role | Model | Turns | Wall | Cost (list) | Session | Outcome |
|---|---|---|---|---|---|---|---|
| 1 | `thinker` | opus | 13 | 91 s | $0.61 | `220fdb80-627c-426a-811e-352119436cc3` | wrote the charter; found the zero-`spawn-done` fact that R3 exists for |
| 2 | `planner` (⓪–⑤) | opus | 52 | 679 s | $3.21 | `23d48bce-839d-426e-a9b8-b84be0ce3fe8` | cut (1 unit, 1 wave), both audits, Plan rev 3, `L-adr-0001`, 3 owed questions — then **stopped on a blocking escalation** |
| 3 | `plan-auditor` (stage `cut`) | fable | — | 77 s | $0.77 | — | `bad_cut: false`, 4 findings + 1 `charter-gap`; used the script block as ground truth |
| 4 | `plan-auditor` (stage `plan`) | fable | — | — | $1.06 | — | 5 findings, all acted on in-Plan; §5.4 one round per stage |
| 5 | `planner` (⑥–⑦) | opus | 12 | 367 s | $0.62 | `f5f9f8e6-fc72-4714-82f6-2bda4dfb360d` | dispatched the spec-writer, printed the handover, stopped |
| 6 | `spec-writer` | opus | — | — | $1.38 | — | `L-spec-0004` written |
| 7 | `executor` tick 1 | opus | 6 | — | $0.20 | — | dispatched `spec-auditor` |
| 8 | `spec-auditor` (1st) | fable | 23 | — | **$2.19, wasted** | `7d2f02b7-d6bc-40cf-b80d-15db769c3e0e` | **`spawn-failed`: repo status changed across a non-builder spawn** — the changed file was *this document*, being written in the same tree |
| 9 | `spec-auditor` (2nd) | fable | 23 | — | $2.09 | `f60a8ce4-aabf-4416-9d5c-60e5f72f077d` | `bad_cut: false`, `contamination: false`, **12 findings**, several of them citations the spec had got wrong |
| 10 | `executor` tick 2 | opus | 11 | — | $0.44 | — | **escalated**: `doit packet spec-writer` crashed, so the rework could not be dispatched |

**Totals to that point: 10 spawns, $8.45 list, 2 blocking escalations, 1 wasted
spawn.** Ten of the twelve contracts have now run inside one charter's chain;
`builder`, `grader`, `reviewer` and `charter-reviewer` are the ones this step
did not reach.

## What the operator had to do — the §8.9 denominator

| When | What | Avoidable? |
|---|---|---|
| before the run | `ln -sfn ~/Projects/do-it ~/.do-it/repos/do-it` — no `repos/` existed under the real root | yes, and nothing creates it: `doit up` makes `events/` and `logs/` and not `repos/` |
| before the run | closed three August spec subjects (`spec-closed`) and retracted `L-charter-0001` | **no, and this is the finding below**: the lane is global, so §12.2's "while everything else continues on the old path" is not expressible |
| after the Thinker | `doit think --land` | no; the landing check is deliberately the driver's (§3.3) |
| escalation 1 | fixed `dispatch.alloc` (commit `3a9d35c`), appended `unblocked` | the fix is durable; the escalation was correct and cheap |
| mid-run | `doit tick` crashed on a pre-wrapper event; fixed (`a996cee`) | it stopped the whole loop silently — every tick after it was dead |
| escalation 2 | fixed `packet.py` (commit `80d1ba4`), appended `unblocked` | as above |

**Four operator interventions, three of them code fixes to DO-IT itself, none
of them a product decision.** That is the honest baseline number: the first
charter through this chain needed the operator four times in its forward half,
and every intervention was a defect in the machinery rather than a judgment the
system could not make.

## What the machinery got right

- **Both escalations were correct and specific.** The Planner did not invent a
  spec id when `doit alloc` handed back one the ledger already owned; it stopped,
  named the durable fix and the operator-correction alternative, removed the
  zero-byte file it had created, and said `content/` was byte-identical to its
  pre-charter state. It was.
- **The auditors read reality, not the packet.** The plan-auditor found that the
  Executor's own `spawn-done` carries no `project` (Active Problem 13) from the
  code, and filed the resulting silence in the charter as a `charter-gap`. The
  spec-auditor found twelve things including stale line citations, a
  shell-quoting bug in the spec's own verification regex, and one acceptance
  criterion that was unreachable and therefore vacuous.
- **The pre-pass did its job.** `doit audit` answered `undetermined` for the
  acquisition trail at stage `cut` and `none` at stage `plan`, and the auditor
  treated the block as ground truth and spent its pass on semantics.
- **Nothing silently succeeded.** Every failure above is an event.

## Findings that are not yet fixed

1. **The lane is global, so one charter cannot run "beside" the old path.**
   `tick.lane()` is every actionable spec plus every `L1-complete` charter.
   Twelve-day-old subjects from an abandoned run were on it, and the first tick
   would have spent spawns on them. §12.2 step 3 assumes a charter can move
   through the new chain while everything else continues on the old one; the
   tick has no such scoping. Closing the old subjects was the operator's
   workaround.
2. **A spawn is voided by any concurrent edit anywhere in the repo.** The
   wrapper's "repo status unchanged across a non-builder spawn" check reads
   `git status --porcelain` over the whole tree, so the driver writing its own
   handoff file voided a $2.19 spawn. The check is right to exist (D120 W3) and
   its blast radius is wrong: it should compare against the paths the spawn was
   granted, or the driver must keep its tree clean for the duration.
3. **Three real defects were found by running, and none by the test suite** —
   `alloc` over files only, `tick` on a pathless start event, `packet` on a
   pathless `spec-written`. All three are the same shape: **a field the wrapper
   always writes, absent on an event written before the wrapper existed.** The
   ledger is append-only, so pre-wrapper events are permanent inputs.


## The build half (twelfth session)

| # | Role | Model | Turns | Wall | Cost (list) | Session | Outcome |
|---|---|---|---|---|---|---|---|
| 11 | `executor` ticks 3–7 | opus | 5–11 | — | $1.67 | — | dispatched builder, grader, reviewer; merged; swept to fixpoint |
| 12 | `builder` | opus | 29 | 358 s | $2.07 | `3ca4f264-97c3-4306-8d80-01c7c28ad982` | one commit `4de8d60`, 3 deviations, `worked`, a `spec-contradiction` and an `adr-friction` |
| 13 | `grader` | fable | 13 | 119 s | $1.14 | `0eba0bd8-cc56-4dba-9587-a26dbf6518b5` | all nine criteria met, `card_ok: yes`; one `card-quality` — the card had rows for AC1–AC6 only |
| 14 | `reviewer` round 1 | opus | 11 | 330 s | $0.52 | `30e8bc7b-644e-448e-b17a-be8cc62eefd7` | **`n_blocking: 0` on a review of nothing** — its packet said "none found in the spec" |
| 15 | `reviewer` round 2 | opus | 19 | 265 s | $1.07 | `33da77e7-fff2-4cee-9a6e-f7a27e308925` | all nine criteria driven at depth `full`, `n_blocking: 0`, and it asked whether the round label was right |

**Build half: 5 spawns + 5 executor ticks, $6.47 list.** Running total for the
charter: **20 spawns, $14.92 list, 2 blocking escalations, 1 wasted spawn, 1
wasted review.**

## What the operator had to do — the build half

| When | What | Avoidable? |
|---|---|---|
| after review round 1 | fixed `packet.py` (commit `5450cef`), re-dispatched the reviewer | the defect was durable and is now fixed; **the review that reviewed nothing cost a full Opus spawn and would have closed the charter** |
| at charter close | fixed `fold.py` + `planner.md` (commit `9b5f1d3`), appended `l1-complete` as operator | the fix is durable; the append was needed once, for a charter whose Planner ran before the line existed |
| after review round 2 | answered the reviewer's `question` with a `decision` | no — that is §4.9 working: it asked, it defaulted safely, it did not wait |

**Seven operator interventions across the whole charter, five of them code fixes
to DO-IT itself, two of them answers the system asked for correctly.** No
product decision was ever escalated to the operator.

## What the build half taught

- **Absent input reads as an empty answer.** Both defects here are that: a
  packet reporting `none` over nine criteria, and an event nobody ever wrote.
  Neither was a wrong answer — both were *no answer, formatted as one*.
- **A review with nothing to review is byte-identical to a clean review.** The
  reviewer said so in an `evidence-gap` and returned no blocking finding
  anyway, which is correct behaviour on a packet that lied to it.
- **The suite supplies what the world does not.** `test_fold.py` had been
  appending `l1-complete` by hand since the fold was written, and
  `test_packet.py`'s fixture spec used unbolded `AC1 [` lines. Four of this
  repo's defects now have that shape.

## The close half (thirteenth and fourteenth sessions) — the charter reached L2

The charter-reviewer's `not-complete` (spawn 16) is where §3.13's return path
begins, and running it is what proved the path did not exist: nothing checked
`sweep-fixpoint`, and no role, script or pane could author the spec an in-scope
brief implies. Both are now rules (`8b22c8a`). The triage itself stayed
authorship's — one in-scope brief citing R3, three adjacent, each excluded by
the charter's own Constraints — and **the charter was not narrowed to fit what
had been built.**

| # | Role | Model | Turns | Wall | Cost (list) | Session | Outcome |
|---|---|---|---|---|---|---|---|
| 17 | `spec-writer` (L-spec-0005) | opus | 28 | 385 s | $2.19 | `6a883730-b54f-4466-9975-b4829e8e6c76` | five ACs over `ui`/`backend`/`observed-data`, and a real `charter-gap`: R3 is reachable only where a name is asserted |
| 18 | `spec-auditor` | fable | 44 | 243 s | $2.43 | `6d024639-7dee-4c06-92dc-76a4858dec28` | 11 findings, `contamination: false` |
| 19 | `spec-writer` (rework) | opus | 10 | 152 s | $0.78 | `abb901c5-2dc2-4fd5-b11b-0b2d112db9dd` | the fix list answered |
| 20 | `builder` | opus | 39 | 505 s | $2.25 | `1e000724-72fc-49a0-88cb-d4be0d2c2364` | one commit `94f2963`, 2 deviations, `L-adr-0002`, and a `spec-ambiguity` naming a line the spec had located wrongly |
| 21 | `grader` | fable | 13 | 110 s | $0.99 | `208c19ec-da5f-41a4-b214-f37529ac10e5` | all five criteria met, `card_ok: yes`, one `checker-coverage-change` naming what the checker does *not* see |
| 22 | `reviewer` | opus | 13 | 134 s | $0.52 | `138cd480-4df6-4345-a643-77ff29fbf493` | round 1 at depth `full`, `n_blocking: 0` — on a packet that carried its criteria this time |
| 23 | `charter-reviewer` round 2 | opus | 25 | 486 s | $1.21 | `e099082d-187c-4392-b1ae-3539a3681a99` | **`verdict: complete`**, `uncovered_requirement_ids: []`, plus four findings and two `charter-gap`s it filed anyway |

**Close half: 7 contract spawns + 14 executor ticks, $15.92 list.** Whole
charter: **41 spawns, $30.84 list, 3 blocking escalations, 1 wasted spawn, 1
wasted review.** Merge `534d08c` through the gate; `tree-reaped` on both
branches; `L-charter-0002` is `L2-complete` and nothing of it stands on disk.

## What the operator had to do — the close half

| When | What | Avoidable? |
|---|---|---|
| after the `not-complete` | triaged five findings by §2.6's citation test, appended one in-scope `brief` and three adjacent, cleared the escalation | **no — this is the one judgment §2.6 reserves for authorship**, and the register has no rule that could have made it |
| escalation 3 | fixed `packet.py` (`0755894`), appended `unblocked` | the fix is durable: the grader's packet refused every spec whose branch is named for the spec, which is the Executor's own cut recipe |
| at close | fixed `tick.py` (`67c0618`) so a reapable charter stays on the lane | durable; before it, `doit reap` was unreachable by any tick |
| after close | fixed `fold.py` (`611e60f`) so a `charter` field that is a path names the same charter as one that is an id, then re-ran `doit reap` | durable; the reaper had reported success over a standing worktree |

## What the close half taught

- **The return path is the part nobody runs.** Four sessions built the forward
  chain and it worked the first time. §3.13's *"not complete → back to ① with
  new specs"* had two unreachable halves and a packet that refused round one,
  and none of it was visible until a real charter-reviewer said no.
- **A charter reference has two spellings and only one of them was ever
  compared.** `L-charter-0002` and `/…/content/L-charter-0002.md` are the same
  charter; the fold compared the raw field, so a spec written with the second
  belonged to no charter at all. The reaper skipped its worktree and reported
  `retained: []`, the charter-reviewer's packet named one card for a charter
  that shipped two — and the L2 conjuncts are quantified over that same set, so
  **the charter could have closed over a spec that was never built.** It is the
  seventh defect of the shape this repo keeps finding, and the fifth to be
  invisible because absent input reads as an empty answer.
- **The system's own escalations named their fixes again.** All three did. The
  cheapest way to find out what to build next is to read what the Executor said
  it could not do.
