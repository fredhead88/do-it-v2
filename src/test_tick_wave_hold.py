"""Scratch-ledger regressions: builder and reviewer starts hold on wave order,
spec shape and duplicate or in-progress dispatch before any packet exists.

The process boundary is intercepted; no paid call and no production write."""
import os
import tempfile

# Bind the scratch root before any product module is imported.
_BOOT = tempfile.TemporaryDirectory(prefix='wave-hold-')
os.environ['DOIT_ROOT'] = _BOOT.name
os.environ['DOIT_NO_POKE'] = '1'
os.environ.pop('DOIT_PROJECT', None)

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import dispatch
import fold
import fold_stalls
import lane_driver
import lane_rules
import nextstep
import spawns
import tick

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
SID, SIBLING, CHARTER = 'L-spec-9901', 'L-spec-9900', 'L-charter-9001'
PLAN = """# plan
## early
Wave: 1
Footprint:
- src/early.py

## late
Wave: 2
Footprint:
- src/late.py
"""


class Boundary:
    """Records any attempt to start work; a held Step must record none."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *a, **k):
        self.calls.append(argv)
        raise AssertionError(f'process boundary reached: {argv}')


class WaveHoldBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wave-hold-case-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'events').mkdir()
        (self.root / 'state').mkdir()
        (self.root / 'content').mkdir()
        (self.root / 'content' / f'plan-{CHARTER}.md').write_text(PLAN)
        self.scope = patch.dict(os.environ, DOIT_ROOT=str(self.root))
        self.scope.start()
        self.addCleanup(self.scope.stop)
        for p in (patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', self.root / 'events'),
                  patch.object(dispatch, 'CONTENT', self.root / 'content'), patch.object(fold, 'NOW', NOW)):
            p.start()
            self.addCleanup(p.stop)
        self.boundary = Boundary()
        for target in ('run', 'Popen', 'call', 'check_call', 'check_output'):
            p = patch(f'subprocess.{target}', self.boundary)
            p.start()
            self.addCleanup(p.stop)
        self.rules = lane_rules.load()

    def emit(self, kind, actor='operator', seconds=0, subject=SID, **fields):
        with patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=seconds)).isoformat()):
            error = dispatch.emit(self.root / 'events' / f'L-{actor}-local.jsonl',
                                  dict(subject=subject, project='demo'), kind, **fields)
        self.assertIsNone(error)

    def spec(self, subject, wave_file, seconds=-5000):
        self.emit('spec-written', 'spec-writer', seconds=seconds, subject=subject, charter=CHARTER,
                  footprint=[wave_file], path=str(self.root / 'content' / (subject + '.md')))
        self.emit('spawn-done', 'spec-auditor', seconds=seconds + 10, subject=subject, bad_cut=False)

    def snapshot(self):
        events = fold.read_events(use_cache=False)
        return events, fold.fold(events)[0]

    def step(self, subject=SID, waves=None, now=NOW):
        events, specs = self.snapshot()
        return nextstep.step_for(events, specs, subject, now, self.rules, waves)

    def seed_wave(self):
        self.spec(SIBLING, 'src/early.py')
        self.spec(SID, 'src/late.py', seconds=-4000)

    def built(self):
        """SID has a finished build and a confirmed verdict: a review candidate."""
        self.spec(SID, 'src/late.py', seconds=-4000)
        self.emit('build-done', 'builder', seconds=-3000, status='DONE', ready_sha='a' * 40)
        self.emit('verdict', 'grader', seconds=-2000, confirmed=True)

    def assertHeld(self, step, reason):
        self.assertEqual((step.state, step.action, step.owner, step.reason),
                         ('hold', None, 'tick', 'not-ready:' + reason), step)
        self.assertTrue(step.clears)


class TestAC1BlockedAndReleased(WaveHoldBase):
    def test_ac1_wave_order_first_build_and_release(self):
        self.seed_wave()
        spec_written = [e for e in fold.read_events(use_cache=False)
                        if e['subject'] == SID and e['type'] == 'spec-written'][0]
        held = self.step()
        self.assertHeld(held, 'wave-order')
        self.assertIn('arlier-wave', held.clears)
        self.assertEqual(held.item, SID)
        self.assertEqual(held.since, spec_written['ts'])
        self.emit('shipped', 'integrator', seconds=-1000, subject=SIBLING)
        released = self.step()
        self.assertEqual((released.state, released.action), ('action', 'build'), released)

    def test_ac1_spec_shape_blocks_until_new_spec_written(self):
        self.spec(SID, 'src/late.py', seconds=-4000)
        self.emit('spec-shape-failed', 'spec-writer', seconds=-3000, findings=['x'])
        held = self.step()
        self.assertHeld(held, 'spec-shape')
        self.assertIn('spec-written', held.clears)
        self.emit('spec-written', 'spec-writer', seconds=-2000, charter=CHARTER, footprint=['src/late.py'],
                  path=str(self.root / 'content' / (SID + '.md')))
        self.emit('spawn-done', 'spec-auditor', seconds=-1900, bad_cut=False)
        self.assertEqual(self.step().action, 'build')

    def test_ac1_wave_order_on_rebuild_and_waiver_precedence(self):
        self.seed_wave()
        self.emit('build-started', 'builder', seconds=-3500, spawn='L-builder-9001', role='builder')
        self.emit('build-blocked', 'builder', seconds=-3400, spawn='L-builder-9001', reason='hard')
        self.emit('spawn-done', 'builder', seconds=-3390, spawn='L-builder-9001')
        self.emit('decision', 'thinker', seconds=-3000, rework='yes', ref='x')
        step = self.step()
        # A rework of an unbuilt spec is still a first build for wave order.
        self.assertEqual(step.reason, 'not-ready:wave-order', step)
        self.emit('decision', 'operator', seconds=-2000, wave_waive='yes')
        self.assertNotIn('wave-order', self.step().reason)

    def test_ac1_duplicate_and_in_progress_for_builder_and_reviewer(self):
        for label, build in (('builder', self.spec_only), ('reviewer', self.built)):
            with self.subTest(role=label):
                self.setUp()
                build()
                expected = 'build' if label == 'builder' else 'review'
                self.assertEqual(self.step().action, expected)
                with patch.object(spawns, 'lock_held', return_value=True):
                    self.assertHeld(self.step(), 'dispatch-in-progress')
                with patch.object(spawns, 'conflict', return_value='open ' + label):
                    self.assertHeld(self.step(), 'duplicate-dispatch')

    def spec_only(self):
        self.spec(SID, 'src/late.py', seconds=-4000)

    def test_ac1_dependency_hold_keeps_precedence(self):
        self.seed_wave()
        self.emit('dependency-hold', 'planner', seconds=-3500, waits_for='L-spec-9800', why='fixture')
        step = self.step()
        self.assertFalse(step.reason.startswith('not-ready:'), step)
        self.assertRegex(step.reason, 'held|dependency|blocker|undetermined')

    def test_ac1_base_refresh_recovery_and_fabricated_step(self):
        self.spec(SID, 'src/late.py', seconds=-4000)
        self.emit('spawn-failed', 'tick', seconds=-3000, spawn='L-builder-9003', role='builder',
                  reason='stale-base', exit_code=1)
        step = self.step()
        self.assertEqual((step.action, step.reason[:13]), ('build', 'base-refresh:'), step)
        with patch.object(spawns, 'lock_held', return_value=True):
            self.assertHeld(self.step(), 'dispatch-in-progress')
        fake = replace(step, reason='base-refresh: forged', trigger='forged')
        runner = Boundary()
        events, specs = self.snapshot()
        runner.events, runner.specs, runner.now = events, specs, NOW
        self.assertIsNone(tick.launch(fake, dispatcher=runner))
        self.assertEqual(runner.calls, [])


class TestAC2WaveRow(WaveHoldBase):
    def limits(self):
        return self.rules

    def test_ac2_step_row_maps_wave_hold_and_keeps_others(self):
        base = dict(item=SID, item_kind='spec', stage='planned', state='hold', action=None,
                    owner='tick', trigger='t', clears='c', since=NOW.isoformat())
        for reason, rule in (('not-ready:wave-order', 'wave-held'), ('wave: earlier', 'wave-held'),
                             ('held: dependency', 'dependency-held'),
                             ('undetermined: x', 'undetermined'),
                             ('not-ready:spec-shape', 'idle'), ('not-ready:duplicate-dispatch', 'idle'),
                             ('idle: awaiting lane condition', 'idle')):
            with self.subTest(reason=reason):
                row = fold_stalls.step_row(nextstep.Step(**base, reason=reason), self.rules, NOW)
                self.assertEqual(row['rule'], rule)
                if rule == 'wave-held':
                    self.assertEqual(row['limit_min'], self.rules['wave-held']['limit_min'])

    def test_ac2_one_hold_when_legacy_and_shared_wave_both_apply(self):
        self.seed_wave()
        waves = {SIBLING: 1, SID: 2}
        events, specs = self.snapshot()
        steps = [s for s in nextstep.next_steps(events, specs, NOW, self.rules, waves) if s.item == SID]
        self.assertEqual(len(steps), 1)
        self.assertHeld(steps[0], 'wave-order')
        # The legacy hold's since (the blocking sibling's trigger) is preserved.
        self.assertEqual(steps[0].since, (NOW + timedelta(seconds=-5000)).isoformat())
        self.assertEqual(fold_stalls.step_row(steps[0], self.rules, NOW)['rule'], 'wave-held')
        # Rebuild candidate: blocked before any build-done, reworked while the sibling is open.
        self.emit('build-started', 'builder', seconds=-3500, spawn='L-builder-9001', role='builder')
        self.emit('build-blocked', 'builder', seconds=-3400, spawn='L-builder-9001', reason='hard')
        self.emit('spawn-done', 'builder', seconds=-3390, spawn='L-builder-9001')
        self.emit('decision', 'thinker', seconds=-3000, rework='yes', ref='x')
        events, specs = self.snapshot()
        for supplied in (waves, None):
            steps = [s for s in nextstep.next_steps(events, specs, NOW, self.rules, supplied) if s.item == SID]
            self.assertEqual(len(steps), 1, steps)
            self.assertHeld(steps[0], 'wave-order')
            self.assertTrue(steps[0].trigger and steps[0].since)
            self.assertEqual(fold_stalls.step_row(steps[0], self.rules, NOW)['rule'], 'wave-held')


class TestAC3EntryPoints(WaveHoldBase):
    def run_all(self, subject_reason):
        events, specs = self.snapshot()
        out = tick.run_steps(events, specs, NOW, dispatcher=self.boundary)
        self.assertIn(f'hold {SID}: not-ready:{subject_reason}', out)
        out = lane_driver.run(events, specs, NOW, runner=self.boundary)
        self.assertIn(f'hold {SID}: not-ready:{subject_reason}', out)
        # Direct launch of a stale action Step is re-checked against a fresh read.
        action = replace(self.step_ignoring_hold(), state='action', action='build', reason='condition met',
                         clears=None)
        self.boundary.events, self.boundary.specs, self.boundary.now = events, specs, NOW
        self.assertIsNone(tick.launch(action, dispatcher=self.boundary))
        self.assertEqual(tick.HOLD_REASONS.get(SID), f'not-ready:{subject_reason}')
        self.assertEqual(self.boundary.calls, [])
        self.assertEqual(self.step().reason, f'not-ready:{subject_reason}')
        self.assertFalse(list((self.root / 'worktrees').glob('*')) if (self.root / 'worktrees').exists() else [])

    def step_ignoring_hold(self):
        return self.step()

    def test_ac3_wave_order_held_at_every_entry_point(self):
        self.seed_wave()
        self.run_all('wave-order')

    def test_ac3_spec_shape_held_at_every_entry_point(self):
        self.spec(SID, 'src/late.py', seconds=-4000)
        self.emit('spec-shape-failed', 'spec-writer', seconds=-3000, findings=['x'])
        self.run_all('spec-shape')


if __name__ == '__main__':
    unittest.main()
