#!/usr/bin/env python3
"""Hermetic checks on autodispatch.py — AC1-AC16, AC19, AC24 (L-spec-0427).
Run: python3 test_autodispatch.py

No real git, no real `doit dispatch`, no network — every recipe call in
`autodispatch.run` goes through an injectable fake `runner`; every ledger read
is a freshly-written tmp-root fixture folded for real through `fold.fold`."""
import datetime, json, os, pathlib, sys, tempfile

os.environ["DOIT_ROOT"] = str(pathlib.Path(tempfile.mkdtemp()))
os.environ["DOIT_NO_POKE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dispatch, fold, shape, tick, autodispatch  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CLEAN_SPEC = """# Goal

Fixture spec used only by test_autodispatch.py.

# Acceptance criteria

AC1 [backend]: trivially true. review_path: n/a.

Writes:
- fixture/file.py

# Verification

```
true && echo VERIFIED
```
"""


def _fresh(project="proj", make_repo=True):
    """A brand-new root: `fold`/`dispatch` both rebound to it (D90's filename-
    derived actor and `dispatch.spec_path`'s CONTENT both read module globals,
    never a passed-in root)."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "content").mkdir(parents=True, exist_ok=True)
    fold.ROOT, fold.EVENTS = root, root / "events"
    dispatch.ROOT, dispatch.EVENTS, dispatch.CONTENT = root, root / "events", root / "content"
    if make_repo:
        (root / "repos").mkdir(parents=True, exist_ok=True)
        bare = root / "bare-repo"
        bare.mkdir()
        (root / "repos" / project).symlink_to(bare)
    return root


def _write(root, name, *events):
    p = root / "events" / name
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    return p


def _spec_file(root, sid, text=CLEAN_SPEC):
    (root / "content" / f"{sid}.md").write_text(text)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _fold_and_candidates(root):
    ev = fold.read_events()
    specs, charters, _, _ = fold.fold(ev)
    return ev, specs, autodispatch.candidates(ev, specs, fold.NOW)


def _row(rows, sid):
    return next(r for r in rows if r["spec"] == sid)


NOW = fold.NOW

# ── AC1: five conditions clean -> dispatchable ──────────────────────────────
root = _fresh("proj1")
_spec_file(root, "L-spec-1001")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-1001",
        "project": "proj1"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-1001")
assert r["status"] == "dispatchable", f"AC1: expected dispatchable, got {r}"
print("autodispatch-0427 AC1 ok")

# ── AC2: wave_blocker names a sibling -> blocked ────────────────────────────
root = _fresh("proj2")
plan = ("## Unit A\nFootprint:\n- src/sibling.py\nWave: 1\n\n"
        "## Unit B\nFootprint:\n- src/mine.py\nWave: 2\n")
(root / "content" / "plan-L-charter-0042.md").write_text(plan)
_spec_file(root, "L-spec-2002")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-2001",
        "project": "proj2", "charter": "L-charter-0042", "footprint": ["src/sibling.py"]},
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-2002",
        "project": "proj2", "charter": "L-charter-0042", "footprint": ["src/mine.py"]})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-2002")
assert r["status"] == "blocked" and "L-spec-2001" in (r["reason"] or ""), f"AC2: {r}"
print("autodispatch-0427 AC2 ok")

# ── AC3: shape.check block non-empty -> blocked, never dispatched ──────────
root = _fresh("proj3")
_spec_file(root, "L-spec-3001", text="not a real spec")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-3001",
        "project": "proj3"})
ev, specs, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-3001")
assert r["status"] == "blocked" and (r["reason"] or "").startswith("shape:"), f"AC3: {r}"
calls = []
autodispatch.run(ev, specs, fold.NOW, runner=lambda a: calls.append(a) or {"code": 0, "stdout": "", "stderr": ""})
assert calls == [], f"AC3: a blocked spec must never reach the recipe runner: {calls}"
print("autodispatch-0427 AC3 ok")

# ── AC4: standing autodispatch-failed blocks; a later decision clears it ──
root = _fresh("proj4")
_spec_file(root, "L-spec-4001")
t0 = NOW - datetime.timedelta(hours=2)
t1 = NOW - datetime.timedelta(hours=1)
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(t0), "type": "spec-written", "subject": "L-spec-4001", "project": "proj4"})
_write(root, "L-tick-local.jsonl",
       {"v": 1, "ts": _iso(t1), "type": "autodispatch-failed", "subject": "L-spec-4001", "reason": "boom"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-4001")
assert r["status"] == "blocked" and r["reason"] == "boom", f"AC4a: {r}"
t2 = NOW - datetime.timedelta(minutes=30)
_write(root, "L-operator-local.jsonl",
       {"v": 1, "ts": _iso(t2), "type": "decision", "subject": "L-spec-4001"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-4001")
assert r["status"] == "dispatchable", f"AC4b: a later decision must clear the standing failure: {r}"
print("autodispatch-0427 AC4 ok")

# ── AC5: an open escalation-blocking (no spawn) -> blocked ─────────────────
root = _fresh("proj5")
_spec_file(root, "L-spec-5001")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-5001", "project": "proj5"})
_write(root, "L-operator-local.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "escalation-blocking", "subject": "L-spec-5001"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-5001")
assert r["status"] == "blocked", f"AC5: {r}"
print("autodispatch-0427 AC5 ok")

# ── AC6: missing $R/repos/<project> symlink -> blocked, names it ──────────
root = _fresh("proj6", make_repo=False)
_spec_file(root, "L-spec-6001")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-6001", "project": "ghost-project"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-6001")
assert r["status"] == "blocked" and "ghost-project" in (r["reason"] or ""), f"AC6: {r}"
print("autodispatch-0427 AC6 ok")

# ── AC7: capacity truncation, oldest-first, and one occupied seat ─────────
root = _fresh("proj7")
(root / "models.toml").write_text("[seats]\nbuilder = 2\n")
for i, sid in enumerate(["L-spec-7001", "L-spec-7002", "L-spec-7003"]):
    _spec_file(root, sid)
ts_a = NOW - datetime.timedelta(hours=3)
ts_b = NOW - datetime.timedelta(hours=2)
ts_c = NOW - datetime.timedelta(hours=1)
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(ts_a), "type": "spec-written", "subject": "L-spec-7001", "project": "proj7"},
       {"v": 1, "ts": _iso(ts_b), "type": "spec-written", "subject": "L-spec-7002", "project": "proj7"},
       {"v": 1, "ts": _iso(ts_c), "type": "spec-written", "subject": "L-spec-7003", "project": "proj7"})
_, _, rows = _fold_and_candidates(root)
st = {r["spec"]: r["status"] for r in rows}
assert st == {"L-spec-7001": "dispatchable", "L-spec-7002": "dispatchable", "L-spec-7003": "seat-wait"}, f"AC7a: {st}"
_write(root, "L-builder-9001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "build-started", "subject": "L-spec-9999", "spawn": "L-builder-9001"})
_, _, rows = _fold_and_candidates(root)
st = {r["spec"]: r["status"] for r in rows}
assert st == {"L-spec-7001": "dispatchable", "L-spec-7002": "seat-wait", "L-spec-7003": "seat-wait"}, f"AC7b: {st}"
print("autodispatch-0427 AC7 ok")

# ── AC8: capacity() reads [seats].builder; falls back to 4 ────────────────
r1 = _fresh("p8a")
(r1 / "models.toml").write_text("[seats]\nbuilder = 6\n")
assert autodispatch.capacity(root=r1) == 6, "AC8a"
r2 = _fresh("p8b")
(r2 / "models.toml").write_text("[defaults]\nbackend = \"seat\"\n")
assert autodispatch.capacity(root=r2) == 4, "AC8b: no [seats] table"
r3 = _fresh("p8c")
assert autodispatch.capacity(root=r3) == 4, "AC8c: no models.toml at all"
print("autodispatch-0427 AC8 ok")

# ── AC9: since = spec-written's own ts, or a later decision's ts ──────────
root = _fresh("proj9")
_spec_file(root, "L-spec-9001")
_spec_file(root, "L-spec-9002")
t0 = NOW - datetime.timedelta(hours=5)
t1 = NOW - datetime.timedelta(hours=1)
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(t0), "type": "spec-written", "subject": "L-spec-9001", "project": "proj9"},
       {"v": 1, "ts": _iso(t0), "type": "spec-written", "subject": "L-spec-9002", "project": "proj9"})
