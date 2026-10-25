#!/usr/bin/env python3
"""Destructive disk reapers: every path and process fixture is disposable."""
import hashlib, json, os, pathlib, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import disk_reaper as dr
import reap_tmp

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


class DiskReaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / 'var-tmp'
        self.root.mkdir()
        self.proc = self.base / 'proc'
        self.proc.mkdir()

    def file(self, path, days=20, data='bytes'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        self.old(path, days)
        return path

    def old(self, path, days=20):
        t = (NOW - timedelta(days=days)).timestamp()
        os.utime(path, (t, t), follow_symlinks=False)

    def ref(self, name, path):
        pid = self.proc / str(os.getpid())
        (pid / 'fd').mkdir(parents=True, exist_ok=True)
        (pid / name).symlink_to(path)

    def var(self, **kw):
        return dr.reap_var_tmp(self.root, now=NOW, proc_root=self.proc, **kw)

    def test_var_tmp(self):
        tree = self.root / 'tree'
        self.file(tree / 'nested' / 'data', data='12345')
        for p in (tree / 'nested', tree):
            self.old(p)
        old = self.file(self.root / 'old', data='123')
        target = self.file(self.base / 'outside')
        link = self.root / 'link'
        link.symlink_to(target)
        self.old(link)
        young = self.file(self.root / 'young', 2)
        dt = self.file(self.root / 'dt')
        cwd = self.file(self.root / 'cwd')
        fd = self.file(self.root / 'fd')
        recorded = self.file(self.root / 'recorded')
        alien = self.file(self.root / 'alien')
        self.ref('cwd', cwd)
        self.ref('fd/7', fd)
        real = os.lstat
        def lstat(path, *args, **kwargs):
            st = real(path, *args, **kwargs)
            if str(path) == str(alien):
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        with patch.object(os, 'lstat', side_effect=lstat):
            dry = self.var(recorded_refs=(recorded,), dry_run=True)
            self.assertEqual(len(list(self.root.iterdir())), 9)
            self.assertEqual(set(dry['removed']), {str(tree), str(old), str(link)})
            out = self.var(recorded_refs=(recorded,))
        self.assertEqual(out['freed_bytes'], 8)
        self.assertEqual(set(out['removed']), set(dry['removed']))
        self.assertTrue(target.exists())
        for p in (young, dt, cwd, fd, recorded, alien):
            self.assertTrue(p.exists(), str(p))
        self.assertEqual(len(out['kept']), 6)

    def test_var_fail_closed(self):
        old = self.file(self.root / 'old')
        self.ref('cwd', self.base)
        with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')):
            out = self.var()
        self.assertTrue(old.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        with patch.object(reap_tmp, '_newest_mtime', side_effect=PermissionError('mtime')):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        with patch.object(os, 'lstat', side_effect=PermissionError('lstat')):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])

    def test_other_uid_unreadable_process_does_not_block_cleanup(self):
        old = self.file(self.root / 'old')
        log = self.file(self.base / 'sessions/old.jsonl')
        pid = self.proc / str(os.getpid())
        pid.mkdir()
        real_lstat = os.lstat
        def other_uid(path, *args, **kwargs):
            st = real_lstat(path, *args, **kwargs)
            if pathlib.Path(path) == pid:
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        self.assertEqual(sum(p.exists() for p in (old, log)), 2)
        with (patch.object(os, 'lstat', side_effect=other_uid),
              patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('other uid')) as refs):
            var = self.var()
            sessions = self.sessions()
        refs.assert_not_called()
        self.assertEqual(var['removed'], [str(old)])
        self.assertEqual(sessions['removed'], [str(log)])
        self.assertEqual(var['other_uid_skipped'], 1)
        self.assertEqual(sessions['other_uid_skipped'], 1)
        self.assertFalse(old.exists())
        self.assertFalse(log.exists())

    def test_process_listing_and_pid_ownership_fail_closed(self):
        old = self.file(self.root / 'old')
        log = self.file(self.base / 'sessions/old.jsonl')
        pid = self.proc / str(os.getpid())
        pid.mkdir()
        real_lstat = os.lstat
        def unreadable_pid(path, *args, **kwargs):
            if pathlib.Path(path) == pid:
                raise PermissionError('pid ownership')
            return real_lstat(path, *args, **kwargs)
        for failure in (patch.object(reap_tmp, '_pid_dir_names', side_effect=PermissionError('proc listing')),
                        patch.object(os, 'lstat', side_effect=unreadable_pid)):
            with failure:
                var = self.var()
                sessions = self.sessions()
            self.assertTrue(old.exists())
            self.assertTrue(log.exists())
            self.assertIn('undetermined', var['kept'][0]['reason'])
            self.assertIn('undetermined', sessions['kept'][0]['reason'])
            self.assertFalse((self.base / 'state').exists())

    def test_var_nested_and_scan_safety(self):
        tree = self.root / 'tree'
        child = self.file(tree / 'child', 1)
        self.old(tree)
        self.assertEqual(self.var()['kept'][0]['reason'], 'too young')
        self.old(child)
        real_inventory = dr._inventory
        with patch.object(dr, '_inventory', side_effect=PermissionError('unreadable child')):
            out = self.var()
        self.assertTrue(child.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        with patch.object(os.path, 'ismount', side_effect=lambda p: pathlib.Path(p) == tree):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(child.exists())
        def touch_child(path, *args, **kw):
            self.old(child, 1)
            return real_inventory(path, *args, **kw)
        with patch.object(dr, '_inventory', side_effect=touch_child):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(child.exists())
        self.old(child)
        outside = self.file(self.base / 'outside')
        # A replacement after inventory cannot redirect deletion through a link.
        def replace(path, *args, **kw):
            records = real_inventory(path, *args, **kw)
            child.unlink()
            tree.rmdir()
            tree.symlink_to(outside.parent, target_is_directory=True)
            return records
        with patch.object(dr, '_inventory', side_effect=replace):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(outside.exists())

    def sessions(self, **kw):
        return dr.reap_session_logs([self.root, self.base / 'sessions'], now=kw.pop('now', NOW),
                                   proc_root=self.proc, state_path=self.base / 'state/date.json', **kw)

    def test_sessions(self):
        (self.base / 'sessions').mkdir()
        old = self.file(self.root / 'project' / 'old.jsonl', data='1234')
        young = self.file(self.root / 'young.jsonl', 13)
        opened = self.file(self.root / 'open.jsonl')
        notes = self.file(self.root / 'memory/notes.md')
        settings = self.file(self.root / 'settings.json')
        outside = self.file(self.base / 'outside.jsonl')
        (self.root / 'alias.jsonl').symlink_to(outside)
        (self.root / 'linked').symlink_to(self.base, target_is_directory=True)
        self.ref('fd/3', opened)
        self.assertTrue(self.sessions(dry_run=True)['ran'])
        self.assertTrue(old.exists())
        self.assertFalse((self.base / 'state').exists())
        out = self.sessions()
        self.assertEqual(out['removed'], [str(old)])
        self.assertEqual(out['freed_bytes'], 4)
        self.assertFalse(old.parent.exists())
        self.assertTrue(self.root.is_dir())
        for p in (young, opened, notes, settings, outside):
            self.assertTrue(p.exists())
        self.assertFalse(self.sessions()['ran'])
        self.assertTrue(self.sessions(now=NOW + timedelta(days=1))['ran'])

    def test_session_scan_failure_does_not_mark(self):
        (self.base / 'sessions').mkdir()
        old = self.file(self.root / 'old.jsonl')
        self.ref('cwd', self.base)
        with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')):
            out = self.sessions()
        self.assertTrue(old.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        self.assertFalse((self.base / 'state').exists())
        real = dr._inventory
        def fail(path, *args, **kwargs):
            if pathlib.Path(path) == self.base / 'sessions':
                raise PermissionError('scan')
            return real(path, *args, **kwargs)
        with patch.object(dr, '_inventory', side_effect=fail):
            out = self.sessions()
        self.assertTrue(out['errors'])
        self.assertFalse((self.base / 'state').exists())


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiskReaperTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    body = json.dumps({'tests': result.testsRun, 'success': result.wasSuccessful()}, sort_keys=True)
    print(json.dumps(dict(url='cli://disk-reaper/AC3-AC4-AC7', status=0 if result.wasSuccessful() else 1,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    raise SystemExit(not result.wasSuccessful())
