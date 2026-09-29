#!/usr/bin/env python3
"""One runnable check on the fold rules. Run: python3 test_fold.py"""
import json, os, pathlib, re, shutil, sys, tempfile, types
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
import freeze  # noqa: E402

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


# ── AC10 (L-spec-0429) · freeze.FLAG resolves under THIS file's own DOIT_ROOT
# (set above, before `import fold`, which imports `freeze`) — never under the
# real Path.home() — so a live Thinker freeze on the shared box can never flip
# this suite's `board.startswith("# board · ")` assertions red. ─────────────
assert str(freeze.FLAG).startswith(str(TMP)), freeze.FLAG
assert not str(freeze.FLAG).startswith(str(pathlib.Path.home())), freeze.FLAG
print("AC10 ok")

# AC5's fourth case (absent) baseline: captured here, before ANY freeze.FLAG
# override in this file (freeze.FLAG still points under TMP, to a file that
# does not exist — a fresh tempdir), so later cases can diff against an
# untouched render().
B_BEFORE_FREEZE = fold.render(*ledger())


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
# L-charter-0046/fold-proving-state (L-spec-0472): `closed-unbuilt` is
# BUILD_DONE, so this charter (l1-complete, one spec) now enters "proving"
# the instant it folds — it never reaches CHARTER CLOSE.
assert closed[2][C]["state"] == "proving", closed[2][C]["state"]
# The substring is unchanged, but it now comes from the new PROVING row's own
# "closed unbuilt" suffix (D112, AC12) — CHARTER CLOSE no longer lists this
# charter at all.
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

