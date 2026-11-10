#!/usr/bin/env python3
"""Offline replay of the rehearsal CSV: old flow versus the short-sweep new flow.

A modeled comparison under stated assumptions, never a measured post-deploy
result. Standard library only; reads one local CSV file and nothing else.
"""
import argparse
import csv
import heapq
import io
import math
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

COLUMNS = ('n', 'era', 'start_utc', 'base', 'specs', 'n_specs', 'local_verdict', 'local_end_utc',
           'local_min', 'local_red_class', 'local_red_tests_or_reason', 'local_attribution', 'ci_id',
           'ci_launch_utc', 'ci_outcome', 'ci_end_utc', 'ci_min', 'first_to_finish', 'window_id',
           'window_launch_utc', 'window_merged_utc', 'window_outcome', 'local_cancelled_ci', 'bucket')
VERDICTS = ('clean', 'red', 'lost')
RED_CLASSES = ('', 'static-preflight', 'guard-tests-only', 'guard+other-tests', 'other-tests',
               'run-broken(timeout/zero-tests)', 'vitest')
CI_OUTCOMES = ('green', 'red', 'killed-by-local-red', 'killed-stale(base moved/restart; no verdict)',
               'no-CI-started(local red first, serial era)', 'refused(cap)',
               'undetermined-infra/preflight')
WINDOW_OUTCOMES = ('', 'merged')
FIRST = ('', 'local', 'CI')
BOOLS = ('True', 'False')
SWEEP_REJECT = ('static-preflight', 'guard-tests-only')
SWEEP_MIN = 3.0
RUNNERS = 3
DAYS = 5
MAX_BYTES = 64 * 1024 * 1024

ASSUMPTIONS = (
    'SD15(1) new service time = 3-minute sweep + modeled ci_min + recomputed queue wait '
    '(three identical CI runners, no local-pass serialization).',
    'SD15(2) only local_red_class static-preflight or guard-tests-only with local_verdict=red '
    'is a sweep reject; its members reappear only in their next observed batches.',
    'SD15(3) actual CI cancellation (ci_outcome=killed-by-local-red) is CI-red under both flows; '
    'local_cancelled_ci=True alone does not cancel a completed green CI run.',
    'SD15(4) guard+other-tests, other-tests, run-broken(timeout/zero-tests) and vitest local reds '
    'pay the full modeled CI duration under the new flow.',
    'SD15(5) missing or censored CI durations use the same-date then five-day median donors; a lost '
    'run without an old endpoint uses the old-service donor median and stays non-merging.',
)
LIMITS = ('Modeled batch elapsed cost per merged spec under assumptions; not measured post-deploy '
          'performance, ready-to-ship median, CI billing or CPU.',
         'The CSV has no ready times, full cancelled-run outcomes/durations or alternative batch '
         'results; recorded ci_min is taken as the full counterfactual duration even when censored.')


def safe(value, limit=60):
    """One terminal-safe line: control and separator characters become spaces."""
    text = ''.join(' ' if unicodedata.category(c)[0] == 'C' or unicodedata.category(c) in ('Zl', 'Zp')
                   else c for c in str(value))
    return text if len(text) <= limit else text[:limit] + '...'


@dataclass
class ReplayResult:
    merged_specs: int
    minutes_per_merged_spec: float
    merged_ids: list
    refused_by_old_but_merged: list
    assumptions: list
    numerator: float = 0.0
    queue_minutes: float = 0.0
    service_minutes: float = 0.0
    imputations: list = field(default_factory=list)
    censored_rows: list = field(default_factory=list)
    dates: tuple = ()
    row_count: int = 0
    attempts: list = field(default_factory=list)
    admitted_rows: list = field(default_factory=list)


class _Row:
    pass


def _bad(row, message):
    raise ValueError(f'row n={safe(row)}: {message}' if row is not None else message)


def _stamp(row, name, value, required=False):
    value = value.strip()
    if not value:
        if required:
            _bad(row, f'{name} is required')
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _bad(row, f'invalid timestamp in {name}: {safe(value)!r}')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _minutes(row, name, value):
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        _bad(row, f'invalid number in {name}: {safe(value)!r}')
    if not math.isfinite(number) or number < 0:
        _bad(row, f'{name} must be finite and not negative: {safe(value)!r}')
    return number


