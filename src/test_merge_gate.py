#!/usr/bin/env python3
"""merge-gate checks. Every one runs against a real git repo, because the whole
scar is that a diff LOOKED clean — nothing here can be proven with a mock."""
import json, os, pathlib, subprocess, sys, tempfile

TMP = tempfile.mkdtemp()
os.environ["DOIT_ROOT"] = str(pathlib.Path(TMP) / "ledger")
os.environ["DOIT_GATE_LEDGER_FILE"] = "L-executor-test.jsonl"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harness  # noqa: E402 -- L-spec-0430: a real, disposable DOIT_SCRATCH for secret_scan's
                 # own scratch.sub("gate") calls, never the real $HOME/doit-scratch
os.environ["DOIT_SCRATCH"] = str(harness.test_root("gate-scratch"))
import merge_gate as mg

# The direct-import contract also has a process-isolated selector. Reject typos
# before running any fixture; an empty selection must never report success.
if len(sys.argv) > 1 and sys.argv[1:] != ["--spec-1868-ac1"]:
    sys.exit("usage: test_merge_gate.py [--spec-1868-ac1]")


def _spec_response(ac, body, success=True):
    import hashlib
    raw = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(url="cli://L-spec-1868/" + ac, status=0 if success else 1,
                         body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))


def spec1868_ac1():
    """Real Git, scanner processes and kernel locks; no lock mocks."""
    import fcntl
    import shlex
    import signal
    import time
    import unittest
    from unittest.mock import patch
    observations = []

    class Ownership(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(prefix="scan-ownership-")
            self.addCleanup(self.temp.cleanup)
            self.root = pathlib.Path(self.temp.name)
            self.repo = self.root / "repo"
            self.repo.mkdir()
            self.processes = []
            self.addCleanup(self.stop)
            self.git("init", "-q", "-b", "main")
            self.git("config", "user.name", "fixture")
            self.git("config", "user.email", "fixture@localhost")
            self.release = self.root / "release"
            code = ("import os,pathlib,sys,time,json; "
                    f"root=pathlib.Path({str(self.root)!r}); "
                    "script=pathlib.Path(sys.argv[1]); "
                    "p=root/('scan-'+script.parent.name+'.json'); "
                    "row=dict(start=time.monotonic(),cwd=os.getcwd(),script=str(script),pid=os.getpid()); "
                    "q=p.with_suffix('.pending'); q.write_text(json.dumps(row)); q.replace(p); "
                    "exec(\"while not (root/'release').exists(): time.sleep(.01)\"); "
                    "row['end']=time.monotonic(); q.write_text(json.dumps(row)); q.replace(p)")
            ci = self.repo / "scripts" / "ci"
            ci.mkdir(parents=True)
            (ci / "detect-secrets-changed.sh").write_text(
                "#!/bin/bash\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + ' "$0"\n')
            self.git("add", "."); self.git("commit", "-qm", "scanner")
            self.sha = self.git("rev-parse", "HEAD")
            self.module = str(pathlib.Path(mg.__file__).parent)
            self.lock = self.repo / ".git" / "doit-secret-scan-locks" / (self.sha + ".lock")

        def git(self, *args, repo=None):
            return subprocess.check_output(["git", "-C", str(repo or self.repo), *args],
                                           stderr=subprocess.DEVNULL).strip().decode()

        def stop(self):
            self.release.touch()
            for proc in self.processes:
                if proc.poll() is None:
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.communicate()

        def start(self, repo=None, sha=None, top=None):
            n = str(len(self.processes))
            env = dict(os.environ, DOIT_ROOT=str(self.root / (n + "-ledger")),
                       DOIT_LEDGER_FILE="L-executor-fixture.jsonl",
                       DOIT_SCRATCH=str(self.root / (n + "-scratch")))
            env.pop("DOIT_GATE_DEADLINE", None)
            code = ("import sys,json; sys.path.insert(0,sys.argv[1]); import merge_gate as m; "
                    "m._TOP=sys.argv[5]; result=m.secret_scan(*sys.argv[2:5]); "
                    "assert m._TOP==sys.argv[5]; print(json.dumps(result))")
            proc = subprocess.Popen([sys.executable, "-B", "-c", code, self.module,
                    str(repo or self.repo), self.sha, sha or self.sha, str(top or "prior-context")],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.processes.append(proc)
            return proc

        def rows(self):
            return [json.loads(p.read_text()) for p in self.root.glob("scan-*.json")]

        def entered(self, count):
            until = time.monotonic() + 10
            while len(self.rows()) < count and time.monotonic() < until:
                time.sleep(.01)
            self.assertEqual(len(self.rows()), count, "scanner failed to enter")

        def finish(self, proc, expected="clean"):
            output, error = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, error.decode())
            result = json.loads(output)
            self.assertEqual(result[0], expected, result)
            return result

        def assert_serial(self):
            rows = sorted(self.rows(), key=lambda r: r["start"])
            self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(rows, rows[1:])))
            self.assertEqual(len({r["cwd"] for r in rows}), len(rows))
            self.assertEqual(len({r["script"] for r in rows}), len(rows))
            for row in rows:
                self.assertFalse(pathlib.Path(row["cwd"]).exists())
                self.assertFalse(pathlib.Path(row["script"]).exists())
            observations.append(dict(case=self._testMethodName, intervals=rows))

        def test_same_key_alias_and_linked_worktree(self):
            owner = self.start(); self.entered(1)
            alias = self.root / "alias"; alias.symlink_to(self.repo, target_is_directory=True)
            linked = self.root / "linked"
            self.git("worktree", "add", "--detach", str(linked), self.sha)
            for spelling in (self.repo, alias, linked):
                loser = self.start(repo=spelling)
                self.assertEqual(self.finish(loser, "undetermined")[1][0], "lock-busy")
                self.assertFalse((self.root / (str(len(self.processes)-1) + "-scratch")).exists())
            self.assertEqual(len(self.rows()), 1)
            self.assertTrue(pathlib.Path(self.rows()[0]["cwd"]).exists())
            self.release.touch(); self.finish(owner)
            self.git("worktree", "remove", str(linked))
            self.assert_serial()

        def test_distinct_keys_progress(self):
            owner = self.start(); self.entered(1)
            (self.repo / "added").write_text("second commit")
            self.git("add", "."); self.git("commit", "-qm", "second")
            second = self.start(sha=self.git("rev-parse", "HEAD")); self.entered(2)
            clone = self.root / "clone"
            subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True)
            third = self.start(repo=clone); self.entered(3)
            self.release.touch()
            for proc in (owner, second, third): self.finish(proc)
            self.assertTrue(all(r["end"] >= max(x["start"] for x in self.rows()) for r in self.rows()))
            observations.append(dict(case=self._testMethodName, concurrent_owners=3))

        def test_stale_file_completion_and_unique_reacquisition(self):
            self.lock.parent.mkdir(); self.lock.touch()
            inode = self.lock.stat().st_ino
            self.release.touch()
            for count in (1, 2):
                self.finish(self.start())
                self.assertEqual(len(self.rows()), count)
                self.assertEqual(self.lock.stat().st_ino, inode)
            self.assert_serial()

        def test_release_race_open_descriptor_and_newcomer(self):
            owner = self.start(); self.entered(1)
            inode = self.lock.stat().st_ino
            code = ("import fcntl,json,sys,time; f=open(sys.argv[1],'a+b'); print('opened',flush=True); "
                    "sys.stdin.readline(); "
                    "exec(\"while True:\\n try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); break\\n except BlockingIOError: time.sleep(.001)\"); "
                    "print(json.dumps(dict(start=time.monotonic())),flush=True); "
                    "time.sleep(.1); print(json.dumps(dict(end=time.monotonic())),flush=True)")
            contender = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.lock)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(contender.stdout.readline().strip(), "opened")
            # A real same-key scan loses before release; its retry races the
            # already-open contender and a new scan across owner completion.
            self.assertEqual(self.finish(self.start(), "undetermined")[1][0], "lock-busy")
            contender.stdin.write("go\n"); contender.stdin.flush()
            self.release.touch()
            newcomer = self.start()
            self.finish(owner)
            output, err = newcomer.communicate(timeout=5)
            self.assertEqual(newcomer.returncode, 0, err.decode())
            self.assertIn(json.loads(output)[0], ("clean", "undetermined"))
            rest, err = contender.communicate(timeout=5)
            self.assertEqual(contender.returncode, 0, err)
            interval = [json.loads(line) for line in rest.splitlines()]
            self.assertEqual(len(interval), 2)
            self.finish(self.start())
            self.assertEqual(self.lock.stat().st_ino, inode)
            for row in self.rows():
                self.assertTrue(row["end"] <= interval[0]["start"] or interval[1]["end"] <= row["start"])
            self.assert_serial()

        def test_sigkill_releases_lock(self):
            owner = self.start(); self.entered(1)
            old = self.rows()[0]
            inode = self.lock.stat().st_ino
            owner.kill(); owner.communicate()
            self.release.touch()  # Let the orphaned scanner finish; no claim of finally after SIGKILL.
            self.finish(self.start())
            self.assertEqual(len(self.rows()), 2)
            self.assertEqual(self.lock.stat().st_ino, inode)
            new = next(r for r in self.rows() if r["cwd"] != old["cwd"])
            self.assertNotEqual(new["script"], old["script"])
            observations.append(dict(case=self._testMethodName, unique_reacquisition=True, stable_inode=True))

        def test_repository_argument_controls_all_operations(self):
            other = self.root / "other"
            subprocess.run(["git", "clone", "-q", str(self.repo), str(other)], check=True)
            self.git("config", "user.name", "fixture", repo=other)
            self.git("config", "user.email", "fixture@localhost", repo=other)
            (other / "scripts/ci/detect-secrets-changed.sh").write_text("exit 98\n")
            (other / "unrelated-path").write_text("other-only")
            self.git("add", ".", repo=other); self.git("commit", "-qm", "unrelated", repo=other)
            self.git("checkout", "-qb", "work")
            (self.repo / "repo-only-path").touch()
            self.git("add", "."); self.git("commit", "-qm", "repo-only branch")
            before = self.git("worktree", "list", "--porcelain", repo=other)
            original_git = mg.git
            trace = []
            def recording_git(*args, **kw):
                out = original_git(*args, **kw)
                trace.append((args, mg._TOP, out if args[0] == "diff" else None))
                return out
            self.release.touch()
            with patch.object(mg, "_TOP", str(other)), patch.object(mg, "_RUN_ACTIVE", False), \
                 patch.dict(os.environ, DOIT_SCRATCH=str(self.root / "direct-scratch")), \
                 patch.object(mg, "git", side_effect=recording_git):
                self.assertEqual(mg.secret_scan(self.repo, "main", "work"), ("clean", []))
                self.assertEqual(mg._TOP, str(other))
            self.assertTrue(all(top == str(self.repo) for _, top, _ in trace))
            self.assertEqual(next(data for args, _, data in trace if args[0] == "diff"), b"repo-only-path\0")
            self.assertEqual(self.git("worktree", "list", "--porcelain", repo=other), before)
            self.assertFalse((other / ".git/doit-secret-scan-locks").exists())
            self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree "))-1, 1)
            self.assert_serial()

        def test_real_scanner_timeout_reaps_and_releases(self):
            pids, paths = [], []
            real_popen = subprocess.Popen
            def popen(argv, **kw):
                proc = real_popen(argv, **kw)
                if argv[0] == "bash":
                    pids.append(proc.pid)
                    paths.extend((pathlib.Path(argv[1]), pathlib.Path(kw["cwd"])))
                return proc
            with patch.object(mg, "_RUN_ACTIVE", False), \
                 patch.dict(os.environ, DOIT_SCRATCH=str(self.root / "direct-scratch")), \
                 patch.object(mg, "scan_deadline", return_value=2.0), \
                 patch.object(mg.subprocess, "Popen", side_effect=popen):
                result = mg.secret_scan(self.repo, self.sha, self.sha)
            self.assertEqual(result[0], "undetermined")
            self.assertEqual(result[1][0], "timeout")
            self.assertEqual(len(pids), 2)
            self.assertTrue(all(not pathlib.Path("/proc", str(pid)).exists() for pid in pids))
            self.assertTrue(all(not path.exists() for path in paths))
            with self.lock.open("a+b") as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observations.append(dict(case=self._testMethodName, killed_reaped=2, cleaned=True))

        def test_spawn_and_partial_add_failure_cleanup(self):
            real_git = mg.git
            for failure in ("spawn", "add"):
                added = []
                def git(*args, **kw):
                    if args[:2] == ("worktree", "add"):
                        added.append(pathlib.Path(args[3]))
                        if failure == "add":
                            added[-1].mkdir(); (added[-1] / "partial").touch()
                            raise mg.Undetermined("fixture partial worktree add failure")
                    return real_git(*args, **kw)
                real_popen = subprocess.Popen
                def popen(argv, **kw):
                    if argv[0] == "bash": raise OSError("fixture spawn failure")
                    return real_popen(argv, **kw)
                with patch.object(mg, "_RUN_ACTIVE", False), \
                     patch.dict(os.environ, DOIT_SCRATCH=str(self.root / "direct-scratch")), \
                     patch.object(mg, "git", side_effect=git), \
                     patch.object(mg.subprocess, "Popen", side_effect=popen):
                    result = mg.secret_scan(self.repo, self.sha, self.sha)
                self.assertEqual(result[0], "undetermined")
                self.assertEqual(result[1][0], "worktree" if failure == "add" else "tool")
                self.assertEqual(len(added), 1)
                self.assertFalse(added[0].exists())
                self.assertFalse(added[0].parent.exists())
                self.assertTrue(self.lock.exists())
                with self.lock.open("a+b") as descriptor:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observations.append(dict(case=self._testMethodName, cleaned_failures=["spawn", "partial-add"]))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Ownership)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    _spec_response("AC1", dict(executed=result.testsRun, observations=observations), result.wasSuccessful())
    print(f"L-spec-1868 AC1: {result.testsRun} executed, {len(result.failures) + len(result.errors)} failures")
    return result.testsRun > 0 and result.wasSuccessful()


