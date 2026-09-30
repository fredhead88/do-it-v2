#!/usr/bin/env python3
"""One runnable check on owed.py's rules. Run: python3 test_owed.py"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# R8.2: HOME/DOIT_ROOT isolation, set before any project module is imported —
# a direct `python3 test_owed.py` must never resolve fold.ROOT to ~/.do-it.
_TMP8027 = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-iso-"))
os.environ["HOME"] = str(_TMP8027 / "home")
os.environ["DOIT_ROOT"] = str(_TMP8027 / "root")

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import owed  # noqa: E402

NOW = datetime.now(timezone.utc)


def stamp(d=0):
    """`d` days ago as an ISO-8601 string (negative `d` is `d` days in the
    future) — the same convention `test_fold.py`'s own `stamp()` uses."""
    return (NOW - timedelta(days=d)).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


# ── AC1 · STATUSES is the pinned tuple ───────────────────────────────────────
assert owed.STATUSES == ("waiting", "due", "met", "waived", "expired", "dropped",
                          "unshipped"), owed.STATUSES
print("AC1 ok")

# ── AC2 · a criterion declared well before ship: the ship-anchored interval,
# not the raw wake_at, decides due-ness ─────────────────────────────────────
S2 = "L-spec-9002"
ac2 = {"ts": stamp(30), "type": "owed-ac", "subject": S2, "criterion": "AC1",
      "wake_at": stamp(25), "_src": "f:1"}
ship2 = {"ts": stamp(2), "type": "shipped", "subject": S2}
rows2 = owed.checks([ac2, ship2], NOW)
assert len(rows2) == 1, rows2
row2 = rows2[0]
assert row2["status"] == "waiting", row2
assert row2["due_at"] == parse(stamp(-3)), (row2["due_at"], parse(stamp(-3)))
assert isinstance(row2["shipped_at"], datetime) and isinstance(row2["due_at"], datetime), row2
print("AC2 ok")

# ── AC3 · a later executor re-date: due_at == wake_at literally, and the
# pre-ship declaration no longer governs ────────────────────────────────────
redate3 = {"ts": stamp(1), "type": "owed-ac", "subject": S2, "criterion": "AC1",
          "wake_at": stamp(3), "actor": "executor", "_src": "f:2"}
rows3 = owed.checks([ac2, ship2, redate3], NOW)
row3 = rows3[0]
assert row3["due_at"] == parse(stamp(3)), row3
assert row3["status"] == "due", row3
print("AC3 ok")

# ── AC4 · met is existence-only after the governing owed-ac; a later re-date
# reopens it; an identical-ts tie is NOT met (strict >) ─────────────────────
S4 = "L-spec-9004"
ac4 = {"ts": stamp(10), "type": "owed-ac", "subject": S4, "criterion": "AC1", "wake_at": stamp(20)}
ship4 = {"ts": stamp(9), "type": "shipped", "subject": S4}
met4 = {"ts": stamp(5), "type": "owed-met", "subject": S4, "criterion": "AC1", "evidence": "e1"}
rows4 = owed.checks([ac4, ship4, met4], NOW)
assert rows4[0]["status"] == "met", rows4[0]
assert rows4[0]["evidence"] == "e1", rows4[0]

redate4 = {"ts": stamp(1), "type": "owed-ac", "subject": S4, "criterion": "AC1",
          "wake_at": stamp(-5), "actor": "executor"}
rows4b = owed.checks([ac4, ship4, met4, redate4], NOW)
assert rows4b[0]["status"] == "waiting", rows4b[0]

tie_ts = stamp(10)
ac_tie = {"ts": tie_ts, "type": "owed-ac", "subject": S4, "criterion": "AC1", "wake_at": stamp(20)}
met_tie = {"ts": tie_ts, "type": "owed-met", "subject": S4, "criterion": "AC1", "evidence": "e2"}
ship_tie = {"ts": stamp(9), "type": "shipped", "subject": S4}
rows_tie = owed.checks([ac_tie, ship_tie, met_tie], NOW)
assert rows_tie[0]["status"] != "met", rows_tie[0]
print("AC4 ok")

# ── AC5 · two owed-failed after the governing owed-ac expires it regardless
# of due_at; one alone does not; a later owed-met flips it back to met ──────
S5 = "L-spec-9005"
ac5 = {"ts": stamp(10), "type": "owed-ac", "subject": S5, "criterion": "AC1", "wake_at": stamp(-30)}
ship5 = {"ts": stamp(5), "type": "shipped", "subject": S5}
fail1 = {"ts": stamp(3), "type": "owed-failed", "subject": S5, "criterion": "AC1",
        "evidence": "f1", "kind": "unmet"}
