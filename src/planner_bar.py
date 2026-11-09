#!/usr/bin/env python3
"""Read-only three-UTC-date Planner history report (stdlib only).

The pure API consumes supplied rows; only main reads named files. Unknown
history never supplies zero CPU or an exact process death time. The dialog
check is the approved ledger proxy, not a claim about terminal contents.
"""
import sys

# Set before the sole local import, including ordinary invocation without -B.
sys.dont_write_bytecode = True

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import zlib

import ledger_files

UTC = dt.timezone.utc
CADENCE = 1200
CHARTER = re.compile(r'L-charter-\d+\Z')
PLANNER = re.compile(r'L-planner-\d+\Z')
SOURCE = re.compile(r'([A-Za-z0-9_.-]+\.jsonl(?:\.gz)?):([1-9]\d*)\Z')


def utc(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp needs timezone')
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError('invalid timestamp') from None


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def number(value, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


class BarResult(dict):
    """JSON-serializable mapping; to_dict also supports explicit API callers."""

    def to_dict(self):
        return json.loads(json.dumps(self, allow_nan=False))


def new_check(**coverage):
    return dict(status='pass', reasons=[], evidence=[], coverage=coverage)


def mark(check, status, reason):
    if check['status'] != 'fail' and (status == 'fail' or check['status'] == 'pass'):
        check['status'] = status
    if reason not in check['reasons']:
        check['reasons'].append(reason)


def incomplete_history(checks, incomplete):
    """Missing rows cannot prove absence; retain independent positive proofs."""
    for name in ('pickup', 'idle_cpu'):
        checks[name]['status'] = 'undetermined'
        for evidence in checks[name]['evidence']:
            evidence['status'] = 'undetermined'
    dialog = checks['dialog']
    for evidence in dialog['evidence']:
        filename = evidence['source'].rsplit(':', 1)[0]
        if '*' in incomplete or filename in incomplete:
            evidence['status'] = 'undetermined'
    dialog['status'] = ('fail' if any(e['status'] == 'fail' for e in dialog['evidence']) else 'undetermined')


def source(row):
    value = row.get('_src')
    match = SOURCE.fullmatch(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def identity(row):
    fields = [row[k] for k in ('planner', 'spawn') if k in row]
    if not fields and source(row):
        fields = [source(row)[:-6]]
    if (not fields or any(not isinstance(v, str) or not PLANNER.fullmatch(v) for v in fields)
            or len(set(fields)) != 1):
        return None
    return fields[0]


def lifecycle(rows, end):
    spans, ignored, errors = {}, set(), []
    for when, row in rows:
        if when > end:
            continue
        kind, charter = row.get('type'), row.get('subject')
        if kind not in ('planner-started', 'planner-ended'):
            continue
        who = identity(row)
        if kind == 'planner-started' and row.get('mode') == 'serving':
            if who:
                ignored.add(who)
            continue
        if who in ignored:
            continue
        if kind == 'planner-started':
            if not isinstance(charter, str) or not CHARTER.fullmatch(charter):
                continue
            if who is None or row.get('mode') not in ('charter', 'ctx-restart') or who in spans:
                errors.append('invalid or conflicting Planner start identity/mode')
                continue
            spans[who] = dict(planner=who, charter=charter, start=when, last=when,
                              upper=end, exact=False, never=False, row=row)
        elif who is None or who not in spans:
            errors.append('unmatched Planner end identity')
        else:
            span = spans[who]
            if (span.get('ended') or charter != span['charter']
                    or ('mode' in row and row['mode'] not in ('charter', 'ctx-restart'))):
                errors.append('conflicting Planner end identity/mode')
                continue
            span['ended'] = when
            span['upper'] = when
            span['never'] = row.get('reason') == 'never-started'
            span['exact'] = row.get('reason') != 'vanished'
            if span['exact']:
                span['last'] = when
    # Identity-specific activity, not a box-wide alive tick, proves continued life.
    for when, row in rows:
        if when > end or row.get('type') in ('planner-started', 'planner-ended', 'tick'):
            continue
        who = identity(row)
        if who in spans:
            span = spans[who]
            if span['start'] <= when <= span['upper']:
                span['last'] = max(span['last'], when)
    return sorted(spans.values(), key=lambda s: (s['start'], s['planner'])), errors


def live_state(spans, when):
    possible = False
    for span in spans:
        if span['never'] or not span['start'] < when < span['upper']:
            continue
        if when <= span['last']:
            return 'alive'
        possible = True
    return 'unknown' if possible else 'idle'


def tick_history(rows, start, end, check):
    ticks = []
    statuses = {'started', 'starting', 'alive', 'idle', 'escalated', 'undetermined'}
    for when, row in rows:
        if row.get('type') != 'tick':
            continue
        ready, status = row.get('planner_ready'), row.get('planner_status')
        valid = (isinstance(status, str) and status in statuses and status != 'undetermined' and isinstance(ready, list)
                 and all(isinstance(c, str) and CHARTER.fullmatch(c) for c in ready)
                 and len(set(ready)) == len(ready)
                 and (status != 'alive' or not ready)
                 and (status not in ('started', 'starting') or bool(ready)))
        ticks.append((when, row, valid))
    usable = [t for t, _, valid in ticks if valid]
    gaps = [(b - a).total_seconds() for a, b in zip(usable, usable[1:]) if b > start and a < end]
    before = [t for t in usable if t <= start]
    after = [t for t in usable if t >= end]
    check['coverage'].update(tick_count=len(ticks), cadence_limit_seconds=CADENCE,
                             max_gap_seconds=max(gaps, default=None),
                             predecessor=stamp(max(before)) if before else None,
                             successor=stamp(min(after)) if after else None)
    if (not before or not after or (start - max(before)).total_seconds() > CADENCE
            or (min(after) - end).total_seconds() > CADENCE or any(g > CADENCE for g in gaps)):
        mark(check, 'undetermined', 'missing bracketing ticks or tick gap exceeds 1200 seconds')
    if any(not valid for t, _, valid in ticks if t <= end):
        mark(check, 'undetermined', 'invalid or unknown tick readiness/status')
    return ticks


def pickup_check(ticks, spans, start, end, check):
    episodes, pending = [], {}
    timeline = [(t, 0, row, valid) for t, row, valid in ticks if t < end]
    timeline += [(s['start'], 1, s, True) for s in spans if s['start'] < end]
    timeline.sort(key=lambda item: (item[0], item[1], str(item[2].get('_src', item[2].get('planner', '')))))
    for when, kind, row, valid in timeline:
        if kind:
            charter = row['charter']
            if charter in pending:
                ep = pending.pop(charter)
                ep['picked_at'] = when
                ep['planner'] = row['planner']
            continue
        if not valid:
            continue
        state = live_state(spans, when)
        if row['planner_status'] == 'alive' or state == 'alive':
            continue
        if state == 'unknown':
            if row['planner_ready']:
                mark(check, 'undetermined', 'readiness eligibility has uncertain Planner liveness')
            continue
        for charter in sorted(row['planner_ready']):
            if charter not in pending:
                ep = dict(charter=charter, ready_at=when, picked_at=None, source=row.get('_src') if source(row) else None)
                pending[charter] = ep
                episodes.append(ep)
    for ep in episodes:
        picked, ready = ep['picked_at'], ep['ready_at']
        if picked is not None and picked < start:
            continue
        elapsed = ((picked or end) - ready).total_seconds()
        status = ('pass' if elapsed <= CADENCE else 'fail') if picked is not None else ('fail' if elapsed >= CADENCE else 'undetermined')
        check['evidence'].append(dict(episode=f"{ep['charter']}@{stamp(ready)}", charter=ep['charter'],
                                      planner=ep.get('planner'), ready_at=stamp(ready),
                                      picked_at=stamp(picked) if picked else None,
                                      latency_seconds=elapsed if picked else None,
                                      elapsed_seconds=elapsed, status=status, source=ep['source']))
        if status != 'pass':
            mark(check, status, 'pickup exceeds 1200 seconds' if status == 'fail' else 'pickup not yet proven at window end')
    return episodes


def overlap_check(spans, start, end, check):
    for i, left in enumerate(spans):
        if left['never']:
            continue
        for right in spans[i + 1:]:
            if right['never'] or left['charter'] != right['charter']:
                continue
            lower = max(start, left['start'], right['start'])
            upper = min(end, left['upper'], right['upper'])
            known = min(end, left['last'], right['last'])
            if lower >= upper:
                continue
            status = 'fail' if lower < known else 'undetermined'
            check['evidence'].append(dict(charter=left['charter'], planners=[left['planner'], right['planner']],
                                          start=stamp(lower), known_live_end=stamp(known),
                                          possible_end=stamp(upper), status=status,
                                          bounds=[dict(planner=s['planner'], start=stamp(s['start']),
                                                       last_known_alive=stamp(s['last']), possible_end=stamp(s['upper']),
                                                       exact_end=s['exact']) for s in (left, right)]))
            mark(check, status, 'proved same-charter Planner overlap' if status == 'fail' else 'possible overlap with uncertain end')


def dialog_check(rows, spans, start, end, check):
    check['coverage']['method'] = 'SD11 ledger proxy; 900-second start-only file'
    for span in spans:
        row, launched = span['row'], span['start']
        if launched >= end:
            continue
        filename = source(row)
        if not filename:
            mark(check, 'undetermined', 'missing Planner source-file identity')
            continue
        others = [t for t, r in rows if t <= end and source(r) == filename and r is not row and t >= launched]
        first = min(others) if others else None
        threshold = launched + dt.timedelta(seconds=900)
        if first is not None and first <= start:
            continue
        status = 'pass'
        if threshold <= end and (first is None or first > threshold):
            status = 'fail'
        elif first is None:
            status = 'undetermined'
        check['evidence'].append(dict(planner=span['planner'], charter=span['charter'],
                                      source=row['_src'], started_at=stamp(launched),
                                      threshold_at=stamp(threshold), first_other_at=stamp(first) if first else None,
                                      status=status))
        if status != 'pass':
            mark(check, status, 'historical 900-second start-only ledger proxy' if status == 'fail' else 'start-only proxy not yet mature')


def supervision(cmd):
    if not isinstance(cmd, str):
        return False
    # Only the actual up entry points identify supervision; relay and active
    # planner-run processes remain outside this measure.
    if re.search(r'(?<![\w-])(?:relay|planner[_-]run)(?:\.py)?(?:\s|$)', cmd):
        return False
    return bool(re.search(r'(?:^|[\s/])up\.py(?:\s|$)|(?:^|[\s/])doit\s+up(?:\s|$)', cmd))


def cpu_check(samples, ticks, spans, episodes, start, end, check, lifecycle_unknown):
    intervals, previous = [], None
    for row in samples:
        try:
            if not isinstance(row, dict):
                raise ValueError()
            closed = utc(row.get('ts'))
            wall, by, top = row.get('wall'), row.get('by'), row.get('top')
            if (not number(wall, positive=True) or not isinstance(by, dict)
                    or not all(isinstance(k, str) and number(v) for k, v in by.items())
                    or not isinstance(top, list)
                    or not all(isinstance(p, dict) and number(p.get('cpu')) for p in top)):
                raise ValueError()
            opened = closed - dt.timedelta(seconds=wall)
            if previous is not None and closed <= previous:
                mark(check, 'undetermined', 'duplicate or backwards sample timestamp')
            previous = closed
            intervals.append((opened, closed, row))
        except (ValueError, TypeError, OverflowError):
            mark(check, 'undetermined', 'invalid native compute sample')
    selected = [(a, b, r) for a, b, r in intervals if b > start and a < end]
    check['coverage'].update(sample_count=len(selected), covered_start=stamp(selected[0][0]) if selected else None,
                             covered_end=stamp(selected[-1][1]) if selected else None,
                             gap_tolerance_seconds=1, overlap_tolerance_seconds=1.5)
    if not selected:
        mark(check, 'undetermined', 'no meter coverage')
        return
    if (selected[0][0] - start).total_seconds() > 1 or (end - selected[-1][1]).total_seconds() > 1:
        mark(check, 'undetermined', 'incomplete meter boundary coverage')
    union, cursor = 0.0, start
    for a, b, _ in selected:
        union += max(0, (min(b, end) - max(a, cursor, start)).total_seconds())
        cursor = max(cursor, b)
    check['coverage']['covered_seconds'] = union
    deltas = [(a - prev_b).total_seconds() for (_, prev_b, _), (a, _, _) in zip(selected, selected[1:])]
    check['coverage']['max_gap_seconds'] = max([0] + deltas)
    check['coverage']['max_overlap_seconds'] = max([0] + [-d for d in deltas])
    if any(d > 1 or d < -1.5 for d in deltas):
        mark(check, 'undetermined', 'meter gap or overlap exceeds tolerance')
    idle_count, idle_cpu = 0, 0.0
    for a, b, row in selected:
        lo, hi = max(a, start), min(b, end)
        points = {lo, hi}
        for span in spans:
            points.update(t for t in (span['start'], span['last'], span['upper']) if lo < t < hi)
        for ep in episodes:
            points.update(t for t in (ep['ready_at'], ep['picked_at'] or end) if lo < t < hi)
        points.update(t for t, _, _ in ticks if lo < t < hi)
        states = set()
        ordered = sorted(points)
        for p, q in zip(ordered, ordered[1:]):
            mid = p + (q - p) / 2
            state = live_state(spans, mid)
            if any(ep['ready_at'] <= mid < (ep['picked_at'] or end) for ep in episodes):
                state = 'active'
            elif state == 'alive':
                state = 'active'
            elif state == 'idle':
                left = [t for t in ticks if t[0] <= mid]
                right = [t for t in ticks if t[0] >= mid]
                if (lifecycle_unknown or not left or not right or not left[-1][2] or not right[0][2]
                        or (right[0][0] - left[-1][0]).total_seconds() > CADENCE
                        or left[-1][1].get('planner_status') in ('alive', 'started', 'starting')
                        or left[-1][1].get('planner_ready')):
                    state = 'unknown'
            states.add(state)
        if states == {'active'}:
            continue
        if states != {'idle'}:
            mark(check, 'undetermined', 'mixed or uncertain idle classification')
            continue
        idle_count += 1
        positive = sum(p['cpu'] for p in row['top'] if supervision(p.get('cmd')))
        if positive and (a < start or b > end):
            mark(check, 'undetermined', 'positive CPU crosses window boundary without separate attribution')
            continue
        if positive:
            idle_cpu += positive
            mark(check, 'fail', 'positive identifiable Planner supervision CPU during idle')
            check['evidence'].append(dict(start=stamp(a), end=stamp(b), cpu_seconds=positive,
                                          source=row.get('_src') if source(row) else None))
        elif any(v > 0 for v in row['by'].values()) or any(p['cpu'] > 0 for p in row['top']):
            mark(check, 'undetermined', 'truncated attribution cannot prove zero Planner supervision CPU')
    check['coverage'].update(idle_sample_count=idle_count, identified_idle_cpu_seconds=idle_cpu)
    if not idle_count:
        mark(check, 'undetermined', 'no provable idle interval')


def planner_bar(events, samples, end_day):
    """Evaluate supplied finite histories without mutating inputs or reading state."""
    if not isinstance(end_day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end_day):
        raise ValueError('end_day must be YYYY-MM-DD')
    try:
        day = dt.date.fromisoformat(end_day)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), UTC)
        start = end - dt.timedelta(days=3)
    except (ValueError, OverflowError):
        raise ValueError('invalid end_day') from None
    checks = {name: new_check() for name in ('pickup', 'overlap', 'dialog', 'idle_cpu')}
    rows, errors, incomplete = [], [], set()
    for row in events:
        try:
            if not isinstance(row, dict):
                raise ValueError()
            rows.append((utc(row.get('ts')), row))
        except (ValueError, TypeError):
            errors.append('invalid event row/timestamp')
            incomplete.add(source(row) or '*' if isinstance(row, dict) else '*')
    def order(item):
        ref = item[1].get('_src')
        match = SOURCE.fullmatch(ref) if isinstance(ref, str) else None
        return (item[0], match.group(1) if match else '',
                int(match.group(2)) if match else 0, str(item[1].get('type', '')))
    rows.sort(key=order)
    spans, lifecycle_errors = lifecycle(rows, end)
    ticks = tick_history(rows, start, end, checks['pickup'])
    episodes = pickup_check(ticks, spans, start, end, checks['pickup'])
    overlap_check(spans, start, end, checks['overlap'])
    dialog_check(rows, spans, start, end, checks['dialog'])
    cpu_check(samples, ticks, spans, episodes, start, end, checks['idle_cpu'], bool(errors or lifecycle_errors))
    if errors:
        incomplete_history(checks, incomplete)
    for check in checks.values():
        check['coverage']['event_rows'] = len(rows)
        check['coverage']['planner_count'] = len(spans)
        for reason in errors + lifecycle_errors:
            mark(check, 'undetermined', reason)
    return BarResult(schema_version=1, window=dict(start=stamp(start), end=stamp(end), end_day=end_day),
                     checks=checks, success=all(c['status'] == 'pass' for c in checks.values()))


def parse_lines(text, name):
    result, errors = [], []
    for ordinal, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError()
            result.append({**row, '_src': f'{name}:{ordinal}'})
        except ValueError:
            errors.append('malformed JSON-object source row')
    if text and not text.endswith('\n'):
        errors.append('unterminated JSONL tail')
    return result, errors


def read_events(directory):
    """Compose virtual/live sources, diagnosing incomplete indexes without writes."""
    rows, errors, incomplete = [], [], set()
    try:
        ledger_files.assert_readable(directory)
    except (OSError, ValueError, UnicodeError):
        errors.append('missing or unreadable ledger source')
    try:
        if ledger_files.problems(directory):
            errors.append('damaged or incomplete ledger segment index')
            incomplete.add('*')
        # problems() intentionally grants orphans one hour; completeness cannot.
        segments = Path(directory) / ledger_files.SEGMENT_DIR
        for member in segments.glob('*.jsonl'):
            if not member.with_suffix('.idx.json').is_file():
                errors.append('unindexed ledger segment')
                incomplete.add('*')
        # Verify hashes in memory: verify_segments() would write a cache.
        for index in segments.glob('*.idx.json'):
            try:
                obj = json.loads(index.read_text(encoding='utf-8'))
                data = index.with_name(index.name[:-9] + '.jsonl').read_bytes()
                if hashlib.sha256(data).hexdigest() != obj['sha256']:
                    raise ValueError()
                for member in obj['members']:
                    part = data[member['offset']:member['offset'] + member['length']]
                    if hashlib.sha256(part).hexdigest() != member['sha256']:
                        raise ValueError()
            except (OSError, ValueError, TypeError, KeyError, UnicodeError):
                errors.append('damaged ledger segment integrity')
                incomplete.add('*')
        for name in ledger_files.names(directory):
            try:
                found, invalid = parse_lines(ledger_files.read_text(directory, name, errors='strict'), name)
                rows.extend(found)
                errors.extend(invalid)
                if invalid:
                    incomplete.add(name)
            except (OSError, ValueError, UnicodeError):
                errors.append('unreadable ledger source')
                incomplete.add(name)
    except (OSError, ValueError, UnicodeError):
        errors.append('missing or unreadable ledger directory')
        incomplete.add('*')
    if not rows:
        errors.append('no ledger history')
        incomplete.add('*')
    return rows, sorted(set(errors)), incomplete


def read_samples(paths):
    rows, errors = [], []
    for path in paths:
        try:
            if not Path(path).stat().st_mode & 0o444:
                raise PermissionError()
            opener = gzip.open if str(path).endswith('.gz') else open
            with opener(path, 'rt', encoding='utf-8', errors='strict') as stream:
                # Keep independently usable rows if a later member is damaged.
                for ordinal, line in enumerate(stream, 1):
                    found, invalid = parse_lines(line, Path(path).name)
                    for row in found:
                        row['_src'] = f'{Path(path).name}:{ordinal}'
                    rows.extend(found)
                    errors.extend(invalid)
        except (OSError, ValueError, EOFError, UnicodeError, zlib.error):
            errors.append('missing, unreadable or corrupt sample source')
    return rows, sorted(set(errors))


class BarParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'planner_bar: invalid arguments or date; use --help\n')


def main(argv=None):
    parser = BarParser(description=__doc__)
    parser.add_argument('--end-day', required=True)
    parser.add_argument('--events', default=str(Path(os.environ.get('DOIT_ROOT', str(Path.home() / '.do-it'))) / 'events'))
    parser.add_argument('--samples', action='append')
    args = parser.parse_args(argv)
    try:
        planner_bar([], [], args.end_day)
    except ValueError:
        parser.error('invalid --end-day (expected YYYY-MM-DD)')
    events, event_errors, incomplete = read_events(args.events)
    paths = args.samples or ['/var/tmp/push/compute/samples.jsonl']
    samples, sample_errors = read_samples(paths)
    result = planner_bar(events, samples, args.end_day)
    if event_errors:
        incomplete_history(result['checks'], incomplete)
    for name, check in result['checks'].items():
        for reason in event_errors + (sample_errors if name == 'idle_cpu' else []):
            mark(check, 'undetermined', reason)
    result['checks']['idle_cpu']['coverage']['sources'] = [Path(p).name for p in paths]
    result['success'] = all(c['status'] == 'pass' for c in result['checks'].values())
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
