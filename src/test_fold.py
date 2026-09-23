#!/usr/bin/env python3
"""One runnable check on the fold rules. Run: python3 test_fold.py"""
import json, os, pathlib, shutil, sys, tempfile, types
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
# INBOUND (R9/L-spec-0196) calls the REAL carry.uncarried() when `carry` is not
# explicitly stubbed (AC22 and friends). carry.py's own ledger/inbox dirs key off
# V4_LEDGER_DIR/V4_INBOX_DIR, NOT DOIT_ROOT — pinned here too, or this file would
# read the operator's real ~/.claude/ledger and ~/.claude/spec-inbox (measured:
# it does, 15 real records, the moment this line is absent).
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger-absent")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox-absent")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold  # noqa: E402

# src/panes.py has landed (L-adr-0044), so every render() below now calls the REAL
# live_panes() — against the operator's own ~/.claude/sessions unless this is
# pinned. A check that reads live machine state is not a check. Pinned to an empty
# scratch dir so LIVE PANES renders deterministically; the section's own behaviour
# is proved further down against a stub, on both the present and absent legs.
SESSIONS_DEFAULT = fold.SESSIONS
fold.SESSIONS = TMP / "sessions"

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

# §3.11's L1 conjunct is the Planner's claim, and nothing derives it: the first
# real charter had every spec accepted and stayed `open`, so the Executor's close
# row was unreachable. A builder saying so is recorded and ignored like any other
# unauthorized emit.
l1 = ledger(**{"L-planner-01.jsonl": [built[0], {"ts": stamp(0), "type": "l1-complete", "subject": C}]})
assert l1[2][C]["state"] == "L1-complete", l1[2][C]["state"]
notl1 = ledger(**{"L-planner-01.jsonl": [built[0]], "L-builder-01.jsonl": [
    {"ts": stamp(0), "type": "l1-complete", "subject": C}]})
assert C not in notl1[2] and notl1[3], \
    "only the Planner or the operator may declare L1-complete; a builder's is ignored"

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

# §3.12 — completion is a fixpoint. An IN-SCOPE brief (one citing the charter
# requirement it serves, §2.6) holds the close open no matter what else is true:
# before this rule the Executor's own row said "briefs open → nothing until
# specced" and nothing stopped a `sweep-fixpoint` being stamped over one.
L1 = {**done, "L-planner-01.jsonl": [{"ts": stamp(3), "type": "l1-complete", "subject": C}]}
inscope = [{"ts": stamp(1), "type": "brief", "subject": C, "requirement": "R3",
            "blocked_me": False, "hit_while": S, "fact": "the no-spawn project renders silence"}]
_, _, ch, _, _ = ledger(**{**L1, "L-grader-02.jsonl": inscope})
assert ch[C]["state"] == "L1-complete" and ch[C]["briefs"] == 1, \
    "an unanswered in-scope brief holds the sweep open (§3.12)"

# ...and the negative, which is the whole reason the citation is the criterion:
# an ADJACENT brief is the Thinker's inbox (§7.9), never this charter's blocker.
adjacent = [{k: v for k, v in inscope[0].items() if k != "requirement"}]
_, _, ch, _, _ = ledger(**{**L1, "L-grader-02.jsonl": adjacent})
assert ch[C]["state"] == "L2-complete" and ch[C]["briefs"] == 0, \
    "no citable requirement -> adjacent -> not this charter's (§2.6)"

# a spec answers it by `ref` — the same file:line handle a decision uses
answered = [{"ts": stamp(0), "type": "brief-answered", "subject": C,
             "ref": "L-grader-02.jsonl:1", "spec": "L-spec-0143"}]
_, _, ch, _, _ = ledger(**{**L1, "L-grader-02.jsonl": inscope,
                           "L-executor-02.jsonl": answered})
assert ch[C]["state"] == "L2-complete", "brief-answered by ref discharges it"

_, _, ch, _, _ = ledger(**{**L1, "L-grader-02.jsonl": inscope, "L-executor-02.jsonl":
                           [{**answered[0], "ref": "L-grader-02.jsonl:7"}]})
assert ch[C]["state"] == "L1-complete", "a ref naming nothing answers nothing"

# both new events are L2 conjuncts in all but name, so both are authorized: a
# builder that may stamp either closes a charter from the seat being judged.
_, _, ch, ig, _ = ledger(**{**L1, "L-grader-02.jsonl": inscope,
                            "L-builder-02.jsonl": answered})
assert ch[C]["state"] == "L1-complete" and len(ig) == 1, "only the Executor may answer a brief"
_, _, ch, ig, _ = ledger(**{**L1, "L-executor-01.jsonl": shipped,
                            "L-builder-02.jsonl": [{"ts": stamp(0), "type": "sweep-fixpoint",
                                                    "subject": C}]})
assert ch[C]["state"] == "L1-complete" and len(ig) == 1, "a builder may not declare the sweep done"

# and the board says so, because a close blocked by something invisible is a wedge
evs = ledger(**{**L1, "L-grader-02.jsonl": inscope})
assert "1 in-scope brief(s) open" in fold.render(*evs), "CHARTER CLOSE names what holds it"

# A `charter` field is sometimes a PATH — that is what the packet hands the role,
# and the ledger is append-only, so both forms are permanent input to every fold.
# Unnormalised, the spec belonged to NO charter: `mine` was empty, so `doit reap`
# skipped its worktree and reported success, the charter-reviewer's packet named
# one card for a charter that shipped two, and — the severe half — the L2
# conjuncts are quantified over `mine`, so the charter closed without it.
P = f"/Users/x/.do-it/content/{C}.md"
pathish = {**done, "L-builder-01.jsonl": [{**built[0], "charter": P}] + built[1:]}
_, sp, ch, _, _ = ledger(**pathish)
assert sp[S]["charter"] == C, sp[S]["charter"]
assert ch[C]["state"] == "L2-complete", "a path-form charter still closes on its own spec"
# the negative, and it is the one that matters: an unaccepted spec named by path
# must hold the close, exactly as an id-named one does.
_, sp, ch, _, _ = ledger(**{k: v for k, v in pathish.items() if k != "L-reviewer-01.jsonl"})
assert sp[S]["state"] == "shipped" and ch[C]["state"] == "open", \
    "a path-named spec that is not accepted must block L2, not vanish from the charter"
# and retraction reaches it too — D76's drop is the same comparison
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": [{**built[0], "charter": P}],
                           "L-operator-01.jsonl": [{"ts": stamp(0), "type": "charter-retracted",
                                                    "subject": C}]})
assert sp[S]["state"] == "dropped", "a retracted charter drops its path-named specs too"
assert fold.charter_id(None) is None and fold.charter_id(C) == C, "an id normalises to itself"

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

# OWED EVIDENCE renders the first owed-ac that CARRIES a wake_at — a declaration
# without one (the pre-schema shape) must not hide the instant a later one names.
(fold.EVENTS / "L-spec-writer-77.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
    {"ts": stamp(0), "type": "spec-written", "subject": "L-spec-0077", "charter": "L-charter-owed"},
    {"ts": stamp(0), "type": "owed-ac", "subject": "L-spec-0077", "line": "no instant here"},
    {"ts": stamp(0), "type": "owed-ac", "subject": "L-spec-0077", "criterion": "AC1", "wake_at": "2099-01-01T00:00:00Z", "line": "x"}]))
(fold.EVENTS / "L-executor-77.jsonl").write_text(json.dumps(
    {"ts": stamp(0), "type": "shipped", "subject": "L-spec-0077", "sha": "abc", "charter": "L-charter-owed"}) + "\n")
_ev = fold.read_events(); _sp, _ch, _ig, _by = fold.fold(_ev)
assert _sp["L-spec-0077"]["state"] == "shipped-owed-evidence", _sp["L-spec-0077"]["state"]
assert "wakes 2099-01-01T00:00:00Z" in fold.render(_ev, _sp, _ch, _ig, _by), "the render names the instant, not None"
for _f in ("L-spec-writer-77.jsonl", "L-executor-77.jsonl"):
    (fold.EVENTS / _f).unlink()

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

# L-charter-0002 — spend, derived at fold time. One row per project the ledger
# knows, a `$` never travelling without the words that say what it is not, and a
# spawn count on every row including zero. The fixtures write under
# L-operator-local: `over_budget` is untouched by this charter and raises on a
# string cost, so an actor outside fold.caps() is what keeps the two apart.
spend = lambda evs: [l for l in fold.render(*evs).splitlines() if l.startswith("  spend · ")]
sp_ev = lambda **kw: {"ts": stamp(0), "type": "spawn-done", "subject": S, **kw}

# the cost_usd predicate, on every shape the ledger can carry: None, ABSENT, the
# STRING "1.5" that `doit append … cost_usd=1.5` writes, and a float. Two priced,
# two unpriced, nothing raised, and the two unpriced ones SAY so.
rows = spend(ledger(**{"L-operator-local.jsonl": [
    sp_ev(project="p", cost_usd=None), sp_ev(project="p"),
    sp_ev(project="p", cost_usd="1.5"),
    sp_ev(project="p", type="spawn-failed", cost_usd=0.4)]}))
assert len(rows) == 1 and "spend · p · $1.90 · 4 spawns · 2 unpriced" in rows[0], rows
assert "list-price estimate" in rows[0] and "not money billed" in rows[0], \
    "a $ never renders without both phrases on the same line"
for bad in (float("nan"), float("inf"), True, "later", [1], {"a": 1}):
    r = spend(ledger(**{"L-operator-local.jsonl": [sp_ev(project="p", cost_usd=bad),
                                                   sp_ev(project="p", cost_usd=2.0)]}))
    assert "$2.00 · 2 spawns · 1 unpriced" in r[0], (bad, r)
assert " unpriced" not in spend(ledger(**{"L-operator-local.jsonl": [
    sp_ev(project="p", cost_usd=1.0)]}))[0], "a fully priced row never says unpriced"

# SD3b — a failed spawn drew real money, so it is summed and counted like any other
rows = spend(ledger(**{"L-operator-local.jsonl": [
    sp_ev(project="p", cost_usd=0.6), sp_ev(project="p", type="spawn-failed", cost_usd=0.4)]}))
assert "spend · p · $1.00 · 2 spawns" in rows[0], rows

# zero is a measurement, not silence: a project the ledger names but never spawned
# for still gets its row — an unmeasured project must not read as a free one.
rows = spend(ledger(**{"L-operator-local.jsonl": [
    sp_ev(project="rich", cost_usd=3.0),
    {"ts": stamp(0), "type": "merge-gate-clean", "subject": S, "project": "quiet"}]}))
assert "spend · rich · $3.00 · 1 spawns" in rows[0] and "spend · quiet · $0.00 · 0 spawns" in rows[1], \
    rows                                            # and dollars descending orders them

# L-adr-0001 — unattributed spend is its own row and is spread across nobody. The
# tick's own spawns carry no project (its base is {"spawn": …}), so this is today's
# board, not a hypothetical.
rows = spend(ledger(**{"L-operator-local.jsonl": [
    sp_ev(project="p", cost_usd=1.0), sp_ev(cost_usd=2.0)]}))
assert "spend · p · $1.00 · 1 spawns" in rows[0], "named projects keep only their own"
assert "spend · (no project) · $2.00 · 1 spawns" in rows[-1], \
    "the residue is last and is a row, not a share of anyone's"
assert rows[0].endswith("free one (R2/R3)") and "attributed only" not in rows[0], \
    "unfiltered, the figure is not an attributed share"

# §9.1/D93 — under the filter, one project's row, and it says the figure is
# attributed rather than total. PROJECT is read inside read_events at call time.
fold.PROJECT = "p"
try:
    rows = spend(ledger(**{"L-operator-local.jsonl": [
        sp_ev(project="p", cost_usd=1.0), sp_ev(project="q", cost_usd=5.0), sp_ev(cost_usd=2.0)]}))
finally:
    fold.PROJECT = None
assert len(rows) == 1 and "spend · p · $1.00 · 1 spawns · attributed only" in rows[0], rows
assert "list-price estimate" in rows[0], "the filtered row still says what it is not"

