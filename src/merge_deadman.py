"""Detect factory-wide merge silence independently of the dispatch loop."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import re
import unicodedata

import merge_loop_state
import notification_policy
import tg_say

WINDOW = timedelta(minutes=90)
REPEAT = timedelta(hours=3)
PROJECTS = {'albert-scott': 'master', 'do-it-v2': 'main'}
SUBJECT = 'factory:merging'
KIND = 'merge-deadman'
LEDGER = 'L-look-local.jsonl'


def stamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _summary(value, limit=400):
    """Bound untrusted diagnostics before handing plain text to the formatter."""
    text = str(value)[:8000]
    text = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)
    text = re.sub(r'(?i)\b(?:secret|token|password|credential|api[-_ ]?key)\s*[:=]\s*\S+',
                  '[redacted]', text)
    text = re.sub(r'\b[A-Za-z0-9_-]{24,}\b', '[redacted]', text)
    text = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://\S+|(?:~?/|[A-Za-z]:[\\/])\S+',
                  '[path removed]', text)
    text = re.sub(r'(?i)\bUTC\b|\+00:00|\d{2}:\d{2}(?::\d{2})?Z\b|\blocalhost\b',
                  '[time or host removed]', text)
    text = re.sub(r'[^A-Za-z0-9 ._:;,()=-]', ' ', text)
    text = ' '.join(text.split())[:limit] or 'unavailable'
    try:
        notification_policy.validate_text(text)
    except ValueError:
        # Diagnostics must not disable observation by impersonating a message clause.
        identifiers = re.findall(r'\b(?:L-spec-[0-9]{4}|test_[A-Za-z0-9_.:]+)\b', text)
        text = ' '.join(identifiers)[:80] + ' diagnostic unavailable'
    return text


def cause(state):
    """Describe only the reader's observed status and ordered failures."""
    status = _summary(state.status, 24)
    parts = [f'Merge loop status: {status}.']
    if state.status == 'undetermined':
        parts.append('The cause cannot be confirmed.')
    for failure in state.failures[:3]:
        attempts = failure.attempts
        attempts = str(attempts) if type(attempts) is int and attempts >= 0 else 'unknown'
        parts.append(f'{_summary(failure.spec, 80)}: {_summary(failure.reason)}; attempts: {attempts}.')
    if not state.failures:
        parts.append('No failure reason is available.')
    if len(state.failures) > 3:
        parts.append(f'{len(state.failures) - 3} more failures.')
    return ' '.join(parts)


def evaluate(events, now, *, state, merges=None):
    """Use queue truth for eligibility and landed times only for the clock."""
    now = stamp(now)
    if not (state.queued or state.status == 'undetermined'):
        return None
    times = merges.values() if merges is not None else [state.last_merge]
    landed = [t for value in times if (t := stamp(value)) is not None and t <= now]
    since = max(landed) if landed else None
    if since is not None and now - since < WINDOW:
        return None
    prior = [t for e in events if e.get('type') == 'escalation-blocking'
             and e.get('subject') == SUBJECT and e.get('kind') == KIND
             and (t := stamp(e.get('ts'))) is not None and t <= now]
    if prior and now - max(prior) < REPEAT:
        return None
    return dict(kind=KIND, owner='thinker', since=since.isoformat() if since else None,
                cause=cause(state), why='Thinker investigation required for merge-loop inactivity.',
                default='Thinker to investigate the merge loop and report findings.',
                deadline=now.isoformat(), revert='Resume normal observation when merging recovers')


def message(alarm, now):
    what = ('No factory merge for at least 90 minutes.' if alarm['since'] else
            'Factory merge timing is unknown; silence duration is unknown.')
    return notification_policy.format_message(
        '[DOWN]', what, alarm['cause'],
        'No operator action is required. The Thinker owns investigation.', by=now)


def probe_merges(root, *, runner=subprocess.run):
    """Read only commits landed on the watched first-parent branches."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    result = {}
    for project, branch in PROJECTS.items():
        repo = Path(root) / 'repos' / project
        if not repo.is_dir():
            continue
        try:
            filters = ['--extended-regexp', r'--grep=^merge\(L-spec'] if project == 'albert-scott' else []
            proc = runner(['git', '-C', str(repo), 'log', '--first-parent', '-1', '--format=%cI',
                           *filters, branch, '--'],
                          capture_output=True, text=True, env=env, timeout=10)
            result[project] = proc.stdout.strip() if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            result[project] = None
    return result


def run(events, now, root, *, dry_run=False, sender=None, merges=None):
    """Called by look under its pass lock, so dispatch failure cannot hide it."""
    import dispatch
    root = Path(root)
    events = list(events)
    state = merge_loop_state.read(root=root, now=now)
    dst = root / 'events' / LEDGER
    # Include durable alarms even when a caller reused its pre-alarm snapshot.
    import json
    import ledger_files
    persisted = []
    try:
        lines = ledger_files.read_text(root / 'events', LEDGER).splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            persisted.append(event)
    alarm = evaluate(list(events) + persisted, now, state=state, merges=merges)
    if alarm is None or dry_run:
        return alarm
    dst.parent.mkdir(parents=True, exist_ok=True)
    refusal = dispatch.emit(dst, {'subject': SUBJECT}, 'escalation-blocking', strict=True,
                            **alarm)
    if refusal:
        raise ValueError('merge dead-man escalation refused: ' + refusal)
    # Record before attempting delivery; restarted callers consult this row.
    try:
        result = tg_say.deliver('merge-deadman', message(alarm, now), label='[DOWN]', root=root)
        if isinstance(result, tg_say.Delivery) and result.state in {'sent', 'held', 'failed', 'refused'}:
            alarm['delivery'] = dict(state=result.state, http=result.http)
        else:
            alarm['delivery'] = dict(state='failed', http=None, error='invalid delivery result')
    except Exception as exc:
        # Transport diagnostics can contain private payloads; expose the error class only.
        alarm['delivery'] = dict(state='failed', http=None, error=type(exc).__name__)
    return alarm
