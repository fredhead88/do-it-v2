"""Owned supersession through real subprocesses, durable state and a local gh."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from test_window_dispatch import Identity, CHECKS
import window_gate as gate
import window_dispatch
import window_rehearse


class Supersession(Identity):
    def fixtures(self):
        self.git('remote', 'set-url', '--push', 'origin', str(self.remote))
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/project.git')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'gh'
        fake.write_text('''#!/usr/bin/python3
import fcntl, json, os, sys
from pathlib import Path
root = Path(os.environ['DOIT_ROOT'])
with (root / 'gh.lock').open('w') as lock:
 fcntl.flock(lock, fcntl.LOCK_EX)
 p = root / 'remote.json'
 data = json.loads(p.read_text()) if p.exists() else dict(runs=[], calls=[], lists=0)
 a = sys.argv[1:]
 data['calls'].append(a)
 mode = (root / 'error').read_text() if (root / 'error').exists() else ''
 result, code = None, 0
 if a[0] == 'fixture':
  pid, sha = a[2:4]
  rid = len(data['runs']) + 1
  row = dict(databaseId=rid, headBranch='ci-verify/' + sha[:12] + '-' + pid,
             headSha=sha, status='in_progress', conclusion=None)
  data['runs'].append(row)
  data['late'] = dict(row, databaseId=rid + 1)
 elif a[:2] == ['run', 'list']:
  data['lists'] += 1
  if data['lists'] == 2 and 'late' in data:
   data['runs'].append(data.pop('late'))
  branch = a[a.index('--branch') + 1]
  result = [r.copy() for r in data['runs'] if r['headBranch'] == branch]
  if mode == 'foreign' and result: result[0]['headBranch'] += '-foreign'
  if mode == 'sha' and result: result[0]['headSha'] = 'f' * 40
  if mode == 'unbounded' and result: result = result * 101
  if mode == 'gh': code = 1
 elif a[:2] == ['run', 'view']:
  result = next(r.copy() for r in data['runs'] if str(r['databaseId']) == a[2])
 elif a[:2] == ['run', 'cancel']:
  if mode == 'reject': code = 1
  elif mode != 'never':
   r = next(r for r in data['runs'] if str(r['databaseId']) == a[2])
   r.update(status='completed', conclusion='cancelled')
 p.write_text(json.dumps(data))
 if result is not None: print(json.dumps(result))
 if a[:2] == ['run', 'cancel'] and not code: print('Requested cancellation of workflow run ' + a[2])
 sys.exit(code)
''')
        fake.chmod(0o700)
        companion = self.root / 'companion.sh'
        companion.write_text('''gh fixture launch "$$" "$PREDEPLOY_GATE_DEPLOY_SHA"
if [ "${CANCEL_TEST_MODE:-}" != replacement ]; then
  trap 'printf "%s\\n" "$CANCEL_TEST_OUTPUT"; exit "${CANCEL_TEST_EXIT:-1}"' TERM
  touch "$DOIT_ROOT/ready"
  while :; do sleep 0.05; done
fi
printf '%s\\n' '{"verdict":"green","checks":{"pytest":"ran","pg-parity":"ran","vitest":"ran"}}'
''')
        self.stack.enter_context(patch.dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
            DOIT_WINDOW_PREDEPLOY=str(companion), CANCEL_TEST_MODE='replacement'))
        import window_cancel as cancel
        self.stack.enter_context(patch.object(cancel, 'POLL_SECS', 0.02))
        self.stack.enter_context(patch.object(cancel, 'MAX_POLLS', 8))
        self.stack.enter_context(patch.object(cancel, 'QUIET_POLLS', 3))
        return cancel

    def old(self, window='old', specs=('A',), tree=None, required=None, chain=False, production='', output='FAILED api/tests/test_x.py::test_x', code=1):
        (self.root / 'ready').unlink(missing_ok=True)
        runner = self.root / 'runner.py'
        runner.write_text('''import json, os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import window_dispatch, window_gate as gate
repo, sha, window, tree, specs, required, out, chain, base, production = sys.argv[2:]
repo, specs = Path(repo), json.loads(specs)
def verify(*args, **kw):
 with window_dispatch.managed(tree, known=dict(project='albert-scott', base_sha=base, specs=specs,
       entries=[(s, s, sha) for s in specs])):
  result = gate.verify(repo, sha, json.loads(required), window)
 Path(out).write_text(json.dumps(result))
 return 2 if result.get('superseded') else 0
if production:
 import window_rehearse
 gate.base_red_snapshot = lambda *a, **k: dict(complete=True)
 gate.scope = lambda *a, **k: dict(checks=[gate.PYTEST_CHECK], reason='fixture')
 if production == 'judge':
  rc = gate._run_attempt(window, 'albert-scott', ['A:A'], 'master', False, base, base, None, 0, None)
 else:
  rc = window_rehearse._run(window, 'albert-scott', ['A:A'], base, 'master')
 Path(out).write_text(json.dumps(dict(exit=rc)))
 sys.exit(rc)
if chain == 'True':
 gate.run_window = verify
 sys.exit(gate.main_([window, '--project', 'albert-scott', '--specs', ','.join(s + ':' + s for s in specs)]))
verify()
''')
        out = self.root / ('result-' + window)
        proc = subprocess.Popen(['/usr/bin/python3', str(runner), str(Path(gate.__file__).parent),
            str(self.repo), self.candidate, window, tree or self.tree, json.dumps(specs),
            json.dumps(required or [gate.PYTEST_CHECK]), str(out), str(chain), self.base, production],
            env={**os.environ, 'CANCEL_TEST_MODE': 'old', 'CANCEL_TEST_OUTPUT': output,
                 'CANCEL_TEST_EXIT': str(code)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            for p in (self.root / 'state/window-cancel/albert-scott').glob('*.json'):
                row = json.loads(p.read_text())
                if row['window'] == window and row.get('pid'):
                    try: os.killpg(row['pid'], 9)
                    except ProcessLookupError: pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (self.root / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.root / 'ready').exists(), 'old companion never launched')
        return proc, out

    def advance_generation(self, content=False):
        self.git('switch', '-q', 'A')
        if content:
            self.write('feature.txt', 'new generation ' + self.candidate + '\n')
            self.commit()
        else:
            self.git('commit', '--allow-empty', '-qm', 'new generation')
        self.candidate = self.git('rev-parse', 'HEAD')
        self.tree = gate.tree_of(self.repo, self.candidate)
        self.git('switch', '-q', 'master')

    def admitted_replacement(self, window='new', specs=('A:A',)):
        def run(*args, **kwargs):
            result = self.replacement(window, tuple(s.partition(':')[0] for s in specs))
            return 0 if result['state'] == 'green' else 2
        with patch.object(gate, 'run_window', side_effect=run):
            return gate.main_([window, '--project', 'albert-scott', '--specs', ','.join(specs)])

    def replacement(self, window='old', specs=('A',), tree=None, required=None):
        with window_dispatch.managed(tree or self.tree,
                known=dict(project='albert-scott', base_sha=self.base, specs=list(specs))):
            return gate.verify(self.repo, self.candidate, required or [gate.PYTEST_CHECK], window)

    def calls(self):
        return json.loads((self.root / 'remote.json').read_text())['calls']

    def evidence(self, ac):
        calls = self.calls()
        raw = json.dumps(calls, sort_keys=True)
        print(json.dumps(dict(url='scratch://window-cancel/' + ac, status=200,
            body_sha256=hashlib.sha256(raw.encode()).hexdigest(), body_excerpt=calls)))

    def test_ac1_targets_and_order(self):
        cancel = self.fixtures()
        for window, specs in [('old', ('A',)), ('overlap', ('A', 'B')), ('equivalent', ('A',))]:
            with self.subTest(window=window):
                proc, out = self.old()
                data = json.loads((self.root / 'remote.json').read_text())
                completed = dict(data['runs'][-1], databaseId=1000 + len(data['runs']),
                                 status='completed', conclusion='success')
                data['runs'].append(completed)
                (self.root / 'remote.json').write_text(json.dumps(data))
                before = len(self.calls())
                result = self.replacement(window, specs)
                self.assertEqual(result['state'], 'green', result)
                proc.wait(timeout=5)
                self.assertEqual(json.loads(out.read_text())['nodes'], [])
                calls = self.calls()[before:]
                cancels = [c for c in calls if c[:2] == ['run', 'cancel']]
                self.assertEqual(len(cancels), 2 if window == 'old' else 1)
                self.assertTrue(all(c[3:] == ['-R', 'example/project'] for c in cancels))
                self.assertNotIn(str(completed['databaseId']), [c[2] for c in cancels])
                dispatch = next(i for i, c in enumerate(calls) if c[:2] == ['fixture', 'launch'])
                self.assertTrue(all(i < dispatch for i, c in enumerate(calls) if c[:2] == ['run', 'cancel']))
                for c in cancels:
                    self.assertTrue(any(v[:3] == ['run', 'view', c[2]] for v in calls[calls.index(c) + 1:dispatch]))
                receipts = list((self.root / 'state/window-cancel/albert-scott').glob('*.json'))
                self.assertTrue(any(json.loads(p.read_text()).get('confirmed_at') for p in receipts))
        # Exercise main_'s real lifetime-lock refusal and retry, across processes.
        for window, changed in [('old', True), ('overlap', True), ('equivalent', False)]:
            with self.subTest(admission=window):
                proc, out = self.old(chain=True)
                self.advance_generation(content=changed)
                before = len(self.calls())
                self.assertEqual(self.admitted_replacement(window), 0)
                self.assertEqual(proc.wait(timeout=5), 2)
                self.assertTrue(json.loads(out.read_text())['superseded'])
                calls = self.calls()[before:]
                dispatch = next(i for i, c in enumerate(calls) if c[:2] == ['fixture', 'launch'])
                cancels = [i for i, c in enumerate(calls) if c[:2] == ['run', 'cancel']]
                self.assertTrue(cancels)
                self.assertTrue(all(i < dispatch for i in cancels))
                self.assertTrue(all(any(v[:3] == ['run', 'view', calls[i][2]]
                                        for v in calls[i + 1:dispatch]) for i in cancels))
        self.evidence('AC1')

    def test_ac2_fail_closed(self):
        cancel = self.fixtures()
        for mode in ('reject', 'foreign', 'sha', 'unbounded', 'gh', 'never', 'stale', 'host'):
            with self.subTest(mode=mode):
                # Isolate failed admissions: a prior failed receipt must keep blocking its own window.
                proc, out = self.old(window=mode, specs=(mode,))
                (self.root / 'error').write_text(mode)
                if mode in ('stale', 'host'):
                    p = next(p for p in (self.root / 'state/window-cancel/albert-scott').glob('*.json')
                             if json.loads(p.read_text())['window'] == mode)
                    doc = json.loads(p.read_text())
                    if mode == 'host':
                        doc['holder']['host'] = 'foreign-host'
                    else:
                        doc['start_identity'] = 'stale'
                    p.write_text(json.dumps(doc))
                before = len(self.calls())
                result = self.replacement(mode, (mode,))
                self.assertEqual(result['state'], 'undetermined', result)
                self.assertFalse(any(c[:2] == ['fixture', 'launch'] for c in self.calls()[before:]))
                if mode in ('foreign', 'sha', 'unbounded', 'gh', 'stale', 'host'):
                    self.assertFalse(any(c[:2] == ['run', 'cancel'] for c in self.calls()[before:]))
                (self.root / 'error').unlink()
                # Stop only the test companion after examining the cancellation log.
                for p in (self.root / 'state/window-cancel/albert-scott').glob('*.json'):
                    row = json.loads(p.read_text())
                    if row['window'] == mode and row.get('pid'):
                        try: os.killpg(row['pid'], 9)
                        except ProcessLookupError: pass
                proc.wait(timeout=5)
        proc, out = self.old(window='unrelated', specs=('Z',))
        before = len(self.calls())
        self.assertEqual(self.replacement('independent', ('A',))['state'], 'green')
        self.assertFalse(any(c[:2] == ['run', 'cancel'] for c in self.calls()[before:]))
        self.assertIsNone(proc.poll())
        # Unproven lock holder / generation / receipt must leave the live companion alone.
        for mode in ('same', 'host', 'holder-start', 'missing', 'ref', 'pid', 'unrelated',
                     'foreign', 'sha', 'unbounded', 'gh', 'reject', 'timeout'):
            with self.subTest(admission=mode):
                proc, out = self.old(window='admit-' + mode, specs=('A',), chain=True)
                if mode != 'same':
                    self.advance_generation()
                lockpath = self.root / 'state/window-chain.albert-scott.lock'
                metadata = json.loads(lockpath.read_text())
                receipt = next(p for p in (self.root / 'state/window-cancel/albert-scott').glob('*.json')
                               if json.loads(p.read_text())['window'] == 'admit-' + mode)
                row = json.loads(receipt.read_text())
                if mode in ('host', 'holder-start'):
                    metadata['host' if mode == 'host' else 'start_identity'] = 'foreign'
                    lockpath.write_text(json.dumps(metadata))
                if mode == 'missing': receipt.unlink()
                elif mode in ('ref', 'pid'):
                    row['ref' if mode == 'ref' else 'start_identity'] = 'wrong'
                    receipt.write_text(json.dumps(row))
                if mode in ('foreign', 'sha', 'unbounded', 'gh', 'reject'):
                    (self.root / 'error').write_text(mode)
                before = len(self.calls())
                with patch.object(cancel, 'ADMISSION_SECS', 0 if mode == 'timeout' else 600):
                    self.assertEqual(self.admitted_replacement(specs=('Z:A',) if mode == 'unrelated' else ('A:A',)), 3)
                calls = self.calls()[before:]
                self.assertFalse(any(c[:2] == ['fixture', 'launch'] for c in calls))
                if mode not in ('reject', 'timeout'):
                    self.assertFalse(any(c[:2] == ['run', 'cancel'] for c in calls))
                    self.assertFalse(cancel.paths(row)[1].exists())
                (self.root / 'error').unlink(missing_ok=True)
                # Test-only teardown for refusal cases; production did not signal the holder.
                try: os.killpg(row['pid'], 9)
                except ProcessLookupError: pass
                proc.wait(timeout=5)
        self.evidence('AC2')

    def test_admission_uses_caller_snapshot(self):
        self.fixtures()
        proc, out = self.old(chain=True)
        self.advance_generation()
        events = gate.train_events()
        with patch.object(gate, 'train_events', return_value=events) as read, \
                patch.object(gate, 'resolve_entries', wraps=gate.resolve_entries) as resolve:
            self.assertEqual(self.admitted_replacement(), 0)
        self.assertEqual(read.call_count, 1)
        self.assertIs(resolve.call_args.args[2], events)
        self.assertEqual(proc.wait(timeout=5), 2)

    def test_ac3_superseded_outcome_ignored(self):
        cancel = self.fixtures()
        for output, code in [('FAILED api/tests/test_x.py::test_x', 1),
                (json.dumps(dict(verdict='red', checks={'pytest': 'ran'})), 1),
                (json.dumps(dict(verdict='green', checks={'pytest': 'ran'})), 0), ('undetermined', 2)]:
            proc, out = self.old(output=output, code=code)
            self.assertEqual(self.replacement()['state'], 'green')
            proc.wait(timeout=5)
            result = json.loads(out.read_text())
            self.assertEqual(result['state'], 'undetermined')
            self.assertEqual(result['nodes'], [])
            self.assertEqual(result['doc'], {})
            self.assertTrue(result['superseded'])
            # An independent interpreter observes the same receipt and tombstone after cleanup.
            reader = subprocess.run(['/usr/bin/python3', '-c',
                'import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); '
                'print(json.dumps([json.loads(f.read_text()) for f in p.glob("*.tombstone")]))',
                str(self.root / 'state/window-cancel/albert-scott')], capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(reader.stdout))
        for mode in ('judge', 'rehearse'):
            proc, out = self.old(window=mode, production=mode,
                output=json.dumps(dict(verdict='green', checks={'pytest': 'ran'})), code=0)
            self.assertEqual(self.replacement(mode)['state'], 'green')
            self.assertEqual(proc.wait(timeout=5), 2)
            events = [e for e in gate.fold.read_events()
                      if e.get('window', e.get('subject')) == mode]
            forbidden = {'window-dispatch-verdict', 'window-tree-verified', 'window-dropped',
                         'window-targeted-attribution', 'window-verified'}
            self.assertEqual([e for e in events if e['type'] in forbidden], [])
        self.evidence('AC3')


if __name__ == '__main__':
    unittest.main()