fail2 = {"ts": stamp(1), "type": "owed-failed", "subject": S5, "criterion": "AC1",
        "evidence": "f2", "kind": "unmet"}
rows5a = owed.checks([ac5, ship5, fail1], NOW)
assert rows5a[0]["status"] not in ("expired", "met"), rows5a[0]
assert rows5a[0]["failures"] == 1, rows5a[0]

rows5b = owed.checks([ac5, ship5, fail1, fail2], NOW)
assert rows5b[0]["status"] == "expired", rows5b[0]
assert rows5b[0]["failures"] == 2, rows5b[0]

met5 = {"ts": stamp(0), "type": "owed-met", "subject": S5, "criterion": "AC1", "evidence": "m1"}
rows5c = owed.checks([ac5, ship5, fail1, fail2, met5], NOW)
assert rows5c[0]["status"] == "met", rows5c[0]
print("AC5 ok")

# ── AC6 · a clamped interval (wake_at <= governing ts): due_at == shipped_at
# exactly; expired past 7 days, due at 6 (exclusive boundary) ───────────────
S6 = "L-spec-9006"
same_ts6 = stamp(50)
ac6 = {"ts": same_ts6, "type": "owed-ac", "subject": S6, "criterion": "AC1", "wake_at": same_ts6}
ship6a = {"ts": stamp(10), "type": "shipped", "subject": S6}
rows6a = owed.checks([ac6, ship6a], NOW)
assert rows6a[0]["due_at"] == rows6a[0]["shipped_at"], rows6a[0]
assert rows6a[0]["status"] == "expired", rows6a[0]

ship6b = {"ts": stamp(6), "type": "shipped", "subject": S6}
rows6b = owed.checks([ac6, ship6b], NOW)
assert rows6b[0]["due_at"] == rows6b[0]["shipped_at"], rows6b[0]
assert rows6b[0]["status"] == "due", rows6b[0]
print("AC6 ok")

# ── AC7 · the clamped shape, with owed-unobservable deciding expiry ─────────
S7 = "L-spec-9007"
same_ts7 = stamp(60)
ac7 = {"ts": same_ts7, "type": "owed-ac", "subject": S7, "criterion": "AC1", "wake_at": same_ts7}
ship7a = {"ts": stamp(2), "type": "shipped", "subject": S7}
unobs_droplet = {"ts": stamp(1), "type": "owed-unobservable", "subject": S7, "criterion": "AC1",
                 "capability": "droplet", "why": "no reach"}
rows7a = owed.checks([ac7, ship7a, unobs_droplet], NOW)
assert rows7a[0]["status"] == "expired", rows7a[0]
assert rows7a[0]["last_unobservable_capability"] == "droplet", rows7a[0]

unobs_elapsed = {**unobs_droplet, "capability": "elapsed"}
rows7b = owed.checks([ac7, ship7a, unobs_elapsed], NOW)
assert rows7b[0]["status"] == "due", rows7b[0]

ship7c = {"ts": (NOW - timedelta(hours=12)).isoformat(timespec="seconds"), "type": "shipped", "subject": S7}
rows7c = owed.checks([ac7, ship7c, unobs_droplet], NOW)
assert rows7c[0]["status"] == "due", rows7c[0]

fail7 = {"ts": stamp(0), "type": "owed-failed", "subject": S7, "criterion": "AC1",
        "evidence": "f", "kind": "unmet"}
rows7d = owed.checks([ac7, ship7a, unobs_droplet, fail7], NOW)
assert rows7d[0]["status"] == "due", rows7d[0]
assert rows7d[0]["last_unobservable_capability"] is None, rows7d[0]
print("AC7 ok")

# ── AC8 · killed/void/closed-unbuilt rows read "dropped", excluded from
# owed.expired()/fold.owed_due(); unshipped rows read "unshipped" with
# shipped_at/due_at both None, excluded the same way; a criterion-less
# owed-ac produces no row at all ────────────────────────────────────────────
import fold  # noqa: E402 — pure calls only (owed_due/expired), no ledger I/O

