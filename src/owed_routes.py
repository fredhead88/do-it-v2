#!/usr/bin/env python3
"""Capability routes and readiness, without I/O or project imports."""
from datetime import datetime, timedelta
import re

ROUTES = {"ro-dsn": "plain", "not-live": "plain", "droplet": "plain",
          "elapsed": "plain", "repo": "repo", "browser": "browser",
          "operator-action": None, "ro-dsn-grant": None}
MAX_ATTEMPTS = 2


def route_from_record(record):
    """Route an explicit source without guessing from prose."""
    if record is None:
        return None
    return {"production": "ro-dsn", "scratch": "repo"}.get(record.data_source)


def classify_ask(note, capability=None):
    """Classify the act required, not the privileges of the reporting seat.

    Unknown/refused commands belong to the Thinker for triage. Only positive
    evidence of an exclusive human act goes to the operator. This does not
    grant a runner permission to execute anything.
    """
    text = ' '.join(str(note or '').lower().split())
    if re.search(r'operator(?:\W+\w+){0,3}\W+ruling|written operator ruling|decision, not an observable', text):
        return dict(owner='operator', kind='decision', capability='operator-action')
    approval = re.search(r'operator-gated|operator cron install|operator re-enables|operator-run write|'
                         r'flag toggle is a production change|sudo unshare.*live|tmpfs over the live|'
                         r'approve|approval|client email|purchase', text)
    requested = re.split(r'(?:operator|you)\s*:', text)[-1]
    tooling = re.search(r'\b(?:batch|actions[: ]read|dsn|ro-dsn|browser|chrome-devtools|'
                        r'dev server|review account|review session|verifier session|staff login|deploy time)\b',
                        requested) and not approval
    privilege = re.search(r'\bgrant (?:select|insert|update|delete|usage) on \S+', requested)
    # Tooling falls through to the route rules; a named privilege stays access.
    # A denied SELECT proves only that this reporting seat cannot read it.
    # The Thinker may already have an approved route; do not demand a new grant.
    if (privilege or not tooling and (
            re.search(r'(?:operator|you)\s*:\s*(?:please\s+)?(?:give|grant|provide)\b', text)
            and not re.search(r'grant a batch|\bno (?:ro[ -]*dsn|review account)\b|'
                              r'grant (?:a |one |the )?(?:read-only )?dsn|'
                              r'provide the review account|no browser.*here', text)
            or 'authorized scratch supavisor' in text)):
        return dict(owner='operator', kind='access', capability='ro-dsn-grant')
    if approval:
        kind = 'real-world' if re.search(r'client email|purchase', text) else 'approval'
        return dict(owner='operator', kind=kind, capability='operator-action')
    if re.search(r'not (?:yet )?deployed|not live|needs (?:a |integrator )?deploy|deploy has not run', text):
        route = 'not-live'
    elif re.search(r'elapsed|not yet due|next night|window cannot be complete|no deploy is in progress', text):
        route = 'elapsed'
    elif re.search(r'browser|review account|staff.key|authenticated|review credentials', text):
        route = 'browser'
    elif re.search(r'ro.dsn|ro.sql|database|dsn', text):
        route = 'ro-dsn'
    elif re.search(r'droplet|ssh', text):
        route = 'droplet'
    else:
        route = 'repo'
    return dict(owner='thinker', kind='follow-up', capability=route)


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
