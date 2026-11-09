#!/usr/bin/env python3
"""Offline regression cases for the date-bounded speed report."""
import os
import tempfile
from pathlib import Path

# Isolate before importing any runtime reader. The real shared quota file is never
# read or compared: other box processes use gh, so it moves independently of this suite.
import hashlib
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def quota_snapshot():
    # Digest of the isolated temporary quota state (GH_LIMIT_STATE) and its lock.
    quota = Path(os.environ['GH_LIMIT_STATE'])
    return {p: digest(p) for p in (quota, quota.with_name(quota.name + '.lock'))}
_scratch = tempfile.TemporaryDirectory(prefix='speed-report-tests-')
_root = Path(_scratch.name)
for key, value in {'HOME': str(_root / 'home'), 'DOIT_ROOT': str(_root / 'doit'),
                   'GH_LIMIT_STATE': str(_root / 'quota.json'),
                   'DOIT_REAL_GH': str(_root / 'fake-gh'),
                   'XDG_CONFIG_HOME': str(_root / 'config'),
                   'XDG_CACHE_HOME': str(_root / 'cache'),
                   'XDG_STATE_HOME': str(_root / 'state')}.items():
    os.environ[key] = value
os.environ.pop('DOIT_LEDGER_FILE', None)
os.environ.pop('DOIT_PROJECT', None)
for _key in list(os.environ):
    if _key.startswith('SUPABASE_DB_URL') or _key in ('DATABASE_URL', 'TEST_DB'):
        os.environ.pop(_key, None)
(_root / 'fake-gh').write_text('#!/bin/sh\nexit 73\n')
(_root / 'fake-gh').chmod(0o700)
(_root / 'gh').symlink_to(_root / 'fake-gh')
(_root / 'claude').write_text('#!/bin/sh\nexit 73\n')
(_root / 'claude').chmod(0o700)
os.environ['PATH'] = str(_root) + os.pathsep + os.defpath

import base64
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import unittest
import socket
from datetime import timedelta
from unittest.mock import patch

import speed_report as sr

REPO = Path(__file__).resolve().parent.parent
SINCE = sr.utc('2026-11-03')
UNTIL = sr.utc('2026-11-05')


def setUpModule():
    # Fail closed on accidental direct network use in every acceptance class.
    _network = patch.object(socket, 'socket', side_effect=AssertionError('live network prohibited'))
    _network.start()
    unittest.addModuleCleanup(_network.stop)
    popen = subprocess.Popen
    def local_process(argv, *args, **kwargs):
        if kwargs.get('shell') or not isinstance(argv, (list, tuple)):
            raise AssertionError('shell transport prohibited')
        executable = str(argv[0])
        path = Path(executable).resolve()
        allowed = (executable == 'git' or path == Path('/usr/bin/python3').resolve()
                   or path == REPO / 'doit' or path.is_relative_to(_root) and path.name == 'fake-gh'
                   or list(argv) == ['claude', '--version'] and shutil.which('claude') == str(_root / 'claude'))
        if not allowed:
            raise AssertionError('nonlocal transport prohibited')
        return popen(argv, *args, **kwargs)
    _process = patch.object(subprocess, 'Popen', side_effect=local_process)
    _process.start()
    unittest.addModuleCleanup(_process.stop)


def attempt(rid=1, minutes=10, **kw):
    row = dict(id=rid, run_attempt=1, path='.github/workflows/python-tests.yml',
               head_branch='ci-verify/fixture', head_sha='a' * 40, status='completed',
               conclusion='success', created_at='2026-11-03T01:00:00Z',
               run_started_at='2026-11-03T01:01:00Z',
               updated_at=(SINCE + timedelta(hours=1, minutes=minutes)).isoformat())
    return dict(row, **kw)


def job(name, minutes, rid=1, jid=1, **kw):
    return dict(dict(id=jid, run_id=rid, run_attempt=1, name=name, status='completed',
                     conclusion='success', started_at='2026-11-03T02:00:00Z',
                     completed_at=(SINCE + timedelta(hours=2, minutes=minutes)).isoformat()), **kw)


def event(kind, stamp='2026-11-03T01:00:00Z', project='albert-scott', subject='fixture', **kw):
    return dict(type=kind, ts=stamp, project=project, subject=subject, _src='L-executor-fixture.jsonl:1', **kw)


class FakeGH:
    def __init__(self, runs=(), jobs=None, logs=None, workflows=None, extra=None):
        self.runs, self.job_rows = list(runs), jobs or {}
        self.logs, self.workflows = logs or {}, workflows or {}
        self.extra, self.calls = extra or {}, []

    def json(self, path):
        self.calls.append(path)
        if path in self.extra:
            value = self.extra[path]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        import re
        m = re.search(r'/attempts/(\d+)$', path)
        if m:
            rid = int(path.split('/runs/')[1].split('/')[0])
            match = next((r for r in self.runs if r['id'] == rid and r['run_attempt'] == int(m[1])), None)
            if match is None:
                raise sr.Gap('attempt-metadata-unavailable')
            return copy.deepcopy(match)
        raise AssertionError('unexpected API GET ' + path)

    def pages(self, path, key):
        self.calls.append(path)
        if key == 'workflow_runs':
            latest = {}
            for row in self.runs:
                if row['id'] not in latest or latest[row['id']]['run_attempt'] < row['run_attempt']:
                    latest[row['id']] = row
            return copy.deepcopy(list(latest.values()))
        rid, rest = path.split('/runs/')[1].split('/attempts/')
        num = int(rest.split('/')[0])
        value = self.job_rows.get((int(rid), num), sr.Gap('jobs-unavailable'))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def log(self, rid, num):
        self.calls.append(('log', rid, num))
        value = self.logs.get((rid, num), 'CI-SELECTION mode=full reason=fixture files=0')
        if isinstance(value, Exception):
            raise value
        return value

    def workflow(self, sha):
        value = self.workflows.get(sha, sr.Gap('workflow-unavailable'))
        if isinstance(value, Exception):
            raise value
        return value