S8k = "L-spec-9008k"
ac8k = {"ts": stamp(20), "type": "owed-ac", "subject": S8k, "criterion": "AC1", "wake_at": stamp(15)}
killed8k = {"ts": stamp(1), "type": "spec-killed", "subject": S8k}
ship8k = {"ts": stamp(10), "type": "shipped", "subject": S8k}
rows8k = owed.checks([ac8k, ship8k, killed8k], NOW)
assert rows8k[0]["status"] == "dropped", rows8k
specs8k = {S8k: {"evs": [ac8k, ship8k, killed8k]}}
assert owed.expired(specs8k, NOW) == [], owed.expired(specs8k, NOW)
assert fold.owed_due(specs8k) == [], fold.owed_due(specs8k)

S8u = "L-spec-9008u"
ac8u = {"ts": stamp(20), "type": "owed-ac", "subject": S8u, "criterion": "AC1", "wake_at": stamp(15)}
rows8u = owed.checks([ac8u], NOW)
assert rows8u[0]["status"] == "unshipped", rows8u
assert rows8u[0]["shipped_at"] is None and rows8u[0]["due_at"] is None, rows8u
assert fold.owed_due({S8u: {"evs": [ac8u]}}) == []

S8l = "L-spec-9008l"
loose8 = {"ts": stamp(1), "type": "owed-ac", "subject": S8l, "wake_at": stamp(1)}
ship8l = {"ts": stamp(2), "type": "shipped", "subject": S8l}
assert owed.checks([loose8, ship8l], NOW) == []
print("AC8 ok")

# ── extra: owed.expired() flattens status=="expired" rows across every spec ─
specs = {S6: {"evs": [ac6, ship6a]}, S8k: {"evs": [ac8k, ship8k, killed8k]}}
exp = owed.expired(specs, NOW)
assert {r["spec"] for r in exp} == {S6}, exp

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0470 · ledger-vocabulary R3 (L-charter-0046) — the "waived" status
# ══════════════════════════════════════════════════════════════════════════════

# ── 0470-AC5 · owed.STATUSES gains "waived", inserted directly after "met" ───
assert owed.STATUSES == ("waiting", "due", "met", "waived", "expired", "dropped",
                          "unshipped"), owed.STATUSES
print("0470-AC5 ok")

# ── 0470-AC6 · an owed-waived after the governing owed-ac produces a "waived"
# row: (a) alone; (b) with two prior owed-failed also after governing (waived
# overrides expiry, unlike an unwaived row at 2 failures); (c) the reverse
# order — the two owed-failed timestamped AFTER the owed-waived; (d) a later
# owed-met (also post-governing) added to case (a) makes the row "met", never
# "waived", whichever of the pair has the later ts ──────────────────────────
S6w = "L-spec-9016"
ac6w = {"ts": stamp(10), "type": "owed-ac", "subject": S6w, "criterion": "AC1", "wake_at": stamp(-30)}
ship6w = {"ts": stamp(5), "type": "shipped", "subject": S6w}

# (a) alone
waive6a = {"ts": stamp(3), "type": "owed-waived", "subject": S6w, "criterion": "AC1",
           "reason": "manual review substituted"}
rows6a = owed.checks([ac6w, ship6w, waive6a], NOW)
assert rows6a[0]["status"] == "waived", rows6a[0]

# (b) two prior owed-failed (both after governing owed-ac, both BEFORE the waiver)
fail6b1 = {"ts": stamp(4), "type": "owed-failed", "subject": S6w, "criterion": "AC1",
           "evidence": "f1", "kind": "unmet"}
fail6b2 = {"ts": stamp(3.5), "type": "owed-failed", "subject": S6w, "criterion": "AC1",
           "evidence": "f2", "kind": "unmet"}
waive6b = {"ts": stamp(3), "type": "owed-waived", "subject": S6w, "criterion": "AC1",
           "reason": "manual review substituted"}
rows6b = owed.checks([ac6w, ship6w, fail6b1, fail6b2, waive6b], NOW)
assert rows6b[0]["status"] == "waived", rows6b[0]
assert rows6b[0]["failures"] == 2, rows6b[0]

# (c) the reverse order — both owed-failed timestamped AFTER the owed-waived
waive6c = {"ts": stamp(4), "type": "owed-waived", "subject": S6w, "criterion": "AC1",
           "reason": "manual review substituted"}
fail6c1 = {"ts": stamp(3), "type": "owed-failed", "subject": S6w, "criterion": "AC1",
           "evidence": "f1", "kind": "unmet"}
fail6c2 = {"ts": stamp(2), "type": "owed-failed", "subject": S6w, "criterion": "AC1",
           "evidence": "f2", "kind": "unmet"}
