#!/usr/bin/env python3
"""Focused, offline wall-cap and owned-temp regressions (L-spec-1713)."""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import os
import pathlib
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# dispatch probes the CLI at import time; even that probe must stay offline.
with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture", "")):
    import dispatch
import build_runner as runner
import fold
import grader_packet
import grading_env
import scratch


SID = "L-spec-9000"
TIP, BASE = "a" * 40, "b" * 40


def evidence(ac, body):
    text = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url=f"cli://test_grading_wait/{ac}", status=0,
        body_sha256=hashlib.sha256(text.encode()).hexdigest(), body_excerpt=text)))
    print(f"L-spec-1713 {ac} ok")


class GradingWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grading-wait-")
        self.addCleanup(self.temp.cleanup)
        self.top = pathlib.Path(self.temp.name).resolve()
        self.root = self.top / "ledger"
        self.content = self.root / "content"
        self.content.mkdir(parents=True)
        (self.content / f"{SID}.md").write_text("fixture")
        self.packet = self.top / "packet"
        self.packet.write_text("fixture packet")
        self.events = [dict(type="build-done", subject=SID, project="demo",
                            ready_sha=TIP, base_sha=BASE)]
        self.clock = datetime(2026, 10, 27, tzinfo=timezone.utc)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root),
            DOIT_SCRATCH=str(self.top / "scratch"), DOIT_NO_POKE="1"))
        aliases, backing = self.top / "aliases", self.top / "backing"
        aliases.mkdir()
        backing.mkdir()
        self.stack.enter_context(patch.multiple(runner, SHORT_TMP_ROOT=aliases,
                                                SHORT_TMP_BACKING_ROOT=backing))
        self.stack.enter_context(patch.multiple(fold, ROOT=self.root, EVENTS=self.root / "events"))
        self.stack.enter_context(patch.object(fold, "read_events", side_effect=lambda *a, **k: self.events[:]))
        self.stack.enter_context(patch.object(fold, "fold", side_effect=lambda ev: (
            {SID: {"evs": ev}}, {}, [], {SID: ev})))
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("unexpected command")))

    def emit_runner(self, spec, project, kind, **fields):
        self.events.append(dict(type=kind, subject=spec, project=project, **fields))

    def test_ac1(self):
        def tree(*args):
            deadlines.append(args[-1])
            return [], [], [], [], {}

        deadlines = []
        config = self.top / "grading.toml"
        config.write_text('[project.demo]\n[project.fast]\nrunner_wall_cap_s=37\n'
                          '[project.slow]\nrunner_wall_cap_s=91\n')
        with patch.dict(os.environ, DOIT_GRADING_TOML=str(config)), \
                patch.object(grading_env, "_project_checkout", return_value=self.top), \
                patch.object(runner, "_resolve", side_effect=lambda repo, sha, env, deadline: sha), \
                patch.object(runner, "_commands", return_value=([("AC1", "true")], [])), \
                patch.object(runner, "_run_tree", side_effect=tree) as checks, \
                patch.object(runner, "_event", side_effect=self.emit_runner), \
                patch.object(runner.time, "monotonic", return_value=100), \
                patch.object(signal, "setitimer") as alarm:
            for project, cap in (("demo", 1800), ("fast", 37), ("slow", 91)):
                self.events[0]["project"] = project
                path = runner.run(SID)
                self.assertEqual(deadlines[-2:], [100 + cap, 100 + cap])
                self.assertEqual([c.args[1] for c in alarm.call_args_list if c.args[1]], [cap])
                path.unlink()
                shutil.rmtree(path.with_suffix(""))
                alarm.reset_mock()
            checks.reset_mock()
            for invalid in (0, -1, True, False, "30", "bad", 1.5, None, [], {}):
                with patch.object(grading_env, "_project_row", return_value={"runner_wall_cap_s": invalid}):
                    with self.assertRaisesRegex(runner.Undetermined, "positive integer"):
                        runner.run(SID)
                checks.assert_not_called()
            with patch.object(runner, "_run_tree", side_effect=runner.WallCap("wall cap exceeded")):
                with self.assertRaisesRegex(runner.Undetermined, "wall cap exceeded"):
                    runner.run(SID)
            failed = self.events[-1]
            self.assertEqual(failed["type"], "evidence-bundle-failed")
            self.assertEqual(failed["reason"], "wall cap exceeded")
            self.assertEqual(failed["reason_code"], "wall-cap")
            self.assertIsNone(fold.required_reason(failed))
        evidence("AC1", dict(caps=[1800, 37, 91], invalid_rejected=10,
            matching_deadlines_and_alarms=True, reason=failed["reason"], reason_code=failed["reason_code"]))

    def dispatch_once(self):
        args = argparse.Namespace(role="grader", subject=SID, packet=str(self.packet),
            cwd=str(self.top), project="demo", charter=None, path=None, seat=False,
            mcp_config=None, timeout=None, max_usd=None)

        def emit(dst, base, type_, **fields):
            self.events.append(dict(base, type=type_, ts=self.clock.isoformat(), **fields))

        with patch.multiple(dispatch, ROOT=self.root, EVENTS=self.root / "events", CONTENT=self.content), \
                patch.object(dispatch, "emit", side_effect=emit), \
                patch.object(dispatch, "now", side_effect=lambda: self.clock.isoformat()), \
                patch.object(dispatch.models, "resolve", return_value=dict(backend="codex", model="fixture", source="fixture")), \
                patch.object(dispatch, "run_codex", side_effect=AssertionError("model call")), \
                patch.object(dispatch, "run_seat", side_effect=AssertionError("seat call")), \
                patch.object(dispatch, "grading_budget", return_value=None), \
                patch.object(dispatch, "ensure_room", return_value=True), \
                patch.object(dispatch.spawns, "gate", return_value=None), \
                patch("look._clear"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exited:
                dispatch.main(args)
            return exited.exception.code

    def test_ac2(self):
        with patch.object(grader_packet.subprocess, "Popen") as launched:
            for fields in ({"reason_code": "wall-cap", "reason": "wall cap exceeded"},
                           {"reason": "wall cap exceeded"}):
                self.events = self.events[:1]
                self.events.append(dict(type="evidence-bundle-failed", subject=SID, sha=TIP, **fields))
                launched.reset_mock()
                self.assertEqual(self.dispatch_once(), 2)
                infra = [e for e in self.events if e["type"] == "infra-failure"]
                self.assertEqual(len(infra), 1)
                self.assertEqual(infra[0]["class"], "runner-or-network")
                due = self.clock + timedelta(minutes=dispatch.RETRY_MINUTES[0])
                self.assertEqual(infra[0]["retry_at"], due.isoformat(timespec="seconds"))
                self.assertEqual(self.dispatch_once(), 2)
                launched.assert_not_called()
                self.clock = due
                self.assertEqual(self.dispatch_once(), 2)
                launched.assert_called_once()
                self.assertEqual(launched.call_args.args[0][-1], TIP)
                self.assertEqual(self.dispatch_once(), 2)
                launched.assert_called_once()
                self.assertFalse(any(e["type"] == "spawn-failed" or e.get("kind") == "failure-unknown"
                                     for e in self.events))
                # A fresh failed runner consumes the next rung, ending after four retries.
                for rung in range(1, len(dispatch.RETRY_MINUTES) + 1):
                    self.events.append(dict(type="evidence-bundle-failed", subject=SID, sha=TIP,
                                            ts=self.clock.isoformat(), **fields))
                    self.assertEqual(self.dispatch_once(), 2)
                    if rung < len(dispatch.RETRY_MINUTES):
                        latest = [e for e in self.events if e["type"] == "infra-failure"][-1]
                        self.assertEqual(latest["retry_at"], (self.clock + timedelta(
                            minutes=dispatch.RETRY_MINUTES[rung])).isoformat(timespec="seconds"))
                        self.clock = datetime.fromisoformat(latest["retry_at"])
                        self.assertEqual(self.dispatch_once(), 2)
                self.assertEqual(len([e for e in self.events if e["type"] == "infra-failure"]), 4)
                self.assertEqual(self.events[-1].get("kind"), "infra-exhausted")
            for reason in ("runner check failed", "prefix wall cap exceeded", "wall cap exceeded suffix"):
                self.events = self.events[:1] + [dict(type="evidence-bundle-failed", subject=SID, sha=TIP, reason=reason)]
                self.assertEqual(self.dispatch_once(), 1)
                self.assertEqual(self.events[-1]["type"], "spawn-failed")
                self.assertFalse(any(e["type"] == "infra-failure" for e in self.events))
            self.events = self.events[:1] + [dict(type="evidence-bundle-failed", subject=SID,
                sha=BASE, reason="wall cap exceeded", reason_code="wall-cap")]
            launched.reset_mock()
            self.assertEqual(self.dispatch_once(), 1)
            self.assertFalse(any(e["type"] == "infra-failure" for e in self.events))
            self.assertEqual(launched.call_args.args[0][-1], TIP)
            self.events = [dict(type="evidence-bundle-failed", subject=SID,
                sha=BASE, reason="wall cap exceeded", reason_code="wall-cap")]
            launched.reset_mock()
            self.assertEqual(self.dispatch_once(), 1)
            launched.assert_not_called()
            self.assertFalse(any(e["type"] == "infra-failure" for e in self.events))
        evidence("AC2", dict(new_and_historical=True, retry_once_when_due=True,
            ladder_exhausted=True, unrelated_failures_preserved=True, old_commit_not_retried=True))

    def test_ac3(self):
        runner_root = scratch.sub("runner")
        self.assertEqual(len(list(runner_root.iterdir())), 0)
        tails = self.top / "permanent"
        tails.mkdir()
        bundle = tails / "bundle.json"
        bundle.write_text("permanent fixture")
        observed = []
        outside = str(self.top / "outside")
        original_env = dict(os.environ)
        def required(argv, **kw):
            if "clone" in argv:
                pathlib.Path(argv[-1]).mkdir()
            return ""

        def provision(tree, auxiliary, row, env, deadline):
            tmp = pathlib.Path(env["TMPDIR"])
            self.assertEqual(env["TMPDIR"], env["TMP"])
            self.assertEqual(env["TMPDIR"], env["TEMP"])
            self.assertTrue(tmp.is_absolute() and tmp.parent == runner.SHORT_TMP_ROOT)
            self.assertEqual(tmp.resolve().parent, runner.SHORT_TMP_BACKING_ROOT)
            self.assertTrue(tmp.is_dir())
            self.assertNotEqual(str(tmp), outside)
            self.assertEqual(pathlib.Path(env["HOME"]), auxiliary / "home")
            self.assertEqual(pathlib.Path(env["DOIT_ROOT"]), auxiliary / "root")
            observed.append(tmp)
            # A real harmless child uses tempfile's environment lookup.
            with patch.object(subprocess, "Popen", REAL_POPEN):
                result = REAL_RUN([sys.executable, "-c",
                    "import tempfile,pathlib; p=pathlib.Path(tempfile.mkstemp()[1]); print(p.parent)"],
                    env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), str(tmp))
            return {}

        def check(*a, **kw):
            self.assertEqual(kw["env"]["TMPDIR"], str(observed[-1]))
            return 0, b"fixture output\n"

        def invoke(sha=TIP):
            return runner._run_tree(SID, sha, self.top, {}, [("AC1", "true")], tails, 0, 1000, 999)

        with patch.dict(os.environ, TMPDIR=outside, TMP=outside, TEMP=outside), \
                patch.object(runner, "_required", side_effect=required), \
                patch.object(runner, "_provision", side_effect=provision), \
                patch.object(runner, "_process", side_effect=check) as process, \
                patch.object(grading_env, "teardown"):
            parent = dict(os.environ)
            invoke()
            invoke(BASE)
            self.assertNotEqual(observed[0], observed[1])
            self.assertEqual(dict(os.environ), parent)
            self.assertEqual(list(runner_root.iterdir()), [])
            for failure in (RuntimeError("check exception"), runner.WallCap("wall cap exceeded")):
                process.side_effect = failure
                with self.assertRaises(type(failure)):
                    invoke()
                self.assertEqual(list(runner_root.iterdir()), [])
            for sig in (signal.SIGINT, signal.SIGTERM):
                process.side_effect = lambda *a, **kw: runner._interrupted(sig, None)
                with self.assertRaisesRegex(runner.Undetermined, "interrupted"):
                    invoke()
                self.assertEqual(list(runner_root.iterdir()), [])
            mkdir = pathlib.Path.mkdir
            for part in ("home", "root"):
                def partial(path, *a, **kw):
                    if path.name == part:
                        raise OSError("partial directory setup")
                    return mkdir(path, *a, **kw)
                with patch.object(pathlib.Path, "mkdir", partial):
                    with self.assertRaises(OSError):
                        invoke()
                self.assertEqual(list(runner_root.iterdir()), [])
            with patch.object(runner, "short_tmpdir", side_effect=OSError("allocation failed")):
                with self.assertRaises(OSError):
                    invoke()
            self.assertEqual(list(runner_root.iterdir()), [])
            self.assertEqual(list(runner.SHORT_TMP_ROOT.iterdir()), [])
            self.assertEqual(list(runner.SHORT_TMP_BACKING_ROOT.iterdir()), [])
            auxiliary = runner_root / f"{SID}-{TIP[:12]}-{os.getpid()}-env"
            auxiliary.mkdir()
            marker = auxiliary / "keep"
            marker.write_text("unowned fixture")
            with self.assertRaisesRegex(runner.Undetermined, "already exists"):
                invoke()
            self.assertEqual(marker.read_text(), "unowned fixture")
            marker.unlink()
            auxiliary.rmdir()
        self.assertEqual(dict(os.environ), original_env)
        self.assertEqual(bundle.read_text(), "permanent fixture")
        self.assertEqual((tails / "0.tail").read_bytes(), b"fixture output\n")
        evidence("AC3", dict(child_temp_owned=True, tip_base_isolated=True, parent_unchanged=True,
            owned_residue=0, preexisting_preserved=1, permanent_evidence_preserved=True))


REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen
if __name__ == "__main__":
    unittest.main()
