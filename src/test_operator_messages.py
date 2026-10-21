"""Operator messages: waiting classes and stale evidence, without any sender."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import stuck_digest as digest

NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)
SID = 'L-spec-9001'


def event(kind, minutes, subject=SID, actor='thinker', **fields):
    return dict(type=kind, subject=subject, actor=actor,
                ts=(NOW - timedelta(minutes=minutes)).isoformat(), **fields)


def clean():
    return [event('spec-written', 180, actor='planner', project='fixture'),
            event('build-done', 150, actor='builder', status='DONE'),
            event('review', 120, actor='reviewer', n_blocking=0, depth='full')]


def row(sid=SID, **fields):
    return dict(spec=sid, rule='idle', action='none', actor='thinker', overdue=True,
                reason_live=False, reason='condition met', since=event('x', 180)['ts'],
                limit_min=60, **fields)


class WaitingMessages(unittest.TestCase):
    def build(self, events, rows=None):
        events = sorted(events, key=lambda e: e['ts'])
        with patch.object(digest.lane_rules, 'due', return_value=rows or [row()]):
            return digest.build(events, NOW)

    def assert_merge(self, events):
        obj = self.build(events)
        self.assertEqual(obj['action_rows'], [])
        self.assertEqual([r['waiting_for'] for r in obj['waiting_rows']], ['merge'])
        self.assertEqual(obj['rows'], [row()])
        self.assertIn('1 finished spec is waiting its turn to merge.', digest.render_telegram(obj))
        self.assertIn(SID, digest.next_steps(obj))
        self.assertNotIn('Thinker:', digest.next_steps(obj))

    def assert_stuck(self, events):
        obj = self.build(events)
        self.assertEqual([r['spec'] for r in obj['action_rows']], [SID])
        self.assertEqual(obj['waiting_rows'], [])
        self.assertIn('1 spec is stuck and the Thinker is on it.', digest.render_telegram(obj))

    def test_latest_clean_review_and_harmless_later_events(self):
        self.assert_merge(clean())  # confirmed is absent in real legacy reviews
        self.assert_merge(clean() + [event('spawn-done', 90, actor='reviewer'),
                                    event('decision', 60, why='Keep waiting for the queue.')])
        self.assert_merge(clean() + [event('decision', -60, rework='yes')])
        self.assert_stuck(clean()[:-1])
        for blocking in (1, None, False, '0'):
            self.assert_stuck(clean()[:-1] + [event('review', 120, actor='reviewer',
                                                  n_blocking=blocking, depth='full')])
        # Exercise the real due rules as well as the classification boundary.
        obj = digest.build(clean(), NOW)
        self.assertTrue(obj['rows'])
        self.assertEqual(obj['action_rows'], [])
        self.assertTrue(all(r['waiting_for'] == 'merge' for r in obj['waiting_rows']))

    def test_later_resets_and_blocking_reviews_are_stuck(self):
        for kind, fields, actor in (
                ('decision', {'rework': 'yes'}, 'thinker'),
                ('decision', {'regrade': 'yes'}, 'thinker'),
                ('decision', {'close': 'yes'}, 'thinker'),
                ('build-started', {}, 'builder'),
                ('merge-gate-failed', {}, 'executor'),
                ('merge-conflict', {}, 'executor'),
                ('window-dropped', {}, 'executor'),
                ('verdict', {}, 'grader'),
                ('review', {'n_blocking': 1, 'depth': 'full'}, 'reviewer')):
            with self.subTest(kind=kind, fields=fields):
                self.assert_stuck(clean() + [event(kind, 60, actor=actor, **fields)])
        # Same-time contradictory evidence cannot prove readiness.
        self.assert_stuck(clean() + [event('decision', 120, rework='yes')])
        self.assert_stuck(clean() + [event('review', 60, actor='reviewer', n_blocking=1)])
        # A fresh clean review after a reset restores readiness.
        self.assert_merge(clean() + [event('decision', 90, rework='yes'),
                                    event('review', 60, actor='reviewer', n_blocking=0)])

    def test_standing_reject_and_unanswered_question(self):
        rejected = event('rejected-criterion', 140, actor='grader', criterion='AC1')
        self.assert_stuck(clean() + [rejected])
        self.assert_merge(clean() + [rejected, event('criterion-cleared', 60, actor='grader', criterion='AC1')])
        question = event('question', 60, asks='Investigate the missing result.', _src='fixture:1')
        self.assert_stuck(clean() + [question])
        self.assert_merge(clean() + [question, event('decision', 30, ref='fixture:1', why='Resolved.')])
        approval = dict(question, asks='Please approve the production change.')
        obj = self.build(clean() + [approval])
        self.assertEqual(obj['action_rows'][0]['owner'], 'operator')
        message = digest.render_telegram(obj)
        self.assertIn('NEEDS YOU:', message)
        self.assertIn('Recommend', message)
        self.assertNotIn('finished spec', message)

    def test_merge_window_rulings_and_invalidation(self):
        base = clean()[:2]
        for why in ('Not stuck: reviewed clean, waiting for a merge window.',
                    'NOT STUCK: waiting in the MERGE WINDOW queue.',
                    'Not stuck: already enqueued in window c31; queue ahead.'):
            with self.subTest(why=why):
                ruling = event('decision', 90, subject='lane-stale:' + SID, why=why)
                self.assert_merge(base + [ruling])
                self.assert_stuck(base + [ruling, event('merge-gate-failed', 60, actor='executor')])
                self.assert_stuck(base + [ruling, event('decision', 60, regrade='yes')])
                self.assert_stuck(base + [ruling, event('must-fix', 100, actor='reviewer', criterion='AC1')])
                self.assert_stuck(base + [ruling, event('question', 60, asks='Investigate.')])
        for why in ('Waiting for a merge window.', 'Not stuck: unrelated work.',
                    'Not stuck: enqueued in window c31.'):
            self.assert_stuck(base + [event('decision', 90, subject='lane-stale:' + SID, why=why)])
        self.assert_stuck(base + [event('decision', 90, subject='lane-stale:' + SID,
                                       actor='builder', why='Not stuck: merge window.')])

    def test_dependency_waiting_until_target_merges(self):
        base = clean()[:1]
        target = 'L-spec-9002'
        held = event('dependency-hold', 120, waits_for=target, why='Wait for the prerequisite.')
        obj = self.build(base + [held])
        self.assertEqual(obj['action_rows'], [])
        self.assertEqual(obj['waiting_rows'][0]['waiting_for'], 'dependency')
        self.assertIn('1 spec is waiting for other work to merge first.', digest.render_telegram(obj))
        self.assertIn(SID, digest.next_steps(obj))
        self.assert_stuck(base + [held, event('shipped', 60, subject=target, actor='executor')])
        self.assert_stuck(base + [dict(held, waits_for=SID)])
        self.assert_stuck(base + [dict(held, waits_for='')])
        self.assert_stuck(clean() + [dict(held, waits_for=SID, ts=event('x', 60)['ts'])])

    def test_mixed_classes_and_full_next_steps(self):
        waiting = [dict(row('L-spec-9002'), waiting_for='merge'),
                   dict(row('L-spec-9003'), waiting_for='dependency'),
                   dict(row('L-spec-9004'), waiting_for='active')]
        obj = dict(rows=[row()], action_rows=[dict(row(), owner='thinker')], waiting_rows=waiting)
        self.assertEqual(digest.render_telegram(obj),
                         'Nothing needs you today.\n'
                         '1 finished spec is waiting its turn to merge.\n'
                         '1 spec is waiting for other work to merge first.\n'
                         '1 spec is waiting on work already in progress.\n'
                         '1 spec is stuck and the Thinker is on it.')
        message = digest.render_telegram(obj)
        for jargon in ('with the Thinker', '{', '"', 'L-spec-', 'idle', 'regrade'):
            self.assertNotIn(jargon, message)
        for sid in [SID, 'L-spec-9002', 'L-spec-9003', 'L-spec-9004']:
            self.assertIn(sid, digest.next_steps(obj))
        active = dict(row(), reason_live=True)
        obj = self.build(clean()[:1], [active])
        self.assertEqual(obj['waiting_rows'][0]['waiting_for'], 'active')
        self.assertEqual(digest.render_telegram(dict(rows=[], action_rows=[], waiting_rows=[])),
                         'Nothing needs you today. No stalled work needs intervention.')
        # Count distinct specs, with action taking priority over waiting.
        obj = dict(rows=[], action_rows=[dict(row(), owner='thinker')] * 2,
                   waiting_rows=[dict(row(), waiting_for='merge')])
        self.assertNotIn('finished', digest.render_telegram(obj))
        self.assertIn('1 spec is stuck', digest.render_telegram(obj))

    def test_plural_counts_and_markdown_keep_the_full_list(self):
        waiting = [dict(row(f'L-spec-{i:04d}'), waiting_for='merge') for i in range(9000, 9064)]
        actions = [dict(row(f'L-spec-{i:04d}'), owner='thinker') for i in range(9100, 9118)]
        obj = dict(rows=waiting + actions, action_rows=actions, waiting_rows=waiting)
        self.assertEqual(digest.render_telegram(obj),
                         'Nothing needs you today.\n'
                         '64 finished specs are waiting their turn to merge.\n'
                         '18 specs are stuck and the Thinker is on them.')
        real = self.build(clean())
        with patch('health_alarms.summary_lines', return_value=[]):
            markdown = digest.render_md(real, clean(), NOW)
        self.assertIn('spec: ' + SID, markdown)  # original timer row
        self.assertIn('Waiting their turn to merge. No reply needed from you. [' + SID + ']', markdown)
        self.assertIn('Waiting to merge, on a dependency, or on work in progress: 1 rows.', markdown)


if __name__ == '__main__':
    unittest.main()
