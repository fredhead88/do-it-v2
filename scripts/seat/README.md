# Seat-route helpers (the pane's side of `doit dispatch --seat`)

Used to drive charter 3 (2026-09-15). **`packet.py` now owns the Planner's packets (a-14)** —
`doit packet plan-auditor <charter> --stage cut|plan|charter-set` and
`doit packet spec-writer <spec> --unit <name> --charter <path>` (or bare `--slot` with `--unit`)
build the same shape these two scripts built by hand, straight from the ledger's `cut-written` /
`plan-written` events and the `content/cut-<charter>.md` / `content/plan-<charter>.md` convention,
with the same "no Rationale" strip and the same Blindness refusal every other role's packet gets.
`mkpacket.py` and `mkslot.py` stay here as the reference shape those two `doit packet` paths
reproduce — read them when `doit packet`'s output looks wrong, not as a second way to build one.

- `stamp.sh <spawn> <model> <session> <tool_uses> <duration_ms> <subagent_tokens>` — validates
  `seat/<spawn>.output.json` and writes `<spawn>.meta.json`, which is the wrapper's completion
  signal (S13). Run after the Agent-tool sub-agent returns; the numbers are in its usage line.
  Not superseded — `doit packet` builds packets, not this.
- `mkpacket.py <charter> cut|plan <out>` — the plan-auditor packet (stage cut or plan) in the
  pilot's shape: done-condition, requirements, the cut, (the Plan + the latest cut-audit's
  findings at stage plan), the `doit audit` block. No rationale. Reference shape for
  `doit packet plan-auditor <charter> --stage cut|plan`.
- `mkslot.py <charter> <unit> <spec-id> <out>` — the round-one spec-writer slot: charter extract,
  the unit's block, the Plan's Seams + Shared decisions verbatim, envelope, siblings' Produces,
  ADRs, write path, the 400-line cap. Reference shape for
  `doit packet spec-writer <spec> --unit <unit> --charter <charter-file>`.

Serving pattern (R2: the harness snapshots agent types at pane start, so serve as
`general-purpose` and name the contract file): Agent(model=<from models.toml>, prompt = "You are the
DO-IT `<role>` contract, spawn id <id>. 1. Read /home/albert/do-it-v2/agents/<role>.md (binding,
incl. Seat route) and its schema. 2. Read /home/albert/.do-it/seat/<id>.packet.md; cwd <repo or
worktree>; write nothing under it. 3. Write seat/<id>.output.json and run `doit validate <role>
<file>` until VALID. 4. End with `DONE <id>`.")
