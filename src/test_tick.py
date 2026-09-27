#!/usr/bin/env python3
"""One runnable check on the tick. Run: python3 test_tick.py"""
import contextlib, datetime, fcntl, io, json, os, pathlib, re, sys, tempfile, time

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
import carry, fold, intake, pane_resume, tick  # noqa: E402

NOW = fold.NOW.isoformat(timespec="seconds")
EV = TMP / "events"

# L-spec-0242 (AC11): `tick._record()` now calls `intake.run(ev)` on EVERY pass —
# ONE module-level `intake.ghlimit = STUB`-shaped assignment (L-spec-0379 fix 2:
# `intake.ghlimit`, never `intake.subprocess`), before the FIRST `tick.main()`
# call below, so all 22 existing fixtures plus every new one route through it;
# a per-fixture patch that only new fixtures received would leave the 22
# existing ones reaching a REAL `gh` (installed on this box).
INTAKE_CALLS = []


class _IntakeStub:
    """A healthy rate, empty listings both labels — `intake.run()` contributes
    nothing to any fixture's ledger, so every existing lane/tick assertion
    below is unaffected by its presence. Implements `ghlimit`'s own two real
    signatures — `.gate(wait=False) -> {"ok": ...}` / `.run(argv, wait=False)
    -> (rc, out, err)` (a tuple, never `subprocess`'s `SimpleNamespace`)."""
    def gate(self, wait=False):
        return {"ok": True, "remaining": 5000, "reset": 0, "waited_s": 0}

    def run(self, argv, wait=False):
        INTAKE_CALLS.append(list(argv))
        if argv[:3] == ["gh", "pr", "list"]:
            return (0, "[]", "")
        raise AssertionError(f"test_tick.py's intake stub got an unexpected call: {argv}")


intake.ghlimit = _IntakeStub()

# L-spec-0274 (R3): `tick._record()` now calls `pane_resume.run(ev)` on EVERY
# pass too. Left unpatched, that call reads this BOX's own real
# `~/.claude/sessions` (pane_resume.run's own default) and could send a real
# `continue` into a real tmux pane out of this very test file — the one
# outcome the spec's own Constraints forbid a test suite from ever risking.
# Stubbed the same way `intake.ghlimit` is: one module-level swap, before
# the FIRST `tick.main()` below, so every existing fixture and every new one
# routes through it; `test_pane_resume.py` alone exercises the real function.
PANE_RESUME_CALLS = []
_real_pane_resume_run = pane_resume.run


def _pane_resume_stub(ev, **kw):
    PANE_RESUME_CALLS.append(len(ev))
    return {"resumed": [], "escalated": [], "skipped": []}


pane_resume.run = _pane_resume_stub


def ticks():
    """`tick`-TYPED lines only: in_flight() appends spawn-stale to the same file."""
    p = tick.tick_path()
    lines = p.read_text().splitlines() if p.exists() else []
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
# L-charter-0042/L-spec-0427, SD3: a plain `written` spec is now the TICK's own
# job (`autodispatch.run`), off this lane, unless it carries a standing
# `autodispatch-failed`. This whole fixture block (through line ~176) tests
# `in_flight`/busy filtering, orthogonal to SD3 — a far-future standing
# failure keeps "L-spec-0001" on the lane whenever not busy, exactly as every
# assertion below already expected, without re-deriving each one under SD3.
write("L-tick-0001.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0001",
                            "reason": "autodispatch-0427: kept standing for this pre-existing busy-filter fixture"})
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
stale = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale and stale[-1]["spawn"] == "L-grader-0009" and ticks()[-1]["lane"] == 1, "past 2× the cap: stale, back on the lane"
# L-charter-0042/L-spec-0427, R1: `autodispatch.candidates()` calls BOTH
# `tick.in_flight`/`tick.spawn_in_flight` (Consumes) — each backed by the SAME
# side-effecting `_spawn_busy`, and `_record()` calls `autodispatch.run` before
# `lane()`'s own separate `in_flight(ev)` call — so ONE pass that first
# observes an aged spawn now records `spawn-stale` more than once for it
# (inherited, "never suppressed", AC14/AC16). The invariant that still holds:
# it never grows further once that spawn is terminal on a later pass's fresh read.
count_after_first_pass = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
tick.main()
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == count_after_first_pass, \
    "stale never grows further once the spawn is terminal on a later pass"
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
before_stale = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
assert tick.main() == 0 and ticks()[-1]["lane"] == 1, "aged out: back on the lane"
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == before_stale, \
    "nothing to name: a spawn-stale with no spawn id would be unmatchable and repeat every tick"
ev_file.unlink()

# ── L-spec-0269 AC6 · window_min-aware staleness replaces the flat 2×cap ──────
# A seat grader recorded with window_min=45 (the observed-data bump) is NOT
# stale at 50 minutes — the old rule (2×15=30) would have called it stale; the
# new (window_min or cap)+cap = 45+15 = 60 does not.
ev269a_file = write("L-grader-0269a.jsonl", {"ts": (fold.NOW - datetime.timedelta(minutes=50)).isoformat(timespec="seconds"),
                                             "type": "spawn-started", "role": "grader", "subject": "L-spec-0269a",
                                             "spawn": "L-grader-0269a", "window_min": 45})
ev269a = fold.read_events()
before_stale269 = len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l])
busy269a = tick.in_flight(ev269a)
assert "L-spec-0269a" in busy269a, "50 min < window_min(45)+cap(15)=60: still busy, not aged out"
assert len([l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]) == before_stale269, \
    "no spawn-stale for the window_min=45 fixture at 50 minutes"
ev269a_file.unlink()

