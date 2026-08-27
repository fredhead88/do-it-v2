#!/usr/bin/env python3
"""merge-gate checks. Every one runs against a real git repo, because the whole
scar is that a diff LOOKED clean — nothing here can be proven with a mock."""
import json, os, pathlib, subprocess, sys, tempfile

TMP = tempfile.mkdtemp()
os.environ["DOIT_ROOT"] = str(pathlib.Path(TMP) / "ledger")
os.environ["DOIT_LEDGER_FILE"] = "L-executor-test.jsonl"
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import merge_gate as mg

GRANT, ok = ["src/*", "docs/"], []


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout


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
removed, reverts = mg.gate("work", "main", GRANT)
check("gate names the removal", any(r.startswith("late.txt@") for r in removed))
check("the removal carries main's revision", removed and len(removed[0].split("@")[1]) == 7)

# 1b — PRECISION. main moved, the branch simply hasn't merged it. A three-way
# merge KEEPS those paths, so naming them is the misapplication that gets a
# guard worked around. Must be clean.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "keep.txt").write_text("branch edit"), merge=False)
check("un-merged main advance is NOT a removal", mg.gate("work", "main", GRANT)[0] == [])

# 2 — a removal INSIDE the writes: grant is the branch's own business.
d, _ = repo()
sh("git", "-C", d, "checkout", "-q", "main")
pathlib.Path(d, "src").mkdir(); pathlib.Path(d, "src/mine.py").write_text("x")
sh("git", "-C", d, "add", "-A"); sh("git", "-C", d, "commit", "-qm", "granted file")
on(d, lambda d: pathlib.Path(d, "src/mine.py").unlink())
check("granted removal is clean", mg.gate("work", "main", GRANT)[0] == [])

# 3 — migrations are named whatever the grant says (the excision case: the
# deletion removes the evidence along with the artifact, so parity stays GREEN).
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "migrations/001.sql").unlink())
check("migrations removal named even under a grant covering it",
      any(r.startswith("migrations/001.sql@")
          for r in mg.gate("work", "main", ["migrations/"])[0]))
check("migrations removal named with no grant",
      any(r.startswith("migrations/") for r in mg.gate("work", "main", GRANT)[0]))

# 4 — a survivor that reverts to a pre-main revision.
d, _ = repo()
on(d, lambda d: pathlib.Path(d, "old.txt").write_text("v1"))
_, reverts = mg.gate("work", "main", GRANT)
check("revert to a pre-main revision is caught", reverts == ["old.txt"])
check("a file that simply never moved is not a revert", "keep.txt" not in reverts)

# 5 — nothing to report.
d, _ = repo()
on(d, lambda d: None)
check("no removals, no reverts -> clean", mg.gate("work", "main", GRANT) == ([], []))

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

print(f"merge-gate: {sum(ok)}/{len(ok)} checks pass")
sys.exit(0 if all(ok) else 1)