class RunTimingTests(unittest.TestCase):
    def test_even_median_and_failure(self):
        gh = FakeGH([attempt(4, 40), attempt(2, 20, conclusion='failure'), attempt(1, 10), attempt(3, 30)])
        rows, ev = sr.run_population(gh, SINCE, UNTIL)
        self.assertEqual(sr.run_metric(rows, ev)['value'], 25)
        self.assertEqual(len(ev['included']), 4)
        print('durations [40,20(failed),10,30] => median 25 minutes')

    def test_rerun_uses_start_and_prior_attempts(self):
        gh = FakeGH([attempt(1, created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:10:00Z'),
                     attempt(1, run_attempt=2, created_at='2026-11-01T00:00:00Z',
                             run_started_at='2026-11-03T12:00:00Z', updated_at='2026-11-03T12:20:00Z')])
        rows, ev = sr.run_population(gh, SINCE, UNTIL)
        self.assertEqual(sr.run_metric(rows, ev)['value'], 20)
        self.assertEqual(rows[0]['duration_basis'], 'run_started_at')
        self.assertEqual(len(ev['excluded']), 1)

    def test_boundaries_exclusions_and_invalid_population(self):
        rows = [attempt(1, created_at='2026-11-03T00:00:00Z'),
                attempt(2, created_at='2026-11-05T00:00:00Z', updated_at='2026-11-05T00:10:00Z'),
                attempt(3, head_branch='master'), attempt(4, status='in_progress'),
                attempt(5, conclusion='cancelled'), attempt(6, path='other.yml')]
        kept, ev = sr.run_population(FakeGH(rows), SINCE, UNTIL)
        self.assertEqual([r['id'] for r in kept], [1])
        self.assertEqual(len(ev['excluded']), 5)
        for changes in ({'updated_at': None}, {'updated_at': '2026-11-02T00:00:00Z'}, {'run_attempt': 0}):
            with self.subTest(changes=changes):
                kept, ev = sr.run_population(FakeGH([attempt(**changes)]), SINCE, UNTIL)
                self.assertEqual(sr.run_metric(kept, ev)['value'], sr.UNKNOWN)

    def test_paginated_population_dedup_and_truncation(self):
        rows = [attempt(n) for n in range(1, 102)]
        pages = [dict(total_count=101, workflow_runs=rows[:100]),
                 dict(total_count=101, workflow_runs=[rows[0], rows[-1]])]
        gh = sr.GitHub('fixture/repo')
        with patch.object(gh, 'json', side_effect=pages):
            found = gh.pages('actions/runs', 'workflow_runs')
        self.assertEqual(len(found), 101)
        with patch.object(gh, 'json', return_value=pages[1]):
            with self.assertRaises(sr.Gap):
                gh.pages('actions/runs', 'workflow_runs')
        with patch.object(gh, 'json', side_effect=sr.Gap('unavailable')):
            rows, ev = sr.run_population(gh, SINCE, UNTIL)
        self.assertEqual(sr.run_metric(rows, ev)['value'], sr.UNKNOWN)


def jobs_for(rid=1, durations=(10, 12, 13, 14), pg=12, number=1):
    result = [job(f'pytest — shard {i} of {len(durations)}', d, rid, i, run_attempt=number)
              for i, d in enumerate(durations, 1)]
    return result + [job('pg-parity (Postgres)', pg, rid, 99, run_attempt=number)]


class JobTimingTests(unittest.TestCase):
    def measure(self, gh):
        rows, pop = sr.run_population(gh, SINCE, UNTIL)
        return sr.job_metrics(gh, rows, pop)

    def test_hand_results_and_job_exclusions(self):
        jobs1 = jobs_for() + [job('plan', 1, jid=101), job('pytest', 60, jid=102),
                              job('pytest priority-stage', 50, jid=103),
                              job('pytest — shard 5 of 5', 0, jid=104, conclusion='skipped')]
        gh = FakeGH([attempt(1), attempt(2)], jobs={(1, 1): jobs1, (2, 1): jobs_for(2, (20, 21), 18)})
        pg, spread = self.measure(gh)
        self.assertEqual(pg['value'], 15)
        self.assertEqual(spread['value'], 2.5)
        self.assertEqual([r['minutes'] for r in spread['samples']], [4, 1])
        self.assertTrue(any(e['reason'] == 'inactive-or-cancelled' for e in spread['excluded']))
        print('PG [12,18] => 15; job spreads [4,1] => 2.5 minutes; plan/fan-in/inactive excluded')

    def test_missing_duplicate_wrong_owner_and_timestamps(self):
        good = jobs_for()
        for bad in (good[1:], good + [dict(good[0], id=110)],
                    [dict(j, run_attempt=2) for j in good],
                    [dict(good[0], started_at=None)] + good[1:]):
            with self.subTest(jobs=bad):
                _, spread = self.measure(FakeGH([attempt()], jobs={(1, 1): bad}))
                self.assertEqual(spread['value'], sr.UNKNOWN)
        gh = FakeGH([attempt()], jobs={(1, 1): sr.Gap('jobs-unavailable')})
        rows, pop = sr.run_population(gh, SINCE, UNTIL)
        self.assertEqual(sr.run_metric(rows, pop)['value'], 10)
        self.assertTrue(all(m['value'] == sr.UNKNOWN for m in sr.job_metrics(gh, rows, pop)))

    def test_missing_pg_in_one_attempt_is_incomplete(self):
        gh = FakeGH([attempt(1), attempt(2)], jobs={(1, 1): jobs_for(), (2, 1): jobs_for(2)[:-1]})
        pg, spread = self.measure(gh)
        self.assertEqual(pg['value'], sr.UNKNOWN)
        self.assertIn('required-pg-job-missing-or-duplicate', pg['reasons'])
        self.assertEqual(spread['value'], 4)

    def test_selection_declarations_and_immutable_fallback(self):
        cases = [('step\t2026-11-03T00:00:00Z \x1b[32mCI-SELECTION mode=full reason=fixture files=0\x1b[0m', {}, 4),
                 ('CI-SELECTION mode=full reason=x files=1\nCI-SELECTION mode=full reason=x files=1', {}, 4),
                 ('CI-SELECTION mode=selected reason=paths files=2', {}, sr.UNKNOWN),
                 ('CI-SELECTION mode=rerun reason=retry files=2', {}, sr.UNKNOWN),
                 ('complete legacy logs', {'a' * 40: 'name: Python Tests\non: push'}, 4),
                 ('complete legacy logs', {'a' * 40: 'inputs:\n  select_base:\n    default: null'}, sr.UNKNOWN),
                 ('complete legacy logs', {}, sr.UNKNOWN),
                 ('CI-SELECTION malformed', {}, sr.UNKNOWN),
                 ('CI-SELECTION mode=full reason=x files=0\nCI-SELECTION mode=selected reason=x files=0', {}, sr.UNKNOWN)]
        for text, workflows, expected in cases:
            with self.subTest(text=text):
                gh = FakeGH([attempt()], jobs={(1, 1): jobs_for()}, logs={(1, 1): text}, workflows=workflows)
                pg, spread = self.measure(gh)
                self.assertEqual(pg['value'], 12)
                self.assertEqual(spread['value'], expected)
                if spread['selections']:
                    self.assertEqual(spread['selections'][0]['run_id'], 1)
                    self.assertEqual(spread['selections'][0]['attempt'], 1)


def lane(text, events=()):
    records, source = sr.dated_log(text)
    spans, gaps, dispositions = sr.lane_spans(records, events, 'albert-scott', source)
    return spans, gaps, dispositions, source


CLEAN_LOG = ('00:00:00 rehearse on aaaaaaa: 1\n'
             '00:20:00 NOT CLEAN: red\n'
             '00:30:00 rehearse on bbbbbbb: 2\n'
             '01:30:00 CLEAN -> queued: 2\n'
             '01:40:00 LAUNCH win-20261103-r0140: 2\n'
             '02:00:00 TG: window win-20261103-r0140 merged. shipped=2 dropped=\n')


class LaneShareTests(unittest.TestCase):
    def test_hand_fraction(self):
        spans, gaps, _, source = lane(CLEAN_LOG)
        metric = sr.lane_metric(spans, gaps, SINCE, UNTIL, source)
        self.assertEqual(metric['value'], .2)
        self.assertEqual(metric['numerator_seconds'], 1200)
        self.assertEqual(metric['denominator_seconds'], 6000)
        self.assertEqual(source['anchors'], 1)
        self.assertEqual(source['terminals'], 3)
        print('20 red + 60 green rehearsal + 20 window minutes => failed share 0.2')

    def test_boundary_clip_midnight_and_overlap(self):
        log = ('23:50:00 rehearse on aaaaaaa: 1\n'
               '23:55:00 LAUNCH win-20261102-r2355: 2\n'
               '00:10:00 NOT CLEAN: undetermined\n'
               '00:15:00 TG: window win-20261102-r2355 did not merge: red\n')
        spans, gaps, _, source = lane(log)
        metric = sr.lane_metric(spans, gaps, SINCE, UNTIL, source)
        self.assertEqual(metric['value'], .4)
        self.assertEqual(metric['denominator_seconds'], 1500)

    def test_full_ci_parallel_phase_and_ledger_terminal(self):
        log = ('00:00:00 full-CI rehearsal rh-1103-000000 started for 1 (in parallel with the local pass)\n'
               '00:01:00 rehearse on aaaaaaa: 1\n'
               '00:10:00 CLEAN -> queued: 1 (full CI pending)\n'
               '00:30:00 LAUNCH win-20261103-r0030: 1\n'
               '00:40:00 TG: window win-20261103-r0030 merged. shipped=1 dropped=\n')
        terminal = event('window-rehearsed', '2026-11-03T00:20:00Z', subject='rh-1103-000000',
                         specs=['L-spec-1'], base_sha='aaaaaaa', state='red', rehearsal=True)
        spans, gaps, _, source = lane(log, [terminal, dict(terminal)])
        self.assertFalse(gaps)
        self.assertEqual(sr.lane_metric(spans, gaps, SINCE, UNTIL, source)['value'], 2/3)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]['start'], SINCE)

    def test_cancellation_and_serial_phases(self):
        log = ('00:00:00 rehearse on aaaaaaa: 1\n'
               '00:01:00 full-CI rehearsal rh-1103-000100 started for 1\n'
               '00:02:00 NOT CLEAN: local red\n'
               '00:03:00 cancelled the parallel CI rehearsal rh-1103-000100 (local pass red)\n'
               '00:04:00 LAUNCH win-20261103-r0004: 2\n'
               '00:05:00 TG: window win-20261103-r0004 did not merge: refused\n')
        spans, gaps, _, source = lane(log)
        self.assertFalse(gaps)
        self.assertEqual(sr.lane_metric(spans, gaps, SINCE, UNTIL, source)['value'], .75)

    def test_unanchored_restart_clock_ambiguity_and_unmatched(self):
        cases = [CLEAN_LOG.replace('01:40:00 LAUNCH win-20261103-r0140: 2\n', ''),
                 CLEAN_LOG.replace('01:30:00 CLEAN', '00:10:00 CLEAN'),
                 CLEAN_LOG.replace('01:30:00 CLEAN -> queued: 2\n', ''),
                 '23:00:00 rloop start (until 2026-11-06)\n23:05:00 rehearse on aaaaaaa: 1\n' +
                 '00:00:00 rloop start (until 2026-11-06)\n' + CLEAN_LOG,
                 CLEAN_LOG + '02:01:00 full-CI rehearsal not green: failure\n']
        for log in cases:
            with self.subTest(log=log):
                spans, gaps, _, source = lane(log)
                self.assertTrue(gaps)
                self.assertEqual(sr.lane_metric(spans, gaps, SINCE, UNTIL, source)['value'], sr.UNKNOWN)


