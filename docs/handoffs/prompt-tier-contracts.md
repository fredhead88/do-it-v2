# Prompt Tier — Ten Contracts, Ten Schemas, Three Drivers

## Status
**The ten contracts and their ten schemas exist and five have run on their own
models.** The three driver skills, the dispatch wrapper and the Executor tick
are unwritten. Nothing blocks them: (a) is decided (D116), the panes (D117),
the contract count (D118), the skills mechanism (D119) and the first-spawn
findings (D120) are all in the design.
Last updated: 2026-09-08 (second session)

## Goal
Write the artifacts that make DO-IT v2's roles *exist*: ten sub-agent contracts,
their ten `Output` schemas, and three driver skills (Planner, Executor, Thinker).
**A contract is an agent file** — its `tools:` line is the sandbox, its `model:`
line is the model, its body is the byte-identical prefix — spawned by one line:

```
env -u ANTHROPIC_API_KEY claude -p "<packet>" \
  --agent <contract> --strict-mcp-config --permission-mode dontAsk \
  --disable-slash-commands --allowedTools Write \
  --json-schema "$(cat agents/<contract>.schema.json)" --output-format json
```

(`--allowedTools Write` only for the roles that write; `--add-dir` the content
dir; the browser via `--mcp-config` for the two reviewer roles. See D120 for why
the grant is whole-tool.)

## Current State

**Repo:** `~/Projects/do-it` · remote `https://github.com/fredhead88/do-it-v2.git` · Public.
**Register:** `~/.claude/do-it-v2/open-topics.md` (D1–D120) · design copy synced.

**Built and running** [verified e2e: `doit test`, 58 checks; one real charter;
the merge gate has fired on a real merge]:

| File | Lines | What |
|---|---|---|
| `src/fold.py` | 301 | append-only ledger, derives all state, renders board |
| `src/merge_gate.py` | 421 | catches a merge that *removes* a file nobody watches |
| `src/backup.sh` | 69 | one-way restic push + restore drill |
| `src/test_*.py` | 580 | the 58 checks |
| **`agents/<name>.md` × 10** | **986** | **the ten contracts — agent files, nine fields each** |
| **`agents/<name>.schema.json` × 10** | **1692** | **their Output schemas — draft-07, no `$schema`, no `$ref`** |
| `install.sh` | +7 | symlinks `agents/*.md` into `~/.claude/agents/` so `--agent <name>` resolves from any cwd |

**Spawned for real on the D116 line, key unset** (all in the 09-08 meter file):

| Contract | Model | Ran? | Notes |
|---|---|---|---|
| `research` | haiku | **yes** — a real dig into `fold.py`, correct | Write needs the whole-tool grant (D120) |
| `grader` | Fable | **yes** — contamination exit | |
| `spec-auditor` | Fable | **yes**, after two rewordings | safeguard bisected, D120 |
| `plan-auditor` | Fable | **yes**, after the schema fix | |
| `charter-reviewer` | Opus | **yes** — contamination exit | |
| `reuse-scout` | Sonnet | model id resolves; **contract not spawned** | needs `vet-dep` (does not exist) |
| `spec-writer` · `builder` · `reviewer` · `probe` | Opus | **not spawned** | need a packet, a worktree, a review account — i.e. the wrapper |

**Not written:**

| Missing | Size | Kind |
|---|---|---|
| **The dispatch wrapper** — the line above; `build-started` before, events from the Output object after; **null `structured_output` → failed**; **file at the named path or failed**; **`git status --porcelain` unchanged across non-builder spawns or failed**; **`is_error` → failed, never retried identically**; `usage` → event; `api_error` → operator escalation | ~80 | code |
| **`do-it tick`** — fold first, spawn `-p --agent executor` only on an actionable lane, `flock`, `tick` event (D117) | ~40 | code |
| Three driver skills — Planner, Executor, Thinker | ~900 | **prompt** |
| §3.6's six audit scripts, the packet script (per role: what it strips, what it carries), `scripts/vet-dep.mjs` | 250–350 | code |
| Rest of fold's rules (typed ACs, blind grading, wedge, Budget vs `usage`, `must-fix`, `tick-stale`, charter-review verdict) | ~250 | code |

## Active Problems

### 1–6 — all DECIDED (D116–D119); see the register.

