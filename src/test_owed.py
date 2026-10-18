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
assert owed.STATUSES == ("waiting", "due", "blocked", "failed", "correcting", "quarantined",
                          "met", "waived", "expired", "dropped", "unshipped", "unverifiable"), owed.STATUSES
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

# ── AC5 · failures remain failed regardless of count or due_at;
# a later owed-met flips the row back to met ──────
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
assert rows5b[0]["status"] == "failed", rows5b[0]
assert rows5b[0]["failures"] == 2, rows5b[0]

met5 = {"ts": stamp(0), "type": "owed-met", "subject": S5, "criterion": "AC1", "evidence": "m1"}
rows5c = owed.checks([ac5, ship5, fail1, fail2, met5], NOW)
assert rows5c[0]["status"] == "met", rows5c[0]
print("AC5 ok")

# ── AC6 · a clamped interval (wake_at <= governing ts): due_at == shipped_at
# exactly; both old and recent rows remain due ───────────────
S6 = "L-spec-9006"
same_ts6 = stamp(50)
ac6 = {"ts": same_ts6, "type": "owed-ac", "subject": S6, "criterion": "AC1", "wake_at": same_ts6}
ship6a = {"ts": stamp(10), "type": "shipped", "subject": S6}
rows6a = owed.checks([ac6, ship6a], NOW)
assert rows6a[0]["due_at"] == rows6a[0]["shipped_at"], rows6a[0]
assert rows6a[0]["status"] == "due", rows6a[0]

ship6b = {"ts": stamp(6), "type": "shipped", "subject": S6}
rows6b = owed.checks([ac6, ship6b], NOW)
assert rows6b[0]["due_at"] == rows6b[0]["shipped_at"], rows6b[0]
assert rows6b[0]["status"] == "due", rows6b[0]
print("AC6 ok")

# ── AC7 · unobservable stays blocked; a later failure becomes failed ─────────
S7 = "L-spec-9007"
same_ts7 = stamp(60)
ac7 = {"ts": same_ts7, "type": "owed-ac", "subject": S7, "criterion": "AC1", "wake_at": same_ts7}
ship7a = {"ts": stamp(2), "type": "shipped", "subject": S7}
unobs_droplet = {"ts": stamp(1), "type": "owed-unobservable", "subject": S7, "criterion": "AC1",
                 "capability": "droplet", "why": "no reach"}
rows7a = owed.checks([ac7, ship7a, unobs_droplet], NOW)
assert rows7a[0]["status"] == "blocked", rows7a[0]
assert rows7a[0]["last_unobservable_capability"] == "droplet", rows7a[0]

unobs_elapsed = {**unobs_droplet, "capability": "elapsed"}
rows7b = owed.checks([ac7, ship7a, unobs_elapsed], NOW)
assert rows7b[0]["status"] == "blocked", rows7b[0]

ship7c = {"ts": (NOW - timedelta(hours=12)).isoformat(timespec="seconds"), "type": "shipped", "subject": S7}
rows7c = owed.checks([ac7, ship7c, unobs_droplet], NOW)
assert rows7c[0]["status"] == "blocked", rows7c[0]

fail7 = {"ts": stamp(0), "type": "owed-failed", "subject": S7, "criterion": "AC1",
        "evidence": "f", "kind": "unmet"}
rows7d = owed.checks([ac7, ship7a, unobs_droplet, fail7], NOW)
assert rows7d[0]["status"] == "failed", rows7d[0]
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
unknown6 = {"ts": stamp(1), "type": "owed-unobservable", "subject": S6,
            "criterion": "AC1", "capability": "zzz", "why": "no route"}
specs = {S6: {"evs": [ac6, ship6a, unknown6]}, S8k: {"evs": [ac8k, ship8k, killed8k]}}
exp = owed.expired(specs, NOW)
assert {r["spec"] for r in exp} == {S6}, exp

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0470 · ledger-vocabulary R3 (L-charter-0046) — the "waived" status
# ══════════════════════════════════════════════════════════════════════════════