ANCHOR = event('shipped', '2026-11-02T00:00:00Z', 'do-it-v2', 'L-spec-2149', sha='a' * 40)


def clean_census():
    return [ANCHOR,
            event('window-opened', '2026-11-03T00:00:00Z', subject='win-fixture', specs=['L-spec-1', 'L-spec-2'], base='a' * 40),
            event('window-verified', '2026-11-04T12:00:00Z', subject='win-fixture', sha='b' * 40,
                  kept=['L-spec-1', 'L-spec-2'], dropped=[], runs=[])]


class ParkTests(unittest.TestCase):
    def test_segmented_ledger_and_torn_or_unreadable_source(self):
        import ledger_files
        with tempfile.TemporaryDirectory(dir=_root) as directory:
            root = Path(directory)
            events = root / 'events'
            segment_dir = events / ledger_files.SEGMENT_DIR
            segment_dir.mkdir(parents=True)
            data = (json.dumps(ANCHOR) + '\n').encode()
            sha = hashlib.sha256(data).hexdigest()
            (segment_dir / 'fixture.jsonl').write_bytes(data)
            (segment_dir / 'fixture.idx.json').write_text(json.dumps(dict(version=1,
                segment='fixture.jsonl', bytes=len(data), sha256=sha,
                members=[dict(name='L-executor-fixture.jsonl', offset=0, length=len(data),
                              lines=1, sha256=sha, ends_nl=True)])))
            rows, evidence = sr.read_ledger(root)
            self.assertEqual([r['subject'] for r in rows], ['L-spec-2149'])
            self.assertEqual(evidence['reasons'], [])
            live = events / 'L-executor-live.jsonl'
            live.write_text(json.dumps(event('window-opened')))
            _, evidence = sr.read_ledger(root)
            self.assertIn('ledger-record-torn', evidence['reasons'])
            live.write_text('{invalid\n')
            _, evidence = sr.read_ledger(root)
            self.assertIn('ledger-record-invalid', evidence['reasons'])
            with patch.object(ledger_files, 'assert_readable', side_effect=OSError()):
                _, evidence = sr.read_ledger(root)
            self.assertIn('ledger-unavailable-or-invalid', evidence['reasons'])

    def test_reader_refreshes_cache_without_project_prefilter(self):
        import fold
        import ledger_cache
        with tempfile.TemporaryDirectory(dir=_root) as directory:
            root = Path(directory)
            events = root / 'events'
            events.mkdir()
            path = events / 'L-executor-fixture.jsonl'
            path.write_text(json.dumps(ANCHOR) + '\n')
            original = (fold.ROOT, fold.EVENTS, fold.PROJECT)
            with patch.object(ledger_cache, 'tail_refresh', wraps=ledger_cache.tail_refresh) as refresh:
                with patch.object(fold, 'PROJECT', 'albert-scott'):
                    rows, evidence = sr.read_ledger(root)
                    self.assertEqual([r['subject'] for r in rows], ['L-spec-2149'])
                    self.assertEqual(fold.PROJECT, 'albert-scott')
                self.assertEqual(refresh.call_count, 1)
            self.assertEqual((fold.ROOT, fold.EVENTS, fold.PROJECT), original)
            self.assertEqual(evidence['reasons'], [])

    def measure(self, events, **kw):
        return sr.park_metric(events, 'albert-scott', SINCE, UNTIL, dict(reasons=kw.pop('reasons', [])),
                              kw.pop('spans', []), kw.pop('gaps', []), kw.pop('dispositions', []))

    def test_occurrences_release_dedup_and_daily_counts(self):
        events = clean_census()
        first = event('batch-member-parked', subject='L-spec-1', batch='win-fixture', attributed=False, ids=[])
        second = event('batch-member-parked', '2026-11-04T01:00:00Z', subject='L-spec-2', batch='win-fixture', attributed=False, ids=[])
        events += [first, dict(first), second,
                   event('batch-member-released', '2026-11-04T02:00:00Z', subject='L-spec-1', batch='win-fixture', why='ready'),
                   event('batch-member-parked', subject='L-spec-3', batch='win-fixture', attributed=True, ids=['x']),
                   event('batch-member-parked', project='other', batch='win-other', attributed=False, ids=[])]
        metric = self.measure(events)
        self.assertEqual(metric['value'], 1)
        self.assertEqual(metric['daily_counts'], {'2026-11-03': 1, '2026-11-04': 1})
        self.assertEqual(len(metric['releases']), 1)
        print('two real unattributed transitions / two days => 1/day; release subtracts nothing')

    def test_anchor_clean_zero_and_boundaries(self):
        self.assertEqual(self.measure(clean_census())['value'], 0)
        metric = self.measure(clean_census() + [event('batch-member-parked', '2026-11-03T00:00:00Z', batch='win-fixture', attributed=False),
                                               event('batch-member-parked', '2026-11-05T00:00:00Z', batch='win-fixture', attributed=False)])
        self.assertEqual(metric['value'], .5)
        for events in (clean_census()[1:], [dict(ANCHOR, ts='2026-11-04T00:00:00Z')] + clean_census()[1:]):
            metric = self.measure(events)
            self.assertIn('park-instrumentation-not-established', metric['reasons'])

    def test_gaps_malformed_dispositions_and_missing_terminal(self):
        bads = [clean_census()[:-1], [ANCHOR],
                clean_census() + [event('batch-member-parked', attributed='false', batch='win-fixture')],
                clean_census() + [event('batch-member-parked', 'bad', attributed=False, batch='win-fixture')],
                clean_census() + [event('batch-member-parked', project=None, attributed=False, batch='win-fixture')],
                [*clean_census()[:2], dict(clean_census()[2], kept=['L-spec-1'], dropped=['L-spec-2'])]]
        for events in bads:
            with self.subTest(events=events):
                self.assertIn('park-disposition-census-incomplete', self.measure(events)['reasons'])
        metric = self.measure(clean_census(), dispositions=[dict(ts=SINCE, line=1)])
        self.assertEqual(metric['value'], sr.UNKNOWN)
        self.assertEqual(self.measure(clean_census(), reasons=['ledger-segment-incomplete'])['value'], sr.UNKNOWN)


