#!/usr/bin/env python3
"""Checks on `doit audit` — the deterministic pre-pass under both fable audits.
Run: python3 test_audit.py

Every check here has its negative, because the failure this script exists to
prevent is a check that reads as clean when it could not run: no `--repo` and the
size heuristic says "none"; a unit with no `Wave:` and the overlap check says
"none". Both are `undetermined` here, and the wrapper refuses a plan-auditor
packet that carries no block at all.
"""
import os, pathlib, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import audit, fold, think  # noqa: E402

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


CUT = """# cut for L-charter-0001

## enum-states
Goal: settle the Transaction status states
Delivers: R1
Footprint: src/model.py
Produces: TxStatus enum
Wave: 1

## refund-flow
Goal: refunds
Delivers: R2
Footprint:
- src/refund.py
- src/model.py
Consumes: TxStatus enum
Produces: refund(tx, amount) -> Receipt
Wave: 2

## cancel-flow
Goal: cancellations
Delivers: R3
Footprint: src/cancel.py
Consumes: TxStatus enum
Wave: 2

## Notes
Not a unit: no Footprint line.
"""
us = audit.units(CUT)

# ── the parse: a block is a unit when it declares a footprint ────────────────
ok([u["name"] for u in us] == ["enum-states", "refund-flow", "cancel-flow"],
   f"a heading with no `Footprint:` is not a unit — a preamble must not become one: {[u['name'] for u in us]}")
ok(us[1]["footprint"] == ["src/refund.py", "src/model.py"],
   f"a label's list continues the label: {us[1]['footprint']}")
ok(us[1]["produces"] == ["refund(tx, amount) -> Receipt"],
   f"a signature keeps its commas; only ids and paths split on them: {us[1]['produces']}")
ok(us[0]["wave"] == 1 and us[2]["delivers"] == ["R3"], f"wave and delivers parse: {us[0]}")
try:
    audit.prepass("cut", "# a cut with no unit blocks\n")
    ok(False, "a cut file declaring no unit must be loud, not an all-clear")
except SystemExit as e:
    ok("Undetermined is never clean" in str(e), f"and it says why: {e}")

# ── 1 · same-wave footprint overlap ─────────────────────────────────────────
found, und = audit.overlap(us)
ok(und is None and found == [], f"wave 1 and wave 2 may share a file — across waves it is unlimited (§3.7): {found}")
clash = audit.units(CUT.replace("Footprint: src/cancel.py", "Footprint: src/refund.py"))
found, und = audit.overlap(clash)
ok(len(found) == 1 and "src/refund.py" in found[0] and "wave 2" in found[0],
   f"two units in ONE wave touching one file is the overlap this check exists for: {found}")
found, und = audit.overlap(audit.units(CUT.replace("Wave: 2\n\n## cancel", "\n\n## cancel")))
ok(found == [] and und and "no `Wave:`" in und,
   f"a unit that cannot be placed in a wave makes the check undetermined, never clean: {(found, und)}")

# ── 2 · the seam graph ──────────────────────────────────────────────────────
found, und = audit.seams(us)
ok(found == [] and und is None, f"every Consumes has a Produces, and the producer is earlier: {found}")
found, _ = audit.seams(audit.units(CUT.replace("Produces: TxStatus enum\nWave: 1", "Wave: 1")))
ok(len(found) == 2 and all("no unit produces it" in f for f in found),
   f"a Consumes nobody produces is `seam-undefined`, once per consumer: {found}")
late = CUT.replace("## enum-states\nGoal: settle the Transaction status states\nDelivers: R1\n"
                   "Footprint: src/model.py\nProduces: TxStatus enum\nWave: 1",
                   "## enum-states\nGoal: settle the Transaction status states\nDelivers: R1\n"
                   "Footprint: src/model.py\nProduces: TxStatus enum\nWave: 3")
found, _ = audit.seams(audit.units(late))
ok(len(found) == 2 and "produced only in wave 3" in found[0],
   f"a seam produced after it is consumed is defined and still unbuildable: {found}")

# ── 3 · a shared name introduced twice with no owner ────────────────────────
found, und = audit.shared(us, None)
ok(found == [], f"one producer is one owner: {found}")
two = CUT.replace("Footprint: src/cancel.py\nConsumes:",
                  "Footprint: src/cancel.py\nProduces: TxStatus enum\nConsumes:")
