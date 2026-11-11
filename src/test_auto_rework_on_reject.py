"""Seeded auto-rework evidence; no ledger, network or provider access."""
from copy import deepcopy
import hashlib
import json
import unittest
from types import SimpleNamespace

import attempts
import dispatch
import lane_rules
import nextstep
import packet
from test_nextstep import event, NOW, SID


def seeded():
    return [event('spec-written', -100), event('build-done', -80, status='DONE'),
            event('verdict', -60, actor='grader', spawn='L-grader-9001', confirmed=False),
            event('rejected-criterion', -60, actor='grader', spawn='L-grader-9001',
                  criterion='AC1', why='First reason: keep punctuation, and detail.'),
            event('rejected-criterion', -60, actor='grader', spawn='L-grader-9001',
                  criterion='AC3', why='Second reason — verbatim evidence required.')]


def selected(rows, **kwargs):
    return nextstep.step_for(rows, {SID: dict(state='written')}, SID, NOW,
                             lane_rules.load(), **kwargs)


def signed(criterion, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://auto-rework/'+criterion, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=body)))


class AutoReworkTests(unittest.TestCase):
    def trigger(self, rows, caps=None):
        import auto_rework
        return auto_rework.trigger(rows, caps or [])

    def test_ac1_action_packet_and_current_build(self):
        rows = seeded()
        original = deepcopy(rows)
        step = selected(rows)
        self.assertEqual(step.action, 'rebuild')
        self.assertTrue(step.reason.startswith('auto-rework: '))
        for criterion in ('AC1', 'AC3'):
            self.assertIn(criterion, step.reason)
        self.assertEqual(step.trigger, rows[2]['_src'])
        rejected, _ = packet.Ctx.standing(SimpleNamespace(
            evs=rows, all_of=lambda kind: [e for e in rows if e['type'] == kind]))
        for rejection in rows[3:]:
            self.assertIn(rejection['criterion']+': '+rejection['why'], rejected)
        self.assertEqual(rows, original)
        for kind in ('build-started', 'build-done', 'spec-written', 'decision'):
            with self.subTest(newer=kind):
                newer = rows + [event(kind, -50, actor='thinker')]
                self.assertIsNone(self.trigger(newer))
                self.assertFalse(selected(newer).reason.startswith('auto-rework: '))
        signed('AC1', dict(action=step.action, trigger=step.trigger,
                          reason=step.reason, packet=rejected, newer_events_checked=4))

    def test_ac2_vetoes(self):
        rows = seeded()
        for field, value in (('confirmed', True), ('missing_capability', ['browser'])):
            candidate = deepcopy(rows)
            candidate[2][field] = value
            self.assertIsNone(self.trigger(candidate))
            self.assertNotEqual(selected(candidate).action, 'rebuild')
        cap = event('capability-hold', -40, subject='capability:browser:'+SID)
        self.assertIsNone(self.trigger(rows, [[cap]]))
        self.assertNotEqual(selected(rows+[cap]).action, 'rebuild')
        release = event('unblocked', -30, subject=cap['subject'], actor='thinker')
        self.assertEqual(self.trigger(rows, [[cap, release]]), rows[2])
        other = dict(cap, subject='capability:browser:L-spec-9002')
        self.assertEqual(selected(rows+[other]).action, 'rebuild')
        vetoes = [event('decision', -40, actor='thinker', close='superseded'),
                  event('question', -40, actor='builder', asks='Which outcome?'),
                  event('dependency-hold', -40, waits_for='L-spec-9002'),
                  event('decision', -40, actor='thinker', regrade='yes'),
                  event('decision', -40, actor='operator', action='hold', why='Wait')]
        for veto in vetoes:
            with self.subTest(veto=veto):
                self.assertNotEqual(selected(rows+[veto]).action, 'rebuild')
        signed('AC2', dict(confirmed=True, missing_capability=True, open_hold=True,
                          released_hold=True, unrelated_hold=True, vetoes=len(vetoes)))

    def test_ac3_repeat_and_clear(self):
        rows = seeded()
        prior = event('rejected-criterion', -70, actor='grader', spawn='L-grader-9000',
                      criterion='AC1', why='Earlier rejection')
        repeated = [prior]+rows
        self.assertIsNone(self.trigger(repeated))
        self.assertEqual(dispatch.grading_budget(repeated, SID), 'repeat-rejection:AC1')
        mixed = deepcopy(repeated)
        mixed[-1]['criterion'] = 'AC2'
        self.assertIsNone(self.trigger(mixed))
        cleared = [prior, event('criterion-cleared', -65, criterion='AC1')]+rows
        self.assertEqual(self.trigger(cleared), rows[2])
        self.assertIsNone(dispatch.grading_budget(cleared, SID))
        self.assertIsNone(self.trigger(rows+[event('criterion-cleared', -50, criterion='AC1'),
                                            event('criterion-cleared', -50, criterion='AC3')]))
        signed('AC3', dict(repeated='repeat-rejection:AC1', mixed_suppressed=True,
                          fresh_after_clear=True, all_cleared=True))

    def test_ac4_base_red_budget_and_human_trigger(self):
        rows = seeded()
        rows[2]['pre_existing'] = ['tests/a.py::t1', 'tests/b.py::t2']
        rows[3]['why'] = 'Failure tests/a.py::t1'
        rows[4]['why'] = 'Failure tests/b.py::t2,'
        self.assertIsNone(self.trigger(rows))
        for why in ('Failure tests/c.py::t3', 'No node ids here',
                    'tests/a.py::t1; tests/c.py::t3.)'):
            candidate = deepcopy(rows)
            candidate[4]['why'] = why
            self.assertEqual(self.trigger(candidate), candidate[2])
        rows = seeded()
        exhausted = rows + [event('spawn-failed', -50+n, role='builder',
                                 spawn=f'L-builder-{9000+n}', reason='work failed')
                            for n in range(attempts.MAX_ATTEMPTS)]
        step = selected(exhausted)
        self.assertEqual(step.state, 'hold')
        self.assertTrue(step.reason.startswith('retries-exhausted: builder'))
        decision = event('decision', -10, actor='thinker', rework='yes')
        human = selected(exhausted+[decision])
        self.assertEqual(human.action, 'rebuild')
        self.assertEqual(human.trigger, decision['_src'])
        retry = selected(rows+[event('spawn-failed', -1, role='builder',
                                    spawn='L-builder-9090', reason='work failed')])
        self.assertEqual(retry.state, 'hold')
        self.assertTrue(retry.reason.startswith('retry-wait:'))
        signed('AC4', dict(base_red_suppressed=True, mixed_new=True, no_node_new=True,
                          budget_reason=step.reason, human_trigger=human.trigger,
                          retry_reason=retry.reason))

    def test_pairing_newest_actor_and_timestamp(self):
        rows = seeded()
        for change in (dict(spawn='L-grader-9000'), dict(actor='reviewer'),
                       dict(ts=event('x', -61)['ts'])):
            candidate = deepcopy(rows)
            for rejection in candidate[3:]:
                rejection.update(change)
            self.assertIsNone(self.trigger(candidate))
        legacy = [{k: v for k, v in e.items() if k != 'spawn'} for e in rows]
        self.assertEqual(self.trigger(legacy), legacy[2])
        newest = rows+[event('verdict', -30, actor='grader', confirmed=True)]
        self.assertIsNone(self.trigger(newest))
        self.assertEqual(self.trigger(list(reversed(rows))), rows[2])


if __name__ == '__main__':
    unittest.main()
