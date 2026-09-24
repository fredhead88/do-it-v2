#!/usr/bin/env python3
"""One runnable check on the relay's fold queries. Run: python3 test_relay.py

Every check is a fixture ledger under its own temp root, so no scenario can inherit
another's charters — the count throttle is a system-wide tally and a shared ledger
would make every later assertion depend on the order of the earlier ones.

★ `DOIT_PROJECT` is set HERE, before `import fold`, and every fixture event carries
that same value. `fold.PROJECT` is read at import (fold.py) and `read_events()` then
drops every event without a matching `project`, so a suite that leaves it to the
operator's shell passes in one pane and fails in another — which is exactly what
test_fold.py does today (measured 2026-09-17). The Verification command runs this
file twice, once with the variable cleared and once with it set to something no
fixture uses, and both runs must print the same count.
"""
import datetime, json, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
PROJECT = "relay-fixture"
os.environ["DOIT_ROOT"], os.environ["DOIT_PROJECT"] = str(TMP), PROJECT
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fold, relay  # noqa: E402

N = 0
CLOCK = [0]


def ok(cond, msg):
    global N
    assert cond, msg
    N += 1


def scene(name):
    """A fresh root: its own events/, content/ and seat/, and fold re-pointed at it."""
    d = TMP / name
    for sub in ("events", "content", "seat"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    fold.ROOT, fold.EVENTS = d, d / "events"
    return d


def ev(d, actor_file, type_, subject, **kw):
    """One event, appended to the file whose NAME decides the actor (D90)."""
    CLOCK[0] += 1
    when = (fold.NOW - datetime.timedelta(minutes=100_000 - CLOCK[0])).isoformat(timespec="seconds")
    p = d / "events" / f"{actor_file}.jsonl"
    with open(p, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": when, "type": type_, "subject": subject,
                             "project": PROJECT, **kw}, sort_keys=True) + "\n")


read = lambda: fold.read_events()
TAIL = " (order keys unavailable: goal_date, set_order)"

# ── AC1(a): an unmet `after:` excludes, and names the charter it is waiting on ──
d = scene("after")
ev(d, "L-operator-a", "charter-filed", "L-charter-0001", covers="none")
ev(d, "L-operator-a", "charter-filed", "L-charter-0002", covers="none", after="L-charter-0001")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0001"], f"the ungated charter is ready: {ready}")
ok(dict(waiting) == {"L-charter-0002": "after: L-charter-0001 has not landed" + TAIL},
   f"an unmet after: excludes and names it, with the keys it could not order on: {waiting}")
# AC4: the one-ready-one-waiting shape, and the same list twice.
lines = relay.waiting_lines(read(), d)
ok(lines[0] == "PLANNER WAITING ON" and lines[1] ==
   "  L-charter-0002 · after: L-charter-0001 has not landed" + TAIL, lines)
ok(all(x.startswith("note:") for x in lines[2:]) and lines == relay.waiting_lines(read(), d),
   f"notes follow the waiting lines, and two calls agree: {lines}")
# and `after:` is met once every spec the named charter owns is accepted.
ev(d, "L-spec-writer-0001", "spec-written", "L-spec-0001", charter="L-charter-0001", footprint=["src/a.py"])
ev(d, "L-executor-0001", "shipped", "L-spec-0001", charter="L-charter-0001")
ev(d, "L-grader-0001", "verdict", "L-spec-0001", confirmed=True)
ev(d, "L-reviewer-0001", "review", "L-spec-0001")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0001", "L-charter-0002"] and not waiting,
   f"a landed after: gates nothing: {ready} {waiting}")

