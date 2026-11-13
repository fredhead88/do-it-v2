#!/usr/bin/env python3
"""Tick-owned Planner scheduling, reaping and bounded continuations (L-spec-2310), scratch only."""
import ast
import inspect
import atexit
import fcntl
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_ISOLATION = tempfile.TemporaryDirectory(prefix='tick-launch-import-')
atexit.register(_ISOLATION.cleanup)
os.environ.update(HOME=_ISOLATION.name, DOIT_ROOT=_ISOLATION.name, DOIT_NO_POKE='1')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dispatch, fold, panes, relay, up
import tick

# The combined verification imports test_tick, whose legacy script stubs these
# process-wide boundaries. Planner tests always exercise their real entrypoints.
_PLANNER_BOUNDARIES = ((up, 'ensure_planner', up.ensure_planner),
                       (tick.pane_resume, 'run', tick.pane_resume.run))


class Scratch(unittest.TestCase):
    def setUp(self):
        for module, name, real in _PLANNER_BOUNDARIES:
            self.enterContext(patch.object(module, name, real))
        root = self.enterContext(tempfile.TemporaryDirectory(prefix='tick-launch-'))
        self.root = pathlib.Path(root)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.enterContext(patch.dict(os.environ, HOME=root, DOIT_ROOT=root, DOIT_PROJECT=''))
        for obj, key, val in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.events),
                              (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.events),
                              (panes, 'SESSIONS', self.sessions)):
            self.enterContext(patch.object(obj, key, val))
        self.now = datetime.now(timezone.utc)
        self.cli = self.root / 'claude'
        self.cli.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(120)\n')
        self.cli.chmod(0o700)
        self.enterContext(patch.dict(os.environ, DOIT_CLAUDE_BIN=str(self.cli)))
        self.visibility = self.enterContext(patch.object(up, '_planner_host_namespace', return_value='fixture-host'))
        self.scan = self.enterContext(patch.object(up, '_planner_processes', return_value=[]))

    def start(self, number=9001, **fields):
        stem = f'L-planner-{number}'
        path = self.events / f'{stem}.jsonl'
        base = dict(ts=(self.now-timedelta(hours=2)).isoformat())
        mode = fields.pop('mode', 'charter')
        with patch.object(dispatch, 'now', return_value=base['ts']):
            self.assertIsNone(dispatch.emit(path, {}, 'planner-started', subject='L-charter-9001',
                                          planner=stem, mode=mode, **fields))
        return next(e for e in fold.read_events(use_cache=False)
                    if e["_src"] == path.name+":1")

    def readback(self):
        result = subprocess.run(['/usr/bin/python3', '-c',
            'import json,fold; print(json.dumps(fold.read_events(use_cache=False)))'],
            cwd=pathlib.Path(__file__).resolve().parent, env=dict(os.environ),
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def approve(self, rows):
        target = self.root / 'state/planner-reap-approval.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(dict(pid_namespace='fixture-host', starts=[
            dict(planner=e['planner'], started_at=e['ts']) for e in rows
            if e['type']=='planner-started' and 'wrapper_pid' not in e])))
        target.chmod(0o600)
        return target

    def ended(self, rows):
        return [e for e in rows if e['type']=='planner-ended']

    def file_charters(self, *charters):
        (self.root / 'repos' / 'do-it-v2').mkdir(parents=True, exist_ok=True)
        for charter in charters:
            self.assertIsNone(dispatch.emit(self.events / 'L-operator-9001.jsonl', {}, 'charter-filed',
                                            subject=charter, project='do-it-v2'))
        return fold.read_events(use_cache=False)

    def failed_run(self, number, charter='L-charter-9001', reason='exit-1', **end):
        stem = f'L-planner-{number}'
        path = self.events / f'{stem}.jsonl'
        self.assertIsNone(dispatch.emit(path, {}, 'planner-started', subject=charter,
                                        planner=stem, mode='charter', wrapper_pid=os.getpid()))
        self.assertIsNone(dispatch.emit(path, {}, 'planner-ended', subject=charter,
                                        planner=stem, reason=reason, **end))
        return fold.read_events(use_cache=False)

    def ensure(self, rows, result='started', now=None, **kwargs):
        launches, queries = [], []
        real = relay.plannable
        def spy(*args, **kw):
            queries.append(args)
            return real(*args, **kw)
        def launch(*args, **kw):
            launches.append((args, kw))
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(up, 'launch_planner', side_effect=launch), \
             patch.object(relay, 'plannable', side_effect=spy):
            status = up.ensure_planner(rows, now or self.now, **kwargs)
        return status, launches, queries


