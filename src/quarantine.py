"""Dated quarantine policy, with automatic base-red evidence and queued Git edits."""
from datetime import datetime, timedelta, timezone
import json
import hashlib
import fcntl
import os
import re
import sys
from itertools import combinations

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


def _metadata(value):
    return isinstance(value, str) and bool(value) and not re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)


def _evidence(prefix, values):
    return prefix + json.dumps(values, separators=(",", ":"), ensure_ascii=True)


def _diagnostics(rule, excluded, *, unavailable=None):
    if excluded or unavailable:
        print(json.dumps(dict(rule=rule, excluded_count=len(excluded),
                              excluded=sorted(excluded, key=lambda e: (e["source"], e["reason"])),
                              **({"unavailable": unavailable} if unavailable else {})),
                         ensure_ascii=True), file=sys.stderr)


def _excluded(excluded, event, reason):
    source = event.get("_src")
    if not isinstance(source, str) or not source:
        source = _evidence("event:", [event.get(k) for k in ("subject", "sha", "tree")])
    excluded.append(dict(source=source, reason=reason))


def _prior_until(events, project, now):
    """A lift does not erase the evidence boundary imposed by a prior row."""
    result = {}
    for e in events:
        if (e.get("type") == "test-quarantined" and e.get("project") == project
                and _time(e["ts"]) <= now):
            node, until = e["node"], _time(e["until"])
            result[node] = max(result.get(node, until), until)
    return result


def _nodes(ids):
    if not isinstance(ids, list) or any(not isinstance(n, str) for n in ids):
        return "invalid-ids"
    if any(not valid_node(n) or not _metadata(n) for n in ids):
        return "invalid-node-ids"
    return None


def _members(records, tree):
    """Resolve exact matches without merging contradictory membership claims."""
    choices = set()
    for e in records:
        specs = e.get("specs")
        if specs is None or specs == []:
            continue
        if not isinstance(specs, list) or any(not _metadata(s) for s in specs):
            return None, "invalid-membership"
        if e.get("type") == "window-rehearsed" and "tree" in e and e["tree"] != tree:
            return None, "contradictory-tree"
        choices.add(frozenset(specs))
    if len(choices) > 1:
        return None, "contradictory-membership"
    return (next(iter(choices)), None) if choices else (None, None)


def spec_due(events, project, now):
    """Propose reds shared by two distinct trees with disjoint spec membership."""
    now = _time(now)
    joins = {}
    for e in events:
        if (e.get("project") == project and e.get("type") in ("window-candidate", "window-rehearsed")
                and _time(e["ts"]) <= now
                and all(_metadata(e.get(k)) for k in ("subject", "sha"))):
            joins.setdefault((e["project"], e["subject"], e["sha"]), []).append(e)
    excluded, observations = [], []
    for e in events:
        if e.get("project") != project or e.get("type") != "window-tree-verified":
            continue
        if not now-timedelta(days=QUARANTINE_DAYS) <= _time(e["ts"]) <= now:
            continue
        reason = None
        if not all(_metadata(e.get(k)) for k in ("subject", "sha", "tree")) or any(
                k in e and not _metadata(e[k]) for k in ("_src", "window")):
            reason = "invalid-identity"
        elif type(e.get("green")) is not bool:
            reason = "invalid-green"
        else:
            reason = _nodes(e.get("new_ids"))
        if reason:
            _excluded(excluded, e, reason)
            continue
        records = joins.get((project, e["subject"], e["sha"]), [])
        specs, reason = _members([r for r in records if r["type"] == "window-candidate"], e["tree"])
        if not specs and not reason:
            specs, reason = _members([r for r in records if r["type"] == "window-rehearsed"], e["tree"])
        if reason or not specs:
            _excluded(excluded, e, reason or "missing-membership")
            continue
        if e["green"] is False:
            observations.append((e, specs, _evidence("tree:", [e[k] for k in ("subject", "sha", "tree")])))
    _diagnostics("two-specs", excluded)
    current, prior = active(events, project, now), _prior_until(events, project, now)
    by_node = {}
    for e, specs, source in observations:
        for node in set(e["new_ids"]):
            if node not in current and (node not in prior or _time(e["ts"]) > prior[node]):
                by_node.setdefault(node, []).append((e["tree"], specs, source))
    result = []
    for node, rows in sorted(by_node.items()):
        for a, b in combinations(sorted(rows, key=lambda r: r[2]), 2):
            if a[0] != b[0] and a[1].isdisjoint(b[1]):
                result.append(dict(node=node, rule="two-specs", windows=sorted({a[2], b[2]})))
                break
    return result


