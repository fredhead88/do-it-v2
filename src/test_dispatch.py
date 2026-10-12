#!/usr/bin/env python3
"""Run dispatch regressions and the ordered historical fixture chunks."""
from pathlib import Path
import subprocess, sys

def test_dispatch_0898():
    _checks = Path(__file__).with_name("dispatch_checks_3.py")
    _new = _checks.read_text().split("# END L0898 REGRESSIONS\n", 1)[0]
    # Separate processes keep module-level ledger roots hermetic during collection.
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, " +
                    repr(str(_checks.parent)) + "); " + _new + "\ncheck_dispatch_0898()"], check=True)

_legacy = r"""
from pathlib import Path
_historical = Path(__file__).with_name("dispatch_checks_3.py").read_text().split("# END L0898 REGRESSIONS\n", 1)[1]
for _chunk in range(1, 4):
    _path = Path(__file__).with_name(f"dispatch_checks_{_chunk}.py")
    _source = _historical if _chunk == 3 else _path.read_text()
    # Historical fake builder checkouts lack per-subject branches; the new
    # regressions above prove the worktree guard using real checkouts.
    _source = _source.replace("import dispatch, fold, grader_view, harness  # noqa: E402",
        "import dispatch, fold, grader_view, harness\n"
        "dispatch.builder_worktree_preflight = lambda *args: None")
    _source = _source.replace("importlib.reload(dispatch)",
        "importlib.reload(dispatch)\ndispatch.builder_worktree_preflight = lambda *args: None")
    exec(compile(_source, str(_path), "exec"), globals())
"""
if __name__ == "__main__":
    test_dispatch_0898()
    subprocess.run([sys.executable, "-c", "__file__ = " + repr(__file__) + "\n" + _legacy], check=True)
