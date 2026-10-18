#!/usr/bin/env python3
"""Offline regression and real local store round-trip checks for base_red."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Isolation precedes imports: even direct execution cannot select the live store.
_ISO = tempfile.TemporaryDirectory(prefix="base-red-tests-")
os.environ["DOIT_ROOT"] = _ISO.name
os.environ["HOME"] = str(pathlib.Path(_ISO.name) / "home")
import base_red as br

FIX = HERE / "fixtures" / "base_red"
KINDS = ("pytest", "pg-parity", "vitest", "suite-error")
STEP = "Run spec-839/895/926/949 scratch-database tests (pg_scratch)"


def archive(**members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


class BaseRedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="base-red-case-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / "repos" / "demo"
        self.repo.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()
        git("init", "-q")
        git("-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture", "--allow-empty")
        git("remote", "add", "origin", "git@github.com:example/demo.git")
        self.sha = git("rev-parse", "HEAD")
        self.calls = []
        self.runs = [self.run_doc(1, "Python Tests"), self.run_doc(2, "Dashboard PR Quality")]
        self.zips = {
            1: archive(**{"pytest.txt": (FIX / "pytest.log").read_text(),
                          "pg-parity (Postgres).txt": (FIX / "pg-parity.log").read_text()}),
            2: archive(**{"dashboard.txt": (FIX / "vitest.log").read_text()}),
        }
        self.gh_error = False
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(br.fold, "ROOT", self.root))
        stack.enter_context(patch.dict(os.environ, {"DOIT_ROOT": str(self.root),
                                                     "HOME": str(self.root / "home"),
                                                     "DOIT_LEDGER_FILE": "L-executor-0001.jsonl"}))
        stack.enter_context(patch.object(br.ghlimit, "run", self.gh))
        stack.enter_context(patch.object(br.ghlimit, "gate", return_value={"ok": True}))
        stack.enter_context(patch.object(br.ghlimit, "real_gh", return_value="base-red-fake-gh"))
        # Only binary gh is intercepted; git, ledger and CLI use real subprocesses.
        self.real_run = subprocess.run
        stack.enter_context(patch.object(br.subprocess, "run", self.binary))
        stack.enter_context(patch.object(br.window_gate, "verify", side_effect=AssertionError("unexpected dispatch")))

    def run_doc(self, rid, name, **kw):
        return dict(id=rid, name=name, head_sha=self.sha, status="completed",
                    conclusion="failure", html_url=f"https://github.com/example/demo/actions/runs/{rid}", **kw)

    def gh(self, argv):
        self.calls.append(argv)
        self.assertEqual(argv[:2], ["gh", "api"])
        self.assertNotIn("view", argv)
        if self.gh_error:
            return 1, "", "fixture error"
        if "/jobs?" in argv[2]:
            return 0, json.dumps([{"jobs": [{"name": "pg-parity (Postgres)", "conclusion": "failure",
                                              "steps": [{"name": STEP, "conclusion": "failure"}]}]}]), ""
        self.assertIn("actions/runs?", argv[2])
        self.assertIn("--paginate", argv)
        return 0, json.dumps([{"workflow_runs": self.runs}]), ""

    def binary(self, argv, **kw):
        if argv[0] != "base-red-fake-gh":
            return self.real_run(argv, **kw)
        self.calls.append(argv)
        self.assertEqual(argv[1], "api")
        self.assertFalse(kw.get("text", False), "zip bytes must never use text capture")
        self.assertRegex(argv[2], r"^repos/example/demo/actions/runs/\d+/logs$")
        rid = int(argv[2].split("/")[-2])
        return subprocess.CompletedProcess(argv, 0, self.zips[rid], b"")

    @property
    def cache(self):
        return self.root / "base-red" / "demo" / f"{self.sha}.json"

    def events(self):
        p = self.root / "events" / "L-base-red-local.jsonl"
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def expected(self):
        return {"pytest": ["tests/test_a.py::test_changed", "tests/test_a.py::test_error", "tests/test_a.py::test_failed"],
                "pg-parity": ["pg-parity::" + STEP],
                "vitest": ["dashboard/a.test.ts::describe > test"],
                "suite-error": ["dashboard/x.ts"]}

    def cli(self, sha=None):
        return subprocess.run([str(HERE.parent / "doit"), "base-red", "demo", sha or self.sha, "--json"],
                              capture_output=True, text=True, env=dict(os.environ), timeout=15)

    def test_parse_failed_error_changed_vitest_suite_error_pg_parity(self):
        actual = br.parse_log("\n".join(p.read_text() for p in sorted(FIX.glob("*.log"))))
        self.assertEqual(actual, {**{k: set(v) for k, v in self.expected().items()}, "undetermined": []})
        self.assertEqual(br.parse_log("nothing failed"), {**{k: set() for k in KINDS}, "undetermined": []})
        text = (FIX / "pg-parity.log").read_text().replace("scratch authority cleanup refused", "FAILED tests/pg.py::test_x - failure")
        parsed = br.parse_log(text)
        self.assertEqual(parsed["pytest"], {"tests/pg.py::test_x"})
        self.assertEqual(parsed["pg-parity"], set())

    def test_summary(self):
        def summary(new=0, changed=0):
            return f"SUMMARY pytest check: FAIL baseline=0 known=0 new={new} fixed=0 changed={changed}"
        self.assertEqual(br.parse_log(summary())["undetermined"], [])
        for text in (summary(new=1), summary(changed=1), "NEW error: tests/a.py::x\n" + summary(new=2)):
            reasons = br.parse_log(text)["undetermined"]
            self.assertIsInstance(reasons, list)
            self.assertTrue(reasons)
            self.assertTrue(all(isinstance(r, str) for r in reasons))
        self.assertEqual(br.parse_log("NEW error: tests/a.py::x\n" + summary(new=1))["undetermined"], [])
        # A named failure in a different gate cannot explain a missing id.
        self.assertTrue(br.parse_log("NEW failed: tests/a.py::x\nSUMMARY vitest check: FAIL new=1 fixed=0")["undetermined"])

    def test_failing_complete_no_run_view_zip_bytes(self):
        other = self.run_doc(3, "Python Tests")
        other["head_sha"] = "0" * 40
        self.runs.append(other)
        result = br.failing("demo", self.sha)
        self.assertTrue(result["complete"])
        self.assertEqual(result["ids"], self.expected())
        self.assertEqual(result["sources"], [r["html_url"] for r in self.runs[:2]])

    def test_zip_corrupt_and_undetermined_log(self):
        for content in (b"corrupt", archive(log="SUMMARY pytest check: FAIL new=1 changed=0")):
            with self.subTest(content=content[:10]):
                self.zips[1] = content
                r = br.failing("demo", self.sha)
                self.assertFalse(r["complete"])
                self.assertTrue(r["undetermined"])
                self.assertFalse(self.cache.exists())
                self.assertEqual(self.events(), [])

    def test_zip_corrupt_deflate_member(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("gate.log", "ordinary log text" * 100)
        data = bytearray(buf.getvalue())
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.infolist()[0]
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        data[offset] = 0xff  # DEFLATE's reserved block type, inside a valid zip.
        self.zips[1] = bytes(data)
        result = br.failing("demo", self.sha)
        self.assertFalse(result["complete"])
        self.assertTrue(result["undetermined"])
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.events(), [])

    def test_incomplete(self):
        for state in ("in_progress", "queued", "cancelled", "completed"):
            self.runs[1].update(status=state, conclusion="cancelled")
            r = br.failing("demo", self.sha)
            self.assertFalse(r["complete"])
            self.assertEqual(r["ids"]["pytest"], self.expected()["pytest"])
            self.assertFalse(self.cache.exists())
            self.assertEqual(self.events(), [])

    def test_roundtrip_foreign_ledger_env_short_and_full_sha_cli_exit_0(self):
        first = br.failing("demo", self.sha[:7])
        self.assertEqual(first["sha"], self.sha)
        self.assertEqual(json.loads(self.cache.read_text()), first)
        self.assertEqual(os.environ["DOIT_LEDGER_FILE"], "L-executor-0001.jsonl")
        self.assertFalse((self.root / "events" / "L-executor-0001.jsonl").exists())
        self.calls.clear()
        self.assertEqual(br.failing("demo", self.sha), first)
        self.assertEqual(self.calls, [])
        proc = self.cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), first)
        event, = self.events()
        self.assertEqual(event["type"], "base-red")
        self.assertEqual(event["sha"], self.sha)
        self.assertEqual(event["ids"], first["ids"])
        self.assertIs(event["complete"], True)
        self.assertNotIn("actor", event)

    def test_concurrent_append(self):
        barrier = threading.Barrier(2)
        results = []
        def call():
            barrier.wait(timeout=5)
            results.append(br.failing("demo", self.sha))
        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["complete"] for r in results))
        self.assertEqual(len(self.events()), 1)

    def test_ensure_once_concurrent(self):
        self.runs.clear()
        marker = self.cache.with_suffix(".dispatched")
        def verify(repo, sha, required, window):
            self.assertTrue(marker.exists())
            second = []
            t = threading.Thread(target=lambda: second.append(br.failing("demo", self.sha, ensure=True)))
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "second caller must not wait on verify")
            self.assertFalse(second[0]["complete"])
            self.runs[:] = [self.run_doc(1, "Python Tests"), self.run_doc(2, "Dashboard PR Quality")]
            return {"state": "red"}
        with patch.object(br.window_gate, "required_checks", return_value=["pytest"]) as checks, \
             patch.object(br.window_gate, "verify", side_effect=verify) as dispatch:
            self.assertTrue(br.failing("demo", self.sha, ensure=True)["complete"])
            self.assertTrue(br.failing("demo", self.sha, ensure=True)["complete"])
            dispatch.assert_called_once()
            checks.assert_called_once_with(self.repo, self.sha)

    def test_ensure_noop(self):
        self.assertTrue(br.failing("demo", self.sha, ensure=True)["complete"])
        self.assertFalse(self.cache.with_suffix(".dispatched").exists())
        br.window_gate.verify.assert_not_called()

    def test_ensure_verify_raises(self):
        self.runs.clear()
        with patch.object(br.window_gate, "required_checks", return_value=["pytest"]), \
             patch.object(br.window_gate, "verify", side_effect=RuntimeError("fixture")) as dispatch:
            r = br.failing("demo", self.sha, ensure=True)
            self.assertFalse(r["complete"])
            self.assertTrue(r["undetermined"])
            self.assertTrue(self.cache.with_suffix(".dispatched").exists())
            self.assertFalse(br.failing("demo", self.sha, ensure=True)["complete"])
            dispatch.assert_called_once()

    def test_classify_b10_and_incomplete(self):
        ids = ["A::describe > t1", "dashboard/x.ts", "B::describe > t2"]
        for complete in (True, False):
            base = {"complete": complete, "ids": {"vitest": ids[:1], "suite-error": ids[1:2]}}
            with patch.object(br, "failing", return_value=base):
                r = br.classify("demo", self.sha, ids)
                self.assertEqual(r, {"pre_existing": ids[:2] if complete else [],
                                     "new": ids[2:] if complete else ids, "complete": complete})
                self.assertEqual(br.classify("demo", self.sha, []), {"pre_existing": [], "new": [], "complete": complete})
        with patch.object(br, "failing", return_value={"complete": False, "ids": {}, "undetermined": ["fixture"]}):
            self.assertEqual(br.classify("demo", self.sha, ids)["new"], ids)

    def test_cli_exit_3_and_2(self):
        for runs, error, expected in (([], False, 3), ([], True, 2)):
            self.runs, self.gh_error = runs, error
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = br.main(["demo", self.sha, "--json"])
            self.assertEqual(rc, expected)
            self.assertFalse(json.loads(out.getvalue())["complete"])
            self.assertEqual(self.events(), [])
            self.assertFalse(self.cache.exists())
        proc = self.cli("0" * 40)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["undetermined"])
        self.gh_error = False
        self.runs = [self.run_doc(1, "Python Tests"), self.run_doc(2, "Dashboard PR Quality")]
        self.zips[1] = b"corrupt"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(br.main(["demo", self.sha, "--json"]), 2)
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
