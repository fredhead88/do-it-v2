#!/usr/bin/env python3
"""Fixture-only regression groups for L-spec-1663; accepts AC ids as argv."""
import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest.mock as mock

SOURCE = Path(__file__).resolve().parent
SESSION = tempfile.TemporaryDirectory(prefix="preflight-tests-")
BASE = Path(SESSION.name)
os.environ.update(HOME=str(BASE / "home"), DOIT_ROOT=str(BASE / "root"),
                  DOIT_SCRATCH=str(BASE / "scratch"))
import spec_preflight as sp
import fold

NOW = datetime.now(timezone.utc)


def proc(argv, **kw):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, check=True, timeout=30, **kw).stdout.strip()


class Fixture:
    def __init__(self, project="do-it-v2"):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.repo = self.root / "repos" / project
        self.project = project
        self.repo.mkdir(parents=True)
        (self.root / "content").mkdir()
        (self.root / "events").mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "fixture")
        self.git("commit", "-q", "--allow-empty", "-m", "root")
        self.events = []

    def git(self, *args):
        return proc(["git", "-C", str(self.repo), *args])

    def file(self, path, body):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def event(self, kind, sid="L-spec-9001", **fields):
        actor = {"spec-written": "spec-writer", "spec-closed": "operator",
                 "build-started": "builder"}.get(kind, "executor")
        event = dict(type=kind, subject=sid,
                     ts=(NOW-timedelta(minutes=10)+timedelta(milliseconds=len(self.events))).isoformat(), **fields)
        path = self.root / "events" / f"L-{actor}-fixture.jsonl"
        with path.open("a") as out:
            out.write(json.dumps(event) + "\n")
        self.events.append(event)

    def spec(self, verify="/usr/bin/python3 -c 'import json'", ac="works.",
             writes=("src/a.py",), sid="L-spec-9001"):
        path = self.root / "content" / (sid + ".md")
        path.write_text(f"project: {self.project}\nbase_sha: {self.git('rev-parse', 'HEAD')}\n"
                        "## Acceptance\nAC1 [backend]: " + ac + "\n"
                        "## Verification\n```bash\n" + verify + "\n```\n"
                        "## Interfaces\nWrites: " + " ".join(writes) + "\n")
        self.event("spec-written", sid, path=str(path), project=self.project,
                   footprint=list(writes))
        return path

    def run(self):
        return sp.run("L-spec-9001", root=self.root, now=NOW)

    def state(self):
        return [self.git(*args) for args in (("branch", "--list"), ("for-each-ref",),
                                           ("worktree", "list", "--porcelain"))]


def check(body, n):
    return [f for f in body["findings"] if f["check"] == n]


def AC1():
    f = Fixture("albert-scott")
    f.file("probe.py", "import json\nassert json.loads('1') == 1\n")
    f.commit()
    for command in ("$PWD/.venv/bin/python -m pytest x", "../.venv/bin/python x",
                    "python3 x", "python x", "/usr/bin/python3 -m pytest x",
                    "/usr/bin/python3 tests/test_x.py", "/usr/bin/python3 -c 'import psycopg2'"):
        f.spec(command)
        assert check(f.run(), 1), command
    for command in ("/opt/albert-scott/.venv/bin/python -m pytest x",
                    "/usr/bin/python3 -c 'import json'", "/usr/bin/python3 probe.py"):
        f.spec(command)
        assert not check(f.run(), 1), command
    f.spec("/usr/bin/python3 missing.py")
    assert any(x.startswith("check 1:") for x in f.run()["undetermined"])
    f.spec("/usr/bin/python3 -c 'import json'", ac="python3 x outside Verification.")
    assert not check(f.run(), 1)
    other = Fixture()
    other.spec("python3 x")
    assert not check(other.run(), 1)


def AC2():
    f = Fixture()
    sha = f.git("rev-parse", "HEAD")
    for candidate, expected in ((sha, True), (sha[:7], True), ("a1" * 20, False), ("deadbeef", False)):
        f.spec(ac="commit " + candidate)
        assert bool(check(f.run(), 2)) == expected, candidate
    path = f.spec()
    with path.open("a") as out:
        out.write("\n## Assumptions[]\n" + sha)
    assert not check(f.run(), 2)
    f.spec(ac="commit " + sha)
    f.repo.rename(f.repo.with_name("gone"))
    body = f.run()
    assert not check(body, 2) and any(x.startswith("check 2:") for x in body["undetermined"])


