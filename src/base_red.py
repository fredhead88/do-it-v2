#!/usr/bin/env python3
"""The shared, offline-by-default base CI failure service (L-spec-1106).

Only --ensure may dispatch CI. An undetermined result never enters the cache.
"""
import argparse
import collections
import fcntl
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import zipfile
import zlib

import fold
import ghlimit
import window_gate

KINDS = ("pytest", "pg-parity", "vitest", "suite-error")
WORKFLOWS = {"Python Tests", "Dashboard PR Quality"}
DOIT = pathlib.Path(__file__).resolve().parent.parent / "doit"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NEW = re.compile(r"\bNEW (failed|error|suite-error): (.+)$")
CHANGED = re.compile(r"\bCHANGED: (.+?)\s+\(baseline:.*\)$")
SUMMARY = re.compile(r"\bSUMMARY (.*?) check: (PASS|FAIL)\b(.*)$")
NODE = re.compile(r"\b(?:FAILED|ERROR)\s+([^\s]+\.py::\S+)")


def parse_log(text):
    """Parse gate output; every unnamed count has an explicit reason.

    Both timestamped Actions zip members and job/step-prefixed log lines are
    accepted. A pg step's node ids take priority over its step-level signature.
    """
    result = {k: set() for k in KINDS}
    result["undetermined"] = []
    pending = collections.defaultdict(lambda: collections.Counter())
    pg_nodes, pg_failed = collections.defaultdict(set), set()
    for raw in text.splitlines():
        line = ANSI.sub("", raw).strip()
        parts = line.split("\t", 2)
        pg_step = parts[1] if len(parts) == 3 and "pg-parity" in parts[0].lower() else None
        m = NEW.search(line)
        if m:
            kind, node = m.groups()
            node = node.strip()
            family = "pytest" if re.match(r".+\.py::", node) else "vitest"
            if kind == "suite-error":
                result["suite-error"].add(node)
                family = "vitest"
            elif "::" in node:
                result[family].add(node)
            else:
                result["undetermined"].append("NEW failure has no attributable id")
                continue
            pending[family]["new"] += 1
            if pg_step and family == "pytest":
                pg_nodes[pg_step].add(node)
        m = CHANGED.search(line)
        if m:
            result["pytest"].add(m[1])
            pending["pytest"]["changed"] += 1
            if pg_step:
                pg_nodes[pg_step].add(m[1])
        if pg_step:
            m = NODE.search(line)
            if m:
                result["pytest"].add(m[1])
                pg_nodes[pg_step].add(m[1])
            if "##[error]" in line or re.search(r"Process completed with exit code [1-9]", line):
                pg_failed.add(pg_step)
        m = SUMMARY.search(line)
        if m:
            program, verdict, counters = m.groups()
            family = "vitest" if "vitest" in program.lower() else "pytest" if "pytest" in program.lower() else None
            counts = dict((k, int(v)) for k, v in re.findall(r"\b(new|changed)=(\d+)", counters))
            if family is None:
                result["undetermined"].append("unclassified SUMMARY: " + program)
            else:
                for label, count in counts.items():
                    if count > pending[family][label]:
                        result["undetermined"].append(f"{program}: {label}={count} but only {pending[family][label]} named")
                if verdict == "FAIL" and not counts:
                    result["undetermined"].append(program + ": FAIL without failure counters")
                pending[family].clear()
    result["pg-parity"].update("pg-parity::" + step for step in pg_failed if not pg_nodes[step])
    return result


def _result(sha, reasons=()):
    return {"sha": sha, "complete": False, "ids": {k: [] for k in KINDS},
            "sources": [], "undetermined": list(reasons)}


def _project(project, sha):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
        raise ValueError("unknown project")
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        raise ValueError("unknown project")
    if not re.fullmatch(r"[a-fA-F0-9]{7,40}", sha):
        raise ValueError("sha must be 7 to 40 hex characters")
    full = window_gate.git(repo, "rev-parse", "--verify", sha + "^{commit}").stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", full):
        raise ValueError("sha did not resolve to a commit")
    return repo, full


def _owner(repo):
    remote = window_gate.git(repo, "remote", "get-url", "origin").stdout.strip()
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    if not m:
        raise ValueError("origin is not a classified GitHub repository")
    return m[1]


def _api(path, key):
    rc, out, _ = ghlimit.run(["gh", "api", path, "--paginate", "--slurp"])
    if rc:
        raise ValueError("GitHub API failed")
    pages = json.loads(out)
    if isinstance(pages, dict):
        pages = [pages]
    return [row for page in pages for row in page[key]]


def _logs(owner, rid):
    if not ghlimit.gate()["ok"]:
        raise ValueError("GitHub log quota unavailable")
    proc = subprocess.run([ghlimit.real_gh(), "api", f"repos/{owner}/actions/runs/{rid}/logs"],
                          capture_output=True, timeout=120)
    if proc.returncode:
        raise ValueError("GitHub logs unavailable")
    with zipfile.ZipFile(io.BytesIO(proc.stdout)) as archive:
        members = [(m.filename, archive.read(m).decode("utf-8-sig"))
                   for m in archive.infolist() if not m.is_dir()]
    if not members:
        raise ValueError("empty logs zip")
    return members


