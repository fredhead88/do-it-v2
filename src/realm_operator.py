"""Pure, read-only operator projections over admitted ledger facts."""
import datetime as dt
import json
import pathlib
import re
import statistics

SPEC = re.compile(r'L-spec-\d+[a-z]?')
FINISHED = {'accepted', 'shipped', 'shipped-owed', 'shipped-owed-due', 'killed', 'void', 'dropped'}


def utc(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def plain(value, limit=300):
    return ' '.join(str(value or '').split())[:limit]


def load_priorities(path):
    data = json.loads(pathlib.Path(path).read_text())
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, list)
            or not all(isinstance(s, str) and SPEC.fullmatch(s) for s in v) for k, v in data.items()):
        raise ValueError('priorities must map group names to arrays of spec ids')
    return data


def window_facts(events, now):
    """Elapsed wall time of completed windows, not queue wait or CI minutes."""
    opened, merged, durations, active = {}, {}, [], {}
    start = now - dt.timedelta(hours=24)
    for e in events:
        ty, sid, at = e.get('type'), e.get('subject'), utc(e.get('ts'))
        if at is None or at > now:
            continue
        if ty == 'window-opened':
            opened[sid] = at
            active[sid] = list(e.get('specs') or [])
        elif ty == 'window-candidate':
            active[sid] = list(e.get('specs') or [])
        elif ty in {'window-verified', 'window-dropped', 'escalation-blocking'}:
            active.pop(sid, None)
            if ty == 'window-verified':
                for spec in e.get('kept') or []:
                    merged[spec] = at
                began = opened.pop(sid, None)
                if began is not None and start <= at and at > began:
                    durations.append((at - began).total_seconds() / 60)
            else:
                opened.pop(sid, None)
    return merged, statistics.median(durations) if durations else None, {s for v in active.values() for s in v}


def stage(state, *, position=None, merged=False, deployed=False):
    if deployed:
        return 'live'
    if state == 'accepted':
        return 'live'
    if state.startswith('shipped') or merged:
        return 'shipped'
    if position is not None:
        return f'waiting to merge (position {position})'
    return {'building': 'building', 'dispatched': 'building', 'built': 'checking',
            'grading': 'checking', 'graded': 'in review', 'reviewing': 'in review',
            'written': 'written, ready, no builder yet', 'killed': 'stopped',
            'void': 'stopped', 'dropped': 'stopped'}.get(state, 'not started' if not state else 'checking')


def hold_rows(rule_rows, classified):
    """Any explicit hold wins, even when its blocker is overdue itself."""
    held = {}
    for row in [*(classified or []), *rule_rows]:
        if row.get('rule') in {'wave-held', 'dependency-held', 'migration-sequenced'} or row.get('bucket') in {'held', 'waiting-on-prereq'}:
            blocker = row.get('blocker')
            held[row['spec']] = dict(blocker=blocker, reason='waiting on ' + plain(blocker or row.get('reason') or 'a recorded hold'))
    return held



def merge_candidates(queue_rows, classified):
    """Old executors merge directly from clean reviews, without queue-add.

    Use the fold's admitted readiness classification for those specs; an
    active builder/grader is never a merge candidate. Explicit enqueue order
    wins when present, otherwise clean-time FIFO supplies the position.
    """
    rows = {r['spec']: dict(r) for r in queue_rows}
    for r in classified:
        if r.get('bucket') == 'queued' and str(r.get('reason', '')).startswith(('awaiting merge window after ', 'in merge window ')):
            rows.setdefault(r['spec'], dict(spec=r['spec'], added_at=r.get('since'), source='clean review / gate'))
    return sorted(rows.values(), key=lambda r: (utc(r.get('added_at')) or dt.datetime.max.replace(tzinfo=dt.timezone.utc), r['spec']))


