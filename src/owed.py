#!/usr/bin/env python3
"""owed — one pure definition of a check, its clock, and its expiry.

`checks(evs, now)` takes ONE subject's own ts-sorted event list (the same
per-subject `evs` `fold.spec_state`/`fold.owed_due` already hold) and returns
one row per criterion declared by a criterion-bearing `owed-ac` in it. No
network, no clock read of its own (`now` is always handed in), no side
effect — pure, so `fold.py` (and anything else) can call it as often as it
likes with no caching.

The clock: `shipped_at` is the ts of the first `shipped` event in `evs`
(`None` when absent). Per criterion, the GOVERNING event is the LATEST
`owed-ac` naming it — last-write-by-position, exactly like the `last_wake`
overwrite this replaces — and `due_at` is the interval the governing event's
own `wake_at` asked for, anchored at ship time, UNLESS the governing event's
own ts is strictly after `shipped_at` (a re-date), in which case `due_at` is
`wake_at` as written. Every "after" here is a strict `>`; an identical
literal `ts` is never "after" either way.

`owed.py` never imports `fold` — `fold.py` imports THIS module, so the
reverse would cycle — so the three terminal shapes (killed / void /
closed-unbuilt) that force a row to `dropped` are duplicated here from
`fold.spec_state`'s own tests, the same duplication `fold.py` already
accepts for `tick.SPEC_DONE`.

L-spec-8034/R12.i: a `to=="owed"` `criterion-routed` criterion (a capability
the grader never has, routed away rather than held) is synthesized into
`last_ac` exactly like a real `owed-ac` would be — `routing.owed_rows_for_
routed` reads the same `evs`, never `fold`, so this stays a leaf module. Its
governing ts and `wake_at` are both the routing event's own `ts`, so `due_at`
lands at ship time with no wait (Assumption/R12.i: "due at once"); an explicit
`owed-ac` on the identical criterion always wins (checked first, so the
synthetic row never overwrites it).
"""
from datetime import timedelta
from datetime import datetime as _datetime
import routing

STATUSES = ("waiting", "due", "met", "waived", "expired", "dropped", "unshipped")


def _ts(s, now):
    """Parse an event's own `ts` field; an unparseable or missing one falls
    back to `now` rather than raising (`fold.ts`'s own fallback, mirrored
    here with `now` as the fallback since `owed.py` carries no NOW of its
    own)."""
    try:
        return _datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now


def _dated(w):
    return w is not None and str(w).strip() != ""


def _dropped(evs, types):
    """killed / void / closed-unbuilt — `fold.spec_state`'s own three
    terminal tests, duplicated rather than imported (see module docstring)."""
    if "spec-killed" in types:
        return True
    if ("spec-written" not in types and "spec-killed" not in types
            and any(e.get("type") == "spawn-started" and e.get("actor") == "spec-writer" for e in evs)
            and any(e.get("type") == "spawn-failed" and e.get("actor") == "spec-writer" for e in evs)):
        return True
    if "spec-closed" in types and "shipped" not in types:
        return True
    return False


def checks(evs, now):
    """One row per (spec, criterion): `spec criterion project declared_src
    line wake_at shipped_at due_at status failures last_failed_at
    last_unobservable_at last_unobservable_capability evidence`.
    `due_at`/`shipped_at` are `datetime.datetime` or `None`; `wake_at` is the
    raw declared string, echoed verbatim. A criterion-less `owed-ac` produces
    no row."""
    types = {e.get("type") for e in evs}
    spec = next((e.get("subject") for e in evs if e.get("subject")), None)
    project = next((e.get("project") for e in evs if e.get("project")), None)
    dropped = _dropped(evs, types)
    shipped_ev = next((e for e in evs if e.get("type") == "shipped"), None)
    shipped_at = _ts(shipped_ev.get("ts"), now) if shipped_ev else None

    last_ac = {}
    for e in evs:
        if e.get("type") == "owed-ac" and e.get("criterion"):
            last_ac[e["criterion"]] = e             # last-write-by-position
    for row in routing.owed_rows_for_routed(evs, spec):
        cid = row["criterion"]
        if cid in last_ac:
            continue  # an explicit owed-ac on the same criterion always wins
        last_ac[cid] = {"type": "owed-ac", "criterion": cid, "ts": row["ts"], "wake_at": row["ts"],
                        "_src": "routing",
                        "line": f"grader capability {row['capability']!r} has no grader — routed to owed"}

    out = []
    for c, ac in last_ac.items():
        governing_ts = _ts(ac.get("ts"), now)
        wake_at = ac.get("wake_at")
        if shipped_at is None or not _dated(wake_at):
            due_at = None
        elif governing_ts > shipped_at:              # strict "after" — a re-date
            due_at = _ts(wake_at, now)
        else:
            due_at = shipped_at + max(timedelta(0), _ts(wake_at, now) - governing_ts)

        met_evs = [e for e in evs if e.get("type") == "owed-met" and e.get("criterion") == c
                   and _ts(e.get("ts"), now) > governing_ts]
        failed_evs = [e for e in evs if e.get("type") == "owed-failed" and e.get("criterion") == c
                      and _ts(e.get("ts"), now) > governing_ts]
        waived_evs = [e for e in evs if e.get("type") == "owed-waived" and e.get("criterion") == c
                      and _ts(e.get("ts"), now) > governing_ts]
        post = [e for e in evs if e.get("criterion") == c
                and e.get("type") in ("owed-failed", "owed-met", "owed-unobservable")
                and _ts(e.get("ts"), now) > governing_ts]

        last_unobservable_at = last_unobservable_capability = None
        if post and post[-1].get("type") == "owed-unobservable":
            last_unobservable_at = _ts(post[-1].get("ts"), now)
            last_unobservable_capability = post[-1].get("capability")

        failures = len(failed_evs)
        last_failed_at = _ts(failed_evs[-1].get("ts"), now) if failed_evs else None
        met = bool(met_evs)
        evidence = (met_evs[-1].get("evidence") if met
                    else failed_evs[-1].get("evidence") if failures else None)

        if dropped:
            status = "dropped"
        elif shipped_at is None:
            status = "unshipped"
        elif met:
            status = "met"
        elif waived_evs:
            status = "waived"
        elif failures >= 2:
            status = "expired"
        elif due_at is not None and (now - due_at) > timedelta(days=7):
            status = "expired"
        elif (due_at is not None and (now - due_at) > timedelta(days=1)
              and last_unobservable_capability not in (None, "elapsed")):
            status = "expired"
        elif due_at is not None and due_at <= now:
            status = "due"
        else:
            status = "waiting"

        out.append({"spec": spec, "criterion": c, "project": project,
                    "declared_src": ac.get("_src"), "line": ac.get("line"),
                    "wake_at": wake_at, "shipped_at": shipped_at, "due_at": due_at,
                    "status": status, "failures": failures,
                    "last_failed_at": last_failed_at,
                    "last_unobservable_at": last_unobservable_at,
                    "last_unobservable_capability": last_unobservable_capability,
                    "evidence": evidence})
    return out


def expired(specs, now):
    """`checks()`'s `status == "expired"` rows, flattened across every spec in
    `specs` (`fold.fold()`'s own `{sid: {"evs": [...], ...}}` dict)."""
    out = []
    for s in specs.values():
        out.extend(r for r in checks(s["evs"], now) if r["status"] == "expired")
    return out
