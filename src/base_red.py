#!/usr/bin/env python3
"""The shared, offline-by-default base CI failure service (L-spec-1106).

Only --ensure and nightly may dispatch CI. Uncertain evidence is never cached.
"""
import argparse
import collections
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib

import fold
import ghlimit
import window_gate

KINDS = ("pytest", "pg-parity", "vitest", "suite-error")
WORKFLOWS = {"Python Tests", "Dashboard PR Quality"}
WORKFLOW_FILES = {"Python Tests": "python-tests.yml",
                  "Dashboard PR Quality": "dashboard-pr-quality.yml"}
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
            "sources": [], "undetermined": list(reasons), "workflows": [], "kinds_covered": []}


def _repo(project):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
        raise ValueError("unknown project")
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        raise ValueError("unknown project")
    return repo


def _project(project, sha):
    repo = _repo(project)
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
    rc, out, _ = ghlimit.run(["gh", "api", path, "--paginate"])
    if rc:
        raise ValueError("GitHub API failed")
    source = out.lstrip()
    if not source:
        raise ValueError("missing GitHub API pages")
    decoder, rows = json.JSONDecoder(), []
    while source:
        page, end = decoder.raw_decode(source)
        if not isinstance(page, dict) or key not in page:
            raise ValueError("invalid GitHub API page")
        batch = page[key]
        if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
            raise ValueError("invalid GitHub API rows")
        rows.extend(batch)
        source = source[end:].lstrip()
    return rows


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


def _selection(repo, sha, run, members):
    declarations = set()
    for _, text in members:
        for raw in text.splitlines():
            line = ANSI.sub("", raw).strip().split("\t")[-1].strip()
            line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)
            if not line.startswith("CI-SELECTION"):
                continue
            if not re.fullmatch(r"CI-SELECTION mode=(full|selected|rerun) reason=[\w-]+ files=\d+", line):
                raise ValueError("unreadable CI selection")
            declarations.add(line)
    if len(declarations) > 1:
        raise ValueError("conflicting CI selection")
    if declarations:
        return next(iter(declarations)).split()[1].split("=")[1]
    filename = WORKFLOW_FILES.get(run["name"])
    if filename is None:
        raise ValueError("workflow selection cannot be classified")
    source = window_gate.git(repo, "show", f"{sha}:.github/workflows/{filename}")
    return "selected" if re.search(r"(?m)^\s*select_base\s*:", source.stdout) else "full"


def _runs_seen(runs, sha):
    return sorted({str(r["id"]) for r in runs if r["head_sha"] == sha
                   and r["status"] == "completed" and r.get("name") in WORKFLOWS})


