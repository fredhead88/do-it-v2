"""The ledger projection is pure and pinned to the disposable cache format."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import ledger_compact
import ledger_cache


def projection_pin_matches():
    return (ledger_cache.VERSION == 3 and
            hashlib.sha256(json.dumps(ledger_compact.PROJECTION, sort_keys=True).encode()).hexdigest()
            == '9f02677ac73a9b85e375c9ec3381449ea217cfc00e9840d3e23edf9ddb93c1b9')


class CompactTests(unittest.TestCase):
    def test_compact_projection_and_purity(self):
        cases = [
            ({'type': 'tree-reaped', 'retained': ['a', 'b'], 'retained_reason': ['a: x', 'b: y']},
             {'type': 'tree-reaped', 'retained_count': 2}),
            ({'type': 'tree-reaped', 'retained': ['a'], 'retained_count': 7},
             {'type': 'tree-reaped', 'retained_count': 7}),
            ({'type': 'tree-reaped', 'retained_count': 3}, {'type': 'tree-reaped', 'retained_count': 3}),
            ({'type': 'tree-reaped', 'retained': 'bad', 'retained_reason': 4}, {'type': 'tree-reaped'}),
            ({'type': 'tick', 'recorded_reap': ['big'], 'lane': 'ready', 'v': 2},
             {'type': 'tick', 'lane': 'ready', 'v': 2}),
            ({'type': 'build-started', 'detail': ['kept']}, {'type': 'build-started', 'detail': ['kept']}),
            ({'type': ['unhashable'], 'retained': [1]}, {'type': ['unhashable'], 'retained': [1]}),
            ({'type': None}, {'type': None}),
        ]
        identity = dict(actor='stored-é', _src='unchanged:9', ts='2026-01-01', subject='L-charter-01')
        for event, expected in cases:
            event.update(identity)
            expected.update(identity)
            before = copy.deepcopy(event)
            with self.subTest(event=event):
                self.assertEqual(ledger_compact.compact(event), expected)
                self.assertEqual(event, before)

    def test_projection_pinned_to_cache_version(self):
        self.assertEqual(ledger_cache.VERSION, 3)
        self.assertEqual(ledger_compact.PROJECTION, {
            'tree-reaped': ('retained', 'retained_reason'), 'tick': ('recorded_reap',)})
        self.assertTrue(projection_pin_matches())
        changed = {**ledger_compact.PROJECTION,
                   'tree-reaped': (*ledger_compact.PROJECTION['tree-reaped'], 'extra')}
        with patch.object(ledger_compact, 'PROJECTION', changed):
            self.assertFalse(projection_pin_matches())


if __name__ == '__main__':
    unittest.main()
