# Prompt Tier — Ten Contracts, the Wrapper, the Tick, Three Drivers

## Status
**The dispatch wrapper and `do-it tick` are written, tested, and the wrapper
has run two contracts for real.** Seven of the ten contracts have now been
spawned on their own models: `spec-writer` and `builder` went through
`src/dispatch.py` on a throwaway spec — an eleven-slot spec with a mutation
step in its Verification, then a one-commit build with an 11-line card, every
event derived from the Output object, the poke firing a tick afterwards.
The three driver skills, the `executor` agent file the tick spawns, the packet
scripts, `vet-dep`, and most of the fold rules are unwritten. Nothing blocks
them.
Last updated: 2026-09-08 (handover after the third session)

## Goal
Write the artifacts that make DO-IT v2's roles *exist*: ten sub-agent contracts,
their ten `Output` schemas, the wrapper that spawns them, the tick that drives
the Executor, and three driver skills (Planner, Executor, Thinker).
**A contract is an agent file** — its `tools:` line is the sandbox, its `model:`
line is the model, its body is the byte-identical prefix — spawned by the
wrapper on one line:

```
env -u ANTHROPIC_API_KEY claude -p --agent <contract> --strict-mcp-config \
  --permission-mode dontAsk --disable-slash-commands --output-format json \
  --json-schema "$(cat agents/<contract>.schema.json)" \
  --max-budget-usd <cap> --allowedTools <the contract's tools: line> \
  --add-dir ~/.do-it/content [--disallowedTools <builder deny list>] [--mcp-config <browser>]
```

The packet is stdin. `doit dispatch <role> <subject> --packet FILE --path P --cwd DIR …`
is that line plus every check below.

## Current State

**Repo:** `~/Projects/do-it` · remote `https://github.com/fredhead88/do-it-v2.git` · Public.
**Register:** `~/.claude/do-it-v2/open-topics.md` (D1–D120) · design copy synced.

**Built and running** [`doit test`: fold 33 + merge-gate 69 + dispatch 16 mocked
spawns + tick 5; two real spawns through the wrapper]:

| File | Lines | What |
|---|---|---|
| `src/fold.py` | ~315 | append-only ledger, derives all state, renders board; **actor derivation keeps a hyphenated role whole** (`L-spec-writer-0007` → `spec-writer`, not `spec`); **`tick-stale` HEALTH line** (D117) |
| `src/merge_gate.py` | 421 | catches a merge that *removes* a file nobody watches |
| **`src/dispatch.py`** | **~290** | **the wrapper.** Allocates `L-<role>-NNNN` (max+1, `O_EXCL`); `build-started` before a builder spawn; the D116 line; then in order: `is_error` → `spawn-failed` (+ `escalation-blocking` on `api_error`), null `structured_output` → failed, `contamination` → failed, file/dir at `--path` or failed, `git status --porcelain` unchanged across a non-builder spawn or failed; then the contract's events from the Output object, then `spawn-done` with `usage`. Refuses an identical packet+contract that already failed as a refusal or contamination — before it spends. Files ADRs, renders the card, ends with a tick poke |
| **`src/tick.py`** | **~75** | **D117.** `flock`; fold; `tick{lane, spawned}` event; spawns `-p --agent executor` with `--disallowedTools` naming §10.5's RETIRE list only when the lane is actionable; idle spawns nothing. **Cannot spawn until `agents/executor.md` exists** — says so loudly and exits 1 |
| `src/test_dispatch.py` · `src/test_tick.py` | ~160 | every after-the-fact check exercised against the failure it was written for |
| `src/backup.sh` | 69 | one-way restic push + restore drill |
| `agents/<name>.md` × 10 · `<name>.schema.json` × 10 | 986 · 1692 | the ten contracts and their Output schemas (unchanged this session) |
| `doit` | | `dispatch`, `tick` subcommands; `test` runs all four suites |

