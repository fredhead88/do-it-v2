#!/usr/bin/env python3
"""Read-only report on merge CI: selection mode, wall time, canary, billed minutes.

Usage: python3 src/merge_ci_report.py <project> [--n 20] [--json]
Exit 0 when every section is determined, 3 when any is not, 2 on usage errors.
Unknown is reported as unknown (None / "undetermined"), never as zero.
"""
import argparse
import collections
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone

import base_red
import ghlimit
import guards_live

BASELINE_MINUTES = 29.2
WORKFLOW = "python-tests.yml"
MAX_N = 100
# Restated from albert-scott scripts/ci/ci_selection.py (LINE); that repo is not importable here.
SELECTION = re.compile(r"CI-SELECTION mode=(full|selected|rerun) reason=([\w-]+) files=(\d+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+")
HOSTED = ("ubuntu-", "windows-", "macos-")


def _default_fetch(argv):
    return ghlimit.run(argv, wait=False)


def _slug(value):
    if not isinstance(value, str):
        return None
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:40] or None


def _why(rc, err):
    """A fixed-shape reason for a failed read; never raw API text."""
    m = re.search(r"HTTP (\d{3})", err or "")
    if m:
        return f"HTTP {m[1]}"
    if rc == 75:
        return "quota low"
    return f"gh exit {rc}"


def _get(fetch, path, key):
    """Return (rows, reason). rows is None whenever the read was not determined."""
    try:
        rc, out, err = fetch(["gh", "api", path])
    except Exception:
        return None, "read failed"
    if rc:
        return None, _why(rc, err)
    try:
        body = json.loads(out)
    except (TypeError, ValueError):
        return None, "unreadable JSON"
    rows = body.get(key) if isinstance(body, dict) else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        return None, "unexpected JSON shape"
    return rows, None


def parse_selection(members):
    """Return (mode, reason, files, why) from run-log members of (name, text)."""
    found = set()
    try:
        for _, text in members:
            for raw in text.splitlines():
                line = ANSI.sub("", raw).strip().split("\t")[-1].strip()
                line = TIMESTAMP.sub("", line)
                if not line.startswith("CI-SELECTION"):
                    continue
                if not SELECTION.fullmatch(line):
                    return "undetermined", "malformed CI-SELECTION line", None, "malformed CI-SELECTION line"
                found.add(line)
    except Exception:
        return "undetermined", "logs unreadable", None, "logs unreadable"
    if len(found) > 1:
        return "undetermined", "conflicting CI-SELECTION lines", None, "conflicting CI-SELECTION lines"
    if not found:
        return "undetermined", "no CI-SELECTION line", None, "no CI-SELECTION line"
    mode, reason, files = SELECTION.fullmatch(next(iter(found))).groups()
    return mode, reason, int(files), None


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def _job_bill(job):
    """Return (minutes, status, reason) for one job; minutes None when unknown."""
    labels = job.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        return None, "undetermined", "runner labels missing"
    if "self-hosted" in labels or any(x.startswith("ubicloud-") for x in labels):
        return 0, "not-github-billed", None
    if not any(x.startswith(HOSTED) for x in labels):
        return None, "undetermined", "runner labels unrecognised"
    start, end = _time(job.get("started_at")), _time(job.get("completed_at"))
    if start is None or end is None or end < start:
        return None, "undetermined", "job timestamps missing"
    return math.ceil((end - start).total_seconds() / 60), "billed", None


def _run_money(jobs):
    """Return (wall_seconds, billed_minutes, billed_status, reason) for one run."""
    stamps = [(_time(j.get("started_at")), _time(j.get("completed_at"))) for j in jobs]
    starts = [s for s, _ in stamps if s is not None]
    ends = [e for _, e in stamps if e is not None]
    wall = (max(ends) - min(starts)).total_seconds() if starts and ends else None
    if wall is not None and wall < 0:
        wall = None
    if not jobs:
        return wall, None, "undetermined", "no jobs"
    bills = [_job_bill(j) for j in jobs]
    bad = [b for b in bills if b[0] is None]
    if bad:
        return wall, None, "undetermined", bad[0][2]
    minutes = sum(b[0] for b in bills)
    status = "billed" if any(b[1] == "billed" for b in bills) else "not-github-billed"
    return wall, minutes, status, None


