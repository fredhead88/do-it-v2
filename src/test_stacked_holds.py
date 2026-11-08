#!/usr/bin/env python3
"""Scratch-only regressions for the union of unresolved dependency holds."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='stacked-import-')
os.environ.update(DOIT_ROOT=_IMPORT_ROOT.name, DOIT_NO_POKE='1')
import autodispatch as ad
import dependency_holds
import dispatch
import fold
import lane_rules
import nextstep
import tick

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)
SID, X, Y = 'L-spec-9901', 'L-spec-9902', 'L-spec-9903'


def event(kind, minute=0, subject=SID, **fields):
    return dict(type=kind, subject=subject, actor='operator', project='fixture',
                ts=(NOW - timedelta(days=3) + timedelta(minutes=minute)).isoformat(),
                _src=f'L-operator-scratch.jsonl:{minute + 100}') | fields


def hold(dep, minute=1):
    return event('dependency-hold', minute, waits_for=dep, why='scratch prerequisite')


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='stacked-holds-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('content', 'events', 'seat'):
            (self.root / name).mkdir()
        for module, names in ((dispatch, ('ROOT', 'CONTENT', 'EVENTS', 'SEAT')),
                              (fold, ('ROOT', 'EVENTS', 'SESSIONS'))):
            for name in names:
                p = patch.object(module, name, self.root if name == 'ROOT' else self.root / name.lower())
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        p.start()
        self.addCleanup(p.stop)
        # Every reader must route through the scratch content root.
        spec_path = dispatch.spec_path
        def guarded(sid):
            path = spec_path(sid)
            self.assertTrue(path.is_relative_to(self.root / 'content'), path)
            return path
        p = patch.object(dispatch, 'spec_path', side_effect=guarded)
        p.start()
        self.addCleanup(p.stop)
        self.rules = lane_rules.load()
        self.written = event('spec-written', actor='planner')
        self.base = [self.written]
        self.path = self.root / 'content' / (SID + '.md')
        self.path.write_text('# scratch spec\n')

    def tearDown(self):
        body = json.dumps({'test': self.id(), 'failures': len(self._outcome.result.failures),
                           'errors': len(self._outcome.result.errors)}, sort_keys=True)
        print(json.dumps(dict(url='scratch://stacked-holds/' + self.id(),
                             status=int(bool(self._outcome.result.failures or self._outcome.result.errors)),
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def own(self, events):
        return [e for e in events if e['subject'] == SID]

    def dependencies(self, events, **kwargs):
        return ad.open_dependencies(self.own(events), events, **kwargs)

    def reason(self, events, **kwargs):
        return ad.held_by_dependency(self.own(events), events, **kwargs)


class LedgerStackTests(Scratch):
    def test_union_and_shipping(self):
        evs = self.base + [hold(X), hold(Y, 2), hold(X, 3)]
        for sample in (evs, list(reversed(evs))):
            self.assertEqual(set(self.dependencies(sample)), {X, Y})
            self.assertEqual(len(self.dependencies(sample)), 2)
            shipped_y = sample + [event('shipped', 4, Y)]
            self.assertEqual(self.dependencies(shipped_y), [X])
            self.assertIn(X, self.reason(shipped_y))
            self.assertEqual(self.dependencies(sample, shipped={Y}), [X])
            both = shipped_y + [event('shipped', 5, X)]
            self.assertEqual(self.dependencies(both), [])
            self.assertIsNone(self.reason(both))
            self.assertEqual(self.dependencies(sample, shipped={X, Y}), [])
            self.assertIsNone(self.reason(sample, shipped={X, Y}))
        early = [event('shipped', -1, X)] + self.base + [hold(X)]
        self.assertEqual(self.dependencies(early), [])


class HoldReleaseTests(Scratch):
    def test_release_authority_matrix(self):
        first, second = hold(X), hold(Y, 2)
        evs = self.base + [first, second]
        for actor in ('thinker', 'operator'):
            release = event('decision', 3, actor=actor, ref=second['_src'])
            self.assertEqual(self.dependencies(evs + [release]), [X])
            clear = event('unblocked', 3, actor=actor)
            self.assertEqual(self.dependencies(evs + [clear, hold(Y, 4)]), [Y])
        for fields in ({'ref': 'unrelated'}, {'subject': X}, {'actor': 'builder'},
                       {'actor': 'tick'}, {'ts': second['ts']}, {'ts': first['ts']}, {'ref': None}):
            release = event('decision', 3, ref=second['_src']) | fields
            self.assertEqual(set(self.dependencies(evs + [release])), {X, Y})
        for kind in ('spec-written', 'spec-amended', 'build-started'):
            self.assertEqual(set(self.dependencies(evs + [event(kind, 3)])), {X, Y})
        duplicate = hold(X, 2)
        evs = self.base + [first, duplicate, event('decision', 3, ref=duplicate['_src'])]
        self.assertEqual(self.dependencies(evs), [X])
        self.assertEqual(self.dependencies(evs + [event('decision', 4, ref=first['_src'])]), [])

    def test_killed_is_not_satisfied(self):
        evs = self.base + [hold(X), event('spec-killed', 2, X)]
        self.assertEqual(self.dependencies(evs), [X])
        self.assertIn('waits for killed ' + X, self.reason(evs))


class TextUnionTests(Scratch):
    def test_text_ledger_union_epochs_and_duplicates(self):
        self.path.write_text('Dependency-hold: waits_for=' + X)
        evs = self.base + [hold(Y, 2)]
        self.assertEqual(set(self.dependencies(evs)), {X, Y})
        self.assertEqual(self.dependencies(evs + [event('shipped', 3, Y)]), [X])
        self.assertIsNone(self.reason(evs + [event('shipped', 3, Y), event('shipped', 4, X)]))
        self.assertEqual(self.dependencies(self.base), [X])
        self.assertEqual(self.dependencies(self.base + [hold(X)]), [X])
        released = evs + [event('decision', 3, ref=evs[-1]['_src'])]
        self.assertEqual(self.dependencies(released), [X])
        self.assertIn(X, dependency_holds.text_hold_reason(self.own(released), released))
        release_text = event('decision', 4, ref='spec-text:' + SID)
        self.assertEqual(self.dependencies(released + [release_text]), [])
        revised = released + [release_text, event('spec-amended', 5)]
        self.assertEqual(self.dependencies(revised), [X])
        self.assertIsNone(dependency_holds.text_hold_reason(self.own(released + [release_text]), released))
        self.assertEqual(set(self.dependencies(evs + [event('spec-amended', 5)])), {X, Y})

    def test_fenced_examples(self):
        self.path.write_text('```\nDependency-hold: waits_for=' + X + '\n```\n')
        self.assertEqual(self.dependencies(self.base), [])


class ReaderParityTests(Scratch):
    def assert_readers(self, evs, source, timestamp):
        own = self.own(evs)
        specs = {SID: dict(id=SID, state='written', evs=own, age=3)}
        steps = nextstep.next_steps(evs, specs, NOW, self.rules)
        step = next(s for s in steps if s.item == SID)
        self.assertEqual(step, nextstep.step_for(evs, specs, SID, NOW, self.rules))
        self.assertIsNone(step.action, step)
        self.assertIn(X, step.reason)
        built = evs + [event('build-done', 4, status='DONE')]
        grading_specs = {SID: dict(specs[SID], state='graded', evs=self.own(built))}
        grade_step = nextstep.step_for(built, grading_specs, SID, NOW, self.rules)
        self.assertIsNone(grade_step.action, grade_step)
        self.assertIn(X, grade_step.reason)
        rows = fold.stall_rows(evs, specs, now=NOW, dwell={'written': 0})
        row = next(r for r in rows if r['spec'] == SID)
        self.assertEqual((row['bucket'], row['blocker'], row['src'], row['since']),
                         ('held', X, source, timestamp))
        lanes = lane_rules.due(evs, specs, NOW)
        lane = next(r for r in lanes if r['spec'] == SID and r['rule'] == 'dependency-held')
        self.assertEqual(lane['blocker'], X)
        self.assertFalse(lane['reason'].startswith('undetermined:'))
        self.assertIn(X, ad._automatic_hold(own, evs))
        self.assertIn(X, ad.held_by_dependency(own, evs))
        transport = Mock()
        transport.dry_run = False
        transport.events = evs
        for action in ('build',):
            ready = nextstep.Step(SID, 'spec', 'planned', 'action', action, 'tick',
                                  source, 'ready', None, own[0]['ts'])
            self.assertIsNone(tick.launch(ready, dispatcher=transport))
        transport.assert_not_called()
        stale = event('spawn-stale', 20, spawn='L-grader-9901', role='grader')
        stale['ts'] = (NOW - timedelta(minutes=1)).isoformat()
        recovery = evs + [event('build-done', 19, status='DONE'), stale]
        self.assertEqual(ad.dead_spawns(recovery, NOW), [])
        shipped = recovery + [event('shipped', 21, X), event('shipped', 22, Y)]
        if not any(e.get('waits_for') == SID or e.get('waits_for') in (None, '', 7, [])
                   for e in self.own(evs) if e['type'] == 'dependency-hold'):
            if not self.reason(shipped):
                self.assertEqual([r['role'] for r in ad.dead_spawns(shipped, NOW)], ['grader'])
        both = evs + [event('shipped', 10, X), event('shipped', 11, Y)]
        self.assertIsNone(ad.held_by_dependency(self.own(both), both))
        # Independent standing rulings retain their own veto after shipping.
        ruling = event('decision', 12, action='hold')
        self.assertIn('ruling hold', ad._automatic_hold(self.own(both) + [ruling], both + [ruling]))

    def test_ledger_and_text_provenance(self):
        first = hold(X)
        evs = self.base + [first, hold(Y, 2), event('shipped', 3, Y)]
        self.assert_readers(evs, first['_src'], first['ts'])
        self.path.write_text('Dependency-hold: waits_for=' + X)
        self.assert_readers(self.base, 'spec-text:' + SID, self.written['ts'])

    def test_killed_blocker(self):
        first = hold(X)
        evs = self.base + [first, event('spec-killed', 2, X)]
        self.assert_readers(evs, first['_src'], first['ts'])
        self.assertIn('waits for killed ' + X, self.reason(evs))
        step = nextstep.step_for(evs, {SID: dict(state='written')}, SID, NOW, self.rules)
        self.assertIn('waits for killed ' + X, step.reason)


class CompatibilityTests(Scratch):
    def assert_invalid(self, evs):
        reason = self.reason(evs)
        self.assertRegex(reason, 'invalid|unresolvable|unreadable')
        specs = {SID: dict(id=SID, state='written', evs=self.own(evs), age=3)}
        step = nextstep.step_for(evs, specs, SID, NOW, self.rules)
        self.assertIsNone(step.action, step)
        self.assertRegex(step.reason, 'invalid|unresolvable|unreadable')
        self.assertIn('release', step.clears)
        self.assertNotIn('newer dependency-hold', step.clears)
        stalled = next(r for r in fold.stall_rows(evs, specs, now=NOW, dwell={'written': 0})
                       if r['spec'] == SID)
        self.assertEqual(stalled['bucket'], 'STUCK')
        self.assertIsNone(stalled['blocker'])
        self.assertRegex(stalled['reason'], 'invalid|unresolvable|unreadable')
        transport = Mock()
        transport.dry_run = False
        transport.events = evs
        for action in ('build',):
            ready = nextstep.Step(SID, 'spec', 'planned', 'action', action, 'tick',
                                  None, 'ready', None, self.written['ts'])
            self.assertIsNone(tick.launch(ready, dispatcher=transport))
        transport.assert_not_called()
        stale = event('spawn-stale', 20, spawn='L-grader-9901', role='grader')
        stale['ts'] = (NOW - timedelta(minutes=1)).isoformat()
        recovery = evs + [event('build-done', 19, status='DONE'), stale]
        self.assertEqual(ad.dead_spawns(recovery, NOW), [])
        shipped = recovery + [event('shipped', 21, X), event('shipped', 22, Y)]
        if not any(e.get('waits_for') == SID or e.get('waits_for') in (None, '', 7, [])
                   for e in self.own(evs) if e['type'] == 'dependency-hold'):
            if not self.reason(shipped):
                self.assertEqual([r['role'] for r in ad.dead_spawns(shipped, NOW)], ['grader'])
        return step

    def test_older_invalid_precedes_valid_until_its_release(self):
        for value in (None, '', 7, [], SID):
            bad = hold(value)
            for missing in (False, True) if value is None else (False,):
                if missing:
                    bad.pop('waits_for', None)
                evs = self.base + [bad, hold(Y, 2)]
                with self.subTest(value=value, missing=missing):
                    self.assert_invalid(evs)
                    evs += [event('decision', 3, ref=bad['_src'])]
                    self.assertEqual(self.dependencies(evs), [Y])
                    specs = {SID: dict(state='written')}
                    step = nextstep.step_for(evs, specs, SID, NOW, self.rules)
                    self.assertIsNone(step.action)
                    self.assertIn(Y, step.reason)
                    self.assertNotRegex(step.reason, 'invalid|unresolvable')

    def test_invalid_and_unreadable_text(self):
        for text in ('Dependency-hold: waits_for=[]',
                     'Dependency-hold: waits_for=' + SID,
                     ('Dependency-hold: waits_for=' + X + '\n') * 2):
            self.path.write_text(text)
            self.assert_invalid(self.base + [hold(Y, 2)])
        self.path.unlink()
        self.path.mkdir()  # Deterministically unreadable as text, even as root.
        self.assert_invalid(self.base + [hold(Y, 2)])
        self.assert_invalid(self.base + [event('unblocked', 3)])


if __name__ == '__main__':
    unittest.main()
