#!/usr/bin/env python3
"""L-spec-2138/carried-close: a charter whose remaining work moved to another
charter closes as `carried`, through the tick, and its failures are recorded
against the target.

The sweep is untouched. What it does today for a closed charter (stated here
because R3 asks for it): `sweep_owed.due_candidates` walks every spec and never
reads a charter or its state; `fold.owed_due` and a charter's `owed` tally have
no charter-state filter either. A carried charter's due owed check is therefore
still selected, dispatched and counted. Nothing in this module changes that.

Pure functions over already-folded charters. This module imports no product
module at load (fold imports it); the guard resolves the ledger lazily.
"""
import re

CHARTER_ID = re.compile(r"L-charter-\d{4}")
# A carry may not start from, or land on, a charter in one of these states.
TERMINAL = ("L2-complete", "carried", "retracted")
# The two Thinker-brief conditions the tick raises (look.emit_once), named here so
# the tick's own event vocabulary lists only the events it appends.
INVALID_TARGET = "carry-target-invalid"
FAILURES_AT_CLOSE = "carried-failures-at-close"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def text(value):
    """One line of plain text: control characters collapse to a space."""
    return _CONTROL.sub(" ", str(value)).strip()


def request(evs):
    """The newest ADMITTED `charter-carried` in `evs` (canonical order, so a
    timestamp tie goes to the later line), or None."""
    return next((e for e in reversed(list(evs or ())) if e.get("type") == "charter-carried"), None)


def carried_close(evs):
    """The newest admitted `charter-closed` with label=carried and a usable
    `carried_to`, or None. A carried close without a target is malformed and
    does not make a charter carried."""
    for e in reversed(list(evs or ())):
        if e.get("type") == "charter-closed" and e.get("label") == "carried":
            target = e.get("carried_to")
            if isinstance(target, str) and CHARTER_ID.fullmatch(target):
                return e
            return None
    return None


def _filed(c):
    return bool(c) and any(e.get("type") == "charter-filed" for e in c.get("evs", ()))


def _next_hop(c):
    """The charter a charter's work is going to: its carried target, or the
    target of its pending request while it is not yet terminal."""
    if not c:
        return None
    if c.get("state") == "carried":
        return c.get("carried_to")
    if c.get("state") in TERMINAL:
        return None
    req = request(c.get("evs"))
    return req.get("carried_to") if req else None


def target_problem(source, target, charters):
    """A reason this carry is not allowed, or None when it is."""
    if not isinstance(source, str) or not CHARTER_ID.fullmatch(source):
        return f"source {source!r} is not a charter id"
    if not isinstance(target, str) or not CHARTER_ID.fullmatch(target):
        return f"carried_to {target!r} is not a charter id"
    if source == target:
        return f"{source} cannot be carried into itself"
    src, tgt = charters.get(source), charters.get(target)
    if not _filed(src):
        return f"source {source} is not a filed charter"
    if not _filed(tgt):
        return f"target {target} is not a filed charter"
    if src.get("state") in TERMINAL:
        return f"source {source} is already {src.get('state')}"
    if tgt.get("state") in TERMINAL:
        return f"target {target} is {tgt.get('state')}, not open"
    seen, cur = {source, target}, target
    while True:
        nxt = _next_hop(charters.get(cur))
        if not nxt:
            return None
        if nxt == source:
            return f"carrying {source} into {target} makes a cycle back to {source}"
        if nxt in seen:
            return f"the carry chain from {target} does not end"
        seen.add(nxt)
        cur = nxt


def final_target(target, charters):
    """Follow `carried_to` through carried charters. Stops before following a
    repeated id and at a missing or non-carried endpoint; never guesses."""
    seen, cur = [], target
    while cur not in seen:
        seen.append(cur)
        c = charters.get(cur)
        if not c or c.get("state") != "carried" or not c.get("carried_to"):
            break
        nxt = c["carried_to"]
        if nxt in seen:
            break
        cur = nxt
    return cur


def guard(subject, kv, *, events=None):
    """`(kv, refusal)` for a `charter-carried` about to be appended on `subject`.
    Every failure to establish the answer is a refusal."""
    try:
        fields = kv if isinstance(kv, dict) else {}
        for k in ("carried_to", "why"):
            if not isinstance(fields.get(k), str) or not fields[k].strip():
                return kv, f"charter-carried {subject} needs a non-empty {k}="
        import fold
        if events is None:
            from spawns import current_events
            events = current_events()
        _, charters, _, _ = fold.fold(events)
        problem = target_problem(subject, fields["carried_to"], charters)
    except Exception as exc:                           # undetermined is never clean
        return kv, f"charter-carried {subject} refused: could not establish the target ({exc})"
    return kv, (f"charter-carried {subject} refused: {problem}" if problem else None)


def _close_problem(event):
    if event.get("type") != "charter-closed" or event.get("label") != "carried":
        return None
    target = event.get("carried_to")
    if not isinstance(target, str) or not CHARTER_ID.fullmatch(target):
        return "charter-closed label=carried missing required field(s): carried_to="
    if target not in str(event.get("reason", "")):
        return f"charter-closed label=carried reason= must name the target {target}"
    return None


