"""Stale routine steps reach the Executor before the Thinker."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lane_rules
import look
from nextstep import Step
import tick


class LaneStaleOwnerTests(unittest.TestCase):
    def test_executor_first_then_one_thinker_escalation(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        step = Step('L-spec-fixture', 'spec', 'merging', 'action', 'merge-gate',
                    'tick', 'review:fixture', 'clean review awaits gate', None,
                    (now - timedelta(hours=3)).isoformat())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tick, '_steps', return_value=[step]), \
                patch.object(lane_rules, 'ladder', return_value='inbox'):
            root = Path(directory)
            path = root / 'events' / look.LEDGER_NAME
            look._check_lane_stale([], now, root, [])
            rows = look._read_jsonl(path)
            self.assertEqual(rows[0]['owner'], 'executor')
            self.assertEqual(rows[0]['nextstep'], 'merge-gate')
            self.assertEqual(rows[0]['waiting_on'], 'review:fixture')
            for minute in (0, 59, 60, 61, 120):
                look._check_lane_stale([], now + timedelta(minutes=minute), root, [])
            rows = look._read_jsonl(path)
            self.assertEqual([e['owner'] for e in rows], ['executor', 'thinker'])

    def test_judgment_hold_stays_thinker_owned(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        step = Step('L-spec-fixture', 'spec', 'planned', 'hold', None,
                    'thinker', None, 'spec-ambiguity', 'Thinker decision',
                    (now - timedelta(hours=3)).isoformat())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tick, '_steps', return_value=[step]), \
                patch.object(lane_rules, 'ladder', return_value='inbox'):
            look._check_lane_stale([], now, Path(directory), [])
            rows = look._read_jsonl(Path(directory) / 'events' / look.LEDGER_NAME)
        self.assertEqual(rows[0]['owner'], 'thinker')
        self.assertEqual(rows[0]['waiting_on'], 'Thinker decision')


if __name__ == '__main__':
    unittest.main()
