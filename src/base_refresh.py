"""Memoize the main tip for builder rebuilds caused by conflicts or releases."""
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
_UNSET = object()


def refresh_tip(c):
    """Return a fresh base only for a pending conflict or dependency release.

    Ctx.evs is the subject's stream in ledger order. A completed build consumes
    earlier triggers, while a dependency hold may precede that completed build.
    Cache failures and non-refresh decisions too, so every packet pin agrees.
    """
    cached = getattr(c, "_base_refresh", _UNSET)
    if cached is not _UNSET:
        return cached
    c._base_refresh = None
    if getattr(c.a, "role", None) != "builder":
        return None

    held, due = False, False
    for event in getattr(c, "evs", None) or []:
        kind = event.get("type")
        if kind == "build-done":
            due = False
        elif kind == "dependency-hold":
            held = True
        elif kind == "conflict-rework" or (kind == "unblocked" and held):
            due = True
        elif kind == "spawn-failed" and str(event.get("reason", "")).startswith("wave-order"):
            due = True
        elif kind == "decision" and event.get("rebase") == "yes":
            due = True
    if not due:
        return None

    try:
        import look
        project = c.project()
        main = look.load_toml()["push"]["main"].get(project, "master")
        if not isinstance(main, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", main):
            return None
        repo = getattr(c.a, "repo", None) or str(ROOT / "repos" / project)
        result = subprocess.run(["git", "-C", repo, "rev-parse", main],
                                capture_output=True, text=True, timeout=10)
        tip = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", tip):
            c._base_refresh = tip
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass
    return c._base_refresh
