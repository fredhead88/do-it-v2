"""Public append output compatibility and the no-fold fast path, on temp ledgers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SRC = Path(__file__).resolve().parent


class AppendOutputTests(unittest.TestCase):
    def test_quiet_json_and_explicit_board(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, DOIT_ROOT=folder, DOIT_PROJECT='fixture',
                       DOIT_LEDGER_FILE='L-operator-fixture.jsonl', HOME=folder)
            for flags in ([], ['--quiet'], ['--json'], ['--board']):
                result = subprocess.run([str(SRC.parent / 'doit'), 'append', 'observed', 'fixture', 'why=test', *flags],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if '--board' in flags:
                    self.assertTrue(result.stdout.startswith('# board · '), result.stdout)
                elif '--json' in flags:
                    self.assertEqual(json.loads(result.stdout)['why'], 'test')
                else:
                    self.assertEqual(result.stdout, '')
            path = Path(folder) / 'events/L-operator-fixture.jsonl'
            self.assertEqual(len(path.read_text().splitlines()), 4)
            before = path.read_bytes()
            refused = subprocess.run([str(SRC.parent / 'doit'), 'append', 'observed', 'fixture', 'actor=forged'],
                                     env=env, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(before, path.read_bytes())

    def test_explicit_project_never_reads_or_folds(self):
        with tempfile.TemporaryDirectory() as folder:
            # Profile real CLI Python calls, including startup: any ledger read,
            # full-file verification read_text, fold, or render is a failure.
            code = '''import pathlib,runpy,sys
src=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(src.parent))
def guard(frame,event,arg):
    if event=='call' and frame.f_code.co_name in ('read_events','fold','render','read_text'):
        raise AssertionError('unexpected full read/fold: '+frame.f_code.co_name)
sys.setprofile(guard)
sys.argv=[str(src),'append','observed','fixture','project=explicit']
runpy.run_path(str(src),run_name='__main__')
'''
            env = dict(os.environ, DOIT_ROOT=folder, HOME=folder)
            env.pop('DOIT_PROJECT', None)
            result = subprocess.run([sys.executable, '-c', code, str(SRC/'fold.py')],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
