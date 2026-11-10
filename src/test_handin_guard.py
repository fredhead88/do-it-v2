#!/usr/bin/env python3
"""L-spec-2209: builder hand-in is independently checked. Scratch only; no backend, no credential.

Groups: ContractTests, WrapperTests, RoutingTests, RoundTripTests (python3 src/test_handin_guard.py <Group>)."""
import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
# Bind a disposable root before any product module is imported.
BOOT = tempfile.TemporaryDirectory(prefix="handin-guard-import-")
os.environ["DOIT_ROOT"] = str(Path(BOOT.name) / "default")
os.environ["DOIT_TEST_PROCESS"] = "1"
os.environ["DOIT_NO_POKE"] = "1"
os.environ.pop("DOIT_LEDGER_FILE", None)
os.environ.pop("DOIT_PROJECT", None)

import dispatch
import fold
import handin_check as hc
import lane_rules
import merge_gate as mg
import models
import nextstep

SID = "L-spec-9921"
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SECRET = "handin-check:secret-scan"


def card(status="DONE", ready="a" * 40, base="b" * 40):
    return dict(
        status=status,
        identity=dict(spec_id=SID, built_by="L-builder-0001", branch="l-spec-9921", base_sha=base, ready_sha=ready),
        acs=[dict(id="AC1", criterion_type="backend", evidence="out", evidence_type="command-output",
                  check="true", disposition="done")],
        verify=dict(command="true", exit_code=0, result="ok"), stubs=[], deviations=[],
        tests=dict(added=True), not_built=[], unknowns=[], built_against=[], escalations=[], declarations=[])


