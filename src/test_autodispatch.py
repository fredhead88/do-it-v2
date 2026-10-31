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
import subprocess
import unittest
from unittest.mock import patch


class WorktreeRecordTests(unittest.TestCase):
    def test_worktree_record(self):
        with tempfile.TemporaryDirectory(prefix='worktree-record-') as directory:
            root = pathlib.Path(directory)
            repo, wt = root / 'repo', root / 'worktrees/proj/l-spec-9001'
            repo.mkdir()
            wt.parent.mkdir(parents=True)

            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                      capture_output=True, text=True)

            git('init', '-q', '-b', 'seed')
            (repo / 'seed').write_text('fixture')
            git('add', 'seed')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'seed')
            with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'), \
                 patch.dict(os.environ, DOIT_LEDGER_FILE='L-tick-local.jsonl'):
                failed = autodispatch.add_builder_worktree(repo, wt, 'l-spec-9001', 'seed',
                    runner=lambda argv: dict(code=1, stdout='', stderr='fixture failure'), reuse_branch=False)
                self.assertEqual(failed['code'], 1)
                self.assertFalse((root / 'events').exists())
                result = autodispatch.add_builder_worktree(repo, wt, 'l-spec-9001', 'seed')
                self.assertEqual(result['code'], 0)
                # Reusing a checkout neither runs git nor creates a second record.
                with patch.object(autodispatch, '_real_runner', side_effect=AssertionError('reused git')):
                    self.assertEqual(autodispatch.add_builder_worktree(repo, wt, 'l-spec-9001', 'seed')['code'], 0)
                # A new process opens its own ledger handle with the real reader.
                env = dict(os.environ, DOIT_ROOT=str(root))
                child = subprocess.run([sys.executable, '-c',
                    'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'],
                    cwd=pathlib.Path(__file__).resolve().parent, env=env,
                    capture_output=True, text=True, check=True)
                rows = json.loads(child.stdout)
                self.assertEqual(len(rows), 1)
                self.assertEqual({k: rows[0][k] for k in ('type', 'kind', 'ref', 'lane', 'subject')},
                                 dict(type='resource-created', kind='worktree', ref=str(wt),
                                      lane='build', subject='L-spec-9001'))
                import hashlib
                body = json.dumps(rows, sort_keys=True)
                print(json.dumps(dict(url='cli://autodispatch/AC6', status=0,
                                      body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


if __name__ == '__main__' and '-k' in sys.argv and sys.argv[-1] == 'worktree_record':
    unittest.main()

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

## Metadata
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

# ── wave waiver: a decision with wave_waive=yes lifts the hold for that subject only
_write(root, "L-thinker-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "decision", "subject": "L-spec-2002",
        "project": "proj2", "wave_waive": "yes", "why": "its producers merged"})
_, _, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-2002")
assert r["status"] == "dispatchable", f"wave waiver: expected dispatchable, got {r}"
print("autodispatch wave-waive ok")
_write(root, "L-thinker-0001.jsonl",
       {"v": 1, "ts": _iso(NOW + datetime.timedelta(seconds=1)), "type": "decision", "subject": "L-spec-2002",
        "project": "proj2", "wave_waive": "no", "why": "revoked"})
_, _, rows = _fold_and_candidates(root)
assert _row(rows, "L-spec-2002")["status"] == "blocked", "wave waiver: a later wave_waive=no must restore the hold"
print("autodispatch wave-waive revoke ok")

# ── AC3: shape.check block non-empty -> blocked, never dispatched ──────────
root = _fresh("proj3")
_spec_file(root, "L-spec-3001", text="not a real spec")
_write(root, "L-planner-0001.jsonl",
       {"v": 1, "ts": _iso(NOW), "type": "spec-written", "subject": "L-spec-3001",
        "project": "proj3"})
ev, specs, rows = _fold_and_candidates(root)
r = _row(rows, "L-spec-3001")
assert r["status"] == "blocked" and (r["reason"] or "").startswith("shape:"), f"AC3: {r}"
# Launch eligibility now belongs to next_steps; this checks the retained reader.
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


# Routine build transport and capacity now run through tick.launch in
# test_tick.py and test_lane_driver.py; candidates remains a read-only view.

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
vanished_lines = [json.loads(l) for l in tick.tick_path().read_text().splitlines() if '"spawn-failed"' in l]
assert len(vanished_lines) == 1 and vanished_lines[0]['reason'] == 'vanished'
admitted16 = [e for e in fold.read_events(use_cache=False) if e['type'] == 'spawn-failed']
assert len(admitted16) == 1 and admitted16[0]['actor'] == 'tick'
assert fold.admits(admitted16[0], admitted16[0]['actor']) is None
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


assert not hasattr(autodispatch, 'grader_' + 'candidates')

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
autodispatch.redispatch_dead(ev_bl, specs_bl, NOW, runner=lambda a: calls_bl.append(a) or {"code": 0, "stdout": "", "stderr": ""})
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
    autodispatch.redispatch_dead(ev9, specs9, NOW, runner=runner9)
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
# also what already keeps the old grader diagnostic from treating this same
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
autodispatch.redispatch_dead(ev10, specs10, NOW, runner=runner10)
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
autodispatch.redispatch_dead(ev10b, specs10b, NOW, runner=runner10b)
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
autodispatch.redispatch_dead(ev11, specs11, NOW, runner=runner11)
lines11 = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
disp11a = [e for e in lines11 if e["type"] == "autodispatched" and e.get("replaces") == spawn11a]
assert len(disp11a) == 1 and disp11a[0]["spawn"] == spawn11b, disp11a

ev11b = fold.read_events()
specs11b, _, _, _ = fold.fold(ev11b)
runner11b = _FakeRunner()
autodispatch.redispatch_dead(ev11b, specs11b, NOW, runner=runner11b)
assert runner11b.calls == [], f"AC11: must not replay the same dead spawn twice: {runner11b.calls}"

_write(root, "L-tick-local2.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=3)), "type": "spawn-stale",
                                      "subject": sid11, "spawn": spawn11b, "role": "builder"})
ev11c = fold.read_events()
specs11c, _, _, _ = fold.fold(ev11c)
runner11c = _FakeRunner()
autodispatch.redispatch_dead(ev11c, specs11c, NOW, runner=runner11c)
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
autodispatch.redispatch_dead(ev11d, specs11d, NOW, runner=runner11d)
lines11d = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
esc11d = [e for e in lines11d if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn11b]
assert len(esc11d) == 1, esc11d
assert runner11d.calls == [], runner11d.calls

_write(root, "L-operator-answer.jsonl", {"ts": _iso(NOW - datetime.timedelta(minutes=1)), "type": "decision",
                                          "subject": sid11, "why": "ack"})
ev11e = fold.read_events()
specs11e, _, _, _ = fold.fold(ev11e)
runner11e = _FakeRunner()
autodispatch.redispatch_dead(ev11e, specs11e, NOW, runner=runner11e)
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
autodispatch.redispatch_dead(ev11f, specs11f, NOW, runner=runner11f)
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
autodispatch.redispatch_dead(ev12, specs12, NOW, runner=runner12)
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
autodispatch.redispatch_dead(ev12c, specs12c, NOW, runner=runner12c)
lines12c = [json.loads(l) for l in tick.tick_path().read_text().splitlines()]
escf12c = [e for e in lines12c if e["type"] == "escalation-blocking" and e.get("dead_spawn") == spawn12c]
assert len(escf12c) == 1 and escf12c[0]["kind"] == "spawn-redispatch-failed", escf12c
ev12d = fold.read_events()
specs12d, _, _, _ = fold.fold(ev12d)
runner12d = _FakeRunner()
autodispatch.redispatch_dead(ev12d, specs12d, NOW, runner=runner12d)
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


def test_dependency_holds():
    from unittest import mock

    sid = "L-spec-1043"
    t0 = NOW - datetime.timedelta(hours=3)
    t1 = NOW - datetime.timedelta(hours=2)
    t2 = NOW - datetime.timedelta(hours=1)
    hold = {"type": "dependency-hold", "subject": sid, "ts": _iso(t1),
            "waits_for": "L-spec-9002", "why": "needs producer",
            "_src": "L-thinker-0001.jsonl:1"}
    reason = "held: waits for L-spec-9002 (needs producer) [L-thinker-0001.jsonl:1]"
    held = autodispatch.held_by_dependency
    assert held([], []) is None
    assert held([hold], [hold]) == reason
    print("dependency-hold AC3 ok")

    for shipped_at in (t0, t2):
        shipped = {"type": "shipped", "subject": "L-spec-9002", "ts": _iso(shipped_at)}
        assert held([hold], [hold, shipped]) is None
    for kind in ("unblocked", "decision"):
        for released_at in (t0, t1, t2):
            release = {"type": kind, "subject": sid, "ts": _iso(released_at),
                       "actor": "thinker", "ref": hold['_src']}
            expected = None if released_at > t1 else reason
            assert held([hold, release], [hold, release]) == expected
    other_ship = {"type": "shipped", "subject": "L-spec-9003", "ts": _iso(t2)}
    assert held([hold, {"type": "decision", "subject": sid, "ts": _iso(t2),
                        "actor": "thinker"}], []) == reason
    assert held([hold], [hold, other_ship]) == reason
    print("dependency-hold AC4 ok")

    self_shipped = {"type": "shipped", "subject": sid, "ts": _iso(t2)}
    for invalid in ("", None, sid):
        bad = {**hold, "waits_for": invalid}
        if invalid is None:
            del bad["waits_for"]
        assert "unresolvable dependency-hold" in held([bad], [bad, self_shipped])
        for kind in ("unblocked", "decision"):
            release = {"type": kind, "subject": sid, "ts": _iso(t2),
                       "actor": "operator", "ref": hold['_src']}
            assert held([bad, release], [bad, release, self_shipped]) is None
    older = {**hold, "waits_for": "L-spec-9003", "ts": _iso(t0)}
    # Deliberately reverse ledger order: timestamp alone chooses the hold.
    assert held([hold, older], [hold, older, other_ship]) == reason
    assert held([older, hold], [hold, older, other_ship]) == reason
    # Lexical order differs from time order with these timezone offsets.
    offset_older = {**older, "ts": "2026-10-15T12:00:00+02:00"}
    utc_newer = {**hold, "ts": "2026-10-15T11:00:00+00:00"}
    assert held([utc_newer, offset_older], [other_ship]) == reason
    print("dependency-hold AC5 ok")

    with mock.patch.multiple(fold, ROOT=fold.ROOT, EVENTS=fold.EVENTS), \
            mock.patch.multiple(dispatch, ROOT=dispatch.ROOT, EVENTS=dispatch.EVENTS, CONTENT=dispatch.CONTENT):
        root = _dispatchable_fixture("dependency-hold", sid, "hold-project", t0)
        _write(root, "L-thinker-0001.jsonl", hold)
        ev, specs, rows = _fold_and_candidates(root)
        row = _row(rows, sid)
        assert row["status"] == "blocked" and row["reason"] == held(specs[sid]["evs"], ev)
        runner = _FakeRunner()
        assert tick.run_steps(ev, specs, NOW, dispatcher=runner)[0].startswith('hold ')
        assert runner.calls == []
        assert not any(e["type"] in ("autodispatched", "autodispatch-failed")
                       and e.get("subject") == sid for e in fold.read_events())
        print("dependency-hold AC7 ok")

        failure = {"type": "autodispatch-failed", "subject": sid, "ts": _iso(t0), "reason": "old failure"}
        assert autodispatch._standing_autodispatch_failed([failure, hold]) is None
        newer_failure = {**failure, "ts": _iso(t2)}
        assert autodispatch._standing_autodispatch_failed([hold, newer_failure]) == newer_failure
        _write(root, "L-tick-local.jsonl", newer_failure)
        ev, specs, rows = _fold_and_candidates(root)
        assert _row(rows, sid)["reason"] == reason
        assert _row(rows, sid)["_dispatch_failed"] is False
        print("dependency-hold AC8 autodispatch ok")

        # A hold clears a prior failure without moving the original queue marker.
        _write(root, "L-tick-local.jsonl", failure)
        _write(root, "L-executor-0001.jsonl",
               {"type": "shipped", "subject": "L-spec-9002", "ts": _iso(t2)})
        _, _, rows = _fold_and_candidates(root)
        row = _row(rows, sid)
        assert row["status"] == "dispatchable" and row["reason"] is None
        assert row["since"] == _iso(t0)
        print("dependency-hold AC6 ok")


test_dependency_holds()


def test_dispatcher_base_1134():
    import hashlib
    from unittest import mock

    def signed(ac, body):
        print(json.dumps({"criterion": ac, "url": "cli://autodispatch/" + ac, "status": 0,
                          "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                          "body_excerpt": body}))

    with tempfile.TemporaryDirectory() as temporary:
        root = pathlib.Path(temporary)
        (root / "content").mkdir()
        (root / "events").mkdir()
        (root / "repos" / "fixture").mkdir(parents=True)
        with mock.patch.multiple(fold, ROOT=root, EVENTS=root / "events"), \
                mock.patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"):
            with mock.patch.dict(os.environ):
                for caller in (None, str(root / "L-executor-0001.jsonl")):
                    if caller is None:
                        os.environ.pop("DOIT_LEDGER_FILE", None)
                    else:
                        os.environ["DOIT_LEDGER_FILE"] = caller
                    parent = dict(os.environ)
                    with mock.patch.object(autodispatch.subprocess, "run") as child:
                        child.return_value.returncode = 0
                        child.return_value.stdout = "ok"
                        child.return_value.stderr = ""
                        for argv in (["git", "status"], ["doit", "packet", "builder", "L-spec-9134"],
                                     ["doit", "dispatch", "--detach", "builder", "L-spec-9134"]):
                            assert autodispatch._real_runner(argv)["code"] == 0
                            assert child.call_args.kwargs["env"]["DOIT_LEDGER_FILE"] == str(tick.tick_path())
                            expected = [autodispatch.DOIT_BIN] + argv[1:] if argv[0] == "doit" else argv
                            assert child.call_args.args[0] == expected
                    assert dict(os.environ) == parent
            signed("AC5", {"children": 6, "tick_ledger": True, "parent_unchanged": True})
            print("base AC5 ok")

            sid = "L-spec-9134"
            _spec_file(root, sid, CLEAN_SPEC + "\nBase: realm\n")
            _write(root, "L-planner-0001.jsonl", {"v": 1, "type": "spec-written", "subject": sid,
                                               "project": "fixture", "ts": _iso(NOW)})
            events = fold.read_events()
            specs, *_ = fold.fold(events)
            head = mock.Mock(return_value="main")
            row = _row(autodispatch.candidates(events, specs, NOW, head_of=head), sid)
            reason = "base realm: the Executor cuts this worktree by hand"
            assert row["status"] == "blocked" and row["reason"] == reason, row
            head.assert_called_once_with("fixture")
            signed("AC6", {"status": row["status"], "reason": row["reason"], "packet_or_dispatch_calls": 0})
            print("base AC6 ok")

            for text, expected_reads in ((CLEAN_SPEC, 0), (CLEAN_SPEC + "\nBase: main\n", 1),
                                         (CLEAN_SPEC + "\nBase: SD1 — master after L-spec-0652 and L-spec-0636 merge ...\n", 0)):
                _spec_file(root, sid, text)
                head = mock.Mock(return_value="main")
                row = _row(autodispatch.candidates(events, specs, NOW, head_of=head), sid)
                assert row["status"] == "dispatchable" and row["reason"] is None, row
                assert head.call_count == expected_reads
            _spec_file(root, sid, CLEAN_SPEC + "\nBase: realm\nBase: main\n")
            row = _row(autodispatch.candidates(events, specs, NOW, head_of=lambda project: None), sid)
            assert row["status"] == "blocked" and row["reason"] == reason
            # Multiple specs in the same project share one HEAD read.
            other = "L-spec-9135"
            _spec_file(root, other, CLEAN_SPEC + "\nBase: realm\n")
            specs[other] = {**specs[sid], "id": other}
            head = mock.Mock(return_value="realm")
            assert all(row["status"] == "dispatchable" for row in autodispatch.candidates(events, specs, NOW, head_of=head))
            head.assert_called_once_with("fixture")
            # The default reader fails closed on git failure or an OS error.
            for effect in (None, OSError("fixture")):
                with mock.patch.object(autodispatch.subprocess, "run", side_effect=effect) as git:
                    git.return_value.returncode = 1
                    assert all(row["reason"] == reason for row in autodispatch.candidates(events, specs, NOW))
                    git.assert_called_once_with(["git", "-C", str(root / "repos" / "fixture"),
                                                 "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True)
            # Earlier reasons retain priority, without reading HEAD.
            hold = {"type": "dependency-hold", "subject": sid, "waits_for": "L-spec-9002", "ts": _iso(NOW)}
            specs[sid]["evs"] = [*specs[sid]["evs"], hold]
            with mock.patch.object(autodispatch.subprocess, "run") as git:
                row = _row(autodispatch.candidates(events + [hold], {sid: specs[sid]}, NOW), sid)
                assert row["reason"].startswith("held:")
                git.assert_not_called()
            (root / "content" / f"{other}.md").unlink()
            row = _row(autodispatch.candidates(events, {other: specs[other]}, NOW, head_of=head), other)
            assert row["reason"] == "spec file unreadable"
            signed("AC7", {"no_base": "dispatchable", "matching_base": "dispatchable", "prose": "dispatchable",
                           "unknown_head": "blocked", "head_reads_per_project": 1, "earlier_reasons_preserved": True})
            print("base AC7 ok")


if __name__ == "__main__":
    test_dispatcher_base_1134()

# L-spec-1137 AC10/AC13/AC16: released waves use the ordinary builder recipe,
# independent of whether their first action is already past the hold limit.
from unittest.mock import patch
import lane_rules
for released_minutes in (11, 5):
    root = _fresh('released')
    sid, blocker = 'L-spec-1137', 'L-spec-1136'
    charter = 'L-charter-0052'
    (root / 'content' / f'plan-{charter}.md').write_text(
        '## Unit A\nFootprint:\n- src/producer.py\nWave: 1\n\n'
        '## Unit B\nFootprint:\n- src/consumer.py\nWave: 2\n')
    _spec_file(root, sid)
    _spec_file(root, blocker)
    _write(root, 'L-planner-local.jsonl', *[
        dict(v=1, type='spec-written', ts=_iso(NOW - datetime.timedelta(hours=2)),
             subject=subject, charter=charter, project='released', footprint=[footprint])
        for subject, footprint in ((blocker, 'src/producer.py'), (sid, 'src/consumer.py'))])
    _write(root, 'L-executor-local.jsonl',
           dict(v=1, type='shipped', subject=blocker,
                ts=_iso(NOW - datetime.timedelta(minutes=released_minutes))))
    ev = fold.read_events(use_cache=False)
    specs = fold.fold(ev)[0]
    table = root / 'limits.toml'
    table.write_text(lane_rules.DEFAULT_TABLE.read_text().replace('limit_min = 30', 'limit_min = 45'))
    calls = []
    def released_runner(argv):
        calls.append(argv)
        if argv[:2] == ['doit', 'packet']:
            stdout = '/packet.md'
        elif argv[:2] == ['doit', 'dispatch']:
            stdout = json.dumps({'detached': 'L-builder-9999'})
        else:
            stdout = 'master'
            if 'add' in argv:
                pathlib.Path(argv[5]).mkdir(parents=True)
        return dict(code=0, stdout=stdout, stderr='')
    with patch.dict(os.environ, {'DOIT_LANE_RULES': str(table)}):
        rows = autodispatch.candidates(ev, specs, NOW, head_of=lambda _: 'master')
        assert _row(rows, sid)['limit_min'] == 45, rows
        out = tick.run_steps(ev, specs, NOW, dispatcher=released_runner)
    dispatches = [argv for argv in calls if argv[:2] == ['doit', 'dispatch']]
    assert len(dispatches) == 1 and dispatches[0][3:5] == ['builder', sid], calls
    written = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
    launches = [e for e in written if e['type'] == 'autodispatched']
    assert len(launches) == 1 and launches[0]['rule'] == 'build', written
    assert any(e['type'] == 'resource-created' for e in fold.read_events(use_cache=False))
    assert len(out) == 1 and out[0].startswith('acted '), out
print('lane-driver-1137 AC10 AC13 AC16 released-wave first builds ok')


class HeldCandidateTests(unittest.TestCase):
    def test_enforce_AC16_candidates_and_idle(self):
        import loadgate as lg
        import look
        import hashlib
        old = fold.ROOT,fold.EVENTS,dispatch.ROOT,dispatch.EVENTS,dispatch.CONTENT
        self.addCleanup(lambda: self.restore(old))
        root = _fresh('held')
        self.addCleanup(__import__('shutil').rmtree,root)
        now = datetime.datetime(2026,10,27,tzinfo=datetime.timezone.utc)
        ago = lambda minutes: _iso(now-datetime.timedelta(minutes=minutes))
        a,b,c,d,e,f = 'L-spec-9801','L-spec-9802','L-spec-'+'x'*100,'L-spec-9804','L-spec-9805','L-spec-9806'
        events = [dict(type='spec-written',subject=sid,ts=ago(age),project='held',actor='operator')
                  for sid,age in ((a,41),(b,40),(c,39),(d,90),(e,90),(f,90))]
        for sid in (a,b,c,d,e,f):
            _spec_file(root,sid)
        for sid in (d,e):
            events += [dict(type=kind,subject=sid,ts=ago(age),actor='builder',role='builder',
                            spawn='L-builder-'+sid,status='DONE')
                       for kind,age in (('build-started',85),('spawn-started',84),('spawn-done',71),('build-done',70))]
        events += [dict(type='verdict',subject=e,ts=ago(80),verdict='REWORK',actor='grader'),
                   dict(type='shipped',subject=f,ts=ago(60),actor='executor')]
        specs = fold.fold(events)[0]
        self.assertEqual((specs[d]['state'],specs[e]['state']),('graded','reviewing'))
        state_path = root / 'state/admission.json'
        opt = root / 'admission.toml'
        opt.write_text('mode="enforce"')
        state = lg._fresh() | dict(t=now.timestamp()-60,holding={lg.hold_key(sid):ago(30) for sid in (a,c,d,e,f)})
        lg.save_state(state_path,state)
        def rows():
            return autodispatch.candidates(events,specs,now)
        def idle(sid,rows):
            evs = [ev for ev in events if ev.get('subject') == sid]
            # SD2 idle fixtures carry classified buckets, not candidate status.
            classified = {r['spec']: dict(r, bucket='queued' if r['status']=='seat-wait' else 'STUCK')
                          for r in rows}
            return look._spec_idle_bad(sid,specs[sid],evs,events,classified,set(),set(),now)
        with patch.object(autodispatch,'capacity',return_value=1):
            result = rows()
            for sid in (a,c,d,e):
                r = _row(result,sid)
                self.assertEqual((r['status'],r['reason']),('seat-wait','resource-wait'))
            self.assertEqual(_row(result,b)['status'],'dispatchable')
            self.assertNotIn(f,[r['spec'] for r in result])
            self.assertFalse(idle(d,result))
            self.assertFalse(idle(e,result))
            self.assertEqual([r['spec'] for r in result][-2:],[d,e])
            self.assertEqual(_row(result,d)['since'],_iso(now))
            for fault in ('missing','stale','helper','unnamed'):
                with __import__('contextlib').ExitStack() as stack:
                    if fault == 'missing':
                        opt.unlink()
                    elif fault == 'stale':
                        lg.save_state(state_path,state | dict(t=now.timestamp()-3660))
                    elif fault == 'helper':
                        stack.enter_context(patch.object(lg,'held_items',side_effect=RuntimeError('failed')))
                    else:
                        lg.save_state(state_path,state | dict(holding={}))
                    result = rows()
                    self.assertEqual(_row(result,a)['status'],'dispatchable')
                    self.assertEqual(_row(result,b)['status'],'seat-wait')
                    self.assertNotIn(d,[r['spec'] for r in result])
                    self.assertTrue(idle(d,result))
                opt.write_text('mode="enforce"')
                lg.save_state(state_path,state)
            events.append(dict(type='autodispatch-failed',subject=a,ts=ago(1),reason='fixture refused',role='builder',actor='tick'))
            specs = fold.fold(events)[0]
            result = rows()
            self.assertEqual(_row(result,a)['status'],'blocked')
            self.assertTrue(_row(result,a)['_dispatch_failed'])
        for age in (3660,-301):
            lg.save_state(state_path,state | dict(t=now.timestamp()-age))
            self.assertEqual(lg.held_items(root,now=now),frozenset())
        state_path.write_text('bad')
        self.assertFalse(lg.held_items(root,now=now))
        state_path.unlink()
        self.assertFalse(lg.held_items(root,now=now))
        lg.save_state(state_path,state)
        self.assertFalse(lg.held_items(root,now=now.replace(tzinfo=None)))
        opt.write_text('mode="observe"')
        self.assertFalse(lg.held_items(root,now=now))
        body = json.dumps(dict(held_build_preserves_seat=True,held_grades_suppress_idle=True,
                               stale_restores_alarm=True,hashed_key=True,blocked_preserved=True),sort_keys=True)
        print(json.dumps(dict(url='cli://autodispatch/AC16-1649',status=0,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),body_excerpt=body)))

    @staticmethod
    def restore(old):
        fold.ROOT,fold.EVENTS,dispatch.ROOT,dispatch.EVENTS,dispatch.CONTENT = old

class AuditCandidateTests(unittest.TestCase):
    def test_ac4_unaudited_never_uses_builder_slot(self):
        old = fold.ROOT, fold.EVENTS, dispatch.ROOT, dispatch.EVENTS, dispatch.CONTENT
        self.addCleanup(HeldCandidateTests.restore, old)
        root = _fresh()
        now = fold.NOW
        for n in (9673, 9674, 9675):
            sid = f'L-spec-{n}'
            _spec_file(root, sid)
            with patch.object(dispatch, 'now', return_value=_iso(now - datetime.timedelta(minutes=90 - (n-9673)))):
                self.assertIsNone(dispatch.emit(root / 'events' / 'L-spec-writer-9673.jsonl', {}, 'spec-written', subject=sid, project='proj'))
        dispatch.emit(root / 'events' / 'L-spec-auditor-9673.jsonl', {}, 'spawn-done', subject='L-spec-9674', bad_cut=False)
        dispatch.emit(root / 'events' / 'L-operator-9673.jsonl', {}, 'spec-carried', subject='L-spec-9675', source='fixture', tier='fixture', audited_at=now.isoformat())
        with patch.object(autodispatch, 'capacity', return_value=1):
            events = fold.read_events(use_cache=False)
            rows = autodispatch.candidates(events, fold.fold(events)[0], now)
            self.assertEqual(_row(rows, 'L-spec-9673')['status'], 'blocked')
            self.assertTrue(_row(rows, 'L-spec-9673')['reason'].startswith('unaudited'))
            self.assertEqual(_row(rows, 'L-spec-9674')['status'], 'dispatchable')
            self.assertEqual(_row(rows, 'L-spec-9675')['status'], 'seat-wait')
            dispatch.emit(root / 'events' / 'L-spec-auditor-9673.jsonl', {}, 'spawn-done', subject='L-spec-9673', bad_cut=False)
            events = fold.read_events(use_cache=False)
            after = autodispatch.candidates(events, fold.fold(events)[0], now)
            self.assertEqual(_row(after, 'L-spec-9673')['status'], 'dispatchable')
        with patch.object(autodispatch, 'capacity', return_value=3):
            self.assertEqual(_row(autodispatch.candidates(events, fold.fold(events)[0], now), 'L-spec-9675')['status'], 'dispatchable')
        import hashlib
        payload = json.dumps(dict(before=rows, after=after), sort_keys=True)
        print(json.dumps(dict(url='cli://audit-candidates/AC4', status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


class VanishedRecoveryTests(unittest.TestCase):
    def test_tick_terminal_takes_recovery_from_fallback(self):
        import nextstep, lane_rules, hashlib, socket, panes
        from contextlib import ExitStack
        for count in (1, 2, 3):
            with tempfile.TemporaryDirectory(prefix='vanished-autodispatch-') as directory, ExitStack() as stack:
                root = pathlib.Path(directory)
                (root/'events').mkdir()
                (root/'content').mkdir()
                for module, key, value in ((fold, 'ROOT', root), (fold, 'EVENTS', root/'events'),
                        (dispatch, 'ROOT', root), (dispatch, 'EVENTS', root/'events'),
                        (dispatch, 'CONTENT', root/'content')):
                    stack.enter_context(patch.object(module, key, value))
                sid = 'L-spec-9861'
                def emit(actor, kind, minute, **kw):
                    path = root/'events'/f'L-{actor}-9861.jsonl'
                    with patch.object(dispatch, 'now', return_value=(NOW+datetime.timedelta(minutes=minute)).isoformat()):
                        self.assertIsNone(dispatch.emit(path, {}, kind, strict=True, subject=sid, project='proj', **kw))
                emit('operator', 'spec-written', -300)
                for n in range(count):
                    spawn = f'L-builder-{9860+n}'
                    emit('builder', 'build-started', -250+n, spawn=spawn, role='builder')
                    if n < count-1:
                        emit('tick', 'spawn-failed', -60+n, spawn=spawn, role='builder', reason='vanished')
                before = fold.read_events(use_cache=False)
                with patch.object(dispatch, 'now', return_value=NOW.isoformat()):
                    tick.spawn_in_flight(before)
                fresh = fold.read_events(use_cache=False)
                failures = [e for e in fresh if e['type'] == 'spawn-failed']
                self.assertEqual(len(failures), count)
                for f in failures:
                    self.assertIsNone(fold.admits(f, f['actor']))
                    self.assertTrue(autodispatch._own_terminal_event(fresh, f['spawn']))
                specs = fold.fold(fresh)[0]
                runner = _FakeRunner()
                replay = autodispatch.redispatch_dead(fresh, specs, NOW, runner=runner)
                self.assertEqual(runner.calls, [])
                steps = nextstep.next_steps(fresh, specs, NOW, lane_rules.load())
                step = next(s for s in steps if s.item == sid)
                self.assertIsNone(step.action)
                if count < 3:
                    self.assertIn('next_retry_at', step.reason)
                    later = NOW+datetime.timedelta(minutes=(5, 15)[count-1])
                    due = next(s for s in nextstep.next_steps(fresh, specs, later, lane_rules.load()) if s.item == sid)
                    self.assertEqual((due.action, due.owner), ('build', 'tick'))
                else:
                    self.assertEqual(step.owner, 'executor')
                    self.assertIn('exhaust', step.reason)
                tick.in_flight(before)
                second = fold.read_events(use_cache=False)
                autodispatch.redispatch_dead(second, fold.fold(second)[0], NOW, runner=runner)
                self.assertEqual(len([e for e in second if e['type'] == 'spawn-failed']), count)
                self.assertEqual(runner.calls, [])
                body = json.dumps(dict(events=second, steps=[s.__dict__ for s in steps],
                                       fallback=replay, dispatch_calls=runner.calls), sort_keys=True)
                print(json.dumps(dict(url='cli://vanished-autodispatch/'+str(count), status=0,
                                      body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
                # A possibly-live old waiter retains elapsed-stale seat release.
                path = root/'events'/'L-builder-9899.jsonl'
                with patch.object(dispatch, 'now', return_value=(NOW-datetime.timedelta(minutes=200)).isoformat()):
                    self.assertIsNone(dispatch.emit(path, {}, 'build-started', subject='L-spec-9899',
                        spawn='L-builder-9899', waiter_pid=os.getpid(), waiter_host=socket.gethostname(),
                        waiter_proc_start=panes.proc_start(os.getpid())))
                self.assertNotIn('L-spec-9899', tick.spawn_in_flight(fold.read_events(use_cache=False)))
                rows = fold.read_events(use_cache=False)
                self.assertEqual([e['type'] for e in rows if e.get('spawn') == 'L-builder-9899'],
                                 ['build-started', 'spawn-stale'])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
