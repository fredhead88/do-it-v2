#!/usr/bin/env python3
"""Judge the previous complete UTC day from retained local flow evidence."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
import sys

import cron_run
import fold
import scorecard
import stall_sample

STALE_MIN = 15
DIALOG_MAX_MIN = 10
VERIFIED_MIN_PCT = 80
WAIT_MAX_MIN = 120
MEASURES = ('M1', 'M2', 'M3', 'M5')
UTC = timezone.utc
HOUR = timedelta(hours=1)


def _time(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def m1(events, start, end, now):
    slots = [start + i * HOUR for i in range(-1, 24)]
    candidates = defaultdict(list)
    for row in stall_sample.samples(slots[0], root=fold.ROOT):
        ts = _time(row.get('ts'))
        if ts is not None and slots[0] <= ts < end:
            candidates[ts.replace(minute=0, second=0, microsecond=0)].append((ts, row))
    covered, reasons = {}, []
    for slot in slots:
        rows = sorted(candidates[slot], key=lambda p: p[0])
        valid = [r for _, r in rows if r.get('ok') is True and isinstance(r.get('buckets'), dict)]
        if valid:
            covered[slot] = valid[-1]
        else:
            detail = '; '.join(str(r.get('reason', 'failed or malformed sample')) for _, r in rows)
            reasons.append(f'uncovered slot {slot.isoformat()}' + (f': {detail}' if detail else ': missing sample'))
    runs, cases = {}, []

    def finish(spec):
        run = runs.pop(spec)
        if len(run) >= 2:
            cases.append(dict(spec=spec, first_sample=run[0]['ts'], last_sample=run[-1]['ts'], samples=len(run)))

    for slot in slots:
        row = covered.get(slot)
        stuck = set(row['buckets'].get('STUCK', [])) if row else set()
        for spec in list(runs):
            if spec not in stuck:
                finish(spec)
        for spec in sorted(stuck):
            runs.setdefault(spec, []).append(row)
    for spec in list(runs):
        finish(spec)
    return sorted(cases, key=lambda c: (c['spec'], c['first_sample'])), reasons


def m2(events, start, end, now):
    if not events:
        return [], ['empty decoded ledger']
    written, builds, audits = defaultdict(list), defaultdict(list), defaultdict(list)
    carried, reasons = set(), []
    for row in events:
        kind, spec = row.get('type'), row.get('subject')
        if kind == 'spec-carried':
            carried.add(spec)
        target = (written if kind == 'spec-written' else
                  builds if kind == 'build-started' and row.get('actor') == 'builder' else
                  audits if kind == 'spawn-done' and row.get('role') == 'spec-auditor' else None)
        if target is not None:
            ts = _time(row.get('ts'))
            if ts is None:
                reasons.append(f'{kind} {spec}: invalid ts {row.get("ts")}')
            else:
                target[spec].append((ts, row))
    cases = []
    for spec in sorted(written):
        first = min(written[spec], key=lambda p: p[0])[1]
        if spec in carried or not str(first.get('spawn', '')).startswith('L-spec-writer-') or not builds[spec]:
            continue
        built, row = min(builds[spec], key=lambda p: p[0])
        audited = min(audits[spec], key=lambda p: p[0]) if audits[spec] else None
        if start <= built < end and (audited is None or audited[0] > built):
            cases.append(dict(spec=spec, build_event_type='build-started', build_actor='builder',
                              build_started_at=row['ts'], audit_done_at=audited[1]['ts'] if audited else None))
    return cases, reasons


def m3(events, start, end, now):
    if not events:
        return [], ['empty decoded ledger']
    stats = scorecard.window_stats(events, start, end)
    required = ('attempts', 'verified', 'dropped', 'verified_pct', 'median_wait_min')
    if not isinstance(stats, dict):
        return [], ['window_stats returned non-dict']
    missing = [k for k in required if k not in stats]
    if missing:
        return [], ['window_stats missing ' + ', '.join(missing)]
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not number(stats['attempts']) or stats['attempts'] < 0:
        return [], ['window_stats invalid attempts']
    if stats['attempts'] == 0:
        return [], []
    bad = [k for k in required if not number(stats[k])]
    if bad:
        return [], ['window_stats invalid or null ' + ', '.join(bad)]
    failed = []
    if stats['verified_pct'] < VERIFIED_MIN_PCT:
        failed.append('verified_pct')
    if stats['median_wait_min'] >= WAIT_MAX_MIN:
        failed.append('median_wait_min')
    return ([dict(attempts=stats['attempts'], verified=stats['verified'], verified_pct=stats['verified_pct'],
                  median_wait_min=stats['median_wait_min'], failed=failed)] if failed else []), []


def m5(events, start, end, now):
    cases, reasons, points = [], [], []
    for row in events:
        if row.get('type') != 'pane-dialog-resolved':
            continue
        resolved = _time(row.get('resolved_at'))
        if resolved is None:
            reasons.append(f'dialog {row.get("pane")}: invalid resolved_at')
            continue
        if not start <= resolved < end:
            continue
        previous = _time(row.get('prev_scan_at'))
        if previous is None or previous > resolved:
            reasons.append(f'dialog {row.get("pane")}: invalid prev_scan_at')
            continue
        minutes = (resolved - previous).total_seconds() / 60
        if minutes > DIALOG_MAX_MIN:
            cases.append(dict(pane=row.get('pane'), role=row.get('role'), dialog_min=minutes, resolved_at=row['resolved_at']))
    store_ok = heartbeat_ok = False
    try:
        rows = [json.loads(line) for line in cron_run.store_path('pane-watchdog').read_text().splitlines() if line.strip()]
        store_ok = True
        for row in rows:
            if row.get('ev') == 'end' and row.get('rc') == 0:
                point = _time(row.get('ts'))
                if point is None:
                    reasons.append('watchdog store: invalid end ts')
                else:
                    points.append(point)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        store_error = f'{type(exc).__name__}: {exc}'
    try:
        heartbeat = json.loads((fold.ROOT / 'state/pane-watchdog.json').read_text())
        point = _time(heartbeat.get('ts'))
        if point is None:
            reasons.append('watchdog heartbeat: invalid ts')
        else:
            heartbeat_ok = True
            points.append(point)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        reasons.append(f'watchdog heartbeat: {type(exc).__name__}: {exc}')
    if not store_ok and not heartbeat_ok:
        reasons.append('watchdog history unavailable: ' + store_error)
    points = sorted(set(p for p in points if p <= now))
    earlier = [p for p in points if p <= start]
    previous = earlier[-1] if earlier else None
    if previous is None:
        reasons.append(f'watchdog missing history/day-start gap {start.isoformat()}')
    for point in [p for p in points if start < p < end] + [end]:
        if previous is not None and (point - previous).total_seconds() > STALE_MIN * 60:
            reasons.append(f'watchdog gap {previous.isoformat()} to {point.isoformat()}')
        previous = point
    return cases, reasons


def daily(events: list[dict], now: datetime) -> list[dict]:
    now = _time(now)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    day = start.date().isoformat()
    existing = {e.get('flow_key') for e in events if e.get('type') in ('health-alarm', 'lesson')}
    result = []
    for measure, evaluator in zip(MEASURES, (m1, m2, m3, m5)):
        try:
            cases, reasons = evaluator(events, start, end, now)
        except Exception as exc:
            cases, reasons = [], [f'{type(exc).__name__}: {exc}']
        bodies = []
        if cases or reasons:
            state = 'breach' if cases else 'undetermined'
            specs = ', '.join(sorted({c['spec'] for c in cases if 'spec' in c}))
            line = f'{measure} {day} {state}: {len(cases)} cases' + (f' ({specs})' if specs else '')
            if reasons:
                line += f'; undetermined ({len(reasons)}): ' + '; '.join(reasons)
            bodies.append(dict(type='health-alarm', subject=f'flow-health-{measure}-{day}', alarm='flow-health',
                               severity='summary', line=' '.join(line.split())[:200], measure=measure, day=day,
                               state=state, cases=cases, undetermined=reasons, flow_key=f'{measure}:{day}'))
        if measure == 'M1':
            for case in cases:
                spec, first, last, count = (case[k] for k in ('spec', 'first_sample', 'last_sample', 'samples'))
                body = dict(type='lesson', subject=spec, problem='stuck-over-1h',
                            why=f'M1 {day}: STUCK from {first} to {last}, {count} consecutive hourly samples',
                            measure='M1', day=day, flow_key=f'M1:{day}:{spec}:{first}')
                history = sorted((e for e in events if e.get('subject') == spec and _time(e.get('ts')) is not None),
                                 key=lambda e: _time(e['ts']))
                if history and history[-1].get('project') is not None:
                    body['project'] = history[-1]['project']
                bodies.append(body)
        result.extend(b for b in bodies if b['flow_key'] not in existing)
    return result


def append_body(body):
    previous = os.environ.get('DOIT_LEDGER_FILE')
    os.environ['DOIT_LEDGER_FILE'] = 'L-health-local.jsonl'
    try:
        fold.append([body['type'], body['subject']] +
                    [f'{k}:={json.dumps(v)}' for k, v in body.items() if k not in ('type', 'subject')], strict=True)
    finally:
        if previous is None:
            os.environ.pop('DOIT_LEDGER_FILE', None)
        else:
            os.environ['DOIT_LEDGER_FILE'] = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=date.fromisoformat)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        directory = fold.EVENTS
        stat = directory.stat()
        if not directory.is_dir() or not stat.st_mode & 0o444 or not stat.st_mode & 0o111:
            raise OSError('events directory unreadable')
        names = os.listdir(directory)
        if not any(name.endswith('.jsonl') and (directory / name).is_file() for name in names):
            raise OSError('events directory has no jsonl files')
        events = fold.read_events()
        now = (datetime.combine(args.day + timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(minutes=30)
               if args.day else datetime.now(UTC))
        day = (now - timedelta(days=1)).date().isoformat()
        bodies = daily(events, now)
        # Prior flow keys still report their judged outcome on an idempotent rerun.
        statuses = {m: 'ok' for m in MEASURES}
        for body in events + bodies:
            if body.get('type') == 'health-alarm' and body.get('alarm') == 'flow-health' and body.get('day') == day:
                if body.get('measure') in statuses:
                    statuses[body['measure']] = body['state']
        if args.dry_run:
            print(json.dumps(bodies, sort_keys=True))
        else:
            for body in bodies:
                append_body(body)
        summary = ' '.join(f'{m}={statuses[m]}' for m in MEASURES)
        print(f'flow-health: day={day} {summary} appended={0 if args.dry_run else len(bodies)}')
        return 0
    except (Exception, SystemExit) as exc:
        print(f'flow-health: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
