# Changelog

All notable changes to DO-IT v2. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the version is in `VERSION` and `doit version` prints it. Versions are tagged on `main`.

Each entry names the pilot-record finding it answers (`S<n>` driver, `T<n>` thinker, `R<n>` retro —
`docs/handoffs/albert-scott-pilot-2026-09-14.md`), and every finding in that record carries a
**Fix status** line pointing back here, so "did we fix it, and did the fix work" is one lookup in
each direction.

## [Unreleased]

### Fixed
- `doit dispatch` refuses an empty or non-file `--packet` before allocating a spawn id (a shell
  that captured a failed `doit packet` into a variable handed `""`, which read `.` and crashed
  after allocation). ("Smaller" list; hit again on charter 3.)
- `doit packet spec-writer` on a rework reads the Planner's round-one slot from
  `content/slot-<spec>.md` when no packet is on disk, so the first rework of a spec no longer
  needs `--slot` typed by hand. (S6, half.)

## [0.2.0] — 2026-09-15

The day-after-the-pilot release: the model map, and the changes that stop the driver pane doing a
machine's job.

### Added
- **`models.toml` per ledger root** (`src/models.py`): contract → backend (`pane` · `seat` ·
  `claude-p` · `codex`) → model, decided once per root. The loader refuses a map that lies —
  Fable off a pane, a pane role dispatched, an unknown backend. Templates: `models.example.toml`
  (mixed Claude/Codex) and `models.claude-only.toml`. `doit models use <mixed|claude-only>`
  installs one and records a `models-changed` event; `doit models show` prints the ruling. (S5, S32)
- **`fallback` per contract**: when the primary backend refuses for its own reason (a Codex limit,
  an unreachable seat) the wrapper re-dispatches once on the fallback and stamps
  `backend_fallback: true`. (R2)
- **`codex` backend** in `dispatch.py`: `codex exec -C <cwd> -m <model> --sandbox <per role>
  --output-schema <schema> -o <output> --json`, both vendors' metered keys stripped from the
  environment; schema enforced by the CLI, so no validate loop and no transcription. (S1, S18)
- **Requested-vs-used stamping** on every terminal event: `backend`, `model_requested`,
  `model_used`, `model_observed`, `model_match`, `first_on_model` — the D120 trust run of a
  (contract, model) pair is now a ledger fact. (S5)
- **Tick refusal**: `poke()` and `tick.main` read the map and refuse, with a recorded
  `tick{refused}` event, when the root's Executor is not `claude-p`. Fired on the Albert Scott root
  2026-09-15T05:58:31Z. (S32, R1)
- **Seat route in every dispatchable contract**: the sub-agent writes its own
  `seat/<spawn>.output.json` and runs `doit validate` until VALID; `plan-auditor`, `spec-auditor`,
  `grader`, `reviewer`, `charter-reviewer` gain the `Write`/`Bash` that needs. The pane never
  types a finding again. (S18, S28, S10, S29-5)
- `VERSION`, this changelog, `doit version`.

### Changed
- `dispatch.py`: the backend comes from the map; `--seat`/`DOIT_SEAT` may only agree with it or the
  spawn is refused before a start event or a spend; `--model` rides the `-p` line; the terminal
  event's `model` is what came back, null when unobserved — never the contract's line.
- `install.sh` installs `models.toml` from the mixed template when the root has none.
- README brought up to date with what the repo actually holds (it still described the fold and one
  guard).

### Docs
- `docs/handoffs/pilot-retro-change-list.md`: S1–S35 + T1–T15 read whole and ranked by measured
  pilot cost; per-role table; S32 resolved.
- Pilot record: every entry carries a **Fix status** line.

## [0.1.0] — 2026-09-14

The state of the repo at the end of the Albert Scott pilot, day 1 (three charters closed, the
60-spec backlog deployed, 41 spawns, 7 failed). Everything before this was the self-build
(`docs/handoffs/baseline-2026-09.md`); this tag marks the first version that ran a real project.

### Included
- The fold (`fold.py`): append-only ledger, derived state, board, `EMITS` authorization.
- The merge gate (`merge_gate.py`), `deploy.py`, `tree_cleanup.py`, `audit.py` (the six mechanical
  pre-pass checks), `packet.py` (per-role packets with the Blindness strip), `think.py`, `up.py`,
  `tick.py`, `paid_call.py`, `validate.py`, `vet-dep.mjs`, `backup.sh`.
- `dispatch.py` with the `claude-p` and `seat` backends, schema enforcement on both, the seat
  completion stamp (`meta.json`), `DOIT_REPO_VOLATILE`, the full card sidecar for the grader.
- Thirteen contracts under `agents/` with ten Output schemas.
- Pilot fixes landed during the day: `c1ceab5` (S13), `a8b7b76` (S9), `9c7ecdb` (S10, S11),
  `8d2b325` (S14), `ee9efb2` (S16), `dd8d993` (S35 first half).