def _canary(project, run_ids, shas):
    base = {"status": "undetermined", "count": None, "misses": None, "undetermined": None,
            "reason": None, "miss_rows": []}
    try:
        events = guards_live._events()
    except Exception:
        return {**base, "reason": "ledger unreadable"}
    count = misses = unknown = 0
    miss_rows = []
    for row in events:
        if row.get("type") != "selected-canary" or row.get("schema") != 2:
            continue
        if row.get("project") != project:
            continue
        ids = {str(row.get("run_id")), str(row.get("full_run_id"))}
        if not (ids & run_ids or row.get("sha") in shas):
            continue
        if row.get("complete") is not True or not isinstance(row.get("ids"), list):
            unknown += 1
            continue
        count += 1
        if row["ids"]:
            misses += 1
            miss_rows.append({"sha": row.get("sha"), "ids": [str(x) for x in row["ids"]]})
    out = {"status": "recorded", "count": count, "misses": misses, "undetermined": unknown,
           "reason": None, "miss_rows": miss_rows}
    if unknown:
        out.update(status="undetermined", reason=f"{unknown} incomplete canary row(s)")
    elif not count:
        out["status"] = "none-recorded"
    return out


def report(project, n=20, *, fetch=None, logs=None, owner=None):
    if type(n) is not int or not 1 <= n <= MAX_N:
        raise ValueError(f"n must be an integer from 1 to {MAX_N}")
    if owner is None:
        owner = base_red._owner(base_red._repo(project))
    fetch = fetch or _default_fetch
    logs = logs or base_red._logs
    undetermined = []
    out = {"project": project, "n": n, "baseline_minutes": BASELINE_MINUTES, "runs": [],
           "summary": {}, "canary": {}, "billed": {}, "undetermined": undetermined}

    listed, why = _get(fetch, f"repos/{owner}/actions/workflows/{WORKFLOW}/runs"
                       f"?status=completed&per_page={n}", "workflow_runs")
    if listed is None:
        undetermined.append(f"run listing unavailable: {why}")
        listed = []
    listed = [r for r in listed if r.get("status") in (None, "completed")]
    listed.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    runs, reasons = [], []
    for item in listed:
        if len(runs) == n:
            break
        rid, sha = str(item.get("id")), item.get("sha") or item.get("head_sha")
        if not rid.isdigit() or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
            undetermined.append("a listed run had a malformed id or sha")
            continue
        jobs, jwhy = _get(fetch, f"repos/{owner}/actions/runs/{rid}/jobs?per_page=100", "jobs")
        if jobs is None:
            wall, minutes, status, bwhy = None, None, "undetermined", jwhy
            undetermined.append(f"jobs unavailable for run {rid}: {jwhy}")
        else:
            wall, minutes, status, bwhy = _run_money(jobs)
            if status == "undetermined":
                undetermined.append(f"billed minutes undetermined for run {rid}: {bwhy}")
        try:
            mode, reason, files, mwhy = parse_selection(logs(owner, rid))
        except Exception:
            mode, reason, files, mwhy = "undetermined", "logs unavailable", None, "logs unavailable"
        if mwhy:
            undetermined.append(f"selection mode undetermined for run {rid}: {mwhy}")
        runs.append({"run_id": rid, "sha": sha, "event": _slug(item.get("event")),
                     "conclusion": _slug(item.get("conclusion")), "mode": mode, "reason": reason,
                     "files": files, "wall_seconds": wall, "billed_minutes": minutes,
                     "billed_status": status})
        if status == "undetermined":
            reasons.append(bwhy)
    out["runs"] = runs

    walls = [r["wall_seconds"] for r in runs if r["wall_seconds"] is not None]
    median = statistics.median(walls) if walls else None
    out["summary"] = {
        "median_wall_seconds": median,
        "delta_vs_baseline_seconds": None if median is None else median - BASELINE_MINUTES * 60,
        "runs_counted": len(walls), "runs_excluded": len(runs) - len(walls),
        "mode_counts": dict(collections.Counter(r["mode"] for r in runs))}
    if median is None:
        undetermined.append("median wall time undetermined: no run has job timestamps")

    if not runs:
        out["canary"] = {"status": "undetermined", "count": None, "misses": None,
                         "undetermined": None, "reason": "no runs listed", "miss_rows": []}
        out["billed"] = {"status": "undetermined", "minutes": None, "reason": "no runs listed"}
    else:
        out["canary"] = _canary(project, {r["run_id"] for r in runs}, {r["sha"] for r in runs})
        if reasons:
            out["billed"] = {"status": "undetermined", "minutes": None,
                             "reason": "; ".join(dict.fromkeys(reasons))}
        else:
            total = sum(r["billed_minutes"] for r in runs)
            kind = "billed" if any(r["billed_status"] == "billed" for r in runs) else "not-github-billed"
            out["billed"] = {"status": kind, "minutes": total, "reason": None}
    if out["canary"]["status"] == "undetermined":
        undetermined.append(f"canary undetermined: {out['canary']['reason']}")
    out["undetermined"] = list(dict.fromkeys(undetermined))
    return out


