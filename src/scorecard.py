#!/usr/bin/env python3
"""Pure UTC production scorecard; only the optional CLI reads external state.

Study definitions are ported here, never imported from the study scripts.
Window cohorts are attributed to their open day, including later outcomes.
"""
import json
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
HOUR = timedelta(hours=1)
STUDY_MEASURES = ('shipped', 'median_lead_h', 'window_attempts', 'windows_verified',
                  'windows_dropped', 'median_wait_min', 'multi_build_pct')
BANDS = {
    'median_lead_h': ('low', 10, 15), 'stuck_over_1h': ('low', 1, 2),
    'window_verified_pct': ('high', 80, 60), 'median_wait_min': ('low', 120, 240),
    'failed_share_pct': ('low', 20, 35), 'seat_busy_pct': ('high', 60, 40),
    'disk_used_pct': ('low', 85, 92), 'ram_used_pct': ('low', 90, 95),
    'claude_7d_delta': ('abs', 10, 20), 'codex_7d_delta': ('abs', 10, 20),
}
STALL_MEASURES = ('stalled_spec_h', 'stuck_over_1h', 'waiting_seat_h',
                  'waiting_window_h', 'waiting_human_h', 'seat_busy_pct', 'ignored_bucket_samples')
PACE_MEASURES = ('disk_used_pct', 'ram_used_pct', 'swap_used_pct', 'claude_5h_pct',
                 'claude_7d', 'codex_7d', 'claude_7d_delta', 'codex_7d_delta')
HUMAN = {'held', 'waiting-on-prereq', 'waiting-on-human'}
LABELS = {
    'shipped': 'Specs shipped', 'median_lead_h': 'Median written-to-shipped (h)',
    'window_attempts': 'Window attempts', 'windows_verified': 'Windows verified',
    'windows_dropped': 'Window drop events', 'median_wait_min': 'Median review-to-window wait (min)',
    'multi_build_pct': 'Multi-build ships (%)', 'window_verified_pct': 'Windows verified (%)',
    'stalled_spec_h': 'Stalled spec-hours', 'stuck_over_1h': 'Specs stuck over 1 h',
    'waiting_seat_h': 'Waiting for seat (spec-hours)', 'waiting_window_h': 'Waiting for window (spec-hours)',
    'waiting_human_h': 'Waiting for human or hold (spec-hours)', 'seat_busy_pct': 'Builder seats busy (%)',
    'ignored_bucket_samples': 'Ignored bucket entries (scheduled progress or unknown bucket)',
    'skipped_events': 'Skipped events (invalid timestamp)', 'disk_used_pct': 'Disk used (%)',
    'ram_used_pct': 'RAM used (%)', 'swap_used_pct': 'Swap used (%)', 'claude_5h_pct': 'Claude 5h used (%)',
    'claude_7d': 'Claude weekly allowance', 'codex_7d': 'Codex weekly allowance',
    'failed_runs': 'Failed runs', 'total_runs': 'Total runs', 'failed_share_pct': 'Failed runs (%)',
    'failed_tokens': 'Failed-run tokens', 'capacity_unused': 'Capacity left unused',
}


