#!/usr/bin/env python3
"""harness — a place to create a test root that can never be the real thing.

Written after the 2026-09-22 ~04:28Z incident (see
`scripts/hooks/destructive-delete-guard.py`'s own docstring): a test that
sandboxes `DOIT_ROOT`/`HOME` with a bare `tempfile.mkdtemp()` call still has
to clean up after itself, and a `cleanup()` that trusts its own argument is
one keystroke away from the exact command that wiped `/home/albert`.

`test_root(name)` hands out directories under ONE base this module creates
once per process, itself an ordinary `tempfile.mkdtemp()` result and never the
real `$HOME` or `$DOIT_ROOT`. `cleanup(path)` refuses anything not under that
base — not `$HOME`, not the real `$DOIT_ROOT`, not the bare OS tempdir root,
not `/`, and not a directory some OTHER call made with its own raw
`tempfile.mkdtemp()` — by construction, not by a denylist: those five never
sit inside a directory this process just created.
"""
import pathlib
import shutil
import tempfile

_BASE = None


class NotHarnessPath(Exception):
    """Raised by cleanup() on any path that is not under this process's own
    harness base — the refusal that keeps a typo from reaching a real root."""


def _base():
    global _BASE
    if _BASE is None:
        _BASE = pathlib.Path(tempfile.mkdtemp(prefix="doit-harness-"))
    return _BASE


def test_root(name):
    """A fresh directory under the harness base, named after `name` but never
    colliding with a prior call — including a repeat call with the same
    `name` — because `tempfile.mkdtemp` itself guarantees the suffix is
    unique. Exists as a directory immediately on return."""
    base = _base()
    return pathlib.Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(base)))


def cleanup(path):
    """Remove a directory `test_root()` returned. Raises `NotHarnessPath` and
    deletes nothing when `path` is not strictly under the harness base for
    this process — that covers the real `$HOME`, the real `$DOIT_ROOT`, the
    bare OS tempdir, `/`, and a directory from some other, non-harness
    `tempfile.mkdtemp()` call, none of which can ever resolve to a
    descendant of a directory this module only just created."""
    base = _base().resolve()
    p = pathlib.Path(path).resolve()
    if p == base:
        raise NotHarnessPath(f"{p} is the harness base itself, not a test_root() result")
    try:
        p.relative_to(base)
    except ValueError:
        raise NotHarnessPath(f"{p} is not under the harness base {base}") from None
    shutil.rmtree(p)