**Spawned for real, key unset** (five in the 09-08 meter file; two through the wrapper this session, in a scratch `DOIT_ROOT` — nothing touched `~/.do-it`):

| Contract | Model | Ran? | Notes |
|---|---|---|---|
| `research` | haiku | **yes** — a real dig into `fold.py`, correct | Write needs the whole-tool grant (D120) |
| `grader` | Fable | **yes** — contamination exit | |
| `spec-auditor` | Fable | **yes**, after two rewordings | safeguard bisected, D120 |
| `plan-auditor` | Fable | **yes**, after the schema fix | |
| `charter-reviewer` | Opus | **yes** — contamination exit | |
| **`spec-writer`** | Opus | **yes, through the wrapper** — 199-line spec, eleven slots, two typed ACs with review paths, a three-step Verification including a mutation test; 9 turns, 79 s, $0.34 | `spec-written` + `spawn-done` appended; repo status unchanged |
| **`builder`** | Opus | **yes, through the wrapper** — one commit on its branch, worktree clean, the spec's own verify block exits 0; 14 turns, 100 s, $0.60, no denials | `build-done`, 2 × `build-deviation` (minor), `worked`, `spawn-done`; card 11 lines |
| `reuse-scout` | Sonnet | model id resolves; **not spawned** | needs `vet-dep` (does not exist) |
| `reviewer` · `probe` | Opus | **not spawned** | need a review account + `--mcp-config` browser; a run dir + §10.4's paid-call wrapper |

**Not written:**

