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

# D90: the actor is the filename, and a hyphenated role must not collapse to its
# first token — L-spec-writer and L-spec-auditor are two actors, not one "spec".
ev, *_ = ledger(**{"L-spec-writer-0007.jsonl": [{"ts": stamp(0), "type": "spec-written", "subject": S}],
                   "L-operator-local.jsonl": [{"ts": stamp(0), "type": "observed", "subject": S}]})
assert {e["actor"] for e in ev} == {"spec-writer", "operator"}, {e["actor"] for e in ev}
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

# D101 — a reviewer's must-fix blocks acceptance exactly like a rejected criterion,
# on its own. The wrapper writes a rejected-criterion beside each one today; that is
# the wrapper's choice and acceptance must not depend on it.
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed + [
                               {"ts": stamp(0), "type": "must-fix", "subject": S,
                                "criterion": "AC4", "reverify": "drive the flow again"}],
                           "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "shipped" and sp[S]["rejects"] == 1, "a standing must-fix blocks"
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded + [
    {"ts": stamp(0), "type": "criterion-cleared", "subject": S, "criterion": "AC4"}],
    "L-reviewer-01.jsonl": reviewed + [{"ts": stamp(1), "type": "must-fix", "subject": S,
                                        "criterion": "AC4"}],
    "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "accepted", "and a re-test that clears the criterion unblocks it"
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded + [
    {"ts": stamp(0), "type": "must-fix", "subject": S, "criterion": "AC4"}],
    "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "accepted" and len(ig) == 1, "must-fix is the reviewer's event (D101)"

# §4.4's May-declare line is authorization, not documentation: a declaration lands
# as an event typed by its term, so a term off the role's list is a stamp.
_, _, _, ig, by = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(0), "type": "worked", "subject": S, "line": "the cut held"},
    {"ts": stamp(0), "type": "hollow", "subject": S, "line": "not mine to call"}]})
assert len(ig) == 1 and ig[0]["type"] == "hollow", "the builder may declare worked, never hollow"
assert any(e["type"] == "worked" for e in by[S])

# authorization lives in the fold: a builder's self-issued verdict is recorded and IGNORED
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": True}],
    "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "shipped" and len(ig) == 1, "builder may not grade itself"

# the actor is the filename, never a field in the body
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built + [
    {"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": True, "actor": "grader"}]})
assert len(ig) == 1, "an actor field in the body is a stamp and is not read"

# D25 shipped-owed-evidence, and D76 retraction. `owed-ac` is on the spec-writer's
# and spec-auditor's May-declare lists and nobody else's — an executor that could
# declare one could walk any shipped spec into a terminal success state alone.
owed = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "wake_at": stamp(-7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": owed})
assert sp[S]["state"] == "shipped-owed-evidence"
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built,
                            "L-executor-01.jsonl": shipped + owed})
assert sp[S]["state"] == "shipped" and len(ig) == 1, \
    "only a role whose contract declares owed-ac may emit one"

_, sp, ch, _, _ = ledger(**{"L-builder-01.jsonl": built,
                            "L-operator-01.jsonl": [{"ts": stamp(0), "type": "charter-retracted",
                                                     "subject": C}]})
assert sp[S]["state"] == "dropped" and ch[C]["state"] == "retracted", "D76"

# L2-complete needs the sweep fixpoint, the charter review, AND count(owed) <= K
done = {"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
        "L-reviewer-01.jsonl": reviewed,
        "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "sweep-fixpoint", "subject": C}],
        "L-charter-reviewer-01.jsonl": [{"ts": stamp(0), "type": "charter-review-complete",
                                         "subject": C}]}
evs = ledger(**done)
assert evs[2][C]["state"] == "L2-complete"

# ...and the verdict is the charter-reviewer's to give. The Executor closing its
# own charter is §2.5's stamp, one level up from a spec.
_, _, ch, ig, _ = ledger(**{**done, "L-charter-reviewer-01.jsonl": [],
                            "L-executor-01.jsonl": done["L-executor-01.jsonl"] + [
                                {"ts": stamp(0), "type": "charter-review-complete", "subject": C}]})
