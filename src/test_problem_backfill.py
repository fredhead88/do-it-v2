#!/usr/bin/env python3
"""Hermetic, subprocess-isolated tests for scripts/problem_backfill.py
(L-spec-0278). `fold.ROOT`/`EVENTS`/`PROJECT` bind once at `import fold` time
from `os.environ` (`src/fold.py:19-28`), so every fixture case here runs the
script as its OWN subprocess with a fresh, minimal env carrying only `PATH`
and `DOIT_ROOT` (never `DOIT_PROJECT`) -- never by importing `fold` or the
script in-process and reassigning `DOIT_ROOT` afterward. That is what makes
these fixtures actually hermetic against the real `~/.do-it` ledger.

Two checks (AC3's `--help` case, AC7) are explicitly *about* the real, live
ledger per the spec itself, and run against it read-only, via their own
subprocess with `DOIT_ROOT` pointed at the real default location.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

PYTHON = "/opt/albert-scott/.venv/bin/python3"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "problem_backfill.py"
SLUG_RE = re.compile(r"^[a-z0-9-]{3,60}$")
REAL_DOIT_ROOT = pathlib.Path(os.environ.get("DOIT_ROOT") or (pathlib.Path.home() / ".do-it"))

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"ok - {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL - {name} {detail}")


def run(doit_root, out=None, extra_args=None):
    """Run the script as its own subprocess against `doit_root`, with a
    minimal, freshly-built env: only PATH and DOIT_ROOT. DOIT_PROJECT is
    guaranteed absent because the env dict is built from scratch."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DOIT_ROOT": str(doit_root)}
    args = [PYTHON, str(SCRIPT)]
    if out is not None:
        args += ["--out", str(out)]
    if extra_args:
        args += extra_args
    return subprocess.run(args, capture_output=True, text=True, env=env)


def write_ledger(root, events, fname="L-fixture-0001.jsonl"):
    """One fixture ledger file, one event per line, in creation order."""
    ev_dir = pathlib.Path(root) / "events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    path = ev_dir / fname
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


def parse_groups(text):
    """Split proposal markdown into (heading_slug, json_obj) pairs. Raises
    AssertionError on any shape violation (AC9's own parse-success check)."""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            heading_slug = line[3:]
            assert lines[i + 1] == "```json", f"expected fenced json block after heading {heading_slug!r}"
            j = i + 2
            body_lines = []
            while lines[j] != "```":
                body_lines.append(lines[j])
                j += 1
            obj = json.loads("\n".join(body_lines))
            blocks.append((heading_slug, obj))
            i = j + 1
        else:
            i += 1
    return blocks


def sha256_tree(events_dir):
    out = {}
    for f in sorted(pathlib.Path(events_dir).glob("*.jsonl")):
        out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


LONG_STATEMENT = (
    "This is a deliberately long fixture statement for AC3, well over six "
    "hundred characters, containing multiple lines and markdown-significant "
    "characters such as # headings, `code spans`, and *emphasis* so the "
    "round-trip through JSON encoding and decoding must preserve every byte "
    "exactly.\n"
    "# A markdown heading embedded in the statement\n"
    "Here is a `code span` and here is *emphasized text* and here is another "
    "paragraph that exists purely to push the total length of this fixture "
    "statement comfortably past six hundred characters so the assertion in "
    "the spec (over 600 characters) is unambiguously satisfied by this exact "
    "string, without any truncation, summarization, or loss of the trailing "
    "punctuation mark that ends it right here -> done."
)
assert len(LONG_STATEMENT) > 600


