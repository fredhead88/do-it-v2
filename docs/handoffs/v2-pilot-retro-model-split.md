# do-it-v2 pilot retro — inefficiency, model-per-agent, and the Codex/Claude split

## Status
**2026-09-15 08:00Z — charter 3 (L-charter-0003) is L2-complete** on the Claude-only map: 77 min
first cut → reap, two specs, 17 spawns, 2.12M blended tokens, zero operator interventions (R7 in
the pilot record). v0.2.0 shipped; four wrapper/contract defects fixed on the day (see CHANGELOG
Unreleased). Owed: L-spec-0006 AC7 wakes 2026-09-16T11:04Z — see Next Steps 8.

Pilot day 1 (2026-09-14) is over: 3 charters closed, the 60-spec backlog deployed, prod on
`5cd5c47de`. The systemic record (S1–**S35** driver, T1–T15 thinker — the "S14" here was
stale) has now been read whole and turned into one ranked list:
`docs/handoffs/pilot-retro-change-list.md` (2026-09-15). Steps 1, 2, 3 and 5 below are done;
the model map is installed at `~/.do-it/models.toml` and enforced by `dispatch.py`/`tick.py`.

## Goal
Make v2 run a second and third real charter with fewer operator interventions, less wall clock,
and Fable 5.1 used where it earns its cost — and Codex (GPT-6 Astra) used where it is better —
without the seat spend of running everything on a frontier model.