class AC1(Scratch):
    A, B = 'L-charter-9001', 'L-charter-9002'

    def test_queue_matrix(self):
        sentinel = object()
        with self.subTest(case='empty'):
            status, launches, queries = self.ensure(self.file_charters(), launcher=sentinel)
            self.assertEqual((status, launches, len(queries), up.LAST_PLANNER_READY), ('idle', [], 1, []))
        rows = self.file_charters(self.A)
        with self.subTest(case='one'):
            status, launches, queries = self.ensure(rows, launcher=sentinel)
            self.assertEqual(status, 'started')
            self.assertEqual(launches, [((self.A, 'charter', 1), {'launcher': sentinel, 'now': self.now})])
            self.assertEqual((len(queries), up.LAST_PLANNER_READY), (1, [self.A]))
        rows = self.file_charters(self.B)
        with self.subTest(case='two'):
            status, launches, queries = self.ensure(rows)
            self.assertEqual([args for args, _ in launches], [(self.A, 'charter', 1)])
            self.assertEqual((len(queries), up.LAST_PLANNER_READY), (1, [self.A, self.B]))
        with self.subTest(case='held charter'):
            with (self.events / 'L-planner-9901.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(v=1, ts=self.now.isoformat(), type='planner-held',
                                             subject=self.A, project='do-it-v2')) + '\n')
            status, launches, queries = self.ensure(fold.read_events(use_cache=False))
            self.assertEqual(([args for args, _ in launches], up.LAST_PLANNER_READY), ([(self.B, 'charter', 1)], [self.B]))
        (self.events / 'L-planner-9901.jsonl').unlink()
        rows = fold.read_events(use_cache=False)
        with self.subTest(case='held lock'):
            fd = up._planner_lock()
            try:
                status, launches, queries = self.ensure(rows)
            finally:
                os.close(fd)
            self.assertEqual((status, launches, queries, up.LAST_PLANNER_READY), ('alive', [], [], []))
        with self.subTest(case='live pane only'):
            session = self.sessions / f'{os.getpid()}.json'
            session.write_text(json.dumps({'name': 'L-planner-9777'}))
            try:
                status, launches, queries = self.ensure(rows)
            finally:
                session.unlink()
            self.assertEqual((status, launches, queries), ('alive', [], []))
        for label in ('unreadable lock', 'unreadable session', 'unknown process scan'):
            with self.subTest(case=label):
                lock = self.root / 'planner.lock'
                session = self.sessions / '123.json'
                scan = []
                if label == 'unreadable lock':
                    lock.touch(); lock.chmod(0)
                elif label == 'unreadable session':
                    session.write_text('{')
                else:
                    scan = None
                try:
                    with patch.object(up, '_planner_processes', return_value=scan):
                        status, launches, queries = self.ensure(rows)
                finally:
                    lock.chmod(0o600) if lock.exists() else None
                    session.unlink(missing_ok=True)
                self.assertEqual((status, launches, queries, up.LAST_PLANNER_READY), ('undetermined', [], [], None))
        with self.subTest(case='reap before deciding, lock probed first'):
            order = []
            lock_fn, scan_fn = up._planner_lock, up._planner_processes
            with patch.object(up, '_planner_lock', side_effect=lambda: order.append('lock') or lock_fn()), \
                 patch.object(up, '_planner_processes', side_effect=lambda: order.append('scan') or scan_fn()):
                rows = self.failed_run(9301)
                self.start(9302, wrapper_pid=99999999)
                status, launches, queries = self.ensure(fold.read_events(use_cache=False))
            self.assertEqual(order[:2], ['lock', 'scan'])
            # Both counted dead starts for one charter close in this pass: real planner_attempts
            # counts both, so ensure_planner escalates once and launches nothing.
            self.assertEqual((status, launches), ('escalated', []))
            self.assertEqual([e['reason'] for e in self.ended(self.readback()) if e['planner'] == 'L-planner-9302'], ['vanished'])

    def test_sessionless_orphan_blocks_launch(self):
        rows = self.file_charters('L-charter-9001')
        process = subprocess.Popen([str(self.cli), '-n', 'L-planner-9001'])
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait(timeout=5)))
        time.sleep(.05)
        identity = up._proc_identity(process.pid)
        self.assertEqual(identity['planner'], 'L-planner-9001')
        with patch.object(up, '_planner_processes', wraps=up._scan_planner_processes), \
             patch.object(up, '_planner_host_namespace', return_value=identity['pid_namespace']):
            with self.subTest(case='no start row'):
                status, launches, queries = self.ensure(rows)
                self.assertEqual((status, launches, queries, up.LAST_PLANNER_READY), ('alive', [], [], []))
            self.start()
            rows = fold.read_events(use_cache=False)
            ack = self.approve(rows)
            data = json.loads(ack.read_text()); data['pid_namespace'] = identity['pid_namespace']
            ack.write_text(json.dumps(data))
            with self.subTest(case='young orphan is live, not dead'):
                with patch.object(up.os, 'kill') as kill:
                    status, launches, queries = self.ensure(rows)
                kill.assert_not_called()
                self.assertEqual((status, launches, queries), ('alive', [], []))
                self.assertIsNone(process.poll())
                self.assertEqual(self.ended(self.readback()), [])
            with self.subTest(case='old orphan ignoring SIGTERM stays blocking'):
                ignoring = self.root / 'stubborn' / 'claude'
                ignoring.parent.mkdir()
                ignoring.write_text('#!/usr/bin/python3\nimport signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(120)\n')
                ignoring.chmod(0o700)
                stubborn = subprocess.Popen([str(ignoring), '-n', 'L-planner-9001'])
                process.kill(); process.wait(timeout=5)
                self.addCleanup(lambda: (stubborn.poll() is None and stubborn.kill(), stubborn.wait(timeout=5)))
                time.sleep(.2)
                with patch.dict(os.environ, DOIT_CLAUDE_BIN=str(ignoring)):
                    status, launches, queries = self.ensure(rows, now=self.now + timedelta(hours=2))
                self.assertEqual((status, launches, queries), ('alive', [], []))
                self.assertIsNone(stubborn.poll())
                self.assertEqual(self.ended(self.readback()), [])
                stubborn.kill(); stubborn.wait(timeout=5)
            with self.subTest(case='old orphan killed by SIGTERM stops blocking after confirmed death'):
                process = subprocess.Popen([str(self.cli), '-n', 'L-planner-9001'])
                self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait(timeout=5)))
                time.sleep(.2)
                waiter = __import__('threading').Thread(target=process.wait, daemon=True)
                waiter.start()
                status, launches, queries = self.ensure(rows, now=self.now + timedelta(hours=2))
                waiter.join(timeout=5)
                self.assertEqual(process.poll(), -15)
                self.assertEqual([e['reason'] for e in self.ended(self.readback())], ['never-started'])
                self.assertEqual(status, 'started')
                self.assertEqual([args for args, _ in launches], [('L-charter-9001', 'charter', 1)])

    def test_admission_outcomes(self):
        rows = self.file_charters('L-charter-9001', 'L-charter-9002')
        for outcome in ('started', 'starting', 'alive', 'undetermined'):
            with self.subTest(outcome=outcome):
                status, launches, queries = self.ensure(rows, result=outcome)
                self.assertEqual((status, len(launches), len(queries), up.LAST_PLANNER_READY),
                                 (outcome, 1, 1, ['L-charter-9001', 'L-charter-9002']))
        with self.subTest(outcome='launch exception'):
            status, launches, queries = self.ensure(rows, result=OSError('tmux'))
            self.assertEqual((status, len(launches), len(queries)), ('undetermined', 1, 1))
        with self.subTest(outcome='unknown events'):
            self.assertEqual(up.ensure_planner(None, self.now), 'undetermined')
            self.assertIsNone(up.LAST_PLANNER_READY)

    def test_up_returns(self):
        rows = self.file_charters('L-charter-9001')
        slept = []
        with patch.object(up, 'install'), patch.object(up, 'launch_planner') as launch, \
             patch.object(up, '_SLEEP', side_effect=slept.append), patch.object(time, 'sleep', side_effect=slept.append), \
             patch.object(up, '_supervisor_main') as supervisor:
            out = io.StringIO()
            from contextlib import redirect_stdout
            with redirect_stdout(out):
                self.assertEqual(up.main(), 0)
                self.assertEqual(up.main(max_cycles=5), 0)
            launch.assert_not_called()
            supervisor.assert_not_called()
        self.assertEqual(slept, [])
        self.assertIn('do-it-relay', out.getvalue())
        self.assertIn('tick', out.getvalue())
        self.assertEqual([p.name for p in self.events.glob('L-up-*')], [])
        self.assertEqual([e for e in fold.read_events(use_cache=False) if e['type'] != 'charter-filed'], [])
        with self.subTest(case='print_only keeps cron output'):
            out = io.StringIO()
            with redirect_stdout(out):
                cmd, env = up.main(print_only=True)
            self.assertIn('doit tick', out.getvalue())
            self.assertIsInstance(cmd, list)
        with self.subTest(case='doit up exits 0 without supervising'):
            result = subprocess.run([str(pathlib.Path(__file__).resolve().parent.parent / 'doit'), 'up'],
                                    env=dict(os.environ, DOIT_NO_POKE='1'), capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            self.assertIn('do-it-relay', result.stdout)
        with self.subTest(case='relay reexec seam remains'):
            self.assertTrue(callable(up.maybe_reexec))


class AC2(Scratch):
    def test_reap_class_matrix(self):
        for wrapper, launched, reason in [(True, True, 'vanished'), (True, False, 'vanished'),
                                         (False, True, 'vanished'), (False, False, 'never-started')]:
            with self.subTest(wrapper=wrapper, launched=launched):
                n = 9001 + len(list(self.events.glob('L-planner-*')))
                start = self.start(n, **({'wrapper_pid': 99999999} if wrapper else {}))
                rows = fold.read_events(use_cache=False)
                if launched:
                    rows.append(dict(type='role-launched', pane=start['planner'], pid=99999998,
                                     proc_start='1'))
                self.approve(rows)
                self.assertEqual([e['reason'] for e in up.reap_planners(rows, self.now)], [reason])
        self.assertEqual([e['actor'] for e in self.ended(self.readback())], ['planner']*4)

    def test_persisted_closeout_idempotency(self):
        self.start(wrapper_pid=99999999)
        stale = fold.read_events(use_cache=False)
        self.assertEqual(len(up.reap_planners(stale, self.now)), 1)
        self.assertEqual(up.reap_planners(stale, self.now), [])
        self.assertEqual(up.reap_planners(fold.read_events(use_cache=False), self.now), [])
        ends = self.ended(self.readback())
        self.assertEqual([(e['reason'],e['actor']) for e in ends], [('vanished','planner')])

    def test_migration_report_and_ack(self):
        for i in range(63):
            self.start(9001+i, mode='ctx-restart' if i < 58 else 'charter')
        rows = fold.read_events(use_cache=False)
        report = io.StringIO()
        with patch.object(up.os, 'kill') as kill, redirect_stderr(report):
            self.assertEqual(up.reap_planners(rows, self.now), [])
            kill.assert_not_called()
        summary = json.loads(report.getvalue().removeprefix('planner-reap '))
        self.assertEqual((summary['closed'],summary['signaled'],summary['left']),(0,0,63))
        self.assertEqual(len(self.ended(self.readback())), 0)
        ack = self.approve(rows)
        self.assertEqual(len(up.reap_planners(rows, self.now)), 63)
        self.assertEqual(len(self.ended(self.readback())), 63)
        self.start(9998)
        self.assertEqual(up.reap_planners(fold.read_events(use_cache=False), self.now), [])
        ack.write_text('{"pid_namespace":"wrong", "starts":[]}')
        self.assertEqual(up.reap_planners(fold.read_events(use_cache=False), self.now), [])

    def test_unsafe_identity_matrix(self):
        for label, fields in [('malformed', {'wrapper_pid':None}), ('live-wrapper', {'wrapper_pid':os.getpid()}),
                              ('lock', {}), ('unknown-scan', {}), ('hidden-host', {}), ('live-child', {}), ('empty-session', {})]:
            with self.subTest(label=label):
                start = self.start(9100+len(list(self.events.glob('L-planner-*'))), **fields)
                rows = fold.read_events(use_cache=False)
                self.approve(rows)
                with patch.object(up, '_planner_processes', return_value=None if label=='unknown-scan' else []), \
                     patch.object(up, '_planner_host_namespace', return_value=None if label=='hidden-host' else 'fixture-host'):
                    if label == 'live-child':
                        rows.append(dict(type='role-launched',pane=start['planner'],pid=os.getpid(),
                                         proc_start=panes.proc_start(os.getpid())))
                    invalid_session = self.sessions / f'{os.getpid()}.json'
                    if label == 'empty-session':
                        invalid_session.write_text(json.dumps({'name':''}))
                    fd = up._planner_lock() if label=='lock' else None
                    try:
                        before = len(self.ended(self.readback()))
                        with patch.object(up.os, 'kill') as kill:
                            up.reap_planners(rows, self.now)
                            kill.assert_not_called()
                        self.assertEqual(len(self.ended(self.readback())),before)
                    finally:
                        if fd is not None: os.close(fd)
                        if label == 'empty-session': invalid_session.unlink()
                # Keep this case from contaminating the next one.
                path = self.events / (start['planner']+'.jsonl')
                self.assertIsNone(dispatch.emit(path, {}, 'planner-ended', subject=start['subject'],
                                               planner=start['planner'], reason='held'))

    def test_signal_identity_guards(self):
        for i, label in enumerate(('pid-reuse', 'different-uid', 'namespace', 'duplicate',
                                    'extra-own-row', 'role-launched', 'young-row', 'unrelated-argv',
                                    'unreadable-proc', 'ended')):
            with self.subTest(label=label):
                start = self.start(9400+i)
                rows = fold.read_events(use_cache=False)
                self.approve(rows)
                identity = dict(pid=99999998, uid=os.geteuid(), start_ticks=42,
                    pid_namespace='fixture-host', started_at=self.now.timestamp()-7200,
                    planner=start['planner'], zombie=False)
                current = dict(identity)
                processes = [identity]
                if label == 'pid-reuse': current['start_ticks'] += 1
                if label == 'different-uid': current['uid'] += 1
                if label == 'namespace': current['pid_namespace'] = 'hidden'
                if label == 'unrelated-argv': current['planner'] = None
                if label == 'duplicate': processes = [identity,dict(identity,pid=99999997)]
                path = self.events / (start['planner']+'.jsonl')
                if label == 'extra-own-row':
                    self.assertIsNone(dispatch.emit(path, {}, 'planner-started', subject=start['subject'],
                                                   planner=start['planner'],mode='charter'))
                if label == 'role-launched':
                    rows.append(dict(type='role-launched',pane=start['planner'],pid=os.getpid(),
                                     proc_start=panes.proc_start(os.getpid())))
                if label == 'young-row':
                    # A fresh snapshot cannot silently substitute a different start row.
                    rows = [{**e,'ts':self.now.isoformat()} if e.get('planner')==start['planner'] else e
                            for e in rows]
                    self.approve(rows)
                if label == 'ended':
                    self.assertIsNone(dispatch.emit(path, {}, 'planner-ended',subject=start['subject'],
                                                   planner=start['planner'],reason='held'))
                before = len(self.ended(self.readback()))
                with patch.object(up,'_planner_processes',return_value=processes), \
                     patch.object(up,'_proc_identity',side_effect=PermissionError if label=='unreadable-proc' else None,
                                  return_value=current), patch.object(up.os,'kill') as kill:
                    self.assertEqual(up.reap_planners(rows,self.now),[])
                    kill.assert_not_called()
                self.assertEqual(len(self.ended(self.readback())),before)
                # End fixture cases so subsequent passes do not see earlier candidates.
                if label != 'ended':
                    self.assertIsNone(dispatch.emit(path, {}, 'planner-ended',subject=start['subject'],
                                                   planner=start['planner'],reason='held'))

    def test_sessionless_pid_identity_and_age(self):
        with patch.object(up, '_planner_processes', wraps=up._scan_planner_processes):
            process = subprocess.Popen([str(self.cli), '-n', 'L-planner-9001'])
            def cleanup():
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
            self.addCleanup(cleanup)
            time.sleep(.05)
            start = self.start()
            rows = fold.read_events(use_cache=False)
            self.approve(rows)
            identity = up._proc_identity(process.pid)
            self.assertEqual(identity['planner'],start['planner'])
            self.assertEqual(up.reap_planners(rows, self.now), [])
            self.assertIsNone(process.poll())
            # Both row and kernel process start must be older than one hour.
            future = self.now + timedelta(hours=2)
            with patch.object(up, '_planner_host_namespace', return_value=identity['pid_namespace']):
                ack = self.approve(rows)
                data = json.loads(ack.read_text()); data['pid_namespace']=identity['pid_namespace']
                ack.write_text(json.dumps(data))
                out = up.reap_planners(rows, future)
            self.assertEqual([e['reason'] for e in out], ['never-started'])
            process.wait(timeout=3)


class AC3(Scratch):
    def pairs(self, reasons, handover=None):
        rows = []
        for i, reason in enumerate(reasons):
            rows.extend([dict(type='planner-started',subject='L-charter-9001'),
                         dict(type='planner-ended',subject='L-charter-9001',reason=reason,
                              handover=str(handover) if handover else None)])
        return rows

    def test_attempt_policy_matrix(self):
        for reasons, count, action in [([],0,'start'),(['exit-1'],1,'retry'),
            (['exit-1','vanished'],2,'escalate'),(['never-started'],0,'start'),
            (['l1-complete','held'],0,'start')]:
            with self.subTest(reasons=reasons):
                result=relay.planner_attempts(self.pairs(reasons),'L-charter-9001')
                self.assertEqual((result['attempts'], result['next']), (count,action))

    def test_ctx_continuation_cap(self):
        handover = self.root / 'handover.md'
        handover.write_text('Continue the charter')
        for n in range(1,6):
            with self.subTest(n=n):
                result=relay.planner_attempts(self.pairs(['ctx']*n,handover),'L-charter-9001')
                self.assertEqual(result['attempts'],max(0,n-2))
        handover.write_text('A genuinely rewritten handover')
        self.assertEqual(relay.planner_attempts(self.pairs(['ctx']*4,handover),'L-charter-9001')['attempts'],2)
        self.assertEqual(relay.planner_attempts(self.pairs(['ctx','never-started','ctx','ctx'],handover),
                                              'L-charter-9001')['attempts'],1)
        for bad in [None, self.root/'missing', self.root]:
            self.assertEqual(relay.planner_attempts(self.pairs(['ctx']*3,bad),'L-charter-9001')['attempts'],3)
        handover.write_text('')
        self.assertEqual(relay.planner_attempts(self.pairs(['ctx']*3,handover),'L-charter-9001')['attempts'],3)
        handover.write_text('again'); handover.chmod(0)
        self.assertEqual(relay.planner_attempts(self.pairs(['ctx'],handover),'L-charter-9001')['attempts'],1)
        handover.chmod(0o600)
        rows=self.pairs(['ctx']*3,handover)+self.pairs(['vanished','ctx'],handover)
        self.assertEqual(relay.planner_attempts(rows,'L-charter-9001')['attempts'],2)

    def test_escalation_persisted_once(self):
        charter = 'L-charter-9001'
        self.file_charters(charter)
        stale = self.failed_run(9501)
        stale = self.failed_run(9502, reason='vanished')
        status, launches, queries = self.ensure(stale)
        self.assertEqual((status, launches), ('escalated', []))
        # Repeated passes with the same stale snapshot and with fresh input write nothing more.
        for rows in (stale, stale, fold.read_events(use_cache=False)):
            status, launches, _ = self.ensure(rows)
            self.assertEqual(launches, [])
            self.assertIn(status, ('escalated', 'idle'))
        result = subprocess.run(['/usr/bin/python3', '-c',
            'import json,fold; print(json.dumps([e for e in fold.read_events(use_cache=False) '
            'if e["type"]=="escalation-blocking"]))'],
            cwd=pathlib.Path(__file__).resolve().parent, env=dict(os.environ),
            capture_output=True, text=True, check=True)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['actor'], row['subject'], row['attempts'], row['last_reason']),
                         ('up', charter, 2, 'vanished'))
        self.assertTrue(row['_src'].startswith('L-up-'))
        self.assertEqual(row['irreversible'], up._attempt_escalation(
            charter, dict(attempts=2, last_reason='vanished'))['irreversible'])
        self.assertIn('replan=yes', row['irreversible'])
        # A decision with replan=yes releases it under the existing policy.
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec='seconds')
        with patch.object(dispatch, 'now', return_value=later):
            self.assertIsNone(dispatch.emit(self.events / 'L-operator-9001.jsonl', {}, 'decision',
                                            subject=charter, replan='yes'))
        status, launches, _ = self.ensure(fold.read_events(use_cache=False))
        self.assertEqual((status, [args for args, _ in launches]), ('started', [(charter, 'charter', 1)]))

    def test_decision_release_and_hold(self):
        rows=self.pairs(['ctx']*4)
        escalation=dict(type='escalation-blocking',subject='L-charter-9001',actor='up',attempts=4,
                        _src='L-up-9001.jsonl:1')
        answer=dict(type='decision',subject='L-charter-9001',_src='L-operator-9001.jsonl:1')
        self.assertEqual(relay.planner_attempts(rows+[escalation,answer],'L-charter-9001')['next'],'held')
        answer={**answer,'replan':'yes'}
        reset=rows+[escalation,answer]
        self.assertEqual(relay.planner_attempts(reset,'L-charter-9001')['attempts'],0)
        handover=self.root/'handover'; handover.write_text('continue')
        self.assertEqual(relay.planner_attempts(reset+self.pairs(['ctx']*2,handover),'L-charter-9001')['next'],'start')
        self.assertEqual(relay.planner_attempts(reset+self.pairs(['ctx']*3,handover),'L-charter-9001')['next'],'escalate')


