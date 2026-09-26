#!/usr/bin/env python3
"""One runnable check on `ghlimit` and `scripts/gh/gh`. Run: python3 test_ghlimit.py

Every AC1-AC11 section scopes its own `GH_LIMIT_STATE` under a fresh temp dir
and swaps `ghlimit.subprocess`/`ghlimit.sleep`/`ghlimit.clock` before any
call — no real `gh` process, no real sleep, no network. The two exceptions,
both explicitly permitted (spec A7): AC9(a) compares against the REAL
`/usr/bin/gh --version` (pass-through, no network), and AC12-AC15 invoke the
real `scripts/gh/gh` as a subprocess with `DOIT_REAL_GH` always pointed at a
local stub script — never a real GitHub call.
"""
import json, os, pathlib, pty, subprocess as real_sp, sys, tempfile, threading, time, types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ghlimit  # noqa: E402

SHIM = HERE.parent / "scripts" / "gh" / "gh"

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


TMP = pathlib.Path(tempfile.mkdtemp(prefix="ghlimit-test-"))
_state_n = [0]


def fresh_state():
    _state_n[0] += 1
    return TMP / f"state-{_state_n[0]}.json"


def _ns(rc, out, err):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeSubprocess:
    """Stands in for `ghlimit.subprocess`. Answers `<real_gh> api rate_limit`
    from a canned quota reading (or a failure mode); everything else is
    consumed off a canned queue, in order."""
    def __init__(self):
        self.calls = []
        self.quota = {"core": {"limit": 5000, "remaining": 5000, "reset": 0},
                      "graphql": {"limit": 5000, "remaining": 5000, "reset": 0}}
        self.quota_fail = False
        self.quota_raise = False
        self.quota_bad_json = False
        self.queue = []

    def run(self, argv, capture_output=True, text=True):
        self.calls.append(list(argv))
        if len(argv) >= 3 and argv[1:3] == ["api", "rate_limit"]:
            if self.quota_raise:
                raise OSError("boom")
            if self.quota_fail:
                return _ns(1, "", "boom")
            if self.quota_bad_json:
                return _ns(0, "not-json", "")
            return _ns(0, json.dumps({"resources": self.quota}), "")
        if self.queue:
            rc, out, err = self.queue.pop(0)
            return _ns(rc, out, err)
        return _ns(0, "", "")


FAKE = FakeSubprocess()
SLEEPS = []


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        return self.t

    def advance(self, s):
        self.t += s


CLOCK = FakeClock()


def fake_sleep(s):
    SLEEPS.append(s)
    CLOCK.advance(s)
    # simulate the quota actually resetting once we reach its reset time
    FAKE.quota["core"]["remaining"] = FAKE.quota["core"]["limit"]
    FAKE.quota["core"]["reset"] = CLOCK.now() + 3600


ghlimit.subprocess = FAKE
ghlimit.sleep = fake_sleep
ghlimit.clock = CLOCK.now
os.environ["DOIT_REAL_GH"] = "REALGH"


def reset_all():
    FAKE.calls.clear()
    FAKE.queue.clear()
    FAKE.quota_fail = FAKE.quota_raise = FAKE.quota_bad_json = False
    SLEEPS.clear()


def new_state(remaining_core=5000, limit_core=5000, reset_core=None):
    os.environ["GH_LIMIT_STATE"] = str(fresh_state())
    if reset_core is None:
        reset_core = CLOCK.t + 100
    FAKE.quota = {"core": {"limit": limit_core, "remaining": remaining_core, "reset": reset_core},
                  "graphql": {"limit": 5000, "remaining": 5000, "reset": CLOCK.t + 3600}}


def quota_reads():
    return [c for c in FAKE.calls if c[1:3] == ["api", "rate_limit"]]


