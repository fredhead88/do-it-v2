#!/usr/bin/env python3
"""Hermetic queue regressions: real ledger admission, git gates and CLI writes."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
                                   'DOIT_LEDGER_FILE': 'L-executor-0001.jsonl'})
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
        self.seed(events)

    def test_window_launch_and_exit_codes(self):
        self.window_seed(7)
        for code in (0, 1, 2, 3, 4):
            def runner(argv):
                events = fold.read_events()
                batch = [e for e in events if e['type'] == 'queue-batch'][-1]
                self.assertEqual(batch['subject'], argv[2])
                self.assertRegex(argv[2], r'^[A-Za-z0-9._-]+$')
                self.assertEqual(argv, ['doit', 'gate-window', batch['subject'], '--project',
                                        'albert-scott', '--specs', ','.join(f'L-spec-{9001+i}' for i in range(6))])
                return dict(code=code)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(mq.window_run('albert-scott', runner=runner), code)
            if code == 3:
                self.assertIn('window-run: another window holds the lock', output.getvalue())
            self.assertEqual(len(mq.queue(fold.read_events())), 7)
        before = sum(len(p.read_text().splitlines()) for p in (self.root / 'events').glob('*.jsonl'))
        self.assertEqual(mq.window_run('other', runner=self.fake), 1)
        self.assertEqual(mq.window_run('albert-scott', 7, runner=self.fake), 1)
        self.assertEqual(sum(len(p.read_text().splitlines()) for p in (self.root / 'events').glob('*.jsonl')), before)
        self.seed([])
        self.assertEqual(mq.window_run('albert-scott', runner=self.fake), 0)
        self.assertEqual(self.calls, [])
        self.mark(7, 'batch-before-gate', 'empty', 'wrong-project', 'exit-3 keeps queue', 'max-cap', 'exit passthrough')

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
        self.assertEqual(mq.window_run('albert-scott', runner=runner), 1)
        evs = fold.read_events()
        removes = [e for e in evs if e['type'] == 'queue-remove']
        self.assertEqual(len(removes), 3)
        self.assertEqual({e['subject']: e['reason'] for e in removes},
                         {'L-spec-9001': 'shipped', 'L-spec-9002': 'window-dropped', 'L-spec-9003': 'merge-conflict'})
        self.assertTrue(all(e.get('window', '').startswith('window-') for e in removes))
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
        self.assertEqual({r['spec'] for r in mq.queue([clean, add])},
                         {s for s, b in window_gate.pending_from_ledger([clean, add])})
        self.assertEqual(mq.queue([clean]), [])
        self.assertTrue(window_gate.pending_from_ledger([clean]))
        conflict = [clean, add, self.event('merge-conflict', 1)]
        with patch.object(lane_rules, 'due', return_value=[]):
            mq.enqueue_due(conflict, self.now)
        self.assertEqual(mq.queue(conflict + self.landed()[-1:]), [])
        self.assertTrue(window_gate.pending_from_ledger(conflict))
        self.fixture()
        self.git('branch', 'realm')
        (self.root / 'content' / (self.sid + '.md')).write_text('Base: realm\n')
        self.clean('albert-scott')
        (self.root / 'repos' / 'albert-scott').symlink_to(self.repo, target_is_directory=True)
        before = len(self.landed())
        self.due('enqueue')
        self.assertFalse(any(e['type'] == 'queue-add' for e in self.landed()[before:]))
        self.assertTrue(window_gate.pending_from_ledger(self.events))
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
        sequence = []
        def driver(*a, **kw):
            sequence.append('driver')
            return ['driver line']
        def queue(*a, **kw):
            sequence.append('queue')
            self.assertIs(kw['runner'], self.fake_runner)
            self.assertTrue(kw['dry_run'])
            return ['queue line']
        self.fake_runner = self.fake
        with patch.object(autodispatch, 'candidates', return_value=[]), \
             patch.object(autodispatch, 'grader_candidates', return_value=[]), \
             patch.object(autodispatch, 'dead_spawns', side_effect=lambda *a: sequence.append('replay') or []), \
             patch('lane_driver.run', side_effect=driver), patch.object(mq, 'enqueue_due', side_effect=queue) as call:
            out = autodispatch.run([], {}, self.now, runner=self.fake_runner, dry_run=True)
            self.assertEqual(out, ['driver line', 'queue line'])
            self.assertEqual(sequence, ['replay', 'driver', 'queue'])
            self.assertEqual(call.call_count, 1)
        with patch.object(autodispatch, 'candidates', return_value=[]), \
             patch.object(autodispatch, 'grader_candidates', return_value=[]), \
             patch.object(autodispatch, 'dead_spawns', return_value=[]), \
             patch('lane_driver.run', return_value=['earlier']), \
             patch.object(mq, 'enqueue_due', side_effect=ValueError('fixture failure')):
            out = autodispatch.run([], {}, self.now, runner=self.fake)
            self.assertEqual(out[0], 'earlier')
            self.assertEqual(self.landed()[-1]['role'], 'queue')
        self.mark(10, 'called once after driver', 'same runner', 'dry-run propagated', 'queue error contained')

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
        for phrase in ('Tick-owned: gate', 'doit window-run --project albert-scott', 'doit queue list'):
            self.assertIn(phrase, text)
        for phrase in ('--specs A,B,C', 'Run `doit gate` per branch', 'doit gate <branch> <main> --spec', 'seq-'):
            self.assertNotIn(phrase, text)
        result = self.cli('help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('doit window-run', result.stdout)
        self.assertIn('doit queue', result.stdout)
        self.mark(11, 'executor tick ownership ok', 'queue and window help ok')


if __name__ == '__main__':
    unittest.main()