# D76 retraction, and `owed-ac` authorization. `owed-ac` is on the spec-writer's
# and spec-auditor's May-declare lists and nobody else's — an executor that could
# declare one could walk any shipped spec into a terminal success state alone.
# L-charter-0038/L-spec-0384, R2: a criterion-LESS `owed-ac` (this fixture's own
# shape, pre-dating the schema's `criterion` requirement) now produces no
# `owed.checks()` row at all — the dead branch that used to read it as
# `shipped-owed-evidence` is deleted, not ported (0 such events exist live).
owed = [{"ts": stamp(0), "type": "owed-ac", "subject": S, "wake_at": stamp(-7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-executor-01.jsonl": shipped,
                           "L-spec-writer-01.jsonl": owed})
assert sp[S]["state"] == "shipped", "a criterion-less owed-ac produces no row"
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
assert ch[C]["state"] == "proving" and ch[C]["briefs"] == 1, \
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
assert ch[C]["state"] == "proving", "a ref naming nothing answers nothing"

# both new events are L2 conjuncts in all but name, so both are authorized: a
# builder that may stamp either closes a charter from the seat being judged.
_, _, ch, ig, _ = ledger(**{**L1, "L-grader-02.jsonl": inscope,
                            "L-builder-02.jsonl": answered})
assert ch[C]["state"] == "proving" and len(ig) == 1, "only the Executor may answer a brief"
_, _, ch, ig, _ = ledger(**{**L1, "L-executor-01.jsonl": shipped,
                            "L-builder-02.jsonl": [{"ts": stamp(0), "type": "sweep-fixpoint",
                                                    "subject": C}]})
assert ch[C]["state"] == "proving" and len(ig) == 1, "a builder may not declare the sweep done"

# and the board says so, because a close blocked by something invisible is a wedge.
# L-charter-0046/fold-proving-state (L-spec-0472): this charter is now "proving"
# (not L1-complete), so CHARTER CLOSE no longer lists it and carries no
# in-scope-brief count — the fact survives on `c["briefs"]` itself, computed
# unconditionally regardless of state, even though the render string that used
# to carry it does not.
evs = ledger(**{**L1, "L-grader-02.jsonl": inscope})
assert evs[2][C]["state"] == "proving" and evs[2][C]["briefs"] == 1, \
    "the fact survives even though CHARTER CLOSE no longer names it"

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
assert len(sections(eboard)) == 18, \
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
assert len(sections(fboard)) == 18, "sections, always"
assert len(frows) == 1 and "spend · x ## FORGED (9) · $0.00 · 0 spawns" in frows[0], frows

# a label is a DIRECTORY NAME by default and nothing curates it: a newline in one
# must not forge a board line, and above all not an extra section — §8.3's
# positional layout is the whole reason that check exists.
forged = ledger(**{"L-operator-local.jsonl": [sp_ev(project="x\n## FORGED (9)", cost_usd=1.0)]})
board = fold.render(*forged)
assert len(sections(board)) == 18, "sections, always"
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
# L-spec-0276/R3 Target 1-2: an unparseable, non-anchor deadline no longer
# auto-passes (Target 1) — it surfaces under DEADLINE UNRESOLVABLE instead
# (Target 2), never silently dropped either. Was: "an unparseable deadline is
# past — undetermined is never clean" (the pre-Target-1 behaviour this spec
# replaces — Boundaries names this exact assertion).
bad = fold.render(*ledger(**{"L-builder-01.jsonl": [{**q, "deadline": "sometime"}]}))
assert "unanswered past" not in bad, "an unparseable deadline never silently auto-overdue"
assert "## NEEDS YOU (0)" in bad, "...and so it is not under NEEDS YOU either"
du_block = bad[bad.index("## DEADLINE UNRESOLVABLE"):]
assert S in du_block and "sometime" in du_block, \
    "an unparseable deadline is visible under DEADLINE UNRESOLVABLE instead"

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
# L-charter-0038/L-spec-0384, AC4: `met` is strict-after the governing
# `owed-ac`, never a tie — `stamp_h(-1)` (an hour after `owed_ac7`'s own
# `stamp(0)`) keeps this fixture on the non-tie side of that pin.
owed_met = [{"ts": stamp_h(-1), "type": "owed-met", "subject": S, "criterion": "AC7",
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
assert len(sections(wb)) == 18, ("the block is not a section of its own (18 = ten + SPEND + LIVE PANES "
                                 "+ OWED DUE/UNSERVED/INBOUND/NOTES, L-spec-0196/0244 + DEADLINE "
                                 "UNRESOLVABLE, L-spec-0276 + PROVING, L-spec-0472)")

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
assert len(sections(gone)) == 18, "the degrade line is a HEALTH row, not a section"

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

# ── AC1 · a due, unmet owed-ac -> shipped-owed-due, not evidence or plain
# shipped. L-charter-0038/L-spec-0384: ship-anchored now, not the raw
# wake_at — `shipped_early` ships 5 days ago, the owed-ac is declared 10 days
# ago with `wake_at` 7 days ago (a 3-day interval), landing `due_at` 2 days
# ago.
shipped_early = [{"ts": stamp(5), "type": "shipped", "subject": S}]
due_ac1 = [{"ts": stamp(10), "type": "owed-ac", "subject": S, "criterion": "AC1", "wake_at": stamp(7)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped_early,
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "shipped-owed-due", sp[S]["state"]

# ── AC2 · the SAME 10d-ago declaration, `wake_at` only 2 days ago: the 8-day
# interval anchored at the 5d-ago ship lands `due_at` 3 days in the FUTURE ->
# stays shipped-owed-evidence (D25)
future_ac1 = [{**due_ac1[0], "wake_at": stamp(2)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped_early,
                           "L-spec-writer-01.jsonl": future_ac1})
assert sp[S]["state"] == "shipped-owed-evidence", sp[S]["state"]

# ── AC3 · a due criterion plus a co-existing NOT-yet-due one -> due wins
due_plus_pending = due_ac1 + [{"ts": stamp(10), "type": "owed-ac", "subject": S,
                                "criterion": "AC2", "wake_at": stamp(2)}]
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped_early,
                           "L-spec-writer-01.jsonl": due_plus_pending})
assert sp[S]["state"] == "shipped-owed-due", sp[S]["state"]

# ── AC4 · owed-met, strictly after the governing owed-ac, discharges it -> accepted
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed,
                           "L-executor-01.jsonl": shipped_early + [{"ts": stamp(0), "type": "owed-met",
                                                              "subject": S, "criterion": "AC1",
                                                              "evidence": "checked by hand"}],
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "accepted", sp[S]["state"]

# ── AC5 · a standing rejected-criterion routes away from shipped-owed-due
# entirely — UNCHANGED: open_rejects short-circuits before any owed-clock
# logic runs, so the shape of the owed-ac underneath it is immaterial.
_, sp, _, _, _ = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
                           "L-reviewer-01.jsonl": reviewed,
                           "L-executor-01.jsonl": shipped + [{"ts": stamp(0), "type": "rejected-criterion",
                                                              "subject": S, "criterion": "AC1"}],
                           "L-spec-writer-01.jsonl": due_ac1})
assert sp[S]["state"] == "shipped", sp[S]["state"]

# ── AC9 (L-charter-0038/L-spec-0384, R3) · owed_due() ship-anchored, across
# every spec — a FRESH fixture: neither `built`/`graded`/`reviewed`/`shipped`
# nor `shipped_early` above (both ship "today" relative to their own owed-ac)
# can, alone, produce a genuinely overdue `due_at` for THIS assertion's own
# two-spec shape, so `S`/`S2` here ship 5 days ago and declare 10 days ago,
# `wake_at` 7 days ago — `due_at` 2 days ago, on two specs.
S2 = "L-spec-0143"
built_s2 = [{**e, "subject": S2} for e in built]
graded_s2 = [{**e, "subject": S2} for e in graded]
reviewed_s2 = [{**e, "subject": S2} for e in reviewed]
shipped_early_s2 = [{**e, "subject": S2} for e in shipped_early]
due_ac1_s2 = [{"ts": stamp(10), "type": "owed-ac", "subject": S2, "criterion": "AC1", "wake_at": stamp(7)}]
_, spdue, _, _, _ = ledger(**{
    "L-builder-01.jsonl": built + built_s2, "L-grader-01.jsonl": graded + graded_s2,
    "L-reviewer-01.jsonl": reviewed + reviewed_s2,
    "L-executor-01.jsonl": shipped_early + shipped_early_s2,
    "L-spec-writer-01.jsonl": due_ac1 + due_ac1_s2})
rows = fold.owed_due(spdue)
assert {(r["spec"], r["criterion"]) for r in rows} == {(S, "AC1"), (S2, "AC1")}, rows
assert all(r["days_overdue"] > 0 for r in rows), rows
assert all(set(r) == {"spec", "criterion", "due_at", "days_overdue", "src"} for r in rows), rows
assert all(isinstance(r["due_at"], str) and " " not in r["due_at"] for r in rows), rows
print("AC9 ok")

# ── AC9 (L-spec-0192) · killed is terminal — survives a LATER stage event on the same subject
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
                  "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped_early,
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

# ── AC1 (L-charter-0038/L-spec-0384, R3/AC14) · one due, unmet owed-ac
# (2d overdue, AC9's own S/shipped_early/due_ac1 fixture) -> one OWED DUE row
# naming the row's OWN computed due_at, never the raw wake_at it used to echo
# verbatim (the bug this unit fixes); empty stays visible.
ev1 = ledger(**{"L-builder-01.jsonl": built, "L-grader-01.jsonl": graded,
               "L-reviewer-01.jsonl": reviewed, "L-executor-01.jsonl": shipped_early,
               "L-spec-writer-01.jsonl": due_ac1})
board1 = fold.render(*ev1)
assert "## OWED DUE (1)" in board1, board1
od_block1 = board1.split("## OWED DUE")[1].split("## CHARTER CLOSE")[0]
assert S in od_block1 and "AC1" in od_block1 and "2d overdue" in od_block1, od_block1
assert stamp(2) in od_block1, od_block1
assert due_ac1[0]["wake_at"] not in od_block1, od_block1
empty_board1 = fold.render(*ledger(**{"L-operator-local.jsonl": []}))
assert "## OWED DUE (0)" in empty_board1, empty_board1

# ── AC2 (L-charter-0038/L-spec-0384, R3/AC14) · a due row (S, AC9's fixture)
# and an EXPIRED-only row (S2, AC6's clamped shape, `shipped_at` 10 days ago)
# — `owed_due`/HEALTH count only the due one; the expired row is no longer
# "overdue" at all, and OVER-7-DAYS is gone (deleted, not re-thresholded).
S2 = "L-spec-0143"
built2 = [{"ts": stamp(3), "type": "spec-written", "subject": S2, "charter": C},
          {"ts": stamp(2), "type": "build-started", "subject": S2},
          {"ts": stamp(2), "type": "build-done", "subject": S2}]
graded2 = [{"ts": stamp(1), "type": "verdict", "subject": S2, "confirmed": True}]
reviewed2 = [{"ts": stamp(1), "type": "review", "subject": S2, "depth": "gates-only"}]
shipped2 = [{"ts": stamp(10), "type": "shipped", "subject": S2}]
due3 = due_ac1                                # S, AC1, 2d overdue — due
same_ts_s2 = stamp(50)
due9 = [{"ts": same_ts_s2, "type": "owed-ac", "subject": S2, "criterion": "AC2", "wake_at": same_ts_s2}]
ev2 = ledger(**{"L-builder-01.jsonl": built + built2, "L-grader-01.jsonl": graded + graded2,
               "L-reviewer-01.jsonl": reviewed + reviewed2,
               "L-executor-01.jsonl": shipped_early + shipped2,
               "L-spec-writer-01.jsonl": due3 + due9})
board2 = fold.render(*ev2)
due_line2 = [l for l in board2.splitlines() if l.strip().startswith("due owed:")][0]
assert "due owed: 1" in due_line2 and "OVER 7 DAYS" not in due_line2, due_line2
assert S2 not in due_line2 and "AC2" not in due_line2, due_line2
od_block2 = board2.split("## OWED DUE")[1].split("## CHARTER CLOSE")[0]
assert S in od_block2 and "AC1" in od_block2, od_block2
assert S2 not in od_block2 and "AC2" not in od_block2, od_block2
rows2 = fold.owed_due(ev2[1])
assert {(r["spec"], r["criterion"]) for r in rows2} == {(S, "AC1")}, rows2
assert ev2[1][S2]["state"] == "shipped-owed-expired", ev2[1][S2]["state"]
print("AC14 ok")

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

# ── AC9 · 18 sections (16, plus DEADLINE UNRESOLVABLE — L-spec-0276, plus
# PROVING — L-spec-0472, both last), original twelve (plus NOTES, L-spec-0244)
# in their original relative order, AWAITING VERIFICATION unmoved (only its
# picked contents narrow, R7), LIVE PANES still between SPEND and HEALTH
board9 = fold.render(*ledger(**{"L-executor-01.jsonl": shipped}))
assert len(sections(board9)) == 18, sections(board9)
expected_order9 = ["## NEEDS YOU", "## BLOCKED", "## WRITTEN, NOT PICKED UP", "## IN FLIGHT",
                   "## UNSERVED", "## AWAITING VERIFICATION", "## OWED EVIDENCE", "## OWED DUE",
                   "## CHARTER CLOSE", "## PROVING", "## NOTES", "## INBOUND",
                   "## SHIPPED SINCE YOU LOOKED",
                   "## DECIDED WITHOUT YOU", "## SPEND", "## LIVE PANES", "## HEALTH",
                   "## DEADLINE UNRESOLVABLE"]
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

# ── L-spec-0264 (deployer-actor) AC1 · deploy-landed's set gains `deployer`,
# exact equality — a stray extra or a dropped existing actor both fail.
assert fold.EMITS["deploy-landed"] == {"executor", "operator", "deployer"}, \
    fold.EMITS["deploy-landed"]

# AC2 · the two new EMITS entries, pinned to their own minimal sets; `deployer`
# is explicitly excluded from deploy-approved (SD8/SD24: it never clears its
# own hold).
assert fold.EMITS["deploy-attempt"] == {"deployer"}, fold.EMITS["deploy-attempt"]
assert fold.EMITS["deploy-approved"] == {"operator", "thinker"}, fold.EMITS["deploy-approved"]
assert "deployer" not in fold.EMITS["deploy-approved"]

# AC3 · deploy-started/deploy-failed/deploy-refused stay absent from EMITS —
# the open-door design is unchanged, proven rather than merely unbroken.
assert "deploy-started" not in fold.EMITS
assert "deploy-failed" not in fold.EMITS
assert "deploy-refused" not in fold.EMITS

# AC4 · check_append() enforces the new/changed grants at the single-event door
ev_landed_264 = {"type": "deploy-landed", "subject": S}
assert fold.check_append(ev_landed_264, "deployer") is None, \
    fold.check_append(ev_landed_264, "deployer")
assert fold.check_append(ev_landed_264, "builder") is not None

ev_attempt_264 = {"type": "deploy-attempt", "subject": S, "outcome": "deployed"}
assert fold.check_append(ev_attempt_264, "deployer") is None, \
    fold.check_append(ev_attempt_264, "deployer")
assert fold.check_append(ev_attempt_264, "executor") is not None
assert fold.check_append(ev_attempt_264, "operator") is not None

ev_approved_264 = {"type": "deploy-approved", "subject": S, "sha": "abc1234"}
assert fold.check_append(ev_approved_264, "operator") is None, \
    fold.check_append(ev_approved_264, "operator")
assert fold.check_append(ev_approved_264, "thinker") is None, \
    fold.check_append(ev_approved_264, "thinker")
assert fold.check_append(ev_approved_264, "deployer") is not None

ev_started_264 = {"type": "deploy-started", "subject": S}
assert fold.check_append(ev_started_264, "deployer") is None, \
    fold.check_append(ev_started_264, "deployer")

# AC5 · D90's real filename-to-actor path (no mock): `L-deployer-local.jsonl`
# derives actor "deployer" for free, and fold() honours its deploy-landed/
# deploy-attempt claims rather than ignoring them.
ev264, *_ = ledger(**{"L-deployer-local.jsonl": [
    {"ts": stamp(0), "type": "deploy-landed", "subject": S},
    {"ts": stamp(1), "type": "deploy-attempt", "subject": S, "outcome": "deployed"}]})
assert {e["actor"] for e in ev264} == {"deployer"}, {e["actor"] for e in ev264}
_, _, ignored264, by_subject264 = fold.fold(ev264)
assert not any(e["type"] in ("deploy-landed", "deploy-attempt") for e in ignored264), ignored264
assert any(e["type"] == "deploy-landed" for e in by_subject264[S]), by_subject264[S]
assert any(e["type"] == "deploy-attempt" for e in by_subject264[S]), by_subject264[S]

# AC6 · a deployer-authored deploy-started/deploy-landed pair renders and
# clears "deploy in flight" exactly as an executor-authored pair already does
# (in_flight_deploys() is actor-blind by construction — unaffected by this spec).
board264a = fold.render(*ledger(**{"L-deployer-local.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9, "sha": sha_full, "target": "prod"}]}))
assert "deploy in flight" in board264a, board264a
board264b = fold.render(*ledger(**{"L-deployer-local.jsonl": [
    {"ts": stamp(0), "type": "deploy-started", "subject": S9, "sha": sha_full, "target": "prod"},
    {"ts": stamp(0), "type": "deploy-landed", "subject": S9, "sha": sha_full, "target": "prod"}]}))
assert "deploy in flight" not in board264b, board264b

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0271 · board-owners (L-charter-0033)
# ══════════════════════════════════════════════════════════════════════════════
import re as re271  # noqa: E402

# ── AC1 · fold.BOARD_OWNERS is complete and well-typed for every block()-
# rendered section, and HEALTH is excluded by name ─────────────────────────────
board271_empty = fold.render(*ledger())
titles271 = {m.group(1) for m in re271.finditer(r"^## (.+) \(\d+\)$", board271_empty, re271.M)}
assert titles271 == set(fold.BOARD_OWNERS), (titles271, set(fold.BOARD_OWNERS))
assert "HEALTH" not in fold.BOARD_OWNERS, \
    "★ BOARD_OWNERS is complete and well-typed for every block()-rendered section, and HEALTH is excluded by name"
assert not re271.search(r"^## HEALTH \(\d+\)$", board271_empty, re271.M), \
    "★ BOARD_OWNERS is complete and well-typed for every block()-rendered section, and HEALTH is excluded by name"
for _title271, (_owner271, _closer271) in fold.BOARD_OWNERS.items():
    assert isinstance(_owner271, str) and _owner271, (_title271, _owner271)
    if isinstance(_closer271, str):
        assert _closer271.startswith("derived: ") and len(_closer271) > len("derived: "), \
            (_title271, _closer271)
    else:
        assert (isinstance(_closer271, tuple) and _closer271
               and all(isinstance(c, str) and c for c in _closer271)), (_title271, _closer271)

# ── AC2 · UNSERVED is relay-owned/derived and TRIAGE is thinker-owned with
# both closers ──────────────────────────────────────────────────────────────────
assert fold.BOARD_OWNERS["UNSERVED"][0] == "relay", fold.BOARD_OWNERS["UNSERVED"]
assert (isinstance(fold.BOARD_OWNERS["UNSERVED"][1], str)
       and fold.BOARD_OWNERS["UNSERVED"][1].startswith("derived: ")), fold.BOARD_OWNERS["UNSERVED"]
assert fold.BOARD_OWNERS["TRIAGE"] == ("thinker", ("brief-answered", "brief-routed")), \
    fold.BOARD_OWNERS["TRIAGE"]

# ── AC3 · a requirement-less brief renders under TRIAGE with its age; a
# requirement-bearing one does not (open_briefs', not triage_briefs') ──────────
brief_ev3 = {"ts": stamp(2), "type": "brief", "subject": "L-charter-0033", "why": "orphan"}
board271_3 = fold.render(*ledger(**{"L-thinker-0001.jsonl": [brief_ev3]}))
triage271_3 = board271_3.split("## TRIAGE")[1].split("## NEEDS YOU")[0]
expected_age3 = f"{fold.age_days(brief_ev3):.1f}d"
assert "orphan" in triage271_3 and expected_age3 in triage271_3, (triage271_3, expected_age3)

brief_ev3b = {"ts": stamp(2), "type": "brief", "subject": "L-charter-0033", "why": "orphan",
             "requirement": "R2"}
board271_3b = fold.render(*ledger(**{"L-thinker-0001.jsonl": [brief_ev3b]}))
triage271_3b = board271_3b.split("## TRIAGE")[1].split("## NEEDS YOU")[0]
assert "orphan" not in triage271_3b, triage271_3b

# ── AC4 · TRIAGE clears via an authorized brief-answered or brief-routed
# (executor/operator, thinker/operator); an unauthorized-actor brief-routed
# does not clear it, and the check is triage_briefs' own, not fold.fold()'s
# ignored list ───────────────────────────────────────────────────────────────
brief_ev4 = {"ts": stamp(2), "type": "brief", "subject": "L-charter-0033", "why": "orphan4"}
ev4a, *_ = ledger(**{"L-thinker-0001.jsonl": [brief_ev4]})
src4 = next(e["_src"] for e in ev4a if e.get("why") == "orphan4")

board4a = fold.render(*ledger(**{"L-thinker-0001.jsonl": [brief_ev4],
                                 "L-executor-0001.jsonl": [
                                     {"ts": stamp(0), "type": "brief-answered", "ref": src4}]}))
triage4a = board4a.split("## TRIAGE")[1].split("## NEEDS YOU")[0]
assert "orphan4" not in triage4a, triage4a

board4b = fold.render(*ledger(**{"L-thinker-0001.jsonl": [brief_ev4],
                                 "L-thinker-0002.jsonl": [
                                     {"ts": stamp(0), "type": "brief-routed", "ref": src4}]}))
triage4b = board4b.split("## TRIAGE")[1].split("## NEEDS YOU")[0]
assert "orphan4" not in triage4b, triage4b

board4c = fold.render(*ledger(**{"L-thinker-0001.jsonl": [brief_ev4],
                                 "L-builder-0001.jsonl": [
                                     {"ts": stamp(0), "type": "brief-routed", "ref": src4}]}))
triage4c = board4c.split("## TRIAGE")[1].split("## NEEDS YOU")[0]
assert "orphan4" in triage4c, triage4c

# ── AC5 · inbound-covered is admitted for spec-writer/executor and requires
# source+covered_by ─────────────────────────────────────────────────────────────
assert fold.EMITS["inbound-covered"] == {"spec-writer", "executor"}, fold.EMITS["inbound-covered"]
assert fold.REQUIRED["inbound-covered"] == ("source", "covered_by"), fold.REQUIRED["inbound-covered"]
reason5 = fold.check_append({"type": "inbound-covered", "subject": "S", "source": "1472"}, "executor")
assert reason5 is not None and "covered_by" in reason5, reason5

# ── AC8-AC11 · a killed spec's superseded_by, read against the SAME five-state
# done-set every other spec in `mine` is checked against ───────────────────────
CID271, S1_271, S2_271 = "L-charter-2801", "L-spec-2801", "L-spec-2802"


def _kill_fixture271(s2_files, superseded_by=S2_271):
    kill_ev = {"ts": stamp(4), "type": "spec-killed", "subject": S1_271, "check": 2}
    if superseded_by is not None:
        kill_ev["superseded_by"] = superseded_by
    files = {
        "L-spec-writer-01.jsonl": [
            {"ts": stamp(5), "type": "spec-written", "subject": S1_271, "charter": CID271}],
        "L-builder-01.jsonl": [kill_ev],
        "L-thinker-01.jsonl": [
            {"ts": stamp(3), "type": "charter-filed", "subject": CID271, "covers": "none"}],
        "L-executor-01.jsonl": [
            {"ts": stamp(0), "type": "sweep-fixpoint", "subject": CID271}],
    }
    files.update(s2_files)
    return ledger(**files)


accepted_s2_files271 = {
    "L-spec-writer-02.jsonl": [
        {"ts": stamp(3), "type": "spec-written", "subject": S2_271, "charter": CID271}],
    "L-builder-02.jsonl": [{"ts": stamp(2), "type": "build-started", "subject": S2_271},
                          {"ts": stamp(2), "type": "build-done", "subject": S2_271}],
    "L-grader-02.jsonl": [{"ts": stamp(1), "type": "verdict", "subject": S2_271, "confirmed": True}],
    "L-reviewer-02.jsonl": [{"ts": stamp(1), "type": "review", "subject": S2_271, "depth": "gates-only"}],
    "L-executor-02.jsonl": [{"ts": stamp(0), "type": "shipped", "subject": S2_271}],
}

# AC8 · a killed spec's superseded_by, accepted, releases L2-complete via both
# fold() and closable()
ev8, specs8, charters8, _, _ = _kill_fixture271(accepted_s2_files271)
assert specs8[S2_271]["state"] == "accepted", specs8[S2_271]["state"]
assert charters8[CID271]["state"] == "L2-complete", charters8[CID271]
cl8 = fold.closable(ev8, CID271)
assert cl8["all_accepted"] is True and fold.l2_complete(cl8), cl8

# AC9 · a superseded_by target reaching any done-state (closed-unbuilt), not
# only accepted, still releases L2-complete
closed_unbuilt_s2_files271 = {
    "L-operator-02.jsonl": [{"ts": stamp(0), "type": "spec-closed", "subject": S2_271,
                            "charter": CID271}],
}
ev9, specs9, charters9, _, _ = _kill_fixture271(closed_unbuilt_s2_files271)
assert specs9[S2_271]["state"] == "closed-unbuilt", specs9[S2_271]["state"]
assert charters9[CID271]["state"] == "L2-complete", charters9[CID271]

# AC10 · a superseded_by target outside every done-state does not release the charter
shipped_only_s2_files271 = {
    "L-spec-writer-02.jsonl": [
        {"ts": stamp(3), "type": "spec-written", "subject": S2_271, "charter": CID271}],
    "L-builder-02.jsonl": [{"ts": stamp(2), "type": "build-started", "subject": S2_271},
                          {"ts": stamp(2), "type": "build-done", "subject": S2_271}],
    "L-executor-02.jsonl": [{"ts": stamp(0), "type": "shipped", "subject": S2_271}],
}
ev10, specs10, charters10, _, _ = _kill_fixture271(shipped_only_s2_files271)
assert specs10[S2_271]["state"] == "shipped", specs10[S2_271]["state"]
assert charters10[CID271]["state"] != "L2-complete", charters10[CID271]
assert fold.closable(ev10, CID271)["all_accepted"] is False

# AC11 · a killed spec with no superseded_by still blocks L2-complete, unchanged
ev11, specs11, charters11, _, _ = _kill_fixture271(accepted_s2_files271, superseded_by=None)
assert specs11[S1_271]["state"] == "killed", specs11[S1_271]["state"]
assert charters11[CID271]["state"] != "L2-complete", charters11[CID271]
assert fold.closable(ev11, CID271)["all_accepted"] is False

# ── AC13 (fold half) · unserved_line() maps "stale-still-offered" to the
# display text "stale, still offered" ───────────────────────────────────────────
relay_stub271 = types.ModuleType("relay")
relay_stub271.waiting_lines = lambda events, root: []
relay_stub271.unserved = lambda events, root: [
    {"spawn": "L-research-0200", "role": "research", "subject": "L-spec-0272",
     "age_min": 6.0, "status": "stale-still-offered"}]
_saved271 = sys.modules.get("relay", "‹absent›")
try:
    sys.modules["relay"] = relay_stub271
    board271_stale = fold.render(*ledger())
finally:
    if _saved271 == "‹absent›":
        sys.modules.pop("relay", None)
    else:
        sys.modules["relay"] = _saved271
unserved_block271 = board271_stale.split("## UNSERVED")[1].split("## AWAITING VERIFICATION")[0]
assert "stale, still offered" in unserved_block271, unserved_block271
assert "pending" not in unserved_block271, unserved_block271

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0276 · defaults-and-dispatch-order (L-charter-0033) — R3 Target 1-2
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · "before merge" passes once a `shipped` event for the subject exists ──
ev276 = [{"type": "spec-written", "subject": "S276"}]
assert fold.deadline_passed("before merge", ev276, "S276") is False
ev276b = ev276 + [{"type": "shipped", "subject": "S276"}]
assert fold.deadline_passed("before merge", ev276b, "S276") is True

# ── AC2 · "before deploy" needs its OWN subject's deploy-landed when the
# project has ANY deploy-landed at all — the fallback must NOT engage ─────────
ev276c = [{"type": "spec-written", "subject": "S276", "project": "P276"},
          {"type": "spec-written", "subject": "T276", "project": "P276"},
          {"type": "deploy-landed", "subject": "T276", "project": "P276"}]
assert fold.deadline_passed("before deploy", ev276c, "S276") is False, \
    "★ before-deploy needs its OWN subject's deploy-landed when the project has any"
ev276d = ev276c + [{"type": "deploy-landed", "subject": "S276", "project": "P276"}]
assert fold.deadline_passed("before deploy", ev276d, "S276") is True

# ── AC3 · before-deploy falls back to shipped when its project has no
# deploy-landed AT ALL ─────────────────────────────────────────────────────────
ev276e = [{"type": "spec-written", "subject": "S276", "project": "P276"},
          {"type": "shipped", "subject": "S276", "project": "P276"}]
assert fold.deadline_passed("before deploy", ev276e, "S276") is True, \
    "★ before-deploy falls back to shipped when its project has no deploy-landed at all"
ev276f = [{"type": "spec-written", "subject": "S276", "project": "P276"}]
assert fold.deadline_passed("before deploy", ev276f, "S276") is False

# ── AC4 · an ISO deadline is unchanged; blank and unparseable ones never
# auto-pass ─────────────────────────────────────────────────────────────────
assert fold.deadline_passed(stamp(1), [], "S276") is True, \
    "★ an ISO deadline is unchanged; blank and unparseable ones never auto-pass"
assert fold.deadline_passed(stamp(-3), [], "S276") is False
assert fold.deadline_passed("whenever", [], "S276") is False
assert fold.deadline_passed(None, [], "S276") is False

# ── AC5 · an unparseable deadline is never silently overdue and always
# visible somewhere ─────────────────────────────────────────────────────────
S276g = "L-spec-276g"
q276g = {"ts": stamp(2), "type": "question", "subject": S276g, "asks": "x?",
         "default": "d", "deadline": "whenever"}
b276g = fold.render(*ledger(**{"L-builder-276g.jsonl": [q276g]}))
assert S276g not in b276g.split("## NEEDS YOU")[1].split("## BLOCKED")[0], \
    "★ an unparseable deadline is never silently overdue and always visible somewhere"
du276g = b276g[b276g.index("## DEADLINE UNRESOLVABLE"):]
assert S276g in du276g and "whenever" in du276g and "builder" in du276g, du276g

# ── AC6 · a blank deadline is never silently overdue either ───────────────────
S276h = "L-spec-276h"
q276h = {"ts": stamp(2), "type": "question", "subject": S276h, "asks": "x?", "default": "d"}
b276h = fold.render(*ledger(**{"L-builder-276h.jsonl": [q276h]}))
assert S276h not in b276h.split("## NEEDS YOU")[1].split("## BLOCKED")[0], \
    "★ a blank deadline is never silently overdue either"
du276h = b276h[b276h.index("## DEADLINE UNRESOLVABLE"):]
assert S276h in du276h and "no deadline" in du276h, du276h

# ── AC7 · an anchored deadline on a killed subject is unreachable, not
# invisible ─────────────────────────────────────────────────────────────────
S276i = "L-spec-276i"
q276i = {"ts": stamp(2), "type": "question", "subject": S276i, "asks": "x?",
         "default": "d", "deadline": "before merge"}
b276i = fold.render(*ledger(**{
    "L-spec-writer-276i.jsonl": [{"ts": stamp(3), "type": "spec-killed", "subject": S276i, "check": 1}],
    "L-builder-276i.jsonl": [q276i]}))
assert S276i not in b276i.split("## NEEDS YOU")[1].split("## BLOCKED")[0], \
    "★ an anchored deadline on a killed subject is unreachable, not invisible"
du276i = b276i[b276i.index("## DEADLINE UNRESOLVABLE"):]
assert S276i in du276i and "before merge" in du276i, du276i

# ── AC8 · DEADLINE UNRESOLVABLE is registered in BOARD_OWNERS with a
# non-empty owner and closers ─────────────────────────────────────────────────
assert fold.BOARD_OWNERS["DEADLINE UNRESOLVABLE"] == ("operator", ("decision", "unblocked")), \
    "★ DEADLINE UNRESOLVABLE is registered in BOARD_OWNERS with a non-empty owner and closers"

print("fold: 104 checks pass · +91 assertions (L-charter-0021: R3 R6 R11 R13 R14 R15)"
      " · +L-spec-0192 (fold-states-owed-due-and-killed: AC1-5 AC8 AC9 AC15-22)"
      " · +L-spec-0196 (board-shows-each-signal-as-itself: AC1-7 AC9 AC10 AC12)"
      " · +L-spec-0242 (intake-core: AC5 AC8)"
      " · +L-spec-0264 (deployer-actor: AC1-9)"
      " · +L-spec-0276 (defaults-and-dispatch-order: AC1-9)")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0279 · problem-register (L-charter-0034)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC18 · fold.EMITS["fix-shipped"] admits exactly {executor, operator,
# thinker} — check_append refuses a builder-authored one, clears an
# thinker-authored one
ev_fix18 = {"type": "fix-shipped", "subject": "p1", "ref": "abc", "ts": stamp(0)}
assert fold.check_append(ev_fix18, "builder") is not None, \
    "a builder may not claim a fix shipped"
assert fold.check_append(ev_fix18, "thinker") is None, \
    fold.check_append(ev_fix18, "thinker")
assert fold.EMITS["fix-shipped"] == {"executor", "operator", "thinker"}, fold.EMITS["fix-shipped"]
assert fold.EMITS["problem-occurred"] == {"tick"}, fold.EMITS["problem-occurred"]
print("AC18 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0384 · owed-check-model (L-charter-0038)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC10 · fold.EMITS/check_append doors this unit opens or widens ──────────
ev_met10 = {"type": "owed-met", "subject": "L-spec-9384", "criterion": "AC1",
           "evidence": "e", "ts": stamp(0)}
for a in ("owed-sweeper", "executor", "operator"):
    assert fold.check_append(ev_met10, a) is None, (a, fold.check_append(ev_met10, a))
assert fold.check_append(ev_met10, "thinker") is not None

ev_failed10 = {"type": "owed-failed", "subject": "L-spec-9384", "criterion": "AC1",
              "evidence": "e", "kind": "unmet", "ts": stamp(0)}
for a in ("owed-sweeper", "executor", "operator"):
    assert fold.check_append(ev_failed10, a) is None, (a, fold.check_append(ev_failed10, a))
for a in ("thinker", "builder"):
    assert fold.check_append(ev_failed10, a) is not None, a
ev_failed10_noev = {"type": "owed-failed", "subject": "L-spec-9384", "criterion": "AC1",
                    "kind": "unmet", "ts": stamp(0)}
for a in ("owed-sweeper", "executor", "operator", "thinker", "builder"):
    assert fold.check_append(ev_failed10_noev, a) is not None, \
        f"missing evidence must refuse every actor, including {a}"

ev_pp = {"type": "pane-paused", "subject": "x", "ts": stamp(0)}
assert fold.check_append(ev_pp, "look") is None
for a in ("executor", "thinker", "operator"):
    assert fold.check_append(ev_pp, a) is not None, a
for t in ("ci-red", "ci-green"):
    ev_t = {"type": t, "subject": "x", "ts": stamp(0)}
    assert fold.check_append(ev_t, "look") is None
    for a in ("executor", "thinker", "operator"):
        assert fold.check_append(ev_t, a) is not None, (t, a)

ev_pr = {"type": "pane-resumed", "subject": "x", "ts": stamp(0)}
assert fold.check_append(ev_pr, "look") is None
assert fold.check_append(ev_pr, "tick") is None
assert fold.check_append(ev_pr, "executor") is not None

ev_ma = {"type": "message-answered", "subject": "x", "ts": stamp(0)}
for a in ("look", "planner", "executor", "thinker"):
    assert fold.check_append(ev_ma, a) is None, a
for a in ("builder", "grader"):
    assert fold.check_append(ev_ma, a) is not None, a
print("AC10 ok")

# ── AC11 · shipped-owed-expired, its priority against due/waiting, and the
# widened owed tally + done_states ───────────────────────────────────────────
S11 = "L-spec-9386"
same_ts11 = stamp(50)
ac11 = {"ts": same_ts11, "type": "owed-ac", "subject": S11, "criterion": "AC1", "wake_at": same_ts11}
ship11 = {"ts": stamp(10), "type": "shipped", "subject": S11}
assert fold.spec_state([ac11, ship11], set()) == "shipped-owed-expired", \
    fold.spec_state([ac11, ship11], set())

ac11_due = {"ts": stamp(15), "type": "owed-ac", "subject": S11, "criterion": "AC2", "wake_at": stamp(8)}
assert fold.spec_state([ac11, ship11, ac11_due], set()) == "shipped-owed-due", \
    "a due criterion beats an expired-only one"

ac11_wait = {"ts": stamp(15), "type": "owed-ac", "subject": S11, "criterion": "AC2", "wake_at": stamp(-5)}
assert fold.spec_state([ac11, ship11, ac11_wait], set()) == "shipped-owed-evidence", \
    "waiting beats expired-only"

C11 = "L-charter-9386"
S11c = "L-spec-9386c"
ac11c = {"ts": stamp(50), "type": "owed-ac", "subject": S11c, "criterion": "AC1",
        "wake_at": stamp(50), "charter": C11}
ship11c = {"ts": stamp(10), "type": "shipped", "subject": S11c, "charter": C11}
charter_filed11 = {"ts": stamp(50), "type": "charter-filed", "subject": C11}
_K11 = fold.K
fold.K = 1
ev11c = ledger(**{"L-spec-writer-01.jsonl": [ac11c], "L-executor-01.jsonl": [ship11c],
                  "L-thinker-01.jsonl": [charter_filed11]})
assert ev11c[1][S11c]["state"] == "shipped-owed-expired", ev11c[1][S11c]["state"]
assert ev11c[2][C11]["owed"] == 1, ev11c[2][C11]["owed"]
cl11 = fold.closable(ev11c[0], C11)
assert cl11["owed_within_k"] is True, cl11
assert cl11["all_accepted"] is True, cl11
fold.K = 0
ev11c0 = ledger(**{"L-spec-writer-01.jsonl": [ac11c], "L-executor-01.jsonl": [ship11c],
                   "L-thinker-01.jsonl": [charter_filed11]})
assert ev11c0[2][C11]["owed"] == 1, ev11c0[2][C11]["owed"]
cl11b = fold.closable(ev11c0[0], C11)
assert cl11b["owed_within_k"] is False, cl11b
fold.K = _K11
print("AC11 ok")

# ── AC12 · accepted reads LIVE off owed.checks(), not a criterion-ID
# membership test — a later re-date reopens it ───────────────────────────────
S12 = "L-spec-9387"
built12 = [{**e, "subject": S12} for e in built]
graded12 = [{**e, "subject": S12} for e in graded]
reviewed12 = [{**e, "subject": S12} for e in reviewed]
shipped12 = [{"ts": stamp(9), "type": "shipped", "subject": S12}]
ac12 = [{"ts": stamp(10), "type": "owed-ac", "subject": S12, "criterion": "AC1", "wake_at": stamp(9)}]
met12 = [{"ts": stamp(5), "type": "owed-met", "subject": S12, "criterion": "AC1", "evidence": "checked"}]
ev12 = ledger(**{"L-builder-01.jsonl": built12, "L-grader-01.jsonl": graded12,
                "L-reviewer-01.jsonl": reviewed12,
                "L-executor-01.jsonl": shipped12 + met12,
                "L-spec-writer-01.jsonl": ac12})
assert ev12[1][S12]["state"] == "accepted", ev12[1][S12]["state"]

redate12 = [{"ts": stamp(1), "type": "owed-ac", "subject": S12, "criterion": "AC1",
            "wake_at": stamp(-5), "actor": "executor"}]
ev12b = ledger(**{"L-builder-01.jsonl": built12, "L-grader-01.jsonl": graded12,
                 "L-reviewer-01.jsonl": reviewed12,
                 "L-executor-01.jsonl": shipped12 + met12 + redate12,
                 "L-spec-writer-01.jsonl": ac12})
assert ev12b[1][S12]["state"] == "shipped-owed-evidence", ev12b[1][S12]["state"]
print("AC12 ok")

# ── AC13 · owed.checks() returns datetime instances; fold.owed_due()
# serializes with .isoformat(timespec="seconds"), never str()'s space form ──
rows_ac13 = fold.owed.checks(shipped_early + due_ac1, fold.NOW)
assert isinstance(rows_ac13[0]["due_at"], datetime), rows_ac13
assert isinstance(rows_ac13[0]["shipped_at"], datetime), rows_ac13
due_rows13 = fold.owed_due({S: {"evs": shipped_early + due_ac1}})
assert isinstance(due_rows13[0]["due_at"], str) and " " not in due_rows13[0]["due_at"], due_rows13
assert datetime.fromisoformat(due_rows13[0]["due_at"]) == rows_ac13[0]["due_at"], \
    (due_rows13[0]["due_at"], rows_ac13[0]["due_at"])
assert due_rows13[0]["due_at"] == rows_ac13[0]["due_at"].isoformat(timespec="seconds"), due_rows13
print("AC13 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0410 · board-deterministic-order (L-charter-0040 R5)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · fold.read_events()'s comparator ───────────────────────────────────
T410 = stamp(0)
E410_1 = {"ts": T410, "type": "spec-written", "subject": "L-spec-9410a"}
E410_2 = {"ts": T410, "type": "spec-written", "subject": "L-spec-9410b"}

# (a) cross-file: content-driven order, never filename-driven. Two layouts —
# "both events in one file" and "one event per file, reverse-alphabetical
# file-name pairing" — must agree, driven only by (subject, type, json).
ev_410a = ledger(**{"L-operator-410a.jsonl": [E410_1, E410_2]})[0]
order_410a = [e["subject"] for e in ev_410a if e.get("subject") in ("L-spec-9410a", "L-spec-9410b")]
assert order_410a == ["L-spec-9410a", "L-spec-9410b"], order_410a

ev_410b = ledger(**{"L-aaa-410.jsonl": [E410_2], "L-zzz-410.jsonl": [E410_1]})[0]
order_410b = [e["subject"] for e in ev_410b if e.get("subject") in ("L-spec-9410a", "L-spec-9410b")]
assert order_410b == order_410a, \
    f"★ AC1(a): cross-file same-ts order is content-driven, never filename-driven: {order_410b}"

# an unrelated third file, before AND after in glob order, must not perturb it
for extra_name410 in ("L-aaa-410x.jsonl", "L-zzz-410x.jsonl"):
    ev_410c = ledger(**{"L-aaa-410.jsonl": [E410_2], "L-zzz-410.jsonl": [E410_1],
                        extra_name410: [{"ts": stamp(9), "type": "observed", "subject": "unrelated-410"}]})[0]
    order_410c = [e["subject"] for e in ev_410c if e.get("subject") in ("L-spec-9410a", "L-spec-9410b")]
    assert order_410c == order_410a, (extra_name410, order_410c)

# (b) same-file causal order: a deploy-started immediately followed by a
# deploy-refused for the SAME subject at the IDENTICAL ts, same file — never
# reordered by content, though "deploy-refused" < "deploy-started"
# alphabetically (the exact phantom this fix closes).
DEP_SUBJ_410 = "deploy-9410dep"
dep_started_410 = {"ts": T410, "type": "deploy-started", "subject": DEP_SUBJ_410, "sha": "abc1234"}
dep_refused_410 = {"ts": T410, "type": "deploy-refused", "subject": DEP_SUBJ_410}
ev_410dep = ledger(**{"L-executor-410dep.jsonl": [dep_started_410, dep_refused_410]})[0]
assert fold.in_flight_deploys(ev_410dep) == [], \
    "★ AC1(b): same-file same-ts causal order preserved — no phantom in-flight deploy"

for extra_name410b in ("L-aaa-410dep.jsonl", "L-zzz-410dep.jsonl"):
    ev_410dep2 = ledger(**{"L-executor-410dep.jsonl": [dep_started_410, dep_refused_410],
                           extra_name410b: [{"ts": stamp(9), "type": "observed",
                                             "subject": "unrelated-410dep"}]})[0]
    assert fold.in_flight_deploys(ev_410dep2) == [], \
        f"★ AC1(b): an unrelated third file ({extra_name410b}) must not perturb same-file causal order"
print("AC1 ok")

# ── AC2 · fold.BLOCK_ORDER's key set; owed_due(specs) sorts by (due_at, spec,
# criterion) ─────────────────────────────────────────────────────────────────
assert set(fold.BLOCK_ORDER) == {
    "TRIAGE", "NEEDS YOU", "BLOCKED", "WRITTEN, NOT PICKED UP", "IN FLIGHT",
    "AWAITING VERIFICATION", "OWED EVIDENCE", "OWED DUE", "CHARTER CLOSE",
    "SHIPPED SINCE YOU LOOKED", "DECIDED WITHOUT YOU", "DEADLINE UNRESOLVABLE",
}, set(fold.BLOCK_ORDER)
assert all(isinstance(v, tuple) for v in fold.BLOCK_ORDER.values()), fold.BLOCK_ORDER

# owed_due(specs): three criteria (later/earlier/tied-with-a-different-spec),
# seeded across files in an order that CONTRADICTS the expected result.
ev_420, sp_420, _c420, _i420, _b420 = ledger(**{
    "L-spec-writer-9420z.jsonl": [{"ts": stamp(10), "type": "owed-ac", "subject": "L-spec-9420z",
                                   "criterion": "AC2", "wake_at": stamp(10)}],
    "L-executor-9420z.jsonl": [{"ts": stamp(5), "type": "shipped", "subject": "L-spec-9420z"}],
    "L-spec-writer-9420x.jsonl": [{"ts": stamp(10), "type": "owed-ac", "subject": "L-spec-9420x",
                                   "criterion": "AC1", "wake_at": stamp(7)}],
    "L-executor-9420x.jsonl": [{"ts": stamp(5), "type": "shipped", "subject": "L-spec-9420x"}],
    "L-spec-writer-9420y.jsonl": [{"ts": stamp(10), "type": "owed-ac", "subject": "L-spec-9420y",
                                   "criterion": "AC1", "wake_at": stamp(10)}],
    "L-executor-9420y.jsonl": [{"ts": stamp(5), "type": "shipped", "subject": "L-spec-9420y"}],
})
due_rows_420 = fold.owed_due(sp_420)
order_420 = [(r["spec"], r["criterion"]) for r in due_rows_420
            if r["spec"] in ("L-spec-9420x", "L-spec-9420y", "L-spec-9420z")]
# y and z tie on due_at (same ship/declare/wake offsets, different spec/criterion);
# x is less overdue (a later due_at) — ascending (due_at, spec, criterion) puts
# y before z (spec name) before x (due_at), regardless of specs-dict order.
assert order_420 == [("L-spec-9420y", "AC1"), ("L-spec-9420z", "AC2"), ("L-spec-9420x", "AC1")], order_420
print("AC2 ok")

# ── AC3 · fold.render() is byte-identical across two file-name layouts:
# same-file same-ts pairs keep their line order; the one cross-file same-ts
# pair reorders identically regardless of which file glob-sorts first ──────
AC3_S_WRITTEN, AC3_S_BUILDING, AC3_S_GRADED = "L-spec-9440a", "L-spec-9440b", "L-spec-9440c"
AC3_S_OWED_EV, AC3_S_OWED_DUE = "L-spec-9440d", "L-spec-9440e"
AC3_C_CLOSE = "L-charter-9440f"
# L-charter-0046/fold-proving-state (L-spec-0472): AC3_C_CLOSE (l1-complete,
# no specs, no sweep-fixpoint) now folds to proving, not L1-complete alone —
# it moved from CHARTER CLOSE to the new PROVING block. A second, genuinely
# `retracted` charter keeps CHARTER CLOSE itself populated (a retracted
# charter is decided BEFORE SD1's proving/reopened branch, so it is
# unaffected by this move) for the "every block populated" check below.
AC3_C_RETRACTED = "L-charter-9440n"
AC3_DEP_LIVE, AC3_DEP_AC1B = "deploy-9440-live", "deploy-9440-ac1b"
AC3_PAST = "2020-01-01T00:00:00Z"

triage_a3 = {"ts": stamp(4), "type": "brief", "subject": "triage-9440-a"}
triage_b3 = {"ts": stamp(4), "type": "brief", "subject": "triage-9440-b"}
needs_you_q1_3 = {"ts": stamp(4), "type": "question", "subject": "needsyou-9440-a",
                  "asks": "a?", "default": "d", "deadline": AC3_PAST}
needs_you_q2_3 = {"ts": stamp(4), "type": "question", "subject": "needsyou-9440-b",
                  "asks": "b?", "default": "d", "deadline": AC3_PAST}
dep_live_3 = {"ts": stamp(0), "type": "deploy-started", "subject": AC3_DEP_LIVE, "sha": "1111111"}
dep_ac1b_started_3 = {"ts": stamp(0), "type": "deploy-started", "subject": AC3_DEP_AC1B, "sha": "2222222"}
dep_ac1b_refused_3 = {"ts": stamp(0), "type": "deploy-refused", "subject": AC3_DEP_AC1B}


def _ac3_fixed_files():
    """Files that stay byte-identical, same name, same position, in BOTH
    layouts — no cross-file tie of their own, so their presence just proves
    every OTHER block still renders (and renders the SAME) while the one
    deliberately-varied pair below (TRIAGE) is what actually exercises the
    cross-file half of the fix."""
    return {
        "L-spec-writer-9440a.jsonl": [{"ts": stamp(3), "type": "spec-written", "subject": AC3_S_WRITTEN}],
        "L-spec-writer-9440b.jsonl": [{"ts": stamp(3), "type": "spec-written", "subject": AC3_S_BUILDING}],
        "L-builder-9440b.jsonl": [{"ts": stamp(2), "type": "build-started", "subject": AC3_S_BUILDING}],
        "L-spec-writer-9440c.jsonl": [{"ts": stamp(3), "type": "spec-written", "subject": AC3_S_GRADED}],
        "L-builder-9440c.jsonl": [{"ts": stamp(2), "type": "build-started", "subject": AC3_S_GRADED},
                                  {"ts": stamp(2), "type": "build-done", "subject": AC3_S_GRADED}],
        "L-spec-writer-9440d.jsonl": [{"ts": stamp(10), "type": "owed-ac", "subject": AC3_S_OWED_EV,
                                       "criterion": "AC1", "wake_at": stamp(-5)}],
        "L-executor-9440d.jsonl": [{"ts": stamp(5), "type": "shipped", "subject": AC3_S_OWED_EV}],
        "L-spec-writer-9440e.jsonl": [{"ts": stamp(10), "type": "owed-ac", "subject": AC3_S_OWED_DUE,
                                       "criterion": "AC1", "wake_at": stamp(7)}],
        "L-executor-9440e.jsonl": [{"ts": stamp(5), "type": "shipped", "subject": AC3_S_OWED_DUE}],
        "L-operator-9440f.jsonl": [{"ts": stamp(1), "type": "l1-complete", "subject": AC3_C_CLOSE}],
        "L-operator-9440n.jsonl": [{"ts": stamp(1), "type": "charter-retracted",
                                    "subject": AC3_C_RETRACTED}],
        "L-operator-9440g.jsonl": [{"ts": stamp(2), "type": "blocked", "subject": "blocked-9440",
                                    "why": "waiting on x"}],
        "L-operator-9440h.jsonl": [needs_you_q1_3, needs_you_q2_3],          # SAME file, kept fixed
        "L-operator-9440i.jsonl": [{"ts": stamp(4), "type": "question", "subject": "deadunresolv-9440",
                                    "asks": "c?", "default": "d", "deadline": ""}],
        "L-operator-9440j.jsonl": [{"ts": stamp(6), "type": "observed", "subject": "op-9440"}],
        "L-executor-9440j.jsonl": [{"ts": stamp(1), "type": "shipped", "subject": "shipsince-9440"}],
        "L-operator-9440k.jsonl": [{"ts": stamp(1), "type": "decision", "subject": "decided-9440",
                                    "why": "ruling"}],
        "L-executor-9440-live.jsonl": [dep_live_3],
        "L-executor-9440-ac1b.jsonl": [dep_ac1b_started_3, dep_ac1b_refused_3],  # SAME file, kept fixed
    }


layout1_files = dict(_ac3_fixed_files())
layout1_files["L-thinker-9440-1.jsonl"] = [triage_a3]
layout1_files["L-thinker-9440-2.jsonl"] = [triage_b3]

layout2_files = dict(_ac3_fixed_files())
# the ONE deliberate cross-file same-ts perturbation: same two events, same
# two filenames, CONTENTS SWAPPED — glob order of the two files is unchanged
# ("…-1" still before "…-2"), but WHICH event physically sits in which file —
# what the OLD `_src`-keyed comparator was sensitive to — is reversed.
layout2_files["L-thinker-9440-1.jsonl"] = [triage_b3]
layout2_files["L-thinker-9440-2.jsonl"] = [triage_a3]

_saved_relay3, _saved_carry3 = sys.modules.get("relay", "‹absent›"), sys.modules.get("carry", "‹absent›")
try:
    relay_stub3 = types.ModuleType("relay")
    relay_stub3.waiting_lines = lambda e, r: ["PLANNER WAITING ON: no open charters"]
    relay_stub3.unserved = lambda events, root: [
        {"spawn": "L-builder-9440", "role": "builder", "subject": "L-spec-9440z",
         "age_min": 12.0, "status": "pending"}]
    sys.modules["relay"] = relay_stub3
    carry_stub3 = types.ModuleType("carry")
    carry_stub3.uncarried = lambda events: [
        {"source": "9440", "kind": "v4", "registered_at": "2026-09-01T00:00:00Z",
         "attempts": 0, "last_error": None}]
    sys.modules["carry"] = carry_stub3

    board_l1 = fold.render(*ledger(**layout1_files))
    board_l2 = fold.render(*ledger(**layout2_files))
finally:
    for _name3, _saved3 in (("relay", _saved_relay3), ("carry", _saved_carry3)):
        if _saved3 == "‹absent›":
            sys.modules.pop(_name3, None)
        else:
            sys.modules[_name3] = _saved3

# every one of the fourteen blocks actually populated — a byte-equal
# comparison of two EMPTY renders would prove nothing.
for _title3 in ("TRIAGE", "NEEDS YOU", "BLOCKED", "WRITTEN, NOT PICKED UP", "IN FLIGHT",
               "UNSERVED", "AWAITING VERIFICATION", "OWED EVIDENCE", "OWED DUE",
               "CHARTER CLOSE", "PROVING", "SHIPPED SINCE YOU LOOKED", "DECIDED WITHOUT YOU",
               "DEADLINE UNRESOLVABLE", "INBOUND"):
    header3 = [l for l in board_l1.splitlines() if l.startswith(f"## {_title3} (")][0]
    n3 = int(header3.split("(")[1].split(")")[0])
    assert n3 >= 1, f"★ AC3: {_title3} must carry at least one row in the fixture: {header3}"

if board_l1 != board_l2:
    l1_lines, l2_lines = board_l1.splitlines(), board_l2.splitlines()
    first_diff = next((i for i, (a3, b3) in enumerate(zip(l1_lines, l2_lines)) if a3 != b3), None)
    raise AssertionError(
        f"★ AC3: the two layouts must render byte-identically; first differing line "
        f"{first_diff}: {l1_lines[first_diff] if first_diff is not None else None!r} vs "
        f"{l2_lines[first_diff] if first_diff is not None else None!r}")
print("AC3 ok")

# ── AC6 · consumer order-safety beyond AC1(b)'s deploy scenario ────────────
# (a) owed_line's "wakes" pick, across a spec-writer declaration + a later
# executor re-date sharing an identical ts in DIFFERENT files. `L-executor-…`
# and `L-spec-writer-…` have a FIXED relative alphabetical order (role name
# dominates: 'e' < 's', whatever numeric suffix follows) — under the OLD
# `(ts, _src)` key the executor's file always sorted first, so `fold()`'s
# widening check (which requires an already-admitted spec-writer/spec-auditor
# owed-ac for the SAME criterion already in `by_subject[subj]`) would find
# nothing yet and drop the re-date — a real, silent loss, independent of
# content. The fix reads the SAME two events in CONTENT order instead:
# declare6 lacks "wake_at" and carries "line" (which sorts before "subject"
# alphabetically — true regardless of the actual values), so declare6's
# canonical JSON is ALWAYS smaller than redate6's — declare6 is processed
# first no matter which file glob-sorts first, `by_subject` already carries
# it when redate6 is processed, and the re-date is admitted.
AC6_S1 = "L-spec-9451"
T6a = stamp(4)
declare6 = {"ts": T6a, "type": "owed-ac", "subject": AC6_S1, "criterion": "AC1",
           "line": "no instant here"}
redate6 = {"ts": T6a, "type": "owed-ac", "subject": AC6_S1, "criterion": "AC1", "wake_at": stamp(-5)}
ship6 = {"ts": stamp(5), "type": "shipped", "subject": AC6_S1}

declare_key6 = fold.event_content_key({**declare6, "_src": "x", "actor": "spec-writer"})
redate_key6 = fold.event_content_key({**redate6, "_src": "y", "actor": "executor"})
assert declare_key6 < redate_key6, (declare_key6, redate_key6)

board6a = fold.render(*ledger(**{"L-executor-9451.jsonl": [redate6],
                                 "L-spec-writer-9451.jsonl": [declare6],
                                 "L-executor-9451b.jsonl": [ship6]}))
oe6a = board6a.split("## OWED EVIDENCE")[1].split("## OWED DUE")[0]
assert AC6_S1 in oe6a and f"wakes {stamp(-5)}" in oe6a, \
    f"★ AC6(a): the executor's re-date is admitted (content order, not filename order): {oe6a}"

# (b) open_escalations: the same-file causal-order guarantee applied beyond
# in_flight_deploys — an escalation-blocking immediately followed by a
# decision for the SAME subject at the IDENTICAL ts, SAME file: the decision
# (later in the file) wins, and NEEDS YOU shows no row for that subject.
AC6_S2 = "needsyou-9452"
T6b = stamp(3)
esc6 = {"ts": T6b, "type": "escalation-blocking", "subject": AC6_S2, "why": "why?",
       "default": "d", "deadline": "2099-01-01T00:00:00Z", "revert": "r"}
dec6 = {"ts": T6b, "type": "decision", "subject": AC6_S2, "why": "resolved"}
ev6b = ledger(**{"L-operator-9452.jsonl": [esc6, dec6]})[0]           # SAME file, escalation first
assert fold.open_questions(ev6b) == [], \
    "★ AC6(b): same-file causal order applies to open_escalations too — the later " \
    "same-file decision wins, no phantom NEEDS YOU row"
print("AC6 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0424 · ledger-vocabulary (L-charter-0042)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · fold.EMITS["autodispatched"] = {"tick"};
# fold.REQUIRED["autodispatched"] = ("spawn", "since") ───────────────────────
ev424_ad = {"type": "autodispatched", "subject": "L-spec-9401", "spawn": "S1",
            "since": "2026-01-01T00:00:00+00:00"}
assert fold.check_append(ev424_ad, "tick") is None, fold.check_append(ev424_ad, "tick")
for actor424 in ("executor", "operator", "builder"):
    assert fold.check_append(ev424_ad, actor424) is not None, \
        f"★ AC1: actor {actor424!r} must not be admitted for autodispatched"
for missing424 in (
        {k: v for k, v in ev424_ad.items() if k != "spawn"},
        {k: v for k, v in ev424_ad.items() if k != "since"},
        {k: v for k, v in ev424_ad.items() if k not in ("spawn", "since")}):
    assert fold.check_append(missing424, "tick") is not None, \
        f"★ AC1: {missing424} must refuse actor tick"
print("AC1 ok")

# ── AC2 · fold.EMITS["autodispatch-failed"] = {"tick"};
# fold.REQUIRED["autodispatch-failed"] = ("reason",) ─────────────────────────
ev424_adf = {"type": "autodispatch-failed", "subject": "L-spec-9401", "reason": "wave-blocked"}
assert fold.check_append(ev424_adf, "tick") is None, fold.check_append(ev424_adf, "tick")
for actor424b in ("executor", "operator", "builder"):
    assert fold.check_append(ev424_adf, actor424b) is not None, \
        f"★ AC2: actor {actor424b!r} must not be admitted for autodispatch-failed"
adf_no_reason = {k: v for k, v in ev424_adf.items() if k != "reason"}
assert fold.check_append(adf_no_reason, "tick") is not None, \
    "★ AC2: dropping reason must refuse actor tick"
print("AC2 ok")

# ── AC3 · fold.EMITS["supervisor-code"] = {"up"};
# fold.REQUIRED["supervisor-code"] = ("kind", "sha", "pid") ──────────────────
ev424_sc = {"type": "supervisor-code", "subject": "SUPERVISOR", "kind": "planner",
            "sha": "a" * 40, "pid": 123}
assert fold.check_append(ev424_sc, "up") is None, fold.check_append(ev424_sc, "up")
for actor424c in ("executor", "planner", "thinker"):
    assert fold.check_append(ev424_sc, actor424c) is not None, \
        f"★ AC3: actor {actor424c!r} must not be admitted for supervisor-code"
for drop424 in ("kind", "sha", "pid"):
    dropped424 = {k: v for k, v in ev424_sc.items() if k != drop424}
    assert fold.check_append(dropped424, "up") is not None, \
        f"★ AC3: dropping {drop424} must refuse actor up"
print("AC3 ok")

# ── AC4 · fold.EMITS["install-synced"] = {"tick"};
# fold.REQUIRED["install-synced"] = ("sha",) ─────────────────────────────────
ev424_is = {"type": "install-synced", "subject": "L-spec-9402", "sha": "b" * 40}
assert fold.check_append(ev424_is, "tick") is None, fold.check_append(ev424_is, "tick")
for actor424d in ("executor", "operator", "builder"):
    assert fold.check_append(ev424_is, actor424d) is not None, \
        f"★ AC4: actor {actor424d!r} must not be admitted for install-synced"
is_no_sha = {k: v for k, v in ev424_is.items() if k != "sha"}
assert fold.check_append(is_no_sha, "tick") is not None, \
    "★ AC4: dropping sha must refuse actor tick"
print("AC4 ok")

# ── AC5 · fold.EMITS["rejected-criterion"]/["criterion-cleared"] each gain
# "builder", on top of every actor already admitted (no regression) ─────────
ev424_rc = {"type": "rejected-criterion", "subject": "L-spec-9403",
            "criterion": "COMMIT-SHAPE", "why": "2 commits above base"}
assert fold.check_append(ev424_rc, "builder") is None, fold.check_append(ev424_rc, "builder")
for actor424e in ("grader", "reviewer", "executor"):
    assert fold.check_append(ev424_rc, actor424e) is None, \
        f"★ AC5: actor {actor424e!r} must still be admitted for rejected-criterion"
ev424_cc = {"type": "criterion-cleared", "subject": "L-spec-9403", "criterion": "COMMIT-SHAPE"}
assert fold.check_append(ev424_cc, "builder") is None, fold.check_append(ev424_cc, "builder")
for actor424f in ("grader", "reviewer"):
    assert fold.check_append(ev424_cc, actor424f) is None, \
        f"★ AC5: actor {actor424f!r} must still be admitted for criterion-cleared"
for actor424g in ("thinker", "operator"):
    assert fold.check_append(ev424_rc, actor424g) is not None, \
        f"★ AC5: actor {actor424g!r} must remain refused on rejected-criterion"
    assert fold.check_append(ev424_cc, actor424g) is not None, \
        f"★ AC5: actor {actor424g!r} must remain refused on criterion-cleared"
print("AC5 ok")

# ── AC6 · the criterion-scoped narrowing lives in fold(), proven through
# fold()'s own subject state, using ledger()'s existing (a) selfclear shape
# and (b) the new COMMIT-SHAPE carve-out ─────────────────────────────────────
S424 = "L-spec-9405"
selfclear424 = ledger(**{
    "L-grader-01.jsonl": [{"ts": stamp(1), "type": "rejected-criterion", "subject": S424,
                           "criterion": "AC1"}],
    "L-builder-01.jsonl": [{"ts": stamp(0), "type": "criterion-cleared", "subject": S424,
                            "criterion": "AC1"}]})
assert selfclear424[1][S424]["rejects"] == 1, \
    "★ AC6(a): a builder-authored criterion-cleared for a non-COMMIT-SHAPE " \
    "criterion is ignored — test_fold.py:99-106's selfclear case, re-asserted"
commitshape424 = ledger(**{
    "L-grader-01.jsonl": [{"ts": stamp(1), "type": "rejected-criterion", "subject": S424,
                           "criterion": "COMMIT-SHAPE"}],
    "L-builder-01.jsonl": [{"ts": stamp(0), "type": "criterion-cleared", "subject": S424,
                            "criterion": "COMMIT-SHAPE"}]})
assert commitshape424[1][S424]["rejects"] == 0, \
    "★ AC6(b): a builder-authored criterion-cleared for COMMIT-SHAPE survives " \
    "the fold and clears it (rework-base-pinned's SD7 need)"
print("AC6 ok")

# ── AC7 · fold.EMITS["grader-view-built"] = {"grader"};
# fold.REQUIRED["grader-view-built"] = ("view", "ready_sha") ─────────────────
ev424_gvb = {"type": "grader-view-built", "subject": "L-spec-9404", "view": "/scratch/grade/x",
             "ready_sha": "c" * 40}
assert fold.check_append(ev424_gvb, "grader") is None, fold.check_append(ev424_gvb, "grader")
for actor424h in ("tick", "executor", "builder"):
    assert fold.check_append(ev424_gvb, actor424h) is not None, \
        f"★ AC7: actor {actor424h!r} must not be admitted for grader-view-built"
for drop424b in ("view", "ready_sha"):
    dropped424b = {k: v for k, v in ev424_gvb.items() if k != drop424b}
    assert fold.check_append(dropped424b, "grader") is not None, \
        f"★ AC7: dropping {drop424b} must refuse actor grader"
print("AC7 ok")

# ── AC8 · fold.EMITS["grader-pane-started"] = {"tick"};
# fold.REQUIRED["grader-pane-started"] = ("pane", "spawn_ids") ───────────────
ev424_gps = {"type": "grader-pane-started", "subject": "PANE", "pane": "grader-3",
             "spawn_ids": "L-grader-0500,L-grader-0501"}
assert fold.check_append(ev424_gps, "tick") is None, fold.check_append(ev424_gps, "tick")
for actor424i in ("grader", "executor", "operator"):
    assert fold.check_append(ev424_gps, actor424i) is not None, \
        f"★ AC8: actor {actor424i!r} must not be admitted for grader-pane-started"
for drop424c in ("pane", "spawn_ids"):
    dropped424c = {k: v for k, v in ev424_gps.items() if k != drop424c}
    assert fold.check_append(dropped424c, "tick") is not None, \
        f"★ AC8: dropping {drop424c} must refuse actor tick"
print("AC8 ok")

# ── AC5 (L-spec-0429) · fold.render's first line reflects freeze.state(),
# following the file's existing `fold.SESSIONS = TMP / "sessions"` override
# pattern for `fold.freeze.FLAG` ─────────────────────────────────────────────
_FLAG429 = TMP / "freeze-flag-429"


def _render_with_flag429(text):
    _FLAG429.write_text(text)
    fold.freeze.FLAG = _FLAG429
    return fold.render(*ledger())


b429_cap = _render_with_flag429("frozen — cap 18:00Z")
l429_cap = b429_cap.splitlines()
assert l429_cap[0] == "MERGE FREEZE (building allowed) until 18:00Z · frozen — cap 18:00Z", l429_cap[0]
assert l429_cap[1] == "", l429_cap[1]
assert l429_cap[2].startswith("# board · "), l429_cap[2]

b429_nocap = _render_with_flag429("frozen for Codex #445")
l429_nocap = b429_nocap.splitlines()
assert l429_nocap[0] == "MERGE FREEZE (building allowed) until lifted by the Thinker · frozen for Codex #445", \
    l429_nocap[0]
assert l429_nocap[1] == ""
assert l429_nocap[2].startswith("# board · ")

_FLAG429.unlink()
fold.freeze.FLAG = _FLAG429                      # now points at an absent path
b429_absent = fold.render(*ledger())
assert b429_absent.splitlines()[0] == B_BEFORE_FREEZE.splitlines()[0], \
    (b429_absent.splitlines()[0], B_BEFORE_FREEZE.splitlines()[0])

b429_nl = _render_with_flag429("a\n## NEEDS YOU")
l429_nl = b429_nl.splitlines()
assert l429_nl[0] == "MERGE FREEZE (building allowed) until lifted by the Thinker · a ## NEEDS YOU", l429_nl[0]
assert l429_nl[1] == ""
assert l429_nl[2].startswith("# board · ")
assert sum(1 for l in l429_nl if l.startswith("MERGE FREEZE")) == 1, \
    "the embedded newline must not forge a second physical freeze line"

fold.freeze.FLAG = freeze.FLAG                   # restore the module default
print("AC5 ok")

# ── AC9 (L-spec-0429) · no file outside this spec's Writes: gains a new
# reference to the flag's literal path, to DOIT_IGNORE_FREEZE, or to
# `import freeze` ────────────────────────────────────────────────────────────
_WRITES429 = {"src/freeze.py", "src/test_freeze.py", "src/merge_gate.py",
              "src/test_merge_gate.py", "src/fold.py", "src/test_fold.py"}
_SRC429 = pathlib.Path(__file__).parent
_FLAGPAT429 = re.compile(r"master-frozen-for-codex|DOIT_IGNORE_FREEZE")
_IMPPAT429 = re.compile(r"^import freeze", re.MULTILINE)
_flag_hits429, _imp_hits429 = set(), set()
for p429 in sorted(_SRC429.glob("*.py")):
    t429 = p429.read_text()
    if _FLAGPAT429.search(t429):
        _flag_hits429.add(f"src/{p429.name}")
    if _IMPPAT429.search(t429):
        _imp_hits429.add(f"src/{p429.name}")
assert _flag_hits429 <= _WRITES429, _flag_hits429 - _WRITES429
assert {"src/freeze.py", "src/merge_gate.py"} <= _flag_hits429, _flag_hits429
assert _imp_hits429 <= _WRITES429, _imp_hits429 - _WRITES429
assert {"src/fold.py", "src/merge_gate.py"} <= _imp_hits429, _imp_hits429
print("AC9 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0432 (L-charter-0042 R6) · fold.answered / overdue_questions /
# unresolvable_deadline_questions / open_questions
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1 · by-`ref`, cross-subject — the existing rule, unbroken by the refactor ──
ac1_ev, *_ = ledger(**{
    "L-builder-432a.jsonl": [
        {"ts": stamp(2), "type": "question", "subject": "L-spec-432-s1",
         "asks": "a?", "blocks": ["AC1"], "default": "d", "deadline": stamp(1)}],
    "L-operator-432a.jsonl": [
        {"ts": stamp(0), "type": "decision", "subject": "OTHER-432-a",
         "ref": "L-builder-432a.jsonl:1", "why": "answers Q by ref, no matching subject"}]})
q1_src = "L-builder-432a.jsonl:1"
assert q1_src in fold.answered(ac1_ev), fold.answered(ac1_ev)
assert not any(e["_src"] == q1_src for e in fold.overdue_questions(ac1_ev)), \
    fold.overdue_questions(ac1_ev)
print("answered-0432 AC1 ok")

# ── AC2 · same-subject, no `ref=`, decision strictly LATER — newly answered ──
ac2_ev, *_ = ledger(**{
    "L-builder-432b.jsonl": [
        {"ts": stamp(3), "type": "question", "subject": "L-spec-432-s2",
         "asks": "a?", "blocks": ["AC2"], "default": "d", "deadline": stamp(1)}],
    "L-operator-432b.jsonl": [
        {"ts": stamp(2), "type": "decision", "subject": "L-spec-432-s2", "why": "later, no ref"}]})
q2_src = "L-builder-432b.jsonl:1"
assert q2_src in fold.answered(ac2_ev), fold.answered(ac2_ev)
assert not any(e["_src"] == q2_src for e in fold.overdue_questions(ac2_ev)), \
    fold.overdue_questions(ac2_ev)
print("answered-0432 AC2 ok")

# ── AC3 · same shape, decision strictly EARLIER — still NOT answered ──────────
ac3_ev, *_ = ledger(**{
    "L-builder-432c.jsonl": [
        {"ts": stamp(1), "type": "question", "subject": "L-spec-432-s2b",
         "asks": "a?", "blocks": ["AC3"], "default": "d", "deadline": stamp(0.5)}],
    "L-operator-432c.jsonl": [
        {"ts": stamp(2), "type": "decision", "subject": "L-spec-432-s2b", "why": "earlier, no ref"}]})
q3_src = "L-builder-432c.jsonl:1"
assert q3_src not in fold.answered(ac3_ev), fold.answered(ac3_ev)
assert any(e["_src"] == q3_src for e in fold.overdue_questions(ac3_ev)), \
    fold.overdue_questions(ac3_ev)
print("answered-0432 AC3 ok")

# ── AC4 · unresolvable-deadline question, answered same-subject/no-ref ────────
ac4_ev, *_ = ledger(**{
    "L-builder-432d.jsonl": [
        {"ts": stamp(3), "type": "question", "subject": "L-spec-432-s3",
         "asks": "a?", "blocks": ["AC4"], "default": "d", "deadline": "sometime"}],
    "L-operator-432d.jsonl": [
        {"ts": stamp(2), "type": "decision", "subject": "L-spec-432-s3", "why": "later, no ref"}]})
q4_src = "L-builder-432d.jsonl:1"
assert not any(e["_src"] == q4_src for e in fold.unresolvable_deadline_questions(ac4_ev)), \
    fold.unresolvable_deadline_questions(ac4_ev)
print("answered-0432 AC4 ok")

# ── AC5 · two escalations same subject; a ref answers only the OLDER one ─────
ac5_ev, *_ = ledger(**{
    "L-executor-432e.jsonl": [
        {"ts": stamp(3), "type": "escalation-blocking", "subject": "L-spec-432-s4",
         "why": "e_old", "default": "d1", "deadline": stamp(-3), "revert": "r1"},
        {"ts": stamp(1), "type": "escalation-blocking", "subject": "L-spec-432-s4",
         "why": "e_new", "default": "d2", "deadline": stamp(-3), "revert": "r2"}],
    "L-operator-432e.jsonl": [
        {"ts": stamp(2), "type": "decision", "subject": "L-spec-432-s4",
         "ref": "L-executor-432e.jsonl:1", "why": "answers e_old by ref"}]})
e_old_src, e_new_src = "L-executor-432e.jsonl:1", "L-executor-432e.jsonl:2"
ans5 = fold.answered(ac5_ev)
assert e_old_src in ans5 and e_new_src not in ans5, ans5
esc_rows5 = [r for r in fold.open_questions(ac5_ev)
            if r["kind"] == "escalation" and r["subject"] == "L-spec-432-s4"]
assert len(esc_rows5) == 1 and esc_rows5[0]["src"] == e_new_src, esc_rows5
print("answered-0432 AC5 ok")

# ── AC6 · one escalation, no same-subject answer; a DIFFERENT subject's ref
# answers it — open_escalations still returns it, open_questions excludes it ──
ac6_ev, *_ = ledger(**{
    "L-executor-432f.jsonl": [
        {"ts": stamp(2), "type": "escalation-blocking", "subject": "L-spec-432-s5",
         "why": "E", "default": "d", "deadline": stamp(-3), "revert": "r"}],
    "L-operator-432f.jsonl": [
        {"ts": stamp(1), "type": "decision", "subject": "L-spec-432-s6",
         "ref": "L-executor-432f.jsonl:1", "why": "cross-subject ref answer"}]})
e_src6 = "L-executor-432f.jsonl:1"
assert e_src6 in fold.answered(ac6_ev), fold.answered(ac6_ev)
assert any(e["_src"] == e_src6 for e in fold.open_escalations(ac6_ev)), \
    "★ AC6: open_escalations itself must still return E, unfiltered"
assert not any(r["kind"] == "escalation" and r["subject"] == "L-spec-432-s5"
              for r in fold.open_questions(ac6_ev)), \
    "★ AC6: open_questions must exclude it via fold.answered, not via open_escalations"
print("answered-0432 AC6 ok")

# ── L-spec-0476/plain-core, R2 item 3 / AC7+AC8 · fold.SPEC_STATES/
# CHARTER_STATES completeness, proved by walking fold.py's own source with the
# `ast` module. Additive, changes no existing behavior: this only PROVES the
# two tuples above are supersets of what `spec_state`/`fold()` can actually
# return/assign — direct string returns, an `IfExp`'s literal branches
# (nested or not), and a `Name` bound by a `for state, marker in (...)` loop,
# for `spec_state`; a literal assigned to a `["state"]` subscript on a
# variable holding a charter dict, for `fold()`. A second, independent
# count/membership assertion guards against a walker that silently regresses
# to missing the `IfExp` shape (and so collects neither "closed-shipped" nor
# "closed-unbuilt") and would otherwise still pass the subset check with an
# empty offending set. ───────────────────────────────────────────────────────
import ast as _ast  # noqa: E402

_fold_src = pathlib.Path(fold.__file__).read_text()
_fold_tree = _ast.parse(_fold_src)
_spec_state_fn = next(n for n in _fold_tree.body
                       if isinstance(n, _ast.FunctionDef) and n.name == "spec_state")
_fold_fn = next(n for n in _fold_tree.body
                if isinstance(n, _ast.FunctionDef) and n.name == "fold")


def _loop_state_values(fn_node):
    """`state` -> the set of string literals fed to it by any
    `for state, marker in ((...), (...), ...)` loop inside `fn_node`."""
    values = {}
    for node in _ast.walk(fn_node):
        if not isinstance(node, _ast.For):
            continue
        target, it = node.target, node.iter
        if not (isinstance(target, _ast.Tuple) and len(target.elts) == 2
                and isinstance(target.elts[0], _ast.Name) and isinstance(it, _ast.Tuple)):
            continue
        found = set()
        for elt in it.elts:
            if (isinstance(elt, _ast.Tuple) and elt.elts
                    and isinstance(elt.elts[0], _ast.Constant)
                    and isinstance(elt.elts[0].value, str)):
                found.add(elt.elts[0].value)
        if found:
            values[target.elts[0].id] = found
    return values


def _expr_literals(expr, loop_vars):
    """String literals `expr` can evaluate to: a direct string `Constant`
    (a); an `IfExp`, recursing into both `body` and `orelse` so a chain of
    conditional returns is fully covered (b); a `Name` bound by a
    `for state, marker in (...)` loop, yielding every value that loop can
    bind it to (c)."""
    if expr is None:
        return set()
    if isinstance(expr, _ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, _ast.IfExp):
        return _expr_literals(expr.body, loop_vars) | _expr_literals(expr.orelse, loop_vars)
    if isinstance(expr, _ast.Name) and expr.id in loop_vars:
        return set(loop_vars[expr.id])
    return set()


def _return_literals(fn_node):
    loop_vars = _loop_state_values(fn_node)
    out = set()
    for node in _ast.walk(fn_node):
        if isinstance(node, _ast.Return):
            out |= _expr_literals(node.value, loop_vars)
    return out


_collected_spec_states = _return_literals(_spec_state_fn)
_offending_spec = _collected_spec_states - set(fold.SPEC_STATES)
assert not _offending_spec, f"spec_state can return {_offending_spec}, not in fold.SPEC_STATES"
assert len(_collected_spec_states) >= 15, _collected_spec_states
assert "closed-shipped" in _collected_spec_states, _collected_spec_states
assert "closed-unbuilt" in _collected_spec_states, _collected_spec_states


def _charter_state_literals(fn_node):
    out = set()
    for node in _ast.walk(fn_node):
        if not (isinstance(node, _ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, _ast.Subscript):
            continue
        sl = tgt.slice
        if not (isinstance(sl, _ast.Constant) and sl.value == "state"):
            continue
        if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
            out.add(node.value.value)
    return out


_collected_charter_states = _charter_state_literals(_fold_fn)
_offending_charter = _collected_charter_states - set(fold.CHARTER_STATES)
assert not _offending_charter, \
    f'a charter["state"] literal is {_offending_charter}, not in fold.CHARTER_STATES'
assert _collected_charter_states == {"retracted", "L2-complete", "L1-complete", "open"}, \
    _collected_charter_states
print("spec-states-ast ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0470 · ledger-vocabulary R1/R3/R4/R6 (L-charter-0046)
# ══════════════════════════════════════════════════════════════════════════════

# ── 0470-AC1 · fold.EMITS["charter-proving"] = {"tick"};
# fold.REQUIRED["charter-proving"] = ("reason", "deadline") ──────────────────
ev0470_1 = {"type": "charter-proving", "subject": "L-charter-9401", "reason": "entered",
            "deadline": "2026-10-05T00:00:00+00:00"}
assert fold.check_append(ev0470_1, "tick") is None, fold.check_append(ev0470_1, "tick")
for actor0470_1 in ("executor", "operator", "builder"):
    assert fold.check_append(ev0470_1, actor0470_1) is not None, \
        f"★ 0470-AC1: actor {actor0470_1!r} must not be admitted for charter-proving"
for missing0470_1 in (
        {k: v for k, v in ev0470_1.items() if k != "reason"},
        {k: v for k, v in ev0470_1.items() if k != "deadline"},
        {k: v for k, v in ev0470_1.items() if k not in ("reason", "deadline")}):
    assert fold.check_append(missing0470_1, "tick") is not None, \
        f"★ 0470-AC1: {missing0470_1} must refuse actor tick"
print("0470-AC1 ok")

# ── 0470-AC2 · fold.EMITS["charter-closed"] = {"tick"};
# fold.REQUIRED["charter-closed"] = ("reason",) ──────────────────────────────
ev0470_2 = {"type": "charter-closed", "subject": "L-charter-9401", "reason": "last check met"}
assert fold.check_append(ev0470_2, "tick") is None, fold.check_append(ev0470_2, "tick")
for actor0470_2 in ("executor", "operator", "builder"):
    assert fold.check_append(ev0470_2, actor0470_2) is not None, \
        f"★ 0470-AC2: actor {actor0470_2!r} must not be admitted for charter-closed"
missing0470_2 = {k: v for k, v in ev0470_2.items() if k != "reason"}
assert fold.check_append(missing0470_2, "tick") is not None, \
    "★ 0470-AC2: dropping reason must refuse actor tick"
print("0470-AC2 ok")

# ── 0470-AC3 · fold.EMITS["charter-reopened"] = {"tick"};
# fold.REQUIRED["charter-reopened"] = ("spec", "criterion", "failed_src") ────
ev0470_3 = {"type": "charter-reopened", "subject": "L-charter-9401", "spec": "L-spec-9402",
            "criterion": "AC3", "failed_src": "L-owed-sweeper-01.jsonl:9"}
assert fold.check_append(ev0470_3, "tick") is None, fold.check_append(ev0470_3, "tick")
for actor0470_3 in ("executor", "operator", "owed-sweeper"):
    assert fold.check_append(ev0470_3, actor0470_3) is not None, \
        f"★ 0470-AC3: actor {actor0470_3!r} must not be admitted for charter-reopened"
for drop0470_3 in ("spec", "criterion", "failed_src"):
    dropped0470_3 = {k: v for k, v in ev0470_3.items() if k != drop0470_3}
    assert fold.check_append(dropped0470_3, "tick") is not None, \
        f"★ 0470-AC3: dropping {drop0470_3} must refuse actor tick"
print("0470-AC3 ok")

# ── 0470-AC4 · fold.EMITS["owed-waived"] = {"operator", "thinker"};
# fold.REQUIRED["owed-waived"] = ("criterion", "reason") ─────────────────────
ev0470_4 = {"type": "owed-waived", "subject": "L-spec-9405", "criterion": "AC2",
            "reason": "manual review substituted"}
assert fold.check_append(ev0470_4, "operator") is None, fold.check_append(ev0470_4, "operator")
assert fold.check_append(ev0470_4, "thinker") is None, fold.check_append(ev0470_4, "thinker")
for actor0470_4 in ("executor", "grader", "builder"):
    assert fold.check_append(ev0470_4, actor0470_4) is not None, \
        f"★ 0470-AC4: actor {actor0470_4!r} must not be admitted for owed-waived"
for drop0470_4 in ("criterion", "reason"):
    dropped0470_4 = {k: v for k, v in ev0470_4.items() if k != drop0470_4}
    assert fold.check_append(dropped0470_4, "operator") is not None, \
        f"★ 0470-AC4: dropping {drop0470_4} must refuse actor operator"
print("0470-AC4 ok")

# ── 0470-AC4b · the mirror of AC10(b): a mis-authored owed-waived is inert
# end-to-end (via fold.fold(), not check_append alone) — using the existing
# built/graded(confirmed)/reviewed fixture plus one governing owed-ac
# (criterion AC1) plus one owed-waived for AC1 timestamped after it ─────────
S0470_4b = "L-spec-9407"
built0470_4b = [{**e, "subject": S0470_4b} for e in built]
graded0470_4b = [{**e, "subject": S0470_4b} for e in graded]
reviewed0470_4b = [{**e, "subject": S0470_4b} for e in reviewed]
shipped0470_4b = [{"ts": stamp(9), "type": "shipped", "subject": S0470_4b}]
ac0470_4b = [{"ts": stamp(10), "type": "owed-ac", "subject": S0470_4b, "criterion": "AC1",
             "wake_at": stamp(9)}]
waived0470_4b = {"ts": stamp(5), "type": "owed-waived", "subject": S0470_4b, "criterion": "AC1",
                 "reason": "manual review substituted"}

# authored "builder" — dropped by fold()'s admission loop, state NOT accepted
ev0470_4b_a = ledger(**{"L-builder-01.jsonl": built0470_4b + [waived0470_4b],
                        "L-grader-01.jsonl": graded0470_4b,
                        "L-reviewer-01.jsonl": reviewed0470_4b,
                        "L-executor-01.jsonl": shipped0470_4b,
                        "L-spec-writer-01.jsonl": ac0470_4b})
assert ev0470_4b_a[1][S0470_4b]["state"] != "accepted", ev0470_4b_a[1][S0470_4b]["state"]

# authored "grader" — also dropped, state NOT accepted
ev0470_4b_b = ledger(**{"L-builder-01.jsonl": built0470_4b,
                        "L-grader-01.jsonl": graded0470_4b + [waived0470_4b],
                        "L-reviewer-01.jsonl": reviewed0470_4b,
                        "L-executor-01.jsonl": shipped0470_4b,
                        "L-spec-writer-01.jsonl": ac0470_4b})
assert ev0470_4b_b[1][S0470_4b]["state"] != "accepted", ev0470_4b_b[1][S0470_4b]["state"]

# authored "operator" — admitted, state IS accepted
ev0470_4b_c = ledger(**{"L-builder-01.jsonl": built0470_4b,
                        "L-grader-01.jsonl": graded0470_4b,
                        "L-reviewer-01.jsonl": reviewed0470_4b,
                        "L-executor-01.jsonl": shipped0470_4b,
                        "L-spec-writer-01.jsonl": ac0470_4b,
                        "L-operator-01.jsonl": [waived0470_4b]})
assert ev0470_4b_c[1][S0470_4b]["state"] == "accepted", ev0470_4b_c[1][S0470_4b]["state"]
print("0470-AC4b ok")

# ── 0470-AC8 · fold.spec_state() counts a "waived" row exactly as "met" ─────
S0470_8 = "L-spec-9420"
shipped0470_8 = {"ts": stamp(9), "type": "shipped", "subject": S0470_8}
verdict0470_8 = {"ts": stamp(8), "type": "verdict", "subject": S0470_8, "confirmed": True}
review0470_8 = {"ts": stamp(8), "type": "review", "subject": S0470_8, "depth": "gates-only"}

# a single owed criterion, "waived" — accepted
ac0470_8_1 = {"ts": stamp(10), "type": "owed-ac", "subject": S0470_8, "criterion": "AC1",
             "wake_at": stamp(9)}
waived0470_8_1 = {"ts": stamp(5), "type": "owed-waived", "subject": S0470_8, "criterion": "AC1",
                  "reason": "manual review substituted"}
assert fold.spec_state([shipped0470_8, verdict0470_8, review0470_8, ac0470_8_1, waived0470_8_1],
                       set()) == "accepted"

# two owed criteria, one "met" one "waived" — accepted
ac0470_8_2a = {"ts": stamp(10), "type": "owed-ac", "subject": S0470_8, "criterion": "AC1",
              "wake_at": stamp(9)}
met0470_8_2a = {"ts": stamp(5), "type": "owed-met", "subject": S0470_8, "criterion": "AC1",
               "evidence": "e"}
ac0470_8_2b = {"ts": stamp(10), "type": "owed-ac", "subject": S0470_8, "criterion": "AC2",
              "wake_at": stamp(9)}
waived0470_8_2b = {"ts": stamp(5), "type": "owed-waived", "subject": S0470_8, "criterion": "AC2",
                   "reason": "manual review substituted"}
assert fold.spec_state([shipped0470_8, verdict0470_8, review0470_8, ac0470_8_2a, met0470_8_2a,
                        ac0470_8_2b, waived0470_8_2b], set()) == "accepted"

# one "waived" plus one still-"waiting" — the waiting row alone blocks accepted
ac0470_8_3a = {"ts": stamp(10), "type": "owed-ac", "subject": S0470_8, "criterion": "AC1",
              "wake_at": stamp(9)}
waived0470_8_3a = {"ts": stamp(5), "type": "owed-waived", "subject": S0470_8, "criterion": "AC1",
                   "reason": "manual review substituted"}
ac0470_8_3b = {"ts": stamp(10), "type": "owed-ac", "subject": S0470_8, "criterion": "AC2",
              "wake_at": stamp(-5)}
res0470_8_3 = fold.spec_state([shipped0470_8, verdict0470_8, review0470_8, ac0470_8_3a,
                               waived0470_8_3a, ac0470_8_3b], set())
assert res0470_8_3 == "shipped-owed-evidence", res0470_8_3

# separating case: shipped + verdict-confirmed, NO review event, one owed
# criterion "waived" — must return bare "shipped", never any shipped-owed-*
res0470_8_4 = fold.spec_state([shipped0470_8, verdict0470_8, ac0470_8_3a, waived0470_8_3a], set())
assert res0470_8_4 == "shipped", res0470_8_4
print("0470-AC8 ok")

# ── 0470-AC9 · fold.EMITS["kill-accepted"] = {"operator", "thinker"};
# fold.REQUIRED["kill-accepted"] = ("reason",) — subject always the killed spec
ev0470_9 = {"type": "kill-accepted", "subject": "L-spec-9406",
            "reason": "coverage moved to L-spec-9407 outside this charter"}
assert fold.check_append(ev0470_9, "operator") is None, fold.check_append(ev0470_9, "operator")
assert fold.check_append(ev0470_9, "thinker") is None, fold.check_append(ev0470_9, "thinker")
for actor0470_9 in ("executor", "builder", "spec-writer"):
    assert fold.check_append(ev0470_9, actor0470_9) is not None, \
        f"★ 0470-AC9: actor {actor0470_9!r} must not be admitted for kill-accepted"
missing0470_9 = {k: v for k, v in ev0470_9.items() if k != "reason"}
assert fold.check_append(missing0470_9, "operator") is not None, \
    "★ 0470-AC9: dropping reason must refuse actor operator"
print("0470-AC9 ok")

# ── 0470-AC10 · using the existing _kill_fixture271 harness — a spec-killed
# spec with NO superseded_by at all: (a) an admitted kill-accepted (actor
# operator) makes closable()["all_accepted"] True and l2_complete True for an
# otherwise-closable charter; (b) the identical fixture with the kill-accepted
# authored "builder" instead — dropped by fold()'s admission loop — leaves
# all_accepted False, unchanged from AC11's existing no-superseded-by baseline
def _kill_fixture0470(actor):
    extra0470 = dict(accepted_s2_files271)
    ka_ev0470 = {"ts": stamp(2), "type": "kill-accepted", "subject": S1_271,
                 "reason": "no replacement in charter"}
    extra0470[f"L-{actor}-09.jsonl"] = [ka_ev0470]
    return _kill_fixture271(extra0470, superseded_by=None)

ev0470_10a, specs0470_10a, charters0470_10a, _, _ = _kill_fixture0470("operator")
assert specs0470_10a[S1_271]["state"] == "killed", specs0470_10a[S1_271]["state"]
cl0470_10a = fold.closable(ev0470_10a, CID271)
assert cl0470_10a["all_accepted"] is True, cl0470_10a
assert fold.l2_complete(cl0470_10a) is True, cl0470_10a

ev0470_10b, *_ = _kill_fixture0470("builder")
cl0470_10b = fold.closable(ev0470_10b, CID271)
assert cl0470_10b["all_accepted"] is False, cl0470_10b
print("0470-AC10 ok")

# ── 0470-AC11 · fold.EMITS["charter-classified"] = {"executor", "operator"};
# fold.REQUIRED["charter-classified"] = ("klass", "reason") ─────────────────
ev0470_11 = {"type": "charter-classified", "subject": "L-charter-9401", "klass": "proving",
             "reason": "2 items remaining, next owed-check 2026-10-03"}
assert fold.check_append(ev0470_11, "executor") is None, fold.check_append(ev0470_11, "executor")
assert fold.check_append(ev0470_11, "operator") is None, fold.check_append(ev0470_11, "operator")
for actor0470_11 in ("tick", "thinker", "builder"):
    assert fold.check_append(ev0470_11, actor0470_11) is not None, \
        f"★ 0470-AC11: actor {actor0470_11!r} must not be admitted for charter-classified"
for drop0470_11 in ("klass", "reason"):
    dropped0470_11 = {k: v for k, v in ev0470_11.items() if k != drop0470_11}
    assert fold.check_append(dropped0470_11, "executor") is not None, \
        f"★ 0470-AC11: dropping {drop0470_11} must refuse actor executor"
print("0470-AC11 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0472 · fold-proving-state R1/R5 (L-charter-0046)
# ══════════════════════════════════════════════════════════════════════════════

# ── 0472-AC1 · l1-complete + one accepted spec -> "proving"; the identical
# charter with that spec merely "building" stays at L1-complete alone,
# c["proving"] is None; a charter-retracted event -> "retracted", c["proving"]
# is None regardless of any proving-shaped events also present ─────────────
S0472_1, C0472_1 = "L-spec-9410", "L-charter-9410"
built0472_1 = [{"ts": stamp(3), "type": "spec-written", "subject": S0472_1, "charter": C0472_1},
               {"ts": stamp(2), "type": "build-started", "subject": S0472_1},
               {"ts": stamp(2), "type": "build-done", "subject": S0472_1}]
graded0472_1 = [{"ts": stamp(1), "type": "verdict", "subject": S0472_1, "confirmed": True}]
reviewed0472_1 = [{"ts": stamp(1), "type": "review", "subject": S0472_1, "depth": "gates-only"}]
shipped0472_1 = [{"ts": stamp(0), "type": "shipped", "subject": S0472_1}]
l1_0472_1 = {"ts": stamp(0), "type": "l1-complete", "subject": C0472_1}

_, specs0472_1a, charters0472_1a, _, _ = ledger(**{
    "L-builder-9410.jsonl": built0472_1, "L-grader-9410.jsonl": graded0472_1,
    "L-reviewer-9410.jsonl": reviewed0472_1, "L-executor-9410.jsonl": shipped0472_1,
    "L-planner-9410.jsonl": [l1_0472_1]})
assert specs0472_1a[S0472_1]["state"] == "accepted", specs0472_1a[S0472_1]["state"]
assert charters0472_1a[C0472_1]["state"] == "proving", charters0472_1a[C0472_1]["state"]
p0472_1a = charters0472_1a[C0472_1]["proving"]
assert p0472_1a is not None and p0472_1a["id"] == C0472_1 and p0472_1a["phase"] == "proving", p0472_1a

_, specs0472_1b, charters0472_1b, _, _ = ledger(**{
    "L-builder-9410.jsonl": built0472_1[:2],           # spec-written + build-started only
    "L-planner-9410.jsonl": [l1_0472_1]})
assert specs0472_1b[S0472_1]["state"] == "building", specs0472_1b[S0472_1]["state"]
assert charters0472_1b[C0472_1]["state"] == "L1-complete", charters0472_1b[C0472_1]["state"]
assert charters0472_1b[C0472_1]["proving"] is None, charters0472_1b[C0472_1]["proving"]

_, _, charters0472_1c, _, _ = ledger(**{
    "L-builder-9410.jsonl": built0472_1, "L-grader-9410.jsonl": graded0472_1,
    "L-reviewer-9410.jsonl": reviewed0472_1, "L-executor-9410.jsonl": shipped0472_1,
    "L-planner-9410.jsonl": [l1_0472_1],
    "L-operator-9410b.jsonl": [{"ts": stamp(0), "type": "charter-retracted", "subject": C0472_1}],
    "L-tick-9410b.jsonl": [{"ts": stamp(0), "type": "charter-proving", "subject": C0472_1,
                           "reason": "entered", "deadline": stamp(-3)}]})
assert charters0472_1c[C0472_1]["state"] == "retracted", charters0472_1c[C0472_1]["state"]
assert charters0472_1c[C0472_1]["proving"] is None, charters0472_1c[C0472_1]["proving"]
print("0472-AC1 ok")

# ── 0472-AC2 · a charter-reopened newer than its newest charter-proving ->
# "reopened", c["proving"]["phase"] == "reopened" ────────────────────────────
proving0472_2 = {"ts": stamp(2), "type": "charter-proving", "subject": C0472_1,
                  "reason": "entered", "deadline": stamp(-3)}
reopened0472_2 = {"ts": stamp(1), "type": "charter-reopened", "subject": C0472_1,
                   "spec": S0472_1, "criterion": "AC1",
                   "failed_src": "L-owed-sweeper-9410.jsonl:1"}
_, _, charters0472_2, _, _ = ledger(**{
    "L-builder-9410.jsonl": built0472_1, "L-grader-9410.jsonl": graded0472_1,
    "L-reviewer-9410.jsonl": reviewed0472_1, "L-executor-9410.jsonl": shipped0472_1,
    "L-planner-9410.jsonl": [l1_0472_1],
    "L-tick-9410.jsonl": [proving0472_2, reopened0472_2]})
assert charters0472_2[C0472_1]["state"] == "reopened", charters0472_2[C0472_1]["state"]
assert charters0472_2[C0472_1]["proving"]["phase"] == "reopened", charters0472_2[C0472_1]["proving"]
print("0472-AC2 ok")

# ── 0472-AC3 · an L2-complete charter (L-charter-1101 shape: l1-complete,
# sweep-fixpoint, one accepted spec, covers: "none") stays L2-complete even
# carrying a charter-proving event — proving.phase() is never reached ───────
S0472_3, C0472_3 = "L-spec-9411", "L-charter-9411"
built0472_3 = [{"ts": stamp(3), "type": "spec-written", "subject": S0472_3, "charter": C0472_3},
               {"ts": stamp(2), "type": "build-started", "subject": S0472_3},
               {"ts": stamp(2), "type": "build-done", "subject": S0472_3}]
graded0472_3 = [{"ts": stamp(1), "type": "verdict", "subject": S0472_3, "confirmed": True}]
reviewed0472_3 = [{"ts": stamp(1), "type": "review", "subject": S0472_3, "depth": "gates-only"}]
shipped0472_3 = [{"ts": stamp(0), "type": "shipped", "subject": S0472_3},
                  {"ts": stamp(0), "type": "sweep-fixpoint", "subject": C0472_3}]
_, specs0472_3, charters0472_3, _, _ = ledger(**{
    "L-builder-9411.jsonl": built0472_3, "L-grader-9411.jsonl": graded0472_3,
    "L-reviewer-9411.jsonl": reviewed0472_3, "L-executor-9411.jsonl": shipped0472_3,
    "L-operator-9411.jsonl": [{"ts": stamp(3), "type": "charter-filed", "subject": C0472_3,
                               "covers": "none"},
                              {"ts": stamp(3), "type": "l1-complete", "subject": C0472_3}],
    "L-tick-9411.jsonl": [{"ts": stamp(0), "type": "charter-proving", "subject": C0472_3,
                           "reason": "entered", "deadline": stamp(-3)}]})
assert specs0472_3[S0472_3]["state"] == "accepted", specs0472_3[S0472_3]["state"]
assert charters0472_3[C0472_3]["state"] == "L2-complete", charters0472_3[C0472_3]["state"]
assert charters0472_3[C0472_3]["proving"] is None, charters0472_3[C0472_3]["proving"]
print("0472-AC3 ok")

# ── 0472-AC4 · set(c["proving"]) is exactly the nine SD6 keys; title reads
# the charter's own newest charter-filed title, falling back to the charter
# id when absent (0472-AC1a's fixture carries no charter-filed at all) ──────
assert set(p0472_1a) == {"id", "title", "phase", "label", "entered_at", "remaining",
                          "next", "deadline", "owner", "colour", "items"}, set(p0472_1a)
assert p0472_1a["title"] == C0472_1, p0472_1a["title"]

S0472_4, C0472_4 = "L-spec-9412", "L-charter-9412"
built0472_4 = [{"ts": stamp(3), "type": "spec-written", "subject": S0472_4, "charter": C0472_4},
               {"ts": stamp(2), "type": "build-started", "subject": S0472_4},
               {"ts": stamp(2), "type": "build-done", "subject": S0472_4}]
graded0472_4 = [{"ts": stamp(1), "type": "verdict", "subject": S0472_4, "confirmed": True}]
reviewed0472_4 = [{"ts": stamp(1), "type": "review", "subject": S0472_4, "depth": "gates-only"}]
shipped0472_4 = [{"ts": stamp(0), "type": "shipped", "subject": S0472_4}]
_, _, charters0472_4, _, _ = ledger(**{
    "L-builder-9412.jsonl": built0472_4, "L-grader-9412.jsonl": graded0472_4,
    "L-reviewer-9412.jsonl": reviewed0472_4, "L-executor-9412.jsonl": shipped0472_4,
    "L-operator-9412.jsonl": [{"ts": stamp(3), "type": "charter-filed", "subject": C0472_4,
                               "title": "widget onboarding"},
                              {"ts": stamp(3), "type": "l1-complete", "subject": C0472_4}]})
assert charters0472_4[C0472_4]["proving"]["title"] == "widget onboarding", \
    charters0472_4[C0472_4]["proving"]
print("0472-AC4 ok")

# ── 0472-AC5 · the board-lint AC1 above (unmodified) already proves this;
# re-asserted directly here with its own marker: PROVING is one of
# render()'s headers on every render, including an empty ledger ────────────
board0472_5 = fold.render(*ledger())
assert "## PROVING (0)" in board0472_5, board0472_5
assert titles271 == set(fold.BOARD_OWNERS), (titles271, set(fold.BOARD_OWNERS))
print("0472-AC5 ok")

# ── 0472-AC6 · one proving charter (one shipped-owed-due spec, no
# sweep-fixpoint, remaining==2) renders its PROVING row field-for-field; a
# second proving charter with a LATER deadline renders after the first ─────
S0472_6a, C0472_6a = "L-spec-9413", "L-charter-9413"
built0472_6a = [{"ts": stamp(3), "type": "spec-written", "subject": S0472_6a, "charter": C0472_6a},
                {"ts": stamp(2), "type": "build-started", "subject": S0472_6a},
                {"ts": stamp(2), "type": "build-done", "subject": S0472_6a}]
graded0472_6a = [{"ts": stamp(1), "type": "verdict", "subject": S0472_6a, "confirmed": True}]
reviewed0472_6a = [{"ts": stamp(1), "type": "review", "subject": S0472_6a, "depth": "gates-only"}]
shipped0472_6a = [{"ts": stamp(5), "type": "shipped", "subject": S0472_6a}]
due0472_6a = [{"ts": stamp(10), "type": "owed-ac", "subject": S0472_6a, "criterion": "AC1",
               "wake_at": stamp(7)}]
l1_0472_6a = {"ts": stamp(6), "type": "l1-complete", "subject": C0472_6a}

_, specs0472_6a, charters0472_6a, _, _ = ledger(**{
    "L-builder-9413.jsonl": built0472_6a, "L-grader-9413.jsonl": graded0472_6a,
    "L-reviewer-9413.jsonl": reviewed0472_6a, "L-executor-9413.jsonl": shipped0472_6a,
    "L-spec-writer-9413.jsonl": due0472_6a, "L-planner-9413.jsonl": [l1_0472_6a]})
assert specs0472_6a[S0472_6a]["state"] == "shipped-owed-due", specs0472_6a[S0472_6a]["state"]
assert charters0472_6a[C0472_6a]["state"] == "proving", charters0472_6a[C0472_6a]["state"]
p6a = charters0472_6a[C0472_6a]["proving"]
assert p6a["remaining"] == 2, p6a

board0472_6a = fold.render(*ledger(**{
    "L-builder-9413.jsonl": built0472_6a, "L-grader-9413.jsonl": graded0472_6a,
    "L-reviewer-9413.jsonl": reviewed0472_6a, "L-executor-9413.jsonl": shipped0472_6a,
    "L-spec-writer-9413.jsonl": due0472_6a, "L-planner-9413.jsonl": [l1_0472_6a]}))
row0472_6a = (f"{C0472_6a} · {p6a['title']} · {p6a['remaining']} left · "
              + (f"next {p6a['next']['kind']} {p6a['next']['due_at']}" if p6a["next"]
                 else "nothing remaining")
              + f" · deadline {p6a['deadline']} · owner {p6a['owner']} · {p6a['colour']}")
assert row0472_6a in board0472_6a, (row0472_6a, board0472_6a)

# a second proving charter, its one owed criterion RE-DATED (declared after
# ship) far into the future -> its own deadline (due_at + GRACE_DAYS) lands
# well after 0472_6a's (max(NOW, a 2-days-ago due) + GRACE_DAYS)
S0472_6b, C0472_6b = "L-spec-9414", "L-charter-9414"
built0472_6b = [{"ts": stamp(3), "type": "spec-written", "subject": S0472_6b, "charter": C0472_6b},
                {"ts": stamp(2), "type": "build-started", "subject": S0472_6b},
                {"ts": stamp(2), "type": "build-done", "subject": S0472_6b}]
graded0472_6b = [{"ts": stamp(1), "type": "verdict", "subject": S0472_6b, "confirmed": True}]
reviewed0472_6b = [{"ts": stamp(1), "type": "review", "subject": S0472_6b, "depth": "gates-only"}]
shipped0472_6b = [{"ts": stamp(0), "type": "shipped", "subject": S0472_6b}]
due0472_6b = [{"ts": stamp(-1), "type": "owed-ac", "subject": S0472_6b, "criterion": "AC1",
               "wake_at": stamp(-400)}]                        # a re-date: due_at == wake_at
l1_0472_6b = {"ts": stamp(0), "type": "l1-complete", "subject": C0472_6b}

board0472_6b = fold.render(*ledger(**{
    "L-builder-9413.jsonl": built0472_6a, "L-grader-9413.jsonl": graded0472_6a,
    "L-reviewer-9413.jsonl": reviewed0472_6a, "L-executor-9413.jsonl": shipped0472_6a,
    "L-spec-writer-9413.jsonl": due0472_6a, "L-planner-9413.jsonl": [l1_0472_6a],
    "L-builder-9414.jsonl": built0472_6b, "L-grader-9414.jsonl": graded0472_6b,
    "L-reviewer-9414.jsonl": reviewed0472_6b, "L-executor-9414.jsonl": shipped0472_6b,
    "L-spec-writer-9414.jsonl": due0472_6b, "L-planner-9414.jsonl": [l1_0472_6b]}))
lines0472_6b = board0472_6b.splitlines()
rows0472_6a = [i for i, ln in enumerate(lines0472_6b) if ln.strip().startswith(f"{C0472_6a} ·")]
rows0472_6b = [i for i, ln in enumerate(lines0472_6b) if ln.strip().startswith(f"{C0472_6b} ·")]
assert len(rows0472_6a) == 1 and len(rows0472_6b) == 1, (rows0472_6a, rows0472_6b, board0472_6b)
assert rows0472_6a[0] < rows0472_6b[0], \
    "the later-deadline charter renders after the earlier one"
print("0472-AC6 ok")

# ── 0472-AC12 · a proving charter's own closed-unbuilt spec still renders
# its suffix — now on the new PROVING row, not CHARTER CLOSE (D112 survives
# SD2's move); a proving charter with c["unbuilt"] == 0 renders no suffix ──
board0472_12 = fold.render(*closed)
prov_line0472_12 = next(line for line in board0472_12.splitlines()
                        if line.strip().startswith(C + " ·"))
assert prov_line0472_12.strip().endswith("1 closed unbuilt"), prov_line0472_12
assert not row0472_6a.endswith("closed unbuilt"), row0472_6a
print("0472-AC12 ok")

# ── 0472-AC13 · no bare state-literal comparison in test_fold.py/test_tick.py
# was left unmigrated by SD2 (fix 1's own regression); `fold.closable` (the
# real one, `fold.py`'s own `def closable`) is never left deleted by the
# removed test_tick.py stub sub-block.
#
# The needle is built from character codes, never written as a source
# literal, so this very assertion cannot self-match. Pinned to 3, not the
# spec's own stale 1 (written against `base_sha` 462a7ad, before
# `ledger-vocabulary`'s AST-completeness literal at "spec-states-ast" landed,
# and before this unit's own AC1 needed one legitimate comparison of its
# own) — the three genuine sources, enumerated so a fourth is never silent:
# test_fold.py's own line 117 (S `written`, never BUILD_DONE — untouched by
# SD2, per R5's Target), the AST-lint completeness set two sections up, and
# 0472-AC1's own "building" sub-case just above ─────────────────────────────
_needle0472 = chr(34) + "".join(chr(c) for c in
                                (76, 49, 45, 99, 111, 109, 112, 108, 101, 116, 101)) + chr(34)
_tf_text0472 = pathlib.Path(__file__).read_text()
_tt_text0472 = (pathlib.Path(__file__).parent / "test_tick.py").read_text()
assert _tf_text0472.count(_needle0472) == 3, _tf_text0472.count(_needle0472)
assert _tt_text0472.count(_needle0472) == 0, _tt_text0472.count(_needle0472)
assert getattr(fold, "closable", None) is not None, \
    "the real fold.closable must never be left deleted"
print("0472-AC13 ok")

# ═══════════════════════ L-spec-0481 AC15 — capability_holds / held_specs ══════════
assert fold.EMITS["grading-preflight-failed"] == {"grader", "reviewer"}
assert fold.EMITS["capability-hold"] == {"grader", "reviewer"}
assert fold.REQUIRED["grading-preflight-failed"] == ("capability",)
assert fold.REQUIRED["capability-hold"] == ("capability", "spec")
assert fold.required_reason({"type": "grading-preflight-failed", "subject": "x"}) is not None
assert fold.required_reason({"type": "capability-hold", "subject": "x", "capability": "tools"}) is not None
assert fold.required_reason(
    {"type": "capability-hold", "subject": "x", "capability": "tools", "spec": "L-spec-9701"}) is None
print("AC15 EMITS/REQUIRED ok")

AC15_SPECS_DIR = TMP / "content-ac15"
AC15_SPECS_DIR.mkdir(parents=True, exist_ok=True)
(AC15_SPECS_DIR / "L-spec-9703.md").write_text("# fixture\n\nnothing special here.\n")
(AC15_SPECS_DIR / "verify-L-spec-9703-grader.sh").write_text("npm run test\n")   # needs node_modules
(AC15_SPECS_DIR / "L-spec-9704.md").write_text("# fixture\n\nnothing special here.\n")
(AC15_SPECS_DIR / "verify-L-spec-9704-grader.sh").write_text("echo fine\n")      # needs nothing

ev15, *_ = ledger(**{
    "L-grader-9701.jsonl": [
        # (a) shared shape: capability:node_modules, triggered by L-spec-9703's own preflight
        {"ts": stamp(2), "type": "capability-hold", "subject": "capability:node_modules",
         "capability": "node_modules", "spec": "L-spec-9703", "source": "preflight"},
        # (b) spec-local shape: capability:view-paths:L-spec-9701 (no content on disk at all)
        {"ts": stamp(2), "type": "capability-hold", "subject": "capability:view-paths:L-spec-9701",
         "capability": "view-paths", "spec": "L-spec-9701", "source": "preflight"},
        # (c) unknown per-spec shape
        {"ts": stamp(2), "type": "capability-hold", "subject": "capability:unknown:L-spec-9702",
         "capability": "unknown", "spec": "L-spec-9702", "source": "preflight"},
    ],
    "L-builder-0001.jsonl": [
        # a non-thinker/operator close of the spec-local hold — must NOT close it
        {"ts": stamp(1), "type": "unblocked", "subject": "capability:view-paths:L-spec-9701"},
    ],
})
holds15 = fold.capability_holds(ev15)
assert set(holds15) == {"capability:node_modules", "capability:view-paths:L-spec-9701",
                         "capability:unknown:L-spec-9702"}, holds15
assert holds15["capability:node_modules"]["capability"] == "node_modules", holds15
assert holds15["capability:view-paths:L-spec-9701"]["specs"] == {"L-spec-9701"}, holds15
print("AC15 three-shape/open ok")

held15 = fold.held_specs(ev15, content_dir=AC15_SPECS_DIR, project_of=lambda s: "testproj")
assert "L-spec-9703" in held15, held15          # shared hold, needs node_modules
assert "L-spec-9704" not in held15, held15      # shared hold, does NOT need node_modules
assert "L-spec-9701" in held15, held15          # spec-local, no content — held anyway
assert "L-spec-9702" in held15, held15          # unknown per-spec — held anyway
assert not ({"L-spec-9702"} & fold.held_specs(
    [e for e in ev15 if e.get("subject") != "capability:unknown:L-spec-9702"],
    content_dir=AC15_SPECS_DIR, project_of=lambda s: "testproj")), \
    "a per-spec hold must never cover another spec"
print("AC15 held_specs/missing-content ok")

# thinker closes the shared hold — it must disappear
ev15b, *_ = ledger(**{
    "L-grader-9701.jsonl": [
        {"ts": stamp(2), "type": "capability-hold", "subject": "capability:node_modules",
         "capability": "node_modules", "spec": "L-spec-9703", "source": "preflight"},
    ],
    "L-thinker-0001.jsonl": [
        {"ts": stamp(1), "type": "unblocked", "subject": "capability:node_modules"},
    ],
})
assert "capability:node_modules" not in fold.capability_holds(ev15b), fold.capability_holds(ev15b)
# the spec-local hold from ev15, closed only by a non-thinker/operator actor, stays open
assert "capability:view-paths:L-spec-9701" in holds15, holds15
print("AC15 actor-gated close ok")