def _unknown(value, fmt="{}"):
    return "unknown" if value is None else fmt.format(value)


def _minutes(seconds, fmt):
    return _unknown(None if seconds is None else seconds / 60, fmt)


def render(rep):
    lines = [f"merge CI report: {rep['project']} (last {rep['n']} python-tests runs, newest first)"]
    for r in rep["runs"]:
        lines.append("{} {} {} mode={} reason={} files={} wall={} billed={} ({})".format(
            r["run_id"], r["sha"][:7], r["event"] or "unknown", r["mode"], r["reason"],
            _unknown(r["files"]), _minutes(r["wall_seconds"], "{:.1f}m"),
            _unknown(r["billed_minutes"], "{}m"), r["billed_status"]))
    s = rep["summary"]
    lines.append("median wall: {} vs baseline {} min, delta {}".format(
        _minutes(s["median_wall_seconds"], "{:.1f} min"), rep["baseline_minutes"],
        _minutes(s["delta_vs_baseline_seconds"], "{:+.1f} min")))
    c = rep["canary"]
    lines.append("canary: status={} count={} misses={} undetermined={}{}".format(
        c["status"], _unknown(c["count"]), _unknown(c["misses"]), _unknown(c["undetermined"]),
        f" ({c['reason']})" if c["reason"] else ""))
    for m in c.get("miss_rows", []):
        lines.append(f"  miss: sha={m['sha']} ids={','.join(m['ids'])}")
    b = rep["billed"]
    lines.append("billed: status={} minutes={}{}".format(
        b["status"], _unknown(b["minutes"]), f" ({b['reason']})" if b["reason"] else ""))
    for u in rep["undetermined"]:
        lines.append(f"undetermined: {u}")
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"merge_ci_report: {message}", file=sys.stderr)
        raise SystemExit(2)


def main(argv=None, *, fetch=None, logs=None, owner=None):
    parser = _Parser(prog="merge_ci_report.py", add_help=True)
    parser.add_argument("project")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    try:
        rep = report(args.project, args.n, fetch=fetch, logs=logs, owner=owner)
    except (ValueError, OSError) as error:
        print(f"merge_ci_report: {(str(error).splitlines() or ['error'])[0][:120]}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    return 0 if not rep["undetermined"] else 3


if __name__ == "__main__":
    sys.exit(main())
