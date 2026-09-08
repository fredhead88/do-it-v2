#!/usr/bin/env python3
"""One runnable check on the packet builder. Run: python3 test_packet.py

Every role gets two: the Input list it must carry, and — the point of the file —
that its Blindness list never reaches the packet. The second is exercised twice
per role: the honest packet is scanned for the real forbidden strings, and the
builder is then made to leak one and the refusal must fire."""
import json, os, pathlib, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_PROJECT"] = str(TMP), "t"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, packet  # noqa: E402

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
cd src && python3 test_fold.py
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
t = build("spec-auditor")
assert str(SPEC) in t and "cwd is the repository" in t, t
assert "Verification section holds a runnable command: yes" in t
assert "[NEEDS CLARIFICATION] count: 0" in t
assert "charter ids the spec does not cite: R2" in t, "ids only — the diff, never the charter's text"
assert "Calibration examples: none" in t
c = packet.Ctx(packet.argparse.Namespace(subject="L-spec-0001", charter=None, project="t"))
absent(t, packet.strip(c, "spec-auditor"))
assert "never named a metered source" not in t, "the author's Assumptions reasoning is stripped"
refuses("spec-auditor", "Currency is USD to two places, and the seat's list price is a size, not a bill.")

# the verify script is a file now, not a 300-char field (§5.10)
V = TMP / "content" / "verify-L-spec-0001.sh"
assert V.exists() and "cd src && python3 test_fold.py" in V.read_text() and os.access(V, os.X_OK)
assert "set -euo pipefail" in V.read_text(), "a multi-step block must not exit 0 over an earlier failure"

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
t = build("charter-reviewer", subject="L-charter-0001", charter=str(CHARTER))
assert str(CHARTER) in t and str(CARD) in t and str(SPEC) in t and str(SIB) in t
assert "fixpoint result: reached 20" in t and "never money" in t
cc = packet.Ctx(packet.argparse.Namespace(subject="L-charter-0001", charter=str(CHARTER), project="t"))
absent(t, packet.strip(cc, "charter-reviewer"))
assert "keeps the footprint from colliding" not in t, "the Plan's rationale is stripped"
refuses("charter-reviewer", "Cut this way because one wave keeps the footprint from colliding with itself.",
        subject="L-charter-0001", charter=str(CHARTER))

# ── the file itself ──────────────────────────────────────────────────────────
ps = {p.name for p in (TMP / "packets").glob("*.md")}
assert ps == {"L-charter-0001-charter-reviewer-1.md", "L-spec-0001-builder-1.md",
              "L-spec-0001-grader-1.md", "L-spec-0001-grader-2.md",
              "L-spec-0001-reviewer-1.md",
              "L-spec-0001-spec-auditor-1.md", "L-spec-0001-spec-writer-1.md",
              "L-spec-0001-spec-writer-2.md"}, ps
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

print(f"packet: {N} packets built, six Blindness lists enforced")
