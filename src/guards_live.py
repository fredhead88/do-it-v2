#!/usr/bin/env python3
"""Read-only measurement of the three master safety evidence streaks."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import re

import fold
import ledger_files


STREAMS = {"r1": "selected-canary", "r2": "master-full-run", "r3": "base-red-nightly"}


def _time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp")
    return stamp.astimezone(timezone.utc)


def _events():
    """Use virtual members and the fold's corrections without its writable cache.

    The ordinary fold tolerates torn rows; a safety measurement cannot skip a
    possibly red record. Validate the source before applying the same operator
    corrections, retaining the original filename-derived identity.
    """
    directory = fold.ROOT / "events"
    ledger_files.assert_readable(directory)
    if ledger_files.problems(directory):
        raise ValueError("corrupt source")
    rows = []
    for name in ledger_files.names(directory):
        for number, line in enumerate(ledger_files.read_text(directory, name).splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("malformed source")
            row["actor"], row["_src"] = fold.actor_of(name), f"{name}:{number}"
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("ts", "")))
    return fold.apply_corrections(rows)


def _run_id(value):
    return ((type(value) is int and value > 0)
            or (isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None
                and int(value) > 0))


def _observation(row, guard):
    """Return validated run identities and a green/red/undetermined reason."""
    if not isinstance(row.get("sha"), str) or not re.fullmatch(r"[a-fA-F0-9]{7,40}", row["sha"]):
        return [], "malformed evidence"
    ids = row.get("ids")
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i.strip() for i in ids):
        return [], "malformed evidence"
    runs = [row.get("run_id")] if guard == "r1" else row.get("run_ids")
    if not isinstance(runs, list) or not runs or not all(_run_id(rid) for rid in runs):
        return [], "malformed evidence"
    runs = [str(rid) for rid in runs]
    if guard in ("r1", "r2") and type(row.get("green")) is not bool:
        return runs, "undetermined green status"
    if guard in ("r2", "r3") and row.get("complete") is not True:
        return runs, "incomplete evidence"
    if guard == "r3":
        if row.get("suite") != "nightly":
            return runs, "nightly suite not declared"
        return runs, "red evidence" if ids else None
    return runs, None if row["green"] else "red evidence"


def _streak(rows, guard, now):
    admitted = fold.EMITS.get(STREAMS[guard])
    if not admitted:
        return False, "missing producer registration"
    observations, seen = [], {}
    for row in rows:
        if row.get("type") != STREAMS[guard] or row["actor"] not in admitted:
            continue
        try:
            stamp = _time(row.get("ts"))
        except (ValueError, TypeError, OverflowError):
            return False, "invalid timestamp"
        if stamp > now:
            return False, "future evidence"
        runs, reason = _observation(row, guard)
        item = [stamp, reason]
        # Compare evidence fields, excluding envelopes and diagnostic decoration.
        signature = json.dumps({k: row.get(k) for k in
                                ("ts", "sha", "ids", "green", "complete", "suite", "run_id", "run_ids")},
                               sort_keys=True)
        for rid in runs:
            previous = seen.setdefault(rid, [])
            if any(old_signature != signature for old_signature, _ in previous):
                item[1] = "contradictory run records"
                for _, old_item in previous:
                    old_item[1] = "contradictory run records"
            previous.append((signature, item))
        observations.append(item)
    oldest, reason = None, "missing producer evidence"
    # Green first at a shared instant, so red/unknown wins independent of order.
    for stamp, failure in sorted(observations, key=lambda item: (item[0], item[1] is not None)):
        if failure:
            oldest, reason = None, failure
        elif oldest is None:
            oldest, reason = stamp, "green history shorter than 24 hours"
    return (True, None) if oldest is not None and now - oldest >= timedelta(hours=24) else (False, reason)


def check(project, now=None):
    """Return exactly r1/r2/r3/live/why; never acquire or mutate evidence."""
    result = dict(r1=False, r2=False, r3=False, live=False, why=[])

    def failed(reason):
        result.update(r1=False, r2=False, r3=False, live=False)
        result["why"] = [f"{guard}: {reason}" for guard in STREAMS]
        return result

    if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
        return failed("invalid project")
    try:
        if not (fold.ROOT / "repos" / project).is_dir():
            return failed("unknown project")
        if now is None:
            now = datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return failed("invalid clock")
        now = now.astimezone(timezone.utc)
        rows = [row for row in _events() if row.get("project") == project]
        for guard in STREAMS:
            result[guard], reason = _streak(rows, guard, now)
            if reason:
                result["why"].append(f"{guard}: {reason}")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        return failed("evidence source unreadable or corrupt")
    result["live"] = all(result[guard] for guard in STREAMS)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    result = check(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["live"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
