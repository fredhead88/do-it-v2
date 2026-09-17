#!/usr/bin/env python3
"""conflict checks. Every collision here is a REAL one — two branches, plain git,
`merge-tree` performing the merge. Nothing about a conflict can be proven by
handing a parser a string somebody typed: the whole question is what git does."""
import os, pathlib, subprocess, sys, tempfile

TMP = tempfile.mkdtemp()
os.environ["DOIT_ROOT"] = str(pathlib.Path(TMP) / "ledger")
os.environ["DOIT_GATE_LEDGER_FILE"] = "L-executor-conflict-test.jsonl"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import conflict as cf
import merge_gate as mg

ok = []


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout


def check(name, cond):
    ok.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + name)


def build(on_main, on_work, base=(("keep.txt", "keep"),)):
    """A repo with a base, an advance on main and an advance on work. The repo is
    left checked out on main and is NEVER cd'd into — `conflicted()` takes the
    directory as an argument precisely so it does not need the process to move."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t")
    sh("git", "-C", d, "config", "user.name", "t")
    for f, c in base:
        p = pathlib.Path(d, f)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    sh("git", "-C", d, "branch", "work")
    on_main(d)
    sh("git", "-C", d, "add", "-A")
    sh("git", "-C", d, "commit", "-q", "--allow-empty", "-m", "main moves on")
    sh("git", "-C", d, "checkout", "-q", "work")
    on_work(d)
    sh("git", "-C", d, "add", "-A")
    sh("git", "-C", d, "commit", "-q", "--allow-empty", "-m", "branch work")
    sh("git", "-C", d, "checkout", "-q", "main")
    return d


def w(d, name, text):
    p = pathlib.Path(d, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


LINES = "a\nb\nc\nd\ne\n"

# AC1 — an ordinary three-way merge of disjoint work is NOT a conflict, and
# asking the question does not move the caller's feet.
d = build(lambda d: w(d, "m.txt", "main only"), lambda d: w(d, "w.txt", "work only"))
cwd_before = os.getcwd()
r = cf.conflicted("work", "main", d)
check("disjoint three-way merge reports no conflict", r.conflicts is False)
check("a clean merge names no paths", r.paths == [])
check("★ the process cwd is unchanged across the call (never os.chdir)",
      os.getcwd() == cwd_before)

# AC1 — and a branch that IS main. `gate()` calls this could-not-determine
# because an empty diff is not a clean merge; the narrower question has an answer.
cwd_before = os.getcwd()
r = cf.conflicted("main", "main", d)
check("branch == main is not a conflict", r.conflicts is False and r.paths == [])
check("the cwd survives the same-commit path too", os.getcwd() == cwd_before)

# AC2 — one same-line content conflict, named exactly once, as a bare path.
d = build(lambda d: w(d, "f.txt", "MAIN\n" + LINES),
          lambda d: w(d, "f.txt", "WORK\n" + LINES),
          base=(("f.txt", "BASE\n" + LINES),))
r = cf.conflicted("work", "main", d)
check("a same-line content conflict is a conflict", r.conflicts is True)
check("★ exactly the colliding path, once, with no oid or stage leaking in",
      r.paths == ["f.txt"])

# AC3 — two independent collisions at once: a content conflict and an add/add.
# ★ The merge-tree message block carries an `Auto-merging <path>` record for
# files that merged CLEANLY, so a parse that runs past the empty field reports
# paths that never collided. `clean.txt` here is that trap, armed.
def two_main(d):
    w(d, "f.txt", "MAIN\n" + LINES)
    w(d, "dup.txt", "added on main")
    w(d, "clean.txt", "CLEAN-TOP\nx\ny\nz\n")


def two_work(d):
    w(d, "f.txt", "WORK\n" + LINES)
    w(d, "dup.txt", "added on work")
    w(d, "clean.txt", "top\nx\ny\nCLEAN-BOTTOM\n")


r = cf.conflicted("work", "main", build(two_main, two_work,
                                        base=(("f.txt", "BASE\n" + LINES),
                                              ("clean.txt", "top\nx\ny\nz\n"))))
check("two collisions: exactly two entries covering both files",
      len(r.paths) == 2 and sorted(r.paths) == ["dup.txt", "f.txt"])
check("neither is duplicated across its three stages", len(set(r.paths)) == 2)
check("★ a file that auto-merged cleanly is not reported as colliding",
      "clean.txt" not in r.paths)

# AC4 — the -z parse, proved on the two shapes that break a line-split or a
# C-quoted one. D114's lesson, re-applied: a Hebrew filename was silently
# dropped by C-quoting once already.
TAB = "tab\tfile.txt"
r = cf.conflicted("work", "main", build(lambda d: w(d, TAB, "MAIN\n"),
                                        lambda d: w(d, TAB, "WORK\n")))
check("★ a path containing a literal TAB is named byte for byte", r.paths == [TAB])
HEB = "sod/סוד.txt"
r = cf.conflicted("work", "main", build(lambda d: w(d, HEB, "MAIN\n"),
                                        lambda d: w(d, HEB, "WORK\n")))
check("★ a Hebrew path is named byte for byte, not C-quoted", r.paths == [HEB])

# and the NUL delimiter itself: `.strip()` does not remove \x00, so a parse that
# leans on whitespace-stripping keeps a NUL on the end of the last path.
check("no NUL survives into a reported path", all("\0" not in p for p in r.paths))

# AC5 — the count, and what it means. Pure: a list in, a verdict out.
E = [{"type": "conflict-rework", "subject": "L-spec-0047"}]
OTHER = {"type": "conflict-rework", "subject": "L-spec-0049",
         "paths": ["src/merge_gate.py"]}
check("no prior conflict -> attempt 1, re-dispatch",
      cf.conflict_attempts([], "L-spec-0047") == (1, "re-dispatch"))
check("★ one prior -> attempt 2, escalate (R8's rule, mechanised)",
      cf.conflict_attempts(E, "L-spec-0047") == (2, "escalate"))
check("two prior -> attempt 3, still escalate",
      cf.conflict_attempts(E * 2, "L-spec-0047") == (3, "escalate"))
check("★ another spec's conflict on the identical paths does not count here",
      cf.conflict_attempts([OTHER], "L-spec-0047") == (1, "re-dispatch"))
check("unrelated event types are ignored",
      cf.conflict_attempts([{"type": "merge-gate-rework", "subject": "L-spec-0047"}],
                           "L-spec-0047") == (1, "re-dispatch"))
check("the subject match is exact, never a prefix",
      cf.conflict_attempts([{"type": "conflict-rework", "subject": "L-spec-00470"}],
                           "L-spec-0047") == (1, "re-dispatch"))
check("conflict_attempts writes nothing and reads no ledger",
      not (pathlib.Path(os.environ["DOIT_ROOT"]) / "events").exists())

# the hardening is INHERITED, not reimplemented — a truncated history cannot
# answer "would this conflict?" any more than it can answer the gate's question.
d = build(lambda d: w(d, "f.txt", "MAIN\n"), lambda d: w(d, "f.txt", "WORK\n"),
          base=(("f.txt", "BASE\n"),))
shallow = tempfile.mkdtemp() + "/s"
sh("git", "clone", "-q", "--depth", "1", "--no-local", "file://" + d, shallow)
try:
    cf.conflicted("HEAD", "HEAD", shallow); check("shallow repo raises", False)
except mg.Undetermined as e:
    check("★ a shallow clone is a hard stop here too, not a 'no conflict'",
          "shallow" in str(e).lower())
try:
    cf.conflicted("no-such-branch", "main", d); check("unknown ref raises", False)
except mg.Undetermined:
    check("an unresolvable ref re-raises Undetermined unchanged", True)

# and GIT_* cannot redirect this question at another repository. `d` above is a
# real conflict; `other` is clean. Built FIRST — the fixture's own plain git calls
# would be redirected by the very variable under test.
other = build(lambda d: w(d, "m.txt", "m"), lambda d: w(d, "w.txt", "w"))
os.environ["GIT_DIR"] = str(pathlib.Path(d, ".git"))
try:
    check("★ GIT_DIR cannot redirect conflicted() at another repository",
          cf.conflicted("work", "main", other).conflicts is False)
finally:
    del os.environ["GIT_DIR"]
# ...and the mechanism itself, not just one variable: the ONLY GIT_* keys git is
# handed are the two the module sets on purpose. An inherited GIT_EDITOR — which
# this very suite runs under — must not be among them.
check("the only GIT_* in the env git is handed are the two set deliberately",
      {k for k in mg.ENV if k.startswith("GIT_")}
      == {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"})

print(f"conflict: {sum(ok)}/{len(ok)} checks pass")
sys.exit(0 if all(ok) else 1)
