# Baseline — the first real charter through the contracts (D100, §12.2 step 3)

§12.2 step 3 is the one moment the old path and the new one both exist, and D100
says the baseline is perishable: capture it here or *is this working* becomes
permanently unanswerable. This is the record of `L-charter-0002` — filed by a
real Thinker, cut and planned by a real Planner, audited twice, specced, and
driven by real Executor ticks — on this repository, under the real `~/.do-it`.

**Read every dollar as §4.2 says.** Each `cost_usd` is the CLI's list-price
estimate under seat auth (`costBasis: list`). It is a *size* measure, not money
billed; the real meter is the seat's usage window.

**Status: the forward half ran; the build half had not finished when the step's
context ran out.** What is here is what actually happened, not a projection.

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
