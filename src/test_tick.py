#!/usr/bin/env python3
"""One runnable check on the tick. Run: python3 test_tick.py"""
import contextlib, datetime, fcntl, io, json, os, pathlib, re, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
# L-spec-0190: `tick.main()` now calls `carry.uncarried()`/`carry.sync_v4()` for
# real on every run. `carry.py`'s own doc comment is explicit that these three
# must point at fixture directories, never the operator's real ledger/inbox/
# staging tree (test_carry.py's own precedent) — left unset, the very first
# `tick.main()` below reads the operator's live `~/.claude/ledger` and leaks
# real inbound rows into this suite.
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox")
os.environ["V4_STAGING_DIR"] = str(TMP / "v4-staging")
# ★ `fold.PROJECT` is read ONCE at import and `fold.read_events()` filters on it.
# These fixtures carry no `project` key, so a pane with DOIT_PROJECT set turns every
# assertion below false-red. Popped here, before the import, never in the caller's shell.
os.environ.pop("DOIT_PROJECT", None)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import carry, fold, tick  # noqa: E402

NOW = fold.NOW.isoformat(timespec="seconds")
EV = TMP / "events"


def ticks():
    """`tick`-TYPED lines only: in_flight() appends spawn-stale to the same file."""
    lines = tick.TICK.read_text().splitlines() if tick.TICK.exists() else []
    return [e for e in (json.loads(a) for a in lines) if e["type"] == "tick"]


def write(name, *events):
    p = EV / name
    p.write_text("".join(json.dumps({"v": 1, "ts": NOW, **e}) + "\n" for e in events))
    return p


def folded():
    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    return ev, specs, charters


# R1/AC1: there is no Executor-spawn path left in the file, under any backend — the
# grep is the acceptance criterion, so it is also the regression test.
SRC = (pathlib.Path(__file__).parent / "tick.py").read_text()
assert not re.search(r"run_claude|--agent|executor\.schema\.json|models\.|dispatch\.alloc", SRC), \
    "tick.py must contain no code path that starts an Executor process (L-adr-0035)"

# An idle tick: one tick event, lane 0, nothing spawned and no `spawned` key to report it.
assert tick.main() == 0, "a tick is a fold-and-record; it is never an error"
assert len(ticks()) == 1 and ticks()[-1]["lane"] == 0, "idle: exactly one tick event, lane 0"
assert "spawned" not in ticks()[-1], "nothing spawns, so nothing reports having spawned"

# A lane with work on it is recorded, not acted on — and the executor contract's
# presence on disk is no longer this process's business (it used to exit 1 over it).
write("L-planner-0001.jsonl", {"ts": "2026-09-08T10:00:00+00:00", "type": "spec-written", "subject": "L-spec-0001"})
assert tick.main() == 0 and ticks()[-1]["lane"] == 1, "a lane of 1 is recorded and the tick exits clean"
assert len(ticks()) == 2, "AC10: exactly one tick event per run that takes the lock"

# in flight: a start with no terminal event keeps the subject off the lane; a stale one puts it back
ev_file = write("L-grader-0009.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-0001", "spawn": "L-grader-0009"})
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an in-flight grader keeps its subject off the lane"
old_ts = (fold.NOW - datetime.timedelta(minutes=45)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old_ts, "type": "spawn-started", "role": "grader",
                               "subject": "L-spec-0001", "spawn": "L-grader-0009"}) + "\n")
tick.main()
stale = [json.loads(l) for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]
assert stale and stale[-1]["spawn"] == "L-grader-0009" and ticks()[-1]["lane"] == 1, "past 2× the cap: stale, back on the lane"
tick.main()
assert len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]) == 1, "stale is recorded once"
ev_file.unlink()