def spec1868_ac2():
    """Controlled scanner clocks with real Git setup and ledger persistence."""
    import contextlib
    import unittest
    from unittest.mock import patch
    observations = []

    class Budget(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(prefix="scan-budget-")
            self.addCleanup(self.temp.cleanup)
            self.root = pathlib.Path(self.temp.name)
            self.repo = self.root / "repo"
            self.repo.mkdir()
            self.git("init", "-q", "-b", "main")
            self.git("config", "user.name", "fixture")
            self.git("config", "user.email", "fixture@localhost")
            ci = self.repo / "scripts" / "ci"; ci.mkdir(parents=True)
            (ci / "detect-secrets-changed.sh").write_text("#!/bin/bash\nexit 0\n")
            (self.repo / "modified").write_text("base")
            (self.repo / "deleted").write_text("base")
            self.git("add", "."); self.git("commit", "-qm", "scanner")
            self.main = self.git("rev-parse", "HEAD")
            self.git("checkout", "-qb", "work")
            self.clock, self.calls, self.git_calls, self.killed, self.reaped = [1000.0], [], [], [], []
            self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
            self.stack.enter_context(patch.object(mg, "_TOP", "prior-context"))
            self.stack.enter_context(patch.object(mg, "_START", -2000.0))
            self.stack.enter_context(patch.object(mg, "_RUN_ACTIVE", False))
            self.stack.enter_context(patch.object(mg, "DEADLINE", 60.0))
            self.stack.enter_context(patch.object(mg, "DETECT_SECRETS_TIMEOUT", 45.0))
            self.stack.enter_context(patch.dict(os.environ, DOIT_SCRATCH=str(self.root / "scratch")))
            # No ambient override may silently change the default fixtures.
            self.stack.enter_context(patch.dict(os.environ))
            os.environ.pop("DOIT_GATE_DEADLINE", None)
            self.stack.enter_context(patch.object(mg.time, "monotonic", side_effect=lambda: self.clock[0]))
            self.real_git = mg.git
            self.real_popen = subprocess.Popen
            self.real_run = subprocess.run
            self.stack.enter_context(patch.object(mg.subprocess, "Popen", side_effect=self.popen))
            self.stack.enter_context(patch.object(mg.subprocess, "run", side_effect=self.git_run))
            self.stack.enter_context(patch.object(mg.os, "killpg", side_effect=lambda pid, sig: self.killed.append((pid, sig))))
            self.plan = []
            self.setup_spend = 0
            self.branch = self.main

        def git(self, *args):
            return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                           stderr=subprocess.DEVNULL).strip().decode()

        def changed(self, count):
            # Once per fixture, except the lifecycle test which supplies two
            # enumerations while retaining real Git identity/worktree setup.
            for n in range(count): (self.repo / ("path-" + str(n))).touch()
            self.git("add", "."); self.git("commit", "-qm", "branch", "--allow-empty")
            self.branch = self.git("rev-parse", "HEAD")

        def git_run(self, argv, **kw):
            self.git_calls.append(dict(argv=list(argv), timeout=kw.get("timeout"),
                                       start=mg._START, deadline=mg.DEADLINE, now=self.clock[0]))
            result = self.real_run(argv, **kw)
            if "add" in argv and "worktree" in argv: self.clock[0] += self.setup_spend
            return result

        def popen(self, argv, **kw):
            if argv[0] != "bash": return self.real_popen(argv, **kw)
            case = self
            class Proc:
                pid = 1234567
                def __init__(self):
                    case.assertTrue(kw["start_new_session"])
                    case.assertTrue(pathlib.Path(kw["cwd"]).is_dir())
                    self.duration, self.returncode, self.stdout, self.stderr = case.plan.pop(0)
                    self.call = dict(start=mg._START, deadline=mg.DEADLINE, script=argv[1],
                                     cwd=kw["cwd"], effective=mg.DETECT_SECRETS_TIMEOUT)
                    case.calls.append(self.call)
                def communicate(self, timeout=None):
                    if timeout is None:
                        case.reaped.append(self.pid)
                        return b"", b""
                    self.call["timeout"] = timeout
                    if self.duration is None or self.duration > timeout:
                        case.clock[0] += timeout
                        raise subprocess.TimeoutExpired(argv, timeout)
                    case.clock[0] += self.duration
                    return self.stdout, self.stderr
            return Proc()

        def scan(self):
            return mg.secret_scan(self.repo, self.main, self.branch)

        def assert_restored(self):
            self.assertEqual((mg._TOP, mg._START, mg.DEADLINE, mg.DETECT_SECRETS_TIMEOUT),
                             ("prior-context", -2000.0, 60.0, 45.0))
            self.assertFalse(mg._RUN_ACTIVE)
            for call in self.calls:
                self.assertFalse(pathlib.Path(call["cwd"]).exists())
                self.assertFalse(pathlib.Path(call["script"]).exists())

        def test_budget_table_and_domain(self):
            table = [(0,60),(1,60),(10,60),(34,132),(40,150),(100,330),(190,600),(1000,600)]
            for count, expected in table: self.assertEqual(mg.scan_deadline(count), expected)
            for invalid in (-1, True, False, 1.5, "34", None):
                with self.assertRaises(ValueError): mg.scan_deadline(invalid)
            observations.append(dict(case=self._testMethodName, table=table, invalid_counts=6))

        def test_divergent_history_and_byte_enumeration(self):
            self.git("checkout", "-q", "main")
            (self.repo / "main-only").touch()
            self.git("add", "."); self.git("commit", "-qm", "main advance")
            self.main = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", "work")
            (self.repo / "modified").write_text("branch")
            (self.repo / "deleted").unlink()
            (self.repo / "newline\npath").touch()
            self.git("add", "."); self.git("commit", "-qm", "branch divergence")
            self.branch = self.git("rev-parse", "HEAD")
            enumerations = []
            def capture(*args, **kw):
                output = self.real_git(*args, **kw)
                if args[0] == "diff":
                    self.assertEqual(args, ("diff", "--diff-filter=ACMRT", "--name-only", "-z", self.main + "..." + self.branch))
                    enumerations.append(set(output.split(b"\0")) - {b""})
                return output
            with patch.object(mg, "git", side_effect=capture):
                self.plan = [(0,0,b"",b"")]
                self.assertEqual(self.scan(), ("clean", []))
            self.assertEqual(enumerations, [{b"modified", b"newline\npath"}])
            # The undecodable path exists on disk and is enumerated as bytes.
            fd = os.open(os.fsencode(self.repo) + b"/byte-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            self.git("add", "."); self.git("commit", "-qm", "raw filename")
            self.branch = self.git("rev-parse", "HEAD")
            self.plan = [(0,0,b"",b"")]
            with patch.object(mg, "git", side_effect=capture): self.assertEqual(self.scan()[0], "clean")
            self.assertIn(b"byte-\xff", enumerations[-1])
            self.assert_restored()
            observations.append(dict(case=self._testMethodName, divergent_count=2, raw_byte_count=3))

        def test_duplicate_empty_and_failed_enumeration(self):
            for output in (b"a\0a\0\0b\0", b"incomplete", None, mg.Undetermined("fixture git nonzero")):
                def injected(*args, **kw):
                    if args[0] == "diff":
                        if isinstance(output, Exception): raise output
                        return output
                    return self.real_git(*args, **kw)
                self.plan = [(0,0,b"",b"")]
                with patch.object(mg, "git", side_effect=injected): result = self.scan()
                if output == b"a\0a\0\0b\0":
                    self.assertEqual(result, ("clean", []))
                    self.assertEqual(self.calls[-1]["deadline"], 120)
                else:
                    self.assertEqual(result[0:1], ("undetermined",))
                    self.assertEqual(result[1][0], "tool")
                self.assert_restored()
            observations.append(dict(case=self._testMethodName, unique_count=2, closed_failures=3))

        def test_default_34_allows_61_seconds(self):
            self.changed(34); self.plan = [(61,0,b"",b"")]
            self.assertEqual(self.scan(), ("clean", []))
            self.assertEqual((self.calls[0]["timeout"], self.calls[0]["deadline"]), (66,192))
            self.assertNotIn("DOIT_GATE_DEADLINE", mg._KNOBS)
            self.assertEqual(mg.DEFAULTS["DOIT_GATE_DEADLINE"], "auto")
            self.assert_restored()
            observations.append(dict(case=self._testMethodName, scanner_elapsed=61, pass_timeout=66, gate_deadline=192))

        def test_explicit_overrides_and_validation(self):
            self.changed(34)
            for value, duration, verdict, reason, timeout in (("60",61,"undetermined","deadline",60),
                    ("1000",67,"undetermined","timeout",66)):
                with patch.dict(os.environ, DOIT_GATE_DEADLINE=value):
                    self.plan = [(duration,0,b"",b""), (None,0,b"",b"")]
                    offset = len(self.calls)
                    result = self.scan()
                    self.assertEqual(result[0], verdict)
                    self.assertEqual(result[1][0], reason)
                    self.assertEqual(self.calls[offset]["timeout"], timeout)
                    self.assertEqual(mg._KNOBS["DOIT_GATE_DEADLINE"], value)
                    self.assert_restored()
            for value in ("auto", "bad", "nan", "inf", "-inf", "0", ".9"):
                with patch.dict(os.environ, DOIT_GATE_DEADLINE=value):
                    self.assertEqual(self.scan()[0], "undetermined")
                    with self.assertRaises(mg.Undetermined): mg.load_config()
                mg.DEADLINE = 60.0
            observations.append(dict(case=self._testMethodName, overrides=[60,1000], invalid=7,
                                     killed=len(self.killed), reaped=len(self.reaped)))

        def test_retry_findings_success_and_exhaustion(self):
            self.changed(34)
            for last, verdict in (((1,0,b"",b""),"clean"),
                    ((1,1,b"  example.py:1 -- Secret Keyword\n",b""),"rework"),
                    ((None,0,b"",b""),"undetermined")):
                self.plan = [(None,0,b"",b""), last]
                offset = len(self.calls)
                result = self.scan()
                self.assertEqual(result[0], verdict)
                self.assertEqual(len(self.calls)-offset, 2)
                self.assertEqual([c["timeout"] for c in self.calls[offset:]], [66,66])
                if verdict == "undetermined": self.assertEqual(result[1][0], "timeout")
                self.assert_restored()
            self.assertEqual(len(self.killed), 4)
            self.assertEqual(len(self.reaped), 4)
            observations.append(dict(case=self._testMethodName, outcomes=["clean","rework","timeout"], passes=2))

        def test_implicit_lifecycle_does_not_leak_large_budget(self):
            enumerations = iter([b"".join(str(n).encode()+b"\0" for n in range(1000)), b"one\0"])
            def git(*args, **kw):
                if args[0] == "diff": return next(enumerations)
                return self.real_git(*args, **kw)
            with patch.object(mg, "git", side_effect=git), \
                 patch.dict(os.environ, DOIT_GIT_TIMEOUT="1000"):
                self.plan = [(1,0,b"",b"")]
                self.assertEqual(self.scan(), ("clean", []))
                self.assertEqual((self.calls[0]["timeout"],self.calls[0]["deadline"]), (300,660))
                self.assert_restored()
                self.clock[0] += 700
                second_start = self.clock[0]
                self.plan = [(None,0,b"",b""), (None,0,b"",b"")]
                offset = len(self.git_calls)
                self.assertEqual(self.scan()[1][0], "timeout")
                self.assertEqual([c["timeout"] for c in self.calls[1:]], [30,30])
                self.assertEqual([c["deadline"] for c in self.calls[1:]], [120,120])
                self.assertEqual(self.clock[0]-second_start, 60)
                setup_calls = [c for c in self.git_calls[offset:] if "remove" not in c["argv"]]
                self.assertTrue(all(c["timeout"] == 60 for c in setup_calls))
                self.assertTrue(all(c["start"] == second_start for c in setup_calls))
                self.assert_restored()
            observations.append(dict(case=self._testMethodName, first_budget=600, second_budget=60,
                                     second_passes=[30,30], second_gate=120, origin_gap=701))

        def test_explicit_setup_is_preserved_and_cannot_borrow(self):
            self.changed(34)
            mg.begin_run(self.repo); start = mg._START
            self.clock[0] += 20
            self.plan = [(61,0,b"",b"")]
            self.assertEqual(self.scan(), ("clean", []))
            self.assertEqual((mg._START, self.calls[-1]["start"]), (start,start))
            self.assertFalse(mg._RUN_ACTIVE)
            mg.begin_run(self.repo); self.clock[0] += 61
            offset = len(self.calls)
            self.assertEqual(self.scan()[1][0], "deadline")
            self.assertEqual(len(self.calls), offset)
            self.assertFalse(mg._RUN_ACTIVE)
            # A direct call whose worktree setup consumes 61s cannot borrow S.
            mg._RUN_ACTIVE = False
            self.setup_spend = 61
            self.assertEqual(self.scan()[1][0], "deadline")
            self.assertEqual(len(self.calls), offset)
            observations.append(dict(case=self._testMethodName, explicit_setup=20, expired_setup=61,
                                     setup_cannot_borrow=True))

        def test_main_caller_matrix_local_ledger(self):
            # Keep production append/admission/read behavior; only the scan
            # verdict and unrelated guard are supplied by the fixture.
            self.stack.enter_context(patch.object(mg.freeze, "state", return_value=None))
            self.stack.enter_context(patch.object(mg, "_TOP", str(self.repo)))
            ledger = mg.fold.EVENTS / os.environ["DOIT_GATE_LEDGER_FILE"]
            def rows():
                return [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
            for reason in ("lock-busy", "deadline", "timeout", "worktree", "tool"):
                for metadata in ([], ["infra:capacity"]):
                    before = len(rows())
                    with patch.object(mg, "gate", return_value=([],[],self.main,self.main,self.main)), \
                         patch.object(mg, "secret_scan", return_value=("undetermined", [reason]+metadata)) as scan:
                        self.assertEqual(mg.main_(["work", "main"]), 2)
                        self.assertEqual(scan.call_count, 1)
                    self.assertEqual(len(rows()), before)
                    self.assertFalse(mg._RUN_ACTIVE)
            for verdict, findings, rc, event in (("clean",[],0,"merge-gate-clean"),
                    ("rework",["  example.py:1 -- Secret Keyword"],1,"merge-gate-rework")):
                with patch.object(mg, "gate", return_value=([],[],self.main,self.main,self.main)), \
                     patch.object(mg, "secret_scan", return_value=(verdict,findings)) as scan:
                    self.assertEqual(mg.main_(["work", "main"]), rc)
                    self.assertEqual(scan.call_count, 1)
                self.assertEqual(rows()[-1]["type"], event)
            with patch.object(mg, "gate", return_value=(["outside"],[],self.main,self.main,self.main)), \
                 patch.object(mg, "secret_scan") as scan:
                self.assertEqual(mg.main_(["work", "main"]), 1)
                scan.assert_not_called()
            self.assertEqual(rows()[-1]["type"], "merge-gate-rework")
            observations.append(dict(case=self._testMethodName, holds=10, controls=3, gate_events=3))

        def test_production_records_read_by_fresh_process(self):
            self.plan = [(None,0,b"",b""), (None,0,b"",b"")]
            token = mg._SCAN_SUBJECT.set("L-spec-1868-fixture")
            try: self.assertEqual(self.scan()[1][0], "timeout")
            finally: mg._SCAN_SUBJECT.reset(token)
            code = ("import json,sys; sys.path.insert(0,sys.argv[1]); import ledger_files as l,fold; "
                    "print(json.dumps([dict(json.loads(line),fixture_filename=name,fixture_actor=fold.actor_of(name)) "
                    "for name in l.names(sys.argv[2]) for line in l.read_text(sys.argv[2],name).splitlines()]))")
            output = subprocess.check_output([sys.executable,"-B","-c",code,
                       str(pathlib.Path(mg.__file__).parent),str(mg.fold.EVENTS)])
            rows = json.loads(output)
            resource = next(r for r in reversed(rows) if r["type"] == "resource-created" and r["subject"] == "L-spec-1868-fixture")
            infra = next(r for r in reversed(rows) if r["type"] == "infra-failure" and r["subject"] == "L-spec-1868-fixture")
            self.assertEqual(resource["ref"], self.calls[0]["cwd"])
            self.assertEqual(resource["kind"], "worktree")
            self.assertEqual(infra["class"], "runner-or-network")
            self.assertEqual(infra["backend"], "merge-gate")
            self.assertEqual(infra["fixture_filename"], os.environ["DOIT_GATE_LEDGER_FILE"])
            self.assertEqual(infra["fixture_actor"], "executor")
            observations.append(dict(case=self._testMethodName, subject=infra["subject"],
                resource_path=resource["ref"], infrastructure_class=infra["class"], actor_file=infra["fixture_filename"]))

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Budget))
    _spec_response("AC2", dict(executed=result.testsRun, observations=observations), result.wasSuccessful())
    print(f"L-spec-1868 AC2: {result.testsRun} executed, {len(result.failures) + len(result.errors)} failures")
    return result.testsRun > 0 and result.wasSuccessful()


