#!/usr/bin/env python3
"""Hermetic prequeue contract tests: real merges and executor ledger appends."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prequeue
import fold
import window_gate

REPO = Path(__file__).resolve().parent.parent
SPEC = 'L-spec-2352'
GREEN = dict(verdict='green', scope='selected', checks={'pytest': 'pass'},
             failing=[], wall_s=0.01, why='')


def git(repo, *args):
    proc = subprocess.run(['git', '-c', 'user.name=fixture', '-c',
                           'user.email=fixture@localhost', '-C', str(repo), *args],
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def signed(ac, body):
    encoded = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(ac=ac, url='scratch://prequeue/' + ac, status=0,
                          body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                          body_excerpt=encoded)))


class PrequeueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='prequeue-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repos/albert-scott'
        self.repo.parent.mkdir(parents=True)
        git(self.root, 'init', '--bare', '-q', str(self.root / 'origin.git'))
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(self.repo))
        git(self.repo, 'checkout', '-qb', 'master')
        (self.repo / 'base.txt').write_text('base\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.report = self.root / 'observed.json'
        self.nodes = self.root / 'provisioned-node-modules'
        self.nodes.mkdir()
        self.wrapper = self.root / 'python-override'
        self.wrapper.write_text('#!/bin/sh\nprintf "%s" "$0" > "$PREQUEUE_ARGV"\n'
                                'exec /usr/bin/python3 "$@"\n')
        self.wrapper.chmod(0o700)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(fold, 'ROOT', self.root))
        stack.enter_context(patch.object(fold, 'EVENTS', self.root / 'events'))
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root),
                            DOIT_NO_POKE='1', DOIT_PREQUEUE_PYTHON=str(self.wrapper),
                            PREQUEUE_OBSERVED=str(self.report),
                            PREQUEUE_ARGV=str(self.root / 'argv'),
                            DOIT_GATE_LEDGER_FILE='L-executor-0001.jsonl'))
        stack.enter_context(patch.object(prequeue.os, 'getloadavg', return_value=(0, 0, 0)))
        stack.enter_context(patch.object(prequeue, 'NODE_MODULES', self.nodes))
        stack.enter_context(patch.object(prequeue.base_red, 'known_red', return_value={}))
        stack.enter_context(patch.object(prequeue, 'settings',
                            return_value=dict(enforce=False, wall_cap_s=5, load_max=15)))

    def branch(self, result=GREEN, body=None, conflict=False, missing=False):
        git(self.repo, 'checkout', '-qB', 'candidate', self.base)
        if missing:
            (self.repo / 'candidate.txt').write_text('candidate')
        else:
            script = self.repo / 'scripts/ci/prequeue_check.py'
            script.parent.mkdir(parents=True)
            script.write_text('import json, os, pathlib, subprocess, sys, time\n'
                'def git(*args):\n'
                '    return subprocess.check_output(["git", *args], text=True).strip()\n'
                'nodes = pathlib.Path("dashboard/node_modules")\n'
                'pathlib.Path(os.environ["PREQUEUE_OBSERVED"]).write_text(json.dumps(dict(\n'
                '    second=git("rev-parse", "HEAD^2"),\n'
                '    parents=git("show", "-s", "--format=%P", "HEAD").split(),\n'
                '    detached=git("rev-parse", "--abbrev-ref", "HEAD"),\n'
                '    author=git("show", "-s", "--format=%cn <%ce>", "HEAD"),\n'
                '    args=sys.argv[1:], secret_present="ANTHROPIC_API_KEY" in os.environ,\n'
                '    symlink=nodes.is_symlink(), target=str(nodes.resolve()))))\n'
                + (body if body is not None else 'print("progress")\nprint(' + repr(json.dumps(result)) + ')\n'
                   + 'sys.exit(' + str({'green': 0, 'red': 1, 'undetermined': 2}[result['verdict']]) + ')\n'))
        if conflict:
            (self.repo / 'base.txt').write_text('candidate conflict\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'candidate')
        self.sha = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', 'master')
        (self.repo / 'master.txt').write_text('master advanced\n')
        if conflict:
            (self.repo / 'base.txt').write_text('master conflict\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'master')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        # Real resolver selects the newest clean branch, not a stale entry.
        with patch.object(window_gate, 'train_events', return_value=[]):
            window_gate.ev('merge-gate-clean', SPEC, project='albert-scott', branch='absent')
            window_gate.ev('merge-gate-clean', SPEC, project='albert-scott', branch='candidate')
        self.before = git(self.repo, 'for-each-ref')

    def run_check(self, expected, why=None):
        before = git(self.repo, 'for-each-ref')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = prequeue.main([SPEC, '--project', 'albert-scott'])
        self.assertEqual(code, expected, output.getvalue())
        rows = [e for e in fold.read_events() if e['type'].startswith('prequeue-')]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['type'], {0: 'prequeue-green', 1: 'prequeue-red',
                                      2: 'prequeue-undetermined'}[expected])
        self.assertEqual(row['spec'], SPEC)
        self.assertEqual(row['project'], 'albert-scott')
        self.assertEqual(row['branch_sha'], getattr(self, 'sha', 'unknown'))
        if why:
            self.assertEqual(row['why'], why)
        self.assertEqual(before, git(self.repo, 'for-each-ref'))
        self.assertEqual(len(git(self.repo, 'worktree', 'list', '--porcelain').split('worktree ')), 2)
        scratch = self.root / 'scratch/prequeue'
        self.assertFalse(scratch.exists() and list(scratch.iterdir()))
        self.assertFalse((self.repo / '.git/MERGE_HEAD').exists())
        return row, output.getvalue()

    def test_ac1_real_merge_and_cleanup(self):
        self.branch()
        row, _ = self.run_check(0)
        seen = json.loads(self.report.read_text())
        self.assertEqual(seen['second'], self.sha)
        self.assertEqual(seen['parents'], [self.base, self.sha])
        self.assertEqual(seen['detached'], 'HEAD')
        self.assertEqual(seen['author'], 'doit-prequeue <doit-prequeue@localhost>')
        self.assertEqual(row['base_sha'], self.base)
        self.assertEqual(row['scope'], 'selected')
        self.assertEqual(row['wall_s'], 0.01)
        signed('AC1', dict(exit=0, event='prequeue-green', parents=2, detached=True,
                           refs_unchanged=True, scratch_removed=True))

    def test_ac2_red_and_known_red(self):
        cases = [(['a', 'b'], {}, 1, ['a', 'b']),
                 (['a', 'b'], {'a': {}, 'b': {}}, 0, []),
                 (['a', 'b'], {'a': {}}, 1, ['b']), ([], {}, 1, [])]
        for failing, known, code, left in cases:
            with self.subTest(failing=failing, known=known):
                # Separate scratch fixture and ledger for each verdict.
                case = PrequeueTests()
                case.setUp()
                try:
                    case.branch(dict(GREEN, verdict='red', failing=failing, checks={'pytest': 'fail'}))
                    with patch.object(prequeue.base_red, 'known_red', return_value=known):
                        row, output = case.run_check(code)
                    if code == 0:
                        self.assertEqual(row['scope'], 'partial')
                    else:
                        self.assertEqual(row['failing'], left)
                        self.assertEqual(row['checks'], {'pytest': 'fail'})
                    summary = json.loads(output.splitlines()[-1])
                    self.assertEqual(summary['excluded_count'], len(failing) - len(left))
                    if known:
                        self.assertEqual(summary['excluded_reason'], 'known-red-on-master')
                finally:
                    case.doCleanups()
        signed('AC2', dict(cases=4, red=True, all_known_partial=True,
                           leftovers_only=True, empty_stays_red=True))

    def test_ac3_undetermined_paths(self):
        for why in ('conflict', 'no-script', 'bad-output', 'timeout', 'load', 'no-branch'):
            with self.subTest(why=why):
                case = PrequeueTests()
                case.setUp()
                try:
                    if why != 'no-branch':
                        case.branch(conflict=why == 'conflict', missing=why == 'no-script',
                                    body={'bad-output': '', 'timeout': 'time.sleep(10)\n'}.get(why))
                    with contextlib.ExitStack() as stack:
                        if why == 'load':
                            stack.enter_context(patch.object(prequeue.os, 'getloadavg', return_value=(99, 0, 0)))
                            create = stack.enter_context(patch.object(prequeue, 'git', wraps=prequeue.git))
                        if why == 'timeout':
                            stack.enter_context(patch.object(prequeue, 'settings', return_value=dict(
                                enforce=False, wall_cap_s=0.1, load_max=15)))
                        case.run_check(2, why)
                        if why == 'load':
                            self.assertFalse(any('add' in c.args for c in create.call_args_list))
                finally:
                    case.doCleanups()
        signed('AC3', dict(cases=6, exit=2, no_scratch_or_merge_state=True,
                           load_creates_no_worktree=True))

    def test_ac4_environment_and_arguments(self):
        self.branch()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-fixture'}):
            self.run_check(0)
        seen = json.loads(self.report.read_text())
        self.assertFalse(seen['secret_present'])
        self.assertTrue(seen['symlink'])
        self.assertEqual(os.path.realpath(seen['target']), os.path.realpath(str(self.nodes)))
        self.assertEqual((self.root / 'argv').read_text(), str(self.wrapper))
        self.assertEqual(seen['args'], ['--base', self.base, '--json', '--wall-cap', '5'])
        signed('AC4-environment', dict(override=True, credential_absent=True,
                                       symlink=True, arguments=True))

    def test_exception_and_invalid_output_cleanup(self):
        for body in ('print("not-json")\n', 'print("[]")\n',
                     'print(\'{"verdict":"green"}\')\n',
                     'print(\'{"verdict":"green","scope":"selected","wall_s":0}\')\nsys.exit(1)\n'):
            with self.subTest(body=body):
                case = PrequeueTests()
                case.setUp()
                try:
                    case.branch(body=body)
                    case.run_check(2, 'bad-output')
                finally:
                    case.doCleanups()
        self.branch()
        with patch.object(prequeue, 'run_child', side_effect=OSError('fixture')):
            self.run_check(2, 'execution')


class SettingsTests(unittest.TestCase):
    def test_ac4_defaults_and_validation(self):
        defaults = dict(enforce=False, wall_cap_s=900, load_max=15)
        self.assertEqual(prequeue.settings(), defaults)
        with tempfile.TemporaryDirectory(prefix='prequeue-settings-') as temp:
            config = Path(temp) / 'look.toml'
            with patch.object(prequeue, 'CONFIG', config):
                self.assertEqual(prequeue.settings(), defaults)
                for text in ('[[[', '[other]\n', 'prequeue="invalid"',
                             '[prequeue]\nwall_cap_s=-1\n',
                             '[prequeue]\nenforce="false"\n',
                             '[prequeue]\nload_max=nan\n'):
                    config.write_text(text)
                    self.assertEqual(prequeue.settings(), defaults)
                config.write_text('[prequeue]\nenforce=true\nwall_cap_s=20\nload_max=3\n')
                self.assertEqual(prequeue.settings(), dict(enforce=True, wall_cap_s=20, load_max=3))
        signed('AC4-settings', defaults)

    def test_ac5_dispatch_and_documentation(self):
        self.assertIn('prequeue) shift; exec python3 "$SRC/prequeue.py" "$@" ;;',
                      (REPO / 'doit').read_text())
        proc = subprocess.run([str(REPO / 'doit'), 'prequeue', '--help'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        signed('AC5', dict(dispatch=True, help_exit=0))


if __name__ == '__main__':
    unittest.main()