def _parse(raw):
    if not isinstance(raw, dict):
        raise ValueError('row is not a mapping')
    missing = [c for c in COLUMNS if c not in raw]
    if missing:
        raise ValueError('missing required columns: ' + ', '.join(missing))
    if any(raw[c] is None or not isinstance(raw[c], str) for c in COLUMNS):
        raise ValueError(f'row n={safe(raw.get("n"))}: a required cell is missing or not text')
    n_text = raw['n'].strip()
    if not n_text.isascii() or not n_text.isdigit():
        raise ValueError(f'invalid row number n: {safe(n_text)!r}')
    row = _Row()
    row.n = n = int(n_text)
    for name, allowed in (('local_verdict', VERDICTS), ('local_red_class', RED_CLASSES),
                          ('ci_outcome', CI_OUTCOMES), ('window_outcome', WINDOW_OUTCOMES),
                          ('first_to_finish', FIRST), ('local_cancelled_ci', BOOLS)):
        if raw[name] not in allowed:
            _bad(n, f'unknown {name} token {safe(raw[name])!r}')
        setattr(row, name, raw[name])
    row.start = _stamp(n, 'start_utc', raw['start_utc'], True)
    row.local_end = _stamp(n, 'local_end_utc', raw['local_end_utc'])
    row.ci_launch = _stamp(n, 'ci_launch_utc', raw['ci_launch_utc'])
    row.ci_end = _stamp(n, 'ci_end_utc', raw['ci_end_utc'])
    row.merged = _stamp(n, 'window_merged_utc', raw['window_merged_utc'])
    _stamp(n, 'window_launch_utc', raw['window_launch_utc'])
    row.ci_min = _minutes(n, 'ci_min', raw['ci_min'])
    _minutes(n, 'local_min', raw['local_min'])
    row.members = tuple(raw['specs'].split())
    count = raw['n_specs'].strip()
    if not count.isascii() or not count.isdigit() or int(count) != len(set(row.members)) \
            or len(set(row.members)) != len(row.members) or not row.members:
        _bad(n, 'n_specs is inconsistent with the distinct specs listed')
    row.admitted = row.ci_outcome == 'green' and row.window_outcome == 'merged'
    if row.admitted and row.merged is None:
        _bad(n, 'merged row has no window_merged_utc')
    row.rejected = row.local_verdict == 'red' and row.local_red_class in SWEEP_REJECT
    return row


def _select(raw_rows):
    rows = [_parse(raw) for raw in raw_rows]
    seen = set()
    for row in rows:
        if row.n in seen:
            _bad(row.n, 'duplicate n')
        seen.add(row.n)
    if not rows:
        raise ValueError('empty selection: no rows')
    rows.sort(key=lambda r: (r.start, r.n))
    last = max(r.start.date() for r in rows)
    first = last - timedelta(days=DAYS - 1)
    chosen = [r for r in rows if r.start.date() >= first]
    dates = tuple((first + timedelta(days=i)).isoformat() for i in range(DAYS))
    return chosen, dates


def _sec(moment):
    return moment.timestamp()


def _scoped_median(row, donors, label):
    """donors: list of (row n, date, value). Same-date first, then the five-day set."""
    date = row.start.date().isoformat()
    same = [(n, v) for n, d, v in donors if d == date]
    scope, picked = (f'same-date {date}', same) if same else ('five-day', [(n, v) for n, _, v in donors])
    if not picked:
        raise ValueError(f'row n={row.n}: no donor evidence for {label}; undetermined')
    value = statistics.median([v for _, v in picked])
    return {'kind': label, 'row': row.n, 'scope': scope, 'donors': picked, 'value': value}


def _old_endpoint(row):
    if row.admitted:
        return row.merged
    if (row.ci_outcome == 'killed-by-local-red' or row.ci_outcome.startswith('no-CI-started')) \
            and row.local_end is not None:
        return row.local_end
    return row.ci_end or row.local_end