def shape(events, states, queue_rows, rule_rows, classified, priorities, now, *, titles=None, answered=()):
    """No file, process, database or clock reads. All ordering uses parsed UTC."""
    events = sorted((e for e in events if utc(e.get('ts')) and utc(e['ts']) <= now), key=lambda e: utc(e['ts']))
    states = dict(states)
    titles = titles or {}
    merged, median, in_window = window_facts(events, now)
    held = hold_rows(rule_rows, classified)
    # A released hold cannot survive a terminal state.
    held = {s: h for s, h in held.items() if states.get(s) not in FINISHED and s not in merged}
    terminal = {e['subject'] for e in events if e.get('type') in {'shipped', 'spec-killed', 'spec-closed'}} | set(merged)
    queue_rows = merge_candidates(queue_rows, classified)
    queued = [r for r in queue_rows if r['spec'] not in terminal and states.get(r['spec']) not in FINISHED]
    positions = {r['spec']: i for i, r in enumerate(queued, 1)}
    shipped = {e['subject']: e for e in events if e.get('type') == 'shipped'}
    deployments = [e for e in events if e.get('type') == 'deploy-landed']

    def item(sid):
        at = utc(shipped.get(sid, {}).get('ts')) or merged.get(sid)
        # A deployment must explicitly cover the spec or the exact shipped SHA.
        # A later unrelated deploy is not evidence that this spec is live.
        sha = shipped.get(sid, {}).get('sha')
        deployed = any((e.get('subject') == sid or sid in (e.get('specs') or []) or sha and e.get('sha') == sha)
                       and (at is None or utc(e['ts']) >= at) for e in deployments)
        return dict(spec=sid, title=plain(titles.get(sid) or 'Untitled spec'),
                    stage=held[sid]['reason'] if sid in held else stage(states.get(sid, ''), position=positions.get(sid),
                                                                       merged=sid in merged, deployed=deployed))

    next_merges = []
    for row in queued:
        sid = row['spec']
        if sid in held:
            continue
        position = positions[sid]
        next_merges.append(dict(item(sid), position=position, eta_min=position * median if median is not None else None))
        if len(next_merges) == 5:
            break
    latest_escalation = {}
    for e in events:
        if e.get('type') in {'escalation-blocking', 'decision', 'unblocked'}:
            latest_escalation[e.get('subject')] = e
    needs = [dict(item(e['subject']), asks=plain(e.get('why')), since=e['ts']) for e in latest_escalation.values()
             if e.get('type') == 'escalation-blocking' and str(e.get('owner', '')).lower() == 'operator'
             and e.get('_src') not in answered]

    stuck, seen = [], set()
    waiting_human = {e.get('subject') for e in events if e.get('type') in {'question', 'escalation-blocking'} and e.get('_src') not in answered}
    for row in rule_rows:
        sid = row['spec']
        if (sid in seen or sid in held or sid in positions or sid in in_window or sid in waiting_human
                or sid in terminal or states.get(sid) in FINISHED or row.get('reason_live') or not row.get('overdue')):
            continue
        if str(row.get('reason', '')).startswith('undetermined:'):
            continue
        seen.add(sid)
        since = utc(row.get('since'))
        stuck.append(dict(item(sid), rule=row['rule'], reason=plain(row.get('action') if row.get('reason') == 'condition met' else row.get('reason')), limit_min=row['limit_min'],
                          idle_min=max(0, (now - since).total_seconds() / 60) if since else None))
    shelf = {'held': [], 'ready': [], 'other': []}
    for sid, state in states.items():
        if state != 'written':
            continue
        row = item(sid)
        if sid in held:
            shelf['held'].append(row)
        elif sid in waiting_human:
            shelf['other'].append(dict(row, stage='waiting on a ruling'))
        else:
            shelf['ready'].append(row)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Queue occupancy is reconstructed at midnight and now; removals/requeues
    # affect trend even if the number of merges/intakes happens to be equal.
    start_queue = set()
    for e in events:
        if utc(e['ts']) >= midnight:
            break
        if e.get('type') == 'queue-add' or e.get('type') == 'merge-gate-clean' or e.get('type') == 'review' and e.get('n_blocking') == 0:
            start_queue.add(e['subject'])
        elif e.get('type') in {'queue-remove', 'shipped', 'spec-killed', 'spec-closed', 'build-started', 'build-blocked', 'merge-conflict', 'window-dropped'} or e.get('type') == 'review' and e.get('n_blocking') != 0:
            start_queue.discard(e['subject'])
        elif e.get('type') == 'window-verified':
            start_queue.difference_update(e.get('kept') or [])
    delta = len(queued) - len(start_queue)
    return dict(at=now.isoformat(timespec='seconds'), error=None, next=next_merges, median_window_min=median,
                eta_note='Rough merge ETA = queue position × median completed window in the last 24 h; deployment is additional.',
                priorities=[dict(name=name, specs=[item(s) for s in ids]) for name, ids in priorities.items()],
                needs_you=needs, stuck=stuck, held=[dict(item(s), **h) for s, h in held.items()], shelf=shelf,
                keeping_up=dict(merged=sum(at >= midnight for at in merged.values()),
                                added=len({e['subject'] for e in events if (e.get('type') in {'queue-add', 'merge-gate-clean'} or e.get('type') == 'review' and e.get('n_blocking') == 0) and utc(e['ts']) >= midnight}),
                                trend='growing' if delta > 0 else 'shrinking' if delta < 0 else 'steady', delta=delta,
                                queue=len(queued)),
                notices=[dict(spec=r['spec'], reason=plain(r['reason'])) for r in rule_rows if str(r.get('reason', '')).startswith('undetermined:')])
