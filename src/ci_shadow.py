#!/usr/bin/env python3
"""Compare hosted ci-verify runs with self-hosted shadows, one bounded pass."""
import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import base_red
import fold
import ghlimit
import window_gate

DOIT = Path(__file__).resolve().parents[1] / "doit"
PG = "pg-parity (Postgres)"
CI_EXCLUDED = {PG, "Auth Coverage · Secret Scan · CVE Check", "Secret shape scan"}
MAX_INFLIGHT = 3


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return result


def compare(hosted, shadow):
    by_check, differences, unknown = {}, [], []
    for name in sorted(hosted["checks"].keys() & shadow["checks"].keys()):
        h, s = hosted["checks"][name], shadow["checks"][name]
        hi, si = h["ids"], s["ids"]
        by_check[name] = {"hosted": sorted(hi) if hi is not None else None,
                          "shadow": sorted(si) if si is not None else None,
                          "hosted_conclusion": h["conclusion"],
                          "shadow_conclusion": s["conclusion"]}
        if h["infra"] or s["infra"]:
            steps = [side.get("failed_step") or "unknown-step" for side in (h, s) if side["infra"]]
            differences.append(f"{name}: infrastructure failure: {', '.join(steps)}")
        elif hi is None or si is None:
            unknown.append(f"{name}: log-undetermined")
        elif hi != si:
            differences.append(f"{name}: ids hosted={sorted(hi)} shadow={sorted(si)}")
        elif h["conclusion"] != s["conclusion"]:
            differences.append(f"{name}: conclusions {h['conclusion']} != {s['conclusion']}")
    if differences:
        identical, reason = False, "; ".join(differences)
    elif unknown:
        identical, reason = None, "; ".join(unknown)
    elif not by_check:
        identical, reason = None, "no-common-checks"
    else:
        identical, reason = True, ""
    return {"identical": identical, "by_check": by_check, "reason": reason}


def day(events, now, kind):
    comparisons = sorted((event for event in events if event.get("type") == "ci-shadow"
                          and event.get("kind") == kind and timestamp(event["ts"]) <= now),
                         key=lambda event: timestamp(event["ts"]))
    emitted = {event["to"] for event in events
               if event.get("type") == "ci-shadow-passed" and event.get("kind") == kind}
    for end in reversed(comparisons):
        stop = timestamp(end["ts"])
        start = stop - timedelta(hours=24)
        span = [event for event in comparisons if start <= timestamp(event["ts"]) <= stop]
        if end["ts"] not in emitted and len(span) >= 5 and all(e.get("identical") is True for e in span):
            return {"kind": kind, "from": start.isoformat(), "to": end["ts"],
                    "n": len(span), "shas": [event["sha"] for event in span]}
    return None


def _gh(*args):
    rc, out, _ = ghlimit.run(["gh", *args], wait=False)
    if rc:
        raise RuntimeError(f"GitHub {args[0]} unavailable (exit {rc})")
    return out


def _rows(path, key):
    try:
        # The installed gh predates --slurp; --paginate emits consecutive JSON
        # documents. Decode each page without depending on that newer option.
        source = _gh("api", path, "--paginate").lstrip()
        decoder, pages = json.JSONDecoder(), []
        while source:
            page, end = decoder.raw_decode(source)
            pages.append(page)
            source = source[end:].lstrip()
        if not pages:
            raise ValueError("missing pages")
        result = []
        for page in pages:
            rows = page[key]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid rows")
            result.extend(rows)
        return result
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"{key} API unreadable") from exc


def _owner(repo):
    remote = window_gate.git(repo, "remote", "get-url", "origin").stdout.strip()
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:"):]
    else:
        url = urlsplit(remote)
        if url.hostname != "github.com" or url.scheme not in ("https", "ssh"):
            raise RuntimeError("origin is not GitHub")
        path = url.path.lstrip("/")
    path = path.removesuffix("/").removesuffix(".git")
    if len(path.split("/")) != 2 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-" for c in path):
        raise RuntimeError("origin is not a GitHub repository")
    return path


