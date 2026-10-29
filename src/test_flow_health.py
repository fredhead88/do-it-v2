#!/usr/bin/env python3
"""Hermetic daily flow judgements and production ledger round trips."""
import contextlib
import hashlib
import io
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

import cron_run
import crons
import fold
import flow_health as flow

UTC = timezone.utc
START = datetime(2026, 10, 26, tzinfo=UTC)
END = START + timedelta(days=1)
NOW = END + timedelta(minutes=30)


def event(kind, spec='P', at=None, **fields):
    return dict(type=kind, subject=spec, ts=(at or START).isoformat(), **fields)


def sample_rows():
    return [dict(ts=(START + timedelta(hours=i)).isoformat(), ok=True, buckets={})
            for i in range(-1, 24)]


def stuck_rows():
    rows = sample_rows()
    for i in (3, 4, 5):
        rows[i + 1]['buckets'] = {'STUCK': ['A']}
    rows[10]['buckets'] = {'STUCK': ['B']}
    rows[12]['buckets'] = {'STUCK': ['C']}
    rows[14]['buckets'] = {'STUCK': ['C']}
    rows[0]['buckets'] = {'STUCK': ['D']}
    rows[1]['buckets'] = {'STUCK': ['D']}
    return rows


def windows(verified=4, wait=90):
    rows = []
    for i in range(5):
        spec, win = f'W-spec-{i}', f'win-{i}'
        opened = START + timedelta(hours=5, minutes=i)
        rows += [event('review', spec, opened - timedelta(minutes=wait), n_blocking=0),
                 event('window-opened', win, opened, specs=[spec])]
        rows.append(event('window-verified' if i < verified else 'window-dropped', win, opened))
    return rows


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flow-health-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'events').mkdir()
        (self.root / 'state').mkdir()
        for key, value in [('ROOT', self.root), ('EVENTS', self.root / 'events')]:
            p = patch.object(fold, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'DOIT_ROOT': str(self.root)})
        p.start()
        self.addCleanup(p.stop)
        self.rows = sample_rows()
        self.save_samples()
        self.store = cron_run.store_path('pane-watchdog')
        self.points = [START - timedelta(minutes=5) + timedelta(minutes=i * 5)
                       for i in range(295)]
        self.write_points(self.points)
        (self.root / 'state/pane-watchdog.json').write_text(json.dumps(dict(ts=(END + timedelta(minutes=28)).isoformat())))
        self.events = [event('spec-written', 'fixture', project='fixture')]
        self.evidence = []

    def save_samples(self):
        (self.root / 'state/stall-samples.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.rows))

    def write_points(self, points):
        self.store.unlink(missing_ok=True)
        for t in points:
            for ev in ('start', 'end'):
                cron_run._append_row(self.store, dict(ev=ev, ts=t.isoformat(), rc=0))

    def judge(self, events=None, now=NOW):
        return flow.daily(self.events if events is None else events, now)

    def alarm(self, measure, events=None, now=NOW):
        bodies = [b for b in self.judge(events, now) if b['type'] == 'health-alarm' and b['measure'] == measure]
        self.evidence.extend(bodies)
        self.assertLessEqual(len(bodies), 1)
        return bodies[0] if bodies else None

    def install(self, rows):
        groups = {}
        for row in rows:
            row = dict(row)
            actor = row.pop('actor', 'planner')
            groups.setdefault(actor, []).append(row)
        for actor, values in groups.items():
            (fold.EVENTS / f'L-{actor}-fixture.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in values))

    def cli(self, *args):
        return subprocess.run(['/usr/bin/python3', str(Path(flow.__file__).resolve()), '--day', '2026-10-26', *args],
                              env=dict(os.environ), text=True, capture_output=True)

    def test_AC1(self):
        self.rows = stuck_rows()
        # Latest successful sample in the slot wins, even if a later failed row exists.
        self.rows += [dict(ts=(START + timedelta(hours=3, minutes=50)).isoformat(), ok=False, reason='late failure')]
        self.save_samples()
        bodies = self.judge(self.events + [event('spec-written', 'A', project='fixture')])
        alarm = next(b for b in bodies if b['type'] == 'health-alarm' and b['measure'] == 'M1')
        self.assertEqual(alarm['state'], 'breach')
        self.assertEqual([(c['spec'], c['samples']) for c in alarm['cases']], [('A', 3), ('D', 2)])
        lessons = [b for b in bodies if b['type'] == 'lesson']
        self.assertEqual([b['subject'] for b in lessons], ['A', 'D'])
        self.assertEqual(lessons[0]['project'], 'fixture')
        self.assertTrue(all(b['problem'] == 'stuck-over-1h' for b in lessons))
        self.assertTrue(all('actor' not in b for b in bodies))
        self.assertEqual(len({b['flow_key'] for b in bodies}), len(bodies))
        self.assertEqual(flow.MEASURES, ('M1', 'M2', 'M3', 'M5'))
        self.assertEqual((flow.STALE_MIN, flow.DIALOG_MAX_MIN, flow.VERIFIED_MIN_PCT, flow.WAIT_MAX_MIN), (15, 10, 80, 120))
        self.assertLessEqual(len(alarm['line']), 200)
        self.assertNotIn('\n', alarm['line'])
        self.evidence = bodies

    def test_AC2(self):
        for slot in (7, -1):
            self.rows = [r for r in sample_rows() if r['ts'] != (START + timedelta(hours=slot)).isoformat()]
            self.save_samples()
            alarm = self.alarm('M1')
            self.assertEqual(alarm['state'], 'undetermined')
            self.assertIn((START + timedelta(hours=slot)).isoformat(), ' '.join(alarm['undetermined']))
        for row in (dict(ok=False, reason='probe failed'), dict(ok=True, buckets=[])):
            self.rows = sample_rows()
            self.rows[12].update(row)
            self.save_samples()
            self.assertEqual(self.alarm('M1')['state'], 'undetermined')
        self.rows = stuck_rows()
        del self.rows[8]
        self.save_samples()
        alarm = self.alarm('M1')
        self.assertEqual(alarm['state'], 'breach')
        self.assertTrue(alarm['cases'] and alarm['undetermined'])
        self.rows = []
        self.save_samples()
        self.assertEqual(len(self.alarm('M1')['undetermined']), 25)
        with patch.object(flow.stall_sample, 'samples', side_effect=OSError('reader failed')):
            self.assertIn('OSError', ' '.join(self.alarm('M1')['undetermined']))

    def test_AC3(self):
        rows = []
        for i in range(1, 8):
            spec = f'P{i}'
            rows += [event('spec-written', spec, START - timedelta(days=2), spawn='L-spec-writer-1' if i != 7 else 'operator'),
                     event('build-started', spec, START + timedelta(hours=10), actor='builder')]
        rows += [event('spawn-done', 'P1', START + timedelta(hours=9), role='spec-auditor'),
                 event('spawn-done', 'P2', START + timedelta(hours=10, minutes=30), role='spec-auditor'),
                 event('spec-carried', 'P4'),
                 event('build-started', 'P5', START - timedelta(hours=1), actor='builder')]
        next(r for r in rows if r['subject'] == 'P6' and r['type'] == 'build-started')['ts'] = '2026-10-26T23:59:59Z'
        alarm = self.alarm('M2', rows)
        self.assertEqual([c['spec'] for c in alarm['cases']], ['P2', 'P3', 'P6'])
        self.assertEqual(alarm['cases'][0]['audit_done_at'], (START + timedelta(hours=10, minutes=30)).isoformat())
        self.assertTrue(all(c['build_event_type'] == 'build-started' and c['build_actor'] == 'builder' for c in alarm['cases']))
        bad = [dict(type='build-started', actor='builder', subject='P8', ts='not-a-time')]
        self.assertIn('P8', ' '.join(self.alarm('M2', bad)['undetermined']))
        for kind, fields in [('spec-written', dict(spawn='L-spec-writer-1')),
                             ('spawn-done', dict(role='spec-auditor'))]:
            bad_row = dict(type=kind, subject='P8', ts='not-a-time', **fields)
            self.assertIn('P8', ' '.join(self.alarm('M2', [bad_row])['undetermined']))
        self.assertIsNone(self.alarm('M2'))
        raw = '{"project": "albert-scott", "spawn": "L-builder-0164", "subject": "L-spec-0073", "ts": "2026-09-22T08:32:20+00:00", "type": "build-started", "v": 1, "worktree": "/home/albert/.do-it/worktrees/albert-scott/l-spec-0073"}'
        (fold.EVENTS / 'L-builder-0164.jsonl').write_text(raw + '\n')
        self.install([event('spec-written', 'L-spec-0073', datetime(2026, 9, 21, tzinfo=UTC), spawn='L-spec-writer-1')])
        self.assertEqual(self.alarm('M2', fold.read_events(), datetime(2026, 9, 23, tzinfo=UTC))['cases'][0]['build_started_at'], '2026-09-22T08:32:20+00:00')
        self.assertIsNone(self.alarm('M2', [event('spec-written', spawn='L-spec-writer-1'), event('spawn-started', role='builder', actor='builder')]))
        for measure in ('M2', 'M3'):
            self.assertEqual(self.alarm(measure, [])['state'], 'undetermined')

    def test_AC4(self):
        for verified, wait, failed in [(3, 60, ['verified_pct']), (5, 130, ['median_wait_min']), (4, 119, []), (5, 120, ['median_wait_min'])]:
            alarm = self.alarm('M3', windows(verified, wait))
            self.assertEqual(alarm['cases'][0]['failed'] if alarm else [], failed)
        self.assertIsNone(self.alarm('M3'))
        for value in (None, {'attempts': 3}, dict(attempts=3, verified=3, dropped=0, verified_pct=100, median_wait_min=None)):
            with patch.object(flow.scorecard, 'window_stats', return_value=value) as call:
                self.assertEqual(self.alarm('M3')['state'], 'undetermined')
                call.assert_called_once_with(self.events, START, END)
        with patch.object(flow.scorecard, 'window_stats', side_effect=ValueError('unreadable')):
            self.assertIn('ValueError', ' '.join(self.alarm('M3')['undetermined']))

    def test_AC5(self):
        self.assertIsNone(self.alarm('M5'))
        resolved = lambda minute: event('pane-dialog-resolved', resolved_at=(START + timedelta(hours=14, minutes=minute)).isoformat(), prev_scan_at=(START + timedelta(hours=14)).isoformat(), pane='%1', role='builder')
        alarm = self.alarm('M5', [resolved(9), resolved(11), dict(resolved(12), resolved_at=END.isoformat())])
        self.assertEqual([c['dialog_min'] for c in alarm['cases']], [11])
        bad = resolved(9)
        del bad['prev_scan_at']
        self.assertEqual(self.alarm('M5', [bad])['state'], 'undetermined')
        gap_start = START + timedelta(hours=12)
        for gap in (15, 16):
            points = [t for t in self.points if not gap_start < t < gap_start + timedelta(minutes=gap)]
            points.append(gap_start + timedelta(minutes=gap))
            self.write_points(sorted(set(points)))
            alarm = self.alarm('M5')
            if gap == 15:
                self.assertIsNone(alarm)
            else:
                self.assertIn(gap_start.isoformat(), ' '.join(alarm['undetermined']))
                cron_run._append_row(self.store, dict(ev='end', ts=(gap_start + timedelta(minutes=10)).isoformat(), rc=1))
                self.assertEqual(self.alarm('M5')['state'], 'undetermined')
        for points in ([t for t in self.points if t > START], [t for t in self.points if t < END - timedelta(minutes=15)]):
            self.write_points(points)
            self.assertEqual(self.alarm('M5')['state'], 'undetermined')
        self.write_points(self.points)
        rows = self.store.read_text().splitlines()
        self.store.write_text('\n'.join(rows[-50:]) + '\n')
        self.assertEqual(self.alarm('M5')['state'], 'undetermined')
        self.store.unlink()
        heartbeat = self.root / 'state/pane-watchdog.json'
        heartbeat.unlink()
        self.assertEqual(self.alarm('M5')['state'], 'undetermined')
        heartbeat.write_text('{"ts":"bad"}')
        self.assertIn('heartbeat', ' '.join(self.alarm('M5')['undetermined']))
        heartbeat.write_text('not-json')
        self.assertIn('heartbeat', ' '.join(self.alarm('M5')['undetermined']))
        # >50 hours written through the production ring, freshly reopened.
        points = [END - timedelta(hours=51) + timedelta(minutes=5*i) for i in range(619)]
        self.write_points(points)
        heartbeat.write_text(json.dumps(dict(ts=(END + timedelta(minutes=28)).isoformat())))
        self.assertEqual(len(self.store.read_text().splitlines()), 1200)
        self.assertLess(flow._time(json.loads(self.store.read_text().splitlines()[0])['ts']), START)
        self.assertIsNone(self.alarm('M5'))

    def test_AC6(self):
        more = windows(4, 60)
        for row in more:
            row['subject'] += '-extra'
            if 'specs' in row:
                row['specs'] = [spec + '-extra' for spec in row['specs']]
        healthy = self.events + windows(5, 60) + more + [
            event('spec-written', 'audited', START - timedelta(days=1), spawn='L-spec-writer-1'),
            event('spawn-done', 'audited', START + timedelta(hours=1), role='spec-auditor'),
            event('build-started', 'audited', START + timedelta(hours=2), actor='builder')]
        self.assertEqual(flow.scorecard.window_stats(healthy, START, END)['verified_pct'], 90)
        self.assertEqual(self.judge(healthy), [])
        self.rows = stuck_rows()
        self.save_samples()
        unhealthy = self.events + windows(3, 60) + [
            event('spec-written', 'unaudited', START - timedelta(days=1), spawn='L-spec-writer-1'),
            event('build-started', 'unaudited', START + timedelta(hours=2), actor='builder')]
        bodies = self.judge(unhealthy)
        self.install(unhealthy)
        with patch.object(fold, 'NOW', NOW):
            for body in bodies:
                if body['measure'] == 'M1':
                    flow.append_body(body)
        partial = self.judge(fold.read_events())
        self.assertFalse(any(b['measure'] == 'M1' for b in partial))
        self.assertEqual({b['measure'] for b in partial}, {'M2', 'M3'})
        with patch.object(fold, 'NOW', NOW):
            for body in partial:
                flow.append_body(body)
        self.assertEqual(self.judge(fold.read_events()), [])
        self.assertTrue(self.judge([dict(b, flow_key='different') for b in bodies]))
        original = self.judge(self.events)
        with patch.object(flow, 'm3', side_effect=RuntimeError('evaluator failed')):
            changed = self.judge(self.events)
        self.assertEqual([b for b in original if b['measure'] != 'M3'], [b for b in changed if b['measure'] != 'M3'])
        self.assertIn('RuntimeError', ' '.join(next(b for b in changed if b['measure'] == 'M3')['undetermined']))
        for now in (END, NOW, END + timedelta(hours=23, minutes=59)):
            self.assertTrue(all(b['day'] == '2026-10-26' for b in self.judge([], now)))
        self.evidence = dict(clean=[], partial=partial, replay=[], isolated=changed,
                             clock_day='2026-10-26', verified_pct=90, median_wait_min=60)

    def test_AC8(self):
        self.rows = stuck_rows()
        self.save_samples()
        self.install([event('spec-written', 'P', START - timedelta(days=1), spawn='L-spec-writer-1'), event('build-started', actor='builder')])
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'flow-health: day=2026-10-26 M1=breach M2=breach M3=ok M5=ok appended=4')
        raw = (fold.EVENTS / 'L-health-local.jsonl').read_bytes()
        self.assertTrue(all('actor' not in json.loads(line) for line in raw.splitlines()))
        code = 'import sys,json;sys.path.insert(0,sys.argv[1]);import fold;print(json.dumps([e for e in fold.read_events() if e.get("flow_key")]))'
        fresh = subprocess.run(['/usr/bin/python3', '-I', '-c', code, str(Path(__file__).resolve().parent)], env=dict(os.environ), text=True, capture_output=True)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        events = json.loads(fresh.stdout)
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e['actor'] == 'health' for e in events))
        self.assertIn('appended=0', self.cli().stdout)
        dry = self.cli('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout.splitlines()[0]), [])
        self.assertEqual((fold.EVENTS / 'L-health-local.jsonl').read_bytes(), raw)
        self.evidence = dict(summary=result.stdout.strip(), events=events, fresh_process=True, dry_run=dry.stdout.strip())
        for path in list(fold.EVENTS.iterdir()):
            path.unlink()
        for args in ((), ('--dry-run',)):
            self.assertEqual(self.cli(*args).returncode, 1)  # empty directory
            (fold.EVENTS / 'other.txt').write_text('not ledger')
            self.assertEqual(self.cli(*args).returncode, 1)
            (fold.EVENTS / 'other.txt').unlink()
            fold.EVENTS.rmdir()
            self.assertEqual(self.cli(*args).returncode, 1)
            self.assertFalse(fold.EVENTS.exists())
            fold.EVENTS.mkdir()
            (fold.EVENTS / 'L-planner-empty.jsonl').write_text('')
            fold.EVENTS.chmod(0)
            try:
                self.assertEqual(self.cli(*args).returncode, 1)
            finally:
                fold.EVENTS.chmod(0o700)
            with patch.object(flow.os, 'listdir', side_effect=PermissionError('blocked')), patch.object(fold, 'append') as append:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(flow.main(['--day', '2026-10-26', *args]), 1)
                append.assert_not_called()
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('M2=undetermined M3=undetermined', result.stdout)
            self.assertNotIn('M2=ok', result.stdout)
            self.assertNotIn('M3=ok', result.stdout)
            for path in fold.EVENTS.iterdir():
                path.unlink()
        with patch.object(fold, 'read_events', side_effect=OSError('read failed')), patch.object(fold, 'append') as append:
            (fold.EVENTS / 'L-planner-empty.jsonl').write_text('')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(flow.main([]), 1)
            append.assert_not_called()
        with patch.object(fold, 'append', side_effect=OSError('append failed')) as append, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(flow.main(['--day', '2026-10-26']), 1)
            self.assertEqual(append.call_count, 1)

    def test_AC9(self):
        rows = crons.rows()
        hits = [r for r in rows if r['name'] == 'flow-health-daily']
        self.assertEqual(len(hits), 1)
        row = hits[0]
        for key, value in dict(where='user', schedule='30 0 * * *', path='/usr/bin/python3', max_runtime_s=600, owner='thinker', project='do-it-v2').items():
            self.assertEqual(row[key], value)
        self.assertIn('cron-run flow-health-daily -- /usr/bin/python3 ', row['command'])
        self.assertIn(str(Path(__file__).resolve().parent / 'flow_health.py'), row['command'])
        self.assertIn(f'>> {fold.ROOT}/logs/flow-health.log 2>&1', row['command'])
        self.assertEqual([r['name'] for r in rows if re.search(row['sig'], r['command'])], ['flow-health-daily'])
        self.evidence = row


def main():
    groups = ['AC1', 'AC2', 'AC3', 'AC4', 'AC5', 'AC6', 'AC8', 'AC9']
    selected = sys.argv[1:] or groups
    if any(g not in groups for g in selected):
        return 2
    for group in selected:
        test = FlowTests('test_' + group)
        result = unittest.TextTestRunner().run(unittest.TestSuite([test]))
        if not result.wasSuccessful():
            return 1
        body = json.dumps(test.evidence, sort_keys=True)
        print(f'L-spec-1676 {group} ok')
        print(json.dumps(dict(url=f'cli://flow-health/{group}', status=0, body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=test.evidence)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
