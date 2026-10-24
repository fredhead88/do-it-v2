"""Dependencies and per-check scratch Postgres for shipped-sha owed sweeps.

The CLI reads the check's shell commands on stdin. Endpoint values never enter
the manifest, provisioning record, command line or diagnostics.
"""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import build_runner
import fold
import resources
from contextlib import contextmanager
import grading_env

LIVE_DASHBOARD = pathlib.Path("/opt/albert-scott/dashboard")
PRIVATE_PG = pathlib.Path(__file__).resolve().parents[1] / "scripts/private-pg.sh"
PG_CTL = "/usr/lib/postgresql/17/bin/pg_ctl"
PG_MARKERS = re.compile(r"pg_scratch|live_db|AS_LOCAL_PG_ADMIN")


@contextmanager
def _resource_ledger():
    previous = os.environ.get("DOIT_LEDGER_FILE")
    os.environ["DOIT_LEDGER_FILE"] = "L-owed-sweeper-provision.jsonl"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = previous


def dashboard(tree, root, *, live=LIVE_DASHBOARD):
    import checks
    import provision
    record = checks.CheckRecord("sweep:setup", pathlib.Path(tree).parent.name, "setup", [], "",
                                "scratch", False, "repo-venv", "pre-merge", "model")
    with _resource_ledger(), provision.context(tree=tree, dashboard=(root, live)):
        return provision.prepare(record, lane="owed").summary["node_modules"]


def _pg_path(path):
    path = pathlib.Path(path)
    if (path.parent != pathlib.Path("/var/tmp") or not path.name.startswith("owed-pg-")
            or path.is_symlink() or path.resolve() != path):
        raise build_runner.Undetermined("unsafe scratch Postgres cleanup path")
    return path


def _stop_pg(path, env):
    import provision
    return provision._stop_pg(_pg_path(path), env, PG_CTL)


def run_check(tree, base, commands, record, *, timeout=build_runner.WALL_CAP):
    """Execute in a shared profile, retaining bounded signal-safe cleanup."""
    import checks
    import provision
    summary = json.loads(pathlib.Path(record).read_text())
    subject = summary.get("subject") or pathlib.Path(tree).parent.name
    profile = "repo-scratch-pg" if PG_MARKERS.search(commands) else "repo-venv"
    check = checks.CheckRecord("sweep:check", subject, "check", [], base,
                               "scratch", False, profile, "pre-merge", "model")
    previous = {sig: signal.signal(sig, build_runner._interrupted)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    prepared = None
    try:
        deadline = time.monotonic() + timeout
        with _resource_ledger(), provision.context(tree=tree, env=grading_env._clean_env({"BASE": base}),
                pg_script=PRIVATE_PG, pg_ctl=PG_CTL, pg_deadline=deadline, pg_prefix="owed-pg-", pg_parent="/var/tmp"):
            prepared = provision.prepare(check, lane="owed")
        summary.update(pg=prepared.summary.get("pg", "none"), prod_dsns="unset")
        pathlib.Path(record).write_text(json.dumps(summary, indent=2) + "\n")
        return build_runner._process(["/bin/bash", "-euo", "pipefail", "-c", commands],
                                     cwd=tree, env=prepared.env, deadline=deadline)
    except provision.Undetermined as exc:
        raise build_runner.Undetermined(str(exc)) from None
    except (build_runner.Undetermined, OSError, subprocess.SubprocessError) as exc:
        category = getattr(exc, "category", None)
        if category is None and grading_env.failure_kind(str(exc)) == "infra":
            category = grading_env.infra_class(str(exc))
        if not category:
            raise
        fields = grading_env.infra_fields(pathlib.Path(record).stem, "owed-sweeper", "")
        fields.update({"class": category, "project": summary.get("project") or os.environ.get("DOIT_PROJECT", "unknown")})
        with _resource_ledger():
            fold.append(["infra-failure", subject] + [f"{key}:={json.dumps(value)}" for key, value in fields.items()])
        return 2, ("infra:" + category + "\n").encode()
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if prepared is not None:
                prepared.cleanup()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def check_command(tree, base, record):
    return shlex.join([sys.executable, str(pathlib.Path(__file__).resolve()),
                       "--clone", str(tree), "--base", base, "--record", str(record)])


def packet_row(row):
    if not row.get("check_runner"):
        return []
    return [f"     provisioning: {row['provisioning']}",
            f"     Run: {row['check_runner']} <<'OWED_CHECK'",
            "     <this row's exact Verification or criterion shell commands>",
            "     OWED_CHECK",
            f"     Provisioning record: {row['provisioning_record']}"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone", required=True, type=pathlib.Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--record", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        code, output = run_check(args.clone, args.base, sys.stdin.read(), args.record)
    except (build_runner.Undetermined, OSError, subprocess.SubprocessError) as exc:
        # External process output and endpoints are deliberately excluded.
        reason = str(exc) if isinstance(exc, build_runner.Undetermined) else type(exc).__name__
        print("owed check provisioning/execution unavailable: " + reason, file=sys.stderr)
        return 2
    sys.stdout.buffer.write(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
