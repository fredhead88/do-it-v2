# Seat-route helpers (the pane's side of `doit dispatch --seat`)

Used to drive charter 3 (2026-09-15). **`packet.py` now owns the Planner's packets (a-14)** —
`doit packet plan-auditor <charter> --stage cut|plan|charter-set` and
`doit packet spec-writer <spec> --unit <name> --charter <path>` (or bare `--slot` with `--unit`)
build the same shape these two scripts built by hand, straight from the ledger's `cut-written` /
`plan-written` events and the `content/cut-<charter>.md` / `content/plan-<charter>.md` convention,
with the same "no Rationale" strip and the same Blindness refusal every other role's packet gets.
`mkpacket.py` and `mkslot.py` stay here as the reference shape those two `doit packet` paths
reproduce — read them when `doit packet`'s output looks wrong, not as a second way to build one.

- `stamp.sh <spawn> <model> <session> <turns> <duration_ms> <subagent_tokens>` — validates
  `seat/<spawn>.output.json`, then calls `src/usage.py stamp` to resolve `<session>`'s OWN
  sub-agent transcript (`~/.claude/projects/*/*/subagents/agent-<id>.jsonl`, newest match) and
  write `<spawn>.meta.json` — the wrapper's completion signal (S13). `<model>`,`<session>`,
  `<duration_ms>`,`<subagent_tokens>` are still read off the sub-agent's own usage line by hand,
  same as before, and are never compared against anything or corrected; `usage.py` now ALSO
  writes, alongside the hand-supplied blended `subagent_tokens`, the four-way split
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` —
  deduped by message id, since a streamed response repeats its usage object as it grows) and
  `model_observed` — the model the transcript's assistant lines actually ran on, which
  `dispatch.run_seat` prefers over the typed `<model>` for `model_used`, and which makes
  `model_observed` on the terminal event true only when that evidence exists (retro step 9,
  2026-09-15). No transcript resolves -> the four fields and `model_observed` stay null
  (unmeasured is never zero) and stamp.sh warns on stderr, but still stamps — a missing
  transcript must not block the completion signal. `<turns>` is the one field that may now be
  the literal `-`, meaning "omitted": `usage.py` then derives it from the transcript's own
  distinct-assistant-id count (itself possibly null, never invented as 0); a hand-typed
  `<turns>` that differs from that derived count by more than 10% is overridden with the
  derived value, and the override lands on the meta as `turns_supplied`/`stamp_corrected:
  "turns"` — `<duration_ms>`/`<subagent_tokens>` carry no such path, ever.
- `mkpacket.py <charter> cut|plan <out>` — the plan-auditor packet (stage cut or plan) in the
  pilot's shape: done-condition, requirements, the cut, (the Plan + the latest cut-audit's
  findings at stage plan), the `doit audit` block. No rationale. Reference shape for
  `doit packet plan-auditor <charter> --stage cut|plan`.
- `mkslot.py <charter> <unit> <spec-id> <out>` — the round-one spec-writer slot: charter extract,
  the unit's block, the Plan's Seams + Shared decisions verbatim, envelope, siblings' Produces,
  ADRs, write path, the 400-line cap. Reference shape for
  `doit packet spec-writer <spec> --unit <unit> --charter <charter-file>`.
- `backfill_tokens.py [--apply] [--root R]` — the one-time retro backfill (operator ask,
  2026-09-15): for every seat `spawn-done` with a `session` and no split yet, resolves the
  transcript the same way `stamp.sh` now does going forward and prints what it would write
  (default) or appends a `correction` event under `DOIT_LEDGER_FILE` (`--apply`) — the ledger is
  append-only, so this repairs by naming the old event, never by editing it. `--apply` is the
  integrator's call, never run against a root you do not operate.

## `doit spend <charter|spec|spawn-id>` (retro step 9)

Not a seat-route helper — it lives in `src/fold.py`, wired the way `doit states` is (the `doit`
entry script's wildcard case forwards to `fold.py`, which dispatches `spend` internally). A
per-spawn table (spawn, role, model, the four tokens, a weighted input-equivalent total from
`models.toml`'s `[weights]`, duration, wall clock) plus totals; for a charter, also the stage
wall clock derived from event timestamps (`cut-written -> l1-complete -> first shipped -> last
shipped -> charter-review-complete -> tree-reaped`). Read-only: it never writes `board.md`. The
board itself grew a `## SPEND` block from the same fold (`spend_by_model` in `src/fold.py`),
alongside the older per-project dollar line on `## HEALTH`, which is unchanged.

A writing role's write destination is never asked or inferred (L-spec-0262):
`dispatch.run_seat`/`dispatch.run_codex` resolve it before any spend and write it
in two places a server reads from, never derives — the packet's own first line,
`WRITE PATH: <absolute path>`, followed by a blank line ahead of the packet's
existing content; and `seat/<spawn>.cmd.json`'s `"path"` key (an absolute string
for a writing role, `null` for a non-writing one). A relay pane or the Planner
serving a writing-role seat reads its destination off one of those two, not by
asking the operator or guessing from the contract file.

Serving pattern (R2: the harness snapshots agent types at pane start, so serve as
`general-purpose` and name the contract file): Agent(model=<from models.toml>, prompt = "You are the
DO-IT `<role>` contract, spawn id <id>. 0. Before anything else, run scripts/seat/claim.sh <id> —
if it exits non-zero, stop: someone else already claimed this seat. 1. Read
/home/albert/do-it-v2/agents/<role>.md (binding, incl. Seat route) and its schema. 2. Read
/home/albert/.do-it/seat/<id>.packet.md; cwd <repo or worktree>; write nothing under it. 3. Write
seat/<id>.output.json and run `doit validate <role> <file>` until VALID. 4. End with `DONE <id>`.")
