#!/usr/bin/env python3
"""Lane labels, dwell limits, ladder and legacy read-only row adapter."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tomllib

import autodispatch
import dispatch
import fold
import tick


DEFAULT_TABLE = Path(__file__).resolve().parent.parent / 'lane_rules.toml'
TERMINAL_EVENTS = {'shipped', 'spec-killed', 'spec-closed'}
JUDGING = {'verdict', 'cannot-assess', 'review'}
DISPATCH_EVENTS = {'spawn-started', 'autodispatched', 'spawn-failed', 'autodispatch-failed'}


def _time(value):
    """Reject bad timestamps instead of falling back to a global clock."""
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timestamp needs a timezone')
    return value


def _busy(events, now):
    ended = {e.get('spawn') for e in events
             if e.get('type') in ('spawn-done', 'spawn-failed', 'spawn-stale')}
    spawns = set()
    for e in events:
        if e.get('type') not in ('spawn-started', 'build-started'):
            continue
        spawn = e.get('spawn')
        if spawn and spawn in ended:
            continue
        role = '-'.join(spawn.split('-')[1:-1]) if spawn else e.get('role', '')
        if not role and e['type'] == 'build-started':
            role = 'builder'
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        window = e.get('window_min') or cap
        if (now - _time(e['ts'])).total_seconds() <= (window + cap) * 60:
            spawns.add(e.get('subject'))
    escalations = fold.open_escalations(events)
    future = {e['subject'] for e in escalations if e.get('deadline') and _time(e['deadline']) > now}
    return spawns, future, {e['subject'] for e in escalations}


def _wave(c):
    """Match dispatch.wave_blocker without its clock-dependent spec_state call."""
    from nextstep import _terminal
    evs, sid = c['evs'], c['sid']
    if any(e.get('type') in ('build-done', 'shipped') for e in evs):
        return None
    waiver = next((e for e in reversed(evs) if e.get('type') == 'decision' and e.get('wave_waive')), None)
    if waiver and waiver['wave_waive'] == 'yes':
        return None
    charter = dispatch.resolve_charter(evs, sid, None)
    written = next((e for e in reversed(evs) if e.get('type') == 'spec-written' and e.get('footprint')), None)
    wave = dispatch.spec_wave(charter, written['footprint']) if charter and written else None
    if wave is None:
        return None
    for other, other_evs in c['by_subject'].items():
        if other == sid or not other.startswith('L-spec-'):
            continue
        other_charter = next((e.get('charter') for e in reversed(other_evs) if e.get('charter')), None)
        if not other_charter or Path(other_charter).stem != charter:
            continue
        other_written = next((e for e in reversed(other_evs)
                              if e.get('type') == 'spec-written' and e.get('footprint')), None)
        other_wave = dispatch.spec_wave(charter, other_written['footprint']) if other_written else None
        if other_wave is not None and other_wave < wave and not _terminal(other_evs, c['specs'].get(other)):
            return other
    return None


NAMES = frozenset({'build', 'grade', 'review', 'gate', 'enqueue', 'merge',
                   'idle', 'rebuild', 'regrade', 'close', 'wave-held', 'dependency-held', 'sweep-check'})


def load(path=None):
    path = Path(path if path is not None else os.environ.get('DOIT_LANE_RULES', DEFAULT_TABLE))
    try:
        with path.open('rb') as stream:
            doc = tomllib.load(stream)
        rows = doc['rules']
        if set(doc) != {'rules'} or not isinstance(rows, list):
            raise ValueError('expected rules array')
        table = {}
        for row in rows:
            if set(row) != {'name', 'action', 'actor', 'limit_min'}:
                raise ValueError('rule fields must be name, action, actor, limit_min')
            if any(not isinstance(row[k], str) or not row[k] for k in ('name', 'action', 'actor')):
                raise ValueError('rule strings must be nonempty')
            if type(row['limit_min']) is not int or row['limit_min'] <= 0:
                raise ValueError('limit_min must be a positive integer')
            if row['name'] in table:
                raise ValueError('duplicate rule ' + row['name'])
            table[row['name']] = dict(row)
        if set(table) != NAMES:
            raise ValueError('rule names and lane registry differ')
        return table
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'{path}: {exc}') from exc


def _row(sid, name, rule, result, now):
    since, limit = result['since'], result.get('limit_min', rule['limit_min'])
    return dict(spec=sid, rule=name, action=rule['action'], actor=rule['actor'], since=since,
                limit_min=limit, overdue=result.get('overdue', (now - _time(since)).total_seconds() > limit * 60),
                reason=result.get('reason', 'condition met'), reason_live=result.get('reason_live', False),
                blocker=result.get('blocker'))


def _undetermined(sid, name, rule, now, exc):
    return _row(sid, name, rule, dict(since=now.isoformat(), overdue=True,
                reason=f'undetermined: {exc}'), now)


def due(events, specs, now, table=None, *, only=None):
    """Evaluate a snapshot; the driver can select only its owned predicates.

    Diagnostic idle/held rows depend on acting rows, so watchdog callers use
    the default full evaluation. All memoized work is local to this call.
    """
    from nextstep import PREDICATES, _terminal

    try:
        rules = load(table)
    except ValueError as exc:
        return [_undetermined(sid, 'undetermined', dict(action='none', actor='none', limit_min=0), now, exc)
                for sid in sorted(specs)]
    events = sorted(events, key=lambda e: str(e.get('ts', '')))
    by_subject = {}
    for e in events:
        by_subject.setdefault(e.get('subject', ''), []).append(e)
    capabilities = {}
    try:
        for subject, evs in by_subject.items():
            if subject.startswith('capability:'):
                capabilities.setdefault(subject.split(':', 2)[-1], []).append(evs)
        shipped = {e.get('subject') for e in events if e.get('type') == 'shipped'}
        # Validate before calling any inherited helper: fold.ts falls back to
        # a global clock for malformed input. Such input is undetermined here.
        for e in events:
            _time(e.get('ts'))
        spawns, future, escalations = _busy(events, now)
        busy = spawns | escalations
    except Exception as exc:
        return [_undetermined(sid, 'undetermined', dict(action='none', actor='none', limit_min=0), now, exc)
                for sid in sorted(specs)]
    rows, contexts, memo = [], [], {}
    for sid, spec in sorted(specs.items()):
        evs = by_subject.get(sid, spec.get('evs', []))
        if _terminal(evs, spec):
            continue
        try:
            c = dict(sid=sid, spec=spec, specs=specs, events=events, evs=evs,
                     by_subject=by_subject, now=now, table=rules, busy=busy, live=spawns | future,
                     capabilities=capabilities, memo=memo)
            c['dependency'] = autodispatch.held_by_dependency(evs, events, shipped=shipped)
            c['wave'] = _wave(c)
            contexts.append(c)
        except Exception as exc:
            rows.append(_undetermined(sid, 'undetermined', dict(action='none', actor='none', limit_min=0), now, exc))
    for names in ([n for n in PREDICATES if n not in ('idle', 'wave-held', 'dependency-held')],
                  ['wave-held', 'dependency-held'], ['idle']):
        for c in contexts:
            c['acting_rows'] = rows
            for name in names:
                if only is not None and name not in only:
                    continue
                try:
                    result = PREDICATES[name](c)
                    if result is not None:
                        if name not in ('wave-held', 'dependency-held'):
                            result = dict(reason_live=c['sid'] in c['live'], **result)
                        rows.append(_row(c['sid'], name, rules[name], result, now))
                except Exception as exc:
                    rows.append(_undetermined(c['sid'], name, rules[name], now, exc))
    return rows


def ladder(events, row, now):
    def matches(e):
        return e.get('rule') == row['rule'] and e.get('since') == row['since']
    if any(e.get('type') == 'escalation-blocking' and e.get('subject') == 'lane-stale:' + row['spec']
           and e.get('kind') == 'lane-stale' and matches(e) for e in events):
        return 'digest'
    if row['rule'] in ('merge', 'idle'):
        return 'act' if not row['overdue'] or row['reason_live'] else 'inbox'
    retries = [e for e in events if e.get('type') == 'autodispatched' and e.get('subject') == row['spec']
               and e.get('trigger') == 'retry' and matches(e)]
    if retries and (now - min(_time(e['ts']) for e in retries)).total_seconds() > row['limit_min'] * 60:
        return 'inbox'
    if retries:
        return 'retry'
    if not row['overdue']:
        return 'act'
    return 'retry'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--table')
    args = parser.parse_args()
    events = fold.read_events()
    specs = fold.fold(events)[0]
    rows = due(events, specs, datetime.now(timezone.utc), table=args.table)
    try:
        load(args.table)
    except ValueError as exc:
        print('lane-rules: ' + str(exc).replace('\n', ' '), file=sys.stderr)
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(f"{row['spec']} {row['rule']} overdue={row['overdue']} {row['reason']}")


if __name__ == '__main__':
    main()