# ══ AC1 · refresh only when missing/stale ══════════════════════════════════
reset_all()
new_state()
ghlimit.gate(wait=False)
check(len(quota_reads()) == 1, f"AC1: a fresh state makes exactly one real read: {FAKE.calls}")
CLOCK.advance(59)
ghlimit.gate(wait=False)
check(len(quota_reads()) == 1, f"AC1: a 59s-old state reuses the cache: {FAKE.calls}")
CLOCK.advance(2)  # 61s since checked_at
ghlimit.gate(wait=False)
check(len(quota_reads()) == 2, f"AC1: a 61s-old state makes exactly one new read: {FAKE.calls}")

# ══ AC2 · low exactly when remaining - spent < max(100, 5% of limit) ═══════
reset_all(); new_state(remaining_core=200, limit_core=5000)
check(ghlimit.gate(wait=False)["ok"] is False, "AC2: limit=5000, remaining=200 (threshold 250) is low")
reset_all(); new_state(remaining_core=250, limit_core=5000)
check(ghlimit.gate(wait=False)["ok"] is True, "AC2: limit=5000, remaining=250 meets the threshold")
reset_all(); new_state(remaining_core=99, limit_core=30)
check(ghlimit.gate(wait=False)["ok"] is False, "AC2: limit=30 floors the threshold at 100; 99 is low")
reset_all(); new_state(remaining_core=100, limit_core=30)
check(ghlimit.gate(wait=False)["ok"] is True, "AC2: limit=30, remaining=100 meets the 100 floor")

reset_all(); new_state(remaining_core=5000, limit_core=5000)
FAKE.queue = [(0, "ok", "")]
ghlimit.run(["gh", "pr", "list"], wait=False)
s1 = json.loads(pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_text())
check(s1["spent_since_check"] == 1, f"AC2: one gated call increments spent_since_check by 1: {s1}")
FAKE.queue = [(0, "ok", "")]
ghlimit.run(["gh", "pr", "view", "x"], wait=False)
s2 = json.loads(pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_text())
check(s2["spent_since_check"] == 2, f"AC2: a second gated call increments again: {s2}")

# ══ AC3 · wait formula, waited_s, and the non-blocking branch ══════════════
reset_all(); new_state(remaining_core=10, limit_core=5000)
g3a = ghlimit.gate(wait=False)
check(g3a["ok"] is False and g3a["waited_s"] == 0, f"AC3: wait=False never blocks: {g3a}")
check(SLEEPS == [], "AC3: wait=False truly never calls sleep")

reset_all()
t_before = CLOCK.t
new_state(remaining_core=10, limit_core=5000, reset_core=t_before + 100)
g3b = ghlimit.gate(wait=True)
check(g3b["ok"] is True, f"AC3: after waiting past the reset, ok True: {g3b}")
check(SLEEPS == [105.0], f"AC3: sleep argument is (latest low reset + 5s), capped at +120s: {SLEEPS}")
check(g3b["waited_s"] == 105.0, f"AC3: waited_s equals the time actually slept: {g3b}")

reset_all(); new_state(remaining_core=5000, limit_core=5000)
g3c = ghlimit.gate(wait=True)
check(g3c["ok"] is True and g3c["waited_s"] == 0, f"AC3: nothing low -> waited_s 0: {g3c}")
check(SLEEPS == [], "AC3: nothing low -> sleep is never called")

# ══ AC4 · pass-through bypasses the gate entirely ══════════════════════════
reset_all(); new_state(remaining_core=5000, limit_core=5000)
ghlimit.gate(wait=False)  # establishes a real state file to prove byte-identity against
before4 = pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_bytes()
for argv in (["gh", "api", "rate_limit"], ["gh", "auth", "status"], ["gh", "--version"], ["gh", "help"]):
    FAKE.queue = [(0, "out", "")]
    ghlimit.run(argv, wait=False)
    after4 = pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_bytes()
    check(after4 == before4, f"AC4: {argv} must not touch the state file")

