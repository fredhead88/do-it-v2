# Pilot retro — the ranked change list from S1–S35 and T1–T15

Written 2026-09-15 from a whole read of `albert-scott-pilot-2026-09-14.md` (895 lines: S1–S35,
"Smaller", T1–T15, both run logs) and a fold of `~/.do-it/events/*.jsonl`. This is Next Step 1 of
`v2-pilot-retro-model-split.md`, plus Step 3 (the measured table) and Step 5 (the two priced
spawns). Step 2 (the model map) is in this same commit: `models.example.toml`, `src/models.py`,
and the dispatch/tick changes.

Correction to the retro handoff's framing: the record runs to **S35**, not S14, and the ledger
holds **7 of 41** terminal events as `spawn-failed`, not 9 (the ninth and eighth were a
`spawn-started` with no terminal event and a mis-stamped spawn re-served as `-0005`).

## Ranking rule

Ranked by **minutes of wall clock or operator hands the item removes from charter 2 and 3**, as
measured in the record — not by how much design it touches (that was the record's own order).
Each line names the entries it closes. (a) = code change in `~/do-it-v2` with a test; (b) = a
design-doc or contract change; (c) = drop, with why.

## (a) Code changes, ranked

| # | Change | Closes | Measured cost in the pilot | Files |
|---|---|---|---|---|
| 1 | **The structured return is the Output — the pane never types a finding.** Every contract's `tools:` line carries `Write, Bash` on the seat backend so the sub-agent writes `seat/<spawn>.output.json` and runs `doit validate` itself (the sandbox is after-the-fact anyway, S4/S18); on the codex backend `--output-schema -o` removes the loop entirely (this commit). | S18, S28, S10, S29(5) | two transcription mistakes ≈ 15 min, one spawn on an empty fix list, findings trimmed by hand (the D120 safeguard failing at the wrong layer) | `agents/plan-auditor.md`, `agents/spec-auditor.md`, `agents/grader.md`; `dispatch.run_seat` |
| 2 | **The backend ruling lives in the ledger root, and the poke and the tick obey it.** `models.toml` per root; `poke()` never spawns `tick.py` when the executor's backend is not `claude-p`; `tick.main` refuses with a recorded `tick{refused}` event. **Done in this commit** (see Step 5 below for the breach it closes). | S32, S2 | $2.09 metered, three 30-minute unserved seat spawns, one ghost spec, two `brief-answered` for one brief, three operator events to close | `src/models.py`, `src/dispatch.py`, `src/tick.py` |
| 3 | **Model per contract, decided once per root, stamped as requested vs used.** `models.toml` → `{backend, model}` per contract; every terminal event carries `backend`, `model_requested`, `model_used`, `model_match`, `first_on_model` (the D120 trust run is derivable). Fable refused on any non-pane backend (the Agent tool substitutes Sonnet silently). `codex` backend: `codex exec -C <cwd> -m <model> --output-schema <schema> -o <output> --json`, sandbox per role, `OPENAI_API_KEY` stripped the way `ANTHROPIC_API_KEY` is. **Done in this commit**; the live D120 run per (contract, model) is the next charter's. | S5, S1 | 3 of 13 contracts never ran on their model; D120's rules calibrated on a model that never ran | `src/models.py`, `src/dispatch.py`, `models.example.toml`, `install.sh` |
| 4 | **`deploy.py` tells the truth on a live deploy.** (i) `live()` matches `sha[:7]` (the target prints 9, the code wanted 12 — S34); (ii) command output streams to `$R/logs/deploy-<spec>-<sha>-<n>.log` as produced, the event carries the path, never only a tail (S30/S34); (iii) refuse a second launch *before* opening any file the first holds (S30); (iv) exit 2 `deploy-refused{why}` when the gate refuses before touching the target vs exit 1 `deploy-failed` when the target changed (S8/S17); (v) `--rollback <cmd>` is the target's own rollback, run by the script on a failed `--check` — never `git revert` advice for a target failure (S30); (vi) a `deploy-started` with no terminal event renders on the board (S34). | S34, S30, S8, S17 | ~12 min of a live prod deploy with a false pending `deploy-failed`; the one error line of the first failed deploy lost; a whole `deploy.sh` log lost | `src/deploy.py`, `src/fold.py` |
| 5 | **`verify_script` lints the §9 block before writing the checker:** `bash -n`; no bare `;`, `||`, or newline-separated statement inside the gated chain; absolute interpreter; pin `BASE=<base_sha>` at build time instead of `git merge-base` at run time; refuse the packet on a violation. | S23, S31(a) | one full grade-bounce → spec rework 2 + builder rework (≈ 40 min); a post-merge re-grade that could not run the checker | `src/packet.py` |
| 6 | **Owed evidence can actually be owed, and a shipped spec can be closed truthfully.** `owed-ac` requires `wake_at` + the observation (schema + spec-writer contract); a grader `cannot-assess` on an owed row does not zero `confirmed`; fold derives `shipped-owed-evidence` from *confirmed over evaluable rows + owed rows with a future wake_at*; an `owed-met` event (operator or Executor citing evidence) folds into the verdict so `accepted` derives after merge; `spec-closed` on a `shipped` spec labels `closed-shipped`; an allocation with no `spec-written` after its spawn failed is `void` and does not bind the charter's L2 conjunct. | S15, S33 | two operator rulings + three operator events to close two charters the ledger already had every fact to close; a board line that misdescribed the pilot's headline result | `src/fold.py`, `agents/spec-writer.schema.json`, `agents/grader.md`, `src/dispatch.py` |
| 7 | **Packets carry the AC table and the diff, not the whole spec.** Builder, grader and reviewer each re-read the whole spec and the whole code path (S29-2). The builder packet carries the spec; the grader and reviewer packets carry the AC table + review paths + the diff summary. Pair with the contract cap in (b)-3 (spec ≤ 400 lines). | S29(2) | spec-writer 8.5–15 min, spec-auditor 9–11, builder 11–21 per spec; ≈ 2 h per small fix spec | `src/packet.py` |
| 8 | **Grader packet scrubs `Co-Authored-By` / `Claude-Session` trailers** from any commit text it hands over (or the AC10 shape points at the card and history only). | S31(b) | the builder's model leaked to the judge through the artifact under grade | `src/packet.py` |
| 9 | **The charter set is per goal, open charters are on the board, and `--land` needs a goal when there are two.** `think.land` builds the set from every `charter-filed` citing the goal (plus the ones passed); `doit think --audit-set <goal>` rebuilds and dispatches without landing; board `CHARTERS OPEN (n)` (charter-filed, no plan-written); `--land` refuses to diff without `--goal` when >1 goal is filed. | T11, S20, T12, T8 | the charter-set packet under-reported Goal A on every call; the driver asked the operator for a ruling without knowing three charters had landed; the pane rebuilt the packet by importing `think.py` | `src/think.py`, `src/fold.py` |
| 10 | **Grant-scoped porcelain.** Compare the spawn's *grant* (paths it may read and must not write) — or hash the granted tree — instead of whole-tree `git status`; `DOIT_REPO_VOLATILE` stays as the declared-volatility fallback. | S11 | one 15-minute audit voided by a cron; volatility must be re-declared per project | `src/dispatch.py` |
| 11 | **A rework packet with no fix list is refused; a killed wrapper leaves no ghost.** `doit packet spec-writer` (rework) refuses when the subject has a spec-auditor `spawn-done` and zero `audit-finding` since the last `spec-written`; `dispatch --seat` writes `spawn-abandoned` on SIGTERM. | S28 | one spawn dispatched on an empty fix list; a `spawn-started` with no terminal event | `src/packet.py`, `src/dispatch.py` |
| 12 | **A subject's events are the subject's project.** `subject_project()` wins over `cwd.name` whenever the subject has a prior event; `dispatch` warns loudly when `cwd.name` differs from the subject's project; board `IGNORED (other project) (n)`. | S35(2) | an 11-minute Opus charter review invisible to the filtered board; one re-dispatch (~15 min) | `src/fold.py`, `src/dispatch.py` |
| 13 | **Sidecar objects for every role whose Output carries arrays the next role must test** — reviewer `blocking`/`reverify`, charter-reviewer `unrolled_not_built` — beside the render, as `write_card` now does for the builder. | S14 | rows 7+ lost to the fifteen-line render on the first chain (fixed for the builder only) | `src/dispatch.py` |
| 14 | **`packet.py` builds the Planner's packets too** — plan-auditor at all three stages, `spec-writer --slot` round one — with a strip list for the Planner's rationale; packet builders read term-typed declaration events. | S6, S21 | rationale-blindness was manual; a plan audit ran without the cut audit's declaration lines | `src/packet.py` |
| 15 | **`doit ingest <pr>`** — performs the human-gated fetch and appends `spec-written{charter: null, author: <prefix>}`; the human gate is a `decision` event on the ledger the command checks, not a flag. | T7, T13 | Yitzy's 1448/1449 sit numbered with no v2 path; "human-only" mis-read as "human keystrokes" | new `src/ingest.py` |
| 16 | **Small, each with a one-line test:** `--packet ""` refused before allocating; `doit alloc` validates `kind` and honours `-h` (S19, T2); `audit.fields` treats `none`/`—` as empty (S7); `install.sh` creates `repos/` and verifies the contracts load as Agent types (S4, "doctor"); `doit append` prints the event line only; `doit up --print-only` allocates nothing; packet refusals exit non-zero on stderr (S24 — verify: `die()` already uses `sys.exit(str)`); fold counts role-switches per session id on HEALTH (S3). | Smaller, S19, T2, S7, S4, S24, S3 | tracebacks, one junk content file, ~25 lines of board noise per append | `src/dispatch.py`, `src/fold.py`, `src/audit.py`, `install.sh`, `src/up.py`, `src/packet.py` |
| 17 | **Verify T14:** a `decision` on a charter another pane drives surfaces under `DECIDED WITHOUT YOU` for that pane on its next fold. A test, then a fix only if it fails. | T14 | unknown — the pilot could not wait to see | `src/test_fold.py` |
| 18 | **Launchers read the model map.** `up.pane_cmd()`/`think.pane_cmd()` pass `--model` from `models.toml`; pane contracts lose their frontmatter `model:` line (or it is generated). | R8 | two panes opened on the wrong model but for a hand-typed flag | `src/up.py`, `src/think.py`, `agents/thinker.md`, `agents/planner.md` |
| 19 | **Operator event shapes.** `fold.append` carries a required-field table per operator event type (`decision`, `owed-met`, `correction`, `brief-answered`) and refuses a miss; `doit alloc` refuses a bare or flag-shaped kind. | R11 | one `?` on the board + one correction; one junk content file | `src/fold.py`, `src/dispatch.py` |
| 20 | **Serving is observable.** `usage.py` supplies turns and duration to `stamp.sh` from the transcript; the wrapper writes a `seat-unserved` alarm when a packet has no `.meta.json` after N minutes. | R15 | hand-typed numbers per spawn; an unserved packet is silent | `src/usage.py`, `scripts/seat/stamp.sh`, `src/dispatch.py` |
| 21 | **One brief shape.** `fold.append`'s field table gains `brief`: `fact`, `footprint` (a list of paths), `blocked_me`, `hit_while`; `why`/`footprint_hint` refused or mapped; the a-19 table one type over. | R18 | 11 briefs one shape, 4 another; dedupe by footprint was a hand read | `src/fold.py`, `src/validate.py`, `agents/executor.md` |
| 22 | **The inbox is on the board and a tag is an event.** Board `INBOX (n)`: briefs with no `requirement` and no `brief-answered`, deduped by footprint, oldest first; a `blocked_me: true` brief with no `requirement` sits under NEEDS YOU until cited or answered. New event `brief-triaged{ref, tag, writeup}` on the thinker's and operator's lists; `doit think --land` accepts a triage write-up and appends one per tagged brief. | R17 | 10 open adjacent facts invisible; one delivered brief unstampable by the seat that found it | `src/fold.py`, `src/think.py`, `agents/thinker.md` |
| 23 | **A looked-at cursor.** `looked{actor}` appended by the pane on open (or `doit --since <ts>`); `DECIDED WITHOUT YOU` and `SHIPPED SINCE YOU LOOKED` render only what is newer than the reader's last cursor, the rest as a count. | R21 | ~5 KB of acted-on decisions re-read on every fold | `src/fold.py`, `agents/thinker.md`, `agents/planner.md` |
| 24 | **`doit append decision` validates `ref` against the subject's open questions.** When the subject has an open `question` (a `question` event with no `decision` whose `ref` equals its `src`), a `decision` append is refused unless `ref` equals one of those `src` values (or the subject has no open question, in which case `ref` is free-form as today, e.g. citing an AC or a design point). Turns a silently-stale NEEDS YOU row into a refusal at write time. | R22 | a ruling shipped and deployed a day before the fold stopped claiming it was unanswered; one session's worth of re-investigation | `src/fold.py`, `src/dispatch.py`, `src/validate.py` |
| 25 | **`doit packet probe`.** The eighth role: charter verbatim, externals, N, credential names, the run directory, the record shape, the read-only rules — and the same `strip()` (no cut, no Plan, no architecture) every other packet gets. | R25 | four hand-built packets, 347 lines, unchecked by `strip()` | `src/packet.py` |
| 26 | **A detached dispatch logs per spawn.** `--detach` names its log by role + spawn id (allocate before the fork, or rename after), never by the second. | R26 | four wrappers interleaved in one file | `src/dispatch.py` |
| 27 | **Probe packets state checkout-vs-deployed drift per path.** `doit packet probe` runs `git diff --stat <deployed> <checkout> -- <paths under test>` in the script pre-pass and prints the result per external; an empty diff is ground truth the probe cites instead of declaring a gap. | R29 | one charter-gap declared and closed by hand for a fact a script has | `src/packet.py` |
| 28 | **`--print-only` never appends.** `think.land(print_only=True)` runs `check()` on every file and prints the coverage diff and the audit command, and appends nothing; a test asserts the ledger is byte-identical after a dry run. | R31 | six duplicate `charter-filed` events, six corrections | `src/think.py`, `src/test_think.py` |
| 29 | **The wrapper self-stamps a served spawn.** `dispatch --seat`: when `seat/<id>.output.json` validates and no `.meta.json` appears within N minutes, the wrapper runs the stamp itself from the transcript `usage.py` resolves (or stamps `unmeasured`), appends the terminal events, and logs that the serving pane skipped the stamp. | R44 | one audit served, unstamped, folded by hand from the file | `src/dispatch.py`, `scripts/seat/stamp.sh` |
| 29 | **`research` default timeout 15 min** (`ROLES["research"]`), or the packet passes `--timeout`; a spawn that finished its file after the wrapper gave up is an undeclared success the ledger cannot see. | R33 | one 5-min timeout on a 6-min map; a usable file with no `research-done` | `src/dispatch.py` |
| 30 | **`probe-run` carries `summary`** beside path/externals/n_inputs/spend/complete, so the charter's event stream says what the probe found, not only where it looked. | R34 | five probes' findings readable only from `spawn-done` scalars | `src/dispatch.py` |
| 31 | **`evidence` covering every charter R derives L1** for a charter with zero specs (the operational lane, b-7), or the fold flags "evidence complete, no l1-complete" on the board; today L1 waits on a Planner event nothing reminds it to write. | R37 | one operational charter, closed by hand | `src/fold.py` |
| 32 | **A research packet template for a cut's footprint**: for every function a unit touches — its call sites (file:line), whether it is pure or reads state, the generated files its output feeds — so the Planner cuts on topology, not pointers. | R38 | two Opus cut-audit rounds (8 min) spent on call-site facts | `src/packet.py` (research), `agents/research.md` |
| 33 | **The pre-pass refuses a footprint entry that resolves to nothing** (a glob or path with no file on disk) instead of folding it into "unmeasurable" only when every entry misses; a new-file entry is declared with a `(new)` suffix. | R38 | `test_1346*.py` in a cut that two audits read | `src/audit.py` |
| 34 | **`packet.py` reads an empty `Produces:` as empty**, never the next line; a sibling with nothing to produce is listed as "produces: nothing". | R39 | four spec-writer packets carrying `produces: Wave: 1` | `src/packet.py` |
| 35 | **Probe residue by reference.** When a charter's Constraints name a document as its probe (D96 by reference — "the audit report is the probe"), the spec-writer packet's `Probe residue` line carries that path and the Plan's Probe findings section, not "none on file". | R39 | four packets telling writers there is no probe on a probe-backed charter | `src/packet.py` |
| 36 | **Role timeouts from measured p95.** `ROLES[...]` timeouts are set from `doit spend`'s per-role durations (spec-auditor 5.7–7.5 min actual vs 15 min cap tripped; research 6 min vs 5), or the wrapper accepts a VALID Output that lands after its wait instead of voiding it. | R46 | two voided-then-corrected spawns in one day | `src/dispatch.py` |
| 37 | **The seat scan tells a dead spawn from a pending one.** A `seat/<spawn>.packet.md` is pending only while its spawn has `spawn-started` and no terminal event; the dispatcher marks or removes the packet on `spawn-done` / `spawn-failed`, and the `seat-unserved` alarm (a-20) and the Planner's serving rule read that, not the directory. | R47 | five dead packets listed as unserved at 13:10Z; a literal reading of the contract would have spawned five stale contracts | `src/dispatch.py`, `agents/planner.md`, `scripts/seat/README.md` |
| 38 | **`SEAT PENDING (n)` is a standing board block.** Every seat spawn with `spawn-started`, no terminal event and no `seat/<spawn>.output.json`, oldest first with its age and dispatcher — the fold's cross-pane serving trigger (a-20's alarm as a board line, not a threshold), so a pane that serves foreign packets sees them without reading the directory. | R50 | two Planner spawns expired unserved at 0 tokens (15 + 15 min of stage clock) while the serving pane stamped only its own | `src/fold.py` (board), `scripts/seat/README.md` |

