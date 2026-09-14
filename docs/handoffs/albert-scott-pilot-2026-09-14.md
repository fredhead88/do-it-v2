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

### S5. Model substitution changes which safeguard rules apply
- **Mechanism:** `grader`, `spec-auditor`, `plan-auditor` are `model: claude-fable-5-1`.
  The Agent tool substitutes Sonnet for Fable silently; the project rule is "opus for
  audits". D120's per-model, per-word safeguard rule was measured on Fable.
- **Pilot:** fable contracts run on Opus, passed explicitly. The terminal event's
  `model` is whatever the result envelope says, so the ledger is honest.
- **Systemic:** the contract's `model:` should be a *preference* the backend maps,
  with the mapping recorded once per ledger root, and `contract_sha256` re-trust
  (D120) keyed on `(contract, model actually used)`.

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

### S7. Cut-file documentation gap: an empty seam label vs `none`
- **Mechanism:** `audit.fields()` splits a label's value into names. `Consumes: none`
  is a seam named `none` with no producer → a false `undefined seam` finding. An
  *empty* value is what a unit with no seams must write, and nothing says so.
- **Pilot:** `Consumes:` / `Produces:` left empty; pre-pass clean.
- **Systemic:** `planner.md` ① should show the no-seam form; or `split()` should
  treat `none`/`—` as empty.

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

### T2. `doit alloc` with no kind crashes instead of printing usage
- **Mechanism:** `fold.py:603` reads `sys.argv[2]` unguarded → `IndexError` traceback.
- **Pilot:** harmless; `doit help` documents the form. **Patch only.**

### T3. The record has no per-role convention, and the brief forbade the second role from writing to it
- **Mechanism:** `think-goals-brief.md` lists this file under *"read, do not edit"*;
  the operator then asked the Thinker to record into it *"through the lens of a
  different perspective and recorded as such."* Two panes, one record, no rule.
- **Pilot:** this section — appended, never interleaved, `T<n>` ids, the driver's
  text untouched.
- **Systemic:** if the pilot record is the systemic log, every pane role that runs
  during a pilot gets an appended section keyed by its ledger actor id. Same shape as
  the ledger itself: the filename is the actor.

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

### T12. An `open` charter is invisible on the board
- **Mechanism:** `doit states` shows L-charter-0002/0003/0004 as `open`; `doit` (the
  board) lists none of them — `WRITTEN, NOT PICKED UP` is a spec heading, and no heading
  shows a charter that has been filed but not cut. The driver pane asked the operator
  for a ruling on "the deploy charter" without knowing three had landed.
- **Pilot:** the operator carries the fact between panes by hand.
- **Systemic:** the board needs a `CHARTERS OPEN (n)` line (charter-filed, no
  `plan-written`), the mirror of `CHARTER CLOSE`. A Planner that "does not wait to be
  asked" (§3.5) has to be able to see what it should pick up.

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

### S19. `doit alloc` treats any argument as a kind — `doit alloc -h` allocated `L--h-0001.md`
- **Mechanism:** `alloc <kind>` builds `L-<kind>-NNNN` with no validation of the kind
  token (T2 is the no-argument crash; this is its sibling).
- **Pilot:** the junk content file was deleted by hand (no event references it).
- **Systemic:** validate `kind` against the id kinds §2.8 names, and honour `-h`.

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

### S21. Declarations land as events typed by their term, and nothing reads them back by that name
- **Mechanism:** a spawn's `declarations` land as one event per term (`charter-gap`,
  `worked`, `seam-undefined` …), not under a `declaration` type. A reader that greps for
  the word "declaration" finds none; the pane's hand-built plan-stage packet carried the
  nine findings and no declaration lines because it looked for the wrong type.
- **Pilot:** the plan audit ran without the cut-audit's three declaration lines.
- **Systemic:** minor; the packet builders (S6) should read the term-typed events, and
  `dispatch.py` should document the shape once.

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

### T14. The driver pane cannot be told anything except by typing into it
- **Mechanism:** the ruling on the deploy was appended as a `decision` on
  L-charter-0002; the driver was mid-spawn (a plan-auditor running) and its board has
  no heading for open charters (T12), so the Thinker also `tmux send-keys` a pointer
  into its prompt. Two panes on one ledger with no notification path except the fold.
- **Systemic:** `DECIDED WITHOUT YOU` already exists for decisions; a `decision` whose
  subject is a charter another pane is driving should surface there for that pane on
  its next fold. Verify whether it does; the pilot could not wait to see.

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
