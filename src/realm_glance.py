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


def window_facts(events):
    """An opened window stays open until its actual terminal event.

    A candidate alone cannot open one. Tree verification is intermediate: red
    means checks failed; green still waits for promotion. A per-spec drop must
    not close other specs in the same window. Reopening a name starts a new run.
    """
    windows, shipped = {}, {}
    for e in events:
        kind, sid = e.get('type'), e.get('subject')
        if kind == 'shipped':
            shipped[sid] = e
            for row in windows.values():
                row['specs'] = [s for s in row['specs'] if s != sid]
        if e.get('actor') != 'executor':
            continue
        if kind == 'window-opened':
            members = ids(e.get('specs'))
            for row in windows.values():
                row['specs'] = [s for s in row['specs'] if s not in members]
            windows[sid] = {'specs': members, 'phase': 'merging now'}
        elif kind == 'window-candidate' and sid in windows:
            windows[sid].update(specs=ids(e.get('specs')), phase='merging now')
        elif kind == 'window-tree-verified' and sid in windows:
            windows[sid]['phase'] = 'checks failed; waiting for repair' if e.get('green') is False else 'merging now'
        elif kind in {'window-verified', 'window-cancelled', 'escalation-blocking'}:
            windows.pop(sid, None)
        elif kind in {'shipped', 'window-dropped', 'window-deferred', 'merge-conflict'}:
            name = e.get('window')
            if name in windows:
                windows[name]['specs'] = [s for s in windows[name]['specs'] if s != sid]
            if kind == 'shipped':
                shipped[sid] = e
    active = {s: row['phase'] for row in windows.values() for s in row['specs']}
    return active, shipped


def shape(events, states, rules, classified, priorities, now, *, needs=(), answered=()):
    events = sorted((e for e in events if utc(e.get('ts')) and utc(e['ts']) <= now), key=lambda e: utc(e['ts']))
    states = dict(states)
    active, shipped = window_facts(events)
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
        why = str(e.get('why', '')).lower()
        if e['type'] == 'decision' and (why.startswith(('held on purpose:', 'dependency-held', 'wave-held'))
                or why.startswith('not stuck:') and 'migration order' in why):
            held.add(sid)
    in_progress = {r['spec'] for r in classified if r.get('bucket') in {'queued', 'waiting-on-human'}}
    for sid in list(held):
        if sid in shipped or states.get(sid) in FINISHED:
            held.discard(sid)
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
        return {'building': 'building', 'dispatched': 'building', 'built': 'being checked',
                'grading': 'being checked', 'graded': 'being reviewed', 'reviewing': 'being reviewed',
                'written': 'ready to build', 'blocked': 'waiting for repair',
                'killed': 'stopped', 'void': 'stopped', 'dropped': 'stopped'}.get(state, 'status unconfirmed')

    groups = []
    for name, specs in priorities.items():
        counts = {}
        for sid in specs:
            status = stage(sid)
            counts[status] = counts.get(status, 0) + 1
        # Surface active work before completed work, preserving a short sentence.
        order = ['merging now', 'checks failed; waiting for repair', 'held on purpose', 'waiting for repair',
                 'building', 'being checked', 'being reviewed', 'ready to build', 'live', 'shipped', 'stopped', 'status unconfirmed']
        status = next(iter(counts)) if len(counts) == 1 else ', '.join(f'{counts[k]} {k}' for k in order if k in counts)
        lead = next(k for k in order if k in counts)
        short = status if len(counts) == 1 else f'{counts[lead]} {lead}'
        groups.append({'name': name, 'specs': specs, 'status': status,
                       'text': f'{name}: {short}.', 'detail': f'{name}: {status}.'})
    needs = [e for e in needs if str(e.get('owner', '')).strip().lower() in {'operator', 'you'}
             and e.get('_src') not in answered]
    failures = sorted({r['spec'] for r in rules if r.get('overdue') and not r.get('reason_live')
                       and r.get('spec') not in held and r.get('spec') not in in_progress and r.get('spec') not in active
                       and r.get('spec') not in shipped and states.get(r.get('spec')) not in FINISHED
                       and r.get('rule') not in HELD_RULES and not str(r.get('reason', '')).startswith('undetermined:')})
    broken = sum(p == 'checks failed; waiting for repair' for p in active.values())
    attention = []
    if needs:
        attention.append(f'{len(needs)} {"thing needs" if len(needs) == 1 else "things need"} you')
    else:
        attention.append('No operator decisions waiting')
    if failures:
        attention.append(f'{len(failures)} {"step is" if len(failures) == 1 else "steps are"} overdue')
    if broken:
        attention.append('Merge checks need repair')
    # Lead with active merging, then the first priorities and required action.
    # Expansion shows all five groups; never shrink the font to fit a phone.
    ordered = sorted(groups, key=lambda g: 'merging now' not in g['status'])
    collapsed = ' · '.join(g['text'].rstrip('.').replace(': ', ' ', 1) for g in ordered[:3]) + ' · ' + ' · '.join(attention)
    return {'at': now.isoformat(timespec='seconds'), 'error': None, 'groups': groups,
            'collapsed': collapsed, 'attention': ' · '.join(attention) + (': ' + ' '.join(str(needs[0].get('why') or needs[0].get('asks') or 'Decision requested').split())[:100] if needs else '') + '.',
            'needs_you': [{'spec': e.get('subject'), 'ask': ' '.join(str(e.get('why') or e.get('asks') or 'Decision requested').split())[:200]} for e in needs],
            'overdue': failures, 'held': sorted(held), 'merging': sorted(active),
            'queue_available': False}
