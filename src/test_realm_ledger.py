"""Realm shares one isolated fold and handles torn tails without retaining bytes."""
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
READS = FOLDS = 0
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

    def test_shared_fold_deadline_refresh_and_replacement(self):
        path, modules = sys.path[:], sys.modules.copy()
        first = realm.lane_payload(NOW)
        second = realm.lane_payload(NOW+dt.timedelta(seconds=30))
        self.assertIsNone(first['lane_error']); self.assertIsNone(second['lane_error'])
        self.assertEqual((self.ledger.loads,self.ledger.reads,self.ledger.folds),(1,1,1))
        self.assertNotEqual(first['lane'][0]['now'],second['lane'][0]['now'])
        realm.lane_payload(NOW+dt.timedelta(minutes=1))
        self.assertEqual((self.ledger.reads,self.ledger.folds),(1,2))
        with self.file.open('a') as stream:
            stream.write(self.file.read_text().splitlines()[0]+'\n')
        third = realm.lane_payload(NOW+dt.timedelta(minutes=1))
        self.assertEqual(third['lane'][0]['events'],2)
        self.assertEqual((self.ledger.loads,self.ledger.reads,self.ledger.folds),(1,2,3))
        self.file.write_text(self.file.read_text().splitlines()[0]+'\n')
        self.assertEqual(realm.lane_payload(NOW)['lane'][0]['events'],1)
        self.assertEqual(sys.path,path); self.assertEqual(sys.modules,modules)
        # Import bundle stays bounded; every lazy sibling is reused.
        names = set(self.ledger.modules)
        for _ in range(15):
            self.assertIsNone(realm.lane_payload(NOW)['lane_error'])
        self.assertEqual(set(self.ledger.modules),names)
        self.assertEqual(self.ledger.loads,1)

    def test_threads_share_one_snapshot_and_import_registry(self):
        errors=[]
        def read():
            value=realm.lane_payload(NOW)
            if value['lane_error']: errors.append(value)
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
            third=realm.codex_payload()
            self.assertEqual(project.call_count,2)
            self.assertEqual(third['sessions'][0]['id'],'L-builder-01')
            third['sessions'][0]['state']='caller mutation'
            self.assertEqual(realm.codex_payload()['sessions'][0]['state'],'live')


if __name__=='__main__': unittest.main()
