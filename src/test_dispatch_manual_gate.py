#!/usr/bin/env python3
"""Standalone manual dispatch guard regressions (L-spec-2045 AC4)."""
from pathlib import Path
import atexit, json, os, subprocess, sys, tempfile, unittest

# Establish isolation before importing dispatch or any ledger reader.
_root = tempfile.TemporaryDirectory(prefix="manual-dispatch-test-")
atexit.register(_root.cleanup)
os.environ["DOIT_ROOT"] = _root.name
os.environ.pop("DOIT_LEDGER_FILE", None)
os.environ["DOIT_NO_POKE"] = "1"


class ManualDispatchTests(unittest.TestCase):
    def test_2045_ac4_manual_guard_and_cli(self):
        import dispatch, fold, hashlib, runpy
        from unittest.mock import patch
        for env in ({}, {'DOIT_LEDGER_FILE': 'L-operator-local.jsonl'}):
            self.assertTrue(dispatch.manual_dispatch_refusal('builder', env, None))
            self.assertTrue(dispatch.manual_dispatch_refusal('builder', env, '  '))
            self.assertIsNone(dispatch.manual_dispatch_refusal('builder', env, 'repair incident'))
        self.assertIsNone(dispatch.manual_dispatch_refusal('builder', {'DOIT_SUPERVISED': '1'}, None))
        for actor in ('tick', 'base-red', 'executor'):
            self.assertIsNone(dispatch.manual_dispatch_refusal('builder', {'DOIT_LEDGER_FILE': f'L-{actor}-local.jsonl'}, None))
        with patch.object(fold, 'actor_of', side_effect=ValueError('unreadable')):
            self.assertTrue(dispatch.manual_dispatch_refusal('builder', {'DOIT_LEDGER_FILE': 'L-tick-local.jsonl'}, None))
        with patch.object(fold, 'actor_of', return_value=None):
            self.assertTrue(dispatch.manual_dispatch_refusal('builder', {'DOIT_LEDGER_FILE': 'L-tick-local.jsonl'}, None))
        with tempfile.TemporaryDirectory(prefix='manual-dispatch-') as tmp:
            env = {'DOIT_ROOT': tmp, 'DOIT_NO_POKE': '1'}
            for override in (None, 'repair incident'):
                args = [str(Path(dispatch.__file__)), 'builder', 'L-spec-9001']
                if override:
                    args += ['--manual-override-reason', override]
                with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', args), \
                     patch.object(dispatch, 'cli_version', return_value='fixture'), \
                     patch('models.load', side_effect=SystemExit('guard passed')):
                    with self.assertRaises(SystemExit):
                        runpy.run_path(dispatch.__file__, run_name='__main__')
            with patch.object(fold, 'ROOT', Path(tmp)), patch.object(fold, 'EVENTS', Path(tmp) / 'events'):
                rows = [e for e in fold.read_events(use_cache=False) if e['type'] == 'manual-dispatch']
            self.assertEqual([e['outcome'] for e in rows], ['refused', 'overridden'])
            self.assertEqual(len(dispatch.manual_dispatch_violations(rows)), 2)
            body = json.dumps(rows, sort_keys=True)
            print(json.dumps(dict(url='cli://L-spec-2045/AC4', status=0,
                                 body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_2045_role_override_does_not_emit_manual_violation(self):
        import dispatch, runpy
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='role-dispatch-') as tmp:
            for actor in ('base-red', 'tick', 'executor'):
                args = [dispatch.__file__, 'builder', 'L-spec-9001',
                        '--manual-override-reason', 'scheduled check']
                env = dict(DOIT_ROOT=tmp, DOIT_NO_POKE='1', DOIT_LEDGER_FILE=f'L-{actor}-local.jsonl')
                with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', args), \
                     patch.object(dispatch, 'cli_version', return_value='fixture'), \
                     patch('models.load', side_effect=SystemExit('guard passed')):
                    with self.assertRaises(SystemExit):
                        runpy.run_path(dispatch.__file__, run_name='__main__')
                self.assertFalse((Path(tmp) / 'events' / env['DOIT_LEDGER_FILE']).exists())

    def test_2045_detached_override_is_recorded_once(self):
        import dispatch, time
        with tempfile.TemporaryDirectory(prefix='manual-detach-') as tmp:
            args = [sys.executable, dispatch.__file__, 'builder', 'L-spec-9001',
                    '--detach', '--packet', tmp + '/missing', '--manual-override-reason', 'repair incident']
            env = dict(os.environ, DOIT_ROOT=tmp, DOIT_NO_POKE='1')
            for key in ('DOIT_SUPERVISED', 'DOIT_LEDGER_FILE'):
                env.pop(key, None)
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = Path(tmp) / 'events/L-operator-local.jsonl'
            deadline = time.monotonic() + 10
            while not ledger.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual([e['outcome'] for e in rows], ['overridden'])

if __name__ == '__main__':
    unittest.main()
