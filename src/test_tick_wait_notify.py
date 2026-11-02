#!/usr/bin/env python3
"""Standalone notification/fallback regressions: python3 src/test_tick_wait_notify.py."""
import contextlib
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_TEMP = tempfile.TemporaryDirectory(prefix='tick-wait-notify-')
TMP = Path(_TEMP.name)
os.environ.update(DOIT_ROOT=str(TMP), DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=str(TMP / 'v4-ledger'),
                  V4_INBOX_DIR=str(TMP / 'v4-inbox'),
                  V4_STAGING_DIR=str(TMP / 'v4-staging'))
os.environ.pop('DOIT_PROJECT', None)
os.environ.pop('DOIT_LEDGER_FILE', None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold
import tick
import realm_ledger
import relay
import install_sync

EVENT_LINE = json.dumps(dict(v=1, type='message-sent', subject='L-spec-9999',
                            actor='operator',
                            ts=datetime.datetime.now(datetime.timezone.utc).isoformat())) + '\n'


def evidence(ac, **body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://tick/wait-notify-' + ac, status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)), flush=True)


class WaitNotifyTests(unittest.TestCase):
    def setUp(self):
        # Match test_tick.py's integration boundaries: the real closing tick
        # still folds and appends, but must not query GitHub, resume operator
        # panes or install agent links while measuring a fixture-only wait.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        intake_gh = stack.enter_context(patch.object(tick.intake, 'ghlimit'))
        intake_gh.gate.return_value = dict(ok=True, remaining=5000, reset=0, waited_s=0)
        intake_gh.run.return_value = (0, '[]', '')
        stack.enter_context(patch.object(tick.pane_resume, 'run',
                            return_value=dict(resumed=[], escalated=[], skipped=[])))
        stack.enter_context(patch.object(install_sync, 'run', return_value=[]))
        self.root = TMP / self._testMethodName
        (self.root / 'events').mkdir(parents=True)
        cron = self.root / 'state/cron-runs/look.jsonl'
        cron.parent.mkdir(parents=True)
        cron.write_text(json.dumps(dict(ev='start', pid=1,
                        ts=datetime.datetime.now(datetime.timezone.utc).isoformat())) + '\n')
        self.tag = 'wait-notify-' + self._testMethodName.split('_')[1] + ':'

    def notification_required(self):
        probe = realm_ledger.Changes(self.root / 'events')
        try:
            self.assertIsNotNone(probe.fd, self.tag + ' inotify unavailable; check inotify instance limit')
        finally:
            probe.close()

    @contextlib.contextmanager
    def writer(self, action, delay=0.1):
        errors, done = [], []
        baseline_ready = threading.Event()
        snapshot = tick._events_state

        def baseline(name):
            value = snapshot(name)
            baseline_ready.set()
            return value

        def run():
            try:
                if not baseline_ready.wait(10):
                    raise AssertionError('wait never established its baseline')
                time.sleep(delay)
                action()
                done.append(time.monotonic())
            except BaseException as exc:
                errors.append(exc)

        # The specified mid-wait write must follow the baseline even if this
        # process is descheduled while installing its counters on a busy host.
        with patch.object(tick, '_events_state', side_effect=baseline):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                yield done
            finally:
                thread.join()
                self.assertFalse(errors, self.tag + ' writer errors: ' + repr(errors))

    def measured(self, maximum, *, record=True):
        with contextlib.ExitStack() as stack:
            snapshot = stack.enter_context(patch.object(tick, '_events_state', wraps=tick._events_state))
            heartbeat = stack.enter_context(patch.object(relay, 'ledger_changed', wraps=relay.ledger_changed))
            closing = stack.enter_context(patch.object(tick, '_record')) if record else None
            wall, cpu = time.monotonic(), time.process_time()
            reason = tick.wait(self.root, max_s=maximum)
            result = dict(reason=reason, wall=time.monotonic() - wall,
                          cpu=time.process_time() - cpu, snapshots=snapshot.call_count,
                          heartbeat=heartbeat.call_count)
            if closing is not None:
                self.assertEqual(closing.call_count, 1, self.tag + ' exactly one closing tick')
            self.assertEqual(result['heartbeat'], 0, self.tag + ' no relay scans')
            return result

    def test_AC1_idle_cost(self):
        self.notification_required()
        for n in range(5000):
            (self.root / 'events' / f'L-builder-{n:04d}.jsonl').touch()
        tick._bind_root(self.root)
        before = time.process_time()
        tick._events_state(tick.TICK_NAME)
        scan = time.process_time() - before
        result = self.measured(1.0)
        self.assertEqual(result['reason'], 'interval', self.tag)
        self.assertGreaterEqual(result['wall'], 0.95, self.tag)
        self.assertEqual(result['snapshots'], 1, self.tag + ' baseline only')
        self.assertLess(result['cpu'], 3 * scan + 0.05, self.tag)
        evidence('AC1', scan_cpu=scan, **result)

    def test_AC2_create_and_append(self):
        self.notification_required()
        results = []
        for existing in (False, True):
            path = self.root / 'events' / ('existing.jsonl' if existing else 'new.jsonl')
            if existing:
                path.write_text(EVENT_LINE)
            def append():
                with path.open('a') as stream:
                    stream.write(EVENT_LINE)
            with self.writer(append) as written:
                result = self.measured(5)
                returned = time.monotonic()
            self.assertEqual(result['reason'], 'changed', self.tag)
            self.assertLess(returned - written[0], 0.5, self.tag + ' detection')
            self.assertEqual(result['snapshots'], 2, self.tag)
            results.append(result)
        path = self.root / 'events/real.jsonl'
        with self.writer(lambda: path.write_text(EVENT_LINE)):
            result = self.measured(5, record=False)
        self.assertEqual(result['reason'], 'changed', self.tag)
        self.assertLess(result['wall'], 2, self.tag + ' real closing tick smoke')
        ticks = [json.loads(line) for line in tick.tick_path().read_text().splitlines()]
        self.assertEqual(sum(e['type'] == 'tick' for e in ticks), 1, self.tag)
        evidence('AC2', detection=results, real_tick=result)

    def test_AC3_tick_and_nonledger_exclusion(self):
        self.notification_required()
        def noise():
            for n in range(12):
                (self.root / 'events' / tick.TICK_NAME).write_text(str(n))
                (self.root / 'events/noise.txt').write_text(str(n))
                time.sleep(0.025)
        with self.writer(noise):
            result = self.measured(0.6)
        self.assertEqual(result['reason'], 'interval', self.tag)
        self.assertGreaterEqual(result['wall'], 0.55, self.tag)
        self.assertEqual(result['snapshots'], 1, self.tag)
        evidence('AC3', **result)

    def test_AC4_equal_confirmation(self):
        self.notification_required()
        path = self.root / 'events/existing.jsonl'
        path.write_text('{}\n')
        def unchanged():
            path.chmod(0o600)
            with path.open('a'):
                pass
        with self.writer(unchanged):
            result = self.measured(0.6)
        self.assertEqual(result['reason'], 'interval', self.tag)
        self.assertGreaterEqual(result['wall'], 0.55, self.tag)
        self.assertGreaterEqual(result['snapshots'], 2, self.tag)
        evidence('AC4', **result)

    def fallback_cases(self, factory):
        results = []
        with patch.object(realm_ledger, 'Changes', factory):
            with self.writer(lambda: (self.root / 'events/fallback.jsonl').write_text('{}\n')) as written:
                result = self.measured(5)
                returned = time.monotonic()
            self.assertEqual(result['reason'], 'changed', self.tag)
            self.assertLess(returned - written[0], 0.5, self.tag)
            results.append(result)
            with self.writer(lambda: (self.root / 'events' / tick.TICK_NAME).write_text('{}\n')):
                result = self.measured(0.6)
            self.assertEqual(result['reason'], 'interval', self.tag)
            self.assertGreaterEqual(result['wall'], 0.55, self.tag)
            self.assertLessEqual(result['snapshots'], 5, self.tag + ' quarter-second fallback')
            results.append(result)
        self.assertEqual(tick.POLL_STEP, 0.25, self.tag)
        return results

    def test_AC5_unavailable_fallback(self):
        class Unavailable:
            fd = None
            def __init__(self, directory):
                pass
            def close(self):
                pass
        with patch.object(realm_ledger, 'Changes', Unavailable):
            idle = self.measured(1.0)
        self.assertEqual(idle['reason'], 'interval', self.tag + ' (a) idle fallback')
        self.assertGreaterEqual(idle['wall'], 0.95, self.tag + ' (a) full interval')
        self.assertLessEqual(idle['snapshots'], 8, self.tag + ' (a) bounded snapshot polling')
        evidence('AC5', idle=idle, results=self.fallback_cases(Unavailable))

    def test_AC6_lost_watcher_fallback(self):
        instances = []
        outer = self
        class Lost:
            def __init__(self, directory):
                self.fd, self.writer = os.pipe()
                os.write(self.writer, b'x')
                self.closes = 0
                instances.append(self)
            def close(self):
                self.closes += 1
                if self.fd is not None:
                    os.close(self.fd)
                    os.close(self.writer)
                    self.fd = None
            def poll(self):
                self.close()  # Changes itself closes on uncertainty.
                return None
        original = tick._events_state
        def checked_snapshot(name):
            if instances:
                outer.assertIsNone(instances[-1].fd, outer.tag + ' fallback holds no watcher')
                outer.assertGreaterEqual(instances[-1].closes, 2, outer.tag + ' immediately closed again')
            return original(name)
        # The initial baseline legitimately holds the watcher; check after poll.
        def snapshots(name):
            return checked_snapshot(name) if instances and instances[-1].closes else original(name)
        with patch.object(tick, '_events_state', side_effect=snapshots):
            results = self.fallback_cases(Lost)
        self.assertTrue(all(i.fd is None for i in instances), self.tag)
        evidence('AC6', results=results, closes=[i.closes for i in instances])

    def test_AC7_cleanup(self):
        self.notification_required()
        original, instances = realm_ledger.Changes, []
        class Tracked(original):
            def __init__(self, directory):
                super().__init__(directory)
                instances.append(self)
        fd_count = len(list(Path('/proc/self/fd').iterdir()))
        with patch.object(realm_ledger, 'Changes', Tracked):
            for _ in range(100):
                self.measured(0)
            with self.writer(lambda: (self.root / 'events/change.jsonl').write_text('{}\n')):
                self.assertEqual(self.measured(5)['reason'], 'changed', self.tag)
            with patch.object(tick, '_events_state', side_effect=RuntimeError('snapshot failure')):
                with self.assertRaisesRegex(RuntimeError, 'snapshot failure', msg=self.tag):
                    tick.wait(self.root, max_s=0.1)
            with patch.object(tick, '_record', side_effect=RuntimeError('closing failure')):
                with self.assertRaisesRegex(RuntimeError, 'closing failure', msg=self.tag):
                    tick.wait(self.root, max_s=0)
            with patch.object(Tracked, 'poll', side_effect=RuntimeError('poll failure')):
                with self.writer(lambda: (self.root / 'events/poll.jsonl').write_text('{}\n')):
                    with self.assertRaisesRegex(RuntimeError, 'poll failure', msg=self.tag):
                        tick.wait(self.root, max_s=1)
        self.assertTrue(all(i.fd is None for i in instances), self.tag)
        self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), fd_count, self.tag)
        evidence('AC7', closed=len(instances), descriptors_before=fd_count)

    def test_AC8_lazy_import(self):
        code = '''import sys
import tick
assert 'realm_ledger' not in sys.modules, 'wait-notify-AC8: lazy watcher import'
assert 'relay' not in sys.modules, 'wait-notify-AC8: no eager relay import'
tick._record = lambda: None
tick.wait(max_s=0)
assert 'realm_ledger' in sys.modules, 'wait-notify-AC8: watcher imported at call time'
assert 'relay' not in sys.modules, 'wait-notify-AC8: no relay import in wait'
'''
        result = subprocess.run([sys.executable, '-c', code],
                                cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, self.tag + result.stdout + result.stderr)
        evidence('AC8', import_check='passed')

    def test_AC10_watcher_before_baseline(self):
        self.notification_required()
        original, instances = realm_ledger.Changes, []
        original_snapshot = tick._events_state
        calls = []
        path = self.root / 'events/before.jsonl'
        class Between(original):
            def __init__(self, directory):
                super().__init__(directory)
                instances.append(self)
                calls.append('Changes')

        def before_snapshot(name):
            calls.append('_events_state')
            if calls.count('_events_state') == 1:
                path.write_text('{}\n')
            return original_snapshot(name)

        with patch.object(realm_ledger, 'Changes', Between), \
                patch.object(tick, '_events_state', side_effect=before_snapshot):
            result = self.measured(0.6)
        self.assertEqual(result['reason'], 'interval', self.tag + ' pre-baseline write is baseline')
        self.assertGreaterEqual(result['wall'], 0.55, self.tag)
        self.assertEqual(calls, ['Changes', '_events_state', '_events_state'],
                         self.tag + ' (a) watcher before baseline and one confirmation')
        self.assertEqual(result['snapshots'], 2, self.tag + ' (a) exactly two snapshots')
        before_calls = calls[:]
        calls.clear()

        def after_snapshot(name):
            calls.append('_events_state')
            return original_snapshot(name)

        with patch.object(realm_ledger, 'Changes', Between), \
                patch.object(tick, '_events_state', side_effect=after_snapshot):
            with self.writer(lambda: (self.root / 'events/after.jsonl').write_text('{}\n')) as written:
                result_after = self.measured(5)
                returned = time.monotonic()
        self.assertEqual(result_after['reason'], 'changed', self.tag + ' post-baseline write is observed')
        detection = returned - written[0]
        self.assertLess(detection, 0.5, self.tag + ' (b) threaded write detection')
        self.assertEqual(calls, ['Changes', '_events_state', '_events_state'],
                         self.tag + ' (b) watcher before baseline and one confirmation')
        self.assertEqual(result_after['snapshots'], 2, self.tag + ' (b) exactly two snapshots')
        self.assertTrue(all(i.fd is None for i in instances), self.tag)
        evidence('AC10', before=result, before_calls=before_calls,
                 after=result_after, after_calls=calls, detection=detection)

    def test_AC11_readable_empty_and_interrupted(self):
        polls, pipe_ends = [], []
        class Empty:
            def __init__(self, directory):
                self.fd, self.writer = os.pipe()
                pipe_ends.extend((self.fd, self.writer))
                os.write(self.writer, b'x')
            def poll(self):
                polls.append(time.monotonic())
                return set()
            def close(self):
                if self.fd is not None:
                    os.close(self.fd)
                    os.close(self.writer)
                    self.fd = None
        with patch.object(realm_ledger, 'Changes', Empty):
            result = self.measured(0.6)
        self.assertEqual(result['reason'], 'interval', self.tag)
        self.assertGreaterEqual(result['wall'], 0.55, self.tag)
        self.assertLess(result['cpu'], 0.2, self.tag + ' bounded process CPU')
        self.assertLessEqual(len(polls), 40, self.tag + ' bounded empty wakes')
        self.assertEqual(result['snapshots'], 1, self.tag)
        self.assertEqual(tick.NOTIFY_MIN_GAP, 0.02, self.tag)
        self.assertEqual(len(pipe_ends), 2, self.tag + ' one pipe created')
        for fd in pipe_ends:
            with self.assertRaises(OSError, msg=self.tag + ' pipe end closed') as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF, self.tag + ' pipe end invalid')
        self.notification_required()
        import select
        original = select.poll
        calls = []
        class Interrupted:
            def __init__(self):
                self.inner = original()
            def register(self, *args):
                self.inner.register(*args)
            def poll(self, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    raise InterruptedError()
                if len(calls) == 2:
                    return []
                return self.inner.poll(timeout)
        with patch.object(select, 'poll', Interrupted):
            retry = self.measured(0.2)
        self.assertEqual(retry['reason'], 'interval', self.tag)
        self.assertGreaterEqual(retry['wall'], 0.19, self.tag)
        self.assertGreaterEqual(len(calls), 3, self.tag)
        self.assertTrue(all(t >= 0 for t in calls), self.tag)
        evidence('AC11', wakes=len(polls), idle=result, closed_pipe_ends=len(pipe_ends),
                 interrupted=retry)

    def test_AC12_rename_and_delete(self):
        self.notification_required()
        results = []
        for operation in ('rename-in', 'rename-out', 'delete'):
            source = self.root / 'events' / (operation + ('.txt' if operation == 'rename-in' else '.jsonl'))
            source.write_text('{}\n')
            target = source.with_suffix('.jsonl' if operation == 'rename-in' else '.txt')
            def change():
                if operation == 'delete':
                    source.unlink()
                else:
                    source.rename(target)
            with self.writer(change) as written:
                result = self.measured(5)
                returned = time.monotonic()
            self.assertEqual(result['reason'], 'changed', self.tag + operation)
            self.assertLess(returned - written[0], 0.5, self.tag + operation)
            self.assertEqual(result['snapshots'], 2, self.tag + operation)
            results.append(dict(operation=operation, **result))
        evidence('AC12', results=results)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        _TEMP.cleanup()
