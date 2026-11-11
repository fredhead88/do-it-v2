#!/usr/bin/env python3
"""Offline snapshot-reader regressions for master full-suite evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
import threading
import tomllib
from unittest.mock import patch

import test_base_red as fixtures

br = fixtures.br
NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
PY = "api/tests/test_full.py::test_red"
PG = "pg-parity::scratch checks"
JS = "dashboard.test.ts::red"


WORKFLOW = """name: Python Tests
on:
  workflow_dispatch:
    inputs:
      suite: {type: choice, options: [window, nightly, full]}
      full_run: {type: boolean, default: false}
      select_base: {type: string, default: ''}
      rerun_ids: {type: string, default: ''}
      checks: {type: string, default: ''}
concurrency:
  group: ${{ format('{0}-{1}-{2}-{3}', github.workflow, github.ref, inputs.suite || 'window', inputs.full_run || false) }}
  cancel-in-progress: true
jobs:
  request:
    steps:
      - env:
          SELECT_BASE: ${{ inputs.select_base }}
          RERUN_IDS: ${{ inputs.rerun_ids }}
        run: python3 scripts/ci/ci_selection.py request --out request.json
"""


class MasterFixture(fixtures.SelectiveFixture):
    def setUp(self):
        super().setUp()
        import master_full
        self.mf = master_full
        target = self.repo.with_name("albert-scott")
        self.repo.rename(target)
        self.repo = target
        self.workflow_path = self.repo / ".github/workflows/python-tests.yml"
        self.workflow_path.write_text(WORKFLOW)
        self.commit("workflow")
        (self.repo / "api/tests").mkdir(parents=True)
        (self.repo / "api/tests/test_canary.py").write_text("def test_canary(): pass\n")
        self.base = self.sha
        self.commit("Python change")
        self.runs = []
        self.dispatches = []
        self.clock = NOW
        self.artifacts = {}
        self.paths = []
        self.api_calls = []
        self.all_runs = None
        self.listing_error = False
        self.run_reply = None
        self.no_artifact = set()
        self.stack = fixtures.contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(master_full, "_now", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(master_full, "_tip", side_effect=lambda repo: self.sha))
        self.stack.enter_context(patch.object(br, "_api", side_effect=self.api))
        self.stack.enter_context(patch.object(br.ghlimit, "run", side_effect=self.transport))

    def commit(self, subject):
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", subject)
        self.sha = self.git("rev-parse", "HEAD")

    def api(self, path, key):
        if key == "workflow_runs":
            self.paths.append(path)
            if "/workflows/python-tests.yml/runs?" in path:
                if self.listing_error:
                    raise ValueError("GitHub API failed")
                terms = path.split("?", 1)[1].split("&")
                created = [t[len("created=>="):] for t in terms if t.startswith("created=>=")]
                if len(created) != 1 or not {"event=workflow_dispatch", "branch=master",
                                             "per_page=100"} <= set(terms):
                    return []
                floor = br._time(created[0])
                return [r for r in self.runs if "created_at" in r and br._time(r["created_at"]) >= floor]
            return list(self.runs) if self.all_runs is None else list(self.all_runs)
        if key == "jobs":
            self.paths.append(path)
            return self.jobs[1]
        if key == "artifacts":
            self.paths.append(path)
            rid = int(path.split("/")[-2])
            if rid in self.no_artifact:
                return []
            return [dict(id=rid, name="ci-selection-request", expired=False)]
        raise AssertionError((path, key))

    def binary(self, argv, **kw):
        if argv[0] == "base-red-fake-gh" and "/artifacts/" in argv[2]:
            rid = int(argv[2].split("/")[-2])
            data = fixtures.archive(**{"ci-selection-request.json": json.dumps(self.artifacts[rid])})
            return subprocess.CompletedProcess(argv, 0, data, b"")
        return super().binary(argv, **kw)

    def transport(self, argv, **kw):
        if argv[:2] == ["gh", "api"]:
            self.assertFalse(kw.get("wait", True))
            self.api_calls.append(argv)
            if self.run_reply is not None:
                return self.run_reply
            match = re.fullmatch(r"repos/example/demo/actions/runs/(\d+)", argv[2])
            self.assertIsNotNone(match, argv)
            run = next((r for r in self.runs if r["id"] == int(match[1])), None)
            if run is None:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps(run), ""
        self.assertEqual(argv[:3], ["gh", "workflow", "run"])
        self.assertFalse(kw.get("wait", True))
        self.dispatches.append(argv)
        return 0, "", ""

    def tick(self):
        return self.mf.tick("albert-scott")

    def stage(self, kind="full", *, rid=1, ids=None, conclusion="success", **changes):
        if ids is None: ids = []
        mode = "full" if kind == "full" else "selected"
        reason = "suite-full" if kind == "full" else "fixture"
        self.artifacts[rid] = dict(mode=mode, reason=reason)
        if mode == "selected": self.artifacts[rid]["pytest_files"] = ["api/tests/test_canary.py"]
        text = (f"CI-SELECTION mode={mode} reason={reason} files=1\n"
                + "".join("NEW failed: " + i + "\n" for i in ids)
                + f"SUMMARY pytest_baseline_gate.py check: {'FAIL' if ids else 'PASS'} new={len(ids)} changed=0\n")
        self.zips[rid] = fixtures.archive(**{"pytest.txt": text})
        self.jobs[1] = [dict(name="pytest", conclusion=conclusion),
                        dict(name="pytest — shard api-1", conclusion=conclusion),
                        dict(name="pg-parity (Postgres)", conclusion="success", steps=[])]
        run = self.run_doc(rid, "Python Tests", event="workflow_dispatch", head_branch="master",
                           created_at=self.clock.isoformat())
        run.update(conclusion=conclusion, **changes)
        self.runs.append(run)
        return run

    def intent(self, kind="full", *, sha=None, sent=None, before=(), run_id=None):
        result = dict(kind=kind, sha=sha or self.sha, slot=0, sent_at=(sent or self.clock).isoformat(),
                      before_ids=list(before),
                      inputs=dict(suite="full" if kind == "full" else "window", full_run=True,
                                  select_base="" if kind == "full" else self.base, rerun_ids="", checks=""))
        if run_id is not None: result["run_id"] = str(run_id)
        return result

    def put_intent(self, intent):
        directory = self.root / "master-full/albert-scott"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "state.json"
        state = json.loads(path.read_text()) if path.exists() else dict(pending=[], slots={}, last_full=self.sha)
        state["pending"].append(intent)
        self.mf._save(path, state)
        self.quiet()

    def quiet(self):
        """Mark the cadence slots used at the current clock so a tick never dispatches."""
        path = self.root / "master-full/albert-scott/state.json"
        state = json.loads(path.read_text())
        state["slots"] = dict(full=int(self.clock.timestamp()) // 10800,
                              canary=int(self.clock.timestamp()) // 43200)
        state["last_full"] = self.sha
        self.mf._save(path, state)

    def state(self):
        return json.loads((self.root / "master-full/albert-scott/state.json").read_text())

    def reset(self):
        for name in ("master-full/albert-scott/state.json", "events/L-base-red-local.jsonl"):
            (self.root / name).unlink(missing_ok=True)
        self.runs, self.artifacts, self.api_calls, self.paths = [], {}, [], []
        self.run_reply, self.listing_error, self.no_artifact = None, False, set()

    def rows(self, event=None):
        return [r for r in br._nightly_rows("albert-scott") if event is None or r.get("type") == event]

    def candidate(self, rid, mode, reason, *, head=None, minutes=5):
        self.artifacts[rid] = dict(mode=mode, reason=reason)
        if mode == "selected": self.artifacts[rid]["pytest_files"] = ["api/tests/test_canary.py"]
        run = self.run_doc(rid, "Python Tests", event="workflow_dispatch", head_branch="master",
                           created_at=(self.clock + timedelta(minutes=minutes)).isoformat())
        if head: run["head_sha"] = head
        self.runs.append(run)
        return run

    def ready(self, kind="full", *, rid=1, ids=None, conclusion="success", intent_sha=None, **changes):
        run = self.stage(kind, rid=rid, ids=ids, conclusion=conclusion, **changes)
        self.put_intent(self.intent(kind, sha=intent_sha, run_id=rid))
        return run

    def receipt(self, criterion, body):
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url="scratch://master-full/" + criterion, status=200,
                              body_sha256=hashlib.sha256(encoded.encode()).hexdigest(), body_excerpt=body)))


class TestCadence(MasterFixture):
    def test_first_due_boundary_and_unchanged(self):
        self.clock = NOW + timedelta(minutes=179)
        self.assertTrue(self.tick()["dispatched"])
        self.assertEqual(len(self.dispatches), 2)
        full, canary = self.dispatches
        for argv in self.dispatches:
            self.assertIn("master", argv)
            for field in ("full_run=true", "rerun_ids=", "checks="):
                self.assertIn(field, argv)
        self.assertIn("suite=full", full)
        self.assertIn("select_base=", full)
        self.assertIn("suite=window", canary)
        self.assertIn("select_base=" + self.base, canary)
        self.clock = NOW + timedelta(hours=3)
        self.assertFalse(self.tick()["dispatched"])
        (self.repo / "api/tests/test_later.py").write_text("def test_later(): pass\n")
        self.commit("new head")
        self.assertTrue(self.tick()["dispatched"])
        self.assertEqual(len(self.dispatches), 3)
        self.clock = NOW + timedelta(hours=12)
        self.assertTrue(self.tick()["dispatched"])
        self.assertEqual(len(self.dispatches), 4)
        self.receipt("AC1", dict(dispatches=len(self.dispatches), exact_inputs=True))

    def test_concurrent_ticks_and_durable_ambiguous_intent(self):
        # Transport failure can mean GitHub accepted the request: never resend it.
        with patch.object(br.ghlimit, "run", return_value=(1, "", "")) as send:
            threads = [threading.Thread(target=self.tick) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(send.call_count, 2)
            self.assertTrue(self.tick()["undetermined"])
            self.assertEqual(send.call_count, 2)
        state = json.loads((self.root / "master-full/albert-scott/state.json").read_text())
        self.assertEqual(len(state["pending"]), 2)
        self.assertTrue(all(p["before_ids"] == [] for p in state["pending"]))

    def test_unsafe_prerequisites_never_dispatch(self):
        sources = [WORKFLOW.replace("[window, nightly, full]", "[window, nightly]"),
                   WORKFLOW.replace("inputs.suite || 'window'", "'legacy'"),
                   WORKFLOW.replace("inputs.full_run || false", "false"),
                   WORKFLOW.replace("github.ref", "'master'"),
                   WORKFLOW.replace("${{ inputs.select_base }}", "${{ inputs.full_run && '' || inputs.select_base }}")]
        for source in sources:
            with self.subTest(source=source):
                self.workflow_path.write_text(source)
                self.commit("unsafe")
                result = self.tick()
                self.assertFalse(result["dispatched"])
                self.assertTrue(result["undetermined"])
        self.assertEqual(self.dispatches, [])

    def test_invalid_project_and_clock(self):
        for project in ("../albert-scott", "/albert-scott", "demo"):
            result = self.mf.tick(project)
            self.assertFalse(result["dispatched"])
            self.assertTrue(result["undetermined"])
        self.assertEqual(self.dispatches, [])

    def test_cron_collection_interval_and_legacy_concurrency(self):
        manifest = tomllib.loads((fixtures.HERE.parent / "crons.toml").read_text())
        rows = [r for r in manifest["row"] if r["name"] == "master-full"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schedule"], "*/5 * * * *")
        self.assertIn("cron-run master-full --", row["command"])
        self.assertIn("{repo}/src/master_full.py albert-scott", row["command"])
        legacy = ("${{ inputs.tree != '' && format('{0}-tree-{1}', github.workflow, inputs.tree) "
                  "|| format('{0}-{1}-{2}-{3}-{4}-{5}', github.workflow, github.ref, "
                  "inputs.runner || 'auto', inputs.pg_runner || 'auto', inputs.checks || 'all', "
                  "inputs.full_run || false) }}")
        old = WORKFLOW.replace("${{ format('{0}-{1}-{2}-{3}', github.workflow, github.ref, inputs.suite || 'window', inputs.full_run || false) }}", legacy)
        self.workflow_path.write_text(old)
        self.commit("legacy pre-suite grouping")
        self.assertTrue(self.tick()["undetermined"])
        self.assertEqual(self.dispatches, [])

    def test_reconcile_and_no_lane_cancellation(self):
        self.tick()
        self.ready(rid=9)
        # Remove the ready helper's correlated marker: tick must discover it
        # using the original persisted pre-dispatch intent instead.
        path = self.root / "master-full/albert-scott/state.json"
        state = json.loads(path.read_text())
        state["pending"].pop()
        self.mf._save(path, state)
        self.assertTrue(self.tick()["collected"])
        self.assertEqual(len(self.dispatches), 2)
        self.assertTrue(all("cancel" not in argv for argv in self.calls + self.dispatches))

    def test_active_unrelated_run_and_crash_before_acceptance(self):
        self.runs = [self.run_doc(3, "Python Tests", head_branch="master")]
        self.runs[0]["status"] = "in_progress"
        self.assertTrue(self.tick()["undetermined"])
        self.assertEqual(self.dispatches, [])
        self.runs = []
        with patch.object(br.ghlimit, "run", side_effect=OSError("interrupted")):
            self.assertTrue(self.tick()["undetermined"])
        self.assertFalse(self.tick()["dispatched"])


class TestCollection(MasterFixture):
    def test_queued_running_terminal_and_green(self):
        run = self.ready(status="queued")
        self.assertFalse(self.tick()["collected"])
        run["status"] = "in_progress"
        self.assertFalse(self.tick()["collected"])
        run["status"] = "completed"
        self.assertTrue(self.tick()["collected"])
        rows = br._rows("albert-scott")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["complete"])
        self.assertTrue(row["green"])
        self.assertEqual(row["sha"], self.sha)
        self.assertEqual(row["covered"], ["pg-parity", "pytest"])
        self.receipt("AC2", dict(complete=row["complete"], green=row["green"], covered=row["covered"]))

    def test_missing_partial_malformed_and_selected_never_green(self):
        self.ready()
        self.artifacts[1] = dict(mode="selected", pytest_files=["api/tests/test_canary.py"])
        self.assertTrue(self.tick()["undetermined"])
        self.assertEqual(br._rows("albert-scott"), [])
        self.artifacts[1] = dict(mode="full")
        self.jobs[1][-1]["conclusion"] = "skipped"
        self.assertTrue(self.tick()["collected"])
        row = br._rows("albert-scott")[0]
        self.assertFalse(row["complete"])
        self.assertFalse(row["green"])

    def test_skipped_shard_cannot_establish_full_coverage(self):
        self.ready()
        self.jobs[1].append(dict(name="pytest — shard api-2", conclusion="skipped"))
        self.assertTrue(self.tick()["undetermined"])
        row = br._nightly_rows("albert-scott")[-1]
        self.assertFalse(row["complete"])
        self.assertFalse(row["green"])

    def test_cancelled_superseded_and_sha_race(self):
        self.ready(conclusion="cancelled")
        self.assertTrue(self.tick()["collected"])
        self.assertFalse(br._rows("albert-scott")[0]["complete"])
        self.ready(rid=2, head_sha=self.base)
        self.assertTrue(self.tick()["undetermined"])
        self.assertEqual(br._rows("albert-scott")[-1]["sha"], self.base)

    def test_red_question_recovery_and_attribution(self):
        self.ready()
        self.tick()
        old = self.sha
        self.git("checkout", "-qb", "side")
        (self.repo / "api/tests/test_side.py").write_text("pass\n")
        self.commit("untrusted\x1b[31m subject")
        self.git("checkout", "-q", "master")
        self.git("-c", "user.name=t", "-c", "user.email=t@localhost", "merge", "--no-ff", "-qm", "merge side", "side")
        self.sha = self.git("rev-parse", "HEAD")
        self.ready(rid=2, ids=[PY], conclusion="failure")
        real_append = br._append
        def append(event, *args, **kwargs):
            if event == "question": raise ValueError("interrupted before question")
            return real_append(event, *args, **kwargs)
        with patch.object(br, "_append", side_effect=append):
            self.assertTrue(self.tick()["undetermined"])
        self.assertTrue(self.tick()["collected"])
        self.tick()
        rows = br._nightly_rows("albert-scott")
        questions = [r for r in rows if r["type"] == "question"]
        self.assertEqual(len(questions), 1)
        red = next(r for r in rows if r["type"] == "master-full-run" and not r["green"])
        self.assertFalse(red["attribution_undetermined"])
        self.assertEqual(red["baseline"], old)
        self.assertEqual([m["sha"] for m in red["merges_since_green"]], [self.sha])
        self.assertIn(PY, questions[0]["asks"])
        self.assertIn("actions/runs/2", questions[0]["asks"])
        self.assertFalse(br.fold.nightly_unowned(br.fold.read_events(use_cache=False), "albert-scott"))
        self.receipt("AC4", dict(question_count=len(questions), attributed_merges=1, lane_unowned=False))

    def test_artifact_traversal_and_size_rejected(self):
        self.ready()
        for members in ({"../ci-selection-request.json": "{}"},
                        {"ci-selection-request.json": "{}", "other": "{}"}):
            with patch.object(br.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, fixtures.archive(**members), b"")):
                with self.assertRaises(ValueError): self.mf._artifact("example/demo", "1")

    def test_descendant_full(self):
        old = self.sha
        (self.repo / "api/tests/test_later.py").write_text("def test_later(): pass\n")
        self.commit("later merge")
        later = self.sha
        self.git("checkout", "-qb", "side", old)
        (self.repo / "api/tests/test_side2.py").write_text("def test_s(): pass\n")
        self.commit("diverged")
        side = self.sha
        self.git("checkout", "-q", "master")
        self.sha = later
        self.ready(rid=1, intent_sha=old, head_sha=later)
        self.assertTrue(self.tick()["collected"])
        row = next(r for r in self.rows() if rid_of(r) == "1")
        self.assertTrue(row["complete"], row)
        self.assertTrue(row["green"])
        self.assertEqual(row["sha"], later)
        self.assertFalse([u for u in row["undetermined"] if "master advanced" in u])
        cases = {2: ("diverged", later, side), 3: ("unreadable", old, later),
                 4: ("invalid intent sha", "bad", later)}
        real_git = br.window_gate.git
        def flaky(repo, *args, **kw):
            if args[:2] == ("rev-list", "--first-parent") and not flaky.failed:
                flaky.failed = True
                raise RuntimeError("git rev-list: unreadable")
            return real_git(repo, *args, **kw)
        for rid, (label, intent_sha, head) in cases.items():
            with self.subTest(label):
                self.ready(rid=rid, intent_sha=intent_sha, head_sha=head)
                flaky.failed = label != "unreadable"
                with patch.object(br.window_gate, "git", side_effect=flaky):
                    self.tick()
                row = next(r for r in self.rows() if rid_of(r) == str(rid))
                self.assertFalse(row["complete"])
                self.assertIn("master advanced before dispatch acceptance", row["undetermined"])
        self.ready("canary", rid=5, intent_sha=old, head_sha=later)
        self.tick()
        row = next(r for r in self.rows() if rid_of(r) == "5")
        self.assertFalse(row["complete"])
        self.assertIn("master advanced before dispatch acceptance", row["undetermined"])


class TestShardActivity(MasterFixture):
    def shard_jobs(self, plan, conclusions=None, total=8):
        jobs = [dict(name="pytest", conclusion="success"),
                dict(name="pg-parity (Postgres)", conclusion="success", steps=[])]
        for k in range(1, total + 1):
            jobs.append(dict(name=f"pytest \u2014 shard {k} of {plan}",
                             conclusion="success" if k <= plan else "skipped"))
        for k, v in (conclusions or {}).items():
            jobs[2 + k - 1]["conclusion"] = v
            if v == "failure": jobs[0]["conclusion"] = "failure"
        return jobs

    def collect(self, kind, jobs, **kw):
        self.rid = getattr(self, "rid", 0) + 1
        self.ready(kind, rid=self.rid, **kw)
        self.jobs[1] = jobs
        self.tick()
        kinds = {"master-full-run", "selected-canary"}
        return [r for r in br._nightly_rows("albert-scott") if r["type"] in kinds][-1]

    def names_shard(self, row):
        return any("pytest \u2014 shard" in u for u in row["undetermined"])

    def test_inactive_shards_full_run_complete(self):
        results = []
        for plan in (2, 4, 6, 8):
            for failing in (False, True):
                with self.subTest(plan=plan, failing=failing):
                    row = self.collect("full", self.shard_jobs(plan, {1: "failure"} if failing else None),
                                       **(dict(ids=[PY], conclusion="failure") if failing else {}))
                    self.assertTrue(row["complete"], row["undetermined"])
                    self.assertFalse(self.names_shard(row))
                    self.assertEqual(row["green"], not failing)
                    results.append(dict(plan=plan, failing=failing, complete=row["complete"],
                                        green=row["green"], undetermined=row["undetermined"]))
        self.receipt("AC1", results)

    def test_active_shard_not_executed_incomplete(self):
        four = self.shard_jobs
        cases = {}
        for v in ("cancelled", "timed_out", "skipped", None):
            cases[f"shard3-{v}"] = four(4, {3: v})
        cases["inactive-numbered-cancelled"] = four(4, {6: "cancelled"})
        unparseable = four(4)
        unparseable.append(dict(name="pytest \u2014 shard api-2", conclusion="skipped"))
        cases["unparseable"] = unparseable
        mixed = four(4)
        mixed[2 + 4] = dict(name="pytest \u2014 shard 5 of 8", conclusion="skipped")
        cases["mixed-counts"] = mixed
        only_inactive = four(4)[:2] + four(4)[2 + 4:]
        cases["only-inactive"] = only_inactive
        missing = four(4)
        del missing[2 + 2]
        cases["missing-shard-3"] = missing
        zero = [dict(j, name=f"pytest \u2014 shard {k} of 0", conclusion="skipped")
                for k, j in enumerate(four(4)[2:], 1)]
        cases["of-zero"] = four(4)[:2] + zero
        newline = four(4)
        newline[2 + 5] = dict(name="pytest \u2014 shard 6 of 4\n", conclusion="skipped")
        cases["trailing-newline"] = newline
        digits = four(4)
        digits[2 + 5] = dict(name="pytest \u2014 shard \u0666 of 4", conclusion="skipped")
        cases["non-ascii-digit"] = digits
        offending = {
            **{f"shard3-{v}": "pytest — shard 3 of 4"
               for v in ("cancelled", "timed_out", "skipped", None)},
            "inactive-numbered-cancelled": "pytest — shard 6 of 4",
            "unparseable": "pytest — shard api-2",
            "mixed-counts": "pytest — shard 5 of 8",
            "only-inactive": "pytest — shard 5 of 4",
            "missing-shard-3": "pytest — shard 5 of 4",
            "of-zero": "pytest — shard 1 of 0",
            "trailing-newline": "pytest — shard 6 of 4",
            "non-ascii-digit": "pytest — shard \u0666 of 4",
        }
        results = []
        for label, jobs in cases.items():
            with self.subTest(label):
                active = self.mf._active_shards(jobs)
                self.assertTrue(any(j.get("conclusion") not in {"success", "failure"} for j in active))
                row = self.collect("full", jobs)
                self.assertFalse(row["complete"])
                self.assertFalse(row["green"])
                self.assertTrue(any(offending[label] in why for why in row["undetermined"]), row)
                results.append(dict(case=label, complete=row["complete"], green=row["green"],
                                    undetermined=row["undetermined"]))
        self.receipt("AC2", results)

    def test_canary_inactive_shards(self):
        row = self.collect("canary", self.shard_jobs(4))
        self.assertTrue(row["complete"])
        self.assertTrue(row["green"])
        self.assertFalse(self.names_shard(row))
        self.assertFalse(any("incomplete pytest coverage" in u for u in row["undetermined"]))
        results = [dict(case="valid", complete=row["complete"], green=row["green"],
                        undetermined=row["undetermined"])]
        bad = {
            "active-skipped": self.shard_jobs(4, {2: "skipped"}),
            "of-zero": self.shard_jobs(4)[:2] + [
                dict(name=f"pytest \u2014 shard {k} of 0", conclusion="skipped") for k in range(1, 9)],
            "only-inactive": self.shard_jobs(4)[:2] + self.shard_jobs(4)[2 + 4:],
        }
        for label, jobs in bad.items():
            with self.subTest(label):
                row = self.collect("canary", jobs)
                self.assertFalse(row["complete"])
                self.assertFalse(row["green"])
                results.append(dict(case=label, complete=row["complete"], green=row["green"],
                                    undetermined=row["undetermined"]))
        self.receipt("AC3", results)

class TestCanary(MasterFixture):
    def test_selected_request_without_executed_pytest_is_undetermined(self):
        self.ready("canary")
        self.jobs[1][0]["conclusion"] = "skipped"
        result = self.tick()
        self.assertTrue(result["undetermined"])
        self.assertFalse(br._nightly_rows("albert-scott")[-1]["green"])

    def test_actual_selected_green_and_explained_red(self):
        self.ready("canary")
        self.assertTrue(self.tick()["collected"])
        first = br._nightly_rows("albert-scott")[-1]
        self.assertTrue(first["green"])
        # Positive evidence at the exact tested head explains a canary failure.
        br._append("master-full-run", "albert-scott", self.sha, complete=True,
                   ids=[PY], green=False, covered=["pg-parity", "pytest"], suite="full", run_ids=["3"])
        self.ready("canary", rid=2, ids=[PY], conclusion="failure")
        self.assertTrue(self.tick()["collected"])
        row = br._nightly_rows("albert-scott")[-1]
        self.assertTrue(row["green"])
        self.assertEqual(row["classification"]["known_red"], [PY])
        self.receipt("AC5", dict(selected=True, green=row["green"], explained_failures=len(row["ids"])))

    def test_fallback_empty_and_unexplained_failures(self):
        self.ready("canary", ids=[PY], conclusion="failure")
        self.artifacts[1] = dict(mode="full")
        self.assertTrue(self.tick()["undetermined"])
        self.assertEqual(br._rows("albert-scott"), [])
        self.artifacts[1] = dict(mode="selected", pytest_files=[])
        self.assertTrue(self.tick()["undetermined"])
        self.artifacts[1]["pytest_files"] = ["api/tests/test_canary.py"]
        self.assertTrue(self.tick()["collected"])
        row = br._nightly_rows("albert-scott")[-1]
        self.assertFalse(row["green"])
        self.assertEqual(row["classification"]["rerun_on_base"], [PY])


class TestRoundTrip(MasterFixture):
    def fresh(self, code):
        command = "import sys; sys.path.insert(0,sys.argv[1]); " + code
        proc = self.real_run([sys.executable, "-I", "-c", command, str(fixtures.HERE)],
                             env=dict(os.environ), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_production_append_fresh_reader_answer_and_saved_state(self):
        self.ready(ids=[PY], conclusion="failure")
        self.assertTrue(self.tick()["collected"])
        code = ("import json,fold,base_red,thinker_inbox; rows=fold.read_events(use_cache=False); "
                "print(json.dumps(dict(known=list(base_red.known_red('albert-scott')), "
                "items=thinker_inbox.open_items(), admitted=[e['type'] for e in rows])))")
        read = json.loads(self.fresh(code))
        self.assertEqual(read["known"], [PY])
        self.assertIn("master-full-run", read["admitted"])
        self.assertEqual(len(read["items"]), 1)
        source = read["items"][0]["src"]
        proc = self.real_run([str(br.DOIT), "thinker-answer", source, "Acknowledged fixture red"],
                             env=dict(os.environ), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(self.fresh(code))["items"], [])
        self.fresh("import thinker_inbox; thinker_inbox.save_state({'delivered': {'fixture': True}, 'tg': {}})")
        state = self.fresh("import json,thinker_inbox; print(json.dumps(thinker_inbox.load_state()))")
        self.assertEqual(json.loads(state)["delivered"], {"fixture": True})
        # A forged ledger cannot authorize either a result or a positive id.
        (self.root / "events/L-builder-forged.jsonl").write_text(json.dumps(dict(
            type="master-full-run", ts=NOW.isoformat(), project="albert-scott", sha=self.sha,
            complete=True, ids=["forged.py::test"], covered=["pg-parity", "pytest"],
            suite="full", green=False, run_ids=["4"])) + "\n")
        self.assertEqual(json.loads(self.fresh(code))["known"], [PY])
        self.ready(rid=2)
        self.tick()
        self.assertEqual(json.loads(self.fresh(code))["known"], [])
        self.receipt("AC6", dict(fresh_reader=True, answer_closed=True, delivery_persisted=True, green_replaced=True))

    def test_restart_pending_and_concurrent_collection(self):
        self.ready()
        # A new interpreter reads the durable pending marker and uses fixture
        # transports, without replacing production scheduling or append code.
        code = ("import json,master_full,contextlib; from unittest.mock import patch; "
                "from datetime import datetime; "
                "state=json.loads((master_full.br.fold.ROOT/'master-full/albert-scott/state.json').read_text()); "
                "assert state['pending'][0]['run_id']=='1'; ")
        body = ("with contextlib.ExitStack() as stack:\n"
                f" stack.enter_context(patch.object(master_full, '_now', return_value=datetime.fromisoformat({self.clock.isoformat()!r})))\n"
                f" stack.enter_context(patch.object(master_full, '_tip', return_value={self.sha!r}))\n"
                f" stack.enter_context(patch.object(master_full.br, '_api', side_effect=lambda path,key: {self.runs!r} if key=='workflow_runs' else {self.jobs[1]!r}))\n"
                f" stack.enter_context(patch.object(master_full, '_run', return_value={self.runs[0]!r}))\n"
                " stack.enter_context(patch.object(master_full, '_artifact', return_value={'mode':'full'}))\n"
                " stack.enter_context(patch.object(master_full.br, '_logs', return_value=[('pytest.txt','CI-SELECTION mode=full reason=fixture files=1\\nSUMMARY pytest_baseline_gate.py check: PASS new=0 changed=0')]))\n"
                " result=master_full.tick('albert-scott')\n"
                " assert result['collected'], result\n")
        self.fresh(code + "exec(" + repr(body) + ")")
        threads = [threading.Thread(target=self.tick) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len([r for r in br._rows("albert-scott") if r["type"] == "master-full-run"]), 1)

    def test_accept_expire_fresh_reader(self):
        self.stage("full", rid=1)
        self.put_intent(self.intent("full"))
        old = self.intent("canary", sent=NOW - timedelta(hours=25))
        self.put_intent(old)
        self.assertTrue(self.tick()["collected"])
        read = ("import json,fold,base_red; ev=fold.read_events(use_cache=False); "
                "state=json.loads((fold.ROOT/'master-full/albert-scott/state.json').read_text()); "
                "print(json.dumps(dict(state=state, rows=base_red._nightly_rows('albert-scott'), "
                "types=[e['type'] for e in ev])))")
        first = json.loads(self.fresh(read))
        self.assertEqual([p["kind"] for p in first["state"]["pending"]], ["canary"])
        expired = first["state"]["pending"][0]
        self.assertEqual(expired["expired"]["reason"], "no-run-found")
        full = [r for r in first["rows"] if r["type"] == "master-full-run"]
        canary = [r for r in first["rows"] if r["type"] == "selected-canary"]
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0]["run_ids"], ["1"])
        self.assertEqual(len(canary), 1)
        self.assertEqual(canary[0]["expired_intent"], expired["sent_at"])
        self.assertIn("mode=full", canary[0]["last_reason"])
        self.assertTrue(all("actor" not in r for r in first["rows"]))
        self.assertEqual(first["types"].count("master-full-run"), 1)
        self.assertEqual(first["types"].count("selected-canary"), 1)
        body = ("with contextlib.ExitStack() as stack:\n"
                f" stack.enter_context(patch.object(master_full, '_now', return_value=datetime.fromisoformat({self.clock.isoformat()!r})))\n"
                f" stack.enter_context(patch.object(master_full, '_tip', return_value={self.sha!r}))\n"
                " stack.enter_context(patch.object(master_full.br, '_api', side_effect=lambda path,key: []))\n"
                " result=master_full.tick('albert-scott')\n"
                " assert not result['collected'] and not result['dispatched'], result\n")
        self.fresh("import json,master_full,contextlib; from unittest.mock import patch; "
                   "from datetime import datetime; exec(" + repr(body) + ")")
        self.assertEqual(json.loads(self.fresh(read))["rows"], first["rows"])



class TestAccept(MasterFixture):
    LIST = ("repos/example/demo/actions/workflows/python-tests.yml/runs?event=workflow_dispatch"
            "&branch=master&created=>=2026-11-07T00:00:00Z&per_page=100")

    def test_workflow_scoped_listing(self):
        for kind in ("full", "canary"):
            with self.subTest(kind=kind):
                self.reset()
                mode, reason = ("full", "suite-full") if kind == "full" else ("selected", "fixture")
                self.all_runs = [self.run_doc(1000 + i, "Other", event="push", head_branch="main")
                                 for i in range(150)]
                pending = self.intent(kind, before=["5"])
                self.candidate(5, mode, reason)                    # already in before_ids
                self.candidate(6, mode, reason, minutes=-60)       # created before sent_at
                self.assertEqual(self.mf.accept("albert-scott", self.repo, "example/demo", pending), None)
                self.candidate(7, mode, reason, head=self.base)
                self.paths.clear()
                self.assertEqual(self.mf.accept("albert-scott", self.repo, "example/demo", pending), "7")
                self.assertEqual([p for p in self.paths if "/artifacts" not in p], [self.LIST])
                self.candidate(8, mode, reason)                    # head equals the intent's sha: wins
                self.assertEqual(self.mf.accept("albert-scott", self.repo, "example/demo", pending), "8")
                self.candidate(9, mode, reason, head=self.base)
                self.candidate(10, mode, reason, head=self.base)
                self.runs = [r for r in self.runs if r["id"] not in (8,)]
                with self.assertRaises(ValueError):
                    self.mf.accept("albert-scott", self.repo, "example/demo", pending)
        self.receipt("AC1", dict(path=self.LIST))

    def test_artifact_failure_and_quota_are_never_none(self):
        for kind in ("full", "canary"):
            self.reset()
            mode, reason = ("full", "suite-full") if kind == "full" else ("selected", "fixture")
            pending = self.intent(kind)
            self.candidate(7, mode, reason)
            with patch.object(self.mf, "_artifact", side_effect=ValueError("selection artifact unavailable or oversized")):
                with self.assertRaises(ValueError):
                    self.mf.accept("albert-scott", self.repo, "example/demo", pending)
            self.assertIn("7", pending["last_reason"])
            pending = self.intent(kind)
            with patch.object(br.ghlimit, "gate", return_value={"ok": False}):
                with self.assertRaises(ValueError):
                    self.mf.accept("albert-scott", self.repo, "example/demo", pending)
            self.assertIn("quota", pending["last_reason"])
            with patch.object(br, "_api", side_effect=OSError("down")):
                with self.assertRaises(ValueError):
                    self.mf.accept("albert-scott", self.repo, "example/demo", self.intent(kind))

    def test_reason_and_mode_proof(self):
        accept = lambda p: self.mf.accept("albert-scott", self.repo, "example/demo", p)
        pending = self.intent("full")
        self.candidate(11, "full", "suite-nightly")
        self.candidate(12, "full", "suite-nightly", head=self.base)
        self.candidate(13, "full", "default")
        self.assertIsNone(accept(pending))
        for rid in ("11", "12", "13"):
            self.assertIn(rid, pending["last_reason"])
        self.assertIn("mode=full reason=suite-nightly", pending["last_reason"])
        self.assertIn("reason=default", pending["last_reason"])
        self.candidate(14, "full", "suite-full")
        self.assertEqual(accept(pending), "14")
        canary = self.intent("canary")
        self.reset()
        self.candidate(21, "full", "selector-error")
        self.candidate(22, "full", "bad\x1b[31m\nreason")
        self.assertIsNone(accept(canary))
        self.assertIn("21: mode=full reason=selector-error", canary["last_reason"])
        self.assertIn("22", canary["last_reason"])
        self.assertNotIn("\x1b", canary["last_reason"])
        self.assertNotIn("\n", canary["last_reason"])
        self.candidate(23, "selected", "fixture")
        self.assertEqual(accept(canary), "23")


class TestAcceptRunId(MasterFixture):
    def test_old_run_collected_by_id_not_listing(self):
        for kind, event in (("canary", "selected-canary"), ("full", "master-full-run")):
            with self.subTest(kind=kind):
                self.reset()
                self.ready(kind)
                with patch.object(self.mf.br, "_api", side_effect=lambda path, key: []
                                  if "workflows/" in path else self.api(path, key)):
                    result = self.tick()
                self.assertTrue(result["collected"], result)
                self.assertEqual(len(self.rows(event)), 1)
                self.assertEqual(self.state()["pending"], [])
                self.assertEqual(self.api_calls, [["gh", "api", "repos/example/demo/actions/runs/1"]])

    def test_not_found_versus_undetermined(self):
        self.ready()
        self.run_reply = (1, "", "gh: Not Found (HTTP 404)")
        self.assertIsNone(self.mf._run("example/demo", "1"))
        replies = [(1, "", "HTTP 502"), (75, "", "ghlimit: quota low, skipped"), (1, "", ""),
                   (0, "not json", ""), (0, json.dumps(dict(id=2)), ""), (0, "[]", "")]
        for reply in replies:
            with self.subTest(reply=reply):
                self.run_reply = reply
                with self.assertRaises(ValueError):
                    self.mf._run("example/demo", "1")
                result = self.tick()
                self.assertTrue(result["undetermined"])
                pending = self.state()["pending"]
                self.assertEqual(len(pending), 1)
                self.assertNotIn("expired", pending[0])
                self.assertTrue(pending[0]["last_reason"])
        self.assertEqual(self.rows(), [])
        self.run_reply = (1, "", "gh: Not Found (HTTP 404)")
        self.tick()
        self.assertEqual(self.state()["pending"][0]["expired"]["reason"], "run-missing")
        self.assertEqual(len(self.rows("master-full-run")), 1)


class TestExpiry(MasterFixture):
    def at(self, hours):
        self.clock = NOW + timedelta(hours=hours)
        self.quiet()

    def settle(self, make, slug, hours=25):
        """An intent is pending when young and expired with slug when old."""
        for age, expected in ((1, None), (hours, slug)):
            self.reset()
            self.clock = NOW
            make()
            self.at(age)
            self.tick()
            pending = self.state()["pending"]
            self.assertEqual(len(pending), 1)
            if expected is None:
                self.assertNotIn("expired", pending[0])
                self.assertEqual(self.rows(), [])
            else:
                self.assertEqual(pending[0]["expired"],
                                 {"at": self.clock.isoformat(), "reason": slug})
                rows = self.rows()
                self.assertEqual(len(rows), 1)
                self.assertIn("last_reason", rows[0])
                self.assertEqual(rows[0]["undetermined"], ["intent expired: " + slug])
                self.assertFalse(rows[0]["complete"])
                self.assertFalse(rows[0]["green"])
                self.assertEqual((rows[0]["ids"], rows[0]["run_ids"]), ([], []))
                self.assertEqual(rows[0]["expired_intent"], pending[0]["sent_at"])

    def test_slugs_and_transient(self):
        # no-run-found, with the dropped candidate cause carried onto the row
        self.settle(lambda: (self.put_intent(self.intent("canary")), self.candidate(31, "full", "selector-error")),
                    "no-run-found")
        self.assertIn("31: mode=full reason=selector-error", self.rows()[0]["last_reason"])
        # ambiguous-runs
        def ambiguous():
            self.put_intent(self.intent("full"))
            self.candidate(41, "full", "suite-full", head=self.base)
            self.candidate(42, "full", "suite-full", head=self.base)
        self.settle(ambiguous, "ambiguous-runs")
        # run-not-completed
        self.settle(lambda: self.ready(status="in_progress"), "run-not-completed")
        # collection-unprovable, once per named cause
        def retention():
            self.ready(); self.no_artifact.add(1)
        def mode_proof():
            self.ready(); self.artifacts[1] = dict(mode="selected", pytest_files=["api/tests/test_canary.py"])
        def empty_selection():
            self.ready("canary"); self.artifacts[1]["pytest_files"] = []
        def bad_head():
            self.ready(head_sha="not-a-sha")
        for make in (retention, mode_proof, empty_selection, bad_head):
            with self.subTest(make=make.__name__):
                self.settle(make, "collection-unprovable")
        # run-missing needs no age
        self.reset(); self.clock = NOW
        self.ready(); self.runs.clear(); self.at(1); self.tick()
        self.assertEqual(self.state()["pending"][0]["expired"]["reason"], "run-missing")
        # transient failures never expire, at any age
        def stays(label):
            self.at(48)
            self.tick()
            pending = self.state()["pending"]
            self.assertEqual(len(pending), 1, label)
            self.assertNotIn("expired", pending[0], label)
            self.assertEqual(self.rows(), [], label)
        for message in sorted(self.mf.TRANSIENT):
            self.reset(); self.clock = NOW; self.ready()
            with patch.object(self.mf, "_collect", side_effect=ValueError(message)):
                stays(message)
        for error in (OSError("io"), subprocess.SubprocessError("sub")):
            self.reset(); self.clock = NOW; self.ready()
            with patch.object(self.mf, "_collect", side_effect=error):
                stays(repr(error))
        self.reset(); self.clock = NOW; self.put_intent(self.intent("full"))
        self.listing_error = True
        stays("listing")
        self.reset(); self.clock = NOW; self.put_intent(self.intent("full")); self.candidate(51, "full", "suite-full")
        with patch.object(br.ghlimit, "gate", return_value={"ok": False}):
            stays("quota")
        self.reset(); self.clock = NOW; self.ready()
        self.run_reply = (1, "", "HTTP 502")
        stays("run lookup")
        # an expired intent costs no artifact, log or jobs call on later ticks
        self.reset(); self.clock = NOW; self.put_intent(self.intent("canary")); self.at(25)
        self.tick()
        self.paths.clear(); self.api_calls.clear()
        with patch.object(self.mf, "_artifact", side_effect=AssertionError), \
                patch.object(br, "_logs", side_effect=AssertionError):
            self.tick()
        self.assertFalse([p for p in self.paths if "/jobs" in p or "/artifacts" in p or "workflows/" in p])
        self.assertEqual(self.api_calls, [])
        import guards_live
        row = self.rows("selected-canary")[0]
        self.assertIsNotNone(guards_live._observation(row, "r1")[1])

    def test_crash_before_save(self):
        def expired(count):
            return [r for r in self.rows("selected-canary") if r.get("expired_intent")] if count is None else \
                self.assertEqual(len([r for r in self.rows("selected-canary") if r.get("expired_intent")]), count)
        self.put_intent(self.intent("canary"))
        self.at(25)
        self.tick(); expired(1)
        self.tick(); expired(1)
        path = self.root / "master-full/albert-scott/state.json"
        state = self.state()
        del state["pending"][0]["expired"]
        self.mf._save(path, state)
        self.tick(); expired(1)
        self.assertIn("expired", self.state()["pending"][0])
        self.reset(); self.clock = NOW
        self.put_intent(self.intent("canary")); self.at(25)
        with patch.object(br, "_append", side_effect=ValueError("base-red ledger append failed")):
            self.assertTrue(self.tick()["undetermined"])
        self.assertNotIn("expired", self.state()["pending"][0])
        expired(0)
        self.tick(); expired(1)
        self.assertIn("expired", self.state()["pending"][0])
        self.tick(); expired(1)


class TestShardNames(MasterFixture):
    def collect(self, kind, rid, jobs, conclusion="success"):
        self.ready(kind, rid=rid, conclusion=conclusion)
        base = [dict(name="pytest", conclusion=conclusion),
                dict(name="pg-parity (Postgres)", conclusion="success", steps=[])]
        self.jobs[1] = base + jobs
        self.tick()
        return next(r for r in self.rows() if rid_of(r) == str(rid))

    def shards(self, **conclusions):
        return [dict(name=f"pytest — shard {k} of 4", conclusion=conclusions.get(f"s{k}", "success" if k <= 4 else "skipped"))
                for k in range(1, 9)]

    def test_by_design_skips_do_not_block(self):
        for kind, rid in (("full", 1), ("canary", 2)):
            with self.subTest(kind=kind):
                row = self.collect(kind, rid, self.shards())
                self.assertTrue(row["complete"], row)
                self.assertTrue(row["green"], row)
                self.assertFalse([u for u in row["undetermined"] if "shard" in u])

    def test_real_skips_and_unparsable_names_block(self):
        cases = [("s3 skipped", self.shards(s3="skipped"), "pytest — shard 3 of 4: skipped"),
                 ("s3 cancelled", self.shards(s3="cancelled"), "pytest — shard 3 of 4: cancelled"),
                 ("unparsable", self.shards() + [dict(name="pytest — shard api-2", conclusion="skipped")],
                  "pytest — shard api-2: skipped"),
                 ("5 of 8", self.shards() + [dict(name="pytest — shard 5 of 8", conclusion="skipped")],
                  "pytest — shard 5 of 8: skipped")]
        rid = 10
        for kind in ("full", "canary"):
            for label, jobs, text in cases:
                rid += 1
                with self.subTest(kind=kind, case=label):
                    row = self.collect(kind, rid, jobs)
                    self.assertFalse(row["complete"])
                    self.assertFalse(row["green"])
                    self.assertTrue([u for u in row["undetermined"] if text in u], row["undetermined"])
                    self.assertTrue(all(len(u) <= 400 and "\n" not in u for u in row["undetermined"]))


def rid_of(row):
    return (row.get("run_ids") or [row.get("run_id")])[0]


class TestKnownRed(fixtures.SelectiveFixture):
    def row(self, kind="master-full-run", *, age=0, ids=None, **fields):
        result = dict(type=kind, project="demo", sha=self.sha,
                      ts=(NOW - timedelta(hours=age)).isoformat(), complete=True,
                      ids=[PY] if ids is None else ids)
        if kind == "master-full-run":
            result.update(suite="full", covered=["pg-parity", "pytest"],
                          green=not result["ids"], run_ids=["123"])
        result.update(fields)
        return result

    def store(self, *rows):
        path = self.root / "events/L-base-red-local.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def classify(self, ids):
        base = dict(sha=self.sha, complete=False,
                    ids={k: [] for k in br.KINDS}, kinds_covered=[])
        with patch.object(br, "failing", return_value=base):
            return br.classify("demo", self.sha, ids, known_red=True, now=NOW)

    def test_per_kind_snapshot_replacement_and_reruns(self):
        nightly = self.row("base-red-nightly", age=2, ids=[PY, PG, JS])
        full = self.row(age=1, ids=[PG])
        rerun = self.row("base-red", ids=[PY], complete=False, source="rerun")
        self.store(nightly, full)
        self.assertEqual(set(br.known_red("demo")), {PG, JS})
        self.assertEqual(self.classify([PY, PG, JS])["known_red"], [PG, JS])
        self.store(nightly, full, rerun)
        known, anchor = br._known_state("demo")
        self.assertEqual(set(known), {PY, PG, JS})
        self.assertEqual(known[PY]["source"], "rerun")
        self.assertEqual(anchor["snapshots"]["vitest"]["ts"], nightly["ts"])
        self.assertEqual(anchor["snapshots"]["pytest"]["ts"], full["ts"])
        self.assertEqual(br.classify_selected("demo", self.sha, [PY, PG, JS], now=NOW),
                         dict(known_red=[PY, PG, JS], rerun_on_base=[], undetermined=False))
        self.store(nightly, full, rerun,
                   self.row("base-red-nightly", ids=[], age=-0.01))
        self.assertEqual(br.known_red("demo"), {})

    def test_freshness_boundaries_and_coverage_use_same_snapshot(self):
        observations = []
        for age in (0, 35.999, 36, 37, -1):
            for kind in ("master-full-run", "base-red-nightly"):
                with self.subTest(age=age, kind=kind):
                    self.store(self.row(kind, age=age))
                    fresh = 0 <= age < 36
                    result = self.classify([PY])
                    self.assertEqual(result["pre_existing"], [PY] if fresh else [])
                    self.assertEqual(result["known_red"], [PY] if fresh else [])
                    self.assertEqual(result["complete"], fresh)
                    selected = br.classify_selected("demo", self.sha, [PY], now=NOW)
                    self.assertEqual(selected, dict(known_red=[PY] if fresh else [],
                        rerun_on_base=[] if fresh else [PY], undetermined=not fresh))
                    observations.append(dict(kind=kind, age=age, classify=result, selected=selected))
        # A fresh Python-only run cannot freshen stale Dashboard evidence.
        self.store(self.row("base-red-nightly", age=37, ids=[PY, JS]),
                   self.row(ids=[PY]))
        self.assertEqual(self.classify([PY, JS])["pre_existing"], [PY])
        self.assertFalse(self.classify([PY, JS])["complete"])
        self.assertEqual(br.classify_selected("demo", self.sha, [PY, JS], now=NOW),
                         dict(known_red=[PY], rerun_on_base=[JS], undetermined=True))
        # Conversely, newer full green clears Python ids without erasing JS.
        self.store(self.row("base-red-nightly", age=1, ids=[PY, JS]), self.row(ids=[]))
        self.assertEqual(self.classify([PY, JS])["known_red"], [JS])
        self.assertTrue(self.classify([PY, JS])["complete"])
        body = json.dumps(observations, sort_keys=True)
        print(json.dumps(dict(url="scratch://master-full/AC3", status=200,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            body_excerpt=observations)))

    def test_invalid_incomplete_foreign_and_unauthorized_full_rows(self):
        invalid = [dict(complete=False), dict(project="foreign"), dict(suite="window"),
                   dict(covered=["pytest"]), dict(covered=["pytest", "vitest"]),
                   dict(green=True), dict(run_ids=[]), dict(run_ids=["bad"]),
                   dict(sha="a" * 39), dict(ts="broken"), dict(ids=[JS]),
                   dict(ids=[None])]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.store(self.row(**changes))
                self.assertEqual(br.known_red("demo"), {})
        self.store()
        # Actor fields cannot promote an event written in a forged file.
        (self.root / "events/L-builder-forged.jsonl").write_text(
            json.dumps(self.row(actor="base-red")) + "\n")
        self.assertEqual(br.known_red("demo"), {})
        for event in ("master-full-run", "selected-canary"):
            self.assertEqual(br.fold.EMITS[event], {"base-red"})

    def test_selected_ancestry_uses_the_kind_snapshot(self):
        root = self.sha
        tree = self.git("rev-parse", "HEAD^{tree}")
        unrelated = self.git("-c", "user.name=t", "-c", "user.email=t@localhost",
                             "commit-tree", tree, "-m", "unrelated")
        self.store(self.row("base-red-nightly", age=2, ids=[JS]),
                   self.row(age=1, ids=[PY], sha=unrelated))
        self.assertEqual(br.classify_selected("demo", root, [PY, JS], now=NOW),
                         dict(known_red=[JS], rerun_on_base=[PY], undetermined=True))
        self.store(self.row(age=2, ids=[PY]),
                   self.row("base-red", age=1, ids=[PG], complete=False,
                            source="rerun", sha=unrelated))
        self.assertEqual(br.classify_selected("demo", root, [PY, PG], now=NOW),
                         dict(known_red=[PY], rerun_on_base=[PG], undetermined=False))

    def test_real_append_and_fresh_reader(self):
        br._append("master-full-run", "demo", self.sha, complete=True, ids=[PY],
                   suite="full", covered=["pg-parity", "pytest"], green=False,
                   run_ids=["123"], merges_since_green=[], attribution_undetermined=True)
        code = ("import sys,json; sys.path.insert(0,sys.argv[1]); import base_red; "
                "print(json.dumps(base_red.known_red('demo')))")
        proc = self.real_run([sys.executable, "-I", "-c", code, str(fixtures.HERE)],
                             env=dict(os.environ), capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)[PY]["source"], "master-full")


if __name__ == "__main__":
    unittest.main(verbosity=2)
