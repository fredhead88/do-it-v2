"""Operator messages: waiting classes and stale evidence, without any sender."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import stuck_digest as digest
from nextstep import Step

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
    def build(self, events, steps=None):
        events = sorted(events, key=lambda e: e['ts'])
        if steps is None:
            steps = [Step(SID, 'spec', 'merging', 'action', 'merge', 'tick',
                          'fixture:1', 'condition met', None, event('x', 180)['ts'])]
        with patch.object(digest.tick, '_steps', return_value=steps):
            return digest.build(events, NOW)

    def assert_merge(self, events):
        obj = self.build(events)
        self.assertEqual(obj['action_rows'], [])
        self.assertEqual([r['waiting_for'] for r in obj['waiting_rows']], ['merge'])
        self.assertIn('1 finished spec is waiting its turn to merge.', digest.render_telegram(obj))
        self.assertIn(SID, digest.next_steps(obj))
        self.assertNotIn('Thinker:', digest.next_steps(obj))

    def assert_stuck(self, events):
        obj = self.build(events, [Step(
            SID, 'spec', 'planned', 'hold', None, 'executor', None,
            'judgment needed', 'executor decision', event('x', 180)['ts'])])
        self.assertEqual([r['spec'] for r in obj['action_rows']], [SID])
        self.assertEqual(obj['waiting_rows'], [])
        self.assertIn('1 spec is stuck and the Thinker is on it.', digest.render_telegram(obj))

    def test_merge_and_judgment_step_messages(self):
        # Eligibility belongs to nextstep; these fixtures exercise its reader.
        self.assert_merge(clean())
        self.assert_stuck(clean()[:-1])

    def test_operator_step_message(self):
        obj = self.build(clean(), [Step(
            SID, 'spec', 'planned', 'hold', None, 'operator', None,
            'Please approve the production change.', 'operator approval',
            event('x', 180)['ts'])])
        self.assertEqual(obj['action_rows'][0]['owner'], 'operator')
        message = digest.render_telegram(obj)
        self.assertIn('NEEDS YOU:', message)
        self.assertIn('Recommend', message)
        self.assertNotIn('finished spec', message)

    def test_dependency_step_message(self):
        obj = self.build(clean()[:1], [Step(
            SID, 'spec', 'planned', 'hold', None, 'tick', 'fixture:2',
            'held: waiting for prerequisite', 'prerequisite shipped',
            event('x', 180)['ts'])])
        self.assertEqual(obj['action_rows'], [])
        self.assertEqual(obj['waiting_rows'][0]['waiting_for'], 'dependency')
        self.assertIn('1 spec is waiting for other work to merge first.', digest.render_telegram(obj))
        self.assertIn(SID, digest.next_steps(obj))

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
        active = Step(SID, 'spec', 'building', 'action', 'build', 'tick',
                      'fixture:3', 'active build', None, event('x', 180)['ts'])
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
