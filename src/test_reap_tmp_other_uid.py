#!/usr/bin/env python3
"""L-spec-1766: the tmp process snapshot, using only fixture paths."""
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import reap_tmp


class OtherUidTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', '/var/tmp'))
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'tmp'
        self.proc = self.base / 'proc'
        self.root.mkdir()
        self.proc.mkdir()
        self.uid = os.getuid()
        self.now = datetime.now(timezone.utc)
        self.pid = 1766
        (self.proc / str(self.pid)).mkdir()
        self.addCleanup(patch.stopall)
        patch.object(reap_tmp.panes, 'alive', return_value=True).start()
        patch.object(reap_tmp, 'user_quota', return_value={
            'used_bytes': 0, 'limit_bytes': 1000, 'limit_source': 'fixture'}).start()
        patch.object(reap_tmp.disk_reaper, 'LOOK_TOML_PATH', self.base / 'look.toml').start()

    def entry(self, name):
        path = self.root / name
        path.mkdir()
        stamp = self.now.timestamp() - 24 * 3600
        os.utime(path, (stamp, stamp))
        return path

    def run_reaper(self, **kwargs):
        return reap_tmp.run(root=self.root, proc_root=self.proc, now=self.now, **kwargs)

    def test_ac1_other_uid_links_are_never_read(self):
        old = self.entry('old')
        real_lstat = os.lstat

        def other_uid(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == self.proc / str(self.pid):
                fields = list(result)
                fields[4] = self.uid + 1
                return os.stat_result(fields)
            return result

        self.assertEqual(len(list(self.root.iterdir())), 1)
        with (patch.object(os, 'lstat', side_effect=other_uid),
              patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError) as refs):
            result = self.run_reaper(uid=self.uid)
        refs.assert_not_called()
        self.assertEqual(result['removed'], [str(old)])
        self.assertFalse(old.exists())
        self.assertEqual(result['tmp_other_uid_skipped'], 1)
        self.assertEqual(result['other_uid_skipped'], 0)
        print('L-spec-1766 AC1 ok')

    def test_ac2_own_uid_uncertainty_keeps_every_entry(self):
        entries = [self.entry(f'old-{i}') for i in range(3)]
        for error in (PermissionError, OSError):
            with (self.subTest(error=error),
                  patch.object(reap_tmp, '_pid_refs', side_effect=error) as refs):
                result = self.run_reaper(uid=self.uid)
            refs.assert_called_once_with(self.proc, self.pid)
            self.assertEqual(result['removed'], [])
            self.assertEqual(result['kept'], [
                {'path': str(path), 'reason': 'undetermined'} for path in entries])
            self.assertTrue(all(path.exists() for path in entries))
            self.assertEqual(result['tmp_other_uid_skipped'], 0)
        print('L-spec-1766 AC2 ok')

    def test_ac3_own_uid_cwd_protects_entry(self):
        old = self.entry('old')
        (self.proc / str(self.pid) / 'cwd').symlink_to(old)
        result = self.run_reaper(uid=self.uid)
        self.assertEqual(result['removed'], [])
        self.assertEqual(len(result['kept']), 1)
        self.assertEqual(result['kept'][0]['path'], str(old))
        self.assertTrue(result['kept'][0]['reason'].startswith('a live process references'))
        self.assertTrue(old.exists())
        # The explicit run uid, even when different from os.getuid(), controls the snapshot.
        result = self.run_reaper(uid=self.uid + 1)
        self.assertEqual(result['tmp_other_uid_skipped'], 1)
        self.assertTrue(old.exists())
        print('L-spec-1766 AC3 ok')

    def test_ac4_snapshot_once_per_run_and_refreshed(self):
        entries = [self.entry(f'old-{i}') for i in range(3)]
        self.assertEqual(len(list(self.root.iterdir())), 3)
        with (patch.object(reap_tmp, '_own_uid_refs', wraps=reap_tmp._own_uid_refs) as snapshot,
              patch.object(reap_tmp, '_pid_refs', return_value=[]) as refs):
            result = self.run_reaper(dry_run=True)
            snapshot.assert_called_once_with(self.proc, self.uid)
            refs.assert_called_once_with(self.proc, self.pid)
        self.assertEqual(result['removed'], [str(path) for path in entries])
        self.assertTrue(all(path.exists() for path in entries))
        with patch.object(reap_tmp, '_pid_refs', return_value=[str(entries[0])]):
            refreshed = self.run_reaper(dry_run=True)
        self.assertNotIn(str(entries[0]), refreshed['removed'])
        self.assertEqual(len(refreshed['kept']), 1)
        print('L-spec-1766 AC4 ok')


if __name__ == '__main__':
    unittest.main()
