"""Scratch-only contracts for the recorded Planner bar; no production inputs."""
import copy
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from planner_bar import planner_bar, read_events

DAY = '2026-01-03'
BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
END = 259200


def ts(n):
    return (BASE + dt.timedelta(seconds=n)).isoformat().replace('+00:00', 'Z')


def event(n, kind, who='L-planner-0001', charter='L-charter-0001', **kw):
    row = dict(ts=ts(n), type=kind, subject=charter, planner=who,
               _src=who + '.jsonl:' + str(int(n + 10000)))
    if kind == 'planner-started':
        row['mode'] = 'charter'
    row.update(kw)
    return row


def ticks(step=600):
    return [dict(ts=ts(n), type='tick', planner_status='idle', planner_ready=[],
                 _src='L-tick-local.jsonl:' + str(i + 1))
            for i, n in enumerate(range(-step, END + step + 1, step))]


def samples(step=600):
    return [dict(ts=ts(n), wall=step, by={}, top=[]) for n in range(step, END + 1, step)]


def check(events=(), meter=None, name='pickup'):
    return planner_bar(list(events), samples() if meter is None else meter, DAY)['checks'][name]


class WindowContract(unittest.TestCase):
    def test_window_purity_and_conjunction(self):
        ev, sm = ticks(), samples()
        before = copy.deepcopy((ev, sm))
        result = planner_bar(ev, sm, DAY)
        self.assertEqual(result['window'], dict(start=ts(0), end=ts(END), end_day=DAY))
        self.assertTrue(result['success'])
        self.assertEqual((ev, sm), before)
        self.assertEqual(json.dumps(result, sort_keys=True), json.dumps(planner_bar(reversed(ev), sm, DAY), sort_keys=True))
        self.assertFalse(planner_bar(ev, [], DAY)['success'])
        self.assertEqual(planner_bar([], [], '2026-01-01')['window']['start'], '2025-12-30T00:00:00Z')
        offset = copy.deepcopy(ev)
        offset[1]['ts'] = '2026-01-01T01:00:00+01:00'
        self.assertEqual(planner_bar(ev, sm, DAY), planner_bar(offset, sm, DAY))
        with self.assertRaises(ValueError):
            planner_bar(ev, sm, '2026-02-30')
        outside = [event(END + 1, 'planner-started'), event(END + 2, 'planner-started', who='L-planner-0002')]
        self.assertTrue(planner_bar(ev + outside, sm, DAY)['success'])
        self.assertFalse(planner_bar(ev + [event(1, 'planner-started')], sm, DAY)['success'])
        malformed = event(100, 'progress', ts='invalid')
        result = planner_bar(ev + [event(1, 'planner-started'), malformed], sm, DAY)
        self.assertEqual(result['checks']['dialog']['status'], 'undetermined')


class PickupContract(unittest.TestCase):
    def ready(self, n, ids=None):
        return dict(ts=ts(n), type='tick', planner_status='idle',
                    planner_ready=ids or ['L-charter-0001'], _src='L-tick-local.jsonl:9000')

    def test_deadlines_and_persistent_clock(self):
        for latency in (0, 1199, 1200, 1201):
            with self.subTest(latency=latency):
                ev = ticks() + [self.ready(10), self.ready(50), self.ready(70, ['L-charter-0002']),
                                event(10 + latency, 'planner-started'), event(2000, 'planner-ended')]
                result = check(ev)
                ep = next(e for e in result['evidence'] if e['charter'] == 'L-charter-0001')
                self.assertEqual(ep['ready_at'], ts(10))
                self.assertEqual(ep['latency_seconds'], latency)
                self.assertEqual(ep['status'], 'fail' if latency > 1200 else 'pass')
        for age, status in ((1199, 'undetermined'), (1200, 'fail')):
            self.assertEqual(check(ticks() + [self.ready(END - age)])['status'], status)
        r = check(ticks() + [self.ready(-100), event(1200, 'planner-started')])
        self.assertEqual(r['status'], 'fail')
        self.assertEqual(r['evidence'][0]['ready_at'], ts(-100))

    def test_live_queue_and_history(self):
        ev = ticks() + [event(-20, 'planner-started'), event(1000, 'planner-ended'), self.ready(10)]
        self.assertEqual(check(ev)['evidence'], [])
        unknown = ticks() + [event(-20, 'planner-started'), self.ready(10)]
        self.assertEqual(check(unknown)['status'], 'undetermined')
        for step in (337, 700, 1104, 1200, 1201):
            self.assertEqual(check(ticks(step))['status'], 'undetermined' if step > 1200 else 'pass')
        for ev in ([r for r in ticks() if r['ts'] > ts(0)],
                   [r for r in ticks() if r['ts'] < ts(END)]):
            self.assertEqual(check(ev)['status'], 'undetermined')
        for replacement in ({'planner_ready': None}, {'planner_status': 'undetermined'}, {'planner_status': {}}, {'planner_status': 'alive', 'planner_ready': ['L-charter-0001']}):
            ev = ticks()
            ev[3].update(replacement)
            self.assertEqual(check(ev)['status'], 'undetermined')
        ev = ticks()
        del ev[3]['planner_ready']
        self.assertEqual(check(ev)['status'], 'undetermined')


