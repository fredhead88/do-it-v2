#!/usr/bin/env python3
"""Hermetic CI shadow regressions; GitHub, repositories and ledgers are isolated."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_shadow as cs

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]
PYTHON = ".github/workflows/python-tests.yml"
DASHBOARD = ".github/workflows/dashboard-pr-quality.yml"
CHECK = "pytest (shard 1)"
REAL_RUN = subprocess.run


def report(case, body):
    raw = json.dumps(body, sort_keys=True)
    print(json.dumps({"case": case, "url": "python://ci_shadow/" + case, "status": 200,
                      "body_sha256": hashlib.sha256(raw.encode()).hexdigest(), "body_excerpt": raw}))


def check(ids=(), conclusion="success", infra=False, step=None):
    return {"ids": set(ids) if ids is not None else None, "conclusion": conclusion,
            "infra": infra, "failed_step": step}


def side(**checks):
    return {"runs": [], "checks": checks}


class CompareTests(unittest.TestCase):
    def test_paginated_api_without_slurp(self):
        pages = '{"jobs": [{"name": "first"}]}\n{"jobs": [{"name": "second"}]}\n'
        with patch.object(cs.ghlimit, "run", return_value=(0, pages, "")) as call:
            self.assertEqual(cs._rows("repos/fixture/shadow/actions/runs/1/jobs", "jobs"),
                             [{"name": "first"}, {"name": "second"}])
        self.assertNotIn("--slurp", call.call_args.args[0])
        report("paginated_api_without_slurp", {"pages": 2, "rows": 2})

    def test_compare_equal(self):
        result = cs.compare(side(pytest=check(["b", "a"]), one_sided=check()), side(pytest=check(["a", "b"])))
        self.assertIs(result["identical"], True)
        self.assertEqual(result["reason"], "")
        self.assertEqual(set(result["by_check"]), {"pytest"})
        self.assertEqual(result["by_check"]["pytest"]["hosted"], ["a", "b"])
        report("compare_equal", result)

    def test_compare_ids_differ(self):
        result = cs.compare(side(pytest=check(["a"])), side(pytest=check(["b"])))
        self.assertIs(result["identical"], False)
        for word in ("pytest", "a", "b"):
            self.assertIn(word, result["reason"])
        report("compare_ids_differ", result)

    def test_compare_conclusion_differs(self):
        result = cs.compare(side(pytest=check()), side(pytest=check(conclusion="failure")))
        self.assertIs(result["identical"], False)
        self.assertIn("pytest: conclusions", result["reason"])
        report("compare_conclusion_differs", result)

    def test_compare_one_sided(self):
        result = cs.compare(side(pytest=check(), other=check(["bad"], infra=True)), side(pytest=check()))
        self.assertIs(result["identical"], True)
        report("compare_one_sided", result)

    def test_compare_infra(self):
        result = cs.compare(side(pytest=check()), side(pytest=check(None, infra=True, step="Set up Python")))
        self.assertIs(result["identical"], False)
        self.assertIn("pytest", result["reason"])
        self.assertIn("Set up Python", result["reason"])
        report("compare_infra", result)

    def test_compare_missing_log(self):
        import ci_shadow
        hosted = {"runs": [], "checks": {"pytest": {
            "conclusion": "success", "ids": None, "failed_step": None, "infra": False}}}
        shadow = {"runs": [], "checks": {"pytest": {
            "conclusion": "success", "ids": set(), "failed_step": None, "infra": False}}}
        self.assertIsNone(ci_shadow.compare(hosted, shadow)["identical"])
        report("compare_missing_log", ci_shadow.compare(hosted, shadow))

    def test_compare_precedence(self):
        for unknown in ("a", "z"):
            result = cs.compare(side(**{unknown: check(None), "m": check(["a"])}),
                                side(**{unknown: check(), "m": check(["b"])}))
            self.assertIs(result["identical"], False)
        report("compare_precedence", result)

    def test_compare_no_common(self):
        result = cs.compare(side(a=check()), side(b=check()))
        self.assertEqual(result, {"identical": None, "by_check": {}, "reason": "no-common-checks"})
        report("compare_no_common", result)

    def test_day_rule(self):
        events = [{"type": "ci-shadow", "kind": "ci", "sha": str(i), "identical": True,
                   "ts": (NOW - timedelta(hours=i)).isoformat()} for i in range(5)]
        result = cs.day(events, NOW, "ci")
        self.assertEqual(result["n"], 5)
        self.assertEqual(set(result["shas"]), {str(i) for i in range(5)})
        self.assertIsNone(cs.day(events[:4], NOW, "ci"))
        self.assertIsNone(cs.day(events, NOW, "pg"))
        self.assertIsNone(cs.day(events + [{"type": "ci-shadow-passed", **result}], NOW, "ci"))
        for verdict in (None, False):
            changed = copy.deepcopy(events)
            changed[1]["identical"] = verdict
            self.assertIsNone(cs.day(changed, NOW, "ci"))
        changed = copy.deepcopy(events)
        changed[-1]["ts"] = (NOW - timedelta(hours=25)).isoformat()
        self.assertIsNone(cs.day(changed, NOW, "ci"))
        report("day_rule", result)


class PassTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ci-shadow-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repos" / "albert-scott"
        self.repo.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"DOIT_ROOT": str(self.root), "DOIT_PROJECT": "do-it-v2", "DOIT_MAIN": "master"})
        self.env.start()
        self.addCleanup(self.env.stop)
        fold_patch = patch.multiple(cs.fold, ROOT=self.root, EVENTS=self.root / "events", PROJECT="do-it-v2")
        fold_patch.start()
        self.addCleanup(fold_patch.stop)
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@localhost")
        self.git("remote", "add", "origin", "https://github.com/fixture/shadow.git")
        self.write("scripts/ci/predeploy_gate.sh", 'REQUIRED_CHECKS=("pytest" "pg-parity (Postgres)" "TypeScript · Biome · Tests · File Sizes")\n')
        self.write(PYTHON, self.workflow(pg=True))
        self.write(DASHBOARD, self.workflow())
        self.sha = self.commit()
        self.git("commit", "-q", "--allow-empty", "-m", "second")
        self.other = self.git("rev-parse", "HEAD").stdout.strip()
        substitutions = {"READY": self.sha, "MISSING": self.other,
                         **{key: char * 40 for key, char in zip(("OLD", "SEEN", "SELF", "SHADOW", "OBJECT", "FLIGHT"), "abcdef")}}
        rows = json.loads((Path(__file__).parent / "fixtures/ci_shadow/runs.json").read_text())
        self.runs = [{**row, "head_sha": substitutions[row["head_sha"]], "event": "workflow_dispatch",
                      "status": "completed", "html_url": f"https://github.com/fixture/shadow/actions/runs/{row['id']}"}
                     for row in rows]
        self.jobs = {row["id"]: [self.job(CHECK), self.job(cs.PG)] for row in self.runs}
        self.jobs[2] = [self.job(cs.window_gate.DASHBOARD_CHECK)]
        self.jobs[5][0]["labels"] = ["self-hosted", "as-ci"]
        self.calls, self.logs = [], {}
        for rid, jobs in self.jobs.items():
            self.logs[rid] = self.zip({j["name"] + "/1_Run.txt": "SUMMARY pytest check: PASS new=0 changed=0\n" for j in jobs})
        self.git_impl = cs.window_gate.git
        git_patch = patch.object(cs.window_gate, "git", side_effect=self.git_stub)
        git_patch.start()
        self.addCleanup(git_patch.stop)
        for patcher in (patch.object(cs.ghlimit, "run", side_effect=self.gh),
                        patch.object(cs.ghlimit, "gate", return_value={"ok": True}),
                        patch.object(cs.ghlimit, "real_gh", return_value="fixture-gh"),
                        patch.object(cs.subprocess, "run", side_effect=self.process)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    @staticmethod
    def workflow(pg=False, runner=True):
        return "on:\n  workflow_dispatch:\n    inputs:\n" + ("      runner:\n        type: choice\n" if runner else "") + ("      pg_runner:\n        type: choice\n" if pg else "")

    def git(self, *args):
        return cs.window_gate.git(self.repo, *args)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def git_stub(self, repo, *args, **kwargs):
        if args[0] == "push":
            self.calls.append(["git", *args])
            return subprocess.CompletedProcess(args, 0, "", "")
        return self.git_impl(repo, *args, **kwargs)

    def gh(self, argv, wait=True):
        self.calls.append(argv)
        self.assertIsInstance(argv, list)
        self.assertNotEqual(argv[1:3], ["run", "view"])
        if argv[1] == "api":
            endpoint = argv[2]
            if "/jobs?" in endpoint:
                result = {"jobs": self.jobs[int(endpoint.split("/runs/")[1].split("/")[0])]}
            elif "/actions/runs?" in endpoint:
                result = {"workflow_runs": self.runs}
            else:
                self.fail("unexpected API " + endpoint)
            self.assertNotIn("--slurp", argv)
            return 0, json.dumps(result), ""
        if argv[1:3] in (["workflow", "run"], ["run", "cancel"]):
            return 0, "", ""
        self.fail("unexpected gh invocation")

    def process(self, argv, **kwargs):
        if argv[0] == "fixture-gh":
            self.calls.append(argv)
            rid = int(argv[2].split("/runs/")[1].split("/")[0])
            return subprocess.CompletedProcess(argv, 0, self.logs[rid], b"")
        return REAL_RUN(argv, **kwargs)

    @staticmethod
    def zip(members):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, text in members.items():
                archive.writestr(name, text)
        return stream.getvalue()

    @staticmethod
    def job(name, conclusion="success", steps=None):
        return {"name": name, "labels": ["ubuntu-latest"], "conclusion": conclusion, "steps": steps or []}

    def candidate(self):
        return {"sha": self.sha, "tree": self.git("rev-parse", self.sha + "^{tree}").stdout.strip(),
                "ref": "ci-verify/shadow-" + self.sha[:12], "hosted": self.runs[:2], "reason": "", "owner": "fixture/shadow"}

    def events(self):
        return cs.fold.read_events()

    def start(self):
        self.assertEqual(cs.start(self.candidate(), self.repo, NOW), (0, True))
        return json.loads(cs._state_path(self.sha).read_text())

    def shadows(self, status="completed"):
        result = []
        for run in self.runs[:2]:
            shadow = {**run, "id": run["id"] + 100, "head_sha": self.sha,
                      "head_branch": self.candidate()["ref"], "status": status,
                      "html_url": run["html_url"] + "00"}
            result.append(shadow)
            self.jobs[shadow["id"]] = copy.deepcopy(self.jobs[run["id"]])
            self.logs[shadow["id"]] = self.logs[run["id"]]
        return result

    def test_candidates(self):
        cs._store({"sha": "f" * 40})
        with patch.object(cs.fold, "read_events", return_value=[{"type": "ci-shadow", "sha": "b" * 40}]):
            result = cs.candidates(NOW)
        self.assertEqual({c["sha"]: c["reason"] for c in result}, {
            self.sha: "", self.other: "hosted-missing:" + cs.window_gate.DASHBOARD_CHECK,
            "e" * 40: "object-missing"})
        self.assertEqual(next(c for c in result if c["sha"] == "e" * 40)["tree"], "unknown")
        report("candidates", [{"reason": c["reason"], "tree_known": c["tree"] != "unknown"} for c in result])

    def test_required_checks_raises(self):
        self.write("scripts/ci/predeploy_gate.sh", "# no array\n")
        self.commit()
        self.runs = self.runs[:2]
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(cs.main(["--json"]), 1)
        self.assertIn("required-checks-unreadable", error.getvalue())
        self.assertEqual(self.events(), [])
        self.assertFalse(any(c[0] == "git" for c in self.calls))
        report("required_checks_raises", {"exit": 1, "events": 0, "error": error.getvalue().strip()})

    def test_pass_lock(self):
        path = self.root / "state/ci-shadow.lock"
        path.parent.mkdir()
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = cs.run_pass(now=NOW)
        self.assertEqual(result, dict.fromkeys(("started", "collected", "recorded", "passed"), 0))
        self.assertEqual(self.calls, [])
        report("pass_lock", result)

    def test_state_atomic(self):
        state = {"sha": self.sha, "value": "old"}
        cs._store(state)
        with patch.object(cs.os, "replace", side_effect=InterruptedError("killed")):
            with self.assertRaises(InterruptedError):
                cs._store({**state, "value": "new"})
        self.assertEqual(json.loads(cs._state_path(self.sha).read_text()), state)
        self.assertEqual(len(list(cs._state_path(self.sha).parent.iterdir())), 1)
        report("state_atomic", {"previous_state_preserved": True, "partial_files": 0})

    def test_start_dispatch_argv(self):
        state = self.start()
        dispatches = [call for call in self.calls if call[:3] == ["gh", "workflow", "run"]]
        self.assertEqual(len(dispatches), 2)
        for call in dispatches:
            expected = ["gh", "workflow", "run", call[3], "--repo", "fixture/shadow", "--ref", state["ref"], "-f", "runner=self-hosted"]
            if call[3] == PYTHON:
                expected += ["-f", "full_run=true", "-f", "pg_runner=self-hosted"]
            self.assertEqual(call, expected)
        self.assertTrue(state["pg"])
        report("start_dispatch_argv", {"dispatches": dispatches})

    def test_ref_name(self):
        state = self.start()
        pushes = [call for call in self.calls if call[0] == "git"]
        self.assertEqual(pushes, [["git", "push", "--no-verify", "origin", self.sha + ":refs/heads/" + state["ref"]]])
        report("ref_name", {"prefix": state["ref"].split("-")[0], "pushes": len(pushes)})

    def test_input_gate_required(self):
        for path in (PYTHON, DASHBOARD):
            self.write(path, self.workflow(runner=False))
            self.sha = self.commit()
            self.calls.clear()
            recorded, started = cs.start(self.candidate(), self.repo, NOW)
            self.assertEqual((recorded, started), (1, False))
            self.assertEqual(self.events()[-1]["reason"], "no-runner-input:" + path)
            self.assertFalse(any(c[0] == "git" or c[:3] == ["gh", "workflow", "run"] for c in self.calls))
            self.write(path, self.workflow())
        candidate = self.candidate()
        candidate["sha"] = self.other
        candidate["hosted"] = []
        self.assertEqual(cs.start(candidate, self.repo, NOW), (1, False))
        self.assertEqual(self.events()[-1]["reason"], "no-runner-input:<none>")
        report("input_gate_required", {"terminal_events": len(self.events()), "pushes": 0})

    def test_ref_deleted_after(self):
        state = self.start()
        shadows = self.shadows()
        shadows[1]["status"] = "in_progress"
        self.assertEqual(cs.collect(state, self.repo, NOW, shadows), (0, False))
        self.assertFalse(any(call[-1].startswith(":refs/") for call in self.calls))
        shadows[1]["status"] = "completed"
        self.assertEqual(cs.collect(state, self.repo, NOW, shadows), (2, True))
        self.assertEqual(self.calls[-1], ["git", "push", "--no-verify", "origin", ":refs/heads/" + state["ref"]])
        self.assertFalse(cs._state_path(self.sha).exists())
        self.assertTrue(all(e["identical"] is True for e in self.events()))
        report("ref_deleted_after", {"recorded": 2, "early_delete": False})

    def test_shadow_timeout(self):
        state = self.start()
        result = cs.collect(state, self.repo, NOW + timedelta(hours=6), self.shadows("in_progress"))
        self.assertEqual(result, (2, True))
        self.assertEqual([e["reason"] for e in self.events()], ["shadow-timeout", "shadow-timeout"])
        self.assertTrue(all(e["identical"] is None for e in self.events()))
        self.assertEqual(len([c for c in self.calls if c[:3] == ["gh", "run", "cancel"]]), 2)
        self.assertFalse(cs._state_path(self.sha).exists())
        report("shadow_timeout", {"recorded": 2, "cancelled": 2, "reason": "shadow-timeout"})

    def test_pass_roundtrip(self):
        self.runs = self.runs[:2]
        result = cs.run_pass(now=NOW)
        self.assertEqual(result, {"started": 1, "collected": 0, "recorded": 0, "passed": 0})
        self.runs += self.shadows()
        result = cs.run_pass(now=NOW + timedelta(minutes=10))
        self.assertEqual(result, {"started": 0, "collected": 1, "recorded": 2, "passed": 0})
        again = cs.run_pass(now=NOW + timedelta(minutes=20))
        self.assertEqual(again, dict.fromkeys(result, 0))
        self.assertEqual([event["kind"] for event in self.events()], ["ci", "pg"])
        report("pass_roundtrip", {"collected": result, "repeat": again})

    def test_duplicate_run_still_open(self):
        state = self.start()
        shadows = self.shadows()
        older = {**shadows[0], "id": 500, "status": "in_progress", "created_at": "2026-10-18T10:00:00Z"}
        self.assertEqual(cs.collect(state, self.repo, NOW, shadows + [older]), (0, False))
        self.assertTrue(cs._state_path(self.sha).exists())
        report("duplicate_run_still_open", {"deleted": False})

    def test_setup_failure_without_logs(self):
        self.jobs[1] = [self.job(CHECK, "failure", [{"name": "Set up Python", "conclusion": "failure"}])]
        self.logs[1] = b"unavailable"
        result = cs.compare(cs._side("fixture/shadow", self.runs[:1], "ci"), side(**{CHECK: check()}))
        self.assertIs(result["identical"], False)
        self.assertIn("Set up Python", result["reason"])
        report("setup_failure_without_logs", result)

    def test_runs_api_unreadable(self):
        state = self.start()
        with patch.object(cs, "_jobs", side_effect=RuntimeError("jobs API unavailable")):
            self.assertEqual(cs.collect(state, self.repo, NOW, self.shadows()), (2, True))
        self.assertTrue(all(e["identical"] is None and e["reason"] == "runs-api-unreadable" for e in self.events()))
        report("runs_api_unreadable", {"identical": None, "reason": "runs-api-unreadable"})

    def test_inflight_cap(self):
        candidates = []
        for char in "abcd":
            candidate = self.candidate()
            candidate.update(sha=char * 40, ref="ci-verify/shadow-" + char * 12)
            candidates.append(candidate)
        def start(candidate, repo, now):
            cs._store(candidate)
            return 0, True
        with patch.object(cs, "candidates", return_value=candidates), patch.object(cs, "start", side_effect=start):
            result = cs.run_pass(now=NOW)
        self.assertEqual(result["started"], 3)
        self.assertFalse(cs._state_path("d" * 40).exists())
        report("inflight_cap", result)

    def test_kind_ci_excludes_pg(self):
        result = cs._side("fixture/shadow", self.runs[:1], "ci")
        self.assertEqual(set(result["checks"]), {CHECK})
        report("kind_ci_excludes_pg", {"checks": list(result["checks"])})

    def test_kind_ci_excludes_secret(self):
        self.jobs[1] += [self.job("Auth Coverage · Secret Scan · CVE Check"), self.job("Secret shape scan")]
        result = cs._side("fixture/shadow", self.runs[:1], "ci")
        self.assertEqual(set(result["checks"]), {CHECK})
        report("kind_ci_excludes_secret", {"checks": list(result["checks"])})

    def test_kind_pg_only_when_input(self):
        self.write(PYTHON, self.workflow(pg=False))
        self.sha = self.commit()
        state = self.start()
        self.assertFalse(state["pg"])
        self.assertEqual(cs.collect(state, self.repo, NOW, self.shadows()), (1, True))
        self.assertEqual([e["kind"] for e in self.events()], ["ci"])
        report("kind_pg_only_when_input", {"kinds": [e["kind"] for e in self.events()]})

    def test_logs_zip_parse(self):
        self.jobs[1] = [self.job(CHECK, "failure")]
        self.logs[1] = self.zip({CHECK + "/1_Run.txt":
            "NEW failed: tests/test_a.py::test_a\nNEW error: tests/test_b.py::test_b\n"
            "CHANGED: tests/test_c.py::test_c (baseline: fail)\n"
            "NEW failed: widget.test.ts::bad\nNEW suite-error: suite startup\n",
            "another-job/1_Run.txt": "NEW failed: tests/test_wrong.py::wrong\n"})
        with patch.object(cs.base_red, "parse_log", wraps=cs.base_red.parse_log) as parser:
            result = cs._side("fixture/shadow", self.runs[:1], "ci")
        ids = result["checks"][CHECK]["ids"]
        self.assertEqual(ids, {"tests/test_a.py::test_a", "tests/test_b.py::test_b", "tests/test_c.py::test_c", "widget.test.ts::bad", "suite startup"})
        self.assertEqual(parser.call_count, 1)
        report("logs_zip_parse", {"ids": sorted(ids), "binary_calls": len([c for c in self.calls if c[0] == "fixture-gh"])})

    def test_logs_corrupt(self):
        self.logs[1] = b"not a zip"
        result = cs._side("fixture/shadow", self.runs[:1], "ci")
        self.assertIsNone(result["checks"][CHECK]["ids"])
        report("logs_corrupt", {"ids": result["checks"][CHECK]["ids"]})

    def test_pg_log_context(self):
        self.jobs[1] = [self.job(cs.PG, "failure", [{"name": "Run parity", "number": 3, "conclusion": "failure"}])]
        self.logs[1] = self.zip({cs.PG + "/3_Run parity.txt":
                                 "FAILED tests/test_pg.py::test_isolated\nProcess completed with exit code 1\n"})
        result = cs._side("fixture/shadow", self.runs[:1], "pg")
        self.assertEqual(result["checks"][cs.PG]["ids"], {"tests/test_pg.py::test_isolated"})
        self.logs[1] = self.zip({cs.PG + "/3_Run parity.txt": "Process completed with exit code 1\n"})
        result = cs._side("fixture/shadow", self.runs[:1], "pg")
        self.assertEqual(result["checks"][cs.PG]["ids"], {"pg-parity::Run parity"})
        report("pg_log_context", {"ids": sorted(result["checks"][cs.PG]["ids"])})

    def test_logs_undetermined(self):
        self.logs[1] = self.zip({CHECK + "/1_Run.txt": "SUMMARY pytest check: FAIL new=2 changed=0\n"})
        result = cs._side("fixture/shadow", self.runs[:1], "ci")
        self.assertIsNone(result["checks"][CHECK]["ids"])
        report("logs_undetermined", {"ids": result["checks"][CHECK]["ids"]})

    def test_append_roundtrip(self):
        state = self.candidate()
        result = cs.compare(side(pytest=check()), side(pytest=check()))
        self.assertEqual(cs._record(state, "ci", result, self.shadows()), 1)
        cs.append("ci-shadow-passed", {"kind": "ci", "from": "a", "to": "b", "n": 5, "shas": ["a"]})
        self.assertEqual(cs._record(state, "ci", result), 0)
        code = "import fold,json; print(json.dumps(fold.read_events()))"
        read = REAL_RUN([sys.executable, "-c", code], cwd=REPO / "src", env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(read.returncode, 0, read.stderr)
        events = json.loads(read.stdout)
        self.assertEqual([e["type"] for e in events], ["ci-shadow", "ci-shadow-passed"])
        self.assertIs(events[0]["identical"], True)
        self.assertEqual(events[0]["subject"], "ci-shadow")
        self.assertTrue(all(e["actor"] == "ci-shadow" for e in events))
        raw = [json.loads(line) for line in (self.root / "events/L-ci-shadow-local.jsonl").read_text().splitlines()]
        self.assertFalse(any("actor" in e for e in raw))
        self.assertEqual(set(events[0]["by_check"]), {"pytest"})
        report("append_roundtrip", {"types": [e["type"] for e in events], "identical": events[0]["identical"], "fresh_process": True})

    def test_append_object_missing(self):
        state = self.candidate()
        state["tree"] = "unknown"
        self.assertEqual(cs._record(state, "ci", cs._unknown("object-missing")), 1)
        state["sha"] = self.other
        self.assertEqual(cs._record(state, "ci", cs._unknown("no-runner-input:" + PYTHON)), 1)
        self.assertEqual([e["reason"] for e in self.events()], ["object-missing", "no-runner-input:" + PYTHON])
        self.assertTrue(all(e["tree"] == "unknown" and e["identical"] is None for e in self.events()))
        report("append_object_missing", {"reasons": [e["reason"] for e in self.events()], "null_preserved": True})

    def test_dry_run_inert(self):
        self.runs = self.runs[:2]
        def forbidden(*args, **kwargs):
            self.fail("dry run attempted mutation")
        with patch.object(cs, "append", side_effect=forbidden), patch.object(cs, "_store", side_effect=forbidden):
            result = cs.run_pass(now=NOW, dry_run=True)
        self.assertEqual(result, dict.fromkeys(("started", "collected", "recorded", "passed"), 0))
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(self.events(), [])
        self.assertFalse(any(c[0] == "git" or c[:3] == ["gh", "workflow", "run"] for c in self.calls))
        report("dry_run_inert", result)

    def test_cli(self):
        self.runs = self.runs[:2]
        with patch.object(cs, "timestamp", return_value=datetime.now(timezone.utc)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cs.main(["--dry-run", "--json"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"started", "collected", "recorded", "passed"})
        help_result = REAL_RUN([str(REPO / "doit"), "--help"], capture_output=True, text=True)
        self.assertIn("doit ci-shadow [--dry-run] [--json]", help_result.stdout)
        report("cli", result)

    def test_cli_subprocess(self):
        fake = self.root / "gh-fixture"
        fake.write_text("#!/usr/bin/python3\nimport json,sys,time\n"
                        "if sys.argv[1:3] == ['api','rate_limit']:\n"
                        " print(json.dumps({'resources': {k: {'remaining':5000, 'limit':5000, 'reset':time.time()+3600} for k in ('core','graphql')}}))\n"
                        "elif sys.argv[1] == 'api' and '/actions/runs?' in sys.argv[2]:\n"
                        " print(json.dumps({'workflow_runs':[]}))\n"
                        "else: sys.exit(99)\n")
        fake.chmod(0o755)
        env = {**os.environ, "DOIT_REAL_GH": str(fake), "GH_LIMIT_STATE": str(self.root / "gh-quota.json")}
        result = REAL_RUN([str(REPO / "doit"), "ci-shadow", "--dry-run", "--json"],
                          env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual(body, dict.fromkeys(("started", "collected", "recorded", "passed"), 0))
        self.assertEqual(self.events(), [])
        report("cli_subprocess", body)


if __name__ == "__main__":
    unittest.main()
