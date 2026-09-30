#!/usr/bin/env python3
"""R8.2 AC4 — a static proof, not a runtime one: in each of the 12 test files
that set no `DOIT_ROOT` of their own (test_contracts, test_ghlimit,
test_look_wallclock, test_owed, test_packet_lint, test_paid_call,
test_pane_resume, test_panes, test_plain, test_shape, test_usage,
test_validate), the assignment of BOTH `os.environ["HOME"]` and
`os.environ["DOIT_ROOT"]` must precede the first `import`/`from ... import`
statement naming a project module (any module whose name is a `src/*.py`
stem) — a direct `python3 test_x.py` must never resolve `fold.ROOT` (or any
other module state keyed off HOME/DOIT_ROOT at import time) against the real
environment. Run: python3 test_isolation_static.py

An ast scan, not a byte-offset regex: `ast.walk` finds every `Assign` and
`Import`/`ImportFrom` node in the whole tree (module level or nested — "the
first import of any project module" is read literally, with no exemption for
one hidden inside a function or an `if`), then each is ordered by its own
1-based `lineno` for a deterministic, source-order comparison. AC4's own
negative case — a fixture file with the import first — is exercised below
against the same `scan()` this file runs over the real 12, so the checker is
proven to fail loud, not just pass quiet.
"""
import ast, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent

FILES = ("test_contracts", "test_ghlimit", "test_look_wallclock", "test_owed",
         "test_packet_lint", "test_paid_call", "test_pane_resume", "test_panes",
         "test_plain", "test_shape", "test_usage", "test_validate")

PROJECT_STEMS = {p.stem for p in HERE.glob("*.py")}

N = 0


def ok(cond, msg):
    global N
    assert cond, msg
    N += 1


def _env_keys_assigned(node):
    """Yield "HOME"/"DOIT_ROOT" for an `os.environ["HOME"] = ...`-shaped
    assignment: a plain `Assign` whose target is a `Subscript` on `os.environ`
    with a string-literal key. `AugAssign` and anything indirected through a
    helper function are deliberately NOT matched — this file's own 12 edits
    all use the plain literal form, and a scan that also had to chase helper
    indirection would be proving something it can't actually see."""
    if not isinstance(node, ast.Assign):
        return
    for target in node.targets:
        if not isinstance(target, ast.Subscript):
            continue
        val = target.value
        if not (isinstance(val, ast.Attribute) and val.attr == "environ"
                and isinstance(val.value, ast.Name) and val.value.id == "os"):
            continue
        key_node = target.slice
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            yield key_node.value


def _project_import_names(node):
    """Yield every top-level dotted-first-component name a plain
    `import a.b`/`import a as x`/`from a import b` statement names — the
    piece that must match a `src/*.py` stem for this to count as a project
    import."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name.split(".")[0]
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.level == 0:
            yield node.module.split(".")[0]


def scan(source, project_stems):
    """Parse SOURCE; return `(env_line, import_line)`:
    - `env_line`: the 1-based line of the statement that completes setting
      BOTH `os.environ["HOME"]` and `os.environ["DOIT_ROOT"]` (the later of
      the two, since both must be set) — `None` if the file never sets both.
    - `import_line`: the 1-based line of the first import naming a module in
      `project_stems` — `None` if there is none.
    `ast.walk` is not itself line-ordered, so every matching node is
    collected first and then the two lines are taken as the min/first over a
    lineno-sorted pass — deterministic regardless of `ast.walk`'s own
    (BFS, parent-then-children) traversal order."""
    tree = ast.parse(source)
    nodes = sorted((n for n in ast.walk(tree) if hasattr(n, "lineno")),
                   key=lambda n: n.lineno)
    assigned, env_line, import_line = set(), None, None
    for node in nodes:
        for key in _env_keys_assigned(node):
            assigned.add(key)
        if env_line is None and {"HOME", "DOIT_ROOT"} <= assigned:
            env_line = node.lineno
        if import_line is None:
            for name in _project_import_names(node):
                if name in project_stems:
                    import_line = node.lineno
                    break
    return env_line, import_line


# ══════════════════════════════════════════════════════════════════════════
# AC4 · the real 12 files: env assignment strictly precedes the first
# project import, in every one
# ══════════════════════════════════════════════════════════════════════════
for name in FILES:
    path = HERE / f"{name}.py"
    ok(path.is_file(), f"AC4: {name}.py exists in {HERE}")
    env_line, import_line = scan(path.read_text(), PROJECT_STEMS)
    ok(env_line is not None,
       f"AC4: {name}.py never sets BOTH os.environ['HOME'] and os.environ['DOIT_ROOT']")
    print(f"· {name}.py — env_line={env_line} first_project_import_line={import_line}")
    if import_line is not None:
        ok(env_line < import_line,
           f"AC4: {name}.py imports a project module (line {import_line}) "
           f"before its env assignment completes (line {env_line})")
print("AC4 (real files) ok")


# ══════════════════════════════════════════════════════════════════════════
# AC4 · negative case: a fixture file with the import FIRST is reported as
# failing — the checker actually detects a violation, not just a vacuous pass
# ══════════════════════════════════════════════════════════════════════════
FIXTURE_BACKWARDS = """\
import os, sys
import fold  # a project import, deliberately BEFORE the env assignment below

os.environ["HOME"] = "/tmp/whatever-home"
os.environ["DOIT_ROOT"] = "/tmp/whatever-root"
"""
fenv, fimp = scan(FIXTURE_BACKWARDS, PROJECT_STEMS | {"fold"})
ok(fimp is not None and fenv is not None and fimp < fenv,
   f"AC4 negative: the backwards fixture is caught — import at {fimp}, env at {fenv}")

FIXTURE_CORRECT = """\
import os, sys

os.environ["HOME"] = "/tmp/whatever-home"
os.environ["DOIT_ROOT"] = "/tmp/whatever-root"

import fold  # after the env assignment: correct order
"""
cenv, cimp = scan(FIXTURE_CORRECT, PROJECT_STEMS | {"fold"})
ok(cenv is not None and cimp is not None and cenv < cimp,
   f"AC4 negative: a corrected fixture (same content, right order) passes — env at {cenv}, import at {cimp}")

FIXTURE_ONLY_HOME = """\
import os
os.environ["HOME"] = "/tmp/only-home"
import fold
"""
oenv, oimp = scan(FIXTURE_ONLY_HOME, PROJECT_STEMS | {"fold"})
ok(oenv is None and oimp is not None,
   f"AC4 negative: setting HOME alone (never DOIT_ROOT) never satisfies env_line: {(oenv, oimp)}")
print("AC4 (fixture negative case) ok")


# a fixture file dropped to disk and re-read, proving `scan()` works on real
# file text/paths exactly as it does on the inline string fixtures above
with tempfile.TemporaryDirectory() as td:
    fpath = pathlib.Path(td) / "test_fixture_backwards.py"
    fpath.write_text(FIXTURE_BACKWARDS)
    denv, dimp = scan(fpath.read_text(), PROJECT_STEMS | {"fold"})
    ok(dimp is not None and denv is not None and dimp < denv,
       "AC4 negative: the same check, run against a real file on disk, still catches it")
print("AC4 (on-disk fixture) ok")

print(f"isolation_static: {N} checks pass")