# ── AC1(b): a `conflicts:` charter holding a building spec, and an unknown id ──
d = scene("conflicts")
ev(d, "L-operator-b", "charter-filed", "L-charter-0001", covers="none")
ev(d, "L-operator-b", "charter-filed", "L-charter-0002", covers="none", conflicts="L-charter-0001")
ev(d, "L-operator-b", "charter-filed", "L-charter-0003", covers="none", after="L-charter-0099")
ev(d, "L-spec-writer-0002", "spec-written", "L-spec-0005", charter="L-charter-0001", footprint=["src/x.py"])
ev(d, "L-builder-0002", "build-started", "L-spec-0005", charter="L-charter-0001")
ready, waiting = relay.plannable(read(), d)
w = dict(waiting)
ok(ready == ["L-charter-0001"], f"the conflicting charter itself is untouched: {ready}")
ok(w["L-charter-0002"] == "conflicts: L-charter-0001 holds L-spec-0005 (building)" + TAIL, w)
ok(w["L-charter-0003"] == "after: L-charter-0099 has not landed" + TAIL,
   "a charter id with no charter-filed anywhere is not landed, so the gate holds")
ok(relay.sequencing({"after": "L-charter-0099"}, read())["unknown"] == ["L-charter-0099"],
   "and it is disclosed as unknown rather than silently gating nothing")

# ── AC1(c)+(e): the count throttle, and the alongside-only block that skips it ──
d = scene("throttle")
for cid in ("L-charter-0010", "L-charter-0011"):
    ev(d, "L-operator-c", "charter-filed", cid, covers="none")
    ev(d, "L-planner-0001", "cut-written", cid)
ev(d, "L-operator-c", "charter-filed", "L-charter-0020", covers="none")
ev(d, "L-operator-c", "charter-filed", "L-charter-0021", covers="none", alongside="L-charter-0010")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0021"], f"a declared block skips the throttle entirely (R4, Plan decision 6): {ready}")
ok(dict(waiting) == {"L-charter-0020": "count throttle: L-charter-0010 not landed" + TAIL},
   f"blockless, with two cut-and-unlanded charters: the throttle, naming the lowest id: {waiting}")
ok(relay.sequencing({"alongside": "L-charter-0010"}, read()) is not None
   and relay.sequencing({"covers": "none"}, read()) is None,
   "alongside-only is a declared block; no block at all is None — the throttle reads the difference")

d = scene("throttle-one")
ev(d, "L-operator-d", "charter-filed", "L-charter-0010", covers="none")
ev(d, "L-planner-0002", "cut-written", "L-charter-0010")
ev(d, "L-operator-d", "charter-filed", "L-charter-0020", covers="none")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0020"] and not waiting,
   f"one unlanded cut is under the count: a blockless charter is not excluded: {ready} {waiting}")

# ── AC1(d): a standing escalation excludes regardless of every other check ──
d = scene("escalation")
ev(d, "L-operator-e", "charter-filed", "L-charter-0001", covers="none")
ev(d, "L-executor-0003", "escalation-blocking", "L-charter-0001", why="no repo link")
ready, waiting = relay.plannable(read(), d)
ok(ready == [] and dict(waiting)["L-charter-0001"] == "escalation open: no repo link" + TAIL,
   f"a standing escalation is the operator's, not the queue's: {waiting}")
ev(d, "L-operator-e", "decision", "L-charter-0001", why="linked", revert="rm")
ready, _ = relay.plannable(read(), d)
ok(ready == ["L-charter-0001"], "a decision after it returns the charter to the queue (the tick's own rule)")

# ── AC2(a)+(e): the goal-date key, read off the newest goal file only ──
d = scene("goal-date")
goal = d / "content" / "L-goal-0001.md"
goal.write_text("# a goal\n\ndate: 2026-09-14\n\n## Requirements\n- G1: the first thing\n- G2: the second\n")
ev(d, "L-thinker-0001", "goal-filed", "L-goal-0001", path=str(goal))     # no `date` field: the file is the fallback
ev(d, "L-operator-f", "charter-filed", "L-charter-0001", covers="none")
ev(d, "L-operator-f", "charter-filed", "L-charter-0002", covers="G1")
ev(d, "L-operator-f", "charter-filed", "L-charter-0003", covers="G2", after="L-charter-0099")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0002", "L-charter-0001"],
   f"the dated charter precedes the undated one, lower id second: {ready}")
ok(dict(waiting)["L-charter-0003"].endswith(" (order keys unavailable: set_order)"),
   f"a charter that DID get a goal_date discloses only the key that was unavailable: {waiting}")

