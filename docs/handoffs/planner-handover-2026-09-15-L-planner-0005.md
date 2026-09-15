# Planner handover — L-planner-0005 → next Planner context (2026-09-15 ~12:55Z)

## State (ledger is truth; `doit` from `~/.do-it/repos/albert-scott`)
- **L-charter-0010** `L1-complete` 11:12Z (operational, zero units). Write-up `content/evidence-L-charter-0010.md` (landed as `evidence`), Plan `content/plan-L-charter-0010.md`, five probe run dirs `content/L-probe-0001…0005/`. Four dead-item `decision`s on L-goal-0001 + three operator rulings recorded 12:0xZ (1178 restore proceeds under L-charter-0014; the 1316-chat and 1335-load writes authorized; the pane's rulings stand). Executor owes: sweep-fixpoint, charter-review, close.
- **L-charter-0005** `L1-complete` 11:55Z. Cut `content/cut-L-charter-0005.md` (third cut), Plan `content/plan-L-charter-0005.md`, ADRs L-adr-0006/0007. Specs `written`: L-spec-0007 (backend totals + allocator, wave 1), L-spec-0008 (frontend render, wave 1), L-spec-0009 (tie-out verifier, wave 1), L-spec-0010 (advertising figure, wave 2, last merge, one owed AC9 = R7 on production, wake_at 2026-09-16T12:00Z). The Executor has already started spec-audits (one 15-min timeout corrected by the operator at 12:52Z).
- **Operator rulings (12:0xZ):** next charter = **L-charter-0006**; all pane rulings stand; 1178 restore proceeds under 0014; both writes authorized.
- **Owed to the operator:** one `correction` per malformed decision at `L-planner-0005.jsonl:4-7` (commands in the session reply).

## Traps measured this session (pilot record R24–R28, R32–R34, R36–R40, R46 (R41–R45 are other panes'); change list a-25…a-36, b-18…b-23, b-25)
- Probe: `doit alloc probe --dir`, `--cwd <run dir>`; no `doit packet probe` (hand-build; no cut/Plan in it); require `date -u` in raw files; `SET default_transaction_read_only = on` as the first statement (PGOPTIONS is ignored by the pooler); no read-only DSN exists.
- Research role times out at 5 min; spec-auditor at 15 — a VALID Output after the wait is voided; the operator corrects.
- On a monolith the cut-audit needs two rounds; cut on call-site topology (ask research for call sites / purity per touched function); every footprint glob must resolve (`test_1346*.py` matched nothing).
- `doit append decision` needs `why=` as its own k=v; a quoted blob lands in one field and renders `?`.
- Sibling `Produces:` in spec-writer packets reads the next line when empty; `Probe residue: none on file` on a charter whose probe is a document.
- Owed sweep questions are not events; nothing on the board shows them.

## Next Planner (L-charter-0006, Bridge and Cash proofs: B6/B7/B8-Sales)
Read `content/L-charter-0006.md` and its `charter-revised` events; shared-file lines: `api/app/routers/sales.py` and `components/sales/sales-view.tsx` are shared with 0009; `day_detail.py` / overview are 0005's until its specs merge — check `doit states` for L-spec-0007…0010 before cutting anything that touches them. The audit report is the probe (D96 by reference); its Bridge/Cash tie-out tables carry the numbers. Two Opus cut rounds are likely on `bridge_reconciler.py` / `cash_*` — commission `research` for call sites first, with `--timeout 15`.