def AC3():
    f = Fixture()
    for path, body in {"src/a.py": "", "src/test_a_import.py": "import a\n",
                       "src/test_a_named.py": "path = 'src/a.py'\n",
                       "src/test_inside.py": "import a\n", "src/test_unrelated.py": "pass\n"}.items():
        f.file(path, body)
    f.commit()
    f.spec(writes=("src/a.py", "src/test_inside.py", "src/new.py"))
    rows = check(f.run(), 3)
    assert {r["id"] for r in rows} == {"src/test_a_import.py", "src/test_a_named.py"}, rows
    assert all("src/a.py" in r["line"] for r in rows)
    f.spec(writes=("src/a.py", "dashboard/a.ts"))
    body = f.run()
    assert len(check(body, 3)) == 3  # the formerly in-Writes import now also reaches a.py
    assert "check 3: unsupported Writes file dashboard/a.ts" in body["undetermined"]
    f.repo.rename(f.repo.with_name("gone"))
    assert any(x.startswith("check 3:") for x in f.run()["undetermined"])


def AC4():
    f = Fixture()
    f.file("src/test_green.py", "assert True\n")
    f.file("src/test_red.py", "raise SystemExit(1)\n")
    f.commit()
    f.spec("/usr/bin/python3 src/test_green.py\n/usr/bin/python3 src/test_red.py\n"
           "/usr/bin/python3 src/test_new.py\n/usr/bin/python3 src/test_green.py::test_x",
           writes=("src/test_new.py",))
    before = f.state()
    clones = []
    actual = sp._process

    def capture(argv, **kw):
        assert isinstance(argv, list) and kw.get("shell") is not True
        if argv[0] == "/usr/bin/python3":
            checkout = Path(kw["cwd"])
            clones.append(checkout)
            assert (checkout / ".git").is_dir()
            assert proc(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == f.git("rev-parse", "main")
            assert not (checkout / ".git/HEAD").read_text().startswith("ref:")
        return actual(argv, **kw)

    with mock.patch.object(sp, "_process", side_effect=capture):
        body = f.run()
    assert body["red_on_master"] == ["src/test_red.py"], body
    assert check(body, 4) == [dict(check=4, id="src/test_new.py", line="new on master")]
    assert any("not runnable by the script harness" in x for x in body["undetermined"])
    assert f.state() == before and all(not p.exists() for p in clones)

    # One total budget: two two-second scripts cannot each get three seconds.
    f.file("src/test_first.py", "import time\ntime.sleep(2)\n")
    f.file("src/test_second.py", "import time\ntime.sleep(2)\n")
    marker = f.base / "pids"
    f.file("src/test_sleep.py", "import os, time\n"
           "child = os.fork()\n"
           "if child == 0:\n    time.sleep(30)\n    os._exit(0)\n"
           f"with open({str(marker)!r}, 'w') as out: out.write(str(os.getpid()) + ' ' + str(child))\n"
           "time.sleep(30)\n")
    f.commit()
    real_popen = subprocess.Popen
    runner_pids = []

    def track(argv, **kw):
        child = real_popen(argv, **kw)
        if argv[0] == '/usr/bin/python3':
            runner_pids.append(child.pid)
        return child

    budget_state = f.state()
    for commands in (("src/test_first.py", "src/test_second.py"), ("src/test_sleep.py",)):
        f.spec("\n".join('/usr/bin/python3 ' + path for path in commands))
        start = time.monotonic()
        with mock.patch.object(sp, "TEST_CAP_S", 3), mock.patch.object(subprocess, "Popen", side_effect=track):
            body = f.run()
        assert time.monotonic() - start < 8
        assert any("timeout" in x and commands[-1] in x for x in body["undetermined"]), body
    assert len(runner_pids) == 3
    assert not any(x.endswith('src/test_first.py') for x in body['red_on_master'])
    for pid in runner_pids + list(map(int, marker.read_text().split())):
        status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while status.exists() and status.read_text().split()[2] not in {"Z", "X"} and time.monotonic() < deadline:
            time.sleep(.01)
        assert not status.exists() or status.read_text().split()[2] in {"Z", "X"}, pid
    assert f.state() == budget_state

    # Real script writes only to its private DOIT_ROOT; observe before cleanup.
    sentinel = f.root / "sentinel"
    sentinel.write_text("unchanged")
    observed = []
    f.file("src/test_isolation.py", "import os, json\nfrom pathlib import Path\n"
           "p = Path(os.environ['DOIT_ROOT']) / 'events/fixture.jsonl'\n"
           "p.parent.mkdir(parents=True); p.write_text('marker')\n")
    f.commit()
    f.spec("/usr/bin/python3 src/test_isolation.py")
    before_files = {str(p): p.read_bytes() for p in f.root.rglob('*') if p.is_file()}

    def isolated(argv, **kw):
        result = actual(argv, **kw)
        if argv[0] == "/usr/bin/python3":
            env = kw["env"]
            names = ("HOME", "DOIT_ROOT", "DOIT_SCRATCH", "TMPDIR", "XDG_CONFIG_HOME",
                     "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
            paths = [Path(env[n]) for n in names]
            assert len(set(paths)) == len(names)
            assert all(p.is_dir() and p.is_relative_to(Path(kw['cwd']).parent) for p in paths)
            assert all(n not in env for n in ("DOIT_LEDGER_FILE", "DOIT_TEST_LIVE_ROOT", "DOIT_LIVE_REPO", "SUPABASE_DB_URL"))
            observed.append((Path(env['DOIT_ROOT']) / 'events/fixture.jsonl').read_text())
        return result

    with mock.patch.object(sp, "_process", side_effect=isolated):
        assert not f.run()["red_on_master"]
    assert observed == ["marker"]
    assert before_files == {str(p): p.read_bytes() for p in f.root.rglob('*') if p.is_file()}

    albert = Fixture("albert-scott")
    albert.file("tests/test_x.py", "")
    albert.commit()
    albert.spec("/opt/albert-scott/.venv/bin/python -m pytest tests/test_x.py")
    called = []

    def runner(argv, **kw):
        if argv[0].startswith("/opt/"):
            called.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return actual(argv, **kw)

    with mock.patch.object(sp, "_process", side_effect=runner):
        albert.run()
    assert called[0][:3] == ["/opt/albert-scott/.venv/bin/python", "-m", "pytest"]
    for unsupported, expected in (("npx vitest run dashboard/a.test.ts", "vitest"),
                                  ("node dashboard/a.test.ts", "node"),
                                  ("dashboard/a.test.ts", "dashboard/a.test.ts")):
        f.spec(unsupported + "\n/usr/bin/python3 src/test_green.py", writes=("dashboard/a.ts",))
        body = f.run()
        assert f"check 4: unsupported runner {expected}" in body["undetermined"], body
        assert "check 3: unsupported Writes file dashboard/a.ts" in body["undetermined"]
        assert not body["red_on_master"]


def AC5():
    f = Fixture()
    f.file("src/test_x.py", "raise SystemExit(1)\n")
    (f.repo / "test_link.py").symlink_to(f.base / "outside.py")
    (f.base / "outside.py").write_text("raise SystemExit(1)\n")
    f.commit()
    marker = f.base / "pwned"
    hostile = (f"src/test_x.py; touch {marker}", f"$(touch {marker})/test_x.py",
               f"`touch {marker}`/test_x.py", "../../etc/test_x.py", "/etc/test_x.py",
               "--rootdir=/ test_x.py", "-k test_x.py", "'src/with space/test_x.py'",
               "`echo unsafe`/test_x.py", "src/test_x.py$SUFFIX", "test_link.py")
    for name in hostile:
        f.spec(name)
        called = []
        actual = sp._process

        def guarded(argv, **kw):
            assert isinstance(argv, list) and kw.get("shell") is not True
            called.append(argv[0])
            return actual(argv, **kw)

        with mock.patch.object(sp, "_process", side_effect=guarded):
            body = f.run()
        assert not marker.exists() and not body["red_on_master"]
        assert any("unsafe test id" in x and ("outside checkout" in x if name == "test_link.py" else True)
                   for x in body["undetermined"]), (name, body)
        assert "/usr/bin/python3" not in called, (name, called)


def AC6():
    f = Fixture()
    f.spec()
    before = {p.name: p.read_bytes() for p in (f.root / "events").iterdir()}
    for body in (f.run(), sp.run("L-spec-unknown", root=f.root)):
        assert set(body) == {"spec", "findings", "red_on_master", "undetermined"}
        assert all(isinstance(body[k], list) for k in ("findings", "red_on_master", "undetermined"))
    assert f.run()["spec"] == "L-spec-9001"
    assert not any(f.run()[k] for k in ("findings", "red_on_master", "undetermined"))
    assert sp.run("L-spec-unknown", root=f.root)["undetermined"]
    assert before == {p.name: p.read_bytes() for p in (f.root / "events").iterdir()}
    f.repo.rename(f.repo.with_name("gone"))
    assert f.run()["undetermined"]


def AC7():
    events = [dict(type=t, subject=s) for t, s in (("spec-written", "A"), ("spec-written", "B"),
              ("spec-preflight", "A"), ("spec-written", "A"), ("spec-written", "C"), ("spec-preflight", "C"))]
    assert sp.pending(events) == ["B", "A"]
    assert sp.pending([]) == []
    assert sp.pending([dict(type="spec-preflight", subject="X")]) == []
    f = Fixture()
    for i in range(10):
        f.spec(sid=f"L-spec-{9100+i}")
    f.event("build-started", "L-spec-9100")
    for i, kind in enumerate(("shipped", "spec-closed", "spec-killed"), 1):
        f.event(kind, f"L-spec-{9100+i}")
    # Test eligibility directly as well as a tick with the append boundary observed.
    old = dict(type="spec-written", subject="old", ts=(NOW-timedelta(days=4)).isoformat())
    assert not sp._eligible("old", [old], NOW)
    bodies = []
    with mock.patch.object(sp, "_append", side_effect=lambda root, body: bodies.append(body)):
        assert sp.tick(root=f.root, now=NOW) == 0
    assert [b["spec"] for b in bodies] == [f"L-spec-{9100+i}" for i in range(4, 8)], bodies


def AC8():
    f = Fixture()
    f.spec()
    env = {**os.environ, "DOIT_ROOT": str(f.root), "DOIT_PROJECT": "", "DOIT_READ_CACHE": "0"}
    argv = ["/usr/bin/python3", str(SOURCE / "spec_preflight.py"), "tick"]
    proc(argv, env=env)
    stored = f.root / "events/L-spec-preflight-local.jsonl"
    original = stored.read_bytes()
    line = json.loads(original)
    assert "actor" not in line and line["subject"] == "L-spec-9001"
    read = ["/usr/bin/python3", "-c", "import json, fold; print(json.dumps(fold.fold(fold.read_events())[3]['L-spec-9001']))"]
    events = json.loads(proc(read, env={**env, "PYTHONPATH": str(SOURCE)}))
    event = next(e for e in events if e["type"] == "spec-preflight")
    assert event["actor"] == "spec-preflight"
    assert all(k in event for k in ("spec", "findings", "red_on_master", "undetermined"))
    proc(argv, env=env)
    assert original == stored.read_bytes()
    (f.root / "events/L-builder-9999.jsonl").write_bytes(original)
    ignored = json.loads(proc(["/usr/bin/python3", "-c", "import json, fold; print(json.dumps(fold.fold(fold.read_events())[2]))"],
                             env={**env, "PYTHONPATH": str(SOURCE)}))
    assert any(e["type"] == "spec-preflight" and e["actor"] == "builder" for e in ignored)

    other = Fixture("albert-scott")
    other.spec()
    cross_env = {**env, 'DOIT_ROOT': str(other.root), 'DOIT_PROJECT': 'do-it-v2'}
    proc(argv, env=cross_env)
    cross_events = json.loads(proc(read, env={**cross_env, 'DOIT_PROJECT': 'albert-scott', 'PYTHONPATH': str(SOURCE)}))
    assert any(e['type'] == 'spec-preflight' and e['project'] == 'albert-scott' for e in cross_events)


def AC10():
    rows = tomllib.loads((SOURCE.parent / "crons.toml").read_text())["row"]
    selected = [r for r in rows if r["name"] == "spec-preflight"]
    assert len(selected) == 1
    row = selected[0]
    for k, value in dict(where="user", schedule="*/5 * * * *", path="/usr/bin/python3",
                         max_runtime_s=3300, owner="thinker", project="do-it-v2").items():
        assert row[k] == value
    assert "cron-run spec-preflight --" in row["command"]
    assert "src/spec_preflight.py tick" in row["command"]
    assert ">> {root}/logs/spec-preflight.log 2>&1" in row["command"]
    assert [r["name"] for r in rows if re.search(row["sig"], r["command"])] == ["spec-preflight"]
    assert fold.EMITS["spec-preflight"] == {"spec-preflight"}


def AC11():
    import ast
    source = (SOURCE / "spec_preflight.py").read_text()
    assert not re.search("claude|anthropic|paid_call|api_key", source, re.I)
    tree = ast.parse(source)
    # The sole process creation point enforces list argv, stdin, timeout, own group.
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert {n.func.attr for n in calls} == {"Popen", "CompletedProcess"}
    assert "start_new_session=True" in source and "stdin=subprocess.DEVNULL" in source
    assert sp.TEST_CAP_S == 600 and sp.TICK_MAX == 4


GROUPS = {name: value for name, value in list(globals().items()) if re.fullmatch(r"AC\d+", name)}
if __name__ == "__main__":
    selected = sys.argv[1:] or list(GROUPS)
    if any(name not in GROUPS for name in selected):
        sys.exit(2)
    for name in selected:
        GROUPS[name]()
        print(f"L-spec-1663 {name} ok", flush=True)
