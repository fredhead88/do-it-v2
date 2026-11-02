"""Read-only seats receive bounded git history from real packed repositories."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import packet
import spec_history


class HistoryTests(unittest.TestCase):
    def test_packed_history_and_patch_only_for_footprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'target.py').write_text('original\n')
            (repo / 'unrelated.py').write_text('private-marker\n')
            git('add', '.')
            git('commit', '-qm', 'fixture history')
            git('gc', '--prune=now')
            ctx = SimpleNamespace(a=SimpleNamespace(role='spec-writer', repo=str(repo)),
                                  footprint=lambda: [], project=lambda: 'fixture')
            before = git('status', '--porcelain'), git('rev-parse', 'HEAD')
            items = spec_history.packet_items(ctx, ['- Footprint (= the merge grant, `Writes:`): target.py'])
            body = '\n'.join(items)
            self.assertIn('fixture history', body)
            self.assertIn('+original', body)
            self.assertNotIn('private-marker', body)
            self.assertEqual(before, (git('status', '--porcelain'), git('rev-parse', 'HEAD')))
            with patch.object(spec_history.subprocess, 'run', side_effect=AssertionError('unrelated role called git')):
                ctx.a.role = 'grader'
                self.assertEqual(spec_history.packet_items(ctx, ['unchanged']), ['unchanged'])

    def test_missing_repo_is_explicitly_undetermined(self):
        ctx = SimpleNamespace(a=SimpleNamespace(role='spec-writer', repo='/nonexistent-fixture'),
                              footprint=lambda: ['a.py'], project=lambda: 'fixture')
        self.assertIn('undetermined', '\n'.join(spec_history.packet_items(ctx, [])))

    def test_carried_packet_gets_history_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q'); git('config', 'user.name', 'f'); git('config', 'user.email', 'f@example.invalid')
            (repo / 'target.py').write_text('x\n')
            git('add', '.'); git('commit', '-qm', 'carried history')
            raw = '---\nintent: "Writes: target.py"\n---\n\n# T\n'
            out = spec_history.augment_packet_text(raw, str(repo), 'p')
            self.assertIn(spec_history.HEADING, out)
            self.assertIn('carried history', out)
            self.assertTrue(out.startswith('---\nintent'))
            self.assertEqual(out, spec_history.augment_packet_text(out, str(repo), 'p'))


if __name__ == '__main__':
    unittest.main()
