#!/usr/bin/env python3
"""Daily observation of overdue lane rules, owed counts and health alarms."""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import dispatch
import autodispatch
import digest_actions
import fold
import fold_stalls
import lane_rules
import owed_digest
import owed_routes

LEDGER_NAME = 'L-digest-local.jsonl'


def build(events, now):
    """Build from the supplied ledger and clock; retain due rows verbatim."""
    now = owed_digest._time(now)
    events = digest_actions.as_of(events, now)
    folded = fold.fold(events)
    rows = sorted((r for r in lane_rules.due(events, folded[0], now) if r['overdue']),
                  key=lambda r: (r['spec'], r['rule']))
    actions, waiting = [], []
    admitted = [e for evs in folded[3].values() for e in evs]
    admitted.extend(e for e in events if e.get('type') in ('thinker-answer', 'operator-approval')
                    and e.get('actor') in ('thinker', 'operator'))
    answers = [e for e in admitted if e.get('type') in ('decision', 'thinker-answer', 'operator-approval',
                                                     'owed-met', 'owed-failed', 'owed-waived', 'owed-corrective')]
    sources = {e['_src']: e for e in admitted if e.get('_src')}
    for row in rows:
        evs = folded[3].get(row['spec'], [])
        held = autodispatch.held_by_dependency(evs, events)
        gate = fold_stalls.latest(evs, {'merge-gate-clean'})
        review = fold_stalls.latest(evs, {'review'})
        resets = [e for e in evs if e['type'] in fold_stalls.RESETS - {'merge-gate-clean'}
                  or e['type'] in {'spec-shape-failed', 'spawn-started', 'autodispatched',
                                   'spawn-failed', 'autodispatch-failed', 'queue-remove'}
                  or e['type'] == 'review' and not (type(e.get('n_blocking')) is int
                                                   and e['n_blocking'] == 0)
                  or e['type'] == 'decision' and (e.get('rework') == 'yes' or e.get('regrade') == 'yes'
                                                or e.get('close'))]
        def current(e):
            return e and not any(digest_actions.instant(r['ts']) >= digest_actions.instant(e['ts'])
                                 for r in resets) and not fold.standing_rejects(evs)

        queued = current(gate) or (review and type(review.get('n_blocking')) is int
                                  and review['n_blocking'] == 0 and current(review))
        # Some queued windows were recorded as scoped Thinker rulings before
        # the gate emitted its own event. Accept both recorded phrasings, but
        # never let a ruling hide a later failure or a standing rejection.
        ruling = next((e for e in reversed(folded[3].get('lane-stale:' + row['spec'], []))
                       if e['type'] == 'decision' and e.get('actor') in ('thinker', 'operator')), None)
        why = str((ruling or {}).get('why', ''))
        if (re.match(r'^not stuck:', why, re.I)
                and (re.search(r'\bmerge window\b', why, re.I)
                     or re.search(r'enqueued in window', why, re.I) and 'queue ahead' in why.lower())
                and current(ruling)):
            queued = True
        question = next((e for e in reversed(evs)
                         if e['type'] in ('question', 'escalation-blocking')
                         and not digest_actions.answered(
                             dict(spec=row['spec'], criterion=e.get('criterion') or 'next step', since=e['ts']),
                             e, answers, now, sources)), {})
        note = question.get('asks') or question.get('why') or ''
        classification = owed_routes.classify_ask(note)
        action = dict(row, owner=classification['owner'], ask=note,
                      kind=classification['kind'], criterion=question.get('criterion') or 'next step')
        # An unanswered question needs a next action even after a clean review.
        # Dependency holds retain their existing waiting behavior.
        if action['owner'] != 'operator' and held and 'unresolvable' not in held:
            waiting.append(dict(row, waiting_for='dependency'))
        elif not held and not question and queued:
            waiting.append(dict(row, waiting_for='merge'))
        elif action['owner'] != 'operator' and row.get('reason_live'):
            waiting.append(dict(row, waiting_for='active'))
        else:
            actions.append(action)
    return dict(date=now.date().isoformat(), stuck_count=len(rows), listed=len(rows),
                live_reason_count=sum(bool(r['reason_live']) for r in rows), rows=rows,
                action_rows=actions, waiting_rows=waiting)


def render_telegram(obj):
    """Name an owner and a concrete next step; preserve full rows in Markdown."""
    rows = obj.get('action_rows', obj['rows'])
    operator = [r for r in rows if r.get('owner') == 'operator']
    if operator:
        entries = ['NEEDS YOU: ' + digest_actions.operator_line(r) for r in operator]
        header = digest_actions.pack('Work needing your answer:', entries)[0]
    else:
        header = 'Nothing needs you today.'
    lines = [header]
    seen = {r['spec'] for r in rows}
    for kind, description in (
            ('merge', 'finished {noun} {verb} waiting {possessive} turn to merge'),
            ('dependency', '{noun} {verb} waiting for other work to merge first'),
            ('active', '{noun} {verb} waiting on work already in progress')):
        specs = {r['spec'] for r in obj.get('waiting_rows', []) if r.get('waiting_for') == kind} - seen
        seen.update(specs)
        if specs:
            lines.append(f"{len(specs)} " + description.format(
                noun='spec' if len(specs) == 1 else 'specs', verb='is' if len(specs) == 1 else 'are',
                possessive='its' if len(specs) == 1 else 'their') + '.')
    count = len({r['spec'] for r in rows if r.get('owner') != 'operator'} - {r['spec'] for r in operator})
    if count:
        lines.append(f'{count} {"spec is" if count == 1 else "specs are"} stuck '
                     f'and the Thinker is on {"it" if count == 1 else "them"}.')
    elif len(lines) == 1 and not operator:
        lines[0] += ' No stalled work needs intervention.'
    return '\n'.join(lines)


