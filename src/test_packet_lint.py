#!/usr/bin/env python3
"""One runnable check on packet_lint. Run: python3 test_packet_lint.py

Every fixture below is a literal in-test string, never a `content/*.md` disk
read (finding 4) — AC1, AC4-AC10, AC12, AC14, AC15. AC2/AC3 (the `validate`
wiring) live in test_validate.py; AC11 (the merge_gate regex fix itself)
lives in test_merge_gate.py; AC13 (the `packet.py` builder-packet wiring)
lives in test_packet.py."""
import pathlib, re, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import merge_gate as mg  # noqa: E402
import packet_lint  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def spec_text(*, base="abcdef1234567890", verify="/usr/bin/python3 t.py && /usr/bin/python3 -m pytest",
              accept="AC1 [backend]: does the thing.\n  review_path: run `t.py` — worked if exit 0.",
              heading_extra="", writes="src/a.py"):
    return ("# L-spec-0001\n"
            f"base_sha: {base}\n"
            f"{heading_extra}"
            "## Verification\n```\n" + verify + "\n```\n"
            "## Acceptance\n" + accept + "\n"
            f"Writes: {writes}\n")


def ids(text, applies_to="spec", checklist=packet_lint.CHECKLIST):
    return {f["id"] for f in packet_lint.run(text, checklist, applies_to)}


CLEAN = spec_text()

# ── AC1: a spec clean of all eight spec-scoped PL conditions returns [] ───────
check(packet_lint.run(CLEAN, packet_lint.CHECKLIST, "spec") == [],
      f"a clean spec fires nothing: {packet_lint.run(CLEAN, packet_lint.CHECKLIST, 'spec')}")

# ── AC4 PL-001: base_sha literally HEAD fires; a real hex sha does not ───────
HEAD_FIXTURE = spec_text(base="HEAD")
check("PL-001" in ids(HEAD_FIXTURE), "a base_sha of literal HEAD fires PL-001")
check("PL-001" not in ids(CLEAN), "a real hex base_sha never fires PL-001")

# ── AC5 PL-002: last criterion truncated (no review_path, no closing .!?) ───
TRUNCATED = spec_text(accept="AC1 [backend]: does the thing")
check("PL-002" in ids(TRUNCATED), "a truncated last criterion fires PL-002")
check("PL-002" not in ids(CLEAN), "review_path + a closing period never fires PL-002")

# ── AC6 PL-003: a relative `cd /opt/albert-scott` fires; absolute paths do not
CD_PREFIX = spec_text(verify="cd /opt/albert-scott && /usr/bin/python3 -m pytest")
check("PL-003" in ids(CD_PREFIX), "cd /opt/albert-scott in the Verification block fires PL-003")
ABS_ONLY = spec_text(verify="/usr/bin/python3 -m pytest && echo OK")
check("PL-003" not in ids(ABS_ONLY), "absolute interpreter paths never fire PL-003")

# ── AC7 PL-004: bare npx fires; /usr/bin/npx does not ────────────────────────
BARE_NPX = spec_text(verify="npx test && /usr/bin/python3 -m pytest")
check("PL-004" in ids(BARE_NPX), "bare npx fires PL-004")
ABS_NPX = spec_text(verify="/usr/bin/npx test && /usr/bin/python3 -m pytest")
check("PL-004" not in ids(ABS_NPX), "/usr/bin/npx never fires PL-004")

# ── AC8 PL-005: a heading naming a different spec id fires; own id never does
SIBLING = spec_text(heading_extra="## L-spec-9999 · other unit\n")
check("PL-005" in ids(SIBLING), "a ## heading naming a different spec id fires PL-005")
check("PL-005" not in ids(CLEAN), "a spec whose headings only name its own id never fires PL-005")

# ── AC9 PL-006: base-drift phrase fires unquoted, is silent quoted, and this
# spec's own AC9 review_path line does not trip its own lint ─────────────────
UNQUOTED = "review_path: diff against main to confirm"
check(packet_lint.check_review_path_base_comparison(UNQUOTED) is not None,
      "an unquoted base-drift phrase on a review_path: line fires PL-006")
