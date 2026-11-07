"""Evidence identity and amendment freshness shared by runner and grader."""
import hashlib
import json


import uuid
from datetime import datetime, timezone


def _time(value):
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date


def amendment(events, subject):
    return max((_time(e['ts']) for e in events if e.get('subject') == subject
                and e.get('type') == 'spec-amended'), default=None)


def current(events, subject, event):
    changed = amendment(events, subject)
    return changed is None or bool(event.get('ts') and _time(event['ts']) > changed)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def cache_key(text, events, subject, build):
    # Even identical amendment text and a repeated ready SHA need fresh evidence.
    return digest(digest(text) + str(amendment(events, subject)) + str(build.get('ts', '')))[:16]


def bundle_key(text, events, subject, sha, base):
    """Stable identity excluding build time; readers enforce build freshness."""
    return digest(json.dumps([subject, digest(text), str(amendment(events, subject)), sha, base]))


def new_invocation_id() -> str:
    return uuid.uuid4().hex


def stamp(event: dict, invocation_id: str) -> dict:
    return {**event, "invocation_id": invocation_id}


def latest(events: list, subject: str, sha: str) -> dict | None:
    """Newest success or failure; identified rows win timestamp ties, then order."""
    candidates = []
    for index, event in enumerate(events):
        if (event.get("subject") != subject or event.get("sha") != sha
                or event.get("type") not in {"evidence-bundle", "evidence-bundle-failed"}):
            continue
        bundle = event.get("bundle") or {}
        invocation = event.get("invocation_id") or bundle.get("invocation_id")
        candidates.append((_time(event["ts"]), bool(invocation), index,
                           stamp(event, invocation)))
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None
