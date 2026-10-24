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
        for code in (0, 1, 2, 3, 4):
            self.window_seed(7)
            def runner(argv):
                batch = [e for e in fold.read_events() if e['type'] == 'queue-batch'][-1]
                self.assertEqual(argv[2], batch['subject'])
                self.assertEqual(len(batch['specs'].split(',')), 1)
                return dict(code=code)
            with patch.object(mq, '_repository', return_value=(self.root, 'main')), \
                 patch.object(mq, 'spec_paths', return_value=[('M', 'file.py')]):
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
        with patch.object(mq, '_repository', return_value=(self.root, 'main')), patch.object(mq, 'spec_paths', return_value=[('M', 'file.py')]):
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
                                 'import fold,json; print(json.dumps(fold.read_events()))'],
                                cwd=Path(mq.__file__).parent, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_train_caps(self):
        config = self.root / 'look.toml'
        config.write_text('[merge_queue]\nbatch_cap = 9\nbatch_cap_floor = 6\n')
        def cap(rate=None, windows=0, events=(), path=config, env=None):
            with patch.object(mq.window_gate, 'red_rate', return_value=dict(rate=rate, windows=windows)):
                return mq.effective_cap(list(events), self.now, env={} if env is None else env, toml_path=path)
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
            self.assertEqual(mq.effective_cap([], self.now, {}, config)['cap'], 6)
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
        import tomllib
        table = tomllib.loads((Path(DOIT).parent / 'look.toml').read_text())['merge_queue']
        self.assertEqual(table, dict(batch_cap=10, batch_cap_floor=6))
        self.train_mark(4, 'config', 'unreadable floor', 'rate none many windows floor',
                        'few windows keep newest', 'few windows configured', 'floor on red', 'boundary 0.40',
                        'configured when calm', 'max_merges clamp', 'record once', 'unchanged silent',
                        'roundtrip fresh process', 'wrong actor ignored', 'emits row', 'look.toml keys')

    def train_call(self, *, flying=(), depth=2, code=0, **kwargs):
        calls = []
        def runner(argv):
            self.assertTrue(any(e['type'] == 'queue-batch' and e['subject'] == argv[2] for e in fold.read_events()))
            calls.append(argv)
            return dict(code=code)
        with patch.object(mq.window_gate, 'in_flight', return_value=list(flying)), \
             patch.object(mq, '_repository', return_value=(self.root, 'main')), \
             patch.object(mq, 'spec_paths', return_value=[('M', 'shared.py')]):
            result = mq.window_run('albert-scott', runner=runner, now=self.now, **kwargs)
        return result, calls

    def test_train_step(self):
        self.window_seed(12)
        with patch.object(mq.window_gate, 'train_depth', side_effect=AssertionError('depth called')):
            self.assertEqual(self.train_call()[0], 0)
        batch = [e for e in fold.read_events() if e['type'] == 'queue-batch'][-1]
        self.assertEqual(batch['specs'], self.sid)
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
        self.train_mark(5, 'single head', 'in flight waits', 'reservation holds', 'no gate internals')

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
             patch.object(mq, 'effective_cap', side_effect=AssertionError('cap used')), \
             patch.object(mq, 'form_batch', side_effect=AssertionError('batch used')):
            code, calls = self.train_call()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index('--specs') + 1], head)
        self.assertNotIn('--after', calls[0])
        self.assertEqual(self.train_call(flying=[dict(window='busy', specs=[head])]), (3, []))
        with self.assertRaises(TypeError):
            mq.window_run('albert-scott', maximum=2)
        cli = self.cli('window-run', '--project', 'albert-scott', '--max', '2')
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn('unrecognized arguments', cli.stderr)
        self.simple_mark(2, dict(argv=calls[0], in_flight_launches=0, maximum_rejected=True))

    def test_batching_allowed(self):
        self.assertFalse(mq.batching_allowed(self.fresh_events(), 5))
        measurement = dict(single_p50_s=20, batched_p50_s=10, equal_input=True)
        for actor in ('builder', 'grader'):
            events = self.simple_emit('merge-measurement', actor=actor, **measurement)
            self.assertFalse(mq.batching_allowed(events, 5))
        for fields in (dict(measurement, equal_input=False), dict(measurement, batched_p50_s=20),
                       dict(measurement, batched_p50_s=30), dict(single_p50_s=20, equal_input=True),
                       dict(measurement, single_p50_s='20'), dict(measurement, single_p50_s=True),
                       dict(measurement, batched_p50_s=-1), dict(measurement, batched_p50_s=float('nan'))):
            events = self.simple_emit('merge-measurement', actor='operator', **fields)
            self.assertFalse(mq.batching_allowed(events, 5))
        events = self.simple_emit('merge-measurement', actor='operator', **measurement)
        self.assertFalse(mq.batching_allowed(events, mq.BATCH_MIN_QUEUE - 1))
        self.assertTrue(mq.batching_allowed(events, mq.BATCH_MIN_QUEUE))
        events = self.simple_emit('merge-measurement', actor='executor', **dict(measurement, equal_input=False))
        self.assertFalse(mq.batching_allowed(events, 5))
        self.simple_emit('merge-measurement', actor='executor', **measurement)
        for actor in ('builder', 'grader'):
            events = self.simple_emit('merge-measurement', actor=actor, **dict(measurement, batched_p50_s=1))
            self.assertTrue(mq.batching_allowed(events, 5))
        for i in range(5):
            self.simple_add(f'L-spec-{9001+i}')
        expected = [self.sid, 'L-spec-9002', 'L-spec-9003']
        with patch.dict(os.environ, DOIT_WINDOW_MAX_MERGES='3'), \
             patch.object(mq, 'form_batch', wraps=mq.form_batch) as form:
            code, calls = self.train_call()
        self.assertEqual(code, 0)
        self.assertTrue(form.called)
        self.assertEqual(calls[0][calls[0].index('--specs') + 1].split(','), expected)
        self.assertEqual([e['cap'] for e in self.fresh_events() if e['type'] == 'batch-cap'], [3])
        self.simple_mark(3, dict(authorised=True, untrusted_ignored=True, invalid_disabled=True,
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


if __name__ == '__main__':
    unittest.main()