class AC4(Scratch):
    def setUp(self):
        super().setUp()
        for target, result in (('intake.run', {}), ('notes.run', None), ('pr_closeout.run', None),
                ('install_sync.run', []), ('push_origin.run', None), ('carry.uncarried', []),
                ('carry.sync_v4', None), ('pane_resume.run', None), ('crons.check', []),
                ('look._read_state', {'last_pass': fold.NOW.isoformat()}), ('look.heartbeat_missed', False),
                ('tree_cleanup.reap_worktrees', ([], [])), ('autodispatch.redispatch_dead', []),
                ('merge_queue.enqueue_due', []), ('tick._steps', []), ('tick.run_steps', [])):
            self.enterContext(patch(target, return_value=result))
        self.enterContext(patch.object(up, 'ensure_relay', return_value='idle'))
        self.file_charters('L-charter-9001')

    def tick_rows(self):
        result = subprocess.run(['/usr/bin/python3', '-c',
            'import json,fold; print(json.dumps([e for e in fold.read_events(use_cache=False) if e["type"]=="tick"]))'],
            cwd=pathlib.Path(__file__).resolve().parent, env=dict(os.environ),
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def run_tick(self, entry='main', launch='started'):
        launches, queries, reads = [], [], []
        real_plannable, real_read = relay.plannable, fold.read_events
        def read(*args, **kwargs):
            reads.append(1)
            return real_read(*args, **kwargs)
        def query(*args, **kwargs):
            queries.append(1)
            return real_plannable(*args, **kwargs)
        def launcher(*args, **kwargs):
            launches.append(args)
            if isinstance(launch, Exception):
                raise launch
            return launch
        with patch.object(up, 'launch_planner', side_effect=launcher), patch.object(relay, 'plannable', side_effect=query), \
             patch.object(fold, 'read_events', side_effect=read), redirect_stderr(io.StringIO()), \
             patch('sys.stdout', new_callable=io.StringIO):
            if entry == 'main':
                self.assertEqual(tick.main(), 0)
            else:
                self.assertEqual(tick.wait(root=self.root, max_s=0), 'interval')
        return self.tick_rows()[-1], launches, len(queries), len(reads)

    def test_tick_status_readback_matrix(self):
        A = 'L-charter-9001'
        cases = []
        cases.append(('started', 'started', [A], 1, {}))
        cases.append(('starting', 'starting', [A], 1, {}))
        cases.append(('undetermined admission', 'undetermined', [A], 1, {}))
        cases.append(('launch exception', 'undetermined', [A], 1, {'launch': OSError('tmux')}))
        for label, status, ready, count, extra in cases:
            with self.subTest(case=label):
                row, launches, queries, _ = self.run_tick(launch=extra.get('launch', status))
                self.assertEqual((row['planner_status'], row['planner_ready'], queries, len(launches)),
                                 (status, ready, count, 1))
                self.assertEqual(row['relay'], 'idle')
                self.assertIn('lane', row)
                (self.root / 'planner.starting').unlink(missing_ok=True)
        with self.subTest(case='alive'):
            fd = up._planner_lock()
            try:
                row, launches, queries, _ = self.run_tick()
            finally:
                os.close(fd)
            self.assertEqual((row['planner_status'], row['planner_ready'], queries, launches), ('alive', [], 0, []))
        with self.subTest(case='undetermined'):
            with patch.object(up, 'planner_alive', return_value=None):
                row, launches, queries, _ = self.run_tick()
            self.assertEqual((row['planner_status'], row['planner_ready'], queries, launches),
                             ('undetermined', None, 0, []))
        with self.subTest(case='migration report-only'):
            self.start(9601, mode='ctx-restart')
            row, launches, queries, _ = self.run_tick()
            self.assertEqual((row['planner_status'], row['planner_ready'], queries, launches),
                             ('undetermined', None, 0, []))
            self.assertEqual(self.ended(self.readback()), [])
            self.approve(fold.read_events(use_cache=False))
            row, launches, queries, _ = self.run_tick()
            self.assertEqual(self.ended(self.readback())[0]['reason'], 'never-started')
            self.assertEqual(row['planner_status'], 'started')
            self.assertEqual(row['planner_ready'], [A])
        with self.subTest(case='idle'):
            for path in self.events.glob('L-operator-9001.jsonl'):
                path.unlink()
            row, launches, queries, _ = self.run_tick()
            self.assertEqual((row['planner_status'], row['planner_ready'], queries, launches), ('idle', [], 1, []))
        with self.subTest(case='escalated'):
            self.file_charters(A)
            self.failed_run(9701)
            self.failed_run(9702)
            row, launches, queries, _ = self.run_tick()
            self.assertEqual((row['planner_status'], row['planner_ready'], queries, launches), ('escalated', [A], 1, []))

    def test_shared_snapshot_and_entrypoints(self):
        seen = []
        def spy(name, result):
            def call(events, now, **kw):
                seen.append((name, events))
                return result
            return call
        for entry in ('main', 'wait'):
            with self.subTest(entry=entry):
                seen.clear()
                with patch.object(up, 'ensure_relay', side_effect=spy('relay', 'idle')), \
                     patch.object(up, 'ensure_planner', side_effect=spy('planner', 'idle')), \
                     patch('sys.stdout', new_callable=io.StringIO):
                    if entry == 'main':
                        tick.main()
                    else:
                        tick.wait(root=self.root, max_s=0)
                self.assertEqual([name for name, _ in seen], ['relay', 'planner'])
                self.assertIs(seen[0][1], seen[1][1], 'one source events snapshot feeds both ensures')
                self.assertEqual(self.tick_rows()[-1]['planner_status'], 'idle')
        with self.subTest(case='one scheduling read, plannable once, both routes'):
            for entry in ('main', 'wait'):
                row, launches, queries, reads = self.run_tick(entry=entry)
                self.assertEqual((row['planner_status'], queries, len(launches)), ('started', 1, 1))
                (self.root / 'planner.starting').unlink(missing_ok=True)
                # Reaper ends are folded into the local snapshot, not a second full-ledger read.
                self.assertLessEqual(reads, 3)

    def test_planner_failure_isolation(self):
        with patch.object(up, 'ensure_planner', side_effect=RuntimeError('planner exploded')), \
             patch.object(up, 'ensure_relay', return_value='started') as relay_call, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(tick.main(), 0)
        relay_call.assert_called_once()
        row = self.tick_rows()[-1]
        self.assertEqual((row['relay'], row['planner_status'], row['planner_ready']), ('started', 'undetermined', None))
        self.assertIn('lane', row)
        with patch.object(up, 'ensure_planner', return_value=None), patch('sys.stdout', new_callable=io.StringIO):
            tick.main()
        self.assertEqual(self.tick_rows()[-1]['planner_status'], 'undetermined')


class AC5(Scratch):
    SRC = pathlib.Path(__file__).resolve().parent
    FIXTURES = ('test_tick_held.py', 'test_autodispatch_fault_isolation.py', 'test_close_reviews.py',
                'test_reader_snapshot.py', 'test_rework_wait_alarm.py')
    REGRESSIONS = ('test_up.py', 'test_tick.py', 'test_relay.py', 'test_pane_resume.py', 'test_planner_run.py',
                   'test_tick_held.py', 'test_autodispatch_fault_isolation.py', 'test_close_reviews.py',
                   'test_reader_snapshot.py', 'test_rework_wait_alarm.py', 'test_relay_builder.py',
                   'test_relay_supervision.py', 'test_tick_retry1851.py', 'test_tick_wait_notify.py',
                   'test_launch.py', 'test_ledger_cache.py')

    def test_retired_launch_routes(self):
        import planner_run, pane_resume
        for module, name in ((up, 'restart_pane'), (planner_run, 'restart_pane'), (up, 'RESTART_CONFIRM_SECONDS'),
                             (pane_resume, '_restart_ctx'), (pane_resume, 'ctx_ended_without_successor')):
            self.assertFalse(hasattr(module, name), f'{module.__name__}.{name}')
        # A ctx end alone cannot launch or write pane-resumed through pane_resume.run.
        end = dict(type='planner-ended', subject='L-charter-9001', planner='L-planner-9001', reason='ctx',
                   ts=(self.now - timedelta(minutes=3)).isoformat())
        with patch.object(up, 'launch_planner') as launch:
            result = pane_resume.run([end], now=self.now, sessions_dir=self.sessions)
        launch.assert_not_called()
        self.assertEqual(result['restarted'], [])
        self.assertEqual([e for e in fold.read_events(use_cache=False) if e['type'].startswith('pane-resume')], [])
        # The wrapper still invokes the shared lifecycle helper.
        self.assertTrue(callable(up._run_planner))
        # Only ensure_planner calls launch_planner or writes the attempt-limit escalation.
        callers, writers = [], []
        for path in sorted(self.SRC.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, 'attr', getattr(node.func, 'id', None)) == 'launch_planner'):
                        callers.append((path.name, fn.name))
                    if isinstance(node, ast.Constant) and node.value == 'escalation-blocking':
                        text = ast.get_source_segment(path.read_text(), fn) or ''
                        if 'planner attempt(s)' in text or 'drew their budget' in text:
                            writers.append((path.name, fn.name))
        self.assertEqual(sorted(set(callers)), [('up.py', 'ensure_planner')])
        self.assertEqual(sorted(set(writers)), [])
        self.assertIn('drew their budget', inspect.getsource(up._attempt_escalation))
        with patch.object(up, 'launch_planner') as launch, patch.object(up, 'install'), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(up.main(), 0)
        launch.assert_not_called()

    def test_fixture_isolation(self):
        tripwire = (
            "import os,runpy,sys\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import up\n"
            "def trip(name):\n"
            "    def f(*a, **k):\n"
            "        open(sys.argv[3], 'a').write(name + '\\n'); raise SystemExit(99)\n"
            "    return f\n"
            "for name in ('launch_planner', 'reap_planners', '_planner_processes'):\n"
            "    setattr(up, name, trip(name))\n"
            "os.kill = trip('kill')\n"
            "runpy.run_path(sys.argv[1], run_name='__main__')\n")
        for name in self.FIXTURES:
            with self.subTest(fixture=name):
                self.assertIn('ensure_planner', (self.SRC / name).read_text(), 'fixture patches the boundary')
                marker = self.root / f'tripped-{name}'
                env = {k: v for k, v in os.environ.items() if not k.startswith('SUPABASE_DB_URL')}
                subprocess.run(['/usr/bin/python3', '-c', tripwire, str(self.SRC / name), str(self.SRC), str(marker)],
                               env=env, capture_output=True, text=True, timeout=300)
                self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else '')

    def test_existing_regressions(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix='2310-regress-'))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SUPABASE_DB_URL', 'TEST_', 'DOIT_'))}
        env.update(DOIT_NO_POKE='1', HOME=scratch, DOIT_ROOT=scratch, DOIT_PROJECT='')
        root = self.SRC.parent

        def failing_ids(path, cwd):
            done = subprocess.run(['/usr/bin/python3', str(path)], env=env, cwd=cwd,
                                  capture_output=True, text=True, timeout=900)
            ids = sorted({line.split(':', 1)[1].strip() for line in (done.stdout + done.stderr).splitlines()
                          if line.startswith(('FAIL:', 'ERROR:'))})
            return done.returncode, ids

        def git(*args):
            return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)

        results, red = {}, []
        for name in self.REGRESSIONS:
            code, ids = failing_ids(self.SRC / name, root)
            results[name] = (code, ids)
            print(f'{name}: exit {code}')
            if code:
                red.append(name)
        if red:
            base = os.environ.get('SPEC_BASE_SHA', '').strip()
            if not base:
                for ref in ('main', 'origin/main', 'master', 'origin/master'):
                    found = git('merge-base', 'HEAD', ref)
                    if found.returncode == 0 and found.stdout.strip():
                        base = found.stdout.strip()
                        break
            self.assertTrue(base, 'no base commit established: undetermined is never clean')
            wt = pathlib.Path(scratch) / 'base-worktree'
            added = git('worktree', 'add', '--detach', str(wt), base)
            self.assertEqual(added.returncode, 0, added.stderr)
            try:
                for name in red:
                    base_code, base_ids = failing_ids(wt / 'src' / name, wt)
                    print(f'{name}: branch ids {results[name][1]}; base {base[:9]} exit {base_code} ids {base_ids}')
                    self.assertNotEqual(base_code, 0, f'{name} fails only on the branch')
                    self.assertTrue(base_ids, f'{name}: base failing ids undetermined')
                    self.assertTrue(results[name][1], f'{name}: branch failing ids undetermined')
                    self.assertLessEqual(set(results[name][1]), set(base_ids),
                                         f'{name}: new failing ids absent from base')
            finally:
                git('worktree', 'remove', '--force', str(wt))