assert ch[C]["state"] == "open" and len(ig) == 1, "a charter may not review itself complete"

# ...and it is the NEWEST verdict that counts. A charter reviewed complete, reopened
# and reviewed again as not-complete leaves L2 — set membership could never say so.
_, _, ch, _, _ = ledger(**{**done, "L-charter-reviewer-01.jsonl": [
    {"ts": stamp(2), "type": "charter-review-complete", "subject": C},
    {"ts": stamp(0), "type": "charter-review-not-complete", "subject": C}]})
assert ch[C]["state"] == "open", "the newest charter-review verdict decides, not any of them"
_, _, ch, _, _ = ledger(**{**done, "L-charter-reviewer-01.jsonl": [
    {"ts": stamp(2), "type": "charter-review-not-complete", "subject": C},
    {"ts": stamp(0), "type": "charter-review-complete", "subject": C}]})
assert ch[C]["state"] == "L2-complete", "and a re-review that passes closes it"

# the board renders every section, keeps empty ones, and carries its own provenance
evs = ledger(**done)                      # back to the L2 ledger the checks above left
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

# §4.4 Budget, checked. The cap is dispatch.ROLES / tick's, the usage is what the
# CLI returned on spawn-done, and the role is the FILENAME — a spawn cannot declare
# itself within budget. The Executor's own spend is capped too (its cap is the
# tick's), which is the one spend nothing else on the board reports.
caps = fold.caps()
assert caps["builder"] == (90, 15) and caps["executor"][1] > 0, caps
fine = {"ts": stamp(0), "type": "spawn-done", "subject": S, "spawn": "L-grader-0009",
        "cost_usd": 0.6, "duration_ms": 100_000}
_, sp, _, _, _ = ledger(**{"L-grader-0009.jsonl": [fine]})
assert not fold.over_budget(fold.read_events()), "a spawn inside both caps is silent"
_, *rest = ledger(**{"L-grader-0009.jsonl": [{**fine, "cost_usd": 9.5}],
                     "L-builder-0002.jsonl": [{**fine, "spawn": "L-builder-0002",
                                               "duration_ms": 100 * 60_000}],
                     "L-executor-0003.jsonl": [{**fine, "spawn": "L-executor-0003",
                                                "cost_usd": 99.0}]})
over = fold.over_budget(fold.read_events())
joined = " | ".join(over)
assert len(over) == 3 and "grader · $9.50 > $3" in joined and "builder · 100m > 90m" in joined, over
assert "L-executor-0003 · executor · $99.00 > $5" in joined, \
    "the driver's own spend is compared against the tick's cap, not exempt"
assert "budget-exceeded: 3 spawn(s) over cap" in fold.render(fold.read_events(), *rest)

# §4.9 — "wait indefinitely is a wedge, not a default". A question past its deadline
# with nothing naming it is the operator's; a decision naming its file:line is not.
q = {"ts": stamp(2), "type": "question", "subject": S, "asks": "refunds inline or deferred?",
     "blocks": ["AC3"], "default": "defer", "deadline": stamp(1)}
assert "unanswered past" in fold.render(*ledger(**{"L-builder-01.jsonl": [q]}))
live = fold.render(*ledger(**{"L-builder-01.jsonl": [{**q, "deadline": stamp(-3)}]}))
assert "unanswered past" not in live, "a deadline still ahead is the lane's, not yours"
answered = fold.render(*ledger(**{"L-builder-01.jsonl": [q], "L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "decision", "subject": S, "ref": "L-builder-01.jsonl:1",
     "why": "defer, per ADR-0009", "revert": "revert the commit"}]}))
assert "unanswered past" not in answered, "a decision naming its file:line answers it"
assert "## NEEDS YOU (0)" in answered
bad = fold.render(*ledger(**{"L-builder-01.jsonl": [{**q, "deadline": "sometime"}]}))
assert "unanswered past" in bad, "an unparseable deadline is past — undetermined is never clean"

