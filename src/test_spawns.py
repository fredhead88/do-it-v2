#!/usr/bin/env python3
"""L-spec-0755 R2 (AC3, AC4, and the AC9 tick-agreement check). Run: python3 test_spawns.py

Hermetic: a throwaway DOIT_ROOT, DOIT_NO_POKE=1, no paid call. The dispatch.py
subprocesses below are refused BEFORE anything is spent (that is the property under test)."""
import datetime, fcntl, json, os, pathlib, socket, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="spawns-test-"))
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
os.environ["HOME"] = str(TMP / "home")
os.environ.pop("DOIT_PROJECT", None)
os.environ.pop("DOIT_LEDGER_FILE", None)
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold, panes, spawns, tick  # noqa: E402

EV = TMP / "events"
EV.mkdir(parents=True)
HOST = socket.gethostname()
NOW = fold.NOW
iso = lambda dt: dt.isoformat(timespec="seconds")


def start(spawn, subject, *, type_="spawn-started", ago_min=1, **kv):
    return {"v": 1, "type": type_, "subject": subject, "spawn": spawn, "ts": iso(NOW - datetime.timedelta(minutes=ago_min)),
            "actor": spawn.split("-")[1], "_src": "x", **kv}


def end(spawn, subject, type_="spawn-done"):
    return {"v": 1, "type": type_, "subject": subject, "spawn": spawn, "ts": iso(NOW), "actor": "x", "_src": "x"}


dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
me = os.getpid()
live = dict(waiter_pid=me, waiter_host=HOST, waiter_proc_start=panes.proc_start(me), window_min=90)

# ── open_spawns: the same terminal tuple and aging rule as tick._spawn_busy ──
fixtures = {
    "open": [start("L-builder-0001", "L-spec-0001", type_="build-started", worktree="/w/a", **live)],
    "done": [start("L-builder-0002", "L-spec-0002", type_="build-started", **live), end("L-builder-0002", "L-spec-0002")],
    "failed": [start("L-grader-0003", "L-spec-0003", **live), end("L-grader-0003", "L-spec-0003", "spawn-failed")],
    "stale": [start("L-grader-0004", "L-spec-0004", **live), end("L-grader-0004", "L-spec-0004", "spawn-stale")],
    "aged": [start("L-builder-0005", "L-spec-0005", type_="build-started", ago_min=90 + 90 + 5, window_min=90)],
    "not-yet-aged": [start("L-builder-0006", "L-spec-0006", type_="build-started", ago_min=90 + 90 - 5, window_min=90)],
    "waiter-dead": [start("L-builder-0007", "L-spec-0007", type_="build-started", waiter_pid=dead.pid, waiter_host=HOST,
                          waiter_proc_start=panes.proc_start(me), window_min=90)],
    "other-host": [start("L-builder-0008", "L-spec-0008", type_="build-started", waiter_pid=dead.pid,
                         waiter_host="some-other-host", window_min=90)],
    "reviewer-open": [start("L-reviewer-0009", "L-spec-0009", **live)],
}
for name, evs in fixtures.items():
    got = {o["subject"] for o in spawns.open_spawns(evs, NOW)}
    want = tick._spawn_busy(evs)
    assert got == want, (name, got, want)
assert {o["subject"] for o in spawns.open_spawns(fixtures["open"], NOW)} == {"L-spec-0001"}
for name in ("done", "failed", "stale", "aged", "waiter-dead"):
    assert spawns.open_spawns(fixtures[name], NOW) == [], name
assert [o["subject"] for o in spawns.open_spawns(fixtures["other-host"], NOW)] == ["L-spec-0008"]
every = [e for evs in fixtures.values() for e in evs]
assert {o["subject"] for o in spawns.open_spawns(every, NOW)} == tick._spawn_busy(every)
assert spawns.open_spawns(fixtures["open"], NOW)[0]["worktree"] == "/w/a"
print("L-spec-0755 spawns.open_spawns agrees with tick._spawn_busy ok")

# ── conflict(): the role table ───────────────────────────────────────────────
def o(role, subject, worktree=None):
    return [{"spawn": f"L-{role}-0100", "role": role, "subject": subject, "worktree": worktree, "ts": ""}]

for role, other, expect in [("builder", "builder", True), ("builder", "grader", True), ("builder", "reviewer", True),
                            ("grader", "builder", True), ("grader", "grader", True), ("grader", "reviewer", False),
                            ("reviewer", "builder", True), ("reviewer", "grader", False),
                            ("spec-writer", "builder", False), ("spec-writer", "spec-writer", False),
                            ("owed-sweeper", "builder", False), ("research", "builder", False),
                            ("probe", "builder", False), ("reuse-scout", "builder", False),
                            ("spec-auditor", "builder", False), ("plan-auditor", "builder", False)]:
    got = spawns.conflict(role, "S", "/w/x", o(other, "S"))
    assert bool(got) is expect, (role, other, got)
    assert spawns.conflict(role, "OTHER", "/w/x", o(other, "S")) is None or (role == "builder" and False), (role, other)