def _utc(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _events(events):
    parsed = [(_utc(e.get('ts')), e) for e in events]
    return sorted(((t, e) for t, e in parsed if t is not None), key=lambda p: p[0])


def _median(values):
    return statistics.median(values) if values else None


def _wid(event):
    return event.get('window') or event.get('subject')


def _window_cohort(parsed, since, until):
    opens = [(t, e) for t, e in parsed if e.get('type') == 'window-opened' and since <= t < until]
    ids = {_wid(e) for _, e in opens}
    verified = ids & {_wid(e) for _, e in parsed if e.get('type') == 'window-verified'}
    drops = [e for _, e in parsed if e.get('type') == 'window-dropped' and _wid(e) in ids]
    return opens, ids, verified, drops


def window_stats(events: list[dict], since: datetime, until: datetime) -> dict:
    """Count distinct attempted windows and their complete observed outcomes."""
    parsed = _events(events)
    opens, ids, verified, drops = _window_cohort(parsed, _utc(since), _utc(until))
    reviews = defaultdict(list)
    for t, e in parsed:
        if e.get('type') == 'review' and e.get('n_blocking') == 0:
            reviews[e.get('subject')].append(t)
    waits = []
    for t, e in opens:
        for spec in e.get('specs') or []:
            prior = [r for r in reviews[spec] if r <= t]
            if prior:
                waits.append((t - prior[-1]).total_seconds() / 60)
    return dict(attempts=len(ids), verified=len(verified), dropped=len(drops),
                verified_pct=100 * len(verified) / len(ids) if ids else None,
                median_wait_min=_median(waits))


def band(measure: str, value: float | None, *, swap_pct: float | None = None) -> str:
    if value is None:
        return 'not sampled' if measure in STALL_MEASURES + PACE_MEASURES + ('capacity_unused',) else 'no data'
    if measure not in BANDS:
        return 'no data' if measure == 'claude_5h_pct' else 'green'
    direction, green, red = BANDS[measure]
    if direction == 'abs':
        value = abs(value)
        result = 'green' if value <= green else 'red' if value > red else 'amber'
    elif direction == 'high':
        result = 'green' if value >= green else 'red' if value < red else 'amber'
    else:
        result = 'green' if value < green else 'red' if value > red else 'amber'
    if measure == 'ram_used_pct' and result == 'green' and swap_pct is not None and swap_pct > 90:
        return 'amber'
    return result


def _safe(value):
    return ''.join(c for c in str(value) if not unicodedata.category(c).startswith('C'))[:200]


def _allowance(samples, key):
    readings = [(t, e) for t, e in samples if e.get(key + '_pct') is not None]
    if not readings:
        return None
    t, e = readings[-1]
    reset = _utc(e.get(key + '_reset'))
    if reset is None or reset <= t:
        return None
    pace = min(100, max(0, 100 * (t - (reset - timedelta(days=7))).total_seconds() / (7 * 86400)))
    return dict(pct=e[key + '_pct'], pace_pct=pace, delta=e[key + '_pct'] - pace,
                reset=e[key + '_reset'])


def _spawns(parsed):
    starts, ends = {}, defaultdict(list)
    for t, e in parsed:
        spawn, kind = e.get('spawn'), e.get('type')
        if not spawn:
            continue
        if kind == 'build-started' or (kind == 'spawn-started' and e.get('role') == 'builder'):
            starts.setdefault(spawn, t)
        if kind in ('spawn-done', 'spawn-failed'):
            ends[spawn].append(t)
    return [(start, next((end for end in ends[spawn] if end >= start),
                         start.replace(minute=0, second=0, microsecond=0) + HOUR))
            for spawn, start in starts.items()]


def _top_entry(top, spec):
    return top.setdefault(spec, dict(stalled=0, waiting=0, failed=0, buckets=Counter(), reasons=[]))


def _stall_values(row, hours, histories, intervals, since, until, top):
    selected = [(h, sample) for h, sample in hours.items() if since <= h < until]
    row['sampled']['stall'] = bool(selected)
    row.update({key: None for key in STALL_MEASURES})
    if not selected:
        return
    row.update({key: 0 for key in STALL_MEASURES})
    stuck, busy, capacity = set(), 0, 0
    for hour, (sample_t, sample) in selected:
        buckets = sample.get('buckets') or {}
        previous = hours.get(hour - HOUR)
        if previous:
            stuck.update(set(buckets.get('STUCK') or []) & set(previous[1]['buckets'].get('STUCK') or []))
        for bucket, specs in buckets.items():
            if bucket not in {'STUCK', 'queued'} | HUMAN:
                row['ignored_bucket_samples'] += 1
                continue
            for spec in set(specs or []):
                entry = _top_entry(top, spec)
                entry['buckets'][bucket] += 1
                if bucket == 'STUCK':
                    row['stalled_spec_h'] += 1
                    entry['stalled'] += 1
                else:
                    entry['waiting'] += 1
                    key = 'waiting_human_h'
                    if bucket == 'queued':
                        history = [(t, e) for t, e in histories.get(spec, []) if t <= sample_t]
                        reviews = [(t, e) for t, e in history if e.get('type') == 'review']
                        builds = [t for t, e in history if e.get('type') == 'build-started']
                        clean = bool(reviews and reviews[-1][1].get('n_blocking') == 0
                                     and (not builds or reviews[-1][0] > builds[-1]))
                        key = 'waiting_window_h' if clean else 'waiting_seat_h'
                    row[key] += 1
        capacity += sample.get('builder_capacity') or 0
        end_hour = min(hour + HOUR, until)
        busy += sum(max(0, (min(end, end_hour) - max(start, hour)).total_seconds()) / 3600
                    for start, end in intervals)
    row['stuck_over_1h'] = len(stuck)
    row['seat_busy_pct'] = 100 * busy / capacity if capacity else None


def _failures(row, parsed, since, until, top):
    failures, total = [], 0
    for t, e in parsed:
        if not since <= t < until:
            continue
        kind = e.get('type')
        if (kind == 'build-blocked' or kind == 'verdict' or
                (kind == 'review' and e.get('n_blocking') is not None)):
            total += 1
            if (kind == 'build-blocked' or (kind == 'verdict' and e.get('confirmed') is not True)
                    or (kind == 'review' and e['n_blocking'] > 0)):
                failures.append(e)
        elif kind == 'build-done' and e.get('status') == 'DONE':
            total += 1
    opens, ids, verified, drops = _window_cohort(parsed, since, until)
    row['failed_runs'] = len(failures) + len(ids - verified)
    row['total_runs'] = total + len(ids)
    row['failed_share_pct'] = 100 * row['failed_runs'] / row['total_runs'] if row['total_runs'] else None
    terminals = {}
    for _, e in parsed:
        if e.get('type') == 'spawn-done' and e.get('spawn'):
            terminals.setdefault(e['spawn'], e)
    row['failed_tokens'] = sum(sum(terminals.get(e.get('spawn'), {}).get(key) or 0
                                      for key in ('input_tokens', 'output_tokens', 'cache_read', 'cache_creation'))
                               for e in failures)
    for e in failures:
        if e.get('subject'):
            entry = _top_entry(top, e['subject'])
            entry['failed'] += 1
            entry['reasons'].append(e.get('reason') or e.get('why') or e['type'])
    for wid in ids - verified:
        op = next(e for _, e in opens if _wid(e) == wid)
        dropped = [e for e in drops if _wid(e) == wid]
        reason = 'window not verified'
        if dropped:
            e = dropped[-1]
            reason = e.get('reason') or e.get('why') or f"window dropped on {len(e.get('nodes') or [])} test(s)"
        for spec in set(op.get('specs') or []):
            entry = _top_entry(top, spec)
            entry['failed'] += 1
            entry['reasons'].append(reason)


def _top(top, parsed, until):
    result = []
    for spec in sorted(top, key=lambda s: (-top[s]['stalled'], -top[s]['failed'], -top[s]['waiting'], s))[:5]:
        entry = top[spec]
        reasons = []
        if entry['buckets']:
            last = next((e for t, e in reversed(parsed) if t < until and e.get('subject') == spec), None)
            suffix = f"; last event {last['type']} at {last['ts']}" if last else '; no ledger event'
            reasons.append(', '.join(f'{n} sampled hour(s) in {bucket}'
                                     for bucket, n in sorted(entry['buckets'].items())) + suffix)
        # Keep failure facts visible even when a sampled-hour explanation is long.
        reasons = entry['reasons'] + reasons
        result.append(dict(spec=spec, kinds=[k for k in ('stalled', 'waiting', 'failed') if entry[k]],
                           reason=_safe('; '.join(map(str, reasons)))))
    return result


def _display(value):
    return f'{value:.6g}' if isinstance(value, float) else str(value)


def _cells(row):
    row['bands'] = {}
    for key in BANDS:
        value = row[key.removesuffix('_delta')]['delta'] if key.endswith('_delta') and row[key.removesuffix('_delta')] else row.get(key)
        row['bands'][key] = band(key, value, swap_pct=row['swap_used_pct'])
    cells = []
    for key, label in LABELS.items():
        value = row[key]
        cell_band = row['bands'].get(key, band(key, value))
        if key in ('claude_7d', 'codex_7d'):
            cell_band = row['bands'][key + '_delta']
            text = ('not sampled' if value is None else
                    f"{_display(value['pct'])}% used; pace {_display(value['pace_pct'])}%; "
                    f"delta {_display(value['delta'])} points; reset {value['reset']}")
        elif key == 'capacity_unused':
            text = 'not sampled' if value is None else 'yes' if value else 'no'
            cell_band = 'not sampled' if value is None else 'amber' if value else 'green'
        else:
            text = band(key, None) if value is None else _display(value)
        row['bands'][key] = cell_band
        cells.append(dict(key=key, label=label, value=value, text=text, band=cell_band))
    row['cells'] = cells


def day_rows(events: list[dict], now: datetime, days: int = 7, samples: list[dict] | None = None) -> list[dict]:
    now = _utc(now)
    parsed = _events(events)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    written, shipped, builds = {}, {}, defaultdict(list)
    histories = defaultdict(list)
    for t, e in parsed:
        kind, spec = e.get('type'), e.get('subject')
        histories[spec].append((t, e))
        if kind == 'spec-written':
            written.setdefault(spec, t)
        elif kind == 'shipped':
            shipped.setdefault(spec, t)
        elif kind == 'build-done':
            builds[spec].append(t)
    hours, pace = {}, []
    for t, sample in _events(samples or []):
        if t > now:
            continue
        if 'buckets' in sample:
            if sample.get('ok') is True:
                hours[t.replace(minute=0, second=0, microsecond=0)] = (t, sample)
        elif 'disk_used_pct' in sample:
            pace.append((t, sample))
    intervals = _spawns(parsed)
    result = []
    for ago in reversed(range(days)):
        since = midnight - timedelta(days=ago)
        until = min(since + timedelta(days=1), now)
        cohort = [s for s, t in shipped.items() if s in written and since <= t < until]
        row = dict(date=str(since.date()), partial=ago == 0, through=until.isoformat(),
                   skipped_events=len(events) - len(parsed), sampled=dict(stall=False, pace=False),
                   shipped=len(cohort), median_lead_h=_median([(shipped[s] - written[s]).total_seconds() / 3600 for s in cohort]),
                   multi_build_pct=100 * sum(sum(t <= shipped[s] for t in builds[s]) > 1 for s in cohort) / len(cohort) if cohort else None)
        ws = window_stats(events, since, until)
        for src, dst in [('attempts', 'window_attempts'), ('verified', 'windows_verified'),
                         ('dropped', 'windows_dropped'), ('verified_pct', 'window_verified_pct'), ('median_wait_min', 'median_wait_min')]:
            row[dst] = ws[src]
        top = {}
        _stall_values(row, hours, histories, intervals, since, until, top)
        today_pace = [(t, e) for t, e in pace if since <= t < until]
        row['sampled']['pace'] = bool(today_pace)
        for key in ('disk_used_pct', 'ram_used_pct', 'swap_used_pct'):
            values = [e[key] for _, e in today_pace if e.get(key) is not None]
            row[key] = max(values) if values else None
        row['claude_5h_pct'] = next((e['claude_5h_pct'] for _, e in reversed(today_pace) if e.get('claude_5h_pct') is not None), None)
        for key in ('claude_7d', 'codex_7d'):
            row[key] = _allowance(today_pace, key)
        deltas = [row[key]['delta'] if row[key] else None for key in ('claude_7d', 'codex_7d')]
        loss = row['stalled_spec_h'] + row['waiting_seat_h'] if row['sampled']['stall'] else None
        row['capacity_unused'] = (False if loss == 0 else
                                  True if loss is not None and any(d is not None and d < -10 for d in deltas) else
                                  False if all(d is not None and d >= -10 for d in deltas) else None)
        _failures(row, parsed, since, until, top)
        row['top'] = _top(top, parsed, until)
        _cells(row)
        result.append(row)
    return result


def copy_text(row: dict) -> str:
    lines = [f"Scorecard {row['date']}" + (f" (partial, through {row['through']})" if row['partial'] else '')]
    lines.extend(f"{cell['label']}: {cell['text']} [{cell['band']}]" for cell in row['cells'])
    lines.extend(f"{_safe(item['spec'])}: {', '.join(item['kinds'])}: {_safe(item['reason'])}" for item in row['top'][:5])
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: scorecard.py YYYY-MM-DD', file=sys.stderr)
        return 2
    try:
        day = datetime.strptime(argv[0], '%Y-%m-%d').replace(tzinfo=UTC)
    except ValueError:
        print('expected a UTC date: YYYY-MM-DD', file=sys.stderr)
        return 2
    from fold import read_events
    samples = []
    for name in ('pace', 'stall_sample'):
        try:
            module = __import__(name, fromlist=['samples'])
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        samples.extend(module.samples())
    # A completed day, represented by the older row of a two-day range.
    row = day_rows(read_events(), day + timedelta(days=1), days=2, samples=samples or None)[0]
    print(json.dumps(row))
    print(copy_text(row))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