if sys.argv[1:] == ["--spec-1868-ac1"]:
    sys.exit(0 if spec1868_ac1() else 1)

GRANT, ok = ["src/*", "docs/"], [spec1868_ac1(), spec1868_ac2()]
CONTENT = pathlib.Path(os.environ["DOIT_ROOT"]) / "content"
CONTENT.mkdir(parents=True, exist_ok=True)
(CONTENT / "L-spec-0001.md").write_text("# spec\n\nwrites: src/*, docs/\n")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout


def _exit(fn):
    """True when the callable exits rather than proceeding on bad input."""
    try:
        fn(); return False
    except SystemExit:
        return True


def check(name, cond):
    ok.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + name)


def repo(prefix=None):
    """main gains `late.txt` AFTER the branch point — the exact scar shape.
    Returns (dir, recorded base_sha) — the sha v1's guard would have diffed from.
    `prefix`: AC6/AC8 (L-spec-0429) need the repo's own path to control whether
    it contains the substring `albert-scott` — the freeze refusal's scoping."""
    d = tempfile.mkdtemp(prefix=prefix) if prefix else tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    for f, c in (("keep.txt", "keep"), ("old.txt", "v1"), ("migrations/001.sql", "create")):
        p = pathlib.Path(d, f); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(c)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    base = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "branch", "work")                       # base_sha is RECORDED here
    pathlib.Path(d, "late.txt").write_text("main gained this")
    pathlib.Path(d, "old.txt").write_text("v2")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "main moves on")
    return d, base


def on(d, fn, merge=True):
    """Do the branch's work. `merge` = the branch pulled main in first, which is
    what makes the deletion a deletion — and what erases it from the range."""
    sh("git", "-C", d, "checkout", "-q", "work")
    if merge:
        sh("git", "-C", d, "merge", "-q", "--no-edit", "main")
    fn(d)
    sh("git", "-C", d, "add", "-A")
    sh("git", "-C", d, "commit", "-q", "--allow-empty", "-m", "branch work")
    os.chdir(d)


# 1 — THE SCAR. v1's range check sees nothing at all; the gate must see it.
d, base = repo()
on(d, lambda d: pathlib.Path(d, "late.txt").unlink())
check("base_sha..branch produces no row at all (why v1's guard missed it)",
      "late.txt" not in sh("git", "-C", d, "diff", "--name-status", base, "work"))
removed, reverts = mg.gate("work", "main", GRANT)[:2]
check("gate names the removal", any(r.startswith("late.txt@") for r in removed))
check("the removal carries main's revision", removed and len(removed[0].split("@")[1]) == 7)

# 1b — PRECISION. main moved, the branch simply hasn't merged it. A three-way
# merge KEEPS those paths, so naming them is the misapplication that gets a
# guard worked around. Must be clean.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "keep.txt").write_text("branch edit"), merge=False)
check("un-merged main advance is NOT a removal", mg.gate("work", "main", GRANT).removed == [])

# 2 — a removal INSIDE the writes: grant is the branch's own business.
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "src").mkdir(); pathlib.Path(d, "src/mine.py").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "granted file")
on(d, lambda d: pathlib.Path(d, "src/mine.py").unlink())
check("granted removal is clean", mg.gate("work", "main", GRANT).removed == [])

# 3 — migrations are named whatever the grant says (the excision case: the
# deletion removes the evidence along with the artifact, so parity stays GREEN).
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "migrations/001.sql").unlink())
check("migrations removal named even under a grant covering it",
      any(r.startswith("migrations/001.sql@")
          for r in mg.gate("work", "main", ["migrations/"]).removed))
check("migrations removal named with no grant",
      any(r.startswith("migrations/") for r in mg.gate("work", "main", GRANT).removed))

# 4 — a survivor that reverts to a pre-main revision.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "old.txt").write_text("v1"))
reverts = mg.gate("work", "main", GRANT).reverted
check("revert to a pre-main revision is caught", reverts == ["old.txt"])
check("a file that simply never moved is not a revert", "keep.txt" not in reverts)

# 5 — nothing to report.
d, _ = repo()
on(d, lambda d: None)
check("no removals, no reverts -> clean", mg.gate("work", "main", GRANT)[:2] == ([], []))

# 6 — could-not-determine is never clean, and it is never silent.
try:
    mg.gate("no-such-branch", "main", GRANT); check("unresolvable ref raises", False)
except mg.Undetermined:
    check("unresolvable ref raises Undetermined", True)
check("undetermined exits rework, not clean", mg.main_(["no-such-branch", "main"]) == 1)
LEDGER = pathlib.Path(os.environ["DOIT_ROOT"]) / "events" / "L-executor-test.jsonl"
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("the stop is in the ledger",
      ev[-1]["type"] == "merge-gate-rework" and "undetermined" in ev[-1])

# 7 — the caller contract: exit code, subject, and the rework payload.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "late.txt").unlink())
check("a dirty branch exits 1", mg.main_(["work", "main", "--spec", "L-spec-0001"]) == 1)
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("rework event carries removed[] and reverted[]",
      ev[-1]["type"] == "merge-gate-rework" and ev[-1]["subject"] == "L-spec-0001"
      and any(r.startswith("late.txt@") for r in ev[-1]["removed"]))
d, _ = repo(); on(d, lambda d: None)
check("a clean branch exits 0", mg.main_(["work", "main"]) == 0)
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("clean event written too", ev[-1]["type"] == "merge-gate-clean")

# ---------------------------------------------------------------- D113 cases
# Every one of these was CLEAN under the merge-base mechanism and is a real
# removal. They are the reason D110 was superseded.

