# Prompt Tier — Ten Contracts, Ten Schemas, Three Drivers

## Status
The design is complete and the substrate runs. **The prompt tier — ~4/5 of the
total build and almost none of it code — is unwritten, and as of 2026-09-08 it is
unblocked:** the (a) decision is written (D116), the §4.2 freeze is lifted, and
the contract spawn line is measured end to end on the seat.
Last updated: 2026-09-08 (second session)

## Goal
Write the artifacts that make DO-IT v2's roles *exist*: ten sub-agent contracts,
their ten `Output` schemas, and three driver skills (Planner, Executor, Thinker).
These are markdown prompts and JSON schemas, **not programs**. **A contract is an
agent file** — its `tools:` line is the sandbox, its `model:` line is the model,
its body is the byte-identical prefix — and it is spawned by one line (D116):

```
env -u ANTHROPIC_API_KEY claude -p "<packet>" \
  --agent <contract> --strict-mcp-config --permission-mode dontAsk \
  --json-schema <Output schema> --output-format json
```

Measured floor for that line: **7,440 tok** (this machine, CLI 2.1.263).

## Current State

**Repo:** `~/Projects/do-it` · remote `https://github.com/fredhead88/do-it-v2.git`
· clean tree after `docs(design): carry D116 into 4.2`. Public.
**Register:** `~/.claude/do-it-v2/open-topics.md` (D1–D116) · design copy synced.

**Built and running** [verified e2e: `doit test`, 58 checks; one real charter — this
system building itself; the merge gate has fired on a real merge]:

| File | Lines | What |
|---|---|---|
| `src/fold.py` | 301 | append-only ledger, derives all state, renders board |
| `src/merge_gate.py` | 421 | catches a merge that *removes* a file nobody watches |
| `src/backup.sh` | 69 | one-way restic push + restore drill |
| `src/test_*.py` | 580 | the 58 checks |

That is build group ① plus item ③·9. **Everything else in `design/system-design-v2.md`
is prose.**

**Not written** (sizes from §12.1·④, D109; the wrapper added by D116):

| Missing | Size | Kind |
|---|---|---|
| Ten sub-agent contracts (agent files) | 1,100–1,500 | **prompt** |
| Ten `Output` schemas | ~200 | schema |
| Three driver skills | ~900 | **prompt** |
| **The dispatch wrapper** — `env -u`, the line above, null-`structured_output` → failed spawn, `usage` → event, `api_error` → operator escalation | ~40 | code |
| §3.6's six audit scripts | 150–250 | code |
| Rest of fold's rules (typed ACs, blind grading, wedge, **Budget vs `usage`**) | ~250 | code |

Honest total ~3,000–3,700 lines, of which ~650 is code.

## Active Problems

### 1. ~~The (a) decision~~ — DECIDED (D116, 2026-09-08)
**`--bare` buys config isolation and costs the seat — by definition:** its help
text says *"OAuth and keychain are never read."* Conceded. The seat is the meter.
"Nothing implicit" is now the contract file's `tools:` + `model:` lines
(runtime-enforced on both seat paths), `--strict-mcp-config`, and the packet
script; **what stays implicit is a measured residual** (`CLAUDE.md`, memory, git
status — ~4k tok here). Evidence: `~/.claude/do-it-v2/research/METER-TEST-2026-09-08.md`,
both runs (26 spawns). Carried into §1.10, §3.9, §4.2, §4.3, §4.4, §4.5,
§4.6·3, §4.6·5, §6.7e, §10.1, §12.5.

**Three traps found on the way, all of which the contracts must carry:**
- **`--json-schema` needs the `StructuredOutput` tool.** A `tools:` line that
  omits it returns `is_error:false` with `structured_output:null` — silent.
  **Every contract's `tools:` line includes `StructuredOutput`**; the wrapper
  treats null as a failed spawn.
- **The `tools:` line does not exclude MCP servers under `-p`.** `~/.claude.json`'s
  servers (`context7`, `supabase`) leaked into a spawn declared `Read`.
  `--strict-mcp-config` is what removes them. D105's in-session claim does not
  transfer.
- **The seat is on disk, not in the session.** A plain terminal or cron
  environment reaches it; `env -i` fails only because it drops `USER`.

### 2. ~~§4.4's Output row~~ — FIXED under D116.

### 3. ~~The silent-fallback defect~~ — CLOSED BY CONSTRUCTION
The dispatch environment has no `ANTHROPIC_API_KEY`, so there is no meter to fall
through to; an unreachable seat is `is_error:true`, `terminal_reason:api_error`,
0 tokens. The wrapper routes that as an escalation to the operator (`/login`),
never a retry, never `seat-exhausted`. **The wrapper does not exist yet** — see
the table above.