### 7. The Fable safeguard rule is per-model and per-word (D120)
Fable 5.1's `[reasoning_extraction]` safeguard refuses a spawn whose **schema
descriptions** contain "rationale", or whose body asks the model to report
candidates it "considered and dropped, with why." Deterministic; charges the
prefix (~$0.25). Fixed in all three schemas that carried the word and in the
one body line. **Every contract is spawned once on its own model with its own
schema before it is trusted, and again on model or CLI change.** Do not
reintroduce those phrasings; if a future contract is refused, bisect by section
with `--system-prompt` and `--tools ""` (~$0.17 a row) — the method is in the
meter file.

### 8. Write confinement is after the fact (D120)
Path-pattern grants do not work under `dontAsk` in either spelling. Grant
`Write` whole and check afterwards: file at the named path, repo status
unchanged. The research contract returned `answered: yes` with no file on disk
on the first day — the check is load-bearing, not hygiene.

### 9. Five contracts have never run
`spec-writer`, `builder`, `reviewer`, `probe`, `reuse-scout`. Their model ids
resolve and their files parse (the symlink install ran clean), but a real run
needs the wrapper (a packet, a worktree, a review account, `vet-dep`). Opus and
Sonnet carry no safeguard of the Fable kind, so the D120 refusal class is
unlikely there — but D120's rule applies regardless.

### 10. The design's event vocabulary and the fold's are not yet one thing
The contracts name the events the wrapper appends: `spec-written`,
`spec-killed`, `audit-finding`, `build-started`, `build-done`,
`build-deviation`, `build-stub`, `build-blocked`, `question`, `verdict`,
`rejected-criterion`, `criterion-cleared`, `review`, `must-fix`,
`checker-coverage-change`, `research-filed`, `reuse-scouted`, `probe-run`,
`charter-review-complete` / `-not-complete`, `gate-infra`. `fold.py` derives
from a subset (see `spec_state` and `EMITS`). The rest are recorded and ignored
until the fold rules land — which is the intended order (content before
derivation), but the list above is the fold's to-do.

## Key Decisions Made

- **2026-09-08 (D120) — first real spawns.** Whole-tool Write grant plus
  after-the-fact checks; a refused spawn is a failed spawn that charged; the
  schema is part of the prefix the safeguards read; one spawn per contract per
  model before trust. §12.5's model-id row closed.
- **2026-09-08 (D119) — skills land as two flags.** No Skill tool plus
  `--disable-slash-commands` for sub-agents; `--disallowedTools` carrying the
  RETIRE list for drivers.
- **2026-09-08 (D118) — no eleventh contract.** A unit's shape is reviewed
  where it crosses units; inside one it is reversible and the builder's.
- **2026-09-08 (D117) — one pane and one job.** The Executor is a tick; the
  Planner stays a pane for D80's reason; the two-pane shape is the fallback.
- **2026-09-08 (D116) — `--bare` conceded; the seat is the meter; the contract
  is the agent file.** In-session dispatch of the same file is the fallback.
- **2026-09-08 — six proposed design changes audited, five cut** (line-number
  anchors, a cross-vendor auditor, a per-lens log, more coverage-script stages,
  restating the operator gate). Do not re-propose.
- **Panels and multi-model voting are rejected on measurement** (`:5514` in
  the pre-D116 numbering; Part 11).

## Next Steps

1. **Write the dispatch wrapper** (`src/dispatch.py` or `.sh`, ~80 lines) —
   the table above is its spec. Every check it makes converts a silent failure
   into a loud one; each was observed this session.
2. **Write `do-it tick`** (D117) — fold, decide, spawn-or-exit, `flock`.
3. **Draft the three driver skills** — Planner (pane), Executor (the tick's
   agent file), Thinker (interactive). Reference implementations:
   `~/.claude/skills/orc`, `think`, `rev` (659 lines for three thinner drivers).
   The Executor and Planner are agent files too, spawned on the same line with
   `--disallowedTools` naming §10.5's RETIRE list (D119).
