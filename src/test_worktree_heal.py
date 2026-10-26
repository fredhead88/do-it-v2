#!/usr/bin/env python3
"""Missing checkout regressions: real git, real resource ledger and retry state."""
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Isolate before importing any module that captures a root or a home directory.
_INVOKING_HOME = Path.home().resolve()
_ISO = tempfile.TemporaryDirectory(prefix='worktree-heal-import-')
_IMPORT_ROOT = Path(_ISO.name)
os.environ.update(HOME=str(_IMPORT_ROOT / 'home'), DOIT_ROOT=str(_IMPORT_ROOT / 'root'),
                  DOIT_NO_POKE='1')
for _name in ('DOIT_LEDGER_FILE', 'DOIT_PROJECT', 'DOIT_REAP_LEDGER_FILE'):
    os.environ.pop(_name, None)

import autodispatch
import dispatch
import fold
import lane_driver
import nextstep
import resources
import tick
import tree_cleanup

assert Path.home().resolve() != _INVOKING_HOME
assert all(p.resolve().is_relative_to(_IMPORT_ROOT)
           for p in (fold.ROOT, dispatch.ROOT, fold.EVENTS))


class Fixture:
    sid = 'L-spec-9001'
    branch = sid.lower()

    def __init__(self, root, stack):
        self.root = root
        self.repo = root / 'repos/proj'
        self.wt = root / 'worktrees/proj' / self.branch
        self.state = root / 'state/worktree-heal.json'
        self.calls = []
        self.now = dt.datetime.now(dt.timezone.utc)
        for module, name, value in (
                (fold, 'ROOT', root), (fold, 'EVENTS', root / 'events'),
                (fold, 'PROJECT', None), (dispatch, 'ROOT', root),
                (dispatch, 'EVENTS', root / 'events'), (dispatch, 'CONTENT', root / 'content')):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(root)))
        for name in ('events', 'content', 'repos/proj', 'repos/other'):
            (root / name).mkdir(parents=True)
        (root / 'content' / (self.sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        self.git('init', '-q', '-b', 'main')
        self.commit(self.repo, 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('worktree', 'add', '-q', str(self.wt), '-b', self.branch)
        self.commit(self.wt, 'ready')
        self.tip = self.git('rev-parse', self.branch)
        self.add('spec-written', project='proj', charter='L-charter-0052')

    def git(self, *args, cwd=None):
        p = subprocess.run(['git', '-C', str(cwd or self.repo), *args],
                           text=True, capture_output=True)
        if p.returncode:
            raise AssertionError(f'git {args}: {p.stderr}')
        return p.stdout.strip()

    def commit(self, path, text):
        (path / 'seed').write_text(text)
        self.git('add', 'seed', cwd=path)
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', text, cwd=path)

    def add(self, kind, **fields):
        # Fixtures are appended to disk too; every pass reopens the full ledger.
        stamp = dt.datetime.now(dt.timezone.utc)
        if kind == 'spec-written':
            stamp -= dt.timedelta(hours=1)
        with (self.root / 'events/L-operator-fixture.jsonl').open('a') as out:
            out.write(json.dumps(dict(v=1, subject=self.sid, type=kind,
                                      ts=stamp.isoformat(), **fields)) + '\n')

    def events(self):
        return fold.read_events(use_cache=False)

    def rows(self, kind):
        return [e for e in self.events() if e['type'] == kind]

    def runner(self, argv):
        self.calls.append(argv)
        if argv[0] == 'git':
            p = subprocess.run(argv, text=True, capture_output=True)
            return dict(code=p.returncode, stdout=p.stdout, stderr=p.stderr)
        if argv[:2] == ['doit', 'packet']:
            return dict(code=0, stdout='/fixture-packet.md', stderr='')
        if argv[:2] == ['doit', 'dispatch']:
            return dict(code=0, stdout=json.dumps({'detached': 'L-fixture-9001'}), stderr='')
        raise AssertionError(f'unexpected provider boundary: {argv}')

    def step(self, action):
        return nextstep.Step(self.sid, 'spec', 'checking', 'action', action, 'tick',
                             'fixture:1', 'condition met', None, self.now.isoformat())

    def run(self, action='rebuild', runner=None, **kwargs):
        events = self.events()
        return tick.run_steps(events, {}, self.now, dispatcher=runner or self.runner,
                              steps=[self.step(action)], **kwargs)

    def missing(self, prune=False):
        # Count and prove the only deletion target belongs to this scratch fixture.
        assert self.wt.is_relative_to(self.root) and self.wt.is_dir()
        shutil.rmtree(self.wt)
        if prune:
            self.git('worktree', 'prune')

    def ready(self, **fields):
        self.add('build-done', project='proj', branch=self.branch,
                 ready_sha=fields.pop('ready_sha', self.tip), **fields)

    def lock_runner(self, *, raised=False, probe=False, cause='fatal: fixture lock'):
        def run(argv):
            target = 'show-ref' in argv if probe else argv[-4:] == [
                'worktree', 'add', str(self.wt), self.branch]
            if target:
                self.calls.append(argv)
                if raised:
                    raise OSError('fixture fork')
                return dict(code=128, stdout='', stderr=cause)
            return self.runner(argv)
        return run

    def ledger_bytes(self):
        return {p.name: p.read_bytes() for p in (self.root / 'events').glob('*.jsonl')}


class HealTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory(dir=_IMPORT_ROOT) as d, contextlib.ExitStack() as stack:
            yield Fixture(Path(d), stack)

    def repaired(self, f, out, tip=None):
        self.assertTrue(out[0].startswith('acted '), out)
        self.assertEqual(f.git('symbolic-ref', '--short', 'HEAD', cwd=f.wt), f.branch)
        self.assertEqual(f.git('rev-parse', 'HEAD', cwd=f.wt), tip or f.tip)
        listing = f.git('worktree', 'list', '--porcelain')
        self.assertEqual(listing.count('worktree ' + str(f.wt)), 1)
        self.assertNotIn('prunable', listing)
        rows = f.rows('resource-created')
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ('kind', 'ref', 'lane', 'subject')},
                         dict(kind='worktree', ref=str(f.wt), lane='build', subject=f.sid))
        self.assertTrue(f.rows('autodispatched')[-1]['worktree_recreated'])
        self.assertEqual(f.rows('autodispatch-failed'), [])

    def definitive(self, f, action, cause, no_calls=False):
        before = (f.git('branch', '--list'), f.git('worktree', 'list', '--porcelain'),
                  sorted(str(p.relative_to(f.root)) for p in (f.root / 'worktrees').rglob('*')))
        f.calls.clear()
        out = f.run(action)
        expected = f'worktree {f.wt} missing; not recreated: {cause}'
        self.assertEqual(f.rows('autodispatch-failed')[-1]['reason'], expected)
        self.assertEqual(len(f.rows('autodispatch-failed')), 1)
        self.assertIn(expected, out[0])
        self.assertEqual(f.rows('resource-created'), [])
        self.assertFalse(any('worktree' in a and any(x in a for x in ('add', 'remove', 'prune'))
                             for a in f.calls))
        after = (f.git('branch', '--list'), f.git('worktree', 'list', '--porcelain'),
                 sorted(str(p.relative_to(f.root)) for p in (f.root / 'worktrees').rglob('*')))
        self.assertEqual(before, after)
        if no_calls:
            self.assertEqual(f.calls, [])

    def test_AC1_authoritative_project_argv(self):
        for action in ('rebuild', 'regrade', 'review', 'build'):
            with self.subTest(action=action), self.fixture() as f:
                f.add('decision', project='other')
                if action == 'build':
                    f.git('reset', '--hard', f.base, cwd=f.wt)
                self.assertTrue(f.run(action)[0].startswith('acted'))
                packet = next(a for a in f.calls if a[:2] == ['doit', 'packet'])
                launched = next(a for a in f.calls if a[:2] == ['doit', 'dispatch'])
                self.assertEqual(packet[packet.index('--worktree') + 1], str(f.wt))
                self.assertEqual(launched[launched.index('--project') + 1], 'proj')
                self.assertFalse(any('other' in str(a) for a in f.calls))
                self.assertFalse((f.root / 'worktrees/other').exists())

    def test_AC1_newest_witness_and_exact_fallback(self):
        import worktree_heal as heal
        with self.fixture() as f:
            evs = [dict(type='decision', project='other')]
            self.assertEqual(heal.project_of(evs, f.root), 'other')
            self.assertEqual(heal.project_of([], f.root), 'unknown')
            for invalid in ('..', '.', 'a/b', 'missing', 'a\x00b', 42):
                with self.subTest(invalid=invalid):
                    rows = [dict(type='build-done', project='proj'),
                            dict(type='spec-written', project=invalid), *evs]
                    self.assertEqual(heal.project_of(rows, f.root), 'other')
            self.assertEqual(heal.project_of([dict(type='spec-written', project='other'),
                                             dict(type='build-done', project='proj')], f.root), 'proj')

    def test_AC1_fallback_and_recency_transport(self):
        for kind in ('legacy', '..', 'a/b', 'missing', 'new-build', 'new-invalid'):
            with self.subTest(kind=kind), self.fixture() as f:
                ledger = f.root / 'events/L-operator-fixture.jsonl'
                if kind == 'legacy':
                    ledger.write_text('')
                elif kind in ('..', 'a/b', 'missing'):
                    f.add('spec-written', project=kind)
                elif kind == 'new-build':
                    ledger.write_text(ledger.read_text().replace('"project": "proj"', '"project": "other"'))
                    f.ready()
                else:
                    f.ready()
                    f.add('spec-written', project='missing')
                    # This witness is newer than build-done, as required by the fixture.
                    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
                    rows[-1]['ts'] = dt.datetime.now(dt.timezone.utc).isoformat()
                    ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                f.add('decision', project='other')
                expected = 'proj' if kind == 'new-build' else 'other'
                tree = f.root / 'worktrees' / expected / f.branch
                tree.mkdir(parents=True, exist_ok=True)
                self.assertTrue(f.run('regrade')[0].startswith('acted'))
                packet, launched = f.calls
                self.assertEqual(packet[packet.index('--worktree') + 1], str(tree))
                self.assertEqual(launched[launched.index('--project') + 1], expected)

    def test_AC1_sentinel_restores_project_rule(self):
        with self.fixture() as f:
            f.add('decision', project='other')
            other = f.root / 'worktrees/other' / f.branch
            other.mkdir(parents=True)
            f.state.parent.mkdir()
            (f.state.parent / 'worktree-heal.off').touch()
            self.assertTrue(f.run()[0].startswith('acted'))
            self.assertEqual(f.calls[-1][-2:], ['--project', 'other'])

    def test_AC2_stale_pruned_and_other_stale_registration(self):
        for variant in ('stale', 'pruned', 'other'):
            with self.subTest(variant=variant), self.fixture() as f:
                branches = f.git('branch', '--list')
                f.missing(prune=variant != 'stale')
                if variant == 'other':
                    old = f.root / 'worktrees/old' / f.branch
                    f.git('worktree', 'add', str(old), f.branch)
                    self.assertEqual(sum(p == old for p in old.parent.iterdir()), 1)
                    shutil.rmtree(old)
                self.repaired(f, f.run())
                self.assertEqual(f.git('branch', '--list'), branches)
                self.assertFalse(f.state.exists())
                if variant == 'other':
                    self.assertNotIn(str(old), f.git('worktree', 'list', '--porcelain'))

    def test_AC2_symlinked_repository(self):
        with self.fixture() as f:
            actual = f.root / 'actual-repo'
            f.repo.rename(actual)
            f.repo.symlink_to(actual, target_is_directory=True)
            f.missing()
            self.repaired(f, f.run())

    def test_AC3_grade_regrade_review_exact_commit(self):
        for action in ('grade', 'regrade', 'review'):
            with self.subTest(action=action), self.fixture() as f:
                f.ready()
                f.missing()
                self.repaired(f, f.run(action))

    def test_AC3_wrong_tip_then_decision_repairs(self):
        with self.fixture() as f:
            f.ready()
            f.commit(f.wt, 'later')
            later = f.git('rev-parse', f.branch)
            f.missing(prune=True)
            self.definitive(f, 'grade', f'branch {f.branch} tip {later} is not ready_sha {f.tip}')
            self.assertFalse(f.wt.exists())
            f.add('decision')
            f.git('branch', '-f', f.branch, f.tip)
            self.assertTrue(f.run('grade')[0].startswith('acted'))
            self.assertEqual(f.git('rev-parse', 'HEAD', cwd=f.wt), f.tip)

    def test_AC3_unknown_ready_commit(self):
        with self.fixture() as f:
            sha = '1' * 40
            f.ready(ready_sha=sha)
            f.missing()
            self.definitive(f, 'regrade', f'ready_sha {sha} is not a commit in {f.repo}')

    def test_AC4_definitive_matrix(self):
        cases = ('branch-rebuild', 'branch-review', 'repo-missing', 'branch-name', 'other-live',
                 'other-locked', 'own-locked', 'file', 'symlink', 'no-ready', 'non-repo', 'first-project')
        for case in cases:
            with self.subTest(case=case), self.fixture() as f:
                action, no_calls = 'rebuild', False
                if case == 'own-locked':
                    f.git('worktree', 'lock', str(f.wt))
                f.missing(prune=case not in ('own-locked',))
                if case.startswith('branch-') and case != 'branch-name':
                    f.git('branch', '-D', f.branch)
                    if case == 'branch-review':
                        action = 'review'
                        f.ready()
                    cause = f'branch {f.branch} does not exist'
                elif case == 'repo-missing':
                    f.repo.rename(f.root / 'saved-repo')
                    cause, no_calls = f'repo {f.repo} is not a directory', True
                elif case == 'branch-name':
                    f.add('build-done', branch=f.branch + '-r2')
                    cause, no_calls = f'newest build-done names branch {f.branch}-r2, not {f.branch}', True
                elif case.startswith('other-'):
                    old = f.root / 'worktrees/old' / f.branch
                    f.git('worktree', 'add', str(old), f.branch)
                    if case == 'other-locked':
                        f.git('worktree', 'lock', str(old))
                        shutil.rmtree(old)
                    cause = f'branch {f.branch} is checked out at {old.resolve()}'
                elif case == 'own-locked':
                    cause = f'registration of {f.wt} is locked'
                elif case in ('file', 'symlink'):
                    if case == 'file':
                        f.wt.write_bytes(b'occupied')
                    else:
                        f.wt.symlink_to(f.root / 'absent')
                    cause, no_calls = 'path is occupied by a non-directory', True
                elif case == 'no-ready':
                    action, cause, no_calls = 'grade', 'no build-done with a ready_sha', True
                elif case == 'non-repo':
                    f.repo.rename(f.root / 'saved-repo')
                    f.repo.mkdir()
                    cause = f'repo {f.repo} is not the top of a git work tree'
                else:
                    fixture = f.root / 'events/L-operator-fixture.jsonl'
                    fixture.write_text(fixture.read_text().replace('"project": "proj"', '"project": "other"'))
                    f.add('spec-written', project='proj')
                    cause, no_calls = "project proj is not the subject's first recorded project other", True
                # The saved real repo supplies read-only snapshots in repo absence fixtures.
                saved = f.root / 'saved-repo'
                if saved.exists():
                    before = subprocess.check_output(['git', '-C', str(saved), 'branch', '--list'])
                    f.calls.clear()
                    out = f.run(action)
                    self.assertIn(cause, out[0])
                    self.assertEqual(f.rows('autodispatch-failed')[0]['reason'],
                                     f'worktree {f.wt} missing; not recreated: {cause}')
                    self.assertEqual(len(f.rows('autodispatch-failed')), 1)
                    self.assertEqual(f.rows('resource-created'), [])
                    self.assertEqual(before, subprocess.check_output(['git', '-C', str(saved), 'branch', '--list']))
                    self.assertFalse(any('worktree' in a for a in f.calls))
                    if no_calls:
                        self.assertEqual(f.calls, [])
                else:
                    self.definitive(f, action, cause, no_calls)
                if case == 'file':
                    self.assertEqual(f.wt.read_bytes(), b'occupied')
                if case == 'symlink':
                    self.assertTrue(f.wt.is_symlink())

    def test_AC5_git_failure_and_runner_exceptions(self):
        for kind in ('lock', 'probe-raise', 'add-raise', 'long'):
            with self.subTest(kind=kind), self.fixture() as f:
                f.missing()
                before = f.ledger_bytes()
                out = f.run(runner=f.lock_runner(raised='raise' in kind, probe=kind == 'probe-raise',
                                                cause='x' * 800 if kind == 'long' else 'fatal: fixture lock'))
                prefix = f'failed {f.sid} rebuild: worktree {f.wt} missing; repair failed, retrying next tick: '
                self.assertTrue(out[0].startswith(prefix), out)
                cause = out[0][len(prefix):]
                self.assertLessEqual(len(cause), 200)
                if kind != 'long':
                    self.assertEqual(cause, 'OSError: fixture fork' if 'raise' in kind else 'fatal: fixture lock')
                self.assertEqual(f.ledger_bytes(), before)
                entry = json.loads(f.state.read_text())[f.sid + ' rebuild']
                self.assertEqual(entry['count'], 1)
                self.assertEqual(entry['last_error'], cause)
                self.assertIsNotNone(dt.datetime.fromisoformat(entry['last_ts']).tzinfo)
                self.assertEqual(list(f.state.parent.glob('.worktree-heal.*')), [])
                self.repaired(f, f.run())
                self.assertNotIn(f.sid + ' rebuild', json.loads(f.state.read_text()))

    def test_AC5_direct_retry_escapes_emit(self):
        import worktree_heal as heal
        with self.fixture() as f:
            f.missing()
            runner = tick._PassDispatcher(f.lock_runner(), f.events(), {}, f.now, False)
            with patch.object(dispatch, 'emit') as emit, self.assertRaises(heal.RepairRetry) as caught:
                tick.launch(f.step('rebuild'), dispatcher=runner)
            self.assertIsInstance(caught.exception, ValueError)
            emit.assert_not_called()

    def test_AC5_record_failure_cleanup_and_readback_keep(self):
        for readback in (False, True):
            with self.subTest(readback=readback), self.fixture() as f:
                f.missing()
                target, exc = ('_readback', resources.RecordReadbackError('fixture readback')) if readback else (
                    'record', ValueError('fixture record'))
                with patch.object(resources, target, side_effect=exc):
                    out = f.run()
                self.assertIn('repair failed, retrying next tick:', out[0])
                self.assertIn(str(exc), out[0])
                self.assertEqual(f.wt.exists(), readback)
                self.assertEqual(f.rows('autodispatch-failed'), [])
                self.assertEqual(f.rows('autodispatched'), [])
                f.calls.clear()
                self.assertTrue(f.run()[0].startswith('acted'))
                self.assertEqual(len([r for r in resources.live(f.events()) if r.ref == str(f.wt)]), 1)
                if readback:
                    self.assertFalse(any(a[0] == 'git' for a in f.calls))
                    self.assertNotIn('worktree_recreated', f.rows('autodispatched')[0])
                else:
                    self.assertTrue(f.rows('autodispatched')[0]['worktree_recreated'])

    def test_AC5_failed_record_cleanup_stands_and_keeps_tree(self):
        with self.fixture() as f:
            f.missing(prune=True)
            def runner(argv):
                if argv[-3:] == ['worktree', 'remove', str(f.wt)] and f.wt.is_dir():
                    f.calls.append(argv)
                    return dict(code=128, stdout='', stderr='fixture removal')
                return f.runner(argv)
            with patch.object(resources, 'record', side_effect=ValueError('fixture record')):
                f.run(runner=runner)
            self.assertTrue(f.wt.is_dir())
            self.assertEqual(f.rows('autodispatch-failed')[0]['reason'],
                             f'worktree {f.wt} missing; not recreated: record failed: fixture record; removing the new tree failed: fixture removal')
            self.assertEqual(f.rows('resource-created'), [])

    def test_AC6_six_attempt_bound_and_standing_seventh(self):
        with self.fixture() as f:
            f.missing()
            before = f.ledger_bytes()
            for n in range(1, 7):
                out = f.run(runner=f.lock_runner())
                if n < 6:
                    self.assertIn('repair failed, retrying next tick:', out[0])
                    self.assertEqual(f.ledger_bytes(), before)
                    self.assertEqual(json.loads(f.state.read_text())[f.sid + ' rebuild']['count'], n)
                else:
                    self.assertEqual(f.rows('autodispatch-failed')[0]['reason'],
                                     f'worktree {f.wt} missing; not recreated: repair failed 6 times in a row: fatal: fixture lock')
                    self.assertNotIn(f.sid + ' rebuild', json.loads(f.state.read_text()))
            f.calls.clear()
            f.run()
            self.assertEqual(f.calls, [])
            self.assertEqual(len(f.rows('autodispatch-failed')), 1)

    def test_AC6_success_resets_and_actions_are_independent(self):
        for reset in (False, True):
            with self.subTest(reset=reset), self.fixture() as f:
                f.missing()
                for _ in range(3):
                    f.run(runner=f.lock_runner())
                if reset:
                    self.assertTrue(f.run()[0].startswith('acted'))
                    f.missing()
                    for _ in range(5):
                        f.run(runner=f.lock_runner())
                    self.assertEqual(f.rows('autodispatch-failed'), [])
                    self.assertEqual(json.loads(f.state.read_text())[f.sid + ' rebuild']['count'], 5)
                else:
                    f.ready()
                    f.run('regrade', runner=f.lock_runner())
                    state = json.loads(f.state.read_text())
                    self.assertEqual(state[f.sid + ' rebuild']['count'], 3)
                    self.assertEqual(state[f.sid + ' regrade']['count'], 1)

    def test_AC6_bounded_corrupt_and_unwritable_state(self):
        for kind in ('full', 'json', 'array', 'parent-file', 'state-dir'):
            with self.subTest(kind=kind), self.fixture() as f:
                f.missing()
                if kind == 'parent-file':
                    f.state.parent.write_text('occupied')
                else:
                    f.state.parent.mkdir()
                    if kind == 'state-dir':
                        f.state.mkdir()
                    elif kind == 'full':
                        f.state.write_text(json.dumps({f'other-{n} rebuild': dict(count=1, last_ts=f'{n:04}', last_error='old')
                                                       for n in range(200)}))
                    else:
                        f.state.write_text('not json' if kind == 'json' else '[]')
                out = f.run(runner=f.lock_runner())
                if kind in ('parent-file', 'state-dir'):
                    self.assertTrue(f.rows('autodispatch-failed')[0]['reason'].startswith(
                        f'worktree {f.wt} missing; not recreated: retry not counted: fatal: fixture lock; state file: '))
                else:
                    self.assertIn('retrying next tick', out[0])
                    state = json.loads(f.state.read_text())
                    self.assertEqual(state[f.sid + ' rebuild']['count'], 1)
                    self.assertEqual(len(state), 200 if kind == 'full' else 1)
                    self.assertNotIn('other-0 rebuild', state)

    def test_AC6_failed_counter_cleanup_logs_but_dispatches(self):
        import worktree_heal as heal
        with self.fixture() as f:
            f.missing()
            f.run(runner=f.lock_runner())
            err = io.StringIO()
            with patch.object(heal, '_write_state', side_effect=OSError('fixture write')), contextlib.redirect_stderr(err):
                out = f.run()
            self.assertTrue(out[0].startswith('acted'))
            self.assertEqual(len(f.rows('autodispatched')), 1)
            self.assertIn('worktree-heal: state file: fixture write', err.getvalue())
            self.assertEqual(json.loads(f.state.read_text())[f.sid + ' rebuild']['count'], 1)

    def test_AC7_existing_directory_exact_transport_and_no_state(self):
        for action in ('rebuild', 'grade', 'regrade', 'review'):
            with self.subTest(action=action), self.fixture() as f:
                self.assertTrue(f.run(action)[0].startswith('acted'))
                self.assertEqual([a[:2] for a in f.calls], [['doit', 'packet'], ['doit', 'dispatch']])
                self.assertNotIn('worktree_recreated', f.rows('autodispatched')[0])
                self.assertFalse(f.state.parent.exists())

    def test_AC7_wait_dry_hold_standing_and_review_refusals(self):
        for kind in ('seat', 'dry', 'hold', 'undetermined', 'standing', 'review-round', 'browser'):
            with self.subTest(kind=kind), self.fixture() as f:
                f.missing()
                with contextlib.ExitStack() as stack:
                    if kind == 'seat':
                        stack.enter_context(patch.object(autodispatch, 'capacity', return_value=0))
                    if kind in ('hold', 'undetermined'):
                        from dataclasses import replace
                        step = replace(f.step('rebuild'), **({'state': 'hold', 'clears': 'decision'} if kind == 'hold' else {'reason': 'undetermined: fixture'}))
                        tick.run_steps(f.events(), {}, f.now, dispatcher=f.runner, steps=[step])
                    else:
                        if kind == 'standing':
                            f.add('autodispatch-failed', reason='fixture')
                        if kind.startswith('review') or kind == 'browser':
                            stack.enter_context(patch.object(lane_driver, '_review_options', return_value=None if kind == 'review-round' else ('needs-browser', 1)))
                        f.run('review' if kind in ('review-round', 'browser') else 'rebuild', dry_run=kind == 'dry')
                self.assertEqual(f.calls, [])
                self.assertFalse(f.wt.exists())
                self.assertFalse(f.state.parent.exists())

    def test_AC7_switch_and_import_fallback_then_reenable(self):
        for kind in ('file', 'dangling', 'import'):
            with self.subTest(kind=kind), self.fixture() as f:
                f.missing()
                off = f.state.parent / 'worktree-heal.off'
                if kind != 'import':
                    off.parent.mkdir()
                    off.symlink_to(f.root / 'absent') if kind == 'dangling' else off.touch()
                with contextlib.ExitStack() as stack:
                    if kind == 'import':
                        stack.enter_context(patch.dict(sys.modules, worktree_heal=None))
                    out = f.run()
                self.assertEqual(out, [f'failed {f.sid} rebuild: worktree {f.wt} missing'])
                self.assertEqual(f.rows('autodispatch-failed')[0]['reason'], f'worktree {f.wt} missing')
                self.assertEqual(f.calls, [])
                self.assertFalse(f.state.exists())
                if kind != 'import':
                    off.unlink()
                f.add('decision')
                self.assertTrue(f.run()[0].startswith('acted'))

    def test_AC8_blocked_ancestor_and_recorded_tree_survive_reapers(self):
        for repaired in (False, True):
            with self.subTest(repaired=repaired), self.fixture() as f:
                f.git('reset', '--hard', f.base, cwd=f.wt)
                f.tip = f.base
                origin = f.root / 'origin.git'
                f.git('init', '--bare', str(origin))
                f.git('remote', 'add', 'origin', str(origin))
                f.git('push', '-u', 'origin', 'main')
                f.add('build-started', spawn='L-builder-9001')
                f.add('spawn-done', spawn='L-builder-9001')
                f.ready(status='BLOCKED')
                f.add('build-blocked')
                f.add('question', asks='fixture')
                before = f.events()
                if repaired:
                    f.missing()
                    self.assertTrue(f.run()[0].startswith('acted'))
                for events, reason in ((before, 'spec not finished'), (f.events(), 'recorded resource' if repaired else 'spec not finished')):
                    removed, retained = tree_cleanup.reap_worktrees(events)
                    self.assertEqual(removed, [])
                    self.assertTrue(any(reason in r['why'] for r in retained), retained)
                tree_cleanup.reap_merged_specs(f.events())
                self.assertTrue(f.wt.is_dir())
                self.assertIn(str(f.wt), f.git('worktree', 'list', '--porcelain'))
                self.assertFalse(any(e['type'] in ('resource-reaped', 'worktree-reaped', 'tree-reaped') for e in f.events()))
                self.assertEqual(len([r for r in resources.live(f.events()) if r.ref == str(f.wt)]), int(repaired))

    def test_AC9_import_isolation(self):
        self.assertNotEqual(Path.home().resolve(), _INVOKING_HOME)
        for path in (fold.ROOT, dispatch.ROOT, fold.EVENTS):
            self.assertTrue(path.resolve().is_relative_to(_IMPORT_ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--criterion', choices=[f'AC{n}' for n in range(1, 10)])
    args = parser.parse_args()
    criteria = [args.criterion] if args.criterion else [f'AC{n}' for n in range(1, 10)]
    for criterion in criteria:
        names = [name for name in unittest.defaultTestLoader.getTestCaseNames(HealTests)
                 if name.startswith('test_' + criterion + '_')]
        if not names:
            parser.error('empty criterion selection')
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(HealTests(n) for n in names))
        if not result.wasSuccessful():
            return 1
        body = json.dumps(dict(criterion=criterion, tests=result.testsRun, assertions='passed',
                               matrix=names, data_source='scratch', customer_data=False), sort_keys=True)
        print(json.dumps(dict(url=f'cli://worktree-heal/{criterion}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
