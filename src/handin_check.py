#!/usr/bin/env python3
"""Bounded early secret scan of committed HEAD, with an observational report."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import resource
import time

import cpu_share
import fold
import merge_gate

ATTEMPT_SECONDS = 300
CPU_ACCOUNTING = "rusage-reaped;timeout-descendants-may-be-unaccounted"
REASONS = {"lock-busy", "deadline", "timeout", "worktree", "tool"}


@dataclass
class HandinResult:
    ok: bool
    failed_check: str | None
    findings: list[str]
    cpu_s: float
    wall_s: float


def _cpu():
    return sum(r.ru_utime + r.ru_stime for r in
               (resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)))


@contextmanager
def _ledger_root(root):
    """Scanner bookkeeping and reporting share the override, including errors."""
    prior = fold.ROOT, fold.EVENTS, fold.BOARD, os.environ.get("DOIT_ROOT")
    selected = Path(root if root is not None else os.environ.get("DOIT_ROOT", Path.home() / ".do-it"))
    try:
        fold.ROOT, fold.EVENTS, fold.BOARD = selected, selected / "events", selected / "board.md"
        os.environ["DOIT_ROOT"] = str(selected)
        yield
    finally:
        fold.ROOT, fold.EVENTS, fold.BOARD = prior[:3]
        if prior[3] is None:
            os.environ.pop("DOIT_ROOT", None)
        else:
            os.environ["DOIT_ROOT"] = prior[3]


@contextmanager
def _scanner_state():
    # Imported callers must not inherit another gate's repository or budget.
    names = ("_TOP", "_START", "_RUN_ACTIVE", "DEADLINE", "GIT_TIMEOUT", "MIGRATIONS",
             "REVERT_DEPTH", "REVERT_MAX_PATHS", "_KNOBS", "_DEADLINE_OVERRIDE",
             "DETECT_SECRETS_TIMEOUT", "_RESOLVED", "ENV")
    prior = {name: getattr(merge_gate, name) for name in names}
    try:
        merge_gate._RESOLVED = {}
        merge_gate.ENV = dict(merge_gate.ENV, DOIT_ROOT=os.environ["DOIT_ROOT"])
        yield
    finally:
        for name, value in prior.items():
            setattr(merge_gate, name, value)


def _outcome(response):
    """Never persist raw tool diagnostics, exceptions or environment values."""
    if not isinstance(response, tuple) or len(response) != 2:
        return "undetermined", ["malformed scanner response"]
    status, details = response
    if not isinstance(details, list) or not all(isinstance(x, str) for x in details):
        return "undetermined", ["malformed scanner response"]
    if status == "clean" and not details:
        return None, []
    if status == "rework" and details and all(merge_gate.FINDING_LINE_RE.fullmatch(x) for x in details):
        return "secret-scan", details
    if status == "undetermined" and details and details[0] in REASONS:
        return "undetermined", [details[0], "secret scanner could not complete"]
    return "undetermined", ["malformed scanner response"]


def run(worktree, subject, *, main=None, root=None):
    """Check one captured full HEAD. No gate verdict, dispatch or seat admission."""
    started, cpu_before = time.monotonic(), _cpu()
    sha, failed, findings = None, "undetermined", ["repository preflight failed"]
    with _ledger_root(root):
        with _scanner_state():
            token = merge_gate._SCAN_WALL_END.set(started + ATTEMPT_SECONDS)
            subject_token = merge_gate._SCAN_SUBJECT.set(subject)
            try:
                # Unlike an ordinary gate, an early check must report ambiguous
                # inherited targeting rather than quietly choosing a repository.
                if any(k.startswith("GIT_") for k in os.environ):
                    findings = ["inherited Git redirection is not supported"]
                else:
                    merge_gate._START = started
                    merge_gate._RUN_ACTIVE = False
                    merge_gate.load_config()
                    merge_gate.preflight(str(Path(worktree).resolve()))
                    sha = merge_gate.resolve("HEAD")
                    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                        sha = None
                        raise ValueError("unresolved HEAD")
                    base = merge_gate.resolve(main if main is not None else os.environ.get("DOIT_MAIN", "main"))
                    left = started + ATTEMPT_SECONDS - time.monotonic()
                    if left <= 0:
                        findings = ["deadline"]
                    else:
                        failed, findings = _outcome(merge_gate.secret_scan(
                            merge_gate._TOP, base, sha, wrap=cpu_share.niced, wall_cap=left))
            except (Exception, SystemExit):
                failed = "undetermined"
                findings = ["deadline" if time.monotonic() >= started + ATTEMPT_SECONDS
                            else "repository or scanner tooling failed"]
            finally:
                merge_gate._SCAN_SUBJECT.reset(subject_token)
                merge_gate._SCAN_WALL_END.reset(token)
        result = HandinResult(failed is None, failed, findings,
                              max(0.0, _cpu() - cpu_before), max(0.0, time.monotonic() - started))
        fields = dict(sha=sha, checks=["secret-scan"],
                      result="clean" if result.ok else "rework" if failed == "secret-scan" else "undetermined",
                      failed_check=failed, cpu_s=result.cpu_s, wall_s=result.wall_s,
                      cpu_accounting=CPU_ACCOUNTING)
        try:
            fold.append(["handin-checked", subject] +
                        [f"{key}:={json.dumps(value)}" for key, value in fields.items()], strict=True)
        except (Exception, SystemExit):
            result.ok, result.failed_check = False, "undetermined"
            result.findings = ["handin-checked ledger append failed; report not confirmed"]
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Secret-scan committed HEAD before hand-in (300-second bound).")
    parser.add_argument("subject")
    parser.add_argument("--cwd", required=True, help="worktree whose committed HEAD is scanned")
    args = parser.parse_args(argv)
    result = run(args.cwd, args.subject)
    print("handin-check: " + ("clean" if result.ok else result.failed_check))
    for finding in result.findings:
        print(finding)
    return 0 if result.ok else 1 if result.failed_check == "secret-scan" else 2


if __name__ == "__main__":
    raise SystemExit(main())
