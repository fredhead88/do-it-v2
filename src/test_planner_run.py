#!/usr/bin/env python3
"""Local process/ledger regression selectors; no vendor calls or live ledger writes."""
import atexit
import ctypes
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
_ISO = tempfile.TemporaryDirectory(prefix='planner-suite-')
atexit.register(_ISO.cleanup)
os.environ['HOME'] = str(pathlib.Path(_ISO.name) / 'home')
os.environ['DOIT_ROOT'] = str(pathlib.Path(_ISO.name) / 'ledger')
sys.path.insert(0, str(REPO / 'src'))
import dispatch, fold, launch, pane_resume, panes, up

FAKE = '''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys, time
if sys.argv[1:] == ['--version']:
    print('fixture-cli'); sys.exit(0)
root = pathlib.Path(os.environ['DOIT_ROOT'])
ledger = root / 'events' / os.environ['DOIT_LEDGER_FILE']
rows = [json.loads(s) for s in ledger.read_text().splitlines()]
assert rows[0]['type'] == 'planner-started'
data = dict(pid=os.getpid(), cwd=os.getcwd(), argv=sys.argv[1:], ledger=ledger.name,
            supervised=os.environ.get('DOIT_SUPERVISED'), path=os.environ['PATH'],
            root=str(root), stripped='PLANNER_STRIP_TEST' not in os.environ,
            configured=os.environ.get('PLANNER_SET_TEST'),
            scratch=os.environ.get('TMPDIR'), claude_scratch=os.environ.get('CLAUDE_CODE_TMPDIR'))
if os.environ.get('TEST_GRANDCHILD'):
    child = subprocess.Popen(['/bin/sleep', '120'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data['grandchild'] = child.pid
(root / 'probe.json').write_text(json.dumps(data))
if os.environ.get('TEST_CHILD_END'):
    sys.path.insert(0, os.environ['TEST_SOURCE'])
    import dispatch
    dispatch.emit(ledger, {'spawn': ledger.stem}, 'planner-ended',
                  subject=rows[0]['subject'], planner=ledger.stem, mode=rows[0]['mode'], reason='ctx')
release = os.environ.get('TEST_RELEASE')
while release and not pathlib.Path(release).exists(): time.sleep(.01)
time.sleep(float(os.environ.get('TEST_SLEEP') or '0'))
sys.exit(int(os.environ.get('TEST_RC', '0')))
'''


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix='planner-case-'))
        self.base = pathlib.Path(self.tmp)
        self.root = self.base / 'ledger'
        self.events = self.root / 'events'
        self.events.mkdir(parents=True)
        self.home = self.base / 'home'
        self.sessions = self.home / '.claude/sessions'
        self.sessions.mkdir(parents=True)
        self.cli = self.base / 'bin/claude'
        self.cli.parent.mkdir()
        self.cli.write_text(FAKE)
        self.cli.chmod(0o755)
        cfg = self.base / 'launch.toml'
        cfg.write_text('[roles.planner]\nstrip=["PLANNER_STRIP_TEST"]\n'
                       '[roles.planner.set]\nPLANNER_SET_TEST="configured"\n')
        env = dict(HOME=str(self.home), DOIT_ROOT=str(self.root), DOIT_CLAUDE_BIN=str(self.cli),
                   DOIT_SCRATCH=str(self.base / 'scratch'), DOIT_LAUNCH_TOML=str(cfg),
                   DOIT_TICK_MIN='5', PLANNER_STRIP_TEST='synthetic-strip-marker',
                   TEST_SOURCE=str(REPO / 'src'))
        for key in list(os.environ):
            if key.startswith('TEST_') or key.startswith('SUPABASE_DB_URL'):
                env[key] = ''
        env['TEST_SOURCE'] = str(REPO / 'src')
        self.enterContext(patch.dict(os.environ, env))
        for obj, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.events),
                                (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.events),
                                (panes, 'SESSIONS', self.sessions),
                                (launch, 'CLAUDE_CONFIG', self.home / '.claude.json')):
            self.enterContext(patch.object(obj, key, value))
        self.now = datetime.now(timezone.utc)

    def wrapper(self, **env):
        return subprocess.Popen([str(REPO / 'doit'), 'planner-run', 'L-charter-9001',
                                 '--mode', 'charter', '--attempt', '1'],
                                env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def finish(self, proc, expected=0):
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, expected, (out.decode(), err.decode()))

    def rows(self):
        return [json.loads(line) for file in sorted(self.events.glob('*.jsonl'))
                for line in file.read_text().splitlines() if line.strip()]

    def wait_probe(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                return json.loads((self.root / 'probe.json').read_text())
            except (FileNotFoundError, ValueError):
                time.sleep(.01)
        self.fail('synthetic child never started')

    def lock_free(self):
        fd = os.open(self.root / 'planner.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        finally:
            os.close(fd)

    def held(self):
        fd = os.open(self.root / 'planner.lock', os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def end(self, reason='ctx'):
        row = dict(v=1, type='planner-ended', subject='L-charter-9001', planner='L-planner-9001',
                   reason=reason, ts=(self.now - timedelta(minutes=2)).isoformat())
        path = self.events / 'L-planner-9001.jsonl'
        path.write_text(json.dumps(row) + '\n')
        return row


class AC1(Scratch):
    def test_durable_lifetimes(self):
        for rc, child_end in ((0, False), (7, False), (143, False), (0, True)):
            with self.subTest(rc=rc, child_end=child_end):
                proc = self.wrapper(TEST_RC=str(rc), TEST_CHILD_END='1' if child_end else '')
                self.finish(proc)
        # A separate interpreter reopens every file and asserts persisted correlation.
        check = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); launch=[json.loads(x) for x in (p/'L-launch-local.jsonl').read_text().splitlines()]
files=sorted(p.glob('L-planner-*.jsonl')); assert len(files)==4
for file,rc,reason in zip(files,[0,7,143,0],['exit-0','exit-7','exit-143','ctx']):
 rows=[json.loads(x) for x in file.read_text().splitlines()]
 assert [r['type'] for r in rows]==['planner-started','planner-ended']
 start,end=rows; assert start['planner']==start['spawn']==end['planner']==file.stem
 assert start['subject']==end['subject']=='L-charter-9001'
 assert start['mode']==end['mode']=='charter' and start['attempt']==1 and start['code_sha']
 assert end['reason']==reason
 launched=[r for r in launch if r['type']=='role-launched' and r['pane']==file.stem]
 ended=[r for r in launch if r['type']=='role-ended' and r['pane']==file.stem]
 assert len(launched)==len(ended)==1 and launched[0]['pid']>0 and ended[0]['rc']==rc
print('independent persisted read: exits 0,7,143; child-authored end; matching lifecycle/launch identities')
'''
        result = subprocess.run(['/usr/bin/python3', '-c', check, str(self.events)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        print(result.stdout.strip())
        probe = self.wait_probe()
        last = [r for r in self.rows() if r['type'] == 'role-launched'][-1]
        self.assertEqual(last['pid'], probe['pid'])

    def test_popen_failure_closes_start(self):
        import planner_run
        with patch.object(up.subprocess, 'Popen', side_effect=OSError('synthetic spawn failure')):
            self.assertNotEqual(planner_run.main(['L-charter-9001', '--mode', 'charter', '--attempt', '1']), 0)
        rows = self.rows()
        self.assertEqual([r['type'] for r in rows], ['planner-started', 'planner-ended'])
        self.assertEqual(rows[-1]['reason'], 'launch-failed')
        self.assertTrue(self.lock_free())


class AC2(Scratch):
    def kill_owned(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_wrapper_only_lock_normal_and_killed(self):
        self.assertEqual(ctypes.CDLL(None).prctl(36, 1, 0, 0, 0), 0)  # reap owned orphan fixtures
        for killed in (False, True):
            with self.subTest(killed=killed):
                (self.root / 'probe.json').unlink(missing_ok=True)
                release=self.root/('release-'+str(killed))
                owner = self.wrapper(TEST_RELEASE=str(release), TEST_GRANDCHILD='1')
                self.addCleanup(lambda p=owner: p.poll() is None and (p.kill(), p.wait()))
                probe = self.wait_probe()
                self.addCleanup(self.kill_owned, probe['grandchild'])
                if killed:
                    self.addCleanup(self.kill_owned, probe['pid'])
                self.assertFalse(self.lock_free())
                before = len(list(self.events.glob('L-planner-*.jsonl')))
                loser = self.wrapper()
                self.finish(loser, 3)
                self.assertEqual(len(list(self.events.glob('L-planner-*.jsonl'))), before)
                if killed:
                    owner.kill()
                    owner.wait(timeout=3)
                    owner.stdout.close(); owner.stderr.close()
                else:
                    release.touch()
                    self.finish(owner)
                os.kill(probe['grandchild'], 0)
                self.assertTrue(self.lock_free(), 'descendants must not inherit the descriptor')
                (self.sessions / f"{probe['grandchild']}.json").write_text(json.dumps({'name':'L-planner-9001'}))
                self.assertIs(up.planner_alive(), True)
                (self.sessions / f"{probe['grandchild']}.json").unlink()
                self.assertIs(up.planner_alive(), False)

    def test_liveness_matrix(self):
        self.assertIs(up.planner_alive(), False)
        self.assertFalse((self.root / 'planner.lock').exists(), 'probe leaves no lock')
        fd = self.held()
        self.assertIs(up.planner_alive(), True)
        fcntl.flock(fd, fcntl.LOCK_UN)
        for name, expected in (('L-planner-9001', True), ('L-relay-9001', False)):
            (self.sessions / f'{os.getpid()}.json').write_text(json.dumps({'name':name}))
            self.assertIs(up.planner_alive(), expected)
        (self.sessions / f'{os.getpid()}.json').unlink()
        self.assertIs(up.planner_alive(), False)
        lock = self.root / 'planner.lock'
        lock.chmod(0)
        self.addCleanup(lock.chmod, 0o600)
        self.assertIsNone(up.planner_alive())
        lock.chmod(0o600)
        meta = self.sessions / '123.json'
        meta.write_text('{')
        self.assertIsNone(up.planner_alive())
        self.assertEqual(up.launch_planner('L-charter-9001', 'charter', 1), 'undetermined')
        meta.write_text('{}')
        meta.chmod(0)
        self.assertIsNone(up.planner_alive())
        meta.chmod(0o600)
        print('liveness: held/no pane; free/planner,relay,none; missing; unreadable lock/metadata')


class AC3(Scratch):
    def launch(self, launcher=None, now=None):
        return up.launch_planner('L-charter-9001', 'ctx-restart', 2,
                                 launcher=launcher or self.calls.append, now=now or self.now)

    def setUp(self):
        super().setUp()
        self.calls = []

    def test_outcomes_and_marker_recovery(self):
        fd = self.held()
        self.assertEqual(self.launch(), 'alive')
        fcntl.flock(fd, fcntl.LOCK_UN)
        with patch.object(up, 'planner_alive', return_value=None):
            self.assertEqual(self.launch(), 'undetermined')
        self.assertEqual(self.launch(), 'started')
        argv = self.calls[0]
        self.assertEqual(argv[-6:], ['planner-run', 'L-charter-9001', '--mode', 'ctx-restart', '--attempt', '2'])
        self.assertNotIn('claude', argv)
        self.assertEqual(self.launch(), 'starting')
        marker = self.root / 'planner.starting'
        stamp = self.now.timestamp() - 301
        os.utime(marker, (stamp, stamp))
        self.assertEqual(self.launch(), 'started')
        future = self.now.timestamp() + 1
        os.utime(marker, (future, future))
        self.assertEqual(self.launch(), 'undetermined')
        marker.unlink()
        self.assertEqual(self.launch(launcher=lambda argv: (_ for _ in ()).throw(OSError())), 'undetermined')
        marker.unlink()
        with patch.dict(os.environ, {'DOIT_TICK_MIN':'nan'}):
            self.assertEqual(self.launch(), 'undetermined')
        with patch.object(up.os, 'open', side_effect=FileExistsError):
            self.assertEqual(self.launch(), 'undetermined')

    def test_two_real_processes_admit_once(self):
        code = '''import pathlib,sys,time
sys.path.insert(0,sys.argv[1]); import up
print(up.launch_planner('L-charter-9001','charter',1,launcher=lambda argv: pathlib.Path(sys.argv[2]).open('a').write('launch\\n')))
'''
        log = self.root / 'admitted'
        procs = [subprocess.Popen(['/usr/bin/python3','-c',code,str(REPO/'src'),str(log)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for proc in procs:
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn(out.strip(), (b'started', b'starting', b'undetermined'))
        self.assertEqual(log.read_text().splitlines(), ['launch'])

    def test_planner_tmux_cwd_and_relay_unchanged(self):
        for present in (0, 1):
            calls=[]
            def runner(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, present if argv[1]=='has-session' else 0)
            up.planner_launcher(['echo', 'literal ; $()'], runner=runner)
            for argv in calls[1:]:
                self.assertEqual(argv[argv.index('-c')+1], str(REPO))
            self.assertIn("'literal ; $()'", calls[-1][-1])
            calls.clear()
            up.default_launcher(['doit','relay'], runner=runner)
            self.assertEqual(calls[-1], ['tmux','new-window','-d','-t','do-it-relay','doit','relay'])
            self.assertTrue(all('-c' not in argv for argv in calls))


class AC4(Scratch):
    def test_real_readers_unconfirmed_and_confirmed(self):
        end = self.end()
        dst = self.events / pane_resume.LEDGER_NAME
        for outcome, reason in ((False,'no-restart-entry'), (None,'restart-unconfirmed'), ('error','restart-undetermined')):
            def restart(name):
                if outcome == 'error':
                    raise OSError()
                return outcome
            resumed, skipped = pane_resume._restart_ctx([end], [], self.now, dst, restart)
            self.assertEqual((resumed,skipped), ([],[end['planner']]))
            self.assertEqual(self.rows()[-1]['reason'], reason)
        self.assertTrue(pane_resume.ctx_ended_without_successor([end, *self.rows()], self.now))
        with patch.object(up, 'launch_planner', return_value='started'), \
             patch.object(up, 'RESTART_CONFIRM_SECONDS', .03):
            resumed, skipped = pane_resume._restart_ctx([end], [], self.now, dst, up.restart_pane)
            self.assertFalse(resumed)
            self.assertTrue(skipped)
        self.assertEqual(len(list(self.events.glob('L-planner-*.jsonl'))), 1)
        def actual_launch(argv):
            self.finish(subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        with patch.object(up, 'planner_launcher', side_effect=actual_launch):
            self.assertEqual(pane_resume._restart_ctx([end],[],self.now,dst,up.restart_pane),([end['planner']],[]))
        self.assertEqual(pane_resume._restart_ctx([end],[],self.now,dst,lambda name:self.fail('stale snapshot repeated')),([],[]))
        # The real production reader sees the confirmed successor, including a stale input snapshot.
        self.assertFalse(pane_resume.ctx_ended_without_successor(fold.read_events(use_cache=False), self.now+timedelta(seconds=2)))
        with patch.object(up, 'launch_planner') as launch_again:
            self.assertFalse(up.restart_pane(end['planner']))
            launch_again.assert_not_called()

    def test_lost_admission_and_early_failure_remain_recoverable(self):
        import relay
        (self.root / 'repos' / 'do-it-v2').mkdir(parents=True)
        for fail in ('occupied','setup'):
            with self.subTest(fail=fail):
                end=self.end()
                marker=self.root/'planner.starting'
                marker.unlink(missing_ok=True)
                def launcher(argv):
                    if fail=='occupied':
                        fd=self.held()
                        try:
                            self.finish(subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE),3)
                        finally:
                            fcntl.flock(fd,fcntl.LOCK_UN)
                    else:
                        self.finish(subprocess.Popen(argv,env={**os.environ,'HOME':'/dev/null'},
                                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE),1)
                dst=self.events/pane_resume.LEDGER_NAME
                with patch.object(up,'planner_launcher',side_effect=launcher), patch.object(up,'RESTART_CONFIRM_SECONDS',.03):
                    self.assertEqual(pane_resume._restart_ctx([end],[],self.now,dst,up.restart_pane), ([],[end['planner']]))
                self.assertTrue(pane_resume.ctx_ended_without_successor([end,*pane_resume._resume_rows(dst)],self.now))
                self.assertFalse(any(r['type']=='pane-resumed' for r in self.rows()))
                # Real relay reader with a complete synthetic charter: loss must not consume a start/attempt.
                dispatch.emit(self.events/'L-operator-9001.jsonl',{},'charter-filed',
                              subject=end['subject'],project='do-it-v2')
                rows=fold.read_events(use_cache=False)
                self.assertEqual(relay.planner_attempts(rows,charter_id=end['subject'])['next'],'start')
                self.assertIn(end['subject'], relay.plannable(rows, self.root)[0])
                with patch.object(up,'maybe_reexec'):
                    up._cycle({'up':None,'watermark':None,'said':None},None)
                self.assertEqual(len([r for r in self.rows() if r['type']=='planner-started']),1)
                for file in self.events.glob('*.jsonl'):
                    file.unlink()

    def test_restart_eligibility_and_distinct_admission_outcomes(self):
        for reason in ('exit-0','ctx','held'):
            row=self.end(reason)
            for result, expected in (('alive',False),('starting',False),('undetermined','error')):
                with patch.object(up,'launch_planner',return_value=result):
                    if reason!='exit-0' and expected=='error':
                        with self.assertRaises(RuntimeError): up.restart_pane(row['planner'])
                    else:
                        self.assertIs(up.restart_pane(row['planner']),False)
        for bad in ({'ts':'bad'},{'subject':''}):
            row=self.end(); row.update(bad)
            (self.events/(row['planner']+'.jsonl')).write_text(json.dumps(row)+'\n')
            with patch.object(up,'launch_planner') as launcher:
                self.assertFalse(up.restart_pane(row['planner']))
                launcher.assert_not_called()
        self.assertFalse(up.restart_pane('L-relay-9001'))
        self.assertFalse(up.restart_pane('L-planner-9999'))

    def test_old_or_unrelated_start_cannot_confirm(self):
        end=self.end()
        old=dict(type='planner-started',subject=end['subject'],planner='L-planner-9002',
                 spawn='L-planner-9002',mode='ctx-restart',attempt=1,wrapper_pid=os.getpid(),
                 ts=(self.now-timedelta(minutes=3)).isoformat(),v=1)
        (self.events/'L-planner-9002.jsonl').write_text(json.dumps(old)+'\n')
        def unrelated(*args):
            dispatch.emit(self.events/'L-planner-9003.jsonl',{'spawn':'L-planner-9003'},'planner-started',
                          subject='L-charter-9999',planner='L-planner-9003',mode='ctx-restart',attempt=1,
                          wrapper_pid=os.getpid())
            return 'started'
        with patch.object(up,'launch_planner',side_effect=unrelated), patch.object(up,'RESTART_CONFIRM_SECONDS',.03):
            self.assertIsNone(up.restart_pane(end['planner']))
        with patch.object(up,'launch_planner',return_value='started'), \
             patch.object(fold,'read_events',side_effect=[fold.read_events(use_cache=False),OSError('unreadable')]):
            with self.assertRaises(OSError): up.restart_pane(end['planner'])

    def test_wrapper_handover_behaviour(self):
        handover=self.root/'handover with spaces.md'
        handover.write_text('resume')
        for variant in ('readable','missing','empty','unrelated'):
            row=self.end()
            row['handover']=str(handover)
            if variant=='missing': row['handover']=str(self.root/'absent')
            if variant=='empty': handover.write_text('')
            (self.events/(row['planner']+'.jsonl')).write_text(json.dumps(row)+'\n')
            if variant=='unrelated':
                dispatch.emit(self.events/(row['planner']+'.jsonl'),{},'planner-ended',subject=row['subject'],reason='exit-0')
            self.finish(self.wrapper())
            prompt=self.wait_probe()['argv'][-1]
            self.assertEqual(str(handover) in prompt,variant=='readable')
            for path in self.events.glob('*.jsonl'): path.unlink()


class AC5(Scratch):
    def test_dispatch_environment_and_literal_arguments(self):
        # Runner scratch paths can contain symlinks; child_env resolves them.
        scratch_target = self.base / 'runner-tmp'
        scratch_target.mkdir()
        (self.base / 'scratch').symlink_to(scratch_target, target_is_directory=True)
        self.finish(self.wrapper())
        probe=self.wait_probe()
        self.assertEqual(probe['supervised'],'1')
        self.assertEqual(probe['cwd'],str(REPO))
        self.assertEqual(probe['root'],str(self.root))
        self.assertTrue(probe['path'].startswith(str(REPO)+':'))
        self.assertTrue(probe['stripped'])
        self.assertEqual(probe['configured'],'configured')
        expected_scratch = str((self.base / 'scratch').resolve())
        self.assertEqual(probe['scratch'], expected_scratch)
        self.assertEqual(probe['claude_scratch'], expected_scratch)
        self.assertEqual(probe['argv'][probe['argv'].index('--agent')+1],'planner')
        self.assertEqual(probe['argv'][probe['argv'].index('-n')+1]+'.jsonl',probe['ledger'])
        self.assertEqual(probe['argv'][-1],'L-charter-9001')
        self.assertIn('--settings',probe['argv'])
        self.assertNotIn('--model',probe['argv'])
        self.assertNotIn('synthetic-strip-marker',json.dumps(self.rows())+json.dumps(probe))
        sentinel=self.root/'injection'
        for charter,mode,attempt in ((f'L-charter-9001; touch {sentinel}','charter','1'),
                                     ('L-charter-9001','$(echo bad)','1'),
                                     ('L-charter-9001','charter','0'),
                                     ('L-charter-9001','charter','-1'),
                                     ('L-charter-9001','charter','1; echo nope')):
            result=subprocess.run([str(REPO/'doit'),'planner-run',charter,'--mode',mode,'--attempt',attempt],
                                  capture_output=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
        self.assertFalse(sentinel.exists())
        with patch.object(up.launch,'model_for',return_value='configured-model'):
            cmd=up.pane_cmd('literal ; $() \"prompt\"',name='L-planner-9001',model=launch.model_for('planner'))
        self.assertEqual(cmd[-1],'literal ; $() \"prompt\"')
        self.assertEqual(cmd[cmd.index('--model')+1],'configured-model')


class AC6(Scratch):
    def test_cross_path_lock_and_supervisor_cwd(self):
        fd=self.held()
        self.assertEqual(up._start({},'L-charter-9001','charter',1,'L-charter-9001'),up.PLANNER_OCCUPIED)
        self.finish(self.wrapper(),3)
        self.assertFalse(self.rows())
        fcntl.flock(fd,fcntl.LOCK_UN)
        self.assertEqual(ctypes.CDLL(None).prctl(36,1,0,0,0),0)
        release=self.root/'release'
        os.environ['TEST_RELEASE']=str(release)
        os.environ['TEST_GRANDCHILD']='1'
        # A separate supervisor parent owns the same non-inheritable lock.
        code="import sys;sys.path.insert(0,sys.argv[1]);import up;up._start({},'L-charter-9001','charter',1,'L-charter-9001')"
        owner=subprocess.Popen(['/usr/bin/python3','-c',code,str(REPO/'src')],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        probe=self.wait_probe()
        self.addCleanup(AC2.kill_owned,self,probe['grandchild'])
        self.addCleanup(lambda:owner.poll() is None and (owner.kill(),owner.wait()))
        self.finish(self.wrapper(),3)
        self.assertEqual(up.launch_planner('L-charter-9001','charter',1),'alive')
        release.touch()
        self.finish(owner)
        os.kill(probe['grandchild'],0)
        self.assertEqual(self.wait_probe()['cwd'],str(REPO))
        self.assertTrue(self.lock_free())

    def cycles(self, real=False):
        fd=self.held()
        clock=[0.0]; stamps=[]; counts=[]
        read_events=fold.read_events
        def read():
            stamps.append(time.monotonic() if real else clock[0])
            dispatch.emit(self.events/'L-up-local.jsonl',{},'supervisor-code',subject='planner',
                          kind='planner',sha='undetermined',pid=os.getpid(),reason='test-watermark-change')
            return read_events(use_cache=False)
        def guarded(fn,*a,**kw):
            counts.append(fn)
            return (True, (['L-charter-9001'],[]) if fn=='plannable' else
                    {'next':'start','attempts':0} if fn=='planner_attempts' else (True,len(counts)))
        with ExitStack() as stack:
            stack.enter_context(patch.object(up,'install'))
            stack.enter_context(patch.object(up,'maybe_reexec'))
            stack.enter_context(patch.object(up,'code_sha',return_value=None))
            stack.enter_context(patch.object(fold,'read_events',side_effect=read))
            stack.enter_context(patch.object(up,'_guarded',side_effect=guarded))
            stack.enter_context(patch.object(up,'_interval',return_value=.05 if real else 3.0))
            if not real:
                stack.enter_context(patch.object(up,'_SLEEP',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)))
            self.assertEqual(up.main(max_cycles=3),3)
        self.assertEqual(counts.count('plannable'),3)
        self.assertEqual(len(stamps),3)
        self.assertGreaterEqual(stamps[2]-stamps[0],.09 if real else 6)
        self.assertFalse(any(r['type']=='planner-started' for r in self.rows()))
        fcntl.flock(fd,fcntl.LOCK_UN)
        up._start({},'L-charter-9001','charter',1,'L-charter-9001')
        self.assertEqual(len([r for r in self.rows() if r['type']=='planner-started']),1)

    def test_deterministic_occupied_cycles(self):
        self.cycles()

    def test_real_clock_bounded_cycles(self):
        before=time.monotonic()
        previous=signal.signal(signal.SIGALRM,lambda *args:(_ for _ in ()).throw(AssertionError('5-second bound')))
        signal.setitimer(signal.ITIMER_REAL,5)
        try:
            self.cycles(real=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous)
        self.assertLess(time.monotonic()-before,5)

    def test_existing_footprint_suites(self):
        for name in ('test_up.py','test_pane_resume.py'):
            env={k:v for k,v in os.environ.items() if not k.startswith('TEST_')
                 and k not in ('DOIT_LAUNCH_TOML','DOIT_CLAUDE_BIN')}
            result=subprocess.run(['/usr/bin/python3',str(REPO/'src'/name)],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout[-1800:]+result.stderr[-1800:])
            print(name+': exit 0')


if __name__ == '__main__':
    selections=sys.argv[1:] or ['AC1','AC2','AC3','AC4','AC5','AC6']
    if any(name not in ('AC1','AC2','AC3','AC4','AC5','AC6') for name in selections):
        raise SystemExit('expected AC1 through AC6')
    for name in selections:
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(globals()[name])
        names=[test.id().rsplit('.',1)[-1] for test in suite]
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        body=json.dumps({'criterion':name,'cases':names,'tests':result.testsRun,'success':result.wasSuccessful()},sort_keys=True)
        print(json.dumps(dict(url='cli://L-spec-2167/'+name,status=0 if result.wasSuccessful() else 1,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),body_excerpt=body)))
        if not result.wasSuccessful(): raise SystemExit(1)
