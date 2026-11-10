#!/usr/bin/env python3
"""Detached dispatch checks readiness in the parent, before any fork (L-spec-2207).

Scratch only: one isolated DOIT_ROOT, real start_check predicate, real CLI branch
(`dispatch.py` run as __main__). Only the dispatch-child Popen and the version
probe are replaced, so no backend can run.
"""
import atexit, contextlib, warnings, fcntl, hashlib, io, json, os, runpy, shutil, stat, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Isolation is established before dispatch or fold are imported.
_scratch = tempfile.TemporaryDirectory(prefix='prefork-test-')
atexit.register(_scratch.cleanup)
ROOT = Path(_scratch.name).resolve() / 'root'
ROOT.mkdir()
os.environ.update(DOIT_ROOT=str(ROOT), DOIT_NO_POKE='1', PYTHONDONTWRITEBYTECODE='1')
for _key in ('DOIT_LEDGER_FILE', 'DOIT_SUPERVISED', 'DOIT_MAIN'):
    os.environ.pop(_key, None)

import dispatch, fold, grader_packet, precheck, project_scope, spawns, start_check  # noqa: E402

SUBJECT = 'L-spec-9001'
CHARTER = 'L-charter-test'
OUT = Path(_scratch.name) / 'out'
OUT.mkdir()
PACKET = OUT / 'packet.md'
PACKET.write_text('fixture packet\nPINNED_BASE_SHA: X0000000\n')
REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
ROLES = ('builder', 'grader', 'reviewer')
KEEP = json.dumps(dict(v=1, type='note', subject='L-spec-0001', ts='2026-11-01T00:00:00+00:00')) + '\n'


def stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec='seconds')


