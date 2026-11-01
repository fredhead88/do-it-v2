"""Run reviewer commands in an owned disk-backed worktree and clean on exit."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

import scratch


def run(repo, sha, command):
    parent = Path(tempfile.mkdtemp(prefix="review-", dir=scratch.sub("reviewer")))
    tree = parent / "tree"
    try:
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), sha],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proc = subprocess.Popen(command, cwd=tree, start_new_session=True)
        try:
            return proc.wait()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a review command is required after --")
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    return run(Path(args.repo).resolve(), args.sha, command)


if __name__ == "__main__":
    raise SystemExit(main())
