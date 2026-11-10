"""Candidate identity at the real subprocess boundary; local Git and fake CI only."""
import contextlib
import inspect
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
import fold
import window_gate as gate
import window_members
import window_rehearse

CHECKS = [gate.PYTEST_CHECK, gate.PG_CHECK, gate.DASHBOARD_CHECK]
KEYS = ('TREE', 'WINDOW', 'SUITE', 'QUICK_IDS', 'CI_ONLY', 'ONLY_GREEN',
        'DEPLOY_SHA', 'REPO_ROOT', 'CI_CHECKS', 'REQUIRED_CHECKS', 'DISPATCH_TIMEOUT_MIN')


def response(criterion, body):
    raw = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://window-dispatch/' + criterion, status=200,
        body_sha256=hashlib.sha256(raw.encode()).hexdigest(), body_excerpt=body)))


class Identity(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='window-dispatch-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / 'repos/albert-scott'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        self.remote = self.root / 'origin.git'
        self.git('init', '-q', '--bare', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.write('base.txt', 'base\n')
        self.write(gate.PREDEPLOY, 'REQUIRED_CHECKS=(\n  "' + '"\n  "'.join(CHECKS) + '"\n)\n')
        self.write('scripts/ci/actions_changes_gate.sh', "printf '%s\\n' '" + json.dumps(dict(
            checks=CHECKS, everything=True, unclassified=[], reason='fixture')) + "'\n")
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'master')
        self.git('switch', '-qc', 'A')
        self.write('feature.txt', 'feature\n')
        self.commit()
        self.candidate = self.git('rev-parse', 'HEAD')
        self.tree = gate.tree_of(self.repo, self.candidate)
        self.git('switch', '-q', 'master')
        (self.root / 'content').mkdir()
        (self.root / 'content/A.md').write_text('Writes: feature.txt\n')
        self.capture = self.root / 'environments.jsonl'
        fake = self.root / 'fake.sh'
        fake.write_text('exec /usr/bin/python3 ' + str(self.root / 'fake.py') + '\n')
        (self.root / 'fake.py').write_text(
            'import json, os\n'
            f'keys = {KEYS!r}\n'
            f'with open({str(self.capture)!r}, "a") as stream:\n'
            ' stream.write(json.dumps({k: os.environ.get("PREDEPLOY_GATE_" + k) for k in keys}) + "\\n")\n'
            'checks = os.environ["PREDEPLOY_GATE_REQUIRED_CHECKS"].splitlines()\n'
            'print(json.dumps(dict(verdict="green", checks={k: "ran" for k in checks})))\n')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
            DOIT_WINDOW_PREDEPLOY=str(fake), DOIT_WINDOW_RECHAIN='off', DOIT_MAIN='master',
            DOIT_GATE_LEDGER_FILE='L-executor-dispatch.jsonl',
            PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN='7', DOIT_WINDOW_VERIFY_TIMEOUT_SECS='13'))
        for key in ('PREDEPLOY_GATE_TREE', 'PREDEPLOY_GATE_SUITE', 'PREDEPLOY_GATE_QUICK_IDS',
                    'DOIT_LEDGER_FILE', 'DOIT_WINDOW_GATE'):
            os.environ.pop(key, None)
        self.stack.enter_context(patch.object(fold, 'ROOT', self.root))
        self.stack.enter_context(patch.object(fold, 'EVENTS', self.root / 'events'))

    def git(self, *args):
        proc = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=fixture',
            '-c', 'user.email=fixture@localhost', *args], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def rows(self):
        return [json.loads(line) for line in self.capture.read_text().splitlines()] if self.capture.exists() else []

    def verify(self, window='explicit', **kwargs):
        result = gate.verify(self.repo, self.candidate, CHECKS, window, **kwargs)
        self.assertEqual(result['state'], 'green', result)
        row = self.rows()[-1]
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/ci-verify/*'), '')
        return row

    def test_ac1_env_contract(self):
        expected = dict(TREE=self.tree, WINDOW='explicit', SUITE='window', QUICK_IDS='one\ntwo',
            CI_ONLY='1', ONLY_GREEN='1', DEPLOY_SHA=self.candidate, REPO_ROOT=str(self.repo),
            CI_CHECKS='\n'.join(CHECKS), REQUIRED_CHECKS='\n'.join(CHECKS), DISPATCH_TIMEOUT_MIN='7')
        original_popen = subprocess.Popen
        def launch(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            if args[0][0] == 'bash':
                communicate = proc.communicate
                def checked_communicate(*a, **kw):
                    self.assertEqual(kw['timeout'], 13)
                    return communicate(*a, **kw)
                proc.communicate = checked_communicate
            return proc
        with patch.object(gate.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(self.verify(tree=self.tree, quick=('one', 'two')), expected)
        self.assertEqual(self.verify('rehearsal', rehearsal=True, tree=self.tree),
                         dict(expected, WINDOW='rehearsal', QUICK_IDS=''))
        with patch.dict(os.environ, PREDEPLOY_GATE_TREE='inherited', PREDEPLOY_GATE_QUICK_IDS='inherited'):
            self.assertEqual(self.verify('legacy'), dict(expected, TREE='', WINDOW='legacy', QUICK_IDS=''))
        with patch.dict(os.environ, PREDEPLOY_GATE_SUITE='nightly'):
            self.assertEqual(self.verify(tree=self.tree, quick=('one', 'two')), dict(expected, SUITE='nightly'))
        before = len(self.rows())
        for kwargs in ({'quick': ('bad\rline',)}, {'quick': ('bad\nline',)},
                       {'quick': ('bad\x00line',)}, {'quick': ('',)},
                       {'tree': '0' * 40}, {'tree': ''}):
            with self.subTest(kwargs=kwargs), patch.object(gate, 'git', wraps=gate.git) as git:
                result = gate.verify(self.repo, self.candidate, CHECKS, 'invalid', **kwargs)
                self.assertEqual(result['state'], 'undetermined')
                self.assertTrue(result['why'])
                self.assertFalse(any(call.args[1] == 'push' for call in git.call_args_list))
        with patch.dict(os.environ, PREDEPLOY_GATE_SUITE='invalid'), \
             patch.object(gate, 'git', wraps=gate.git) as git:
            self.assertEqual(gate.verify(self.repo, self.candidate, CHECKS, 'invalid')['state'], 'undetermined')
            git.assert_not_called()
        with patch.object(gate, 'git', wraps=gate.git) as git:
            self.assertEqual(gate.verify(self.repo, 'unresolvable', CHECKS, 'invalid', tree=self.tree)['state'],
                             'undetermined')
            self.assertFalse(any(call.args[1] == 'push' for call in git.call_args_list))
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/ci-verify/*'), '')
        response('AC1', dict(environments=self.rows(), invalid_launches=len(self.rows()) - before))

    def test_ac2_managed_callers(self):
        # Production judge creates and verifies its own candidate; only baseline metadata is local.
        with patch.object(gate, 'base_red_snapshot', return_value={'complete': True}), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = gate._run_attempt('identity-judge', 'albert-scott', ['A:A'], 'master', False,
                                   self.base, self.base, None, 1, None)
        self.assertEqual(rc, 0)
        rows = self.rows()
        self.assertEqual(rows[0]['TREE'], '')  # base_pg before the managed block
        self.assertEqual(rows[-1]['TREE'], gate.tree_of(self.repo, rows[-1]['DEPLOY_SHA']))
        # Remove the judge's receipt so the real rehearsal must dispatch as well.
        for ledger in (self.root / 'events').glob('*.jsonl'):
            ledger.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = window_rehearse._run('identity-rehearsal', 'albert-scott', ['A:A'], self.base, 'master')
        self.assertEqual(rc, 0)
        row = self.rows()[-1]
        self.assertEqual(row['WINDOW'], 'identity-rehearsal')
        self.assertEqual(row['TREE'], gate.tree_of(self.repo, row['DEPLOY_SHA']))
        # An undetermined root baseline may inspect one ancestor; both stay legacy.
        original_parse = gate.parse_verdict
        parses = []
        def parse(out):
            parses.append(out)
            return ({}, [], '') if len(parses) == 1 else original_parse(out)
        start = len(self.rows())
        with patch.object(gate, 'parse_verdict', side_effect=parse), \
             patch.object(gate, 'scope', return_value={'checks': [gate.PYTEST_CHECK]}):
            result, _ = gate.base_pg(self.repo, self.candidate, CHECKS, 'identity-baseline', [])
        self.assertEqual(result['state'], 'green')
        self.assertEqual([r['TREE'] for r in self.rows()[start:]], ['', ''])
        kind, result = window_members._verify(gate, self.repo, self.candidate, CHECKS, 'identity-member')
        self.assertEqual((kind, result['state']), ('ok', 'green'))
        self.assertEqual(self.rows()[-1]['TREE'], '')
        import window_dispatch
        with self.assertRaisesRegex(RuntimeError, 'restore'):
            with window_dispatch.managed(self.tree, ('outer',)):
                self.assertEqual(self.verify()['QUICK_IDS'], 'outer')
                with window_dispatch.managed(gate.tree_of(self.repo, self.base)):
                    self.assertEqual(self.verify(tree=self.tree)['TREE'], self.tree)
                raise RuntimeError('restore')
        self.assertEqual(self.verify('later-legacy')['TREE'], '')
        self.assertEqual(self.rows()[-1]['QUICK_IDS'], '')
        response('AC2', dict(environments=self.rows()))

    def test_ac4_signature(self):
        self.assertEqual(str(inspect.signature(gate.verify)),
            '(repo, cand, required, window, *, rehearsal=False, tree=None, quick=())')
        count = len(Path(gate.__file__).read_text().splitlines())
        self.assertLessEqual(count, 2099)
        response('AC4', dict(signature=str(inspect.signature(gate.verify)), window_gate_lines=count))


if __name__ == '__main__':
    unittest.main()
