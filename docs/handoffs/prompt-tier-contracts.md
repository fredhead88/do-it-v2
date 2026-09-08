# Prompt Tier — Ten Contracts, the Wrapper, the Tick, Three Drivers

## Status
**The wrapper, the tick, and the Executor contract exist and have driven a
throwaway spec from `written` to `accepted` on ticks alone.** **All ten contracts have now run on their own models.** **The
packets are a script now, not the Executor's prose** — `doit packet` builds each
role's Input list from the ledger and refuses to write a packet carrying what
that role's Blindness strips. The remaining work is queued under Next Steps as a checklist that
`scripts/drive.sh` executes unattended, one fresh session per item, with the
handoff as the only state. **The Planner pane exists and loads**, and so does **the Thinker session**: `doit think`
opens it, and `doit think --land` is the one place a charter's five sections, its
`review_path` and its `Covers:` are ever checked. **The install gate exists and fires**: `scripts/vet-dep.mjs` is a `PreToolUse`
hook registered in this repo's `.claude/settings.json`, and §10.4's logging
wrapper is `doit paid-call`. The audit scripts, the deploy and tree-cleanup
scripts, and the one real charter are what is left in that queue.
Last updated: 2026-09-08 (eighth session — the driver loop's fifth item)

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

**Built and running** [`doit test`: fold 55 + merge-gate 73 + dispatch 17 mocked
spawns + packet 22 + tick 11 + up 31 + think 57 + paid-call 27 + vet-dep 73; five
real spawns through the wrapper]:

| File | Lines | What |
|---|---|---|
| **`src/fold.py`** | **~456** | append-only ledger, derives all state, renders board; actor derivation keeps a hyphenated role whole; `tick-stale` HEALTH line (D117). **Now also: `DECLARES` — every contract's May-declare list is authorization, so a term off a role's list is recorded and ignored like any other stamp; `must-fix` blocks acceptance beside `rejected-criterion` (D101); the NEWEST `charter-review-*` verdict decides L2 and only the charter-reviewer may give it; `over_budget()` compares every `spawn-done` against `dispatch.ROLES` (and the Executor's against the tick's cap) onto HEALTH; an overdue `question` and an unanswered `escalation-blocking` render under NEEDS YOU by the tick's own rule; `dwell_days()` measures the wedge bar from the log's stage crossings** |
| `src/merge_gate.py` | 421 | catches a merge that *removes* a file nobody watches |
| **`src/dispatch.py`** | **~290** | **the wrapper.** Allocates `L-<role>-NNNN` (max+1, `O_EXCL`); `build-started` before a builder spawn; the D116 line; then in order: `is_error` → `spawn-failed` (+ `escalation-blocking` on `api_error`), null `structured_output` → failed, `contamination` → failed, file/dir at `--path` or failed, `git status --porcelain` unchanged across a non-builder spawn or failed; then the contract's events from the Output object, then `spawn-done` with `usage`. Refuses an identical packet+contract that already failed as a refusal or contamination — before it spends. Files ADRs, renders the card, ends with a tick poke |
| **`src/tick.py`** | **~85** | **D117.** `flock`; fold; `tick{lane, spawned}` event; spawns `-p --agent executor` with the executor schema and `--disallowedTools` naming §10.5's RETIRE list, only when the lane is actionable; idle spawns nothing; a null Output is a failed spawn; the Executor's `actions[]` land on its `spawn-done` |
| **`agents/executor.md`** + `.schema.json` | **157** | **the Executor contract** — the lane-state → action table, the per-role packet recipes (hand-built until the packet scripts exist), the worktree cut, the rules that bind, `actions[]` Output. Spawned only by the tick. `doit dispatch --detach` is its one spawn path; `doit events <subject>` its one ledger read |
| **`scripts/drive.sh`** + `docs/handoffs/driver-prompt.md` | 43 + 38 | **the unattended driver** for Next Steps: one fresh `claude -p` (Opus, $10 cap) per unchecked item; refuses a dirty tree; stops on a red suite, an uncommitted step, or a `- [!]` item; `touch $DOIT_ROOT/drive.stop` stops it |
| **`src/packet.py`** | **~300** | **the packet builder.** `doit packet <role> <subject>` writes `$R/packets/<subject>-<role>-<n>.md` from the ledger and content dir, for the six roles the Executor dispatches. The Input list is the per-role builder; **the Blindness list is `strip()`, which pulls this subject's *real* forbidden strings — the builder's spawn id and branch, the grader's reasons, a sibling spec's body, the Plan's rationale — and refuses before writing.** Also writes `$R/content/verify-<spec>.sh` from the spec's `## 8. Verification` block |
| `src/test_packet.py` | ~200 | per role: the Input list it must carry, the honest packet scanned against its real strip list, and the builder forced to leak one so the refusal fires |
| `src/test_dispatch.py` · `src/test_tick.py` | ~160 | every after-the-fact check exercised against the failure it was written for |
| **`agents/planner.md`** | **~175** | **the Planner pane** (§3.5, D80) — the seven-step cycle with its exact command lines, the Plan's nine required sections as slots, the sweep's blocks-vs-owed rule, and the rules that bind (never read a spec it commissioned; read-only on code; no return path). No schema: a pane's Output is the files and events it writes |
| **`src/up.py`** + `src/test_up.py` | **~75** + ~95 | **`doit up`** — links the contract into `~/.claude/agents/`, allocates the pane's `L-planner-NNNN.jsonl`, prints the cron line and never installs it |
| **`agents/thinker.md`** | **~175** | **the Thinker session** (§3.3) — the twelfth agent file and the second interactive one: the inventory-first opening, the three shapes, the charter's five sections written out as the template `--land` enforces, §7.9's triage rules (adjacent only, never deletes, name the master threads), and the rules that bind (spawns nothing; read-only on code; requirements, never execution shape) |
| **`src/think.py`** + `src/test_think.py` | **~200** + ~190 | **`doit think`** — opens `claude -n think-<topic> --agent thinker` (named, so §8.9's `creative` counter is derivable), links the contract where `--agent` looks, allocates `L-thinker-NNNN.jsonl`. **`--land FILE …` is the only place a charter is checked**: five sections, a requirement with a stable id, a `review_path` with both halves, a non-empty `Covers:`, no execution-shape heading — then `charter-filed`, then the both-directions coverage diff and a detached `plan-auditor` at **stage `charter-set`** (D98). `--discard` leaves one line so "ended with nothing" and "still open" differ |
| **`scripts/vet-dep.mjs`** + `.claude/settings.json` | **~230** | **the install gate (§6.7b), and it IS the vetting script.** A `PreToolUse` hook on Bash, registered in this repo's settings so it fires under any permission mode. Detects an install *with a package argument* in any segment of a compound command (heredoc bodies dropped, anchored at the segment start so writing ABOUT an install is not an install); lets `npm ci`, a bare install and `-r` through; **blocks a BUMP before any network call** — a package already in the manifest at the install's own cwd; then four checks — exists (a 404 is named as the hallucinated-name case), 7-day cooldown, OSV `MAL-` **against the version that would be installed**, license allowlist. **Fails CLOSED on a definite bad answer, OPEN with a recorded `install-warned` on an inconclusive one.** `DOIT_INSTALL_OVERRIDE='<why>'` is the sanctioned override and writes `install-override`; `fold` renders both counts on HEALTH, because the override count is the metric that says the cooldown is set wrong. Also `doit vet-dep [--pypi] <pkg>` — the scout's ground truth, raw values and `pass: true|false|null` |
| **`src/paid_call.py`** | **~95** | **§10.4's logging wrapper.** `doit paid-call <label> --cap USD -- <cmd…>` writes `spend.jsonl` in the run directory: label, argv, cost, cumulative, cap, exit. **There is no uncapped mode** (§4.4 correction #9). The cost is the command's own `total_cost_usd`/`cost_usd`, else `DOIT_CALL_USD`, else logged as `unpriced` — never silently free. The call after the cap is crossed does not run (exit 3). `probe` is the one contract that spends before anything lands on head, so this wrapper, not the pre-dispatch gate, is what caps it (D96) |
| `src/test_vet_dep.mjs` · `src/test_paid_call.py` | ~200 · ~100 | 73 + 27 checks. The registry is stubbed — a guard whose test needs the network is a guard whose test gets skipped — and the two network-free hook paths run as real subprocesses, because exit 2 is the whole mechanism |
| `src/backup.sh` | 69 | one-way restic push + restore drill |
| `agents/<name>.md` × 10 · `<name>.schema.json` × 10 | 986 · 1692 | the ten contracts and their Output schemas (unchanged this session) |
| `doit` | | `dispatch` (`--detach`), `packet`, `tick`, `up`, `alloc`, `events` subcommands; `test` runs all six suites |

**Spawned for real, key unset** (five in the 09-08 meter file; three through the wrapper, each in a scratch `DOIT_ROOT` — nothing touched `~/.do-it`):

| Contract | Model | Ran? | Notes |
|---|---|---|---|
| `research` | haiku | **yes** — a real dig into `fold.py`, correct | Write needs the whole-tool grant (D120) |
| `grader` | Fable | **yes** — contamination exit | |
| **`spec-auditor`** | Fable | **yes, through the wrapper, on a `doit packet` packet** — 6 findings + 6 rejected on a deliberately flawed scratch spec, `bad_cut: false`, `contamination: false`; 20 turns, 114 s, $1.25 | it read the real repo: named `test_fold.py`'s 33 fixed assertions, `EMITS`, and `tick.py`'s base dict. Earlier run: safeguard bisected, D120 |
| `plan-auditor` | Fable | **yes**, after the schema fix | |
| `charter-reviewer` | Opus | **yes** — contamination exit | |
| **`spec-writer`** | Opus | **yes, through the wrapper** — 199-line spec, eleven slots, two typed ACs with review paths, a three-step Verification including a mutation test; 9 turns, 79 s, $0.34 | `spec-written` + `spawn-done` appended; repo status unchanged |
| **`builder`** | Opus | **yes, through the wrapper** — one commit on its branch, worktree clean, the spec's own verify block exits 0; 14 turns, 100 s, $0.60, no denials | `build-done`, 2 × `build-deviation` (minor), `worked`, `spawn-done`; card 11 lines |
| **`executor`** (driver) | Opus | **yes, nine ticks** — dispatched grader, rework builder, re-grade, reviewer; decided one builder question; escalated twice on real gaps with the fix named in the text; merged through the gate. 6–12 turns each | every action on its `spawn-done` |
| **`reviewer`** | Opus | **yes, through the wrapper** — `gates-only`, round 1, no blocking finding, one `unverifiable`; first run of the contract | `review` + `spawn-done` |
| **`reuse-scout`** | Sonnet | **yes, through the wrapper** — one need, two candidates (one `previously_rejected` off the ADR trail, unre-evaluated), `nothing_cleared: true`; 6 turns, 78 s, $0.21; session `5c5127ff-abff-4912-bef9-8e9195668615` | it ran `vet-dep` and reported its raw values rather than its own recollection — **and correctly scored two undetermined gates as failing**, which is how two `vet-dep` defects were found |
| **`probe`** | Opus | **yes, through the wrapper** — 6 wrapped calls to 3 real externals on 3 real packages, raw outputs captured before reading, run record written, $0.006 of a $0.50 cap logged; 10 turns, 113 s, $0.55; session `e52ae0a2-f6a2-42b1-bbb3-bc0e70ac73ae` | declared two real `charter-gap`s, one of which is a defect in `vet-dep` itself (below) |

**Not written:**

| Missing | Size | Kind |
|---|---|---|
| §3.6's six audit scripts · `scripts/vet-dep.mjs` | ~150 | code |
| Rest of fold's rules: typed ACs, blind grading | ~80 | code |
| `doit doctor` — `agents/*.md` vs `~/.claude/agents/` (Active Problem 15) | ~15 | code |

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

### 9. All ten contracts have now run — one depth has not
`probe` and `reuse-scout` ran for real this session, each on its own model with
its own schema, so D120's trust condition is met for all ten. **What is still
unrun is a depth, not a contract:** the reviewer has only run `gates-only`; its
`full` depth needs a browser `--mcp-config` and the capability-scoped review
account, and the charter-reviewer holds the same account (D99). Neither exists.

### 10. The fold's vocabulary — done for declaration terms, open for typed ACs
The wrapper appends exactly the events the contracts name (`spec-written`,
`spec-killed`, `audit-finding`, `build-*`, `question`, `verdict`,
`rejected-criterion`, `criterion-cleared`, `review`, `must-fix`,
`checker-coverage-change`, `research-filed`, `reuse-scouted`, `probe-run`,
`charter-review-(not-)complete`, `gate-infra`) plus its own: `spawn-done`
(usage + the Output's top-level scalars), `spawn-failed{why}`,
`escalation-blocking`, `adr-filed`, `tick`, and **a declaration lands as an
event typed by its term** (`worked`, `spec-ambiguity{root_cause}`, …). The
fold derives from a subset. **`EMITS` now authorizes every declaration term per
role** — `fold.DECLARES` is the nine contracts' May-declare lists transcribed and
folded into `EMITS` at import, so a builder declaring `hollow` is recorded and
ignored. `escaped` is deliberately on nobody's list (the builder cannot see the
audit) and is not yet *derived* either. What remains of this item: typed
acceptance criteria and blind grading as fold rules.

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

### 12. What the first real chain taught — fixed, and carried here so nobody re-learns it
- **The seat is not metered in dollars.** Every `cost_usd` in the ledger is the
  CLI's list-price estimate (`costBasis: list`) under seat auth — the meter
  file's "$0 metered". It is a *size* measure for Budget (§4.4) and nothing
  else. The real budget is the seat's usage window (D94); `scripts/drive.sh`
  sleeps across it and retries the same item.
- **Only the builder had a start event, so nothing marked a grader or reviewer
  as in flight** and a cron tick could dispatch the same work twice. The wrapper
  now appends `spawn-started{role}` for every non-builder role; the tick keeps
  any subject with an open start off the lane; a start older than twice its
  role's cap with no terminal event is recorded once as `spawn-stale` and the
  subject returns to the lane for the Executor's failed-spawn row.
- **The grader never cleared a rejection it re-tested as met**, so a rework
  could never reach `accepted`. The wrapper now appends `criterion-cleared`
  for a standing rejection whose criterion a later grade marks `met`; the
  grader is an authorized clearer in `fold.EMITS`.
- **The Executor's first table looped**: "standing rejection → rework builder"
  would re-fire after the rework landed. Rewritten around **the newest of
  `build-done` / `verdict` / `review`** — a rework re-enters at `build-done`
  and gets a re-grade, not another builder.
- **Residue in a worktree is a rejection waiting to happen.** Running the
  verify script in the worktree after the build (to check the builder's claim)
  left `__pycache__/`, and the grader rightly rejected the done-condition. The
  grader's own checker run leaves the same residue. The done-condition for a
  repo without a `.gitignore` for bytecode cannot be "porcelain empty";
  the packet script should phrase it as "no tracked file modified, no
  untracked file outside `__pycache__/`" or the charter must ignore bytecode.

- **The spec template has no `Writes:` line, and the merge gate needs a grant.**
  The gate returned could-not-determine on the first real merge. The Executor
  now passes the `spec-written` event's `footprint` as `--writes`; the packet
  script step should also add a `Writes:` line (= the footprint) to the spec
  template so the gate can read it from the file. A could-not-determine is
  escalated, never forced.
- **The gate wrote its verdict as `L-executor-0001`** (a pane-era default);
  the tick now sets `DOIT_GATE_LEDGER_FILE` to the current Executor's file.
  `doit gate --help` appended a rework event with subject `--help`; a flag
  where the branch belongs is now refused before any event.
- **A resolved escalation stayed under NEEDS YOU** — the tick had a rule (the
  subject returns to the lane) and the board did not. **Fixed** in the fold-rules
  item: `fold.open_escalations` is the tick's rule verbatim (newest of
  `escalation-blocking` / `decision` / `unblocked` per subject), so the two now
  give one answer.
- **Every Executor that hit a gap escalated instead of looping**, and two of
  its escalations named the fix that was then made (the grader clearing a
  re-tested rejection; the gate's grant). The escalation text is worth
  reading before diagnosing.

### 13. The Executor's own `spawn-done` carries no `project` — found by the real audit
`tick.py:82` builds its base as `{"spawn": ledger.stem}`: no `project`, no
`subject`. So a project-filtered board (§9.1/D93) drops every Executor spawn,
and **the Budget-vs-`usage` rule queued in the next item would silently miss
the driver's own spend** — the same shape as Active Problem 11's last bullet
about the `tick` event. Not fixed here: which project a tick that acted on
three charters belongs to is the fold-rules item's question, not a guess for
this one. (Found by `L-spec-auditor-0001` reading the repo, 2026-09-08.)

**Still open after the fold-rules item.** `over_budget()` now compares the
Executor's own `spawn-done` against the tick's cap and reports it on HEALTH — on
an *unfiltered* board. Under `DOIT_PROJECT` the Executor's spawn-done is still
dropped by `read_events`' filter, so the driver's spend is invisible there. The
fold-rules item declined to guess a project for a tick that acted on three
charters; the fix is either a `project` on the tick's base dict (a guess) or a
rule that a project-less event is infrastructure and never filtered out (a change
to §9.1/D93 that belongs in the register, not in a fold commit).

### 15. An agent file in this repo is invisible to `--agent` until it is linked
`claude --agent <name>` resolves through `~/.claude/agents/`, never through
`~/Projects/do-it/agents/`. The ten contracts were symlinked there by hand in an
earlier session and nothing recorded that as a step, so the Planner's first real
launch failed **after** `doit up` had printed the cron line — output that reads
like success. `up.install()` now makes the link idempotently and refuses over a
file that is not this repo's contract, naming the `ln -sfn` line. **The same hole
is open for any future contract**: `doit dispatch` reads `agents/<role>.md` off
disk for its frontmatter and hash, so it never notices, and the spawn fails at
the CLI with a message the wrapper records as `is_error`. A `doit doctor` that
diffs `agents/*.md` against `~/.claude/agents/` is the fix; it is not written.
`think.py` links the twelfth contract through the same `up.install()` — the
function now takes any contract and derives the link name from it — so the two
launchers cannot drift apart, but `doit dispatch` still notices nothing.

### 16. §3.3 and §2.1 disagree about who writes a goal
§3.3's shape table says a **brainstorm** produces *"a goal or a charter"*; §2.1's
artifact table says the **Goal** is *written by the operator*. `think.py` therefore
authorizes `charter-filed` to `{thinker, operator}` and leaves `goal-filed`
unauthorized — open to any actor, as it was — rather than guess which table wins.
It matters because `--land`'s charter-set diff reads the newest `goal-filed`
event's path: whoever may write that event chooses what every charter is diffed
against. A register decision, not a code one. (Found writing the Thinker,
2026-09-08.)

### 17. A clean OSV query is byte-identical to a query that matched nothing
Found by the first real `probe` run (`L-probe-0001`, 2026-09-08), and it is a
defect in `vet-dep`: `POST /v1/query` returns HTTP 200 with a literal `{}` both
when a package has no advisory and when the query matched nothing at all — a
wrong ecosystem string, a renamed field, a silent server-side change. `vet-dep`
reads `{}` as *no MAL- advisory* and passes the gate, which is the exact failure
R2 of the probe's charter names and the exact failure the repo's own rule
forbids: **undetermined is never clean.**

The cheap fix is a **control query** — one extra call per run against a package
known to carry a `MAL-` advisory, so "the server answered `{}` for everything"
becomes loud instead of silent. It was not taken here because it needs a
permanent control fixture, and choosing one is a register decision (which
package, and what happens when its advisory is withdrawn), not a guess inside a
driver step. Until then the malware gate is a *definite* block on a hit and a
*hopeful* pass on a miss. The version-scoping half of the same finding **was**
fixed: OSV advisories are version-scoped (chalk's `MAL-2025-46969` affects
`5.6.1` only), and a package-scoped query banned chalk forever over a version
nobody would install.

### 18. The install gate's heredoc ceiling, and it is named in the code
`packagesIn` drops everything from the first `<<` onward, because a file being
written that contains an install line is not an install — the gate fired on this
session's own `cat > file <<'EOF'` before that was true. The cost is that a real
install *after* a heredoc closes is missed. The backstop is the one that already
exists: §6.7e's acquisition-set gate at merge, which reads the lockfile diff.

### 14. The previous step's `- [x]` cannot be re-run
Its evidence — `doit states` → `L-spec-0001 accepted`, a toy repo's merge sha —
lived in a scratch root the session deleted, by design. What *is* re-runnable
held: `./doit test` green on all suites. Every later step that spawns for real
should expect the same: the ledger line is the durable claim, the scratch root
is not.

### 19. Scratch roots are now kept, and the session id is recorded
The driver prompt changed between the seventh and eighth steps: a step's scratch
root is **kept**, never deleted, because it is the evidence the next step
verifies, and each real spawn's `session` id is recorded so the CLI's own
transcript (`~/.claude/projects/<cwd slug>/<session>.jsonl`) is the durable
record if the root is ever lost. This session verified the seventh step's
thinker spawn that way after its root was gone: session
`3f0463e0-2a47-4781-920e-7b5bc31e207b`, `--agent thinker`, `claude-opus-5`, one
turn, the five sections named back in order. `~/.do-it-scratch/vet-dep-probe`
is this session's root and is kept.

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

## Next Steps — the driver's queue

`scripts/drive.sh` takes the first `- [ ]` or `- [~]` item, one fresh session
each, and stops at a `- [!]`. Each item is sized for one context. Order matters:
the packet scripts before the drivers that call them; the fold rules before the
charter that relies on them.

- [x] `agents/executor.md` + schema, `doit dispatch --detach`, `doit events`, the tick passing the schema and recording `actions[]`. Verified: a real tick on the throwaway spawned the Executor, which dispatched the grader detached with a blind packet (2026-09-08, third session).
- [x] The throwaway chain to `accepted`, driven by ticks alone (a 90-second `doit tick` loop standing in for cron): grader (Fable) rejected on residue → Executor dispatched rework → builder returned BLOCKED with a §4.9 question → Executor decided it → rework → re-grade confirmed → operator `correction` voided the stale rejection (D111) → reviewer (Opus, first run, `gates-only`, no blocking) → gate refused twice on the missing `Writes:` grant, Executor escalated, operator decided, Executor re-ran the gate with `--writes` and merged `--no-ff` → `shipped` → the fold derived `accepted`. Verified: `doit states` → `L-spec-0001 accepted`; the toy's `master` carries merge `8714276`. Nine Executor ticks, three builders, two graders, one reviewer; seven defects found and fixed on the way (Active Problem 12). Scratch root gone with the session (2026-09-08, third session).
- [x] **Packet scripts** — `src/packet.py`, `doit packet <role> <subject>`, six roles (the ones the Executor dispatches), each an Input list in code and a `strip()` list pulled from the ledger. `agents/executor.md` §Dispatching is now six command lines instead of five hand-built recipes. `Writes:` joined slot 4 of the spec template in both `spec-writer.md` and `spec-auditor.md` — the merge gate reads it from the file, and its absence blocked the first real merge. Verified: `./doit test` green, `packet: 22 packets built, six Blindness lists enforced` — per role the honest packet is scanned against that subject's real forbidden strings *and* the role's builder is monkey-patched to leak one, which must make `main` refuse before any file is written. Spawned for real (scratch `DOIT_ROOT=~/.do-it-scratch/packet-scripts`, key unset, `~/.do-it` untouched): `spec-auditor` on Fable through `doit packet` on a deliberately flawed scratch spec — 20 turns, 114 s, $1.25 list, `contamination: false`, `bad_cut: false`, 6 findings + 6 rejected, including the R3 the pre-pass fed it and three defects it found by reading this repo. Scratch root removed (2026-09-08, fourth session).

- [x] **Fold rules** — in `src/fold.py`: `DECLARES` (the nine contracts' May-declare lists) folded into `EMITS` at import; `over_budget()` against `dispatch.ROLES` and the tick's cap → a `budget-exceeded` HEALTH line, not an eleventh board section (§8.3's ten are positional); `must-fix` joined `standing_rejects` (D101) so acceptance does not depend on the wrapper also writing a `rejected-criterion` beside it; overdue `question`s under NEEDS YOU, answered by a `decision`/`unblocked` whose `ref` is the `file:line` `doit events` already prints; the NEWEST `charter-review-*` verdict decides L2 and only the charter-reviewer may give it; `dwell_days()` derives the wedge bar from the log's own stage crossings (2 × median, the 1-day default until three crossings). Also closed one AP12 bullet: `open_escalations()` is the tick's lane rule verbatim, so a decided escalation leaves NEEDS YOU. Verified: `./doit test` green, `fold: 55 checks pass` (33 → 55) — each rule has its own check *including the negative*: a builder's `hollow` ignored, an executor's `owed-ac` ignored, a charter refused its own `charter-review-complete`, a `not-complete` after a `complete` leaving L2, a `decision` answering by `ref`, an unparseable deadline read as past, and a 4-day build that wedges against the 1-day default but not against the measured 6. Re-folded the real `~/.do-it` ledger before and after: `doit states` identical and the ignored list unchanged at 3, so no historical event was newly invalidated (2026-09-08, fifth session).
- [x] **Planner driver** — `agents/planner.md` (the eleventh agent file, and the only interactive one: no schema, no `StructuredOutput`, `Skill` kept so §10.5's KEEP list survives the deny). The cycle is ⓪ probe → ① cut file + `cut-written` → ② `doit dispatch plan-auditor` at stage `cut` → ③ the Plan's nine required sections + `plan-written` → ④ plan-audit at stage `plan` → ⑤ the batched question sweep → ⑥ one `spec-writer` dispatch per slot → ⑦ clear. `src/up.py` + `doit up`: links the contract where `--agent` looks, allocates the pane's own `L-planner-NNNN.jsonl` (D90), **prints** the cron line and never installs it. Two new pieces the cycle could not run without: `doit alloc <kind>` (§2.8 max+1 under content, `O_EXCL`) because a Planner inventing spec ids hands two units the same one, and `EMITS[cut-written|plan-written] = {planner}` because the two fable audits are blind to an author the fold did not otherwise name. Verified: `./doit test` green, `up: 31 checks pass` — the cron line honours `DOIT_TICK_MIN` and carries this root and this `doit`; `crontab` appears nowhere in the module; the RETIRE list is denied by name; a missing contract and a foreign file at the link name each exit loud with the fixing line; two `doit alloc spec` calls give 0001 and 0002 and a different kind numbers from its own max; a `plan-written` from a builder and a `cut-written` from an executor are recorded and ignored while the Planner's lands. **Spawned for real, key unset** (`~/.do-it-scratch/planner-load`, `~/.do-it` untouched): `claude -p --agent planner` resolved on `claude-opus-5`, 1 turn, 3.5 s, $0.14 list — and the first attempt exited 1 with `--agent 'planner' not found` **after** the cron line had printed, which is the defect `install()` and its checks now hold. Re-folded the real `~/.do-it` before and after: `doit states` identical, ignored still 3. Scratch roots removed (2026-09-08, sixth session).
- [x] **Thinker driver** — `agents/thinker.md` (the twelfth agent file, the second interactive one) plus `src/think.py` + `doit think`. The item said `doit append charter-filed`; **the append is the wrong half of it** — a charter appended by hand is a charter nothing checked, and §3.4's rules would have shipped as prose (§7.3). So landing is a command: `doit think --land FILE …` refuses the five sections one at a time, a requirement with no stable id, a `review_path` missing either half (D99), an empty `Covers:`, a charter written outside `content/`, and an execution-shape *heading* (seams · waves · interfaces · data shapes · error handling · schema · branch — §3.4's own NOT-in-the-charter list); only then does it append `charter-filed`, and only then — if the set cites a goal — does it run the requirement-id diff **both directions** and dispatch `plan-auditor` at **stage `charter-set`** detached (D98, §3.3: the check is the driver's, because the Thinker spawns nothing). `--discard` is the equally cheap exit and leaves one line so "ended with nothing" and "still open" are different states. `fold.EMITS["charter-filed"] = {thinker, operator}` — the landing checks are what authorize the seat. `up.install()` generalised to any contract, so both launchers close Active Problem 15 the same way. Verified: `./doit test` green, `think: 57 checks pass` — every refusal with its negative (the word "seams" in a *sentence* still lands; `Covers: none` is the §12.2 adopted-project case and passes; a set with one bad charter appends *nothing*, not the good ones; a `charter-filed` from the planner or a builder is recorded and ignored; a charter citing goal ids with no goal file escalates rather than passing silently). **Run for real, key unset** (`DOIT_ROOT=~/.do-it-scratch/thinker`, `~/.do-it` untouched): `doit alloc charter` → a real charter written → `doit think --land` filed it as actor `thinker` and the fold derived `L-charter-0001 open`; `claude -p --agent thinker` resolved on `claude-opus-5`, 1 turn, 5.2 s, $0.11 list, and named the five sections back in order — the template in the body is the one the check enforces. Re-folded the real `~/.do-it` before and after: `doit states` identical, ignored still 3. Scratch root removed (2026-09-08, seventh session).
- [x] **`scripts/vet-dep.mjs`, `doit paid-call`, and the last two contracts spawned for real.** The gate is a `PreToolUse` hook registered in `.claude/settings.json` — it *is* the vetting script, not a reminder to run one: install-with-a-package detected per segment of a compound command, a BUMP refused before any network call, then exists / 7-day cooldown / OSV `MAL-` / license allowlist; **closed on a definite bad answer, open with a recorded `install-warned` on an inconclusive one**; `DOIT_INSTALL_OVERRIDE` is the sanctioned override and writes `install-override`, and `fold` renders both counts on HEALTH because §6.7b's override count is only a metric if something shows it. `src/paid_call.py` is §10.4's logging wrapper — `spend.jsonl` per call, no uncapped mode, the call after the cap does not run, an unpriced call logged as `unpriced` rather than as free. `doit alloc probe --dir` allocates the run directory; `agents/probe.md` and its schema now name `content/L-probe-NNNN/`, which is what the allocator makes. Verified: `./doit test` green — `vet-dep: 73 checks pass`, `paid-call: 27 checks pass`, `dispatch: 17 spawns mocked` (was 16). The registry is stubbed in the tests and the two network-free hook paths run as real subprocesses, because exit 2 is the whole mechanism; every three-state case has its negative (an unreachable registry is `warn` and its gate is `null`, never `false`; a definite bad answer beats an inconclusive one; a `GHSA` is not a `MAL-`; 7.5 days passes where 2 days blocks; writing ABOUT an install is not an install). **Spawned for real, key unset** (`DOIT_ROOT=~/.do-it-scratch/vet-dep-probe`, **kept**; `~/.do-it` untouched): `reuse-scout` on Sonnet — 6 turns, 78 s, $0.21 list, session `5c5127ff-abff-4912-bef9-8e9195668615`; 2 candidates, one `previously_rejected` off the ADR trail and not re-evaluated, `nothing_cleared: true`, 2 acquisition ADRs filed including the rejection. `probe` on Opus — 10 turns, 113 s, $0.55 list, session `e52ae0a2-f6a2-42b1-bbb3-bc0e70ac73ae`; 6 wrapped calls to 3 real externals on 3 real packages, raw outputs captured before being read, `spend.jsonl` showing $0.006 of a $0.50 cap, a run record, and two real `charter-gap` declarations. All ten contracts have now run on their own models (D120). **Three defects in `vet-dep` were found by the two real runs, not by its tests, and all three are fixed with their own regression checks**: PyPI moved the license to `license_expression` (PEP 639) so every modern Python package read as undetermined; PyPI publishes no download count so `alive` printed `?` and read as undetermined; and OSV advisories are **version-scoped**, so a package-scoped query banned `chalk` forever over `MAL-2025-46969`, which affects `5.6.1` only. Two more found and carried as Active Problems 17 and 18. Re-folded the real `~/.do-it` before and after: `doit states` identical, ignored still 3 (2026-09-08, eighth session).
- [ ] **§3.6's audit scripts** — `src/audit.py`: same-wave footprint overlap; the `Consumes:`/`Produces:` graph; a shared name introduced twice with no owner; the requirement-id diff in both directions (units vs charter; **charter set vs goal already exists as `think.coverage()` — move it, do not write a second one**); the size heuristic against §4.3; the ADR-trail diff. Its output is the plan-auditor's packet item 3. Tests.
- [ ] **`deploy` and `tree-cleanup` scripts** (§4.11) — `src/deploy.py`: serial, waits, verifies the sha is live, `deploy-started` / `deploy-landed` / `deploy-failed`; `src/tree_cleanup.py`: reaps only the provably dead (patch-id ancestry, clean worktree, nothing unmerged), `tree-reaped{reaped, retained, retained_reason}`. Update the Executor's merge and close rows to call them. Tests.
- [ ] **One real charter through the ten contracts** (§12.2 step 3) on this repository under `~/.do-it`: the Thinker files it, the Planner cuts, plans and specs it, the tick drives it to L2-complete (run `doit tick` every 90 s in a loop for the duration — installing cron is the operator's, never the step's). Keep it one wave — for example "the board shows spend per project from `spawn-done`". Record the baseline (D100) in `docs/handoffs/baseline-2026-09.md`: spawns, cost, turns, escalations, what the operator had to do.

## Reference

**Paths**
- Wrapper: `~/Projects/do-it/src/dispatch.py` · tick: `src/tick.py` · pane: `src/up.py` · `doit dispatch -h`
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

**The eleventh and twelfth agent files are the Planner and the Thinker** — panes, not
contracts: no schema, no `StructuredOutput`, `Skill` on the `tools:` line, launched
interactively by `doit up` and `doit think` and never by the wrapper. Both are
symlinked into `~/.claude/agents/` like the ten. The Planner is standing and holds one
charter; the Thinker is ephemeral and ends when its artifact lands.

**The ten contracts** (models are the *contracts'*, from §4.6):

| # | Contract | Model id | Spawned on its model |
|---|---|---|---|
| 1 | `spec-writer` | `claude-opus-5` | yes (wrapper) |
| 2 | `spec-auditor` | `claude-fable-5-1` | yes |
| 3 | `builder` | `claude-opus-5` | yes (wrapper) |
| 4 | `grader` | `claude-fable-5-1` | yes |
| 5 | `reviewer` | `claude-opus-5` | yes (wrapper) |
| 6 | `plan-auditor` | `claude-fable-5-1` | yes |
| 7 | `research` | `claude-haiku-4-5-20251001` | yes |
| 8 | `reuse-scout` | `claude-sonnet-5` | yes (wrapper) |
| 9 | `charter-reviewer` | `claude-opus-5` | yes |
| 10 | `probe` | `claude-opus-5` | yes (wrapper) |

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
- 2026-09-08 (eighth session, driver item 5): the install gate and the last two contracts. `scripts/vet-dep.mjs` + `.claude/settings.json` (73 checks), `src/paid_call.py` + `doit paid-call` (27 checks), `doit alloc <kind> --dir`, two HEALTH lines for the override and warning counts, `install.sh` now refuses without node because a gate that cannot run is not a gate. `reuse-scout` (Sonnet) and `probe` (Opus) spawned for real, which completes D120's one-spawn-per-contract condition. Two defects in `dispatch.py` had to be fixed before the probe could run at all, and both were invisible to a suite that had never dispatched a `dir`-kind role: **`porcelain()` read "not a git repository" as undetermined and refused the one contract whose cwd is outside every repo on purpose**, and the `--path` check compared a trailing-slash run directory against a bare name. The probe then found a defect in `vet-dep` that `vet-dep`'s own tests could not — OSV advisories are version-scoped — which is the probe doing exactly what §4.6·10 says it is for.
- 2026-09-08 (seventh session, driver item 4): the Thinker. `agents/thinker.md` and `src/think.py` + `doit think` (open · `--land` · `--discard`), `src/test_think.py` (57 checks) wired into `doit test`, `charter-filed` authorized to the Thinker and the operator in `fold.EMITS`, `up.install()` generalised so both launchers link their contract the same way. The item's `doit append charter-filed` became a landing command instead: §3.4's five sections, the `review_path` and the `Covers:` line only bind if something refuses the file, and nothing downstream ever re-reads a charter for shape. The charter-set audit (D98) landed with it — the coverage diff runs both directions and the packet carries stage `charter-set`. One finding carried as Active Problem 16: §3.3 and §2.1 disagree about who may file a goal, so `goal-filed` was left unauthorized rather than guessed.
- 2026-09-08 (sixth session, driver item 3): the Planner pane. `agents/planner.md` (the eleventh agent file, interactive, no schema), `src/up.py` + `doit up`, `doit alloc`, `cut-written`/`plan-written` authorized to the Planner in `fold.EMITS`, `src/test_up.py` (31 checks) wired into `doit test`. The pane was launched for real and the first launch failed `--agent 'planner' not found` — an agent file in this repo is invisible until it is symlinked into `~/.claude/agents/`, a step nothing had recorded (Active Problem 15). `up.install()` is that step, and the second launch loaded on Opus in 1 turn.
- 2026-09-08 (fifth session, driver item 2): the fold's remaining rules. `fold.py` 315 → 456 lines, `test_fold.py` 33 → 55 checks. Two authorization holes closed that nothing had noticed: any actor could emit `owed-ac` and walk a shipped spec into `shipped-owed-evidence`, and any actor could emit `charter-review-complete` — a charter could review itself closed. Two existing tests moved their events to the actor whose contract declares them. The board keeps its ten sections; the Budget comparison is a HEALTH line.
- 2026-09-08 (fourth session, driver item 1): `src/packet.py` + `src/test_packet.py` written; `doit packet` wired; the Executor's five hand-built recipes replaced by six command lines; `Writes:` added to slot 4 of the spec template. `spec-auditor` spawned for real on Fable through the new packet — clean, 12 findings, and three of them were defects in *this* repo that a grep could not have found. Two findings carried here as Active Problems 13 and 14.
- 2026-09-08 (third session, end): the throwaway reached `accepted` on ticks alone; nine of ten contracts have run; the gate gained `--writes`-composes-with-`--spec`, a refused `--help`, and the tick names its ledger file; `scripts/drive.sh` started on the Next Steps queue.
- 2026-09-08 (third session, later): the first real chain ran tick → Executor → grader → Executor → rework builder; the grader (Fable, 5 turns) rejected the done-condition on `__pycache__/` residue and the second Executor (12 turns) correctly dispatched a rework. Four gaps found and fixed (Active Problem 12): `spawn-started` for every role, in-flight and stale handling in the tick, re-grade clears a standing rejection, the Executor's rows re-cut around the newest event. Cost figures corrected to seat usage.
- 2026-09-08 (third session, continued): `agents/executor.md` and its schema written; `doit dispatch --detach`, `doit events`; the tick passes the schema and records the Executor's actions. A real tick on the throwaway spawned the Executor (Opus, 8 turns, $0.39), which wrote a blind grader packet and dispatched the grader. `scripts/drive.sh` and the driver prompt written; Next Steps rewritten as its queue.
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
