"""Scratch-only regressions for the advisory start eligibility seam."""
import ast
import atexit
import builtins
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

# The combined AC4/AC5 suites import this harness first. Keep their legacy
# append fixtures inside a process-owned root as well as scoping each predicate.
_PROCESS_ROOT = tempfile.TemporaryDirectory(prefix='start-check-process-')
atexit.register(_PROCESS_ROOT.cleanup)
os.environ['DOIT_ROOT'] = _PROCESS_ROOT.name
os.environ.pop('DOIT_LEDGER_FILE', None)

import grader_packet
import spawns
import project_scope

SUBJECT = 'L-spec-9001'
READY = 'a' * 40
MAIN = 'b' * 40
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def event(event_type, subject=SUBJECT, ts='2026-11-08T11:00:00Z', **fields):
    return dict(type=event_type, subject=subject, ts=ts, **fields)


def acquire_child(root, subject, conn):
    # Real acquisition in a different process, never called by the predicate.
    lock, refusal = spawns.acquire(root, 'builder', subject, None, [])
    conn.send(refusal)
    conn.recv()
    if lock:
        lock.close()
    conn.close()


class ScratchTests(unittest.TestCase):
    def setUp(self):
        # Legacy regression modules loaded in the same suite establish their own
        # roots before importing dispatch/fold/tick. Do not bind those globals
        # while unittest is still importing the suite's modules.
        global start_check, dispatch, fold
        import start_check
        import dispatch
        import fold
        tmp = tempfile.TemporaryDirectory(prefix='start-check-')
        self.addCleanup(tmp.cleanup)
        # TMPDIR may itself be a symlink. The read guard resolves candidates,
        # so configure every fixture reader with the same canonical root.
        self.root = Path(tmp.name).resolve()
        self.content = self.root / 'content'
        self.content.mkdir()
        (self.root / 'repos/demo').mkdir(parents=True)
        self.directory = self.content / 'evidence' / SUBJECT
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'bundle.json'
        self.data = dict(spec=SUBJECT, sha=READY, base_sha='c' * 40,
                         master_sha=MAIN, local_master_sha=MAIN,
                         commands=[], comparison={})
        self.build = event('build-done', ready_sha=READY, project='demo')
        self.bundle = event('evidence-bundle', ts='2026-11-08T11:01:00Z',
                            sha=READY, path=str(self.path))
        self.publish()
        self.events = [self.build, self.bundle]
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(grader_packet, 'root', return_value=self.root))
        stack.enter_context(patch.object(dispatch, 'CONTENT', self.content))
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root)))

    def publish(self):
        raw = json.dumps(self.data).encode()
        self.path.write_bytes(raw)
        self.bundle['bundle_sha256'] = hashlib.sha256(raw).hexdigest()

    def state(self, events=None, main=MAIN):
        return grader_packet.evidence_state(self.events if events is None else events,
                                          SUBJECT, main_sha=main)

    def refusal(self, role='builder', events=None, **kw):
        return start_check.refusal(role, SUBJECT, self.events if events is None else events,
                                   now=NOW, root=self.root, main_sha=MAIN, **kw)

    @contextmanager
    def read_only(self):
        """Guard calls, including eligibility reads, after fixture creation."""
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        original_open, original_path_open = builtins.open, Path.open

        def allowed(path, mode):
            if isinstance(path, int):
                return
            p = Path(path).resolve()
            if any(c in mode for c in 'wax+'):
                raise AssertionError(f'write during predicate: {p}')
            if not (p.is_relative_to(self.root) or str(p).startswith('/proc/')
                    or p.suffix == '.py' and p.is_relative_to(Path(__file__).parent)):
                raise AssertionError(f'non-scratch eligibility read: {p}')

        def safe_open(path, mode='r', *args, **kw):
            allowed(path, mode)
            return original_open(path, mode, *args, **kw)

        def safe_path_open(path, mode='r', *args, **kw):
            allowed(path, mode)
            return original_path_open(path, mode, *args, **kw)

        with ExitStack() as stack:
            stack.enter_context(patch('builtins.open', side_effect=safe_open))
            stack.enter_context(patch.object(Path, 'open', safe_path_open))
            for target in ('subprocess.Popen', 'fold.read_events', 'fold.append',
                           'fcntl.flock', 'os.mkdir', 'os.remove', 'os.unlink',
                           'os.rename', 'os.open'):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            yield
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


