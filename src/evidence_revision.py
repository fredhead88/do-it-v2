"""Evidence identity and amendment freshness shared by runner and grader."""
import hashlib
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
