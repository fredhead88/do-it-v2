"""Member verdicts: parallel alone-tree verification and tree-keyed reuse (L-spec-2012).

After a red window, each remaining member is verified ALONE on the window's base
(`merge_into` + `tree_of`), up to `member_verify_parallel` CI calls at once. Every
verification appends a durable `member-verdict`. A green verdict on a tree, like a
green `window-tree-verified`, is reusable for `verdict_fresh_hours`; nothing else is.
Undetermined is never green and never reusable.

window_gate imports this module, so it never imports window_gate: the gate's
`verify`/`merge_into`/`tree_of`/`ev`/`covers` arrive as the `gate` argument.
"""
import contextvars
import math
import pathlib
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import fold

TOML = pathlib.Path(__file__).resolve().parent.parent / "look.toml"
DEFAULT_PARALLEL = 4
DEFAULT_FRESH_HOURS = 24
STATES = ("green", "red", "undetermined")
TREE = re.compile(r"[0-9a-f]{40}")
SOFT = (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError)


def _table(toml_path=None):
    try:
        import tomllib
        with open(toml_path or TOML, "rb") as stream:
            table = tomllib.load(stream).get("merge_queue")
    except (OSError, ValueError, ImportError):
        return {}
    return table if isinstance(table, dict) else {}


def member_verify_parallel(toml_path=None):
    value = _table(toml_path).get("member_verify_parallel")
    return value if type(value) is int and value >= 1 else DEFAULT_PARALLEL


def verdict_fresh_hours(toml_path=None):
    value = _table(toml_path).get("verdict_fresh_hours")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_FRESH_HOURS
    return value if math.isfinite(value) and value > 0 else DEFAULT_FRESH_HOURS


def _stamp(event, now, hours):
    """The event time, or None unless it is a real timestamp in [now - hours, now]."""
    try:
        raw = event["ts"]
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None or not isinstance(raw, str):
            return None
        if not now - timedelta(hours=hours) <= parsed <= now:
            return None
        return parsed
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def prior_result(events, tree, *, now=None, hours=None, toml_path=None):
    """Newest fresh determined result on this exact tree, from window-tree-verified
    or member-verdict events. A member-verdict comes back shaped like a tree result
    (`green`, `required`); one without `required` can never cover a check."""
    now = now or datetime.now(timezone.utc)
    hours = verdict_fresh_hours(toml_path) if hours is None else hours
    newest, newest_ts = None, None
    for event in events:
        if not isinstance(event, dict) or event.get("tree") != tree or not isinstance(tree, str) or not TREE.fullmatch(tree):
            continue
        kind = event.get("type")
        if kind == "window-tree-verified" and type(event.get("green")) is bool:
            shaped = event
        elif kind == "member-verdict" and event.get("state") in ("green", "red"):
            shaped = dict(event, green=event["state"] == "green")
        else:
            continue
        stamp = _stamp(event, now, hours)
        if stamp is not None and (newest_ts is None or stamp >= newest_ts):
            newest, newest_ts = shaped, stamp
    return newest


def _alone(gate, repo, base, entry, window):
    """-> (spec, branch_sha, alone_commit, tree) or None when the alone-tree cannot be built."""
    spec, _branch, sha = entry
    try:
        cand, _conflicts = gate.merge_into(repo, base, sha, f"member-alone({spec}): window {window}")
        return None if cand is None else (spec, sha, cand, gate.tree_of(repo, cand))
    except SOFT:
        return None


def _reusable(gate, built, events, scope_of, now, hours, toml_path=None):
    """(reusable, required): a fresh green on the exact alone-tree that covers the required
    checks. Scope is only computed once some evidence on the tree exists."""
    if built is None:
        return False, None
    prior = prior_result(events, built[3], now=now, hours=hours, toml_path=toml_path)
    if prior is None or prior.get("green") is not True:
        return False, None
    try:
        required = list(scope_of(built[2]))
    except SOFT:
        return False, None
    return gate.covers(prior, required), required


def _verify(gate, repo, cand, required, window):
    try:
        result = gate.verify(repo, cand, required, window)
    except getattr(gate, "TrainCancelled", ()) as exc:
        return "cancelled", exc
    except Exception as exc:  # an unreadable or crashed verification never establishes green
        return "ok", dict(state="undetermined", nodes=[], url="", why=type(exc).__name__)
    if not isinstance(result, dict) or result.get("state") not in STATES:
        return "ok", dict(state="undetermined", nodes=[], url="", why="unreadable verdict")
    return "ok", result


