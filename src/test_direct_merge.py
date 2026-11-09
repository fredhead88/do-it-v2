#!/usr/bin/env python3
"""Scratch-git and real-ledger regressions for the one-spec merge routine."""
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import direct_merge
import dispatch
import fold


def receipt(case, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url=f'cli://direct-merge/{case}', status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)))


class DirectMergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'merge.gpgsign', 'false')
        self.write('shared', 'base\n')
        self.write('unrelated', 'base\n')
        self.commit('base')
        self.git('switch', '-c', 'feature')
        self.write('shared', 'feature\n')
        self.commit('branch tip subject\n\nBody excluded from merge subject.')
        self.git('switch', 'main')
        self.spec = 'L-spec-9001'
        self.row = dict(spec=self.spec, project='do-it-v2', branch='feature')
        self.emit = Mock()
        self.gate = Mock(return_value=(0, ''))

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), *args],
                              capture_output=True, check=check)

    def write(self, name, body):
        (self.repo / name).write_text(body)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)

    def merge(self, **kwargs):
        return direct_merge.merge_one(
            self.spec, self.row, repo=self.repo, gate=self.gate,
            run=subprocess.run, emit=kwargs.get('emit', self.emit), now=lambda: 'fixture')

    def assert_no_merge(self):
        self.assertEqual(self.git('log', '--merges', '--oneline').stdout, b'')
        self.emit.assert_not_called()

    def test_ac1_shipped_real_ledger_round_trip(self):
        events = self.root / 'ledger' / 'events'
        events.mkdir(parents=True)
        dst = events / 'L-tick-9001.jsonl'

        def emit(kind, spec, **fields):
            self.assertIsNone(dispatch.emit(dst, {'subject': spec}, kind,
                                            strict=True, **fields))

        result = self.merge(emit=emit)
        self.assertEqual(result['status'], 'shipped')
        self.assertEqual(result['sha'], self.git('rev-parse', 'HEAD').stdout.decode().strip())
        self.assertEqual(self.git('log', '--merges', '-1', '--format=%s').stdout.decode().strip(),
                         'merge(L-spec-9001): branch tip subject')
        self.assertEqual(len(self.git('show', '-s', '--format=%P', 'HEAD').stdout.split()), 2)
        with patch.object(fold, 'ROOT', events.parent), patch.object(fold, 'EVENTS', events):
            fresh = fold.read_events(use_cache=False)
        self.assertEqual(len(fresh), 1)
        self.assertEqual({key: fresh[0][key] for key in
                          ('type', 'subject', 'branch', 'project', 'sha', 'actor')},
                         dict(type='shipped', subject=self.spec, branch='feature',
                              project='do-it-v2', sha=result['sha'], actor='tick'))
        self.gate.assert_called_once_with('feature', 'main', self.spec)
        receipt('AC1', dict(status=result['status'], merge_parents=2,
                            subject='merge(L-spec-9001): branch tip subject',
                            branch=fresh[0]['branch'], project=fresh[0]['project'],
                            actor=fresh[0]['actor'], sha_matches_head=fresh[0]['sha'] == result['sha']))

    def test_ac2_main_moved_during_gate(self):
        def advance(*args):
            self.git('commit', '--allow-empty', '-m', 'advance main')
            return 0, ''
        self.gate.side_effect = advance
        self.assertEqual(self.merge()['status'], 'regate')
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), b'advance main')
        self.assert_no_merge()
        receipt('AC2-main-moved', dict(status='regate', main_subject='advance main', emits=0, merges=0))

    def test_branch_moved_during_gate(self):
        def advance(*args):
            tree = self.git('rev-parse', 'feature^{tree}').stdout.decode().strip()
            new = self.git('commit-tree', tree, '-p', 'feature', '-m', 'advance branch')
            self.git('update-ref', 'refs/heads/feature', new.stdout.decode().strip())
            return 0, ''
        self.gate.side_effect = advance
        self.assertEqual(self.merge()['status'], 'regate')
        self.assert_no_merge()

    def test_ac2_gate_red(self):
        self.gate.return_value = 1, 'rework'
        result = self.merge()
        self.assertEqual(result, dict(status='gate-red', sha=None, detail='rework'))
        self.assert_no_merge()
        receipt('AC2-gate-red', dict(result, emits=0, merges=0))

    def test_branch_deleted_during_gate(self):
        def delete(*args):
            self.git('branch', '-D', 'feature')
            return 0, ''
        self.gate.side_effect = delete
        self.assertEqual(self.merge()['status'], 'regate')
        self.assert_no_merge()

    def test_wrong_integrator_branch_refused(self):
        self.git('switch', 'feature')
        self.assertEqual(self.merge()['status'], 'regate')
        self.gate.assert_not_called()
        self.assert_no_merge()

    def test_subject_is_first_message_line(self):
        self.git('switch', 'feature')
        self.git('commit', '--allow-empty', '-m', 'First line\nSecond line in same paragraph')
        self.git('switch', 'main')
        self.assertEqual(self.merge()['status'], 'shipped')
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(),
                         b'merge(L-spec-9001): First line')

    def test_ac3_dirty_overlap_before_gate(self):
        self.write('shared', 'uncommitted\n')
        result = self.merge()
        self.assertEqual(result['status'], 'refused-dirty')
        self.assertIn('shared', result['detail'])
        self.gate.assert_not_called()
        self.assert_no_merge()
        receipt('AC3-dirty', dict(result, gate_calls=0, emits=0, merges=0))

    def test_ac3_unrelated_dirty_merges(self):
        self.write('unrelated', 'uncommitted\n')
        self.assertEqual(self.merge()['status'], 'shipped')
        self.assertEqual((self.repo / 'unrelated').read_text(), 'uncommitted\n')
        self.emit.assert_called_once()
        receipt('AC3-unrelated-dirty', dict(status='shipped', unrelated_dirty_preserved=True, emits=1))

    def test_ac3_conflict_aborted(self):
        self.write('shared', 'main conflict\n')
        self.commit('conflicting main')
        old = self.git('rev-parse', 'HEAD').stdout
        self.assertEqual(self.merge()['status'], 'merge-failed')
        self.assertNotEqual(self.git('rev-parse', '--verify', 'MERGE_HEAD', check=False).returncode, 0)
        self.assertEqual(self.git('status', '--porcelain').stdout, b'')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, old)
        self.assert_no_merge()
        receipt('AC3-conflict', dict(status='merge-failed', merge_in_progress=False,
                                    clean_status=True, head_unchanged=True, emits=0))

    def test_dirty_rename_source_and_space_paths(self):
        self.git('mv', 'shared', 'renamed file')
        self.assertEqual(self.merge()['status'], 'refused-dirty')
        self.gate.assert_not_called()

    def test_ac4_shipped_authorization(self):
        self.assertEqual(fold.EMITS['shipped'], {'executor', 'tick'})
        for actor in ('executor', 'tick'):
            self.assertIsNone(fold.admits({'type': 'shipped', 'subject': self.spec}, actor))
        self.assertIsNotNone(fold.admits({'type': 'shipped', 'subject': self.spec}, 'builder'))
        receipt('AC4', dict(shipped_actors=sorted(fold.EMITS['shipped']), builder_refused=True))

    def test_invalid_identifiers_refused_without_git(self):
        for spec, branch in (('bad', 'feature'), (self.spec, '--help'),
                             (self.spec, 'feature;echo bad')):
            with self.subTest(spec=spec, branch=branch):
                runner = Mock(side_effect=AssertionError('must validate before git'))
                result = direct_merge.merge_one(
                    spec, dict(self.row, spec=spec, branch=branch), repo=self.repo,
                    gate=self.gate, run=runner, emit=self.emit, now=lambda: 'fixture')
                self.assertEqual(result['status'], 'gate-red')
        self.gate.assert_not_called()
        self.assert_no_merge()

    def test_unexpected_git_error_before_merge_raises(self):
        self.row['branch'] = 'absent'
        with self.assertRaises(direct_merge.merge_gate.Undetermined):
            self.merge()
        self.assert_no_merge()

    def test_cli_gate_argv_and_output(self):
        completed = subprocess.CompletedProcess([], 1, b'gate output\n', b'gate error\n')
        with patch.object(direct_merge.subprocess, 'run', return_value=completed) as runner:
            code, output = direct_merge.cli_gate(self.repo)('feature', 'main', self.spec)
        self.assertEqual(code, 1)
        self.assertEqual(output, 'gate output\ngate error\n')
        self.assertEqual(runner.call_args.args[0],
                         [direct_merge.DOIT, 'gate', 'feature', 'main', '--spec', self.spec])
        self.assertEqual(runner.call_args.kwargs['cwd'], self.repo)
        self.assertNotIn('shell', runner.call_args.kwargs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
