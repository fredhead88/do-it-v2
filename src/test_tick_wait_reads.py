#!/usr/bin/env python3
"""Standalone read-guard regressions for tick.wait: python3 src/test_tick_wait_reads.py [AC1Tests ...].

Proves two things about the real `tick.wait` against scratch ledgers, with no
production change: while waiting it performs zero ledger content reads, and it
wakes on a real size or mtime_ns change.

Measured phase: entry to the real `tick.wait` -> entry to its closing `_record`
sentinel, waiting thread only. Guarded path set: the scratch `fold.EVENTS`
directory and its descendants (any extension). Forbidden in the phase: a
read-capable `builtins.open` / `io.open` (incl. `Path.open`) / `os.open` on that
set (relative paths and `dir_fd` resolved to the real destination), any
`fold.read_events` or `ledger_files.read_text` call, and any `os.read` that is
not on the native inotify descriptor. Directory enumeration, stat and close are
permitted. Anything the guard cannot resolve counts as forbidden.

`L-tick-local.jsonl` is excluded from change detection because every returning
tick appends to it; counting it would make each return wake the next waiter.
That exclusion is from change detection only, not from the read guard.
"""
import builtins
import collections
import ctypes  # noqa: F401 - imported before measurement (watcher dependency)
import io
import json
import math  # noqa: F401 - imported before measurement (wait imports it lazily)
import os
from pathlib import Path
import select  # noqa: F401 - imported before measurement
import struct  # noqa: F401 - imported before measurement (watcher dependency)
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_TEMP = tempfile.TemporaryDirectory(prefix='tick-wait-reads-')
TMP = Path(_TEMP.name)
os.environ.update(DOIT_ROOT=str(TMP), DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=str(TMP / 'v4-ledger'),
                  V4_INBOX_DIR=str(TMP / 'v4-inbox'),
                  V4_STAGING_DIR=str(TMP / 'v4-staging'))
os.environ.pop('DOIT_PROJECT', None)
os.environ.pop('DOIT_LEDGER_FILE', None)
SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import fold
import ledger_files
import realm_ledger
import tick

LINE = '{"v":1,"type":"message-sent","subject":"L-spec-9999"}\n'
REAL_OPEN, REAL_IO_OPEN, REAL_OS_OPEN, REAL_OS_READ = builtins.open, io.open, os.open, os.read
REAL_READ_EVENTS, REAL_READ_TEXT = fold.read_events, ledger_files.read_text
CATEGORIES = ('builtins.open', 'io.open', 'os.open', 'os.read', 'fold.read_events',
              'ledger_files.read_text', 'uncertain')


