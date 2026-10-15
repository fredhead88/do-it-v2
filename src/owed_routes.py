#!/usr/bin/env python3
"""Capability routes and readiness, without I/O or project imports."""
from datetime import datetime, timedelta

ROUTES = {"ro-dsn": "plain", "not-live": "plain", "droplet": "plain",
          "elapsed": "plain", "repo": "repo", "browser": "browser",
          "operator-action": None}
MAX_ATTEMPTS = 2


def route_for(capability):
    """Return the capability key, including the legacy `other` spelling."""
    capability = "repo" if capability == "other" else capability
    return capability if capability in ROUTES else None


def from_grader(grader_capability):
    return {"browser": "browser", "db": "ro-dsn", "deploy": "not-live"}.get(grader_capability, "repo")


def _instant(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def ready(row, now, epochs):
    """Whether the row's capability can be retried against these epochs."""
    capability = route_for(row.get("capability"))
    if capability in ("repo", "browser"):
        return True
    if capability == "droplet":
        epoch = epochs.get("droplet-wrapper")
        return epoch is not None and epoch != row.get("last_route_epoch")
    if capability in ("ro-dsn", "not-live", "elapsed"):
        last = _instant(row.get("last_unobservable_at"))
        if last is None:
            return False
        try:
            if capability == "elapsed":
                return now - last >= timedelta(hours=12)
            deploy = _instant(epochs.get("deploy"))
            return deploy is not None and deploy > last
        except TypeError:  # A timestamp without a compatible timezone cannot prove readiness.
            return False
    return False