# R3 — and the filter value seeds its OWN row, so a project the ledger never names
# reads zero-out-of-zero-spawns rather than as silence. The `attributed only`
# caveat stays on the zero row: drop it only when the number is zero and the
# reader learns the caveat is about size.


def sections(b):
    """The '## ' headings fold.py itself wrote — everything from NEEDS YOU down.

    The PLANNER WAITING ON block sits ABOVE NEEDS YOU and is a verbatim
    pass-through of relay's waiting_lines() (R5/AC4): fold.py may not strip a
    prefix it did not add, so a '## ' inside that block is relay's line, not a
    fold.py section, and counting it would break these checks with nobody at
    fault. Everything the forgery checks below care about (a label reaching SPEND
    or HEALTH) is downstream of NEEDS YOU, so this loses none of their sharpness.
    """
    return [l for l in b[b.index("## NEEDS YOU"):].splitlines() if l.startswith("## ")]


fold.PROJECT = "ghost"
try:
    rows = spend(ledger(**{"L-operator-local.jsonl": [sp_ev(project="p", cost_usd=1.0)]}))
    empty = ledger(**{"L-operator-local.jsonl": []})
    erows, eboard = spend(empty), fold.render(*empty)
finally:
    fold.PROJECT = None
for r in (rows, erows):
    assert len(r) == 1 and "spend · ghost · $0.00 · 0 spawns · attributed only" in r[0], r
    assert "list-price estimate" in r[0] and " unpriced" not in r[0], r
# 12, not 11: §8.3's ten, plus SPEND (retro step 9) and now LIVE PANES (R14).
# These three counts are the check that a forged label cannot invent a section —
# the NUMBER moves when a section is deliberately added, the check does not.
# Counted through sections() (the PLANNER WAITING ON pass-through is relay's
# lines, not a fold.py section) so both rules hold at once.
assert len(sections(eboard)) == 16, \
    "an empty ledger under a filter still renders every section, and does not raise"

# ...and that label is now UNVOUCHED: DOIT_PROJECT is operator environment reaching
# the board with no event behind it, so it must pass the same collapse as a ledger
# label or it forges an extra section — §8.3's sections (ten, plus SPEND, retro
# step 9) are positional.
fold.PROJECT = "x\n## FORGED (9)"
try:
    forged_env = ledger(**{"L-operator-local.jsonl": [sp_ev(project="p", cost_usd=1.0)]})
    frows, fboard = spend(forged_env), fold.render(*forged_env)
finally:
    fold.PROJECT = None
assert len(sections(fboard)) == 16, "sections, always"
assert len(frows) == 1 and "spend · x ## FORGED (9) · $0.00 · 0 spawns" in frows[0], frows

# a label is a DIRECTORY NAME by default and nothing curates it: a newline in one
# must not forge a board line, and above all not an extra section — §8.3's
# positional layout is the whole reason that check exists.
forged = ledger(**{"L-operator-local.jsonl": [sp_ev(project="x\n## FORGED (9)", cost_usd=1.0)]})
board = fold.render(*forged)
assert len(sections(board)) == 16, "sections, always"
assert len(spend(forged)) == 1 and "spend · x ## FORGED (9) · $1.00 · 1 spawns" in spend(forged)[0], \
    spend(forged)

# retro step 9 (operator ask, 2026-09-15) — SPEND: a per-model token block of its
# own, one row per model, split spawns' four columns summed separately from
# blended-only spawns' subagent_tokens (the two are never combined — they
# measure different things, src/usage.py), a weighted total from models.toml's
# [weights], and a final unmeasured count.
(TMP / "models.toml").write_text(
    '[defaults]\nbackend = "seat"\n'
    '[weights.claude-sonnet-5]\ninput = 1.0\noutput = 5.0\ncache_read = 0.1\ncache_creation = 1.25\n')
tok_ev = lambda **kw: {"ts": stamp(0), "type": "spawn-done", "subject": S, "spawn": "L-grader-9001", **kw}
def spend_block(evs):
    # ★ Stops at the NEXT section header, whatever it is — not at "## HEALTH" by
    # name. LIVE PANES (R14) now sits between SPEND and HEALTH, and a helper that
    # names its neighbour breaks every time a section is legitimately added.
    lines = fold.render(*evs).split("## SPEND")[1].strip().splitlines()
    out = []
    for l in lines[1:]:                          # lines[0] is the "(n)" count suffix
        if l.startswith("## "):
            break
        out.append(l.strip())
    while out and not out[-1]:
        out.pop()
    return out

split_only = ledger(**{"L-operator-local.jsonl": [
    tok_ev(model_used="claude-sonnet-5", input_tokens=100, output_tokens=10,
          cache_read=50, cache_creation=20)]})
rows = spend_block(split_only)
assert rows[0].startswith("claude-sonnet-5 · 1 spawns · in 100 out 10 cache_read 50 cache_creation 20"), rows
# weighted = 100*1.0 + 10*5.0 + 50*0.1 + 20*1.25 = 180
assert "weighted 180 input-equiv tokens" in rows[0], rows
assert rows[-1] == "unmeasured: 0 spawn(s) carry no usage at all", rows

blended_only = ledger(**{"L-operator-local.jsonl": [
    tok_ev(model_used="claude-opus-5", subagent_tokens=89662)]})
rows = spend_block(blended_only)
assert "claude-opus-5 · 1 spawns · blended 89,662 tokens (1 unsplit seat spawn(s))" in rows[0], rows

no_weights = ledger(**{"L-operator-local.jsonl": [
    tok_ev(model_used="gpt-unweighed", input_tokens=1, output_tokens=1, cache_read=0, cache_creation=0)]})
rows = spend_block(no_weights)
assert "weighted n/a — no [weights] for this model" in rows[0], \
    "a model with no [weights] entry renders unweighted, never a fabricated ratio"

neither = ledger(**{"L-operator-local.jsonl": [tok_ev(model_used="m")]})
rows = spend_block(neither)
assert rows[-1] == "unmeasured: 1 spawn(s) carry no usage at all", rows

# a spawn genuinely measured at all-zero tokens (all four present, all 0) must
# still render as measured — a truthy-sum check would wrongly fall through to
# looking unmeasured.
all_zero = ledger(**{"L-operator-local.jsonl": [
    tok_ev(model_used="claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0)]})
rows = spend_block(all_zero)
assert rows[0].startswith("claude-sonnet-5 · 1 spawns · in 0 out 0 cache_read 0 cache_creation 0"), rows
assert rows[-1] == "unmeasured: 0 spawn(s) carry no usage at all", rows

# unattributed spend (no `model_used`/`model_requested`/`model` at all) groups
# under "(unknown)" rather than silently vanishing.
unknown = ledger(**{"L-operator-local.jsonl": [{"ts": stamp(0), "type": "spawn-done", "subject": S,
                                                "spawn": "L-x-0001"}]})
rows = spend_block(unknown)
assert rows[0].startswith("(unknown) · 1 spawns") and rows[-1] == "unmeasured: 1 spawn(s) carry no usage at all", rows

# `doit spend <charter|spec|spawn-id>` — a per-spawn table + totals, and for a
# charter the stage wall clock. Read-only: never touches board.md.
stamp_h = lambda hh: (T - timedelta(hours=hh)).isoformat(timespec="seconds")   # larger hh = further in the past
CID = "L-charter-0099"
charter_evs = {
    "L-planner-01.jsonl": [{"ts": stamp_h(50), "type": "cut-written", "subject": CID},
                           {"ts": stamp_h(45), "type": "cut-written", "subject": CID},  # a re-cut; the FIRST still wins
                           {"ts": stamp_h(40), "type": "l1-complete", "subject": CID}],
    "L-builder-9002.jsonl": [{"ts": stamp_h(35), "type": "spawn-started", "subject": S, "spawn": "L-builder-9002",
                              "charter": CID},
                             {"ts": stamp_h(30), "type": "spawn-done", "subject": S, "spawn": "L-builder-9002",
                              "charter": CID, "model_used": "claude-sonnet-5", "input_tokens": 200,
                              "output_tokens": 40, "cache_read": 100, "cache_creation": 10, "duration_ms": 60000}],
    "L-executor-01.jsonl": [{"ts": stamp_h(25), "type": "shipped", "subject": S, "charter": CID}],
    "L-charter-reviewer-01.jsonl": [{"ts": stamp_h(5), "type": "charter-review-complete", "subject": CID}],
    "L-executor-02.jsonl": [{"ts": stamp_h(1), "type": "tree-reaped", "subject": CID}],
}
before_board = (TMP / "board.md").read_text() if (TMP / "board.md").is_file() else None
ev, sp, ch, ign, by_subj = ledger(**charter_evs)
assert sp[S]["charter"] == CID, "the spec's own spawn-done events carry the charter field"
out = fold.render_spend(CID, ev, sp)
assert "L-builder-9002 · builder · claude-sonnet-5" in out and "in 200 out 40 cache_read 100 cache_creation 10" in out, out
assert "weighted 422" in out, out          # 200*1 + 40*5 + 100*0.1 + 10*1.25 = 422.5 -> 422
assert "1.0m" in out, "duration_ms=60000 -> 1.0 minute"
assert "cut-written → l1-complete" in out and "charter-review-complete → tree-reaped" in out
assert (TMP / "board.md").read_text() == before_board or before_board is None, \
    "doit spend must never write board.md"

# a spec id narrows to that spec's own spawns
out_spec = fold.render_spend(S, ev, sp)
assert "L-builder-9002" in out_spec

# a bare spawn id renders that one row alone
out_spawn = fold.render_spend("L-builder-9002", ev, sp)
assert out_spawn.startswith(f"# spend · L-builder-9002 · 1 spawn(s)") and "L-builder-9002" in out_spawn
assert "## stage wall clock" not in out_spawn, "the stage clock is a charter-only section"

# a subject with no spawns at all renders zero rows, never raises
out_none = fold.render_spend("L-spec-9999", ev, sp)
assert "0 spawn(s)" in out_none

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

# ── a-4·vi: a deploy-started with no terminal event renders IN FLIGHT (S34) ────
S9, sha_full = "L-spec-0900", "abcdef1234567890"
inflight = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9,
     "sha": sha_full, "target": "prod", "log": "/tmp/x.log"}]})
board = fold.render(*inflight)
assert "deploy in flight · L-spec-0900 · abcdef1 ·" in board, \
    "★ vi: a killed deploy wrapper is visible on the board, not silently lost"

landed = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9, "sha": sha_full, "target": "prod"},
    {"ts": stamp(0), "type": "deploy-landed", "subject": S9, "sha": sha_full, "target": "prod"}]})
assert "deploy in flight" not in fold.render(*landed), "a landed deploy is no longer in flight"

refused = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9, "sha": sha_full, "target": "prod"},
    {"ts": stamp(0), "type": "deploy-refused", "subject": S9, "sha": sha_full, "target": "prod"}]})
assert "deploy in flight" not in fold.render(*refused), "a refused deploy is no longer in flight either"

failed = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9, "sha": sha_full, "target": "prod"},
    {"ts": stamp(0), "type": "deploy-failed", "subject": S9, "sha": sha_full, "target": "prod"}]})
assert "deploy in flight" not in fold.render(*failed), "a failed deploy is no longer in flight either"

# a-6 (S15/S33) — owed evidence can actually be owed, and a shipped spec can be
# closed truthfully.

# 1/2. A grader `cannot-assess` on an OWED row (an `owed-ac` on the subject
# already names the criterion) must not zero `confirmed` — the fold reads
# `confirmed`/`matches_intent`/`card_ok`/`cannot_assess` and derives confirmed
# over the evaluable rows. Not yet `accepted`, though: the owed criterion itself
# still has no `owed-met`, so it stays `shipped-owed-evidence`.
owed_ac7 = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC7", "wake_at": stamp(-7)}]
cannot_assess_owed = [{"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False,
                       "matches_intent": "yes", "card_ok": "yes", "cannot_assess": ["AC7"]}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_owed,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": owed_ac7})
assert sp[S]["state"] == "shipped-owed-evidence", \
    "cannot-assess on an owed row doesn't zero confirmed, but the owed criterion is still unmet"

# ...and the negative: cannot-assess on a row NOBODY declared owed must still
# zero confirmed exactly as before.
cannot_assess_nonowed = [{"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False,
                          "matches_intent": "yes", "card_ok": "yes", "cannot_assess": ["AC9"]}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_nonowed,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped})
