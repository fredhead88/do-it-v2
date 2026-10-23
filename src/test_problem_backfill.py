#!/usr/bin/env python3
"""Hermetic, subprocess-isolated tests for scripts/problem_backfill.py
(L-spec-0278). `fold.ROOT`/`EVENTS`/`PROJECT` bind once at `import fold` time
from `os.environ` (`src/fold.py:19-28`), so every fixture case here runs the
script as its OWN subprocess with a fresh, minimal env carrying only `PATH`
and `DOIT_ROOT` (never `DOIT_PROJECT`) -- never by importing `fold` or the
script in-process and reassigning `DOIT_ROOT` afterward. That is what makes
these fixtures actually hermetic against the real `~/.do-it` ledger.

Only --live runs the real-ledger AC3/AC7 checks. doit supplies the explicit
DOIT_TEST_LIVE_ROOT before isolating HOME/DOIT_ROOT; without it they print
SKIP live and exit 3. The default suite never resolves a real ledger root.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

PYTHON = sys.executable
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "problem_backfill.py"
SLUG_RE = re.compile(r"^[a-z0-9-]{3,60}$")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"ok - {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL - {name} {detail}")


def run(doit_root, out=None, extra_args=None):
    """Run the script as its own subprocess against `doit_root`, with a
    minimal, freshly-built env: PATH and fixture HOME/DOIT_ROOT. DOIT_PROJECT is
    guaranteed absent because the env dict is built from scratch."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(doit_root), "DOIT_ROOT": str(doit_root)}
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
    live_root = os.environ.get("DOIT_TEST_LIVE_ROOT")
    if not live_root or not (pathlib.Path(live_root) / "events").is_dir():
        print("SKIP live: no live ledger root")
        return 3
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "real-out.md"
        result = run(live_root, out=out)
        check("real ledger: exit 0", result.returncode == 0, result.stderr)
        if result.returncode:
            return 1
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


def test_live_launcher_contract():
    """Exercise the real launcher with disposable probes, never the live ledger."""
    with tempfile.TemporaryDirectory(prefix="doit-live-test-") as tmp:
        tree = pathlib.Path(tmp)
        launcher = tree / "doit"
        launcher.write_bytes((REPO_ROOT / "doit").read_bytes())
        src = tree / "src"
        src.mkdir()
        # The copied launcher runs the real retirement check after its probes.
        (src / "retired.py").write_bytes((REPO_ROOT / "src" / "retired.py").read_bytes())
        # The copied launcher also runs the machine gate with real dependencies.
        for module in (REPO_ROOT / "src").glob("*.py"):
            if not module.name.startswith("test_"):
                (src / module.name).write_bytes(module.read_bytes())
        (tree / "retired.d").mkdir()
        (tree / "retired.d" / "fixture.toml").write_text(
            '[[retired]]\nname="fixture-clean"\nkind="code"\n'
            'pattern="removed fixture mechanism"\npaths=["src/test_*.py"]\n')
        probe = '''import os, pathlib, sys
if sys.argv[1:] == ["--live"]:
    root = pathlib.Path(os.environ["DOIT_TEST_LIVE_ROOT"])
    assert (root / "events" / "L-fixture.jsonl").is_file()
    assert os.environ["DOIT_ROOT"] != str(root)
    assert "DOIT_LEDGER_FILE" not in os.environ
    print("probe live")
    sys.exit(int(os.environ.get("DOIT_LIVE_PROBE_RC", "0")))
assert "DOIT_TEST_LIVE_ROOT" not in os.environ
print("probe hermetic")
'''
        for name in ("test_harvest.py", "test_problem_backfill.py"):
            (src / name).write_text(probe)
        (src / "test_probe.py").write_text(
            'import sys\nassert not sys.argv[1:]\nprint("unmarked hermetic")\n')
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        subprocess.run(["git", "-C", str(tree), "add", "src", "retired.d"], check=True)
        live_root = tree / "ledger"
        (live_root / "events").mkdir(parents=True)
        ledger = live_root / "events" / "L-fixture.jsonl"
        ledger.write_text('{}\n')
        before = sha256_tree(live_root / "events")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(tree / "home"), "DOIT_ROOT": str(live_root),
               "DOIT_LEDGER_FILE": "L-sentinel.jsonl"}

        def invoke(*args, **overrides):
            return subprocess.run(["bash", str(launcher), "test", *args],
                                  env={**env, **overrides}, cwd=tree,
                                  capture_output=True, text=True)

        for label, root in (("missing", tree / "absent"), ("empty", tree / "empty")):
            (tree / "empty" / "events").mkdir(parents=True, exist_ok=True)
            result = invoke("--live", DOIT_ROOT=str(root))
            check(f"live launcher: {label} ledger is not run",
                  result.returncode == 3 and result.stdout.splitlines()[-1:] == ["live: not run"],
                  (result.returncode, result.stdout, result.stderr))
            check(f"live launcher: {label} ledger starts no checks", "probe" not in result.stdout)

        for code, label in ((0, "pass"), (1, "fail"), (3, "not run")):
            result = invoke("--live", DOIT_LIVE_PROBE_RC=str(code))
            check(f"live launcher: reports {label} and exit {code}",
                  result.returncode == code and result.stdout.splitlines()[-1:] == [f"live: {label}"],
                  (result.returncode, result.stdout, result.stderr))
            check(f"live launcher: {label} runs only the two live checks",
                  result.stdout.count("probe live") == 2 and "hermetic" not in result.stdout)
            check(f"live launcher: {label} identifies each live check",
                  [line for line in result.stdout.splitlines() if line.startswith("live-check ")]
                  == ["live-check test_harvest.py:check_ledger",
                      "live-check test_problem_backfill.py:test_real_ledger_ac3_help_and_ac7"])
            check(f"live launcher: {label} has exactly one result line",
                  sum(line.startswith("live:") for line in result.stdout.splitlines()) == 1)

        result = invoke(DOIT_TEST_LIVE_ROOT=str(live_root))
        check("hermetic launcher: clears inherited live root and runs all files",
              result.returncode == 0 and result.stdout.count("PASS test_") == 3
              and "probe live" not in result.stdout
              and not any(line.startswith("live:") for line in result.stdout.splitlines()),
              (result.returncode, result.stdout, result.stderr))
        check("live launcher: fixture ledger unchanged", before == sha256_tree(live_root / "events"))


