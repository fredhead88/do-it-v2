#!/usr/bin/env python3
"""Attempt-local comparison of an original scheduler answer and a receiver refusal.

Approval is descriptive metadata, never a dispatch permission or a gate input.
"""
import json
import datetime as dt
import collections
import re

ROLES = dict(build='builder', rebuild='builder', grade='grader', regrade='grader', review='reviewer')
FIELDS = ('item', 'role', 'state', 'owner', 'action', 'trigger', 'since', 'attempt')


def parse(value, subject, role):
    try:
        if len(value if isinstance(value, str) else json.dumps(value)) > 8192:
            return None
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict) or set(value) != set(FIELDS):
            return None
        if (value['item'] != subject or not isinstance(subject, str) or not subject
                or value['role'] != role or value['role'] != ROLES.get(value['action'])
                or value['state'] != 'action' or value['owner'] != 'tick'
                or not isinstance(value['since'], str) or not value['since']
                or type(value['attempt']) is not int or value['attempt'] < 0
                or value['trigger'] is not None and not isinstance(value['trigger'], str)):
            return None
        return json.loads(json.dumps(value))
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def from_step(step):
    role = ROLES.get(step.action)
    if role is None:
        return None
    answer = {k: getattr(step, k) for k in FIELDS if k != 'role'}
    answer['role'] = role
    return parse(answer, step.item, role)


class Attempt:
    """One invocation owns one comparison, including nested fail callbacks."""
    def __init__(self, value, subject, role):
        self.answer = parse(value, subject, role)
        self.role = role
        self.recorded = False

    def refuse(self, emit, ledger, base, rule, *, backend_ran=False):
        if self.answer is None or self.recorded or backend_ran:
            return
        self.recorded = True
        rule = rule if isinstance(rule, str) and rule.strip() else 'receiver-refused'
        emit(ledger, base, 'dispatch-disagreed', role=self.role, rule=rule,
             nextstep_answer=self.answer, receiver_answer=dict(state='refused', rule=rule))


def event_reason(event):
    """Validate comparison records at the existing ledger admission boundary."""
    if any(not isinstance(event.get(k), str) or not event[k].strip()
           for k in ('subject', 'project', 'spawn', 'role', 'rule')):
        return 'dispatch-disagreed requires subject, project, spawn, role and rule'
    if parse(event.get('nextstep_answer'), event['subject'], event['role']) is None:
        return 'dispatch-disagreed requires the original nextstep answer'
    if event.get('receiver_answer') != dict(state='refused', rule=event['rule']):
        return 'dispatch-disagreed requires a matching receiver answer'
    return None


def prefork(a, rule, dispatch):
    """Record an approved parent-side refusal without starting a child."""
    attempt = Attempt(getattr(a, 'nextstep_approval', None), a.subject, a.role)
    if attempt.answer is None:
        return
    ledger = dispatch.alloc(dispatch.EVENTS, f'L-{a.role}-', '.jsonl')
    base = dict(subject=a.subject, project=a.project or 'unknown', spawn=ledger.stem)
    attempt.refuse(dispatch.emit, ledger, base, rule)


def refused(why):
    return re.search(r'ahead of unsettled|identical packet|D120|spec-shape|before any spend|refus(ing|ed)',
                     str(why or ''), re.I) is not None


def stamp(value):
    try:
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.astimezone(dt.timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def daily(rows, now):
    """Count admitted compact rows over (now - 24 hours, now]."""
    start = now - dt.timedelta(hours=24)
    rules, reasons = collections.Counter(), collections.Counter()
    for row in rows:
        time = stamp(row.get('t'))
        if row.get('a') == 'relay' or time is None or not start < time <= now:
            continue
        if row.get('ty') == 'dispatch-disagreed':
            rules[row['rule']] += 1
        elif row.get('ty') == 'spawn-failed' and row.get('refused', refused(row.get('why'))):
            reasons[row.get('reason') or 'unspecified'] += 1
    return dict(start=start.isoformat(), end=now.isoformat(),
                disagreements=sum(rules.values()), disagreements_by_rule=dict(rules),
                refusals_by_reason=dict(reasons))


def observe(root, spec, now=None):
    """Read-only M4 observation; never infer an unobservable zero."""
    import fold
    import realm
    import pathlib
    now = now or dt.datetime.now(dt.timezone.utc)
    root = pathlib.Path(root)
    report = dict(daily([], now), state='cannot-observe', ship_at=None,
                  disagreements=None, reason=None)
    def unavailable(reason):
        return dict(report, state='cannot-observe', reason=reason, disagreements=None)
    if not (root / 'events').is_dir():
        return unavailable('unreadable ledger')
    prior = fold.ROOT, fold.EVENTS, fold.PROJECT
    try:
        fold.ROOT, fold.EVENTS, fold.PROJECT = root, root / 'events', None
        events = fold.read_events(use_cache=False)
    except (OSError, ValueError, TypeError) as exc:
        return unavailable(type(exc).__name__)
    finally:
        fold.ROOT, fold.EVENTS, fold.PROJECT = prior
    anchors = [stamp(e.get('ts')) for e in events
               if e.get('type') == 'shipped' and e.get('subject') == spec
               and fold.check_append(e, e['actor']) is None]
    anchors = [time for time in anchors if time is not None and time <= now]
    if not anchors:
        return unavailable('missing ship anchor')
    report['ship_at'] = max(anchors).isoformat()
    if now - max(anchors) < dt.timedelta(hours=24):
        return unavailable('insufficient post-ship time')
    for event in events:
        if (event.get('type') == 'dispatch-disagreed' and event.get('actor') in ROLES.values()
                and ((time := stamp(event.get('ts'))) is None
                     or now - dt.timedelta(hours=24) < time <= now and event_reason(event))):
            return unavailable('malformed comparison')
    rows = [row for event in events if fold.check_append(event, event['actor']) is None
            for row in [realm.compact(event, event['actor'])] if row]
    report.update(daily(rows, now))
    report['state'] = 'met' if report['disagreements'] <= 3 else 'failed'
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-m4', action='store_true', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--spec', required=True)
    args = parser.parse_args()
    report = observe(args.root, args.spec)
    print(json.dumps(report, sort_keys=True))
    raise SystemExit({'met': 0, 'failed': 1, 'cannot-observe': 2}[report['state']])
