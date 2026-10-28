#!/usr/bin/env python3
"""Hermetic SD6 regression groups; each -k selects an acceptance criterion."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pace

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent
FIELDS = {'ts', 'disk_used_pct', 'disk_total_gb', 'ram_used_pct', 'swap_used_pct',
          'claude_5h_pct', 'claude_5h_reset', 'claude_7d_pct', 'claude_7d_reset',
          'codex_7d_pct', 'codex_7d_reset', 'source'}


class PaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pace-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'root'
        self.home = Path(temp.name) / 'home'
        self.root.mkdir(); self.home.mkdir()
        for name in ('events', 'state'):
            (self.root / name).mkdir()
        self.mem = Path(temp.name) / 'meminfo'
        self.mem.write_text('MemTotal: 10485760 kB\nMemAvailable: 4194304 kB\n'
                            'SwapTotal: 2097152 kB\nSwapFree: 1572864 kB\n')
        for p in (patch.dict(os.environ, DOIT_ROOT=str(self.root), HOME=str(self.home)),
                  patch.object(pace, 'MEMINFO', self.mem),
                  patch.object(pace.shutil, 'disk_usage', return_value=(1000*2**30, 828*2**30, 131*2**30))):
            p.start(); self.addCleanup(p.stop)
        self.usage(self.root)

    def usage(self, root, pct=14):
        (root / 'state').mkdir(exist_ok=True)
        (root / 'state/claude-usage.json').write_text(json.dumps(dict(
            observed_at=pace._iso(NOW), session_id='fixture',
            five_hour=dict(used_percentage=pct, resets_at=(NOW+timedelta(hours=2)).timestamp()),
            seven_day=dict(used_percentage=36, resets_at=(NOW+timedelta(days=3)).timestamp()))))

    def rollout(self, at=NOW, weekly=31, minutes=10080, reset=None, name='rollout-a.jsonl'):
        path = self.home / '.codex/sessions/2026/10/28' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(timestamp=pace._iso(at), type='event_msg', payload=dict(type='token_count',
                   rate_limits=dict(primary=dict(used_percent=99, window_minutes=300,
                       resets_at=(NOW+timedelta(hours=1)).timestamp()), secondary=dict(
                       used_percent=weekly, window_minutes=minutes,
                       resets_at=reset or (NOW+timedelta(days=4)).timestamp()))))
        with path.open('a') as f:
            f.write(json.dumps(row)+'\n')
        return path

    def ledger(self, rows):
        (self.root / 'events/L-seat-usage-local.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def digest(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'events').iterdir()}

    def test_row_shape(self):
        self.rollout()
        row = pace.sample(NOW, root=self.root)
        self.assertEqual(set(row), FIELDS)
        self.assertEqual(row['ts'], '2026-10-28T12:00:00Z')
        for key, val in dict(disk_used_pct=100*828/959, disk_total_gb=1000., ram_used_pct=60.,
                             swap_used_pct=25., claude_5h_pct=14., claude_7d_pct=36., codex_7d_pct=31.).items():
            self.assertAlmostEqual(row[key], val)
        self.assertEqual(row['source'], 'live')
        self.assertEqual(row['codex_7d_reset'], '2026-11-01T12:00:00Z')
        other = self.root.parent/'other'; other.mkdir(); self.usage(other, 77)
        self.assertEqual(pace.sample(NOW, root=other)['claude_5h_pct'], 77)
        self.assertEqual(pace.record(NOW, root=other)['claude_5h_pct'], 77)
        with patch.dict(os.environ, DOIT_ROOT=str(other)):
            self.assertEqual(pace.sample(NOW)['claude_5h_pct'], 77)
        self.assertEqual(pace.sample(NOW)['claude_5h_pct'], 14)
        self.assertFalse((self.root/'state/pace-samples.jsonl').exists())

    def test_missing_null(self):
        row = pace.sample(NOW+timedelta(seconds=900))
        self.assertIsNone(row['claude_7d_pct'])
        self.assertIsNone(row['codex_7d_pct'])
        self.mem.write_text('MemTotal: 0 kB\nSwapTotal: 0 kB\n')
        self.assertIsNone(pace.sample(NOW)['swap_used_pct'])
        self.mem.unlink()
        row = pace.sample(NOW)
        self.assertIsNone(row['ram_used_pct'])
        with patch.object(pace.shutil, 'disk_usage', side_effect=OSError('unavailable')):
            self.assertIsNone(pace.sample(NOW)['disk_used_pct'])
        (self.root/'state/claude-usage.json').write_text('{bad')
        self.assertIsNone(pace.sample(NOW)['claude_5h_pct'])

    def test_codex_window(self):
        path = self.rollout(at=NOW-timedelta(minutes=2), weekly=17)
        self.rollout(at=NOW-timedelta(minutes=1), weekly=23)
        self.rollout(at=NOW, weekly=95, minutes=300)
        with path.open('a') as f:
            f.write('{torn\n'+json.dumps(dict(type='event_msg', payload={} ))+'\n')
        self.assertEqual(pace.sample(NOW)['codex_7d_pct'], 23)
        self.rollout(at=NOW, reset=NOW.timestamp()-1)
        self.assertIsNone(pace.sample(NOW)['codex_7d_pct'])
        self.rollout(at=NOW+timedelta(seconds=1), weekly=float('nan'))
        self.assertIsNone(pace.sample(NOW)['codex_7d_pct'])
        for i in range(6):
            p = self.rollout(at=NOW+timedelta(seconds=i+2), weekly=40+i, name=f'rollout-{i}.jsonl')
            os.utime(p, (100+i, 100+i))
        os.utime(path, (1, 1))
        self.assertEqual(pace.sample(NOW)['codex_7d_pct'], 45)

    def test_pace_pct(self):
        for key, window in pace.WINDOWS.items():
            self.assertEqual(pace.pace_pct(NOW, window, NOW-window/2), 50)
            self.assertEqual(pace.pace_pct(NOW, window, NOW+window), 200)
            self.assertEqual(pace.pace_pct(NOW, window, NOW-2*window), -100)
        self.assertEqual(pace.SAFE_CEILING, dict(disk=85, ram=90))
        for args in ((NOW.replace(tzinfo=None), timedelta(hours=1), NOW),
                     (NOW, timedelta(0), NOW), (NOW, timedelta(days=1), NOW.replace(tzinfo=None))):
            with self.assertRaises(ValueError): pace.pace_pct(*args)

    def cli(self, *args):
        return subprocess.run(['/usr/bin/python3', str(REPO/'src/pace.py'), *args], capture_output=True, text=True)

    def test_roundtrip(self):
        (self.root/'state/pace-backfill.json').write_text('{}')
        result = self.cli('sample')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (self.root/'state/pace-samples.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        code = 'import pace,json; from datetime import datetime,timezone; print(json.dumps(pace.samples(datetime(2000,1,1,tzinfo=timezone.utc))))'
        out = subprocess.check_output(['/usr/bin/python3','-c',code], cwd=REPO/'src', text=True)
        self.assertEqual(json.loads(out), [json.loads(lines[0])])
        self.reader_edges()

    def reader_edges(self):
        path = self.root/'state/pace-samples.jsonl'
        path.unlink()
        self.assertEqual(pace.samples(NOW), [])
        rows = [dict(ts=pace._iso(NOW+timedelta(minutes=m))) for m in (1,-1,0)]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows)+'{"ts":')
        self.assertEqual(pace.samples(NOW), [rows[2],rows[0]])
        with patch.object(Path, 'read_text', side_effect=PermissionError('read denied')):
            with self.assertRaises(PermissionError): pace.samples(NOW)

    def test_retention(self):
        path = self.root/'state/pace-samples.jsonl'
        rows = [dict(ts=pace._iso(NOW+delta)) for delta in
                (-timedelta(days=10,minutes=1), -timedelta(days=10)+timedelta(minutes=1), -timedelta(hours=1))]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        before = self.digest()
        live = pace.record(NOW)
        self.assertEqual(pace.samples(NOW-timedelta(days=20)), rows[1:]+[live])
        self.assertTrue(path.with_suffix('.jsonl.lock').exists())
        self.assertEqual(before, self.digest())

    def backfill_fixture(self):
        at = NOW-timedelta(hours=2)
        seat = dict(type='seat-usage', observed_at=pace._iso(at), five_hour_pct='14', seven_day_pct='36',
                    five_hour_resets_at=pace._iso(NOW+timedelta(hours=2)), seven_day_resets_at=pace._iso(NOW+timedelta(days=3)))
        self.ledger([dict(seat, observed_at=pace._iso(at-timedelta(days=11))), dict(seat, five_hour_pct='9'),
                     dict(seat, observed_at=pace._iso(at+timedelta(minutes=2))),
                     dict(seat, observed_at=pace._iso(at+timedelta(minutes=20)))])
        (self.root/'events/L-tick-local.jsonl').write_text(json.dumps(dict(type='tick',
            admission_sampled_at=pace._iso(at), admission_readings=dict(mem_avail_gb=4,swap_free_gb=1.5)))+'\n')
        (self.root/'realm-history.json').write_text(json.dumps(dict(disk=[[pace._iso(at),82.8]])))
        self.rollout(at=at)
        return at

    def test_backfill_rows(self):
        at = self.backfill_fixture()
        result = pace.backfill(now=NOW, root=self.root)
        self.assertEqual(result['rows_added'], 2)
        rows = pace.samples(NOW-timedelta(days=20))
        self.assertEqual(rows[0]['ts'], pace._iso(at))
        self.assertEqual(rows[0]['source'], 'backfill:admission+codex-rollout+realm-history+seat-usage')
        self.assertEqual(rows[0]['claude_5h_pct'],14.)
        self.assertEqual(rows[0]['claude_7d_reset'],'2026-10-31T12:00:00Z')
        self.assertAlmostEqual(rows[0]['disk_used_pct'], 82.8/.959)
        self.assertEqual(rows[0]['ram_used_pct'], 60.)
        self.assertEqual(rows[0]['swap_used_pct'],25.)
        self.assertEqual(rows[0]['codex_7d_pct'],31.)
        self.assertIsNone(rows[0]['disk_total_gb'])
        self.assertEqual(rows[1]['source'],'backfill:seat-usage')
        self.assertIsNone(rows[1]['ram_used_pct'])
        self.assertIsNone(rows[1]['disk_used_pct'])
        self.assertEqual(set(rows[1]), FIELDS)

    def test_backfill_safe(self):
        self.backfill_fixture()
        before = self.digest()
        live = pace.record(NOW-timedelta(minutes=100))
        pace.backfill(now=NOW)
        path = self.root/'state/pace-samples.jsonl'
        content = path.read_bytes()
        self.assertEqual(pace.backfill(now=NOW)['rows_added'],0)
        self.assertEqual(path.read_bytes(),content)
        self.assertEqual(pace.samples(NOW-timedelta(minutes=100)),[live])
        self.assertEqual(before,self.digest())
        marker = self.root/'state/pace-backfill.json'
        self.assertTrue(marker.exists())
        marker.unlink()
        with patch.object(pace.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError): pace.backfill(now=NOW)
        self.assertFalse(marker.exists())
        with patch.object(pace, 'backfill', side_effect=OSError('source failed')) as bf:
            self.assertEqual(pace.main(['sample']),1)
            bf.assert_called_once()
        self.assertFalse(marker.exists())
        marker.write_text('{}')
        with patch.object(pace, 'backfill') as bf:
            self.assertEqual(pace.main(['sample']),0)
            bf.assert_not_called()
        marker.unlink()
        (self.root/'realm-history.json').write_text('{invalid')
        pace.backfill(now=NOW)
        marker.unlink()
        with patch.object(pace, '_rollouts', side_effect=OSError('rollout directory')):
            with self.assertRaises(OSError): pace.backfill(now=NOW)
        self.assertFalse(marker.exists())
        # Exercise the actual CLI, rather than only a patched main function.
        result = self.cli('sample')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        marker.unlink()
        sessions = self.home/'.codex/sessions'
        import shutil
        shutil.rmtree(sessions)
        sessions.write_text('not a directory')
        count = len(path.read_text().splitlines())
        result = self.cli('sample')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('backfill:', result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(len(path.read_text().splitlines()), count+1)

    def test_cron_rows(self):
        import crons
        rows = crons.rows(str(REPO/'crons.toml'))
        self.assertEqual([r['name'] for r in rows[-2:]], ['pace-sample','stall-sample'])
        for row, interval, runtime, module in zip(rows[-2:],(600,3600),(540,1800),('pace','stall_sample')):
            self.assertEqual(crons.interval_s(row['schedule']),interval)
            for field,value in dict(where='user',path='/usr/bin/python3',owner='thinker',project='do-it-v2',max_runtime_s=runtime).items():
                self.assertEqual(row[field],value)
            self.assertIn(f'cron-run {row["name"]} -- /usr/bin/python3 {REPO}/src/{module}.py sample',row['command'])
            self.assertIn(f'/logs/{row["name"]}.log',row['command'])
            self.assertEqual([r['name'] for r in rows if re.search(row['sig'],r['command'])],[row['name']])

    def test_no_paid_calls(self):
        tokens = ('claude -p','"claude"','"codex"','urllib','http.client','requests','socket',
                  'anthropic','openai','fold.append','"actor"')
        for filename in ('pace.py','stall_sample.py'):
            source = (REPO/'src'/filename).read_text()
            self.assertNotIn('subprocess',source)
            for token in tokens:
                self.assertNotIn(token,source)
                print(f'{filename}: {token}: absent')


if __name__ == '__main__':
    unittest.main()
