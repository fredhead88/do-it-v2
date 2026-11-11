#!/usr/bin/env python3
"""Disposable-root proofs for the selection switch predicate."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Isolate the root before importing product modules; no production ledger is read.
_imports = tempfile.TemporaryDirectory(prefix="selection-import-")
with patch.dict(os.environ, {"DOIT_ROOT": _imports.name}):
    import guards_live
    import selection_switch as sw

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parent
REASONS = {"canary-green", "no-canary", "canary-miss", "undetermined"}
ACTOR_FILE = "L-base-red-local.jsonl"
GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null")


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV), check=True)
    return done.stdout.strip()


def at(minutes):
    return (NOW - timedelta(hours=10) + timedelta(minutes=minutes)).isoformat()


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="selection-switch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "events").mkdir()
        self.repo = self.root / "repos" / "example"
        self.repo.mkdir(parents=True)
        (self.root / "repos" / "other").mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(sw.fold, "ROOT", self.root))
        stack.enter_context(patch.dict(os.environ, {"DOIT_ROOT": str(self.root),
                                                    "PYTHONDONTWRITEBYTECODE": "1"}))
        self.seq = 0
        self.results = []

    def graph(self):
        if (self.repo / ".git").exists():
            return
        git(self.repo, "init", "-q")
        def commit(name, parent=None):
            if parent:
                git(self.repo, "checkout", "-q", "--detach", parent)
            else:
                git(self.repo, "checkout", "-q", "--orphan", name)
                git(self.repo, "rm", "-rfq", "--ignore-unmatch", ".")
            (self.repo / name).write_text(name)
            git(self.repo, "add", name)
            git(self.repo, "commit", "-q", "-m", name)
            return git(self.repo, "rev-parse", "HEAD")
        self.A = commit("a")
        self.B = commit("b", self.A)
        self.C = commit("c", self.B)
        self.D = commit("d", self.A)   # descends from A only
        self.X = commit("x")           # unrelated root

    def row(self, kind="green", minutes=0, sha=None, **changes):
        self.seq += 1
        row = dict(type="selected-canary", schema=2, project="example",
                   sha=sha or "a" * 40, run_id=self.seq, ts=at(minutes),
                   ids=[], green=True, complete=True)
        if kind == "miss":
            row.update(green=False, ids=["tests/test_x.py::t"])
        row.update(changes)
        return row

    def write(self, rows, name=ACTOR_FILE):
        path = self.root / "events" / name
        with path.open("a") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def reset(self):
        for path in (self.root / "events").iterdir():
            path.unlink()

    def ask(self, project="example", now=NOW):
        result = sw.allowed(project, now)
        self.results.append(result)
        return result

    def expect(self, rows, allowed, reason, since="ANY", **kw):
        self.reset()
        self.write(rows, **kw)
        result = self.ask()
        self.assertEqual((result["allowed"], result["reason"]), (allowed, reason), rows)
        if since != "ANY":
            self.assertEqual(result["since"], since)
        return result

    def digest(self):
        out = {}
        for path in sorted((self.root / "events").iterdir()):
            data = path.read_bytes()
            out[path.name] = (len(data), hashlib.sha256(data).hexdigest())
        return out


class TestSwitch(Fixture):
    def test_ac1_ignored_rows_are_no_canary(self):
        self.assertEqual(self.ask(), dict(allowed=False, reason="no-canary", since=None))
        scenarios = {
            "schema-1": [self.row(schema=1)],
            "schema missing": [{k: v for k, v in self.row().items() if k != "schema"}],
            "other project": [self.row(project="other")],
            "other type bad ts": [self.row(type="master-full-run", ts="garbage")],
            "other project future ts": [self.row(project="other", ts=(NOW + timedelta(days=3)).isoformat())],
            "other project bad ts": [self.row(project="other", ts="nope")],
        }
        for label, rows in scenarios.items():
            with self.subTest(label):
                self.expect(rows, False, "no-canary", None)
        with self.subTest("unadmitted actor"):
            self.expect([self.row()], False, "no-canary", None, name="L-thinker-local.jsonl")

    def test_ac2_single_green(self):
        row = self.row(minutes=5)
        self.assertEqual(guards_live._observation(row, "r1")[1], None)
        self.expect([row], True, "canary-green", row["ts"])
        later = self.row(minutes=9)
        self.expect([row, later], True, "canary-green", later["ts"])
        self.expect([later, row], True, "canary-green", later["ts"])

    def test_ac3_miss_cleared_only_by_descendant_green(self):
        self.graph()
        A, B, C, D, X = self.A, self.B, self.C, self.D, self.X
        green_a, miss_b = self.row(minutes=1, sha=A), self.row("miss", 2, sha=B)
        self.expect([green_a, miss_b], False, "canary-miss", miss_b["ts"])
        self.expect([green_a, miss_b, self.row(minutes=3, sha=A)], False, "canary-miss", miss_b["ts"])
        self.expect([green_a, miss_b, self.row(minutes=3, sha=D)], False, "canary-miss", miss_b["ts"])
        self.expect([green_a, miss_b, self.row(minutes=3, sha=X)], False, "canary-miss", miss_b["ts"])
        self.expect([green_a, miss_b, self.row(minutes=3, sha=B)], False, "canary-miss", miss_b["ts"])
        self.expect([green_a, miss_b, self.row(minutes=3, sha=B[:10])], False, "canary-miss", miss_b["ts"])
        c = self.row(minutes=3, sha=C)
        self.expect([green_a, miss_b, c], True, "canary-green", c["ts"])
        # Two outstanding misses; a green descending from only one stays blocked.
        miss_d = self.row("miss", 2.5, sha=D)
        self.expect([green_a, miss_b, miss_d, c], False, "canary-miss", miss_d["ts"])
        self.expect([green_a, miss_b, miss_d, c, self.row(minutes=4, sha=D[:12])], False, "canary-miss")
        # Equal ts never clears.
        self.expect([miss_b, self.row(minutes=2, sha=C)], False, "canary-miss", miss_b["ts"])

    def test_ac4_undetermined_never_allowed(self):
        self.graph()
        A, B, C = self.A, self.B, self.C
        bad = {
            "torn line": None,
            "ids not list": self.row(ids="x"),
            "complete false": self.row(complete=False),
            "malformed sha": self.row(sha="zz"),
            "future green": self.row(ts=(NOW + timedelta(minutes=1)).isoformat()),
            "unparseable ts": self.row(ts="not-a-time"),
            "red without ids": self.row(green=False),
            "green with ids": self.row(ids=["t"]),
        }
        for label, row in bad.items():
            with self.subTest(label):
                self.reset()
                if row is None:
                    self.write([self.row()])
                    with (self.root / "events" / ACTOR_FILE).open("a") as handle:
                        handle.write("{torn\n")
                    result = self.ask()
                else:
                    self.write([row])
                    result = self.ask()
                self.assertEqual(result, dict(allowed=False, reason="undetermined", since=None))
        for label, kwargs in {"naive now": dict(now=NOW.replace(tzinfo=None)),
                              "bad now": dict(now="today"),
                              "bad project": dict(project="../x"),
                              "unknown project": dict(project="ghost"),
                              "non-string project": dict(project=None)}.items():
            with self.subTest(label):
                self.reset()
                self.write([self.row()])
                self.assertEqual(self.ask(**kwargs),
                                 dict(allowed=False, reason="undetermined", since=None))
        with self.subTest("absent miss sha"):
            self.expect([self.row("miss", 1, sha="e" * 40), self.row(minutes=2, sha=C)],
                        False, "undetermined", None)
        malformed = self.row(minutes=5, complete=False)
        with self.subTest("tie"):
            self.expect([malformed, self.row(minutes=5)], False, "undetermined", None)
        with self.subTest("later green clears"):
            later = self.row(minutes=6)
            self.expect([malformed, later], True, "canary-green", later["ts"])
        with self.subTest("earlier green does not clear"):
            self.expect([self.row(minutes=4), malformed], False, "undetermined", None)
        with self.subTest("malformed then descendant green"):
            miss = self.row("miss", 1, sha=B)
            green = self.row(minutes=7, sha=C)
            self.expect([miss, malformed, green], True, "canary-green", green["ts"])
            self.expect([miss, malformed, self.row(minutes=7, sha=A)], False, "canary-miss", miss["ts"])
        with self.subTest("precedence"):
            self.expect([self.row(minutes=1, sha=A), self.row("miss", 2, sha=B),
                         self.row(minutes=3, complete=False)], False, "undetermined", None)

    def test_ac5_result_shape(self):
        self.test_ac1_ignored_rows_are_no_canary()
        self.test_ac2_single_green()
        self.test_ac3_miss_cleared_only_by_descendant_green()
        self.test_ac4_undetermined_never_allowed()
        self.assertGreater(len(self.results), 30)
        for result in self.results:
            self.assertEqual(set(result), {"allowed", "reason", "since"})
            self.assertIs(type(result["allowed"]), bool)
            self.assertIn(result["reason"], REASONS)
            self.assertTrue(result["since"] is None or isinstance(result["since"], str))
            self.assertEqual(result["allowed"], result["reason"] == "canary-green")

    def test_ac6_round_trip_read_only_and_cli(self):
        green = self.row(minutes=1)
        self.write([green])
        before = self.digest()
        self.assertEqual(self.ask(), dict(allowed=True, reason="canary-green", since=green["ts"]))
        self.assertEqual(self.digest(), before)
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        cli = lambda: subprocess.run([sys.executable, str(SRC / "selection_switch.py"),
                                      "--project", "example"], env=env,
                                     capture_output=True, text=True, timeout=30)
        # CLI uses the real clock; the green row is in the past so it is allowed.
        done = cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout.splitlines()), 1)
        self.assertEqual(json.loads(done.stdout), sw.allowed("example"))
        self.assertEqual(self.digest(), before)
        self.graph()
        self.reset()
        self.write([self.row("miss", 1, sha=self.B)])
        done = cli()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["reason"], "canary-miss")
        self.assertEqual(json.loads(done.stdout), sw.allowed("example"))
        unmodified = subprocess.run([sys.executable, str(SRC / "test_guards_live.py")], env=env,
                                    capture_output=True, text=True, timeout=300, cwd=SRC)
        self.assertEqual(unmodified.returncode, 0, unmodified.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