class EvidenceStateTests(ScratchTests):
    def test_selection_matrix(self):
        cases = [([], 'pending'), ([self.build], 'pending'),
                 ([dict(self.build, ready_sha=None), self.bundle], 'pending'),
                 (self.events, 'ready'),
                 ([self.build, dict(self.bundle, sha=READY[:9])], 'ready'),
                 ([self.build, dict(self.bundle, sha='d' * 40)], 'pending'),
                 ([self.build, dict(self.bundle, criteria=['AC1'])], 'pending')]
        for ts, expected in [('2026-11-08T10:59:00Z', 'pending'),
                             (self.build['ts'], 'pending'),
                             (self.bundle['ts'], 'ready')]:
            cases.append(([self.build, dict(self.bundle, ts=ts)], expected))
        amendment = event('spec-amended', ts=self.bundle['ts'])
        cases += [(self.events + [amendment], 'pending'),
                  ([self.build, amendment, dict(self.bundle, ts='2026-11-08T11:02:00Z')], 'ready')]
        failure = dict(self.bundle, type='evidence-bundle-failed', reason='broken')
        cases += [(self.events + [failure], 'failed'),
                  (self.events + [failure, dict(self.bundle)], 'ready'),
                  ([self.build, failure, self.bundle], 'ready'),
                  ([self.build, dict(failure, ts='2026-11-08T11:02:00Z'), self.bundle], 'ready'),
                  ([dict(self.build, ts=None), dict(failure, ts=None)], 'failed'),
                  ([dict(self.build, ts=None), self.bundle], 'pending'),
                  ([self.build, dict(failure, ts=None)], 'pending')]
        for events, expected in cases:
            with self.subTest(events=events), self.read_only():
                state = self.state(events)
                self.assertEqual(state.state, expected)
                self.assertEqual(state.ready_sha, (events[0].get('ready_sha') if events else None))
        self.assertEqual(self.state(main=None).state, 'pending')
        self.assertEqual(self.state(self.events + [failure], main=None).state, 'failed')

    def test_identity_and_hostile_paths(self):
        for fields in ({'master_sha': None}, {'master_sha': 'd' * 40},
                       {'local_master_sha': 'd' * 40}, {'master_sha': ''},
                       {'master_sha': MAIN[:9]}, {'local_master_sha': MAIN[:9]}):
            with self.subTest(fields=fields):
                self.path.write_text(json.dumps(dict(self.data, **fields)))
                state = self.state()
                self.assertEqual(state.state, 'pending')
                self.assertEqual(state.bundle_event, self.bundle)
        self.path.write_text('{invalid')
        self.assertEqual(self.state().state, 'pending')
        self.path.write_text('[]')
        self.assertEqual(self.state().state, 'pending')
        self.path.unlink()
        self.assertEqual(self.state().state, 'pending')
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(self.data))
        self.path.symlink_to(outside)
        self.assertEqual(self.state().state, 'pending')
        self.path.unlink()
        for path in (str(outside), str(self.directory / '../outside.json')):
            self.assertEqual(self.state([self.build, dict(self.bundle, path=path)]).state, 'pending')
        self.publish()
        with patch.object(Path, 'read_bytes', side_effect=PermissionError):
            self.assertEqual(self.state().state, 'pending')
        # Main identity comes from the file, never from event base_sha.
        stale = dict(self.data, base_sha=MAIN, master_sha='d' * 40)
        self.path.write_text(json.dumps(stale))
        self.assertEqual(self.state().state, 'pending', 'L-spec-2133 stale main')
        self.path.write_text(json.dumps(dict(self.data, local_master_sha=None)))
        self.assertEqual(self.state().state, 'ready')
        # Containment includes the subject directory, not just the event path.
        escaped = self.content / 'escaped'
        escaped.mkdir()
        escape_path = escaped / 'bundle.json'
        escape_path.write_text(json.dumps(self.data))
        (self.content / 'evidence' / 'L-spec-link').symlink_to(escaped, target_is_directory=True)
        for subject in ('../escaped', 'L-spec-link'):
            events = [dict(self.build, subject=subject),
                      dict(self.bundle, subject=subject, path=str(escape_path))]
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('escaped identity read')):
                self.assertEqual(grader_packet.evidence_state(events, subject, main_sha=MAIN).state, 'pending')

    def test_real_bundle_for_accepts_different_base_and_current_file_main(self):
        self.assertNotEqual(self.data['base_sha'], self.data['master_sha'])
        self.assertNotIn('master_sha', self.bundle)
        import build_runner
        text = '## Acceptance criteria\nAC1 [backend]: scratch identity fixture.\n'
        (self.content / f'{SUBJECT}.md').write_text(text)
        key = grader_packet.evidence_revision.bundle_key(text, self.events, SUBJECT, READY,
                                                       self.data['base_sha'])
        self.data.update(project='demo', bundle_key=key, criteria=None,
                         spec_sha256=grader_packet.evidence_revision.digest(text),
                         runner_version=build_runner.VERSION)
        self.bundle.update(project='demo', bundle_key=key, base_sha=self.data['base_sha'])
        self.publish()
        with patch.object(build_runner, '_event') as reused, \
                patch('subprocess.Popen', side_effect=AssertionError('paid launch')):
            self.assertIsNotNone(grader_packet.bundle_for(
                self.events, SUBJECT, READY, identity=(self.data['base_sha'], MAIN, MAIN)))
            reused.assert_called_once()
        with self.read_only():
            self.assertEqual(self.state().state, 'ready')
            self.assertIsNone(self.refusal('grader'))

    def test_key_and_one_identity_read(self):
        original = self.state().key
        self.assertEqual(original, self.state().key)
        variants = [self.events + [dict(self.build)],
                    self.events + [event('spec-amended')],
                    [dict(self.build, ready_sha='e' * 40), self.bundle]]
        for events in variants:
            self.assertNotEqual(original, self.state(events).key)
        self.assertNotEqual(original, self.state(main='e' * 40).key)
        self.assertNotEqual(original, grader_packet.evidence_state([], 'other', main_sha=MAIN).key)
        original_read = Path.read_bytes
        reads = []
        def read(path):
            reads.append(path)
            return original_read(path)
        with patch.object(Path, 'read_bytes', read), self.read_only():
            # read_only snapshots also read files; count only the actual call.
            reads.clear()
            self.state()
            self.assertEqual(reads, [self.path])


