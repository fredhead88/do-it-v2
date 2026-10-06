#!/usr/bin/env python3
"""owed_timed — timed owed checks: the first reading on a verifier pass, the second scheduled
(L-spec-0682 R2).

A check that needs two readings (say, two pg_stat_statements snapshots at least
24 h apart) can never be answered if nothing captured the first at ship. A spec
declares such a check by putting `readings=2` and `interval_h=<hours>` on its
`owed-ac` (optionally `reading=pg_stat_statements` or `reading_sql=<a SELECT>`).

  capture_first(spec_id, criterion, reading)   store the first reading, once
  load(spec_id, criterion)                     read it back (fresh read, no cache)
  schedule_second(spec_id, criterion, ...)     append a re-dated `owed-ac` whose
                                               `wake_at` is first reading + interval
  run_pass(evs, reader, now)                   for every shipped spec with a timed
                                               declaration lacking a first reading:
                                               read it, store it, schedule the second

The re-dated `owed-ac` is the existing re-date mechanism (`owed.checks`: a governing
event stamped after ship carries its own `wake_at`), so until that instant the check
is `waiting`, never `expired`; no new event type exists. The first reading lives in
`$DOIT_ROOT/state/owed-readings/`, one JSON file per (spec, criterion). The reader
is injected; the default one reads production only through `scripts/sweep/ro-sql`.
The first reading is taken on the first verifier pass after the `shipped` event (the
pass runs every two hours). Ship-time capture is deferred by the binding scope
correction for L-spec-0682; this module does not install a ship-time hook.
"""
import json, pathlib, subprocess, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, owed  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
LEDGER_NAME = "L-verifier-local.jsonl"
PG_STAT_SQL = ("SELECT queryid, calls, left(query, 200) FROM pg_stat_statements "
               "ORDER BY calls DESC LIMIT 200")


def _dir(root=None):
    return pathlib.Path(root or fold.ROOT) / "state" / "owed-readings"


def _path(spec_id, criterion, root=None):
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))
    return _dir(root) / f"{safe(spec_id)}__{safe(criterion)}.json"


def ledger_path():
    """The module's own ledger (filename-derived actor `verifier`), resolved
    fresh against the CURRENT `fold.EVENTS` on every call."""
    return fold.EVENTS / LEDGER_NAME


def capture_first(spec_id, criterion, reading, *, now=None, root=None):
    """Store the FIRST reading for a check — once. A second call never overwrites
    the stored one (the first reading is the baseline the second is compared to);
    returns the record that is on disk."""
    p = _path(spec_id, criterion, root)
    if p.is_file():
        return json.loads(p.read_text())
    rec = {"spec": spec_id, "criterion": criterion, "reading": reading,
           "captured_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, sort_keys=True))
    return rec


def load(spec_id, criterion, *, root=None):
    p = _path(spec_id, criterion, root)
    return json.loads(p.read_text()) if p.is_file() else None


def schedule_second(spec_id, criterion, *, interval_h, first_at, line=None, ledger=None, events=()):
    """Append ONE re-dated `owed-ac` dating the second reading `interval_h` after
    `first_at` (an ISO string or datetime). Idempotent: a prior re-date by this
    module for the same `first_at` is returned instead of a duplicate. Returns
    the `wake_at` string, or raises ValueError when the append is refused."""
    first = first_at if isinstance(first_at, datetime) else datetime.fromisoformat(str(first_at).replace("Z", "+00:00"))
    wake = (first + timedelta(hours=float(interval_h))).isoformat(timespec="seconds")
    for e in events:
        if (e.get("type") == "owed-ac" and e.get("subject") == spec_id and e.get("criterion") == criterion
                and e.get("second_reading_of") == first.isoformat(timespec="seconds")):
            return e.get("wake_at")
    reason = dispatch.emit(ledger or ledger_path(), {"subject": spec_id}, "owed-ac", criterion=criterion,
                           wake_at=wake, line=line or f"second reading, {interval_h} h after the first",
                           second_reading_of=first.isoformat(timespec="seconds"), interval_h=interval_h)
    if reason:
        raise ValueError(reason)
    return wake


def timed_declarations(spec_evs):
    """`{criterion: governing-declaration}` for each criterion whose governing
    DECLARING `owed-ac` says `readings` >= 2 and names an `interval_h`."""
    out = {}
    for e in spec_evs:
        if e.get("type") == "owed-ac" and e.get("criterion") and not e.get("second_reading_of"):
            out[e["criterion"]] = e
    keep = {}
    for c, e in out.items():
        try:
            if int(e.get("readings", 0)) >= 2 and float(e.get("interval_h", 0)) > 0:
                keep[c] = e
        except (TypeError, ValueError):
            pass
    return keep


def default_reader(spec_id, criterion, decl):
    """A read-only production reading through `scripts/sweep/ro-sql`; `None` when
    the declaration names no readable query or the read does not succeed."""
    import sweep_wrappers
    sql = decl.get("reading_sql") or (PG_STAT_SQL if decl.get("reading") == "pg_stat_statements" else None)
    if not sql or sweep_wrappers.sql_statement_refusal(sql) or not decl.get("project"):
        return None
    r = subprocess.run([str(HERE.parent / "scripts" / "sweep" / "ro-sql"), decl["project"], sql],
                       capture_output=True, text=True, timeout=120)
    return {"sql": sql, "rows": r.stdout[:6000]} if r.returncode == 0 else None


def run_pass(by_subject, now, reader=None, *, root=None, ledger=None, events=()):
    """For every shipped spec carrying a timed declaration: ensure the first reading
    is stored and the second is scheduled. Returns one line per action taken."""
    reader, lines = reader or default_reader, []
    for sid, spec_evs in sorted(by_subject.items()):
        if not any(e.get("type") == "shipped" for e in spec_evs) or owed._dropped(
                spec_evs, {e.get("type") for e in spec_evs}):
            continue
        for crit, decl in sorted(timed_declarations(spec_evs).items()):
            rec = load(sid, crit, root=root)
            if rec is None:
                reading = reader(sid, crit, decl)
                if reading is None:
                    lines.append(f"{sid} {crit}: first reading not takeable this pass")
                    continue
                rec = capture_first(sid, crit, reading, now=now, root=root)
                lines.append(f"{sid} {crit}: first reading stored")
            try:
                wake = schedule_second(sid, crit, interval_h=decl["interval_h"], first_at=rec["captured_at"],
                                       line=decl.get("line"), ledger=ledger,
                                       events=[e for e in list(events) + spec_evs])
                lines.append(f"{sid} {crit}: second reading due {wake}")
            except ValueError as x:
                lines.append(f"{sid} {crit}: second reading not scheduled: {x}")
    return lines
