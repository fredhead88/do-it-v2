"""Real flock regression for starvation, writer progress, and gate cleanup."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ledger_cache


# Base reference: the pre-turnstile _locked, used only to demonstrate starvation.
_BASE_HELD = set()


@contextmanager
def base_locked(lock, mode):
    claim = (str(lock), os.getpid(), threading.get_ident())
    if claim in _BASE_HELD:
        raise ledger_cache.ReentrantRead(f'ledger cache: reentrant read ({lock})')
    try:
        fh = lock.open('rb')
    except FileNotFoundError:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fh = os.fdopen(fd, 'rb')
    with fh:
        wait = ledger_cache.lock_wait_s()
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ledger_cache.LockTimeout(f'ledger cache: lock not acquired within {wait:g}s ({lock})')
                time.sleep(min(.05, remaining))
        _BASE_HELD.add(claim)
        try:
            yield
        finally:
            _BASE_HELD.discard(claim)
            fcntl.flock(fh, fcntl.LOCK_UN)


class FairnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ledger-lock-fairness-')
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / 'ledger-test.lock'

    def overlapping_readers(self, locked, *, starves):
        stop = threading.Event()
        state = threading.Condition()
        started = [threading.Event() for _ in range(3)]
        counts = [0, 0, 0]
        active = 0
        exclusive = False
        failures = []
        timings = []
        gaps = []
        monitor = False
        writers = []

        def reader(index):
            nonlocal active
            try:
                while not stop.is_set():
                    with locked(self.lock, fcntl.LOCK_SH):
                        with state:
                            self.assertFalse(exclusive, 'reader overlapped writer')
                            active += 1
                            counts[index] += 1
                            started[index].set()
                            state.notify_all()
                        try:
                            stop.wait(.2)
                        finally:
                            with state:
                                active -= 1
                                if monitor and active == 0:
                                    gaps.append(time.monotonic())
                                state.notify_all()
            except BaseException as exc:
                with state:
                    failures.append(exc)
                    state.notify_all()

        def writer():
            nonlocal exclusive
            begin = time.monotonic()
            try:
                with locked(self.lock, fcntl.LOCK_EX):
                    elapsed = time.monotonic() - begin
                    with state:
                        self.assertEqual(active, 0, 'writer overlapped reader')
                        self.assertFalse(exclusive)
                        exclusive = True
                    try:
                        time.sleep(.1)
                        with state:
                            self.assertEqual(active, 0)
                    finally:
                        with state:
                            exclusive = False
                    timings.append(elapsed)
            except BaseException as exc:
                with state:
                    failures.append(exc)
                    state.notify_all()

        readers = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(3)]
        with patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='5'):
            try:
                for i, thread in enumerate(readers):
                    thread.start()
                    self.assertTrue(started[i].wait(2), 'reader did not start')
                    if i < 2:
                        time.sleep(.06)
                with state:
                    self.assertEqual(active, 3)
                    monitor = starves
                first = threading.Thread(target=writer, daemon=True)
                writers.append(first)
                first.start()
                first.join(6 if starves else 2)
                self.assertFalse(first.is_alive(), 'writer missed progress deadline')
                if starves:
                    self.assertEqual(timings, [], 'base unexpectedly acquired')
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], ledger_cache.LockTimeout)
                    self.assertEqual(gaps, [], 'base fixture did not maintain overlapping readers')
                    print('base reference: timed out at 5s with continuously overlapping readers', flush=True)
                else:
                    self.assertEqual(failures, [])
                    self.assertEqual(len(timings), 1)
                    # Prove every reader resumes before requesting the second writer.
                    with state:
                        before = counts.copy()
                        self.assertTrue(state.wait_for(
                            lambda: all(a > b for a, b in zip(counts, before)) or failures,
                            timeout=2), 'readers failed to resume')
                    second = threading.Thread(target=writer, daemon=True)
                    writers.append(second)
                    second.start()
                    second.join(2)
                    self.assertFalse(second.is_alive(), 'second writer missed progress deadline')
                    self.assertEqual(failures, [])
                    self.assertEqual(len(timings), 2)
                    for elapsed in timings:
                        self.assertLess(elapsed, 2)
                    print('writers: ' + ', '.join(f'{t:.3f}s' for t in timings)
                          + '; readers resumed; no shared/exclusive overlap', flush=True)
            finally:
                with state:
                    monitor = False
                stop.set()
                for thread in readers + writers:
                    if thread.ident is not None:
                        thread.join(6)
                self.assertFalse(any(t.is_alive() for t in readers + writers))

    def test_ac1_writer_progress_and_base_starvation(self):
        self.overlapping_readers(base_locked, starves=True)
        self.overlapping_readers(ledger_cache._locked, starves=False)
        print('L-spec-1761 AC1 ok', flush=True)

    def test_ac2_timeout_releases_gate(self):
        # One helper process holds the main SH lock until explicitly released.
        child = subprocess.Popen([sys.executable, '-c',
            "import fcntl,os,sys; "
            "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); "
            "fcntl.flock(fd,fcntl.LOCK_SH); print('held',flush=True); sys.stdin.readline()",
            str(self.lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        duplicates = []
        real_flock = fcntl.flock
        gate = self.lock.with_suffix('.gate')

        def observe(handle, mode):
            result = real_flock(handle, mode)
            fd = handle if isinstance(handle, int) else handle.fileno()
            if mode == fcntl.LOCK_EX | fcntl.LOCK_NB and gate.exists():
                if os.fstat(fd).st_ino == gate.stat().st_ino:
                    # Keep the open file description alive so close() cannot mask
                    # a missing explicit release on the main-lock timeout path.
                    duplicates.append(os.dup(fd))
            return result

        try:
            self.assertEqual(child.stdout.readline(), 'held\n')
            with patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='0.2'), \
                    patch.object(ledger_cache.fcntl, 'flock', side_effect=observe):
                begin = time.monotonic()
                with self.assertRaises(ledger_cache.LockTimeout) as caught:
                    with ledger_cache._locked(self.lock, fcntl.LOCK_EX):
                        self.fail('writer acquired while subprocess holds SH')
            self.assertGreaterEqual(time.monotonic() - begin, .2)
            self.assertLess(time.monotonic() - begin, 2)
            self.assertEqual(str(caught.exception),
                f'ledger cache: lock not acquired within 0.2s ({self.lock})')
            self.assertTrue(duplicates, 'writer never took the gate')
            with gate.open('rb') as fresh:
                real_flock(fresh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                real_flock(fresh, fcntl.LOCK_UN)
            print('timeout: existing message; gate explicitly released before helper exits', flush=True)
            print('L-spec-1761 AC2 ok', flush=True)
        finally:
            for fd in duplicates:
                os.close(fd)
            child.communicate('\n', timeout=3)
            self.assertEqual(child.returncode, 0)

    def test_one_deadline_and_zero_wait(self):
        gate = self.lock.with_suffix('.gate')
        self.lock.touch(mode=0o600)
        gate.touch(mode=0o600)
        clock = [0.0]
        sleeps = []
        with self.lock.open('rb') as main_holder, gate.open('rb') as gate_holder:
            fcntl.flock(main_holder, fcntl.LOCK_SH)
            fcntl.flock(gate_holder, fcntl.LOCK_EX)

            def advance(delay):
                sleeps.append(delay)
                clock[0] += delay
                # First spend 50ms waiting at the gate, then the remaining
                # 30ms waiting for the main lock: no second timeout budget.
                fcntl.flock(gate_holder, fcntl.LOCK_UN)

            with patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='0.08'), \
                    patch.object(ledger_cache, '_monotonic', side_effect=lambda: clock[0]), \
                    patch.object(ledger_cache.time, 'sleep', side_effect=advance):
                with self.assertRaises(ledger_cache.LockTimeout):
                    with ledger_cache._locked(self.lock, fcntl.LOCK_EX):
                        self.fail('main lock was held shared')
            self.assertEqual(len(sleeps), 2)
            self.assertAlmostEqual(sum(sleeps), .08)

            with patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='0'), \
                    patch.object(ledger_cache.time, 'sleep', side_effect=AssertionError('slept')):
                # A free gate is attempted even at zero wait; main then fails.
                with self.assertRaises(ledger_cache.LockTimeout):
                    with ledger_cache._locked(self.lock, fcntl.LOCK_EX):
                        self.fail('main lock was held shared')
                # A blocked gate fails immediately for either request mode.
                fcntl.flock(gate_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
                    with self.assertRaises(ledger_cache.LockTimeout):
                        with ledger_cache._locked(self.lock, mode):
                            self.fail('gate was held exclusively')
                fcntl.flock(gate_holder, fcntl.LOCK_UN)

        with patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='0'):
            for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
                with ledger_cache._locked(self.lock, mode), gate.open('rb') as fresh:
                    # No gate held while the caller performs cache work.
                    fcntl.flock(fresh, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