assert sp[S]["state"] == "shipped", \
    "cannot-assess on a row nobody declared owed still is not confirmed"

# 1. An `owed-met` (Executor or operator, citing the evidence) discharges the
# owed criterion, and `accepted` derives with no new verdict — no re-grade spawn.
owed_met = [{"ts": stamp(0), "type": "owed-met", "subject": S, "criterion": "AC7",
            "evidence": "the post-merge check-run: https://ci/run/42 green"}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_owed,
                           "L-reviewer-01.jsonl": reviewed,
                           "L-executor-01.jsonl": shipped + owed_met,
                           "L-spec-writer-01.jsonl": owed_ac7})
assert sp[S]["state"] == "accepted", "owed-met discharges the owed criterion; the fold derives accepted alone"

# ...and only the Executor or the operator may write it — the same restriction
# as `owed-ac` one level up, for the same reason: a builder that could clear its
# own owed criterion could walk a shipped spec to `accepted` from one seat.
_, sp, _, ig, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_owed,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": owed_ac7,
                           "L-builder-02.jsonl": owed_met})
assert sp[S]["state"] == "shipped-owed-evidence" and len(ig) == 1, \
    "only the executor or the operator may discharge an owed criterion"

# 5. The board names a partially-discharged owed spec: one criterion met and
# awaiting the next fold, the other still just waiting on its wake_at.
owed_two = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC7", "wake_at": stamp(-7)},
            {"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC9", "wake_at": stamp(-3)}]
cannot_assess_both = [{"ts": stamp(1), "type": "verdict", "subject": S, "confirmed": False,
                       "matches_intent": "yes", "card_ok": "yes", "cannot_assess": ["AC7", "AC9"]}]
partial = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_both,
                    "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped + owed_met,
                    "L-spec-writer-01.jsonl": owed_two})
assert partial[1][S]["state"] == "shipped-owed-evidence", partial[1][S]["state"]
pboard = fold.render(*partial)
assert "AC7 met, awaiting fold" in pboard and "AC9 met, awaiting fold" not in pboard, pboard
# and a spec with no owed-met at all renders exactly as before — no note.
plain_owed = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": cannot_assess_owed,
                       "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                       "L-spec-writer-01.jsonl": owed_ac7})
assert "met, awaiting fold" not in fold.render(*plain_owed), "no owed-met yet: the line is unchanged"

# 3. D112 + S33 — the operator's only close instrument must not call a BUILT,
# MERGED spec `closed-unbuilt`. A `shipped` event on the subject makes it
# `closed-shipped` instead, and that label counts as done for the charter's L2
# conjunct exactly like `closed-unbuilt` does.
closed_shipped = ledger(**{"L-builder-01.jsonl": built, "L-executor-01.jsonl": shipped,
                           "L-operator-01.jsonl": [{"ts": stamp(0), "type": "spec-closed", "subject": S,
                                                    "charter": C, "why": "post-merge observation only"}]})
assert closed_shipped[1][S]["state"] == "closed-shipped", closed_shipped[1][S]["state"]
l2_with_closed_shipped = ledger(**{
    "L-builder-01.jsonl": built,
    "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "sweep-fixpoint", "subject": C}],
    "L-operator-01.jsonl": [{"ts": stamp(0), "type": "spec-closed", "subject": S, "charter": C,
                             "why": "post-merge observation only"}],
    "L-charter-reviewer-01.jsonl": [{"ts": stamp(0), "type": "charter-review-complete", "subject": C}]})
assert l2_with_closed_shipped[1][S]["state"] == "closed-shipped"
assert l2_with_closed_shipped[2][C]["state"] == "L2-complete", \
    "closed-shipped counts as done for the charter's L2 conjunct (S33)"
# and the untouched case is unchanged: never built at all is still closed-unbuilt.
never_built = ledger(**{"L-planner-01.jsonl": [built[0]], "L-operator-01.jsonl": [
    {"ts": stamp(0), "type": "spec-closed", "subject": S, "charter": C, "why": "answered another way"}]})
assert never_built[1][S]["state"] == "closed-unbuilt", never_built[1][S]["state"]

# 4. S32/S33 — an allocation whose spec-writer spawn failed and never wrote a
# spec derives `void`, not a pipeline state it looks stuck in.
ghost = {"L-spec-writer-09.jsonl": [
    {"ts": stamp(1), "type": "spawn-started", "subject": "L-spec-0999", "role": "spec-writer", "charter": C},
    {"ts": stamp(0), "type": "spawn-failed", "subject": "L-spec-0999", "why": "timeout after 30 min"}]}
_, sp, _, _, _ = ledger(**ghost)
assert sp["L-spec-0999"]["state"] == "void", sp["L-spec-0999"]["state"]
# ...and a retry that DOES land a spec-written is never void, even carrying the
# same subject's earlier failure.
retried = ledger(**{"L-spec-writer-09.jsonl": ghost["L-spec-writer-09.jsonl"] + [
    {"ts": stamp(0), "type": "spec-written", "subject": "L-spec-0999", "charter": C}]})
assert retried[1]["L-spec-0999"]["state"] == "written", retried[1]["L-spec-0999"]["state"]
# a void allocation does not bind — and does not block — its charter's L2
# conjunct: `done` below is otherwise a clean L2-complete charter (see the fixed
# ledger built earlier); adding one ghost spec on the same charter must not hold
# it at L1 the way the real L-spec-0004 did.
voidbind = ledger(**{**done, **ghost})
assert voidbind[1]["L-spec-0999"]["state"] == "void"
assert voidbind[2][C]["state"] == "L2-complete", \
    "a void allocation does not bind the charter's L2 conjunct (S32/S33)"

shutil.rmtree(TMP)
# D117: liveness is a fold query. Fresh tick: fine. Old tick: the alarm. No tick: says so.
mins = lambda m: (T - timedelta(minutes=m)).isoformat(timespec="seconds")
fresh = ledger(**{"L-tick-local.jsonl": [{"ts": mins(1), "type": "tick", "lane": 0}]})
assert "last tick: 1m ago" in fold.render(*fresh) and "STALE" not in fold.render(*fresh)
stale = ledger(**{"L-tick-local.jsonl": [{"ts": mins(30), "type": "tick", "lane": 0}]})
assert "TICK STALE" in fold.render(*stale)
assert "last tick: never" in fold.render(*ledger(**{"L-operator-local.jsonl": []}))

# R5/R6 — PLANNER WAITING ON. relay-queries is the producer; this file only proves
# the SLOT: the lines it returns reach the board verbatim, above NEEDS YOU, and a
# missing producer degrades instead of raising. relay is stubbed through sys.modules
# so these hold whether or not src/relay.py exists in the tree yet.
empty_ev = ledger(**{"L-operator-local.jsonl": []})


def with_relay(lines):
    """Render once with waiting_lines() stubbed to return `lines`.

    `lines=None` installs the None sentinel, which makes `from relay import ...`
    raise ImportError whether or not src/relay.py is on disk — that is the whole
    point: the degrade check must not become a no-op the day relay-queries merges.
    """
    prev, had = sys.modules.get("relay"), "relay" in sys.modules
    if lines is None:
        sys.modules["relay"] = None
    else:
        m = types.ModuleType("relay")
        m.waiting_lines = lambda e, r: list(lines)
        sys.modules["relay"] = m
    try:
        return fold.render(*empty_ev)
    finally:
        sys.modules.pop("relay", None)
        if had:
            sys.modules["relay"] = prev


stub = ["PLANNER WAITING ON", "  L-charter-0007 · after: L-charter-0002 not landed"]
wb = with_relay(stub)
assert wb.startswith("# board · "), "the block goes UNDER the title, never above it"
assert 0 < wb.index("\n".join(stub)) < wb.index("## NEEDS YOU"), \
    "the waiting block renders between the title and NEEDS YOU"
assert "## PLANNER WAITING ON" not in wb, \
    "fold.py synthesizes no header of its own around a pass-through block"
assert "PLANNER WAITING ON" not in wb[wb.index("## SPEND"):wb.index("## HEALTH")], \
    "and never in the SPEND/HEALTH gap, which spend_block() slices"
assert len(sections(wb)) == 16, "the block is not a section of its own (16 = ten + SPEND + LIVE PANES + OWED DUE/UNSERVED/INBOUND/NOTES, L-spec-0196/0244)"

# R6: a dry queue is CONTENT, not a reason to omit the slot.
assert "PLANNER WAITING ON: no open charters" in with_relay(
    ["PLANNER WAITING ON: no open charters"]), "an empty queue still renders its line"

# AC4: pure pass-through — order kept, duplicates kept, nothing dropped or re-wrapped.
dupes = ["PLANNER WAITING ON", "  L-charter-0009 · conflict building",
         "  L-charter-0011 · footprint intersects L-spec-0031",
         "  L-charter-0011 · footprint intersects L-spec-0031",
         "  L-charter-0003 · count throttle: L-charter-0002 not landed"]
assert "\n".join(dupes) in with_relay(dupes), \
    "fold.py does not reorder, dedupe or truncate what relay handed it"

# L-adr-0033: no relay yet -> one line on HEALTH, and the run does NOT raise.
gone = with_relay(None)
degrade = "PLANNER WAITING ON: unavailable — relay-queries not merged (L-adr-0033)"
assert gone.count(degrade) == 1 and gone.index(degrade) > gone.index("## HEALTH"), \
    "a missing producer says so once, under HEALTH"
assert gone.count("PLANNER WAITING ON") == 1, \
    "and renders no block content it does not have"
assert len(sections(gone)) == 16, "the degrade line is a HEALTH row, not a section"

# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0021 · the-fold-and-the-board
# ══════════════════════════════════════════════════════════════════════════════

# ── R3/AC1 · append() refuses a malformed escalation-blocking, AT THE DOOR ─────
# Through the real CLI, so the EXIT CODE and the stderr the operator sees are what
# is checked, not an in-process exception. Nothing partially written: the ledger
# file must not exist at all after the refusal.
import subprocess                                                     # noqa: E402
ESC_LEDGER = "L-executor-esc.jsonl"


def run_append(*argv, ledger_file=ESC_LEDGER):
    env = {**os.environ, "DOIT_ROOT": str(TMP), "DOIT_LEDGER_FILE": ledger_file}
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "fold.py"),
                           "append", *argv], capture_output=True, text=True, env=env)


def esc_lines(name=ESC_LEDGER):
    f = TMP / "events" / name
    return [l for l in f.read_text().splitlines() if l.strip()] if f.is_file() else []


ledger(**{"L-operator-local.jsonl": []})
# (a) bare why= — the exact shape agents/executor.md's question lane row writes today
r = run_append("escalation-blocking", "L-test-0001", "why=x")
assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
out = r.stdout + r.stderr
assert "default" in out and "deadline" in out and "revert" in out and "irreversible" in out, \
    "the refusal NAMES the missing requirement — the documented Executor/Planner command " \
    "is one field short until the-contracts lands, and a bare non-zero exit tells nobody why"
assert esc_lines() == [], "a refused append writes NOTHING, not even a partial line"

# (b) the reversible shape: all three together
r = run_append("escalation-blocking", "L-test-0001", "why=x", "default=defer",
               f"deadline={stamp(-1)}", "revert=revert the merge commit")
assert r.returncode == 0, (r.returncode, r.stderr)
assert len(esc_lines()) == 1, esc_lines()

# (c) the irreversible shape: no default is possible, and the act is named
r = run_append("escalation-blocking", "L-test-0001", "why=x",
               "irreversible=the deploy is already live")
