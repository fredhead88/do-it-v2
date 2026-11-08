#!/usr/bin/env python3
"""Standing direct-entry refusal regressions; all writes/processes are scratch-only.

Run without arguments, or select EvidenceRefusals, StartRefusals,
GateStructural or GateScanner. No tick, detached parent or hand-in precheck
participates in these calls. Scanner scripts are committed on fixture main.
"""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Import-time roots must also be disposable. Do not inherit credentials, Git
# routing, production ledger destinations, config, or scanner overrides.
_bootstrap = tempfile.TemporaryDirectory(prefix="retained-refusals-")
_safe_env = dict(PATH=os.defpath, HOME=_bootstrap.name + "/home",
                 XDG_CONFIG_HOME=_bootstrap.name + "/config",
                 DOIT_ROOT=_bootstrap.name + "/ledger",
                 DOIT_SCRATCH=_bootstrap.name + "/scratch",
                 DOIT_NO_POKE="1", DOIT_READ_CACHE="0", DOIT_TEST_PROCESS="1",
                 GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
for _key in ("DOIT_TEST_MACHINE_ROOT", "DOIT_TEST_LIVE_ROOT", "TMPDIR"):
    if _key in os.environ:
        _safe_env[_key] = os.environ[_key]
_environment = patch.dict(os.environ, _safe_env, clear=True)
_environment.start()
for _key in ("HOME", "XDG_CONFIG_HOME", "DOIT_ROOT", "DOIT_SCRATCH"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

import dispatch  # noqa: E402 -- isolation precedes environment-capturing imports
import fold
import grader_packet
import grading_env
import merge_gate as mg
import models
import panes
import spawns


class UnpaidBoundary(BaseException):
    """A control crossed the guard; stop before routing/provisioning/spend."""


class Refused(Exception):
    def __init__(self, why, *, reason=None):
        self.why, self.reason = why, reason


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="case-", dir=_bootstrap.name))
        # The evidence runner's TMPDIR can be a symlink. Dispatch resolves cwd;
        # seed the same physical worktree identity in live-start fixtures.
        self.top = Path(self.temp).resolve()
        self.root = self.top / "ledger"
        self.events, self.content = self.root / "events", self.root / "content"
        self.events.mkdir(parents=True)
        self.content.mkdir()
        self.scratch = self.top / "scratch"
        self.scratch.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            "DOIT_ROOT": str(self.root), "DOIT_SCRATCH": str(self.scratch),
            "DOIT_GATE_LEDGER_FILE": "L-executor-fixture.jsonl",
            "DOIT_LEDGER_FILE": "L-executor-fixture.jsonl"}))
        self.stack.enter_context(patch.multiple(fold, ROOT=self.root, EVENTS=self.events,
                                               BOARD=self.root / "board.md"))
        self.stack.enter_context(patch.multiple(dispatch, ROOT=self.root,
                                               EVENTS=self.events, CONTENT=self.content))
        self.stack.enter_context(patch.multiple(models, ROOT=self.root,
                                               PATH=self.root / "models.toml"))
        # main_ intentionally changes module run state. Restore it even on errors.
        for name in ("_TOP", "_COMMON", "_RUN_ACTIVE", "_START", "_RESOLVED",
                     "LAST_APPEND_ONLY", "LAST_LIST_ERROR", "DEADLINE",
                     "GIT_TIMEOUT", "REVERT_DEPTH", "REVERT_MAX_PATHS", "MIGRATIONS",
                     "_KNOBS", "_DEADLINE_OVERRIDE", "DETECT_SECRETS_TIMEOUT"):
            value = getattr(mg, name)
            if isinstance(value, (dict, list)):
                value = value.copy()
            self.stack.enter_context(patch.object(mg, name, value))
        previous = Path.cwd()
        self.stack.callback(os.chdir, previous)
        self.seq = 0
        self.observations = []

    def tearDown(self):
        if self.observations:
            result = self._outcome.result
            failed = any(case is self or getattr(case, "test_case", None) is self
                         for case, _ in result.errors + result.failures)
            body = json.dumps(self.observations, sort_keys=True)
            print(json.dumps(dict(url="cli://refusals-retained/" + self.id(), status=int(failed),
                body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def seed(self, kind, subject="L-spec-9901", actor="executor", **fields):
        self.seq += 1
        row = dict(v=1, type=kind, subject=subject,
                   ts=(datetime.now(timezone.utc) - timedelta(minutes=10)
                       + timedelta(seconds=self.seq)).isoformat(), **fields)
        with (self.events / f"L-{actor}-fixture.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        return row

    def rows(self):
        # Reopen every JSONL after the entry point exits; never assert only on
        # an emitter spy or a reader's pre-call cache.
        return [json.loads(line) for path in sorted(self.events.glob("*.jsonl"))
                for line in path.read_text().splitlines() if line.strip()]

    def git(self, *args, repo=None):
        result = subprocess.run(["git", "-C", str(repo or self.repo), *args],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def put(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "inert fixture")
        return self.git("rev-parse", "HEAD")

    def repository(self, files=None, older=None, scanner="clean", name="repo"):
        self.repo = self.top / name
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@localhost")
        self.marker = self.top / (name + "-scanner-calls")
        # This file is on MAIN before the branch point. Its marker proves
        # production secret_scan actually ran the trusted scanner bytes.
        script = ("#!/bin/bash\n" + "printf 'invoked\\n' >> "
                  + shlex.quote(str(self.marker)) + "\n")
        if scanner == "finding":
            script += "printf '  added.py:1 -- Inert Fixture Finding\\n'\nexit 1\n"
        elif scanner == "malformed":
            script += "printf 'inert unrecognized diagnostic\\n'\nexit 1\n"
        elif scanner == "tool":
            script += "exit 2\n"
        else:
            script += "exit 0\n"
        self.put("scripts/ci/detect-secrets-changed.sh", script)
        self.put("base.txt", "base\n")
        for path, text in (older or {}).items():
            self.put(path, text)
        if older:
            self.commit()
        for path, text in (files or {}).items():
            self.put(path, text)
        self.main_sha = self.commit()
        self.git("checkout", "-qb", "work")
        return self.repo

    def scanner_calls(self):
        return len(self.marker.read_text().splitlines()) if self.marker.exists() else 0


class DispatchCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.repository()
        self.subject = "L-spec-9901"
        self.packet = self.top / "packet.md"
        self.packet.write_text("PINNED_BASE_SHA: " + self.main_sha + "\n")
        (self.root / "models.toml").write_text(
            '[defaults]\nbackend="codex"\n'
            '[contracts.builder]\nmodel="fixture"\n'
            '[contracts.grader]\nmodel="fixture"\n')

    def live(self, role="builder", subject=None, **fields):
        return self.seed("build-started" if role == "builder" else "spawn-started",
            subject or self.subject, actor=role, spawn=f"L-{role}-{9800 + self.seq:04d}",
            worktree=str(self.repo), waiter_pid=os.getpid(), waiter_host=socket.gethostname(),
            waiter_proc_start=panes.proc_start(os.getpid()), window_min=90, **fields)

    def invoke(self, reason=None, role="builder", control=False, charter=None):
        args = argparse.Namespace(role=role, subject=self.subject, packet=str(self.packet),
            path=None, cwd=str(self.repo), charter=charter, project="fixture",
            mcp_config=None, timeout=None, max_usd=None, seat=False)
        before = {p.name for p in self.events.glob("*.jsonl")}
        with ExitStack() as stack:
            # Only unrelated resource/process boundaries are intercepted.
            stack.enter_context(patch.object(dispatch.atexit, "register"))
            for name in ("run_codex", "run_claude", "run_seat"):
                backend = stack.enter_context(patch.object(dispatch, name,
                    side_effect=AssertionError("paid backend reached")))
                stack.callback(backend.assert_not_called)
            launcher = stack.enter_context(patch.object(grader_packet.subprocess, "Popen"))
            free = dispatch.shutil.disk_usage(self.scratch)._replace(free=100 * 2**30)
            stack.enter_context(patch.object(dispatch.shutil, "disk_usage", return_value=free))
            if control:
                target, symbol = (dispatch, "_room_need_gb") if role == "builder" else (grading_env, "required")
                sentinel = stack.enter_context(patch.object(target, symbol, side_effect=UnpaidBoundary))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if control:
                    with self.assertRaises(UnpaidBoundary):
                        dispatch.main(args)
                    sentinel.assert_called_once()
                else:
                    with self.assertRaises(SystemExit) as stopped:
                        dispatch.main(args)
                    self.assertNotEqual(stopped.exception.code, 0)
            calls = [list(call.args[0]) for call in launcher.call_args_list]
            if role != "grader":
                self.assertEqual(calls, [])
        new = [json.loads(line) for p in self.events.glob("*.jsonl") if p.name not in before
               for line in p.read_text().splitlines()]
        self.assertFalse(any(e["type"] in ("spawn-started", "build-started") for e in new))
        if control:
            self.assertEqual(new, [])
        else:
            terminal = [e for e in new if e["type"] == "spawn-failed"]
            self.assertEqual(len(terminal), 1, new)
            self.assertEqual(terminal[0]["reason"], reason)
            self.assertFalse(terminal[0].get("backend_ran"))
            self.assertNotEqual(grading_env.failure_kind(terminal[0]["why"]), "infra")
            self.observations.append(dict(reason=reason, terminal=terminal[0], launches=calls))
        # Dispatch holds an admitted subject descriptor until its frame exits.
        return new, calls


class EvidenceRefusals(DispatchCase):
    def build(self):
        return self.seed("build-done", actor="builder", project="fixture", ready_sha=self.main_sha)

    def bundle(self, *, sha=None, stale=False):
        sha = sha or self.main_sha
        directory = self.content / "evidence" / self.subject
        directory.mkdir(parents=True, exist_ok=True)
        tail = directory / "tail.txt"
        tail.write_text("inert command output\n")
        data = dict(spec=self.subject, sha=sha, base_sha=self.main_sha,
                    commands=[dict(tail=str(tail))], comparison={})
        path = directory / "bundle.json"
        raw = json.dumps(data).encode()
        path.write_bytes(raw)
        fields = dict(sha=sha, criteria=None, path=str(path), bundle_sha256=hashlib.sha256(raw).hexdigest())
        event = self.seed("evidence-bundle", actor="executor", **fields)
        if stale:
            event["ts"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            file = self.events / "L-executor-fixture.jsonl"
            file.write_text(json.dumps(event) + "\n")
        self.packet.write_text("bundle_sha256: `" + fields["bundle_sha256"] + "`\n")
        return fields["bundle_sha256"]

    def direct(self, reason=None):
        emitted = []
        events = fold.read_events(use_cache=False)
        with patch.object(grader_packet.subprocess, "Popen") as launcher:
            if reason:
                with self.assertRaises(Refused) as refused:
                    grader_packet.gate(events, self.subject, self.packet.read_text(),
                                       lambda kind, **kw: emitted.append(dict(type=kind, **kw)),
                                       lambda why, **kw: (_ for _ in ()).throw(Refused(why, **kw)))
                self.assertEqual(refused.exception.reason, reason)
                self.assertNotEqual(grading_env.failure_kind(refused.exception.why), "infra")
            else:
                self.assertIsNotNone(grader_packet.gate(events, self.subject, self.packet.read_text(),
                    lambda *a, **kw: self.fail("eligible bundle emitted refusal"),
                    lambda *a, **kw: self.fail("eligible bundle refused")))
        return emitted, [list(call.args[0]) for call in launcher.call_args_list]

    def pending(self):
        emitted, requested = self.direct("bundle-pending")
        self.assertEqual(emitted, [])
        _, launches = self.invoke("bundle-pending", role="grader")
        for calls in (requested, launches):
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1:], ["run-checks", self.subject, "--sha", self.main_sha])

    def test_missing_bundle_requests_evidence_and_persists_refusal(self):
        self.build()
        self.pending()

    def test_stale_bundle_is_pending(self):
        self.build()
        self.bundle(stale=True)
        self.pending()

    def test_different_ready_sha_is_pending(self):
        self.build()
        self.bundle(sha="b" * 40)
        self.pending()

    def test_packet_digest_must_match_eligible_bundle(self):
        self.build()
        self.bundle()
        self.packet.write_text("bundle_sha256: `" + "0" * 64 + "`\n")
        self.pending()

    def test_newer_noninfra_runner_failure_retains_terminal_reason(self):
        self.build()
        self.bundle()
        self.seed("evidence-bundle-failed", sha=self.main_sha, reason="fixture runner check failed")
        emitted, calls = self.direct("preflight:runner")
        self.assertEqual([(e["type"], e["capability"], e["reason"]) for e in emitted],
                         [("grading-preflight-failed", "runner", "fixture runner check failed")])
        self.assertEqual(calls, [])
        rows, calls = self.invoke("preflight:runner", role="grader")
        self.assertEqual(calls, [])
        self.assertEqual([(e["type"], e.get("capability"), e.get("reason")) for e in rows],
            [("grading-preflight-failed", "runner", "fixture runner check failed"),
             ("spawn-failed", None, "preflight:runner")])

    def test_eligible_bundle_reaches_unpaid_downstream_sentinel(self):
        self.build()
        self.bundle()
        self.assertEqual(self.direct(), ([], []))
        _, calls = self.invoke(role="grader", control=True)
        self.assertEqual(calls, [])


class StartRefusals(DispatchCase):
    def real_gate(self, role, subject, reason=None):
        events = fold.read_events(use_cache=False)
        if reason:
            with self.assertRaises(Refused) as stopped:
                lock = spawns.gate(self.root, role, subject, str(self.repo), events,
                    lambda why, **kw: (_ for _ in ()).throw(Refused(why, **kw)))
                if lock is not None:
                    lock.close()
            self.assertEqual(stopped.exception.reason, reason)
            self.assertNotEqual(grading_env.failure_kind(stopped.exception.why), "infra")
        else:
            lock = spawns.gate(self.root, role, subject, str(self.repo), events,
                              lambda *a, **kw: self.fail("admissible start refused"))
            if role in spawns.CONFLICTS:
                self.assertIsNotNone(lock)
                lock.close()
            else:
                self.assertIsNone(lock)

    def test_live_conflict_table_and_builder_worktree_collision(self):
        roles = ("builder", "grader", "reviewer", "charter-reviewer", "spec-writer",
                 "owed-sweeper", "research", "probe", "reuse-scout", "spec-auditor", "plan-auditor")
        expected = {"builder": {"builder", "grader", "reviewer"},
                    "grader": {"builder", "grader"}, "reviewer": {"builder", "reviewer"},
                    "charter-reviewer": {"charter-reviewer"}}
        self.assertEqual(spawns.CONFLICTS, expected)
        for other in ("builder", "grader", "reviewer", "charter-reviewer"):
            with self.subTest(other=other):
                event = self.live(other)
                for role in roles:
                    self.real_gate(role, self.subject,
                                   "duplicate-dispatch" if other in expected.get(role, set()) else None)
                if other in expected["builder"]:
                    self.invoke("duplicate-dispatch")
                self.seed("spawn-done", actor=other, spawn=event["spawn"])
                self.real_gate(other, self.subject)
        self.live(subject="L-spec-9902")
        self.real_gate("builder", self.subject, "duplicate-dispatch")
        self.invoke("duplicate-dispatch")
        self.real_gate("grader", self.subject)

    def test_occupied_kernel_flock_persists_dispatch_in_progress(self):
        lock, refusal = spawns.acquire(self.root, "builder", self.subject, str(self.repo), [])
        self.assertIsNone(refusal)
        self.assertIsNotNone(lock)
        try:
            self.real_gate("builder", self.subject, "dispatch-in-progress")
            self.invoke("dispatch-in-progress")
        finally:
            lock.close()
        self.real_gate("builder", self.subject)
        self.invoke(control=True)

    def test_regular_file_state_path_is_noninfra_lock_refusal(self):
        # All schemas, packets, model mapping and ledgers are already ready.
        (self.root / "state").write_text("inert obstruction\n")
        self.real_gate("builder", self.subject, "dispatch-in-progress")
        self.invoke("dispatch-in-progress")
        (self.root / "state").unlink()
        self.real_gate("builder", self.subject)
        self.invoke(control=True)

    def written(self, subject=None, footprint=None):
        return self.seed("spec-written", subject or self.subject, actor="operator",
                         project="fixture", charter="L-charter-9901", footprint=footprint or ["src/later.py"])

    def test_unsettled_earlier_wave_and_settled_control(self):
        (self.content / "plan-L-charter-9901.md").write_text(
            "## earlier\nFootprint:\n- src/earlier.py\nWave: 1\n"
            "## later\nFootprint:\n- src/later.py\nWave: 2\n")
        self.written()
        self.written("L-spec-9902", ["src/earlier.py"])
        self.assertEqual(dispatch.spec_wave("L-charter-9901", ["src/later.py"]), 2)
        self.assertEqual(dispatch.wave_blocker(fold.read_events(use_cache=False),
                                             "L-charter-9901", self.subject, 2), "L-spec-9902")
        self.invoke("wave-order")
        self.seed("spec-killed", "L-spec-9902", actor="operator", reason="inert settled fixture")
        self.invoke(control=True)

    def test_latest_shape_failure_and_newer_written_control(self):
        self.written()
        self.seed("spec-shape-failed", actor="spec-writer", findings=["inert shape fixture"])
        self.invoke("spec-shape")
        self.written()
        self.invoke(control=True)


class GateCase(ScratchCase):
    def invoke_gate(self, expected, *extra, branch="work", main="main", reason=None):
        before = len(self.rows())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = mg.main_([branch, main, *extra])
        self.assertEqual(result, expected)
        rows = [e for e in self.rows()[before:] if e["type"].startswith("merge-gate-")]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["type"], {0: "merge-gate-clean", 1: "merge-gate-rework",
                                      2: "merge-gate-undetermined"}[expected])
        if reason:
            self.assertEqual(row["reason"], reason)
        if expected != 0:
            self.assertFalse(any(e["type"] == "merge-gate-clean" for e in rows))
        if row.get("branch_sha"):
            self.assertEqual(row["branch_sha"], self.git("rev-parse", branch)[:len(row["branch_sha"])])
        if row.get("main_sha"):
            self.assertEqual(row["main_sha"], self.git("rev-parse", main)[:len(row["main_sha"])])
        if row.get("merged_tree"):
            tree = self.git("merge-tree", "--write-tree", main, branch).splitlines()[0]
            self.assertEqual(row["merged_tree"], tree[:len(row["merged_tree"])])
        self.observations.append(dict(exit_code=result, receipt=row))
        return row

    def ready(self, **kw):
        self.repository(**kw)
        self.put("added.py", "value = 1\n")
        self.branch_sha = self.commit()
        os.chdir(self.repo)


class GateStructural(GateCase):
    def test_cli_and_spec_grants_reject_prose_empty_and_off_switch(self):
        self.ready()
        for tokens in (["the", "web", "dashboard"], ["**"], ["*/**"], ["**.*"]):
            with self.subTest(tokens=tokens):
                self.invoke_gate(1, "--writes", *tokens)
        for text in ("Writes:\n", "Writes: the web dashboard only\n", "Writes: **\n", "no grant\n"):
            with self.subTest(spec=text):
                (self.content / "L-spec-9901.md").write_text(text)
                self.invoke_gate(1, "--spec", "L-spec-9901")
        self.invoke_gate(1, "--spec", "missing")
        self.invoke_gate(1, "--spec", "../invalid")
        with self.assertRaises(SystemExit):
            mg.main_(["work", "main", "--writes"])
        self.assertEqual(self.scanner_calls(), 0)
        (self.content / "L-spec-9901.md").write_text("Writes: added.py\n")
        self.invoke_gate(0, "--spec", "L-spec-9901", "--writes", "docs/")
        self.assertEqual(self.scanner_calls(), 1)

    def test_removal_rename_type_and_grant_boundaries(self):
        cases = [("docs/a.txt", "remove", [], 1),
                 ("docs/a.txt", "rename", ["moved.txt"], 1),
                 ("docs/a.txt", "type", [], 1),
                 ("migrations/001.sql", "remove", ["migrations/001.sql"], 1),
                 ("docs-extra/a.txt", "remove", ["docs/"], 1),
                 ("docs/sub/a.txt", "remove", ["docs/*"], 1),
                 ("docs/a.txt", "remove", ["docs/"], 0),
                 ("docs/a.txt", "remove", ["docs/*.txt"], 0),
                 ("docs/sub/a.txt", "remove", ["docs/**"], 0)]
        for index, (path, action, grant, status) in enumerate(cases):
            with self.subTest(path=path, action=action, grant=grant):
                self.repository({path: "inert contents\n"}, name=f"repo-{index}")
                if action == "rename":
                    self.git("mv", path, "moved.txt")
                else:
                    (self.repo / path).unlink()
                    if action == "type":
                        (self.repo / path).symlink_to("../base.txt")
                self.commit()
                os.chdir(self.repo)
                row = self.invoke_gate(status, *(["--writes", *grant] if grant else []))
                self.assertEqual(self.scanner_calls(), int(status == 0))
                if status:
                    self.assertTrue(any(value.startswith(path + "@") for value in row["removed"]))

    def test_revert_append_only_exemption_and_violation(self):
        for index, (after, listed, status) in enumerate((
                ("old\n", False, 1), ("old\n", True, 1),
                ("old\nnew\n", True, 0), ("old\n", False, 0))):
            with self.subTest(after=after, listed=listed, status=status):
                # Case 3 is a granted revert; case 2 is a revert-shaped append.
                older = "old\nnew\n" if index == 2 else "old\n"
                current = "old\n" if index == 2 else "old\ncurrent\n"
                files = {"data.txt": current}
                if listed:
                    files[mg.APPEND_LIST] = "data.txt\n"
                self.repository(files, older={"data.txt": older}, name=f"repo-{index}")
                self.put("data.txt", after)
                self.commit()
                os.chdir(self.repo)
                row = self.invoke_gate(status, *(["--writes", "data.txt"] if index == 3 else []))
                self.assertEqual(self.scanner_calls(), int(status == 0))
                if status:
                    self.assertEqual(row["reverted"], ["data.txt"])
                if listed:
                    self.assertEqual(row["append_only"][0]["ok"], status == 0)

    def test_unresolvable_ambiguous_identical_and_explicit_refs(self):
        self.ready()
        self.invoke_gate(2, branch="missing", reason="ref-resolution")
        self.invoke_gate(2, branch="main", reason="nothing-to-gate")
        self.git("tag", "work", "main")
        self.invoke_gate(2, reason="ref-resolution")
        self.assertEqual(self.scanner_calls(), 0)
        self.invoke_gate(0, branch="refs/heads/work")
        self.assertEqual(self.scanner_calls(), 1)

    def test_unmergeable_branches_retain_conflict_diagnosis(self):
        self.repository()
        self.put("base.txt", "branch change\n")
        self.commit()
        self.git("checkout", "-q", "main")
        self.put("base.txt", "main change\n")
        self.commit()
        os.chdir(self.repo)
        row = self.invoke_gate(1)
        self.assertIn("conflict", row["undetermined"])
        self.assertEqual(self.scanner_calls(), 0)

    def test_invalid_disabling_config_and_revert_path_cap(self):
        self.ready()
        for key, value in (("DOIT_GIT_TIMEOUT", "bad"), ("DOIT_GATE_DEADLINE", "0"),
                           ("DOIT_REVERT_DEPTH", "0"), ("DOIT_REVERT_MAX_PATHS", "0"),
                           ("DOIT_MIGRATIONS", "["), ("DOIT_MIGRATIONS", "never-matches")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                self.invoke_gate(2, reason="configuration")
        self.assertEqual(self.scanner_calls(), 0)
        self.put("base.txt", "updated\n")
        self.put("scripts/ci/detect-secrets-changed.sh", "# deliberately modified fixture\n")
        self.commit()
        with patch.dict(os.environ, DOIT_REVERT_MAX_PATHS="1"):
            self.invoke_gate(1)
            self.invoke_gate(0, "--writes", "base.txt")
        self.assertEqual(self.scanner_calls(), 1)

    def test_repository_preconditions_and_clean_control(self):
        self.ready()
        self.invoke_gate(0)
        initial_calls = self.scanner_calls()
        os.chdir(self.top)
        self.invoke_gate(2, reason="repository-precondition")
        os.chdir(self.repo)
        self.git("config", "core.worktree", str(self.repo))
        self.invoke_gate(2, reason="repository-precondition")
        self.git("config", "--unset", "core.worktree")
        self.git("replace", self.main_sha, self.branch_sha)
        self.invoke_gate(2, reason="repository-precondition")
        self.git("replace", "-d", self.main_sha)
        shallow = self.top / "shallow"
        self.git("clone", "-q", "--depth", "1", "--branch", "work", self.repo.as_uri(), str(shallow))
        os.chdir(shallow)
        self.invoke_gate(2, reason="repository-precondition")
        os.chdir(self.repo)
        self.assertEqual(self.scanner_calls(), initial_calls)
        self.invoke_gate(0)

    def test_submodule_cwd_holds(self):
        self.ready()
        outer = self.top / "outer"
        outer.mkdir()
        self.git("init", "-q", "-b", "main", repo=outer)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
                 str(self.repo), "child", repo=outer)
        os.chdir(outer / "child")
        self.invoke_gate(2, reason="repository-precondition")
        self.assertEqual(self.scanner_calls(), 0)

    def test_applicable_freeze_exits_without_receipt_then_absent_flag_allows(self):
        self.ready(name="albert-scott-retained")
        flag = self.top / "freeze-flag"
        flag.write_text("inert fixture freeze\n")
        before = self.rows()
        with patch.object(mg.freeze, "FLAG", flag):
            self.assertEqual(set(os.environ), set(_safe_env) | {
                "DOIT_GATE_LEDGER_FILE", "DOIT_LEDGER_FILE"})
            with self.assertRaisesRegex(SystemExit, "REFUSED: master is frozen"):
                mg.main_(["work", "main"])
            self.assertEqual(self.rows(), before)
            self.assertEqual(self.scanner_calls(), 0)
            flag.unlink()
            self.invoke_gate(0)
            self.assertEqual(self.scanner_calls(), 1)


class GateScanner(GateCase):
    def handin(self):
        self.seed("handin-checked", subject="work", actor="builder", sha=self.git("rev-parse", "work"),
                  checks=["secret-scan"], result="pass", failed_check=None, cpu_s=0, wall_s=0)

    def test_prior_matching_handin_pass_never_replaces_scan(self):
        for mode, status in (("clean", 0), ("finding", 1)):
            with self.subTest(mode=mode):
                self.ready(scanner=mode, name=mode)
                # Same exact branch/scanner, paired absent/present clean rows.
                for prior in (False, True):
                    if prior:
                        self.handin()
                    count = self.scanner_calls()
                    row = self.invoke_gate(status, reason="secret-scan" if status else None)
                    self.assertEqual(self.scanner_calls(), count + 1)
                    if status:
                        self.assertEqual(row["findings"], ["  added.py:1 -- Inert Fixture Finding"])
                if status:
                    self.put("added.py", "value = 2\n")
                    self.commit()
                    self.handin()
                    count = self.scanner_calls()
                    self.invoke_gate(1, reason="secret-scan")
                    self.assertEqual(self.scanner_calls(), count + 1)
                self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)
                self.assertEqual(list(self.scratch.glob("gate/scan-*")), [])

    def test_malformed_scanner_output_and_tool_failure_hold(self):
        for mode in ("malformed", "tool"):
            with self.subTest(mode=mode):
                self.ready(scanner=mode, name=mode)
                self.handin()
                self.invoke_gate(2, reason="tool")
                self.assertEqual(self.scanner_calls(), 1)
                self.assertEqual(list(self.scratch.glob("gate/scan-*")), [])

    def test_branch_scanner_cannot_override_trusted_main_finding(self):
        self.ready(scanner="finding")
        self.put("scripts/ci/detect-secrets-changed.sh", "#!/bin/bash\nexit 0\n")
        self.commit()
        self.handin()
        self.invoke_gate(1, reason="secret-scan")
        self.assertEqual(self.scanner_calls(), 1)

    def test_held_scanner_lock_holds_without_running_scanner(self):
        self.ready()
        self.handin()
        lockdir = self.repo / ".git" / "doit-secret-scan-locks"
        lockdir.mkdir()
        with (lockdir / (self.branch_sha + ".lock")).open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.invoke_gate(2, reason="lock-busy")
        self.assertEqual(self.scanner_calls(), 0)
        self.invoke_gate(0)
        self.assertEqual(self.scanner_calls(), 1)

    def test_worktree_setup_failure_is_tooling_hold(self):
        self.ready()
        self.handin()
        run = subprocess.run
        def refuse_worktree(args, **kw):
            if "worktree" in args and "add" in args:
                return subprocess.CompletedProcess(args, 128, b"", b"inert worktree setup failure")
            return run(args, **kw)
        with patch.object(mg.subprocess, "run", side_effect=refuse_worktree):
            self.invoke_gate(2, reason="worktree")
        self.assertEqual(self.scanner_calls(), 0)
        self.invoke_gate(0)

    def test_timeout_at_process_boundary_is_hold_and_reaps_scanner(self):
        self.ready()
        self.handin()
        popen = subprocess.Popen
        children = []
        class TimeoutProcess:
            def __init__(self, args, **kw):
                self.child = popen(args, **kw)
                children.append(self.child)
                self.pid = self.child.pid
            def communicate(self, timeout=None):
                if timeout is not None:
                    self.child.communicate(timeout=5)
                    raise subprocess.TimeoutExpired("inert fixture scanner", timeout)
                return self.child.communicate()
        def launch(args, **kw):
            if args[0] == "bash" and "detect-secrets-changed.sh" in args[1]:
                return TimeoutProcess(args, **kw)
            return popen(args, **kw)
        with patch.object(mg.subprocess, "Popen", side_effect=launch):
            self.invoke_gate(2, reason="timeout")
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.scanner_calls(), 2)
        self.assertEqual(list(self.scratch.glob("gate/scan-*")), [])
        self.invoke_gate(0)

    def test_deadline_at_clock_boundary_is_hold(self):
        self.ready()
        self.handin()
        popen, monotonic = subprocess.Popen, mg.time.monotonic
        elapsed = [0]
        class ClockProcess:
            def __init__(self, args, **kw):
                self.child = popen(args, **kw)
                self.pid = self.child.pid
            def communicate(self, timeout=None):
                result = self.child.communicate(timeout=timeout)
                elapsed[0] = 2
                return result
            @property
            def returncode(self):
                return self.child.returncode
        def launch(args, **kw):
            if args[0] == "bash" and "detect-secrets-changed.sh" in args[1]:
                return ClockProcess(args, **kw)
            return popen(args, **kw)
        with patch.dict(os.environ, DOIT_GATE_DEADLINE="1"), \
                patch.object(mg.time, "monotonic", side_effect=lambda: monotonic() + elapsed[0]), \
                patch.object(mg.subprocess, "Popen", side_effect=launch):
            self.invoke_gate(2, reason="deadline")
        self.assertEqual(self.scanner_calls(), 1)
        self.assertEqual(list(self.scratch.glob("gate/scan-*")), [])


if __name__ == "__main__":
    try:
        allowed = {"EvidenceRefusals", "StartRefusals", "GateStructural", "GateScanner"}
        if sys.argv[1:] and (len(sys.argv) != 2 or sys.argv[1] not in allowed):
            sys.exit("select EvidenceRefusals, StartRefusals, GateStructural or GateScanner")
        unittest.main()
    finally:
        _bootstrap.cleanup()
        _environment.stop()
