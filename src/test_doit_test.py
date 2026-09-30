#!/usr/bin/env python3
"""R8.1 AC1/AC2 — `doit test`'s own environment isolation, proven end to end
against a REAL `bash <copy-of-doit> test`, never the real repo's `./doit`.
Run: python3 test_doit_test.py

A throwaway tree — `<tree>/doit` (a byte-for-byte copy of THIS repo's `doit`)
and `<tree>/src/test_probe.py` (a one-file fixture, the only test the copy
ever runs) — is built fresh per case. The fixture writes what it saw straight
to `DOIT_TEST_PROBE_OUT`, a path the outer harness controls directly and
`doit test`'s isolation never touches (it only exports HOME/DOIT_ROOT/
XDG_CONFIG_HOME and unsets DOIT_LEDGER_FILE), so the report survives even
though the isolated tree itself is removed before this process ever looks at
it. `TMPDIR` is pointed at a fresh, otherwise-empty directory of our own for
each run, so `mktemp -d` inside the copied `doit` lands there and "the temp
dir is gone after" becomes a literal, checkable claim: is that TMPDIR empty
once the subprocess exits?
"""
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_DOIT = REPO_ROOT / "doit"

N = 0


def ok(cond, msg):
    global N
    assert cond, msg
    N += 1


PROBE_SRC = '''\
import json, os, pathlib, sys

out = pathlib.Path(os.environ["DOIT_TEST_PROBE_OUT"])
home = os.environ.get("HOME")
root = os.environ.get("DOIT_ROOT")
xdg = os.environ.get("XDG_CONFIG_HOME")

# the same shape of write a real test file makes — into the ISOLATED tree
# `doit test` set up, never the invoker's own sentinel HOME/DOIT_ROOT
agents_dir = pathlib.Path(home) / ".claude" / "agents"
agents_dir.mkdir(parents=True, exist_ok=True)
(agents_dir / "probe.marker").write_text("x")
seat_dir = pathlib.Path(root) / "seat"
seat_dir.mkdir(parents=True, exist_ok=True)
(seat_dir / "probe.marker").write_text("x")

out.write_text(json.dumps({
    "HOME": home, "DOIT_ROOT": root, "XDG_CONFIG_HOME": xdg,
    "DOIT_LEDGER_FILE_present": "DOIT_LEDGER_FILE" in os.environ,
}))

if os.environ.get("DOIT_TEST_PROBE_FAIL"):
    sys.exit(1)
'''


def _build_tree(tmp):
    """`<tmp>/doit` (copy of the real launcher) + `<tmp>/src/test_probe.py`
    (the fixture above) — the only test file the copy will ever see."""
    tree = pathlib.Path(tempfile.mkdtemp(dir=tmp))
    doit_copy = tree / "doit"
    doit_copy.write_text(REAL_DOIT.read_text())
    doit_copy.chmod(doit_copy.stat().st_mode | stat.S_IEXEC)
    (tree / "src").mkdir()
    (tree / "src" / "test_probe.py").write_text(PROBE_SRC)
    return tree


def _run_case(outer_tmp, *, fail=False):
    """One full `bash <tree>/doit test` under a scoped sentinel HOME/DOIT_ROOT/
    XDG_CONFIG_HOME/DOIT_LEDGER_FILE/TMPDIR. Returns
    `(rc, probe, sentinel_home, sentinel_root, sentinel_xdg, iso_tmpdir)`."""
    tree = _build_tree(outer_tmp)
    sentinel_home = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-home-"))
    sentinel_root = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-root-"))
    sentinel_xdg = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-xdg-"))
    iso_tmpdir = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="iso-tmpdir-"))
    probe_out = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="probe-out-")) / "probe.json"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(sentinel_home),
        "DOIT_ROOT": str(sentinel_root),
        "XDG_CONFIG_HOME": str(sentinel_xdg),
        "DOIT_LEDGER_FILE": "L-sentinel-0001.jsonl",
        "TMPDIR": str(iso_tmpdir),
        "DOIT_TEST_PROBE_OUT": str(probe_out),
    }
    if fail:
        env["DOIT_TEST_PROBE_FAIL"] = "1"
    proc = subprocess.run(["bash", str(tree / "doit"), "test"], cwd=str(tree),
                          env=env, capture_output=True, text=True, timeout=60)
    probe = json.loads(probe_out.read_text()) if probe_out.exists() else None
    return proc, probe, sentinel_home, sentinel_root, sentinel_xdg, iso_tmpdir


def _empty(d):
    return not any(pathlib.Path(d).rglob("*"))


# ══════════════════════════════════════════════════════════════════════════
# AC1 · a passing fixture: isolation replaces HOME/DOIT_ROOT/XDG_CONFIG_HOME,
# DOIT_LEDGER_FILE is unset in the child, and the isolated tmp dir is gone
# ══════════════════════════════════════════════════════════════════════════
OUTER1 = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-test-"))
proc1, probe1, shome1, sroot1, sxdg1, isotmp1 = _run_case(OUTER1, fail=False)
ok(proc1.returncode == 0, f"AC1: a passing fixture exits 0: rc={proc1.returncode} "
                         f"stdout={proc1.stdout!r} stderr={proc1.stderr!r}")
ok(probe1 is not None, f"AC1: the fixture reported its observed env: {proc1.stdout!r}")
ok(probe1["HOME"] != str(shome1) and probe1["DOIT_ROOT"] != str(sroot1)
   and probe1["XDG_CONFIG_HOME"] != str(sxdg1),
   f"AC1: none of the child's HOME/DOIT_ROOT/XDG_CONFIG_HOME equal the invoker's sentinels: {probe1}")
ok(probe1["HOME"] and probe1["DOIT_ROOT"] and probe1["XDG_CONFIG_HOME"],
   f"AC1: all three are still set (to something), just not the sentinels: {probe1}")
ok(probe1["DOIT_LEDGER_FILE_present"] is False,
   f"AC1: DOIT_LEDGER_FILE is unset before any runner starts: {probe1}")
ok(_empty(isotmp1), "AC1: the isolation temp dir (under our own TMPDIR) is removed after — nothing left behind")
print("AC1 ok")


# ══════════════════════════════════════════════════════════════════════════
# AC2 · nothing lands in the invoker's sentinel HOME/DOIT_ROOT, in either a
# passing or a failing run; a failing fixture still exits non-zero and the
# temp dir is still removed
# ══════════════════════════════════════════════════════════════════════════
ok(_empty(shome1) and _empty(sroot1),
   f"AC2: the invoker's sentinel HOME/DOIT_ROOT stay empty after a passing run: "
   f"{list(shome1.rglob('*'))} {list(sroot1.rglob('*'))}")

OUTER2 = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-test-fail-"))
proc2, probe2, shome2, sroot2, sxdg2, isotmp2 = _run_case(OUTER2, fail=True)
ok(proc2.returncode != 0, f"AC2: a failing fixture makes `doit test` exit non-zero: rc={proc2.returncode}")
ok(probe2 is not None and probe2["DOIT_LEDGER_FILE_present"] is False,
   f"AC2: the failing fixture still ran under full isolation before it failed: {probe2}")
ok(_empty(isotmp2), "AC2: the isolation temp dir is removed after a FAILING run too")
ok(_empty(shome2) and _empty(sroot2),
   f"AC2: the invoker's sentinel HOME/DOIT_ROOT stay empty after a failing run too: "
   f"{list(shome2.rglob('*'))} {list(sroot2.rglob('*'))}")
print("AC2 ok")


print(f"doit_test: {N} checks pass")
