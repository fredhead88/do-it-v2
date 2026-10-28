"""Scratch-ledger equivalence, bounded queries, and corrupt-cache regressions."""
import atexit
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
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


if __name__ == '__main__':
    unittest.main()