def release_log(ids=(), pg=True):
    rows = ['CI-SELECTION mode=full reason=fixture files=0']
    for index in (1, 2):
        name = f'pytest — shard {index} of 2'
        if index == 1:
            rows += [name + '\tgate\tFAILED ' + ident for ident in ids]
        rows.append(name + '\tgate\tSUMMARY pytest_baseline_gate.py check: PASS new=0 changed=0')
    if pg:
        rows.append('pg-parity (Postgres)\tPostgres 17\t2 passed')
    return '\n'.join(rows)


class EscapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'logs').mkdir()
        path = self.root / 'logs/deploy-fixture.log'
        path.write_text(json.dumps(dict(sha='a' * 40, checks={'Python': dict(state='ran', run_id=1,
                               url='https://github.com/fixture/repo/actions/runs/1/attempts/1')})))
        self.deploy = event('deploy-landed', '2026-11-03T12:00:00Z', project='orchestration-context',
                            target='albert-scott', sha='a' * 40, log=str(path), elapsed_s=10)
        self.gh = FakeGH([attempt()], jobs={(1, 1): jobs_for(durations=(10, 12))}, logs={(1, 1): release_log(['tests/test_x.py::test_red'])})
        self.gh.owner = 'fixture/repo'
        self.gh.extra['actions/runs/1/attempts/1'] = attempt()

    def measure(self, events):
        return sr.escape_metric(events, 'albert-scott', SINCE, UNTIL, dict(reasons=[]), self.gh, self.root)

    def test_exact_ids_strict_chronology_and_distinct_occurrences(self):
        ident = 'tests/test_x.py::test_red'
        for stamp, project, complete, carried, expected in [
            ('2026-11-02T00:00:00Z', 'albert-scott', True, ident, 0),
            ('2026-11-03T12:00:00Z', 'albert-scott', True, ident, 1),
            ('2026-11-04T00:00:00Z', 'albert-scott', True, ident, 1),
            ('2026-11-02T00:00:00Z', 'other', True, ident, 1),
            ('2026-11-02T00:00:00Z', 'albert-scott', False, ident, 1),
            ('2026-11-02T00:00:00Z', 'albert-scott', True, 'tests/test_x.py', 1)]:
            with self.subTest(stamp=stamp, project=project, complete=complete, carried=carried):
                guard = event('master-full-run', stamp, project, complete=complete, ids=[carried])
                result = self.measure([guard, self.deploy, dict(self.deploy, ts='2026-11-04T12:00:00Z')])
                self.assertEqual(result['value'], expected)
                self.assertEqual(len(result['occurrences']), 2)
        print('exact-id chronological joins; repeated release occurrences retained; distinct escape count 1')

    def test_missing_release_or_gate_and_target_mapping(self):
        self.assertEqual(self.measure([])['value'], sr.UNKNOWN)
        self.assertEqual(self.measure([dict(self.deploy, type='window-verified')])['value'], sr.UNKNOWN)
        self.assertIn('release-target-unmapped', self.measure([dict(self.deploy, target='droplet')])['reasons'])
        Path(self.deploy['log']).write_text('{}')
        self.assertIn('release-gate-run-unresolved', self.measure([self.deploy])['reasons'])

    def test_sha_selection_and_partial_failure_data(self):
        for change in ({'head_sha': 'b' * 40}, {'updated_at': '2026-11-04T00:00:00Z'}):
            with self.subTest(change=change):
                self.gh.extra['actions/runs/1/attempts/1'] = attempt(**change)
                self.assertEqual(self.measure([self.deploy])['value'], sr.UNKNOWN)
        self.gh.extra['actions/runs/1/attempts/1'] = attempt()
        for text in (release_log(pg=False), 'CI-SELECTION mode=selected reason=paths files=1',
                     'CI-SELECTION mode=full reason=fixture files=0\n<testsuite tests="1"/>'):
            self.gh.logs[(1, 1)] = text
            self.assertEqual(self.measure([self.deploy])['value'], sr.UNKNOWN)

    def test_missing_release_shard_cannot_prove_zero(self):
        self.gh.job_rows[(1, 1)] = jobs_for(durations=(10, 12))[1:]
        self.assertEqual(self.measure([self.deploy])['value'], sr.UNKNOWN)

    def test_redaction_preserves_distinct_exact_id_count(self):
        ids = ['tests/test_x.py::test_red[<a>]', 'tests/test_x.py::test_red[<b>]']
        self.gh.logs[(1, 1)] = release_log(ids)
        result = self.measure([self.deploy])
        self.assertEqual(result['value'], 2)
        self.assertNotIn('<a>', json.dumps(result))


