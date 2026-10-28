#!/usr/bin/env python3
"""Offline regression and real local store round-trip checks for base_red."""
import ast
import contextlib
import hashlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
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
        self.jobs = {
            1: [{"name": "pytest — shard api-1", "conclusion": "success"},
                {"name": "pytest — shard api-2", "conclusion": "success"},
                {"name": "pytest", "conclusion": "failure"},
                {"name": "pg-parity (Postgres)", "conclusion": "failure",
                 "steps": [{"name": STEP, "conclusion": "failure"}]}],
            2: [{"name": "TypeScript · Biome · Tests · File Sizes", "conclusion": "success"}],
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
            rid = int(argv[2].split("/")[-2])
            run = next(r for r in self.runs if r["id"] == rid)
            jobs = self.jobs.get(rid, self.jobs[1 if run["name"] == "Python Tests" else 2])
            return 0, json.dumps({"jobs": jobs}), ""
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

    def test_kind_ac2_classify_python_only(self):
        self.runs = self.runs[:1]
        self.zips[1] = archive(**{"0_pytest.txt":
            "NEW failed: A.py::t1\nSUMMARY pytest_baseline_gate.py check: FAIL new=1 changed=0"})
        self.jobs[1][-1]["steps"] = [{"name": "Run parity", "conclusion": "failure"}]
        nodes = ["A.py::t1", "B.py::t2", "pg-parity::Run parity"]
        result = br.classify("demo", self.sha, nodes)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["pre_existing"], [nodes[0], nodes[2]])
        self.assertEqual(result["new"], [nodes[1]])
        self.assertEqual(result["needed"], ["pg-parity", "pytest"])
        self.assertEqual(result["covered"], ["pg-parity", "pytest"])
        vitest = "dashboard/src/x.test.ts::renders"
        result = br.classify("demo", self.sha, nodes + [vitest])
        self.assertFalse(result["complete"])
        self.assertIn("vitest", result["needed"])
        self.assertNotIn("vitest", result["covered"])
        self.assertEqual(result["uncovered"], [vitest])
        self.jobs[1][-1]["conclusion"] = "skipped"
        self.assertFalse(br.classify("demo", self.sha, [nodes[2]])["complete"])
        self.assertTrue(br.classify("demo", self.sha, [nodes[0]])["complete"])
        self.runs = [self.run_doc(2, "Dashboard PR Quality")]
        self.assertTrue(br.classify("demo", self.sha, [vitest])["complete"])
        self.assertFalse(br.classify("demo", self.sha, [nodes[0]])["complete"])
        for node, kind in (("A.py::t", "pytest"), ("pg-parity::s", "pg-parity"),
                           ("x.test.ts::t", "vitest"), ("x.test.ts", "suite-error")):
            self.assertEqual(br.id_kind(node), kind)
        self.assertEqual(set(br.window_gate.BASE_RED_KINDS), set(br.KINDS))
        self.assertEqual(set(br.window_gate.BASE_RED_WORKFLOWS), br.WORKFLOWS)
        # The node's shape, rather than its source bucket, decides coverage.
        with patch.object(br, "failing", return_value={"complete": False,
                "ids": {"pytest": ["x.test.ts"]}, "kinds_covered": ["pytest"]}):
            self.assertEqual(br.classify("demo", self.sha, ["x.test.ts"])["pre_existing"], [])
        print("base-kind ac2 ok")

    def test_kind_ac1_snapshot_and_cache(self):
        python, dashboard = self.runs
        self.runs = [python]
        result = br.failing("demo", self.sha)
        self.assertFalse(result["complete"])
        self.assertEqual(result["workflows"], ["Python Tests"])
        self.assertEqual(result["kinds_covered"], ["pg-parity", "pytest"])
        self.assertEqual(result["undetermined"], [])
        self.assertFalse(self.cache.exists())
        self.runs.append(dashboard)
        result = br.failing("demo", self.sha)
        self.assertTrue(result["complete"])
        self.assertEqual(result["workflows"], sorted(br.WORKFLOWS))
        self.assertEqual(result["kinds_covered"], sorted(KINDS))
        # An independent interpreter opens the actual production-written file.
        def read_cache():
            proc = self.real_run([sys.executable, "-I", "-c",
                "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))", str(self.cache)],
                capture_output=True, text=True, check=True)
            return json.loads(proc.stdout)
        self.assertEqual(read_cache(), result)
        self.assertEqual(self.events()[0]["ids"], result["ids"])
        self.assertEqual(len(self.events()), 1)

        # A full-kind cache still short-circuits every API call.
        self.calls.clear()
        self.assertEqual(br.failing("demo", self.sha), result)
        self.assertEqual(self.calls, [])
        self.cache.unlink()
        self.jobs[1][-1]["conclusion"] = "skipped"
        result = br.failing("demo", self.sha)
        self.assertTrue(result["complete"])
        self.assertNotIn("pg-parity", result["kinds_covered"])
        rows = len(self.events())
        self.assertEqual(rows, 2)  # One row for each first-store fixture.
        self.calls.clear()
        self.assertEqual(br.failing("demo", self.sha), result)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("actions/runs?", self.calls[0][2])

        self.runs.append(self.run_doc(3, "Python Tests"))
        self.zips[3], self.jobs[3] = self.zips[1], self.jobs[1]
        result = br.failing("demo", self.sha)
        self.assertIn("3", result["runs_seen"])
        self.assertEqual(read_cache(), result)
        self.assertEqual(len(self.events()), rows)
        self.runs.append(self.run_doc(4, "Python Tests"))
        self.runs[-1]["conclusion"] = "cancelled"
        result = br.failing("demo", self.sha)
        self.assertIn("4", read_cache()["runs_seen"])
        self.calls.clear()
        self.assertEqual(br.failing("demo", self.sha), result)
        self.assertEqual(len(self.calls), 1)
        before = self.cache.read_bytes()
        self.runs.append(self.run_doc(5, "Python Tests"))
        self.zips[5] = b"unreadable"
        self.assertEqual(br.failing("demo", self.sha), result)
        self.assertEqual(self.cache.read_bytes(), before)
        with patch.object(br, "_api", side_effect=ValueError("fixture unavailable")):
            self.assertEqual(br.failing("demo", self.sha), result)
        self.zips[5] = archive(log="NEW failed: extra.py::t\nSUMMARY pytest_baseline_gate.py check: FAIL new=1 changed=0")
        self.jobs[5] = self.jobs[1]
        result = br.failing("demo", self.sha)
        self.assertIn("extra.py::t", read_cache()["ids"]["pytest"])
        self.assertEqual(len(self.events()), rows + 1)
        self.assertEqual(self.events()[-1]["ids"], result["ids"])
        self.runs.append(self.run_doc(6, "Python Tests"))
        self.zips[6] = archive(log="pg parity passed")
        self.jobs[6] = [{"name": "pg-parity (Postgres)", "conclusion": "success"}]
        result = br.failing("demo", self.sha)
        self.assertEqual(result["kinds_covered"], sorted(KINDS))
        self.assertEqual(read_cache(), result)
        self.assertEqual(len(self.events()), rows + 1)

        # A legacy file is refreshed without another ledger row, or fails closed.
        for missing in (("kinds_covered",), ("workflows",)):
            legacy = {k: v for k, v in result.items() if k not in missing}
            self.cache.write_text(json.dumps(legacy))
            result = br.failing("demo", self.sha)
            self.assertEqual(read_cache(), result)
            self.assertIn("kinds_covered", result)
            self.assertIn("workflows", result)
            self.assertEqual(len(self.events()), rows + 1)
        legacy = {k: v for k, v in result.items() if k != "kinds_covered"}
        self.cache.write_text(json.dumps(legacy))
        before = self.cache.read_bytes()
        self.runs = [python]
        self.assertFalse(br.failing("demo", self.sha)["complete"])
        self.assertEqual(self.cache.read_bytes(), before)
        self.cache.unlink()
        self.runs = [python, dashboard, self.run_doc(6, "", path="BuildFailed")]
        self.runs[-1]["conclusion"] = "startup_failure"
        self.assertTrue(br.failing("demo", self.sha)["complete"])
        self.cache.unlink()
        self.runs[-1]["path"] = ".github/workflows/python-tests.yml@master"
        result = br.failing("demo", self.sha)
        self.assertFalse(result["complete"])
        self.assertTrue(result["undetermined"])
        print("base-kind ac1 ok")

    def test_kind_ac3_unknown_stays_unknown(self):
        python, dashboard = self.runs
        default_jobs = json.dumps(self.jobs)
        default_zips = dict(self.zips)
        cases = ("selected", "cancelled", "running", "other-sha", "logs", "dashboard-unknown",
                 "api", "shards-skipped", "shard-cancelled", "aggregator-skipped",
                 "aggregator-cancelled", "no-summary", "shard-timeout", "missing-shards",
                 "missing-aggregator", "dashboard-api")
        for case in cases:
            with self.subTest(case=case):
                self.cache.unlink(missing_ok=True)
                self.runs = [dict(python)]
                self.jobs = {int(k): v for k, v in json.loads(default_jobs).items()}
                self.zips = dict(default_zips)
                self.gh_error = case == "api"
                if case == "selected":
                    self.zips[1] = archive(log="CI-SELECTION mode=selected reason=paths files=1")
                elif case == "cancelled":
                    self.runs[0]["conclusion"] = "cancelled"
                elif case == "running":
                    self.runs[0]["status"] = "in_progress"
                elif case == "other-sha":
                    self.runs[0]["head_sha"] = "0" * 40
                elif case == "logs":
                    self.zips[1] = b"unreadable"
                elif case == "dashboard-unknown":
                    self.runs.append(dashboard)
                    self.zips[2] = archive(log="SUMMARY vitest_baseline_gate.mjs check: FAIL new=1")
                elif case == "dashboard-api":
                    self.runs.append(dashboard)
                    self.jobs[2] = None
                elif case == "shards-skipped":
                    for job in self.jobs[1][:2]:
                        job["conclusion"] = "skipped"
                elif case.startswith("shard-"):
                    self.jobs[1][0]["conclusion"] = "cancelled" if case.endswith("cancelled") else "timed_out"
                elif case.startswith("aggregator-"):
                    self.jobs[1][2]["conclusion"] = case.split("-")[1]
                elif case == "no-summary":
                    self.zips[1] = archive(log="NEW failed: A.py::t1")
                elif case == "missing-shards":
                    self.jobs[1] = self.jobs[1][2:]
                elif case == "missing-aggregator":
                    self.jobs[1].pop(2)
                result = br.classify("demo", self.sha, ["tests/test_a.py::test_failed"])
                self.assertFalse(result["complete"], result)
                self.assertEqual(result["pre_existing"], [])
        self.gh_error = False
        for case in ("skipped", "no-summary"):
            self.cache.unlink(missing_ok=True)
            self.runs = [dashboard]
            self.jobs = {int(k): v for k, v in json.loads(default_jobs).items()}
            self.zips = dict(default_zips)
            if case == "skipped":
                self.jobs[2][0]["conclusion"] = "skipped"
            else:
                self.zips[2] = archive(log="tree already verified")
            result = br.classify("demo", self.sha, ["dashboard/a.test.ts::describe > test"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["pre_existing"], [])
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.ledger_fixture([self.ledger_row()])
        self.assertTrue(br.classify("demo", self.sha, ["A.py::t"], known_red=True, now=now)["complete"])
        print("base-kind ac3 ok")

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
            covered = ["suite-error", "vitest"] if complete else []
            base = {"complete": complete, "ids": {"vitest": ids[:1], "suite-error": ids[1:2]},
                    "kinds_covered": covered}
            with patch.object(br, "failing", return_value=base):
                r = br.classify("demo", self.sha, ids)
                self.assertEqual(r, {"pre_existing": ids[:2] if complete else [],
                                     "new": ids[2:] if complete else ids, "complete": complete,
                                     "needed": ["suite-error", "vitest"], "covered": covered,
                                     "uncovered": [] if complete else ids, "undetermined": False})
                self.assertEqual(br.classify("demo", self.sha, []), {"pre_existing": [], "new": [], "complete": complete,
                    "needed": [], "covered": covered, "uncovered": [], "undetermined": False})
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
            covered = ["suite-error"] if complete else []
            base = {"complete": complete, "ids": {"pytest": ["base"]}, "kinds_covered": covered}
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
                                               "complete": complete, "needed": ["suite-error"],
                                               "covered": covered, "uncovered": [] if complete else ids,
                                               "undetermined": False})
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
        # Real local fetch; only GitHub identity and gh are faked here.
        subprocess.run(['git', '-C', str(self.repo), 'branch', '-M', 'master'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'set-url', 'origin', str(self.repo)], check=True)
        self.dispatched = []
        self.pending_runs = []
        self.listings = 0
        self.appear_after = 0 if finish else 1000
        self.now = datetime.now(timezone.utc)
        def gh(argv, **kw):
            if argv[1:3] != ['workflow', 'run']:
                if 'actions/runs?' in argv[2]:
                    self.listings += 1
                    if self.listings > self.appear_after:
                        self.runs.extend(self.pending_runs)
                        self.pending_runs.clear()
                return self.gh(argv)
            self.dispatched.append(argv)
            name = {'python-tests.yml': 'Python Tests',
                    'dashboard-pr-quality.yml': 'Dashboard PR Quality'}[argv[3]]
            rid = 100 + len(self.dispatched)
            self.pending_runs.append(self.run_doc(rid, name, event='workflow_dispatch',
                head_branch='master', created_at=(self.now + timedelta(seconds=1)).isoformat()))
            self.zips[rid] = archive(log='CI-SELECTION mode=full reason=nightly files=0\n' +
                '\n'.join(p.read_text() for p in sorted(FIX.glob('*.log'))))
            return 0, '', ''
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(br, '_owner', return_value='example/demo'))
        stack.enter_context(patch.object(br.ghlimit, 'run', side_effect=gh))
        stack.enter_context(patch.object(br.time, 'sleep', return_value=None))

    def nightly_mark(self, number):
        body = f'nightly-ac{number} ok'
        print(body)
        print(json.dumps(dict(url=f'cli://doit-v2/nightly/AC{number}', status=0,
              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_ac5_nightly_complete_and_idempotent(self):
        self.nightly_fixture()
        self.runs = []
        self.assertFalse(br.nightly('demo')['undetermined'])
        self.assertEqual(len(self.dispatched), 2)
        self.assertTrue(br.nightly_collect('demo')['complete'])
        rows = self.events()
        nightly, = [e for e in rows if e['type'] == 'base-red-nightly']
        self.assertEqual(set(nightly['ids']), {i for values in self.expected().values() for i in values})
        self.assertEqual(set(self.fresh_read()['known']), set(nightly['ids']))
        self.assertTrue(all('actor' not in r for r in rows))
        self.assertFalse(br.nightly('demo')['undetermined'])
        self.assertEqual(len(self.dispatched), 2)
        self.assertEqual(br.nightly_collect('demo')['message'], 'already collected')
        self.assertEqual(self.events(), rows)

    def test_ac5_dashboard_only_and_selected(self):
        self.nightly_fixture()
        self.runs = [self.run_doc(2, 'Dashboard PR Quality'), self.run_doc(3, 'Python Tests')]
        self.zips[3] = archive(log='CI-SELECTION mode=selected reason=paths files=1')
        self.assertFalse(br.nightly('demo')['undetermined'])
        self.assertEqual(len(self.dispatched), 2)
        self.assertTrue(br.nightly_collect('demo')['complete'])
        python, = [a for a in self.dispatched if a[3] == 'python-tests.yml']
        self.assertIn('full_run=true', python)
        self.assertNotIn('suite=nightly', python)

    def test_ac5_incomplete(self):
        self.nightly_fixture(finish=False)
        self.runs = []
        self.assertTrue(br.nightly('demo')['undetermined'])
        self.assertEqual(self.events(), [])
        self.assertEqual(br.known_red('demo'), {})
        br.nightly('demo')
        self.assertEqual(len(self.dispatched), 2)
        self.appear_after = 0
        self.assertTrue(br.nightly_collect('demo')['complete'])

    def test_ac5_undetermined_and_active(self):
        self.nightly_fixture()
        br.nightly('demo')
        original = list(self.runs)
        for status in ('queued', 'in_progress'):
            self.runs = [dict(r, status=status) for r in original if r['id'] >= 100]
            self.assertTrue(br.nightly_collect('demo')['waiting'])
        self.runs = original
        for r in self.runs:
            if r['id'] >= 100:
                self.zips[r['id']] = archive(log='CI-SELECTION mode=unknown reason=paths files=0')
        self.assertTrue(br.nightly_collect('demo')['undetermined'])
        self.assertEqual([e['type'] for e in self.events()], ['base-red-nightly-dispatched'])

    def test_nightly_ac1_late_and_pending(self):
        self.nightly_fixture(finish=False)
        self.runs = []
        self.assertTrue(br.nightly('demo')['undetermined'])
        self.assertEqual(self.listings, 8)  # one pre-list per workflow, six polls
        markers = list((self.root / 'base-red/demo').glob('*.pending.json'))
        self.assertEqual(len(markers), 2)
        for p in markers:
            marker = json.loads(p.read_text())
            self.assertEqual(marker['sha'], self.sha)
            self.assertIn('dispatch_time', marker)
            self.assertIn('before_ids', marker)
        br.nightly('demo')
        self.assertEqual(len(self.dispatched), 2)
        self.appear_after = 0
        self.assertFalse(br.nightly('demo')['undetermined'])
        row, = self.events()
        self.assertEqual(row['type'], 'base-red-nightly-dispatched')
        self.assertEqual(len(row['run_ids']), 2)
        self.assertEqual(row['suite'], 'undeclared')
        self.assertEqual(row['ids'], [])
        self.assertIs(row['complete'], False)
        br.nightly('demo')
        self.assertEqual(len(self.events()), 1)
        self.nightly_mark(1)

    def test_nightly_ac1_second_poll_suite_and_expiry(self):
        self.nightly_fixture()
        self.runs = []
        self.appear_after = 3
        original = br.window_gate.git
        source = 'on:\n  workflow_dispatch:\n    inputs:\n      suite:\n        default: full\n'
        def git(repo, *args, **kw):
            if args[0] == 'show':
                return subprocess.CompletedProcess(args, 0, source, '')
            return original(repo, *args, **kw)
        with patch.object(br.window_gate, 'git', side_effect=git):
            self.assertFalse(br.nightly('demo')['undetermined'])
        self.assertEqual(self.listings, 4)
        for argv in self.dispatched:
            self.assertEqual('suite=nightly' in argv, argv[3] == 'python-tests.yml')
        self.assertEqual(self.events()[0]['suite'], 'nightly')
        # A new sha with an expired marker must be retired before any retry.
        directory = self.root / 'base-red/demo'
        marker = directory / 'expired.pending.json'
        marker.write_text(json.dumps(dict(sha='a'*40, workflow='Python Tests',
            dispatch_time=(self.now-timedelta(hours=7)).isoformat(), before_ids=[], suite='nightly')))
        result = br.nightly_collect('demo')
        self.assertIn('dispatch never appeared', ' '.join(result['undetermined']))
        self.assertFalse(marker.exists())
        self.assertTrue(list(directory.glob('expired.pending.json.*')))
        self.nightly_mark(1)

    def test_nightly_ac1_unreadable_and_failed_dispatch(self):
        self.nightly_fixture()
        original = br.window_gate.git
        def git(repo, *args, **kw):
            if args[0] == 'show':
                raise RuntimeError('unreadable')
            return original(repo, *args, **kw)
        with patch.object(br.window_gate, 'git', side_effect=git):
            self.assertTrue(br.nightly('demo')['undetermined'])
        self.assertEqual(self.dispatched, [])
        with patch.object(br.ghlimit, 'run', return_value=(1, '', '')):
            self.assertTrue(br.nightly('demo')['undetermined'])
        self.assertEqual(list((self.root / 'base-red/demo').glob('*.pending.json')), [])
        self.nightly_mark(1)

    def collect_fixture(self, suite='nightly'):
        br._append('base-red-nightly-dispatched', 'demo', self.sha, complete=False,
                   ids=[], run_ids=['1', '2'], suite=suite)

    def test_nightly_ac2_pinned_and_quiet_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(br.main(['nightly-collect', 'demo']), 0)
        self.assertIn('no nightly dispatched', output.getvalue())
        self.collect_fixture('undeclared')
        original = br.window_gate.git
        def git(repo, *args, **kw):
            self.assertNotEqual(args[0], 'fetch')
            self.assertNotIn('origin/master', args)
            return original(repo, *args, **kw)
        self.runs.append(self.run_doc(99, 'Python Tests'))  # logs deliberately absent
        with patch.object(br.window_gate, 'git', side_effect=git):
            self.runs[0]['status'] = 'in_progress'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(br.main(['nightly-collect', 'demo']), 3)
            self.assertEqual(len(self.events()), 1)
            self.runs[0]['status'] = 'completed'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(br.main(['nightly-collect', 'demo']), 0)
        verdict = self.events()[-1]
        self.assertTrue(verdict['complete'])
        self.assertEqual(verdict['suite'], 'undeclared')
        self.assertTrue(any(e['type'] == 'nightly-red' for e in self.events()))
        count = len(self.events())
        self.assertEqual(br.nightly_collect('demo')['message'], 'already collected')
        self.assertEqual(len(self.events()), count)
        self.nightly_mark(2)

    def test_nightly_ac2_undeclared_green(self):
        import merge_queue
        br._append('nightly-red', 'demo', 'a' * 40, subject='nightly:demo',
                   complete=True, ids=['old'], url='fixture')
        self.collect_fixture('undeclared')
        self.zips = {i: archive(log='CI-SELECTION mode=full reason=nightly files=0\nSUMMARY pytest check: PASS new=0 changed=0') for i in (1, 2)}
        with patch.object(br, '_api', wraps=br._api):
            # Jobs, too, must be green; a failed pg-parity job is a real red.
            original = self.gh
            def gh(argv):
                if '/jobs?' in argv[2]: return 0, '{"jobs": []}', ''
                return original(argv)
            with patch.object(br.ghlimit, 'run', side_effect=gh):
                self.assertTrue(br.nightly_collect('demo')['complete'])
        self.assertEqual(self.events()[-1]['suite'], 'undeclared')
        self.assertEqual(self.events()[-1]['ids'], [])
        self.assertIsNone(merge_queue.merge_freeze(self.events(), 'demo'))
        self.nightly_mark(2)

    def test_nightly_ac3_crash_safe_alarm(self):
        self.collect_fixture()
        append = br._append
        for crash_kind in ('nightly-red', 'escalation-blocking'):
            path = self.root / 'events/L-base-red-local.jsonl'
            path.write_text('')
            self.collect_fixture()
            crashed = False
            def crashing(kind, *args, **kwargs):
                nonlocal crashed
                append(kind, *args, **kwargs)
                if kind == crash_kind and not crashed:
                    crashed = True
                    raise RuntimeError('crash')
            with patch.object(br, '_append', side_effect=crashing):
                self.assertTrue(br.nightly_collect('demo')['undetermined'])
            self.assertTrue(br.nightly_collect('demo')['complete'])
            rows = self.events()
            self.assertEqual([r['type'] for r in rows], ['base-red-nightly-dispatched',
                'nightly-red', 'escalation-blocking', 'base-red-nightly'])
            alarm = rows[2]
            self.assertTrue(br.fold.escalation_ok(alarm))
            gap = br._time(alarm['deadline']) - br._time(rows[-1]['ts'])
            self.assertLess(abs(gap.total_seconds() - 86400), 10)
            self.assertIn('integrator', alarm['default'])
            self.assertTrue(all('actor' not in row for row in rows))
        path.write_text('')
        br._append('base-red-nightly', 'demo', 'a' * 40, complete=True,
                   ids=sorted({i for v in self.expected().values() for i in v}), url='fixture')
        self.collect_fixture()
        self.assertTrue(br.nightly_collect('demo')['complete'])
        self.assertFalse(any(e['type'] == 'nightly-red' for e in self.events()))
        self.nightly_mark(3)

    def test_nightly_ac6_admission(self):
        import fold_emits
        rows = {'base-red-nightly-dispatched': ('base-red', dict(sha=self.sha, run_ids=['1'])),
                'nightly-red': ('base-red', dict(sha=self.sha, ids=['A'], url='fixture')),
                'nightly-red-owned': ('operator', dict(ids=['A'], owner='quarantine')),
                'merge-frozen': ('executor', dict(project='demo', reason='A'))}
        for kind, (actor, fields) in rows.items():
            row = dict(type=kind, **fields)
            self.assertIsNone(br.fold.check_append(row, actor))
            for field in fold_emits.REQUIRED_ROWS[kind]:
                self.assertIsNotNone(br.fold.check_append({k:v for k,v in row.items() if k != field}, actor))
        for actor in ('builder', 'grader', 'reviewer'):
            self.assertIsNotNone(br.fold.admits(dict(type='nightly-red-owned'), actor))
        self.nightly_mark(6)

    def test_nightly_ac8_real_store_fresh_reader(self):
        self.collect_fixture()
        # This process inherits no Python mocks. Only the gh executable is fake.
        fixture = self.root / 'gh-fixture'
        fixture.mkdir()
        (fixture / 'runs.json').write_text(json.dumps({'workflow_runs': self.runs}))
        for rid, data in self.zips.items():
            (fixture / f'{rid}.zip').write_bytes(data)
        fake = fixture / 'gh'
        fake.write_text("""#!/usr/bin/python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
path = sys.argv[2]
if path == 'rate_limit':
    print(json.dumps({'resources': {k: dict(limit=5000, remaining=5000, reset=time.time()+3600) for k in ('core', 'graphql')}}))
elif '/logs' in path:
    sys.stdout.buffer.write((root / (path.split('/')[-2]+'.zip')).read_bytes())
elif '/jobs?' in path:
    print(json.dumps({'jobs': []}))
else:
    print((root / 'runs.json').read_text())
""")
        fake.chmod(0o700)
        proc = self.real_run(['/usr/bin/python3', str(HERE / 'base_red.py'), 'nightly-collect', 'demo'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'DOIT_REAL_GH': str(fake), 'GH_LIMIT_STATE': str(fixture / 'quota.json')})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([e['type'] for e in self.events()], ['base-red-nightly-dispatched',
                         'nightly-red', 'escalation-blocking', 'base-red-nightly'])

        code = "import fold,merge_queue,json; print(json.dumps(merge_queue.merge_freeze(fold.read_events(use_cache=False), 'demo')))"
        def freeze():
            proc = self.real_run([sys.executable, '-c', code], capture_output=True, text=True,
                env={**os.environ, 'PYTHONPATH': str(HERE)}, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        self.assertIsNotNone(freeze())
        verdict = self.events()[-1]
        ids = verdict['ids']
        # Ledger timestamps have second precision; ownership must be strictly later.
        delay = (br._time(verdict['ts']) + timedelta(seconds=1) - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0, delay) + 0.01)
        result = self.real_run([str(br.DOIT), 'append', 'nightly-red-owned', 'nightly:demo',
            'ids:=' + json.dumps(ids), 'owner=quarantine'], capture_output=True, text=True,
            env={**os.environ, 'DOIT_LEDGER_FILE': 'L-operator-local.jsonl'}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(freeze())
        self.nightly_mark(8)

    def test_nightly_ac9_undetermined(self):
        self.collect_fixture()
        row = self.events()[0]
        original = [dict(r) for r in self.runs]
        for case in ('api', 'sha', 'logs', 'cancelled', 'overdue', 'malformed', 'partial'):
            self.runs = [dict(r) for r in original]
            event = dict(row)
            self.gh_error = case == 'api'
            if case == 'sha': self.runs[0]['head_sha'] = 'a' * 40
            if case == 'cancelled': self.runs[0]['conclusion'] = 'cancelled'
            if case == 'overdue':
                self.runs[0]['status'] = 'in_progress'
                event['ts'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            if case == 'malformed': event['run_ids'] = 'bad'
            red = dict(type='nightly-red', project='demo', subject='nightly:demo',
                       sha=self.sha, ts=row['ts'], ids=['old'], complete=True, url='fixture')
            self.ledger_fixture([red, event])
            original_zip = self.zips[1]
            if case == 'logs': self.zips[1] = b'broken'
            if case == 'partial': self.zips[1] = archive(log='CI-SELECTION mode=selected reason=paths files=1')
            result = br.nightly_collect('demo')
            self.zips[1] = original_zip
            self.assertTrue(result['undetermined'], (case, result))
            if case == 'overdue': self.assertIn('overdue', ' '.join(result['undetermined']))
            self.assertEqual(len(self.events_valid()), 2)
            import merge_queue
            self.assertIsNotNone(merge_queue.merge_freeze(self.events_valid(), 'demo'))
        self.nightly_mark(9)

    def events_valid(self):
        path = self.root / 'events/L-base-red-local.jsonl'
        return [json.loads(s) for s in path.read_text().splitlines() if s.startswith('{')]

    def test_nightly_ac7_collect_cron(self):
        import crons
        row, = [r for r in crons.rows() if r['name'] == 'base-red-nightly-collect']
        self.assertEqual(row, dict(name='base-red-nightly-collect', where='user',
            schedule='40 * * * *', command=f'{HERE.parent}/doit cron-run base-red-nightly-collect -- '
            f'/usr/bin/python3 {HERE}/base_red.py nightly-collect albert-scott >> '
            f'{self.root}/logs/base-red-nightly-collect.log 2>&1', path='/usr/bin/python3',
            sig=r'base_red\.py\s+nightly-collect\s+albert-scott\b', owner='thinker',
            project='albert-scott', max_runtime_s=1800))
        self.nightly_mark(7)

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


class RecheckTests(unittest.TestCase):
    def test_l1664_ac5_recheck(self):
        with tempfile.TemporaryDirectory(prefix="recheck-proof-") as temp:
            root = pathlib.Path(temp)
            repo = root / "repos/demo"
            repo.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
            git("init", "-q")
            (repo / "test_cases.py").write_text("""import pytest
@pytest.fixture
def bad_setup():
    raise RuntimeError('setup')
@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError('teardown')
def test_red(): assert False
def test_green(): assert True
def test_fixture(missing_fixture): pass
def test_setup(bad_setup): pass
def test_teardown(bad_teardown): assert False
@pytest.mark.skip(reason='skip')
def test_skip(): pass
@pytest.mark.xfail(reason='xfail')
def test_xfail(): assert False
""")
            (repo / "test_collection.py").write_text("def test_broken(:\n")
            (repo / "conftest.py").write_text("raise RuntimeError('conftests must stay off')\n")
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "fixture")
            sha = git("rev-parse", "HEAD")
            common = pathlib.Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip()).resolve()
            candidates = [os.environ.get("DOIT_RECHECK_PYTHON"), str(common.parent/".venv/bin/python"), "/opt/albert-scott/.venv/bin/python"]
            python = next((p for p in candidates if p and pathlib.Path(p).is_file() and subprocess.run(
                [p, "-c", "import pytest"], capture_output=True).returncode == 0), None)
            self.assertIsNotNone(python, "real pytest interpreter is required")
            def ids(*names): return ["test_cases.py::test_"+n for n in names]
            before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
            calls = []
            real_run = subprocess.run
            def observed(argv, **kw):
                calls.append(argv)
                self.assertNotIn(argv[0], ("gh", "claude"))
                return real_run(argv, **kw)
            with patch.object(br.fold, "ROOT", root), patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_RECHECK_PYTHON=python), patch.object(br.subprocess, "run", side_effect=observed):
                probe_ids = ids("red", "fixture", "green") + ["bad node"] + ids("setup") + ["missing.py::test_absent"]
                def injected(repo, base, nodes):
                    self.assertNotIn("bad node", nodes)
                    return {"red": ids("red"), "green": ids("green"), "absent": ["missing.py::test_absent"], "undetermined": ids("setup", "fixture")}
                injected_result = br.recheck("demo", sha, probe_ids, runner=injected)
                self.assertEqual(injected_result, {"red": ids("red"), "green": ids("green"), "absent": ["missing.py::test_absent"], "undetermined": ids("fixture") + ["bad node"] + ids("setup")})
                omitted = br.recheck("demo", sha, ids("red", "green"), runner=lambda *a: {"red": ids("red")})
                self.assertEqual(omitted["undetermined"], ids("green"))
                for exception in (RuntimeError("runner"), subprocess.TimeoutExpired("runner", 1)):
                    with patch.object(br, "_run_recheck", side_effect=exception):
                        self.assertEqual(br.recheck("demo", sha, ids("red"))["undetermined"], ids("red"))
                result = br.recheck("demo", sha, ids("red", "green", "fixture", "setup", "teardown", "skip", "xfail") + ["missing.py::test_absent"])
                self.assertEqual(result["red"], ids("red"))
                self.assertEqual(result["green"], ids("green"))
                self.assertEqual(result["absent"], ["missing.py::test_absent"])
                self.assertEqual(result["undetermined"], ids("fixture", "setup", "teardown", "skip", "xfail"))
                for nodes in [ids("nonexistent"), ["test_collection.py::test_unknown"]]:
                    self.assertEqual(br.recheck("demo", sha, nodes)["undetermined"], nodes)
                for code in (2, 3, 4, 5):
                    def aborted(argv, **kw):
                        if "-m" in argv and "pytest" in argv:
                            return subprocess.CompletedProcess(argv, code, b"", b"")
                        return observed(argv, **kw)
                    with patch.object(br.subprocess, "run", side_effect=aborted):
                        self.assertEqual(br.recheck("demo", sha, ids("red"))["undetermined"], ids("red"))
                for interpreter in [str(root/"missing-python"), "/usr/bin/python3"]:
                    with patch.dict(os.environ, DOIT_RECHECK_PYTHON=interpreter):
                        self.assertEqual(br.recheck("demo", sha, ids("red"))["undetermined"], ids("red"))
                invalid = ["../test_cases.py::test_red", "-x.py::test_red", "test_cases.py::bad name", "/test_cases.py::test_red", "vitest::test"]
                runner = unittest.mock.Mock()
                self.assertEqual(br.recheck("demo", sha, invalid, runner=runner)["undetermined"], invalid)
                runner.assert_not_called()
                with patch.object(br, "_run_recheck", side_effect=ValueError("runner unavailable")):
                    self.assertEqual(br.recheck("demo", sha, ids("red"))["undetermined"], ids("red"))
                import grading_env
                with patch.dict(os.environ, DOIT_RECHECK_PYTHON=""):
                    with patch.object(grading_env, "project_venv", return_value="/chosen/venv"):
                        self.assertEqual(br._recheck_python("demo"), "/chosen/venv/bin/python")
                    with patch.object(grading_env, "project_venv", return_value=None):
                        self.assertEqual(br._recheck_python("demo"), str(repo/".venv/bin/python"))
            after = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(any("archive" in call for call in calls))
            body = {"red": result["red"], "green": result["green"],
                    "absent": result["absent"], "undetermined": result["undetermined"],
                    "checkout_unchanged": before == after}
            print(json.dumps(dict(url="cli://base-red/1664/AC5", status=0,
                                  body_sha256=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                                  body_excerpt=body)))
            print("L-spec-1664 AC5 base_red ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