d = scene("id-sort")
for cid in ("L-charter-0003", "L-charter-0001", "L-charter-0002"):
    ev(d, "L-operator-g", "charter-filed", cid, covers="none")
ready, _ = relay.plannable(read(), d)
ok(ready == ["L-charter-0001", "L-charter-0002", "L-charter-0003"],
   f"no date anywhere: ascending by charter id, and no None ever compared to a str: {ready}")

# ── AC2(c)+(d): the footprint intersection, and the skip that is disclosed ──
d = scene("footprint")
cut = d / "content" / "cut-L-charter-0002.md"
cut.write_text("# cut-L-charter-0002\n\n## one-unit\nGoal: something\nFootprint: src/relay.py src/fold.py\nWave: 1\n")
ev(d, "L-operator-h", "charter-filed", "L-charter-0001", covers="none")
ev(d, "L-operator-h", "charter-filed", "L-charter-0002", covers="none")
ev(d, "L-spec-writer-0003", "spec-written", "L-spec-0007", charter="L-charter-0001",
   footprint=["src/other.py", "src/fold.py"])
ok(relay.cut_footprint("L-charter-0002", d) == ["src/fold.py", "src/relay.py"],
   "the cut's units are parsed by audit.units, never by a second parser")
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0001"] and dict(waiting)["L-charter-0002"] ==
   "footprint: L-spec-0007 (written) holds src/fold.py" + TAIL,
   f"a written spec of ANOTHER charter holding a path in the cut excludes it: {waiting}")
note = "note: footprint check unmeasured for L-charter-0002 (no content/cut-L-charter-0002.md)"
ok(note not in relay.waiting_lines(read(), d), "a check that RAN is never disclosed as unmeasured")
cut.unlink()
ready, waiting = relay.plannable(read(), d)
ok(ready == ["L-charter-0001", "L-charter-0002"] and not waiting,
   f"no cut on disk: the check is skipped, never failed: {ready}")
ok(note in relay.waiting_lines(read(), d),
   f"and skipping it is disclosed, ready or not: {relay.waiting_lines(read(), d)}")

# ── AC4: the dry queue ──
d = scene("dry")
ok(relay.waiting_lines(read(), d) == ["PLANNER WAITING ON: no open charters"],
   "zero open charters is a measurement and renders as one line, never as an empty list")

# ── AC3: sequencing, both source forms ──
evs = [{"type": "charter-filed", "subject": "L-charter-0001"},
       {"type": "charter-filed", "subject": "L-charter-0002"}]
want = {"after": ["L-charter-0001"], "alongside": [], "conflicts": ["L-charter-0002"], "unknown": []}
body = "# a charter\n\nsome prose\n- after: L-charter-0001\n- conflicts: L-charter-0002\n\n## Intent\n"
ok(relay.sequencing({"after": "L-charter-0001", "conflicts": "L-charter-0002"}, evs) == want,
   "the event form: values the fold hands back as strings")
ok(relay.sequencing(body, evs) == want, "the raw-charter-body form returns the identical dict")
ok(relay.sequencing({"after": ["L-charter-0001"], "conflicts": ["L-charter-0002"]}, evs) == want,
   "and so does a JSON array, which is the other shape the ledger may carry")
ok(relay.sequencing({"title": "x"}, evs) is None and relay.sequencing("# c\n\nno block here\n", evs) is None,
   "no block at all is None in both forms — never an empty block")
ok(relay.sequencing({"after": ""}, evs) == {"after": [], "alongside": [], "conflicts": [], "unknown": []},
   "a declared-but-empty block is NOT None: it declared something, so the throttle stays off")
self_ref = relay.sequencing({"conflicts": "L-charter-0002 L-charter-0404"}, evs)
ok(self_ref["conflicts"] == ["L-charter-0002", "L-charter-0404"] and self_ref["unknown"] == ["L-charter-0404"],
   "a known id is never reported unknown; an unfiled one always is")

