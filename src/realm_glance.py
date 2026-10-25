"""Small operator sentences from admitted facts. Never infer Executor queue order."""
import datetime as dt
import json
from pathlib import Path
import re

FINISHED = {'accepted', 'killed', 'void', 'dropped'}
HELD_RULES = {'dependency-hold', 'dependency-held', 'wave-held', 'migration-sequenced'}


def load_priorities(path):
    groups = json.loads(Path(path).read_text())
    if not isinstance(groups, dict) or len(groups) != 5 or any(
            not isinstance(name, str) or not name or len(name) > 60 or not isinstance(ids, list) or not ids
            or any(not isinstance(s, str) or not re.fullmatch(r'L-spec-\d+', s) for s in ids)
            for name, ids in groups.items()):
        raise ValueError('priorities must contain five named groups of spec ids')
    return groups


def utc(value):
    try:
        at = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at.replace(tzinfo=dt.timezone.utc) if at.tzinfo is None else at.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def ids(value):
    return value.split(',') if isinstance(value, str) else list(value or [])


def window_facts(events, *, include_windows=False):
    """An opened window stays open until its actual terminal event.

    A candidate alone cannot open one. Tree verification is intermediate: red
    means checks failed; green still waits for promotion. A per-spec drop must
    not close other specs in the same window. Reopening a name starts a new run.
    """
    windows, shipped, failures, members = {}, {}, {}, {}
    for e in events:
        kind, sid = e.get('type'), e.get('subject')
        if kind in {'shipped', 'build-started', 'spec-written', 'spec-killed', 'spec-closed'}:
            failures.pop(sid, None)
        if kind == 'shipped':
            shipped[sid] = e
            for row in windows.values():
                row['specs'] = [s for s in row['specs'] if s != sid]
        if e.get('actor') != 'executor':
            continue
        if kind == 'window-opened':
            current = ids(e.get('specs'))
            members[sid] = current
            for spec in current:
                failures.pop(spec, None)
            for row in windows.values():
                row['specs'] = [s for s in row['specs'] if s not in current]
            windows[sid] = {'window': sid, 'specs': current, 'phase': 'merging now', 'since': e['ts'], 'src': e.get('_src')}
        elif kind == 'window-candidate' and sid in windows:
            windows[sid].update(specs=ids(e.get('specs')), phase='merging now')
        elif kind == 'window-tree-verified' and sid in windows:
            windows[sid]['phase'] = 'checks failed; waiting for repair' if e.get('green') is False else 'merging now'
        elif kind == 'window-tree-verified' and e.get('green') is False:
            for spec in members.get(sid, []):
                if failures.get(spec, {}).get('window') == sid:
                    failures[spec]['phase'] = 'checks need repair'
        elif kind in {'window-verified', 'window-cancelled', 'escalation-blocking'}:
            row = windows.pop(sid, None)
            if kind == 'escalation-blocking' and row:
                for spec in row['specs']:
                    failures[spec] = {'window': sid, 'phase': 'checks need repair' if e.get('outcome') == 'red' else 'checks need attention'}
        elif kind in {'shipped', 'window-dropped', 'window-deferred', 'merge-conflict'}:
            name = e.get('window')
            if name in windows:
                windows[name]['specs'] = [s for s in windows[name]['specs'] if s != sid]
            if kind == 'shipped':
                shipped[sid] = e
    active = {s: row['phase'] for row in windows.values() for s in row['specs']}
    result = active, shipped, {s: row['phase'] for s, row in failures.items()}
    return (*result, list(windows.values())) if include_windows else result


def recorded_hold(e):
    why = str(e.get('why', '')).lower()
    return e.get('actor') in {'thinker', 'operator', 'executor'} and e.get('type') == 'decision' and (
        why.startswith(('held on purpose:', 'dependency-held', 'wave-held'))
        or why.startswith('not stuck:') and 'migration order' in why)


def hold_facts(events, states, rules, classified, now):
    events = sorted((e for e in events if utc(e.get('ts')) and utc(e['ts']) <= now), key=lambda e: utc(e['ts']))
    # The live classifier understands migration sequencing, recorded rulings,
    # dependency and wave holds. Read it even before a wait is overdue.
    held = {r['spec'] for r in [*classified, *rules] if r.get('rule') in HELD_RULES
            or r.get('bucket') in {'held', 'waiting-on-prereq'}}
    # Some deliberate migration holds are rulings on lane-stale:<spec>,
    # rather than on the spec itself. Read only that exact target and explicit
    # hold language from an authorized decision, never incidental spec mentions.
    rulings = {}
    for e in events:
        sid = str(e.get('subject') or '').removeprefix('lane-stale:')
        if re.fullmatch(r'L-spec-\d+', sid) and e.get('actor') in {'thinker', 'operator', 'executor'}:
            if e.get('type') in {'decision', 'unblocked'}:
                rulings[sid] = e
    for sid, e in rulings.items():
        if recorded_hold(e):
            held.add(sid)
    return {'held': held, 'rulings': rulings, 'windows': window_facts(events, include_windows=True)[3]}


