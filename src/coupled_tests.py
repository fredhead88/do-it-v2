#!/usr/bin/env python3
"""List existing tests coupled to a spec's Writes grant, without changing its repo."""
import argparse
import pathlib
import re
import subprocess
import sys
import time

import merge_gate


TEST = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|\.test\.tsx?$")
MAX_WRITES = 40
GREP_SECONDS = 60


class Truncated(Exception):
    pass


def git(repo, *args, timeout=60, no_match=False):
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, timeout=timeout, env=merge_gate.ENV)
    if result.returncode not in ((0, 1) if no_match else (0,)):
        raise ValueError(f"git {args[0]} failed (check repository and revision)")
    return result.stdout


def boundaries(text):
    match = re.search(r"(?ms)^##[ \t]+Boundaries[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)", text)
    return match.group(1) if match else ""


def named(source, test):
    stem = pathlib.PurePosixPath(source).stem
    name = pathlib.PurePosixPath(test).name
    return (name == f"test_{stem}.py" or
            (name.startswith(f"test_{stem}_") and name.endswith(".py")) or
            name in (f"{stem}.test.ts", f"{stem}.test.tsx"))


def writes_paths(grant, tree):
    # Expand directory/glob grants with the gate's own coverage semantics.
    paths = set()
    for entry in grant:
        if entry.endswith("/") or any(c in entry for c in "*?"):
            paths.update(p for p in tree if merge_gate.granted(p, [entry]))
        else:
            paths.add(entry)
    paths = sorted(p for p in paths if not TEST.search(p))
    if len(paths) > MAX_WRITES:
        raise Truncated
    return paths


def scan(text, repo, rev=None):
    grant = merge_gate.grant_from_text(text)
    if len(grant) > MAX_WRITES:
        raise Truncated
    base = re.search(r"(?m)^base_sha:[ \t]*['\"]?([^\s'\"#]+)", text)
    rev = rev or (base.group(1) if base else "HEAD")
    # Resolve once: both tree and grep see the same committed snapshot.
    rev = git(repo, "rev-parse", "--verify", "--end-of-options", rev + "^{commit}").strip()
    tree = git(repo, "ls-tree", "-r", "--name-only", "-z", rev, "--").split("\0")
    tests = {p for p in tree if TEST.search(p) and not merge_gate.granted(p, grant)}
    fenced = boundaries(text)
    deadline = time.monotonic() + GREP_SECONDS
    rows = []
    for source in writes_paths(grant, tree):
        hits = {p for p in tests if named(source, p)}
        path = pathlib.PurePosixPath(source)
        module = (str(path.with_suffix("")).replace("/", ".")
                  if path.suffix == ".py" else path.stem)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Truncated
        output = git(repo, "grep", "-l", "-z", "-F", "-e", module, rev, "--",
                     timeout=remaining, no_match=True)
        references = {p[len(rev) + 1:] for p in output.split("\0") if p}
        for tier, found in (("named", hits), ("referencing", (references & tests) - hits)):
            for test in sorted(found):
                rows.append((tier, source, test, test in fenced))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=pathlib.Path)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--rev")
    args = parser.parse_args(argv)
    try:
        rows = scan(args.spec.read_text(), args.repo, args.rev)
    except (Truncated, subprocess.TimeoutExpired):
        print("truncated")
        return 2
    except (OSError, ValueError, merge_gate.Undetermined) as exc:
        print(f"coupled_tests: {exc}", file=sys.stderr)
        return 2
    for tier, source, test, fenced in rows:
        print("\t".join((tier, source, test)) + ("\tfenced" if fenced else ""))
    return 3 if any(tier == "named" and not fenced for tier, _, _, fenced in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
