#!/usr/bin/env python3
"""scratch — one shared, home-based scratch root (L-spec-0425, SD15).

Written after the 2026-09-27 incident (probe Q3/Q4, `content/probe-L-charter-0042/`):
`/tmp` is a quota-limited tmpfs with no readable limit, and two orphaned git
clones plus two live private Postgres clusters pushed uid 1000 from 68% to 80%
in ten minutes before a write failed with "Disk quota exceeded". `$HOME` sits on
the same filesystem with far more headroom, so `root()` defaults there instead.

`root()` also refuses (raises, creates nothing) when the resolved scratch root
would sit at or under `$DOIT_ROOT` — the ledger snapshot cron commits to that
tree, and probe Q3 found that masking a nested `.git` does not stop git's own
parent-directory discovery walk from reaching it — or at or under any existing
git work tree. Both checks are independent: in production `$DOIT_ROOT` is
itself a git repo, so they typically fire together, but each is evaluated (and
each raises its own message) on its own.

`DOIT_ROOT` is read the SAME `os.environ.get(...) or default` way
`src/intake.py:31` and `src/carry.py:86` already read it — never the
two-argument `os.environ.get(key, default)` form `src/dispatch.py:33` and
`src/reap_tmp.py:44` use for the identical variable, which a set-but-empty
value would silently bypass. `DOIT_SCRATCH` follows the same `or`-based
convention here for the same reason: `DOIT_SCRATCH=""` (set but empty) must be
treated exactly like unset, not as an explicit empty override. Do not "fix"
either `or` back to a two-argument `.get()` — that would reintroduce the
set-but-empty bypass this module is written to avoid.

The git-work-tree check is a pure filesystem scan (existence of a `.git` entry,
directory or file, at each ancestor level) — never a `git` subprocess call, so
this module carries no dependency on the `git` binary and behaves identically
whether or not one is installed.
"""
import os
import pathlib


class ScratchRootError(Exception):
    """Raised by root() — and by sub(), which delegates to root() rather than
    re-deriving anything, so the identical exception surfaces through both —
    when the resolved scratch root would sit at or under DOIT_ROOT, or at or
    under an existing git work tree. Nothing is created when this raises."""


def _doit_root():
    """The existing project-wide DOIT_ROOT convention, `or`-based (see module
    docstring), expanded and fully resolved (symlinks included) BEFORE any
    containment comparison runs — so a DOIT_ROOT reached through a symlink
    cannot be evaded by addressing its target directly."""
    raw = os.environ.get("DOIT_ROOT") or (pathlib.Path.home() / ".do-it")
    return pathlib.Path(raw).expanduser().resolve()


def _has_git_entry(path):
    """True if path/.git exists as either a directory (normal checkout) or a
    file (the shape `git worktree add` leaves behind). No git subprocess."""
    git = path / ".git"
    return git.is_dir() or git.is_file()


def _git_ancestor(resolved):
    """Walk upward from `resolved` (inclusive) through its ancestors, testing
    at each level for a `.git` entry. Looks only upward: a `.git` a caller
    creates later, nested underneath an already-approved root, must never
    disqualify a later call — only an ancestor may."""
    p = resolved
    while True:
        if _has_git_entry(p):
            return True
        parent = p.parent
        if parent == p:
            return False
        p = parent


def root():
    """`$DOIT_SCRATCH` (or-based; `""` treated as unset), default
    `$HOME/doit-scratch`, expanded and fully resolved (symlinks included) and
    created (parents included) before returning. Raises ScratchRootError and
    creates nothing when the resolved path is `$DOIT_ROOT` itself or a
    descendant of it, or when it sits at or under an existing git work tree.
    Idempotent: a repeat call returns the identical existing directory."""
    raw = os.environ.get("DOIT_SCRATCH") or str(pathlib.Path.home() / "doit-scratch")
    resolved = pathlib.Path(raw).expanduser().resolve()

    doit_root = _doit_root()
    if resolved == doit_root or doit_root in resolved.parents:
        raise ScratchRootError(
            f"scratch root {resolved} sits at or under DOIT_ROOT {doit_root} "
            "— refusing to create it there (the ledger snapshot/restic tree)"
        )

    if _git_ancestor(resolved):
        raise ScratchRootError(
            f"scratch root {resolved} sits at or under an existing git work tree "
            "— refusing to create it there (probe Q3's history-leak)"
        )

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def sub(name):
    """A created subdirectory of root() named `name`. Calls root() itself for
    the base — never independently re-derives DOIT_SCRATCH/DOIT_ROOT — so an
    invalid scratch-root configuration raises the identical ScratchRootError
    through here too. `name` must be one bare path component: raises
    ValueError and creates nothing when `name` is empty, is exactly "." or
    "..", or contains a path separator (os.sep, and os.altsep when the
    platform defines one). No closed set — any bare component satisfying this
    shape is accepted. Idempotent: a repeat call with the same valid name
    returns the same existing directory."""
    if (
        not name
        or name in (".", "..")
        or os.sep in name
        or (os.altsep and os.altsep in name)
    ):
        raise ValueError(
            f"scratch.sub(name) requires one bare path component, got {name!r}"
        )
    base = root()
    target = base / name
    target.mkdir(parents=True, exist_ok=True)
    return target