class SafetyTests(unittest.TestCase):
    def test_read_only_transport_and_in_memory_reuse(self):
        import ghlimit
        calls = []
        def transport(argv, *, wait):
            self.assertFalse(wait)
            calls.append(argv)
            self.assertEqual(argv[0], 'gh')
            self.assertTrue(argv[1] == 'api' and argv[-2:] == ['--method', 'GET']
                            or argv[1:3] == ['run', 'view'] and '--log' in argv)
            return 0, json.dumps(dict(total_count=0, workflow_runs=[])), ''
        gh = sr.GitHub('fixture/repo')
        with patch.object(ghlimit, 'run', side_effect=transport):
            self.assertEqual(gh.pages('actions/runs', 'workflow_runs'), [])
            self.assertEqual(gh.pages('actions/runs', 'workflow_runs'), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls, gh.trace)
        print('GET-only trace=' + json.dumps(calls) + '; repeated response fetched once')

    def test_source_digests_redaction_and_path_protection(self):
        quota_before = quota_snapshot()
        fixture = CliTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        protected = [*list((fixture.root / 'events').glob('*')), fixture.log,
                     fixture.root / 'logs/deploy-fixture.log']
        before = {p: digest(p) for p in protected}
        proc = fixture.call()
        fixture.assert_report(proc, 0)
        self.assertEqual({p: digest(p) for p in protected}, before)
        calls = [json.loads(line) for line in (fixture.root / 'calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(argv == ['api', 'rate_limit'] or argv[0] == 'api' and argv[-2:] == ['--method', 'GET']
                            or argv[:2] == ['run', 'view'] and '--log' in argv for argv in calls))
        for name in ('.env', '.env.production', 'credentials', '.ssh/id_rsa', 'secret.txt', '.config/gh/hosts.yml'):
            with self.subTest(path=name), patch.object(Path, 'read_text', side_effect=AssertionError('prohibited read')):
                with self.assertRaises(sr.Gap):
                    sr.read_log(fixture.root / name)
        self.assertEqual(sr.safe_id('untrusted\x1b[31m\nhttps://invalid.example'), '[redacted]')
        gh = sr.GitHub('fixture/repo')
        import ghlimit
        with patch.object(ghlimit, 'run', return_value=(1, '', 'untrusted transport diagnostic')):
            rows, evidence = sr.run_population(gh, SINCE, UNTIL)
        self.assertEqual(sr.run_metric(rows, evidence)['value'], sr.UNKNOWN)
        self.assertNotIn('untrusted transport diagnostic', json.dumps(evidence))
        self.assertEqual(quota_snapshot(), quota_before)
        print('source sha256 before=after; prohibited paths denied before reads; isolated quota state unchanged')

    def run_dispatcher_child(self, name, root, *args):
        fake = root / 'fake-gh'
        fake.write_text('#!/bin/sh\nexit 73\n')
        fake.chmod(0o700)
        (root / 'gh').symlink_to(fake)
        child_tmp = root / 'tmp'
        child_tmp.mkdir()
        env = dict(os.environ, HOME=str(root / 'home'), DOIT_ROOT=str(root / 'doit'),
                   TMPDIR=str(child_tmp), TEMP=str(child_tmp), TMP=str(child_tmp),
                   DOIT_SCRATCH=str(child_tmp),
                   GH_LIMIT_STATE=str(root / 'quota.json'), DOIT_REAL_GH=str(fake),
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
                   XDG_STATE_HOME=str(root / 'state'), PATH=str(root) + os.pathsep + os.defpath)
        # Keep all tempfile fallbacks private and register that same scratch root
        # for tmpdir resources. Venv fixtures additionally require a /tmp ancestor.
        for key in ('DOIT_LEDGER_FILE', 'DOIT_PROJECT'):
            env.pop(key, None)
        path = f'src/test_{name}.py'
        proc = subprocess.run(['/usr/bin/python3', path, *args], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=600)
        return path, proc

    def test_resources_child_ignores_inherited_temp_paths(self):
        # Python considers TEMP and TMP after TMPDIR. A checkout is writable but
        # outside resources.ROOTS, reproducing the grader's refused/would-reap failure.
        for name in ('test_reapable_and_exclude', 'test_reap_safety_removes'):
            with self.subTest(test=name), tempfile.TemporaryDirectory(dir='/tmp') as directory:
                with patch.dict(os.environ, TMPDIR=str(REPO), TEMP=str(REPO), TMP=str(REPO)):
                    path, proc = self.run_dispatcher_child('resources', Path(directory), '-k', name)
                print(f'{path} -k {name}: exit={proc.returncode}\n{proc.stdout}{proc.stderr}', flush=True)
                self.assertEqual(proc.returncode, 0, path + '\n' + proc.stderr[-1500:])
        print('resources child: inherited TMPDIR/TEMP/TMP cannot relocate fixtures outside allowed roots')

    def test_all_dispatcher_references_in_isolated_children(self):
        quota_before = quota_snapshot()
        names = ('build_runner', 'cron_run', 'doit_test', 'launch', 'packet', 'packet_lint',
                 'problems', 'reap_tmp', 'resources', 'scratch', 'shape', 'throughput', 'up', 'window_gate')

        # The runner's TMPDIR can be under a protected root. Use a neutral /tmp
        # ancestor so resource fixtures (including venvs) meet the reaper's roots.
        # Each child still gets its own private HOME, state and temp directories.
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            roots = {name: Path(directory) / name for name in names}
            for root in roots.values():
                root.mkdir()
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                done = list(pool.map(lambda name: self.run_dispatcher_child(name, roots[name]), names))
        results = []
        for name, (path, proc) in zip(names, done):
            with self.subTest(file=name):
                results.append(dict(file=path, exit_code=proc.returncode,
                                    output_sha256=hashlib.sha256((proc.stdout + proc.stderr).encode()).hexdigest()))
                print('dispatcher regression ' + json.dumps(results[-1]), flush=True)
                self.assertEqual(proc.returncode, 0, path + '\n' + proc.stdout[-1500:] + proc.stderr[-1500:])
        self.assertEqual(len(results), 14)
        self.assertEqual(quota_snapshot(), quota_before)


