# Planner handover — L-planner-0010 → next Planner context (2026-09-16 ~12:25Z)

## State
- **L-charter-0009 is `L1-complete`** (appended 12:24Z). Four slots, all `written`, nothing owed from a
  next Planner. The close — spec-audits, builds, grades, reviews, merges, sweep, charter-review, reap —
  is the Executor's. `L-spec-0020` (backend, 8 ACs), `L-spec-0021` (Promos page, 10), `L-spec-0022`
  (Sales, 4), `L-spec-0023` (verifier, 9, one owed AC9 = the production re-run, K=1).
- Cut: `content/cut-L-charter-0009.md` — four units, two waves. Audited `L-plan-auditor-0020`
  (`bad_cut: true`, 14) → `-0021` (`bad_cut: false`, 9) → plan-audit `-0022` (`bad_cut: false`, 9).
  All six pre-pass checks read clean at close. Plan: `content/plan-L-charter-0009.md` (nine sections).
  ADRs `L-adr-0021` (delta_pct changes meaning under its name), `-0022` (deploy-skew fallbacks),
  `-0023` (Pacific normalisation), `-0024` (pin @playwright/test — an acquisition).
- Research: `content/L-research-0004.md`. Recorded `spawn-failed` on a **path mismatch only** — the
  findings are complete and the card validates (R62). Read the path named here, not the ledger's stub
  `content/L-research-0003.md`, which is 0 bytes.

## The operator's ruling this session
**"Run charter 9 and then 20."** L-charter-0009 is done. **Next charter is L-charter-0020** (the
Planner relay: the pane ends itself at ⑦ and `doit up` supervises). Before starting it:
- The symlink `~/.do-it/repos/do-it-v2` **does not exist**. The charter names this as the Planner's
  first escalation. One operator act: `ln -s ~/do-it-v2 ~/.do-it/repos/do-it-v2`.
- `Covers: none` is legitimate — §12.2, an adopted project runs `goal: null`. Not an escalation.
- **Settle the version pin in Shared decisions before any spec is written.** `which doit` resolves to
  `/home/albert/do-it-v2/doit`, which sets `SRC="$HERE/src"` — the live working tree. Every pane,
  both Executors and the Planner, runs `~/do-it-v2/src/*.py` as it exists at the instant of
  invocation. Charter 20's footprint is `src/up.py`, `src/fold.py`, `src/think.py`,
  `agents/planner.md`. Builds are isolated in worktrees; the **merge** is not. A broken `fold.py` does
  not crash — it makes the board omit work, which is exactly what R58/R59 cost (a spec merged with no
  reviewer ever dispatched). The operator's own framing for this is L-charter-0017's: one writer, and
  adopting new code is a deliberate act, not a side effect of a merge.
- The operator asked whether charter 20 can run under a **separate Executor**. Answer given: yes at the
  repo level (different repo, no shared file, no deploy lock, `DOIT_PROJECT` already scopes a lane,
  ids are `O_EXCL`), but `board.md` is a single global path (`fold.py:20`) that two folds would clobber,
  and `tick.py` is a per-ledger singleton by design ("One at a time per ledger — flock"). Not ruled.

## Traps measured this session (pilot record R62–R65)
- `doit alloc` and `doit dispatch` allocate from **two different counters**; they had drifted by one, the
  contract wrote to its spawn-id path, and the wrapper failed a good spawn on the mismatch (R62). The
  repair is operator-only — `correction` is restricted to `actor: operator` (`fold.py:159`).
- Three audit rounds returned **32 findings with nil overlap** and no decay (14 → 9 → 9). The two that
  would have shipped broken were invisible to the pre-pass: the response model living in
  `promo_effect_v2.py` (a file in no footprint, so every new field would be computed and never
  serialised), and `mtdDays` having two consumers reading it as different units (`sales-view.tsx:730`
  days, `:652` rows), so fixing R6's number would have silently shrunk a working tile on every load.
  Evidence against extending the one-round blind-audit cap from specs to cuts (R63).
- `seat/<spawn>.cmd.json` records a `claude -p` line it **never runs** (`dispatch.run_seat` does not
  spawn). Alarming on sight in a project that bans `claude -p`; cost one mid-charter investigation (R64).
- **Two panes served the same packets concurrently** — two of four spec-writers were double-served,
  burning 229K and 196K sub-agent tokens producing discarded work, while a third sat unstamped for ten
  minutes with its waiter blocked. b-26 says serve what you dispatch; the other pane is still applying
  b-23's "serve everything" as R50's mitigation. Fix: an `O_EXCL` `seat/<spawn>.claimed` file (R65).
- `doit packet spec-writer` works on **round one** (no sibling specs to trip a-40's false positive).
  `doit packet` prints the path, not the content — capture it and `test -f` before dispatching (R54).
- The cut/Plan parsers: `audit.split()` separates signatures on **commas only** (a semicolon is not a
  separator, and a paren anywhere makes the whole line one signature) — use a bullet list per seam;
  `audit.acquisition()` reads only lines starting with `-`/`*`, so a markdown table passes vacuously
  and a bolded `**None.**` reads as a dependency named `None.`.

## Owed at close
Ten sweep questions, defaults in the Plan, none blocking; named for veto: Q1 (`delta_pct` changes
meaning under its name), Q4 (the deploy-skew window is accepted, so R4 is briefly unsatisfied on
production after merge), Q5 (two copy strings carved out of L-charter-0019), Q6 (`sales.py` deliberately
not touched, deviating from the charter's own footprint bound). **Q3 outlives this charter and should be
raised against the set:** the dashboard has no deployed-sha mechanism at all — no `NEXT_PUBLIC_*` sha, no
`VERCEL_GIT_COMMIT` anywhere under `dashboard/` — so no charter can prove the frontend it measured is
the frontend it merged. That lands on L-charter-0017's thesis, not only this one. Q9 is a brief against
L-spec-0019: its comment claims `@playwright/test` is installed and it is not.
