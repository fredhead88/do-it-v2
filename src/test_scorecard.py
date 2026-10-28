#!/usr/bin/env python3
"""Fixture-only scorecard acceptance checks; no production readers or clock."""
import ast
import copy
import io
import json
import statistics
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scorecard as sc

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
DAY = NOW.replace(hour=0)


def ev(kind, hour=0, subject='S', **fields):
    return dict(type=kind, ts=(DAY + timedelta(hours=hour)).isoformat(),
                subject=subject, **fields)


def stall(hour, buckets=None, **fields):
    return dict(ts=(DAY + timedelta(hours=hour)).isoformat(),
                buckets=buckets or {}, builder_capacity=2, ok=True, **fields)


def pace(hour=1, **fields):
    return dict(ts=(DAY + timedelta(hours=hour)).isoformat(),
                disk_used_pct=None, **fields)


def row(events=(), samples=None, **kwargs):
    return sc.day_rows(list(events), NOW, days=1, samples=samples, **kwargs)[0]


class ScorecardTests(unittest.TestCase):
    def test_AC1_windows_cohort_and_wait(self):
        events = [ev('review', -3, n_blocking=2), ev('review', -2, n_blocking=0),
                  ev('review', -1, 'T', n_blocking=0),
                  ev('window-opened', 1, 'w1', specs=['S', 'T', 'missing']),
                  ev('window-opened', 2, 'w1', specs=['S']),
                  ev('window-verified', 25, 'w1'),
                  ev('window-opened', 3, 'w2', specs=[]),
                  ev('window-dropped', 26, 'w2', nodes=['a']),
                  ev('window-opened', -2, 'old', specs=[]),
                  ev('window-dropped', 4, 'old')]
        expected = dict(attempts=2, verified=1, dropped=1,
                        verified_pct=50, median_wait_min=180)
        self.assertEqual(sc.window_stats(events, DAY, DAY + timedelta(days=1)), expected)
        for wid, counts in [('w1', (1, 1, 0)), ('w2', (1, 0, 1)), ('old', (0, 0, 0))]:
            got = sc.window_stats([e for e in events if e['subject'] == wid or e['type'] == 'review'],
                                  DAY, DAY + timedelta(days=1))
            self.assertEqual(tuple(got[k] for k in ('attempts', 'verified', 'dropped')), counts)
            print('AC1', wid, {k: got[k] for k in ('attempts', 'verified', 'dropped')})
        daily = row(events)
        for key, value in expected.items():
            self.assertEqual(daily[{'attempts': 'window_attempts', 'verified': 'windows_verified',
                                    'dropped': 'windows_dropped', 'verified_pct': 'window_verified_pct'
                                    }.get(key, key)], value)
        self.assertEqual(sc.window_stats([], DAY, NOW), dict(
            attempts=0, verified=0, dropped=0, verified_pct=None, median_wait_min=None))
        alias = [ev('window-opened', 1, 'different', window='w'),
                 ev('window-verified', 2, 'another', window='w')]
        self.assertEqual(sc.window_stats(alias, DAY, NOW)['verified'], 1)

    def test_AC2_shipping_matches_inline_study(self):
        events = [ev('spec-written', -48), ev('spec-written', -24),
                  ev('build-done', -5), ev('build-done', -3),
                  ev('shipped', -1 / 3600), ev('shipped', 3),
                  ev('spec-written', -4, 'T'), ev('shipped', 0, 'T'),
                  ev('shipped', 1, 'carried')]
        rows = sc.day_rows(events, NOW, days=3)
        for daily in rows:
            ships = []
            for spec in {e['subject'] for e in events}:
                written = sorted(datetime.fromisoformat(e['ts']) for e in events
                                 if e['subject'] == spec and e['type'] == 'spec-written')
                shipped = sorted(datetime.fromisoformat(e['ts']) for e in events
                                 if e['subject'] == spec and e['type'] == 'shipped')
                if written and shipped and str(shipped[0].date()) == daily['date']:
                    ships.append((shipped[0] - written[0]).total_seconds() / 3600)
            self.assertEqual(daily['shipped'], len(ships))
            self.assertEqual(daily['median_lead_h'], statistics.median(ships) if ships else None)
        self.assertAlmostEqual(rows[1]['median_lead_h'], 48 - 1 / 3600)
        self.assertEqual(rows[1]['multi_build_pct'], 100)
        self.assertEqual(rows[2]['median_lead_h'], 4)
        self.assertEqual(rows[2]['multi_build_pct'], 0)
        self.assertTrue(rows[2]['partial'])
        self.assertFalse(rows[1]['partial'])
        self.assertEqual(rows[2]['through'], NOW.isoformat())
        self.assertIsNone(rows[0]['median_lead_h'])
        print('AC2', [(r['date'], r['shipped'], r['median_lead_h'], r['multi_build_pct']) for r in rows])

    def test_AC3_samples_and_every_bucket(self):
        events = [ev('review', -1, 'clean', n_blocking=0),
                  ev('review', -1, 'rebuilt', n_blocking=0), ev('build-started', 0, 'rebuilt')]
        samples = [stall(-1, {'STUCK': ['cross']}), stall(0, {'STUCK': ['cross']}),
                   stall(3, {'STUCK': ['gap'], 'queued': ['clean', 'raw', 'rebuilt']}),
                   stall(3.5, {'STUCK': ['gap'], 'queued': ['clean', 'raw', 'rebuilt']}),
                   stall(4, {'STUCK': ['bad']}), stall(5, {'STUCK': ['gap']}),
                   stall(6, {'held': ['H'], 'waiting-on-human': ['U'], 'waiting-on-prereq': ['P']})]
        samples[4]['ok'] = False
        got = row(events, samples)
        self.assertEqual([got[k] for k in ('stalled_spec_h', 'stuck_over_1h', 'waiting_seat_h',
                                          'waiting_window_h', 'waiting_human_h')], [3, 1, 2, 1, 3])
        expected = {'STUCK': 'stalled_spec_h', 'queued': 'waiting_seat_h',
                    'held': 'waiting_human_h', 'waiting-on-human': 'waiting_human_h',
                    'waiting-on-prereq': 'waiting_human_h', 'audit-wait': None,
                    'retry-wait': None, 'in-flight': None, 'invented': None}
        for bucket, measure in expected.items():
            got = row(samples=[stall(1, {bucket: ['S']})])
            for key in ('stalled_spec_h', 'waiting_seat_h', 'waiting_window_h', 'waiting_human_h'):
                self.assertEqual(got[key], int(key == measure), bucket)
            self.assertEqual(got['ignored_bucket_samples'], int(measure is None), bucket)
        for key in ('stalled_spec_h', 'stuck_over_1h', 'waiting_seat_h', 'waiting_window_h', 'waiting_human_h'):
            self.assertIsNone(row()[key])
            self.assertEqual(row()['bands'][key], 'not sampled')
        self.assertIsNone(row(samples=[{**stall(1), 'ok': False}])['stalled_spec_h'])
        blocking = [ev('review', 0, n_blocking=0), ev('review', 1, n_blocking=2)]
        self.assertEqual(row(blocking, [stall(2, {'queued': ['S']})])['waiting_seat_h'], 1)
        print('AC3 hourly dedup, consecutive hours, waiting splits and all buckets checked')

    def test_AC4_builder_seat_intervals(self):
        events = [ev('spawn-started', .5, spawn='b', role='builder'),
                  ev('build-started', .6, spawn='b'), ev('spawn-done', 2.5, spawn='b'),
                  ev('spawn-started', 2.5, spawn='open', role='builder'),
                  ev('spawn-started', 0, spawn='reviewer', role='reviewer'),
                  ev('spawn-done', 3, spawn='reviewer')]
        got = row(events, [stall(0), stall(2)])
        self.assertEqual(got['seat_busy_pct'], 37.5)  # .5 + .5 + .5 / 4 seat-hours
        self.assertIsNone(row(events)['seat_busy_pct'])
        self.assertEqual(row([ev('build-started', .5, spawn='only')], [stall(0)])['seat_busy_pct'], 25)
        self.assertEqual(row(events, [dict(stall(0), builder_capacity=0)])['seat_busy_pct'], None)
        print('AC4 busy=1.5h capacity=4h share=37.5%')

    def test_AC5_pace_resources_and_unused(self):
        reset = (DAY + timedelta(days=3.5, hours=1)).isoformat()
        samples = [pace(1, claude_7d_pct=20, claude_7d_reset=reset,
                        codex_7d_pct=50, codex_7d_reset=reset, ram_used_pct=80, swap_used_pct=91),
                   dict(pace(2, claude_7d_pct=None, ram_used_pct=70), disk_used_pct=88),
                   dict(pace(3, ram_used_pct=85), disk_used_pct=80), stall(1, {'STUCK': ['S']})]
        got = row(samples=samples)
        self.assertEqual(got['claude_7d']['pace_pct'], 50)
        self.assertEqual(got['claude_7d']['delta'], -30)
        self.assertEqual(got['disk_used_pct'], 88)
        self.assertEqual(got['ram_used_pct'], 85)
        self.assertEqual(got['swap_used_pct'], 91)
        self.assertEqual(got['bands']['ram_used_pct'], 'amber')
        self.assertTrue(got['capacity_unused'])
        for elapsed, expected in [(0, 0), (3.5, 50), (7 - 1 / 86400, 100 * (7 - 1 / 86400) / 7)]:
            ts = DAY + timedelta(days=elapsed - 7)
            sample = dict(pace(), ts=ts.isoformat(), claude_7d_pct=40, claude_7d_reset=DAY.isoformat())
            sample_now = ts + timedelta(microseconds=1)
            reading = sc.day_rows([], sample_now, days=1, samples=[sample])[0]['claude_7d']
            self.assertAlmostEqual(reading['pace_pct'], expected)
        self.assertIsNone(row(samples=[pace(1, claude_7d_pct=20, claude_7d_reset=DAY.isoformat())])['claude_7d'])
        for pct, expected in [(40, False), (39.9, True)]:
            sample = pace(1, claude_7d_pct=pct, claude_7d_reset=reset,
                          codex_7d_pct=50, codex_7d_reset=reset)
            self.assertEqual(row(samples=[sample, stall(1, {'queued': ['S']})])['capacity_unused'], expected)
        self.assertIsNone(row(samples=[stall(1, {'queued': ['S']})])['capacity_unused'])
        self.assertIsNone(row(samples=[samples[0]])['capacity_unused'])
        self.assertFalse(row(samples=[stall(1)])['capacity_unused'])
        self.assertIsNone(row(samples=[pace()])['ram_used_pct'])
        self.assertIsNone(row(samples=[pace(1, claude_7d_pct=40, claude_7d_reset=reset),
                                       stall(1, {'STUCK': ['S']})])['capacity_unused'])
        at_reset = pace(1, claude_7d_pct=50, claude_7d_reset=(DAY + timedelta(hours=1)).isoformat())
        self.assertIsNone(row(samples=[at_reset])['claude_7d'])
        unbanded = row(samples=[pace(1, claude_5h_pct=99, claude_7d_pct=50, claude_7d_reset=reset,
                                             codex_7d_pct=50, codex_7d_reset=reset),
                               stall(1, {'queued': ['S']})])
        self.assertEqual(unbanded['claude_5h_pct'], 99)
        self.assertEqual(unbanded['bands']['claude_5h_pct'], 'no data')
        self.assertFalse(unbanded['capacity_unused'])
        print('AC5 formula start/middle/end, nulls, maxima and delta -10/-10.1 checked')

    def test_AC6_failed_runs_and_tokens(self):
        events = [ev('build-blocked', 1, spawn='b'), ev('build-done', 1, spawn='b', status='BLOCKED'),
                  ev('build-done', 1, status='NEEDS_CONTEXT'), ev('build-done', 1),
                  ev('verdict', 2, spawn='g', confirmed=False), ev('review', 3, spawn='r', n_blocking=1),
                  ev('window-opened', 4, 'w', specs=['S']),
                  ev('build-done', 5, spawn='success', status='DONE'),
                  ev('verdict', 5, confirmed=True), ev('review', 5, n_blocking=0),
                  ev('window-opened', 6, 'ok'), ev('window-verified', 25, 'ok')]
        for spawn in ['b', 'g', 'r', 'success']:
            events.append(ev('spawn-done', 7, spawn=spawn, input_tokens=10, output_tokens=2,
                             cache_read=3, cache_creation=4 if spawn == 'b' else None))
        got = row(events)
        self.assertEqual([got[k] for k in ('failed_runs', 'total_runs', 'failed_share_pct', 'failed_tokens')],
                         [4, 8, 50, 49])
        for status in ('BLOCKED', 'NEEDS_CONTEXT', None):
            got = row([ev('build-blocked', 1), ev('build-done', 1, status=status)])
            self.assertEqual((got['failed_runs'], got['total_runs']), (1, 1))
        self.assertEqual((row([ev('build-done', 1, status='DONE')])['failed_runs'],
                          row([ev('build-done', 1, status='DONE')])['total_runs']), (0, 1))
        self.assertIsNone(row()['failed_share_pct'])
        print('AC6 failed=4 total=8 share=50% tokens=49')

    def test_AC7_band_boundaries(self):
        expected = {'median_lead_h': ('low', 10, 15), 'stuck_over_1h': ('low', 1, 2),
                    'window_verified_pct': ('high', 80, 60), 'median_wait_min': ('low', 120, 240),
                    'failed_share_pct': ('low', 20, 35), 'seat_busy_pct': ('high', 60, 40),
                    'disk_used_pct': ('low', 85, 92), 'ram_used_pct': ('low', 90, 95),
                    'claude_7d_delta': ('abs', 10, 20), 'codex_7d_delta': ('abs', 10, 20)}
        self.assertEqual(sc.BANDS, expected)
        for key, (direction, green, red) in expected.items():
            for edge in (green, red):
                for delta in (-.01, 0, .01):
                    value = edge + delta
                    want = ('green' if value >= green else 'red' if value < red else 'amber') if direction == 'high' else (
                        'green' if (value <= green if direction == 'abs' else value < green)
                        else 'red' if value > red else 'amber')
                    self.assertEqual(sc.band(key, value), want, (key, value))
                    if direction == 'abs':
                        self.assertEqual(sc.band(key, -value), want)
            self.assertEqual(sc.band(key, None), 'not sampled' if key in (
                'stuck_over_1h', 'seat_busy_pct', 'disk_used_pct', 'ram_used_pct',
                'claude_7d_delta', 'codex_7d_delta') else 'no data')
            self.assertIn(key, row()['bands'], key)
            print('AC7', key, direction, green, red, 'edges checked')
        self.assertEqual(sc.band('ram_used_pct', 80, swap_pct=90), 'green')
        self.assertEqual(sc.band('ram_used_pct', 80, swap_pct=90.1), 'amber')

    def test_AC8_copy_and_top_reasons(self):
        events = [ev('build-blocked', 1, f'S{i}', reason='bad\x00\n\t' + 'x' * 400) for i in range(7)]
        events += [ev('spec-written', -2, 'shipped'), ev('shipped', 3, 'shipped'),
                   ev('window-opened', 1, 'verified', specs=['shipped']),
                   ev('review', 0, 'shipped', n_blocking=0), ev('window-verified', 2, 'verified')]
        samples = [stall(2, {'STUCK': ['S1'], 'held': ['S2']}),
                   dict(pace(1, ram_used_pct=80, swap_used_pct=5, claude_5h_pct=30,
                             claude_7d_pct=40, claude_7d_reset=(DAY + timedelta(days=4)).isoformat(),
                             codex_7d_pct=40, codex_7d_reset=(DAY + timedelta(days=4)).isoformat()),
                        disk_used_pct=70)]
        got = sc.day_rows(events, NOW + timedelta(days=1), days=2, samples=samples)[0]
        self.assertFalse(got['partial'])
        self.assertTrue(all(c['value'] is not None for c in got['cells']))
        self.assertEqual(len(got['top']), 5)
        self.assertEqual([t['spec'] for t in got['top']], ['S1', 'S2', 'S0', 'S3', 'S4'])
        for top in got['top']:
            self.assertLessEqual(len(top['reason']), 200)
            self.assertFalse(any(ord(c) < 32 for c in top['reason']))
        for daily in (got, row()):
            text = sc.copy_text(daily)
            print(text)
            for cell in daily['cells']:
                self.assertIn(cell['text'], text, cell['key'])
                self.assertIn(cell['band'], text, cell['key'])
            for top in daily['top']:
                self.assertIn(top['spec'], text)
                self.assertIn(top['reason'], text)
            self.assertIn('capacity left unused', text.lower())
            self.assertNotIn('S6:', text)
            self.assertNotIn('<', text)
        self.assertIn('not sampled', sc.copy_text(row()))
        drops = [ev('window-opened', 1, 'w', specs=['S']),
                 ev('window-dropped', 2, 'w', nodes=['t1', 't2'])]
        self.assertIn('window dropped on 2 test(s)', row(drops)['top'][0]['reason'])

    def test_AC9_contract_purity_and_cli(self):
        required = set(sc.STUDY_MEASURES) | set('date partial through skipped_events sampled window_verified_pct '
            'stalled_spec_h stuck_over_1h waiting_seat_h waiting_window_h waiting_human_h ignored_bucket_samples '
            'seat_busy_pct disk_used_pct ram_used_pct swap_used_pct claude_5h_pct claude_7d codex_7d failed_runs '
            'total_runs failed_share_pct failed_tokens capacity_unused bands cells top'.split())
        self.assertEqual(sc.STUDY_MEASURES, ('shipped', 'median_lead_h', 'window_attempts', 'windows_verified',
                                          'windows_dropped', 'median_wait_min', 'multi_build_pct'))
        events, samples = [ev('shipped'), {'ts': 'bad', 'type': 'review'}], [stall(1), pace()]
        before = copy.deepcopy((events, samples))
        for days in (1, 7, 10):
            rows = sc.day_rows(events, NOW, days=days, samples=samples)
            self.assertEqual(len(rows), days)
            self.assertEqual(rows, sc.day_rows(events, NOW, days=days, samples=samples))
            self.assertEqual([r['date'] for r in rows], [str((DAY - timedelta(days=i)).date())
                                                       for i in reversed(range(days))])
            for daily in rows:
                self.assertEqual(set(daily), required)
                self.assertEqual(daily['skipped_events'], 1)
                self.assertEqual({c['key'] for c in daily['cells']},
                                 required - set('date partial through sampled bands cells top'.split()))
        self.assertEqual((events, samples), before)
        tree = ast.parse(Path(sc.__file__).read_text())
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertTrue(all(n.name.split('.')[0] in sys.stdlib_module_names for n in node.names))
                self.assertFalse(any(n.name.split('.')[0] in {'fold', 'subprocess', 'urllib', 'http', 'socket'}
                                     for n in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module.split('.')[0], sys.stdlib_module_names)
                self.assertNotIn(node.module.split('.')[0], {'fold', 'subprocess', 'urllib', 'http', 'socket'})
        for tag in range(1, 10):
            self.assertTrue(any(name.startswith(f'test_AC{tag}_') for name in dir(type(self))), tag)
        fake = types.ModuleType('fold')
        fake.read_events = lambda: []
        with patch.dict(sys.modules, {'fold': fake, 'pace': None, 'stall_sample': None}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(sc.main(['2026-10-27']), 0)
            lines = output.getvalue().splitlines()
            self.assertEqual(json.loads(lines[0])['date'], '2026-10-27')
            self.assertFalse(json.loads(lines[0])['partial'])
            self.assertIn('not sampled', output.getvalue())
        print('AC9 shapes, deterministic pure calls, malformed timestamps, CLI fallback checked')


if __name__ == '__main__':
    unittest.main()