## (b) Design-doc and contract changes, ranked

| # | Change | Closes | Where |
|---|---|---|---|
| 1 | **§4.2/§3.1: the backend is a first-class seam with three named values — `claude-p`, `seat`, `codex` — chosen once per ledger root in `models.toml`.** Under `seat` the Executor is a standing pane again and the tick is a poke that wakes it (S2 option a); D117 holds only for roots whose executor backend is `claude-p`. State which role collapses seat-only permits (Planner+Executor in one pane is permitted; Thinker+anything is not) and that the fold counts them (S3). `cost_usd: null` renders *unmeasured*, never `$0.00`. | S1, S2, S3, S5 | §3.1 D117 block, §4.2, §4.4 Model row, §4.5 library table |
| 2 | **§4.5 library table: the Model column becomes the map's *default*, with the rule that a judge never shares its author's vendor** (spec-writer/builder on Codex ⇒ spec-auditor/grader on Claude). Correction #4 ("cross-vendor is moot, one meter") is un-mooted: there are two meters now. D120's re-trust is keyed on `(contract, model_used)` and the first stamped `first_on_model` run is that trust run. | S5, D120 | §4.5, §4.4 corrections table row 4 |
| 3 | **Spec size cap in the spec-writer contract and the spec-auditor's grep:** a spec is ≤ 400 lines or it is split; the 578- and 700-line specs were the cost, not the audit round (S29-1). And a rework that changes one literal is an Executor `decision` + edit, not a spawn (S29-3, amend D116's "the Executor never edits a spec" with the recorded-decision carve-out the pilot already used). | S29(1), S29(3) | `agents/spec-writer.md`, `agents/spec-auditor.md`, §3.9 |
| 4 | **The `Writes:` grant shape is stated in the spec-writer contract and checked by the spec-auditor** (paths on the line, or one per line under it, nothing else). | S16 | `agents/spec-writer.md`, `agents/spec-auditor.md` |
| 5 | **Builder contract names where evidence files go:** `content/<spec>-…`, never `seat/`. | S24 | `agents/builder.md` |
| 6 | **A charter Intent that names a production state carries a probe (D96), even when the external is our own box** — `/version` is a claim, `find -newer` is a measurement. Thinker's five sections get a "measured, not read" requirement for the premise. | S22 | `agents/thinker.md`, §3.4 |
| 7 | **Operational charter shape:** a charter with no code unit — the Executor or operator is the builder, the card is an evidence file; its lane is `evidence` → `l1-complete` → sweep → charter-review → L2 (the fold half is fixed, `dd8d993`). | S25, S35(1) | §3.4, §3.13, `agents/executor.md` |
| 8 | **Goal model:** `Delivered by: L-charter-NNNN` on a goal requirement that the diff reads (pre-goal and cross-goal delivery stop being permanent false findings); requirement ids namespaced by goal or `Covers:` takes `goal:`; the goal shape's date line says *the date orders concurrent charters*; the Thinker re-measures "waiting on us" from the far end (delivered-to-prod) and carries a "re-measure on the new sha" requirement for adopted projects with a deploy backlog. | T5, T9, T8, T1, T4, T6, S12 | `agents/thinker.md`, §2.5 D98, §3.3 |
| 9 | **Executor contract rows:** deploy refused for an external → `blocked` + `brief`, never revert (S17); rework packet with zero findings is impossible (S28); the Executor never hand-writes a checker — `verify_script` is the place (S23). | S17, S28, S23 | `agents/executor.md` |
| 10 | **Thinker re-folds before answering anything about state after an idle gap**, not only on the first message. | T15 | `agents/thinker.md` |
| 11 | **Planner contract D96:** a probe whose external is CI runs *on* CI (`gh workflow run … full_run=true` is the canonical enumerate-reds probe for this repo); a done-condition of "check-run green" is that enumeration, not one red per charter. Cut-file docs show the no-seam form (empty `Consumes:`). | S26, S27, S7 | `agents/planner.md` |
| 12 | **Pilot-record convention:** every pane role that runs during a pilot appends its own section keyed by ledger actor id, never interleaved. | T3 | `docs/handoffs/` README line |
| 13 | **Thinker §3 carries a required deploy line** — how the charter's result reaches the surface §4 names; `think.py` may refuse a §4 host with no §3 deploy line. | R9 | `agents/thinker.md`, `src/think.py` |
| 14 | **A v2 change is a build**: the driver cuts the worktree of `~/do-it-v2` and names it in the sub-agent brief; the harness's `isolation: worktree` pins to the cwd repo. | R12 | `scripts/seat/README.md` |
| 15 | **§7.9 / thinker: a triage tag is an event, not prose.** The write-up cites the `brief-triaged` events it produced; archive is the `tag: archive` value on that event, reversible by a later one. | R17 | `design/system-design-v2.md` §7.9, `agents/thinker.md` |
| 16 | **Operational charter: the deliverable lands as `evidence{path, covers}`, and the charter says so.** The Thinker's operational-charter Constraints name the landing event; the probe contract may declare `evidence`; a pane waiting on it reads `doit events <charter>`, not `find -newer`. | R20 | `agents/thinker.md`, `agents/probe.md`, `agents/executor.md`, b-7's lane text |
| 17 | **`executor.md`'s dispatching section states the `--check` shape for a `box:droplet` target: SSH-wrapped** (`ssh $SERVER "curl -sf http://127.0.0.1:<port>/version"`), matching `deploy.sh`'s own rollback-block convention (deploy.sh:785) — never a bare public-URL curl, which 404s or 401s depending on which host actually answers `/version`. | R23 | a pane driving its first deploy would have to find the convention in a 3,900-line script instead of the contract that tells it to dispatch the deploy | `agents/executor.md` |
| 18 | **`planner.md` ⓪ matches the wrapper:** `doit alloc probe --dir` allocates the run directory, `--cwd` is that directory (outside every repo, `dispatch.py:98`), one probe per external-credential pair. | R24 | `agents/planner.md` |
| 19 | **`planner.md` gains the operational-charter branch** (pairs with b-16): when the charter's Constraints say the deliverable is a record, the cycle is ⓪ probe(s) → the cut and Plan written as records (no unit blocks, no audit spawn — `doit audit` refuses or no-ops on a zero-unit cut instead of reporting every R uncovered; `audit.sizes()` reports an all-new-file footprint as "evidence unit", not "unmeasurable") → `evidence{path, covers}` → `l1-complete`. | R27 | `agents/planner.md`, `src/audit.py` |
| 20 | **`probe.md` requires the read-only proof in the run record** when the credential it holds has write scope: the session-level `SET default_transaction_read_only = on` issued as the first statement of every connection (the startup `PGOPTIONS` parameter is silently ignored by the Supabase pooler, R34) and its `SHOW` output pasted once per session; a root shell's command list captured verbatim. | R28 | `agents/probe.md` |
| 21 | **Thinker Intent names the newest record for every id it cites.** `agents/thinker.md` §1: for each brief/spec id in the Intent, the newest v4 ledger record citing it and its status; the plan-auditor's charter-set stage gains `stale-source` as a finding kind when a cited record is older than a shipped successor. | R30 | B1189 chartered as open two weeks after its fix shipped; 827's policy chartered as undecided after a spec decided it | `agents/thinker.md`, `agents/plan-auditor.md` |
| 22 | **`probe.md`: measurement time is captured, never typed** — each raw file begins with a `date -u` line and the run record's `measured_at` is copied from it; the consolidator cites the ledger window. | R32 | `agents/probe.md` |
| 23 | **The Thinker serves the audit its own `--land` produced; the Planner serves everything else.** Operator ruling 2026-09-15: the reviewer is blind by construction (the packet strips rationale, the sub-agent has no memory of the pane), so which pane presses spawn changes nothing about blindness; D73's rule was written for `claude -p` and a standing pane. `agents/thinker.md`: Agent tool allowed for exactly the seat packets `think.land` printed, no other spawn; `agents/planner.md`: serves every other pending packet under `seat/` (a-20's `seat-unserved` alarm is the trigger); §3.3 restated. | R35 | **Done 2026-09-15** (`agents/thinker.md`, `agents/planner.md`; a pane opened before the edit runs the old contract until restarted, R2) — a charter-set audit sat unserved between two panes until the operator asked | `agents/thinker.md`, `agents/planner.md`, `design/system-design-v2.md` §3.3 |
| 24 | **A landing names its order and is bounded.** `agents/thinker.md`: a charter set landed against a goal states which charter the Planner cuts first and why; a landing of more than three charters carries a one-line queue order in the charter-set write-up, or `--land` refuses. `agents/planner.md`: cut and plan charters in parallel when their footprints do not intersect (the intersection script already exists); serialise only on shared files. | R43 | twelve charters, one cut, operator read it as ten behind | `agents/thinker.md`, `agents/planner.md`, `src/think.py` |
| 25 | **Owed questions and named-for-veto rulings are events**: `escalation-owed{charter, q, default, revert}` on the Planner's EMITS, a board block `OWED QUESTIONS (n)` with defaults, answered by an operator `decision` whose `ref` is the question's `src`; the Plan cites the events. | R46 | nine owed questions and six rulings invisible to the board; operator asked in-pane | `design/system-design-v2.md` §8.7, `agents/planner.md`, `src/fold.py` |
| 26 | **The serving pane's `tools:` line carries `Agent`.** b-23 names the Planner as the pane that serves every seat packet; `agents/planner.md`'s `tools:` omits `Agent`, so the rule cannot run there. Either the Planner's contract gains the tool or b-23 names the Executor; the launcher and the contract say the same thing. | R49 | one pane booted to serve and could not; serving re-routed by operator prompt | `agents/planner.md` §Input, `agents/executor.md`, D121 |

