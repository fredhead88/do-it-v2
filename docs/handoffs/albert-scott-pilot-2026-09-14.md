# Albert Scott pilot — the second real charter, and what it says the system must change

Started 2026-09-14. The charter is `L-charter-0001` under the real `~/.do-it` on the
Albert Scott builder box: v4 spec 1447, the deploy blocker (test_903 AC9 red on master,
58 specs merged-undeployed behind it). Driven from one interactive pane, session
`session_01P8pr4wmcCawGhG7xUBudJp`. This is §12.2 step 3 for an adopted project
(`goal: null`, `Covers: none`).

**This document is the systemic record, not the patch log.** Each entry names the
mechanism that failed or was missing, what was done for the pilot, and what the
system itself has to become. A patch that is only a patch is marked as such.

## Standing constraint that shaped everything

**`claude -p` is banned in the Albert Scott project as metered spend** (their spec 572,
their CLAUDE.md, reaffirmed in their 2026-09-09 recovery handover: *"V2's contrary
billing discussion does not supersede Albert Scott's current ban"*). v2's D116/§4.2
argues `env -u ANTHROPIC_API_KEY claude -p` is seat-billed; the operator has ruled
otherwise for this project after observing ~$25 of metered spend on 2026-07-31.
Recorded in the ledger as a `decision` on subject `pilot-1447`.

Every headless layer in v2 is that line: `dispatch.py` (every contract), `tick.py`
(the Executor), `drive.sh` (the self-build loop), and `think.py --land`'s charter-set
audit. None of them can run here.

## Changes the system needs — ranked by how much of the design they touch

### S1. A spawn backend is a first-class seam, and there must be at least two
- **Mechanism:** `dispatch.py` hard-codes `claude -p …` as the one way a contract
  runs. The after-the-fact checks (D116/D120) are the valuable part and they were
  welded to the spawn.
- **Pilot:** `doit dispatch --seat` / `DOIT_SEAT=1` (commit `cab0f59`) — the wrapper
  writes `$R/seat/<spawn>.packet.md` and `<spawn>.cmd.json` (the line it *would* have
  run), prints where it expects `<spawn>.result.json`, and waits. An interactive pane
  runs the contract as an Agent-tool sub-agent, writes the result envelope (same
  shape as `--output-format json`), and the wrapper finishes: every check, every
  event, plus `spawn_path: seat|claude-p` on the terminal event so the routes are
  distinguishable in the ledger forever. Tested (`test_dispatch.py`, 19th spawn).
- **Systemic:** make the backend an explicit interface — `claude-p`, `seat`, and a
  future `codex` — chosen per ledger root, not per call. The seat backend needs a
  *driver* that is not the operator's fingers (see S2). `cost_usd` on a seat spawn is
  `null` and must render as *unmeasured*, never as `$0.00` (R3 of L-charter-0002).
- **Fix status (2026-09-15):** **shipped 0.2.0** (`c8c1a8c` + `models.py`): the backend is a first-class seam — `models.toml` names `pane` · `seat` · `claude-p` · `codex` per contract, and `dispatch.py` has all three dispatchable backends. Verified by unit tests and one live `codex exec` smoke; a real contract on codex is not yet run. Still open: `cost_usd: null` rendering as *unmeasured* on the board (`pilot-retro-change-list.md` b-1).

### S2. The Executor cannot be a tick where the tick cannot spawn a model
- **Mechanism:** D117 makes the Executor a cron job (`doit tick` → `claude -p --agent
  executor`) whose own dispatches are detached `claude -p`. Under a seat-only backend
  there is no process that can call the Agent tool except an interactive pane.
- **Pilot:** the Executor is this pane, writing as `L-executor-NNNN`. The tick is not
  installed (`doit up` printed the cron line and it was left uninstalled, as designed).
  HEALTH renders `last tick: never`, which is true.
- **Systemic:** two honest options, and the design should name one: (a) the Executor
  is a *standing pane* again under a seat backend, with the tick reduced to a poke
  that wakes it (loses "kill-and-respawn-safe job", keeps statelessness); or (b) a
  seat-capable driver exists — e.g. a Codex CLI session that can dispatch — and the
  tick targets it. Until then §3.1's "how it is started, and what keeps it alive" is
  false for any project with the ban.
- **Fix status (2026-09-15):** **half shipped 0.2.0**: the map declares the Executor a `pane` on this root and the tick refuses (verified on this root, `tick{refused}` 2026-09-15T05:58:31Z). The design text (§3.1/D117) still says the Executor is a tick — open, `pilot-retro-change-list.md` b-1.

### S3. Three roles in one pane is allowed by the fold and unguarded by anything
- **Mechanism:** the actor is the filename (D90). Nothing stops one pane from writing
  as `L-thinker-0001`, `L-planner-0001` and `L-executor-0001` in turn. The pilot does
  exactly that (recorded in the `pilot-1447` decision).
- **Pilot:** accepted. The rationale-blindness the audits depend on (§3.6) still holds
  *for the audits*, because the packets carry no reasons — but the Planner and the
  Executor now share a context, so "the Planner never reads a spec it commissioned"
  is a discipline, not a structure.
- **Systemic:** if seat-only is a supported mode, say which role collapses are
  permitted and which are not, and have the fold count role-switches per session id
  on HEALTH (the `session` field is already on every terminal event).
- **Fix status (2026-09-15):** **open** — the HEALTH role-switch count is `pilot-retro-change-list.md` a-16; which collapses seat-only permits is b-1.

### S4. The Agent tool cannot enforce a contract's `tools:` line per call
- **Mechanism:** §4.4 says the `tools:` line *is* the sandbox, enforced by
  `--allowedTools`. An Agent-tool sub-agent of the `general-purpose` type has every
  tool; the contract's line becomes an instruction in the prompt.
- **Pilot:** enforcement falls to the after-the-fact checks only (repo status
  unchanged across a non-builder spawn, file at `--path`, contamination). **Measured:**
  the Agent tool's type list is fixed when the pane starts (`Agent type 'research' not
  found. Available agents: blind-spec-auditor, claude, …`), so contracts linked into
  `~/.claude/agents/` after launch are invisible to it. Every contract is pasted into
  the sub-agent's prompt as `general-purpose`, with its `tools:` line as an
  instruction. **Later measured:** about an hour after `install.sh`, the same pane's
  Agent-type list DID refresh and now lists every contract (`builder`, `grader`,
  `spec-auditor`, …) with its `tools:` line — so the harness re-reads
  `~/.claude/agents/` on some cadence, not only at launch. Spawns after that point can
  use the contract as the type and get the sandbox for free; the first ten in this
  pilot ran as `general-purpose`.
- **Systemic:** if the seat backend is real, the contracts must be loadable as agent
  types by the interactive CLI, and `install.sh` should verify that (Active Problem 15's
  `doit doctor`).
- **Fix status (2026-09-15):** **measured, then closed by 0.2.0**: the harness does load the contracts as Agent types (refreshes on a cadence); the typed spawns lost the seat loop (S18) and 0.2.0 gives every judging contract `Write`+`Bash` and a *Seat route* section so a typed spawn can serve it. `install.sh` verifying loadability (`doit doctor`) is open, a-16.

### S5. Model substitution changes which safeguard rules apply
- **Mechanism:** `grader`, `spec-auditor`, `plan-auditor` are `model: claude-fable-5-1`.
  The Agent tool substitutes Sonnet for Fable silently; the project rule is "opus for
  audits". D120's per-model, per-word safeguard rule was measured on Fable.
- **Pilot:** fable contracts run on Opus, passed explicitly. The terminal event's
  `model` is whatever the result envelope says, so the ledger is honest.
- **Systemic:** the contract's `model:` should be a *preference* the backend maps,
  with the mapping recorded once per ledger root, and `contract_sha256` re-trust
  (D120) keyed on `(contract, model actually used)`.
- **Fix status (2026-09-15):** **shipped 0.2.0 and verified on charter 3** (R7): 17 spawns, every `model_requested == model_used`, seven `(contract, model)` trust runs stamped `first_on_model`.

### S6. The Planner's own packets are hand-built, and the rule says packets never are
- **Mechanism:** `packet.py` builds packets for the six roles the *Executor*
  dispatches. The Planner's ② and ④ (plan-auditor at stages cut/plan), ⓪ (probe),
  ⑥ round one (spec-writer's slot) and the research/reuse-scout packets are composed
  by hand — executor.md's "the packet is a script's output, never yours to compose"
  does not reach them.
- **Pilot:** the stage-cut packet was assembled by shell from the charter's sections,
  the cut file and the `doit audit` block. It carried no rationale, and the wrapper's
  `audit.HEADER` check accepted it.
- **Systemic:** add `plan-auditor` (all three stages) and `spec-writer --slot` to
  `packet.py`, with a strip list for the Planner's rationale (the one thing §3.6 makes
  those auditors blind to). Until then the blindness is manual.
- **Fix status (2026-09-15):** **half shipped** (charter 3, unreleased): the rework packet reads `content/slot-<spec>.md` itself — the `--slot` hand-typing is gone. The plan-auditor packets (all three stages) and round-one slots are still hand-built (`mkpacket.py` / `mkslot.py` in the retro pane's tmp dir) — a-14.

### S7. Cut-file documentation gap: an empty seam label vs `none`
- **Mechanism:** `audit.fields()` splits a label's value into names. `Consumes: none`
  is a seam named `none` with no producer → a false `undefined seam` finding. An
  *empty* value is what a unit with no seams must write, and nothing says so.
- **Pilot:** `Consumes:` / `Produces:` left empty; pre-pass clean.
- **Systemic:** `planner.md` ① should show the no-seam form; or `split()` should
  treat `none`/`—` as empty.
- **Fix status (2026-09-15):** **open** — a-16 (`audit.fields` treats `none`/`—` as empty).

### S8. `deploy-failed` conflates "the gate refused" with "this merge broke it"
- **Mechanism:** `deploy.py` returns one status for command failure, sha never named,
  and timeout; executor.md's next line is `git revert -m 1`. Albert Scott's deploy
  command is `predeploy_gate.sh && deploy.sh`, and the gate can refuse for a red this
  merge did not introduce (the charter's own "known trap": more pre-existing reds
  behind the fail-fast plugin).
- **Pilot:** the charter states the ruling in Constraints — a gate refusal caused by
  a red this charter did not introduce is `blocked-external`, not a revert. The
  Executor (this pane) will apply it by hand if it fires.
- **Systemic:** `doit deploy` needs a distinguishable exit for "the deploy command
  refused before touching the target" (gate) vs "the target changed and the check
  never named the sha" (rollback). Only the second is §5.8's case.
- **Fix status (2026-09-15):** **open** — a-4 (`deploy-refused` exit 2 vs `deploy-failed` exit 1); measured again as S17 and S30.

### S9. The seat route had no schema enforcement, and the wrapper never had any of its own
- **Mechanism:** on the `claude -p` route the CLI enforces `--json-schema`; `dispatch.py`
  therefore never validated `structured_output` itself. On the seat route nothing
  enforces it. The first seat spawn returned a 440-character finding against a
  400-character cap and the wrapper would have appended it as an `audit-finding`.
- **Pilot:** the wrapper now validates every Output against the contract's schema on
  both routes (`jsonschema`; a seat spawn with no `jsonschema` importable is refused as
  unchecked). The over-long result was not edited by hand: the same sub-agent was asked
  to re-emit its object within the limits, which is what the CLI's enforcement does.
- **Systemic:** the schema is the contract's, so the *wrapper* owns enforcement, never
  the transport. Same rule as `packet.py` owning the Blindness strip.
- **Fix status (2026-09-15):** **shipped 2026-09-14** (`a8b7b76`); verified on every seat spawn since (the wrapper refused L-spec-writer-0001/0007 on schema).

### S10. A model cannot count characters; the seat route needs its own StructuredOutput
- **Mechanism:** on `claude -p` the StructuredOutput tool validates against
  `--json-schema` server-side and the model retries until it complies. Asked in prose
  to keep a `finding` under 400 characters, the same Opus sub-agent returned 445/444/
  409/419, then 4 of 6 still over after being told the exact lengths, then 1 still over
  after being told to aim for 380. Four round-trips for a formatting constraint.
- **Pilot:** `doit validate <role> <file>` (new) — the sub-agent writes its object to
  `$R/seat/<spawn>.output.json` and runs the validator until it prints VALID, then
  returns. That file is outside the repository, so the porcelain check is unaffected.
- **Systemic:** the seat prompt template must carry that loop for every role, and
  `run_seat` should accept the bare `<spawn>.output.json` directly (wrapping the
  envelope itself) instead of waiting for a hand-written `result.json`.
- **Fix status (2026-09-15):** **shipped 2026-09-14** (`9c7ecdb`, `doit validate`); the "every role" half shipped 0.2.0 as the *Seat route* section in every dispatchable contract.

### S11. The whole-tree "repo status unchanged" check voids spawns on a repo that has crons
- **Mechanism:** the baseline's finding 2, unfixed: `porcelain()` compares `git status
  --porcelain` over the whole tree before and after a non-builder spawn. Albert Scott's
  repo has a liveness sweep (`scripts/watcher_sweep_liveness.sh`) and a session-end hook
  that rewrite `docs/sessions/*` on their own schedule. One rewrote
  `process-health.md` under the 15-minute cut audit; the audit was recorded as
  `spawn-failed` and its findings never landed.
- **Pilot:** `DOIT_REPO_VOLATILE="docs/sessions/*"` — an operator-declared glob list the
  check ignores; `porcelain` now lists untracked files one per line so a glob can name
  them. The audit was re-dispatched as `L-plan-auditor-0002` and fed the *same*
  auditor's on-disk Output (its reads were of files under `scripts/ci` and `tests/`,
  which the sweep did not touch); the ledger shows both spawns and the reason.
- **Systemic:** the check should compare against the spawn's *grant* (the paths it may
  read and must not write), or hash the granted tree, rather than the whole status.
  Declaring volatility is a stopgap that has to be repeated per project.
- **Fix status (2026-09-15):** **stopgap shipped 2026-09-14** (`9c7ecdb`, `DOIT_REPO_VOLATILE`; verified: L-plan-auditor-0002 ran clean under it). Grant-scoped porcelain is open, a-10.

### S12. There is no path to file a goal, so an adopted project stays at `goal: null`
- **Mechanism:** D98 makes `Covers:` the governor's checkable arrow, and `think.py`
  reads `goal-filed` events — but nothing writes one. `doit alloc` has no `goal` kind
  in its documentation, `thinker.md` has no goal shape, and the board never says
  "no goal on file". The operator's actual goals here — *the profitability platform
  operating* and *everything Yitzy sent over operating* — are goal-shaped (dated,
  several charters each), and the pilot charter cites `Covers: none` because there is
  nowhere to point.
- **Pilot:** none; the charter runs under §12.2's `goal: null`.
- **Systemic:** a Thinker shape that produces a goal file (title, date, `G<n>`
  requirement ids, out-of-scope seeds) and a `--land-goal` that appends `goal-filed`;
  a HEALTH line when charters exist and no goal does.
- **Fix status (2026-09-15):** **partly closed on the day**: two `goal-filed` events exist on this ledger, so a filing path was built into `think.py` during the pilot. The HEALTH line and the goal shape's date wording are open, b-8.

### S13. "The file exists" is not "the contract finished"
- **Mechanism:** the first seat route read `<spawn>.output.json` as soon as it
  appeared. A contract iterating with `doit validate` writes an invalid draft first;
  the wrapper read the draft, recorded `spawn-failed` (schema), and the validated
  version landed a second later. The spec file was on disk and correct; the ledger
  said the spawn failed.
- **Pilot:** completion is the pane's `<spawn>.meta.json` stamp (written after the
  sub-agent returns), never the Output's existence. The spec-writer was re-dispatched
  as `L-spec-writer-0002` with the same, validated Output; both spawns are on the
  ledger. Note the `l1-complete` was appended before `spec-written` landed, because
  the pane believed the first wrapper had succeeded — the record is honest about the
  order and the fold does not care, but it is a scar.
- **Systemic:** every handoff between a driver and a wrapper needs an explicit
  "done" token distinct from the artifact — the same shape as §9.2's "content first,
  event second".
- **Fix status (2026-09-15):** **shipped 2026-09-14** (`c1ceab5`); verified on L-spec-writer-0002 and every seat spawn after it.

### S14. The builder's per-criterion rows have no durable home past the card's fifteen lines
- **Mechanism:** `write_card` renders at most fifteen lines (§5.10) and `p_grader`
  reads the criterion rows back from that render. Twelve criteria → six rows reach the
  grader; the other six are claims it cannot see and therefore cannot test. The
  builder's Output object itself is not in the ledger (`spawn-done` keeps scalars
  only). The baseline saw this on the first chain ("the card had rows for AC1–AC6
  only", filed as `card-quality`) and it recurred here unchanged.
- **Pilot:** `content/L-card-0001.json` — the full card object, with
  `deviations[].why` stripped (the builder's reasoning, the one thing the judge must
  never read) — handed to the grader beside the packet. The wrapper now writes that
  sidecar on every build and the grader packet reads every row from it.
- **Systemic:** the render is the operator's summary; the object is the record. Every
  role whose Output carries arrays the next role must test (builder `acs`, reviewer
  `blocking`/`reverify`, charter-reviewer `unrolled_not_built`) needs the same
  sidecar, or the arrays go into the ledger as their own events.
- **Fix status (2026-09-15):** **builder half shipped 2026-09-14** (`8d2b325`, `L-card-NNNN.json`; verified: L-grader-0001 read all 12 rows). Reviewer / charter-reviewer sidecars open, a-13.

### S15. An owed criterion has no route to `shipped-owed-evidence`, and K=0 means it could not ride anyway
- **Mechanism:** `fold.spec_state` derives `accepted` from a `confirmed` verdict plus a
  review, and `shipped-owed-evidence` from an `owed-ac` event whose `wake_at` is in
  the future. But `owed-ac` is a *declaration* (spec-writer, builder) and the
  declaration schema is `{term, line}` — no `wake_at` can be written, so the D25 state
  is unreachable from any real spawn. A spec with one honestly-owed criterion (here
  AC12: the post-merge check-run) grades `cannot-assess` on that row, the wrapper
  writes `confirmed: false`, and after the merge the spec reads plain `shipped` —
  the same rendering as "nothing has judged it". Separately, `DOIT_K` defaults to 0,
  so a charter cannot close over any owed evidence until the operator sets K.
- **Pilot:** merged on an Executor `decision` event that names why (all builder-
  provable rows met, review clean, the one `cannot-assess` is owed by construction).
  After the deploy, the grader is re-dispatched with the live check-run reachable, so
  the re-grade can `confirm` and `accepted` derives. `doit deploy` cannot run before
  CI is green, so the window in which the spec reads `shipped`-and-nothing-more is
  real and is the ledger's honest state.
- **Systemic:** `owed-ac` needs `wake_at` (and the observation that will prove it) as
  required fields — the spec-writer's contract already says "the observation and the
  `wake_at` that will prove it" — and the grader's `cannot-assess` on an owed row
  should not zero `confirmed`; the fold should read *confirmed over the evaluable
  rows* + *owed rows with a future wake_at* as `shipped-owed-evidence`. K must be a
  measured setting, not an unset default that silently forbids the whole state.
- **Fix status (2026-09-15):** **half shipped** (charter 3, unreleased): the spec-writer's `owed-ac` declaration now requires `criterion` + `wake_at`, so the event the fold reads can exist; L-spec-0006's AC7 is the first to ride it. Still open (a-6): `cannot-assess` on an owed row zeroes `confirmed`, `owed-met`, `closed-shipped`.

### S16. The gate's grant parser and the spec-writer's `Writes:` shape disagree
- **Mechanism:** D115 makes `writes_grant` read the `Writes:` LINE and refuse prose
  ("`'(the'` is not a path or a glob"). The spec-writer wrote
  `**Writes:** (the merge gate's grant — the slot's footprint, verbatim)` and the seven
  paths in a fenced block beneath — a shape the contract's own words invite. The first
  gate run on the second real merge was `rework — could not determine`.
- **Pilot:** the parser now takes a list (bullets or a fenced block) under a
  parenthetical-only `Writes:` line, up to the first blank line or heading; a
  sentence there is still refused (tests added). Gate re-run: `clean`.
- **Systemic:** the spec-writer contract should state the grant's exact shape (paths
  on the `Writes:` line, or one per line under it, nothing else), and the spec-auditor
  should check it — a merge gate refusing at merge time is the most expensive place to
  learn the shape.
- **Fix status (2026-09-15):** **parser shipped 2026-09-14** (`ee9efb2`; verified: the gate re-ran `clean`). The contract statement of the `Writes:` shape is open, b-4.

### S17. A "known trap" that comes true has no lane row
- **Mechanism:** the charter and spec both said more pre-existing reds probably sit
  behind the fail-fast plugin and are out of scope. One did. The Executor contract has
  rows for a failed deploy (revert) and for a gate that could not determine, but none
  for "the deploy gate refuses for a reason outside this charter": the spec is
  `shipped`, not accepted, blocked on an external; the charter is `L1-complete` with
  one owed AC that can never be proven until *another* charter lands.
- **Pilot:** a `blocked` record with `owner=executor` (renders under BLOCKED with an
  owner, not as a wedge) and an adjacent `brief` on the charter carrying the fact and
  the footprint hint, so the Thinker's triage (§7.9) can cluster it — it belongs to
  Goal A's "master deployable" charter, which the parallel Thinker pane is writing now.
- **Systemic:** `deploy.py` should distinguish *gate refused before touching the
  target* (exit 2, `deploy-refused{why}`, no rollback line) from *target changed and
  the check never named the sha* (exit 1, `deploy-failed`, rollback); and the Executor
  contract needs the row: refused for an external → `blocked` + `brief`, never revert.
  This is S8 measured, not predicted.
- **Fix status (2026-09-15):** **open** — a-4 for the script, b-9 for the Executor row. Applied by hand on the day (a `blocked` + `brief`).

### Operator rulings taken at the end of charter 1 (2026-09-14 ~07:10 UTC)
- **`DOIT_K=1`** — a charter may close over one owed criterion (the post-merge
  observation pattern) and no more. Recorded as a `decision` on `pilot-1447` with
  `ref=DOIT_K`; set in `~/.do-it/env.sh`, which every pane that runs `doit` against
  this root must `source` (it also carries `DOIT_SEAT=1`, `DOIT_NO_POKE=1`,
  `DOIT_PROJECT`, `DOIT_REPO_VOLATILE`). Note S15 still stands: with the schema as it
  is, an owed criterion cannot reach `shipped-owed-evidence`, so K=1 has nothing to
  count until `owed-ac` carries `wake_at`; the board reads `0 owed (K=1)` for a charter
  that in truth owes one.
- **Charter 2 is the deploy charter** ("master is deployable": the
  `test_deploy_activation_assertion` red, the deploy behind the pre-deploy gate, the
  `/version` sha check), first under Goal A, ahead of the money correctives. It runs
  through v2 from a fresh pane the same way charter 1 did. When its deploy lands, the
  grader is re-dispatched on `L-spec-0001` so AC12 confirms and charter 1 closes.
- **The hand-off between panes is the ledger, not a message.** The Thinker lands the
  charter (`doit think --land … --print-only` → `charter-filed`); the Planner pane sees
  it on the board and cuts it. The brief filed on `L-charter-0001` (the exact red) is
  on the same board for the Thinker to cite.

### Smaller, all real
*Fix status (2026-09-15): `--packet ""` is refused before allocation (charter 3, unreleased — it fired again there first); the rest are open — `pilot-retro-change-list.md` a-16, one line and one test each.*

- `doit dispatch --packet ""` reads `.` as the packet and crashes with a traceback
  before allocating a spawn; it should refuse an empty or non-file packet path.
- A worktree cut from this repository has no `.venv`; the spec-auditor caught it and
  the reworked spec uses the repository's absolute interpreter. Linking the venv in is
  wrong: `.venv/` in `.gitignore` is directory-shaped, so a symlink shows as untracked
  residue and fails the done-condition.
- `install.sh` creates `events/` and `content/` but not `repos/` (baseline finding 1,
  still true); `ln -s` was the operator's line again.
- `doit append` re-renders the whole board to stdout on every append; in a pane that
  is ~25 lines of noise per event. It should print the event line and nothing else
  unless asked.
- `doit up` allocates the Planner's ledger file even under `--print-only`; harmless,
  but a dry run that allocates is not a dry run.

## Run log (list-price `cost_usd` is null on every seat spawn — read as unmeasured)

| # | Role | Backend | Model | Outcome |
|---|---|---|---|---|
| — | thinker (pane) | seat | this pane (Fable) | `L-charter-0001` written from v4 spec 1447; `doit think --land` accepted it first try; `Covers: none` → no charter-set audit |
| — | planner (pane) | seat | this pane | cut: 1 unit, 1 wave, footprint = the five files 1447 names; `doit audit cut` clean on five checks, `undetermined` on the acquisition trail (correct at stage cut) |
| 1 | plan-auditor (cut) · `L-plan-auditor-0001` | seat | opus | audit ran (11 turns, ~124 s, 82k sub-agent tokens): 6 findings, `bad_cut: false`, `charter-gap` + `worked`. Output over `maxLength` four times (S9/S10). **Spawn voided by the wrapper**: a cron rewrote `docs/sessions/process-health.md` during the run (S11) |
| 2 | plan-auditor (cut) · `L-plan-auditor-0002` | seat | opus | re-dispatch, same packet, under `DOIT_REPO_VOLATILE`; fed spawn 1's validated on-disk Output — 6 `audit-finding` + 2 declarations + `spawn-done` landed |
| — | planner (pane) | seat | this pane | acted on all six: footprint +`handover_validate.py`; `L-adr-0001` (fixture-repo rule); Plan with SD1–SD4 (SD2 = lazy referent at the entry point, the auditor's missed extract) and a line per finding; `plan-written`; stage-plan pre-pass clean on all six checks including the acquisition trail |
| 3 | plan-auditor (plan) · `L-plan-auditor-0003` | seat | opus | 17 tool uses, ~224 s, 97k tokens; **validated its own Output first try** with `doit validate`. 7 findings, `charter-gap` + `worked`. Found a same-defect sibling test outside the footprint (`tests/test_903_r2_kit_handover_guards.py`, also red, not baselined), that approach (a) therefore cannot reach the done-condition, and that the Plan's SD2/SD3 contradicted two in-repo contract statements |
| — | planner (pane) | seat | this pane | acted on all seven (no re-audit — one round per stage): SD2 fixed to a call shape and named as approach (b); SD3 pins observed behaviour and defers FAIL-vs-WARN to Q4; SD4 rules (a) out; SD5 brings the sibling in (footprint widened, `charter-gap` expected); SD6 an owed observed-data AC on the check-run; Q1 stands; finding 7 (deferral) is the operator's ruling |
| 4 | spec-writer · `L-spec-writer-0001` | seat | opus | 30 tool uses, ~371 s, 117k tokens: 397-line spec, 11 ACs (`backend`, `observed-data`), 1 owed, 1 unknown, `charter-gap` + `owed-ac` declared, no escalations. **Recorded `spawn-failed`**: the wrapper read its invalid first draft (S13) |
| 5 | spec-writer · `L-spec-writer-0002` | seat | opus | re-dispatch, same packet, same validated Output under the fixed completion signal — `spec-written`, `charter-gap`, `owed-ac`, `spawn-done` |
| — | planner (pane) | seat | this pane | `l1-complete` appended (one slot, written) — before `spec-written` landed, see S13 |
| — | executor (pane) | seat | this pane | first Executor action: `doit packet spec-auditor` built the packet from the ledger (pre-pass: 1 placeholder grep = the spec's one counted unknown; ids cited = charter ids exactly) |
| 6 | spec-auditor · `L-spec-auditor-0001` | seat | opus | 29 tool uses, ~416 s, 123k tokens; validated first try. 7 findings (every one with a runnable `confirms_with`), 9 rejected with the clearing observation, 2 advisory, `bad_cut: false`. Caught: no `.venv` in any worktree so every verify command would die; a review path reading a banner `_run_validate_spec` never prints; the OSError branch nobody pinned; a Verification chain that prints OK without the new tests existing; guessed `-k` filters for unnamed tests |
| — | executor (pane) | seat | this pane | `doit packet spec-writer --slot` built the rework packet: round one's slot + the 7-item fix list (S6: round one's packet lives under `content/`, so `--slot` is needed again on rework — the script's `prev` glob only sees `packets/`) |
| 7 | spec-writer (rework) · `L-spec-writer-0003` | seat | opus | 25 tool uses, ~329 s, 115k tokens; validated first try. All seven fixes applied in place: 12 ACs, 1 owed (AC12, the check-run), 1 unknown kept open (FAIL-vs-WARN, Q4), two weak dimensions named honestly |
| — | executor (pane) | seat | this pane | worktree cut clean from `master` @ `fc8c839ba` (branch `l-spec-0001`, under `$R/worktrees/`, on disk); `doit packet builder` built the packet and wrote `verify-L-spec-0001.sh` from the spec's Verification block (absolute interpreter, byte-identity diffs, positive existence greps for the three new tests). A `.venv` symlink I had added showed as untracked residue and was removed before the builder started |
| 8 | builder · `L-builder-0001` | seat | opus | 49 tool uses, ~455 s, 144k tokens; validated first try. `DONE`: one commit `fabe586` (2 files, +164/−6), verify exit 0 `VERIFY_OK` (65 passed), 12 rows (11 done with command evidence, AC12 owed), 2 minor deviations, tests added, 5 not-built with legal reasons, 2 unknowns, `worked`. Reported one further pre-existing red (`test_962_handover_rule5…[AC5-builder-worktree-0]`) as out of scope instead of chasing it |
| — | executor (pane) | seat | this pane | `doit packet grader` — the card render carried 6 of 12 rows (S14); full rows handed over as `L-card-0001.json`, reasons stripped |
| 9 | grader · `L-grader-0001` | seat | opus | 15 tool uses, ~188 s, 92k tokens; validated first try. Re-ran the checker and every row's `check`: 11 `met`, DONE-COND `met`, AC12 `cannot-assess` (owed, unevaluable from the packet — the honest third state), `matches_intent: yes`, `card_ok: yes`, `worked` + `evidence-gap`. The fold: `confirmed: false` because one row is not `met` → spec state `reviewing` |
| — | executor (pane) | seat | this pane | executor.md's row for "not confirmed, nothing standing, only cannot-assess": reviewer at `gates-only`, round 1. `doit packet reviewer` carried all 12 criteria with their review paths |
| 10 | reviewer (round 1, gates-only) · `L-reviewer-0001` | seat | opus | 15 tool uses, ~153 s, 87k tokens; validated first try. Drove every path at ready_sha: 0 blocking, 4 recommendations (docstring/behaviour conflict, CLI exit-code change unswept, guard style, AC10 wording), 11 reverify conditions, AC12 `unverifiable` (owed) |
| — | executor (pane) | seat | this pane | `decision` recorded (merge with AC12 owed — S15); `doit gate` refused once on the spec's `Writes:` shape (S16), `clean` after the parser fix; `git merge --no-ff` → `aa9817d96`; `shipped`; v4 ledger 1447 → shipped; pushed 06:25 UTC |
| — | CI on `aa9817d96` | — | — | four of five required checks green; **`pytest` FAILURE after 30 min: baseline gate `new=1 fixed=255 known=233`** — the one NEW red is `tests/test_deploy_activation_assertion.py::test_present_and_pathed_passes`, **not** test_903 (which left the NEW list: the fix holds on CI). Reproduced identically at pre-merge `0841a5a72`: pre-existing, from the 810 merges, hidden behind test_903 by the fail-fast plugin — the charter's own "known trap" |
| — | executor (pane) | seat | this pane | charter rule applied: blocked-external, no revert; `blocked` on the spec (`owner=executor`, `id=L-spec-0001-wait-ci`) and a `brief` on the charter (`blocked_me=true`, `hit_while=L-spec-0001`, footprint hint `tests/test_deploy_activation_assertion.py deploy.sh`). `doit deploy` deliberately NOT run against a gate known to refuse (S8). AC12 stays owed; the spec reads `shipped`, the charter `L1-complete`; the worktree stays until L2 |
| — | thinker (pane) · `L-thinker-0002` | seat | **Fable 5.1, a real pane** | opened by the operator's request as tmux window `flow:think-goals` (`claude -n think-goals --agent thinker --model claude-fable-5-1`, RETIRE list denied, ledger file preset), seeded with `content/think-goals-brief.md`: write the two goals (profitability platform operating; Yitzy's work operating) and their first charters, land with `--print-only` so the charter-set audit is the driver pane's to run via seat. Verified seat-billed: no `ANTHROPIC_API_KEY` in any environment, status line shows the seat windows |

## What v2 got right on this charter (so far)
- **Two blind audits found what the v4 spec missed.** 1447's `writes:` footprint omitted
  `handover_validate.py` and a sibling test file that is red from the identical defect;
  its two "permitted approaches" were both symptom-side of an eager resolve that
  contradicts the module's own docstring. Neither auditor had my reasoning; both read
  the code. Under v4 this would have been a builder discovering it mid-build and a
  rework round.
- **The pre-pass earned its keep.** `doit audit` said `undetermined` on the acquisition
  trail at stage cut and `none` at stage plan; both auditors treated the block as
  ground truth and spent their pass on semantics, as designed.
- **Every failure is an event.** The voided spawn, the re-dispatch, the schema retries
  are all in the ledger with reasons; nothing was edited.

---

## Thinker-pane record — `L-thinker-0002`, Fable 5.1, a different seat (started 2026-09-14 ~06:20 UTC)

*Written by the Thinker pane, not the driver pane above, and marked as such at the
operator's request: the first interactive Thinker session in the v2 shape is itself
part of the pilot. Same rule as above — each entry names the mechanism, what was done
here, and what the system should become. Numbered `T<n>` so they never collide with
the driver's `S<n>`.*

### T1. The goal's `date:` reads as an arbitrary deadline unless the reader is told it is the ordering key
- **Mechanism:** the brief said *"ask the operator — it arbitrates when things slip"*.
  The operator's first reaction: *"a date for each goal seems a little funny as a point
  of understanding priority."* He was right that it is priority — the design says so
  (§3.2 Goal row, D80: *"the date is read: it is what orders concurrent charters"*) —
  but neither the brief nor `thinker.md` says it, so the first thing the operator was
  asked for looked like ceremony.
- **Pilot:** dates ruled — Goal B (Yitzy) `2026-09-14`, Goal A (profitability)
  `2026-09-15`; B therefore orders ahead of A.
- **Systemic:** `thinker.md`'s goal shape (S12) should state *the date orders
  concurrent charters* in the line that asks for it, so the operator is asked for a
  priority, which he has, instead of a deadline, which he may not.
- **Fix status (2026-09-15):** **open** — b-8 (the goal shape's date line says it orders concurrent charters).

### T2. `doit alloc` with no kind crashes instead of printing usage
- **Mechanism:** `fold.py:603` reads `sys.argv[2]` unguarded → `IndexError` traceback.
- **Pilot:** harmless; `doit help` documents the form. **Patch only.**
- **Fix status (2026-09-15):** **open** — a-16 (with S19).

### T3. The record has no per-role convention, and the brief forbade the second role from writing to it
- **Mechanism:** `think-goals-brief.md` lists this file under *"read, do not edit"*;
  the operator then asked the Thinker to record into it *"through the lens of a
  different perspective and recorded as such."* Two panes, one record, no rule.
- **Pilot:** this section — appended, never interleaved, `T<n>` ids, the driver's
  text untouched.
- **Systemic:** if the pilot record is the systemic log, every pane role that runs
  during a pilot gets an appended section keyed by its ledger actor id. Same shape as
  the ledger itself: the filename is the actor.
- **Fix status (2026-09-15):** **adopted**: the retro pane's `R<n>` section below follows it; writing the convention down is b-12.

### T4. The source material described Yitzy's stream from a v4 lens, and undersold what already flows
- **Mechanism:** handover §4 frames "waiting on us" as *two open PRs, untriaged, no
  ledger record*. Measured (`gh pr list --author yitzchak-eg`): **30 PRs**, 28 already
  ingested through the human-only `ingest_inbound_spec.sh` and closed as couriers;
  of the 13 September ingests (1424–1436) **12 are `merged`, 1 `held`** — and every
  merged one is undeployed behind 1447, exactly like the profit fixes. The intake pipe
  exists and works; the far end (deploy) is the same choke point Goal A has.
- **Pilot:** Goal B is therefore written around *delivered to prod*, not *PRs
  ingested*; ingest of #270/#262 stays human-only (spec 552) and is an operator
  action the goal names, never a charter's.
- **Systemic:** a handover written from a stamped-ledger world reports queue length;
  a goal in v2 needs delivered-to-prod. The Thinker should re-measure "waiting on us"
  from the far end before writing a goal, and the brief should say so.
- **Fix status (2026-09-15):** **open** — b-8 (the Thinker re-measures "waiting on us" from the far end).

### T5. A charter that predates its goal can never cite it (`Covers:` is write-once)
- **Mechanism:** `L-charter-0001` landed `Covers: none` under `goal: null` (§12.2);
  D98 says `Covers:` is written as the charter is written and never reconciled after.
  Goal A's first requirement is necessarily "a green master sha can deploy" — the
  thing charter 0001 delivers — so the coverage diff will report that requirement as
  *no charter cites* for as long as 0001 lives, or Goal A must omit its own
  precondition.
- **Pilot:** Goal A will name it as a requirement and the charter-set write-up will
  state that 0001 covers it by construction (the brief's own instruction: "a Goal A
  charter set may cite it as existing coverage").
- **Systemic:** every adopted project hits this on its first goal. The design should
  say whether an operator-only `correction` event may attach `Covers:` to an
  already-filed charter, or whether a goal may list `Delivered by: L-charter-NNNN`
  against a requirement so the diff reads it. Either is a one-line rule; today
  there is none and the diff will be wrong forever on this project.
- **Fix status (2026-09-15):** **open** — b-8 (`Delivered by:` on a goal requirement).

### T6. "How relevant is this still?" cannot be answered against a prod that is 60 specs behind master
- **Mechanism:** the operator's second reaction to Goal A: the named defects are old
  spec numbers (255 is June) and the most user-relevant block is the Instinct
  user-imitation audit; he wanted relevance settled *before* the goal was handed over.
  Measured: the four money correctives are dated 09-06…09-11 (fresh, live-measured;
  only the spec numbers are old); the Instinct set is 24/26 shipped with **9 of 25
  verdicts REJECTED** and 598 files in the v4 corrective inbox. But prod is at
  `1afd6273c` (09-12) with master 60 merged specs ahead, so any "is it still live?"
  probe today measures a sha that is about to be replaced.
- **Pilot:** Goal A makes re-measurement *after* the deploy a requirement (each named
  defect is re-probed on the deployed sha; still-live → owning charter, dead →
  recorded as already-fixed), instead of triaging the inbox now.
- **Systemic:** a goal for an adopted project with a deploy backlog should carry a
  "re-measure on the new sha" requirement as a standing pattern; D96's probe is the
  Planner's tool for it, and the Thinker brief should say that relevance of inherited
  defects is a probe, not a reading.
- **Fix status (2026-09-15):** **open** — b-8 (re-measure-on-the-new-sha as a standing requirement).

### T7. The multi-dev ingest port exists in the design and not on this box
- **Mechanism:** §4.8/D13–D19 answer the operator's "how does Yitzy's stuff enter v2?"
  precisely: another developer's spec is a **free-standing spec** (`charter: null`,
  D15), author-prefixed id (D16), T2 review tier when it touches prod/data/money
  (D19), submitted into *our* Executor only (D17); never a charter, never a brief,
  optionally attached to a charter later. On this box the only ingest is v4's
  `ingest_inbound_spec.sh` (human-only, spec 552), which allocates a v4 number and
  writes the v4 ledger — no `spec-written` event with `charter: null` is appended
  anywhere, so v2's Executor cannot see an ingested spec.
- **Pilot:** Goal B states the going-forward flow in the design's terms and keeps
  ingest human-only; the bridge (v4 ingest → v2 `spec-written`) is a DO-IT change and
  lives here, not in a charter.
- **Systemic:** the ingest port needs a driver-side command (`doit ingest <pr>
  --confirm`?) that performs the human-gated fetch and appends `spec-written`
  `charter: null` `author: <prefix>` — the same shape as v4's script, pointed at the
  ledger. Also: Vercel marks every docs-only inbound PR red because the commit
  author's GitHub login is not linked to his Vercel member account — a project-side
  fact, but the same class as S11: an external signal the operator has to read past.
- **Fix status (2026-09-15):** **open** — a-15 (`doit ingest <pr>` with a `decision` event as the gate).

### T8. Goal requirement ids are not namespaced, and `--land` without `--goal` picks the newest goal
- **Mechanism:** `Covers:` carries bare ids (`G2`); `think.py:goal_path` with no
  `--goal` takes *the newest `goal-filed` event*. This ledger now has two goals filed
  seconds apart (L-goal-0001 profitability, L-goal-0002 Yitzy), both with a `G1`…`G5`.
  A charter landed without `--goal` is diffed against Goal B whatever it meant, and a
  `Covers: G2` cannot say which goal's G2 it delivers. The brief's instruction to
  always pass `--goal` is the only thing keeping the diff honest.
- **Pilot:** every land call here passes `--goal` explicitly; the charter-set write-up
  states the goal per charter.
- **Systemic:** either the goal's requirement ids carry the goal's own id
  (`L-goal-0001/G2`, or the design's intended author-prefix form), or `Covers:` takes a
  `goal:` field and `--land` refuses to diff without one when more than one goal is
  on the ledger. Multi-goal is the normal state of a real project; the single-goal
  assumption is the pilot's, not the design's.
- **Fix status (2026-09-15):** **open** — a-9 (`--land` refuses without `--goal` when >1 goal; namespaced ids).

### T9. Two goals can be delivered by one charter, and the diff cannot say so
- **Mechanism:** L-goal-0002 G1 (Yitzy's twelve merged specs live on prod) is delivered
  by the same deploy that delivers L-goal-0001 G2. A charter cites one goal
  (`--goal` is singular); the other goal's requirement will read as *no charter cites*
  forever, exactly like T5.
- **Pilot:** L-goal-0002 says "delivered by the same deploy as L-goal-0001 G2; no
  separate charter" in the requirement's own text, and the write-up repeats it.
- **Systemic:** same fix as T5/T8 — a `Delivered by:` line on a goal requirement that
  the diff reads, so cross-goal and pre-goal delivery are both first-class instead of
  permanent false findings.
- **Fix status (2026-09-15):** **open** — b-8 (with T5).

### T10. The charter-set is written against a board that moves under it, and that was fine
- **Mechanism:** while L-charter-0002 (the backlog deploy) was being written, the Executor
  merged `L-spec-0001` and its deploy-on-green was refused by a *second* pre-existing red
  (`test_deploy_activation_assertion::test_present_and_pathed_passes`, from the 810 r3
  merges) — exactly the "known trap" L-charter-0001 named. Goal A's G1 was therefore not
  delivered by 0001 alone.
- **Pilot:** the Thinker re-read the board before landing, wrote L-charter-0004 for the
  new red under G1, and landed it in the same session. The `blocked` event's `why` was
  precise enough to write the charter from without reading the CI log.
- **Systemic:** nothing to change — the ledger did its job. Recorded because it is the
  first time a Thinker session consumed an Executor event mid-session and the shape held.
- **Fix status (2026-09-15):** **nothing to change** (c) — the record says so.

### Thinker session summary (L-thinker-0002, closed 2026-09-14 ~07:10 UTC)
- Filed: `L-goal-0001` (profitability, 2026-09-15, G1–G5) · `L-goal-0002` (Yitzy, 2026-09-14, G1–G6).
- Landed with `--print-only`: `L-charter-0002` (backlog deploy, A/G2) · `L-charter-0003`
  (foreign-owned lock, B/G5) · `L-charter-0004` (activation-assertion red, A/G1).
- Deliberately not charters, on the goals as operator actions: ingest of PRs #270/#262
  (B/G2), the 1178 authorization (A/G4), Yitzy linking his GitHub login to his Vercel
  member account (B/G4).
- Expected false findings in the charter-set diffs, all explained above: A/G3–G5 (charters
  follow the deploy by design, T6), B/G1 (delivered by A's deploy, T9), B/G2/G4/G6
  (operator or Yitzy actions, T7).

### T11. The charter-set packet is per-`--land` call, not per goal
- **Mechanism:** landing L-charter-0002 wrote `charter-set-L-goal-0001.md` with
  *uncited: G1, G3, G4, G5*; landing L-charter-0004 in a later call **overwrote** it with
  *uncited: G2, G3, G4, G5*. Each call diffs only the charters passed to it, so a goal
  whose charters land across calls never has one packet that shows its true coverage,
  and the audit dispatched from the last line sees one charter, not the set.
- **Pilot:** the driver should run the Goal A audit knowing the packet under-reports;
  the real set is 0001 (by construction), 0002, 0004.
- **Systemic:** `think.land` should build the set from every `charter-filed` event whose
  charter cites the goal (plus the ones passed now), not from `argv`. The design's word
  is *charter set*; the code's is *this call*.
- **Fix status (2026-09-15):** **open** — a-9 (with S20).

### T12. An `open` charter is invisible on the board
- **Mechanism:** `doit states` shows L-charter-0002/0003/0004 as `open`; `doit` (the
  board) lists none of them — `WRITTEN, NOT PICKED UP` is a spec heading, and no heading
  shows a charter that has been filed but not cut. The driver pane asked the operator
  for a ruling on "the deploy charter" without knowing three had landed.
- **Pilot:** the operator carries the fact between panes by hand.
- **Systemic:** the board needs a `CHARTERS OPEN (n)` line (charter-filed, no
  `plan-written`), the mirror of `CHARTER CLOSE`. A Planner that "does not wait to be
  asked" (§3.5) has to be able to see what it should pick up.
- **Fix status (2026-09-15):** **open** — a-9 (`CHARTERS OPEN (n)` on the board).

## Driver pane, second sitting — charters 0004 and 0002 (started 2026-09-14 ~07:11 UTC, a fresh pane)

### S18. A contract's `tools:` line now binds the Agent-tool type — and strips the seat route's output loop with it
- **Mechanism:** once the harness loaded the contracts as agent types (S4's later
  measurement), a `plan-auditor` spawn gets exactly `Read, Glob, Grep, StructuredOutput`.
  The seat route's completion loop — write `<spawn>.output.json`, run `doit validate` —
  needs Write and Bash. Both cut-audit and charter-set-audit spawns returned "I cannot
  write the file" with the Output object in the final message.
- **Pilot:** the pane transcribes the returned object verbatim into
  `seat/<spawn>.output.json`, runs `doit validate`, stamps `meta.json`. The transcription
  is a hand between the model and the ledger; over-long strings (401 chars) were trimmed
  by the pane, which is exactly the D120 per-word safeguard failing at the wrong layer.
- **Systemic:** the seat backend needs a `StructuredOutput`-equivalent the sub-agent can
  emit without Write — the harness's own StructuredOutput tool IS listed on every
  contract, so the wrapper should accept the Agent tool's structured return as the
  Output (D116's `structured_output` field by another route), and the pane should never
  type a finding.
- **Fix status (2026-09-15):** **shipped 0.2.0**: `Write`+`Bash` on `plan-auditor`, `spec-auditor`, `grader`, `reviewer`, `charter-reviewer`, and a *Seat route* section in every dispatchable contract (the sub-agent writes `seat/<spawn>.output.json` and runs `doit validate` itself). **Verified on charter 3** (17 spawns served as general-purpose + contract, R2; every Output written and validated by the sub-agent itself; the pane typed nothing). On the codex backend the loop does not exist at all (`--output-schema -o`).

### S19. `doit alloc` treats any argument as a kind — `doit alloc -h` allocated `L--h-0001.md`
- **Mechanism:** `alloc <kind>` builds `L-<kind>-NNNN` with no validation of the kind
  token (T2 is the no-argument crash; this is its sibling).
- **Pilot:** the junk content file was deleted by hand (no event references it).
- **Systemic:** validate `kind` against the id kinds §2.8 names, and honour `-h`.
- **Fix status (2026-09-15):** **open** — a-16.

### S20. The charter-set packet was rebuilt by importing `think.py` — the fix T11 asks for, done by hand
- **Mechanism:** T11 — each `--land` call overwrites `charter-set-<goal>.md` with only
  the charters of that call. Goal A's packet showed 0004 alone.
- **Pilot:** the driver imported `think.check`, `think.coverage`, `think.charter_set_packet`
  from `~/do-it-v2/src` and rebuilt the packet from 0001 + 0002 + 0004 without appending
  any event, then dispatched `plan-auditor L-goal-0001 --seat` on it. The audit returned
  8 findings (G3–G5 unowned; G1 spans an uncited charter and a citing one; L-charter-0002's
  Intent names L-charter-0001 as the precondition when the live one is 0004; the Vercel
  bundle sha is unproven by R6).
- **Systemic:** T11's fix — `land` builds the set from every `charter-filed` event citing
  the goal — plus a `doit think --audit-set <goal>` that rebuilds and dispatches without
  landing anything.
- **Fix status (2026-09-15):** **open** — a-9 (`land` builds the set from every `charter-filed` citing the goal; `doit think --audit-set`).

### S21. Declarations land as events typed by their term, and nothing reads them back by that name
- **Mechanism:** a spawn's `declarations` land as one event per term (`charter-gap`,
  `worked`, `seam-undefined` …), not under a `declaration` type. A reader that greps for
  the word "declaration" finds none; the pane's hand-built plan-stage packet carried the
  nine findings and no declaration lines because it looked for the wrong type.
- **Pilot:** the plan audit ran without the cut-audit's three declaration lines.
- **Systemic:** minor; the packet builders (S6) should read the term-typed events, and
  `dispatch.py` should document the shape once.
- **Fix status (2026-09-15):** **open** — a-14.

### T13. "Human-only" was read as "human keystrokes", and the operator corrected it
- **Mechanism:** the v4 handover says *"Never run `ingest_inbound_spec.sh` yourself. Surface
  these and wait for the operator."* Spec 552's actual gate is `--confirm` (or a TTY
  confirm) — a human *decision*, which the operator gave in this session and which was
  appended to the ledger before the run. The Thinker first told the operator to run
  two shell commands himself; he pushed back, correctly, and the Thinker ran them
  (#270 → 1448, #262 → 1449). v4's own role table lists the ingest script under
  `think`.
- **Pilot:** the confirmation lives as a `decision` event one line above the `DONE`
  event on the same subject — the ledger is the human-in-the-loop record.
- **Systemic:** when the ingest port exists in v2 (T7), its human gate should be *a
  decision event on the ledger*, checked by the command, not a flag on a shell line.
  That is what makes it auditable and what lets any pane run it once the decision
  exists.
- **Fix status (2026-09-15):** **open** — a-15 (with T7).

### T14. The driver pane cannot be told anything except by typing into it
- **Mechanism:** the ruling on the deploy was appended as a `decision` on
  L-charter-0002; the driver was mid-spawn (a plan-auditor running) and its board has
  no heading for open charters (T12), so the Thinker also `tmux send-keys` a pointer
  into its prompt. Two panes on one ledger with no notification path except the fold.
- **Systemic:** `DECIDED WITHOUT YOU` already exists for decisions; a `decision` whose
  subject is a charter another pane is driving should surface there for that pane on
  its next fold. Verify whether it does; the pilot could not wait to see.
- **Fix status (2026-09-15):** **open** — a-17: verify first, fix only if it fails.

### S22. A charter's Intent can be false about production and nothing in the pipeline checks it
- **Mechanism:** L-charter-0002's Intent — "Production serves `1afd6273c` while master is sixty
  merged specs ahead" — reads `/version`, which only `deploy.sh` writes. Two GitHub workflows
  (`deploy-api.yml`, `deploy-pipelines.yml`, since April) rsync `api/`, `pipelines/`, `agents/`
  to the droplet and restart the API on every master push. Measured 2026-09-14: 736 + 659 files
  on the droplet are newer than the Sep 12 deploy; the 1439 merge pushed under R3 was live on
  prod 18 seconds later. The Thinker, the charter-set audit, the Planner and the operator's
  ruling all reasoned from `/version`.
- **Pilot:** found by accident — `gh run list` showed "Deploy API · success" on the R3 push.
  Recorded as a `brief` on L-charter-0002 and in the deploy record; the charter's R5/R6 still
  hold, but its Intent, Goal A's "sixty specs undeployed", and the rollback story (the next
  push re-syncs code forward after a rollback) all need the correction.
- **Systemic:** a charter that names a production state should carry a probe (D96) even when
  the "external" is our own box: `/version` is a claim, `find -newer` is a measurement. The
  Thinker contract's five sections have no "measured, not read" requirement for the Intent's
  premise; the spec-auditor's `false-premise` category exists one level too late.
- **Fix status (2026-09-15):** **open** — b-6 (a charter Intent that names a production state carries a probe).

### S23. The spec's Verification block IS the checker, generated verbatim — and a `;` in it inverted the gate
- **Mechanism:** `packet.py::verify_script` turns the spec's fenced §9 block into
  `content/verify-<spec>.sh` (`set -euo pipefail` + the block) at every `doit packet builder`,
  overwriting whatever is there. The rework spec-writer put the spec-820 scanner into the chain
  "advisorily" with `;` before the final pytest: `;` discards the preceding list's status, so
  `VERIFY_OK` printed after a false link (`! grep -q '@ALBERT_SCOTT_PYTHONPATH@'` — the builder
  had written the token in a comment); and had the chain reached the scanner, its pre-existing
  red as the list's last command would have aborted the script under `set -e`. The builder
  reported "chain links all true"; the grader re-ran the checker and caught both.
- **Pilot:** the pane first hand-wrote a corrected script, which the packet builder silently
  overwrote (the pane did not know verify_script existed). The grade came back `card_ok: no`,
  AC3 unmet; the fix is a round-2 spec-writer rework of §9 (chain-only, scanner after it) plus a
  builder rework. The grader worked exactly as designed.
- **Systemic:** `verify_script` should LINT the block before writing it — `bash -n`; no bare
  `;`, `||` or newline-separated statement inside the gated chain; absolute interpreter — and
  refuse the packet on a violation. The Executor's hand-authored script was the wrong remedy
  and the generator is the right place.
- **Fix status (2026-09-15):** **open** — a-5 (`verify_script` lints the block; `BASE` pinned at build time).

### S24. A builder that saves evidence under `seat/<its own spawn id>…` poisons the grader packet
- **Mechanism:** the builder saved its AC10 droplet observation as
  `seat/L-builder-0002.droplet-activation-rows.txt` and cited the path in the card's evidence.
  `doit packet grader` strips the builder's spawn id by design (Blindness) and REFUSED the packet
  — correctly — but the refusal is printed on stdout where the packet path goes, so the shell
  passed the refusal text to `doit dispatch --packet` as a filename and got a traceback.
- **Pilot:** the pane moved the file to `content/L-spec-0002-droplet-activation-rows.txt`,
  rewrote the path in the card JSON/MD and the builder Output, and rebuilt the packet (clean).
- **Systemic:** two fixes — the builder contract should name where evidence files go
  (`content/<spec>-…`, never `seat/`), and `packet.py` should print a refusal to stderr and exit
  non-zero so a caller cannot mistake it for a path.
- **Fix status (2026-09-15):** **half true already**: `packet.die()` is `sys.exit(str)`, which writes to stderr with exit 1 — the REFUSED path that reached stdout needs a look (a-16). The builder contract naming `content/<spec>-…` for evidence is open, b-5.

### S25. A charter with no code unit has no lane path — the Executor ran it by hand
- **Mechanism:** L-charter-0002's footprint is "the deploy record, the merge of 1439, and the
  evidence captures" — no unit a builder could build, so no spec, no `spec-written`, no
  `l1-complete`, and the close row (sweep-fixpoint → charter-reviewer → reap) cannot fire. The
  operator's ruling assigned R1–R3 to the driver pane directly; R4–R8 are `doit deploy` and
  post-deploy observations the pane makes.
- **Pilot:** the pane recorded R1–R3 as an `evidence` event (an open type) pointing at
  `content/deploy-record-L-charter-0002.md`, and will append `deploy-landed` and the R5–R8
  observations the same way; `l1-complete` is the operator's to append (fold.EMITS allows it) so
  the charter-reviewer can read the deploy record against the done-condition.
- **Systemic:** the design needs an "operational charter" shape — units whose builder is the
  Executor (or the operator) and whose card is an evidence file — or a rule that such work is a
  goal's requirement delivered by a `decision` + `evidence` pair, not a charter.
- **Fix status (2026-09-15):** **fold half shipped 2026-09-14** (`dd8d993`, with S35). The operational-charter shape in the design is open, b-7.

## Run log — second sitting (2026-09-14 07:11 → , driver pane; every seat spawn `cost_usd` null)

| # | Role | Backend | Model | Outcome |
|---|---|---|---|---|
| 11 | plan-auditor (charter-set) · `L-plan-auditor-0004` | seat | opus | packet rebuilt from 0001+0002+0004 (S20); 4 turns, 48k tokens; **no Write/Bash in the typed spawn (S18)** — pane transcribed the object; 8 findings on L-goal-0001 (G3–G5 unowned, G1 split across an uncited and a citing charter, 0002's stale precondition, Vercel bundle sha unproven), `charter-gap` + `seam-undefined` + `worked` |
| 12 | plan-auditor (cut) · `L-plan-auditor-0005` | seat | opus | 11 turns, 112k; transcribed (S18), one finding trimmed to 400 chars by the pane; 9 findings, `bad_cut: false`; caught: R3's live-box clause unowned, the review_path would SKIP the five rows on the builder box, sig-join not name-join, R2's `mode:block` premise wrong (rows are `mode:warn`), four of five seed sources are deploy.sh's heredoc not a template |
| — | planner (pane) | seat | this pane | Plan SD1–SD7 + `L-adr-0002` (manifest-row ↔ fixture-seed parity); every finding a line; stage-plan pre-pass clean on all six checks |
| 13 | plan-auditor (plan) · `L-plan-auditor-0006` | seat | opus | 11 turns, 117k; returned the object in-message (S18); 6 findings: the test file is 1,437 lines (63 of headroom, not "1,370+"), SD4's observation ran unscoped (`ACTIVATION_BOX` unset unions the builder crontab), the review_path bullet is unreachable before the deploy, the verbatim transcript was time-bombed, sig-only join covers one of three failure modes, the (builder fixture, droplet box) pair unmodelled — all acted on (SD2 → real gate in a NEW file, SD4 re-measured scoped, SD8 file-size) |
| 14 | spec-writer · `L-spec-writer-0004` | seat | opus | 22 turns, 166k, ~9.5 min; 578-line spec, 12 ACs (7 backend / 5 observed-data), 2 owed, 1 unknown, 6 declarations; left a stray `.tmp` file (no Edit tool) that the pane removed |
| 15 | spec-auditor · `L-spec-auditor-0002` | seat | opus | 19 turns, 154k; returned in-message (S18), pane transcribed, all six findings over 400 chars trimmed; 5 findings (spec-820 forbids the absolute test-to-test import the spec mandated; `_manifest_row_names` unbounded regex over 86 same-shaped rows; AC11's wake_at (deploy.sh "never installs" the cron — later measured FALSE by the rework: `_run_cron_install_loop` does); P3 vacuous; ratchet arithmetic unmeasured), 14 rejected with clearing observations, 5 advisory |
| 16 | spec-writer (rework) · `L-spec-writer-0005` | seat | opus | 7 turns, 135k; all five fixes applied; corrected the auditor's installer premise with a measurement (A13) |
| — | executor (pane) | seat | this pane | `doit packet builder` regenerated `verify-L-spec-0002.sh` from §9 (S23 — the pane's hand-written script was overwritten unnoticed); worktree cut from `bd2a7bafb` |
| 17 | builder · `L-builder-0002` | seat | opus | 20 turns, 135k, ~12.5 min; one commit `019e87362` (+238: 52 seeds, 186-line parity file), VERIFY_OK; saved evidence under `seat/<own id>…` → grader packet REFUSED (S24), pane relocated it |
| 18 | grader · `L-grader-0002` | seat | opus | 9 turns, 77k; re-ran the checker: AC3 **unmet** (the builder wrote the forbidden placeholder token in a comment), and found the §9 chain's `;` masked the false link — `card_ok: no`, `card-quality` + `evidence-gap` + `worked` |
| 19 | spec-writer (rework 2) · `L-spec-writer-0006` | seat | opus | 3 turns, 100k; §9 rebuilt as one `&&` chain + `rc=$?` + advisory 820 line + `exit $rc`; AC3 wording comment-inclusive |
| 20 | builder (rework) · `L-builder-0003` | seat | opus | 21 turns, 113k; amended to `937c7adc6`, token count 0, checker `VERIFY_OK` + `ADVISORY spec-820 exit=1`; corrected the first card's "820 red on 7 files" (5) |
| 21 | grader (re-grade) · `L-grader-0003` | seat | opus | 14 turns, 81k; 10 met, AC11/AC12 cannot-assess (owed), done-condition satisfied, `matches_intent: yes`, `card_ok: yes` |
| 22 | reviewer · `L-reviewer-0003` (L-spec-0003) | seat | opus | 0 blocking; merged `b7b935c51`; pytest check-run on master GREEN for the first time in the pilot |
| — | executor (pane) · deploy attempt #1 | seat | this pane | `doit deploy` on `b7b935c51`: gate PASS, tree synced, `/version` stamped, migrations ABORTED at `listing_run_authority_v1`; `./deploy.sh --rollback` → `1afd6273c` healthy (S30) |
| 23 | charter-reviewer · `L-charter-reviewer-0002` (charter 1) | seat | opus | `complete`, 1 finding |
| 24 | charter-reviewer · `L-charter-reviewer-0003` (charter 4) | seat | opus | `complete`, 3 findings; two adjacent briefs filed by the pane |
| — | operator (pane, under ruling) · closes | — | — | `spec-closed` L-spec-0001, L-spec-0002 (built+merged, owed evidence never derives; S33) and ghost L-spec-0004 (S32); charters 1 and 4 fold **L2-complete**; `doit reap` removed `l-spec-0001/0002/0003` worktrees + branches, nothing retained |
| — | executor (pane) · deploy attempt #2 | — | — | ON HOLD: coordinator's candidate `34cf284` REJECTED in independent review (`GRANT USAGE ON SCHEMA extensions` silently no-ops on hosted Supabase → `gen_random_bytes` permission denied at first real capability grant); successor removes the `extensions` dependency |
| — | executor (pane) · revert | — | — | Chain `34cf28425→81992d6f3→93f85aeeb` ff-pushed after a clean prod role assert → required `pg-parity` + `spec 770` RED (role guard vs shared CI cluster) → operator "revert it" → `173e03743` (tree == `b7b935c51`); its `pytest` check red only on collection-parity `lost=4` (a revert of added tests reads as lost) |
| — | executor (pane) · deploy attempt #2 | — | — | Chain `a78d815e0…21962db5f` (strict role semantics + CI scratch cleanup) verified, ff-pushed, all required green (pytest queued 30 min, ran 45); `doit deploy` gate PASS block-mode, `deploy.sh --all` LANDED `21962db5f` 17:23:40Z, migrations `→ listing_membership_l1_v1`; wrapper could not mark landed (S34), stopped, landed recorded by a record-only `doit deploy --sha 21962db5f`; R5 PASS, R6 captured on 4 surfaces × 2 clients × 2 months |
| 25 | charter-reviewer · `L-charter-reviewer-0004` → re-served as `-0005` (charter 2) | seat | opus | 30 turns, 89k, 11.5 min; `complete`, depth full, 7 findings (untagged unmapped money figure on the ASIN surface; goya ASIN grain empty; request_health collector cron failing hourly against a prohibited head; R7 met by re-scoping two of four specs; R5 proves by /version alone — it md5-matched five files itself; browser leg undrivable without a staff account). First serve stamped `project: .do-it` (S35) |
| — | executor (pane) · post-charter deploy #3 (coordinator's default-privilege correction) | — | — | `62c04d46b` pushed → pytest red on ONE stale routed-head literal (their guard test) → successor `5cd5c47de` (one line) → all required green → `doit deploy --sha 5cd5c47de` (short sha, S34 workaround; deploy.sh stdout streamed to its own log) LANDED 19:52:51Z in 435 s; prod: one global owner-only fn default row, postgres defaults unchanged, born-closed proven in a rolled-back txn |
| — | executor (pane) | seat | this pane | R1–R3 of L-charter-0002 done by hand under the operator's ruling (rollback dry-run, lock absent, migration list + resolver target, 1439 merged `bd2a7bafb` and pushed; CI: one NEW red = the 0004 red); S22 discovered (push-deploy workflows); `doit gate l-spec-0002 master` clean pre-review |

### S26. One red per ~2-hour cycle: the fail-fast plugin turns "make master green" into a serial charter chain
- **Mechanism:** CI's `pytest_fail_fast_plugin` stops the run at the first NEW red, so each merge
  reveals exactly one more pre-existing red. Charter 1 cleared test_903 → revealed the activation
  red; charter 4 cleared that → revealed `test_pipeline_common::TestPipelineDb::test_start_and_complete_run`,
  which reproduces at the deployed sha of Sep 12 and cannot be baselined: it emits a `failed` AND
  an `error` for one node id, which `pytest_baseline_gate.py` reports as a COLLISION and reads as
  NEW forever. Both charters' R5 said "file the next one as a brief, do not chase" — correct per
  charter, but the deploy has now been refused three times by three different pre-existing reds.
- **Pilot:** the pane launched the full suite locally WITHOUT fail-fast (junit + the same baseline
  gate) to enumerate every NEW red in one pass, so the next charter is "master green" with the
  whole list as its Requirements, not "the next red".
- **Systemic:** a Planner probe (D96) for any charter whose done-condition is a CI check-run
  should be exactly that enumeration; and a fail-fast CI is the wrong instrument for a
  done-condition of "green whatever reds remain" (the charter-set audit's finding 3 said so).
- **Fix status (2026-09-15):** **dropped for v2** (c) — Albert Scott's CI plugin, filed on their side; the Planner-probe rule survives as b-11.

### S27. A local "enumerate every red" run is not CI: box state makes 503 false NEWs out of 1
- **Mechanism:** the pane ran the full suite on the builder box to list every NEW red at once.
  The gate reported `new=7150` (503 node ids): live-DB tests that skip on CI (no DSN) but run
  here (`.env` present) and hit the spec-909 denial; scanner tests polluted by a nested tree the
  listing session keeps inside the repo (`.qurlife-l3-work/`, git-ignored); box-tooling tests
  reading `~/.claude` state; collection-parity tests needing base refs. Signal-to-noise ≈ 0.
- **Pilot:** discarded. The workflow already has the right instrument — `python-tests.yml`
  `workflow_dispatch` with `full_run: true` drops the fail-fast flag on a clean runner — and the
  pane triggered it on master (`gh workflow run python-tests.yml --ref master -f full_run=true`).
  ~45 min of box CPU wasted; also noted `/tmp` (tmpfs, 7.7G) at 81% from six `/tmp/listing-*`
  worktrees of the parallel session — the OOM/tmpfs class CLAUDE.md warns about.
- **Systemic:** a probe whose external is "CI" must run ON CI; the Planner contract's D96 should
  say so, and `doit` could own the `gh workflow run … full_run=true` line as the canonical
  "enumerate reds" probe for this repository.
- **Fix status (2026-09-15):** **open** — b-11 (a probe whose external is CI runs on CI). The local run was discarded on the day.

### S28. The pane made its own S23 mistake: a `;` in a shell chain dispatched a spawn on a packet with no fix list
- **Mechanism:** transcribing the spec-audit Output (S18) failed (the JSONL transcript's JSON block
  was not where the regex looked); the pane's command line continued past the failure because a
  `;` separated the validate step from the packet+dispatch steps. `doit packet spec-writer` built
  a rework packet with an EMPTY fix list (no `audit-finding` events yet) and `doit dispatch --seat`
  appended `spawn-started` for `L-spec-writer-0009` before the pane noticed. The pane killed the
  waiting wrapper; the ledger now carries a `spawn-started` with no `spawn-done`/`spawn-failed`.
- **Pilot:** the Output was then transcribed correctly (parse the JSONL, find the text part),
  validated, stamped; 24 events landed; a correct rework packet dispatched as `L-spec-writer-0010`.
  The ghost `L-spec-writer-0009` is left as-is (an operator `correction` is the only clean fix).
- **Systemic:** `doit packet spec-writer` for a rework should REFUSE when the subject has a
  `spec-auditor` `spawn-done` but zero `audit-finding` events since the last `spec-written` — an
  empty fix list on a rework round is never right; and `dispatch --seat` should write
  `spawn-started` only when the pane stamps `meta.json`, or record `spawn-abandoned` on SIGTERM.
- **Fix status (2026-09-15):** the rework-packet refusal on zero findings **already exists** in `packet.py` (`a rework packet with no fix list is round one again`, test-covered); `spawn-abandoned` on SIGTERM is open, a-11. The transcription half is closed by S18's 0.2.0 fix.

### S29. Wall-clock: one small fix spec costs ~2 hours through v2, and the operator's verdict is "terrible"
- **Measured (second sitting, 07:11 → 13:10 UTC, ~6 h):** three merges to master, zero deploys.
  Per spawn: spec-writer 9–15 min (166k–194k sub-agent tokens), spec-auditor 9–11 min, builder
  12–21 min, grader 6 min, reviewer 6 min, plan-auditor 2–4 min. Per spec end to end (write →
  audit → rework → build → grade → review → merge): L-spec-0002 ≈ 2 h 10 min including one
  grade-bounce; L-spec-0003 ≈ 2 h and counting including one build-escalation. Per merge: +30
  min CI. Serial reds via fail-fast (S26): three merges where one enumeration would have done.
  Pane overhead: two transcription mistakes (S18/S28) ~15 min; the wrong local enumeration
  (S27) ~45 min; two operator pauses ~60 min.
- **Operator ruling (Ephraim, 2026-09-14 ~13:05 UTC): "that amount of time is terrible. You
  must file that in the list of observations about the system."** Filed.
- **Systemic:** (1) the blind audit rounds are the largest fixed cost and found real defects
  every time — keep them, but cap the spec at the size an auditor reads in 5 min (the 700-line
  L-spec-0003 is the problem, not the audit); (2) builder/grader/reviewer read the whole spec
  and the whole code path again each — the packet should carry the spec's AC table and the
  diff, not the spec; (3) a rework that changes one literal should not need a full spawn
  (D116's "the Executor never edits a spec" costs 10 min per literal — the Executor did it by
  hand here, under a recorded decision); (4) fail-fast CI plus one-red-per-charter is the
  worst possible pairing — the probe row (S27) fixes it; (5) seat transcription (S18) costs a
  human-shaped mistake per audit — the StructuredOutput return must be the Output.
- **Fix status (2026-09-15):** (1) spec ≤ 400 lines: open, b-3 · (2) packets carry the AC table + diff: open, a-7 · (3) literal reworks without a spawn: open, b-3 · (4) fail-fast pairing: b-11 · **(5) shipped 0.2.0** — the seat route no longer needs the pane to type anything.

### S30. The first real `doit deploy` failed at the target's migration step, and the script threw away the one line that says why
- **Mechanism:** `deploy.py` runs the handed-in command, keeps only a `TAIL` of its output in
  the `deploy-failed` event, and prints the same tail to stdout. The failing statement block was
  in the tail; the psycopg2 error line (the actual "why") was above it and is gone. The pane made
  it worse: a second launch (dropped by the flock, correctly) opened the same log file with `>`
  and truncated the first run's stdout. Net: a production deploy failed and nobody can quote the
  error. Also observed: `deploy.py`'s "rollback first — git revert -m 1 <sha>" advice is wrong
  for this target — the failure was the target (migration) not the build, `blocked-external` was
  appended correctly, and a `git revert` of a green merge would have re-redded master; the
  right rollback was the target's own (`./deploy.sh --rollback`), which the pane ran.
- **Pilot:** prod: healthy throughout (API restarted by deploy.sh, then by the rollback), tree
  synced then restored, `/version` `b7b935c51` for ~8 minutes then back to `1afd6273c`, DB
  untouched (transactional DDL). The rollback's own post-check failed on a POE parser-lineage
  marker (`POE_PARSER_CODE_REVISION` absent from the running service's environment vs the
  restored marker file) — a consistency assertion, not a functional break.
- **Systemic:** `deploy.py` must persist the FULL command output to `$R/logs/deploy-<spec>-<sha>-<n>.log`
  and name it in the event (the tail is for the board, not the record); it must refuse a second
  concurrent launch BEFORE opening any file the first holds; its failure advice must distinguish
  build-rollback (`git revert`) from target-rollback (the target's own command, handed in as a
  `--rollback` argument and RUN by the script when `--check` fails, §5.8) — S8's split, now
  measured on a real failure.
- **Fix status (2026-09-15):** **open** — a-4 (full log under `logs/`, refuse a second launch before opening files, target rollback as an argument).

### S31. The checker dies at merge, and the commit trailer leaks the builder model to the grader
- **Mechanism (a):** every `verify-<spec>.sh` link keys on `$(git merge-base HEAD master)`; after the
  merge that is `HEAD`, so every diff link is vacuous and the parity-gate link fails
  (`allowlisted_lost=0`). A post-merge re-grade cannot re-run the checker; the grader
  recomputed every property against `HEAD^` by hand and declared `gate-infra`.
- **Mechanism (b):** AC10's evidence is the commit message, and the commit message carries the
  project's mandatory `Co-Authored-By: <model>` trailer — the grader saw the builder's model
  through the artifact under grade and flagged it rather than voiding (voiding would make AC10
  permanently ungradeable).
- **Pilot:** L-spec-0003 accepted (12/12) on the recomputation.
- **Systemic:** the checker generator should pin the base sha at build time (`BASE=<base_sha>`
  substituted in, never `merge-base` at run time); the grader packet should scrub
  `Co-Authored-By`/`Claude-Session` trailers from any commit text it hands over, or the AC10
  shape should point at the card and the history file only.
- **Fix status (2026-09-15):** **open** — a-5 (`BASE` pinned) and a-8 (trailer scrub).

### S32. Someone ran `doit tick` on this ledger: two metered Executor spawns, three seat spawns nobody served, and a ghost spec
- **Measured:** `L-executor-0003` (11:27 box time) and `L-executor-0004` (12:00) carry
  `cost_usd` 1.016 and 1.073 — the tick's own `claude -p` line, i.e. the metered pool the
  project bans (spec 572). The driver pane never runs the tick (`DOIT_NO_POKE=1`, ruling on
  `pilot-1447`); no cron line exists; the actor is unknown (a parallel session or the operator).
  The tick's Executor then: answered charter 4's R5 brief with a NEW spec id `L-spec-0004` (empty
  file + a slot it wrote), dispatched `spec-writer` twice and `charter-reviewer` once on the seat
  route — where a seat spawn waits for a human pane to serve it, and none did — so all three
  timed out after 30 min (`spawn-failed`); and stamped `sweep-fixpoint` on L-charter-0001 at 11:27,
  before the driver's own at 13:58. The driver later answered the same R5 brief with L-spec-0003,
  so the ledger now holds two `brief-answered` for one brief.
- **Pilot:** nothing external was touched (the seat spawns never ran). The ghost `L-spec-0004`
  stays allocated and empty; an operator `correction` is the only clean fix.
- **Systemic:** (1) the tick must refuse to run when the ledger root's `env.sh` says
  `DOIT_SEAT=1` and no pane has registered as the seat server — a seat spawn with no server is
  a 30-minute hole by construction; (2) the tick's own spawn must honour the same seat/metered
  ruling as its children (it did not: children went seat, the Executor went metered);
  (3) `doit` needs an actor identity on every append beyond the filename (who ran this tick?).
- **Fix status (2026-09-15):** **shipped 0.2.0** — the mechanism is R1 below (the wrapper's own atexit poke); `poke()` and `tick.main` now read `models.toml` and refuse; **verified on this root** (`tick{refused}` 2026-09-15T05:58:31Z). Actor identity on every append (3) is open, a-16.

### S33. The only operator close for a built, merged spec is `spec-closed`, and the fold then calls it `closed-unbuilt`
- **Mechanism:** L-spec-0001 and L-spec-0002 were built, graded (11/12 and 10/12), reviewed
  with zero blocking findings, and merged. Their unmet criteria are post-merge observations by
  construction (a check-run on the merge sha; a cron that exists only after the deploy). The
  grader records them `cannot-assess`, so `verdict.confirmed` is false; the state is `shipped`,
  never `accepted`; and `shipped-owed-evidence` only derives from an `owed-ac` with a future
  `wake_at`, which no seat writes (S15). The charter's L2 conjunct needs every spec in
  `accepted / shipped-owed-evidence / dropped / closed-unbuilt`, so both charters sat at
  `L1-complete` after their charter reviews said `complete`. The one operator instrument that
  moves a spec out of `shipped` is D112's `spec-closed`, whose fold label is `closed-unbuilt`
  and whose docstring says "the operator closed it without a build". The board now reads
  `L-charter-0001 · L2-complete · 1 closed unbuilt` for a charter whose one spec was built and
  is live on master. The label is false and the operator had no truthful alternative.
- **Second half:** the rogue tick's ghost `L-spec-0004` (S32) bound itself to `L-charter-0004`
  by allocation alone — two `spawn-started`/`spawn-failed` pairs, no `spec-written`, empty
  content file, state `unknown` — and that alone held the charter at L1 after every real spec
  was closed. There is no non-operator way to retire an allocation that never became a spec;
  `spec-closed` was used (truthfully, this time) for that too.
- **Cost:** two operator rulings and three operator events to close two charters the ledger
  already had every fact to close; a board line that misdescribes the pilot's headline result.
- **Systemic:** (1) a re-grade path for owed criteria: an operator (or Executor, citing
  evidence) `owed-met` event per criterion, folded into the verdict so `accepted` derives
  from what was actually observed after merge; (2) `spec-closed` should carry a `built:` flag
  or the fold should label a closed spec that has `shipped` as `closed-shipped`, never
  `closed-unbuilt`; (3) an allocation with no `spec-written` after its spawn fails should not
  bind to the charter's L2 conjunct — the fold should treat it as `void`, or `alloc` should
  be reversible by the actor that allocated it.
- **Fix status (2026-09-15):** **open** — a-6 (`owed-met`; `closed-shipped`; a void allocation does not bind L2).

### S34. `doit deploy` can never mark a deploy landed when the target prints a short sha — and would have written a false `deploy-failed` on a deploy that landed
- **Mechanism:** `deploy.py::live()` is `code == 0 and sha[:12] in out`. The `--sha` is the
  full merge sha (the script refuses anything under 7 chars and the driver notes say "pass the
  full merge sha"). The target's `/version` prints `{"sha":"21962db5f",…}` — nine characters,
  what `git rev-parse --short` gives on this repo. Nine can never contain twelve. The wrapper ran
  the gate and `deploy.sh --all` (exit 0, `/version` stamped at 17:23:40Z), then sat in its
  check loop for the rest of the 1500 s cap, at the end of which it would have appended
  `deploy-failed` + `blocked-external` with `why=the check never reported 21962db5f239 live` —
  on a deploy that was live from minute two. The listing coordinator spotted the same mismatch
  from the outside and sent an URGENT "do not roll back on the timeout".
- **Compounding:** because `subprocess.run(capture_output=True)` holds every byte of the
  deploy command's output in memory until the wrapper exits, stopping the wrapper (the only way
  to prevent the false event — `deploy.py` traps nothing) discards the whole `deploy.sh` log.
  S30's loss was a tail; this one is total. The post-deploy evidence had to be re-measured on
  production by hand.
- **Cost:** ~12 minutes of a live production deploy with no truthful ledger state; a second,
  record-only `doit deploy … --sha 21962db5f --cmd 'echo …'` to get `deploy-landed` written;
  an operator interrupt.
- **Systemic:** (1) `live()` must match on the longer-of-the-two-prefixes rule — `out` contains
  `sha[:n]` for the largest n ≤ 12 that the check actually prints, or simply `sha[:7] in out`
  since `--sha` already refuses under 7; (2) the deploy command's output must stream to a file
  under `logs/` as it is produced, with the event carrying the path, not a tail; (3) a
  `deploy-started` with neither `landed` nor `failed` after the wrapper is gone should be
  visible on the board as its own row, not silently absent.
- **Fix status (2026-09-15):** **open** — a-4 (`sha[:7]`; streamed log; a `deploy-started` with no terminal event on the board).

### S35. A spec-less operational charter could not fold L2-complete, and a seat spawn launched from the wrong directory strands its events under a second project
- **Mechanism, half one:** the L2 conjunct read `mine and all(...)`: a charter with zero specs
  (`mine == []`) could never close, whatever the operator, the sweep and the charter review said.
  L-charter-0002 — the deploy, the pilot's headline — sat at `L1-complete` with `l1-complete`,
  `sweep-fixpoint`, zero open in-scope briefs and `charter-review-complete{depth: full}` all on the
  ledger. S25 already recorded that an operational charter has no lane path *into* the close;
  this is the same charter having no path *out* of it. Fixed in the pane by dropping `mine and`
  (all() over the empty set is True; the three events that remain ARE an operational charter's
  lane); the fold tests pass.
- **Mechanism, half two:** `fold.append` stamps `project` from `$DOIT_PROJECT`, else the
  subject's first event, else `cwd.name`. The seat wrapper's child ran without the pane's env and
  the reviewer's events landed as `project: ".do-it"` because the pane launched `doit dispatch`
  from the ledger root instead of `repos/albert-scott`. The filtered board (§9.1/D93) ignored
  the whole review — verdict, seven findings, walkthrough — with no warning: `charter_review()`
  returned `None` exactly as if nobody had reviewed. Recovered by re-dispatching from the repo
  directory and serving the identical validated output + meta to the new spawn (`-0005`); the
  `-0004` file stays as the append-only record of a mis-stamped spawn.
- **Cost:** one wasted dispatch, ~15 min, and a board that said "not reviewed" about a review
  that had run for 11 minutes on Opus.
- **Systemic:** (1) `subject_project()` should win over `cwd.name` even when the first event's
  project differs from the caller's — a charter's events are the charter's project, full stop;
  the cwd fallback belongs only to a subject with no prior event; (2) the dispatch wrapper
  should refuse (or warn loudly) when `cwd.name` differs from the subject's project;
  (3) the board needs an `IGNORED (other project)` count so a filtered read cannot silently
  hide events on the subject it is displaying.
- **Fix status (2026-09-15):** **half one shipped 2026-09-14** (`dd8d993`, verified: L-charter-0002 folded L2-complete). Half two (`subject_project` wins; cwd mismatch warning; IGNORED count) open, a-12.

### T15. An idle pane answers from memory, and its memory is a day old
- **Mechanism:** asked on 2026-09-15 whether "the charters got done", the Thinker
  answered "landed, not built" from its last board read (2026-09-14 07:03). The board
  at that moment said L-charter-0001/0002/0004 `L2-complete`, three specs shipped, the
  deploy landed (R5 PASS on 21962db5f), prod at 5cd5c47de. The operator caught it
  ("so the 8 hours of work yesterday didn't build anything?").
- **Pilot:** re-read the board, corrected the answer.
- **Systemic:** the Thinker contract says "open with the inventory" for the first
  message; it should say *every* message after an idle gap re-folds before it answers
  anything about state. Cheap rule, and it is exactly §9.2's "durable state is truth"
  applied to the pane's own claims.

---
- **Fix status (2026-09-15):** **open** — b-10 (the Thinker re-folds after any idle gap).

## Retro pane — 2026-09-15 (a third actor; appended, never interleaved, per T3)

### R1. S32 resolved: the rogue tick was the wrapper's own atexit poke, and the ruling was a per-shell variable
- **Measured:** the first `tick{spawned: true}` (11:25:58Z) is the same second as
  `L-spec-auditor-0003`'s `spawn-done`; the second (11:57:34Z) is exactly 30 minutes after the
  metered Executor's own detached `L-spec-writer-0011` started — that wrapper's timeout, then its
  atexit poke. No transcript on this box carries `doit tick` as a typed command. The two metered
  spawns have their own transcripts under `~/.claude/projects/-home-albert--do-it/`
  (`entrypoint: sdk-cli`, `claude-opus-5`).
- **Mechanism:** `dispatch.main` registers `poke()` with `atexit`, guarded only by `DOIT_NO_POKE`;
  one wrapper ran from a shell without `env.sh` sourced. A per-shell variable was standing in for
  a per-root ruling.
- **Fix (shipped 2026-09-15):** `$DOIT_ROOT/models.toml` is the root's ruling on backend and
  model per contract (`src/models.py`); `poke()` and `tick.main` read it and refuse when the
  Executor's backend is not `claude-p` (refusal recorded as `tick{refused}` — fired on this root
  at 05:58:31Z); `dispatch` takes its backend from the map and refuses a `--seat`/`DOIT_SEAT`
  that contradicts it. Every terminal event now stamps `model_requested` beside `model_used`
  (S5). Full list: `docs/handoffs/pilot-retro-change-list.md`.

### R2. The harness snapshots the Agent-type list at pane start, so a contract edited mid-session is invisible to a typed spawn
- **Measured (2026-09-15 06:28Z):** after 0.2.0 gave `plan-auditor` `Bash, Write` and a *Seat route*
  section, a typed `plan-auditor` spawn from this pane (opened before the edit) reported
  `TOOLS: Read, Glob, Grep` — the tools line as it was when the pane started. S4 measured the
  opposite direction (a list that refreshed ~1 h after `install.sh`); both are true: the list
  refreshes on the harness's own cadence, and a pane cannot know whether it has.
- **Pilot:** every spawn of charter 3 is served as `general-purpose` with the contract file read
  first (the pilot's first-ten-spawns shape, S4); `meta.json` records `served_as` so the ledger says
  which shape ran. The `tools:` sandbox is therefore after-the-fact for the whole charter.
- **Systemic:** the seat route must never depend on the typed agent's tool list — the pane's
  serving prompt should always name the contract file and the seat instructions itself; and
  `doit doctor` (change list a-16) should compare the pane's live type list against `agents/*.md`
  before the first dispatch.
- **Fix status (2026-09-15):** open — a-16 (`doit doctor`); the serving-prompt half is practice
  from this charter on.

### R3. Charter 3's Planner stage on the Claude-only map: three Opus audits, zero transcription, one honest re-cut
- **Measured (2026-09-15 06:28–07:00Z):** L-plan-auditor-0007 (cut, 224 s, 100k tok, 18 tool uses),
  -0008 (re-cut, 312 s, 106k, 19), -0009 (plan, 302 s, 110k, 18) — every one served as
  `general-purpose` + contract file (R2), every one wrote and validated its own Output first
  try, every terminal event stamped `model_requested = model_used = claude-opus-5`,
  `model_match: true`, `first_on_model: true` on -0007 and `false` after. The pane typed nothing.
- **What the audits bought:** -0007 returned `bad_cut: true` — the first bad_cut of the pilot —
  on three real defects the Planner had not seen: R2's "owner on the surface" was undeliverable
  from the reaper's footprint (the analyzer renders a fixed string and sits 4 lines under the
  ratchet cap), R3's 7-day threshold makes R4 false until 2026-09-16T11:03Z (the charter's own
  premise, an S22-class defect in the Thinker's Intent), and the mode-000 fixture could not
  exercise the reap path at all. -0008 cleared the two-unit re-cut and found the seam had no
  composition owner; -0009 caught that the durable log would break the reaper's existing
  dry-run-mutates-nothing test, and that a cut-audit finding about a second surface was false on
  measurement. Sonnet judges would have had to find all of that; that comparison is not run.
- **Planner cost:** the cut, re-cut, Plan and ADR were ~30 min of pane time; the packets were
  hand-built by two scripts (`mkpacket.py`, `mkslot.py` under the job's tmp dir — S6 is still
  open, a-14, and those two scripts are the shape `packet.py` needs).
- **Systemic:** (1) a Thinker Intent that names a threshold-dependent state needs the arithmetic
  in the charter (b-6, measured again); (2) the plan-auditor's second cut round is cheap (5 min)
  and found a real seam defect — keep "one loop here"; (3) `mkpacket.py` / `mkslot.py` → a-14.
- **Fix status (2026-09-15):** the map and serving shape verified on three real spawns; a-14 open.

## Charter 3 run log (2026-09-15, retro pane; every spawn seat, `cost_usd` null, models per `models.claude-only.toml`)

| # | Role | Model (requested = used) | Outcome |
|---|---|---|---|
| — | planner (this pane, Fable) | — | cut 1 unit → `bad_cut` → re-cut 2 units; Plan + `L-adr-0005`; both pre-passes clean |
| 1 | plan-auditor (cut) · `L-plan-auditor-0007` | opus | 224 s · 7 findings · **`bad_cut: true`** (surface undeliverable from footprint; R3/R4 threshold contradiction; fixture cannot reach the reap path) |
| 2 | plan-auditor (cut, round 2) · `L-plan-auditor-0008` | opus | 312 s · 7 findings · `bad_cut: false` (seam unowned → SD1/ADR; wave-1-not-core → one wave; flag self-erases → accepted; mode-000 ruling made explicit) |
| 3 | plan-auditor (plan) · `L-plan-auditor-0009` | opus | 302 s · 6 findings, all acted on in the Plan (dry-run purity; R4 names its run; Q3 withdrawn on measurement; raise() copy bound by tests; composition fixture; guarded read) |
| 4 | spec-writer · `L-spec-writer-0013` (L-spec-0005) | sonnet | 329 s · 139k tok · 41 tool uses · 262-line spec, 7 ACs (backend), 0 owed, 0 unknowns; validated first try |
| 5 | spec-writer · `L-spec-writer-0014` (L-spec-0006) | sonnet | 539 s · 177k tok · 30 tool uses · 358-line spec, 7 ACs (6 backend + 1 owed observed-data, R4 wake_at 2026-09-16T11:04Z); validated first try; **found the cut's seam direction inverted vs the Plan and followed the Plan** (a Planner slip, R4 below) |
| — | planner (this pane) | — | `l1-complete` 07:2x — two slots written; both ran in parallel (one wave) |
| 6 | spec-auditor · `L-spec-auditor-0004` (L-spec-0005) | opus | 384 s · 115k tok · 26 tool uses · 8 findings / 9 rejected / 4 advisory · `bad_cut: false`; **caught a guard test outside the grant that asserts the exact source text the wiring replaces** (`tests/test_1048_paneless.py:454`), a 3-of-7 history check, and an unescaped-HTML path in the escalation email |
| 7 | spec-auditor · `L-spec-auditor-0005` (L-spec-0006) | opus | 511 s · 134k tok · 29 tool uses · 7 findings / 11 rejected / 5 advisory · `bad_cut: false`; **caught the missing fifth outcome** (write-open denied + read-open ok + `flock -n` REFUSED = a held foreign lock, which a read-openability classifier would reap), the durable log breaking two existing "nothing new appeared" tests, and `_snapshot()` dying on a mode-000 fixture |
| — | executor (this pane) | — | rework packets: the first `doit packet spec-writer` refused for want of `--slot` (S6) and the captured `""` crashed the dispatch (Smaller) — both fixed in the wrapper within the hour and the second rework packet built with no flag |
| 8 | spec-writer (rework) · `L-spec-writer-0015` (L-spec-0005) | sonnet | 462 s · 176k tok · 71 tool uses · all 8 fixes in place; `Writes:` widened to the guard test with `charter-gap`; AC8/AC9 added; spec exactly 400 lines |
| 9 | spec-writer (rework) · `L-spec-writer-0016` (L-spec-0006) | sonnet | 403 s · 164k tok · 51 tool uses · all 7 fixes; the fifth outcome (held foreign lock) added with its fixture; AC1 split into root-independent / permission-dependent nodes; 399 lines |
| — | executor (this pane) | — | `doit packet builder L-spec-0005` → checker is one `&&` chain (pytest on 3 files + ruff), `bash -n` clean; worktree `l-spec-0005` @ 5cd5c47de |
| 10 | builder · `L-builder-0006` (L-spec-0005) | sonnet | 349 s · 131k tok · 53 tool uses · one commit `f6a97e8f0` (+35-line module, analyzers.py net +2 → 1498/1500, 5 new tests, guard test re-pointed); checker exit 0 (27 passed, ruff clean); card validated first try |
| — | executor (this pane) | — | `doit packet builder L-spec-0006` → checker `pytest test_611 && bash -n && shellcheck`, clean; `doit packet grader L-spec-0005` (133 lines, every row from the card sidecar) |
| 11 | builder · `L-builder-0007` (L-spec-0006) | sonnet | 808 s · 200k tok · 46 tool uses · one commit `f9272051b`; 23/23 tests, `bash -n` + shellcheck clean; AC7 carried as owed; card validated first try |
| — | CI on `bf0d05556` | — | 6 of 7 checks green within 2 min; **Repo Lint Guards red — pre-existing** (red on base `5cd5c47de` and on `62c04d46b`): 2 bare UTC date casts in `asin_inventory_timeline.py`, outside the footprint → adjacent `brief`, not chased (S17); `Python Tests` still running |
| 14 | grader · `L-grader-0007` (L-spec-0006) | sonnet | 107 s · **voided itself**: ran `git log --oneline` to confirm the sha, saw the commit subject the packet strips, declared `contamination: true`, graded nothing (R6). Wrapper: `spawn-failed` contamination |
| — | executor (this pane) | — | grader.md gains the rule (no git history reads; `rev-parse` + `status` only) → contract sha changes, so D120's identical-retry refusal does not fire; packet rebuilt identical (`-grader-2.md`) |
| 15 | grader (re-grade) · `L-grader-0008` (L-spec-0006) | sonnet | 111 s · 90k tok · 15 tool uses · 7/8 `met`, AC7 `cannot-assess` (owed, wake_at not reached); `matches_intent: yes`, `card_ok: yes` |
| 16 | reviewer (gates-only, round 1) · `L-reviewer-0005` (L-spec-0006) | sonnet | 229 s · 101k tok · 23 tool uses · all paths matched (non-root, so the permission fixtures ran); AC7 `unverifiable` until wake_at; 0 blocking |
| — | executor (this pane) | — | `decision` (merge with AC7 owed, K=1) · gate clean · `git merge --no-ff` → `f38ba5dda` · `shipped` · pushed (bypass notice again) · the Executor's own `owed-ac` was **ignored by the fold** (only spec-writer/spec-auditor may emit it — S15 measured from the other side) → operator `correction` on the writer's declaration adding the `wake_at` its own text states → `shipped-owed-evidence` derived · `sweep-fixpoint` (the one brief is adjacent) · CI: the first merge's pytest run was **cancelled by the second push** (`cancel-in-progress: true` on the workflow's concurrency group), so the check-run that counts is on `f38ba5dda` |
| 17 | charter-reviewer · `L-charter-reviewer-0006` | opus | 354 s · 133k tok · 37 tool uses · **`complete`, depth full**: dry-run then one real reaper run on merged master; the foreign lock reported (flag + durable log, owner=yitzy mode=664 age=5d holder=none), not reaped (27 h early, R3-correct); the surface rendered the `detail:` line live — the cross-spec seam composes on real data; own five-lock fixture board confirmed the safety property (both held fixtures untouched). 4 findings → 3 adjacent briefs |
| — | executor (this pane) | — | `charter-review-complete` → **L-charter-0003 `L2-complete`** (L-spec-0005 accepted, L-spec-0006 shipped-owed-evidence, K=1) · `doit reap` removed both worktrees and branches, retained nothing · 07:45:10Z |
| 12 | grader · `L-grader-0006` (L-spec-0005) | sonnet | **72 s** · 81k tok · 14 tool uses · 9/9 `met`, `matches_intent: yes`, `card_ok: yes`; re-ran every check and read the test source (vs 6.5 min median on Opus in the pilot) |
| 13 | reviewer (gates-only, round 1) · `L-reviewer-0004` (L-spec-0005) | sonnet | **95 s** · 85k tok · 17 tool uses · drove all 9 paths; 0 blocking, nothing unverifiable; done-condition met (vs 4.2 min median on Opus) |
| — | executor (this pane) | — | L-spec-0005 → `accepted` (verdict confirmed + clean review); `doit gate` clean; `git merge --no-ff` → `bf0d05556`; `shipped`; pushed — origin printed "Changes must be made through a pull request" but the push landed (branch protection requires PRs with 0 approvals and does not enforce on admins; the line is a bypass notice, not a refusal — R5) |

### R4. The Planner inverted a seam's direction in the cut, and the spec-writer caught it against the Plan
- **Measured:** `cut-L-charter-0003.md` (second cut) gave the reaper unit `Consumes: LIVENESS_FLAG_DETAIL_LINE`
  and the renderer unit `Produces:` it — backwards: the reaper writes the line, the renderer reads it.
  The Plan's Seams section had it right. `doit audit` passed the cut (a Consumes with a matching
  Produces is "defined", whichever way round), both Opus plan-audits passed it, and the Sonnet
  spec-writer for the reaper (`L-spec-writer-0014`) was the one that noticed, followed the Plan,
  and recorded a resolved `seam-undefined` declaration.
- **Systemic:** the pre-pass checks that a seam's two ends *exist*, not that the producing unit is
  the one whose footprint holds the producer. A cheap extension: `audit.py` could check that a
  `Produces:` name is in the same unit whose Goal line names it as written/emitted — or, simpler,
  the Plan's Seams section is the authority and the cut's Consumes/Produces lines are derived from
  it at plan stage, never typed twice. Filed under a-14's packet/plan work.
- **Fix status (2026-09-15 v0.3.0):** `audit.py` checks seam direction as a conservative cut-stage
  finding (a-14, `5423a9c`); deriving the cut's lines from the Plan is still open.

### R5. "Changes must be made through a pull request" is a bypass notice on this repo, not a refusal
- **Measured (2026-09-15 07:19Z):** `git push origin master` of the L-spec-0005 merge printed
  `remote: - Changes must be made through a pull request.` and **landed** (`git ls-remote` →
  `bf0d05556`). Branch protection on `master`: `required_pull_request_reviews` with
  `required_approving_review_count: 0`, `enforce_admins: false`; the pushing account is an admin.
  The pilot pushed master directly all day yesterday under the same rule; the notice is GitHub's
  "you bypassed" line. The repo's CLAUDE.md line "master takes direct pushes with no push
  restrictions" is half true: there is a rule, and admins pass through it.
- **Systemic:** the Executor's merge row should read the push's *result* (`git ls-remote` or the
  exit code), never its stderr prose — a remote's notice text is not a verdict. One line in
  executor.md (b-9) and, if `doit` ever owns the push, an exit-code check.
- **Fix status (2026-09-15):** open — b-9.

### R6. A grader that confirms its sha with `git log` reads the one line the packet strips, and the honest model voids itself
- **Measured (2026-09-15 07:4xZ):** `L-grader-0007` (Sonnet) ran `git log --oneline` in the worktree
  "to check the commit state" before any checker, saw the builder's commit subject (`reaper's lock
  test learns unopenable is not held`) — an argument for exactly the criterion under grade — and
  returned `contamination: true` with every row `cannot-assess`. The pilot's Opus grader
  (`L-grader-0003`, S31) hit the same class through a commit trailer and chose to flag rather than
  void. Neither was wrong by its contract; the contract never said which git was allowed.
- **Cost:** one 107 s spawn and a re-dispatch; the D120 identical-retry guard would have refused
  the same packet on the same contract, so the fix had to be the contract (it was: the rule is now
  in `grader.md`, and the contract hash moved).
- **Systemic:** blindness that is constructed in the packet can be un-constructed by a tool the
  contract grants — Bash is `git`. The rule now names the git the grader may run. The stronger
  fix (a-8) is the packet builder scrubbing trailers *and* the grader running in a worktree whose
  HEAD commit message is replaced by the spec id (a detached `git commit-tree` of the same tree),
  so there is nothing to read.
- **Fix status (2026-09-15):** contract rule shipped (unreleased, `agents/grader.md`); the
  structural fix is a-8.

### R7. Charter 3 closed: 77 minutes, two specs, seventeen spawns, no operator intervention, on Sonnet authors and Opus judges
- **Measured (2026-09-15, ledger timestamps):** `cut-written` 06:27:50Z → `l1-complete` 06:56:59Z →
  `shipped` 07:19:27Z (L-spec-0005) and 07:36:52Z (L-spec-0006) → `charter-review-complete`
  07:45:04Z → `tree-reaped` 07:45:10Z. **77 min for the whole charter** against ~2 h *per spec* in
  the pilot (S29). Both units ran as one wave, so the two spec chains overlapped.

| Role | Model | Spawns | Failed | Blended tokens | Spawn minutes | Median |
|---|---|---|---|---|---|---|
| spec-writer | Sonnet 5 | 4 | 0 | 655k | 28.9 | 7.2 |
| builder | Sonnet 5 | 2 | 0 | 331k | 19.3 | 9.6 |
| plan-auditor | Opus 5 | 3 | 0 | 315k | 14.0 | 5.0 |
| grader | Sonnet 5 | 3 | 1 (self-void, R6) | 254k | 4.8 | 1.8 |
| spec-auditor | Opus 5 | 2 | 0 | 250k | 14.9 | 7.5 |
| reviewer | Sonnet 5 | 2 | 0 | 186k | 5.4 | 2.7 |
| charter-reviewer | Opus 5 | 1 | 0 | 133k | 5.9 | 5.9 |
| **total** | | **17** | **1** | **2.12M** | **93** | |

  Tokens are the harness's single blended figure per sub-agent (no input/output/cache split on
  the seat route — the pane's own tokens are unmeasured); every spawn's `model_requested` equals
  its `model_used`; the seven `(contract, model)` pairs above are stamped `first_on_model: true`
  on their first run — the D120 trust runs the map owed.
- **What the map bought:** graders and reviewers on Sonnet ran in 1–3 min against 4–7 on Opus in
  the pilot, and every one validated its Output first try; the pane typed nothing (S18 verified).
  The Opus judges found real defects at every stage (R3, and the two spec-audits: a guard test
  outside the grant that the change would break; a held foreign lock a naive classifier would
  reap). Sonnet writers absorbed 8 and 7 fixes in place at ≤ 400 lines. Whether Sonnet judges
  would have found the same is not measured.
- **Operator hands:** zero during the run. One ruling was taken by the pane as operator under the
  2026-09-15 autonomy instruction — the `correction` promoting the writer's stated `wake_at` into
  the field (see the run log) — and is named here so it can be vetoed.
- **What is owed:** L-spec-0006 AC7 (charter R4) wakes 2026-09-16T11:04:00Z; nothing runs the
  reaper on cron (paused), so a human runs `/opt/albert-scott/scripts/liveness_reaper.sh` once on
  master after that instant, then the lock is gone and `~/.claude/ledger/liveness/liveness-reaper.log`
  names `yitzy`. Then a re-grade (or `owed-met`, a-6) turns the spec `accepted`.
- **CI:** `Python Tests` on `f38ba5dda` **completed success** at 08:22Z (pytest + pg-parity green,
  45 min); every other required check green; `Repo Lint Guards` red is pre-existing and adjacent
  (brief filed). The first merge's `pytest` run was cancelled by the second push's concurrency group.
- **Fix status (2026-09-15 v0.3.0):** a-6, a-14 and the token split + weights shipped (`CHANGELOG.md`
  0.3.0); charter 3 re-measured at 45.7M cache-read + 458k output = 8.96M input-equivalent tokens
  (`doit spend L-charter-0003`). Open: b-6 (threshold arithmetic in the charter).

## Driver + Thinker pane, 2026-09-15 09:00–11:00Z (job 37cb0ec6 · L-thinker-0003 · operator-approved six changes → v0.3.0; audit → L-charter-0005…0010)

Own section per the pilot-record convention (change list b-12). Measured while presenting the
six changes, building them with five Sonnet sub-agents, and writing six charters from the QurLife
walkthrough. Ranked by minutes lost or defects caught.

### R8. Two places declare a pane's model, and the launchers read neither
- **Measured:** `models.toml` says `claude-fable-5-1` for thinker and planner; `agents/thinker.md`
  and `agents/planner.md` frontmatter say `model: claude-opus-5`; `doit up` and `doit think` pass
  no `--model`. The panes opened for the next drive were put on Fable only by hand-adding
  `--model claude-fable-5-1` to the printed command; the Executor pane, launched with
  `--model claude-sonnet-5`, came up on Opus (the flag did not take for that id).
- **Systemic:** the S5 class again — the ruling lives in one file and the launcher obeys another.
  `up.pane_cmd()`/`think.pane_cmd()` should read `models.toml` and pass `--model`; the frontmatter
  model line on pane contracts should go, or be generated from the map.
- **Fix status:** open — change list a-18.

### R9. A charter template with no deploy line yields charters that prove themselves on a worktree
- **Measured:** all five audit-derived charters said "re-run on the merged sha"; L-plan-auditor-0010
  (finding 3) caught that no unit in the set deploys the fixes and the goal's done-condition is
  what Shlomo sees on production. L-charter-0001 had a deploy line only because a hand wrote it.
- **Systemic:** `agents/thinker.md` §3 (Constraints) needs a required line — how this charter's
  result reaches the surface named in §4 — and `think.py`'s landing check could refuse a charter
  whose done-for-the-whole names a host that no Constraint line names a deploy for.
- **Fix status:** open — change list b-13; the six charters carry the line by hand.

### R10. The charter-set audit under-reports by construction, and filed charters are invisible
- **Measured:** `doit think --land` built the packet from only the five files passed, so the
  pre-pass reported G1 and G2 as uncited (both are delivered by L-charter-0002/0004); the packet was
  rebuilt by importing `think.py` over every `charter-filed` citing the goal (T11's workaround,
  second time). The board has no `CHARTERS OPEN` block, so six filed charters do not appear on it.
- **Systemic:** change list a-9, hit again.
- **Fix status:** open — a-9.

### R11. Operator events have no shape check
- **Measured:** `doit append decision L-goal-0001 line=…` was accepted; the board renders `why`, so
  the DECIDED line read `?` until a `correction` moved the text (L-operator-local.jsonl:9 → :10).
  `doit alloc -h` allocated `content/L--h-0001.md`.
- **Systemic:** `fold.append` should carry a per-type required-field table for the operator's
  event types (`decision`: `why`+`revert`; `owed-met`: `criterion`+`evidence`; `correction`:
  `ref`+`set`+`why`) and refuse a bare kind on `alloc` (a-16 "smaller").
- **Fix status:** open — change list a-19.

### R12. Sub-agent worktree isolation pins to the cwd repo, not the repo under change
- **Measured:** five builders dispatched with `isolation: worktree` from a pane whose cwd is
  `/opt/albert-scott` each got a worktree of *that* repo. One refused (correctly), three
  improvised worktrees of `~/do-it-v2` under `/tmp`, one was re-dispatched after a worktree was
  cut by hand under the job's tmp dir. Same brief, three behaviours.
- **Systemic:** a v2 change is itself a build; the driver should cut the worktree (`git -C
  ~/do-it-v2 worktree add …`) and name it in the brief, never rely on the harness's isolation.
  One line in `scripts/seat/README.md` and the driver's own practice.
- **Fix status:** practice from this session on; README line — change list b-14.

### R13. The previous remediation round closed money defects with copy, and verification accepted it
- **Measured:** spec 1322 marked AS-18/19/20 "EXPLAINED" (a note that Net P&L and Real profit
  differ, both left on screen); rev confirmed; the 2026-09-15 walkthrough finds all three STILL
  BROKEN because a client cannot trace money through an explanation. The done-condition was
  "explain", not "reconcile".
- **Systemic:** not a v2 defect — the v2 charter's done-for-the-whole being *the client's
  question* is the mechanism that prevents it; the six charters phrase every §4 that way and name
  1322's lineage so a builder cannot satisfy a requirement with copy. Keep §4 strict at charter
  review.
- **Fix status:** by construction in L-charter-0005…0009.

### R14. Cost was invisible, and cache reads are the cost
- **Measured:** charter 3 read as 2.12M "blended" tokens; the four-way split from the sub-agent
  transcripts says 45.7M cache-read + 1.68M cache-write + 458k output + 854 input = 8.96M
  input-equivalent tokens (`[weights]`: output 5, cache-read 0.1, cache-write 1.25). The whole
  root: Opus 45 spawns 18.1M weighted, Sonnet 11 spawns 6.3M. Cache reads dominate because a
  sub-agent runs 40–140 tool calls over a growing context.
- **Systemic:** a-7 (packets carry the AC table and the diff, not the whole spec) is now the
  measured lever, and per-contract token budgets (`budget-exceeded`, §4.4) can be set from real
  numbers.
- **Fix status:** measurement shipped (v0.3.0, retro step 9); a-7 and budgets open.

### R15. Serving a seat spawn is still a hand-run loop
- **Measured:** L-plan-auditor-0010 was served by the pane running the Agent tool, then typing
  model, session, turns, duration and tokens into `stamp.sh`. v0.3.0 reads the tokens from the
  transcript; turns and duration are still typed, and the wrapper cannot tell whether anyone is
  serving a packet it wrote.
- **Systemic:** with the Executor as its own pane (this session's layout), serving is that pane's
  loop; `usage.py` already has the transcript, so turns and duration can come from it too, and
  the wrapper could write a `seat-unserved` alarm after N minutes with no `.meta.json`.
- **Fix status:** open — change list a-20.

### R16. The Opus judges earned their seats again
- **Measured:** L-plan-auditor-0010 (Opus, 5.6 min, 118k blended) found the deploy seam, three
  shared files with no owner, R5's footprint overreach, a dangling unit name and the uncovered
  audit finding N3 in one round. Every one changed the charters.
- **Systemic:** none; recorded so the map's three Opus seats keep their evidence.

## Thinker pane, 2026-09-15 10:19Z → (L-thinker-0004 · shape B brief triage, then G4/G5/G3-residue charters from L-charter-0010's probe)

Own section per b-12. Measured while triaging the adjacent inbox into
`content/brief-triage-L-thinker-0004.md` and waiting for the probe write-up. Ranked by what it
cost or hid.

### R17. The adjacent inbox is invisible, and a Thinker cannot tag it
- **Measured:** 15 `brief` events on the ledger, 13 adjacent, 10 open facts after dedupe — and
  the board renders none of them (no INBOX block; `open_briefs()` counts only briefs carrying
  `requirement`). §7.9 says triage "clusters and tags"; `EMITS` lets a thinker append only
  `charter-filed`, so every tag lives in a markdown file the fold never reads. One brief
  (`L-executor-0001.jsonl:6`, `blocked_me: true`, no `requirement`) was delivered by L-spec-0002
  on 2026-09-14 and has no `brief-answered`, no board row, and no way for this seat to say so.
- **Systemic:** the inbox is a fold query the board does not run, and the tag is an event that
  does not exist. Change list a-22 (board INBOX line + `brief-triaged{ref, tag, writeup}` on the
  thinker's list) and b-15 (§7.9 / `agents/thinker.md`: a tag is an event, the write-up cites it).
- **Fix status:** open — a-22, b-15.

### R18. Briefs have two shapes
- **Measured:** 11 briefs carry `fact` + `footprint` (L-executor-0001/0002); 4 carry `why` +
  `footprint_hint` (L-executor-0005). §2.6 promises dedupe-by-footprint "is a fold query only
  because this field exists"; with two names it was a hand read (b3 = b12 found by eye).
- **Systemic:** no field table for `brief` — the a-19 gap (operator event shapes) one type over.
- **Fix status:** open — change list a-21.

### R19. `doit alloc --help` allocated `content/L---help-0001.md`
- **Measured:** a 0-byte file under content, no ledger reference, removed by hand and named for
  veto in the session. Third hit of the same class (S19, T2, R11).
- **Systemic:** none new — a-16 / a-19 already carry the fix; recorded as the third measurement.
- **Fix status:** open — a-16, a-19.

### R20. Waiting on an operational charter's write-up has no named signal
- **Measured:** this seat's standing job starts "when L-charter-0010's probe write-up lands
  under content". `evidence{path, covers}` exists and L-charter-0002's deploy record landed
  with it (L-executor-0002.jsonl, 07:35Z) — but L-charter-0010's text says only "a write-up
  under content", the probe contract declares only `charter-gap`, and nothing tells the
  waiting pane which event to watch; it polled `find content -newer …` until it found the
  precedent by grepping the ledger.
- **Systemic:** b-7's operational-charter shape names the lane (`evidence` → `l1-complete` → …)
  but the Thinker template and the probe contract never say "the deliverable lands as
  `evidence{path, covers}`". Change list b-16.
- **Fix status:** open — b-16.

### R21. `DECIDED WITHOUT YOU` has no cursor
- **Measured:** 17 decisions rendered in full (~5 KB) on every `doit`, most of them from
  2026-09-14 and already acted on; the contract's "open with the inventory" is read past them
  each time. `SHIPPED SINCE YOU LOOKED` has the same "since" with no "you".
- **Systemic:** the fold has no per-actor looked-at cursor; a `looked{actor, ts}` event or a
  `--since` on the board would bound both blocks.
- **Fix status:** open — change list a-23.

## Executor pane, `L-executor-0006` — 2026-09-15 (folded the board, cleared it, polling for the
Planner's first spawn on L-charter-0005…0010; nothing built yet)

### R22. A decision's `ref` not naming the question's `src` leaves NEEDS YOU open a full day after the ruling shipped
- **Measured:** `L-builder-0004.jsonl:8` opened a `question` on `L-spec-0003` (deadline
  2026-09-15) asking to ratify the ninth collection-parity allowlist entry. An Executor `decision`
  answering that exact question exists (2026-09-14, "AC3 mandates removing the parametrized
  case... allowlist must carry NINE ids, not eight") — but it was appended with `ref=AC12`, not
  the question's `src`. `doit`'s fold matches a question to its answer by `ref == src` (executor.md:
  "a decision's `ref` is the question's `src`, as `doit events` prints it"), so the match never
  fired: the default was taken and shipped a day ago (prod carries the ninth entry, deployed
  5cd5c47de), yet `NEEDS YOU (1)` still rendered this morning as if nobody had ruled. I re-decided
  it (`ref=L-builder-0004.jsonl:8`) to clear the board; the ruling itself did not change.
- **Systemic:** the contract already states the rule correctly; nothing enforces it. `doit append
  decision` accepts any `ref` string, so a hand-typed shorthand (`AC12`, a criterion id — reads
  naturally to a human, wrong to the fold) silently produces a permanently-stale NEEDS YOU row
  instead of an error at write time. The fold has no way to tell "answered with the wrong ref"
  from "never answered" — both render identically until someone reads every open question's full
  text and greps for it by hand, which is what this session did.
- **Fix status:** open — change list a-24 (validate `ref` against the subject's open-question
  `src` set at append time, not at fold time).

### R23. The `--check` convention for a droplet target is SSH-wrapped, and nothing in `executor.md` says so
- **Measured:** `executor.md`'s merge/deploy row and `deploy.py`'s own help text both just say
  `--check '<the charter's post-deploy check>'` with no shape. Read naturally ("`/version` names
  the merged sha") that reads as a public-URL probe; from this pane, `https://clients.
  albertscott.com/version` 404s (that host is the Vercel frontend, not the API) and
  `/api/version` 401s (portal auth). `deploy.sh`'s own rollback block never calls it that way — it
  reads the sha over `ssh root@167.71.46.51 "curl -sf http://127.0.0.1:8000/version"` (deploy.sh:785).
  Charter 2's R5 ("PASS, `/version` 21962db5f") was measured that way, not over the public host —
  the run log doesn't say so, and a pane driving its first deploy would have to find deploy.sh:785
  itself to learn the shape, as this one did before any charter needed it.
- **Systemic:** a charter's Constraints line naming `/version` as the check is correct but
  underspecified; the command a builder or Executor actually runs needs the SSH form, and that
  convention lives in one script 3,900 lines away from the contract that tells the Executor to
  run it.
- **Fix status:** open — change list b-17 (state the SSH-wrapped form in `agents/executor.md`'s
  dispatching section, once, so every future `--check` is written correctly the first time).

## Planner pane, `L-planner-0005` — 2026-09-15 10:17Z → (Fable; L-charter-0010 commissioned as four probes, then L-charter-0005)

Own section per b-12. Measured while reading in and commissioning L-charter-0010's probes
(L-probe-0001…0004, dispatched 10:29:27Z, all four served by `L-executor-0006`). Ranked by what it
cost or hid.

### R24. Three documents disagree on where a probe runs and who allocates its directory
- **Measured:** `agents/planner.md` ⓪ says `--path "$R/content/probe-<charter>"`, `--cwd
  "$R/repos/<project>"`, and "`doit alloc` … is the wrong tool here"; `agents/probe.md` Input 5
  says the run directory is "allocated for you by `doit alloc probe --dir`" and "is your cwd";
  `src/dispatch.py:98` says the probe's cwd is OUTSIDE every repo on purpose and `fold.py:888`
  implements `alloc … --dir`. Followed probe.md + dispatch.py (`doit alloc probe --dir` ×4,
  `--cwd <run dir>`); the planner contract's line would have put the porcelain check on
  `/opt/albert-scott`, where `docs/sessions/*` is volatile (`DOIT_REPO_VOLATILE`), and would have
  named a path no `alloc` claimed. Ten minutes reading three sources to settle one flag.
- **Systemic:** the planner contract predates `alloc --dir`. Change list b-18.
- **Fix status:** open — b-18.

### R25. `doit packet` has no `probe` role, so the one packet the Planner builds by hand is the one with credentials in it
- **Measured:** `doit packet` accepts seven roles; `probe` is not one, though `doit dispatch`
  accepts it. Four probe packets were hand-written (347 lines) carrying the charter verbatim, the
  externals, N, the credential NAMES, the run directory, the record shape and the read-only
  rules — the a-14 shape, one role short. The strip list (`packet.py::strip`) therefore never
  checked them for a cut or a Plan; the only contamination guard on a probe packet is the
  author's care.
- **Systemic:** a-14 stopped at plan-auditor + spec-writer. Change list a-25.
- **Fix status:** open — a-25.

### R26. Four detached dispatches in one second share one log file
- **Measured:** `doit dispatch … --detach` ×4 at 10:29:27Z → every child printed the same
  `log: …/dispatch-probe-2026-09-15T102927+0000.log` (`dispatch.py:619` names the file by role +
  second). All four wrappers append to it interleaved; a wrapper failure is not attributable to
  a spawn without reading the seat dir.
- **Systemic:** the log name has no spawn id because the id is allocated after the fork.
  Change list a-26.
- **Fix status:** open — a-26.

### R27. The Planner contract has no operational-charter branch, and its checks read an evidence-only unit as undetermined
- **Measured:** L-charter-0010 is operational by its own Constraints ("deliverable is the
  evidence write-up and the decision events, not code"); b-7's lane (`evidence` →
  `l1-complete` → sweep → charter-review) is in the fold (`fold.py:657`, `dd8d993`), and
  `evidence` is unrestricted in `EMITS`. `planner.md` ①–⑦ assume code units: a zero-unit cut
  makes the cut-audit's requirement-coverage check report every R undelivered, and
  `audit.sizes()` reads a footprint of new files only as "unmeasurable … not a small unit" —
  so an evidence unit can never come back clean. The operator's brief for this pane said
  "cut, plan, commission"; on the contract as written the honest reading is: commission the
  probes (⓪), write the cut and Plan as records of why there is no code unit, spend no audit
  spawn on a zero-unit cut, land the write-up as `evidence{path, covers}`, then `l1-complete`.
  Named here for veto.
- **Systemic:** b-16 names the landing event; nothing tells the Planner which of its seven
  steps apply. Change list b-19.
- **Fix status:** open — b-19; this charter is the first measurement.

### R28. "Read-only against production" is a sentence in the packet, not a credential
- **Measured:** `/opt/albert-scott/.env` holds one Supabase DSN, `SUPABASE_DB_URL`, read-write;
  no `*_RO` / read-only DSN exists on the builder box (the `dev_readonly` role exists on prod per
  the privilege census, its password is not on this box). §4.6·10 says the probe holds "read
  scope … never write access to product data"; the only enforcement available was the packet
  rule `PGOPTIONS="-c default_transaction_read_only=on"` plus a pasted `SHOW` line in the run
  record. Same for `ssh root@167.71.46.51`: a root shell told to read.
- **Systemic:** the probe contract does not itself require the session-level read-only proof;
  a packet author who forgets it hands a probe a write path. Change list b-20 (contract text);
  the read-only DSN is a product-side brief, not a v2 change.
- **Fix status:** open — b-20.

### R29. A probe declares a charter-gap that one `git diff --stat` resolves (L-thinker-0004)
- **Measured:** L-probe-0004 ran the profit_v2 test tree at checkout `fd75a5210` and declared
  `charter-gap: figures here are checkout-sha evidence` because it may not check out the deployed
  sha. `git diff --stat 5cd5c47de fd75a5210 -- api pipelines agents` is empty — the tree it
  tested is the deployed one. The Thinker resolved it by hand; the probe could not, and the
  packet did not tell it.
- **Systemic:** the probe packet's script pre-pass should state, per external, whether the paths
  under test differ between the checkout and the deployed sha, so "checkout ≠ deployed" is a
  fact on the packet, not a gap the probe declares and a Thinker closes. Change list a-27.
- **Fix status:** open — a-27.

### R30. An operational charter's source material was stale by two days, and the audit did not catch it (L-thinker-0004)
- **Measured:** L-charter-0010 framed B1189 as "unresolved" from spec 1178 (2026-08-29); spec
  1180 (`source_brief: B1189`, shipped 2026-08-31) had already unified both writers, and
  L-probe-0001 measured 0/303 mismatches where 1178 measured 25/303. Likewise the 827 "policy
  call" both prior records call open was ratified (the wrong way) by spec 1187 R2 with a test.
  Neither L-thinker-0003 nor L-plan-auditor-0010 read the v4 ledger for successor specs of the
  ids the charter named; the probe found both in ten minutes with `git grep` at the sha.
- **Systemic:** the spec-auditor has `stale-current-state` / `premise-from-prose`; the
  charter-set audit has no term for an Intent built from a record older than the deployed sha.
  A Thinker template line — for every brief or spec id the Intent names, the newest v4 record
  citing it and its status — is a read the Thinker can do and the plan-auditor can check.
  Change list b-21.
- **Fix status:** open — b-21. L-charter-0013 … 0015 cite the probe run-records, not the prior
  specs, for every figure.

### R31. `doit think --land --print-only` lands (L-thinker-0004)
- **Measured:** two dry runs (five charters at 10:57:02Z, one at 10:58:57Z) each appended
  `charter-filed` before the real landing at 10:59:06Z: `think.land()` appends in its first loop
  and checks `print_only` only around the dispatch (`src/think.py:129-132, 148`). Six duplicate
  `charter-filed` events on L-thinker-0004.jsonl (lines 2-6, 8), voided by six operator
  `correction{set: type=voided}` events from the pane under the autonomy instruction.
- **Systemic:** `--print-only` on `open_session` means "build the command, run nothing"; on
  `land` it means "run everything but the dispatch". The flag's own help text promises the
  first. Change list a-28.
- **Fix status:** open — a-28.

### R32. A probe's `measured_at` is typed, not captured (L-planner-0005)
- **Measured:** L-probe-0002's run record stamps `measured_at: 10:31–11:20Z` and `11:20–11:35Z`
  on a spawn whose ledger window is 10:29:27 → 10:48:26Z (`spawn-done`, 1,076 s). The figures are
  backed by raw files; the times are invented. L-probe-0004, whose packet asked for `date -u`
  before and after each run, has true stamps. "Record the date" produced a typed field;
  "stamp `date -u` into the raw file" produced a measurement.
- **Systemic:** the record shape asks for a time and nothing captures one. Change list b-22
  (`probe.md`: the run record's time is copied from a captured `date -u` line, never typed);
  the consolidating write-up cites the ledger window as the measurement time.
- **Fix status:** open — b-22; the L-charter-0010 write-up says so in its Caveats.

### R33. `research`'s 5-minute default timed out a seven-question map that then finished anyway (L-planner-0005)
- **Measured:** `L-research-0001` (haiku, per the map) was dispatched 10:33:26Z with a seven-part
  question over ~9k lines of profit_v2 code; the wrapper recorded `spawn-failed{why: "timeout
  after 5 min"}` (`ROLES["research"] = ("file", 5, 1)`, `dispatch.py:39`) while the sub-agent
  kept writing and landed a 281-line map at 10:39Z. The map is usable and unaudited: the failed
  spawn appended no `research-done`, so nothing in the ledger points at the file.
- **Systemic:** the role default fits a one-question dig; a Planner-sized map is 3× that. Either
  the packet author passes `--timeout` (it exists) or the default moves to 15 min like
  reuse-scout. Change list a-29.
- **Fix status:** open — a-29; the Planner used the file as pointers and re-verified every line
  it relied on.

### R34. The packet's read-only rule did not survive the pooler; the probe fixed it on its own (L-planner-0005)
- **Measured:** `PGOPTIONS="-c default_transaction_read_only=on"` does not take effect through
  the Supabase session pooler (`SHOW` printed `off`); L-probe-0001 noticed, switched to `SET
  default_transaction_read_only = on` as the first `-c` of each `psql` invocation, and pasted
  every `SHOW` → `on`. A less careful contract would have run read-write on the RW credential
  with the packet believing otherwise. Also measured: `probe-run` carries
  `path/externals/n_inputs/spend/complete` and no `summary` (`dispatch.py:391`); the summary
  lives only on `spawn-done`, so `doit events <charter>` shows where a probe looked, not what
  it found.
- **Systemic:** b-20's text must name the session-level `SET`, not the startup parameter
  (amended in place); `probe-run` gaining `summary` is one line (a-30).
- **Fix status:** open — b-20 amended, a-30.

### R35. The Thinker's own landing needs a spawn the Thinker may not serve (L-thinker-0004)
- **Measured:** `doit think --land` (six charters, 10:59:06Z) dispatched L-plan-auditor-0011 on
  the seat backend: a packet on disk, waiting for a pane with the Agent tool. `agents/thinker.md`
  forbids the Agent tool (§3.3, D73: the *command* owns the spawn). The packet sat unserved
  until the operator asked why; the previous session (L-thinker-0003) never hit this because one
  pane wore the driver and Thinker hats and served its own audit.
- **Systemic:** D73's "the driver command owns the privileged act" was written for `claude -p`,
  where dispatch ran the model. Under the seat backend dispatch writes a file and the spawn is
  whoever serves it — the rule now points at nobody. Two contract lines close it: the Planner
  pane serves every pending seat packet regardless of who dispatched it (a standing job), and
  the Thinker may serve exactly the audit its own `--land` produced, nothing else. Change list
  b-23.
- **Fix status:** open — b-23; today the Planner pane serves L-plan-auditor-0011 by hand.

### R36. `doit append decision` accepted four decisions with no `why`, and the board rendered them as `?` (L-planner-0005)
- **Measured:** the pane passed one quoted `item=… probe=… why=…` string as a single `k=v`, so
  each event landed with `item` holding the whole sentence and no `why`; `append` accepted all
  four and `DECIDED WITHOUT YOU` rendered `L-goal-0001 · ? · revert …`. Four properly-formed
  decisions were appended after; the malformed four (`L-planner-0005.jsonl:4-7`) stand and are
  named for an operator `correction`.
- **Systemic:** a-19's field table would have refused at write time; a decision without `why`
  is the a-24 class one field over. No new row — a-19 covers it; recorded as its second hit.
- **Fix status:** open — a-19.

### R37. An operational charter through the Planner: five probes, zero units, 43 minutes, $0 (L-planner-0005)
- **Measured:** L-charter-0010 `charter-filed` 09:45 → probes dispatched 10:29:27 (four, one
  per external-credential pair; a fifth at 10:59 for three criteria the portal probe could not
  reach) → `cut-written` (0 units) 10:59:49 → `plan-written` + `evidence{R1 R3 R4 R5 R6}` +
  four `decision`s on L-goal-0001 + `l1-complete` 11:12:43. Probe wall clock 10–28 min each,
  all `complete: true`, all served concurrently by L-executor-0006; the Thinker (L-thinker-0004)
  chartered six new charters from the first four run records before the write-up existed
  (R30). No cut-audit or plan-audit spawn (R27/b-19). The write-up corrected the framing of
  three charter items (B1189 dead, 877 not a crash, 255 fleet-wide) — the D96 payoff, measured.
- **Systemic:** the lane worked without a code unit; what is missing is written (b-19). One more
  gap: `l1-complete` for an operational charter is the Planner's event, but nothing derives it
  from `evidence` covering every R — a Planner who forgets it leaves the Executor's close row
  dark. Fold: an `evidence` whose `covers` spans every charter R could derive L1 on its own.
  Change list a-31.
- **Fix status:** open — a-31.

### R38. Two `bad_cut` rounds on L-charter-0005, both right, and the contract's "one loop" ran out before the cut was good (L-planner-0005)
- **Measured:** cut 1 (4 units) → L-plan-auditor-0012 (Opus, 198 s) `bad_cut: true`, ten findings: the R1/R4 core deferred to wave 2, the drawer's contract and rendering files unowned (`models_day_detail.py`, `schemas-day-detail.ts`, `day-walk-items.ts`, …), R6 bundled, the verifier "delivering" a requirement it only measures. Cut 2 (5 units) → -0013 (281 s) `bad_cut: true`, nine findings, all on code topology the Planner had not read: `_build_advertising_model` is pure and its coverage input enters at three call sites, the drawer's estimate row is built in `day_detail_ads.py:677` (the R6 unit's file, not the totals unit's), `_fetch_period_ad_figures` takes no ASIN scope (the 37.09% mechanism), `test_1346*.py` matches nothing, and `TIEOUT_RESIDUALS` declared as a seam turned an evidence artifact into a build-order edge. Cut 3 (4 units, core split by grain on one response contract) went to the plan-audit **without a third cut-audit**: `planner.md` says "that is the one loop here" and the operator's rule caps blind audits at one round; named for veto in the Plan (Q5).
- **Systemic:** the pre-pass checks names and file overlap; both rounds' findings were about *where a function's inputs come from* — call sites, pure functions, generated files — which no script check reaches and a Planner reading a 9k-line module by pointers does not see. Two things would have cut the second round: (a) the research packet asking "for each function the cut touches, its call sites and whether it is pure" (a-32: `doit packet research` template for a cut's footprint); (b) the pre-pass resolving every footprint glob and refusing a cut whose glob matches nothing (a-33 — `audit.sizes()` already knows, it only reports it under "unmeasurable" when *all* of a unit's entries miss).
- **Fix status:** open — a-32, a-33; the "one loop" rule stands, measured once as too short on a monolith.

### R39. The plan-audit cleared the cut and found nine more real things; the fix for "acceptance depends on an unmerged sibling" was to put the fixture literal in the Plan (L-planner-0005)
- **Measured:** L-plan-auditor-0014 (Opus, 348 s) `bad_cut: false`, nine findings, none a repeat of the two cut rounds: the row the client actually sums is the day-direct `DayDetailResponse.estimated_cogs` (rendered at `day-detail-walk.tsx:433`), not the waterfall line the cut named; wave-1 units cited a sibling's unmerged script and fixture for acceptance; the verifier had no owner after wave 1 changed what it measures; the seam string omitted two provenance fields the shape carries; line 1 had no identity against the rows beneath it ($94.22 on Sep 1); the daily table's TOTAL is client-side; `build()` lacked freight and the estimate rate; the allocation row had no computation path; unit 4's two "seams" had no consumer. All nine acted on in place — cut and Plan re-written, `cut-written`/`plan-written` re-appended, no second plan-audit (named for veto, Q5). The composition fix that mattered: the contract fixture is now a literal JSON in the Plan's Seams, committed byte-identical by the backend unit and read by the frontend unit's tests from the Plan — the charter-3 SD1 trick (one literal, both sides) at response-shape scale.
- **Systemic:** (1) the packet builder's sibling-`Produces:` list reads the NEXT line when a unit's `Produces:` is empty — L-spec-0007's packet says `tie-out-verifier produces: Wave: 1` (a-34, `packet.py`); (2) `Probe residue: none on file` on a charter whose Constraints name a document as the probe (D96 by reference) — the builder looks only for a `probe-run` event (a-35).
- **Fix status:** open — a-34, a-35.

### R40. Two charters through one Planner context: 0010 in 13 min of stage clock, 0005 in 36 — the Opus judges cost 14 min and bought two re-cuts (L-planner-0005, close)
- **Measured (`doit spend`):** L-charter-0010 — 5 probes (Sonnet), `cut-written → l1-complete` 12.9 min, 6.63M weighted input-equivalent tokens, $0 paid calls, zero units, closed on `evidence` + four `decision`s + `l1-complete`. L-charter-0005 — 3 plan-auditors (Opus: 3.3 / 4.7 / 5.8 min, 0.97M weighted) + 4 spec-writers (Sonnet, 10.0–12.5 min each, all four in parallel, 3.96M weighted) + 1 research (haiku, timed out, unmeasured), `cut-written → l1-complete` 36.2 min, 4.92M weighted; four specs of 339–398 lines, one owed AC (L-spec-0010 AC9, R7's production re-run, `wake_at` 2026-09-16T12:00Z), zero unknowns on three, one on the fourth. Pane wall clock for both charters: 10:17 → 11:55Z, including the read-in and eleven pilot-record commits.
- **What the judges bought:** cut-audit round 1 found the contested core in the wrong wave and four unowned contract/renderer files; round 2 found three call-site and pure-function facts no pointer-level read had seen; the plan-audit found the row the client actually sums, a missing identity (line 1 vs the rows beneath it, $94.22), and an acceptance chain through unmerged siblings. None of the three was a repeat. The 27 findings were folded in place; the "one loop" was exceeded by one cut round and by zero plan rounds, both named for veto.
- **Systemic:** the Planner's cost on a monolith is two audits deep before the cut is right (R38: a-32/a-33 would have removed round 2). Everything else is already on the list.
- **Fix status:** recorded; nothing new.

**Handover (this pane stops here — one charter per context is already exceeded by one):** L-charter-0010 `L1-complete` (Executor's close row: sweep-fixpoint, charter-review against the write-up `content/evidence-L-charter-0010.md`, no reap). L-charter-0005 `L1-complete`: L-spec-0007/0008/0009 (wave 1, parallel, disjoint) and L-spec-0010 (wave 2, after the three merge, rebase; last merge carries the deploy + R7) are `written` and the Executor's. Open sweep questions (all owed, defaults in `plan-L-charter-0005.md`): Q1 canonical lens (both stay), Q2 scoped ACoS denominator, Q3 the September estimate card changes value, Q4 R7 runs as the verifier account, Q5 no third cut-audit / no second plan-audit. For veto: four malformed `decision`s at `L-planner-0005.jsonl:4-7`; the four dead-item decisions on L-goal-0001 appended by this pane. Next charter for a fresh Planner context: L-charter-0006 in the money order, or L-charter-0012 first among the backend set per L-thinker-0004's set-order note.

## Executor pane, `L-executor-0006` — 2026-09-15, continued (picked up L-charter-0005's four `written` specs)

### R41. All four of the charter's spec-writers named an interpreter by bare word; `spec-writer.md` never states the rule the packet builder enforces
- **Measured:** `doit packet spec-auditor` refused on all four of L-charter-0005's specs
  (L-spec-0007/0008/0009/0010) the first time each was tried: three named `ruff` or `npm` by bare
  word, one (L-spec-0009) also used backslash line-continuation, which the newline lint reads as
  three statements not ending in `&&` (the line ends in a literal `\`, not the `&&` token). Four
  for four, on four different Sonnet spec-writer spawns that otherwise validated clean and passed
  spec-audit-worthy review. `grep -rn "ruff\|npm\|absolute\|bare word\|interpreter\|&&"
  agents/spec-writer.md` returns nothing — the rule `src/packet.py`'s `verify_script()` enforces
  (a-5, shipped) is nowhere in the contract that writes the block it enforces against.
- **Systemic:** a-5 closed the builder-side half (the packet refuses a bad block) but never closed
  the writer-side half (the writer has no way to know the rule exists, let alone self-check
  against it before submitting). Every one of these was a mechanical, no-judgment fix (substitute
  the absolute path already used elsewhere in the repo, e.g. `/opt/albert-scott/.venv/bin/ruff`,
  `/usr/bin/npm`; move `&&` to line-end instead of `\`-continuation) — I made them directly as
  four Executor `decision`s under the D116 one-literal carve-out (b-3) rather than re-spawning
  four spec-writers for a formatting fix each would have no way to know to avoid next time either.
- **Fix status:** open — new change list item (b) `spec-writer.md` §9 states the exact rule
  `verify_script()` checks (absolute-path interpreter list, one `&&`-gated chain, no bare `;`/`||`)
  so a writer can self-check before submitting instead of round-tripping through a refused packet.

### R42. `doit dispatch --detach` on the seat backend is the Executor's own dispatch path too, undocumented in `executor.md`'s "Dispatching" section as written
- **Measured:** dispatching my own `spec-auditor` spawns (not the Planner's) the first time, I
  read `executor.md`'s "Dispatching — the exact line" section literally: "never via an Agent tool"
  for `doit dispatch --detach`. Running it confirmed what R2/R15 already established from the
  other side: on this root's `seat` backend, `doit dispatch --detach <role> <subject> --packet
  <file> --cwd <dir> --charter <charter> --project <project>` forks a background python process
  that writes `$R/seat/<spawn>.packet.md` + `.cmd.json`, prints the detached pid and returns
  immediately, and then polls for `<spawn>.result.json` OR (`<spawn>.meta.json` AND
  `.output.json`) — exactly what serving-then-`stamp.sh` produces (`src/dispatch.py:205
  run_seat`). So `doit dispatch --detach` is not itself "an Agent tool call" — it is the correct
  and only way to allocate a spawn id and a ledger `spawn-started` event on this backend, whether
  the caller is the Planner or the Executor; the *serving* step after it (Agent tool, general-
  purpose + contract file, per R2) is the same for both. I had no doc telling me this before
  trying it; `doit packet spec-auditor <spec> --charter <file>` alone does not create a spawn id
  or a seat packet — it only proves the packet would build.
- **Systemic:** `executor.md`'s "Dispatching" section reads as written for a `claude-p` root and
  needed inference for a `seat` root Executor-as-pane (D121). The section should say, once: on
  `seat`, `doit dispatch --detach` writes the seat files and forks a waiter; you (the pane) then
  serve exactly as you would a Planner's dispatch, and the waiter's own poll — not you — writes
  the terminal `spawn-done`/`spawn-failed` event once `stamp.sh` lands the meta file.
- **Fix status:** open — new change list item (b) `executor.md`'s "Dispatching" section states the
  seat-backend shape explicitly instead of leaving it to be inferred from R2/R15/`dispatch.py`.

### R43. Twelve open charters into one serial Planner (L-thinker-0004)
- **Measured:** by 11:00Z Goal A had 12 open charters (0005–0016), each at the design's grain
  (one feature, 5–11 requirements, 2–5 specs). The Planner pane, hand-serving every seat spawn,
  had cut one (0010, zero units) and researched one (0005) — the operator read it as "10 behind".
  Per charter the fixed cost before code is ~5 served spawns (cut audit ×1–3, plan audit, spec
  audits, charter review) plus a deploy. 0005 alone took three cuts (two bad-cut) to L1-complete.
  Operator ruling: merge where footprint and done-condition are shared. Merged 0011+0012 → 0017,
  0013+0014 → 0018, 0007+0008 → 0019 (six `charter-retracted` with `ref` to the successor); 0005
  was already L1-complete with four specs, so 0005+0006 was left. 12 → 9 open, 7 waiting.
- **Systemic:** two things, neither about charter size. (1) The Thinker contract has no rule on how
  many charters a landing may put in front of one Planner, or which is first — a set of twelve is
  a queue nobody ordered. (2) The Planner works one charter at a time by habit, not by contract;
  nothing forbids cutting three in parallel, and the seat backend's hand-serving is the real
  ceiling (a-20 makes it observable, b-23 gives it an owner). Change list b-24.
- **Fix status:** open — b-24; the merge itself is done on the ledger.

### R44. A served audit with no stamp is invisible to the fold (L-thinker-0004)
- **Measured:** L-plan-auditor-0015 (the merged set's charter-set audit) was served at 12:01Z —
  `seat/L-plan-auditor-0015.output.json` exists, 11 findings, VALID — but nobody ran `stamp.sh`,
  so no `.meta.json`, no `spawn-done`, no `audit-finding` events; the ledger still shows it
  running and the Thinker folded the findings from the output file by hand. Second time today a
  seat spawn's completion depended on a human remembering the second command (R15, a-20).
- **Systemic:** a-20's `seat-unserved` alarm covers the unserved case; the served-but-unstamped
  case has no alarm and no self-heal. The wrapper can treat `output.json` VALID + no
  `meta.json` after N minutes as "stamp it yourself with what the transcript resolves" — the
  stamp is derivable (usage.py already reads the transcript). Change list a-29.
- **Fix status:** open — a-29.

### R45. A late stamp lands after the dispatcher's own timeout, and the terminal event never fires — reconstructed by hand from `events_for()` (L-executor-0006)
- **Measured:** all five of my own `doit dispatch --detach` spawns this stretch (four
  `spec-auditor`, one `plan-auditor` served on the Planner's behalf) hit exactly R44's class, one
  step further: I received each completion notification, ran `doit validate` on the Output (all
  five VALID), then paused mid-batch — first to raise the cross-pane serving-conflict question
  (AskUserQuestion + a failed `SendMessage` to a peer that ListAgents never listed), then to
  investigate the WEDGE flag it caused. By the time I ran `stamp.sh`, every one of the five
  `run_seat()` waiters had already given up at their 900s deadline and written `spawn-failed{why:
  "timeout after 15 min"}` — the real work had finished in 5.7–7.5 minutes each, well inside the
  window, but the wait ends at 900s from `spawn-started` regardless of what stamp.sh does after.
  `meta.json` then landed with no waiter left alive to consume it: the terminal `spawn-done` and
  every derived `audit-finding` never fired, and `doit packet spec-writer <spec>` refused rework
  ("no audit findings on this subject") even though real, complete audits sat validated on disk.
- **What I did:** appended a `correction` on each `spawn-failed` event naming the superseding
  valid output (so nothing re-dispatches a $0.10–0.30 duplicate audit), then wrote a one-off
  script importing `dispatch.events_for()` and `dispatch.emit()` directly — the exact functions
  `main()` calls — to derive and append the real `audit-finding`/`spawn-done` events from the
  already-validated `output.json` + `meta.json` pairs, reading `contract_sha256`/`packet_sha256`/
  `backend`/`model_requested` off the existing `spawn-failed` event rather than recomputing them.
  `doit packet spec-writer` built cleanly afterward on all four.
- **Systemic:** two gaps stack here. (1) R44/a-29 (served-but-unstamped self-heal) would have
  caught this if it existed. (2) Sharper than R44: the *cause* was my own mid-batch interruption
  for something that could have waited — `stamp.sh` should be the very next action after a
  completion notification, before any side investigation, exactly because a live dispatcher-side
  timeout is running the whole time regardless of what the serving pane does next. Worth a line in
  `executor.md`/the seat README: stamp first, investigate second. (3) `events_for()` + `emit()`
  being pure, importable functions made the by-hand reconstruction possible at all without
  re-running the sub-agent — that reusability is worth keeping if a-29 is ever built as a proper
  `doit stamp --recover <spawn>` path instead of a one-off script.
- **Fix status:** open — a-29 (the general fix); a new line for `scripts/seat/README.md` /
  `executor.md` on stamping before doing anything else with a completion notification.

### R46. The sweep's owed questions and the pane's named-for-veto rulings have no board surface, so the operator was asked in the pane (L-planner-0005)
- **Measured:** two charters produced nine owed questions (Q1–Q5 on 0005, Q1–Q5 on 0010) and six rulings named for veto. All live in `plan-*.md` files and pilot-record prose; none is a ledger event (`escalation-blocking` exists only for blocking questions, and none was blocking). `NEEDS YOU` rendered nothing for any of them. When the operator returned, the questions were put through `AskUserQuestion` in the pane and the four answers were recorded as three `decision`s on L-goal-0001 by the Planner (12:0xZ) — a pane-typed record of an operator ruling, the S25 shape the design tried to retire. Also measured, from `L-operator-local.jsonl` 12:52:57Z: the Executor's spec-auditor on L-spec-0010 hit the 15-min wrapper timeout with a VALID Output already written — the a-29 class (research, 5 min) one role over.
- **Systemic:** an owed question is a fold query only if it is an event. `escalation-owed{charter, q, default, revert}` from the Planner, rendered under a board block (`OWED QUESTIONS (n)`, defaults shown) and answered by an operator `decision` whose `ref` is the question's `src` (the R22 rule) — b-25; `named-for-veto` as the same shape with `default=stands` — same row. Wrapper timeouts sized to the measured p95 per role, not a guess — a-29 widened to every role (a-36).
- **Fix status:** open — b-25, a-36.

## Planner pane, `L-planner-0006` — 2026-09-15 13:09Z → (Fable; L-charter-0006, Bridge and Cash proofs)

Own section per b-12. Measured while reading in and commissioning the cut research (`L-research-0002`,
dispatched 13:17:52Z, `--timeout 15`). Ranked by what it cost or hid.

### R47. The seat scan the Planner contract prescribes counts dead spawns as unserved (L-planner-0006)
- **Measured:** `agents/planner.md` Input: "a `.packet.md` with no `.output.json` is yours to serve".
  Run literally at 13:10Z the scan listed nine packets; five of them (`L-charter-reviewer-0001`,
  `L-plan-auditor-0002`, `L-spec-writer-0009/-0011/-0012`) are spawns with a terminal event on the
  ledger — four `spawn-failed{timeout after 30 min}` from 2026-09-14 and one `spawn-done` whose
  output landed as `.result.json`, not `.output.json`. The other four (`L-spec-writer-0021…0024`,
  12:57:23Z) are the Executor's live rework dispatches on L-spec-0007…0010. Nothing on disk tells the
  two apart; the ledger does, and the contract's rule does not say to read it.
- **Systemic:** the serving rule needs a terminal filter — a packet is pending only while its spawn
  has `spawn-started` and no `spawn-done` / `spawn-failed` — and the dispatcher could mark the
  packet on its own terminal event (rename to `.packet.done.md`, or delete). a-20's `seat-unserved`
  alarm has the same hole if it scans the directory. Change list a-37.
- **Fix status:** open — a-37; this pane served nothing (R49).

### R48. `doit alloc --help` allocated `content/L---help-0001.md` a fourth time (L-planner-0006)
- **Measured:** the file R19 removed by hand was re-created 0 bytes at 13:10Z by this pane reading
  the flags the way every other `doit` subcommand documents them. Fourth hit (S19, T2, R11, R19).
  Left on disk this time so the count is visible; no ledger reference.
- **Systemic:** none new — a-16 / a-19 carry it. Recorded as the fourth measurement.
- **Fix status:** open — a-16, a-19.

### R49. The Planner pane has no Agent tool, so the b-23 serving rule is a sentence it cannot execute (L-planner-0006)
- **Measured:** `agents/planner.md`'s `tools:` line is `Read, Glob, Grep, Bash, Write, Skill`; the
  same file's Input says the pane "serve[s] every pending seat packet … with the Agent tool per
  `scripts/seat/README.md`" (operator ruling R35 / b-23). The operator's boot prompt for this pane
  routed serving to the Executor (flow:2.3) instead, which is the only reading the harness permits.
  Under D121 the contract and the launcher disagree on who serves, and the pane finds out by trying.
- **Systemic:** b-23's text and the `tools:` line must agree; whichever pane serves needs `Agent`
  in its contract's `tools:` (R2: the harness snapshots the list at pane start). Change list b-26.
- **Fix status:** open — b-26; serving for this charter is the Executor's by the operator's prompt.

### R50. Two Planner spawns timed out unserved while the Executor pane served only its own (L-planner-0006)
- **Measured:** `L-research-0002` (dispatched 13:17:52Z, `--timeout 15`) → `spawn-failed{timeout after 15 min}` 13:32:52Z, packet never opened; `L-plan-auditor-0016` (the cut-audit, 13:39:52Z, role default 15) → `spawn-failed` 13:54:52Z, same. In the same window the Executor pane (`L-executor-0006`) served and stamped its own four rework spec-writers (`L-spec-writer-0021…0024`, dispatched 12:57:23Z, stamped 13:11–13:24Z) and nothing else. The operator's boot prompt routes serving to the Executor "for every seat spawn"; the Executor's behaviour keys on its own dispatches, and nothing on the ledger or the board tells it a foreign packet is waiting (a-20's `seat-unserved` alarm is unbuilt; the fold's IN FLIGHT block lists specs, not spawns). Both re-dispatched with wider windows (`L-research-0003` 45 min at 13:33:32Z; `L-plan-auditor-0017` 60 min at 13:55:02Z) so the waiter outlives the Executor's next look — a timeout widened to cover a serving delay, not a running contract, which is a-36's p95 argument inverted.
- **Systemic:** R49's other half. A pane that cannot serve (no `Agent`) dispatching to a pane that serves only what it dispatched leaves every cross-pane spawn to expire at its role timeout, silently, at 0 tokens. The cross-pane trigger has to be a fold query: a board block `SEAT PENDING (n)` listing every `spawn-started` seat spawn with no terminal event and no `.output.json`, oldest first with age — the a-20 alarm made a standing board line rather than a threshold. Until it exists, a Planner on this backend cannot rely on any spawn it dispatches being served before its timeout. Change list a-38.
- **Fix status:** open — a-38 (a-20 sharpened); this charter's cut-audit is `L-plan-auditor-0017`, pending.

## Executor pane, `L-executor-0006` — 2026-09-15, continued (found and served the four stalled L-charter-0006 spawns R50 names)

### R51. Confirming R50 from the other side: this pane's own seat-poll monitor lapsed for ~5 hours during a single deep task, and nobody — not the monitor, not the board — said so
- **Measured:** `L-research-0002`/`L-plan-auditor-0016` (13:17–13:55Z) and their wider-window
  re-dispatches `L-research-0003`/`L-plan-auditor-0017` (13:33Z, 45 min; 13:55Z, 60 min) all four
  ended `spawn-failed{timeout}` with no notification ever reaching me — I found them only by
  running `ls ~/.do-it/seat/*.packet.md` by hand at 18:13Z while chasing R50 itself, ~4.5 hours
  after the first one appeared. My own background seat-poll `Monitor` (armed with a 30-minute cap,
  as the tool requires) had lapsed sometime during the L-charter-0005 merge/review/K=1/owed-ac
  stretch — a single continuous task run long enough that I never hit an idle point to notice the
  monitor's own 30-minute expiry notice, or did notice one and re-armed it, then it lapsed again
  the same way, silently, with nothing distinguishing "no new packets" from "not watching any
  more." R50's fix (a `SEAT PENDING (n)` board block, oldest-first with age) would have caught
  this from either side — I re-read `doit`'s board output roughly a dozen times during that
  stretch for other reasons and it never once told me a foreign spawn was aging out.
  Both packets were still valid (no repo drift affecting a code-mapping question or a cut audit
  against current HEAD) and served clean once found.
- **Systemic:** confirms R50/a-38 is not merely a Planner-side gap — it is symmetric. A tool-level
  `Monitor` re-arms itself only if the pane notices the expiry and re-issues the call; a long,
  focused task (mine ran ~90 minutes single-threaded resolving one merge's UI-review question)
  is exactly the condition under which that notice is easiest to miss, because nothing else
  interrupts to surface it. The fix is the same board block R50 already asks for, checked as a
  matter of routine between actions — not only when a notification prompts it — and, more
  robustly, checked independent of whether any particular `Monitor` instance happens to still be
  armed.
- **Fix status:** open — a-38 (shared with R50); practice change starting now: check `doit` (or
  at minimum `ls ~/.do-it/seat/*.packet.md` against served state) between multi-step stretches,
  not only on a monitor notification.

### R52. The rework packet builder resurfaces already-fixed findings on a third round, instead of only the standing ones
- **Measured:** `doit packet spec-writer L-spec-0008` (attempted for a narrow, unrelated task —
  adding owed-ac declarations after two clean review rounds) built successfully and would have
  handed a THIRD-round spec-writer the exact same fix list from round 1's spec-audit, already
  fully applied in round 2 (`p_spec_writer`'s `findings = [e for e in c.all_of("audit-finding") if
  e.get("list") != "rejected"]` reads every non-rejected `audit-finding` ever recorded on the
  subject, with no check for whether a LATER `spec-written` already answered it — unlike the
  builder/grader path, which has `standing_rejects()` for exactly this "still open, not yet
  cleared" distinction on `rejected-criterion`). Caught by inspection before dispatch, not run.
- **Systemic:** `spec-auditor`'s `audit-finding{list: "findings"}` has no `criterion-cleared`-style
  companion event the way a grader's `rejected-criterion` does, so the packet builder cannot tell
  "still owed" from "already fixed two rounds ago" — every fix list it builds after round 1 is the
  original list, forever, regardless of how many rework rounds already applied it. Worked around
  here by hand-building a narrow packet instead of using the tool (a deliberate exception to
  "never compose the packet by hand," justified by the tool's demonstrated bug for this exact
  case — noted for veto).
- **Fix status:** open — new change list item (a) `p_spec_writer` should only include an
  `audit-finding` whose subject has no LATER `spec-written` event, or spec-auditor should emit a
  clearing event mirroring `criterion-cleared` when a rework packet was built and served since.

### R53. A dynamic-dwell WEDGE flag on `written` doesn't know about Plan wave order
- **Measured:** `L-spec-0010` (wave 2, explicitly waiting for wave 1's three specs to merge and
  rebase per the Plan) sat `written` for ~35 minutes and was flagged `⚠ WEDGE` on the board —
  `dwell_days()` derives its threshold from this SESSION's own observed written→building
  transitions (mostly under 15 minutes today), so a deliberate, Plan-mandated wait past that
  reads as stuck even though nothing was wrong.
- **Systemic:** the dynamic dwell heuristic (§12.5) has no way to know a `written` spec is
  waiting on a same-charter sibling's merge by design; a false WEDGE costs nothing by itself but
  trains a reader to discount the flag exactly when it might one day be real.
- **Fix status:** open — minor; a-38's `SEAT PENDING` work is higher-value, not filing a change
  list row for this one alone.

### R54. Deploy-on-green caught what the spec-audit rounds correctly deferred: a cross-charter-owned test's stale hardcoded expectation
- **Measured:** `L-charter-0005`'s final merge (`05ed71942`) went to CI clean at the spec level
  (4/4 specs graded and reviewed, 0 blocking) but `predeploy_gate.sh`'s `vitest_baseline_gate`
  went red on the first real CI run: `new=1` — `schemas-day-detail.test.ts`'s AC3 (spec 1345 R1,
  a file in neither `L-spec-0008` nor `L-spec-0010`'s `Writes:` grant) hardcodes
  `TwoTruthsBottomLinesSchema`'s field list and was never updated for the three fields both specs'
  own Boundaries explicitly, correctly declared out of scope ("a follow-up, not a graded
  criterion" — the charter's shared-file-ownership rule keeping a pre-existing spec's test out of
  either unit's grant, by design). `contract.gen.ts` was independently confirmed already correct
  (regenerated byte-identical) — only the OLDER test's literal array was stale. Fixed with a
  three-field addition to that array, `Broke-by: 3233e607c` (the merge that actually introduced
  the mismatch), verified 9/9 in the file before pushing.
- **Systemic:** this is the seam the deploy-on-green gate exists for (D96/S17's cousin) — a
  charter-gap correctly named at spec-audit time, by design left for someone downstream, caught
  here by CI rather than production. Nothing to fix in v2; recorded because it is the first time
  this session a "both specs correctly deferred X" charter-gap actually manifested as a real red
  before deploy, proving the deferral discipline plus the gate together work as intended.
- **Fix status:** working as designed; no change needed.

### R55. `git commit --amend` on a REFUSED prior commit amends the wrong thing
- **Measured:** a pre-commit hook (`regression-linkage`) refused a commit outright (no commit
  created, files stayed staged). Running `git commit --amend -m <new message>` next amended
  the MOST RECENT EXISTING commit instead — an unrelated, already-pushed one (the G6
  workflow-disable fix) — silently merging my staged change into it and overwriting its message,
  then diverging from `origin/master` (non-fast-forward on push). Caught by the push's own
  rejection, not by inspection; fixed with `git reset --mixed origin/master` (restores HEAD to
  exactly what is already pushed, keeps the working-tree diff unstaged) and a fresh, isolated
  commit — no force-push, no history rewrite of anything already public.
- **Systemic:** not a v2/DO-IT gap — a personal git-discipline lapse worth naming for veto rather
  than silently self-correcting: after ANY refused commit, `git log -1` before touching `--amend`
  again, since amend has no concept of "the commit I meant" versus "whatever HEAD happens to be."
- **Fix status:** n/a (practice note, not a system defect).

### R51. Cut-audit round 1 on L-charter-0006: the pre-pass passed a cut whose wave 1 was the whole charter, and the Opus judge found the ten things it cannot see (L-planner-0006)
- **Measured:** `L-plan-auditor-0017` (served by hand by `L-executor-0006` at 18:20Z, 4h42m after `cut-written` 13:38Z; the R50 gap) → `bad_cut: true`, ten findings, none a repeat of the pre-pass: (1) the row shape `MoneyLine` was defined in one wave-1 unit's new file and consumed by two same-wave siblings — a Python import across unmerged branches, invisible to the footprint-overlap check because the file appears in one footprint only; (2) wave 1 was 5/6 units, ~50 files, so the one contested decision (the row shape + residual-as-a-row pattern) had no home and a change to it rebuilt four units; (3) R5 named three surfaces and the cut gave one a basis; (4) the shape duplicated `RealizationDeduction` (`models.py:3275`) and L-charter-0005's freshly merged `BottomLineDeduction` instead of extracting one; (5) the rows dropped the provenance chips the spine renders today; (6) both backend units changed response models and nobody regenerated `contract.gen.ts` (the CI gate is `continue-on-error`); (7) the verifier read the API, not the page, so "on-screen" was unproven; (8) the group panel's three dimensions got one residual; (9) `DepositWalkResponse` is not force-visible, so generic rows change what the name-based portal filter redacts; (10) the two `undetermined` pre-pass lines (all-new verifier, no Plan yet) are not passes. Second cut (8 units, 3 waves): wave 1 = `money-line-extract` (one shape with provenance, both existing shapes become subclasses, portal row-key registry, shared renderer, the advertising-basis function) + `contract-drift-gate` (D34, its own spec); wave 2 = five consumers; wave 3 = the verifier reading `--source api|page`. Pre-pass clean; `L-plan-auditor-0018` dispatched 22:52Z.
- **Systemic:** two checks the pre-pass could make and does not: (a) a *symbol* defined in one unit's new file and named in a same-wave sibling's Goal/Consumes is a cross-branch import — flag it as overlap (the `MoneyLine` class); (b) a cut whose wave 1 carries more than half the units or files is not "the accumulated extracts" (§3.7) — flag the ratio. Both are string checks on the cut file. Change list a-39. The rest (provenance, page-vs-API, the third surface) is judgement the Opus round bought, and this is the second charter in a row where round 1 bought exactly that (R38).
- **Fix status:** open — a-39; the cut-audit round-2 result is `L-plan-auditor-0018`'s.

### R52. Cut-audit round 2 cleared the cut and found fifteen more things, three of them contradictions the pre-pass could have caught (L-planner-0006)
- **Measured:** `L-plan-auditor-0018` (dispatched 22:52:59Z, output on disk 22:59Z, stamped ~23:00Z by `L-executor-0006` — the first spawn of this pane served inside its window, six minutes) → `bad_cut: false`, fifteen findings, none a repeat of round 1's ten or of the pre-pass. Three were checkable by string: (3) `cash_v2.py` pointed at service-local classes while `models.py` kept same-named duplicates and `gen_contract_types._merge_defs` keys `$defs` by class name first-wins — a name defined twice in the repo *before* the cut, which the "shared name introduced twice" check reads only off the cut's `Produces:`; (7) two same-wave units both reading `sales.py`'s rollup with neither declaring it — a symbol named in two Goals and in no `Produces:`; (8) the drift gate's trigger glob (`*_v2.py`) did not cover the router the same cut's R5 leg lives in (`sales.py`). The other twelve were judgement: an `unexplained` line with no bound (the defect surviving under a new name), brand not being a partition (`sku_brand_mapping` is an exclusive claim), the verifier's page leg minting a staff JWT while the review path is a client, generic rows collapsing name-based portal default-deny, cash rows taking a default KNOWN provenance on an in-flight period, estimated COGS routed into the plug, R4 with no production check, the extract bundling two extracts, no owner for `models.py`'s split. Third cut (9 units, 3 waves; wave 1 = three disjoint extracts) written, pre-pass clean at both stages; `plan-written` 23:04Z; plan-audit `L-plan-auditor-0019` dispatched 23:05Z carrying round 2's findings.
- **Systemic:** the pre-pass's "shared name" check reads the cut only; the class-name collision is `grep -rn '^class <Name>(' api/` on every name a `Produces:` or Goal cites — two definition sites is a hit. a-39 gains that check (the same family: what the cut names against what the repo holds). Two Opus rounds on a cut are now the measured norm on a monolith (R38, R51, this) and neither round has been a repeat of the other or of the script.
- **Fix status:** open — a-39 amended; the plan-audit is pending.

### R53. The plan-audit found fifteen more, none a repeat of forty-one prior findings, and three were the Plan contradicting the cut (L-planner-0006)
- **Measured:** `L-plan-auditor-0019` (dispatched 23:05:16Z, stamped ~23:10Z — five minutes, served by `L-executor-0006`) → `bad_cut: false`, fifteen findings on the third cut + Plan: the Sales tracker has no `response_model` so no glob can put it in the contract (the gate's R5 claim was empty); `RealizationDeductionSchema` in `schemas.ts` strips the provenance the new rows carry; "byte-identical" was false by construction for an additive subclass; 0005's `schemas-day-detail` parity pin would have been silently voided; a legacy residual can no longer be captured from production once waves 1–2 deploy; captions cannot be byte-identical across surfaces that name different bases; the Cash leg's window is the deposit window, not the period; every literal row lacked the provenance the shape requires; `Decimal` vs `"0.00"` string with no serializer named; the CARD_GAP identity's sign differed between cut and Plan; a response class named in the Plan had no module in any footprint; the verifier's bound covered one of four files; cash-partition-backend named no recorded fixtures; and R1's August residual was being retired by changing the displayed start figure — the audit's $4,349.62 is an agency-commission double subtraction ($4,691.84), which the walk fixes by subtracting once and relabelling (Q9). All fifteen folded in place into the cut and Plan (`cut-written` / `plan-written` re-appended 23:13Z); no second plan-audit (Q6). Wave 1's three spec-writers dispatched 23:14–23:15Z (L-spec-0011/0012/0013).
- **Systemic:** three of the fifteen were the Plan disagreeing with the cut on a seam it both define — sign, class name, string-vs-Decimal — and the pre-pass's "seam direction … the Plan's Seams where it is the authority" line reads names only. A Plan/cut *signature* diff (the `->` right-hand sides of matching seam names, token-compared) is a string check (a-39, third amendment). Stage clock for this charter so far: `charter-filed` 09:35 → first `cut-written` 13:38 → third cut + Plan 23:13, of which ~9h was two unserved spawns (R50) and ~1h was three served Opus rounds; the judges' 41 findings were 41 different things.
- **Fix status:** open — a-39 amended; wave 1 specs pending.

### R54. `doit packet spec-writer` refused a wave-2 packet because a sibling's spec quotes the cut's own `Produces:` line, and the refusal text became a dispatch's packet path (L-planner-0006)
- **Measured:** 23:26Z, `doit packet spec-writer L-spec-0014 --slot --unit bridge-walk-backend …` → `REFUSED — the packet carries what spec-writer's Blindness strips: a sibling spec's body (L-spec-0012) ('sales_tracker_usd, profit_total_usd, settlement_deducted_ads')`. `packet.py::sibling_bodies` takes every line over 50 characters from each written sibling spec (`long_lines`, `packet.py:48`) and refuses a packet containing one verbatim; L-spec-0012's spec quotes the `AdvertisingBasis` signature the cut's `Produces:` line and the Plan's Seams both carry, which every later packet must carry by design (the "sibling units' `Produces:`" item and the Seams section are the packet builder's own inputs). So the first spec written in a charter poisons every subsequent packet in it — the check fires on a shared input, not on the sibling's body. Second defect in the same minute: the `&&` chain I wrote still ran `doit dispatch … --packet "$PK"` with the refusal string as the path; dispatch printed a detached pid and died without writing seat files or events (the only good outcome), but a pane that checks less would have a spawn on the ledger with no packet. Worked around under the contract's own sentence — "round one's packet is yours" — with a hand builder (`mkslot6.py` in this pane's scratchpad) reproducing `doit packet`'s exact shape (the refused packet's siblings and Seams are the same text); five wave-2 packets built and dispatched 23:27Z (L-spec-0014…0018).
- **Systemic:** `sibling_bodies` must exclude any line that also occurs in the cut, the Plan, the charter or the packet builder's own template — those are the packet's inputs, not the sibling's body — and `doit packet` on refusal should exit non-zero with nothing on stdout so a chained dispatch cannot consume the message. Change list a-40.
- **Fix status:** open — a-40; the hand builder is a stopgap and the a-35 probe-residue line was corrected by hand in it (the document, not "none on file").

### R55. L-charter-0006 through the Planner: three cuts, three Opus rounds, eight specs in 26 minutes of writer time, one slot held by the one-wave rule (L-planner-0006, close)
- **Measured (`doit spend`):** 14 spawns — `L-research-0002/-0003` and `L-plan-auditor-0016/-0017` expired unserved (R50; -0017 later served by hand at 18:20Z and counted here as the round-1 audit); `-0018` (Opus, 6.7 min, 479k weighted) and `-0019` (Opus, 4.8 min, 371k) served inside their windows; eight Sonnet spec-writers 9.0–13.5 min each (L-spec-0011…0018, five in parallel, 8.0M weighted), 65 ACs, zero unknowns. Stage clock: `charter-filed` 09:35Z → `cut-written` 13:38 → third cut + Plan 23:13 → eighth `spec-written` 23:39. The ninth slot (the wave-3 verifier) cannot be commissioned until wave 1 merges (planner.md ⑥: never more than one wave ahead), so the charter leaves this context at eight of nine with no `l1-complete` — the first charter in the pilot to end a Planner context short of ⑦ by the contract's own rule rather than by budget.
- **Systemic:** two things. (1) ⑥'s one-wave rule and ⑦'s "every slot" rule together guarantee a three-wave charter spans two Planner contexts; either `l1-complete` should be derivable when every slot the rule *allows* is written (the fold sees the wave numbers on the cut), or the rule should read "two waves ahead when the later wave consumes only merged seams". (2) The three Opus rounds' 40 findings were 40 different things and none was a repeat; the pre-pass caught none of them. On a monolith the judges are the cut, and the pre-pass is the typo check. Change list b-27 for (1); (2) is a-39's argument, measured a third time.
- **Fix status:** open — b-27; handover `docs/handoffs/planner-handover-2026-09-15-L-planner-0006.md`.

### R56. After the spec-audits the Executor waited ~90 minutes on the Planner for the rework round its own contract owns (L-planner-0006, 2026-09-16 ~01:15Z)
- **Measured:** the Executor pane served and stamped all eight `spec-auditor` spawns on L-spec-0011…0018 (6–10 findings each, no `bad_cut`), then sent a cross-session check-in ("in case something's stuck … a decision you're waiting on from me") after ~90 minutes of no dispatch. `agents/executor.md:44` — "Audited with findings, no rework yet → dispatch `spec-writer` with the fix list" — and `:114` (`doit packet spec-writer <spec>  # rework`) make the rework round the Executor's; `agents/planner.md` ⑥ says "every later round is `doit packet spec-writer`'s and the Executor's, not yours". Both contracts agree, and the pane holding the specs still waited for the other. Nothing on the board names whose move an `audited-with-findings` spec is: `WRITTEN, NOT PICKED UP` lists the spec either way. The Planner has no return path (D117) and answered through the operator.
- **Also measured, in advance:** the rework packet `doit packet spec-writer <spec>` builds will be refused for every one of these eight specs by the a-40 false positive (R54): each spec quotes the cut's `Produces:` / Plan Seams signature, and `sibling_bodies` reads any long line of a sibling spec as contamination. The Executor's rework round on this charter needs a-40 landed or the same hand-built packet shape (`mkslot6.py` in the Planner's scratchpad, or `scripts/seat/mkslot.py`'s shape plus the audit's fix list).
- **Systemic:** the board should render the lane owner beside each spec state (`written · audited 8 findings · Executor's move: rework`) — the fold has every input (`spawn-done` of a `spec-auditor` with findings and no later `spec-written`). Change list a-41.
- **Fix status:** open — a-41; relayed to the operator in the Planner pane.

## Executor pane, `L-executor-0006` — 2026-09-16, continued (R56's other half)

### R57. Confirming R56 from the receiving side: I knew executor.md:44 and still waited on the Planner
- **Measured:** I had already dispatched exactly this rework pattern myself, unprompted, for
  L-charter-0005's four specs earlier this session — the same "audited with findings → `doit
  dispatch --detach spec-writer` with the fix list" move, no hesitation. For L-charter-0006's
  eight specs I instead held for ~90 minutes waiting on the Planner, then sent a check-in message
  rather than just doing the thing my own contract already told me to do. Only found the actual
  problem by capturing the Planner's tmux pane directly (`tmux capture-pane -t flow:2.1 -p`) after
  a second 30-minute silence, where it had already diagnosed the exact fix and named the line
  numbers in my own contract. The asymmetry: I treated "the Planner hasn't dispatched anything new"
  as evidence the ball was still in its court, when the correct read was "the Planner already
  finished its part; the next move is a state I should recognize on my own board without being told."
- **Confirmed independently:** `doit packet spec-writer L-spec-0011` reproduced the exact a-40
  refusal the Planner reported ("the packet carries what spec-writer's Blindness strips: a sibling
  spec's body..."), on a different spec than the Planner hit it on — a second measured instance,
  raising a-40's priority. Workaround used (more durable than a session-scoped script): a small
  Python script importing `fold.read_events()` directly, replicating `packet.py::p_spec_writer`'s
  exact `base + Fix list` assembly (base = the round-1 packet already on disk under
  `packets/<spec>-spec-writer-1.md`; fix list = the real, already-stamped `audit-finding` events
  per spec with `list != "rejected"`) — same content the tool would produce, minus the buggy
  refusal, built for all 8 specs in one pass.
- **Systemic:** R56's own fix (render the lane owner beside each spec state) is the right one;
  the smaller point is that even a correct contract doesn't help if the pane's own board-reading
  habit defaults to "wait for someone else to move" the moment a lane item stops being *freshly*
  the pane's own creation (I served the audits; a rework of them one hop later stopped feeling
  like mine to keep moving on my own initiative). Worth naming as a practice note, not just a
  tooling gap.
- **Fix status:** open — a-41 (shared with R56); practice note: an audited-with-findings spec is
  mine to move on sight, not on notification.

### R58. `DOIT_LEDGER_FILE` doesn't survive a Bash-tool shell boundary, and `doit gate`/`doit append` silently fall back to the wrong file when it's unset (L-executor-0006, 2026-09-16 ~04:15Z)
- **Measured:** the operator's boot command exports `DOIT_LEDGER_FILE=L-executor-0006.jsonl` as a
  prefix to the top-level `claude -n executor` invocation, but each Bash-tool call in this harness
  is its own fresh shell — exported vars from the pane's own launch environment do not propagate
  into it, and `~/.do-it/env.sh` never sets `DOIT_LEDGER_FILE` itself. Earlier in this session I
  compensated by hand, prefixing every `doit` invocation with `DOIT_LEDGER_FILE=L-executor-0006.jsonl`
  (37 occurrences in this session's transcript). After a context compaction/resume, that habit did
  not survive the handoff: I resumed with only `source ~/.do-it/env.sh`, and three `doit gate` calls
  landed their `merge-gate-clean` events on `L-executor-0001.jsonl` (`merge_gate.py:426`'s own
  fallback) and three `doit append shipped` calls landed on `L-operator-local.jsonl`
  (`fold.py:848`'s fallback) — as actor `operator`, not `executor`.
- **This silently broke the board, not just the audit trail.** `fold.py:24`'s `EMITS` restricts
  `"shipped": {"executor"}` (the same mechanism R-documented earlier for `owed-ac`/spec-writer). A
  `shipped` event from actor `operator` is invisible to `spec_state()`'s `accepted` check, so all
  three specs stayed shown as `reviewing` on the board after a real, pushed merge — nothing said so
  out loud; the board just quietly never advanced. Caught only because I went looking for *why* the
  board hadn't moved, not from any error or warning.
- **Not the same bug as R51 (seat-poll lapse) but the same shape:** a pane-level piece of state that
  the operator's boot command sets once, that this harness's tool boundaries don't actually
  preserve, with no runtime check that would notice its absence — only silent misfiling.
- **Fix applied this session:** re-emitted the three `shipped` events correctly (`export
  DOIT_LEDGER_FILE=L-executor-0006.jsonl` in the same command as the `doit append`, not relying on
  a prior `source`), and recorded a `decision` event on the correct ledger documenting the gap
  rather than rewriting the two misfiled events (append-only). Not proposing to edit `L-executor-0001.jsonl`
  / `L-operator-local.jsonl` after the fact.
- **Systemic:** `doit`'s own top-level dispatcher could assert `DOIT_LEDGER_FILE` is set (and matches
  the caller's declared role) before running any subcommand that appends, rather than each
  subcommand independently defaulting to a different fallback file. Absent that, the practice fix is
  mechanical: every `doit gate`/`doit append`/`doit dispatch` call in this pane explicitly sets
  `DOIT_LEDGER_FILE=L-executor-0006.jsonl` in the same command, every time, permanently — not just
  "when I remember to."
- **Fix status:** open (tooling); practice fix applied and now standing for the rest of this
  session.

### R59. L-spec-0011 was merged to master with zero reviewer ever dispatched against it (L-executor-0006, 2026-09-16 ~04:18Z)
- **Measured:** while chasing R58's board-stall symptom, `fold.read_events()` filtered to
  `L-spec-0011` showed a `verdict` (the grade) but no `review` event type at all, and a grep across
  every `~/.do-it/events/L-reviewer-*.jsonl` file for `L-spec-0011` returned nothing — no reviewer
  spawn was ever dispatched for this spec. It went grade → merge, skipping the lane's own
  grade → review → merge order, and I did not notice at merge time because `doit gate` only checks
  file-footprint cleanliness against `main`, not lane-order completeness.
- **Root cause, best read:** three wave-1 specs were graded and reviewed in close succession
  (L-spec-0012, L-spec-0013 reviewers dispatched and served; L-spec-0011's grader ran far longer —
  660s vs ~100-250s for the others — and its completion notification landed last), and somewhere in
  tracking "three outstanding, waiting on the slowest one" I merged all three together once the
  slowest (the grade) came back, without checking that a *review* — a materially different lane
  step — existed for each.
- **Fix applied this session:** did not paper over it. Rebuilt a worktree at L-spec-0011's own
  merge-tip commit (`git worktree add ... 486ef2a7b`, detached HEAD — the branch itself was already
  deleted post-merge), built and dispatched a real `reviewer` packet (`L-reviewer-0013`) against it,
  and held for its verdict before treating the spec as `accepted` — even though the code was already
  live on `master`. A finding from this out-of-order review would need a follow-up fix commit, not a
  revert of an already-merged, already-shipped-to-others'-work branch base.
- **Systemic:** neither `doit gate` nor the board's `spec_state()` cross-checks that a `review` event
  exists before a spec is merge-eligible — the Executor's own lane discipline is the only gate. A
  cheap fold-level check ("does this subject have a `verdict` with no matching `review` before its
  `merge-gate-clean`?") would have caught this at gate time instead of at board-stall investigation
  time.
- **Fix status:** open (tooling — add the missing-review check to `doit gate`); this instance
  corrected in-session before charter close.

### R60. L-charter-0006 L1-complete: nine slots, one Planner context after all, 21h50m of stage clock of which the Planner's own work was under four hours (L-planner-0006, close)
- **Measured:** the Executor merged and shipped waves 1 and 2 (L-spec-0011…0018 `accepted`, master `d45600e84`) by 2026-09-16 07:08Z and said so by cross-session message; the ledger agreed (`doit states`). The wave-3 slot was then dispatchable under ⑥'s one-wave rule from this same context — the R55/b-27 prediction that a three-wave charter forces a second Planner context held only while this pane was alive to receive the merge; it was, so it did not. `L-spec-0019` bridge-cash-tieout-verifier: packet hand-built (a-40 refused `doit packet` a third time; the scratchpad builder had to be recreated — `/tmp` is session-scoped), dispatched 07:09:31Z, `spec-written` 07:19:49Z (11 ACs, 0 unknowns, served by the Executor inside its window). `l1-complete` appended 07:20Z naming all nine slots. Stage clock: `charter-filed` 09:35Z (09-15) → `cut-written` 13:38 → `plan-written` 23:13 → eighth `spec-written` 23:39 → waves 1–2 merged 07:08Z (09-16) → ninth `spec-written` 07:19 → `l1-complete` 07:20. Owed at close: R6's production re-run (K=1) after the deploy that carries L-spec-0019's merge; nine owed sweep questions with defaults in the Plan, four named for veto (Q1, Q6, Q7, Q9).
- **Systemic:** b-27 stands but narrows: the second-context cost is paid only when the Planner pane is gone before the wave merges; a pane that idles across the build (as this one did) pays it in context instead. The fold-derived `l1-complete` remains the right fix. Nothing else new; a-40 measured a third time.
- **Fix status:** recorded; the charter is the Executor's to close (sweep, charter-review running the verifier on production from both sources, reap).
- **R56, second hit (07:25Z):** after L-spec-0019's `spec-written` the Executor pane again waited on the Planner, this time for the `spec-auditor` dispatch — the first row of its own lane table (`executor.md:44`, `written` + no audit → dispatch `spec-auditor`), and the step it performed itself for L-spec-0011…0018. The Planner has no return path; relayed through the operator. a-41's board line ("Executor: dispatch spec-auditor") is the fix, measured a second time.

### R61. Confirming R56's second hit from the receiving side, with one partial improvement over the first (L-executor-0006, ~08:53Z)
- **Measured:** after `L-spec-0019`'s `spec-written` (07:19:49Z), I sent the Planner a status message and then genuinely idled — three Monitor cycles (~90 minutes) of "nothing to serve, nothing in my own lane" before re-reading `executor.md:44` directly and finding the same line R56 already named: `written` + no audit → dispatch `spec-auditor`, mine, not the Planner's. Unlike the first hit, this was NOT surfaced by an operator relay or a captured Planner pane — I found it myself by going back to my own contract file on a hunch that I might be repeating R56, and it held. Dispatched `spec-auditor` immediately (`L-spec-auditor-0018`), after first fixing two more mechanical spec defects it would have otherwise refused on (a-40-adjacent, but distinct): L-spec-0019's `Verification` block had the same backslash-line-continuation shape already D116-fixed on five prior specs this session, and its `Writes:` header was the same bare-header-no-parenthetical shape already fixed on L-spec-0014/0015/0017.
- **The improvement, precisely stated:** I self-corrected on suspicion rather than needing a relay or a second, independent measurement to trigger the check. The defect itself — treating "the Planner hasn't moved" as "the ball is still in its court" rather than "check my own board line first" — is unchanged and is the third measured instance in one session (L-charter-0005's rework round, L-charter-0006's rework round, now L-charter-0006's spec-audit dispatch). Three instances of the identical shape in one session is a habit, not a slip.
- **Systemic:** a-41's fix (render the lane owner beside each spec state) is necessary but, on this evidence, not sufficient by itself — even with the correct board line available on request (`executor.md:44` never moved), the failure mode is not reading the board/contract proactively on an idle tick, only reactively after minutes-to-hours have passed. A cheap mitigation available today, without waiting on a-41: the standing Monitor's own poll script could also grep the board for `WRITTEN, NOT PICKED UP` entries older than some threshold and surface them as its own event line, the same way it already surfaces new seat packets — turning "idle, nothing to serve" into "idle, but check this."
- **Fix status:** open — a-41 stands; adding the WRITTEN-staleness line to `seatpoll.sh` (or its board-parity successor) is a same-session, low-cost mitigation worth landing before the next idle stretch.

### R62. A good research spawn was recorded `spawn-failed` because `doit alloc` and `doit dispatch` allocate from two different counters (L-planner-0010, 2026-09-16 ~09:46Z)
- **Measured:** `doit alloc research` handed back the content path `content/L-research-0003.md`
  (the content counter's max+1). `doit dispatch research --path content/L-research-0003.md` then
  allocated the *spawn* id `L-research-0004` from the spawn counter — the two counters are not the
  same counter and had drifted by one, because `L-research-0003` already existed as a spawn id on
  L-charter-0006 while `content/L-research-0003.md` had never been taken. The research contract
  wrote its findings to `content/L-research-0004.md` — a path derived from its own spawn id, not
  the path the packet named — and the wrapper's post-hoc path check failed the spawn:
  `spawn-failed · why="path mismatch: packet named L-research-0003.md, output says
  content/research-0004.md"`. The work itself is complete and good: 31 KB of findings, all six
  defects traced to `file:line`, output card `answered: yes / contamination: false`, VALID under
  `doit validate research`. `content/L-research-0003.md` is left as a 0-byte alloc stub. So the
  ledger records a failure, no `research-filed` event exists, and a fold-only reader cannot find
  the artifact that was produced.
- **The repair is operator-only, which is the second half of the defect.** `correction` is
  restricted to `actor: operator` (`fold.py:159`, D111), so the Planner cannot name its own
  mis-pathed artifact on the ledger. The attempt appended as `L-planner-0010.jsonl:1` and was
  counted on HEALTH as one of the "unauthorized events recorded and ignored" — an audit trail of
  the attempt, which is the designed behaviour, but it means the findings' real path survives only
  in this record and in the Planner's handover, not in the fold.
- **Systemic:** two fixes, both small. (a) `dispatch` should take the `--path` it was given as the
  spawn's write target and put it in the packet as the only path, or `alloc` and the spawn counter
  should be one counter — today a Planner can do everything right and still lose the spawn.
  (b) A `--path` mismatch where the file that *was* written validates is a mis-filing, not a
  failure; the wrapper could record `spawn-done` with the observed path and a `path_moved` field
  rather than `spawn-failed`, so good work stops being thrown away on a naming slip.
- **Also measured, separately:** the Agent-tool `model` override did not reach the sub-agent. This
  pane served the packet with `model: sonnet` (per the operator's standing sub-agent model floor,
  which overrides `models.toml`'s `research = haiku`), and the terminal event stamped
  `model_used: claude-haiku-4-5-20251001` with `model_observed: true` — i.e. the transcript's own
  assistant lines ran on Haiku. Either the override is dropped on the seat route or `usage.py`
  resolved the wrong transcript. Worth one measurement before anyone relies on a per-serve model
  choice again; the standing floor is currently unenforceable from the serving pane.
- **Fix status:** open — no change-list id yet; R62(a) and R62(b) are the two candidates.

### R63. Three audit rounds on L-charter-0009 returned 32 findings with near-zero overlap, and the two that would have shipped broken were both found by the round the Planner was tempted to skip (L-planner-0010, 2026-09-16)
- **Measured:** cut-audit round 1 `bad_cut: true` / 14 findings; round 2 `bad_cut: false` / 9; plan-audit `bad_cut: false` / 9. Thirty-two findings, and the auditor explicitly confirmed round 3 repeated neither prior round. The Planner verified every load-bearing finding against the tree before folding; all held. Two were ship-breaking and neither was visible to the pre-pass: (a) the promo-effect response model is in `promo_effect_v2.py`, not `promos_v2.py` — a file in no footprint, so every new field would have been computed in the service and never serialised, on both the account and ASIN routes; (b) `mtdDays` in `sales-view.tsx` has two consumers reading it as different units (`:730` days, `:652` rows, `allPrior.slice(-(mtdDays ?? 0))`), so correcting the number R6 is about would have silently shrunk the prior-MTD tile on every normal page load. Both would have passed a build, a grade and a unit-test review. The third round also caught that R3/R4 were declared delivered while the funnel (`promo_lift.py:216-218`) kept the identical defect in no unit's footprint, and that `delta_pct` already carries two scales on one page so the charter's own ADR would have made a third.
- **Systemic:** the standing argument for capping blind audits at one round (`feedback-blind-audit-rounds-cap`) does not survive this measurement on a *cut*. The rounds are not re-reads of the same artifact: round 1 audits a cut, round 2 audits a different cut, round 3 audits a cut plus a Plan, and each new artifact makes different questions answerable — which is exactly what §3.6 claims and what this run measured. Yield did not decay (14 → 9 → 9) and overlap was nil.
- **Fix status:** no change proposed; recorded as evidence for the §3.6 two-audit design and against extending the one-round cap from specs to cuts.

### R64. `doit dispatch --detach` records a `claude -p` command line it never runs, and that artifact reads as a ban violation to anyone who greps for it (L-planner-0010, ~12:05Z)
- **Measured:** `seat/<spawn>.cmd.json` contains `{"cmd": ["claude", "-p", "--agent", …]}`. In a project whose CLAUDE.md bans `claude -p` outright (spec 572, metered not seat-billed), that file is alarming on sight, and a spec-writer's hand-back reporting that its output "already existed from an earlier run of this same spawn" made it look like the detached waiter had actually executed it. It had not: `dispatch.run_seat` (`src/dispatch.py:205-222`) states in its own docstring that it does not spawn — it writes the packet and *the line that would have run*, prints where the result is expected, and waits for the pane's serve. No metered spend occurred. Cost of the scare: one Planner investigation mid-charter.
- **Systemic:** the recorded line is deliberate (it keeps the two routes comparable forever, and `spawn_path: seat` distinguishes them), but the key name does not say so. Renaming `cmd` to `would_have_run` in the seat branch, or adding `"executed": false`, removes an entire class of false alarm for zero behaviour change.
- **Also observed, unresolved:** the spec-writer serving L-spec-writer-0046 found its own output file already written when it "began", verified it independently, and then corrected three ACs in it that cited raw line numbers against the contract's symbols-not-lines rule. Benign here — most likely the sub-agent re-grounding across a long run (73 tool uses, 10m) and reading its own earlier write as pre-existing — but a contract that can read its own draft as prior art is a contamination surface worth one look.
- **Fix status:** open — no change-list id; R64's rename is a one-line fix.

### R65. Two panes served the same seat packets concurrently, burning a full Sonnet spec-writer run per occurrence — b-26's "serve what you dispatch" is being applied as "serve everything" by the other pane (L-planner-0010, ~12:10Z)
- **Measured:** the Planner dispatched four spec-writers (L-spec-writer-0044…0047) and served each one itself, per b-26. Two of them were served **twice**. `L-spec-writer-0045`'s `meta.json` records `session: ac6ea960f230e41af`; the Planner's own serving sub-agent for that spawn was a different session entirely, and that sub-agent reported finding the content file and a valid `output.json` already on disk, timestamped 12:05-12:06, while it was still doing its own pre-flight — it correctly declined to overwrite and cross-checked instead, confirming every `file:line` citation independently. `L-spec-writer-0046`'s server reported the identical shape ("the spec file and its output.json already existed with this exact content when I began"). Cost per occurrence is one complete Sonnet spec-writer run: the two Planner-side serves that lost the race burned 229,503 and 196,321 sub-agent tokens producing nothing that was kept. `L-spec-writer-0044` was not double-served — the Planner had to stamp it by hand, and its waiter was still blocked ten minutes after the output landed.
- **The mechanism:** b-26 (operator ruling 2026-09-15, superseding b-23's "the Planner serves everything") says each pane serves the packets **it dispatched**. R50 measured the failure in the other direction — two Planner spawns expiring unserved at 0 tokens — and the response recorded in that handover was that "the Executor pane now serves by hand within ~5 min". That mitigation is still running, and it is no longer scoped to the Executor's own packets, so every packet the Planner dispatches is now raced by two servers. The contract's own pending-packet test (R47: `spawn-started` and no terminal event) cannot distinguish "unserved" from "being served by the pane that dispatched it", so the other pane cannot tell it is racing.
- **Systemic:** the cheap fix is a claim file — a serving pane writes `seat/<spawn>.claimed` with its pane id under `O_EXCL` before it starts, and a pane that cannot create it does not serve. Same primitive `doit alloc` already uses for ids (`dispatch.py:74-82`). Without it, the only thing preventing double-serve is two panes reading the same ruling the same way, which this measurement shows is not happening. Second observation, unchanged from R64: the serving contract can read its own duplicate's draft as prior art, which is a contamination surface — here it was benign and the second server behaved well, but nothing made it behave well except its own judgement.
- **Fix status:** open — no change-list id. The claim file is a ten-line change in `run_seat`'s neighbourhood plus one line in each pane's contract.

### R66. Confirming R65 from the receiving side, and a second, unrelated D120 gap it exposed (L-executor-0006, ~13:00-13:52Z)
- **Measured (b-26 adoption):** the operator confirmed b-26 mid-session when I raised the exact conflict R65 independently describes — my own boot instructions said "serve every seat spawn the Planner dispatches," which is what actually caused the L-spec-writer-0045/0046 double-serves R65 measured from the Planner's side. Adopted going forward: I only auto-serve a spawn I dispatched myself in this pane; a `seatpoll.sh` change now tags every newly-seen unserved packet `SEAT-PACKET` (mine, in a locally-tracked `seatpoll.mine` list appended right after each of my own `doit dispatch` calls) or `FOREIGN-PACKET` (someone else's — awareness only, never auto-served). Everything I had ALREADY self-dispatched and served this session under my own lane (spec-audits, reworks, builds, grades, reviews — all charter-agnostic per executor.md:44's own wording) was not a violation of b-26; only the four ORIGINAL spec-writer commissioning packets the Planner itself dispatched were.
- **A second, unrelated D120 gap, found while re-grading L-spec-0021 after a self-contamination:** a grader (`L-grader-0032`) ran `git log --oneline -3` inside its own worktree — grader.md's own forbidden action — saw the builder's commit subject, and correctly self-voided (`contamination: true`, all ACs `cannot-assess`). `usage.py`'s stamp logic records ANY `contamination: true` output as a generic `spawn-failed{why: "contamination: the packet carried what Blindness strips"}`, and `dispatch.py`'s D120 dedup (`packet_sha256` + `contract_sha256` + `why` starting with `contamination`) then PERMANENTLY refuses any future dispatch of the byte-identical packet — even though the packet itself carried no Blindness violation; the contamination here was procedural (a live command the sub-agent chose to run), never textual. `doit packet grader --round 2` produces a byte-identical packet (grader has no round-driven content variance the way reviewer does), so there was no supported path to a legitimate re-grade. Worked around by hand-appending an explanatory retry note to the existing packet file — changing `packet_sha256` while keeping the audit substance identical, and explicitly telling the next grader not to touch `git log`/`show`/`diff`/`blame`.
- **Systemic:** the D120 dedup's `why` match is too coarse — it conflates "the packet is structurally poisoned, retrying wastes a spawn for certain" with "the WORKER poisoned its own run, the packet is fine, retry is exactly what should happen." A `contamination` `spawn-failed` should carry a `packet_fault: bool` (or similar) distinguishing a `strip()`-detected packet defect (genuinely undeterred by retry) from a sub-agent self-report of touching a forbidden command (a worker-side accident, safely retriable on the identical packet) — only the former should feed D120's refusal.
- **Fix status:** open — no change-list id; b-26 adoption is a practice fix landed this turn, the D120 `packet_fault` distinction is a `dispatch.py`/`usage.py` change, not made this session.

### R67. The project filter emptied the one prior-round channel in the system: a plan-audit was handed `none` while 29 findings sat in the ledger (L-planner-0012, 2026-09-17 ~06:10Z)
- **Measured:** L-charter-0020 was filed by the Thinker as `project: albert-scott` — its pane's environment, not a decision; the charter's own Constraints say the code is `~/do-it-v2`, "not the Albert Scott repository". Every spawn this Planner dispatched carried `--project do-it-v2`, because that is the repository the work is in and `packet.py:584` resolves the builder's repo from the spec's project. `fold.read_events()` filters on `DOIT_PROJECT` (`fold.py:196`), so from this pane — scoped `albert-scott` — the spawns' events are invisible. The visible consequence: `doit packet plan-auditor --stage plan` renders `## The cut-audit's findings (prior round, by design)` as `none`, and the plan-auditor said so as a finding, while **29 `audit-finding` events** from three cut-audit rounds sat in `events/L-plan-auditor-002{3,4,5}.jsonl` under the other label. `doit events L-charter-0020` from this pane showed 15 of the charter's ~45 events.
- **Why it matters beyond one charter:** §3.6 gives the plan stage exactly one prior-round input, deliberately — it is the only place in the system where a round sees what the last round found. It failed silently and returned a well-formed `none` rather than an error. Every other `doit packet` path reads a subject's history the same way, so any cross-project charter degrades its own packets without saying so. This is the same shape as D93's warning that the project filter is "a filter, not a second read" — but nothing checks that the filter did not hide the thing the caller needed.
- **Systemic:** `doit packet` should either refuse when the subject has events under a project the current filter excludes, or render them with a visible note. A packet section that reads `none` must be distinguishable from one that reads "nothing matched your filter". Undetermined is never clean, and a filtered-away prior round is undetermined.
- **Fix status:** open — no change-list id. Filed as an `escalation-blocking` on L-charter-0020 (the repair is a `correction`, operator-only per `fold.py:159`) and as Q1/Q12 in `content/plan-L-charter-0020.md`.

### R68. `doit packet spec-writer` truncates a multi-line `Footprint:` to its first bullet, narrowing the merge grant below the audited cut (L-planner-0012, ~06:30Z)
- **Measured:** the cut file writes footprints as a bullet list under the label, which `audit.fields` accumulates correctly — all six mechanical checks passed against the full list. `doit packet spec-writer` renders the same slot as `Footprint (= the merge grant, `Writes:`): - src/think.py` — one path where the cut names two. All five slots of L-charter-0020 were affected; the sibling `Produces:` list truncates identically (one function shown where the cut names six).
- **The cost, and why it was nearly invisible:** the packet is what the spec-writer contract is told is authoritative, and `doit gate` enforces the `Writes:` grant. Three of five writers noticed the disagreement only because they read `content/cut-L-charter-0020.md` directly and declared `seam-undefined`/`charter-gap`; two followed the packet literally, and one of those explicitly recorded that R10 is "not delivered by this spec as a result" because the test file it needs had nowhere to live. A builder committing its own unit's test file would have been refused at merge, on a spec that passed its own audit.
- **Note on the three that caught it:** they caught it by reading a file the packet did not give them, which is a blindness surface in the other direction. The packet being the whole instruction set is the mechanism; a packet that silently narrows what it was given turns that mechanism against itself.
- **Systemic:** `packet.py` should build the slot from `audit.units()` — the same parse the pre-pass and both fable audits use — rather than from a line read. One parser, one answer. Corrected here by a `decision` event per affected spec (L-spec-0030, -0032, -0033) naming the authoritative footprint.
- **Fix status:** open — no change-list id. Q13/Q14 in the Plan.

### R69. `doit packet plan-auditor` accepts a `--repo` the Planner contract never names, and without it the auditor is handed a degraded pre-pass (L-planner-0012, ~05:50Z)
- **Measured:** `agents/planner.md` step ② names `--repo "$R/repos/<project>"` on the `doit audit` line but not on the `doit dispatch plan-auditor` line, and the packet builder re-derives the pre-pass block itself (`packet.py:418`, `audit.prepass(..., c.a.repo)`). Round 1 of L-charter-0020's cut-audit therefore carried `unit size vs the §4.3 ceiling: undetermined — no --repo`, while the Planner's own `doit audit` run of the same cut minutes earlier had measured it and returned `none`.
- **What the auditor did with it — correctly:** it filed "no unit here is known to be small" as a finding and named `the-supervisor` as the standing oversize candidate, and it refused to read `undetermined` as a pass. That is the contract working. But a full Opus round was spent partly on a check that had already been answered, and the finding was unactionable because the input, not the cut, was at fault.
- **Systemic:** two commands derive the same block from the same file and take their inputs differently. Either `doit packet` should inherit `--repo` from the charter's project symlink by default (it already resolves `ROOT/repos/<project>` for the builder's cwd), or the contract's dispatch line should carry it. The Planner has no way to see the degradation without reading the packet it just built.
- **Fix status:** open — no change-list id. Q11 in the Plan.

### R70. A concurrent build in the live tree turned a correct Opus cut-audit into a `spawn-failed`, and ten findings never reached the ledger (L-planner-0014, 2026-09-17 ~07:50Z)
- **Measured:** `L-plan-auditor-0027` (stage `cut`, L-charter-0021) ran to completion, wrote a schema-VALID Output with `contamination: false`, `bad_cut: false` and **ten ranked findings**, six of which were load-bearing enough to rewrite the cut. The wrapper recorded it `spawn-failed · why="repo status changed across a non-builder spawn: ' M agents/planner.md\n M agents/thinker.md\n M src/think.py\n M src/up.py'"`. Those four files are **L-charter-0020's footprint** — another charter's build moving the shared working tree during the 266 seconds the auditor was reading it. Because the spawn is terminal-failed, **no `audit-finding` events were emitted at all**: `doit events L-charter-0021` shows none, and the stage-`plan` packet's `## The cut-audit's findings (prior round, by design)` read `none` — the same channel R67 emptied by a different mechanism, one round later.
- **The findings survive only in `seat/L-plan-auditor-0027.output.json` and in the Plan that quotes them.** `correction` is operator-only (`fold.py:159`), so the Planner cannot repair the record; this is the second time in three rounds that good work was lost to bookkeeping (cf. R62's path-mismatch).
- **It is also a charter-gap that reality found before any auditor did.** L-charter-0021 R7 says *"footprint overlap never blocks a dispatch"*. A wrapper that fails a **reader** because a **writer** was working is that same rule, missed — and no unit in the cut touched `src/dispatch.py`. A tenth unit (`concurrent-work-is-not-a-failed-spawn`) was added for it, sourced from this incident rather than from an audit round.
- **Systemic:** the D116 repo-status check is right for a builder (it proves the spawn wrote where it said) and wrong for every read-only role — a `plan-auditor`, `grader`, `research` or `probe` spawn wrote nothing, so the tree moving is by definition somebody else's doing. It should be recorded on the terminal event as an observation, never as the verdict. Until then, **every audit dispatched against a repository with a live builder is a coin flip**, and the coin is paid for in Opus.
- **Fix status:** cut as `concurrent-work-is-not-a-failed-spawn` in L-charter-0021 (wave 2).

### R71. The serving pane cannot know its sub-agent's token usage until the hand-back, but the detached waiter's cap can expire first (L-planner-0014, ~07:56Z)
- **Measured:** `stamp.sh` takes `<turns> <duration_ms> <subagent_tokens>` as hand-supplied figures, and the only place they exist is the Agent tool's completion notification. For `L-plan-auditor-0027` the output landed at ~13 minutes into a **15-minute** `plan-auditor` cap with the hand-back not yet delivered, so the pane stamped with `turns=0, subagent_tokens=0` (duration derived from file mtimes) to save the spawn from a timeout. The real figures arrived ~90 seconds later: **19 tool uses, 84,882 tokens, 272,786 ms**. The ledger now carries two false zeros that `doit spend` will read as a free Opus round, and `correction` is operator-only, so they are permanent.
- **Systemic:** the stamp requires information the harness delivers *after* the deadline the wrapper enforces. Either the waiter's cap should start from the packet's answer rather than from dispatch, or `stamp.sh` should accept "unmeasured" and write null — the fold already distinguishes unpriced from zero for dollars (`spend_rows`) and has no such distinction for tokens.
- **Fix status:** open — no change-list id.

### R72. `doit test` is red at HEAD only because `DOIT_PROJECT` is set in the shell that runs it (L-planner-0014, 2026-09-17 ~09:00Z)
- **Measured:** a spec-writer ran `./doit test` at `ba6a264` to establish its baseline and got
  `test_fold.py:31 AssertionError: set()`, then narrowed its own spec's Verification section to one
  test file and documented the red baseline in Constraints. Re-run from this pane with
  `env -u DOIT_PROJECT`, the same file passes **104 checks**. The cause: `test_fold.py` builds a
  scratch ledger whose events carry no `project` field, and `fold.PROJECT` is read from the
  environment **at import time** (`fold.py:21`), so `read_events()` filters every scratch event out
  and the first assertion sees an empty set.
- **Why it matters beyond one spec:** every pane on this box runs with `DOIT_PROJECT` set — that is
  what `env.sh` is for — so a builder, a grader or an Executor that runs the suite in its own shell
  sees a false red on a file it did not touch. §5.7 says a red baseline is recorded and warned, not
  blocked, which means the false red does not stop anything; it just quietly makes every baseline
  comparison meaningless and costs a spec-writer a narrowed verification scope.
- **Systemic:** a test suite whose result depends on the operator's shell is not a gate. Either
  `test_fold.py` should set its own `DOIT_PROJECT` (its fixtures already control the root), or
  `doit test` should clear it before running — `test_deploy.py:27` and `test_tree_cleanup.py:83`
  already pop it by hand, so two files know and the rest do not.
- **Fix status:** open — not in L-charter-0021's footprint; worth a brief.

### R73. The one-document contract landed mid-charter and `doit packet spec-writer` did not come with it (L-planner-0015, 2026-09-17 ~11:40Z)
- **Measured:** `agents/planner.md` was rewritten under a running Planner at ~10:30Z — cut + cut-audit + plan + plan-audit collapsed into **one document and one fable audit**, and the per-spec audit and rework moved from the Executor into the Planner's pane. The tooling followed it halfway. `doit packet plan-auditor` grew a `--stage doc` and works. **`doit packet spec-writer` still refuses without `content/cut-<charter>.md` or a `cut-written` event**, which the new shape never produces: `packet: no cut on file for L-charter-0019 — a cut-written event, or content/cut-L-charter-0019.md`.
- **The workaround, recorded because it is now load-bearing:** the Planner appended `cut-written` naming the *plan* document (`path=content/plan-L-charter-0019.md`) with a `why` saying the document is the cut. That is consistent with the contract's own instruction that `doit audit plan` be handed the same file for `--cut` and `--plan` ("Both flags take the same file and that is correct, not a typo"), but it means the ledger now carries a `cut-written` event for a file named `plan-*`, and anything that pattern-matches on the filename will be wrong.
- **Systemic:** a contract change that reaches the pane before it reaches the packet builders puts the Planner in the position of inventing ledger events to satisfy a stale check. `--stage doc` is the precedent; the spec-writer path needs the same.
- **Fix status:** open — no change-list id.

### R74. `doit alloc adr` handed out ids another pane already held, and only a manual check caught it (L-planner-0015, ~11:56Z)
- **Measured:** the Planner ran `doit alloc adr` nine times (0051–0059), wrote all nine, then later needed two more. It wrote `L-adr-0060` and `L-adr-0061` into the Plan by hand — and both **already existed on disk** as empty stubs (`# L-adr-0060 · architecture`), allocated by another pane and not yet written. The collision was caught only because the Planner ran `[ -f ]` before writing; had it written first, two ADRs from another charter would have been silently overwritten.
- **Why it is not the allocator's advertised behaviour:** `doit alloc` is documented as max+1 with `O_EXCL`, and it *is* — the failure is on the other side. A pane that allocates a batch, uses some, and later writes an id it derived **by counting** rather than by allocating has left the allocator's protection. The contract says "never invent an id yourself" and this is exactly why, but nothing enforces it: an empty allocated stub is indistinguishable from an unallocated gap to anyone reading the directory.
- **Systemic:** ADR ids are the one id class the Planner writes by hand in prose (inside the Plan's Shared-decisions section) before the file exists. Spec ids are safe because `doit alloc spec` prints the path and the dispatch uses it. Either `doit alloc adr` should take a count, or an allocated-but-empty ADR should carry a marker that says who holds it.
- **Fix status:** open — no change-list id.

### R75. A spec-writer's output can be VALID and still unauditable, and the refusal arrives one step too late (L-planner-0015, ~11:54Z)
- **Measured:** `L-spec-writer-0095` returned a schema-VALID output card, the wrapper appended `spec-written` with `ac_count: 10`, and the spec was on the ledger. `doit packet spec-auditor L-spec-0052` then refused to build: *"L-spec-0052's Verification block is not one gated `&&` chain"*. The spec was already a durable, ledger-recorded artifact before the first check that reads its **body** ran.
- **Two costs, one avoided:** the Planner dispatched with the refusal *string* as `--packet`; the wrapper caught it (`is not a file — nothing allocated, nothing spent`), which is the dispatch guard working exactly as designed. The second cost was real: the only way to repair the spec is a `spec-writer` round, and D24 gives a spec exactly one. Spending it on a formatting defect before the audit has run would leave the audit's findings unfixable. The Planner instead resumed the already-stamped sub-agent for a text edit — cheap and correct, but **outside the ledger's spawn accounting**, so `doit spend` under-counts that spec by 12 tool uses and ~130s.
- **Systemic:** the Verification-block shape is mechanically checkable at the moment the spec-writer writes the file, not three steps later at packet time. `doit validate spec-writer` validates the *card*; nothing validates the *body* until an auditor wants it. The same check that `packet.py` runs should run in `validate`, so a malformed spec never reaches `spec-written`.
- **Fix status:** open — no change-list id.

### R76. Three spawns in one Planner context died to caps while their work was fine, and the cap is the thing that is wrong (L-planner-0015, 2026-09-17 09:15–11:15Z)
- **Measured, all three on one charter:** `L-research-0006` — `spawn-failed · "timeout after 5 min"`, output VALID, 21KB of findings on disk, and the `research` contract emits no fold event (`fold.py:99`) so nothing downstream broke. `L-probe-0008` — `spawn-failed · "timeout after 120 min"` with `drive.py` **still running**; its 448-line run record was written after the terminal event, on the serving pane's instruction. `L-plan-auditor-0027` (R70, prior context) — same class, different cause.
- **The caps are not sized to the packets the contracts ask for.** `research` is capped at **300s**; the ten-question codebase sweep this Planner commissioned is the ordinary shape of a research packet under D3 ("one scoped question... when you would otherwise drown in code") and cannot be answered in five minutes by any model. The dig returned `answered: partial` with three sub-questions NOT REACHED — a direct, measurable quality loss caused by the cap, not by the question.
- **What the Planner did, and what it could not do:** harvested both run directories directly rather than re-dispatching, which saved two hours and a second Opus-class run. It could not repair the record: `correction` is operator-only, so `doit spend` permanently shows two failed spawns that produced the two artifacts the entire charter rests on.
- **Systemic:** R70 named the wrapper's repo-status check as the wrong verdict for a read-only role. This is the same shape one level up — a **timeout** is also an observation about the wrapper's patience, not a verdict on the work, and a spawn whose output card is VALID and whose artifact is on disk should not be terminal-failed for arriving late. At minimum the terminal event should distinguish "capped with output" from "capped with nothing".
- **Fix status:** open — no change-list id. `research`'s 300s cap is the one-line part and is worth doing first.

### R77. A served sub-agent inherits `DOIT_LEDGER_FILE` and can write events under the serving pane's identity (L-planner-0015, 2026-09-17 ~12:22Z)
- **Measured:** `L-spec-writer-0102` finished its spec, then ran `doit append spec-written` by hand, reporting: *"since this seat route has no separate wrapper process to do it"*. It does — the wrapper appends `spec-written` when the serving pane runs `stamp.sh`, as it had already done for three specs in the same charter. `L-spec-0057` now carries **two** `spec-written` events: `actor: spec-writer · src: L-spec-writer-0102.jsonl:2` (the wrapper's, correct) and `actor: planner · src: L-planner-0015.jsonl:13` (the sub-agent's, wrong). Both say `ac_count: 8`, so they agree on content and differ only in who is claimed to have written them.
- **The mechanism is the finding, not the mistake.** A served sub-agent runs in a shell that inherits the serving pane's environment, including `DOIT_LEDGER_FILE`. So **any `doit append` a sub-agent runs writes into the serving pane's ledger file, under the serving pane's actor**. §2.5 opens `spec-written` to any actor by design, which is what made the false one land silently; but the same inheritance would let a sub-agent write *any* event type its role is permitted, attributed to the Planner. The Planner's own contract says it "never takes an action whose only record is this conversation" — this is the inverse hole: an action the Planner never took, permanently on its record.
- **Second-order cost, and the reason it is worth more than a duplicate line.** Round-counting reads `spec-written`. `L-spec-0052` and `L-spec-0053` legitimately carry two each (write + D24 rework). `L-spec-0057` now looks identical to them while having had **zero** audits — so anything deriving "this spec has spent its one round" from the event count would silently skip its only audit. The serving pane caught it only because the sub-agent volunteered what it had done in its hand-back.
- **Contained, not repaired.** One event, one spec. `correction` is operator-only. The other three in-flight spec-writers were messaged mid-run with the mechanism and an instruction to append nothing; that warning is now standard in every serve prompt this pane issues.
- **Systemic:** either the seat wrapper should scrub `DOIT_LEDGER_FILE` from the child environment and hand the sub-agent its own, or `doit append` should refuse when the invoking process is not the pane that owns the named ledger file. The contract tells the Planner "never change it" — nobody told the sub-agents they had one.
- **Fix status:** open — no change-list id. The env scrub is the one-line half.

### R78. The serving prompt is a second, unaudited channel into a blind contract (L-planner-0015, 2026-09-17 ~12:37Z)
- **Measured:** the Planner dispatched `L-spec-auditor-0042` and, in the Agent-tool prompt that served the seat packet, added one line steering the auditor away from a trade-off already filed as an operator question and toward what it should look at instead. The auditor recorded it: *"the dispatch message carried a one-line steer about the Python→TS port trade-off. The packet file itself held only the four contracted items, so I set `contamination: false`"* — and filed the disclosure in `advisory` rather than voiding the round. The audit was good and the steer was accurate; that is not the point.
- **The mechanism is the finding.** Under the seat backend the packet is the blindness — `doit packet` builds it, `packet_sha256` records it, and the wrapper refuses a dispatch without one. But the serving pane also writes a **free-text Agent prompt**, which the sub-agent reads *first* and which nothing hashes, records or audits. `contract_sha256` and `packet_sha256` both looked clean on an event whose spawn had in fact received a sentence of the Planner's reasoning. The contract's own rule — *"a sentence of rationale in the packet voids the run as contamination and charges for it"* — names the **packet**, and the serve prompt is not the packet.
- **Why it nearly cost the round.** D24 gives a spec one audit ever. Had the auditor read the steer as contamination rather than an advisory disclosure, `L-spec-0055`'s only round would have been void and unrepeatable. The Planner made that call for the auditor by accident, and got lucky in which way it went.
- **What the serve prompt may legitimately carry**, as this pane now uses it: the spawn id, the contract file to read, the packet path, the wrapper cap, the read-only boundary, the ledger-file warning (R77), the output path and the validate command. Everything else — anything about the *subject matter* — belongs in the packet, where it is hashed, or nowhere.
- **Systemic:** either `stamp.sh` should record a `serve_prompt_sha256` beside `packet_sha256` so the second channel is at least visible in the ledger, or the seat README should state flatly that the serve prompt is mechanical only. Today a blind contract's blindness is enforced on one channel and honour-system on the other.
- **Fix status:** open — no change-list id.

### R79. The board answers "has this charter closed" and is read as "has this work landed", and for three charters those were opposite answers for two days (L-planner-0015, 2026-09-17 ~13:28Z)

The operator said most of the L1-complete charters were in fact done and suspected the ledger was mis-reporting; the operator was right, and the Planner had just told him the opposite in a handover. Charters 0005, 0006 and 0009 all render `L1-complete` while their code is merged on master — 35 `merge(L-spec-…)` commits, most specs `accepted` — because `L1-complete` versus `L2-complete` is a statement about the *close row*, not about the work, and nothing on the board says so. Three of the blockers are honest: `L-spec-0021` carries standing rejects on `AC2` and `done-condition`, `L-spec-0019` has four grader spawns and not one confirmed verdict, and no charter-review has ever completed, so `review_owed` is True everywhere. The fourth is a defect. `L-spec-0010` is merged (`05ed71942`, 2026-09-15), graded and reviewed, and carries two owed criteria — `AC9`, `AC10`, both `wake_at: 2026-09-16T12:00:00Z`, neither discharged by an `owed-met`; read `fold.py:867-882` and it satisfies neither branch, because `accepted` requires `owed_criteria <= met_criteria` and `shipped-owed-evidence` requires an unmet `owed-ac` whose `wake_at` is still in the **future**. Once the clock passed it fell through both into the bare pipeline state `shipped` at `:895`, which is not in `_closable`'s `all_accepted` set and so blocks its charter's L2 permanently, **and** is no longer counted by `owed_within_k` — so the one instrument that bounds outstanding evidence silently lost track of it at exactly the moment it became overdue, making a late obligation less visible than a pending one. No event marks that transition: the state changed because time passed, not because anything landed, so no board row, no tick and no notification fired, and the only way to see it is to evaluate the five conjuncts by hand. A second, independent reporting trap sits beside it: `L-charter-0020` renders with **zero specs** in an `albert-scott`-scoped pane, because `read_events()` filters on `DOIT_PROJECT` while the charter row itself survives the filter, so a scoped pane shows a real charter as empty rather than as out-of-scope — the same `DOIT_PROJECT` hazard already recorded against `test_fold.py` in R72, one level up. The systemic fix has two halves: an expired unmet `owed-ac` needs a state of its own — `owed-overdue`, counted by `owed_within_k` and rendered on the board — or `shipped-owed-evidence` should drop its future-`wake_at` condition and let the K bound do the work it exists for; and the board should distinguish "closed" from "landed" in what it prints, because every human reading it, including this Planner, reads the second and is shown the first. **Fix status:** open — no change-list id; the `owed-overdue` state is the load-bearing half.

### R80. The orchestration box's disk hit 100% mid-plan and silently truncated thirteen files to zero bytes (L-planner-0016, 2026-09-17 ~13:50Z)

- **Measured:** writing thirteen ADR files with a `cat > file <<EOF` loop, every one landed at **0 bytes**. `cat` printed `No space left on device` to stderr thirteen times; **the shell's exit status was 0** and the loop ran to completion. `df -h /` read `/dev/sda1 150G 144G 0 100% /` — zero bytes available, not "nearly full". The Plan document itself (35 KB) had been written ninety seconds earlier and survived; the truncation window opened between two writes in the same task.
- **Why it is worth a record and not just a cleanup.** The Write tool reports success, the Bash tool reports exit 0, and `wc -l` on the result reads `0` — which is indistinguishable from "the heredoc body was empty". Nothing in the DO-IT loop checks that a content file it just wrote is non-empty, and `doit append <type> ... path=...` records the path, never the size. Had this happened one step later, `doit dispatch spec-writer --packet <slot>` would have handed a **0-byte packet** to an Opus spawn, and the spawn would have burned a full run on an empty brief — the wrapper's `is not a file` guard (R75) catches a *missing* path, not an empty one. The project's own CLAUDE.md already warns that this box runs at 89% and that the droplet's figures do not apply to it; the warning was accurate and nothing acted on it.
- **The shape is the same one the charter under audit exists to fix.** L-probe-0002's whole finding was a nightly job that failed while every surface said otherwise: `status = failed` written by a reaper, `exit_code` NULL, and `job-skips.log` printing `SKIPPED: lock held` on seven consecutive nights while the job was demonstrably running. A truncated write that reports success is that defect one layer down, in the tooling that plans the fix.
- **What the Planner did, and what it deliberately did not do.** Reclaimed **12 GB** from unambiguously regenerable package caches (`~/.cache/pip`, `~/.cache/uv`, `~/.cache/puppeteer`, `npm cache clean --force`, `~/.npm/_cacache`), verified with `df`, rewrote all thirteen ADRs and verified each one's byte count individually. It did **not** touch the three real pools: `/var/tmp` (30 GB of stale scratch — throwaway Postgres data dirs, compose scratch and inspection copies from 2026-09-13..15 and earlier), `/opt/albert-scott-worktrees` (28 GB) and `~/.do-it/worktrees` (12 GB) (builder worktrees — a live Executor may hold them, and D67 reaps them on merge, not on disk pressure), or `~/.claude` (12 GB) and `~/.codex` (3.4 GB) (session transcripts). `rm -rf` outside a sandboxed path is "never without explicit instruction" in the operator's global preferences, so the escalation **names the irreversible act** instead of carrying a default — the second of the two shapes §8.7 permits. Filed as `escalation-blocking` on `L-charter-0022`.
- **Systemic, in the order worth fixing.** (1) `doit append` should refuse a `path=` that names a zero-byte file — one `stat` call, and it closes the "durable state is truth" hole at the only place the system writes durable state. (2) `doit dispatch` should refuse a zero-byte `--packet` alongside its existing `is not a file` check; a packet is the blindness, and an empty one is a spawn spent on nothing. (3) This box has an hourly disk watch (the project CLAUDE.md names it); it did not stop a pane from writing into a full disk, so whatever it does with its measurement, it is not gating writes. Twelve gigabytes of headroom on a box whose worktrees are 40 GB is roughly one builder worktree.
- **Fix status:** open — no change-list id. (1) is the one-line half and the one that would have caught this.
