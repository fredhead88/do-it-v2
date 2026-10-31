#!/usr/bin/env python3
"""Scratch-only hold/release and automatic dispatch regressions (L-spec-1863)."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch

import autodispatch as ad
import dispatch
import fold
import fold_emits
import lane_rules
import nextstep
import tick


NOW = dt.datetime(2026, 10, 29, 9, tzinfo=dt.timezone.utc)
SID = 'L-spec-9001'


def event(kind, minute=-10, subject=SID, actor='thinker', **fields):
    return dict(type=kind, subject=subject, actor=actor,
                ts=(NOW + dt.timedelta(minutes=minute)).isoformat(), **fields)


def signed(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://autodispatch-held/' + ac, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=payload)))


class HeldDispatch1863Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='held-1863-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('events', 'content', 'seat'):
            (self.root / name).mkdir()
        for module, fields in ((fold, ('ROOT', 'EVENTS')),
                               (dispatch, ('ROOT', 'EVENTS', 'CONTENT', 'SEAT'))):
            for name in fields:
                p = patch.object(module, name, self.root if name == 'ROOT' else self.root / name.lower())
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(fold, 'PROJECT', None)
        p.start()
        self.addCleanup(p.stop)
        self.rules = lane_rules.load()
        self.calls = []

    def specs(self, events):
        subjects = {e['subject'] for e in events if e['type'] == 'spec-written'}
        return {s: dict(id=s, state='written', evs=[e for e in events if e['subject'] == s])
                for s in subjects}

    def step(self, events, sid=SID):
        specs = self.specs(events)
        step = nextstep.step_for(events, specs, sid, NOW, self.rules)
        self.assertEqual(step, next(s for s in nextstep.next_steps(events, specs, NOW, self.rules)
                                    if s.item == sid))
        return step

    def written(self, sid=SID):
        return event('spec-written', -120, sid, actor='operator', project='fixture')

    def runner(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'dispatch']:
            return dict(code=0, stdout=json.dumps(dict(detached='L-builder-9009')), stderr='')
        return dict(code=0, stdout='/fixture.packet' if argv[:2] == ['doit', 'packet']
                    else '0' if 'rev-list' in argv else 'master', stderr='')

    def test_AC1(self):
        hold = event('dependency-hold', waits_for='L-spec-9002', _src='L-thinker-fixture.jsonl:1')
        reason = ad.held_by_dependency([hold], [hold])
        self.assertTrue(reason)
        cases = []
        for actor in ('thinker', 'operator', 'builder', 'tick', None):
            for kind in ('decision', 'unblocked'):
                for minute in (-11, -10, -9):
                    for ref in (None, '', 'elsewhere:1', hold['_src']):
                        release = event(kind, minute, actor=actor, ref=ref)
                        expected = actor in ('thinker', 'operator') and minute > -10 and (
                            kind == 'unblocked' or ref == hold['_src'])
                        for sample in ([hold, release], [release, hold]):
                            self.assertEqual(ad.held_by_dependency(sample, sample), None if expected else reason)
                        cases.append(dict(actor=actor, kind=kind, minute=minute, ref=ref, released=expected))
        for fields in ({}, dict(_src=None), dict(_src='')):
            missing = dict(hold)
            missing.pop('_src')
            missing.update(fields)
            self.assertIsNotNone(ad.held_by_dependency([missing, event('decision', -9)], []))
        for at in (-20, -5):
            shipped = event('shipped', at, 'L-spec-9002', actor='executor')
            self.assertIsNone(ad.held_by_dependency([hold], [hold, shipped]))
        self.assertIsNone(ad.held_by_dependency([hold], [], shipped={'L-spec-9002'}))
        self.assertIsNotNone(ad.held_by_dependency([hold], [event('shipped', -5, 'L-spec-9999')]))
        for invalid in (None, '', SID):
            bad = dict(hold, waits_for=invalid)
            self.assertIn('unresolvable', ad.held_by_dependency([bad], []))
        self.assertIsNone(ad.held_by_dependency([], []))
        other = event('decision', -9, 'L-spec-9999', ref=hold['_src'])
        self.assertEqual(ad.held_by_dependency([hold, other], []), reason)
        release = event('decision', -8, ref=hold['_src'])
        newer = dict(hold, ts=event('x', -7)['ts'], _src='L-thinker-fixture.jsonl:2')
        for sample in ([hold, release, newer], [newer, release, hold]):
            self.assertIn(newer['_src'], ad.held_by_dependency(sample, []))
        self.assertEqual(ad.held_by_dependency([hold, event('build-started', -5)], []), reason)
        signed('AC1', dict(release_matrix=cases, shipment_and_invalid_controls=True))

    def test_AC2(self):
        written = self.written()
        results = []
        for actor in ('thinker', 'operator', 'builder', 'tick', None):
            for action in ('hold', 'recut', None, 'close', 're-cut'):
                ruling = event('decision', actor=actor, action=action, waits_for='L-spec-9002',
                               _src=f'L-{actor or "unknown"}-fixture.jsonl:1', why='hold or re-cut prose')
                # Omitted actor and source cannot acquire authority.
                if actor is None:
                    ruling.pop('actor')
                    ruling.pop('_src')
                expected = actor in ('thinker', 'operator') and action in ('hold', 'recut')
                for build_minute in (None, -11, -10, -9):
                    sample = [written, ruling]
                    if build_minute is not None:
                        sample.append(event('build-started', build_minute, actor='builder'))
                    held = expected and (build_minute is None or build_minute < -10)
                    self.assertEqual(bool(ad._automatic_hold(sample, sample)), held)
                    if held:
                        step = self.step(sample)
                        self.assertEqual(step.state, 'hold')
                        self.assertIn(action, step.reason)
                        self.assertIn(ruling['_src'], step.reason)
                        self.assertNotIn('undetermined', step.reason)
                        self.assertNotIn('dependency', step.reason)
                        results.append(dict(step=asdict(step)))
                    results.append(dict(actor=actor, action=action, build_minute=build_minute, held=held))
                if not expected:
                    continue
                for release_actor in ('thinker', 'operator', 'builder', 'tick', None):
                    for kind in ('decision', 'unblocked'):
                        for minute in (-11, -10, -9):
                            for ref in (None, '', 'elsewhere:1', ruling['_src']):
                                release = event(kind, minute, actor=release_actor, ref=ref)
                                clears = (release_actor in ('thinker', 'operator') and minute > -10
                                          and (kind == 'unblocked' or ref == ruling['_src']))
                                sample = [written, ruling, release]
                                self.assertEqual(bool(ad._automatic_hold(sample, sample)), not clears)
                unrelated = event('decision', -5, subject='L-spec-9999', action='hold')
                self.assertIsNone(ad._automatic_hold([written], [written, unrelated]))
                tail = [event('decision', -9), event('spec-written', -8, actor='operator')]
                sample = [written, ruling, *tail]
                self.assertEqual(self.step(sample).state, 'hold')
                completion = event('unblocked', -7, actor='operator')
                self.assertIsNone(ad._automatic_hold(sample + [completion], sample + [completion]))
                self.assertFalse(self.step(sample + [completion]).reason.startswith('held: ruling'))
                rehold = dict(ruling, ts=event('x', -6)['ts'], _src=ruling['_src'] + '0')
                self.assertIn(rehold['_src'], self.step(sample + [completion, rehold]).reason)
                dep = event('dependency-hold', -11, waits_for='L-spec-9002', _src='L-thinker-fixture.jsonl:9')
                self.assertIn('waits for L-spec-9002', ad._automatic_hold([written, ruling, dep], []))
                self.assertIn('L-spec-9002', self.step([written, ruling, dep]).reason)
                # Releasing an older ruling never releases a newer one.
                old_release = event('decision', -5, ref=ruling['_src'])
                newer = dict(rehold, ts=event('x', -8)['ts'])
                for sample in ([written, ruling, newer, old_release],
                               [newer, old_release, ruling, written]):
                    self.assertIn(newer['_src'], ad._automatic_hold(sample, sample))
                    self.assertEqual(self.step(sample).state, 'hold')
        signed('AC2', dict(ruling_matrix=results, explicit_release_and_recut_completion=True))

    def replay_cases(self):
        # Source timestamps/refs are historical anchors; the four action fields
        # and fixture dependencies below are synthetic SD10 projections.
        anchors = [
            ('1731', '2026-10-27T22:34:05Z', 8819, None),
            ('1560', '2026-10-26T03:53:03Z', 8075, 'hold'),
            ('1730', '2026-10-28T04:05:16Z', 8973, 'hold'),
            ('1672', '2026-10-28T23:21:25Z', 9814, 'recut'),
            ('1768', '2026-10-28T22:57:20Z', 9807, 'recut')]
        for number, timestamp, line, action in anchors:
            sid = 'L-spec-' + number
            ruling = event('decision' if action else 'dependency-hold', subject=sid,
                           _src=f'L-thinker-0004.jsonl:{line}')
            ruling['ts'] = timestamp
            if action:
                ruling.update(action=action, synthetic='SD10 projection')
                if action == 'hold':
                    ruling['waits_for'] = 'L-spec-' + number
            else:
                ruling['waits_for'] = 'L-spec-1723'
            events = [dict(self.written(sid), ts='2026-10-25T00:00:00Z'), ruling]
            if number == '1731':
                events.append(event('decision', subject=sid, _src='L-thinker-0004.jsonl:8965'))
                events[-1]['ts'] = '2026-10-28T03:35:30Z'
            if number == '1560':
                events.append(event('decision', subject=sid, _src='L-thinker-0004.jsonl:8076',
                                    ref='L-spec-writer-1717.jsonl:3'))
                events[-1]['ts'] = '2026-10-26T03:53:10Z'
            if number in ('1730', '1672', '1768'):
                ruling['ref'] = {'1730': 'L-builder-4056.jsonl:4', '1672': 'L-builder-4398.jsonl:5',
                                 '1768': 'L-builder-4399.jsonl:5'}[number]
            yield sid, events

    def test_AC3(self):
        replayed = []
        for sid, events in self.replay_cases():
            specs = self.specs(events)
            self.assertEqual(self.step(events, sid).state, 'hold')
            self.assertTrue(ad._automatic_hold(events, events))
            stale = event('spawn-stale', -1, sid, actor='tick', role='builder', spawn='L-builder-fixture')
            for dry_run in (True, False):
                before = list(self.root.joinpath('events').glob('*'))
                self.calls.clear()
                with patch.object(ad, 'add_builder_worktree', side_effect=AssertionError('prepared held worktree')), \
                     patch('worktree_heal.repair', side_effect=AssertionError('repaired held worktree')):
                    tick.run_steps(events, specs, NOW, dispatcher=self.runner, dry_run=dry_run)
                    ad.run(events, specs, NOW, runner=self.runner, dry_run=dry_run)
                    ad.redispatch_dead(events + [stale], specs, NOW, runner=self.runner, dry_run=dry_run)
                    direct = tick._PassDispatcher(self.runner, events, specs, NOW, dry_run)
                    for action in ('build', 'rebuild'):
                        step = nextstep.Step(sid, 'spec', 'building', 'action', action, 'tick',
                                             'fixture:1', 'condition met', None, events[0]['ts'])
                        self.assertIsNone(tick.launch(step, dispatcher=direct))
                        tick.run_steps(events, specs, NOW, dispatcher=self.runner, dry_run=dry_run, steps=[step])
                self.assertEqual(self.calls, [])
                if dry_run:
                    self.assertEqual(before, list(self.root.joinpath('events').glob('*')))
            replayed.append(dict(subject=sid, events=events, builder_calls=0))
        # Holds are checked before seat accounting, even for supplied action Steps.
        sid, held = next(self.replay_cases())
        control = self.written('L-spec-9998')
        (self.root / 'worktrees/fixture/l-spec-9998').mkdir(parents=True)
        events = held + [control]
        steps = [nextstep.Step(s, 'spec', 'building', 'action', 'build', 'tick', 'fixture:1',
                              'condition met', None, '2026-10-25T00:00:00Z') for s in (sid, control['subject'])]
        with patch.object(ad, 'capacity', return_value=1), \
             patch('lane_driver._in_flight_role', return_value=0), \
             patch('loadgate.begin', return_value=None):
            self.calls.clear()
            out = tick.run_steps(events, self.specs(events), NOW, dispatcher=self.runner, steps=steps)
        launches = [a for a in self.calls if a[:2] == ['doit', 'dispatch']]
        self.assertEqual([(a[3], a[4]) for a in launches], [('builder', control['subject'])], out)
        signed('AC3', dict(replays=replayed, unheld_dispatches=launches))

    def fresh_read(self):
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(Path(__file__).parent.resolve()))
        env.pop('DOIT_PROJECT', None)
        script = ('import fold,json; events=fold.read_events(use_cache=False); '
                  'print(json.dumps([e for e in events if e["type"]=="autodispatch-skipped" '
                  'and fold.admits(e,e["actor"]) is None]))')
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_AC4(self):
        self.assertEqual(fold_emits.EMITS['autodispatch-skipped'], {'tick'})
        self.assertEqual(fold_emits.REQUIRED_ROWS['autodispatch-skipped'], ('subject', 'reason'))
        for missing in ('subject', 'reason'):
            row = dict(type='autodispatch-skipped', subject=SID, reason='fixture')
            row.pop(missing)
            self.assertIsNotNone(fold.required_reason(row))
        self.assertIsNotNone(fold.admits(dict(type='autodispatch-skipped'), 'builder'))
        written = self.written()
        a = event('decision', action='recut', _src='L-thinker-fixture.jsonl:1')
        b = event('dependency-hold', -9, waits_for='L-spec-9003', _src='L-thinker-fixture.jsonl:2')
        release_b = event('decision', -8, ref=b['_src'], _src='L-thinker-fixture.jsonl:3')
        (self.root / 'events/L-operator-fixture.jsonl').write_text(json.dumps(written) + '\n')
        thinker_path = self.root / 'events/L-thinker-fixture.jsonl'
        fixture = [written]
        counts = []
        for addition, expected in ((a, 1), (None, 1), (b, 2), (None, 2), (release_b, 3)):
            if addition is not None:
                with thinker_path.open('a') as stream:
                    stream.write(json.dumps(addition) + '\n')
                fixture.append(addition)
            # Each invocation owns a fresh interpreter and a real production read.
            script = '''import datetime,json,fold,tick
events = fold.read_events(use_cache=False)
specs = fold.fold(events)[0]
def forbidden(argv):
    raise AssertionError('held pass reached runner')
tick.run_steps(events,specs,datetime.datetime.fromisoformat(__import__('sys').argv[1]),dispatcher=forbidden)
'''
            env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(Path(__file__).parent.resolve()))
            env.pop('DOIT_PROJECT', None)
            subprocess.run([sys.executable, '-c', script, NOW.isoformat()],
                           env=env, check=True, capture_output=True, text=True)
            rows = self.fresh_read()
            self.assertEqual(len(rows), expected)
            self.assertTrue(all(e['actor'] == 'tick' and e['subject'] == SID and e['reason'] for e in rows))
            self.assertEqual(rows[-1]['reason'], ad._automatic_hold(fixture, fixture))
            counts.append(len(rows))
        # Another subject is independent; diagnostic and dry passes remain pure.
        other = 'L-spec-9999'
        events = [self.written(other), dict(a, subject=other)] + self.fresh_read()
        before = tick.tick_path().read_bytes()
        self.step(events, other)
        ad.candidates(events, self.specs(events), NOW)
        tick.run_steps(events, self.specs(events), NOW, dispatcher=self.runner, dry_run=True)
        self.assertEqual(tick.tick_path().read_bytes(), before)
        tick.run_steps(events, self.specs(events), NOW, dispatcher=self.runner)
        rows = self.fresh_read()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(e['subject'] == other for e in rows), 1)
        # Reusing a stale snapshot through compatibility/recovery cannot duplicate.
        ad.run(events, self.specs(events), NOW, runner=self.runner)
        ad.redispatch_dead(events, self.specs(events), NOW, runner=self.runner)
        self.assertEqual(len(self.fresh_read()), 4)
        self.assertEqual(self.calls, [])
        signed('AC4', dict(counts=counts, fresh_process_readback=rows, independent_subject=True))


if __name__ == '__main__':
    unittest.main()
