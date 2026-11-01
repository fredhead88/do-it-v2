"""A real builder worktree receives private, lock-matched dashboard dependencies."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ['DOIT_ROOT'] = tempfile.mkdtemp(prefix='builder-deps-import-')
import dispatch
import provision
import build_runner
import fold


class BuilderDependencies(unittest.TestCase):
    def test_preflight_copies_shared_install_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.enterContext(patch.multiple(fold, ROOT=root, EVENTS=root/'events'))
            self.enterContext(patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_LEDGER_FILE='L-builder-fixture.jsonl'))
            repo = root/'repos/albert-scott'
            repo.mkdir(parents=True)
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo/'dashboard').mkdir()
            (repo/'dashboard/package-lock.json').write_text('{"lockfileVersion":3}')
            (repo/'dashboard/package.json').write_text('{}')
            (repo/'.gitignore').write_text('node_modules/\n')
            git('add', '.')
            git('commit', '-qm', 'fixture')
            shared = repo/'dashboard/node_modules'
            (shared/'.bin').mkdir(parents=True)
            (shared/'fixture').mkdir()
            (shared/'fixture/cli.js').write_text('shared dependency')
            (shared/'.bin/tool').symlink_to('../fixture/cli.js')
            tree = root/'worktrees/albert-scott/l-spec-9966'
            git('worktree', 'add', '-qb', 'l-spec-9966', str(tree))
            args = SimpleNamespace(subject='L-spec-9966', project='albert-scott')
            with patch.object(dispatch, 'ROOT', root), \
                 patch.object(build_runner, '_required', side_effect=AssertionError('matching install must not run npm')):
                dispatch.builder_worktree_preflight(args, str(tree), None, {}, self.fail)
                target = tree/'dashboard/node_modules'
                self.assertTrue((target/'fixture/cli.js').is_file())
                self.assertFalse(target.is_symlink())
                self.assertEqual((target/'.bin/tool').readlink(), Path('../fixture/cli.js'))
                (target/'fixture/cli.js').write_text('private changed dependency')
                dispatch.builder_worktree_preflight(args, str(tree), None, {}, self.fail)
                self.assertEqual((target/'fixture/cli.js').read_text(), 'private changed dependency')
            self.assertEqual((shared/'fixture/cli.js').read_text(), 'shared dependency')
            # A builder can even empty its private copy without damaging shared deps.
            import shutil
            shutil.rmtree(target)
            self.assertTrue((shared/'fixture/cli.js').is_file())
            # A changed branch lock must install only in a private cache stage.
            (tree/'dashboard/package-lock.json').write_text('{"lockfileVersion":3,"changed":true}')
            stages = []
            def install(argv, **kw):
                stages.append(kw['cwd'])
                directory = kw['cwd']/'node_modules'
                directory.mkdir()
                (directory/'new-lock').write_text('fresh')
            with patch.object(dispatch, 'ROOT', root), \
                 patch.object(build_runner, '_required', side_effect=install):
                dispatch.builder_worktree_preflight(args, str(tree), None, {}, self.fail)
            self.assertEqual(len(stages), 1)
            self.assertTrue(stages[0].is_relative_to(root/'cache/node_modules'))
            self.assertEqual((target/'new-lock').read_text(), 'fresh')
            self.assertEqual((shared/'fixture/cli.js').read_text(), 'shared dependency')

    def test_packet_explains_install_prerequisites(self):
        import packet
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as name:
            spec = Path(name)/'spec.md'
            spec.write_text('Run npm ci before verification.')
            context = Mock()
            context.evs = []
            context.a = SimpleNamespace(repo=name, base_sha=None, produces=None, conventions=None, subject='L-spec-9966')
            context.all_of.return_value = []
            context.spec_file.return_value = spec
            context.charter_file.return_value = None
            context.charter_id.return_value = None
            context.standing.return_value = ([], [])
            context.last.return_value = None
            context.footprint.return_value = []
            with patch.object(packet, 'hint_block', return_value=[]), \
                 patch.object(packet, 'verify_base_sha', return_value='a'*40), \
                 patch.object(packet, 'write_verify_script', return_value=None), \
                 patch.object(packet, 'done_condition', return_value='done'), \
                 patch.object(packet, 'conflicts', return_value=[]), \
                 patch.object(packet, 'preflight_lines', return_value=[]), \
                 patch.object(packet.coupled_tests, 'packet_lines', return_value=[]), \
                 patch.object(packet.coupled_tests, 'run_line', return_value='checks'):
                text = '\n'.join(packet.p_builder(context))
            self.assertIn('satisfies an npm ci prerequisite', text)
            self.assertIn('Never run npm ci', text)


if __name__ == '__main__':
    unittest.main()