def shape(events, states, rules, classified, priorities, now, *, needs=(), answered=(), stuck=None):
    events = sorted((e for e in events if utc(e.get('ts')) and utc(e['ts']) <= now), key=lambda e: utc(e['ts']))
    states = dict(states)
    active, shipped, failed = window_facts(events)
    work = {}
    for e in events:
        if e.get('type') in {'spec-written', 'build-started', 'build-done', 'build-blocked', 'verdict', 'review', 'merge-gate-clean'}:
            work[e.get('subject')] = e
    running = {r['spec']: r.get('owner') for r in classified if 'owns active dispatch' in str(r.get('reason', ''))}
    held = hold_facts(events, states, rules, classified, now)['held']
    for sid in list(held):
        if sid in shipped or states.get(sid) in FINISHED:
            held.discard(sid)
    failed = {s: phase for s, phase in failed.items() if s not in shipped and s not in held
              and states.get(s) not in FINISHED and not states.get(s, '').startswith('shipped')}
    deployed = set()
    for e in events:
        if e.get('type') != 'deploy-landed':
            continue
        for sid, ship in shipped.items():
            if utc(e['ts']) >= utc(ship['ts']) and (e.get('subject') == sid or sid in ids(e.get('specs'))
                    or ship.get('sha') and e.get('sha') == ship['sha']):
                deployed.add(sid)

    def stage(s):
        state = states.get(s, '')
        if s in active:
            return active[s]
        if s in deployed:
            return 'live'
        if s in shipped or state.startswith('shipped') or state == 'accepted':
            return 'shipped'
        if s in held:
            return 'held on purpose'
        if s in failed:
            return failed[s]
        if s in running:
            return {'builder':'building', 'grader':'being checked', 'reviewer':'in review'}.get(running[s], 'being checked')
        marker = work.get(s, {})
        if marker.get('type') == 'merge-gate-clean' or marker.get('type') == 'review' and marker.get('n_blocking') == 0:
            return 'ready to merge'
        if marker.get('type') == 'review' and marker.get('n_blocking', 0):
            return 'changes requested'
        return {'building': 'building', 'dispatched': 'building', 'built': 'being checked',
                'grading': 'being checked', 'graded': 'ready for review', 'reviewing': 'in review',
                'written': 'ready to build', 'blocked': 'waiting for repair',
                'killed': 'stopped', 'void': 'stopped', 'dropped': 'stopped'}.get(state, 'status unconfirmed')

    groups = []
    for name, specs in priorities.items():
        counts = {}
        for sid in specs:
            status = stage(sid)
            counts[status] = counts.get(status, 0) + 1
        # Surface active work before completed work, preserving a short sentence.
        order = ['merging now', 'checks failed; waiting for repair', 'held on purpose', 'checks need repair', 'checks need attention', 'waiting for repair',
                 'changes requested', 'building', 'being checked', 'in review', 'ready for review', 'ready to merge', 'ready to build', 'live', 'shipped', 'stopped', 'status unconfirmed']
        status = next(iter(counts)) if len(counts) == 1 else ', '.join(f'{counts[k]} {k}' for k in order if k in counts)
        lead = next(k for k in order if k in counts)
        short = {'checks failed; waiting for repair':'checks need repair', 'waiting for repair':'needs repair',
                 'being checked':'checking', 'status unconfirmed':'unconfirmed'}.get(lead, lead)
        if len(counts) > 1 and lead in {'live', 'shipped'}:
            short = 'partly ' + lead
        groups.append({'name': name, 'specs': specs, 'status': status,
                       'text': f'{name}: {short}.', 'detail': f'{name}: {status}.'})
    needs = [e for e in needs if str(e.get('owner', '')).strip().lower() in {'operator', 'you'}
             and e.get('_src') not in answered]
    broken = sum(p == 'checks failed; waiting for repair' for p in active.values()) + len(failed)
    attention = []
    if needs:
        attention.append(f'{len(needs)} {"thing needs" if len(needs) == 1 else "things need"} you')
    else:
        attention.append('No operator decisions waiting')
    if stuck is None or stuck.get('error'):
        attention.append('stuck count unavailable')
    elif stuck['count']:
        count = stuck['count']
        attention.append(f'{count} {"spec is" if count == 1 else "specs are"} stuck')
    if broken:
        attention.append('Merge checks need attention')
    # Lead with active merging, then the first priorities and required action.
    # Expansion shows all five groups; never shrink the font to fit a phone.
    ordered = sorted(groups, key=lambda g: 'merging now' not in g['status'])
    clauses = [g['text'].rstrip('.').replace(': ', ' ', 1) for g in ordered[:3]]
    collapsed = ' · '.join([*attention, *clauses] if needs else [*clauses, *attention])
    return {'at': now.isoformat(timespec='seconds'), 'error': None, 'groups': groups,
            'collapsed': collapsed, 'attention': ' · '.join(attention) + '.',
            'needs_you': [{'spec': e.get('subject'), 'ask': ' '.join(str(e.get('why') or e.get('asks') or 'Decision requested').split())[:200]} for e in needs],
            'stuck': stuck, 'check_issues': sorted(failed), 'held': sorted(held), 'merging': sorted(active),
            'queue_available': False}