class OverlapContract(unittest.TestCase):
    def pair(self, end=500, reason='exit-0', activity=False, other='L-charter-0001'):
        ev = [event(-100, 'planner-started'), event(100, 'planner-started', who='L-planner-0002', charter=other),
              event(700, 'planner-ended', who='L-planner-0002', charter=other)]
        if end is not None:
            ev.append(event(end, 'planner-ended', reason=reason))
        if activity:
            ev.append(event(200, 'progress'))
        return ev

    def test_known_and_uncertain_intersections(self):
        for end, reason, activity, status in ((500, 'exit-0', False, 'fail'),
                                              (100, 'ctx', False, 'pass'),
                                              (500, 'never-started', False, 'pass'),
                                              (500, 'vanished', False, 'undetermined'),
                                              (500, 'vanished', True, 'fail'),
                                              (None, '', False, 'undetermined'),
                                              (None, '', True, 'fail')):
            ev = self.pair(end, reason, activity)
            self.assertEqual(check(ev, name='overlap')['status'], status)
            self.assertEqual(check(ev, name='overlap'), check(reversed(ev), name='overlap'))
        self.assertEqual(check(self.pair(other='L-charter-0002'), name='overlap')['status'], 'pass')
        ev = [event(-100, 'planner-started'), event(-50, 'planner-started', who='L-planner-0002'),
              event(10, 'planner-ended'), event(20, 'planner-ended', who='L-planner-0002')]
        self.assertEqual(check(ev, name='overlap')['status'], 'fail')
        for ev in ([event(1, 'planner-ended')], [event(1, 'planner-started', spawn='different')],
                   [event(1, 'planner-started', mode=None)]):
            self.assertEqual(check(ev, name='overlap')['status'], 'undetermined')
        self.assertEqual(check([event(0, 'planner-started', mode='serving')], name='overlap')['status'], 'pass')
        disjoint = self.pair(end=50, reason='vanished')
        self.assertEqual(check(disjoint, name='overlap')['status'], 'pass')


class DialogContract(unittest.TestCase):
    def test_historical_threshold_and_sources(self):
        for age in (899, 900, 901):
            self.assertEqual(check([event(END-age, 'planner-started')], name='dialog')['status'],
                             'undetermined' if age == 899 else 'fail')
        for when, status in ((899, 'pass'), (901, 'fail')):
            for kind in ('progress', 'planner-ended'):
                self.assertEqual(check([event(0, 'planner-started'), event(when, kind)], name='dialog')['status'], status)
        for reason in ('never-started', 'vanished'):
            self.assertEqual(check([event(-100, 'planner-started'), event(1000, 'planner-ended', reason=reason)], name='dialog')['status'], 'fail')
        self.assertEqual(check([event(0, 'planner-started'), event(1, 'progress', who='L-planner-0002')], name='dialog')['status'], 'fail')
        self.assertEqual(check([event(0, 'planner-started', _src=None)], name='dialog')['status'], 'undetermined')
        self.assertEqual(check([event(0, 'planner-started', mode='serving')], name='dialog')['status'], 'pass')


