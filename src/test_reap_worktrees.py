#!/usr/bin/env python3
"""Real disposable git worktrees: state, dirty files, branches, roots, dry run."""
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import reap_worktrees as r


def git(path, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='t@example.test',
               GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='t@example.test')
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                          text=True, check=True, env=env).stdout


class Tests(unittest.TestCase):
    def setUp(self):
        environment = tempfile.TemporaryDirectory()
        self.addCleanup(environment.cleanup)
        root = pathlib.Path(environment.name)
        for name, value in (('ROOT', root), ('EVENTS', root / 'events')):
            setting = patch.object(r.fold, name, value)
            setting.start(); self.addCleanup(setting.stop)

    def test_scope_dirty_terminal_dry_run_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo = root / 'repos/project'; repo.mkdir(parents=True)
            git(repo, 'init', '-q', '-b', 'main')
            (repo / 'seed').write_text('seed')
            git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'seed')
            paths = {}
            for n in (1, 2, 3, 4, 5, 6, 8, 9):
                path = root / 'worktrees/project' / f'l-spec-{n:04d}'
                git(repo, 'worktree', 'add', '-q', '-b', f'l-spec-{n:04d}', str(path))
                paths[n] = path
            (paths[2] / 'untracked').write_text('keep me')
            (paths[3] / 'seed').write_text('uncommitted')
            (paths[9] / '.gitignore').write_text('ignored-data\n')
            git(paths[9], 'add', '.gitignore'); git(paths[9], 'commit', '-qm', 'ignore data')
            (paths[9] / 'ignored-data').write_text('keep ignored data too')
            outside = root / 'outside/l-spec-0007'
            git(repo, 'worktree', 'add', '-q', '-b', 'l-spec-0007', str(outside))
            events = [dict(type='spec-killed', actor='thinker', subject=f'L-spec-{n:04d}') for n in range(1, 10) if n not in (4, 8)]
            events += [dict(type='spawn-started', role='builder', spawn='L-builder-123', subject='L-spec-0006')]
            dry = r.run(events, root=root, dry_run=True)
            self.assertEqual(sum(x['action'] == 'would-remove' for x in dry), 2)
            self.assertTrue(all(p.exists() for p in paths.values()))
            rows = r.run(events, root=root)
            self.assertEqual(sum(x['action'] == 'removed' for x in rows), 2)
            for n in (1, 5):
                self.assertFalse(paths[n].exists())
                self.assertTrue(git(repo, 'rev-parse', '--verify', f'l-spec-{n:04d}').strip())
            for n in (2, 3, 4, 6, 8, 9): self.assertTrue(paths[n].exists())
            dirty = [x for x in rows if 'dirty' in x['reason']]
            self.assertEqual({x['path'] for x in dirty}, {str(paths[2]), str(paths[3]), str(paths[9])})
            self.assertTrue(outside.exists()); self.assertTrue(repo.exists())
    def test_tmp_cadence_does_not_call_legacy_branch_deletion(self):
        import reap_tmp
        with tempfile.TemporaryDirectory() as name:
            base = pathlib.Path(name)
            root = base / 'tmp'; root.mkdir()
            proc = base / 'proc'; (proc / 'net').mkdir(parents=True)
            (proc / 'net/unix').write_text('header\n')
            rows = [dict(path=str(base / 'worktrees/project/l-spec-1234'), branch='l-spec-1234',
                         action='removed', freed_bytes=42)]
            with patch.object(reap_tmp.tree_cleanup, 'reap_merged_specs', side_effect=AssertionError('legacy cleanup')), \
                 patch.object(reap_tmp.disk_reaper, 'process_refs', return_value=[]) , \
                 patch.object(r, 'run', return_value=rows) as handler:
                result = reap_tmp.run(root=root, proc_root=proc, events=[], worktree_fn=handler)
            self.assertEqual(result['reaped_worktrees'], ['l-spec-1234'])
            self.assertEqual(result['freed_bytes_worktrees'], 42)
            handler.assert_called_once_with([], dry_run=False, proc_refs=[])

    def test_git_boundary_ignores_caller_git_variables(self):
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name)
            for project in ('actual', 'other'):
                path = root / project; path.mkdir()
                git(path, 'init', '-q', '-b', 'main')
            with patch.dict(os.environ, GIT_DIR=str(root / 'other/.git'), GIT_WORK_TREE=str(root / 'other')):
                actual = r.git(root / 'actual', 'rev-parse', '--show-toplevel').strip()
            self.assertEqual(actual, str(root / 'actual'))

    def test_terminal_states(self):
        for state in ('accepted', 'killed', 'void', 'shipped', 'shipped-owed-due', 'shipped-owed-evidence', 'shipped-owed-expired'):
            self.assertTrue(r.terminal(state), state)
        for state in ('building', 'written', 'reviewing', 'dropped', 'closed-unbuilt'):
            self.assertFalse(r.terminal(state), state)
    def test_symlink_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'outside').mkdir()
            (root / 'worktrees').symlink_to(root / 'outside', target_is_directory=True)
            self.assertEqual(r.run([], root=root)[0]['reason'], 'outside root allowlist')
    def test_cron_cadence(self):
        import tomllib
        doc = tomllib.loads((pathlib.Path(__file__).resolve().parent.parent / 'crons.toml').read_text())
        rows = {r['name']: r for r in doc['row']}
        self.assertEqual(rows['worktree-reaper']['schedule'], rows['tmp-reaper']['schedule'])
        self.assertIn('cron-run worktree-reaper -- {doit} reap-worktrees', rows['worktree-reaper']['command'])

    def test_dispatch_help(self):
        import subprocess
        doit = pathlib.Path(__file__).resolve().parent.parent / 'doit'
        result = subprocess.run([str(doit), 'reap-worktrees', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--dry-run', result.stdout)

    def test_cli_dry_run(self):
        with patch.object(r.disk_reaper, 'process_refs', return_value=[]), patch.object(r.fold, 'read_events', return_value=[]), patch.object(r, 'run', return_value=[]) as run:
            self.assertEqual(r.main(['--dry-run']), 0)
            self.assertTrue(run.call_args.kwargs['dry_run'])

if __name__ == '__main__': unittest.main()
