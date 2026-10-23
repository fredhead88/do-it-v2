#!/usr/bin/env python3
"""`doit test`'s environment and content isolation, proven end to end
against a REAL `bash <copy-of-doit> test`, never the real repo's `./doit`.
Run: python3 test_doit_test.py

A throwaway tree — `<tree>/doit` (a byte-for-byte copy of THIS repo's `doit`)
and one or more fixture test files under `<tree>/src/` — is built fresh per
case. Each fixture writes what it saw straight
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


CONTENT_PROBE_SRC = '''\
import json, os, pathlib
out = pathlib.Path(os.environ["DOIT_TEST_PROBE_OUT"]).parent
name = pathlib.Path(__file__).stem
(out / (name + ".json")).write_text(json.dumps({
    "fixture": name, "HOME": os.environ["HOME"],
    "DOIT_ROOT": os.environ["DOIT_ROOT"],
    "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
}))
content = pathlib.Path(os.environ["DOIT_ROOT"]) / "content"
content.mkdir(parents=True, exist_ok=True)
(content / "L-charter-9999.md").write_text(name)
'''
CONTENT_FIXTURES = {"test_content_a.py", "test_content_b.py"}
ISOLATION_EXPORTS = ('           export HOME="$DOIT_TEST_ISO/home" DOIT_ROOT="$DOIT_TEST_ISO/root" \\\n'
                     '                  XDG_CONFIG_HOME="$DOIT_TEST_ISO/xdg"\n')


def _build_tree(tmp, *, content=False, isolated=True):
    """Copy the real launcher with either one env probe or two content probes."""
    tree = pathlib.Path(tempfile.mkdtemp(dir=tmp))
    doit_copy = tree / "doit"
    shutil.copyfile(REAL_DOIT, doit_copy)
    assert doit_copy.read_bytes() == REAL_DOIT.read_bytes()
    if not isolated:
        source = doit_copy.read_text()
        assert source.count(ISOLATION_EXPORTS) == 1, "negative control must remove the exports"
        doit_copy.write_text(source.replace(ISOLATION_EXPORTS, ""))
    doit_copy.chmod(doit_copy.stat().st_mode | stat.S_IEXEC)
    (tree / "src").mkdir()
    # The copied launcher's retirement gate needs its own tracked-file registry.
    shutil.copyfile(REPO_ROOT / "src" / "retired.py", tree / "src" / "retired.py")
    # Supply the machine gate and its real import dependencies in this checkout.
    for module in (REPO_ROOT / "src").glob("*.py"):
        if not module.name.startswith("test_"):
            shutil.copyfile(module, tree / "src" / module.name)
    (tree / "retired.d").mkdir()
    (tree / "retired.d" / "fixture.toml").write_text(
        '[[retired]]\nname="fixture-clean"\nkind="code"\n'
        'pattern="removed fixture mechanism"\npaths=["src/test_*.py"]\n')
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    sources = ({name: CONTENT_PROBE_SRC for name in CONTENT_FIXTURES} if content
               else {"test_probe.py": PROBE_SRC})
    for name, source in sources.items():
        (tree / "src" / name).write_text(source)
    subprocess.run(["git", "-C", str(tree), "add", "src", "retired.d"], check=True)
    return tree


def _run_case(outer_tmp, *, fail=False, content=False, isolated=True):
    """One full `bash <tree>/doit test` under a scoped sentinel HOME/DOIT_ROOT/
    XDG_CONFIG_HOME/DOIT_LEDGER_FILE/TMPDIR. Returns
    `(proc, probe, sentinel_home, sentinel_root, sentinel_xdg, iso_tmpdir)`,
    plus the before snapshot for content cases."""
    tree = _build_tree(outer_tmp, content=content, isolated=isolated)
    sentinel_home = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-home-"))
    sentinel_root = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-root-"))
    sentinel_xdg = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="sentinel-xdg-"))
    iso_tmpdir = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="iso-tmpdir-"))
    probe_out = pathlib.Path(tempfile.mkdtemp(dir=outer_tmp, prefix="probe-out-")) / "probe.json"
    if content:
        (sentinel_root / "content").mkdir()
        (sentinel_root / "content" / "marker").write_bytes(b"sentinel content\x00\xff\n")
        before = _content_snapshot(sentinel_root)

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
    if content:
        probe = [json.loads(p.read_text()) for p in sorted(probe_out.parent.glob("*.json"))]
    else:
        probe = json.loads(probe_out.read_text()) if probe_out.exists() else None
    result = proc, probe, sentinel_home, sentinel_root, sentinel_xdg, iso_tmpdir
    return (*result, before) if content else result


def _empty(d):
    return not any(pathlib.Path(d).rglob("*"))


def _content_snapshot(root):
    return {p.name: p.read_bytes() for p in (root / "content").iterdir()}


def _content_violations(result):
    """The identical guard checks both the real launcher and the negative control."""
    proc, reports, home, root, xdg, tmp, before = result
    violations = []
    if proc.returncode:
        violations.append(f"fixture runner failed: {proc.stdout} {proc.stderr}")
    if (len(reports) != len(CONTENT_FIXTURES) or
            {r["fixture"] + ".py" for r in reports} != CONTENT_FIXTURES):
        violations.append("not every fixture reported")
    for report in reports:
        paths = [pathlib.Path(report[key]) for key in ("HOME", "DOIT_ROOT", "XDG_CONFIG_HOME")]
        if any(p.resolve() == sentinel.resolve() for p, sentinel in zip(paths, (home, root, xdg))):
            violations.append("fixture saw sentinel roots")
        # resolve() rejects lexical parent escapes; all roots must be siblings
        # under the ONE directory created by the launcher's mktemp, inside TMPDIR.
        parents = {p.resolve().parent for p in paths}
        if (len(set(paths)) != 3 or len(parents) != 1 or
                not all(p.is_absolute() and p.resolve().parent.parent == tmp.resolve()
                        for p in paths)):
            violations.append("fixture roots escape launcher temp directory or overlap")
        if any(p.exists() for p in paths) or any(p.exists() for p in parents):
            violations.append("throwaway roots remain")
    if _content_snapshot(root) != before:
        violations.append("sentinel content changed")
    if not _empty(tmp):
        violations.append("launcher temp directory remains")
    return violations


def test_multifile_content_isolation():
    with tempfile.TemporaryDirectory(prefix="doit-test-content-") as outer:
        isolated = _run_case(outer, content=True)
        violations = _content_violations(isolated)
        ok(not violations, f"AC5 multi-file guard: {violations}")
        print(f"AC5 multi-file guard: {len(isolated[1])} fixtures reported; "
              "HOME/DOIT_ROOT isolated; sentinel content byte-identical; temp roots gone")
        unisolated = _run_case(outer, content=True, isolated=False)
        violations = _content_violations(unisolated)
        ok(unisolated[0].returncode == 0 and len(unisolated[1]) == len(CONTENT_FIXTURES),
           "AC6 negative control must run every fixture successfully")
        ok("fixture saw sentinel roots" in violations and "sentinel content changed" in violations,
           f"AC6 guard must detect the unisolated content write: {violations}")
        print(f"AC6 negative control detected: {', '.join(sorted(set(violations)))}")


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


if __name__ == "__main__":
    test_multifile_content_isolation()
print(f"doit_test: {N} checks pass")