# a start with NO spawn id — hand-written, or written before the wrapper existed.
# It crashed the tick on the first real charter: `sid.split` on None, and every
# tick after it was dead. It cannot be matched to a terminal event, so it ages
# out on the role's cap and names nothing.
ev_file = write("L-builder-0009.jsonl", {"type": "build-started", "subject": "L-spec-0001"})
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an anonymous start does not crash the tick"
old = (fold.NOW - datetime.timedelta(days=12)).isoformat(timespec="seconds")
ev_file.write_text(json.dumps({"v": 1, "ts": old, "type": "spawn-started",
                               "role": "grader", "subject": "L-spec-0001"}) + "\n")
before_stale = len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l])
assert tick.main() == 0 and ticks()[-1]["lane"] == 1, "aged out: back on the lane"
assert len([l for l in tick.TICK.read_text().splitlines() if '"spawn-stale"' in l]) == before_stale, \
    "nothing to name: a spawn-stale with no spawn id would be unmatchable and repeat every tick"
ev_file.unlink()

# AC6 — an open escalation keeps the subject off the lane; a decision after it puts it back.
# The ONE gate at this layer that is still a gate, because it is the operator's.
esc = write("L-executor-0090.jsonl", {"type": "escalation-blocking", "subject": "L-spec-0001",
                                      "why": "seat", "spawn": "L-executor-0090"})
assert tick.main() == 0 and ticks()[-1]["lane"] == 0, "an escalated subject is the operator's, not the lane's"
esc.write_text(esc.read_text() + json.dumps({"v": 1, "ts": NOW, "type": "decision", "subject": "L-spec-0001",
                                             "why": "w", "revert": "r", "spawn": "L-executor-0090"}) + "\n")
tick.main()
assert ticks()[-1]["lane"] == 1, "a decision after the escalation returns it to the lane"
esc.unlink()

# AC9 — a closed charter stays on the lane until it is reaped. §4.11's reaper refuses
# anything not L2-complete or retracted, and the lane used to admit only
# L1-complete: the reap was unreachable by any tick, and L-charter-0001 sat
# retracted with its worktree standing (2026-09-08).
ch = write("L-operator-tick.jsonl",   # retracted is the operator's (fold.EMITS)
           {"ts": "2026-09-08T09:00:00+00:00", "type": "charter-filed", "subject": "L-charter-0003"},
           {"ts": "2026-09-08T10:00:00+00:00", "type": "charter-retracted", "subject": "L-charter-0003", "why": "w"})
ev, specs, charters = folded()
assert charters["L-charter-0003"]["state"] == "retracted"
assert "L-charter-0003 · retracted" in tick.lane(specs, charters), \
    "a charter that is reapable and unreaped is the Executor's, or doit reap never runs"
ch.write_text(ch.read_text() + json.dumps({"v": 1, "ts": "2026-09-08T11:00:00+00:00", "type": "tree-reaped",
                                           "subject": "L-charter-0003", "reaped": [], "retained": []}) + "\n")
ev, specs, charters = folded()
reaped = {e.get("subject") for e in ev if e["type"] == "tree-reaped"}
assert "L-charter-0003 · retracted" not in tick.lane(specs, charters, reaped=reaped), \
    "once reaped it leaves the lane for good, or every tick pays to reap it again"
ch.unlink()

# AC3 (R6) — a free-standing spec is on the lane on its own terms. It names no
# charter at all, and an unrelated charter sitting at L1-complete cannot touch it.
write("L-planner-0007.jsonl", {"type": "spec-written", "subject": "L-spec-0077"})
write("L-operator-0007.jsonl",            # l1-complete is EMITS-gated to planner/operator
      {"type": "charter-filed", "subject": "L-charter-0007"},
      {"type": "l1-complete", "subject": "L-charter-0007"})
ev, specs, charters = folded()
assert specs["L-spec-0077"]["state"] == "written" and specs["L-spec-0077"]["charter"] is None, \
    "the fixture is a genuinely free-standing spec, or AC3 proves nothing"
