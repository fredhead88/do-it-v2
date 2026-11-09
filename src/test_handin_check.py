#!/usr/bin/env python3
"""Scratch-only hand-in contract, with fresh ledger readers and signed evidence."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
# Import fold against a disposable default, never the account's live root.
BOOT = tempfile.TemporaryDirectory(prefix="handin-import-")
os.environ["DOIT_ROOT"] = str(Path(BOOT.name) / "default")
os.environ["DOIT_TEST_PROCESS"] = "1"
import fold
import merge_gate as mg
import handin_check as hc


class HandinContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handin-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "ledger"
        self.repo = self.home / "repo with spaces"
        self.repo.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "DOIT_GATE_", "SUPABASE_DB_URL"))}
        env.update(DOIT_ROOT=str(self.home / "default"),
                   DOIT_SCRATCH=str(self.home / "scratch"), DOIT_NO_POKE="1",
                   DOIT_LEDGER_FILE="L-builder-fixture.jsonl", DOIT_PROJECT="do-it-v2")
        env.pop("DOIT_MAIN", None)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        patch.object(fold, "ROOT", self.home / "default").start()
        patch.object(fold, "EVENTS", fold.ROOT / "events").start()
        patch.object(fold, "BOARD", fold.ROOT / "board.md").start()
        patch.object(fold, "PROJECT", "do-it-v2").start()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@localhost")
        self.script = self.repo / mg.DETECT_SECRETS_SCRIPT
        self.script.parent.mkdir(parents=True)
        self.script.write_text('#!/bin/bash\nif grep -qx FIND fixture; then\n'
                               '  echo "  fixture:1 -- Synthetic finding"; exit 1\nfi\nexit 0\n')
        (self.repo / "fixture").write_text("CLEAN\n")
        self.commit("base")
        self.main = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "work")
        self.observed = []

    def git(self, *args, repo=None):
        return subprocess.check_output(["git", "-C", str(repo or self.repo), *args],
                                       stderr=subprocess.DEVNULL, env=dict(os.environ)).decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-qm", message)

    def scanner(self, text):
        self.git("checkout", "main")
        self.script.write_text(text)
        self.commit("trusted scanner")
        self.main = self.git("rev-parse", "HEAD")
        self.git("checkout", "work")
        self.git("merge", "--no-edit", "main")

    def call(self, **kw):
        return hc.run(kw.pop("worktree", self.repo), "L-spec-fixture", root=self.root, **kw)

    def events(self, root=None):
        env = dict(os.environ, DOIT_ROOT=str(root or self.root), PYTHONPATH=str(SRC))
        env.pop("DOIT_PROJECT", None)
        p = subprocess.run([sys.executable, "-c", "import fold,json; print(json.dumps(fold.read_events(use_cache=False),default=str))"],
                           env=env, capture_output=True, text=True, check=True)
        return json.loads(p.stdout)

    def report(self, result, *, sha=True):
        rows = self.events()
        event = [r for r in rows if r["type"] == "handin-checked"][-1]
        self.assertEqual(event["subject"], "L-spec-fixture")
        self.assertEqual(event["sha"], self.git("rev-parse", "HEAD") if sha else None)
        self.assertEqual(event["checks"], ["secret-scan", "coupled-test-safety"])
        self.assertEqual(event["failed_check"], result.failed_check)
        self.assertEqual(event["result"], "clean" if result.ok else
                         "rework" if result.failed_check == "secret-scan" else "undetermined")
        self.assertEqual(event["cpu_accounting"], hc.CPU_ACCOUNTING)
        for key in ("cpu_s", "wall_s"):
            self.assertIsInstance(event[key], (int, float))
            self.assertTrue(math.isfinite(event[key]) and event[key] >= 0)
            self.assertEqual(event[key], getattr(result, key))
        self.assertEqual(event["actor"], "builder")
        return rows

    def evidence(self, **data):
        body = dict(selector=self._testMethodName, observations=self.observed, **data)
        raw = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url="cli://L-spec-2202/" + self._testMethodName,
                              status=0, body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))

    def test_committed_head(self):
        self.assertEqual(hc.ATTEMPT_SECONDS, 300)
        before = self.git("status", "--porcelain")
        with patch.object(mg, "secret_scan", wraps=mg.secret_scan) as scan, \
             patch.object(mg, "gate", side_effect=AssertionError("gate ran")):
            result = self.call()
            self.assertTrue(result.ok, result)
            args, kw = scan.call_args
            self.assertEqual(args[1:], (self.main, self.git("rev-parse", "HEAD")))
            self.assertIs(kw["wrap"], hc.cpu_share.niced)
            self.assertTrue(0 < kw["wall_cap"] <= 300)
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual((result.failed_check, result.findings), (None, []))
        (self.repo / "fixture").write_text("FIND\n")
        self.assertTrue(self.call().ok)  # dirty bytes cannot replace HEAD
        self.git("add", "fixture")
        (self.repo / "untracked").write_text("FIND\n")
        self.assertTrue(self.call().ok)
        self.commit("finding")
        (self.repo / "fixture").write_text("CLEAN\n")
        self.script.write_text("exit 0\n")
        self.commit("branch tries to replace scanner")
        # Commit the finding again, keeping the malicious branch scanner.
        (self.repo / "fixture").write_text("FIND\n")
        self.commit("committed finding")
        (self.repo / "fixture").write_text("CLEAN\n")
        status = self.git("status", "--porcelain")
        result = self.call()
        self.assertEqual((result.ok, result.failed_check, result.findings),
                         (False, "secret-scan", ["  fixture:1 -- Synthetic finding"]))
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.report(result)
        with patch.dict(os.environ, DOIT_MAIN="missing"):
            self.assertTrue(self.call(main=self.main).failed_check == "secret-scan")
            self.assertEqual(self.call().failed_check, "undetermined")
        with patch.dict(os.environ, DOIT_MAIN="refs/heads/main"):
            self.assertEqual(self.call().failed_check, "secret-scan")
        self.git("tag", "main")
        self.assertEqual(self.call().failed_check, "undetermined")
        self.assertEqual(self.call(main="refs/heads/main").failed_check, "secret-scan")
        self.assertTrue(self.call(main=self.git("rev-parse", "HEAD")).ok)
        self.observed.append("committed bytes; trusted main; explicit/default/ambiguous refs; unchanged status")
        self.evidence()

    def inventory(self):
        claims = {str(p.relative_to(self.root)): p.read_text() for p in (self.root / "seat").glob("*.claimed")}
        rows = [r for r in self.events() if r["type"] in {"spawn-started", "build-started", "seat-claimed"}]
        return dict(claims=claims, rows=rows)

    def test_budget_and_cpu(self):
        (self.root / "seat").mkdir(parents=True)
        (self.root / "seat/caller.claimed").write_text("seeded caller claim")
        with hc._ledger_root(self.root):
            with patch.dict(os.environ, DOIT_LEDGER_FILE="L-relay-fixture.jsonl"):
                fold.append(["seat-claimed", "caller", "project=do-it-v2"], strict=True)
        before = self.inventory()
        # Each process uses its own CPU clock, and the child waits its child.
        marker = self.home / "grandchild.json"
        grand = ("import time,os,json,pathlib; t=time.process_time(); "
                 "exec('while time.process_time()-t < .22: pass'); "
                 f"pathlib.Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(),cpu=time.process_time()-t)))")
        child = ("import time,subprocess,sys; t=time.process_time(); "
                 "exec('while time.process_time()-t < .22: pass'); "
                 f"subprocess.run([sys.executable,'-c',{grand!r}],check=True)")
        self.scanner("#!/bin/bash\nexec /usr/bin/python3 -c " + shlex.quote(child) + "\n")
        calls, during = [], []
        real_popen = subprocess.Popen
        def observe(argv, **kw):
            if argv[:1] == ["/usr/bin/nice"]:
                calls.append(dict(argv=argv[:3] + ["bash", "<trusted-script>", *argv[-3:]],
                                  cap=mg.DETECT_SECRETS_TIMEOUT))
                during.append(self.inventory())
            return real_popen(argv, **kw)
        with patch.object(mg.subprocess, "Popen", side_effect=observe):
            result = self.call()
        self.assertTrue(result.ok, result)
        self.assertGreaterEqual(json.loads(marker.read_text())["cpu"], .20)
        self.assertGreaterEqual(result.cpu_s, .30)
        self.report(result)
        self.assertEqual(before, self.inventory())
        self.assertEqual(during, [before])
        self.assertEqual(calls[0]["argv"][:4], ["/usr/bin/nice", "-n", str(hc.cpu_share.NICE_INCREMENT), "bash"])
        self.observed.append(dict(completion_cpu_s=result.cpu_s, wall_s=result.wall_s,
                                  seats_before=before, seats_during=list(during), seats_after=self.inventory(), calls=list(calls)))
        # Timeout grandchild burns CPU, writes its proof, then stays alive.
        grand += "; time.sleep(60)"
        child = ("import time,subprocess,sys; t=time.process_time(); "
                 "exec('while time.process_time()-t < .22: pass'); "
                 f"p=subprocess.Popen([sys.executable,'-c',{grand!r}]); p.wait()")
        marker.unlink()
        self.scanner("#!/bin/bash\nexec /usr/bin/python3 -c " + shlex.quote(child) + "\n")
        foreign = self.home / "foreign worktree"
        self.git("worktree", "add", "--detach", str(foreign), self.main)
        calls.clear(); during.clear()
        with patch.object(hc, "ATTEMPT_SECONDS", 2.0), \
             patch.dict(os.environ, DOIT_GATE_DEADLINE="5000"), \
             patch.object(mg.subprocess, "Popen", side_effect=observe):
            result = self.call()
        self.assertEqual(result.failed_check, "undetermined")
        self.assertIn("deadline", result.findings)
        self.assertLess(result.wall_s, 5)
        proof = json.loads(marker.read_text())
        self.assertGreaterEqual(proof["cpu"], .20)
        def alive(pid):
            stat = Path(f"/proc/{pid}/stat")
            return stat.exists() and stat.read_text().split()[2] != "Z"
        until = time.monotonic() + 2
        while alive(proof["pid"]) and time.monotonic() < until:
            time.sleep(.01)
        self.assertFalse(alive(proof["pid"]), "timeout grandchild survived")
        rows = self.report(result)
        self.assertTrue(any(r["type"] == "infra-failure" for r in rows))
        self.assertTrue(foreign.exists())
        lock = self.repo / ".git/doit-secret-scan-locks" / (self.git("rev-parse", "HEAD") + ".lock")
        inode = lock.stat().st_ino
        with lock.open("a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.scanner("exit 0\n")
        self.assertTrue(self.call().ok)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(before, self.inventory())
        self.assertTrue(all(x == before for x in during))
        self.observed.append(dict(timeout_wall_s=result.wall_s, cpu_s=result.cpu_s,
                                  cpu_accounting=hc.CPU_ACCOUNTING, grandchild_stopped=True,
                                  foreign_worktree_retained=True, lock_reacquired=True,
                                  seats_during=list(during), calls=list(calls)))
        # A permitted internal retry wraps both Popen calls and shares one cap.
        retry_marker = self.home / "retry"
        self.scanner(f'if [ ! -e {shlex.quote(str(retry_marker))} ]; then touch {shlex.quote(str(retry_marker))}; sleep 60; fi\nexit 0\n')
        calls.clear()
        with patch.object(mg, "scan_deadline", return_value=4), \
             patch.object(hc, "ATTEMPT_SECONDS", 8), \
             patch.object(mg.subprocess, "Popen", side_effect=observe):
            result = self.call()
        self.assertTrue(result.ok, result)
        self.assertEqual(len(calls), 2)
        self.report(result)
        self.observed.append(dict(retry_calls=list(calls), cpu_accounting=hc.CPU_ACCOUNTING))
        # Setup itself consumes the original cap, even across load_config.
        real_load = mg.load_config
        def slow_load():
            real_load(); time.sleep(.08)
        with patch.object(hc, "ATTEMPT_SECONDS", .03), patch.object(mg, "load_config", side_effect=slow_load):
            self.assertEqual(self.call().failed_check, "undetermined")
        self.evidence()

    def test_event_round_trip(self):
        default = fold.ROOT
        fold.append(["fixture", "before", "project=do-it-v2"])
        def counts():
            return {p.name: len(p.read_bytes().splitlines()) for p in (default / "events").glob("*.jsonl")}
        before = counts()
        for expected in ("clean", "rework", "timeout", "tool", "preflight"):
            if expected == "rework":
                (self.repo / "fixture").write_text("FIND\n"); self.commit("finding")
            elif expected == "timeout":
                self.scanner("sleep 60\n")
            elif expected == "tool":
                self.scanner("echo 'No space left on device' >&2; exit 2\n")
            with contextlib.ExitStack() as stack:
                if expected == "timeout":
                    stack.enter_context(patch.object(hc, "ATTEMPT_SECONDS", .4))
                if expected == "tool":
                    stack.enter_context(patch.dict(os.environ, DOIT_GATE_LEDGER_FILE="L-executor-explicit.jsonl"))
                result = self.call(worktree=self.home / "absent" if expected == "preflight" else self.repo)
            rows = self.report(result, sha=expected != "preflight")
            self.assertEqual(counts(), before)
            self.assertEqual(fold.ROOT, default)
            self.assertEqual(os.environ["DOIT_ROOT"], str(default))
            self.observed.append(dict(case=expected, failed_check=result.failed_check, events=len(rows)))
        rows = self.events()
        self.assertEqual(len([r for r in rows if r["type"] == "handin-checked"]), 5)
        self.assertEqual(len([r for r in rows if r["type"] == "resource-created"]), 4)
        self.assertEqual(len([r for r in rows if r["type"] == "infra-failure"]), 2)
        self.assertTrue((self.root / "events/L-executor-0001.jsonl").exists())
        self.assertTrue((self.root / "events/L-executor-explicit.jsonl").exists())
        fold.append(["fixture", "after", "project=do-it-v2"])
        self.assertEqual([r["subject"] for r in self.events(default)], ["before", "after"])
        blocked = self.home / "blocked"; blocked.write_text("not a directory")
        result = hc.run(self.repo, "L-spec-fixture", root=blocked)
        self.assertEqual(result.failed_check, "undetermined")
        self.assertIn("ledger append failed", str(result.findings))
        self.assertEqual(counts()["L-builder-fixture.jsonl"], 2)
        self.evidence(default_root_zero_delta=True, restored_after_success_and_error=True)

    def test_cli(self):
        sentinel = self.home / "injected"
        subject = "L-spec-fixture; touch " + str(sentinel)
        env = dict(os.environ, DOIT_ROOT=str(self.root))
        def cli(*args):
            return subprocess.run([str(REPO / "doit"), "handin-check", *args],
                                  cwd=self.home, env=env, capture_output=True, text=True)
        for mode, expected in (("clean", 0), ("finding", 1), ("undetermined", 2)):
            if mode == "finding":
                (self.repo / "fixture").write_text("FIND\n"); self.commit("finding")
            p = cli(subject, "--cwd", str(self.home / "absent" if expected == 2 else self.repo))
            self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
            self.observed.append(dict(case=mode, exit=p.returncode, output=p.stdout.strip()))
        rows = self.events()
        self.assertEqual([r["subject"] for r in rows if r["type"] == "handin-checked"], [subject] * 3)
        self.assertEqual({r["type"] for r in rows}, {"handin-checked", "resource-created"})
        self.assertFalse(sentinel.exists())
        self.assertIn("committed HEAD", cli("--help").stdout)
        self.assertEqual(cli().returncode, 2)
        self.assertEqual(cli("x", "--cwd", str(self.repo), "--invalid").returncode, 2)
        self.assertEqual(len(rows), len(self.events()))
        help_result = subprocess.run([str(REPO / "doit"), "help"], capture_output=True, text=True)
        self.assertIn("handin-check", help_result.stdout)
        self.evidence()

    def test_gate_compatibility(self):
        real_popen = subprocess.Popen
        for override, expected in ((None, 120.0), ("150", 150.0)):
            captures = []
            def observe(argv, **kw):
                if argv[:1] == ["bash"]:
                    captures.append((argv, mg.DEADLINE))
                return real_popen(argv, **kw)
            with contextlib.ExitStack() as stack:
                stack.enter_context(hc._ledger_root(self.root))
                stack.enter_context(hc._scanner_state())
                if override is not None:
                    stack.enter_context(patch.dict(os.environ, DOIT_GATE_DEADLINE=override))
                stack.enter_context(patch.object(mg.subprocess, "Popen", side_effect=observe))
                self.assertEqual(mg.secret_scan(self.repo, self.main, self.git("rev-parse", "HEAD")), ("clean", []))
            self.assertEqual(len(captures), 1)
            argv, deadline = captures[0]
            self.assertEqual(argv[0], "bash")
            self.assertEqual(argv[2:], ["scan", self.main, self.git("rev-parse", "HEAD")])
            self.assertEqual(deadline, expected)
            self.observed.append(dict(override=override, deadline=deadline, argv_shape=["bash", "<script>", "scan", "<main>", "<HEAD>"]))
        env = dict(os.environ)
        env.pop("DOIT_PROJECT", None)
        p = subprocess.run(["/usr/bin/python3", str(SRC / "test_merge_gate.py")],
                           cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-3000:])
        self.evidence(existing_gate_exit=p.returncode, existing_gate_last_line=p.stdout.strip().splitlines()[-1])

    def test_undetermined(self):
        cases = [("nonexistent", self.home / "absent"), ("non-repository", self.home)]
        unborn = self.home / "unborn"; unborn.mkdir()
        self.git("init", "-q", "-b", "main", repo=unborn)
        cases.append(("unborn", unborn))
        for name, path in cases:
            result = self.call(worktree=path)
            self.assertEqual(result.failed_check, "undetermined", (name, result))
            self.report(result, sha=False)
            self.observed.append(name)
        with patch.dict(os.environ, GIT_DIR=str(self.home / "evil")):
            self.assertEqual(self.call().failed_check, "undetermined")
        self.assertEqual(self.call(main="missing").failed_check, "undetermined")
        lock = self.repo / ".git/doit-secret-scan-locks" / (self.git("rev-parse", "HEAD") + ".lock")
        lock.parent.mkdir(exist_ok=True)
        with lock.open("a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.call()
            self.assertIn("lock-busy", result.findings)
        for response in (("clean", ["partial"]), ("rework", []), ("other", []), None,
                         ("undetermined", ["unknown"]), ("rework", ["raw data"])):
            with patch.object(mg, "secret_scan", return_value=response):
                self.assertEqual(self.call().failed_check, "undetermined", response)
        with patch.object(mg, "secret_scan", side_effect=RuntimeError("PRIVATE_DIAGNOSTIC")):
            result = self.call()
            self.assertEqual(result.failed_check, "undetermined")
            self.assertNotIn("PRIVATE_DIAGNOSTIC", str(result))
        for text, name in (("exit 1\n", "unrecognized exit 1"),
                           ('echo PRIVATE_DIAGNOSTIC >&2; exit 2\n', "tool")):
            self.scanner(text)
            result = self.call()
            self.assertEqual(result.failed_check, "undetermined")
            self.assertNotIn("PRIVATE_DIAGNOSTIC", str(result))
            self.observed.append(name)
        real_git = mg.git
        def broken(*args, **kw):
            if args[:2] == ("worktree", "add"):
                raise mg.Undetermined("synthetic worktree failure")
            return real_git(*args, **kw)
        with patch.object(mg, "git", side_effect=broken):
            self.assertEqual(self.call().failed_check, "undetermined")
        # No scanner on trusted main retains the gate's existing clean behavior.
        self.git("checkout", "main"); self.script.unlink(); self.commit("no scanner")
        self.git("checkout", "work")
        self.assertTrue(self.call().ok)
        self.observed += ["Git redirection", "missing base", "lock-busy", "malformed", "exception", "worktree"]
        self.evidence()


if __name__ == "__main__":
    unittest.main()
