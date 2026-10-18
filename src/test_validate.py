#!/usr/bin/env python3
"""One runnable check on `validate.spec_shape`. Run: python3 test_validate.py

AC1: a well-formed spec returns `[]`; each of three specs broken in exactly one
way apiece returns exactly one non-empty finding, naming which bucket failed."""
import os, pathlib, sys, tempfile

# R8.2: HOME/DOIT_ROOT isolation, set before any project module is imported —
# a direct `python3 test_validate.py` must never resolve fold.ROOT to ~/.do-it.
_TMP8027 = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-iso-"))
os.environ["HOME"] = str(_TMP8027 / "home")
os.environ["DOIT_ROOT"] = str(_TMP8027 / "root")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import validate  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


WELL_FORMED = ("# L-spec-fixture\n"
               "## Verification\n"
               "```\n"
               "true && true\n"
               "```\n"
               "## Acceptance Criteria\n"
               "AC1 [backend]: x.\n"
               "  review_path: y\n"
               "Writes: a.py\n"
               "In plain English: fixture proves the well-formed case.\n")

NO_VERIFICATION = ("# L-spec-fixture\n"
                    "## Acceptance Criteria\n"
                    "AC1 [backend]: x.\n"
                    "  review_path: y\n"
                    "Writes: a.py\n"
                    "In plain English: fixture proves the well-formed case.\n")

NO_ACCEPTANCE = ("# L-spec-fixture\n"
                  "## Verification\n"
                  "```\n"
                  "true && true\n"
                  "```\n"
                  "Writes: a.py\n"
                  "In plain English: fixture proves the well-formed case.\n")

PROSE_WRITES = ("# L-spec-fixture\n"
                "## Verification\n"
                "```\n"
                "true && true\n"
                "```\n"
                "## Acceptance Criteria\n"
                "AC1 [backend]: x.\n"
                "  review_path: y\n"
                "Writes: see the table below\n"
                "In plain English: fixture proves the well-formed case.\n")

# L-spec-0478/AC1: today's WELL_FORMED byte-for-byte, minus the plain line —
# proves the new "plain" bucket's one-finding case.
NO_PLAIN_LINE = ("# L-spec-fixture\n"
                  "## Verification\n"
                  "```\n"
                  "true && true\n"
                  "```\n"
                  "## Acceptance Criteria\n"
                  "AC1 [backend]: x.\n"
                  "  review_path: y\n"
                  "Writes: a.py\n")

# ── AC1: the well-formed fixture ──────────────────────────────────────────────
check(validate.spec_shape(WELL_FORMED) == [], "a well-formed spec returns []")

# ── AC1: the byte-identical text minus the plain line names exactly the new
# "plain" bucket ──────────────────────────────────────────────────────────────
f = validate.spec_shape(NO_PLAIN_LINE)
check(len(f) == 1 and f[0].startswith("Plain English:"),
      f"a spec missing the plain-English line names exactly that bucket: {f}")

# ── AC1(a): Verification block missing entirely ──────────────────────────────
f = validate.spec_shape(NO_VERIFICATION)
check(len(f) == 1 and f[0].startswith("Verification:"),
      f"a spec with no Verification section names exactly that bucket: {f}")

# ── AC1(b): no acceptance-criteria section at all ────────────────────────────
f = validate.spec_shape(NO_ACCEPTANCE)
check(len(f) == 1 and f[0].startswith("Acceptance criteria:"),
      f"a spec with no acceptance criteria names exactly that bucket: {f}")

# ── AC1(c): a Writes line whose value is prose ───────────────────────────────
f = validate.spec_shape(PROSE_WRITES)
check(len(f) == 1 and f[0].startswith("Writes grant:"),
      f"a prose Writes value names exactly that bucket: {f}")

# ── AC2(b)/AC3: a warn-severity PL-001 hit (base_sha: HEAD) never blocks
# spec_shape, and spec_shape_warnings surfaces exactly that finding ──────────
BASE_SHA_HEAD = ("# L-spec-fixture\n"
                  "base_sha: HEAD\n"
                  "## Verification\n"
                  "```\n"
                  "true && true\n"
                  "```\n"
                  "## Acceptance Criteria\n"
                  "AC1 [backend]: x.\n"
                  "  review_path: y\n"
                  "Writes: a.py\n"
                  "In plain English: fixture proves the well-formed case.\n")
check(validate.spec_shape(BASE_SHA_HEAD) == [],
      "a warn-severity PL-001 hit (base_sha: HEAD) never blocks spec_shape")
w = validate.spec_shape_warnings(BASE_SHA_HEAD)
check(len(w) == 1 and w[0].startswith("PL-001: "),
      f"spec_shape_warnings surfaces exactly the PL-001 finding: {w}")
check(validate.spec_shape_warnings(WELL_FORMED) == [],
      "a clean spec carries no spec_shape_warnings either")

print(f"validate: {N} checks over spec_shape's five fixtures")


# Optional evidence verdict metadata remains backwards compatible.
import json
import subprocess
with tempfile.TemporaryDirectory() as _tmp1109:
    _output1109 = pathlib.Path(_tmp1109) / "grader.json"
    _verdict1109 = {"verdicts": [{"ac": "AC1", "verdict": "met", "reason": "evidence"}],
                    "matches_intent": "yes", "card_ok": "yes", "checkers": [],
                    "could_not_run": False, "contamination": False, "declarations": []}
    for _metadata1109 in ({}, {"judgment_only": ["AC1"], "pre_existing": ["known-test"]}):
        _output1109.write_text(json.dumps({**_verdict1109, **_metadata1109}))
        _result1109 = subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("validate.py")),
                                     "grader", str(_output1109)], capture_output=True, text=True)
        check(_result1109.returncode == 0 and "VALID" in _result1109.stdout, _result1109.stderr)
print("grader-evidence AC7 optional grader schema fields and old output passed")