def main_fixture_events():
    return [
        # E1: ordinary auto-slug founding, unique vocabulary.
        {"type": "lesson", "ts": "2026-01-01T00:00:00+00:00", "subject": "e1",
         "text": "zeta theta iota kappa lambda unrelated founding statement"},
        # E2: all-stopword/digit text -> empty candidate -> problem-<n> fallback.
        {"type": "lesson", "ts": "2026-01-01T00:00:01+00:00", "subject": "e2",
         "text": "1999 the a an and"},
        # E3: 5 distinct 14-char words -> 60-char truncation lands exactly on
        # a hyphen; must be stripped, never left dangling.
        {"type": "lesson", "ts": "2026-01-01T00:00:02+00:00", "subject": "e3",
         "text": "aaaaaaaaaaaaaa bbbbbbbbbbbbbb cccccccccccccc dddddddddddddd eeeeeeeeeeeeee"},
        # E4: pre-assigned `problem` value that FAILS the slug pattern ->
        # falls through to case 2/3, slug derived from E4's own text.
        {"type": "lesson", "ts": "2026-01-01T00:00:03+00:00", "subject": "e4",
         "problem": "INVALID SLUG!!", "text": "omicron pi rho sigma tau upsilon phi chi psi omega"},
        # E5/E6: shared non-open fix, textually unrelated -> must merge.
        {"type": "lesson", "ts": "2026-01-01T00:00:04+00:00", "subject": "e5",
         "fix": "ADR-123", "text": "banana apple orange unrelated fruit words"},
        {"type": "lesson", "ts": "2026-01-01T00:00:05+00:00", "subject": "e6",
         "fix": "ADR-123", "text": "quantum neutrino boson entirely different physics"},
        # E7/E8: fix="open" / absent, textually unrelated -> must NOT merge.
        {"type": "lesson", "ts": "2026-01-01T00:00:06+00:00", "subject": "e7",
         "fix": "open", "text": "frog toad newt salamander lizard amphibian words"},
        {"type": "lesson", "ts": "2026-01-01T00:00:07+00:00", "subject": "e8",
         "text": "rocket satellite orbit telescope nebula astronomy words"},
        # E9: AC3's long, multi-line, markdown-bearing statement, sole member
        # of its own group (unique vocabulary).
        {"type": "lesson", "ts": "2026-01-01T00:00:08+00:00", "subject": "e9",
         "text": LONG_STATEMENT},
        # E10/E11: same valid pre-assigned slug, unrelated text -> must join
        # the one group named by that exact slug.
        {"type": "lesson", "ts": "2026-01-01T00:00:09+00:00", "subject": "e10",
         "problem": "custom-valid-slug", "text": "banana mango papaya guava kiwi tropical fruit"},
        {"type": "lesson", "ts": "2026-01-01T00:00:10+00:00", "subject": "e11",
         "problem": "custom-valid-slug", "text": "unrelated distinct vocabulary appears here now"},
    ]


def test_main_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        write_ledger(tmp, main_fixture_events())
        out = pathlib.Path(tmp) / "out.md"
        result = run(tmp, out=out)
        check("main fixture: exit 0", result.returncode == 0, result.stderr)
        text = out.read_text()

        m = re.match(r"^groups: (\d+) lessons: (\d+) ratio: (\d+\.\d{3})$", result.stdout.strip())
        check("main fixture: stdout line format", m is not None, result.stdout)
        check("main fixture: lessons count is 11", m and m.group(2) == "11", result.stdout)

        blocks = parse_groups(text)
        by_slug = {}
        all_members = []
        for slug, obj in blocks:
            check(f"AC9 keys exact for {slug}", set(obj.keys()) == {"slug", "statement", "members"}, obj.keys())
            check(f"AC9 heading matches body slug for {slug}", obj["slug"] == slug)
            check(f"AC2 slug matches pattern for {slug}", bool(SLUG_RE.match(slug)), slug)
            check(f"AC9 members non-empty list of str for {slug}",
                  isinstance(obj["members"], list) and len(obj["members"]) > 0
                  and all(isinstance(m_, str) for m_ in obj["members"]))
            check(f"AC2 no duplicate slug for {slug}", slug not in by_slug)
            by_slug[slug] = obj
            all_members.extend(obj["members"])

        check("AC1 (fixture): every member appears exactly once",
              len(all_members) == len(set(all_members)) == 11, all_members)

        def group_for(subject_line_no):
            src = f"L-fixture-0001.jsonl:{subject_line_no}"
            for slug, obj in blocks:
                if src in obj["members"]:
                    return slug, obj
            return None, None

        # E2 -> problem-<n> fallback (empty candidate).
        slug_e2, _ = group_for(2)
        check("AC2 empty-candidate falls back to problem-<n>", bool(re.match(r"^problem-\d+$", slug_e2 or "")), slug_e2)

        # E3 -> trailing hyphen stripped, never dangling.
        slug_e3, _ = group_for(3)
        check("AC2 trailing hyphen stripped", slug_e3 is not None and not slug_e3.endswith("-"), slug_e3)
        check("AC2 stripped slug still matches pattern", bool(SLUG_RE.match(slug_e3 or "")), slug_e3)

        # E4 -> mismatched pre-assigned value re-derived from its own text.
        slug_e4, _ = group_for(4)
        check("AC2 mismatched pre-assigned slug is NOT the raw invalid value",
              slug_e4 != "INVALID SLUG!!", slug_e4)
        check("AC2 mismatched pre-assigned slug derived from own text",
              slug_e4 == "omicron-pi-rho-sigma-tau", slug_e4)

        # E5/E6 -> merged by shared fix, despite zero text overlap.
        slug_e5, _ = group_for(5)
        slug_e6, _ = group_for(6)
        check("AC6 fix-sharing merges unrelated text", slug_e5 == slug_e6 and slug_e5 is not None,
              (slug_e5, slug_e6))

        # E7/E8 -> NOT merged (fix=open / absent, unrelated text).
        slug_e7, _ = group_for(7)
        slug_e8, _ = group_for(8)
        check("AC6 fix=open/absent does not merge unrelated text", slug_e7 != slug_e8, (slug_e7, slug_e8))

        # E9 -> AC3 byte-identical long/multi-line/markdown statement.
        slug_e9, obj_e9 = group_for(9)
        check("AC3 long multi-line statement byte-identical after JSON round-trip",
              obj_e9 is not None and obj_e9["statement"] == LONG_STATEMENT)

        # E10/E11 -> same valid pre-assigned slug, unrelated text, joined.
        slug_e10, _ = group_for(10)
        slug_e11, _ = group_for(11)
        check("AC7-style pre-assigned slug groups unrelated-text members together",
              slug_e10 == "custom-valid-slug" and slug_e11 == "custom-valid-slug", (slug_e10, slug_e11))