class LockProbeTests(ScratchTests):
    def holder(self, subject):
        parent, child = multiprocessing.Pipe()
        proc = multiprocessing.Process(target=acquire_child, args=(self.root, subject, child))
        proc.start()
        child.close()
        self.assertTrue(parent.poll(5), 'acquire child did not answer')
        self.assertIsNone(parent.recv())
        def release():
            parent.send('release')
            proc.join(5)
            if proc.is_alive():
                proc.terminate()
                proc.join()
            parent.close()
            self.assertEqual(proc.exitcode, 0)
        self.addCleanup(release)
        return release

    def test_absent_held_released_distinct_inodes_and_safe_subject(self):
        absent = self.root / 'missing'
        self.assertFalse(spawns.lock_held('x', absent))
        self.assertFalse(absent.exists())
        self.assertFalse(spawns.lock_held(SUBJECT, self.root))
        self.assertFalse((self.root / 'state').exists())
        subject = 'nested/subject'
        state = self.root / 'state'
        state.mkdir()
        watched = state / spawns.lock_name(subject)
        watched.write_bytes(b'unchanged')
        other = state / spawns.lock_name('other')
        other.write_bytes(b'other')
        with watched.open('rb') as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertTrue(spawns.lock_held(subject, self.root))
            self.assertFalse(spawns.lock_held('other', self.root))
        self.assertFalse(spawns.lock_held(subject, self.root))
        self.assertEqual(watched.read_bytes(), b'unchanged')
        release = self.holder(subject)
        self.assertTrue(spawns.lock_held(subject, self.root))
        # Run this cleanup now, removing it from unittest's cleanup stack.
        self._cleanups.pop()
        release()
        self.assertFalse(spawns.lock_held(subject, self.root))

    def test_unreadable_observation_is_held(self):
        (self.root / 'state').mkdir()
        (self.root / 'state' / spawns.lock_name(SUBJECT)).touch()
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertTrue(spawns.lock_held(SUBJECT, self.root))
        with patch('builtins.open', side_effect=PermissionError):
            self.assertTrue(spawns.lock_held(SUBJECT, self.root))

    def test_probe_descriptor_does_not_contend_with_real_acquire(self):
        (self.root / 'state').mkdir()
        path = self.root / 'state' / spawns.lock_name(SUBJECT)
        path.write_bytes(b'keep')
        entered, resume = threading.Event(), threading.Event()
        read_text = Path.read_text
        def pause(path, *args, **kwargs):
            if str(path) == '/proc/locks':
                entered.set()
                if not resume.wait(5):
                    raise AssertionError('probe was not resumed')
            return read_text(path, *args, **kwargs)
        result = []
        with patch.object(Path, 'read_text', pause), patch('fcntl.flock', side_effect=AssertionError('probe flock')):
            thread = threading.Thread(target=lambda: result.append(spawns.lock_held(SUBJECT, self.root)))
            thread.start()
            self.assertTrue(entered.wait(5))
        # Patch is gone in the child; the probe descriptor stays open while paused.
        try:
            release = self.holder(SUBJECT)
            resume.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [True])
            self.assertTrue(spawns.lock_held(SUBJECT, self.root))
        finally:
            resume.set()
            thread.join(5)
        self._cleanups.pop()
        release()
        self.assertFalse(spawns.lock_held(SUBJECT, self.root))
        self.assertEqual(path.read_bytes(), b'keep')


