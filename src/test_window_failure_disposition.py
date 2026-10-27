#!/usr/bin/env python3
"""Offline production-control-flow regressions for L-spec-1712.

No legacy test imports. CI, process and clock boundaries are injected; git,
cache replacement and event admission/append/read-back are real and local.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix="failure-disposition-")
os.environ.update(DOIT_ROOT=str(Path(_ISO.name) / "root"), DOIT_NO_POKE="1",
                  DOIT_SCRATCH=str(Path(_ISO.name) / "scratch"))
import base_red as br
import fold
import merge_gate as mg
import window_gate as wg

AUDIENCE = "tests/test_audience.py::test_date"
UNKNOWN = "tests/test_missing.py::test_unknown"
OWNED = "tests/test_A.py::test_new"
NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


class Disposition(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_ISO.name)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.repo = self.root / "repos" / "demo"
        self.repo.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "DOIT_ROOT": str(self.root), "DOIT_LEDGER_FILE": "L-executor-0001.jsonl",
            "DOIT_GATE_LEDGER_FILE": "L-executor-0001.jsonl", "DOIT_WINDOW_RECHAIN": "off"}))
        self.stack.enter_context(patch.object(fold, "ROOT", self.root))
        self.stack.enter_context(patch.object(fold, "EVENTS", self.root / "events"))
        self.stack.enter_context(patch.object(fold, "NOW", NOW))
        self.git("init", "-q", "-b", "fixture")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/example/demo.git")

    def git(self, *args, inp=None, env=None):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=fixture",
             "-c", "user.email=fixture@localhost", *args], input=inp,
            text=True, env=env).strip()

    def rows(self, actor="executor-0001"):
        path = self.root / "events" / ("L-" + actor + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self, ids=(), runs=("1", "2")):
        answer = br._result(self.sha)
        answer.update(complete=True, workflows=sorted(br.WORKFLOWS),
                      kinds_covered=list(br.KINDS), runs_seen=list(runs))
        answer["ids"]["pytest"] = list(ids)
        return answer

    def seed(self):
        directory = self.root / "base-red" / "demo"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (self.sha + ".json")
        br._store(path, "demo", self.snapshot())
        return path

    def nightly(self, sha=None, project="demo", complete=True, ids=(AUDIENCE,)):
        with patch.dict(os.environ, DOIT_LEDGER_FILE="L-base-red-local.jsonl"):
            fold.append(["base-red-nightly", "base-" + project, "project=" + project,
                         "sha=" + (sha or self.sha), "complete:=" + json.dumps(complete),
                         "ids:=" + json.dumps(list(ids))])

    def response(self, ac, body):
        raw = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(criterion=ac, url="cli://L-spec-1712/" + ac, status=0,
                              body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))

    def test_AC1(self):
        path = self.seed()
        runs = [dict(id=i, head_sha=self.sha, status="completed", name="Python Tests",
                     conclusion="failure", html_url="scratch://run/" + str(i))
                for i in (1, 2, 3, 3)]
        snapshots = []
        def api(path, key):
            if key == "workflow_runs":
                return runs
            rid = path.split("/")[-2]
            return ([{"name": wg.DASHBOARD_CHECK, "conclusion": "success"}] if rid == "2" else
                    [{"name": "pytest", "conclusion": "success"},
                     {"name": "pytest — shard fixture", "conclusion": "success"},
                     {"name": wg.PG_CHECK, "conclusion": "success"}])
        def logs(owner, rid):
            snapshots.append(rid)
            text = "CI-SELECTION mode=full reason=fixture files=1\n"
            if rid == "2":
                text += "SUMMARY vitest_baseline_gate.mjs check: PASS\n"
            else:
                text += (("NEW failed: " + AUDIENCE + "\n") if rid == "3" else "")
                text += "SUMMARY pytest_baseline_gate.py check: PASS\n"
            return [("fixture.txt", text)]
        runs[1]["name"] = "Dashboard PR Quality"
        with patch.object(br, "_api", side_effect=api) as api_mock, \
             patch.object(br, "_logs", side_effect=logs):
            self.assertTrue(br.failing("demo", self.sha)["complete"])
            api_mock.assert_not_called()  # ordinary all-kind cache read stays offline
            result = br.classify("demo", self.sha, [AUDIENCE], refresh=True)
        self.assertEqual(result["pre_existing"], [AUDIENCE])
        self.assertEqual(snapshots, ["1", "2", "3"])
        self.assertEqual(sum(c.args[1] == "workflow_runs" for c in api_mock.call_args_list), 1)
        stored = json.loads(path.read_text())
        self.assertEqual(stored["runs_seen"], ["1", "2", "3"])
        self.assertTrue(stored["complete"])
        self.assertEqual(stored["ids"]["pytest"], [AUDIENCE])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         [self.sha + ".json", self.sha + ".lock"])
        # A complete same-base nightly contributes positive proof with no API.
        br._store(path, "demo", self.snapshot())
        self.nightly()
        before = path.read_bytes()
        with patch.object(br, "_api", side_effect=ValueError("offline")):
            positive = br.classify("demo", self.sha, [AUDIENCE], refresh=True, now=NOW)
            novel = br.classify("demo", self.sha, [UNKNOWN], refresh=True, now=NOW)
        self.assertEqual(positive["pre_existing"], [AUDIENCE])
        self.assertTrue(positive["complete"])
        self.assertFalse(novel["complete"])
        self.assertTrue(novel["undetermined"])
        self.assertEqual(path.read_bytes(), before)
        with patch.object(br, "_api", return_value=runs), \
             patch.object(br, "_snapshot", return_value=(br._result(self.sha), True)):
            self.assertFalse(br.classify("demo", self.sha, [UNKNOWN], refresh=True)["complete"])
        self.assertEqual(path.read_bytes(), before)
        # Wrong SHA, wrong project, incomplete and future nightlies prove nothing.
        other = self.git("commit-tree", self.git("rev-parse", "HEAD^{tree}"), "-p", self.sha,
                         inp="other synthetic commit\n")
        self.nightly(sha=other, ids=(UNKNOWN,))
        self.nightly(project="elsewhere", ids=(UNKNOWN,))
        self.nightly(complete=False, ids=(UNKNOWN,))
        with patch.object(fold, "NOW", NOW + timedelta(days=1)):
            self.nightly(ids=(UNKNOWN,))
        with patch.object(br, "_api", side_effect=ValueError("offline")):
            result = br.classify("demo", self.sha, [UNKNOWN], refresh=True, now=NOW)
        self.assertEqual(result["pre_existing"], [])
        self.assertFalse(result["complete"])
        with patch.object(br, "_api", side_effect=ValueError("offline")):
            opted_in = br.classify("demo", self.sha, [UNKNOWN], refresh=True, known_red=True, now=NOW)
        self.assertEqual(opted_in["pre_existing"], [])
        self.assertFalse(opted_in["complete"])
        self.assertEqual(self.rows("base-red-local")[0]["type"], "base-red")
        self.response("AC1", {"runs_seen": stored["runs_seen"],
            "snapshot_reads": snapshots, "pre_existing": [AUDIENCE],
            "failed_refresh_preserved_cache": path.read_bytes() == before,
            "wrong_base_project_incomplete_future_rejected": result["pre_existing"] == []})
        print("L-spec-1712 AC1 ok")

    def entries(self, names):
        result = []
        for name in names:
            blob = self.git("hash-object", "-w", "--stdin", inp=name + "\n")
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(self.temp.name) / "index"))
            self.git("read-tree", self.sha, env=env)
            self.git("update-index", "--add", "--cacheinfo", "100644", blob,
                     "tests/test_" + name + ".py", env=env)
            tree = self.git("write-tree", env=env)
            sha = self.git("commit-tree", tree, "-p", self.sha, inp=name + "\n")
            self.git("update-ref", "refs/heads/br-" + name, sha)
            result.append((name, "br-" + name, sha))
        return result

    def attempt(self, names, judgments, *, base_ids=(), complete=True, pg=False, fixes=False,
                base_pg="green", migrations=(), exhaust=False, uncovered=False):
        entries = self.entries(names)
        calls, advanced, verified = [], [], {}
        def verify(repo, cand, required, window):
            kept = tuple(s for s, _, _ in entries if self.git(
                "ls-tree", "-r", "--name-only", cand, "--", "tests/test_" + s + ".py"))
            calls.append(kept)
            state, nodes = judgments(kept, len(calls))
            verified[cand] = (state, list(nodes))
            return dict(state=state, nodes=list(nodes), url="scratch://candidate", doc={},
                        output="", pg_failed=pg and state == "red", why="fixture")
        def advance(repo, main, old, new, window, kept, *args, **kwargs):
            self.assertIn(tuple(kept), calls)
            state, nodes = verified[new]
            self.assertTrue(state == "green" or (state == "red" and complete
                and set(nodes) <= set(base_ids) and not fixes))
            advanced.append((new, list(kept)))
        def failing(project, sha, **kwargs):
            self.assertTrue(kwargs.get("refresh"), "window classifications require refresh")
            answer = self.snapshot(base_ids) if complete else br._result(self.sha)
            if uncovered:
                answer["kinds_covered"] = ["pg-parity"]
            return answer
        patches = [patch.object(wg, "train_events", return_value=[]),
                   patch.object(wg, "required_checks", return_value=[wg.PYTEST_CHECK] + ([wg.PG_CHECK] if pg else [])),
                   patch.object(wg, "base_red_snapshot", return_value=self.snapshot()),
                   patch.object(wg, "scope", return_value={"checks": [wg.PYTEST_CHECK] + ([wg.PG_CHECK] if pg else []), "reason": "fixture"}),
                   patch.object(wg, "verify", side_effect=verify),
                   patch.object(wg, "advance", side_effect=advance),
                   patch.object(wg, "run_minutes", return_value={}),
                   patch.object(br, "failing", side_effect=failing),
                   patch.object(wg, "base_pg", return_value=({"state": base_pg}, []))]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            if exhaust:
                real_bisect = wg.bisect_window
                def exhaust_then_bisect(window, live, prepare, judge, drop, conflict):
                    # Stress the callback's shared bound, regardless of how many
                    # requests a bisection strategy attempts to make.
                    cand, kept, _ = prepare(live)
                    for _ in range(2 * len(names) + 2):
                        judge(cand, kept)
                    return real_bisect(window, live, prepare, judge, drop, conflict)
                stack.enter_context(patch.object(wg, "bisect_window", side_effect=exhaust_then_bisect))
            if migrations:
                stack.enter_context(patch.dict(os.environ, DOIT_WINDOW_RECHAIN="on"))
                stack.enter_context(patch.object(wg.rechain, "added_versions", side_effect=lambda r, b, s: [s] if s in [e[2] for e in entries if e[0] in migrations] else []))
                stack.enter_context(patch.object(wg.rechain, "fingerprint", side_effect=lambda r, c, *a, **kw: c))
            rc = wg._run_attempt("w", "demo", [s + ":" + b for s, b, _ in entries],
                                 "fixture", fixes, self.sha, self.sha, None, 0, None)
        return rc, calls, advanced, self.rows()

    def test_AC2(self):
        for mode in ("base", "mixed", "incomplete", "pg", "coverage"):
            with contextlib.nullcontext():
                # Each independent fixture has a fresh event stream.
                path = self.root / "events" / "L-executor-0001.jsonl"
                if path.exists():
                    path.unlink()
                nodes = [OWNED] if mode == "coverage" else [AUDIENCE] + ([OWNED] if mode == "mixed" else [])
                rc, calls, advanced, rows = self.attempt(
                    "AB", lambda kept, n: ("red", nodes) if n == 1 else ("green", []),
                    base_ids=[AUDIENCE], complete=mode != "incomplete", uncovered=mode == "coverage",
                    pg=mode == "pg", fixes=mode == "pg", base_pg="red")
                drops = [r for r in rows if r["type"] == "window-dropped"]
                if mode == "mixed":
                    self.assertEqual([r["subject"] for r in drops], ["A"])
                    self.assertEqual(drops[0]["nodes"], [OWNED])
                    self.assertEqual(drops[0]["owned_nodes"], [OWNED])
                    self.assertEqual(advanced[0][1], ["B"])
                else:
                    self.assertEqual(drops, [])
                    self.assertEqual(bool(advanced), mode == "base")
                if mode in ("base", "mixed", "pg"):
                    evidence = [r for r in rows if r["type"] == "window-pre-existing"]
                    self.assertEqual(evidence[0]["ids"], [AUDIENCE])
                    self.assertEqual(evidence[0]["count"], 1)
                self.assertEqual(rc, 0 if mode == "base" else 1 if mode == "mixed" else 2)
        # Required-check enforcement belongs to the production verifier too.
        class IncompleteProcess:
            returncode = 0
            def communicate(self, **kwargs):
                return json.dumps({"verdict": "green", "checks": {"pytest": "ran"}}), None
        with patch.object(wg, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(wg.subprocess, "Popen", return_value=IncompleteProcess()):
            result = wg.verify(self.repo, self.sha, [wg.PYTEST_CHECK, wg.PG_CHECK], "coverage")
        self.assertNotEqual(result["state"], "green")
        self.response("AC2", {"cases": ["base", "mixed", "incomplete", "pg", "coverage"],
            "mixed_drop": {"subject": "A", "owned_nodes": [OWNED]},
            "required_check_guard": result["state"]})
        print("L-spec-1712 AC2 ok")

    def test_AC3(self):
        modes = ("mixed", "second", "uncertain", "budget", "exhaust", "migration")
        for mode in modes:
            with contextlib.nullcontext():
                path = self.root / "events" / "L-executor-0001.jsonl"
                if path.exists():
                    path.unlink()
                def judgment(kept, n):
                    if n == 1:
                        return "red", [OWNED, UNKNOWN] if mode == "mixed" else [UNKNOWN] if mode in ("budget", "exhaust", "uncertain") else [OWNED]
                    if mode in ("budget", "exhaust"):
                        return "red", [UNKNOWN]
                    if mode == "uncertain" and kept == ("B", "C"):
                        return "undetermined", []
                    if mode in ("mixed", "second") and len(kept) > 1:
                        return "red", [UNKNOWN]
                    if mode == "mixed" and kept == ("C",):
                        return "red", [UNKNOWN]
                    return "green", []
                rc, calls, advanced, rows = self.attempt("ABC", judgment,
                    migrations=("A", "B") if mode == "migration" else (), exhaust=mode == "exhaust")
                drops = [r for r in rows if r["type"] == "window-dropped"]
                expected = ["A", "C"] if mode == "mixed" else ["A"] if mode in ("second", "migration") else list("ABC") if mode == "budget" else []
                self.assertEqual([r["subject"] for r in drops], expected)
                self.assertLessEqual(len(calls), 7)
                for row in drops:
                    self.assertTrue(row["reason"])
                    self.assertIn("owned_nodes", row)
                    self.assertIn("unattributed_nodes", row)
                    self.assertEqual(row["owned_nodes"], [OWNED] if row["subject"] == "A" and mode in ("mixed", "second", "migration") else [])
                    self.assertEqual(row["unattributed_nodes"], [UNKNOWN] if mode in ("mixed", "budget") else [])
                    if row["reason"] == "bisect-single":
                        self.assertIn((row["subject"],), calls)
                if mode in ("mixed", "second"):
                    self.assertEqual(advanced[0][1], ["B"])
                    self.assertNotIn(("B", "C"), [tuple(a[1]) for a in advanced])
                if mode in ("budget", "exhaust", "migration"):
                    self.assertFalse(advanced)
                if mode == "exhaust":
                    self.assertEqual(len(calls), 7)
                    self.assertTrue(any("budget exhausted" in r.get("why", "") for r in rows))
                if mode == "uncertain":
                    self.assertEqual(advanced[0][1], ["A"])
                    self.assertTrue(any(r["type"] == "escalation-blocking" for r in rows))
        # The production bisection must never judge a union of independently green halves.
        seen = []
        with patch.object(wg, "ev"):
            chosen = wg.bisect_window("w", list("ABCD"),
                lambda half: (tuple(half), half, {}),
                lambda cand, kept: seen.append(cand) or {"state": "green"},
                lambda *a: self.fail("green half dropped"), lambda *a: None)
        self.assertEqual(seen, [("A", "B"), ("C", "D")])
        self.assertEqual(chosen, (("A", "B"), ["A", "B"]))
        self.response("AC3", {"cases": list(modes), "judgment_limit": 7,
            "independent_green_halves": seen, "selected": chosen[1],
            "mixed_drops": ["A", "C"], "second_round_drops": ["A"],
            "exhaustion_drops": [], "unsafe_migration_remainder": "pending"})
        print("L-spec-1712 AC3 ok")

    def test_AC4(self):
        import resources
        for outcomes in (("timeout", "clean"), ("timeout", "timeout"),
                         ("findings",), ("timeout", "findings"), ("other",)):
            with contextlib.nullcontext():
                path = self.root / "events" / "L-executor-0001.jsonl"
                if path.exists():
                    path.unlink()
                scans, killed, reaped, cleaned = [], [], [], []
                def git(*args):
                    if args[0] == "ls-tree": return mg.DETECT_SECRETS_SCRIPT.encode()
                    if args[0] == "show": return b"#!/bin/bash\n"
                    if args[:2] == ("worktree", "add"):
                        Path(args[3]).mkdir(exist_ok=True)
                        return b""
                    self.fail("unexpected scanner git")
                class Proc:
                    pid = 1234567
                    def __init__(p, argv, **kwargs):
                        p.outcome = outcomes[len(scans)]
                        scans.append(argv)
                        self.assertEqual(argv[-2:], [self.sha, self.sha])
                        self.assertTrue(kwargs["start_new_session"])
                        self.assertTrue(Path(kwargs["cwd"]).is_dir())
                        p.returncode = 0 if p.outcome == "clean" else 1
                    def communicate(p, timeout=None):
                        if p.outcome == "timeout" and timeout is not None:
                            raise subprocess.TimeoutExpired("fixture", timeout)
                        if timeout is None: reaped.append(p.pid)
                        return ((b"  tests/example.py:1 -- Secret Keyword\n", b"") if p.outcome == "findings" else (b"", b"fixture error") if p.outcome == "other" else (b"", b""))
                def remove(path):
                    cleaned.append(path)
                    Path(path).rmdir()
                original_diagnostic = mg._scan_diagnostic
                with patch.object(mg, "gate", return_value=([], [], self.sha, self.sha, self.sha)), \
                     patch.object(mg, "_TOP", str(self.repo)), \
                     patch.object(mg.freeze, "state", return_value=None), \
                     patch.object(mg, "git", side_effect=git), \
                     patch.object(resources, "record"), \
                     patch.object(mg.subprocess, "Popen", Proc), \
                     patch.object(mg.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), \
                     patch.object(mg.os, "killpg", side_effect=lambda pid, sig: killed.append((pid, sig))), \
                     patch.object(mg, "_remove_worktree", side_effect=remove), \
                     patch.object(mg, "_scan_diagnostic", side_effect=lambda *a, **kw: original_diagnostic(*a, **kw, clock=NOW)):
                    rc = mg.main_(["work", "fixture"])
                rows = self.rows()
                kinds = [r["type"] for r in rows]
                self.assertEqual(len(scans), len(outcomes))
                self.assertEqual(len(cleaned), len(outcomes))
                self.assertTrue(all(not p.exists() for p in cleaned))
                self.assertEqual(killed, [(1234567, signal.SIGKILL)] * outcomes.count("timeout"))
                self.assertEqual(reaped, [1234567] * outcomes.count("timeout"))
                if outcomes == ("timeout", "timeout"):
                    self.assertEqual(rc, 2)
                    self.assertEqual(kinds, ["infra-failure"])
                    self.assertEqual(rows[0]["backend"], "merge-gate")
                    self.assertEqual(rows[0]["retry_at"], (NOW + timedelta(minutes=5)).isoformat(timespec="seconds"))
                elif outcomes[-1] == "clean":
                    self.assertEqual((rc, kinds), (0, ["merge-gate-clean"]))
                else:
                    self.assertEqual((rc, kinds), (1, ["merge-gate-rework"]))
                    self.assertEqual(rows[0]["reason"], "secret-scan")
        self.response("AC4", {"timeout_clean_scans": 2, "timeout_timeout_scans": 2,
            "double_timeout_exit": 2, "double_timeout_events": ["infra-failure"],
            "retry_at": (NOW + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "group_kill_reap_cleanup": True, "findings_after_timeout": "rework",
            "other_undetermined_scans": len(scans)})
        print("L-spec-1712 AC4 ok")


if __name__ == "__main__":
    # A second bound applies when invoked outside the authored shell chain.
    assert "test_window_gate" not in sys.modules and "test_merge_gate" not in sys.modules
    assert unittest.defaultTestLoader.getTestCaseNames(Disposition) == ["test_AC1", "test_AC2", "test_AC3", "test_AC4"]
    signal.alarm(170)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Disposition))
        for line in captured.getvalue().splitlines():
            if line.startswith(("L-spec-1712 AC", "{")):
                print(line)
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        signal.alarm(0)
        _ISO.cleanup()