ok(len(audit.producers(audit.units(two))["TxStatus"]) == 2, "the fixture really has two producers")
found, _ = audit.shared(audit.units(two), None)
ok(len(found) == 1 and "re-touches" in found[0],
   f"two producers in DIFFERENT waves is not an extract either — an extract has exactly one "
   f"producer (§3.7), and the later unit re-touches what the earlier landed. The first real "
   f"plan-auditor run rejected the earliest-wave-wins rule this replaced: {found}")
ok(audit.shared(audit.units(CUT.replace("Consumes: TxStatus enum\nWave: 2\n", "Wave: 2\n")), None)[0] == [],
   "one producer and any number of consumers is the extract, and it is silent")
same = two.replace("Produces: TxStatus enum\nWave: 1", "Produces: TxStatus enum\nWave: 2")
found, _ = audit.shared(audit.units(same), None)
ok(len(found) == 1 and "TxStatus" in found[0] and "no owner" in found[0],
   f"two producers in ONE wave own nothing between them: {found}")
found, _ = audit.shared(audit.units(same), "## Shared decisions\n- `TxStatus` is owned by enum-states\n")
ok(found == [], f"at stage plan the Shared decisions section is where an owner is named: {found}")

# ── 4 · the requirement-id diff, units vs charter ───────────────────────────
CHARTER = "## Requirements\n- R1: states\n- R2: refunds\n- R4: reporting\n\n## Done for the whole\nx\n"
found, und = audit.units_vs_charter(us, CHARTER)
ok(und is None and found == ["no unit delivers R4", "a unit cites R3, which the charter does not state"],
   f"both directions, and the second one is what the cut alone can never see: {found}")
found, und = audit.units_vs_charter(us, None)
ok(found == [] and und and "nothing to diff" in und, f"no charter is undetermined, not complete coverage: {und}")
found, und = audit.units_vs_charter(us, "## Requirements\nprose with no ids\n")
ok(und and "no stable ids" in und, f"a charter with no citable ids cannot be diffed: {und}")
ok(think.coverage is audit.goal_coverage,
   "§3.6: the same script with a second argument checks the charter set against the goal — "
   "`think` must not carry a second implementation of the diff")

# ── 5 · the size heuristic against §4.3 ─────────────────────────────────────
repo = TMP / "repo"
(repo / "src").mkdir(parents=True)
for f in ("model.py", "refund.py", "cancel.py"):
    (repo / "src" / f).write_text("x = 1\n")
found, und = audit.sizes(us, str(repo))
ok(found == [] and und is None, f"three small files are three small units: {(found, und)}")
found, und = audit.sizes(us, None)
ok(found == [] and und and "unmeasured unit is not a small one" in und,
   f"no repo makes the size heuristic undetermined — this is the check most likely to read as clean: {und}")
(repo / "src" / "refund.py").write_text("y = 2\n" * 300_000)          # ~2.1 MB ≈ 525k tokens
found, und = audit.sizes(us, str(repo))
ok(len(found) == 1 and found[0].startswith("refund-flow") and "cut smaller" in found[0],
   f"a unit whose footprint alone passes the §4.3 ceiling is the cheapest bad-cut detector: {found}")
gone = audit.units(CUT.replace("Footprint: src/cancel.py", "Footprint: src/nowhere.py"))
found, und = audit.sizes(gone, str(repo))
ok(und and "cancel-flow" in und and "nothing on disk" in und,
   f"a footprint that resolves to nothing is unmeasured, not small: {und}")

# ── 6 · the acquisition ADR trail ───────────────────────────────────────────
PLAN = "## Acquisition decisions\n- `chalk` 5 — https://example/chalk\n- `wcwidth` 0 — https://example/w\n"
found, und = audit.acquisition(None)
ok(found == [] and und and "no Plan at this stage" in und,
   f"at stage cut there is no Plan, and that is a stage fact, not a pass: {und}")
found, und = audit.acquisition("## Waves\n- wave 1\n")
ok(len(found) == 1 and "no Acquisition decisions section" in found[0],
   f"a Plan missing the section is a missing decision (§3.5): {found}")
trail = [{"type": "adr-filed", "kind": "acquisition", "candidate": "chalk"}]
found, und = audit.acquisition(PLAN, trail)
ok(found == ["`wcwidth` is declared in the Plan with no acquisition ADR on the trail"],
   f"a declared dependency with no ADR is an orphan decision made mid-build (§6): {found}")