# ══ AC5 · a rate-limited READ-ONLY argv retries exactly once ═══════════════
def ac5(stderr_shape, expected_wait=None):
    reset_all(); new_state(remaining_core=5000, limit_core=5000, reset_core=CLOCK.t + 42)
    argv = ["gh", "pr", "list", "--repo", "x/y"]
    FAKE.queue = [(1, "", stderr_shape), (0, "retried-ok", "")]
    result = ghlimit.run(argv, wait=False)
    execs = [c for c in FAKE.calls if c == ["REALGH", "pr", "list", "--repo", "x/y"]]
    check(len(execs) == 2, f"AC5 {stderr_shape!r}: exactly two invocations of the same argv: {FAKE.calls}")
    check(result == (0, "retried-ok", ""), f"AC5 {stderr_shape!r}: returns the retry's own result: {result}")
    if expected_wait is not None:
        check(SLEEPS == [expected_wait], f"AC5 {stderr_shape!r}: wait formula: {SLEEPS}")


ac5("API rate limit exceeded", expected_wait=42.0)
ac5("you have exceeded a secondary rate limit for the GitHub API", expected_wait=60.0)
ac5("HTTP 429: too many requests", expected_wait=60.0)
ac5("HTTP 429: Retry-After: 17", expected_wait=17.0)

# ══ AC6 · a rate-limited MUTATING argv is returned unchanged, never retried ═
reset_all(); new_state(remaining_core=5000, limit_core=5000)
argv6 = ["gh", "pr", "comment", "URL", "--body", "hi"]
FAKE.queue = [(1, "", "API rate limit exceeded")]
result6 = ghlimit.run(argv6, wait=False)
check(result6 == (1, "", "API rate limit exceeded"), f"AC6: the original failure is returned unchanged: {result6}")
execs6 = [c for c in FAKE.calls if c == ["REALGH", "pr", "comment", "URL", "--body", "hi"]]
check(len(execs6) == 1, f"AC6: the mutating argv executed exactly once, never retried: {FAKE.calls}")

# ══ AC7 · a failed quota read fails OPEN, with one log line ════════════════
for mode in ("quota_fail", "quota_raise", "quota_bad_json"):
    reset_all(); new_state()
    setattr(FAKE, mode, True)
    log = []
    real_log = ghlimit._log
    ghlimit._log = lambda msg: log.append(msg)
    try:
        g7 = ghlimit.gate(wait=False)
        check(g7["ok"] is True, f"AC7 {mode}: gate() fails open: {g7}")
        FAKE.queue = [(0, "proceeded", "")]
        result7 = ghlimit.run(["gh", "pr", "list"], wait=False)
        check(result7 == (0, "proceeded", ""), f"AC7 {mode}: run() still executes the real call: {result7}")
        check(len(log) >= 1, f"AC7 {mode}: at least one log line records the failure: {log}")
    finally:
        ghlimit._log = real_log
    setattr(FAKE, mode, False)

# ══ AC8 · concurrency: interleaved read-modify-write never loses an update ═
reset_all(); new_state(remaining_core=5000, limit_core=5000)
ghlimit.gate(wait=False)  # materializes the state file before the race starts
N_THREADS, PER_THREAD = 8, 25


def _worker():
    for _ in range(PER_THREAD):
        ghlimit._bump_spent()


threads8 = [threading.Thread(target=_worker) for _ in range(N_THREADS)]
for t in threads8:
    t.start()
for t in threads8:
    t.join()
s8 = json.loads(pathlib.Path(os.environ["GH_LIMIT_STATE"]).read_text())
check(s8["spent_since_check"] == N_THREADS * PER_THREAD,
      f"AC8: {N_THREADS}x{PER_THREAD} interleaved increments all land, none lost: {s8}")

# ══ AC9 · run()'s tuple shape matches a direct subprocess.run() on real gh ═
if pathlib.Path("/usr/bin/gh").is_file():
    os.environ["DOIT_REAL_GH"] = "/usr/bin/gh"
    ghlimit.subprocess = real_sp
    direct9a = real_sp.run(["/usr/bin/gh", "--version"], capture_output=True, text=True)
    wrapped9a = ghlimit.run(["gh", "--version"], wait=False)
    check(wrapped9a == (direct9a.returncode, direct9a.stdout, direct9a.stderr),
          f"AC9: pass-through tuple matches a direct capture: {wrapped9a}")
    ghlimit.subprocess = FAKE
    os.environ["DOIT_REAL_GH"] = "REALGH"