# ── 0470-AC5 · owed.STATUSES gains "waived", inserted directly after "met" ───
assert owed.STATUSES == ("waiting", "due", "blocked", "failed", "correcting", "quarantined",
                          "met", "waived", "expired", "dropped", "unshipped", "unverifiable"), owed.STATUSES
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

# L-spec-1003: lifecycle, governing clock, and pure row contract.
import ast
from unittest.mock import patch

assert owed.STATUSES == ("waiting", "due", "blocked", "failed", "correcting",
                         "quarantined", "met", "waived", "expired", "dropped", "unshipped", "unverifiable")
print("owed-lifecycle AC1 ok")

LIFE = datetime(2026, 10, 15, tzinfo=timezone.utc)


def life_event(etype, hour=0, **fields):
    return {"type": etype, "ts": (LIFE + timedelta(hours=hour)).isoformat(),
            "subject": "L-spec-lifecycle", "criterion": "AC1", **fields}


LIFE_AC = life_event("owed-ac", -48, wake_at=(LIFE - timedelta(hours=48)).isoformat(),
                     project="do-it-v2", line="check", _src="declaration")
LIFE_SHIP = life_event("shipped", -24)
LIFE_BASE = [LIFE_AC, LIFE_SHIP]


def life_row(extra=(), now=LIFE, base=LIFE_BASE):
    return owed.checks(sorted([*base, *extra], key=lambda e: e["ts"]), now)[0]


failure = life_event("owed-failed", -12, evidence="failure", kind="unmet")
unobservable = life_event("owed-unobservable", -10, capability="repo", why="need checkout")
quarantine = life_event("owed-quarantined", -8, reason="repeated drift", spec_sha256="digest")
corrective = life_event("owed-corrective", -6, corrective="L-spec-fix")
met_life = life_event("owed-met", -4, evidence="verified")
waived_life = life_event("owed-waived", -2, reason="operator decision")
waiting_base = [{**LIFE_AC, "wake_at": (LIFE + timedelta(hours=24)).isoformat()}, LIFE_SHIP]
cases_life = [
    ("waiting", waiting_base, []), ("due", LIFE_BASE, []),
    ("blocked", LIFE_BASE, [unobservable]), ("failed", LIFE_BASE, [failure]),
    ("correcting", LIFE_BASE, [failure, corrective]),
    ("quarantined", LIFE_BASE, [quarantine]), ("met", LIFE_BASE, [met_life]),
    ("waived", LIFE_BASE, [waived_life]),
    ("expired", LIFE_BASE, [{**unobservable, "capability": "zzz"}]),
    ("dropped", LIFE_BASE, [life_event("spec-killed", -1)]),
    ("unshipped", [LIFE_AC], []),
    ("failed", LIFE_BASE, [failure, quarantine]),
    ("correcting", LIFE_BASE, [failure, corrective]),
    ("met", LIFE_BASE, [failure, met_life]),
    ("failed", LIFE_BASE, [failure, unobservable]),
    ("quarantined", LIFE_BASE, [unobservable, quarantine]),
    ("blocked", LIFE_BASE, [quarantine, {**unobservable, "ts": life_event("unused", -1)["ts"]}]),
    ("waived", LIFE_BASE, [failure, corrective, waived_life]),
    ("met", LIFE_BASE, [met_life, failure, waived_life]),
]
for expected, base_life, extra_life in cases_life:
    assert life_row(extra_life, base=base_life)["status"] == expected, (expected, extra_life)
print("owed-lifecycle AC2 ok")

for days in (1, 8, 60):
    row = life_row([{**unobservable, "capability": "ro-dsn"}],
                   now=datetime.fromisoformat(unobservable["ts"]) + timedelta(days=days))
    assert row["status"] == "blocked", row
assert life_row([{**unobservable, "capability": "operator-action"}],
                now=LIFE + timedelta(days=60))["status"] == "blocked"
assert life_row([{**unobservable, "capability": "zzz"}])["status"] == "expired"
assert life_row([{**unobservable, "capability": None}])["status"] == "expired"
attempts = [life_event("owed-routed", -9, route="repo", epoch="one"),
            life_event("owed-routed", -8, route="repo", epoch="two")]
