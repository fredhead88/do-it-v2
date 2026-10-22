"""Dependencies and per-check scratch Postgres for shipped-sha owed sweeps.

The CLI reads the check's shell commands on stdin. Endpoint values never enter
the manifest, provisioning record, command line or diagnostics.
"""
import argparse
import fcntl
import hashlib
import json
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
import grading_env

LIVE_DASHBOARD = pathlib.Path("/opt/albert-scott/dashboard")
PRIVATE_PG = pathlib.Path(__file__).resolve().parents[1] / "scripts/private-pg.sh"
PG_CTL = "/usr/lib/postgresql/17/bin/pg_ctl"
PG_MARKERS = re.compile(r"pg_scratch|live_db|AS_LOCAL_PG_ADMIN")


def dashboard(tree, root, *, live=LIVE_DASHBOARD):
    """Prefer the lock-keyed cache; use the live tree only with identical locks."""
    directory = pathlib.Path(tree) / "dashboard"
    lock = directory / "package-lock.json"
    if not lock.is_file():
        return "none"
    target = directory / "node_modules"
    if target.exists() or target.is_symlink():
        raise build_runner.Undetermined("node_modules present in committed tree")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    cache_root = pathlib.Path(root) / "cache/node_modules"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / digest
    with (cache_root / (digest + ".lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if cache.exists():
            if (not (cache / "node_modules").is_dir()
                    or not (cache / "package-lock.json").is_file()
                    or (cache / "package-lock.json").read_bytes() != lock.read_bytes()):
                raise build_runner.Undetermined("node cache lock mismatch or incomplete cache")
            source, used = cache / "node_modules", "cache:" + digest
        elif ((live / "node_modules").is_dir() and (live / "package-lock.json").is_file()
              and (live / "package-lock.json").read_bytes() == lock.read_bytes()):
            source, used = live / "node_modules", "matching-lock:" + str(live)
        else:
            # Stage atomically: a failed install must never become a cache hit.
            with tempfile.TemporaryDirectory(prefix=digest[:12] + "-", dir=cache_root) as tmp:
                stage = pathlib.Path(tmp)
                for name in ("package.json", "package-lock.json"):
                    if (directory / name).is_file():
                        shutil.copyfile(directory / name, stage / name)
                build_runner._required(["npm", "ci", "--prefer-offline"], phase="dashboard npm ci",
                    env=grading_env._clean_env(), deadline=time.monotonic() + build_runner.WALL_CAP,
                    cwd=stage)
                if not (stage / "node_modules").is_dir() or (stage / "package-lock.json").read_bytes() != lock.read_bytes():
                    raise build_runner.Undetermined("npm ci produced no lock-matched node_modules")
                stage.rename(cache)
            source, used = cache / "node_modules", "cache:" + digest
        target.symlink_to(source.resolve(), target_is_directory=True)
    return used


def _pg_path(path):
    path = pathlib.Path(path)
    if (path.parent != pathlib.Path("/var/tmp") or not path.name.startswith("owed-pg-")
            or path.is_symlink() or path.resolve() != path):
        raise build_runner.Undetermined("unsafe scratch Postgres cleanup path")
    return path


def _stop_pg(path, env):
    """Stop only this invocation's literal, validated directory before removal."""
    path = _pg_path(path)
    if (path / "data").exists():
        result = subprocess.run([PG_CTL, "-w", "-t", "15", "-D", str(path / "data"),
                                 "-m", "fast", "stop"], env=env,
                                capture_output=True, timeout=20)
        if result.returncode and (path / "data/postmaster.pid").exists():
            raise build_runner.Undetermined("scratch Postgres stop failed; directory retained")
    shutil.rmtree(path)


def run_check(tree, base, commands, record, *, timeout=build_runner.WALL_CAP):
    """One clean child environment and optional loopback admin per invocation."""
    env = grading_env._clean_env({"BASE": base})
    summary = json.loads(pathlib.Path(record).read_text())
    summary.update(pg="none", prod_dsns="unset")
    pg = None
    previous = {sig: signal.signal(sig, build_runner._interrupted)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        deadline = time.monotonic() + timeout
        if PG_MARKERS.search(commands):
            pg = _pg_path(pathlib.Path(tempfile.mkdtemp(prefix="owed-pg-", dir="/var/tmp")))
            output = build_runner._required(["/bin/bash", str(PRIVATE_PG), str(pg), "loopback"],
                phase="scratch Postgres", env=env, deadline=deadline)
            match = re.search(r"^ADMIN_DSN='(postgresql://albert:[0-9a-f]{32}@127\.0\.0\.1:\d+/postgres)'$",
                              output, re.M)
            if not match:
                raise build_runner.Undetermined("scratch Postgres produced no loopback admin endpoint")
            env["AS_LOCAL_PG_ADMIN"] = match[1]
            summary["pg"] = "loopback"
        pathlib.Path(record).write_text(json.dumps(summary, indent=2) + "\n")
        return build_runner._process(["/bin/bash", "-euo", "pipefail", "-c", commands],
                                     cwd=tree, env=env, deadline=deadline)
    finally:
        # A second termination signal must not interrupt the bounded cleanup.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if pg is not None:
                _stop_pg(pg, grading_env._clean_env())
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