### 4. The panes question is reopened with a *different* premise
D104's *"the two panes are the floor"* rested on a spawn being unable to draw the
seat. Row M shows a cron job can. What still argues for panes is D80's Planner
asymmetry (§3.9: an unplanned relay loses a charter's reasoning) — the Executor
is stateless and could be a `-p` spawn from the wake script. **Owed its own D.**

### 5. Per-skill restriction has no CLI mechanism yet
D66 says permitted skills are *named* in the contract. The CLI offers `Skill` in
the `tools:` line or not. **Measure `--allowedTools "Skill(name)"` (one spawn)
before the driver skills are drafted** — it decides whether D66 lands as a
runtime restriction or as a fold check on the transcript.

### 6. Where contract files resolve from
`--agent <name>` resolves from `~/.claude/agents/` or `<cwd>/.claude/agents/`.
Spawns run with cwd = the *client* repo, so the contracts must either be
installed into `~/.claude/agents/` by `install.sh` (symlink from
`do-it/agents/`) or passed inline via `--agents <json>`. One-line decision at
drafting time; §9.5 says they version with the code, so the source of truth is
the repo either way.

## Key Decisions Made

- **2026-09-08 (D116) — `--bare` conceded; the seat is the meter; the contract is
  the agent file; the spawn is the one line above.** In-session dispatch of the
  same file stays valid as the fallback. `-p` is primary because it is the only
  path on which Output (`--json-schema`) and Budget (`usage` in the JSON result)
  are *enforced* rather than hoped. D73's hook rationale re-grounded on §1.4 at
  the four HELD sites.
- **2026-09-08 — six proposed design changes were audited and five were cut.**
  Two blind auditors independently rejected them. Do not re-propose: adding
  line-number anchors to specs (reverses D40 #4 at `:1239`), a cross-vendor
  second auditor (on the Don't Build list at `:5515` with κ=0.80 measurement), a
  per-lens audit log (duplicates `:2163`'s per-category precision), running the
  coverage script at more stages (the script does not exist), restating the
  operator gate (already at `:177–193`).
- **The one surviving finding, and it is not a proposal:** there is **no stage at
  which a solution's shape is a reviewable artifact.** `:1233` — *"**No
  implementation plan.** The builder writes that."* Both audit stages are
  completeness checks. Whether that is a hole or a deliberate consequence of
  one-context sizing **is not written down anywhere.** Worth a paragraph in §3.6
  before the contracts are drafted, because it decides whether an eleventh
  contract exists.
- **Panels and multi-model voting are rejected on measurement**, not taste:
  9 spawns → 2.18 effective votes, overcommitment +4.7%, minority dissent
  suppressed in 48% (`:5514`). An outside operator running 3×3 panels was the
  source of the rejected proposals; his conditions (one product, no frontend, no
  clients, 3× $200 seats) do not transfer.

## Next Steps

1. ~~Write the (a) decision~~ — **done, D116.**
2. **Write the panes decision** (next free D-number) — Active Problem 4. The
   question is now purely D80's asymmetry, not auth.
3. **Settle the shape question** (Key Decisions, item 3) — one paragraph in §3.6.
   It determines the contract count before you write contracts.
4. **Measure `--allowedTools "Skill(name)"`** — Active Problem 5. One spawn.
5. **Draft the ten contracts as agent files.** Order: `spec-writer` →
   `spec-auditor` → `builder` → `grader` → `reviewer` → `plan-auditor` →
   `research` → `reuse-scout` → `charter-reviewer` → `probe`. Each gets §4.4's
   **nine fields**, no more, no fewer; `tools:` always carries `StructuredOutput`;
   `model:` from the table below. Decide Active Problem 6 in the first file.
6. **Draft the ten `Output` schemas** — two shapes only (D82, D62): a capped
   verdict set, or a pointer `{path, summary}`. **Never a transcript.**
7. **Draft the three driver skills** — Planner, Executor, Thinker. Reference
   implementations already on disk: `~/.claude/skills/orc`, `think`, `rev`
   (659 lines for three thinner drivers, per `:5620`). The Executor owns the
   dispatch wrapper.

## Reference

**Paths**
- Design: `~/Projects/do-it/design/system-design-v2.md` (6,012 lines) · copy at
  `~/.claude/do-it-v2/system-design-v2.md` — keep identical
- Register: `~/.claude/do-it-v2/open-topics.md` — one table row per D
- Code: `~/Projects/do-it/src/` · CLI: `doit` (see `doit help`)
- Ledger: `~/.do-it/events/*.jsonl` · `DOIT_ROOT` moves it
- Meter tests: `~/.claude/do-it-v2/research/METER-TEST-2026-08-26.md` and
  `-2026-09-08.md` (two runs in one file)
- Probes: `~/.claude/agents/doit-probe-minimal.md`, `doit-probe-schema.md`
- Design history / open topics: `~/.claude/do-it-v2/`

**The ten contracts and their assigned models** (§4.6 — preserve these; they are
the *contracts'* models, not the drafting session's):

| # | Contract | Model | Line |
|---|---|---|---|
| 1 | `spec-writer` | **Opus** — sits next to the cut | `:2097` |
| 2 | `spec-auditor` | **Fable** — different from writer's Opus | `:2250` |
| 3 | `builder` | **Opus** (D23) — Sonnet tried and rejected | `:2299` |
| 4 | `grader` | **Fable** — different from builder's Opus | `:2393` |
| 5 | `reviewer` | **Opus** | `:2442` |
| 6 | `plan-auditor` | **Fable** — one contract, three stages | `:2501` |
| 7 | `research` | **Haiku** — retrieval over text | `:2515` |
| 8 | `reuse-scout` | Sonnet (§4.5) | `:2537` |
| 9 | `charter-reviewer` | Opus (§4.5) | `:2541` |
| 10 | `probe` | **Opus** | `:2577` |

*(Line numbers as of the D116 pass; verify with `grep -n '^### '`.)*

**§4.4's nine fields, required on every contract:** Input · Blindness · Output ·
Writes · Tools · Model · Budget · May declare · Learns.

**Drafting rules that bind — these are measured, not stylistic:**
- **Prohibition lists are the wrong form** (correction #2). A prohibition arm
  produced *more* unwanted content than a positive-recipe arm. Use **required
  slots**. The banned-phrase list survives only as the auditor's grep vocabulary.
- **Blindness is constructed, never instructed** (correction #6, load-bearing).
  Recency metadata moved verdicts +30%, provenance labels +18%, Cue
  Acknowledgment Rate **0**. Blindness lives in the packet script's strip list,
  never in the prompt.
- **"Prose does not fire."** `nu` fired once in 216 sessions under a standing
  global mandate. A field not enforced by the fold or a hook is decoration.
- **Admission test for every field:** does it convert a silent failure into a
  loud one? If not, delete it.
- **Byte-identical prefix** across all spawns of a type; variable content last.
- **Sub-agents do not spawn sub-agents.** No `tools:` line grants `Agent`.
- **Permitted skills are named in the contract** (D66); anything unnamed is out
  of path — mechanism pending Active Problem 5.
- **Budgets are never uncapped** (correction #9) — `budget-exceeded`, `loop` and
  `context-exhausted` cannot fire without a cap. Under D116 the cap is *checked*:
  the spawn's `usage` comes back in the JSON result.
- **§7.3's landing test:** a change ships as a hook, a fold rule, a required
  field, or a calibration example — **or it did not ship.** A change whose
  landing artifact is prose is scored not-landed.

**Environment note:** the seat probe is per-machine and per-CLI-version. **On CLI
upgrade re-run rows M, R, I2 and Q** of the 09-08 file — seat from a plain
environment, the full line, the `tools:` binding, the MCP leak. Auth changed
under the design once already (2.1.246 → 2.1.263). A failed re-run is a design
event.

## Session Log
- 2026-09-08 (second session): D116 written and carried. Second meter run, 19
  spawns, ~$0.52, $0 metered: cold `env -i` failures were `USER`; a plain
  environment reaches the seat; `--bare` is metered by its own help text;
  `--json-schema` needs `StructuredOutput` in `tools:`; MCP leaks under `-p`
  without `--strict-mcp-config`; the full line measures 7,440 tok. Freeze
  lifted, four HELD blocks re-grounded, §4.4 Output row fixed. No code changed.
- 2026-09-08 (first session): Audited a Chezky transcript for concrete
  suggestions; six proposals drafted, then blind-audited and five cut. Ran the
  §12.5 meter probe (7 spawns): schema path draws the seat, D104 overturned,
  `--bare` isolated as the cause. Wrote `METER-TEST-2026-09-08.md`. No code
  changed.
- Earlier: design v2 completed to D115; build group ① + merge gate shipped and
  running; one real charter closed (this system building itself).