def _base():
    main = os.environ.get("DOIT_MAIN")
    if not main:
        import look
        import push_origin
        main = dict(push_origin.targets(look.load_toml())).get("albert-scott")
    return main or "master"


def _runs(owner):
    return _rows(f"repos/{owner}/actions/runs?event=workflow_dispatch&per_page=100", "workflow_runs")


def _jobs(owner, run):
    rid = run["id"]
    if isinstance(rid, bool) or not str(rid).isdigit():
        raise RuntimeError("invalid run id")
    return _rows(f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")


def _sha(value):
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _workflow(run):
    path = run.get("path", "").split("@", 1)[0]
    if not path.startswith(".github/workflows/") or len(Path(path).parts) != 3 or not path.endswith((".yml", ".yaml")):
        raise RuntimeError("workflow path unreadable")
    return path


def _has_check(name, jobs):
    return any(job["name"] == name or (name == "pytest" and job["name"].startswith(("pytest (", "pytest — ")))
               for job in jobs)


def candidates(now, *, events=None, runs=None, repo=None, owner=None):
    repo = repo or fold.ROOT / "repos" / "albert-scott"
    owner = owner or _owner(repo)
    events = fold.read_events() if events is None else events
    runs = _runs(owner) if runs is None else runs
    seen = {event["sha"] for event in events if event.get("type") == "ci-shadow"}
    result = []
    for run in sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True):
        ref, sha = run.get("head_branch", ""), run.get("head_sha")
        if (not ref.startswith("ci-verify/") or ref.startswith("ci-verify/shadow-")
                or not _sha(sha) or sha in seen or _state_path(sha).exists()
                or run.get("event") != "workflow_dispatch" or run.get("status") != "completed"
                or Path(_workflow(run)).name != "python-tests.yml"
                or not now - timedelta(hours=24) <= timestamp(run["created_at"]) <= now):
            continue
        jobs = _jobs(owner, run)
        if not jobs or any("labels" not in job or not isinstance(job["labels"], list) for job in jobs):
            raise RuntimeError("hosted runner labels unreadable")
        if any("as-ci" in job["labels"] for job in jobs):
            continue
        seen.add(sha)
        hosted = {}
        for other in sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True):
            if (other.get("head_branch") == ref and other.get("head_sha") == sha
                    and other.get("event") == "workflow_dispatch" and other.get("status") == "completed"):
                hosted.setdefault(_workflow(other), other)
        candidate = {"sha": sha, "ref": f"ci-verify/shadow-{sha[:12]}", "hosted": list(hosted.values()),
                     "tree": "unknown", "reason": "", "owner": owner}
        if window_gate.git(repo, "cat-file", "-e", sha, check=False).returncode:
            candidate["reason"] = "object-missing"
        else:
            candidate["tree"] = window_gate.git(repo, "rev-parse", sha + "^{tree}").stdout.strip()
            # An unreadable requirement is a retryable pass failure, never a terminal record.
            required = window_gate.required_checks(repo, _base())
            all_jobs = [job for h in hosted.values() for job in _jobs(owner, h)]
            missing = [name for name in required if not _has_check(name, all_jobs)]
            if missing:
                candidate["reason"] = "hosted-missing:" + missing[0]
        result.append(candidate)
    return result


def _inputs(source):
    """Read block-mapping input keys conservatively; never execute workflow YAML."""
    parents, inputs = [], set()
    for raw in source.splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while parents and parents[-1][0] >= indent:
            parents.pop()
        key, colon, _ = text.partition(":")
        key = key.strip("'\"")
        if not colon:
            continue
        if [parent[1] for parent in parents] == ["on", "workflow_dispatch", "inputs"]:
            inputs.add(key)
        parents.append((indent, key))
    return inputs


def _state_path(sha):
    if not _sha(sha):
        raise RuntimeError("invalid state sha")
    return fold.ROOT / "state" / "ci-shadow" / (sha[:12] + ".json")


