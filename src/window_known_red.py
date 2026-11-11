"""Known-red dispatch guard for window verification (L-spec-2323).

A candidate whose unchanged tree already finished red, on ids master has since
been proven to fail too, is not dispatched again: the retry stays pending with a
recorded reason. Everything here fails closed. A guard that cannot establish its
answer never suppresses a dispatch on a doubtful premise and never reports green.

Evidence is the additive `window-dispatch-verdict` event, emitted after final
judging. A `window-tree-verified` row alone never proves red.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import fold

REASON = "unchanged-tree-master-known-red"
DEFAULT = "retain pending and request operator resolution"
SHA = re.compile(r"[0-9a-f]{40}")
STATES = ("green", "red", "undetermined")
COUNTERS = ("missing_green", "invalid_new_ids", "missing_membership", "ambiguous_membership",
            "conflicting_receipts", "unproven_completion")
LISTS = ("required", "failing_ids", "new_ids", "pre_existing", "flakes", "quarantined")


def _line(value, limit=1000):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(c in value for c in "\r\n\x00")


def _ids(value):
    """Sorted distinct ids from a list of single-line strings, else None."""
    if not isinstance(value, list) or not all(_line(i) for i in value):
        return None
    return sorted(set(value))


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def _verdict(event):
    """Normalized completed observation, or None when malformed."""
    window = event.get("subject")
    stamp = _time(event.get("completed_at"))
    lists = {k: _ids(event.get(k)) for k in LISTS}
    if (not _line(window) or not _line(event.get("project")) or stamp is None or not _line(event.get("source_ref"))
            or event.get("state") not in STATES or any(v is None for v in lists.values())
            or any(not isinstance(event.get(k), str) or not SHA.fullmatch(event[k]) for k in ("sha", "tree", "base_sha"))):
        return None
    state = event["state"]
    if state == "red" and (not lists["failing_ids"] or not lists["new_ids"]
                           or not set(lists["new_ids"]) <= set(lists["failing_ids"])):
        return None
    if state != "red" and lists["new_ids"]:
        return None
    return dict(lists, window=window, project=event["project"], sha=event["sha"], tree=event["tree"],
                base_sha=event["base_sha"], state=state, at=stamp, source_ref=event["source_ref"])


def _membership(events):
    """(window, sha) -> set of member tuples seen on window-candidate / window-rehearsed rows."""
    seen = {}
    for e in events:
        if e.get("type") not in ("window-candidate", "window-rehearsed"):
            continue
        specs, sha = e.get("specs"), e.get("sha")
        if not (_line(e.get("subject")) and isinstance(sha, str) and SHA.fullmatch(sha)
                and isinstance(specs, list) and specs and all(_line(s) for s in specs)):
            continue
        seen.setdefault((e["subject"], sha), set()).add(tuple(sorted(set(specs))))
    return seen


def _historical(events, tree_rows):
    """Rehearsal red from before verdict receipts: only an exact join to a final
    window-rehearsed state=red with named ids, an explicit empty unresolved list and a
    complete exact-base classification counts."""
    out = []
    for e in events:
        if (e.get("type") != "window-rehearsed" or e.get("state") != "red" or e.get("unresolved") != []):
            continue
        ids, cls = _ids(e.get("ids")), e.get("classification")
        new = _ids(cls.get("new")) if isinstance(cls, dict) else None
        stamp = _time(e.get("ts"))
        if (not ids or not new or not set(new) <= set(ids) or cls.get("complete") is not True
                or cls.get("base_sha") != e.get("base_sha") or stamp is None
                or not all(isinstance(e.get(k), str) and SHA.fullmatch(e[k]) for k in ("sha", "tree", "base_sha"))):
            continue
        if not any(r.get("subject") == e.get("subject") and r.get("sha") == e["sha"] and r.get("tree") == e["tree"]
                   and r.get("project") == e.get("project") and r.get("green") is False for r in tree_rows):
            continue
        required = _ids(e.get("required"))
        if required is None:
            continue
        out.append(dict(window=e["subject"], project=e["project"], sha=e["sha"], tree=e["tree"],
                        base_sha=e["base_sha"], state="red", at=stamp, source_ref="rehearsed:" + e["subject"],
                        required=required, failing_ids=ids, new_ids=new, pre_existing=[], flakes=[],
                        quarantined=[]))
    return out


def observations(events):
    """-> (usable determined observations with `specs`, diagnostics counters)."""
    diag = dict.fromkeys(COUNTERS, 0)
    events = [e for e in events if isinstance(e, dict)]
    members = _membership(events)
    tree_rows = [e for e in events if e.get("type") == "window-tree-verified"]
    groups, finals = {}, set()
    for e in events:
        if e.get("type") != "window-dispatch-verdict":
            continue
        v = _verdict(e)
        if v is None:
            diag["invalid_new_ids" if _ids(e.get("new_ids")) is None else "unproven_completion"] += 1
            continue
        finals.add((v["window"], v["sha"], v["tree"]))
        groups.setdefault((v["project"], v["window"], v["sha"], v["tree"]), []).append(v)
    for e in tree_rows:
        if type(e.get("green")) is not bool:
            diag["missing_green"] += 1
            continue
        if "new_ids" in e or e["green"] is False:
            if _ids(e.get("new_ids")) is None:
                diag["invalid_new_ids"] += 1
                continue
        if e["green"] is False and (e.get("subject"), e.get("sha"), e.get("tree")) not in finals:
            diag["unproven_completion"] += 1
    out = []
    for key, rows in groups.items():
        shapes = {json.dumps({k: v[k] for k in v if k not in ("at", "source_ref")}, sort_keys=True) for v in rows}
        if len(shapes) > 1:
            diag["conflicting_receipts"] += 1
            continue
        found = members.get((key[1], key[2]), set())
        if not found:
            diag["missing_membership"] += 1
            continue
        if len(found) > 1:
            diag["ambiguous_membership"] += 1
            continue
        newest = max(rows, key=lambda v: (v["at"], v["source_ref"]))
        out.append(dict(newest, specs=list(next(iter(found)))))
    for h in _historical(events, tree_rows):
        found = members.get((h["window"], h["sha"]), set())
        if len(found) == 1 and not any(o["window"] == h["window"] and o["sha"] == h["sha"] for o in out):
            out.append(dict(h, specs=list(next(iter(found)))))
    return [o for o in out if o["state"] in ("green", "red")], diag


def diagnostics(events):
    """Bounded counts of evidence that could not be used."""
    return observations(events)[1]


def quick_ids(events, specs):
    """Sorted distinct new ids from each requested spec's most recent completed determined
    observation, when that observation is red. Snapshot-only: no network, classify or dispatch."""
    try:
        wanted = [s for s in specs if _line(s)] if isinstance(specs, (list, tuple, set)) else []
        usable, _ = observations(list(events))
    except (TypeError, ValueError, AttributeError):
        return []
    ids = set()
    for spec in wanted:
        mine = [o for o in usable if spec in o["specs"]]
        if mine:
            latest = max(mine, key=lambda o: (o["at"], o["source_ref"]))
            if latest["state"] == "red":
                ids.update(latest["new_ids"])
    return sorted(ids)


def receipt(gate, window, *, project, base_sha, sha, tree, required, state, failing_ids=(), new_ids=(),
            pre_existing=(), flakes=(), quarantined=()):
    """Append the completion proof for one finished judging through gate.ev. Never raises."""
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        fields = dict(required=sorted(set(required or ())), failing_ids=sorted(set(failing_ids)),
                      new_ids=sorted(set(new_ids)), pre_existing=sorted(set(pre_existing)),
                      flakes=sorted(set(flakes)), quarantined=sorted(set(quarantined)))
        if state == "red" and (not fields["new_ids"] or not set(fields["new_ids"]) <= set(fields["failing_ids"])):
            state = "undetermined"  # a red without named, resolved ids is not a completed observation
        if state != "red":
            fields["new_ids"] = []
        seed = json.dumps([window, sha, tree, state, fields, now], sort_keys=True)
        gate.ev("window-dispatch-verdict", window, project=project, window=window, sha=sha, tree=tree or "",
                base_sha=base_sha, state=state, completed_at=now,
                source_ref=window + ":" + hashlib.sha256(seed.encode()).hexdigest()[:16], **fields)
    except Exception as exc:  # a missing receipt only means no later suppression
        print(f"gate-window: dispatch verdict not recorded ({type(exc).__name__})")


def _key(project, tree, base_sha, source_ref, required, specs):
    return hashlib.sha256(json.dumps([project, tree, base_sha, source_ref, sorted(required), sorted(specs)]).encode()).hexdigest()[:24]


def _undetermined(why):
    return dict(state="undetermined", nodes=[], url="", doc={}, why=why, skipped_known=True)


def guard(gate, repo, cand, tree, required, window, *, rehearsal=False, project, base_sha, specs, classify=None):
    """-> None to verify normally, or an undetermined result that suppresses the dispatch.

    Suppression needs a completed determined red on this exact tree whose ids production
    `base_red.classify` positively covers as pre-existing at `base_sha`. It never records
    green, never moves master or membership, and never starts a baseline dispatch."""
    try:
        events = fold.read_events()
        usable, _ = observations(events)
        same = sorted((o for o in usable if o["project"] == project and o["tree"] == tree),
                      key=lambda o: (o["at"], o["source_ref"]))
        if not same or same[-1]["state"] != "red":
            return None
        source = same[-1]
        prior = gate.prior_result(events, tree)
        if gate.covers(prior, required):
            return None
    except Exception:
        return None  # no establishable history: ordinary verification
    try:
        if not set(source["required"]) >= set(required) or not source["new_ids"]:
            return None
        members = sorted({s for s in specs if _line(s)})
        if not members or len(members) != len(list(specs)):
            return None
        if classify is None:
            import base_red
            classify = base_red.classify
        try:
            answer = classify(project, base_sha, list(source["new_ids"]))
        except Exception:
            return None
        if (not isinstance(answer, dict) or answer.get("complete") is not True or answer.get("undetermined")
                or answer.get("new") or answer.get("uncovered")
                or sorted(answer.get("pre_existing", [])) != source["new_ids"]):
            return None
        key = _key(project, tree, base_sha, source["source_ref"], required, members)
        old = [e for e in events if e.get("type") == "escalation-blocking" and e.get("known_red_key") == key]
        if old:
            later = events[events.index(old[-1]) + 1:]
            if any(e.get("type") in ("decision", "unblocked") and e.get("subject") == old[-1].get("subject")
                   for e in later):
                return None  # resolved by the operator: verify again
        skipped = [e for e in events if e.get("type") == "window-skipped-known" and e.get("known_red_key") == key]
        first = _time(skipped[0].get("ts")) if skipped else None
        why = f"known-red: unchanged tree {tree[:12]} failed only on ids master already fails; dispatch suppressed"
        if not skipped:
            gate.ev("window-skipped-known", window, project=project, window=window, sha=cand, tree=tree,
                    base_sha=base_sha, specs=members, ids=list(source["new_ids"]), source_subject=source["window"],
                    source_sha=source["sha"], source_tree=source["tree"], source_ts=source["at"].isoformat(),
                    reason=REASON, required=sorted(required), source_ref=source["source_ref"], known_red_key=key)
            first = datetime.now(timezone.utc)
        if not old:
            due = ((first or datetime.now(timezone.utc)) + timedelta(minutes=30)).isoformat(timespec="seconds")
            gate.ev("escalation-blocking", window, why=why, outcome="undetermined", default=DEFAULT, deadline=due,
                    revert="resolve the escalation once the cause is fixed, or change the tree; no spec is dropped",
                    known_red_key=key, nodes=list(source["new_ids"]))
        return _undetermined(why)
    except Exception as exc:
        return _undetermined(f"known-red guard undetermined ({type(exc).__name__}); not dispatching")
