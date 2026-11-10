#!/usr/bin/env python3
"""Capability routes and readiness; relabel proposes events without appending."""
from datetime import datetime, timedelta
import re

NEED_CAPABILITY = dict(zip(
    ('ro-dsn', 'repo', 'droplet', 'environment', 'credential', 'unobservable'),
    ('ro-dsn', 'repo', 'droplet', 'repo', 'operator-action', 'other')))
OPEN = frozenset(('waiting', 'due', 'blocked', 'failed', 'correcting', 'expired', 'quarantined'))


def criterion_text(row, spec_text):
    """Select only the governing criterion; never borrow another check's prose."""
    import grading_env
    return grading_env._criterion_blocks(str(spec_text or '')).get(row.get('criterion'), '')


def observation_text(text):
    """Remove repository review boilerplate before looking for a login."""
    return re.sub(r'log in as (?:a )?(?:repository reviewer|local checkout reader)\b'
                  r'(?!\s+at\s+https?://)', '', text, flags=re.I).lower()


def authenticated(text):
    text = observation_text(text)
    return bool(re.search(r'\blog in as\b|\bauthenticated\b|staff login|review account|'
                          r'review credentials|logged.in|sign in', text))


def anonymous(text):
    return bool(re.search(r'\banonymous\b.*\b(?:local|fixture)\b|'
                          r'\b(?:local|fixture)\b.*\banonymous\b', observation_text(text)))


def need(row, spec_text):
    """Evidence-based prerequisite label. A seat's missing tools prove no grant."""
    text = observation_text(criterion_text(row, spec_text))
    note = str(row.get('why') or row.get('ask') or '').lower()
    repo = bool(re.search(r'repository|\brepo\b|test output|saved playwright|saved.*(?:trace|screenshot)', text))
    auth = authenticated(text)
    if repo and auth:
        return 'unobservable'
    if re.search(r'historical.*(?:never recorded|absent|unavailable)|irretrievably|no longer exists', text):
        return 'unobservable'
    if re.search(r'not (?:yet )?deployed|not yet due|next.*window|elapsed', text + ' ' + note):
        return 'environment'
    credential = re.search(r'\b(?:missing|no|requires?|needs?)\s+(?:an?\s+|the\s+|approved\s+|review\s+)*'
                           r'(?:credentials?|review account|api token|api key)\b', text)
    if (credential
            or auth and re.search(r'no review account|missing.*(?:account|credentials)|credential.*missing', note)
            or re.search(r'\bgrant (?:select|usage|insert|update|delete) on\b|pg_read_all_stats', note)):
        return 'credential'
    if re.search(r'(?:missing|no|unavailable).*\b(?:node_modules|postgres|venv|chromium|browser executable)\b|'
                 r'\b(?:node_modules|postgres|venv)\b.*(?:missing|unavailable)', note):
        return 'environment'
    if repo:
        return 'repo'
    if re.search(r'read.only sql|production sql|ro.dsn|live_db|\bselect\b.*\bfrom\b', text):
        return 'ro-dsn'
    if re.search(r'\bdroplet\b|\bssh\b', text):
        return 'droplet'
    if auth or anonymous(text) or re.search(r'playwright|browser|\.spec\.ts|local postgres', text):
        return 'environment'
    # A legacy note can name an observation when its spec is unavailable; a
    # generic refusal or old capability label alone cannot establish one.
    if not text:
        if re.search(r'\brepository|\brepo\b|\btests?\b|checkout', note):
            return 'repo'
        if re.search(r'pg_read_all_stats|\bgrant (?:select|usage) on\b', note):
            return 'credential'
        if re.search(r'\buse (?:a |the )?browser\b', note):
            return 'environment'
    return 'unobservable'


def spec_text(row):
    """Read the current criterion source, never an environment or credential file."""
    import dispatch
    from pathlib import Path
    try:
        return Path(row.get('spec_path') or dispatch.spec_path(row['spec'])).read_text()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return ''


def relabel_fields(row, text):
    """The single need-to-legacy mapping shared with the backfill producer."""
    label = need(row, text)
    cap = NEED_CAPABILITY[label]
    block = criterion_text(row, text)
    if label == 'environment':
        pending = classify_ask(block + ' ' + str(row.get('why') or ''))['capability']
        if pending in ('not-live', 'elapsed'):
            cap = pending
        elif authenticated(block) or anonymous(block) or re.search(r'playwright|browser|\.spec\.ts', block, re.I) or (
                not block and re.search(r'\buse (?:a |the )?browser\b', str(row.get('why') or row.get('ask') or ''), re.I)):
            cap = 'browser'
    return dict(need=label, capability=cap)


def relabel(evs, now):
    """Replay-safe proposals over admitted open rows; no observation is fabricated."""
    import fold
    import owed
    out = []
    by_spec = fold.fold(sorted(evs, key=lambda e: fold.ts(e.get('ts'))))[3]
    for sid, history in sorted(by_spec.items()):
        for row in owed.checks(history, now):
            if row['status'] not in OPEN:
                continue
            human = classify_ask(row.get('why') or row.get('ask'))
            if human['owner'] == 'operator' and human['kind'] in ('approval', 'decision', 'real-world'):
                continue
            fields = relabel_fields(row, spec_text(row))
            prior = [e for e in history if e.get('type') == 'owed-routed'
                     and e.get('route') == 'relabel' and e.get('criterion') == row['criterion']]
            if (prior and all(prior[-1].get(k) == v for k, v in fields.items())
                    and row.get('capability') == fields['capability']):
                continue
            common = dict(subject=sid, criterion=row['criterion'])
            out.append(dict(common, type='owed-routed', route='relabel', **fields))
            if fields['need'] == 'unobservable':
                why = 'Cannot determine observation from criterion-specific evidence'
                block = criterion_text(row, spec_text(row))
                if re.search(r'historical.*(?:never recorded|absent|unavailable)|irretrievably|no longer exists', block, re.I):
                    why = 'Historical observation was never recorded or is no longer available'
                evidence = f"{sid}/{row['criterion']}: {why}"
                if not any(e.get('type') == 'owed-unobservable' and e.get('criterion') == row['criterion']
                           and e.get('capability') == 'other' and e.get('why') == why
                           and e.get('evidence') == evidence for e in history):
                    out.append(dict(common, type='owed-unobservable', capability='other', why=why, evidence=evidence))
    return out

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
                         r'\bapprove\b|\bapproval\b|\bhold (?:is|gets|must be) approved\b|client email|\bpurchase\b', text)
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
        kind = 'real-world' if re.search(r'client email|\bpurchase\b', text) else 'approval'
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
    """Only established capability keys are executable; ambiguous labels stay visible."""
    return capability if capability in ROUTES else None


def from_grader(grader_capability):
    known = {"browser":"browser", "review-account":"browser", "db":"ro-dsn", "db-schema":"ro-dsn",
             "deploy":"not-live", **{c:"repo" for c in ("git","view-paths","tools","node_modules","env-file")}}
    return known.get(grader_capability, grader_capability or "unknown")


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