def test_branch_clone_launcher_contract():
    """A branch-only clone gets a disposable grader baseline, never a new ref."""
    with tempfile.TemporaryDirectory(prefix="doit-branch-clone-test-") as tmp:
        tree = pathlib.Path(tmp) / "source"
        (tree / "src").mkdir(parents=True)
        (tree / "doit").write_bytes((REPO_ROOT / "doit").read_bytes())
        (tree / "src" / "retired.py").write_bytes((REPO_ROOT / "src" / "retired.py").read_bytes())
        for module in (REPO_ROOT / "src").glob("*.py"):
            if not module.name.startswith("test_"):
                (tree / "src" / module.name).write_bytes(module.read_bytes())
        (tree / "retired.d").mkdir()
        (tree / "retired.d" / "fixture.toml").write_text(
            '[[retired]]\nname="fixture-clean"\nkind="code"\n'
            'pattern="removed fixture mechanism"\npaths=["src/test_*.py"]\n')
        probe = tree / "src" / "test_grader_view.py"
        probe.write_text('''import pathlib, subprocess
repo = pathlib.Path(__file__).resolve().parent.parent
subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD", "master"],
               check=True, stdout=subprocess.DEVNULL)
assert (repo / "sentinel").read_text() == "committed checkout\\n"
print("branch clone baseline ok")
''')
        subprocess.run(["git", "init", "-q", "-b", "fixture-branch", str(tree)], check=True)
        for key, value in (("user.name", "fixture"), ("user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(tree), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(tree), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tree), "commit", "-qm", "baseline"], check=True)
        (tree / "sentinel").write_text("committed checkout\n")
        subprocess.run(["git", "-C", str(tree), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tree), "commit", "-qm", "candidate"], check=True)
        clone = pathlib.Path(tmp) / "clone"
        subprocess.run(["git", "clone", "-q", "--single-branch", str(tree), str(clone)], check=True)
        refs = ["git", "-C", str(clone), "show-ref"]
        before = subprocess.check_output(refs)
        iso = pathlib.Path(tmp) / "scratch"
        iso.mkdir()
        result = subprocess.run(["bash", str(clone / "doit"), "test"], cwd=clone,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "TMPDIR": str(iso), "HOME": str(pathlib.Path(tmp) / "home")},
                                capture_output=True, text=True)
        check("branch clone launcher: grader check passes with a merge baseline",
              result.returncode == 0 and "PASS test_grader_view.py" in result.stdout,
              (result.returncode, result.stdout, result.stderr))
        check("branch clone launcher: original refs unchanged", before == subprocess.check_output(refs))
        check("branch clone launcher: scratch checkout removed", not any(iso.iterdir()))


def main():
    if sys.argv[1:] == ["--live"]:
        result = test_real_ledger_ac3_help_and_ac7()
        if result:
            return result
    else:
        test_main_fixture()
        test_determinism_ac4()
        test_never_touches_events_ac5()
        test_missing_events_dir_ac5()
        test_live_launcher_contract()
        test_branch_clone_launcher_contract()
        print("SKIP live: real-ledger AC3/AC7 require doit test --live")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
