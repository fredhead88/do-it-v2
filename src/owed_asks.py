#!/usr/bin/env python3
"""owed_asks — the operator-act queue (L-spec-0682 R4).

A check that waits on an operator or production act must show as ONE
plain-English ask with its unblock command, never expire silently. This module
is pure (no network, no clock read of its own, no ledger write):

  queue(evs, now)  -> [{spec, criterion, ask, unblock_command}, ...]
  open_ask_criteria(spec_evs, now) -> {criterion, ...}   (the expiry guard's read)
  guard_expiry(subject, kv)        -> (kv', refusal | None)

`evs` is a flat, ts-sorted ledger event list (every subject); it is folded
through `fold.fold` so an unauthorized actor's event is ignored exactly as the
board ignores it, then `owed.checks` is asked per spec.

A check is "waiting on an act" when its LATEST verdict-shaped event is an
`owed-unobservable` whose `capability` is anything but `elapsed` (an elapsed-
time wait needs no one): `operator-action` is an explicit operator act, and
`ro-dsn` / `droplet` / `browser` / `other` name an environment only an operator
can stand up. It is an ask for as long as the check is neither met, waived,
dropped nor unshipped, and its current route has attempts remaining.
`operator-action` asks never expire on attempts.

`guard_expiry` is the one seam that stops an open ask expiring silently:
`dispatch.emit` calls it for an `escalation-blocking kind=owed-expired`, so
`sweep_owed.sweep_expired` (consumed unmodified) can never escalate a criterion
whose ask is open — an ask is the loud surface, an expiry escalation the silent
one it replaces.

This module appends nothing (AC12): the verifier's allowed event set is owed-met,
owed-failed, owed-unobservable and brief; nothing here waives or drops a check.
"""
import json, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import owed, owed_routes  # noqa: E402

OPEN_STATUSES = ("waiting", "due", "blocked", "expired", "quarantined")

# capability -> what the operator is asked to do. `{why}` is the verifier's own
# plain statement of what it could not do.
_ASK = {
    "operator-action": "{why}",
    "ro-dsn": "The verifier has no read-only production database link, so it cannot read production for this check. {why}",
    "droplet": "The verifier cannot reach the production server read-only for this check. {why}",
    "browser": "The verifier has no working browser for this check. {why}",
    "other": "{why}",
}
_JARGON = re.compile(r"owed-\w+|escalation-blocking|ledger|capability|operator-action|spawn[-_ ]?id|\bevent\b", re.I)
_ANY_ID = re.compile(r"\bL-[a-z][a-z-]*-\d+\b")


def _plain(text, spec):
    """The ask's prose with ledger jargon removed and every id but the spec's
    own replaced — a person reads this, not a parser."""
    text = _ANY_ID.sub(lambda m: m.group(0) if m.group(0) == spec else "another item", str(text or ""))
    text = _JARGON.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" .") or "an action only you can take"


def _by_spec(evs):
    """`{subject: admitted events}` — `fold.fold`'s own authorization applied."""
    import fold
    evs = sorted(evs, key=lambda e: fold.ts(e.get("ts")))
    return fold.fold(evs)[3]


def _last_why(spec_evs, criterion):
    why = None
    for e in spec_evs:
        if e.get("type") == "owed-unobservable" and e.get("criterion") == criterion:
            why = e.get("why") or e.get("evidence") or why
    return why


def _asks_for(spec_evs, now):
    out = []
    for r in owed.checks(spec_evs, now):
        cap = r.get("last_unobservable_capability")
        if cap in (None, "elapsed") or r["status"] not in OPEN_STATUSES:
            continue
        route = owed_routes.route_for(r.get("capability"))
        if (route is not None and route != "operator-action"
                and r.get("route_attempts", {}).get(route, 0) >= owed_routes.MAX_ATTEMPTS):
            continue
        out.append((r, cap))
    return out


def queue(evs, now):
    """One `{spec, criterion, ask, unblock_command}` per waiting check, in spec
    then criterion order. The unblock command re-runs the verifier on exactly
    that check once the act is done — never an event written by hand."""
    rows = []
    for sid, spec_evs in sorted(_by_spec(evs).items()):
        for r, cap in _asks_for(spec_evs, now):
            why = _plain(_last_why(spec_evs, r["criterion"]), r["spec"])
            ask = _ASK.get(cap, _ASK["other"]).format(why=why).replace("..", ".").strip()
            if not ask.endswith((".", "?", "!")):
                ask += "."
            rows.append({"spec": r["spec"], "criterion": r["criterion"],
                         "ask": f"{r['spec']} {r['criterion']} is waiting on you: {ask} "
                                "Once it is done the verifier re-checks it.",
                         "unblock_command": f"doit verify-owed --check {r['spec']}/{r['criterion']}"})
    return sorted(rows, key=lambda x: (x["spec"], x["criterion"]))


def open_ask_criteria(spec_evs, now):
    """The criteria of ONE spec's (admitted, ts-sorted) events whose ask is open."""
    return {r["criterion"] for r, _cap in _asks_for(spec_evs, now)}


def guard_expiry(subject, kv, *, now=None, events=None):
    """`(kv, refusal)` for an `escalation-blocking kind=owed-expired` about to be
    written on `subject`: criteria whose ask is open are stripped (evidence kept
    aligned); when none remain the write is refused with a plain reason. Any
    read failure lets the escalation through — the safe direction for a guard
    whose job is only to quiet a duplicate."""
    from datetime import datetime, timezone
    try:
        crits = json.loads(kv.get("criteria") or "[]")
        if not crits:
            return kv, None
        if events is None:
            import fold
            events = fold.read_events()
        spec_evs = _by_spec(events).get(subject, [])
        held = open_ask_criteria(spec_evs, now or datetime.now(timezone.utc))
        if not held & set(crits):
            return kv, None
        evid = json.loads(kv.get("evidence") or "[]")
        keep = [i for i, c in enumerate(crits) if c not in held]
        if not keep:
            return kv, (f"{subject} {sorted(held & set(crits))} each has an open operator ask; "
                        "an ask is the surface, not an expiry escalation")
        new = dict(kv)
        new["criteria"] = json.dumps([crits[i] for i in keep])
        if len(evid) == len(crits):
            new["evidence"] = json.dumps([evid[i] for i in keep])
        return new, None
    except Exception:  # noqa: BLE001
        return kv, None


def render(rows):
    """The queue as plain lines, one per ask, the unblock command indented."""
    out = []
    for r in rows:
        out += [r["ask"], f"    to unblock: {r['unblock_command']}"]
    return "\n".join(out)


if __name__ == "__main__":
    from datetime import datetime, timezone
    import fold
    print(render(queue(fold.read_events(), datetime.now(timezone.utc))) or "no open asks")
