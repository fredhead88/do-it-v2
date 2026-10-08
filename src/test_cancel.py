#!/usr/bin/env python3
"""L-spec-0755 R1 (AC1, AC2). Run: python3 test_cancel.py

Hermetic: a throwaway DOIT_ROOT, DOIT_NO_POKE=1, no paid call, never the real ~/.do-it."""
import json, os, pathlib, signal, socket, subprocess, sys, tempfile, textwrap, time

TMP = pathlib.Path(tempfile.mkdtemp(prefix="cancel-test-"))
os.environ["DOIT_ROOT"], os.environ["DOIT_NO_POKE"] = str(TMP), "1"
os.environ["HOME"] = str(TMP / "home")
os.environ.pop("DOIT_PROJECT", None)
os.environ.pop("DOIT_LEDGER_FILE", None)
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cancel, dispatch, fold, panes, spawns  # noqa: E402

EV, SEAT = TMP / "events", TMP / "seat"
EV.mkdir(parents=True)
SEAT.mkdir(parents=True)
HOST = socket.gethostname()


def terminal(spawn):
    return [e for e in fold.read_events() if e.get("spawn") == spawn and e["type"] in ("spawn-done", "spawn-failed", "spawn-stale")]


def seed(spawn, subject, role, pid=None, host=HOST, start_type="spawn-started"):
    """A start event in the spawn's own ledger file, plus the packet and cmd.json it waits with."""
    e = {"v": 1, "ts": "2026-10-08T00:00:00+00:00", "type": start_type, "subject": subject, "spawn": spawn,
         "project": "p", "role": role, "backend": "seat", "window_min": 30}
    if pid:
        e.update(waiter_pid=pid, waiter_host=host, waiter_proc_start=panes.proc_start(pid))
    (EV / f"{spawn}.jsonl").write_text(json.dumps(e) + "\n")
    (SEAT / f"{spawn}.packet.md").write_text("PACKET " + spawn)
    (SEAT / f"{spawn}.cmd.json").write_text("{}")


def as_actor(ledger_file, spawn, *extra):
    return subprocess.run([sys.executable, str(HERE / "cancel.py"), spawn, *extra], capture_output=True, text=True,
                          env={**os.environ, "DOIT_LEDGER_FILE": ledger_file})


# ── AC1 · a queued, unclaimed spawn ends cleanly ─────────────────────────────
waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
seed("L-grader-0501", "L-spec-0501", "grader", pid=waiter.pid)
r = as_actor("L-executor-local.jsonl", "L-grader-0501")
assert r.returncode == 0, (r.stdout, r.stderr)
assert (SEAT / "cancelled" / "L-grader-0501.packet.md").read_text() == "PACKET L-grader-0501", "packet is moved, never deleted"
assert (SEAT / "cancelled" / "L-grader-0501.cmd.json").is_file()
assert not (SEAT / "L-grader-0501.packet.md").exists() and not (SEAT / "L-grader-0501.cmd.json").exists()
assert (SEAT / "L-grader-0501.cancelled").is_file()
assert waiter.wait(timeout=10) == -signal.SIGTERM, "the recorded waiter received SIGTERM"
t = terminal("L-grader-0501")
assert len(t) == 1 and t[0]["type"] == "spawn-failed" and t[0]["reason"] == "cancelled", t
assert t[0]["subject"] == "L-spec-0501" and t[0]["role"] == "grader" and t[0]["why"] == "cancelled by executor", t
assert t[0]["actor"] == "executor"
# a second cancel of the same spawn is refused and adds no second terminal event
r = as_actor("L-executor-local.jsonl", "L-grader-0501")
assert r.returncode != 0 and len(terminal("L-grader-0501")) == 1
print("L-spec-0755 AC1 ok (cancel: packet moved, marker written, waiter signalled, one spawn-failed reason=cancelled)")

# a waiter on another host, or one whose recorded start time no longer matches, is never signalled
bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
seed("L-grader-0502", "L-spec-0502", "grader", pid=bystander.pid, host="some-other-host")
assert as_actor("L-operator-local.jsonl", "L-grader-0502").returncode == 0
seed("L-grader-0503", "L-spec-0503", "grader", pid=bystander.pid)
ev = json.loads((EV / "L-grader-0503.jsonl").read_text())
ev["waiter_proc_start"] = "1"                                   # pid reuse: a different process now owns the pid
(EV / "L-grader-0503.jsonl").write_text(json.dumps(ev) + "\n")
assert as_actor("L-thinker-local.jsonl", "L-grader-0503").returncode == 0
time.sleep(0.5)
assert bystander.poll() is None, "a foreign-host or reused pid must never be signalled"
bystander.kill(), bystander.wait()
print("L-spec-0755 AC1 ok (foreign host / reused pid is not signalled; operator and thinker may cancel)")