class BuilderEligibilityTests(ScratchTests):
    def wave_events(self):
        (self.content / 'plan-L-charter-test.md').write_text(
            '## first\nFootprint:\n- src/first.py\nWave: 1\n'
            '## second\nFootprint:\n- src/second.py\nWave: 2\n')
        return [event('spec-written', subject='L-spec-9000', charter='L-charter-test', footprint=['src/first.py']),
                event('spec-written', charter='L-charter-test', footprint=['src/second.py']),
                event('spec-written', footprint=[])]

    def test_wave_delegation_and_precedence(self):
        events = self.wave_events() + [event('spec-shape-failed')]
        with self.read_only(), patch.object(dispatch, 'resolve_charter', wraps=dispatch.resolve_charter) as resolve, \
                patch.object(dispatch, 'spec_wave', wraps=dispatch.spec_wave) as wave, \
                patch.object(dispatch, 'wave_blocker', wraps=dispatch.wave_blocker) as blocker:
            result = self.refusal(events=events)
            self.assertEqual(result.reason, 'wave-order')
            resolve.assert_called_once_with(events, SUBJECT, None)
            wave.assert_any_call('L-charter-test', ['src/second.py'])
            blocker.assert_called_once_with(events, 'L-charter-test', SUBJECT, 2)
        self.assertEqual(self.refusal(events=events, charter='/x/L-charter-test.md').reason, 'wave-order')
        settled = events + [event('spec-closed', subject='L-spec-9000')]
        self.assertEqual(self.refusal(events=settled).reason, 'spec-shape')
        for exemption in (event('build-done'), event('shipped'), event('decision', wave_waive='yes')):
            self.assertEqual(self.refusal(events=events + [exemption]).reason, 'spec-shape')
        waived = events + [event('decision', wave_waive='yes'),
                           event('decision', ts='2026-11-08T11:02:00Z', wave_waive='no')]
        self.assertEqual(self.refusal(events=waived).reason, 'wave-order')
        cleared = settled + [event('spec-written')]
        self.assertIsNone(self.refusal(events=cleared))
        for role in ('grader', 'reviewer'):
            result = self.refusal(role, events=events)
            self.assertTrue(result is None or result.reason == 'bundle-pending')

    def test_unknown_charter_plan_wave_and_shape(self):
        for events, charter in (([], None), ([], 'L-charter-missing'),
                                ([event('spec-written', footprint=['unknown'])], 'L-charter-test')):
            with self.read_only():
                self.assertIsNone(self.refusal(events=events, charter=charter))
        self.assertEqual(self.refusal(events=[event('spec-shape-failed')]).reason, 'spec-shape')
        self.assertIsNone(self.refusal(events=[event('spec-shape-failed'), event('spec-written')]))


