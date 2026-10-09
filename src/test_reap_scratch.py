#!/usr/bin/env python3
"""Scratch retention and safety regressions, entirely inside temporary dirs."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import reap_tmp


class ScratchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'doit-scratch'
        self.root.mkdir()
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        self.now = datetime.now(timezone.utc)

    def entry(self, name, hours=0):
        path = self.root / name
        path.mkdir(parents=True)
        (path / 'file').write_text('scratch')
        stamp = self.now.timestamp() - hours * 3600
        for child in [*path.rglob('*'), path]:
            os.utime(child, (stamp, stamp))
        return path

    def scan(self, dry=False):
        return reap_tmp._scan_scratch(self.root, os.getuid(), self.now, self.proc, dry)

    def process(self, path, cmdline):
        proc = self.proc / str(os.getpid())
        proc.mkdir(exist_ok=True)
        (proc / 'cmdline').write_bytes(cmdline)
        (proc / 'cwd').symlink_to(path)

    def test_retention_and_dry_run(self):
        old = self.entry('old', 13)
        boundary = self.entry('boundary', 12)
        sweep = self.entry('sweeps/old', 7)
        young = self.entry('sweeps/young', 6)
        protected = self.entry('claude-1000/cache', 48)
        unknown = self.entry('grade/unknown', 48)
        shared = [self.entry(n + '/x', 48) for n in ('grader-claude', 'gate', 'node-compile-cache')]
        removed, _ = self.scan(True)
        self.assertEqual(set(removed), {str(old), str(sweep)})
        self.assertTrue(old.exists() and sweep.exists())
        self.scan()
        self.assertFalse(old.exists() or sweep.exists())
        self.assertTrue(all(p.exists() for p in (boundary, young, protected, unknown, *shared)))

    def test_cmdline_cwd_and_relative_references(self):
        paths = [self.entry(name, 24) for name in ('argv', 'cwd', 'relative', 'sweeps/busy')]
        self.process(paths[1], f'bash\0-c\0cat {paths[0]}/file\0--data={paths[3]}/file\0../relative/file\0'.encode())
        removed, _ = self.scan()
        self.assertEqual(removed, [])
        self.assertTrue(all(p.exists() for p in paths))

    def test_scan_snapshots_processes_and_sockets_once(self):
        paths = {name: self.entry(name, 24) for name in
                 ('argv', 'cwd', 'relative', 'fd', 'mapped', 'socket', 'pidfile')}
        free = [self.entry(f'free-{i}', 24) for i in range(24)]
        self.process(paths['cwd'], f'bash\0{paths["argv"]}/file\0../relative/file\0'.encode())
        proc = self.proc / str(os.getpid())
        (proc / 'fd').mkdir()
        (proc / 'fd/3').symlink_to(paths['fd'] / 'file')
        (proc / 'maps').write_text(
            f'1000-2000 r--p 00000000 00:00 1 {paths["mapped"]}/file\n')
        (self.proc / 'net').mkdir()
        (self.proc / 'net/unix').write_text(
            f'Header\n0: 00000002 00000000 00010000 0001 01 1 {paths["socket"]}/sock\n')
        pidfile = paths['pidfile'] / 'worker.pid'
        pidfile.write_text(str(os.getpid()))
        stamp = self.now.timestamp() - 24 * 3600
        os.utime(pidfile, (stamp, stamp))
        os.utime(paths['pidfile'], (stamp, stamp))
        with patch.object(reap_tmp, '_pid_dir_names', wraps=reap_tmp._pid_dir_names) as listing, \
                patch.object(reap_tmp, '_pid_refs', wraps=reap_tmp._pid_refs) as refs, \
                patch.object(Path, 'read_bytes', autospec=True, side_effect=Path.read_bytes) as reads, \
                patch.object(Path, 'read_text', autospec=True, side_effect=Path.read_text) as texts, \
                patch.object(reap_tmp, '_bound_sockets', wraps=reap_tmp._bound_sockets) as sockets:
            removed, kept = self.scan(True)
        self.assertEqual(set(removed), {str(path) for path in free})
        self.assertEqual({row['path'] for row in kept}, {str(path) for path in paths.values()})
        listing.assert_called_once_with(self.proc)
        refs.assert_called_once_with(self.proc, os.getpid())
        reads.assert_called_once_with(proc / 'cmdline')
        sockets.assert_called_once_with(self.proc)
        self.assertEqual(sum(call.args[0] == proc / 'maps' for call in texts.call_args_list), 1)
        self.assertEqual(sum(call.args[0] == self.proc / 'net/unix' for call in texts.call_args_list), 1)
        self.assertTrue(all(path.exists() for path in free))
        # A subsequent scan refreshes the evidence instead of reusing old rows.
        (proc / 'cmdline').write_bytes(b'bash\0')
        removed, _ = self.scan(True)
        self.assertIn(str(paths['argv']), removed)
        self.assertIn(str(paths['relative']), removed)

    def test_snapshot_failure_is_cached_and_fails_closed(self):
        old = [self.entry(f'old-{i}', 24) for i in range(3)]
        young = self.entry('young')
        self.process(self.base, b'bash\0')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError) as reads:
            removed, kept = self.scan()
        reads.assert_called_once()
        self.assertEqual(removed, [])
        reasons = {row['path']: row['reason'] for row in kept}
        self.assertTrue(all(reasons[str(path)] == 'undetermined' for path in old))
        self.assertEqual(reasons[str(young)], 'too young')
        self.assertTrue(all(path.exists() for path in old))
        with patch.object(reap_tmp, '_scratch_snapshot', wraps=reap_tmp._scratch_snapshot) as snapshot:
            # Young-only scans do not consult proc at all.
            for path in old:
                reap_tmp._delete(path)
            self.scan(True)
        snapshot.assert_not_called()

    def test_snapshot_preserves_other_uid_and_nondumpable_skips(self):
        for pid in (101, 102, 103):
            proc = self.proc / str(pid)
            proc.mkdir()
            (proc / 'cmdline').write_bytes(b'bash\0')
        real_stat = Path.stat
        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == self.proc / '102':
                # Only the ownership field is used for proc directories.
                from types import SimpleNamespace
                return SimpleNamespace(st_uid=os.getuid() + 1)
            return result
        with patch.object(Path, 'stat', autospec=True, side_effect=stat), \
                patch.object(reap_tmp.panes, 'alive', side_effect=lambda pid: pid != 103), \
                patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError) as refs, \
                patch.object(Path, 'read_bytes', autospec=True, side_effect=Path.read_bytes) as reads:
            snapshot = reap_tmp._scratch_snapshot(self.proc)
        self.assertEqual(snapshot, {})
        refs.assert_called_once_with(self.proc, 101)
        reads.assert_called_once_with(self.proc / '101/cmdline')

    def test_symlink_containment(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'valuable').write_text('keep')
        (self.root / 'grade').symlink_to(outside)
        (self.root / 'sweeps').symlink_to(outside)
        removed, errors = reap_tmp._reap_grade_views(self.root, [], self.proc, False)
        self.assertEqual(removed, [])
        self.assertTrue(errors)
        self.scan()
        self.assertEqual((outside / 'valuable').read_text(), 'keep')

    def test_terminal_stop_before_remove_and_failure(self):
        for event in ('spawn-done', 'spawn-failed', 'spawn-stale'):
            view = self.entry(f'grade/{event}')
            data = view / 'pg/data'
            data.mkdir(parents=True)
            (data / 'postmaster.pid').write_text('999999999\n')
            events = [{'type': event, 'spawn': event}]
            calls = []
            def stop(path):
                self.assertTrue(view.exists())
                calls.append(path)
            args = (self.root, events, self.proc)
            result, _ = reap_tmp._reap_grade_views(*args, True, stop_fn=stop)
            self.assertEqual(result, [str(view)])
            self.assertEqual(calls, [])
            def fail(path):
                raise RuntimeError('stop failed')
            result, errors = reap_tmp._reap_grade_views(*args, False, stop_fn=fail)
            self.assertEqual(result, [])
            self.assertTrue(errors and view.exists())
            result, errors = reap_tmp._reap_grade_views(*args, False, stop_fn=stop)
            self.assertEqual(result, [str(view)])
            self.assertFalse(errors or view.exists())
            self.assertEqual(calls, [data])

    def test_live_terminal_view_is_kept_without_stop(self):
        view = self.entry('grade/busy')
        self.process(self.base, f'python3\0{view}/file\0'.encode())
        with patch.object(reap_tmp, '_default_stop_cluster') as stop:
            removed, _ = reap_tmp._reap_grade_views(
                self.root, [{'type': 'spawn-done', 'spawn': 'busy'}], self.proc, False)
        self.assertEqual(removed, [])
        stop.assert_not_called()
        self.assertTrue(view.exists())

    def test_private_pg_is_reported_in_dry_run_and_stopped_before_reap(self):
        view = self.entry('grade/pg')
        data = view / 'pg/data'
        data.mkdir(parents=True)
        child = subprocess.Popen(['/bin/sleep', '60'])
        self.addCleanup(lambda: child.poll() is None and child.terminate())
        proc = self.proc / str(child.pid)
        proc.mkdir()
        (proc / 'cwd').symlink_to(data)
        (proc / 'cmdline').write_bytes(f'/pg/postgres\0-D\0{data}\0'.encode())
        (data / 'postmaster.pid').write_text(f'{child.pid}\nother PG fields\n')
        args = (self.root, [{'type': 'spawn-done', 'spawn': 'pg'}], self.proc)
        def stop(path):
            self.assertEqual(path, data)
            child.terminate()
            child.wait(timeout=5)
        removed, _ = reap_tmp._reap_grade_views(*args, True, stop_fn=stop)
        self.assertEqual(removed, [str(view)])
        self.assertIsNone(child.poll())
        removed, errors = reap_tmp._reap_grade_views(*args, False, stop_fn=stop)
        self.assertEqual(removed, [str(view)])
        self.assertFalse(errors or view.exists())

    def test_unreadable_process_fails_closed(self):
        path = self.entry('old', 24)
        with patch.object(reap_tmp, '_scratch_refs', side_effect=PermissionError):
            removed, kept = self.scan()
        self.assertEqual(removed, [])
        self.assertEqual(kept[0]['reason'], 'undetermined')
        self.assertTrue(path.exists())

    def test_stop_command_checks_exit_status(self):
        data = self.root / 'grade/x/pg/data'
        data.mkdir(parents=True)
        (data / 'postmaster.pid').write_text(str(os.getpid()))
        with patch.object(reap_tmp.shutil, 'which', return_value='/pg/pg_ctl'), \
                patch.object(reap_tmp.subprocess, 'run') as run:
            reap_tmp._default_stop_cluster(self.root / 'grade/x/pg/data')
        self.assertEqual(run.call_args.args[0], [
            '/pg/pg_ctl', '-D', str(self.root / 'grade/x/pg/data'), 'stop', '-m', 'fast'])
        self.assertTrue(run.call_args.kwargs['check'])


if __name__ == '__main__':
    unittest.main()
