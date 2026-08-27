#!/usr/bin/env python3
"""One runnable check on the fold rules. Run: python3 test_fold.py"""
import json, os, pathlib, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold  # noqa: E402

T = datetime.now(timezone.utc)
stamp = lambda d=0: (T - timedelta(days=d)).isoformat(timespec="seconds")


def ledger(**files):
    shutil.rmtree(TMP / "events", ignore_errors=True)
    (TMP / "events").mkdir(parents=True)
    for name, evs in files.items():
        (TMP / "events" / name).write_text(
            "".join(json.dumps({"v": 1, **e}) + "\n" for e in evs))
    ev = fold.read_events()
    return (ev,) + fold.fold(ev)


S, C = "L-spec-0142", "L-charter-0031"
built = [{"ts": stamp(3), "type": "spec-written", "subject": S, "charter": C},
         {"ts": stamp(2), "type": "build-started", "subject": S},
         {"ts": stamp(2), "type": "build-done", "subject": S}]
graded = [{"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": True}]
reviewed = [{"ts": stamp(1), "type": "review", "subject": S, "depth": "gates-only"}]
shipped = [{"ts": stamp(0), "type": "shipped", "subject": S}]

# accepted() needs all four conjuncts — none of them alone, and no event says "accepted"
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "accepted", sp[S]["state"]

_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "shipped", "no review event -> never accepted (D29)"

# a failed grade must not render like a spec merely waiting to be looked at
failed = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False},
    {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC1"},
    {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC3"}]})
assert failed[1][S]["rejects"] == 2, "standing rejections are counted"
assert "2 REJECTED, needs rework" in fold.render(*failed), \
    "the board distinguishes 'not looked at yet' from 'looked at and failed'"
cleared = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False},
    {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC1"},
    {"ts": stamp(0), "type": "criterion-cleared", "subject": S, "criterion": "AC1"}]})
assert cleared[1][S]["rejects"] == 0, "a cleared criterion stops standing"

# D112 — the operator may close a spec that was never built, and it never reads
# as accepted. Anyone else saying so is ignored like any other unauthorized emit.
closed = ledger(**{"L-planner-01.jsonl": [built[0]], "L-operator-01.jsonl": [
    {"ts": stamp(0), "type": "spec-closed", "subject": S, "charter": C,
     "why": "answered by operator evidence, never built"},
    {"ts": stamp(0), "type": "l1-complete", "subject": C}]})
assert closed[1][S]["state"] == "closed-unbuilt", closed[1][S]["state"]
assert "1 closed unbuilt" in fold.render(*closed), "an unbuilt close is visible at charter close"
notop = ledger(**{"L-planner-01.jsonl": [built[0]], "L-builder-01.jsonl": [
    {"ts": stamp(0), "type": "spec-closed", "subject": S, "charter": C}]})
assert notop[1][S]["state"] == "written", "only the operator may close a spec unbuilt"

# a builder may not clear the rejections against its own work
selfclear = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(0), "type": "criterion-cleared", "subject": S, "criterion": "AC1"}],
    "L-grader-01.jsonl": [
        {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False},
        {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC1"}]})
assert selfclear[1][S]["rejects"] == 1, "a builder cannot clear its own rejected criterion"
gclear = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False},
    {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC1"},
    {"ts": stamp(0), "type": "criterion-cleared", "subject": S, "criterion": "AC1"}]})
assert gclear[1][S]["rejects"] == 0, "a grader can"
exclear = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": [
    {"ts": stamp(1), "type": "rejected-criterion", "subject": S, "criterion": "AC1"}],
    "L-executor-01.jsonl": [
        {"ts": stamp(0), "type": "criterion-cleared", "subject": S, "criterion": "AC1"}]})
assert exclear[1][S]["rejects"] == 1, \
    "the executor may REJECT (its merge gate is a gate) but may not CLEAR"

_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped +
                           [{"ts": stamp(0), "type": "rejected-criterion", "subject": S,
                             "criterion": "AC2"}]})
assert sp[S]["state"] == "shipped", "a standing rejected criterion blocks acceptance"

# authorization lives in the fold: a builder's self-issued verdict is recorded and IGNORED
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": True}],
    "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "shipped" and len(ig) == 1, "builder may not grade itself"

# the actor is the filename, never a field in the body
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": True, "actor": "grader"}]})
assert len(ig) == 1, "an actor field in the body is a stamp and is not read"

# D25 shipped-owed-evidence, and D76 retraction
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-executor-01.jsonl": shipped + [
    {"ts": stamp(0), "type": "owed-ac", "subject": S, "wake_at": stamp(-7)}]})
assert sp[S]["state"] == "shipped-owed-evidence"

_, sp, ch, _, _ = ledger(**{"L-builder-01.jsonl": built,
                            "L-operator-01.jsonl": [{"ts": stamp(0), "type": "charter-retracted",
                                                     "subject": C}]})