def crisscross():
    """A long-lived branch merged into and kept going has SEVERAL merge-bases."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    pathlib.Path(d, "a.txt").write_text("a")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    sh("git", "-C", d, "branch", "work")
    pathlib.Path(d, "victim.txt").write_text("victim")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "M1")
    m1 = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "checkout", "-q", "work")
    pathlib.Path(d, "w.txt").write_text("w")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "W1")
    w1 = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "merge", "-q", "--no-edit", m1)
    sh("git", "-C", d, "checkout", "-q", "--detach", m1)
    sh("git", "-C", d, "merge", "-q", "--no-edit", w1)
    sh("git", "-C", d, "branch", "-f", "main", "HEAD")
    sh("git", "-C", d, "checkout", "-q", "work")
    sh("git", "-C", d, "rm", "-q", "victim.txt")
    sh("git", "-C", d, "commit", "-qm", "work deletes victim")
    os.chdir(d)
    return d

d = crisscross()
check("criss-cross history has more than one merge-base",
      len(sh("git", "-C", d, "merge-base", "--all", "main", "work").split()) > 1)
check("★ criss-cross removal is named (D110 said clean here)",
      any(r.startswith("victim.txt@") for r in mg.gate("work", "main", []).removed))

d, _ = repo()
on(d, lambda d: (pathlib.Path(d, "sod").mkdir(exist_ok=True),
                 pathlib.Path(d, "sod/סוד.txt").write_text("x")))
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "sod").mkdir(exist_ok=True); pathlib.Path(d, "sod/סוד.txt").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "main gains a hebrew path")
on(d, lambda d: pathlib.Path(d, "sod/סוד.txt").unlink())
check("★ a Hebrew filename is named, not silently dropped by C-quoting",
      any(r.startswith("sod/סוד.txt@") for r in mg.gate("work", "main", GRANT).removed))

d, _ = repo()
def swap(d):
    pathlib.Path(d, "migrations/001.sql").unlink()
    os.symlink("/dev/null", pathlib.Path(d, "migrations/001.sql"))
on(d, swap)
check("★ typechange (file replaced by a symlink) is a removal",
      any(r.startswith("migrations/001.sql@") for r in mg.gate("work", "main", []).removed))

# grant boundaries — over-granting is under-reporting
check("a grant of `src` does not cover `src_backup/keys.py`",
      not mg.granted("src_backup/keys.py", ["src"]))
check("a grant of `docs` does not cover `docs-internal/runbook.md`",
      not mg.granted("docs-internal/runbook.md", ["docs"]))
check("`*` does not cross a path separator", not mg.granted("secrets/prod_keys.py", ["*.py"]))
check("a bare `*` does not disable the gate", not mg.granted("secrets/prod.py", ["*"]))
check("`**` does cross, when you ask for it", mg.granted("src/deep/a.py", ["src/**"]))
check("a grant of a file does not cover its .orig",
      not mg.granted("src/a.py.orig", ["src/a.py"]))
check("a directory grant still works", mg.granted("docs/x.md", ["docs/"]))

# the migrations carve-out must fire where migrations actually live
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "supabase/migrations").mkdir(parents=True)
pathlib.Path(d, "supabase/migrations/001_init.sql").write_text("create")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "real migrations location")
on(d, lambda d: pathlib.Path(d, "supabase/migrations/001_init.sql").unlink())
check("★ supabase/migrations/ is named even under a grant covering it",
      any("supabase/migrations" in r for r in mg.gate("work", "main", ["supabase/**"]).removed))

# could-not-determine, in all its forms
d, _ = repo(); on(d, lambda d: None)
try:
    mg.writes_grant("L-spec-nonexistent"); check("missing spec raises", False)
except mg.Undetermined:
    check("a named spec with no content file is could-not-determine", True)
(CONTENT / "L-spec-nogrant.md").write_text("# spec with no grant line\n")
try:
    mg.writes_grant("L-spec-nogrant"); check("missing grant raises", False)
except mg.Undetermined:
    check("a spec stating no writes: grant is could-not-determine, not an empty grant", True)
check("the design's own `| **Writes** | src/* |` table row parses",
      (lambda: [(CONTENT / "L-spec-tbl.md").write_text("| **Writes** | `src/*`, docs/ |\n"),
                mg.writes_grant("L-spec-tbl")][1])() == ["src/*", "docs/"])

# ------------------------------------------------- D114: ambient state and renames
# Both judging seats, blind to each other, ranked the rename hole first.

d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "supabase/migrations").mkdir(parents=True)
pathlib.Path(d, "supabase/migrations/001_init.sql").write_text("create")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "real migrations")
def moveout(d):
    pathlib.Path(d, "src").mkdir(exist_ok=True)
    sh("git", "-C", d, "mv", "late.txt", "src/late.txt")
    sh("git", "-C", d, "mv", "supabase/migrations/001_init.sql", "src/001_init.sql")
on(d, moveout)
r = mg.gate("work", "main", ["src/**"])
check("★ a rename into the grant is still a removal of the old path",
      any(x.startswith("late.txt@") for x in r.removed))
check("★ a migration moved out of migrations/ is named despite the grant",
      any("supabase/migrations/001_init.sql" in x for x in r.removed))
check("the rename destination is reported, not hidden",
      any("-> src/late.txt" in x for x in r.removed))

# an ambiguous ref must not silently resolve to the wrong object
d, _ = repo()
sh("git", "-C", d, "tag", "main", "HEAD~1")          # a TAG named main, beside the branch
os.chdir(d)
try:
    mg.gate("work", "main", []); check("ambiguous ref raises", False)
except mg.Undetermined as e:
    check("★ a ref that is both a tag and a branch is could-not-determine",
          "ambiguous" in str(e).lower())

# a truncated history cannot answer a question about history
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
shallow = tempfile.mkdtemp() + "/s"
sh("git", "clone", "-q", "--depth", "1", "--no-local", "file://" + d, shallow)
os.chdir(shallow)
try:
    mg.gate("HEAD", "HEAD", []); check("shallow repo raises", False)
except mg.Undetermined as e:
    check("★ a shallow clone is a hard stop, not a clean", "shallow" in str(e).lower())

# ambient git config must not be able to hide a removal
d, _ = repo()
sh("git", "-C", d, "config", "diff.relative", "true")
sh("git", "-C", d, "config", "diff.ignoreSubmodules", "all")
on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
pathlib.Path(d, "web").mkdir(exist_ok=True)
os.chdir(pathlib.Path(d, "web"))
check("★ diff.relative + a subdirectory cwd cannot hide a removal",
      any(x.startswith("late.txt@") for x in mg.gate("work", "main", []).removed))
os.chdir(d)

# the deadline is per run, not per process
d, _ = repo(); on(d, lambda dd: None)
mg.DEADLINE = 3600
import time as _t
mg.gate("work", "main", [])
mg._START = _t.monotonic() - 10_000          # as if a previous run had burned the clock
mg.DEADLINE = 60
check("★ the deadline clock restarts each run", mg.gate("work", "main", [])[:2] == ([], []))

# grants that are not grants
for bad in ("*", "**", "**/*"):
    (CONTENT / "L-spec-off.md").write_text(f"writes: {bad}\n")
    try:
        mg.writes_grant("L-spec-off"); check(f"grant {bad!r} rejected", False)
    except mg.Undetermined as e:
        check(f"★ a grant of {bad!r} is refused as an off switch", "off switch" in str(e))

# prose must not become a grant
(CONTENT / "L-spec-prose.md").write_text(
    "Writes are limited to the merge gate script.\n\n| **Writes** | `src/*` |\n")
check("★ an English sentence starting with 'Writes' is not parsed as a grant",
      mg.writes_grant("L-spec-prose") == ["src/*"])
(CONTENT / "L-spec-empty.md").write_text("| writes | |\n")
try:
    mg.writes_grant("L-spec-empty"); check("empty grant rejected", False)
except mg.Undetermined:
    check("a writes: line with no paths on it is could-not-determine", True)

# argument handling: a typo is not a verdict
check("a stray argument is refused", _exit(lambda: mg.parse(["main", "src/*"])))
check("--writes with no value is refused", _exit(lambda: mg.parse(["main", "--writes"])))
check("an unknown flag is refused", _exit(lambda: mg.parse(["main", "-x"])))
check("★ a flag where the branch belongs is refused before any event (`doit gate --help`)", _exit(lambda: mg.main_(["--help"])))

# ------------------------------------------------- D115: round 3's findings
# ★ The lesson of --writes: three arg checks all asserted FAILURE cases, so a
# flag that never worked at all passed its own suite. Positive cases first.
check("★ --writes actually works (it exited unconditionally for two rounds)",
      mg.parse(["main", "--writes", "src/*"]) == ("main", None, ["src/*"]))
check("--writes takes several values",
      mg.parse(["main", "--writes", "src/*", "docs/"])[2] == ["src/*", "docs/"])
check("--spec and --writes compose",
      mg.parse(["main", "--writes", "src/*", "--spec", "L-spec-0001"])[:2] == ("main", "L-spec-0001"))
check("a stray argument is still refused", _exit(lambda: mg.parse(["main", "src/*"])))
check("--writes with no value is still refused", _exit(lambda: mg.parse(["main", "--writes"])))

# ★ the second real merge: the spec-writer put a parenthetical on the Writes: line and the
# paths in a fenced block under it. A list under an empty line is a grant; a sentence is not.
(CONTENT / "L-spec-block.md").write_text(
    "**Writes:** (the merge gate's grant — the slot's footprint, verbatim)\n\n```\na/b.py\nc/\n```\n\n## 5. Next\n")
check("a parenthetical-only Writes: line takes the fenced list under it",
      mg.writes_grant("L-spec-block") == ["a/b.py", "c/"])
(CONTENT / "L-spec-bullets.md").write_text("Writes: (see below)\n- `a/b.py`\n- c/\n\nnot a path\n")
check("bullets under it too, and the list ends at the blank line",
      mg.writes_grant("L-spec-bullets") == ["a/b.py", "c/"])
(CONTENT / "L-spec-parenprose.md").write_text("Writes: (the grant)\n\nthe web dashboard only\n")
try:
    mg.writes_grant("L-spec-parenprose"); check("prose under a parenthetical is still refused", False)
except mg.Undetermined:
    check("prose under a parenthetical is still refused", True)

# ★ the first real merge: the spec template has no Writes: line, --writes was given anyway
(CONTENT / "L-spec-nw.md").write_text("# a spec with no grant line\n")
check("★ --writes supplies the grant when the spec file states none", mg.grant_for("L-spec-nw", ["calc.py"]) == ["calc.py"])
try:
    mg.grant_for("L-spec-nw", []); check("no grant line and no --writes is could-not-determine", False)
except mg.Undetermined as e:
    check("no grant line and no --writes is could-not-determine", "states no writes" in str(e))
try:
    mg.grant_for("L-spec-none", ["calc.py"]); check("a missing content file stays could-not-determine under --writes", False)
except mg.Undetermined as e:
    check("a missing content file stays could-not-determine under --writes", "no content file" in str(e))

# prose after the colon must not become a grant
for prose, why in ((("writes: the web dashboard only"), "'web' became a directory grant"),
                   (("Writes: see the table below"), "'table' became a directory grant")):
    (CONTENT / "L-spec-p.md").write_text(prose + "\n")
    try:
        g = mg.writes_grant("L-spec-p"); check(f"★ prose refused ({why})", False)
    except mg.Undetermined as e:
        check(f"★ prose after the colon is not a grant ({why})", "not a path" in str(e))

# breadth is behaviour, not a list of spellings
for bad in ("*/**", "**.*", "**/*.*", "*", "**", "**/*"):
    try:
        mg.check_grant([bad], "t"); check(f"grant {bad!r} refused", False)
    except mg.Undetermined as e:
        check(f"★ {bad!r} refused as an off switch", "off switch" in str(e))

# ...while the real forms still parse
(CONTENT / "L-spec-bold.md").write_text("**Writes:** `src/*`, docs/\n")
check("★ **Writes:** src/* parses (it was refused, accusing the author)",
      mg.writes_grant("L-spec-bold") == ["src/*", "docs/"])
(CONTENT / "L-spec-tbl2.md").write_text("| **Writes** | `src/**`, `db/migrate/` |\n")
check("a table row with symmetric globs survives",
      mg.writes_grant("L-spec-tbl2") == ["src/**", "db/migrate/"])
check("a bare directory needs its slash, and then works", mg.granted("src/a.py", ["src/"]))

# a line break in ANY path must not misalign the batch and kill revert detection
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
weird = pathlib.Path(d, "we\nird.txt"); weird.write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "a path with a line break")
on(d, lambda dd: pathlib.Path(dd, "old.txt").write_text("v1"))
check("★ a newline-bearing path does not kill revert detection for other files",
      mg.gate("work", "main", GRANT).reverted == ["old.txt"])

# ambient env must not redirect the gate at another repo
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
other, _ = repo()
os.chdir(d); os.environ["GIT_DIR"] = str(pathlib.Path(other, ".git"))
try:
    check("★ GIT_DIR cannot redirect the gate to another repository",
          any(x.startswith("late.txt@") for x in mg.gate("work", "main", []).removed))
finally:
    del os.environ["GIT_DIR"]

# an ambiguous ref is refused even when git is told not to warn about it
d, _ = repo()
sh("git", "-C", d, "tag", "main", "HEAD~1")
sh("git", "-C", d, "config", "core.warnAmbiguousRefs", "false")
os.chdir(d)
try:
    mg.gate("work", "main", []); check("ambiguity refused", False)
except mg.Undetermined as e:
    check("★ ambiguity is read from the ref store, not from a warning git can be told to suppress",
          "ambiguous" in str(e))

# knobs may not silently weaken the gate
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "migrations/001.sql").unlink())
for var, val in (("DOIT_MIGRATIONS", "zzz"), ("DOIT_REVERT_DEPTH", "0")):
    os.environ[var] = val
    try:
        mg.gate("work", "main", ["migrations/"]); check(f"{var}={val} refused", False)
    except mg.Undetermined:
        check(f"★ {var}={val} is refused, not silently honoured", True)
    finally:
        del os.environ[var]
mg.load_config()

d, _ = repo(); os.chdir(d)
try:
    mg.gate("main", "main", []); check("self-merge refused", False)
except mg.Undetermined as e:
    check("gating a branch against itself is could-not-determine", "nothing to gate" in str(e))

# ------------------------------------------- a conflict is its own outcome (R7/R8)
# ★ A real content conflict used to raise the SAME undifferentiated Undetermined
# as a shallow clone and a malformed grant, so `merge-gate-rework` was all the
# Executor ever saw — and its only rule for that is escalation. Two specs
# building the same files is the normal case; it cost a human decision every time.


def conflict_repo():
    """main and work edit the SAME line of the same file. A real collision,
    performed by git, never modelled — the branch is left checked out and the
    process cwd is the repo, exactly as the Executor's merge step stands."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    pathlib.Path(d, "src").mkdir()
    pathlib.Path(d, "src/a.py").write_text("BASE\nx\ny\n")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    sh("git", "-C", d, "branch", "work")
    pathlib.Path(d, "src/a.py").write_text("MAIN\nx\ny\n")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "main edits the line")
    sh("git", "-C", d, "checkout", "-q", "work")
    pathlib.Path(d, "src/a.py").write_text("WORK\nx\ny\n")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "work edits the line")
    os.chdir(d)
    return d


