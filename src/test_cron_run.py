#!/usr/bin/env python3
"""cron_run.py — the per-run cron wrapper (L-charter-0042 R14a, L-spec-0485).
Run: python3 test_cron_run.py

Hermetic: every fixture uses a temp DOIT_ROOT/DOIT_SCRATCH; nothing here
touches the real crontab or the real /etc/cron.d directly. AC2-AC4 exercise
the real `./doit cron-run` CLI as a subprocess (the wrapper IS a subprocess
boundary, so that is the honest way to prove its exit-code/stdout/stderr
contract); AC1/AC3 exercise the store logic in-process, AC3 also via a
separate, freshly-spawned reader process. AC14 is a meta-harness proving the
whole cron-adjacent test surface (this file, test_crons.py, test_look.py)
never reaches a real crontab binary or a real cron.d path.
"""
import collections, errno, fcntl, hashlib, json, os, pathlib, signal, subprocess, sys, tempfile, time
from datetime import datetime
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DOIT = REPO / "doit"
sys.path.insert(0, str(HERE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="cron-run-test-"))
os.environ["DOIT_ROOT"] = str(TMP / "doit-root-unused")
os.environ["DOIT_SCRATCH"] = str(pathlib.Path(tempfile.mkdtemp(prefix="cron-run-test-scratch-")))

import cron_run  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


# ══ R14-AC1 · error_class ════════════════════════════════════════════════════
check(cron_run.error_class(0, "anything") == "", "R14-AC1: rc==0 -> empty string regardless of stderr")
got_ac1 = cron_run.error_class(1, "boom 123 at 0xdeadbeef\nx")
check(got_ac1 == "rc:1:boom at", f"R14-AC1: digits/hex stripped from the first line: {got_ac1!r}")
check(cron_run.error_class(2, "") == "rc:2:", "R14-AC1: empty stderr -> 'rc:2:' with nothing after")
tail_a = cron_run.error_class(1, "failed at 111 0xAAAAAAA\n")
tail_b = cron_run.error_class(1, "failed at 222 0xBBBBBBB\n")
check(tail_a == tail_b, f"R14-AC1: two tails differing only in digits/hex give equal classes: {tail_a!r} vs {tail_b!r}")
tail_c = cron_run.error_class(1, "a completely different line\n")
check(tail_a != tail_c, f"R14-AC1: different first lines give different classes: {tail_a!r} vs {tail_c!r}")
print("R14-AC1 ok")


# ══ R14-AC2 · the real wrapper, run as a subprocess ══════════════════════════
def _env_for(root):
    env = dict(os.environ)
    env["DOIT_ROOT"] = str(root)
    return env


root_ac2 = TMP / "ac2-root"
root_ac2.mkdir()
p_ac2 = subprocess.run([str(DOIT), "cron-run", "j1", "--", "sh", "-c",
                        'echo out; echo "err 7" >&2; exit 3'],
                       capture_output=True, text=True, env=_env_for(root_ac2))
check(p_ac2.returncode == 3, f"R14-AC2: exit code 3: {p_ac2.returncode}")
check(p_ac2.stdout == "out\n", f"R14-AC2: stdout is exactly 'out': {p_ac2.stdout!r}")
check(p_ac2.stderr == "err 7\n", f"R14-AC2: stderr is exactly 'err 7': {p_ac2.stderr!r}")
store_ac2 = root_ac2 / "state" / "cron-runs" / "j1.jsonl"
rows_ac2 = [json.loads(l) for l in store_ac2.read_text().splitlines() if l.strip()]
check(len(rows_ac2) == 2 and rows_ac2[0]["ev"] == "start" and rows_ac2[1]["ev"] == "end",
      f"R14-AC2: exactly a start row then an end row: {rows_ac2}")
end_ac2 = rows_ac2[1]
check(end_ac2["rc"] == 3, f"R14-AC2: end row rc == 3: {end_ac2}")
check(end_ac2["error_class"] == "rc:3:err", f"R14-AC2: error_class == 'rc:3:err': {end_ac2}")
check("err 7" in end_ac2["error_tail"], f"R14-AC2: error_tail contains 'err 7': {end_ac2}")
check(end_ac2["duration_s"] >= 0, f"R14-AC2: duration_s >= 0: {end_ac2}")

root_ac2b = TMP / "ac2b-root"
root_ac2b.mkdir()
p_ac2b = subprocess.run([str(DOIT), "cron-run", "j1", "--", "true"],
                        capture_output=True, text=True, env=_env_for(root_ac2b))
check(p_ac2b.returncode == 0, f"R14-AC2: a clean command exits 0: {p_ac2b.returncode}")
store_ac2b = root_ac2b / "state" / "cron-runs" / "j1.jsonl"
rows_ac2b = [json.loads(l) for l in store_ac2b.read_text().splitlines() if l.strip()]
end_ac2b = rows_ac2b[-1]
check(end_ac2b["rc"] == 0 and end_ac2b["error_class"] == "", f"R14-AC2: a clean end row: {end_ac2b}")
print("R14-AC2 ok")


# ══ R14-AC3 · store round trip — 50-row cap; a fresh-open reader agrees ══════
root_ac3 = TMP / "ac3-root"
root_ac3.mkdir()
for i in range(30):
    cron_run.run("j-ac3", [sys.executable, "-c", f"import sys; sys.exit({i % 7})"], root=root_ac3)
store_ac3 = cron_run.store_path("j-ac3", root=root_ac3)
rows_ac3 = [json.loads(l) for l in store_ac3.read_text().splitlines() if l.strip()]
check(len(rows_ac3) == 50, f"R14-AC3: exactly 50 rows kept after 30 runs (60 rows appended): {len(rows_ac3)}")
last_rc_ac3 = 29 % 7
p_ac3 = subprocess.run([sys.executable, "-c",
                        "import json,sys; print(json.loads(open(sys.argv[1]).read().splitlines()[-1])['rc'])",
                        str(store_ac3)], capture_output=True, text=True)
