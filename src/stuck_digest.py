#!/usr/bin/env python3
"""Daily observation of overdue lane rules, owed counts and health alarms."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import dispatch
import fold
import lane_rules
import owed_digest

LEDGER_NAME = 'L-digest-local.jsonl'


def build(events, now):
    """Build from the supplied ledger and clock; retain due rows verbatim."""
    now = owed_digest._time(now)
    rows = sorted((r for r in lane_rules.due(events, fold.fold(events)[0], now) if r['overdue']),
                  key=lambda r: (r['spec'], r['rule']))
    return dict(date=now.date().isoformat(), stuck_count=len(rows), listed=len(rows),
                live_reason_count=sum(bool(r['reason_live']) for r in rows), rows=rows)


def _line(value):
    return ' '.join(str(value).split())


def render_md(obj, events, now):
    owed = owed_digest.render_md(owed_digest.build(events, now))
    counts = owed.split('## Counts by status and cause\n', 1)[1].split('## NEEDS YOU', 1)[0].strip()
    lines = [f"# Stuck digest — {obj['date']}", '', '## STUCK',
             f"stuck: {obj['stuck_count']} (live reason: {obj['live_reason_count']})"]
    for row in obj['rows']:
        fields = '; '.join(f'{key}: {_line(row[key])}' for key in
                           ('spec', 'rule', 'action', 'since', 'limit_min', 'reason'))
        lines.append(f"- {fields}; {'live' if row['reason_live'] else 'not-live'}")
    if not obj['rows']:
        lines.append('none')
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
            print(json.dumps(obj, sort_keys=True, default=str))
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
                message = _line(f"STUCK-DIGEST {date}: stuck_count={obj['stuck_count']} "
                                f"live_reason_count={obj['live_reason_count']} {path}")[:400]
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
