#!/usr/bin/env python3
"""Fixture proofs for the read-only merge CI report; no live GitHub call."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

# Import only after isolating the root; no production ledger is consulted.
_imports = tempfile.TemporaryDirectory(prefix="merge-ci-import-")
with patch.dict(os.environ, {"DOIT_ROOT": _imports.name}):
    import merge_ci_report as mcr

SHA = "a" * 40
BASE_T = "2026-11-10T09:{:02d}:{:02d}Z"
LINE = "CI-SELECTION mode=selected reason=impacted files=365"


def stamp(seconds):
    return BASE_T.format(seconds // 60 % 60, seconds % 60)


def job(start, length, labels=("ubuntu-latest",), done=True):
    return {"started_at": stamp(start), "completed_at": stamp(start + length) if done else None,
            "labels": list(labels)}


class Fixture:
    """Path-aware stand-in for the GitHub transport; records every argv."""

    def __init__(self, runs=25, jobs=None, failures=None):
        self.argvs = []
        self.failures = failures or {}
        self.runs = [{"id": 1000 + i, "head_sha": f"{i:040x}", "event": "push",
                      "conclusion": "success", "status": "completed",
                      "created_at": f"2026-11-{1 + i // 3:02d}T0{i % 3}:00:00Z"} for i in range(runs)]
        self.jobs = jobs or (lambda rid: [job(0, 600), job(100, 700)])

    def __call__(self, argv):
        self.argvs.append(list(argv))
        assert argv[:2] == ["gh", "api"], argv
        path = argv[2]
        if "workflows/python-tests.yml/runs" in path:
            return 0, json.dumps({"workflow_runs": self.runs}), ""
        m = re.fullmatch(r"repos/o/r/actions/runs/(\d+)/jobs(\?.*)?", path)
        if m:
            if m[1] in self.failures:
                return 1, "", self.failures[m[1]]
            return 0, json.dumps({"jobs": self.jobs(m[1])}), ""
        raise AssertionError(f"unknown path {path}")


def logs_ok(owner, rid):
    return [("1_setup.txt", "nothing here"), ("2_report.txt", f"2026-11-10T09:00:00.1234567Z {LINE}")]


class TestRows(unittest.TestCase):
    KEYS = {"run_id", "sha", "event", "conclusion", "mode", "reason", "files", "wall_seconds",
            "billed_minutes", "billed_status"}

    def test_exactly_n_newest_rows_with_keys(self):
        fx = Fixture(25)
        rep = mcr.report("p", 20, fetch=fx, logs=logs_ok, owner="o/r")
        self.assertEqual(len(rep["runs"]), 20)
        newest = sorted(fx.runs, key=lambda r: r["created_at"], reverse=True)[:20]
        self.assertEqual([r["run_id"] for r in rep["runs"]], [str(r["id"]) for r in newest])
        for row in rep["runs"]:
            self.assertEqual(set(row), self.KEYS)

    def test_n_three_and_bad_n(self):
        rep = mcr.report("p", 3, fetch=Fixture(25), logs=logs_ok, owner="o/r")
        self.assertEqual(len(rep["runs"]), 3)
        for bad in (0, -1, "3", 2.0, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mcr.report("p", bad, fetch=Fixture(), logs=logs_ok, owner="o/r")

    def test_wrong_path_fails_fixture(self):
        def wrong(argv):
            if not argv:
                raise AssertionError('x')
            return Fixture()(["gh", "api", "repos/o/r/actions/runs?per_page=5"])
        with self.assertRaises(AssertionError):
            wrong([])
        rep = mcr.report("p", 3, fetch=wrong, logs=logs_ok, owner="o/r")
        self.assertEqual(rep["runs"], [])  # the unknown path is never silently answered
        self.assertTrue(rep["undetermined"])

    def test_listing_failure_is_undetermined(self):
        rep = mcr.report("p", 3, fetch=lambda argv: (1, "", "gh: HTTP 403"), logs=logs_ok, owner="o/r")
        self.assertEqual(rep["runs"], [])
        self.assertTrue(any("HTTP 403" in u for u in rep["undetermined"]))
        self.assertIsNone(rep["billed"]["minutes"])
        self.assertEqual(rep["canary"]["status"], "undetermined")


class TestSelectionLine(unittest.TestCase):
    def parse(self, *texts):
        return mcr.parse_selection([(f"{i}.txt", t) for i, t in enumerate(texts)])

    def test_valid_lines(self):
        self.assertEqual(self.parse(f"2026-11-10T09:00:00.1234567Z {LINE}")[:3], ("selected", "impacted", 365))
        ansi = "2026-11-10T09:00:00.1234567Z \x1b[36;1mCI-SELECTION mode=full reason=full-path files=2585\x1b[0m"
        self.assertEqual(self.parse(ansi)[:3], ("full", "full-path", 2585))
        self.assertEqual(self.parse(LINE)[:3], ("selected", "impacted", 365))
        self.assertEqual(self.parse("CI-SELECTION mode=rerun reason=failed-only files=4")[:3],
                         ("rerun", "failed-only", 4))

    def test_line_only_in_later_member(self):
        self.assertEqual(self.parse("setup only", f"x\t{LINE}")[0], "selected")

    def test_bad_cases_are_undetermined(self):
        for texts in (("nothing",), ("CI-SELECTION mode=selected reason=x files=abc",),
                      ("CI-SELECTION mode=weird reason=x files=1",),
                      (LINE, "CI-SELECTION mode=full reason=full-path files=9"),
                      ("echo CI-SELECTION mode=full reason=x files=1 trailing",)):
            mode, reason, files, why = self.parse(*texts)
            self.assertEqual((mode, files), ("undetermined", None), texts)
            self.assertTrue(reason and why)

    def test_unreadable_log_is_undetermined_in_report(self):
        def boom(owner, rid):
            raise ValueError("GitHub logs unavailable")
        rep = mcr.report("p", 2, fetch=Fixture(3), logs=boom, owner="o/r")
        self.assertTrue(all(r["mode"] == "undetermined" and r["files"] is None for r in rep["runs"]))
        self.assertTrue(any("selection mode undetermined" in u for u in rep["undetermined"]))


class TestSummary(unittest.TestCase):
    def run_with(self, lengths):
        fx = Fixture(len(lengths), jobs=lambda rid: (
            [] if lengths[int(rid) - 1000] is None else [job(0, lengths[int(rid) - 1000])]))
        return mcr.report("p", len(lengths), fetch=fx, logs=logs_ok, owner="o/r")

    def test_odd(self):
        s = self.run_with([600, 900, 1200, 1800, 3000])["summary"]
        self.assertEqual((s["median_wall_seconds"], s["delta_vs_baseline_seconds"]), (1200, 1200 - 29.2 * 60))
        self.assertAlmostEqual(s["delta_vs_baseline_seconds"], -552)

    def test_even_excluded_and_modes(self):
        rep = self.run_with([600, 1200, None, 1800, 3000])
        s = rep["summary"]
        self.assertEqual(s["median_wall_seconds"], 1500)
        self.assertEqual((s["runs_counted"], s["runs_excluded"]), (4, 1))
        tally = {}
        for row in rep["runs"]:
            tally[row["mode"]] = tally.get(row["mode"], 0) + 1
        self.assertEqual(s["mode_counts"], tally)

    def test_empty_is_undetermined_not_zero(self):
        rep = self.run_with([None, None])
        s = rep["summary"]
        self.assertIsNone(s["median_wall_seconds"])
        self.assertIsNone(s["delta_vs_baseline_seconds"])
        self.assertTrue(any("median" in u for u in rep["undetermined"]))
        self.assertEqual(rep["baseline_minutes"], 29.2)


class TestCanary(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="merge-ci-canary-")
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "events").mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(mcr.guards_live.fold, "ROOT", self.root))
        self.fx = Fixture(5)

    def write(self, rows):
        text = "".join(json.dumps(r) + "\n" for r in rows)
        (self.root / "events" / "L-base-red-local.jsonl").write_text(text)

    def row(self, i, **extra):
        r = {"ts": f"2026-11-10T10:00:0{i}Z", "type": "selected-canary", "project": "p", "schema": 2,
             "sha": f"{i:040x}", "run_id": str(1000 + i), "full_run_id": "9", "ids": [],
             "selected": [], "full_failures": [], "green": True, "complete": True}
        r.update(extra)
        return r

    def canary(self):
        return mcr.report("p", 5, fetch=self.fx, logs=logs_ok, owner="o/r")["canary"]

    def test_counts_and_miss(self):
        self.write([self.row(0), self.row(1), self.row(2), self.row(3, ids=["t::x"]),
                    self.row(4, schema=1), self.row(1, project="other"),
                    self.row(1, run_id="555", sha="b" * 40)])
        c = self.canary()
        self.assertEqual((c["status"], c["count"], c["misses"], c["undetermined"]), ("recorded", 4, 1, 0))
        self.assertEqual(c["miss_rows"], [{"sha": f"{3:040x}", "ids": ["t::x"]}])
        # one row dropped the schema field entirely
        rows = [self.row(0)]
        del rows[0]["schema"]
        self.write(rows)
        self.assertEqual(self.canary()["count"], 0)

    def test_incomplete_is_undetermined(self):
        self.write([self.row(0), self.row(1, complete=False)])
        c = self.canary()
        self.assertEqual((c["status"], c["count"], c["undetermined"]), ("undetermined", 1, 1))

    def test_corrupt_ledger_is_undetermined(self):
        (self.root / "events" / "L-base-red-local.jsonl").write_text("{not json\n")
        c = self.canary()
        self.assertEqual((c["status"], c["count"], c["misses"]), ("undetermined", None, None))
        self.assertTrue(c["reason"])

    def test_missing_ledger_dir_is_undetermined(self):
        with patch.object(mcr.guards_live.fold, "ROOT", self.root / "absent"):
            self.assertEqual(self.canary()["status"], "undetermined")

    def test_empty_ledger(self):
        c = self.canary()
        self.assertEqual((c["status"], c["count"]), ("none-recorded", 0))


class TestBilled(unittest.TestCase):
    def one(self, jobs, failures=None):
        fx = Fixture(1, jobs=lambda rid: jobs, failures=failures)
        return mcr.report("p", 1, fetch=fx, logs=logs_ok, owner="o/r")

    def test_hosted_ceil_rule(self):
        jobs = [job(0, 61), job(0, 60), job(0, 1)]
        rep = self.one(jobs)
        self.assertEqual((rep["runs"][0]["billed_minutes"], rep["runs"][0]["billed_status"]), (4, "billed"))
        self.assertEqual((rep["billed"]["status"], rep["billed"]["minutes"]), ("billed", 4))

    def test_not_github_billed(self):
        for labels in (["self-hosted", "linux", "as-ci"], ["ubicloud-standard-2"]):
            rep = self.one([job(0, 61, labels)])
            self.assertEqual((rep["runs"][0]["billed_minutes"], rep["runs"][0]["billed_status"]),
                             (0, "not-github-billed"))
            self.assertEqual(rep["billed"]["minutes"], 0)

    def test_unreadable_is_never_zero(self):
        cases = [self.one([job(0, 61, ["mystery"])]), self.one([job(0, 61, done=False)]),
                 self.one([], None), self.one([job(0, 5)], {"1000": "gh: HTTP 403"}),
                 self.one([job(0, 5)], {"1000": "gh: HTTP 404 Not Found"})]
        for rep in cases:
            self.assertIsNone(rep["runs"][0]["billed_minutes"])
            self.assertEqual(rep["runs"][0]["billed_status"], "undetermined")
            self.assertEqual(rep["billed"]["status"], "undetermined")
            self.assertIsNone(rep["billed"]["minutes"])
        self.assertIn("HTTP 403", cases[3]["billed"]["reason"])
        self.assertIn("HTTP 404", cases[4]["billed"]["reason"])

    def test_partial_total_not_complete(self):
        fx = Fixture(2, failures={"1000": "HTTP 403"})
        rep = mcr.report("p", 2, fetch=fx, logs=logs_ok, owner="o/r")
        known = [r for r in rep["runs"] if r["billed_status"] != "undetermined"]
        self.assertEqual(len(known), 1)
        self.assertGreaterEqual(known[0]["billed_minutes"], 0)
        self.assertIsNone(rep["billed"]["minutes"])


class TestCli(unittest.TestCase):
    def run_main(self, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mcr.main(argv, **kw)
        return code, out.getvalue(), err.getvalue()

    def seams(self, **kw):
        return dict(fetch=Fixture(4, **kw), logs=logs_ok, owner="o/r")

    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="merge-ci-cli-")
        self.addCleanup(t.cleanup)
        (Path(t.name) / "events").mkdir()
        s = contextlib.ExitStack()
        self.addCleanup(s.close)
        s.enter_context(patch.object(mcr.guards_live.fold, "ROOT", Path(t.name)))
        s.enter_context(patch.object(mcr.base_red.fold, "ROOT", Path(t.name)))

    def test_text_mode(self):
        code, out, _ = self.run_main(["p", "--n", "3"], **self.seams())
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(sum("mode=selected reason=impacted files=365" in l for l in lines), 3)
        self.assertTrue(any(l.startswith("median wall:") and "29.2" in l for l in lines))
        self.assertTrue(any(l.startswith("canary:") for l in lines))
        self.assertTrue(any(l.startswith("billed:") for l in lines))

    def test_json_equals_report_and_exit_zero(self):
        code, out, _ = self.run_main(["p", "--n", "2", "--json"], **self.seams())
        want = mcr.report("p", 2, **self.seams())
        self.assertEqual(json.loads(out), want)
        self.assertEqual(code, 0 if not want["undetermined"] else 3)

    def test_undetermined_exits_3_with_output(self):
        code, out, _ = self.run_main(["p", "--n", "2"], **self.seams(failures={str(1000 + i): "HTTP 403" for i in range(4)}))
        self.assertEqual(code, 3)
        self.assertIn("undetermined", out)

    def test_usage_errors_exit_2(self):
        for argv in (["p", "--n", "0"], ["p", "--bogus"], ["p", "--n", "x"], []):
            code, out, err = self.run_main(argv, **self.seams())
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertTrue(err.strip())

    def test_unknown_project_exits_2(self):
        code, _, err = self.run_main(["nonexistent"], fetch=Fixture(), logs=logs_ok)
        self.assertEqual(code, 2)
        self.assertIn("unknown project", err)


class TestReadOnly(unittest.TestCase):
    FORBIDDEN = {"rerun", "cancel", "delete", "dispatch", "run", "workflow", "-X", "-f", "-F", "--method"}

    def snapshot(self, root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
                for p in sorted(root.rglob("*"))}

    def test_get_only_and_nothing_written(self):
        t = tempfile.TemporaryDirectory(prefix="merge-ci-ro-")
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        (root / "events").mkdir()
        (root / "events" / "L-base-red-local.jsonl").write_text(json.dumps(
            {"ts": "2026-11-10T10:00:00Z", "type": "selected-canary", "project": "p", "schema": 2,
             "sha": f"{0:040x}", "run_id": "1000", "ids": [], "complete": True}) + "\n")
        cwd = Path.cwd()
        before, cwd_before = self.snapshot(root), sorted(os.listdir(cwd))
        fx = Fixture(4)
        with patch.object(mcr.guards_live.fold, "ROOT", root):
            mcr.report("p", 3, fetch=fx, logs=logs_ok, owner="o/r")
            with contextlib.redirect_stdout(io.StringIO()):
                mcr.main(["p", "--n", "2", "--json"], fetch=fx, logs=logs_ok, owner="o/r")
        self.assertTrue(fx.argvs)
        for argv in fx.argvs:
            self.assertEqual(argv[:2], ["gh", "api"])
            self.assertEqual(len(argv), 3)
            self.assertFalse(self.FORBIDDEN & set(argv), argv)
            self.assertFalse(re.search(r"rerun|cancel|delete|dispatch", argv[2]), argv)
        self.assertEqual(self.snapshot(root), before)
        self.assertEqual(sorted(os.listdir(cwd)), cwd_before)

    def test_source_has_no_append_helper(self):
        source = Path(mcr.__file__).read_text()
        for needle in ("_append", "record(", "doit append", "base_red.record", "fold.append"):
            self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