class Guard:
    """Counts forbidden ledger accesses made by the waiting thread in the measured phase."""
    def __init__(self, events):
        self.events = os.path.realpath(events)
        self.thread = None
        self.active = False
        self.counts = collections.Counter()
        self.hits = []
        self.permitted = collections.Counter()   # ledger accesses outside the phase (closing)
        self.inotify = set()

    def start(self):
        self.thread, self.active = threading.get_ident(), True

    def stop(self):
        self.active = False

    def mine(self):
        return self.active and threading.get_ident() == self.thread

    def inside(self, path):
        return path == self.events or path.startswith(self.events + os.sep)

    def resolve(self, path, dir_fd=None):
        """Real destination of `path`, or None when it cannot be established."""
        try:
            if isinstance(path, int):
                return os.path.realpath(os.readlink('/proc/self/fd/%d' % path))
            text = os.fsdecode(os.fspath(path))
            if not os.path.isabs(text):
                base = (os.readlink('/proc/self/fd/%d' % dir_fd)
                        if dir_fd is not None else os.getcwd())
                text = os.path.join(base, text)
            return os.path.realpath(text)
        except (OSError, TypeError, ValueError):
            return None

    def note(self, category, path, read_capable, dir_fd=None):
        if not read_capable:
            return
        in_phase = self.mine()
        if not in_phase and threading.get_ident() != self.thread:
            return
        target = self.resolve(path, dir_fd)
        if target is None:
            if in_phase:
                self.counts['uncertain'] += 1
                self.hits.append(('uncertain', repr(path)))
            return
        if self.inside(target):
            if in_phase:
                self.counts[category] += 1
                self.hits.append((category, target))
            else:
                self.permitted[category] += 1

    # wrappers -------------------------------------------------------------
    def open_wrapper(self, real, category):
        def wrapper(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            self.note(category, file, any(c in mode for c in 'r+'))
            return real(file, *args, **kwargs)
        return wrapper

    def os_open(self, path, flags, *args, **kwargs):
        capable = (flags & os.O_ACCMODE) != os.O_WRONLY and not flags & os.O_DIRECTORY
        self.note('os.open', path, capable, kwargs.get('dir_fd'))
        return REAL_OS_OPEN(path, flags, *args, **kwargs)

    def os_read(self, fd, length):
        if self.mine() and fd not in self.inotify:
            self.counts['os.read'] += 1
            self.hits.append(('os.read', self.resolve(fd) or repr(fd)))
        return REAL_OS_READ(fd, length)

    def read_events(self, *args, **kwargs):
        self.forbid('fold.read_events')
        return REAL_READ_EVENTS(*args, **kwargs)

    def read_text(self, *args, **kwargs):
        self.forbid('ledger_files.read_text')
        return REAL_READ_TEXT(*args, **kwargs)

    def forbid(self, category):
        if self.mine():
            self.counts[category] += 1
            self.hits.append((category, 'any arguments'))

    def total(self):
        return sum(self.counts.values())


class Unavailable:
    """The unavailable-watcher row: no descriptor, so wait polls snapshots."""
    fd = None
    def __init__(self, directory):
        pass
    def close(self):
        pass
    def poll(self):
        return None


class Snapshots:
    """Observation wrapper: delegates to the real `_events_state`, never replaces its answer."""
    def __init__(self, real, inject=None):
        self.real, self.inject = real, inject
        self.calls = []                       # (monotonic end, result)
        self.baseline_ready = threading.Event()

    def __call__(self, name):
        first = not self.calls
        if first and self.inject is not None:
            self.inject()
        value = self.real(name)
        self.calls.append((time.monotonic(), value))
        if first:
            self.baseline_ready.set()
        return value


def make_ledger(name):
    root = TMP / name
    events = root / 'events'
    events.mkdir(parents=True)
    (root / 'content').mkdir()
    (events / 'a.jsonl').write_text(LINE)
    (events / 'b.jsonl').write_text(LINE * 2)
    (events / tick.TICK_NAME).write_text(LINE)
    return root, events


def run_row(name, mode, *, max_s, target=None, mutate=None, inject=None, closing_read=None,
            before=None, after=None):
    """One measured `tick.wait` row. Returns the evidence dict; asserts nothing about cleanliness."""
    assert mode in ('native', 'fallback'), mode
    root, events = make_ledger(name)
    guard = Guard(events)
    ctx = before(events) if before is not None else None      # fixture work, outside the phase
    snaps = Snapshots(tick._events_state, (lambda: inject(ctx)) if inject is not None else None)
    stamps, errors, closing, instances, polls = {}, [], [], [], []
    stat = {}
    abort = threading.Event()

    class Recording(realm_ledger.Changes):
        def __init__(self, directory):
            super().__init__(directory)
            instances.append(self.fd)
            if self.fd is not None:
                guard.inotify.add(self.fd)
        def poll(self):
            names = super().poll()
            polls.append((time.monotonic(), None if names is None else sorted(names)))
            return names

    def sentinel():
        guard.stop()                         # the phase ends on entry to the closing call
        stamps['closing'] = time.monotonic()
        closing.append(1)
        if closing_read is not None:
            stamps['closing_read'] = closing_read(events)

    def stat_of(path):
        st = os.stat(path)
        return dict(size=st.st_size, mtime_ns=st.st_mtime_ns, mode=st.st_mode & 0o777)

    def writer():
        try:
            if not snaps.baseline_ready.wait(10):
                raise AssertionError('wait never completed its first snapshot')
            if abort.is_set():
                return
            time.sleep(0.05)                 # only after the baseline has finished computing
            stamps['baseline'] = snaps.calls[0][0]
            stat['pre'] = stat_of(events / target)
            stamps['mutation'] = time.monotonic()   # before the mutate call: no wake can precede it
            mutate(events / target)
            stat['post'] = stat_of(events / target)
            stamps['mutation_done'] = time.monotonic()
        except BaseException as exc:         # surfaced below
            errors.append(exc)

    saved = fold.ROOT, fold.EVENTS
    thread = threading.Thread(target=writer) if mutate is not None else None
    try:
        with patch.object(builtins, 'open', guard.open_wrapper(REAL_OPEN, 'builtins.open')), \
                patch.object(io, 'open', guard.open_wrapper(REAL_IO_OPEN, 'io.open')), \
                patch.object(os, 'open', guard.os_open), \
                patch.object(os, 'read', guard.os_read), \
                patch.object(fold, 'read_events', guard.read_events), \
                patch.object(ledger_files, 'read_text', guard.read_text), \
                patch.object(realm_ledger, 'Changes', Recording if mode == 'native' else Unavailable), \
                patch.object(tick, '_events_state', snaps), \
                patch.object(tick, '_record', sentinel):
            if thread is not None:
                thread.start()
            started = time.monotonic()
            guard.start()
            try:
                reason = tick.wait(root, max_s=max_s)
            finally:
                guard.stop()
                abort.set()
                snaps.baseline_ready.set()
            wall = time.monotonic() - started
            stamps['returned'] = time.monotonic()
        bound_events = fold.EVENTS
    finally:
        abort.set()
        if thread is not None:
            thread.join(15)
        fold.ROOT, fold.EVENTS = saved
        if after is not None:
            after(ctx)
    if thread is not None and thread.is_alive():
        raise AssertionError(name + ': writer join timed out')
    if errors:
        raise AssertionError(name + ' writer errors: ' + repr(errors))
    return dict(name=name, mode=mode, actual_mode='native' if instances and instances[0] is not None
                else 'fallback', reason=reason, wall=wall, max_s=max_s, closing=len(closing),
                forbidden=dict(guard.counts), forbidden_total=guard.total(), hits=guard.hits,
                permitted_after_phase=dict(guard.permitted), phase_open=guard.active,
                snapshots=[(t, v) for t, v in snaps.calls], polls=polls, stamps=stamps,
                stat=stat, target=target, bound=str(bound_events), events=str(events),
                watchers=len(instances), closing_read=stamps.get('closing_read'))


def report(row, **extra):
    stamps = row['stamps']
    base = stamps.get('baseline')
    ordering = None
    if 'mutation' in stamps:
        ordering = dict(baseline_to_mutation=round(stamps['mutation'] - base, 4),
                        mutation_to_return=round(stamps['closing'] - stamps['mutation'], 4))
    body = dict(row=row['name'], watcher_mode=row['actual_mode'], reason=row['reason'],
                wall=round(row['wall'], 3), snapshots=len(row['snapshots']),
                notifications=[p[1] for p in row['polls']],
                forbidden={c: row['forbidden'].get(c, 0) for c in CATEGORIES},
                forbidden_total=row['forbidden_total'], closing_calls=row['closing'],
                stat=row['stat'], ordering=ordering,
                permitted_after_phase=row['permitted_after_phase'], **extra)
    print(json.dumps(body, sort_keys=True, default=str), flush=True)


class RowCase(unittest.TestCase):
    def clean(self, row, mode, reason):
        tag = row['name'] + ': '
        self.assertEqual(row['actual_mode'], mode, tag + 'watcher mode')
        if mode == 'native':
            self.assertEqual(row['watchers'], 1, tag + 'native watcher missing; inotify unavailable')
        self.assertEqual(row['bound'], row['events'], tag + 'wait bound the scratch events dir')
        self.assertEqual(row['reason'], reason, tag)
        self.assertEqual(row['forbidden_total'], 0, tag + 'forbidden accesses %r' % (row['hits'],))
        self.assertEqual(row['closing'], 1, tag + 'exactly one closing call')
        self.assertFalse(row['phase_open'], tag + 'phase ended at the closing call')
        self.assertGreaterEqual(len(row['snapshots']), 1, tag + 'baseline snapshot taken')

    def interval(self, row, mode):
        self.clean(row, mode, 'interval')
        self.assertGreaterEqual(row['wall'], row['max_s'] * 0.9, row['name'] + ': waited the interval')

    def delta(self, row, kind):
        tag = row['name'] + ': '
        pre, post, stamps = row['stat']['pre'], row['stat']['post'], row['stamps']
        baseline = row['snapshots'][0][1][row['target']]
        self.assertEqual(baseline, (pre['size'], pre['mtime_ns']), tag + 'baseline matches pre-mutation stat')
        if kind == 'size':
            self.assertGreater(post['size'], pre['size'], tag)
        elif kind == 'mtime':
            self.assertGreater(post['mtime_ns'], pre['mtime_ns'], tag)
            self.assertEqual(post['size'], pre['size'], tag + 'size preserved')
        elif kind == 'chmod':
            self.assertNotEqual(post['mode'], pre['mode'], tag + 'mode changed')
            self.assertEqual((post['size'], post['mtime_ns']), (pre['size'], pre['mtime_ns']), tag)
        self.assertLess(stamps['baseline'], stamps['mutation'], tag + 'baseline before mutation')

    def wake(self, row, mode, kind):
        self.clean(row, mode, 'changed')
        self.delta(row, kind)
        self.assertLess(row['stamps']['mutation'], row['stamps']['closing'], row['name'] + ': mutation before return')
        self.assertLess(row['wall'], row['max_s'], row['name'] + ': returned before timeout')
        if mode == 'native':
            self.assertTrue(any(p[1] and row['target'] in p[1] for p in row['polls']),
                            row['name'] + ': native notification for the target observed')
        post = row['snapshots'][-1][1][row['target']]
        self.assertNotEqual(post, row['snapshots'][0][1][row['target']], row['name'] + ': confirming snapshot moved')


def append(path):
    with path.open('a') as stream:
        stream.write(LINE)


def bump_mtime(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def toggle_mode(path):
    os.chmod(path, 0o400 if os.stat(path).st_mode & 0o777 != 0o400 else 0o600)


def tick_append(path):
    append(path)


class AC1Tests(RowCase):
    def test_native_unchanged(self):
        row = run_row('AC1-native-unchanged', 'native', max_s=0.8)
        self.interval(row, 'native')
        report(row)

    def test_fallback_unchanged(self):
        row = run_row('AC1-fallback-unchanged', 'fallback', max_s=0.8)
        self.interval(row, 'fallback')
        # at least two real polling snapshots after the baseline
        self.assertGreaterEqual(len(row['snapshots']) - 1, 2, 'fallback polling snapshots after baseline')
        self.assertEqual(row['polls'], [], 'no notification claimed on the fallback row')
        report(row)


class AC2Tests(RowCase):
    def test_native_append_size(self):
        row = run_row('AC2-native-append-size', 'native', max_s=5, target='a.jsonl', mutate=append)
        self.wake(row, 'native', 'size')
        report(row)

    def test_fallback_append_size(self):
        row = run_row('AC2-fallback-append-size', 'fallback', max_s=5, target='a.jsonl', mutate=append)
        self.wake(row, 'fallback', 'size')
        report(row)

    def test_native_utime_mtime_ns(self):
        row = run_row('AC2-native-utime-mtime', 'native', max_s=5, target='a.jsonl', mutate=bump_mtime)
        self.wake(row, 'native', 'mtime')
        report(row)

    def test_fallback_utime_mtime_ns(self):
        row = run_row('AC2-fallback-utime-mtime', 'fallback', max_s=5, target='a.jsonl', mutate=bump_mtime)
        self.wake(row, 'fallback', 'mtime')
        report(row)


class AC3Tests(RowCase):
    """Noise rows. The tick file is excluded from change detection so that each
    returning tick (which appends to it) cannot wake the next waiter."""
    def test_native_tick_only_append(self):
        row = run_row('AC3-native-tick-append', 'native', max_s=0.8, target=tick.TICK_NAME, mutate=tick_append)
        self.interval(row, 'native')
        self.assertGreater(row['stat']['post']['size'], row['stat']['pre']['size'])
        self.assertTrue(any(p[1] and tick.TICK_NAME in p[1] for p in row['polls']),
                        'native notification for the tick file observed')
        self.assertTrue(all(tick.TICK_NAME not in s[1] for s in row['snapshots']), 'tick file excluded')
        report(row, why_excluded='each returning tick appends to it and must not wake the next waiter')

    def test_fallback_tick_only_append(self):
        row = run_row('AC3-fallback-tick-append', 'fallback', max_s=0.8, target=tick.TICK_NAME, mutate=tick_append)
        self.interval(row, 'fallback')
        self.assertGreater(row['stat']['post']['size'], row['stat']['pre']['size'])
        self.assertTrue(all(tick.TICK_NAME not in s[1] for s in row['snapshots']), 'tick file excluded')
        self.assertEqual(row['polls'], [], 'no notification claimed on the fallback row')
        report(row, why_excluded='each returning tick appends to it and must not wake the next waiter')

    def chmod_row(self, mode):
        row = run_row('AC3-%s-chmod' % mode, mode, max_s=0.8, target='a.jsonl', mutate=toggle_mode)
        self.interval(row, mode)
        self.delta(row, 'chmod')
        baseline = row['snapshots'][0][1]
        after = [s for s in row['snapshots'][1:] if s[0] > row['stamps']['mutation']]
        self.assertTrue(after, 'a real snapshot after the mutation')
        self.assertTrue(all(s[1] == baseline for s in after), 'post-mutation snapshots equal baseline')
        return row

    def test_native_chmod(self):
        row = self.chmod_row('native')
        seen = [p for p in row['polls'] if p[1] and 'a.jsonl' in p[1] and p[0] > row['stamps']['mutation']]
        self.assertTrue(seen, 'actual attribute notification observed')
        report(row)

    def test_fallback_chmod(self):
        row = self.chmod_row('fallback')
        self.assertEqual(row['polls'], [], 'no notification claimed on the fallback row')
        report(row)


class AC4Tests(RowCase):
    """Guard sensitivity: every injected forbidden access is detected, and the
    behavior check fails on it. Injection runs in the waiting thread through the
    snapshot boundary; the real `_events_state` answer is still returned."""
    def control(self, label, category, injected, before=None, after=None):
        marker = []
        def inject(ctx):
            injected(ctx)
            marker.append(1)
        row = run_row('AC4-control-' + label, 'native', max_s=0.4, inject=inject,
                      before=before, after=after)
        self.assertEqual(marker, [1], label + ': injection ran (instrumentation not inert)')
        self.assertGreaterEqual(row['forbidden'].get(category, 0), 1,
                                '%s: %s not detected: %r' % (label, category, row['forbidden']))
        with self.assertRaises(AssertionError, msg=label + ': behavior check must fail'):
            self.clean(row, 'native', 'interval')
        self.assertEqual(row['closing'], 1)
        report(row, control=label, detected_category=category)
        return row

    # the controls address the ledger by its stable scratch location: a path
    # run_row creates, known before the call because it derives from the row name.
    @staticmethod
    def target(label):
        return str(TMP / ('AC4-control-' + label) / 'events' / 'a.jsonl')

    def test_builtins_open(self):
        label = 'builtins-open'
        self.control(label, 'builtins.open', lambda _: builtins.open(self.target(label)).close())

    def test_io_open(self):
        label = 'io-open'
        self.control(label, 'io.open', lambda _: io.open(self.target(label)).close())

    def test_path_open(self):
        label = 'path-open'
        self.control(label, 'io.open', lambda _: Path(self.target(label)).open().close())

    def test_os_open(self):
        label = 'os-open'
        self.control(label, 'os.open', lambda _: os.close(os.open(self.target(label), os.O_RDONLY)))

    def test_os_open_dir_fd(self):
        label = 'os-open-dirfd'
        def run(_):
            dfd = REAL_OS_OPEN(os.path.dirname(self.target(label)), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.close(os.open('a.jsonl', os.O_RDONLY, dir_fd=dfd))
            finally:
                os.close(dfd)
        self.control(label, 'os.open', run)

    def test_relative_open(self):
        label = 'relative-open'
        def run(_):
            cwd = os.getcwd()
            os.chdir(os.path.dirname(self.target(label)))
            try:
                builtins.open('a.jsonl').close()
            finally:
                os.chdir(cwd)
        self.control(label, 'builtins.open', run)

    def test_preopened_descriptor_read(self):
        # the ledger descriptor is opened before the phase; the content read happens inside it
        label = 'preopened-descriptor'
        self.control(label, 'os.read', lambda fd: os.read(fd, 8),
                     before=lambda events: REAL_OS_OPEN(events / 'a.jsonl', os.O_RDONLY),
                     after=os.close)

    def test_read_events(self):
        self.control('read-events', 'fold.read_events', lambda _: fold.read_events(use_cache=False))

    def test_read_text(self):
        label = 'read-text'
        self.control(label, 'ledger_files.read_text',
                     lambda _: ledger_files.read_text(Path(self.target(label)).parent, 'a.jsonl'))

    def test_inotify_read_is_permitted(self):
        # genuine native row: the only descriptor reads are on the inotify fd
        row = run_row('AC4-genuine-native', 'native', max_s=5, target='a.jsonl', mutate=append)
        self.wake(row, 'native', 'size')
        self.assertTrue(row['polls'], 'inotify reads happened and were permitted')
        report(row)

    def test_closing_sentinel_may_read(self):
        def read(events):
            with builtins.open(events / 'a.jsonl') as stream:
                return stream.read()
        row = run_row('AC4-closing-read', 'native', max_s=0.3, closing_read=read)
        self.interval(row, 'native')
        self.assertEqual(row['closing_read'], LINE, 'closing sentinel really read the scratch ledger')
        self.assertGreaterEqual(row['permitted_after_phase'].get('builtins.open', 0), 1)
        report(row)

    def test_existing_notification_suite_unchanged(self):
        done = subprocess.run([sys.executable, str(SRC / 'test_tick_wait_notify.py')],
                              capture_output=True, text=True, timeout=300, cwd=SRC)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        print(json.dumps(dict(row='AC4-existing-notify-suite', returncode=done.returncode,
                              tail=done.stderr.strip().splitlines()[-1:])), flush=True)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        _TEMP.cleanup()
