"""Bounded operational diagnostics follow the tick's current root."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fold
import tick


class LaneLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = datetime(2026, 10, 22, 10, 5, 21, tzinfo=timezone.utc)
        p = patch.object(fold, 'ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)

    def test_outcomes_are_timestamped_and_append_without_ledger_writes(self):
        lines = ['acted L-spec-1417 rebuild spawn=42', 'seat-wait L-spec-1451 rebuild',
                 'busy L-spec-1422 rebuild', 'held L-spec-1455 rebuild',
                 'suppressed L-spec-1410 rebuild']
        tick._log_lane_driver(lines, self.snapshot)
        tick._log_lane_driver(['next pass\nsecond line'], self.snapshot)
        written = (self.root / 'logs/lane-driver.log').read_text().splitlines()
        self.assertEqual(len(written), 6)
        for actual, expected in zip(written, lines):
            self.assertIn('snapshot=' + self.snapshot.isoformat(), actual)
            self.assertTrue(actual.endswith(expected))
            datetime.fromisoformat(actual.split()[0])
        self.assertTrue(written[-1].endswith('next pass\\nsecond line'))
        self.assertFalse((self.root / 'events').exists())

    def test_rotation_is_bounded_even_for_one_oversized_outcome(self):
        with patch.object(tick, 'LANE_LOG_BYTES', 512):
            tick._log_lane_driver(['first L-spec-1417'], self.snapshot)
            tick._log_lane_driver(['🛠' * 10000], self.snapshot)
            tick._log_lane_driver(['last L-spec-1455'], self.snapshot)
        paths = sorted((self.root / 'logs').iterdir())
        self.assertEqual([p.name for p in paths], ['lane-driver.log', 'lane-driver.log.1'])
        self.assertTrue(all(p.stat().st_size <= 512 for p in paths))
        self.assertTrue(paths[1].read_text().endswith('\n'))
        self.assertIn('last L-spec-1455', paths[0].read_text())

    def test_logging_follows_a_rebound_root(self):
        tick._log_lane_driver(['first'], self.snapshot)
        other = self.root / 'other'
        with patch.object(fold, 'ROOT', other):
            tick._log_lane_driver(['second'], self.snapshot)
        self.assertNotIn('second', (self.root / 'logs/lane-driver.log').read_text())
        self.assertIn('second', (other / 'logs/lane-driver.log').read_text())


if __name__ == '__main__':
    unittest.main()
