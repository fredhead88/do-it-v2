#!/usr/bin/env python3
"""Offline snapshot-reader regressions for master full-suite evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pathlib
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
            return list(self.runs)
        if key == "jobs":
            return self.jobs[1]
        if key == "artifacts":
            rid = int(path.split("/")[-2])
            return [dict(id=rid, name="ci-selection-request", expired=False)]
        raise AssertionError((path, key))

    def binary(self, argv, **kw):
        if argv[0] == "base-red-fake-gh" and "/artifacts/" in argv[2]:
            rid = int(argv[2].split("/")[-2])
            data = fixtures.archive(**{"ci-selection-request.json": json.dumps(self.artifacts[rid])})
            return subprocess.CompletedProcess(argv, 0, data, b"")
        return super().binary(argv, **kw)

    def transport(self, argv, **kw):
        self.assertEqual(argv[:3], ["gh", "workflow", "run"])
        self.assertFalse(kw.get("wait", True))
        self.dispatches.append(argv)
        return 0, "", ""

    def tick(self):
        return self.mf.tick("albert-scott")

    def ready(self, kind="full", *, rid=1, ids=None, conclusion="success", **changes):
        if ids is None: ids = []
        mode = "full" if kind == "full" else "selected"
        self.artifacts[rid] = dict(mode=mode, reason="fixture")
        if mode == "selected": self.artifacts[rid]["pytest_files"] = ["api/tests/test_canary.py"]
        text = (f"CI-SELECTION mode={mode} reason=fixture files=1\n"
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
        intent = dict(kind=kind, sha=self.sha, slot=0, run_id=str(rid),
                      sent_at=self.clock.isoformat(), before_ids=[],
                      inputs=dict(suite="full" if kind == "full" else "window", full_run=True,
                                  select_base="" if kind == "full" else self.base, rerun_ids="", checks=""))
        directory = self.root / "master-full/albert-scott"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "state.json"
        state = json.loads(path.read_text()) if path.exists() else dict(pending=[], slots={}, last_full=self.sha)
        state["slots"] = dict(full=int(self.clock.timestamp()) // 10800,
                              canary=int(self.clock.timestamp()) // 43200)
        state["pending"].append(intent)
        self.mf._save(path, state)
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
                " stack.enter_context(patch.object(master_full, '_artifact', return_value={'mode':'full'}))\n"
                " stack.enter_context(patch.object(master_full.br, '_logs', return_value=[('pytest.txt','CI-SELECTION mode=full reason=fixture files=1\\nSUMMARY pytest_baseline_gate.py check: PASS new=0 changed=0')]))\n"
                " result=master_full.tick('albert-scott')\n"
                " assert result['collected'], result\n")
        self.fresh(code + "exec(" + repr(body) + ")")
        threads = [threading.Thread(target=self.tick) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len([r for r in br._rows("albert-scott") if r["type"] == "master-full-run"]), 1)



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