# ── AC5: the attempt tally ──
C = "L-charter-0007"
base = [{"type": relay.PLANNER_STARTED, "subject": C}, {"type": relay.PLANNER_ENDED, "subject": C, "reason": "exit-1"}]
ok(relay.planner_attempts([], C) == {"attempts": 0, "last_reason": None, "next": "start"}, "nothing yet: start")
ok(relay.planner_attempts(base, C) == {"attempts": 1, "last_reason": "exit-1", "next": "retry"},
   "one failure: R7's single restart")
ok(relay.planner_attempts(base * 2, C)["next"] == "escalate"
   and relay.planner_attempts(base * 2, C)["attempts"] == 2, "two failures: the operator's")
ok(relay.planner_attempts(base + [{"type": relay.PLANNER_STARTED, "subject": C}], C)["attempts"] == 1,
   "an unmatched trailing start is the run happening now, never a failure")
serving = base + [{"type": relay.PLANNER_STARTED, "subject": C, "mode": "serving"},
                  {"type": relay.PLANNER_ENDED, "subject": C, "mode": "serving", "reason": "exit-1"}]
ok(relay.planner_attempts(serving, C)["attempts"] == 1, "a serving-mode pair is not an attempt at cutting (decision 4)")
done = [{"type": relay.PLANNER_STARTED, "subject": C}, {"type": relay.PLANNER_ENDED, "subject": C, "reason": "l1-complete"}]
ok(relay.planner_attempts(done, C) == {"attempts": 0, "last_reason": "l1-complete", "next": "start"},
   "a completed charter is not a failed attempt")

# ── AC6: pending packets and the ledger watermark ──
d = scene("packets")
(d / "seat" / "L-builder-0044.packet.md").write_text("packet")
(d / "seat" / "L-builder-0045.packet.md").write_text("packet")
(d / "seat" / "L-builder-0045.output.json").write_text("{}")
started = (fold.NOW - datetime.timedelta(minutes=7)).isoformat(timespec="seconds")
for sid in ("L-builder-0044", "L-builder-0045", "L-builder-0046"):
    p = d / "events" / f"{sid}.jsonl"
    p.write_text(json.dumps({"v": 1, "ts": started, "type": "spawn-started", "subject": "L-spec-0029",
                             "project": PROJECT, "spawn": sid}, sort_keys=True) + "\n")
pend = relay.pending_packets(read(), d)
ok([p["spawn"] for p in pend] == ["L-builder-0044"],
   f"an answered seat and a seat with no packet on disk are both absent: {pend}")
ok(6.9 < pend[0]["age_min"] < 7.2, f"age_min is minutes from spawn-started to fold.NOW: {pend[0]['age_min']}")
with open(d / "events" / "L-builder-0044.jsonl", "a") as fh:
    fh.write(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "spawn-done",
                         "subject": "L-spec-0029", "project": PROJECT, "spawn": "L-builder-0044"}) + "\n")
ok(relay.pending_packets(read(), d) == [], "a terminal event after the start closes it")
changed, mark = relay.ledger_changed(d, None)
ok(changed is True and mark, "the first look has seen nothing, so it is always changed")
again, mark2 = relay.ledger_changed(d, mark)
ok(again is False and mark2 == mark, "no write in between: unchanged, same watermark")
with open(d / "events" / "L-builder-0046.jsonl", "a") as fh:
    fh.write(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "observed",
                         "subject": "x", "project": PROJECT}) + "\n")
changed, mark3 = relay.ledger_changed(d, mark)
ok(changed is True and mark3 != mark, "one appended line moves the watermark")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0189 · unserved-seat-fails-loudly (L-charter-0028) — R6
# ══════════════════════════════════════════════════════════════════════════════
os.environ.pop("DOIT_SEAT_CLAIM_SEC", None)      # the default (300s) for this whole block


def sev(d, sid, type_, subject, ts, **kw):
    """One event for `relay.unserved`'s fixtures — an explicit `ts`, since these tests
    pin exact boundaries (elapsed=299s/301s, `since_h`'s 2h/30h) the auto-incrementing
    `ev()` helper (one-minute-per-call resolution) cannot express."""
    p = d / "events" / f"{sid}.jsonl"
    with open(p, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": ts, "type": type_, "subject": subject,
                             "project": PROJECT, "spawn": sid, **kw}, sort_keys=True) + "\n")