else:
    check(False, "AC9 requires /usr/bin/gh present on this box (confirmed live)")

STUB_GH = TMP / "stub_gh.py"
STUB_GH.write_text(
    "#!/usr/bin/env python3\n"
    "import json, sys, time\n"
    "argv = sys.argv[1:]\n"
    "if argv[:2] == ['api', 'rate_limit']:\n"
    "    now = int(time.time())\n"
    "    print(json.dumps({'resources': {'core': {'limit': 5000, 'remaining': 5000, 'reset': now + 3600},\n"
    "                                     'graphql': {'limit': 5000, 'remaining': 5000, 'reset': now + 3600}}}))\n"
    "else:\n"
    "    print('STUB ' + ' '.join(argv))\n"
)
STUB_GH.chmod(0o755)
os.environ["DOIT_REAL_GH"] = str(STUB_GH)
new_state()
ghlimit.subprocess = real_sp
wrapped9b = ghlimit.run(["gh", "pr", "list", "--repo", "x/y"], wait=False)
direct9b = real_sp.run([str(STUB_GH), "pr", "list", "--repo", "x/y"], capture_output=True, text=True)
check(wrapped9b == (direct9b.returncode, direct9b.stdout, direct9b.stderr),
      f"AC9: gated tuple matches a direct capture of the same underlying command: {wrapped9b}")
ghlimit.subprocess = FAKE
os.environ["DOIT_REAL_GH"] = "REALGH"

# ══ AC10 · a low quota skips argv entirely ══════════════════════════════════
reset_all(); new_state(remaining_core=50, limit_core=5000)
result10 = ghlimit.run(["gh", "pr", "list"], wait=False)
check(result10 == (75, "", "ghlimit: quota low, skipped"), f"AC10: the skip tuple: {result10}")
execs10 = [c for c in FAKE.calls if c and c[0] == "REALGH" and c[1:3] != ["api", "rate_limit"]]
check(execs10 == [], f"AC10: argv is never executed: {FAKE.calls}")

# ══ AC11 · never resolves `gh` through PATH, even with a `gh` shim on it ═══
reset_all()
real_stub_calls = TMP / "real_stub.calls"
REAL_STUB = TMP / "real_stub.py"
REAL_STUB.write_text(
    "#!/usr/bin/env python3\n"
    f"import sys, pathlib\n"
    f"pathlib.Path({str(real_stub_calls)!r}).open('a').write(' '.join(sys.argv[1:]) + chr(10))\n"
    "print('REAL-STUB-OUT')\n"
)
REAL_STUB.chmod(0o755)
fakebin = TMP / "fakebin"
fakebin.mkdir(exist_ok=True)
fake_gh = fakebin / "gh"
fake_gh.write_text("#!/bin/sh\necho FAKE-PATH-GH-RAN >&2\nexit 99\n")
fake_gh.chmod(0o755)

old_path = os.environ.get("PATH", "")
os.environ["PATH"] = str(fakebin) + os.pathsep + old_path
os.environ["DOIT_REAL_GH"] = str(REAL_STUB)
seed11 = fresh_state()
os.environ["GH_LIMIT_STATE"] = str(seed11)
seed11.write_text(json.dumps({"core": {"limit": 5000, "remaining": 5000, "reset": ghlimit.clock() + 3600},
                              "graphql": {"limit": 5000, "remaining": 5000, "reset": ghlimit.clock() + 3600},
                              "checked_at": ghlimit.clock(), "spent_since_check": 0}))
ghlimit.subprocess = real_sp
try:
    result11 = ghlimit.run(["gh", "pr", "list"], wait=False)
