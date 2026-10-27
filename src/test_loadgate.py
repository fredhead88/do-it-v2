#!/usr/bin/env python3
"""Hermetic admission observations; run directly with stdlib unittest."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

_isolation = tempfile.TemporaryDirectory(prefix='loadgate-tests-')
os.environ['HOME'] = str(Path(_isolation.name) / 'home')
os.environ['DOIT_ROOT'] = str(Path(_isolation.name) / 'root')
os.environ.pop('DOIT_PROJECT', None)
import fold
assert fold.ROOT == Path(os.environ['DOIT_ROOT'])
import loadgate as lg


def fixture(root, mem=20):
    proc, cg = root / 'proc', root / 'cgroup'
    files = {'meminfo': f'MemAvailable: {int(mem * 2**20)} kB\nSwapFree: {8 * 2**20} kB\nShmem: {2**20} kB\n',
             'loadavg': '8.00 4.00 2.00 3/2500 123\n',
             'vmstat': 'pswpin 2000\npswpout 3000\n', 'stat': 'cpu 1 2 3\nprocs_running 3\n'}
    psi = 'some avg300=0.3 total=123 avg10=0.1 avg60=0.2\nfull avg60=0.02 avg10=0.01 total=0 avg300=0.03\n'
    for source in ('memory', 'cpu', 'io'):
        files['pressure/' + source] = psi
    for name, content in files.items():
        p = proc / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cron = cg / 'system.slice/cron.service'
    cron.mkdir(parents=True)
    (cron / 'memory.pressure').write_text(psi)
    (cron / 'memory.current').write_text(str(2**30))
    return proc, cg


def signed(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://loadgate/' + ac, status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


class LoadgateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proc, self.cg = fixture(self.root)

    def begin(self, **kw):
        return lg.begin(root=self.root, env={}, proc_root=self.proc, cgroup_root=self.cg, ncpu=8, **kw)

    def test_AC1_sources_and_fault_isolation(self):
        r = lg.readings(self.proc, cgroup_root=self.cg, ncpu=8)
        for key, value in dict(mem_avail_gb=20, swap_free_gb=8, shmem_gb=1, load1=8,
                               load5=4, load15=2, load1_per_core=1, procs_running=3,
                               cron_mem_gb=1, cron_mem_some10=.1, cron_mem_full10=.01).items():
            self.assertEqual(r[key], value, key)
        for prefix in ('mem', 'cpu', 'io'):
            for line, values in (('some', (.1, .2, .3)), ('full', (.01, .02, .03))):
                for avg, value in zip((10, 60, 300), values):
                    self.assertEqual(r[f'{prefix}_{line}{avg}'], value)
        self.assertEqual(r['unreadable'], [])
        shutil.rmtree(self.proc / 'pressure')
        r = lg.readings(self.proc, cgroup_root=self.cg)
        self.assertEqual(r['unreadable'], ['pressure/cpu', 'pressure/io', 'pressure/memory'])
        self.assertTrue(all(r[f'{p}_{s}{a}'] is None for p in ('mem', 'cpu', 'io')
                            for s in ('some', 'full') for a in (10, 60, 300)))
        (self.proc / 'meminfo').write_text('garbage')
        r = lg.readings(self.proc, cgroup_root=self.cg)
        self.assertIn('meminfo', r['unreadable'])
        self.assertTrue(all(r[k] is None for k in ('mem_avail_gb', 'swap_free_gb', 'shmem_gb')))
        r = lg.readings(self.root / 'missing', cgroup_root=self.cg)
        self.assertEqual(len(r['unreadable']), 7)
        self.assertEqual(r['cron_mem_gb'], 1)
        r = lg.readings(self.root / 'missing', cgroup_root=self.root / 'missing')
        self.assertEqual(len(r['unreadable']), 8)
        signed('AC1', dict(sources=9, psi_values=18, faults_isolated=True))

    def test_AC2_swap_rates(self):
        for prev, expected in ((dict(t=990, pswpin=1000, pswpout=2000), 100.),
                               (None, None), (dict(t=1000, pswpout=2000), None),
                               (dict(t=-3000, pswpout=2000), None),
                               (dict(t=990, pswpout=4000), None)):
            r = lg.readings(self.proc, cgroup_root=self.cg, prev=prev, clock=lambda: 1000.)
            self.assertEqual(r['pswpout_s'], expected)
            self.assertEqual(r['unreadable'], [])
        signed('AC2', dict(rate=100., invalid_gaps_fail_open=True))

    def test_AC3_decision_matrix(self):
        calm = dict(mem_avail_gb=20, swap_free_gb=8, load1_per_core=1, mem_some10=0, mem_full10=0)
        cases = [(dict(mem_avail_gb=7), 'mem-hard-stop'),
                 (dict(mem_avail_gb=10, swap_free_gb=1), 'mem-swap-hard-stop'),
                 (dict(load1_per_core=3.5), 'load-delay'), (dict(load1_per_core=4.5), 'load-stop')]
        for changes, reason in cases:
            self.assertEqual(lg.admit('builder', calm | changes)[:2], (False, reason))
        for mem, ok in ((8.4, False), (8.6, True)):
            result = lg.admit('builder', calm | dict(mem_avail_gb=mem), {'builder': 8})
            self.assertEqual(result[0], ok)
            self.assertEqual(result[2]['need_gb'], 8.5)
        self.assertEqual(lg.admit('probe', calm, {'builder': 8})[2]['need_gb'], 8)
        self.assertEqual(lg.admit('probe', calm)[2]['unknown_roles'], [])
        self.assertIn('sweep_owed', lg.admit('sweep_owed', calm)[2]['unknown_roles'])
        self.assertEqual(lg.admit('tick', calm | dict(mem_avail_gb=1))[:2], (True, 'bypass'))
        self.assertEqual(lg.admit('builder', None)[:2], (True, 'unreadable'))
        self.assertEqual(lg.admit('builder', dict.fromkeys(calm), state={'psi_tripped': True})[:2], (True, 'unreadable'))
        self.assertEqual(lg.admit('builder', calm, state={'psi_tripped': True})[1], 'mem-pressure')
        result = lg.admit('builder', calm | dict(mem_some10=None, mem_full10=None), state={'psi_tripped': True})
        self.assertTrue(result[0])
        self.assertIn('mem-pressure', result[2]['skipped'])
        self.assertTrue(lg.admit('builder', calm | dict(swap_free_gb=0, cpu_some10=95, io_some10=95, pswpout_s=10000))[0])
        result = lg.admit('builder', calm | dict(mem_avail_gb=1, swap_free_gb=0, load1_per_core=5), state={'psi_tripped': True})
        self.assertEqual(result[2]['failed'], list(lg.RULES))
        signed('AC3', dict(ordered_rules=result[2]['failed'], headroom=8.5))

    def test_AC4_hysteresis(self):
        for line, high, low, reset in (('some', 6, 1, 6), ('full', 2, .5, 2)):
            r = dict(mem_some10=0, mem_some60=0, mem_full10=0, mem_full60=0)
            state = lg.advance({}, r | {f'mem_{line}10': high}, lg.DEFAULTS)
            self.assertTrue(state['psi_tripped'])
            calm = r | {f'mem_{line}10': low, f'mem_{line}60': 4 if line == 'some' else .5}
            first = lg.advance(state, calm, lg.DEFAULTS)
            self.assertEqual(first['psi_release_streak'], 1)
            self.assertFalse(lg.advance(first, calm, lg.DEFAULTS)['psi_tripped'])
            self.assertEqual(lg.advance(first, calm | {f'mem_{line}60': reset}, lg.DEFAULTS)['psi_release_streak'], 0)
            self.assertEqual(lg.advance(first, dict.fromkeys(r), lg.DEFAULTS), first)
            self.assertEqual(lg.advance(first, calm | {f'mem_{line}60': None}, lg.DEFAULTS), first)
        signed('AC4', dict(some_and_full=True, release_readings=2, missing_unchanged=True))

    def test_AC5_settings(self):
        p = self.root / 'look.toml'
        def setting(text, env=None):
            p.write_text('[admission]\n' + text)
            return lg.settings(env=env or {}, toml_path=p)
        self.assertEqual(setting('mode="off"', {'DOIT_ADMISSION': 'observe'})[0]['mode'], 'observe')
        self.assertEqual(setting('mode="off"', {'DOIT_ADMISSION': ''})[0]['mode'], 'off')
        for text, env in (('mode="off"', {'DOIT_ADMISSION': 'ENFORCE '}), ('mode="enforce"', {})):
            s, errors = setting(text, env)
            self.assertEqual(s['mode'], 'observe')
            self.assertIn('enforce only via $DOIT_ROOT/admission.toml; observing', errors)
        self.assertIn('bad mode', setting('mode=1', {'DOIT_ADMISSION': 'off'})[1])
        self.assertIn('bad mode', setting('mode="bogus"')[1])
        for key, value in (('mem_floor_gb', '"x"'), ('mem_hard_stop_gb', 'true'), ('mem_hard_stop_gb', '-1'), ('release_readings', '0')):
            s, errors = setting(key + '=' + value)
            self.assertEqual(s[key], lg.DEFAULTS[key])
            self.assertIn('bad ' + key, errors)
        s, errors = setting('max_wait_min=600')
        self.assertEqual(s['max_wait_min'], 60)
        self.assertIn('max_wait_min capped at 60', errors)
        self.assertEqual(lg.settings(env={}, toml_path=self.root / 'absent'), (lg.DEFAULTS, []))
        p.write_text('[')
        self.assertEqual(lg.settings(env={}, toml_path=p), (lg.DEFAULTS, ['toml unreadable']))
        self.assertEqual(lg.settings(env={}, toml_path=Path(__file__).resolve().parent.parent / 'look.toml'), (lg.DEFAULTS, []))
        for bad in ('nan', 'inf', '1' + '0' * 400):
            self.assertIn('bad mem_floor_gb', setting('mem_floor_gb=' + bad)[1])
        signed('AC5', dict(defaults_and_validation=True, enforce_observes=True))

    def test_hooks_and_oversize_fallbacks(self):
        env = dict(DOIT_ADMISSION_TEST_PROC=str(self.proc), DOIT_ADMISSION_TEST_CGROUP=str(self.cg),
                   DOIT_ADMISSION_TEST_NCPU='8', DOIT_ADMISSION_TEST_TOML=str(self.root / 'absent'))
        with patch.object(lg, 'readings', wraps=lg.readings) as sampler:
            g = lg.begin(root=self.root, env=env)
            self.assertEqual(sampler.call_args.args[0], '/proc')
            self.assertFalse(g.test_hooks)
        env['DOIT_ADMISSION_TEST'] = '1'
        g = lg.begin(root=self.root, env=env)
        self.assertEqual(g.readings['mem_avail_gb'], 20)
        self.assertTrue(g.finish()['admission_test_hooks'])
        with patch.object(lg, 'readings', wraps=lg.readings) as sampler:
            lg.begin(root=self.root, env=env, proc_root=self.root / 'explicit', ncpu=4)
            self.assertEqual(sampler.call_args.args[0], self.root / 'explicit')
            self.assertEqual(sampler.call_args.kwargs['ncpu'], 4)
        g = self.begin()
        g.readings['mem_avail_gb'] = 1e100
        g.trips = list(lg.RULES)
        g.errors = ['x' * 160]
        g.problems = ['x' * 60] * 5
        g.unreadable = ['cgroup', 'meminfo', 'pressure/memory', 'pressure/cpu', 'pressure/io', 'loadavg', 'vmstat', 'stat', 'state']
        f = g.finish()
        self.assertEqual(f, dict(admission_mode='observe', admission_error='oversize'))
        with patch.object(lg, 'save_state', side_effect=OSError('disk')):
            self.assertIn('disk', self.begin().finish()['admission_error'])

    def test_AC6_state_round_trip_and_bounds(self):
        p = self.root / 'state/admission.json'
        for content in (None, 'x' * 20480, 'garbage', '[]'):
            if content is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(exist_ok=True)
                p.write_text(content)
            state, usable = lg.load_state(p)
            self.assertFalse(usable)
            g = self.begin()
            fields = g.finish()
            self.assertEqual('state' in fields.get('admission_unreadable', []), content is not None)
            self.assertTrue(lg.load_state(p)[1])
            self.assertEqual(list(p.parent.glob('*.tmp')), [])
        self.assertEqual(lg.hold_key('a' * 80), 'a' * 80)
        for name in ('é', 'a' * 78 + '"', 'a' * 200):
            key = lg.hold_key(name)
            self.assertRegex(key, r'^[ -!#-\[\]-~]*#[0-9a-f]{8}$')
            self.assertLessEqual(len(key), 80)
        names = ['a' * 150 + str(i).zfill(50) for i in range(1000)]
        self.assertEqual(len(set(map(lg.hold_key, names))), 1000)
        (self.proc / 'meminfo').write_text('MemAvailable: 7340032 kB\nSwapFree: 0 kB\n')
        g = self.begin(clock=iter([1000.] * 1001 + [1090.]).__next__)
        for name in names:
            self.assertTrue(g.check(name, 'builder').would_hold)
        g.finish()
        first = json.loads(p.read_text())
        self.assertEqual(len(first['holding']), 64)
        self.assertLess(p.stat().st_size, 16384)
        name = min(names, key=lg.hold_key)
        key = lg.hold_key(name)
        g = self.begin(clock=iter([1150., 1150., 1150.]).__next__)
        g.check(name, 'builder')
        fields = g.finish()
        second, usable = lg.load_state(p)
        self.assertTrue(usable)
        self.assertEqual(second['holding'][key], first['holding'][key])
        self.assertGreaterEqual(fields['admission_oldest_hold_s'], 60)
        for bad in (dict(t=True), dict(holding={'x': '2026-01-01'}), dict(trip_since='2026-01-01T01:00:00+01:00')):
            p.write_text(json.dumps(second | bad))
            self.assertFalse(lg.load_state(p)[1])
        signed('AC6', dict(retained=64, bytes=len(json.dumps(first)), oldest_hold=fields['admission_oldest_hold_s'], independent_round_trip=True))

    def test_AC7_fail_open_no_external_work_and_cost(self):
        with patch.object(lg, 'readings', side_effect=RuntimeError('sample')):
            g = self.begin()
            self.assertFalse(g.check('x', 'builder').would_hold)
            f = g.finish()
            self.assertNotIn('admission_sampled_at', f)
            self.assertIn('sample', f['admission_error'])
        with patch.object(lg, 'readings') as sample:
            self.assertIsNone(lg.begin(root=self.root / 'off', env={'DOIT_ADMISSION': 'off'}))
            sample.assert_not_called()
            self.assertFalse((self.root / 'off/state').exists())
        with patch('subprocess.run', side_effect=AssertionError('subprocess')), \
             patch('subprocess.Popen', side_effect=AssertionError('subprocess')), \
             patch.object(fold, 'read_events', side_effect=AssertionError('ledger')), \
             patch.object(fold, 'fold', side_effect=AssertionError('fold')):
            start = time.monotonic()
            g = self.begin()
            for i in range(3):
                g.check(str(i), 'builder')
                g.note_launch(str(i), True)
            f = g.finish()
            self.assertLess(time.monotonic() - start, .5)
            self.assertNotIn('admission_error', f)
            self.assertIsInstance(f['admission_cost_ms'], int)
        # Nested wrappers count each physical open once, including Path.read_text.
        opened, active = [], set()
        from contextlib import ExitStack
        import builtins
        def wrap(original):
            def call(path, *args, **kwargs):
                name = str(path)
                outer = name not in active
                if outer:
                    opened.append(name)
                    active.add(name)
                try:
                    return original(path, *args, **kwargs)
                finally:
                    if outer:
                        active.remove(name)
            return call
        with ExitStack() as stack:
            for obj, attr in ((builtins, 'open'), (io, 'open'), (os, 'open'), (Path, 'open'), (Path, 'read_text')):
                stack.enter_context(patch.object(obj, attr, wrap(getattr(obj, attr))))
            lg.readings(self.proc, cgroup_root=self.cg)
        self.assertEqual(len(opened), 9)
        self.assertEqual(len(set(opened)), 9)
        signed('AC7', dict(opens=len(opened), cost_ms=f['admission_cost_ms'], fail_open=True))

    def test_AC13_exact_clock(self):
        def clock(values):
            calls = []
            def now():
                self.assertLess(len(calls), len(values))
                calls.append(values[len(calls)])
                return calls[-1]
            return now, calls
        c, calls = clock([1000., 1060., 1180., 1300., 1310.])
        g = self.begin(clock=c)
        for i in range(3):
            g.check(str(i), 'builder')
            g.note_launch(str(i), True)
        f = g.finish()
        self.assertEqual(len(calls), 5)
        self.assertEqual(f['admission_reading_age_s'], 300)
        self.assertEqual(f['admission_sampled_at'], '1970-01-01T00:16:40+00:00')
        c, calls = clock([1000., 1010.])
        self.assertNotIn('admission_reading_age_s', self.begin(clock=c).finish())
        self.assertEqual(len(calls), 2)
        state, _ = lg.load_state(self.root / 'state/admission.json')
        state['trip_since'] = '1970-01-01T00:06:40+00:00'
        lg.save_state(self.root / 'state/admission.json', state)
        (self.proc / 'meminfo').write_text('MemAvailable: 7340032 kB\nSwapFree: 0 kB\n')
        c, calls = clock([1000., 1060., 1090.])
        g = self.begin(clock=c)
        g.check('item', 'builder')
        f = g.finish()
        self.assertEqual(f['admission_trip_s'], 600)
        self.assertEqual(f['admission_oldest_hold_s'], 0)
        saved, usable = lg.load_state(self.root / 'state/admission.json')
        self.assertTrue(usable)
        self.assertEqual(saved['t'], 1000.)
        self.assertEqual(saved['trip_since'], state['trip_since'])
        self.assertEqual(saved['holding']['item'], '1970-01-01T00:18:10+00:00')
        self.assertEqual(len(calls), 3)
        signed('AC13', dict(age_s=300, trip_s=600, saved_t=saved['t'], exact_calls=True))


if __name__ == '__main__':
    unittest.main()