def _snapshot(repo, sha):
    result = _result(sha)
    owner = _owner(repo)
    runs = _api(f"repos/{owner}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs")
    complete_runs = [r for r in runs if r["head_sha"] == sha and r["status"] == "completed"
                     and r.get("conclusion") != "cancelled"]
    workflows, seen = set(), set()
    ids = {k: set() for k in KINDS}
    for run in complete_runs:
        if not isinstance(run.get("name"), str) or not run["name"]:
            raise ValueError("workflow cannot be classified")
        # A renamed required workflow is unknown, rather than an absent clean run.
        expected = {"python-tests.yml": "Python Tests", "dashboard-pr-quality.yml": "Dashboard PR Quality"}
        filename = str(run.get("path", "")).split("/")[-1].split("@")[0]
        if filename in expected and run["name"] != expected[filename]:
            raise ValueError("required workflow cannot be classified")
        rid = str(run["id"])
        if not rid.isdigit():
            raise ValueError("invalid run id")
        if rid in seen:
            continue
        seen.add(rid)
        workflows.add(run["name"])
        members = _logs(owner, rid)
        # Step metadata supplies display names for zip members, which otherwise
        # contain only timestamped command output. Never invent a command name.
        pg_jobs = []
        if run["name"] == "Python Tests":
            pg_jobs = [j for j in _api(f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")
                       if "pg-parity" in j["name"].lower()]
        pg_text = "\n".join(text for name, text in members if "pg-parity" in name.lower())
        pg_named = set()
        for name, text in members:
            parsed = parse_log(text)
            for k in KINDS:
                ids[k].update(parsed[k])
            result["undetermined"].extend(parsed["undetermined"])
            if "pg-parity" in name.lower():
                pg_named.update(parsed["pytest"])
        # Raw pytest failure lines also appear in pg-parity, without a baseline
        # gate. Prefer those ids to the failed-step signature.
        pg_named.update(NODE.findall(pg_text))
        ids["pytest"].update(pg_named)
        if not pg_named:
            for job in pg_jobs:
                if job.get("conclusion") == "failure":
                    steps = [s["name"] for s in job["steps"] if s.get("conclusion") == "failure"]
                    if not steps:
                        result["undetermined"].append("failed pg-parity job has no failing step")
                    for step in steps:
                        parsed = parse_log(f"pg-parity\t{step}\t##[error]failed step")
                        ids["pg-parity"].update(parsed["pg-parity"])
        result["sources"].append(run["html_url"])
    result["ids"] = {k: sorted(v) for k, v in ids.items()}
    result["complete"] = WORKFLOWS <= workflows and not result["undetermined"]
    return result, bool(complete_runs)


def _store(path, project, result):
    # Stage first; append through the public entry point with a private env.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        staged = pathlib.Path(f.name)
        json.dump(result, f, sort_keys=True)
    try:
        proc = subprocess.run([str(DOIT), "append", "base-red", "base-" + project,
                               "project=" + project, "sha=" + result["sha"], "complete:=true",
                               "ids:=" + json.dumps(result["ids"]), "sources:=" + json.dumps(result["sources"])],
                              env={**os.environ, "DOIT_ROOT": str(fold.ROOT),
                                   "DOIT_LEDGER_FILE": "L-base-red-local.jsonl"},
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise ValueError("base-red ledger append failed")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def failing(project, sha, *, ensure=False):
    """Compute a full-sha CI set, caching and recording only complete evidence."""
    result = _result(sha)
    try:
        repo, sha = _project(project, sha)
        result["sha"] = sha
        directory = fold.ROOT / "base-red" / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (sha + ".json")
        with (directory / (sha + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                cached = json.loads(path.read_text())
                if cached["sha"] != sha or not cached["complete"] or cached.get("undetermined"):
                    raise ValueError("invalid base-red cache")
                return cached
            result, has_runs = _snapshot(repo, sha)
            if result["complete"]:
                _store(path, project, result)
                return result
            if not ensure or has_runs or result["undetermined"]:
                return result
            marker = directory / (sha + ".dispatched")
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return result
            os.close(fd)
        # Release the cache lock for the duration of verify. A second caller
        # observes the marker and returns immediately instead of waiting on CI.
        required = window_gate.required_checks(repo, sha)
        verdict = window_gate.verify(repo, sha, required, "base-" + project)
        if verdict.get("state") == "undetermined":
            raise ValueError("base verification undetermined")
        return failing(project, sha)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, EOFError,
            zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, subprocess.SubprocessError) as exc:
        result["complete"] = False
        # Errors never echo subprocess stderr, remote credentials or log content.
        result["undetermined"].append("base-red could not establish evidence (" + type(exc).__name__ + ")")
        return result


def classify(project, base_sha, ids):
    base = failing(project, base_sha)
    complete = bool(base["complete"] and not base.get("undetermined"))
    known = set().union(*(set(v) for v in base["ids"].values())) if complete else set()
    return {"pre_existing": [i for i in ids if i in known],
            "new": [i for i in ids if i not in known], "complete": complete}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("sha")
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = failing(args.project, args.sha, ensure=args.ensure)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        state = "undetermined" if result["undetermined"] else "complete" if result["complete"] else "incomplete"
        print(f"{args.project} {result['sha']}: {state}")
        for kind in KINDS:
            for node in result["ids"][kind]:
                print(f"{kind}: {node}")
        for reason in result["undetermined"]:
            print(reason)
    return 2 if result["undetermined"] else 0 if result["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
