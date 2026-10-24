"""The legacy row adapter and Steps agree on the configured name registry."""
from datetime import datetime, timezone
import unittest
import lane_rules
import nextstep
import fold


class EvaluationTests(unittest.TestCase):
    def test_table_vs_steps_agreement(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        events = [dict(type='spec-written', subject='L-spec-9401', project='fixture',
                       ts='2026-10-23T10:00:00+00:00', actor='planner',
                       _src='L-planner-fixture.jsonl:1')]
        specs = fold.fold(events)[0]
        rules = lane_rules.load()
        steps = nextstep.next_steps(events, specs, now, rules)
        self.assertEqual([(s.item, s.action) for s in steps], [('L-spec-9401', 'build')])
        self.assertTrue(all(row['rule'] in lane_rules.NAMES
                            for row in lane_rules.due(events, specs, now)))
        self.assertTrue(all(fold.step_row(step, rules, now)['rule'] in lane_rules.NAMES
                            for step in steps))


if __name__ == '__main__':
    unittest.main()
