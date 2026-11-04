"""Box-wide, non-preemptive test sessions. DOIT_TEST_SLOTS defaults to three.

Stable slot inodes carry flock ownership; queue decisions use a short mutex.
No lock is held while sleeping. Queue logs contain scheduling metadata only.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

ROLES = ('rehearsal', 'grader', 'builder', 'sweep')
POLL = .05
AGING = 20 * 60
_role = ContextVar('test_slot_role', default='builder')
_local = threading.local()


def root_path(root=None):
    return Path(root if root is not None else os.environ.get('DOIT_TEST_SLOT_ROOT') or
                os.environ.get('DOIT_ROOT', Path.home() / '.do-it')).resolve()


def capacity(env=None):
    value = (os.environ if env is None else env).get('DOIT_TEST_SLOTS', '3')
    try:
        count = int(value)
    except (ValueError, TypeError):
        raise ValueError('DOIT_TEST_SLOTS must be an integer >= 2') from None
    if str(count) != str(value) or count < 2:
        raise ValueError('DOIT_TEST_SLOTS must be an integer >= 2')
    return count


def _directory(root):
    return root_path(root) / 'state' / 'test-slots'


@contextmanager
def _mutex(root):
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'scheduler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def _alive(pid):
    try:
        os.kill(pid, 0)
        # A zombie retains a pid, but cannot make progress or release state.
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat.rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_queue(directory):
    try:
        rows = json.loads((directory / 'queue.json').read_text())
    except FileNotFoundError:
        return []
    return [row for row in rows if _alive(row['pid'])]


def _save_queue(directory, rows):
    temporary = directory / 'queue.pending'
    temporary.write_text(json.dumps(rows))
    temporary.replace(directory / 'queue.json')


def priority(row, now):
    index = ROLES.index(row['role'])
    promotions = max(0, int((now - row['since']) // AGING))
    return max(1, index - promotions) if index else 0


def ordered(rows, now):
    return sorted(rows, key=lambda row: (priority(row, now), row['sequence']))


def _log(directory, event, row, now):
    record = {key: row[key] for key in ('pid', 'ticket', 'role', 'spec', 'since')}
    record.update(event=event, at=now)
    if event == 'acquired':
        record['wait_s'] = max(0, now - row['since'])
    with (directory / 'queue.jsonl').open('a') as stream:
        stream.write(json.dumps(record) + '\n')


def _enqueue(root, role, spec, now):
    if role not in ROLES:
        raise ValueError('invalid test-slot role')
    with _mutex(root) as directory:
        rows = _read_queue(directory)
        sequence_file = directory / 'sequence'
        sequence = int(sequence_file.read_text()) + 1 if sequence_file.exists() else 0
        sequence_file.write_text(str(sequence))
        row = dict(pid=os.getpid(), ticket=secrets.token_hex(16), role=role,
                   spec=spec, since=now, sequence=sequence)
        _save_queue(directory, rows + [row])
        _log(directory, 'queued', row, now)
        return row


def _held(directory):
    held = 0
    for path in directory.glob('slot-*.lock'):
        with path.open('r+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held += 1
    return held


def held_count(root=None):
    """Read kernel ownership, never stale pid metadata or CPU claims."""
    return _held(_directory(root))


def owns(root=None):
    return getattr(_local, 'held', {}).get((os.getpid(), str(root_path(root))), 0) > 0


class Slot:
    def __init__(self, root, handle, row):
        self.root, self.handle, self.row = root_path(root), handle, row
        self.key = os.getpid(), str(self.root)
        held = getattr(_local, 'held', None)
        if held is None:
            held = _local.held = {}
        held[self.key] = held.get(self.key, 0) + 1

    def close(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            with _mutex(self.root) as directory:
                _log(directory, 'released', self.row, time.time())
        finally:
            handle.close()
            _local.held[self.key] -= 1


def _poll(root, row, now):
    with _mutex(root) as directory:
        rows = _read_queue(directory)
        _save_queue(directory, rows)
        if not rows or ordered(rows, now)[0]['ticket'] != row['ticket']:
            return None
        count = capacity()
        # Existing holders also count if an operator reduces the configured N.
        if _held(directory) >= count:
            return None
        for index in range(count):
            handle = (directory / f'slot-{index}.lock').open('a+')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                handle.seek(0)
                handle.truncate()
                json.dump(row, handle)
                handle.flush()
                _save_queue(directory, [r for r in rows if r['ticket'] != row['ticket']])
                _log(directory, 'acquired', row, now)
                return Slot(root, handle, row)
            except BaseException:
                handle.close()
                raise
        return None


def _cancel(root, row):
    with _mutex(root) as directory:
        rows = _read_queue(directory)
        if any(r['ticket'] == row['ticket'] for r in rows):
            _save_queue(directory, [r for r in rows if r['ticket'] != row['ticket']])
            _log(directory, 'cancelled', row, time.time())


@contextmanager
def acquire(role, timeout, *, root=None, spec=None):
    """Hold a session slot, or raise TimeoutError without leaving a waiter."""
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError('test-slot timeout must be finite and nonnegative')
    capacity()
    deadline = None if timeout is None else time.monotonic() + timeout
    row = _enqueue(root, role, spec, time.time())
    selected = None
    try:
        while selected is None:
            selected = _poll(root, row, time.time())
            if selected is not None:
                break
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise TimeoutError('test-slot wait timed out')
            time.sleep(POLL if left is None else min(POLL, left))
        yield selected
    finally:
        if selected is not None:
            selected.close()
        else:
            _cancel(root, row)


def waiters(root=None, *, now=None):
    if not _directory(root).exists():
        return []
    now = time.time() if now is None else now
    with _mutex(root) as directory:
        rows = ordered(_read_queue(directory), now)
    return [dict(row, **{'class': ROLES[priority(row, now)],
                        'waiting_since': datetime.fromtimestamp(row['since'], timezone.utc).isoformat()})
            for row in rows]


@contextmanager
def for_role(role):
    token = _role.set(role)
    try:
        yield
    finally:
        _role.reset(token)


def current_role():
    return _role.get()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run'])
    parser.add_argument('--role', required=True, choices=ROLES)
    parser.add_argument('--timeout', type=float, default=None)
    # Keep scheduler options separate from the command's own flags.
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        parser.print_help()
        return 0
    if '--' not in argv:
        parser.error('expected -- followed by a command')
    boundary = argv.index('--')
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    if not command:
        parser.error('command is required')
    with acquire(args.role, args.timeout):
        return subprocess.call(command)


if __name__ == '__main__':
    sys.exit(main())
