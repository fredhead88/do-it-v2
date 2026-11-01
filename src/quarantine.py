"""Dated quarantine policy, with automatic base-red evidence and queued Git edits."""
from datetime import datetime, timedelta, timezone
import json
import hashlib
import fcntl
import os
import re

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
    owned = set()
    for e in events:
        if (e.get("type") == "window-dropped" and e.get("project") == project
                and now-timedelta(hours=BLAME_HOURS) <= _time(e["ts"]) <= now):
            if isinstance(e.get("owned_nodes"), list):
                owned.update(e["owned_nodes"])
            for node in e.get("nodes", []) if e.get("window") else []:
                blamed.setdefault(node, set()).add(e["window"])
    current = active(events, project, now)
    return [dict(node=n, rule="repeat-blame", windows=sorted(ws))
            for n, ws in sorted(blamed.items()) if len(ws) >= BLAME_WINDOWS and n not in current and n not in owned]


def base_due(events, project, now):
    """Two distinct consecutive complete base runs, or base-proven windows.

    Re-reading one CI artifact, or bisecting one window, is one observation.
    A green observation breaks the streak; stale evidence cannot renew a row.
    """
    streams = {"ci": {}, "window": {}}
    for e in sorted(events, key=lambda e: _time(e["ts"])):
        if e.get("project") != project or _time(e["ts"]) > now:
            continue
        kind = e.get("type")
        if kind in ("base-red", "base-red-nightly") and e.get("complete") is True:
            observations = e.get("observations")
            if observations:
                for observed in observations:
                    key = ("run:"+str(observed["run_id"])+":"+str(observed.get("attempt", 1)),)
                    ids = observed.get("ids", {}) if observed.get("complete") is True else {}
                    nodes = set().union(*(set(v) for v in ids.values()))
                    stamp = observed.get("timestamp") or e["ts"]
                    streams["ci"].setdefault(key, (dict(e, ts=stamp), nodes))
                continue
            sources = e.get("sources") or e.get("run_ids") or ([e["url"]] if e.get("url") else [])
            if not sources:
                continue  # SHA alone cannot distinguish two runs of the same tree.
            key = tuple(sorted(map(str, sources)))
            ids = e.get("ids", [])
            nodes = set().union(*(set(v) for v in ids.values())) if isinstance(ids, dict) else set(ids)
            streams["ci"].setdefault(key, (e, nodes))
        elif kind in ("window-pre-existing", "window-base-observed", "window-verified"):
            key = e.get("window") or e.get("subject")
            if not key:
                continue
            nodes = set(e.get("pre_existing", []) if kind == "window-verified" else e.get("ids", []))
            previous = streams["window"].get(key)
            if previous and kind != "window-verified":
                nodes.update(previous[1])
            streams["window"][key] = (e, nodes)
    current, result = active(events, project, now), {}
    for stream, rows in streams.items():
        pair = sorted(rows.items(), key=lambda pair: _time(pair[1][0]["ts"]))[-2:]
        if len(pair) != 2:
            continue
        if stream == "ci" and set(pair[0][0]) & set(pair[1][0]):
            continue  # overlapping snapshots cannot count one CI run twice
        for node in pair[0][1][1] & pair[1][1][1]:
            if node in current or not valid_node(node):
                continue
            previous = [e for e in events if e.get("project") == project and e.get("node") == node
                        and e.get("type") == "test-quarantined"]
            if previous and any(_time(row[0]["ts"]) <= max(_time(e["until"]) for e in previous)
                                for _, row in pair):
                continue
            result[node] = dict(node=node, rule="auto-base-red",
                                windows=[stream + ":" + ",".join(map(str, key)) if isinstance(key, tuple)
                                         else stream + ":" + str(key) for key, _ in pair])
    return list(result.values())


def valid_node(node):
    return isinstance(node, str) and bool(re.fullmatch(r"[A-Za-z0-9_./-]+\.py::[^\s\x00-\x1f]+", node))


