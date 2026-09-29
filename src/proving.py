#!/usr/bin/env python3
"""proving — one pure module deciding whether a finished charter is Proving
or Reopened, what work remains, when the next check is due, its deadline,
who owns the next step, and its display colour.

No I/O of its own: every function here is pure over its arguments — a
charter's own ts-sorted event slice (`charter_evs`), its non-void spec dicts
(`mine`, `fold.fold()`'s own per-spec shape), and a caller-supplied `now`.
`phase`, `entered_at`, `should_enter`, `to_reopen` and `summary` import
neither `fold` nor anything that imports `fold`, at module level or inside
themselves — `fold.py` imports THIS module's siblings (`owed.py`) the same
way, and the reverse would cycle; only the CLI's `__main__` block below
lazily imports `fold` (Plan Consumes, verbatim: "proving.py never imports
fold at module level").

Name "Proving" is a proposal (charter L-charter-0046): `LABEL` is the one
place the literal string appears; every branch below compares the literal
strings `"proving"`/`"reopened"`, so relabeling never touches the rule.

R6 (`L-spec-0474`, proving-backfill): `backfill()` classifies the existing
population of L1-complete/proving/reopened/not-yet-reaped charters into
open/proving/closed/retracted, once. It is the one function besides the CLI's
own `__main__` block that imports `fold` — lazily, inside itself, never at
module level, for the identical reason (no import cycle with `fold.py`,
which imports this module's sibling `owed.py`).
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import owed

LABEL = "Proving"
GRACE_DAYS = 3
UNDATED_DAYS = 7
DISCHARGED = ("met", "waived", "dropped")
BUILD_DONE = ("accepted", "shipped", "shipped-owed-due", "shipped-owed-evidence",
              "shipped-owed-expired", "closed-shipped", "closed-unbuilt", "killed", "dropped")

# R2/R5's kind order — also the tie-break order `summary()`'s `next` uses
# when two items share a `due_at`.
_KIND_ORDER = ("owed-check", "spec-review", "sweep", "brief", "killed-spec",
               "l1-missing", "charter-review")
_KIND_RANK = {k: i for i, k in enumerate(_KIND_ORDER)}

# R4's governing post-declaration types — wider than `owed.checks()`'s own
# internal "post" list (that one omits `owed-waived`; this unit's callers
# need it, per SD9/ledger-vocabulary).
_POST_TYPES = ("owed-failed", "owed-met", "owed-unobservable", "owed-waived")


def _parse_ts(s):
    """Strict ISO-8601 parse: `None` on anything unparseable or missing —
    never a clock fallback (`entered_at`'s own contract, reused everywhere
    this module reads a `ts` with no `now` to fall back to)."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _ts_fallback(s, now):
    """Same parse, but falls back to `now` on failure — `owed.py`'s own
    `_ts` convention, used only where a `now` is already in hand (R4/R2's
    governing-event math, which all take `now`)."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _newest_by_type(evs, etype):
    """The event of type `etype` in `evs` with the latest parseable `ts`;
    `(None, None)` when none exists or none parses. Events with an
    unparseable `ts` are excluded from the comparison, never treated as
    "now" (this module reads no clock of its own for `phase`/`entered_at`/
    `should_enter`)."""
    candidates = [(e, _parse_ts(e.get("ts"))) for e in evs if e.get("type") == etype]
    candidates = [(e, ts) for e, ts in candidates if ts is not None]
    if not candidates:
        return None, None
    return max(candidates, key=lambda p: p[1])


def _governing_ac(evs, criterion):
    """The LATEST `owed-ac` naming `criterion` — last-write-by-position,
    `owed.checks()`'s own internal `last_ac` convention, duplicated here
    since its rows expose neither `kind` nor `_src` (R4, SD4)."""
    last = None
    for e in evs:
        if e.get("type") == "owed-ac" and e.get("criterion") == criterion:
            last = e
    return last


def _governing_post(evs, criterion, after_ts, now):
    """The newest of `_POST_TYPES` on `criterion` with `ts` strictly after
    `after_ts` (the governing `owed-ac`'s own ts) — `owed.checks()`'s own
    "post" definition, duplicated per-event and widened to include
    `owed-waived` (R4's docstring, verbatim)."""
    candidates = []
    for e in evs:
        if e.get("criterion") != criterion or e.get("type") not in _POST_TYPES:
            continue
        ts = _ts_fallback(e.get("ts"), now)
        if ts > after_ts:
            candidates.append((ts, e))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p[0])[1]


def _governing_event(evs, criterion, now):
    """`_governing_ac` + `_governing_post`, composed — written once and
    reused by both `to_reopen()` and `summary()`'s `owed-check` owner
    (Assumptions)."""
    ac = _governing_ac(evs, criterion)
    if ac is None:
        return None
    ac_ts = _ts_fallback(ac.get("ts"), now)
    return _governing_post(evs, criterion, ac_ts, now)


def phase(charter_evs, mine):
    """`"proving"` | `"reopened"` | `None` — R1/SD1/SD2. No L2-completeness
    check of its own: callers invoke this only on a charter not already
    L2-complete per `fold._closable` (Assumptions); this unit's CLI (R5)
    replicates that short-circuit explicitly."""
    types = {e.get("type") for e in charter_evs}
    if "charter-retracted" in types:
        return None

    entry = "l1-complete" in types
    if not entry and "cut-written" in types and len(mine) >= 1:
        ended_ev, ended_ts = _newest_by_type(charter_evs, "planner-ended")
        if ended_ev is not None:
            _, started_ts = _newest_by_type(charter_evs, "planner-started")
            if started_ts is None or ended_ts > started_ts:
                entry = True
    if not entry:
        return None

    for s in mine:
        if s.get("state") not in BUILD_DONE:
            return None
        if s.get("state") == "shipped" and s.get("rejects", 0) > 0:
            return None                                   # rework, SD2 revision 2

    _, proving_ts = _newest_by_type(charter_evs, "charter-proving")
    _, reopened_ts = _newest_by_type(charter_evs, "charter-reopened")
    if reopened_ts is not None and (proving_ts is None or reopened_ts > proving_ts):
        return "reopened"
    return "proving"


def entered_at(charter_evs):
    """The `ts` of the newest `charter-proving` event, parsed; `None` when
    none exists or its `ts` fails to parse. Never falls back to "now"."""
    _, ts = _newest_by_type(charter_evs, "charter-proving")
    return ts


def should_enter(charter_evs, mine):
    """SD3 — `True` exactly on first entry, or on a reopen whose own
    criterion has since been fixed."""
    if phase(charter_evs, mine) is None:
        return False

    proving_ev, proving_ts = _newest_by_type(charter_evs, "charter-proving")
    if proving_ev is None:
        return True                                        # (a) first entry

    reopened_ev, reopened_ts = _newest_by_type(charter_evs, "charter-reopened")
    if reopened_ev is None or not (reopened_ts > proving_ts):
        return False                                        # already proving, nothing to re-enter

    spec_id = reopened_ev.get("spec")
    criterion = reopened_ev.get("criterion")
    target = next((s for s in mine if s.get("id") == spec_id), None)
    if target is None:
        return False

    for e in target.get("evs", []):
        et = e.get("type")
        if et not in ("owed-met", "owed-waived", "owed-ac"):
            continue
        if e.get("criterion") != criterion:
            continue
        if et == "owed-ac" and e.get("actor") != "executor":
            continue
        e_ts = _parse_ts(e.get("ts"))
        if e_ts is not None and e_ts > reopened_ts:
            return True
    return False


def to_reopen(charter_evs, mine, now, checks=None):
    """R4/SD9 — `{"spec","criterion","failed_src"}` rows for every criterion
    whose governing post-declaration event is a fresh, unmet failure.
    `checks=` is accepted for signature symmetry with `summary()` only —
    never read, never mutated (kind/`_src` are not on `owed.checks()`'s own
    rows)."""
    entered = entered_at(charter_evs)
    if entered is None:
        return []

    used_srcs = {e.get("failed_src") for e in charter_evs if e.get("type") == "charter-reopened"}
    out = []
    for s in mine:
        evs = s.get("evs", [])
        criteria = []
        for e in evs:
            c = e.get("criterion")
            if e.get("type") == "owed-ac" and c and c not in criteria:
                criteria.append(c)
        for c in criteria:
            gov = _governing_event(evs, c, now)
            if gov is None:
                continue
            if gov.get("type") != "owed-failed" or gov.get("kind") != "unmet":
                continue
            gov_ts = _ts_fallback(gov.get("ts"), now)
            if not (gov_ts > entered):
                continue
            src = gov.get("_src")
            if src in used_srcs:
                continue
            out.append({"spec": s["id"], "criterion": c, "failed_src": src})
    return out


def summary(charter_id, charter_evs, mine, now, *, title, review_owed, open_briefs, checks=None):
    """R2/R5/SD4-SD6 — the JSON-ready per-charter summary. `checks` is
    `dict[spec_id -> list[owed.checks() rows]] | None`; `None` computes it
    per spec, a supplied dict is used verbatim, never recomputed over."""
    types = {e.get("type") for e in charter_evs}
    entered = entered_at(charter_evs)
    entered_or_now = entered if entered is not None else now
    base_due = entered_or_now                              # spec-review/sweep/brief/killed-spec/
                                                             # l1-missing/charter-review's own due_at
    default_owed_due = entered_or_now + timedelta(days=UNDATED_DAYS)

    checks_map = checks if checks is not None else {
        s["id"]: owed.checks(s.get("evs", []), now) for s in mine}

    items = []

    for s in mine:                                          # owed-check
        sid = s["id"]
        s_evs = s.get("evs", [])
        for row in checks_map.get(sid, []):
            if row.get("status") in DISCHARGED:
                continue
            crit = row.get("criterion")
            text = row.get("line") or f"{sid} {crit}"
            due = row.get("due_at") if row.get("due_at") is not None else default_owed_due
            gov = _governing_event(s_evs, crit, now)
            if row.get("status") == "expired" or (gov is not None and gov.get("type") == "owed-unobservable"):
                owner = "operator"
            elif gov is not None and gov.get("type") == "owed-failed" and gov.get("kind") == "unmet":
                owner = "thinker"
            else:
                owner = "owed-sweeper"
            items.append({"kind": "owed-check", "spec": sid, "criterion": crit, "text": text,
                          "due_at": due, "status": row.get("status"), "owner": owner})

    for s in mine:                                          # spec-review
        if s.get("state") == "shipped":
            items.append({"kind": "spec-review", "spec": s["id"], "criterion": None,
                          "text": f"{s['id']} awaits its grade/review", "due_at": base_due,
                          "status": None, "owner": "executor"})

    if "sweep-fixpoint" not in types:                        # sweep
        items.append({"kind": "sweep", "spec": None, "criterion": None,
                      "text": "sweep not yet run", "due_at": base_due,
                      "status": None, "owner": "executor"})

    for e in open_briefs:                                    # brief
        items.append({"kind": "brief", "spec": None, "criterion": None,
                      "text": e.get("why"), "due_at": base_due,
                      "status": None, "owner": "executor"})

    done_by_id = {s["id"]: s for s in mine}
    for s in mine:                                           # killed-spec
        if s.get("state") != "killed":
            continue
        kill_ev = next((e for e in reversed(s.get("evs", [])) if e.get("type") == "spec-killed"), None)
        repl_id = kill_ev.get("superseded_by") if kill_ev else None
        repl = done_by_id.get(repl_id) if repl_id else None
        repl_done = repl is not None and repl["state"] in BUILD_DONE
        admitted = any(e.get("type") == "kill-accepted" and e.get("actor") in ("operator", "thinker")
                       for e in s.get("evs", []))
        if not repl_done and not admitted:
            items.append({"kind": "killed-spec", "spec": s["id"], "criterion": None,
                          "text": f"{s['id']} was killed with no replacement; accept the kill or replace it",
                          "due_at": base_due, "status": None, "owner": "thinker"})

    if "l1-complete" not in types:                           # l1-missing
        items.append({"kind": "l1-missing", "spec": None, "criterion": None,
                      "text": "planner ended without l1-complete; confirm every slot was written",
                      "due_at": base_due, "status": None, "owner": "operator"})

    blocking = any(it["kind"] in ("owed-check", "spec-review", "killed-spec") for it in items)
    if review_owed and not blocking:                         # charter-review
        items.append({"kind": "charter-review", "spec": None, "criterion": None,
                      "text": "charter review is owed", "due_at": base_due,
                      "status": None, "owner": "executor"})

    all_due = [entered_or_now]                                # deadline: every owed row, discharged too
    for s in mine:
        for row in checks_map.get(s["id"], []):
            all_due.append(row.get("due_at") if row.get("due_at") is not None else default_owed_due)
    deadline = max(all_due) + timedelta(days=GRACE_DAYS)

    def sort_key(it):
        return (it["due_at"], _KIND_RANK[it["kind"]], it["spec"] or "", it["criterion"] or "")

    next_item = min(items, key=sort_key) if items else None
    next_out = ({"text": next_item["text"], "due_at": _iso(next_item["due_at"]),
                "kind": next_item["kind"]} if next_item else None)
    owner_out = next_item["owner"] if next_item is not None else "tick"

    today = now.date()
    overdue = now > deadline or any(it["due_at"].date() < today for it in items)
    due_today = any(it["due_at"].date() == today for it in items)
    colour = "overdue" if overdue else ("due-today" if due_today else "on-time")

    out_items = [{**it, "due_at": _iso(it["due_at"])} for it in items]

    return {
        "id": charter_id,
        "title": title,
        "phase": phase(charter_evs, mine),
        "label": LABEL,
        "entered_at": _iso(entered) if entered is not None else "entry not recorded",
        "remaining": len(items),
        "next": next_out,
        "deadline": _iso(deadline),
        "owner": owner_out,
        "colour": colour,
        "items": out_items,
    }


def _proving_reason(p):
    """R6: the `reason` string for a `"proving"`/`"reopened"` population row.
    `p` is a `summary()`-shaped dict (or a hand-built two-key stub, AC3) —
    only `"remaining"` and `"next"` are read, nothing else on `p` is
    required."""
    reason = f'{p["remaining"]} items remaining'
    nxt = p.get("next")
    if nxt is not None:
        reason += f', next {nxt["kind"]} {nxt["due_at"][:10]}'
    return reason


def _open_reason(mine):
    """R6: the `reason` string for an `"open"` population row (an
    `L1-complete` charter, not yet build-done). `mine` is already sorted by
    `id` — the first element failing the identical "not build-done" test
    `phase()` applies (BUILD_DONE membership, plus the shipped-with-rejects
    rework carve-out) is the reason; `"no specs yet"` when none exists (or
    `mine` is empty)."""
    first = next((s for s in mine if s["state"] not in BUILD_DONE
                  or (s["state"] == "shipped" and s.get("rejects", 0) > 0)), None)
    if first is None:
        return "no specs yet"
    return f'{first["id"]} {first["state"]}'


def backfill(events, now):
    """R6 (`L-spec-0474`): classify every charter currently at `L1-complete`,
    `proving`, `reopened`, or `L2-complete`/`retracted`-not-yet-reaped into
    exactly one of `open`/`proving`/`closed`/`retracted`, once. Returns one
    `{"charter", "klass", "reason"}` dict per population member, sorted by
    `charter` ascending (determinism) — never a second, hand-maintained list;
    the population and every field read here come from `fold.fold()`'s own
    output (charter constraint: "the state is derived from the ledger by
    fold")."""
    import fold                                              # lazy: never at module level

    specs, charters, ignored, by_subject = fold.fold(events)
    rows = []
    for cid in sorted(charters):
        c = charters[cid]
        state = c["state"]
        reaped = any(e.get("type") == "tree-reaped" for e in c.get("evs", []))
        in_population = (
            state in ("L1-complete", "proving", "reopened")
            or (state == "L2-complete" and not reaped)
            or (state == "retracted" and not reaped)
        )
        if not in_population:
            continue
        if state in ("proving", "reopened"):
            klass, reason = "proving", _proving_reason(c["proving"])
        elif state == "L2-complete":
            klass, reason = "closed", "L2 predicate holds"
        elif state == "retracted":
            klass, reason = "retracted", "retracted"
        else:                                                # "L1-complete" (or defensively "open")
            mine = sorted((s for s in specs.values()
                          if s["charter"] == cid and s["state"] != "void"), key=lambda s: s["id"])
            klass, reason = "open", _open_reason(mine)
        rows.append({"charter": cid, "klass": klass, "reason": reason})
    return rows


def _classify_actor():
    """R6: the SAME actor-from-filename rule `fold.read_events()` already
    applies — never a second, independently-written rule that could drift."""
    fname = os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    stem = pathlib.Path(fname).stem
    parts = stem.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else stem


def _backfill_report(rows, today):
    o = sum(1 for r in rows if r["klass"] == "open")
    p = sum(1 for r in rows if r["klass"] == "proving")
    c = sum(1 for r in rows if r["klass"] == "closed")
    r_ = sum(1 for r in rows if r["klass"] == "retracted")
    lines = [f"# Proving backfill — {today}", "",
             f"{len(rows)} charters classified: {o} open, {p} proving, {c} closed, "
             f"{r_} retracted.", ""]
    for row in rows:
        lines.append(f"- {row['charter']} · {row['klass']} · {row['reason']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    _argv = sys.argv[1:]

    # R6 (`L-spec-0474`): `--apply` is additive to `--backfill` only — alone,
    # it is a usage error. Checked first, before any import/read/fold, so it
    # never reads the ledger at all.
    if "--apply" in _argv and "--backfill" not in _argv:
        print("usage: proving.py --backfill [--apply] [--out PATH]", file=sys.stderr)
        sys.exit(2)

    import fold                                              # lazy: never at module level

    if "--backfill" in _argv:
        # No try/except here, same R5 convention as the default board below:
        # any exception (a torn ledger, a directory where an events/*.jsonl
        # should be) propagates uncaught, nothing on stdout, non-zero exit.
        # `rows` is built in full before anything prints or is written.
        events = fold.read_events()
        rows = backfill(events, fold.NOW)
        today = fold.NOW.date().isoformat()
        report_text = _backfill_report(rows, today)

        out_arg = None
        if "--out" in _argv:
            i = _argv.index("--out")
            if i + 1 < len(_argv):
                out_arg = _argv[i + 1]
        out_path = (pathlib.Path(out_arg) if out_arg
                    else fold.ROOT / "content" / f"proving-backfill-{today}.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_text)
        print(report_text, end="")

        if "--apply" in _argv:
            actor = _classify_actor()
            if actor not in fold.EMITS["charter-classified"]:
                print(f"refused: actor {actor!r} may not append charter-classified "
                      f"(admitted: {sorted(fold.EMITS['charter-classified'])})", file=sys.stderr)
                sys.exit(1)
            _specs2, charters2, _ignored2, _by_subject2 = fold.fold(events)
            for row in rows:
                cid = row["charter"]
                already = any(e.get("type") == "charter-classified"
                              for e in charters2.get(cid, {}).get("evs", []))
                if already:
                    continue
                try:
                    fold.append(["charter-classified", cid, f"klass={row['klass']}",
                                f"reason={row['reason']}"])
                except SystemExit:
                    print(cid, file=sys.stderr)
                    raise
        sys.exit(0)

    # No try/except here: any exception (a torn ledger, a directory where an
    # events/*.jsonl should be) propagates uncaught — Python's default exits
    # non-zero with nothing on stdout (the traceback goes to stderr), which is
    # exactly R5's contract. `results` is built in full before anything
    # prints, so a mid-fold exception can never leave a partial list behind.
    events = fold.read_events()
    specs, charters, ignored, by_subject = fold.fold(events)
    results = []
    for cid, c in charters.items():
        mine = [s for s in specs.values() if s["charter"] == cid and s["state"] != "void"]
        if fold.l2_complete(fold._closable(c["evs"], mine)):
            continue
        ph = phase(c["evs"], mine)
        if ph is None:
            continue
        filed = [e for e in c["evs"] if e.get("type") == "charter-filed"]
        title = filed[-1].get("title") if filed and filed[-1].get("title") else cid
        review_owed = fold.charter_review_owed(c["evs"])
        open_briefs_list = fold.open_briefs(c["evs"])
        results.append(summary(cid, c["evs"], mine, fold.NOW, title=title,
                               review_owed=review_owed, open_briefs=open_briefs_list))
    results.sort(key=lambda r: r["deadline"])

    if "--json" in sys.argv[1:]:
        print(json.dumps(results))
    else:
        for r in results:
            next_part = ("nothing remaining" if r["next"] is None
                        else f"next {r['next']['kind']} {r['next']['due_at']}")
            print(f"{r['id']} · {r['title']} · {r['remaining']} left · {next_part} · "
                  f"deadline {r['deadline']} · owner {r['owner']} · {r['colour']}")
