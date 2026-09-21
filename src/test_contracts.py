#!/usr/bin/env python3
"""L-spec-0051 — the contracts say what the charter decided.

A marker checker, not a linter. Every rule L-charter-0021 puts into a pane
contract or into the design record is a literal string that must be PRESENT in
the file that must carry it, or a literal string that must be ABSENT because the
old behaviour it describes is retired. One ok() call per acceptance criterion,
AC1..AC19, AC21; AC20 is this file existing and running green.

Marker literals live here as Python constants and are compared with `in` against
the file's text — never shell-quoted, never round-tripped through grep — so a
backtick is a backtick and a `**` is a `**`. A marker that spans a line wrap in
the prose would not match, which is deliberate: the contract has to read as one
phrase, not as two halves a reader reassembles.

Run:  cd src && python3 test_contracts.py
  or: python3 src/test_contracts.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXECUTOR = "agents/executor.md"
PLANNER = "agents/planner.md"
THINKER = "agents/thinker.md"
PLAN_AUDITOR = "agents/plan-auditor.md"
DESIGN = "design/system-design-v2.md"

PANES = (EXECUTOR, PLANNER, THINKER)

_cache: dict[str, str] = {}
_failures: list[str] = []
_checks = 0


def text(rel: str) -> str:
    """The file's contents, read once. A missing file is a failure, not a crash."""
    if rel not in _cache:
        path = ROOT / rel
        if not path.is_file():
            _failures.append(f"{rel}: file does not exist (looked under {ROOT})")
            _cache[rel] = ""
        else:
            _cache[rel] = path.read_text(encoding="utf-8")
    return _cache[rel]


def ok(ac: str, rule: str, present=(), absent=()) -> None:
    """One acceptance criterion: markers that must appear, markers that must not.

    `present` and `absent` are (file, marker) pairs. Every pair is checked and
    every failure is reported — the first missing marker does not hide the rest.
    """
    global _checks
    _checks += 1
    bad = []
    for rel, marker in present:
        if marker not in text(rel):
            bad.append(f"MISSING from {rel}: {marker!r}")
    for rel, marker in absent:
        if marker in text(rel):
            bad.append(f"STILL PRESENT in {rel}: {marker!r}")
    if bad:
        for line in bad:
            _failures.append(f"{ac} ({rule}) — {line}")


# --- AC1 .. AC9 — agents/executor.md -----------------------------------------

ok(
    "AC1",
    "R13 · the Executor pane can send a message at all",
    present=[(EXECUTOR, "SendMessage")],
)

ok(
    "AC2",
    "R1, R2 · a supervised pane on a five-minute interval, not a tick",
    present=[
        (EXECUTOR, "supervised pane"),
        (EXECUTOR, "five minutes"),
        (EXECUTOR, "quiet point"),
        (EXECUTOR, "one-line handover"),
        (EXECUTOR, "restarted fresh by the launcher"),
    ],
    absent=[(EXECUTOR, "Spawned by `doit tick` only")],
)

ok(
    "AC3",
    "R2 · the ledger is written before the pane ends, so a restart is free",
    present=[(EXECUTOR, "before it ends"), (EXECUTOR, "loses nothing")],
)

ok(
    "AC4",
    "R10 · the spec reaching the Executor is already audited",
    present=[(EXECUTOR, "already audited")],
    absent=[(EXECUTOR, "no audit → dispatch")],
)

ok(
    "AC5",
    "R7 · the footprint-collision wait is gone",
    absent=[(EXECUTOR, "-wait owner=executor")],
)

ok(
    "AC6",
    "R8 · a merge conflict is a builder re-dispatch, never a hand-merge",
    present=[
        (EXECUTOR, "packet_builder_rework"),
        (EXECUTOR, "conflict_attempts"),
        (EXECUTOR, "second conflict"),
    ],
)

ok(
    "AC7",
    "R5 · a carried spec's review tier is read off its event, not derived",
    present=[(EXECUTOR, "spec-carried"), (EXECUTOR, "review_tier")],
)

ok(
    "AC8",
    "R15 · the Executor never edits a repository and never runs the suite",
    present=[
        (EXECUTOR, "never edits a file under"),
        (EXECUTOR, "never runs the test suite"),
        (EXECUTOR, "executor_deny_list"),
    ],
)