class ParallelBuilderLaunch(Scratch):
    """Real scratch Git worktrees and ledger appends, with costly detach calls."""
    DELAY = 0.5

    def setUp(self):
        import contextlib
        with patch.object(up, 'ensure_planner', up.ensure_planner):
            from test_tick import _builder_project
        super().setUp()
        self.stack = self.enterContext(contextlib.ExitStack())
        _builder_project(self.root, self.stack)
        self.enterContext(patch.object(dispatch, 'CONTENT', self.root / 'content'))
        self.enterContext(patch.object(fold, 'PROJECT', None))
        (self.root / 'content').mkdir()
        self.calls = []
        self.failing = set()
        self.main_thread = __import__('threading').get_ident()
        self.worker_threads = set()
        self.enterContext(patch('autodispatch.capacity', return_value=8))
        self.enterContext(patch('loadgate.begin', return_value=None))

    def seed(self, count):
        import nextstep
        self.subjects = [f'L-spec-{9100 + n}' for n in range(count)]
        self.rows = [dict(type='spec-written', subject=sid, project='proj', actor='operator',
                          ts=(self.now - timedelta(hours=1)).isoformat(), _src=f'fixture:{n}')
                     for n, sid in enumerate(self.subjects)]
        for sid in self.subjects:
            (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        self.steps = [nextstep.Step(sid, 'spec', 'checking', 'action', 'build', 'tick',
                                   f'fixture:{n}', 'condition met', None, self.rows[n]['ts'])
                      for n, sid in enumerate(self.subjects)]

    def runner(self, argv):
        if argv[0] == 'git':
            done = subprocess.run(argv, capture_output=True, text=True)
            return dict(code=done.returncode, stdout=done.stdout, stderr=done.stderr)
        if argv[:2] == ['doit', 'dispatch']:
            import threading
            sid = argv[4]
            self.calls.append(sid)
            self.worker_threads.add(threading.get_ident())
            time.sleep(self.DELAY)
            if sid in self.failing:
                return dict(code=1, stdout='', stderr='fixture detach failure')
            return dict(code=0, stdout=json.dumps(dict(detached='L-builder-' + sid[7:])), stderr='')
        return dict(code=0, stdout='/packet.md', stderr='')

    def run_pass(self):
        state = dict(skips=[], launched=set(), wrote=False)
        token = tick._PASS.set(state)
        seats = {}
        start = time.monotonic()
        try:
            lines = tick.run_steps(self.rows, {}, self.now, dispatcher=self.runner,
                                   steps=self.steps, seat_state=seats)
        finally:
            tick._PASS.reset(token)
        return lines, time.monotonic() - start, state, seats

    def evidence(self, ac, body):
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://tick-parallel/' + ac, status=0,
                              body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                              body_excerpt=payload)))

    def test_ready_specs_start_within_one_pass(self):
        self.seed(15)
        lines, elapsed, state, seats = self.run_pass()
        acted = [line for line in lines if line.startswith('acted ') and ' build spawn=' in line]
        waiting = [line for line in lines if line.startswith('seat-wait ')]
        events = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
        self.assertEqual(len(acted), 8, lines)
        self.assertEqual(len(waiting), 7, lines)
        self.assertEqual(sum(e['type'] == 'autodispatched' for e in events), 8)
        self.assertEqual([line.split()[1].rstrip(':') for line in lines], self.subjects)
        self.assertEqual(state['launched'], set(self.subjects[:8]))
        self.assertTrue(state['wrote'])
        self.assertEqual(seats, {'builder': 0})
        self.assertNotIn(self.main_thread, self.worker_threads)
        self.assertGreater(len(self.worker_threads), 1)
        self.assertLessEqual(len(self.worker_threads), 4)
        self.assertLess(elapsed, self.DELAY * 8 / 2)
        self.evidence('AC1', dict(acted=len(acted), seat_wait=len(waiting),
                                autodispatched=8, elapsed=elapsed, bound=self.DELAY * 8 / 2))

    def test_bookkeeping_stays_on_main_thread_and_worker_errors_are_isolated(self):
        from dataclasses import replace
        from types import SimpleNamespace
        import dispatch_isolation
        import threading
        self.seed(4)
        self.steps[0] = replace(self.steps[0], action='rebuild')
        noted, reported = [], []
        reporter = dispatch_isolation.Pass(self.rows, self.now)
        original_fail = reporter.fail

        def fail(item, error, **kwargs):
            self.assertEqual(threading.get_ident(), self.main_thread)
            reported.append(item)
            original_fail(item, error, **kwargs)

        def note(item, spawned, **kwargs):
            self.assertEqual(threading.get_ident(), self.main_thread)
            noted.append((item, spawned, kwargs['raised']))

        def launch(step, **kwargs):
            self.assertIsNotNone(tick._PASS.get())
            if step.item == self.subjects[0]:
                raise RuntimeError('worker failed')
            if step.item == self.subjects[1]:
                raise ValueError('launch refused')
            if step.item == self.subjects[2]:
                return None
            return 'L-builder-9103'

        gate = SimpleNamespace(grades_first=False, mode='observe',
                               check=lambda *a, **kw: SimpleNamespace(hold=False, override=None),
                               note_launch=note, finish=lambda: {})
        with patch('autodispatch.capacity', return_value=4), \
             patch('loadgate.begin', return_value=gate), \
             patch('dispatch_isolation.Pass', return_value=reporter), \
             patch.object(reporter, 'fail', side_effect=fail), \
             patch.object(tick, 'launch', side_effect=launch):
            lines, elapsed, state, seats = self.run_pass()
        self.assertEqual([line.split()[1].rstrip(':') for line in lines], self.subjects)
        self.assertIn('RuntimeError', lines[0])
        self.assertIn('launch refused', lines[1])
        self.assertEqual(lines[2], f'hold {self.subjects[2]}: standing dispatch failure')
        self.assertEqual(reported, self.subjects[:2])
        self.assertEqual(noted, [(self.subjects[0], True, True),
                                 (self.subjects[1], True, True),
                                 (self.subjects[2], False, False),
                                 (self.subjects[3], True, False)])
        self.assertEqual(seats, {'builder': 3})

    def test_dry_run_uses_no_workers_or_dispatcher(self):
        self.seed(15)
        with patch.object(tick, 'ThreadPoolExecutor', side_effect=AssertionError('dry-run thread')):
            lines = tick.run_steps(self.rows, {}, self.now, dispatcher=self.runner,
                                   steps=self.steps, dry_run=True)
        self.assertEqual(len([line for line in lines if line.startswith('would act ')]), 8)
        self.assertEqual(len([line for line in lines if line.startswith('seat-wait ')]), 7)
        self.assertEqual(self.calls, [])

    def test_guards_and_seat_accounting_survive_parallel_launch(self):
        self.seed(12)
        standing, dependency, retry, failed = self.subjects[:4]
        later = (self.now - timedelta(minutes=10)).isoformat()
        self.rows.extend([
            dict(type='autodispatch-failed', subject=standing, ts=later, reason='standing'),
            dict(type='dependency-hold', subject=dependency, ts=later, waits_for='L-spec-9999', why='fixture'),
            dict(type='escalation-blocking', kind='retries-exhausted', subject=retry, ts=later,
                 actor='tick', role='builder', reason='fixture'),
        ])
        self.failing.add(failed)
        self.steps.insert(5, self.steps[4])
        lines, elapsed, state, seats = self.run_pass()
        events = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
        success = self.subjects[4:]
        self.assertEqual(len(success), 8)
        self.assertCountEqual(self.calls, [failed] + success)
        self.assertEqual(len(self.calls), len(set(self.calls)))
        self.assertIn(f'hold {standing}: standing dispatch failure', lines)
        self.assertTrue(any(line.startswith(f'hold {dependency}: held: waits for L-spec-9999') for line in lines), lines)
        self.assertIn(f'hold {retry}: standing dispatch failure', lines)
        self.assertTrue(any(line.startswith(f'failed {failed} build:') for line in lines), lines)
        self.assertEqual([e['subject'] for e in events if e['type'] == 'autodispatch-failed'], [failed])
        self.assertCountEqual([e['subject'] for e in events if e['type'] == 'autodispatched'], success)
        self.assertEqual(state['launched'], set(success))
        self.assertEqual(seats, {'builder': 0})
        # Every successful launch used a real worktree in the same Git repository.
        for sid in success:
            wt = self.root / 'worktrees/proj' / sid.lower()
            self.assertTrue((wt / '.git').is_file(), sid)
        self.evidence('AC2', dict(successes=8, failed=[failed], guards=[standing, dependency, retry],
                                duplicate_launches=0, remaining_seats=seats['builder']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
