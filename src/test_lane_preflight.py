"""Offline merge-lane regression tests; fixtures use only temporary repositories."""
import hashlib
import json
import os
import shlex
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "lane"


def command(*args, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise AssertionError(f"{args[0]} exited {result.returncode}: {result.stdout}")
    return result.stdout.strip()


def evidence(ac, bodies):
    body = "\n".join(bodies)
    print(json.dumps({"ac": ac, "url": Path(__file__).as_uri() + "#" + ac,
                      "status": 200, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "body_excerpt": body[:1000]}), flush=True)


class ScriptTests(unittest.TestCase):
    def test_ac1_source_and_syntax(self):
        for script in ("rloop.sh", "preflight.sh", "attr.sh"):
            command("bash", "-n", str(LANE / script))
            entry = command("git", "ls-files", "-s", "lane/" + script, cwd=ROOT)
            self.assertEqual(entry.split()[0], "100755", entry)
        evidence("AC1", ["bash -n and tracked mode 100755: 3 passed"])

    def test_ac2_calls(self):
        text = (LANE / "rloop.sh").read_text()
        self.assertNotIn("rehearse-with-pg.sh", text)
        self.assertNotIn("/var/tmp/push/attr.sh", text)
        self.assertEqual(text.count("$L/preflight.sh"), 2)
        self.assertEqual(text.count("$L/attr.sh"), 1)
        evidence("AC2", ["preflight calls: 2; relative attr calls: 1; old paths absent"])


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            __import__("pytest")
        except ImportError as exc:
            raise AssertionError(f"missing module pytest in {sys.executable}") from exc

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "PG", "SUPABASE_DB_URL", "AS_LOCAL_PG_", "DATA_MCP_DSN"))
                    and k not in ("DATABASE_URL", "GITHUB_ENV", "GITHUB_PATH")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                        # A nested pytest run must not load the dispatch runner's
                        # plugins, which are unavailable in candidate worktrees.
                        PYTEST_ADDOPTS="-p _doit_runner_pytest_addopts_missing",
                        PYTEST_PLUGINS="_doit_runner_pytest_plugins_missing",
                        PYTHONPATH=str(self.home / "dispatch-runner"))
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "Lane fixture")
        self.git("config", "user.email", "lane@example.invalid")
        self.write("tests/test_ok.py", "def test_ok(): assert True\n")
        self.write("tests/test_guard.py", "def test_guard(): assert True\n")
        self.write("shared.txt", "original\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.env.update(PREFLIGHT_REPO=str(self.repo), PREFLIGHT_INTEG=str(self.repo),
                        PREFLIGHT_OUT=str(self.home / "out"), PREFLIGHT_PY=sys.executable,
                        PREFLIGHT_CONTENT=str(self.home / "content"),
                        PREFLIGHT_SRC=str(ROOT / "src"),
                        PREFLIGHT_GUARDS="tests/test_guard.py",
                        PREFLIGHT_WT=str(self.home / "worktrees"))
        (self.home / "content").mkdir()

    def git(self, *args):
        return command("git", *args, cwd=self.repo, env=self.env)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def branch(self, number, files):
        self.git("checkout", "-qb", "l-spec-" + number, self.base)
        for name, text in files.items():
            self.write(name, text)
        self.commit()

    def batch_args(self, *numbers):
        return ["bash", str(LANE / "preflight.sh"), self.base,
                *("L-spec-" + n for n in numbers)]

    def run_batch(self, *numbers):
        command(*self.batch_args(*numbers), env=self.env)
        report = self.home / "out" / ("rehearse-" + "-".join(numbers) + ".txt")
        self.assertEqual(list((self.home / "worktrees").iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        text = report.read_text()
        self.assertTrue(text.endswith("DONE\n"), text)
        return text

    def test_ac3_batches_and_conflict(self):
        self.branch("0001", {"tests/test_added.py": "def test_added(): assert True\n",
                             "shared.txt": "one\n"})
        self.branch("0002", {"tests/test_bad.py": "def test_bad(): assert False\n"})
        self.branch("0003", {"shared.txt": "three\n"})
        clean = self.run_batch("0001")
        self.assertIn("NO-NEW-FAILURES", clean)
        self.assertFalse((self.home / "out/rehearse-0001.txt.base").exists())
        failed = self.run_batch("0001", "0002")
        self.assertIn("NEW-FAILURES:\ntests/test_bad.py::test_bad", failed)
        self.assertNotIn("NO-NEW-FAILURES", failed)
        conflict = self.run_batch("0001", "0003")
        self.assertIn("CONFLICT L-spec-0003", conflict)
        self.assertNotIn("cand:", conflict)
        evidence("AC3", [clean, failed, conflict])

    def test_ac4_scratch_exclusion_and_base_red(self):
        self.branch("0001", {"tests/test_scratch.py":
                             "# pg_scratch ScratchRequired\ndef test_db(): assert False\n"})
        excluded = self.run_batch("0001")
        targets = next(line for line in excluded.splitlines() if line.startswith("targets:"))
        self.assertNotIn("test_scratch.py", targets)
        self.assertIn("excluded: 1", excluded)
        self.assertIn("ScratchRequired", excluded)
        self.git("checkout", "-q", "master")
        self.write("tests/test_guard.py", "def test_guard(): assert False\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0002", {"tests/test_guard.py": "# same failure\ndef test_guard(): assert False\n"})
        # The unchanged passing guard lets the run distinguish failures from a broken zero-pass run.
        self.env["PREFLIGHT_GUARDS"] += " tests/test_ok.py"
        red = self.run_batch("0002")
        self.assertIn("NO-NEW-FAILURES", red)
        self.assertIn("base:", red)
        evidence("AC4", [excluded, red])

    def test_ac5_timeout(self):
        self.write("tests/test_sleep.py", "import time\ndef test_sleep(): time.sleep(30)\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0001", {"shared.txt": "one\n"})
        self.env.update(PREFLIGHT_GUARDS="tests/test_sleep.py", PREFLIGHT_PYTEST_TIMEOUT="5")
        start = time.monotonic()
        report = self.run_batch("0001")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 60)
        self.assertIn("PREFLIGHT-TIMEOUT\nNO-NEW-FAILURES", report)
        evidence("AC5", [report, f"elapsed < 60 seconds: {elapsed:.2f}"])

    def test_collection_error_and_zero_pass_are_broken(self):
        self.branch("0001", {"tests/test_invalid.py": "def invalid syntax\n"})
        self.assertIn("RUN-BROKEN:", self.run_batch("0001"))
        self.branch("0002", {"tests/test_skip.py":
                             "import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n"})
        self.env["PREFLIGHT_GUARDS"] = ""
        self.assertIn("RUN-BROKEN: candidate ran zero passing tests", self.run_batch("0002"))

    def test_environment_scrub_and_no_nearby_tests(self):
        self.env.update(PG_LANE_SENTINEL="fixture", SUPABASE_DB_URL_SENTINEL="fixture",
                        AS_LOCAL_PG_SENTINEL="fixture", DATA_MCP_DSN="fixture",
                        DATA_MCP_DSN_SENTINEL="fixture", DATABASE_URL="fixture",
                        GITHUB_ENV="fixture", GITHUB_PATH="fixture",
                        NODE_ID_SECRET="fixture", CI_BUILD_BOX="true")
        self.branch("0001", {"tests/test_environment.py":
            "import os\ndef test_environment():\n"
            "    prefixes = ('P' + 'G', 'AS_LOCAL_' + 'PG_', 'DATA_MCP_' + 'DSN')\n"
            "    assert not any(k.startswith(prefixes) for k in os.environ)\n"
            "    for suffix in ('', '_DIRECT', '_RO', '_MIGRATIONS'):\n"
            "        assert os.environ['SUPABASE_' + 'DB_URL' + suffix] == ''\n"
            "    assert 'SUPABASE_' + 'DB_URL_SENTINEL' not in os.environ\n"
            "    assert os.environ['DATABASE_' + 'URL'] == ''\n"
            "    assert 'GITHUB_ENV' not in os.environ and 'GITHUB_PATH' not in os.environ\n"
            "    assert 'PYTEST_ADDOPTS' not in os.environ\n"
            "    assert 'PYTEST_PLUGINS' not in os.environ\n"
            "    assert os.environ['PYTHONPATH'] == 'api:.'\n"
            "    assert os.environ['NODE_ID_SECRET'] == 'ci-pg-parity-node-id-secret'\n"
            "    assert os.environ['CI_BUILD_BOX'] == 'false'\n"
            "    assert os.environ['PATH'].split(os.pathsep)[0] == os.path.dirname(os.environ['PREFLIGHT_PY'])\n",
            "feature.py": "value = 1\n"})
        self.git("checkout", "-q", "master")
        self.write("tests/test_nearby.py", "# feature\ndef test_unrelated(): assert False\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        report = self.run_batch("0001")
        self.assertIn("NO-NEW-FAILURES", report)
        self.assertNotIn("test_nearby.py", report)

    def test_static_marker_and_writes_overlap(self):
        self.write("tests/test_scratch.py", "# pg_scratch ScratchRequired\ndef test_db(): pass\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0001", {"tests/test_scratch.py": "# ScratchRequired\ndef test_db(): pass\n"})
        report = self.run_batch("0001")
        self.assertIn("has no pg_scratch marker", report)
        self.assertNotIn("cand:", report)
        self.branch("0002", {"two.txt": "two\n"})
        self.branch("0003", {"three.txt": "three\n"})
        for number in ("0002", "0003"):
            (self.home / "content" / ("L-spec-" + number + ".md")).write_text(
                "Writes:\n- shared.txt\n")
        report = self.run_batch("0002", "0003")
        self.assertIn("OVERLAP L-spec-0003: Writes grant shared.txt", report)
        self.assertNotIn("cand:", report)
        evidence("AC6", [report, "marker removal rejected before pytest"])

    def test_ac6_sigterm_cleans_worktrees(self):
        self.branch("0001", {"tests/test_signal.py":
            "import os, time\nfrom pathlib import Path\n"
            "def test_sleep():\n"
            "    Path(os.environ['STARTED']).touch()\n"
            "    time.sleep(120)\n"})
        started = self.home / "started"
        self.env["STARTED"] = str(started)
        with subprocess.Popen(self.batch_args("0001"), env=self.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True) as proc:
            try:
                deadline = time.monotonic() + 15
                while not started.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "candidate pytest did not start")
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=10), 143)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        self.assertEqual(list((self.home / "worktrees").iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        evidence("AC6", ["SIGTERM: exit 143, no pf-* worktrees remain"])

    def test_marker_lint_exempts_existing_missing_marker(self):
        self.write("tests/test_scratch.py", "# ScratchRequired\ndef test_db(): pass\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0001", {"tests/test_scratch.py": "# ScratchRequired\ndef test_db(): assert False\n"})
        self.assertIn("NO-NEW-FAILURES", self.run_batch("0001"))

    def test_stale_report_and_worktrees_replaced(self):
        self.branch("0001", {"shared.txt": "one\n"})
        out = self.home / "out"
        out.mkdir()
        report = out / "rehearse-0001.txt"
        report.write_text("NEW-FAILURES:\nstale failure\nDONE\n")
        parent = self.home / "worktrees"
        parent.mkdir()
        for name in ("pf-cand-0001", "pf-base-0001"):
            self.git("worktree", "add", "-q", "--detach", str(parent / name), self.base)
        text = self.run_batch("0001")
        self.assertNotIn("stale failure", text)
        self.assertIn("NO-NEW-FAILURES", text)

    def test_timeout_preserves_emitted_failures(self):
        self.write("conftest.py",
            "def pytest_runtest_logreport(report):\n"
            "    if report.failed: print('\\nFAILED ' + report.nodeid, flush=True)\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0001", {"tests/test_early.py":
            "import time\ndef test_a_bad(): assert False\ndef test_z_sleep(): time.sleep(30)\n"})
        self.env["PREFLIGHT_PYTEST_TIMEOUT"] = "5"
        report = self.run_batch("0001")
        self.assertIn("PREFLIGHT-TIMEOUT", report)
        self.assertIn("tests/test_early.py::test_a_bad", report)
        self.assertNotIn("NO-NEW-FAILURES", report)

    def test_vitest_own_targets_and_base_comparison(self):
        # A local stand-in checks the CLI contract without invoking node/network.
        binary = self.home / "bin"
        binary.mkdir()
        # Keep the interpreter and npx stand-in together: preflight correctly
        # prepends the interpreter directory, which may otherwise contain npx.
        python = binary / "python"
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)
        npx = binary / "npx"
        npx.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "$VITEST_CALLS"\n'
            '[ -L node_modules ] && [ "$(readlink node_modules)" = "$PREFLIGHT_REPO/dashboard/node_modules" ] || exit 2\n'
            'if grep -q red src/own.test.tsx; then\n'
            '  echo " × src/own.test.tsx > same failure 2ms"; exit 1\nfi\n')
        npx.chmod(0o755)
        calls = self.home / "vitest-calls"
        self.env.update(PATH=str(binary) + os.pathsep + self.env["PATH"],
                        VITEST_CALLS=str(calls), PREFLIGHT_PY=str(python))
        self.write("dashboard/src/own.test.tsx", "green\n")
        self.write("dashboard/src/nearby.test.ts", "red\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0001", {"dashboard/src/own.test.tsx": "red\n"})
        report = self.run_batch("0001")
        self.assertIn("vitest: src/own.test.tsx > same failure", report)
        self.assertEqual(len(calls.read_text().splitlines()), 2)
        self.assertTrue(all("--no-install vitest run" in line for line in calls.read_text().splitlines()))
        self.assertNotIn("nearby.test.ts", calls.read_text())
        self.branch("0003", {"dashboard/src/component.tsx": "export default 1;\n"})
        calls.write_text("")
        self.assertIn("NO-NEW-FAILURES", self.run_batch("0003"))
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertIn("nearby.test.ts", calls.read_text())
        self.git("checkout", "-q", "master")
        self.write("dashboard/src/own.test.tsx", "red\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.branch("0002", {"dashboard/src/own.test.tsx": "red\n// comment\n"})
        self.assertIn("NO-NEW-FAILURES", self.run_batch("0002"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