rows6c = owed.checks([ac6w, ship6w, waive6c, fail6c1, fail6c2], NOW)
assert rows6c[0]["status"] == "waived", rows6c[0]

# (d) precedence — a later owed-met (also post-governing) added to case (a):
# "met" wins whichever of the met/waived pair has the later ts
met6d_earlier = {"ts": stamp(4), "type": "owed-met", "subject": S6w, "criterion": "AC1",
                  "evidence": "m1"}
rows6d1 = owed.checks([ac6w, ship6w, met6d_earlier, waive6a], NOW)
assert rows6d1[0]["status"] == "met", rows6d1[0]

met6d_later = {"ts": stamp(2), "type": "owed-met", "subject": S6w, "criterion": "AC1",
                "evidence": "m2"}
rows6d2 = owed.checks([ac6w, ship6w, waive6a, met6d_later], NOW)
assert rows6d2[0]["status"] == "met", rows6d2[0]
print("0470-AC6 ok")

# ── 0470-AC7 · a later executor re-date for the same criterion, timestamped
# after a "waived" row's governing owed-waived, makes the row read "waiting"
# (matching the re-date's own wake_at) again — the redate4-style regression
# AC4 already proves for "met", reused here for "waived" ───────────────────
S7w = "L-spec-9017"
ac7w = {"ts": stamp(10), "type": "owed-ac", "subject": S7w, "criterion": "AC1", "wake_at": stamp(20)}
ship7w = {"ts": stamp(9), "type": "shipped", "subject": S7w}
waive7w = {"ts": stamp(5), "type": "owed-waived", "subject": S7w, "criterion": "AC1",
           "reason": "manual review substituted"}
rows7w = owed.checks([ac7w, ship7w, waive7w], NOW)
assert rows7w[0]["status"] == "waived", rows7w[0]

redate7w = {"ts": stamp(1), "type": "owed-ac", "subject": S7w, "criterion": "AC1",
            "wake_at": stamp(-5), "actor": "executor"}
rows7wb = owed.checks([ac7w, ship7w, waive7w, redate7w], NOW)
assert rows7wb[0]["status"] == "waiting", rows7wb[0]
print("0470-AC7 ok")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-8034 (capability-routing) · AC10 — to=owed routed criteria are owed checks
# ══════════════════════════════════════════════════════════════════════════════
S10 = "L-spec-9010"
routed10 = {"ts": stamp(3), "type": "criterion-routed", "subject": S10, "actor": "grader",
           "criterion": "AC2", "capability": "git", "to": "owed"}
routed10_verify = {"ts": stamp(3), "type": "criterion-routed", "subject": S10, "actor": "grader",
                   "criterion": "verify", "capability": "git", "to": "owed"}
routed10_rev = {"ts": stamp(3), "type": "criterion-routed", "subject": S10, "actor": "grader",
                "criterion": "AC5", "capability": "browser", "to": "reviewer"}
routed10_none = {"ts": stamp(3), "type": "criterion-routed", "subject": S10, "actor": "grader",
                 "criterion": "AC9", "capability": "deploy", "to": "none"}
ship10 = {"ts": stamp(1), "type": "shipped", "subject": S10}
rows10 = owed.checks([routed10, routed10_verify, routed10_rev, routed10_none, ship10], NOW)
ids10 = {r["criterion"] for r in rows10}
assert ids10 == {"AC2", "verify"}, ids10  # to=reviewer and to=none never become owed checks
for r in rows10:
    assert r["status"] == "due", r          # due once the spec is shipped (no wait)
    assert r["declared_src"] == "routing", r
assert fold.owed_due({S10: {"evs": [routed10, routed10_verify, routed10_rev, routed10_none, ship10]}})
print("8034-AC10(a) ok")

# an explicit owed-ac on the same criterion is never duplicated
S10b = "L-spec-9010b"
ac10b = {"ts": stamp(4), "type": "owed-ac", "subject": S10b, "criterion": "AC2",
        "wake_at": stamp(4), "actor": "spec-writer", "_src": "f:1"}
routed10b = {"ts": stamp(3), "type": "criterion-routed", "subject": S10b, "actor": "grader",
            "criterion": "AC2", "capability": "git", "to": "owed"}
ship10b = {"ts": stamp(1), "type": "shipped", "subject": S10b}
rows10b = owed.checks([ac10b, routed10b, ship10b], NOW)
assert len(rows10b) == 1 and rows10b[0]["declared_src"] == "f:1", rows10b
print("8034-AC10(b) ok — no duplicate row")

print("owed: all checks pass")
