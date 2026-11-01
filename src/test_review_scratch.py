"""A review success, failure or termination releases its private worktree."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class ReviewScratchTests(unittest.TestCase):
    def test_command_exit_and_signal_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            repo = top / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c",
                            "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base"], check=True)
            env = {**os.environ, "DOIT_ROOT": str(top / "root"), "DOIT_SCRATCH": str(top / "scratch")}
            wrapper = Path(__file__).with_name("review_scratch.py")
            for code in (0, 7, "signal"):
                script = "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); "
                script += "time.sleep(60)" if code == "signal" else f"raise SystemExit({code})"
                ready = top / (str(code) + ".ready")
                proc = subprocess.Popen([sys.executable, str(wrapper), "--repo", str(repo),
                    "--sha", "HEAD", "--", sys.executable, "-c", script, str(ready)], env=env)
                try:
                    if code == "signal":
                        deadline = time.monotonic() + 10
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertTrue(ready.exists())
                        proc.terminate()
                    self.assertEqual(proc.wait(timeout=10), 143 if code == "signal" else code)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                self.assertEqual(list((top / "scratch/reviewer").iterdir()), [])
                worktrees = subprocess.check_output(["git", "-C", str(repo), "worktree", "list"], text=True)
                self.assertEqual(len(worktrees.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
