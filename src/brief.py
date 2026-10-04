#!/usr/bin/env python3
"""L-spec-0477/dossier-core: `doit brief <goal|charter|spec>` — a dossier
generated only from the ledger and the content files, never a hand-written
status page. `dossier()` is computed fresh on every call (R6): no caching, no
memoization, no on-disk artifact.

Pure-ish library (`dossier`, `goals`, `render`, `proving_lookup`) plus a thin
`__main__` CLI. The one subprocess this module ever runs is `proving.py
--json` (L-charter-0046's pinned CLI, a different, concurrent charter) — this
module never `import`s it (SD7), and tolerates its permanent absence.
"""
import argparse
import collections
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402
import plain  # noqa: E402

# `brief.PROVING_PATH` — L-charter-0046's `proving-core` (L-spec-0471), a
# different, concurrent charter, not yet built at this spec's tip. Guarded for
# permanent absence (SD7): `proving_lookup` only subprocesses it when it
# exists.
PROVING_PATH = pathlib.Path(__file__).resolve().parent / "proving.py"

_TITLE_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)

# R4 item 2's bucket rule (a declared default — SD6 states the sentence shape,
# not the split; Assumptions). "proven" = terminal success; "waiting" = never
# started; "building" = everything else that is not a stopped state.
_SPEC_STOPPED = {"killed", "dropped", "closed-unbuilt", "void"}
_SPEC_PROVEN = {"accepted", "closed-shipped"}
_SPEC_WAITING = {"written"}

_CHARTER_STOPPED = {"retracted"}
_CHARTER_PROVEN = {"L2-complete"}
_CHARTER_WAITING = {"open"}

# The terminal `next`/`expected_done` a charter needs no further action on.
_DONE = ("nothing — done", "nobody")


class UnknownItem(Exception):
    """Raised by `dossier()` for an id matching no goal/charter/spec subject
    and no content file (Target R3's "unknown id" case, R5 item 2)."""


# ── small, shared helpers ────────────────────────────────────────────────────

def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _own_stream(events, item_id):
    return [e for e in events if e.get("subject") == item_id]


def _newest(evs, etype):
    cand = [e for e in evs if e.get("type") == etype]
    if not cand:
        return None
    return max(cand, key=lambda e: str(e.get("ts", "")))


def _earliest(evs, *types):
    cand = [fold.ts(e["ts"]) for e in evs if e.get("type") in types and e.get("ts")]
    return _iso(min(cand)) if cand else None


def _last_change(evs):
    if not evs:
        return None
    return _iso(fold.ts(evs[-1]["ts"]))


def _content_title(root, item_id):
    """Charter/spec title (Target item 1): the item's own content file's
    first `^#\\s+(.*)$` line, stripped. `item_id` itself when no content file
    or no matching heading exists (defensive only)."""
    path = pathlib.Path(root) / "content" / f"{item_id}.md"
    if path.exists():
        m = _TITLE_RE.search(path.read_text())
        if m:
            return m.group(1).strip()
    return item_id


def _goal_title(item_id, events, root):
    """Goal title (Target item 1): the `title` field of the newest
    `goal-filed` event on the goal's own stream; the `.md` content-title rule
    on a missing/empty title (defensive only — none exists live)."""
    evs = [e for e in events if e.get("type") == "goal-filed" and e.get("subject") == item_id]
    newest = _newest(evs, "goal-filed")
    if newest is not None and newest.get("title"):
        return newest["title"]
    return _content_title(root, item_id)


def _bucket(states, stopped, proven, waiting):
    n = k = j = 0
    for s in states:
        if s in stopped:
            continue
        if s in proven:
            n += 1
        elif s in waiting:
            j += 1
        else:
            k += 1
    return n, k, j


def _spec_buckets(states):
    return _bucket(states, _SPEC_STOPPED, _SPEC_PROVEN, _SPEC_WAITING)


def _charter_buckets(states):
    return _bucket(states, _CHARTER_STOPPED, _CHARTER_PROVEN, _CHARTER_WAITING)


def _catch_me_up(plain_text, middle, next_):
    return [f"What it is: {plain_text}", middle,
            f"What happens next: {next_[0]} ({next_[1]})"]


# ── R3/R4 item 6 · spend ─────────────────────────────────────────────────────

def _terminal_by_spawn(events):
    """The same per-spawn terminal-event selection `fold.spend_detail`
    performs internally, replicated here (plan-auditor finding 2's fix):
    `spend_detail`'s own rows carry neither `cost_usd` nor `backend`."""
    by_spawn = collections.defaultdict(list)
    for e in events:
        if e.get("spawn"):
            by_spawn[e["spawn"]].append(e)
    out = {}
    for spawn_id, evs in by_spawn.items():
        terminal = next((e for e in evs if e["type"] in ("spawn-done", "spawn-failed")), None)
        if terminal is not None:
            out[spawn_id] = terminal
    return out