def _store(state):
    path = _state_path(state["sha"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def append(typ, fields):
    argv = [str(DOIT), "append", typ, "ci-shadow"]
    argv.extend(f"{key}:={json.dumps(value)}" for key, value in fields.items())
    proc = subprocess.run(argv, env={**os.environ, "DOIT_ROOT": str(fold.ROOT),
                                    "DOIT_LEDGER_FILE": "L-ci-shadow-local.jsonl"},
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("ci-shadow ledger append failed")


def _record(state, kind, result, shadow=()):
    # A crash after append but before state cleanup must not duplicate evidence.
    if any(e.get("type") == "ci-shadow" and e.get("sha") == state["sha"] and e.get("kind") == kind
           for e in fold.read_events()):
        return 0
    append("ci-shadow", {"sha": state["sha"], "tree": state["tree"], "kind": kind,
                         "hosted_runs": [r["html_url"] for r in state["hosted"]],
                         "shadow_runs": [r["html_url"] for r in shadow], **result})
    return 1


def _unknown(reason):
    return {"identical": None, "by_check": {}, "reason": reason}


def start(candidate, repo, now):
    paths = sorted({_workflow(run) for run in candidate["hosted"]})
    options = {}
    for path in paths:
        source = window_gate.git(repo, "show", f"{candidate['sha']}:{path}", check=False)
        options[path] = _inputs(source.stdout) if not source.returncode else set()
        if "runner" not in options[path]:
            return _record(candidate, "ci", _unknown("no-runner-input:" + path)), False
    if not paths:
        return _record(candidate, "ci", _unknown("no-runner-input:<none>")), False
    state = {**candidate, "created_at": now.isoformat(), "paths": paths, "dispatched": [],
             "pg": any(Path(path).name == "python-tests.yml" and "pg_runner" in keys
                       for path, keys in options.items()), "options": {p: sorted(v) for p, v in options.items()}}
    _store(state)
    window_gate.git(repo, "push", "--no-verify", "origin", f"{state['sha']}:refs/heads/{state['ref']}")
    _dispatch(state)
    return 0, True


def _dispatch(state):
    for path in state["paths"]:
        if path in state["dispatched"]:
            continue
        args = ["workflow", "run", path, "--repo", state["owner"], "--ref", state["ref"], "-f", "runner=self-hosted"]
        if Path(path).name == "python-tests.yml":
            args.extend(["-f", "full_run=true"])
        if "pg_runner" in state["options"][path]:
            args.extend(["-f", "pg_runner=self-hosted"])
        _gh(*args)
        state["dispatched"].append(path)
        _store(state)


def _logs(owner, run):
    if not ghlimit.gate(wait=False)["ok"]:
        raise ValueError("log quota unavailable")
    proc = subprocess.run([ghlimit.real_gh(), "api", f"repos/{owner}/actions/runs/{run['id']}/logs"],
                          capture_output=True, timeout=120)
    if proc.returncode:
        raise ValueError("logs unavailable")
    with zipfile.ZipFile(io.BytesIO(proc.stdout)) as archive:
        return [(member.filename, archive.read(member).decode("utf-8-sig"))
                for member in archive.infolist() if not member.is_dir()]


def _side(owner, runs, kind):
    checks = {}
    for run in runs:
        jobs = _jobs(owner, run)
        try:
            members = _logs(owner, run)
        except (OSError, ValueError, zipfile.BadZipFile, zlib.error, subprocess.TimeoutExpired):
            members = []
        for job in jobs:
            name = job["name"]
            if (kind == "ci" and name in CI_EXCLUDED) or (kind == "pg" and name != PG):
                continue
            # Actions zips contain <job>/<step>.txt plus a root aggregate.
            # Only the job directory is used, preventing aggregate double reads.
            texts = []
            for member, text in members:
                if member.split("/", 1)[0] != name or "/" not in member:
                    continue
                if name == PG:
                    # parse_log owns pg-parity recognition, including raw pytest
                    # nodes and step failures. Supply the zip's step context.
                    ordinal, _, title = Path(member).stem.partition("_")
                    step = next((s["name"] for s in job.get("steps", [])
                                 if str(s.get("number")) == ordinal), title or "unknown-step")
                    text = "\n".join(f"pg-parity\t{step}\t{line}" for line in text.splitlines())
                texts.append(text)
            parsed = base_red.parse_log("\n".join(texts)) if texts else None
            ids = None if parsed is None or parsed["undetermined"] else set().union(
                *(parsed[kind] for kind in ("pytest", "pg-parity", "vitest", "suite-error")))
            failed = [step["name"] for step in job.get("steps", []) if step.get("conclusion") == "failure"]
            conclusion = job.get("conclusion")
            # Known failure ids distinguish test failures from setup failures.
            infra = (conclusion in ("timed_out", "startup_failure", "action_required")
                     or any(step.lower().startswith(("set up", "setup", "install ", "checkout", "check out", "restore "))
                            for step in failed))
            if conclusion not in ("success", "skipped", "failure") or (conclusion == "failure" and not ids and not infra):
                ids = None
            checks[name] = {"conclusion": conclusion, "ids": ids, "infra": infra,
                            "failed_step": ", ".join(failed) or None}
    return {"runs": [run["html_url"] for run in runs], "checks": checks}


def collect(state, repo, now, runs):
    shadow, matching = {}, []
    for run in sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True):
        if (run.get("head_branch") == state["ref"] and run.get("head_sha") == state["sha"]
                and run.get("event") == "workflow_dispatch" and _workflow(run) in state["paths"]):
            matching.append(run)
            shadow.setdefault(_workflow(run), run)
    timed_out = now - timestamp(state["created_at"]) >= timedelta(hours=6)
    if not timed_out and (len(shadow) != len(state["paths"]) or any(r["status"] != "completed" for r in matching)):
        return 0, False
    recorded = 0
    for kind in (["ci", "pg"] if state["pg"] else ["ci"]):
        if timed_out:
            result = _unknown("shadow-timeout")
        else:
            try:
                result = compare(_side(state["owner"], state["hosted"], kind),
                                 _side(state["owner"], list(shadow.values()), kind))
            except RuntimeError:
                result = _unknown("runs-api-unreadable")
        recorded += _record(state, kind, result, shadow.values())
    if timed_out:
        for run in matching:
            if run["status"] != "completed":
                _gh("run", "cancel", str(run["id"]), "--repo", state["owner"])
    window_gate.git(repo, "push", "--no-verify", "origin", ":refs/heads/" + state["ref"])
    _state_path(state["sha"]).unlink()
    return recorded, True


def run_pass(*, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    summary = {"started": 0, "collected": 0, "recorded": 0, "passed": 0}
    if dry_run:
        # A dry run intentionally does not even create the lock/state directories.
        for candidate in candidates(now):
            print(f"ci-shadow: would compare {candidate['sha']}: {candidate['reason'] or 'ready'}", file=sys.stderr)
        return summary
    lock_path = fold.ROOT / "state" / "ci-shadow.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return summary
        repo = fold.ROOT / "repos" / "albert-scott"
        owner, directory = _owner(repo), fold.ROOT / "state" / "ci-shadow"
        runs = _runs(owner)
        for path in sorted(directory.glob("*.json")):
            state = json.loads(path.read_text())
            if now - timestamp(state["created_at"]) < timedelta(hours=6):
                # Retry a failed push/dispatch from persisted state before polling.
                if len(state["dispatched"]) < len(state["paths"]):
                    window_gate.git(repo, "push", "--no-verify", "origin", f"{state['sha']}:refs/heads/{state['ref']}")
                    _dispatch(state)
            recorded, completed = collect(state, repo, now, runs)
            summary["recorded"] += recorded
            summary["collected"] += int(completed)
        for candidate in candidates(now, runs=runs, repo=repo, owner=owner):
            if candidate["reason"]:
                summary["recorded"] += _record(candidate, "ci", _unknown(candidate["reason"]))
            elif len(list(directory.glob("*.json"))) < MAX_INFLIGHT:
                recorded, started = start(candidate, repo, now)
                summary["recorded"] += recorded
                summary["started"] += int(started)
        for kind in ("ci", "pg"):
            passed = day(fold.read_events(), now, kind)
            if passed:
                append("ci-shadow-passed", passed)
                summary["passed"] += 1
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_pass(dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"ci-shadow: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else "ci-shadow: " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