## Current State
- **Ledger:** `~/.do-it` (v2). Goals `L-goal-0001` (profitability, 2026-09-15), `L-goal-0002`
  (Yitzy, 2026-09-14). Charters 0001/0002/0004 `L2-complete`; 0003 (foreign-owned lock) `open`,
  untouched. One escalation NEEDS YOU: `L-spec-0003` ninth collection-parity allowlist entry
  (listing coordinator's code, driver waiting).
- **Systemic record:** `~/do-it-v2/docs/handoffs/albert-scott-pilot-2026-09-14.md` —
  driver S1–S14 + smaller, thinker T1–T15. This is the raw material for the retro. Read it
  whole before proposing anything; every entry names mechanism / pilot patch / systemic fix.
- **Contracts** `~/do-it-v2/agents/*.md`, `model:` line as written:
  opus-5 → builder, charter-reviewer, executor, planner, probe, reviewer, spec-writer, thinker;
  fable-5-1 → grader, plan-auditor, spec-auditor; haiku-4-5 → research; sonnet-5 → reuse-scout.
- **What actually ran (41 terminal events on 09-14, 7 `spawn-failed` — not 9; 39 seat,
  2 metered tick spawns):** every one on `claude-opus-5`. The three Fable contracts (grader, both auditors) never ran on Fable —
  the Agent tool substitutes Sonnet for Fable, so the driver passed Opus explicitly (S5). The
  thinker contract says opus-5 but the real Thinker was a Fable *pane*. So: the model choice per
  agent exists on paper and was not honoured for 3 of 13 contracts, and D120's per-model
  safeguard rules were calibrated on a model that never ran.
- **Two executor spawns carry a price** (`L-executor-0003` $1.02, `L-executor-0004` $1.07,
  `spawn_path` blank). Everything else is unpriced seat. Check whether those two went through
  `claude -p` — if so the ban was breached once; if not, the wrapper mis-stamps.
- **Codex:** `flow:0` = listing-spine coordinator session; `flow:4 codex-2` = the client
  walkthrough started 2026-09-15 on gpt-6-astra (prompt `~/codex-walkthrough-qurlife-profit-v2.md`,
  report due at `docs/sessions/2026-09-15-profit-v2-client-walkthrough.md`). Codex weekly limit
  was at 6% when it started. Codex has no MCP browser; it uses repo-venv Playwright.

## Active Problems
1. **Wall clock.** Charter 0001: thinker 05:14 → merge 06:23 (69 min) → deploy refused on a
   second red → 0004 written 07:0x → deploy landed 17:34. Three charters = one full day.
   Spawn durations (ms in ledger): builders 550k–1,286k; spec-writers 325k–916k; spec-auditors
   415k–634k; charter-reviewers 386k–690k; graders 187k–424k; plan-auditors 123k–240k.
   Serial by construction: audit → rework → audit → build → grade → review → charter-review,
   each a 5–20 min Opus spawn, each waiting on the previous.
2. **Retries and voids.** 9 of 41 spawns `spawn-failed`: spec-writer ×5 (0001 S13 completion
   signal; 0007/0009/0011/0012 — read their events for cause), plan-auditor 0001 (S11 volatile
   repo), charter-reviewer 0001. Each failure re-ran the full spawn (S9/S10 format retries too).
3. **Model per agent is not enforced anywhere.** The seat route can't load contracts as agent
   types (S4), can't honour `model:` (S5), can't enforce `tools:`. It is prose in a prompt.
4. **Packets are hand-built for the Planner's own spawns** (S6) and the charter-set packet is
   per-call, not per-goal (T11). Rationale-blindness is a discipline, not a structure (S3).
5. **Board blind spots:** open charters invisible (T12); idle pane answers from memory (T15);
   no cross-pane notification except typing into the other pane (T14).
6. **Goal/Covers model can't express** pre-goal charters (T5), cross-goal delivery (T9), or
   two goals with the same `G<n>` ids (T8); `--land` without `--goal` picks the newest goal.
7. **Ingest port for Yitzy exists only in the design** (T7): v4's ingest writes v4's ledger;
   no `spec-written charter: null` event; his 1448/1449 sit numbered with no v2 path.

## Key Decisions Made
- `claude -p` stays banned in this project (pilot-1447 decision); every spawn is seat-billed
  via `doit dispatch --seat` + Agent tool. Fable is a pane role only (silent Sonnet substitution
  as sub-agent). Opus 5 is the operator's stated disappointment — do not default to it.
- K=1 (one owed criterion per charter, the post-merge check-run pattern) — pilot-1447.
- Goal dates order concurrent charters (D80) — operator accepted once told (T1).
- Rollback-first on prod; migrations listed and acked before a deploy (charter 0002 R2 was
  acked by the operator in the driver pane).
- Listing spine stays with Codex; not in either goal.

## Next Steps
1. ~~**Read the pilot record whole** and sort into (a)/(b)/(c); one ranked list.~~ **DONE
   2026-09-15** → `docs/handoffs/pilot-retro-change-list.md` (17 code items ranked, 12
   design/contract items, 7 drops, each naming the S/T entries it closes and the measured cost).
2. ~~**Model map, decided once per ledger root.**~~ **DONE 2026-09-15**: `models.example.toml`
   (repo) → `~/.do-it/models.toml` (this root); `src/models.py` loads and validates it;
   `dispatch.py` takes backend + model from it (flags/env may only agree), adds a `codex`
   backend (`codex exec … --output-schema … -o … --json`, smoke-tested live: schema-shaped
   output, usage per turn, thread id), and stamps `backend`, `model_requested`, `model_used`,
   `model_observed`, `model_match`, `first_on_model` on every terminal event; `poke()` and
   `tick.main` refuse when the root's Executor is not `claude-p` (proved on the real root:
   `tick{refused}` event at 2026-09-15T05:58:31Z). The map as decided, and where it departs
   from the proposal below: **judge ≠ author's vendor** (spec-writer/builder/probe on Astra ⇒
   spec-auditor/grader on Claude Sonnet, Opus the named fallback, never a default); executor
   is `pane` (under a seat-only root the driver pane is the Executor, S2); reviewer and
   charter-reviewer are Codex **provisionally** until the walkthrough report lands. What is
   NOT done: the live D120 trust run per (contract, model) — every contract has
   `first_on_model` still unset on its new model; that is charter 3's first measurement.
   The proposal as it was written:
   - Thinker, Planner (pane roles, judgement): **Fable 5.1 pane** — one standing pane, not
     per-spawn.
   - grader, spec-auditor, plan-auditor (blind, packet-only, short): **Sonnet 5** first; these
     are where D120's safeguard rule must be re-measured per model.
   - spec-writer, builder: **Codex (GPT-6 Astra)** — long, tool-heavy, code-writing; the
     operator rates Astra highest for hands-on work. Needs a `codex` backend in `dispatch.py`
     (S1) — `codex exec` non-interactive with the packet on stdin and the Output schema
     validated by `doit validate` (S9).
   - reviewer, charter-reviewer (drive the deployed thing as a user): **Codex** for the same
     reason — this is what the walkthrough is testing today.
   - research: haiku; reuse-scout: sonnet — keep.
   - executor: whoever drives the pane; it should be cheap (Sonnet) because it is "dumb and
     fast" by contract.
3. ~~**Measure before changing.**~~ **DONE** — the per-role table with proposed targets is in
   the change list (§ Step 3). Headline: none of the seven failures was a model failure; all
   were wrapper or driver, so (a)-1/2/10 outrank any model change for wall clock.
4. **Cut the serial chain where the design allows:** S9/S10 format retries → `doit validate`
   loop in every seat prompt (done for some roles, not all); S13 completion token everywhere;
   S11 grant-scoped porcelain instead of whole-tree; run grader and reviewer concurrently
   where the reviewer has no dependency on the grade.