def iso(delta_seconds):
    return (fold.NOW - datetime.timedelta(seconds=delta_seconds)).isoformat(timespec="seconds")


# ── AC9 · a claimed seat is never listed, whatever the state of its claim ──────
d = scene("unserved-claimed")
sev(d, "L-builder-0100", "build-started", "L-spec-0040", iso(1000), backend="seat")
(d / "seat" / "L-builder-0100.claimed").write_text("")
ok(relay.unserved(read(), d) == [], "a served seat is never listed, however long it has been running")

# ── AC8 (relay half) · role inferred as "builder" for a pending build-started row ──
# `build-started` carries no `role` field of its own (main()'s builder branch never
# writes one) — relay.unserved must infer the literal "builder" rather than reading
# a field that is not there. Distinct from AC9's claimed-build-started fixture: this
# one has NO `.claimed` file and NO terminal event, so it must surface as `pending`.
d = scene("unserved-builder-pending")
sev(d, "L-builder-0101", "build-started", "L-spec-0055", iso(301), backend="seat")
rows = relay.unserved(read(), d)
ok(len(rows) == 1 and rows[0]["role"] == "builder" and rows[0]["status"] == "pending"
   and rows[0]["spawn"] == "L-builder-0101" and rows[0]["subject"] == "L-spec-0055",
   f"a pending build-started row must infer role=='builder': {rows}")

# ── AC10 · failed-unserved, and the since_h decay window ───────────────────────
d = scene("unserved-failed")
sev(d, "L-research-0100", "spawn-started", "L-spec-0041", iso(3 * 3600), backend="seat", role="research")
sev(d, "L-research-0100", "spawn-failed", "L-spec-0041", iso(1 * 3600), reason="unserved")
rows = relay.unserved(read(), d, since_h=24)
ok(len(rows) == 1 and rows[0]["status"] == "failed-unserved" and rows[0]["role"] == "research"
   and rows[0]["spawn"] == "L-research-0100", rows)

d = scene("unserved-decayed")
sev(d, "L-research-0101", "spawn-started", "L-spec-0042", iso(33 * 3600), backend="seat", role="research")
sev(d, "L-research-0101", "spawn-failed", "L-spec-0042", iso(30 * 3600), reason="unserved")
ok(relay.unserved(read(), d, since_h=24) == [], "a failure 30h old at since_h=24 has decayed off the board")

# ── AC11 · exclusions: not-seat, resolved, aged-out, and an unrelated reason ───
d = scene("unserved-exclusions")
sev(d, "L-research-0102", "spawn-started", "L-spec-0043", iso(1000), backend="claude-p", role="research")
sev(d, "L-research-0103", "spawn-started", "L-spec-0044", iso(1000), backend="seat", role="research")
sev(d, "L-research-0103", "spawn-done", "L-spec-0044", iso(10))
sev(d, "L-research-0104", "spawn-started", "L-spec-0045", iso(1000), backend="seat", role="research")
sev(d, "L-research-0104", "spawn-stale", "L-spec-0045", iso(10))
sev(d, "L-research-0105", "spawn-started", "L-spec-0046", iso(1000), backend="seat", role="research")
sev(d, "L-research-0105", "spawn-failed", "L-spec-0046", iso(10), reason="something-else")
ok(relay.unserved(read(), d) == [],
   "(a) not seat-route, (b) served-then-done, (c) aged out by tick's own spawn-stale, "
   "(d) a spawn-failed for an unrelated reason — all four absent")

# ── AC12 · the 300s default boundary, behaviourally ─────────────────────────────
d = scene("unserved-boundary-over")
sev(d, "L-research-0106", "spawn-started", "L-spec-0047", iso(301), backend="seat", role="research")
rows = relay.unserved(read(), d)
ok(len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["spawn"] == "L-research-0106", rows)

d = scene("unserved-boundary-under")
sev(d, "L-research-0107", "spawn-started", "L-spec-0048", iso(299), backend="seat", role="research")
ok(relay.unserved(read(), d) == [], "299s has not elapsed the default 300s claim window")

