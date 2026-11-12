#!/usr/bin/env python3
"""Read-only, date-bounded merge-lane measurements with explicit coverage."""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys

UNKNOWN = 'undetermined'
KEYS = ('ci_verify_median_min', 'pg_parity_median_min', 'shard_spread_min',
        'unattributed_parks_per_day', 'failed_ci_share', 'escaped_reds')
UTC = timezone.utc
SHA = re.compile(r'[a-fA-F0-9]{40}')
SELECTION = re.compile(r'CI-SELECTION mode=(full|selected|rerun) reason=([\w-]+) files=(\d+)')
SHARD = re.compile(r'pytest [—–-] shard (\d+) of (\d+)')
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Gap(ValueError):
    """A bounded reason code, never raw source text or transport diagnostics."""


def utc(value):
    if not isinstance(value, str):
        raise Gap('timestamp-invalid')
    try:
        if re.fullmatch(r'\d{4}-\d\d-\d\d', value):
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise Gap('timestamp-invalid') from None


def safe_id(value):
    # Identifiers cannot introduce terminal controls, URLs or credential strings.
    text = str(value)
    return text[:240] if re.fullmatch(r'[\w./:@+ —()\[\],=-]{1,240}', text) and '://' not in text else '[redacted]'


def measurement(formula, *, samples=(), included=(), excluded=(), reasons=(), value=UNKNOWN, **extra):
    reasons = sorted(set(reasons))
    if reasons or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        value = UNKNOWN
    return dict(value=value, coverage='complete' if value != UNKNOWN else 'incomplete',
                formula=formula, sample_count=len(samples), samples=list(samples),
                included=list(included), excluded=list(excluded), reasons=reasons, **extra)


def median(values):
    return statistics.median(values) if values else UNKNOWN


def project_repo(project, root):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project):
        raise Gap('project-invalid')
    repos = (Path(root) / 'repos').resolve()
    repo = (repos / project).resolve()
    if repo.parent != repos or not repo.is_dir():
        raise Gap('project-unknown')
    return repo


def owner_of(repo):
    proc = subprocess.run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'],
                          capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                         r'([\w.-]+/[\w.-]+?)(?:\.git)?/?', proc.stdout.strip())
    if proc.returncode or not match:
        raise Gap('github-origin-unclassified')
    return match[1]


