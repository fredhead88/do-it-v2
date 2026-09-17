#!/usr/bin/env python3
"""One runnable check on the packet builder. Run: python3 test_packet.py

Every role gets two: the Input list it must carry, and — the point of the file —
that its Blindness list never reaches the packet. The second is exercised twice
per role: the honest packet is scanned for the real forbidden strings, and the
builder is then made to leak one and the refusal must fire."""
import json, os, pathlib, re, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_PROJECT"] = str(TMP), "t"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import audit, fold, packet  # noqa: E402

REPO = TMP / "repo"
(TMP / "content").mkdir(parents=True)
REPO.mkdir()
subprocess.run(["git", "init", "-q"], cwd=REPO, check=True)
subprocess.run(["git", "-C", str(REPO), "commit", "-q", "--allow-empty", "-m", "root"],
               check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
N = 0

CHARTER = TMP / "content" / "L-charter-0001.md"
CHARTER.write_text("""# charter
## Intent
The board must show spend per project, which nobody can see today at all.
## Requirements
- R1 — the board carries a spend column that is derived and never stamped
- R2 — spend is read from spawn-done usage and from nothing else whatsoever
## Constraints and product decisions
Currency is USD to two places, and the seat's list price is a size, not a bill.
## Done for the whole
review_path: go to the board / worked if a spend column renders per project
## Covers
R1 R2
""")
SPEC = TMP / "content" / "L-spec-0001.md"
SPEC.write_text("""# L-spec-0001
## 1. Goal
Spend per project changes from invisible to a rendered column.
## 4. Interfaces
Consumes: fold.read_events
Produces: fold.spend_by_project(events) -> dict
Writes: src/fold.py
## 6. Assumptions
Assumed the seat's list price is the only figure available, because the charter
never named a metered source and the meter file records zero dollars metered.
## 8. Verification
```
cd src && /usr/bin/python3 test_fold.py
```
## Acceptance criteria
AC1 [backend]: the board renders a spend column per project.
  review_path: log in as nobody / go to `doit` / do nothing / worked if a
  SPEND line appears / failed if it does not.
""")
SIB = TMP / "content" / "L-spec-0002.md"
SIB.write_text("# L-spec-0002\nA sibling slot's internals, which no other role may ever be handed.\n")


def ev(actor, type_, subject, **kv):
    os.environ["DOIT_LEDGER_FILE"] = f"L-{actor}-0001.jsonl"
    return fold.append([type_, subject] + [f"{k}:={json.dumps(v)}" for k, v in kv.items()])


def build(role, subject="L-spec-0001", **kw):
    global N
    N += 1
    argv = [role, subject] + [x for k, v in kw.items() for x in (f"--{k.replace('_', '-')}", str(v))]
    return pathlib.Path(packet.main(argv)).read_text()


def refuses(role, leak, subject="L-spec-0001", **kw):
    """Force the role's Input list to carry one forbidden string; the guard must refuse."""
    global N
    N += 1
    real = packet.BUILD[role]
    packet.BUILD[role] = lambda c: real(c) + [f"leaked: {leak}"]
    try:
        build(role, subject, **kw)
    except SystemExit as e:
        assert "REFUSED" in str(e.code), e.code
        return str(e.code)
    finally:
        packet.BUILD[role] = real
    raise AssertionError(f"{role} did not refuse a packet carrying {leak!r}")


def absent(text, pairs):
    for why, s in pairs:
        assert not s or str(s) not in text, f"{why} reached the packet: {str(s)[:60]!r}"


ev("operator", "charter-filed", "L-charter-0001", path=str(CHARTER))
ev("spec-writer", "spec-written", "L-spec-0001", spec="L-spec-0001", path=str(SPEC),
   footprint=["src/fold.py"], requirement_ids=["R1"], charter="L-charter-0001")
ev("spec-writer", "spec-written", "L-spec-0002", spec="L-spec-0002", path=str(SIB),
   footprint=["src/fold.py"], requirement_ids=["R2"], charter="L-charter-0001")

# ── 1. spec-auditor ──────────────────────────────────────────────────────────
t = build("spec-auditor", worktree=str(REPO))
assert str(SPEC) in t and "cwd is the repository" in t, t
assert "Verification section holds a runnable command: yes" in t
assert "[NEEDS CLARIFICATION] count: 0" in t
assert "charter ids the spec does not cite: R2" in t, "ids only — the diff, never the charter's text"
assert "Calibration examples: none" in t
c = packet.Ctx(packet.argparse.Namespace(subject="L-spec-0001", charter=None, project="t"))
absent(t, packet.strip(c, "spec-auditor"))
assert "never named a metered source" not in t, "the author's Assumptions reasoning is stripped"
refuses("spec-auditor", "Currency is USD to two places, and the seat's list price is a size, not a bill.",
        worktree=str(REPO))

# the verify script is a file now, not a 300-char field (§5.10)
V = TMP / "content" / "verify-L-spec-0001.sh"
assert V.exists() and "cd src && /usr/bin/python3 test_fold.py" in V.read_text() and os.access(V, os.X_OK)
assert "set -euo pipefail" in V.read_text(), "a multi-step block must not exit 0 over an earlier failure"
assert re.match(r"#!/usr/bin/env bash\nset -euo pipefail\nBASE=\S+\n", V.read_text()), \
    "BASE is pinned once, right after set -euo pipefail (a-5 rule d)"

# ── 1b. verify_script lint (a-5, S23/S31a) ────────────────────────────────────
def bad_verify(block, want, subject):
    """One spec whose Verification block violates one rule; the packet must refuse."""
    global N
    p = TMP / "content" / f"{subject}.md"
    p.write_text(f"# {subject}\n## 8. Verification\n```\n{block}\n```\n"
                 "## Acceptance criteria\nAC1 [backend]: x.\n"
                 "  review_path: log in as x / go to x / do x / worked if x / failed if x.\n")
    ev("spec-writer", "spec-written", subject, path=str(p), footprint=["src/fold.py"])
    N += 1
    try:
        build("spec-auditor", subject=subject, worktree=str(REPO))
    except SystemExit as e:
        assert want in str(e.code), e.code
    else:
        raise AssertionError(f"{subject}: {block!r} should have been refused ({want})")


bad_verify("cd src; /usr/bin/python3 test_fold.py", "bare `;`", "L-spec-0021")
bad_verify("cd src && /usr/bin/python3 test_fold.py || true", "bare `||`", "L-spec-0022")
bad_verify("cd src\n/usr/bin/python3 test_fold.py", "newline-separated statement", "L-spec-0023")
bad_verify("cd src && python3 test_fold.py", "absolute path", "L-spec-0024")
bad_verify("cd src && /usr/bin/env python3 test_fold.py", "absolute path", "L-spec-0025")
bad_verify("cd src && /usr/bin/python3 -c \"print('", "fails `bash -n`", "L-spec-0026")

# the merge-base rewrite and the BASE pin, together
MERGE = TMP / "content" / "L-spec-0027.md"
MERGE.write_text("# L-spec-0027\n## 8. Verification\n```\necho $(git merge-base main HEAD)\n```\n"
                 "## Acceptance criteria\nAC1 [backend]: x.\n"
                 "  review_path: log in as x / go to x / do x / worked if x / failed if x.\n")
ev("spec-writer", "spec-written", "L-spec-0027", path=str(MERGE), footprint=["src/fold.py"])
build("spec-auditor", subject="L-spec-0027", worktree=str(REPO))
mtxt = (TMP / "content" / "verify-L-spec-0027.sh").read_text()
mlines = mtxt.splitlines()
assert mlines[1] == "set -euo pipefail" and mlines[2].startswith("BASE="), mlines[:4]
assert "$(git merge-base" not in mtxt and "echo $BASE" in mtxt, mtxt
N += 1

# ── 2. builder ───────────────────────────────────────────────────────────────
t = build("builder", worktree=str(REPO), repo=str(REPO))
assert f"bash {V}" in t and "__pycache__" in t, "the residue lesson is in the done-condition"
assert "Writes (the merge grant" in t and "src/fold.py" in t, "the footprint is the grant"
assert "L-spec-0002 · src/fold.py · written" in t, "the conflict list is id, what, state"
assert "Currency is USD" in t, "the charter EXTRACT — constraints and decisions — is carried verbatim"
assert "one commit, on that branch" in t
absent(t, packet.strip(c, "builder"))
refuses("builder", "A sibling slot's internals, which no other role may ever be handed.",
        worktree=str(REPO), repo=str(REPO))

# ── 3. grader ────────────────────────────────────────────────────────────────
CARD = TMP / "content" / "L-card-0001.md"
CARD.write_text("# L-card-0001 · DONE · built by L-builder-0007\n"
                "branch l-spec-0001 · base abc1234 · ready def5678\n"
                "verify `bash verify.sh` → exit 0 · all green\n"
                "AC1 [backend] built · log · the column renders for two projects\n"
                "not built: the CSV export (out of scope)\n")
ev("builder", "build-done", "L-spec-0001", status="DONE", card=str(CARD), branch="l-spec-0001",
   base_sha="abc1234deadbeef", ready_sha="def5678deadbeef", verify_exit=0, tests_added=True)
ev("builder", "build-deviation", "L-spec-0001", deviation="minor",
   what="renamed the helper because the twin check found a near-identical one already")
t = build("grader", worktree=str(REPO))
assert "AC1 [backend] built · log" in t, "the card's per-criterion rows are carried, as claims"
assert "verify `bash verify.sh` → exit 0" in t and "validator: not installed" in t
assert "verify-L-spec-0001" in t and "coverage note" in t and "__pycache__" in t
absent(t, packet.strip(c, "grader"))
for gone in ("built by L-builder-0007", "branch l-spec-0001 · base", "abc1234deadbeef",
             "def5678deadbeef", "renamed the helper", "not built:"):
    assert gone not in t, f"the grader must never see {gone!r}"
refuses("grader", "L-card-0001 · DONE · built by L-builder-0007", worktree=str(REPO))
refuses("grader", "renamed the helper because the twin check found a near-identical one already",
        worktree=str(REPO))

# The Executor's cut recipe names the worktree directory for the branch and the
# branch for the spec, so the grader packet — the one packet that must carry the
# worktree path — carried the builder's branch and was refused: every spec built
# on that recipe was ungradeable, and the suite missed it because its worktree was
# never named after the branch (L-spec-0005, 2026-09-08). A branch the grader can
# derive from the subject it already holds is not a cue.
WT = TMP / "worktrees" / "t" / "l-spec-0001"
WT.mkdir(parents=True)
t = build("grader", worktree=str(WT))
assert str(WT) in t, "the grader must be told where the build is"
# ...and a branch it cannot derive is still a cue, and still stripped.
ev("builder", "build-done", "L-spec-0002", status="DONE", branch="claude/L-spec-0002-feature",
   base_sha="1111111", ready_sha="2222222", verify_exit=0, tests_added=True)
c2 = packet.Ctx(packet.argparse.Namespace(subject="L-spec-0002", charter=None, project="t"))
assert ("the builder's branch", "claude/L-spec-0002-feature") in packet.builder_cues(c2), \
    "a branch that is not the subject is a cue the grader must never see"

# ── 4. spec-writer (rework) ──────────────────────────────────────────────────
ev("spec-auditor", "audit-finding", "L-spec-0001", list="findings", field="Verification",
   category="unfalsifiable", finding="AC1's review path observes nothing that could fail",
   suggested_fix="name the SPEND line and the project it renders under")
SLOT = TMP / "slot.md"
SLOT.write_text(f"1. Charter extract: R1, and the constraints above.\n"
                f"9. The path to write the spec: {SPEC}\n")
t = build("spec-writer", slot=str(SLOT))
assert "Fix list" in t and "AC1's review path observes nothing" in t and "name the SPEND line" in t
assert str(SPEC) in t, "rework is round one's packet plus the list — same spec id, same path"
t2 = build("spec-writer")
assert t2.startswith(t.split("## Fix list")[0][:60]), "round two reuses the packet on disk, not --slot"
absent(t, packet.strip(c, "spec-writer"))
refuses("spec-writer", "A sibling slot's internals, which no other role may ever be handed.")
# The Planner's round-one slot lives at content/slot-<spec>.md by convention; a rework
# with no packet on disk reads it without --slot (pilot S6 — every first rework needed
# the flag typed by hand; charter 3 crashed a dispatch on the refusal captured as "").
SPEC10 = TMP / "content" / "L-spec-0010.md"
SPEC10.write_text("# L-spec-0010\n## 1. Goal\nx\n")
(TMP / "content" / "slot-L-spec-0010.md").write_text(f"1. Charter extract: R1.\n9. The path to write the spec: {SPEC10}\n")
ev("spec-writer", "spec-written", "L-spec-0010", spec="L-spec-0010", path=str(SPEC10), footprint=[])
ev("spec-auditor", "audit-finding", "L-spec-0010", list="findings", field="Goal", category="vague",
   finding="the goal names no observable", suggested_fix="name one")
t10 = build("spec-writer", subject="L-spec-0010")
assert "Fix list" in t10 and "the goal names no observable" in t10 and str(SPEC10) in t10, "the content slot is round one's packet"

# ── 5. reviewer ──────────────────────────────────────────────────────────────
ev("grader", "verdict", "L-spec-0001", confirmed=False, n=1, matches_intent="yes", card_ok="yes")
ev("grader", "rejected-criterion", "L-spec-0001", criterion="AC1",
   why="the column renders but the total is the sum of a filtered read, which is wrong")
t = build("reviewer", worktree=str(REPO), depth="gates-only", round="1")
assert "AC1 [backend]" in t and "review_path" in t, "every criterion, and every review path"
assert "reported exit 0" in t and "depth: gates-only · round: 1" in t
assert "review account" in t and "never move money" in t
absent(t, packet.strip(c, "reviewer"))
assert "sum of a filtered read" not in t, "the grader's reasons are the reviewer's blind spot"
refuses("reviewer", "the column renders but the total is the sum of a filtered read, which is wrong",
        worktree=str(REPO))

# ── 6. charter-reviewer ──────────────────────────────────────────────────────
PLAN = TMP / "content" / "L-plan-0001.md"
PLAN.write_text("# plan\nCut this way because one wave keeps the footprint from colliding with itself.\n")
ev("planner", "plan-written", "L-charter-0001", path=str(PLAN))
ev("executor", "sweep-fixpoint", "L-charter-0001")
# A spec whose `charter` field is the charter's PATH — which is what the packet
# hands the role, so it is what the role writes back — belongs to the charter all
# the same. Unnormalised it belonged to none, and the real charter-reviewer was
# handed one card for a charter that had shipped two (L-charter-reviewer-0002).
CARD3 = TMP / "content" / "L-card-0003.md"
CARD3.write_text("# L-card-0003 · DONE\n")
ev("spec-writer", "spec-written", "L-spec-0003", spec="L-spec-0003", path=str(SIB),
   footprint=["src/fold.py"], requirement_ids=["R3"], charter=str(CHARTER))
ev("builder", "build-done", "L-spec-0003", status="DONE", card=str(CARD3), ready_sha="3333333")
t = build("charter-reviewer", subject="L-charter-0001", charter=str(CHARTER))
assert str(CHARTER) in t and str(CARD) in t and str(SPEC) in t and str(SIB) in t
assert str(CARD3) in t, "a path-named spec's card is the charter's card too"
assert "fixpoint result: reached 20" in t and "never money" in t
cc = packet.Ctx(packet.argparse.Namespace(subject="L-charter-0001", charter=str(CHARTER), project="t"))
absent(t, packet.strip(cc, "charter-reviewer"))
assert "keeps the footprint from colliding" not in t, "the Plan's rationale is stripped"
refuses("charter-reviewer", "Cut this way because one wave keeps the footprint from colliding with itself.",
        subject="L-charter-0001", charter=str(CHARTER))
# ...and the same field is how every packet FINDS the charter on disk. Unnormalised
# it matched no `charter-filed` subject, `charter_file()` returned None, and the
# builder's item 2 — the charter's binding constraints, verbatim — read "none".
t = build("builder", subject="L-spec-0003", worktree=str(REPO), repo=str(REPO))
assert "Currency is USD" in t, "a path-named charter is still found on disk"

# ── 7. plan-auditor (a-14, S6/S21) ────────────────────────────────────────────
CUT_L1 = TMP / "content" / "cut-L-charter-0001.md"
CUT_L1.write_text("""# cut for L-charter-0001

## unit-a
Goal: derive spend per project
Delivers: R1
Footprint: src/fold.py
Consumes:
Produces:
Wave: 1

## unit-b
Goal: read spend from spawn-done usage only
Delivers: R2
Footprint: src/other.py
Consumes:
Produces:
Wave: 1

## Rationale
This cut was chosen because it keeps wave 1 minimal and testable.
""")
ev("planner", "cut-written", "L-charter-0001", path=str(CUT_L1))

t = build("plan-auditor", subject="L-charter-0001", stage="cut", repo=str(REPO))
assert "stage: cut" in t and "charter: L-charter-0001" in t
assert "review_path" in t and "worked if a spend column renders" in t, "the done-condition, verbatim"
assert "R1 — the board carries a spend column" in t, "the requirements, verbatim"
assert "## unit-a" in t and "## unit-b" in t, "the cut, embedded whole"
assert audit.HEADER in t and "same-wave footprint overlap" in t, "the doit audit block"
assert "kept wave 1 minimal" not in t, "no rationale — strip_rationale removes the section"
pa1 = packet.Ctx(packet.argparse.Namespace(subject="L-charter-0001", charter=None, project="t", stage="cut"))
absent(t, packet.strip(pa1, "plan-auditor"))
N += 1
try:
    build("plan-auditor", subject="L-charter-0001")
except SystemExit as e:
    assert "needs --stage" in str(e.code), e.code
else:
    raise AssertionError("plan-auditor with no --stage should refuse")

PLAN_L1 = TMP / "content" / "plan-L-charter-0001.md"
PLAN_L1.write_text("""# Plan · L-charter-0001

## Seams
None — both units are independent.

## Shared decisions
None.

## Rationale
Chose one wave because the two units' footprints never collide with each other.
""")
ev("planner", "plan-written", "L-charter-0001", path=str(PLAN_L1))
os.environ["DOIT_LEDGER_FILE"] = "L-plan-auditor-0001.jsonl"
fold.append(["audit-finding", "L-charter-0001", "stage=cut", "category=stale",
             "finding=old finding, superseded by the re-cut"])
os.environ["DOIT_LEDGER_FILE"] = "L-plan-auditor-0002.jsonl"
fold.append(["audit-finding", "L-charter-0001", "stage=cut", "category=seam-undefined",
             "finding=wave 1 has no producer for X", "confirms_with=script"])

t = build("plan-auditor", subject="L-charter-0001", stage="plan", repo=str(REPO))
assert "## The Plan" in t and "None — both units are independent." in t
assert "wave 1 has no producer for X" in t and "confirms_with: script" in t
assert "old finding, superseded" not in t, "only the LATEST cut-audit round rides along (mkpacket.py's files[-1:])"
assert "kept wave 1 minimal" not in t and "footprints never collide" not in t, \
    "no rationale, from either the cut or the Plan"
assert audit.HEADER in t and "stage: plan" in t
pa2 = packet.Ctx(packet.argparse.Namespace(subject="L-charter-0001", charter=None, project="t", stage="plan"))
absent(t, packet.strip(pa2, "plan-auditor"))
refuses("plan-auditor", "Chose one wave because the two units' footprints never collide with each other.",
        subject="L-charter-0001", stage="plan", repo=str(REPO))
# a charter with a cut but no Plan yet
CUT_L2 = TMP / "content" / "cut-L-charter-0002.md"
CUT_L2.write_text("## unit-x\nGoal: x\nDelivers: R9\nFootprint: x.py\nWave: 1\n")
ev("operator", "charter-filed", "L-charter-0002", path=str(CHARTER))
ev("planner", "cut-written", "L-charter-0002", path=str(CUT_L2))
N += 1
try:
    build("plan-auditor", subject="L-charter-0002", stage="plan")
except SystemExit as e:
    assert "needs a Plan" in str(e.code), e.code
else:
    raise AssertionError("stage plan with no Plan on file should refuse")

# stage charter-set: no single charter — the whole set, diffed against a goal
GOAL = TMP / "content" / "L-goal-0001.md"
GOAL.write_text("# goal\n## Requirements\n- G1: reduce onboarding time\n"
               "- G2: measure spend per project\n\n"
               "## Done for the whole\nreview_path: go to the board / worked if both are visible.\n")
ev("operator", "charter-filed", "L-charter-0005", path=str(CHARTER), title="Spend visibility", covers="G2")
t = build("plan-auditor", subject="not-a-real-charter-subject", stage="charter-set", goal=str(GOAL))
assert "stage: charter-set" in t and "goal: L-goal-0001" in t
assert "go to the board / worked if both are visible" in t
assert "goal requirements no charter cites:" in t and "G1" in t, "the both-directions diff"
assert "## L-charter-0001 — L-charter-0001" in t and "## L-charter-0005 — Spend visibility" in t
N += 1
try:
    build("plan-auditor", subject="also-not-real", stage="charter-set")
except SystemExit as e:
    assert "needs a goal" in str(e.code), e.code
else:
    raise AssertionError("stage charter-set with no goal should refuse")

# ── 8. spec-writer round one, built from scratch (a-14, closes S6/S21) ───────
t = build("spec-writer", subject="L-spec-0030", charter=str(CHARTER), unit="unit-a", worktree=str(REPO))
assert "unit `unit-a`" in t and "charter L-charter-0001" in t
assert "R1 — the board carries a spend column" in t, "the requirement lines this unit delivers, verbatim"
assert "Currency is USD" in t, "constraints and product decisions, verbatim"
assert "### Seams" in t and "None — both units are independent." in t
assert "unit-b" in t and "produces: nothing" in t, "the sibling's Produces:"
assert str(TMP / "content" / "L-spec-0030.md") in t, "the write path"
assert "at most 400 lines" in t
assert "kept wave 1 minimal" not in t and "footprints never collide" not in t, "no rationale, either document"
t2 = build("spec-writer", subject="L-spec-0031", charter=str(CHARTER), unit="unit-b",
          envelope="a builder alone can run pytest", cost_path="none")
assert "a builder alone can run pytest" in t2 and "Cost-path inventory: none" in t2
N += 1
try:
    # bare --slot (the CLI shape a-14 names) with no --unit: reaches
    # `_round_one_slot`'s own check, not Ctx's "no events" guard.
    packet.main(["spec-writer", "L-spec-0032", "--slot", "--charter", str(CHARTER)])
except SystemExit as e:
    assert "needs --unit" in str(e.code), e.code
else:
    raise AssertionError("round one from scratch with no --unit should refuse")
N += 1
try:
    build("spec-writer", subject="L-spec-0033", unit="unit-a")
except SystemExit as e:
    assert "needs --charter" in str(e.code), e.code
else:
    raise AssertionError("round one from scratch with no --charter should refuse")

# ── the file itself ──────────────────────────────────────────────────────────
ps = {p.name for p in (TMP / "packets").glob("*.md")}
assert ps == {"L-charter-0001-charter-reviewer-1.md", "L-spec-0001-builder-1.md",
              "L-spec-0003-builder-1.md",
              "L-spec-0001-grader-1.md", "L-spec-0001-grader-2.md",
              "L-spec-0001-reviewer-1.md",
              "L-spec-0001-spec-auditor-1.md", "L-spec-0001-spec-writer-1.md",
              "L-spec-0001-spec-writer-2.md", "L-spec-0010-spec-writer-1.md",
              "L-spec-0027-spec-auditor-1.md",
              "L-charter-0001-plan-auditor-1.md", "L-charter-0001-plan-auditor-2.md",
              "not-a-real-charter-subject-plan-auditor-1.md",
              "L-spec-0030-spec-writer-1.md", "L-spec-0031-spec-writer-1.md"}, ps
assert "REFUSED" not in "".join(p.read_text() for p in (TMP / "packets").glob("*.md")), \
    "a refused packet is never written to disk"
first = (TMP / "packets" / "L-spec-0001-spec-auditor-1.md").read_text()
build("spec-auditor")
assert (TMP / "packets" / "L-spec-0001-spec-auditor-2.md").exists(), "n increments"
assert (TMP / "packets" / "L-spec-0001-spec-auditor-1.md").read_text() == first, "never overwritten"

# A `spec-written` with no `path` — the pre-wrapper era wrote three, and one of
# them crashed `doit packet spec-writer` on the first real rework dispatch, which
# is the one dispatch a spec with audit findings cannot proceed without.
ev("planner", "spec-written", "L-spec-0009")            # no path=, deliberately
build("spec-writer")
assert (TMP / "packets" / "L-spec-0001-spec-writer-2.md").exists(), "a pathless sibling is skipped, not crashed on"

# Same shape one layer over: a `build-done` with no `card`. Four sit in the real
# ledger from the pre-wrapper era and one of them crashed `doit packet builder`,
# which is the one dispatch a written spec cannot proceed without.
ev("builder", "build-done", "L-spec-0002")              # no card=, deliberately
t = build("builder", worktree=str(REPO), repo=str(REPO))
assert "another builder's card" not in t, "a cardless build-done is skipped, not crashed on"

# The criteria the grader and the reviewer are handed. `L-reviewer-0002` reviewed
# `L-spec-0004` against "none found in the spec" and returned no blocking finding:
# the reviewer's own extractor anchored on an `AC1 [` line and the real spec's
# criteria are bold (`**AC1 [ui] — ...**`). Both roles now read the same
# extractor, and a spec it cannot read refuses instead of reporting none.
BOLD = TMP / "content" / "L-spec-0011.md"
BOLD.write_text("""# L-spec-0011
## 8. Verification
```
true
```
## Acceptance criteria

**AC1 [ui] - every project gets a line.**
    worked if   a spend line renders
    failed if   none does
""")
ev("spec-writer", "spec-written", "L-spec-0011", path=str(BOLD), footprint=["src/fold.py"])
ev("builder", "build-done", "L-spec-0011", card=str(CARD), ready_sha="deadbee", verify_exit=0)
for role in ("grader", "reviewer"):
    t = build(role, "L-spec-0011", worktree=str(REPO))
    assert "AC1 [ui]" in t, f"the {role} packet dropped a bold criterion"
    assert "none found in the spec" not in t, f"the {role} packet reported none over a spec that has one"

NONE = TMP / "content" / "L-spec-0012.md"
NONE.write_text("# L-spec-0012\n## 1. Goal\nA spec with no criteria at all.\n")
ev("spec-writer", "spec-written", "L-spec-0012", path=str(NONE), footprint=["src/fold.py"])
ev("builder", "build-done", "L-spec-0012", card=str(CARD), ready_sha="deadbee", verify_exit=0)
for role in ("grader", "reviewer"):
    N += 1
    try:
        build(role, "L-spec-0012", worktree=str(REPO))
    except SystemExit as e:
        assert "no acceptance criteria" in str(e.code), e.code
    else:
        raise AssertionError(f"{role} built a packet over a spec with no criteria")

# D7 · §3.12 — the Executor authors the spec an in-scope brief implies. That spec
# has no plan slot (the Planner cleared) and no audit yet, so its round one is a
# slot the EXECUTOR wrote. Before this the rework path refused it: "no audit
# findings on this subject", and the close row had nowhere to go.
BRIEF_SLOT = TMP / "slot-L-spec-0020.md"
BRIEF_SLOT.write_text("Brief (L-charter-reviewer-0001.jsonl:3, cites R3): the no-spawn project renders\n"
                      "silence. Footprint hint: src/fold.py.\n9. The path to write the spec: "
                      f"{TMP / 'content' / 'L-spec-0020.md'}\n")
t = build("spec-writer", subject="L-spec-0020", slot=str(BRIEF_SLOT))
assert "cites R3" in t and "Fix list" not in t, "round one from a slot carries no fix list"

# ...and the negatives, both of them: no slot at all is still the Planner's round
# one and refuses, and a PRIOR packet with no findings is a rework with nothing to
# rework — the mistake the original refusal was written for, still refused.
for kw, want in ((dict(), "round one carries the plan slot"),
                 (dict(slot=str(BRIEF_SLOT)), "a rework packet with no fix list")):
    N += 1
    try:
        build("spec-writer", "L-spec-0009" if not kw else "L-spec-0020", **kw)
    except SystemExit as e:
        assert want in str(e.code), e.code
    else:
        raise AssertionError(f"spec-writer built a packet it should have refused: {want}")

# ── 9. R9(b) · the multi-line Footprint:/Consumes:/Produces: reader ──────────
# A real cut writes a seam's entries on the lines BENEATH a bare label, and a footprint
# that does not fit one line the same way. Read as "the remainder of the label line",
# every entry after the first was silently dropped (measured on the real
# `packets/L-spec-0049-spec-writer-1.md`), a two-line `Footprint:` was truncated to its
# first path — narrowing the merge grant below the audited cut — and a label with NO
# entry lines bled the next field in: a bare `Produces:` above `Wave: 2` rendered
# `produces: Wave: 2`. All three are asserted below.
CH3 = TMP / "content" / "L-charter-0003.md"
CH3.write_text("""# charter
## Requirements
- R1 — every entry of a multi-line field reaches the packet, in the cut's own order
- R2 — a label with no entry line beneath it still reads nothing
## Constraints and product decisions
Entries are never re-ordered and never deduplicated.
## Done for the whole
review_path: go to the packet / worked if every entry is there
""")
CUT_L3 = TMP / "content" / "cut-L-charter-0003.md"
CUT_L3.write_text("""# cut for L-charter-0003

## unit-m
Goal: carry every entry
Delivers: R1
Footprint: src/one.py
src/two.py
Consumes:
Produces:
p_alpha(a) -> alpha
p_beta(b) -> beta
p_gamma(c) -> gamma
Wave: 1

## unit-n
Goal: read the sibling's produces
Delivers: R2
Footprint: src/four.py
Consumes:
c_one(x) -> one
Produces:
Wave: 2
""")
ENTRIES = "p_alpha(a) -> alpha; p_beta(b) -> beta; p_gamma(c) -> gamma"

t = build("spec-writer", subject="L-spec-0050", charter=str(CH3), unit="unit-m")
assert "- Footprint (= the merge grant, `Writes:`): src/one.py src/two.py" in t, \
    "a two-line Footprint is the whole merge grant, not its first line"
assert f"- Wave: 1. Seams: Consumes nothing; Produces {ENTRIES}." in t, \
    "every entry under a bare Produces: label, and `nothing` only where there is none"
assert "- `unit-n` produces: nothing" in t, \
    "a label with no entry line beneath it reads nothing — never the next field bled in"
assert "produces: Wave: 2" not in t, "the `\\s*`-crosses-the-newline bleed is closed"
for entry in ("src/one.py", "src/two.py", "p_alpha(a) -> alpha", "p_beta(b) -> beta",
              "p_gamma(c) -> gamma"):
    assert entry in t, f"{entry!r} was dropped by the field reader"

t = build("spec-writer", subject="L-spec-0051", charter=str(CH3), unit="unit-n")
assert f"- `unit-m` produces: {ENTRIES}" in t, "a sibling's multi-line Produces:, whole"
assert "- Wave: 2. Seams: Consumes c_one(x) -> one; Produces nothing." in t
assert "- Footprint (= the merge grant, `Writes:`): src/four.py" in t, "one-line fields are unchanged"

# ── 10. R9(a) · --stage doc: one document, one audit, no cut file ─────────────
DOC = TMP / "content" / "plan-L-charter-0009.md"
DOC.write_text("""# L-charter-0009 — the cut and the Plan, one document

## unit-solo
Goal: one document, one audit
Delivers: R1
Footprint: src/solo.py
Consumes:
Produces:
solo(x) -> y
Wave: 1

## Seams
solo(x) -> y, produced by unit-solo.

## Rationale
Collapsed because two documents meant two audits and one of them was always stale.
""")
ev("operator", "charter-filed", "L-charter-0009", path=str(CHARTER))
assert not (TMP / "content" / "cut-L-charter-0009.md").exists(), "stage doc asks for no cut file"
assert not [e for e in fold.read_events()
            if e["type"] == "cut-written" and e["subject"] == "L-charter-0009"], "and no cut-written event"

CALLS = []
REAL_PREPASS = packet.audit.prepass
STANDIN = "STAND-IN GROUND TRUTH BLOCK · stage doc"


def standin(*a, **k):
    """`src/audit.py` is a sibling's footprint and is not on this tree: the call SHAPE is
    what this unit owes, and a stand-in is what proves it (never the sibling's body)."""
    CALLS.append((a, k))
    return STANDIN


packet.audit.prepass = standin
try:
    t = build("plan-auditor", subject="L-charter-0009", stage="doc", charter=str(CHARTER),
              repo=str(REPO))
    assert "stage: doc" in t and "charter: L-charter-0009" in t
    assert "## unit-solo" in t and "solo(x) -> y" in t, "the whole document body is embedded"
    assert "worked if a spend column renders" in t, "the charter's done-condition, verbatim"
    assert "R1 — the board carries a spend column" in t, "the charter's requirements, verbatim"
    assert "two audits and one of them was always stale" not in t, "no rationale, ever"
    assert STANDIN in t, "the audit's return value lands verbatim in the packet"
    (args, kw), = CALLS
    assert kw == {} and len(args) == 4, f"prepass(doc_text, charter_text, repo, events): {args!r}"
    assert args[0] == packet.strip_rationale(DOC.read_text().rstrip()), "arg 1 is the WHOLE document"
    assert args[1] == CHARTER.read_text(), "arg 2 is the charter text"
    assert args[2] == str(REPO), "arg 3 is the repo"
    assert args[3] is None, "arg 4 is events — None; the sibling resolves it via fold.read_events()"
    pa3 = packet.Ctx(packet.argparse.Namespace(subject="L-charter-0009", charter=str(CHARTER),
                                               project="t", stage="doc"))
    absent(t, packet.strip(pa3, "plan-auditor"))
    refuses("plan-auditor", "Collapsed because two documents meant two audits and one of "
            "them was always stale.", subject="L-charter-0009", stage="doc",
            charter=str(CHARTER), repo=str(REPO))
    # ...and the document may equally be named by a `plan-written` event.
    DOC2 = TMP / "content" / "one-doc-L-charter-0010.md"
    DOC2.write_text("# L-charter-0010\n\n## unit-solo2\nGoal: g\nDelivers: R2\n"
                    "Footprint: src/s2.py\nWave: 1\n")
    ev("operator", "charter-filed", "L-charter-0010", path=str(CHARTER))
    ev("planner", "plan-written", "L-charter-0010", path=str(DOC2))
    t = build("plan-auditor", subject="L-charter-0010", stage="doc", charter=str(CHARTER),
              repo=str(REPO))
    assert "## unit-solo2" in t, "a plan-written event's path is the document too"
finally:
    packet.audit.prepass = REAL_PREPASS

ev("operator", "charter-filed", "L-charter-0011", path=str(CHARTER))
N += 1
try:
    build("plan-auditor", subject="L-charter-0011", stage="doc", charter=str(CHARTER))
except SystemExit as e:
    assert "needs the one document" in str(e.code), e.code
else:
    raise AssertionError("stage doc with no document on file should refuse")

# ── 11. R10 · the Planner dispatches the audit and the rewrite itself ─────────
REPOS_T = TMP / "repos" / "t"
REPOS_T.mkdir(parents=True)
subprocess.run(["git", "init", "-q"], cwd=REPOS_T, check=True)
subprocess.run(["git", "-C", str(REPOS_T), "commit", "-q", "--allow-empty", "-m", "root"],
               check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
REPO_HEAD = subprocess.run(["git", "-C", str(REPOS_T), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()

SPEC60 = TMP / "content" / "L-spec-0060.md"
SPEC60.write_text("""# L-spec-0060
## 8. Verification
```
cd src && /usr/bin/python3 test_fold.py
```
## Acceptance criteria
AC1 [backend]: x.
  review_path: log in as x / go to x / do x / worked if x / failed if x.
""")
# no base_sha on the event, deliberately: not one of the ledger's spec-written events
# carries one, because a spec's audit is dispatched before any build exists.
ev("spec-writer", "spec-written", "L-spec-0060", spec="L-spec-0060", path=str(SPEC60),
   footprint=["src/fold.py"], charter="L-charter-0001")
assert not (TMP / "worktrees" / "t" / "l-spec-0060").is_dir(), "no worktree before the build"
N += 1
p60 = pathlib.Path(packet.packet_spec_auditor("L-spec-0060"))
assert p60.is_file() and str(SPEC60) in p60.read_text(), \
    "a bare packet_spec_auditor(spec) builds a packet; it does not die for want of a base"
assert f"BASE={REPO_HEAD}\n" in (TMP / "content" / "verify-L-spec-0060.sh").read_text(), \
    "with no worktree on disk the base is the project repo's HEAD, pinned once"
N += 1
assert pathlib.Path(packet.packet_spec_auditor("L-spec-0060", base_sha="cafe1234")).is_file()
assert (TMP / "content" / "verify-L-spec-0060.sh").read_text().splitlines()[2] == "BASE=cafe1234", \
    "an explicit base_sha still wins — the existing precedence above the fallback is untouched"


def same_body_across_ledgers(fn, *a):
    """The one variable under test is DOIT_LEDGER_FILE. Each call's packet is removed
    between runs because `p_spec_writer` legitimately builds on the previous packet on
    disk, and that is a fact about the spec's history, not about who is calling."""
    global N
    out = []
    for led in ("L-planner-0001.jsonl", "L-executor-0001.jsonl"):
        os.environ["DOIT_LEDGER_FILE"] = led
        N += 1
        q = pathlib.Path(fn(*a))
        out.append(q.read_text())
        q.unlink()
    return out


(TMP / "content" / "slot-L-spec-0060.md").write_text(
    f"1. Charter extract: R1.\n9. The path to write the spec: {SPEC60}\n")
ev("spec-auditor", "audit-finding", "L-spec-0060", list="findings", field="Goal",
   category="vague", finding="the goal names no observable", suggested_fix="name one")

b1, b2 = same_body_across_ledgers(packet.packet_spec_auditor, "L-spec-0060")
assert b1 == b2, "the spec-auditor packet body must not depend on the caller's ledger file"
b1, b2 = same_body_across_ledgers(packet.packet_spec_rework, "L-spec-0060")
assert b1 == b2 and "Fix list" in b1 and "the goal names no observable" in b1, \
    "the spec-rework packet carries the standing findings, and the same body for any caller"
packet.audit.prepass = standin
try:
    b1, b2 = same_body_across_ledgers(packet.packet_plan_auditor, "L-charter-0009")
finally:
    packet.audit.prepass = REAL_PREPASS
assert b1 == b2 and "stage: doc" in b1, \
    "the plan-auditor packet body must not depend on the caller's ledger file"
os.environ["DOIT_LEDGER_FILE"] = "L-operator-0001.jsonl"

# R10's consumed seam: `review_tier(footprint)`'s two-word vocabulary is already the
# shipped flag's. Proven, not built — no src/packet.py change is owed for it.
for d in ("full", "gates-only"):
    t = build("reviewer", worktree=str(REPO), depth=d, round="1")
    assert f"depth: {d} · round: 1" in t, "the review-tier vocabulary builds, unchanged"

# ── 12. R15 · a rework is a re-dispatch carrying a hint, never a diff ─────────
HINT = "The predicate reads `client`; the column is `client_id`."
plain = build("builder", worktree=str(REPO), repo=str(REPO))
hinted = build("builder", worktree=str(REPO), repo=str(REPO), hint=HINT)
BLOCK = ["This is a re-dispatch on the same worktree and branch. The correction, verbatim:",
         HINT, "   The paths it concerns: src/fold.py."]
hl = hinted.splitlines()
i = hl.index(BLOCK[0])
assert hl[i:i + 3] == BLOCK, hl[i:i + 3]
assert hl[:i] + hl[i + 3:] == plain.splitlines(), \
    "same worktree, branch, Writes:, conflict list and ADRs — the hint adds one block and moves nothing"
assert "rejected — AC1:" in hinted and BLOCK[0] in hinted, \
    "a standing grader rejection and a hint are two independent sections; neither is dropped"
N += 1
assert HINT in pathlib.Path(packet.packet_builder_rework("L-spec-0001", HINT)).read_text(), \
    "the Python-import route builds the same re-dispatch packet as the CLI"

before = len(list((TMP / "packets").glob("*.md")))
for bad in ("Use this:\n```\nx = 1\n```\n",
            "--- a/src/fold.py\n+++ b/src/fold.py\n",
            "@@ -1,3 +1,3 @@\n-old\n+new\n",
            "+++ b/src/fold.py"):
    N += 1
    try:
        packet.packet_builder_rework("L-spec-0001", bad)
    except SystemExit as e:
        assert "carries a diff" in str(e.code), e.code
    else:
        raise AssertionError(f"a diff-shaped hint was rendered into a packet: {bad!r}")
assert len(list((TMP / "packets").glob("*.md"))) == before, "a refused hint writes no packet"

# ...and the negative control: the scan is over the HINT ARGUMENT ALONE. A charter's
# Constraints extract may legitimately carry a `---` rule, and the build still succeeds.
CH7 = TMP / "content" / "L-charter-0007.md"
CH7.write_text("""# charter
## Requirements
- R7 — the refusal scan runs on the hint argument alone
## Constraints and product decisions
---
The line above is a horizontal rule in a charter, not a diff hunk.
## Done for the whole
review_path: go / worked if the build still succeeds
""")
ev("operator", "charter-filed", "L-charter-0007", path=str(CH7))
SPEC70 = TMP / "content" / "L-spec-0070.md"
SPEC70.write_text("""# L-spec-0070
## 8. Verification
```
cd src && /usr/bin/python3 test_fold.py
```
## Acceptance criteria
AC1 [backend]: x.
  review_path: log in as x / go to x / do x / worked if x / failed if x.
""")
ev("spec-writer", "spec-written", "L-spec-0070", spec="L-spec-0070", path=str(SPEC70),
   footprint=["src/fold.py"], charter="L-charter-0007")
t = build("builder", subject="L-spec-0070", worktree=str(REPO), repo=str(REPO),
          hint="Rename the column in the predicate; one line.")
assert "\n---\n" in t, "a `---` rule in the charter extract is carried, never scanned"
assert "Rename the column in the predicate; one line." in t, "and the honest hint still builds"

print(f"packet: {N} packets built, seven Blindness lists enforced")
