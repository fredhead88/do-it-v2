"""Scratch-only regression coverage for virtual ledger file composition."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

# No imports in this file may bind to the operator's ledger.
_ROOT = tempfile.TemporaryDirectory(prefix='ledger-files-tests-')
os.environ['DOIT_ROOT'] = _ROOT.name
import ledger_files as lf


class LedgerFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()

    def segment(self, label, members, directory=None):
        directory = (directory or self.events) / lf.SEGMENT_DIR
        directory.mkdir(exist_ok=True)
        data, rows = b'', []
        for name, content in members:
            rows.append(dict(name=name, offset=len(data), length=len(content),
                             lines=len(content.splitlines()), sha256=hashlib.sha256(content).hexdigest(),
                             ends_nl=not content or content.endswith(b'\n')))
            data += content
        segment = directory / f'{label}.jsonl'
        segment.write_bytes(data)
        index = directory / f'{label}.idx.json'
        index.write_text(json.dumps(dict(version=1, segment=segment.name, bytes=len(data),
                                        sha256=hashlib.sha256(data).hexdigest(), members=rows)))
        return segment, index

    def test_ac1_equivalence_and_continuations(self):
        live = self.root / 'live'
        live.mkdir()
        a, b = 'L-builder-0001.jsonl', 'L-executor-0002.jsonl'
        self.segment('a', [(a, b'first'), (b, b'bad-utf8-\xff\r\n')])
        self.segment('b', [(a, b'second')])
        (self.events / a).write_bytes(b'third\n')  # recreated live file
        (live / a).write_bytes(b'first\nsecond\nthird\n')
        (live / b).write_bytes(b'bad-utf8-\xff\r\n')
        self.assertEqual(lf.names(live), lf.names(self.events))
        for name in lf.names(live):
            self.assertEqual(lf.read_text(live, name, errors='replace'),
                             lf.read_text(self.events, name, errors='replace'))
        with self.assertRaises(UnicodeDecodeError):
            lf.read_text(self.events, b)
        (self.events / b).write_bytes(b'bad-utf8-\xff\r\n')
        self.assertEqual(lf.read_text(self.events, b, errors='replace'), 'bad-utf8-\ufffd\n')
        (self.events / b).write_bytes(b'bad-utf8-\xff\r\nsuffix\n')
        self.assertEqual(lf.read_text(self.events, b, errors='replace'), 'bad-utf8-\ufffd\nsuffix\n')
        # iter_sources is strict, so remove the intentionally invalid bytes first.
        (live / b).unlink()
        clean = self.root / 'clean'
        clean.mkdir()
        self.segment('a', [(a, (live / a).read_bytes())], clean)
        self.assertEqual(list(lf.iter_sources(live)), list(lf.iter_sources(clean)))
        self.assertEqual(list(lf.iter_sources(clean))[0][1], ['first', 'second', 'third'])
        self.assertTrue(lf.exists(self.events, a))
        self.assertFalse(lf.exists(self.events, 'absent.jsonl'))
        with self.assertRaises(FileNotFoundError):
            lf.read_text(self.events, 'absent.jsonl')
        before = lf.stat_key(self.events, a)
        self.assertEqual(len(before), 5)
        with (self.events / a).open('ab') as stream:
            stream.write(b'fourth\n')
        self.assertNotEqual(before, lf.stat_key(self.events, a))
        index = self.events / lf.SEGMENT_DIR / 'a.idx.json'
        before = lf.stat_key(self.events, a)
        stamp = index.stat().st_mtime_ns + 10_000_000
        os.utime(index, ns=(stamp, stamp))
        self.assertNotEqual(before, lf.stat_key(self.events, a))
        file = live / a
        st = file.stat()
        self.assertEqual(lf.stat_key(live, a), (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        print('L-spec-1787 AC1 ok')

    def test_invalid_structure_tmp_names_and_purity(self):
        name = 'L-builder-1.jsonl'
        _, index = self.segment('a', [(name, b'one\n')])
        original = json.loads(index.read_text())
        for field in ('name', 'offset', 'length', 'lines', 'sha256', 'ends_nl'):
            obj = json.loads(json.dumps(original))
            del obj['members'][0][field]
            index.write_text(json.dumps(obj))
            self.assertEqual(lf.names(self.events), [])
            self.assertTrue(lf.problems(self.events))
        for changes, reason in ((dict(ends_nl=False), 'ends_nl'),
                                (dict(offset=100), 'slice'),
                                (dict(name='../escape.jsonl'), 'basename')):
            obj = json.loads(json.dumps(original))
            obj['members'][0].update(changes)
            index.write_text(json.dumps(obj))
            self.assertIn(reason, lf.problems(self.events)[0][1])
        index.write_text(json.dumps(original))
        (index.parent / 'in-progress.idx.json.tmp').write_text('incomplete')
        self.assertEqual(lf.problems(self.events), [])
        for name in ('../x', 'x/y', '..', 'a..b'):
            for fn in (lf.exists, lf.read_text, lf.stat_key):
                with self.assertRaises(ValueError):
                    fn(self.events, name)
        fresh = self.root / 'fresh'
        fresh.mkdir()
        self.assertEqual(lf.names(fresh), [])
        lf.assert_readable(fresh)
        self.assertEqual(lf.problems(fresh), [])
        self.assertEqual(lf.verify_segments(fresh, self.root / 'state'), [])
        self.assertEqual(list(fresh.iterdir()), [])
        self.assertFalse((self.root / 'state').exists())
        with self.assertRaises(FileNotFoundError):
            lf.names(self.root / 'missing')

    def test_segment_problems_and_cached_hash_failure(self):
        segment, index = self.segment('a', [('L-builder-1.jsonl', b'one\n')])
        self.assertEqual(lf.problems(self.events), [])
        self.assertEqual(lf.verify_segments(self.events, self.root / 'clean-state'), [])
        segment.write_bytes(b'two\n')  # same length and boundary, structurally valid
        self.assertEqual(lf.problems(self.events), [])
        state = self.root / 'state'
        expected = [(str(segment), 'sha256 mismatch')]
        self.assertEqual(lf.verify_segments(self.events, state), expected)
        with patch.object(lf.hashlib, 'sha256', side_effect=AssertionError('cached hash re-read')):
            self.assertEqual(lf.verify_segments(self.events, state), expected)
        cache = json.loads((state / 'ledger-segments-verified.json').read_text())
        self.assertIs(cache[index.name]['ok'], False)
        # A new index mtime invalidates the hash cache.
        segment.write_bytes(b'one\n')
        stamp = index.stat().st_mtime_ns + 10_000_000
        os.utime(index, ns=(stamp, stamp))
        self.assertEqual(lf.verify_segments(self.events, state), [])
        segment.unlink()
        self.assertIn('segment file missing', lf.problems(self.events)[0][1])
        orphan = index.parent / 'orphan.jsonl'
        orphan.write_bytes(b'')
        old = time.time() - 3601
        os.utime(orphan, (old, old))
        pending = index.parent / '.pending'
        pending.mkdir()
        (pending / 'L-builder-unknown.jsonl').write_bytes(b'')
        issues = lf.problems(self.events)
        self.assertEqual(len(issues), 3)
        self.assertTrue(any('orphan' in reason for _, reason in issues))
        self.assertTrue(any('pending' in reason for _, reason in issues))
        import health_alarms
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for issues in (expected, *([issue] for issue in lf.problems(self.events))):
            alarms = health_alarms.evaluate([], now, probes={'ledger_segments': issues})
            alarm = next(a for a in alarms if a['alarm'] == 'ledger-segment-invalid')
            self.assertEqual(alarm['severity'], 'immediate')
            self.assertEqual(alarm['reading']['count'], len(issues))
            self.assertTrue(health_alarms._plan(alarm, [], now)[0])
        for directory in (self.root / 'absent', self.root / 'empty'):
            issues = lf.problems(directory) + lf.verify_segments(directory, state)
            self.assertFalse(any(a['alarm'] == 'ledger-segment-invalid' for a in
                health_alarms.evaluate([], now, probes={'ledger_segments': issues})))
        print('L-spec-1787 AC5 ok')

    def test_assert_readable_opens_without_reading(self):
        segment, index = self.segment('a', [('L-builder-1.jsonl', b'one\n')])
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('read content')), \
             patch.object(Path, 'read_text', side_effect=AssertionError('read content')):
            lf.assert_readable(self.events)
        for path in (segment, index, index.parent, self.events):
            mode = path.stat().st_mode
            try:
                path.chmod(0)
                with self.assertRaises(PermissionError):
                    lf.assert_readable(self.events)
            finally:
                path.chmod(mode)
        with self.assertRaises(FileNotFoundError):
            lf.assert_readable(self.root / 'missing')

    def test_live_read_text_preserves_unreadable_file_failure(self):
        name = 'L-builder-9.jsonl'
        (self.events / name).write_text('{}\n')
        with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable fixture')):
            with self.assertRaises(PermissionError):
                lf.read_text(self.events, name)

    def fixture(self):
        return ('L-builder-0042.jsonl', b'{"v":1,"type":"spawn-done","spawn":"L-builder-0042","subject":"L-spec-0042","ts":"2026-10-28T00:00:00Z"}\n')

    def test_ac2_readers_cache_and_v3_rebuild(self):
        import fold, ledger_cache, sqlite3
        from contextlib import closing
        name, data = self.fixture()
        live = self.root / 'live' / 'events'
        live.mkdir(parents=True)
        (live / name).write_bytes(data)
        self.segment('a', [(name, data)])
        with patch.multiple(fold, ROOT=self.root, EVENTS=live, PROJECT=None):
            expected = fold._read_events_uncached()
        for directory in (live, self.events):
            with patch.multiple(fold, ROOT=self.root, EVENTS=directory, PROJECT=None):
                self.assertEqual(expected, fold._read_events_uncached())
                self.assertEqual(expected, fold.read_events())
                self.assertEqual(expected, fold.read_events())
        with patch.multiple(fold, ROOT=self.root, EVENTS=self.events, PROJECT=None):
            # A mixed live prefix is deduplicated, then fully re-read on append.
            (self.events / name).write_bytes(data)
            self.assertEqual(expected, fold.read_events())
            with (self.events / name).open('ab') as stream:
                stream.write(data.replace(b'0042', b'0043'))
            self.assertEqual(fold._read_events_uncached(), fold.read_events())
            self.assertEqual(fold.read_events()[-1]['_src'], name + ':2')
            for dbpath in (self.root / 'cache').glob('*.sqlite3'):
                with closing(sqlite3.connect(dbpath)) as db:
                    db.execute('PRAGMA user_version=3')
            self.assertEqual(fold._read_events_uncached(), fold.read_events())
            versions = []
            for dbpath in (self.root / 'cache').glob('*.sqlite3'):
                with closing(sqlite3.connect(dbpath)) as db:
                    versions.append(db.execute('PRAGMA user_version').fetchone()[0])
            self.assertIn(4, versions)
        self.assertEqual(ledger_cache.VERSION, 4)
        print('L-spec-1787 AC2 ok')

    def test_ac3_adopted_readers(self):
        import fold, carry, dispatch, autodispatch, tick, owed_digest, window_gate
        import realm, grading_env, realm_omens, realm_ledger
        from datetime import datetime, timezone
        name, data = self.fixture()
        ask = dict(type='realm-ask', panel=next(iter(realm.ASK_PANELS)), subject='L-spec-0042', ts='2026-10-28T00:00:00Z')
        deploy = dict(type='deploy-started', subject='albert-scott', ts='2026-10-28T00:00:00Z')
        data += (json.dumps(ask) + '\n' + json.dumps(deploy) + '\n').encode()
        live = self.root / 'live' / 'events'
        live.mkdir(parents=True)
        (live / name).write_bytes(data)
        self.segment('a', [(name, data)])
        results = []
        for directory in (live, self.events):
            with patch.multiple(fold, ROOT=self.root, EVENTS=directory, PROJECT=None), \
                 patch.multiple(dispatch, EVENTS=directory), patch.multiple(realm, EVENTS=directory), \
                 patch.object(carry, 'root', return_value=self.root), \
                 patch.object(grading_env, 'ROOT', directory.parent):
                # carry and grading_env bind their events directory through root.
                with patch.object(carry, 'root', return_value=directory.parent):
                    carry_rows = carry.scan_events(lambda e: True)
                realm._REF_CACHE.clear()
                results.append((fold._read_events_uncached(), dispatch.subject_ids('L-spec-'),
                    autodispatch._own_terminal_event([], 'L-builder-0042'), tick._ended_since_read('L-builder-0042'),
                    owed_digest._read_ledger(use_cache=False), window_gate.train_events(), realm.asks_state(),
                    realm._ref_subject(name + ':2'), realm_omens.scan(directory, {}), carry_rows,
                    grading_env.release_in_progress(datetime(2026, 10, 28, 0, 1, tzinfo=timezone.utc))))
                ledger = realm_ledger.Ledger()
                self.addCleanup(lambda ledger=ledger: ledger.watch.close() if ledger.watch else None)
                ledger.refresh(fold, directory, datetime(2026, 10, 28, tzinfo=timezone.utc))
                self.assertEqual(ledger.raw_events, fold._read_events_uncached())
                self.assertTrue(lf.exists(directory, name))
                lf.assert_readable(directory)
        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0][-1])
        self.assertTrue(results[0][-3])
        self.assertEqual(results[0][7], 'L-spec-0042')
        with patch.object(dispatch, 'EVENTS', self.events):
            self.assertEqual(dispatch.alloc(self.events, 'L-builder-', '.jsonl').name, 'L-builder-0043.jsonl')
        self.segment('bad', [('bad.jsonl', b'[]\n')])
        with patch.object(fold, 'EVENTS', self.events), self.assertRaisesRegex(OSError, 'train ledger unreadable'):
            window_gate.train_events()
        print('L-spec-1787 AC3 ok')


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        _ROOT.cleanup()
