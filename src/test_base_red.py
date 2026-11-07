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
import re
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
    def partition_fixture(self):
        raw = (FIX / "pg-parity-partitioned.log").read_text()
        provenance = json.loads((FIX / "pg-parity-partitioned.provenance.json").read_text())
        return raw, provenance

    def partition_response(self, ac, body):
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url="scratch://pg-partition-naming/" + ac, status=200,
                              body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                              body_excerpt=body), sort_keys=True))

    def test_partition_fixture_provenance(self):
        raw, provenance = self.partition_fixture()
        checkout = os.environ.get("PG_PARTITION_PRODUCER_CHECKOUT")
        self.assertTrue(checkout, "PG_PARTITION_PRODUCER_CHECKOUT is required, never skipped")
        sha = provenance["producer_sha"]
        source = subprocess.check_output(["git", "-C", checkout, "show", sha + ":scripts/ci/pg_scratch_parallel.py"])
        self.assertEqual(hashlib.sha256(source).hexdigest(), provenance["producer_source_sha256"])
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), provenance["fixture_sha256"])
        argv = ["/usr/bin/python3", "src/fixtures/base_red/regenerate_pg_partition.py",
                "--producer-checkout", "$PG_PARTITION_PRODUCER_CHECKOUT", "--producer-sha", sha,
                "--output-dir", "<fresh-scratch-dir>"]
        self.assertEqual(provenance["generator_argv"], argv)
        self.assertEqual(provenance["working_directory"], "do-it-v2 worktree root")
        output = self.root / "regenerated"
        argv[3], argv[-1] = checkout, str(output)
        proc = subprocess.run(argv, cwd=HERE.parent, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        regenerated = json.loads((output / "pg-parity-partitioned.provenance.json").read_text())
        replay = (output / "pg-parity-partitioned.log").read_text()
        self.assertEqual(hashlib.sha256(replay.encode()).hexdigest(), regenerated["fixture_sha256"])
        for key in ("groups", "synthetic_sources", "weights", "expected_nodes", "producer_source_sha256",
                    "patched_boundaries", "placeholder_variables", "independent_placeholders", "exit_status"):
            self.assertEqual(regenerated[key], provenance[key], key)
        groups = provenance["groups"]
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(groups))
        self.assertFalse(set(groups[0]) & set(groups[1]))
        self.assertEqual(set(groups[0] + groups[1]), set(provenance["synthetic_sources"]))
        self.assertEqual(provenance["exit_status"], 1)
        # Independent normalization: do not trust the generator's inventory.
        def inventory(text):
            rows = []
            for raw_line in text.splitlines():
                line = br.ANSI.sub("", raw_line).split("\t")[-1].strip()
                line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)
                match = re.fullmatch(r"(FAILED|ERROR)\s+(\S+\.py::\S+)(?:\s+-\s+.*)?", line)
                if not match:
                    continue
                kind, node = match.groups()
                file, test = node.split("::", 1)
                files = [f for f in groups[0] + groups[1] if file == f or file.endswith("/" + f)]
                self.assertEqual(len(files), 1)
                canonical = files[0] + "::" + test
                group, = [i for i, members in enumerate(groups, 1) if files[0] in members]
                rows.append(dict(group=group, kind=kind, node=canonical))
            return rows
        expected, actual = inventory(raw), inventory(replay)
        self.assertEqual(expected, actual)
        self.assertEqual([r["group"] for r in actual], [1, 2])
        self.assertEqual({r["node"] for r in actual}, set(provenance["expected_nodes"]))
        self.assertEqual(expected, provenance["normalized_inventory"])
        self.assertEqual(actual, regenerated["normalized_inventory"])
        self.partition_response("AC1", dict(expected_groups=groups, actual_groups=regenerated["groups"],
                                expected=expected, actual=actual, aggregate_status=regenerated["exit_status"]))

    def test_partition_parse_log(self):
        raw, provenance = self.partition_fixture()
        nodes = provenance["expected_nodes"]
        def only(indices):
            return "\n".join(line for line in raw.splitlines()
                             if not any("FAILED " + node in line for i, node in enumerate(nodes) if i not in indices))
        cases = [("two-red", raw, set(nodes)), ("first-red", only({0}), {nodes[0]}),
                 ("second-red", only({1}), {nodes[1]}), ("green", only(set()), set()),
                 ("duplicate", raw + raw, set(nodes)),
                 ("parameter", raw.replace(nodes[0], "tests/test_partition_a.py::test_x[a-b]"),
                  {"tests/test_partition_a.py::test_x[a-b]", nodes[1]}),
                 ("error", raw.replace("FAILED " + nodes[1], "ERROR " + nodes[1]), set(nodes))]
        observations = []
        for name, text, expected in cases:
            for wrapper in (lambda line: line, lambda line: "\x1b[31m2026-11-07T00:00:00Z " + line + "\x1b[0m"):
                wrapped = "\n".join(wrapper(line) for line in text.splitlines())
                envelope = "\n".join("pg-parity (Postgres)\t" + STEP + "\t" + line for line in wrapped.splitlines())
                if expected:
                    envelope += "\npg-parity (Postgres)\t" + STEP + "\t##[error]Process completed with exit code 1"
                parsed = br.parse_log(envelope)
                self.assertEqual(parsed["pytest"], expected, name)
                self.assertEqual(parsed["pg-parity"], set(), name)
                self.assertEqual(parsed["undetermined"], [], name)
                # Real zip acquisition/envelope code; only GitHub transport is fake.
                self.zips[1] = archive(**{"pg-parity (Postgres)/5_scratch.txt": wrapped})
                self.jobs[1] = [dict(name=br.window_gate.PG_CHECK, conclusion="failure" if expected else "success",
                                     steps=[dict(name=STEP, number=5, conclusion="failure" if expected else "success")])]
                doc = dict(checks={br.window_gate.PG_CHECK: dict(run_id=1)})
                candidate = br.window_gate.candidate_failures(self.repo, dict(doc=doc))
                self.assertEqual(candidate["pytest"], expected, name)
                self.assertEqual(candidate["pg-parity"], set(), name)
                self.assertEqual(candidate["undetermined"], [], name)
            observations.append(dict(case=name, expected=sorted(expected), actual=sorted(candidate["pytest"])))
        self.assertEqual(br.parse_log(raw)["pytest"], set(), "bare pytest is not PG context")
        setup = "pg-parity\tSet up scratch\t##[error]Process completed with exit code 1"
        named = "pg-parity\t" + STEP + "\tFAILED " + nodes[0]
        parsed = br.parse_log(named + "\n" + setup)
        self.assertEqual(parsed["pytest"], {nodes[0]})
        self.assertEqual(parsed["pg-parity"], {"pg-parity::Set up scratch"})
        self.assertEqual(br.parse_log("pg-parity\t" + STEP + "\t##[error]failed")["pg-parity"], {"pg-parity::" + STEP})
        for marker in ("Scratch cluster B shutdown failed", "Scratch parallel execution failed; readiness undetermined"):
            mixed = named + "\npg-parity\t" + STEP + "\t" + marker
            parsed = br.parse_log(mixed)
            self.assertEqual(parsed["pytest"], {nodes[0]})
            self.assertTrue(any(marker in reason for reason in parsed["undetermined"]))
            with patch.object(br.window_gate, "candidate_logs", return_value=[("1", mixed)]):
                candidate = br.window_gate.candidate_failures(self.repo, dict(doc={}))
            self.assertEqual(candidate["pytest"], {nodes[0]})
            self.assertTrue(any(marker in reason for reason in candidate["undetermined"]))
        sentinel = self.root / "must-not-exist"
        malicious = named + " - $(touch " + str(sentinel) + "); `false`"
        self.assertEqual(br.parse_log(malicious)["pytest"], {nodes[0]})
        self.assertFalse(sentinel.exists())
        self.partition_response("AC2", observations)

    def test_partition_store_round_trip(self):
        raw, provenance = self.partition_fixture()
        text = "\n".join("pg-parity\t" + STEP + "\t" + line for line in raw.splitlines())
        ids = sorted(br.parse_log(text)["pytest"])
        self.assertEqual(ids, sorted(provenance["expected_nodes"]))
        br._append("base-red-nightly", "demo", self.sha, complete=True, ids=[])
        br.record("demo", self.sha, ids, source="rerun")
        (self.root / "repos" / "other").mkdir()
        code = ("import base_red,json; print(json.dumps(dict(known=base_red.known_red('demo'), "
                "other=base_red.known_red('other'), rows=base_red._rows('demo'))))")
        proc = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(HERE)}, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        read = json.loads(proc.stdout)
        self.assertEqual(set(read["known"]), set(ids))
        self.assertEqual(read["other"], {})
        self.assertTrue(all(row["source"] == "rerun" and row["sha"] == self.sha for row in read["known"].values()))
        self.assertEqual(read["rows"][-1]["ids"], ids)
        self.assertFalse(read["rows"][-1]["complete"])
        self.partition_response("AC4", read)

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


