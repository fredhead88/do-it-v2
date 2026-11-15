#!/usr/bin/env python3
"""L-spec-2446: dispatch's run clock honours its offered window."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class WindowTimeoutTests(unittest.TestCase):
    def dispatch_timeout(self, role, backend, expected, *, window=None,
                         observed=False, timeout=None, fallback=None):
        with tempfile.TemporaryDirectory(prefix="dispatch-window-") as name, \
                tempfile.TemporaryDirectory(prefix="dispatch-window-scratch-") as scratch, \
                contextlib.ExitStack() as stack:
            root = Path(name)
            stack.enter_context(patch.dict(os.environ, HOME=name, DOIT_ROOT=name,
                DOIT_SCRATCH=scratch, XDG_CONFIG_HOME=name, DOIT_NO_POKE="1"))
            for key in ("DOIT_LEDGER_FILE", "DOIT_SEAT", "DOIT_PROJECT"):
                os.environ.pop(key, None)
            # Import-time CLI probing is also stubbed: no process or network.
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "fixture", "")):
                import dispatch
                import fold
                import grader_view
                import grading_env
                import models
                import wip_resume
            for directory in ("content", "events", "repo", "view"):
                (root / directory).mkdir()
            subject = "L-spec-9946"
            spec = root / "content" / f"{subject}.md"
            spec.write_text("# Fixture\n" + (f"window_min: {window}\n" if window is not None else "") + "## Goal\n")
            packet = root / "packet.md"
            packet.write_text("Fixture.\nPINNED_BASE_SHA: " + "a" * 40 + "\n")
            seeds = [dict(type="spec-written", spec=subject, path=str(spec),
                          ac_types=["observed-data"] if observed else ["backend"]),
                     dict(type="spawn-done", actor="spec-auditor", bad_cut=False),
                     dict(type="build-done", base_sha="a" * 40, ready_sha="b" * 40)]
            (root / "events" / "L-fixture-9900.jsonl").write_text("".join(
                json.dumps(dict(v=1, ts="2026-11-14T00:00:00Z", subject=subject,
                                project="fixture", **row)) + "\n" for row in seeds))
            stack.enter_context(patch.multiple(dispatch, ROOT=root, CONTENT=root / "content",
                                               EVENTS=root / "events", SEAT=root / "seat"))
            stack.enter_context(patch.multiple(fold, ROOT=root, EVENTS=root / "events"))
            mapping = dict(backend=backend, model="fixture", source="fixture")
            if fallback:
                mapping["fallback"] = dict(backend=fallback, model="fixture-fallback")
            stack.enter_context(patch.object(models, "resolve", return_value=mapping))
            for method in ("builder_worktree_preflight", "builder_effective_base", "builder_branch_preflight"):
                stack.enter_context(patch.object(dispatch, method))
            stack.enter_context(patch.object(dispatch, "ensure_room", return_value=True))
            stack.enter_context(patch.object(dispatch, "provision_worktree_env", return_value="absent"))
            stack.enter_context(patch.object(dispatch, "porcelain", return_value=""))
            stack.enter_context(patch.object(dispatch.atexit, "register"))
            stack.enter_context(patch.object(dispatch, "relay_serving", return_value=True))
            stack.enter_context(patch.object(dispatch.grader_packet, "gate", return_value=None))
            stack.enter_context(patch.object(grading_env, "required", return_value=[]))
            stack.enter_context(patch.object(grading_env, "preflight", return_value=[]))
            stack.enter_context(patch.object(grader_view, "build", return_value=root / "view"))
            stack.enter_context(patch.object(wip_resume, "on_timeout", return_value=""))
            def no_process(cmd, *args, **kwargs):
                if cmd == ["claude", "--version"]:
                    return subprocess.CompletedProcess(cmd, 0, "fixture", "")
                raise AssertionError(f"unexpected subprocess: {cmd}")
            stack.enter_context(patch.object(subprocess, "run", side_effect=no_process))
            stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")))
            runners = {name: stack.enter_context(patch.object(dispatch, name,
                side_effect=subprocess.TimeoutExpired("fixture", expected * 60)))
                for name in ("run_seat", "run_codex", "run_claude")}
            if fallback:
                runners["run_codex"].side_effect = None
                runners["run_codex"].return_value = subprocess.CompletedProcess([], 1,
                    json.dumps(dict(is_error=True, result="fixture refusal")), "")
            args = argparse.Namespace(role=role, subject=subject, packet=str(packet),
                path=None, cwd=str(root / "repo"), charter=None, project="fixture",
                mcp_config=None, timeout=timeout, max_usd=None, seat=False)
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as stopped:
                    dispatch.main(args)
            self.assertEqual(stopped.exception.code, 2)
            route = {"seat": "run_seat", "codex": "run_codex", "claude-p": "run_claude"}
            called = {route[backend]} | ({route[fallback]} if fallback else set())
            for name, runner in runners.items():
                self.assertEqual(runner.call_count, int(name in called), name + ": " + stderr.getvalue())
                if name in called:
                    self.assertEqual(runner.call_args.args[6 if name == "run_codex" else 4 if name == "run_seat" else 3], expected * 60)
            rows = fold.read_events(use_cache=False)
            start = next(e for e in rows if e["type"] in ("spawn-started", "build-started"))
            failure = rows[-1]["detail"]
            self.assertIn(f"timeout after {expected} min", failure)
            offered = 240 if window and window > 240 else max(dispatch.ROLES[role][1], window or 0,
                45 if observed and backend == "seat" and role in ("reviewer", "grader") else 0)
            self.assertEqual(start["window_min"], offered)
            if "run_seat" in called:
                self.assertEqual(runners["run_seat"].call_args.kwargs["window"], offered * 60)
            body = dict(role=role, backend=backend, fallback=fallback, timeout_s=expected * 60,
                        window_min=start["window_min"], failure=failure)
            print(json.dumps(dict(url="cli://dispatch-window-timeout", status=0,
                body_sha256=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                body_excerpt=body)))

    def test_ac1_spec_window_all_backends(self):
        for backend in ("seat", "codex", "claude-p"):
            with self.subTest(backend=backend):
                self.dispatch_timeout("reviewer", backend, 60, window=60)

    def test_ac1_observed_data_grader(self):
        self.dispatch_timeout("grader", "seat", 45, observed=True)

    def test_ac2_explicit_timeout_all_backends(self):
        for backend in ("seat", "codex", "claude-p"):
            with self.subTest(backend=backend):
                self.dispatch_timeout("reviewer", backend, 20, window=60, timeout=20)

    def test_ac2_default_role_caps_all_backends(self):
        for role, minutes in (("reviewer", 30), ("grader", 30), ("builder", 90)):
            for backend in ("seat", "codex", "claude-p"):
                with self.subTest(role=role, backend=backend):
                    self.dispatch_timeout(role, backend, minutes)

    def test_window_cap_and_lower_override(self):
        self.dispatch_timeout("reviewer", "codex", 240, window=500)
        self.dispatch_timeout("reviewer", "seat", 30, window=10)

    def test_fallback_keeps_chosen_limit(self):
        for backend in ("seat", "claude-p"):
            with self.subTest(fallback=backend):
                self.dispatch_timeout("reviewer", "codex", 60, window=60, fallback=backend)
                self.dispatch_timeout("reviewer", "codex", 20, window=60, timeout=20, fallback=backend)


if __name__ == "__main__":
    unittest.main()
