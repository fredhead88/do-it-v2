#!/usr/bin/env python3
"""Queued engine work keeps its own Executor owner in mixed-project snapshots."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix='merge-routing-import-')
os.environ['DOIT_ROOT'] = _ISO.name
import fold
import lane_rules
import nextstep

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)


def queued(sid='L-spec-9001', project='do-it-v2', age=180):
    return dict(type='queue-add', subject=sid, project=project, branch=sid.lower(),
                actor='executor', ts=(NOW - timedelta(minutes=age)).isoformat(),
                _src='L-executor-fixture.jsonl:' + sid.split('-')[-1])


def steps(events):
    specs = {e['subject']: dict(state='reviewing', evs=[e]) for e in events}
    return nextstep.next_steps(events, specs, NOW, lane_rules.load())


class MergeRoutingTests(unittest.TestCase):
    def test_direct_merge_is_owned_and_ages_from_queue_even_with_window_project(self):
        events = [queued(), queued('L-spec-9002', 'albert-scott')]
        own = {s.item: s for s in steps(events)}
        direct = own['L-spec-9001']
        self.assertEqual((direct.state, direct.action, direct.owner), ('action', 'merge-direct', 'executor'))
        self.assertEqual((own['L-spec-9002'].action, own['L-spec-9002'].owner), ('merge', 'tick'))
        self.assertEqual(direct.since, events[0]['ts'])
        self.assertIn('current main', direct.reason)
        row = fold.step_row(direct, lane_rules.load(), NOW)
        self.assertEqual((row['rule'], row['action'], row['overdue']), ('merge', 'merge-direct', True))

    def test_unknown_project_remains_explicitly_held(self):
        own, = steps([queued(project='other')])
        self.assertEqual(own.state, 'hold')
        self.assertIn('undetermined: project', own.reason)


    def test_queue_without_step_two_hours_alerts_once(self):
        import merge_queue_watchdog as watch
        events = [queued()]
        self.assertEqual(watch.due(events, [], NOW), __import__('merge_queue').queue(events))
        self.assertEqual(watch.due(events, steps(events), NOW), [])
        self.assertEqual(watch.due([queued(age=120)], [], NOW), [])
        with tempfile.TemporaryDirectory(prefix='queue-no-step-') as tmp:
            root = Path(tmp)
            with patch('look._send_operator', side_effect=AssertionError('no notification expected')):
                self.assertEqual(len(watch.check(events, {}, NOW, root, steps=[])), 1)
                self.assertEqual(watch.check(events, {}, NOW, root, steps=[]), [])
            rows = [json.loads(line) for line in (root / 'events/L-look-local.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['kind'], 'queue-no-step')



if __name__ == '__main__':
    unittest.main()