assert charters["L-charter-0007"]["state"] == "L1-complete", "the fixture charter really is at L1-complete"
assert "L-spec-0077 · written" in tick.lane(specs, charters, events=ev), \
    "R6: a spec's presence on the lane never depends on any charter's state"

# AC8 (L-spec-0125 R11) — a `Covers: none` charter with every other L2 conjunct
# true, and no `charter-review-complete` event ever appended, still reaches
# `L2-complete` through the REAL fold.closable() (not the fallback) and moves
# from the lane's "awaiting a decision" state onto the reap branch.
assert getattr(fold, "closable", None) is not None, \
    "the real fold.closable must be resolved here, not the fallback (AC8's own precondition)"
RC = "L-charter-1101"
write("L-operator-1101.jsonl",            # charter-filed + l1-complete: both operator-emittable
      {"type": "charter-filed", "subject": RC, "covers": "none"},
      {"type": "l1-complete", "subject": RC})
write("L-builder-1101.jsonl",
      {"type": "spec-written", "subject": "L-spec-1101", "charter": RC},
      {"type": "build-started", "subject": "L-spec-1101"},
      {"type": "build-done", "subject": "L-spec-1101"})
write("L-grader-1101.jsonl", {"type": "verdict", "subject": "L-spec-1101", "confirmed": True})
write("L-reviewer-1101.jsonl", {"type": "review", "subject": "L-spec-1101", "depth": "gates-only"})
write("L-executor-1101.jsonl",
      {"type": "shipped", "subject": "L-spec-1101"},
      {"type": "sweep-fixpoint", "subject": RC})
# deliberately NO charter-review-* event of any kind for RC anywhere above.
ev, specs, charters = folded()
assert specs["L-spec-1101"]["state"] == "accepted", specs["L-spec-1101"]["state"]
assert charters[RC]["state"] == "L2-complete", \
    "R11: Covers: none must never hold a charter open on a review nothing gates the dispatch of"
lanes = tick.lane(specs, charters, events=ev)
assert f"{RC} · L2-complete" in lanes, \
    "AC8: the charter reaches the reap branch, not stuck at L1-complete awaiting a decision"
assert f"{RC} · L1-complete" not in lanes, "AC8: the pre-fix bug (stuck at L1) must not reproduce here"
# Cleanup: this fixture's own build-started (no spawn id) would otherwise leak
# into in_flight()'s busy set for every fixture below it in this file.
for f in ("L-operator-1101.jsonl", "L-builder-1101.jsonl", "L-grader-1101.jsonl",
          "L-reviewer-1101.jsonl", "L-executor-1101.jsonl"):
    (EV / f).unlink()

# AC7/AC8 (R11) — the wave-1 seam decides the L1-complete charter, and only it.
seen = {}


def stub(events, charter):
    seen.update(n=len(events), cid=charter["id"] if isinstance(charter, dict) else charter)
    return seen["verdict"]


seen["verdict"] = (True, True, False)          # all accepted, fixpoint derived, no review owed
fold.closable = stub
lanes = tick.lane(specs, charters, events=ev)
assert "L-charter-0007 · L1-complete" not in lanes, \
    "AC7: a fully-accepted charter closes without an Executor decision"
assert seen["n"] == len(ev) and seen["cid"] == "L-charter-0007", \
    "closable() is handed the whole ledger, never the charter's own c['evs']"
assert "L-spec-0077 · written" in lanes, "R6 again: the charter leaving does not take the free spec with it"
seen["verdict"] = (True, True, True)           # a charter-review is still owed
assert "L-charter-0007 · L1-complete" in tick.lane(specs, charters, events=ev), \
    "AC8: a review still owed is the Executor's dispatch to make"
del fold.closable                              # back to the fallback for everything below