5. ~~**Resolve the two priced executor spawns.**~~ **DONE — a ban breach, twice, $2.09,
   mechanically explained** (change list § Step 5): `dispatch.main`'s atexit `poke()` fired
   from a wrapper whose shell lacked `DOIT_NO_POKE` (spec-auditor-0003 exited at 11:25:58Z,
   the first tick's second), spawned `tick.py` → metered `claude -p --agent executor`; that
   Executor's own seat spec-writer timed out unserved at 11:57:34Z and its poke ran the second.
   Closed by the map: the ruling is per root now, not per shell.
6. **Board fixes:** CHARTERS OPEN line (T12); per-goal charter-set packet (T11); `--land`
   refuses without `--goal` when >1 goal (T8).
7. **Ingest port** `doit ingest <pr>` with a ledger `decision` as the human gate (T7/T13).
8. ~~Charter 0003~~ **DONE 2026-09-15** (L2-complete 07:45Z, R7). **Operator action tomorrow,
   after 2026-09-16T11:04Z**, on the builder box as `albert`:
   ```
   cd /opt/albert-scott && git pull --ff-only && scripts/liveness_reaper.sh --dry-run && scripts/liveness_reaper.sh
   ls -la ~/.claude/ledger/liveness/ | grep grading; tail -3 ~/.claude/ledger/liveness/liveness-reaper.log
   ```
   Expected: no 957 lock; a `reaped unopenable lock … owner=yitzy` line. Then re-dispatch the
   grader on L-spec-0006 (`doit packet grader L-spec-0006 --worktree <a fresh worktree of master>`)
   so AC7 confirms and the spec derives `accepted` — or, once a-6 ships, append `owed-met`.
   Also on the record for a veto: the pane, acting as operator, appended one `correction`
   (`L-operator-local.jsonl`, ref `L-spec-writer-0016.jsonl:5`) adding the writer's own stated
   `wake_at` to its owed-ac declaration so the fold could derive `shipped-owed-evidence`.
   CI on the merges: 6 of 7 checks green on both; `Repo Lint Guards` red is pre-existing (brief
   filed); `Python Tests` on `f38ba5dda` **completed success** 08:22Z (pytest + pg-parity). Charter 3's
   master is green on every required check.
9. **Token accounting (operator ask, 2026-09-15):** record the backend's usage split on every
   terminal event (`claude -p` and codex already give input/output/cache read/cache write; the
   seat route gives one blended figure), add a `[weights]` table to `models.toml` (per model: the
   four price ratios), render per-model raw + weighted totals on the board (`SPEND` block) and
   `doit spend <charter|spec>`; derive stage wall-clock from event timestamps; backfill the split
   for seat spawns from their sub-agent transcripts (`session` → `~/.claude/projects/…`) once, to
   calibrate. Then declared token budgets per contract with `budget-exceeded` (§4.4).
10. Then Goal A's G3 re-measurement charters on `f38ba5dda`.

## Reference
- v2 repo `~/do-it-v2` (`src/`, `agents/`, `design/system-design-v2.md`, `docs/handoffs/`).
- Ledger `~/.do-it` (`events/*.jsonl`, `content/`, `seat/`); `doit`, `doit states`,
  `doit events <subject>`; `~/.do-it/env.sh` sourced by every pane (DOIT_K=1).
- Albert Scott repo `/opt/albert-scott`; prod droplet 167.71.46.51; `/version` names the sha.
- tmux session `flow`: 0 codex (listing), 1 vision (driver/executor pane), 2 think-goals
  (this thinker, closing), 3 cockpit, 4 codex-2 (walkthrough).
- Codex CLI 0.153.4; dangerous mode = `codex --dangerously-bypass-approvals-and-sandbox`;
  `codex exec` for non-interactive. Codex swallows `/` typed via tmux send-keys.
- Portal verifier: `verify@albertscott.com`, `VERIFIER_CLIENT_PASS` in `/opt/albert-scott/.env`;
  `hello@ephraimgreenblatt.com` is deactivated as a portal login.

## Session Log
- 2026-09-15 (retro pane, second block): v0.2.0 shipped (profiles, fallback, seat route in every
  contract, VERSION/CHANGELOG/README, Fix status on every record entry); charter 3 driven end to
  end from this pane as Planner + Executor on `models.claude-only.toml` — R2–R7 recorded, run log
  in the pilot record; fixes shipped on the day: empty `--packet` refusal, rework slot fallback,
  grader git-history rule, owed-ac `wake_at` in the schema, OWED EVIDENCE render.
- 2026-09-15 (retro pane): read S1–S35 + T1–T15 whole; wrote the ranked change list; decided
  and installed the model map; codex backend + requested/used stamping + tick refusal shipped
  with tests (`./doit test` green, 26 dispatch spawns mocked); tick refusal proved on the real
  root. Next: (a)-1 (structured return is the Output — `Write, Bash` on the three no-Write
  contracts), (a)-4 (deploy.py), (b)-1/2 (design §3.1/§4.2/§4.5), then charter 3 as the D120
  trust run of Sonnet judges + Astra writers.
- 2026-09-15: Corrected the stale "not built" claim; opened codex-2 on Astra and started the
  client walkthrough; confirmed the poe-migration-guard red is fixed on the deployed sha; wrote
  this retro handoff.
- 2026-09-14: Thinker L-thinker-0002 — two goals, three charters (0002 deploy, 0003 lock,
  0004 activation red), decisions on 1178 (conditional yes) and Yitzy ingest (done: 1448/1449),
  pilot record T1–T15. Driver pane closed 0001/0002/0004 and landed the deploy the same day.
