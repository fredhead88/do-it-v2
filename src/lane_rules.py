#!/usr/bin/env python3
"""Read-only lane conditions. All predicate clocks come from the caller."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tomllib

import autodispatch
import dispatch
import fold
import routing
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


def _latest(events, kinds):
    return next((e for e in reversed(events) if e.get('type') in kinds), None)


def _after(events, trigger, kinds, role=None):
    return any(e.get('type') in kinds and _time(e['ts']) >= _time(trigger['ts'])
               and (role is None or e.get('role') == role) for e in events)


def _terminal(evs, spec=None):
    return (any(e.get('type') in TERMINAL_EVENTS for e in evs)
            or (spec or {}).get('state') in dispatch.SETTLED_STATES)


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


def _result(trigger, reason='condition met', **fields):
    _time(trigger['ts'])
    return dict(since=trigger['ts'], reason=reason, **fields)


def _builder_dispatch(e):
    return (e.get('type') in DISPATCH_EVENTS and
            (e.get('role') or '-'.join(str(e.get('spawn') or '').split('-')[1:-1])
             or 'builder') == 'builder')


def _builder_question(evs):
    """The latest builder attempt must still end at this question."""
    activity = [e for e in evs if e.get('type') in {'build-started', 'build-blocked', 'build-done'}
                or e.get('type') == 'question' and e.get('actor') == 'builder'
                or _builder_dispatch(e)]
    question = _latest(activity, {'question', 'build-blocked', 'build-done'})
    if not question or question['type'] != 'question' or not question.get('_src'):
        return None
    attempt = _latest(activity, DISPATCH_EVENTS | {'build-started', 'build-blocked'})
    if attempt and (_time(attempt['ts']) >= _time(question['ts'])
                    or attempt.get('spawn') and attempt['spawn'] != question.get('spawn')):
        return None
    return question


def _rebuild(c):
    evs = c['evs']
    # Settled fold states include shipped (and all shipped-owed-* states),
    # accepted, closed-shipped, closed-unbuilt, dropped, killed and void.
    if _terminal(evs, c['spec']) or c['dependency']:
        return None
    blocked = _latest(evs, {'build-blocked'})
    outcome = _latest(evs, {'build-done', 'build-blocked'})
    # thinker-answer refs the builder's question, not the build-blocked row.
    # A spawn ties that question to this blocked build; legacy rows without a
    # spawn must at least belong to the current block's time interval.
    questions = [e for e in evs if blocked and e.get('type') == 'question'
                 and e.get('actor') == 'builder' and e.get('_src')
                 and (e.get('spawn') == blocked['spawn'] if blocked.get('spawn')
                      else _time(e['ts']) >= _time(blocked['ts']))]
    question = _builder_question(evs)
    if question and question not in questions:
        questions.append(question)
    decisions = [e for e in evs if e.get('type') == 'decision' and e.get('actor') in ('thinker', 'operator')
                 and not e.get('close') and e.get('regrade') != 'yes' and not e.get('wave_waive')
                 # Explicit rework belongs to the latest completed builder attempt,
                 # even when its ref also answers a historical blocked build.
                 and (e.get('rework') != 'yes' or not outcome or _time(e['ts']) > _time(outcome['ts']))
                 and (blocked and _time(e['ts']) > _time(blocked['ts']) and e.get('ref')
                      and e['ref'] == blocked.get('_src')
                      or any(e.get('ref') == q['_src'] and _time(e['ts']) > _time(q['ts'])
                             for q in questions)
                      or e.get('rework') == 'yes')]
    rewrites = [e for e in evs if blocked and e.get('type') == 'spec-written'
                and _time(e['ts']) > _time(blocked['ts'])]
    trigger = max(decisions + rewrites, key=lambda e: _time(e['ts']), default=None)
    dispatched = [e for e in evs if _builder_dispatch(e)]
    if (trigger and (not blocked or _time(trigger['ts']) > _time(blocked['ts']))
            and not _after(dispatched, trigger, DISPATCH_EVENTS)
            and not _after(evs, trigger, {'build-started'})):
        return _result(trigger)


def _close(c):
    trigger = next((e for e in reversed(c['evs']) if e.get('type') == 'decision'
                    and e.get('actor') in ('thinker', 'operator') and e.get('close')), None)
    return _result(trigger) if trigger else None


def _regrade(c):
    if c['dependency']:
        return None
    triggers = []
    for evs in c['capabilities'].get(c['sid'], []):
        hold = _latest(evs, {'capability-hold'})
        unblocked = _latest(evs, {'unblocked'})
        if (hold and unblocked and unblocked.get('actor') in ('thinker', 'operator')
                and unblocked.get('regrade') != 'no'
                and _time(unblocked['ts']) > _time(hold['ts'])
                and not routing.hold_ruled(c['evs'], hold, c['sid'])):
            triggers.append(unblocked)
    decision, kind = autodispatch._grader_trigger(c['evs'])
    if decision and kind == 'regrade':
        triggers.append(decision)
    trigger = max(triggers, key=lambda e: _time(e['ts']), default=None)
    if trigger and not _after(c['evs'], trigger, DISPATCH_EVENTS, 'grader'):
        return _result(trigger)


def _build(c):
    if c['spec'].get('state') != 'written' or c['sid'] in c['busy'] or c['dependency'] or c['wave']:
        return None
    if _rebuild(c) or _close(c) or _regrade(c):
        return None
    # Hand-authored later pipeline events are still authoritative if a fold
    # rejects their envelope; never dispatch another first build over them.
    if any(e.get('type') in JUDGING | {'build-done', 'queue-add', 'merge-gate-clean', 'build-blocked'} for e in c['evs']):
        return None
    trigger = _latest(c['evs'], {'spec-written'})
    return _result(trigger) if trigger else None


def _grade(c):
    if c['dependency']:
        return None
    trigger, kind = autodispatch._grader_trigger(c['evs'])
    if not trigger or kind == 'regrade' or _rebuild(c) or _regrade(c) or _close(c):
        return None
    if _after(c['evs'], trigger, DISPATCH_EVENTS, 'grader') or _after(c['evs'], trigger, JUDGING | {'queue-add', 'merge-gate-clean'}):
        return None
    return _result(trigger)


def _review(c):
    trigger = _latest(c['evs'], JUDGING | {'build-done', 'build-blocked'})
    if not trigger or not (trigger['type'] == 'cannot-assess' or
                           trigger['type'] == 'verdict' and trigger.get('confirmed') is True):
        return None
    if fold.standing_rejects(c['evs']) or _rebuild(c) or _close(c) or _regrade(c):
        return None
    if _after(c['evs'], trigger, DISPATCH_EVENTS, 'reviewer') or _after(c['evs'], trigger, {'queue-add', 'merge-gate-clean'}):
        return None
    return _result(trigger)


def _clean_review(c):
    trigger = _latest(c['evs'], JUDGING | {'build-done', 'build-blocked'})
    if (trigger and trigger['type'] == 'review' and trigger.get('confirmed') is True
            and type(trigger.get('n_blocking')) is int and trigger['n_blocking'] == 0
            and not fold.standing_rejects(c['evs'])):
        return trigger


def _gate(c):
    trigger = _clean_review(c)
    if not trigger or _close(c) or _rebuild(c) or _regrade(c):
        return None
    build = _latest(c['evs'], {'build-done'})
    anchor = build or trigger
    if any(str(e.get('type', '')).startswith('merge-gate-') and _time(e['ts']) >= _time(anchor['ts']) for e in c['evs']):
        return None
    if _after(c['evs'], trigger, {'queue-add'}):
        return None
    return _result(trigger)


def _enqueue(c):
    trigger = _latest(c['evs'], {'merge-gate-clean'})
    if not trigger or _close(c) or _rebuild(c) or _regrade(c):
        return None
    if _after(c['evs'], trigger, {'queue-add', 'shipped', 'window-dropped', 'merge-conflict'}):
        return None
    review = _clean_review(c)
    if review is None:
        raise ValueError('clean gate has no clean review timestamp')
    return _result(review)


def _merge(c):
    trigger = _latest(c['evs'], {'queue-add'})
    if not trigger or _after(c['evs'], trigger, {'queue-remove', 'shipped', 'window-dropped', 'merge-conflict'}):
        return None
    from merge_queue import queue
    live = next((r for r in queue(c['events']) if r.get('spec') == c['sid']), None)
    if live is not None:
        return _result(dict(ts=live['added_at']))


def _hold(c, blocker, kind):
    evs = c['by_subject'].get(blocker, [])
    terminal = _terminal(evs, c['specs'].get(blocker))
    if kind == 'dependency-held' and (not evs or terminal or c['specs'].get(blocker, {}).get('state', 'unknown') == 'unknown'):
        hold = _latest(c['evs'], {'dependency-hold'})
        return _result(hold, 'blocker-dead: ' + str(blocker), blocker=blocker, overdue=True)
    owned = next((r for r in c['acting_rows'] if r['spec'] == blocker
                  and not r['reason'].startswith('undetermined: ')), None)
    if owned:
        return dict(since=owned['since'], limit_min=owned['limit_min'], blocker=blocker,
                    reason='blocker-open: ' + owned['rule'], reason_live=not owned['overdue'])
    trigger = _latest(evs, TERMINAL_EVENTS) if terminal else (evs[-1] if evs else None)
    trigger = trigger or _latest(c['evs'], {'dependency-hold', 'spec-written'})
    return _result(trigger, 'blocker-terminal: ' + str(blocker) if terminal else 'blocker-open: no-row',
                   blocker=blocker, reason_live=not terminal and blocker in c['live'])


def _wave_held(c):
    return _hold(c, c['wave'], 'wave-held') if c['wave'] else None


def _dependency_held(c):
    if not c['dependency']:
        return None
    hold = _latest(c['evs'], {'dependency-hold'})
    return _hold(c, hold.get('waits_for'), 'dependency-held')


def _idle(c):
    if (not re.fullmatch(r'L-spec-\d{4}', c['sid']) or c['spec'].get('state') == 'unknown'
            or not any(e.get('type') == 'spec-written' for e in c['evs'])
            or c['wave'] or c['dependency'] or c['sid'] in c['busy']
            or any(r['spec'] == c['sid'] for r in c['acting_rows'])):
        return None
    # A standing dispatch failure and capability hold have another owner.
    fail = _latest(c['evs'], {'autodispatch-failed'})
    marker = _latest(c['evs'], {'spec-written', 'decision', 'unblocked', 'dependency-hold'})
    if fail and (not marker or _time(fail['ts']) > _time(marker['ts'])):
        return None
    for evs in c['capabilities'].get(c['sid'], []):
        last = _latest(evs, {'capability-hold', 'unblocked'})
        if last and last['type'] == 'capability-hold':
            return None
    # This is a ledger-wide query, including cross-subject refs. Share it for
    # this one due() call rather than rescanning the ledger for every idle spec.
    if 'answered' not in c['memo']:
        c['memo']['answered'] = fold.answered(c['events'])
    answered = c['memo']['answered']
    if any(e.get('type') == 'question' and e.get('_src') not in answered for e in c['evs']):
        return None
    trigger = c['evs'][-1] if c['evs'] else None
    if trigger and (c['now'] - _time(trigger['ts'])).total_seconds() >= c['table']['idle']['limit_min'] * 60:
        return _result(trigger)


PREDICATES = {'build': _build, 'grade': _grade, 'review': _review, 'gate': _gate,
              'enqueue': _enqueue, 'merge': _merge, 'idle': _idle, 'rebuild': _rebuild,
              'regrade': _regrade, 'close': _close, 'wave-held': _wave_held,
              'dependency-held': _dependency_held}


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
        if set(table) != set(PREDICATES):
            raise ValueError('rule names and predicate registry differ')
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