assert r.returncode == 0, (r.returncode, r.stderr)
assert len(esc_lines()) == 2, esc_lines()

# ...and each of the three, alone, is NOT enough — the triple is a conjunction
for partial in (("default=defer",), (f"deadline={stamp(-1)}",), ("revert=undo it",),
                ("default=defer", f"deadline={stamp(-1)}"),
                ("default=defer", "revert=undo it")):
    r = run_append("escalation-blocking", "L-test-0001", "why=x", *partial)
    assert r.returncode != 0, (partial, r.returncode)
assert len(esc_lines()) == 2, "no partial shape landed a line"
# ...and a blank value is not a present field: typing the field name is not the rule
r = run_append("escalation-blocking", "L-test-0001", "why=x", "default=", "deadline=", "revert=")
assert r.returncode != 0, "default= with nothing after it is absent, not present"

# (d) the refusal binds escalation-blocking ONLY. A question's mandatory four are
# asks/blocks/default/deadline; its revert lives on the answering decision, so a
# question with no revert must still land.
r = run_append("question", "L-test-0002", "asks=inline or deferred?", "blocks=AC3",
               "default=defer", f"deadline={stamp(-1)}", ledger_file="L-builder-q.jsonl")
assert r.returncode == 0, (r.returncode, r.stderr)
assert len(esc_lines("L-builder-q.jsonl")) == 1, "append() gains no new rule for `question`"

# ── R3/AC2 · the ledger's OWN malformed escalations, which append() never saw ──
# dispatch.py and tick.py write escalation-blocking through dispatch.emit(), not
# through append(), and the ledger is append-only — so counting is the only
# enforcement that reaches them.
bad_esc = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(2), "type": "escalation-blocking", "subject": S,
     "why": "the gate wants a --writes grant"}]})
assert len(fold.malformed_escalations(bad_esc[0])) == 1, fold.malformed_escalations(bad_esc[0])
assert fold.malformed_escalations(bad_esc[0])[0]["subject"] == S
bad_board = fold.render(*bad_esc)
assert "malformed escalations: 1" in bad_board, bad_board
assert f"(last: {S} ·" in bad_board, "HEALTH names the subject, not just a number"
# ...and a compliant ledger says ZERO rather than going quiet: a count nobody
# renders at zero cannot tell a holding refusal from an unmeasured one.
good_esc = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(2), "type": "escalation-blocking", "subject": S, "why": "w",
     "default": "defer", "deadline": stamp(1), "revert": "revert the merge"}]})
assert "malformed escalations: 0" in fold.render(*good_esc)
assert fold.malformed_escalations(good_esc[0]) == []

# ── R3/AC3 · NEEDS YOU rows carry what it takes to DECIDE ─────────────────────
S_R, S_I, S_Q, S_M = "L-spec-0201", "L-spec-0202", "L-spec-0203", "L-spec-0204"
needs = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(2), "type": "escalation-blocking", "subject": S_R, "why": "the gate wants a grant",
     "default": "take the narrow grant", "deadline": stamp(1), "revert": "revert the merge commit"},
    {"ts": stamp(3), "type": "escalation-blocking", "subject": S_I, "why": "the deploy already went",
     "irreversible": "the droplet is serving the new sha"},
    {"ts": stamp(4), "type": "escalation-blocking", "subject": S_M, "why": "bare, pre-R3"}],
    "L-builder-01.jsonl": [
    {"ts": stamp(2), "type": "question", "subject": S_Q, "asks": "refunds inline or deferred?",
     "blocks": ["AC3"], "default": "defer", "deadline": stamp(1)}]})
rows = {r["subject"]: r for r in fold.open_questions(needs[0])}
assert set(rows) == {S_R, S_I, S_Q, S_M}, rows
assert rows[S_Q]["revert"] == "revert n/a — question", rows[S_Q]
assert rows[S_I]["irreversible"] == "the droplet is serving the new sha"
assert rows[S_M]["malformed"] and not rows[S_R]["malformed"] and not rows[S_I]["malformed"]
nboard = fold.render(*needs)
needs_block = nboard.split("## NEEDS YOU")[1].split("## BLOCKED")[0]
line = {s: next(l for l in needs_block.splitlines() if s in l) for s in (S_R, S_I, S_Q, S_M)}
# the reversible escalation: default, deadline, revert, age — all four
assert "default take the narrow grant" in line[S_R] and "revert revert the merge commit" in line[S_R], line[S_R]
assert stamp(1) in line[S_R] and line[S_R].rstrip().endswith("d"), line[S_R]
# the irreversible one: the named act stands where the revert would be, and the
# two absent fields say WHY they are absent rather than rendering empty
assert "irreversible: the droplet is serving the new sha" in line[S_I], line[S_I]
assert line[S_I].count("n/a — irreversible act named") == 2, line[S_I]
# the question: asks, default, deadline, age, and the literal — never an invented revert
assert "refunds inline or deferred?" in line[S_Q] and "default defer" in line[S_Q], line[S_Q]
assert "revert n/a — question" in line[S_Q] and f"unanswered past {stamp(1)}" in line[S_Q], line[S_Q]
# the malformed one renders DIFFERENTLY — the reader must not have to notice an absence
assert "⚠ malformed" in line[S_M] and "⚠ malformed" not in line[S_R] + line[S_I] + line[S_Q], line[S_M]
assert "revert n/a — question" not in line[S_R] + line[S_I], "a question's literal is a question's"
# ...and an escalation carrying BOTH keeps both — the revert the operator would
# run and the thing that cannot be undone are different facts, and neither is dropped
S_B = "L-spec-0205"
both = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(1), "type": "escalation-blocking", "subject": S_B, "why": "partial rollout",
     "default": "hold", "deadline": stamp(1), "revert": "revert the code",
     "irreversible": "the migration already ran"}]})
bline = next(l for l in fold.render(*both).split("## NEEDS YOU")[1].split("## BLOCKED")[0].splitlines()
             if S_B in l)
assert "revert revert the code" in bline and "irreversible: the migration already ran" in bline, bline

# ── R3/AC4 · decide_overdue settles a reversible overdue question EXACTLY once ──
os.environ["DOIT_LEDGER_FILE"] = "L-executor-auto.jsonl"
auto_q = {"ts": stamp(2), "type": "question", "subject": S, "asks": "refunds inline or deferred?",
          "blocks": ["AC3"], "default": "defer", "deadline": stamp(1)}
stale_ev = ledger(**{"L-builder-01.jsonl": [auto_q]})[0]
first = fold.decide_overdue(stale_ev)
assert len(first) == 1, first
assert first[0]["type"] == "decision" and first[0]["subject"] == S, first
assert first[0]["ref"] == "L-builder-01.jsonl:1", first[0]["ref"]
assert "defer" in first[0]["why"], first[0]["why"]
after = fold.read_events()
assert len([e for e in after if e["type"] == "decision"]) == 1, "exactly one decision landed"
# ★ the buggy-caller case: fed the PRE-first-call snapshot, in which the question
# is still open. It must re-read before writing — the snapshot is not the ledger.
assert fold.decide_overdue(stale_ev) == [], "a second call on a stale snapshot appends nothing"
assert len([e for e in fold.read_events() if e["type"] == "decision"]) == 1, \
    "re-read before the write is what makes two back-to-back calls idempotent"
# ...and an IRREVERSIBLE overdue question — no recorded default — is never settled
# automatically. That distinction is the entire reason the default is mandatory.
nodef = ledger(**{"L-builder-01.jsonl": [{k: v for k, v in auto_q.items() if k != "default"}]})[0]
assert fold.decide_overdue(nodef) == [], "no default: it stays the operator's"
assert [e for e in fold.read_events() if e["type"] == "decision"] == []

# ── R3/AC5 · and it renders through the EXISTING DECIDED WITHOUT YOU block ────
auto = ledger(**{"L-builder-01.jsonl": [auto_q]})
assert S in fold.render(*auto).split("## NEEDS YOU")[1].split("## BLOCKED")[0], \
    "before: the question is the operator's"
fold.decide_overdue(auto[0])
settled_ev = fold.read_events()
sboard = fold.render(settled_ev, *fold.fold(settled_ev))
decided = sboard.split("## DECIDED WITHOUT YOU")[1].split("## SPEND")[0]
assert S in decided and "defer" in decided, decided
assert "revert ⚠ none" in decided, \
    "an auto-settled decision has no operator-authored undo, and the EXISTING fallback says so"
assert S not in sboard.split("## NEEDS YOU")[1].split("## BLOCKED")[0], \
    "after: the newest event on the subject is a decision, so it leaves NEEDS YOU"
os.environ["DOIT_LEDGER_FILE"] = "L-operator-01.jsonl"

# ── R6/AC6 · free_standing — accepted with no charter, and at what tier ───────
FS = "L-spec-0301"
free = ledger(**{
    "L-builder-01.jsonl": built + [                    # `built` carries charter C
        {"ts": stamp(3), "type": "spec-written", "subject": FS},          # no charter, anywhere
        {"ts": stamp(2), "type": "build-started", "subject": FS},
        {"ts": stamp(2), "type": "build-done", "subject": FS}],
    "L-grader-01.jsonl": graded + [{"ts": stamp(1), "type": "verdict", "subject": FS, "confirmed": True}],
    "L-reviewer-01.jsonl": reviewed + [{"ts": stamp(1), "type": "review", "subject": FS,
                                        "depth": "gates-only"}],
    "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "shipped", "subject": FS}]})
assert free[1][FS]["state"] == "accepted" and free[1][S]["state"] == "accepted"
assert fold.free_standing(free[0]) == [{"id": FS, "tier": "gates-only"}], fold.free_standing(free[0])
fboard2 = fold.render(*free)
assert "free-standing accepted specs: 1" in fboard2 and f"{FS} at gates-only" in fboard2, fboard2
# a chartered accepted spec is not free-standing, and an unaccepted charter-less
# one is not either — "accepted" is the whole promise R6 makes observable.
notyet = ledger(**{"L-builder-01.jsonl": [{"ts": stamp(3), "type": "spec-written", "subject": FS}]})
assert fold.free_standing(notyet[0]) == [], "written is not accepted"
assert "free-standing accepted specs: 0" in fold.render(*notyet), "zero is a measurement"

# ── R11/AC7 · closable() IS fold()'s predicate, all five conjuncts ────────────
# (i) every charter-state fixture above already ran, unmodified. (ii) on each of
# them, `L2-complete` iff the five-conjunct AND — the polarity pinned once, in
# fold.l2_complete, so neither a builder nor a consumer picks it.
brief_answer = [{"ts": stamp(0), "type": "brief-answered", "subject": C,
                 "ref": "L-grader-02.jsonl:1", "spec": "L-spec-0143"}]
# ★ K is read from DOIT_K at import, and the operator's env.sh sets it to 1 on
# this box — so a test that ASSUMED the 0 default would pass or fail depending on
# whose shell ran it. Pinned by assignment, restored after (Assumptions: K stays
# fold.K, never a parameter).
_K = fold.K
fold.K = 0
for name, files in (("L2", done),
                    ("L1", L1),
                    ("brief open", {**L1, "L-grader-02.jsonl": inscope}),
                    ("brief answered", {**L1, "L-grader-02.jsonl": inscope,
                                        "L-executor-02.jsonl": brief_answer}),
                    ("adjacent brief", {**L1, "L-grader-02.jsonl": adjacent}),
                    ("re-review reopened", {**done, "L-charter-reviewer-01.jsonl": [
                        {"ts": stamp(2), "type": "charter-review-complete", "subject": C},
                        {"ts": stamp(0), "type": "charter-review-not-complete", "subject": C}]}),
                    ("void allocation", {**done, **ghost}),
                    ("closed-shipped credit", {
                        "L-builder-01.jsonl": built,
                        "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "sweep-fixpoint",
                                                           "subject": C}],
                        "L-operator-01.jsonl": [{"ts": stamp(0), "type": "spec-closed", "subject": S,
                                                 "charter": C, "why": "post-merge observation only"}],
                        "L-charter-reviewer-01.jsonl": [{"ts": stamp(0),
                                                         "type": "charter-review-complete",
                                                         "subject": C}]}),
                    ("retracted", {**done, "L-operator-02.jsonl": [
                        {"ts": stamp(0), "type": "charter-retracted", "subject": C}]})):
    fx = ledger(**files)
    cl = fold.closable(fx[0], C)
    # .keys(), not set(cl): the positional projection below owns bare iteration.
    assert set(cl.keys()) == {"all_accepted", "sweep_derived", "owed_within_k",
                              "no_open_briefs", "review_owed"}, (name, cl)
    if fx[2][C]["state"] != "retracted":             # retraction pre-empts the predicate
        assert (fx[2][C]["state"] == "L2-complete") == fold.l2_complete(cl), (name, fx[2][C]["state"], cl)
    # ★ L-adr-0044 reconciliation: the landed tick.lane() unpacks a 3-tuple and
    # ANDs it as `accepted and fixpoint and not review_owed`. That projection must
    # agree with l2_complete() on every fixture — and must LOSE no conjunct: K and
    # the open-brief ride with the fact each belongs to, exactly as
    # tick.closable_fallback folds them. Passing the charter DICT too, which is
    # what tick actually hands over.
    acc, fix, owed_rev = cl
    assert (acc, fix, owed_rev) == (cl["all_accepted"] and cl["owed_within_k"],
                                    cl["sweep_derived"] and cl["no_open_briefs"],
                                    cl["review_owed"]), (name, cl)
    assert (acc and fix and not owed_rev) == fold.l2_complete(cl), (name, cl)
    assert dict(fold.closable(fx[0], fx[2][C])) == dict(cl), \
        "a charter dict reads the same as its id — tick.lane() passes the dict"

