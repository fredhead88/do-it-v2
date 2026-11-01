#!/usr/bin/env python3
"""A poisoned item cannot interrupt healthy neighbors or flood escalations."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_IMPORT = tempfile.TemporaryDirectory(prefix='item-isolation-import-')
os.environ.update(DOIT_ROOT=_IMPORT.name, DOIT_NO_POKE='1')
import autodispatch
import dispatch
import fold
import nextstep
import tick

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
IDS = ['L-spec-9001', 'L-spec-9002', 'L-spec-9003']


class ItemIsolationTests(unittest.TestCase):
    def test_unwritable_counter_cannot_abort_healthy_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='item-state-') as tmp:
            root = Path(tmp)
            (root / 'events').mkdir()
            (root / 'state').write_text('occupied')
            steps = [nextstep.Step(sid, 'spec', 'checking', 'action', 'grade', 'tick',
                                  None, 'ready', None, NOW.isoformat()) for sid in IDS]
            launched = []
            def launch(step, **kwargs):
                if step.item == IDS[0]:
                    raise RuntimeError('private payload')
                launched.append(step.item)
                return 'L-grader-9001'
            with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'), \
                 patch.object(tick, 'launch', side_effect=launch), patch('loadgate.begin', return_value=None):
                outcomes = tick.run_steps([], {}, NOW, steps=steps)
                self.assertEqual(launched, IDS[1:])
                self.assertTrue(any('item accounting' in line for line in outcomes))
                events = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
                self.assertEqual([e['subject'] for e in events if e['type'] == 'autodispatch-failed'], [IDS[0]])

    def test_bad_wave_preparation_cannot_abort_snapshot(self):
        rows = [dict(type='spec-written', subject=sid, actor='operator',
                     ts=NOW.isoformat(), project='fixture', charter='L-charter-9001',
                     _src='L-operator-fixture.jsonl:' + sid.rsplit('-', 1)[-1],
                     footprint=['poison' if sid == IDS[0] else sid + '.py']) for sid in IDS]
        specs = {e['subject']: dict(state='written', evs=[e]) for e in rows}
        def wave(charter, footprint):
            if footprint == ['poison']:
                raise RuntimeError('private payload')
            return None
        with patch.object(dispatch, 'spec_wave', side_effect=wave):
            steps = {s.item: s for s in tick._steps(rows, specs, NOW)}
        self.assertIn('evaluation failed: RuntimeError', steps[IDS[0]].reason)
        self.assertEqual([steps[sid].action for sid in IDS[1:]], ['build', 'build'])

    def test_exception_in_hold_observer_or_launch_preserves_two_healthy_items(self):
        for phase in ('hold', 'launch', 'planning', 'retry'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix='item-isolation-') as tmp:
                root = Path(tmp)
                (root / 'events').mkdir()
                specs = {sid: dict(state='written') for sid in IDS}
                rows = [dict(type='spec-written', subject=sid, actor='operator',
                             ts=NOW.isoformat(), project='fixture') for sid in IDS]
                steps = [nextstep.Step(sid, 'spec', 'checking', 'action', 'grade', 'tick',
                                      None, 'ready', None, NOW.isoformat()) for sid in IDS]
                if phase == 'planning':
                    steps[0] = nextstep._spec_exception(IDS[0], rows[:1], RuntimeError('private payload'))
                launches = []
                def launch(step, **kwargs):
                    if step.item == IDS[0] and phase == 'launch':
                        raise RuntimeError('private payload')
                    if step.item == IDS[0] and phase == 'retry':
                        from worktree_heal import RepairRetry
                        raise RepairRetry('already counted; retry next tick')
                    launches.append(step.item)
                    return 'L-grader-9001'
                def held(evs, *args, **kwargs):
                    if evs and evs[0]['subject'] == IDS[0] and phase == 'hold':
                        raise RuntimeError('private payload')
                    return None
                with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'), \
                     patch.object(dispatch, 'ROOT', root), patch.object(dispatch, 'CONTENT', root / 'content'), \
                     patch.object(dispatch, 'now', return_value=NOW.isoformat()), \
                     patch.object(tick, 'launch', side_effect=launch), \
                     patch.object(autodispatch, '_automatic_hold', side_effect=held), \
                     patch('loadgate.begin', return_value=None):
                    for n in range(4):
                        launches.clear()
                        tick.run_steps(rows, specs, NOW + timedelta(minutes=5*n), steps=steps)
                        self.assertEqual(launches, IDS[1:])
                    events = ([json.loads(line) for line in tick.tick_path().read_text().splitlines()]
                              if tick.tick_path().exists() else [])
                    failures = [e for e in events if e['type'] == 'autodispatch-failed']
                    self.assertEqual(len(failures), 0 if phase == 'retry' else 4)
                    self.assertTrue(all(e['subject'] == IDS[0] for e in failures))
                    self.assertTrue(all('RuntimeError' in e['reason'] for e in failures))
                    self.assertFalse(any('private payload' in json.dumps(e) for e in events))
                    escalations = [e for e in events if e['type'] == 'escalation-blocking']
                    self.assertEqual(len(escalations), 0 if phase == 'retry' else 1)
                    if phase != 'retry':
                        self.assertEqual((escalations[0]['subject'], escalations[0]['count']), (IDS[0], 3))
                    # A healthy pass resets the consecutive streak and permits one new alarm.
                    tick.run_steps(rows[1:], {sid: specs[sid] for sid in IDS[1:]}, NOW + timedelta(minutes=25), steps=steps[1:])
                    for n in range(3):
                        tick.run_steps(rows, specs, NOW + timedelta(minutes=30+5*n), steps=steps)
                    events = ([json.loads(line) for line in tick.tick_path().read_text().splitlines()]
                              if tick.tick_path().exists() else [])
                    self.assertEqual(len([e for e in events if e['type'] == 'escalation-blocking']), 0 if phase == 'retry' else 2)


if __name__ == '__main__':
    unittest.main()