def _snapshot(repo, sha, *, runs=None, covered=None):
    result = _result(sha)
    owner = _owner(repo)
    if runs is None:
        runs = _api(f"repos/{owner}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs")
    result["runs_seen"] = _runs_seen(runs, sha)
    complete_runs = [r for r in runs if r["head_sha"] == sha and r["status"] == "completed"
                     and r.get("conclusion") != "cancelled"]
    workflows, seen, kinds_covered = set(), set(), set()
    ids = {k: set() for k in KINDS}
    for run in complete_runs:
        filename = str(run.get("path", "")).split("/")[-1].split("@")[0]
        if not isinstance(run.get("name"), str) or not run["name"]:
            if (not run.get("name") and run.get("conclusion") == "startup_failure"
                    and filename not in WORKFLOW_FILES.values()):
                continue
            raise ValueError("workflow cannot be classified")
        # A renamed required workflow is unknown, rather than an absent clean run.
        expected = {"python-tests.yml": "Python Tests", "dashboard-pr-quality.yml": "Dashboard PR Quality"}
        if filename in expected and run["name"] != expected[filename]:
            raise ValueError("required workflow cannot be classified")
        rid = str(run["id"])
        if not rid.isdigit():
            raise ValueError("invalid run id")
        if rid in seen:
            continue
        seen.add(rid)
        if run["name"] not in WORKFLOWS:
            continue
        members = _logs(owner, rid)
        try:
            mode = _selection(repo, sha, run, members)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            result["undetermined"].append("workflow selection could not be established")
            continue
        if mode != "full":
            continue
        workflows.add(run["name"])
        # Step metadata supplies display names for zip members, which otherwise
        # contain only timestamped command output. Never invent a command name.
        jobs = _api(f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")
        pg_jobs = []
        text = "\n".join(text for _, text in members)
        ran = {"success", "failure"}
        if run["name"] == "Python Tests":
            pg_jobs = [j for j in jobs if "pg-parity" in j["name"].lower()]
            shards = [j for j in jobs if j["name"].startswith("pytest — shard ")]
            if (any(j["name"] == "pytest" and j.get("conclusion") in ran for j in jobs)
                    and any(j.get("conclusion") in ran for j in shards)
                    and not any(j.get("conclusion") in {"cancelled", "timed_out"} for j in shards)
                    and re.search(r"\bSUMMARY pytest_baseline_gate\.py check:", text)):
                kinds_covered.add("pytest")
            if any(j["name"] == "pg-parity (Postgres)" and j.get("conclusion") in ran for j in jobs):
                kinds_covered.add("pg-parity")
        elif (any(j["name"] == "TypeScript · Biome · Tests · File Sizes"
                  and j.get("conclusion") in ran for j in jobs)
              and re.search(r"\bSUMMARY vitest_baseline_gate\.mjs check:", text)):
            kinds_covered.update(("vitest", "suite-error"))
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
    result["workflows"] = sorted(workflows)
    result["kinds_covered"] = sorted(kinds_covered)
    result["complete"] = WORKFLOWS <= workflows and not result["undetermined"]
    if covered is not None:
        covered.update(workflows)
    return result, bool(complete_runs)


def _store(path, project, result, *, record=True):
    # Stage first; append through the public entry point with a private env.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        staged = pathlib.Path(f.name)
        json.dump(result, f, sort_keys=True)
    try:
        if record:
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


def failing(project, sha, *, ensure=False, refresh=False):
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
                if (cached["sha"] != sha or cached.get("project", project) != project
                        or cached.get("complete") is not True or cached.get("undetermined")
                        or not isinstance(cached.get("ids"), dict)
                        or set(cached["ids"]) != set(KINDS)
                        or any(not isinstance(v, list) or any(not isinstance(i, str) for i in v)
                               for v in cached["ids"].values())):
                    raise ValueError("invalid base-red cache")
                if refresh:
                    # Keep positive proof on refresh failure, never stale negatives.
                    if (set(cached.get("workflows", [])) != WORKFLOWS
                            or not set(cached.get("kinds_covered", [])) <= set(KINDS)):
                        raise ValueError("invalid base coverage")
                    result = dict(cached)
                    result["undetermined"] = []
                    runs = _api(f"repos/{_owner(repo)}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs")
                    if (set(KINDS) <= set(cached.get("kinds_covered", []))
                            and set(_runs_seen(runs, sha)) <= set(cached.get("runs_seen", []))):
                        return cached
                    refreshed, _ = _snapshot(repo, sha, runs=runs)
                    if (refreshed["complete"] and not refreshed["undetermined"]
                            and set(KINDS) <= set(refreshed["kinds_covered"])):
                        _store(path, project, refreshed, record=refreshed["ids"] != cached["ids"])
                        return refreshed
                    result["complete"] = False
                    result["undetermined"].append("required base refresh incomplete")
                    return result
                if "workflows" not in cached or "kinds_covered" not in cached:
                    result, _ = _snapshot(repo, sha)
                    if result["complete"]:
                        _store(path, project, result, record=False)
                    return result
                if set(KINDS) <= set(cached["kinds_covered"]):
                    return cached
                try:
                    runs = _api(f"repos/{_owner(repo)}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs")
                    if set(_runs_seen(runs, sha)) <= set(cached.get("runs_seen", [])):
                        return cached
                    refreshed, _ = _snapshot(repo, sha, runs=runs)
                    if refreshed["complete"]:
                        _store(path, project, refreshed, record=refreshed["ids"] != cached["ids"])
                        return refreshed
                except (OSError, RuntimeError, ValueError, KeyError, TypeError, EOFError,
                        zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, subprocess.SubprocessError):
                    pass  # Previously established complete evidence still holds.
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


def _time(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _rows(project):
    _repo(project)
    try:
        lines = (fold.ROOT / "events" / "L-base-red-local.jsonl").read_text().splitlines()
    except (OSError, UnicodeError):
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if (row.get("project") == project and isinstance(row.get("ids"), list)
                    and all(isinstance(i, str) for i in row["ids"])
                    and re.fullmatch(r"[a-fA-F0-9]{7,40}", row.get("sha", ""))):
                _time(row["ts"])
                rows.append(row)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return rows


def _known_state(project):
    rows = sorted(_rows(project), key=lambda row: _time(row["ts"]))
    nightlies = [r for r in rows if r.get("type") == "base-red-nightly" and r.get("complete") is True]
    nightly = nightlies[-1] if nightlies else None
    known = {}
    if nightly:
        known.update({i: {"sha": nightly["sha"], "source": "nightly", "since": nightly["ts"]}
                      for i in nightly["ids"]})
    for row in rows:
        if (row.get("type") == "base-red" and row.get("source") == "rerun"
                and row.get("complete") is False
                and (nightly is None or _time(row["ts"]) >= _time(nightly["ts"]))):
            known.update({i: {"sha": row["sha"], "source": "rerun", "since": row["ts"]}
                          for i in row["ids"]})
    return known, nightly


def known_red(project):
    """Newest complete nightly plus subsequent base-attributed reruns."""
    return _known_state(project)[0]


def _append(event, project, sha, *, complete, ids, subject=None, **fields):
    argv = [str(DOIT), "append", event, subject or "base-" + project, "project=" + project,
            "sha=" + sha, "complete:=" + json.dumps(complete), "ids:=" + json.dumps(ids)]
    argv.extend(key + ":=" + json.dumps(value) for key, value in fields.items())
    proc = subprocess.run(argv, env={**os.environ, "DOIT_ROOT": str(fold.ROOT),
                                    "DOIT_LEDGER_FILE": "L-base-red-local.jsonl"},
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise ValueError("base-red ledger append failed")


def record(project, sha, ids, source):
    """Record attributable ids without claiming a complete base run."""
    if source not in ("nightly", "rerun"):
        raise ValueError("source must be nightly or rerun")
    _, sha = _project(project, sha)
    ids = list(ids)
    if not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be strings")
    _append("base-red", project, sha, complete=False, ids=sorted(set(ids)), source=source)


def id_kind(node):
    if node.startswith("pg-parity::"):
        return "pg-parity"
    if ".py::" in node:
        return "pytest"
    return "vitest" if "::" in node else "suite-error"


def classify(project, base_sha, ids, *, known_red=False, now=None, refresh=False):
    ids = list(ids)
    base = failing(project, base_sha, refresh=True) if refresh else failing(project, base_sha)
    undetermined = bool(base.get("undetermined"))
    needed = {id_kind(i) for i in ids}
    covered = set() if undetermined else set(base.get("kinds_covered", []))
    complete = not undetermined and (needed <= covered if ids else bool(base["complete"]))
    known = {i for bucket in base["ids"].values() for i in bucket
             if id_kind(i) in covered}
    if refresh:
        # Exact-SHA positive proof survives an unavailable refresh. Nightlies
        # never supply negative proof for unrelated ids when the API failed.
        known.update(i for bucket in base["ids"].values() for i in bucket)
        current = _time(now) if now is not None else datetime.now(timezone.utc)
        for row in _rows(project):
            if (row.get("type") == "base-red-nightly" and row.get("complete") is True
                    and row["sha"] == base["sha"] and _time(row["ts"]) <= current):
                known.update(row["ids"])
        if ids and set(ids) <= known:
            complete, undetermined = True, False
    recorded = {}
    if known_red:
        recorded, nightly = _known_state(project)
        current = _time(now) if now is not None else datetime.now(timezone.utc)
        if refresh:
            recorded = {i: meta for i, meta in recorded.items()
                        if meta["sha"] == base["sha"] and _time(meta["since"]) <= current}
        known.update(recorded)
        if not refresh:
            fresh = nightly is not None and timedelta(0) <= current - _time(nightly["ts"]) < timedelta(hours=36)
            complete = complete or fresh
        elif ids and set(ids) <= known:
            complete, undetermined = True, False
    result = {"pre_existing": [i for i in ids if i in known],
              "new": [i for i in ids if i not in known], "complete": complete,
              "needed": sorted(needed), "covered": sorted(covered),
              "uncovered": [i for i in ids if id_kind(i) not in covered],
              "undetermined": undetermined}
    if known_red:
        result["known_red"] = [i for i in ids if i in recorded]
    return result


def _nightly_rows(project):
    """Read dispatches raw: a malformed newest dispatch must not select an older one."""
    path = fold.ROOT / 'events' / 'L-base-red-local.jsonl'
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # append-only ledgers may have torn lines
        if isinstance(row, dict) and row.get('project') == project:
            rows.append(row)
    return rows


def _nightly_suite(repo, sha):
    source = window_gate.git(repo, 'show', f'{sha}:.github/workflows/python-tests.yml')
    if source.returncode or not source.stdout.strip():
        raise ValueError('master workflow unreadable')
    # Only a suite key beneath workflow_dispatch.inputs declares the input.
    parents = []
    for line in source.stdout.splitlines():
        match = re.match(r'^( *)([A-Za-z_][\w-]*)\s*:', line)
        if not match:
            continue
        indent, key = len(match[1]), match[2]
        while parents and parents[-1][0] >= indent:
            parents.pop()
        if key == 'suite' and [p[1] for p in parents][-2:] == ['workflow_dispatch', 'inputs']:
            return 'nightly'
        parents.append((indent, key))
    return 'undeclared'


def _pending(directory):
    return [(p, json.loads(p.read_text())) for p in sorted(directory.glob('*.pending.json'))]


def _resolve_pending(project, directory, *, sleep=time.sleep, polls=1):
    markers = _pending(directory)
    if not markers:
        return None
    sha = markers[0][1]['sha']
    if any(m['sha'] != sha or m['workflow'] not in WORKFLOWS for _, m in markers):
        raise ValueError('pending dispatch malformed')
    repo = _repo(project)
    owner = _owner(repo)
    for attempt in range(polls):
        runs = _api(f'repos/{owner}/actions/runs?head_sha={sha}&per_page=100', 'workflow_runs')
        found = {}
        for path, marker in markers:
            matches = [r for r in runs if r.get('head_sha') == sha
                       and r.get('name') == marker['workflow']
                       and r.get('event') == 'workflow_dispatch' and r.get('head_branch') == 'master'
                       and str(r.get('id', '')).isdigit()
                       and str(r['id']) not in marker['before_ids']
                       and _time(r['created_at']) >= _time(marker['dispatch_time'])]
            if matches:
                found[marker['workflow']] = str(min(matches, key=lambda r: int(r['id']))['id'])
            elif datetime.now(timezone.utc) - _time(marker['dispatch_time']) > timedelta(hours=6):
                path.rename(path.with_name(path.name + '.' + str(time.time_ns()) + '.expired'))
                raise ValueError('dispatch never appeared')
        if WORKFLOWS <= found.keys():
            rows = _nightly_rows(project)
            if not any(e.get('type') == 'base-red-nightly-dispatched' and e.get('sha') == sha for e in rows):
                _append('base-red-nightly-dispatched', project, sha, complete=False, ids=[],
                        run_ids=sorted(found.values()), suite=markers[0][1]['suite'])
            for path, _ in markers:
                path.unlink(missing_ok=True)
            return sha
        if attempt + 1 < polls:
            sleep(10)
    return None


_NIGHTLY_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TypeError, EOFError,
                   zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, subprocess.SubprocessError)


def nightly(project, *, sleep=None):
    """Dispatch a full master nightly once; persist intent before each paid call."""
    result = _result('')
    try:
        repo = _repo(project)
        directory = fold.ROOT / 'base-red' / project
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'nightly.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            markers = _pending(directory)
            if markers:
                resolved = _resolve_pending(project, directory)
                if resolved:
                    result.update(sha=resolved, message='nightly dispatched')
                    return result
                sha = markers[0][1]['sha']
                suite = markers[0][1]['suite']
                existing = {m['workflow'] for _, m in markers}
                if WORKFLOWS <= existing:
                    result['undetermined'].append('dispatch runs have not appeared')
                    return result
            else:
                window_gate.git(repo, 'fetch', 'origin', 'master')
                tip = window_gate.git(repo, 'rev-parse', 'origin/master').stdout.strip()
                repo, sha = _project(project, tip)
                result['sha'] = sha
                if any(e.get('type') == 'base-red-nightly-dispatched' and e.get('sha') == sha
                       for e in _nightly_rows(project)):
                    result['message'] = 'already dispatched'
                    return result
                suite = _nightly_suite(repo, sha)
                existing = set()
            result['sha'] = sha
            owner = _owner(repo)
            for name in sorted(WORKFLOWS - existing):
                if not ghlimit.gate(wait=False)['ok']:
                    raise ValueError('GitHub quota unavailable')
                runs = _api(f'repos/{owner}/actions/runs?head_sha={sha}&per_page=100', 'workflow_runs')
                marker = directory / (sha + '.' + WORKFLOW_FILES[name] + '.pending.json')
                marker.write_text(json.dumps(dict(sha=sha, workflow=name,
                    dispatch_time=datetime.now(timezone.utc).isoformat(),
                    before_ids=[str(r['id']) for r in runs], suite=suite)))
                argv = ['gh', 'workflow', 'run', WORKFLOW_FILES[name], '--repo', owner, '--ref', 'master']
                if name == 'Python Tests':
                    argv.extend(['-f', 'full_run=true'])
                    if suite == 'nightly':
                        argv.extend(['-f', 'suite=nightly'])
                rc, _, _ = ghlimit.run(argv, wait=False)
                if rc:
                    marker.unlink(missing_ok=True)
                    raise ValueError('nightly dispatch unavailable')
            resolved = _resolve_pending(project, directory, sleep=sleep or time.sleep, polls=6)
            if resolved:
                result['message'] = 'nightly dispatched'
            else:
                result['undetermined'].append('dispatch runs have not appeared')
        return result
    except _NIGHTLY_ERRORS as exc:
        result['undetermined'].append('dispatch never appeared' if str(exc) == 'dispatch never appeared'
                                      else 'nightly could not establish dispatch')
        return result


def nightly_collect(project):
    """Collect only the recorded dispatch, without fetching or resolving master."""
    result = _result('')
    try:
        repo = _repo(project)
        directory = fold.ROOT / 'base-red' / project
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'nightly.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _pending(directory) and not _resolve_pending(project, directory):
                result['waiting'] = True
                result['message'] = 'waiting for dispatch runs'
                return result
            rows = _nightly_rows(project)
            dispatched = [e for e in rows if e.get('type') == 'base-red-nightly-dispatched']
            if not dispatched:
                result['message'] = 'no nightly dispatched'
                return result
            event = dispatched[-1]
            sha, run_ids, suite = event['sha'], event['run_ids'], event['suite']
            if (not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', sha)
                    or not isinstance(run_ids, list) or len(run_ids) != len(WORKFLOWS)
                    or any(not isinstance(i, (str, int)) or not str(i).isdigit() for i in run_ids)
                    or len(set(map(str, run_ids))) != len(run_ids)
                    or suite not in ('nightly', 'undeclared')):
                raise ValueError('malformed nightly dispatch')
            dispatched_at = _time(event['ts'])
            result['sha'] = sha
            if any(e.get('type') == 'base-red-nightly' and e.get('sha') == sha
                   and e.get('complete') is True for e in rows):
                result['message'] = 'already collected'
                return result
            owner = _owner(repo)
            listed = _api(f'repos/{owner}/actions/runs?head_sha={sha}&per_page=100', 'workflow_runs')
            runs = [r for r in listed if str(r.get('id')) in set(map(str, run_ids))]
            if len(runs) != len(run_ids) or any(r.get('head_sha') != sha for r in runs):
                raise ValueError('dispatched runs missing or sha differs')
            if any(r.get('conclusion') == 'cancelled' for r in runs):
                raise ValueError('dispatched run cancelled')
            if any(r.get('status') != 'completed' for r in runs):
                if datetime.now(timezone.utc) - dispatched_at > timedelta(hours=24):
                    result['undetermined'].append('nightly overdue')
                else:
                    result['waiting'] = True
                    result['message'] = 'nightly runs queued or in progress'
                return result
            result, _ = _snapshot(repo, sha, runs=runs)
            if not result['complete'] or result['undetermined']:
                result['complete'] = False
                result['undetermined'].append('nightly snapshot incomplete')
                return result
            ids = sorted({i for values in result['ids'].values() for i in values})
            new = sorted(set(ids) - set(known_red(project)))
            alarms = [e for e in rows if e.get('type') == 'nightly-red' and e.get('sha') == sha]
            subject = 'nightly:' + project
            url = next(iter(result['sources']), '')
            if new or alarms:
                if not alarms:
                    _append('nightly-red', project, sha, complete=True, ids=new, subject=subject, url=url)
                if not any(e.get('type') == 'escalation-blocking' and e.get('subject') == subject
                           and e.get('sha') == sha for e in rows):
                    _append('escalation-blocking', project, sha, complete=True, ids=new, subject=subject,
                            default='Create a quarantine-row or fix spec with owner integrator for these nightly ids',
                            deadline=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                            revert='Remove the quarantine row after the integrator fix is verified')
            _append('base-red-nightly', project, sha, complete=True, ids=ids, url=url, suite=suite)
        return result
    except _NIGHTLY_ERRORS as exc:
        result['complete'] = False
        result['undetermined'].append('dispatch never appeared' if str(exc) == 'dispatch never appeared'
                                      else 'nightly collect could not establish evidence')
        return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["nightly"], ["nightly-collect"]):
        command = argv[0]
        parser = argparse.ArgumentParser(description="Dispatch or collect full master CI evidence")
        parser.add_argument("project")
        args = parser.parse_args(argv[1:])
        result = nightly(args.project) if command == 'nightly' else nightly_collect(args.project)
        state = 'undetermined' if result['undetermined'] else result.get('message', 'complete')
        print(f'{args.project} {command}: {state}')
        for reason in result['undetermined']:
            print(reason)
        return 2 if result['undetermined'] else 3 if result.get('waiting') else 0
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
