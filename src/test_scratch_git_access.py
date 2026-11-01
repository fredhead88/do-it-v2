"""An empty .git is diagnostic debris, while real Git entries block access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dispatch
import look
import scratch


class ScratchGitAccessTests(unittest.TestCase):
    def test_empty_git_does_not_silently_disable_sweep_access(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            checkout = root / 'checkout'
            checkout.mkdir()
            with patch.object(dispatch, 'readonly_dsn', return_value='synthetic-readonly'), \
                    patch.object(dispatch, '_parse_env', return_value={'SUPABASE_DB_URL_RO': 'synthetic-readonly'}):
                self.assertEqual(dispatch.provision_sweep_env(root / 'sweeps/one', checkout), 'readonly')
                (root / '.git/HEAD').write_text('ref: refs/heads/main\n')
                self.assertEqual(dispatch.provision_sweep_env(root / 'sweeps/two', checkout), 'refused')
                self.assertFalse((root / 'sweeps/two/.env').exists())
            target = root / 'empty-target'
            target.mkdir()
            (root / '.git/HEAD').unlink()
            (root / '.git').rmdir()
            (root / '.git').symlink_to(target, target_is_directory=True)
            self.assertTrue(scratch._has_git_entry(root))

    def test_stray_git_and_refused_access_have_named_health_alerts(self):
        import look_scratch
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            root = Path(directory)
            scratch_root = root / 'scratch'
            (scratch_root / '.git').mkdir(parents=True)
            latest = dict(type='spawn-started', subject='L-sweep-fixture', project='fixture',
                          role='owed-verifier', dsn_role='refused', cwd=str(scratch_root / 'batch'))
            readings = []
            def settle(kind, key, owner, bad, reading, *args):
                readings.append((kind, bad, reading))
            with patch.dict('os.environ', DOIT_SCRATCH=str(scratch_root)), \
                    patch.object(look, '_settle', side_effect=settle):
                look_scratch.check([latest], root, [], [])
                self.assertTrue(all(row[1] for row in readings))
                self.assertTrue(any('.git at ' in row[2]['why'] for row in readings))
                (scratch_root / '.git').rmdir()
                readings.clear()
                look_scratch.check([latest, dict(latest, dsn_role='readonly')], root, [], [])
                self.assertTrue(all(not row[1] for row in readings))


if __name__ == '__main__':
    unittest.main()
