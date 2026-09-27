#!/usr/bin/env python3
"""test_scratch — proves AC1-AC6 of L-spec-0425 (scratch.root / scratch.sub).

Run: python3 test_scratch.py

Every fixture directory here is created fresh under harness.test_root(...)
for this run and is never the real $HOME, the real $DOIT_ROOT, or a directory
shared with another AC's fixture (spec's AC-shared note). DOIT_SCRATCH/
DOIT_ROOT/HOME are pinned directly on os.environ with no teardown, matching
test_pane_end.py's existing convention — a fresh interpreter runs this file
each ./doit test pass, so there is nothing to restore.

No AC here invokes a real `git` subprocess: the git-work-tree fixtures create
a bare `.git` file or directory by hand, sufficient to exercise
scratch._git_ancestor's existence-only scan without adding a git-binary
dependency to this test.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402
import scratch  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def set_env(**kv):
    """Set/clear os.environ entries. A value of None pops the key (unset);
    any other value (including "") sets it verbatim."""
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)


def make_git(path, as_file):
    """Create a bare .git entry (dir or file) under path, by hand — never a
    real `git init`/subprocess."""
    path.mkdir(parents=True, exist_ok=True)
    git = path / ".git"
    if as_file:
        git.write_text("gitdir: /nowhere\n")
    else:
        git.mkdir()


def raises(exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type as e:
        return e
    return None


# ── AC1: unset (and unset-equivalent "") DOIT_SCRATCH → $HOME/doit-scratch ──
home1 = harness.test_root("ac1-home")
set_env(DOIT_SCRATCH=None, DOIT_ROOT=None, HOME=str(home1))
expect1 = (home1 / "doit-scratch").resolve()
r1 = scratch.root()
check(r1 == expect1, f"AC1: root() must return {expect1}, got {r1}")
check(r1.is_dir(), f"AC1: root() must create {r1} as a directory")
r1b = scratch.root()
check(r1b == r1 and r1.is_dir(), "AC1: second call must return the identical existing path, unremoved")

home1c = harness.test_root("ac1-home-empty")
set_env(DOIT_SCRATCH="", DOIT_ROOT=None, HOME=str(home1c))
expect1c = (home1c / "doit-scratch").resolve()
r1c = scratch.root()
check(r1c == expect1c and r1c.is_dir(), "AC1: DOIT_SCRATCH='' must be treated the same as unset")
print("AC1 ok")

# ── AC2: DOIT_SCRATCH set to a not-yet-existing path outside root/git ───────
base2 = harness.test_root("ac2-base")
target2 = base2 / "nested" / "scratch-target"
set_env(DOIT_SCRATCH=str(target2), DOIT_ROOT=None, HOME=str(harness.test_root("ac2-home")))
r2 = scratch.root()
check(r2 == target2.resolve(), f"AC2: root() must return {target2.resolve()}, got {r2}")
check(r2.is_dir(), "AC2: root() must create the target directory (parents included)")
subdir2 = scratch.sub("grade")
check(subdir2 == r2 / "grade" and subdir2.is_dir(), "AC2: sub('grade') must create root()/'grade'")
print("AC2 ok")

# ── AC3: DOIT_ROOT containment (never mentions git) ─────────────────────────
root3a = harness.test_root("ac3-root-a")  # plain directory, deliberately NOT a git repo
target3a = root3a / "nested" / "target"  # strictly under DOIT_ROOT, parents not yet existing
set_env(DOIT_SCRATCH=str(target3a), DOIT_ROOT=str(root3a), HOME=str(harness.test_root("ac3-home-a")))
e3a = raises(scratch.ScratchRootError, scratch.root)
check(e3a is not None, "AC3a: root() must raise ScratchRootError for a DOIT_SCRATCH strictly under DOIT_ROOT")
msg3a = str(e3a)
check("DOIT_ROOT" in msg3a, f"AC3a: message must name DOIT_ROOT, got {msg3a!r}")
check(".git" not in msg3a and "git work tree" not in msg3a, f"AC3a: message must not mention git, got {msg3a!r}")
check(not target3a.exists(), "AC3a: nothing partially created")

root3b = harness.test_root("ac3-root-b")  # not a git repo
set_env(DOIT_SCRATCH=str(root3b), DOIT_ROOT=str(root3b), HOME=str(harness.test_root("ac3-home-b")))
e3b = raises(scratch.ScratchRootError, scratch.root)
check(e3b is not None, "AC3b: root() must raise when DOIT_SCRATCH equals DOIT_ROOT exactly")
check("DOIT_ROOT" in str(e3b), f"AC3b: message must name DOIT_ROOT, got {e3b!r}")

real_root3c = harness.test_root("ac3-real-root")  # not a git repo
link_base3c = harness.test_root("ac3-link-base")
root_link3c = link_base3c / "root_link"
root_link3c.symlink_to(real_root3c, target_is_directory=True)
target3c = real_root3c / "nested" / "target"  # addressed via real_root3c directly, never root_link3c
set_env(DOIT_SCRATCH=str(target3c), DOIT_ROOT=str(root_link3c), HOME=str(harness.test_root("ac3-home-c")))
e3c = raises(scratch.ScratchRootError, scratch.root)
check(e3c is not None, "AC3c: a symlinked DOIT_ROOT must still be enforced against its resolved target")
check("DOIT_ROOT" in str(e3c), f"AC3c: message must name DOIT_ROOT, got {e3c!r}")
check(not target3c.exists(), "AC3c: nothing partially created")
print("AC3 ok")

# ── AC4: git-work-tree ancestor (never mentions DOIT_ROOT), ancestor-only ──
unrelated_root4 = harness.test_root("ac4-unrelated-doit-root")  # not a git repo; unrelated to the rest
git_dir4a = harness.test_root("ac4-git-ancestor-dir")
make_git(git_dir4a, as_file=False)
target4a = git_dir4a / "level1" / "level2"  # nested two levels beneath the .git directory
set_env(DOIT_SCRATCH=str(target4a), DOIT_ROOT=str(unrelated_root4), HOME=str(harness.test_root("ac4-home-a")))
e4a = raises(scratch.ScratchRootError, scratch.root)
check(e4a is not None, "AC4a: root() must raise for a .git DIRECTORY ancestor")
msg4a = str(e4a)
check(".git" in msg4a or "git work tree" in msg4a, f"AC4a: message must name git, got {msg4a!r}")
check("DOIT_ROOT" not in msg4a, f"AC4a: message must not mention DOIT_ROOT, got {msg4a!r}")
check(not target4a.exists(), "AC4a: nothing partially created")

git_dir4b = harness.test_root("ac4-git-ancestor-file")
make_git(git_dir4b, as_file=True)  # worktree shape: .git is a FILE
target4b = git_dir4b / "level1" / "level2"
set_env(DOIT_SCRATCH=str(target4b), DOIT_ROOT=str(unrelated_root4), HOME=str(harness.test_root("ac4-home-b")))
e4b = raises(scratch.ScratchRootError, scratch.root)
check(e4b is not None, "AC4b: root() must raise for a .git FILE ancestor (worktree shape)")
msg4b = str(e4b)
check(".git" in msg4b or "git work tree" in msg4b, f"AC4b: message must name git, got {msg4b!r}")
check("DOIT_ROOT" not in msg4b, f"AC4b: message must not mention DOIT_ROOT, got {msg4b!r}")
check(not target4b.exists(), "AC4b: nothing partially created")

approved_root4c = harness.test_root("ac4-approved-root")  # not a git repo
set_env(DOIT_SCRATCH=str(approved_root4c), DOIT_ROOT=str(unrelated_root4), HOME=str(harness.test_root("ac4-home-c")))
r4c = scratch.root()
check(r4c == approved_root4c.resolve(), "AC4c: first root() call must succeed under this configuration")
gate_sub4c = scratch.sub("gate")
make_git(gate_sub4c, as_file=True)  # simulate a worktree cut later, nested under the approved root
r4c2 = scratch.root()  # identical DOIT_SCRATCH — must still succeed
check(r4c2 == r4c, "AC4c: a .git nested UNDER an approved root must not disqualify a later root() call")
print("AC4 ok")

# ── AC5: sub() name validation, no closed enum ──────────────────────────────
home5 = harness.test_root("ac5-home")
set_env(DOIT_SCRATCH=None, DOIT_ROOT=None, HOME=str(home5))
scratch.root()  # establish the root once

bad_names = ["", ".", "..", "a/b", "/etc/passwd"]
for bad in bad_names:
    before = {p.name for p in scratch.root().iterdir()}
    e = raises(ValueError, scratch.sub, bad)
    check(e is not None, f"AC5: sub({bad!r}) must raise ValueError")
    after = {p.name for p in scratch.root().iterdir()}
    check(before == after, f"AC5: sub({bad!r}) must create nothing (before={before}, after={after})")

good_names = ["grade", "gate", "clones", "pg", "grader-claude"]
for name in good_names:
    p = scratch.sub(name)
    check(p == scratch.root() / name and p.is_dir(), f"AC5: sub({name!r}) must create root()/{name!r}")
    p2 = scratch.sub(name)  # repeat call with the same name
    check(p2 == p and p.is_dir(), f"AC5: repeat sub({name!r}) must return the same existing directory")
print("AC5 ok")

# ── AC6: sub() delegates to root() — identical exception surfaces ──────────
root6 = harness.test_root("ac6-root")
set_env(DOIT_SCRATCH=str(root6), DOIT_ROOT=str(root6), HOME=str(harness.test_root("ac6-home")))
e6 = raises(scratch.ScratchRootError, scratch.sub, "grade")
check(e6 is not None, "AC6: sub() must raise ScratchRootError, identical to root(), not silently skip the check")
print("AC6 ok")

print(f"scratch: {N} checks pass")