class GitHub:
    """Cached GETs through the existing transport; no runner or artifact writes."""
    def __init__(self, owner):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', owner):
            raise Gap('github-origin-unclassified')
        self.owner, self.cache, self.trace = owner, {}, []

    def read_response(self, argv):
        key = tuple(argv)
        if key not in self.cache:
            import ghlimit
            self.trace.append(list(argv))
            try:
                rc, out, _ = ghlimit.run(argv, wait=False)
            except (OSError, ValueError, subprocess.SubprocessError):
                rc, out = 1, ''
            self.cache[key] = Gap('github-source-unavailable') if rc else out
        value = self.cache[key]
        if isinstance(value, Gap):
            raise value
        return value

    def json(self, path):
        try:
            return json.loads(self.read_response(['gh', 'api', f'repos/{self.owner}/{path}', '--method', 'GET']))
        except (TypeError, json.JSONDecodeError):
            raise Gap('github-json-invalid') from None

    def pages(self, path, key):
        rows, seen, total = [], {}, None
        for page in range(1, 10001):
            obj = self.json(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            if not isinstance(obj, dict) or not isinstance(obj.get(key), list):
                raise Gap('github-pagination-invalid')
            count = obj.get('total_count')
            if type(count) is not int or count < 0 or total is not None and count != total:
                raise Gap('github-population-changing-or-incomplete')
            total = count
            batch = obj[key]
            for row in batch:
                if not isinstance(row, dict) or type(row.get('id')) is not int:
                    raise Gap('github-record-invalid')
                identity = (row['id'], row.get('run_attempt', 1))
                if identity in seen and seen[identity] != row:
                    raise Gap('github-conflicting-record')
                if identity not in seen:
                    seen[identity] = row
                    rows.append(row)
            if len(batch) < 100:
                if len(rows) != total:
                    raise Gap('github-pagination-truncated')
                return rows
        raise Gap('github-pagination-truncated')

    def log(self, rid, num):
        numeric(rid, num)
        return self.read_response(['gh', 'run', 'view', str(rid), '--attempt', str(num),
                          '--repo', self.owner, '--log'])

    def workflow(self, sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise Gap('head-sha-invalid')
        obj = self.json(f'contents/.github/workflows/python-tests.yml?ref={sha}')
        try:
            if obj['encoding'] != 'base64':
                raise ValueError()
            return base64.b64decode(obj['content']).decode('utf-8')
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise Gap('workflow-unavailable') from None


def numeric(*values):
    if any(type(v) is not int or v < 1 for v in values):
        raise Gap('run-attempt-identifier-invalid')


def run_population(gh, since, until):
    evidence = dict(included=[], excluded=[], reasons=[])
    kept, seen = [], set()
    try:
        population = gh.pages('actions/runs', 'workflow_runs')
        for latest in population:
            rid = latest.get('id')
            numeric(rid, latest.get('run_attempt'))
            path = str(latest.get('path', '')).split('@')[0].split('/')[-1]
            if path != 'python-tests.yml' or not str(latest.get('head_branch', '')).startswith('ci-verify/'):
                evidence['excluded'].append(dict(id=rid, reason='workflow-or-branch'))
                continue
            for number in range(1, latest['run_attempt'] + 1):
                identity = (rid, number)
                if identity in seen:
                    continue
                seen.add(identity)
                row = latest if number == latest['run_attempt'] else gh.json(f'actions/runs/{rid}/attempts/{number}')
                if row.get('id') != rid or row.get('run_attempt') != number:
                    raise Gap('attempt-ownership-invalid')
                ref = dict(run_id=rid, attempt=number)
                if row.get('status') != 'completed' or row.get('conclusion') == 'cancelled':
                    evidence['excluded'].append(dict(ref, reason='incomplete-or-cancelled'))
                    continue
                basis = 'created_at' if number == 1 else 'run_started_at'
                start, end = utc(row.get(basis)), utc(row.get('updated_at'))
                if end < start:
                    raise Gap('attempt-duration-negative')
                if not since <= start < until:
                    evidence['excluded'].append(dict(ref, reason='outside-interval'))
                    continue
                row = dict(row, minutes=(end-start).total_seconds()/60, duration_basis=basis)
                kept.append(row)
                evidence['included'].append(dict(ref, minutes=row['minutes'], duration_basis=basis))
    except (Gap, KeyError, TypeError) as exc:
        evidence['reasons'].append(str(exc) if isinstance(exc, Gap) else 'run-metadata-invalid')
    if not kept:
        evidence['reasons'].append('eligible-run-population-empty')
    return kept, evidence


def run_metric(rows, ev):
    return measurement('median(attempt elapsed seconds / 60)', samples=ev['included'],
                       included=ev['included'], excluded=ev['excluded'], reasons=ev['reasons'],
                       value=median([r['minutes'] for r in rows]))


def selection(gh, row):
    rid, number = row['id'], row['run_attempt']
    text = gh.log(rid, number)
    declarations = set()
    for raw in text.splitlines():
        line = ANSI.sub('', raw).strip().split('\t')[-1].strip()
        line = re.sub(r'^\d{4}-\d\d-\d\dT\S+\s+', '', line)
        if not line.startswith('CI-SELECTION'):
            continue
        m = SELECTION.fullmatch(line)
        if not m:
            raise Gap('selection-malformed')
        declarations.add(m.groups())
    if len(declarations) > 1:
        raise Gap('selection-conflicting')
    if declarations:
        mode, reason, files = declarations.pop()
        return dict(mode=mode, reason=reason, files=int(files), run_id=rid, attempt=number,
                    source='attempt-job-log')
    workflow = gh.workflow(row.get('head_sha'))
    if re.search(r'(?m)^\s*select_base\s*:', workflow):
        raise Gap('selection-declaration-missing')
    return dict(mode='full', reason='legacy-workflow-without-select-base', files=None,
                run_id=rid, attempt=number, source='immutable-workflow-and-complete-log')


def job_minutes(row):
    start, end = utc(row.get('started_at')), utc(row.get('completed_at'))
    if end < start:
        raise Gap('job-duration-negative')
    return (end-start).total_seconds()/60


def job_metrics(gh, rows, population):
    pg, spreads, selections, excluded = [], [], [], []
    pg_reasons, spread_reasons = list(population['reasons']), list(population['reasons'])
    for row in rows:
        rid, number = row['id'], row['run_attempt']
        try:
            jobs = gh.pages(f'actions/runs/{rid}/attempts/{number}/jobs', 'jobs')
            if any(j.get('run_id') != rid or j.get('run_attempt') != number for j in jobs):
                raise Gap('job-attempt-ownership-invalid')
            active = []
            for j in jobs:
                if j.get('conclusion') in ('skipped', 'cancelled'):
                    excluded.append(dict(run_id=rid, attempt=number, job_id=j.get('id'), reason='inactive-or-cancelled'))
                elif j.get('status') != 'completed':
                    raise Gap('jobs-incomplete')
                else:
                    active.append(j)
        except Gap as exc:
            pg_reasons.append(str(exc)); spread_reasons.append(str(exc))
            continue
        expected_pg = [j for j in jobs if j.get('name') == 'pg-parity (Postgres)']
        if len(expected_pg) != 1:
            pg_reasons.append('required-pg-job-missing-or-duplicate')
        for j in active:
            if j.get('name') == 'pg-parity (Postgres)':
                try:
                    pg.append(dict(run_id=rid, attempt=number, job_id=j['id'], minutes=job_minutes(j)))
                except Gap as exc:
                    pg_reasons.append(str(exc))
        try:
            sel = selection(gh, row)
            selections.append(sel)
            if sel['mode'] != 'full':
                excluded.append(dict(run_id=rid, attempt=number, reason='selected-or-rerun-only'))
                continue
            shards = []
            for j in active:
                m = SHARD.fullmatch(str(j.get('name')))
                if m:
                    k, n = map(int, m.groups())
                    shards.append((k, n, job_minutes(j)))
            widths = {n for _, n, _ in shards}
            if len(widths) != 1:
                raise Gap('shard-layout-incomplete')
            width = widths.pop()
            if width < 2 or sorted(k for k, _, _ in shards) != list(range(1, width+1)):
                raise Gap('required-shard-missing-or-duplicate')
            values = [v for _, _, v in shards]
            spreads.append(dict(run_id=rid, attempt=number, expected_indices=list(range(1, width+1)),
                                durations_min=values, minutes=max(values)-min(values)))
        except Gap as exc:
            spread_reasons.append(str(exc))
    if not pg:
        pg_reasons.append('executed-pg-population-empty')
    if not spreads:
        spread_reasons.append('full-shard-population-empty')
    return (measurement('median(pg job completed_at - started_at) / 60', samples=pg, included=pg,
                        excluded=excluded, reasons=pg_reasons, value=median([p['minutes'] for p in pg])),
            measurement('median(max(shard wall minutes) - min(shard wall minutes))', samples=spreads,
                        included=spreads, excluded=excluded, reasons=spread_reasons,
                        value=median([p['minutes'] for p in spreads]), selections=selections))


def read_ledger(root):
    """Check raw virtual sources, then use the canonical correction/admission path."""
    import fold
    import ledger_cache
    import ledger_files
    events_dir = Path(root) / 'events'
    reasons, inventory = [], []
    try:
        if not events_dir.is_dir():
            raise Gap('ledger-unavailable')
        ledger_files.assert_readable(events_dir)
        if ledger_files.problems(events_dir):
            reasons.append('ledger-segment-incomplete')
        # Verify immutable segments without the cache-writing verification helper.
        for path, obj, problem in ledger_files._indexes(events_dir):
            if problem or obj is None:
                reasons.append('ledger-segment-incomplete')
            elif hashlib.sha256((path.parent / obj['segment']).read_bytes()).hexdigest() != obj['sha256']:
                reasons.append('ledger-segment-digest-mismatch')
        for name in ledger_files.names(events_dir):
            raw = ledger_files.read_text(events_dir, name)
            lines = raw.splitlines()
            if raw and not raw.endswith('\n'):
                reasons.append('ledger-record-torn')
            digest = hashlib.sha256(raw.encode()).hexdigest()
            inventory.append(dict(source=safe_id(name), lines=len(lines), sha256=digest))
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError()
                    utc(row.get('ts'))
                except (ValueError, TypeError):
                    reasons.append('ledger-record-invalid')
        previous = (fold.ROOT, fold.EVENTS, fold.PROJECT)
        try:
            fold.ROOT, fold.EVENTS, fold.PROJECT = Path(root), events_dir, None
            rows = ledger_cache.tail_refresh()
        finally:
            fold.ROOT, fold.EVENTS, fold.PROJECT = previous
        admitted, rejected = [], []
        prior = defaultdict(list)
        for row in rows:
            # actor is filename-derived by fold, including after corrections.
            rejection = fold.admits(row, row['actor'], prior[row.get('subject')])
            if rejection:
                rejected.append(dict(source=safe_id(row['_src']), reason='actor-not-admitted'))
            else:
                admitted.append(row)
                prior[row.get('subject')].append(row)
        return admitted, dict(reasons=sorted(set(reasons)), sources=inventory, excluded=rejected)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return [], dict(reasons=['ledger-unavailable-or-invalid'], sources=inventory, excluded=[])


def read_log(path):
    requested = Path(path)
    path = requested.resolve()
    for candidate in (requested, path):
        for part in candidate.parts:
            if re.search(r'(?i)(^\.env(?:\.|$)|credentials|secret|token|^\.(?:ssh|aws|gnupg|kube|docker)$|^\.netrc$|\.pem$|^id_rsa|^id_ed25519)', part):
                raise Gap('log-path-prohibited')
        if any(pair == ('.config', 'gh') for pair in zip(candidate.parts, candidate.parts[1:])):
            raise Gap('log-path-prohibited')
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise Gap('log-unavailable') from None


LOG_LINE = re.compile(r'^(\d\d:\d\d:\d\d) (.*)$')
LAUNCH = re.compile(r'LAUNCH (win-(\d{8})-r(\d{4})): (.*)')
RELEVANT = re.compile(r'rehearse on |LAUNCH |CLEAN -> queued:|NOT CLEAN:|full.CI rehearsal|'
                      r'TG: window |precheck:|Set aside:|Holding that batch|cancelled .*CI rehearsal')


def dated_log(text):
    """A launch anchors its own epoch; deadlines and filesystem times never do."""
    epochs, epoch = [], []
    inventory = dict(anchors=0, terminals=0, parsed_lines=0, uncertainty=[], sha256=hashlib.sha256(text.encode()).hexdigest())
    for offset, raw in enumerate(text.splitlines(), 1):
        m = LOG_LINE.fullmatch(raw)
        if not m:
            if RELEVANT.search(raw):
                inventory['uncertainty'].append(dict(line=offset, reason='log-line-malformed'))
            continue
        clock, body = m.groups()
        if body.startswith('rloop start') and epoch:
            epochs.append(epoch); epoch = []
        epoch.append(dict(clock=clock, text=body, line=offset))
        if re.search(r'CLEAN -> queued:|NOT CLEAN:|TG: window .* (?:merged\.|did not merge:)|full-CI rehearsal not green:', body):
            inventory['terminals'] += 1
    if epoch:
        epochs.append(epoch)
    dated = []
    for epoch in epochs:
        anchors = []
        bad = None
        for i, row in enumerate(epoch):
            try:
                datetime.strptime(row['clock'], '%H:%M:%S')
                m = LAUNCH.fullmatch(row['text'])
                if m:
                    if row['clock'][:2] + row['clock'][3:5] != m[3]:
                        raise ValueError()
                    stamp = datetime.strptime(m[2] + 'T' + row['clock'], '%Y%m%dT%H:%M:%S').replace(tzinfo=UTC)
                    anchors.append((i, stamp))
            except ValueError:
                bad = 'log-anchor-or-clock-invalid'
        inventory['anchors'] += len(anchors)
        if not anchors:
            bad = 'log-epoch-unanchored'
        if bad:
            if any(RELEVANT.search(r['text']) for r in epoch):
                inventory['uncertainty'].append(dict(first=epoch[0]['line'], last=epoch[-1]['line'], reason=bad))
            continue
        anchor_index, anchor_stamp = anchors[0]
        epoch[anchor_index]['ts'] = anchor_stamp
        for direction in (-1, 1):
            indices = range(anchor_index-1, -1, -1) if direction == -1 else range(anchor_index+1, len(epoch))
            for index in indices:
                neighbor = epoch[index-direction]
                clock = epoch[index]['clock']
                left, right = (clock, neighbor['clock']) if direction == -1 else (neighbor['clock'], clock)
                rollover = left > right
                if rollover and not (left >= '18:00:00' and right < '06:00:00'):
                    bad = 'log-clock-jump-ambiguous'
                    break
                day = neighbor['ts'].date() + timedelta(days=direction if rollover else 0)
                epoch[index]['ts'] = datetime.fromisoformat(day.isoformat() + 'T' + clock).replace(tzinfo=UTC)
            if bad:
                break
        if not bad and any(epoch[i]['ts'] != stamp for i, stamp in anchors):
            bad = 'log-anchors-inconsistent'
        if bad:
            inventory['uncertainty'].append(dict(first=epoch[0]['line'], last=epoch[-1]['line'], reason=bad))
        else:
            dated.extend(epoch)
    inventory['parsed_lines'] = len(dated)
    return dated, inventory


def members(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r'[ ,]+', str(value).split('(')[0].strip())
    return tuple(sorted(str(v).removeprefix('L-spec-') for v in parts if str(v)))


def lane_spans(records, events, project, source):
    attempts, windows, gaps = [], {}, [r['reason'] for r in source['uncertainty']]
    dispositions = []
    local = None

    def close(item, stamp, state, ref):
        if item.get('end') is not None:
            if item['end'] != stamp or item['state'] != state:
                gaps.append('lane-terminal-conflicting')
            return
        item.update(end=stamp, state=state)
        item['refs'].append(ref)

    for rec in records:
        text, stamp, ref = rec['text'], rec['ts'], rec['line']
        m = re.fullmatch(r'rehearse on ([a-fA-F0-9]+): (.+)', text)
        if m:
            if local and local.get('local_end') is None:
                gaps.append('rehearsal-start-ambiguous')
            group = members(m[2])
            pending = [a for a in attempts if a.get('id') and a['members'] == group and not a.get('local_start') and a.get('end') is None]
            if len(pending) > 1:
                gaps.append('rehearsal-phase-join-ambiguous')
            local = pending[0] if len(pending) == 1 else dict(kind='rehearsal', members=group, start=stamp, refs=[], end=None)
            if local not in attempts:
                attempts.append(local)
            local.update(base=m[1], local_start=stamp, start=min(stamp, local['start']))
            local['refs'].append(ref)
            continue
        m = re.fullmatch(r'full-CI rehearsal (rh-\d{4}-\d{6}) started for (.+)', text)
        if m:
            group = members(m[2])
            matching = [a for a in attempts if a['members'] == group and a.get('end') is None and not a.get('id')]
            if len(matching) > 1 or any(a.get('id') == m[1] for a in attempts):
                gaps.append('rehearsal-phase-join-ambiguous')
                continue
            item = matching[0] if matching else dict(kind='rehearsal', members=group, start=stamp, refs=[], end=None)
            if item not in attempts:
                attempts.append(item)
            item.update(id=m[1], start=min(stamp, item['start']))
            item['refs'].append(ref)
            continue
        if text.startswith(('CLEAN -> queued:', 'NOT CLEAN:')):
            if not local or local.get('local_end') is not None:
                gaps.append('local-terminal-unmatched')
            else:
                state = 'green' if text.startswith('CLEAN') else 'red'
                local['local_end'] = stamp
                if not local.get('id'):
                    close(local, stamp, state, ref)
            continue
        m = LAUNCH.fullmatch(text)
        if m:
            if m[1] in windows:
                gaps.append('window-start-ambiguous')
            else:
                windows[m[1]] = dict(kind='window', id=m[1], members=members(m[4]), start=stamp, end=None, refs=[ref])
            continue
        m = re.match(r'TG: window (win-\d{8}-r\d{4}) (merged\.|did not merge:)', text)
        if m:
            if m[1] not in windows:
                gaps.append('window-terminal-unmatched')
            else:
                close(windows[m[1]], stamp, 'green' if m[2] == 'merged.' else 'red', ref)
            continue
        m = re.match(r'TG: full CI rehearsal of (.+?) was not green:', text)
        if m:
            group = members(m[1])
            matching = [a for a in attempts if a['members'] == group and a.get('id') and a.get('end') is None]
            if len(matching) == 1:
                close(matching[0], stamp, 'red', ref)
            else:
                gaps.append('full-ci-terminal-ambiguous')
        elif text.startswith('full-CI rehearsal not green:'):
            # Only an immediately associated membership-bearing TG line can join this.
            next_row = next((r for r in records if r['line'] == ref + 1), None)
            if not next_row or not re.match(r'TG: full CI rehearsal of .+ was not green:', next_row['text']):
                gaps.append('full-ci-terminal-ambiguous')
        m = re.match(r'cancelled the parallel CI rehearsal (rh-\d{4}-\d{6})', text)
        if m:
            matching = [a for a in attempts if a.get('id') == m[1]]
            if len(matching) == 1:
                close(matching[0], stamp, 'cancelled', ref)
            else:
                gaps.append('full-ci-cancellation-unmatched')
        elif text.startswith('cancelled stale CI rehearsal for '):
            gaps.append('full-ci-cancellation-ambiguous')
        if re.search(r'precheck:.*parked|NOT CLEAN:|full-CI rehearsal not green:|Set aside:|Holding that batch', text):
            dispositions.append(dict(ts=stamp, line=ref, reason='legacy-disposition-requires-reconciliation'))
    for row in events:
        if row.get('project') != project or row.get('type') != 'window-rehearsed':
            continue
        matching = [a for a in attempts if a.get('id') == row.get('subject')]
        if not matching:
            gaps.append('ledger-rehearsal-start-unmatched')
            continue
        item = matching[0]
        if members(row.get('specs', [])) != item['members'] or item.get('base') != row.get('base_sha'):
            gaps.append('rehearsal-ledger-join-invalid')
            continue
        try:
            # Green flake-only verdicts remain green; non-green states stay measured.
            close(item, utc(row.get('ts')), row.get('state'), safe_id(row.get('_src', 'ledger')))
        except Gap as exc:
            gaps.append(str(exc))
    spans = attempts + list(windows.values())
    for item in spans:
        if item.get('end') is None:
            gaps.append('lane-terminal-missing')
        elif item['end'] < item['start']:
            gaps.append('lane-span-negative')
    return spans, sorted(set(gaps)), dispositions


def lane_metric(spans, gaps, since, until, source):
    numerator, denominator, samples = 0., 0., []
    for span in spans:
        if span.get('end') is None or span['end'] < span['start']:
            continue
        seconds = max(0., (min(until, span['end'])-max(since, span['start'])).total_seconds())
        if not seconds:
            continue
        failed = span['kind'] == 'rehearsal' and span['state'] != 'green'
        numerator += seconds if failed else 0
        denominator += seconds
        samples.append(dict(kind=span['kind'], id=safe_id(span.get('id', 'local')),
                            seconds=seconds, not_green=failed, sources=span['refs']))
    reasons = list(gaps)
    if not denominator:
        reasons.append('lane-denominator-zero')
    return measurement('non-green rehearsal seconds / (all rehearsal + window seconds)',
                       samples=samples, included=samples, reasons=reasons, value=numerator/denominator if denominator else UNKNOWN,
                       numerator_seconds=numerator, denominator_seconds=denominator, log_inventory=source)


def park_metric(events, project, since, until, ledger, spans, lane_gaps, dispositions):
    reasons, anchors, parks, released = list(ledger['reasons']), [], [], []
    relevant, replay = [], set()
    malformed = False
    for row in events:
        kind = row.get('type')
        if kind == 'shipped' and row.get('subject') == 'L-spec-2149' and row.get('project') == 'do-it-v2':
            try:
                if not isinstance(row.get('sha'), str) or not SHA.fullmatch(row['sha']):
                    raise Gap('anchor-sha-invalid')
                anchors.append(utc(row.get('ts')))
            except Gap:
                malformed = True
        if kind not in ('batch-member-parked', 'batch-member-released', 'window-opened',
                        'window-dropped', 'window-verified', 'window-refused', 'window-rehearsed', 'shipped'):
            continue
        if not isinstance(row.get('project'), str):
            malformed = True
            continue
        if row['project'] != project:
            continue
        try:
            stamp = utc(row.get('ts'))
        except Gap:
            malformed = True
            continue
        if stamp >= until:
            continue
        if kind == 'batch-member-parked':
            if type(row.get('attributed')) is not bool or not row.get('batch') or not row.get('subject'):
                malformed = True
                continue
            identity = (row['subject'], row['batch'], stamp, row['attributed'], json.dumps(row.get('ids'), sort_keys=True))
            if identity in replay:
                continue
            replay.add(identity)
            parks.append(dict(spec=safe_id(row['subject']), batch=safe_id(row['batch']), ts=stamp.isoformat(),
                              attributed=row['attributed'], source=safe_id(row.get('_src', 'ledger'))))
        elif kind == 'batch-member-released':
            released.append(dict(spec=safe_id(row.get('subject')), batch=safe_id(row.get('batch')),
                                 ts=stamp.isoformat(), source=safe_id(row.get('_src', 'ledger'))))
        relevant.append((stamp, row))
    if not anchors or since < min(anchors):
        reasons.append('park-instrumentation-not-established')
    census = []
    starts = [(stamp, row) for stamp, row in relevant if row['type'] == 'window-opened']
    covered = set()
    for stamp, start in starts:
        ident = start.get('subject')
        terminal = [(t, r) for t, r in relevant if r['type'] == 'window-verified' and r.get('subject') == ident and t >= stamp]
        expected = members(start.get('specs', []))
        if len(terminal) != 1 or not expected:
            malformed = True
            continue
        end, finish = terminal[0]
        if end < since:
            continue
        kept, dropped = members(finish.get('kept', [])), members(finish.get('dropped', []))
        if sorted(kept + dropped) != list(expected):
            malformed = True
        explained = {str(p['spec']).removeprefix('L-spec-') for p in parks + released
                     if p['batch'] == ident and stamp <= utc(p['ts']) <= end}
        for _, r in relevant:
            if r['type'] == 'window-dropped' and r.get('window') == ident and r.get('disposition') in ('requeued', 'released', 'ready'):
                explained.add(str(r.get('subject')).removeprefix('L-spec-'))
        if set(dropped) - explained:
            malformed = True
        covered.add(ident)
        census.append(dict(window=safe_id(ident), expected=list(expected), kept=list(kept), dropped=list(dropped)))
    # A terminal without its started population is an incomplete census.
    if any(r['type'] == 'window-verified' and t >= since and r.get('subject') not in covered for t, r in relevant):
        malformed = True
    for span in spans:
        if span['kind'] != 'rehearsal' or span.get('end') is None or span['end'] < since or span['start'] >= until:
            continue
        group = set(span['members'])
        matched = {str(p['spec']).removeprefix('L-spec-') for p in parks + released
                   if span['start'] <= utc(p['ts']) <= span['end']}
        if span['state'] != 'green' and group - matched:
            malformed = True
        census.append(dict(rehearsal=safe_id(span.get('id', 'local')), members=sorted(group), state=span['state']))
    # Every legacy disposition must have a matching dated member outcome.
    for disp in dispositions:
        if since <= disp['ts'] < until and not any(abs((utc(p['ts'])-disp['ts']).total_seconds()) <= 1 for p in parks + released):
            malformed = True
    if malformed or lane_gaps or not census:
        reasons.append('park-disposition-census-incomplete')
    included = [p for p in parks if since <= utc(p['ts']) < until and p['attributed'] is False]
    daily = Counter(utc(p['ts']).date().isoformat() for p in included)
    day = since.date()
    while datetime.combine(day, datetime.min.time(), UTC) < until:
        daily.setdefault(day.isoformat(), 0)
        day += timedelta(days=1)
    return measurement('unattributed admitted park transitions / (interval seconds / 86400)',
                       samples=included, included=included, excluded=[p for p in parks if p not in included],
                       reasons=reasons, value=len(included)/((until-since).total_seconds()/86400),
                       daily_counts=dict(sorted(daily.items())), releases=released, census=census,
                       instrumentation_anchor=min(anchors).isoformat() if anchors else None)


def gate_runs(text, sha, owner):
    """Read only the named deploy log's SHA-bound gate verdict."""
    decoder, found = json.JSONDecoder(), []
    for match in re.finditer(r'\{', text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get('sha') == sha and isinstance(obj.get('checks'), dict):
            found.append(obj)
    if len(found) != 1:
        raise Gap('release-gate-run-unresolved')
    refs = set()
    for check in found[0]['checks'].values():
        if not isinstance(check, dict) or check.get('state') != 'ran':
            continue
        rid = check.get('run_id')
        if type(rid) is bool or not str(rid).isdigit() or int(rid) < 1:
            raise Gap('release-gate-run-unresolved')
        url = check.get('url', '')
        m = re.fullmatch(r'https://github\.com/' + re.escape(owner) + r'/actions/runs/' + str(rid) + r'(?:/attempts/(\d+))?', url)
        if not m:
            raise Gap('release-gate-run-unresolved')
        refs.add((int(rid), int(m[1]) if m[1] else None))
    if not refs:
        raise Gap('release-gate-run-unresolved')
    return refs


def failure_ids(gh, run):
    """Require full job-log coverage; a small JUnit or successful run is insufficient."""
    import base_red
    text = gh.log(run['id'], run['run_attempt'])
    jobs = gh.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
    required = [j for j in jobs if SHARD.fullmatch(str(j.get('name'))) or j.get('name') == 'pg-parity (Postgres)']
    if not required or not any(j.get('name') == 'pg-parity (Postgres)' for j in required):
        raise Gap('release-failure-coverage-incomplete')
    layout = [tuple(map(int, SHARD.fullmatch(j['name']).groups())) for j in required if SHARD.fullmatch(j['name'])]
    widths = {n for _, n in layout}
    if len(widths) != 1:
        raise Gap('release-failure-coverage-incomplete')
    width = widths.pop()
    if width < 2 or sorted(k for k, _ in layout) != list(range(1, width + 1)):
        raise Gap('release-failure-coverage-incomplete')
    parsed = base_red.parse_log(text)
    ids = set(i for family in base_red.KINDS for i in parsed[family])
    ids.update(base_red.NODE.findall(text))
    if parsed['undetermined']:
        raise Gap('release-failure-coverage-incomplete')
    for job in required:
        if (job.get('run_id') != run['id'] or job.get('run_attempt') != run['run_attempt']
                or job.get('status') != 'completed' or job.get('conclusion') not in ('success', 'failure')):
            raise Gap('release-failure-coverage-incomplete')
        lines = [line for line in text.splitlines() if line.split('\t', 1)[0] == job['name']]
        if not lines:
            raise Gap('release-failure-coverage-incomplete')
        body = '\n'.join(lines)
        if SHARD.fullmatch(job['name']) and not re.search(r'\bSUMMARY pytest_baseline_gate\.py check: (PASS|FAIL)', body):
            raise Gap('release-failure-coverage-incomplete')
        if job['conclusion'] == 'failure' and not base_red.NODE.search(body) and not re.search(r'NEW (?:failed|error|suite-error):|CHANGED:', body):
            raise Gap('release-unnamed-failure')
    return sorted(ids)


def escape_metric(events, project, since, until, ledger, gh, root):
    reasons, releases, occurrences, excluded = list(ledger['reasons']), [], [], []
    escaped = set()
    if project != 'albert-scott':
        reasons.append('release-target-unmapped')
    guards = []
    for row in events:
        if row.get('type') in ('master-full-run', 'base-red-nightly') and row.get('project') == project:
            try:
                stamp = utc(row.get('ts'))
                ids = row.get('ids')
                if row.get('complete') is True and isinstance(ids, list) and all(isinstance(i, str) for i in ids):
                    guards.append((stamp, set(ids)))
            except Gap:
                reasons.append('guard-history-invalid')
    seen = set()
    for row in events:
        if row.get('type') != 'deploy-landed':
            continue
        target = row.get('target')
        if target != 'albert-scott' or project != 'albert-scott':
            excluded.append(dict(source=safe_id(row.get('_src')), reason='release-target-unmapped'))
            # Unknown targets cannot establish a complete release census.
            if project == 'albert-scott' and target not in ('albert-scott',):
                try:
                    if since <= utc(row.get('ts')) < until:
                        reasons.append('release-target-unmapped')
                except Gap:
                    reasons.append('release-census-invalid')
            continue
        try:
            stamp = utc(row.get('ts'))
            if not since <= stamp < until:
                continue
            sha = row.get('sha')
            if not isinstance(sha, str) or not SHA.fullmatch(sha):
                raise Gap('release-sha-invalid')
            identity = (target, sha, stamp)
            if identity in seen:
                continue
            seen.add(identity)
            release = dict(target=target, sha=sha, ts=stamp.isoformat(), source=safe_id(row.get('_src')))
            releases.append(release)
            path = Path(row.get('log', '')).resolve()
            if path.parent != (Path(root) / 'logs').resolve() or not path.name.startswith('deploy-') or path.suffix != '.log':
                raise Gap('release-log-path-prohibited')
            if gh is None:
                raise Gap('release-gate-run-unresolved')
            refs = gate_runs(read_log(path), sha, gh.owner)
            eligible, ids = [], set()
            for rid, number in refs:
                metadata = gh.json(f'actions/runs/{rid}' + (f'/attempts/{number}' if number else ''))
                numeric(metadata.get('id'), metadata.get('run_attempt'))
                if metadata['id'] != rid or number is not None and metadata['run_attempt'] != number:
                    raise Gap('release-gate-attempt-unresolved')
                if str(metadata.get('path', '')).split('@')[0].split('/')[-1] != 'python-tests.yml':
                    continue
                if metadata.get('head_sha') != sha or utc(metadata.get('updated_at')) > stamp or metadata.get('status') != 'completed':
                    raise Gap('release-gate-sha-or-time-mismatch')
                # Without an explicit attempt in the gate URL, retain a proven
                # pre-release latest attempt only; no post-release rerun substitution.
                if selection(gh, metadata)['mode'] != 'full':
                    raise Gap('release-gate-selection-not-full')
                ids.update(failure_ids(gh, metadata))
                eligible.append(dict(run_id=rid, attempt=metadata['run_attempt']))
            if not eligible:
                raise Gap('release-gate-run-unresolved')
            release['runs'] = eligible
            release['ids'] = [safe_id(i) for i in sorted(ids)]
            for ident in sorted(ids):
                covered = any(time < stamp and ident in carried for time, carried in guards)
                if not covered:
                    escaped.add(ident)
                occurrences.append(dict(id=safe_id(ident), id_sha256=hashlib.sha256(ident.encode()).hexdigest(),
                                        ts=stamp.isoformat(), sha=sha, covered=covered))
        except (Gap, OSError, TypeError, KeyError) as exc:
            reasons.append(str(exc) if isinstance(exc, Gap) else 'release-census-invalid')
    if not releases:
        reasons.append('release-population-empty')
    return measurement('distinct exact ids at releases without strictly earlier complete same-project guard',
                       samples=releases, included=releases, excluded=excluded, reasons=reasons,
                       value=len(escaped), occurrences=occurrences, guard_count=len(guards))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's raw bad argument can contain arbitrary secrets/control bytes.
        self.exit(2, 'speed-report: invalid arguments; see --help\n')


def report(project, since, until, root, log_path, gh):
    rows, population = run_population(gh, since, until) if gh else ([], dict(included=[], excluded=[], reasons=['github-origin-unavailable']))
    pg, spread = job_metrics(gh, rows, population)
    events, ledger = read_ledger(root)
    try:
        if log_path is None:
            if project != 'albert-scott':
                raise Gap('project-log-unmapped')
            log_path = '/var/tmp/push/rq/rloop.log'
        records, source = dated_log(read_log(log_path))
    except Gap as exc:
        records, source = [], dict(anchors=0, terminals=0, parsed_lines=0, uncertainty=[dict(reason=str(exc))])
    spans, gaps, dispositions = lane_spans(records, events, project, source)
    gaps.extend(ledger['reasons'])
    metrics = dict(zip(KEYS, (run_metric(rows, population), pg, spread,
                             park_metric(events, project, since, until, ledger, spans, gaps, dispositions),
                             lane_metric(spans, gaps, since, until, source),
                             escape_metric(events, project, since, until, ledger, gh, root))))
    return {k: v['value'] for k, v in metrics.items()}, dict(project=project, since=since.isoformat(), until=until.isoformat(),
                                                          interval='[since,until)', metrics=metrics, ledger=ledger)


def main(argv=None, *, now=None):
    parser = Parser(description=__doc__, epilog='Minutes for CI/PG/spread; parks per elapsed day; failed share is a fraction; '
                    'escapes are distinct test ids. UTC half-open [since,until): dates mean midnight; instants require a timezone. '
                    'Exit 0: all determined, including missed thresholds; exit 2: incomplete or invalid. '
                    'Evidence is JSON on stderr; stdout has exactly six values.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--rloop-log', help='read-only project log override; default albert-scott /var/tmp/push/rq/rloop.log')
    args = parser.parse_args(argv)
    root = Path(os.environ.get('DOIT_ROOT', str(Path.home() / '.do-it')))
    try:
        since, until = utc(args.since), utc(args.until)
        clock = now or datetime.now(UTC)
        if since >= until or until > clock:
            raise Gap('interval-invalid-or-future')
        repo = project_repo(args.project, root)
    except Gap as exc:
        print('speed-report: ' + str(exc), file=sys.stderr)
        return 2
    try:
        gh = GitHub(owner_of(repo))
    except (Gap, OSError, subprocess.SubprocessError):
        gh = None
    values, evidence = report(args.project, since, until, root, args.rloop_log, gh)
    print(json.dumps(values, allow_nan=False, sort_keys=True))
    print(json.dumps(evidence, allow_nan=False, sort_keys=True), file=sys.stderr)
    return 2 if UNKNOWN in values.values() else 0


if __name__ == '__main__':
    sys.exit(main())
