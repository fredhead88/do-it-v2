"""Receipt-backed merge-gate regressions, using disposable real Git histories."""
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

# Reuse the resolver owner's migration fixture rather than a competing model.
import test_rechain as resolver_tests
from test_rechain import Fixture, git
import merge_gate as mg
import rechain

SOURCE = Path(__file__).resolve().parent
SPEC = 'L-spec-9001'


class GateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gate-rechain-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = Fixture(self.root)
        self.fx.write(rechain.TEST, resolver_tests.RechainTests.ISOLATION)
        self.fx.write('tests/test_pins.py', 'HISTORY = "h0"\n')
        self.fx.write('data.txt', 'original\n')
        self.fx.write('type.txt', 'regular file\n')
        self.fx.base = self.fx.commit('gate fixture base')
        self.a = self.fx.branch('a')
        def owned(f):
            f.write(rechain.TEST, resolver_tests.RechainTests.HANDWRITTEN)
            f.write('tests/test_pins.py', 'HISTORY = "h0"\nPARENT = "h0"\nAGAIN = "h0"\nREVISION = "b"\n')
        self.b = self.fx.branch('b', mutation=owned)
        self.repo = self.fx.repo
        self.main = self.a[1]
        self.branch = self.b[1]
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.ledger = self.root / 'ledger'
        self.stack.enter_context(patch.object(mg.fold, 'ROOT', self.ledger))
        self.stack.enter_context(patch.object(mg.fold, 'EVENTS', self.ledger / 'events'))
        self.stack.enter_context(patch.object(mg.freeze, 'state', return_value=None))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.ledger),
            DOIT_SCRATCH=str(self.root / 'scratch'), DOIT_NO_POKE='1',
            DOIT_GATE_LEDGER_FILE='L-executor-fixture.jsonl'))
        self.stack.enter_context(patch.object(mg, 'ENV', dict(os.environ, LC_ALL='C')))
        (self.ledger / 'content').mkdir(parents=True)
        self.grant(rechain.VERSIONS + '*.py', rechain.MANIFEST, rechain.TEST,
                   rechain.AUTHORITY, rechain.FINGERPRINT, 'tests/test_pins.py')
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

    def grant(self, *paths):
        (self.ledger / 'content' / (SPEC + '.md')).write_text('Writes: ' + ', '.join(paths) + '\n')

    def invoke(self, expected=0):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mg.main_([self.branch, self.main, '--spec', SPEC])
        self.assertEqual(rc, expected)
        rows = mg.fold.read_events(use_cache=False)
        self.assertFalse(any(e['type'] in ('rechained', 'rechain-declined', 'shipped') for e in rows))
        row = rows[-1]
        body = dict(fixture=self.id(), inputs=[self.a[2], self.b[2]], verdict=row)
        encoded = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url='cli://merge-gate/' + self.id(), status=rc,
                             body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))
        return row

    def fresh(self):
        env = dict(os.environ, DOIT_ROOT=str(self.ledger), PYTHONPATH=str(SOURCE))
        return json.loads(subprocess.check_output([sys.executable, '-c',
            'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'], env=env))

    def edit(self, ref, changes):
        git(self.repo, 'checkout', ref)
        for path, value in changes.items():
            if value is None:
                (self.repo / path).unlink()
            elif isinstance(value, tuple):
                (self.repo / path).unlink()
                (self.repo / path).symlink_to(value[1])
            else:
                self.fx.write(path, value)
        sha = self.fx.commit('fixture edit')
        git(self.repo, 'checkout', 'master')
        if ref == self.branch:
            self.b = (self.b[0], ref, sha)
        elif ref == self.main:
            self.a = (self.a[0], ref, sha)
        return sha

    def hold(self, reason):
        with patch.object(mg, 'conflict_event', side_effect=AssertionError('builder path')), \
             patch.object(mg, 'secret_scan', side_effect=AssertionError('scanner after uncertainty')):
            row = self.invoke(2)
        self.assertEqual(row['type'], 'merge-gate-undetermined')
        self.assertEqual(row['reason'], reason)
        self.assertFalse(any('rework' in e['type'] for e in self.fresh()))
        return row