def _spend_shape(rows, term_by_spawn):
    per_role = {}
    for r in rows:
        pr = per_role.setdefault(r["role"], {"spawns": 0, "seat_min": 0.0, "weighted_tokens": 0.0})
        pr["spawns"] += 1
        pr["seat_min"] += r.get("duration_min") or 0
        pr["weighted_tokens"] += r.get("weighted") or 0
    total = {"spawns": 0, "seat_min": 0.0, "weighted_tokens": 0.0}
    for pr in per_role.values():
        for k in total:
            total[k] += pr[k]
    metered_usd = 0.0
    metered_unmeasured = 0
    for r in rows:
        # `spend_detail` only ever emits a row once it found a terminal event
        # (`terminal is None` rows are dropped there), so this lookup always hits.
        term = term_by_spawn.get(r["spawn"], {})
        cost = term.get("cost_usd")
        backend = term.get("backend")
        if cost is not None:
            metered_usd += cost
        elif backend not in ("seat", "pane"):
            metered_unmeasured += 1
    return {"per_role": per_role, "total": total, "metered_usd": metered_usd,
            "metered_unmeasured": metered_unmeasured}


def _spend_for_subject(subject, events):
    rows = fold.spend_detail(events, subject)
    return _spend_shape(rows, _terminal_by_spawn(events))


def _sum_spends(spends):
    per_role = {}
    total = {"spawns": 0, "seat_min": 0.0, "weighted_tokens": 0.0}
    metered_usd = 0.0
    metered_unmeasured = 0
    for sp in spends:
        for role, v in sp["per_role"].items():
            pr = per_role.setdefault(role, {"spawns": 0, "seat_min": 0.0, "weighted_tokens": 0.0})
            for k in total:
                pr[k] += v[k]
        for k in total:
            total[k] += sp["total"][k]
        metered_usd += sp["metered_usd"]
        metered_unmeasured += sp["metered_unmeasured"]
    return {"per_role": per_role, "total": total, "metered_usd": metered_usd,
            "metered_unmeasured": metered_unmeasured}


# ── R3, R3.SD7 · proving_lookup ──────────────────────────────────────────────

