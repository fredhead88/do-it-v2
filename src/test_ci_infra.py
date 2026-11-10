#!/usr/bin/env python3
"""Hermetic setup-failure classification and whole-run retry regressions."""
import copy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

import ci_infra
import window_gate


# Reduced metadata recorded for run 42466615977: no logs or credentials.
RECORDED_JOBS = [
    {"name": f"pytest — shard {number} of 4", "conclusion": "failure", "steps": [
        {"name": "Set up job", "conclusion": "success"},
        {"name": "Run actions/checkout@v4", "conclusion": "success"},
        {"name": "Install uv", "conclusion": "failure"},
        {"name": "Install API package (editable, dev extra)", "conclusion": "skipped"},
        {"name": "Run tests", "conclusion": "skipped"},
    ]} for number in (1, 2)
] + [
    {"name": "pytest", "conclusion": "failure", "steps": [
        {"name": "Shards concluded", "conclusion": "failure"},
    ]},
    {"name": "pytest — shard ${{ matrix.shard }}", "conclusion": "skipped"},
]
URL = "https://github.com/o/r/actions/runs/42466615977"
NEXT_URL = "https://github.com/o/r/actions/runs/42466615978"


def verdict(state="red", url=URL, nodes=None, checks=None):
    return dict(state=state, url=url, nodes=nodes or [],
                doc={"checks": checks or {}}, why="gate exit 1")


class AC1(unittest.TestCase):
    def test_recorded_setup_only(self):
        matched, why = ci_infra.setup_only(RECORDED_JOBS)
        self.assertTrue(matched)
        for number in (1, 2):
            self.assertIn(f"pytest — shard {number} of 4: Install uv", why)


class AC2(unittest.TestCase):
    def test_test_failure_mixed_with_setup_failure(self):
        jobs = copy.deepcopy(RECORDED_JOBS)
        jobs[1]["steps"][2]["conclusion"] = "success"
        jobs[1]["steps"][-1]["conclusion"] = "failure"
        self.assertFalse(ci_infra.setup_only(jobs)[0])

    def test_other_failed_job(self):
        self.assertFalse(ci_infra.setup_only(RECORDED_JOBS + [
            {"name": "Dashboard PR Quality", "conclusion": "failure", "steps": []}
        ])[0])

    def test_missing_or_empty_jobs(self):
        for jobs in (None, [], [{"name": "pytest", "conclusion": "failure"}]):
            with self.subTest(jobs=jobs):
                self.assertFalse(ci_infra.setup_only(jobs)[0])

    def test_candidate_breakable_first_failure(self):
        for name in ("Verify test dependencies importable", "Install API package (editable, dev extra)"):
            jobs = copy.deepcopy(RECORDED_JOBS)
            jobs[0]["steps"][2]["name"] = name
            with self.subTest(step=name):
                self.assertFalse(ci_infra.setup_only(jobs)[0])

    def test_transient_allowlist(self):
        for name in ("Install uv", "Run actions/setup-python@v5", "Run actions/checkout@v4",
                     "Run actions/setup-node@v4", "Run actions/download-artifact@v4"):
            jobs = copy.deepcopy(RECORDED_JOBS)
            jobs[0]["steps"][2]["name"] = name
            with self.subTest(step=name):
                self.assertTrue(ci_infra.setup_only(jobs)[0])

    def test_tests_must_be_later_and_skipped(self):
        for mode in ("absent", "earlier", "success", "failure", "no-first-failure"):
            jobs = copy.deepcopy(RECORDED_JOBS)
            steps = jobs[0]["steps"]
            if mode == "absent":
                steps.pop()
            elif mode == "earlier":
                steps.insert(0, steps.pop())
            elif mode == "no-first-failure":
                steps[2]["conclusion"] = "success"
            else:
                steps[-1]["conclusion"] = mode
            with self.subTest(mode=mode):
                self.assertFalse(ci_infra.setup_only(jobs)[0])


