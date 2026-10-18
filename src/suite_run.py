#!/usr/bin/env python3
"""Run the pushed do-it-v2 suite in a disposable clone and record its result."""
import argparse
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch
import fold

LEDGER_NAME = "L-suite-daily-local.jsonl"


def ledger_path():
    return fold.EVENTS / LEDGER_NAME


def _git(repo, *args, timeout=10):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL, timeout=timeout).strip()


def _branch(branch):
    if branch is not None:
        return branch or None
    # Read only explicit configuration: look.load_toml merges a default "main"
    # that would mask both origin/HEAD and an unresolvable branch.
    try:
        with (fold.ROOT / "look.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    configured = config.get("push", {}).get("main", {}).get("do-it-v2")
    if configured:
        return configured
    ref = _git(fold.ROOT / "repos" / "do-it-v2",
               "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return ref[len("origin/"):] if ref.startswith("origin/") else None


def run_suite(repo, timeout_s=1500, branch=None, now=None):
    """Return sha/rc/failed/wall_s without writing. `now` may supply a clock."""
    clock = now or time.monotonic
    started = clock()
    event = {"sha": "unresolved", "rc": 125, "failed": [], "wall_s": 0}
    scratch = None
    try:
        origin = _git(repo, "remote", "get-url", "origin")
        resolved = _branch(branch)
        if not origin or not resolved:
            return event
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="suite-daily-"))
        clone = scratch / "clone"
        subprocess.run(["git", "clone", "--branch", resolved, origin, str(clone)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=timeout_s)
        event["sha"] = _git(clone, "rev-parse", "HEAD")
        proc = subprocess.Popen(["./doit", "test"], cwd=clone, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout_s)
            event["rc"] = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
            event["rc"] = 124
        event["failed"] = re.findall(r"^FAIL (\S+) \(exit ", output, re.MULTILINE)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        event["rc"] = 125
    finally:
        if scratch is not None:
            shutil.rmtree(scratch)
        event["wall_s"] = round(clock() - started, 3)
    return event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent)
    parser.add_argument("--timeout", type=float, default=1500)
    args = parser.parse_args(argv)
    event = run_suite(args.repo, timeout_s=args.timeout)
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        reason = dispatch.emit(path, {"subject": "L-charter-0051"}, "suite-run", **event)
        if reason is not None:
            print(f"suite-run: append refused: {reason}", file=sys.stderr)
            return 2
    except (OSError, AssertionError) as exc:
        print(f"suite-run: append failed: {exc}", file=sys.stderr)
        return 2
    return 0 if event["rc"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