def git(cwd, *args):
    return REAL_RUN(['git', *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


def tree():
    """Every path under the scratch root: type, mode, size, mtime_ns and bytes (atime excluded)."""
    rows = {}
    for p in sorted(ROOT.rglob('*')):
        st = p.lstat()
        rows[str(p.relative_to(ROOT))] = (stat.S_IFMT(st.st_mode), st.st_mode, st.st_size, st.st_mtime_ns,
                                          p.read_bytes() if stat.S_ISREG(st.st_mode) else None)
    return rows


def version_probe(*args, **kw):
    if args and args[0] == ['claude', '--version']:
        return subprocess.CompletedProcess(args[0], 0, 'fixture 1\n', '')
    return REAL_RUN(*args, **kw)


class Fake:
    pid = 4242


class Base(unittest.TestCase):
    def setUp(self):
        for child in list(ROOT.iterdir()):
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        (ROOT / 'content').mkdir()
        self.repo = ROOT / 'repos' / 'demo'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'one')
        self.local = git(self.repo, 'rev-parse', 'HEAD')
        self.children = []
        self.ready = 'a' * 40
        self.seeded = 0

    # -- ledger fixtures ---------------------------------------------------
    def seed(self, rows, actor='seed'):
        (ROOT / 'events').mkdir(exist_ok=True)
        self.seeded += 1
        path = ROOT / 'events' / f'L-{actor}-{self.seeded:04d}.jsonl'
        path.write_text(''.join(json.dumps({'v': 1, 'subject': SUBJECT, **row}) + '\n' for row in rows))

    def registered(self, **extra):
        return dict(type='spec-written', ts=stamp(30), project='demo', footprint=['src/second.py'], **extra)

    def events(self):
        return fold.read_events(use_cache=False)

    def bundle(self, master, local=None, listed=True):
        directory = ROOT / 'content' / 'evidence' / SUBJECT
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'bundle.json'
        raw = json.dumps(dict(spec=SUBJECT, sha=self.ready, base_sha='c' * 40, master_sha=master,
                              local_master_sha=local, commands=[], comparison={})).encode()
        path.write_bytes(raw)
        rows = [dict(type='build-done', ts=stamp(20), ready_sha=self.ready, project='demo')]
        if listed:
            rows.append(dict(type='evidence-bundle', ts=stamp(10), sha=self.ready, path=str(path),
                             bundle_sha256=hashlib.sha256(raw).hexdigest()))
        return rows

    def hold_lock(self):
        (ROOT / 'state').mkdir(exist_ok=True)
        fd = open(ROOT / 'state' / spawns.lock_name(SUBJECT), 'a')
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(fd.close)

    # -- the production CLI ------------------------------------------------
    def run_cli(self, role, *extra, ledger=None, supervised=False, detach=True, packet=True):
        argv = [dispatch.__file__, role, SUBJECT] + (['--detach'] if detach else []) \
            + (['--packet', str(PACKET)] if packet else []) + list(extra)
        env = {k: v for k, v in os.environ.items() if k not in ('DOIT_LEDGER_FILE', 'DOIT_SUPERVISED')}
        if ledger:
            env['DOIT_LEDGER_FILE'] = ledger
        if supervised:
            env['DOIT_SUPERVISED'] = '1'
        children = []

        def popen(cmd, *a, **kw):
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == dispatch.__file__:
                children.append(cmd)
                return Fake()
            return REAL_POPEN(cmd, *a, **kw)

        out, err = io.StringIO(), io.StringIO()
        code = None
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', argv), \
             patch('subprocess.Popen', popen), patch('subprocess.run', version_probe), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)  # the accepted path leaves its detached log open, as production does
            try:
                runpy.run_path(dispatch.__file__, run_name='__main__')
            except SystemExit as exc:
                code = exc.code
        self.children = children
        return code, out.getvalue(), err.getvalue()

    def assert_refused(self, result, reason):
        code, out, err = result
        self.assertEqual((code, out, err), (3, '', f'refused-before-start: {reason}\n'))
        self.assertEqual(self.children, [])


CALLERS = (('operator-override', None, ('--manual-override-reason', 'repair')),
           ('operator', None, ()), ('tick', 'L-tick-fixture.jsonl', ()))


class ParentRefusalTests(Base):
    """AC1: refusal precedes fork and every ledger/log/cache write, for every caller."""

    def rows(self, role, reason):
        reg = self.registered()
        if reason in ('duplicate-dispatch',):
            opened = {'builder': 'builder', 'grader': 'builder', 'reviewer': 'reviewer'}[role]
            return [reg, dict(type='spawn-started', ts=stamp(1), spawn=f'L-{opened}-123')]
        if reason == 'dispatch-in-progress':
            return [reg]
        if reason == 'wave-order':
            (ROOT / 'content' / f'plan-{CHARTER}.md').write_text(
                '## first\nFootprint:\n- src/first.py\nWave: 1\n## second\nFootprint:\n- src/second.py\nWave: 2\n')
            return [dict(type='spec-written', ts=stamp(40), subject='L-spec-9000', charter=CHARTER,
                         footprint=['src/first.py'], project='demo'), self.registered(charter=CHARTER)]
        if reason == 'spec-shape':
            return [reg, dict(type='spec-shape-failed', ts=stamp(5))]
        return [reg, dict(type='build-done', ts=stamp(20), ready_sha=self.ready, project='demo')]

    def test_matrix(self):
        matrix = [(r, 'duplicate-dispatch') for r in ROLES] + [(r, 'dispatch-in-progress') for r in ROLES] \
            + [('builder', 'wave-order'), ('builder', 'spec-shape'), ('grader', 'bundle-pending')]
        for role, reason in matrix:
            for name, ledger, extra in CALLERS:
                for variant in ('events', 'no-events-dir', 'caller-ledger'):
                    if variant == 'no-events-dir' and reason not in ('dispatch-in-progress', 'bundle-pending'):
                        continue
                    for sentinels in (False, True):
                        with self.subTest(role=role, reason=reason, caller=name, variant=variant, sentinels=sentinels):
                            self.setUp()
                            if variant != 'no-events-dir':
                                self.seed(self.rows(role, reason))
                            if variant == 'caller-ledger' and ledger:
                                (ROOT / 'events' / ledger).write_text(KEEP)
                            elif variant == 'caller-ledger':
                                (ROOT / 'events' / 'L-operator-local.jsonl').write_text(KEEP)
                            if reason == 'dispatch-in-progress':
                                self.hold_lock()
                            if sentinels:
                                for d in ('logs', 'cache'):
                                    (ROOT / d).mkdir()
                                    (ROOT / d / 'sentinel').write_text('keep')
                            before = tree()
                            args = ('--project', 'demo') if variant == 'no-events-dir' else ()
                            result = self.run_cli(role, *args, *extra, ledger=ledger)
                            self.assert_refused(result, reason)
                            self.assertEqual(tree(), before)
                            if not sentinels:
                                self.assertFalse((ROOT / 'logs').exists() or (ROOT / 'cache').exists())

    def test_supervised_and_cwd_forms_refuse_too(self):
        self.seed([self.registered(), dict(type='spawn-started', ts=stamp(1), spawn='L-builder-1')])
        before = tree()
        self.assert_refused(self.run_cli('builder', supervised=True), 'duplicate-dispatch')
        self.assert_refused(self.run_cli('builder', '--project', 'demo', '--cwd', str(self.repo)), 'duplicate-dispatch')
        self.assertEqual(tree(), before)


class ParentAllowTests(Base):
    """AC2: an eligible request reaches the child once, with current context."""

    def spies(self, stack):
        order, reads = [], []
        real_read, real_subject, real_refusal = fold.read_events, project_scope.subject_project, start_check.refusal

        def read(*a, **kw):
            result = real_read(*a, **kw)
            reads.append((kw, result))
            order.append('read')
            return result

        def subject(events, subject):
            order.append('project')
            self.assertTrue(any(events is r for _, r in reads))
            return real_subject(events, subject)

        seen = []

        def refusal(*a, **kw):
            order.append('predicate')
            seen.append((a, kw))
            return real_refusal(*a, **kw)

        stack.enter_context(patch.object(fold, 'read_events', read))
        stack.enter_context(patch.object(project_scope, 'subject_project', subject))
        stack.enter_context(patch.object(start_check, 'refusal', refusal))
        stack.enter_context(patch.object(fold, 'subject_project', side_effect=AssertionError('cached project read')))
        return order, reads, seen

    def test_eligible_builder_reviewer_grader_explicit_and_omitted(self):
        for role in ROLES:
            for explicit in (False, True):
                with self.subTest(role=role, explicit=explicit), contextlib.ExitStack() as stack:
                    self.setUp()
                    self.seed([self.registered()] + (self.bundle(self.local) if role == 'grader' else []))
                    order, reads, seen = self.spies(stack)
                    extra = ('--project', 'demo', '--cwd', str(self.repo)) if explicit else ()
                    code, out, err = self.run_cli(role, *extra, '--charter', CHARTER, ledger='L-tick-fixture.jsonl')
                    self.assertEqual((code, err), (0, ''), err)
                    self.assertEqual(json.loads(out)['detached'], 4242)
                    self.assertEqual(sorted(json.loads(out)), ['detached', 'log', 'role', 'subject'])
                    self.assertEqual(len(self.children), 1)
                    self.assertNotIn('--detach', self.children[0])
                    self.assertEqual(self.children[0][2:5], [role, SUBJECT, '--packet'])
                    self.assertEqual(reads[0][0], {'use_cache': False})
                    self.assertLess(order.index('read'), order.index('predicate'))
                    self.assertGreaterEqual(order.count('project'), 1)  # every project lookup (checked in the spy) used the one fresh snapshot
                    (_, kw), = seen
                    self.assertIsNotNone(kw['now'].tzinfo)
                    self.assertIsNotNone(kw['now'].utcoffset())
                    self.assertEqual((kw['root'], kw['charter']), (ROOT, CHARTER))
                    self.assertIs(seen[0][0][2], reads[0][1])
                    self.assertEqual(seen[0][0][:2], (role, SUBJECT))
                    self.assertFalse((ROOT / 'cache').exists())

    def test_new_refusal_seen_after_stale_snapshot(self):
        self.seed([self.registered()])
        stale = self.events()
        self.assertIsNone(start_check.refusal('builder', SUBJECT, stale, now=datetime.now(timezone.utc), root=ROOT))
        self.seed([dict(type='spawn-started', ts=stamp(1), spawn='L-builder-5')])
        before = tree()
        self.assert_refused(self.run_cli('builder', ledger='L-tick-fixture.jsonl'), 'duplicate-dispatch')
        self.assertEqual(tree(), before)
        self.assertFalse((ROOT / 'cache').exists())

    def origin_repo(self):
        """origin/fixture-main plus a diverging local fixture-main."""
        bare = Path(_scratch.name) / 'origin.git'
        shutil.rmtree(bare, ignore_errors=True)
        git(self.repo, 'branch', '-m', 'fixture-main')
        git(Path(_scratch.name), 'init', '-q', '--bare', str(bare))
        git(self.repo, 'remote', 'add', 'origin', str(bare))
        git(self.repo, 'push', '-q', 'origin', 'fixture-main')
        git(self.repo, 'fetch', '-q', 'origin')
        remote = git(self.repo, 'rev-parse', 'origin/fixture-main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'local only')
        return remote, git(self.repo, 'rev-parse', 'fixture-main')

    def grade(self, *extra, env=None):
        with patch.dict(os.environ, env or {}), patch.object(precheck, 'master_sha', wraps=precheck.master_sha) as spy:
            result = self.run_cli('grader', *extra, ledger='L-tick-fixture.jsonl')
        return result, spy

    def test_grader_uses_remote_master_identity(self):
        remote, local = self.origin_repo()
        self.assertNotEqual(remote, local)
        for extra in ((), ('--project', 'demo', '--cwd', str(self.repo))):
            with self.subTest(extra=extra):
                self.setUp(); remote, local = self.origin_repo()
                self.seed([self.registered()] + self.bundle(remote))
                (code, out, err), spy = self.grade(*extra, env={'DOIT_MAIN': 'fixture-main'})
                self.assertEqual(code, 0, err)
                self.assertEqual(spy.call_args_list[0].args, (ROOT / 'repos' / 'demo', 'origin/fixture-main'))
                self.assertEqual(len(self.children), 1)
        # A bundle recorded at a stale main stays pending against the fresh one.
        self.setUp(); remote, local = self.origin_repo()
        self.seed([self.registered()] + self.bundle('d' * 40))
        before = tree()
        (result, spy) = self.grade(env={'DOIT_MAIN': 'fixture-main'})
        self.assert_refused(result, 'bundle-pending')
        self.assertEqual(tree(), before)
        # The local tip is never the argument: a bundle at the local tip is not ready.
        self.setUp(); remote, local = self.origin_repo()
        self.seed([self.registered()] + self.bundle(local, local))
        (result, spy) = self.grade(env={'DOIT_MAIN': 'fixture-main'})
        self.assert_refused(result, 'bundle-pending')

    def test_grader_local_main_fallback_and_terminal_failure(self):
        self.seed([self.registered()] + self.bundle(self.local, self.local))
        (code, out, err), spy = self.grade()
        self.assertEqual(code, 0, err)
        self.assertEqual([c.args[1] for c in spy.call_args_list], ['origin/main', 'main'])
        self.setUp()
        self.seed([self.registered()] + self.bundle(self.local))
        before = tree()
        with patch.object(start_check, 'refusal', side_effect=AssertionError('called with None')):
            (result, _) = self.grade(env={'DOIT_MAIN': 'no-such-branch'})
            self.assert_refused(result, 'bundle-pending')
            self.assertEqual(tree(), before)
            shutil.rmtree(self.repo / '.git')  # no repository at all: nothing resolves
            before = tree()
            (result, _) = self.grade()
            self.assert_refused(result, 'bundle-pending')
            self.assertEqual(tree(), before)

    def test_accepted_override_audits_once_and_unauthorized_operator_is_refused(self):
        self.seed([self.registered()])
        code, out, err = self.run_cli('builder', '--manual-override-reason', 'repair')
        self.assertEqual(code, 0)
        self.assertEqual(len(self.children), 1)
        # The accepted override is audited by the child, once; the parent adds no row.
        self.assertEqual([e for e in self.events() if e['type'] == 'manual-dispatch'], [])
        self.assertEqual(self.children[0][-2:], ['--manual-override-reason', 'repair'])
        code, out, err = self.run_cli('builder')
        self.assertIn('requires --manual-override-reason', str(code))
        self.assertEqual(len(self.children), 0)
        self.assertEqual([e['outcome'] for e in self.events() if e['type'] == 'manual-dispatch'], ['refused'])


class RetainedGateTests(Base):
    """AC3: state that turns unready after the parent passes still meets the in-process gates."""

    def child(self, role, *extra):
        """The child half of a detached start: the same arguments, no --detach, real admission and emit."""
        result = self.run_cli(role, *extra, ledger='L-tick-fixture.jsonl', detach=False)
        return result

    def failures(self):
        return [e for e in fold.read_events(use_cache=False) if e['type'] == 'spawn-failed']

    def test_parent_passes_then_child_refuses(self):
        cases = {'duplicate-dispatch': [dict(type='spawn-started', ts=stamp(1), spawn='L-builder-5')],
                 'spec-shape': [dict(type='spec-shape-failed', ts=stamp(1))]}
        for reason, extra in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                self.seed([self.registered()])
                self.assertEqual(self.run_cli('builder', ledger='L-tick-fixture.jsonl')[0], 0)
                self.seed(extra)
                with patch.object(dispatch, 'run_claude', side_effect=AssertionError('backend')):
                    code, out, err = self.child('builder')
                self.assertEqual(code, 1, err)
                failed = self.failures()
                self.assertEqual([e['reason'] for e in failed], [reason])
                self.assertEqual(failed[0]['subject'], SUBJECT)

    def test_wave_lock_and_bundle_refusals_remain_in_process(self):
        (ROOT / 'content' / f'plan-{CHARTER}.md').write_text(
            '## first\nFootprint:\n- src/first.py\nWave: 1\n## second\nFootprint:\n- src/second.py\nWave: 2\n')
        self.seed([self.registered(charter=CHARTER)])
        self.assertEqual(self.run_cli('builder', ledger='L-tick-fixture.jsonl')[0], 0)
        self.seed([dict(type='spec-written', ts=stamp(40), subject='L-spec-9000', charter=CHARTER,
                        footprint=['src/first.py'], project='demo')])
        self.assertEqual(self.child('builder')[0], 1)
        self.assertEqual([e['reason'] for e in self.failures()], ['wave-order'])
        self.setUp()
        self.seed([self.registered()])
        self.hold_lock()
        self.assertEqual(self.child('builder')[0], 1)
        self.assertEqual([e['reason'] for e in self.failures()], ['dispatch-in-progress'])
        self.setUp()
        self.seed([self.registered(), dict(type='build-done', ts=stamp(20), ready_sha=self.ready, project='demo')])
        with patch.object(grader_packet.subprocess, 'Popen', side_effect=AssertionError('run-checks')), \
             patch.object(dispatch, 'run_claude', side_effect=AssertionError('backend')):
            code, out, err = self.child('grader')
        self.assertEqual(code, 1)
        self.assertEqual([e['reason'] for e in self.failures()], ['bundle-pending'])


class CompatibilityTests(Base):
    """AC4: other routes are unchanged; context errors defer to the child; failures close."""

    def test_non_detached_and_unrelated_roles_skip_the_parent_check(self):
        self.seed([self.registered(), dict(type='spawn-started', ts=stamp(1), spawn='L-builder-1')])
        with patch.object(start_check, 'refusal', side_effect=AssertionError('parent check')):
            with patch.object(dispatch, 'run_claude', side_effect=AssertionError('backend')):
                code, out, err = self.run_cli('builder', '--manual-override-reason', 'x', detach=False)
            self.assertEqual(code, 1)  # the in-process gate refuses, as before
            self.assertNotIn('refused-before-start', err)
            self.assertEqual([e['reason'] for e in fold.read_events(use_cache=False) if e['type'] == 'spawn-failed'],
                             ['duplicate-dispatch'])
            code, out, err = self.run_cli('research', '--manual-override-reason', 'x')
            self.assertEqual(code, 0)
            code, out, err = self.run_cli('builder', '--manual-override-reason', 'x', packet=False)
            self.assertEqual(code, '--detach needs --packet FILE')

    def test_every_caller_context_gets_exit_3(self):
        self.seed([self.registered(), dict(type='spawn-started', ts=stamp(1), spawn='L-builder-1')])
        before = tree()
        for ledger, supervised, extra in (('L-operator-local.jsonl', False, ('--manual-override-reason', 'x')),
                                          (None, True, ()), ('L-tick-fixture.jsonl', False, ()),
                                          ('L-executor-fixture.jsonl', False, ())):
            self.assert_refused(self.run_cli('builder', *extra, ledger=ledger, supervised=supervised), 'duplicate-dispatch')
        self.assertEqual(tree(), before)

    def test_context_errors_defer_to_the_child(self):
        # Unregistered project: the child keeps its own preflight:cwd-unresolved diagnostic.
        self.seed([dict(type='spec-written', ts=stamp(30), project='missing', footprint=[]),
                   dict(type='spawn-started', ts=stamp(1), spawn='L-builder-1')])
        self.assertEqual(self.run_cli('builder', ledger='L-tick-fixture.jsonl')[0], 0)
        self.assertEqual(len(self.children), 1)
        code, out, err = self.run_cli('builder', ledger='L-tick-fixture.jsonl', detach=False)
        self.assertIn('preflight:cwd-unresolved', str(code) + err)
        # No registered project at all.
        self.setUp()
        self.seed([dict(type='spec-shape-failed', ts=stamp(1))])
        self.assertEqual(self.run_cli('builder', ledger='L-tick-fixture.jsonl')[0], 0)
        # An explicit project that differs from the registered one: parent success, child wrong-project.
        self.setUp()
        (ROOT / 'repos' / 'other').mkdir()
        self.seed([self.registered()])
        self.assertEqual(self.run_cli('builder', '--project', 'other', ledger='L-tick-fixture.jsonl')[0], 0)
        self.assertEqual(len(self.children), 1)
        with patch.object(dispatch, 'run_claude', side_effect=AssertionError('backend')):
            code, out, err = self.run_cli('builder', '--project', 'other', ledger='L-tick-fixture.jsonl', detach=False)
        self.assertEqual(code, 1, err)
        failed = [e for e in fold.read_events(use_cache=False) if e['type'] == 'spawn-failed']
        self.assertEqual([e['reason'] for e in failed], ['wrong-project'])

    def test_unreadable_context_fails_closed(self):
        self.seed([self.registered()])
        for name, target, kwargs in (('ledger', fold, dict(attribute='read_events', side_effect=OSError('read'))),
                                     ('predicate', start_check, dict(attribute='refusal', side_effect=KeyError('boom')))):
            with self.subTest(name), patch.object(kwargs.pop('target', target), kwargs.pop('attribute'), **kwargs):
                before = tree()
                self.assert_refused(self.run_cli('builder', ledger='L-tick-fixture.jsonl'), 'preflight:start-context-unavailable')
                self.assertEqual(tree(), before)


if __name__ == '__main__':
    unittest.main()