# AC11 — the fallback mirrors fold.fold()'s FULL L2 conjunction: an open in-scope
# brief holds the charter at L1-complete, so it must stay on the lane for the
# Executor's brief-author row. A three-conjunct fallback would drop it here and
# strand it short of reap forever.
write("L-operator-0011.jsonl",
      {"type": "charter-filed", "subject": "L-charter-0011"},
      {"type": "l1-complete", "subject": "L-charter-0011"},
      {"type": "sweep-fixpoint", "subject": "L-charter-0011"},
      {"type": "brief", "subject": "L-charter-0011", "requirement": "R3", "why": "unanswered"})
write("L-charter-reviewer-0011.jsonl", {"type": "charter-review-complete", "subject": "L-charter-0011"})
ev, specs, charters = folded()
c11 = charters["L-charter-0011"]
assert c11["state"] == "L1-complete" and c11["briefs"] == 1 and c11["owed"] == 0, \
    "the fold itself holds this charter at L1 on the brief alone"
assert tick.closable_fallback(ev, c11) == (True, False, False), \
    "AC11: accepted and reviewed, but an open brief means no fixpoint"
assert "L-charter-0011 · L1-complete" in tick.lane(specs, charters, events=ev), \
    "AC11: it stays on the lane, or the brief is never authored and reap never comes"
brief_src = next(e["_src"] for e in ev if e["type"] == "brief" and e.get("subject") == "L-charter-0011")
write("L-executor-0011.jsonl", {"type": "brief-answered", "subject": "L-charter-0011", "ref": brief_src})
ev, specs, charters = folded()
assert tick.closable_fallback(ev, charters["L-charter-0011"]) == (True, True, False), \
    "answer the brief and the same fallback reports the fixpoint derived"
(EV / "L-operator-0011.jsonl").unlink()
(EV / "L-charter-reviewer-0011.jsonl").unlink()
(EV / "L-executor-0011.jsonl").unlink()

# AC4 (R7) — a `blocked` event is a footprint wait, not a lane exclusion. The
# Executor writes one when two units share a footprint; nothing here reads it.
write("L-executor-0044.jsonl",
      {"type": "spec-written", "subject": "L-spec-0044"},
      {"type": "blocked", "subject": "L-spec-0044", "id": "L-spec-0044-wait",
       "owner": "executor", "why": "footprint overlap with L-spec-0045"})
ev, specs, charters = folded()
assert specs["L-spec-0044"]["state"] == "written", "a blocked spec is still a written spec"
assert "L-spec-0044 · written" in tick.lane(specs, charters, tick.in_flight(ev), events=ev), \
    "AC4: an unmatched `blocked` never removes a spec from the lane"

# AC5 (R7) — one subject's own in-flight spawn excludes that subject and nothing else.
# Two written specs sharing a footprint both stand, while a third builds.
write("L-planner-0055.jsonl",
      {"type": "spec-written", "subject": "L-spec-0055"},
      {"type": "spec-written", "subject": "L-spec-0056"},
      {"type": "spec-written", "subject": "L-spec-0057"})
write("L-builder-0055.jsonl", {"type": "build-started", "subject": "L-spec-0057", "spawn": "L-builder-0055"})
ev, specs, charters = folded()
busy = tick.in_flight(ev)
lanes = tick.lane(specs, charters, busy, events=ev)
assert busy == {"L-spec-0057"}, "only the subject with the open spawn is busy"
assert "L-spec-0055 · written" in lanes and "L-spec-0056 · written" in lanes, \
    "AC5: a footprint-adjacent spec is never struck off for a sibling's build"
assert specs["L-spec-0057"]["state"] == "building" and not [x for x in lanes if x.startswith("L-spec-0057")], \
    "the building spec is in flight, not waiting"

# AC10 — one tick event per lock-acquiring run, carrying the count lane() returned.
expect = len(tick.lane(specs, charters, tick.in_flight(ev),
                       {e.get("subject") for e in ev if e["type"] == "tree-reaped"}, events=ev))