check(p_ac3.stdout.strip() == str(last_rc_ac3),
      f"R14-AC3: a fresh-open process (not this test's own handle) reads the last row's rc "
      f"== {last_rc_ac3}: {p_ac3.stdout!r} / {p_ac3.stderr!r}")
events_dir_ac3 = root_ac3 / "events"
check(not events_dir_ac3.exists() or not any(events_dir_ac3.iterdir()),
      "R14-AC3: no file appears under $DOIT_ROOT/events/")
print("R14-AC3 ok")


# ══ R14-AC4 · an unwritable $DOIT_ROOT/state never changes CMD's behaviour ═══
root_ac4 = TMP / "ac4-root"
root_ac4.mkdir()
(root_ac4 / "state").write_text("not a directory")   # a FILE where the dir should be
p_ac4 = subprocess.run([str(DOIT), "cron-run", "j2", "--", "sh", "-c", "echo hi; exit 5"],
                       capture_output=True, text=True, env=_env_for(root_ac4))
check(p_ac4.returncode == 5, f"R14-AC4: exit code 5 despite an unwritable state dir: {p_ac4.returncode}")
check(p_ac4.stdout == "hi\n", f"R14-AC4: stdout is still 'hi': {p_ac4.stdout!r}")
check(p_ac4.stderr == "", f"R14-AC4: nothing on stderr beyond CMD's own (CMD wrote none): {p_ac4.stderr!r}")
print("R14-AC4 ok")


def _overlap_signed(ac, body):
    body = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url=f"cli://cron-run/L-spec-1532/AC{ac}", status=0,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