class DryRunTests(GateFixture):
    def test_real_receipt_and_persisted_verdict(self):
        receipts = []
        commit, why = rechain.resolve(self.repo, self.a[2], (SPEC, self.branch, self.b[2]),
            'gate-' + SPEC, ev=lambda t, s, **kw: receipts.append(dict(type=t, subject=s, **kw)))
        self.assertIsNone(why)
        row = self.invoke()
        self.assertEqual(row['type'], 'merge-gate-clean')
        self.assertTrue(row['rechain_dry_run'])
        tree = git(self.repo, 'rev-parse', commit + '^{tree}')
        self.assertEqual(row['merged_tree'], tree[:12])
        self.assertEqual(row['rechain'], {'old_parent': 'h0', 'new_parent': 'a'})
        self.assertEqual(row['main_sha'], self.a[2][:12])
        self.assertEqual(row['branch_sha'], self.b[2][:12])
        self.assertEqual(self.fresh()[-1], row)
        self.assertNotIn('rework', json.dumps(self.fresh()))
        print(json.dumps(dict(resolved_tree=tree, receipt=receipts[-1])))

    def test_receipt_identity_and_conflict_proof_are_required(self):
        original = mg._resolver_call
        def invalid(kind):
            def call(*args):
                result = original(*args)
                if kind == 'absent':
                    result['receipts'] = []
                elif kind == 'proof':
                    result['proofs'][0][0] = 0
                elif kind == 'parent':
                    result['receipts'][0]['old_parent'] = ''
                else:
                    result['receipts'][0][kind] = 'wrong'
                return result
            return call
        for kind in ('absent', 'proof', 'parent', 'subject', 'branch', 'branch_sha', 'merge_sha'):
            with self.subTest(kind=kind), patch.object(mg, '_resolver_call', side_effect=invalid(kind)):
                self.hold('conflict-diagnostic')

    def test_synthetic_conflict_is_not_proof(self):
        with patch.object(mg, 'gate', side_effect=mg.Undetermined('synthetic', reason='merge-conflict')), \
             patch.object(mg, '_resolver_call', side_effect=AssertionError('unproven resolver')):
            self.hold('conflict-diagnostic')

    def test_moved_input_holds(self):
        original = mg._resolver_call
        def moved(*args):
            result = original(*args)
            git(self.repo, 'update-ref', 'refs/heads/' + self.branch, self.fx.base)
            return result
        with patch.object(mg, '_resolver_call', side_effect=moved):
            row = self.hold('conflict-diagnostic')
        self.assertEqual(row['branch_sha'], self.b[2])