class RealSourceTests(unittest.TestCase):
    def test_unchanged_copied_real_log_and_event_snapshots(self):
        quota_before = quota_snapshot()
        content = Path('/home/albert/.do-it/content')
        sources = [content / 'probe-input-L-charter-0076/rloop.log',
                   content / 'probe-L-charter-0076/raw/lane-ledger-events.json',
                   content / 'probe-L-charter-0076/raw/nightly-events.json']
        before = {p: digest(p) for p in sources}
        self.assertTrue(all(value is not None for value in before.values()), 'required real probe source missing')
        with tempfile.TemporaryDirectory(dir=_root) as directory:
            copies = []
            for source in sources:
                target = Path(directory) / source.name
                shutil.copyfile(source, target)
                self.assertEqual(digest(target), before[source])
                copies.append(target)
            text = sr.read_log(copies[0])
            records, inventory = sr.dated_log(text)
            import re
            expected_anchors = sum(bool(re.fullmatch(r'\d\d:\d\d:\d\d LAUNCH win-\d{8}-r\d{4}: .*', line))
                                   for line in text.splitlines())
            expected_terminals = sum(bool(re.search(r'^\d\d:\d\d:\d\d (?:CLEAN -> queued:|NOT CLEAN:|'
                                        r'TG: window .* (?:merged\.|did not merge:)|full-CI rehearsal not green:)', line))
                                     for line in text.splitlines())
            self.assertGreater(expected_anchors, 0)
            self.assertGreater(expected_terminals, 0)
            self.assertEqual(inventory['anchors'], expected_anchors)
            self.assertEqual(inventory['terminals'], expected_terminals)
            self.assertGreater(inventory['parsed_lines'], 0)
            self.assertFalse(any(g['reason'] == 'log-line-malformed' for g in inventory['uncertainty']))
            rows = [row for path in copies[1:] for row in json.loads(path.read_text())]
            self.assertTrue(rows)
            self.assertTrue(all(isinstance(row, dict) and 'type' in row and 'ts' in row for row in rows))
            spans, gaps, _ = sr.lane_spans(records, rows, 'albert-scott', inventory)
            metric = sr.lane_metric(spans, gaps, sr.utc('2026-11-02'), sr.utc('2026-11-06'), inventory)
            if metric['value'] == sr.UNKNOWN:
                self.assertTrue(gaps)
                demonstrated = {'rehearsal-start-ambiguous', 'rehearsal-phase-join-ambiguous',
                                'window-start-ambiguous', 'window-terminal-unmatched', 'local-terminal-unmatched',
                                'full-ci-terminal-ambiguous', 'full-ci-cancellation-ambiguous',
                                'ledger-rehearsal-start-unmatched', 'rehearsal-ledger-join-invalid',
                                'lane-terminal-missing', 'lane-terminal-conflicting', 'lane-span-negative',
                                'log-epoch-unanchored', 'log-clock-jump-ambiguous', 'log-anchors-inconsistent'}
                self.assertTrue(set(gaps) <= demonstrated, gaps)
            else:
                self.assertGreater(metric['denominator_seconds'], 0)
                self.assertEqual(metric['value'], metric['numerator_seconds'] / metric['denominator_seconds'])
            print('real source evidence=' + json.dumps(dict(sources=[dict(path=str(p), sha256=before[p]) for p in sources],
                  anchors=expected_anchors, terminals=expected_terminals, parsed_lines=inventory['parsed_lines'],
                  event_rows=len(rows), spans=len(spans), failed_ci_share=metric['value'],
                  numerator_seconds=metric['numerator_seconds'], denominator_seconds=metric['denominator_seconds'],
                  demonstrated_gaps=gaps), sort_keys=True))
        self.assertEqual({p: digest(p) for p in sources}, before)
        self.assertEqual(quota_snapshot(), quota_before)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repo = self.root / 'repos/albert-scott'
        repo.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin',
                        'https://github.com/fixture/repo.git'], check=True, capture_output=True)
        (self.root / 'events').mkdir()
        (self.root / 'logs').mkdir()
        self.log = self.root / 'rloop.log'
        self.log.write_text('00:00:00 rehearse on aaaaaaa: 1 2\n'
                            '00:20:00 CLEAN -> queued: 1 2\n'
                            '00:30:00 LAUNCH win-20261103-r0030: 1 2\n'
                            '00:50:00 TG: window win-20261103-r0030 merged. shipped=1,2 dropped=\n')
        deploy_log = self.root / 'logs/deploy-fixture.log'
        deploy_log.write_text(json.dumps(dict(sha='a' * 40, checks={'Python': dict(state='ran', run_id=1,
                              url='https://github.com/fixture/repo/actions/runs/1/attempts/1')})))
        self.rows = clean_census() + [event('deploy-landed', '2026-11-03T12:00:00Z',
                         project='orchestration-context', target='albert-scott', sha='a' * 40,
                         log=str(deploy_log), elapsed_s=10)]
        self.write_events()
        self.fake = self.root / 'fake-gh'
        self.fake.write_text('''#!/usr/bin/python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args == ['api', 'rate_limit']:
    print(json.dumps({'resources': {k: {'limit': 5000, 'remaining': 4999, 'reset': 4102444800}
                                  for k in ('core', 'graphql', 'search')}}))
    sys.exit(0)
responses = json.loads((root / 'responses.json').read_text())
key = json.dumps(args)
if key not in responses:
    sys.exit(73)
print(responses[key])
''')
        self.fake.chmod(0o700)
        self.responses = {}
        self.respond(['api', 'repos/fixture/repo/actions/runs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=1, workflow_runs=[attempt()]))
        self.respond(['api', 'repos/fixture/repo/actions/runs/1/attempts/1/jobs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=3, jobs=jobs_for(durations=(10, 12))))
        self.respond(['api', 'repos/fixture/repo/actions/runs/1/attempts/1', '--method', 'GET'], attempt())
        self.respond(['run', 'view', '1', '--attempt', '1', '--repo', 'fixture/repo', '--log'], release_log())
        self.env = dict(os.environ, HOME=str(self.root / 'home'), DOIT_ROOT=str(self.root),
                        GH_LIMIT_STATE=str(self.root / 'quota.json'), DOIT_REAL_GH=str(self.fake),
                        XDG_CONFIG_HOME=str(self.root / 'config'), XDG_CACHE_HOME=str(self.root / 'cache'),
                        XDG_STATE_HOME=str(self.root / 'state'), DOIT_PROJECT='caller-project')
        self.env.pop('DOIT_READ_CACHE', None)
        self.env.pop('DOIT_LEDGER_FILE', None)
        self.env['PATH'] = str(self.root) + os.pathsep + os.defpath
        (self.root / 'gh').symlink_to(self.fake)

    def write_events(self):
        (self.root / 'events/L-executor-fixture.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in self.rows))

    def respond(self, argv, body):
        self.responses[json.dumps(argv)] = body if isinstance(body, str) else json.dumps(body)
        (self.root / 'responses.json').write_text(json.dumps(self.responses))

    def call(self, *, project='albert-scott', since='2026-11-03', until='2026-11-05', extra=()):
        return subprocess.run([str(REPO / 'doit'), 'speed-report', '--project', project,
                               '--since', since, '--until', until, '--rloop-log', str(self.log), *extra],
                              cwd=REPO, env=self.env, text=True, capture_output=True, timeout=30)

    def assert_report(self, proc, status):
        self.assertEqual(proc.returncode, status, proc.stderr)
        values, evidence = json.loads(proc.stdout), json.loads(proc.stderr)
        self.assertEqual(set(values), set(sr.KEYS))
        for value in values.values():
            self.assertTrue(value == sr.UNKNOWN or type(value) in (int, float) and value >= 0)
        self.assertEqual(evidence['project'], 'albert-scott')
        self.assertEqual(evidence['interval'], '[since,until)')
        for metric in evidence['metrics'].values():
            for key in ('coverage', 'sample_count', 'included', 'excluded', 'formula', 'reasons'):
                self.assertIn(key, metric)
        print(json.dumps(dict(url='exec://doit/speed-report', status=status,
                              body_sha256=hashlib.sha256(proc.stdout.encode()).hexdigest(),
                              body_excerpt=values), sort_keys=True))
        return values, evidence

    def test_complete_and_threshold_misses(self):
        values, _ = self.assert_report(self.call(), 0)
        self.assertEqual(values, dict(zip(sr.KEYS, (10, 12, 2, 0, 0, 0))))
        self.respond(['api', 'repos/fixture/repo/actions/runs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=1, workflow_runs=[attempt(minutes=40)]))
        values, _ = self.assert_report(self.call(), 0)
        self.assertEqual(values['ci_verify_median_min'], 40)

    def test_partial_and_empty_reports(self):
        self.respond(['api', 'repos/fixture/repo/actions/runs/1/attempts/1/jobs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=0, jobs=[]))
        values, _ = self.assert_report(self.call(), 2)
        self.assertEqual(values['ci_verify_median_min'], 10)
        self.assertEqual(values['pg_parity_median_min'], sr.UNKNOWN)
        self.rows = []
        self.write_events()
        self.log.write_text('')
        self.respond(['api', 'repos/fixture/repo/actions/runs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=0, workflow_runs=[]))
        values, _ = self.assert_report(self.call(), 2)
        self.assertEqual(set(values.values()), {sr.UNKNOWN})

    def test_utc_half_open_boundaries(self):
        rows = [attempt(1, created_at='2026-11-02T19:00:00-05:00', updated_at='2026-11-03T00:10:00Z'),
                attempt(2, created_at='2026-11-05T00:00:00Z', updated_at='2026-11-05T00:20:00Z')]
        self.respond(['api', 'repos/fixture/repo/actions/runs?per_page=100&page=1', '--method', 'GET'],
                     dict(total_count=2, workflow_runs=rows))
        values, evidence = self.assert_report(self.call(since='2026-11-02T19:00:00-05:00'), 0)
        self.assertEqual(values['ci_verify_median_min'], 10)
        self.assertEqual(evidence['since'], SINCE.isoformat())
        self.assertEqual(evidence['metrics']['ci_verify_median_min']['included'][0]['run_id'], 1)
        self.assertEqual(evidence['metrics']['ci_verify_median_min']['excluded'][0]['id']
                         if 'id' in evidence['metrics']['ci_verify_median_min']['excluded'][0]
                         else evidence['metrics']['ci_verify_median_min']['excluded'][0]['run_id'], 2)

    def test_invalid_arguments_before_source_access(self):
        for kw in ({'since': 'bad'}, {'since': '2026-11-03T00:00:00'}, {'since': '2026-11-06'},
                   {'until': '2099-01-01'}, {'until': '2026-11-03'}, {'project': 'missing'},
                   {'project': '../albert-scott'}, {'project': 'albert-scott/../../events'},
                   {'extra': ('--unknown', 'untrusted\x1b[31m')}):
            with self.subTest(arguments=kw):
                proc = self.call(**kw)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertLess(len(proc.stderr), 160)
                self.assertNotIn('\x1b', proc.stderr)
        self.assertFalse((self.root / 'calls.jsonl').exists())
        self.assertFalse((self.root / 'cache').exists())

    def test_help(self):
        proc = subprocess.run([str(REPO / 'doit'), 'speed-report', '--help'], env=self.env,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        for text in ('[since,until)', 'midnight', 'timezone', 'Exit 0', 'exit 2', '--rloop-log', 'fraction'):
            self.assertIn(text, proc.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
