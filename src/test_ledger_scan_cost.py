"""Scratch-ledger equivalence, bounded queries, and corrupt-cache regressions."""
import atexit
import copy
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import subprocess
import sys
import time
import resource
from contextlib import contextmanager
import unittest
from unittest.mock import patch


# Imports and every test use disposable roots, never the operator's ledger.
IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='ledger-scan-import-')
atexit.register(IMPORT_ROOT.cleanup)
with patch.dict(os.environ, DOIT_ROOT=IMPORT_ROOT.name,
                V4_LEDGER_DIR=str(Path(IMPORT_ROOT.name) / 'v4-ledger'),
                V4_INBOX_DIR=str(Path(IMPORT_ROOT.name) / 'v4-inbox')):
    import dispatch
    import fold
    import ledger_cache
import ledger_compact
import ledger_files
import ledger_rollup


def original_subject_ids(prefix):
    out = []
    for f in sorted(dispatch.EVENTS.glob('*.jsonl')):
        for line in f.read_text().splitlines():
            m = re.match(rf'{re.escape(prefix)}(\d+)$', (json.loads(line).get('subject') or '')
                         ) if line.strip().startswith('{') else None
            m and out.append(int(m.group(1)))
    return out


class CountQueries:
    def __init__(self, db):
        self.db = db
        self.statements = []
        self.streams = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        cursor = self.db.execute(sql, *args)
        if sql == 'SELECT name,seq,data,sha FROM chunks ORDER BY name,seq':
            # A generator has no fetchall: chunks must be consumed as a stream.
            def stream():
                try:
                    yield from cursor
                finally:
                    cursor.close()
            rows = stream()
            # Retain it so an abandoned stream cannot pass through GC cleanup.
            self.streams.append(rows)
            return rows
        return cursor


class ScanCostTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ledger-scan-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = self.root / 'events'
        self.cache = self.root / 'cache'
        self.events.mkdir()
        for patches in (
                patch.multiple(fold, ROOT=self.root, EVENTS=self.events,
                               SESSIONS=self.root / 'sessions', PROJECT=None),
                patch.object(dispatch, 'EVENTS', self.events),
                patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_READ_CACHE='1')):
            patches.start()
            self.addCleanup(patches.stop)

    def test_warm_full_ledger_budget(self):
        # Build and measure in separate processes: no process-local event reuse,
        # and RSS excludes the cold parser's high-water mark.
        for mode in ('build', 'warm'):
            run = subprocess.run([sys.executable, __file__, '--profile-worker',
                                  str(self.root), mode], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            if mode == 'warm':
                result = json.loads(run.stdout.splitlines()[-1])
                print('L-spec-2044 phase table (seconds): ' + json.dumps(result, sort_keys=True))
                self.assertEqual(result['events'], 216875)
                self.assertLess(result['cpu_s'], 5)
                self.assertLess(result['rss_mb'], 250)

    def test_ac1_subject_equivalence_and_parse_count(self):
        lines = [
            json.dumps(dict(subject='L-builder-12')),
            json.dumps(dict(subject='L-spec-3', ref='L-builder-14')),
            json.dumps(dict(subject='L-spec-4', why='L-builder-15')),
            json.dumps(dict(subject='L-builder-12x')),
            json.dumps(dict(subject='L-spec-5')),
            'not JSON', '',
        ]
        (self.events / 'L-operator-01.jsonl').write_text('\n'.join(lines) + '\n')
        (self.events / 'L-operator-02.jsonl').write_text(
            '  ' + json.dumps(dict(subject='L-builder-7')) + '\n')
        expected = original_subject_ids('L-builder-')
        parse_lines = [line for file in sorted(self.events.glob('*.jsonl'))
                       for line in file.read_text().splitlines()
                       if 'L-builder-' in line and line.strip().startswith('{')]
        with patch.object(dispatch.json, 'loads', wraps=json.loads) as loads:
            self.assertEqual(dispatch.subject_ids('L-builder-'), expected)
            self.assertEqual([call.args[0] for call in loads.call_args_list], parse_lines)
        self.assertEqual(sorted(expected), [7, 12])
        # The intentional malformed-line change applies only without the prefix.
        (self.events / 'L-operator-03.jsonl').write_text('{broken\n')
        self.assertEqual(dispatch.subject_ids('L-builder-'), expected)
        print(f'AC1 equality=true parsed={len(parse_lines)} matching_ids={len(expected)}')
        print('L-spec-1786 AC1 ok')

    def fixture(self):
        sizes = (0, 1, 255, 256, 257, 3 * ledger_cache.CHUNK_EVENTS + 3)
        for index in range(40):
            rows = [dict(ts=f'{n:05}', type='observed', subject=f'L-spec-{index}',
                         value=n) for n in range(sizes[index % len(sizes)])]
            text = ''.join(json.dumps(row) + '\n' for row in rows)
            if index % 3 == 0:
                text += json.dumps(dict(ts='99999', type='observed', tail=index))
            (self.events / f'L-operator-{index:02}.jsonl').write_text(text)

    def read_cache(self):
        connections = []
        original_connect = ledger_cache._connect

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            connections.append(db)
            return db

        with patch.object(ledger_cache, '_connect', side_effect=connect):
            result = ledger_cache.read(self.events, self.cache, fold.actor_of)
        self.assertTrue(connections)
        for db in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed database'):
                db.execute('SELECT 1')
        return result

    def database(self):
        paths = list(self.cache.glob('*.sqlite3'))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_unchanged_zero_parses_and_committed_fresh_session(self):
        self.fixture()
        expected = self.read_cache()  # Production SQLite writer, fully closed.
        with patch.object(ledger_cache, '_parse', side_effect=AssertionError('unchanged parse')):
            self.assertEqual(self.read_cache(), expected)
            with closing(ledger_cache._connect(self.database())) as fresh:
                self.assertEqual(ledger_cache._read(fresh), expected)
        print('L-spec-2049 AC3 unchanged_parsed_lines=0 committed_fresh_session_equal=true')

    def test_projection_consumer_search_and_value_preservation(self):
        field = 'worktrees_retained'
        pattern = re.compile(re.escape(field))
        matches = []
        for path in sorted(Path(__file__).parent.rglob('*.py')):
            if path.name.startswith('test'):
                continue
            for number, source in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(source):
                    matches.append(f'{path}:{number}:{source}')
        search = '\n'.join(matches)
        # Every use outside the declaration must be a producer. Assert exact
        # producer expressions, so adding a consumer inside tick fails too.
        expected_producer = {
            'worktrees_retained = None', 'worktrees_retained = len(wt_retained)',
            'if worktrees_retained is not None:',
            'kv["worktrees_retained"] = worktrees_retained',
        }
        uses = set()
        for line in search.splitlines():
            path, number, source = line.split(':', 2)
            if Path(path).name != 'ledger_compact.py':
                self.assertEqual(Path(path).name, 'tick.py')
                uses.add(source.strip())
        self.assertEqual(uses, expected_producer)
        self.assertIn(field, ledger_compact.PROJECTION['tick'])
        event = dict(type='tick', ts='1', subject='tick', worktrees_retained=7,
                     recorded_reap=['old'], recorded_reap_counts={'kept': 7},
                     detail={'nested': [1, False, None, 'é']})
        before = copy.deepcopy(event)
        projected = ledger_compact.compact(event)
        self.assertEqual(event, before)
        self.assertEqual(projected, {k: v for k, v in event.items()
                                    if k not in ledger_compact.PROJECTION['tick']})
        self.assertEqual(ledger_compact.compact(projected), projected)
        self.assertEqual(projected['recorded_reap_counts'], {'kept': 7})
        # A previously committed cache must not keep the superseded view.
        path = self.events / 'L-tick-01.jsonl'
        path.write_text(json.dumps(event) + '\n')
        old_projection = {**ledger_compact.PROJECTION, 'tick': ('recorded_reap',)}
        with patch.object(ledger_compact, 'PROJECTION', old_projection):
            self.assertEqual(self.read_cache()[0][field], 7)
        self.assertNotIn(field, self.read_cache()[0])
        print('L-spec-2049 AC3 field-consumer grep:\n' + search.rstrip())
        print('L-spec-2049 AC3 projection_pure=true retained_values_equal=true')

    def test_executor_40mb_rotation_bytes_hash_and_fold(self):
        if '--rotation-worker' not in sys.argv:
            # Keep this fixture's 40 MB byte comparisons out of the parent
            # process: forked performance workers inherit its RSS high-water.
            worker = subprocess.run([sys.executable, __file__, '--rotation-worker'],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(worker.returncode, 0, worker.stdout + worker.stderr)
            print(worker.stdout, end='')
            return
        # Forty MB of raw history, including whitespace and CRLF; only forty
        # event objects so the proof does not need a ledger-sized folded heap.
        name = 'L-executor-0001.jsonl'
        path = self.events / name
        original = b''.join((json.dumps(dict(type='observed', ts=f'{n:03}', subject='fixture'))
                            .encode() + b'\r\n' + b' ' * 1_000_000 + b'\n') for n in range(40))
        path.write_bytes(original)
        expected = fold.read_events()
        state = fold.fold(expected)
        (self.root / 'state').mkdir()
        (self.root / 'state/ledger-rollup-approved').touch()
        with patch.object(ledger_rollup.os, 'nice'), \
             patch.object(ledger_rollup, 'open_files', return_value=(set(), False, 0)):
            self.assertEqual(ledger_rollup.main([]), 0)
        indexes = ledger_files._indexes(self.events)
        self.assertTrue(indexes)
        parts = []
        for index_path, obj, error in indexes:
            self.assertIsNone(error)
            self.assertEqual(obj['version'], 1)
            parts.append((index_path.parent / obj['segment']).read_bytes())
        packed = b''.join(parts)
        self.assertEqual(packed, original)
        self.assertEqual(hashlib.sha256(packed).digest(), hashlib.sha256(original).digest())
        self.assertLess(path.stat().st_size, 8_000_000)
        self.assertEqual(fold.read_events(), expected)
        self.assertEqual(fold.fold(fold.read_events()), state)
        # Continue the same source, then rotate a second continuation. Source
        # line identities/order and concatenated raw hashes remain unchanged.
        suffix = json.dumps(dict(type='observed', ts='999', subject='tail')).encode() + b'\n'
        suffix += b' ' * 8_000_001 + b'\n'
        with path.open('ab') as stream:
            stream.write(suffix)
        self.assertEqual(ledger_files._read_bytes(self.events, name), original + suffix)
        with patch.object(ledger_rollup.os, 'nice'), \
             patch.object(ledger_rollup, 'open_files', return_value=(set(), False, 0)):
            self.assertEqual(ledger_rollup.main([]), 0)
        self.assertEqual(path.stat().st_size, 0)
        self.assertEqual(ledger_files._read_bytes(self.events, name), original + suffix)
        self.assertEqual(fold.read_events()[-1]['_src'], name + ':81')
        self.assertEqual(ledger_files.verify_segments(self.events, self.root / 'verify'), [])
        print('L-spec-2049 AC2 ' + json.dumps(dict(source_bytes=len(original),
              segment_bytes=len(packed), live_bytes=path.stat().st_size,
              source_sha256=hashlib.sha256(original).hexdigest(),
              segment_sha256=hashlib.sha256(packed).hexdigest(), bytes_equal=True, fold_equal=True)))

    def test_executor_rotation_defers_busy_and_torn_tail(self):
        path = self.events / ledger_rollup.EXECUTOR
        path.write_bytes(b' ' * 8_000_001)
        (self.root / 'state').mkdir()
        (self.root / 'state/ledger-rollup-approved').touch()
        stat = path.stat()
        with patch.object(ledger_rollup.os, 'nice'), patch.object(ledger_rollup, 'open_files',
                return_value=({(stat.st_dev, stat.st_ino)}, False, 0)):
            self.assertEqual(ledger_rollup.main([]), 0)
        self.assertEqual(path.stat().st_size, 8_000_001)
        self.assertFalse((self.events / 'segments').exists())
        with patch.object(ledger_rollup.os, 'nice'), \
             patch.object(ledger_rollup, 'open_files', return_value=(set(), False, 0)):
            self.assertEqual(ledger_rollup.main([]), 0)
        self.assertEqual(path.read_bytes(), b' ' * 8_000_001)
        self.assertFalse((self.events / 'segments').exists())
        print('L-spec-2049 AC2 busy_writer_deferred=true torn_tail_retained=true')

    def test_executor_rotation_crash_recovers_empty_live(self):
        path = self.events / ledger_rollup.EXECUTOR
        data = b' ' * 8_000_001 + b'\n'
        path.write_bytes(data)
        (self.root / 'state').mkdir()
        (self.root / 'state/ledger-rollup-approved').touch()
        class Crash(BaseException):
            pass
        def crash(step):
            if step == 8:
                raise Crash()
        with patch.object(ledger_rollup.os, 'nice'), \
             patch.object(ledger_rollup, 'open_files', return_value=(set(), False, 0)):
            with patch.object(ledger_rollup, 'checkpoint', side_effect=crash), self.assertRaises(Crash):
                ledger_rollup.main([])
            self.assertFalse(path.exists())
            self.assertEqual(ledger_files._read_bytes(self.events, path.name), data)
            self.assertEqual(ledger_rollup.main([]), 0)
        self.assertEqual(path.stat().st_size, 0)
        self.assertEqual(ledger_files._read_bytes(self.events, path.name), data)
        print('L-spec-2049 AC2 executor_crash_recovery=true live_bytes=0')

    def test_ac2_equivalence_appends_and_constant_queries(self):
        counts = []
        # Compare one file with forty files, not just a single upper bound.
        for file_count in (1, 40):
            self.fixture()
            if file_count == 1:
                for file in sorted(self.events.glob('*.jsonl'))[1:]:
                    file.unlink()
            for appended in (False, True):
                if appended:
                    for file in self.events.glob('*.jsonl'):
                        with file.open('a') as stream:
                            stream.write('\n' + json.dumps(dict(ts='99999', type='appended')) + '\n')
                expected = fold._read_events_uncached()
                actual = self.read_cache()
                self.assertEqual(actual, expected)
                self.assertEqual(fold.finish_events(actual), fold.finish_events(expected))
                with closing(sqlite3.connect(self.database())) as db:
                    proxy = CountQueries(db)
                    self.assertEqual(ledger_cache._read(proxy), actual)
                    self.assertTrue(all(rows.gi_frame is None for rows in proxy.streams))
                    self.assertLessEqual(len(proxy.statements), 3)
                    self.assertEqual(proxy.statements.count(
                        'SELECT name,seq,data,sha FROM chunks ORDER BY name,seq'), 1)
                    counts.append(len(proxy.statements))
        self.assertEqual(len(set(counts)), 1)
        print(f'AC2 equality=true files=1,40 appends=true execute_counts={counts}')
        print('L-spec-1786 AC2 ok')

    def test_ac3_corruption_and_orphans(self):
        self.fixture()
        expected = fold.finish_events(fold._read_events_uncached())
        self.assertEqual(fold.finish_events(self.read_cache()), expected)
        name = 'L-operator-05.jsonl'  # Four chunks, so seq=1 is a middle chunk.
        cases = (('middle', 'cache chunk sequence'),
                 ('last', 'cache missing chunks'),
                 ('all', 'cache missing chunks'),
                 ('byte', 'cache checksum'),
                 ('orphan', None), ('orphans', None))
        for case, error in cases:
            with self.subTest(case=case):
                with closing(sqlite3.connect(self.database())) as db:
                    if case in ('middle', 'last', 'all'):
                        condition = {'middle': 'seq=1', 'last': 'seq=3', 'all': 'seq>=0'}[case]
                        count = db.execute('SELECT COUNT(*) FROM chunks WHERE name=? AND ' + condition,
                                           (name,)).fetchone()[0]
                        self.assertEqual(count, 4 if case == 'all' else 1)
                        print(f'AC3 {case} scratch_rows_before_delete={count}')
                        db.execute('DELETE FROM chunks WHERE name=? AND ' + condition, (name,))
                    elif case == 'byte':
                        data = db.execute('SELECT data FROM chunks WHERE name=? AND seq=0',
                                          (name,)).fetchone()[0]
                        db.execute('UPDATE chunks SET data=? WHERE name=? AND seq=0',
                                   (bytes([data[0] ^ 1]) + data[1:], name))
                    else:
                        names = (['L-operator-04x.jsonl'] if case == 'orphan' else
                                 ['A-orphan.jsonl', 'L-operator-04x.jsonl', 'Z-orphan.jsonl'])
                        for orphan in names:
                            # Bad sequence and checksum must be ignored before validation.
                            db.execute('INSERT OR REPLACE INTO chunks VALUES (?,?,?,?)',
                                       (orphan, 99, b'bad', b'bad'))
                    db.commit()
                    proxy = CountQueries(db)
                    if error:
                        with self.assertRaisesRegex(ledger_cache.Invalid, '^' + error + '$'):
                            ledger_cache._read(proxy)
                    else:
                        self.assertEqual(fold.finish_events(ledger_cache._read(proxy)), expected)
                    self.assertTrue(all(rows.gi_frame is None for rows in proxy.streams))
                self.assertEqual(fold.read_events(), expected)
                print(f'AC3 {case}: {error or "ignored"}; observable_equality=true')

        # Simulate a collation disagreement: skip even late orphans first,
        # then reject a real name that moves backwards across a finished tail.
        with closing(sqlite3.connect(self.database())) as db:
            class Reordered(CountQueries):
                def execute(proxy, sql, *args):
                    rows = super().execute(sql, *args)
                    if sql == 'SELECT name,seq,data,sha FROM chunks ORDER BY name,seq':
                        def backwards():
                            first = None
                            for row in rows:
                                if first is None and row[0] == name:
                                    first = row
                                yield row
                            self.assertIsNotNone(first)
                            yield first
                        return backwards()
                    return rows
            with self.assertRaisesRegex(ledger_cache.Invalid, '^cache chunk order$'):
                ledger_cache._read(Reordered(db))
        (self.events / 'L-operator-bad.jsonl').write_text('{"subject": "L-builder-12", broken\n')
        with self.assertRaises(json.JSONDecodeError):
            dispatch.subject_ids('L-builder-')
        print('AC3 backwards_order=rejected malformed_matching_line=rejected')
        print('L-spec-1786 AC3 ok')


def profile_worker(root, mode):
    root = Path(root)
    events = root / 'events'
    cache = root / 'cache'
    if mode == 'build':
        # Repeated evidence and nested payloads, spread across independent chunks.
        for file in range(128):
            with (events / f'L-operator-{file:04}.jsonl').open('w') as stream:
                for n in range(file, 216875, 128):
                    stream.write(json.dumps(dict(ts=f'{n:09}', type='observed',
                        subject=f'L-spec-{n % 2000:04}',
                        reading={'description': 'repeated evidence ' * 32,
                                 'rows': ['same evidence', n]})) + '\n')
        ledger_cache.read(events, cache, fold.actor_of)
        return
    phases = {name: dict(wall_s=0.0, cpu_s=0.0)
              for name in ('lock_wait', 'unpack', 'digest', 'fold')}

    def clocks():
        return time.perf_counter(), time.process_time()

    def record(name, start):
        wall, cpu = clocks()
        phases[name]['wall_s'] += wall - start[0]
        phases[name]['cpu_s'] += cpu - start[1]

    original_lock = ledger_cache._locked
    @contextmanager
    def locked(*args):
        start = clocks()
        with original_lock(*args):
            record('lock_wait', start)
            yield
    def timed(name, function):
        def call(*args, **kwargs):
            start = clocks()
            try:
                return function(*args, **kwargs)
            finally:
                record(name, start)
        return call
    # SHA timings are subtracted from unpack to keep the phase table disjoint.
    original_sha = ledger_cache.hashlib.sha256
    with patch.object(ledger_cache, '_locked', locked), \
         patch.object(ledger_cache, '_unpack', timed('unpack', ledger_cache._unpack)), \
         patch.object(ledger_cache.hashlib, 'sha256', timed('digest', original_sha)), \
         patch.multiple(fold, ROOT=root, EVENTS=events, PROJECT=None):
        start = clocks()
        rows = fold.read_events()
        fold_start = clocks()
        state = fold.fold(rows)
        record('fold', fold_start)
        end = clocks()
        totals = dict(wall_s=end[0] - start[0], cpu_s=end[1] - start[1])
    phases['normalize_and_other'] = {}
    for clock, total in totals.items():
        phases['unpack'][clock] = max(0, phases['unpack'][clock] - phases['digest'][clock])
        phases['normalize_and_other'][clock] = max(
            0, total - sum(phase.get(clock, 0) for phase in phases.values()))
    print(json.dumps(dict(totals, phases=phases, events=len(rows),
                         rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--profile-worker':
        profile_worker(*sys.argv[2:])
    elif '--rotation-worker' in sys.argv:
        unittest.main(argv=[__file__, 'ScanCostTests.test_executor_40mb_rotation_bytes_hash_and_fold'])
    else:
        unittest.main()
