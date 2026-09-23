#!/usr/bin/env python3
"""Checks on the test harness itself: AC1-AC3 of L-spec-0183.

Run: python3 test_harness.py

The one thing this file must never do is prove the harness safe by trusting
the harness — every "must raise" case names a real, resolvable path (the
real HOME, the real default DOIT_ROOT, the bare OS tempdir, `/`, and a raw
`tempfile.mkdtemp()` made outside the harness) and confirms `cleanup()`
refuses every one of them without deleting anything.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harness  # noqa: E402

n = 0


def ok(cond, why):
    global n
    assert cond, why
    n += 1


# ── AC1: test_root() exists immediately, and distinct calls never collide ───
r1 = harness.test_root("alpha")
r2 = harness.test_root("alpha")  # same name, repeated call
r3 = harness.test_root("beta")
for r in (r1, r2, r3):
    ok(r.exists() and r.is_dir(), f"{r} must exist as a directory immediately after test_root()")
ok(len({r1, r2, r3}) == 3, f"pairwise-distinct even with a repeated name: {r1}, {r2}, {r3}")

# ── AC2: cleanup() on a harness path removes it and returns None ────────────
victim = harness.test_root("gamma")
marker = victim / "keep-me.txt"
marker.write_text("x")
result = harness.cleanup(victim)
ok(result is None, f"cleanup() returns None, got {result!r}")
ok(not victim.exists(), f"cleanup() must actually remove {victim}")

# ── AC3: cleanup() refuses these five, deletes none of them ──────────────────
real_home = pathlib.Path.home()
real_doit_root = pathlib.Path(os.environ.get("DOIT_ROOT", str(pathlib.Path.home() / ".do-it")))
tempdir_root = pathlib.Path(tempfile.gettempdir())
root_slash = pathlib.Path("/")
outside = pathlib.Path(tempfile.mkdtemp())  # raw mkdtemp, NOT from the harness

cases = [
    ("real HOME", real_home),
    ("real default DOIT_ROOT", real_doit_root),
    ("bare OS tempdir", tempdir_root),
    ("/", root_slash),
    ("outside raw mkdtemp", outside),
]

for label, p in cases:
    existed_before = p.exists()
    raised = False
    try:
        harness.cleanup(p)
    except harness.NotHarnessPath:
        raised = True
    ok(raised, f"cleanup() must raise NotHarnessPath for {label} ({p}), it did not")
    ok(p.exists() == existed_before, f"cleanup() must not touch {label} ({p}) — it still must exist")

# clean up our own outside-the-harness fixture by hand; harness.cleanup()
# correctly refuses it, so this file is responsible for its own litter.
if outside.exists():
    import shutil
    shutil.rmtree(outside)

harness.cleanup(r1)
harness.cleanup(r2)
harness.cleanup(r3)

print(f"harness: {n} checks pass")