class TreeChecksTests(GateFixture):
    def test_ungranted_removal_and_granted_migration_removal(self):
        self.edit(self.branch, {'data.txt': None})
        row = self.invoke(1)
        self.assertTrue(any(p.startswith('data.txt@') for p in row['removed']))
        self.edit(self.branch, {rechain.VERSIONS + 'h0.py': None})
        row = self.invoke(1)
        self.assertTrue(any(p.startswith(rechain.VERSIONS + 'h0.py@') for p in row['removed']))

    def test_append_only_valid_invalid_and_revert(self):
        listing = 'data.txt\n'
        self.edit(self.main, {mg.APPEND_LIST: listing})
        self.edit(self.branch, {'data.txt': 'original\nadded\n'})
        row = self.invoke()
        self.assertTrue(row['append_only'][0]['ok'])
        self.edit(self.branch, {'data.txt': 'replacement\n'})
        row = self.invoke()
        self.assertFalse(row['append_only'][0]['ok'])
        # Revert to a recent main blob that predates the branch's base.
        git(self.repo, 'checkout', 'master')
        self.fx.write('data.txt', 'historical\n')
        historical = self.fx.commit('historical data')
        self.fx.write('data.txt', 'original\n')
        current = self.fx.commit('restore original')
        git(self.repo, 'merge', '--no-edit', self.main)
        git(self.repo, 'branch', '-f', self.main, 'HEAD')
        self.a = (*self.a[:2], git(self.repo, 'rev-parse', self.main))
        self.edit(self.branch, {'data.txt': 'historical\n'})
        row = self.invoke(1)
        self.assertIn('data.txt', row['reverted'])
        self.assertFalse(row['append_only'][0]['ok'])
        self.assertNotEqual(historical, current)

    def test_scanner_sees_resolved_bytes_and_uses_trusted_script(self):
        # Only a resolved tree contains down_revision='a' in b.py, proving the
        # real scanner checkout rather than mocked argument agreement.
        script = """#!/bin/bash
if grep -q "down_revision = 'a'" api/alembic_supabase/versions/b.py; then
  echo '  api/alembic_supabase/versions/b.py:2 -- SyntheticParent'
  exit 1
fi
exit 0
"""
        self.edit(self.main, {mg.DETECT_SECRETS_SCRIPT: script})
        row = self.invoke(1)
        self.assertEqual(row['reason'], 'secret-scan')
        self.assertIn('SyntheticParent', row['findings'][0])
        self.assertEqual(row['branch_sha'], self.b[2])
        self.assertEqual(git(self.repo, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_scanner_external_timeout_and_setup_error_hold(self):
        for outcome, reason in [(('undetermined', ['timeout', 'budget=60.0', 'scan-exhausted=false']), 'timeout'),
                                (('undetermined', ['worktree']), 'worktree')]:
            with self.subTest(reason=reason), patch.object(mg, 'secret_scan', return_value=outcome):
                row = self.invoke(2)
                self.assertEqual(row['reason'], reason)
                self.assertEqual(row['type'], 'merge-gate-undetermined')


class DeclineTests(GateFixture):
    def test_real_rules_declines_preserve_attempts_refresh_and_rule(self):
        import branch_refresh
        import conflict
        original_conflicted, original_attempts = conflict.conflicted, conflict.conflict_attempts
        order = []
        def diagnosed(*args):
            order.append('conflicted')
            return original_conflicted(*args)
        def counted(*args):
            order.append('attempts')
            return original_attempts(*args)
        def refresh(*args):
            order.append('refresh')
            return None
        self.edit(self.branch, {rechain.VERSIONS + 'extra.py': "revision='extra'\ndown_revision='b'\n"})
        with patch.object(conflict, 'conflicted', side_effect=diagnosed), \
             patch.object(conflict, 'conflict_attempts', side_effect=counted), \
             patch.object(branch_refresh, 'gate_refresh', side_effect=refresh):
            row = self.invoke(1)
        self.assertEqual(row['type'], 'conflict-rework')
        self.assertEqual(row['rule'], 'branch must add exactly one active migration')
        self.assertEqual(order, ['conflicted', 'attempts', 'refresh'])
        self.assertEqual(self.fresh()[-1], row)
        self.assertIn('attempt', row)
        with patch.object(branch_refresh, 'gate_refresh', return_value=0) as refresh:
            before = len(self.fresh())
            self.assertEqual(mg.main_([self.branch, self.main, '--spec', SPEC]), 0)
            self.assertEqual(len(self.fresh()), before)
            refresh.assert_called_once()

    def test_bookkeeping_edit_declines(self):
        self.edit(self.branch, {rechain.TEST: resolver_tests.RechainTests.HANDWRITTEN + '\ndef helper():\n    return True\n'})
        import branch_refresh
        with patch.object(branch_refresh, 'gate_refresh', return_value=None):
            row = self.invoke(1)
        self.assertEqual(row['rule'], 'unrecognised isolation-test branch change')

    def test_ordinary_content_conflict_no_invented_rule(self):
        # A branch with no migration addition, conflicting with the real main.
        git(self.repo, 'branch', '-f', 'ordinary', self.fx.base)
        self.edit(self.main, {'data.txt': 'main edit\n'})
        self.edit('ordinary', {'data.txt': 'branch edit\n'})
        self.branch = 'ordinary'
        import branch_refresh
        with patch.object(branch_refresh, 'gate_refresh', return_value=None):
            row = self.invoke(1)
        self.assertEqual(row['type'], 'conflict-rework')
        self.assertNotIn('rule', row)

    def test_tooling_provenance_table(self):
        original = mg._resolver_call
        valid = None
        def tooling(rule):
            def call(*args):
                nonlocal valid
                valid = valid or original(*args)
                row = dict(type='rechain-declined', subject=SPEC, window='gate-' + SPEC,
                           branch=self.branch, rule=rule)
                return dict(valid, commit=None, reason='re-chain rules could not be applied safely',
                            receipts=[] if rule is None else [row],
                            proofs=[] if rule == 'merge-tree failed' else valid['proofs'])
            return call
        for rule in ('git object operation failed', 'merge-tree failed', 'TimeoutExpired',
                     'CalledProcessError', 'SubprocessError', 'KeyError', 'TypeError',
                     'SyntaxError', 'UnicodeDecodeError', None, 'unknown rule'):
            with self.subTest(rule=rule), patch.object(mg, '_resolver_call', side_effect=tooling(rule)):
                self.hold('timeout' if rule == 'TimeoutExpired' else 'gate-tooling')
        for exc in (OSError(), UnicodeDecodeError('utf8', b'x', 0, 1, 'injected'),
                    subprocess.TimeoutExpired('git', 1), subprocess.CalledProcessError(1, 'git')):
            with self.subTest(error=type(exc).__name__), patch.object(mg, '_resolver_call', side_effect=exc):
                self.hold('timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'gate-tooling')

    def test_deadline_before_and_after_acceptance_and_decline(self):
        original = mg._resolver_call
        real_clock = mg.time.monotonic
        for phase in ('before', 'accepted', 'declined'):
            expired = [False]
            def clock():
                return mg._START + mg.DEADLINE + 1 if expired[0] else real_clock()
            def call(*args):
                result = original(*args)
                if phase == 'declined':
                    result.update(commit=None, reason='re-chain rules could not be applied safely', receipts=[
                        dict(type='rechain-declined', subject=SPEC, window='gate-' + SPEC,
                             branch=self.branch, rule='branch must add exactly one active migration')])
                expired[0] = True
                return result
            original_run = mg.run
            def proof(args, stdin=None):
                result = original_run(args, stdin)
                if phase == 'before' and args[:2] == ('merge-tree', '--write-tree'):
                    expired[0] = True
                return result
            with self.subTest(phase=phase), patch.object(mg.time, 'monotonic', side_effect=clock), \
                 patch.object(mg, 'run', side_effect=proof), patch.object(mg, '_resolver_call', side_effect=call) as resolver:
                self.hold('deadline')
                self.assertEqual(resolver.call_count, 0 if phase == 'before' else 1)


class ReviewerPathsTests(GateFixture):
    def test_actual_tree_paths_and_moving_refs(self):
        self.edit(self.branch, {'outside-new.txt': 'added\n', 'data.txt': 'changed\n',
                                'outside.txt': None, 'renamed.txt': 'initial\n',
                                'type.txt': ('symlink', 'data.txt')})
        row = self.invoke(1)
        commit, _ = rechain.resolve(self.repo, self.a[2], (SPEC, self.branch, self.b[2]),
                                   'gate-' + SPEC, ev=lambda *a, **kw: None)
        tree = git(self.repo, 'rev-parse', commit + '^{tree}')
        self.assertEqual(row['merged_tree'], tree[:12])
        expected = ['data.txt', 'outside-new.txt', 'renamed.txt']
        self.assertEqual(mg.out_of_grant(self.branch, self.main, SPEC), expected)
        print(json.dumps(dict(tree=tree, actual_diff=git(self.repo, 'diff', '--name-status', self.a[2], tree), paths=expected)))
        self.edit(self.branch, {'second.txt': 'later\n'})
        self.assertEqual(mg.out_of_grant(self.branch, self.main, SPEC), expected + ['second.txt'])

    def test_append_only_and_removal_semantics(self):
        self.edit(self.main, {mg.APPEND_LIST: 'data.txt\n'})
        self.edit(self.branch, {'data.txt': 'original\naddition\n', 'outside.txt': None})
        self.assertEqual(mg.out_of_grant(self.branch, self.main, SPEC), [])
        self.edit(self.branch, {'data.txt': 'replacement\n'})
        self.assertEqual(mg.out_of_grant(self.branch, self.main, SPEC), ['data.txt'])

    def test_uncertainty_never_returns_empty_paths(self):
        original = mg._resolver_call
        def absent(*args):
            result = original(*args)
            result['receipts'] = []
            return result
        for failure, reason in [(absent, 'conflict-diagnostic'),
                                (OSError('git unavailable'), 'gate-tooling'),
                                (subprocess.TimeoutExpired('git', 1), 'timeout')]:
            with self.subTest(reason=reason), patch.object(mg, '_resolver_call', side_effect=failure):
                with self.assertRaises(mg.Undetermined) as caught:
                    mg.out_of_grant(self.branch, self.main, SPEC)
                self.assertEqual(caught.exception.reason, reason)
        with patch.object(mg, '_remaining_deadline', side_effect=mg.Undetermined('expired', reason='deadline')):
            with self.assertRaises(mg.Undetermined) as caught:
                mg.out_of_grant(self.branch, self.main, SPEC)
            self.assertEqual(caught.exception.reason, 'deadline')
        self.edit(self.branch, {rechain.VERSIONS + 'extra.py': "revision='extra'\ndown_revision='b'\n"})
        with self.assertRaises(mg.Undetermined) as caught:
            mg.out_of_grant(self.branch, self.main, SPEC)
        self.assertEqual(caught.exception.reason, 'merge-conflict')

    def test_revert_shaped_append_annotation(self):
        self.edit(self.main, {mg.APPEND_LIST: 'data.txt\n', 'data.txt': 'original\naddition\n'})
        self.edit(self.main, {'data.txt': 'original\n'})
        self.edit(self.branch, {'data.txt': 'original\naddition\n'})
        row = self.invoke()
        self.assertTrue(row['append_only'][0]['ok'])
        self.assertTrue(row['append_only'][0]['revert_shaped'])
        self.assertEqual(mg.out_of_grant(self.branch, self.main, SPEC),
                         ['data.txt (append-only ok, revert-shaped)'])


class IsolationTests(GateFixture):
    def snapshot(self, repo):
        paths = git(repo, 'ls-files', '-z').split('\0')
        return dict(refs=git(repo, 'show-ref'), index=(repo / '.git/index').read_bytes(),
                    tracked={p: (repo / p).read_bytes() for p in paths if p},
                    worktrees=git(repo, 'worktree', 'list', '--porcelain'))

    def test_routing_and_environment_isolation(self):
        other = Fixture(self.root / 'other')
        benign = self.invoke()
        forbidden = """
def forbidden(*a, **kw):
    raise AssertionError('forbidden external/landing boundary')
import window_gate
rechain.fingerprint = forbidden
rechain._fingerprint = forbidden
window_gate.advance = forbidden
window_gate.run_attempt = forbidden
window_gate.run_window = forbidden
window_gate.verify = forbidden
window_gate.fingerprint_candidate = forbidden
rechain.fold.append = forbidden
"""
        worker = mg._RESOLVER_SCRIPT.replace('receipts, proofs = [], []', forbidden + '\nreceipts, proofs = [], []')
        hostile = dict(GIT_DIR=str(other.repo / '.git'), GIT_INDEX_FILE=str(other.repo / '.git/index'),
                       GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='diff.relative', GIT_CONFIG_VALUE_0='true',
                       GIT_CONFIG_KEY_1='diff.ignoreSubmodules', GIT_CONFIG_VALUE_1='all')
        for phase in ('accepted', 'declined', 'exception'):
            if phase == 'declined':
                self.edit(self.branch, {rechain.VERSIONS + 'extra.py': "revision='extra'\ndown_revision='b'\n"})
            before, other_before = self.snapshot(self.repo), self.snapshot(other.repo)
            injected = worker
            if phase == 'exception':
                injected = worker.replace('commit, reason = rechain.resolve', 'raise OSError("injected")\n    commit, reason = rechain.resolve')
            with patch.dict(os.environ, hostile), patch.object(mg, '_RESOLVER_SCRIPT', injected):
                environment = dict(os.environ)
                if phase == 'accepted':
                    row = self.invoke()
                    self.assertEqual(row['merged_tree'], benign['merged_tree'])
                elif phase == 'declined':
                    import branch_refresh
                    with patch.object(branch_refresh, 'gate_refresh', return_value=None):
                        self.invoke(1)
                else:
                    row = self.invoke(2)
                    self.assertEqual(row['reason'], 'gate-tooling')
                # main_ owns its ledger selector; resolver owns no caller env.
                environment['DOIT_LEDGER_FILE'] = 'L-executor-fixture.jsonl'
                self.assertEqual(dict(os.environ), environment)
            self.assertEqual(self.snapshot(self.repo), before)
            self.assertEqual(self.snapshot(other.repo), other_before)
            self.assertFalse(list((self.root / 'scratch').glob('**/worktree')))
            print(json.dumps(dict(phase=phase, refs_index_tracked_unchanged=True, environment_restored=True)))

    def test_plain_and_specless_paths_and_start_are_preserved(self):
        original = mg._resolver_call
        starts = []
        def call(*args):
            starts.append(mg._START)
            return original(*args)
        with patch.object(mg, '_resolver_call', side_effect=call):
            self.invoke()
        self.assertEqual(starts, [mg._START])
        with patch.object(mg, '_resolver_call', side_effect=AssertionError('spec-less resolver')):
            self.assertEqual(mg.main_([self.branch, self.main]), 1)
        git(self.repo, 'branch', '-f', 'plain', self.a[2])
        self.edit('plain', {'simple.txt': 'plain addition\n'})
        with patch.object(mg, '_resolver_call', side_effect=AssertionError('plain resolver')):
            self.assertEqual(mg.main_(['plain', self.main, '--spec', SPEC]), 0)

    def test_existing_regression_bytes_and_script(self):
        repo = SOURCE.parent
        # Integration-base bytes include already committed neighbour work.
        integration = git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD')
        baseline = subprocess.check_output(['git', '-C', str(repo), 'show',
                                             integration + ':src/test_merge_gate.py'])
        # The one granted fixture edit (test_guard_short_circuits_and_failed_conflict_diagnosis)
        # is excised from both copies; every other byte stays pinned.
        def without_granted(data):
            head = b'        def test_guard_short_circuits_and_failed_conflict_diagnosis(self):\n'
            start = data.index(head)
            end = data.index(b'\n        def ', start + len(head))
            return data[:start] + data[end:]
        self.assertEqual(without_granted((SOURCE / 'test_merge_gate.py').read_bytes()),
                         without_granted(baseline))
        result = subprocess.run(['/usr/bin/python3', str(SOURCE / 'test_merge_gate.py')],
                                cwd=repo, capture_output=True, text=True)
        failures = [line for line in result.stdout.splitlines() if 'FAIL' in line]
        self.assertEqual(result.returncode, 0, '\n'.join(failures) + result.stderr[-3000:])
        print('Unmodified src/test_merge_gate.py: ' + result.stdout.splitlines()[-1])


if __name__ == '__main__':
    unittest.main()
