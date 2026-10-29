"""Memoize the main tip for builder rebuilds caused by conflicts or releases."""
import os
import pathlib
import subprocess

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
_UNSET = object()


def refresh_tip(c):
    """Return a fresh base only for a pending conflict or dependency release.

    Ctx.evs is the subject's stream in ledger order. Only DONE consumes
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
        if kind == "build-done" and event.get("status") == "DONE":
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
        import packet
        c._base_refresh = packet._builder_pin(c)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass
    return c._base_refresh
