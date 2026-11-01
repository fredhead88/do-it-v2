"""Detect factory-wide merge silence independently of the dispatch loop."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess

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


def ready_specs(events):
    """Track unshipped gated/queued work; newer rework invalidates old readiness."""
    ready, projects = {}, {}
    for event in events:
        sid, kind = event.get('subject'), event.get('type')
        if not isinstance(sid, str) or not sid.startswith('L-spec-'):
            continue
        if event.get('project'):
            projects[sid] = event['project']
        if kind in {'shipped', 'spec-killed', 'spec-closed', 'queue-remove', 'window-dropped',
                    'spec-written', 'spec-amended', 'build-started', 'merge-gate-rework',
                    'merge-conflict', 'conflict-rework', 'rejected-criterion'}:
            ready.pop(sid, None)
        elif kind in {'merge-gate-clean', 'queue-add'} or (kind == 'review' and event.get('n_blocking') == 0):
            ready.setdefault(sid, stamp(event.get('ts')))
    return {sid: when for sid, when in ready.items() if when is not None and projects.get(sid) in PROJECTS}


def cause(events):
    """Name observed components, without copying arbitrary diagnostic payloads."""
    ticks = [e for e in events if e.get('type') == 'tick']
    labels = []
    if ticks and (ticks[-1].get('autodispatch_pass_failed') is True or ticks[-1].get('autodispatch_error')):
        labels.append('dispatch loop erroring')
    latest_base = {}
    for event in events:
        kind = event.get('type', '')
        text = ' '.join(str(event.get(k, '')) for k in ('reason', 'why', 'condition', 'error', 'reason_class')).lower()
        if 'quota' in text or 'usage limit' in text or kind == 'quota-exhausted':
            labels.append('seats paused for quota')
        if 'chain_lock' in text or 'chain lock' in text or 'chain-lock' in text:
            labels.append('chain_lock held')
        if ('ledger' in text and 'lock' in text) or 'locktimeout' in text:
            labels.append('ledger lock')
        if kind in {'base-red', 'base-red-nightly', 'nightly-red', 'merge-frozen'}:
            latest_base[event.get('project')] = event
    if any(e.get('ids') or e.get('complete') is False or e.get('type') == 'merge-frozen' for e in latest_base.values()):
        labels.append('base not releasable')
    return '; '.join(dict.fromkeys(labels)) or 'merge routing or Executor stalled; no recent component diagnostic'


def evaluate(events, now, *, merges):
    """Return one due alarm from explicit events, clock and landed merge times."""
    now = stamp(now)
    rows = sorted((e for e in events if stamp(e.get('ts')) is not None and stamp(e['ts']) <= now),
                  key=lambda e: stamp(e['ts']))
    ready = ready_specs(rows)
    if not ready:
        return None
    landed = [stamp(t) for t in merges.values() if stamp(t) is not None and stamp(t) <= now]
    landed += [stamp(e['ts']) for e in rows if e.get('type') == 'shipped' and
               (e.get('project') in PROJECTS or e.get('subject') in ready)]
    # Start a new observation interval when work first becomes ready.
    since = max([min(ready.values()), *landed])
    if now - since < WINDOW:
        return None
    prior = [stamp(e['ts']) for e in rows if e.get('type') == 'escalation-blocking'
             and e.get('subject') == SUBJECT and e.get('kind') == KIND]
    if prior and now - max(prior) < REPEAT:
        return None
    recent = [e for e in rows if now - stamp(e['ts']) <= WINDOW]
    component = cause(recent)
    return dict(kind=KIND, owner='operator', since=since.isoformat(), ready=sorted(ready),
                cause=component, why=f'No factory merge for 90 minutes with merge-ready specs: {component}',
                default='Keep ready work queued; repair the named stopped component',
                deadline=now.isoformat(), revert='Resume merging after the stopped component is repaired')


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
    if not any(e.get('type') in {'merge-gate-clean', 'queue-add', 'review'} for e in events):
        return None
    merges = probe_merges(root) if merges is None else merges
    # A root with no watched repositories is not an installed factory.
    if not merges:
        return None
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
    alarm = evaluate(list(events) + persisted, now, merges=merges)
    if alarm is None or dry_run:
        return alarm
    dst.parent.mkdir(parents=True, exist_ok=True)
    refusal = dispatch.emit(dst, {'subject': SUBJECT}, 'escalation-blocking', strict=True,
                            **alarm)
    if refusal:
        raise ValueError('merge dead-man escalation refused: ' + refusal)
    # Record before sending: a sender failure or interrupted process cannot flood.
    message = alarm['why'] + '; ready=' + ', '.join(alarm['ready'][:8])
    try:
        if sender:
            sender(message, root)
        else:
            subprocess.run([str(root / 'telegram/tg-send.sh'), message],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    return alarm
