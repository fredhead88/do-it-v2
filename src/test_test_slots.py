"""Disposable processes exercise the production scheduler and its state readers."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_slots as slots


def signed(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='fixture://test-slots/' + ac, status=200,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


def until(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('fixture timed out')


CHILD = '''import sys, pathlib
sys.path.insert(0, sys.argv[1])
import test_slots
with test_slots.acquire(sys.argv[3], 10, root=pathlib.Path(sys.argv[2]), spec=sys.argv[4]):
    print('held', flush=True)
    input()
'''


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='test-slots-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DOIT_TEST_SLOTS': '3'}).start()

    def child(self, role, spec):
        proc = subprocess.Popen([sys.executable, '-c', CHILD, str(Path(__file__).parent),
                                 str(self.root), role, spec], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.addCleanup(cleanup)
        return proc

    def test_AC1_six_sessions_priority_and_state(self):
        holders = [self.child('builder', str(i)) for i in range(3)]
        for child in holders:
            self.assertEqual(child.stdout.readline().strip(), 'held')
        waiting = []
        for role in ('rehearsal', 'builder', 'grader'):
            waiting.append(self.child(role, role))
            until(lambda: len(slots.waiters(self.root)) == len(waiting))
        self.assertEqual(slots.held_count(self.root), 3)
        reader = subprocess.check_output([sys.executable, '-c',
            'import sys,json; sys.path.insert(0,sys.argv[1]); import test_slots; '
            'print(json.dumps(test_slots.waiters(sys.argv[2])))',
            str(Path(__file__).parent), str(self.root)], text=True)
        self.assertEqual([r['role'] for r in json.loads(reader)], ['rehearsal', 'grader', 'builder'])
        trace = [dict(event='start', spec=str(i), held=3) for i in range(3)]
        for running, next_child, role in zip(holders, [waiting[0], waiting[2], waiting[1]],
                                             ['rehearsal', 'grader', 'builder']):
            running.communicate('\n', timeout=5)
            self.assertEqual(running.returncode, 0)
            self.assertEqual(next_child.stdout.readline().strip(), 'held')
            self.assertEqual(slots.held_count(self.root), 3)
            trace.extend([dict(event='end', pid=running.pid), dict(event='start', spec=role, held=3)])
        for child in waiting:
            child.communicate('\n', timeout=5)
        self.assertEqual(slots.held_count(self.root), 0)
        self.assertEqual(slots.waiters(self.root), [])
        log = [json.loads(line) for line in (self.root / 'state/test-slots/queue.jsonl').read_text().splitlines()]
        self.assertEqual(sum(r['event'] == 'acquired' for r in log), 6)
        self.assertTrue(all(r['wait_s'] >= 0 for r in log if r['event'] == 'acquired'))
        signed('AC1', dict(trace=trace, peak=3, readback=json.loads(reader), lifecycle_events=len(log)))

    def test_config_fifo_timeout_and_cleanup(self):
        self.assertEqual(slots.capacity(env={}), 3)
        self.assertEqual(slots.capacity(env={'DOIT_TEST_SLOTS': '4'}), 4)
        for value in ('0', '1', '-2', 'x', '2.5'):
            with self.assertRaises(ValueError):
                slots.capacity(env={'DOIT_TEST_SLOTS': value})
        with self.assertRaises(ValueError):
            with slots.acquire('unknown', 0, root=self.root):
                self.fail('invalid role admitted')
        with slots.acquire('builder', 1, root=self.root), slots.acquire('builder', 1, root=self.root), \
                slots.acquire('builder', 1, root=self.root):
            with self.assertRaises(TimeoutError):
                with slots.acquire('sweep', .02, root=self.root):
                    self.fail('full scheduler admitted a fourth session')
            self.assertEqual(slots.waiters(self.root), [])
        with self.assertRaises(RuntimeError):
            with slots.acquire('builder', 1, root=self.root):
                raise RuntimeError('fixture')
        self.assertEqual(slots.held_count(self.root), 0)
        rows = [dict(role='builder', since=0, sequence=i) for i in (2, 1)]
        self.assertEqual([r['sequence'] for r in slots.ordered(rows, 10)], [1, 2])

    def test_cli(self):
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_TEST_SLOT_ROOT=str(self.root))
        result = subprocess.run([sys.executable, str(Path(slots.__file__)), 'run', '--role', 'rehearsal', '--',
                                 sys.executable, '-c', 'raise SystemExit(7)'], env=env, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(slots.held_count(self.root), 0)
        log = [json.loads(line) for line in (self.root / 'state/test-slots/queue.jsonl').read_text().splitlines()]
        self.assertEqual([r['role'] for r in log], ['rehearsal'] * 3)

    def test_sweep_launchers(self):
        import fold
        import precheck
        import precheck_targeted
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        seen = []
        def launched(argv, **kwargs):
            self.assertEqual(slots.held_count(self.root), 1)
            log = [json.loads(line) for line in
                   (self.root / 'state/test-slots/queue.jsonl').read_text().splitlines()]
            self.assertEqual((log[-1]['role'], log[-1]['event']), ('sweep', 'acquired'))
            self.assertEqual(os.path.realpath(kwargs['env']['DOIT_TEST_SLOT_ROOT']),
                             os.path.realpath(self.root))
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(fold, 'ROOT', self.root), patch.object(subprocess, 'run', side_effect=launched):
            self.assertEqual(precheck.guard_runner(self.root)(self.root), [])
            self.assertEqual(precheck_targeted.runner(self.root, ['tests/test_fixture.py'],
                                                     dashboard=False, timeout=5), [])
        self.assertEqual(len(seen), 2)
        self.assertEqual(slots.held_count(self.root), 0)
        signed('AC1-sweeps', dict(launches=2, role='sweep', held_during_launch=1, held_after=0))

    def test_board_waiter_rows(self):
        import autodispatch
        import fold
        import loadgate
        from datetime import datetime, timezone
        row = slots._enqueue(self.root, 'builder', 'L-spec-9000', time.time())
        try:
            with patch.object(fold, 'ROOT', self.root), patch.object(loadgate, 'held_items', return_value=frozenset()):
                rows = autodispatch.candidates([], {}, datetime.now(timezone.utc))
            self.assertEqual(len(rows), 1)
            self.assertEqual({key: rows[0][key] for key in ('spec', 'role', 'class', 'status', 'reason')},
                             dict(spec='L-spec-9000', role='builder', **{'class': 'builder'},
                                  status='slot-wait', reason='test-slot'))
            self.assertTrue(rows[0]['since'])
            signed('AC1-board', rows)
        finally:
            slots._cancel(self.root, row)


def test_AC2_dead_holder():
    with tempfile.TemporaryDirectory(prefix='test-slots-death-') as directory:
        root = Path(directory)
        proc = subprocess.Popen([sys.executable, '-c', CHILD, str(Path(__file__).parent),
                                 str(root), 'builder', 'dead-holder'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == 'held'
            assert slots.held_count(root) == 1
            proc.kill()
            proc.communicate(timeout=5)
            # The very next scan observes the kernel-released lock.
            assert slots.held_count(root) == 0
            with slots.acquire('rehearsal', slots.POLL, root=root):
                assert slots.held_count(root) == 1
            signed('AC2', dict(killed_pid=proc.pid, returncode=proc.returncode, polls=1, reclaimed=True))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)


def test_AC3_aging():
    def row(role, since, sequence):
        return dict(role=role, since=since, sequence=sequence)
    old = row('builder', 0, 0)
    grader = row('grader', 1199, 1)
    assert slots.ordered([old, grader], 1199)[0] == grader
    assert slots.ordered([old, grader], 1200)[0] == old
    assert slots.priority(row('sweep', 0, 0), 1200) == 2
    assert slots.priority(row('sweep', 0, 0), 2400) == 1
    rehearsal = row('rehearsal', 1200, 2)
    assert slots.ordered([old, grader, rehearsal], 1200)[0] == rehearsal
    # Fake-clock N=3: a queued rehearsal reserves the FIRST released slot.
    clock = [100.0]
    remaining = [17, 40, 90]
    trace = []
    with tempfile.TemporaryDirectory(prefix='test-slots-aging-') as directory:
        root = Path(directory)
        with patch.dict(os.environ, {'DOIT_TEST_SLOTS': '3'}), contextlib.ExitStack() as stack:
            holders = [stack.enter_context(slots.acquire('builder', 1, root=root)) for _ in range(3)]
            low = slots._enqueue(root, 'grader', 'queued-grader', clock[0])
            top = slots._enqueue(root, 'rehearsal', 'queued-rehearsal', clock[0])
            try:
                assert slots._poll(root, low, clock[0]) is None
                assert slots._poll(root, top, clock[0]) is None
                clock[0] += min(remaining)
                holders[0].close()
                assert slots._poll(root, low, clock[0]) is None
                acquired = slots._poll(root, top, clock[0])
                assert acquired is not None
                acquired.close()
                trace.append(dict(wait=clock[0] - 100, shortest_remaining=min(remaining), holders_alive=2))
            finally:
                slots._cancel(root, low)
                slots._cancel(root, top)
    signed('AC3', dict(before=slots.priority(old, 1199), at_threshold=slots.priority(old, 1200),
                       rehearsal_bound=trace, no_holder_killed=True))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SchedulerTests)
    for test in (test_AC2_dead_holder, test_AC3_aging):
        suite.addTest(unittest.FunctionTestCase(test))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
