#!/usr/bin/env python3
"""Fixture-root stall census, failure persistence and store regressions."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Import at root A, then exercise explicit root B with no environment change.
TEMP = tempfile.TemporaryDirectory(prefix='stall-test-')
A = Path(TEMP.name)/'A'
A.mkdir()
os.environ['DOIT_ROOT'] = str(A)
os.environ['HOME'] = str(Path(TEMP.name)/'home')
import autodispatch
import dispatch
import fold
import pace
import stall_sample

NOW = datetime(2026,10,28,12,tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent


def fixture(root, prefix, cap):
    (root/'events').mkdir(parents=True)
    (root/'content').mkdir()
    (root/'models.toml').write_text(f'[seats]\nbuilder = {cap}\n')
    rows = {}
    def add(role, kind, sid, minute, **kw):
        rows.setdefault(role, []).append(dict(type=kind, subject=sid,
            ts=pace._iso(NOW+timedelta(minutes=minute)), **kw))
    for n in range(3):
        sid = f'L-spec-{prefix}{n}'
        add('spec-writer','spec-written',sid,-3000)
        if n == 1:
            add('builder','build-done',sid,-2900,status='DONE')
            add('grader','verdict',sid,-2800,confirmed=True)
            add('reviewer','review',sid,-2700,n_blocking=0)
        if n == 2:
            add('builder','question',sid,-2900,asks='choose',owner='operator',deadline='2026-10-29T00:00:00Z')
    for role, events in rows.items():
        (root/f'events/L-{role}-fixture.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))


fixture(A,'910',6)


class StallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)/'B'
        fixture(self.root,'920',8)

    def expected(self, root):
        with patch.object(fold,'ROOT',root), patch.object(fold,'EVENTS',root/'events'), patch.object(dispatch,'CONTENT',root/'content'):
            rows = fold.stall_rows(fold.read_events(use_cache=False),now=NOW)
        buckets = defaultdict(list)
        for row in rows:
            buckets[row['bucket']].append(row['spec'])
        return dict(buckets)

    def digest(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'events').iterdir()}

    def reader(self, root, since='2000-01-01T00:00:00Z'):
        code = 'import stall_sample,pace,json,sys; from pathlib import Path; print(json.dumps(stall_sample.samples(pace._time(sys.argv[2]),root=Path(sys.argv[1]))))'
        return json.loads(subprocess.check_output(['/usr/bin/python3','-c',code,str(root),since],cwd=REPO/'src',text=True))

    def test_grouping(self):
        original = (fold.ROOT,fold.EVENTS,dispatch.CONTENT,list(fold.APPLIED))
        for root, cap in ((self.root,8),(A,6),(self.root,8)):
            row = stall_sample.sample(NOW,root=root)
            self.assertTrue(row['ok'],row)
            self.assertEqual(row['buckets'],self.expected(root))
            self.assertEqual(set(row['buckets']),{'STUCK','queued','waiting-on-human'})
            self.assertEqual(row['builder_capacity'],autodispatch.capacity(root))
            self.assertEqual(row['builder_capacity'],cap)
            self.assertEqual(row['hold_kinds'],{})
            self.assertEqual(row['ts'],'2026-10-28T12:00:00Z')
        with patch.object(fold,'stall_rows',return_value=[dict(spec='one',bucket='held',hold_kind='dependency'),dict(spec='two',bucket='STUCK',hold_kind=None)]):
            self.assertEqual(stall_sample.sample(NOW,root=self.root)['hold_kinds'],{'one':'dependency'})
        saved = stall_sample.record(NOW,root=self.root)
        self.assertEqual(self.reader(self.root),[saved])
        (self.root/'models.toml').unlink()
        self.assertEqual(stall_sample.sample(NOW,root=self.root)['builder_capacity'],4)
        with patch.dict(os.environ,DOIT_ROOT=str(self.root)):
            self.assertEqual(stall_sample.sample(NOW)['buckets'],self.expected(self.root))
        self.assertEqual((fold.ROOT,fold.EVENTS,dispatch.CONTENT,list(fold.APPLIED)),original)

    def test_grouping_context(self):
        # A long-running caller must fold at the requested sampling clock.
        original = (fold.ROOT, fold.EVENTS, fold.BOARD, fold.NOW,
                    dispatch.ROOT, dispatch.EVENTS, dispatch.CONTENT, list(fold.APPLIED))
        def inspect(events, *, now):
            self.assertEqual(fold.NOW, NOW)
            self.assertEqual((fold.ROOT, fold.EVENTS, dispatch.CONTENT),
                             (self.root, self.root/'events', self.root/'content'))
            return []
        with patch.object(fold, 'stall_rows', side_effect=inspect):
            row = stall_sample.sample(NOW, root=self.root)
        self.assertTrue(row['ok'], row)
        self.assertEqual((fold.ROOT, fold.EVENTS, fold.BOARD, fold.NOW,
                          dispatch.ROOT, dispatch.EVENTS, dispatch.CONTENT, list(fold.APPLIED)), original)
        # The real parser must apply corrections and reject forged role events.
        correction = dict(type='correction', subject='L-spec-9200',
                          ts=pace._iso(NOW-timedelta(hours=1)),
                          ref='L-spec-writer-fixture.jsonl:1', set={'subject': 'L-spec-9209'})
        (self.root/'events/L-operator-fixture.jsonl').write_text(json.dumps(correction)+'\n')
        with (self.root/'events/L-builder-fixture.jsonl').open('a') as file:
            file.write(json.dumps(dict(type='shipped', subject='L-spec-9999',
                                       ts=pace._iso(NOW-timedelta(days=2))))+'\n')
        for root in (self.root, A, self.root):
            row = stall_sample.sample(NOW, root=root)
            self.assertTrue(row['ok'], row)
            ids = [sid for group in row['buckets'].values() for sid in group]
            self.assertNotIn('L-spec-9999', ids)
            if root == self.root:
                self.assertIn('L-spec-9209', ids)
                self.assertNotIn('L-spec-9200', ids)
            else:
                self.assertTrue(all(sid.startswith('L-spec-910') for sid in ids))
            self.assertEqual((fold.ROOT, fold.EVENTS, fold.BOARD, fold.NOW,
                              dispatch.ROOT, dispatch.EVENTS, dispatch.CONTENT, list(fold.APPLIED)), original)

    def test_failure(self):
        original = (fold.ROOT,fold.EVENTS,dispatch.CONTENT,list(fold.APPLIED))
        for name, error in (('stall_rows',ValueError('classification unavailable for L-spec-0001: boom')),
                            ('read_events',OSError('ledger unavailable'))):
            with patch.object(fold,name,side_effect=error):
                row = stall_sample.sample(NOW,root=self.root)
                self.assertFalse(row['ok'])
                self.assertIn(str(error),row['reason'])
                self.assertEqual(row['buckets'],{})
                self.assertEqual(row['hold_kinds'],{})
                self.assertEqual(row['builder_capacity'],8)
                with patch.dict(os.environ,DOIT_ROOT=str(self.root)):
                    self.assertEqual(stall_sample.main(['sample']),1)
            self.assertEqual((fold.ROOT,fold.EVENTS,dispatch.CONTENT,list(fold.APPLIED)),original)
        self.assertEqual(len(self.reader(self.root)),2)
        self.assertTrue(stall_sample.sample(NOW,root=A)['ok'])
        # A file where a directory belongs reliably denies writing even as root.
        bad = Path(TEMP.name)/'not-directory'; bad.write_text('blocked')
        env = dict(os.environ,DOIT_ROOT=str(bad))
        result = subprocess.run(['/usr/bin/python3',str(REPO/'src/stall_sample.py'),'sample'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertTrue(result.stderr)
        # Force a real subprocess evidence failure: .jsonl is a directory.
        (self.root/'events/broken.jsonl').mkdir()
        result = subprocess.run(['/usr/bin/python3',str(REPO/'src/stall_sample.py'),'sample'],env=dict(os.environ,DOIT_ROOT=str(self.root)),capture_output=True,text=True)
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_store(self):
        before = self.digest()
        env = dict(os.environ,DOIT_ROOT=str(self.root))
        result = subprocess.run(['/usr/bin/python3',str(REPO/'src/stall_sample.py'),'sample'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        path = self.root/'state/stall-samples.jsonl'
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines),1)
        self.assertEqual(self.reader(self.root),[json.loads(lines[0])])
        path.unlink()
        self.assertEqual(stall_sample.samples(NOW,root=self.root),[])
        rows = [dict(ts=pace._iso(NOW+delta)) for delta in
                (-timedelta(days=10,minutes=1), -timedelta(days=10)+timedelta(minutes=1), timedelta(0), timedelta(minutes=1))]
        path.write_text(''.join(json.dumps(r)+'\n' for r in reversed(rows))+'{"ts":')
        self.assertEqual(stall_sample.samples(NOW,root=self.root),rows[2:])
        saved = stall_sample.record(NOW,root=self.root)
        self.assertEqual(stall_sample.samples(NOW-timedelta(days=20),root=self.root),[rows[1],rows[2],saved,rows[3]])
        self.assertEqual(before,self.digest())


if __name__ == '__main__':
    unittest.main()
