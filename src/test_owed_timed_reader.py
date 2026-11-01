"""First-reading failures are visible and retryable without leaking diagnostics."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ['DOIT_ROOT'] = tempfile.mkdtemp(prefix='timed-reader-import-')
import fold
import owed_timed

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
DECL = dict(type='owed-ac', subject='L-spec-0651', criterion='AC7', readings=2,
            interval_h=24, reading='pg_stat_statements', project='albert-scott',
            ts='2026-10-26T00:00:00+00:00', _src='declaration')
SHIP = dict(type='shipped', subject='L-spec-0651', ts='2026-10-26T01:00:00+00:00')


class ReaderTests(unittest.TestCase):
    def test_default_uses_observation_function_and_declared_filter(self):
        with patch.object(owed_timed.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='123\t7\tselect amazon_settlement_lines\n')) as run:
            self.assertIsNotNone(owed_timed.default_reader('L-spec-0651', 'AC7', DECL))
            self.assertIn('FROM doit_obs.stat_statements()', run.call_args.args[0][-1])
            sql = "SELECT queryid, calls, query FROM doit_obs.stat_statements() WHERE query LIKE '%amazon_settlement_lines%'"
            owed_timed.default_reader('L-spec-0651', 'AC7', dict(DECL, reading_sql=sql))
            self.assertEqual(run.call_args.args[0][-1], sql)

    def test_failures_emit_once_and_recover(self):
        for reader in (lambda *a: None,
                       lambda *a: (_ for _ in ()).throw(subprocess.TimeoutExpired('fixture', 1))):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as name:
                root = pathlib.Path(name)
                ledger = root/'events/L-verifier-local.jsonl'
                ledger.parent.mkdir()
                by = {'L-spec-0651': [DECL, SHIP]}
                with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', ledger.parent):
                    owed_timed.run_pass(by, NOW, reader, root=root, ledger=ledger)
                    rows = [json.loads(s) for s in ledger.read_text().splitlines()]
                    self.assertEqual([r['type'] for r in rows], ['owed-unobservable'])
                    self.assertEqual(rows[0]['criterion'], 'AC7')
                    self.assertIn('first timed reading unavailable', rows[0]['why'])
                    owed_timed.run_pass(by, NOW, reader, root=root, ledger=ledger, events=rows)
                    self.assertEqual(len(ledger.read_text().splitlines()), 1)
                    self.assertIsNone(owed_timed.load('L-spec-0651', 'AC7', root=root))
                    owed_timed.run_pass(by, NOW, lambda *a: {'rows': 'visible'},
                                        root=root, ledger=ledger, events=rows)
                    self.assertEqual(owed_timed.load('L-spec-0651', 'AC7', root=root)['reading']['rows'], 'visible')
                    rows = [json.loads(s) for s in ledger.read_text().splitlines()]
                    self.assertEqual(rows[-1]['type'], 'owed-ac')
                    self.assertEqual(rows[-1]['wake_at'], '2026-10-28T12:00:00+00:00')

    def test_empty_hidden_and_error_output_are_not_readings(self):
        for code, stdout in ((0, ''), (0, '123\t4\t<insufficient privilege>'), (1, '')):
            with self.subTest(code=code, stdout=stdout), patch.object(owed_timed.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=code, stdout=stdout, stderr='secret fixture')):
                with self.assertRaises(owed_timed.ReadingUnavailable) as caught:
                    owed_timed.default_reader('L-spec-0651', 'AC7', DECL)
                self.assertNotIn('secret fixture', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