def _old_schedule(rows, release):
    services, donors, lost = {}, [], []
    for row in rows:
        end = _old_endpoint(row)
        if end is None:
            if row.local_verdict == 'lost' and not row.admitted:
                lost.append(row)
                continue
            _bad(row.n, 'no old endpoint (incomplete evidence)')
        seconds = _sec(end) - _sec(row.start)
        if seconds < 0:
            _bad(row.n, 'old endpoint precedes start_utc')
        services[row.n] = seconds / 60.0
        if seconds > 0:
            donors.append((row.n, row.start.date().isoformat(), seconds / 60.0))
    imputed = []
    for row in lost:
        found = _scoped_median(row, donors, 'old-service')
        services[row.n] = found['value']
        imputed.append(found)
    finish, last, out = {}, {}, []
    for row in rows:
        end = _old_endpoint(row)
        finish[row.n] = _sec(end) if end is not None else _sec(row.start) + services[row.n] * 60.0
        elig = max([release[m] for m in row.members] + [finish[last[m]] for m in row.members if m in last])
        queue = max(0.0, (_sec(row.start) - elig) / 60.0)
        for m in row.members:
            last[m] = row.n
        out.append({'row': row.n, 'eligibility': elig, 'queue': queue, 'service': services[row.n]})
    return out, imputed


def _new_schedule(rows, release):
    donors = [(r.n, r.start.date().isoformat(), r.ci_min) for r in rows
              if r.ci_outcome in ('green', 'red') and r.ci_launch and r.ci_end
              and r.ci_min is not None and r.ci_min > 0]
    by_n = {r.n: r for r in rows}
    prev = {}
    last, waiting, successors = {}, {}, {}
    for row in rows:
        prev[row.n] = sorted({last[m] for m in row.members if m in last})
        for m in row.members:
            last[m] = row.n
        waiting[row.n] = len(prev[row.n])
        for p in prev[row.n]:
            successors.setdefault(p, []).append(row.n)
    imputed, info, finish = [], {}, {}
    heap = []

    def push(n):
        row = by_n[n]
        elig = max([release[m] for m in row.members] + [finish[p] for p in prev[n]])
        info[n] = {'row': n, 'eligibility': elig, 'sweep_start': elig}
        heapq.heappush(heap, (elig + SWEEP_MIN * 60.0, n))

    def complete(n, at):
        finish[n] = at
        info[n]['completion'] = at
        for s in successors.get(n, ()):
            waiting[s] -= 1
            if waiting[s] == 0:
                push(s)

    for row in rows:
        if waiting[row.n] == 0:
            push(row.n)
    free = [float('-inf')] * RUNNERS
    while heap:
        swept, n = heapq.heappop(heap)
        row = by_n[n]
        if row.rejected:
            info[n].update(queue=0.0, service=SWEEP_MIN, ci_start=None)
            complete(n, swept)
            continue
        if row.ci_min is not None:
            duration = row.ci_min
        else:
            found = _scoped_median(row, donors, 'ci-duration')
            imputed.append(found)
            duration = found['value']
        tail = 0.0
        if row.admitted:
            if row.ci_end is None:
                _bad(n, 'merged row has no ci_end_utc')
            tail = _sec(row.merged) - _sec(row.ci_end)
            if tail < 0:
                _bad(n, 'merge precedes ci_end_utc')
        slot = min(range(RUNNERS), key=lambda i: free[i])
        start = max(swept, free[slot])
        free[slot] = start + duration * 60.0
        info[n].update(queue=(start - swept) / 60.0, service=SWEEP_MIN + duration + tail / 60.0,
                       ci_start=start)
        complete(n, start + duration * 60.0 + tail)
    return [info[r.n] for r in rows], imputed


def _new_admitted(rows):
    return [r for r in rows if r.admitted and not r.rejected]


def _censored(rows):
    return sorted(r.n for r in rows if r.ci_outcome.startswith('killed') and r.ci_min is not None
                  and not r.rejected)