def attribute(gate, repo, base, entries, window, *, scope_of, blame=None, events=None, now=None,
              parallel=None, hours=None, toml_path=None):
    """Verify every entry alone on `base`, concurrently, bounded by member_verify_parallel.

    Reuses a fresh covered green on the same alone-tree instead of dispatching. Appends one
    `member-verdict` per dispatched verification as it completes. `blame`, when given, is the
    set of window-charged failing nodes: a red member is dropped only for nodes in it.
    -> dict(drop={spec: nodes}, verdicts={spec: state}, reused=[...], dispatched=[...], skipped=[...])
    """
    now = now or datetime.now(timezone.utc)
    events = list(fold.read_events() if events is None else events)
    hours = verdict_fresh_hours(toml_path) if hours is None else hours
    width = max(1, parallel if parallel is not None else member_verify_parallel(toml_path))
    out = dict(drop={}, verdicts={}, urls={}, reused=[], dispatched=[], skipped=[])
    todo = []
    for entry in entries:
        built = _alone(gate, repo, base, entry, window)
        if built is None:
            out["skipped"].append(entry[0])
            continue
        reusable, required = _reusable(gate, built, events, scope_of, now, hours, toml_path)
        if reusable:
            out["reused"].append(built[0])
            out["verdicts"][built[0]] = "green"
            continue
        try:
            required = list(scope_of(built[2])) if required is None else required
        except SOFT:
            out["skipped"].append(built[0])
            continue
        todo.append(built + (required,))
    cancelled = None
    with ThreadPoolExecutor(max_workers=min(width, max(1, len(todo)))) as pool:
        futures = {pool.submit(contextvars.copy_context().run, _verify, gate, repo, c, req, window):
                   (spec, tree, req) for spec, _sha, c, tree, req in todo}
        for future in as_completed(futures):
            spec, tree, required = futures[future]
            kind, result = future.result()
            if kind == "cancelled":
                cancelled = cancelled or result
                continue
            state, nodes = result["state"], sorted(str(n) for n in result.get("nodes") or [])
            if state == "red" and not nodes:
                state = "undetermined"  # a red nobody can name is not a charge
            gate.ev("member-verdict", spec, spec=spec, window=window, tree=tree, state=state,
                    url=str(result.get("url") or ""), nodes=nodes, required=required)
            out["dispatched"].append(spec)
            out["verdicts"][spec] = state
            out["urls"][spec] = str(result.get("url") or "")
            if state == "red" and (blame is None or set(nodes) & set(blame)):
                out["drop"][spec] = nodes if blame is None else [n for n in nodes if n in blame]
    if cancelled is not None:
        raise cancelled
    return out


def recovering(events, specs):
    """{spec: failing nodes} for specs whose newest queue-add is a red window's escalation-default
    requeue (the unattributed-failure recovery path), so they are verified alone, not re-bisected."""
    newest = {}
    for index, event in enumerate(events):
        if isinstance(event, dict) and event.get("type") == "queue-add" and event.get("subject") in specs:
            key = (str(event.get("ts", "")), index)
            if event["subject"] not in newest or key >= newest[event["subject"]][0]:
                newest[event["subject"]] = (key, event)
    return {s: sorted(n for n in e.get("nodes") if isinstance(n, str)) for s, (_k, e) in newest.items()
            if e.get("rule") == "escalation-default" and isinstance(e.get("nodes"), list) and e["nodes"]}


def drop_result(alone, spec, base):
    """The result shape the gate's drop_specs reads, for one alone-red member."""
    return dict(nodes=alone["drop"][spec], url=alone["urls"].get(spec, ""), candidate=base, paths={}, unattributed=[],
                targeted_owners=alone["drop"], targeted_proven=alone["drop"], attribution_mode="member-alone")


def cleared(gate, repo, base, entries, window, *, scope_of, events=None, now=None, hours=None, toml_path=None):
    """True only when every entry carries a fresh covered green alone-verdict on `base`'s
    CURRENT alone-tree. Recomputed here; anything unbuildable or missing is not cleared."""
    now = now or datetime.now(timezone.utc)
    events = list(fold.read_events() if events is None else events)
    if not entries:
        return False
    for entry in entries:
        if not _reusable(gate, _alone(gate, repo, base, entry, window), events, scope_of, now, hours, toml_path)[0]:
            return False
    return True


