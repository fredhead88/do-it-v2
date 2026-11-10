#!/usr/bin/env python3
"""One read-only predicate: may merges run selected tests right now?

Computed only from `selected-canary` ledger rows (schema 2). Undetermined is
never allowed: every path that cannot prove the answer returns allowed=false.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess

import fold
import guards_live

PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA = re.compile(r"[a-fA-F0-9]{7,40}")


def _result(allowed, reason, since=None):
    return {"allowed": allowed, "reason": reason, "since": since}


def _undetermined():
    return _result(False, "undetermined")


def _classify(row):
    """green, miss or undetermined, per R1 over guards_live._observation."""
    _, reason = guards_live._observation(row, "r1")
    complete, ids = row.get("complete"), row.get("ids")
    if reason is None and complete is True and ids == []:
        return "green"
    if reason == "red evidence" and complete is True and isinstance(ids, list) and ids:
        return "miss"
    return "undetermined"


def _ancestor(repo, older, newer):
    """True/False from `merge-base --is-ancestor`; None when it cannot be decided."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode in (0, 1):
        return done.returncode == 0
    return None


def _descends(repo, miss_sha, green_sha):
    """Strict descent: a different commit that has the miss as an ancestor."""
    if miss_sha.lower() == green_sha.lower():
        return False
    forward = _ancestor(repo, miss_sha, green_sha)
    if not forward:
        return forward
    back = _ancestor(repo, green_sha, miss_sha)  # abbreviated spelling of the same commit
    return None if back is None else not back


def _compute(project, now):
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        return _undetermined()
    repo = fold.ROOT / "repos" / project
    if not repo.is_dir():
        return _undetermined()
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return _undetermined()
    now = now.astimezone(timezone.utc)
    admitted = fold.EMITS.get("selected-canary")
    if not admitted:
        return _undetermined()

    items = []
    for row in guards_live._events():
        if (row.get("type") != "selected-canary" or row.get("project") != project
                or row.get("actor") not in admitted
                or type(row.get("schema")) is not int or row["schema"] != 2):
            continue
        stamp = guards_live._time(row.get("ts"))  # ValueError -> undetermined
        if stamp > now:
            return _undetermined()
        items.append(dict(ts=stamp, raw=row["ts"], kind=_classify(row), row=row))
    if not items:
        return _result(False, "no-canary")

    # Green first at a shared instant; a green never clears an equal-ts row.
    items.sort(key=lambda item: (item["ts"], item["kind"] != "green"))
    misses, unknown, greens = [], [], []
    for item in items:
        if item["kind"] == "miss":
            item["unprovable"] = False
            misses.append(item)
        elif item["kind"] == "undetermined":
            unknown.append(item)
        else:
            greens.append(item)
            unknown = [u for u in unknown if not u["ts"] < item["ts"]]
            remaining = []
            for miss in misses:
                if not miss["ts"] < item["ts"]:
                    remaining.append(miss)
                    continue
                answer = _descends(repo, miss["row"]["sha"], item["row"]["sha"])
                miss["unprovable"] = answer is None
                if not answer:
                    remaining.append(miss)
            misses = remaining
    if unknown or any(m["unprovable"] for m in misses):
        return _undetermined()
    if misses:
        return _result(False, "canary-miss", misses[0]["raw"])
    if greens:
        return _result(True, "canary-green", greens[-1]["raw"])
    return _undetermined()


def allowed(project, now=None):
    """Return {"allowed", "reason", "since"}; never raises, never writes."""
    try:
        return _compute(project, now)
    except Exception:  # undetermined is never clean
        return _undetermined()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    result = allowed(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