def events():
    return [json.loads(l) for l in LEDGER.read_text().splitlines()]


def of_type(t, subject=None):
    return [e for e in events() if e["type"] == t and (subject is None or e["subject"] == subject)]


# the branch really does conflict — assert the premise before grading the verdict
d = conflict_repo()
check("the fixture is a real conflict (git itself says so)",
      subprocess.run(("git", "-C", d, "merge-tree", "--write-tree", "main", "work"),
                     capture_output=True).returncode != 0)
before = len(of_type("conflict-rework", "L-spec-0001"))
check("a conflicting branch with --spec still exits 1",
      mg.main_(["work", "main", "--spec", "L-spec-0001"]) == 1)
ev = of_type("conflict-rework", "L-spec-0001")
check("★ a real conflict appends conflict-rework, not merge-gate-rework",
      len(ev) == before + 1 and events()[-1]["type"] == "conflict-rework")
check("the first conflict is attempt 1 and a re-dispatch",
      ev[-1]["attempt"] == 1 and ev[-1]["verdict"] == "re-dispatch")
check("★ attempt is a JSON integer, not the string \"1\" (fold's bare-value rule)",
      isinstance(ev[-1]["attempt"], int) and not isinstance(ev[-1]["attempt"], bool))
check("the colliding paths are on the event, exactly",
      ev[-1]["paths"] == ["src/a.py"] and ev[-1]["branch"] == "work")

# ★ R8: the SECOND time is an escalation. Nothing counted before this, so every
# conflict was could-not-determine and every one of those escalated on attempt 1.
check("a second conflict for the same spec still exits 1",
      mg.main_(["work", "main", "--spec", "L-spec-0001"]) == 1)
ev = of_type("conflict-rework", "L-spec-0001")
check("★ the second conflict is attempt 2 and escalate",
      len(ev) == before + 2 and ev[-1]["attempt"] == 2 and ev[-1]["verdict"] == "escalate")
check("★ the gate never appends escalation-blocking itself — that is the "
      "Executor's act, and the boundary is held in code, not prose",
      "escalation-blocking" not in LEDGER.read_text())

# a conflict with NO spec id has nothing to re-dispatch and nothing to count
check("a conflict without --spec still exits 1", mg.main_(["work", "main"]) == 1)
check("★ no --spec: the pre-existing merge-gate-rework path, untouched",
      events()[-1]["type"] == "merge-gate-rework"
      and "does not apply cleanly" in events()[-1]["undetermined"])

# ★ AND ONLY THE CONFLICT MESSAGE RECLASSIFIES. check_grant/grant_for raise
# BEFORE gate() runs, so a missing content file on a genuinely conflicting branch
# would otherwise report a conflict and hide the grant error behind it.
n_conflict = len(of_type("conflict-rework"))
for argv, why in ((["no-such-branch", "main", "--spec", "L-spec-0001"], "an unknown ref"),
                  (["main", "main", "--spec", "L-spec-0001"], "a branch gated against itself"),
                  (["work", "main", "--spec", "L-spec-none"], "a spec with no content file")):
    check(f"★ with --spec, {why} is still generic rework", mg.main_(argv) == 1
          and events()[-1]["type"] == "merge-gate-rework")
check("★ not one of the three emitted conflict-rework (a grant error is never "
      "masked behind a conflict verdict)", len(of_type("conflict-rework")) == n_conflict)

# and a CLEAN merge with --spec is still clean — the new branch is inert there
d, _ = repo(); on(d, lambda dd: None)
check("a clean branch with --spec is unaffected", mg.main_(["work", "main", "--spec", "L-spec-0001"]) == 0)
check("and it wrote merge-gate-clean", events()[-1]["type"] == "merge-gate-clean")

# ------------------------------------------------- R3e/R3f: writes: grant shapes

# R3e/AC7: a bare, EMPTY **Writes:** declaration reads the path list beneath it
# exactly as a parenthetical value already does, instead of raising
# "has a writes: line with no paths on it".
(CONTENT / "L-spec-bare.md").write_text("**Writes:**\n- src/foo.py\n- src/bar.py\n")
check("★ a bare, empty **Writes:** reads the path list beneath it (R3e/AC7)",
      mg.writes_grant("L-spec-bare") == ["src/foo.py", "src/bar.py"])
(CONTENT / "L-spec-bareprose.md").write_text("**Writes:** see the table below\n- src/x.py\n")
try:
    mg.writes_grant("L-spec-bareprose"); check("bare-shape prose rejected", False)
except mg.Undetermined as e:
    check("★ prose on a bare Writes: line is still refused (not the empty-value case)",
          "not a path" in str(e))

# AC8: grant_from_text(text) is a new, pure function — no content-directory
# read, no spec id — reading all three accepted shapes directly.
check("★ grant_from_text(a) — the bare-label path block",
      mg.grant_from_text("**Writes:**\n- src/foo.py\n- src/bar.py\n") == ["src/foo.py", "src/bar.py"])
check("★ grant_from_text(b) — the wrapped-parenthetical shape, unchanged",
      mg.grant_from_text("**Writes:** (the merge gate's grant)\n\n```\na/b.py\nc/\n```\n")
      == ["a/b.py", "c/"])
try:
    mg.grant_from_text("Writes: the web dashboard only\n"); check("prose(a/b) rejected", False)
except mg.Undetermined as e:
    check("★ grant_from_text refuses prose in the bare/parenthetical shapes",
          "not a path" in str(e))

# AC8(c): the YAML-frontmatter writes: list, per content/carry-L-spec-0166.packet.md:34-36
# ("the shape all four inbound PRs use").
FRONT = ("---\nintent: >\n  some text\nbase_sha: ad7a9f7c4\nwrites:\n"
         "  - pipelines/amazon/collectors/tier1.py\n"
         "  - tests/pipelines/amazon/test_tier1.py\n---\n\n# title\nbody\n")
check("★ grant_from_text(c) — the YAML-frontmatter writes: list",
      mg.grant_from_text(FRONT) == ["pipelines/amazon/collectors/tier1.py",
                                     "tests/pipelines/amazon/test_tier1.py"])
(CONTENT / "L-spec-front.md").write_text(FRONT)
check("★ writes_grant reads the frontmatter shape too, through the same indirection",
      mg.writes_grant("L-spec-front") == ["pipelines/amazon/collectors/tier1.py",
                                           "tests/pipelines/amazon/test_tier1.py"])

# A9: the fast path (PyYAML importable, the normal case here) and the narrow
# regex fallback (PyYAML hidden) parse the SAME frontmatter identically.
real_yaml = sys.modules.get("yaml")
sys.modules["yaml"] = None
try:
    check("★ A9 fallback (PyYAML hidden) parses the frontmatter shape identically",
          mg.grant_from_text(FRONT) == ["pipelines/amazon/collectors/tier1.py",
                                         "tests/pipelines/amazon/test_tier1.py"])
finally:
    if real_yaml is not None:
        sys.modules["yaml"] = real_yaml
    else:
        sys.modules.pop("yaml", None)

# frontmatter prose is still refused.
try:
    mg.grant_from_text("---\nwrites: the web dashboard only\n---\n\nbody\n")
    check("frontmatter prose rejected", False)
except mg.Undetermined as e:
    check("★ prose in the frontmatter writes: value is still refused",
          "not a path" in str(e))

# a frontmatter block with no writes: key at all falls through cleanly — it
# never crashes on a missing key.
try:
    mg.grant_from_text("---\nintent: x\n---\n\nno writes: line anywhere\n")
    check("frontmatter-with-no-writes-key rejected", False)
except mg.Undetermined as e:
    check("★ a frontmatter block with no writes: key falls through, not a crash",
          "states no writes" in str(e))

# L-spec-0273/AC11 PL-008(a): the path-token regex accepts `(` and `)`, so a
# Next.js route-group path no longer raises Undetermined — the pre-fix pattern
# refused a legitimate Writes: grant outright.
BRACKET_PATH = "dashboard/src/app/(dashboard)/dashboard/clients/[name]/profit-v2/reports/page.tsx"
check("★ AC11 — grant_from_text accepts a Writes: path holding ( ) [ ]",
      mg.grant_from_text(f"Writes: {BRACKET_PATH}\n") == [BRACKET_PATH])

# ------------------------------------------------------ R3f/AC9: out_of_grant

(CONTENT / "L-spec-oog.md").write_text("**Writes:** src/*\n")

# Reproduction 1: an addition/modification outside the grant, on a branch that
# is otherwise gate()-clean (no removal, no revert).
d, _ = repo()
on(d, lambda dd: (pathlib.Path(dd, "docs").mkdir(exist_ok=True),
                  pathlib.Path(dd, "docs/new.md").write_text("outside the grant")))
og = mg.out_of_grant("work", "main", "L-spec-oog")
check("★ out_of_grant names an addition outside the grant (R3f/AC9 repro 1)",
      og == ["docs/new.md"])
gr = mg.gate("work", "main", ["src/*"])
check("★ ...while gate() on the identical branch/grant still reports clean",
      gr.removed == [] and gr.reverted == [])

# Reproduction 2: a rename FROM a granted path INTO an ungranted destination —
# named by the DESTINATION, never the source; gate()'s own verdict (which
# judges the SAME rename by its source) is unchanged.
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "src").mkdir(exist_ok=True); pathlib.Path(d, "src/keep2.py").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "granted source file")
def rename_out(dd):
    pathlib.Path(dd, "docs").mkdir(exist_ok=True)
    sh("git", "-C", dd, "mv", "src/keep2.py", "docs/keep2.py")
on(d, rename_out)
og2 = mg.out_of_grant("work", "main", "L-spec-oog")
check("★ a rename INTO an ungranted destination is named by the destination (R3f/AC9 repro 2)",
      og2 == ["docs/keep2.py"])
gr2 = mg.gate("work", "main", ["src/*"])
check("★ gate() itself is unchanged — it still judges the SAME rename by its source",
      gr2.removed == [])

# out_of_grant still hard-stops on the same conditions gate() does.
try:
    mg.out_of_grant("no-such-branch", "main", "L-spec-oog")
    check("out_of_grant unresolvable ref raises", False)
except mg.Undetermined:
    check("★ out_of_grant raises Undetermined for the same hard stops as gate() "
          "(unresolvable ref)", True)

