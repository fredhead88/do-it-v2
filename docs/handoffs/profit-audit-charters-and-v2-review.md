# Profit audit → charters, and the v2 review after charter 3

## Status
2026-09-15 08:40Z. Charter 3 closed on the Claude-only map in 77 min with no operator hands; v0.2.0 shipped.
A Codex audit of the QurLife profitability portal exists and says **NOT READY** with blocking money
defects. Nothing has been done with that audit yet. The next session has two jobs, in this order:
(1) present the system changes today's run argues for, in plain English, and do the ones the
operator approves; (2) as a Thinker, turn the audit into charters under Goal A and land them.

## Goal
Every blocking finding in the profitability audit is owned by a charter with a dollar figure and a
done-condition (Goal A's own words), and the v2 pipeline runs charters 4+ with less pane time than
charter 3 — tokens and wall clock visible on the board while it does.

## Current State
- **v2 repo** `~/do-it-v2` at `main` (v0.2.0 + unreleased fixes; `doit version`, `CHANGELOG.md`).
  Ledger `~/.do-it`; every pane sources `~/.do-it/env.sh`. Model map `~/.do-it/models.toml` =
  the Claude-only profile (`doit models show`); Codex weekly quota is exhausted until it resets.
- **Charters:** 0001/0002/0003/0004 all `L2-complete`. Goals `L-goal-0001` (profitability, G1–G5,
  dated 2026-09-15) and `L-goal-0002` (Yitzy, G1–G6). Board: `doit` from `~/.do-it/repos/albert-scott`.
- **Owed:** `L-spec-0006` AC7 wakes **2026-09-16T11:04Z** — one human run of
  `/opt/albert-scott/scripts/liveness_reaper.sh` on master after that instant, then a re-grade.
  Commands in `v2-pilot-retro-model-split.md` Next Steps 8.
- **Prod:** master `f38ba5dda` (both charter-3 merges), every required CI check green except the
  pre-existing `Repo Lint Guards` red (adjacent brief on L-charter-0003). Droplet `/version` is
  whatever the push-deploy workflows synced; `deploy.sh` has not run since `5cd5c47de`.
- **The audit:** `/opt/albert-scott/docs/sessions/2026-09-15-profit-v2-client-walkthrough.md`
  (406 lines, untracked in git; written by the Codex session `flow:4 codex-2` from the prompt
  `~/codex-walkthrough-qurlife-profit-v2.md`; screenshots under
  `/opt/albert-scott/output/walkthrough/2026-09-15/`). Sections: Walkthrough record · Blocking
  findings (B1 two after-cost profit answers differ by exactly advertising, B2 seven August
  drawers omit estimated COGS from Real profit, B3 September daily sums ≠ period total, …) ·
  Non-blocking findings · AS-1…AS-40 live re-verification · Math tie-out tables · What could not
  be tested · What would make this READY. Measured on the deployed sha of 2026-09-15 against
  `verify@albertscott.com`, QurLife scope, Aug 1–31 and Sep 1–15.
- **Today's record:** `~/do-it-v2/docs/handoffs/albert-scott-pilot-2026-09-14.md` — R2–R7 and the
  charter 3 run log (per-role tokens/minutes table in R7); every S/T entry carries a Fix status.
  Ranked change list: `~/do-it-v2/docs/handoffs/pilot-retro-change-list.md`. Retro handoff:
  `~/do-it-v2/docs/handoffs/v2-pilot-retro-model-split.md` (Next Steps 4, 6, 7, 9, 10 open).
- **Seat helpers** now in the repo: `~/do-it-v2/scripts/seat/` (`stamp.sh`, `mkpacket.py`,
  `mkslot.py`, README with the serving prompt shape).

## Active Problems
1. **The audit is unowned.** Goal A's G3/G5 ask exactly for this ("re-measure on the deployed sha;
   every remaining money defect owned with a dollar figure") — the audit IS that measurement, and
   no charter cites it. Its blocking findings carry the numbers a charter Intent needs (S22/b-6:
   measured, not read).
2. **Pane time is the unmeasured cost.** Charter 3's 17 spawns were 93 spawn-minutes inside 77
   wall-clock minutes; the pane's own tokens are invisible, and the seat route reports one blended
   token figure per sub-agent (no input/output/cache split; model weights not applied). Operator
   asked for per-spawn tokens + wall clock on the ledger and board — retro Next Steps 9.
3. **Still open from the change list, in value order:** a-4 `deploy.py` (false failure on a live
   deploy, lost log), a-5 checker lint + BASE pin, a-6 owed evidence (`owed-met`,
   `closed-shipped`; the declaration half shipped today), a-14 Planner packets into `packet.py`
   (the three seat helpers are the shape), b-1/b-2 design-doc text for the backend seam and the
   vendor rule, b-6 threshold arithmetic in a charter Intent (bit charter 3 twice).
4. **Typed Agent spawns are stale for a pane opened before a contract edit** (R2): serve as
   `general-purpose` + contract file; a `doit doctor` is a-16.
5. **The reaper's cron is paused** (gating-watch tick, `PAUSED-2026-09-14`): a future foreign lock
   surfaces only when a human runs the script. Adjacent brief on L-charter-0003; operator's call.

## Key Decisions Made
- **Claude-only map, decided 2026-09-15:** Sonnet authors (spec-writer, builder, probe, reviewer,
  grader), Opus judges (spec-auditor, plan-auditor, charter-reviewer), Fable panes (thinker,
  planner). Opus is nobody's default; it is chosen at three judging seats on measured value.
  Judge ≠ author's vendor when Codex is back (`models.example.toml`, with `fallback`).
- **`claude -p` stays banned**; every spawn is seat; the tick refuses on this root. Env vars may
  only agree with `models.toml`.
- **K=1**; a merge with one honestly owed criterion goes under a recorded Executor `decision`,
  and the owed-ac carries `wake_at` (schema) so the fold derives `shipped-owed-evidence`. Never
  `spec-closed` on a built spec (S33).
- **Pre-existing CI reds outside the footprint are `blocked-external` + brief, never a revert**
  (S17). The `Repo Lint Guards` red is one.
- **The pane may act as operator under the 2026-09-15 autonomy instruction** for reversible
  ledger repairs, and must name each one for veto (one `correction` today, on
  `L-spec-writer-0016.jsonl:5`).
- **Specs ≤ 400 lines**; a rework applies fixes in place; one audit round per stage.
- **Charters, not a new goal, for the audit** (recommendation, not yet ruled): Goal A exists and
  its G3/G5 are the slot; land with `--goal L-goal-0001`.

## Next Steps
1. **Read, in this order** (30 min): `pilot-retro-change-list.md` whole; R2–R7 in the pilot record
   (from `### R2.`); `v2-pilot-retro-model-split.md` Next Steps 4–10. Then `doit` and
   `doit models show`.
2. **Present the system changes to the operator in plain English** — one short list, each item:
   what breaks today, what changes, what it costs (minutes), what it removes. Candidates, ranked:
   (a) token + wall-clock accounting on the ledger and board with model weights and the four token
   types where the backend gives them (retro step 9); (b) `deploy.py` truthfulness (a-4);
   (c) checker lint + BASE pin (a-5); (d) owed-evidence completion (a-6); (e) Planner packets
   into `packet.py` from `scripts/seat/` (a-14); (f) the two design-doc paragraphs (b-1, b-2).
   Do what he approves; tests for each; `./doit test` green; conventional commits; push; bump
   `VERSION`/`CHANGELOG.md` (0.3.0 if (a) ships).
3. **If the session is after 2026-09-16T11:04Z:** run the owed reaper step first (retro Next
   Steps 8) and re-grade L-spec-0006 so it derives `accepted`.
4. **Thinker for the audit** (`doit think profit-audit` from `~/.do-it/repos/albert-scott`, Fable
   pane): read the walkthrough report whole, then Goal A (`~/.do-it/content/L-goal-0001.md`) and
   its G1–G5. Cluster the blocking findings by root cause and footprint, not by page — e.g. one
   charter for the money model (B1 advertising missing from Net P&L, B2 estimated COGS omitted,
   B3 daily-vs-period allocation; footprint `api/app/lib/profit_v2/`), one for client-facing
   surface defects (stale settlement download link, internal terminology, confidence labels that
   contradict their details), one for "what would make this READY" residue. Each charter Intent
   carries the report's measured numbers and the deployed sha; Constraints name the report as the
   probe (D96) and the bug-fix protocol; Done-for-the-whole is the client's question. Land each
   with `doit think --land <file> --goal L-goal-0001 --print-only`; record a `decision` on
   `L-goal-0001` naming the audit as G3's re-measurement. Present the charter set to the
   operator before landing if the cluster boundaries are a judgment call — they are.
5. **Then drive them** from a Planner/Executor pane exactly as charter 3 was driven (the seat
   helpers + README), one charter at a time, K=1, reading R2–R7 first for the traps.

## Reference
- v2: `~/do-it-v2` (`src/`, `agents/`, `scripts/seat/`, `design/system-design-v2.md`,
  `docs/handoffs/`). `./doit test` · `doit help` · `doit models show|use`.
- Ledger `~/.do-it` (`events/`, `content/`, `packets/`, `seat/`, `logs/`, `worktrees/`); repo
  symlink `~/.do-it/repos/albert-scott` → `/opt/albert-scott`.
- Albert Scott: `/opt/albert-scott` (master), prod droplet 167.71.46.51, portal
  `clients.albertscott.com`, verifier login `verify@albertscott.com` (`VERIFIER_CLIENT_PASS` in
  `/opt/albert-scott/.env`; strip outer quotes). CI: `gh run list --commit <sha>`; pytest ≈ 45 min;
  a second push cancels the first run (`cancel-in-progress`). Master push prints a bypass notice
  ("must be made through a pull request") and lands (R5).
- tmux `flow`: 0 codex (listing spine), 1 vision (driver), 2 think-goals, 3 cockpit, 4 codex-2
  (the audit session, quota exhausted).
- Rules that bind: `/opt/albert-scott/CLAUDE.md` (no `claude -p`; rollback-first; bug-fix
  protocol), `~/do-it-v2/CLAUDE.md` (append-only ledger; no `actor` field; undetermined is never
  clean).

## Session Log
- 2026-09-15 (retro pane): v0.2.0 shipped; charter 3 closed on the Claude-only map (R2–R7);
  five fixes on the day; audit report landed from Codex; this handoff written.
- 2026-09-15 (earlier): pilot record read whole → ranked change list; model map decided; S32
  resolved (atexit poke).
- 2026-09-14: pilot day 1 — charters 0001/0002/0004 closed, backlog deployed, S1–S35, T1–T15.
