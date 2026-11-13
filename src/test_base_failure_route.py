#!/usr/bin/env python3
"""Base-only handoffs: scratch repositories, real evidence and durable ledgers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import timedelta

from test_handin_guard import Repos, card, SID, NOW, SRC, REPO
import autodispatch
import bundle_wait
import dispatch
import fold
import grader_packet
import lane_rules
import nextstep
import tick


class Fixture(Repos):
    def setUp(self):
        super().setUp()
        self.content = self.root / 'content'
        self.content.mkdir()
        patch.object(dispatch, 'CONTENT', self.content).start()
        patch.object(dispatch, 'ROOT', self.root).start()
        (self.content / (SID + '.md')).write_text('Project: do-it-v2.\n## Acceptance criteria\nAC1 [backend]: fixture.\n')
        self.args = SimpleNamespace(subject=SID, cwd=str(self.repo), pinned_base_sha=self.main,
                                    pinned_base_sha_explicit=True)
        self.base = dict(subject=SID, project='do-it-v2', spawn='L-builder-0001')
        self.evs = [self.event('spec-written', actor='spec-writer', seconds=-100,
                               path=str(self.content / (SID + '.md'))),
                    self.event('spawn-done', actor='spec-auditor', seconds=-90, bad_cut=False)]
        self.admit()

    def event(self, kind, *, actor='builder', seconds=-10, **kv):
        self.serial = getattr(self, 'serial', 0) + 1
        return dict(type=kind, actor=actor, subject=SID, project='do-it-v2',
                    spawn='L-builder-0001', ts=(NOW + timedelta(seconds=seconds)).isoformat(),
                    _src=f'L-{actor}-fixture.jsonl:{self.serial}', **kv)

    def out(self, **fields):
        return dict(card('BLOCKED', self.head(), self.main), base_only=True, cited=['fixture failure'],
                    declarations=[dict(term='worked', line='base status unknown')], **fields)

    def admit(self, out=None):
        self.output = out or self.out()
        self.rows = dispatch.events_for('builder', self.output, self.args, self.base, events=self.evs)
        self.evs += [self.event(kind, **fields) for kind, fields in self.rows]
        return self.rows

    def publish(self, *, sha=None, n_new=0, seconds=-1, main=None, criteria=None):
        directory = self.content / 'evidence' / SID
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ('bundle-' + str(len(self.evs)) + '.json')
        data = dict(spec=SID, sha=sha or self.head(), base_sha=self.main,
                    master_sha=self.main if main is None else main,
                    local_master_sha=self.main if main is None else main,
                    commands=[], comparison=dict(new=[] if n_new == 0 else ['new']))
        path.write_text(json.dumps(data))
        event = self.event('evidence-bundle', actor='build-runner', seconds=seconds,
                           sha=data['sha'], path=str(path), n_new=n_new, criteria=criteria,
                           bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.evs.append(event)
        return event

    def step(self, evs=None, *, mains=True):
        events = self.evs if evs is None else evs
        return nextstep.step_for(events, fold.fold(events)[0], SID, NOW, lane_rules.load(),
                                 mains={'do-it-v2': self.main} if mains else {})

    def trigger(self):
        return autodispatch._grader_trigger(self.evs, main_sha=self.main)

    def question(self, asks='base status unknown', **fields):
        event = self.event('question', seconds=-9, asks=asks, blocks=[SID],
                           default='hold', deadline=(NOW + timedelta(days=1)).isoformat())
        event.update(fields)
        self.evs.append(event)
        return event

    def persist(self, rows=None):
        for index, event in enumerate(self.evs if rows is None else rows):
            fields = {k: v for k, v in event.items() if k not in
                      {'actor', '_src', 'type', 'ts', 'subject', 'project', 'spawn'}}
            with patch.object(dispatch, 'now', return_value=event['ts']):
                error = dispatch.emit(self.root / 'events' / ('L-' + event['actor'] + '-fixture.jsonl'),
                    dict(subject=SID, project='do-it-v2', spawn=event.get('spawn')), event['type'], **fields)
            self.assertIsNone(error)

class TestCardEvents(Fixture):
    def test_optional_types_bounds_and_legacy(self):
        import jsonschema
        schema = json.loads((REPO / 'agents/builder.schema.json').read_text())
        legacy = card('DONE', self.head(), self.main)
        jsonschema.validate(legacy, schema)
        valid = dict(legacy, base_only=True, cited=['x' * 200] * 40, notes=['x' * 400] * 20)
        jsonschema.validate(valid, schema)
        for fields in (dict(base_only='true'), dict(base_only=1), dict(cited='id'),
                       dict(cited=['']), dict(cited=[1]), dict(cited=['x'] * 41),
                       dict(cited=['x' * 201]), dict(notes='note'), dict(notes=[1]),
                       dict(notes=['x'] * 21), dict(notes=['x' * 401])):
            with self.subTest(fields=next(iter(fields))):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(dict(legacy, **fields), schema)

    def test_notes_and_claim_payloads(self):
        rows = self.admit(dict(card('DONE', self.head(), self.main), notes=['comparison: no HEAD-only failures']))
        self.assertEqual(next(v for k, v in rows if k == 'build-done')['notes'],
                         ['comparison: no HEAD-only failures'])
        self.assertNotIn('build-blocked', [k for k, v in rows])
        rows = self.admit()
        block = next(v for k, v in rows if k == 'build-blocked')
        self.assertEqual((block['ready_sha'], block['base_only'], block['cited']),
                         (self.head(), True, ['fixture failure']))
        self.args._handin = 'secret-scan'
        block = next(v for k, v in self.admit() if k == 'build-blocked')
        self.assertEqual(block['reason'], 'handin-check:secret-scan')
        self.assertIsNot(block.get('base_only'), True)

    def test_builder_decision_contract(self):
        text = (REPO / 'agents/builder.md').read_text()
        for needle in ('DONE+notes', 'BLOCKED+base_only', 'HEAD-only', 'inconclusive scratch',
                       'one clean commit', 'doit handin-check <subject> --cwd <worktree>'):
            self.assertIn(needle, text)


class TestPendingRoute(Fixture):
    def test_stable_hold_and_question_scope(self):
        self.question()
        before = copy.deepcopy(self.evs)
        first, second = self.step(), self.step()
        self.assertEqual(first, second)
        self.assertEqual((first.state, first.owner, first.reason),
                         ('hold', 'tick', 'not-ready:bundle-pending'))
        self.assertIn(self.head(), first.clears)
        self.assertIn('key=', first.clears)
        self.assertEqual(self.evs, before)
        self.assertEqual(self.trigger(), (None, None))
        self.publish()
        self.assertEqual(self.step().action, 'grade')
        for fields in (dict(asks='different asks'), dict(spawn='L-builder-0002'),
                       dict(actor='operator'), dict(blocks=['L-spec-9999'])):
            with self.subTest(fields=fields):
                self.evs = copy.deepcopy(before)
                self.evs[-1].update(fields)
                self.assertIn('open question', self.step().reason)
                self.publish()
                self.assertNotEqual(self.step().action, 'grade')

    def test_stale_main_requests_current_evidence(self):
        self.publish(main='c' * 40)
        self.persist()
        events = fold.read_events(use_cache=False)
        with patch.object(bundle_wait, 'project_main', return_value=self.main) as main_read:
            step = next(s for s in tick._steps(events, fold.fold(events)[0], NOW) if s.item == SID)
        main_read.assert_called_once()
        self.assertEqual(step.reason, 'not-ready:bundle-pending')
        with patch.object(tick.ledger_cache, 'tail_refresh', return_value=events), \
             patch.object(bundle_wait, 'project_main', side_effect=AssertionError('extra main read')), \
             patch.object(tick.subprocess, 'Popen') as runner:
            bundle_wait.run(step, events, NOW, root=self.root,
                            dst=self.root / 'events/L-tick-fixture.jsonl')
        self.assertEqual(runner.call_count, 1,
                         'Current-main pending hold must request a fresh comparison, not reuse stale-main evidence')

    def test_existing_tick_request_seam(self):
        self.question()
        self.persist()
        events = fold.read_events(use_cache=False)
        step = self.step(events)
        # Fake only the process boundary and liveness: no local or paid seat starts.
        child = SimpleNamespace(pid=os.getpid())
        with patch.object(tick.ledger_cache, 'tail_refresh', side_effect=lambda: fold.read_events(use_cache=False)), \
             patch.object(tick.subprocess, 'Popen', return_value=child) as runner, \
             patch.object(tick, '_checker_live', return_value=True):
            bundle_wait.run(step, events, NOW, root=self.root, dst=self.root / 'events/L-tick-fixture.jsonl')
            bundle_wait.run(step, events, NOW, root=self.root, dst=self.root / 'events/L-tick-fixture.jsonl')
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], ['doit', 'run-checks', SID, '--sha', self.head()])
        fresh = fold.read_events(use_cache=False)
        self.assertFalse(any(e['type'] == 'spawn-started' and e.get('role') == 'grader' for e in fresh))


class TestProofRoute(Fixture):
    def test_positive_and_pending_matrix(self):
        original = copy.deepcopy(self.evs)
        for case in ('absent', 'wrong-sha', 'restricted', 'pre-build', 'amended', 'stale-main', 'missing-main'):
            with self.subTest(case=case):
                self.evs = copy.deepcopy(original)
                if case != 'absent':
                    self.publish(sha='c' * 40 if case == 'wrong-sha' else None,
                                 criteria=['AC1'] if case == 'restricted' else None,
                                 seconds=-20 if case == 'pre-build' else -2,
                                 main='c' * 40 if case == 'stale-main' else None)
                if case == 'amended':
                    self.evs.append(self.event('spec-amended', actor='operator', seconds=-1))
                trigger = autodispatch._grader_trigger(self.evs, main_sha=None if case == 'missing-main' else self.main)
                self.assertEqual(trigger, (None, None))
                self.assertEqual(self.step(mains=case != 'missing-main').reason, 'not-ready:bundle-pending')
        self.evs = original
        self.publish(sha=self.head()[:9])
        self.assertEqual(self.trigger()[1], 'base-only-proven')
        self.assertEqual(self.step().action, 'grade')
        self.assertEqual(autodispatch._grader_trigger(self.evs), (None, None))

    def test_negative_matrix_and_supersession(self):
        self.publish()
        original = copy.deepcopy(self.evs)
        for field, values in (('base_only', [False, 'true', 1, None]),
                              ('cited', [[], None, 'id', [''], [7], ['x'] * 41, ['x' * 201]]),
                              ('ready_sha', ['', 'bad', None])):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.evs = copy.deepcopy(original)
                    block = next(e for e in self.evs if e['type'] == 'build-blocked')
                    block[field] = value
                    self.assertEqual(self.trigger(), (None, None))
                    self.assertNotEqual(self.step().action, 'grade')
                    self.assertNotEqual(self.step().reason, 'not-ready:bundle-pending')
        for count in (1, None, False, '0'):
            self.evs = copy.deepcopy(original)
            self.evs[-1]['n_new'] = count
            self.assertEqual(self.trigger(), (None, None))
            self.assertNotEqual(self.step().reason, 'not-ready:bundle-pending')
        for kind in ('build-started', 'build-done', 'build-blocked'):
            self.evs = copy.deepcopy(original)
            self.evs.append(self.event(kind, seconds=0, status='BLOCKED'))
            self.assertEqual(self.trigger(), (None, None))
        self.evs = copy.deepcopy(original)
        self.evs.append(self.event('decision', actor='operator', seconds=0, regrade='yes'))
        self.assertEqual(self.trigger()[1], 'regrade')
        self.evs = copy.deepcopy(original)
        self.evs.append(self.event('evidence-bundle-failed', actor='build-runner', seconds=-1,
                                   sha=self.head(), reason='runner failed'))
        self.assertEqual(self.trigger(), (None, None))
        self.assertNotEqual(self.step().reason, 'not-ready:bundle-pending')

    def test_real_commit_shape_first_attempt(self):
        original = self.evs[:2]
        one = self.head()
        self.commit('second spec commit')
        two = self.head()
        self.git('checkout', '--orphan', 'unrelated')
        self.commit('unrelated')
        unrelated = self.head()
        self.git('checkout', 'work')
        for ready, good in ((self.main, False), (two, False), ('f' * 40, False),
                            (unrelated, False), (one, True)):
            with self.subTest(shape='one' if good else 'invalid'):
                self.evs = copy.deepcopy(original)
                out = self.out()
                out['identity']['ready_sha'] = ready
                self.admit(out)
                self.publish(sha=ready)
                shape = [e for e in self.evs if e.get('criterion') == 'COMMIT-SHAPE']
                self.assertEqual(shape[-1]['type'], 'criterion-cleared' if good else 'rejected-criterion')
                self.assertEqual(self.trigger()[1] == 'base-only-proven', good)
                if not good:
                    self.assertNotEqual(self.step().action, 'grade')
                    self.assertNotEqual(self.step().reason, 'not-ready:bundle-pending')
        self.evs.append(self.event('rejected-criterion', criterion='COMMIT-SHAPE', seconds=0, why='standing'))
        self.assertEqual(self.trigger(), (None, None))


class TestSafetyCompatibility(Fixture):
    def test_real_bundle_gate_and_pure_router(self):
        import grader_view
        # An older DONE must never supply the packet/view identity.
        self.evs.insert(2, self.event('build-done', seconds=-30, status='DONE',
                                     ready_sha=self.main, base_sha=self.main))
        self.publish()
        with patch.object(fold, 'read_events', side_effect=AssertionError('router ledger scan')):
            self.assertEqual(self.trigger()[1], 'base-only-proven')
            self.assertEqual(self.step().action, 'grade')
        event = self.evs[-1]
        digest = event['bundle_sha256']
        packet = 'bundle_sha256: `' + digest + '`'
        class Refused(Exception):
            pass
        def fail(why, reason=None):
            raise Refused(reason)
        bundle = grader_packet.gate(self.evs, SID, packet, lambda *a, **k: None, fail)
        self.assertEqual(bundle['bundle']['sha'], self.head())
        receipt, rule = self.trigger()
        self.assertEqual(rule, 'base-only-proven')
        (self.content / ('verify-' + SID + '-grader.sh')).write_text('#!/bin/sh\nexit 0\n')
        with patch.object(grader_view, 'CONTENT', self.content), \
             patch.object(grader_view, 'SEAT', self.root / 'seat'), \
             patch('grading_env.project_venv', return_value=None):
            view = grader_view.build(SID, self.repo, receipt['base_sha'],
                                     receipt['ready_sha'], 'L-grader-fixture')
        self.assertEqual(json.loads((view / 'view.json').read_text())['ready_sha'], self.head())
        self.assertTrue((view / 'tree/fixture2').is_file())
        (self.root / 'repos').mkdir()
        (self.root / 'repos/do-it-v2').symlink_to(self.repo, target_is_directory=True)
        context = SimpleNamespace(evs=self.evs, a=SimpleNamespace(subject=SID),
            last=lambda kind: next(e for e in reversed(self.evs) if e['type'] == kind),
            project=lambda: 'do-it-v2')
        items = grader_packet.packet_items(context, ['5. Checker: fixture'])
        self.assertIn(digest, '\n'.join(items))
        self.assertIn('fixture2', '\n'.join(items))
        for mode in ('tampered', 'missing'):
            if mode == 'tampered':
                Path(event['path']).write_text('{}')
            else:
                Path(event['path']).unlink()
            with self.assertRaises(Refused):
                grader_packet.gate(self.evs, SID, packet, lambda *a, **k: None, fail)

    def test_unchanged_regressions(self):
        names = ('autodispatch', 'autodispatch_ended_equal', 'autodispatch_fault_isolation',
                 'autodispatch_held', 'autodispatch_since_equal', 'dispatch', 'dispatch_cwd',
                 'dispatch_failure_detail', 'dispatch_item_isolation', 'dispatch_manual_gate',
                 'contracts', 'window_gate', 'build_readiness', 'coupled_tests', 'nextstep',
                 'nextstep_base_refresh')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DOIT_', 'GIT_', 'SUPABASE_DB_URL'))}
        env.update(DOIT_NO_POKE='1', DOIT_TEST_PROCESS='1')
        for name in names:
            with self.subTest(file='src/test_' + name + '.py'):
                result = subprocess.run([sys.executable, '-B', str(SRC / ('test_' + name + '.py'))],
                                        capture_output=True, text=True, timeout=180, env=env)
                self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])


class TestLedgerRoundTrip(Fixture):
    def test_production_append_and_independent_reader(self):
        self.question()
        self.persist()
        fresh = self.reader()
        block = next(e for e in fresh if e['type'] == 'build-blocked')
        self.assertEqual((block['ready_sha'], block['base_only'], block['cited']),
                         (self.head(), True, ['fixture failure']))
        self.assertEqual((block['actor'], block['subject'], block['spawn']),
                         ('builder', SID, self.base['spawn']))
        self.assertEqual(self.step(fresh).reason, 'not-ready:bundle-pending')
        self.publish()
        self.persist(self.evs[-1:])
        self.assertEqual(self.step(self.reader()).action, 'grade')
        # Separate durable ledger for DONE, through events_for and emit again.
        for path in (self.root / 'events').glob('*.jsonl'):
            path.unlink()
        self.evs = self.evs[:2]
        self.admit(dict(card('DONE', self.head(), self.main), notes=['complete comparison: zero new failures']))
        self.persist()
        done = next(e for e in self.reader() if e['type'] == 'build-done')
        self.assertEqual(done['notes'], ['complete comparison: zero new failures'])
        self.assertEqual(done['actor'], 'builder')


if __name__ == '__main__':
    program = unittest.main(exit=False)
    result = program.result
    body = json.dumps(dict(tests=result.testsRun, failures=len(result.failures),
                           errors=len(result.errors)), sort_keys=True)
    print(json.dumps(dict(url='scratch://base-failure-route/' + (sys.argv[1] if len(sys.argv) > 1 else 'all'),
                          status=200 if result.wasSuccessful() else 409,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    sys.exit(0 if result.wasSuccessful() else 1)
