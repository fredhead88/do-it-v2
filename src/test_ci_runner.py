#!/usr/bin/env python3
"""Hermetic runner-switch regressions, with real ghlimit and ledger processes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parents[1]
DOIT = REPO / "doit"
VARIABLE = "CI_SELF_HOSTED"
PG_VARIABLE = "CI_PG_SELF_HOSTED"
SLUG = "fixture/runner-switch"
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

FAKE_GH = r'''#!/usr/bin/python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
config_path = pathlib.Path(os.environ["FAKE_GH_CONFIG"])
config = json.loads(config_path.read_text())
with open(os.environ["FAKE_GH_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\n")
if "--slurp" in args:
    print("unknown flag: --slurp", file=sys.stderr)
    sys.exit(1)
if args == ["api", "rate_limit"]:
    print(json.dumps({"resources": {k: {"limit": 5000, "remaining": 5000,
        "reset": time.time() + 3600} for k in ("core", "graphql")}}))
elif args[:2] == ["variable", "set"]:
    if config.get("write_rc", 0):
        sys.exit(config["write_rc"])
    if config.get("write_sticks", True):
        config["variables"][args[2]] = args[args.index("--body") + 1]
        config_path.write_text(json.dumps(config))
elif args[0] == "api":
    endpoint = args[1].split("?", 1)[0]
    if "/actions/variables/" in endpoint:
        response = config["variables"][endpoint.rsplit("/", 1)[1]]
    elif endpoint == "repos/fixture/runner-switch":
        response = config.get("repo", "fixture/runner-switch")
    else:
        response = config.get("api", {}).get(endpoint)
        if response is None:
            print("unexpected endpoint: " + endpoint, file=sys.stderr)
            sys.exit(9)
        if isinstance(response, dict) and "rc" not in response:
            response = json.dumps(response)
        elif isinstance(response, list):
            response = "\n".join(json.dumps(page) for page in response)
    if isinstance(response, dict):
        print(response.get("out", ""), end="")
        print(response.get("err", ""), file=sys.stderr, end="")
        sys.exit(response.get("rc", 0))
    print(response)
else:
    sys.exit(9)
'''


class RunnerSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ci-runner-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "gh"
        self.fake.write_text(FAKE_GH)
        self.fake.chmod(0o755)
        self.config_path = self.root / "config.json"
        self.calls_path = self.root / "calls.jsonl"
        env = {"HOME": str(self.root / "home"), "DOIT_ROOT": str(self.root / "doit"),
               "DOIT_PROJECT": "do-it-v2", "CI_RUNNER_REPO": SLUG,
               "CI_RUNNER_GH": str(self.fake), "DOIT_REAL_GH": str(self.fake),
               "GH_LIMIT_STATE": str(self.root / "quota.json"),
               "FAKE_GH_CONFIG": str(self.config_path), "FAKE_GH_CALLS": str(self.calls_path)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        import ci_runner
        import fold
        self.runner = ci_runner
        self.fold = fold
        self.ledger = Path(env["DOIT_ROOT"]) / "events"
        fold_patch = patch.multiple(fold, ROOT=Path(env["DOIT_ROOT"]), EVENTS=self.ledger,
                                   BOARD=Path(env["DOIT_ROOT"]) / "board.md", PROJECT="do-it-v2")
        fold_patch.start()
        self.addCleanup(fold_patch.stop)
        self.config = {"variables": {VARIABLE: "true", PG_VARIABLE: "false"}, "api": {
            f"repos/{SLUG}/actions/runners": {"runners": [
                {"name": "as-ci-1", "busy": False, "status": "online", "labels": [{"name": "as-ci"}]}]},
            f"repos/{SLUG}/actions/runs": {"workflow_runs": []}}}
        self.save()
        self.quota()

    def save(self):
        self.config_path.write_text(json.dumps(self.config))

    def quota(self, remaining=5000):
        Path(os.environ["GH_LIMIT_STATE"]).write_text(json.dumps({
            **{k: {"limit": 5000, "remaining": remaining, "reset": time.time() + 3600}
               for k in ("core", "graphql")}, "checked_at": time.time(), "spent_since_check": 0}))

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []

    def writes(self):
        return [args for args in self.calls() if args[:2] == ["variable", "set"]]

    def rows(self):
        return self.fold.fold(self.fold.read_events())[3].get("ci-runner", [])

    def report(self, case, body):
        raw = json.dumps(body, sort_keys=True)
        print(case, json.dumps({"url": "python://ci_runner/" + case, "status": 200,
              "body_sha256": hashlib.sha256(raw.encode()).hexdigest(), "body_excerpt": raw}))

    def append_shadow(self, kind="ci", project="do-it-v2"):
        env = dict(os.environ, DOIT_LEDGER_FILE="L-ci-shadow-local.jsonl", DOIT_PROJECT=project)
        result = subprocess.run([str(DOIT), "append", "ci-shadow-passed", "ci-shadow",
                                 f"kind={kind}", "from=a", "to=b", "n:=2", 'shas=["a","b"]'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def fresh_fold(self):
        direct = subprocess.run([sys.executable, str(REPO / "src/fold.py")],
                                capture_output=True, text=True)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        code = "import fold,json; f=fold.fold(fold.read_events()); print(json.dumps({'kept':f[3], 'ignored':f[2]}))"
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO / "src",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_state(self):
        self.assertEqual(self.runner.state(), {VARIABLE: "true", PG_VARIABLE: "false"})
        self.report("AC1(a)", self.runner.state())
        self.config["variables"][VARIABLE] = {"rc": 1, "err": "HTTP 404 (Not Found)"}
        self.save()
        self.assertEqual(self.runner.state(), {VARIABLE: "false", PG_VARIABLE: "false"})
        self.report("AC1(b)", self.runner.state())
        for response in ({"rc": 1, "err": "HTTP 500"}, "", "TRUE", "null", "other"):
            self.config["variables"][VARIABLE] = response
            self.save()
            result = self.runner.state()
            self.assertEqual(result, {VARIABLE: None, PG_VARIABLE: "false"})
            self.report("AC1(c)", result)
        self.config["variables"][VARIABLE] = {"rc": 1, "err": "HTTP 404"}
        self.config["repo"] = {"rc": 1, "err": "HTTP 404"}
        self.save()
        self.assertIsNone(self.runner.state()[VARIABLE])

    def test_health(self):
        runners = f"repos/{SLUG}/actions/runners"
        runs = f"repos/{SLUG}/actions/runs"
        result = self.runner.healthy(NOW)
        self.assertEqual((result["healthy"], result["online"], result["stuck_queued"]), (True, 1, 0))
        self.report("AC2(a)", result)
        for case, runner in (("b", {"name": "as-ci-1", "busy": False, "status": "offline", "labels": [{"name": "as-ci"}]}),
                             ("c", {"name": "as-ci-1", "busy": False, "status": "online", "labels": [{"name": "other"}]})):
            self.config["api"][runners] = {"runners": [runner]}
            self.save()
            result = self.runner.healthy(NOW)
            self.assertIs(result["healthy"], False)
            self.assertEqual(result["online"], 0)
            self.assertIn("as-ci", result["reason"])
            self.report(f"AC2({case})", result)
        self.config["api"][runners] = [{"runners": []}, {"runners": [
            {"name": "as-ci-1", "busy": False, "status": "online", "labels": [{"name": "as-ci"}]}]}]
        self.config["api"][runs] = [{"workflow_runs": []}, {"workflow_runs": [{"id": 42}]}]
        jobs = f"repos/{SLUG}/actions/runs/42/jobs"
        for minutes, expected in ((16, False), (15, True), (14, True)):
            self.config["api"][jobs] = {"jobs": [{"status": "queued", "labels": ["self-hosted"],
                "created_at": (NOW - timedelta(minutes=minutes)).isoformat()}]}
            self.save()
            result = self.runner.healthy(NOW)
            self.assertIs(result["healthy"], expected)
            self.assertEqual(result["stuck_queued"], int(not expected))
            self.report("AC2(d)", result)
        self.config["api"][jobs]["jobs"][0]["labels"] = ["ubuntu-latest"]
        self.save()
        self.assertIs(self.runner.healthy(NOW)["healthy"], True)
        for response in ({"rc": 1, "err": "unreadable"}, "not json", {}, {"runners": None}):
            self.config["api"][runners] = response
            self.save()
            result = self.runner.healthy(NOW)
            self.assertEqual((result["healthy"], result["online"], result["stuck_queued"]), (None, 0, 0))
            self.report("AC2(e)", result)
        self.config["api"][runners] = {"runners": [{"name": "as-ci-1", "busy": False, "status": "online", "labels": [{"name": "as-ci"}]}]}
        for endpoint in (runs, jobs):
            prior = self.config["api"][endpoint]
            self.config["api"][endpoint] = {"rc": 1, "err": "unreadable"}
            self.save()
            result = self.runner.healthy(NOW)
            self.assertIsNone(result["healthy"])
            self.report("AC2(f)", result)
            self.config["api"][endpoint] = prior

    def test_ac1_runner_pages_and_paginated_calls(self):
        a = {"name": "as-ci-A", "status": "online", "busy": True}
        b = {"name": "as-ci-B", "status": "offline", "busy": False}
        self.config["api"][f"repos/{SLUG}/actions/runners"] = [
            {"runners": [{**a, "labels": [{"name": "as-ci"}]}]},
            {"runners": [{**b, "labels": [{"name": "as-ci"}]},
                         {"name": "other", "status": "online", "busy": False,
                          "labels": [{"name": "other"}]}]}]
        self.config["api"][f"repos/{SLUG}/actions/runs"] = {"workflow_runs": [{"id": 42}]}
        self.config["api"][f"repos/{SLUG}/actions/runs/42/jobs"] = [{"jobs": []}, {"jobs": []}]
        self.save()
        result = self.runner.healthy(NOW)
        self.assertEqual(result["runners"], [a, b])
        self.assertEqual((result["healthy"], result["online"], result["stuck_queued"]), (True, 1, 0))
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual([args[1].split("?", 1)[0] for args in calls], [
            f"repos/{SLUG}/actions/runners", f"repos/{SLUG}/actions/runs",
            f"repos/{SLUG}/actions/runs/42/jobs"])
        for args in calls:
            self.assertIn("--paginate", args)
            self.assertNotIn("--slurp", args)
        self.report("ac1-runner-pages", {"health": result, "calls": calls})
        rejected = subprocess.run([str(self.fake), "api", "unused", "--slurp"],
                                  capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("unknown flag: --slurp", rejected.stderr)

    def test_ac1_queued_run_and_job_pages(self):
        self.config["api"][f"repos/{SLUG}/actions/runs"] = [
            {"workflow_runs": [{"id": 41}]}, {"workflow_runs": [{"id": 42}]}]
        self.config["api"][f"repos/{SLUG}/actions/runs/41/jobs"] = {"jobs": []}
        self.config["api"][f"repos/{SLUG}/actions/runs/42/jobs"] = [
            {"jobs": []}, {"jobs": [{"status": "queued", "labels": ["self-hosted"],
                "created_at": (NOW - timedelta(minutes=16)).isoformat()}]}]
        self.save()
        result = self.runner.healthy(NOW)
        self.assertIs(result["healthy"], False)
        self.assertEqual(result["stuck_queued"], 1)
        for args in self.calls():
            self.assertIn("--paginate", args)
            self.assertNotIn("--slurp", args)
        self.report("ac1-queued-pages", result)

    def test_ac2_malformed_runners(self):
        good = self.config["api"][f"repos/{SLUG}/actions/runners"]["runners"][0]
        other = {"name": "other", "status": "online", "busy": False,
                 "labels": [{"name": "other"}]}
        cases = {"exit": {"rc": 1}, "empty": "  ", "missing-key": {},
                 "non-list": {"runners": {}}, "trailing-garbage": '{"runners": []} garbage',
                 "non-object-page": "null", "non-object-row": {"runners": [None]},
                 "missing-later-page-key": [{"runners": [good]}, {}]}
        for field, value in (("name", None), ("status", 1), ("busy", 0)):
            bad = dict(other)
            if field == "name":
                del bad[field]
            else:
                bad[field] = value
            cases[field] = {"runners": [good, bad]}
        for case, response in cases.items():
            with self.subTest(case=case):
                self.config["api"][f"repos/{SLUG}/actions/runners"] = response
                self.save()
                result = self.runner.healthy(NOW)
                self.assertIsNone(result["healthy"])
                self.assertEqual((result["online"], result["stuck_queued"], result["runners"]), (0, 0, []))
                self.assertTrue(result["reason"].startswith("runner health undetermined"))
                self.report("ac2-" + case, result)

    def test_ac2_unreadable_runs_and_jobs(self):
        runs, jobs = f"repos/{SLUG}/actions/runs", f"repos/{SLUG}/actions/runs/42/jobs"
        for endpoint, key in ((runs, "workflow_runs"), (jobs, "jobs")):
            for response in ({"rc": 1}, "", {}, {key: None}, '{"' + key + '": []} garbage'):
                with self.subTest(endpoint=endpoint, response=response):
                    self.config["api"][runs] = {"workflow_runs": [{"id": 42}]}
                    self.config["api"][jobs] = {"jobs": []}
                    self.config["api"][endpoint] = response
                    self.save()
                    result = self.runner.healthy(NOW)
                    self.assertIsNone(result["healthy"])
                    self.assertEqual((result["online"], result["stuck_queued"], result["runners"]), (0, 0, []))
                    self.assertTrue(result["reason"].startswith("runner health undetermined"))
                    self.report("ac2-" + key, result)

    def test_ac3_health_and_queue_boundary(self):
        runners = f"repos/{SLUG}/actions/runners"
        result = self.runner.healthy(NOW)
        self.assertEqual((result["healthy"], result["online"], result["stuck_queued"]), (True, 1, 0))
        self.report("ac3-online", result)
        self.config["api"][runners]["runners"][0]["status"] = "offline"
        self.save()
        result = self.runner.healthy(NOW)
        self.assertIs(result["healthy"], False)
        self.assertEqual((result["online"], result["stuck_queued"]), (0, 0))
        self.assertIn("as-ci", result["reason"])
        self.assertEqual(result["runners"], [{"name": "as-ci-1", "status": "offline", "busy": False}])
        self.report("ac3-offline", result)
        self.config["api"][runners]["runners"][0]["status"] = "online"
        self.config["api"][f"repos/{SLUG}/actions/runs"] = {"workflow_runs": [{"id": 42}]}
        for minutes, expected in ((16, False), (15, True), (14, True)):
            self.config["api"][f"repos/{SLUG}/actions/runs/42/jobs"] = {"jobs": [{
                "status": "queued", "labels": ["self-hosted"],
                "created_at": (NOW - timedelta(minutes=minutes)).isoformat()}]}
            self.save()
            result = self.runner.healthy(NOW)
            self.assertIs(result["healthy"], expected)
            self.assertEqual((result["online"], result["stuck_queued"]), (1, int(not expected)))
            self.report(f"ac3-{minutes}-minutes", result)

    def test_ac3_billing_health_consumer(self):
        import gh_bill
        with patch.dict(os.environ, {"GH_BILL_CI_RUNNER": ""}):
            for status, expected in (("online", True), ("offline", False), (None, None)):
                if status is None:
                    self.config["api"][f"repos/{SLUG}/actions/runners"] = {"rc": 1}
                else:
                    self.config["api"][f"repos/{SLUG}/actions/runners"]["runners"][0]["status"] = status
                self.save()
                result = gh_bill._runner_inputs(NOW.date())
                self.assertIs(result[0], self.runner)
                self.assertIs(result[2], expected)
                self.assertEqual(result[-1], "")
                self.report("ac3-billing-" + str(status), {"health": result[2]})

    def test_ac4_status_text_json_and_quota(self):
        expected = [{"name": "as-ci-A", "status": "online", "busy": True},
                    {"name": "as-ci-B", "status": "offline", "busy": False}]
        self.config["api"][f"repos/{SLUG}/actions/runners"] = {
            "runners": [{**runner, "labels": [{"name": "as-ci"}]} for runner in expected]}
        self.save()
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                if exhausted:
                    self.quota(0)
                text = subprocess.run([str(DOIT), "ci-runner", "status"], capture_output=True, text=True)
                self.assertEqual(text.returncode, 0, text.stderr)
                lines = text.stdout.splitlines()
                self.assertTrue(lines[2].startswith("healthy: "))
                self.assertEqual(lines[3:], [] if exhausted else [
                    "runner as-ci-A online busy=True", "runner as-ci-B offline busy=False"])
                status = subprocess.run([str(DOIT), "ci-runner", "status", "--json"],
                                        capture_output=True, text=True)
                self.assertEqual(status.returncode, 0, status.stderr)
                health = json.loads(status.stdout)["health"]
                self.assertEqual(health["runners"], [] if exhausted else expected)
                self.assertIs(health["healthy"], None if exhausted else True)
                self.report("ac4-quota-" + str(exhausted), {"text": text.stdout, "health": health})

    def test_shadow_selection(self):
        events = [{"type": "ci-shadow-passed", "kind": kind, "ts": ts, "subject": "ci-shadow"}
                  for kind, ts in (("ci", "2026-10-17T12:00:00Z"), ("pg", "2026-10-18T12:00:00Z"),
                                   ("ci", "2026-10-18T11:00:00+00:00"))]
        self.assertEqual(self.runner.shadow_passed(events, "ci"), events[2])
        self.report("AC3(latest)", events[2])
        self.assertEqual(self.runner.shadow_passed(events, "pg"), events[1])
        self.report("AC3(kind)", events[1])
        self.assertIsNone(self.runner.shadow_passed([], "ci"))
        self.report("AC3(absent)", None)
        self.assertIsNone(self.runner.shadow_passed([{"type": "other", "kind": "ci"}], "ci"))
        self.report("AC3(type)", None)

    def test_shadow_refusals_and_validation(self):
        for variable, kind in ((VARIABLE, None), (VARIABLE, "pg"), (PG_VARIABLE, "ci")):
            if kind:
                self.append_shadow(kind)
            if variable == PG_VARIABLE:
                (self.ledger / "L-ci-shadow-local.jsonl").unlink()
                self.append_shadow("ci")
            result = self.runner.set(variable, "true", why="t", trigger="manual")
            self.assertIs(result["ok"], False)
            self.assertEqual(self.writes(), [])
            self.assertEqual(self.rows(), [])
            self.report("AC4(refused)", {**result, "variable_set_calls": 0})
        for variable, value, trigger in (("BAD", "true", "manual"), (VARIABLE, "true", "bad"),
                                         (VARIABLE, "TRUE", "manual")):
            before = len(self.calls())
            with self.assertRaises(SystemExit) as caught:
                self.runner.set(variable, value, why="t", trigger=trigger)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(len(self.calls()), before)
            self.report("AC4(usage)", {"gh_calls": 0, "exit": 2})

    def test_false_and_failed_verification(self):
        self.config["variables"][VARIABLE] = {"rc": 1, "err": "unreadable"}
        self.save()
        why = 'operator text\nquotes " = $(no-shell) --inbox'
        result = self.runner.set(VARIABLE, "false", why=why, trigger="rollback")
        self.assertIs(result["ok"], True)
        self.assertEqual(len(self.writes()), 1)
        row, = self.rows()
        self.assertEqual({k: row[k] for k in ("variable", "from", "to", "why", "trigger")},
                         {"variable": VARIABLE, "from": "unset", "to": "false", "why": why, "trigger": "rollback"})
        self.report("AC5(applied)", result)
        self.config["variables"][VARIABLE] = "true"
        self.config["write_sticks"] = False
        self.save()
        result = self.runner.set(VARIABLE, "false", why="t", trigger="manual")
        self.assertIs(result["ok"], False)
        self.assertEqual(len(self.rows()), 1)
        self.report("AC5(unverified)", result)

    def test_real_ledger_round_trip(self):
        self.append_shadow()
        self.config["variables"][VARIABLE] = "false"
        self.save()
        result = self.runner.set(VARIABLE, "true", why="t", trigger="manual")
        self.assertIs(result["ok"], True)
        self.report("AC6(a)", result)
        raw_row = json.loads((self.ledger / "L-ci-runner-local.jsonl").read_text())
        self.assertNotIn("actor", raw_row)
        self.assertEqual(raw_row["subject"], "ci-runner")
        fresh = self.fresh_fold()
        self.assertEqual(len(fresh["kept"]["ci-runner"]), 1)
        self.assertEqual(fresh["ignored"], [])
        self.report("AC6(b)", fresh["kept"]["ci-runner"])
        shadow_path = self.ledger / "L-ci-shadow-local.jsonl"
        row = shadow_path.read_text()
        shadow_path.unlink()
        bad_path = self.ledger / "L-thinker-local.jsonl"
        bad_path.write_text(row)
        fresh = self.fresh_fold()
        self.assertEqual(len(fresh["ignored"]), 1)
        result = self.runner.set(VARIABLE, "true", why="t", trigger="manual")
        self.assertIs(result["ok"], False)
        before = bad_path.read_bytes()
        strict = subprocess.run([str(DOIT), "append", "ci-shadow-passed", "ci-shadow", "kind=ci", "from=a", "to=b"],
                                env=dict(os.environ, DOIT_LEDGER_FILE=bad_path.name), capture_output=True, text=True)
        self.assertNotEqual(strict.returncode, 0)
        self.assertEqual(bad_path.read_bytes(), before)
        self.assertEqual(len(self.writes()), 1)
        self.report("AC6(c)", {"refused": result, "ignored": len(fresh["ignored"]), "strict_exit": strict.returncode})

    def test_project_filter_and_pg(self):
        self.append_shadow("pg", project="another-project")
        self.assertFalse(self.runner.set(PG_VARIABLE, "true", why="t", trigger="bill-flip")["ok"])
        self.append_shadow("pg")
        self.assertTrue(self.runner.set(PG_VARIABLE, "true", why="t", trigger="bill-flip")["ok"])

    def test_quota_and_write_failure(self):
        self.quota(0)
        self.assertEqual(self.runner.state(), {VARIABLE: None, PG_VARIABLE: None})
        self.assertIsNone(self.runner.healthy(NOW)["healthy"])
        self.assertFalse(self.runner.set(VARIABLE, "false", why="t", trigger="rollback")["ok"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.rows(), [])
        self.quota()
        self.config["write_rc"] = 1
        self.save()
        self.assertFalse(self.runner.set(VARIABLE, "false", why="t", trigger="rollback")["ok"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.rows(), [])

    def test_cli(self):
        def cli(*args):
            return subprocess.run([str(DOIT), "ci-runner", *args], capture_output=True, text=True)
        status = cli("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(set(json.loads(status.stdout)), {"state", "health"})
        self.quota(0)
        status = cli("status", "--json")
        self.assertEqual(status.returncode, 0)
        undetermined = json.loads(status.stdout)
        self.assertEqual(undetermined["state"], {VARIABLE: None, PG_VARIABLE: None})
        self.assertIsNone(undetermined["health"]["healthy"])
        self.quota()
        refused = cli("set", VARIABLE, "true", "--why", "x")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("ci", refused.stderr)
        bad = cli("set", "BAD", "true", "--why", "x")
        self.assertEqual(bad.returncode, 2)
        applied = cli("set", VARIABLE, "false", "--why", "rollback")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.rows()[0]["trigger"], "manual")
        help_result = subprocess.run([str(DOIT), "help"], capture_output=True, text=True)
        self.assertIn("ci-runner status", help_result.stdout)
        self.assertIn("ci-runner set", help_result.stdout)
        self.report("AC7", undetermined)


if __name__ == "__main__":
    unittest.main()