QUOTED = 'review_path: run the fixture whose value reads "diff against main to confirm"'
check(packet_lint.check_review_path_base_comparison(QUOTED) is None,
      "the same phrase, quoted inside the value, does not fire PL-006")
# The exact review_path: line this spec's own AC9 carries (content/L-spec-0273.md) —
# a literal copy, proving the check does not trip on its own description of itself.
AC9_SELF = ("review_path: run `/usr/bin/python3 src/test_packet_lint.py` — worked if (a) a")
check(packet_lint.check_review_path_base_comparison(AC9_SELF) is None,
      "this spec's own AC9 review_path line does not trip PL-006")

# ── AC10 PL-007: an identifier asserted exit 0 in one AC and exit 1 in another
# fires; the same identifier agreeing everywhere does not ───────────────────
CONTRA = "AC1 [backend]: `case_a` exits 0 here.\nAC2 [backend]: `case_a` exits 1 here.\n"
check("PL-007" in ids(spec_text(accept=CONTRA)), "case_a asserted exit 0 and exit 1 fires PL-007")
AGREE = "AC1 [backend]: `case_a` exits 0 here.\nAC2 [backend]: `case_a` exits 0 here.\n"
check("PL-007" not in ids(spec_text(accept=AGREE)), "case_a agreeing on exit 0 everywhere never fires PL-007")

# ── AC12 PL-008(b): the standing regression guard around merge_gate's fix ───
BRACKET_WRITES = spec_text(writes="dashboard/src/app/(dashboard)/dashboard/clients/[name]/profit-v2/reports/page.tsx")
real_path_re = mg.PATH_TOKEN_RE
mg.PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@+*?\[\]/-]+")  # the pre-fix pattern, no parens
try:
    check("PL-008" in ids(BRACKET_WRITES), "the pre-fix regex rejects the bracketed path, firing PL-008")
finally:
    mg.PATH_TOKEN_RE = real_path_re
check("PL-008" not in ids(BRACKET_WRITES), "the real fixed regex accepts the bracketed path — no PL-008")

# ── AC14: a [[promotion]] row makes that id's effective severity block; every
# other firing entry stays warn ──────────────────────────────────────────────
FIX_DIR = pathlib.Path(tempfile.mkdtemp())
FIX_TOML = FIX_DIR / "checklist-with-promotion.toml"
FIX_TOML.write_text(packet_lint.CHECKLIST.read_text() +
                     '\n[[promotion]]\nid = "PL-001"\npromoted_at = "2026-09-24"\nreason = "test"\n')
MULTI_FIRE = spec_text(base="HEAD", verify="cd /opt/albert-scott && /usr/bin/python3 -m pytest")
findings = packet_lint.run(MULTI_FIRE, FIX_TOML, "spec")
by_id = {f["id"]: f["severity"] for f in findings}
check(by_id.get("PL-001") == "block", f"a promoted PL-001 is effectively block: {by_id}")
check(by_id.get("PL-003") == "warn", f"an unpromoted PL-003 stays warn: {by_id}")

# ── AC15: the checklist parses, 9 unique ids, every check resolves, both new
# files stay under the 400-line cap, and install.sh names test_packet_lint.py
data = packet_lint.load(packet_lint.CHECKLIST)
eids = [e["id"] for e in data["entry"]]
check(len(eids) == 9 and len(set(eids)) == 9 and set(eids) == {f"PL-{n:03d}" for n in range(1, 10)},
      f"nine unique ids PL-001..PL-009: {eids}")
check(all(hasattr(packet_lint, e["check"]) for e in data["entry"]),
      "every entry's check function resolves on packet_lint")
HERE = pathlib.Path(__file__).resolve().parent
for f in (HERE / "packet_lint.py", HERE / "test_packet_lint.py"):
    n = len(f.read_text().splitlines())
    check(n <= 400, f"{f.name} is {n} lines, over the 400-line cap")
install = (HERE.parent / "install.sh").read_text()
check("test_packet_lint.py" in install, "install.sh's test line names test_packet_lint.py")

print(f"packet_lint: {N} checks pass")
