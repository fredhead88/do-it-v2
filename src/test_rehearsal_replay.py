#!/usr/bin/env python3
"""Hermetic regressions for the rehearsal flow replay (L-spec-2264).

Expected values are hand-calculated literals, never calls back into the replay.
Run one class by name, or `--snapshot-check CSV` for the retained probe snapshot.
"""
import contextlib
import copy
import csv
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rehearsal_replay as rr

REPO = Path(__file__).resolve().parent.parent
ORIGIN = datetime(2026, 11, 8)
SNAPSHOT_SHA = '74bf3a99341280142a841eb01b06017cbb78b87a07fb154a7b26442504b2b56d'
NOCI = 'no-CI-started(local red first, serial era)'


def T(minutes):
    return (ORIGIN + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def row(n, start, specs='1', **kw):
    out = dict.fromkeys(rr.COLUMNS, '')
    out.update(n=str(n), era='parallel', base='b', start_utc=T(start), specs=specs,
               n_specs=str(len(specs.split())), local_verdict='clean', ci_outcome='red',
               local_cancelled_ci='False', bucket='x')
    out.update({k: str(v) for k, v in kw.items()})
    return out


def merged(n, start, end, specs='1', tail=0, **kw):
    """Green, merged at `end`; CI ran start..end-tail."""
    out = row(n, start, specs, ci_outcome='green', window_outcome='merged', ci_launch_utc=T(start),
              ci_end_utc=T(end - tail), ci_min=end - tail - start, window_merged_utc=T(end),
              local_end_utc=T(end))
    out.update({k: str(v) for k, v in kw.items()})
    return out


def red(n, start, end, specs='1', **kw):
    out = row(n, start, specs, ci_outcome='red', ci_launch_utc=T(start), ci_end_utc=T(end),
              ci_min=end - start, local_end_utc=T(end))
    out.update({k: str(v) for k, v in kw.items()})
    return out


def static(n, start, local_end, specs='1', cls='static-preflight', **kw):
    out = row(n, start, specs, local_verdict='red', local_red_class=cls, ci_outcome=NOCI,
              local_end_utc=T(local_end))
    out.update({k: str(v) for k, v in kw.items()})
    return out


def both(rows):
    return rr.replay(rows, 'old'), rr.replay(rows, 'new')


def col(result, key):
    return [a.get(key) for a in result.attempts]


class InputTests(unittest.TestCase):
    def test_five_dates_end_on_latest_input_date(self):
        rows = [merged(i, i * 1440, i * 1440 + 10, str(i + 1)) for i in range(7)]
        old = rr.replay(rows, 'old')
        self.assertEqual(old.dates, ('2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-14'))
        self.assertEqual((old.row_count, old.merged_specs, old.merged_ids), (5, 5, ['3', '4', '5', '6', '7']))
        self.assertAlmostEqual(old.numerator, 50.0)
        self.assertAlmostEqual(old.minutes_per_merged_spec, 10.0)
        # boundary: a row exactly five dates back is out, four dates back is in
        edge = rr.replay([merged(1, 0, 10, 'a'), merged(2, 4 * 1440 + 5, 4 * 1440 + 15, 'b')], 'old')
        self.assertEqual((edge.row_count, edge.merged_ids), (2, ['a', 'b']))
        edge = rr.replay([merged(1, 0, 10, 'a'), merged(2, 5 * 1440, 5 * 1440 + 10, 'b')], 'old')
        self.assertEqual((edge.row_count, edge.merged_ids), (1, ['b']))

    def test_no_clock_no_mutation_and_input_order_irrelevant(self):
        rows = [merged(1, 0, 10, 'a'), red(2, 0, 10, 'b'), merged(3, 20, 30, 'b'), red(4, 5, 9, 'c')]
        before = copy.deepcopy(rows)

        def clock(*_):
            raise AssertionError('clock read')
        with patch('time.time', clock), patch('time.monotonic', clock), patch('time.process_time', clock):
            first = both(rows)
            shuffled = both(list(reversed(rows)))
        self.assertEqual(rows, before)
        for a, b in zip(first, shuffled):
            self.assertEqual((a.numerator, a.merged_ids, a.attempts), (b.numerator, b.merged_ids, b.attempts))
        # parsed time, then numeric n: 10 sorts after 9 even though '10' < '9' as text
        tied = [red(10, 0, 5, 'a'), red(9, 0, 5, 'b'), merged(2, 0, 5, 'c')]
        self.assertEqual(col(rr.replay(tied, 'old'), 'row'), [2, 9, 10])

    def test_malformed_input_is_a_named_error(self):
        good = [merged(1, 0, 10, 'a')]
        cut = dict(good[0])
        del cut['bucket']
        cases = {
            'missing required columns: bucket': [cut],
            'duplicate n': good + [merged(1, 5, 15, 'b')],
            'invalid timestamp': [merged(1, 0, 10, 'a', ci_end_utc='yesterday')],
            'finite and not negative': [merged(1, 0, 10, 'a', ci_min='-1')],
            'finite and not negative ': [merged(1, 0, 10, 'a', ci_min='nan')],
            'finite and not negative  ': [merged(1, 0, 10, 'a', ci_min='inf')],
            'invalid number': [merged(1, 0, 10, 'a', ci_min='ten')],
            'n_specs is inconsistent': [merged(1, 0, 10, 'a', n_specs=2)],
            'n_specs is inconsistent ': [merged(1, 0, 10, 'a b', n_specs=1)],
            'unknown ci_outcome': [merged(1, 0, 10, 'a', ci_outcome='mystery')],
            'unknown local_red_class': [merged(1, 0, 10, 'a', local_red_class='weird')],
            'unknown local_verdict': [merged(1, 0, 10, 'a', local_verdict='maybe')],
            'start_utc is required': [merged(1, 0, 10, 'a', start_utc='')],
            'invalid row number': [merged('x', 0, 10, 'a')],
            'empty selection': [],
            'zero merged specs': [red(1, 0, 10, 'a')],
            'merged row has no window_merged_utc': [merged(1, 0, 10, 'a', window_merged_utc='')],
        }
        for message, rows in cases.items():
            for flow in ('old', 'new'):
                with self.subTest(message, flow=flow):
                    with self.assertRaisesRegex(ValueError, message.strip()):
                        rr.replay(rows, flow)
        with self.assertRaises(ValueError):
            rr.replay(good, 'sideways')

    def test_missing_is_not_zero_and_naive_is_utc(self):
        parsed = rr._parse(red(1, 0, 10, 'a', ci_min=''))
        self.assertIsNone(parsed.ci_min)
        self.assertEqual(rr._parse(red(1, 0, 10, 'a', ci_min='0')).ci_min, 0.0)
        self.assertIsNone(rr._parse(red(1, 0, 10, 'a', ci_end_utc='')).ci_end)
        naive = rr.replay([merged(1, 0, 10, 'a')], 'old')
        zone = merged(1, 0, 10, 'a')
        for key in ('start_utc', 'ci_launch_utc', 'ci_end_utc', 'window_merged_utc', 'local_end_utc'):
            zone[key] = zone[key].replace(' ', 'T') + '+00:00'
        self.assertEqual(rr.replay([zone], 'old').numerator, naive.numerator)
        shifted = dict(zone)  # same instants written in UTC+01:00
        for key in ('start_utc', 'ci_launch_utc', 'ci_end_utc', 'window_merged_utc', 'local_end_utc'):
            stamp = datetime.fromisoformat(zone[key]) + timedelta(hours=1)
            shifted[key] = stamp.isoformat().replace('+00:00', '') + '+01:00'
        self.assertEqual(rr.replay([shifted], 'old').numerator, naive.numerator)

    def test_csv_reader_rejects_structural_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a b.csv'
            head = ','.join(rr.COLUMNS)
            path.write_text(head + '\n1,2\n')
            with self.assertRaisesRegex(ValueError, 'wrong number of fields'):
                rr.read_csv(path)
            path.write_text(head + ',n\n')
            with self.assertRaisesRegex(ValueError, 'duplicate csv header'):
                rr.read_csv(path)
            path.write_bytes(b'\xff\xfe\x00')
            with self.assertRaisesRegex(ValueError, 'unreadable csv'):
                rr.read_csv(path)


class SchedulingTests(unittest.TestCase):
    def test_four_slots(self):
        rows = [merged(i, 0, 10, str(i)) for i in range(1, 5)]
        old, new = both(rows)
        self.assertEqual((old.numerator, old.merged_specs), (40.0, 4))
        self.assertEqual(col(new, 'ci_start'), [3, 3, 3, 13])
        self.assertEqual(col(new, 'completion'), [13, 13, 13, 23])
        self.assertEqual(col(new, 'queue'), [0, 0, 0, 10])
        self.assertEqual((new.numerator, new.merged_specs, new.queue_minutes), (62.0, 4, 10.0))

    def test_retry_seen_members(self):
        rows = [red(1, 0, 10), merged(2, 20, 30)]
        old, new = both(rows)
        self.assertEqual(col(old, 'eligibility'), [0, 10])
        self.assertEqual(col(old, 'queue'), [0, 10])
        self.assertEqual((old.numerator, old.merged_specs), (30.0, 1))
        self.assertEqual(col(new, 'eligibility'), [0, 13])
        self.assertEqual(col(new, 'ci_start'), [3, 16])
        self.assertEqual(col(new, 'completion'), [13, 26])
        self.assertEqual(col(new, 'queue'), [0, 0])
        self.assertEqual((new.numerator, new.merged_specs), (26.0, 1))

    def test_already_admitted(self):
        old, new = both([merged(1, 0, 10), merged(2, 20, 30)])
        self.assertEqual((old.numerator, new.numerator), (30.0, 26.0))
        self.assertEqual((old.merged_specs, new.merged_specs, new.row_count), (1, 1, 2))
        self.assertEqual(new.admitted_rows, [1, 2])

    def test_ties_break_on_numeric_n_and_late_row_waits(self):
        rows = [red(1, 0, 30, 'a'), red(2, 0, 20, 'b'), merged(3, 0, 10, 'c'), merged(4, 0, 5, 'd')]
        _, new = both(rows)
        self.assertEqual(col(new, 'ci_start'), [3, 3, 3, 13])
        self.assertEqual(col(new, 'completion'), [33, 23, 13, 18])
        self.assertEqual(col(new, 'queue'), [0, 0, 0, 10])
        self.assertEqual(col(new, 'service'), [33, 23, 13, 8])
        self.assertEqual((new.numerator, new.merged_specs), (87.0, 2))

    def test_sweep_reject_takes_no_slot_and_waits_for_none(self):
        rows = [red(1, 0, 100, 'a'), red(2, 0, 100, 'b'), merged(3, 0, 100, 'c'), static(4, 0, 1, 'd')]
        _, new = both(rows)
        self.assertEqual(col(new, 'service')[3], 3)
        self.assertEqual(col(new, 'queue')[3], 0)
        self.assertEqual(col(new, 'completion')[3], 3)
        self.assertEqual(col(new, 'ci_start'), [3, 3, 3, None])

    def test_mixed_seen_and_new_members(self):
        old, new = both([red(1, 0, 10, 'A'), merged(2, 5, 15, 'A B')])
        self.assertEqual(col(old, 'eligibility'), [0, 10])
        self.assertEqual(col(old, 'queue'), [0, 0])
        self.assertEqual((old.numerator, old.merged_specs), (20.0, 2))
        self.assertEqual(col(new, 'eligibility'), [0, 13])
        self.assertEqual((new.numerator, new.merged_specs), (26.0, 2))
        # the new member's release (40) is later than the seen member's completion (13)
        old, new = both([red(1, 0, 10, 'A'), merged(2, 40, 50, 'A B')])
        self.assertEqual(col(old, 'eligibility'), [0, 40])
        self.assertEqual(col(new, 'eligibility'), [0, 40])
        self.assertEqual(col(new, 'ci_start'), [3, 43])
        self.assertEqual((old.numerator, new.numerator), (20.0, 26.0))

    def test_runner_frees_before_merge_tail_but_successor_waits_for_it(self):
        rows = [merged(1, 0, 30, 'a', tail=20), merged(2, 0, 10, 'b'), merged(3, 0, 10, 'c'),
                merged(4, 0, 10, 'd'), merged(5, 50, 60, 'a')]
        old, new = both(rows)
        self.assertEqual(col(new, 'ci_start'), [3, 3, 3, 13, 36])
        self.assertEqual(col(new, 'completion'), [33, 13, 13, 23, 46])
        self.assertEqual(col(new, 'eligibility')[4], 33)
        self.assertEqual(col(new, 'service'), [33, 13, 13, 13, 13])
        self.assertEqual((new.numerator, new.merged_specs), (95.0, 4))
        self.assertEqual((old.numerator, old.merged_specs), (90.0, 4))
        self.assertEqual(col(old, 'queue')[4], 20)  # eligible at old completion 30, started at 50

    def test_dependency_delayed_sweep_competes_with_independent_sweep(self):
        rows = [red(1, 0, 100, 'x'), red(2, 0, 100, 'y'), merged(3, 0, 10, 'A'),
                merged(4, 1, 11, 'A'), merged(5, 12, 22, 'B')]
        old, new = both(rows)
        self.assertEqual(col(new, 'ci_start'), [3, 3, 3, 25, 15])
        self.assertEqual(col(new, 'queue'), [0, 0, 0, 9, 0])
        self.assertEqual(col(new, 'completion'), [103, 103, 13, 35, 25])
        self.assertEqual((old.numerator, new.numerator, new.merged_specs), (230.0, 254.0, 2))

    def test_queue_is_recomputed_not_copied(self):
        # old flow queues 9 minutes behind a predecessor; the new flow has none to charge
        rows = [red(1, 0, 20, 'a'), merged(2, 11, 21, 'a')]
        old, new = both(rows)
        self.assertEqual(col(old, 'queue'), [0, 0])
        rows = [red(1, 0, 20, 'a'), merged(2, 30, 40, 'a')]
        old, new = both(rows)
        self.assertEqual(col(old, 'queue'), [0, 10])
        self.assertEqual(col(new, 'queue'), [0, 0])


class AssumptionTests(unittest.TestCase):
    def test_static_then_retry(self):
        for cls in ('static-preflight', 'guard-tests-only'):
            with self.subTest(cls):
                old, new = both([static(1, 0, 1, cls=cls), merged(2, 20, 30)])
                self.assertEqual((old.numerator, old.merged_specs), (30.0, 1))
                self.assertEqual(col(old, 'queue'), [0, 19])
                self.assertEqual(col(new, 'completion')[0], 3)
                self.assertEqual(col(new, 'ci_start'), [None, 6])
                self.assertEqual((new.numerator, new.merged_specs), (16.0, 1))

    def test_nonstatic_reds_pay_full_ci_and_the_fixture_loses(self):
        for cls in ('other-tests', 'guard+other-tests', 'run-broken(timeout/zero-tests)', 'vitest'):
            with self.subTest(cls):
                cut = row(1, 0, '1', local_verdict='red', local_red_class=cls,
                          ci_outcome='killed-by-local-red', local_end_utc=T(1), ci_min=30,
                          local_cancelled_ci='True', first_to_finish='local')
                old, new = both([cut, merged(2, 0, 10, '2')])
                self.assertEqual((old.numerator, old.merged_specs), (11.0, 1))
                self.assertEqual((new.numerator, new.merged_specs), (46.0, 1))
                self.assertEqual(new.admitted_rows, [2])
                self.assertGreaterEqual(new.minutes_per_merged_spec, old.minutes_per_merged_spec)
                self.assertEqual(new.censored_rows, [1])

    def test_cancel_intent_flag_alone_changes_nothing(self):
        plain = [static(1, 0, 1), merged(2, 20, 30, '2')]
        flagged = [dict(r, local_cancelled_ci='True') for r in plain]
        self.assertEqual([x.numerator for x in both(plain)], [x.numerator for x in both(flagged)])

    def test_lost_endpoint_row_36_shape(self):
        lost = row(3, 0, '3', local_verdict='lost', local_red_class='other-tests', ci_outcome=NOCI)
        rows = [merged(1, 0, 10, '1'), merged(2, 0, 30, '2'), lost]
        old, new = both(rows)
        self.assertEqual((old.numerator, old.merged_specs), (60.0, 2))
        self.assertEqual((new.numerator, new.merged_specs), (69.0, 2))
        self.assertEqual(new.merged_ids, ['1', '2'])
        for res, kind in ((old, 'old-service'), (new, 'ci-duration')):
            (item,) = res.imputations
            self.assertEqual((item['kind'], item['row'], item['value']), (kind, 3, 20.0))
            self.assertEqual(item['donors'], [(1, 10.0), (2, 30.0)])
            self.assertTrue(item['scope'].startswith('same-date'))
        # donors on another selected date: the five-day median applies
        far = [merged(1, -1440, -1430, '1'), merged(2, -1440, -1410, '2'),
               row(3, 0, '3', local_verdict='lost', local_red_class='other-tests', ci_outcome=NOCI)]
        old, new = both(far)
        self.assertEqual((old.numerator, new.numerator), (60.0, 69.0))
        self.assertEqual(old.imputations[0]['scope'], 'five-day')
        self.assertEqual(new.imputations[0]['scope'], 'five-day')
        # odd donor count takes the middle value
        odd = [merged(1, 0, 10, '1'), merged(2, 0, 20, '2'), merged(4, 0, 60, '4'), lost]
        self.assertEqual(rr.replay(odd, 'old').imputations[0]['value'], 20.0)

    def test_no_donor_is_undetermined(self):
        lost = row(3, 0, '3', local_verdict='lost', local_red_class='other-tests', ci_outcome=NOCI)
        zero = merged(1, 0, 0, '1')
        with self.assertRaisesRegex(ValueError, 'no donor evidence for old-service'):
            rr.replay([zero, lost], 'old')
        nolaunch = merged(1, 0, 10, '1', ci_launch_utc='')
        rr.replay([nolaunch, lost], 'old')
        with self.assertRaisesRegex(ValueError, 'no donor evidence for ci-duration'):
            rr.replay([nolaunch, lost], 'new')
        with self.assertRaisesRegex(ValueError, 'no old endpoint'):
            rr.replay([merged(1, 0, 10, '1'), row(3, 0, '3', local_verdict='red', ci_outcome=NOCI)], 'old')

    def test_missing_duration_differs_from_recorded_zero(self):
        base = merged(1, 0, 10, '1')
        zero = red(2, 0, 0, '2')
        old, new = both([base, zero])
        self.assertEqual((old.numerator, new.numerator), (10.0, 16.0))
        self.assertEqual(new.imputations, [])
        blank = row(2, 0, '2', ci_outcome='undetermined-infra/preflight', local_end_utc=T(2))
        old, new = both([base, blank])
        self.assertEqual((old.numerator, new.numerator), (12.0, 26.0))
        self.assertEqual(new.imputations[0]['value'], 10.0)
        self.assertEqual(old.imputations, [])

    def test_unobserved_retry_is_not_synthesized(self):
        old, new = both([static(1, 0, 1, '9'), merged(2, 0, 10, '2')])
        self.assertEqual((old.merged_ids, new.merged_ids), (['2'], ['2']))
        self.assertEqual((new.numerator, old.numerator), (13.0 + 3.0, 11.0))

    def test_assumptions_are_printed(self):
        new = rr.replay([merged(1, 0, 10)], 'new')
        self.assertEqual(len(new.assumptions), 5)
        self.assertTrue(all(f'SD15({i + 1})' in a for i, a in enumerate(new.assumptions)))


class AdmissionTests(unittest.TestCase):
    def shape(self, n, spec, **kw):
        out = merged(n, 0, 31, spec, local_verdict='red', local_red_class='run-broken(timeout/zero-tests)',
                     local_cancelled_ci='True', first_to_finish='CI', local_end_utc=T(60))
        out.update({k: str(v) for k, v in kw.items()})
        return out

    def test_ci_finished_first_58_and_166(self):
        for n, spec in ((58, '2026'), (166, '2027')):
            with self.subTest(n=n):
                keep = [self.shape(n, spec), merged(1, 100, 110, '1')]
                old, new = both(keep)
                self.assertEqual((old.merged_ids, new.merged_ids), (sorted([spec, '1']),) * 2)
                self.assertEqual(old.admitted_rows, sorted([n, 1]))
                killed = [self.shape(n, spec, ci_outcome='killed-by-local-red'), merged(1, 100, 110, '1')]
                for result in both(killed):
                    self.assertEqual(result.merged_ids, ['1'])
                flag = [self.shape(n, spec, local_cancelled_ci='False'), merged(1, 100, 110, '1')]
                self.assertEqual([r.merged_ids for r in both(flag)], [sorted([spec, '1'])] * 2)

    def test_only_green_and_merged_is_admitted(self):
        keep = merged(1, 0, 10, '1')
        for label, bad in (('green-without-window', merged(2, 0, 10, '2', window_outcome='', window_merged_utc='')),
                           ('red', red(2, 0, 10, '2', window_outcome='merged', window_merged_utc=T(10))),
                           ('stale', row(2, 0, '2', ci_outcome='killed-stale(base moved/restart; no verdict)', local_end_utc=T(3))),
                           ('refused', row(2, 0, '2', ci_outcome='refused(cap)', local_end_utc=T(3))),
                           ('infra', row(2, 0, '2', ci_outcome='undetermined-infra/preflight', local_end_utc=T(3))),
                           ('unstarted', static(2, 0, 1, '2'))):
            with self.subTest(label):
                old, new = both([keep, bad])
                self.assertEqual((old.merged_ids, new.merged_ids), (['1'], ['1']))
                self.assertEqual(new.refused_by_old_but_merged, [])

    def test_distinct_ids_and_repeats_still_cost(self):
        old, new = both([merged(1, 0, 10, '7 8'), merged(2, 0, 10, '7')])
        self.assertEqual((old.merged_ids, old.merged_specs), (['7', '8'], 2))
        self.assertEqual((old.numerator, old.minutes_per_merged_spec), (20.0, 10.0))
        self.assertEqual((new.merged_specs, new.numerator), (2, 26.0 + 0.0))

    def test_earlier_refusal_stays_refused(self):
        rows = [row(1, 0, '8', ci_outcome='refused(cap)', local_end_utc=T(1)), merged(2, 5, 15, '8')]
        for result in both(rows):
            self.assertEqual(result.merged_ids, ['8'])
            self.assertEqual(result.admitted_rows, [2])
            self.assertEqual(result.refused_by_old_but_merged, [])

    def test_sweep_reject_of_a_merged_row_is_dropped_not_promoted(self):
        rows = [static(1, 0, 5, '5', ci_outcome='green', window_outcome='merged',
                       window_merged_utc=T(12), ci_end_utc=T(11), ci_launch_utc=T(0), ci_min=11),
                merged(2, 0, 10, '2')]
        old, new = both(rows)
        self.assertEqual((old.merged_ids, new.merged_ids), (['2', '5'], ['2']))
        self.assertEqual(new.refused_by_old_but_merged, [])

    def test_safety_list_is_computed_from_new_admissions(self):
        rows = [red(1, 0, 10, '5'), merged(2, 0, 10, '2')]
        _, clean = both(rows)
        self.assertEqual(clean.refused_by_old_but_merged, [])
        real = rr._new_admitted
        with patch.object(rr, '_new_admitted', lambda chosen: chosen):
            unsafe = rr.replay(rows, 'new')
        self.assertEqual(unsafe.refused_by_old_but_merged, [{'row': 1, 'spec': '5'}])
        self.assertEqual(unsafe.merged_ids, ['2', '5'])
        self.assertIs(rr._new_admitted, real)
        self.assertEqual(rr.replay(rows, 'old').refused_by_old_but_merged, [])


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / 'with space'
        self.dir.mkdir()

    def write(self, rows, name='rows one.csv'):
        path = self.dir / name
        with open(path, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=rr.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def run_doit(self, *args):
        done = subprocess.run([str(REPO / 'doit'), 'rehearsal-replay', *args], capture_output=True,
                              text=True, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def test_improvement_exits_zero(self):
        code, out, err = self.run_doit('--csv', str(self.write([static(1, 0, 1), merged(2, 20, 30)])))
        self.assertEqual(code, 0, out + err)
        for text in ('old minutes_per_merged_spec=30.0 merged_specs=1 numerator=30.0',
                     'new minutes_per_merged_spec=16.0 merged_specs=1 numerator=16.0',
                     'selected_dates=2026-11-04..2026-11-08 count=5 rows=2',
                     'refused_by_old_but_merged=0', 'assumption: SD15(1)', 'imputations: old=0 new=0',
                     'censored_ci_rows', 'limitation:', 'replay_cpu_seconds=', 'result: PASS'):
            self.assertIn(text, out)

    def test_equality_and_regression_exit_one(self):
        equal = [merged(1, 0, 10, '1'), static(2, 0, 6, '2')]
        code, out, _ = self.run_doit('--csv', str(self.write(equal)))
        self.assertEqual(code, 1, out)
        self.assertIn('old minutes_per_merged_spec=16.0', out)
        self.assertIn('new minutes_per_merged_spec=16.0', out)
        self.assertIn('result: FAIL new is not lower than old', out)
        code, out, _ = self.run_doit('--csv', str(self.write([merged(1, 0, 10, '1')])))
        self.assertEqual(code, 1, out)
        self.assertIn('new minutes_per_merged_spec=13.0', out)
        self.assertIn('result: FAIL', out)

    def test_unsafe_result_at_the_comparison_boundary_exits_one(self):
        path = self.write([static(1, 0, 1, '5\x1b[2J'), merged(2, 20, 30, '2')])
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rr, '_new_admitted', lambda chosen: chosen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rr.main(['--csv', str(path)])
        text = out.getvalue()
        self.assertEqual(code, 1, text + err.getvalue())
        self.assertIn('refused_by_old_but_merged=1', text)
        self.assertIn('result: FAIL safety violation', text)
        self.assertNotIn('\x1b', text)

    def test_invalid_input_exits_two_with_one_safe_line(self):
        hostile = merged(1, 0, 10, '1', ci_outcome='x\x1b[2J\nresult: PASS')
        cut = dict(merged(1, 0, 10, '1'))
        for label, args, expect in (
                ('token', ('--csv', str(self.write([hostile]))), 'unknown ci_outcome'),
                ('empty', ('--csv', str(self.write([], 'empty.csv'))), 'empty selection'),
                ('zero', ('--csv', str(self.write([red(1, 0, 10)], 'zero.csv'))), 'zero merged specs'),
                ('absent', ('--csv', str(self.dir / 'nope.csv')), 'invalid input'),
                ('directory', ('--csv', str(self.dir)), 'invalid input'),
                ('nocolumns', ('--csv', str(self._short(cut))), 'missing required columns')):
            with self.subTest(label):
                code, out, err = self.run_doit(*args)
                self.assertEqual(code, 2, out + err)
                self.assertEqual(out, '')
                self.assertIn(expect, err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn('\x1b', err)
        self.assertEqual(self.run_doit()[0], 2)

    def _short(self, source):
        path = self.dir / 'short.csv'
        names = [c for c in rr.COLUMNS if c != 'bucket']
        with open(path, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=names, extrasaction='ignore')
            writer.writeheader()
            writer.writerow(source)
        return path

    def test_csv_text_is_data_and_no_process_is_built_from_it(self):
        hostile = merged(1, 0, 10, '1', bucket='$(touch pwned); `id`', local_attribution='"; rm -rf /')
        marker = self.dir / 'pwned'
        code, out, _ = self.run_doit('--csv', str(self.write([hostile, static(2, 0, 1, '2')])))
        self.assertIn(code, (0, 1))
        self.assertFalse(marker.exists())
        source = (REPO / 'src/rehearsal_replay.py').read_text()
        for banned in ('subprocess', 'os.system', 'eval(', 'exec(', '__import__', 'importlib'):
            self.assertNotIn(banned, source)

    def test_dispatcher_help_lists_the_command(self):
        done = subprocess.run([str(REPO / 'doit'), 'help'], capture_output=True, text=True, timeout=60)
        self.assertIn('doit rehearsal-replay --csv', done.stdout + done.stderr)


def snapshot_check(path):
    """AC6: fixtures first, then the exact snapshot, then the real dispatcher."""
    suite = unittest.TestSuite()
    for case in (SchedulingTests, AssumptionTests, AdmissionTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    if not unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite).wasSuccessful():
        print('snapshot-check: prerequisite fixtures failed; snapshot not opened', file=sys.stderr)
        return 1
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != SNAPSHOT_SHA:
        print('snapshot-check: sha256 differs from the retained probe CSV', file=sys.stderr)
        return 1
    raw = rr.read_csv(path)
    old = rr.replay(raw, 'old')
    checks = (('rows', old.row_count, 183), ('merged_specs', old.merged_specs, 92),
              ('dates', old.dates, ('2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07', '2026-11-08')))
    for name, got, want in checks:
        if got != want:
            print(f'snapshot-check: {name} {got!r} != {want!r}', file=sys.stderr)
            return 1
    for name, got, want in (('numerator', old.numerator, 19716.65),
                            ('minutes_per_merged_spec', old.minutes_per_merged_spec, 214.31141304347827),
                            ('queue', old.queue_minutes, 14017.616666666667),
                            ('service', old.service_minutes, 5699.033333333334)):
        if abs(got - want) > 1e-6:
            print(f'snapshot-check: {name} {got!r} != {want!r}', file=sys.stderr)
            return 1
    (lost,) = old.imputations
    if (lost['row'], len(lost['donors']), abs(lost['value'] - 32.43333333333333) > 1e-6) != (36, 50, False) \
            or not lost['scope'].startswith('same-date'):
        print('snapshot-check: row 36 imputation differs from the preregistered donors', file=sys.stderr)
        return 1
    if [r for r in raw if r['n'] == '182'][0]['ci_end_utc'] == '' or any(i['row'] == 182 for i in old.imputations):
        print('snapshot-check: row 182 must use its observed CI endpoint', file=sys.stderr)
        return 1
    done = subprocess.run([str(REPO / 'doit'), 'rehearsal-replay', '--csv', str(path)],
                          capture_output=True, text=True, timeout=300)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    if done.returncode != 0 or 'refused_by_old_but_merged=0' not in done.stdout \
            or 'old minutes_per_merged_spec=' not in done.stdout or 'new minutes_per_merged_spec=' not in done.stdout \
            or 'merged_specs=92' not in done.stdout or 'imputed old old-service rows=36 ' not in done.stdout:
        print(f'snapshot-check: comparison failed (doit exit {done.returncode})', file=sys.stderr)
        return 1
    print('snapshot-check: ok')
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--snapshot-check':
        sys.exit(snapshot_check(sys.argv[2]))
    unittest.main()