ok(
    "AC9",
    "R13 · message discipline, in the Executor's binding rules",
    present=[
        (EXECUTOR, "names the ledger event it concerns"),
        (EXECUTOR, "message-sent"),
    ],
)


# --- AC10 .. AC13 — the three pane contracts ---------------------------------

ok(
    "AC10",
    "R3 · every escalation-blocking carries default/deadline/revert, or names "
    "the irreversible act",
    present=[(f, m) for f in PANES for m in ("deadline=", "irreversible")],
)

ok(
    "AC11",
    "R9 · the Planner writes one document and runs one fable audit over it",
    present=[
        (PLANNER, "one fable audit"),
        (PLANNER, "plan-<charter>.md"),
    ],
    absent=[(PLANNER, "② Cut-audit"), (PLANNER, "④ Plan-audit")],
)

ok(
    "AC12",
    "R10 · the Planner dispatches each spec's own audit before l1-complete",
    present=[
        (PLANNER, "dispatch `spec-auditor`"),
        (PLANNER, "l1-complete"),
    ],
)

ok(
    "AC13",
    "R13 · message discipline, on the Planner and the Thinker too",
    present=[
        (PLANNER, "names the ledger event it concerns"),
        (THINKER, "names the ledger event it concerns"),
    ],
)


# --- AC14, AC15 — agents/plan-auditor.md -------------------------------------

_bad_cut_at_plan = any(
    "bad_cut" in line and "stage: plan" in line
    for line in text(PLAN_AUDITOR).splitlines()
)

ok(
    "AC14",
    "R9 · two stages, and bad_cut re-homed to the stage that still exists",
    present=[(PLAN_AUDITOR, "two stages")],
    absent=[
        (PLAN_AUDITOR, "three stages (cut, plan, charter-set)"),
        (PLAN_AUDITOR, "stage: cut"),
    ],
)
if not _bad_cut_at_plan:
    _failures.append(
        f"AC14 (R9 · bad_cut re-homed) — MISSING from {PLAN_AUDITOR}: "
        "no single line names both 'bad_cut' and 'stage: plan'"
    )

ok(
    "AC15",
    "R7 · same-wave overlap is advisory, never a bad_cut trigger on its own",
    present=[(PLAN_AUDITOR, "overlap is advisory")],
)


# --- AC16 .. AC19 — design/system-design-v2.md -------------------------------

ok(
    "AC16",
    "R9 · the design record describes one document and one fable audit",
    present=[(DESIGN, "one fable audit")],
    absent=[
        (DESIGN, "audit #2"),
        # all three occurrences must be gone: the §3.6 heading, the §3.6
        # blindness table row, and the §9.x D32 instrument row.
        (DESIGN, "the two fable audits"),
        (DESIGN, "CUT-AUDIT"),
    ],
)

ok(
    "AC17",
    "R7 · §3.7 states advisory overlap instead of zero overlap",
    present=[(DESIGN, "overlap is advisory")],
    absent=[(DESIGN, "Within a wave: **zero**")],
)

ok(
    "AC18",
    "R1, R2 · §3.9 and the §12.6 ops row describe a supervised pane",
    present=[(DESIGN, "supervised pane")],
    absent=[
        (DESIGN, "the Executor runs as a tick, not a pane"),
        (DESIGN, "restart-on-exit and the D94 restart are gone"),
    ],
)

ok(
    "AC19",
    "R9 · §4.6·6's plan-auditor row matches the two-stage contract",
    present=[(DESIGN, "two stages")],
    absent=[(DESIGN, "per cut, per plan (2×)")],
)


# --- AC21 — L-spec-0125 R11: row 52's not-owed branch, pinned to one literal ---

ok(
    "AC21",
    "L-spec-0125 R11 · charter-reviewer dispatched only where doit review-owed says owed",
    present=[(EXECUTOR, "not-owed → no charter-reviewer dispatch")],
)


# --- report -------------------------------------------------------------------

if _failures:
    print(f"contracts: {len(_failures)} failure(s) across {_checks} checks\n")
    for line in _failures:
        print(f"  FAIL {line}")
    sys.exit(1)

print(f"contracts: {_checks} checks pass")
