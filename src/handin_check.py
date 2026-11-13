#!/usr/bin/env python3
"""Bounded early checks of committed HEAD, with an observational report."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import multiprocessing
import os
import signal
import tempfile
from pathlib import Path
import re
import resource
import time

import cpu_share
import fold
import merge_gate
import preflight_sweep as sweep
from preflight_sweep import guard_inventory
import scratch

ATTEMPT_SECONDS = 300
GUARD_SECONDS = 240
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


def _guard_worker(repo, sha, base, directory, deadline, channel):
    """Reuse the rehearsal inventory, report parser and detached checkout helper."""
    os.setsid()
    failed, findings = "undetermined", ["rehearsal guard tooling unavailable"]
    try:
        nodes, exclusion = guard_inventory()
        candidate = sweep.checkout(repo, directory / "candidate", sha)
        absent = [n for n in nodes if not (candidate / n.split('::')[0]).exists()]
        findings = ([f"skipped: {len(absent)} guard file targets absent"] +
                    ["skipped: " + n + " (guard file absent)" for n in absent]) if absent else []
        nodes = [n for n in nodes if n not in absent]
        failed = None
        if nodes:
            python = sweep.python_for("albert-scott")
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}
            for name in ('home', 'tmp', 'root'):
                (directory / name).mkdir()
            env.update(HOME=str(directory/'home'), TMPDIR=str(directory/'tmp'),
                       DOIT_ROOT=str(directory/'root'), DOIT_TEST_PROCESS='1', PYTHONDONTWRITEBYTECODE='1')
            deselect = [exclusion[0]] if exclusion else []

            def stage(tree, targets):
                # Run the bounded guard set to completion: an inherited failure
                # must not hide a later candidate regression behind fail-fast.
                for target in targets:
                    path = tree / target.split('::')[0]
                    if path.is_symlink() or not path.resolve().is_relative_to(tree.resolve()):
                        raise ValueError('unsafe guard path')
                report = directory / 'results.xml'
                report.unlink(missing_ok=True)
                argv = [python, '-m', 'pytest', '-p', 'no:cacheprovider', '-o', 'addopts=',
                        '-o', 'junit_family=xunit1', '--junitxml='+str(report)]
                for node in deselect:
                    argv += ['--deselect', node]
                proc = sweep.command(argv+['--', *targets], cwd=tree, env=env, deadline=deadline)
                if proc.returncode == 4:
                    raise FileNotFoundError("guard node unavailable")
                rows = sweep.parse_junit(report)
                if (proc.returncode not in (0, 1) or rows is None
                        or not sweep.collected(rows, targets) or any(error for _, _, _, error in rows)):
                    raise ValueError('incomplete guard run')
                red = [node for node, _, bad, _ in rows if bad]
                if bool(red) != bool(proc.returncode):
                    raise ValueError('inconsistent guard run')
                return red

            red = stage(candidate, nodes)
            base_tree = sweep.checkout(repo, directory/'base', base) if red else None
            for node in red:
                try:
                    inherited = ((base_tree / node.split('::')[0]).is_file()
                                 and node in stage(base_tree, [node]))
                except FileNotFoundError:
                    inherited = False
                if inherited:
                    findings.append('base-red: '+node)
                else:
                    failed = 'rehearsal-guards'
                    findings.append(node)
    except (Exception, SystemExit):
        failed, findings = 'undetermined', ['rehearsal guard tooling unavailable']
    finally:
        channel.send((failed, findings))
        channel.close()


def _rehearsal_guards(repo, sha, base):
    """Bound the whole stage, including checkouts, and kill all descendants."""
    process = receive = send = None
    try:
        deadline = time.monotonic() + GUARD_SECONDS
        with tempfile.TemporaryDirectory(prefix='handin-guards-', dir=scratch.sub('handin-guards')) as temp:
            ctx = multiprocessing.get_context('fork')
            receive, send = ctx.Pipe(duplex=False)
            process = ctx.Process(target=_guard_worker,
                                  args=(Path(repo), sha, base, Path(temp), deadline, send))
            try:
                process.start()
                send.close()
                if not receive.poll(sweep.remaining(deadline)):
                    raise TimeoutError('guard deadline expired')
                return receive.recv()
            finally:
                if process.pid:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if process.is_alive():
                        process.kill()
                    process.join()
                    process.close()
    except (Exception, SystemExit):
        return 'undetermined', ['rehearsal guards expired or unavailable']
    finally:
        for channel in (receive, send):
            if channel is not None:
                channel.close()


def run(worktree, subject, *, main=None, root=None):
    """Check one captured full HEAD. No gate verdict, dispatch or seat admission."""
    started, cpu_before = time.monotonic(), _cpu()
    sha, failed, findings = None, "undetermined", ["repository preflight failed"]
    checks = ["secret-scan", "coupled-test-safety"]
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
                        # Preserve scanner failures; grant safety is an additional
                        # check and cannot replace a secret finding with clean.
                        if failed is None:
                            try:
                                findings = merge_gate.coupled_test_safety(
                                    subject, base, sha, check_permissions=True)
                                failed = "coupled-test-safety" if findings else None
                            except merge_gate.Undetermined as exc:
                                failed, findings = "coupled-test-safety", [str(exc)]
                        if failed is None and fold.subject_project(subject) == "albert-scott":
                            checks.append("rehearsal-guards")
                            failed, findings = _rehearsal_guards(merge_gate._TOP, sha, base)
            except (Exception, SystemExit):
                failed = "undetermined"
                findings = ["deadline" if time.monotonic() >= started + ATTEMPT_SECONDS
                            else "repository or scanner tooling failed"]
            finally:
                merge_gate._SCAN_SUBJECT.reset(subject_token)
                merge_gate._SCAN_WALL_END.reset(token)
        result = HandinResult(failed is None, failed, findings,
                              max(0.0, _cpu() - cpu_before), max(0.0, time.monotonic() - started))
        fields = dict(sha=sha, checks=checks,
                      result="clean" if result.ok else "rework" if failed in {"secret-scan", "coupled-test-safety", "rehearsal-guards"} else "undetermined",
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
    parser = argparse.ArgumentParser(description="Check committed HEAD before hand-in (300-second scan, 240-second guards).")
    parser.add_argument("subject")
    parser.add_argument("--cwd", required=True, help="worktree whose committed HEAD is scanned")
    args = parser.parse_args(argv)
    result = run(args.cwd, args.subject)
    print("handin-check: " + ("clean" if result.ok else result.failed_check))
    for finding in result.findings:
        print(finding)
    return 0 if result.ok else 1 if result.failed_check in {"secret-scan", "coupled-test-safety", "rehearsal-guards"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
