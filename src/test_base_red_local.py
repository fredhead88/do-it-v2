"""Local nightly evidence is exact-sha, complete, offline, and never guessed."""
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import tempfile
import unittest

import base_red
import base_red_local


class LocalBaseRedTests(unittest.TestCase):
    def test_collector_records_real_local_suite_and_cleans_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repos/do-it-v2'
            (repo / 'src').mkdir(parents=True)
            (repo / 'src/test_bad.py').touch()
            (repo / 'doit').write_text('#!/bin/sh\necho "FAIL test_bad.py (exit 1)"\n'
                                     'echo "PASS retired check"\necho "PASS machine check"\nexit 1\n')
            (repo / 'doit').chmod(0o700)
            for args in (['init', '-q', '-b', 'main'], ['add', '.'],
                         ['-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'base']):
                subprocess.run(['git', '-C', str(repo), *args], check=True)
            with patch.object(base_red.fold, 'ROOT', root):
                sha, ids = base_red_local.nightly()
                result = base_red.failing('do-it-v2', sha)
                self.assertEqual(base_red.known_red('do-it-v2').keys(), {'src/test_bad.py'})
            self.assertEqual(ids, ['src/test_bad.py'])
            self.assertTrue(result['complete'])
            self.assertEqual(result['ids']['pytest'], ids)
            worktrees = subprocess.check_output(['git', '-C', str(repo), 'worktree', 'list'], text=True)
            self.assertEqual(len(worktrees.splitlines()), 1)

    def test_complete_and_truncated_outputs(self):
        expected = ['test_a.py', 'test_b.mjs', 'retired check', 'machine check']
        output = 'PASS test_a.py\nFAIL test_b.mjs (exit 1)\nPASS retired check\nPASS machine check\n'
        self.assertEqual(base_red_local.parse(output, expected, 1), ['src/test_b.mjs'])
        for text, code in ((output, 0), (output, 124), (output.rsplit('PASS machine', 1)[0], 1),
                           (output + 'PASS test_a.py\n', 1)):
            with self.assertRaises(ValueError):
                base_red_local.parse(text, expected, code)

    def test_lookup_uses_exact_sha_without_github(self):
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'events').mkdir()
            (root / 'repos/do-it-v2').mkdir(parents=True)
            (root / 'events/L-base-red-local.jsonl').write_text(json.dumps(dict(
                type='base-red-nightly', project='do-it-v2', sha=sha, complete=True,
                suite='local-hermetic', ts='2026-11-01T00:00:00+00:00', ids=['src/test_bad.py'])) + '\n')
            with patch.object(base_red.fold, 'ROOT', root), \
                    patch.object(base_red, '_project', return_value=(root, sha)), \
                    patch.object(base_red, '_snapshot', side_effect=AssertionError('CI')):
                result = base_red.failing('do-it-v2', sha)
            self.assertTrue(result['complete'])
            self.assertEqual(result['ids']['pytest'], ['src/test_bad.py'])
            with patch.object(base_red.fold, 'ROOT', root), \
                    patch.object(base_red, '_project', return_value=(root, 'b' * 40)), \
                    patch.object(base_red, '_snapshot', return_value=(base_red._result('b' * 40), False)):
                self.assertFalse(base_red.failing('do-it-v2', 'b' * 40)['complete'])


if __name__ == '__main__':
    unittest.main()