# (iii) the `owed <= K` conjunct, which nothing exercised before: a charter whose
# specs are OTHERWISE all accepted, with the fixpoint, no open brief and a complete
# review, plus exactly one shipped-owed-evidence spec, at the default K = 0.
SO = "L-spec-0401"
owedk = ledger(**{
    "L-builder-01.jsonl": built + [{"ts": stamp(3), "type": "spec-written", "subject": SO, "charter": C},
                                   {"ts": stamp(2), "type": "build-started", "subject": SO}],
    "L-grader-01.jsonl": graded,
    "L-reviewer-01.jsonl": reviewed,
    "L-spec-writer-01.jsonl": [{"ts": stamp(0), "type": "owed-ac", "subject": SO,
                                "criterion": "AC7", "wake_at": stamp(-7)}],
    "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "shipped", "subject": SO},
                                      {"ts": stamp(0), "type": "sweep-fixpoint", "subject": C}],
    "L-charter-reviewer-01.jsonl": [{"ts": stamp(0), "type": "charter-review-complete", "subject": C}]})
assert owedk[1][SO]["state"] == "shipped-owed-evidence", owedk[1][SO]["state"]
assert owedk[1][S]["state"] == "accepted"
assert owedk[2][C]["state"] != "L2-complete", \
    "one owed spec at K=0 must hold the charter open — the conjunct nothing tested"
ck = fold.closable(owedk[0], C)
assert ck["owed_within_k"] is False, ck
assert (ck["all_accepted"], ck["sweep_derived"], ck["no_open_briefs"], ck["review_owed"]) \
    == (True, True, True, False), ck
assert not fold.l2_complete(ck)
# ★ and the 3-value projection does NOT lose the K conjunct: a consumer that only
# unpacks (accepted, fixpoint, review_owed) must still see this charter as open,
# or the lane drops it before its owed evidence lands (tick.closable_fallback's
# own warning, which this reconciliation must not reintroduce).
assert tuple(ck) == (False, True, False), tuple(ck)
assert not (tuple(ck)[0] and tuple(ck)[1] and not tuple(ck)[2]), ck
# ...and raising K to 1 closes it, through the same one predicate
fold.K = 1
try:
    raised = ledger(**{
        "L-builder-01.jsonl": built + [{"ts": stamp(3), "type": "spec-written", "subject": SO, "charter": C},
                                       {"ts": stamp(2), "type": "build-started", "subject": SO}],
        "L-grader-01.jsonl": graded, "L-reviewer-01.jsonl": reviewed,
        "L-spec-writer-01.jsonl": [{"ts": stamp(0), "type": "owed-ac", "subject": SO,
                                    "criterion": "AC7", "wake_at": stamp(-7)}],
        "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "shipped", "subject": SO},
                                          {"ts": stamp(0), "type": "sweep-fixpoint", "subject": C}],
        "L-charter-reviewer-01.jsonl": [{"ts": stamp(0), "type": "charter-review-complete",
                                         "subject": C}]})
    assert raised[2][C]["state"] == "L2-complete" and fold.l2_complete(fold.closable(raised[0], C))
finally:
    fold.K = 0
assert fold.K == 0, "K is pinned by assignment for this block, not read from the shell"
# ...and a charter the ledger has never named answers honestly instead of raising —
# "is this closable yet" is asked BEFORE the sweep, not only after it.
unknown_c = fold.closable(owedk[0], "L-charter-9999")
assert unknown_c["all_accepted"] is True and unknown_c["sweep_derived"] is False \
    and unknown_c["review_owed"] is True and not fold.l2_complete(unknown_c), unknown_c
fold.K = _K

# ── L-spec-0125 R11/AC1-AC4 · charter_review_owed(charter_evs) — the sole gate ─
# D90: only a `thinker`/`operator`-named file lands `charter-filed`, and only a
# `charter-reviewer`-named file lands `charter-review-complete` — any other
# filename is silently dropped, and the fixture would pass for the wrong reason
# (no events reaching charter_evs, not the intended case).
RC = "L-charter-1101"
_review_complete = [{"ts": stamp(0), "type": "charter-review-complete", "subject": RC}]


def _owed(covers=None, filed=True, reviewed=False):
    files = {}
    if filed:
        files["L-thinker-0001.jsonl"] = [{"ts": stamp(1), "type": "charter-filed",
                                          "subject": RC, "covers": covers}]
    if reviewed:
        files["L-charter-reviewer-0001.jsonl"] = _review_complete
    fx = ledger(**files)
    return fold.charter_review_owed(fx[4].get(RC, []))


# AC1 — the explicit "none" string short-circuits False, review landed or not.
assert _owed("none", reviewed=False) is False, "AC1: Covers: none never owes a review"
assert _owed("none", reviewed=True) is False, "AC1: still False once a review lands too"

# AC2 — case-insensitive "null" and whitespace-padded " NONE " short-circuit the same way.
for spelling in ("null", "NULL", "Null", " NONE "):
    assert _owed(spelling, reviewed=False) is False, (spelling, "AC2")

# AC3 — a non-empty id list is the UNCHANGED existing rule: True with no review,
# False once one lands.
assert _owed("G3 G4 G5", reviewed=False) is True, "AC3: an id list still owes a review"
assert _owed("G3 G4 G5", reviewed=True) is False, "AC3: ...until one lands, unchanged"

# AC4 — undetermined is never clean: a raw JSON `null` covers value and an absent
# `charter-filed` event both behave exactly like AC3's non-empty list, never like
# the explicit none/null spelling.
assert _owed(None, filed=True, reviewed=False) is True, "AC4a: raw JSON null falls through"
assert _owed(None, filed=True, reviewed=True) is False, "AC4a: ...until a review lands"
assert _owed(filed=False, reviewed=False) is True, "AC4b: no charter-filed event -> unchanged rule"
assert _owed(filed=False, reviewed=True) is False, "AC4b: ...until a review lands"

# The same fixture shape, run through the real fold.closable()/l2_complete(), so
# R11's fix is proven at the conjunct it actually gates, not only at the bare function.
none_fx = ledger(**{"L-thinker-0001.jsonl": [{"ts": stamp(1), "type": "charter-filed",
                                              "subject": RC, "covers": "none"}]})
none_cl = fold.closable(none_fx[0], RC)
assert none_cl["review_owed"] is False, none_cl
_none_specs = [s for s in none_fx[1].values() if s["charter"] == RC]
assert _none_specs == [], "no specs on this fixture charter, by construction"

# ── R4/R8/R13/R15/AC8 · the four new EMITS entries, exactly their actors ──────
# R10/L-spec-0192 widens spec-carried to admit the executor's own `doit carry`
# write — see the dedicated EMITS-widening block further down.
assert fold.EMITS["spec-carried"] == {"operator", "executor"}
assert fold.EMITS["conflict-rework"] == {"executor"}
assert fold.EMITS["message-sent"] == {"planner", "executor", "thinker"}
assert fold.EMITS["repo-edit"] == {"executor"}
for etype, good_file, bad_file in (("spec-carried", "L-operator-01.jsonl", "L-builder-01.jsonl"),
                                   ("conflict-rework", "L-executor-01.jsonl", "L-builder-01.jsonl"),
                                   ("message-sent", "L-thinker-01.jsonl", "L-builder-01.jsonl"),
                                   ("repo-edit", "L-executor-01.jsonl", "L-planner-01.jsonl")):
    body = {"ts": stamp(0), "type": etype, "subject": S}
    okfx = ledger(**{good_file: [body]})
    assert okfx[3] == [] and any(e["type"] == etype for e in okfx[4][S]), (etype, okfx[3])
    badfx = ledger(**{bad_file: [body]})
    assert len(badfx[3]) == 1 and badfx[3][0]["type"] == etype, (etype, badfx[3])
    assert S not in badfx[4], "an unauthorized emit does not reach the fold at all"
    assert "unauthorized events recorded and ignored: 1" in fold.render(*badfx), etype
# ...and message-sent's other two panes are authorized, while a dispatched
# sub-agent role is not — a sub-agent returns to its seat, it does not address a pane
for pane in ("L-planner-01.jsonl", "L-executor-01.jsonl"):
    assert ledger(**{pane: [{"ts": stamp(0), "type": "message-sent", "subject": S}]})[3] == [], pane
assert len(ledger(**{"L-grader-01.jsonl": [{"ts": stamp(0), "type": "message-sent",
                                            "subject": S}]})[3]) == 1

# ── R15/AC9 · the repo-edit count is honest at zero AND at nonzero ────────────
none_edits = ledger(**{"L-executor-01.jsonl": shipped})
assert "repo-edit events: 0" in fold.render(*none_edits), \
    "a session that should read zero must SAY zero — silence is not a measurement"
two_edits = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(1), "type": "repo-edit", "subject": "L-executor-0007.jsonl",
     "path": "src/fold.py"},
    {"ts": stamp(0), "type": "repo-edit", "subject": "L-executor-0007.jsonl",
     "path": "src/tick.py"}]})
eboard2 = fold.render(*two_edits)
assert "repo-edit events: 2" in eboard2 and "src/tick.py" in eboard2, eboard2

