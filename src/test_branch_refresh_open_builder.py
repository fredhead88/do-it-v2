#!/usr/bin/env python3
"""Deterministic builder lifetime regressions; no provider or filesystem access."""
from datetime import datetime, timedelta, timezone
import unittest

import branch_refresh as br
from test_branch_refresh import Fixture


class TestOpenBuilder(unittest.TestCase):
    def test_lifetime_table(self):
        now = datetime.now(timezone.utc)
        sid = 'L-spec-9774'
        def row(kind, **fields):
            return dict(type=kind, subject=sid, ts=now.isoformat(), **fields)
        start = row('spawn-started', spawn='L-builder-1')
        cases = [
            ('numeric-int', [row('autodispatched', role='builder', spawn=123)], False),
            ('numeric-string', [row('autodispatched', role='builder', spawn='123')], False),
            ('named-marker', [row('autodispatched', role='builder', spawn='L-builder-1')], False),
            ('named-start', [start], True),
            ('build-inference', [row('build-started')], True),
            ('explicit-role', [row('spawn-started', role='builder', spawn='custom')], True),
            ('other-role', [row('spawn-started', role='reviewer', spawn='L-reviewer-1')], False),
            ('other-subject', [dict(start, subject='L-spec-9999')], False),
            ('exact-expiry', [dict(start, ts=(now-timedelta(minutes=180)).isoformat())], True),
            ('past-expiry', [dict(start, ts=(now-timedelta(minutes=180, seconds=1)).isoformat())], False),
            ('explicit-window', [dict(start, window_min=2, ts=(now-timedelta(minutes=92)).isoformat())], True),
            ('past-window', [dict(start, window_min=2, ts=(now-timedelta(minutes=92, seconds=1)).isoformat())], False),
        ]
        for kind in ('spawn-done', 'spawn-failed', 'spawn-stale', 'build-done', 'build-blocked'):
            cases.append((kind, [start, row(kind, spawn='L-builder-1')], False))
        for kind in ('build-done', 'build-blocked'):
            cases.append((kind+'-subject', [start, row(kind)], False))
        for name, events, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(br.open_builder(events, sid, now=now), expected)
        self.assertTrue(br.open_builder([start], sid))
        self.assertFalse(br.open_builder([start, row('build-done')], sid))
        Fixture.signed(self, 'AC1', rows=[(name, expected) for name, _, expected in cases],
                       compatibility=True, inclusive_expiry=True)


if __name__ == '__main__':
    unittest.main()