# ── AC6/AC7/AC8 (L-spec-0429) · the freeze refusal reads through
# freeze.state() and preserves afc2860's exact conditions/message ───────────
_FLAG_DEFAULT = mg.freeze.FLAG
os.environ.pop("DOIT_IGNORE_FREEZE", None)

# AC6 — an `albert-scott`-path repo, flag present, DOIT_IGNORE_FREEZE unset:
# main_() refuses, message carries the exact flag text, via freeze.state().
d6, _ = repo(prefix="albert-scott-")
on(d6, lambda dd: None)
flag6 = pathlib.Path(TMP) / "flag6"
flag6.write_text("frozen for Codex #445 — cap 18:00Z")
mg.freeze.FLAG = flag6
try:
    mg.main_(["work", "main"])
    raised6 = False
except SystemExit as e:
    raised6, msg6 = True, str(e)
check("★ AC6: main_() refuses via freeze.state(), message carries the flag text",
      raised6 and "frozen for Codex #445 — cap 18:00Z" in msg6)

# AC7 — same frozen albert-scott-path repo; DOIT_IGNORE_FREEZE=1 bypasses the
# refusal (existing knob preserved unchanged by the refactor).
os.environ["DOIT_IGNORE_FREEZE"] = "1"
try:
    rc7 = mg.main_(["work", "main"])
    raised7 = False
except SystemExit:
    raised7, rc7 = True, None
check("★ AC7: DOIT_IGNORE_FREEZE=1 bypasses the refusal — an ordinary gate() "
      "verdict, no SystemExit", not raised7 and rc7 in (0, 1))
os.environ.pop("DOIT_IGNORE_FREEZE", None)

# AC8 — flag present, but the repo path does NOT contain `albert-scott` (the
# plain repo()/on() fixtures, unchanged): the existing repo-scoping carve-out
# is preserved unchanged by the refactor.
d8, _ = repo()
on(d8, lambda dd: None)
mg.freeze.FLAG = flag6           # still exists; only the repo path differs now
try:
    rc8 = mg.main_(["work", "main"])
    raised8 = False
except SystemExit:
    raised8, rc8 = True, None
check("★ AC8: a non-albert-scott repo path bypasses the refusal (existing "
      "repo-scoping carve-out)", not raised8 and rc8 in (0, 1))

mg.freeze.FLAG = _FLAG_DEFAULT

# ============================================================ L-spec-0430
# ------------------------------------------- R4/gate-secret-scan: AC1-AC10
# Every fixture below commits the REAL, already-shipped albert-scott scanner
# and baseline (sha256s cited in the spec) except where an AC deliberately
# substitutes a stand-in for its own script (AC4b/AC4d/AC10's branch-side
# rewrite) — never a mock of detect-secrets itself (AC5/AC6/AC7/AC8/AC9/AC10
# exec the box's real, already-installed detect-secrets 1.5.0).

REAL_SECRET_SCRIPT = pathlib.Path("/opt/albert-scott/scripts/ci/detect-secrets-changed.sh").read_bytes()
REAL_SECRET_BASELINE = pathlib.Path("/opt/albert-scott/.secrets.baseline").read_bytes()
REAL_DETECT_SECRETS_BIN = "/opt/albert-scott/.venv/bin/detect-secrets"
RESTRICTED_PATH = "/usr/bin:/bin"           # bash/git/mktemp/cp/openssl/python3 live here;
                                             # detect-secrets deliberately does not


def _install_detect_secrets(repo_dir, marker=None):
    """<repo_dir>/.venv/bin/detect-secrets — a thin wrapper that (optionally)
    appends one line to `marker` and then execs the box's real, already-
    installed detect-secrets 1.5.0 with argv unchanged (AC5). Not a stub:
    scanning behaviour is identical to every other AC."""
    binp = pathlib.Path(repo_dir, ".venv", "bin", "detect-secrets")
    binp.parent.mkdir(parents=True, exist_ok=True)
    marker_line = f'printf \'x\\n\' >> "{marker}"\n' if marker else ""
    binp.write_text(f"#!/usr/bin/env bash\n{marker_line}exec {REAL_DETECT_SECRETS_BIN} \"$@\"\n")
    binp.chmod(0o755)


