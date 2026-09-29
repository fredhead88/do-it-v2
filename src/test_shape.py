#!/usr/bin/env python3
"""Seven runnable checks on `shape.py`. Run: python3 test_shape.py

AC1-AC7, one block apiece, each printing "AC<n> ok" to stdout on success
(L-spec-0385 Verification). Hermetic (SD13): no network, no ledger, no
droplet — only a local `tempfile` scratch dir and, for AC6, a real
subprocess of the repo's own `./doit shape`."""
import contextlib, io, pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import shape  # noqa: E402
import validate  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def run_cli(argv):
    """`shape.main(argv)` in-process, its stdout/stderr captured, and the
    `SystemExit` code it always raises (AC4's own framing)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            shape.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ── fixtures matching test_validate.py's shapes (Assumptions — new copies,
# never an import: that module runs its own assertions at import time) ───────
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

NO_VERIFICATION = ("# L-spec-fixture\n"
                    "## Acceptance Criteria\n"
                    "AC1 [backend]: x.\n"
                    "  review_path: y\n"
                    "Writes: a.py\n"
                    "In plain English: fixture proves the well-formed case.\n")

# ══ AC1 ══════════════════════════════════════════════════════════════════════
for fx in (WELL_FORMED, BASE_SHA_HEAD, NO_VERIFICATION):
    check(shape.check(fx) == {"block": validate.spec_shape(fx),
                               "warn": validate.spec_shape_warnings(fx)},
          f"shape.check must mirror validate.spec_shape/spec_shape_warnings exactly: {fx!r}")

check(shape.check(WELL_FORMED) == {"block": [], "warn": []},
      "the well-formed fixture gives {block: [], warn: []}")

r = shape.check(BASE_SHA_HEAD)
check(r["block"] == [] and len(r["warn"]) == 1 and r["warn"][0].startswith("PL-001: "),
      f"BASE_SHA_HEAD gives {{block: [], warn: [w]}} with w starting 'PL-001: ': {r}")

r = shape.check(NO_VERIFICATION)
check(len(r["block"]) == 1 and r["block"][0].startswith("Verification:") and r["warn"] == [],
      f"the missing-Verification fixture gives {{block: [b], warn: []}} with b starting 'Verification:': {r}")

print("AC1 ok")

# ══ AC2 ══════════════════════════════════════════════════════════════════════
# Fixture A — block form, five Writes lines, one path per line.
FIX_A = ("# L-spec-fixture\n"
         "## Verification\n"
         "```\n"
         "true && true\n"
         "```\n"
         "## Acceptance Criteria\n"
         "AC1 [backend]: x.\n"
         "  review_path: y\n"
         "**Writes:**\n"
         "- src/a.py (new)\n"
         "- src/b.py — a helper\n"
         "- src/c.py # temp\n"
         "- app/(dashboard)/[name]/x.tsx\n"
         "- src/d.py src/e.py\n"
         "\n"
         "In plain English: fixture proves the block-form Writes repair.\n")

new_a, repairs_a = shape.mechanical_fix(FIX_A)
lines_in_a, lines_out_a = FIX_A.split("\n"), new_a.split("\n")
check(lines_out_a[9] == "- src/a.py", f"line 1 stripped to the path token only: {lines_out_a[9]!r}")
check(lines_out_a[10] == "- src/b.py", f"line 2 stripped to the path token only: {lines_out_a[10]!r}")
check(lines_out_a[11] == "- src/c.py", f"line 3 stripped to the path token only: {lines_out_a[11]!r}")
check(lines_out_a[12] == lines_in_a[12],
      "the bracket/paren path line is byte-identical — its ( / [ sit inside the path token itself")
check(lines_out_a[13] == lines_in_a[13],
      "the two-valid-path line is byte-identical — neither token is ever stripped")
check(repairs_a == ["writes-annotation", "writes-annotation", "writes-annotation"],
      f"one repair entry per line actually changed, in order: {repairs_a}")
check(lines_out_a[:9] == lines_in_a[:9],
      "Verification and Acceptance-Criteria sections are byte-identical between input and output")

# Fixture B — inline form.
FIX_B = ("# L-spec-fixture\n"
         "## Verification\n"
         "```\n"
         "true && true\n"
         "```\n"
         "## Acceptance Criteria\n"
         "AC1 [backend]: x.\n"
         "  review_path: y\n"
         "**Writes:** src/f.py src/g.py (new)\n")

new_b, repairs_b = shape.mechanical_fix(FIX_B)
lines_in_b, lines_out_b = FIX_B.split("\n"), new_b.split("\n")
check(lines_out_b[8] == "**Writes:** src/f.py src/g.py",
      f"the inline label line is stripped, label itself unchanged: {lines_out_b[8]!r}")
check(repairs_b == ["writes-annotation"], f"exactly one repair: {repairs_b}")
check(lines_out_b[:8] == lines_in_b[:8],
      "Verification and Acceptance-Criteria sections are byte-identical between input and output")

print("AC2 ok")

# ══ AC3 ══════════════════════════════════════════════════════════════════════
AC3_FIXTURE = ("- AC1 [backend]: x.\n"
               "1. **AC2** [ui]: z.\n"
               "AC3 [backend]: already clean.\n"
               "**AC4 [ui] — already valid, do not touch.**\n")
new_3, repairs_3 = shape.mechanical_fix(AC3_FIXTURE)
lines_3 = new_3.split("\n")
check(lines_3[0] == "AC1 [backend]: x.", f"dash-bulleted AC1 un-bulleted: {lines_3[0]!r}")
check(lines_3[1] == "AC2 [ui]: z.", f"numbered, tight-bold AC2 un-bulleted and un-bolded: {lines_3[1]!r}")
check(lines_3[2] == "AC3 [backend]: already clean.", "unbulleted AC3 is untouched")
check(lines_3[3] == "**AC4 [ui] — already valid, do not touch.**",
      "whole-line-bold AC4 is left completely untouched")
check(repairs_3 == ["ac-bullet", "ac-bullet"], f"AC3/AC4 contribute no entry: {repairs_3}")

VERIF_ONLY_DEFECT = ("# L-spec-fixture\n"
                      "## Verification\n"
                      "```\n"
                      "true ; true\n"
                      "```\n"
                      "## Acceptance Criteria\n"
                      "AC1 [backend]: x.\n"
                      "  review_path: y\n"
                      "Writes: a.py\n")
new_v, repairs_v = shape.mechanical_fix(VERIF_ONLY_DEFECT)
check(new_v == VERIF_ONLY_DEFECT, "a Verification-only defect is never auto-repaired — text unchanged")
check(repairs_v == [], f"and no repair is claimed: {repairs_v}")

print("AC3 ok")

# ══ AC4 ══════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as td:
    p_ok = pathlib.Path(td) / "ok.md"
    p_ok.write_text(WELL_FORMED)
    p_warn = pathlib.Path(td) / "warn.md"
    p_warn.write_text(BASE_SHA_HEAD)
    p_block = pathlib.Path(td) / "block.md"
    p_block.write_text(NO_VERIFICATION)
    missing = str(pathlib.Path(td) / "missing.md")

    code, out, err = run_cli([str(p_ok)])
    check(code == 0 and "BLOCK:" not in out, f"(a) well-formed: exit {code}, out {out!r}")

    code, out, err = run_cli([str(p_warn)])
    lines = [l for l in out.splitlines() if l]
    check(code == 0 and len(lines) == 1 and lines[0].startswith("WARN: PL-001:"),
          f"(b) warn-only: exit {code}, lines {lines}")

    code, out, err = run_cli([str(p_block)])
    lines = [l for l in out.splitlines() if l]
    check(code == 1 and len(lines) == 1 and lines[0].startswith("BLOCK: Verification:"),
          f"(c) block: exit {code}, lines {lines}")

    code, out, err = run_cli([missing])
    check(code == 2 and out == "" and missing in err,
          f"(d) missing path: exit {code}, out {out!r}, err {err!r}")

print("AC4 ok")

# ══ AC5 ══════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as td:
    p1 = pathlib.Path(td) / "spec1.md"
    p1.write_text(FIX_A)
    code, out, err = run_cli([str(p1)])
    lines = [l for l in out.splitlines() if l]
    block_lines = [l for l in lines if l.startswith("BLOCK:")]
    # PL-008(b) also fires a WARN here — the offending token '(new)' itself
    # contains a paren, which is a coincidental side effect of a real,
    # unrelated regression guard in packet_lint.py, not this unit's concern
    # (Constraints — validate.py's rules are read-only). AC5 asks for exactly
    # one BLOCK line, not an empty WARN list.
    check(code == 1 and len(block_lines) == 1 and block_lines[0].startswith("BLOCK: Writes grant:")
          and "'(new)'" in block_lines[0],
          f"no --fix: exit {code}, lines {lines}")
    check(p1.read_text() == FIX_A, "no --fix must never mutate the file")

    p2 = pathlib.Path(td) / "spec2.md"
    p2.write_text(FIX_A)
    code2, out2, _ = run_cli([str(p2), "--fix"])
    check(code2 == 0 and "BLOCK:" not in out2, f"--fix run: exit {code2}, out {out2!r}")
    expected_fixed, _ = shape.mechanical_fix(FIX_A)
    check(p2.read_text() == expected_fixed, "the file is rewritten to the repaired five-line form")

    before = p2.read_text()
    code3, out3, _ = run_cli([str(p2), "--fix"])
    check(code3 == 0, f"second --fix run: exit {code3}")
    check(p2.read_text() == before, "a second --fix run on an already-clean file changes no bytes")

print("AC5 ok")

# ══ AC6 ══════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as td:
    ok_path = pathlib.Path(td) / "ok.md"
    ok_path.write_text(WELL_FORMED)
    p = subprocess.run(["./doit", "shape", str(ok_path)], cwd=str(REPO_ROOT),
                        capture_output=True, text=True)
    check(p.returncode == 0 and "BLOCK:" not in p.stdout,
          f"(a) real subprocess, well-formed: rc {p.returncode}, stdout {p.stdout!r}")

    p2 = subprocess.run(["./doit", "shape", "/no/such/path"], cwd=str(REPO_ROOT),
                         capture_output=True, text=True)
    check(p2.returncode == 2 and "/no/such/path" in p2.stderr,
          f"(b) real subprocess, missing path: rc {p2.returncode}, stderr {p2.stderr!r}")

print("AC6 ok")

# ══ AC7 ══════════════════════════════════════════════════════════════════════
AC7_FIXTURE = ("# L-spec-fixture\n"
               "## Verification\n"
               "```\n"
               "true && true\n"
               "```\n"
               "## Acceptance Criteria\n"
               "- AC1 [backend]: x.\n"
               "  review_path: y\n"
               "Writes: a.py\n"
               "In plain English: fixture proves the dash-bulleted AC line case.\n")
check(shape.check(AC7_FIXTURE) == {"block": [], "warn": []},
      "a dash-bulleted AC line under a real ## Acceptance heading is not a block finding")

with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "ac7.md"
    p.write_text(AC7_FIXTURE)
    before = p.read_text()
    code, out, _ = run_cli([str(p)])
    check(code == 0 and "BLOCK:" not in out, f"no --fix: exit {code}, out {out!r}")
    check(p.read_text() == before, "no --fix must never mutate the file")

    p2 = pathlib.Path(td) / "ac7b.md"
    p2.write_text(AC7_FIXTURE)
    code2, out2, _ = run_cli([str(p2), "--fix"])
    check(code2 == 0 and "BLOCK:" not in out2, f"--fix: exit {code2}, out {out2!r}")
    fixed_text, fixed_repairs = shape.mechanical_fix(AC7_FIXTURE)
    check(p2.read_text() == fixed_text,
          "the file on disk is rewritten even though the pre-fix block list was already empty")
    check(fixed_repairs == ["ac-bullet"], f"the bullet was in fact repaired: {fixed_repairs}")

print("AC7 ok")

print(f"shape: {N} checks over shape.py's seven acceptance criteria")