n = len(ticks())
assert tick.main() == 0 and len(ticks()) == n + 1, "exactly one tick line per run"
assert ticks()[-1]["lane"] == expect and isinstance(ticks()[-1]["lane"], int), \
    "AC10: the tick records the count lane() returned, as an int"

# AC12 — the three constants out-of-footprint readers depend on are untouched.
assert (tick.MINUTES, tick.USD) == (20, 5) and len(tick.RETIRE) == 7, "the Executor's declared cap survives"
assert fold.caps()["executor"] == (tick.MINUTES, tick.USD), "fold.caps() reads them from here, still"
assert tick.RETIRE[0] == "subagent-driven-development" and tick.RETIRE[-1] == "dispatching-parallel-agents", \
    "§10.5's RETIRE tuple, verbatim — think.pane_cmd() and up.pane_cmd() deny it by name"

# AC10, the other half — a run the flock drops records nothing at all.
held = open(TMP / "tick.lock", "w")
fcntl.flock(held, fcntl.LOCK_EX)
before = len(ticks())
assert tick.main() == 0 and len(ticks()) == before, "flock: a second tick is dropped, not queued"
held.close()

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0192 · fold-states-owed-due-and-killed (L-charter-0028) — R7
# ══════════════════════════════════════════════════════════════════════════════

# ── AC6/AC7 · shipped-owed-due is ACTIONABLE, but an open escalation still
# excludes its subject from the lane exactly like any other actionable state.
assert "shipped-owed-due" in tick.ACTIONABLE and "shipped-owed-due" not in tick.SPEC_DONE, \
    "AC7: shipped-owed-due is on the lane, and is never counted as done for a charter's L2 conjunct"
DUE_S = "L-spec-9192"
old_wake = (fold.NOW - datetime.timedelta(days=7)).isoformat(timespec="seconds")
write("L-spec-writer-9192.jsonl",
      {"type": "spec-written", "subject": DUE_S},
      {"type": "owed-ac", "subject": DUE_S, "criterion": "AC1", "wake_at": old_wake})
write("L-builder-9192.jsonl",
      {"type": "build-started", "subject": DUE_S}, {"type": "build-done", "subject": DUE_S})
write("L-executor-9192.jsonl",
      {"type": "shipped", "subject": DUE_S},
      {"type": "escalation-blocking", "subject": DUE_S, "why": "footprint overlap",
       "default": "wait", "deadline": "2099-01-01T00:00:00Z", "revert": "n/a",
       "spawn": "L-executor-9192-esc"})
ev, specs, charters = folded()
assert specs[DUE_S]["state"] == "shipped-owed-due", specs[DUE_S]["state"]
busy = tick.in_flight(ev)
assert DUE_S in busy, "the open escalation must mark the subject busy"
lanes = tick.lane(specs, charters, busy, events=ev)
assert not any(l.startswith(DUE_S) for l in lanes), \
    "AC6: an open escalation excludes a shipped-owed-due subject exactly as any other ACTIONABLE state"
for f in ("L-spec-writer-9192.jsonl", "L-builder-9192.jsonl", "L-executor-9192.jsonl"):
    (EV / f).unlink()

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0190 · inbound-picked-up-unattended (L-charter-0028) — R9
# ══════════════════════════════════════════════════════════════════════════════


def _isolated_events():
    """A fresh, empty ledger dir — for the 'otherwise-empty ledger' fixtures
    below, decoupled from the shared TMP/events every fixture above this
    banner has been accumulating into."""
    iso = pathlib.Path(tempfile.mkdtemp()) / "events"
    iso.mkdir(parents=True)
    return iso


def write_to(d, name, *events):
    p = d / name
    p.write_text("".join(json.dumps({"v": 1, "ts": NOW, **e}) + "\n" for e in events))
    return p


