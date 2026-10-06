#!/usr/bin/env python3
"""owed_metrics — open count, median age, 48 h verdict rate, overdue list; the board line and the
overdue brief (L-spec-0682 R6: AC15, AC16). Run: python3 test_owed_metrics.py"""
import json, os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp(prefix="owed-metrics-test-"))
os.environ["HOME"], os.environ["DOIT_ROOT"] = str(TMP / "home"), str(TMP / "root")
os.environ["DOIT_SCRATCH"] = str(TMP / "scratch")
for k in ("DOIT_PROJECT", "DOIT_LEDGER_FILE"):
    os.environ.pop(k, None)
ROOT = pathlib.Path(os.environ["DOIT_ROOT"])
EV = ROOT / "events"
EV.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, look, owed_metrics  # noqa: E402

N = 0
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def at(h):
    """`h` hours before NOW, as the ledger's own timestamp string."""
    return (NOW - timedelta(hours=h)).isoformat(timespec="seconds")


def write(actor, rows):
    with (EV / f"L-{actor}-0001.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps({"v": 1, **r}) + "\n")


def spec(sid, shipped_h, verdict=None, verdict_after_h=None):
    """One owed criterion, due 24 h after ship; an optional verdict `verdict_after_h` after ship."""
    write("spec-writer", [{"ts": at(shipped_h + 1), "type": "owed-ac", "subject": sid, "criterion": "AC1",
                           "wake_at": at(shipped_h - 23), "line": "AC1 OWED prod read"}])
    write("executor", [{"ts": at(shipped_h), "type": "shipped", "subject": sid}])
    if verdict:
        body = {"evidence": "saw it"} | ({"kind": "unmet"} if verdict == "owed-failed" else {})
        write("owed-sweeper", [{"ts": at(shipped_h - verdict_after_h), "type": verdict, "subject": sid,
                                "criterion": "AC1", **body}])


# M1 100h old, nothing said: open, overdue 76h.   M2 60h old, met 10h after ship: closed, within 48h.
# M3 80h old, failed 50h after ship: open, judged, NOT within 48h.   M4 10h old: open, too young to judge.
# M5 30h old, failed 20h after ship: open, within 48h.
spec("L-spec-6001", 100)
spec("L-spec-6002", 60, "owed-met", 10)
spec("L-spec-6003", 80, "owed-failed", 50)
spec("L-spec-6004", 10)
spec("L-spec-6005", 30, "owed-failed", 20)

EXPECTED = {"open": 4, "median_age_h": 55.0, "pct_verdict_within_48h": 50.0,
            "overdue_24h": [{"spec": "L-spec-6001", "criterion": "AC1", "due_at": at(76), "hours_overdue": 76.0}]}


def test_summary_exact_table():
    s = owed_metrics.summary(fold.read_events(), NOW)
    check(s == EXPECTED, f"the full table, not a sample:\n got {s}\nwant {EXPECTED}")
    check(owed_metrics.summary([], NOW) == {"open": 0, "median_age_h": None, "pct_verdict_within_48h": None,
                                            "overdue_24h": []}, "an empty ledger is all zero / n/a")


def test_board_line_and_overdue_brief():
    line = owed_metrics.board_line(EXPECTED)
    check(line == "owed checks: 4 open, median age 55.0h, 50% reach a verdict within 48h", f"the one line: {line}")
    events = fold.read_events()
    specs, charters, ignored, by = fold.fold(events)
    board = fold.render(events, specs, charters, ignored, by)
    health = board.split("## HEALTH", 1)[1]
    check(any(l.strip().startswith("owed checks: ") and " open, median age " in l for l in health.splitlines()),
          f"the board's HEALTH carries the line: {health[:600]}")
    briefs = []
    look._check_owed_overdue(events, NOW, ROOT, briefs)
    check(len(briefs) == 1 and briefs[0]["key"] == "L-spec-6001/AC1" and briefs[0]["condition"] == "owed-overdue",
          f"one brief, for the one check due >24h with no verdict: {briefs}")
    again = []
    look._check_owed_overdue(fold.read_events(), NOW, ROOT, again)
    check(again == [], f"deduped by the existing brief path — a second pass files nothing: {again}")
    written = [e for e in fold.read_events() if e.get("type") == "brief" and e.get("condition") == "owed-overdue"]
    check(len(written) == 1, f"exactly one brief on the ledger: {len(written)}")


test_summary_exact_table()
test_board_line_and_overdue_brief()
import shutil
shutil.rmtree(str(TMP), ignore_errors=True)
print(f"owed_metrics: {N} checks pass")