finally:
    os.environ["PATH"] = old_path
    ghlimit.subprocess = FAKE
    os.environ["DOIT_REAL_GH"] = "REALGH"
check("REAL-STUB-OUT" in result11[1], f"AC11: the real stub (never the PATH fake) answered: {result11}")
check(real_stub_calls.read_text().count("\n") == 1,
      f"AC11: the real stub was invoked exactly once: {real_stub_calls.read_text()}")
check(SLEEPS == [], f"AC11: never sleeps: {SLEEPS}")

ghlimit.sleep, ghlimit.clock = fake_sleep, CLOCK.now  # restored for cleanliness

# ══════════════════════════════════════════════════════════════════════════
# AC12-AC15 · `scripts/gh/gh`, invoked directly by path as a real subprocess
# ══════════════════════════════════════════════════════════════════════════
REC_STUB = TMP / "rec_stub.py"
REC_STUB.write_text(
    "#!/usr/bin/env python3\n"
    "import json, os, sys, time\n"
    "argv = sys.argv[1:]\n"
    "log = os.environ.get('CALL_LOG')\n"
    "if log:\n"
    "    open(log, 'a').write(json.dumps(argv) + chr(10))\n"
    "if argv[:2] == ['api', 'rate_limit']:\n"
    "    now = int(time.time())\n"
    "    reset = int(os.environ.get('FAKE_RESET', now + 3600))\n"
    "    remaining = int(os.environ.get('FAKE_REMAINING', 5000))\n"
    "    print(json.dumps({'resources': {'core': {'limit': 5000, 'remaining': remaining, 'reset': reset},\n"
    "                                     'graphql': {'limit': 5000, 'remaining': 5000, 'reset': now + 3600}}}))\n"
    "else:\n"
    "    print('SHIM-OUT:' + json.dumps(argv))\n"
    "    print('SHIM-ERR:' + json.dumps(argv), file=sys.stderr)\n"
)
REC_STUB.chmod(0o755)


def shim_env(**over):
    e = os.environ.copy()
    e.pop("CALL_LOG", None)
    e.pop("FAKE_RESET", None)
    e.pop("FAKE_REMAINING", None)
    e.update(over)
    return e


# ── AC12 · gate-then-exec, byte-for-byte, both a high- and a low-quota run ──
env12a = shim_env(DOIT_REAL_GH=str(REC_STUB), GH_LIMIT_STATE=str(fresh_state()))
r12a = real_sp.run([str(SHIM), "pr", "list", "--repo", "x/y"], capture_output=True, text=True, env=env12a)
direct12a = real_sp.run([str(REC_STUB), "pr", "list", "--repo", "x/y"], capture_output=True, text=True, env=env12a)
check(r12a.returncode == 0 and r12a.stdout == direct12a.stdout,
      f"AC12: high-quota run passes through byte-for-byte: {r12a.stdout!r} vs {direct12a.stdout!r}")

env12b = shim_env(DOIT_REAL_GH=str(REC_STUB), GH_LIMIT_STATE=str(fresh_state()),
                   FAKE_REMAINING="10", FAKE_RESET=str(int(time.time()) + 1))
t0 = time.time()
r12b = real_sp.run([str(SHIM), "pr", "list", "--repo", "x/y"], capture_output=True, text=True, env=env12b)
elapsed12b = time.time() - t0
check(r12b.returncode == 0, f"AC12 low-quota: still exits 0 after the wait: {r12b}")
check(elapsed12b >= 4.0, f"AC12 low-quota: elapsed time proves the wait (>=4s): {elapsed12b}")
check("waiting" in r12b.stderr and "remaining" in r12b.stderr,
      f"AC12 low-quota: stderr names the wait: {r12b.stderr!r}")
direct12b = real_sp.run([str(REC_STUB), "pr", "list", "--repo", "x/y"], capture_output=True, text=True, env=env12b)
check(r12b.stdout == direct12b.stdout, "AC12 low-quota: stdout still passes through byte-for-byte")