class CpuContract(unittest.TestCase):
    def test_attribution(self):
        for cmd, status in (('python src/up.py planner', 'fail'), ('doit up', 'fail'),
                            ('python src/up.py relay', 'undetermined'),
                            ('python src/planner_run.py', 'undetermined')):
            sm = samples()
            sm[0].update(by={'other': 1}, top=[dict(cpu=1, cat='other', cmd=cmd)])
            self.assertEqual(check(ticks(), sm, 'idle_cpu')['status'], status)
        for top in ([], [dict(cpu=1, cat='other', cmd='worker')] * 8):
            sm = samples()
            sm[0].update(by={'other': 8}, top=top)
            self.assertEqual(check(ticks(), sm, 'idle_cpu')['status'], 'undetermined')
        self.assertEqual(check(ticks(), samples(), 'idle_cpu')['status'], 'pass')
        self.assertEqual(check(ticks() + [event(-100, 'planner-started'), event(END, 'planner-ended')], samples(), 'idle_cpu')['status'], 'undetermined')
        sm = samples()
        ev = ticks() + [event(100, 'planner-started'), event(200, 'planner-ended')]
        self.assertEqual(check(ev, sm, 'idle_cpu')['status'], 'undetermined')

    def test_coverage_and_invalid_samples(self):
        for wall, status in ((599, 'pass'), (598.9, 'undetermined'), (600.9, 'pass'), (601.5, 'pass'), (602, 'undetermined')):
            sm = samples()
            sm[1]['wall'] = wall
            self.assertEqual(check(ticks(), sm, 'idle_cpu')['status'], status)
        for sm in ([], samples()[:-1], samples()[1:], list(reversed(samples())), samples() + samples()[-1:]):
            self.assertEqual(check(ticks(), sm, 'idle_cpu')['status'], 'undetermined')
        for change in ({'ts': 'bad'}, {'wall': 0}, {'wall': True}, {'by': None},
                       {'by': {'x': -1}}, {'by': {'x': True}}, {'by': {'x': float('nan')}},
                       {'top': [{'cpu': float('inf')}]}, {'top': [{'cpu': -1}]},
                       {'top': [{'cpu': True}]}):
            sm = samples()
            sm[2].update(change)
            self.assertEqual(check(ticks(), sm, 'idle_cpu')['status'], 'undetermined')


class CliContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.meter = self.root / 'samples.jsonl'
        self.write_events(ticks())
        self.meter.write_text(''.join(json.dumps(r) + '\n' for r in samples()))

    def write_events(self, rows):
        groups = {}
        for row in rows:
            row = dict(row)
            name = row.pop('_src').split(':')[0]
            groups.setdefault(name, []).append(row)
        for name, group in groups.items():
            (self.events / name).write_text(''.join(json.dumps(r) + '\n' for r in group))

    def inventory(self, root):
        result = {}
        for p in root.rglob('*'):
            try:
                content = p.read_bytes() if p.is_file() else None
            except PermissionError:
                content = 'unreadable'
            result[str(p.relative_to(root))] = (p.stat().st_mode, content)
        return result

    def run_cli(self, extra=(), code=None, sources=None):
        script = Path(__file__).with_name('planner_bar.py')
        cmd = [sys.executable, str(script), '--end-day', DAY, '--events', str(self.events)]
        for source in sources or [self.meter]:
            cmd += ['--samples', str(source)]
        cmd += list(extra)
        if code is not None:
            cmd = [sys.executable, '-B', '-c', code] + cmd[2:]
        before = self.inventory(self.root), self.inventory(script.parent)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env={**os.environ, 'DOIT_ROOT': str(self.root), 'HOME': str(self.root),
                                   'PYTHONPATH': str(script.parent)}, timeout=20)
        self.assertEqual(before, (self.inventory(self.root), self.inventory(script.parent)))
        if proc.returncode != 2:
            value = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 0 if value['success'] else 1)
            self.assertNotIn('SECRET-CANARY', proc.stdout)
            return value
        return proc

    def test_clean_errors_and_each_violation(self):
        self.assertTrue(self.run_cli()['success'])
        for name, ev in (('pickup', [dict(ts=ts(10), type='tick', planner_status='idle', planner_ready=['L-charter-0001'], _src='extra.jsonl:1')]),
                         ('overlap', OverlapContract().pair()),
                         ('dialog', [event(0, 'planner-started', cmd='SECRET-CANARY')])):
            self.write_events(ev)
            self.assertEqual(self.run_cli()['checks'][name]['status'], 'fail')
            for p in self.events.iterdir():
                p.unlink()
            self.write_events(ticks())
        sm = samples()
        sm[0].update(by={'other': 1}, top=[dict(cpu=1, cmd='python up.py planner SECRET-CANARY')])
        self.meter.write_text(''.join(json.dumps(r) + '\n' for r in sm))
        self.assertEqual(self.run_cli()['checks']['idle_cpu']['status'], 'fail')
        self.assertEqual(self.run_cli(['--end-day', 'bad']).returncode, 2)
        self.assertEqual(self.run_cli(['--unknown']).returncode, 2)
        for text in ('{}\n{', '[]\n', '{bad}\n'):
            (self.events / 'bad.jsonl').write_text(text)
            self.assertFalse(self.run_cli()['success'])
        (self.events / 'bad.jsonl').unlink()
        code = "import runpy,unittest.mock,ledger_files; p=unittest.mock.patch.object(ledger_files,'read_text',side_effect=OSError); p.start(); runpy.run_module('planner_bar',run_name='__main__')"
        self.assertFalse(self.run_cli(code=code)['success'])
        self.write_events([event(1, 'planner-started')])
        (self.events / 'unreadable.jsonl').write_text('{}\n')
        (self.events / 'unreadable.jsonl').chmod(0)
        self.assertEqual(self.run_cli()['checks']['dialog']['status'], 'fail')
        (self.events / 'unreadable.jsonl').chmod(0o600)
        self.assertFalse(self.run_cli(['--events', str(self.root / 'absent')])['success'])

    def test_rotation_and_damage(self):
        rows = samples()
        rotated = self.root / 'samples.jsonl.gz'
        with gzip.open(rotated, 'wt') as f:
            f.write(''.join(json.dumps(r) + '\n' for r in rows[:200]))
        self.meter.write_text(''.join(json.dumps(r) + '\n' for r in rows[200:]))
        self.assertTrue(self.run_cli(sources=[rotated, self.meter])['success'])
        self.assertFalse(self.run_cli()['success'])
        self.assertFalse(self.run_cli(sources=[self.meter, rotated])['success'])
        self.assertFalse(self.run_cli(sources=[rotated, rotated, self.meter])['success'])
        rotated.write_bytes(rotated.read_bytes()[:-8])
        self.assertFalse(self.run_cli(sources=[rotated, self.meter])['success'])
        rotated.write_bytes(b'bad gzip')
        self.assertFalse(self.run_cli(sources=[rotated])['success'])
        rotated.write_bytes(b'\x1f\x8b\x08\x00' + b'\0' * 6 + b'\x07' + b'\0' * 8)
        self.assertFalse(self.run_cli(sources=[rotated])['success'])
        self.meter.write_text(''.join(json.dumps(r) + '\n' for r in rows[:-1]))
        self.assertEqual(self.run_cli()['checks']['idle_cpu']['status'], 'undetermined')

    def test_incomplete_history_cannot_prove_absence(self):
        self.write_events([event(1, 'planner-started')])
        path = self.events / 'L-planner-0001.jsonl'
        path.write_text(path.read_text() + '{torn activity')
        result = self.run_cli()
        self.assertEqual(result['checks']['dialog']['status'], 'undetermined')
        self.write_events(OverlapContract().pair())
        (self.events / 'unrelated.jsonl').write_text('{bad}\n')
        self.assertEqual(self.run_cli()['checks']['overlap']['status'], 'fail')

    def test_virtual_sources_and_strict_integrity(self):
        rows = [event(10, 'planner-started'), event(20, 'planner-ended')]
        self.write_events(rows)
        expected = self.run_cli()
        name = 'L-planner-0001.jsonl'
        lines = (self.events / name).read_bytes().splitlines(keepends=True)
        packed = lines[0]
        directory = self.events / 'segments'
        directory.mkdir()
        segment = directory / 'a.jsonl'
        segment.write_bytes(packed)
        member = dict(name=name, offset=0, length=len(packed), lines=1,
                      sha256=hashlib.sha256(packed).hexdigest(), ends_nl=True)
        index = directory / 'a.idx.json'
        index.write_text(json.dumps(dict(version=1, segment='a.jsonl', bytes=len(packed),
                                        sha256=hashlib.sha256(packed).hexdigest(), members=[member])))
        (self.events / name).write_bytes(lines[1])
        self.assertEqual(expected, self.run_cli())
        original = index.read_text()
        damaged = json.loads(original)
        damaged['sha256'] = '0' * 64
        index.write_text(json.dumps(damaged))
        self.assertFalse(self.run_cli()['success'])
        index.write_text(original)
        pending = directory / '.pending'
        pending.mkdir()
        (pending / 'L-planner-9999.jsonl').write_text('{}\n')
        self.assertFalse(self.run_cli()['success'])
        (pending / 'L-planner-9999.jsonl').unlink()
        index.write_text('{}')
        self.assertFalse(self.run_cli()['success'])
        index.unlink()
        self.assertFalse(self.run_cli()['success'])


if __name__ == '__main__':
    unittest.main()