class StalenessTests(unittest.TestCase):
    NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nightly-staleness-')
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / 'repos' / 'albert-scott'
        self.repo.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        git('init', '-q')
        git('-c', 'user.name=test', '-c', 'user.email=test@localhost',
            'commit', '--allow-empty', '-qm', 'scratch fixture')
        git('remote', 'add', 'origin', 'git@github.com:example/scratch.git')
        self.sha = git('rev-parse', 'HEAD')
        self.ledger = self.root / 'events' / 'L-base-red-local.jsonl'
        self.ledger.parent.mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(br.fold, 'ROOT', self.root))
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root),
            HOME=str(self.root / 'home'), PYTHONPATH=str(HERE)))

    def mark(self, ac, body):
        print(json.dumps(dict(url=f'cli://base-red/nightly-staleness/AC{ac}', status=0,
              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def seed(self, rows):
        self.ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def alarms(self):
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()] if self.ledger.exists() else []
        return [row for row in rows if row.get('subject') == 'nightly-stale:albert-scott'
                and row.get('type') == 'escalation-blocking']

    def dispatch(self, at=None, **fields):
        return dict(dict(type='base-red-nightly-dispatched', project='albert-scott',
                         sha=self.sha, suite='nightly', run_ids=['1', '2'],
                         ts=(at or self.NOW).isoformat()), **fields)

    def assert_locked(self):
        path = self.root / 'base-red' / 'albert-scott' / 'nightly.lock'
        with path.open('a') as other:
            with self.assertRaises(BlockingIOError):
                br.fcntl.flock(other, br.fcntl.LOCK_EX | br.fcntl.LOCK_NB)

    def completion(self, age=0, **fields):
        return dict(dict(type='base-red-nightly', project='albert-scott',
                         suite='nightly', complete=True,
                         ts=(self.NOW - timedelta(seconds=age)).isoformat()), **fields)

    def test_staleness_ac1_matrix(self):
        for suite in ('nightly', 'undeclared', 'local-hermetic', None, 'unknown'):
            for complete in (True, False, None, 'true'):
                for project in ('albert-scott', 'foreign'):
                    for kind in ('base-red-nightly', 'base-red-nightly-dispatched', 'base-red'):
                        row = self.completion(suite=suite, complete=complete, project=project, type=kind)
                        if suite is None:
                            del row['suite']
                        if complete is None:
                            del row['complete']
                        valid = (suite in ('nightly', 'undeclared') and complete is True
                                 and project == 'albert-scott' and kind == 'base-red-nightly')
                        case = f'{suite}/{complete}/{project}/{kind}'
                        with self.subTest(case=case):
                            rows = [self.completion(27 * 3600), row]
                            alert = br.nightly_stale(rows, 'albert-scott', self.NOW)
                            self.assertEqual(alert is None, valid)
                            print('staleness ac1', case, 'PASS')
        for age, stale in ((26 * 3600, False), (26 * 3600 + 1, True)):
            row = self.completion(age)
            self.assertEqual(br.nightly_stale([row], 'albert-scott', self.NOW) is not None, stale)
            print('staleness ac1 boundary', age, 'PASS')
        fresh, old = self.completion(ids=['red']), self.completion(100000)
        for rows in ([old, fresh, old], [fresh, old, fresh]):
            self.assertIsNone(br.nightly_stale(rows, 'albert-scott', self.NOW))
        print('staleness ac1 unordered duplicate fresh-red PASS')
        self.mark(1, 'PASS: 120 matrix cases; exact boundary; unordered duplicates; fresh red')

    def test_staleness_ac2_episodes_and_uncertainty(self):
        def alert(rows, now=None):
            answer = br.nightly_stale(iter(rows), 'albert-scott', now or self.NOW)
            self.assertEqual(answer, br.nightly_stale(reversed(rows), 'albert-scott', now or self.NOW))
            return answer
        empty = alert([])
        self.assertEqual(empty['state'], 'undetermined')
        self.assertEqual(empty['episode'], 'never-observed')
        self.assertIsNone(empty['last_complete_at'])
        self.assertTrue(br.fold.escalation_ok(empty))
        self.assertEqual(br._time(empty['deadline']), self.NOW + timedelta(hours=24))
        self.assertIsNone(br.nightly_stale([], 'do-it-v2', self.NOW))
        for age, expected in ((26 * 3600, None), (26 * 3600 + 1, 'stale')):
            rows = [self.dispatch(self.NOW - timedelta(seconds=age)), self.dispatch()]
            result = alert(rows)
            self.assertEqual(result['state'] if result else None, expected)
            print('staleness ac2 dispatch-only boundary', age, 'PASS')
        for stamp in ('bad', None, '2026-11-06T12:00:00', 42,
                      (self.NOW + timedelta(seconds=1)).isoformat()):
            rows = [self.completion(ts=stamp)]
            self.assertEqual(alert(rows)['state'], 'undetermined')
            self.assertEqual(alert(rows + [self.dispatch()])['state'], 'undetermined')
            print('staleness ac2 invalid/future completion', repr(stamp), 'PASS')
        self.assertEqual(alert([self.dispatch(ts='bad')])['state'], 'undetermined')
        self.assertIsNone(alert([self.completion(ts='2026-11-06T14:00:00+02:00')]))
        offset_now = self.NOW.astimezone(timezone(timedelta(hours=-5)))
        old = self.completion(27 * 3600)
        first = alert([old], offset_now)
        self.assertEqual(first['last_complete_at'], old['ts'])
        alarm = dict(first, type='escalation-blocking')
        rows = [old, alarm, dict(type='decision', project='albert-scott', subject=first['subject'])]
        for hours in range(1, 5):
            self.assertIsNone(alert(rows + [self.dispatch()], self.NOW + timedelta(hours=hours)))
        for wrong in (dict(alarm, project='foreign'), dict(alarm, episode='other'),
                      dict(alarm, type='base-red'), dict(alarm, default='')):
            self.assertIsNotNone(alert([old, wrong]))
        recovered = self.completion()
        self.assertIsNone(alert(rows + [recovered]))
        second = alert(rows + [recovered], self.NOW + timedelta(hours=27))
        self.assertNotEqual(first['episode'], second['episode'])
        self.mark(2, 'PASS: empty/dispatch-only histories, invalid/future times, UTC normalization, '
                     'repeated ticks, decisions, recovery, second outage, order independence')

    def test_staleness_ac3_collector_branches(self):
        now = datetime.now(timezone.utc)
        old = self.completion(ts=(now - timedelta(hours=27)).isoformat(), sha='a' * 40)
        dispatch = self.dispatch(now)
        runs = [dict(id=i, head_sha=self.sha, status='completed', conclusion='success') for i in (1, 2)]
        for branch in ('no-dispatch', 'already-collected', 'pending-unresolved', 'pending-error',
                       'queued', 'in-progress', 'overdue', 'missing', 'cancelled',
                       'malformed', 'malformed-time', 'malformed-null-time', 'unavailable-api', 'incomplete-snapshot'):
            with self.subTest(branch=branch), contextlib.ExitStack() as stack:
                rows = [old, dispatch]
                if branch == 'no-dispatch':
                    rows = []
                elif branch == 'already-collected':
                    rows = [dict(old, sha=self.sha), dispatch]
                elif branch == 'malformed':
                    rows[-1] = dict(dispatch, run_ids=[])
                elif branch == 'malformed-time':
                    rows[-1] = dict(dispatch, ts='broken')
                elif branch == 'malformed-null-time':
                    rows[-1] = dict(dispatch, ts=None)
                elif branch == 'overdue':
                    rows[-1] = self.dispatch(now - timedelta(hours=27))
                self.seed(rows)
                stack.enter_context(patch.object(br, '_pending', return_value=branch.startswith('pending')))
                resolve = stack.enter_context(patch.object(br, '_resolve_pending', return_value=None))
                if branch == 'pending-error':
                    resolve.side_effect = ValueError('dispatch never appeared')
                selected = [dict(run) for run in runs]
                if branch in ('queued', 'in-progress', 'overdue'):
                    selected[0]['status'] = 'in_progress' if branch == 'in-progress' else 'queued'
                if branch == 'missing':
                    selected = []
                if branch == 'cancelled':
                    selected[0]['conclusion'] = 'cancelled'
                api = stack.enter_context(patch.object(br, '_api', return_value=selected))
                if branch == 'unavailable-api':
                    api.side_effect = ValueError('GitHub API failed')
                stack.enter_context(patch.object(br, '_snapshot', return_value=(br._result(self.sha), {})))
                for name in ('nightly', 'failing'):
                    stack.enter_context(patch.object(br, name, side_effect=AssertionError('forbidden dispatch')))
                stack.enter_context(patch.object(br.ghlimit, 'run', side_effect=AssertionError('unexpected gh')))
                # Observe real reads, decisions and writes; do not replace persistence.
                for name in ('_nightly_rows', 'nightly_stale', '_append'):
                    original = getattr(br, name)
                    def observe(*args, original=original, **kwargs):
                        self.assert_locked()
                        return original(*args, **kwargs)
                    stack.enter_context(patch.object(br, name, side_effect=observe))
                results = []
                collect = br.nightly_collect
                def record(project):
                    result = collect(project)
                    results.append(result)
                    return result
                stack.enter_context(patch.object(br, 'nightly_collect', side_effect=record))
                for tick in range(4):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = br.main(['nightly-collect', 'albert-scott'])
                    result = results[-1]
                    self.assertEqual(code, 2 if tick == 0 else 0, (branch, result))
                    self.assertFalse(result['complete'])
                    self.assertTrue(result['undetermined'])
                    self.assertIn('undetermined', output.getvalue())
                    status = 'appended' if tick == 0 else 'already-alarmed'
                    self.assertEqual(result['nightly_staleness']['alarm_status'], status)
                    self.assertIn(status, output.getvalue())
                    self.assertEqual(len(self.alarms()), 1)
                    print('staleness ac3', branch, 'tick', tick, 'exit', code, status, 'count=1 PASS')
                if branch in ('no-dispatch', 'already-collected'):
                    api.assert_not_called()
        self.mark(3, 'PASS: 14 collector branches x 4 ticks; locked read/decision/append; '
                     'first exit 2 then three exits 0 with explicit uncertainty and one durable alarm')

    def test_staleness_ac3_fresh_completion_and_storage_errors(self):
        now = datetime.now(timezone.utc)
        self.seed([self.completion(ts=(now - timedelta(hours=27)).isoformat(), sha='a' * 40),
                   self.dispatch(now)])
        runs = [dict(id=i, head_sha=self.sha, status='completed', conclusion='success') for i in (1, 2)]
        result = br._result(self.sha)
        result['complete'] = True
        with patch.object(br, '_api', return_value=runs), patch.object(br, '_snapshot', return_value=(result, {})), \
             patch.object(br, 'known_red', return_value=[]), patch('quarantine.apply') as quarantine:
            self.assertTrue(br.nightly_collect('albert-scott')['complete'])
            quarantine.assert_called_once_with('albert-scott')
        self.assertEqual(self.alarms(), [])
        for failure in ('append', 'read', 'lock'):
            self.seed([])
            name = {'append': '_append', 'read': '_nightly_rows'}
            target = patch.object(br, name[failure], side_effect=OSError('injected')) if failure != 'lock' else \
                     patch.object(br.fcntl, 'flock', side_effect=OSError('injected'))
            with target, contextlib.redirect_stdout(io.StringIO()):
                answer = br.nightly_collect('albert-scott')
                self.assertEqual(answer['nightly_staleness']['alarm_status'], 'failed')
                self.assertTrue(answer['undetermined'])
                self.assertEqual(br.main(['nightly-collect', 'albert-scott']), 2)
            self.assertEqual(self.alarms(), [])
            print('staleness ac3', failure, 'failure exit=2 PASS')
        # Even an existing durable alarm cannot hide a new read/lock failure.
        br.nightly_collect('albert-scott')
        with patch.object(br, '_nightly_rows', side_effect=OSError('unreadable')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(br.main(['nightly-collect', 'albert-scott']), 2)
        self.mark(3, 'PASS: newly committed completion is fresh; append/read/lock errors exit 2; '
                     'prior delivery cannot mask a new storage failure')

    def process_fixture(self):
        """Unmodified production CLI/store, with a fake external gh executable."""
        fake = self.root / 'gh'
        fake.write_text('''#!/usr/bin/python3
import fcntl, json, os, pathlib, sys, time
root = pathlib.Path(os.environ['DOIT_ROOT'])
assert sys.argv[1] == 'api', 'collector attempted a paid dispatch'
if sys.argv[2] == 'rate_limit':
    print(json.dumps({'resources': {k: dict(limit=5000, remaining=5000, reset=time.time()+3600)
                                   for k in ('core', 'graphql')}}))
else:
    with (root / 'base-red/albert-scott/nightly.lock').open('a') as other:
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('API exception path was unlocked')
    with (root / 'api-observed').open('a') as out:
        out.write('unavailable-api\\n')
    sys.exit(1)
''')
        fake.chmod(0o700)
        self.child_env = dict(os.environ, DOIT_REAL_GH=str(fake),
                              GH_LIMIT_STATE=str(self.root / 'quota.json'))
        # Profiling observes the production functions and injects only the two
        # explicitly required crash points. It never substitutes an append/read.
        self.runner = self.root / 'collect.py'
        self.runner.write_text('''import fcntl, json, os, pathlib, sys, time
import base_red
root = pathlib.Path(os.environ['DOIT_ROOT'])
mode, sync = sys.argv[1:]
trace = root / ('trace-' + str(os.getpid()))
def record(value):
    with trace.open('a') as out:
        out.write(json.dumps(value) + '\\n')
def locked():
    with (root / 'base-red/albert-scott/nightly.lock').open('a') as other:
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise AssertionError('alarm read/decision/append without nightly.lock')
def observe(frame, event, arg):
    if frame.f_code.co_filename != base_red.__file__:
        return
    name = frame.f_code.co_name
    if event == 'call' and name in ('_nightly_rows', 'nightly_stale', '_append'):
        locked()
        record({'locked': name})
        if name == '_append' and mode == 'before':
            raise OSError('injected before append commit')
    if event == 'return' and name == '_append' and mode == 'after':
        record({'crash': 'after committed append'})
        os._exit(19)
    if event == 'return' and name == 'nightly_collect':
        record({'result': arg})
if sync != '-':
    (root / ('ready-' + str(os.getpid()))).touch()
    limit = time.monotonic() + 15
    while not pathlib.Path(sync).exists():
        assert time.monotonic() < limit, 'start coordination timed out'
        time.sleep(.01)
sys.setprofile(observe)
sys.exit(base_red.main(['nightly-collect', 'albert-scott']))
''')

    def child(self, mode='normal'):
        return subprocess.run([sys.executable, str(self.runner), mode, '-'],
                              env=self.child_env, capture_output=True, text=True, timeout=30)

    def fresh_alarm_read(self, expected):
        # Each assertion is backed by both real raw bytes and the admitted fold
        # in an independent interpreter, never a same-process cached snapshot.
        code = '''import fold, json
p = fold.ROOT / 'events/L-base-red-local.jsonl'
raw = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
def alarms(rows):
    return [e for e in rows if e.get('type') == 'escalation-blocking'
            and e.get('subject') == 'nightly-stale:albert-scott']
raw, admitted = alarms(raw), alarms(fold.read_events(use_cache=False))
assert len(raw) == len(admitted)
for e in admitted:
    assert fold.escalation_ok(e) and all(e.get(k) for k in ('default', 'deadline', 'revert'))
    assert e['complete'] is False and e['ids'] == [] and e['project'] == 'albert-scott'
print(json.dumps({'raw_count': len(raw), 'admitted_count': len(admitted),
                  'episodes': [e['episode'] for e in admitted],
                  'sha_empty': [not e['sha'] for e in admitted]}))
'''
        proc = subprocess.run([sys.executable, '-c', code], env=self.child_env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['admitted_count'], expected, result)
        print('staleness ac4 fresh reader', json.dumps(result))
        return result

    def test_staleness_ac4_real_store_concurrency_and_errors(self):
        self.process_fixture()
        for branch in ('no-dispatch', 'unavailable-api'):
            with self.subTest(branch=branch):
                now = datetime.now(timezone.utc)
                rows = [] if branch == 'no-dispatch' else [self.dispatch(now - timedelta(hours=27))]
                self.seed(rows)
                before_api = (self.root / 'api-observed').read_text().count('\n') if (self.root / 'api-observed').exists() else 0
                start = self.root / ('start-' + branch)
                children = [subprocess.Popen([sys.executable, str(self.runner), 'normal', str(start)],
                             env=self.child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            for _ in range(2)]
                try:
                    limit = time.monotonic() + 10
                    while not all((self.root / ('ready-' + str(child.pid))).exists() for child in children):
                        self.assertLess(time.monotonic(), limit, 'children did not reach start coordination')
                        time.sleep(.01)
                    start.touch()
                    outcomes = []
                    for child in children:
                        stdout, stderr = child.communicate(timeout=30)
                        self.assertIn('undetermined', stdout, stderr)
                        outcomes.append(child.returncode)
                        traces = [json.loads(line) for line in (self.root / ('trace-' + str(child.pid))).read_text().splitlines()]
                        names = {row['locked'] for row in traces if 'locked' in row}
                        self.assertTrue({'_nightly_rows', 'nightly_stale'} <= names)
                        result = next(row['result'] for row in traces if 'result' in row)
                        self.assertFalse(result['complete'])
                        self.assertTrue(result['undetermined'])
                        if child.returncode == 2:
                            self.assertIn('_append', names)
                            self.assertEqual(result['nightly_staleness']['alarm_status'], 'appended')
                        else:
                            self.assertEqual(result['nightly_staleness']['alarm_status'], 'already-alarmed')
                    self.assertEqual(sorted(outcomes), [0, 2])
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.communicate()
                if branch == 'unavailable-api':
                    self.assertEqual((self.root / 'api-observed').read_text().count('\n') - before_api, 2)
                summary = self.fresh_alarm_read(1)
                self.assertEqual(summary['sha_empty'], [branch == 'no-dispatch'])
                for _ in range(3):
                    repeat = self.child()
                    self.assertEqual(repeat.returncode, 0, repeat.stderr)
                    self.assertIn('already-alarmed', repeat.stdout)
                self.fresh_alarm_read(1)
                print('staleness ac4 concurrent', branch, 'both entered; exits=0,2; count=1 PASS')
        # The same production writer is exercised on either side of its commit.
        self.seed([])
        failure = self.child('before')
        self.assertEqual(failure.returncode, 2, failure.stderr)
        self.assertIn('storage or lock unavailable', failure.stdout)
        self.fresh_alarm_read(0)
        self.assertEqual(self.child().returncode, 2)
        self.fresh_alarm_read(1)
        self.seed([])
        self.assertEqual(self.child('after').returncode, 19)
        self.fresh_alarm_read(1)
        self.assertEqual(self.child().returncode, 0)
        self.fresh_alarm_read(1)
        # Historic recovery fixture: fresh at landing, stale 27 hours later.
        # Only history is synthetic; every alarm is appended by the real CLI.
        now = datetime.now(timezone.utc)
        recovered_at = now - timedelta(hours=27)
        recovered = self.completion(ts=recovered_at.isoformat(), sha=self.sha)
        prior = self.alarms()
        self.assertIsNone(br.nightly_stale(prior + [recovered], 'albert-scott', recovered_at))
        with self.ledger.open('a') as out:
            out.write(json.dumps(recovered) + '\n')
        self.assertEqual(self.child().returncode, 2)
        summary = self.fresh_alarm_read(2)
        self.assertEqual(len(set(summary['episodes'])), 2)
        # Exercise both uninstrumented public CLI routes too.
        for argv in ([str(br.DOIT), 'base-red', 'nightly-collect', 'albert-scott'],
                     [sys.executable, str(HERE / 'base_red.py'), 'nightly-collect', 'albert-scott']):
            proc = subprocess.run(argv, env=self.child_env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('undetermined', proc.stdout)
            self.assertIn('already-alarmed', proc.stdout)
        self.fresh_alarm_read(2)
        self.mark(4, 'PASS: production CLI -> _append -> doit append; fresh raw/fold readers; '
                     'no-dispatch and unavailable-API concurrent processes each yield one admitted alarm; '
                     'locked reads/decisions/appends, precommit retry, postcommit crash, recovery and second outage')

    def test_staleness_ac5_purity_security_and_scope(self):
        hostile = '$(touch injected); `touch injected` <script>bad</script>'
        rows = [self.completion(27 * 3600, reason=hostile), self.dispatch(ts=hostile),
                dict(type='escalation-blocking', project='foreign', subject='nightly-stale:albert-scott',
                     episode='never-observed', reason=hostile)]
        with patch.object(br.subprocess, 'run', side_effect=AssertionError('pure helper called subprocess')), \
             patch.object(br, '_api', side_effect=AssertionError('pure helper called API')), \
             patch.object(pathlib.Path, 'open', side_effect=AssertionError('pure helper opened file')):
            answer = br.nightly_stale(rows, 'albert-scott', self.NOW)
            self.assertEqual(answer['state'], 'stale')
            self.assertIsNone(br.nightly_stale(rows, 'do-it-v2', self.NOW))
        # Arbitrary text stays JSON data through the actual argv/store path.
        br._append('escalation-blocking', 'albert-scott', '', complete=False, ids=[],
                   subject=answer['subject'], **{k: v for k, v in answer.items() if k not in ('subject', 'project', 'reason')},
                   reason=hostile)
        self.assertEqual(self.alarms()[0]['reason'], hostile)
        self.assertFalse((pathlib.Path.cwd() / 'injected').exists())
        foreign_repo = self.root / 'repos' / 'do-it-v2'
        foreign_repo.mkdir()
        before = self.ledger.read_bytes()
        with patch.object(br, 'nightly', side_effect=AssertionError('paid dispatch')), \
             patch.object(br.ghlimit, 'run', side_effect=AssertionError('paid dispatch')):
            self.assertNotIn('nightly_staleness', br.nightly_collect('do-it-v2'))
        self.assertEqual(self.ledger.read_bytes(), before)
        with self.assertRaises(ValueError):
            br._repo('../albert-scott')
        self.mark(5, 'PASS: pure helper has no file/network/subprocess effects; real JSON argv preserves '
                     'hostile text as data; no paid calls, project leakage or invalid repository traversal')


def selective_response(ac, body):
    raw = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url="scratch://base-red-selective/" + ac, status=200,
        body_sha256=hashlib.sha256(raw.encode()).hexdigest(), body_excerpt=body)))


class SelectiveFixture(unittest.TestCase):
    setUp = BaseRedTests.setUp
    run_doc = BaseRedTests.run_doc
    gh = BaseRedTests.gh
    binary = BaseRedTests.binary
    events = BaseRedTests.events

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def workflow(self):
        (self.repo / ".github/workflows/targeted-attribution.yml").write_text(
            "inputs:\n  tree_sha:\n  node_ids:\n  probe_id:\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "probe")
        self.sha = self.git("rev-parse", "HEAD")


class SelectiveClassificationTests(SelectiveFixture):
    def test_partitions_and_boundaries(self):
        import window_attribution as wa
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        old, other = "api/tests/test_x.py::test_a", "api/tests/test_x.py::test_b"
        anchor = dict(sha=self.sha, ts=now.isoformat())
        def meta(sha=None, since=None):
            return dict(sha=sha or self.sha, since=since or anchor["ts"], source="nightly")
        cases = [
            ("known", [old], {old: meta()}, anchor, [old], [], False),
            ("unknown", [other], {old: meta()}, anchor, [], [other], False),
            ("mixed-duplicates", [other, old, other, old], {old: meta()}, anchor, [old], [other], False),
            ("fresh-empty", [old], {}, anchor, [], [old], False),
            ("missing", [old], {}, None, [], [old], True),
            ("incomplete-only", [old], {old: meta()}, None, [], [old], True),
        ]
        for seconds, name, unknown in [(0, "age-zero", False), (36*3600-1, "age-before", False),
                (36*3600, "age-boundary", True), (36*3600+1, "age-after", True), (-1, "future", True)]:
            a = dict(anchor, ts=(now-timedelta(seconds=seconds)).isoformat())
            cases.append((name, [old], {old: meta(since=a["ts"])}, a,
                          [] if unknown else [old], [old] if unknown else [], unknown))
        cases.append(("malformed", [old], {old: meta()}, dict(anchor, ts="bad"), [], [old], True))
        cases.append(("future-positive", [old], {old: meta(since=(now+timedelta(seconds=1)).isoformat())},
                      anchor, [], [old], False))
        cases.append(("before-anchor", [old], {old: meta(since=(now-timedelta(seconds=1)).isoformat())},
                      anchor, [], [old], False))
        rows = []
        with patch.object(wa, "probe", side_effect=AssertionError("dispatch forbidden")) as probe, \
             patch.object(br, "failing", side_effect=AssertionError("full refresh forbidden")):
            for name, ids, known, a, positive, pending, unknown in cases:
                with self.subTest(name=name), patch.object(br, "_known_state", return_value=(known, a)):
                    result = br.classify_selected("demo", self.sha, ids, now=now)
                    self.assertEqual(result, dict(known_red=positive, rerun_on_base=pending, undetermined=unknown))
                    rows.append(dict(case=name, result=result))
            with patch.object(br, "_project", side_effect=AssertionError("empty must be local")):
                self.assertEqual(br.classify_selected("bad", "bad", []),
                                 dict(known_red=[], rerun_on_base=[], undetermined=False))
            probe.assert_not_called()
        selective_response("AC1-boundaries", rows)

    def test_real_graph_and_registry(self):
        import window_attribution as wa
        now = datetime.now(timezone.utc)
        root = self.sha
        tree = self.git("rev-parse", "HEAD^{tree}")
        def commit(parent=None):
            args = ["-c", "user.name=t", "-c", "user.email=t@localhost", "commit-tree", tree, "-m", "fixture"]
            if parent:
                args += ["-p", parent]
            return self.git(*args)
        base = commit(root)
        descendant = commit(base)
        unrelated = commit()
        node = "api/tests/test_x.py::test_a"
        anchor = dict(sha=root, ts=(now-timedelta(hours=1)).isoformat())
        rows = []
        with patch.object(wa, "probe", side_effect=AssertionError("no dispatch")):
            for sha, qualifies, name in [(base, True, "equal"), (root, True, "ancestor"),
                    (descendant, False, "descendant"), (unrelated, False, "unrelated"),
                    ("f"*40, False, "missing-object"), ("", False, "missing-sha")]:
                for kind in ("anchor", "positive"):
                    a = dict(anchor, sha=sha) if kind == "anchor" else anchor
                    known = {node: dict(sha=sha, since=anchor["ts"], source="rerun")}
                    with patch.object(br, "_known_state", return_value=(known, a)):
                        result = br.classify_selected("demo", base, [node], now=now)
                    self.assertEqual(result, dict(known_red=[node] if qualifies else [],
                        rerun_on_base=[] if qualifies else [node], undetermined=not qualifies if kind == "anchor" else False))
                    rows.append(dict(case=kind+"-"+name, result=result))
            with patch.object(br, "_known_state", return_value=({node: dict(sha=root, since=anchor["ts"])}, anchor)):
                self.assertTrue(br.classify_selected("demo", "invalid", [node], now=now)["undetermined"])
                original = br.window_gate.git
                def fail_ancestry(repo, *args, **kwargs):
                    if args[0] == "merge-base":
                        raise RuntimeError("graph unavailable")
                    return original(repo, *args, **kwargs)
                with patch.object(br.window_gate, "git", side_effect=fail_ancestry):
                    self.assertEqual(br.classify_selected("demo", base, [node], now=now),
                                     dict(known_red=[], rerun_on_base=[node], undetermined=True))
            br._append("base-red-nightly", "demo", root, complete=True, ids=[node])
            br.record("demo", base, ["api/tests/test_x.py::test_b"], "rerun")
            result = br.classify_selected("demo", base, [node, "api/tests/test_x.py::test_b"])
            self.assertEqual(result, dict(known_red=[node, "api/tests/test_x.py::test_b"], rerun_on_base=[], undetermined=False))
            # New unrelated complete evidence blocks fallback to an older ancestor.
            br._append("base-red-nightly", "demo", unrelated, complete=True, ids=[node])
            self.assertTrue(br.classify_selected("demo", base, [node])["undetermined"])
            # Project filtering and malformed/incomplete history use the real reader.
            event_file = self.root / "events/L-base-red-local.jsonl"
            for rows_in in ([dict(type="base-red-nightly", project="foreign", sha=base, ts=now.isoformat(), complete=True, ids=[node])],
                    [dict(type="base-red-nightly", project="demo", sha=base, ts="bad", complete=True, ids=[node])],
                    [dict(type="base-red-nightly", project="demo", sha=base, ts=now.isoformat(), complete=False, ids=[node])]):
                event_file.write_text("\n".join(map(json.dumps, rows_in))+"\n")
                self.assertEqual(br.classify_selected("demo", base, [node]),
                                 dict(known_red=[], rerun_on_base=[node], undetermined=True))
        self.assertEqual(self.calls, [])
        selective_response("AC1-graph", rows)


class SelectiveRerunTests(SelectiveFixture):
    def test_probe_matrix_and_support(self):
        import window_attribution as wa
        self.workflow()
        red, green, absent = ["api/tests/test_x.py::test_"+x for x in ("red", "green", "absent")]
        for state in ("red", "green", "absent", "undetermined", "nonsense", None, ["red", "green"]):
            answer = dict(states={} if state is None else {red: state}, sha=self.sha)
            with patch.object(wa, "probe", return_value=answer) as probe, patch.object(br, "record") as record:
                result = br.rerun_on_base("demo", self.sha[:10], [red, red])
                self.assertEqual(result, dict(pre_existing=[red] if state == "red" else [],
                    new=[red] if state in ("green", "absent") else [], undetermined=state not in ("red", "green", "absent")))
                probe.assert_called_once_with(self.repo, self.sha, [red])
                if state == "red":
                    record.assert_called_once_with("demo", self.sha, [red], source="rerun")
                else:
                    record.assert_not_called()
                selective_response("AC2-state", dict(state=state, result=result))
        unsupported = ["pg-parity::x", "dashboard/src/broken.test.ts"]
        for state in ("red", "green"):
            with patch.object(wa, "probe", return_value=dict(states={red: state})) as probe, patch.object(br, "record"):
                result = br.rerun_on_base("demo", self.sha, [red, *unsupported])
                probe.assert_called_once_with(self.repo, self.sha, [red])
                self.assertEqual(result, dict(pre_existing=[red] if state == "red" else [],
                    new=[red] if state == "green" else [], undetermined=True))
                selective_response("AC2-mixed", dict(state=state, requested=[red, *unsupported], dispatched=[red], result=result))
        with patch.object(wa, "probe") as probe, patch.object(br, "record") as record:
            for ids in ([], unsupported, ["../a.py::x", "/a.py::x", "-a.py::x", "a.py::bad\n"]):
                self.assertEqual(br.rerun_on_base("demo", self.sha, ids),
                                 dict(pre_existing=[], new=[], undetermined=bool(ids)))
            for project, sha in (("../bad", self.sha), ("demo", "bad-sha"), ("missing", self.sha)):
                self.assertTrue(br.rerun_on_base(project, sha, [red])["undetermined"])
            probe.assert_not_called()
            record.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_fail_closed_transport_and_append(self):
        import window_attribution as wa
        self.workflow()
        node = "api/tests/test_x.py::test_a"
        for answer in (dict(states={node: "red"}, sha="f"*40), dict(states={node: "red", "extra": "green"}),
                       None, dict(states=None)):
            with patch.object(wa, "probe", return_value=answer), patch.object(br, "record") as record:
                self.assertEqual(br.rerun_on_base("demo", self.sha, [node]),
                                 dict(pre_existing=[], new=[], undetermined=True))
                record.assert_not_called()
        for exc in (TimeoutError(), ValueError("quota"), RuntimeError("dispatch"), OSError("artifact")):
            with patch.object(wa, "probe", side_effect=exc):
                self.assertTrue(br.rerun_on_base("demo", self.sha, [node])["undetermined"])
        with patch.object(wa, "probe", return_value=dict(states={node: "red"})), \
             patch.object(br, "record", side_effect=ValueError("append failed")):
            self.assertEqual(br.rerun_on_base("demo", self.sha, [node]),
                             dict(pre_existing=[node], new=[], undetermined=True))
        # Old workflow and the transport's own aggregate payload limit never fall back.
        old = self.git("rev-parse", "HEAD^")
        with patch.object(wa, "probe") as probe:
            self.assertTrue(br.rerun_on_base("demo", old, [node])["undetermined"])
            probe.assert_not_called()
        many = ["api/tests/test_x.py::test_" + str(i) + "x"*200 for i in range(300)]
        self.assertTrue(wa.supported(self.repo, self.sha, [many[0]]))
        with patch.object(wa, "_gh", side_effect=AssertionError("no dispatch")), \
             patch.object(br, "record") as record:
            self.assertEqual(br.rerun_on_base("demo", self.sha, many),
                             dict(pre_existing=[], new=[], undetermined=True))
            record.assert_not_called()
        selective_response("AC2-refusals", dict(identity=True, unavailable=True, append_failure=True,
                                                old_workflow=True, aggregate_limit=True))


class SelectiveRecordRoundTripTests(SelectiveFixture):
    def test_production_append_independent_reader(self):
        import window_attribution as wa
        self.workflow()
        red, green, unknown = ["api/tests/test_x.py::test_"+x for x in ("red", "green", "unknown")]
        br._append("base-red-nightly", "demo", self.sha, complete=True, ids=[])
        with patch.object(wa, "probe", return_value=dict(sha=self.sha,
                states={red: "red", green: "green", unknown: "undetermined"})) as probe:
            result = br.rerun_on_base("demo", self.sha, [red, green, unknown])
            self.assertEqual(result, dict(pre_existing=[red], new=[green], undetermined=True))
            probe.assert_called_once_with(self.repo, self.sha, [red, green, unknown])
        code = ("import sys,json; sys.path.insert(0,sys.argv[1]); import base_red; "
                "print(json.dumps(dict(rows=base_red._rows('demo'), known=base_red.known_red('demo'), "
                "root=str(base_red.fold.ROOT))))")
        proc = self.real_run([sys.executable, "-I", "-c", code, str(HERE)],
                             capture_output=True, text=True, env=dict(os.environ), timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        read = json.loads(proc.stdout)
        self.assertEqual(read["root"], str(self.root))
        self.assertEqual(set(read["known"]), {red})
        row = read["rows"][-1]
        self.assertEqual({k: row[k] for k in ("project", "sha", "ids", "source", "complete")},
                         dict(project="demo", sha=self.sha, ids=[red], source="rerun", complete=False))
        self.assertEqual(list((self.root / "events").glob("*")),
                         [self.root / "events/L-base-red-local.jsonl"])
        selective_response("AC3", dict(writer=result, reader=read, fixture_only=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
