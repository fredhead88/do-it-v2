"""Evaluation work is bounded per snapshot and preserves diagnostic rows."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dispatch
import fold
import lane_driver
import lane_rules


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for module in (fold, dispatch):
            patcher = patch.object(module, 'ROOT', Path(self.tmp.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)

    def event(self, kind, sid, minutes, **fields):
        return dict(type=kind, subject=sid, ts=(self.now - timedelta(minutes=minutes)).isoformat(),
                    **fields)

    def test_answered_scan_once_and_cache_does_not_outlive_snapshot(self):
        events, specs = [], {}
        for n in range(10):
            sid = f'L-spec-{1000 + n}'
            events += [self.event('spec-written', sid, 180),
                       self.event('build-done', sid, 120, status='BLOCKED')]
            specs[sid] = dict(state='graded')
        question = self.event('question', 'L-spec-1000', 120, _src='builder:2')
        events.append(question)
        with patch.object(fold, 'answered', wraps=fold.answered) as answered:
            rows = lane_rules.due(events, specs, self.now)
            self.assertEqual(answered.call_count, 1)
            self.assertNotIn('L-spec-1000', {r['spec'] for r in rows if r['rule'] == 'idle'})
            # Cross-subject refs must still clear questions, and a fresh call
            # must see the newly answered question (no process-global cache).
            events.append(self.event('decision', 'another-subject', 60, ref='builder:2'))
            rows = lane_rules.due(events, specs, self.now)
            self.assertEqual(answered.call_count, 2)
            self.assertIn('L-spec-1000', {r['spec'] for r in rows if r['rule'] == 'idle'})

    def test_driver_omits_watchdog_work_and_keeps_same_owned_rows(self):
        sid = 'L-spec-1417'
        events = [self.event('spec-written', sid, 40),
                  self.event('build-blocked', sid, 20, _src='builder:3'),
                  self.event('decision', sid, 1, actor='thinker', ref='builder:3')]
        specs = {sid: dict(state='graded')}
        expected = [r for r in lane_rules.due(events, specs, self.now)
                    if r['rule'] in lane_driver.ROLES]
        with patch.object(lane_rules, '_idle', side_effect=AssertionError('watchdog work')):
            # The registry stores the callable; patch that registry as well.
            with patch.dict(lane_rules.PREDICATES, idle=lane_rules._idle):
                self.assertEqual(lane_driver._rows(events, specs, self.now), expected)

    def test_charter_resolution_only_receives_own_events(self):
        sid = 'L-spec-1451'
        events = [self.event('spec-written', sid, 40),
                  self.event('note', 'another-subject', 10)]
        with patch.object(dispatch, 'resolve_charter', wraps=dispatch.resolve_charter) as resolve:
            lane_rules.due(events, {sid: dict(state='written')}, self.now)
        self.assertTrue(resolve.called)
        self.assertTrue(all(e['subject'] == sid for e in resolve.call_args.args[0]))


if __name__ == '__main__':
    unittest.main()
