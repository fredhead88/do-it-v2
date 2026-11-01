"""Relative planner cwd and Codex ENOENT use the same absolute spawn path."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ['DOIT_ROOT'] = tempfile.mkdtemp(prefix='cwd-test-import-')
os.environ['DOIT_NO_POKE'] = '1'
import dispatch
import fold
import models
import spawns


class CwdTests(unittest.TestCase):
    def test_root_then_process_resolution(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'root'
            repo = root/'repos/project'
            repo.mkdir(parents=True)
            local = Path(name)/'local'
            local.mkdir()
            before = Path.cwd()
            try:
                os.chdir(local)
                self.assertEqual(spawns.resolve_cwd('repos/project', root), str(repo))
                self.assertEqual(spawns.resolve_cwd('missing', root), str(local/'missing'))
                self.assertEqual(spawns.resolve_cwd(None, root), str(local))
                self.assertEqual(spawns.resolve_cwd(str(repo), root), str(repo))
            finally:
                os.chdir(before)

    def test_cmd_json_and_launch_failure_are_classified(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root/'repos/project'
            repo.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            packet = root/'packet.md'
            packet.write_text('Review this fixture.')
            (root/'seat').mkdir()
            args = argparse.Namespace(role='reviewer', subject='L-spec-9955', packet=str(packet),
                cwd='repos/project', project='project', charter=None, path=None,
                mcp_config=None, timeout=None, max_usd=None, seat=False)
            mapping = {'backend': 'codex', 'model': 'fixture', 'source': 'fixture'}
            result = subprocess.CompletedProcess([], 1, '', 'Error: No such file or directory (os error 2)')
            with patch.multiple(dispatch, ROOT=root, EVENTS=root/'events', CONTENT=root/'content', SEAT=root/'seat'), \
                 patch.multiple(fold, ROOT=root, EVENTS=root/'events'), \
                 patch.object(models, 'resolve', return_value=mapping), \
                 patch.object(dispatch, 'codex_quota', return_value=None), \
                 patch.object(dispatch, 'relay_serving', return_value=False), \
                 patch.object(dispatch, 'run_codex_exec', return_value=result) as backend, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    dispatch.main(args)
                self.assertEqual(stopped.exception.code, 2)
                backend.assert_called_once()
                rows = fold.read_events(use_cache=False)
            self.assertEqual(rows[-1]['type'], 'infra-failure')
            self.assertFalse(any(e.get('kind') == 'failure-unknown' for e in rows))
            cmd, = list((root/'seat').glob('*.cmd.json'))
            metadata = json.loads(cmd.read_text())
            self.assertEqual(metadata['cwd'], str(repo))
            self.assertEqual(metadata['cmd'][metadata['cmd'].index('-C')+1], str(repo))
            self.assertEqual(backend.call_args.args[2], str(repo))


if __name__ == '__main__':
    unittest.main()