# ...and an escalation the operator answered leaves NEEDS YOU. The rule is the
# tick's verbatim — newest of escalation-blocking / decision / unblocked per subject
# — because a board that still shows it while the tick has put the subject back on
# the lane is two answers to one question.
esc = {"L-executor-01.jsonl": [{"ts": stamp(2), "type": "escalation-blocking", "subject": S,
                                "why": "the gate wants a --writes grant"}]}
assert "the gate wants" in fold.render(*ledger(**esc))
ok = fold.render(*ledger(**{"L-executor-01.jsonl": esc["L-executor-01.jsonl"] + [
    {"ts": stamp(0), "type": "decision", "subject": S, "why": "pass the footprint",
     "revert": "n/a"}]}))
assert "## NEEDS YOU (0)" in ok, "a decision after the escalation closes it"
again = fold.render(*ledger(**{"L-executor-01.jsonl": esc["L-executor-01.jsonl"] + [
    {"ts": stamp(1), "type": "decision", "subject": S, "why": "x", "revert": "n/a"},
    {"ts": stamp(0), "type": "escalation-blocking", "subject": S, "why": "and now this"}]}))
assert "and now this" in again, "a fresh escalation after the decision reopens it"

# §12.5's expected dwell, measured. Under DWELL_MIN_N crossings the default stands;
# at DWELL_MIN_N the median crossing sets the bar, and a build slower than every
# build before it wedges while one merely slower than the 1-day default does not.
def crossed(n, days):
    return {f"L-builder-{i:02d}.jsonl": [
        {"ts": stamp(days + 1), "type": "spec-written", "subject": f"L-spec-{i:04d}"},
        {"ts": stamp(days), "type": "build-started", "subject": f"L-spec-{i:04d}"},
        {"ts": stamp(0), "type": "build-done", "subject": f"L-spec-{i:04d}"}] for i in range(n)}
_, _, _, _, by = ledger(**crossed(2, 3))
assert fold.dwell_days(by)["building"] == 1, "two crossings is not a measurement"
slow = {"L-builder-99.jsonl": [{"ts": stamp(4), "type": "spec-written", "subject": "L-spec-9999"},
                               {"ts": stamp(4), "type": "build-started", "subject": "L-spec-9999"}]}
_, sp, _, _, by = ledger(**{**crossed(3, 3), **slow})
assert fold.dwell_days(by)["building"] == 6, fold.dwell_days(by)   # 2 x median(3, 3, 3)
assert not fold.wedged(sp["L-spec-9999"], fold.dwell_days(by)), \
    "4 days building is a wedge against the 1-day default and NOT against the measured 6"
_, sp, _, _, by = ledger(**{**crossed(3, 3), "L-builder-99.jsonl": [
    {"ts": stamp(7), "type": "spec-written", "subject": "L-spec-9999"},
    {"ts": stamp(7), "type": "build-started", "subject": "L-spec-9999"}]})
assert fold.wedged(sp["L-spec-9999"], fold.dwell_days(by)), "7 days is past twice the median"

shutil.rmtree(TMP)
# D117: liveness is a fold query. Fresh tick: fine. Old tick: the alarm. No tick: says so.
mins = lambda m: (T - timedelta(minutes=m)).isoformat(timespec="seconds")
fresh = ledger(**{"L-tick-local.jsonl": [{"ts": mins(1), "type": "tick", "lane": 0}]})
assert "last tick: 1m ago" in fold.render(*fresh) and "STALE" not in fold.render(*fresh)
stale = ledger(**{"L-tick-local.jsonl": [{"ts": mins(30), "type": "tick", "lane": 0}]})
assert "TICK STALE" in fold.render(*stale)
assert "last tick: never" in fold.render(*ledger(**{"L-operator-local.jsonl": []}))

print("fold: 55 checks pass")
