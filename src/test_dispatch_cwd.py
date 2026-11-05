"""Relative planner cwd and Codex ENOENT use the same absolute spawn path."""
import argparse
import contextlib
import hashlib
import runpy
import sys
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

_import_root = tempfile.TemporaryDirectory(prefix='cwd-test-import-')
os.environ['DOIT_ROOT'] = _import_root.name
os.environ.pop('DOIT_LEDGER_FILE', None)
os.environ['DOIT_NO_POKE'] = '1'
import dispatch
import fold
import models
import spawns


class CwdTests(unittest.TestCase):
    def test_root_then_process_resolution(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'root'
            repo = root/'repos/project'
            repo.mkdir(parents=True)
            local = Path(name)/'local'
            local.mkdir()
            before = Path.cwd()
            try:
                os.chdir(local)
                self.assertEqual(spawns.resolve_cwd('repos/project', root), str(repo))
                self.assertEqual(spawns.resolve_cwd('missing', root), str(local/'missing'))
                self.assertEqual(spawns.resolve_cwd(None, root), str(local))
                self.assertEqual(spawns.resolve_cwd(str(repo), root), str(repo))
            finally:
                os.chdir(before)

    def test_cmd_json_and_launch_failure_are_classified(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root/'repos/project'
            repo.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            packet = root/'packet.md'
            packet.write_text('Review this fixture.')
            (root/'seat').mkdir()
            args = argparse.Namespace(role='reviewer', subject='L-spec-9955', packet=str(packet),
                cwd='repos/project', project='project', charter=None, path=None,
                mcp_config=None, timeout=None, max_usd=None, seat=False)
            mapping = {'backend': 'codex', 'model': 'fixture', 'source': 'fixture'}
            result = subprocess.CompletedProcess([], 1, '', 'Error: No such file or directory (os error 2)')
            with patch.multiple(dispatch, ROOT=root, EVENTS=root/'events', CONTENT=root/'content', SEAT=root/'seat'), \
                 patch.multiple(fold, ROOT=root, EVENTS=root/'events'), \
                 patch.object(models, 'resolve', return_value=mapping), \
                 patch.object(dispatch, 'codex_quota', return_value=None), \
                 patch.object(dispatch, 'relay_serving', return_value=False), \
                 patch.object(dispatch, 'run_codex_exec', return_value=result) as backend, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    dispatch.main(args)
                self.assertEqual(stopped.exception.code, 2)
                backend.assert_called_once()
                rows = fold.read_events(use_cache=False)
            self.assertEqual(rows[-1]['type'], 'infra-failure')
            self.assertFalse(any(e.get('kind') == 'failure-unknown' for e in rows))
            cmd, = list((root/'seat').glob('*.cmd.json'))
            metadata = json.loads(cmd.read_text())
            self.assertEqual(metadata['cwd'], str(repo))
            self.assertEqual(metadata['cmd'][metadata['cmd'].index('-C')+1], str(repo))
            self.assertEqual(backend.call_args.args[2], str(repo))


def signed(criterion, body):
    print(json.dumps(dict(criterion=criterion, url='cli://dispatch-cwd/' + criterion,
        status=0, body_sha256=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
        body_excerpt=body)))


class DefaultCwdTests(unittest.TestCase):
    def test_role_caller_matrix(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            callers = [root/'repos/project', root/'repos/other', root/'outside']
            for caller in callers:
                caller.mkdir(parents=True)
            with contextlib.chdir(callers[0]):
                for role in dispatch.ROLES:
                    expected = root/('worktrees/project/l-spec-9955' if role == 'builder' else 'repos/project')
                    for caller in callers:
                        with self.subTest(role=role, caller=caller), contextlib.chdir(caller):
                            actual = spawns.default_cwd(role, 'L-spec-9955', 'project', root)
                            self.assertEqual(actual, str(expected))
                            signed('AC1', dict(role=role, caller=str(caller), expected=str(expected), actual=actual))
                self.assertFalse((root/'worktrees/project/l-spec-9955').exists())

    def test_invalid_components(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root/'repos/project').mkdir(parents=True)
            for project in (None, '', 'missing', '.', '..', '/absolute', '../project', 'a/b', 'a\\b'):
                with self.subTest(project=project), self.assertRaises(ValueError):
                    spawns.default_cwd('builder', 'L-spec-9955', project, root)
            for subject in ('', '.', '..', '/absolute', '../escape', 'a/b', 'a\\b'):
                with self.subTest(subject=subject), self.assertRaises(ValueError):
                    spawns.default_cwd('builder', subject, 'project', root)


class CapturedLaunch(BaseException):
    """Stop at the local process boundary, before a model can run."""


class CwdFixture:
    subject = 'L-spec-9955'

    @contextlib.contextmanager
    def fixture(self, ledger_project='project', worktree=True):
        import builder_deps
        import packet as packet_module
        with tempfile.TemporaryDirectory(prefix='dispatch-cwd-') as name, contextlib.ExitStack() as stack:
            root = Path(name).resolve()
            scratch_path = stack.enter_context(tempfile.TemporaryDirectory(prefix='dispatch-cwd-scratch-'))
            repo = root/'repos/project'
            repo.mkdir(parents=True)
            other = root/'repos/other'
            other.mkdir()
            outside = root/'outside'
            outside.mkdir()
            self.root, self.repo = root, repo
            self.callers = [repo, other, outside]
            self.tree = root/'worktrees/project'/self.subject.lower()
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q', '-b', 'fixture-main')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '--allow-empty', '-qm', 'fixture')
            self.pin = git('rev-parse', 'HEAD')
            if worktree:
                git('worktree', 'add', '-qb', self.subject.lower(), str(self.tree))
            self.packet = root/'packet.md'
            self.packet.write_text('Synthetic packet.\nPINNED_BASE_SHA: ' + self.pin + '\n')
            for directory in ('events', 'content', 'seat'):
                (root/directory).mkdir()
            stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE='1',
                                           DOIT_SUPERVISED='1', HOME=str(root/'home'), DOIT_SCRATCH=scratch_path))
            for key in ('DOIT_LEDGER_FILE', 'DOIT_SEAT', 'DOIT_PROJECT'):
                os.environ.pop(key, None)
            stack.enter_context(patch.multiple(dispatch, ROOT=root, EVENTS=root/'events',
                                               CONTENT=root/'content', SEAT=root/'seat'))
            stack.enter_context(patch.multiple(fold, ROOT=root, EVENTS=root/'events'))
            stack.enter_context(patch.object(dispatch.atexit, 'register'))
            stack.enter_context(patch.object(models, 'resolve', return_value=dict(
                backend='codex', model='fixture', source='fixture')))
            stack.enter_context(patch.object(dispatch, 'ensure_room', return_value=True))
            stack.enter_context(patch.object(packet_module, '_effective_base', return_value=self.pin))
            self.effective = packet_module._effective_base
            stack.enter_context(patch.object(builder_deps, 'prepare', wraps=builder_deps.prepare))
            self.deps = builder_deps.prepare
            stack.enter_context(patch.object(dispatch, 'provision_worktree_env', return_value='absent'))
            self.provision = dispatch.provision_worktree_env
            if ledger_project in ('seat', 'src', '.do-it'):
                self.seed('spawn-done', actor='research', project=ledger_project)
            elif ledger_project is not None:
                self.seed('spec-written', project=ledger_project)
                self.seed('spawn-done', actor='spec-auditor', bad_cut=False)
            real_run = subprocess.run
            self.launches = []
            def local_run(cmd, *args, **kwargs):
                if cmd[:2] == ['claude', '--version']:
                    return subprocess.CompletedProcess(cmd, 0, 'fixture', '')
                if cmd and cmd[0] == 'git':
                    return real_run(cmd, *args, **kwargs)
                if 'codex' in cmd and 'exec' in cmd:
                    self.launches.append(dict(cmd=cmd, cwd=kwargs['cwd']))
                    raise CapturedLaunch()
                raise AssertionError('unexpected external command: ' + str(cmd))
            stack.enter_context(patch.object(subprocess, 'run', side_effect=local_run))
            yield

    def seed(self, kind, actor='spec-writer', **kv):
        target = self.root/'events'/f'L-{actor}-9900.jsonl'
        error = dispatch.emit(target, dict(subject=self.subject), kind, **kv)
        self.assertIsNone(error)
        self.assertTrue(any(e['type'] == kind for e in fold.read_events(use_cache=False)))

    def args(self, role='builder', project=None, cwd=None, stdin=False):
        return argparse.Namespace(role=role, subject=self.subject, packet='-' if stdin else str(self.packet),
            cwd=cwd, project=project, charter=None, path=(str(self.root/'content'/f'{self.subject}.md') if role == 'spec-writer'
                  else str(self.root/'output') if role == 'probe' else None), mcp_config=None, timeout=None, max_usd=None,
            seat=False, augment_history=False)

    def invoke(self, args, caller, route='direct'):
        self.stderr = io.StringIO()
        with contextlib.chdir(caller), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(self.stderr), patch.object(sys, 'stdin', io.StringIO(self.packet.read_text())):
            try:
                if route == 'direct':
                    dispatch.main(args)
                else:
                    argv = [str(Path(dispatch.__file__).resolve()), args.role, args.subject,
                            '--packet', args.packet]
                    for key in ('project', 'cwd', 'path'):
                        if getattr(args, key) is not None:
                            argv += ['--' + key, getattr(args, key)]
                    if route == 'detached':
                        captured = []
                        def child(command, **kwargs):
                            captured.append(command)
                            return argparse.Namespace(pid=12345)
                        with patch.object(sys, 'argv', argv + ['--detach']), \
                             patch.object(subprocess, 'Popen', side_effect=child):
                            try:
                                runpy.run_path(dispatch.__file__, run_name='__main__')
                            except SystemExit as exc:
                                self.assertEqual(exc.code, 0)
                        self.assertEqual(len(captured), 1)
                        argv = captured[0][1:]
                        self.assertNotIn('--cwd', argv)
                    with patch.object(sys, 'argv', argv):
                        runpy.run_path(dispatch.__file__, run_name='__main__')
            except CapturedLaunch:
                return 'launched'
            except SystemExit as exc:
                return exc.code
        self.fail('dispatch returned without reaching capture or refusal')

    def recorded_start(self):
        rows = fold.read_events(use_cache=False)
        return next(e for e in rows if e['type'] in ('spawn-started', 'build-started'))

    def assert_launch(self, expected):
        self.assertEqual(len(self.launches), 1, self.stderr.getvalue())
        launch = self.launches[0]
        self.assertEqual(launch['cwd'], str(expected))
        self.assertEqual(launch['cmd'][launch['cmd'].index('-C') + 1], str(expected))
        metadata, = (self.root/'seat').glob('*.cmd.json')
        self.assertEqual(json.loads(metadata.read_text())['cwd'], str(expected))


class DispatchCwdTests(CwdFixture, unittest.TestCase):
    def test_packet_route_project_caller_matrix(self):
        for role in ('builder', 'reviewer', 'probe'):
            for route in ('direct', 'cli', 'detached'):
                for stdin in (False, True):
                    if route == 'detached' and stdin:
                        continue  # Production explicitly refuses detached stdin.
                    for project in (None, 'project', 'other'):
                        for caller_index in range(3):
                            with self.subTest(role=role, route=route, stdin=stdin, project=project,
                                              caller=caller_index), self.fixture():
                                a = self.args(role, project, stdin=stdin)
                                selected = project or 'project'
                                expected = (self.root/'worktrees'/selected/self.subject.lower()
                                            if role == 'builder' else self.root/'repos'/selected)
                                outcome = self.invoke(a, self.callers[caller_index], route)
                                if role == 'builder' and project == 'other':
                                    self.assertEqual(outcome, 1)
                                    self.assertEqual(self.launches, [])
                                    self.assertIn('wrong-project', json.dumps(fold.read_events(use_cache=False)))
                                else:
                                    self.assertEqual(outcome, 'launched', self.stderr.getvalue())
                                    self.assert_launch(expected)
                                    self.assertEqual(self.recorded_start()['project'], selected)
                                    if route == 'direct':
                                        self.assertEqual(a.project, selected)
                                        self.assertEqual(a.cwd, str(expected))
                                    if role == 'builder':
                                        self.assertEqual(self.effective.call_args.args[:2], (self.repo, selected))
                                        self.assertEqual(self.deps.call_args.args[:3], (self.tree, self.root, self.repo))
                                        if route == 'direct':
                                            self.assertEqual(self.provision.call_args.args, (str(self.tree), self.repo))
                                signed('AC2', dict(role=role, route=route, packet='stdin' if stdin else 'file',
                                    cli_project=project, caller=str(self.callers[caller_index]),
                                    cwd=str(expected), outcome=outcome))

    def test_history_and_environment_project(self):
        import grading_env
        import spec_history
        with self.fixture(), patch.object(spec_history, 'augment_packet_text', side_effect=lambda text, *_: text) as history:
            a = self.args('spec-writer')
            a.augment_history = True
            self.assertEqual(self.invoke(a, self.callers[2]), 'launched')
            self.assertEqual(history.call_args.args[1:], (str(self.repo), 'project'))
        with self.fixture(), patch.object(grading_env, 'preflight', return_value=[]) as preflight:
            a = self.args('reviewer')
            self.assertEqual(self.invoke(a, self.callers[2]), 'launched')
            self.assertEqual(preflight.call_args.args[2:4], (self.repo, 'project'))
        signed('AC2', dict(history_project='project', environment_project='project'))

    def test_grader_view_project(self):
        import grader_packet
        import grader_view
        import grading_env
        import precheck
        with self.fixture(), patch.object(models, 'resolve', return_value=dict(backend='seat', model='fixture', source='fixture')), \
             patch.object(precheck, 'pregrade_refusal', return_value=None), \
             patch.object(grader_packet, 'gate', return_value=None), \
             patch.object(grading_env, 'required', return_value=[]), \
             patch.object(grader_view, 'build', side_effect=CapturedLaunch) as view:
            self.seed('build-done', actor='builder', status='DONE', ready_sha=self.pin, base_sha=self.pin)
            self.assertEqual(self.invoke(self.args('grader'), self.callers[2]), 'launched')
            self.assertEqual(view.call_args.args[:2], (self.subject, self.repo))
            self.assertEqual(self.launches, [])
        signed('AC2', dict(grader_repo='repos/project', backend_launches=0))


class GateCwdTests(CwdFixture, unittest.TestCase):
    def test_real_gate_order_and_refusals(self):
        import fcntl
        for conflict in ('none', 'live', 'lock'):
            observed = []
            for explicit in (False, True):
                with self.subTest(conflict=conflict, explicit=explicit), self.fixture(), contextlib.ExitStack() as stack:
                    order = []
                    real_gate = spawns.gate
                    def gate(*args):
                        order.append(('gate', args[3]))
                        return real_gate(*args)
                    stack.enter_context(patch.object(spawns, 'gate', side_effect=gate))
                    def preflight(*args):
                        order.append(('preflight', args[1]))
                        raise CapturedLaunch()
                    pf = stack.enter_context(patch.object(dispatch, 'builder_worktree_preflight', side_effect=preflight))
                    if conflict == 'live':
                        self.seed('build-started', actor='builder', spawn='L-builder-9900',
                                  worktree=str(self.tree), base_sha=self.pin)
                    if conflict == 'lock':
                        (self.root/'state').mkdir(exist_ok=True)
                        held = stack.enter_context((self.root/'state'/spawns.lock_name(self.subject)).open('a'))
                        fcntl.flock(held, fcntl.LOCK_EX)
                    a = self.args(project='project' if explicit else None, cwd=str(self.tree) if explicit else None)
                    outcome = self.invoke(a, self.callers[2])
                    self.assertEqual(order[0], ('gate', str(self.tree)))
                    if conflict == 'none':
                        self.assertEqual(outcome, 'launched')
                        self.assertEqual(order, [('gate', str(self.tree)), ('preflight', str(self.tree))])
                    else:
                        self.assertEqual(outcome, 1)
                        pf.assert_not_called()
                        failure = next(e for e in fold.read_events(use_cache=False) if e['type'] == 'spawn-failed')
                        self.assertEqual(failure['reason'], 'duplicate-dispatch' if conflict == 'live' else 'dispatch-in-progress')
                    self.assertEqual(self.launches, [])
                    observed.append([stage for stage, _ in order])
                    signed('AC3', dict(explicit=explicit, conflict=conflict, order=order, backend_launches=0))
            self.assertEqual(observed[0], observed[1])


class RefusalCwdTests(CwdFixture, unittest.TestCase):
    def test_preallocation_refusals(self):
        import spec_history
        for ledger_project, cli_project in ((None, None), ('seat', None), ('src', None), ('.do-it', None),
                                            ('project', '../escape'), ('project', '/escape'),
                                            ('project', '.'), ('project', ''), ('project', 'missing')):
            with self.subTest(ledger=ledger_project, cli=cli_project), self.fixture(ledger_project), \
                 patch.object(dispatch, 'alloc', wraps=dispatch.alloc) as alloc, \
                 patch.object(spec_history, 'augment_packet_text') as history, \
                 patch.object(dispatch, 'builder_worktree_preflight') as preflight:
                before = {p.name: p.read_bytes() for p in (self.root/'events').iterdir()}
                a = self.args('spec-writer', cli_project)
                a.augment_history = True
                outcome = self.invoke(a, self.callers[2])
                self.assertIn('reason=preflight:cwd-unresolved', str(outcome))
                if cli_project in ('../escape', '/escape', '.'):
                    self.assertIn(repr(cli_project), str(outcome))
                else:
                    self.assertIn('no-project', str(outcome))
                self.assertNotIn('Synthetic packet', str(outcome))
                self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root/'events').iterdir()})
                alloc.assert_not_called()
                history.assert_not_called()
                preflight.assert_not_called()
                self.assertEqual(self.launches, [])
                # Fresh reader after the attempted dispatch, without the fold cache.
                self.assertEqual(len(fold.read_events(use_cache=False)),
                    0 if ledger_project is None else 1 if ledger_project in ('seat', 'src', '.do-it') else 2)
                signed('AC4', dict(ledger_project=ledger_project, cli_project=cli_project,
                                   refusal=outcome, new_files=0, backend_launches=0))
        with self.fixture(), patch.object(dispatch, 'alloc', wraps=dispatch.alloc) as alloc:
            a = self.args(project='project')
            a.subject = '../escape'
            outcome = self.invoke(a, self.callers[2])
            self.assertIn("invalid subject component '../escape'", str(outcome))
            alloc.assert_not_called()
            self.assertEqual(self.launches, [])

    def test_resolved_failure_store_roundtrip(self):
        import autodispatch
        import branch_refresh
        for mode, reason in (('missing', 'preflight:worktree-missing'), ('branch', 'preflight:wrong-branch'),
                             ('ancestry', 'branch-not-descendant')):
            with self.subTest(mode=mode), self.fixture(worktree=mode != 'missing'), contextlib.ExitStack() as stack:
                if mode == 'missing':
                    stack.enter_context(patch.object(autodispatch, 'add_builder_worktree', return_value={'code': 1}))
                if mode == 'branch':
                    subprocess.run(['git', '-C', str(self.tree), 'switch', '-qc', 'wrong-branch'], check=True)
                if mode == 'ancestry':
                    stack.enter_context(patch.object(branch_refresh, 'dispatch_preflight', return_value=False))
                    # An unresolved base proves the ancestry-command refusal without a rebase or branch movement.
                    real_run = subprocess.run
                    def ancestry_run(cmd, *args, **kwargs):
                        if 'merge-base' in cmd and '--is-ancestor' in cmd:
                            return subprocess.CompletedProcess(cmd, 128, '', 'synthetic ancestry unavailable')
                        return real_run(cmd, *args, **kwargs)
                    stack.enter_context(patch.object(subprocess, 'run', side_effect=ancestry_run))
                outcome = self.invoke(self.args(), self.callers[2])
                self.assertEqual(outcome, 1, self.stderr.getvalue())
                self.assertIn(str(self.tree), self.stderr.getvalue())
                # This subprocess imports a fresh fold and reads production emit's actual file.
                command = [sys.executable, '-c', 'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))']
                reader = subprocess.Popen(command, cwd=Path(dispatch.__file__).parent, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, env=dict(os.environ))
                stdout, stderr = reader.communicate(timeout=30)
                self.assertEqual(reader.returncode, 0, stderr)
                failure = next(e for e in json.loads(stdout) if e['type'] == 'spawn-failed')
                self.assertEqual(failure['project'], 'project')
                self.assertEqual(failure['reason'], reason)
                self.assertEqual(failure['cwd'], str(self.tree))
                self.assertIn(str(self.tree), failure['why'])
                self.assertEqual(self.launches, [])
                signed('AC4', dict(mode=mode, project=failure['project'], reason=reason,
                                   cwd=failure['cwd'], why=failure['why'], reader_exit=reader.returncode))


