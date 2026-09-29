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