# AC1 — tick.lane() gains a 6th, keyword-defaulted `inbound` argument: one
# "inbound:<source> · uncarried" row per item not in `busy`, merged into the
# same sorted result as the existing spec/charter rows.
lanes = tick.lane({}, {}, inbound=[{"source": "283", "kind": "pr"}])
assert lanes == ["inbound:283 · uncarried"], \
    f"AC1: a lone inbound item not in busy is the sole row returned: {lanes}"

# AC2(a) — an open escalation-blocking whose subject equals the source id
# excludes it exactly like the existing busy-filtering behavior specs/charters
# get from in_flight(); a decision after it restores the row.
iso = _isolated_events()
saved_events, saved_tick = fold.EVENTS, tick.TICK
fold.EVENTS = iso
p = write_to(iso, "L-executor-0283.jsonl", {"type": "escalation-blocking", "subject": "283",
                                            "why": "carry", "spawn": "L-executor-0283"})
ev = fold.read_events()
busy = tick.in_flight(ev)
lanes = tick.lane({}, {}, busy, events=ev, inbound=[{"source": "283", "kind": "pr"}])
assert not any(l.startswith("inbound:283") for l in lanes), \
    "AC2(a): an escalated source is excluded exactly like a spec or charter"
p.write_text(p.read_text() + json.dumps({"v": 1, "ts": NOW, "type": "decision", "subject": "283",
                                         "why": "w", "revert": "r", "spawn": "L-executor-0283"}) + "\n")
ev = fold.read_events()
busy = tick.in_flight(ev)
lanes = tick.lane({}, {}, busy, events=ev, inbound=[{"source": "283", "kind": "pr"}])
assert any(l.startswith("inbound:283") for l in lanes), \
    "AC2(a): a decision after the escalation restores the row"
fold.EVENTS = saved_events

# AC2(b) — a content/carry-<spec_id>.packet.md file whose trailing line names
# the source, and whose own spec_id is itself in `busy`, excludes the source
# until that spawn reaches a terminal event (fix 5).
iso = _isolated_events()
fold.EVENTS = iso
content_dir = fold.ROOT / "content"
content_dir.mkdir(parents=True, exist_ok=True)
packet = content_dir / "carry-L-spec-0299.packet.md"
packet.write_text("body\n\n---\nSource: v4 spec 283.\n")
try:
    write_to(iso, "L-spec-writer-0299.jsonl", {"type": "spawn-started", "role": "spec-writer",
                                               "subject": "L-spec-0299", "spawn": "L-spec-writer-0299"})
    ev = fold.read_events()
    busy = tick.in_flight(ev)
    assert "L-spec-0299" in busy, "AC2(b) precondition: the spec-writer spawn must be in flight"
    lanes = tick.lane({}, {}, busy, events=ev, inbound=[{"source": "283", "kind": "pr"}])
    assert not any(l.startswith("inbound:283") for l in lanes), \
        "AC2(b): a packet-detected in-flight carry excludes its source from the lane"
    write_to(iso, "L-executor-0299.jsonl", {"type": "spawn-done", "subject": "L-spec-0299",
                                            "spawn": "L-spec-writer-0299", "status": "written"})
    ev = fold.read_events()
    busy = tick.in_flight(ev)
    lanes = tick.lane({}, {}, busy, events=ev, inbound=[{"source": "283", "kind": "pr"}])
    assert any(l.startswith("inbound:283") for l in lanes), \
        "AC2(b): once the spawn reaches a terminal event, the exclusion lifts"
finally:
    packet.unlink()
    fold.EVENTS = saved_events

# AC3 — tick.main() calls carry.uncarried(ev) once per run and folds every
# item it returns into the todo list lane() computes, idle or busy.
iso = _isolated_events()
fold.EVENTS, tick.TICK = iso, iso / "L-tick-local.jsonl"
real_uncarried, real_sync = carry.uncarried, carry.sync_v4
carry.uncarried = lambda events: [{"source": "999-ac3", "kind": "pr", "registered_at": NOW,
                                   "attempts": 0, "last_error": None}]