_write(root, "L-operator-local.jsonl",
       {"v": 1, "ts": _iso(t1), "type": "decision", "subject": "L-spec-9002"})
_, _, rows = _fold_and_candidates(root)
assert _row(rows, "L-spec-9001")["since"] == _iso(t0), "AC9a"
assert _row(rows, "L-spec-9002")["since"] == _iso(t1), "AC9b"
print("autodispatch-0427 AC9 ok")

# ── AC10: never wave-blocked -> since is the marker ts alone ──────────────
root = _fresh("proj10")
plan = "## Unit A\nFootprint:\n- src/only.py\nWave: 1\n"
(root / "content" / "plan-L-charter-0099.md").write_text(plan)
_spec_file(root, "L-spec-10001")
t0 = NOW - datetime.timedelta(hours=4)
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(t0), "type": "spec-written", "subject": "L-spec-10001", "project": "proj10",
        "charter": "L-charter-0099", "footprint": ["src/only.py"]})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-10001")
assert r["charter"] == "L-charter-0099" and r["since"] == _iso(t0), f"AC10: {r}"
print("autodispatch-0427 AC10 ok")


class _FakeRunner:
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, argv):
        i = len(self.calls)
        self.calls.append(list(argv))
        return self.script.get(i, {"code": 0, "stdout": "", "stderr": ""})


def _dispatchable_fixture(root_label, sid, project, ts):
    root = _fresh(project)
    _spec_file(root, sid)
    _write(root, "L-planner-0001.jsonl",
           {"v": 1, "ts": _iso(ts), "type": "spec-written", "subject": sid, "project": project})
    return root


# ── AC11: exact 4-call recipe, in order, worktree/repo agree between steps ─
root = _dispatchable_fixture("11", "L-spec-11001", "proj11", NOW)
ev, specs, _ = _fold_and_candidates(root)
detach_json = json.dumps({"detached": 4242, "role": "builder", "subject": "L-spec-11001", "log": "/tmp/x.log"})
runner = _FakeRunner({
    0: {"code": 0, "stdout": "main\n", "stderr": ""},
    1: {"code": 0, "stdout": "", "stderr": ""},
    2: {"code": 0, "stdout": "/pkt/L-spec-11001-builder-1.md\n", "stderr": ""},
    3: {"code": 0, "stdout": detach_json, "stderr": ""},
})
out = autodispatch.run(ev, specs, fold.NOW, runner=runner)
assert len(runner.calls) == 4, f"AC11: {runner.calls}"
repo = str(root / "repos" / "proj11")
worktree = str(root / "worktrees" / "proj11" / "l-spec-11001")
assert runner.calls[0] == ["git", "-C", repo, "symbolic-ref", "--short", "HEAD"], runner.calls[0]
assert runner.calls[1] == ["git", "-C", repo, "worktree", "add", worktree, "-b", "l-spec-11001", "main"], runner.calls[1]
assert runner.calls[2] == ["doit", "packet", "builder", "L-spec-11001", "--worktree", worktree, "--repo", repo], runner.calls[2]
assert runner.calls[2][runner.calls[2].index("--worktree") + 1] == runner.calls[1][5], "AC11: worktree must match step 2"
assert runner.calls[2][runner.calls[2].index("--repo") + 1] == repo, "AC11: repo must match step 2's repo"
assert runner.calls[3][:5] == ["doit", "dispatch", "--detach", "builder", "L-spec-11001"], runner.calls[3]
print("autodispatch-0427 AC11 ok")

# ── AC12: exactly one autodispatched event, fields from the detach JSON ──
lines = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp = [e for e in lines if e["type"] == "autodispatched"]
assert len(disp) == 1, f"AC12: {disp}"
d = disp[0]
assert d["spawn"] == "4242" and d["worktree"] == worktree and d["log"] == "/tmp/x.log" and d["since"], f"AC12: {d}"
print("autodispatch-0427 AC12 ok")

# ── AC13: worktree add fails -> exactly one autodispatch-failed, no further calls ─
root = _dispatchable_fixture("13", "L-spec-13001", "proj13", NOW)
ev, specs, _ = _fold_and_candidates(root)
runner = _FakeRunner({
    0: {"code": 0, "stdout": "main\n", "stderr": ""},
    1: {"code": 1, "stdout": "", "stderr": "boom-worktree"},
})
autodispatch.run(ev, specs, fold.NOW, runner=runner)
assert len(runner.calls) == 2, f"AC13: no further recipe call expected: {runner.calls}"
lines = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
fails = [e for e in lines if e["type"] == "autodispatch-failed"]
assert len(fails) == 1 and fails[0]["reason"] == "boom-worktree", f"AC13: {fails}"
print("autodispatch-0427 AC13 ok")

# ── AC14: dry_run calls the runner zero times, appends zero events ───────
root = _fresh("proj14")
_spec_file(root, "L-spec-14001")
_spec_file(root, "L-spec-14002")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(hours=2)), "type": "spec-written",
        "subject": "L-spec-14001", "project": "proj14"},
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(hours=1)), "type": "spec-written",
        "subject": "L-spec-14002", "project": "proj14"})
ev, specs, _ = _fold_and_candidates(root)
runner = _FakeRunner()


def _never(a):
    raise AssertionError(f"AC14: runner must not be called under dry_run: {a}")


out = autodispatch.run(ev, specs, fold.NOW, runner=_never, dry_run=True)
assert len(out) == 2, f"AC14: {out}"
before = tick.tick_path()
assert not before.exists() or not any(
    json.loads(l)["type"] in ("autodispatched", "autodispatch-failed")
    for l in before.read_text().splitlines()), "AC14: dry_run must append no event"
print("autodispatch-0427 AC14 ok")

# ── AC15: capacity()==1, two dispatchable -> only the older is dispatched ──
root = _fresh("proj15")
(root / "models.toml").write_text("[seats]\nbuilder = 1\n")
_spec_file(root, "L-spec-15001")
_spec_file(root, "L-spec-15002")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(hours=2)), "type": "spec-written",
        "subject": "L-spec-15001", "project": "proj15"},
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(hours=1)), "type": "spec-written",
        "subject": "L-spec-15002", "project": "proj15"})