## (c) Dropped, with why

| Entry | Why dropped |
|---|---|
| T10 | The record says so itself: nothing to change, the ledger did its job. |
| S27's local enumeration | Already discarded in the pilot; the systemic half lives in (b)-11. |
| S26 fail-fast CI pairing as a v2 change | Albert Scott's CI plugin, not v2's; filed on their side as a brief. v2 keeps only the Planner probe rule (b)-11. |
| S22's Vercel-login red | Project-side (Yitzy's GitHub↔Vercel link); a goal requirement, not a v2 change. |
| "Smaller" `.venv` note | Project-side; the spec's absolute interpreter already handles it. |
| S3 beyond a HEALTH count | Policing role collapse structurally under seat-only would re-introduce the panes the design deleted; the count is enough and (b)-1 says which collapses are permitted. |
| S13, S9, S11-pilot, S14-builder, S16, S35(1) | Already fixed in the repo (`c1ceab5`, `a8b7b76`, `9c7ecdb`, `8d2b325`, `ee9efb2`, `dd8d993`); listed above only where a systemic half remains. |

## Step 3 — measured per role (41 terminal events, 2026-09-14, every spawn on `claude-opus-5`)

Durations from `spawn-done.duration_ms`; tokens are the seat sidecar's `subagent_tokens`
(the events' `input_tokens` are null on the seat route — one more reason for item (a)-3's
stamping).