found, _ = audit.acquisition(PLAN, trail + [{"type": "adr-filed", "kind": "acquisition", "candidate": "wcwidth"}])
ok(found == [], f"and both filed is none: {found}")
found, _ = audit.acquisition("## Acquisition decisions\n- none\n", [])
ok(found == [], f"`none` is a decision, and it needs no ADR: {found}")

# ── the block: undetermined can never render as none ────────────────────────
block = audit.prepass("cut", CUT, CHARTER, None, None, [])
ok(block.startswith("units: 3 · waves: [1, 2]"), f"the counts lead: {block.splitlines()[0]}")
ok(audit.HEADER in block and "stage: cut" in block, "the header the wrapper looks for, and the stage")
ok("- unit size vs the §4.3 ceiling: undetermined" in block, f"no repo → undetermined in the rendered block:\n{block}")
ok("- acquisition ADR trail: undetermined" in block, "no plan → undetermined in the rendered block")
ok("undetermined" not in block.split("same-wave footprint overlap")[1].splitlines()[0],
   "a check that DID run says none, so the two are distinguishable at a glance")
ok("no unit delivers R4" in block, "and the findings are the auditor's ground truth")
ok("; found anyway: " in audit.render("cut", [("x", ["a finding"], "why")]),
   "an undetermined check that found something anyway still reports both")

# ── the wrapper refuses a plan-auditor packet with no pre-pass ──────────────
import dispatch  # noqa: E402
(TMP / "events").mkdir(exist_ok=True), (TMP / "content").mkdir(exist_ok=True)
pk = TMP / "packet.md"
pk.write_text("stage: cut\nthe cut file, pasted, and no script output at all\n")
r = subprocess.run([sys.executable, str(pathlib.Path(dispatch.__file__)), "plan-auditor", "L-charter-0001",
                    "--packet", str(pk), "--cwd", str(repo)], capture_output=True, text=True,
                   env={**os.environ, "DOIT_ROOT": str(TMP)})
ok(r.returncode == 1 and "no script pre-pass" in r.stderr,
   f"§3.6: the script runs first, and a packet without its output is refused before it spends: {r.stderr[-200:]}")
ok(any('"spawn-failed"' in l and "pre-pass" in l for f in (TMP / "events").glob("*.jsonl")
       for l in f.read_text().splitlines()), "and the refusal is on the record, not just on stderr")
pk.write_text("stage: cut\n" + audit.prepass("cut", CUT, CHARTER, None, str(repo), []))
r = subprocess.run([sys.executable, str(pathlib.Path(dispatch.__file__)), "plan-auditor", "L-charter-0001",
                    "--packet", str(pk), "--cwd", str(repo)], capture_output=True, text=True,
                   env={**os.environ, "DOIT_ROOT": str(TMP), "PATH": "/nonexistent"})
ok("no script pre-pass" not in r.stderr, f"a packet carrying the block gets past the check: {r.stderr[-200:]}")

# ── the CLI, end to end ─────────────────────────────────────────────────────
(TMP / "cut.md").write_text(CUT)
(TMP / "plan.md").write_text(PLAN)
DOIT = pathlib.Path(__file__).resolve().parent.parent / "doit"
r = subprocess.run([str(DOIT), "audit", "cut", "L-charter-0001", "--cut", str(TMP / "cut.md")],
                   capture_output=True, text=True, env={**os.environ, "DOIT_ROOT": str(TMP)})
ok(r.returncode == 0 and audit.HEADER in r.stdout, f"`doit audit cut` prints the block: {r.stderr[-200:]}")
r = subprocess.run([str(DOIT), "audit", "plan", "L-charter-0001", "--cut", str(TMP / "cut.md")],
                   capture_output=True, text=True, env={**os.environ, "DOIT_ROOT": str(TMP)})
ok(r.returncode != 0 and "needs --plan" in r.stderr,
   f"stage plan without the Plan is refused, not audited half-way: {r.stderr[-120:]}")
r = subprocess.run([str(DOIT), "audit", "plan", "L-charter-0001", "--cut", str(TMP / "cut.md"),
                    "--plan", str(TMP / "plan.md"), "--repo", str(repo), "--out", str(TMP / "block.md")],
                   capture_output=True, text=True, env={**os.environ, "DOIT_ROOT": str(TMP)})
ok(r.returncode == 0 and "stage: plan" in r.stdout and "wcwidth" in r.stdout,
   f"stage plan reads the Plan and the trail: {r.stdout[-300:]}")
ok((TMP / "block.md").read_text() == r.stdout, "--out writes the same block the packet gets")

print(f"audit: {n} checks pass")