class AC3(unittest.TestCase):
    def invoke(self, first, second=None, jobs=RECORDED_JOBS):
        with patch.object(window_gate, "_verify_once", side_effect=[first, second]) as once, \
             patch.object(ci_infra, "jobs_for", return_value=jobs) as fetch, \
             patch.object(window_gate, "ev") as event:
            result = window_gate.verify(Path("/fixture"), "candidate", ["pytest"], "window", rehearsal=True)
        return result, once, fetch, event

    def assert_event(self, event):
        event.assert_called_once_with("ci-setup-rerun", "window", window="window",
                                      candidate="candidate", run_url=URL,
                                      why=ci_infra.setup_only(RECORDED_JOBS)[1])

    def test_setup_red_then_green(self):
        result, once, fetch, event = self.invoke(verdict(), verdict("green", NEXT_URL))
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["setup_rerun_of"], URL)
        self.assertEqual(once.call_args_list, [
            call(Path("/fixture"), "candidate", ["pytest"], "window", rehearsal=True)
        ] * 2)
        fetch.assert_called_once_with(Path("/fixture"), URL)
        self.assert_event(event)

    def test_two_setup_failures_hold(self):
        result, once, fetch, event = self.invoke(verdict(), verdict(url=NEXT_URL))
        self.assertEqual(result["state"], "undetermined")
        self.assertTrue(result["why"].startswith("infra: CI setup failed twice: "))
        self.assertEqual(result["setup_rerun_of"], URL)
        self.assertEqual(once.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assert_event(event)

    def test_no_retry_or_lookup_when_ineligible(self):
        for first in (verdict(url=""), verdict(nodes=["test_x.py::test_red"]),
                      verdict("green"), verdict(checks={"a": {"run_id": 1}, "b": {"run_id": 2}})):
            with self.subTest(first=first):
                result, once, fetch, event = self.invoke(first)
                self.assertIs(result, first)
                self.assertEqual(once.call_count, 1)
                fetch.assert_not_called()
                event.assert_not_called()

    def test_non_setup_failure_unchanged(self):
        for jobs in (None, [], [{"name": "pytest", "conclusion": "failure"}]):
            first = verdict()
            result, once, fetch, event = self.invoke(first, jobs=jobs)
            self.assertIs(result, first)
            self.assertEqual(once.call_count, 1)
            event.assert_not_called()

    def test_second_test_failure_remains_red(self):
        second = verdict(url=NEXT_URL, nodes=["test_x.py::test_red"])
        result, once, fetch, event = self.invoke(verdict(), second)
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["nodes"], second["nodes"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(once.call_count, 2)
        self.assert_event(event)

    def test_undetermined_can_retry_and_same_run_ids_are_unambiguous(self):
        first = verdict("undetermined", checks={"a": {"run_id": 1}, "b": {"run_id": "1"}})
        result, once, fetch, event = self.invoke(first, verdict("green", NEXT_URL))
        self.assertEqual(result["state"], "green")
        self.assert_event(event)

    def test_signature_preserved(self):
        self.assertEqual(inspect.signature(window_gate.verify), inspect.signature(window_gate._verify_once))


class JobsFor(unittest.TestCase):
    def test_fetch_jobs_from_run_url(self):
        with patch.object(ci_infra.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"jobs": RECORDED_JOBS}), "")) as run:
            self.assertEqual(ci_infra.jobs_for(Path("/fixture"), URL), RECORDED_JOBS)
        run.assert_called_once_with(["gh", "run", "view", "42466615977", "--json", "jobs"],
                                    cwd=Path("/fixture"), capture_output=True, text=True, timeout=60)

    def test_errors_are_not_setup_evidence(self):
        for output, code in (("bad-json", 0), ("{}", 0), ('{"jobs":null}', 0),
                             ('{"jobs":{}}', 0), ('{"jobs":[]}', 1)):
            with patch.object(ci_infra.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], code, output, "")):
                self.assertIsNone(ci_infra.jobs_for(Path("/fixture"), URL))
        for error in (OSError(), subprocess.TimeoutExpired("gh", 60)):
            with patch.object(ci_infra.subprocess, "run", side_effect=error):
                self.assertIsNone(ci_infra.jobs_for(Path("/fixture"), URL))
        with patch.object(ci_infra.subprocess, "run") as run:
            for url in ("", "https://github.com/o/r/actions/runs/not-a-number", None):
                self.assertIsNone(ci_infra.jobs_for(Path("/fixture"), url))
            run.assert_not_called()


if __name__ == "__main__":
    success = True
    for group in (AC1, AC2, AC3, JobsFor):
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(group))
        body = json.dumps(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors)), sort_keys=True)
        print(json.dumps(dict(url="cli://test_ci_infra.py/" + group.__name__,
                              status=0 if result.wasSuccessful() else 1,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)), flush=True)
        success &= result.wasSuccessful()
    raise SystemExit(0 if success else 1)