class GateParityTests(ScratchTests):
    def gate(self, packet=None, events=None):
        emitted = []
        def fail(why, **fields):
            raise RuntimeError(fields['reason'])
        result = grader_packet.gate(self.events if events is None else events, SUBJECT,
                                    packet if packet is not None else 'bundle_sha256: ' + self.bundle['bundle_sha256'],
                                    lambda *a, **kw: emitted.append((a, kw)), fail)
        return result, emitted

    def test_gate_uses_selection_but_dispatch_validates_files(self):
        with patch.object(grader_packet, 'evidence_state', wraps=grader_packet.evidence_state) as state, \
                patch('subprocess.Popen', side_effect=AssertionError('unexpected runner')):
            self.assertEqual(self.state(main=None).state, 'pending')
            self.assertIsNotNone(self.gate()[0])
            self.assertTrue(state.called)
        for corruption in ('missing', 'corrupt'):
            self.publish()
            if corruption == 'missing':
                self.path.unlink()
            else:
                self.path.write_text('broken')
            with patch('subprocess.Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'preflight:runner'):
                    self.gate()
                launch.assert_not_called()
        self.publish()
        with patch('subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'bundle-pending'):
                self.gate(packet='wrong digest')
            launch.assert_called_once()
        self.bundle['bundle_key'] = 'stable-unreadable'
        self.path.unlink()
        with patch('subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'bundle-pending'):
                self.gate()
            launch.assert_called_once()

    def test_failure_order_and_wall_cap_claim(self):
        failure = dict(self.bundle, type='evidence-bundle-failed', reason='broken')
        with patch('subprocess.Popen', side_effect=AssertionError('runner')):
            with self.assertRaisesRegex(RuntimeError, 'preflight:runner'):
                self.gate(events=self.events + [failure])
            self.assertIsNotNone(self.gate(events=[self.build, failure, self.bundle])[0])
            self.assertIsNotNone(self.gate(events=self.events + [failure, dict(self.bundle)])[0])
        wall = dict(failure, reason='wall cap exceeded', reason_code='wall-cap')
        for claimed in (False, True):
            events = self.events + [wall, event('infra-failure', role='grader')]
            if claimed:
                events.append(event('grading-preflight-failed', sha=READY, reason='wall-cap-retry'))
            emitted = []
            def fail(why, **kw):
                raise RuntimeError(kw['reason'])
            with patch('subprocess.Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'wall-cap-retry'):
                    grader_packet.gate(events, SUBJECT, '', lambda *a, **kw: emitted.append(kw), fail)
                self.assertEqual(launch.call_count, int(not claimed))
                self.assertEqual(len(emitted), int(not claimed))


class DuplicateEligibilityTests(ScratchTests):
    def start(self, role='builder', subject=SUBJECT, **kw):
        return event('spawn-started', subject=subject, spawn=f'L-{role}-123', **kw)

    def test_nine_role_pairs_and_shared_readers(self):
        expected = {'builder': {'builder', 'grader', 'reviewer'},
                    'grader': {'builder', 'grader'}, 'reviewer': {'builder', 'reviewer'}}
        for requested in expected:
            for opened in expected:
                events = self.events + [self.start(opened)]
                with self.read_only(), patch.object(spawns, 'open_spawns', wraps=spawns.open_spawns) as reader, \
                        patch.object(spawns, 'conflict', wraps=spawns.conflict) as conflict:
                    result = self.refusal(requested, events=events)
                    blocked = bool(result and result.reason == 'duplicate-dispatch')
                    self.assertEqual(blocked, opened in expected[requested])
                    reader.assert_called_once_with(events, NOW)
                    self.assertEqual(conflict.call_args.args[0:2], (requested, SUBJECT))
                print(f'{requested}/{opened}: {"duplicate-dispatch" if blocked else "allowed"}')

    def test_ended_filter_matches_dispatch_source_expression(self):
        # main is the crash-handling wrapper; _main contains its dispatch gates.
        tree = ast.parse(inspect.getsource(dispatch._main))
        ended = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'ended' for t in node.targets))
        gate = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'gate'
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'spawns')
        for kind, fields in [('infra-failure', {}), ('escalation-blocking', {'kind': 'failure-unknown'}),
                             ('escalation-blocking', {'kind': 'infra-exhausted'}),
                             ('escalation-blocking', {'kind': 'other'}), ('infra-failure', {'attempt': None})]:
            for form in spawns.START_TYPES:
                start = dict(self.start(), type=form)
                terminal = event(kind, attempt=start['spawn'])
                terminal.update(fields)
                # Include another subject and a missing-attempt start too.
                events = [start, terminal, self.start(subject='L-spec-9002', worktree='elsewhere'),
                          event(form, spawn=None, role='builder')]
                env = dict(inherited=events, spawns=spawns)
                env['ended'] = eval(compile(ast.Expression(ended.value), '<dispatch-ended>', 'eval'), env)
                reference = eval(compile(ast.Expression(gate.args[4]), '<dispatch-filter>', 'eval'), env)
                self.assertEqual(start_check._without_ended_attempts(events), reference)

    def test_terminal_stale_vanished_paired_and_lock_precedence(self):
        start = self.start()
        for terminal in spawns.TERMINAL:
            self.assertIsNone(self.refusal(events=[start, event(terminal, spawn=start['spawn'])]))
        self.assertIsNone(self.refusal(events=[dict(start, ts='2020-01-01T00:00:00Z')]))
        vanished = dict(start, waiter_pid=99999999, waiter_host=socket.gethostname())
        self.assertIsNone(self.refusal(events=[vanished]))
        self.assertIsNone(self.refusal(events=[self.start(subject='L-spec-other')]))
        paired = [dict(start, type='build-started'), dict(start, worktree='somewhere')]
        self.assertEqual(len(spawns.open_spawns(paired, NOW)), 1)
        self.assertEqual(self.refusal(events=paired).reason, 'duplicate-dispatch')
        with patch.object(spawns, 'lock_held', return_value=True):
            self.assertEqual(self.refusal(events=paired).reason, 'dispatch-in-progress')
        for terminal in (event('infra-failure', attempt=start['spawn']),
                         event('escalation-blocking', attempt=start['spawn'], kind='failure-unknown'),
                         event('escalation-blocking', attempt=start['spawn'], kind='infra-exhausted')):
            self.assertIsNone(self.refusal(events=paired + [terminal]))

    def test_default_worktree_project_uses_snapshot_and_missing_project_still_checks_subject(self):
        written = event('spec-written', project='demo')
        cwd = spawns.default_cwd('builder', SUBJECT, 'demo', self.root)
        other = self.start(subject='L-spec-other', worktree=cwd)
        with patch.object(project_scope, 'subject_project', wraps=project_scope.subject_project) as project, \
                patch.object(fold, 'subject_project', side_effect=AssertionError('ledger reread')):
            events = [written, other]
            self.assertEqual(self.refusal(events=events).reason, 'duplicate-dispatch')
            project.assert_called_once_with(events, SUBJECT)
        for project in (None, 'unregistered'):
            events = [event('spec-written', project=project), self.start()]
            self.assertEqual(self.refusal(events=events).reason, 'duplicate-dispatch')
            self.assertIsNone(self.refusal(events=events[:1]))


class ReadOnlyContractTests(BuilderEligibilityTests):
    def test_symlinked_tmpdir_keeps_scratch_read_boundaries(self):
        target = self.root / 'scratch-real'
        target.mkdir()
        alias = self.root / 'scratch-alias'
        alias.symlink_to(target, target_is_directory=True)
        code = (
            "import sys, unittest; sys.path.insert(0, 'src'); "
            "suite = unittest.defaultTestLoader.loadTestsFromNames(["
            "'test_start_check.EvidenceStateTests', "
            "'test_start_check.BuilderEligibilityTests', "
            "'test_start_check.ReadOnlyContractTests.test_reason_diagnostics_determinism_and_no_side_effects', "
            "'test_start_check.ReadOnlyContractTests.test_read_guard_rejects_scratch_escape']); "
            "sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())"
        )
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                cwd=Path(__file__).resolve().parent.parent,
                                env=dict(os.environ, TMPDIR=str(alias)),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_guard_rejects_scratch_escape(self):
        with tempfile.TemporaryDirectory(prefix='start-check-outside-') as directory:
            outside = Path(directory).resolve() / 'outside.json'
            outside.write_text('{}')
            link = self.root / 'escaped.json'
            link.symlink_to(outside)
            for path in (outside, link):
                with self.subTest(path=path), self.read_only():
                    with self.assertRaisesRegex(AssertionError, 'non-scratch eligibility read'):
                        path.read_bytes()

    def test_reason_diagnostics_determinism_and_no_side_effects(self):
        cases = [('grader', [], 'bundle-pending'),
                 ('builder', [self.start_event()], 'duplicate-dispatch'),
                 ('builder', [event('spec-shape-failed')], 'spec-shape'),
                 ('builder', self.wave_events(), 'wave-order'),
                 ('builder', [], None), ('reviewer', self.events, None),
                 ('unsupported', [], None),
                 ('grader', self.events + [dict(self.bundle, type='evidence-bundle-failed')], None)]
        # Existing free lock file proves the predicate never takes even a transient flock.
        (self.root / 'state').mkdir()
        (self.root / 'state' / spawns.lock_name(SUBJECT)).write_bytes(b'preserve')
        for role, events, reason in cases:
            original = deepcopy(events)
            with self.subTest(role=role, reason=reason), self.read_only():
                first = self.refusal(role, events=events)
                second = self.refusal(role, events=events)
                self.assertEqual(first, second)
                self.assertEqual(events, original)
                self.assertEqual(first.reason if first else None, reason)
                if first:
                    self.assertTrue(first.why.strip())
                    self.assertTrue(first.clears.strip())
        with patch.object(spawns, 'lock_held', return_value=True), self.read_only():
            result = self.refusal()
            self.assertEqual(result.reason, 'dispatch-in-progress')
            self.assertTrue(result.why and result.clears)

    def start_event(self):
        return event('spawn-started', spawn='L-builder-77')

    def test_first_call_does_not_launch_dispatch_version_probe(self):
        code = ("import sys; sys.path.insert(0, 'src'); import start_check; "
                "from datetime import datetime, timezone; from unittest.mock import patch; "
                "guard = patch('subprocess.Popen', side_effect=AssertionError('predicate child')); "
                "guard.start(); "
                "assert start_check.refusal('reviewer', 'L-spec-cold', [], "
                "now=datetime.now(timezone.utc)) is None")
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                cwd=Path(__file__).resolve().parent.parent,
                                env=dict(os.environ, DOIT_ROOT=str(self.root)),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_import_orders(self):
        for order in ('import start_check; import dispatch', 'import dispatch; import start_check'):
            result = subprocess.run([sys.executable, '-B', '-c',
                                     'import sys; sys.path.insert(0, "src"); ' + order],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    env=dict(os.environ, DOIT_ROOT=str(self.root)),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
