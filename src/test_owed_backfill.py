#!/usr/bin/env python3
"""Hermetic backfill regressions using the real CLI and append path."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Resolve module-level roots inside a disposable fixture even when run directly.
_ISO = tempfile.TemporaryDirectory(prefix='owed-backfill-test-')
os.environ['DOIT_ROOT'] = str(pathlib.Path(_ISO.name) / 'root')
os.environ['HOME'] = str(pathlib.Path(_ISO.name) / 'home')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold
import owed
import owed_backfill as backfill

REPO = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def event(event_type, spec='L-spec-9001', seconds=-100, actor='operator', **kv):
    return dict(type=event_type, subject=spec, ts=(NOW + timedelta(seconds=seconds)).isoformat(), actor=actor, **kv)


def row(spec='L-spec-9001', criterion='AC1', capability=None, ask=None, status='due'):
    evs = [event('owed-ac', spec, criterion=criterion, wake_at=(NOW - timedelta(days=1)).isoformat()),
           event('shipped', spec, seconds=-90, actor='executor')]
    if ask is not None:
        evs.append(event('owed-unobservable', spec, seconds=-80, actor='owed-sweeper',
                         criterion=criterion, capability=capability, why=ask))
    elif capability:
        evs.append(event('owed-routed', spec, seconds=-80, criterion=criterion, route='relabel', capability=capability))
    if status in ('failed', 'met', 'waived', 'unverifiable', 'correcting'):
        kind = 'failed' if status == 'correcting' else status
        evs.append(event('owed-' + kind, spec, seconds=-60, criterion=criterion, evidence='proof', kind='unmet', reason='fixture'))
        if status == 'correcting':
            evs.append(event('owed-corrective', spec, seconds=-50, criterion=criterion, corrective='L-spec-9999'))
    return evs


def audit(spec='L-spec-9001', criterion='AC1', cls='CRITICAL', verdict='FAIL', **kw):
    return dict(spec=spec, criterion=criterion, **{'class': cls}, verdict=verdict,
                how_to_check=kw.get('how_to_check', 'check fixture'), evidence=kw.get('evidence', 'audit finding'))


def writes(actions):
    return [a['event'] for a in actions if a.get('event')]


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='owed-backfill-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content/owed-audit').mkdir(parents=True)
        self.target = self.root / 'events/L-thinker-0001.jsonl'
        self.env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_LEDGER_FILE=self.target.name,
                        DOIT_READ_CACHE='0', PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('DOIT_PROJECT', None)
        self.install(row(), [audit()])

    def install(self, events, audits):
        for p in (self.root / 'events').glob('*.jsonl'):
            p.unlink()
        grouped = {}
        for e in sorted(events, key=lambda e: e['ts']):
            obj = dict(e)
            grouped.setdefault(obj.pop('actor'), []).append(obj)
        for actor, evs in grouped.items():
            (self.root / f'events/L-{actor}-fixture.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in evs))
        self.target.write_text('')
        (self.root / 'content/owed-audit/result-fixture.json').write_text(json.dumps(audits))

    def cli(self, *args):
        return subprocess.run([str(REPO / 'doit'), 'owed-backfill', *args], env=self.env, text=True, capture_output=True)

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def planned(self, events, audits):
        specs = fold.fold(sorted(events, key=lambda e: e['ts']))[0]
        return backfill.plan({s: v['evs'] for s, v in specs.items()}, audits, NOW)

    def test_ac1_plan(self):
        evs = row('L-spec-0152', 'AC4', 'other', 'run repository tests')
        self.install(evs, [audit('L-spec-0152', 'AC4')])
        before = self.snapshot()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / 'content/L-owed-backfill-report.md').exists())
        for word in ('owed-failed', 'owed-corrective', 'owed-routed', 'L-spec-0152', 'AC4', 'reason=', 'after: not applied'):
            self.assertIn(word, result.stdout)
        print('plan writes nothing; plan lists actions ok')

    def test_ac2_dropped(self):
        evs = [event('owed-met', actor='thinker', criterion='AC1', evidence='$ check\npassed'),
               event('owed-failed', actor='thinker', criterion='AC2'),
               event('owed-waived', actor='executor', criterion='AC3')]
        classes = [('executor', 'worked'), ('executor', 'blocked-external'), ('executor', 'origin-pushed'),
                   ('operator', 'shipped'), ('owed-sweeper', 'worked'), ('owed-sweeper', 'gate-infra'),
                   ('thinker', 'origin-pushed'), ('thinker', 'autodispatch-failed')]
        evs += [event(kind, actor=actor) for actor, kind in classes]
        self.install(evs, [])
        out = self.cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('refused total: 10; class sum: 10', out.stdout)
        for actor, kind in classes:
            self.assertIn(f'{kind} / {actor}: 1', out.stdout)
        for criterion, state in [('AC1', 'admitted'), ('AC2', 'still-dropped'), ('AC3', 'still-dropped')]:
            self.assertRegex(out.stdout, criterion + r'.*' + state)
        print('dropped by class; owed itemised; class sum equals total ok')

    def test_ac3_failures(self):
        evs, audits = [], []
        for i in range(30):
            sid = f'L-spec-{9100+i}'
            audits.append(audit(sid))
            if i >= 4:
                evs += row(sid, status='failed' if i < 6 else 'due')
        actions = self.planned(evs, audits)
        failures = [a for a in actions if a['section'] == 'audit']
        self.assertEqual(len(failures), 30)
        self.assertEqual(len(writes(failures)), 24)
        self.assertEqual(sum(a.get('event') is None for a in failures), 6)
        self.assertTrue(all(a['event_type'] == 'owed-failed' for a in failures))
        for e in writes(failures):
            self.assertEqual(e['kind'], 'unmet')
            self.assertEqual(e['evidence'], '$ check fixture\naudit finding')
        print('failures planned; evidence shape; skip reasons ok')

    def test_ac4_correctives_close(self):
        evs, audits = [], []
        for (sid, cid), corrective in backfill.CORRECTIVES.items():
            evs += row(sid, cid)
            audits.append(audit(sid, cid))
        actions = self.planned(evs, audits)
        corrections = [e for e in writes(actions) if e['type'] == 'owed-corrective']
        self.assertEqual(len(corrections), 9)
        self.assertEqual({e['corrective'] for e in corrections}, {'L-spec-0998', 'L-spec-0999', 'L-spec-1001'})
        for e in corrections:
            self.assertGreater(datetime.fromisoformat(e['ts']), NOW)
            self.assertIn(e['corrective'], e['reason'])
        for i in range(165):
            sid = f'L-spec-{9300+i}'
            evs += row(sid, status='waived' if i < 160 else 'due')
            audits.append(audit(sid, cls='CLOSE', verdict='CLOSE'))
        evs += row('L-spec-9600', capability='other', ask='read repository')
        audits.append(audit('L-spec-9600', verdict='COULD-NOT-RUN'))
        self.install(evs, audits)
        out = self.cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('CLOSE already waived: 160', out.stdout)
        self.assertIn('CLOSE not waived: 5', out.stdout)
        self.assertRegex(out.stdout, r'L-spec-9600.*COULD-NOT-RUN.*status=blocked.*capability=other')
        self.assertNotIn('owed-waived', [e['type'] for e in writes(self.planned(evs, audits))])
        print('nine correctives; corrective ts after failure; no waiver ever; could-not-run listed ok')

    def test_ac5_union(self):
        evs = sum((row(f'L-spec-{9700+i}', 'verify') for i in range(3)), [])
        self.install(evs, [])
        (self.root / 'state').mkdir()
        (self.root / 'state/sweep-owed-orphans.retired.json').write_text(json.dumps([f'L-spec-{9700+i}/verify' for i in (1,2,3,4)]))
        out = self.cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('5 vs 92: -87', out.stdout)
        self.assertEqual(sum(line.startswith('| `L-spec-97') for line in out.stdout.splitlines()), 5)
        self.assertIn('only open: L-spec-9700/verify', out.stdout)
        self.assertIn('only retired: L-spec-9703/verify, L-spec-9704/verify', out.stdout)
        (self.root / 'state/sweep-owed-orphans.retired.json').unlink()
        self.assertIn('retired-orphans: unavailable', self.cli().stdout)
        print('union rows; set difference stated ok')

    def test_ac6_relabel(self):
        capabilities = [('other','repository tests'), ('other','use browser'), ('other','PG_READ_ALL_STATS access'),
                        ('other',None), (None,'repository tests'), ('repo','repository tests'),
                        ('operator-action','repository tests'), ('ro-dsn','use browser')]
        evs = sum((row(f'L-spec-{9800+i}', capability=c, ask=a) for i,(c,a) in enumerate(capabilities)), [])
        evs += row('L-spec-9808', capability='other', ask='repository tests', status='met')
        actions = self.planned(evs, [])
        routed = [e for e in writes(actions) if e['type'] == 'owed-routed']
        self.assertEqual({e['subject'] for e in routed}, {f'L-spec-{9800+i}' for i in (0,1,2,4)})
        self.assertEqual(next(e for e in routed if e['subject']=='L-spec-9802')['capability'], 'operator-action')
        self.assertTrue(all('epoch' not in e and 'batch' not in e for e in routed))
        self.install(evs, [])
        labels = self.root / 'labels.json'
        labels.write_text(json.dumps({'L-spec-9800/AC1':'browser'}))
        out = self.cli('--apply', '--labels', str(labels))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('skipped `no ask, not relabelled`: 1', out.stdout)
        self.assertIn('non-open excluded from backfill / met: 1', out.stdout)
        self.assertIn('probe disagreements', out.stdout)
        self.assertNotIn('L-spec-9800', self.target.read_text())
        # The production probe indexes its archived source rows, not today's order.
        labels.write_text(json.dumps({'0':'browser'}))
        (self.root / 'unobs_open.json').write_text(json.dumps([{'spec':'L-spec-9800','crit':'AC1'}]))
        self.assertIn('probe disagreement', self.cli('--labels', str(labels)).stdout)
        print('relabel set; pg_read_all_stats operator-action; no ask listed; operator-action never downgraded; probe disagreements listed not applied ok')

    def test_ac7_atomic_validation(self):
        self.install(row(capability='other', ask='repository tests'), [audit()])
        self.env['DOIT_LEDGER_FILE'] = 'L-executor-0001.jsonl'
        before = self.snapshot()
        out = self.cli('--apply')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('owed-routed by executor', out.stdout + out.stderr)
        self.assertIn('would-be-refused', out.stdout)
        self.assertEqual(self.snapshot(), before)
        self.install(row('L-spec-0152', 'AC4'), [audit('L-spec-0152', 'AC4')])
        out = self.cli('--apply')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len((self.root / 'events/L-executor-0001.jsonl').read_text().splitlines()), 2)
        self.env['DOIT_LEDGER_FILE'] = self.target.name
        self.install(row(capability='other', ask='repository tests'), [audit()])
        self.assertEqual(self.cli('--apply').returncode, 0)
        self.assertTrue(all('actor' not in json.loads(line) for line in self.target.read_text().splitlines()))
        # A valid earlier plan entry must not land before a malformed later one.
        self.install(row() + row('L-spec-9002'), [audit(), audit('L-spec-9002', evidence='')])
        before = self.snapshot()
        out = self.cli('--apply')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('evidence', out.stdout + out.stderr)
        self.assertEqual(before, self.snapshot())
        print('refused under executor names owed-routed; executor applies failures only; applies under thinker; missing required field refused; no actor field ok')

    def test_ac8_round_trip(self):
        evs, audits = [], []
        for sid,cid in backfill.CORRECTIVES:
            evs += row(sid,cid)
            audits.append(audit(sid,cid))
        evs += row('L-spec-9900', capability='other', ask='pg_read_all_stats')
        audits.append(audit('L-spec-9900'))
        self.install(evs,audits)
        first = self.cli('--apply')
        self.assertEqual(first.returncode,0,first.stderr)
        recorded = [json.loads(line) for line in self.target.read_text().splitlines()]
        self.assertEqual({e['type'] for e in recorded}, {'owed-failed','owed-corrective','owed-routed'})
        second = self.cli()
        self.assertEqual(second.returncode,0,second.stderr)
        for sid,cid in backfill.CORRECTIVES:
            self.assertIn(f'{sid}/{cid} status=correcting',second.stdout)
        self.assertIn('L-spec-9900/AC1 status=failed',second.stdout)
        saved = self.target.read_bytes()
        again = self.cli('--apply')
        self.assertEqual(again.returncode,0,again.stderr)
        self.assertIn('pending writes: 0',again.stdout)
        self.assertIn('skipped-already-present:',again.stdout)
        self.assertEqual(self.target.read_bytes(),saved)
        print('round trip fresh process; nine read correcting; second apply writes zero ok')

    def test_ac10_report(self):
        self.install(row() + row('L-spec-9002',status='failed') + row('L-spec-9003', status='unverifiable'), [audit()])
        out = self.cli('--apply')
        self.assertEqual(out.returncode,0,out.stderr)
        report = (self.root/'content/L-owed-backfill-report.md').read_text()
        headings = ['## Before','## After','## Unverifiable','## Admission','## Audit outcomes','## Verify reconciliation','## Relabels','## Skipped audit classes']
        self.assertEqual(sorted(report.index(h) for h in headings),[report.index(h) for h in headings])
        self.assertEqual(report.count('open total: 2'),2)
        self.assertIn('unverifiable closes: before=1 after=1',report)
        self.assertIn('L-spec-9001/AC1 status=failed',report)
        self.assertIn('L-spec-9002/AC1 status=failed',report)
        self.assertFalse(list((self.root/'content').glob('.L-owed-backfill-report.md.*')))
        # Unsafe text is displayed once, bounded and contained in a code span.
        self.install(row(),[audit(how_to_check='unsafe`\n\x00'+'x'*500)])
        out=self.cli()
        self.assertNotIn('\x00',out.stdout)
        self.assertNotIn('x'*301,out.stdout)
        print('report sections in order; before and after sums; two specs one criterion id; unverifiable counted ok')

    def test_ac12_skipped_classes(self):
        audits=[audit(),audit('L-spec-9002',verdict='PASS'),audit('L-spec-9003',cls='WORTH-A-LOOK',verdict='FAIL'),
                audit('L-spec-9004',cls='WORTH-A-LOOK',verdict='WORTH-A-LOOK'),
                audit('L-spec-9005',cls='WORTH-A-LOOK',verdict='COULD-NOT-RUN')]
        actions=self.planned(row(),audits)
        self.assertEqual(len(writes(actions)),1)
        self.assertEqual(sum(a['section']=='skipped-class' for a in actions),4)
        self.install(row(),audits)
        out=self.cli('--apply')
        self.assertEqual(out.returncode,0,out.stderr)
        for label in ('CRITICAL PASS','WORTH-A-LOOK FAIL','WORTH-A-LOOK','WORTH-A-LOOK COULD-NOT-RUN'):
            self.assertIn(f'{label}: 1',out.stdout)
        self.assertNotIn('owed-met',self.target.read_text())
        print('skipped classes counted; no owed-met planned ok')

    def test_fail_closed_inputs(self):
        audit_path=self.root/'content/owed-audit/result-fixture.json'
        for data in ('{','{}','[{}]'):
            audit_path.write_text(data)
            before=self.snapshot()
            out=self.cli('--apply')
            self.assertNotEqual(out.returncode,0)
            self.assertEqual(before,self.snapshot())
        self.install(row(),[audit()])
        for directory in (self.root/'events',self.root/'content/owed-audit'):
            directory.chmod(0)
            try:
                self.assertNotEqual(self.cli('--apply').returncode,0)
            finally:
                directory.chmod(0o700)
        outside=self.root/'outside.json'
        outside.write_text(json.dumps([audit()]))
        audit_path.unlink()
        audit_path.symlink_to(outside)
        self.assertNotEqual(self.cli('--apply').returncode,0)


if __name__=='__main__':
    unittest.main()
