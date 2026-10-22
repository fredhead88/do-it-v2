#!/usr/bin/env python3
"""One runnable check on routing.py's AC1-AC13-adjacent behaviour (L-spec-8034).
Run: python3 test_routing.py

Pure module tests only — `routing` is imported and exercised directly, never
through `fold` (AC13's purity requirement: this file never imports `fold`).
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routing  # noqa: E402

assert "fold" not in sys.modules, "test_routing.py must not import fold (purity, AC13)"

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


# ═══ AC13 — size regression: baselines recorded from `wc -l` on the rebased
# base (e11e6847a4038a9d1d7b703d0f4924b5e7f8d106), before this spec's first
# edit — dispatch.py=1630, fold.py=2435, packet.py=1576. Needs no git. ═══════
# Re-baselined 2026-10-04 by the Thinker after the operator-ruled grader-route fixes
# (grading.env, spec-local verdict holds, grader cap, owed declarers): 1697/2460/1578.
BASELINES = {"dispatch.py": 1697, "fold.py": 2465, "packet.py": 1578}
for name, baseline in BASELINES.items():
    n = sum(1 for _ in (HERE / name).open())
    check(n <= baseline, f"{name}: {n} lines exceeds its recorded baseline {baseline}")
for name in ("routing.py", "grading_env.py"):
    n = sum(1 for _ in (HERE / name).open())
    check(n < 1500, f"{name}: {n} lines, must stay under 1500")
print("AC13 (size) ok —", {k: sum(1 for _ in (HERE / k).open()) for k in list(BASELINES) + ["routing.py", "grading_env.py"]})


# ═══ latest_routed_events / routed_to — actor-scoped, latest-wins, to=none ══
EVS_A = [
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "AC1", "capability": "browser", "to": "reviewer", "spawn": "s1"},
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "AC2", "capability": "git", "to": "owed", "spawn": "s1"},
    # a later row on AC1 supersedes the first (latest-wins)
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "AC1", "capability": "browser", "to": "none", "spawn": "s2"},
    # a non-grader actor's row is ignored entirely
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "executor",
     "criterion": "AC3", "capability": "deploy", "to": "owed", "spawn": "s3"},
    # a different subject never leaks in
    {"type": "criterion-routed", "subject": "L-spec-2", "actor": "grader",
     "criterion": "AC1", "capability": "git", "to": "owed", "spawn": "s4"},
]
latest = routing.latest_routed_events(EVS_A, "L-spec-1")
check(set(latest) == {"AC1", "AC2"}, f"latest_routed_events ids: {set(latest)}")
check(latest["AC1"]["to"] == "none", "latest_routed_events: AC1's latest row wins (to=none)")
check(latest["AC2"]["to"] == "owed", "latest_routed_events: AC2 unaffected")
routed = routing.routed_to(EVS_A, "L-spec-1")
check(routed == {"AC2": "owed"}, f"routed_to drops to=none and non-grader rows: {routed}")
print("latest_routed_events/routed_to ok")


# ═══ explicit_owed_ids — spec-writer/spec-auditor actor only ═══════════════
EVS_B = [
    {"type": "owed-ac", "subject": "L-spec-1", "actor": "spec-writer", "criterion": "AC5"},
    {"type": "owed-ac", "subject": "L-spec-1", "actor": "spec-auditor", "criterion": "AC6"},
    {"type": "owed-ac", "subject": "L-spec-1", "actor": "executor", "criterion": "AC7"},
    {"type": "owed-ac", "subject": "L-spec-2", "actor": "spec-writer", "criterion": "AC5"},
]
check(routing.explicit_owed_ids(EVS_B, "L-spec-1") == {"AC5", "AC6"},
      f"explicit_owed_ids: {routing.explicit_owed_ids(EVS_B, 'L-spec-1')}")
print("explicit_owed_ids ok")


# ═══ diff_routes — new, changed, unchanged, cleared ════════════════════════
computed = {"AC1": ("browser", "reviewer"), "AC2": ("git", "owed")}
prior_empty = {}
rows = routing.diff_routes(computed, prior_empty)
check(sorted(rows) == [("AC1", "browser", "reviewer"), ("AC2", "git", "owed")],
      f"diff_routes fresh: {rows}")

prior_same = {"AC1": {"to": "reviewer"}, "AC2": {"to": "owed"}}
check(routing.diff_routes(computed, prior_same) == [], "diff_routes: unchanged spec emits nothing")

prior_changed = {"AC1": {"to": "owed"}, "AC2": {"to": "owed"}}
rows2 = routing.diff_routes(computed, prior_changed)
check(rows2 == [("AC1", "browser", "reviewer")], f"diff_routes: only the changed id: {rows2}")

# AC1 dropped by a rework -> to=none, carrying the prior capability
prior_had_ac3 = {"AC1": {"to": "reviewer"}, "AC2": {"to": "owed"},
                 "AC3": {"to": "owed", "capability": "deploy"}}
rows3 = routing.diff_routes(computed, prior_had_ac3)
check(rows3 == [("AC3", "deploy", "none")], f"diff_routes: cleared row: {rows3}")

# a prior to=none is not re-cleared
prior_already_none = {"AC3": {"to": "none", "capability": "deploy"}}
check(routing.diff_routes({}, prior_already_none) == [],
      "diff_routes: an already-none prior id is not re-emitted")
print("diff_routes ok")


# ═══ group_blocks / drop_blocks — AC-anchor grouping, whole-block drop ═════
LINES = [
    "AC1 [backend]: first.",
    "review_path: one",
    "AC2 [ui]: second.",
    "review_path: two UNIQUE-TOKEN-2",
    "AC3 [backend]: third.",
    "review_path: three",
]
groups = routing.group_blocks(LINES)
check(set(groups) == {"AC1", "AC2", "AC3"}, f"group_blocks ids: {set(groups)}")
check(groups["AC2"] == ["AC2 [ui]: second.", "review_path: two UNIQUE-TOKEN-2"], groups["AC2"])

dropped = routing.drop_blocks(LINES, {"AC2"})
check("UNIQUE-TOKEN-2" not in "\n".join(dropped), "drop_blocks: AC2's continuation line is gone")
check(dropped == LINES[:2] + LINES[4:], f"drop_blocks: AC1/AC3 survive intact: {dropped}")
check(routing.drop_blocks(LINES, set()) == LINES, "drop_blocks: empty drop_ids reproduces input unchanged")
print("group_blocks/drop_blocks ok")


# ═══ reviewer_cleared / reviewer_gate_clear ═════════════════════════════════
EVS_C = [
    {"type": "criterion-cleared", "subject": "L-spec-1", "actor": "reviewer", "criterion": "AC1"},
    {"type": "criterion-cleared", "subject": "L-spec-1", "actor": "builder", "criterion": "AC2"},
    {"type": "criterion-cleared", "subject": "L-spec-1", "actor": "reviewer", "criterion": "AC3"},
    {"type": "rejected-criterion", "subject": "L-spec-1", "actor": "reviewer", "criterion": "AC3"},
]
check(routing.reviewer_cleared(EVS_C, "L-spec-1", "AC1") is True, "reviewer_cleared: reviewer clear counts")
check(routing.reviewer_cleared(EVS_C, "L-spec-1", "AC2") is False, "reviewer_cleared: builder clear never counts")
check(routing.reviewer_cleared(EVS_C, "L-spec-1", "AC3") is False, "reviewer_cleared: a later reject reopens it")
check(routing.reviewer_cleared(EVS_C, "L-spec-1", "AC9") is False, "reviewer_cleared: no row at all -> False")
check(routing.reviewer_gate_clear(EVS_C, "L-spec-1", set()) is True, "reviewer_gate_clear: empty set is vacuous")
check(routing.reviewer_gate_clear(EVS_C, "L-spec-1", {"AC1"}) is True, "reviewer_gate_clear: all cleared")
check(routing.reviewer_gate_clear(EVS_C, "L-spec-1", {"AC1", "AC2"}) is False,
      "reviewer_gate_clear: one uncleared blocks the whole gate")
print("reviewer_cleared/reviewer_gate_clear ok")


# ═══ owed_rows_for_routed ════════════════════════════════════════════════
EVS_D = [
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "AC1", "capability": "git", "to": "owed", "ts": "2026-09-01T00:00:00+00:00"},
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "AC2", "capability": "browser", "to": "reviewer", "ts": "2026-09-01T00:00:00+00:00"},
    {"type": "criterion-routed", "subject": "L-spec-1", "actor": "grader",
     "criterion": "verify", "capability": "git", "to": "owed", "ts": "2026-09-02T00:00:00+00:00"},
]
rows = routing.owed_rows_for_routed(EVS_D, "L-spec-1")
check({r["criterion"] for r in rows} == {"AC1", "verify"}, f"owed_rows_for_routed: {rows}")
check(all(r["ts"] for r in rows), "owed_rows_for_routed: carries the governing event's own ts")
print("owed_rows_for_routed ok")

# W44: exact real decision shapes, authorization, revocation, and legacy holds.
S44 = "L-spec-1387"
def decision44(**fields):
    return dict(type="decision", subject=S44, actor="thinker", **fields)

explicit44 = decision44(criterion="AC9", why="Pre-existing baseline failure.", evidence="evidence/base.json")
prose44 = decision44(why="AC01, AC04, AC09 and AC11 met on build a8d0e22f4c. "
                        "AC04: Thinker rerun gives 3 passed and 0 skipped.", evidence="evidence/all.json")
rulings44 = [explicit44, prose44]
check(set(routing.ruled_criteria(rulings44, S44)) == {"AC9", "AC01", "AC04", "AC09", "AC11"},
      "W44: explicit and why-only multi-AC rulings preserve zero-padded ids")
for actor in ("builder", "grader", "executor"):
    check(not routing.ruled_criteria([dict(prose44, actor=actor)], S44), "W44: actor gate")
check(not routing.ruled_criteria(rulings44, "L-spec-other"), "W44: spec isolation")
check(not routing.ruled_criteria([decision44(why="Inspect AC1. AC10 is met.")], S44).get("AC1"),
      "W44: unrelated mention and AC1/AC10 collision are not rulings")
check(set(routing.ruled_criteria([decision44(criteria=["AC1", "AC2"], why="Operator evidence accepted.")], S44))
      == {"AC1", "AC2"}, "W44: structured criteria list")
check(set(routing.ruled_criteria([decision44(criteria="AC1, AC2", why="Evidence accepted.")], S44))
      == {"AC1", "AC2"}, "W44: structured criteria text")
check("AC9" not in routing.ruled_criteria(rulings44 + [decision44(criterion="AC9", why="AC9 is not met")], S44),
      "W44: later negative decision revokes a ruling")
check(not routing.ruled_criteria(rulings44 + [dict(type="spec-written", subject=S44)], S44),
      "W44: a replacement spec invalidates old rulings")
hold44 = dict(type="capability-hold", subject="capability:unknown:" + S44, spec=S44,
              source="verdict", capability="unknown", spawn="L-grader-2594")
verdict44 = dict(type="verdict", subject=S44, spawn="L-grader-2594",
                 cannot_assess=["AC01", "AC04", "AC09", "AC11"],
                 missing_capability=[dict(ac=cid, capability="unknown") for cid in ("AC01", "AC04", "AC11")])
check(routing.hold_ruled(rulings44 + [verdict44], hold44, S44), "W44: legacy hold joins its own verdict")
check(not routing.hold_ruled([explicit44, verdict44], hold44, S44), "W44: unruled hold stays")
check(not routing.hold_ruled(rulings44 + [dict(verdict44, spawn="unrelated")], hold44, S44),
      "W44: never borrow an unrelated verdict")
check(not routing.hold_ruled(rulings44, hold44, S44), "W44: absent ids remain unknown")
check(not routing.hold_ruled(rulings44 + [verdict44], dict(hold44, source="preflight"), S44),
      "W44: preflight hold is not a criterion ruling")
check(routing.hold_ruled(rulings44, dict(hold44, criteria=["AC01", "AC04"]), S44),
      "W44: explicit hold criteria need no verdict lookup")
check(not routing.hold_ruled(rulings44, dict(hold44, criteria=["AC01", "AC99"]), S44),
      "W44: one ruled criterion cannot clear a mixed hold")
print("W44 criterion rulings and hold association ok")

shutil_needed = False  # no tempdir cleanup needed — this file writes nothing to disk
print(f"ALL OK ({N} checks)")