def test_determinism_ac4():
    with tempfile.TemporaryDirectory() as tmp:
        write_ledger(tmp, main_fixture_events())
        out1 = pathlib.Path(tmp) / "out1.md"
        out2 = pathlib.Path(tmp) / "out2.md"
        r1 = run(tmp, out=out1)
        r2 = run(tmp, out=out2)
        check("AC4: both runs exit 0", r1.returncode == 0 and r2.returncode == 0)
        check("AC4: two runs to two --out paths are byte-identical",
              out1.read_bytes() == out2.read_bytes())


def test_never_touches_events_ac5():
    with tempfile.TemporaryDirectory() as tmp:
        write_ledger(tmp, main_fixture_events())
        before = sha256_tree(pathlib.Path(tmp) / "events")
        out = pathlib.Path(tmp) / "out.md"
        run(tmp, out=out)
        after = sha256_tree(pathlib.Path(tmp) / "events")
        check("AC5: no fixture event file created/modified/deleted", before == after, (before, after))


def test_missing_events_dir_ac5():
    with tempfile.TemporaryDirectory() as tmp:
        # deliberately no events/ subdir created
        out = pathlib.Path(tmp) / "out.md"
        result = run(tmp, out=out)
        check("AC5: missing events/ dir exits non-zero", result.returncode != 0, result.returncode)
        check("AC5: missing events/ dir writes no output file", not out.exists())


def test_real_ledger_ac3_help_and_ac7():
    if not (REAL_DOIT_ROOT / "events").is_dir():
        check("AC3/AC7 real-ledger check: real ledger present", False, str(REAL_DOIT_ROOT))
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "real-out.md"
        result = run(REAL_DOIT_ROOT, out=out)
        check("real ledger: exit 0", result.returncode == 0, result.stderr)
        text = out.read_text()
        blocks = parse_groups(text)

        help_group = None
        for slug, obj in blocks:
            if any(m.endswith("L-operator-local.jsonl:1") for m in obj["members"]):
                help_group = (slug, obj)
                break
        check("AC3: real textless lesson L-operator-local.jsonl:1 found", help_group is not None)
        if help_group:
            check("AC3: its statement is exactly '--help'", help_group[1]["statement"] == "--help",
                  help_group[1]["statement"])

        thinker_group = None
        for slug, obj in blocks:
            if slug == "spec-shape-defect-escapes-spec-writer":
                thinker_group = obj
                break
        check("AC7: pre-assigned slug 'spec-shape-defect-escapes-spec-writer' present as a group",
              thinker_group is not None)
        if thinker_group:
            check("AC7: its members include a L-thinker-0004.jsonl ref",
                  any("L-thinker-0004.jsonl" in m for m in thinker_group["members"]),
                  thinker_group["members"])


def main():
    test_main_fixture()
    test_determinism_ac4()
    test_never_touches_events_ac5()
    test_missing_events_dir_ac5()
    test_real_ledger_ac3_help_and_ac7()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