# ── AC13 · inherited stdio (isatty), proving `os.execv`, not `subprocess.run`
TTY_STUB = TMP / "tty_stub.py"
TTY_STUB.write_text("#!/usr/bin/env python3\nimport os\n"
                    "print('ISATTY_STDOUT=%s ISATTY_STDERR=%s' % (os.isatty(1), os.isatty(2)))\n")
TTY_STUB.chmod(0o755)


def run_under_pty(argv, env):
    master, slave = pty.openpty()
    p = real_sp.Popen(argv, stdout=slave, stderr=slave, stdin=slave, env=env)
    os.close(slave)
    p.wait()
    out = b""
    try:
        while True:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    os.close(master)
    return out.decode(errors="replace")


def isatty_line(out):
    lines = [l for l in out.splitlines() if l.startswith("ISATTY_")]
    return lines[-1] if lines else None


baseline13 = run_under_pty([str(TTY_STUB)], os.environ.copy())
base_line13 = isatty_line(baseline13)
check(base_line13 == "ISATTY_STDOUT=True ISATTY_STDERR=True",
      f"AC13 baseline: a direct run under a pty is a tty: {baseline13!r}")
env13 = shim_env(DOIT_REAL_GH=str(TTY_STUB), GH_LIMIT_STATE=str(fresh_state()))
shim_out13 = run_under_pty([str(SHIM), "pr", "list"], env13)
check(isatty_line(shim_out13) == base_line13,
      f"AC13: the shim's exec preserves the same isatty reading: {shim_out13!r}")

# ── AC14 · the self-recursion guard, before any gate call ══════════════════
state14 = fresh_state()
env14 = shim_env(DOIT_REAL_GH=str(SHIM), GH_LIMIT_STATE=str(state14))
r14 = real_sp.run([str(SHIM), "pr", "list"], capture_output=True, text=True, env=env14)
check(r14.returncode != 0, f"AC14: the self-reference is refused non-zero: {r14}")
check(not state14.exists(), "AC14: zero gate() calls — no state file was ever written")

# ── AC15 · pass-through skips gate(); a gated argv reaches gate() first ════
log15a = TMP / "log15a.jsonl"
env15a = shim_env(DOIT_REAL_GH=str(REC_STUB), GH_LIMIT_STATE=str(fresh_state()), CALL_LOG=str(log15a))
real_sp.run([str(SHIM), "--version"], capture_output=True, text=True, env=env15a)
calls15a = [json.loads(l) for l in log15a.read_text().splitlines()] if log15a.exists() else []
check(not any(c[:2] == ["api", "rate_limit"] for c in calls15a), f"AC15: pass-through never reaches gate(): {calls15a}")

log15b = TMP / "log15b.jsonl"
env15b = shim_env(DOIT_REAL_GH=str(REC_STUB), GH_LIMIT_STATE=str(fresh_state()), CALL_LOG=str(log15b))
real_sp.run([str(SHIM), "pr", "list", "--repo", "x/y"], capture_output=True, text=True, env=env15b)
calls15b = [json.loads(l) for l in log15b.read_text().splitlines()]
check(any(c[:2] == ["api", "rate_limit"] for c in calls15b), f"AC15: a gated argv DOES reach gate() first: {calls15b}")
check(any(c == ["pr", "list", "--repo", "x/y"] for c in calls15b),
      f"AC15: the gated argv still reaches DOIT_REAL_GH with the identical argv: {calls15b}")

LINES = pathlib.Path(__file__).read_text().splitlines()
check(len(LINES) <= 500, f"SD10: test_ghlimit.py is {len(LINES)} physical lines (cap 500)")
GHLIMIT_SRC = (HERE / "ghlimit.py").read_text().splitlines()
check(len(GHLIMIT_SRC) <= 400, f"SD10: ghlimit.py is {len(GHLIMIT_SRC)} physical lines (cap 400)")

print(f"ghlimit: {N} checks pass")
