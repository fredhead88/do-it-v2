#!/usr/bin/env python3
"""Scratch-only regression checks for builder base refreshes (L-spec-1784)."""
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import base_refresh
import packet


class Context:
    def __init__(self, repository, events, **args):
        self.evs = events
        self.a = SimpleNamespace(role="builder", base_sha=None, repo=str(repository),
                                 subject="L-spec-1784")
        for key, value in args.items():
            setattr(self.a, key, value)

    def project(self):
        return "scratch"

    def all_of(self, kind):
        return [event for event in self.evs if event.get("type") == kind]

    def last(self, kind):
        return next(iter(reversed(self.all_of(kind))), None)


class BaseRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="l1784-refresh-")
        self.addCleanup(temporary.cleanup)
        self.repo = pathlib.Path(temporary.name)
        self.git("init", "-q", "--initial-branch=release/main")
        self.git("config", "user.name", "Scratch test")
        self.git("config", "user.email", "scratch@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "earliest")
        self.earliest = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "--allow-empty", "-m", "current main")
        self.tip = self.git("rev-parse", "HEAD")
        config = patch("look.load_toml", return_value={
            "push": {"main": {"scratch": "release/main"}}})
        config.start()
        self.addCleanup(config.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def context(self, *kinds, **args):
        # Identical timestamps deliberately exercise list ordering, not time.
        events = [{"type": kind, "ts": "2026-10-28T00:00:00Z"} for kind in kinds]
        for event in events:
            if event["type"] == "build-done":
                event["base_sha"] = self.earliest
        return Context(self.repo, events, **args)

    def test_ac1_conflict_and_memoization(self):
        context = self.context("build-done", "conflict-rework")
        with patch("base_refresh.subprocess.run", wraps=subprocess.run) as run:
            self.assertEqual(base_refresh.refresh_tip(context), self.tip)
            self.assertEqual(packet.verify_base_sha(context), self.tip)
            self.assertEqual(base_refresh.refresh_tip(context), self.tip)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(base_refresh.refresh_tip(self.context("conflict-rework")), self.tip)
        self.assertIsNone(base_refresh.refresh_tip(
            self.context("conflict-rework", "build-done")))
        print("L-spec-1784 AC1 ok")

    def test_ac2_dependency_release_order(self):
        context = self.context("dependency-hold", "build-done", "unblocked")
        self.assertEqual(base_refresh.refresh_tip(context), self.tip)
        self.assertEqual(packet.verify_base_sha(context), self.tip)
        self.assertEqual(base_refresh.refresh_tip(
            self.context("dependency-hold", "unblocked")), self.tip)
        for kinds in (("build-done", "unblocked"),
                      ("dependency-hold", "unblocked", "build-done"),
                      ("build-done", "unblocked", "dependency-hold")):
            self.assertIsNone(base_refresh.refresh_tip(self.context(*kinds)))
        print("L-spec-1784 AC2 ok")

    def test_ac3_must_fix_keeps_earliest_base(self):
        for kind in ("must-fix", "rejected-criterion"):
            context = self.context("build-done", "build-done", kind)
            context.evs[1]["base_sha"] = self.tip
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertEqual(packet.verify_base_sha(context), self.earliest)
            context.evs[1]["base_sha_explicit"] = True
            self.assertEqual(packet.verify_base_sha(context), self.tip)
        print("L-spec-1784 AC3 ok")

    def test_ac4_explicit_pin_and_other_roles(self):
        context = self.context("build-done", "conflict-rework", base_sha=self.earliest)
        self.assertEqual(packet.verify_base_sha(context), self.earliest)
        for role in ("grader", None):
            context = self.context("build-done", "conflict-rework", role=role)
            if role is None:
                del context.a.role
            with patch("base_refresh.subprocess.run") as run:
                self.assertIsNone(base_refresh.refresh_tip(context))
                self.assertEqual(packet.verify_base_sha(context), self.earliest)
                run.assert_not_called()
        context = self.context()
        del context.evs
        self.assertIsNone(base_refresh.refresh_tip(context))
        context = self.context()
        context.evs = None
        self.assertIsNone(base_refresh.refresh_tip(context))
        print("L-spec-1784 AC4 ok")

    def test_ac5_git_failure_and_invalid_results(self):
        context = self.context("build-done", "conflict-rework")
        context.a.repo = str(self.repo / "not-a-repository")
        with patch("base_refresh.subprocess.run", wraps=subprocess.run) as run:
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertEqual(packet.verify_base_sha(context), self.earliest)
            self.assertEqual(run.call_count, 1)
        for main in ("bad ref", "main;exit", "", None):
            with patch("look.load_toml", return_value={"push": {"main": {"scratch": main}}}), \
                    patch("base_refresh.subprocess.run") as run:
                self.assertIsNone(base_refresh.refresh_tip(self.context("conflict-rework")))
                run.assert_not_called()
        with patch("base_refresh.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="not-a-commit\n")):
            self.assertIsNone(base_refresh.refresh_tip(self.context("conflict-rework")))
        with patch("base_refresh.subprocess.run", side_effect=OSError("git unavailable")):
            self.assertIsNone(base_refresh.refresh_tip(self.context("conflict-rework")))
        with patch("base_refresh.ROOT", self.repo.parent), \
                patch("look.load_toml", return_value={"push": {"main": {}}}), \
                patch("base_refresh.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.tip + "\n")) as run:
            context = self.context("conflict-rework", repo=None)
            self.assertEqual(base_refresh.refresh_tip(context), self.tip)
            self.assertEqual(run.call_args.args[0],
                             ["git", "-C", str(self.repo.parent / "repos" / "scratch"),
                              "rev-parse", "master"])
        print("L-spec-1784 AC5 ok")


if __name__ == "__main__":
    unittest.main()
