#!/usr/bin/env python3
"""merge-gate checks. Every one runs against a real git repo, because the whole
scar is that a diff LOOKED clean — nothing here can be proven with a mock."""
import json, os, pathlib, subprocess, sys, tempfile

TMP = tempfile.mkdtemp()
os.environ["DOIT_ROOT"] = str(pathlib.Path(TMP) / "ledger")
os.environ["DOIT_GATE_LEDGER_FILE"] = "L-executor-test.jsonl"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import merge_gate as mg

GRANT, ok = ["src/*", "docs/"], []
CONTENT = pathlib.Path(os.environ["DOIT_ROOT"]) / "content"
CONTENT.mkdir(parents=True, exist_ok=True)
(CONTENT / "L-spec-0001.md").write_text("# spec\n\nwrites: src/*, docs/\n")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout


def _exit(fn):
    """True when the callable exits rather than proceeding on bad input."""
    try:
        fn(); return False
    except SystemExit:
        return True


def check(name, cond):
    ok.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + name)


def repo():
    """main gains `late.txt` AFTER the branch point — the exact scar shape.
    Returns (dir, recorded base_sha) — the sha v1's guard would have diffed from."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    for f, c in (("keep.txt", "keep"), ("old.txt", "v1"), ("migrations/001.sql", "create")):
        p = pathlib.Path(d, f); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(c)
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    base = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "branch", "work")                       # base_sha is RECORDED here
    pathlib.Path(d, "late.txt").write_text("main gained this")
    pathlib.Path(d, "old.txt").write_text("v2")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "main moves on")
    return d, base


def on(d, fn, merge=True):
    """Do the branch's work. `merge` = the branch pulled main in first, which is
    what makes the deletion a deletion — and what erases it from the range."""
    sh("git", "-C", d, "checkout", "-q", "work")
    if merge:
        sh("git", "-C", d, "merge", "-q", "--no-edit", "main")
    fn(d)
    sh("git", "-C", d, "add", "-A")
    sh("git", "-C", d, "commit", "-q", "--allow-empty", "-m", "branch work")
    os.chdir(d)


# 1 — THE SCAR. v1's range check sees nothing at all; the gate must see it.
d, base = repo()
on(d, lambda d: pathlib.Path(d, "late.txt").unlink())
check("base_sha..branch produces no row at all (why v1's guard missed it)",
      "late.txt" not in sh("git", "-C", d, "diff", "--name-status", base, "work"))
removed, reverts = mg.gate("work", "main", GRANT)[:2]
check("gate names the removal", any(r.startswith("late.txt@") for r in removed))
check("the removal carries main's revision", removed and len(removed[0].split("@")[1]) == 7)

# 1b — PRECISION. main moved, the branch simply hasn't merged it. A three-way
# merge KEEPS those paths, so naming them is the misapplication that gets a
# guard worked around. Must be clean.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "keep.txt").write_text("branch edit"), merge=False)
check("un-merged main advance is NOT a removal", mg.gate("work", "main", GRANT).removed == [])

# 2 — a removal INSIDE the writes: grant is the branch's own business.
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "src").mkdir(); pathlib.Path(d, "src/mine.py").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "granted file")
on(d, lambda d: pathlib.Path(d, "src/mine.py").unlink())
check("granted removal is clean", mg.gate("work", "main", GRANT).removed == [])

# 3 — migrations are named whatever the grant says (the excision case: the
# deletion removes the evidence along with the artifact, so parity stays GREEN).
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "migrations/001.sql").unlink())
check("migrations removal named even under a grant covering it",
      any(r.startswith("migrations/001.sql@")
          for r in mg.gate("work", "main", ["migrations/"]).removed))
check("migrations removal named with no grant",
      any(r.startswith("migrations/") for r in mg.gate("work", "main", GRANT).removed))

# 4 — a survivor that reverts to a pre-main revision.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "old.txt").write_text("v1"))
reverts = mg.gate("work", "main", GRANT).reverted
check("revert to a pre-main revision is caught", reverts == ["old.txt"])
check("a file that simply never moved is not a revert", "keep.txt" not in reverts)

# 5 — nothing to report.
d, _ = repo()
on(d, lambda d: None)
check("no removals, no reverts -> clean", mg.gate("work", "main", GRANT)[:2] == ([], []))

# 6 — could-not-determine is never clean, and it is never silent.
try:
    mg.gate("no-such-branch", "main", GRANT); check("unresolvable ref raises", False)
except mg.Undetermined:
    check("unresolvable ref raises Undetermined", True)
check("undetermined exits rework, not clean", mg.main_(["no-such-branch", "main"]) == 1)
LEDGER = pathlib.Path(os.environ["DOIT_ROOT"]) / "events" / "L-executor-test.jsonl"
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("the stop is in the ledger",
      ev[-1]["type"] == "merge-gate-rework" and "undetermined" in ev[-1])

# 7 — the caller contract: exit code, subject, and the rework payload.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "late.txt").unlink())
check("a dirty branch exits 1", mg.main_(["work", "main", "--spec", "L-spec-0001"]) == 1)
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("rework event carries removed[] and reverted[]",
      ev[-1]["type"] == "merge-gate-rework" and ev[-1]["subject"] == "L-spec-0001"
      and any(r.startswith("late.txt@") for r in ev[-1]["removed"]))
d, _ = repo(); on(d, lambda d: None)
check("a clean branch exits 0", mg.main_(["work", "main"]) == 0)
ev = [json.loads(l) for l in LEDGER.read_text().splitlines()]
check("clean event written too", ev[-1]["type"] == "merge-gate-clean")

# ---------------------------------------------------------------- D113 cases
# Every one of these was CLEAN under the merge-base mechanism and is a real
# removal. They are the reason D110 was superseded.

def crisscross():
    """A long-lived branch merged into and kept going has SEVERAL merge-bases."""
    d = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", d)
    sh("git", "-C", d, "config", "user.email", "t@t"); sh("git", "-C", d, "config", "user.name", "t")
    pathlib.Path(d, "a.txt").write_text("a")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "base")
    sh("git", "-C", d, "branch", "work")
    pathlib.Path(d, "victim.txt").write_text("victim")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "M1")
    m1 = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "checkout", "-q", "work")
    pathlib.Path(d, "w.txt").write_text("w")
    sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "W1")
    w1 = sh("git", "-C", d, "rev-parse", "HEAD").strip()
    sh("git", "-C", d, "merge", "-q", "--no-edit", m1)
    sh("git", "-C", d, "checkout", "-q", "--detach", m1)
    sh("git", "-C", d, "merge", "-q", "--no-edit", w1)
    sh("git", "-C", d, "branch", "-f", "main", "HEAD")
    sh("git", "-C", d, "checkout", "-q", "work")
    sh("git", "-C", d, "rm", "-q", "victim.txt")
    sh("git", "-C", d, "commit", "-qm", "work deletes victim")
    os.chdir(d)
    return d

d = crisscross()
check("criss-cross history has more than one merge-base",
      len(sh("git", "-C", d, "merge-base", "--all", "main", "work").split()) > 1)
check("★ criss-cross removal is named (D110 said clean here)",
      any(r.startswith("victim.txt@") for r in mg.gate("work", "main", []).removed))

d, _ = repo()
on(d, lambda d: (pathlib.Path(d, "sod").mkdir(exist_ok=True),
                 pathlib.Path(d, "sod/סוד.txt").write_text("x")))
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "sod").mkdir(exist_ok=True); pathlib.Path(d, "sod/סוד.txt").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "main gains a hebrew path")
on(d, lambda d: pathlib.Path(d, "sod/סוד.txt").unlink())
check("★ a Hebrew filename is named, not silently dropped by C-quoting",
      any(r.startswith("sod/סוד.txt@") for r in mg.gate("work", "main", GRANT).removed))

d, _ = repo()
def swap(d):
    pathlib.Path(d, "migrations/001.sql").unlink()
    os.symlink("/dev/null", pathlib.Path(d, "migrations/001.sql"))
on(d, swap)
check("★ typechange (file replaced by a symlink) is a removal",
      any(r.startswith("migrations/001.sql@") for r in mg.gate("work", "main", []).removed))

# grant boundaries — over-granting is under-reporting
check("a grant of `src` does not cover `src_backup/keys.py`",
      not mg.granted("src_backup/keys.py", ["src"]))
check("a grant of `docs` does not cover `docs-internal/runbook.md`",
      not mg.granted("docs-internal/runbook.md", ["docs"]))
check("`*` does not cross a path separator", not mg.granted("secrets/prod_keys.py", ["*.py"]))
check("a bare `*` does not disable the gate", not mg.granted("secrets/prod.py", ["*"]))
check("`**` does cross, when you ask for it", mg.granted("src/deep/a.py", ["src/**"]))
check("a grant of a file does not cover its .orig",
      not mg.granted("src/a.py.orig", ["src/a.py"]))
check("a directory grant still works", mg.granted("docs/x.md", ["docs/"]))

# the migrations carve-out must fire where migrations actually live
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "supabase/migrations").mkdir(parents=True)
pathlib.Path(d, "supabase/migrations/001_init.sql").write_text("create")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "real migrations location")
on(d, lambda d: pathlib.Path(d, "supabase/migrations/001_init.sql").unlink())
check("★ supabase/migrations/ is named even under a grant covering it",
      any("supabase/migrations" in r for r in mg.gate("work", "main", ["supabase/**"]).removed))

# could-not-determine, in all its forms
d, _ = repo(); on(d, lambda d: None)
try:
    mg.writes_grant("L-spec-nonexistent"); check("missing spec raises", False)
except mg.Undetermined:
    check("a named spec with no content file is could-not-determine", True)
(CONTENT / "L-spec-nogrant.md").write_text("# spec with no grant line\n")
try:
    mg.writes_grant("L-spec-nogrant"); check("missing grant raises", False)
except mg.Undetermined:
    check("a spec stating no writes: grant is could-not-determine, not an empty grant", True)
check("the design's own `| **Writes** | src/* |` table row parses",
      (lambda: [(CONTENT / "L-spec-tbl.md").write_text("| **Writes** | `src/*`, docs/ |\n"),
                mg.writes_grant("L-spec-tbl")][1])() == ["src/*", "docs/"])

# ------------------------------------------------- D114: ambient state and renames
# Both judging seats, blind to each other, ranked the rename hole first.

d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "supabase/migrations").mkdir(parents=True)
pathlib.Path(d, "supabase/migrations/001_init.sql").write_text("create")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "real migrations")
def moveout(d):
    pathlib.Path(d, "src").mkdir(exist_ok=True)
    sh("git", "-C", d, "mv", "late.txt", "src/late.txt")
    sh("git", "-C", d, "mv", "supabase/migrations/001_init.sql", "src/001_init.sql")
on(d, moveout)
r = mg.gate("work", "main", ["src/**"])
check("★ a rename into the grant is still a removal of the old path",
      any(x.startswith("late.txt@") for x in r.removed))
check("★ a migration moved out of migrations/ is named despite the grant",
      any("supabase/migrations/001_init.sql" in x for x in r.removed))
check("the rename destination is reported, not hidden",
      any("-> src/late.txt" in x for x in r.removed))

# an ambiguous ref must not silently resolve to the wrong object
d, _ = repo()
sh("git", "-C", d, "tag", "main", "HEAD~1")          # a TAG named main, beside the branch
os.chdir(d)
try:
    mg.gate("work", "main", []); check("ambiguous ref raises", False)
except mg.Undetermined as e:
    check("★ a ref that is both a tag and a branch is could-not-determine",
          "ambiguous" in str(e).lower())

# a truncated history cannot answer a question about history
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
shallow = tempfile.mkdtemp() + "/s"
sh("git", "clone", "-q", "--depth", "1", "--no-local", "file://" + d, shallow)
os.chdir(shallow)
try:
    mg.gate("HEAD", "HEAD", []); check("shallow repo raises", False)
except mg.Undetermined as e:
    check("★ a shallow clone is a hard stop, not a clean", "shallow" in str(e).lower())

# ambient git config must not be able to hide a removal
d, _ = repo()
sh("git", "-C", d, "config", "diff.relative", "true")
sh("git", "-C", d, "config", "diff.ignoreSubmodules", "all")
on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
pathlib.Path(d, "web").mkdir(exist_ok=True)
os.chdir(pathlib.Path(d, "web"))
check("★ diff.relative + a subdirectory cwd cannot hide a removal",
      any(x.startswith("late.txt@") for x in mg.gate("work", "main", []).removed))
os.chdir(d)

# the deadline is per run, not per process
d, _ = repo(); on(d, lambda dd: None)
mg.DEADLINE = 3600
import time as _t
mg.gate("work", "main", [])
mg._START = _t.monotonic() - 10_000          # as if a previous run had burned the clock
mg.DEADLINE = 60
check("★ the deadline clock restarts each run", mg.gate("work", "main", [])[:2] == ([], []))

# grants that are not grants
for bad in ("*", "**", "**/*"):
    (CONTENT / "L-spec-off.md").write_text(f"writes: {bad}\n")
    try:
        mg.writes_grant("L-spec-off"); check(f"grant {bad!r} rejected", False)
    except mg.Undetermined as e:
        check(f"★ a grant of {bad!r} is refused as an off switch", "off switch" in str(e))

# prose must not become a grant
(CONTENT / "L-spec-prose.md").write_text(
    "Writes are limited to the merge gate script.\n\n| **Writes** | `src/*` |\n")
check("★ an English sentence starting with 'Writes' is not parsed as a grant",
      mg.writes_grant("L-spec-prose") == ["src/*"])
(CONTENT / "L-spec-empty.md").write_text("| writes | |\n")
try:
    mg.writes_grant("L-spec-empty"); check("empty grant rejected", False)
except mg.Undetermined:
    check("a writes: line with no paths on it is could-not-determine", True)

# argument handling: a typo is not a verdict
check("a stray argument is refused", _exit(lambda: mg.parse(["main", "src/*"])))
check("--writes with no value is refused", _exit(lambda: mg.parse(["main", "--writes"])))
check("an unknown flag is refused", _exit(lambda: mg.parse(["main", "-x"])))
check("★ a flag where the branch belongs is refused before any event (`doit gate --help`)", _exit(lambda: mg.main_(["--help"])))

# ------------------------------------------------- D115: round 3's findings
# ★ The lesson of --writes: three arg checks all asserted FAILURE cases, so a
# flag that never worked at all passed its own suite. Positive cases first.
check("★ --writes actually works (it exited unconditionally for two rounds)",
      mg.parse(["main", "--writes", "src/*"]) == ("main", None, ["src/*"]))
check("--writes takes several values",
      mg.parse(["main", "--writes", "src/*", "docs/"])[2] == ["src/*", "docs/"])
check("--spec and --writes compose",
      mg.parse(["main", "--writes", "src/*", "--spec", "L-spec-0001"])[:2] == ("main", "L-spec-0001"))
check("a stray argument is still refused", _exit(lambda: mg.parse(["main", "src/*"])))
check("--writes with no value is still refused", _exit(lambda: mg.parse(["main", "--writes"])))

# prose after the colon must not become a grant
for prose, why in ((("writes: the web dashboard only"), "'web' became a directory grant"),
                   (("Writes: see the table below"), "'table' became a directory grant")):
    (CONTENT / "L-spec-p.md").write_text(prose + "\n")
    try:
        g = mg.writes_grant("L-spec-p"); check(f"★ prose refused ({why})", False)
    except mg.Undetermined as e:
        check(f"★ prose after the colon is not a grant ({why})", "not a path" in str(e))

# breadth is behaviour, not a list of spellings
for bad in ("*/**", "**.*", "**/*.*", "*", "**", "**/*"):
    try:
        mg.check_grant([bad], "t"); check(f"grant {bad!r} refused", False)
    except mg.Undetermined as e:
        check(f"★ {bad!r} refused as an off switch", "off switch" in str(e))

# ...while the real forms still parse
(CONTENT / "L-spec-bold.md").write_text("**Writes:** `src/*`, docs/\n")
check("★ **Writes:** src/* parses (it was refused, accusing the author)",
      mg.writes_grant("L-spec-bold") == ["src/*", "docs/"])
(CONTENT / "L-spec-tbl2.md").write_text("| **Writes** | `src/**`, `db/migrate/` |\n")
check("a table row with symmetric globs survives",
      mg.writes_grant("L-spec-tbl2") == ["src/**", "db/migrate/"])
check("a bare directory needs its slash, and then works", mg.granted("src/a.py", ["src/"]))

# a line break in ANY path must not misalign the batch and kill revert detection
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
weird = pathlib.Path(d, "we\nird.txt"); weird.write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "a path with a line break")
on(d, lambda dd: pathlib.Path(dd, "old.txt").write_text("v1"))
check("★ a newline-bearing path does not kill revert detection for other files",
      mg.gate("work", "main", GRANT).reverted == ["old.txt"])

# ambient env must not redirect the gate at another repo
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "late.txt").unlink())
other, _ = repo()
os.chdir(d); os.environ["GIT_DIR"] = str(pathlib.Path(other, ".git"))
try:
    check("★ GIT_DIR cannot redirect the gate to another repository",
          any(x.startswith("late.txt@") for x in mg.gate("work", "main", []).removed))
finally:
    del os.environ["GIT_DIR"]

# an ambiguous ref is refused even when git is told not to warn about it
d, _ = repo()
sh("git", "-C", d, "tag", "main", "HEAD~1")
sh("git", "-C", d, "config", "core.warnAmbiguousRefs", "false")
os.chdir(d)
try:
    mg.gate("work", "main", []); check("ambiguity refused", False)
except mg.Undetermined as e:
    check("★ ambiguity is read from the ref store, not from a warning git can be told to suppress",
          "ambiguous" in str(e))

# knobs may not silently weaken the gate
d, _ = repo(); on(d, lambda dd: pathlib.Path(dd, "migrations/001.sql").unlink())
for var, val in (("DOIT_MIGRATIONS", "zzz"), ("DOIT_REVERT_DEPTH", "0")):
    os.environ[var] = val
    try:
        mg.gate("work", "main", ["migrations/"]); check(f"{var}={val} refused", False)
    except mg.Undetermined:
        check(f"★ {var}={val} is refused, not silently honoured", True)
    finally:
        del os.environ[var]
mg.load_config()

d, _ = repo(); os.chdir(d)
try:
    mg.gate("main", "main", []); check("self-merge refused", False)
except mg.Undetermined as e:
    check("gating a branch against itself is could-not-determine", "nothing to gate" in str(e))

print(f"merge-gate: {sum(ok)}/{len(ok)} checks pass")
sys.exit(0 if all(ok) else 1)