def secrets_fixture(secret=False, baseline_on_branch=True, script_bytes=None, install_bin=False, bin_marker=None):
    """main commits the (real, unless overridden) scanner + baseline; the
    branch adds exactly one relevant-extension file (`added.py`), with or
    without a secret-shaped literal. Returns (dir, main_sha, branch_sha)."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    ci = pathlib.Path(d, "scripts", "ci"); ci.mkdir(parents=True)
    (ci / "detect-secrets-changed.sh").write_bytes(script_bytes if script_bytes is not None else REAL_SECRET_SCRIPT)
    (ci / "detect-secrets-changed.sh").chmod(0o755)
    pathlib.Path(d, ".secrets.baseline").write_bytes(REAL_SECRET_BASELINE)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base w/ scanner")
    main_sha = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "branch", "work")
    sh("git", "-C", d, "checkout", "-q", "work")
    if not baseline_on_branch:
        sh("git", "-C", d, "rm", "-q", ".secrets.baseline")
    content = ('API_KEY = "%s"\n' % os.urandom(32).hex()) if secret else "x = 1\n"
    pathlib.Path(d, "added.py").write_text(content)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "branch work")
    branch_sha = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    if install_bin:
        _install_detect_secrets(d, marker=bin_marker)
    return d, main_sha, branch_sha


def _with_restricted_path(fn):
    """Run fn() with mg.ENV["PATH"] narrowed to RESTRICTED_PATH — so an
    ambient detect-secrets elsewhere on this dev box's PATH cannot mask a
    DETECT_SECRETS_BIN precedence check — restoring the original value
    (present or absent) unconditionally afterward."""
    had = "PATH" in mg.ENV
    orig = mg.ENV.get("PATH")
    mg.ENV["PATH"] = RESTRICTED_PATH
    try:
        return fn()
    finally:
        if had:
            mg.ENV["PATH"] = orig
        else:
            mg.ENV.pop("PATH", None)


# AC1 — not-applicable when main_sha never had any scripts/ci/ path at all.
d, base = repo()
os.chdir(d); mg.begin_run(d)
m_sha, b_sha = sh("git", "-C", d, "rev-parse", "main").strip(), sh("git", "-C", d, "rev-parse", "work").strip()
check("AC1", mg.secret_scan(d, m_sha, b_sha) == ("clean", []))

# AC2 — a relevant-extension addition with no secret-shaped content is clean,
# end-to-end through main_ too, with no reason=/findings= keys on the event.
d, main_sha, branch_sha = secrets_fixture(secret=False, install_bin=True)
os.chdir(d); mg.begin_run(d)
check("AC2 secret_scan direct call",
      _with_restricted_path(lambda: mg.secret_scan(d, main_sha, branch_sha)) == ("clean", []))
rc = _with_restricted_path(lambda: mg.main_(["work", "main"]))
check("AC2 main_ exits 0", rc == 0)
ev = events()[-1]
check("AC2 merge-gate-clean carries no reason/findings",
      ev["type"] == "merge-gate-clean" and "reason" not in ev and "findings" not in ev)

# AC3 — a secret-shaped literal with no baseline entry is findings; through
# main_ it flips clean to rework, carrying reason=secret-scan and findings=,
# and NO removed/reverted keys.
d, main_sha, branch_sha = secrets_fixture(secret=True, install_bin=True)
os.chdir(d); mg.begin_run(d)
verdict, findings = _with_restricted_path(lambda: mg.secret_scan(d, main_sha, branch_sha))
check("AC3 secret_scan direct call",
      verdict == "rework" and findings and all(mg.FINDING_LINE_RE.match(l) for l in findings))
rc = _with_restricted_path(lambda: mg.main_(["work", "main"]))
check("AC3 main_ exits 1", rc == 1)
ev = events()[-1]
check("AC3 rework carries reason=secret-scan, matching findings, no removed/reverted",
      ev["type"] == "merge-gate-rework" and ev.get("reason") == "secret-scan"
      and ev.get("findings") == findings and "removed" not in ev and "reverted" not in ev)

# AC4 — four undetermined triggers, each via mg.main_(["work", "main"]).
# (a) the committed baseline is absent from the branch tip. The removal is
# itself granted (--writes covers it) so gate()'s OWN removed-list stays
# empty and this run reaches secret_scan at all — proving the SCRIPT's own
# exit-2 baseline check, not gate()'s unrelated out-of-grant-removal path.
d, main_sha, branch_sha = secrets_fixture(secret=False, baseline_on_branch=False)
os.chdir(d)
rc = mg.main_(["work", "main", "--writes", ".secrets.baseline", "--writes", "added.py"])
ev = events()[-1]
check("AC4(a) tooling failure holds without gate rework", rc == 2 and ev["type"] != "merge-gate-rework")

# (b) the script subprocess hangs past secret_scan's own timeout; the WHOLE
# process group is killed, not just `bash`.
SLEEP_SCRIPT = b"#!/usr/bin/env bash\nexec -a L_SPEC_0430_AC4B_MARKER sleep 999999 &\nwait\n"
d, main_sha, branch_sha = secrets_fixture(secret=False, script_bytes=SLEEP_SCRIPT)
os.chdir(d)
_orig_timeout = mg.DETECT_SECRETS_TIMEOUT
_orig_scan_budget = mg.scan_deadline
mg.scan_deadline = lambda count: 2.0
try:
    rc = mg.main_(["work", "main"])
finally:
    mg.DETECT_SECRETS_TIMEOUT = _orig_timeout
    mg.scan_deadline = _orig_scan_budget
ev = events()[-1]
check("AC4(b) two timeouts -> infrastructure hold, no rework",
      rc == 2 and ev["type"] == "infra-failure" and ev.get("backend") == "merge-gate"
      and ev.get("retry_at"))
_ps = subprocess.run(["ps", "-ef"], capture_output=True, text=True).stdout
check("★ AC4(b) the whole process group was killed, not just bash",
      "L_SPEC_0430_AC4B_MARKER" not in _ps)

# (c) no detect-secrets anywhere — neither <repo>/.venv/bin/ nor the PATH.
d, main_sha, branch_sha = secrets_fixture(secret=False, install_bin=False)
os.chdir(d)
rc = _with_restricted_path(lambda: mg.main_(["work", "main"]))
ev = events()[-1]
check("AC4(c) tooling failure holds without gate rework", rc == 2 and ev["type"] != "merge-gate-rework")

# (d) exit 1 with only banner lines, no line matching the finding shape.
BANNER_ONLY_SCRIPT = (
    b"#!/usr/bin/env bash\n"
    b'echo "\xf0\x9f\x9a\xa8 1 potential secret(s) detected in changed files:"\n'
    b'echo "If these are false positives, audit them into .secrets.baseline."\n'
    b'echo "If real: rotate the secret immediately, then remove it from code."\n'
    b"exit 1\n")
d, main_sha, branch_sha = secrets_fixture(secret=False, script_bytes=BANNER_ONLY_SCRIPT)
os.chdir(d)
rc = mg.main_(["work", "main"])
ev = events()[-1]
check("AC4(d) tooling failure holds without gate rework", rc == 2 and ev["type"] != "merge-gate-rework")

# AC5 — DETECT_SECRETS_BIN precedence and invocation count: the repo-local
# wrapper, never an ambient binary, is invoked exactly twice (the script's
# own negative-control self-test, then the real scan).
marker = pathlib.Path(tempfile.mkdtemp()) / "calls.log"
d, main_sha, branch_sha = secrets_fixture(secret=False, install_bin=True, bin_marker=marker)
os.chdir(d); mg.begin_run(d)
verdict, findings = _with_restricted_path(lambda: mg.secret_scan(d, main_sha, branch_sha))
check("AC5 verdict clean", (verdict, findings) == ("clean", []))
calls = marker.read_text().splitlines() if marker.exists() else []
check("★ AC5 the repo-local wrapper was invoked exactly twice", len(calls) == 2)
check("★ AC5 never runs git worktree add against /opt/albert-scott itself",
      mg._TOP != "/opt/albert-scott" and not str(d).startswith("/opt/albert-scott"))

# AC6 — worktree lifecycle: clean, findings, and the AC4(b) timeout case,
# leaving `git worktree list` byte-identical before and after.
d = tempfile.mkdtemp()
sh("git", "init", "-q", "-b", "main", d)
sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
ci = pathlib.Path(d, "scripts", "ci"); ci.mkdir(parents=True)
(ci / "detect-secrets-changed.sh").write_bytes(REAL_SECRET_SCRIPT); (ci / "detect-secrets-changed.sh").chmod(0o755)
pathlib.Path(d, ".secrets.baseline").write_bytes(REAL_SECRET_BASELINE)
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base w/ real scanner")
main_sha_a = sh("git", "-C", d, "rev-parse", "HEAD").strip()
sh("git", "-C", d, "branch", "work-clean")
sh("git", "-C", d, "checkout", "-q", "work-clean")
pathlib.Path(d, "added.py").write_text("x = 1\n")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "clean add")
branch_sha_clean = sh("git", "-C", d, "rev-parse", "HEAD").strip()
sh("git", "-C", d, "checkout", "-q", "main")
sh("git", "-C", d, "branch", "work-findings")
sh("git", "-C", d, "checkout", "-q", "work-findings")
pathlib.Path(d, "added.py").write_text('API_KEY = "%s"\n' % os.urandom(32).hex())
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "findings add")
branch_sha_findings = sh("git", "-C", d, "rev-parse", "HEAD").strip()
sh("git", "-C", d, "checkout", "-q", "main")
(ci / "detect-secrets-changed.sh").write_bytes(SLEEP_SCRIPT)
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "swap in a sleep stand-in")
main_sha_b = sh("git", "-C", d, "rev-parse", "HEAD").strip()
sh("git", "-C", d, "branch", "work-timeout")
branch_sha_timeout = sh("git", "-C", d, "rev-parse", "work-timeout").strip()
_install_detect_secrets(d, marker=None)
os.chdir(d)
wt_before = sh("git", "-C", d, "worktree", "list")
_orig_timeout = mg.DETECT_SECRETS_TIMEOUT
_orig_scan_budget = mg.scan_deadline

def _ac6():
    mg.begin_run(d)
    v1 = mg.secret_scan(d, main_sha_a, branch_sha_clean)
    mg.begin_run(d)
    v2 = mg.secret_scan(d, main_sha_a, branch_sha_findings)
    mg.scan_deadline = lambda count: 2.0
    mg.begin_run(d)
    v3 = mg.secret_scan(d, main_sha_b, branch_sha_timeout)
    return v1, v2, v3

try:
    (v1, f1), (v2, f2), (v3, f3) = _with_restricted_path(_ac6)
finally:
    mg.DETECT_SECRETS_TIMEOUT = _orig_timeout
    mg.scan_deadline = _orig_scan_budget
check("AC6 call 1 clean", (v1, f1) == ("clean", []))
check("AC6 call 2 findings", v2 == "rework" and bool(f2))
check("AC6 call 3 undetermined (timeout)", v3 == "undetermined" and bool(f3))
wt_after = sh("git", "-C", d, "worktree", "list")
check("★ AC6 git worktree list is byte-identical before and after all three verdicts",
      wt_before == wt_after)

# AC7 — poisoned GIT_DIR/GIT_WORK_TREE must not redirect secret_scan's OWN
# git calls (ls-tree/show/worktree add) at another repository.
d, main_sha, branch_sha = secrets_fixture(secret=False, install_bin=True)
other_d, _, _ = secrets_fixture(secret=False)
os.chdir(d); mg.begin_run(d)
os.environ["GIT_DIR"] = str(pathlib.Path(other_d, ".git"))
os.environ["GIT_WORK_TREE"] = other_d
try:
    verdict, findings = mg.secret_scan(d, main_sha, branch_sha)
    check("AC7 poisoned GIT_DIR/GIT_WORK_TREE cannot redirect secret_scan's own git calls",
          (verdict, findings) == ("clean", []))
finally:
    del os.environ["GIT_DIR"]; del os.environ["GIT_WORK_TREE"]

# AC8 — the same poisoning must not redirect the SCRIPT SUBPROCESS's own
# internal `git diff` either (its env is dict(ENV, ...), never os.environ).
d, main_sha, branch_sha = secrets_fixture(secret=True, install_bin=True)
os.chdir(d); mg.begin_run(d)
os.environ["GIT_DIR"] = str(pathlib.Path(other_d, ".git"))
os.environ["GIT_WORK_TREE"] = other_d
try:
    verdict, findings = mg.secret_scan(d, main_sha, branch_sha)
    check("AC8 the script subprocess's own git diff is isolated from ambient GIT_DIR/GIT_WORK_TREE",
          verdict == "rework" and bool(findings))
finally:
    del os.environ["GIT_DIR"]; del os.environ["GIT_WORK_TREE"]

# AC9 — call-site gating: gate() already found a removal outside the grant ->
# secret_scan is never reached, however secret-shaped the addition is.
d = tempfile.mkdtemp()
sh("git", "init", "-q", "-b", "main", d)
sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
ci = pathlib.Path(d, "scripts", "ci"); ci.mkdir(parents=True)
(ci / "detect-secrets-changed.sh").write_bytes(REAL_SECRET_SCRIPT); (ci / "detect-secrets-changed.sh").chmod(0o755)
pathlib.Path(d, ".secrets.baseline").write_bytes(REAL_SECRET_BASELINE)
pathlib.Path(d, "keep.txt").write_text("keep")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
sh("git", "-C", d, "branch", "work")
sh("git", "-C", d, "checkout", "-q", "work")
pathlib.Path(d, "keep.txt").unlink()                       # removal outside --writes docs/
pathlib.Path(d, "added.py").write_text('API_KEY = "%s"\n' % os.urandom(32).hex())
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "branch work")
os.chdir(d)
rc = mg.main_(["work", "main", "--writes", "docs/"])
ev = events()[-1]
check("AC9 gate()'s own grant violation short-circuits secret_scan entirely",
      rc == 1 and ev["type"] == "merge-gate-rework" and ev.get("removed")
      and "reason" not in ev and "findings" not in ev)

# AC10 — gate-gaming: a branch rewrites its OWN copy of the script to exit 0;
# secret_scan still executes main_sha's committed bytes and still finds it.
d = tempfile.mkdtemp()
sh("git", "init", "-q", "-b", "main", d)
sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
ci = pathlib.Path(d, "scripts", "ci"); ci.mkdir(parents=True)
(ci / "detect-secrets-changed.sh").write_bytes(REAL_SECRET_SCRIPT); (ci / "detect-secrets-changed.sh").chmod(0o755)
pathlib.Path(d, ".secrets.baseline").write_bytes(REAL_SECRET_BASELINE)
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base w/ real scanner")
main_sha = sh("git", "-C", d, "rev-parse", "HEAD").strip()
sh("git", "-C", d, "branch", "work")
sh("git", "-C", d, "checkout", "-q", "work")
(ci / "detect-secrets-changed.sh").write_text("#!/usr/bin/env bash\nexit 0\n")   # branch disarms ITS OWN copy
pathlib.Path(d, "added.py").write_text('API_KEY = "%s"\n' % os.urandom(32).hex())
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "branch disarms its own scanner")
branch_sha = sh("git", "-C", d, "rev-parse", "HEAD").strip()
_install_detect_secrets(d, marker=None)
os.chdir(d); mg.begin_run(d)
verdict, findings = _with_restricted_path(lambda: mg.secret_scan(d, main_sha, branch_sha))
check("AC10 the executed bytes come from main_sha, not the branch's rewritten copy",
      verdict == "rework" and bool(findings))

# ------------------------------------------------------ R6: append-only grants
# Kept verbatim from the committed albert-scott list, never branch-controlled.
APPEND_ENTRIES = [
    "scratch_listing_authority_cleanup.py::AUTHORITY_REVISIONS",
    "scripts/ci/collection_parity_allowlist.txt",
    "scripts/ci/vitest_known_red_baseline.txt",
    "deploy.sh::REQUIRED_ACTIVATIONS",
    "scripts/ci/check_declared_imports.py::ALLOWLIST",
    "tests/test_820_no_abs_test_imports.py::PYTEST_INI_SHA256_HISTORY",
]
LIST_PATH = "scripts/ci/append_only_files.txt"
SHELL_BLOCK = 'REQUIRED_ACTIVATIONS=(\n"old"\n)\necho end\n'


def append_repo(files, listing=None, older=None):
    root = harness.test_root("append-only")
    import atexit
    atexit.register(harness.cleanup, root)
    sh("git", "init", "-q", "-b", "main", str(root))
    sh("git", "-C", str(root), "config", "user.email", "t@t")
    sh("git", "-C", str(root), "config", "user.name", "t")
    def write(items):
        for name, data in items.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if older:
        write(older)
        sh("git", "-C", str(root), "add", "-A")
        sh("git", "-C", str(root), "commit", "-qm", "older shared data")
    write(files)
    if listing is not None:
        write({LIST_PATH: listing})
    sh("git", "-C", str(root), "add", "-A")
    sh("git", "-C", str(root), "commit", "-qm", "main shared data")
    sh("git", "-C", str(root), "checkout", "-qb", "work")
    os.chdir(root)
    return root


def append_commit():
    sh("git", "add", "-A")
    sh("git", "commit", "-q", "--allow-empty", "-m", "append fixture")
    mg.begin_run()


def append_case(name, before, after, expected, entry="data.txt", mutate=None):
    path = entry.split("::")[0]
    root = append_repo({path: before} if before is not None else {"keep": "keep"})
    target = root / path
    if after is None:
        target.unlink()
    else:
        target.write_bytes(after if isinstance(after, bytes) else after.encode())
    if mutate:
        mutate(target)
    append_commit()
    verdict, detail = mg.append_only_detail(entry, "main", "work")
    check("append_only " + name, verdict == expected and
          (detail == "" if expected else detail.startswith(path + ": ")) and
          mg.append_only(entry, "main", "work") == expected)
    return root


plain_cases = [
    ("end append", "a\nb\n", "a\nb\nc\n", True),
    ("middle append", "a\nb\n", "a\nc\nb\n", True),
    ("line deleted", "a\nb\n", "a\n", False),
    ("line changed", "a\nb\n", "a\nc\n", False),
    ("no final newline", "a", "a\nb\n", False),
    ("deleted file", "a\n", None, False),
    ("created file", None, "a\n", False),
    ("binary", b"a\0b\n", b"a\0b\nc\n", False),
    ("identical", "a\n", "a\n", False),
    ("CRLF changed to LF", b"a\r\nb\r\n", b"a\nb\r\nc\r\n", False),
]
for hostile in (False, True):
    for name, before, after, expected in plain_cases:
        def ambient(target):
            if hostile:
                script = target.parent / "external.sh"
                script.write_text("#!/bin/sh\necho useless\n")
                script.chmod(0o755)
                for key, value in (("color.ui", "always"), ("diff.external", str(script)),
                                   ("diff.algorithm", "patience"), ("diff.indentHeuristic", "false"),
                                   ("diff.unsafe.textconv", str(script)), ("diff.renames", "copies")):
                    sh("git", "config", key, value)
                (target.parent / ".gitattributes").write_text("data.txt diff=unsafe\n")
        append_case(name + (" hostile config" if hostile else ""), before, after, expected,
                    mutate=ambient)
    for extra in (False, True):
        append_case("mode change" + (" and append" if extra else "") + str(hostile),
                    "a\n", "a\nb\n" if extra else "a\n", False,
                    mutate=lambda p: (ambient(p), p.chmod(0o755)))
    append_case("symlink " + str(hostile), "a\n", "a\nb\n", False,
                mutate=lambda p: (ambient(p), p.unlink(), p.symlink_to("keep")))
    append_case("rename away " + str(hostile), "a\n", "a\n", False,
                mutate=lambda p: (ambient(p), p.rename(p.with_name("renamed.txt"))))
try:
    mg.append_only("data.txt", "unresolvable-ref", "work")
    check("append_only bad tree raises Undetermined", False)
except mg.Undetermined:
    check("append_only bad tree raises Undetermined", True)

shell_entry = "deploy.sh::REQUIRED_ACTIVATIONS"
for name, after, expected in [
    ("shell literal", SHELL_BLOCK.replace('"old"', '"old"\n"new"'), True),
    ("shell comment", SHELL_BLOCK.replace('"old"', '"old"\n# comment'), True),
    ("shell blank", SHELL_BLOCK.replace('"old"', '"old"\n'), True),
    ("shell after close", SHELL_BLOCK + '"new"\n', False),
    ("shell before open", '"new"\n' + SHELL_BLOCK, False),
    ("shell duplicate open", SHELL_BLOCK.replace('"old"', 'REQUIRED_ACTIVATIONS=(\n"old"'), False),
    ("shell wrong opener", 'REQUIRED_ACTIVATIONS_TEXT="\n"old"\n)\n', False),
    ("shell zero open", 'OTHER=(\n"old"\n"new"\n)\n', False),
    ("shell inside and outside", SHELL_BLOCK.replace('"old"', '"old"\n"new"') + '# outside\n', False),
] + [("shell rejects " + payload, SHELL_BLOCK.replace('"old"', '"old"\n' + payload), False)
     for payload in ('"x" ) ; cmd ; _z=(', '"$(cmd)"', '"`cmd`"', 'word', ')')]:
    append_case(name, SHELL_BLOCK, after, expected, shell_entry)
    if name == "shell inside and outside":
        check("SYMBOL reason names outside line", "line 6" in mg.append_only_detail(shell_entry, "main", "work")[1])

py_tuple = 'X = (\n    "old",\n)\n'
py_dict = 'ALLOWLIST: dict[str, str] = {\n    "old": """multi\nline""",\n    # interleaved comment\n}\n'
py_set = 'AUTHORITY_REVISIONS = frozenset(\n    {\n        ("old", "v1"),\n    }\n)\n'
for name, before, after, symbol, expected in [
    ("Assign tuple", py_tuple, py_tuple.replace('    "old",', '    "old",\n    ("new", 2),'), "X", True),
    ("AnnAssign dict multiline", py_dict, py_dict.replace('    #', '    "new": {"literal": 2},\n    #'), "ALLOWLIST", True),
    ("after statement", py_tuple, py_tuple + 'Y = 2\n', "X", False),
    ("changed entry", py_tuple, py_tuple.replace('old', 'new'), "X", False),
    ("one line no interior", 'X = (1,)\n', 'X = (1,)\n# new\n', "X", False),
    ("nested assignment", 'def f():\n    X = (\n        1,\n    )\n',
     'def f():\n    X = (\n        1,\n        2,\n    )\n', "X", False),
    ("unparseable", py_tuple, py_tuple.replace('    "old",', '    "old",\n    broken ?'), "X", False),
    ("absent name", py_tuple, py_tuple.replace('    "old",', '    "old",\n    2,'), "ABSENT", False),
    ("dict call", py_dict, py_dict.replace('    #', '    "k": __import__("os").system("x"),\n    #'), "ALLOWLIST", False),
    ("literal cannot hide same-line call", py_tuple,
     py_tuple.replace('    "old",', '    "old",\n    "new", cmd(),'), "X", False),
    ("tuple call", py_tuple, py_tuple.replace('    "old",', '    "old",\n    cmd(),'), "X", False),
    ("frozenset call gap", py_set, py_set.replace('    }\n', '    }\n    # injected\n'), "AUTHORITY_REVISIONS", False),
    ("frozenset literal", py_set, py_set.replace('        ("old", "v1"),', '        ("old", "v1"),\n        ("new", "v2"),'), "AUTHORITY_REVISIONS", True),
]:
    append_case("SYMBOL " + name, before, after, expected, "data.py::" + symbol)
for expression in ('1 + 2', '-1', '(lambda: 1)()', 'f"{1}"', '"safe", cmd()'):
    append_case("SYMBOL nonliteral " + expression, py_tuple,
                py_tuple.replace('    "old",', '    "old",\n    ' + expression + ','), False, "data.py::X")
for suffix in ("txt", "md"):
    append_case("SYMBOL wrong kind " + suffix, py_tuple, py_tuple + '# new\n', False, "data." + suffix + "::X")

root = append_repo({"data.txt": "a\n"}, "# shared files\n\n" + "\n".join(APPEND_ENTRIES) + "\n")
append_commit()
check("list six committed entries round-trip in order", mg.append_only_list("main") == APPEND_ENTRIES)
(root / LIST_PATH).write_text("branch-only.txt\n")
append_commit()
check("list ignores branch widening", mg.append_only_list("main") == APPEND_ENTRIES)
root = append_repo({"data.txt": "a\n"})
(root / LIST_PATH).parent.mkdir(parents=True, exist_ok=True)
(root / LIST_PATH).write_text("data.txt\n")
append_commit()
check("list absent at main, branch-only ignored", mg.append_only_list("main") == [])
for invalid in ("deploy.sh (new)", "-deploy.sh", "deploy.sh::", "x.py::A::B", "a b.txt", "some prose",
                "deploy.sh\ndeploy.sh::REQUIRED_ACTIVATIONS", "x.py::A\nx.py::B"):
    root = append_repo({"data.txt": "a\nb\n"}, "data.txt\n" + invalid + "\n",
                       older={"data.txt": "a\n"})
    (root / "data.txt").write_text("a\n")
    append_commit()
    check("list unusable " + invalid.replace("\n", "/"), mg.append_only_list("main") == [] and
          mg.LAST_LIST_ERROR == invalid.splitlines()[-1])
    import contextlib, io
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = mg.main_(["work", "main"])
    check("unusable list exempts nothing and reports first bad line", rc == 1 and
          "append-only list unusable: " + invalid.splitlines()[-1] in output.getvalue() and
          events()[-1]["reverted"] == ["data.txt"] and "append_only" not in events()[-1])

(CONTENT / "L-spec-append.md").write_text("Writes: src/*\n")
for kind in ("append", "revert-shaped", "rewrite-revert", "rewrite-new", "granted", "removed", "unlisted"):
    older = {"deploy.sh": SHELL_BLOCK.replace('"old"', '"prior"'), "tests/allow.txt": "a\nb\n"}
    root = append_repo({"deploy.sh": SHELL_BLOCK, "tests/allow.txt": "a\n"},
                       "deploy.sh::REQUIRED_ACTIVATIONS\ntests/allow.txt\n", older=older)
    if kind in ("append", "granted", "revert-shaped"):
        (root / "deploy.sh").write_text(SHELL_BLOCK.replace('"old"', '"old"\n"new"'))
        (root / "tests/allow.txt").write_text("a\nb\n" if kind == "revert-shaped" else "a\nc\n")
    elif kind == "removed":
        (root / "deploy.sh").unlink()
    elif kind == "unlisted":
        (root / "unlisted.txt").write_text("new\n")
    else:
        (root / "deploy.sh").write_text(SHELL_BLOCK.replace('"old"', '"prior"' if kind == "rewrite-revert" else '"different"'))
    append_commit()
    grant = ["deploy.sh", "tests/allow.txt"] if kind == "granted" else []
    result = mg.gate("work", "main", grant)
    channel = [dict(row) for row in mg.LAST_APPEND_ONLY]
    expected_entries = (["deploy.sh::REQUIRED_ACTIVATIONS", "tests/allow.txt"] if kind in
                        ("append", "revert-shaped") else ["deploy.sh::REQUIRED_ACTIVATIONS"]
                        if kind.startswith("rewrite") else [])
    check("gate channel order and reset " + kind, [row["entry"] for row in channel] == expected_entries and
          mg.LAST_LIST_ERROR is None and len(result) == 5 and
          all(set(row) == {"entry", "ok", "detail", "revert_shaped"} for row in channel))
    check("gate verdict " + kind, bool(result.removed) == (kind == "removed") and
          result.reverted == (["deploy.sh"] if kind == "rewrite-revert" else []))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = mg.main_(["work", "main"] + (["--writes"] + grant if grant else []))
    check("gate CLI and event " + kind, rc == (1 if kind in ("removed", "rewrite-revert") else 0) and
          events()[-1].get("append_only", []) == channel)
    for row in channel:
        prefix = "append-only ok (revert-shaped): " if row["ok"] and row["revert_shaped"] else (
                 "append-only ok: " if row["ok"] else "append-only violated: ")
        check("gate printed verdict " + row["entry"], prefix + row["entry"] in output.getvalue() and
              row["ok"] == (not kind.startswith("rewrite")) and
              (row["detail"] == "" if row["ok"] else "deleted" in row["detail"]))
    advisory = mg.out_of_grant("work", "main", "L-spec-append")
    expected = (["tests/allow.txt (append-only ok, revert-shaped)"] if kind == "revert-shaped" else
                ["deploy.sh"] if kind.startswith("rewrite") else ["unlisted.txt"] if kind == "unlisted" else [])
    check("out_of_grant append rules " + kind, advisory == expected)
    if kind == "revert-shaped":
        check("gate channel marks revert-shaped", channel[1]["revert_shaped"] is True)
    # main_ must only print the channel: forbid helper/git access after gate returns.
    real_gate, real_git, real_scan = mg.gate, mg.git, mg.secret_scan
    def cached_gate(*args):
        mg.LAST_APPEND_ONLY, mg.LAST_LIST_ERROR = channel, None
        return result
    def no_git(*args, **kwargs):
        raise AssertionError("main_ recomputed append-only verdicts")
    mg.gate, mg.git, mg.secret_scan = cached_gate, no_git, lambda *args: ("clean", [])
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mg.main_(["work", "main"])
        check("main_ only reads append channel " + kind, True)
    finally:
        mg.gate, mg.git, mg.secret_scan = real_gate, real_git, real_scan
    mg.begin_run()
    check("begin_run resets append channel " + kind, mg.LAST_APPEND_ONLY == [] and mg.LAST_LIST_ERROR is None)

# A later git failure must preserve already-computed diagnostics on rework.
root = append_repo({"deploy.sh": SHELL_BLOCK}, "deploy.sh::REQUIRED_ACTIVATIONS\n")
(root / "deploy.sh").write_text(SHELL_BLOCK.replace('"old"', '"old"\n"new"'))
append_commit()
real_git = mg.git

def fail_later(*args, **kwargs):
    if args[:1] == ("diff",) and "--no-ext-diff" in args:
        raise mg.Undetermined("injected git diff failure")
    return real_git(*args, **kwargs)

# The first real path is judged; force a subsequent git failure before return.
real_gate = mg.gate

def partial_gate(*args):
    real_gate(*args)
    mg.git = fail_later
    mg.append_only("deploy.sh::REQUIRED_ACTIVATIONS", "main", "work")

mg.gate = partial_gate
try:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = mg.main_(["work", "main"])
    check("Undetermined preserves judged append event and output", rc == 1 and
          events()[-1].get("append_only") == mg.LAST_APPEND_ONLY and
          len(mg.LAST_APPEND_ONLY) == 1 and
          "append-only ok: deploy.sh::REQUIRED_ACTIVATIONS" in output.getvalue())
finally:
    mg.gate, mg.git = real_gate, real_git
with contextlib.redirect_stdout(io.StringIO()):
    mg.main_(["work", "main", "--writes", "**"])
check("pre-gate grant failure cannot reuse old append channel", "append_only" not in events()[-1])

# L-spec-1494: an infra secret-scan failure holds without rework, and cleans
# even the partial worktree left by a failed git worktree add.
from unittest.mock import patch
import hashlib
infra_events = []
removed_worktrees = []
real_git = mg.git

def infra_git(*args, **kwargs):
    if args[:1] == ("ls-tree",):
        return b"scripts/ci/detect-secrets-changed.sh\n"
    if args[:1] == ("show",):
        return b"#!/bin/bash\nexit 0\n"
    if args[:2] == ("worktree", "add"):
        raise mg.Undetermined("git worktree add: No space left on device")
    return real_git(*args, **kwargs)

with patch.object(mg, "git", side_effect=infra_git), \
     patch.object(mg, "_remove_worktree", side_effect=removed_worktrees.append), \
     patch.object(mg.fold, "append", side_effect=infra_events.append):
    verdict, findings = mg.secret_scan(d, "main", "work")
check("infra scan is undetermined", verdict == "undetermined" and findings == ["worktree", "infra:no-space"])
check("infra scan records once and cleans partial worktree", len(infra_events) == 1 and
      infra_events[0][0] == "infra-failure" and len(removed_worktrees) == 1)
with patch.object(mg, "gate", return_value=([], [], "main-sha", "branch-sha", "tree-sha")), \
     patch.object(mg, "secret_scan", return_value=(verdict, findings)), \
     patch.object(mg.fold, "append", side_effect=infra_events.append):
    check("infra main verdict is hold", mg.main_(["work", "main"]) == 2)
check("infra scan appends no rework or findings", len(infra_events) == 1)
body = json.dumps(dict(events=infra_events, diagnostics=findings, partial_worktrees_cleaned=len(removed_worktrees)), sort_keys=True).encode()
print(json.dumps(dict(criterion="AC4", url="cli://infra-retry/AC4", status=0 if all(ok) else 1,
                     body_sha256=hashlib.sha256(body).hexdigest(), body_excerpt=json.loads(body))))

print(f"merge-gate: {sum(ok)}/{len(ok)} checks pass")
sys.exit(0 if all(ok) else 1)