def seed_due(events, project, now):
    """Seed a project once, from its latest complete counted master nightly."""
    now = _time(now)
    if any(e.get("type") == "test-quarantined" and e.get("actor") == "base-red"
           and e.get("rule") == "master-seed" and e.get("project") == project
           and _time(e["ts"]) <= now for e in events):
        return []
    counted = [(i, e) for i, e in enumerate(events)
               if e.get("type") == "base-red-nightly" and e.get("project") == project
               and e.get("complete") is True and e.get("suite") in ("nightly", "undeclared")
               and _time(e["ts"]) <= now]
    if not counted:
        _diagnostics("master-seed", [], unavailable="no-counted-nightly")
        return []
    _, event = max(counted, key=lambda p: (_time(p[1]["ts"]), str(p[1].get("_src", "")), p[0]))
    ids = event.get("ids")
    if isinstance(ids, dict):
        ids = [n for values in ids.values() for n in values] if all(
            isinstance(values, list) for values in ids.values()) else None
    reason = ("invalid-identity" if not all(_metadata(event.get(k)) for k in ("sha", "_src"))
              else _nodes(ids))
    if reason:
        excluded = []
        _excluded(excluded, event, reason)
        _diagnostics("master-seed", excluded)
        return []
    current, prior = active(events, project, now), _prior_until(events, project, now)
    return [dict(node=node, rule="master-seed",
                 windows=[_evidence("nightly:", [event["_src"], event["sha"]])])
            for node in sorted(set(ids)) if node not in current
            and (node not in prior or _time(event["ts"]) > prior[node])]


def lifts(events, project, now):
    now = _time(now)
    verified = sorted((e for e in events if e.get("project") == project
                       and e.get("type") == "window-verified" and _time(e["ts"]) <= now),
                      key=lambda e: _time(e["ts"]))
    result = []
    for node, row in _latest(events, project, now).items():
        since, until = _time(row["since"]), _time(row["until"])
        windows = [e for e in verified if _time(e["ts"]) > since][-GREEN_WINDOWS:]
        green = (row.get("rule") not in ("auto-base-red", "two-specs", "master-seed")
                 and len(windows) == GREEN_WINDOWS and all(
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
    if rule not in ("repeat-blame", "base-red", "auto-base-red", "two-specs", "master-seed"):
        raise ValueError("unknown quarantine rule")
    if rule in ("two-specs", "master-seed") and (
            not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", project)
            or project in (".", "..") or not valid_node(node) or not _metadata(node)
            or not isinstance(windows, list) or not windows or any(not _metadata(w) for w in windows)):
        raise ValueError("invalid quarantine evidence")
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
    pending = (base_due(events, project, now) + due(events, project, now)
               + spec_due(events, project, now) + seed_due(events, project, now))
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
    unique = {}
    for item in pending:
        unique.setdefault(item["node"], item)
    recorded = [record(project, **item, now=now) for item in unique.values()]
    for source, nodes in receipts:
        _append("quarantine-ruling-applied", project, now, ref=source, nodes=nodes)
    import quarantine_file
    quarantine_file.enqueue(project, fold.read_events(), now)
    return {"recorded": recorded, "lifted": lifted}
