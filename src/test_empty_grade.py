"""Zero-row grading completes mechanically; delegated checks stay outstanding."""
import json
from datetime import timedelta
import shutil
import unittest
from unittest.mock import patch

import fold
import lane_rules
import nextstep
import owed
import routing
import tick
import test_lane_driver as fixtures


class EmptyGradeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DriverTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.sid = self.f.sid
        self.text = '## Acceptance criteria\nAC1 [backend]: one.\nAC2 [ui]: two.\n'
        self.path = self.f.root / 'content' / (self.sid + '.md')
        self.path.write_text(self.text)
        self.events = [self.f.event('spec-written', 40, project='proj', actor='spec-writer', path=str(self.path)),
                       self.f.event('build-done', 30, project='proj', actor='builder',
                                    status='DONE', ready_sha='a'*40),
                       self.f.event('criterion-routed', 25, actor='grader', criterion='AC1', to='owed'),
                       self.f.event('criterion-routed', 25, actor='grader', criterion='AC2', to='reviewer')]
        self.persist()

    def persist(self):
        groups = {}
        for event in self.events:
            groups.setdefault(event['actor'], []).append(
                {k: v for k, v in event.items() if k not in ('actor', '_src')})
        for actor, events in groups.items():
            (self.f.root / 'events' / ('L-' + actor + '-fixture.jsonl')).write_text(
                ''.join(json.dumps(e) + '\n' for e in events))

    def snapshot(self):
        events = fold.read_events(use_cache=False)
        return events, fold.fold(events)[0]

    def test_exhausted_empty_grade_advances_without_packet_seat_or_worktree(self):
        # The live 1933 history also has an earlier no-spend bundle wait.
        self.events.append(self.f.event('autodispatched', 24, actor='tick', role='grader',
                                        spawn='12345', since=self.events[1]['ts']))
        self.events.append(self.f.event('spawn-failed', 23, actor='grader',
                                        spawn='L-grader-9001', reason='bundle-pending'))
        for minutes in (20, 15, 10):
            self.events.append(self.f.event('autodispatch-failed', minutes, actor='tick',
                role='grader', rule='grade', reason='packet: empty grader criteria for ' + self.sid + ': all criteria routed or absent\n'))
        self.persist()
        shutil.rmtree(self.f.worktree)
        events, specs = self.snapshot()
        steps = nextstep.next_steps(events, specs, self.f.now, lane_rules.load())
        self.assertTrue(any(s.item == self.sid and 'exhausted:' in s.reason for s in steps))
        result = tick.run_steps(events, specs, self.f.now, dispatcher=self.f.runner, steps=steps)
        events, specs = self.snapshot()
        verdicts = [e for e in events if e['type'] == 'verdict']
        self.assertEqual(len(verdicts), 1, result)
        self.assertEqual(verdicts[0]['n'], 0)
        self.assertTrue(verdicts[0]['confirmed'])
        self.assertEqual(verdicts[0]['ready_sha'], 'a'*40)
        self.assertEqual(verdicts[0]['delegated_criteria'], {'AC1': 'owed', 'AC2': 'reviewer'})
        self.assertEqual(self.f.calls, [])
        self.assertFalse(any(e['type'] == 'spawn-started' for e in events))
        self.assertEqual(len([e for e in events if e['type'] == 'autodispatched']), 1)
        following = nextstep.next_steps(events, specs, self.f.now, lane_rules.load())
        self.assertTrue(any(s.item == self.sid and s.action == 'review' for s in following), following)
        self.assertFalse(routing.reviewer_gate_clear(events, self.sid, {'AC2'}))
        self.assertTrue(any(c['criterion'] == 'AC1' and c['status'] != 'met' for c in owed.checks(events, self.f.now)))
        tick.run_steps(events, specs, self.f.now, dispatcher=self.f.runner, steps=steps)
        self.assertEqual(len([e for e in self.snapshot()[0] if e['type'] == 'verdict']), 1)

    def test_fresh_and_dry_run_do_not_dispatch_or_consume_retry(self):
        events, specs = self.snapshot()
        steps = nextstep.next_steps(events, specs, self.f.now, lane_rules.load())
        result = tick.run_steps(events, specs, self.f.now, dispatcher=self.f.runner, steps=steps, dry_run=True)
        self.assertFalse(any(e['type'] == 'verdict' for e in self.snapshot()[0]))
        self.assertEqual(self.f.calls, [])
        self.assertTrue(any('zero grader criteria' in line for line in result), result)
        tick.run_steps(events, specs, self.f.now, dispatcher=self.f.runner, steps=steps)
        self.assertEqual(len([e for e in self.snapshot()[0] if e['type'] == 'verdict']), 1)
        self.assertEqual(self.f.calls, [])
        self.f.now += timedelta(minutes=5)
        self.events.append(self.f.event('build-done', 1, actor='builder', project='proj',
                                        status='DONE', ready_sha='b'*40))
        self.persist()
        events, specs = self.snapshot()
        steps = nextstep.next_steps(events, specs, self.f.now, lane_rules.load())
        tick.run_steps(events, specs, self.f.now, dispatcher=self.f.runner, steps=steps)
        verdicts = [e for e in self.snapshot()[0] if e['type'] == 'verdict']
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(verdicts[-1]['ready_sha'], 'b'*40)

    def test_malformed_partial_revoked_rejected_and_unrelated_failures_stay_closed(self):
        import empty_grade
        for variant in ('absent', 'partial', 'revoked', 'rejected', 'not-done', 'missing-sha', 'registered-path', 'unregistered'):
            with self.subTest(variant=variant):
                events = list(self.events)
                self.path.write_text(self.text)
                if variant == 'absent': self.path.write_text('## Acceptance criteria\nNothing defined.\n')
                if variant == 'partial': events = events[:-1]
                if variant == 'revoked': events.append(self.f.event('criterion-routed', 1, actor='grader', criterion='AC2', to='none'))
                if variant == 'rejected': events.append(self.f.event('rejected-criterion', 1, actor='grader', criterion='AC1'))
                if variant == 'not-done': events[1] = dict(events[1], status='NEEDS_CONTEXT')
                if variant == 'missing-sha': events[1] = dict(events[1], ready_sha=None)
                if variant == 'registered-path':
                    actual = self.path.with_name('registered.md')
                    actual.write_text(self.text + 'AC3 [backend]: still needs grading.\n')
                    events[0] = dict(events[0], path=str(actual))
                if variant == 'unregistered': events = events[1:]
                self.assertIsNone(empty_grade.proof(events, self.sid))
        self.path.write_text(self.text)
        step = self.f.step(action=None, state='hold', owner='executor', reason='exhausted: fatal: unrelated', clears='operator')
        self.assertFalse(empty_grade.complete(step, self.events, dry_run=True))
        automatic = dict(type='verdict', actor='grader', automatic=empty_grade.MARKER,
                         n=0, confirmed=True, delegated_criteria={'AC1': 'owed'})
        self.assertFalse(empty_grade.clears_failure(dict(reason='fatal: unrelated'), automatic))
        self.assertFalse(empty_grade.clears_failure(dict(type='autodispatch-failed', role='builder',
            reason='packet: empty grader criteria for ' + self.sid + ': all criteria routed or absent'), automatic))


if __name__ == '__main__':
    unittest.main()