def _close_request_problem(event, prior):
    if event.get("type") == "charter-closed" and event.get("label") == "carried" and prior is not None:
        asked = request(prior)
        if asked is None or asked.get("carried_to") != event.get("carried_to"):
            return "charter-closed label=carried has no admitted charter-carried naming that target"
    return None


def _append_problem(event, read):
    if event.get("type") != "charter-carried":
        return None
    try:
        events = read()
    except Exception as exc:
        return f"could not read the ledger ({exc})"
    return guard(event["subject"], event, events=events)[1]


def owes_no_review(evs):
    """True for a charter whose spec says `Covers: none`, or one that never had a
    charter review: the target whose carried failures reach the Thinker as one
    brief after its close, because no reviewer packet will list them."""
    evs = list(evs or ())
    filed = [e for e in evs if e.get("type") == "charter-filed"]
    if filed and str(filed[-1].get("covers", "")).strip().lower() in ("none", "null"):
        return True
    return not any(str(e.get("type", "")).startswith("charter-review-") for e in evs)


def records(target_evs):
    """The target's admitted `carried-check-failed` records."""
    return [e for e in (target_evs or ()) if e.get("type") == "carried-check-failed"]


def record(dst, final, source, row):
    """Append one `carried-check-failed` on the final target through the one
    production writer (actor from the destination filename: the tick's). Returns
    the writer's refusal or None. Lives here so the tick's own event list stays
    the events the tick itself decides."""
    import dispatch
    return dispatch.emit(dst, {}, "carried-check-failed", subject=final, source=source, **row)


def failures(source_evs, target_evs, mine, now, checks=None):
    """`{spec, criterion, failed_src}` for every blocking owed check whose
    governing observation is an unmet `owed-failed`, standing or fresh, that
    the target has no record of yet. `proving.to_reopen`'s filter without its
    entered_at prerequisite: a source that never entered Proving still counts."""
    import proving
    source = next((e.get("subject") for e in (source_evs or ()) if e.get("subject")), None)
    if source is None:
        return []
    recorded = {e.get("failed_src") for e in records(target_evs) if e.get("source") == source}
    blockers = {(row["spec"], row["criterion"])
                for row in proving.unproven_checks(mine, now, checks) if row["blocks"]}
    out = []
    for s in mine:
        evs = s.get("evs", [])
        criteria = []
        for e in evs:
            c = e.get("criterion")
            if e.get("type") == "owed-ac" and c and c not in criteria:
                criteria.append(c)
        for c in criteria:
            if (s["id"], c) not in blockers:
                continue
            gov = proving._governing_event(evs, c, now)
            if gov is None or gov.get("type") != "owed-failed" or gov.get("kind") != "unmet":
                continue
            src = gov.get("_src") or f"{s['id']}|{c}|{gov.get('ts')}"
            if src in recorded:
                continue
            recorded.add(src)
            out.append({"spec": s["id"], "criterion": c, "failed_src": src})
    return out


def record_status(rec, specs, now):
    """`still failed` while the record's own observation still governs its
    check; otherwise what replaced it. Undetermined reads as still failed."""
    import proving
    s = specs.get(rec.get("spec")) if specs else None
    if s is None:
        return "still failed (spec not found)"
    gov = proving._governing_event(s.get("evs", []), rec.get("criterion"), now)
    if gov is None:
        return "still failed (no later observation)"
    if gov.get("type") == "owed-failed" and gov.get("_src") == rec.get("failed_src"):
        return "still failed"
    if gov.get("type") == "owed-failed":
        return "superseded by a newer failure"
    return f"no longer failed ({gov.get('type')})"


def record_lines(target_evs, specs, now):
    """One line per record: source, spec, criterion, failed_src, current status."""
    return [f"   {r.get('source')} · {r.get('spec')} · {r.get('criterion')} · "
            f"{r.get('failed_src')} · {record_status(r, specs, now)}"
            for r in records(target_evs)]


def review_items(target_evs, specs, now):
    """The charter-reviewer packet's extra list item: failed checks carried into
    this charter. Empty (no item at all) unless the target holds records."""
    lines = record_lines(target_evs, specs, now)
    if not lines:
        return []
    return ["6. Failed checks carried into this charter (source · spec · criterion · failure ref · "
            "status now). They do not hold the close; list them in your review:", *lines]


def count_buckets(charters):
    """The five charter-only counts. `accepted` is an L2-complete charter whose
    newest charter-closed label is `accepted` or absent; carried never is."""
    out = {"open": 0, "carried": 0, "retracted": 0, "accepted": 0, "closed-other": 0}
    for c in charters.values():
        state = c.get("state")
        if state == "carried":
            out["carried"] += 1
        elif state == "retracted":
            out["retracted"] += 1
        elif state == "L2-complete":
            closed = next((e for e in reversed(c.get("evs", ())) if e.get("type") == "charter-closed"), None)
            label = closed.get("label") if closed else None
            out["accepted" if label in (None, "accepted") else "closed-other"] += 1
        else:
            out["open"] += 1
    return out


def _print_counts(charters):
    for name, count in count_buckets(charters).items():
        print(f"{name}\t{count}")
    return 0