ev, specs, rows = _fold_and_candidates(root)
assert _row(rows, "L-spec-15001")["status"] == "dispatchable"
assert _row(rows, "L-spec-15002")["status"] == "seat-wait"
runner = _FakeRunner({
    0: {"code": 0, "stdout": "main\n", "stderr": ""},
    1: {"code": 0, "stdout": "", "stderr": ""},
    2: {"code": 0, "stdout": "/pkt/x.md\n", "stderr": ""},
    3: {"code": 0, "stdout": json.dumps({"detached": 1, "role": "builder", "subject": "L-spec-15001", "log": "l"}),
        "stderr": ""},
})
autodispatch.run(ev, specs, fold.NOW, runner=runner)
assert len(runner.calls) == 4, f"AC15: only one spec's recipe should run: {runner.calls}"
assert all("15001" in c for c in (runner.calls[1][5], runner.calls[2][3])), runner.calls
lines = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp = [e for e in lines if e["type"] == "autodispatched"]
assert len(disp) == 1 and disp[0]["subject"] == "L-spec-15001", f"AC15: {disp}"
print("autodispatch-0427 AC15 ok")

# ── AC16: an in-flight build-started occupies a slot; aged past window frees it ─
root = _fresh("proj16")
(root / "models.toml").write_text("[seats]\nbuilder = 1\n")
_spec_file(root, "L-spec-16001")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(hours=1)), "type": "spec-written",
        "subject": "L-spec-16001", "project": "proj16"})
