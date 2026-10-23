#!/usr/bin/env python3
"""Hermetic ref-only origin push regressions, with real bare origins and clones."""
import copy
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, look, push_origin


def response(criterion, body):
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps({"criterion": criterion, "url": f"cli://push_origin/{criterion}",
                      "status": 0, "body_sha256": hashlib.sha256(encoded).hexdigest(),
                      "body_excerpt": body}))


class RecordingRunner(push_origin.Runner):
    def __init__(self, root, failures=None, timeouts=(), after_fetch=None):
        self.root = root
        self.calls = []
        self.failures = failures or {}
        self.timeouts = timeouts
        self.after_fetch = after_fetch

    def git(self, args, cwd, timeout):
        cwd = pathlib.Path(cwd).resolve()
        self.calls.append((list(args), cwd))
        assert cwd.is_relative_to(self.root), f"git cwd escaped fixture: {cwd}"
        if args[0] in self.timeouts:
            raise subprocess.TimeoutExpired("git", timeout)
        if args[0] in self.failures:
            reply = self.failures[args[0]]
            return reply if isinstance(reply, tuple) else (reply, "", "induced failure")
        result = super().git(args, cwd, timeout)
        if args[0] == "fetch" and self.after_fetch:
            callback, self.after_fetch = self.after_fetch, None
            callback()
        return result


class OriginBehindRunner:
    def __init__(self, recorder):
        self.recorder = recorder

    def git(self, args, cwd, timeout):
        return self.recorder.git(args, cwd, timeout)[1]

    def clock(self):
        return time.monotonic()


class PushOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="origin-push-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.events = self.root / "events"
        self.events.mkdir()
        self.runners = []
        self.original_load = look.load_toml
        self.cfg = self.original_load()
        # Every configured absolute path is replaced before any run/alarm call.
        self.cfg["push"]["path"] = {
            name: str(self.root / "outside-repos" / name)
            for name in (self.cfg["push"].get("path") or {})}
        self.cfg["push"]["path"]["do-it-v2-realm"] = str(self.root / "outside-repos" / "realm")
        for p in (patch.object(fold, "ROOT", self.root),
                  patch.object(fold, "EVENTS", self.events),
                  patch.object(fold, "PROJECT", ""),
                  patch.dict(os.environ, {"DOIT_ROOT": str(self.root)}),
                  patch.object(look, "load_toml", lambda: copy.deepcopy(self.cfg)),
                  patch.object(push_origin, "Runner", self.runner)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        calls = [call for runner in self.runners for call in runner.calls]
        for args, cwd in calls:
            self.assertTrue(cwd.is_relative_to(self.root), (args, cwd))
            self.assertIn(args[0], {"fetch", "symbolic-ref", "rev-parse", "rev-list",
                                    "merge-base", "push"})
            if args[0] == "push":
                self.assertEqual(len(args), 3)
                self.assertEqual(args[1], "origin")
                self.assertRegex(args[2], r"^[0-9a-f]{40}:refs/heads/[^:]+$")
        if calls:
            response("AC7", {"git_calls": len(calls), "all_cwds_under_temp_root": True})

    def runner(self, **kw):
        runner = RecordingRunner(self.root, **kw)
        self.runners.append(runner)
        return runner

    def git(self, cwd, *args):
        self.assertTrue(pathlib.Path(cwd).resolve().is_relative_to(self.root))
        return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def commit(self, clone, msg):
        (clone / "f.txt").write_text(msg)
        self.git(clone, "add", "f.txt")
        self.git(clone, "-c", "user.email=t@example.com", "-c", "user.name=t",
                 "commit", "-qm", msg)
        return self.git(clone, "rev-parse", "HEAD")

    def pair(self, name="do-it-v2", branch="main", tag=None):
        tag = tag or name
        origin = self.root / f"{tag}.git"
        clone = pathlib.Path(self.cfg["push"]["path"].get(name, self.root / "repos" / name))
        clone.mkdir(parents=True)
        self.git(self.root, "init", "--bare", "-q", str(origin))
        self.git(origin, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        self.git(self.root, "init", "-q", "-b", branch, str(clone))
        self.git(clone, "remote", "add", "origin", str(origin))
        self.commit(clone, "initial")
        self.git(clone, "push", "-u", "origin", branch)
        self.git(clone, "remote", "set-head", "origin", "-a")
        return origin, clone

    def ledger(self, kind):
        return [e for e in fold.read_events() if e.get("type") == kind]

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.events.glob("*.jsonl")}

    def remote_clone(self, origin, tag="racer"):
        clone = self.root / tag
        self.git(self.root, "clone", "-q", str(origin), str(clone))
        return clone

    def test_hand_merge_and_external_realm(self):
        for name, branch in (("do-it-v2", "main"), ("do-it-v2-realm", "realm")):
            origin, clone = self.pair(name, branch)
            self.git(clone, "checkout", "-qb", "side")
            self.commit(clone, "side commit")
            self.git(clone, "checkout", branch)
            self.git(clone, "-c", "user.email=t@example.com", "-c", "user.name=t",
                     "merge", "--no-ff", "side", "-m", "hand merge")
            sha = self.git(clone, "rev-parse", f"refs/heads/{branch}")
            runner = self.runner()
            self.assertEqual(push_origin.run([], runner=runner), [name])
            self.assertEqual(self.git(origin, "rev-parse", branch), sha)
            events = [e for e in self.ledger("origin-pushed") if e["subject"] == name]
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0]["sha"], events[0]["branch"], events[0]["actor"]),
                             (sha, branch, "tick"))
            _, _, ignored, _ = fold.fold(fold.read_events())
            self.assertFalse(any(e.get("type") == "origin-pushed" for e in ignored))
            self.assertFalse(self.ledger("shipped"))
            self.assertEqual(push_origin.run([], runner=self.runner()), [])
            self.assertEqual(len([e for e in self.ledger("origin-pushed") if e["subject"] == name]), 1)
        self.assertFalse((self.root / "repos" / "do-it-v2-realm").exists())
        response("AC1", {"hand_merge": "pushed", "realm_external_path": "pushed",
                         "fresh_ledger_read": "one event per project", "shipped_events": 0})

    def test_no_push_cases(self):
        cases = {"equal": {}, "behind": {}, "other-branch": {}, "detached": {},
                 "fetch-fails": {"failures": {"fetch": 1}},
                 "ancestry-not-ancestor": {"failures": {"merge-base": 1}},
                 "ancestry-fails": {"failures": {"merge-base": 2}},
                 "walk-fails": {"failures": {"rev-list": 1}},
                 "refs-fail": {"failures": {"rev-parse": 1}},
                 "walk-malformed": {"failures": {"rev-list": (0, "unknown", "")}},
                 "fetch-timeout": {"timeouts": {"fetch"}},
                 "walk-timeout": {"timeouts": {"rev-list"}},
                 "ancestry-timeout": {"timeouts": {"merge-base"}}}
        for case, kw in cases.items():
            with self.subTest(case=case):
                self.cfg["push"]["path"]["do-it-v2"] = str(self.root / "cases" / case)
                origin, clone = self.pair(tag=case)
                if case == "behind":
                    racer = self.remote_clone(origin, case + "-remote")
                    self.commit(racer, "origin ahead")
                    self.git(racer, "push", "origin", "HEAD")
                elif case != "equal":
                    self.commit(clone, "local ahead")
                if case == "other-branch":
                    self.git(clone, "checkout", "-qb", "feature")
                if case == "detached":
                    self.git(clone, "checkout", "--detach", "HEAD")
                before, ledger = self.git(origin, "rev-parse", "main"), self.snapshot()
                runner = self.runner(**kw)
                self.assertEqual(push_origin.run([], runner=runner), [])
                self.assertEqual(self.git(origin, "rev-parse", "main"), before)
                self.assertEqual(self.snapshot(), ledger)
                self.assertFalse(any(args[0] == "push" for args, _ in runner.calls))
        response("AC2", {"no_push_no_append_cases": list(cases), "remote_refs": "unchanged"})

    def test_config_and_checkout_resolution(self):
        cfg = self.original_load()
        self.assertEqual(cfg["push"]["path"]["do-it-v2-realm"], "/home/albert/do-it-v2-realm")
        self.assertEqual(dict(push_origin.targets(cfg)),
                         {"do-it-v2": "main", "albert-scott": "master", "do-it-v2-realm": "realm"})
        for resolver in (look.push_checkout, push_origin.checkout):
            self.assertEqual(resolver(cfg, "do-it-v2-realm"), pathlib.Path("/home/albert/do-it-v2-realm"))
            self.assertEqual(resolver(cfg, "do-it-v2"), self.root / "repos" / "do-it-v2")
        absent = self.root / "no-path.toml"
        absent.write_text('[push]\nprojects=["do-it-v2"]\n[push.main]\ndo-it-v2="main"\n')
        self.assertEqual(self.original_load(absent)["push"]["path"], {})
        defaults = self.original_load(self.root / "missing.toml")
        self.assertEqual(defaults["push"]["path"], {})
        self.assertEqual(dict(push_origin.targets(defaults)), dict(push_origin.targets(cfg)))
        self.assertFalse(any(r.calls for r in self.runners))
        response("AC3", {"shipped_path": "preserved", "absent_path": {}, "missing_file_targets": 3,
                         "resolvers": "configured path or ROOT/repos/project"})

    def test_target_branch_fallback_uses_path(self):
        self.cfg["push"]["path"]["custom"] = str(self.root / "custom-checkout")
        self.pair("custom", "trunk")
        cfg = {"push": {"projects": ["custom"], "path": self.cfg["push"]["path"]}}
        self.assertEqual(push_origin.targets(cfg), [("custom", "trunk")])
        self.assertEqual(push_origin.targets({"push": {"projects": ["missing"]}}), [])

    def test_missing_checkouts_skip_without_calls(self):
        runner = self.runner()
        self.assertEqual(push_origin.run([], runner=runner), [])
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.snapshot())

    def test_divergence_and_ledger_dedupe(self):
        origin, clone = self.pair()
        local = self.commit(clone, "local only")
        racer = self.remote_clone(origin)
        remote = self.commit(racer, "origin only")
        self.git(racer, "push", "origin", "HEAD")
        for _ in range(2):
            runner = self.runner()
            self.assertEqual(push_origin.run([], runner=runner), [])
            self.assertFalse(any(args[0] == "push" for args, _ in runner.calls))
            self.assertEqual(self.git(origin, "rev-parse", "main"), remote)
        rows = self.ledger("origin-diverged")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["subject"], rows[0]["sha"], rows[0]["origin_sha"], rows[0]["branch"]),
                         ("do-it-v2", local, remote, "main"))
        local = self.commit(clone, "local advances again")
        self.assertEqual(push_origin.run([], runner=self.runner()), [])
        self.assertEqual(len(self.ledger("origin-diverged")), 2)
        self.assertEqual(self.ledger("origin-diverged")[-1]["sha"], local)
        remote = self.commit(racer, "origin advances again")
        self.git(racer, "push", "origin", "HEAD")
        self.assertEqual(push_origin.run([], runner=self.runner()), [])
        self.assertEqual(len(self.ledger("origin-diverged")), 3)
        self.assertEqual(self.ledger("origin-diverged")[-1]["origin_sha"], remote)
        self.assertEqual(self.git(origin, "rev-parse", "main"), remote)
        self.assertFalse(self.ledger("origin-pushed"))
        self.commit(clone, "unreadable ledger")
        before = self.snapshot()
        with patch.object(fold, "read_events", side_effect=OSError("unreadable ledger")):
            self.assertEqual(push_origin.run([], runner=self.runner()), [])
        self.assertEqual(self.snapshot(), before)
        for kw in ({"failures": {"merge-base": 2}}, {"timeouts": {"merge-base"}},
                   {"failures": {"rev-list": 1}}):
            self.assertEqual(push_origin.run([], runner=self.runner(**kw)), [])
            self.assertEqual(self.snapshot(), before)
        response("AC4", {"repeat_pair_events": 1, "after_each_side_advances": 3,
                         "origin": "unchanged by pass", "unreadable_ledger": "no append"})

    def test_origin_behind_resolves_external_checkout(self):
        self.pair("do-it-v2-realm", "realm")
        cfg = copy.deepcopy(self.cfg)
        cfg["push"]["projects"] = ["do-it-v2-realm"]
        cfg["push"]["main"]["do-it-v2-realm"] = "realm"
        briefs, answered = [], []
        runner = OriginBehindRunner(self.runner())
        now = datetime.now(timezone.utc)
        look._check_origin_behind(cfg, runner, now, runner.clock() + 90, [], self.root, briefs, answered)
        self.assertFalse(briefs)
        self.assertTrue(runner.recorder.calls)
        self.assertTrue(all(cwd == pathlib.Path(cfg["push"]["path"]["do-it-v2-realm"])
                            for _, cwd in runner.recorder.calls))
        self.assertFalse((self.root / "repos" / "do-it-v2-realm").exists())
        cfg["push"]["projects"] = ["missing"]
        cfg["push"]["main"]["missing"] = "main"
        look._check_origin_behind(cfg, runner, now, runner.clock() + 90, [], self.root, briefs, answered)
        self.assertTrue(any(b["condition"] == "reading-undetermined"
                            and b["key"] == "undetermined:origin-behind" for b in briefs))
        response("AC6", {"realm": "determined", "missing_checkout": "reading-undetermined"})

    def test_constant_call_budget_and_tip_after_shipped_commit(self):
        origin, clone = self.pair()
        base = self.git(clone, "rev-parse", "HEAD")
        marks = self.root / "history.marks"
        stream = []
        # Import the whole history in one process instead of spawning hundreds
        # of commit-tree processes (and their automatic maintenance).
        for i in range(500):
            message = f"history-{i}"
            parent = base if i == 0 else f":{i}"
            stream.append(f"commit refs/heads/main\nmark :{i + 1}\n"
                          f"committer t <t@example.com> {1700000000 + i} +0000\n"
                          f"data {len(message)}\n{message}\nfrom {parent}\n\n")
        result = subprocess.run(
            ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "fast-import", "--quiet", f"--export-marks={marks}"],
            cwd=clone, input="".join(stream), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = dict(line.split() for line in marks.read_text().splitlines())
        self.assertEqual(set(imported), {f":{i + 1}" for i in range(500)})
        self.assertEqual(self.git(clone, "rev-list", "--count", f"{base}..main"), "500")
        self.assertEqual(self.git(clone, "rev-parse", "main"), imported[":500"])
        events = [{"type": "shipped", "project": "do-it-v2", "sha": imported[f":{i + 1}"]}
                  for i in range(500)]
        tip = self.commit(clone, "unshipped tip")
        runner = self.runner()
        self.assertEqual(push_origin.run(events, runner=runner), ["do-it-v2"])
        self.assertEqual(self.git(origin, "rev-parse", "main"), tip)
        self.assertEqual(self.ledger("origin-pushed")[0]["sha"], tip)
        self.assertLessEqual(len(runner.calls), 6)
        self.assertEqual(push_origin.run(events, runner=self.runner()), [])
        self.assertEqual(len(self.ledger("origin-pushed")), 1)

    def test_push_failure_race_and_project_independence(self):
        origin, clone = self.pair()
        before = self.git(origin, "rev-parse", "main")
        tip = self.commit(clone, "ahead")
        for kw in ({"failures": {"push": 1}}, {"timeouts": {"push"}}):
            runner = self.runner(**kw)
            self.assertEqual(push_origin.run([], runner=runner), [])
            self.assertEqual(sum(args[0] == "push" for args, _ in runner.calls), 1)
            self.assertEqual(self.git(origin, "rev-parse", "main"), before)
            self.assertFalse(self.snapshot())
        other_origin, other = self.pair("albert-scott", "master")
        other_tip = self.commit(other, "other ahead")
        racer = self.remote_clone(origin)
        def race():
            self.commit(racer, "race")
            self.git(racer, "push", "origin", "HEAD")
        # The other project must still succeed after a real non-fast-forward race.
        runner = self.runner(after_fetch=race)
        self.assertEqual(push_origin.run([], runner=runner), ["albert-scott"])
        self.assertNotEqual(self.git(origin, "rev-parse", "main"), tip)
        self.assertEqual(self.git(other_origin, "rev-parse", "master"), other_tip)
        self.assertEqual([e["subject"] for e in self.ledger("origin-pushed")], ["albert-scott"])

    def test_isolation_detects_external_cwd(self):
        runner = self.runner()
        # The assertion precedes subprocess execution, so the negative control is harmless.
        with self.assertRaisesRegex(AssertionError, "escaped fixture"):
            runner.git(["symbolic-ref", "HEAD"], pathlib.Path("/home/albert/do-it-v2-realm"), 10)
        args, cwd = runner.calls.pop()
        self.assertFalse(cwd.is_relative_to(self.root))
        self.assertEqual(args, ["symbolic-ref", "HEAD"])


if __name__ == "__main__":
    unittest.main()