assert life_row([unobservable, *attempts])["status"] == "expired"
assert life_row([{**unobservable, "capability": "other"}, *attempts])["status"] == "expired"
assert life_row([{**unobservable, "capability": "operator-action"},
                 *[{**e, "route": "operator-action"} for e in attempts]])["status"] == "blocked"
status_source = pathlib.Path(owed.__file__).read_text().split("        if dropped:", 1)[1].split("        out.append", 1)[0]
assert "timedelta(days=" not in status_source
print("owed-lifecycle AC3 ok")

for count in (1, 2, 3, 10):
    failures_life = [life_event("owed-failed", -22 + i, evidence=str(i)) for i in range(count)]
    assert life_row(failures_life, now=LIFE + timedelta(days=365))["status"] == "failed"
    fixed = [*failures_life, corrective]
    assert life_row(fixed)["status"] == "correcting"
    assert life_row(fixed)["corrective"] == "L-spec-fix"
    assert life_row([*fixed, waived_life])["status"] == "waived"
    # Rechecks and re-dates discard old verdicts and route attempts alike.
    for reset in (life_event("owed-ac", -3, wake_at=(LIFE + timedelta(hours=1)).isoformat()),
                  life_event("owed-routed", -3, route="recheck")):
        assert life_row([*fixed, reset])["status"] == ("waiting" if reset["type"] == "owed-ac" else "due")
        for verdict, expected in ((met_life, "met"), (failure, "failed"),
                                  (unobservable, "blocked"),
                                  ({**unobservable, "capability": "zzz"}, "expired")):
            assert life_row([*fixed, reset, {**verdict, "ts": life_event("unused", -1)["ts"]}])["status"] == expected
assert life_row([failure, {**corrective, "ts": failure["ts"]}])["status"] == "failed"
assert life_row([corrective, life_event("owed-failed", -1)])["corrective"] is None
print("owed-lifecycle AC6 ok")

row = life_row([quarantine])
assert row["status"] == "quarantined" and row["quarantine_reason"] == "repeated drift", row
recheck = life_event("owed-routed", -1, route="recheck")
assert life_row([quarantine, recheck])["status"] == "due"
assert life_row([quarantine, recheck], base=waiting_base)["status"] == "waiting"
assert life_row([quarantine, {**unobservable, "ts": recheck["ts"]}])["status"] == "blocked"
assert life_row([{**quarantine, "ts": LIFE_AC["ts"]}])["status"] == "due"
print("owed-lifecycle AC7 ok")

fields_life = {"spec", "criterion", "project", "declared_src", "line", "wake_at", "shipped_at",
               "due_at", "status", "failures", "last_failed_at", "last_unobservable_at",
               "last_unobservable_capability", "evidence", "capability", "last_route_epoch",
               "route_attempts", "ask", "quarantine_reason", "corrective", "governing_at"}
relabel = life_event("owed-routed", -7, route="relabel", capability="browser", epoch="ignore")
row = life_row([failure, unobservable, *attempts, relabel, corrective])
assert set(row) == fields_life, row
assert row["capability"] == "browser" and row["ask"] == "need checkout", row
assert row["last_unobservable_capability"] == "repo", row
assert row["last_unobservable_at"] == datetime.fromisoformat(unobservable["ts"]), row
assert row["route_attempts"] == {"repo": 2} and row["last_route_epoch"] == "two", row
assert row["governing_at"] == datetime.fromisoformat(LIFE_AC["ts"]), row
assert row["corrective"] == "L-spec-fix" and row["evidence"] == "failure", row
reset_row = life_row([failure, unobservable, *attempts, relabel, corrective, recheck])
assert reset_row["route_attempts"] == {} and reset_row["failures"] == 0, reset_row
assert reset_row["governing_at"] == datetime.fromisoformat(recheck["ts"]), reset_row
assert life_row([relabel, {**unobservable, "ts": recheck["ts"]}])["capability"] == "repo"
# A recheck earlier than a re-date cannot move the governing instant backwards.
redate = life_event("owed-ac", -0.5, wake_at=LIFE.isoformat())
assert life_row([recheck, redate])["governing_at"] == datetime.fromisoformat(redate["ts"])
for capability, expected in (("git", "repo"), ("browser", "browser"), ("deploy", "not-live")):
    routed = {**LIFE_AC, "type": "criterion-routed", "actor": "grader", "to": "owed", "capability": capability}
    assert life_row(base=[routed, LIFE_SHIP])["capability"] == expected
    # A fresh grader route replaces capability context from before that route.
    old_unobservable = {**unobservable, "ts": life_event("unused", -49)["ts"]}
    assert life_row([old_unobservable], base=[routed, LIFE_SHIP])["capability"] == expected
    assert life_row([unobservable], base=[routed, LIFE_SHIP])["capability"] == "repo"
    assert life_row([relabel], base=[routed, LIFE_SHIP])["capability"] == "browser"
