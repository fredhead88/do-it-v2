"""Offline merge-lane regression tests; fixtures use only temporary repositories."""
import hashlib
import json
import os
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

    def test_ac6_adapter_has_no_embedded_python_or_duplicate_inventory(self):
        text = (LANE / "preflight.sh").read_text()
        for forbidden in ("python", "import ", "merge -q", "worktree add", "pytest", "tests/ci/test_no_remote_ref_tests.py",
                          "\nGUARDS="):
            self.assertNotIn(forbidden, text)
        self.assertIn("guard-tests.txt", text)
        self.assertIn(" preflight --project ", text)
        self.assertEqual(text.count('preflight --project'), 1)
        guards = [line for line in (LANE / "guard-tests.txt").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertEqual(len(guards), 29)
        evidence("AC6", ["adapter has no embedded Python, raw merge, or guard list; 29 guards in the one file"])


class AdapterTests(unittest.TestCase):
    """The adapter runs against a recording stand-in for `doit`, never a real test pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.calls = self.home / "calls"
        self.stub = self.home / "doit"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PG", "SUPABASE_DB_URL", "AS_LOCAL_PG_", "DATA_MCP_DSN"))
                    and k not in ("DATABASE_URL", "GITHUB_ENV", "GITHUB_PATH", "PYTHONPATH")}
        self.env.update(PREFLIGHT_DOIT=str(self.stub), PREFLIGHT_OUT=str(self.home / "out"),
                        PREFLIGHT_WT=str(self.home / "scratch"), CALLS=str(self.calls))
        self.script("echo clean; exit 0")

    def script(self, body):
        self.stub.write_text('#!/bin/bash\n'
                             'echo "$*|guards=${DOIT_PREFLIGHT_GUARDS:-}|tmp=${TMPDIR:-}|pg=${SUPABASE_DB_URL-unset}${PGHOST-}" >> "$CALLS"\n'
                             + body + "\n")
        self.stub.chmod(0o755)

    def run_adapter(self, *numbers, check=True, env=None):
        args = ["bash", str(LANE / "preflight.sh"), "b" * 40, *("L-spec-" + n for n in numbers)]
        result = subprocess.run(args, env=env or self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout)
        report = self.home / "out" / ("rehearse-" + "-".join(numbers) + ".txt")
        text = report.read_text()
        self.assertTrue(text.endswith("DONE\n"), text)
        return text

    def test_ac6_clean_red_and_undetermined_tokens(self):
        clean = self.run_adapter("0001")
        self.assertIn("NO-NEW-FAILURES\nDONE", clean)
        self.assertNotIn("NEW-FAILURES:", clean)
        self.script("echo 'PREFLIGHT-RED guards - guard guard assertion failed: tests/x.py::t'; exit 1")
        red = self.run_adapter("0001")
        self.assertIn("NEW-FAILURES:\nPREFLIGHT-RED guards - guard guard assertion failed: tests/x.py::t\nDONE", red)
        self.assertNotIn("NO-NEW-FAILURES", red)
        self.script("echo 'PREFLIGHT-UNDETERMINED guards base-red tests/x.py::t'; exit 2")
        held = self.run_adapter("0001")
        self.assertIn("NEW-FAILURES:\nRUN-BROKEN: PREFLIGHT-UNDETERMINED guards base-red tests/x.py::t\nDONE", held)
        self.assertNotIn("NO-NEW-FAILURES", held)
        self.script("exit 7")
        silent = self.run_adapter("0001")
        self.assertIn("RUN-BROKEN:", silent)
        self.assertNotIn("NO-NEW-FAILURES", silent)
        evidence("AC6", [clean, red, held, silent])

    def test_ac6_red_classes_keep_the_consumer_lines(self):
        for cls, owner, expect in (("content-conflict", "L-spec-0002", "CONFLICT L-spec-0002"),
                                   ("declined", "L-spec-0002", "CONFLICT L-spec-0002"),
                                   ("overlap", "L-spec-0002", "OVERLAP L-spec-0002: overlaps L-spec-0001"),
                                   ("writes", "L-spec-0002", "OVERLAP L-spec-0002: overlaps L-spec-0001"),
                                   ("pg-marker", "L-spec-0002", "OVERLAP L-spec-0002: overlaps L-spec-0001"),
                                   ("alembic-heads", "L-spec-0002", "ALEMBIC-HEADS L-spec-0002: overlaps L-spec-0001")):
            self.script("echo 'PREFLIGHT-RED static %s %s overlaps L-spec-0001'; exit 1" % (owner, cls))
            text = self.run_adapter("0001", "0002")
            self.assertIn(expect + "\n", text, text)
            self.assertIn("NEW-FAILURES:\nPREFLIGHT-RED static", text)
            self.assertTrue(text.endswith("DONE\n"))

    def test_ac6_consumes_the_one_guard_file_and_runs_once(self):
        text = self.run_adapter("0001", "0002")
        self.assertIn("guards: 29 from " + str(LANE / "guard-tests.txt"), text)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("preflight --project albert-scott --on " + "b" * 40 + " --specs L-spec-0001,L-spec-0002", calls[0])
        self.assertIn("guards=" + str(LANE / "guard-tests.txt"), calls[0])
        # An override file is read, counted and handed through; there is no second inventory.
        other = self.home / "other-guards.txt"
        other.write_text("# note\ntests/a.py\n\ntests/b.py\n")
        self.env["PREFLIGHT_GUARD_FILE"] = str(other)
        self.calls.unlink()
        text = self.run_adapter("0003")
        self.assertIn("guards: 2 from " + str(other), text)
        self.assertIn("guards=" + str(other), self.calls.read_text())
        self.env["PREFLIGHT_GUARD_FILE"] = str(self.home / "missing.txt")
        self.assertIn("RUN-BROKEN: guard inventory unreadable", self.run_adapter("0004"))

    def test_ac6_bounded_timeout_hold_and_scratch_environment(self):
        self.script("sleep 60")
        self.env["PREFLIGHT_OUTER_TIMEOUT"] = "1"
        start = time.monotonic()
        text = self.run_adapter("0001")
        self.assertLess(time.monotonic() - start, 30)
        self.assertIn("RUN-BROKEN: PREFLIGHT-UNDETERMINED timeout", text)
        self.assertNotIn("NO-NEW-FAILURES", text)
        self.script("echo ok")
        env = dict(self.env, PGHOST="fixture", SUPABASE_DB_URL="postgresql://live.invalid/x", DATABASE_URL="x")
        self.run_adapter("0005", env=env)
        last = self.calls.read_text().splitlines()[-1]
        self.assertIn("tmp=" + str(self.home / "scratch"), last)
        self.assertIn("pg=", last)
        self.assertNotIn("live.invalid", last)
        self.assertNotIn("fixture", last)

    def test_ac6_sigterm_stops_the_sweep_and_exits_143(self):
        started = self.home / "started"
        self.script('touch "%s"; exec sleep 120' % started)
        with subprocess.Popen(["bash", str(LANE / "preflight.sh"), "c" * 40, "L-spec-0001"], env=self.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True) as proc:
            try:
                deadline = time.monotonic() + 15
                while not started.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "sweep stand-in did not start")
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=10), 143)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        evidence("AC6", ["SIGTERM: exit 143"])

    def test_ac6_stale_report_replaced(self):
        out = self.home / "out"
        out.mkdir()
        (out / "rehearse-0001.txt").write_text("NEW-FAILURES:\nstale failure\nDONE\n")
        text = self.run_adapter("0001")
        self.assertNotIn("stale failure", text)
        self.assertIn("NO-NEW-FAILURES", text)


class RealSweepAdapterTests(unittest.TestCase):
    """The adapter with the real `doit preflight`: bookkeeping is re-chained, not raw-merged."""

    def test_ac6_two_migration_branches_rechain_with_no_alembic_heads_rejection(self):
        from unittest.mock import patch
        from test_rechain import Fixture, git
        import fold
        with tempfile.TemporaryDirectory(prefix="lane-real-") as tmp, patch.dict(os.environ):
            home = Path(tmp)
            fx = Fixture(home)
            fx.write("tests/test_guard.py", "def test_guard(): assert True\n")
            import rechain
            isolation = (fx.repo / rechain.TEST).read_text()
            fx.write(rechain.TEST, "import json\nfrom pathlib import Path\n"
                     "head=json.loads((Path(__file__).parents[1]/'alembic_supabase/branch_isolation.json').read_text())['heads'][0]['revision']\n"
                     "declared_heads=actual_heads={head}\nMANIFEST=VERSIONS=None\n"
                     "def resolve_scratch_target(*args): return head\ndef resolve_target(*args): return head\n"
                     "def resolve_scratch_union_heads(*args): return (head,)\n" + isolation)
            base = fx.commit("guard")
            fx.base = base
            state = home / "state"
            for d in ("content", "events", "repos"):
                (state / d).mkdir(parents=True)
            (state / "repos" / "albert-scott").symlink_to(fx.repo, target_is_directory=True)
            config = state / "grading.toml"
            config.write_text('[project.albert-scott]\nvenv = "/opt/albert-scott/.venv"\n')
            guards = home / "guards.txt"
            guards.write_text("tests/test_guard.py\n")
            os.environ.update(DOIT_ROOT=str(state), DOIT_LEDGER_FILE="L-builder-fixture.jsonl", DOIT_GRADING_TOML=str(config))
            with patch.object(fold, "ROOT", state), patch.object(fold, "EVENTS", state / "events"):
                numbers = []
                for name in ("2001", "2002"):
                    spec, branch, sha = fx.branch(name)
                    numbers.append(name)
                    (state / "content" / ("L-spec-%s.md" % name)).write_text(
                        "Writes:\n" + "".join("- %s\n" % p for p in git(fx.repo, "diff", "--name-only", base, sha).splitlines()))
                    fold.append(["spec-written", "L-spec-" + name, "project=albert-scott"], strict=True)
                    with patch.dict(os.environ, DOIT_LEDGER_FILE="L-executor-fixture.jsonl"):
                        fold.append(["merge-gate-clean", "L-spec-" + name, "project=albert-scott", "branch=" + branch], strict=True)
            env = dict(os.environ, PREFLIGHT_OUT=str(home / "out"), PREFLIGHT_WT=str(home / "scratch"),
                       PREFLIGHT_GUARD_FILE=str(guards))
            (home / "scratch").mkdir()
            result = subprocess.run(["bash", str(LANE / "preflight.sh"), base, "L-spec-2001", "L-spec-2002"],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            report = (home / "out/rehearse-2001-2002.txt").read_text()
            self.assertEqual(result.returncode, 0, result.stdout + report)
            self.assertNotIn("ALEMBIC-HEADS", report)
            self.assertIn("PREFLIGHT-RECHAINED L-spec-2002 h0 ", report)
            self.assertIn("NO-NEW-FAILURES\nDONE\n", report)
            evidence("AC6", [report])


if __name__ == "__main__":
    unittest.main(verbosity=2)
