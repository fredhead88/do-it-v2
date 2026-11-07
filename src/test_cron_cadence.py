"""Approved cadence survives manifest installation; paused jobs stay paused."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import crons


class CadenceTests(unittest.TestCase):
    def test_approved_cadence(self):
        self.assertIn('portal-latency-daily', crons.paused_names())
        expected = {'look': '33 * * * *', 'sweep-owed': '11 0,6,12,18 * * *',
                    'owed-digest': '0 6 * * *', 'ledger-snapshot': '2 * * * *',
                    'health-alarms': '6,36 * * * *', 'balance': '8 * * * *',
                    'backup-watch': '*/15 * * * *',
                    'owed-digest-json': '17 * * * *', 'tmp-reaper': '*/15 * * * *'}
        for row in crons.rows():
            if row['name'] in expected:
                self.assertEqual(row['schedule'], expected[row['name']], row['name'])

    def test_reinstall_preserves_live_periods(self):
        rows = {row['name']: row for row in crons.rows()}
        for name, period in (('tmp-reaper', 900), ('owed-digest-json', 3600)):
            self.assertEqual(crons.interval_s(rows[name]['schedule']), period,
                             f'{name}: a reinstall must not speed it up beyond the live cadence')

    def test_reaper_and_sweep_runtime_caps(self):
        rows = {row['name']: row for row in crons.rows()}
        for name in ('tmp-reaper', 'sweep-owed'):
            self.assertEqual(rows[name]['max_runtime_s'], 3600, name)

    def test_alert_digest_is_never_faster_than_daily(self):
        alerts = [row for row in crons.rows() if 'owed-digest --alert' in row['command']]
        self.assertTrue(alerts)
        for row in alerts:
            self.assertGreaterEqual(crons.interval_s(row['schedule']), 86400, row['name'])

    def test_paused_job_never_demands_or_installs_an_activation(self):
        import install_sync
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crons.toml'
            path.write_text('''[[row]]
name = "paused"
paused = true
where = "user"
schedule = "0 * * * *"
command = "/bin/true"
path = "/bin/true"
sig = "paused-fixture"
owner = "thinker"
project = "do-it-v2"
''')
            self.assertEqual(crons.check(manifest=path, crontab_text='', crond_dir=directory), [])
            with patch.object(crons, '_live_crontab_text', side_effect=AssertionError('live')):
                with self.assertRaisesRegex(ValueError, 'paused'):
                    crons.install('paused', manifest=path)
            with patch.object(install_sync.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, path.read_text(), '')):
                self.assertEqual(install_sync._crons_toml_at(Path(directory), 'HEAD'), {})


if __name__ == '__main__':
    unittest.main()