_write(root, "L-builder-9002.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "build-started", "subject": "L-spec-9998", "spawn": "L-builder-9002"})
_, _, rows = _fold_and_candidates(root)
assert _row(rows, "L-spec-16001")["status"] == "seat-wait", "AC16a: a fresh in-flight build occupies the seat"
_write(root, "L-builder-9002.jsonl",
       {"v": 1, "ts": _iso(NOW - datetime.timedelta(minutes=200)), "type": "build-started",
        "subject": "L-spec-9998", "spawn": "L-builder-9002"})
_, _, rows = _fold_and_candidates(root)
assert _row(rows, "L-spec-16001")["status"] == "dispatchable", "AC16b: aged past window, the seat frees up"
stale_lines = [l for l in tick.tick_path().read_text().splitlines() if '"spawn-stale"' in l]
assert stale_lines, "AC16b: tick.spawn_in_flight's own spawn-stale side effect must still land"
print("autodispatch-0427 AC16 ok")

# ── AC19: agents/executor.md's `written` row, verbatim ────────────────────
executor_md = (REPO_ROOT / "agents" / "executor.md").read_text()
assert "act only on a `written` spec with an `autodispatch-failed` newer than its `spec-written`, on that failure's reason" \
    in executor_md, "AC19: new sentence missing verbatim"
assert "Cut the worktree, install the wave's ratified dependencies if the Plan names any (D73), dispatch `builder`" \
    not in executor_md, "AC19: the old unconditional action must be gone"
print("autodispatch-0427 AC19 ok")

# ── AC24: both models toml files parse, real tomllib, [seats].builder == 4 ─
import tomllib
for name in ("models.example.toml", "models.claude-only.toml"):
    doc = tomllib.loads((REPO_ROOT / name).read_text())
    assert doc["seats"]["builder"] == 4, f"AC24: {name} seats.builder != 4: {doc.get('seats')}"
print("autodispatch-0427 AC24 ok")

# ── L-spec-0478/AC5 · SD9: a `written` spec blocked ONLY by the new "Plain
# English:" finding is dispatchable (CLEAN_SPEC, unmodified — carries no
# plain line and no plain-backfill sidecar, standing in for any spec written
# before this unit shipped); a spec blocked on a DIFFERENT bucket (the
# existing "not a real spec" fixture, AC3's own text) still blocks — the
# filter removes only the one named finding ─────────────────────────────────
root = _fresh("proj478")
_spec_file(root, "L-spec-47801")  # CLEAN_SPEC, unmodified
_spec_file(root, "L-spec-47802", text="not a real spec")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-47801", "project": "proj478"},
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-47802", "project": "proj478"})
_, _, rows = _fold_and_candidates(root)
r_plain = _row(rows, "L-spec-47801")
assert r_plain["status"] == "dispatchable", \
    f"AC-plain: a spec blocked only by the missing plain-English line must be dispatchable: {r_plain}"
r_other = _row(rows, "L-spec-47802")
assert r_other["status"] == "blocked" and (r_other["reason"] or "").startswith("shape:"), \
    f"AC-plain: a spec blocked on a different bucket must stay blocked: {r_other}"
print("autodispatch-0478 AC-plain ok")


# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0483 · tick-next-step (L-charter-0042 R13a/R13b) — the tick dispatches
# the grader after build-done/rework/regrade, and replays or escalates a dead
# builder/grader/spec-writer/spec-auditor spawn.
# ══════════════════════════════════════════════════════════════════════════════
import socket  # noqa: E402


def _dead_pid():
    """A forked-and-reaped child pid — guaranteed dead, no network (mirrors
    test_tick.py's own L-spec-0275 AC8 fixture)."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


HOST483 = socket.gethostname()
dispatch.SEAT.mkdir(parents=True, exist_ok=True)


def _gc(root):
    ev = fold.read_events()
    specs, _, _, _ = fold.fold(ev)
    return ev, specs, autodispatch.grader_candidates(ev, specs, NOW)


# ── AC1: grader_candidates — trigger=build-done, since=ts of the newest
# build-done(status=DONE); no row for BLOCKED/NEEDS_CONTEXT ─────────────────
root = _fresh("g1")
_spec_file(root, "L-spec-48101")
_spec_file(root, "L-spec-48102")
_spec_file(root, "L-spec-48103")
t0 = NOW - datetime.timedelta(hours=2)
_write(root, "L-planner-0001.jsonl",
       {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48101", "project": "g1"},
       {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48102", "project": "g1"},
       {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48103", "project": "g1"})
t1 = NOW - datetime.timedelta(minutes=30)
_write(root, "L-builder-48101.jsonl", {"ts": _iso(t1), "type": "build-done", "subject": "L-spec-48101", "status": "DONE"})
_write(root, "L-builder-48102.jsonl", {"ts": _iso(t1), "type": "build-done", "subject": "L-spec-48102", "status": "BLOCKED"})
_write(root, "L-builder-48103.jsonl", {"ts": _iso(t1), "type": "build-done", "subject": "L-spec-48103", "status": "NEEDS_CONTEXT"})
_, _, rows = _gc(root)
r = next(r for r in rows if r["spec"] == "L-spec-48101")
assert r["trigger"] == "build-done" and r["since"] == _iso(t1), f"AC1a: {r}"
assert not any(r["spec"] == "L-spec-48102" for r in rows), "AC1b: BLOCKED yields no row"
assert not any(r["spec"] == "L-spec-48103" for r in rows), "AC1c: NEEDS_CONTEXT yields no row"
print("autodispatch-0483 AC1 ok")

# ── AC2: build-done newer than a rejected-criterion -> rework; no earlier
# rejected-criterion -> build-done ──────────────────────────────────────────
root = _fresh("g2")
_spec_file(root, "L-spec-48201")
_spec_file(root, "L-spec-48202")
t0 = NOW - datetime.timedelta(hours=3)
_write(root, "L-planner-0001.jsonl",
       {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48201", "project": "g2"},
       {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48202", "project": "g2"})
t_reject = NOW - datetime.timedelta(hours=1)
t_bd = NOW - datetime.timedelta(minutes=20)
_write(root, "L-grader-48201.jsonl", {"ts": _iso(t_reject), "type": "rejected-criterion",
                                       "subject": "L-spec-48201", "criterion": "AC1"})
_write(root, "L-builder-48201.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": "L-spec-48201", "status": "DONE"})
_write(root, "L-builder-48202.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": "L-spec-48202", "status": "DONE"})
_, _, rows = _gc(root)
assert next(r for r in rows if r["spec"] == "L-spec-48201")["trigger"] == "rework", "AC2a"
assert next(r for r in rows if r["spec"] == "L-spec-48202")["trigger"] == "build-done", "AC2b"
print("autodispatch-0483 AC2 ok")

# ── AC3: decision regrade=yes from thinker/operator, newer than build-done
# -> regrade; the same from actor builder, or no regrade=yes -> build-done ──
root = _fresh("g3")
sids3 = ("L-spec-48301", "L-spec-48302", "L-spec-48303", "L-spec-48304")
for sid in sids3:
    _spec_file(root, sid)
t0 = NOW - datetime.timedelta(hours=3)
_write(root, "L-planner-0001.jsonl", *[
    {"ts": _iso(t0), "type": "spec-written", "subject": sid, "project": "g3"} for sid in sids3])
t_bd = NOW - datetime.timedelta(hours=2)
t_dec = NOW - datetime.timedelta(minutes=10)
for sid in sids3:
    _write(root, f"L-builder-{sid[-5:]}.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": sid, "status": "DONE"})
_write(root, "L-thinker-0001.jsonl", {"ts": _iso(t_dec), "type": "decision", "subject": "L-spec-48301", "regrade": "yes"})
_write(root, "L-operator-local.jsonl", {"ts": _iso(t_dec), "type": "decision", "subject": "L-spec-48302", "regrade": "yes"})
_write(root, "L-builder-48303b.jsonl", {"ts": _iso(t_dec), "type": "decision", "subject": "L-spec-48303", "regrade": "yes"})
_write(root, "L-thinker-0002.jsonl", {"ts": _iso(t_dec), "type": "decision", "subject": "L-spec-48304"})
_, _, rows = _gc(root)
assert next(r for r in rows if r["spec"] == "L-spec-48301")["trigger"] == "regrade", "AC3a thinker"
assert next(r for r in rows if r["spec"] == "L-spec-48302")["trigger"] == "regrade", "AC3b operator"
assert next(r for r in rows if r["spec"] == "L-spec-48303")["trigger"] == "build-done", "AC3c builder actor ignored"
assert next(r for r in rows if r["spec"] == "L-spec-48304")["trigger"] == "build-done", "AC3d no regrade=yes ignored"
print("autodispatch-0483 AC3 ok")

# ── AC4: a decision regrade=yes rework=yes newer than build-done, with a
# standing rejected-criterion, yields NO row and no runner call; a build-done
# appended after it then yields one row with trigger=rework ────────────────
root = _fresh("g4")
_spec_file(root, "L-spec-48401")
t0 = NOW - datetime.timedelta(hours=5)
_write(root, "L-planner-0001.jsonl", {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48401", "project": "g4"})
t_bd0 = NOW - datetime.timedelta(hours=4)
_write(root, "L-builder-48401a.jsonl", {"ts": _iso(t_bd0), "type": "build-done", "subject": "L-spec-48401", "status": "DONE"})
t_grade = NOW - datetime.timedelta(hours=3)
_write(root, "L-grader-48401.jsonl",
       {"ts": _iso(t_grade), "type": "spawn-started", "role": "grader", "subject": "L-spec-48401", "spawn": "L-grader-48401"},
       {"ts": _iso(t_grade), "type": "rejected-criterion", "subject": "L-spec-48401", "criterion": "AC1"})
t_dec = NOW - datetime.timedelta(hours=2)
_write(root, "L-thinker-0003.jsonl", {"ts": _iso(t_dec), "type": "decision", "subject": "L-spec-48401",
                                       "regrade": "yes", "rework": "yes"})
ev, specs, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48401" for r in rows), f"AC4a: {rows}"
calls4 = []
autodispatch.run(ev, specs, NOW, runner=lambda a: calls4.append(a) or {"code": 0, "stdout": "", "stderr": ""})
assert not any("48401" in str(c) for c in calls4), f"AC4a: no runner call for 48401: {calls4}"
t_bd1 = NOW - datetime.timedelta(minutes=15)
_write(root, "L-builder-48401b.jsonl", {"ts": _iso(t_bd1), "type": "build-done", "subject": "L-spec-48401", "status": "DONE"})
_, _, rows = _gc(root)
r = next(r for r in rows if r["spec"] == "L-spec-48401")
assert r["trigger"] == "rework" and r["since"] == _iso(t_bd1), f"AC4b: {r}"
print("autodispatch-0483 AC4 ok")

# ── AC5: seven suppressor cases -> no row; a newer build-done after a refusal
# makes the spec a candidate again ──────────────────────────────────────────
def _fresh_trigger_spec(label, sid):
    root = _fresh(label)
    _spec_file(root, sid)
    t0 = NOW - datetime.timedelta(hours=4)
    _write(root, "L-planner-0001.jsonl", {"ts": _iso(t0), "type": "spec-written", "subject": sid, "project": label})
    t_bd = NOW - datetime.timedelta(hours=3)
    _write(root, "L-builder-x.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": sid, "status": "DONE"})
    return root, t_bd


t_after5 = NOW - datetime.timedelta(hours=2)

print("case grader-spawn-started")
root, _ = _fresh_trigger_spec("g5a", "L-spec-48501")
_write(root, "L-grader-48501.jsonl", {"ts": _iso(t_after5), "type": "spawn-started", "role": "grader",
                                       "subject": "L-spec-48501", "spawn": "L-grader-48501"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48501" for r in rows), rows

print("case grader-spawn-failed")
root, _ = _fresh_trigger_spec("g5b", "L-spec-48502")
_write(root, "L-grader-48502.jsonl", {"ts": _iso(t_after5), "type": "spawn-failed", "role": "grader",
                                       "subject": "L-spec-48502", "reason": "held:tools", "why": "x"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48502" for r in rows), rows

print("case autodispatched-role-grader")
root, _ = _fresh_trigger_spec("g5c", "L-spec-48503")
_write(root, "L-tick-local.jsonl", {"ts": _iso(t_after5), "type": "autodispatched", "role": "grader",
                                     "subject": "L-spec-48503", "spawn": "9", "since": _iso(t_after5)})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48503" for r in rows), rows

print("case autodispatch-failed-role-grader")
root, _ = _fresh_trigger_spec("g5d", "L-spec-48504")
_write(root, "L-tick-local.jsonl", {"ts": _iso(t_after5), "type": "autodispatch-failed", "role": "grader",
                                     "subject": "L-spec-48504", "reason": "worktree missing"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48504" for r in rows), rows
t_bd5d_new = NOW - datetime.timedelta(minutes=5)
_write(root, "L-builder-x2.jsonl", {"ts": _iso(t_bd5d_new), "type": "build-done", "subject": "L-spec-48504", "status": "DONE"})
_, _, rows = _gc(root)
r5d = next((r for r in rows if r["spec"] == "L-spec-48504"), None)
assert r5d is not None and r5d["since"] == _iso(t_bd5d_new), f"AC5 refresh: {r5d}"

print("case held")
root, _ = _fresh_trigger_spec("g5e", "L-spec-48505")
_write(root, "L-operator-local.jsonl", {"ts": _iso(t_after5), "type": "capability-hold",
                                         "subject": "capability:view-paths:L-spec-48505",
                                         "capability": "view-paths", "spec": "L-spec-48505"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48505" for r in rows), rows

print("case shipped")
root, _ = _fresh_trigger_spec("g5f", "L-spec-48506")
_write(root, "L-executor-local.jsonl", {"ts": _iso(t_after5), "type": "shipped",
                                         "subject": "L-spec-48506", "sha": "abc123", "branch": "l-spec-48506"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48506" for r in rows), rows

print("case killed")
root, _ = _fresh_trigger_spec("g5g", "L-spec-48507")
_write(root, "L-operator-local.jsonl", {"ts": _iso(t_after5), "type": "spec-killed", "subject": "L-spec-48507"})
_, _, rows = _gc(root)
assert not any(r["spec"] == "L-spec-48507" for r in rows), rows

print("autodispatch-0483 AC5 ok")

# ── AC6: run's grader recipe — exact calls, store round trip, idempotent,
# a failing runner leaves a standing autodispatch-failed, dry_run does nothing ─
root = _fresh("g6")
_spec_file(root, "L-spec-48601")
t0 = NOW - datetime.timedelta(hours=2)
_write(root, "L-planner-0001.jsonl", {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48601", "project": "g6"})
t_bd = NOW - datetime.timedelta(hours=1)
_write(root, "L-builder-48601.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": "L-spec-48601", "status": "DONE"})
worktree6 = root / "worktrees" / "g6" / "l-spec-48601"
worktree6.mkdir(parents=True)
ev, specs, _ = _gc(root)
detach6 = json.dumps({"detached": 777, "role": "grader", "subject": "L-spec-48601", "log": "/tmp/g6.log"})
runner6 = _FakeRunner({0: {"code": 0, "stdout": "/pkt/L-spec-48601-grader.md\n", "stderr": ""},
                        1: {"code": 0, "stdout": detach6, "stderr": ""}})
autodispatch.run(ev, specs, NOW, runner=runner6)
assert len(runner6.calls) == 2, f"AC6: {runner6.calls}"
assert runner6.calls[0] == ["doit", "packet", "grader", "L-spec-48601", "--worktree", str(worktree6)], runner6.calls[0]
assert runner6.calls[1][:5] == ["doit", "dispatch", "--detach", "grader", "L-spec-48601"], runner6.calls[1]
assert not any(c[0] == "git" for c in runner6.calls), "AC6: no git call for a grader-only dispatch"
lines6 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp6 = [e for e in lines6 if e["type"] == "autodispatched" and e.get("role") == "grader"]
assert len(disp6) == 1 and disp6[0]["spawn"] == "777" and disp6[0]["trigger"] == "build-done", disp6

ev2 = fold.read_events()
assert any(e.get("type") == "autodispatched" and e.get("role") == "grader"
           and e.get("subject") == "L-spec-48601" for e in ev2), "AC6: store round trip"
specs2, _, _, _ = fold.fold(ev2)
runner6b = _FakeRunner()
autodispatch.run(ev2, specs2, NOW, runner=runner6b)
assert runner6b.calls == [], f"AC6: second run must issue nothing further: {runner6b.calls}"

root7 = _fresh("g6b")
_spec_file(root7, "L-spec-48602")
_write(root7, "L-planner-0001.jsonl", {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48602", "project": "g6b"})
_write(root7, "L-builder-48602.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": "L-spec-48602", "status": "DONE"})
(root7 / "worktrees" / "g6b" / "l-spec-48602").mkdir(parents=True)
ev7, specs7, _ = _gc(root7)
runner7 = _FakeRunner({0: {"code": 1, "stdout": "", "stderr": "boom-packet"}})
autodispatch.run(ev7, specs7, NOW, runner=runner7)
assert len(runner7.calls) == 1, runner7.calls
lines7 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
fails7 = [e for e in lines7 if e["type"] == "autodispatch-failed" and e.get("role") == "grader"
          and e.get("subject") == "L-spec-48602"]
assert len(fails7) == 1 and fails7[0]["reason"] == "boom-packet", fails7
ev7b = fold.read_events()
specs7b, _, _, _ = fold.fold(ev7b)
runner7b = _FakeRunner()
autodispatch.run(ev7b, specs7b, NOW, runner=runner7b)
assert runner7b.calls == [], "AC6: next run issues no call after a standing autodispatch-failed"

root8 = _fresh("g6c")
_spec_file(root8, "L-spec-48603")
_write(root8, "L-planner-0001.jsonl", {"ts": _iso(t0), "type": "spec-written", "subject": "L-spec-48603", "project": "g6c"})
_write(root8, "L-builder-48603.jsonl", {"ts": _iso(t_bd), "type": "build-done", "subject": "L-spec-48603", "status": "DONE"})
(root8 / "worktrees" / "g6c" / "l-spec-48603").mkdir(parents=True)
ev8, specs8, _ = _gc(root8)


def _never6(a):
    raise AssertionError(f"AC6: dry_run must not call the runner: {a}")


autodispatch.run(ev8, specs8, NOW, runner=_never6, dry_run=True)
before8 = tick.tick_path()
assert not before8.exists() or not any(
    json.loads(l)["type"] in ("autodispatched", "autodispatch-failed") for l in before8.read_text().splitlines()), \
    "AC6: dry_run must append no event"
print("autodispatch-0483 AC6 ok")

# ── AC7: a spawn-stale-named spawn, and a waiter-dead spawn-started, both on a
# live subject inside 2h, are `dead_spawns` rows; excluded: a later verdict on
# the same spawn id, a live waiter (this test process), a different
# waiter_host with no spawn-stale ───────────────────────────────────────────
root = _fresh("d7")
sids7 = ("L-spec-48701", "L-spec-48702", "L-spec-48703", "L-spec-48704", "L-spec-48705")
for sid in sids7:
    _spec_file(root, sid)
t0 = NOW - datetime.timedelta(hours=3)
_write(root, "L-planner-0001.jsonl", *[
    {"ts": _iso(t0), "type": "spec-written", "subject": sid, "project": "d7"} for sid in sids7])
for i, sid in enumerate(sids7, 1):
    _write(root, f"L-builder-487{i}.jsonl", {"ts": _iso(t0), "type": "build-done", "subject": sid, "status": "DONE"})
_write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                                     "subject": "L-spec-48701", "spawn": "L-grader-48701", "role": "grader"})
dead7b = _dead_pid()
_write(root, "L-grader-48702.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=20)), "type": "spawn-started",
                                       "role": "grader", "subject": "L-spec-48702", "spawn": "L-grader-48702",
                                       "waiter_pid": dead7b, "waiter_host": HOST483})
_write(root, "L-tick-local2.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                                      "subject": "L-spec-48703", "spawn": "L-grader-48703", "role": "grader"})
_write(root, "L-grader-48703v.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=10)), "type": "verdict",
                                        "subject": "L-spec-48703", "spawn": "L-grader-48703"})
_write(root, "L-grader-48704.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=20)), "type": "spawn-started",
                                       "role": "grader", "subject": "L-spec-48704", "spawn": "L-grader-48704",
                                       "waiter_pid": os.getpid(), "waiter_host": HOST483})
_write(root, "L-grader-48705.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=20)), "type": "spawn-started",
                                       "role": "grader", "subject": "L-spec-48705", "spawn": "L-grader-48705",
                                       "waiter_pid": dead7b, "waiter_host": "some-other-host"})
ev7ds = fold.read_events()
rows7 = autodispatch.dead_spawns(ev7ds, NOW)
spawns7 = {r["spawn"] for r in rows7}
assert "L-grader-48701" in spawns7, rows7
assert "L-grader-48702" in spawns7, rows7
assert "L-grader-48703" not in spawns7, rows7
assert "L-grader-48704" not in spawns7, rows7
assert "L-grader-48705" not in spawns7, rows7
print("autodispatch-0483 AC7 ok")

# ── AC8: no row for a terminal subject (shipped/killed/void), a spawn-stale or
# waiter-dead start past the 2h horizon, or a non-replayable role; a 28-spawn
# backlog costs zero calls and zero events ──────────────────────────────────
root = _fresh("d8")


def _w8(name, *evs):
    _write(root, name, *evs)


print("case shipped-subject")
_spec_file(root, "L-spec-48801")
_w8("L-planner-8801.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48801", "project": "d8"})
_w8("L-tick-8801.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48801", "spawn": "L-builder-48801", "role": "builder"})
_w8("L-executor-8801.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=10)), "type": "shipped",
                              "subject": "L-spec-48801", "sha": "x", "branch": "y"})

print("case killed-subject")
_spec_file(root, "L-spec-48802")
_w8("L-planner-8802.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48802", "project": "d8"})
_w8("L-tick-8802.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48802", "spawn": "L-builder-48802", "role": "builder"})
_w8("L-operator-8802.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=10)), "type": "spec-killed",
                              "subject": "L-spec-48802"})

print("case void-subject")
_w8("L-spec-writer-8803a.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spawn-started",
                                  "subject": "L-spec-48803"})
_w8("L-spec-writer-8803b.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=4)), "type": "spawn-failed",
                                  "subject": "L-spec-48803", "reason": "x"})
_w8("L-tick-8803.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48803", "spawn": "L-builder-48803", "role": "builder"})

print("case spawn-stale-past-2h")
_spec_file(root, "L-spec-48804")
_w8("L-planner-8804.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48804", "project": "d8"})
_w8("L-tick-8804.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spawn-stale",
                          "subject": "L-spec-48804", "spawn": "L-builder-48804", "role": "builder"})

print("case waiter-dead-past-2h")
_spec_file(root, "L-spec-48805")
_w8("L-planner-8805.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48805", "project": "d8"})
dead8 = _dead_pid()
_w8("L-builder-48805.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "build-started",
                              "subject": "L-spec-48805", "spawn": "L-builder-48805",
                              "waiter_pid": dead8, "waiter_host": HOST483})

print("case role-relay")
_spec_file(root, "L-spec-48806")
_w8("L-planner-8806.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48806", "project": "d8"})
_w8("L-tick-8806.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48806", "spawn": "L-relay-48806", "role": "relay"})

print("case role-planner")
_spec_file(root, "L-spec-48807")
_w8("L-planner-8807.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48807", "project": "d8"})
_w8("L-tick-8807.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48807", "spawn": "L-planner-48807", "role": "planner"})

print("case role-reviewer")
_spec_file(root, "L-spec-48808")
_w8("L-planner-8808.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spec-written",
                             "subject": "L-spec-48808", "project": "d8"})
_w8("L-tick-8808.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=30)), "type": "spawn-stale",
                          "subject": "L-spec-48808", "spawn": "L-reviewer-48808", "role": "reviewer"})

ev8ds = fold.read_events()
rows8 = autodispatch.dead_spawns(ev8ds, NOW)
assert rows8 == [], f"AC8: expected zero rows across all eight cases: {rows8}"

# a 28-spawn backlog (15 builder, 9 spec-writer, 4 relay/planner; 16 on a
# terminal (shipped) subject) — all well past both the 10-minute and 2-hour
# windows — costs zero calls and zero new events.
root_bl = _fresh("d8backlog")
roles28 = ["builder"] * 15 + ["spec-writer"] * 9 + ["relay", "planner", "relay", "planner"]
assert len(roles28) == 28
for i, role in enumerate(roles28):
    sid = f"L-spec-489{i:02d}"
    # Invalid spec text keeps every one of these `blocked` on the SEPARATE,
    # pre-existing R1 builder-dispatch lane (shape.check fails it) — AC8 is
    # about the dead-spawn lane alone, and a real, dispatchable `written` spec
    # here would confound the assertion below with unrelated worktree/builder
    # calls that this backlog fixture was never about.
    _spec_file(root_bl, sid, text="not a real spec")
    evs_i = [{"ts": _iso(NOW - datetime.timedelta(hours=6)), "type": "spec-written",
              "subject": sid, "project": "d8backlog"}]
    if i < 16:
        evs_i.append({"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "shipped",
                      "subject": sid, "sha": "x", "branch": "y"})
    _write(root_bl, f"L-planner-89{i:02d}.jsonl", *evs_i)
    _write(root_bl, f"L-tick-89{i:02d}.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=5)), "type": "spawn-stale",
                                                "subject": sid, "spawn": f"L-{role}-89{i:02d}", "role": role})
ev_bl = fold.read_events()
specs_bl, _, _, _ = fold.fold(ev_bl)
assert autodispatch.dead_spawns(ev_bl, NOW) == [], "AC8: the 28-spawn backlog yields zero dead_spawns rows"
calls_bl = []
autodispatch.run(ev_bl, specs_bl, NOW, runner=lambda a: calls_bl.append(a) or {"code": 0, "stdout": "", "stderr": ""})
assert calls_bl == [], f"AC8: the backlog must call the runner zero times: {calls_bl}"
lines_bl = [json.loads(l) for l in tick.tick_path().read_text().splitlines()] if tick.tick_path().exists() else []
assert not any(e.get("subject", "").startswith("L-spec-489") and e["type"] in ("autodispatched", "escalation-blocking")
               for e in lines_bl), "AC8: the backlog must append zero autodispatched/escalation-blocking events"
print("autodispatch-0483 AC8 ok")

# ── AC9: replay from a dead seat spawn's own packet+cmd.json (builder,
# spec-writer, spec-auditor); seat files stay byte-unchanged; --path only
# when cmd.json carries one ──────────────────────────────────────────────────
for n9, (role9, has_path9) in enumerate((("builder", False), ("spec-writer", True), ("spec-auditor", False))):
    root = _fresh(f"d9-{n9}")
    sid9 = f"L-spec-4891{n9}"
    _spec_file(root, sid9, text="not a real spec")
    _write(root, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                           "subject": sid9, "project": f"d9-{n9}"})
    spawn9 = f"L-{role9}-91{n9}"
    _write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=5)), "type": "spawn-stale",
                                         "subject": sid9, "spawn": spawn9, "role": role9})
    body9 = "BODY LINE 1\nBODY LINE 2"
    orig_packet9 = f"WRITE PATH: /some/path.md\n\n{body9}\n\nspawn_id: {spawn9}\n"
    (dispatch.SEAT / f"{spawn9}.packet.md").write_text(orig_packet9)
    cmd_json9 = {"cmd": ["x"], "cwd": str(root / "cwd9")}
    if has_path9:
        cmd_json9["path"] = "/some/path.md"
    (dispatch.SEAT / f"{spawn9}.cmd.json").write_text(json.dumps(cmd_json9))
    ev9 = fold.read_events()
    specs9, _, _, _ = fold.fold(ev9)
    detach9 = json.dumps({"detached": 111 + n9, "role": role9, "subject": sid9, "log": "l"})
    runner9 = _FakeRunner({0: {"code": 0, "stdout": detach9, "stderr": ""}})
    autodispatch.run(ev9, specs9, NOW, runner=runner9)
    assert len(runner9.calls) == 1, runner9.calls
    replay_path9 = root / "content" / f"replay-{spawn9}.md"
    expect9 = ["doit", "dispatch", "--detach", role9, sid9, "--packet", str(replay_path9), "--cwd", str(root / "cwd9")]
    if has_path9:
        expect9 += ["--path", "/some/path.md"]
    expect9 += ["--charter", "", "--project", f"d9-{n9}"]
    assert runner9.calls[0] == expect9, runner9.calls[0]
    assert replay_path9.read_text() == f"\n{body9}", repr(replay_path9.read_text())
    assert (dispatch.SEAT / f"{spawn9}.packet.md").read_text() == orig_packet9, "AC9: seat packet must stay unchanged"
    assert json.loads((dispatch.SEAT / f"{spawn9}.cmd.json").read_text()) == cmd_json9, "AC9: cmd.json must stay unchanged"
    lines9 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
    disp9 = [e for e in lines9 if e["type"] == "autodispatched" and e.get("subject") == sid9]
    assert len(disp9) == 1 and disp9[0]["trigger"] == "redispatch" and disp9[0]["replaces"] == spawn9, disp9
print("autodispatch-0483 AC9 ok")

# ── AC10: a dead grader replay re-enters R13a's own recipe with the builder
# worktree, never the dead grader's own view/tree cwd or its stale packet; a
# held grader spec is not replayed ──────────────────────────────────────────
root = _fresh("d10")
sid10 = "L-spec-48920"
_spec_file(root, sid10)
_write(root, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid10, "project": "d10"})
_write(root, "L-builder-48920.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=2)), "type": "build-done",
                                        "subject": sid10, "status": "DONE"})
spawn10 = "L-grader-4892"
# Realistic shape: a `spawn-stale` always names a spawn that HAD its own
# `spawn-started` (tick._spawn_busy ages one out into the other) — which is
# also what already keeps `grader_candidates` from treating this same
# build-done as a fresh, un-tried trigger while `dead_spawns` replays it.
_write(root, "L-grader-4892.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=20)), "type": "spawn-started",
                                      "role": "grader", "subject": sid10, "spawn": spawn10})
_write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=5)), "type": "spawn-stale",
                                     "subject": sid10, "spawn": spawn10, "role": "grader"})
(dispatch.SEAT / f"{spawn10}.packet.md").write_text(f"STALE GRADER PACKET — must never be read\nspawn_id: {spawn10}\n")
dead_view_cwd = str(root / "view" / "tree")
(dispatch.SEAT / f"{spawn10}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": dead_view_cwd}))
worktree10 = root / "worktrees" / "d10" / sid10.lower()
worktree10.mkdir(parents=True)
ev10 = fold.read_events()
specs10, _, _, _ = fold.fold(ev10)
detach10 = json.dumps({"detached": 222, "role": "grader", "subject": sid10, "log": "l"})
runner10 = _FakeRunner({0: {"code": 0, "stdout": "/pkt/x.md\n", "stderr": ""},
                         1: {"code": 0, "stdout": detach10, "stderr": ""}})
autodispatch.run(ev10, specs10, NOW, runner=runner10)
assert len(runner10.calls) == 2, runner10.calls
assert runner10.calls[0] == ["doit", "packet", "grader", sid10, "--worktree", str(worktree10)], runner10.calls[0]
assert runner10.calls[1][:5] == ["doit", "dispatch", "--detach", "grader", sid10], runner10.calls[1]
assert str(worktree10) != dead_view_cwd, "AC10: WT must not be the dead grader's own cmd.json cwd"
assert runner10.calls[1][runner10.calls[1].index("--cwd") + 1] == str(worktree10), runner10.calls[1]
lines10 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp10 = [e for e in lines10 if e["type"] == "autodispatched" and e.get("subject") == sid10]
assert len(disp10) == 1 and disp10[0]["role"] == "grader" and disp10[0]["trigger"] == "redispatch" \
    and disp10[0]["replaces"] == spawn10, disp10

root10b = _fresh("d10b")
sid10b = "L-spec-48921"
_spec_file(root10b, sid10b)
_write(root10b, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                          "subject": sid10b, "project": "d10b"})
_write(root10b, "L-builder-48921.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=2)), "type": "build-done",
                                           "subject": sid10b, "status": "DONE"})
spawn10b = "L-grader-4893"
_write(root10b, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=5)), "type": "spawn-stale",
                                        "subject": sid10b, "spawn": spawn10b, "role": "grader"})
_write(root10b, "L-operator-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=4)), "type": "capability-hold",
                                            "subject": f"capability:view-paths:{sid10b}",
                                            "capability": "view-paths", "spec": sid10b})
(dispatch.SEAT / f"{spawn10b}.packet.md").write_text(f"x\nspawn_id: {spawn10b}\n")
(dispatch.SEAT / f"{spawn10b}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": str(root10b / "view" / "tree")}))
ev10b = fold.read_events()
specs10b, _, _, _ = fold.fold(ev10b)
runner10b = _FakeRunner()
autodispatch.run(ev10b, specs10b, NOW, runner=runner10b)
assert runner10b.calls == [], f"AC10: a held grader spec must not be replayed: {runner10b.calls}"
lines10b = [json.loads(l) for l in tick.tick_path().read_text().splitlines()] if tick.tick_path().exists() else []
assert not any(e.get("subject") == sid10b and e["type"] in ("autodispatched", "escalation-blocking", "autodispatch-failed")
               for e in lines10b), "AC10: no autodispatched/escalation-blocking/autodispatch-failed for a held grader spec"
print("autodispatch-0483 AC10 ok")

# ── AC11: a replayed spawn that itself dies escalates exactly once
# (spawn-died-twice); idempotent on repeat; a decision never reopens it; a
# dead spawn with no cmd.json escalates at once with the same kind ─────────
root = _fresh("d11")
sid11 = "L-spec-48930"
_spec_file(root, sid11, text="not a real spec")
_write(root, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid11, "project": "d11"})
spawn11a = "L-builder-4894"
_write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=5)), "type": "spawn-stale",
                                     "subject": sid11, "spawn": spawn11a, "role": "builder"})
(dispatch.SEAT / f"{spawn11a}.packet.md").write_text(f"BODY\n\nspawn_id: {spawn11a}\n")
(dispatch.SEAT / f"{spawn11a}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": str(root / "wt11")}))
ev11 = fold.read_events()
specs11, _, _, _ = fold.fold(ev11)
spawn11b = "8801"
runner11 = _FakeRunner({0: {"code": 0, "stdout": json.dumps({"detached": spawn11b, "role": "builder",
                                                              "subject": sid11, "log": "l"}), "stderr": ""}})
autodispatch.run(ev11, specs11, NOW, runner=runner11)
lines11 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp11a = [e for e in lines11 if e["type"] == "autodispatched" and e.get("replaces") == spawn11a]
assert len(disp11a) == 1 and disp11a[0]["spawn"] == spawn11b, disp11a

ev11b = fold.read_events()
specs11b, _, _, _ = fold.fold(ev11b)
runner11b = _FakeRunner()
autodispatch.run(ev11b, specs11b, NOW, runner=runner11b)
assert runner11b.calls == [], f"AC11: must not replay the same dead spawn twice: {runner11b.calls}"

_write(root, "L-tick-local2.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=3)), "type": "spawn-stale",
                                      "subject": sid11, "spawn": spawn11b, "role": "builder"})
ev11c = fold.read_events()
specs11c, _, _, _ = fold.fold(ev11c)
runner11c = _FakeRunner()
autodispatch.run(ev11c, specs11c, NOW, runner=runner11c)
assert runner11c.calls == [], f"AC11: the second-death step calls the runner zero times: {runner11c.calls}"
lines11c = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
esc11 = [e for e in lines11c if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn11b]
assert len(esc11) == 1 and esc11[0]["kind"] == "spawn-died-twice" and esc11[0]["owner"] == "thinker" \
    and esc11[0]["role"] == "builder" and esc11[0].get("default") and esc11[0].get("deadline") \
    and esc11[0].get("revert"), esc11
assert fold.required_reason(esc11[0]) is None, fold.required_reason(esc11[0])

ev11d = fold.read_events()
specs11d, _, _, _ = fold.fold(ev11d)
runner11d = _FakeRunner()
autodispatch.run(ev11d, specs11d, NOW, runner=runner11d)
lines11d = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
esc11d = [e for e in lines11d if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn11b]
assert len(esc11d) == 1, esc11d
assert runner11d.calls == [], runner11d.calls

_write(root, "L-operator-answer.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=1)), "type": "decision",
                                          "subject": sid11, "why": "ack"})
ev11e = fold.read_events()
specs11e, _, _, _ = fold.fold(ev11e)
runner11e = _FakeRunner()
autodispatch.run(ev11e, specs11e, NOW, runner=runner11e)
lines11e = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
esc11e = [e for e in lines11e if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn11b]
assert len(esc11e) == 1, esc11e
assert runner11e.calls == [], runner11e.calls

sid11f = "L-spec-48931"
_spec_file(root, sid11f, text="not a real spec")
_write(root, "L-planner-0002.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid11f, "project": "d11"})
spawn11f = "L-builder-4895"
_write(root, "L-tick-local3.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=2)), "type": "spawn-stale",
                                      "subject": sid11f, "spawn": spawn11f, "role": "builder"})
ev11f = fold.read_events()
specs11f, _, _, _ = fold.fold(ev11f)
runner11f = _FakeRunner()
autodispatch.run(ev11f, specs11f, NOW, runner=runner11f)
assert runner11f.calls == [], runner11f.calls
lines11f = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
esc11f = [e for e in lines11f if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn11f]
assert len(esc11f) == 1 and esc11f[0]["kind"] == "spawn-died-twice", esc11f
print("autodispatch-0483 AC11 ok")

# ── AC12: the 10-minute redispatch window; a failed replay escalates once ───
root = _fresh("d12")
sid12a = "L-spec-48940"
_spec_file(root, sid12a, text="not a real spec")
_write(root, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid12a, "project": "d12"})
spawn12a = "L-builder-4896"
_write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=9)), "type": "spawn-stale",
                                     "subject": sid12a, "spawn": spawn12a, "role": "builder"})
(dispatch.SEAT / f"{spawn12a}.packet.md").write_text(f"BODY\n\nspawn_id: {spawn12a}\n")
(dispatch.SEAT / f"{spawn12a}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": str(root / "wt12a")}))

sid12b = "L-spec-48941"
_spec_file(root, sid12b, text="not a real spec")
_write(root, "L-planner-0002.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid12b, "project": "d12"})
spawn12b = "L-builder-4897"
_write(root, "L-tick-local2.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=11)), "type": "spawn-stale",
                                      "subject": sid12b, "spawn": spawn12b, "role": "builder"})
(dispatch.SEAT / f"{spawn12b}.packet.md").write_text(f"BODY\n\nspawn_id: {spawn12b}\n")
(dispatch.SEAT / f"{spawn12b}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": str(root / "wt12b")}))

ev12 = fold.read_events()
specs12, _, _, _ = fold.fold(ev12)
runner12 = _FakeRunner({0: {"code": 0, "stdout": json.dumps({"detached": 9001, "role": "builder",
                                                              "subject": sid12a, "log": "l"}), "stderr": ""}})
autodispatch.run(ev12, specs12, NOW, runner=runner12)
assert len(runner12.calls) == 1, runner12.calls
lines12 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp12a = [e for e in lines12 if e["type"] == "autodispatched" and e.get("replaces") == spawn12a]
assert len(disp12a) == 1, disp12a
esc12b = [e for e in lines12 if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn12b]
assert len(esc12b) == 1 and esc12b[0]["kind"] == "spawn-redispatch-lapsed", esc12b

sid12c = "L-spec-48942"
_spec_file(root, sid12c, text="not a real spec")
_write(root, "L-planner-0003.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=3)), "type": "spec-written",
                                       "subject": sid12c, "project": "d12"})
spawn12c = "L-builder-4898"
_write(root, "L-tick-local3.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=2)), "type": "spawn-stale",
                                      "subject": sid12c, "spawn": spawn12c, "role": "builder"})
(dispatch.SEAT / f"{spawn12c}.packet.md").write_text(f"BODY\n\nspawn_id: {spawn12c}\n")
(dispatch.SEAT / f"{spawn12c}.cmd.json").write_text(json.dumps({"cmd": ["x"], "cwd": str(root / "wt12c")}))
ev12c = fold.read_events()
specs12c, _, _, _ = fold.fold(ev12c)
runner12c = _FakeRunner({0: {"code": 1, "stdout": "", "stderr": "boom"}})
autodispatch.run(ev12c, specs12c, NOW, runner=runner12c)
lines12c = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
escf12c = [e for e in lines12c if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn12c]
assert len(escf12c) == 1 and escf12c[0]["kind"] == "spawn-redispatch-failed", escf12c
ev12d = fold.read_events()
specs12d, _, _, _ = fold.fold(ev12d)
runner12d = _FakeRunner()
autodispatch.run(ev12d, specs12d, NOW, runner=runner12d)
assert runner12d.calls == [], runner12d.calls
lines12d = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
escf12d = [e for e in lines12d if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn12c]
assert len(escf12d) == 1, "AC12: no second escalation on the next run"
print("autodispatch-0483 AC12 ok")

# ── AC13: lane effect — an open spawn-died-twice escalation blocks the spec
# (reason starting "busy"); a decision clears it ────────────────────────────
root = _fresh("d13")
sid13 = "L-spec-48950"
_spec_file(root, sid13)
_write(root, "L-planner-0001.jsonl", {"ts": _iso(NOW - datetime.timedelta(hours=2)), "type": "spec-written",
                                       "subject": sid13, "project": "d13"})
_write(root, "L-tick-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=5)), "type": "escalation-blocking",
                                     "subject": sid13, "kind": "spawn-died-twice", "owner": "thinker",
                                     "role": "builder", "dead_spawn": "L-builder-9999", "default": "x",
                                     "deadline": _iso(NOW + datetime.timedelta(hours=24)), "revert": "y"})
ev13 = fold.read_events()
specs13, _, _, _ = fold.fold(ev13)
rows13 = autodispatch.candidates(ev13, specs13, NOW)
r13 = _row(rows13, sid13)
assert r13["status"] == "blocked" and (r13["reason"] or "").startswith("busy"), r13
_write(root, "L-operator-local.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=1)), "type": "decision",
                                         "subject": sid13, "why": "ack"})
ev13b = fold.read_events()
specs13b, _, _, _ = fold.fold(ev13b)
rows13b = autodispatch.candidates(ev13b, specs13b, NOW)
r13b = _row(rows13b, sid13)
assert r13b["status"] == "dispatchable", r13b
print("autodispatch-0483 AC13 ok")

# ── AC15: executor.md's tick-owns-it + rework=yes sentences, and the removal
# of the Executor's own spawn-stale re-dispatch instruction ────────────────
exec_md_483 = (REPO_ROOT / "agents" / "executor.md").read_text()
assert "is the tick's" in exec_md_483 and "(`autodispatch.run`, L-charter-0042 R13a)" in exec_md_483, \
    "AC15: tick-owns-it sentence missing"
assert "carries `rework=yes`, and the tick" in exec_md_483, "AC15: rework=yes sentence missing"
assert "then waits for that rework's own `build-done` before it dispatches." in exec_md_483, \
    "AC15: rework=yes sentence missing its own clause"
assert "spawn-stale` carrying no `reason`" not in exec_md_483, \
    "AC15: the old Executor-side spawn-stale re-dispatch instruction must be gone"
print("autodispatch-0483 AC15 ok")
