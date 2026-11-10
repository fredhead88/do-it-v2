#!/usr/bin/env python3
"""Exercise quiet reporting through disposable copies of the real launcher."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent

# A receipt is independent of console output and survives launcher cleanup.
PY_CHILD = '''import json, os, pathlib, sys, tempfile
name = pathlib.Path(__file__).name
status = int(os.environ.get('FIXTURE_STATUS_' + name, '0'))
scratch = tempfile.mkdtemp()
record = {'name': name, 'argv': sys.argv[1:], 'scratch': scratch,
          'env': {k: v for k, v in os.environ.items()
                  if k in ('HOME', 'DOIT_ROOT', 'XDG_CONFIG_HOME', 'DOIT_LEDGER_FILE',
                           'DOIT_TEST_PROCESS', 'DOIT_TEST_MACHINE_ROOT',
                           'DOIT_TEST_LIVE_ROOT', 'TMPDIR', 'TMP', 'TEMP')
                  or k.startswith('GIT_')}}
if name == 'test_grader_view.py':
    import subprocess
    repo = pathlib.Path(__file__).resolve().parent.parent
    subprocess.run(['git', '-C', str(repo), 'merge-base', 'HEAD', 'master'], check=True,
                   stdout=subprocess.DEVNULL)
    record['grader_repo'] = str(repo)
with open(os.environ['FIXTURE_RECEIPTS'], 'a') as out:
    out.write(json.dumps(record) + '\\n')
label = ('BAD' if status else 'GOOD') + '-' + name
print(label + '-stdout')
print(label + '-stderr', file=sys.stderr)
sys.exit(status)
'''
NODE_CHILD = '''import fs from 'node:fs';
const name = 'test_z.mjs';
const status = Number(process.env['FIXTURE_STATUS_' + name] || 0);
fs.appendFileSync(process.env.FIXTURE_RECEIPTS, JSON.stringify({name, argv: []})+'\\n');
const label = (status ? 'BAD-' : 'GOOD-') + name;
console.log(label + '-stdout'); console.error(label + '-stderr');
process.exit(status);
'''


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doit-quiet-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = self.root / 'tree'
        self.src = self.tree / 'src'
        self.src.mkdir(parents=True)
        shutil.copyfile(REPO / 'doit', self.tree / 'doit')
        self.home, self.caller, self.xdg, self.scratch = [self.root / n for n in
                                                       ('home', 'caller', 'xdg', 'scratch')]
        for p in (self.home, self.caller, self.xdg, self.scratch):
            p.mkdir()
            (p / 'sentinel').write_text('protected')
        (self.scratch / 'sentinel').unlink()
        self.receipts = self.root / 'receipts.jsonl'
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.home),
                    'DOIT_ROOT': str(self.caller), 'XDG_CONFIG_HOME': str(self.xdg),
                    'DOIT_LEDGER_FILE': str(self.caller / 'never-write.jsonl'),
                    'TMPDIR': str(self.scratch), 'TMP': str(self.home), 'TEMP': str(self.xdg),
                    'DOIT_TEST_LIVE_ROOT': 'stale-root',
                    'GIT_DIR': str(self.root / 'invalid-git'), 'GIT_WORK_TREE': 'invalid',
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'invalid',
                    'GIT_CONFIG_VALUE_0': 'invalid', 'FIXTURE_RECEIPTS': str(self.receipts)}

    def child(self, name):
        (self.src / name).write_text(NODE_CHILD if name.endswith('.mjs') else PY_CHILD)

    def hermetic(self):
        for name in ('test_a.py', 'test_b.py', 'test_z.mjs', 'retired.py', 'machine.py'):
            self.child(name)

    def live(self):
        for name in ('test_harvest.py', 'test_problem_backfill.py', 'test_never.py'):
            self.child(name)
        events = self.caller / 'events'
        events.mkdir()
        (events / 'fixture.jsonl').write_text('{}\n')

    def run_launcher(self, *args, statuses=None):
        if self.receipts.exists():
            self.receipts.unlink()
        env = dict(self.env)
        env.update({'FIXTURE_STATUS_' + k: str(v) for k, v in (statuses or {}).items()})
        proc = subprocess.run(['bash', str(self.tree / 'doit'), *args], cwd=self.tree,
                              env=env, text=True, capture_output=True, timeout=30)
        records = ([json.loads(line) for line in self.receipts.read_text().splitlines()]
                   if self.receipts.exists() else [])
        # Optional retained review evidence contains fixture data only.
        if report_dir := os.environ.get('DOIT_QUIET_REPORT_DIR'):
            dest = Path(report_dir)
            dest.mkdir(parents=True, exist_ok=True)
            body = proc.stdout + proc.stderr
            report = {'url': 'cli://doit/' + '/'.join(args), 'status': proc.returncode,
                      'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                      'body_excerpt': body[:4000], 'stdout': proc.stdout,
                      'stderr': proc.stderr, 'receipts': records, 'test': self.id()}
            with tempfile.NamedTemporaryFile(mode='w', prefix=self.id() + '-',
                                             suffix='.json', dir=dest, delete=False) as out:
                json.dump(report, out, indent=2)
        return proc, records

    def quiet_output(self, proc, count):
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], f'Passed: {count} checks')
        self.assertEqual(sum(s.startswith('Passed:') for s in lines), 1)
        for forbidden in ('GOOD-', 'PASS ', '· ', 'live-check ', 'live: pass'):
            self.assertNotIn(forbidden, proc.stdout + proc.stderr)

    def failure(self, proc, name, status):
        self.assertIn(f'FAIL {name} (exit {status})', proc.stdout)
        for stream in ('stdout', 'stderr'):
            self.assertIn(f'BAD-{name.split(":")[0]}-{stream}', proc.stdout)


class QuietChecks(LauncherCase):
    def test_success_and_mixed_and_zero(self):
        self.hermetic()
        names = ['test_a.py', 'test_b.py', 'test_z.mjs', 'retired.py', 'machine.py']
        for failures in ({}, {'test_a.py': 7, 'machine.py': 9},
                         {name: 4 for name in names}, {'retired.py': 5, 'test_z.mjs': 6}):
            with self.subTest(failures=failures):
                proc, receipts = self.run_launcher('test', '--failures-only', statuses=failures)
                self.assertEqual(proc.returncode, int(bool(failures)))
                self.assertEqual([r['name'] for r in receipts], names)
                self.quiet_output(proc, len(names) - len(failures))
                for name, status in failures.items():
                    identity = {'retired.py': 'retired check', 'machine.py': 'machine check'}.get(name, name)
                    self.assertIn(f'FAIL {identity} (exit {status})', proc.stdout)
                    for stream in ('stdout', 'stderr'):
                        self.assertIn(f'BAD-{name}-{stream}', proc.stdout)
                machine = receipts[-1]
                self.assertEqual(machine['env']['DOIT_ROOT'], str(self.caller.resolve()))
                self.assertEqual(machine['argv'], ['check', '--gate'])
                real = os.path.realpath
                argv = receipts[-2]['argv']
                self.assertEqual(argv[:2], ['check', '--root'])
                self.assertEqual(argv[3], '--repo')
                self.assertEqual([real(argv[2]), real(argv[4])],
                                 [real(self.tree / 'retired.d'), real(self.tree)])
                self.assertEqual(len(argv), 5)


class LiveAndArguments(LauncherCase):
    def test_status_matrix_and_option_orders(self):
        self.live()
        names = ['test_harvest.py', 'test_problem_backfill.py']
        identities = [names[0] + ':check_ledger', names[1] + ':test_real_ledger_ac3_help_and_ac7']
        for args in (('--live', '--failures-only'), ('--failures-only', '--live')):
            for statuses, expected in (((0, 0), 0), ((2, 0), 1), ((3, 0), 3),
                                       ((0, 3), 3), ((3, 3), 3), ((2, 3), 1), ((3, 2), 1)):
                with self.subTest(args=args, statuses=statuses):
                    proc, receipts = self.run_launcher('test', *args, statuses=dict(zip(names, statuses)))
                    self.assertEqual(proc.returncode, expected)
                    self.assertEqual([r['name'] for r in receipts], names)
                    self.assertTrue(all(r['argv'] == ['--live'] for r in receipts))
                    self.quiet_output(proc, statuses.count(0))
                    for name, identity, status in zip(names, identities, statuses):
                        if status:
                            self.failure(proc, identity, status)
                    if expected:
                        self.assertIn('live: ' + ('not run' if expected == 3 else 'fail'), proc.stdout)

    def test_missing_and_unreadable_ledger(self):
        self.live()
        ledger = self.caller / 'events' / 'fixture.jsonl'
        ledger.unlink()
        for unreadable in (False, True):
            if unreadable:
                # A directory named *.jsonl cannot be opened as a file, even as root.
                ledger.mkdir()
            proc, receipts = self.run_launcher('test', '--live', '--failures-only')
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(receipts, [])
            self.assertEqual(proc.stdout, 'live: not run\nPassed: 0 checks\n')

    def test_invalid_and_duplicate_arguments(self):
        self.hermetic()
        for args in (('--unknown',), ('--live', '--live'), ('--failures-only', '--failures-only'),
                     ('--live', '--failures-only', '--live'), ('extra', '--failures-only')):
            proc, receipts = self.run_launcher('test', *args)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(receipts, [])
            self.assertNotIn('Passed:', proc.stdout + proc.stderr)
            self.assertIn('usage: doit test [--live] [--failures-only]', proc.stderr)

    def test_verbose_live(self):
        self.live()
        for statuses, expected in (({}, 0), ({'test_harvest.py': 3}, 3),
                                   ({'test_harvest.py': 1, 'test_problem_backfill.py': 3}, 1)):
            proc, receipts = self.run_launcher('test', '--live', statuses=statuses)
            self.assertEqual(proc.returncode, expected)
            self.assertEqual(len(receipts), 2)
            self.assertIn('live-check test_harvest.py:check_ledger', proc.stdout)
            self.assertIn('live: ' + {0: 'pass', 1: 'fail', 3: 'not run'}[expected], proc.stdout)
            self.assertIn('-stdout', proc.stdout)
            self.assertIn('-stderr', proc.stderr)
            self.assertNotIn('Passed:', proc.stdout)


class CompatibilityAndIsolation(LauncherCase):
    def test_paired_inventory_isolation_and_cleanup(self):
        self.hermetic()
        for statuses in ({}, {'test_a.py': 8, 'machine.py': 5}):
            results = []
            for flags in ((), ('--failures-only',)):
                proc, receipts = self.run_launcher('test', *flags, statuses=statuses)
                results.append((proc.returncode, [r['name'] for r in receipts]))
                self.assertEqual(proc.returncode, int(bool(statuses)))
                self.assertEqual(list(self.scratch.iterdir()), [])
                for r in receipts:
                    if r['name'].endswith('.mjs'):
                        continue
                    env = r['env']
                    self.assertEqual(env['DOIT_TEST_PROCESS'], '1')
                    self.assertEqual(env['DOIT_TEST_MACHINE_ROOT'], str(self.caller.resolve()))
                    self.assertNotIn('DOIT_LEDGER_FILE', env)
                    self.assertNotIn('DOIT_TEST_LIVE_ROOT', env)
                    self.assertFalse(any(k.startswith('GIT_') for k in env))
                    keys = ['HOME', 'XDG_CONFIG_HOME', 'TMPDIR', 'TMP', 'TEMP']
                    if r['name'] != 'machine.py':
                        keys.append('DOIT_ROOT')
                    for key in keys:
                        path = Path(env[key])
                        self.assertTrue(path.is_relative_to(self.scratch))
                        self.assertFalse(path.exists())
                    self.assertFalse(Path(r['scratch']).exists())
                for sentinel in (self.home, self.caller, self.xdg):
                    self.assertEqual([p.name for p in sentinel.iterdir()], ['sentinel'])
                    self.assertEqual((sentinel / 'sentinel').read_text(), 'protected')
                if not flags:
                    self.assertIn('· test_a.py', proc.stdout)
                    self.assertIn('PASS test_b.py', proc.stdout)
                    self.assertIn('GOOD-test_b.py-stdout', proc.stdout)
                    self.assertIn('GOOD-test_b.py-stderr', proc.stdout)
                    self.assertIn('machine.py-stdout', proc.stdout)
                    self.assertIn('machine.py-stderr', proc.stderr)
                    self.assertNotIn('Passed:', proc.stdout)
            self.assertEqual(results[0], results[1])

    def test_grader_baseline_fallback(self):
        self.hermetic()
        self.child('test_grader_view.py')
        git_env = {k: v for k, v in self.env.items() if not k.startswith('GIT_')}
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.tree), *args], env=git_env, text=True).strip()
        git('init', '-q', '-b', 'fixture')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('add', '.')
        git('commit', '-qm', 'fixture base')
        git('commit', '--allow-empty', '-qm', 'fixture head')
        before = git('show-ref')
        for flags in ((), ('--failures-only',)):
            proc, receipts = self.run_launcher('test', *flags)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            grader = next(r for r in receipts if r['name'] == 'test_grader_view.py')
            self.assertNotEqual(grader['grader_repo'], str(self.tree))
            self.assertFalse(Path(grader['grader_repo']).exists())
            self.assertEqual(git('show-ref'), before)


class BuilderContract(LauncherCase):
    def test_contract_and_help(self):
        contract = (REPO / 'agents' / 'builder.md').read_text()
        self.assertIn('run each `src/test_*.py` your commit adds or changes', contract)
        self.assertIn('then `./doit test --failures-only`, and put both results in the card', contract)
        self.assertIn('Pre-existing failures', contract)
        self.assertIn('After your final edit, run the coupled-test CLI command', contract)
        proc, _ = self.run_launcher('help')
        self.assertEqual(proc.returncode, 0)
        self.assertIn('doit test [--live] [--failures-only]', proc.stdout)


if __name__ == '__main__':
    unittest.main()
