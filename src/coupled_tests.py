#!/usr/bin/env python3
"""List existing tests coupled to a spec's Writes grant, without changing its repo."""
import argparse
import contextlib
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time

import merge_gate


TEST = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|\.test\.tsx?$")
MAX_WRITES = 40
GREP_SECONDS = 60
MAX_FILES = 30
TOTAL_SECONDS = 1200


def selected(rows):
    """Named first, with each unfenced file run at most once."""
    return list(dict.fromkeys(row[2] for row in sorted(rows, key=lambda r: r[0] != "named")
                              if not row[3]))[:MAX_FILES]


def selection_note(rows, files):
    eligible = {row[2] for row in rows if not row[3]}
    fenced = {row[2] for row in rows if row[3]} - eligible
    return (f"{len(files)} files; {len(fenced)} fenced by Boundaries; "
            f"{len(eligible) - len(files)} omitted by the {MAX_FILES}-file cap")


def command(file, repo):
    if file.endswith((".test.ts", ".test.tsx")):
        return None, "node-test"
    if not file.endswith(".py"):
        return None, "unsupported-test"
    doit = pathlib.Path(repo) / "doit"
    if doit.is_file() and os.access(doit, os.X_OK):
        return ["/usr/bin/python3", file], None
    venv = pathlib.Path("/opt/albert-scott/.venv/bin/python")
    python = str(venv) if venv.is_file() else sys.executable
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", file], None


def packet_lines(text, repo, rev):
    try:
        rows = scan(text, repo, rev)
        files = selected(rows)
    except (Truncated, subprocess.TimeoutExpired):
        return ["coupled tests: undetermined (scan truncated or timed out); report skipped for the set"]
    except (OSError, ValueError, merge_gate.Undetermined):
        return ["coupled tests: undetermined (scan failed; check repository and revision); report skipped for the set"]
    lines = ["9. Coupled existing tests on the base (" + selection_note(rows, files) +
             "; run after the final edit; report each in coupled):"]
    for file in files:
        cmd, reason = command(file, repo)
        lines.append(f"   {file}: {shlex.join(cmd) if cmd else 'skipped (' + reason + ')'}")
    return lines


def run_line(spec, repo, base):
    return "   Run: " + shlex.join(["/usr/bin/python3", str(pathlib.Path(__file__).resolve()),
                                   str(spec), "--repo", str(repo), "--run", "--base", base])


def run(files, repo, base, *, runner=None, timeout=300):
    """Run branch tests and compare failures with one disposable detached base."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    repo = pathlib.Path(repo).resolve()
    files = list(dict.fromkeys(files))
    for file in files:
        path = pathlib.PurePosixPath(file)
        if path.is_absolute() or ".." in path.parts or file.startswith("-"):
            raise ValueError("test files must be repository-relative paths")
    base = git(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
    runner = runner or subprocess.run
    env = {k: v for k, v in merge_gate.ENV.items() if not k.startswith("SUPABASE_DB_URL")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    deadline = time.monotonic() + TOTAL_SECONDS
    results = []

    def execute(cmd, cwd):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            proc = runner(cmd, cwd=cwd, env=env, timeout=min(timeout, remaining),
                          capture_output=True, text=True)
            return "pass" if proc.returncode == 0 else "fail"
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError:
            return "fail"

    with contextlib.ExitStack() as cleanup:
        scratch = None
        for file in files:
            cmd, reason = command(file, repo)
            row = dict(file=file, branch="skipped", base="not-run", status="skipped")
            if cmd is None:
                row["reason"] = reason
            elif time.monotonic() >= deadline:
                row["reason"] = "total-timeout"
            else:
                row["branch"] = execute(cmd, repo)
                if row["branch"] == "pass":
                    row["status"] = "pass"
                else:
                    if scratch is None:
                        temp = cleanup.enter_context(tempfile.TemporaryDirectory(
                            prefix="doit-coupled-", dir=os.environ.get("TMPDIR")))
                        scratch = pathlib.Path(temp) / "base"
                        # Register before add: even a partially created worktree is cleaned.
                        cleanup.callback(git, repo, "worktree", "remove", "--force", str(scratch))
                        git(repo, "worktree", "add", "--detach", str(scratch), base)
                    base_result = execute(cmd, scratch)
                    # Timeouts count as failures on either revision.
                    row["base"] = "fail" if base_result == "timeout" else base_result
                    row["status"] = "base-red" if row["base"] == "fail" else "regressed"
            results.append(row)
    return results


def validate_card(out):
    for row in out.get("coupled", []):
        if row["status"] != "regressed":
            continue
        file = row["file"]
        if out.get("status") == "DONE":
            return f"regressed coupled test {file} blocks DONE"
        if out.get("status") in ("NEEDS_CONTEXT", "BLOCKED") and not any(
                file in e.get("asks", "") for e in out.get("escalations", [])):
            return f"regressed coupled test {file} needs an escalation naming its path"
    return None


def grader_line(sidecar):
    if "coupled" not in sidecar:
        return None
    return ("4a. Coupled existing tests the builder ran (claims to test, not facts): " +
            "; ".join(f"{row['file']} {row['status']}" for row in sidecar["coupled"]))


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
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        if args.run and (not args.base or not math.isfinite(args.timeout) or args.timeout <= 0):
            raise ValueError("--run requires --base and a positive finite --timeout")
        rows = scan(args.spec.read_text(), args.repo, args.base if args.run else args.rev)
        if args.run:
            files = selected(rows)
            print("coupled tests: " + selection_note(rows, files), file=sys.stderr)
            results = run(files, args.repo, args.base, timeout=args.timeout)
            for row in results:
                print(json.dumps(row))
            return 4 if any(row["status"] == "regressed" for row in results) else 0
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
