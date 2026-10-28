#!/usr/bin/env python3
"""Scratch-repo regression checks for coupled_tests; stdlib only."""
import contextlib
import hashlib
import io
import json
import pathlib
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


if __name__ == "__main__":
    unittest.main()