carry.sync_v4 = lambda events: 0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = tick.main()
out = buf.getvalue()
assert rc == 0 and "inbound:999-ac3 · uncarried" in out, \
    f"AC3: carry.uncarried()'s item is folded into the printed lane: {out!r}"
assert len(ticks()) == 1 and ticks()[-1]["lane"] == 1, \
    "AC3: an otherwise-empty ledger records the one inbound item as the whole lane"
carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS, tick.TICK = saved_events, saved_tick

# AC4 — tick.main() calls carry.sync_v4(ev) exactly once per run, independent
# of whether carry.uncarried() returns anything.
iso = _isolated_events()
fold.EVENTS, tick.TICK = iso, iso / "L-tick-local.jsonl"
real_uncarried, real_sync = carry.uncarried, carry.sync_v4
calls = {"n": 0}


def counting_sync(events):
    calls["n"] += 1
    return 0


carry.uncarried = lambda events: []
carry.sync_v4 = counting_sync
assert tick.main() == 0 and calls["n"] == 1, "AC4: sync_v4 called exactly once, this run"
assert tick.main() == 0 and calls["n"] == 2, "AC4: and exactly once more, the next run"
carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS, tick.TICK = saved_events, saved_tick

# AC5 — a carry.uncarried/carry.sync_v4 call that raises does not stop the
# tick's own heartbeat: tick.main() still returns 0, still appends exactly one
# `tick` event carrying a `carry_error` field naming the exception, and the
# OTHER call's contribution still lands (the two calls are guarded
# independently — fix 8).
iso = _isolated_events()
fold.EVENTS, tick.TICK = iso, iso / "L-tick-local.jsonl"
real_uncarried, real_sync = carry.uncarried, carry.sync_v4


def _raise_uncarried(events):
    raise RuntimeError("boom")


def _raise_sync(events):
    raise RuntimeError("sync boom")


carry.uncarried, carry.sync_v4 = _raise_uncarried, (lambda events: 0)
n0 = len(ticks())
assert tick.main() == 0, "AC5: a raising carry.uncarried never stops the tick's own heartbeat"
assert len(ticks()) == n0 + 1, "AC5: exactly one new tick event lands"
assert "boom" in ticks()[-1].get("carry_error", ""), \
    f"AC5: the tick event names the caught exception: {ticks()[-1]}"

carry.uncarried = lambda events: [{"source": "999-ac5", "kind": "pr", "registered_at": NOW,
                                   "attempts": 0, "last_error": None}]
carry.sync_v4 = _raise_sync
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = tick.main()
out = buf.getvalue()
assert rc == 0 and "inbound:999-ac5 · uncarried" in out, \
    f"AC5: carry.uncarried's item still lands when carry.sync_v4 raises: {out!r}"
assert "sync boom" in ticks()[-1].get("carry_error", ""), ticks()[-1]

carry.uncarried, carry.sync_v4 = real_uncarried, real_sync
fold.EVENTS, tick.TICK = saved_events, saved_tick

# AC6 (part 2, fix 1) — the row's --title/--body argv is what the PR path
# needs: carry.pr_slot(url, None, None, repo) refuses by name, exactly
# mirroring a bare `doit carry <source>` on a PR source; supplying both turns
# the refusal into a successful carry that writes the packet file.
pr_url = "https://github.com/fredhead88/albert-scott-platform/pull/999"
try:
    carry.pr_slot(pr_url, None, None, str(TMP))
    raise AssertionError("AC6: carry.pr_slot with no --title/--body must refuse by name")
except carry.Refusal:
    pass
slot, spec_id, source_id, charter = carry.pr_slot(pr_url, "t", "b", str(TMP))
assert pathlib.Path(slot).is_file() and source_id == pr_url and charter is None, \
    "AC6: --title/--body turn the by-name refusal into a successful carry, writing the packet"

print("tick: 48 checks pass")