def refusal(gate, repo, base, entries, window, why, **kw):
    """A named refusal for a window whose members are all green alone, else None."""
    if not cleared(gate, repo, base, entries, window, **kw):
        return None
    return ("member-interaction: every member is green alone on the current base "
            f"({', '.join(e[0] for e in entries)}) but the combined candidate is red; {why}")


def latest(events, spec, now=None):
    """The newest well-formed member-verdict for `spec`, or None (ledger order breaks ties)."""
    best, best_ts = None, None
    for event in events:
        if (not isinstance(event, dict) or event.get("type") != "member-verdict"
                or (event.get("spec") or event.get("subject")) != spec
                or event.get("state") not in STATES or not isinstance(event.get("tree"), str)
                or not TREE.fullmatch(event["tree"])):
            continue
        try:
            stamp = datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if stamp.tzinfo is not None and (best_ts is None or stamp >= best_ts):
            best, best_ts = event, stamp
    if best is None:
        return None
    shipped = [e for e in events if isinstance(e, dict) and e.get("type") == "shipped" and e.get("subject") == spec
               and str(e.get("ts", "")) >= str(best.get("ts", ""))]
    return None if shipped else best


def _age(event, now):
    try:
        seconds = (now - datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))).total_seconds()
    except (KeyError, TypeError, ValueError):
        return "age unknown"
    seconds = max(0, int(seconds))
    return (f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 3600}h" if seconds >= 3600
            else f"{seconds // 60}m") + " ago"


def verdict_text(events, spec, now=None):
    """One line for nextstep/look: tree prefix, state, age. Undetermined is visibly not green."""
    event = latest(events, spec)
    if event is None:
        return None
    now = now or datetime.now(timezone.utc)
    state = event["state"]
    meaning = {"green": "verified alone, waiting for a window",
               "red": "failed alone",
               "undetermined": "UNDETERMINED, not verified, re-admitted to the next window"}[state]
    return f"member-verdict {event['tree'][:8]} {state} ({meaning}), {_age(event, now)}"


def readmit(gate, repo, base, entries, window, *, scope_of, events=None, now=None, hours=None, toml_path=None):
    """Classify members for the next window. Every member stays admitted (prewindow admission
    still applies); a fresh covered green skips repeat alone-tree CI, an undetermined latest
    verdict is marked. -> dict(green=[...], undetermined=[...], other=[...])"""
    now = now or datetime.now(timezone.utc)
    events = list(fold.read_events() if events is None else events)
    out = dict(green=[], undetermined=[], other=[])
    if not any(isinstance(e, dict) and e.get("type") == "member-verdict" for e in events):
        return out  # nothing recorded: no work, no scope reads
    for entry in entries:
        if _reusable(gate, _alone(gate, repo, base, entry, window), events, scope_of, now, hours, toml_path)[0]:
            out["green"].append(entry[0])
            continue
        event = latest(events, entry[0])
        out["undetermined" if event is not None and event["state"] == "undetermined" else "other"].append(entry[0])
    return out


def waiting_rows(events, specs, now):
    """Aggregate look rows (rule, key, detail, severity): specs verified alone and waiting,
    and specs whose latest alone-verdict is undetermined. Never one brief per spec."""
    green, undetermined = [], []
    for spec in sorted(specs):
        event = latest(events, spec)
        if event is None or str(specs[spec].get("state", "")).startswith(("shipped", "accepted", "killed", "dropped", "void", "closed")):
            continue
        if event["state"] == "green":
            green.append(f"{spec} {event['tree'][:8]} {_age(event, now)}")
        elif event["state"] == "undetermined":
            undetermined.append(f"{spec} {event['tree'][:8]} {_age(event, now)}")
    rows = []
    if green:
        rows.append(("member-verdict", "waiting", f"{len(green)} spec(s) verified alone, waiting for a window: "
                     + "; ".join(green[:8]) + (" ..." if len(green) > 8 else ""), "INFO"))
    if undetermined:
        rows.append(("member-verdict", "undetermined", f"{len(undetermined)} spec(s) with an UNDETERMINED alone-verdict "
                     "(not verified, re-admitted): " + "; ".join(undetermined[:8]) + (" ..." if len(undetermined) > 8 else ""), "WARN"))
    return rows
