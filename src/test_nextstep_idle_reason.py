#!/usr/bin/env python3
"""The fall-through hold names its last event, what clears it and the owner."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import fold
import ledger_cache
import lane_rules
import nextstep as ns

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SID = 'L-spec-9301'


def ev(kind, minutes, **extra):
    return dict(type=kind, ts=(NOW + timedelta(minutes=minutes)).isoformat(), subject=SID,
                _src=f'L-operator-fixture.jsonl:{minutes + 20000}', actor='operator') | extra


def hold(events):
    specs = {SID: dict(state='written')}
    with patch.object(fold, 'read_events', side_effect=AssertionError('read')), \
            patch.object(ledger_cache, 'read', side_effect=AssertionError('read'), create=True):
        steps = ns.next_steps(events, specs, NOW, lane_rules.load(), {})
    return next(s for s in steps if s.item == SID)


class IdleReason(unittest.TestCase):
    def test_ac1_unconfirmed_verdict(self):
        evs = [ev('spec-written', -60), ev('build-done', -50), ev('verdict', -40, confirmed=False),
               ev('rejected-criterion', -39), ev('rejected-criterion', -38)]
        r, clears, owner = ns._idle_reason(evs)
        self.assertTrue(r.startswith('after verdict not confirmed (2 rejected criteria)'), r)
        self.assertIn('rework=yes', clears)
        self.assertEqual(owner, 'thinker')
        s = hold(evs)
        self.assertEqual((s.state, s.owner), ('hold', 'thinker'))
        self.assertTrue(s.reason.startswith('after verdict not confirmed (2 rejected criteria)'), s.reason)
        self.assertIn('rework=yes', s.clears)

    def test_ac2_blocked_and_unknown(self):
        why = 'x' * 200
        r, _, owner = ns._idle_reason([ev('spec-written', -60), ev('build-blocked', -50, why=why)])
        self.assertTrue(r.startswith('after build-blocked:'))
        self.assertIn('x' * 120, r)
        self.assertNotIn('x' * 121, r)
        self.assertEqual(owner, 'thinker')
        self.assertIn('no reason recorded', ns._idle_reason([ev('build-blocked', -5)])[0])
        for evs in ([ev('spec-written', -60), ev('mystery-event', -50)], []):
            r, _, owner = ns._idle_reason(evs)
            self.assertTrue(r.startswith('unknown: no rule matched after '))
            self.assertEqual(owner, 'thinker')
            self.assertNotIn('awaiting lane condition', r)
        self.assertTrue(ns._idle_reason([])[0].endswith('after none'))

    def test_ac3_reads_only_given_events(self):
        hold([ev('spec-written', -60), ev('mystery-event', -50)])
        self.assertTrue(ns._idle_reason([ev('mystery-event', -1)])[0])


if __name__ == '__main__':
    unittest.main()