# ── R13/R14/AC5/AC10 · LIVE PANES and the unrecorded-message count DEGRADE ────
# Both legs synthetic. Never "src/panes.py happens to be missing on disk" — that
# is a property of merge order, not of this code, and it would stop testing the
# absent leg the moment pane-identity merges.
# `types` is imported at the top of this file (the with_relay stub needs it too).
pane_fx = ledger(**{"L-executor-01.jsonl": shipped})
_saved = sys.modules.get("panes", "‹absent›")
try:
    sys.modules["panes"] = None          # python raises ImportError on a None entry
    absent = fold.render(*pane_fx)       # must not raise
    assert "## LIVE PANES (1)" in absent, absent
    live_block = absent.split("## LIVE PANES")[1].split("## HEALTH")[0]
    assert "unavailable" in live_block and "panes" in live_block, live_block
    assert "unrecorded messages: unavailable" in absent, absent

    # AC5/R5/L-spec-0196: panes.live_panes()'s NEW Seams-table shape — exactly
    # `{name, role, cwd, ledger, age_s, busy}`, one name per fact, no more
    # two-key reconciliation. All six values render; none of the OLD shape's
    # key names (contract/status/ledger_file/last_event_age_days/name_source)
    # ever appears anywhere in the block.
    stub = types.ModuleType("panes")
    stub.live_panes = lambda sessions_dir, events: [
        {"name": "planner", "role": "planner", "cwd": "/home/x/do-it-v2",
         "ledger": "L-planner-0014.jsonl", "age_s": 259, "busy": True}]
    stub.unrecorded_messages = lambda events: 3
    sys.modules["panes"] = stub
    present = fold.render(*pane_fx)
    live_block = present.split("## LIVE PANES")[1].split("## HEALTH")[0]
    assert "## LIVE PANES (1)" in present, present
    for cell in ("planner", "/home/x/do-it-v2", "L-planner-0014.jsonl", "259", "True"):
        assert cell in live_block, (cell, live_block)
    assert "unavailable" not in live_block, live_block
    for old_key in ("contract", "status", "ledger_file", "last_event_age_days", "name_source"):
        assert old_key not in live_block, (old_key, live_block)
    assert "unrecorded messages: 3" in present, present

    # a key present but None is not a value: `None` must never read as a cell
    stub.live_panes = lambda sessions_dir, events: [
        {"name": "p", "role": None, "cwd": "/w", "ledger": "l.jsonl",
         "age_s": None, "busy": False}]
    nones = fold.render(*pane_fx).split("## LIVE PANES")[1].split("## HEALTH")[0]
    assert "None" not in nones and nones.count("?") == 2, nones

    # a key-name mismatch at merge (e.g. the OLD shape, still landed as of this
    # spec's write time) is a visible `?` cell, never a KeyError that takes
    # every other section of the board down with it
    stub.live_panes = lambda sessions_dir, events: [{"name": "planner"}]
    mismatch = fold.render(*pane_fx)
    assert mismatch.split("## LIVE PANES")[1].split("## HEALTH")[0].count("?") >= 5, mismatch

    # and a sibling that raises is stated too, not propagated
    def _boom(*a, **k):
        raise RuntimeError("seam changed")
    stub.live_panes, stub.unrecorded_messages = _boom, _boom
    raised_b = fold.render(*pane_fx)
    assert "unavailable — panes.live_panes raised RuntimeError" in raised_b, raised_b
    assert "unavailable — panes.unrecorded_messages raised RuntimeError" in raised_b, raised_b
finally:
    if _saved == "‹absent›":
        sys.modules.pop("panes", None)
    else:
        sys.modules["panes"] = _saved
assert SESSIONS_DEFAULT.name == "sessions", SESSIONS_DEFAULT

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0192 · fold-states-owed-due-and-killed (L-charter-0028)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · a due, unmet owed-ac -> shipped-owed-due, not evidence or plain shipped
due_ac1 = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC1", "wake_at": stamp(7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "shipped-owed-due", sp[S]["state"]

# ── AC2 · same fixture, wake_at 7 days in the FUTURE -> stays shipped-owed-evidence (D25)
future_ac1 = [{**due_ac1[0], "wake_at": stamp(-7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": future_ac1})
assert sp[S]["state"] == "shipped-owed-evidence", sp[S]["state"]

# ── AC3 · a due criterion plus a co-existing NOT-yet-due one -> due wins
due_plus_pending = due_ac1 + [{"ts": stamp(0), "type": "owed-ac", "subject": S,
                                "criterion": "AC2", "wake_at": stamp(-7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": due_plus_pending})
assert sp[S]["state"] == "shipped-owed-due", sp[S]["state"]

# ── AC4 · owed-met after the due owed-ac discharges it -> accepted
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed,
                           "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "owed-met",
                                                              "subject": S, "criterion": "AC1",
                                                              "evidence": "checked by hand"}],
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "accepted", sp[S]["state"]

# ── AC5 · a standing rejected-criterion routes away from shipped-owed-due entirely
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed,
                           "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "rejected-criterion",
                                                              "subject": S, "criterion": "AC1"}],
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "shipped", sp[S]["state"]

# ── AC8 · owed_due() — one row per due-and-unmet criterion, across every spec
S2 = "L-spec-0143"
built_s2 = [{**e, "subject": S2} for e in built]
graded_s2 = [{**e, "subject": S2} for e in graded]
reviewed_s2 = [{**e, "subject": S2} for e in reviewed]
shipped_s2 = [{**e, "subject": S2} for e in shipped]
due_ac1_s2 = [{"ts": stamp(0), "type": "owed-ac", "subject": S2, "criterion": "AC1", "wake_at": stamp(7)}]
pending_ac2_s2 = [{"ts": stamp(0), "type": "owed-ac", "subject": S2, "criterion": "AC2", "wake_at": stamp(-7)}]
_, sp8, _, _, _ = ledger(**{
    "L-builder-01.jsonl": built + built_s2, "L-grader-01.jsonl": graded + graded_s2,
    "L-reviewer-01.jsonl": reviewed + reviewed_s2, "L-executor-01.jsonl": shipped + shipped_s2,
    "L-spec-writer-01.jsonl": due_ac1 + due_ac1_s2 + pending_ac2_s2})
rows = fold.owed_due(sp8)
assert {(r["spec"], r["criterion"]) for r in rows} == {(S, "AC1"), (S2, "AC1")}, rows
assert all(r["days_overdue"] > 0 for r in rows), rows
assert all(set(r) == {"spec", "criterion", "due_at", "days_overdue", "src"} for r in rows), rows

# ── AC9 · killed is terminal — survives a LATER stage event on the same subject
S3 = "L-spec-0144"
_, sp9, _, _, _ = ledger(**{
    "L-spec-writer-01.jsonl": [{"ts": stamp(3), "type": "spec-written", "subject": S3},
                              {"ts": stamp(2), "type": "spec-killed", "subject": S3, "check": 2}],
    "L-builder-01.jsonl": [{"ts": stamp(1), "type": "build-started", "subject": S3}]})
assert sp9[S3]["state"] == "killed", sp9[S3]["state"]

# ── AC15 · append() raises for each missing required field, writes nothing;
# an actor/type mismatch ALONE does not raise (the direct pair to AC14's 2nd case)
os.environ["DOIT_LEDGER_FILE"] = "L-ac15-test.jsonl"
AC15_LEDGER = fold.EVENTS / "L-ac15-test.jsonl"
if AC15_LEDGER.is_file():
    AC15_LEDGER.unlink()
for argv, missing_word in (
    (["escalation-blocking", "L-spec-9999"], "default"),
    (["owed-ac", "L-spec-9999", "wake_at=2026-10-01T00:00:00Z"], "criterion"),
    (["spec-carried", "L-spec-9999", "source=1454", "tier=1"], "audited_at"),
):
    before15 = AC15_LEDGER.read_text() if AC15_LEDGER.is_file() else ""
    try:
        fold.append(argv)
        raise AssertionError(f"{argv[0]} with a missing field must raise SystemExit")
    except SystemExit as e:
        assert missing_word in str(e.code), (argv, e.code)
    after15 = AC15_LEDGER.read_text() if AC15_LEDGER.is_file() else ""
    assert after15 == before15, "nothing is written on a refused append"
# the fourth case: an actor/type mismatch alone (verdict from a "random" actor,
# under DOIT_LEDGER_FILE=L-random-local.jsonl) is NOT refused — the direct pair
# to AC14's second sub-case.
os.environ["DOIT_LEDGER_FILE"] = "L-random-local.jsonl"
RANDOM_LEDGER = fold.EVENTS / "L-random-local.jsonl"
if RANDOM_LEDGER.is_file():
    RANDOM_LEDGER.unlink()
fold.append(["verdict", "L-spec-9999", "confirmed=true"])
assert RANDOM_LEDGER.is_file() and len(RANDOM_LEDGER.read_text().splitlines()) == 1, \
    "an actor/type mismatch alone does not refuse the write"
os.environ["DOIT_LEDGER_FILE"] = "L-operator-01.jsonl"

# ── AC16 · check_append() checks BOTH actor membership and required fields —
# only append()/emit()'s OWN use of it (AC14/AC15) is field-only.
sc_ev = {"type": "spec-carried", "subject": "L-spec-1", "source": "1", "tier": "1",
         "audited_at": "2026-09-22T00:00:00Z"}
assert fold.check_append(sc_ev, "executor") is None, fold.check_append(sc_ev, "executor")
r16 = fold.check_append(sc_ev, "builder")
assert r16 is not None and "builder" in r16, r16
# a ledger fixture with a spec-carried event whose FILE actor is executor lands
# in by_subject, not ignored — a regression against today's {"operator"}-only.
_, _, _, ig16, by16 = ledger(**{"L-executor-01.jsonl": [
    {"ts": stamp(0), "type": "spec-carried", "subject": S, "source": "1", "tier": "1",
     "audited_at": stamp(0)}]})
assert ig16 == [] and any(e["type"] == "spec-carried" for e in by16[S]), (ig16, by16.get(S))

# ── AC17 · check_append() on owed-ac: executor passes, builder refused, a
# spec-writer with NO criterion is refused naming the field
oa_ev = {"type": "owed-ac", "criterion": "AC1", "wake_at": "2026-10-01T00:00:00Z"}
assert fold.check_append(oa_ev, "executor") is None, fold.check_append(oa_ev, "executor")
assert fold.check_append(oa_ev, "builder") is not None
oa_missing = {"type": "owed-ac", "wake_at": "2026-10-01T00:00:00Z"}
r17 = fold.check_append(oa_missing, "spec-writer")
assert r17 is not None and "criterion" in r17, r17

# ── AC18 · the three new EMITS entries, exactly, plus restore-verified's widening
# (L-spec-0242/AC5, SD1/SD15: the bare `inbound-registered` equality widens to
# `intake`, plus intake's own seven further entries — exactly these eight,
# nothing else changed.)
assert fold.EMITS["inbound-registered"] == {"operator", "thinker", "intake"}, fold.EMITS["inbound-registered"]
assert fold.EMITS["inbound-awaiting"] == {"intake"}, fold.EMITS["inbound-awaiting"]
assert fold.EMITS["inbound-awaiting-gone"] == {"intake"}, fold.EMITS["inbound-awaiting-gone"]
assert fold.EMITS["inbound-pr-commented"] == {"intake"}, fold.EMITS["inbound-pr-commented"]
assert fold.EMITS["inbound-closed"] == {"intake", "operator"}, fold.EMITS["inbound-closed"]
assert fold.EMITS["inbound-note-listed"] == {"intake"}, fold.EMITS["inbound-note-listed"]
assert fold.EMITS["inbound-note-gone"] == {"intake"}, fold.EMITS["inbound-note-gone"]
assert fold.EMITS["note-answered"] == {"thinker", "operator"}, fold.EMITS["note-answered"]
assert fold.check_append({"type": "inbound-registered", "subject": "u", "source": "u",
                         "project": "albert-scott"}, "intake") is None, \
    "AC5: the intake actor may now emit inbound-registered"
assert fold.EMITS["carry-failed"] == {"executor", "operator"}
assert fold.EMITS["backup-failed"] == {"backup"}
assert {"operator", "executor"} <= fold.EMITS["restore-verified"] and "drill" in fold.EMITS["restore-verified"]
_, _, _, ig18, by18 = ledger(**{"L-operator-01.jsonl": [
    {"ts": stamp(0), "type": "restore-verified", "subject": S}]})
assert ig18 == [] and any(e["type"] == "restore-verified" for e in by18[S]), (ig18, by18.get(S))

# ── AC19 · test_fold.py's PRE-EXISTING ignored-owed-ac fixture (lines ~157-167,
# the `owed`/D25 block above) needed no edit at all — both its assertions already
# ran, unchanged, earlier in this file, and this file already exited 0 to reach
# here. Re-asserted directly as the regression: an executor-authored owed-ac
# with no criterion and no co-existing spec-writer declaration is STILL ignored,
# now that EMITS["owed-ac"] admits the executor — the re-dating gate finds no
# matching prior declaration and drops it exactly as before.
owed19 = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "wake_at": stamp(-7)}]
_, sp19, _, ig19, _ = ledger(**{"L-builder-01.jsonl": built, "L-executor-01.jsonl": shipped + owed19})
assert sp19[S]["state"] == "shipped" and len(ig19) == 1, (sp19[S]["state"], ig19)