def next_steps(obj):
    """The Thinker's full daily list; never crowded into the operator message."""
    steps = {'build': 'start the build', 'rebuild': 'restart the build after its answer',
             'grade': 'start the verification', 'regrade': 'rerun verification after its answer',
             'review': 'start the review', 'gate': 'run the checks before merging',
             'enqueue': 'repair the missing review evidence before scheduling the merge',
             'merge': 'resolve why the ready change has not merged',
             'close': 'carry out the recorded closure',
             'idle': 'choose and start the next step',
             'wave-held': 'resolve the stalled prerequisite',
             'dependency-held': 'repair the unresolved prerequisite',
             'undetermined': 'repair the missing evidence for the next step'}
    rows = obj.get('action_rows', obj['rows'])
    if not rows and not obj.get('waiting_rows'):
        return 'Nothing needs you today. No stalled work needs intervention.'
    entries, seen = [], set()
    for row in rows:
        if row['spec'] in seen:
            continue
        seen.add(row['spec'])
        if row.get('owner') == 'operator':
            entries.append('NEEDS YOU: ' + digest_actions.operator_line(row))
        else:
            step = steps.get(row['rule'], 'inspect the missing next action')
            entries.append(f"Thinker: {step}. No reply needed from you. [{row['spec']}]")
    for row in obj.get('waiting_rows', []):
        if row['spec'] in seen:
            continue
        seen.add(row['spec'])
        status = {'merge': 'Waiting their turn to merge',
                  'dependency': 'Waiting for other work to merge first',
                  'active': 'Waiting on work already in progress'}[row['waiting_for']]
        entries.append(f"{status}. No reply needed from you. [{row['spec']}]")
    return '\n'.join(entries)


def _line(value):
    return ' '.join(str(value).split())


def render_md(obj, events, now):
    owed = owed_digest.render_md(owed_digest.build(events, now))
    counts = owed.split('## Counts by status and cause\n', 1)[1].split('## NEEDS YOU', 1)[0].strip()
    lines = [f"# Stuck digest — {obj['date']}", '', '## STUCK',
             f"stuck: {obj['stuck_count']} (live reason: {obj['live_reason_count']})",
             'Raw overdue timers for audit; waiting and stalled work are listed separately below.']
    for row in obj['rows']:
        fields = '; '.join(f'{key}: {_line(row[key])}' for key in
                           ('spec', 'rule', 'action', 'since', 'limit_min', 'reason'))
        lines.append(f"- {fields}; {'live' if row['reason_live'] else 'not-live'}")
    if not obj['rows']:
        lines.append('none')
    lines.extend(['', '## NEXT ACTIONS', ''])
    lines.append(next_steps(obj))
    lines.append(f"Waiting to merge, on a dependency, or on work in progress: {len(obj.get('waiting_rows', []))} rows.")
    lines.extend(['', '## OWED', counts, '', '## HEALTH ALARMS'])
    try:
        import health_alarms
    except ImportError:
        lines.append('health alarms: unavailable (module not merged)')
    else:
        alarms = health_alarms.summary_lines(events, now)
        lines.extend(f'- {_line(line)}' for line in alarms)
        if not alarms:
            lines.append('none')
    return '\n'.join(lines) + '\n'


def _emit(event_type, **fields):
    refusal = dispatch.emit(fold.EVENTS / LEDGER_NAME, {}, event_type, subject='stuck-digest', **fields)
    if refusal:
        raise ValueError(refusal)


def run(*, send=False, dry_run=False, now=None, sender=None):
    try:
        clock = owed_digest._time(now if now is not None else datetime.now(timezone.utc))
        events = owed_digest._read_ledger()
        obj = build(events, clock)
        if dry_run:
            print(json.dumps(dict(digest=obj, telegrams=[render_telegram(obj)]), sort_keys=True, default=str))
            return 0
        # Resolve all observation failures before creating any report or event.
        render_md(obj, events, clock)
        state = fold.ROOT / 'state'
        state.mkdir(parents=True, exist_ok=True)
        with (state / 'stuck-digest.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            events = owed_digest._read_ledger()
            obj = build(events, clock)
            text = render_md(obj, events, clock)
            date = obj['date']
            path = fold.ROOT / 'content' / f'L-stuck-digest-{date}.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            if not any(e.get('type') == 'stuck-digest' and e.get('date') == date
                       and e.get('path') == str(path) for e in events):
                _emit('stuck-digest', date=date, path=str(path),
                      **{key: obj[key] for key in ('stuck_count', 'listed', 'live_reason_count')})
            if send and not any(e.get('type') == 'operator-alert' and e.get('subject') == 'stuck-digest'
                                and e.get('kind') == 'stuck-digest' and e.get('date') == date
                                and e.get('sent') == 'yes' for e in events):
                message = render_telegram(obj)
                argv = [str(fold.ROOT / 'telegram/tg-send.sh'), message]
                failure = ''
                try:
                    result = sender(argv) if sender else subprocess.run(
                        argv, capture_output=True, text=True,
                        env={**os.environ, 'TG_SENDER': 'stuck-digest'})
                    lines = (result.stdout or '').splitlines()
                    sent = result.returncode == 0 and bool(lines) and lines[-1] == '200'
                except OSError as exc:
                    sent, failure = False, _line(exc)
                _emit('operator-alert', kind='stuck-digest', date=date, path=str(path),
                      sent='yes' if sent else 'no', detail=message)
                if not sent:
                    raise OSError('Telegram send failed' + (': ' + failure if failure else ''))
        return 0
    except Exception as exc:
        print(f'stuck-digest: {type(exc).__name__}: {_line(exc)}', file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    return run(send=args.send, dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
