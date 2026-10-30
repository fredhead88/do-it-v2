#!/usr/bin/env python3
"""Scratch-repo regression checks for coupled_tests; stdlib only."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import coupled_tests


ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "src/coupled_tests.py"


class CoupledTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coupled-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        for path, text in {
            "pkg/foo.py": "value = 1\n",
            "tests/test_foo.py": "# named\n",
            "tests/test_bar.py": "import pkg.foo\n",
            "tests/test_baz.py": "# unrelated\n",
            "tests/test_foobar.py": "# unrelated prefix\n",
        }.items():
            self.write(path, text)
        self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True,
            stderr=subprocess.PIPE, env=coupled_tests.merge_gate.ENV).strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")

    def run_tool(self, writes="pkg/foo.py", *, boundary="", repo=None,
                 base=None, extra=(), text=None):
        spec = self.root / "spec.md"
        if text is None:
            header = f"---\nbase_sha: {base}\n---\n" if base else ""
            text = (header + "## Interfaces\nWrites:\n" +
                    "".join(f"- {p}\n" for p in writes.splitlines()) +
                    "\n## Boundaries\n" + boundary + "\n")
        spec.write_text(text)
        return subprocess.run(
            [sys.executable, str(TOOL), str(spec), "--repo",
             str(repo or self.repo), *extra], text=True, capture_output=True)

    def report(self, case, result):
        # Bind each CLI response to its observed stdout, including empty output.
        print(json.dumps({"url": "exec:coupled_tests/" + case,
                          "status": result.returncode,
                          "body_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                          "body_excerpt": result.stdout}))

    def test_ac1_grants_and_boundaries(self):
        named = "named\tpkg/foo.py\ttests/test_foo.py"
        referencing = "referencing\tpkg/foo.py\ttests/test_bar.py"
        cases = [
            ("missing", "pkg/foo.py", "", 3, [named, referencing]),
            ("explicit", "pkg/foo.py\ntests/test_foo.py", "", 0, [referencing]),
            ("glob", "pkg/foo.py\ntests/test_*.py", "", 0, []),
            ("directory", "pkg/foo.py\ntests/", "", 0, []),
            ("fenced", "pkg/foo.py", "`tests/test_foo.py`: cannot break.",
             0, [named + "\tfenced", referencing]),
            ("basename-only", "pkg/foo.py", "test_foo.py", 3, [named, referencing]),
            ("outside-boundaries", "pkg/foo.py",
             "No exclusions.\n## Other\ntests/test_foo.py", 3, [named, referencing]),
        ]
        for label, writes, boundary, code, lines in cases:
            with self.subTest(label=label):
                result = self.run_tool(writes, boundary=boundary)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout.splitlines(), lines)
                self.report("AC1/" + label, result)
        print("L-spec-1795 AC1 ok")

    def test_ac2_original_1786_writes(self):
        if not (ROOT / ".git").exists():
            print("L-spec-1795 AC2 skipped: no checkout")
            return
        original_writes = ("- src/dispatch.py\n"
                           "- src/ledger_cache.py\n"
                           "- src/test_ledger_scan_cost.py\n")
        fixture = "---\nbase_sha: 19fc884\n---\nWrites:\n" + original_writes
        result = self.run_tool(repo=ROOT, text=fixture)
        line = "named\tsrc/ledger_cache.py\tsrc/test_ledger_cache.py"
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(line, result.stdout.splitlines())
        self.report("AC2/original-writes", result)
        print(line)
        print("L-spec-1795 AC2 ok")

    def test_ac3_bad_input(self):
        nongit = self.root / "nongit"
        nongit.mkdir()
        cases = [
            ("no-writes", {"text": "# No grant\n"}),
            ("prose-writes", {"text": "Writes: see the table below\n"}),
            ("nongit", {"repo": nongit}),
            ("too-many", {"writes": "\n".join(f"pkg/f{i}.py" for i in range(41))}),
            ("bad-rev", {"extra": ("--rev", "missing-revision")}),
        ]
        for label, kwargs in cases:
            with self.subTest(label=label):
                result = self.run_tool(**kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                if label == "too-many":
                    self.assertEqual(result.stdout.splitlines(), ["truncated"])
                self.report("AC3/" + label, result)
        result = subprocess.run([sys.executable, str(TOOL)], capture_output=True)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run([sys.executable, str(TOOL), str(self.root / "missing"),
                                 "--repo", str(self.repo)], capture_output=True)
        self.assertEqual(result.returncode, 2)
        print("L-spec-1795 AC3 ok")

    def test_test_shapes_and_named_deduplication(self):
        for path, text in {
            "pkg/widget.tsx": "export const widget = 1;\n",
            "web/widget.test.tsx": "import widget from '../pkg/widget';\n",
            "test/consumer.js": "import widget from '../pkg/widget';\n",
            "tests/test_foo_extra.py": "import pkg.foo\n",
            "pkg/test_foo.py": "import pkg.foo\n",
            "test/consumer.py": "import pkg.foo\n",
            "pkg/consumer.py": "import pkg.foo\n",
        }.items():
            self.write(path, text)
        self.commit()
        result = self.run_tool("pkg/foo.py\npkg/widget.tsx")
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("named\tpkg/widget.tsx\tweb/widget.test.tsx", lines)
        self.assertIn("referencing\tpkg/widget.tsx\ttest/consumer.js", lines)
        self.assertIn("named\tpkg/foo.py\ttests/test_foo_extra.py", lines)
        self.assertIn("named\tpkg/foo.py\tpkg/test_foo.py", lines)
        self.assertIn("referencing\tpkg/foo.py\ttest/consumer.py", lines)
        self.assertEqual(len(lines), 7)

    def test_revision_pinning_and_read_only(self):
        base = self.git("rev-parse", "HEAD")
        self.write("tests/test_foo_extra.py", "import pkg.foo\n")
        self.commit()
        # An uncommitted referencing edit must not affect either revision.
        self.write("tests/test_baz.py", "import pkg.foo\n")
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        pinned = self.run_tool(base=base)
        head = self.run_tool(base=base, extra=("--rev", "HEAD"))
        self.assertEqual(pinned.returncode, 3, pinned.stderr)
        self.assertEqual(head.returncode, 3, head.stderr)
        self.assertEqual(len(pinned.stdout.splitlines()), 2)
        self.assertEqual(len(head.stdout.splitlines()), 3)
        self.assertNotIn("test_baz.py", head.stdout)
        self.assertIn("test_foo_extra.py", head.stdout)
        self.assertEqual(before, (self.git("status", "--porcelain"),
                                  self.git("rev-parse", "HEAD")))

    def test_expanded_grants_and_limit(self):
        self.assertEqual(len(self.run_tool("pkg/").stdout.splitlines()), 2)
        self.assertEqual(len(self.run_tool("pkg/*.py").stdout.splitlines()), 2)
        result = self.run_tool("\n".join(f"pkg/f{i}.py" for i in range(40)))
        self.assertEqual(result.returncode, 0, result.stderr)
        for i in range(41):
            self.write(f"wide/f{i}.py", "# fixture\n")
        self.commit()
        result = self.run_tool("wide/")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.splitlines(), ["truncated"])

    def test_total_grep_timeout(self):
        self.run_tool("pkg/foo.py\npkg/second.py")  # Create the fixture spec.
        real_run = coupled_tests.subprocess.run
        budgets = []

        def git_run(args, **kwargs):
            if "grep" in args:
                budgets.append(kwargs["timeout"])
                if len(budgets) == 2:
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return real_run(args, **kwargs)

        output = io.StringIO()
        with mock.patch.object(coupled_tests.subprocess, "run", side_effect=git_run), \
                mock.patch.object(coupled_tests.time, "monotonic", side_effect=[100, 100, 121]):
            with contextlib.redirect_stdout(output):
                code = coupled_tests.main([str(self.root / "spec.md"),
                                           "--repo", str(self.repo)])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue().splitlines(), ["truncated"])
        self.assertEqual(budgets, [60, 39])

    def attest(self, criterion, body):
        self.report("1891/" + criterion, subprocess.CompletedProcess([], 0, json.dumps(body)))

    def test_packet_lines(self):
        text = "Writes:\n- pkg/foo.py\n\n## Boundaries\n"
        lines = coupled_tests.packet_lines(text, self.repo, "HEAD")
        block = "\n".join(lines)
        self.assertLess(block.index("tests/test_foo.py"), block.index("tests/test_bar.py"))
        self.assertIn("-m pytest -q -p no:cacheprovider tests/test_foo.py", block)
        cli = shlex.split(coupled_tests.run_line("a spec.md", "a worktree", "HEAD")[8:])
        self.assertEqual(cli[-6:], ["a spec.md", "--repo", "a worktree", "--run", "--base", "HEAD"])
        fenced = coupled_tests.packet_lines(text + "tests/test_foo.py\n", self.repo, "HEAD")
        self.assertNotIn("tests/test_foo.py", "\n".join(fenced))
        self.assertIn("1 fenced by Boundaries", fenced[0])
        for error in (coupled_tests.Truncated(), ValueError("bad revision")):
            with mock.patch.object(coupled_tests, "scan", side_effect=error):
                result = coupled_tests.packet_lines(text, self.repo, "HEAD")
            self.assertEqual(len(result), 1)
            self.assertIn("coupled tests: undetermined (", result[0])
        with mock.patch.object(coupled_tests, "scan", return_value=[
                ("referencing", "a.py", f"tests/test_{i}.py", False) for i in range(45)]):
            capped = coupled_tests.packet_lines(text, self.repo, "HEAD")
            self.assertEqual(len(capped), 31)
            self.assertIn("15 omitted by the 30-file cap", capped[0])
        self.attest("AC1", lines)

    def test_run_classifies(self):
        # The runner identifies the side from fixture data, never from the
        # checkout spelling (TMPDIR or the repo argument may be symlinked).
        self.write("side", "base")
        self.commit()
        base = self.git("rev-parse", "HEAD")
        self.write("side", "branch")
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            file = cmd[-1]
            side = (pathlib.Path(kw["cwd"]) / "side").read_text()
            self.assertIn(side, ("branch", "base"))
            branch = side == "branch"
            if branch and file == "tests/timeout.py":
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            code = int(file == "tests/red.py" or (branch and file == "tests/regressed.py"))
            return subprocess.CompletedProcess(cmd, code)

        files = ["tests/pass.py", "tests/regressed.py", "tests/red.py", "tests/timeout.py",
                 "web/foo.test.ts", "web/foo.test.tsx"]
        alias = self.root / "repo-alias"
        alias.symlink_to(self.repo.resolve(), target_is_directory=True)
        expected = ["pass", "regressed", "base-red", "regressed", "skipped", "skipped"]
        with mock.patch.dict(coupled_tests.merge_gate.ENV, {"SUPABASE_DB_URL_TEST": "sentinel"}):
            for repo in (self.repo, alias):
                with self.subTest(repo=repo.name):
                    rows = coupled_tests.run(files, repo, base, runner=runner)
                    self.assertEqual([r["status"] for r in rows], expected)
        self.assertEqual(rows[0]["base"], "not-run")
        self.assertEqual(rows[3]["branch"], "timeout")
        self.assertEqual(rows[3]["base"], "pass")
        self.assertEqual(rows[4]["reason"], "node-test")
        for cmd, kw in calls:
            self.assertIn("pytest", cmd)
            self.assertLessEqual(kw["timeout"], 300)
            self.assertFalse(any(k.startswith("SUPABASE_DB_URL") for k in kw["env"]))
        self.write("doit", "#!/bin/sh\n")
        (self.repo / "doit").chmod(0o755)
        calls.clear()
        coupled_tests.run(["tests/pass.py"], self.repo, base, runner=runner)
        self.assertEqual(calls[0][0], ["/usr/bin/python3", "tests/pass.py"])
        def timeout_runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        timed = coupled_tests.run(["tests/timeout.py"], self.repo, base, runner=timeout_runner)
        self.assertEqual((timed[0]["branch"], timed[0]["base"], timed[0]["status"]),
                         ("timeout", "fail", "base-red"))
        self.attest("AC2", rows)

    def test_base_worktree_cleaned(self):
        before = self.git("show-ref", "--head"), self.git("rev-parse", "HEAD")
        scratch_paths = []
        self.write(".env", "scratch-sentinel\n")
        scratch_root = self.root / "scratch"
        scratch_root.mkdir()

        def runner(cmd, **kw):
            path = pathlib.Path(kw["cwd"]).resolve()
            if path != self.repo.resolve():
                scratch_paths.append(path)
                self.assertTrue(path.is_relative_to(scratch_root.resolve()))
                self.assertFalse((path / ".env").exists())
                self.assertEqual(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"],
                                               cwd=path, capture_output=True).returncode, 1)
                raise RuntimeError("injected runner failure")
            return subprocess.CompletedProcess(cmd, 1)

        with mock.patch.dict(os.environ, {"TMPDIR": str(scratch_root)}):
            with self.assertRaisesRegex(RuntimeError, "injected runner failure"):
                coupled_tests.run(["tests/test_foo.py"], self.repo, "HEAD", runner=runner)
        self.assertTrue(scratch_paths)
        self.assertTrue(all(not p.exists() for p in scratch_paths))
        self.assertEqual(before, (self.git("show-ref", "--head"), self.git("rev-parse", "HEAD")))
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")), 2)
        self.attest("AC3-cleanup", {"removed": True, "refs_unchanged": True})

    def test_cli_run_exit_codes(self):
        self.write("doit", "#!/bin/sh\n")
        (self.repo / "doit").chmod(0o755)
        self.commit()
        self.run_tool()  # Create the spec, with both coupled test files.
        argv = [str(self.root / "spec.md"), "--repo", str(self.repo), "--run", "--base", "HEAD"]
        cases = [("regressed", 4), ("base-red", 0), ("pass", 0), ("skipped", 0)]
        for status, code in cases:
            rows = [{"file": "tests/test_foo.py", "branch": "pass", "base": "not-run",
                     "status": status}]
            output = io.StringIO()
            with mock.patch.object(coupled_tests, "run", return_value=rows), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(coupled_tests.main(argv), code)
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], rows)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(coupled_tests.main(argv[:-2]), 2)
            self.assertEqual(coupled_tests.main(argv + ["--timeout", "0"]), 2)
        self.attest("AC3-cli", cases)

    def test_run_total_budget(self):
        clock = [100.0]
        budgets = []

        def runner(cmd, **kw):
            budgets.append(kw["timeout"])
            clock[0] += kw["timeout"]
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(coupled_tests.time, "monotonic", side_effect=lambda: clock[0]):
            rows = coupled_tests.run([f"tests/test_{i}.py" for i in range(6)],
                                     self.repo, "HEAD", runner=runner)
        self.assertEqual(budgets, [300] * 4)
        self.assertEqual([row["status"] for row in rows], ["pass"] * 4 + ["skipped"] * 2)
        self.assertEqual(rows[-1]["reason"], "total-timeout")

    def card(self):
        return {"status": "DONE", "identity": {"spec_id": "L-spec-8001", "built_by": "test",
                "branch": "fixture", "base_sha": "a" * 40, "ready_sha": "b" * 40},
                "acs": [{"id": "AC1", "criterion_type": "backend", "evidence": "fixture",
                         "evidence_type": "command-output", "check": "true", "disposition": "done"}],
                "verify": {"command": "true", "exit_code": 0, "result": "ok"},
                "stubs": [], "deviations": [], "tests": {"added": True}, "not_built": [],
                "unknowns": [], "built_against": [], "escalations": [], "declarations": []}

    def test_schema_coupled(self):
        import jsonschema
        schema = json.loads((ROOT / "agents/builder.schema.json").read_text())
        card = self.card()
        jsonschema.validate(card, schema)
        card["coupled"] = [{"file": "tests/test_foo.py", "branch": "pass", "base": "not-run",
                            "status": "pass"}]
        jsonschema.validate(card, schema)
        card["coupled"][0]["status"] = "maybe"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(card, schema)
        self.attest("AC4-schema", {"optional": True, "valid": True, "invalid_rejected": True})

    def test_write_card_sidecar_coupled(self):
        import dispatch
        card = self.card()
        card["coupled"] = [{"file": "tests/test_foo.py", "branch": "pass", "base": "not-run",
                            "status": "pass"}]
        with mock.patch.object(dispatch, "CONTENT", self.root):
            path = dispatch.write_card(card, "L-spec-8001", "test")
        self.assertEqual(json.loads(path.with_suffix(".json").read_text())["coupled"], card["coupled"])
        self.attest("AC4-sidecar", card["coupled"])

    def test_validate_card(self):
        card = self.card()
        self.assertIsNone(coupled_tests.validate_card(card))
        card["coupled"] = [{"file": "tests/test_foo.py", "status": "regressed"}]
        self.assertIn("tests/test_foo.py", coupled_tests.validate_card(card))
        for status in ("NEEDS_CONTEXT", "BLOCKED"):
            card["status"] = status
            self.assertIsNotNone(coupled_tests.validate_card(card))
            card["escalations"] = [{"asks": "Permit tests/test_foo.py"}]
            self.assertIsNone(coupled_tests.validate_card(card))
            card["escalations"] = []
        card["status"] = "DONE"
        card["coupled"] = [{"file": "tests/a.py", "status": s} for s in ("pass", "base-red", "skipped")]
        self.assertIsNone(coupled_tests.validate_card(card))
        self.attest("AC5", {"done_rejected": True, "escalation_required": True, "clean_accepted": True})

    def test_grader_coupled_item(self):
        rows = [{"file": "tests/test_foo.py", "status": "pass", "branch": "pass", "base": "not-run"},
                {"file": "tests/test_bar.py", "status": "base-red", "base_sha": "a" * 40}]
        line = coupled_tests.grader_line({"coupled": rows, "spawn": "L-builder-9999"})
        self.assertTrue(line.startswith("4a. Coupled existing tests"))
        self.assertIn("tests/test_foo.py pass", line)
        self.assertIn("tests/test_bar.py base-red", line)
        self.assertNotRegex(line, r"[0-9a-f]{7,}|L-builder-\d+")
        self.assertIsNone(coupled_tests.grader_line({}))
        self.attest("AC6", line)


if __name__ == "__main__":
    unittest.main()
