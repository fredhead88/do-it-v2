"""A review success, failure or termination releases its private worktree."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class ReviewScratchTests(unittest.TestCase):
    def test_inherited_git_routing_cannot_redirect_review_or_cleanup(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            top = Path(directory)
            clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            repos = [top / name for name in ('source', 'foreign')]
            def git(repo, *args):
                return subprocess.check_output(['git', '-C', str(repo), *args], env=clean, text=True)
            for repo in repos:
                subprocess.run(['git', 'init', '-q', str(repo)], env=clean, check=True)
                (repo / 'owner').write_text(repo.name)
                git(repo, 'add', 'owner')
                git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'base')
            source, foreign = repos
            before = [(git(repo, 'show-ref'), (repo / '.git/config').read_bytes(),
                       git(repo, 'worktree', 'list', '--porcelain')) for repo in repos]
            poisons = [dict(GIT_DIR=str(foreign / '.git'), GIT_WORK_TREE=str(foreign),
                            GIT_COMMON_DIR=str(foreign / '.git'), GIT_INDEX_FILE=str(foreign / '.git/index')),
                       dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.worktree',
                            GIT_CONFIG_VALUE_0=str(foreign))]
            wrapper = Path(__file__).with_name('review_scratch.py')
            script = ("import os,pathlib,subprocess; "
                      "assert pathlib.Path('owner').read_text() == 'source'; "
                      "assert not any(k.startswith('GIT_') for k in os.environ); "
                      "assert pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'], "
                      "text=True).strip()) == pathlib.Path.cwd()")
            for poison in poisons:
                with self.subTest(poison=sorted(poison)):
                    env = dict(clean, DOIT_ROOT=str(top / 'root'), DOIT_SCRATCH=str(top / 'scratch'), **poison)
                    result = subprocess.run([sys.executable, str(wrapper), '--repo', str(source),
                        '--sha', 'HEAD', '--', sys.executable, '-c', script], env=env,
                        capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(list((top / 'scratch/reviewer').iterdir()), [])
                    after = [(git(repo, 'show-ref'), (repo / '.git/config').read_bytes(),
                              git(repo, 'worktree', 'list', '--porcelain')) for repo in repos]
                    self.assertEqual(after, before)

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