# ── AC20 · the re-date governs due-ness; re-dating a DIFFERENT, undeclared
# criterion is dropped exactly like any other unauthorized emit
S4 = "L-spec-0145"
sw_ac7 = [{"ts": stamp(3), "type": "spec-written", "subject": S4, "charter": C},
          {"ts": stamp(2), "type": "owed-ac", "subject": S4, "criterion": "AC7", "wake_at": stamp(-7)}]
built4 = [{"ts": stamp(2), "type": "build-started", "subject": S4},
          {"ts": stamp(2), "type": "build-done", "subject": S4}]
shipped4 = [{"ts": stamp(0), "type": "shipped", "subject": S4}]
redate_due = [{"ts": stamp(0), "type": "owed-ac", "subject": S4, "criterion": "AC7", "wake_at": stamp(7)}]
_, sp20, _, _, by20 = ledger(**{"L-spec-writer-01.jsonl": sw_ac7, "L-builder-01.jsonl": built4,
                                "L-executor-01.jsonl": shipped4 + redate_due})
assert any(e["type"] == "owed-ac" and e["actor"] == "executor" for e in by20[S4]), \
    "the re-date lands in by_subject, not ignored"
assert sp20[S4]["state"] == "shipped-owed-due", sp20[S4]["state"]

redate_other = [{"ts": stamp(0), "type": "owed-ac", "subject": S4, "criterion": "AC9", "wake_at": stamp(7)}]
_, sp20b, _, ig20b, _ = ledger(**{"L-spec-writer-01.jsonl": sw_ac7, "L-builder-01.jsonl": built4,
                                  "L-executor-01.jsonl": shipped4 + redate_other})
assert len(ig20b) == 1 and ig20b[0]["criterion"] == "AC9", ig20b
assert sp20b[S4]["state"] == "shipped-owed-evidence", sp20b[S4]["state"]

# ── AC21 · verdict_confirmed's owed_criteria is spec-writer/spec-auditor-authored
# ONLY — called directly against a hand-built evs bypassing fold()'s own filter
hand_evs = [
    {"type": "shipped", "subject": "L-spec-9099", "actor": "executor", "_src": "x:1", "ts": stamp(0)},
    {"type": "review", "subject": "L-spec-9099", "actor": "reviewer", "_src": "x:2", "ts": stamp(0),
     "depth": "gates-only"},
    {"type": "verdict", "subject": "L-spec-9099", "actor": "grader", "_src": "x:3", "ts": stamp(1),
     "cannot_assess": ["AC9"], "matches_intent": "yes", "card_ok": "yes"},
    {"type": "owed-ac", "subject": "L-spec-9099", "actor": "executor", "_src": "x:4", "ts": stamp(0),
     "criterion": "AC9", "wake_at": stamp(1)},
]
assert fold.spec_state(hand_evs, set()) != "accepted", fold.spec_state(hand_evs, set())

# ── AC12/AC22 · a shipped-owed-due subject renders under OWED DUE, exactly
# once, and NOT under AWAITING VERIFICATION or OWED EVIDENCE (R7/L-spec-0196:
# this unit narrows AWAITING VERIFICATION's pick(...) back to exclude
# `shipped-owed-due` now that OWED DUE — inserted between OWED EVIDENCE and
# CHARTER CLOSE — is its one home; L-spec-0192's own AC22 expected the
# interim "renders under AWAITING VERIFICATION" state pending this unit).
evs22 = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                  "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                  "L-spec-writer-01.jsonl": due_ac1})
board22 = fold.render(*evs22)
av_block = board22.split("## AWAITING VERIFICATION")[1].split("## OWED EVIDENCE")[0]
oe_block = board22.split("## OWED EVIDENCE")[1].split("## OWED DUE")[0]
od_block = board22.split("## OWED DUE")[1].split("## CHARTER CLOSE")[0]
assert S not in av_block, av_block
assert S not in oe_block, oe_block
assert S in od_block, od_block

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0196 · board-shows-each-signal-as-itself (L-charter-0028, wave 3)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · one due, unmet owed-ac (10d overdue) -> one OWED DUE row; empty stays visible
due10 = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC1", "wake_at": stamp(10)}]
ev1 = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
               "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
               "L-spec-writer-01.jsonl": due10})
board1 = fold.render(*ev1)
assert "## OWED DUE (1)" in board1, board1
od_block1 = board1.split("## OWED DUE")[1].split("## CHARTER CLOSE")[0]
assert S in od_block1 and "AC1" in od_block1 and "10d overdue" in od_block1, od_block1
assert due10[0]["wake_at"] in od_block1, od_block1
empty_board1 = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
assert "## OWED DUE (0)" in empty_board1, empty_board1

# ── AC2 · a 3d and a 9d overdue row: HEALTH names only the 9d row in the fault
# fragment; zero rows over 7 days renders no fault fragment at all
S2 = "L-spec-0143"
built2 = [{"ts": stamp(3), "type": "spec-written", "subject": S2, "charter": C},
          {"ts": stamp(2), "type": "build-started", "subject": S2},
          {"ts": stamp(2), "type": "build-done", "subject": S2}]
graded2 = [{"ts": stamp(1), "type": "verdict", "subject": S2, "confirmed": True}]
reviewed2 = [{"ts": stamp(1), "type": "review", "subject": S2, "depth": "gates-only"}]
shipped2 = [{"ts": stamp(0), "type": "shipped", "subject": S2}]
due3 = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "criterion": "AC1", "wake_at": stamp(3)}]
due9 = [{"ts": stamp(0), "type": "owed-ac", "subject": S2, "criterion": "AC2", "wake_at": stamp(9)}]
ev2 = ledger(**{"L-builder-01.jsonl": built + built2, "L-grader-01.jsonl": graded + graded2,
               "L-reviewer-01.jsonl": reviewed + reviewed2,
               "L-executor-01.jsonl": shipped + shipped2,
               "L-spec-writer-01.jsonl": due3 + due9})
board2 = fold.render(*ev2)
due_line2 = [l for l in board2.splitlines() if l.strip().startswith("due owed:")][0]
assert "due owed: 2" in due_line2 and "OVER 7 DAYS" in due_line2, due_line2
assert S2 in due_line2 and "AC2" in due_line2 and "9d" in due_line2, due_line2
assert S not in due_line2, due_line2

ev2b = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                 "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped,
                 "L-spec-writer-01.jsonl": due3})
board2b = fold.render(*ev2b)
due_line2b = [l for l in board2b.splitlines() if l.strip().startswith("due owed:")][0]
assert "due owed: 1" in due_line2b and "OVER 7 DAYS" not in due_line2b, due_line2b

# ── AC3 · carry.uncarried: a v4 row with no last_error, a pr row with one
_saved_carry = sys.modules.get("carry", "‹absent›")
try:
    carry_stub = types.ModuleType("carry")
    carry_stub.uncarried = lambda events: [
        {"source": "1471", "kind": "v4", "registered_at": "2026-09-01T00:00:00Z",
         "attempts": 0, "last_error": None},
        {"source": "https://github.com/x/y/pull/9", "kind": "pr",
         "registered_at": "2026-09-02T00:00:00Z", "attempts": 2, "last_error": "clone failed"},
    ]
    sys.modules["carry"] = carry_stub
    board3 = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    assert "## INBOUND (2)" in board3, board3
    inbound_block3 = board3.split("## INBOUND")[1].split("## SHIPPED SINCE YOU LOOKED")[0]
    assert "1471" in inbound_block3 and "v4" in inbound_block3, inbound_block3
    assert "clone failed" in inbound_block3, inbound_block3
    v4_row3 = [l for l in inbound_block3.splitlines() if "1471" in l][0]
    assert "None" not in v4_row3 and "last_error" not in v4_row3, v4_row3
finally:
    if _saved_carry == "‹absent›":
        sys.modules.pop("carry", None)
    else:
        sys.modules["carry"] = _saved_carry

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0244 · inbound-notes — the NOTES board (AC3, AC4, AC12), against the
# REAL src/notes.py (no stub: this is this unit's own module, not a sibling to
# degrade around).
# ══════════════════════════════════════════════════════════════════════════════
U_OPEN = "https://github.com/fredhead88/albert-scott-platform/pull/9001"
U_ANSWERED = "https://github.com/fredhead88/albert-scott-platform/pull/9002"
U_GONE = "https://github.com/fredhead88/albert-scott-platform/pull/9003"

board_notes3 = fold.render(*ledger(**{"L-intake-local.jsonl": [
    {"ts": stamp(0), "type": "inbound-note-listed", "subject": U_OPEN, "source": U_OPEN,
     "project": "albert-scott", "author_login": "opener", "title": "Open note",
     "opened_at": stamp(0)},
    {"ts": stamp(1), "type": "inbound-note-listed", "subject": U_ANSWERED, "source": U_ANSWERED,
     "project": "albert-scott", "author_login": "asker", "title": "Answered note",
     "opened_at": stamp(1)},
    {"ts": stamp(0), "type": "note-answered", "subject": U_ANSWERED, "source": U_ANSWERED,
     "project": "albert-scott", "response": "/tmp/r.txt"},
    {"ts": stamp(1), "type": "inbound-note-listed", "subject": U_GONE, "source": U_GONE,
     "project": "albert-scott", "author_login": "ghost", "title": "Gone note",
     "opened_at": stamp(1)},
    {"ts": stamp(0), "type": "inbound-note-gone", "subject": U_GONE, "source": U_GONE,
     "project": "albert-scott"},
]}))
notes_block3 = board_notes3.split("## NOTES")[1].split("## INBOUND")[0]
assert "pull/9001" in notes_block3, notes_block3
assert "pull/9002" not in notes_block3 and "pull/9003" not in notes_block3, notes_block3

# ── AC4 · ⚑ >48h at 49h, none at 47h
now_iso = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")
U_47 = "https://github.com/fredhead88/albert-scott-platform/pull/9004"
U_49 = "https://github.com/fredhead88/albert-scott-platform/pull/9005"
board_notes4 = fold.render(*ledger(**{"L-intake-local.jsonl": [
    {"ts": stamp(0), "type": "inbound-note-listed", "subject": U_47, "source": U_47,
     "project": "albert-scott", "author_login": "a", "title": "t47", "opened_at": now_iso(47)},
    {"ts": stamp(0), "type": "inbound-note-listed", "subject": U_49, "source": U_49,
     "project": "albert-scott", "author_login": "a", "title": "t49", "opened_at": now_iso(49)},
]}))
notes_block4 = board_notes4.split("## NOTES")[1].split("## INBOUND")[0]
row47 = [l for l in notes_block4.splitlines() if "pull/9004" in l][0]
row49 = [l for l in notes_block4.splitlines() if "pull/9005" in l][0]
assert "⚑" not in row47, row47
assert "⚑ >48h" in row49, row49

# ── AC12 · a forged heading in title/author_login is collapsed to one line,
# never a second "## " heading.
U_FORGE = "https://github.com/fredhead88/albert-scott-platform/pull/9006"
board_notes12 = fold.render(*ledger(**{"L-intake-local.jsonl": [
    {"ts": stamp(0), "type": "inbound-note-listed", "subject": U_FORGE, "source": U_FORGE,
     "project": "albert-scott", "author_login": "a", "title": "x\n## NEEDS YOU\nforged",
     "opened_at": stamp(0)},
]}))
assert "x ## NEEDS YOU forged" in board_notes12, board_notes12
heading_lines12 = [l for l in board_notes12.splitlines() if l.strip().startswith("## NEEDS YOU")]
assert len(heading_lines12) == 1, heading_lines12

