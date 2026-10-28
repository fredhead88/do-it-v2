"""Dated, ledger-only quarantine of repeat-blamed and base-red test ids."""
from datetime import datetime, timedelta, timezone
import json
import os

import fold

BLAME_WINDOWS = 3
BLAME_HOURS = 48
QUARANTINE_DAYS = 7
GREEN_WINDOWS = 2


def _time(value):
    return (value if isinstance(value, datetime) else
            datetime.fromisoformat(value.replace("Z", "+00:00"))).astimezone(timezone.utc)


def _latest(events, project, now):
    rows = {}
    for _, e in sorted(enumerate(events), key=lambda p: (_time(p[1]["ts"]), p[0])):
        if (e.get("project") == project and e.get("actor") == "base-red"
                and e.get("type") in ("test-quarantined", "test-unquarantined")
                and _time(e["ts"]) <= now):
            rows[e["node"]] = e
    return {n: e for n, e in rows.items() if e["type"] == "test-quarantined"}


def active(events, project, now):
    now = _time(now)
    return {n: e for n, e in _latest(events, project, now).items()
            if _time(e["since"]) <= now < _time(e["until"])}


def due(events, project, now):
    now = _time(now)
    blamed = {}
    for e in events:
        if (e.get("type") == "window-dropped" and e.get("project") == project
                and now-timedelta(hours=BLAME_HOURS) <= _time(e["ts"]) <= now
                and e.get("window")):
            for node in e.get("nodes", []):
                blamed.setdefault(node, set()).add(e["window"])
    current = active(events, project, now)
    return [dict(node=n, rule="repeat-blame", windows=sorted(ws))
            for n, ws in sorted(blamed.items()) if len(ws) >= BLAME_WINDOWS and n not in current]


def lifts(events, project, now):
    now = _time(now)
    verified = sorted((e for e in events if e.get("project") == project
                       and e.get("type") == "window-verified" and _time(e["ts"]) <= now),
                      key=lambda e: _time(e["ts"]))
    result = []
    for node, row in _latest(events, project, now).items():
        since, until = _time(row["since"]), _time(row["until"])
        windows = [e for e in verified if _time(e["ts"]) > since][-GREEN_WINDOWS:]
        green = (len(windows) == GREEN_WINDOWS and all(
            node not in e.get("quarantined", []) and node not in e.get("pre_existing", [])
            for e in windows))
        if green and _time(windows[-1]["ts"]) < until:
            result.append((node, "green-2"))
        elif now >= until:
            result.append((node, "until"))
        elif green:
            result.append((node, "green-2"))
    return result


def _append(kind, project, now, **fields):
    old_file, old_now = os.environ.get("DOIT_LEDGER_FILE"), fold.NOW
    try:
        os.environ["DOIT_LEDGER_FILE"] = "L-base-red-local.jsonl"
        fold.NOW = now
        row = fold.append([kind, "quarantine:"+project, "project="+project] +
                          [k+":="+json.dumps(v) for k, v in fields.items()])
        # fold.append returns the stored content; the reader supplies its _src.
        return next(e for e in reversed(fold.read_events()) if e.get("actor") == "base-red"
                    and all(e.get(k) == v for k, v in row.items()))
    finally:
        fold.NOW = old_now
        if old_file is None:
            os.environ.pop("DOIT_LEDGER_FILE", None)
        else:
            os.environ["DOIT_LEDGER_FILE"] = old_file


def record(project, node, rule, windows, *, now=None):
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    if rule not in ("repeat-blame", "base-red"):
        raise ValueError("unknown quarantine rule")
    current = active(fold.read_events(), project, now)
    if node in current:
        return current[node]
    until = (now+timedelta(days=QUARANTINE_DAYS)).isoformat()
    question = _append("question", project, now, asks="Create a fix spec for "+node,
                       blocks=[node], default="the quarantine lifts at until and windows keep shipping",
                       deadline=until)
    return _append("test-quarantined", project, now, node=node, rule=rule,
                   windows=list(windows), since=now.isoformat(), until=until, fix=question["_src"])


def apply(project, *, now=None):
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    # Decide both sets from one view: a green lift must not immediately earn
    # the very same quarantine again during this apply call.
    events = fold.read_events()
    pending = due(events, project, now)
    lifted = lifts(events, project, now)
    for node, reason in lifted:
        _append("test-unquarantined", project, now, node=node, reason=reason)
    recorded = [record(project, **item, now=now) for item in pending]
    return {"recorded": recorded, "lifted": lifted}
