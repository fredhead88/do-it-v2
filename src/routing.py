#!/usr/bin/env python3
"""routing — L-spec-8034, charter L-charter-0042 R12(f-j).

Pure helpers over an already-read ledger `events` list and over the AC-anchor
line shape the packet uses: reading `criterion-routed` events, the
routed-vs-recorded diff dispatch emits from, the AC-block grouping shared with
the packet (drop / group), the reviewer-clearance check `fold.spec_state`
gates ship-readiness on, and the `to=owed` synthesis `owed.checks` reads.

No subprocess, no ledger read of its own (every function takes `events`/
`evs` already read), and no import of `fold` — `fold.py` imports THIS module
(mirrors `owed.py`'s own no-cycle rule, stated there); `owed.py` imports this
module too. `grading_env.criterion_caps`/`route`/`required` are NOT here —
they stay in `grading_env` (Boundaries) — this module only reads what
`dispatch` already wrote about them, plus the packet/reviewer/owed shaping
that follows from those reads."""
import re

_AC_ANY = re.compile(r"^\s*\**AC(\d+)\s*\[")  # same anchor shape packet._AC_ANY matches


def latest_routed_events(events, spec):
    """`{criterion: event}` — the latest `criterion-routed` row per criterion
    id on `spec`, actor `grader` only (SD-R16-6/AC7), `to` included whatever
    its value (`"reviewer"`, `"owed"`, or `"none"`). `events` is read in
    order; the LAST matching row per id wins (last-write-by-position, the
    same rule `owed.checks`'s own `last_ac` uses)."""
    out = {}
    for e in events:
        if (e.get("type") != "criterion-routed" or e.get("subject") != spec
                or e.get("actor") != "grader"):
            continue
        cid = e.get("criterion")
        if cid:
            out[cid] = e
    return out


def routed_to(events, spec):
    """`{criterion: "reviewer"|"owed"}` — `latest_routed_events` with
    `to == "none"` rows dropped (a rework that removed the need clears it).
    `fold.routed_criteria` is a thin call into this (SD-R16-4: fold never
    runs `grading_env.route()` itself, it only reads events)."""
    return {cid: e.get("to") for cid, e in latest_routed_events(events, spec).items()
            if e.get("to") != "none"}


def explicit_owed_ids(events, spec):
    """The raw `owed-ac` criterion ids on `spec`, spec-writer/spec-auditor
    authored only — the set `grading_env.route()`'s output is ignored for
    (Assumption 8): an explicit `owed-ac` wins over routing, so a criterion in
    this set is never recorded as `criterion-routed`. Deliberately NOT
    `fold.verdict_owed_criteria` (which, after this spec, also folds in
    routed ids) — that would be circular for exactly this use."""
    return {e.get("criterion") for e in events if e.get("type") == "owed-ac" and e.get("subject") == spec
            and e.get("criterion") and e.get("actor") in ("spec-writer", "spec-auditor")}


def diff_routes(computed, prior):
    """`computed`: `{criterion: (capability, to)}`, freshly derived this
    dispatch by `grading_env.route()` (already filtered against
    `explicit_owed_ids`, R12.g). `prior`: `latest_routed_events(...)` — the
    latest recorded row per criterion, `to == "none"` rows included. Returns
    the ordered `[(criterion, capability, to), ...]` rows dispatch must
    append as `criterion-routed`: one per id whose computed route differs
    from its latest recorded `to`, plus a `to="none"` row (capability = the
    prior row's own) for every previously-routed id no longer present in
    `computed`. An unchanged spec (computed == what was last recorded)
    produces an empty list (AC6's "second dispatch appends none")."""
    rows = []
    for cid in sorted(computed):
        cap, to = computed[cid]
        prior_to = (prior.get(cid) or {}).get("to")
        if prior_to != to:
            rows.append((cid, cap, to))
    for cid in sorted(set(prior) - set(computed)):
        if prior[cid].get("to") != "none":
            rows.append((cid, prior[cid].get("capability"), "none"))
    return rows


def group_blocks(lines):
    """`{criterion_id: [lines]}` — group criteria LINES into AC-anchored
    blocks: each starts at a line matching the `AC<n> [` anchor and runs
    through the line before the next such anchor. Lines before the first
    anchor belong to no criterion and are not returned (Assumption 3)."""
    groups, cur_id, cur = {}, None, []

    def flush():
        if cur_id is not None:
            groups[cur_id] = cur
    for l in lines:
        m = _AC_ANY.match(l)
        if m:
            flush()
            cur_id, cur = f"AC{m.group(1)}", [l]
        else:
            cur.append(l)
    flush()
    return groups


def drop_blocks(lines, drop_ids):
    """L-spec-0435's `_drop_owed_blocks`, moved here (R12 size offset) and
    re-imported into `packet` under the old name: group criteria LINES into
    AC-anchored blocks — each starts at a line matching the `AC<n> [` anchor
    and runs through the line before the next such anchor; text before the
    first anchor is its own always-kept block — and drop, WHOLE, every block
    whose anchor id is in `drop_ids`. Dropping by a per-line id match instead
    would leak a dropped AC's wrapped continuation lines into the packet.
    `drop_ids` empty reproduces the input unchanged, line for line."""
    out, cur_id, cur = [], None, []

    def flush():
        if cur_id not in drop_ids:
            out.extend(cur)
    for l in lines:
        m = _AC_ANY.match(l)
        if m:
            flush()
            cur_id, cur = f"AC{m.group(1)}", [l]
        else:
            cur.append(l)
    flush()
    return out


def reviewer_cleared(events, spec, criterion):
    """True iff the LATEST `criterion-cleared`/`rejected-criterion` row on
    (`spec`, `criterion`) is a `criterion-cleared` from actor `reviewer`
    (AC11): a `criterion-cleared` from any other actor (e.g. `builder`) never
    counts, and a later `rejected-criterion` (any actor) reopens it. `False`
    when no such row exists yet."""
    relevant = [e for e in events if e.get("subject") == spec and e.get("criterion") == criterion
                and e.get("type") in ("criterion-cleared", "rejected-criterion")]
    if not relevant:
        return False
    last = relevant[-1]
    return last.get("type") == "criterion-cleared" and last.get("actor") == "reviewer"


def reviewer_gate_clear(events, spec, to_reviewer_ids):
    """`fold.spec_state`'s reviewer-clearance gate (AC11, R12.i): True once
    every id in `to_reviewer_ids` is `reviewer_cleared` on `spec`. An empty
    set is vacuously clear — a spec routing nothing to the reviewer is never
    held on this account."""
    return all(reviewer_cleared(events, spec, cid) for cid in to_reviewer_ids)


def owed_rows_for_routed(events, spec):
    """`to == "owed"` routed criteria as synthetic owed-check inputs:
    `[{"criterion", "capability", "ts"}, ...]`, latest-wins, `to == "none"`
    excluded — `owed.checks`'s own synthesis reads this instead of
    re-deriving the fold itself (R12.i), so it never imports `fold`. `ts` is
    the governing `criterion-routed` event's own timestamp, so a synthetic
    row's `due_at` anchors at ship time exactly like a real `owed-ac`'s does
    when its own `wake_at` equals its own `ts` (no wait)."""
    return [{"criterion": cid, "capability": e.get("capability"), "ts": e.get("ts")}
            for cid, e in latest_routed_events(events, spec).items() if e.get("to") == "owed"]