assert sp[S]["state"] == "dropped" and ch[C]["state"] == "retracted", "D76"

# L2-complete needs the sweep fixpoint, the charter review, AND count(owed) <= K
evs = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped + [
                    {"ts": stamp(0), "type": "sweep-fixpoint", "subject": C},
                    {"ts": stamp(0), "type": "charter-review-complete", "subject": C}]})
assert evs[2][C]["state"] == "L2-complete"

# the board renders every section, keeps empty ones, and carries its own provenance
board = fold.render(*evs)
assert board.startswith("# board · ") and f"fold @ {len(evs[0])}" in board
for h in ("NEEDS YOU", "BLOCKED", "WRITTEN, NOT PICKED UP", "IN FLIGHT",
          "AWAITING VERIFICATION", "OWED EVIDENCE", "CHARTER CLOSE",
          "SHIPPED SINCE YOU LOOKED", "DECIDED WITHOUT YOU", "HEALTH"):
    assert f"## {h}" in board, h
assert "## OWED EVIDENCE (0)" in board, "an empty section stays, showing (0)"
assert "restore drill: never run" in board, "an unproven backup is loud from day one"

# a torn tail never wedges the fold
(TMP / "events" / "L-builder-01.jsonl").open("a").write('{"ts": "x", "typ')
assert len(fold.read_events()) == len(evs[0]), "half-written line skipped, rest still folds"

# append lands, and re-reads to prove it (rule 4)
os.environ["DOIT_LEDGER_FILE"] = "L-operator-01.jsonl"
fold.append(["observed", "-"])

# an all-digit sha must not become an integer; a bool must still be a bool
ev = fold.append(["merge-gate-clean", "L-spec-x", "merged_tree=628758778891",
                  "sha=9b7e02d", "confirmed=false", "payload=[1,2]", "n:=17"])
assert ev["merged_tree"] == "628758778891", "an all-digit sha stays a string"
assert ev["sha"] == "9b7e02d"
assert ev["confirmed"] is False, "true/false/null are unambiguous and stay typed"
assert ev["payload"] == [1, 2] and ev["n"] == 17, "explicit JSON still works"

# a subject's project comes from the subject, not the caller's cwd — a script run
# from elsewhere must not strand its events under a second label (found by running
# merge-gate from the repo root, where cwd said `do-it-v2` and the charter said `do-it`)
here = pathlib.Path.cwd().name
assert fold.append(["charter-filed", "L-charter-new"])["project"] == here, "new subject: cwd"
os.chdir(TMP)
assert fold.append(["merge-gate-clean", "L-charter-new"])["project"] == here, \
    "known subject keeps its own project, whatever directory the caller ran from"
os.chdir(pathlib.Path(__file__).parent)

# D111 — an operator-only correction overrides one event by file:line, never deletes
tgt = fold.EVENTS / "L-operator-99.jsonl"
tgt.write_text(json.dumps({"ts": stamp(0), "type": "charter-retracted", "subject": "L-charter-fix",
                           "project": "wrong-label"}) + "\n")
(fold.EVENTS / "L-builder-99.jsonl").write_text(json.dumps(
    {"ts": stamp(0), "type": "correction", "subject": "L-charter-fix",
     "ref": "L-operator-99.jsonl:1", "set": {"type": "voided", "actor": "operator"}}) + "\n")
ev = fold.read_events()
assert [e for e in ev if e["_src"] == "L-operator-99.jsonl:1"][0]["type"] == "charter-retracted", \
    "a correction from a builder does nothing — authorization is by filename (D90)"
tgt.open("a").write(json.dumps({"ts": stamp(0), "type": "correction", "subject": "L-charter-fix",
                                "ref": "L-operator-99.jsonl:1",
                                "set": {"type": "voided", "project": "right-label",
                                        "actor": "impostor"}}) + "\n")
fixed = [e for e in fold.read_events() if e["_src"] == "L-operator-99.jsonl:1"][0]
assert fixed["type"] == "voided", "the operator's correction lands — a mis-fired retraction is recoverable"
assert fixed["actor"] == "operator", "actor is never overridable (D90) — it is the whole authorization basis"
assert fixed["project"] == "right-label", "a mislabelled project is correctable"
os.environ_project = None
fold.PROJECT = "right-label"
assert any(e["_src"] == "L-operator-99.jsonl:1" for e in fold.read_events()), \
    "corrections apply BEFORE the project filter — else the mis-write they fix is already gone"
fold.PROJECT = None
assert "corrections applied: 1" in fold.render(*((lambda e: (e,) + fold.fold(e))(fold.read_events()))), \
    "a silent correction is an edit with extra steps — and only APPLIED ones count; " \
    "the builder's rejected attempt is already on the unauthorized line"

shutil.rmtree(TMP)
print("fold: 33 checks pass")