class ExplicitCwdTests(CwdFixture, unittest.TestCase):
    def test_explicit_selection_and_paths(self):
        for ledger_project in (None, 'other'):
            for value in ('absolute', 'root-relative', 'caller-relative'):
                with self.subTest(ledger=ledger_project, value=value), self.fixture(ledger_project), \
                     patch.object(fold, 'subject_project', side_effect=AssertionError('explicit selection consulted ledger')):
                    caller = self.callers[2]
                    expected = self.repo if value != 'caller-relative' else caller/'local'
                    if value == 'caller-relative':
                        expected.mkdir()
                    cwd = {'absolute': str(self.repo), 'root-relative': 'repos/project', 'caller-relative': 'local'}[value]
                    a = self.args('spec-auditor', cwd=cwd)
                    self.assertEqual(self.invoke(a, caller), 'launched', self.stderr.getvalue())
                    self.assertIsNone(a.project)
                    self.assert_launch(expected)
                    self.assertEqual(self.recorded_start()['project'], expected.name)
                    signed('AC5', dict(ledger_project=ledger_project, explicit=cwd, cwd=str(expected),
                                       project=self.recorded_start()['project'], args_project=a.project))
        with self.fixture():
            a = self.args('probe', cwd=str(self.callers[2]))
            self.assertEqual(self.invoke(a, self.callers[1]), 'launched', self.stderr.getvalue())
            self.assert_launch(self.callers[2])
            signed('AC5', dict(role='probe', explicit_scratch=str(self.callers[2])))


if __name__ == '__main__':
    unittest.main()
