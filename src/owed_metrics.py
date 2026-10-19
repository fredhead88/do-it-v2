#!/usr/bin/env python3
"""owed_metrics — how many owed checks are open, how old, how fast they reach a
verdict (L-spec-0682 R6). Pure: no network, no clock read, no write.

  summary(evs, now) -> {open, median_age_h, pct_verdict_within_48h, overdue_24h}

`evs` is a flat ts-sorted ledger event list; it is folded through
`fold.fold` (unauthorized actors ignored, as the board does) and
`owed.checks` is asked per spec. `summary_by(by_subject, now)` is the same over
a fold's own `by_subject` dict, for callers (the board) that already hold it.

Definitions, one each, so a number can be re-derived by hand:

  open                  checks still awaiting a successful resolution — shipped,
                        and neither met, waived nor dropped.
  median_age_h          median hours since ship, over the open checks; None if none.
  pct_verdict_within_48h  of the checks that could have reached a verdict by now
                        (shipped >= 48 h ago, or already holding a verdict), the
                        percent whose FIRST owed-met / owed-failed landed within 48 h
                        of ship. None when no check is yet old enough to judge.
                        A verdict is a met or a failed; an operator ask is not one.
  overdue_24h           every open check due more than 24 h ago with no verdict
                        event since its governing owed-ac: [{spec, criterion,
                        due_at, hours_overdue}], oldest first.
"""
import pathlib, statistics, sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import owed  # noqa: E402

OPEN_STATUSES = ("waiting", "due", "blocked", "failed", "correcting", "expired", "quarantined")
VERDICTS = ("owed-met", "owed-failed")
WINDOW = timedelta(hours=48)
OVERDUE = timedelta(hours=24)


def _by_spec(evs):
    import fold
    return fold.fold(sorted(evs, key=lambda e: fold.ts(e.get("ts"))))[3]


def _ts(e, now):
    return owed._ts(e.get("ts"), now)


def summary_by(by_subject, now):
    open_rows, judged, within, overdue = [], 0, 0, []
    for evs in by_subject.values():
        for r in owed.checks(evs, now):
            if r["status"] in ("dropped", "unshipped", "waived") or r["shipped_at"] is None:
                continue
            verdicts = sorted(_ts(e, now) for e in evs if e.get("type") in VERDICTS
                              and e.get("criterion") == r["criterion"]
                              and _ts(e, now) >= r["shipped_at"])
            if verdicts or now - r["shipped_at"] >= WINDOW:
                judged += 1
                if verdicts and verdicts[0] - r["shipped_at"] <= WINDOW:
                    within += 1
            if r["status"] in OPEN_STATUSES:
                open_rows.append(r)
                if r["due_at"] is not None and now - r["due_at"] > OVERDUE and not verdicts:
                    overdue.append({"spec": r["spec"], "criterion": r["criterion"],
                                    "due_at": r["due_at"].isoformat(timespec="seconds"),
                                    "hours_overdue": round((now - r["due_at"]).total_seconds() / 3600, 1)})
    ages = [(now - r["shipped_at"]).total_seconds() / 3600 for r in open_rows]
    overdue.sort(key=lambda x: (x["due_at"], x["spec"], x["criterion"]))
    return {"open": len(open_rows),
            "median_age_h": round(statistics.median(ages), 1) if ages else None,
            "pct_verdict_within_48h": round(100.0 * within / judged, 1) if judged else None,
            "overdue_24h": overdue}


def summary(evs, now):
    return summary_by(_by_spec(evs), now)


def board_line(s):
    """The one board line: open count, median age, 48 h percent."""
    age = "n/a" if s["median_age_h"] is None else f"{s['median_age_h']:.1f}h"
    pct = "n/a" if s["pct_verdict_within_48h"] is None else f"{s['pct_verdict_within_48h']:.0f}%"
    return f"owed checks: {s['open']} open, median age {age}, {pct} reach a verdict within 48h"


def health_line(by_subject, now):
    """`board_line` over a fold's `by_subject`, degrading to a visible line, never a crash."""
    try:
        return board_line(summary_by(by_subject, now))
    except Exception as x:  # noqa: BLE001
        return f"owed checks: unavailable — {type(x).__name__}: {x}"


if __name__ == "__main__":
    from datetime import datetime, timezone
    import fold
    print(board_line(summary(fold.read_events(), datetime.now(timezone.utc))))