4. **Write the packet scripts** — one per role, the strip list *is* the
   Blindness field (correction #6). Then spawn `spec-writer` and `builder` for
   real on a throwaway spec.
5. **Land the fold rules the contracts assume** (Active Problem 10).
6. **Run one real charter through the ten contracts** — the §12.2 strangler
   step 3 — and record the baseline while both systems run (D100).

## Reference

**Paths**
- Contracts: `~/Projects/do-it/agents/<name>.md` + `<name>.schema.json`
- Design: `~/Projects/do-it/design/system-design-v2.md` · copy at
  `~/.claude/do-it-v2/system-design-v2.md` — keep identical
- Register: `~/.claude/do-it-v2/open-topics.md` — one table row per D
- Code: `~/Projects/do-it/src/` · CLI: `doit` (see `doit help`)
- Ledger: `~/.do-it/events/*.jsonl` · `DOIT_ROOT` moves it
- Meter tests: `~/.claude/do-it-v2/research/METER-TEST-2026-08-26.md` and
  `-2026-09-08.md` (two runs, the contract spawns, and the bisection)
- Probes: `~/.claude/agents/doit-probe-minimal.md`, `doit-probe-schema.md`,
  `doit-probe-skill.md` — throwaway, safe to delete

**The ten contracts** (models are the *contracts'*, from §4.6):

| # | Contract | Model id | File |
|---|---|---|---|
| 1 | `spec-writer` | `claude-opus-5` | `agents/spec-writer.md` |
| 2 | `spec-auditor` | `claude-fable-5-1` | `agents/spec-auditor.md` |
| 3 | `builder` | `claude-opus-5` | `agents/builder.md` |
| 4 | `grader` | `claude-fable-5-1` | `agents/grader.md` |
| 5 | `reviewer` | `claude-opus-5` | `agents/reviewer.md` |
| 6 | `plan-auditor` | `claude-fable-5-1` | `agents/plan-auditor.md` |
| 7 | `research` | `claude-haiku-4-5-20251001` | `agents/research.md` |
| 8 | `reuse-scout` | `claude-sonnet-5` | `agents/reuse-scout.md` |
| 9 | `charter-reviewer` | `claude-opus-5` | `agents/charter-reviewer.md` |
| 10 | `probe` | `claude-opus-5` | `agents/probe.md` |

**Conventions the files follow, and the drafting rules that bind:**
- §4.4's **nine fields** on every contract: Input · Blindness · Output · Writes ·
  Tools · Model · Budget · May declare · Learns. Blindness is stated as what
  the packet *does not carry*, because it is constructed in the packet script,
  never instructed (correction #6).
- **Every `tools:` line carries `StructuredOutput`** or `--json-schema` is
  silently ignored (D116). No `Agent`, no `Skill`.
- **Events are appended by the wrapper from the Output object, never by the
  contract.** The Output schema is therefore the single source of every event
  field. The builder's commit is the one exception: the commit is its own.
- **Every schema has `contamination`** (except the builder's card): a packet
  that carries what Blindness strips voids the run.
- **Schemas are draft-07**: no `$schema`, no `$ref`, no `$defs`. Caps on every
  array and string — never a transcript (D82, D62).
- **Prohibition lists are the wrong form** (correction #2): required slots.
- **"Prose does not fire."** A field not enforced by the fold, the wrapper, or a
  hook is decoration. The admission test: does it convert a silent failure into
  a loud one?
- **Byte-identical prefix** across all spawns of a type; variable content last
  — the packet is the user prompt, never the file.
- **Budgets are never uncapped** (correction #9); under D116 the cap is checked
  against the spawn's `usage`.
- **Clear the model's safeguards** (D120): no "rationale" in schema
  descriptions; no "candidates you considered and dropped" in a body.
- **§7.3's landing test:** a change ships as a hook, a fold rule, a required
  field, or a calibration example — or it did not ship.

**Environment note:** the seat probe is per-machine, per-CLI-version and, since
D120, per-model. On CLI upgrade re-run rows M, R, I2, Q of the 09-08 file; on
model change spawn each contract once on its own model with its own schema.

## Session Log
- 2026-09-08 (second session): D116–D120 written and carried. Meter run
  extended to ~60 spawns (~$8, $0 metered): the seat is on disk; `--bare` is
  metered by its help text; the D116 line measured at 7,440 tok; skills settle
  as two flags; the ten contracts and schemas written to `agents/` and
  symlinked by `install.sh`; five contracts spawned on their own models; Write
  grant found to be whole-tool only; Fable's `reasoning_extraction` safeguard
  bisected to two phrasings and fixed. No fold code changed.
- 2026-09-08 (first session): Audited a Chezky transcript for concrete
  suggestions; six proposals drafted, then blind-audited and five cut. Ran the
  §12.5 meter probe (7 spawns): schema path draws the seat, D104 overturned,
  `--bare` isolated as the cause. Wrote `METER-TEST-2026-09-08.md`.
- Earlier: design v2 completed to D115; build group ① + merge gate shipped and
  running; one real charter closed (this system building itself).