# the source-text supplement: the SAME default (300) is read independently in dispatch.py
_dispatch_src = (pathlib.Path(__file__).parent / "dispatch.py").read_text()
ok('os.environ.get("DOIT_SEAT_CLAIM_SEC", 300)' in _dispatch_src,
   "src/dispatch.py must carry the same DOIT_SEAT_CLAIM_SEC default reader (300) relay.py does")

# ── AC7: the two EMITS rows, in the file and through the fold ──
src = (pathlib.Path(__file__).parent / "fold.py").read_text()
literal = src[src.index("EMITS = {"):src.index("\nDECLARES = {")]
ok('"planner-started": {"planner"}' in literal and '"planner-ended": {"planner"}' in literal,
   "both rows are IN the static EMITS literal — not reached through the DECLARES loop")
ok(fold.EMITS[relay.PLANNER_STARTED] == {"planner"} and fold.EMITS[relay.PLANNER_ENDED] == {"planner"},
   "and each authorizes exactly the planner")
d = scene("authorization")
ev(d, "L-planner-0003", relay.PLANNER_STARTED, "L-charter-0001", mode="charter")
ev(d, "L-operator-local", relay.PLANNER_STARTED, "L-charter-0001", mode="charter")
events = read()
_, _, ignored, by_subject = fold.fold(events)
ok([e["actor"] for e in ignored] == ["operator"],
   f"a planner-started from a file whose actor is not the planner is recorded and ignored: {ignored}")
ok(len([e for e in by_subject["L-charter-0001"] if e["type"] == relay.PLANNER_STARTED]) == 1,
   "the planner's own row folds; the stamped one derives nothing")
ok(relay.planner_attempts(events, "L-charter-0001")["next"] == "start",
   "and an in-progress planner-started is not an attempt")

# The project filter itself, which the header of this file exists for.
ok(fold.PROJECT == PROJECT, "DOIT_PROJECT is the suite's, set before fold was imported")
with open(d / "events" / "L-planner-0003.jsonl", "a") as fh:
    fh.write(json.dumps({"v": 1, "ts": fold.NOW.isoformat(timespec="seconds"), "type": "charter-filed",
                         "subject": "L-charter-0404"}) + "\n")          # no project: dropped by read_events
ok("L-charter-0404" not in {e.get("subject") for e in read()},
   "an unstamped event is filtered out, so the fixtures never depend on the operator's shell")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0271 · board-owners (L-charter-0033) — Target 5/SD16
# ══════════════════════════════════════════════════════════════════════════════
os.environ.pop("DOIT_SEAT_CLAIM_SEC", None)      # the default (300s) for this whole block

# ── AC12 · a later same-(subject,role) spawn-done clears the earlier row's
# UNSERVED listing too ─────────────────────────────────────────────────────────
d = scene("unserved-later-spawn-done")
sev(d, "L-builder-0200", "spawn-started", "L-spec-0271", iso(2000), backend="seat", role="builder")
sev(d, "L-builder-0200", "spawn-failed", "L-spec-0271", iso(100), reason="unserved")
sev(d, "L-builder-0201", "spawn-started", "L-spec-0271", iso(1000), backend="seat", role="builder")
sev(d, "L-builder-0201", "spawn-done", "L-spec-0271", iso(10))
ok(relay.unserved(read(), d) == [],
   "A (the earlier, failed-unserved) is excluded because a LATER same-(subject,role) "
   "spawn (B) resolved spawn-done; B is excluded because it is itself resolved "
   "(the file's own existing rule)")

# ── AC13 · a seat-stale'd pending row reports stale-still-offered ──────────────
d = scene("unserved-seat-stale")
sev(d, "L-research-0200", "spawn-started", "L-spec-0272", iso(1000), backend="seat", role="research")
sev(d, "L-research-0200", "seat-stale", "L-spec-0272", iso(301), role="research", age_s=301)
rows = relay.unserved(read(), d)
ok(len(rows) == 1 and rows[0]["status"] == "stale-still-offered"
   and rows[0]["spawn"] == "L-research-0200", rows)

print(f"relay: {N} checks pass")
