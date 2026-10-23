#!/usr/bin/env python3
"""Retirement registry regressions; fixtures never change a live checkout."""
import dataclasses
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retired

REPO = Path(__file__).resolve().parent.parent


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="retired-test-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.root = self.repo / "retired.d"
        self.root.mkdir()
        self.crontab = self.repo / "crontab.txt"
        self.crontab.write_text("")

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        subprocess.run(["git", "-C", str(target.parent), "add", "--", target.name], check=True)
        return target

    def row(self, **kwargs):
        return retired.Retired(**dict(dict(name="old", kind="code", pattern="old()",
                                          paths=["src/**/*.py"]), **kwargs))

    def check(self, row, crontab_text=""):
        return retired.check([row], repo=str(self.repo), crontab_text=crontab_text)

    def manifest(self, text=None):
        self.write("retired.d/unit.toml", text or
                   '[[retired]]\nname="old"\nkind="code"\npattern="old()"\npaths=["src/**/*.py"]\n')

    def cli(self):
        return subprocess.run([str(REPO / "doit"), "retired", "check", "--root", str(self.root),
                               "--repo", str(self.repo), "--crontab-file", str(self.crontab)],
                              capture_output=True, text=True)

    def test_ac1_sorted_files_multiple_rows(self):
        row = '[[retired]]\nname="{}"\nkind="code"\npattern="old"\npaths=["src/*.py"]\n'
        self.write("retired.d/z.toml", row.format("z"))
        self.write("retired.d/a.toml", row.format("a") + row.format("b"))
        rows = retired.load(str(self.root))
        self.assertEqual([r.name for r in rows], ["a", "b", "z"])
        self.assertFalse(rows[0].regex)
        self.assertIsNone(rows[0].repo)

    def test_ac1_malformed_rows(self):
        good = dict(name="old", kind="code", pattern="old", paths=["src/*.py"])
        bads = [{k: v for k, v in good.items() if k != field} for field in good]
        bads += [dict(good, **change) for change in [dict(kind="other"), dict(pattern=""),
                 dict(paths=[]), dict(paths=["/etc/*"]), dict(paths=["src/../*.py"]),
                 dict(paths="src/*"), dict(regex="true"), dict(repo="relative"),
                 dict(provisional="true"),
                 dict(armed_when_path="marker"), dict(armed_when_path="../marker", armed_when_contains="x")]]
        for bad in bads:
            with self.subTest(bad=bad):
                # JSON values have compatible TOML syntax for this fixture's types.
                import json
                text = '[[retired]]\n' + '\n'.join(f'{k}={json.dumps(v)}' for k, v in bad.items())
                self.manifest(text)
                with self.assertRaisesRegex(retired.RetiredError, r"unit.toml.*row 1"):
                    retired.load(str(self.root))
        for text in ['[', 'retired="bad"', '[[retired]]\nname=1']:
            self.manifest(text)
            with self.assertRaisesRegex(retired.RetiredError, "unit.toml"):
                retired.load(str(self.root))
            self.assertEqual(self.cli().returncode, 2)

    def test_ac1_missing_empty_and_checkout_relative_root(self):
        with self.assertRaises(retired.RetiredError):
            retired.load(str(self.repo / "missing"))
        with self.assertRaises(retired.RetiredError):
            retired.load(str(self.root))
        self.assertEqual(self.cli().returncode, 2)
        self.root.rmdir()
        self.assertEqual(self.cli().returncode, 2)
        prior = Path.cwd()
        try:
            os.chdir(self.repo)
            self.assertEqual(retired.load(), retired.load(str(REPO / "retired.d")))
        finally:
            os.chdir(prior)

    def test_ac2_present_removed_and_literal_regex(self):
        path = self.write("src/nested/a.py", "old()\nold()\n")
        hits = self.check(self.row())
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].name, hits[0].kind, hits[0].repo, hits[0].where, hits[0].reason),
                         ("old", "code", str(self.repo), str(path), "present"))
        path.write_text("oldXX\n")
        self.assertEqual(self.check(self.row()), [])
        self.assertEqual(len(self.check(self.row(pattern=r"old.+", regex=True))), 1)
        self.assertEqual(self.check(self.row(pattern=r"old.+")), [])
        path.write_text("")
        self.assertEqual(self.check(self.row(regex=True)), [])
        untracked = self.repo / "src/nested/untracked.py"
        untracked.write_text("old()")
        self.assertEqual(self.check(self.row()), [])
        self.write("src/direct.py", "old()")
        self.assertEqual(len(self.check(self.row())), 1)
        self.assertEqual(self.check(self.row(paths=["src/*.py"]))[0].where,
                         str(self.repo / "src/direct.py"))

    def test_ac2_foreign_repo_and_never_self_match(self):
        foreign = self.repo / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        target = self.write("foreign/src/a.py", "old()")
        subprocess.run(["git", "-C", str(foreign), "add", "src/a.py"], check=True)
        self.assertEqual(self.check(self.row(repo=str(foreign)))[0].where, str(target))
        target.unlink()
        for path in ["retired.d/self.toml", "src/retired.py", "src/test_retired.py"]:
            self.write(path, "old()")
        self.assertEqual(self.check(self.row(paths=["**/*", "**"])), [])

    def test_ac3_cron_and_doc_scopes(self):
        cron = self.row(kind="cron", pattern="old-job", paths=[])
        self.assertEqual(self.check(cron, "  # old-job\n"), [])
        self.assertEqual(self.check(cron, "* * * * * old-job")[0].where, "crontab")
        manifest = self.write("crons.toml", " # old-job\n")
        self.assertEqual(self.check(cron), [])
        manifest.write_text('command="old-job"\n')
        self.assertEqual(self.check(cron)[0].where, str(manifest))
        doc = self.row(kind="doc", pattern="old-doc", paths=[])
        for path in ["src/a.py", "retired.d/row.toml", "CHANGELOG.md", "design/a.md",
                     "docs/handoffs/history.md", "docs/handoffs/nested/history.md"]:
            self.write(path, "old-doc")
        self.assertEqual(self.check(doc), [])
        expected = [self.write(p, "old-doc") for p in ["docs/nested/a.md", "agents/a.md", "README.md"]]
        self.assertEqual({h.where for h in self.check(doc)}, {str(p) for p in expected})

    def test_ac4_undetermined_and_cli_status(self):
        self.manifest()
        self.assertEqual(self.cli().returncode, 0)
        target = self.write("src/a.py", "old()")
        self.assertEqual(self.cli().returncode, 1)
        cases = [self.row(regex=True, pattern="["), self.row(repo=str(self.repo / "missing"))]
        for row in cases:
            self.assertEqual(self.check(row)[0].reason, "undetermined")
        self.assertEqual(self.check(self.row(kind="cron", paths=[]), None)[0].reason, "undetermined")
        target.chmod(0)
        self.addCleanup(target.chmod, 0o600)
        self.assertEqual(self.check(self.row())[0].reason, "undetermined")
        self.assertEqual(self.cli().returncode, 2)
        target.chmod(0o600)
        inaccessible = self.repo / "inaccessible"
        inaccessible.mkdir()
        inaccessible.chmod(0)
        try:
            self.assertEqual(self.check(self.row(repo=str(inaccessible)))[0].reason, "undetermined")
        finally:
            inaccessible.chmod(0o700)
        self.manifest('[[retired]]\nname="bad"\nkind="code"\npattern="["\nregex=true\npaths=["src/*"]\n')
        self.assertEqual(self.cli().returncode, 2)
        self.write("retired.d/good.toml", '[[retired]]\nname="present"\nkind="code"\npattern="old()"\npaths=["src/*"]\n')
        proc = self.cli()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("1 present, 1 undetermined", proc.stdout)
        self.manifest('[[retired]]\nname="missing"\nkind="code"\npattern="x"\npaths=["src/*"]\nrepo="' + str(self.repo / "missing") + '"\n')
        self.assertEqual(self.cli().returncode, 2)
        self.write("retired.d/good.toml", "")
        self.manifest('[[retired]]\nname="cron"\nkind="cron"\npattern="x"\npaths=[]\n')
        self.crontab.unlink()
        self.assertEqual(self.cli().returncode, 2)

    def test_ac4_git_failure_and_unreadable_tracked_file(self):
        self.manifest()
        with patch.object(retired.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["git", "ls-files"], 128, "", "failed")):
            self.assertEqual(self.check(self.row())[0].reason, "undetermined")
        with patch.object(retired.subprocess, "run", side_effect=OSError("missing git")):
            self.assertEqual(self.check(self.row())[0].reason, "undetermined")
        nongit = self.repo / "nongit"
        nongit.mkdir()
        # A directory outside the fixture repo must not inherit its parent index.
        self.manifest('[[retired]]\nname="nongit"\nkind="code"\npattern="x"\npaths=["*"]\nrepo="' + str(nongit) + '"\n')
        with patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.repo)):
            self.assertEqual(self.cli().returncode, 2)
        self.manifest()
        target = self.write("src/missing.py", "old()")
        target.unlink()
        self.assertEqual(self.check(self.row())[0].reason, "undetermined")
        self.assertEqual(self.cli().returncode, 2)

    def test_ac4_crontab_reader(self):
        for code, out, err, expected in [(0, "jobs", "", "jobs"),
                (1, "", "no crontab for test", ""), (1, "", "permission denied", None)]:
            with patch.object(retired.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["crontab", "-l"], code, out, err)) as run:
                self.assertEqual(retired._live_crontab_text(), expected)
                self.assertEqual(run.call_args.args[0], ["crontab", "-l"])
                self.assertFalse(run.call_args.kwargs.get("shell", False))
        with patch.object(retired.subprocess, "run", side_effect=OSError("missing")):
            self.assertIsNone(retired._live_crontab_text())

    def test_ac5_arming_visible_and_repo_checked_first(self):
        row = self.row(armed_when_path="marker", armed_when_contains="replacement")
        self.write("src/a.py", "old()")
        self.assertEqual(self.check(row)[0].reason, "unarmed")
        self.assertEqual(self.check(dataclasses.replace(row, regex=True, pattern="("))[0].reason,
                         "unarmed")
        self.write("marker", "not yet")
        self.assertEqual(self.check(row)[0].reason, "unarmed")
        marker = self.write("marker", "replacement")
        marker.chmod(0)
        try:
            self.assertEqual(self.check(row)[0].reason, "undetermined")
        finally:
            marker.chmod(0o600)
        self.assertEqual(self.check(row)[0].reason, "present")
        self.assertEqual(self.check(dataclasses.replace(row, repo=str(self.repo / "missing")))[0].reason,
                         "undetermined")
        self.manifest('[[retired]]\nname="gated"\nkind="code"\npattern="old()"\npaths=["src/*"]\narmed_when_path="absent"\narmed_when_contains="replacement"\n')
        proc = self.cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("unarmed", proc.stdout)
        self.assertIn("0 present, 0 undetermined, 1 unarmed", proc.stdout)
        self.manifest('[[retired]]\nname="gated"\nkind="code"\npattern="old()"\npaths=["src/*"]\nprovisional=true\narmed_when_path="marker"\narmed_when_contains="replacement"\n')
        for state in ["directory", "unreadable", "decode"]:
            with self.subTest(state=state):
                marker.unlink()
                if state == "directory":
                    marker.mkdir()
                elif state == "unreadable":
                    marker.write_text("replacement")
                    marker.chmod(0)
                else:
                    marker.write_bytes(b"\xff")
                try:
                    self.assertEqual(self.check(row)[0].reason, "undetermined")
                    proc = self.cli()
                    self.assertEqual(proc.returncode, 2, proc.stdout)
                    self.assertIn("0 present, 1 undetermined, 0 unarmed, 1 provisional", proc.stdout)
                finally:
                    if marker.is_dir():
                        marker.rmdir()
                    else:
                        marker.chmod(0o600)
                        marker.unlink()
                    marker.write_text("replacement")
        self.assertEqual(self.cli().returncode, 1)
        marker.unlink()
        proc = self.cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("provisional: gated", proc.stdout)
        self.assertIn("0 present, 0 undetermined, 1 unarmed, 1 provisional", proc.stdout)

    def test_ac6_real_registry_documentation(self):
        rows = retired.load(str(REPO / "retired.d"))
        required = {"upload-archive-once", "realm-views"}
        self.assertTrue(required <= {r.name for r in rows})
        self.assertTrue(all(Path(r.repo).is_absolute() for r in rows if r.repo is not None))
        text = (REPO / "retired.d/README.md").read_text()
        self.assertTrue(next(r for r in rows if r.name == "upload-archive-once").provisional)
        for field in ["name", "kind", "pattern", "paths", "repo", "regex", "provisional", "armed_when_path", "armed_when_contains"]:
            self.assertIn(field, text)
        for code in [0, 1, 2]:
            self.assertIn(f"exit {code}", text)
        print("name kind pattern paths repo regex armed_when_path armed_when_contains; exits 0/1/2")

    def test_ac7_test_runner_gate(self):
        # A complete disposable launcher fixture proves the test-loop gate,
        # including exit 2, without running the whole suite recursively.
        tree = self.repo / "launcher"
        (tree / "src").mkdir(parents=True)
        (tree / "retired.d").mkdir()
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        shutil.copy(REPO / "doit", tree / "doit")
        shutil.copy(REPO / "src/retired.py", tree / "src/retired.py")
        self.write("launcher/src/test_probe.py", "print('probe')\n")
        self.write("launcher/retired.d/clean.toml", '[[retired]]\nname="gone"\nkind="code"\npattern="removed"\npaths=["src/test_probe.py"]\n')
        bindir = self.repo / "bin"
        bindir.mkdir()
        fake = self.write("bin/crontab", "#!/bin/sh\nprintf 'no crontab for test\\n' >&2\nexit 1\n")
        fake.chmod(0o700)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        proc = subprocess.run([str(tree / "doit"), "test"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS retired check", proc.stdout)
        self.write("launcher/retired.d/bad.toml", "invalid = [")
        proc = subprocess.run([str(tree / "doit"), "test"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL retired check (exit 2)", proc.stdout)

    def test_ac7_cli_defaults_independent_of_root_and_cwd(self):
        # Default paths belong to the launcher's checkout, never a live registry.
        tree = self.repo / "launcher"
        (tree / "src").mkdir(parents=True)
        (tree / "retired.d").mkdir()
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        shutil.copy(REPO / "doit", tree / "doit")
        shutil.copy(REPO / "src/retired.py", tree / "src/retired.py")
        self.write("launcher/src/merge_gate.py", "def granted():\n    pass\n")
        self.write("launcher/retired.d/clean.toml", '[[retired]]\nname="gone"\nkind="code"\npattern="removed"\npaths=["src/merge_gate.py"]\n')
        self.manifest('[[retired]]\nname="granted"\nkind="code"\npattern="def granted"\npaths=["src/merge_gate.py"]\n')
        env = dict(os.environ, DOIT_ROOT=str(self.repo))
        outputs = []
        for cwd in (self.repo, tree / "src"):
            proc = subprocess.run([str(tree / "doit"), "retired", "check",
                                   "--crontab-file", str(self.crontab)],
                                  cwd=cwd, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("0 present, 0 undetermined", proc.stdout)
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])
        proc = subprocess.run([str(tree / "doit"), "retired", "check", "--root", str(self.root),
                               "--crontab-file", str(self.crontab)],
                              cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("src/merge_gate.py", proc.stdout)


if __name__ == "__main__":
    unittest.main()
