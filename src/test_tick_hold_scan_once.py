#!/usr/bin/env python3
"""Scratch-only scan counts and dependency-hold equivalence (L-spec-2456)."""
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import autodispatch as ad
import dispatch
import fold
import lane_driver
import nextstep
import tick

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
DEP = 'L-spec-9900'


def event(kind, subject='L-spec-9001', minute=0, **fields):
    return dict(type=kind, subject=subject, actor='thinker',
                ts=(NOW + timedelta(minutes=minute)).isoformat(), **fields)


def signed(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://tick-hold-scan-once/' + ac, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=payload)))


class CountedEvents(list):
    """Count exhausted full-list iterations by their immediate consumer."""
    def __init__(self, rows):
        super().__init__(rows)
        self.full = Counter()

    def __iter__(self):
        caller = sys._getframe(1).f_code.co_name
        rows = super().__iter__()

        def counted():
            yield from rows
            self.full[caller] += 1

        return counted()


class HoldScanTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='hold-scan-')))
        (self.root / 'content').mkdir()
        (self.root / 'events').mkdir()
        stack.enter_context(patch.dict(os.environ, HOME=str(self.root), DOIT_ROOT=str(self.root),
                                      DOIT_SCRATCH=str(self.root), XDG_CONFIG_HOME=str(self.root)))
        for module, name, value in ((fold, 'ROOT', self.root),
                                    (fold, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'ROOT', self.root),
                                    (dispatch, 'CONTENT', self.root / 'content')):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(fold, 'read_events', side_effect=AssertionError('live read')))

    def ledger(self, shipped=False):
        subjects = [f'L-spec-{10000 + n}' for n in range(300)]
        rows = [event('dependency-hold', sid, waits_for=DEP, why='fixture') for sid in subjects]
        if shipped:
            rows.append(event('shipped', DEP))
        rows.extend(event('fixture-noise', 'L-fixture-9001') for _ in range(50000 - len(rows)))
        steps = [nextstep.Step(sid, 'spec', 'planned', 'action', 'build', 'tick',
                               None, 'ready', None, NOW.isoformat()) for sid in subjects]
        return CountedEvents(rows), {sid: {'state': 'written'} for sid in subjects}, steps

    def assert_scans(self, events, grouping):
        scans = events.full[grouping] + events.full['_unresolved_holds']
        self.assertLessEqual(scans, 1, dict(events.full))
        self.assertEqual(events.full['held_by_dependency'], 0, 'full-list copy per hold')
        return dict(events=len(events), subjects=300, shipped_scans=scans,
                    full_iterations=dict(events.full))

    def test_run_steps_held_scan_once(self):
        events, specs, steps = self.ledger()
        with patch.object(lane_driver, '_in_flight_role', return_value=0), \
             patch.object(tick, 'priority_items', return_value={}):
            result = tick.run_steps(events, specs, NOW, steps=steps, dry_run=True)
        self.assertEqual(len(result), 300)
        self.assertTrue(all(line.startswith('hold ') and DEP in line for line in result))
        signed('AC1-run-steps', self.assert_scans(events, 'run_steps'))

    def test_run_steps_launch_reuses_shipped_set(self):
        events, specs, steps = self.ledger(shipped=True)
        reporter = SimpleNamespace(failed={}, fail=Mock(), finish=Mock())
        import bundle_wait
        import dispatch_isolation
        import empty_grade
        import loadgate
        with patch.object(lane_driver, '_in_flight_role', return_value=0), \
             patch.object(ad, 'capacity', return_value=300), \
             patch.object(loadgate, 'begin', return_value=None), \
             patch.object(dispatch_isolation, 'Pass', return_value=reporter), \
             patch.object(empty_grade, 'complete', return_value=False), \
             patch.object(bundle_wait, 'run'), \
             patch.object(tick, '_record_retry_exhaustion', return_value=None), \
             patch.object(tick, 'priority_items', return_value={}), \
             patch.object(ad, '_standing_autodispatch_failed', return_value={'type': 'autodispatch-failed'}) as stopped:
            result = tick.run_steps(events, specs, NOW, steps=steps,
                                    dispatcher=Mock(side_effect=AssertionError('transport')))
        self.assertEqual(stopped.call_count, 300, 'all 300 Steps reached the real launch hold check')
        self.assertEqual(result, [f'hold {step.item}: standing dispatch failure' for step in steps])
        reporter.fail.assert_not_called()
        signed('AC1-launch', self.assert_scans(events, 'run_steps'))

    def test_rework_waits_scan_once(self):
        events, _, steps = self.ledger()
        candidates = [dict(subject=s.item, trigger=s.item) for s in steps]
        with patch.object(tick, 'rework_waits', return_value=candidates):
            result = tick._record_rework_waits(events, steps, NOW, 300)
        self.assertEqual(result, dict(eligible=300, held=300,
                                     held_reasons={'automatic-hold': 300}, observed=0))
        signed('AC1-rework-waits', self.assert_scans(events, '_record_rework_waits'))

    def test_standalone_launch_without_a_stored_shipped_set(self):
        rows = [event('dependency-hold', waits_for=DEP, why='fixture')]
        transport = Mock()
        transport.events, transport.dry_run = rows, True
        step = nextstep.Step(rows[0]['subject'], 'spec', 'planned', 'action', 'build',
                             'tick', None, 'ready', None, rows[0]['ts'])
        self.assertIsNone(tick.launch(step, dispatcher=transport))
        transport.assert_not_called()

    def test_hold_equivalence_and_fresh_snapshots(self):
        sid = 'L-spec-9001'
        hold = event('dependency-hold', waits_for=DEP, why='fixture')
        cases = {
            'shipped': ([hold], [hold, event('shipped', DEP)], None, []),
            'unshipped': ([hold], [hold], f'held: waits for {DEP} (fixture)', [DEP]),
            'killed': ([hold], [hold, event('spec-killed', DEP)],
                       f'held: waits for killed {DEP} (fixture)', [DEP]),
            'invalid': ([dict(hold, waits_for=7)], [hold],
                        'held: invalid waits_for; unresolvable dependency-hold', []),
            'self': ([dict(hold, waits_for=sid)], [hold],
                     f'held: unresolvable dependency-hold (waits_for is self: {sid})', []),
            'released': ([hold, event('unblocked', minute=1)], [hold], None, []),
        }
        text_sid = 'L-spec-9003'
        text = [event('spec-written', text_sid)]
        (self.root / 'content' / (text_sid + '.md')).write_text(f'Dependency-hold: waits_for="{DEP}"\n')
        cases['text'] = (text, text,
                         f'held: waits for {DEP} (Dependency-hold declared in spec text) [spec-text:{text_sid}]', [DEP])
        observed = {}
        for name, (own, rows, reason, dependencies) in cases.items():
            with self.subTest(name=name):
                shipped = {e.get('subject') for e in rows if e.get('type') == 'shipped'}
                for reader, expected in ((ad._automatic_hold, reason),
                                         (ad.held_by_dependency, reason),
                                         (ad.open_dependencies, dependencies)):
                    self.assertEqual(reader(own, rows), expected)
                    self.assertEqual(reader(own, rows, shipped=shipped), expected)
                    self.assertEqual(reader(own, iter(rows)), expected)
                observed[name] = dict(reason=reason, dependencies=dependencies)
        rows = [hold]
        self.assertIsNotNone(ad.held_by_dependency([hold], rows))
        rows.append(event('shipped', DEP))
        self.assertIsNone(ad.held_by_dependency([hold], rows))
        self.assertIsNotNone(ad.held_by_dependency([hold], [hold]))
        self.assertIsNotNone(ad.held_by_dependency([hold], rows, shipped=set()))
        signed('AC2', observed)


if __name__ == '__main__':
    unittest.main()
