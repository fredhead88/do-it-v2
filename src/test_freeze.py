#!/usr/bin/env python3
"""One runnable check on freeze.py's rules. Run: python3 test_freeze.py"""
import os
import pathlib
import subprocess
import sys
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import freeze  # noqa: E402

# ── AC1 · freeze.FLAG resolves under DOIT_ROOT when set, else the historical
# default; the literal path string lives in exactly one non-test file ───────
assert str(freeze.FLAG) == str(TMP / "state" / "master-frozen-for-codex"), freeze.FLAG
_home_default = pathlib.Path.home() / ".do-it" / "state" / "master-frozen-for-codex"
_env = dict(os.environ)
_env.pop("DOIT_ROOT", None)
here = pathlib.Path(__file__).parent
out_default = subprocess.run(
    [sys.executable, "-c", "import freeze; print(freeze.FLAG)"],
    cwd=here, env=_env, capture_output=True, text=True, check=True,
).stdout.strip()
assert out_default == str(_home_default), (out_default, _home_default)
_env2 = dict(_env)
_env2["DOIT_ROOT"] = "/tmp/x"
out_root = subprocess.run(
    [sys.executable, "-c", "import freeze; print(freeze.FLAG)"],
    cwd=here, env=_env2, capture_output=True, text=True, check=True,
).stdout.strip()
assert out_root == str(pathlib.Path("/tmp/x") / "state" / "master-frozen-for-codex"), out_root
grep = subprocess.run(
    ["grep", "-rl", "master-frozen-for-codex", "freeze.py", "merge_gate.py", "fold.py"],
    cwd=here, capture_output=True, text=True,
).stdout.split()
assert grep == ["freeze.py"], grep
print("AC1 ok")

# ── AC2 · state() returns None on a nonexistent flag, whether passed
# explicitly or via freeze.FLAG itself ───────────────────────────────────────
assert freeze.state(TMP / "absent") is None
freeze.FLAG = TMP / "also-absent"
assert freeze.state() is None
print("AC2 ok")

# ── AC3 · exact key set, stripped text, no cap token -> cap is None ─────────
f3 = TMP / "flag3"
f3.write_text("  frozen for Codex #445  \n")
r3 = freeze.state(f3)
assert set(r3) == {"text", "since", "cap"}, r3
assert r3["text"] == "frozen for Codex #445", r3
assert r3["cap"] is None, r3
print("AC3 ok")

# ── AC4 · cap parsing: HH:MMZ, full ISO, and no-token ────────────────────────
f4a = TMP / "flag4a"
f4a.write_text("frozen — cap 18:00Z, ping the Thinker")
assert freeze.state(f4a)["cap"] == "18:00Z", freeze.state(f4a)

f4b = TMP / "flag4b"
f4b.write_text("frozen, cap 2026-09-28T02:00:00Z")
assert freeze.state(f4b)["cap"] == "2026-09-28T02:00:00Z", freeze.state(f4b)

f4c = TMP / "flag4c"
f4c.write_text("frozen for Codex #445")
assert freeze.state(f4c)["cap"] is None, freeze.state(f4c)
print("AC4 ok")

print(f"freeze: all checks pass")
