#!/usr/bin/env python3
"""Hermetic CPU-share regressions, including real locks and child probes."""
import contextlib
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cpu_share
import test_slots

HERE = pathlib.Path(__file__).resolve().parent
PROBE = '''import json, os, pathlib, time
p = pathlib.Path('/proc/self/autogroup')
enabled = pathlib.Path('/proc/sys/kernel/sched_autogroup_enabled')
line = p.read_text() if p.exists() else None
if os.environ.get('WAIT_AUTOGROUP') and enabled.exists() and enabled.read_text().strip() == '1':
    deadline = time.monotonic() + 5
    while not line.rstrip().endswith('nice 10') and time.monotonic() < deadline:
        time.sleep(.05)
        line = p.read_text()
print(json.dumps(dict(priority=os.getpriority(os.PRIO_PROCESS, 0),
    pid=os.getpid(), sid=os.getsid(0), pgid=os.getpgid(0),
    xdist=os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS'),
    ci=os.environ.get('CI_WORKERS'), autogroup=line)))
'''


def autogroup():
    path = pathlib.Path('/proc/self/autogroup')
    return path.read_text() if path.exists() else None


class CpuShareTests(unittest.TestCase):
    def setUp(self):
        # Defer ledger readers until collection has established test isolation.
        global build_runner, dispatch
        import build_runner
        import dispatch
        temp = tempfile.TemporaryDirectory(prefix='cpu-share-')
        self.addCleanup(temp.cleanup)
        self.top = pathlib.Path(temp.name)
        self.root = self.top / 'root'
        self.root.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=False))
        os.environ.pop('PYTEST_XDIST_AUTO_NUM_WORKERS', None)
        os.environ.pop('CI_WORKERS', None)
        self.stack.enter_context(patch.object(cpu_share.os, 'sched_getaffinity', return_value=set(range(8))))

    def claims(self):
        return list(self.root.glob('state/**/*.claim'))

    def child_claim(self):
        code = ('import sys; sys.path.insert(0,sys.argv[1]); import cpu_share; '
                'from pathlib import Path; '
                'exec("with cpu_share.claim(Path(sys.argv[2])):\\n print(\'ready\',flush=True)\\n input()")')
        child = subprocess.Popen([sys.executable, '-c', code, str(HERE), str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(stop)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        return child

    def test_AC1_share_rule_and_environment(self):
        self.assertEqual([cpu_share.workers(k, ncpu=8) for k in range(9)],
                         [8, 4, 2, 2, 1, 1, 1, 1, 1])
        for n in (1, 2):
            self.assertEqual(cpu_share.workers(1, ncpu=n), 1)
        self.assertEqual(cpu_share.workers(None, ncpu=8), 4)
        for parent, expected in [('1', 1), ('16', 4), ('0', 4), ('-3', 4), ('abc', 4), ('', 4)]:
            self.assertEqual(cpu_share.workers(1, ncpu=8, parent=parent), expected)
        for others, expected in [(0, 4), (1, 2), (3, 1), (None, 4)]:
            with patch.object(test_slots, 'held_count', return_value=others):
                self.assertEqual(cpu_share.share(self.root), expected)
            if others is not None:
                with patch.object(test_slots, 'held_count', return_value=others):
                    with cpu_share.claim(self.root) as share:
                        self.assertEqual(share, expected)
                        self.assertEqual(cpu_share.share(self.root), expected)
        source = {'A': 'b'}
        self.assertEqual(cpu_share.child_env(source, 3),
                         {'A': 'b', 'PYTEST_XDIST_AUTO_NUM_WORKERS': '3', 'CI_WORKERS': '3'})
        self.assertEqual(source, {'A': 'b'})

    def test_AC2_claim_lifetimes_threads_and_processes(self):
        self.assertEqual(cpu_share.count(self.root), 0)
        with cpu_share.claim(self.root) as share:
            self.assertEqual(share, 4)
            self.assertEqual(cpu_share.count(self.root), 1)
            own = self.claims()[0]
            self.assertEqual(own.stat().st_mode & 0o777, 0o600)
            self.assertTrue(own.exists())
            with cpu_share.claim(self.root) as nested:
                self.assertEqual(nested, 4)
                self.assertEqual(len(self.claims()), 1)
            inside, release = threading.Event(), threading.Event()
            seen = []
            def second():
                with cpu_share.claim(self.root):
                    seen.append(cpu_share.count(self.root))
                    inside.set()
                    release.wait(5)
            thread = threading.Thread(target=second)
            thread.start()
            try:
                self.assertTrue(inside.wait(5), 'second thread did not enter claim')
                self.assertEqual(seen, [2])
                self.assertEqual(len(self.claims()), 2)
                self.assertTrue(own.exists())
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.claims(), [own])
        self.assertEqual(cpu_share.count(self.root), 0)
        with cpu_share.codex_claim('builder', self.root):
            self.assertEqual(cpu_share.count(self.root), 1)
        with cpu_share.codex_claim('spec-writer', self.root):
            self.assertEqual(cpu_share.count(self.root), 0)
        one, two = self.child_claim(), self.child_claim()
        self.assertEqual(cpu_share.count(self.root), 2)
        first_files = set(self.claims())
        one.kill()
        one.communicate(timeout=5)
        self.assertEqual(cpu_share.count(self.root), 1)
        self.assertEqual(len(first_files - set(self.claims())), 1)
        two.communicate('\n', timeout=5)
        self.assertEqual(two.returncode, 0)
        self.assertEqual(cpu_share.count(self.root), 0)
        directory = next(iter(first_files)).parent
        temporary, stale = directory / '.123-x.tmp', directory / '123-x.claim'
        temporary.touch()
        stale.touch()
        self.assertEqual(cpu_share.count(self.root), 0)
        self.assertTrue(temporary.exists())
        self.assertFalse(stale.exists())

    def test_AC2_unreadable_claim_directory_is_nonblocking(self):
        (self.root / 'state').touch()
        self.assertIsNone(cpu_share.count(self.root))
        with cpu_share.claim(self.root) as share:
            self.assertEqual(share, 4)
        self.assertEqual(cpu_share.workers(cpu_share.count(self.root), ncpu=8), 4)

    def test_AC3_codex_child_priority_session_environment(self):
        before = os.getpriority(os.PRIO_PROCESS, 0), autogroup()
        with patch.object(dispatch, 'ROOT', self.root):
            row = json.loads(dispatch.run_codex_exec(
                [sys.executable, '-c', PROBE], '', str(self.top), 30).stdout)
            self.assertEqual(row['priority'], min(19, before[0] + 10))
            self.assertEqual((row['sid'], row['pgid']), (os.getsid(0), os.getpgid(0)))
            self.assertEqual((row['xdist'], row['ci']), ('4', '4'))
            self.assertEqual(row['autogroup'], before[1])
            cmd = ['codex', 'exec']
            with patch.object(dispatch.subprocess, 'run') as run:
                dispatch.run_codex_exec(cmd, '', str(self.top), 30)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/nice', '-n', '10', *cmd])
                self.assertNotIn('preexec_fn', run.call_args.kwargs)
                self.assertNotIn('start_new_session', run.call_args.kwargs)
        self.assertEqual((os.getpriority(os.PRIO_PROCESS, 0), autogroup()), before)

    def test_AC3_codex_roles_and_exception_cleanup(self):
        schema = self.top / 'schema.json'
        schema.write_text('{"type":"object","properties":{}}')
        seen = []
        def execute(cmd, packet, cwd, timeout):
            seen.append((cpu_share.count(self.root), cpu_share.share(self.root), cmd[:2]))
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(dispatch, 'ROOT', self.root), \
                patch.object(dispatch, 'SEAT', self.top / 'seat'), \
                patch.object(dispatch, 'codex_quota', return_value=None), \
                patch.object(dispatch, 'run_codex_exec', side_effect=execute) as launch:
            for role in ('builder', 'spec-writer'):
                dispatch.run_codex('fixture-' + role, role, 'fixture', schema, '', self.top, 30)
                self.assertEqual(cpu_share.count(self.root), 0)
            self.assertEqual(seen, [(1, 4, ['codex', 'exec']), (0, 4, ['codex', 'exec'])])
            launch.side_effect = RuntimeError('fixture')
            with self.assertRaises(RuntimeError):
                dispatch.run_codex('fixture-error', 'builder', 'fixture', schema, '', self.top, 30)
        self.assertEqual(cpu_share.count(self.root), 0)

    def runner_fixture(self):
        """Real command children; only Git/provision/ledger access is stubbed."""
        runner = build_runner
        content = self.root / 'content'
        content.mkdir()
        (content / 'L-spec-9000.md').write_text('fixture')
        scratch = self.top / 'scratch'
        scratch.mkdir()
        aliases, backing = self.top / 'aliases', self.top / 'backing'
        aliases.mkdir()
        backing.mkdir()
        self.stack.enter_context(patch.object(runner.fold, 'ROOT', self.root))
        self.stack.enter_context(patch.object(runner.fold, 'read_events', return_value=[]))
        self.stack.enter_context(patch.object(runner.fold, 'fold', return_value=(None, None, None, {
            'L-spec-9000': [{'type': 'build-done', 'subject': 'L-spec-9000', 'project': 'fixture',
                             'ready_sha': 'a' * 40, 'base_sha': 'b' * 40}]})))
        self.stack.enter_context(patch.object(runner.grading_env, '_project_row', return_value={}))
        self.stack.enter_context(patch.object(runner.grading_env, '_project_checkout', return_value=self.top))
        self.stack.enter_context(patch.object(runner.grading_env, 'teardown'))
        self.stack.enter_context(patch.object(runner, 'sweep_short_tmpdirs'))
        self.stack.enter_context(patch.object(runner, 'SHORT_TMP_ROOT', aliases))
        self.stack.enter_context(patch.object(runner, 'SHORT_TMP_BACKING_ROOT', backing))
        self.stack.enter_context(patch.object(runner.scratch, 'sub', return_value=scratch))
        self.stack.enter_context(patch.object(runner, '_event'))
        self.stack.enter_context(patch.object(runner, '_resolve', side_effect=lambda repo, sha, env, deadline: sha))
        commands = [('AC1', 'WAIT_AUTOGROUP=1 ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(PROBE)),
                    ('AC2', 'WAIT_AUTOGROUP=1 ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(PROBE))]
        self.stack.enter_context(patch.object(runner, '_commands', return_value=(commands, [])))
        def required(argv, **kwargs):
            if 'clone' in argv:
                pathlib.Path(argv[-1]).mkdir()
            return ''
        self.stack.enter_context(patch.object(runner, '_required', side_effect=required))
        provision = self.stack.enter_context(patch.object(runner, '_provision', return_value={}))
        process = runner._process
        def paced(argv, **kwargs):
            # Linux rate-limits unprivileged autogroup writes per uid. Space
            # these real probes; a missing production write still fails them.
            if kwargs.get('lower'):
                self.assertEqual(test_slots.held_count(self.root), 1)
                self.assertEqual(kwargs['env']['DOIT_TEST_SLOT_ROOT'], str(self.root.resolve()))
                self.assertNotEqual(kwargs['env']['DOIT_ROOT'], str(self.root))
                time.sleep(.2)
            return process(argv, **kwargs)
        self.stack.enter_context(patch.object(runner, '_process', side_effect=paced))
        tails = self.top / 'tails'
        tails.mkdir()
        return commands, provision, tails

    def check_runner_row(self, command, row, before):
        data = json.loads(pathlib.Path(row['tail']).read_text())
        self.assertEqual(row['cmd'], command)
        self.assertEqual(row['exit'], 0)
        self.assertEqual((data['xdist'], data['ci']), ('4', '4'))
        self.assertEqual(data['priority'], min(19, before[0] + 10))
        self.assertEqual(data['sid'], data['pid'])
        enabled = pathlib.Path('/proc/sys/kernel/sched_autogroup_enabled')
        if enabled.exists() and enabled.read_text().strip() == '1':
            self.assertTrue(data['autogroup'].rstrip().endswith('nice 10'), data)

    def test_AC4_runner_fixed_tip_base_share_and_cleanup(self):
        commands, provision, _ = self.runner_fixture()
        before = os.getpriority(os.PRIO_PROCESS, 0), autogroup()
        children = []
        def prepare(*args):
            self.assertEqual(test_slots.held_count(self.root), 0)
            if provision.call_count == 2:
                children.append(self.child_claim())
                self.assertEqual(cpu_share.count(self.root), 1)
            return {}
        provision.side_effect = prepare
        bundle = json.loads(build_runner.run('L-spec-9000').read_text())
        for key in ('commands', 'base'):
            self.assertEqual(len(bundle[key]), 2)
            for (_, command), row in zip(commands, bundle[key]):
                self.check_runner_row(command, row, before)
        children[0].communicate('\n', timeout=5)
        self.assertEqual(cpu_share.count(self.root), 0)
        self.assertEqual((os.getpriority(os.PRIO_PROCESS, 0), autogroup()), before)
        provision.side_effect = OSError('fixture provisioning failure')
        with self.assertRaises(build_runner.Undetermined):
            build_runner.run('L-spec-9000', 'c' * 40)
        self.assertEqual(cpu_share.count(self.root), 0)
        self.assertEqual(test_slots.held_count(self.root), 0)

    def test_workers_follow_held_slots_not_claims(self):
        with cpu_share.claim(self.root), cpu_share.claim(self.root):
            self.assertEqual(cpu_share.share(self.root), 4)
            with test_slots.acquire('builder', 1, root=self.root):
                self.assertEqual(cpu_share.share(self.root), 4)
                with test_slots.acquire('grader', 1, root=self.root):
                    self.assertEqual(cpu_share.share(self.root), 2)
                    self.assertEqual(cpu_share.launch_env({}, self.root)['CI_WORKERS'], '2')
        self.assertEqual(test_slots.held_count(self.root), 0)

    def test_grader_run_checks_role(self):
        self.runner_fixture()
        bundle = json.loads(build_runner.run('L-spec-9000', criteria={'AC1'}).read_text())
        log = [json.loads(line) for line in (self.root / 'state/test-slots/queue.jsonl').read_text().splitlines()]
        self.assertTrue(bundle['commands'])
        self.assertEqual({row['role'] for row in log}, {'grader'})

    def test_AC4_direct_nine_argument_run_tree(self):
        commands, _, tails = self.runner_fixture()
        before = os.getpriority(os.PRIO_PROCESS, 0), autogroup()
        rows, *_ = build_runner._run_tree('L-spec-9000', 'd' * 40, self.top, {}, commands,
                                         tails, 0, 65536, time.monotonic() + 30)
        for (_, command), row in zip(commands, rows):
            self.check_runner_row(command, row, before)
        self.assertEqual(cpu_share.count(self.root), 0)
        self.assertEqual((os.getpriority(os.PRIO_PROCESS, 0), autogroup()), before)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CpuShareTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        for ac in ('AC1', 'AC2', 'AC3', 'AC4'):
            body = json.dumps({'criterion': ac, 'suite': 'test_cpu_share.py', 'passed': True}, sort_keys=True)
            print(json.dumps({'url': 'fixture://cpu-share/' + ac, 'status': 200,
                              'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                              'body_excerpt': body}))
    sys.exit(not result.wasSuccessful())