assert spawns.conflict("builder", "S2", "/w/a", o("builder", "S1", "/w/a")), "same worktree, other subject"
assert spawns.conflict("builder", "S2", "/w/b", o("builder", "S1", "/w/a")) is None
assert spawns.conflict("grader", "S2", "/w/a", o("builder", "S1", "/w/a")) is None, "worktree rule is builder-vs-builder"
assert spawns.conflict("builder", "S", "/w", []) is None
print("L-spec-0755 spawns.conflict table ok")


# ── AC3/AC4 · through the real wrapper ───────────────────────────────────────
PACKET = TMP / "packet.md"
PACKET.write_text("PINNED_BASE_SHA: " + "a" * 40 + "\nnothing real\n")
CWD = TMP / "wt"
CWD.mkdir()


def write_events(name, evs):
    with open(EV / name, "w") as fh:
        for e in evs:
            fh.write(json.dumps({k: v for k, v in e.items() if k not in ("actor", "_src")}) + "\n")


def ledger_events():
    return [e for e in fold.read_events()]


def dispatch(role, subject, cwd=CWD):
    before = {p.name for p in EV.glob("*.jsonl")}
    r = subprocess.run([sys.executable, str(HERE / "dispatch.py"), role, subject, "--packet", str(PACKET),
                        "--cwd", str(cwd), "--project", "p"], capture_output=True, text=True,
                       env={**os.environ, "DOIT_NO_POKE": "1"}, timeout=120)
    new = [e for p in sorted(EV.glob("*.jsonl")) if p.name not in before
           for e in map(json.loads, p.read_text().splitlines())]
    return r, new


write_events("L-builder-0900.jsonl", [start("L-builder-0900", "L-spec-0900", type_="build-started",
                                            worktree=str(CWD), project="p", **live)])
for role, subject, cwd in [("builder", "L-spec-0900", CWD), ("grader", "L-spec-0900", CWD),
                           ("reviewer", "L-spec-0900", CWD), ("builder", "L-spec-0901", CWD)]:
    r, new = dispatch(role, subject, cwd)
    types = [(e["type"], e.get("reason")) for e in new]
    assert r.returncode != 0, (role, subject, r.stdout, r.stderr)
    assert types == [("spawn-failed", "duplicate-dispatch")], (role, subject, types, r.stderr)
    assert not any(e["type"] in ("build-started", "spawn-started") for e in new)
print("L-spec-0755 AC3 ok (same-role, builder/grader, builder/reviewer, same worktree all refused before a start event)")

# a role that runs beside a build by design is not refused by the check
assert spawns.conflict("spec-writer", "L-spec-0900", str(CWD), spawns.open_spawns(ledger_events(), NOW)) is None

# AC4: the subject lock. While another wrapper holds it, a second one fails fast.
(TMP / "events" / "L-builder-0900.jsonl").write_text("")        # no open spawn now
(TMP / "state").mkdir(exist_ok=True)
with open(TMP / "state" / spawns.lock_name("L-spec-0910"), "a") as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    r, new = dispatch("reviewer", "L-spec-0910")
    assert r.returncode != 0 and [(e["type"], e.get("reason")) for e in new] == [("spawn-failed", "dispatch-in-progress")], \
        (r.stdout, r.stderr, new)
assert spawns.lock_name("a/b c") == "dispatch-a_b_c.lock"
print("L-spec-0755 AC4 ok (a held subject lock fails the second wrapper with dispatch-in-progress)")

# a spawn with a terminal event, spawn-stale, or a dead waiter no longer blocks
for name, evs in [("ended", [start("L-builder-0920", "L-spec-0920", type_="build-started", worktree=str(CWD), **live),
                            end("L-builder-0920", "L-spec-0920")]),
                  ("stale", [start("L-builder-0921", "L-spec-0921", type_="build-started", worktree=str(CWD), **live),
                             end("L-builder-0921", "L-spec-0921", "spawn-stale")]),
                  ("dead", [start("L-builder-0922", "L-spec-0922", type_="build-started", worktree=str(CWD),
                                  waiter_pid=dead.pid, waiter_host=HOST, waiter_proc_start=panes.proc_start(me))])]:
    write_events(f"L-builder-09{20 + ['ended', 'stale', 'dead'].index(name)}.jsonl", evs)
    assert spawns.conflict("builder", evs[0]["subject"], str(CWD), spawns.open_spawns(ledger_events(), NOW)) is None, name
print("L-spec-0755 AC4 ok (terminal, spawn-stale and dead-waiter spawns do not block)")
print("ALL OK")
