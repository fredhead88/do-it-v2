#!/usr/bin/env python3
"""Real disposable repos prove runner/gate cannot follow an integrator redirect."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import build_runner as runner
import merge_gate as gate


class IntegratorConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='integrator-config-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'integrator'
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('commit', '-q', '--allow-empty', '-m', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env, stderr=subprocess.DEVNULL).decode().strip()

    def preflight(self, repo=None):
        with patch.object(gate, 'ENV', self.env), patch.object(gate, '_START', time.monotonic()):
            gate.preflight(str(repo or self.repo))

    def test_gate_names_local_core_worktree_before_following_redirect(self):
        for exists in (False, True):
            with self.subTest(scratch_exists=exists):
                target = self.root / ('exists' if exists else 'missing')
                if exists:
                    target.mkdir()
                self.git('config', 'core.worktree', str(target))
                before = (self.repo / '.git/config').read_bytes()
                with self.assertRaisesRegex(gate.Undetermined, r'core\.worktree') as caught:
                    self.preflight()
                self.assertEqual(caught.exception.reason, 'repository-precondition')
                self.assertFalse(caught.exception.retryable)
                self.assertEqual((self.repo / '.git/config').read_bytes(), before)
                self.git('config', '--unset', 'core.worktree')
        self.preflight()
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')

    def test_ordinary_linked_worktree_remains_supported(self):
        linked = self.root / 'linked'
        self.git('worktree', 'add', '--detach', str(linked), 'HEAD')
        self.preflight(linked)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.sha)

    def test_direct_fixture_script_cannot_reinitialize_integrator(self):
        import sys
        config = self.repo / '.git/config'
        before = config.read_bytes()
        tree = self.root / 'extracted'
        tree.mkdir()
        env = {**self.env, 'GIT_DIR': str(self.repo / '.git'), 'GIT_WORK_TREE': str(tree),
               'DOIT_ROOT': str(self.root / 'doit'), 'DOIT_NO_POKE': '1'}
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name('test_autodispatch.py'))],
                              env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.sha)

    def test_runner_clone_isolates_ambient_git_directory_and_config_writes(self):
        config = self.repo / '.git/config'
        before = config.read_bytes()
        scratch = self.root / 'scratch'
        tails = self.root / 'tails'
        tails.mkdir()
        aliases, backing = self.root / 'aliases', self.root / 'backing'
        aliases.mkdir(); backing.mkdir()
        def sub(name):
            result = scratch / name
            result.mkdir(parents=True, exist_ok=True)
            return result
        with patch.dict(os.environ, DOIT_ROOT=str(self.root / 'doit'),
                        GIT_DIR=str(self.repo / '.git'), GIT_WORK_TREE=str(self.root / 'wrong-tree'),
                        GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.worktree',
                        GIT_CONFIG_VALUE_0=str(self.root / 'injected-tree')), \
             patch.object(runner.scratch, 'sub', side_effect=sub), \
             patch.object(runner, 'SHORT_TMP_ROOT', aliases), \
             patch.object(runner, 'SHORT_TMP_BACKING_ROOT', backing), \
             patch.object(runner, '_provision', return_value={}):
            rows, _, _, _, _ = runner._run_tree('L-spec-9001', self.sha, self.repo, {},
                [('AC1', 'git config --local core.worktree /fixture/clone-only')],
                tails, 0, 4096, time.monotonic() + 30)
        self.assertEqual(rows[0]['exit'], 0)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'main')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.sha)
        self.assertFalse((self.root / 'wrong-tree').exists())


if __name__ == '__main__':
    unittest.main()