# the wrapper side: run_seat stops on the marker (and on the SIGTERM that follows it), emitting nothing
child_src = textwrap.dedent(f"""
    import json, os, sys
    sys.path.insert(0, {str(HERE)!r})
    import dispatch, panes, socket, spawns
    spawn = "L-grader-0504"
    e = {{"v": 1, "ts": "2026-10-08T00:00:00+00:00", "type": "spawn-started", "subject": "L-spec-0504", "spawn": spawn,
         "project": "p", "role": "grader", "waiter_pid": os.getpid(), "waiter_host": socket.gethostname(),
         "waiter_proc_start": panes.proc_start(os.getpid()), "window_min": 30}}
    open(os.path.join({str(EV)!r}, spawn + ".jsonl"), "w").write(json.dumps(e) + "\\n")
    try:
        dispatch.run_seat(spawn, ["x"], "packet", "/", 600, window=600)
    except spawns.Cancelled:
        print("CANCELLED-OK", flush=True)
        sys.exit(0)
    print("NOT-CANCELLED", flush=True)
""")
child = subprocess.Popen([sys.executable, "-c", child_src], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "DOIT_ROOT": str(TMP)})
for _ in range(100):
    if (SEAT / "L-grader-0504.packet.md").is_file() and (EV / "L-grader-0504.jsonl").is_file():
        break
    time.sleep(0.1)
before = len(fold.read_events())
r = as_actor("L-executor-local.jsonl", "L-grader-0504")
assert r.returncode == 0, (r.stdout, r.stderr)
out, _ = child.communicate(timeout=30)
assert "CANCELLED-OK" in out and child.returncode == 0, (out, child.returncode)
t = terminal("L-grader-0504")
assert len(t) == 1 and t[0]["reason"] == "cancelled", t
assert len(fold.read_events()) == before + 1, "the waiter emitted nothing; the cancel command is the single emitter"
print("L-spec-0755 AC1 ok (run_seat raises Cancelled on the marker/SIGTERM; no second terminal event)")

# main()'s exit path emits nothing
n = len(fold.read_events())
try:
    spawns.cancelled_exit("L-grader-0504")
except SystemExit as e:
    assert e.code == 1
else:
    raise AssertionError("cancelled_exit must exit")
assert len(fold.read_events()) == n

# ── AC2 · only executor/operator/thinker; a claimed spawn needs --force ──────
seed("L-builder-0510", "L-spec-0510", "builder", start_type="build-started")
n = len(fold.read_events())
for f in ("L-builder-local.jsonl", "L-grader-0099.jsonl", "L-reviewer-local.jsonl", "L-spec-writer-0001.jsonl", "L-tick-local.jsonl"):
    r = as_actor(f, "L-builder-0510")
    assert r.returncode != 0, (f, r.stdout)
    assert not (SEAT / "L-builder-0510.cancelled").exists() and (SEAT / "L-builder-0510.packet.md").exists()
    assert len(fold.read_events()) == n, f"{f}: a refused cancel writes nothing"
# a hand-appended forged cancel from a builder file lands in `ignored`, and is not a terminal event for the cancel command
forged = {"v": 1, "ts": "2026-10-08T00:01:00+00:00", "type": "spawn-failed", "subject": "L-spec-0510",
          "spawn": "L-builder-0510", "reason": "cancelled", "why": "forged"}
(EV / "L-builder-0777.jsonl").write_text(json.dumps(forged) + "\n")
_, _, ignored, by = fold.fold(fold.read_events())
assert any(e.get("why") == "forged" for e in ignored), "forged cancel must be ignored by the fold"
assert not any(e.get("why") == "forged" for e in by["L-spec-0510"])
assert as_actor("L-executor-local.jsonl", "L-builder-0510").returncode == 0, "a forged cancel does not end the spawn for cancel"
# the same event from the executor is admitted
_, _, ignored, by = fold.fold(fold.read_events())
assert any(e.get("reason") == "cancelled" and e["actor"] == "executor" for e in by["L-spec-0510"])
# other spawn-failed reasons from a builder file are untouched by the narrowing
(EV / "L-builder-0778.jsonl").write_text(json.dumps({**forged, "reason": "timeout", "why": "t", "spawn": "L-builder-0778"}) + "\n")
_, _, ignored, by = fold.fold(fold.read_events())
assert not any(e.get("reason") == "timeout" for e in ignored)

seed("L-builder-0511", "L-spec-0511", "builder", start_type="build-started")
(SEAT / "L-builder-0511.claimed").write_text("pane")
n = len(fold.read_events())
r = as_actor("L-executor-local.jsonl", "L-builder-0511")
assert r.returncode != 0 and "claimed" in r.stderr and len(fold.read_events()) == n
assert (SEAT / "L-builder-0511.packet.md").exists() and not (SEAT / "L-builder-0511.cancelled").exists()
r = as_actor("L-executor-local.jsonl", "L-builder-0511", "--force")
assert r.returncode == 0, (r.stdout, r.stderr)
t = terminal("L-builder-0511")
assert len(t) == 1 and t[0]["reason"] == "cancelled" and t[0]["role"] == "builder", t
print("L-spec-0755 AC2 ok (actor restriction, forged cancel ignored in the fold, claimed spawn needs --force)")

# bad ids and unknown spawns are refused before any write
for bad in ("../etc/passwd", "L-grader-x", "nope"):
    assert as_actor("L-executor-local.jsonl", bad).returncode != 0
assert as_actor("L-executor-local.jsonl", "L-grader-9999").returncode != 0
assert subprocess.run([str(HERE.parent / "doit"), "cancel"], capture_output=True, text=True).returncode == 2
print("ALL OK")