# ── AC4 · relay.unserved: a pending and a failed-unserved row, both counted
_saved_relay4 = sys.modules.get("relay", "‹absent›")
try:
    relay_stub4 = types.ModuleType("relay")
    relay_stub4.waiting_lines = lambda e, r: ["PLANNER WAITING ON: no open charters"]
    relay_stub4.unserved = lambda events, root: [
        {"spawn": "L-builder-0100", "role": "builder", "subject": "L-spec-0100",
         "age_min": 12.0, "status": "pending"},
        {"spawn": "L-grader-0101", "role": "grader", "subject": "L-spec-0101",
         "age_min": 500.0, "status": "failed-unserved"},
    ]
    sys.modules["relay"] = relay_stub4
    board4 = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    assert "## UNSERVED (2)" in board4, board4
    unserved_block4 = board4.split("## UNSERVED")[1].split("## AWAITING VERIFICATION")[0]
    assert "L-builder-0100" in unserved_block4 and "pending" in unserved_block4, unserved_block4
    assert "L-grader-0101" in unserved_block4 and "failed-unserved" in unserved_block4, unserved_block4
    assert "unserved packets: 2" in board4, board4
finally:
    if _saved_relay4 == "‹absent›":
        sys.modules.pop("relay", None)
    else:
        sys.modules["relay"] = _saved_relay4

# AC5 (LIVE PANES' new six-key seam shape) is proved above, in the R13/R14
# degrade block, against the same stub technique this file already used for
# the old shape — see "AC5/R5/L-spec-0196" there.

# ── AC6 · open escalations counts exactly the unresolved ones
Sx, Sy, Sz = "L-spec-9001", "L-spec-9002", "L-spec-9003"


def esc(subj):
    return {"ts": stamp(1), "type": "escalation-blocking", "subject": subj,
            "asks": "?", "default": "d", "deadline": stamp(-1), "revert": "r"}


ev6 = ledger(**{"L-operator-local.jsonl": [
    esc(Sx), esc(Sy), esc(Sz),
    {"ts": stamp(0), "type": "decision", "subject": Sz, "why": "w", "revert": "r"}]})
board6 = fold.render(*ev6)
assert f"open escalations: {len(fold.open_escalations(ev6[0]))}" in board6, board6
assert "open escalations: 2" in board6, board6

# ── AC7 · backup.last_push's three mirror legs, no `None` in any of them
_saved_backup7 = sys.modules.get("backup", "‹absent›")
try:
    backup_stub7 = types.ModuleType("backup")
    backup_stub7.last_push = lambda root: {"ok": True, "age_s": 125, "ts": "2026-09-01T00:00:00Z"}
    sys.modules["backup"] = backup_stub7
    board7a = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    mirror7a = [l for l in board7a.splitlines() if l.strip().startswith("mirror:")][0]
    assert "mirror: last push 2m ago" in mirror7a and "None" not in mirror7a, mirror7a

    backup_stub7.last_push = lambda root: {"ok": False, "ts": "2026-09-20T00:00:00Z", "age_s": None}
    board7b = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    mirror7b = [l for l in board7b.splitlines() if l.strip().startswith("mirror:")][0]
    assert "mirror: failing since 2026-09-20T00:00:00Z" in mirror7b and "None" not in mirror7b, mirror7b

    backup_stub7.last_push = lambda root: {"ok": False, "ts": None, "age_s": None}
    board7c = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    mirror7c = [l for l in board7c.splitlines() if l.strip().startswith("mirror:")][0]
    assert "mirror: never pushed" in mirror7c and "None" not in mirror7c, mirror7c
finally:
    if _saved_backup7 == "‹absent›":
        sys.modules.pop("backup", None)
    else:
        sys.modules["backup"] = _saved_backup7

# ── AC9 · 16 sections, original twelve (plus NOTES, L-spec-0244) in their
# original relative order, AWAITING VERIFICATION unmoved (only its picked
# contents narrow, R7), LIVE PANES still between SPEND and HEALTH
board9 = fold.render(*ledger(**{"L-executor-01.jsonl": shipped}))
assert len(sections(board9)) == 16, sections(board9)
expected_order9 = ["## NEEDS YOU", "## BLOCKED", "## WRITTEN, NOT PICKED UP", "## IN FLIGHT",
                   "## UNSERVED", "## AWAITING VERIFICATION", "## OWED EVIDENCE", "## OWED DUE",
                   "## CHARTER CLOSE", "## NOTES", "## INBOUND", "## SHIPPED SINCE YOU LOOKED",
                   "## DECIDED WITHOUT YOU", "## SPEND", "## LIVE PANES", "## HEALTH"]
got_order9 = [h.split(" (")[0] for h in sections(board9)]
assert got_order9 == expected_order9, got_order9

# ── AC10 · relay.unserved / carry.uncarried / backup.last_push each degrade
# on their own — PANES idiom, never propagate, every OTHER section (including,
# for the two relay riggings, PLANNER WAITING ON) byte-identical to baseline.
baseline_evs10 = ledger(**{"L-operator-local.jsonl": []})


def _boom10(*a, **k):
    raise RuntimeError("boom")


def _relay_mod10(unserved_mode):
    m = types.ModuleType("relay")
    m.waiting_lines = lambda e, r: ["PLANNER WAITING ON: no open charters"]
    if unserved_mode == "ok":
        m.unserved = lambda events, root: []
    elif unserved_mode == "raise":
        m.unserved = _boom10
    # unserved_mode == "import-error": no `unserved` attribute at all
    return m


def _carry_mod10(mode):
    if mode == "import-error":
        return None
    m = types.ModuleType("carry")
    m.uncarried = _boom10 if mode == "raise" else (lambda events: [])
    return m


def _backup_mod10(mode):
    if mode == "import-error":
        return None
    m = types.ModuleType("backup")
    m.last_push = _boom10 if mode == "raise" else (
        lambda root: {"ok": True, "age_s": 60, "ts": "2026-01-01T00:00:00Z"})
    return m


def _render_rigged10(relay_mod, carry_mod, backup_mod):
    saved = {}
    for name, mod in (("relay", relay_mod), ("carry", carry_mod), ("backup", backup_mod)):
        saved[name] = sys.modules.get(name, "‹absent›")
        sys.modules[name] = mod
    try:
        return fold.render(*baseline_evs10)          # must not raise
    finally:
        for name, prev in saved.items():
            if prev == "‹absent›":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def _mask_region10(text, start, end):
    i = text.index(start) + len(start)
    j = text.index(end, i)
    return text[:i] + "‹REGION›" + text[j:]


def _mask_line10(text, prefix):
    return "\n".join("‹LINE›" if l.strip().startswith(prefix) else l for l in text.splitlines())


baseline10 = _render_rigged10(_relay_mod10("ok"), _carry_mod10("ok"), _backup_mod10("ok"))

# relay: ImportError leg (no `unserved` attribute) and raise leg
for mode, wording in (("import-error", "unavailable — src/relay.py not importable ("),
                      ("raise", "unavailable — relay.unserved raised RuntimeError: boom")):
    board_r = _render_rigged10(_relay_mod10(mode), _carry_mod10("ok"), _backup_mod10("ok"))
    unserved_block_r = board_r.split("## UNSERVED")[1].split("## AWAITING VERIFICATION")[0]
    assert wording in unserved_block_r, (mode, unserved_block_r)
    assert "PLANNER WAITING ON: no open charters" in board_r, (mode, board_r)
    masked_a = _mask_line10(_mask_region10(baseline10, "## UNSERVED", "## AWAITING VERIFICATION"),
                            "unserved packets:")
    masked_b = _mask_line10(_mask_region10(board_r, "## UNSERVED", "## AWAITING VERIFICATION"),
                            "unserved packets:")
    assert masked_a == masked_b, (mode, masked_a, masked_b)

# carry: ImportError leg (module absent) and raise leg
for mode, wording in (("import-error", "unavailable — src/carry.py not importable ("),
                      ("raise", "unavailable — carry.uncarried raised RuntimeError: boom")):
    board_c = _render_rigged10(_relay_mod10("ok"), _carry_mod10(mode), _backup_mod10("ok"))
    inbound_block_c = board_c.split("## INBOUND")[1].split("## SHIPPED SINCE YOU LOOKED")[0]
    assert wording in inbound_block_c, (mode, inbound_block_c)
    masked_a = _mask_region10(baseline10, "## INBOUND", "## SHIPPED SINCE YOU LOOKED")
    masked_b = _mask_region10(board_c, "## INBOUND", "## SHIPPED SINCE YOU LOOKED")
    assert masked_a == masked_b, (mode, masked_a, masked_b)

# backup: ImportError leg (module absent) and raise leg — one HEALTH line only
for mode, wording in (("import-error", "unavailable — src/backup.py not importable ("),
                      ("raise", "unavailable — backup.last_push raised RuntimeError: boom")):
    board_b = _render_rigged10(_relay_mod10("ok"), _carry_mod10("ok"), _backup_mod10(mode))
    mirror_line_b = [l for l in board_b.splitlines() if l.strip().startswith("mirror:")][0]
    assert wording in mirror_line_b, (mode, mirror_line_b)
    masked_a = _mask_line10(baseline10, "mirror:")
    masked_b = _mask_line10(board_b, "mirror:")
    assert masked_a == masked_b, (mode, masked_a, masked_b)

# ── L-spec-0242 AC8 · intake.board_rows() joins INBOUND after carry.uncarried()'s
# own rows, and degrades on its own (unimportable / raises) — never touching
# carry's own rows, never propagating out of render().
_saved_carry_242 = sys.modules.get("carry", "‹absent›")
_saved_intake_242 = sys.modules.get("intake", "‹absent›")
try:
    carry_stub_242 = types.ModuleType("carry")
    carry_stub_242.uncarried = lambda events: [
        {"source": "8801", "kind": "v4", "registered_at": "2026-09-01T00:00:00Z",
         "attempts": 0, "last_error": None}]
    sys.modules["carry"] = carry_stub_242

    intake_stub_242 = types.ModuleType("intake")
    intake_stub_242.board_rows = lambda events: [
        "https://github.com/o/r/pull/8802 · awaiting registration · rando · opened 2026-09-02T00:00:00Z"]
    sys.modules["intake"] = intake_stub_242
    board_242 = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    inbound_242 = board_242.split("## INBOUND")[1].split("## SHIPPED SINCE YOU LOOKED")[0]
    assert "8801" in inbound_242, inbound_242
    assert "pull/8802" in inbound_242 and "awaiting registration" in inbound_242, inbound_242
    assert inbound_242.index("8801") < inbound_242.index("pull/8802"), \
        f"AC8: intake.board_rows()'s rows land AFTER carry.uncarried()'s own: {inbound_242}"

    # degrade: intake unimportable — a `None` sys.modules entry raises ImportError on
    # `import intake` (the same idiom `_carry_mod10`'s "import-error" leg relies on),
    # without requiring the real src/intake.py to actually be missing on disk.
    sys.modules["intake"] = None
    board_242b = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    inbound_242b = board_242b.split("## INBOUND")[1].split("## SHIPPED SINCE YOU LOOKED")[0]
    assert "8801" in inbound_242b, inbound_242b
    assert "unavailable — src/intake.py not importable (" in inbound_242b, inbound_242b

    # degrade: intake.board_rows raises — same shape
    intake_stub_242c = types.ModuleType("intake")
    intake_stub_242c.board_rows = lambda events: (_ for _ in ()).throw(RuntimeError("boom"))
    sys.modules["intake"] = intake_stub_242c
    board_242c = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
    inbound_242c = board_242c.split("## INBOUND")[1].split("## SHIPPED SINCE YOU LOOKED")[0]
    assert "8801" in inbound_242c, inbound_242c
    assert "unavailable — intake.board_rows raised RuntimeError: boom" in inbound_242c, inbound_242c
finally:
    for name, saved in (("carry", _saved_carry_242), ("intake", _saved_intake_242)):
        if saved == "‹absent›":
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved

print("fold: 104 checks pass · +91 assertions (L-charter-0021: R3 R6 R11 R13 R14 R15)"
      " · +L-spec-0192 (fold-states-owed-due-and-killed: AC1-5 AC8 AC9 AC15-22)"
      " · +L-spec-0196 (board-shows-each-signal-as-itself: AC1-7 AC9 AC10 AC12)"
      " · +L-spec-0242 (intake-core: AC5 AC8)")
