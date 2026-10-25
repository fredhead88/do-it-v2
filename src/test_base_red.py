#!/usr/bin/env python3
"""Offline regression and real local store round-trip checks for base_red."""
import ast
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
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for filename in ("python-tests.yml", "dashboard-pr-quality.yml"):
            (workflows / filename).write_text("on: workflow_dispatch\n")
        git("add", ".github")
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
        if "--slurp" in argv:
            return 1, "", "unknown flag: --slurp"
        self.assertEqual(argv[:2], ["gh", "api"])
        self.assertNotIn("view", argv)
        if self.gh_error:
            return 1, "", "fixture error"
        if "/jobs?" in argv[2]:
            return 0, json.dumps({"jobs": [{"name": "pg-parity (Postgres)", "conclusion": "failure",
                                             "steps": [{"name": STEP, "conclusion": "failure"}]}]}), ""
        self.assertIn("actions/runs?", argv[2])
        self.assertIn("--paginate", argv)
        pages = [{"workflow_runs": [run]} for run in self.runs] or [{"workflow_runs": []}]
        return 0, "\n".join(json.dumps(page) for page in pages), ""

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

    def test_ac1_api_paginates_without_slurp(self):
        path = "repos/example/demo/actions/runs?per_page=100"
        rows = [{"id": 1}, {"id": 2}]
        for key in ("workflow_runs", "jobs"):
            for separator in ("\n", ""):
                with self.subTest(key=key, separator=separator):
                    output = separator.join(json.dumps({key: [row]}) for row in rows)
                    with patch.object(br.ghlimit, "run", return_value=(0, output, "")) as run:
                        self.assertEqual(br._api(path, key), rows)
                    argv = run.call_args.args[0]
                    self.assertEqual(argv, ["gh", "api", path, "--paginate"])
                    self.assertNotIn("--slurp", argv)
        with patch.object(br.ghlimit, "run", return_value=(0, ' \n{"jobs": []} \n', "")):
            self.assertEqual(br._api(path, "jobs"), [])

    def test_ac2_gh_flags_allowlisted(self):
        self.assertEqual(self.gh(["gh", "api", "fixture", "--slurp"]),
                         (1, "", "unknown flag: --slurp"))
        self.calls.clear()
        result = br.failing("demo", self.sha)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["ids"], self.expected())
        self.assertTrue(self.calls)
        self.assertTrue(any("/jobs?" in argv[2] for argv in self.calls))
        for argv in self.calls:
            with self.subTest(argv=argv):
                self.assertLessEqual({arg for arg in argv if arg.startswith("-")}, {"--paginate"})

    def test_ac3_no_slurp_in_gh_callers(self):
        for name in ("base_red.py", "ci_shadow.py"):
            with self.subTest(module=name):
                tree = ast.parse((HERE / name).read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn("--slurp", node.value, f"{name}:{node.lineno}")

    def assert_api_output_incomplete(self, output, rc=0):
        for jobs in (False, True):
            with self.subTest(jobs=jobs):
                def gh(argv):
                    if ("/jobs?" in argv[2]) == jobs:
                        self.calls.append(argv)
                        return rc, output, "fixture error" if rc else ""
                    return self.gh(argv)
                with patch.object(br.ghlimit, "run", side_effect=gh):
                    result = br.failing("demo", self.sha)
                self.assertFalse(result["complete"], result)
                self.assertTrue(result["undetermined"], result)
                self.assertEqual(result["ids"], {key: [] for key in KINDS})
                self.assertFalse(self.cache.exists())
                self.assertEqual(self.events(), [])

    def test_ac4_unreadable_api_output_is_incomplete_empty(self):
        self.assert_api_output_incomplete(" \n\t")

    def test_ac4_unreadable_api_output_is_incomplete_truncated(self):
        self.assert_api_output_incomplete('{"workflow_runs": [], "jobs": []}\n{"')

    def test_ac4_unreadable_api_output_is_incomplete_missing_key(self):
        self.assert_api_output_incomplete('{"workflow_runs": [], "jobs": []}{}')

    def test_ac4_unreadable_api_output_is_incomplete_non_list(self):
        for value in ({}, None, "", 1):
            with self.subTest(value=value):
                self.assert_api_output_incomplete(json.dumps({"workflow_runs": value, "jobs": value}))

    def test_ac4_unreadable_api_output_is_incomplete_nonzero_exit(self):
        self.assert_api_output_incomplete('{"workflow_runs": [], "jobs": []}', rc=1)

    def test_ac4_unreadable_api_output_is_incomplete_invalid_page_or_row(self):
        for output in ('[]', 'null', '{"workflow_runs": [null], "jobs": [null]}'):
            with self.subTest(output=output):
                self.assert_api_output_incomplete(output)

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

    def test_ac4_selected_never_evidence(self):
        self.zips[1] = archive(log="CI-SELECTION mode=selected reason=paths files=1\nNEW failed: tests/selected.py::x")
        result = br.failing("demo", self.sha)
        self.assertFalse(result["complete"])
        self.assertEqual(result["ids"]["pytest"], [])
        self.assertEqual(result["undetermined"], [])
        self.assertFalse(self.cache.exists())
        print("ac4 selection selected-not-evidence")

    def ledger_fixture(self, rows, filename="L-base-red-local.jsonl"):
        path = self.root / "events" / filename
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\nmalformed\n")
        return path

    def ledger_row(self, kind="base-red-nightly", at="2026-10-21T00:00:00Z", **kw):
        return dict(type=kind, project="demo", sha=self.sha, ts=at,
                    complete=True, ids=["nightly"], **kw)

    def test_ac1_newest_and_exclusions(self):
        old = self.ledger_row(at="2026-10-20T00:00:00Z")
        old["ids"] = ["old"]
        current = self.ledger_row()
        rerun = self.ledger_row("base-red", source="rerun")
        rerun.update(complete=False, ids=["rerun"])
        stale = dict(rerun, ts="2026-10-20T12:00:00Z", ids=["stale"])
        cases = [old, dict(current, complete=False, ids=["incomplete"]), stale,
                 dict(current, project="other", ids=["foreign"]),
                 dict(current, type="base-red", ids=["cached"]),
                 dict(current, ts="bad", ids=["bad-time"]),
                 dict(current, ids={"pytest": ["bad-shape"]})]
        for excluded in cases:
            with self.subTest(excluded=excluded):
                self.ledger_fixture([excluded, current, rerun])
                self.assertEqual(br.known_red("demo"), {
                    "nightly": {"sha": self.sha, "source": "nightly", "since": current["ts"]},
                    "rerun": {"sha": self.sha, "source": "rerun", "since": rerun["ts"]}})
        print("ac1 known_red newest-complete")
        print("ac1 known_red rerun-since")
        print("ac1 known_red exclusions")
        self.ledger_fixture([dict(current, ids=["intruder"])], "L-other-local.jsonl")
        self.assertNotIn("intruder", br.known_red("demo"))
        print("ac1 known_red other-ledger-ignored")
        path = self.root / "events" / "L-base-red-local.jsonl"
        path.unlink()
        self.assertEqual(br.known_red("demo"), {})
        path.mkdir()
        self.assertEqual(br.known_red("demo"), {})

    def fresh_read(self):
        code = """
import base_red, health_alarms, json
from datetime import datetime, timezone
from pathlib import Path
rows = [json.loads(s) for s in (base_red.fold.ROOT / 'events/L-base-red-local.jsonl').read_text().splitlines() if s.startswith('{')]
print(json.dumps({'known': base_red.known_red('demo'),
                  'health': health_alarms.summary_lines(rows, datetime.now(timezone.utc))}))
"""
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(HERE)}, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_ac2_record_roundtrip(self):
        ids = ["tests/a.py::broken", "dashboard/t.ts::case"]
        br.record("demo", self.sha, ids, "rerun")
        event, = self.events()
        self.assertIs(event["complete"], False)
        self.assertEqual(event["ids"], sorted(ids))
        self.assertNotIn("actor", event)
        read = self.fresh_read()
        self.assertEqual(set(read["known"]), set(ids))
        self.assertTrue(all(v["source"] == "rerun" for v in read["known"].values()))
        self.assertEqual(read["health"], [])
        self.runs = []
        self.assertFalse(br.failing("demo", self.sha)["complete"])
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(self.events()), 1)
        before = (self.root / "events/L-base-red-local.jsonl").read_bytes()
        for project, sha, source in (("demo", self.sha, "bad"), ("demo", "bad-sha", "rerun"),
                                     ("unknown", self.sha, "rerun"), ("../demo", self.sha, "rerun")):
            with self.assertRaises(ValueError):
                br.record(project, sha, ids, source)
        self.assertEqual((self.root / "events/L-base-red-local.jsonl").read_bytes(), before)
        br.record("demo", self.sha, [], "nightly")
        self.assertIs(self.events()[-1]["complete"], False)
        print("ac2 record roundtrip fresh-process")
        print("ac2 record never complete")
        print("ac2 record refuses bad source")

    def test_ac3_classification_and_freshness(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        ids = ["nightly", "base", "new"]
        for complete in (False, True):
            base = {"complete": complete, "ids": {"pytest": ["base"]}}
            for age in (0, 35.999, 36, 37, None):
                path = self.root / "events/L-base-red-local.jsonl"
                if age is None:
                    path.unlink(missing_ok=True)
                else:
                    self.ledger_fixture([self.ledger_row(at=(now - timedelta(hours=age)).isoformat())])
                with patch.object(br, "failing", return_value=base):
                    default = br.classify("demo", self.sha, ids)
                    self.assertEqual(default, {"pre_existing": ["base"] if complete else [],
                                               "new": ["nightly", "new"] if complete else ids,
                                               "complete": complete})
                    actual = br.classify("demo", self.sha, ids, known_red=True, now=now)
                    expected = (["nightly"] if age is not None else []) + (["base"] if complete else [])
                    self.assertEqual(actual["pre_existing"], expected)
                    self.assertEqual(actual["known_red"], ["nightly"] if age is not None else [])
                    self.assertEqual(actual["complete"], complete or (age is not None and age < 36))
        print("ac3 classify default unchanged")
        print("ac3 classify known_red")
        print("ac3 classify fresh nightly 36h")

    def test_ac4_selection_modes(self):
        for mode in ("selected", "rerun"):
            self.zips[1] = archive(log=f"2026-10-21T03:20:00.000Z CI-SELECTION mode={mode} reason=paths files=1\nNEW failed: tests/partial.py::x")
            result, _ = br._snapshot(self.repo, self.sha)
            self.assertFalse(result["complete"])
            self.assertEqual(result["undetermined"], [])
            self.assertEqual(result["ids"]["pytest"], [])
        print("ac4 selection rerun-not-evidence")
        self.runs.append(self.run_doc(3, "Python Tests"))
        self.zips[3] = archive(log="CI-SELECTION mode=full reason=nightly files=0\nNEW failed: tests/full.py::x")
        result = br.failing("demo", self.sha)
        self.assertTrue(result["complete"])
        self.assertEqual(result["ids"]["pytest"], ["tests/full.py::x"])
        self.assertEqual(json.loads(self.cache.read_text())["ids"]["pytest"], ["tests/full.py::x"])
        print("ac4 selection selected-plus-full-complete")

    def test_ac4_absent_and_garbled(self):
        original = br.window_gate.git
        for declared, readable, expected in ((False, True, "complete"), (True, True, "skipped"),
                                              (False, False, "undetermined")):
            def git(repo, *args, **kw):
                if args[0] == "show":
                    if not readable:
                        raise RuntimeError("fixture unreadable workflow")
                    return subprocess.CompletedProcess(args, 0, "select_base: {}" if declared else "on: push", "")
                return original(repo, *args, **kw)
            with patch.object(br.window_gate, "git", side_effect=git):
                result = br.failing("demo", self.sha)
            self.assertEqual(bool(result["undetermined"]), expected == "undetermined")
            self.assertEqual(result["complete"], expected == "complete")
            if self.cache.exists():
                self.cache.unlink()
        print("ac4 selection absent-declared-skipped")
        print("ac4 selection absent-undeclared-full")
        for text in ("CI-SELECTION mode=unknown reason=paths files=0",
                     "CI-SELECTION mode=full reason=paths files=0\nCI-SELECTION mode=rerun reason=paths files=0",
                     "CI-SELECTION mode=full"):
            self.zips[1] = archive(log=text)
            result = br.failing("demo", self.sha)
            self.assertTrue(result["undetermined"])
            self.assertFalse(self.cache.exists())
        print("ac4 selection garbled-undetermined")

    def nightly_fixture(self, *, finish=True):
        # Real local fetch; only the GitHub identity/listing/log/dispatch seam is faked.
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "master"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "set-url", "origin", str(self.repo)], check=True)
        self.dispatched = []
        def gh(argv, **kw):
            if argv[1:3] != ["workflow", "run"]:
                return self.gh(argv)
            self.dispatched.append(argv)
            filename = argv[3]
            name = {"python-tests.yml": "Python Tests", "dashboard-pr-quality.yml": "Dashboard PR Quality"}[filename]
            if finish:
                rid = 1 if name == "Python Tests" else 2
                self.runs.append(self.run_doc(rid, name))
                self.zips[rid] = archive(log="CI-SELECTION mode=full reason=nightly files=0\n" +
                                            "\n".join(p.read_text() for p in sorted(FIX.glob("*.log"))))
            return 0, "", ""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(br, "_owner", return_value="example/demo"))
        stack.enter_context(patch.object(br.ghlimit, "run", side_effect=gh))

    def test_ac5_nightly_complete_and_idempotent(self):
        self.nightly_fixture()
        self.runs = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(br.main(["nightly", "demo"]), 0)
        self.assertEqual(len(self.dispatched), 2)
        row, = self.events()
        self.assertEqual(row["type"], "base-red-nightly")
        self.assertIs(row["complete"], True)
        self.assertEqual(set(row["ids"]), {i for values in self.expected().values() for i in values})
        self.assertEqual(set(self.fresh_read()["known"]), set(row["ids"]))
        self.assertNotIn("actor", row)
        self.calls.clear()
        self.dispatched.clear()
        self.assertTrue(br.nightly("demo")["complete"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.dispatched, [])
        self.assertEqual(len(self.events()), 1)
        print("ac5 nightly complete")
        print("ac5 nightly idempotent")

    def test_ac5_dashboard_only_and_selected(self):
        self.nightly_fixture()
        self.runs = [self.run_doc(2, "Dashboard PR Quality")]
        result = br.nightly("demo")
        self.assertTrue(result["complete"])
        self.assertEqual([a[3] for a in self.dispatched], ["python-tests.yml"])
        self.assertIn("full_run=true", self.dispatched[0])
        print("ac5 nightly dashboard-only-tip dispatches python")
        (self.root / "events/L-base-red-local.jsonl").unlink()
        for path in (self.root / "base-red/demo").glob("*.dispatched"):
            path.unlink()
        self.dispatched.clear()
        self.runs = [self.run_doc(2, "Dashboard PR Quality"), self.run_doc(3, "Python Tests")]
        self.zips[3] = archive(log="CI-SELECTION mode=selected reason=paths files=1")
        self.assertTrue(br.nightly("demo")["complete"])
        self.assertEqual([a[3] for a in self.dispatched], ["python-tests.yml"])

    def test_ac5_incomplete(self):
        self.nightly_fixture(finish=False)
        self.runs = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(br.main(["nightly", "demo"]), 3)
        self.assertIn("missing", out.getvalue())
        self.assertIs(self.events()[0]["complete"], False)
        self.assertEqual(self.fresh_read()["known"], {})
        self.assertEqual(len(self.dispatched), 2)
        br.nightly("demo")
        self.assertEqual(len(self.dispatched), 2, "per-workflow markers prevent repeated paid dispatch")
        print("ac5 nightly incomplete")

    def test_ac5_undetermined_and_active(self):
        self.nightly_fixture()
        for status in ("queued", "in_progress"):
            self.runs = [dict(self.run_doc(1, "Python Tests"), status=status)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(br.main(["nightly", "demo"]), 2)
            self.assertEqual(self.events(), [])
            self.assertEqual(self.dispatched, [])
        print("ac5 nightly in-progress waits")
        self.runs = [self.run_doc(1, "Python Tests")]
        self.zips[1] = archive(log="CI-SELECTION mode=unknown reason=paths files=0")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(br.main(["nightly", "demo"]), 2)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.dispatched, [])
        with patch.object(br.ghlimit, "gate", return_value={"ok": False}):
            self.assertTrue(br.nightly("demo")["undetermined"])
        self.assertEqual(self.events(), [])
        print("ac5 nightly undetermined appends nothing")

    def test_ac6_cron(self):
        import crons
        rows = crons.rows()
        row, = [r for r in rows if r["name"] == "base-red-nightly"]
        self.assertEqual(row["where"], "user")
        self.assertEqual(row["schedule"], "20 3 * * *")
        self.assertEqual(row["path"], "/usr/bin/python3")
        self.assertEqual(row["owner"], "thinker")
        self.assertEqual(row["project"], "albert-scott")
        self.assertEqual(row["max_runtime_s"], 5400)
        self.assertEqual(row["command"], f"{HERE.parent}/doit cron-run base-red-nightly -- "
                         f"/usr/bin/python3 {HERE}/base_red.py nightly albert-scott "
                         f">> {self.root}/logs/base-red-nightly.log 2>&1")
        self.assertEqual([r["name"] for r in rows if br.re.search(row["sig"], r["command"])],
                         ["base-red-nightly"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