# A `claude-p`-backend grader on an observed-data subject records window_min as
# the FLAT role cap (15) — never the 45-minute bump — so it is stale at 31
# minutes exactly like the pre-existing no-window_min fixture above.
ev269b_file = write("L-grader-0269b.jsonl", {"ts": (fold.NOW - datetime.timedelta(minutes=31)).isoformat(timespec="seconds"),
                                             "type": "spawn-started", "role": "grader", "subject": "L-spec-0269b",
                                             "spawn": "L-grader-0269b", "window_min": 15, "backend": "claude-p"})
ev269b = fold.read_events()
busy269b = tick.in_flight(ev269b)
assert "L-spec-0269b" not in busy269b, "31 min > 15+15=30: aged out, stale, exactly as the no-bump case"
stale269b = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert any(s["spawn"] == "L-grader-0269b" for s in stale269b), stale269b
ev269b_file.unlink()

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
# autodispatch-0427/SD3: a standing failure keeps it on the lane (the tick's own
# job now, otherwise), orthogonal to what this fixture actually tests (R6).
write("L-planner-0007.jsonl", {"type": "spec-written", "subject": "L-spec-0077"})
write("L-tick-0002.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0077",
                            "reason": "autodispatch-0427: kept standing for this pre-existing R6 fixture"})
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
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write("L-tick-0003.jsonl", {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
                            "type": "autodispatch-failed", "subject": "L-spec-0044",
                            "reason": "autodispatch-0427: kept standing for this pre-existing AC4 fixture"})
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
# autodispatch-0427/SD3: standing failures keep both written specs on the lane.
write("L-tick-0004.jsonl",
      {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
       "type": "autodispatch-failed", "subject": "L-spec-0055", "reason": "autodispatch-0427: AC5 fixture"},
      {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
       "type": "autodispatch-failed", "subject": "L-spec-0056", "reason": "autodispatch-0427: AC5 fixture"})
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

# ── AC6/AC7 (superseded by L-charter-0038/L-spec-0388, R1) · shipped-owed-due
# LEFT tick.ACTIONABLE — the sweeper works this state now, off the Executor's
# own lane pass — and it was never counted as done for a charter's L2
# conjunct either way. The DUE_S fixture below (still carrying an open
# escalation) is untouched: it stays absent from the lane, now for a second,
# independent reason on top of the busy-escalation one it originally proved.
assert "shipped-owed-due" not in tick.ACTIONABLE and "shipped-owed-due" not in tick.SPEC_DONE, \
    "L-spec-0388 AC12: shipped-owed-due left ACTIONABLE; never counted as done for a charter's L2 conjunct"
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
# L-charter-0038 · owed-sweep-driver (L-spec-0388) — AC12/AC13/AC14/AC18
# ══════════════════════════════════════════════════════════════════════════════

# AC12/AC13 — pinned as their own value assertions (on top of the inverted
# AC6/AC7 fixture above): "shipped-owed-due" left ACTIONABLE (the sweeper
# works it now); "shipped-owed-expired" joined SPEC_DONE (held like evidence,
# never blocking closure forever over a check that already lapsed).
assert "shipped-owed-due" not in tick.ACTIONABLE
print("sweep-owed-0388 AC12 ok")
assert "shipped-owed-expired" in tick.SPEC_DONE
print("sweep-owed-0388 AC13 ok")

# AC14 — a FRESH fixture, distinct from DUE_S above: shipped plus a 7-day-past
# owed-ac, with NO escalation-blocking anywhere on it and no open spawn. Absent
# from tick.lane() SOLELY because "shipped-owed-due" left tick.ACTIONABLE —
# nothing else (no busy spawn, no escalation) is available to explain it.
DUE_S14 = "L-spec-9388"
old_wake14 = (fold.NOW - datetime.timedelta(days=7)).isoformat(timespec="seconds")
write("L-spec-writer-9388.jsonl",
      {"type": "spec-written", "subject": DUE_S14},
      {"type": "owed-ac", "subject": DUE_S14, "criterion": "AC1", "wake_at": old_wake14})
write("L-executor-9388.jsonl", {"type": "shipped", "subject": DUE_S14})
ev14, specs14, charters14 = folded()
assert specs14[DUE_S14]["state"] == "shipped-owed-due", specs14[DUE_S14]["state"]
busy14 = tick.in_flight(ev14)
assert DUE_S14 not in busy14, "AC14: no escalation and no open spawn — must not read busy"
lanes14 = tick.lane(specs14, charters14, busy14, events=ev14)
assert not any(l.startswith(DUE_S14) for l in lanes14)
print("sweep-owed-0388 AC14 ok")
for f in ("L-spec-writer-9388.jsonl", "L-executor-9388.jsonl"):
    (EV / f).unlink()

# AC18 — closable_fallback's owed tally counts "shipped-owed-expired" the same
# as "shipped-owed-evidence" against K. fold.K pinned to 0 (saved/restored,
# matching test_fold.py's own convention). One charter, one spec, reaching
# "shipped-owed-expired" via shipped + two owed-failed events after the
# governing owed-ac (SD2) — otherwise an ordinary SPEC_DONE member.
S18, C18 = "L-spec-9390", "L-charter-9390"
old_ac_ts18 = (fold.NOW - datetime.timedelta(days=30)).isoformat(timespec="seconds")
old_wake18 = (fold.NOW - datetime.timedelta(days=25)).isoformat(timespec="seconds")
write("L-spec-writer-9390.jsonl",
      {"ts": old_ac_ts18, "type": "spec-written", "subject": S18, "charter": C18},
      {"ts": old_ac_ts18, "type": "owed-ac", "subject": S18, "criterion": "AC1", "wake_at": old_wake18})
write("L-builder-9390.jsonl",
      {"type": "build-started", "subject": S18}, {"type": "build-done", "subject": S18})
write("L-executor-9390.jsonl",
      {"type": "shipped", "subject": S18},
      {"type": "owed-failed", "subject": S18, "criterion": "AC1", "evidence": "e1", "kind": "unmet"},
      {"type": "owed-failed", "subject": S18, "criterion": "AC1", "evidence": "e2", "kind": "unmet"})
