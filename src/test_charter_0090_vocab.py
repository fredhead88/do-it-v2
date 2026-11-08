#!/usr/bin/env python3
"""Scratch-only vocabulary, checkout lifecycle and durable ledger regressions."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fold
import spec_preflight as sp

SOURCE = Path(__file__).resolve().parent


class AC1(unittest.TestCase):
    def test_emitter_and_required_field_matrix(self):
        cases = {
            "writes-granted": ("spec-preflight", ("tests", "why", "spec_written_src"),
                dict(tests=[], why="fixture", spec_written_src="writer:1")),
            "build-timeout": ("builder", ("branch", "last_commit", "wip_commits", "spawn"),
                dict(branch="fixture", last_commit="fixture-commit", wip_commits=[], spawn="fixture")),
        }
        for kind, (actor, required, fields) in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(fold.REQUIRED[kind], required)
                self.assertEqual(fold.EMITS[kind], {actor})
                event = dict(type=kind, subject="L-spec-9001", **fields)
                self.assertIsNone(fold.check_append(event, actor))
                for field in required:
                    missing = {k: v for k, v in event.items() if k != field}
                    self.assertIn(field, fold.check_append(missing, actor))
                for other in ("operator", "grader", "executor", "builder", "spec-preflight"):
                    if other != actor:
                        self.assertIsNotNone(fold.check_append(event, other))
                list_field = "tests" if kind == "writes-granted" else "wip_commits"
                for invalid in (None, "", {}):
                    self.assertIsNotNone(fold.required_reason({**event, list_field: invalid}))
        allowed = {"thinker", "operator", "planner", "tick", "spec-preflight"}
        self.assertEqual(fold.EMITS["dependency-hold"], allowed)
        hold = dict(type="dependency-hold", subject="L-spec-9001",
                    waits_for="L-spec-9002", why="fixture")
        for actor in allowed:
            self.assertIsNone(fold.check_append(hold, actor))
        for actor in ("builder", "grader", "executor"):
            self.assertIsNotNone(fold.check_append(hold, actor))


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="charter-0090-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.repo = self.root / "repos" / "do-it-v2"
        self.repo.mkdir(parents=True)
        (self.root / "content").mkdir()
        (self.root / "events").mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "DOIT_ROOT": str(self.root), "DOIT_SCRATCH": str(self.base / "scratch"),
            "DOIT_PROJECT": "", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "DOIT_TEST_PROCESS": "1",
        }))
        self.stack.enter_context(mock.patch.object(sp, "after_run", []))
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "sentinel.txt").write_text("committed fixture sentinel\n")
        self.git("add", "sentinel.txt")
        self.git("commit", "-qm", "fixture")
        self.now = datetime.now(timezone.utc)
        self.serial = 0

    def proc(self, argv, **kwargs):
        return subprocess.run(argv, text=True, capture_output=True, check=True,
                              timeout=30, **kwargs).stdout.strip()

    def git(self, *args, checkout=None):
        return self.proc(["git", "-C", str(checkout or self.repo), *args])

    def append(self, actor, kind, sid, **fields):
        self.serial += 1
        fields = dict(project="do-it-v2", **fields)
        fields.setdefault("ts", (self.now - timedelta(minutes=10)
                                  + timedelta(seconds=self.serial)).isoformat())
        argv = [str(sp.DOIT), "append", kind, sid, "--json"]
        argv.extend(k + ":=" + json.dumps(v) for k, v in fields.items())
        env = {**os.environ, "DOIT_LEDGER_FILE": f"L-{actor}-fixture.jsonl"}
        return json.loads(self.proc(argv, env=env))

    def spec(self, sid="L-spec-9001"):
        path = self.root / "content" / (sid + ".md")
        path.write_text("## Interfaces\nWrites: src/fixture.py\n\n"
                        "## Acceptance\nAC1 [backend]: fixture works.\n"
                        "## Verification\n```bash\n/usr/bin/python3 -c 'import json'\n```\n")
        self.append("spec-writer", "spec-written", sid, path=str(path),
                    footprint=["src/fixture.py"])
        return path

    def tick(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sp.tick(root=self.root, now=self.now), 0)

    def records(self, kind):
        return [e for e in sp._events(self.root) if e["type"] == kind]


class AC3(ScratchCase):
    def test_live_checkout_order_mutation_and_standalone(self):
        self.spec()
        written = self.records("spec-written")[-1]
        identity = sp._text_identity(self.root, written)
        trace, checkouts = [], []

        def first(sid, body, event, checkout):
            trace.append("first")
            self.assertEqual(event, written)
            self.assertEqual(sid, "L-spec-9001")
            self.assertEqual((checkout / "sentinel.txt").read_text(), "committed fixture sentinel\n")
            self.assertNotEqual(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"],
                cwd=checkout, capture_output=True).returncode, 0)
            checkouts.append(checkout)
            body.setdefault("refused", []).append(dict(reason="fixture refusal"))
            return "ignored"

        def second(sid, body, event, checkout):
            trace.append("second")
            self.assertEqual(body["refused"], [dict(reason="fixture refusal")])
            self.assertEqual(checkout, checkouts[-1])
            body["refused"].append(dict(reason="second fixture refusal"))

        sp.after_run.extend([first, second])
        original_validate, original_identity, original_append = sp._validate, sp._text_identity, sp._append

        def validate(body):
            trace.append("validate")
            return original_validate(body)

        def identify(*args):
            trace.append("identity")
            return original_identity(*args)

        def append(root, body, **kwargs):
            trace.append("append")
            self.assertNotIn(sp._DEFERRED, body)
            self.assertEqual(len(body["refused"]), 2)
            return original_append(root, body, **kwargs)

        with ExitStack() as stack:
            for name in ("_check_interpreter", "_check_shas", "_check_reach", "_check_tests"):
                original = getattr(sp, name)

                def check(*args, _name=name, _original=original):
                    trace.append(_name)
                    return _original(*args)

                stack.enter_context(mock.patch.object(sp, name, side_effect=check))
            stack.enter_context(mock.patch.object(sp, "_validate", side_effect=validate))
            stack.enter_context(mock.patch.object(sp, "_text_identity", side_effect=identify))
            stack.enter_context(mock.patch.object(sp, "_append", side_effect=append))
            runner = stack.enter_context(mock.patch.object(sp, "run", wraps=sp.run))
            self.tick()
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args, ("L-spec-9001",))
        self.assertIs(runner.call_args.kwargs["hooks"], sp.after_run)
        self.assertEqual(runner.call_args.kwargs["expected_identity"], identity)
        self.assertEqual(trace[1:], ["_check_interpreter", "_check_shas", "_check_reach",
            "_check_tests", "validate", "identity", "first", "identity", "second", "identity", "append"])
        self.assertTrue(all(not checkout.exists() for checkout in checkouts))
        self.assertEqual(len(self.records("spec-preflight")[0]["refused"]), 2)
        trace.clear()
        with mock.patch.object(sp, "_append") as appender:
            sp.run("L-spec-9001", root=self.root)
        appender.assert_not_called()
        self.assertEqual(trace, [])


class AC4(ScratchCase):
    def failure(self, exception):
        self.spec()
        self.spec("L-spec-9002")
        calls = []

        def failing(sid, body, written, checkout):
            calls.append(sid)
            body.setdefault("refused", []).append(dict(reason="retained"))
            sp._finding(body, 1, "fixture", "retained")
            raise exception("fixture")

        sp.after_run.append(failing)
        self.tick()
        self.assertEqual(calls, ["L-spec-9001", "L-spec-9002"])
        records = self.records("spec-preflight")
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertTrue(any(failing.__qualname__ in reason and exception.__name__ in reason
                                for reason in record["undetermined"]))
            self.assertEqual(record["refused"], [dict(reason="retained")])
            self.assertEqual(record["findings"], [dict(check=1, id="fixture", line="retained")])

    def test_runtime_error(self):
        self.failure(RuntimeError)

    def test_key_error(self):
        self.failure(KeyError)

    def test_type_error(self):
        self.failure(TypeError)

    def test_assertion_error(self):
        self.failure(AssertionError)

    def test_control_signal_not_swallowed(self):
        self.spec()

        def control(*args):
            raise KeyboardInterrupt()

        sp.after_run.append(control)
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        self.assertEqual(self.records("spec-preflight"), [])

    def test_failed_acquisition_is_none_and_undetermined(self):
        self.spec()
        observed = []
        sp.after_run.append(lambda sid, body, event, checkout: observed.append(checkout))
        original = sp._process

        def process(argv, **kwargs):
            if "show-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return original(argv, **kwargs)

        with mock.patch.object(sp, "_process", side_effect=process):
            self.tick()
        self.assertEqual(observed, [None])
        self.assertTrue(self.records("spec-preflight")[0]["undetermined"])

    def test_change_after_checks_defers_and_continues(self):
        path = self.spec()
        self.spec("L-spec-9002")
        side_effects, returned = [], []
        sp.after_run.append(lambda sid, *args: side_effects.append(sid))
        original_check, original_run = sp._check_tests, sp.run

        def check(body, *args):
            original_check(body, *args)
            if body["spec"] == "L-spec-9001":
                path.write_text(path.read_text() + "changed after checks\n")

        def run(*args, **kwargs):
            body = original_run(*args, **kwargs)
            returned.append(body)
            return body

        with mock.patch.object(sp, "_check_tests", side_effect=check), \
                mock.patch.object(sp, "run", side_effect=run):
            self.tick()
        self.assertEqual(side_effects, ["L-spec-9002"])
        self.assertEqual([e["subject"] for e in self.records("spec-preflight")], ["L-spec-9002"])
        self.assertTrue(all(sp._DEFERRED not in body for body in returned))

    def test_missing_and_unreadable_pre_hook_identity(self):
        self.spec()
        observed = []
        sp.after_run.append(lambda *args: observed.append(True))
        original = sp._text_identity
        for unavailable in (None, OSError("unreadable")):
            count = 0

            def identify(*args):
                nonlocal count
                count += 1
                if count == 2:
                    if isinstance(unavailable, Exception):
                        raise unavailable
                    return unavailable
                return original(*args)

            with mock.patch.object(sp, "_text_identity", side_effect=identify):
                self.tick()
        self.assertEqual(observed, [])
        self.assertEqual(self.records("spec-preflight"), [])

    def test_hook_time_change_skips_remaining_hook_and_defers_append(self):
        path = self.spec()
        observed = []

        def change(*args):
            observed.append("side effect in residual window")
            path.write_text(path.read_text() + "changed inside hook\n")

        sp.after_run.extend([change, lambda *args: observed.append("must not run")])
        self.tick()
        self.assertEqual(observed, ["side effect in residual window"])
        self.assertEqual(self.records("spec-preflight"), [])
        # A sole hook has no next pre-hook guard: tick's post-run guard defers it.
        sp.after_run[:] = [change]
        self.tick()
        self.assertEqual(len(observed), 2)
        self.assertEqual(self.records("spec-preflight"), [])

    def test_persisted_revision_dedup_and_new_revision(self):
        self.spec()
        observed = []
        sp.after_run.append(lambda sid, body, event, checkout: observed.append(event["_src"]))
        original_process = sp._process

        def process(argv, **kwargs):
            # Keep strict append on the production path, with the same fixture
            # clock as spec-written (the CLI otherwise uses wall-clock seconds).
            if argv[1:3] == ["append", "spec-preflight"]:
                argv = [*argv, "ts=" + self.now.isoformat()]
            return original_process(argv, **kwargs)

        self.stack.enter_context(mock.patch.object(sp, "_process", side_effect=process))
        first = self.records("spec-written")[-1]["_src"]
        self.tick()
        self.tick()
        self.assertEqual(observed, [first])
        # The new revision must follow the actual appended preflight timestamp.
        self.now += timedelta(minutes=20)
        self.spec()
        newest = self.records("spec-written")[-1]["_src"]
        self.tick()
        self.tick()
        self.assertNotEqual(first, newest)
        self.assertEqual(observed, [first, newest])


class AC5(ScratchCase):
    def test_strict_append_and_fresh_process_canonical_fold(self):
        self.spec()
        written = self.records("spec-written")[-1]
        tests = [dict(test="src/test_fixture.py", source="coupled", kind="backend", ac_bound=["AC1"])]
        self.append("spec-preflight", "writes-granted", "L-spec-9001", tests=tests,
                    why="fixture coupling", spec_written_src=written["_src"])
        self.append("builder", "build-timeout", "L-spec-9001", branch="fixture",
                    last_commit=self.git("rev-parse", "HEAD"), wip_commits=[], spawn="fixture")
        self.append("spec-preflight", "dependency-hold", "L-spec-9001",
                    waits_for="L-spec-9002", why="fixture prerequisite")

        def mutate(sid, body, event, checkout):
            body.setdefault("refused", []).append(dict(reason="durable fixture mutation"))

        sp.after_run.append(mutate)
        self.tick()
        code = """
