#!/usr/bin/env python3
"""Hermetic queue regressions: real ledger admission, git gates and CLI writes."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Abort before importing any module that could append to the ledger.
_standalone_root = tempfile.TemporaryDirectory(prefix='merge-queue-root-')
import atexit
atexit.register(_standalone_root.cleanup)
os.environ['DOIT_ROOT'] = _standalone_root.name
_test_root = os.environ.get('DOIT_ROOT')
assert _test_root, 'DOIT_ROOT must be set'
assert Path(_test_root).resolve() != (Path.home() / '.do-it').resolve()
# Any DOIT_ROOT other than the live one is allowed: the build runner's is a scratch dir outside TMPDIR.

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autodispatch
import dispatch
import fold
import lane_rules
import tick
import merge_queue as mq

DOIT = str(Path(__file__).resolve().parent.parent / 'doit')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='merge-queue-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        self.now = datetime.now(timezone.utc)
        self.sid = 'L-spec-9001'
        for module, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'ROOT', self.root), (dispatch, 'CONTENT', self.root / 'content'),
                                    (dispatch, 'EVENTS', self.root / 'events'), (fold, 'NOW', self.now),
                                    (dispatch, 'now', lambda: self.now.isoformat())):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'DOIT_ROOT': str(self.root), 'DOIT_NO_POKE': '1',
                                   'DOIT_LEDGER_FILE': 'L-executor-0001.jsonl',
                                   'DOIT_REHEARSAL_LAUNCHER': 'off'})
        p.start()
        self.addCleanup(p.stop)
        self.events = []
        self.calls = []

    def event(self, kind, age=0, **fields):
        return dict(v=1, type=kind, subject=self.sid,
                    ts=(self.now - timedelta(minutes=age)).isoformat(), **fields)

    def landed(self):
        return [json.loads(s) for s in tick.tick_path().read_text().splitlines()] if tick.tick_path().exists() else []

    def mark(self, ac, *cases):
        body = '\n'.join(f'1138-AC{ac} {case}' for case in cases)
        print(body)
        print(json.dumps(dict(url=f'cli://doit-v2/merge-queue/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def nightly_mark(self, number):
        body = f'nightly-ac{number} ok'
        print(body)
        print(json.dumps(dict(url=f'cli://doit-v2/nightly/AC{number}', status=0,
              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def nightly_event(self, kind, minute, **fields):
        return dict(type=kind, subject='nightly:albert-scott', project='albert-scott',
                    ts=(self.now + timedelta(minutes=minute)).isoformat(),
                    actor='operator', _src=f'fixture:{minute}', **fields)

    def test_nightly_ac4_cumulative_freeze(self):
        e = self.nightly_event
        a = e('nightly-red', 0, ids=['A'])
        b = e('nightly-red', 2, ids=['B'])
        events = [a, e('base-red-nightly', 1, complete=True, ids=['A']), b]
        self.assertEqual(fold.nightly_unowned(events, 'albert-scott'), ['A', 'B'])
        for owned in (['A'], ['B']):
            owner = e('nightly-red-owned', 3, ids=owned, owner='L-spec-9001')
            self.assertIsNotNone(mq.merge_freeze(events + [owner], 'albert-scott'))
        owner = e('nightly-red-owned', 3, ids=['A', 'B'], owner='quarantine')
        self.assertIsNone(mq.merge_freeze(events + [owner], 'albert-scott'))
        for bad in (dict(owner, actor='builder'), dict(owner, owner='anyone'),
                    dict(owner, ts=a['ts']), dict(owner, subject='nightly:other'), dict(owner, project='other')):
            self.assertIsNotNone(mq.merge_freeze(events + [bad], 'albert-scott'))
        for verdict in (e('base-red-nightly', 4, complete=False, ids=[]),
                        e('base-red-nightly', 4, complete=True, ids=['C'])):
            self.assertIsNotNone(mq.merge_freeze(events + [verdict], 'albert-scott'))
        green = e('base-red-nightly', 4, complete=True, ids=[])
        self.assertIsNone(mq.merge_freeze(events + [green], 'albert-scott'))
        self.assertIsNotNone(mq.merge_freeze([dict(a, ids=None)], 'albert-scott'))
        self.assertIsNotNone(mq.merge_freeze(events + [owner, e('nightly-red', 5, ids=['A'])], 'albert-scott'))
        self.assertIsNone(mq.merge_freeze(events, 'other'))
        self.nightly_mark(4)

    def test_nightly_ac5_window_freeze(self):
        self.window_seed()
        red = self.nightly_event('nightly-red', 0, ids=['A'])
        self.seed([red], 'L-base-red-local.jsonl')
        for _ in range(2):
            with patch.object(mq, 'batching_allowed', side_effect=AssertionError('batch attempted')):
                self.assertEqual(mq.window_run('albert-scott', runner=self.fake), 3)
        self.assertEqual(self.calls, [])
        events = fold.read_events()
        self.assertFalse(any(e['type'] == 'queue-batch' for e in events))
        frozen = [e for e in events if e['type'] == 'merge-frozen']
        self.assertEqual(len(frozen), 1)
        self.assertIn('A', frozen[0]['reason'])
        for target, failure in ((mq, 'merge_freeze'), (mq.window_gate, 'train_events')):
            with patch.object(target, failure, side_effect=RuntimeError('unreadable')):
                self.assertEqual(mq.window_run('albert-scott', runner=self.fake), 3)
        self.assertEqual(self.calls, [])
        self.nightly_mark(5)

    def test_nightly_freeze_admits_only_automatic_quarantine_maintenance(self):
        self.window_seed()
        maintenance = 'quarantine-' + 'a'*16
        branch = 'auto-quarantine/' + 'a'*16
        row = dict(self.event('queue-add', project='albert-scott', branch=branch, rule='auto-quarantine'),
                   subject=maintenance)
        self.seed([self.nightly_event('nightly-red', 0, ids=['tests/test_clock.py::test_boundary']), row],
                  'L-base-red-local.jsonl')
        code, calls = self.train_call()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], maintenance)
        batch = [e for e in fold.read_events() if e['type'] == 'queue-batch'][-1]
        self.assertEqual(batch['specs'], maintenance)
        self.assertTrue(any(r['spec'] == self.sid for r in mq.queue(fold.read_events())))

    def test_nightly_ac12_escalation_closure(self):
        e = self.nightly_event
        escalation = e('escalation-blocking', 1, default='quarantine',
                       deadline=(self.now - timedelta(hours=1)).isoformat(), revert='remove quarantine')
        events = [e('nightly-red', 0, ids=['A', 'B']), escalation]
        def check(rows, closed):
            self.assertEqual(fold.open_escalations(rows), [] if closed else [escalation])
            self.assertEqual(escalation['_src'] in fold.answered(rows), closed)
            if closed:
                self.assertNotIn(escalation, fold.overdue_questions(rows))
        check(events, False)
        check(events + [e('decision', 2)], False)
        owner = e('nightly-red-owned', 2, ids=['A'], owner='L-spec-9001')
        check(events + [owner], False)
        check(events + [owner, e('nightly-red-owned', 3, ids=['B'], owner='quarantine')], True)
        check(events + [dict(owner, ids=['A', 'B'], actor='reviewer')], False)
        check(events + [e('base-red-nightly', 4, complete=True, ids=[])], True)
        self.nightly_mark(12)

    def test_queue_fold(self):
        add = self.event('queue-add', 5, project='p', branch='b', rule='operator')
        other = dict(add, subject='L-spec-9002')
        self.assertEqual(mq.queue([add])[0]['spec'], self.sid)
        self.assertEqual(mq.queue([add, self.event('queue-remove', 4, reason='operator')]), [])
        readd = dict(add, ts=self.event('x', 1)['ts'])
        self.assertEqual([r['spec'] for r in mq.queue([add, other, readd])], ['L-spec-9002', self.sid])
        self.assertEqual([r['spec'] for r in mq.queue([other, add])], ['L-spec-9002', self.sid])
        self.assertEqual(mq.queue([dict(add, project=None)], 'p'), [])
        batch = self.event('queue-batch', 3, project='p', specs=self.sid)
        batch['subject'] = 'window-test'
        self.assertEqual(mq.queue([add, batch])[0]['batch'], 'window-test')
        self.assertEqual(mq.queue([add, {k: v for k, v in readd.items() if k != 'branch'}]), [])
        self.mark(1, 'add ok', 'remove ok', 're-add ok', 'equal ts ok', 'project filter ok',
                  'batch field ok', 'missing branch ok')

    def redcut_fixture(self):
        """The retained-row contract, with unrelated work after the batch."""
        specs = ['L-spec-9001', 'L-spec-9002', 'L-spec-9003', 'L-spec-9004']
        adds = [dict(self.event('queue-add', 10-i, project='p', branch=branch,
                                rule='operator'), subject=sid)
                for i, (sid, branch) in enumerate(zip(specs, 'abcu'))]
        batch = dict(self.event('queue-batch', 5, project='p', specs=','.join(specs[:3])),
                     subject='window-redcut')
        drop = self.event('window-dropped', 4, window=batch['subject'], reason='owner',
                          evidence='local-repro', tests=['tests/test_a.py::test_a'],
                          owned_nodes=['tests/test_a.py::test_a'])
        close = dict(self.event('window-redcut', 3, window=batch['subject'], project='p',
                               candidate='candidate-fixture', base='base-fixture',
                               dropped=specs[:1], returned=specs[1:3], flakes=[], residual=[],
                               first_red_at=drop['ts'], closed_at=self.event('x', 3)['ts'],
                               outcome='handled-red', automatic=True), subject=batch['subject'])
        expected = [dict(spec=e['subject'], project='p', branch=e['branch'],
                         added_at=e['ts'], batch=None) for e in adds[1:]]
        return adds + [batch, drop], close, expected

    def test_redcut_retains_exact_queue_rows_and_position(self):
        events, close, expected = self.redcut_fixture()
        before = json.dumps(events + [close], sort_keys=True)
        self.assertEqual(mq.queue(events + [close], 'p'), expected)
        self.assertEqual(mq.queue(events + [close, close], 'p'), expected)
        self.assertEqual(json.dumps(events + [close], sort_keys=True), before)
        # A terminal row does not invent a queue entry or affect another project.
        self.assertEqual(mq.queue([close], 'p'), [])
        self.assertEqual(mq.queue(events + [dict(close, project='other')], 'p')[0]['batch'],
                         close['subject'])

    def test_redcut_replay_cannot_clear_successor_or_replace_new_branch(self):
        events, close, expected = self.redcut_fixture()
        successor = dict(self.event('queue-batch', 2, project='p',
                                    specs=','.join(close['returned'])), subject='window-next')
        replay = dict(close, ts=self.event('x', 1)['ts'])
        after_successor = [dict(row, batch='window-next') if row['spec'] in close['returned']
                           else row for row in expected]
        self.assertEqual(mq.queue(events + [close, successor, replay], 'p'), after_successor)
        new_add = dict(self.event('queue-add', 1, project='p', branch='b2', rule='operator'),
                       subject=close['returned'][0])
        expected_new = [expected[1], expected[2], dict(expected[0], branch='b2',
                                                     added_at=new_add['ts'])]
        self.assertEqual(mq.queue(events + [close, new_add, replay], 'p'), expected_new)
        # A fresh add also replaces a batch link without any preceding redcut.
        old_link = [events[0], events[1], events[4], new_add]
        self.assertIsNone(mq.queue(old_link, 'p')[-1]['batch'])

    def test_redcut_does_not_revive_terminal_or_removed_work(self):
        events, close, expected = self.redcut_fixture()
        for kind in ('shipped', 'spec-killed', 'spec-closed', 'window-dropped', 'queue-remove'):
            terminal = dict(self.event(kind, 2), subject=close['returned'][0])
            self.assertEqual(mq.queue(events + [close, terminal, close], 'p'), expected[1:])
        all_dropped = events + [dict(events[-1], subject=sid) for sid in close['returned']]
        self.assertEqual(mq.queue(all_dropped + [dict(close, returned=[])], 'p'), expected[-1:])
        # Flake-only closure returns the original rows without a fresh queue-add.
        no_drop = events[:-1]
        flake = dict(close, dropped=[], returned=[e['subject'] for e in events[:3]],
                     flakes=['tests/test_a.py::test_a'])
        rows = mq.queue(no_drop + [flake], 'p')
        self.assertEqual([r['branch'] for r in rows], list('abcu'))
        self.assertTrue(all(r['batch'] is None for r in rows))

    def test_redcut_production_append_and_fresh_process_queue_readback(self):
        events, close, expected = self.redcut_fixture()
        for actor in ('executor', 'tick'):
            with self.subTest(actor=actor):
                self.seed(events)
                ledger = 'L-' + actor + '-redcut.jsonl'
                path = self.root / 'events' / ledger
                path.unlink(missing_ok=True)
                fields = {k: v for k, v in close.items() if k not in ('v', 'type', 'subject', 'ts')}
                mq._train_emit(ledger, close['subject'], 'window-redcut', **fields)
                self.assertEqual(self.separate_queue(), expected)
                mq._train_emit(ledger, close['subject'], 'window-redcut', **fields)
                self.assertEqual(self.separate_queue(), expected)
                successor = dict(self.event('queue-batch', 0, project='p',
                                           specs=','.join(close['returned'])), subject='window-next')
                self.seed(events + [successor])
                mq._train_emit(ledger, close['subject'], 'window-redcut', **fields)
                self.assertEqual(self.separate_queue(), [dict(r, batch='window-next')
                                 if r['spec'] in close['returned'] else r for r in expected])
                self.assertFalse(any(json.loads(line)['type'] == 'queue-add'
                                     for line in path.read_text().splitlines()))
                path.unlink()

    def test_redcut_admission_requires_actor_and_fields(self):
        _, close, _ = self.redcut_fixture()
        for actor in ('executor', 'tick'):
            self.assertIsNone(fold.check_append(close, actor))
            for field in ('window', 'project', 'candidate', 'base', 'dropped', 'returned',
                          'flakes', 'residual', 'first_red_at', 'closed_at', 'outcome', 'automatic'):
                malformed = dict(close)
                malformed.pop(field)
                self.assertIsNotNone(fold.check_append(malformed, actor), field)
        for actor in ('builder', 'grader', 'reviewer', 'thinker', 'operator'):
            self.assertIsNotNone(fold.admits(close, actor), actor)

    def test_redcut_list_fields_accept_empty_but_refuse_missing_or_nonlist(self):
        _, close, _ = self.redcut_fixture()
        fields = ('dropped', 'returned', 'flakes', 'residual')
        empty = dict(close, **dict.fromkeys(fields, []))
        for actor in ('executor', 'tick'):
            with self.subTest(actor=actor, case='empty lists'):
                self.assertIsNone(fold.check_append(empty, actor))
            for field in fields:
                missing = dict(empty)
                missing.pop(field)
                with self.subTest(actor=actor, field=field, case='missing'):
                    self.assertIsNotNone(fold.check_append(missing, actor))
                for value in (None, '', 'node', {}, 0, False, ('node',)):
                    with self.subTest(actor=actor, field=field, value=value):
                        self.assertIsNotNone(fold.check_append(
                            dict(empty, **{field: value}), actor))


    def git(self, *argv, repo=None):
        result = subprocess.run(['git', *argv], cwd=repo or self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fixture(self):
        self.repo = self.root / 'repos' / 'proj'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Queue fixture')
        for name in ('allowed.txt', 'outside.txt'):
            (self.repo / name).write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture base')
        self.git('checkout', '-qb', self.sid.lower())
        (self.repo / 'allowed.txt').write_text('after\n')
        self.git('commit', '-qam', 'fixture change')
        self.git('checkout', '-q', 'main')
        (self.root / 'content' / (self.sid + '.md')).write_text('Writes: allowed.txt\n')
        self.events = [self.event('spec-written', 100, project='proj', footprint=['allowed.txt']),
                       self.event('build-done', 90), self.event('review', 70, confirmed=True, n_blocking=0)]

    def row(self, rule='gate', **kw):
        return dict(spec=self.sid, rule=rule, since=self.events[-1]['ts'], limit_min=60,
                    overdue=False, reason='condition met', reason_live=False, **kw)

    def fake(self, argv, **kw):
        self.calls.append((argv, kw))
        return dict(code=0, stdout='', stderr='')

    def due(self, rule='gate', *, row=None, **kw):
        with patch.object(lane_rules, 'due', return_value=[row or self.row(rule)]):
            return mq.enqueue_due(self.events, self.now, runner=kw.pop('runner', self.fake), **kw)

    def clean(self, project='proj'):
        self.events.append(self.event('merge-gate-clean', 65, project=project, branch=self.sid.lower()))

    def seed(self, events, filename='L-executor-0001.jsonl'):
        (self.root / 'events' / filename).write_text(''.join(json.dumps(e) + '\n' for e in events))

    def cli(self, *args, ledger=None):
        env = dict(os.environ)
        if ledger:
            env['DOIT_LEDGER_FILE'] = ledger
        return subprocess.run([DOIT, *args], env=env, capture_output=True, text=True)

    def separate_queue(self):
        code = 'import fold,merge_queue,json; print(json.dumps(merge_queue.queue(fold.read_events())))'
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_store_admission_and_roundtrip(self):
        from fold_emits import EMITS
        for kind in ('queue-add', 'queue-remove', 'queue-batch'):
            self.assertTrue({'tick', 'executor'} <= EMITS[kind])
        base = dict(subject=self.sid)
        self.now -= timedelta(seconds=2)
        dispatch.emit(tick.tick_path(), base, 'queue-add', project='p', branch='b', rule='enqueue')
        self.assertEqual(self.separate_queue()[0]['spec'], self.sid)
        dispatch.emit(tick.tick_path(), base, 'queue-remove', reason='operator')
        self.assertEqual(self.separate_queue(), [])
        bad = self.root / 'events' / 'L-grader-0001.jsonl'
        dispatch.emit(bad, base, 'queue-add', project='p', branch='b', rule='enqueue')
        self.assertTrue(bad.read_text())
        self.assertEqual(self.separate_queue(), [])
        result = self.cli('append', 'queue-add', self.sid, 'project=p', 'branch=b', 'rule=operator')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.separate_queue()), 1)
        result = self.cli('append', 'queue-remove', self.sid, 'reason=operator')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.separate_queue(), [])
        self.mark(2, 'roundtrip ok', 'wrong-actor ignored', 'emits rows present')

    def test_gate_routing_failures(self):
        self.fixture()
        self.due()
        argv, kw = self.calls[0]
        self.assertEqual(argv, ['doit', 'gate', self.sid.lower(), 'main', '--spec', self.sid,
                                '--writes', 'allowed.txt'])
        self.assertEqual(kw['cwd'], str(self.repo))
        self.assertNotEqual(kw['cwd'], os.getcwd())
        self.assertEqual(kw['env']['DOIT_GATE_LEDGER_FILE'], 'L-tick-local.jsonl')
        self.assertEqual(self.landed()[0]['type'], 'autodispatched')
        self.assertEqual(self.landed()[0]['since'], self.events[-1]['ts'])
        self.calls.clear()
        self.events[0].pop('footprint')
        self.due()
        self.assertEqual(self.calls, [])
        self.assertIn('footprint', self.landed()[-1]['reason'])
        self.events[0]['footprint'] = ['allowed.txt']
        self.due(runner=lambda *a, **kw: dict(code=1, stdout='first\nlast diagnostic', stderr=''))
        self.assertEqual(self.landed()[-1]['reason'], 'last diagnostic')
        self.assertEqual(self.landed()[-1]['role'], 'gate')
        self.assertFalse(any(e['type'] == 'queue-add' for e in self.landed()))
        self.events[0]['project'] = 'absent'
        self.due()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.landed()[-1]['type'], 'autodispatch-failed')
        self.mark(3, 'gate-argv ok', 'gate-cwd ok', 'gate-env ok', 'no-footprint ok',
                  'gate-fail ok', 'repo-unresolved ok')

    def test_real_gate(self):
        self.fixture()
        with tempfile.TemporaryDirectory(prefix='queue-other-cwd-') as elsewhere:
            old = os.getcwd()
            try:
                os.chdir(elsewhere)
                self.due(runner=autodispatch._real_runner)
            finally:
                os.chdir(old)
        clean = [e for e in self.landed() if e['type'] == 'merge-gate-clean']
        self.assertEqual(len(clean), 1, self.landed())
        self.assertEqual(clean[0]['branch'], self.sid.lower())
        self.assertEqual(clean[0]['repo'], str(self.repo))
        self.assertEqual(clean[0]['subject'], self.sid)
        self.assertFalse((self.root / 'events' / 'L-executor-0001.jsonl').exists())
        self.assertEqual(self.landed()[0]['rule'], 'gate')
        self.git('checkout', '-q', self.sid.lower())
        self.assertEqual(len(self.git('ls-files', '--', 'outside.txt').splitlines()), 1)
        self.git('rm', 'outside.txt')
        self.git('commit', '-qam', 'outside grant')
        self.git('checkout', '-q', 'main')
        count = len(self.landed())
        self.due(runner=autodispatch._real_runner)
        fresh = self.landed()[count:]
        self.assertTrue(any(e['type'].startswith('merge-gate-') for e in fresh))
        self.assertFalse(any(e['type'] in ('merge-gate-clean', 'queue-add') for e in fresh))
        self.mark(4, 'real gate clean in tick file', 'real gate wrong repo not used', 'real gate refusal ok')

    def test_enqueue_base_and_real_rules(self):
        self.fixture()
        self.clean()
        self.due('enqueue')
        self.assertEqual([e['type'] for e in self.landed()], ['autodispatched', 'queue-add'])
        self.assertEqual(self.landed()[-1]['branch'], self.sid.lower())
        self.events.extend(self.landed())
        count = len(self.landed())
        self.due('enqueue')
        self.assertFalse(any(e['type'] == 'queue-add' for e in self.landed()[count:]))
        # The ladder reads the injected row's limit, including an existing retry.
        events = [self.event('autodispatched', 2, rule='enqueue', since=self.row('enqueue')['since'], trigger='retry')]
        row = self.row('enqueue')
        row['limit_min'] = 1
        self.assertEqual(lane_rules.ladder(events, row, self.now), 'inbox')
        row['limit_min'] = 7
        self.assertEqual(lane_rules.ladder(events, row, self.now), 'retry')
        source = Path(mq.__file__).read_text()
        self.assertNotIn('limit_min=', source)
        self.events = self.events[:4]
        rows = lane_rules.due(self.events, fold.fold(self.events)[0], self.now)
        enqueue = next(r for r in rows if r['rule'] == 'enqueue')
        self.assertEqual(enqueue['since'], self.events[2]['ts'])
        self.assertEqual(enqueue['limit_min'], lane_rules.load()['enqueue']['limit_min'])
        mq.enqueue_due(self.events, self.now, runner=self.fake)
        self.assertEqual(self.landed()[-1]['type'], 'queue-add')
        self.mark(5, 'enqueue ok', 'no-requeue ok', 'limit from row ok', 'real due ok')
        self.git('branch', 'realm')
        specpath = self.root / 'content' / (self.sid + '.md')
        for text, blocked, label in [
            ('Base: realm', True, 'base-realm blocked ok'),
            ('Base: SD1 — master after L-spec-0652 and L-spec-0636 merge', False, 'base-sd1 prose ok'),
            ('base: origin/master 9fd2a0547c', False, 'base-lowercase-origin prose ok'),
            ('base: albert-scott master at 9b2827e152', False, 'base-lowercase-text prose ok'),
            ('Base: main', False, 'base-head allowed ok'),
            ('Base: origin/main', False, 'base-remote prose ok')]:
            with self.subTest(text=text):
                specpath.write_text(text + '\n')
                before = len(self.landed())
                self.due('enqueue')
                adds = [e for e in self.landed()[before:] if e['type'] == 'queue-add']
                self.assertEqual(len(adds), 0 if blocked else 1)
                self.mark(5, label)
        specpath.unlink()
        self.due('enqueue')
        self.assertEqual(self.landed()[-1]['type'], 'autodispatch-failed')
        self.assertEqual(self.landed()[-1]['role'], 'enqueue')
        self.mark(5, 'base-undetermined blocked ok')

    def test_retry_suppression_and_dry_run(self):
        self.fixture()
        row = self.row()
        self.due(row=row)
        self.assertEqual(len(self.calls), 1)
        self.events.extend(self.landed())
        self.due(row=row)
        self.assertEqual(len(self.calls), 1)
        row['overdue'] = True
        self.due(row=row)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.landed()[-1]['trigger'], 'retry')
        self.events.extend(self.landed()[-1:])
        self.due(row=row)
        self.assertEqual(len(self.calls), 2)
        with patch.object(lane_rules, 'ladder', return_value='inbox'):
            self.assertEqual(self.due(row=row), [])
        with patch.object(lane_rules, 'ladder', return_value='digest'):
            self.assertEqual(self.due(row=row), [])
        row['overdue'] = False
        self.events = self.events[:3]
        for kind in ('spawn-started', 'spawn-failed', 'autodispatch-failed', 'autodispatched'):
            extra = self.event(kind, 1, role='gate', rule='gate', spawn='L-gate-9001', since=row['since'])
            self.events.append(extra)
            self.due(row=row)
            self.events.pop()
        with patch.object(fold, 'held_specs', return_value={self.sid}):
            self.due(row=row)
        self.assertEqual(len(self.calls), 2)
        before = tick.tick_path().read_bytes()
        self.assertIn('would gate', self.due(row=row, dry_run=True)[0])
        self.assertEqual(tick.tick_path().read_bytes(), before)
        self.assertEqual(len(self.calls), 2)
        row['reason'] = 'undetermined: missing row'
        self.assertIn('undetermined:', self.due(row=row)[0])
        self.assertEqual(tick.tick_path().read_bytes(), before)
        self.mark(6, 'act', 'retry once', 'inbox silent', 'suppressed', 'dry-run silent', 'undetermined named')

    def window_seed(self, count=2):
        events = [self.event('queue-add', 5, project='albert-scott', branch='branch', rule='operator')]
        events.extend(dict(events[0], subject=f'L-spec-{9001+i}') for i in range(1, count))
        for event in events:
            event['branch'] = event['subject'].lower()
        self.seed(events)

    def batching_mark(self, ac, cases):
        body = json.dumps(cases, sort_keys=True)
        print(json.dumps(dict(url=f'cli://doit-v2/window-batching/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              body_excerpt=body)))

    def batching_case(self, count, *, config='batch_cap=10\nbatch_cap_floor=6',
                      extra=(), paths=None, ceiling=10, rate=None, detached=False,
                      code=0, independent=False, callback=None, repository=True, queue_rows=None):
        if config == 'batch_cap=10\nbatch_cap_floor=6' and ceiling != 10:
            config = f'batch_cap={ceiling}\nbatch_cap_floor=6'
        ids = [f'L-spec-{9001+i}' for i in range(count)]
        self.seed([dict(self.event('queue-add', 5, project='albert-scott',
                                   branch=sid.lower(), rule='operator'), subject=sid)
                   for sid in ids] if queue_rows is None else queue_rows)
        with (self.root / 'events' / 'L-executor-0001.jsonl').open('a') as stream:
            stream.write(''.join(json.dumps(e) + '\n' for e in extra))
        config_path = self.root / ('missing.toml' if config is None else 'look.toml')
        if config is not None:
            config_path.write_text('[merge_queue]\n' + config + '\n')
        batches_before = sum(e['type'] == 'queue-batch' for e in fold.read_events(use_cache=False))
        calls, out, err = [], io.StringIO(), io.StringIO()
        def gate(argv, **kw):
            events = self.fresh_events() if independent else fold.read_events(use_cache=False)
            rows = [e for e in events if e['type'] == 'queue-batch']
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['subject'], rows[0]['project'], rows[0]['specs']),
                             (argv[2], 'albert-scott', argv[argv.index('--specs') + 1]))
            calls.append(argv)
            if callback:
                callback(argv)
            if detached:
                return type('Child', (), {'pid': os.getpid()})()
            return dict(code=code)
        repository_value = (self.root, 'main') if repository else None
        with patch.object(mq, '_look_toml', return_value=config_path), \
             patch.object(mq, '_repository', **({'return_value': repository_value} if repository else
                                              {'side_effect': OSError('repository unavailable')})), \
             patch.object(mq, 'spec_paths', side_effect=lambda repo, master, branch:
                          [('M', branch + '.py')] if paths is None else paths.get(branch)), \
             patch.object(mq.window_gate, 'red_rate', return_value=rate or dict(rate=None, windows=0)), \
             patch.dict(os.environ, DOIT_WINDOW_MAX_MERGES=str(ceiling)), \
             redirect_stdout(out), redirect_stderr(err):
            result = mq.window_run('albert-scott', now=self.now,
                                   **({'detach': True, 'launcher': gate} if detached else {'runner': gate}))
        selected = calls[0][calls[0].index('--specs') + 1].split(',') if calls else []
        events = fold.read_events(use_cache=False)
        if not calls:
            self.assertEqual(sum(e['type'] == 'queue-batch' for e in events), batches_before,
                             'refused launch appended a queue-batch')
        return dict(code=result, selected=selected, stdout=out.getvalue(), stderr=err.getvalue(),
                    events=events, calls=calls)

    def test_ac1_fill_to_cap(self):
        matrix = []
        for cap in (1, 3, 6, 10):
            for count in (0, 1, 2, 3, 6, 7, 9, 10, 11, 17):
                result = self.batching_case(count, ceiling=cap)
                expected = [f'L-spec-{9001+i}' for i in range(min(count, cap))]
                self.assertEqual(result['selected'], expected)
                self.assertEqual(result['code'], 0 if count else 3)
                batches = [e for e in result['events'] if e['type'] == 'queue-batch']
                self.assertEqual(len(batches), int(count > 0))
                if count:
                    self.assertEqual(batches[0]['cap'], cap)
                else:
                    self.assertEqual(result['calls'], [])
                matrix.append(dict(queue_len=count, cap=cap, actual=result['selected'], expected=expected))
        for literal in (None, '2', '6', '99', 'true', '"malformed"'):
            config = 'batch_cap=10\nbatch_cap_floor=6'
            if literal is not None:
                config += '\nwindow_target=' + literal
            result = self.batching_case(11, config=config)
            expected = [f'L-spec-{9001+i}' for i in range(10)]
            self.assertEqual(result['selected'], expected)
            matrix.append(dict(legacy=literal, cap=10, actual=result['selected'], expected=expected))
        self.batching_mark(1, matrix)

    def fill_row(self, sid, project='albert-scott', **fields):
        return dict(self.event('queue-add', 5, project=project, branch=sid.lower(), rule='operator'),
                    subject=sid, **fields)

    def fill_metadata(self, tiers):
        self.seed([dict(self.event('spec-written', 10, charter=charter), subject=sid)
                   for sid, charter in tiers.items() if charter], 'L-spec-writer-local.jsonl')

    def fill_report(self, name, rows, result, expected, cap):
        snapshot = mq._ordered(result['events'])
        queue = mq.queue(snapshot, 'albert-scott')
        resolved = {r['spec']: dispatch.resolve_charter(snapshot, r['spec'], None) for r in queue}
        self.assertEqual(result['selected'], expected, name)
        self.assertEqual(result['code'], 0 if expected else 3, result['stderr'])
        if not expected:
            self.assertEqual(result['calls'], [])
            self.assertFalse(any(e['type'] == 'queue-batch' for e in result['events']))
        return dict(case=name, input=[r['subject'] for r in rows], resolved=resolved,
                    cap=cap, actual=result['selected'], expected=expected,
                    deferred=[r['spec'] for r in queue if r['spec'] not in expected])

    def test_ac2_fill_priority(self):
        ids = [f'L-spec-{9100+i}' for i in range(8)]
        a, b, c, d, e, f, g, h = ids
        tiers = {a: 'L-charter-other', b: 'L-charter-0074', c: 'L-charter-0072',
                 d: None, e: '/content/L-charter-0072.md', f: 'L-charter-0074',
                 g: 'L-charter-other', h: 'L-charter-0072'}
        self.fill_metadata(tiers)
        rows = [self.fill_row(sid) for sid in ids]
        cases = []
        def run(name, expected, *, inputs=rows, cap=4, paths=None, extra=()):
            result = self.batching_case(0, queue_rows=inputs, ceiling=cap, paths=paths, extra=extra)
            cases.append(self.fill_report(name, inputs, result, expected, cap))
        run('interleaved-equal-timestamps-path-and-missing', [c, e, h, b])
        correction = dict(self.event('spec-carried', 1, charter='/content/L-charter-0072.md'), subject=b)
        run('newest-metadata-correction', [b, c, e, h], extra=[correction])
        correction_written = dict(self.event('spec-written', 1, charter='L-charter-other'), subject=c)
        with (self.root / 'events' / 'L-spec-writer-local.jsonl').open('a') as stream:
            stream.write(json.dumps(correction_written) + '\n')
        run('newest-written-metadata', [e, h, b, f])
        self.fill_metadata(tiers)
        remove = dict(self.event('queue-remove', 3, reason='operator'), subject=c)
        readd = dict(self.fill_row(c), ts=(self.now - timedelta(minutes=2)).isoformat())
        terminal = dict(self.event('shipped', 1), subject=h)
        run('remove-readd-and-terminal', [e, c, b, f], extra=[remove, readd, terminal])
        other = self.fill_row('L-spec-9199', 'other')
        run('mixed-project-success', [c, e, h, b], inputs=rows[:1] + [other] + rows[1:])
        for project in ('other', None):
            head = dict(other, project=project)
            run('other-project-head' if project else 'undetermined-project-head', [], inputs=[head] + rows)
        paths = {sid.lower(): [('M', 'shared.py' if sid in (a, c, h) else sid + '.py')] for sid in ids}
        run('overlap-backfills-with-priority-and-fifo', [c, e, b, f], paths=paths)
        paths[c.lower()] = [('M', 'deploy.sh')]
        run('risk-label-keeps-membership', [c, e, h, b], paths=paths)
        paths[e.lower()] = None
        run('unknown-footprint-refuses', [], paths=paths)
        self.batching_mark(2, cases)

    def test_ac3_fill_ledger(self):
        cases = []
        for filename, actor in (('L-executor-0001.jsonl', 'executor'), ('L-tick-local.jsonl', 'tick')):
            for detached in (False, True):
                for name, count, cap, paths, expected_reason in (
                    ('configured-cap', 4, 3, None, 'selection-exclusions'),
                    ('cap-one', 4, 1, None, 'selection-exclusions'),
                    ('all-ready', 3, 10, None, 'all-ready'),
                    ('sole-eligible', 1, 10, None, 'sole-eligible'),
                    ('unknown-head', 3, 10, {'l-spec-9001': None}, None),
                    ('empty-footprints', 3, 10, {f'l-spec-{9001+i}': [] for i in range(3)}, 'all-ready'),
                    ('mixed-project-success', 3, 10, None, 'all-ready'),
                    ('other-project-head', 3, 10, None, None)):
                    # Start each production append in a fresh ledger, including for tick.
                    for path in (self.root / 'events').glob('*.jsonl'):
                        path.unlink()
                    rows = [self.fill_row(f'L-spec-{9001+i}') for i in range(count)]
                    if name == 'mixed-project-success':
                        rows.insert(1, self.fill_row('L-spec-9999', 'other'))
                    if name == 'other-project-head':
                        rows.insert(0, self.fill_row('L-spec-9999', 'other'))
                        self.fill_metadata({r['subject']: 'L-charter-0072' for r in rows[1:]})
                    with patch.dict(os.environ, DOIT_LEDGER_FILE=filename):
                        result = self.batching_case(0, queue_rows=rows, ceiling=cap, paths=paths,
                                                    detached=detached, independent=True)
                    fresh = self.fresh_events()  # distinct Python process after the launch
                    batches = [e for e in fresh if e['type'] == 'queue-batch']
                    self.assertEqual(len(batches), int(expected_reason is not None))
                    if expected_reason is None:
                        self.assertEqual((result['code'], result['calls']), (3, []))
                        cases.append(dict(case=name, actor=actor, detached=detached, refused=True))
                        continue
                    batch = batches[0]
                    self.assertEqual(result['code'], 0, result['stderr'])
                    self.assertEqual((batch['actor'], batch['project'], batch['reason'], batch['cap'], batch['queue_len']),
                                     (actor, 'albert-scott', expected_reason, cap, count))
                    self.assertEqual(batch['specs'].split(','), result['selected'])
                    self.assertEqual(batch['specs'], result['calls'][0][-1])
                    self.assertEqual(batch['_src'].split(':')[0], filename)
                    self.assertIs(type(batch['cap']), int)
                    self.assertIs(type(batch['queue_len']), int)
                    if len(result['selected']) < min(cap, count):
                        self.assertEqual(batch['reason'], 'selection-exclusions')
                    cases.append(dict(case=name, detached=detached, actor=batch['actor'], cap=batch['cap'],
                                      reason=batch['reason'], queue_len=batch['queue_len'],
                                      committed=batch['specs'], launch=result['calls'][0][-1]))
        old = dict(self.event('queue-batch', 1, project='albert-scott', specs=self.sid), subject='window-old')
        self.seed([self.fill_row(self.sid), old])
        self.assertEqual(mq.queue(self.fresh_events())[0]['batch'], 'window-old')
        for actor in ('operator', 'builder', 'grader'):
            with self.assertRaisesRegex(ValueError, 'unsupported train ledger actor'):
                mq._train_emit(f'L-{actor}-local.jsonl', 'window-denied', 'queue-batch',
                               project='albert-scott', specs=self.sid, reason='sole-eligible', cap=10, queue_len=1)
        self.batching_mark(3, dict(readbacks=cases, historical_fold=True, unsupported_actors_refused=True))

    def test_ac4_fill_executor(self):
        import re
        text = (Path(DOIT).parent / 'agents' / 'executor.md').read_text()
        launch = text[text.index('`doit window-run'):text.index('## Input')]
        merge = text[text.index('**Project `albert-scott` merges'):text.index('**4** `DOIT_WINDOW_GATE=off`')]
        self.assertIn("Executor's only", launch)
        self.assertIn('doit window-run --project <project> --detach', launch)
        self.assertIn('`doit gate-window --specs` launches are forbidden', launch)
        self.assertIn('including detached launches', launch)
        for line in (launch + merge).splitlines():
            if re.search(r'gate-window.*--specs|(?:run|launch).*`doit gate-window', line):
                self.assertRegex(line, r'forbidden|never|Never')
        self.assertNotRegex(launch + merge, r'(?i)at least five|batching is off|six.spec|window_target|only then does adaptive|measurement proves')
        for phrase in ('L-charter-0072 first', 'L-charter-0074 second', 'effective', 'queue order within each tier',
                       'solo isolation', 'queue-batch', '`reason`', '`cap`', '`queue_len`',
                       'one window at a time', 'Tick-owned: gate', 'single-instance lock'):
            self.assertIn(phrase, merge)
        self.assertIn('Never sleep or poll CI', launch)
        self.assertIn('Never run a window in the foreground', launch)
        self.assertIn('only\n  for an operator-declared CI outage', text)
        self.batching_mark(4, dict(launch=launch, merge=merge, operator_outage_preserved=True))

    def test_ac5_fill_safety(self):
        # These retain their independent assertions, real ledger writes and process locks.
        for name in ('test_window_batching_round_trip', 'test_window_batching_caps',
                     'test_train_lock_and_detach', 'test_train_caps'):
            with self.subTest(safety_regression=name):
                result = unittest.TestResult()
                type(self)(name).run(result)
                self.assertTrue(result.wasSuccessful(), str(result.failures + result.errors))
        result = self.batching_case(3, repository=False)
        self.assertEqual((result['code'], result['calls']), (3, []))
        self.assertTrue(any(e['type'] == 'window-refused' for e in result['events']))
        self.batching_mark(5, dict(project_lock=True, reservation=True, active_window=True, nightly_freeze=True,
                                  malformed_timestamp=True, terminal_reconciliation_once=True, repository_refusal=True,
                                  red_over_040=True, restore_at_040=True, few_windows_preserve=True,
                                  unreadable_floor=True, max_merges=True, detached_no_wait=True, exit_codes=[0,1,2,3,4]))

    def test_ac6_fill_tick_fifo_attribution(self):
        import nextstep
        cases = []
        trigger = 'L-spec-9200'
        priority = ['L-spec-9201', 'L-spec-9202', 'L-spec-9203']
        for risky, cap in ((False, 1), (True, 3)):
            for path in (self.root / 'events').glob('*.jsonl'):
                path.unlink()
            self.fill_metadata({trigger: 'L-charter-other', **{s: 'L-charter-0072' for s in priority}})
            self.seed([self.fill_row(s) for s in [trigger] + priority])
            snapshot = mq._ordered(fold.read_events(use_cache=False))
            self.assertEqual(mq.next_candidate(snapshot), trigger)
            step = nextstep.Step(trigger, 'spec', 'merging', 'action', 'merge', 'tick',
                                 None, 'ready', None, snapshot[-1]['ts'])
            calls = []
            def launcher(argv, **kwargs):
                calls.append(argv)
                batch = [e for e in self.fresh_events() if e['type'] == 'queue-batch']
                self.assertEqual(len(batch), 1)
                self.assertEqual(batch[0]['specs'], argv[-1])
                return type('Child', (), {'pid': os.getpid(), 'wait': lambda self: (_ for _ in ()).throw(
                    AssertionError('detached launch waited'))})()
            def dispatcher(argv):
                self.assertEqual(argv, ['doit', 'window-run', '--project', 'albert-scott', '--detach'])
                return dict(code=mq.window_run('albert-scott', detach=True, launcher=launcher, now=self.now))
            dispatcher.events = snapshot
            config = self.root / 'capacity.toml'
            config.write_text(f'[merge_queue]\nbatch_cap={cap}\n')
            with patch.object(mq, '_look_toml', return_value=config), \
                 patch.object(mq.window_gate, 'train_events', return_value=snapshot), \
                 patch.object(mq, '_repository', return_value=(self.root, 'main')), \
                 patch.object(mq, 'spec_paths', side_effect=lambda repo, master, branch:
                              [('M', 'deploy.sh' if risky and branch == trigger.lower() else branch + '.py')]), \
                 patch.object(mq.window_gate, 'red_rate', return_value=dict(rate=None, windows=0)), \
                 patch.dict(os.environ, DOIT_WINDOW_MAX_MERGES=str(cap), DOIT_LEDGER_FILE='L-tick-local.jsonl'):
                returned = tick.launch(step, dispatcher=dispatcher)
            self.assertEqual(returned, trigger)
            self.assertEqual(len(calls), 1)
            expected = priority[:cap]
            self.assertEqual(calls[0][-1].split(','), expected)
            batch = [e for e in self.fresh_events() if e['type'] == 'queue-batch'][0]
            self.assertEqual(batch['specs'].split(','), expected)
            self.assertNotIn(trigger, expected)
            self.assertEqual((batch['queue_len'], batch['cap'], batch['actor']), (4, cap, 'tick'))
            cases.append(dict(fifo_trigger=trigger, risky_trigger=risky, cap=cap,
                              tiers={s: dispatch.resolve_charter(snapshot, s, None) for s in [trigger] + priority},
                              actual=calls[0][-1].split(','), expected=expected, attribution=returned))
        self.batching_mark(6, cases)

    def assert_batching(self, result, expected, reason, deferred):
        self.assertEqual(result['code'], 0, result['stderr'])
        self.assertEqual(result['selected'], expected)
        self.assertIn(f'window-run: selected {len(expected)}; skipped {deferred};', result['stdout'])
        batch = [e for e in result['events'] if e['type'] == 'queue-batch'][-1]
        self.assertEqual(len(batch['skipped']), deferred)
        for text in ('authorised measurement', 'single queue head'):
            self.assertNotIn(text, result['stdout'] + result['stderr'])

    def test_window_batching_ready_sizes(self):
        matrix = []
        for count in (0, 1, 2, 3, 4, 5, 6, 7, 12):
            result = self.batching_case(count)
            expected = [f'L-spec-{9001+i}' for i in range(min(count, 10))]
            if count:
                reason = 'sole-eligible' if count == 1 else 'all-ready' if count <= 10 else 'effective-cap'
                self.assert_batching(result, expected, reason, count - len(expected))
            else:
                self.assertEqual((result['code'], result['calls']), (3, []))
            matrix.append(dict(ready=count, selected=result['selected'], stdout=result['stdout']))
        for fields in ({}, dict(equal_input=False), dict(equal_input=True, single_p50_s=20, batched_p50_s=10),
                       dict(equal_input=True, single_p50_s=10, batched_p50_s=20)):
            measurement = dict(self.event('merge-measurement', 2, **fields), actor='operator')
            result = self.batching_case(3, extra=[measurement])
            self.assert_batching(result, ['L-spec-9001', 'L-spec-9002', 'L-spec-9003'], 'all-ready', 0)
            matrix.append(dict(measurement=fields, selected=result['selected']))
        for count in range(13):
            self.assertEqual(mq.batching_allowed([], count), count >= 2)
        malformed = dict(self.event('queue-add', project='albert-scott', branch='b'), ts='bad')
        for count in (0, 1, 2, 6):
            with self.assertRaisesRegex(ValueError, 'event timestamp undetermined'):
                mq.batching_allowed([malformed], count)
        result = self.batching_case(2, extra=[malformed])
        self.assertEqual((result['code'], result['calls']), (3, []))
        self.assertIn('window-run: undetermined', result['stderr'])
        self.assertFalse(any(e['type'] == 'queue-batch' for e in result['events']))
        result = self.batching_case(2, paths={'l-spec-9001': [('M', 'deploy.sh')], 'l-spec-9002': []})
        self.assert_batching(result, ['L-spec-9001', 'L-spec-9002'], 'all-ready', 0)
        for text in ('authorised measurement', 'single queue head'):
            self.assertNotIn(text, Path(mq.__file__).read_text())
        self.batching_mark(1, dict(matrix=matrix, malformed_refused=True, solo_head=True))

    def test_window_batching_caps(self):
        matrix = []
        for cap in (1, 3, 6, 10):
            for rate in (dict(rate=.41, windows=5), dict(rate=None, windows=5)):
                result = self.batching_case(12, config=f'batch_cap={cap}\nbatch_cap_floor=1', rate=rate)
                self.assert_batching(result, [f'L-spec-{9001+i}' for i in range(cap)], 'selection-exclusions', 12-cap)
                self.assertFalse(any(e['type'] == 'batch-cap' for e in result['events']))
                matrix.append(dict(cap=cap, rate=rate, selected=result['selected']))
        for config in (None, 'window_target=[', 'batch_cap=0', 'batch_cap=true', 'batch_cap="bad"', 'window_target=2'):
            result = self.batching_case(8, config=config)
            self.assertEqual((result['code'], result['calls']), (3, []))
            self.assertIn('configuration', result['stderr'])
        result = self.batching_case(6, config='batch_cap=6', ceiling=3)
        self.assert_batching(result, [f'L-spec-{9001+i}' for i in range(3)], 'selection-exclusions', 3)
        prior = dict(self.event('batch-cap', 2, cap=1, reason='operator'), subject='merge-train')
        result = self.batching_case(8, extra=[prior])
        self.assert_batching(result, [f'L-spec-{9001+i}' for i in range(8)], 'all-ready', 0)
        self.batching_mark(2, dict(configured_matrix=matrix, invalid_refused=True, ceiling_refused=True, prior_tuning_ignored=True))

    def test_window_batching_solo_and_order(self):
        cases = [([('M', 'deploy.sh')], True), ([('M', 'scripts/release_kit.sh')], True),
                 ([('M', 'scripts/release/x.sh')], True), ([('A', 'alembic/versions/a.py')], True),
                 (None, True), ([('M', 'src/code.py')], False),
                 ([('A', 'deploy/cron/x.sh')], False), ([('M', 'infra/x/deploy.sh')], False)]
        for paths, risky in cases:
            self.assertEqual(mq.is_risky(paths), risky)
            result = self.batching_case(2, paths={'l-spec-9001': paths, 'l-spec-9002': [('M', 'b')]})
            if paths is None:
                self.assertEqual((result['code'], result['calls']), (3, []))
            else:
                self.assert_batching(result, ['L-spec-9001', 'L-spec-9002'], 'all-ready', 0)
        paths = {'l-spec-9001': [('M', 'a')], 'l-spec-9002': [('M', 'b')],
                 'l-spec-9003': [('M', 'a'), ('M', 'c')], 'l-spec-9004': [('M', 'deploy.sh')],
                 'l-spec-9005': [('M', 'x')]}
        result = self.batching_case(5, paths=paths)
        self.assert_batching(result, ['L-spec-9001', 'L-spec-9002', 'L-spec-9004', 'L-spec-9005'], 'selection-exclusions', 1)
        for result in (self.batching_case(3, repository=False),
                       self.batching_case(2, paths={'l-spec-9001': None})):
            self.assertEqual((result['code'], result['calls']), (3, []))
            self.assertTrue(any(e['type'] == 'window-refused' for e in result['events']))
        # Retain the existing pure-function protections for duplicates, empty
        # footprints, unknown non-heads and a rename's two path names.
        rows = [dict(spec=s) for s in ('A', 'B', 'C', 'R', 'X', 'A')]
        footprints = dict(A={'a'}, B={'b'}, C={'a', 'c'}, R={'deploy.sh'}, X={'x'})
        first = mq.form_batch(rows, footprints, 6, {'R'})
        self.assertEqual(first, ['A', 'C', 'B', 'X'])
        self.assertEqual(mq.form_batch([r for r in rows if r['spec'] not in first], footprints, 6, {'R'}), ['R'])
        self.assertEqual(mq.form_batch(rows, dict(footprints, B=None, X=set()), 6, {'R'}), ['A', 'C', 'X'])
        self.assertEqual(mq.form_batch([dict(spec=s) for s in ('rename', 'old', 'empty')],
                                      dict(rename={'old.py', 'new.py'}, old={'old.py'}, empty=set()), 6, set()),
                         ['rename', 'old', 'empty'])
        self.batching_mark(3, dict(risk_classes=cases, overlap_helper_order=first, legacy_target_ignored=True,
                                   unavailable_repository_refusal=True, retained_solo_next=True))

    def test_window_batching_round_trip(self):
        reads = []
        for detached in (False, True):
            for code in ((0, 1, 2, 3, 4) if not detached else (0,)):
                result = self.batching_case(4, detached=detached, code=code, independent=True)
                self.assertEqual(result['code'], 0 if detached else code)
                self.assertEqual(result['selected'], [f'L-spec-{9001+i}' for i in range(4)])
                reads.append(dict(detached=detached, code=result['code'], specs=result['selected']))
        result = self.batching_case(0, independent=True)
        self.assertEqual((result['code'], result['calls']), (3, []))
        with patch.object(mq.window_gate, 'in_flight', return_value=[dict(window='live', specs=[self.sid])]):
            result = self.batching_case(3)
        self.assertEqual((result['code'], result['calls']), (3, []))
        # A live PID reservation is read from the real scratch state file.
        reservation = dict(self.event('queue-batch', 2, project='albert-scott', specs=self.sid),
                           subject='window-reserved')
        pid_path = self.root / 'state' / 'window-run.window-reserved.pid'
        pid_path.write_text(str(os.getpid()))
        result = self.batching_case(3, extra=[reservation])
        self.assertEqual((result['code'], result['calls']), (3, []))
        pid_path.unlink()
        red = self.nightly_event('nightly-red', 0, ids=['test_fixture'])
        self.seed([red], 'L-base-red-local.jsonl')
        result = self.batching_case(3)
        self.assertEqual((result['code'], result['calls']), (3, []))
        self.assertIn('window-run: unowned nightly red:', result['stdout'])
        (self.root / 'events' / 'L-base-red-local.jsonl').unlink()
        for target, name in ((mq, 'merge_freeze'), (mq.window_gate, 'train_events')):
            with patch.object(target, name, side_effect=OSError('unreadable')):
                result = self.batching_case(3)
            self.assertEqual((result['code'], result['calls']), (3, []))
            self.assertIn('window-run: undetermined', result['stderr'])
        wrong = dict(self.event('queue-add', 10, project='other', branch='other', rule='operator'),
                     subject='L-spec-9999')
        malformed = dict(wrong, ts='broken')
        for extra in (wrong, malformed):
            result = self.batching_case(3, extra=[extra])
            self.assertEqual((result['code'], result['calls']), (3, []))
            self.assertFalse(any(e['type'] == 'queue-batch' for e in result['events']))
        for project in ('../bad', '.', '..', ''):
            self.assertEqual(mq.window_run(project, runner=self.fake), 2)
        self.assertFalse(self.calls)
        # Real outcomes and a queue-add committed during the gate are folded
        # only into the next selection, never into the already-recorded batch.
        def outcomes(argv):
            for i, kind in enumerate(('shipped', 'window-dropped', 'merge-conflict')):
                self.simple_emit(kind, f'L-spec-{9001+i}', reason='fixture', window=argv[2])
            self.simple_add('L-spec-9010')
        result = self.batching_case(4, independent=True, callback=outcomes)
        self.assertEqual(result['selected'], [f'L-spec-{9001+i}' for i in range(4)])
        self.assertEqual([r['spec'] for r in mq.queue(self.fresh_events())], ['L-spec-9004', 'L-spec-9010'])
        self.assertEqual(len([e for e in result['events'] if e['type'] == 'queue-remove']), 3)
        code, calls = self.train_call()
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][-1], 'L-spec-9004,L-spec-9010')
        # Reuse the existing two-process lock/reservation regression, which
        # commits to the same production append path in this scratch store.
        self.test_train_concurrent()
        self.batching_mark(4, dict(independent_reads=reads, empty=True, live=True, reservation=True,
                                   freeze=True, unreadable=True, wrong_project=True, malformed=True,
                                   simultaneous_callers=True, terminal_reconciliation=True, mid_window_add=True))

    def test_window_launch_and_exit_codes(self):
        for code in (0, 1, 2, 3, 4):
            self.window_seed(7)
            def runner(argv):
                batch = [e for e in fold.read_events() if e['type'] == 'queue-batch'][-1]
                self.assertEqual(argv[2], batch['subject'])
                self.assertEqual(batch['specs'].split(','), [f'L-spec-{9001+i}' for i in range(min(7, mq.window_gate.max_merges(os.environ)))])
                return dict(code=code)
            with patch.object(mq, '_repository', return_value=(self.root, 'main')), \
                 patch.object(mq, 'spec_paths', return_value=[]):
                self.assertEqual(mq.window_run('albert-scott', runner=runner, now=self.now), code)
            self.assertEqual(len(mq.queue(fold.read_events())), 7)
        self.seed([])
        self.assertEqual(mq.window_run('albert-scott', runner=self.fake), 3)
        self.assertEqual(mq.window_run('../unsafe', runner=self.fake), 2)

    def test_window_outcomes_and_mid_run_add(self):
        self.window_seed(4)
        def runner(argv):
            for i, kind in enumerate(('shipped', 'window-dropped', 'merge-conflict')):
                event = dict(self.event(kind, -20), subject=f'L-spec-{9001+i}')
                with (self.root / 'events' / 'L-executor-0001.jsonl').open('a') as stream:
                    stream.write(json.dumps(event) + '\n')
            # Newer outcome wins without a second removal.
            with (self.root / 'events' / 'L-executor-0001.jsonl').open('a') as stream:
                stream.write(json.dumps(self.event('window-dropped', -19)) + '\n')
                stream.write(json.dumps(dict(self.event('queue-add', -20, project='albert-scott',
                                                        branch='new', rule='operator'), subject='L-spec-9010')) + '\n')
            return dict(code=1)
        with patch.object(mq, '_repository', return_value=(self.root, 'main')), patch.object(mq, 'spec_paths', return_value=[]):
            self.assertEqual(mq.window_run('albert-scott', runner=runner), 1)
        evs = fold.read_events()
        removes = [e for e in evs if e['type'] == 'queue-remove']
        self.assertEqual(len(removes), 3)
        self.assertEqual({e['subject']: e['reason'] for e in removes},
                         {'L-spec-9001': 'shipped', 'L-spec-9002': 'window-dropped', 'L-spec-9003': 'merge-conflict'})
        self.assertTrue(all(e['reason'] in mq.OUTCOMES for e in removes))
        self.assertEqual({r['spec'] for r in mq.queue(evs)}, {'L-spec-9004', 'L-spec-9010'})
        self.mark(8, 'shipped removed', 'dropped removed', 'conflict removed', 'no outcome stays')

    def test_reconciliation_and_parity(self):
        import window_gate
        add = self.event('queue-add', 20, project='albert-scott', branch='branch', rule='enqueue')
        for kind in (*sorted(mq.OUTCOMES), 'spec-killed', 'spec-closed'):
            evs = [add, self.event(kind, 1)]
            with patch.object(lane_rules, 'due', return_value=[]):
                mq.enqueue_due(evs, self.now, runner=self.fake)
            self.assertEqual(self.landed()[-1]['reason'], kind if kind in mq.OUTCOMES else 'killed')
            evs.extend(self.landed()[-1:])
            count = len(self.landed())
            with patch.object(lane_rules, 'due', return_value=[]):
                mq.enqueue_due(evs, self.now, runner=self.fake)
            self.assertEqual(len(self.landed()), count)
        clean = self.event('merge-gate-clean', 30, project='albert-scott', branch='branch')
        self.assertEqual([r['spec'] for r in mq.queue([clean, add])], [self.sid])
        self.assertEqual(mq.queue([clean]), [])
        conflict = [clean, add, self.event('merge-conflict', 1)]
        with patch.object(lane_rules, 'due', return_value=[]):
            mq.enqueue_due(conflict, self.now)
        self.assertEqual(mq.queue(conflict + self.landed()[-1:]), [])
        self.fixture()
        self.git('branch', 'realm')
        (self.root / 'content' / (self.sid + '.md')).write_text('Base: realm\n')
        self.clean('albert-scott')
        (self.root / 'repos' / 'albert-scott').symlink_to(self.repo, target_is_directory=True)
        before = len(self.landed())
        self.due('enqueue')
        self.assertFalse(any(e['type'] == 'queue-add' for e in self.landed()[before:]))
        self.assertEqual(mq.queue(self.events), [])
        self.mark(8, 'killed reconciled', 'parity plain', 'parity differs conflict', 'parity differs no add', 'parity differs base')

    def test_queue_cli(self):
        clean = self.event('merge-gate-clean', 20, project='proj', branch='branch')
        self.seed([clean])
        for argv in [('queue', 'add', self.sid), ('queue', 'add', self.sid, '--why', ''),
                     ('queue', 'add', 'L-spec-9999', '--why', 'reason'),
                     ('queue', 'remove', self.sid, '--why', 'reason')]:
            before = (self.root / 'events' / 'L-executor-0001.jsonl').read_bytes()
            self.assertEqual(self.cli(*argv).returncode, 1)
            self.assertEqual((self.root / 'events' / 'L-executor-0001.jsonl').read_bytes(), before)
        self.assertEqual(self.cli('queue', 'add', self.sid, '--why', 'correction').returncode, 0)
        self.assertEqual(self.cli('queue', 'add', self.sid, '--why', 'again').returncode, 1)
        result = self.cli('queue', 'list', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), mq.queue(fold.read_events()))
        self.assertIn(self.sid, self.cli('queue', 'list').stdout)
        self.assertEqual(json.loads(self.cli('queue', 'list', '--project', 'other', '--json').stdout), [])
        self.assertEqual(self.cli('queue', 'remove', self.sid, '--why', 'correction').returncode, 0)
        self.assertEqual(self.cli('queue', 'remove', self.sid, '--why', 'again').returncode, 1)
        self.assertEqual(self.cli('queue', 'add', self.sid, '--why', 'bad actor', ledger='L-grader-0001.jsonl').returncode, 1)
        self.assertFalse((self.root / 'events' / 'L-grader-0001.jsonl').exists())
        for line in (self.root / 'events' / 'L-executor-0001.jsonl').read_text().splitlines():
            self.assertNotIn('actor', json.loads(line))
        self.seed([self.event('queue-add', 1, project='proj', rule='operator')])
        missing = self.cli('queue', 'list', '--json')
        self.assertEqual(json.loads(missing.stdout), [])
        self.assertIn('omitted 1 spec(s); reason=queue-add missing branch', missing.stderr)
        self.mark(9, 'list', 'list json', 'add', 'add refused', 'remove', 'remove refused', 'no actor field')

    def test_autodispatch_integration(self):
        from contextlib import ExitStack

        sequence = []
        def driver(*a, **kw):
            sequence.append('driver')
            return ['driver line']
        def queue(*a, **kw):
            sequence.append('queue')
            self.assertEqual(a, ([], self.now))
            self.assertEqual(kw, {})
            return ['queue line']
        with ExitStack() as stack:
            # Isolate the pass's unrelated services from machine state and I/O.
            for target, result in (
                ('intake.run', {}), ('notes.run', None), ('pr_closeout.run', None),
                ('install_sync.run', []), ('push_origin.run', None),
                ('carry.uncarried', []), ('carry.sync_v4', None),
                ('pane_resume.run', None), ('crons.check', []),
                ('look._read_state', {'last_pass': self.now.isoformat()}),
                ('look.heartbeat_missed', False),
                ('tree_cleanup.reap_worktrees', ([], []))):
                stack.enter_context(patch(target, return_value=result))
            steps = stack.enter_context(patch.object(tick, '_steps', return_value=[]))
            stack.enter_context(patch.object(tick, 'run_steps', side_effect=driver))
            stack.enter_context(patch.object(autodispatch, 'redispatch_dead',
                side_effect=lambda *a, **kw: sequence.append('replay') or []))
            logged = stack.enter_context(patch.object(tick, '_log_lane_driver'))
            call = stack.enter_context(patch.object(mq, 'enqueue_due', side_effect=queue))
            tick._record()
            self.assertEqual(sequence, ['driver', 'replay', 'queue'])
            self.assertEqual(call.call_count, 1)
            self.assertEqual(steps.call_count, 2)
            logged.assert_any_call(['driver line', 'queue line'], self.now)
            self.assertNotIn('autodispatch_error', self.landed()[-1])
            call.reset_mock(side_effect=True)
            call.side_effect = ValueError('fixture failure')
            tick._record()
            self.assertEqual(call.call_count, 1)
            self.assertEqual(self.landed()[-1]['type'], 'tick')
            self.assertEqual(self.landed()[-1]['autodispatch_error'], 'fixture failure')
        self.mark(10, 'tick called once after driver', 'pass snapshot and clock',
                  'outcomes logged', 'queue error contained')

    def test_due_failure_and_unreadable_table(self):
        self.fixture()
        with patch.object(lane_rules, 'due', side_effect=ValueError('due failed')):
            self.assertIn('queue failed', mq.enqueue_due(self.events, self.now)[0])
        self.assertEqual(self.landed()[-1]['role'], 'queue')
        with patch.object(lane_rules, 'load', side_effect=ValueError('unreadable table')):
            self.due()
        self.assertEqual(len(self.calls), 1)

    def test_help_early_reader(self):
        # A one-byte reader keeps the first pipe page occupied until the
        # match. An early help entry used to leave cat blocked and SIGPIPE.
        for needle in (b'doit queue', b'doit window-run'):
            with subprocess.Popen([DOIT, 'help'], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, pipesize=4096) as process:
                body = bytearray()
                while needle not in body:
                    value = os.read(process.stdout.fileno(), 1)
                    self.assertTrue(value, 'help entry missing')
                    body.extend(value)
                process.stdout.close()
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read().decode())

    def test_executor_and_help(self):
        text = (Path(DOIT).parent / 'agents' / 'executor.md').read_text()
        for phrase in ('Tick-owned: gate', 'one window at a time', 'doit queue list'):
            self.assertIn(phrase, text)
        for phrase in ('--specs A,B,C', 'Run `doit gate` per branch', 'doit gate <branch> <main> --spec', 'seq-'):
            self.assertNotIn(phrase, text)
        result = self.cli('help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('doit window-run', result.stdout)
        self.assertIn('doit queue', result.stdout)
        self.mark(11, 'executor tick ownership ok', 'queue and window help ok')


    def train_mark(self, ac, *cases):
        body = '\n'.join(f'1154-AC{ac} {case} ok' for case in cases)
        print(body)
        print(json.dumps(dict(url=f'cli://doit-v2/merge-queue/1154/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_train_batches(self):
        rows = [dict(spec=s) for s in ('A', 'C', 'X', 'B', 'R')]
        paths = dict(A={'a'}, B={'a', 'b'}, C={'b'}, X={'x'}, R={'a'})
        before = json.dumps([rows, {k: sorted(v) for k, v in paths.items()}])
        self.assertEqual(mq.form_batch(rows, paths, 10, {'R'}), ['A', 'C', 'B', 'X'])
        self.assertEqual(mq.form_batch(rows, paths, 2, {'R'}), ['A', 'C'])
        self.assertEqual(mq.form_batch(rows, paths, 0, set()), ['A'])
        self.assertEqual(mq.form_batch([], {}, 10, set()), [])
        self.assertEqual(mq.form_batch(rows + rows, paths, 10, {'R'}), ['A', 'C', 'B', 'X'])
        self.assertEqual(before, json.dumps([rows, {k: sorted(v) for k, v in paths.items()}]))
        import ast
        node = next(n for n in ast.parse(Path(mq.__file__).read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'form_batch')
        self.assertFalse(any(isinstance(n, ast.Name) and n.id in ('open', 'os', 'fold', 'datetime', 'dispatch')
                             for n in ast.walk(node)))
        self.assertEqual(mq.form_batch([dict(spec='rename'), dict(spec='old')],
                                      {'rename': {'a.py', 'b.py'}, 'old': {'a.py'}}, 2, []), ['rename', 'old'])
        self.train_mark(1, 'head first', 'overlap transitive', 'rest in order', 'cap truncates',
                        'empty', 'pure', 'rename old path overlaps', 'cap below 1')
        self.assertEqual(mq.form_batch(rows, paths, 10, {'A'}), ['A'])
        first = mq.form_batch(rows, paths, 10, {'R'})
        self.assertNotIn('R', first)
        self.assertEqual(mq.form_batch([r for r in rows if r['spec'] not in first], paths, 10, {'R'}), ['R'])
        unknown = dict(paths, A=None, B=None)
        self.assertEqual(mq.form_batch(rows, unknown, 10, set()), ['A'])
        self.assertNotIn('B', mq.form_batch(rows, dict(paths, B=None), 10, set()))
        dozen = [dict(spec=str(i)) for i in range(12)]
        result = mq.form_batch(dozen, {str(i): set() for i in range(12)}, 10, {'2'})
        self.assertEqual(result, ['0', '1', *map(str, range(3, 11))])
        self.train_mark(2, 'risky head solo', 'risky non-head excluded', 'risky next as head',
                        'undetermined solo', 'undetermined excluded', 'twelve spec fixture')

    def test_train_paths(self):
        self.fixture()
        self.assertEqual(mq.spec_paths(self.repo, 'main', self.sid.lower()), [('M', 'allowed.txt')])
        self.assertIsNone(mq.spec_paths(self.repo, 'main', 'absent'))
        self.assertTrue(mq.is_risky(None))
        cases = [([('A', 'alembic/versions/1.py')], True, 'new migration risky'),
                 ([('A', 'api/alembic_alt/versions/1.py')], True, None),
                 ([('M', 'alembic/versions/1.py')], False, 'edited migration not risky'),
                 ([('D', 'deploy.sh')], True, 'deploy risky'),
                 ([('M', 'infra/lambda/lwa-rotation/deploy.sh'),
                   ('M', 'infra/lambda/telegram-alerter/deploy.sh')], False, 'nested lambda deploy not risky'),
                 ([('A', 'deploy/cron/anything')], False, 'deploy cron not risky'),
                 ([('M', 'scripts/release/x')], True, 'release dir risky'),
                 ([('D', 'scripts/release_kit.sh')], True, 'release_kit risky'),
                 ([('M', 'scripts/release_notes.md')], False, 'lookalike not risky'),
                 ([('A', 'alembic/env.py')], False, 'alembic env not risky')]
        for paths, expected, label in cases:
            self.assertEqual(mq.is_risky(paths), expected, paths)
            if label:
                self.train_mark(3, label)
        self.git('checkout', '-q', self.sid.lower())
        self.git('mv', 'allowed.txt', 'renamed file.py')
        odd = 'odd space-' + chr(0xdcff)
        (self.repo / odd).write_bytes(b'odd')
        self.git('add', '.')
        self.git('commit', '-qm', 'rename and odd name')
        paths = mq.spec_paths(self.repo, 'main', self.sid.lower())
        self.assertIn(('D', 'allowed.txt'), paths)
        self.assertIn(('A', 'renamed file.py'), paths)
        self.assertIn(('A', odd), paths)
        seen = []
        def runner(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 1, b'', b'failed')
        self.assertIsNone(mq.spec_paths(self.repo, 'main', 'branch', runner))
        self.assertIsInstance(seen[0], list)
        self.assertIn('--no-renames', seen[0])
        self.train_mark(3, 'real git paths', 'git failure undetermined', 'odd path roundtrip', 'rename both paths')

    def fresh_events(self):
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_READ_CACHE='0')
        result = subprocess.run([sys.executable, '-c',
                                 'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'],
                                cwd=Path(mq.__file__).parent, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_train_caps(self):
        config = self.root / 'look.toml'
        config.write_text('[merge_queue]\nbatch_cap = 9\nbatch_cap_floor = 6\n')
        def cap(rate=None, windows=0, events=(), path=config, env=None):
            with patch.object(mq.window_gate, 'red_rate', return_value=dict(rate=rate, windows=windows)):
                return mq.effective_cap(list(events), self.now, env={'DOIT_WINDOW_MAX_MERGES': '10'} if env is None else env, toml_path=path)
        self.assertEqual(cap()['configured'], 9)
        self.assertEqual(cap(path=self.root / 'missing')['cap'], 6)
        self.assertEqual(cap(path='')['cap'], 6)
        self.assertEqual(cap(None, 5)['cap'], 6)
        latest = [self.event('batch-cap', 2, cap=8, reason='operator'), self.event('batch-cap', 1, cap=7, reason='operator')]
        self.assertEqual(cap(events=latest)['cap'], 7)
        self.assertEqual(cap()['cap'], 9)
        self.assertEqual(cap(.41, 5)['cap'], 6)
        self.assertEqual(cap(.40, 5)['cap'], 9)
        self.assertEqual(cap(.2, 5)['cap'], 9)
        self.assertEqual(cap(.1, 5, env={'DOIT_WINDOW_MAX_MERGES': '3'})['cap'], 3)
        with patch.object(mq.window_gate, 'red_rate', side_effect=OSError('unreadable')):
            self.assertEqual(mq.effective_cap([], self.now, {}, config)['cap'], 4)
        for text, expected, floor in [('', 10, 6), ('batch_cap = "bad"', 10, 6),
                                      ('batch_cap = 0', 10, 6), ('batch_cap = 3\nbatch_cap_floor = 7', 3, 3),
                                      ('batch_cap = true\nbatch_cap_floor = -1', 10, 6)]:
            config.write_text('[merge_queue]\n' + text + '\n')
            value = cap()
            self.assertEqual((value['configured'], value['floor']), (expected, floor))
        config.write_text('[merge_queue]\nbatch_cap = 10\nbatch_cap_floor = 6\n')
        value = cap(.6, 5)
        for filename in ('L-executor-0001.jsonl', 'L-tick-local.jsonl'):
            mq.record_cap(value, [], filename)
            raw = (self.root / 'events' / filename).read_text()
            self.assertEqual(len(raw.splitlines()), 1)
            event = json.loads(raw)
            self.assertEqual(event['subject'], 'merge-train')
            self.assertNotIn('actor', event)
            self.assertEqual(event['from'], 10)
            self.assertEqual(cap(events=self.fresh_events())['cap'], 6)
            mq.record_cap(value, self.fresh_events(), filename)
            self.assertEqual((self.root / 'events' / filename).read_text(), raw)
        wrong = dict(self.event('batch-cap', -1, cap=99, reason='bad'), subject='merge-train')
        self.seed([wrong], 'L-builder-9999.jsonl')
        wrong_read = next(e for e in self.fresh_events() if e.get('cap') == 99)
        self.assertIsNotNone(fold.admits(wrong_read, wrong_read['actor']))
        self.assertEqual(cap(events=self.fresh_events())['cap'], 6)
        with self.assertRaises(ValueError):
            mq._train_emit('L-operator-local.jsonl', 'window-test', 'queue-batch', project='p', specs=self.sid)
        import fold_emits
        self.assertEqual(fold_emits.EMITS['batch-cap'], {'executor', 'tick', 'operator'})
        self.train_mark(4, 'config', 'unreadable floor', 'rate none many windows floor',
                        'few windows keep newest', 'few windows configured', 'floor on red', 'boundary 0.40',
                        'configured when calm', 'max_merges clamp', 'record once', 'unchanged silent',
                        'roundtrip fresh process', 'wrong actor ignored', 'emits row')

    def train_call(self, *, flying=(), depth=2, code=0, config=None, **kwargs):
        if config is None:
            config = self.root / 'train-look.toml'
            config.write_text('[merge_queue]\nbatch_cap=15\nbatch_cap_floor=6\n')
        calls = []
        def runner(argv):
            self.assertTrue(any(e['type'] == 'queue-batch' and e['subject'] == argv[2] for e in fold.read_events()))
            calls.append(argv)
            return dict(code=code)
        with patch.object(mq, '_look_toml', return_value=config), \
             patch.object(mq.window_gate, 'in_flight', return_value=list(flying)), \
             patch.object(mq, '_repository', return_value=(self.root, 'main')), \
             patch.object(mq, 'spec_paths', side_effect=lambda r, m, b: [('M', b)]):
            result = mq.window_run('albert-scott', runner=runner, now=self.now, **kwargs)
        return result, calls

    def test_cap_cases_ignore_operator_tuning(self):
        # An arbitrary future operator tuning must not change these scenarios.
        operator_config = self.root / 'operator-look.toml'
        operator_config.write_text('[merge_queue]\nbatch_cap=1\nbatch_cap_floor=1\n')
        with patch.object(mq, '_look_toml', return_value=operator_config):
            for name in ('test_ac1_fill_to_cap', 'test_window_batching_caps',
                         'test_train_caps', 'test_train_step'):
                with self.subTest(case=name):
                    result = unittest.TestResult()
                    type(self)(name).run(result)
                    self.assertTrue(result.wasSuccessful(), str(result.failures + result.errors))

    def test_train_step(self):
        self.window_seed(12)
        with patch.object(mq.window_gate, 'train_depth', side_effect=AssertionError('depth called')):
            self.assertEqual(self.train_call()[0], 0)
        batch = [e for e in fold.read_events() if e['type'] == 'queue-batch'][-1]
        self.assertEqual(batch['specs'].split(','), [f'L-spec-{9001+i}' for i in range(min(12, mq.window_gate.max_merges(os.environ)))])
        self.assertEqual(self.train_call(flying=[dict(window='active', specs=[self.sid])]), (3, []))
        pending = dict(self.event('queue-batch', 0, project='albert-scott', specs=self.sid), subject='window-pending')
        with (self.root / 'events' / 'L-executor-0001.jsonl').open('a') as stream:
            stream.write(json.dumps(pending) + '\n')
        state = self.root / 'state'
        state.mkdir(exist_ok=True)
        (state / 'window-run.window-pending.pid').write_text(str(os.getpid()))
        self.assertEqual(self.train_call(), (3, []))
        (state / 'window-run.window-pending.pid').unlink()
        import ast
        tree = ast.parse(Path(mq.__file__).read_text())
        forbidden = {'acquire', 'release', 'build_candidate', 'advance'}
        for node in ast.walk(tree):
            self.assertFalse(isinstance(node, ast.Name) and node.id in forbidden)
            self.assertFalse(isinstance(node, ast.Attribute) and node.attr in forbidden)
        self.train_mark(5, 'twelve ready specs under configured fifteen', 'in flight waits', 'reservation holds', 'no gate internals')

    def test_train_lock_and_detach(self):
        self.window_seed()
        state = self.root / 'state'
        state.mkdir()
        lock = state / 'window-run.albert-scott.lock'
        lock.write_text(str(os.getpid()))
        before = (self.root / 'events' / 'L-executor-0001.jsonl').read_bytes()
        self.assertEqual(self.train_call(), (3, []))
        self.assertEqual((self.root / 'events' / 'L-executor-0001.jsonl').read_bytes(), before)
        lock.write_text('2147483647')
        calls = []
        child = None
        def launcher(argv, **kw):
            nonlocal child
            self.assertTrue(lock.exists())
            self.assertTrue(kw['start_new_session'])
            self.assertEqual(kw['stderr'], subprocess.STDOUT)
            self.assertIn('window-run.window-', str(kw['stdout'].name))
            calls.append(argv)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            return child
        try:
            with patch.object(mq, '_repository', return_value=(self.root, 'main')), \
                 patch.object(mq, 'spec_paths', return_value=[('M', 'a.py')]):
                self.assertEqual(mq.window_run('albert-scott', detach=True, launcher=launcher, now=self.now), 0)
            self.assertIsNone(child.poll())
            self.assertFalse(lock.exists())
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.train_call(), (3, []))
        finally:
            if child:
                child.terminate()
                child.wait(timeout=5)
        with tempfile.TemporaryDirectory() as empty_root:
            result = subprocess.run(['bash', DOIT, 'window-run', '--project', 'none-such', '--detach'],
                                    env=dict(os.environ, DOIT_ROOT=empty_root), capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(list((Path(empty_root) / 'events').glob('*.jsonl')))
        self.assertFalse(lock.exists())
        self.train_mark(6, 'detach returns at once', 'lock held exits 3', 'lock held no events',
                        'stale lock taken', 'lock released after launch', 'cli detach arm')

    def test_train_concurrent(self):
        self.window_seed(12)
        # First child pauses inside the lock; second contends while it is held.
        # Its injected launcher starts a real living process, reserving the batch
        # before window-candidate exists, including on a later invocation.
        script = r'''import sys,os,time,subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import merge_queue as m
root = Path(os.environ['DOIT_ROOT'])
def depth(*a, **kw):
    (root/'entered').touch()
    while not (root/'continue').exists(): time.sleep(.01)
    return {'depth':2}
original=m.next_candidate
def head(events):
    depth()
    return original(events)
m.next_candidate=head
m._repository=lambda p:(root,'main')
m.spec_paths=lambda *a:[('M','same.py')]
def launch(*a,**kw):
    proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    (root/'child-pid').write_text(str(proc.pid))
    return proc
sys.exit(m.window_run('albert-scott', detach=True, launcher=launch))
'''
        env = dict(os.environ, DOIT_READ_CACHE='0')
        with (self.root / 'child.log').open('wb') as log:
            first = subprocess.Popen([sys.executable, '-c', script, str(Path(mq.__file__).parent)],
                                     env=env, stdout=log, stderr=log)
            import time
            deadline = time.monotonic() + 5
            while not (self.root / 'entered').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            try:
                self.assertTrue((self.root / 'entered').exists())
                second = subprocess.run([sys.executable, '-c', script, str(Path(mq.__file__).parent)],
                                        env=env, capture_output=True, timeout=5)
                self.assertEqual(second.returncode, 3, second.stderr)
                (self.root / 'continue').touch()
                self.assertEqual(first.wait(timeout=5), 0, (self.root / 'child.log').read_text())
                # Lock is now free but no candidate exists: reservation must hold.
                third = subprocess.run([sys.executable, '-c', script, str(Path(mq.__file__).parent)],
                                       env=env, capture_output=True, timeout=5)
                self.assertEqual(third.returncode, 3, third.stderr)
                batches = [e['specs'].split(',') for e in self.fresh_events() if e['type'] == 'queue-batch']
                self.assertEqual(len(batches), 1)
                self.assertEqual(len([s for b in batches for s in b]), len({s for b in batches for s in b}))
            finally:
                (self.root / 'continue').touch()
                if first.poll() is None:
                    first.terminate()
                first.wait(timeout=5)
                if (self.root / 'child-pid').exists():
                    os.kill(int((self.root / 'child-pid').read_text()), 15)
        self.train_mark(6, 'two concurrent no shared spec')

    def test_train_reconcile(self):
        self.window_seed(4)
        events = fold.read_events()
        events += [dict(self.event(kind, 1), subject=f'L-spec-{9001+i}')
                   for i, kind in enumerate(('shipped', 'window-dropped', 'merge-conflict'))]
        self.seed([{k: v for k, v in e.items() if k not in ('actor', '_src')} for e in events])
        self.train_call()
        self.train_call()
        removes = [e for e in fold.read_events() if e['type'] == 'queue-remove']
        self.assertEqual(len(removes), 3)
        self.assertEqual({e['subject'] for e in removes}, {'L-spec-9001', 'L-spec-9002', 'L-spec-9003'})
        self.window_seed()
        events = fold.read_events()
        events += [dict(self.event('window-candidate', 1, specs=[self.sid], sha='x', verified_on='b'), subject='window-c'),
                   dict(self.event('window-cancelled', 0, after='window-p', reason='rebase'), subject='window-c')]
        self.seed([{k: v for k, v in e.items() if k not in ('actor', '_src')} for e in events])
        self.assertEqual(self.train_call(flying=[dict(window='window-c', specs=[self.sid])], depth=1), (3, []))
        self.assertEqual(len(mq.queue(fold.read_events())), 2)
        # Terminal gate escalation without a spec outcome requeues all specs.
        events.append(dict(self.event('escalation-blocking', -1, reason='undetermined',
                                      default='retry', deadline='2099-01-01', revert='none'), subject='window-c'))
        self.seed([{k: v for k, v in e.items() if k not in ('actor', '_src')} for e in events])
        self.assertEqual(self.train_call()[0], 0)
        for code in (0, 1, 2, 3, 4):
            result, calls = self.train_call(code=code)
            self.assertEqual(result, code)
            self.assertEqual(len(calls), 1)
        self.seed([])
        self.assertEqual(self.train_call(), (3, []))
        self.train_mark(7, 'reconcile once', 'cancelled not outcome', 'undetermined requeued',
                        'exit passthrough', 'exit 3 only if none ran')

    def test_train_executor(self):
        text = (Path(DOIT).parent / 'agents' / 'executor.md').read_text()
        for phrase in ('one window at a time', 'Tick-owned: gate',
                       'doit queue list', 'batch_cap', 'risky', 'window-cancelled', 'queue head',
                       'NEW ', 'L-charter-0053 R5', 'footprint group'):
            self.assertIn(phrase, text)
        for phrase in ('at most 6', '--specs A,B,C', 'seq-'):
            self.assertNotIn(phrase, text)
        self.train_mark(8, 'executor train contract')


    def simple_mark(self, ac, body):
        encoded = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url=f'cli://merge-simple/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                              body_excerpt=body)))
        print(f'merge-simple AC{ac} status=0')

    def simple_emit(self, kind, subject=None, actor='executor', **fields):
        self.now += timedelta(seconds=1)
        dst = self.root / 'events' / f'L-{actor}-local.jsonl'
        self.assertIsNone(dispatch.emit(dst, {'subject': subject or self.sid}, kind, **fields))
        return self.fresh_events()

    def simple_add(self, sid, project='albert-scott'):
        return self.simple_emit('queue-add', sid, project=project, branch=sid.lower(), rule='operator')

    def test_next_candidate(self):
        self.assertIsNone(mq.next_candidate(self.fresh_events()))
        self.simple_add(self.sid)
        self.simple_add('L-spec-9002')
        events = self.simple_add('L-spec-9003')
        self.assertEqual(mq.next_candidate(events), self.sid)
        events = self.simple_emit('queue-remove', reason='operator', project='albert-scott')
        self.assertEqual(mq.next_candidate(events), 'L-spec-9002')
        events = self.simple_emit('shipped', 'L-spec-9002', sha='candidate')
        self.assertEqual(mq.next_candidate(events), 'L-spec-9003')
        with patch.object(mq.window_gate, 'in_flight', return_value=[dict(window='busy', specs=['L-spec-9003'])]):
            self.assertIsNone(mq.next_candidate(events))
        events = self.simple_emit('window-dropped', 'L-spec-9003', reason='new failure', window='w')
        self.assertIsNone(mq.next_candidate(events))
        listing = self.cli('queue', 'list', '--json')
        self.assertEqual(listing.returncode, 0)
        self.assertEqual(json.loads(listing.stdout), [])
        self.assertIn('omitted 2 spec(s)', listing.stderr)
        self.assertIn('shipped', listing.stderr)
        self.assertIn('window-dropped', listing.stderr)
        # Still round-trip through the writer and a separate reader; corrupt the
        # writer's clock, not an in-memory event supplied to the function.
        with patch.object(dispatch, 'now', return_value='not-a-time'):
            self.simple_add('L-spec-9004')
        with self.assertRaises(ValueError):
            mq.next_candidate(self.fresh_events())
        self.simple_mark(1, dict(empty=True, head=True, removed=True, shipped=True,
                                 dropped=True, in_flight=True, malformed_raises=True))

    def test_single_candidate(self):
        for sid in (self.sid, 'L-spec-9002', 'L-spec-9003'):
            self.simple_add(sid)
        head = mq.next_candidate(self.fresh_events())
        with patch.object(mq.window_gate, 'train_depth', side_effect=AssertionError('depth used')), \
             patch.object(mq, 'form_batch', wraps=mq.form_batch) as form:
            code, calls = self.train_call()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(form.called)  # window selection preserves priority/FIFO over overlap
        self.assertEqual(calls[0][calls[0].index('--specs') + 1].split(','),
                         [head, 'L-spec-9002', 'L-spec-9003'])
        self.assertNotIn('--after', calls[0])
        self.assertEqual(self.train_call(flying=[dict(window='busy', specs=[head])]), (3, []))
        with self.assertRaises(TypeError):
            mq.window_run('albert-scott', maximum=2)
        cli = self.cli('window-run', '--project', 'albert-scott', '--max', '2')
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn('unrecognized arguments', cli.stderr)
        self.simple_mark(2, dict(argv=calls[0], in_flight_launches=0, maximum_rejected=True))

    def test_batching_allowed(self):
        self.assertTrue(mq.batching_allowed(self.fresh_events(), 5))
        measurement = dict(single_p50_s=20, batched_p50_s=10, equal_input=True)
        for actor in ('builder', 'grader'):
            events = self.simple_emit('merge-measurement', actor=actor, **measurement)
            self.assertTrue(mq.batching_allowed(events, 5))
            with self.assertRaisesRegex(ValueError, 'unsupported train ledger actor'):
                mq._train_emit(f'L-{actor}-local.jsonl', 'window-denied', 'queue-batch',
                               project='albert-scott', specs=self.sid)
        for fields in (dict(measurement, equal_input=False), dict(measurement, batched_p50_s=20),
                       dict(measurement, batched_p50_s=30), dict(single_p50_s=20, equal_input=True),
                       dict(measurement, single_p50_s='20'), dict(measurement, single_p50_s=True),
                       dict(measurement, batched_p50_s=-1), dict(measurement, batched_p50_s=float('nan'))):
            events = self.simple_emit('merge-measurement', actor='operator', **fields)
            self.assertTrue(mq.batching_allowed(events, 5))
        events = self.simple_emit('merge-measurement', actor='operator', **measurement)
        self.assertFalse(mq.batching_allowed(events, mq.BATCH_MIN_QUEUE - 1))
        self.assertTrue(mq.batching_allowed(events, mq.BATCH_MIN_QUEUE))
        events = self.simple_emit('merge-measurement', actor='executor', **dict(measurement, equal_input=False))
        self.assertTrue(mq.batching_allowed(events, 5))
        self.simple_emit('merge-measurement', actor='executor', **measurement)
        for actor in ('builder', 'grader'):
            events = self.simple_emit('merge-measurement', actor=actor, **dict(measurement, batched_p50_s=1))
            self.assertTrue(mq.batching_allowed(events, 5))
        for i in range(5):
            self.simple_add(f'L-spec-{9001+i}')
        expected = [self.sid, 'L-spec-9002', 'L-spec-9003']
        config = self.root / 'capacity.toml'
        config.write_text('[merge_queue]\nbatch_cap=3\n')
        with patch.object(mq, '_look_toml', return_value=config), \
             patch.dict(os.environ, DOIT_WINDOW_MAX_MERGES='3'), \
             patch.object(mq, 'form_batch', wraps=mq.form_batch) as form:
            code, calls = self.train_call(config=config)
        self.assertEqual(code, 0)
        self.assertFalse(form.called)  # window selection preserves priority/FIFO over overlap
        self.assertEqual(calls[0][calls[0].index('--specs') + 1].split(','), expected)
        self.assertEqual([e['cap'] for e in self.fresh_events() if e['type'] == 'queue-batch'], [3])
        self.simple_mark(3, dict(measurement_independent=True, untrusted_ignored=True, invalid_ignored=True,
                                 minimum=mq.BATCH_MIN_QUEUE, selected=expected, cap_recorded=True))

    def test_tick_launch(self):
        import nextstep
        events = self.simple_add(self.sid)
        step = nextstep.Step(self.sid, 'spec', 'merging', 'action', 'merge', 'tick',
                             None, 'ready', None, events[-1]['ts'])
        calls = []
        def dispatcher(argv):
            calls.append(argv)
            return dict(code=0, stdout='', stderr='')
        dispatcher.events = events
        before = self.fresh_events()
        self.assertEqual(tick.launch(step, dispatcher=dispatcher), self.sid)
        self.assertEqual(calls, [['doit', 'window-run', '--project', 'albert-scott', '--detach']])
        self.assertEqual(before, self.fresh_events())
        calls.clear()
        outcomes = tick.run_steps(events, {}, self.now, dispatcher=dispatcher, steps=[step])
        self.assertEqual(calls, [['doit', 'window-run', '--project', 'albert-scott', '--detach']])
        self.assertTrue(any(line.startswith('acted ') for line in outcomes))
        with patch.object(tick, 'launch', side_effect=AssertionError('empty queue reached launcher')):
            outcomes = tick.run_steps([], {}, self.now, dispatcher=dispatcher, steps=[step])
        self.assertTrue(any('hold undetermined:' in line for line in outcomes))
        from dataclasses import replace
        calls.clear()
        log = io.StringIO()
        with redirect_stdout(log):
            self.assertIsNone(tick.launch(replace(step, item='L-spec-9999'), dispatcher=dispatcher))
            dispatcher.events = []
            self.assertIsNone(tick.launch(step, dispatcher=dispatcher))
        self.assertFalse(calls)
        self.assertIn('hold undetermined:', log.getvalue())
        for project in ('other', None, 'albert-scott'):
            if project is None:
                self.now += timedelta(seconds=1)
                with (self.root / 'events' / 'L-executor-local.jsonl').open('a') as stream:
                    stream.write(json.dumps(self.event('queue-add', branch=self.sid.lower(), rule='operator')) + '\n')
                evs = self.fresh_events()
            else:
                evs = self.simple_add(self.sid, project)
            specs = {self.sid: dict(state='reviewing', evs=evs)}
            steps = nextstep.next_steps(evs, specs, self.now, lane_rules.load())
            own = next(s for s in steps if s.item == self.sid)
            if project == 'albert-scott':
                self.assertEqual((own.action, own.owner), ('merge', 'tick'))
                self.assertEqual(nextstep.ACTIONS['merge'], 'tick')
            else:
                self.assertEqual(own.state, 'hold')
                self.assertTrue(own.reason.startswith('undetermined: project'), own)
        self.simple_mark(7, dict(argv=['doit', 'window-run', '--project', 'albert-scott', '--detach'],
                                 mismatch_launches=0, empty_launches=0, project_holds=True))

    def test_simple_retirement(self):
        for args in (('retired', 'check'), ('howto', '--check')):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repo = Path(DOIT).parent
        original = subprocess.run(['git', 'show', 'main:machine/crontab.txt'], cwd=repo,
                                  capture_output=True, text=True, check=True).stdout
        current = (repo / 'machine/crontab.txt').read_text()
        release = lambda text: [line for line in text.splitlines() if 'release-tick.sh' in line]
        self.assertTrue(release(current))
        self.assertEqual(release(current), release(original))
        self.assertNotIn('window-run', current)
        self.simple_mark(8, dict(retired_clean=True, howto_current=True, release_unchanged=True,
                                 cron_window_launches=0))

    def test_explicit_candidate_and_bypass(self):
        import window_gate
        with patch.object(window_gate, 'train_events', side_effect=AssertionError('ready read')):
            self.assertEqual(window_gate.main_(['w', '--project', 'albert-scott']), 2)
        with self.assertRaisesRegex(RuntimeError, 'missing candidate'):
            window_gate.resolve_entries(self.root, [], [])
        self.simple_add(self.sid)
        self.simple_add('L-spec-9002')
        with patch.dict(os.environ, DOIT_WINDOW_GATE='off'), \
             patch.object(window_gate, 'cleanup'), patch.object(window_gate, 'ev') as emit:
            self.assertEqual(window_gate.main_(['w', '--project', 'albert-scott']), 4)
            emit.assert_called_once_with('window-gate-bypassed', self.sid, window='w')
            emit.reset_mock()
            self.assertEqual(window_gate.main_(['w2', '--project', 'albert-scott', '--specs', 'explicit:branch']), 4)
            emit.assert_called_once_with('window-gate-bypassed', 'explicit', window='w2')
        self.simple_mark(5, dict(missing_refused=True, no_ledger_read=True, bypass_head=self.sid))


class GateToolingQueueTests(unittest.TestCase):
    setUp = QueueTests.setUp
    fixture = QueueTests.fixture
    git = QueueTests.git
    event = QueueTests.event
    landed = QueueTests.landed

    def prepare(self):
        self.now = self.now.replace(microsecond=0)
        self.fixture()
        self.events.insert(1, self.event('spawn-done', 95, spawn='L-spec-auditor-9001', bad_cut=False))
        actors = {'spec-written':'spec-writer', 'build-done':'builder', 'review':'reviewer', 'spawn-done':'spec-auditor'}
        for e in self.events:
            e.setdefault('project', 'proj')
            path = self.root / 'events' / ('L-'+actors[e['type']]+'-fixture.jsonl')
            with path.open('a') as stream:
                stream.write(json.dumps(e)+'\n')

    def reader(self, clock=None):
        code = '''import fold,json,nextstep,lane_rules,os
from dataclasses import asdict
from datetime import datetime
events=fold.read_events(use_cache=False)
now=datetime.fromisoformat(os.environ['FIXTURE_NOW'])
specs=fold.fold(events)[0]
print(json.dumps(dict(events=events,steps=[asdict(s) for s in nextstep.next_steps(events,specs,now,lane_rules.load())])))
'''
        env = dict(os.environ, DOIT_ROOT=str(self.root), FIXTURE_NOW=(clock or self.now).isoformat(),
                   PYTHONPATH=str(Path(__file__).parent.resolve()))
        return json.loads(subprocess.check_output([sys.executable,'-B','-c',code], env=env, text=True))

    def child(self, argv, *, cwd, env):
        self.calls.append(argv)
        code = '''import sys,merge_gate,fold,os
from datetime import datetime
from unittest.mock import patch
fold.NOW=datetime.fromisoformat(os.environ['FIXTURE_NOW'])
with patch.object(merge_gate,'secret_scan',return_value=('undetermined',['tool'])):
    sys.exit(merge_gate.main_(sys.argv[1:]))
'''
        proc = subprocess.run([sys.executable,'-B','-c',code,*argv[2:]], cwd=cwd,
                              env=dict(env, PYTHONPATH=str(Path(__file__).parent.resolve()), FIXTURE_NOW=self.now.isoformat()),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stdout+proc.stderr)
        return dict(code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def signed(self, name, body):
        raw = json.dumps(body,sort_keys=True).encode()
        print(json.dumps(dict(url='cli://L-spec-1869/AC2/'+name,status=0,
                              body_sha256=hashlib.sha256(raw).hexdigest(),body_excerpt=body)))

    def test_child_event_fresh_reader_and_due_launch(self):
        import nextstep
        self.prepare()
        out = mq.enqueue_due(self.events, self.now, runner=self.child)
        self.assertEqual(len(self.calls), 1, out)
        recovered = self.reader()
        outcomes = [e for e in recovered['events'] if e['type']=='merge-gate-undetermined']
        self.assertEqual(len(outcomes),1, recovered)
        self.assertFalse(any(e['type']=='autodispatch-failed' for e in recovered['events']))
        self.assertTrue(any('tooling' in line for line in out), out)
        outcome = outcomes[0]
        self.assertEqual(outcome['cycle'], self.events[-1]['ts'])
        self.assertTrue(outcome['retryable'])
        due = datetime.fromisoformat(outcome['retry_at'])
        for seconds in (-1,0,1):
            replay = self.reader(due+timedelta(seconds=seconds))
            step = next(s for s in replay['steps'] if s['item']==self.sid)
            self.assertEqual(step['action'], 'gate' if seconds>=0 else None, replay)
            if seconds>=0:
                self.assertEqual(step['since'],outcome['ts'])
        snapshot = recovered['events']
        rows = [r for r in lane_rules.due(snapshot,fold.fold(snapshot)[0],due) if r['rule']=='gate']
        self.assertEqual(len(rows),1)
        self.assertEqual(lane_rules.ladder(snapshot,rows[0],due),'act')
        self.now = due
        mq.enqueue_due(snapshot,due,runner=self.child)
        self.assertEqual(len(self.calls),2)
        again = self.reader()
        self.assertEqual(len([e for e in again['events'] if e['type']=='merge-gate-undetermined']),2)
        mq.enqueue_due(again['events'],due,runner=self.child)
        self.assertEqual(len(self.calls),2)
        self.signed('child-roundtrip', dict(first=outcome, boundaries=[-1,0,1], gate_launches=2,
                                          output=out, autodispatch_failed=0))

    def test_persisted_order_refusals_and_live_owner(self):
        import nextstep
        self.prepare()
        outcome = self.event('merge-gate-undetermined', 10, reason='tool', repo=str(self.repo/'.git'),
                             sha='a'*40,branch_sha='a'*40,main_sha='b'*40,branch=self.sid.lower(),
                             cycle=self.events[-1]['ts'],attempt='one',retryable=True,
                             retry_at=(self.now-timedelta(minutes=5)).isoformat(),hold_reason=None,findings=[])
        prior = self.event('autodispatched',10,rule='gate',role='gate',since=self.events[-1]['ts'],trigger='retry')
        history = self.events+[prior,outcome]
        calls = []
        def runner(*args,**kw):
            calls.append(args)
            return dict(code=2,stdout='tooling fixture',stderr='')
        mq.enqueue_due(history,self.now,runner=runner)
        self.assertEqual(len(calls),1)
        tied_old = prior | dict(since=outcome['ts'])
        mq.enqueue_due(self.events+[tied_old,outcome],self.now+timedelta(hours=2),runner=runner)
        self.assertEqual(len(calls),2)
        calls.clear()
        calls.append('first')
        consumed = self.event('autodispatched',10,rule='gate',role='gate',since=outcome['ts'])
        mq.enqueue_due(history+[consumed],self.now,runner=runner)
        self.assertEqual(len(calls),1)
        # Time alone cannot spend the same persisted outcome again, including
        # when an old lane timeout would otherwise offer a dispatch retry.
        for trigger in ('act', 'retry'):
            dispatched = consumed | dict(trigger=trigger)
            mq.enqueue_due(history+[dispatched],self.now+timedelta(hours=2),runner=runner)
            self.assertEqual(len(calls),1, trigger)
        for reason in ('lock','duplicate','dispatch-in-progress'):
            refusal = self.event('spawn-failed',9,role='gate',reason=reason)
            mq.enqueue_due(history+[consumed,refusal],self.now,runner=runner)
        self.assertEqual(len(calls),4)
        refused_retry = consumed | dict(trigger='retry')
        refused = self.event('spawn-failed',9,role='gate',reason='lock')
        mq.enqueue_due(history+[refused_retry,refused],self.now+timedelta(hours=2),runner=runner)
        self.assertEqual(len(calls),5)
        live = self.event('spawn-started',0,role='executor',spawn='L-executor-9002')
        mq.enqueue_due(history+[live],self.now,runner=runner)
        self.assertEqual(len(calls),5)
        # A delayed tick still anchors the ladder at the latest failure.
        delayed = self.event('autodispatched',90,rule='gate',since=self.events[-1]['ts'],trigger='retry')
        row = next(r for r in lane_rules.due(self.events+[delayed,outcome],fold.fold(history)[0],self.now) if r['rule']=='gate')
        self.assertEqual(lane_rules.ladder(history,row,self.now),'act')
        self.signed('ordering',dict(preceding_dispatch='eligible',following_dispatch='consumed',refusals=3,live_owner='held'))

    def test_exit_statuses_and_runner_errors_remain_distinct(self):
        self.prepare()
        observations = []
        for code in (0, 1, 2, 13, None):
            with self.subTest(code=code):
                before = len(self.landed())
                def runner(*args, **kwargs):
                    if code is None:
                        raise OSError('synthetic runner error')
                    return dict(code=code, stdout='synthetic gate result', stderr='')
                output = mq.enqueue_due(self.events, self.now, runner=runner)
                appended = self.landed()[before:]
                failed = [e for e in appended if e['type'] == 'autodispatch-failed']
                self.assertEqual(len(failed), int(code not in (0, 2)), output)
                self.assertTrue(any(('gated' if code == 0 else 'tooling' if code == 2
                                     else 'failed') in line for line in output), output)
                observations.append(dict(code=code, failed=len(failed), output=output))
        self.signed('exit-statuses', observations)


class _RedcutCase(unittest.TestCase):
    setUp = QueueTests.setUp
    seed = QueueTests.seed
    event = QueueTests.event
    fresh_events = QueueTests.fresh_events
    separate_queue = QueueTests.separate_queue

    def signed(self, criterion, body):
        body = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/redcut/' + criterion, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def redcut_fixture(self):
        specs = [f'L-spec-{9001+i}' for i in range(4)]
        def row(kind, subject, minute, **fields):
            return dict(type=kind, subject=subject, project='p',
                        ts=f'2026-10-31T10:{minute:02d}:00Z', **fields)
        adds = [row('queue-add', sid, i+1, branch=branch, rule='operator')
                for i, (sid, branch) in enumerate(zip(specs, 'abcu'))]
        batch = row('queue-batch', 'W', 5, specs=','.join(specs[:3]))
        drop = row('window-dropped', specs[0], 6, window='W', reason='owner',
                   evidence='local-repro', nodes=['src/test_example.py::test_red'])
        close = row('window-redcut', 'W', 7, window='W', candidate='1'*40, base='0'*40,
                    dropped=specs[:1], returned=specs[1:3], flakes=[], residual=[],
                    first_red_at=drop['ts'], closed_at='2026-10-31T10:07:00Z',
                    outcome='handled-red', automatic=True)
        expected = [dict(spec=e['subject'], project='p', branch=e['branch'],
                         added_at=e['ts'], batch=None) for e in adds[1:]]
        return adds + [batch, drop], close, expected

    def snapshot(self):
        code = '''import fold, merge_queue as m, json
events=fold.read_events(use_cache=False)
_,_,ignored,subjects=fold.fold(events)
print(json.dumps(dict(queue=m.queue(events,'p'),launching=m._launching(events,'p',[]),
raw=[e for e in events if e['type'] in ('window-redcut','window-flake')],
ignored=[e for e in ignored if e['type'] in ('window-redcut','window-flake')],
admitted=[e for rows in subjects.values() for e in rows if e['type'] in ('window-redcut','window-flake')],
metrics=m.redcut_metrics(events,project='p',since='2026-10-31T10:00:00Z',until='2026-10-31T11:00:00Z'))))'''
        p = subprocess.run(['/usr/bin/python3', '-c', code], cwd=Path(mq.__file__).parent,
                           env=dict(os.environ, DOIT_READ_CACHE='0'), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)


class RedcutQueueTests(_RedcutCase):
    test_rows = QueueTests.test_redcut_retains_exact_queue_rows_and_position
    test_terminals = QueueTests.test_redcut_does_not_revive_terminal_or_removed_work

    def test_literal_replay_and_new_branch(self):
        events, close, expected = self.redcut_fixture()
        before = json.dumps(events, sort_keys=True)
        self.assertEqual(mq.queue(events+[close], 'p'), expected)
        replay = dict(close, ts='2026-10-31T10:09:00Z')
        self.assertEqual(mq.queue(events+[close, replay], 'p'), expected)
        successor = dict(events[-2], subject='W2', ts='2026-10-31T10:08:00Z',
                         specs=','.join(close['returned']))
        linked = [dict(e, batch='W2') if e['spec'] in close['returned'] else e for e in expected]
        self.assertEqual(mq.queue(events+[close, successor, replay], 'p'), linked)
        new = dict(events[1], branch='b2', ts='2026-10-31T10:10:00Z')
        self.assertEqual(mq.queue(events+[close, successor, new, replay], 'p'),
                         linked[1:] + [dict(expected[0], branch='b2', added_at=new['ts'])])
        self.assertEqual(json.dumps(events, sort_keys=True), before)
        equal = dict(close, ts=events[-2]['ts'])
        self.assertEqual(mq.queue(events+[equal], 'p'), expected)
        for disposition in (dict(close, residual=['unknown']), dict(close, returned=[]),
                            dict(close, returned=[close['returned'][0]])):
            result = mq.queue(events+[disposition], 'p')
            self.assertEqual([r['added_at'] for r in result], [r['added_at'] for r in expected])
        self.signed('AC3-rows', dict(before=events, after=expected, successor=linked))

    def launch_setup(self):
        events, close, expected = self.redcut_fixture()
        self.now = datetime(2026, 10, 31, 10, 10, tzinfo=timezone.utc)
        self.seed(events+[close])
        for name, value in (('_repository', (self.root, 'main')),
                            ('spec_paths', [('M', 'safe.py')])):
            p = patch.object(mq, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        # Distinct footprints preserve the configured selector's exclusion rules.
        p = patch.object(mq, 'spec_paths', side_effect=lambda repo, main, branch: [('M', branch+'.py')])
        p.start()
        self.addCleanup(p.stop)
        return close

    def test_handoff_cleanup_deduplication_and_retry(self):
        close = self.launch_setup()
        calls = []
        def launch(argv, **kw):
            calls.append(argv)
            self.assertEqual(self.snapshot()['queue'][0]['branch'], 'b')
            with mq._run_lock('p') as owned:
                self.assertFalse(owned)
            # A racing ordinary launcher cannot reserve a second successor.
            self.assertEqual(mq.window_run('p', detach=True, launcher=launch, now=self.now), 3)
            return type('Child', (), {'pid': os.getpid()})()
        with mq.window_gate.chain_lock('W', 'p', self.root) as holder:
            self.assertIsNone(holder)
            self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 3)
        self.assertTrue(mq.window_gate.acquire('W'))
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 3)
        mq.window_gate.release()
        def refuse(*args, **kwargs):
            raise OSError('fixture launch refusal')
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=refuse, now=self.now), 3)
        failed_id = [e['subject'] for e in self.fresh_events() if e.get('redcut_of') == 'W'][0]
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 0)
        self.assertEqual(calls[0][2], failed_id)
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 0)
        self.assertEqual(len(calls), 1)
        # A durable successor terminal remains deduplicated after its PID is gone.
        fields = {k:v for k,v in close.items() if k not in ('type','subject','ts')}
        mq._train_emit('L-executor-0001.jsonl', failed_id, 'window-redcut',
                       **dict(fields, window=failed_id, returned=[], dropped=[]))
        (self.root/'state'/f'window-run.{failed_id}.pid').unlink()
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 0)
        self.assertEqual(len(calls), 1)
        self.signed('AC3-handoff', dict(launches=len(calls), successor=failed_id, retry_same_id=True))

    def test_holds_and_exit_five(self):
        close = self.launch_setup()
        calls = []
        launch = lambda *a, **kw: calls.append(a)
        self.assertEqual(mq.redcut_handoff('p', 'missing', launcher=launch), 3)
        with patch.object(mq, 'merge_freeze', return_value='fixture freeze'):
            self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 3)
        self.assertEqual(calls, [])
        events, close, _ = self.redcut_fixture()
        self.seed(events+[dict(close, residual=['unattributed'])])
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 3)
        self.seed([])
        self.assertEqual(mq.redcut_handoff('p', 'W', launcher=launch, now=self.now), 3)

    def test_disposition_variants_and_pending_siblings(self):
        events, close, expected = self.redcut_fixture()
        for returned in ([], close['returned'][:1], close['returned']):
            with self.subTest(returned=returned):
                disposition = dict(close, returned=returned, residual=['unattributed'])
                rows = mq.queue(events+[disposition], 'p')
                self.assertEqual(rows, [dict(r, batch=None if r['spec'] in returned or
                                            r['spec']=='L-spec-9004' else 'W') for r in expected])
        bisected = dict(type='window-bisected',subject='W',project='p',
                        ts='2026-10-31T10:06:30Z',after='0'*40,left=['L-spec-9002'],right=['L-spec-9003'])
        partial = dict(close,returned=['L-spec-9002'],residual=['unattributed'])
        self.assertEqual(mq.queue(events+[bisected,partial], 'p'),
                         [expected[0],dict(expected[1],batch='W'),expected[2]])
        # Entire drop includes unrelated pending work, or an actually empty queue.
        drops = [dict(events[-1],subject=s) for s in close['returned']]
        entire = dict(close,dropped=close['dropped']+close['returned'],returned=[])
        self.assertEqual(mq.queue(events+drops+[entire],'p'),expected[-1:])
        self.assertEqual(mq.queue(events[:3]+events[4:]+drops+[entire],'p'),[])
        self.signed('AC3-dispositions',dict(partial=True,entire=True,bisected=True,residual=True))

    def test_synchronous_exit_five_after_real_cleanup(self):
        close = self.launch_setup()
        events, _, _ = self.redcut_fixture()
        self.seed(events[:4])
        trace = []
        def successor(argv, **kw):
            self.assertTrue(kw['start_new_session'])
            self.assertIsNone(mq.window_gate.lock_holder(0,self.root))
            with mq.window_gate.chain_lock('successor','p',self.root) as holder:
                self.assertIsNone(holder)
            trace.append('successor')
            return type('Child',(),{'pid':os.getpid(),
                                    'wait':lambda child: self.fail('recursive successor wait')})()
        def handled(argv):
            name = argv[2]
            with mq.window_gate.chain_lock(name,'p',self.root) as holder:
                self.assertIsNone(holder)
                self.assertTrue(mq.window_gate.acquire(name))
                try:
                    mq._train_emit('L-executor-0001.jsonl','L-spec-9001','window-dropped',
                                   project='p',window=name,reason='owner',evidence='local-repro',nodes=['literal'])
                    fields = {k:v for k,v in close.items() if k not in ('type','subject','ts')}
                    mq._train_emit('L-executor-0001.jsonl',name,'window-redcut',**dict(fields,window=name))
                    self.assertEqual(mq.redcut_handoff('p',name,launcher=successor,now=self.now),3)
                    self.assertEqual(trace,[])
                finally:
                    mq.window_gate.release()
                trace.append('cleanup')
            trace.append('chain-release')
            return dict(code=5)
        self.assertEqual(mq.window_run('p',runner=handled,launcher=successor,now=self.now),5)
        self.assertEqual(trace,['cleanup','chain-release','successor'])
        landed = self.fresh_events()
        self.assertEqual(sum(e['type']=='queue-add' for e in landed),4)
        links = [e for e in landed if e['type']=='queue-batch' and e.get('redcut_of')]
        self.assertEqual(len(links),1)
        self.assertFalse(any(e['type'] in ('window-verified','shipped','escalation-blocking') for e in landed))
        self.signed('AC3-exit-five',dict(exit=5,trace=trace,links=links))

    def test_existing_exit_meanings_and_bounded_holds(self):
        self.launch_setup()
        events, close, _ = self.redcut_fixture()
        for code in (0,1,2,3,4):
            self.seed(events[:4])
            with patch.object(mq,'redcut_handoff',side_effect=AssertionError('unexpected handoff')):
                self.assertEqual(mq.window_run('p',runner=lambda argv:dict(code=code),now=self.now),code)
        calls = []
        self.seed([close])  # committed closure, genuinely no queued work
        self.assertEqual(mq.redcut_handoff('p','W',launcher=lambda *a,**k:calls.append(a),now=self.now),3)
        self.seed(events+[close])
        with mq._run_lock('p') as owned:
            self.assertTrue(owned)
            self.assertEqual(mq.redcut_handoff('p','W',launcher=lambda *a,**k:calls.append(a),now=self.now),3)
        self.assertEqual(calls,[])
        self.assertFalse(any(e['type']=='escalation-blocking' for e in self.fresh_events()))
        self.signed('AC3-status-holds',dict(exits=[0,1,2,3,4],empty=True,contention=True,launches=0))


class RedcutLedgerMetricsTests(_RedcutCase):
    test_lists = QueueTests.test_redcut_list_fields_accept_empty_but_refuse_missing_or_nonlist
    test_admission = QueueTests.test_redcut_admission_requires_actor_and_fields

    def metric_fixture(self):
        def row(kind, subject, time, **fields):
            return dict(type=kind, subject=subject, project='p', actor='executor',
                        ts='2026-10-31T'+time+':00Z', **fields)
        def close(window, first, closed, dropped, returned):
            return row('window-redcut', window, closed, window=window, candidate='1'*40, base='0'*40,
                       dropped=dropped, returned=returned, flakes=[], residual=[],
                       first_red_at='2026-10-31T'+first+':00Z', closed_at='2026-10-31T'+closed+':00Z',
                       outcome='handled-red', automatic=True)
        return [row('queue-batch','W1','09:59',specs='L-spec-9001,L-spec-9002'),
                close('W1','10:00','10:10',['L-spec-9001'],['L-spec-9002']),
                row('queue-batch','W2','10:11',specs='L-spec-9002',redcut_of='W1'),
                row('window-opened','W2','10:14',specs='L-spec-9002'),
                row('queue-batch','W3','10:14',specs='L-spec-9003'),
                close('W3','10:15','10:20',['L-spec-9003'],[]),
                row('queue-batch','W4','10:24',specs='L-spec-9004'),
                row('window-opened','W4','10:25',specs='L-spec-9004'),
                row('window-dropped','L-spec-9004','10:30',window='W4',nodes=['src/test_example.py::test_red'],reason='owner')]

    def metrics(self, events, **kw):
        return mq.redcut_metrics(events, project='p', since='2026-10-31T10:00:00Z',
                                 until=kw.get('until','2026-10-31T11:00:00Z'))

    def test_literal_metrics_and_decision_intervals(self):
        rows = self.metric_fixture()
        expected = dict(automatic_closed=2, red_closed=3, without_thinker_decision=2,
                        red_to_next_open_minutes=[dict(window='W1',next_window='W2',minutes=14.0)],
                        pending_next_open=['W3'], excluded_invalid=[])
        self.assertEqual(self.metrics(rows), expected)
        self.assertEqual(self.metrics(rows+rows), expected)
        for actor in ('thinker','operator','builder'):
            for subject in ('W1','L-spec-9002','unrelated'):
                for time in ('09:59','10:00','10:05','10:10','10:11'):
                    decision = dict(type='decision',subject=subject,actor=actor,why='inspect failed window',
                                    ts='2026-10-31T'+time+':00Z')
                    wanted = 1 if actor != 'builder' and subject != 'unrelated' and '10:00' <= time <= '10:10' else 2
                    self.assertEqual(self.metrics(rows+[decision]), dict(expected,without_thinker_decision=wanted))
        matching = [dict(e,redcut_of='W1') if e['type']=='window-opened' and e['subject']=='W2' else e for e in rows]
        self.assertEqual(self.metrics(matching), expected)
        no_join = [dict(e,redcut_of='unknown') if e['type']=='queue-batch' and e['subject']=='W2' else e for e in matching]
        self.assertEqual(self.metrics(no_join)['pending_next_open'], ['W1','W3'])
        later = [dict(e,ts='2026-10-31T11:01:00Z') if e['type']=='window-opened' and e['subject']=='W2' else e for e in rows]
        self.assertEqual(self.metrics(later)['pending_next_open'], ['W1','W3'])
        offset = [dict(e,ts=e['ts'].replace('Z','+00:00')) for e in rows]
        self.assertEqual(self.metrics(offset), expected)
        other = [dict(e,project='other',subject='other-'+e['subject']) for e in rows]
        self.assertEqual(self.metrics(rows+other), expected)
        self.assertEqual(self.metrics(rows,until='2026-10-31T10:20:00Z')['automatic_closed'],1)
        self.signed('AC5-metrics', expected)

    def test_invalid_metric_evidence(self):
        rows = self.metric_fixture()
        variants = []
        for field, value in (('closed_at','bad'),('closed_at',None),('closed_at','2026-10-31T10:10:00'),
                             ('closed_at','2026-10-31T09:59:00Z'),('first_red_at',None),('ts','bad')):
            variants.append([dict(e,**{field:value}) if e['type']=='window-redcut' and e['subject']=='W1' else e for e in rows])
        for fields in (dict(redcut_of='W3'),dict(ts='2026-10-31T09:59:00Z'),dict(project='other')):
            variants.append([dict(e,**fields) if e['type']=='window-opened' and e['subject']=='W2' else e for e in rows])
        variants.append(rows+[dict(rows[2],subject='conflicting')])
        for variant in variants:
            result = self.metrics(variant)
            self.assertEqual(result['excluded_invalid'],['W1'])
            self.assertEqual(result['automatic_closed'],1)
        # Non-owner closure evidence is not a human-assisted red closure.
        for kind in ('merge-conflict','escalation-blocking','window-bisected'):
            self.assertEqual(self.metrics(rows[:-1]+[dict(rows[-1],type=kind)])['red_closed'],2)

    def test_production_writes_and_separate_process(self):
        for actor in ('executor','tick'):
            with self.subTest(actor=actor):
                for path in (self.root/'events').glob('*.jsonl'):
                    path.unlink()
                events, close, expected = self.redcut_fixture()
                ledger = f'L-{actor}-redcut.jsonl'
                def emit(e):
                    fields={k:v for k,v in e.items() if k not in ('type','subject','ts','actor')}
                    with patch.object(dispatch,'now',return_value=e['ts']):
                        mq._train_emit(ledger,e['subject'],e['type'],**fields)
                for e in events+[close]:
                    emit(e)
                self.assertEqual(self.snapshot()['queue'],expected)
                self.assertEqual(self.snapshot()['launching'],[])
                successor=dict(events[-2],subject='W2',ts='2026-10-31T10:08:00Z',
                               specs=','.join(close['returned']),redcut_of='W')
                emit(successor)
                emit(dict(close,ts='2026-10-31T10:09:00Z'))
                self.assertEqual([e['batch'] for e in self.snapshot()['queue']],['W2','W2',None])
                self.signed('AC5-ledger-'+actor,self.snapshot())
                # Production ev uses the actor from its configured filename.
                with patch.dict(os.environ,DOIT_GATE_LEDGER_FILE=ledger):
                    mq.window_gate.ev('window-flake','W',window='W',project='p',candidate='1'*40,
                                      node='src/test_example.py::test_red',isolated_state='green',
                                      evidence=dict(snapshot='1'*40,selector='literal',outcome='green',report='scratch'),scope='window-run')
                self.assertTrue(any(e['type']=='window-flake' for e in self.fresh_events()))
                self.assertEqual(sum(e['type']=='queue-add' for e in self.fresh_events()),4)

    def test_wrapper_admission_and_serializable_bad_lists(self):
        events, close, _ = self.redcut_fixture()
        self.seed(events)
        (self.root/'state').mkdir(exist_ok=True)
        (self.root/'state'/'window-run.W.pid').write_text(str(os.getpid()))
        baseline = self.snapshot()
        fields={k:v for k,v in close.items() if k not in ('type','subject','ts')}
        flake=dict(window='W',project='p',candidate='1'*40,node='literal',isolated_state='green',
                   evidence=dict(snapshot='1'*40,selector='literal',outcome='green',report='scratch'),scope='window-run')
        for kind, supplied in (('window-redcut',fields),('window-flake',flake)):
            for actor in ('builder','grader','reviewer','thinker','operator'):
                ledger=f'L-{actor}-unauthorized.jsonl'
                path=self.root/'events'/ledger
                path.touch()
                before=path.read_bytes()
                with self.assertRaises(ValueError):
                    mq._train_emit(ledger,'W',kind,**supplied)
                self.assertEqual(path.read_bytes(),before)
                self.assertIsNotNone(dispatch.emit(path,dict(subject='W'),kind,strict=True,**supplied))
                self.assertEqual(path.read_bytes(),before)
                diagnostic = io.StringIO()
                with redirect_stderr(diagnostic):
                    self.assertIsNone(dispatch.emit(path,dict(subject='W'),kind,actor='executor',**supplied))
                self.assertIn('recorded-and-ignored',diagnostic.getvalue())
                direct = path.read_bytes()
                self.assertNotEqual(direct,before)
                with patch.dict(os.environ,DOIT_GATE_LEDGER_FILE=ledger), redirect_stderr(diagnostic):
                    mq.window_gate.ev(kind,'W',**supplied)
                self.assertNotEqual(path.read_bytes(),direct)
                self.assertEqual(diagnostic.getvalue().count('recorded-and-ignored'),2)
                snapshot = self.snapshot()
                raw = [e for e in snapshot['raw'] if e['type']==kind and e['actor']==actor]
                self.assertEqual(len(raw),2)  # raw read_events retains both append attempts
                self.assertTrue(all(e in snapshot['ignored'] for e in raw))
                self.assertEqual(snapshot['admitted'],[])
                for key in ('queue','launching','metrics'):
                    self.assertEqual(snapshot[key],baseline[key],(actor,kind,key))
        path=self.root/'events'/'L-executor-malformed.jsonl'
        path.touch()
        for field in ('dropped','returned','flakes','residual'):
            missing = dict(fields)
            missing.pop(field)
            for bad in [missing]+[dict(fields,**{field:value}) for value in (None,'node',{},0,False)]:
                for strict in (True,False):
                    before=path.read_bytes()
                    self.assertIsNotNone(dispatch.emit(path,dict(subject='W'),'window-redcut',strict=strict,**bad))
                    self.assertEqual(path.read_bytes(),before)
        self.assertIsNotNone(fold.check_append(dict(type='window-flake',subject='W',**dict(flake,evidence={})), 'executor'))
        self.assertEqual(self.snapshot()['metrics']['automatic_closed'],0)
        self.signed('AC5-admission',dict(unauthorized_actors=5,wrappers=4,malformed_fields=4))


class RedcutRegressionTests(_RedcutCase):
    """Explicit-only paired inventory; children run their ordinary entry once."""
    REFERENCE = '54f306695efa4ee9d2bc122b32ff9387fc98d3be'
    PATHS = ('src/test_merge_queue.py','src/test_fold.py',
             'src/test_ledger_files.py','src/test_rechain.py')

    def test_paired_subprocess_inventory(self):
        repo = Path(__file__).resolve().parent.parent
        # An allowlist removes seat, ledger, configuration, Git, Python and
        # credential overrides equally from both runs. Report key names only.
        keep = {'PATH','LANG','LC_ALL','LC_CTYPE','TZ','TMPDIR'}
        removed = sorted(set(os.environ)-keep)
        clean = {k:v for k,v in os.environ.items() if k in keep}
        clean.update(PYTHONDONTWRITEBYTECODE='1',DOIT_NO_POKE='1')
        self.assertIn('DOIT_LEDGER_FILE',removed)
        records = []
        with tempfile.TemporaryDirectory(prefix='redcut-regression-') as temporary:
            scratch = Path(temporary)
            trees = {}
            try:
                for label, revision in (('reference',self.REFERENCE),('candidate','HEAD')):
                    tree = scratch/label
                    p = subprocess.run(['git','worktree','add','--detach',str(tree),revision],
                                       cwd=repo,env=clean,capture_output=True,text=True,timeout=60)
                    self.assertEqual(p.returncode,0,p.stderr)
                    trees[label] = tree
                # Also exercise the current tracked edits when run before amend.
                for path in ('src/merge_queue.py','src/fold_emits.py','src/fold.py','src/test_merge_queue.py'):
                    (trees['candidate']/path).write_bytes((repo/path).read_bytes())
                counts = {label:len((tree/'src/fold.py').read_text().splitlines())
                          for label,tree in trees.items()}
                self.assertEqual(counts['reference'],2572)
                self.assertEqual(counts['candidate']-counts['reference'],3)
                for path in self.PATHS:
                    pair = []
                    for label, tree in trees.items():
                        isolated = scratch/(label+'-'+Path(path).stem)
                        isolated.mkdir()
                        for name in ('home','root','xdg','tmp'):
                            (isolated/name).mkdir()
                        env = dict(clean,HOME=str(isolated/'home'),DOIT_ROOT=str(isolated/'root'),
                                   XDG_CONFIG_HOME=str(isolated/'xdg'),TMPDIR=str(isolated/'tmp'))
                        command = ['/usr/bin/python3',path]
                        p = subprocess.run(command,cwd=tree,env=env,capture_output=True,text=True,timeout=240)
                        summary = re.search(r'Ran (\d+) tests? in ',p.stderr)
                        failures = re.findall(r'^(?:FAIL|ERROR): (.+)$',p.stderr,re.M)
                        output = p.stdout+p.stderr
                        record = dict(path=path,tree=label,reference_sha=self.REFERENCE,
                                      command=command,exit=p.returncode,
                                      executed_cases=int(summary[1]) if summary else None,
                                      assertion_progress=p.stdout.splitlines()[-4:],
                                      failures=failures,diagnostics=p.stderr,
                                      output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                                      removed_environment_keys=removed,fold_lines=counts[label],
                                      narrow_grant_delta=counts['candidate']-counts['reference'])
                        records.append(record)
                        pair.append(record)
                        # Print each completed subprocess even if a later row fails.
                        self.signed('AC6-run',record)
                        self.assertNotIn('skipped=',p.stderr)
                    with self.subTest(path=path):
                        reference, candidate = pair
                        if path == 'src/test_fold.py':
                            identity = 'assert line_count <= 2465, line_count'
                            self.assertEqual(reference['exit'],1)
                            for row in pair:
                                if row['exit'] == 0:
                                    continue
                                self.assertEqual(row['exit'],1)
                                self.assertIn(identity,row['diagnostics'])
                                self.assertEqual(row['diagnostics'].count('Traceback (most recent call last):'),1)
                                self.assertRegex(row['diagnostics'].strip(),r'AssertionError: \d+$')
                                self.assertEqual(row['failures'],[])
                            # This admits the known assertion site only, never claims
                            # that any later module-level assertions were executed.
                        else:
                            self.assertGreater(reference['executed_cases'] or 0,0)
                            self.assertGreater(candidate['executed_cases'] or 0,0)
                            self.assertEqual(candidate['exit'],0,candidate['diagnostics'])
                            self.assertEqual(candidate['failures'],[])
                            if path == 'src/test_merge_queue.py':
                                self.assertEqual(reference['exit'],1)
                                allowed = {'test_window_batching_caps','test_train_caps','test_train_step',
                                           'test_ac1_fill_to_cap','test_ac5_fill_safety'}
                                self.assertEqual(len(reference['failures']),6)
                                self.assertEqual({f.split()[0] for f in reference['failures']},allowed)
                                safety = [f for f in reference['failures'] if f.startswith('test_ac5_fill_safety ')]
                                self.assertEqual(len(safety),2)
                                for name in ('test_window_batching_caps','test_train_caps'):
                                    self.assertTrue(any("safety_regression='"+name+"'" in f for f in safety))
                            else:
                                self.assertEqual(reference['exit'],0,reference['diagnostics'])
            finally:
                for tree in trees.values():
                    p = subprocess.run(['git','worktree','remove','--force',str(tree)],cwd=repo,
                                       env=clean,capture_output=True,text=True,timeout=60)
                    self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(len(records),8)
        self.signed('AC6',dict(inventory=list(self.PATHS),runs=8,recursive=False,records=records))


class RehearsalLauncherTests(unittest.TestCase):
    """Real Git/persistence coverage of the stacked admission step."""

    def setUp(self):
        QueueTests.setUp(self)
        self.repo = self.root / 'repos' / 'albert-scott'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.specs = [f'L-spec-{9500 + i}' for i in range(9)]
        events = []
        for index, sid in enumerate(self.specs):
            branch = sid.lower()
            self.git('switch', '-qc', branch, self.base)
            (self.repo / f'{index}.txt').write_text(sid + '\n')
            self.git('add', '.')
            self.git('commit', '-qm', sid)
            (self.root / 'content' / (sid + '.md')).write_text(f'Writes: {index}.txt\n')
            events.append(dict(v=1, type='queue-add', subject=sid, actor='executor',
                               ts=self.now.isoformat(), project='albert-scott', branch=branch))
        self.git('switch', '-q', 'master')
        QueueTests.seed(self, events)
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, DOIT_MAIN='master', DOIT_WINDOW_RECHAIN='off',
                                           DOIT_REHEARSAL_BATCHES='3', DOIT_READ_CACHE='0',
                                           DOIT_REHEARSAL_LAUNCHER='on'))

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=fixture',
                                 '-c', 'user.email=fixture@localhost', *args],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fresh(self):
        result = subprocess.run([sys.executable, '-c',
            'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_stacked_refill_persists_441_and_workers_consume_prepared_candidates(self):
        import window_rehearse
        calls, verified = [], []
        def verify(repo, candidate, required, ident, *, rehearsal=False):
            import test_slots
            self.assertTrue(rehearsal)
            self.assertEqual(test_slots.held_count(self.root), 1)
            verified.append(candidate)
            return dict(state='green', nodes=[], url='fixture://ci/' + ident, doc={})
        def runner(argv):
            calls.append(argv)
            # The real worker must consume the parent's immutable preparation.
            with patch.object(window_rehearse, 'prepare', side_effect=AssertionError('candidate rebuilt')):
                self.assertEqual(mq.window_gate.main_(argv[2:]), 0)
            return dict(code=0)
        with patch.object(mq.window_gate, 'required_checks', return_value=['fixture']), \
             patch.object(mq.window_gate, 'scope', return_value=dict(checks=['fixture'])), \
             patch.object(mq.window_gate, 'verify', side_effect=verify):
            started = mq._refill_rehearsals('albert-scott', runner=runner, cap=4)
        batches = mq._rehearsal_batches(self.fresh(), 'albert-scott')
        self.assertEqual(len(started), 3)
        self.assertEqual([len(row['specs'].split(',')) for row in batches], [4, 4, 1])
        base = self.base
        for index, row in enumerate(batches):
            self.assertEqual(row['base_sha'], base)
            self.assertEqual(row['receipt']['base_sha'], base)
            self.assertEqual(row['receipt']['sha'], verified[index])
            self.assertEqual(row['receipt']['state'], 'green')
            base = verified[index]
        self.assertEqual([sid for batch in batches for sid in batch['specs'].split(',')], self.specs)
        self.assertEqual(mq._refill_rehearsals('albert-scott', runner=runner, cap=4), [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.git('rev-parse', 'master'), self.base)
        # Real worker receipts must retain enough identity for the ordinary
        # launcher; synthetic reports with extra fields hid this handoff gap.
        code, launches = self.tick()
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 1)
        self.assertNotIn('--rehearse', launches[0])
        self.assertEqual(launches[0][2], batches[0]['subject'])
        body = json.dumps(dict(calls=calls, batches=batches), sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/C22/stacked-admission', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_prepared_candidate_rejects_changed_branch_tree_base_and_actor(self):
        import window_rehearse
        entries = mq.window_gate.resolve_entries(self.repo, [self.specs[0] + ':' + self.specs[0].lower()], [])
        ident = 'window-binding'
        candidate = window_rehearse.prepare(self.repo, ident, self.base, entries, 'albert-scott', 'master', [])
        receipt = dict(type='window-rehearsed', subject=ident, project='albert-scott', actor='executor',
                       ts=self.now.isoformat(), state='prepared', launcher_prepared=True,
                       base_sha=self.base, sha=candidate, tree=mq.window_gate.tree_of(self.repo, candidate),
                       specs=[entries[0][0]], entries=[list(entry) for entry in entries])
        self.assertEqual(window_rehearse.prepared_candidate(
            self.repo, ident, 'albert-scott', self.base, entries, [receipt]), candidate)
        for change in (dict(tree='0' * 40), dict(base_sha=candidate), dict(entries=[]),
                       dict(specs=[]), dict(sha='missing'), dict(state='green')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                window_rehearse.prepared_candidate(self.repo, ident, 'albert-scott', self.base,
                                                   entries, [dict(receipt, **change)])
        for change in (dict(actor='builder'), dict(project='other'), dict(subject='other')):
            self.assertIsNone(window_rehearse.prepared_candidate(
                self.repo, ident, 'albert-scott', self.base, entries, [dict(receipt, **change)]))

    def test_C22_AC2(self):
        calls = []
        def launcher(argv, **kwargs):
            calls.append(argv)
            self.assertTrue(kwargs['start_new_session'])
            return type('Worker', (), dict(pid=os.getpid()))()
        started = mq._refill_rehearsals('albert-scott', launcher=launcher, cap=4)
        inventory = mq._rehearsal_batches(self.fresh(), 'albert-scott')
        self.assertEqual(len(started), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([len(batch['taken']) for batch in inventory], [4, 4, 1])
        # Exercise the existing append/admission path for a reported red and
        # its matching disposition. Launcher result consumption is a separate
        # contract; the board must distinguish reported from actually parked.
        last = inventory[-1]
        parked = last['taken'][0]
        mq._train_emit('L-executor-0001.jsonl', last['subject'], 'window-rehearsed',
                       project='albert-scott', base_sha=last['base_sha'], sha=last['sha'],
                       specs=last['taken'], state='red', rehearsal=True,
                       attribution=dict(proven={parked: ['fixture::failure']},
                                        drop={parked: ['fixture::failure']}))
        mq._train_emit('L-executor-0001.jsonl', parked, 'queue-remove', project='albert-scott',
                       window=last['subject'], reason='attributed rehearsal red')
        result = subprocess.run([sys.executable, str(Path(fold.__file__)), 'board'],
                                env=dict(os.environ, DOIT_PROJECT='albert-scott'),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        section = result.stdout.split('## MERGE LANE\n', 1)[1].split('\n\n', 1)[0]
        base = self.base
        for batch in inventory:
            self.assertEqual(batch['base_sha'], base)
            self.assertIn(batch['subject'], section)
            self.assertIn('candidate=' + batch['sha'], section)
            self.assertIn('specs=' + batch['specs'], section)
            self.assertIn('chain=' + base + ' -> ' + batch['sha'], section)
            self.assertIn('stacked-on=' + base, section)
            base = batch['sha']
        self.assertIn('rehearsal=prepared', section)
        self.assertIn('rehearsal=red', section)
        for label in ('ATTRIBUTED', 'FALLBACK'):
            self.assertIn(label + ' ' + parked, section)
        self.assertIn('disposition=parked', section)
        self.assertIn('reason=attributed rehearsal red', section)
        body = json.dumps(dict(inventory=inventory, rendered=section), sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/C22/AC2', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


    def emit(self, ident, kind, **fields):
        mq._train_emit('L-executor-0001.jsonl', ident, kind, project='albert-scott', **fields)

    def batches(self, project='albert-scott'):
        return mq._rehearsal_batches(self.fresh(), project)

    def report(self, batch, state, **fields):
        self.emit(batch['subject'], 'window-rehearsed', base_sha=batch['base_sha'], sha=batch['sha'],
                  specs=batch['specs'].split(','), state=state, rehearsal=True,
                  entries=batch['receipt']['entries'], **fields)

    def add_spec(self, project, sid, files, start):
        repo = self.root / 'repos' / project
        self.git('switch', '-qc', sid.lower(), start)
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', sid)
        (self.root / 'content' / (sid + '.md')).write_text('Writes: ' + ', '.join(files) + '\n')
        self.git('switch', '-q', 'master')
        return dict(v=1, type='queue-add', subject=sid, actor='executor', ts=self.now.isoformat(),
                    project=project, branch=sid.lower())


    def tick(self, **kwargs):
        calls = []
        with patch.dict(os.environ, DOIT_WINDOW_MAX_MERGES='4'):
            code = mq.window_run('albert-scott', runner=lambda argv: calls.append(argv) or dict(code=0), **kwargs)
        return code, calls

    def test_C22_AC1(self):
        code, calls = self.tick()
        batches = self.batches()
        self.assertEqual(code, 0)
        self.assertEqual([len(b['specs'].split(',')) for b in batches], [4, 4, 1])
        base = self.base
        for batch, argv in zip(batches, calls):
            self.assertEqual((batch['base_sha'], argv[argv.index('--on') + 1]), (base, base))
            base = batch['sha']
        self.assertEqual(len(calls), 3)
        # Capacity: a further tick starts nothing while three batches are in flight.
        self.assertIsNone(mq.refill_candidate(self.fresh(), 'albert-scott'))
        self.assertEqual(self.tick()[1], [])
        self.assertEqual(len(self.batches()), 3)
        # Green: the first batch launches its ordinary window on the next tick.
        self.report(batches[0], 'green', tree=batches[0]['receipt']['tree'])
        # Capacity must not suppress the tick step that consumes a verdict.
        from nextstep import Step
        step = Step(self.specs[0], 'spec', 'merging', 'action', 'merge', 'tick',
                    'queue-add', 'condition met', None, self.now.isoformat())
        events = self.fresh()
        launches = []
        def dispatcher(argv):
            launches.append(argv)
            return dict(code=0, stdout='', stderr='')
        dispatcher.events = events
        owner = self.root / 'state' / ('window-run.' + batches[0]['subject'] + '.pid')
        owner.write_text(str(os.getpid()))
        outcomes = tick.run_steps(events, {}, self.now, dispatcher=dispatcher, steps=[step])
        owner.unlink()
        self.assertEqual(launches, [['doit', 'window-run', '--project', 'albert-scott', '--detach']])
        self.assertTrue(any(line.startswith('acted ') for line in outcomes), outcomes)
        code, calls = self.tick()
        self.assertEqual(code, 0)
        self.assertEqual([a[2] for a in calls], [batches[0]['subject']])
        self.assertNotIn('--rehearse', calls[0])
        self.assertEqual(calls[0][calls[0].index('--specs') + 1].split(',')[0], 'L-spec-9500:l-spec-9500')
        (self.root / 'state' / ('window-run.' + batches[0]['subject'] + '.pid')).write_text(str(os.getpid()))
        self.assertEqual(self.tick()[1], [])
        # ATTRIBUTED red on the second batch parks the culprit, voids its stacked
        # successor and re-forms one batch on the newest surviving candidate.
        culprit = batches[1]['specs'].split(',')[0]
        self.report(batches[1], 'red', attribution=dict(proven={culprit: ['n::a']}, drop={culprit: ['n::a']}))
        self.report(batches[0], 'green', tree=batches[0]['receipt']['tree'])
        code, calls = self.tick()
        self.assertEqual(code, 0)
        after = self.batches()
        self.assertEqual([b['subject'] for b in after][0], batches[0]['subject'])
        formed = after[1:]
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0]['base_sha'], batches[0]['sha'])
        remaining = [s for s in self.specs if s != culprit and s not in batches[0]['specs'].split(',')]
        self.assertEqual(formed[0]['specs'].split(','), remaining[:4])
        queued = [row['spec'] for row in mq.queue(self.fresh(), 'albert-scott')]
        self.assertNotIn(culprit, queued)
        self.assertEqual(sorted(queued), sorted(s for s in self.specs if s != culprit))
        # FALLBACK-only attribution gets the same treatment.
        victim = formed[0]['specs'].split(',')[1]
        self.report(formed[0], 'red', attribution=dict(proven={}, drop={victim: ['n::b']}))
        self.tick()
        self.assertNotIn(victim, [row['spec'] for row in mq.queue(self.fresh(), 'albert-scott')])
        self.assertTrue(all(victim not in b['specs'] for b in self.batches()))
        # Unattributed red is never clean or parked: the batch is held.
        held = self.batches()[-1]
        self.report(held, 'red')
        self.tick()
        self.assertIn(held['subject'], [b['subject'] for b in self.batches()])
        body = json.dumps(dict(calls=calls, batches=after), sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/C22/AC1', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body[:300])))

    def test_C22_AC1_safety_cases(self):
        project = 'safety'
        self.repo = self.root / 'repos' / project
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        start = self.git('rev-parse', 'HEAD')
        v = 'api/alembic_supabase/versions/'
        rows = [self.add_spec(project, 'L-spec-9601', {'shared.txt': 'a\n'}, start),
                self.add_spec(project, 'L-spec-9602', {'shared.txt': 'b\n'}, start),
                self.add_spec(project, 'L-spec-9603', {v + 'm1.py': 'x = 1\n'}, start),
                self.add_spec(project, 'L-spec-9604', {v + 'm2.py': 'x = 2\n'}, start),
                self.add_spec(project, 'L-spec-9605', {'clash': 'file\n'}, start),
                self.add_spec(project, 'L-spec-9606', {'clash/inner.txt': 'dir\n'}, start)]
        QueueTests.seed(self, [dict(row, project=project) for row in rows])
        mq._refill_rehearsals(project, runner=lambda argv: None, cap=6)
        formed = [b['taken'] for b in self.batches(project)]
        flat = [sid for taken in formed for sid in taken]
        for sid in ('L-spec-9601', 'L-spec-9602', 'L-spec-9603', 'L-spec-9604', 'L-spec-9605', 'L-spec-9606'):
            self.assertLessEqual(flat.count(sid), 1)
        for taken in formed:
            self.assertFalse({'L-spec-9601', 'L-spec-9602'} <= set(taken), 'shared file in one batch')
            self.assertFalse({'L-spec-9603', 'L-spec-9604'} <= set(taken), 'two migrations in one batch')
            self.assertFalse({'L-spec-9605', 'L-spec-9606'} <= set(taken), 'unclean merge on stacked base')
        self.assertLessEqual(len(formed), 3)

    def test_C22_AC3(self):
        import threading
        self.assertFalse(mq.local_prefilter_enabled())
        trace, started, lock = [], threading.Event(), threading.Lock()
        def launcher(argv, **kwargs):
            with lock:
                trace.append(('ci-start', argv[2]))
            started.set()
            return type('W', (), dict(pid=os.getpid() + 1))()
        def local(repo, run, candidate, members):
            with lock:
                trace.append(('local-start', run['batch']))
            self.assertTrue(started.wait(5), 'local ran before CI dispatch: CI would have been delayed')
            with lock:
                trace.append(('local-done', run['batch']))
            return self.verdicts[run['batch']]
        cancels = []
        # Default: no local pass at all, CI dispatches immediately.
        self.verdicts = {}
        mq._refill_rehearsals('albert-scott', launcher=launcher, cap=4, cancel=cancels.append)
        self.assertEqual([t[0] for t in trace], ['ci-start'] * 3)
        self.assertEqual(cancels, [])
        # Environment selects N and the local switch.
        with patch.dict(os.environ, DOIT_LOCAL_PREFILTER='on', DOIT_REHEARSAL_BATCHES='2'):
            self.assertTrue(mq.local_prefilter_enabled())
            import window_rehearse
            self.assertEqual(window_rehearse.capacity(), 2)
        with patch.dict(os.environ, DOIT_LOCAL_PREFILTER='maybe'):
            with self.assertRaises(ValueError):
                mq.local_prefilter_enabled()
        # Enabled: local overlaps CI. Reset to a clean ledger of unbatched ready specs.
        first = self.batches()
        for batch in first:
            mq._release_batch('L-executor-0001.jsonl', 'albert-scott', batch['subject'],
                              batch['specs'].split(','), [], 'fixture reset', candidate=batch['sha'])
        trace.clear()
        started.clear()
        def launcher2(argv, **kwargs):
            trace.append(('ci-start', argv[2]))
            started.set()
            return type('W', (), dict(pid=os.getpid() + 1))()
        class Verdicts(dict):
            def __missing__(self, key):
                return 'green'
        self.verdicts = Verdicts()
        green = self.tick_local(launcher2, local, cancels)
        self.assertEqual(cancels, [], 'local green must cancel nothing')
        self.assertEqual(sorted(t[0] for t in trace), ['ci-start'] * 3 + ['local-done'] * 3 + ['local-start'] * 3)
        for batch in green:
            self.assertLess(trace.index(('ci-start', batch)), trace.index(('local-done', batch)))
        # Local red on the second batch cancels exactly that run and its stacked successor.
        for batch in self.batches():
            mq._release_batch('L-executor-0001.jsonl', 'albert-scott', batch['subject'],
                              batch['specs'].split(','), [], 'fixture reset', candidate=batch['sha'])
        trace.clear()
        started.clear()
        cancels.clear()
        probe = []
        def local_red(repo, run, candidate, members):
            probe.append(run['batch'])
            started.wait(5)
            return 'red' if len(probe) == 2 else 'green'
        self.tick_local(launcher2, local_red, cancels)
        second = [t[1] for t in trace if t[0] == 'ci-start'][1]
        self.assertEqual([c['batch'] for c in cancels], [second, [t[1] for t in trace][2]])
        self.assertNotIn([t[1] for t in trace][0], [c['batch'] for c in cancels])
        queued = [row['spec'] for row in mq.queue(self.fresh(), 'albert-scott')]
        parked = self.batches()
        self.assertEqual([b['subject'] for b in parked][0], [t[1] for t in trace][0])
        redcuts = [e for e in self.fresh() if e['type'] == 'window-redcut' and e.get('outcome') == 'rehearsal-reformed']
        self.assertEqual(sorted(e['subject'] for e in redcuts if e['dropped']), [second])
        removed = [e['subject'] for e in self.fresh() if e['type'] == 'queue-remove' and e.get('window') == second]
        self.assertEqual(len(removed), 4)
        self.assertTrue(set(removed).isdisjoint(queued))
        body = json.dumps(dict(trace=trace, cancels=cancels), sort_keys=True)
        print(json.dumps(dict(url='cli://doit-v2/C22/AC3', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body[:300])))

    def tick_local(self, launcher, local, cancels):
        mq._refill_rehearsals('albert-scott', launcher=launcher, cap=4, local=local, cancel=cancels.append)
        return [b['subject'] for b in self.batches()]


if __name__ == '__main__':
    if sys.argv[1:] == ['--spec-1869-ac2']:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(GateToolingQueueTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print(f'L-spec-1869 AC2 queue: {result.testsRun} executed; success={result.wasSuccessful()}')
        sys.exit(0 if result.testsRun and result.wasSuccessful() else 1)
    if not sys.argv[1:]:
        # AC6 must be explicitly selected: its candidate child invokes this
        # ordinary entry, which must not recursively start the paired harness.
        sys.argv.extend(['QueueTests','GateToolingQueueTests','RedcutQueueTests','RedcutLedgerMetricsTests',
                         'RehearsalLauncherTests'])
    unittest.main()