write("L-thinker-9390.jsonl", {"type": "charter-filed", "subject": C18})
ev18, specs18, charters18 = folded()
assert specs18[S18]["state"] == "shipped-owed-expired", specs18[S18]["state"]
assert specs18[S18]["charter"] == C18, specs18[S18]["charter"]
_saved_K18 = fold.K
fold.K = 0
try:
    accepted18, _fixpoint18, _review18 = tick.closable_fallback(ev18, C18)
finally:
    fold.K = _saved_K18
assert accepted18 is False, \
    f"AC18: an owed criterion in shipped-owed-expired must count against K, same as shipped-owed-evidence: {accepted18}"
print("sweep-owed-0388 AC18 ok")
for f in ("L-spec-writer-9390.jsonl", "L-builder-9390.jsonl", "L-executor-9390.jsonl", "L-thinker-9390.jsonl"):
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

# ══════════════════════════════════════════════════════════════════════════════
# L-charter-0042/L-spec-0427 — R1: the tick dispatches (autodispatch)
# ══════════════════════════════════════════════════════════════════════════════

# AC17 (autodispatch-0427) — a `written` spec with no standing
# `autodispatch-failed` anywhere is NOT on tick.lane(): the tick dispatches it
# itself now (SD3); the Executor's job narrows to a standing failure alone.
iso_ad17 = _isolated_events()
saved_events = fold.EVENTS
fold.EVENTS = iso_ad17
write_to(iso_ad17, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42001"})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
lanes = tick.lane(specs, charters, tick.in_flight(ev), events=ev)
assert lanes == [], f"autodispatch-0427 AC17: no standing failure -> off the lane: {lanes}"
print("autodispatch-0427 AC17 ok")
fold.EVENTS = saved_events

# AC18 (autodispatch-0427) — the identical spec WITH an `autodispatch-failed`
# newer than its `spec-written` IS on tick.lane(); a `decision` after that
# failure removes it again.
iso_ad18 = _isolated_events()
fold.EVENTS = iso_ad18
t0_ad18 = (fold.NOW - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
t1_ad18 = (fold.NOW - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
write_to(iso_ad18, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42002", "ts": t0_ad18})
write_to(iso_ad18, "L-tick-0001.jsonl", {"type": "autodispatch-failed", "subject": "L-spec-42002",
                                          "reason": "boom", "ts": t1_ad18})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
lanes = tick.lane(specs, charters, tick.in_flight(ev), events=ev)
assert lanes == ["L-spec-42002 · written"], f"autodispatch-0427 AC18a: {lanes}"
t2_ad18 = (fold.NOW - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
write_to(iso_ad18, "L-operator-local.jsonl", {"type": "decision", "subject": "L-spec-42002", "ts": t2_ad18})
ev = fold.read_events()
specs, charters, _, _ = fold.fold(ev)
lanes = tick.lane(specs, charters, tick.in_flight(ev), events=ev)
assert lanes == [], f"autodispatch-0427 AC18b: a later decision clears it again: {lanes}"
print("autodispatch-0427 AC18 ok")
fold.EVENTS = saved_events


class _FakeAutodispatch:
    """Substituted into `sys.modules["autodispatch"]` — `tick._record()`'s own
    lazy `import autodispatch` picks this up instead of the real module."""
    def __init__(self, raise_error=False, append_failed=False):
        self.calls = []
        self.raise_error = raise_error
        self.append_failed = append_failed

    def run(self, ev, specs, now, **kw):
        self.calls.append((list(ev), dict(specs), now))
        if self.raise_error:
            raise RuntimeError("boom-autodispatch")
        if self.append_failed:
            import dispatch as _dispatch
            _dispatch.emit(tick.tick_path(), {"subject": "L-spec-42005"}, "autodispatch-failed", reason="x")
        return []


# AC25(a) (autodispatch-0427) — `_record()` calls `autodispatch.run` exactly
# once, with THIS pass's own `ev`/`specs`/`fold.NOW`.
iso_ad25a = _isolated_events()
fold.EVENTS = iso_ad25a
write_to(iso_ad25a, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42003"})
fake25a = _FakeAutodispatch()
sys.modules["autodispatch"] = fake25a
try:
    assert tick.main() == 0
finally:
    del sys.modules["autodispatch"]
assert len(fake25a.calls) == 1, f"autodispatch-0427 AC25a: exactly one call: {fake25a.calls}"
called_ev, called_specs, called_now = fake25a.calls[0]
assert called_now == fold.NOW, "autodispatch-0427 AC25a: now must be fold.NOW"
assert any(e.get("subject") == "L-spec-42003" for e in called_ev), \
    "autodispatch-0427 AC25a: ev must be this pass's own events"
assert "L-spec-42003" in called_specs, "autodispatch-0427 AC25a: specs must be this pass's own folded specs"
fold.EVENTS = saved_events

# AC25(b) (autodispatch-0427) — a raise leaves `autodispatch_error` on the
# same `tick` event, and never stops `pane_resume.run` or the `tick` append.
iso_ad25b = _isolated_events()
fold.EVENTS = iso_ad25b
write_to(iso_ad25b, "L-planner-0001.jsonl", {"type": "spec-written", "subject": "L-spec-42004"})
before_pane_calls = len(PANE_RESUME_CALLS)
sys.modules["autodispatch"] = _FakeAutodispatch(raise_error=True)
try:
    assert tick.main() == 0
finally:
    del sys.modules["autodispatch"]
assert len(PANE_RESUME_CALLS) == before_pane_calls + 1, "autodispatch-0427 AC25b: pane_resume.run still ran"
lines = [json.loads(l) for l in (iso_ad25b / "L-tick-local.jsonl").read_text().splitlines()]
tick_events = [e for e in lines if e["type"] == "tick"]
assert tick_events and tick_events[-1].get("autodispatch_error") == "boom-autodispatch", \
    f"autodispatch-0427 AC25b: {tick_events}"
fold.EVENTS = saved_events

# AC25(c) (autodispatch-0427), corollary — a fake `run` that instead APPENDS
# `autodispatch-failed` does not change THIS pass's own `todo`; it shows only
# on a SECOND `_record()` call.
iso_ad25c = _isolated_events()
fold.EVENTS = iso_ad25c
write_to(iso_ad25c, "L-planner-0001.jsonl",
         {"type": "spec-written", "subject": "L-spec-42005",
          "ts": (fold.NOW - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")})
sys.modules["autodispatch"] = _FakeAutodispatch(append_failed=True)
try:
    todo1 = tick._record()
finally:
    del sys.modules["autodispatch"]
assert todo1 is not None and "L-spec-42005 · written" not in todo1, \
    f"autodispatch-0427 AC25c: this pass's own todo must not reflect an event run() just appended: {todo1}"
sys.modules["autodispatch"] = _FakeAutodispatch()
try:
    todo2 = tick._record()
finally:
    del sys.modules["autodispatch"]
assert todo2 is not None and "L-spec-42005 · written" in todo2, \
    f"autodispatch-0427 AC25c: the SECOND pass sees the standing failure: {todo2}"
print("autodispatch-0427 AC25 ok")
fold.EVENTS = saved_events


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
saved_events = fold.EVENTS
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
fold.EVENTS = iso
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
fold.EVENTS = saved_events

# AC4 — tick.main() calls carry.sync_v4(ev) exactly once per run, independent
# of whether carry.uncarried() returns anything.
iso = _isolated_events()
fold.EVENTS = iso
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
fold.EVENTS = saved_events

# AC5 — a carry.uncarried/carry.sync_v4 call that raises does not stop the
# tick's own heartbeat: tick.main() still returns 0, still appends exactly one
# `tick` event carrying a `carry_error` field naming the exception, and the
# OTHER call's contribution still lands (the two calls are guarded
# independently — fix 8).
iso = _isolated_events()
fold.EVENTS = iso
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
fold.EVENTS = saved_events

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
slot, spec_id, source_id, charter, charter_reason = carry.pr_slot(pr_url, "t", "b", str(TMP))
assert (pathlib.Path(slot).is_file() and source_id == pr_url and charter is None
        and charter_reason == "absent"), \
    "AC6: --title/--body turn the by-name refusal into a successful carry, writing the packet"

print("tick: 48 checks pass")

# spawn_in_flight is the split (0187, Interfaces): an escalation-blocking with NO
# real spawn beside it is busy for in_flight but never for spawn_in_flight.
write("L-executor-9401.jsonl", {"type": "escalation-blocking", "subject": "L-spec-9401",
                                "why": "operator question", "spawn": "L-executor-9401"})
ev9, _, _ = folded()
assert "L-spec-9401" in tick.in_flight(ev9), "sanity: in_flight includes escalation-busy"
assert "L-spec-9401" not in tick.spawn_in_flight(ev9), \
    "0187: spawn_in_flight excludes escalation-busy entirely — the split Interfaces names"
(EV / "L-executor-9401.jsonl").unlink()

print("tick: 41 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0187 · executor-runs-unattended (L-charter-0028) — R3, tick.wait
# ══════════════════════════════════════════════════════════════════════════════
import subprocess as _sp, threading as _th, time as _time  # noqa: E402

DOIT_BIN = str(pathlib.Path(__file__).resolve().parent.parent / "doit")


def _fresh_root(name):
    r = TMP / name
    (r / "events").mkdir(parents=True, exist_ok=True)
    return r


# ── 0187-AC2: no false "changed" on the very first look ──────────────────────
r2 = _fresh_root("wait-ac2")
t0 = _time.monotonic()
reason = tick.wait(r2, max_s=0.3)
elapsed = _time.monotonic() - t0
assert elapsed >= 0.25 and reason == "interval", \
    f"0187-AC2: nothing written at all -> interval, never an instant false 'changed': {elapsed} {reason}"

# ── 0187-AC3: a mid-call ledger write wakes it promptly ──────────────────────
r3 = _fresh_root("wait-ac3")


def _late_write3():
    _time.sleep(0.1)
    (r3 / "events" / "L-operator-wait3.jsonl").write_text(
        json.dumps({"v": 1, "ts": NOW, "type": "spec-written", "subject": "L-spec-9301"}) + "\n")


th3 = _th.Thread(target=_late_write3)
th3.start()
t0 = _time.monotonic()
reason = tick.wait(r3, max_s=5)
elapsed = _time.monotonic() - t0
th3.join()
assert reason == "changed" and elapsed < 1, \
    f"0187-AC3: a real mid-call write wakes wait() well under the 5s bound: {elapsed} {reason}"

# ── 0187-AC4: exactly one `tick` per RETURNING call, tick.main()'s own shape ──
r4 = _fresh_root("wait-ac4")
fold.ROOT, fold.EVENTS = r4, r4 / "events"      # so tick.tick_path() below reads root 4
tick.wait(r4, max_s=0.2)                        # call 1: times out


def _late_write4():
    _time.sleep(0.05)
    (r4 / "events" / "L-operator-wait4.jsonl").write_text(
        json.dumps({"v": 1, "ts": NOW, "type": "spec-written", "subject": "L-spec-9302"}) + "\n")


th4 = _th.Thread(target=_late_write4)
th4.start()
tick.wait(r4, max_s=5)                          # call 2: woken
th4.join()
lines4 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if l.strip()]
assert len(lines4) == 2 and all(e["type"] == "tick" and isinstance(e.get("lane"), int) for e in lines4), \
    f"0187-AC4: exactly one tick event per returning call, each carrying an int lane=: {lines4}"

# ── 0187-AC16: wait()'s own appended tick never wakes a SUBSEQUENT call ──────
r16 = _fresh_root("wait-ac16")
reason1 = tick.wait(r16, max_s=0.3)
reason2 = tick.wait(r16, max_s=0.3)
assert reason1 == "interval" and reason2 == "interval", \
    f"0187-AC16: the first call's own tick append must not read as 'changed' to the second: {reason1} {reason2}"

# ── 0187-AC17: tick.wait(root=...) writes under the GIVEN root, never the ────
# importing process's DOIT_ROOT ────────────────────────────────────────────
rootA = _fresh_root("wait-ac17-A")
rootB = _fresh_root("wait-ac17-B")
fold.ROOT, fold.EVENTS = rootA, rootA / "events"     # "the importing process's DOIT_ROOT"
tick.wait(rootB, max_s=0.2)
tick_a = rootA / "events" / "L-tick-local.jsonl"
tick_b = rootB / "events" / "L-tick-local.jsonl"
assert tick_b.exists() and len(tick_b.read_text().splitlines()) == 1, \
    f"0187-AC17: the tick lands under the GIVEN root B: {tick_b}"
assert not tick_a.exists(), \
    f"0187-AC17: root A (the importing process's own DOIT_ROOT) gains nothing: {tick_a}"

# restore this file's own binding before continuing (tick.wait mutates fold.ROOT/EVENTS globally)
fold.ROOT, fold.EVENTS = TMP, EV

# ── 0187-AC5: `doit wait --max SEC` — real CLI subprocess ────────────────────
r5 = _fresh_root("wait-ac5-cli")
env5 = {**os.environ, "DOIT_ROOT": str(r5)}
env5.pop("DOIT_PROJECT", None)
proc = _sp.run([DOIT_BIN, "wait", "--max", "0.2"], capture_output=True, text=True, env=env5)
assert proc.returncode == 0, f"0187-AC5: exit 0: {proc.returncode} {proc.stderr}"
assert proc.stdout.strip() == "interval", f"0187-AC5: prints 'interval': {proc.stdout!r}"
tickfile5 = r5 / "events" / "L-tick-local.jsonl"
assert tickfile5.exists() and len(tickfile5.read_text().splitlines()) == 1, \
    f"0187-AC5: L-tick-local.jsonl gains exactly one line: {tickfile5}"

print("tick: 0187 R3 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0198 · trusted-author-inbound-charter (L-charter-0028) — AC5's "same
# wake / next pass" reading (A10, Planner ruling): `tick.lane` re-folded over a
# carry's own just-produced `spec-carried` + `spec-written` events lists the
# spec as written, with no `inbound:` row left standing for its now-carried
# source. `src/tick.py` itself is READ here, never written (finding 5) —
# `tick.lane` is the existing, unaffected seam this file alone is granted to call.
# ══════════════════════════════════════════════════════════════════════════════
iso198 = _isolated_events()
fold.EVENTS = iso198
write_to(iso198, "L-spec-writer-9198.jsonl", {"type": "spec-written", "subject": "L-spec-9198"})
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write_to(iso198, "L-tick-0005.jsonl",
         {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
          "type": "autodispatch-failed", "subject": "L-spec-9198", "reason": "autodispatch-0427: AC5(0198) fixture"})
write_to(iso198, "L-operator-9198.jsonl",
         {"type": "spec-carried", "subject": "L-spec-9198", "source": "9198-src",
          "tier": "gates-only", "audited_at": NOW, "charter": "L-charter-9198",
          "trusted_author": "yitzchak-eg"})
ev198 = fold.read_events()
specs198, charters198, _, _ = fold.fold(ev198)
assert specs198["L-spec-9198"]["state"] == "written", \
    f"AC5 (L-spec-0198): fold.fold() reports the just-carried spec as 'written': {specs198['L-spec-9198']}"
busy198 = tick.in_flight(ev198)
# The "next pass": `carry.uncarried()` would no longer report `9198-src` once a
# `spec-carried` names it, so the inbound list this pass folds over already
# excludes it — proven directly (never trusting a separate implementation to
# agree) rather than re-importing `carry` for a source `tick.py` never touches.
inbound198 = [row for row in [{"source": "9198-src", "kind": "v4"}]
             if row["source"] not in {e.get("source") for e in ev198 if e.get("type") == "spec-carried"}]
assert inbound198 == [], "AC5 (L-spec-0198) precondition: the fixture inbound row is excluded"
lanes198 = tick.lane(specs198, charters198, busy198, events=ev198, inbound=inbound198)
assert "L-spec-9198 · written" in lanes198, \
    f"AC5 (L-spec-0198): tick.lane's NEXT pass over the just-produced events lists it as written: {lanes198}"
assert not any(l.startswith("inbound:9198-src") for l in lanes198), \
    "AC5 (L-spec-0198): no inbound: row remains for the now-carried source"
fold.EVENTS = saved_events

print("tick: L-spec-0198 AC5 check passes")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0242 · intake-core (L-charter-0031) — AC13, SD11's wave-1 half:
# `tick._record()` calls `intake.run(ev)` off its FIRST `fold.read_events()`,
# then RE-READS before `carry.uncarried()`/`fold.fold()`/`lane()` run.
# ══════════════════════════════════════════════════════════════════════════════
iso242 = _isolated_events()
fold.EVENTS = iso242
real_intake_run, real_uncarried242 = intake.run, carry.uncarried
seen242 = {}


def _fake_intake_run(events):
    write_to(iso242, "L-intake-local.jsonl",
             {"type": "inbound-registered", "subject": "https://x/pull/1", "source": "https://x/pull/1",
              "project": "albert-scott", "title": "t", "author_login": "a", "auto": True, "body": "b"})
    return {"spec_prs": [], "note_prs": []}


def _asserting_uncarried(events):
    seen242["types"] = {e["type"] for e in events}
    return []


intake.run, carry.uncarried = _fake_intake_run, _asserting_uncarried
assert tick.main() == 0
assert "inbound-registered" in seen242.get("types", set()), \
    "AC13: intake.run(ev)'s own append is visible to carry.uncarried() via the RE-READ ledger"
intake.run, carry.uncarried = real_intake_run, real_uncarried242
fold.EVENTS = saved_events

# AC13, the other half — a raising intake.run never stops the tick's own
# heartbeat: `intake_error` lands additively, `lane`/`carry_error` unaffected.
iso242b = _isolated_events()
fold.EVENTS = iso242b


def _raising_intake_run(events):
    raise RuntimeError("intake boom")


intake.run = _raising_intake_run
n0_242 = len(ticks())
assert tick.main() == 0, "AC13: a raising intake.run never stops the tick's own heartbeat"
assert len(ticks()) == n0_242 + 1, "AC13: exactly one new tick event lands"
last242 = ticks()[-1]
assert "intake boom" in last242.get("intake_error", ""), \
    f"AC13: the tick event names the caught exception on intake_error: {last242}"
assert "carry_error" not in last242 and last242["lane"] == 0, \
    f"AC13: lane/carry_error are otherwise unaffected: {last242}"
intake.run = real_intake_run
fold.EVENTS = saved_events

print("tick: L-spec-0242 AC13 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0274 · pane-resume (L-charter-0033) — AC6: `tick._record()` calls
# `pane_resume.run(ev)` in its own try/except, placed after `carry.sync_v4(ev)`
# and before `todo = lane(...)`, on the same re-read `ev`; a raising `run()`
# contributes `pane_resume_error` without touching `carry_error`/`intake_error`
# or stopping the tick's own heartbeat.
# ══════════════════════════════════════════════════════════════════════════════
iso274 = _isolated_events()
fold.EVENTS = iso274
seen274 = {}


def _raising_pane_resume(ev, **kw):
    seen274["n"] = len(ev)
    raise RuntimeError("pane_resume boom")


pane_resume.run = _raising_pane_resume
n0_274 = len(ticks())
assert tick.main() == 0, "AC6: a raising pane_resume.run never stops the tick's own heartbeat"
assert len(ticks()) == n0_274 + 1, "AC6: exactly one new tick event lands"
last274 = ticks()[-1]
assert "pane_resume boom" in last274.get("pane_resume_error", ""), \
    f"AC6: the tick event names the caught exception on pane_resume_error: {last274}"
assert "carry_error" not in last274 and "intake_error" not in last274 and isinstance(last274["lane"], int), \
    f"AC6: carry_error/intake_error untouched, lane still an int: {last274}"
assert seen274.get("n") == 0, \
    "AC6: pane_resume.run is called on the re-read ev, an otherwise-empty isolated ledger here"
pane_resume.run = _pane_resume_stub
fold.EVENTS = saved_events

# AC6, the other half — a normal (non-raising) pane_resume.run contributes no
# `pane_resume_error` at all, and intake/carry are unaffected by its presence.
iso274b = _isolated_events()
fold.EVENTS = iso274b
assert tick.main() == 0
last274b = ticks()[-1]
assert "pane_resume_error" not in last274b, f"AC6: a clean run adds no error field: {last274b}"
fold.EVENTS = saved_events

print("tick: L-spec-0274 AC6 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0275 · honest-recording (L-charter-0033) — R2 Target 2: a same-host,
# confirmed-dead waiter's spawn goes stale immediately, independent of the
# elapsed-time cap.
# ══════════════════════════════════════════════════════════════════════════════
import socket, panes  # noqa: E402

HOST = socket.gethostname()


def _dead_pid():
    """A forked-and-reaped child pid — guaranteed dead, no network."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


# AC8 — a same-host, confirmed-dead waiter_pid: spawn-stale fires immediately,
# though the event's own ts is fresh (well inside any elapsed-time cap), and
# the subject returns to the lane right away.
iso8 = _isolated_events()
fold.EVENTS = iso8
dead8 = _dead_pid()
write_to(iso8, "L-planner-8801.jsonl", {"type": "spec-written", "subject": "L-spec-8801"})
# autodispatch-0427/SD3: a standing failure keeps it on the lane regardless.
write_to(iso8, "L-tick-0006.jsonl",
         {"ts": (fold.NOW + datetime.timedelta(days=3650)).isoformat(timespec="seconds"),
          "type": "autodispatch-failed", "subject": "L-spec-8801", "reason": "autodispatch-0427: AC8(0275) fixture"})
write_to(iso8, "L-grader-8801.jsonl", {"type": "spawn-started", "role": "grader",
                                       "subject": "L-spec-8801", "spawn": "L-grader-8801",
                                       "waiter_host": HOST, "waiter_pid": dead8})
assert tick.main() == 0
stale8 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale8 and stale8[-1]["spawn"] == "L-grader-8801" and stale8[-1].get("reason") == "waiter-dead", \
    f"AC8: a same-host confirmed-dead waiter is stale immediately, reason=waiter-dead: {stale8}"
ev8, specs8, charters8 = folded()
assert "L-spec-8801 · written" in tick.lane(specs8, charters8, tick.in_flight(ev8), events=ev8), \
    "AC8: the subject returns to the lane immediately, not after 2x the cap"
fold.EVENTS = saved_events

# AC9 — a live waiter_pid whose recorded waiter_proc_start does NOT match its
# own real proc_start (a pid-reuse simulation): dead, not busy, exactly as AC8.
iso9 = _isolated_events()
fold.EVENTS = iso9
write_to(iso9, "L-planner-8802.jsonl", {"type": "spec-written", "subject": "L-spec-8802"})
write_to(iso9, "L-grader-8802.jsonl", {"type": "spawn-started", "role": "grader",
                                       "subject": "L-spec-8802", "spawn": "L-grader-8802",
                                       "waiter_host": HOST, "waiter_pid": os.getpid(),
                                       "waiter_proc_start": "not-a-real-start-time"})
assert tick.main() == 0
stale9 = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale9 and stale9[-1]["spawn"] == "L-grader-8802" and stale9[-1].get("reason") == "waiter-dead", \
    f"AC9: a mismatched proc_start (pid reuse) is dead even though the pid itself is alive: {stale9}"
fold.EVENTS = saved_events

# AC10 — panes.proc_start monkeypatched to always return None (an unreadable
# own start time): an alive pid stays busy, never dead, regardless of what
# waiter_proc_start names.
iso10 = _isolated_events()
fold.EVENTS = iso10
real_proc_start = panes.proc_start
panes.proc_start = lambda pid: None
try:
    write_to(iso10, "L-planner-8803.jsonl", {"type": "spec-written", "subject": "L-spec-8803"})
    write_to(iso10, "L-grader-8803.jsonl", {"type": "spawn-started", "role": "grader",
                                            "subject": "L-spec-8803", "spawn": "L-grader-8803",
                                            "waiter_host": HOST, "waiter_pid": os.getpid(),
                                            "waiter_proc_start": "anything-at-all"})
    assert tick.main() == 0
    stale10 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
    assert not stale10, f"AC10: an unreadable own proc_start is alive, never dead: {stale10}"
    ev10, specs10, charters10 = folded()
    assert "L-spec-8803 · written" not in tick.lane(specs10, charters10, tick.in_flight(ev10), events=ev10), \
        "AC10: the subject stays busy — it is not on the lane"
finally:
    panes.proc_start = real_proc_start
    fold.EVENTS = saved_events

# AC11 — an int-typed waiter_proc_start that matches (once cast to str) is
# never a false positive for dead: the subject stays busy.
iso11 = _isolated_events()
fold.EVENTS = iso11
real_start = panes.proc_start(os.getpid())
write_to(iso11, "L-planner-8804.jsonl", {"type": "spec-written", "subject": "L-spec-8804"})
write_to(iso11, "L-grader-8804.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8804", "spawn": "L-grader-8804",
                                        "waiter_host": HOST, "waiter_pid": os.getpid(),
                                        "waiter_proc_start": int(real_start)})
assert tick.main() == 0
stale11 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert not stale11, f"AC11: an int-typed matching waiter_proc_start is never a false positive: {stale11}"
ev11, specs11, charters11 = folded()
assert "L-spec-8804 · written" not in tick.lane(specs11, charters11, tick.in_flight(ev11), events=ev11), \
    "AC11: the subject stays busy"
fold.EVENTS = saved_events

# AC12(a)/(b) — cross-host and fields-missing spawns fall through unaffected,
# to the existing elapsed-time behavior (busy, no immediate spawn-stale). The
# two pre-existing 2x-cap spawn-stale fixtures earlier in this file (the
# 45-minute-old L-grader-0009 fixture; the 12-day-old anonymous build-started
# fixture) carry no waiter_* fields, are untouched above, and already passed
# exactly as at base_sha (AC12(c)).
iso12 = _isolated_events()
fold.EVENTS = iso12
dead12 = _dead_pid()
write_to(iso12, "L-planner-8805.jsonl", {"type": "spec-written", "subject": "L-spec-8805"})
write_to(iso12, "L-grader-8805.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8805", "spawn": "L-grader-8805",
                                        "waiter_host": "definitely-not-this-host", "waiter_pid": dead12})
write_to(iso12, "L-planner-8806.jsonl", {"type": "spec-written", "subject": "L-spec-8806"})
write_to(iso12, "L-grader-8806.jsonl", {"type": "spawn-started", "role": "grader",
                                        "subject": "L-spec-8806", "spawn": "L-grader-8806"})
assert tick.main() == 0
stale12 = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert not stale12, f"AC12(a)/(b): cross-host and fields-missing spawns are unaffected, still busy: {stale12}"
ev12, specs12, charters12 = folded()
lanes12 = tick.lane(specs12, charters12, tick.in_flight(ev12), events=ev12)
assert "L-spec-8805 · written" not in lanes12 and "L-spec-8806 · written" not in lanes12, \
    "AC12(a)/(b): both subjects stay busy, falling through to the existing elapsed behavior"
fold.EVENTS = saved_events

print("tick: L-spec-0275 R2 Target 2 checks pass")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0322 · look-mutual-watch (L-charter-0036 R1/SD13/SD22) — AC11/AC17
# ══════════════════════════════════════════════════════════════════════════════
# `crons` genuinely does not exist in this repo pre-wave-1-merge: `import crons,
# look` fails on `crons` before `look` is even reached, so the ImportError half
# below needs no stubbing at all. The present-row halves stub BOTH modules —
# `tick._record()`'s own `import crons, look` picks up whatever `sys.modules`
# already holds.


class _FakeLook:
    def __init__(self, state=None):
        self.calls = []
        self._state = state or {}

    def emit_once(self, condition, key, owner, reading, events, **kw):
        self.calls.append((condition, key, owner))
        return {"condition": condition, "key": key}

    def _read_state(self, root):
        return self._state


class _CronsMissingLook:
    @staticmethod
    def check():
        return [{"name": "look"}]


class _CronsPresent:
    @staticmethod
    def check():
        return []


# AC11(a) — the `look` row missing: exactly one cron-missing/look/thinker call.
iso11 = _isolated_events()
fold.EVENTS = iso11
fakelook11 = _FakeLook()
sys.modules["crons"], sys.modules["look"] = _CronsMissingLook(), fakelook11
try:
    assert tick.main() == 0, "AC11: a tick with the look row missing still exits clean"
finally:
    del sys.modules["crons"], sys.modules["look"]
assert fakelook11.calls == [("cron-missing", "look", "thinker")], \
    f"AC11: exactly one cron-missing/look/thinker call via look.emit_once: {fakelook11.calls}"
assert "look_error" not in ticks()[-1], "AC11: no look_error on a clean (stub-importable) pass"
fold.EVENTS = saved_events

# AC11(b) — `crons`/`look` NOT importable: the tick completes normally, its
# usual lane unchanged, and the SAME `tick` event names the exception.
# ★ measured 2026-09-27 (L-spec-0438): `src/crons.py` landed on this tree
# long before this unit's base_sha, so it is a real, importable module here —
# "crons genuinely does not exist pre-wave-1-merge" (this section's own
# opening comment, above) no longer holds. Unimportable is simulated instead,
# the same technique test_pane_end.py's AC4 already uses for `relay`:
# `sys.modules["crons"] = None` is exactly the ModuleNotFoundError
# `import crons` raises when the file genuinely is not there.
iso11b = _isolated_events()
fold.EVENTS = iso11b
real_crons11b = sys.modules.get("crons")
sys.modules["crons"] = None
try:
    assert tick.main() == 0, "AC11: an ImportError degrades the tick, never crashes it"
    assert "look_error" in ticks()[-1] and "crons" in ticks()[-1]["look_error"], \
        f"AC11: the same tick event names the import failure: {ticks()[-1]}"
finally:
    if real_crons11b is not None:
        sys.modules["crons"] = real_crons11b
    else:
        del sys.modules["crons"]
fold.EVENTS = saved_events
print("AC11 ok")

# AC17(a) — `look` row present, `last_pass` >25 min old: one look-stale/thinker call.
iso17 = _isolated_events()
fold.EVENTS = iso17
old_last_pass = (fold.NOW - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
fakelook17 = _FakeLook(state={"last_pass": old_last_pass})
sys.modules["crons"], sys.modules["look"] = _CronsPresent(), fakelook17
try:
    assert tick.main() == 0
finally:
    del sys.modules["crons"], sys.modules["look"]
assert fakelook17.calls == [("look-stale", "look-stale", "thinker")], \
    f"AC17: a stale last_pass (row present) briefs look-stale/thinker: {fakelook17.calls}"
fold.EVENTS = saved_events

# AC17(b) — a fresh last_pass (<25 min) appends nothing.
iso17b = _isolated_events()
fold.EVENTS = iso17b
fresh_last_pass = (fold.NOW - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
fakelook17b = _FakeLook(state={"last_pass": fresh_last_pass})
sys.modules["crons"], sys.modules["look"] = _CronsPresent(), fakelook17b
try:
    assert tick.main() == 0
finally:
    del sys.modules["crons"], sys.modules["look"]
assert fakelook17b.calls == [], "AC17: within 25 minutes, no look-stale brief"
fold.EVENTS = saved_events

# AC17(c) — the REAL `look.run()`, a fresh pass, clears an open look-stale entry.
import look as _real_look  # noqa: E402


class _NullRunner:
    def ssh(self, *a): raise RuntimeError("no net")
    def git(self, *a): raise RuntimeError("no net")
    def tmux_capture(self, *a): raise RuntimeError("no net")
    def crontab_text(self, *a): raise RuntimeError("no net")
    def disk_usage(self, *a): return {"total": 1, "used": 0, "free": 1}
    def clock(self): return time.monotonic()
    def sleep(self, s): pass


root17c = pathlib.Path(tempfile.mkdtemp())
(root17c / "events").mkdir()
toml17c = root17c / "look.toml"
toml17c.write_text("")   # no [[prod]] rows, no codex targets — nothing external ever fires
opened17c = _real_look.emit_once("look-stale", "look-stale", "thinker", {"last_pass": None}, [], root=root17c)
assert opened17c is not None, "AC17: seeding an open look-stale brief to be cleared"
res17c = _real_look.run([], now=fold.NOW, runner=_NullRunner(), root=root17c, toml_path=toml17c)
assert any(a.get("ref") for a in res17c["answered"]), \
    f"AC17: a fresh look.run() pass clears an open look-stale entry that same pass: {res17c}"
print("AC17 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0438 · grader-pane-serve (L-charter-0042 R8) — grader_serve wiring
# ══════════════════════════════════════════════════════════════════════════════
import grader_serve  # noqa: E402

# (a) `grader_serve.run()` raises -> caught, named on `grader_serve_error`,
# `carry_error`/`intake_error` untouched, `lane` still an int (the
# `carry_error`/`pane_resume_error` pattern). `relay.pending_packets` does not
# yet accept `served_by` pre-merge (Boundaries: `relay.SERVERS`/`served_by`
# are install-and-serve's own footprint, consumed once merged) — this IS
# today's real, unstubbed behaviour, proven here rather than assumed.
iso438a = _isolated_events()
fold.EVENTS = iso438a
assert tick.main() == 0, "grader_serve: a raising grader_serve.run() never crashes the tick"
last438a = ticks()[-1]
assert "grader_serve_error" in last438a and "served_by" in last438a["grader_serve_error"], \
    f"grader_serve: the tick event names the caught exception on grader_serve_error: {last438a}"
assert "carry_error" not in last438a and "intake_error" not in last438a and isinstance(last438a["lane"], int), \
    f"grader_serve: carry_error/intake_error untouched, lane still an int: {last438a}"
fold.EVENTS = saved_events

# (b) a clean `grader_serve.run()` (stubbed, once `served_by` lands) adds no
# `grader_serve_error` field, and is called on every pass.
GRADER_SERVE_CALLS = []
real_grader_serve_run = grader_serve.run


def _grader_serve_stub(ev, **kw):
    GRADER_SERVE_CALLS.append(len(ev))
    return []


grader_serve.run = _grader_serve_stub
iso438b = _isolated_events()
fold.EVENTS = iso438b
try:
    assert tick.main() == 0
    last438b = ticks()[-1]
    assert "grader_serve_error" not in last438b, \
        f"grader_serve: a clean run adds no grader_serve_error field: {last438b}"
    assert GRADER_SERVE_CALLS, "grader_serve: tick._record() calls grader_serve.run() on every pass"
finally:
    grader_serve.run = real_grader_serve_run
fold.EVENTS = saved_events

print("tick: L-spec-0438 grader_serve wiring checks pass")
