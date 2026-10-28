"""Realm shares one isolated fold and handles torn tails without retaining bytes."""
import builtins
import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ['DOIT_ROOT'] = tempfile.mkdtemp(prefix='realm-ledger-tests-')
import realm
import realm_ledger

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.timezone.utc)


class RealmLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root/'src'; self.src.mkdir()
        self.events = self.root/'events'; self.events.mkdir()
        self.file = self.events/'L-executor-test.jsonl'
        self.file.write_text('{"ts":"2026-10-20T15:00:00+00:00","type":"tick","subject":"fixture","actor":"executor","_src":"L-executor-test.jsonl:1"}\n')
        (self.src/'fold.py').write_text('''import json, os
from pathlib import Path
from dataclasses import dataclass
@dataclass
class Marker:
    value: str = 'private'
READS = FOLDS = FINISHES = 0
import ledger_compact
def actor_of(name):
    parts = Path(name).stem.split('-')
    return '-'.join(parts[1:-1]) if len(parts) >= 3 else Path(name).stem
def finish_events(raw):
    global FINISHES
    FINISHES += 1
    return [dict(e) for e in raw if not os.environ.get('DOIT_PROJECT') or e.get('project') == os.environ['DOIT_PROJECT']]

def read_events():
    global READS
    READS += 1
    return [json.loads(line) for line in (Path(os.environ['DOIT_ROOT'])/'events/L-executor-test.jsonl').read_text().splitlines()]
def fold(events):
    global FOLDS
    FOLDS += 1
    return {}, {}, [], {'fixture': events}
def stall_lane_rows(events, specs, now, *, by_subject):
    assert by_subject['fixture'] is events
    return [{'now': now.isoformat(), 'events': len(events)}]
''')
        (self.src/'lane_rules.py').write_text('def load(): return {}\n')
        self.ledger = realm_ledger.Ledger()
        patcher = patch.multiple(realm, LEDGER=self.ledger, ROOT=self.root, EVENTS=self.events)
        patcher.start(); self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_LIVE_REPO=str(self.root), HOME=str(self.root/'home'))
        env.start(); self.addCleanup(env.stop)

    def test_flow_cache_reuse_refresh_and_error(self):
        import subprocess
        good=json.dumps({'counts': {}, 'steps': [{'stage':'building','state':'hold','item':'fixture'}]})
        with patch.multiple(realm, LIVE_REPO=self.root), patch.dict(realm.VIEW_CACHE, clear=True), patch.object(realm.subprocess,'run',return_value=subprocess.CompletedProcess([],0,good,'')) as run:
            first=realm._view_payload('next',NOW)
            self.assertIsNone(first['flow_error'])
            self.assertEqual(realm._view_payload('next',NOW+dt.timedelta(seconds=29)),first)
            self.assertEqual(run.call_count,1)
            realm._view_payload('next',NOW+dt.timedelta(seconds=30))
            self.assertEqual(run.call_count,2)
            run.return_value=subprocess.CompletedProcess([],2,'','')
            bad=realm._view_payload('next',NOW+dt.timedelta(seconds=60))
            self.assertIsNone(bad['flow']);self.assertIsNone(bad['steps']);self.assertTrue(bad['flow_error'])
            run.return_value=subprocess.CompletedProcess([],0,good,'')
            self.assertIsNotNone(realm._view_payload('next',NOW+dt.timedelta(seconds=90))['flow'])

    def snapshot(self, now):
        with realm.ledger_snapshot(now) as (_, events, folded):
            return {'rows': [{'now': now.isoformat(), 'events': len(events)}], 'error': None}

    def test_shared_fold_deadline_refresh_and_replacement(self):
        path, modules = sys.path[:], sys.modules.copy()
        first = self.snapshot(NOW)
        second = self.snapshot(NOW+dt.timedelta(seconds=30))
        self.assertIsNone(first['error']); self.assertIsNone(second['error'])
        self.assertEqual((self.ledger.loads,self.ledger.reads,self.ledger.folds),(1,1,1))
        self.assertNotEqual(first['rows'][0]['now'],second['rows'][0]['now'])
        self.snapshot(NOW+dt.timedelta(minutes=1))
        self.assertEqual((self.ledger.reads,self.ledger.folds),(1,2))
        with self.file.open('a') as stream:
            stream.write(self.file.read_text().splitlines()[0]+'\n')
        third = self.snapshot(NOW+dt.timedelta(seconds=70))
        self.assertEqual(third['rows'][0]['events'],2)
        self.assertEqual((self.ledger.loads,self.ledger.reads,self.ledger.folds),(1,2,3))
        self.file.write_text(self.file.read_text().splitlines()[0]+'\n')
        self.assertEqual(self.snapshot(NOW+dt.timedelta(seconds=80))['rows'][0]['events'],1)
        self.assertEqual(sys.path,path); self.assertEqual(sys.modules,modules)
        # Import bundle stays bounded; every lazy sibling is reused.
        names = set(self.ledger.modules)
        for _ in range(15):
            self.assertIsNone(self.snapshot(NOW)['error'])
        self.assertEqual(set(self.ledger.modules),names)
        self.assertEqual(self.ledger.loads,1)

    def test_threads_share_one_snapshot_and_import_registry(self):
        errors=[]
        def read():
            value=self.snapshot(NOW)
            if value['error']: errors.append(value)
        threads=[threading.Thread(target=read) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads)); self.assertEqual(errors,[])
        self.assertEqual((self.ledger.loads,self.ledger.reads,self.ledger.folds),(1,1,1))

    def test_read_all_uses_references_and_does_not_mutate_shared_rows(self):
        rows, raw, offsets=realm.read_all()
        rows[0]['cl']='caller mutation'
        self.assertNotIn('cl',realm.read_all()[0][0])
        self.assertEqual(offsets[self.file.name],self.file.stat().st_size)
        self.assertEqual(self.ledger.reads,1)

    def test_torn_tail_is_read_once_when_completed(self):
        data=self.file.read_bytes()
        self.file.write_bytes(data+b'{"type":"tick"')
        for _ in range(50):
            lines,offset=realm.tail_lines(self.file,len(data))
            self.assertEqual(lines,[]); self.assertEqual(offset,len(data))
        with self.file.open('ab') as stream: stream.write(b'}\n')
        lines,offset=realm.tail_lines(self.file,len(data))
        self.assertEqual(lines,[b'{"type":"tick"}'])
        self.assertEqual(offset,self.file.stat().st_size)

    def test_stalled_stream_is_disconnected_and_can_reconnect(self):
        hub = realm.Hub()
        q = hub.add()
        for i in range(65): hub.send('ledger', {'t':str(i)})
        self.assertNotIn(q, hub.clients)
        self.assertIsNone(q.get_nowait())
        self.assertEqual(len(hub.recent),65)

    def test_directory_notifications_and_fallback(self):
        watch=realm_ledger.Changes(self.events)
        self.addCleanup(watch.close)
        if watch.fd is None:
            self.assertIsNone(watch.poll())
            return
        self.assertEqual(watch.poll(),set())
        with self.file.open('a') as stream: stream.write('\n')
        self.assertIn(self.file.name,watch.poll())
        self.assertEqual(watch.poll(),set())
        moved=self.events/'L-executor-moved.jsonl'
        self.file.replace(moved)
        self.assertEqual(watch.poll(),{self.file.name,moved.name})
        moved.unlink()
        self.assertIn(moved.name,watch.poll())
        watch.close()
        self.assertIsNone(watch.poll())

    def test_codex_projection_reused_without_copying_compact_rows(self):
        with patch.object(realm.realm_codex,'ledger_sessions',wraps=realm.realm_codex.ledger_sessions) as project, \
             patch.object(realm.realm_codex,'process_sessions',return_value=set()), \
             patch.object(realm.realm_codex,'tmux_windows',return_value={}):
            first=realm.codex_payload()
            second=realm.codex_payload()
            self.assertEqual(first,second)
            self.assertEqual(project.call_count,1)
            self.assertEqual(self.ledger.reads,1)
            self.assertIsNone(self.ledger.rows)  # no read_all / full row copies
            with self.file.open('a') as stream:
                stream.write(json.dumps(dict(type='spawn-started',ts=NOW.isoformat(),subject='L-spec-1',
                    actor='executor',_src=self.file.name+':2',spawn='L-builder-01',backend='codex'))+'\n')
            with patch.object(realm.dt, 'datetime', wraps=dt.datetime) as clock:
                clock.now.return_value = dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=11)
                third=realm.codex_payload()
            self.assertEqual(project.call_count,2)
            self.assertEqual(third['sessions'][0]['id'],'L-builder-01')
            third['sessions'][0]['state']='caller mutation'
            self.assertEqual(realm.codex_payload()['sessions'][0]['state'],'live')


    @contextlib.contextmanager
    def counted_reads(self):
        counts = {}
        original_open, path_open = builtins.open, Path.open
        class Counted:
            def __init__(self, file, name): self.file, self.name = file, name
            def __getattr__(self, name): return getattr(self.file, name)
            def __enter__(self): return self
            def __exit__(self, *args): return self.file.__exit__(*args)
            def read(self, *args):
                data = self.file.read(*args)
                counts[self.name] = counts.get(self.name, 0) + len(data)
                return data
        def wrap(file, name):
            return Counted(file, Path(name).name) if Path(name).parent == self.events else file
        def opened(name, *args, **kwargs): return wrap(original_open(name, *args, **kwargs), name)
        def path_opened(name, *args, **kwargs): return wrap(path_open(name, *args, **kwargs), name)
        with patch('builtins.open', opened), patch.object(Path, 'open', path_opened):
            yield counts

    def live_refresh(self, seconds=0):
        with self.ledger.live(self.src, self.root) as fold:
            return self.ledger.refresh(fold, self.events, NOW+dt.timedelta(seconds=seconds))

    def test_finish_events_matches_read_events(self):
        live_src = Path(realm.__file__).parent
        stamp = NOW.isoformat()
        for name, subjects in [('L-builder-a.jsonl', ['z', 'a']),
                               ('L-thinker-b.jsonl', ['m']), ('L-grader-c.jsonl', ['b']),
                               ('L-builder-colon:x.jsonl', ['z', 'a']), ('L-builder-colon:y.jsonl', ['m'])]:
            (self.events/name).write_text(''.join(json.dumps(dict(ts=stamp, type='tick', subject=s,
                                                     project='old'))+'\n' for s in subjects))
        self.file.unlink()
        (self.events/'L-operator-fix.jsonl').write_text(''.join(json.dumps(dict(ts=stamp,
            type='correction', ref=ref, set={'project':'corrected'}))+'\n'
            for ref in ['L-builder-a.jsonl:1', 'missing.jsonl:99']))
        with self.ledger.live(live_src, self.root) as fold:
            raw = fold._read_events_uncached()
            before = copy.deepcopy(raw)
            for cache in ['0', '1']:
                for project in [None, 'corrected']:
                    with patch.dict(os.environ, DOIT_READ_CACHE=cache), patch.object(fold, 'PROJECT', project):
                        finished = fold.finish_events(raw)
                        self.assertEqual(finished, fold.read_events())
                        self.assertEqual(finished, fold.finish_events(raw))
                        self.assertEqual(finished, fold.finish_events(list(reversed(raw))))
                        self.assertEqual(raw, before)

    def test_incremental_reader_matches_reference_parser(self):
        live_src = Path(realm.__file__).parent
        self.file.write_bytes(b'\nnot json\n{"type":"tick","detail":"raw '+ '\u2028'.encode()+
                              b' separator"}\r\n{"type":"tick"')
        other = self.events/'L-builder-other.jsonl'
        other.write_text('{"type":"tick","subject":"other"}\n')
        with self.ledger.live(live_src, self.root) as fold, patch.object(fold, 'read_events', side_effect=AssertionError('cached read')):
            self.ledger.refresh(fold, self.events, NOW)
            self.assertEqual(self.ledger.raw_events, fold._read_events_uncached())
            with self.file.open('ab') as f: f.write(b',"subject":"completed"}\n')
            self.ledger.refresh(fold, self.events, NOW+dt.timedelta(seconds=1))
            self.assertEqual(self.ledger.raw_events, fold._read_events_uncached())
            completed = [e for e in self.ledger.raw_events if e.get('subject') == 'completed']
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]['_src'], self.file.name+':5')
            self.ledger.refresh(fold, self.events, NOW+dt.timedelta(seconds=2))
            self.assertEqual(self.ledger.raw_events, fold._read_events_uncached())
            with other.open('ab') as f: f.write(b'{"type":"tick","subject":"pending"}')
            self.ledger.refresh(fold, self.events, NOW+dt.timedelta(seconds=3))
            self.assertFalse(any(e.get('subject') == 'pending' for e in self.ledger.raw_events))
            with other.open('ab') as f: f.write(b'\n')
            self.ledger.refresh(fold, self.events, NOW+dt.timedelta(seconds=4))
            self.assertEqual(self.ledger.raw_events, fold._read_events_uncached())
            self.assertEqual(sum(e.get('subject') == 'pending' for e in self.ledger.raw_events), 1)

    def test_append_reads_only_new_bytes(self):
        data = b'{"type":"tick","subject":"fixture"}\n' * 1100
        other = self.events/'L-builder-other.jsonl'
        self.file.write_bytes(data); other.write_bytes(data)
        self.live_refresh()
        def append(data, seconds):
            with self.file.open('ab') as f: f.write(data)
            with self.counted_reads() as counts: self.live_refresh(seconds)
            self.assertLessEqual(counts.get(self.file.name, 0), len(data)+4096)
            self.assertNotIn(other.name, counts)
            self.assertEqual(counts.get(self.file.name), len(data))
        append(b'{"type":"tick"}\n'*3, 10)
        append(b'{"type":', 11); append(b'"tick"}\n', 12)
        replacement = self.root/'new'
        replacement.write_bytes(data*2); replacement.replace(self.file)
        with self.counted_reads() as counts: self.live_refresh(20)
        self.assertEqual(counts, {self.file.name: len(data)*2})
        inode = self.file.stat().st_ino
        self.file.write_bytes(data[:100])
        self.assertEqual(inode, self.file.stat().st_ino)
        with self.counted_reads() as counts: self.live_refresh(30)
        self.assertEqual(counts, {self.file.name: 100})
        os.utime(self.file, None)
        with self.counted_reads() as counts: self.live_refresh(40)
        self.assertEqual(counts, {})
        other.unlink()
        fresh = self.events/'L-grader-new.jsonl'
        fresh.write_bytes(b'{"type":"tick","subject":"new"}\n')
        with self.counted_reads() as counts: events, _ = self.live_refresh(50)
        self.assertEqual(counts, {fresh.name: fresh.stat().st_size})
        self.assertNotIn(other.name, self.ledger.file_offsets)
        self.assertEqual(events, sorted(events, key=lambda e: e.get('ts','')))
        snapshot, offsets, raw = self.ledger.events, self.ledger.file_offsets, self.ledger.raw_events
        with self.file.open('ab') as f: f.write(b'\n')
        original = Path.open
        def fail(path, *args, **kwargs):
            if path == self.file: raise PermissionError('fixture unreadable')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', fail), self.assertRaises(PermissionError): self.live_refresh(60)
        self.assertIs(self.ledger.events, snapshot)
        self.assertIs(self.ledger.file_offsets, offsets)
        self.assertIs(self.ledger.raw_events, raw)
        self.live_refresh(60)  # notifications consumed on the failed attempt must be retried
        with self.file.open('ab') as f: f.write(b'\xff\n')
        with self.assertRaises(UnicodeDecodeError): self.live_refresh(70)

    def test_refold_bounded_and_generation(self):
        events, folded = self.live_refresh()
        times = [0]
        generation = self.ledger.generation
        for second in range(1, 51):
            with self.file.open('a') as f: f.write('{"type":"tick","subject":"fixture"}\n')
            before = self.ledger.folds
            pair = self.live_refresh(second)
            if self.ledger.folds != before:
                times.append(second)
                self.assertNotEqual(self.ledger.generation, generation)
                generation = self.ledger.generation
                events, folded = pair
            else:
                self.assertIs(pair[0], events); self.assertIs(pair[1], folded)
                self.assertEqual(self.ledger.generation, generation)
        self.assertEqual(times, [0,10,20,30,40,50])
        self.live_refresh(60)
        self.assertEqual(self.ledger.folds, 7)
        self.assertNotEqual(self.ledger.generation, generation)
        generation = self.ledger.generation
        self.live_refresh(61); self.live_refresh(62)
        self.assertEqual(self.ledger.folds, 7); self.assertEqual(self.ledger.generation, generation)
        # A held pair stays consistent after a refresh in another thread.
        held = self.live_refresh(63)
        with self.file.open('a') as f: f.write('{"type":"tick","subject":"fixture"}\n')
        worker = threading.Thread(target=lambda: self.live_refresh(70))
        worker.start(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIs(held[1][3]['fixture'], held[0])
        self.assertEqual(len(held[0]), 51)
        # Clock and ledger changes use one timer even across minute boundaries.
        self.live_refresh(119)
        self.assertEqual(self.ledger.folds, 8)
        self.live_refresh(120)
        self.assertEqual(self.ledger.folds, 9)
        self.ledger.watch.close()
        self.ledger = realm_ledger.Ledger()
        self.live_refresh(59)
        generation = self.ledger.generation
        self.live_refresh(60)  # the minute changed only one second after bootstrap
        self.assertEqual(self.ledger.folds, 1)
        self.assertEqual(self.ledger.generation, generation)
        self.live_refresh(69)
        self.assertEqual(self.ledger.folds, 2)
        self.assertNotEqual(self.ledger.generation, generation)

    def test_code_change_keeps_raw_events(self):
        self.live_refresh()
        raw, offsets = self.ledger.raw_events, self.ledger.file_offsets
        generation = self.ledger.generation
        old_module = self.ledger.modules['fold']
        (self.src/'new_bundle.py').write_text('MARKER = 1\n')
        with self.counted_reads() as counts: self.live_refresh(1)
        self.assertEqual(counts, {})
        self.assertIs(self.ledger.raw_events, raw); self.assertIs(self.ledger.file_offsets, offsets)
        self.assertEqual(self.ledger.loads, 2); self.assertEqual(self.ledger.folds, 2)
        self.assertIsNot(self.ledger.modules['fold'], old_module)
        self.assertEqual(self.ledger.modules['fold'].FOLDS, 1)
        self.assertEqual(self.ledger.modules['fold'].FINISHES, 1)
        self.assertNotEqual(generation, self.ledger.generation)
        generation = self.ledger.generation
        with patch.dict(os.environ, DOIT_PROJECT='corrected'):
            with self.counted_reads() as counts: events, _ = self.live_refresh(2)
            self.assertEqual(counts, {}); self.assertEqual(events, [])
            self.assertEqual(self.ledger.folds, 3)
            self.assertEqual(self.ledger.modules['fold'].FINISHES, 1)
            self.assertIs(self.ledger.raw_events, raw); self.assertIs(self.ledger.file_offsets, offsets)
            self.assertNotEqual(generation, self.ledger.generation)
            with self.file.open('a') as f: f.write('{"type":"tick","project":"corrected"}\n')
            self.live_refresh(3); self.live_refresh(11)
            self.assertEqual(self.ledger.folds, 3)
            self.assertEqual(len(self.live_refresh(12)[0]), 1)
            self.assertEqual(self.ledger.folds, 4)
            other = self.root/'other-events'; other.mkdir()
            with self.ledger.live(self.src, self.root) as fold:
                self.assertEqual(self.ledger.refresh(fold, other, NOW+dt.timedelta(seconds=13))[0], [])
            self.assertEqual(self.ledger.raw_events, [])


if __name__=='__main__': unittest.main()
