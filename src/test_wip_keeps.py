#!/usr/bin/env python3
"""L-spec-2284: builder WIP checkpoints, timeout preservation and resume. Scratch git and ledger only."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch_cwd as cwdt          # sets a scratch DOIT_ROOT before dispatch is imported
import test_coupled_grants as cg
import branch_refresh
import dispatch
import dispatch_builder
import fold
import models
import spawns
import wip_resume

REPO = Path(__file__).resolve().parent.parent
REAL_RUN = subprocess.run
SUBJECT = 'L-spec-9955'
BRANCH = SUBJECT.lower()
WRITER = r'''
import os, subprocess, sys, time
tree, mode = sys.argv[1], sys.argv[2]
open(os.path.join(tree, 'edit.txt'), 'w').write('one')
if mode == 'stubborn':
    code = ("import signal, time, os, sys\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n open(os.path.join(sys.argv[1], 'late.log'), 'a').write('x')\n time.sleep(0.05)\n")
    subprocess.Popen([sys.executable, '-c', code, tree])
time.sleep(60)
'''
SEEDER = r'''
import os, subprocess, sys
tree = sys.argv[1]
def run(*a):
    subprocess.run(['git', '-C', tree, '-c', 'user.name=B', '-c', 'user.email=b@example.invalid', *a], check=True,
                   capture_output=True)
def write(name, body):
    p = os.path.join(tree, name); os.makedirs(os.path.dirname(p) or tree, exist_ok=True); open(p, 'w').write(body)
write('src/granted.py', 'wip one\n'); run('add', '-A'); run('commit', '--no-verify', '-qm', 'wip(L-spec-9955): first')
write('src/test_coupled.py', 'wip two\n'); run('add', '-A'); run('commit', '--no-verify', '-qm', 'wip(L-spec-9955): second')
write('src/granted.py', 'staged edit\n'); run('add', 'src/granted.py')
write('src/outside_tracked.py', 'unstaged edit\n')
os.remove(os.path.join(tree, 'src/to_delete.txt'))
write('new dir/file with spaces.txt', 'untracked spaces\n')
write('-dash.txt', 'untracked dash\n')
write('ignored.log', 'never saved\n')
import time; time.sleep(60)
'''


def real_group(cmd, packet, cwd, timeout):
    """The production group runner against a real child; the fixtures intercept subprocess.run."""
    with patch.object(subprocess, 'run', REAL_RUN):
        return wip_resume.run_group(cmd, packet, cwd, dict(os.environ), timeout)


def exec_with(mode, seen=None):
    def fake(cmd, packet, cwd, timeout, group=False):
        if seen is not None:
            seen.append(cmd)
        return real_group([sys.executable, '-c', WRITER, str(cwd), mode], packet, cwd, 1)
    return fake


class Clock:
    """Simulated time for the seat grace wait: sleeping advances the clock and may run a hook."""
    def __init__(self, hook=None):
        self.t, self.hook = 0.0, hook

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.hook:
            self.hook(self.t)


class WipKeepsTests(cwdt.CwdFixture, unittest.TestCase):
    # -- helpers ---------------------------------------------------------
    def g(self, *args, cwd=None):
        return REAL_RUN(['git', '-C', str(cwd or self.tree), *args], capture_output=True, text=True, check=True).stdout

    def commit(self, cwd, name, body, message):
        path = Path(cwd) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        self.g('add', '-A', cwd=cwd)
        self.g('-c', 'user.name=B', '-c', 'user.email=b@example.invalid', 'commit', '--no-verify', '-qm', message, cwd=cwd)
        return self.g('rev-parse', 'HEAD', cwd=cwd).strip()

    def rows(self, kind=None):
        rows = fold.read_events(use_cache=False)
        return [e for e in rows if kind is None or e['type'] == kind]

    def terminal_after_timeout(self):
        types = [e['type'] for e in self.rows()]
        self.assertEqual(types.count('build-timeout'), 1, types)
        later = types[types.index('build-timeout') + 1:]
        self.assertTrue(any(t in ('infra-failure', 'spawn-failed', 'escalation-blocking') for t in later), types)

    def seed_at(self, kind, seconds, actor='builder', **kv):
        """Append a row stamped `seconds` after now: same-second rows from different files reorder by content."""
        from datetime import datetime, timedelta, timezone
        row = dict(kv, subject=SUBJECT, project='project', v=1, type=kind,
                   ts=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds'))
        with open(self.root / 'events' / f'L-{actor}-9901.jsonl', 'a') as fh:
            fh.write(json.dumps(row, sort_keys=True) + '\n')

    def grant(self):
        self.root.joinpath('content', SUBJECT + '.md').write_text('Writes: src/granted.py\n')
        written = next(e for e in reversed(self.rows('spec-written')))
        self.seed('writes-granted', actor='spec-preflight', tests=[dict(test='src/test_coupled.py', source='coupled',
                  kind='backend', ac_bound=[])], why='fixture', spec_written_src=written['_src'])

    def snapshot(self):
        return (self.g('rev-parse', 'HEAD'), self.g('status', '--porcelain=v1', '-z', '--untracked-files=all'),
                self.g('rev-parse', 'fixture-main', cwd=self.repo))

    # -- AC1 -------------------------------------------------------------
    def test_checkpoint_contract(self):
        text = (REPO / 'agents/builder.md').read_text()
        flat = ' '.join(text.split())
        for needle in ('at least every 20 minutes', 'wip(L-spec-N): <what>', 'squash', 'one commit',
                       'A clean interval needs no empty commit', '--no-verify', 'git commit -n', 'core.hooksPath',
                       'belongs solely to the timeout wrapper', 'Resume from <last_commit> on <branch>: N WIP commits',
                       'remove each from the branch, or justify', 'does not relax any hand-in grant check'):
            self.assertIn(needle, flat)
        self.assertNotIn('One spec, one commit, on your branch.', flat)
        self.assertLess(flat.index('at least every 20 minutes'), flat.index('## Lessons'))
        self.assertIn('## Lessons (operator rule', text)

    # -- AC2 -------------------------------------------------------------
    def test_timeout_round_trip(self):
        with self.fixture():
            for name, body in (('src/granted.py', 'base\n'), ('src/test_coupled.py', 'base\n'),
                               ('src/outside_tracked.py', 'base\n'), ('src/to_delete.txt', 'gone\n'),
                               ('.gitignore', 'ignored.log\n')):
                last = self.commit(self.tree, name, body, 'seed ' + name)
            self.grant()
            self.effective.return_value = self.pin
            hook = self.repo / '.git/hooks/pre-commit'
            hook.write_text(f'#!/bin/sh\ntouch {self.root}/hook-ran\nexit 1\n')
            hook.chmod(0o755)
            start = self.g('rev-parse', 'HEAD').strip()
            main_before = self.g('rev-parse', 'fixture-main', cwd=self.repo)

            def exec_seeder(cmd, packet, cwd, timeout, group=False):
                return real_group([sys.executable, '-c', SEEDER, str(cwd)], packet, cwd, 2)
            a = self.args()
            a.timeout = 0.03
            with patch.object(dispatch, 'run_codex_exec', exec_seeder):
                code = self.invoke(a, self.callers[2])
            self.assertEqual(code, 2, self.stderr.getvalue())
            self.terminal_after_timeout()
            event, = self.rows('build-timeout')
            head = self.g('rev-parse', 'HEAD').strip()
            self.assertEqual(event['last_commit'], head)
            self.assertEqual(event['branch'], BRANCH)
            self.assertEqual(self.g('log', '-1', '--format=%s').strip(), f'wip({SUBJECT}): timeout')
            self.assertEqual(self.g('status', '--porcelain=v1', '--untracked-files=all'), '')
            self.assertFalse((self.root / 'hook-ran').exists(), 'the hook ran for the wrapper commit')
            expected = self.g('rev-list', '--reverse', start + '..HEAD').split()
            self.assertEqual(event['wip_commits'], expected)
            self.assertEqual(len(expected), 3)
            self.assertEqual(sorted(event['uncommitted_out_of_scope']),
                             sorted(['-dash.txt', 'new dir/file with spaces.txt', 'src/outside_tracked.py', 'src/to_delete.txt']))
            # A fresh independent git process reads the committed bytes and the deletion.
            show = lambda p: REAL_RUN(['git', '-C', str(self.tree), 'show', 'HEAD:' + p], capture_output=True, text=True)
            self.assertEqual(show('src/granted.py').stdout, 'staged edit\n')
            self.assertEqual(show('src/test_coupled.py').stdout, 'wip two\n')
            self.assertEqual(show('src/outside_tracked.py').stdout, 'unstaged edit\n')
            self.assertEqual(show('new dir/file with spaces.txt').stdout, 'untracked spaces\n')
            self.assertEqual(show('-dash.txt').stdout, 'untracked dash\n')
            self.assertNotEqual(show('src/to_delete.txt').returncode, 0)
            self.assertNotEqual(show('ignored.log').returncode, 0)
            # A fresh process sees the persisted event.
            env = {**os.environ, 'PYTHONPATH': str(REPO / 'src'), 'DOIT_ROOT': str(self.root)}
            seen = REAL_RUN([sys.executable, '-c', 'import fold, json; print(json.dumps([e for e in fold.read_events(use_cache=False)'
                             ' if e["type"] == "build-timeout"]))'], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(json.loads(seen.stdout)[0]['last_commit'], head, seen.stderr)
            self.assertEqual(self.g('rev-parse', 'fixture-main', cwd=self.repo), main_before)
            self.assertEqual(self.g('status', '--porcelain=v1', cwd=self.repo), '')
            print(json.dumps(dict(criterion='AC2', wip_commits=expected, flagged=event['uncommitted_out_of_scope'])))

    # -- AC3 -------------------------------------------------------------
    def timeout_run(self, resolve, patches, mode='plain', role='builder'):
        """One dispatch to the backend boundary; returns the exit code."""
        a = self.args(role)
        a.timeout = 0.02
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(models, 'resolve', return_value=dict(source='fixture', model='fixture', **resolve)))
            stack.enter_context(patch.object(dispatch, 'relay_serving', return_value=True))
            for target, fake in patches.items():
                stack.enter_context(patch.object(dispatch, target, fake))
            return self.invoke(a, self.callers[2])

    def test_timeout_routes(self):
        seen = []
        # Subprocess primary routes and their fallbacks: each saves once, then fails.
        fb_seat = dict(backend='codex', fallback=dict(backend='seat', model='fb'))
        fb_claude = dict(backend='codex', fallback=dict(backend='claude-p', model='fb'))
        refuse = lambda cmd, packet, cwd, timeout, group=False: subprocess.CompletedProcess(cmd, 1, '', 'refused')

        matrix = {
            'codex': (dict(backend='codex'), lambda: {'run_codex_exec': exec_with('plain', seen)}, False),
            'claude-p': (dict(backend='claude-p'), lambda: {'run_claude': exec_with('stubborn', seen)}, False),
            'seat': (dict(backend='seat'), None, True),
            'codex->seat': (fb_seat, None, True),
            'codex->claude': (fb_claude, lambda: {'run_codex_exec': refuse, 'run_claude': exec_with('plain', seen)}, False),
        }
        for name, (resolve, make, seat) in matrix.items():
            with self.subTest(route=name), self.fixture(), patch.object(wip_resume, 'TERM_GRACE', 0.4):
                self.commit(self.tree, 'src/seed.txt', 'seed\n', 'seed')
                start = self.g('rev-parse', 'HEAD').strip()
                if seat:
                    spawn = {}

                    def run_seat(spawn_id, cmd, packet, cwd, timeout, **kw):
                        spawn['id'] = spawn_id
                        (Path(cwd) / 'edit.txt').write_text('before the deadline')
                        raise subprocess.TimeoutExpired(cmd, timeout)

                    def hook(t):
                        if t >= 3:
                            (self.tree / 'edit.txt').write_text('late edit before the stamp')
                        if t >= 4:
                            (dispatch.SEAT / (spawn['id'] + '.meta.json')).write_text('{}')
                    clock = Clock(hook)
                    patches = {'run_seat': run_seat}
                    if resolve['backend'] == 'codex':
                        patches['run_codex_exec'] = refuse
                    with patch.object(wip_resume, '_clock', clock.clock), patch.object(wip_resume, '_sleep', clock.sleep):
                        code = self.timeout_run(resolve, patches)
                    self.assertFalse((dispatch.SEAT / (spawn['id'] + '.cancelled')).exists(), 'marker left after a stamp')
                    self.assertEqual((self.tree / 'edit.txt').read_text(), 'late edit before the stamp')
                else:
                    code = self.timeout_run(resolve, make())
                self.assertEqual(code, 2, self.stderr.getvalue())
                self.terminal_after_timeout()
                event, = self.rows('build-timeout')
                self.assertEqual(event['last_commit'], self.g('rev-parse', 'HEAD').strip())
                self.assertEqual(self.g('status', '--porcelain=v1', '--untracked-files=all'), '')
                self.assertEqual(self.g('show', 'HEAD:edit.txt'), (self.tree / 'edit.txt').read_text())
                self.assertEqual(event['wip_commits'], self.g('rev-list', '--reverse', start + '..HEAD').split())
                self.assertEqual(event['uncommitted_out_of_scope'], ['edit.txt', 'late.log'] if name == 'claude-p' else ['edit.txt'])
                if name == 'claude-p':
                    late = self.tree / 'late.log'
                    size = late.stat().st_size
                    self.assertEqual(self.g('show', 'HEAD:late.log'), late.read_text())
                    import time
                    time.sleep(0.4)
                    self.assertEqual(late.stat().st_size, size, 'a descendant kept writing after the checkpoint')
                    self.assertEqual(self.g('status', '--porcelain=v1'), '')
        self.assertEqual(len(seen), 3)
        # The real runners put only the builder in its own process group.
        for runner, cmd, own in ((dispatch.run_claude, ['claude', '-p', '--agent', 'builder'], True),
                                 (dispatch.run_claude, ['claude', '-p', '--agent', 'reviewer'], False),
                                 (dispatch.run_codex_exec, ['codex', 'exec', '--sandbox', 'danger-full-access'], True),
                                 (dispatch.run_codex_exec, ['codex', 'exec', '--sandbox', 'read-only'], False)):
            with patch.object(wip_resume, 'run_group', return_value='group') as grouped, \
                 patch.object(dispatch.subprocess, 'run', return_value='plain'):
                self.assertEqual(runner(cmd, 'p', str(REPO), 1), 'group' if own else 'plain')
                self.assertEqual(grouped.called, own)
        # A live seat writer never reaches a stamp: no snapshot, named diagnostic, no build-timeout.
        with self.fixture():
            head = self.g('rev-parse', 'HEAD').strip()
            spawn = {}

            def run_seat(spawn_id, cmd, packet, cwd, timeout, **kw):
                spawn['id'] = spawn_id
                raise subprocess.TimeoutExpired(cmd, timeout)
            writes = []

            def hook(t):
                (self.tree / 'live.txt').write_text(str(t))
                writes.append(t)
            clock = Clock(hook)
            with patch.object(wip_resume, '_clock', clock.clock), patch.object(wip_resume, '_sleep', clock.sleep):
                code = self.timeout_run(dict(backend='seat'), {'run_seat': run_seat})
            self.assertIn(code, (1, 2))
            self.assertGreater(clock.t, wip_resume.SEAT_GRACE - 1)
            self.assertEqual(self.g('rev-parse', 'HEAD').strip(), head)
            self.assertEqual(self.rows('build-timeout'), [])
            self.assertIn('timeout-preservation:writer-not-quiescent', json.dumps(self.rows()))
            self.assertIn('live.txt', self.g('status', '--porcelain=v1', '--untracked-files=all'))
            self.assertTrue((dispatch.SEAT / (spawn['id'] + '.cancelled')).is_file())
        # A subprocess group that is still alive prevents saving.
        def alive(cmd, packet, cwd, timeout, group=False):
            exc = subprocess.TimeoutExpired(cmd, timeout)
            exc.quiescent = False
            (Path(cwd) / 'edit.txt').write_text('x')
            raise exc
        with self.fixture():
            head = self.g('rev-parse', 'HEAD').strip()
            self.timeout_run(dict(backend='claude-p'), {'run_claude': alive})
            self.assertEqual(self.g('rev-parse', 'HEAD').strip(), head)
            self.assertEqual(self.rows('build-timeout'), [])
            self.assertIn('writer-not-quiescent', json.dumps(self.rows()))
        # Negative controls create no checkpoint: operator cancel, unserved seat, non-builder timeout, plain failure.
        def cancelled(*a, **k):
            raise spawns.Cancelled('x')

        def unserved(spawn_id, cmd, *a, **k):
            raise dispatch.Unserved(spawn_id, Path('/none'), 1)

        def timed_out(cmd, packet, cwd, timeout, group=False):
            raise subprocess.TimeoutExpired(cmd, timeout)
        for label, resolve, patches, role in (
                ('cancel', dict(backend='seat'), {'run_seat': cancelled}, 'builder'),
                ('unserved', dict(backend='seat'), {'run_seat': unserved}, 'builder'),
                ('non-builder', dict(backend='claude-p'), {'run_claude': timed_out}, 'reviewer'),
                ('failure', dict(backend='claude-p'), {'run_claude': lambda c, p, w, t, group=False:
                                                       subprocess.CompletedProcess(c, 1, 'not json', 'bad')}, 'builder')):
            with self.subTest(control=label), self.fixture():
                (self.tree / 'dirty.txt').write_text('left alone')
                head = self.g('rev-parse', 'HEAD').strip()
                self.timeout_run(resolve, patches, role=role)
                self.assertEqual(self.rows('build-timeout'), [])
                self.assertEqual(self.g('rev-parse', 'HEAD').strip(), head)
                self.assertTrue((self.tree / 'dirty.txt').exists())

    # -- AC4 -------------------------------------------------------------
    def test_resume_packet(self):
        result = unittest.TestResult()
        _Packet('check').run(result)
        self.assertTrue(result.wasSuccessful(), [str(f[1])[-800:] for f in result.failures + result.errors])

    # -- AC5 -------------------------------------------------------------
    def admission_case(self, prior_done, position, timeout=True, later_done=False):
        """Seed a saved branch, move main as asked, and run production dispatch admission."""
        with self.fixture():
            self.g('config', 'user.name', 'Fixture', cwd=self.repo)
            self.g('config', 'user.email', 'fixture@example.invalid', cwd=self.repo)
            saved = self.commit(self.tree, 'work.txt', 'saved work\n', f'wip({SUBJECT}): saved')
            spawn = 'L-builder-0001'
            if prior_done:
                self.seed_at('build-done', 10, status='DONE', ready_sha=saved, base_sha=self.pin)
            if timeout:
                self.seed_at('build-timeout', 20, branch=BRANCH, last_commit=saved, wip_commits=[saved],
                             spawn=spawn, uncommitted_out_of_scope=[])
            if later_done:
                self.seed_at('build-done', 30, status='DONE', ready_sha=saved, base_sha=self.pin)
            if position != 'current':
                body = 'main moved\n' if position == 'clean' else 'main wrote the same file\n'
                self.commit(self.repo, 'other.txt' if position == 'clean' else 'work.txt', body, 'main advances')
                newpin = self.g('rev-parse', 'HEAD', cwd=self.repo).strip()
                self.g('checkout', '-q', 'fixture-main', cwd=self.repo)
                self.effective.return_value = newpin
                self.packet.write_text('Synthetic packet.\nPINNED_BASE_SHA: ' + newpin + '\n')
            inode = os.stat(self.tree / '.git').st_ino
            calls = []
            spies = {n: (lambda *a, _n=n, **k: calls.append(_n)) for n in ('recut', 'refresh', 'regate', 'gate_refresh')}
            with contextlib.ExitStack() as stack:
                for n, spy in spies.items():
                    stack.enter_context(patch.object(branch_refresh, n, spy))
                def no_model(cmd, packet, cwd, timeout, group=False):
                    self.launches.append(dict(cmd=cmd, cwd=cwd))
                    raise cwdt.CapturedLaunch()
                stack.enter_context(patch.object(dispatch, 'run_codex_exec', no_model))
                import packet_base
                stack.enter_context(patch.object(packet_base, '_effective_branch', return_value='fixture-main'))
                a = self.args()
                outcome = self.invoke(a, self.callers[2])
            info = dict(outcome=outcome, calls=calls, saved=saved, inode=inode, newpin=self.effective.return_value)
            info['head'] = self.g('rev-parse', 'HEAD').strip()
            info['status'] = self.g('status', '--porcelain=v1', '--untracked-files=all')
            info['events'] = fold.read_events(use_cache=False)
            info['launches'] = len(self.launches)
            info['stderr'] = self.stderr.getvalue()
            info['work'] = (self.tree / 'work.txt').read_text() if (self.tree / 'work.txt').exists() else None
            info['same_tree'] = os.stat(self.tree / '.git').st_ino == inode
            info['ancestor'] = REAL_RUN(['git', '-C', str(self.tree), 'merge-base', '--is-ancestor', str(info['newpin']), 'HEAD']).returncode == 0
            info['ref'] = self.g('rev-parse', '--abbrev-ref', 'HEAD').strip()
            info['rebasing'] = any((Path(self.g('rev-parse', '--git-path', n, cwd=self.tree).strip())).exists()
                                   for n in ('rebase-merge', 'rebase-apply'))
            if position == 'clean' and outcome == 'launched':
                info['lines'] = wip_resume.packet_lines(info['events'], SUBJECT)
                base = str(info['newpin'])
                self.g('reset', '--soft', base)
                self.g('-c', 'user.name=B', '-c', 'user.email=b@example.invalid', 'commit', '--no-verify', '-qm', 'one commit')
                info['squashed'] = (self.g('rev-list', '--count', base + '..HEAD').strip(), self.g('show', 'HEAD:work.txt'))
            return info

    def test_resume_branch(self):
        for prior_done in (False, True):
            for position in ('current', 'clean', 'conflict'):
                with self.subTest(prior_done=prior_done, position=position):
                    r = self.admission_case(prior_done, position)
                    self.assertEqual(r['calls'], [], 'recut/adoption/re-gate ran for incomplete timeout work')
                    self.assertTrue(r['same_tree'])
                    self.assertEqual(r['ref'], BRANCH)
                    self.assertEqual(r['status'], '')
                    self.assertFalse(r['rebasing'])
                    if position == 'conflict':
                        self.assertEqual(r['outcome'], 1)
                        self.assertEqual(r['launches'], 0, 'a backend was launched on a conflict')
                        self.assertEqual(r['head'], r['saved'], 'rebase abort must restore the saved tip')
                        failed = [e for e in r['events'] if e['type'] == 'spawn-failed']
                        self.assertEqual(failed[-1]['reason'], 'branch-not-descendant')
                        self.assertIn('timeout-resume conflict: Executor authorization required', failed[-1]['why'])
                        self.assertFalse([e for e in r['events'] if e['type'] in ('merge-conflict', 'conflict-rework')])
                    else:
                        self.assertEqual(r['outcome'], 'launched', r['stderr'] + json.dumps([e for e in r['events'] if e['type'] != 'spec-written'][-3:]))
                        self.assertEqual(r['work'], 'saved work\n')
                        if position == 'current':
                            self.assertEqual(r['head'], r['saved'], 'dispatch starts at last_commit')
                        else:
                            self.assertTrue(r['ancestor'])
                            self.assertNotEqual(r['head'], r['saved'])
                            self.assertTrue(any(f'Resume from {r["saved"]} on {BRANCH}: 1 WIP commits' in l for l in r['lines']))
                            self.assertEqual(r['squashed'], ('1', 'saved work\n'))
        # Completed-branch recovery is unchanged without an applicable timeout.
        r = self.admission_case(True, 'clean', timeout=False)
        self.assertEqual(r['calls'], ['recut'], r['calls'])
        r = self.admission_case(True, 'current', timeout=False)
        self.assertEqual(r['calls'], [])
        self.assertEqual(r['outcome'], 'launched')
        r = self.admission_case(True, 'clean', later_done=True)
        self.assertEqual(r['calls'], ['recut'], 'a later DONE supersedes the timeout')

    # -- AC6 -------------------------------------------------------------
    def test_preservation_refusals(self):
        spec = dict(subject=SUBJECT, project='project')
        a = type('A', (), spec)()
        # Unsafe checkouts are refused and left untouched.
        with self.fixture():
            start = self.g('rev-parse', 'HEAD').strip()
            (self.tree / 'work.txt').write_text('dirty\n')
            before = self.snapshot()
            with self.assertRaises(wip_resume.PreserveError):
                wip_resume.preserve(self.root / 'worktrees/project/missing', SUBJECT, start, [], self.repo)
            with self.assertRaises(wip_resume.PreserveError):
                wip_resume.preserve(self.tree, SUBJECT, start, [], self.tree)          # shared main checkout
            with self.assertRaises(wip_resume.PreserveError):
                wip_resume.preserve(self.tree, 'L-spec-1111', start, [], self.repo)    # not the spec's branch
            self.g('checkout', '-q', '--detach')
            with self.assertRaises(wip_resume.PreserveError):
                wip_resume.preserve(self.tree, SUBJECT, start, [], self.repo)          # detached HEAD
            self.g('checkout', '-q', BRANCH)
            gitdir = Path(self.g('rev-parse', '--absolute-git-dir').strip())
            for marker, kind in (('rebase-merge', 'dir'), ('index.lock', 'file'), ('MERGE_HEAD', 'file')):
                target = gitdir / marker
                target.mkdir() if kind == 'dir' else target.write_text('x')
                with self.assertRaises(wip_resume.PreserveError, msg=marker):
                    wip_resume.preserve(self.tree, SUBJECT, start, [], self.repo)
                self.assertTrue(target.exists(), 'a lock or operation marker was removed')
                target.rmdir() if kind == 'dir' else target.unlink()
            self.assertEqual(self.snapshot(), before)
            self.assertEqual((self.tree / 'work.txt').read_text(), 'dirty\n')
            # Not quiescent: nothing is saved and no event claims it.
            exc = subprocess.TimeoutExpired(['x'], 1)
            exc.quiescent = False
            ledger = self.root / 'events/L-builder-9901.jsonl'
            note = wip_resume.on_timeout(dispatch, a, self.tree, ledger, dict(subject=SUBJECT, project='project', spawn='L-builder-9901'),
                                         'L-builder-9901', start, exc, False)
            self.assertEqual(note, 'timeout-preservation:writer-not-quiescent')
            self.assertEqual(self.snapshot(), before)
            # Event append failures never read as preserved.
            exc.quiescent = True
            base = dict(subject=SUBJECT, project='project', spawn='L-builder-9901')
            for broken in (OSError('disk full'), 'build-timeout refused'):
                self.g('reset', '-q', '--hard', start)
                (self.tree / 'work.txt').write_text('dirty\n')

                def emit(*args, **kw):
                    if isinstance(broken, Exception):
                        raise broken
                    return broken
                with patch.object(dispatch, 'emit', emit):
                    note = wip_resume.on_timeout(dispatch, a, self.tree, ledger, base, 'L-builder-9901', start, exc, False)
                self.assertTrue(note.startswith('timeout-preservation:'), note)
                self.assertEqual(self.rows('build-timeout'), [])
            # Filename parsing: spaces, leading dashes, staged rename and deletion.
            self.g('reset', '-q', '--hard', start)
            self.commit(self.tree, 'old name.txt', 'renamed\n', 'seed rename')
            self.commit(self.tree, 'doomed.txt', 'bye\n', 'seed delete')
            mid = self.g('rev-parse', 'HEAD').strip()
            self.g('mv', '--', 'old name.txt', '-new name.txt')
            self.g('rm', '-q', '--', 'doomed.txt')
            (self.tree / '--option-like').write_text('plain\n')
            (self.tree / 'sp ace.txt').write_text('space\n')
            self.assertEqual(sorted(wip_resume.dirty_paths(self.tree)),
                             sorted(['-new name.txt', 'old name.txt', 'doomed.txt', '--option-like', 'sp ace.txt']))
            branch, last, wip, flagged = wip_resume.preserve(self.tree, SUBJECT, mid, ['sp ace.txt'], self.repo)
            self.assertEqual((branch, wip), (BRANCH, [last]))
            self.assertEqual(sorted(flagged), sorted(['-new name.txt', 'old name.txt', 'doomed.txt', '--option-like']))
            self.assertEqual(self.g('show', 'HEAD:--option-like'), 'plain\n')
            self.assertEqual(self.g('show', 'HEAD:-new name.txt'), 'renamed\n')
            self.assertEqual(self.g('status', '--porcelain=v1'), '')
            # Grants: current revision (text + recorded tests) is in scope; a stale grant is outside it.
            self.grant()
            old_src = next(e for e in self.rows('writes-granted'))['spec_written_src']
            (self.tree / 'src').mkdir(exist_ok=True)
            for name in ('granted.py', 'test_coupled.py'):
                (self.tree / 'src' / name).write_text('x\n')
            mid = self.g('rev-parse', 'HEAD').strip()
            ledger2 = self.root / 'events/L-builder-9902.jsonl'
            note = wip_resume.on_timeout(dispatch, a, self.tree, ledger2, dict(base, spawn='L-builder-9902'),
                                         'L-builder-9902', mid, exc, False)
            self.assertIsNone(note, note)
            self.assertEqual(next(e for e in self.rows('build-timeout') if e['spawn'] == 'L-builder-9902')['uncommitted_out_of_scope'], [])
            row = {k: v for k, v in next(e for e in reversed(self.rows('spec-written'))).items()
                   if k not in ('_src', 'actor', 'ts', 'type', 'subject', 'project', 'v')}
            self.seed_at('spec-written', 120, actor='spec-writer', **row)
            (self.tree / 'src/test_coupled.py').write_text('y\n')
            mid = self.g('rev-parse', 'HEAD').strip()
            ledger3 = self.root / 'events/L-builder-9903.jsonl'
            note = wip_resume.on_timeout(dispatch, a, self.tree, ledger3, dict(base, spawn='L-builder-9903'),
                                         'L-builder-9903', mid, exc, False)
            self.assertIsNone(note, note)
            newest = next(e for e in reversed(self.rows('spec-written')))['_src']
            self.assertNotEqual(old_src, newest)
            self.assertEqual(next(e for e in self.rows('build-timeout') if e['spawn'] == 'L-builder-9903')['uncommitted_out_of_scope'], ['src/test_coupled.py'])


class _Packet(cg.ConsumerFixture):
    """AC4 against the real p_builder, using the coupled-grants fixture for its granted-tests section."""
    def check(self):
        import packet
        sid = self.sid
        branch = sid.lower()
        before = self.render()
        refs = self.git('for-each-ref')
        mtimes = {p.name: p.read_bytes() for p in (self.root / 'events').iterdir()}
        timeout = dict(type='build-timeout', subject=sid, actor='builder', branch=branch, last_commit='a' * 40,
                       wip_commits=['b' * 40, 'c' * 40], spawn='L-builder-1', uncommitted_out_of_scope=['src/x.py', 'my dir/y z.py'])
        base = list(self.events)
        self.events = base + [timeout]
        text = self.render()
        self.assertIn(f"Resume from {'a' * 40} on {branch}: 2 WIP commits from the timed-out build", text.splitlines())
        for path in ('   src/x.py', '   my dir/y z.py'):
            self.assertIn(path, text.splitlines())
        self.assertIn('remove each from the branch or justify it in the card before hand-in', text)
        self.assertIn('Granted coupled tests (fixture and assertion updates only)', text)   # the neighbour's section survives
        self.assertIn('Granted coupled tests', text)
        # Selection: another spec's timeout, a later DONE or a new spec-written never present an obsolete resume.
        self.events = base + [dict(timeout, subject='L-spec-9999')]
        self.assertEqual(self.render(), before)
        for later in (dict(type='build-done', subject=sid, status='DONE'), dict([e for e in base if e['type'] == 'spec-written' and e.get('subject') == sid][-1], _src='new:1')):
            self.events = base + [timeout, later]
            self.assertNotIn('Resume from', self.render())
        self.events = base + [timeout, dict(type='build-done', subject=sid, status='BLOCKED'), dict(type='build-started', subject=sid)]
        self.assertIn('Resume from', self.render())
        newer = dict(timeout, last_commit='d' * 40, wip_commits=['e' * 40])
        self.events = base + [timeout, newer]
        self.assertIn(f"Resume from {'d' * 40} on {branch}: 1 WIP commits", self.render())
        self.assertNotIn('a' * 40 + ' on', self.render())
        self.events = base
        self.assertEqual(self.render(), before)                # no timeout: content unchanged
        self.assertEqual(self.git('for-each-ref'), refs)       # no branch was derived or mutated
        self.assertEqual(mtimes, {p.name: p.read_bytes() for p in (self.root / 'events').iterdir()})
        # Size: zero net growth against the inherited base, and the fixed cap.
        lines = len(Path(packet.__file__).read_text().splitlines())
        self.assertLessEqual(lines, 1578)
        for ref in ('origin/main', 'main', 'origin/master', 'master'):
            base_blob = REAL_RUN(['git', '-C', str(REPO), 'show', ref + ':src/packet.py'], capture_output=True, text=True)
            if base_blob.returncode == 0:
                self.assertLessEqual(lines, base_blob.stdout.count('\n'), 'packet.py grew against ' + ref)
                break
        for name in ('test_packet.py', 'test_routing.py'):
            self.assertIn('1578', (REPO / 'src' / name).read_text())
        # Resume rendering lives in the helper.
        self.assertEqual(wip_resume.packet_lines(base, sid), [])
        self.assertTrue(wip_resume.packet_lines(base + [timeout], sid))


if __name__ == '__main__':
    unittest.main()
