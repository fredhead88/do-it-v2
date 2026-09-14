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
  instruction. A pane started *after* `install.sh` may see them — untested.
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

### Smaller, all real
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
| 3 | plan-auditor (plan) · `L-plan-auditor-0003` | seat | opus | dispatched with the `doit validate` loop; result pending |