| Missing | Size | Kind |
|---|---|---|
| **`agents/executor.md`** — the tick's spawn target: a driver agent file (KEEP-list skills in `tools:`, D119); its body is the Executor skill | ~300 | **prompt** |
| Planner (pane) and Thinker (interactive) driver skills | ~600 | **prompt** |
| **The packet scripts** — one per role; the strip list *is* the Blindness field (correction #6). Two things the real spawns fixed: a multi-step Verification block goes into a script file and the packet hands `bash <file>` (the card's `verify.command` is capped at 300 chars); the worktree is cut clean, before anything runs in it | 250–350 | code |
| §3.6's six audit scripts · `scripts/vet-dep.mjs` · `do-it up` (one pane + one cron line, D117) | ~200 | code |
| Rest of fold's rules: `EMITS` for declaration terms per role, Budget vs `usage` (`budget-exceeded`), typed ACs, blind grading, wedge, `must-fix`, charter-review verdict | ~250 | code |

## Active Problems

### 1–6 — all DECIDED (D116–D119); see the register.

### 7. The Fable safeguard rule is per-model and per-word (D120)
Unchanged. Do not reintroduce "rationale" in a schema description or a
"considered and dropped" line in a body. The wrapper now records
`contract_sha256` (contract file + schema) and `cli` on every terminal event,
so "first spawn after a change" is a ledger query, not a memory.

### 8. Write confinement is after the fact (D120) — now enforced
`dispatch.py` grants the contract's whole `tools:` line and checks afterwards:
file or non-empty dir at `--path`, output `path` ending in the same name, repo
status unchanged (non-builder). Each is a test case. Bash deny patterns
(`Bash(git push --force:*)`, `Bash(*--no-verify*)`, `Bash(npm install:*)` …)
were measured to fire under `dontAsk` (probe, haiku, $0.05): all three denied,
`echo ok` ran, and `permission_denials` in the JSON is the independent record —
the wrapper carries it as `denied[]` on `spawn-done`.

### 9. Three contracts have never run
`reviewer`, `probe`, `reuse-scout`. Each needs a piece of the packet tier:
a browser `--mcp-config` and a read-only review account; a run directory plus
the paid-call wrapper; `vet-dep`. D120's rule applies: one spawn each on its
own model with its own schema before trust.

### 10. The fold's vocabulary — the wrapper's half is done, the fold's is not
The wrapper appends exactly the events the contracts name (`spec-written`,
`spec-killed`, `audit-finding`, `build-*`, `question`, `verdict`,
`rejected-criterion`, `criterion-cleared`, `review`, `must-fix`,
`checker-coverage-change`, `research-filed`, `reuse-scouted`, `probe-run`,
`charter-review-(not-)complete`, `gate-infra`) plus its own: `spawn-done`
(usage + the Output's top-level scalars), `spawn-failed{why}`,
`escalation-blocking`, `adr-filed`, `tick`, and **a declaration lands as an
event typed by its term** (`worked`, `spec-ambiguity{root_cause}`, …). The
fold derives from a subset; `EMITS` does not yet authorize declaration terms
per role. That list is the fold's to-do.

### 11. Decisions made in code this session — carry as a D if they hold
- Spawn ids are `L-<role>-NNNN`, the ledger file is the actor (D90); cards are
  `L-card-NNNN` with the spec's number; ADRs `L-adr-NNNN`. Allocation is
  max+1 claimed with `O_EXCL`.
- The wrapper appends `spawn_id: L-<role>-NNNN` as the last line of every
  packet — the builder's `built_by` needs it; the packet hash is taken before.
- Per-role caps live in `dispatch.ROLES` (minutes, USD) and go to
  `--max-budget-usd` and the subprocess timeout. Overridable per spawn. The
  fold's Budget comparison reads `spawn-done`.
- A refused or contaminated packet is never re-sent: identical
  `packet_sha256` + `contract_sha256` is refused before spending. An
  `api_error` is the one failure that is retried — after the operator logs in.
- The tick event carries no `project`, so a project-filtered board reads
  "last tick: never" (ponytail note in `fold.render`).

## Key Decisions Made

- **2026-09-08 (D120) — first real spawns.** Whole-tool Write grant plus
  after-the-fact checks; a refused spawn is a failed spawn that charged; the
  schema is part of the prefix the safeguards read; one spawn per contract per
  model before trust.
- **2026-09-08 (D119) — skills land as two flags.** No Skill tool plus
  `--disable-slash-commands` for sub-agents; `--disallowedTools` carrying the
  RETIRE list for drivers.
- **2026-09-08 (D118) — no eleventh contract.**
- **2026-09-08 (D117) — one pane and one job.** The Executor is a tick.
- **2026-09-08 (D116) — `--bare` conceded; the seat is the meter; the contract
  is the agent file.**
- **2026-09-08 — six proposed design changes audited, five cut.** Do not
  re-propose.
- **Panels and multi-model voting are rejected on measurement** (Part 11).

## Next Steps

1. **Write `agents/executor.md`** — the Executor driver as an agent file
   (KEEP-list skills on its `tools:` line; the tick already passes the RETIRE
   deny). Then `do-it up`: one Planner pane + one cron line running
   `doit tick`. Reference implementation: `~/.claude/skills/orc`.
2. **Planner and Thinker driver skills** — `~/.claude/skills/think`, `rev`.
3. **Packet scripts** — per role; strip list = Blindness; wrap Verification
   blocks in a script file; cut the worktree clean. The two hand-written
   packets this session used are the shape: `pk-spec-writer.md` was the ten
   Input items in order, `pk-builder.md` the eight.
4. **Spawn `reviewer`, `probe`, `reuse-scout` for real** (Active Problem 9).
5. **Fold rules** (Active Problem 10) — start with `EMITS` for declaration
   terms and Budget vs `usage`.
6. **One real charter through the ten contracts** — §12.2 strangler step 3 —
   and record the baseline while both systems run (D100).

## Reference

**Paths**
- Wrapper: `~/Projects/do-it/src/dispatch.py` · tick: `src/tick.py` · `doit dispatch -h`
- Contracts: `~/Projects/do-it/agents/<name>.md` + `<name>.schema.json`
- Design: `~/Projects/do-it/design/system-design-v2.md` · copy at
  `~/.claude/do-it-v2/system-design-v2.md` — keep identical
- Register: `~/.claude/do-it-v2/open-topics.md` — one table row per D
- Ledger: `~/.do-it/events/*.jsonl` · `DOIT_ROOT` moves it (the throwaway ran
  under a scratch root; `~/.do-it` is untouched)
- Meter tests: `~/.claude/do-it-v2/research/METER-TEST-2026-08-26.md` and
  `-2026-09-08.md`
- Probes: `~/.claude/agents/doit-probe-minimal.md`, `doit-probe-schema.md`,
  `doit-probe-skill.md` — throwaway, safe to delete

**The ten contracts** (models are the *contracts'*, from §4.6):

| # | Contract | Model id | Spawned on its model |
|---|---|---|---|
| 1 | `spec-writer` | `claude-opus-5` | yes (wrapper) |
| 2 | `spec-auditor` | `claude-fable-5-1` | yes |
| 3 | `builder` | `claude-opus-5` | yes (wrapper) |
| 4 | `grader` | `claude-fable-5-1` | yes |
| 5 | `reviewer` | `claude-opus-5` | no |
| 6 | `plan-auditor` | `claude-fable-5-1` | yes |
| 7 | `research` | `claude-haiku-4-5-20251001` | yes |
| 8 | `reuse-scout` | `claude-sonnet-5` | no |
| 9 | `charter-reviewer` | `claude-opus-5` | yes |
| 10 | `probe` | `claude-opus-5` | no |

**Conventions the files follow, and the drafting rules that bind:**
- §4.4's **nine fields** on every contract. Blindness is what the packet
  *does not carry* (correction #6).
- **Every `tools:` line carries `StructuredOutput`** or `--json-schema` is
  silently ignored (D116). No `Agent`, no `Skill`.
- **Events are appended by the wrapper from the Output object, never by the
  contract.** The builder's commit is the one exception.
- **Every schema has `contamination`** (except the builder's card).
- **Schemas are draft-07**: no `$schema`, no `$ref`, no `$defs`. Caps on every
  array and string.
- **Prohibition lists are the wrong form** (correction #2): required slots.
- **"Prose does not fire."** Admission test: does it convert a silent failure
  into a loud one?
- **Byte-identical prefix** across all spawns of a type; the packet is the
  user prompt (stdin), never the file.
- **Budgets are never uncapped** (correction #9); `dispatch.ROLES` holds them.
- **Clear the model's safeguards** (D120).
- **§7.3's landing test:** a change ships as a hook, a fold rule, a required
  field, or a calibration example — or it did not ship.

**Environment note:** the seat probe is per-machine, per-CLI-version and, since
D120, per-model. On CLI upgrade re-run rows M, R, I2, Q of the 09-08 file; on
model change spawn each contract once on its own model with its own schema.
Both are now visible in the ledger: `spawn-done.cli` and `.contract_sha256`.

## Session Log
- 2026-09-08 (third session): `src/dispatch.py` and `src/tick.py` written with
  their checks; fold's actor derivation fixed for hyphenated roles and the
  `tick-stale` line added; Bash deny patterns measured to fire under
  `dontAsk`; `spec-writer` and `builder` spawned for real through the wrapper
  on a throwaway spec in a scratch root (Opus, $0.94 total) — both clean, one
  wrapper defect found and fixed on the real run (a deviation's `type`
  collided with the event type). `doit test`: 4 suites green.
- 2026-09-08 (second session): D116–D120 written and carried. Meter run
  extended to ~60 spawns (~$8, $0 metered); the ten contracts and schemas
  written and symlinked; five contracts spawned on their own models; Write
  grant found to be whole-tool only; Fable's `reasoning_extraction` safeguard
  bisected to two phrasings and fixed.
- 2026-09-08 (first session): six proposals drafted, blind-audited, five cut.
  §12.5 meter probe: schema path draws the seat, D104 overturned, `--bare`
  isolated as the cause. Wrote `METER-TEST-2026-09-08.md`.
- Earlier: design v2 completed to D115; build group ① + merge gate shipped and
  running; one real charter closed (this system building itself).