def lifts(events, project, now):
    now = _time(now)
    verified = sorted((e for e in events if e.get("project") == project
                       and e.get("type") == "window-verified" and _time(e["ts"]) <= now),
                      key=lambda e: _time(e["ts"]))
    result = []
    for node, row in _latest(events, project, now).items():
        since, until = _time(row["since"]), _time(row["until"])
        windows = [e for e in verified if _time(e["ts"]) > since][-GREEN_WINDOWS:]
        green = (row.get("rule") != "auto-base-red" and len(windows) == GREEN_WINDOWS and all(
            node not in e.get("quarantined", []) and node not in e.get("pre_existing", [])
            for e in windows))
        if green and _time(windows[-1]["ts"]) < until:
            result.append((node, "green-2"))
        elif now >= until:
            result.append((node, "until"))
        elif green:
            result.append((node, "green-2"))
    return result


def _append(kind, project, now, *, _subject=None, **fields):
    old_file, old_now = os.environ.get("DOIT_LEDGER_FILE"), fold.NOW
    try:
        os.environ["DOIT_LEDGER_FILE"] = "L-base-red-local.jsonl"
        fold.NOW = now
        row = fold.append([kind, _subject or "quarantine:"+project, "project="+project] +
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
    if rule not in ("repeat-blame", "base-red", "auto-base-red"):
        raise ValueError("unknown quarantine rule")
    current = active(fold.read_events(), project, now)
    if node in current:
        return current[node]
    until = (now+timedelta(days=QUARANTINE_DAYS)).isoformat()
    if rule == "auto-base-red":
        subject = "quarantine:"+project+":"+hashlib.sha256(node.encode()).hexdigest()[:16]
        question = _append("brief", project, now, _subject=subject, node=node, owner="auto-"+now.date().isoformat(),
                           line="Fix base-red test " + node + "; evidence=" + ",".join(windows),
                           deadline=until)
    else:
        question = _append("question", project, now, asks="Create a fix spec for "+node,
                           blocks=[node], default="the quarantine lifts at until and windows keep shipping",
                           deadline=until)
    return _append("test-quarantined", project, now, node=node, rule=rule,
                   windows=list(windows), since=now.isoformat(), until=until, fix=question["_src"],
                   owner="auto-"+now.date().isoformat())


def apply(project, *, now=None):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", project) or project in (".", ".."):
        raise ValueError("invalid quarantine project")
    state = fold.ROOT / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / ("quarantine." + project + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply(project, now=now)


def _apply(project, *, now=None):
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    # Decide both sets from one view: a green lift must not immediately earn
    # the very same quarantine again during this apply call.
    events = fold.read_events()
    pending = base_due(events, project, now) + due(events, project, now)
    import rulings
    receipts = []
    consumed = {e.get("ref") for e in events if e.get("type") == "quarantine-ruling-applied"}
    for event, ruling in rulings.decisions(events, project):
        if (ruling['kind'] != 'base-red-accepted' or not event.get('_src')
                or event['_src'] in consumed or _time(event['ts']) > now):
            continue
        receipts.append((event['_src'], ruling['nodes']))
        source = 'ruling:' + event['_src']
        for node in ruling['nodes']:
            if not any(e.get('type') == 'test-quarantined' and e.get('project') == project
                       and e.get('node') == node and source in e.get('windows', []) for e in events):
                pending.append(dict(node=node, rule='auto-base-red', windows=[source]))
    lifted = lifts(events, project, now)
    for node, reason in lifted:
        _append("test-unquarantined", project, now, node=node, reason=reason)
        if reason == "until":
            row = _latest(events, project, now)[node]
            subject = next((e['subject'] for e in events if e.get('_src') == row.get('fix')), None)
            _append("escalation-blocking", project, now, _subject=subject, ref=row.get("fix"), node=node,
                    default="Fix expired base-red quarantine for " + node,
                    deadline=(now+timedelta(days=1)).isoformat(),
                    revert="Remove escalation after the fix is verified")
    recorded = [record(project, **item, now=now) for item in pending]
    for source, nodes in receipts:
        _append("quarantine-ruling-applied", project, now, ref=source, nodes=nodes)
    import quarantine_file
    quarantine_file.enqueue(project, fold.read_events(), now)
    return {"recorded": recorded, "lifted": lifted}