def replay(rows, flow):
    if flow not in ('old', 'new'):
        raise ValueError("flow must be 'old' or 'new'")
    chosen, dates = _select(rows)
    release = {}
    for row in chosen:
        for m in row.members:
            release.setdefault(m, _sec(row.start))
    if flow == 'old':
        schedule, imputed = _old_schedule(chosen, release)
        admitted = [r for r in chosen if r.admitted]
    else:
        schedule, imputed = _new_schedule(chosen, release)
        admitted = _new_admitted(chosen)
    origin = _sec(chosen[0].start)
    attempts = []
    for item in schedule:
        entry = {k: v for k, v in item.items()}
        for key in ('eligibility', 'sweep_start', 'ci_start', 'completion'):
            if entry.get(key) is not None:
                entry[key] = (entry[key] - origin) / 60.0
        attempts.append(entry)
    ids = sorted({m for r in admitted for m in r.members})
    refused = [{'row': r.n, 'spec': m} for r in admitted if not r.admitted for m in r.members]
    if not ids:
        raise ValueError('zero merged specs: no comparison')
    queue = math.fsum(i['queue'] for i in schedule)
    service = math.fsum(i['service'] for i in schedule)
    total = queue + service
    return ReplayResult(len(ids), total / len(ids), ids, refused, list(ASSUMPTIONS), total, queue,
                        service, imputed, _censored(chosen) if flow == 'new' else [], dates,
                        len(chosen), attempts, sorted(r.n for r in admitted))


def read_csv(path):
    with open(path, 'rb') as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('csv file is too large')
    try:
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
        header = reader.fieldnames or []
        if len(set(header)) != len(header):
            raise ValueError('duplicate csv header names')
        rows = []
        for raw in reader:
            if None in raw or any(v is None for v in raw.values()):
                raise ValueError(f'csv line {reader.line_num}: wrong number of fields')
            rows.append(raw)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError('unreadable csv: ' + safe(exc)) from None
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog='doit rehearsal-replay', description=__doc__)
    parser.add_argument('--csv', required=True, help='rehearsal CSV (the 24-column report schema)')
    args = parser.parse_args(argv)
    started = time.process_time()
    try:
        raw = read_csv(args.csv)
        old, new = replay(raw, 'old'), replay(raw, 'new')
    except (ValueError, OSError) as exc:
        print('rehearsal-replay: invalid input: ' + safe(exc, 300), file=sys.stderr)
        return 2
    out = ['rehearsal-replay: modeled comparison under assumptions, not measured post-deploy performance',
           f'csv={safe(args.csv, 300)}',
           f'selected_dates={old.dates[0]}..{old.dates[-1]} count={len(old.dates)} rows={old.row_count}']
    for label, res in (('old', old), ('new', new)):
        out.append(f'{label} minutes_per_merged_spec={res.minutes_per_merged_spec!r} '
                   f'merged_specs={res.merged_specs} numerator={res.numerator!r} '
                   f'queue={res.queue_minutes!r} service={res.service_minutes!r}')
    out.extend(f'assumption: {text}' for text in new.assumptions)
    out.extend(f'limitation: {text}' for text in LIMITS)
    out.append(f'imputations: old={len(old.imputations)} new={len(new.imputations)}')
    for flow, res in (('old', old), ('new', new)):
        groups = {}
        for item in res.imputations:
            key = (item['kind'], item['scope'], item['value'], tuple(item['donors']))
            groups.setdefault(key, []).append(item['row'])
        for (kind, scope, value, donors), ids in groups.items():
            out.append(f'imputed {flow} {kind} rows={",".join(map(str, ids))} scope={scope} '
                       f'value={value!r} donors[{len(donors)}] row=value: '
                       + ', '.join(f'{n}={v!r}' for n, v in donors))
    out.append('censored_ci_rows (recorded ci_min used as full duration): '
               + (' '.join(map(str, new.censored_rows)) or 'none'))
    bad = new.refused_by_old_but_merged
    out.append(f'refused_by_old_but_merged={len(bad)}')
    out.extend(f'unsafe: row={i["row"]} spec={safe(i["spec"])}' for i in bad)
    better = math.isfinite(new.minutes_per_merged_spec) and math.isfinite(old.minutes_per_merged_spec) \
        and new.minutes_per_merged_spec < old.minutes_per_merged_spec
    out.append('result: ' + ('PASS new < old, no merge the old flow refused' if better and not bad
                            else 'FAIL ' + ('safety violation' if bad else 'new is not lower than old')))
    out.append(f'replay_cpu_seconds={time.process_time() - started:.3f} '
               '(process CPU; modeled minutes above are not CPU)')
    print('\n'.join(out))
    return 0 if better and not bad else 1


if __name__ == '__main__':
    sys.exit(main())