def _spawn_job(root, name, cmd):
    return subprocess.Popen([sys.executable, str(HERE / "cron_run.py"), name, "--", *cmd],
                            env=_env_for(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)


def _wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        time.sleep(.01)


def _cleanup_job(proc):
    # Also reap commands left in the fixture's own group after wrapper SIGKILL.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.communicate(timeout=5)


def _single_instance_checks():
    with tempfile.TemporaryDirectory(prefix="cron-overlap-") as directory:
        root = pathlib.Path(directory)
        first = _spawn_job(root, "j-ac1", ["sh", "-c", "sleep 4"])
        try:
            _wait_file(cron_run.store_path("j-ac1", root))
            marker = root / "second-ran"
            began = time.monotonic()
            second = _spawn_job(root, "j-ac1", ["sh", "-c", f"echo ran > {marker}"])
            try:
                out, err = second.communicate(timeout=2)
                elapsed = time.monotonic() - began
                check(second.returncode == 0 and elapsed < 2 and out == err == "",
                      "1532 AC1: a contending process returns 0 immediately and silently")
                check(not marker.exists(), "1532 AC1: the second command never runs")
                skips = [json.loads(line) for line in cron_run.skip_store_path("j-ac1", root).read_text().splitlines()]
                check(len(skips) == 1 and set(skips[0]) == {"ev", "ts", "pid", "reason"},
                      "1532 AC1: exactly one skip with exactly the contracted keys")
                check(skips[0]["ev"] == "skip" and skips[0]["reason"] == "already-running"
                      and skips[0]["pid"] == second.pid and isinstance(skips[0]["pid"], int)
                      and datetime.fromisoformat(skips[0]["ts"]).utcoffset() is not None,
                      "1532 AC1: skip identity, reason and ISO instant")
            finally:
                _cleanup_job(second)
            first.communicate(timeout=6)
            rows = [json.loads(line) for line in cron_run.store_path("j-ac1", root).read_text().splitlines()]
            check(first.returncode == 0 and [row["ev"] for row in rows] == ["start", "end"],
                  "1532 AC1: the first copy keeps its own result and two-row ring")
            _overlap_signed(1, dict(second_rc=second.returncode, elapsed_s=elapsed,
                                    stdout=out, stderr=err, marker_exists=marker.exists(),
                                    skip=skips[0], first_rc=first.returncode, ring_events=[r["ev"] for r in rows]))
        finally:
            _cleanup_job(first)

        check(cron_run.run("j-release", ["sh", "-c", "exit 3"], root) == 3,
              "1532 AC2: failing command retains exit 3")
        marker = root / "released"
        check(cron_run.run("j-release", ["sh", "-c", f"touch {marker}; exit 7"], root) == 7
              and marker.exists(), "1532 AC2: another command runs after failure")

        daemon_pid = root / "daemon-pid"
        daemon = _spawn_job(root, "j-daemon", ["sh", "-c",
                            f"sleep 30 >/dev/null 2>&1 & echo $! > {daemon_pid}; exit 0"])
        try:
            daemon.communicate(timeout=5)
            check(daemon.returncode == 0, "1532 AC2: a background leftover does not delay wrapper exit")
            marker = root / "after-daemon"
            check(cron_run.run("j-daemon", ["touch", str(marker)], root) == 0 and marker.exists()
                  and not cron_run.skip_store_path("j-daemon", root).exists(),
                  "1532 AC2: a background leftover cannot retain the lock")
        finally:
            _cleanup_job(daemon)

        fd_job = _spawn_job(root, "j-fds", [sys.executable, "-c",
                             "import os; "
                             "print([os.readlink('/proc/self/fd/'+fd) for fd in os.listdir('/proc/self/fd') "
                             "if os.path.exists('/proc/self/fd/'+fd)])"])
        try:
            fd_out, fd_err = fd_job.communicate(timeout=5)
            check(fd_job.returncode == 0 and not fd_err and ".lock" not in fd_out,
                  "1532 AC2: no lock descriptor reaches CMD")
        finally:
            _cleanup_job(fd_job)

        child_pid = root / "child-pid"
        killed = _spawn_job(root, "j-killed", ["sh", "-c", f"echo $$ > {child_pid}; exec sleep 30"])
        try:
            _wait_file(child_pid)
            killed.kill()
            killed.wait(timeout=5)
            os.kill(int(child_pid.read_text()), 0)
            marker = root / "after-kill"
            check(cron_run.run("j-killed", ["touch", str(marker)], root) == 0 and marker.exists()
                  and not cron_run.skip_store_path("j-killed", root).exists(),
                  "1532 AC2: SIGKILL releases wrapper lock while CMD still lives")
        finally:
            _cleanup_job(killed)

        ready, release = root / "ready", root / "release"
        concurrent = _spawn_job(root, "j-a", [sys.executable, "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); "
            "\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)", str(ready), str(release)])
        try:
            _wait_file(ready)
            marker = root / "other-name"
            check(cron_run.run("j-b", ["touch", str(marker)], root) == 0 and marker.exists()
                  and concurrent.poll() is None, "1532 AC2: different names run concurrently")
            release.touch()
            concurrent.communicate(timeout=5)
            check(concurrent.returncode == 0 and not list(root.glob("state/cron-runs/j-?.skips.jsonl")),
                  "1532 AC2: independent jobs never skip each other")
        finally:
            _cleanup_job(concurrent)
        _overlap_signed(2, dict(failure_rc=3, next_rc=7, daemon_lock_released=True,
                                fd_targets=fd_out.strip(), sigkill_lock_released=True, independent_names=True))

        broken = root / "broken"
        broken.mkdir()
        (broken / "state").write_text("not a directory")
        marker = broken / "ran"
        check(cron_run.run("j-broken", ["sh", "-c", f"touch {marker}; exit 5"], broken) == 5
              and marker.exists() and not list(broken.rglob("*.skips.jsonl")),
              "1532 AC3: a failed lock-directory creation never skips CMD")
        for error in (errno.ENOLCK, errno.EACCES):
            marker = root / f"ran-{error}"
            with patch.object(cron_run.fcntl, "flock", side_effect=OSError(error, "fixture")):
                check(cron_run.run("j-lock-error", ["sh", "-c", f"touch {marker}; exit 6"], root) == 6
                      and marker.exists() and not cron_run.skip_store_path("j-lock-error", root).exists(),
                      "1532 AC3: non-contention flock errors never skip CMD")
        with patch.object(cron_run.fcntl, "flock", side_effect=BlockingIOError(errno.EWOULDBLOCK, "held")), \
                patch.object(cron_run.subprocess, "Popen") as popen:
            check(cron_run.run("j-held", ["false"], root) == 0 and not popen.called,
                  "1532 AC3: only contention skips the command")
        held_rows = [json.loads(line) for line in cron_run.skip_store_path("j-held", root).read_text().splitlines()]
        check(len(held_rows) == 1 and held_rows[0]["reason"] == "already-running",
              "1532 AC3: contention records exactly one skip")
        _overlap_signed(3, dict(directory_failure_rc=5, flock_error_rc=6,
                                flock_errors=[errno.ENOLCK, errno.EACCES], contention_rc=0, skip_rows=held_rows))

        ring = cron_run.store_path("j-ac4", root)
        skip_path = cron_run.skip_store_path("j-ac4", root)
        check(skip_path == root / "state/cron-runs/j-ac4.skips.jsonl",
              "1532 AC4: skip_store_path has the contracted path")
        with patch.object(cron_run.fold, "ROOT", root):
            check(cron_run.skip_store_path("j-ac4") == skip_path, "1532 AC4: default root read live")
        with patch.object(cron_run.fold, "ROOT", broken):
            check(cron_run.skip_store_path("j-ac4") == broken / "state/cron-runs/j-ac4.skips.jsonl",
                  "1532 AC4: default root is never cached")
        with ring.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for index in range(55):
                with patch.object(cron_run.os, "getpid", return_value=20000 + index):
                    check(cron_run.run("j-ac4", ["false"], root) == 0, "1532 AC4: real held-lock skip")
            check(not ring.exists(), "1532 AC4: skips never create the start/end ring")
            reader = subprocess.run([sys.executable, "-c",
                "import cron_run,json,look,sys; print(json.dumps(look._read_cron_store("
                "cron_run.skip_store_path('j-ac4', sys.argv[1]))))", str(root)],
                cwd=HERE, env=_env_for(root), capture_output=True, text=True, check=True)
            rows = json.loads(reader.stdout)
            check(len(rows) == 50 and all(row["ev"] == "skip" for row in rows)
                  and [row["pid"] for row in rows] == list(range(20005, 20055)),
                  "1532 AC4: a fresh reader sees the newest 50 of 55 skips in order")
            original = "\n".join(json.dumps(dict(ev="start" if i == 0 else "end", ts=str(i)))
                                  for i in range(50)) + "\n"
            ring.write_text(original)
            for _ in range(55):
                cron_run.run("j-ac4", ["false"], root)
            check(ring.read_bytes() == original.encode(), "1532 AC4: 55 skips never evict the open start")
        _overlap_signed(4, dict(fresh_reader_rows=len(rows), pids=[row["pid"] for row in rows],
                                ring_created_by_skip=False, existing_ring_byte_identical=True))


_single_instance_checks()


# ══ R14-AC14 · behavioural hermeticity — no real crontab/cron.d ever reached ═
def _run_via_runpy(path, tolerate_msg=None):
    """Executes `path` as `__main__`, in-process (so the module-level patches
    below are visible to its own lazy `import crons`/`import look`). A known,
    pre-existing, unrelated failure (see `tolerate_msg`) still counts as a
    pass here — this harness proves hermeticity, not that every OTHER spec's
    acceptance criterion is green on this box."""
    import runpy
    try:
        runpy.run_path(str(path), run_name="__main__")
        return True
    except SystemExit as e:
        return e.code in (None, 0)
    except AssertionError as e:
        return tolerate_msg is not None and tolerate_msg in str(e)
    except Exception:
        return False


def _r14ac14():
    if os.environ.get("_R14AC14_INNER"):
        return   # guard: never recurse into this harness from inside itself
    import crons, look

    harness_tmp = pathlib.Path(tempfile.mkdtemp(prefix="r14ac14-"))
    fake_home = harness_tmp / "home"; fake_home.mkdir()
    fake_root = harness_tmp / "doit-root"; fake_root.mkdir()
    fake_bin = harness_tmp / "bin"; fake_bin.mkdir()
    marker = harness_tmp / "crontab-invoked.marker"
    sentinel_crond = harness_tmp / "sentinel-crond-does-not-exist"
    stub = fake_bin / "crontab"
    stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 7\n")
    stub.chmod(0o755)

    recorder_calls = []
    real_RUN, real_crond_text, real_crond_default = crons._RUN, crons._crond_text, crons.CROND_DEFAULT
    real_runner_crontab_text = look.Runner.crontab_text

    def rec_run(*a, **k):
        recorder_calls.append(("crons._RUN", a))
        raise AssertionError("crons._RUN must never be called under R14-AC14 (a real crontab binary call)")

    def rec_crond_text(d):
        # A real, non-sentinel directory (this file's own or test_look.py's
        # own empty tempdir default) is a harmless pre-existing pattern
        # (Assumptions) and passes through unchanged; only the SENTINEL path
        # — meaning some code defaulted to `crons.CROND_DEFAULT` without an
        # operator ever pointing it anywhere real — is the leak this proves
        # absent.
        if str(d) == str(sentinel_crond):
            recorder_calls.append(("crons._crond_text:SENTINEL", str(d)))
            raise AssertionError(f"crons._crond_text reached the sentinel cron.d path: {d}")
        return real_crond_text(d)

    def rec_runner_crontab_text(self, timeout):
        recorder_calls.append(("look.Runner.crontab_text", timeout))
        raise AssertionError("look.Runner.crontab_text must never be called under R14-AC14 "
                             "(every test uses its own FakeRunner)")

    crons._RUN = rec_run
    crons._crond_text = rec_crond_text
    crons.CROND_DEFAULT = str(sentinel_crond)
    look.Runner.crontab_text = rec_runner_crontab_text

    saved_env = dict(os.environ)
    os.environ["_R14AC14_INNER"] = "1"
    os.environ["HOME"] = str(fake_home)
    os.environ["DOIT_ROOT"] = str(fake_root)
    os.environ["PATH"] = f"{fake_bin}:{saved_env.get('PATH', '')}"

    # test_look.py's own AC15c is a pre-existing failure, confirmed identical
    # in a detached scratch worktree at this spec's base_sha (unrelated to
    # cron-run/hermeticity, and outside this spec's Writes grant to fix) —
    # tolerated here so this proof isn't blocked by an unrelated, older bug.
    tolerated = {
        HERE / "test_look.py": "AC15: crons unimportable -> the same reading lands",
    }
    files_ok = True
    try:
        for f in (HERE / "test_cron_run.py", HERE / "test_crons.py", HERE / "test_look.py"):
            if not _run_via_runpy(f, tolerate_msg=tolerated.get(f)):
                files_ok = False
    finally:
        crons._RUN, crons._crond_text, crons.CROND_DEFAULT = real_RUN, real_crond_text, real_crond_default
        look.Runner.crontab_text = real_runner_crontab_text
        os.environ.clear()
        os.environ.update(saved_env)

    check(files_ok, "R14-AC14: every one of the three files passes (a known pre-existing "
                    "test_look.py failure tolerated by name)")
    check(not recorder_calls, f"R14-AC14: no recorder saw a call reaching a real seam: {recorder_calls}")
    check(not marker.exists(), "R14-AC14: the crontab stub was never invoked (marker file absent)")
    print("R14-AC14 ok")


def _runtime_checks():
    """Real process/pipe regressions for opt-in limits (L-spec-1651)."""
    import contextlib
    import io
    import threading
    import tomllib

    old_keys = {"ev", "ts", "rc", "duration_s", "error_class", "error_tail", "start_ts"}
    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    root = TMP / "runtime"
    root.mkdir()
    manifest = root / "crons.toml"
    records = collections.defaultdict(list)
    serial = 0

    def configured(text='kill_after_s = 1\nkill_grace_s = 1', name="bounded"):
        manifest.write_text(f'[[row]]\nname = "{name}"\n{text}\n')
        return manifest

    def rows(name="bounded", at=root):
        return [json.loads(line) for line in cron_run.store_path(name, at).read_text().splitlines()]

    def record(ac, body):
        records[ac].append(body)

    def marker(ac):
        body = json.dumps(records[ac], sort_keys=True)
        print(json.dumps(dict(url=f"cli://cron-run/L-spec-1651/AC{ac}", status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
        print(f"L-spec-1651 AC{ac} ok")

    def env():
        return dict(_env_for(root), DOIT_CRON_RUN_TEST="1", DOIT_CRON_RUN_MANIFEST=str(manifest))

    def cli(cmd, overrides=None, name="bounded"):
        effective = env()
        effective.update(overrides or {})
        began = time.monotonic()
        p = subprocess.run([str(DOIT), "cron-run", name, "--", *cmd], env=effective,
                           capture_output=True, timeout=9)
        return p, time.monotonic() - began, rows(name)[-1]

    def inprocess(cmd, threaded=False):
        before_handlers = [signal.getsignal(s) for s in signals]
        before_threads = threading.active_count()
        before_fds = set(os.listdir("/proc/self/fd"))
        result, errors = [], []
        def invoke():
            try:
                result.append(cron_run.run("bounded", cmd, root, manifest))
            except BaseException as exc:
                errors.append(exc)
        began = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            if threaded:
                thread = threading.Thread(target=invoke)
                thread.start()
                thread.join(7)
                check(not thread.is_alive(), "1651 AC12: worker returns within bound")
            else:
                invoke()
        elapsed = time.monotonic() - began
        check(not errors, f"1651: in-process run did not raise: {errors}")
        check([signal.getsignal(s) for s in signals] == before_handlers,
              "1651 AC12: signal handlers restored")
        check(threading.active_count() == before_threads and set(os.listdir("/proc/self/fd")) == before_fds,
              "1651 AC13: no thread or descriptor leak")
        record(13, dict(cmd=cmd, fds_equal=True, threads_equal=True))
        return result[0], elapsed, rows()[-1], stderr.getvalue()

    def stopped(pid):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            try:
                os.kill(pid, 0)
                if pathlib.Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z"):
                    return True
            except (ProcessLookupError, FileNotFoundError):
                return True
            time.sleep(.02)
        return False

    def cleanup(pidfiles):
        for pidfile in pidfiles:
            if pidfile.exists():
                pid = int(pidfile.read_text())
                for kill in (os.killpg, os.kill):
                    try:
                        kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def released():
        marker_path = root / "released"
        marker_path.unlink(missing_ok=True)
        rc, _, _, _ = inprocess(["touch", str(marker_path)])
        check(rc == 0
              and marker_path.exists() and not cron_run.skip_store_path("bounded", root).exists(),
              "1651 AC5: lock released after bounded run")
        record(5, dict(released=True))

    normal = ["sh", "-c", 'echo out; echo "err 7" >&2; exit 3']
    sleep2 = ["sh", "-c", "exec sleep 2"]
    sleep30 = ["sh", "-c", "exec sleep 30"]
    # AC1: all dormant shapes and both exact switches, through the real CLI.
    for text, name, overrides, sentinel in (
            ('kill_after_s = 1', "other", {}, False),
            ('kill_grace_s = 0', "bounded", {}, False),
            ('kill_after_s = 1', "bounded", {"DOIT_CRON_KILL": "off"}, False),
            ('kill_after_s = 1', "bounded", {}, True)):
        configured(text, name)
        switch = root / "state/cron-runs/kill.off"
        if sentinel:
            switch.touch()
        try:
            cron_run.store_path("bounded", root).unlink(missing_ok=True)
            p, elapsed, end = cli(normal, overrides)
            check((p.returncode, p.stdout, p.stderr) == (3, b"out\n", b"err 7\n")
                  and set(end) == old_keys and end["error_class"] == "rc:3:err"
                  and [r["ev"] for r in rows()] == ["start", "end"]
                  and set(rows()[-2]) == {"ev", "ts", "pid"}, "1651 AC1: unchanged dormant CLI")
            record(1, dict(end=end, stdout=p.stdout.decode(), stderr=p.stderr.decode()))
            if sentinel or overrides:
                p, elapsed, end = cli(sleep2, overrides)
                check(p.returncode == 0 and set(end) == old_keys, "1651 AC1: switch bypasses deadline")
        finally:
            switch.unlink(missing_ok=True)
    configured("")
    with patch.object(cron_run.subprocess, "Popen", wraps=subprocess.Popen) as popen:
        cron_run.run("bounded", ["true"], root, manifest)
        check(popen.call_args.kwargs == dict(stdout=None, stderr=subprocess.PIPE, text=True, bufsize=1),
              "1651 AC1: unbounded Popen arguments unchanged")
    configured()
    p, elapsed, end = cli(sleep30, {"DOIT_CRON_KILL": "OFF"})
    check(p.returncode == 124 and end["killed"], "1651 AC1: uppercase OFF does not disable")
    marker(1)

    # The TERM fixture reaps its own child before dying, avoiding orphan zombies.
    cmd_pid, child_pid = root / "cmd-pid", root / "child-pid"
    term_cmd = [sys.executable, "-c", "import os,pathlib,signal,subprocess,time; "
                "child=subprocess.Popen(['sleep','30']); "
                "pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); "
                "pathlib.Path(__import__('sys').argv[2]).write_text(str(child.pid)); "
                "caught=0; "
                "\ndef term(s,f):\n global caught; caught=s\n"
                "signal.signal(signal.SIGTERM,term); child.wait(); "
                "signal.signal(caught,signal.SIG_DFL); os.kill(os.getpid(),caught)", str(cmd_pid), str(child_pid)]
    try:
        # A separate root proves exactly two persisted rows, freshly reopened after exit.
        cron_run.store_path("bounded", root).unlink()
        p, elapsed, end = cli(term_cmd)
        check(p.returncode == 124 and elapsed < 6 and all(stopped(int(f.read_text())) for f in (cmd_pid, child_pid)),
              "1651 AC2: TERM stops CMD and child within bound")
        check([r["ev"] for r in rows()] == ["start", "end"] and end["rc"] == 124
              and end["killed"] and end["error_class"] == "killed" and end["kill_reason"] == "deadline"
              and end["kill_signals"] == ["TERM"] and end["cmd_reaped"] and end["cmd_rc"] == 143
              and end["kill_after_s"] == end["kill_grace_s"] == 1 and end["kill_elapsed_s"] >= 1,
              "1651 AC2: complete persisted TERM diagnostics")
        check(p.stderr.splitlines()[-1].startswith(b"cron-run: ") and b"killed after" in p.stderr.splitlines()[-1],
              "1651 AC2: kill line is last stderr line")
        record(2, dict(end=end, elapsed_s=elapsed, stderr=p.stderr.decode()))
        released()
    finally:
        cleanup([cmd_pid, child_pid])
    marker(2)

    try:
        p, elapsed, end = cli(["sh", "-c", f"echo $$ > {cmd_pid}; trap '' TERM; exec sleep 30"])
        check(p.returncode == 137 and elapsed < 6 and end["killed"] and end["cmd_rc"] == 137
              and end["kill_signals"] == ["TERM", "KILL"] and stopped(int(cmd_pid.read_text())),
              "1651 AC3: TERM-ignoring CMD needs KILL")
        record(3, dict(end=end, elapsed_s=elapsed))
    finally:
        cleanup([cmd_pid])
    marker(3)

    # Both CLI and in-process pipe cases; recorded descendants are always cleaned up.
    def descendant(kind):
        nonlocal serial
        serial += 1
        pidfile = root / f"descendant-{serial}"
        own_session = kind != "c"
        redirect = kind == "a"
        code = ("import os,pathlib,subprocess,sys,time; "
                f"p=subprocess.Popen(['sleep','30'],start_new_session={own_session},"
                f"stdout=subprocess.DEVNULL,stderr={'subprocess.DEVNULL' if redirect else 'None'}); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
                + ("time.sleep(30)" if kind in "ab" else ""))
        return [sys.executable, "-c", code, str(pidfile)], pidfile

    for process_mode in ("cli", "inprocess"):
        for kind in "abcd":
            cmd, pidfile = descendant(kind)
            try:
                if process_mode == "cli":
                    p, elapsed, end = cli(cmd)
                    rc, stderr = p.returncode, p.stderr.decode()
                else:
                    rc, elapsed, end, stderr = inprocess(cmd)
                check(elapsed < (4 if kind == "d" else 6), "1651 AC4: bounded pipe completion")
                pid = int(pidfile.read_text())
                if kind in "ab":
                    check(rc == 124 and end["kill_signals"] == ["TERM"] and end["killed"],
                          "1651 AC4: TERM stops only CMD")
                    os.kill(pid, 0)
                    check(not stopped(pid), "1651 AC4: own-session descendant survives")
                    check(("stderr_abandoned" in end) == (kind == "b"), "1651 AC4: open-pipe diagnostic")
                elif kind == "c":
                    check((rc, end["kill_signals"]) in ((124, ["TERM"]), (137, ["TERM", "KILL"]))
                          and end["killed"] and end["cmd_reaped"] and end["cmd_rc"] == 0
                          and "stderr_abandoned" not in end and stopped(pid), "1651 AC4: same-group leftover killed")
                else:
                    check(rc == 0 and end["error_class"] == "" and end["stderr_abandoned"]
                          and end["kill_signals"] == [] and end["cmd_rc"] == 0 and "killed" not in end
                          and "cron-run:" not in stderr, "1651 AC4: detached pipe abandoned without a kill")
                record(4, dict(case=kind, mode=process_mode, end=end, elapsed_s=elapsed))
                released()
            finally:
                cleanup([pidfile])
    marker(4)

    # A real bounded invocation holds the lock; contention never reads configuration.
    cmd_pid.unlink(missing_ok=True)
    first = subprocess.Popen([str(DOIT), "cron-run", "bounded", "--", "sh", "-c",
                              f"echo $$ > {cmd_pid}; exec sleep 30"],
                             env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_file(cmd_pid)
        with patch.object(cron_run, "_limit", side_effect=AssertionError("lookup during contention")):
            skipped = root / "must-not-run"
            check(cron_run.run("bounded", ["touch", str(skipped)], root, manifest) == 0
                  and not skipped.exists(), "1651 AC5: contending bounded start does not execute")
        skips = [json.loads(l) for l in cron_run.skip_store_path("bounded", root).read_text().splitlines()]
        check(len(skips) == 1 and skips[0]["reason"] == "already-running", "1651 AC5: exactly one skip")
        record(5, dict(skip=skips[0]))
        first.communicate(timeout=6)
        check(first.returncode == 124, "1651 AC5: lock holder was a bounded run")
    finally:
        cleanup([cmd_pid])
        if first.poll() is None:
            first.kill()
        first.communicate(timeout=5)
    cron_run.skip_store_path("bounded", root).unlink()

    configured('kill_after_s = 5')
    p, elapsed, end = cli(normal)
    check((p.returncode, p.stdout, p.stderr) == (3, b"out\n", b"err 7\n")
          and set(end) == old_keys | {"kill_after_s", "kill_grace_s"}
          and end["kill_after_s"] == 5 and end["kill_grace_s"] == 10,
          "1651 AC6: within-limit output and additive default grace")
    record(6, dict(end=end, stdout=p.stdout.decode(), stderr=p.stderr.decode()))
    raw = (b"x" * 5000 + b"\n" + b"line\n" * 30 + b"\xe2\x98\x83\xff")
    p, elapsed, end = cli([sys.executable, "-c", "import os,time; "
                          "os.write(2,b'x'*5000+b'\\n'+b'line\\n'*30+b'\\xe2'); "
                          "time.sleep(.05); os.write(2,b'\\x98\\x83\\xff')"])
    check(p.returncode == 0 and p.stderr == raw and end["error_tail"].endswith("☃�")
          and len(end["error_tail"]) <= 2000 and len(end["error_tail"].splitlines()) <= 20,
          "1651 AC6: raw stderr passthrough, split Unicode decoding and bounded tail")
    record(6, dict(raw_stderr_sha256=hashlib.sha256(p.stderr).hexdigest(), end=end))
    rc, elapsed, end, stderr = inprocess([sys.executable, "-c", "import os; os.write(2,b'\\xe2')"])
    check(rc == 0 and stderr == end["error_tail"] == "�",
          "1651 AC6: final incomplete byte sequence is flushed to a text capture")
    record(6, dict(text_capture=stderr, end=end))
    marker(6)

    invalid = [(f'kill_after_s = {v}\nkill_grace_s = 1') for v in ('"1"', 'true', '0', '-5', '1.5', '86401')]
    invalid += [(f'kill_after_s = 1\nkill_grace_s = {v}') for v in ('0', '301', 'true', '1.5', '"1"')]
    for text in invalid:
        configured(text)
        p, elapsed, end = cli(sleep2)
        check(p.returncode == 0 and set(end) == old_keys | {"kill_config"}
              and end["kill_config"] == "invalid", "1651 AC7: strict invalid config fails open")
        record(7, dict(config=text, end=end))
    for text in ('[[row]]\nname="bounded"\nkill_grace_s=0', 'not valid TOML', None):
        if text is None:
            manifest.unlink()
        else:
            manifest.write_text(text)
        p, elapsed, end = cli(sleep2)
        check(p.returncode == 0 and set(end) == old_keys, "1651 AC7: unreadable/unopted config stays dormant")
        record(7, dict(config=text, end=end))
    configured()
    with manifest.open("a") as f:
        f.write('[[row]]\nname="unrelated"\n')
    p, elapsed, end = cli(sleep30)
    check(p.returncode == 124, "1651 AC7: unrelated malformed row does not disable limit")
    record(7, dict(unrelated_incomplete_row=end))
    lockpath = cron_run.store_path("bounded", root).with_suffix(".lock")
    lockpath.unlink()
    lockpath.mkdir()
    try:
        p, elapsed, end = cli(sleep30)
        check(p.returncode == 124, "1651 AC7: unlocked fallback still bounded")
        record(7, dict(unlocked=end))
    finally:
        lockpath.rmdir()
    p, elapsed, end = cli(sleep2, {"DOIT_CRON_RUN_TEST": "0"})
    check(p.returncode == 0 and set(end) == old_keys, "1651 AC7: manifest env override requires test gate")
    p, elapsed, end = cli(sleep30)
    check(p.returncode == 124, "1651 AC7: gated CLI manifest applies")
    record(7, dict(gated=end))
    # Switch checks short-circuit even an unreadable manifest/sentinel.
    with patch.object(pathlib.Path, "read_text", side_effect=AssertionError("manifest read")):
        with patch.dict(os.environ, {"DOIT_CRON_KILL": "off"}):
            check(cron_run._limit("bounded", cron_run.store_path("bounded", root), manifest) == {},
                  "1651 AC7: exact env off bypasses reads")
    with patch.object(pathlib.Path, "stat", side_effect=PermissionError("sentinel")):
        check(cron_run._limit("bounded", cron_run.store_path("bounded", root), manifest) == {},
              "1651 AC7: sentinel check failure disables kills")
    marker(7)

    configured()
    with patch.object(cron_run, "_killpg", side_effect=PermissionError("fixture")):
        rc, elapsed, end, stderr = inprocess(sleep2)
    check(rc == 0 and elapsed >= 2 and end["error_class"] == "" and "PermissionError" in end["kill_error"]
          and end["kill_signals"] == [] and "killed" not in end, "1651 AC8: refused signal fails open")
    record(8, dict(end=end, elapsed_s=elapsed))
    marker(8)

    # Pin exact docstring promises and require the margin on every future opt-in.
    added = ("A run stopped at its crons.toml `kill_after_s` limit exits 124 when SIGTERM sufficed and 137 "
             "when SIGKILL was sent (as timeout(1)). A run with such a limit that is stopped because cron-run "
             "itself received signal n exits `128+n`. On a run with such a limit, cron-run stops reading CMD's "
             "stderr at most one `kill_grace_s` after its last signal, or at once when nothing in CMD's group "
             "is left to signal, and records `stderr_abandoned` on the `end` row.")
    promise = ("`cron-run` must never change a job's behaviour (Constraints), with one exception: a run whose "
               "crons.toml row declares `kill_after_s`, while kills are not switched off, runs in its own session "
               "and is stopped once that limit passes (SIGTERM to its process group, then SIGKILL only if a member "
               "survives the grace), exits as above, and ends its stderr with one `cron-run:` line. A failure to "
               "write a row (unwritable root, full disk, concurrent writer) is swallowed — nothing is printed, "
               "nothing raises past this module — and, apart from that exception, CMD's exit code and stdout/stderr "
               "pass through exactly as CMD produced them. Only alarms reach the ledger, and only through `look` "
               "(`_check_cron_runs`); this module writes NOTHING to `$DOIT_ROOT/events/`.")
    doc = " ".join(cron_run.__doc__.split())
    check(all(" ".join(s.split()) in doc for s in (added, promise)), "1651 AC9: exact narrow docstring promises")
    real_rows = tomllib.loads((REPO / "crons.toml").read_text())["row"]
    for row in real_rows:
        if "kill_after_s" in row:
            after, grace = row["kill_after_s"], row.get("kill_grace_s", 10)
            check(type(after) is int and 1 <= after <= 86400 and type(grace) is int and 1 <= grace <= 300
                  and row["max_runtime_s"] >= after + 2 * grace + 60, "1651 AC9: opt-in validation and margin")
    record(9, dict(docstring_matches=True, opted_rows=[r["name"] for r in real_rows if "kill_after_s" in r]))
    marker(9)

    import look
    from datetime import timezone
    for classes in (("killed",), ("killed",) * 3, ("killed", "killed", "")):
        with tempfile.TemporaryDirectory(prefix="cron-look-") as directory:
            fresh = pathlib.Path(directory)
            look_manifest = fresh / "crons.toml"
            look_manifest.write_text('[[row]]\nname="bounded"\nwhere="user"\nschedule="* * * * *"\n'
                                     'command="true"\nsig="true"\npath="/usr/bin/true"\nowner="albert"\n'
                                     'project="do-it-v2"\nmax_runtime_s=60\n')
            for cls in classes:
                cron_run._append_row(cron_run.store_path("bounded", fresh), dict(ev="start", ts=cron_run._now_iso(), pid=1))
                end = dict(ev="end", ts=cron_run._now_iso(), rc=124 if cls else 0, duration_s=12,
                           error_class=cls, error_tail="", start_ts=cron_run._now_iso(), kill_after_s=1, kill_grace_s=1)
                if cls:
                    end.update(killed=True, kill_reason="deadline", kill_pgid=1, kill_elapsed_s=1,
                               kill_signals=["TERM"], cmd_reaped=True, cmd_rc=143)
                cron_run._append_row(cron_run.store_path("bounded", fresh), end)
            briefs = []
            look._check_cron_runs(datetime.now(timezone.utc), [], fresh, briefs, set(), look_manifest)
            check(not any(b.get("condition") == "reading-undetermined" for b in briefs),
                  "1651 AC10: no undetermined reading")
            check(len(briefs) == (1 if classes == ("killed",) * 3 else 0), "1651 AC10: exact brief count")
            if briefs:
                check(briefs[0]["condition"] == "repeat-failure" and "killed" in briefs[0]["why"],
                      "1651 AC10: repeat-failure names killed")
            record(10, dict(classes=classes, briefs=briefs))
    marker(10)

    for mode in ("cli", "inprocess"):
        cmd = ["sh", "-c", f"(trap '' TERM; exec sleep 30) >/dev/null 2>&1 & echo $! > {child_pid}; exec sleep 30"]
        try:
            if mode == "cli":
                p, elapsed, end = cli(cmd)
                rc = p.returncode
            else:
                rc, elapsed, end, stderr = inprocess(cmd)
            check(rc == 137 and elapsed < 6 and end["kill_signals"] == ["TERM", "KILL"]
                  and end["cmd_reaped"] and end["killed"] and stopped(int(child_pid.read_text())),
                  "1651 AC11: group escalation continues after CMD reaped")
            record(11, dict(mode=mode, end=end, elapsed_s=elapsed))
            released()
        finally:
            cleanup([child_pid])
    marker(11)
    marker(5)

    # Signal wrapper only; ignored HUP remains ignored. No preexec_fn needed.
    for ignored_hup in (False, True):
        configured(f'kill_after_s = {2 if ignored_hup else 30}\nkill_grace_s = 1')
        cmd_pid.unlink(missing_ok=True)
        command = [sys.executable, "-c", "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)", str(cmd_pid)]
        launcher = [str(DOIT), "cron-run", "bounded", "--", *command]
        if ignored_hup:
            launcher = [sys.executable, "-c", "import os,signal,sys; signal.signal(signal.SIGHUP,signal.SIG_IGN); os.execv(sys.argv[1],sys.argv[1:])", *launcher]
        p = subprocess.Popen(launcher, env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            _wait_file(cmd_pid)
            began = time.monotonic()
            if ignored_hup:
                time.sleep(.5)
            p.send_signal(signal.SIGHUP if ignored_hup else signal.SIGTERM)
            stderr = p.communicate(timeout=5)[1]
            end = rows()[-1]
            check(time.monotonic() - began < 5 and p.returncode == (124 if ignored_hup else 143)
                  and end["killed"] and end["kill_reason"] == ("deadline" if ignored_hup else "wrapper-signal")
                  and end["kill_signals"] == ["TERM"] and stopped(int(cmd_pid.read_text())),
                  "1651 AC12: wrapper signal forwarding and inherited ignore")
            record(12, dict(ignored_hup=ignored_hup, end=end))
        finally:
            cleanup([cmd_pid])
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
    configured()
    rc, elapsed, end, stderr = inprocess(sleep30, threaded=True)
    check(rc == 124 and elapsed < 6, "1651 AC12: non-main thread still enforces deadline")
    record(12, dict(threaded=end, handlers_restored=True))
    # Start failure preserves the same scoped-handler and additive-key contracts.
    rc, elapsed, end, stderr = inprocess([str(root / "missing-command")])
    check(rc == 127 and set(end) == old_keys | {"kill_after_s", "kill_grace_s"},
          "1651: bounded start failure has only configured keys")
    marker(12)
    marker(13)


_runtime_checks()
_r14ac14()

print(f"cron_run: {N} checks pass")

# L-spec-1676: only the exact watchdog start/end store keeps 50 hours.
for basename, limit in (("pane-watchdog.jsonl", 1200), ("another-job.jsonl", 50),
                        ("pane-watchdog.skips.jsonl", 50), ("other-pane-watchdog.jsonl", 50)):
    retained = TMP / "retention-1676" / basename
    for i in range(1210):
        cron_run._append_row(retained, dict(ev="end" if i % 2 else "start", n=i))
    # Fresh-open read-back; never an in-memory mirror of the writer.
    with retained.open() as stream:
        rows = [json.loads(line) for line in stream]
    check(len(rows) == limit, f"1676: {basename} retains {limit}")
    check(rows[0]["n"] == 1210-limit and rows[-1]["n"] == 1209, f"1676: newest rows in {basename}")
print("L-spec-1676 watchdog retention ok")