print("owed-lifecycle AC8 ok")

pure_events = [*LIFE_BASE, failure, unobservable, quarantine, corrective]
first = owed.checks(pure_events, LIFE)
assert owed.checks(pure_events, LIFE) == first
with patch("builtins.open", side_effect=AssertionError("checks opened a file")):
    assert owed.checks(pure_events, LIFE) == first
imports = ast.parse(pathlib.Path(owed.__file__).read_text())
project_modules = {p.stem for p in pathlib.Path(owed.__file__).parent.glob("*.py")}
project_imports = {name.split(".")[0] for node in ast.walk(imports)
                   for name in ([n.name for n in node.names] if isinstance(node, ast.Import)
                                else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                   if name.split(".")[0] in project_modules}
assert project_imports == {"routing", "owed_routes"}, project_imports
print("owed-lifecycle AC9 ok")


# L-spec-1104 AC4: a terminal close survives verdict failures until re-declared.
U_AC = {"ts": stamp(10), "type": "owed-ac", "subject": "L-spec-91104",
        "criterion": "AC1", "wake_at": stamp(0)}
U_SHIP = {"ts": stamp(9), "type": "shipped", "subject": U_AC["subject"]}
U_CLOSE = {"ts": stamp(5), "type": "owed-unverifiable", "subject": U_AC["subject"],
           "criterion": "AC1", "reason": "cannot observe"}


def unverifiable_row(extra=(), close=U_CLOSE):
    # Authorization precedes the pure lifecycle calculation, as in fold.fold().
    assert fold.admits(close, "prod-runner") is None
    events = sorted([U_AC, U_SHIP, close, *extra], key=lambda e: e["ts"])
    return owed.checks(events, NOW)[0]


assert unverifiable_row()["status"] == "unverifiable"
print("1104-AC4 (a) admitted close: unverifiable")
for kind, values in (("owed-failed", {"evidence": "failure", "kind": "unmet"}),
                     ("owed-unobservable", {"capability": "browser", "why": "no browser"}),
                     ("owed-quarantined", {"reason": "repeated", "spec_sha256": "digest"})):
    later = {**U_CLOSE, "ts": stamp(3), "type": kind, **values}
    assert unverifiable_row([later])["status"] == "unverifiable", kind
    print("1104-AC4 (b)", kind, "leaves unverifiable")
later_met = {**U_CLOSE, "ts": stamp(2), "type": "owed-met", "evidence": "observed"}
assert unverifiable_row([later_met])["status"] == "met"
print("1104-AC4 (c) later met: met")
for declaration in ({**U_AC, "ts": stamp(1), "wake_at": stamp(-5)},
                    {**U_AC, "ts": stamp(1), "type": "owed-routed", "route": "recheck"}):
    assert unverifiable_row([declaration])["status"] in ("waiting", "due")
print("1104-AC4 (d) re-date and recheck reopen")
for when in (stamp(11), U_AC["ts"]):
    assert unverifiable_row(close={**U_CLOSE, "ts": when})["status"] in ("waiting", "due")
print("1104-AC4 (e) close before or at declaration ignored")
assert "unverifiable" in owed.STATUSES
print("1104-AC4 (f) STATUSES includes unverifiable; digest checked separately")
