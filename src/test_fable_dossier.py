#!/usr/bin/env python3
"""Checks for `fable_dossier` (L-spec-2407). Run: python3 test_fable_dossier.py [-k test_ac1_]

Every test builds its own scratch DOIT_ROOT with a scratch git repo standing in
for the project checkout. No network, no model call, no real ledger.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
# Isolation, set before any product module is imported: a direct run must never
# resolve fold.ROOT to ~/.do-it.
_ISO = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-iso-"))
os.environ["HOME"] = str(_ISO / "home")
os.environ["DOIT_ROOT"] = str(_ISO / "root")
sys.path.insert(0, str(HERE))
import base_red  # noqa: E402
import fable_dossier  # noqa: E402

PROJECT = "proj"
NOW = "2026-11-10T00:00:00+00:00"
REAL_GIT = shutil.which("git")
RANK = re.compile(r"^[0-9]+\. (\S+) \| tier=(T[1-4]) \| seconds=(\S+) \| failures=(\S+) "
                  r"\| tags=(\S+) \| block=(\S+)$")


def git(repo, *args):
    subprocess.run([REAL_GIT, "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], check=True, capture_output=True)


def digest(path):
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(path).rglob("*")):
        h.update(str(p.relative_to(path)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def tree(path):
    return {str(p.relative_to(path)) for p in pathlib.Path(path).rglob("*")}


class World(unittest.TestCase):
    def make(self, tracked, durations, history, dups, slowest, sources=None):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fd-root-", dir=_ISO))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(setattr, base_red.fold, "ROOT", base_red.fold.ROOT)
        base_red.fold.ROOT = self.root
        self.repo = self.root / "repos" / PROJECT
        for t in tracked:
            f = self.repo / t
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# test\n")
        (self.repo / "scripts/ci").mkdir(parents=True, exist_ok=True)
        (self.repo / "scripts/ci/shard_durations.json").write_text(
            json.dumps({"version": 1, "files": durations}))
        subprocess.run([REAL_GIT, "init", "-q", str(self.repo)], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        self.ev = self.root / "test-evidence" / PROJECT
        self.ev.mkdir(parents=True)
        self.sources = sources or [{"name": "ci", "from": "2026-10-01", "to": "2026-11-01",
                                    "rows": 10}]
        (self.ev / "history.json").write_text(json.dumps(
            {"project": PROJECT, "generated_at": NOW, "sources": self.sources, "tests": history}))
        (self.ev / "duplicates.json").write_text(json.dumps(dups))
        (self.ev / "slowest.json").write_text(json.dumps(slowest))
        (self.root / "events").mkdir()
        (self.root / "events" / "L-x.jsonl").write_text('{"a":1}\n')

    # AC2/AC3 world: one file per case.
    def world2(self, sources=None):
        T = "tests/"
        tracked = [T + n for n in (
            "test_300_x.py", "test_301_y.py", "test_slow_a.py", "test_slow_b.py", "test_slow_f.py",
            "test_400_a.py", "test_400_b.py", "test_500_s.py", "test_dupe.py", "helper.py")]
        dur = {T + "test_300_x.py": 20, T + "test_301_y.py": 25, T + "test_slow_f.py": 25,
               T + "test_slow_a.py": 20, T + "test_slow_b.py": 20, T + "test_500_s.py": 1}
        hist = {T + "test_300_x.py": {"runs": 9, "failures": 0, "last_failure": None,
                                      "real": 0, "flaky": 0, "unknown": 0},
                T + "test_301_y.py": {"runs": 9, "failures": 1, "last_failure": "2026-11-01",
                                      "real": 1, "flaky": 0, "unknown": 0},
                T + "test_slow_f.py": {"runs": 9, "failures": 3, "last_failure": "2026-11-02",
                                       "real": 2, "flaky": 1, "unknown": 0}}
        dups = [{"test": T + "test_300_x.py", "subset_of": T + "test_slow_a.py", "covered": 4},
                {"test": T + "test_300_x.py", "subset_of": T + "test_slow_b.py", "covered": 7},
                {"test": T + "test_301_y.py", "subset_of": T + "test_slow_a.py", "covered": 2},
                {"test": T + "test_dupe.py", "subset_of": T + "test_slow_a.py", "covered": 1}]
        slowest = [{"test": T + "test_301_y.py", "seconds": 25, "history": "x"},
                   {"test": T + "test_300_x.py", "seconds": 20, "history": "x"}]
        self.make(tracked, dur, hist, dups, slowest, sources)
        self.T, self.hist, self.dups = T, hist, dups
        return [T + n for n in ("test_300_x.py", "test_slow_a.py",
                                "test_301_y.py", "test_500_s.py", "test_400_a.py", "test_400_b.py",
                                "test_dupe.py", "test_slow_f.py")]

    def queue_lines(self):
        text = (self.ev / "dossier" / "queue.md").read_text()
        return [RANK.match(l) for l in text.splitlines() if re.match(r"^[0-9]+\. ", l)]

    def proposal(self, paths, name="proposal.md", lines=None):
        d = _ISO / "proposals"
        d.mkdir(exist_ok=True)
        p = d / name
        p.write_text("\n".join(lines if lines is not None else
                               [f"- {x} :: keep-at-merge :: because :: pin=no" for x in paths]) + "\n")
        return p


class Tests(World):
    def test_ac1_union_and_slow_prefix(self):
        T = "tests/"
        tracked = [T + "test_alpha.py", T + "test_beta.py", T + "test_gamma.py",
                   T + "test_100_a.py", "api/tests/test_1234_b.py", "as_toolkit/tests/test_zz.sh",
                   "as_toolkit/tests/test_5555_c.sh", T + "test_dup.py", T + "helper.py",
                   "docs/test_9999_x.py", T + "test_12_short.py"]
        dur = {T + "test_alpha.py": 50, T + "test_ghost.py": 40, T + "test_beta.py": 30,
               T + "test_gamma.py": 10, T + "test_100_a.py": 5, T + "test_dup.py": 2}
        # total 137, 80% = 109.6: alpha 50, ghost 90, beta 120 -> prefix alpha, ghost, beta.
        dups = [{"test": T + "test_dup.py", "subset_of": T + "test_alpha.py", "covered": 3},
                {"test": T + "test_gone.py", "subset_of": T + "test_alpha.py", "covered": 3}]
        self.make(tracked, dur, {}, dups, [])
        out = fable_dossier.build(PROJECT, now=NOW)
        self.assertEqual(out, self.root / "test-evidence" / PROJECT / "dossier")
        queued = {m.group(1) for m in self.queue_lines()}
        expect = {T + "test_alpha.py", T + "test_beta.py",             # slow, ghost dropped
                  T + "test_100_a.py", "api/tests/test_1234_b.py",       # spec-named .py
                  "as_toolkit/tests/test_5555_c.sh",                     # spec-named .sh
                  T + "test_dup.py"}                                     # duplicate
        self.assertEqual(queued, expect)
        blocks = {str(p.relative_to(out / "blocks"))[:-3]
                  for p in (out / "blocks").rglob("*.md")}
        self.assertEqual(blocks, expect)
        for f in ("queue.md", "proposal-template.md"):
            self.assertTrue((out / f).is_file())
        head = (out / "queue.md").read_text()
        self.assertIn("stale-durations=1", head)
        self.assertIn("stale-duplicates=1", head)
        # without the ghost in the total (97, 77.6) the prefix would still be alpha, beta
        # but stale-durations would be 0: the count proves the full-key computation.

    def test_ac2_order_uniqueness_tags(self):
        expect = self.world2()
        fable_dossier.build(PROJECT, now=NOW)
        ms = self.queue_lines()
        self.assertTrue(all(ms), "a rank line does not match the grammar")
        self.assertEqual([m.group(1) for m in ms], expect)
        self.assertEqual([m.group(2) for m in ms],
                         ["T1", "T1", "T2", "T2", "T2", "T2", "T3", "T4"])
        self.assertEqual(len({m.group(1) for m in ms}), len(ms))
        by = {m.group(1): m for m in ms}
        T = self.T
        self.assertEqual(by[T + "test_300_x.py"].group(5), "slow,spec-named,duplicate")
        self.assertEqual(by[T + "test_301_y.py"].group(5), "slow,spec-named,duplicate")
        self.assertEqual(by[T + "test_slow_a.py"].group(5), "slow")
        self.assertEqual(by[T + "test_slow_a.py"].group(4), "none")
        self.assertEqual(by[T + "test_300_x.py"].group(4), "0")
        self.assertEqual(by[T + "test_400_a.py"].group(3), "none")
        self.assertEqual(by[T + "test_500_s.py"].group(3), "1")
        blk = (self.ev / "dossier" / "blocks" / (T + "test_slow_a.py.md")).read_text()
        self.assertIn("history row: none", blk)
        self.assertNotIn("failures", blk)

    def test_ac3_block_header_template(self):
        src = [{"name": "ci", "from": "2026-10-01", "to": "2026-11-01", "rows": 10},
               {"name": "ledger", "from": "2026-10-01", "to": "2026-11-01", "rows": None,
                "reason": "ledger unreadable"}]
        self.world2(src)
        out = fable_dossier.build(PROJECT, now=NOW)
        T = self.T
        blk = (out / "blocks" / (T + "test_300_x.py.md")).read_text()
        for needle in (T + "test_300_x.py", "T1", "slow,spec-named,duplicate", "20",
                       json.dumps(self.hist[T + "test_300_x.py"]),
                       f"subset_of: {T}test_slow_a.py | covered: 4",
                       f"subset_of: {T}test_slow_b.py | covered: 7", "rank: 2"):
            self.assertIn(needle, blk)
        self.assertIn("not listed", (out / "blocks" / (T + "test_slow_a.py.md")).read_text())
        q = (out / "queue.md").read_text()
        self.assertIn(f"project: {PROJECT}", q)
        self.assertIn(f"generated_at: {NOW}", q)
        counts = dict(re.findall(r"(T[1-4])=([0-9]+)", q.split("history-sources")[0]))
        self.assertEqual(sum(int(v) for v in counts.values()), len(self.queue_lines()))
        for s in src:
            self.assertIn(json.dumps(s), q)
        self.assertIn('"rows": null, "reason": "ledger unreadable"', q)
        tpl = (out / "proposal-template.md").read_text()
        for needle in ("- <path> :: <decision> :: <reason> :: pin=<yes|no>", "keep-at-merge",
                       "nightly-only", "retire", "merge-into:<path>", "pin=yes", "archive/",
                       "never delete"):
            self.assertIn(needle, tpl)

    def test_ac4_checker(self):
        queue = self.world2()
        fable_dossier.build(PROJECT, now=NOW)
        T = self.T
        ok = [f"- {p} :: keep-at-merge :: because :: pin=no" for p in queue]
        res = fable_dossier.check(self.proposal(queue), PROJECT)
        self.assertEqual(res, {"covered": True, "missing": []})
        extra = ok + ["- tests/unqueued.py :: retire :: r :: pin=no"]
        self.assertTrue(fable_dossier.check(self.proposal(None, "e.md", extra), PROJECT)["covered"])
        other = T + "helper.py"  # tracked? helper.py is not a test file -> invalid target
        variants = {
            queue[1]: ok[:1] + ok[2:],                                                 # removed
            queue[2]: ok + [ok[2]],                                                    # repeated
            queue[3]: ok[:3] + [f"- {queue[3]} :: delete :: r :: pin=no"] + ok[4:],    # bad decision
            queue[4]: ok[:4] + [f"- {queue[4]} :: retire ::  :: pin=no"] + ok[5:],     # empty reason
            queue[5]: ok[:5] + [f"- {queue[5]} :: retire :: r :: pin=maybe"] + ok[6:],  # bad pin
            queue[6]: ok[:6] + [f"- {queue[6]} :: merge-into:{queue[6]} :: r :: pin=no"] + ok[7:],
            queue[7]: ok[:7] + [f"- {queue[7]} :: merge-into:{other} :: r :: pin=no"],
        }
        for path, lines in variants.items():
            res = fable_dossier.check(self.proposal(None, "v.md", lines), PROJECT)
            self.assertEqual(res, {"covered": False, "missing": [path]}, path)
        good = ok[:7] + [f"- {queue[7]} :: merge-into:{queue[0]} :: r :: pin=yes"]
        self.assertTrue(fable_dossier.check(self.proposal(None, "g.md", good), PROJECT)["covered"])
        two = ok[1:2] + ok[3:]  # two missing, reported in queue order
        res = fable_dossier.check(self.proposal(None, "t.md", two), PROJECT)
        self.assertEqual(res["missing"], [queue[0], queue[2]])

    def test_ac5_undetermined_and_atomicity(self):
        queue = self.world2()
        und = lambda r: self.assertEqual(len(r["missing"]), 1) or self.assertTrue(
            r["missing"][0].startswith("undetermined:") and not r["covered"], r)
        # no queue.md yet
        und(fable_dossier.check(self.proposal(queue), PROJECT))
        fable_dossier.build(PROJECT, now=NOW)
        und(fable_dossier.check(_ISO / "nope.md", PROJECT))
        empty = _ISO / "empty.md"
        empty.write_text("")
        und(fable_dossier.check(empty, PROJECT))
        und(fable_dossier.check(self.proposal(queue), "no-such-project"))
        (self.ev / "dossier" / "queue.md").write_text("# empty\n")
        und(fable_dossier.check(self.proposal(queue), PROJECT))
        fable_dossier.build(PROJECT, now=NOW)

        before = digest(self.ev / "dossier")
        siblings = lambda: [p.name for p in self.ev.iterdir() if ".tmp-" in p.name or ".old-" in p.name]
        durf = self.repo / "scripts/ci/shard_durations.json"
        for f in (self.ev / "history.json", self.ev / "duplicates.json",
                  self.ev / "slowest.json", durf):
            good = f.read_bytes()
            for bad in (None, b"{not json"):
                f.unlink(missing_ok=True)
                if bad is not None:
                    f.write_bytes(bad)
                with self.assertRaises(ValueError) as cm:
                    fable_dossier.build(PROJECT, now=NOW)
                self.assertIn(f.name, str(cm.exception))
                self.assertEqual(digest(self.ev / "dossier"), before)
                self.assertEqual(siblings(), [])
            f.write_bytes(good)
        # path traversal in duplicates.json
        good = (self.ev / "duplicates.json").read_text()
        (self.ev / "duplicates.json").write_text(json.dumps(
            [{"test": "tests/../../x.py", "subset_of": "a", "covered": 1}]))
        with self.assertRaises(ValueError):
            fable_dossier.build(PROJECT, now=NOW)
        self.assertEqual(digest(self.ev / "dossier"), before)
        self.assertEqual(siblings(), [])
        (self.ev / "duplicates.json").write_text(good)
        # injected failure at the second rename restores the previous dossier
        real = os.rename

        def flaky(src, dst):
            if pathlib.Path(src).name.startswith("dossier.tmp-"):
                raise OSError("injected")
            return real(src, dst)
        with mock.patch.object(fable_dossier.os, "rename", flaky):
            with self.assertRaises(OSError):
                fable_dossier.build(PROJECT, now="2030-01-01")
        self.assertEqual(digest(self.ev / "dossier"), before)
        self.assertEqual(siblings(), [])
        # a second successful build replaces it
        fable_dossier.build(PROJECT, now="2030-01-01")
        self.assertIn("generated_at: 2030-01-01", (self.ev / "dossier" / "queue.md").read_text())
        self.assertEqual(siblings(), [])
        # checkout with no tracked test files
        git(self.repo, "rm", "-q", "-r", "tests")
        git(self.repo, "commit", "-q", "-m", "rm")
        with self.assertRaises(ValueError) as cm:
            fable_dossier.build(PROJECT, now=NOW)
        self.assertIn("no tracked test files", str(cm.exception))
        self.assertEqual(siblings(), [])

    def test_ac6_cli_roundtrip_determinism_discipline(self):
        queue = self.world2()
        stub = pathlib.Path(tempfile.mkdtemp(prefix="fd-stub-", dir=_ISO))
        log = stub / "git.log"
        (stub / "git").write_text(f'#!/bin/sh\necho "$1" >> {log}\nexec {REAL_GIT} "$@"\n')
        (stub / "git").chmod(0o755)
        (stub / "sitecustomize.py").write_text(
            "import socket\n"
            "def _no(*a, **k):\n    raise RuntimeError('network call')\n"
            "socket.socket = _no\n")
        env = {**os.environ, "DOIT_ROOT": str(self.root), "HOME": str(self.root / "home"),
               "PATH": f"{stub}:{os.environ['PATH']}", "PYTHONPATH": str(stub)}
        cli = [sys.executable, str(HERE / "fable_dossier.py")]

        def hashes(skip):
            return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(self.root.rglob("*")) if p.is_file() and skip not in p.parents
                    and ".git" not in p.relative_to(self.root).parts}
        dossier = self.ev / "dossier"
        before_hash, before_paths = hashes(dossier), tree(self.root)
        r = subprocess.run(cli + ["build", PROJECT], env=env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, str(dossier)), r.stderr)
        first = digest(dossier)
        self.assertEqual(hashes(dossier), before_hash)
        created = tree(self.root) - before_paths
        self.assertTrue(all(c == "test-evidence/proj/dossier" or
                            c.startswith("test-evidence/proj/dossier/") for c in created), created)
        # deterministic: same inputs, same bytes, the queue's generated_at excepted
        r = subprocess.run(cli + ["build", PROJECT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(digest(dossier), first) if NOW in (dossier / "queue.md").read_text() \
            else None
        a = fable_dossier.build(PROJECT, now=NOW)
        h1 = digest(a)
        fable_dossier.build(PROJECT, now=NOW)
        self.assertEqual(digest(a), h1)
        # check across processes
        good = self.proposal(queue)
        bad = self.proposal(queue[1:], "bad.md")
        for prop, code in ((good, 0), (bad, 1)):
            with mock.patch.dict(os.environ, {"PATH": env["PATH"]}), \
                    mock.patch("socket.socket", side_effect=RuntimeError("network")):
                inproc = fable_dossier.check(prop, PROJECT)
            r = subprocess.run(cli + ["check", str(prop), PROJECT], env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, code, r.stderr)
            self.assertEqual(json.loads(r.stdout), inproc)
        self.assertEqual(json.loads(r.stdout)["missing"], [queue[0]])
        logged = log.read_text().split()
        self.assertTrue(logged)
        self.assertEqual(set(logged), {"ls-files"})
        # in-process build with sockets forbidden and stub git on PATH
        with mock.patch.dict(os.environ, {"PATH": env["PATH"]}), \
                mock.patch("socket.socket", side_effect=RuntimeError("network")):
            fable_dossier.build(PROJECT, now=NOW)
        self.assertEqual(set(log.read_text().split()), {"ls-files"})
        self.assertEqual(hashes(dossier), before_hash)


if __name__ == "__main__":
    unittest.main()
