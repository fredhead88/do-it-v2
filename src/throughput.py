#!/usr/bin/env python3
"""Read-only merge-stage metrics from already-admitted ledger events."""
import argparse
import importlib
import json
import math
import os
import statistics
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

OPEN_WINDOW_MAX_H = 12


def safe_text(value):
    """Keep untrusted ledger text on one terminal-safe line."""
    return ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in str(value))


def _utc(value):
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _same_base(base, sha):
    if not isinstance(base, str) or not isinstance(sha, str):
        return False
    shorter, longer = sorted((base, sha), key=len)
    return len(shorter) >= 7 and longer.startswith(shorter)


def summary(events, now, hours=24):
    """Compute the SD7 dict without reading files, importing fold, or reading a clock.

    Replay queue occupancy before clipping intervals to the requested UTC span.
    Terminal events drain the queue; a later clean event may re-enter a spec.
    Shipment accounting retains only the first shipment after a known clean time.
    """
    now = _utc(now)
    start = now - timedelta(hours=hours)
    result = dict(queue_depth=None if not events else 0, in_flight=[],
                  median_wait_min=None, p90_wait_min=None, merged_per_hour=None,
                  busy_hours=0, shipped=0, stale_merges=None, unproven_merges=0,
                  minutes_per_spec=None, selfhosted_minutes_per_spec=None, reason='')
    if not events:
        result['reason'] = 'no events'
        return result
    ordered, invalid = [], 0
    for e in events:
        try:
            t = _utc(e.get('ts'))
        except (ValueError, TypeError, OverflowError):
            invalid += 1
            continue
        if t <= now:
            ordered.append((t, e))
    ordered.sort(key=lambda item: item[0])  # Stable for equal parsed timestamps.
    reasons = [f'invalid timestamps: {invalid}'] if invalid else []
    specs, queued, merged, waits, busy = {}, set(), set(), [], set()
    windows, previous_sha = {}, {}
    verified_count = estimated = proven = 0
    costs = {'actions_minutes': [], 'selfhosted_minutes': []}
    previous_time = start

    def occupy(left, right):
        left, right = max(left, start), min(right, now)
        if left >= right:
            return
        hour = left.replace(minute=0, second=0, microsecond=0)
        while hour < right:
            busy.add(hour)
            hour += timedelta(hours=1)

    for t, e in ordered:
        if queued:
            occupy(previous_time, t)
        previous_time = t
        kind, subject = e.get('type'), str(e.get('subject', ''))
        if kind in ('review', 'merge-gate-clean', 'shipped', 'spec-killed', 'spec-closed', 'window-dropped'):
            state = specs.setdefault(subject, dict(review=False, review_clean=False,
                                                   clean=None, gate=None, terminal=None))
            if kind == 'review':
                state['review'] = True
                state['review_clean'] = type(e.get('n_blocking')) is int and e['n_blocking'] == 0
                if state['review_clean']:
                    state['clean'] = t
                    queued.add(subject)
                else:
                    queued.discard(subject)
            elif kind == 'merge-gate-clean':
                state['gate'] = t
                # A gate is a fallback only, except the explicit post-drop
                # re-entry allowed by A2. It never overrides a blocking review.
                if not state['review'] or (state['review_clean'] and state['terminal'] is not None
                                           and state['clean'] <= state['terminal']):
                    queued.add(subject)
            else:
                if kind == 'shipped':
                    clean = state['clean'] if state['review'] else state['gate']
                    if clean is not None and subject not in merged:
                        merged.add(subject)
                        if start <= t <= now:
                            waits.append((t-clean).total_seconds()/60)
                state['terminal'] = t
                queued.discard(subject)

        if kind in ('window-opened', 'window-candidate'):
            window = windows.setdefault(subject, dict(specs=[], since=e['ts'], last=t,
                                                       terminal=False, base=None))
            if window['terminal']:
                window['since'] = e['ts']
            window['terminal'], window['last'] = False, t
            if isinstance(e.get('specs'), list):
                window['specs'] = list(e['specs'])
            if kind == 'window-opened':
                window['base'] = e.get('base')
        elif subject in windows:
            windows[subject]['last'] = t
            if kind in ('window-verified', 'escalation-blocking'):
                windows[subject]['terminal'] = True
        if kind != 'window-verified':
            continue
        project = str(e.get('project', ''))
        if start <= t <= now:
            verified_count += 1
            if not e.get('verified_on'):
                result['unproven_merges'] += 1
            else:
                proven += 1
                replaced = e.get('replaced')
                base = windows.get(subject, {}).get('base')
                if replaced:
                    stale = e['verified_on'] != replaced
                elif base:
                    stale = not _same_base(base, e['verified_on'])
                elif previous_sha.get(project):
                    stale = e['verified_on'] != previous_sha[project]
                else:
                    stale = False
                result['stale_merges'] = (result['stale_merges'] or 0) + int(stale)
            estimated += e.get('minutes_source') in (None, 'estimate')
            for field in costs:
                if field not in e:
                    continue
                value = e[field]
                if type(value) in (int, float) and math.isfinite(value):
                    costs[field].append(value)
                else:
                    reasons.append(f'invalid {field}: {safe_text(subject)}')
        if e.get('sha'):
            previous_sha[project] = e['sha']

    if queued:
        occupy(previous_time, now)
    result['queue_depth'] = len(queued)
    result['shipped'] = len(waits)
    result['busy_hours'] = len(busy)
    if busy:
        result['merged_per_hour'] = len(waits)/len(busy)
    if waits:
        result['median_wait_min'] = float(statistics.median(waits))
        result['p90_wait_min'] = float(sorted(waits)[math.ceil(.9*len(waits))-1])
        for field, output in (('actions_minutes', 'minutes_per_spec'),
                              ('selfhosted_minutes', 'selfhosted_minutes_per_spec')):
            if costs[field]:
                result[output] = sum(costs[field])/len(waits)
    for name, window in windows.items():
        if window['terminal']:
            continue
        if now-window['last'] > timedelta(hours=OPEN_WINDOW_MAX_H):
            reasons.append(f'stuck {safe_text(name)}')
        else:
            result['in_flight'].append(dict(window=safe_text(name),
                                           specs=[safe_text(s) for s in window['specs']],
                                           since=safe_text(window['since'])))
    if not proven:
        reasons.append('stale merges unavailable: no verified_on in span')
    if estimated:
        reasons.append(f'minutes: {estimated} of {verified_count} windows estimated')
    result['reason'] = '; '.join(reasons)
    return result


def _hours(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('hours must be an integer in 1..720') from None
    if not 1 <= number <= 720:
        raise argparse.ArgumentTypeError('hours must be an integer in 1..720')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=_hours, default=24)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        now = _utc(os.environ['DOIT_NOW']) if 'DOIT_NOW' in os.environ else datetime.now(timezone.utc)
        ledger = importlib.import_module('fold')
        # Path.glob alone silently treats a missing directory as an empty ledger.
        # Check directory accessibility before using the production reader.
        list(ledger.EVENTS.iterdir())
        events = ledger.read_events()
        ignored = {id(e) for e in ledger.fold(events)[2]}
        stats = summary([e for e in events if id(e) not in ignored], now, args.hours)
    except (OSError, ValueError, TypeError) as exc:
        print(f'throughput: unavailable — {safe_text(type(exc).__name__)}: {safe_text(exc)}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(stats, ensure_ascii=True, allow_nan=False))
    else:
        for key, value in stats.items():
            if key == 'in_flight':
                print(f'in_flight: {len(value)}')
                for window in value:
                    print(f"  {window['window']} · {', '.join(window['specs'])} · since {window['since']}")
            else:
                print(f'{key}: {"?" if value is None else safe_text(value)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