import json, fold, effective_writes
events = fold.read_events(use_cache=False)
specs, charters, ignored, subjects = fold.fold(events)
rows = subjects['L-spec-9001']
print(json.dumps(dict(ignored=ignored, rows=rows,
    grant=effective_writes.effective_grant('L-spec-9001', 'Writes: src/fixture.py', rows),
    tests=effective_writes.granted_tests('L-spec-9001', rows))))
"""
        env = {**os.environ, "PYTHONPATH": str(SOURCE)}
        report = json.loads(self.proc([sys.executable, "-c", code], env=env))
        self.assertEqual(report["ignored"], [])
        self.assertEqual(report["grant"], ["src/fixture.py", "src/test_fixture.py"])
        self.assertEqual(report["tests"], tests)
        rows = {e["type"]: e for e in report["rows"]}
        for kind, actor in (("writes-granted", "spec-preflight"), ("build-timeout", "builder"),
                            ("dependency-hold", "spec-preflight"), ("spec-preflight", "spec-preflight")):
            self.assertEqual(rows[kind]["subject"], "L-spec-9001")
            self.assertEqual(rows[kind]["actor"], actor)
            self.assertTrue(rows[kind]["_src"].startswith(f"L-{actor}-"))
        self.assertEqual(rows["writes-granted"]["spec_written_src"], written["_src"])
        self.assertEqual(rows["build-timeout"]["wip_commits"], [])
        self.assertEqual(rows["dependency-hold"]["waits_for"], "L-spec-9002")
        self.assertEqual(rows["spec-preflight"]["refused"], [dict(reason="durable fixture mutation")])
        self.assertNotIn(sp._DEFERRED, rows["spec-preflight"])


if __name__ == "__main__":
    unittest.main()
