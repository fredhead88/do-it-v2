"""Cached/reference byte equivalence, incremental parsing, and shared cache safety."""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

# Set before importing fold; tests never inspect the operator's root.
ROOT = Path(tempfile.mkdtemp(prefix='ledger-cache-test-'))
os.environ['DOIT_ROOT'] = str(ROOT)
os.environ['V4_LEDGER_DIR'] = str(ROOT / 'v4-ledger')
os.environ['V4_INBOX_DIR'] = str(ROOT / 'v4-inbox')
import fold
import ledger_cache
import ledger_compact
import brief


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.patch = patch.multiple(fold, ROOT=self.root, EVENTS=self.events, SESSIONS=self.root / 'sessions', PROJECT=None)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, rows, mode='w'):
        with (self.events / name).open(mode) as stream:
            stream.write(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in rows))

    def same(self):
        expected = fold.read_events(use_cache=False)
        applied = json.dumps(fold.APPLIED)
        actual = fold.read_events()
        self.assertEqual(json.dumps(actual, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(json.dumps(fold.APPLIED), applied)
        return actual

    def test_projection_cached_equals_reference_and_stored_bytes(self):
        a, b = 'L-executor-01.jsonl', 'L-tick-01.jsonl'
        old = dict(ts='2026-01-01', type='tree-reaped', subject='L-charter-01',
                   retained=['é' * 100] * 400, retained_reason=['reason' * 100] * 400,
                   actor='forged', _src='forged')
        self.write(a, [old, dict(old, ts='2026-01-02', retained_count=7)])
        self.write(b, [dict(ts='2026-01-03', type='tick', subject='tick', recorded_reap=['old']),
                       dict(ts='2026-01-04', type='observed', subject='ordinary')])

        def check():
            events = self.same()
            for e in events:
                if e['type'] == 'tree-reaped':
                    self.assertNotIn('retained', e)
                    self.assertNotIn('retained_reason', e)
                    self.assertIsInstance(e['retained_count'], int)
                    self.assertEqual(e['actor'], 'executor')
                    self.assertNotEqual(e['_src'], 'forged')
                if e['type'] == 'tick':
                    self.assertNotIn('recorded_reap', e)
            cache = next((self.root / 'cache').glob('*.sqlite3'))
            with closing(sqlite3.connect(cache)) as db, db:
                stored = []
                for data, digest in db.execute('SELECT data,sha FROM chunks ORDER BY name,seq'):
                    self.assertNotIn(b'retained_reason', data)
                    stored.extend(ledger_cache._unpack(data, digest))
                for data, digest in db.execute('SELECT tail,tail_sha FROM files ORDER BY name'):
                    self.assertNotIn(b'retained_reason', data)
                    stored.extend(ledger_cache._unpack(data, digest))
            self.assertEqual(sorted(stored, key=lambda e: e['_src']),
                             sorted(events, key=lambda e: e['_src']))
            return cache

        cache = check()
        # New-shape records without a retained list pass through unchanged.
        self.write(a, [dict(ts='2026-01-05', type='tree-reaped', subject='L-charter-01',
                            retained_count=3)], 'a')
        check()
        # Repair an old byte and grow on the same inode.
        with (self.events / a).open('r+b') as stream:
            data = stream.read().replace(b'2026-01-01', b'2026-01-06', 1)
            stream.seek(0)
            stream.write(data + (json.dumps(old) + '\n').encode())
        check()
        data, digest = ledger_cache._pack([old])
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute('PRAGMA user_version=2')
            db.execute('UPDATE chunks SET data=?,sha=?', (data, digest))
        check()
        with closing(sqlite3.connect(cache)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_folded_state_unchanged_by_projection(self):
        cid, sid = 'L-charter-9901', 'L-spec-9901'
        def event(kind, day, subject=cid, **fields):
            return dict(type=kind, ts=f'2026-01-{day:02}T00:00:00+00:00', subject=subject, **fields)
        self.write('L-operator-01.jsonl', [event('charter-filed', 1, title='Fixture')])
        self.write('L-planner-01.jsonl', [event('cut-written', 2), event('l1-complete', 3)])
        self.write('L-spec-writer-01.jsonl', [event('spec-written', 4, sid, charter=cid)])
        self.write('L-builder-01.jsonl', [event('build-started', 5, sid), event('build-done', 6, sid)])
        self.write('L-grader-01.jsonl', [event('verdict', 7, sid, confirmed=True)])
        self.write('L-reviewer-01.jsonl', [event('review', 8, sid, depth='gates-only')])
        self.write('L-executor-01.jsonl', [event('shipped', 9, sid),
            event('sweep-fixpoint', 10),
            event('tree-reaped', 11, retained=['branch'], retained_reason=['branch: kept'])])
        self.write('L-charter-reviewer-01.jsonl', [event('charter-review-complete', 10)])
        self.write('L-tick-01.jsonl', [event('tick', 12, recorded_reap=['old']),
                                     event('charter-closed', 13, reason='done')])

        def projected_state(state):
            # Normalize only embedded event dictionaries, never derived fields.
            specs, charters, ignored, by_subject = copy.deepcopy(state)
            for row in list(specs.values()) + list(charters.values()):
                row['evs'] = [ledger_compact.compact(e) for e in row['evs']]
            ignored = [ledger_compact.compact(e) for e in ignored]
            by_subject = {k: [ledger_compact.compact(e) for e in evs] for k, evs in by_subject.items()}
            return json.dumps((specs, charters, ignored, by_subject), default=str, sort_keys=True)

        with patch.object(fold, 'NOW', fold.ts('2026-01-14T00:00:00+00:00')):
            projected = self.same()
            projected_fold = fold.fold(projected)
            board = fold.render(projected, *projected_fold)
            with patch.object(ledger_compact, 'PROJECTION', {}):
                # A separate cache forces both parsers to actually see the table.
                with patch.object(fold, 'ROOT', self.root / 'unprojected'):
                    raw = self.same()
                raw_fold = fold.fold(raw)
                raw_board = fold.render(raw, *raw_fold)
            self.assertEqual(projected_state(projected_fold), projected_state(raw_fold))
            self.assertEqual(board, raw_board)
            self.assertEqual(projected_fold[0][sid]['state'], 'accepted')
            self.assertEqual(projected_fold[1][cid]['state'], 'L2-complete')
            self.assertEqual(projected_fold[1][cid]['owed'], 0)
            self.assertIn('tree-reaped', dict(fold.CHARTER_STAGE_EVENTS))
            self.assertEqual(fold.charter_stage_clock(projected, cid, projected_fold[0]),
                             fold.charter_stage_clock(raw, cid, raw_fold[0]))
            self.assertEqual(brief._newest(projected, 'tree-reaped')['ts'],
                             brief._newest(raw, 'tree-reaped')['ts'])
            self.assertTrue(any('retained' in e for e in raw))

    def test_projected_read_memory_scales_with_kept_bytes(self):
        rows = [dict(ts=f'{i:04}', type='tree-reaped', subject='L-charter-01',
                     retained=['a' * 1024] * 40, retained_reason=['b' * 1024] * 40)
                for i in range(300)]
        removed_size = sum(len(json.dumps({k: e[k] for k in ('retained', 'retained_reason')})) for e in rows)
        self.write('L-executor-01.jsonl', rows)
        del rows
        fold.read_events()

        def peak():
            tracemalloc.start()
            try:
                events = fold.read_events()
                high = tracemalloc.get_traced_memory()[1]
                self.assertEqual(len(events), 300)
                return high
            finally:
                tracemalloc.stop()

        self.assertLess(peak(), removed_size * .25)
        cache = next((self.root / 'cache').glob('*.sqlite3'))
        self.assertLess(cache.stat().st_size, (self.events / 'L-executor-01.jsonl').stat().st_size * .05)
        with patch.object(ledger_compact, 'PROJECTION', {}), patch.object(fold, 'ROOT', self.root / 'control'):
            fold.read_events()
            self.assertGreater(peak(), removed_size * .25)

    def test_append_streams_prefix_and_still_detects_rewrite(self):
        path = self.events / 'L-executor-01.jsonl'
        self.write(path.name, [dict(ts='1', type='tree-reaped', subject='L-charter-01',
                                    retained=['x' * 1024] * (16 * 1024))])
        old = ledger_cache._file(path, 'executor', None)
        fold.read_events()
        self.write(path.name, [dict(ts='2', type='observed', subject='next')], 'a')
        real_file = ledger_cache._file
        peaks = []
        def measured(*args):
            tracemalloc.start()
            try:
                result = real_file(*args)
                peaks.append(tracemalloc.get_traced_memory()[1])
                self.assertTrue(result['append'])
                return result
            finally:
                tracemalloc.stop()
        with patch.object(ledger_cache, 'PREFIX_CHUNK', 64 * 1024), \
             patch.object(ledger_cache, '_parse', wraps=ledger_cache._parse) as parse, \
             patch.object(ledger_cache, '_file', side_effect=measured):
            actual = fold.read_events()
            self.assertEqual(parse.call_count, 2)
        self.assertEqual(len(peaks), 1)
        self.assertLess(peaks[0], 4 * 1024 * 1024)
        self.assertEqual(json.dumps(actual, ensure_ascii=False),
                         json.dumps(fold.read_events(use_cache=False), ensure_ascii=False))
        # The changed byte is deep inside the old prefix, away from the tail.
        with path.open('r+b') as stream:
            stream.seek(old['meta'][2] // 2)
            self.assertEqual(stream.read(1), b'x')
            stream.seek(-1, 1)
            stream.write(b'y')
        self.write(path.name, [dict(ts='3', type='observed', subject='repair')], 'a')
        repaired = real_file(path, 'executor', old)
        self.assertFalse(repaired['append'])
        self.assertEqual(repaired['events'] + repaired['tail'], fold._read_events_uncached())
        self.assertEqual(repaired['digest'], hashlib.sha256(path.read_bytes()).digest())
        self.same()
        st = path.stat()
        changed = type('ChangedStat', (), dict(st_dev=st.st_dev, st_ino=st.st_ino,
            st_size=st.st_size + 1, st_mtime_ns=st.st_mtime_ns, st_ctime_ns=st.st_ctime_ns))()
        with patch.object(Path, 'stat', side_effect=[st, changed]):
            with self.assertRaisesRegex(OSError, 'ledger changed during cache read'):
                real_file(path, 'executor', repaired)

    def test_reference_equivalence_through_repairs_and_tails(self):
        a, b = 'L-builder-01.jsonl', 'L-executor-01.jsonl'
        def event(ty, **kw):
            return dict(ts='2026-01-01', type=ty, subject='L-spec-01', project='wrong', **kw)
        self.write(a, [event('build-started'), event('build-done', detail={'x': ['é', 1, None]})])
        self.write(b, [event('deploy-refused'), event('deploy-started')])
        self.write('L-operator-01.jsonl', [dict(ts='2026-01-02', type='correction', subject='fix',
                    ref=a + ':1', set={'project': 'right', 'actor': 'forged', '_src': 'forged'})])
        self.same(); self.same()
        with patch.object(fold, 'PROJECT', 'right'):
            self.assertEqual([e['actor'] for e in self.same()], ['builder'])
        # An old timestamp append still goes through the original global merge.
        self.write(a, [dict(event('build-blocked'), ts='2025-12-31')], 'a')
        self.same()
        # Rewrite and GROW the same inode: a metadata-only suffix cache misses this.
        self.write(a, [event('different', data='changed prefix' * 30)])
        self.same()
        self.write(a, [event('short')])  # truncate
        self.same()
        replacement = self.events / 'replacement'
        replacement.write_text((self.events / a).read_text().replace('short', 'other'))
        replacement.replace(self.events / a)
        self.same()
        (self.events / b).unlink()
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('\ninvalid\r\n' + json.dumps(event('valid-tail')))
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('garbage')  # previously valid unterminated row becomes invalid
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('\n{"ts":"broken')
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('","type":"completed","subject":"tail"}\n')
        self.same()
        cache = next((self.root / 'cache').glob('*.sqlite3'))
        cache.write_bytes(b'corrupt cache')
        self.same()
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute("UPDATE chunks SET data=? WHERE rowid=(SELECT min(rowid) FROM chunks)", (b'broken chunk',))
        self.same()

    def test_only_suffix_parsed_and_caller_mutation_not_saved(self):
        name = 'L-operator-01.jsonl'
        self.write(name, [dict(ts='1', type='one', subject='test')])
        first = self.same()
        first[0]['type'] = 'caller mutation'
        with patch.object(ledger_cache, '_parse', wraps=ledger_cache._parse) as parse:
            self.same()
            self.assertEqual(parse.call_count, 0)
            self.write(name, [dict(ts='2', type='two', subject='test')], 'a')
            self.same()
            self.assertEqual(parse.call_count, 2)  # complete suffix + empty tail
            self.assertNotIn(b'"one"', b''.join(c.args[0] for c in parse.call_args_list))

    def test_append_persists_only_the_new_chunk_and_idle_read_does_not_write(self):
        name='L-operator-01.jsonl'
        self.write(name,[dict(ts=str(i),type='row',subject='x') for i in range(700)])
        self.same()
        path=next((self.root/'cache').glob('*.sqlite3'))
        with sqlite3.connect(path) as db:
            before=db.execute('SELECT seq,data,sha FROM chunks ORDER BY seq').fetchall()
        stamp=path.stat().st_mtime_ns
        self.same()
        self.assertEqual(path.stat().st_mtime_ns,stamp)
        self.write(name,[dict(ts='new',type='new',subject='x')],'a')
        with patch.object(ledger_cache,'_pack',wraps=ledger_cache._pack) as pack:
            self.same()
            self.assertTrue(pack.call_args_list)
            self.assertLessEqual(max(len(c.args[0]) for c in pack.call_args_list),1)
        with sqlite3.connect(path) as db:
            after=db.execute('SELECT seq,data,sha FROM chunks ORDER BY seq').fetchall()
        self.assertEqual(after[:-1],before)

    def test_heap_merge_matches_legacy_k_way_order(self):
        import random
        def legacy(items):
            queues = {}
            for event in items:
                queues.setdefault(fold._src_file(event), []).append(event)
            out = []
            while queues:
                name = min(queues, key=lambda k: fold.event_content_key(queues[k][0]))
                out.append(queues[name].pop(0))
                if not queues[name]: del queues[name]
            return out
        rng = random.Random(35)
        for _ in range(300):
            items = [dict(ts='same', type=rng.choice(['a','b','c']), subject=rng.choice(['x','y']),
                          nested={'v': rng.randrange(3)}, _src=f'{actor}.jsonl:{n}')
                     for actor in range(rng.randrange(1, 12)) for n in range(rng.randrange(1, 15))]
            self.assertEqual(fold._merge_same_ts(items), legacy(items))

    def test_malformed_schema_and_executable_pickle_rebuild(self):
        self.write('L-operator-01.jsonl', [dict(ts='1', type='test')])
        self.same()
        path = next((self.root/'cache').glob('*.sqlite3'))
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE files')
            db.execute('CREATE TABLE files (bad TEXT)')
        self.same()
        # A valid checksum is insufficient to authorize importing a class.
        data, digest = ledger_cache._pack(os.system)
        with sqlite3.connect(path) as db:
            db.execute('UPDATE chunks SET data=?,sha=?', (data,digest))
        self.same()
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM chunks')
        self.same()
        with sqlite3.connect(path) as db:
            db.execute('UPDATE files SET offset=offset+1')
        self.same()

    def test_answered_keeps_ref_subject_strict_time_and_admitted_fixes(self):
        def event(ty, subject, stamp, src, **fields):
            return dict(type=ty, subject=subject, ts=f'2026-01-01T00:00:{stamp:02}+00:00',
                        _src=src, actor='operator', **fields)
        events = [event('question','a',1,'q1'), event('question','a',2,'q2'),
                  event('question','b',1,'q3'), event('question','c',1,'q4'),
                  event('decision','a',2,'d1'), event('decision','elsewhere',1,'d2',ref='q3'),
                  event('escalation-blocking','failed',1,'f1',kind='owed-failed',criteria=['AC1','AC2']),
                  event('owed-waived','failed',2,'w1',criterion='AC1',why='accepted'),
                  event('owed-waived','failed',3,'w2',criterion='AC2',why='accepted'),
                  event('owed-waived','c',4,'w3',criterion='AC1',why='different subject')]
        self.assertEqual(fold.answered(events),{'q1','q3','f1'})
        events[7]['actor']='builder'  # unauthorized waiver cannot close f1
        self.assertEqual(fold.answered(events),{'q1','q3'})

    def test_blank_crlf_and_unicode_line_numbers(self):
        (self.events / 'L-operator-01.jsonl').write_bytes(
            b'\r\n\n' + json.dumps(dict(type='first', subject='x', ts='1')).encode() + b'\r\n'
            + '\u2028\r'.encode() + json.dumps(dict(type='last', subject='x', ts='2')).encode())
        self.same()
        with (self.events / 'L-operator-01.jsonl').open('ab') as fh:
            fh.write(b'\r\n\n')
        self.same()

    def test_unwritable_cache_falls_back(self):
        self.write('L-operator-01.jsonl', [dict(ts='1', type='test')])
        with patch.object(ledger_cache, '_connect', side_effect=PermissionError('read-only')):
            self.same()

    def test_fresh_processes_and_concurrent_readers(self):
        self.write('L-operator-01.jsonl', [dict(ts='1', type='test')])
        env = {**os.environ, 'DOIT_ROOT': str(self.root), 'PYTHONPATH': str(Path(__file__).parent)}
        script = 'import fold,json; print(json.dumps(fold.read_events()))'
        procs = [subprocess.Popen([sys.executable, '-c', script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(4)]
        results = [p.communicate(timeout=30) for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), results)
        self.assertTrue(all(out == results[0][0] for out, err in results))
        self.assertEqual(json.loads(results[0][0]), self.same())


if __name__ == '__main__':
    unittest.main()
