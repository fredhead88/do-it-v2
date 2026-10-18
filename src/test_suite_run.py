#!/usr/bin/env python3
"""Hermetic suite-daily checks: local origins, scratch clones and real ledger IO."""
import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold
import suite_run

ENTRY = pathlib.Path(suite_run.__file__).resolve()


class SuiteRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="suite-run-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.origin = self.base / "origin"
        self.origin.mkdir()
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.git(self.origin, "init", "-q", "-b", "fixture-main")
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.look = self.root / "look.toml"
        self.look.write_text('[push.main]\ndo-it-v2 = "fixture-main"\n')
        self.pwd_file = self.base / "pwd"
        self.pid_file = self.base / "pid"
        self.env = patch.dict(os.environ, {
            "DOIT_ROOT": str(self.root), "TMPDIR": str(self.scratch),
            "DOIT_LEDGER_FILE": str(self.base / "must-not-write.jsonl"),
            "SUITE_TEST_PWD": str(self.pwd_file), "SUITE_TEST_PID": str(self.pid_file),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # tempfile caches its directory; exercise the same TMPDIR as a fresh process.
        self.old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.scratch)
        self.addCleanup(setattr, tempfile, "tempdir", self.old_tempdir)
        self.root_patch = patch.multiple(fold, ROOT=self.root, EVENTS=self.root / "events")
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    @staticmethod
    def git(repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def stub(self, body=None):
        if body is not None:
            doit = self.origin / "doit"
            doit.write_text('#!/bin/bash\n[ "$1" = test ] || exit 99\n'
                            'pwd > "$SUITE_TEST_PWD"\n' + body + "\n")
            doit.chmod(0o755)
        else:
            (self.origin / "README").write_text("no runner\n")
        self.git(self.origin, "add", ".")
        self.git(self.origin, "-c", "user.name=Suite Test", "-c", "user.email=suite@example.invalid",
                 "commit", "-qm", "fixture")
        return self.git(self.origin, "rev-parse", "HEAD")

    def events(self):
        path = suite_run.ledger_path()
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_main(self, timeout=5, subprocess_entry=False):
        before = sorted(self.scratch.iterdir())
        args = ["--repo", str(self.repo), "--timeout", str(timeout)]
        if subprocess_entry:
            result = subprocess.run([sys.executable, str(ENTRY), *args],
                                    capture_output=True, text=True, timeout=15)
            rc = result.returncode
        else:
            rc = suite_run.main(args)
        self.assertEqual(sorted(self.scratch.iterdir()), before, "AC4: scratch leaked")
        self.assertFalse((self.base / "must-not-write.jsonl").exists())
        return rc

    def test_AC1_green(self):
        sha = self.stub("echo PASS test_a.py\nexit 0")
        self.assertEqual(self.run_main(), 0)
        events = self.events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["type"], event["subject"], event["rc"], event["failed"], event["sha"]),
                         ("suite-run", "L-charter-0051", 0, [], sha))
        self.assertIsInstance(event["wall_s"], (int, float))
        self.assertGreaterEqual(event["wall_s"], 0)
        self.assertNotIn("actor", event)
        self.assertEqual(suite_run.ledger_path().name, "L-suite-daily-local.jsonl")
        self.assertEqual(fold.actor_of(suite_run.ledger_path().name), "suite-daily")
        clone = pathlib.Path(self.pwd_file.read_text().strip())
        self.assertTrue(clone.is_relative_to(self.scratch))
        self.assertNotIn(clone, (self.repo, self.repo.parent))
        print("AC1 ok; AC4 ok (pass: scratch count 0, cwd inside TMPDIR)")

    def test_AC2_failure(self):
        self.stub("echo 'FAIL test_a.py (exit 1)'\necho 'FAIL test_b.mjs (exit 2)'\n"
                  "echo '  diagnostic FAIL ignored'\necho 'FAIL not-a-runner-line'\nexit 1")
        self.assertNotEqual(self.run_main(), 0)
        self.assertEqual(self.events()[0]["rc"], 1)
        self.assertEqual(self.events()[0]["failed"], ["test_a.py", "test_b.mjs"])
        print("AC2 ok; AC4 ok (failure: scratch count 0)")

    def test_AC3_bad_origin(self):
        self.git(self.repo, "remote", "set-url", "origin", str(self.base / "absent"))
        self.assertNotEqual(self.run_main(), 0)
        self.assertEqual((self.events()[0]["rc"], self.events()[0]["sha"]), (125, "unresolved"))
        print("AC3 ok (clone failure)")

    def test_AC3_unresolved_branch(self):
        self.stub("exit 0")
        self.look.unlink()
        self.assertNotEqual(self.run_main(), 0)
        self.assertEqual((self.events()[0]["rc"], self.events()[0]["sha"]), (125, "unresolved"))
        print("AC3 ok (raw look absent and no canonical repo: unresolved)")

    def test_AC3_missing_doit(self):
        sha = self.stub()
        self.assertNotEqual(self.run_main(), 0)
        self.assertEqual((self.events()[0]["rc"], self.events()[0]["sha"]), (125, sha))
        print("AC3 ok (missing doit)")

    def test_AC3_timeout_group(self):
        self.stub('sleep 60 &\necho $! > "$SUITE_TEST_PID"\nwait')
        self.assertNotEqual(self.run_main(timeout=1, subprocess_entry=True), 0)
        self.assertEqual(self.events()[0]["rc"], 124)
        pid = int(self.pid_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("AC3: grandchild still exists after timeout")
        print("AC3 ok (timeout, grandchild pid dead); AC4 ok (timeout: scratch count 0)")

    def test_AC3_unwritable_ledger(self):
        self.stub("exit 0")
        fold.EVENTS.mkdir(mode=0o500)
        self.addCleanup(fold.EVENTS.chmod, 0o700)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.events(), [])
        self.assertIn("suite-run", stderr.getvalue())
        self.assertTrue(stderr.getvalue().strip())
        print("AC3 ok (unwritable ledger: exit 2, no event, stderr reason)")

    def test_AC3_emit_refusal(self):
        self.stub("exit 0")
        stderr = io.StringIO()
        with patch.object(suite_run.dispatch, "emit", return_value="refused by gate"), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(self.run_main(), 2)
        self.assertIn("refused by gate", stderr.getvalue())
        self.assertEqual(self.events(), [])
        print("AC3 ok (dispatch refusal)")

    def test_AC8_store_round_trip(self):
        sha = self.stub("echo PASS test_a.py\nexit 0")
        self.assertEqual(self.run_main(subprocess_entry=True), 0)
        code = ("import json,sys; sys.path.insert(0, sys.argv[1]); import fold; "
                "print(json.dumps(fold.read_events()))")
        result = subprocess.run([sys.executable, "-c", code, str(ENTRY.parent)],
                                capture_output=True, text=True, check=True, timeout=10)
        events = json.loads(result.stdout)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["sha"], event["rc"], event["failed"], event["actor"]),
                         (sha, 0, [], "suite-daily"))
        print("AC8 ok (separate production writer and fold reader processes)")

    def test_branch_override_and_no_write(self):
        sha = self.stub("exit 0")
        self.look.write_text('[push.main]\ndo-it-v2 = "nonexistent"\n')
        event = suite_run.run_suite(self.repo, branch="fixture-main")
        self.assertEqual((event["rc"], event["sha"]), (0, sha))
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        print("branch override ok; run_suite does not append")

    def test_branch_raw_live_root(self):
        self.stub("exit 0")
        # The source config must never override the explicit config under fold.ROOT.
        (self.repo / "look.toml").write_text('[push.main]\ndo-it-v2 = "wrong-source-branch"\n')
        self.assertEqual(self.run_main(), 0)
        print("branch from raw fold.ROOT/look.toml ok (non-default branch)")

    def test_branch_symbolic_ref_without_defaults(self):
        self.stub("exit 0")
        self.look.write_text('[push.main]\nother-project = "main"\n')
        canonical = self.root / "repos" / "do-it-v2"
        canonical.mkdir(parents=True)
        self.git(canonical, "init", "-q")
        self.git(canonical, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/fixture-main")
        self.assertEqual(self.run_main(), 0)
        print("branch fallback from canonical origin/HEAD ok (no merged DEFAULTS)")

    def test_no_source_origin(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.assertNotEqual(self.run_main(), 0)
        self.assertEqual(self.events()[0]["rc"], 125)
        print("AC3 ok (missing origin)")


if __name__ == "__main__":
    unittest.main()