| Role | n | failed | median min | max min | median turns | median k-tok | max k-tok | Target (proposed) |
|---|---|---|---|---|---|---|---|---|
| spec-writer | 12 | 5 | 8.5 | 15.3 | 12 | 135 | 194 | ≤ 6 min, spec ≤ 400 lines, 0 format retries |
| builder | 5 | 0 | 10.8 | 21.4 | 26 | 135 | 155 | ≤ 12 min |
| spec-auditor | 3 | 0 | 8.8 | 10.6 | 21 | 154 | 208 | ≤ 6 min |
| charter-reviewer | 5 | 1 | 10.0 | 11.5 | 23 | 97 | 155 | ≤ 8 min |
| grader | 5 | 0 | 6.5 | 7.1 | 14 | 82 | 96 | ≤ 4 min |
| reviewer | 3 | 0 | 4.2 | 5.9 | 17 | 87 | 97 | ≤ 5 min |
| plan-auditor | 6 | 1 | 3.2 | 4.0 | 11 | 112 | 117 | ≤ 3 min |
| executor (tick, metered) | 2 | 0 | — | — | 18 | 10 | 12 | 0 — never metered on this root |

Failure causes, all seven: 3 × timeout-unserved (the rogue tick's seat spawns, S32), 2 × schema
(the wrapper read a draft, S13 — fixed), 1 × whole-tree porcelain (S11), 1 × timeout
(charter-reviewer-0001, also the rogue tick's). **None was a model failure.** Every failure was
the wrapper or the driver, which is why (a)-1/2/10 outrank any model change for wall clock.

Per-spec end-to-end as recorded: L-spec-0002 ≈ 2 h 10 min, L-spec-0003 ≈ 2 h, plus 30–45 min
CI per merge. Target for charter 3: **≤ 60 min per spec write→merge**, with the audit rounds
kept (they found real defects every time) and the savings taken from (a)-1, (a)-7, (b)-3.

## Step 5 — the two priced executor spawns: a ban breach, mechanically explained

Both `L-executor-0003` ($1.02, 11:27:54Z) and `L-executor-0004` ($1.07, 12:00:17Z) are the
tick's own `claude -p --agent executor` line. Their transcripts exist under the ledger-root
project slug (`~/.claude/projects/-home-albert--do-it/3af2e834…` at 11:25:59Z and
`4fd5ad56…` at 11:57:35Z, `entrypoint: sdk-cli`, cwd `/home/albert/.do-it`, model
`claude-opus-5`). The two `tick{spawned: true}` events sit at 11:25:58Z and 11:57:34Z.

Nobody typed `doit tick` — no transcript on this box carries it as a command. The mechanism:

1. `L-spec-auditor-0003`'s wrapper finished at **11:25:58Z, the same second as the first tick**.
   `dispatch.main` registers `poke()` with `atexit`, and `poke()` is guarded only by the
   `DOIT_NO_POKE` environment variable. That wrapper ran in a shell without `env.sh` sourced, so
   the poke spawned `tick.py`, which spawned the metered Executor.
2. That Executor's own detached `spec-writer` dispatch (`L-spec-writer-0011`, started 11:27:34Z)
   went seat (its `dispatch-spec-writer-…112734` log shows the seat line), inherited an
   environment with no `DOIT_NO_POKE`, timed out unserved at exactly 30 minutes, **11:57:34Z**,
   and its own atexit poke ran the second tick.

So: a breach of the `claude -p` ban, twice, $2.09, caused by a per-shell environment variable
standing in for a per-root ruling. The wrapper does not mis-stamp — those events have no
`spawn_path` because `tick.py` writes its own terminal event and never set one. Fix, in this
commit: `models.toml` is the root's ruling, `poke()` and `tick.main` read it and refuse; the
tick's refusal is a recorded `tick{refused}` event. The environment variables stay as a
belt-and-braces guard, never as the only one.

## What this commit changes (Step 2, done)

- `models.example.toml` — the decided map, argued in the retro handoff's Session Log; copied to
  `~/.do-it/models.toml` on this box.
- `src/models.py` — loader + resolver; refuses Fable on any non-pane backend, refuses dispatching
  a pane role, refuses an unknown backend or contract.
- `src/dispatch.py` — the backend comes from the map (flags/env may only agree); a `codex`
  backend (`run_codex`); every terminal event stamps `backend`, `model_requested`, `model_used`,
  `model_match`, `first_on_model`; `poke()` obeys the map.
- `src/tick.py` — refuses, with a recorded event, when the root's executor backend is not
  `claude-p`.
- Tests in `test_dispatch.py` and `test_tick.py`; `./doit test` green.

Not done here, and named so nobody reads this as done: the live D120 trust run of each contract
on its mapped model (Sonnet judges, Astra writers) — that is the first thing charter 3 measures;
the reviewer/charter-reviewer on Codex waits for the client walkthrough report
(`docs/sessions/2026-09-15-profit-v2-client-walkthrough.md`) before it is trusted.