def proving_lookup(charter_id, now):
    """`(entry, None)` on a match; `(None, reason)` otherwise. Runs
    `python3 <PROVING_PATH> --json` (timeout 60s) ONLY when `PROVING_PATH`
    exists. Never imports `proving` (SD7)."""
    del now  # not used by this tip's guard; kept for the pinned signature
    if not PROVING_PATH.exists():
        return None, "Proving not derived yet"
    try:
        r = subprocess.run(["python3", str(PROVING_PATH), "--json"],
                            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None, "Proving not derived yet"
    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        return None, f"Proving not derived yet: {stderr}" if stderr else "Proving not derived yet"
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None, "Proving not derived yet"
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and entry.get("id") == charter_id:
            return entry, None
    return None, "not found"


def _expected_done_why(charter_id, now):
    """The dossier-level surfacing of a failed `proving_lookup` (Target item
    5's proving branch): ALWAYS the fixed phrase, "unchanged on
    absence/timeout/malformed JSON/no matching entry" — `proving_lookup`'s own
    `"not found"` string (a successful run, no match) is folded into the same
    phrase here, never surfaced verbatim."""
    entry, err = proving_lookup(charter_id, now)
    if entry is not None:
        return entry, None
    if err == "not found":
        return None, "Proving not derived yet"
    return None, err


# ── R3 item 4 · next, charter kind ──────────────────────────────────────────

def _charter_next(cid, state, mine, now, briefs):
    """SD3's four ordered steps (charter kind), after the retracted/L2-complete
    short-circuit. Step 2 (an open in-scope brief) is this unit's own declared
    default — SD3's own text for it is not part of this spec's extract; no
    acceptance criterion exercises its wording, only step 1's, 3's and 4's
    (AC6). Mirrors `fold._closable`'s `no_open_briefs` conjunct and
    `agents/executor.md`'s own "an open in-scope brief is yours to author" lane
    row, whose actor there is the executor."""
    if state == "retracted":
        return "nothing — withdrawn", "nobody"
    if state == "L2-complete":
        return _DONE
    if state in ("proving", "reopened"):
        entry, err = _expected_done_why(cid, now)
        if entry is not None:
            nxt = entry.get("next")
            if nxt:
                if isinstance(nxt, dict):  # proving.py emits {text, due_at, kind}; owner is a sibling field
                    return nxt.get("text"), entry.get("owner")
                return tuple(nxt)
            return "final checks and close", entry.get("owner")
        del err  # steps 2-4 run below
    if briefs:
        return "answer the open brief", "executor"
    for state_name in fold.SPEC_STATES:
        if any(s["state"] == state_name for s in mine):
            cand = plain.NEXT[state_name]
            if cand != _DONE:
                return cand
    return "final checks and close", "executor"


# ── R3 · charter core (shared by a charter dossier and a goal's child rows) ──

def _charter_core(cid, events, root, now, specs, charters):
    c = charters.get(cid, {"evs": _own_stream(events, cid), "state": "unknown", "briefs": 0})
    evs = c["evs"]
    state = c["state"]
    title = _content_title(root, cid)
    pl = plain.line(cid, root)
    ph = plain.phrase("charter", state)
    mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
    next_text, next_owner = _charter_next(cid, state, mine, now, c.get("briefs", 0))

    filed = _earliest(evs, "charter-filed", "goal-filed", "spec-written")
    planned = _earliest(evs, "plan-written", "cut-written")
    l1_complete = _earliest(evs, "l1-complete")
    shipped_ts = [fold.ts(e["ts"]) for s in mine for e in s["evs"] if e.get("type") == "shipped"]
    first_merge = _iso(min(shipped_ts)) if shipped_ts else None
    last_merge = _iso(max(shipped_ts)) if shipped_ts else None

    if state == "retracted":
        expected_done, expected_done_why = None, "withdrawn"
    elif state == "L2-complete":
        closed = _newest(evs, "charter-closed") or _newest(evs, "tree-reaped")
        expected_done = _iso(fold.ts(closed["ts"])) if closed is not None else (
            _iso(fold.ts(evs[-1]["ts"])) if evs else None)
        expected_done_why = None
    elif state in ("proving", "reopened"):
        entry, why = _expected_done_why(cid, now)
        expected_done = entry.get("deadline") if entry is not None else None
        expected_done_why = why
    else:
        n_open = sum(1 for s in mine if s["state"] not in
                     ("shipped-owed-evidence", "shipped-owed-due", "shipped-owed-expired",
                      "accepted", "closed-shipped", "closed-unbuilt", "dropped", "killed"))
        expected_done, expected_done_why = None, f"still building: {n_open} specs not yet merged"

    timeline = {"filed": filed, "planned": planned, "l1_complete": l1_complete,
                "first_merge": first_merge, "last_merge": last_merge,
                "expected_done": expected_done, "expected_done_why": expected_done_why}
    spend = _spend_for_subject(cid, events)
    children = _spec_children_rows(mine, root)
    return {"title": title, "plain": pl, "state": state, "phrase": ph,
            "next": {"text": next_text, "owner": next_owner}, "timeline": timeline,
            "spend": spend, "children": children, "mine": mine, "last_change": _last_change(evs)}


def _spec_children_rows(mine, root):
    rows = []
    for s in mine:
        text, owner = plain.NEXT[s["state"]]
        rows.append({
            "id": s["id"], "plain": plain.line(s["id"], root),
            "phrase": plain.phrase("spec", s["state"]),
            "filed": _earliest(s["evs"], "spec-written"),
            "last_change": _last_change(s["evs"]),
            "waiting_on": f"{text} ({owner})",
        })
    rows.sort(key=lambda r: r["id"])
    return rows


# ── R3 · per-kind dossiers ───────────────────────────────────────────────────

def _spec_dossier(item_id, events, root, now, specs, charters):
    del now, charters
    s = specs.get(item_id)
    evs = s["evs"] if s is not None else _own_stream(events, item_id)
    state = s["state"] if s is not None else "unknown"
    pl = plain.line(item_id, root)
    ph = plain.phrase("spec", state)
    next_text, next_owner = plain.NEXT[state]
    shipped_ts = _earliest(evs, "shipped")
    timeline = {"filed": _earliest(evs, "spec-written"), "planned": None, "l1_complete": None,
                "first_merge": shipped_ts, "last_merge": shipped_ts,
                "expected_done": None, "expected_done_why": "a spec has no forecast"}
    spend = _spend_for_subject(item_id, events)
    catch_me_up = _catch_me_up(pl["text"], f"Where it stands: {ph}", (next_text, next_owner))
    return {"id": item_id, "kind": "spec", "title": _content_title(root, item_id), "plain": pl,
            "catch_me_up": catch_me_up, "status": {"state": state, "phrase": ph},
            "next": {"text": next_text, "owner": next_owner}, "timeline": timeline,
            "spend": spend, "children": []}


def _charter_dossier(item_id, events, root, now, specs, charters):
    core = _charter_core(item_id, events, root, now, specs, charters)
    n, k, j = _spec_buckets([s["state"] for s in core["mine"]])
    middle = f"Where it stands: {core['phrase']}; {n} of {n + k + j} specs proven, {k} being built, {j} waiting"
    catch_me_up = _catch_me_up(core["plain"]["text"], middle,
                                (core["next"]["text"], core["next"]["owner"]))
    return {"id": item_id, "kind": "charter", "title": core["title"], "plain": core["plain"],
            "catch_me_up": catch_me_up,
            "status": {"state": core["state"], "phrase": core["phrase"]},
            "next": core["next"], "timeline": core["timeline"], "spend": core["spend"],
            "children": core["children"]}


def _linked_charter_ids(charters, goal_id):
    """Charter ids whose newest `charter-filed` event's `goal` field equals
    `goal_id` (Target item 3), in charter-filed ledger order (their own
    EARLIEST `charter-filed` ts, ascending — SD3's goal-rule tie-break for
    `next`, Target item 4)."""
    pairs = []
    for cid, c in charters.items():
        filed = [e for e in c["evs"] if e.get("type") == "charter-filed"]
        if not filed:
            continue
        newest = max(filed, key=lambda e: str(e.get("ts", "")))
        if newest.get("goal") == goal_id:
            earliest_ts = min(str(e.get("ts", "")) for e in filed)
            pairs.append((earliest_ts, cid))
    pairs.sort(key=lambda p: (p[0], p[1]))
    return [cid for _, cid in pairs]


def _goal_dossier(item_id, events, root, now, specs, charters):
    own_evs = _own_stream(events, item_id)
    title = _goal_title(item_id, events, root)
    pl = plain.line(item_id, root)

    linked_all = _linked_charter_ids(charters, item_id)
    states_all = [charters[cid]["state"] for cid in linked_all]
    gstate = plain.goal_state(states_all)
    ph = plain.phrase("goal", gstate)

    non_retracted = [cid for cid in linked_all if charters[cid]["state"] != "retracted"]
    charter_cores = {cid: _charter_core(cid, events, root, now, specs, charters)
                      for cid in non_retracted}

    next_cid = next((cid for cid in linked_all if charters[cid]["state"] not in
                     ("retracted", "L2-complete")), None)
    if next_cid is not None:
        next_text, next_owner = charter_cores[next_cid]["next"]["text"], \
            charter_cores[next_cid]["next"]["owner"]
    else:
        next_text, next_owner = _DONE

    fm = [charter_cores[cid]["timeline"]["first_merge"] for cid in non_retracted]
    lm = [charter_cores[cid]["timeline"]["last_merge"] for cid in non_retracted]
    fm_present, lm_present = [v for v in fm if v], [v for v in lm if v]
    first_merge = min(fm_present) if fm_present else None
    last_merge = max(lm_present) if lm_present else None

    ed = [charter_cores[cid]["timeline"]["expected_done"] for cid in non_retracted]
    if ed and all(v is not None for v in ed):
        expected_done, expected_done_why = max(ed), None
    else:
        expected_done, expected_done_why = None, "still building: a linked charter has no forecast yet"

    timeline = {"filed": _earliest(own_evs, "charter-filed", "goal-filed", "spec-written"),
                "planned": None, "l1_complete": None,
                "first_merge": first_merge, "last_merge": last_merge,
                "expected_done": expected_done, "expected_done_why": expected_done_why}

    spend = _sum_spends([charter_cores[cid]["spend"] for cid in non_retracted])

    children = []
    for cid in sorted(non_retracted):
        core = charter_cores[cid]
        children.append({"id": cid, "plain": core["plain"], "phrase": core["phrase"],
                          "filed": core["timeline"]["filed"], "last_change": core["last_change"],
                          "waiting_on": f"{core['next']['text']} ({core['next']['owner']})",
                          "specs": core["children"]})

    n, k, j = _charter_buckets([charters[cid]["state"] for cid in non_retracted])
    middle = f"Where it stands: {ph}; {n} of {n + k + j} specs proven, {k} being built, {j} waiting"
    catch_me_up = _catch_me_up(pl["text"], middle, (next_text, next_owner))

    return {"id": item_id, "kind": "goal", "title": title, "plain": pl,
            "catch_me_up": catch_me_up, "status": {"state": gstate, "phrase": ph},
            "next": {"text": next_text, "owner": next_owner}, "timeline": timeline,
            "spend": spend, "children": children}


def _kind(item_id):
    if item_id.startswith("L-goal-"):
        return "goal"
    if item_id.startswith("L-charter-"):
        return "charter"
    if item_id.startswith("L-spec-"):
        return "spec"
    return None


def _known(item_id, kind, events, specs, charters, root):
    if (pathlib.Path(root) / "content" / f"{item_id}.md").exists():
        return True
    if kind == "spec":
        return item_id in specs
    if kind == "charter":
        return item_id in charters
    if kind == "goal":
        return any(e.get("type") == "goal-filed" and e.get("subject") == item_id for e in events)
    return False


def dossier(item_id, events, root, now):
    """The SD6 shape for `item_id` (Target R3), one of `kind ==
    "goal"|"charter"|"spec"`. Raises `UnknownItem` for the unknown-id case
    (Target R5 item 2) — the CLI turns that into a one-line stderr, exit 2."""
    root = pathlib.Path(root)
    kind = _kind(item_id)
    specs, charters, _ignored, _by_subject = fold.fold(events)
    if kind is None or not _known(item_id, kind, events, specs, charters, root):
        raise UnknownItem(item_id)
    if kind == "spec":
        d = _spec_dossier(item_id, events, root, now, specs, charters)
    elif kind == "charter":
        d = _charter_dossier(item_id, events, root, now, specs, charters)
    else:
        d = _goal_dossier(item_id, events, root, now, specs, charters)
    d["as_of"] = now.isoformat(timespec="seconds")
    return d


def goals(events, root):
    """`{"id","title","state","phrase"}` per distinct `goal-filed` subject
    (Target R5 item 3). `[]` when none."""
    root = pathlib.Path(root)
    _specs, charters, _ignored, _by_subject = fold.fold(events)
    seen = []
    for e in events:
        gid = e.get("subject")
        if e.get("type") == "goal-filed" and gid and gid not in seen:
            seen.append(gid)
    out = []
    for gid in sorted(seen):
        states = [charters[cid]["state"] for cid in _linked_charter_ids(charters, gid)]
        state = plain.goal_state(states)
        out.append({"id": gid, "title": _goal_title(gid, events, root),
                    "state": state, "phrase": plain.phrase("goal", state)})
    return out


def render(d):
    """Terminal text; `catch_me_up` is its first three lines after the header
    (R4/R5)."""
    lines = [f"# {d['id']} — {d['title']}"]
    lines.extend(d["catch_me_up"])
    lines.append(f"Status: {d['status']['phrase']}")
    tl = d["timeline"]
    lines.append("Timeline:")
    for k in ("filed", "planned", "l1_complete", "first_merge", "last_merge", "expected_done"):
        v = tl.get(k)
        lines.append(f"  {k}: {v if v else 'not yet'}")
    if tl.get("expected_done_why"):
        lines.append(f"  expected_done_why: {tl['expected_done_why']}")
    sp = d["spend"]
    lines.append("Spend:")
    for role, v in sp["per_role"].items():
        lines.append(f"  {role}: {v['spawns']} spawn(s), {v['seat_min']:.1f} seat-min, "
                      f"{v['weighted_tokens']:.0f} weighted tokens")
    t = sp["total"]
    lines.append(f"  total: {t['spawns']} spawn(s), {t['seat_min']:.1f} seat-min, "
                 f"{t['weighted_tokens']:.0f} weighted tokens")
    if sp.get("metered_usd"):
        lines.append(f"  metered: ${sp['metered_usd']:.2f}")
    if sp.get("metered_unmeasured"):
        lines.append(f"  metered_unmeasured: {sp['metered_unmeasured']}")
    if d["children"]:
        lines.append("Children:")
        for c in d["children"]:
            lines.append(f"  {c['id']}: {c['phrase']} — {c['waiting_on']}")
    lines.append(f"as_of: {d['as_of']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brief.py")
    ap.add_argument("id", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--goals", action="store_true")
    a = ap.parse_args(argv)
    root = fold.ROOT
    events = fold.read_events()
    now = datetime.now(timezone.utc)
    if a.goals:
        print(json.dumps(goals(events, root), indent=2))
        return 0
    if not a.id:
        print("brief: an id is required (or --goals --json)", file=sys.stderr)
        return 2
    try:
        d = dossier(a.id, events, root, now)
    except UnknownItem:
        print(f"{a.id}: unknown id — no matching subject or content file", file=sys.stderr)
        return 2
    print(json.dumps(d, indent=2) if a.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
