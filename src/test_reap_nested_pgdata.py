#!/usr/bin/env python3
"""Nested pgdata must be selected independently, without widening tmp recursion."""
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import reap_tmp as r

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)

class Tests(unittest.TestCase):
    def test_nested_retention_and_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'tmp'; root.mkdir()
            proc = pathlib.Path(tmp) / 'proc'; (proc / 'net').mkdir(parents=True)
            (proc / 'net/unix').write_text('header\n')
            pad = root / 'claude-1000/project/session/scratchpad'
            paths = {name: pad / ('pgdata-' + name) for name in ('old', 'young', 'live', 'socket', 'git')}
            for name, path in paths.items():
                path.mkdir(parents=True)
                if name == 'live': (path / 'postmaster.pid').write_text(str(os.getpid()))
                if name == 'git': (path / '.git').write_text('worktree')
            other = pad / 'ordinary'; other.mkdir()
            outside = root / 'other/project/scratchpad/pgdata-old'; outside.mkdir(parents=True)
            target = pathlib.Path(tmp) / 'target'; target.mkdir()
            (pad / 'pgdata-link').symlink_to(target, target_is_directory=True)
            for parent, dirs, files in os.walk(root):
                for name in dirs + files:
                    os.utime(pathlib.Path(parent, name), (NOW.timestamp()-49*3600,)*2, follow_symlinks=False)
            for path in [root, *paths.values()]:
                os.utime(path, (NOW.timestamp()-49*3600,)*2)
            os.utime(paths['young'], (NOW.timestamp()-47*3600,)*2)
            (proc / 'net/unix').write_text(f'header\n0 0 0 0 0 0 0 {paths["socket"]}/sock\n')
            # Parent is kept by a live process; nested dead data is still eligible.
            refs = [str(root / 'claude-1000')]
            with patch.object(r, '_own_uid_refs', return_value=(refs, 0)):
                result = r.run(root=root, proc_root=proc, now=NOW, dry_run=True)
                self.assertIn(str(paths['old']), result['removed'])
                self.assertTrue(paths['old'].exists())
                result = r.run(root=root, proc_root=proc, now=NOW)
            self.assertFalse(paths['old'].exists())
            for name in ('young', 'live', 'socket', 'git'):
                self.assertTrue(paths[name].exists(), name)
            self.assertTrue(other.exists())
            self.assertTrue(target.exists())
            self.assertNotIn(outside, r._nested_pgdata(root))

if __name__ == '__main__': unittest.main()