class Repos(unittest.TestCase):
    """Disposable committed repositories; scanner finding is a synthetic marker line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handin-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "ledger"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DOIT_GATE_", "SUPABASE_DB_URL"))}
        env.update(DOIT_ROOT=str(self.root), DOIT_SCRATCH=str(self.home / "scratch"), DOIT_NO_POKE="1",
                   DOIT_LEDGER_FILE="L-builder-fixture.jsonl", DOIT_PROJECT="do-it-v2")
        env.pop("DOIT_MAIN", None)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        patch.object(fold, "ROOT", self.root).start()
        patch.object(fold, "EVENTS", self.root / "events").start()
        patch.object(fold, "BOARD", self.root / "board.md").start()
        patch.object(fold, "PROJECT", "do-it-v2").start()
        (self.root / "events").mkdir(parents=True)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@localhost")
        script = self.repo / mg.DETECT_SECRETS_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/bash\nif grep -qx FIND fixture; then\n'
                          '  echo "  fixture:1 -- Synthetic finding"; exit 1\nfi\nexit 0\n')
        (self.repo / "fixture").write_text("CLEAN\n")
        self.commit("base")
        self.main = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "work")
        (self.repo / "fixture2").write_text("work\n")
        self.commit("spec commit")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL,
                                       env=dict(os.environ)).decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-qm", message)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def finding(self):
        (self.repo / "fixture").write_text("FIND\n")
        self.commit("synthetic finding")

    def reader(self):
        """A separate process reads the persisted ledger: no same-session object can stand in."""
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(SRC))
        env.pop("DOIT_PROJECT", None)
        p = subprocess.run([sys.executable, "-B", "-c",
                            "import fold,json; print(json.dumps(fold.read_events(use_cache=False),default=str))"],
                           env=env, capture_output=True, text=True, check=True)
        return json.loads(p.stdout)


def result(ok, failed, findings=(), cpu=0.0, wall=0.0):
    return hc.HandinResult(ok, failed, list(findings), cpu, wall)


class ContractTests(unittest.TestCase):
    text = (REPO / "agents/builder.md").read_text()

    def section(self):
        start = self.text.index("**Hand-in check**")
        return self.text[start:self.text.index("**Pre-existing failures**")]

    def test_ordering_and_exits(self):
        s = self.section()
        self.assertIn("doit handin-check <subject> --cwd <worktree>", s)
        self.assertRegex(s, r"After your final commit and before you return a DONE card")
        self.assertRegex(s, r"Exit 0 permits hand-in")
        self.assertRegex(s, r"Exit 1[^.]*finding")
        self.assertIn("amending or rewriting the single spec commit", s)
        self.assertIn("no fix-up commit", s)
        self.assertIn("re-run handin-check on the new HEAD", s)
        self.assertIn("report that new HEAD as `identity.ready_sha`", s)
        self.assertIn("return\nBLOCKED with the finding", s)
        self.assertRegex(s, r"Exit 2 is undetermined: you may return DONE")
        self.assertIn("must not describe it as a passed check", s)
        self.assertIn("merge gate remains authoritative", s)
        self.assertIn("wrapper checks\nindependently", s)
        self.assertIn("Only secret-scan runs early", s)
        self.assertIn("keeps all of its checks, unchanged", s)

    def test_protected_paragraph_untouched(self):
        t = self.text
        gate = t[t.index("**Pre-existing failures**"):t.index("## Deviations")]
        self.assertNotIn("handin-check", gate)
        for needle in ("doit base-red", "scratch-worktree fallback", "Anything failing only at HEAD is yours"):
            self.assertIn(needle, gate)
        self.assertEqual(t.count("**Hand-in check**"), 1)
        self.assertLess(t.index("**Hand-in check**"), t.index("**Pre-existing failures**"))

    def test_one_guard_on_the_common_boundary(self):
        src = (SRC / "dispatch.py").read_text()
        main = src[src.index("def _main(a):"):]
        self.assertEqual(main.count("handin_verdict("), 1)
        guard = main.index("handin_verdict(")
        self.assertLess(main.index("coupled_tests.validate_card(out)"), guard)
        self.assertLess(guard, main.index("events = spawns.or_fail(fail, events_for"))
        # every backend route shares that one boundary: no route calls the guard itself
        for route in ("run_codex(", "run_seat(", "run_claude("):
            self.assertIn(route, main[:guard])
        self.assertIn('if builder and out.get("status") == "DONE":', main[guard - 250:guard])
        pass
        self.assertNotIn("Agent(", src[src.index("def handin_verdict"):src.index("def events_for")])
        print("ContractTests: one guard, after validation, before persistence, for every backend route")


class WrapperTests(Repos):
    def verdict(self, run, ready=None, heads=None):
        out = card(ready=ready or self.head())
        with patch.object(hc, "run", side_effect=run if callable(run) and not isinstance(run, hc.HandinResult) else None,
                          return_value=run if isinstance(run, hc.HandinResult) else None) as spy:
            if heads is not None:
                with patch.object(dispatch, "_committed_head", side_effect=heads):
                    got = dispatch.handin_verdict(out, SID, self.repo, root=self.root)
            else:
                got = dispatch.handin_verdict(out, SID, self.repo, root=self.root)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.args, (self.repo, SID))
        self.assertEqual(spy.call_args.kwargs, {"root": self.root})
        return got

    def test_result_classification(self):
        cases = [
            ("clean", result(True, None), "clean"),
            ("finding", result(False, "secret-scan", ["fixture:1 -- x"]), "secret-scan"),
            ("undetermined", result(False, "undetermined"), "undetermined"),
            ("no discriminator", result(False, None), "undetermined"),
            ("ok with discriminator", result(True, "secret-scan"), "undetermined"),
            ("ok with undetermined", result(True, "undetermined"), "undetermined"),
            ("coupled-test-safety", result(False, "coupled-test-safety"), "undetermined"),
            ("lock-busy", result(False, "undetermined", ["lock-busy"]), "undetermined"),
            ("timeout", result(False, "undetermined", ["timeout"]), "undetermined"),
            ("malformed", SimpleNamespace(ok=1, failed_check=None), "undetermined"),
        ]
        for name, res, expected in cases:
            with self.subTest(name):
                got = self.verdict(lambda *a, _r=res, **k: _r)
                self.assertEqual(got, expected)

    def test_exceptions_and_identity(self):
        def boom(*a, **k):
            raise RuntimeError("tool exploded")

        def exits(*a, **k):
            raise SystemExit("gate refused")
        self.assertEqual(self.verdict(boom), "undetermined")
        self.assertEqual(self.verdict(exits), "undetermined")
        clean = result(True, None)
        finding = result(False, "secret-scan")
        other = "c" * 40
        # same pre/post HEAD, both differing from the card
        self.assertEqual(self.verdict(clean, ready=other), "undetermined")
        self.assertEqual(self.verdict(finding, ready=other), "undetermined")
        # missing HEAD before, after, and card sha
        self.assertEqual(self.verdict(clean, heads=[None, self.head()]), "undetermined")
        self.assertEqual(self.verdict(clean, heads=[self.head(), None]), "undetermined")
        self.assertEqual(self.verdict(clean, heads=[None, None]), "undetermined")
        out = card()
        out["identity"]["ready_sha"] = ""
        with patch.object(hc, "run", return_value=clean):
            self.assertEqual(dispatch.handin_verdict(out, SID, self.repo, root=self.root), "undetermined")
            del out["identity"]
            self.assertEqual(dispatch.handin_verdict(out, SID, self.repo, root=self.root), "undetermined")
        # HEAD changing during the call
        def moves(*a, **k):
            (self.repo / "late").write_text("x\n")
            self.commit("late commit")
            return clean
        self.assertEqual(self.verdict(moves), "undetermined")
        # unreadable worktree
        out = card(ready=self.head())
        with patch.object(hc, "run", return_value=clean):
            self.assertEqual(dispatch.handin_verdict(out, SID, self.home / "nowhere", root=self.root), "undetermined")
        # abbreviated card sha still names the same commit; a lookalike prefix does not
        self.assertEqual(self.verdict(clean, ready=self.head()[:12]), "clean")
        self.assertEqual(self.verdict(clean, ready="0" * 12), "undetermined")

    def events_of(self, handin, status="DONE"):
        content = self.root / "content"
        content.mkdir(exist_ok=True)
        out = card(status=status, ready=self.head())
        a = argparse.Namespace(subject=SID, path=None, cwd=str(self.repo), pinned_base_sha=self.main)
        if handin is not None:
            a._handin = handin
        base = dict(subject=SID, project="demo", spawn="L-builder-0001")
        with patch.object(dispatch, "CONTENT", content):
            if handin == "secret-scan":
                out = {**out, "status": "BLOCKED"}
            events = dispatch.events_for("builder", out, a, base)
            written = json.loads((content / "L-card-9921.json").read_text())
        return events, written

    def test_effective_card_events_and_terminal_status(self):
        events, written = self.events_of("clean")
        done = dict(events)["build-done"]
        self.assertEqual((written["status"], done["status"], done["handin_check"]), ("DONE", "DONE", "clean"))
        self.assertNotIn("handin_reason", done)
        self.assertNotIn("build-blocked", dict(events))
        events, written = self.events_of("undetermined")
        done = dict(events)["build-done"]
        self.assertEqual((written["status"], done["status"], done["handin_check"], done["handin_reason"]),
                         ("DONE", "DONE", "undetermined", "handin-check:undetermined"))
        self.assertNotIn("build-blocked", dict(events))
        events, written = self.events_of("secret-scan")
        kinds = [t for t, _ in events]
        self.assertEqual(written["status"], "BLOCKED")
        self.assertEqual(dict(events)["build-blocked"], {"reason": SECRET})
        self.assertEqual([kv["status"] for t, kv in events if t == "build-done"], ["BLOCKED"])
        self.assertNotIn("handin_check", dict(events)["build-done"])
        # branch, pinned base, ready sha, verification evidence are preserved
        done = dict(events)["build-done"]
        self.assertEqual((done["branch"], done["base_sha"], done["ready_sha"], done["verify_exit"]),
                         ("l-spec-9921", self.main, self.head(), 0))
        self.assertEqual(written["identity"]["ready_sha"], self.head())
        # an unguarded caller (no completion guard ran) adds nothing
        events, _ = self.events_of(None)
        self.assertNotIn("handin_check", dict(events)["build-done"])
        self.assertIn("build-done", kinds)

    def test_generic_escalation_cannot_replace_exact_reason(self):
        content = self.root / "content"
        content.mkdir(exist_ok=True)
        out = card(status="BLOCKED", ready=self.head())
        out["escalations"] = [dict(asks="please look at the scanner", blocks=["AC1"], default="none-irreversible",
                                   deadline="2026-11-10T00:00:00Z")]
        out["declarations"] = [dict(term="worked", line="generic line")]
        a = argparse.Namespace(subject=SID, path=None, cwd=str(self.repo), pinned_base_sha=self.main, _handin="secret-scan")
        base = dict(subject=SID, project="demo", spawn="L-builder-0001")
        with patch.object(dispatch, "CONTENT", content):
            events = dispatch.events_for("builder", out, a, base)
        self.assertEqual(dict(events)["build-blocked"]["reason"], SECRET)
        self.assertIn("question", dict(events))  # escalation reporting preserved
        # a builder-authored BLOCKED with no handin finding keeps its own reason
        a._handin = None
        with patch.object(dispatch, "CONTENT", content):
            events = dispatch.events_for("builder", out, a, base)
        self.assertEqual(dict(events)["build-blocked"]["reason"], "please look at the scanner")


def build_main_run(case, status="DONE", *, backend="codex", role="builder", fallback=False, ready=None):
    """Run the production dispatch.main with a deterministic backend response. Returns SystemExit code or None."""
    root, repo = case.root, case.repo
    packet = case.home / "packet.md"
    packet.write_text("fixture packet\nPINNED_BASE_SHA: " + case.main + "\nPINNED_BASE_SHA_EXPLICIT: true\n")
    out = card(status=status, ready=ready or case.head(), base=case.main)
    if status == "NEEDS_CONTEXT":
        out["reason"] = "needs context"
    response = subprocess.CompletedProcess([], 0, json.dumps(dict(structured_output=out, is_error=False)), "")
    mapping = dict(backend=backend, model="fixture", source="fixture")
    if fallback:
        mapping.update(backend="codex", fallback=dict(backend="seat", model="fixture"))
    a = argparse.Namespace(role=role, subject=SID, packet=str(packet), path=None, cwd=str(repo), charter=None,
                           project="do-it-v2", mcp_config=None, timeout=None, max_usd=None, seat=False)
    refused = subprocess.CompletedProcess([], 1, json.dumps(dict(is_error=True, result="at capacity")), "")
    backends = dict(codex=refused if fallback else response, seat=response, claude=response)
    patches = [patch.multiple(dispatch, ROOT=root, EVENTS=root / "events", CONTENT=root / "content"),
               patch.multiple(fold, ROOT=root, EVENTS=root / "events"),
               patch.object(models, "resolve", return_value=mapping),
               patch.object(dispatch, "ensure_room", return_value=True),
               patch.object(dispatch, "builder_worktree_preflight"),
               patch.object(dispatch, "builder_effective_base"),
               patch.object(dispatch, "builder_branch_preflight"),
               patch.object(dispatch.spawns, "gate", return_value=None),
               patch.object(dispatch, "codex_quota", return_value=None),
               patch.object(dispatch, "relay_serving", return_value=True),
               patch.object(dispatch, "run_codex", return_value=backends["codex"]),
               patch.object(dispatch, "run_seat", return_value=backends["seat"]),
               patch.object(dispatch, "run_claude", return_value=backends["claude"])]
    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            dispatch.main(a)
            code = None
        except SystemExit as exc:
            code = exc.code
            sys.__stderr__.write(err.getvalue()) if code else None
    return code, dict(codex=mocks[10], seat=mocks[11], claude=mocks[12])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handin-routing-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "events").mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, DOIT_ROOT=str(self.root)).start()
        patch.object(fold, "ROOT", self.root).start()
        patch.object(fold, "EVENTS", self.root / "events").start()
        patch.object(dispatch, "CONTENT", self.root / "content").start()
        patch.object(fold, "NOW", NOW).start()
        self.tick = 0
        self.emit("spec-written", "spec-writer", -10000, path=str(self.root / "content" / (SID + ".md")))
        self.emit("spawn-done", "spec-auditor", -9000, bad_cut=False)

    def emit(self, kind, actor="builder", seconds=0, subject=SID, file=None, **fields):
        name = file or f"L-{actor}-local.jsonl"
        with patch.object(dispatch, "now", return_value=(NOW + timedelta(seconds=seconds)).isoformat()):
            error = dispatch.emit(self.root / "events" / name, dict(subject=subject, project="demo"), kind, **fields)
        self.assertIsNone(error)

    def block(self, seconds, spawn, *, reason=SECRET, receipt_first=False, sha="a" * 40, subject=SID, start=True):
        if start:
            self.emit("build-started", "builder", seconds - 600, subject=subject, spawn=spawn, role="builder")
        order = [("build-done", dict(status="BLOCKED", ready_sha=sha, branch="b", base_sha="c" * 40)),
                 ("build-blocked", dict(reason=reason))]
        for kind, fields in (order[::-1] if receipt_first else order):
            self.emit(kind, "builder", seconds, subject=subject, spawn=spawn, **fields)
        self.emit("spawn-done", "builder", seconds, subject=subject, spawn=spawn, role="builder", status="BLOCKED")

    def step(self, seconds=60):
        events = fold.read_events(use_cache=False)
        return nextstep.step_for(events, fold.fold(events)[0], SID, NOW + timedelta(seconds=seconds),
                                 lane_rules.load(), mains={"demo": "d" * 40})

    def assertRebuild(self, step, why=""):
        self.assertEqual((step.action, step.owner), ("rebuild", "tick"), (why, step))

    def assertNoAuto(self, step, why=""):
        self.assertNotEqual(step.action, "rebuild", (why, step))

    def test_first_block_self_triggers_with_equal_timestamps_either_order(self):
        for first in (False, True):
            with self.subTest(receipt_first=first):
                for f in (self.root / "events").glob("L-builder-*.jsonl"):
                    f.unlink()
                self.block(-100, "L-builder-0001", receipt_first=first)
                step = self.step(0)
                self.assertRebuild(step, "own block and same-time receipt do not consume it")
                self.assertTrue(step.trigger, "the block's actual source is returned")
                blocked = [e for e in fold.read_events(use_cache=False) if e["type"] == "build-blocked"][0]
                self.assertEqual(step.trigger, blocked["_src"])
                self.assertFalse(step.reason.startswith("retries-exhausted"))
                print("self-trigger receipt_first=%s -> %s" % (first, step.action))

    def test_repeated_snapshot_then_start_suppresses_duplicate(self):
        self.block(-100, "L-builder-0001")
        self.assertRebuild(self.step(0))
        self.assertRebuild(self.step(1))                      # a repeated snapshot is still one pending start
        self.emit("build-started", "builder", 5, spawn="L-builder-0002", role="builder")
        self.assertNotEqual(self.step(10).action, "rebuild")  # a live attempt suppresses a duplicate start
        self.emit("spawn-failed", "builder", 20, spawn="L-builder-0002", role="builder", reason="vanished")
        self.assertRebuild(self.step(25 * 60))                # the bounded launch retry still applies

    def test_second_distinct_block_needs_hand_path(self):
        self.block(-1000, "L-builder-0001")
        self.assertRebuild(self.step(-900))
        self.block(-100, "L-builder-0002", sha="e" * 40)      # new SHA, new attempt
        self.assertNoAuto(self.step(0), "second distinct block")
        self.block(-50, "L-builder-0003", sha="f" * 40)
        self.assertNoAuto(self.step(0), "third block")

    def test_duplicate_receipts_are_one_block(self):
        self.block(-100, "L-builder-0001")
        self.emit("build-blocked", "builder", -100, spawn="L-builder-0001", reason=SECRET)
        self.emit("build-blocked", "builder", -99, file="L-builder-other.jsonl", spawn="L-builder-0001", reason=SECRET)
        self.assertRebuild(self.step(0))

    def test_legacy_rows_without_spawn(self):
        for kind, fields in (("build-done", dict(status="BLOCKED", ready_sha="a" * 40)), ("build-blocked", dict(reason=SECRET)),
                             ("build-blocked", dict(reason=SECRET))):
            self.emit(kind, "builder", -100, **fields)
        self.assertRebuild(self.step(0))
        self.emit("build-blocked", "builder", -10, reason=SECRET)
        self.assertNoAuto(self.step(0), "second legacy attempt")

    def test_interval_reset_and_non_reset(self):
        self.block(-1000, "L-builder-0001")
        self.block(-500, "L-builder-0002")
        self.assertNoAuto(self.step(0))
        # ordinary thinker response does not renew
        self.emit("decision", "thinker", -400, ref="elsewhere", note="ordinary answer")
        self.assertNoAuto(self.step(0), "thinker response")
        # another subject's events never reset
        self.emit("spec-written", "spec-writer", -300, subject="L-spec-9999")
        self.emit("decision", "operator", -290, subject="L-spec-9999")
        self.assertNoAuto(self.step(0), "cross-subject")
        # a new SHA/build-started alone does not reset
        self.emit("build-started", "builder", -200, spawn="L-builder-0003", role="builder")
        self.emit("spawn-failed", "builder", -190, spawn="L-builder-0003", role="builder", reason="vanished")
        self.assertNoAuto(self.step(0), "new build-started")
        # a later spec-written starts a new interval; the next block self-triggers once again
        self.emit("spec-written", "spec-writer", -150, path="x")
        self.block(-100, "L-builder-0004")
        self.assertRebuild(self.step(0), "new interval after spec-written")
        self.block(-50, "L-builder-0005")
        self.assertNoAuto(self.step(0), "second block in the new interval")
        self.emit("decision", "operator", -40, note="authorize")
        self.block(-20, "L-builder-0006")
        self.assertRebuild(self.step(0), "operator decision starts a new interval")

    def test_unrelated_lookalike_cross_subject_and_superseded(self):
        for reason in ("handin-check:undetermined", "handin-check:secret-scan-extra", "xhandin-check:secret-scan",
                       "handin-check:", "Handin-check:secret-scan", "something else"):
            with self.subTest(reason=reason):
                for f in (self.root / "events").glob("L-builder-*.jsonl"):
                    f.unlink()
                self.block(-100, "L-builder-0001", reason=reason)
                self.assertNoAuto(self.step(0))
        for f in (self.root / "events").glob("L-builder-*.jsonl"):
            f.unlink()
        # a block on another subject does not trigger this one
        self.block(-100, "L-builder-0001", subject="L-spec-9999")
        self.assertNoAuto(self.step(0), "cross-subject")
        # superseded: a later DONE outcome leaves no current block
        self.block(-100, "L-builder-0002")
        self.emit("build-started", "builder", -50, spawn="L-builder-0003", role="builder")
        self.emit("build-done", "builder", -40, spawn="L-builder-0003", status="DONE", ready_sha="a" * 40)
        self.assertNoAuto(self.step(0), "superseded")

    def test_precedence_guards_remain(self):
        self.block(-100, "L-builder-0001")
        self.emit("shipped", "executor", -50)
        events = fold.read_events(use_cache=False)
        self.assertEqual([s for s in nextstep.next_steps(events, fold.fold(events)[0], NOW, lane_rules.load())
                          if s.item == SID and s.action == "rebuild"], [])
        for f in (self.root / "events").glob("L-*.jsonl"):
            f.unlink()
        self.emit("spec-written", "spec-writer", -10000, path="x")
        self.emit("spawn-done", "spec-auditor", -9000, bad_cut=False)
        self.block(-100, "L-builder-0001")
        self.emit("decision", "thinker", -50, regrade="yes")
        self.assertNoAuto(self.step(0), "regrade")
        for f in (self.root / "events").glob("L-thinker-*.jsonl"):
            f.unlink()
        # exhausted launch retries hold, they do not start
        for n in (2, 3, 4):
            self.emit("build-started", "builder", -90 + n, spawn=f"L-builder-000{n}", role="builder")
            self.emit("spawn-failed", "builder", -80 + n, spawn=f"L-builder-000{n}", role="builder", reason="vanished")
        step = self.step(3600)
        self.assertNotEqual((step.action, step.reason.startswith("retries-exhausted")), ("rebuild", False))

    def test_existing_hand_path_unchanged(self):
        self.block(-100, "L-builder-0001", reason="needs a human decision")
        self.assertNoAuto(self.step(0))
        self.emit("decision", "operator", -50, rework="yes", ref="x")
        self.assertRebuild(self.step(0), "authorized manual rework remains available")


class RoundTripTests(Repos):
    def run_case(self, label, patcher=None, **kw):
        """Production wrapper path -> committed ledger -> separate reader process -> fresh card read."""
        self.root.mkdir(exist_ok=True)
        ctx = patcher() if patcher else contextlib.nullcontext()
        with ctx:
            code, mocks = build_main_run(self, ready=self.head(), **kw)
        self.assertIsNone(code, label)
        rows = self.reader()                                    # the writing handle is closed
        card_path = self.root / "content" / "L-card-9921.json"
        written = json.loads(card_path.read_text())             # fresh disk read
        return rows, written, mocks

    def checked(self, rows):
        events = [r for r in rows if r["type"] == "handin-checked"]
        return events

    def step(self, rows=None):
        """Seed the written spec well before the build (so the run saw a legacy hand-in), then evaluate."""
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        (self.root / "content").mkdir(exist_ok=True)
        (self.root / "content" / (SID + ".md")).write_text("# %s\n" % SID)
        with patch.object(dispatch, "now", return_value=past):
            for kind, actor, kv in (("spec-written", "spec-writer", dict(path=str(self.root / "content" / (SID + ".md")))),
                                    ("spawn-done", "spec-auditor", dict(bad_cut=False))):
                dispatch.emit(self.root / "events" / f"L-{actor}-local.jsonl", dict(subject=SID, project="do-it-v2"), kind, **kv)
        rows = self.reader()
        specs = fold.fold(rows)[0]
        now = datetime.now(timezone.utc) + timedelta(minutes=1)
        with patch.object(fold, "NOW", now):
            return nextstep.step_for(rows, specs, SID, now, lane_rules.load(), mains={"do-it-v2": self.main})

    def test_clean(self):
        rows, written, mocks = self.run_case("clean")
        done = [r for r in rows if r["type"] == "build-done"][0]
        self.assertEqual((done["status"], done["handin_check"]), ("DONE", "clean"))
        self.assertEqual(written["status"], "DONE")
        self.assertFalse([r for r in rows if r["type"] == "build-blocked"])
        self.assertEqual(self.terminal(rows), "DONE")
        checked = self.checked(rows)
        self.assertEqual(len(checked), 1)                        # one independent wrapper run
        self.timing(checked[0], "clean")
        self.assertNotEqual(self.step().action, "rebuild")
        mocks["codex"].assert_called_once()
        print("RoundTrip clean: DONE handin_check=clean, 1 handin-checked", checked[0]["sha"][:7])

    def test_finding(self):
        self.finding()
        rows, written, _ = self.run_case("finding")
        dones = [r for r in rows if r["type"] == "build-done"]
        self.assertEqual([d["status"] for d in dones], ["BLOCKED"])
        self.assertEqual(written["status"], "BLOCKED")
        blocked = [r for r in rows if r["type"] == "build-blocked"]
        self.assertEqual([b["reason"] for b in blocked], [SECRET])
        self.assertEqual(self.terminal(rows), "BLOCKED")
        checked = self.checked(rows)
        self.assertEqual(len(checked), 1)
        self.timing(checked[0], "rework", failed="secret-scan")
        step = self.step()
        self.assertEqual((step.action, step.owner), ("rebuild", "tick"))
        self.assertEqual(step.trigger, blocked[0]["_src"])
        print("RoundTrip finding: BLOCKED handin-check:secret-scan, first tick action", step.action)

    def test_agent_side_clean_then_wrapper_finding(self):
        """An agent-side clean run at an earlier HEAD does not stand in for the wrapper's own run."""
        clean_head = self.head()
        self.assertEqual(hc.run(self.repo, SID, root=self.root).failed_check, None)
        self.finding()
        rows, written, _ = self.run_case("amended")
        self.assertEqual(written["status"], "BLOCKED")
        self.assertEqual(len(self.checked(rows)), 2)             # the agent-side run plus the wrapper's independent run
        self.assertEqual(self.checked(rows)[0]["sha"], clean_head)
        self.assertEqual(self.checked(rows)[1]["failed_check"], "secret-scan")
        self.assertEqual([r["reason"] for r in rows if r["type"] == "build-blocked"], [SECRET])

    def test_tooling_failure_and_timeout(self):
        tooling = lambda: patch.object(mg, "secret_scan", return_value=("undetermined", ["tool"]))
        slow = lambda: patch.object(hc, "ATTEMPT_SECONDS", 0)
        for label, patcher in (("tooling", tooling), ("timeout", slow)):
            with self.subTest(label):
                for f in (self.root / "events").glob("L-builder-*.jsonl"):
                    f.unlink()
                rows, written, _ = self.run_case(label, patcher)
                done = [r for r in rows if r["type"] == "build-done"][0]
                self.assertEqual((done["status"], done["handin_check"], done["handin_reason"]),
                                 ("DONE", "undetermined", "handin-check:undetermined"))
                self.assertEqual(written["status"], "DONE")
                self.assertFalse([r for r in rows if r["type"] == "build-blocked"])
                checked = self.checked(rows)
                self.assertEqual(len(checked), 1)
                self.timing(checked[0], "undetermined", failed="undetermined", sha=label == "tooling")
                self.assertNotEqual(self.step().action, "rebuild")
                print("RoundTrip %s: DONE handin_check=undetermined, no block, no secret rebuild" % label)

    def test_inherited_bounded_scanner_only(self):
        self.assertEqual(hc.ATTEMPT_SECONDS, 300)
        with patch.object(mg, "secret_scan", wraps=mg.secret_scan) as scan, \
             patch.object(dispatch.subprocess, "Popen", wraps=dispatch.subprocess.Popen):
            rows, _, mocks = self.run_case("bounded")
        self.assertEqual(scan.call_count, 1)
        self.assertIs(scan.call_args.kwargs["wrap"], hc.cpu_share.niced)
        self.assertTrue(0 < scan.call_args.kwargs["wall_cap"] <= 300)
        self.assertEqual(len([r for r in rows if r["type"] == "spawn-started"]), 0)
        self.assertEqual(mocks["codex"].call_count + mocks["seat"].call_count + mocks["claude"].call_count, 1)

    def test_fallback_backend_reaches_same_guard(self):
        self.finding()
        rows, written, mocks = self.run_case("fallback", fallback=True)
        self.assertEqual(written["status"], "BLOCKED")
        self.assertEqual(len(self.checked(rows)), 1)
        self.assertEqual(mocks["seat"].call_count, 1)

    def test_non_eligible_outcomes_add_no_check(self):
        self.finding()
        for status in ("BLOCKED", "NEEDS_CONTEXT"):
            with self.subTest(status):
                for f in (self.root / "events").glob("L-builder-*.jsonl"):
                    f.unlink()
                rows, written, _ = self.run_case(status, status=status)
                self.assertEqual(self.checked(rows), [])
                self.assertEqual(written["status"], status)
                self.assertFalse([r for r in rows if r["type"] == "build-done" and "handin_check" in r])
        rows_before = self.reader()
        with patch.object(dispatch, "handin_verdict", side_effect=AssertionError("non-builder")):
            events = dispatch.events_for("research", dict(path="x", answered=True), argparse.Namespace(subject=SID),
                                         dict(subject=SID, spawn="L-research-0001"))
        self.assertEqual([t for t, _ in events], ["research-filed"])
        self.assertEqual(len(rows_before), len(self.reader()))

    def terminal(self, rows):
        return [r for r in rows if r["type"] == "spawn-done" and r.get("role") == "builder"][-1]["status"]

    def timing(self, event, result, failed=None, sha=True):
        self.assertEqual(event["result"], result)
        self.assertEqual(event["failed_check"], failed)
        if sha:
            self.assertEqual(event["sha"], self.head())
        self.assertIn("secret-scan", event["checks"])
        for key in ("cpu_s", "wall_s"):
            self.assertIsInstance(event[key], (int, float))
            self.assertTrue(math.isfinite(event[key]) and event[key] >= 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
