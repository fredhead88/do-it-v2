#!/usr/bin/env python3
"""Disposable Git/ledger regressions for automatic branch refresh."""
import contextlib
from datetime import datetime, timezone, timedelta
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branch_refresh as br
import fold


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='br-scratch-', dir='/var/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        isolated = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        isolated.update(HOME=str(self.root / 'home'), DOIT_ROOT=str(self.root))
        environment = patch.dict(os.environ, isolated, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / 'home').mkdir()
        self.repo = self.root / 'repos/do-it-v2'
        self.repo.mkdir(parents=True)
        self.sid = 'L-spec-9774'
        self.branch = self.sid.lower()
        self.now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Original Author')
        self.git('config', 'user.email', 'author@example.invalid')
        (self.repo / 'base').write_text('old\n')
        (self.repo / 'guard.py').write_text('assert True\n')
        self.commit('base')
        self.old = self.git('rev-parse', 'HEAD')
        self.tree = self.root / 'builder'
        self.git('worktree', 'add', '-qb', self.branch, str(self.tree))
        (self.tree / 'feature.py').write_text('feature\n')
        self.commit('feature message', self.tree)
        self.start = self.git('rev-parse', self.branch)
        (self.repo / 'base').write_text('new\n')
        self.commit('main moved')
        self.main = self.git('rev-parse', 'HEAD')
        self.snapshot = self.git('rev-parse', 'HEAD', 'HEAD^{tree}')
        (self.root / 'content').mkdir()
        self.spec = self.root / 'content' / (self.sid + '.md')
        self.spec.write_text('Writes:\n- feature.py\n\n## Verification\n```bash\n/usr/bin/python3 -c "assert True"\n```\n')
        self.events = [dict(type='build-done', subject=self.sid, status='DONE', project='do-it-v2', ts=self.now.isoformat())]
        self.cfg = {'merge_queue': {'branch_refresh_per_day': 3}}
        for p in (patch.multiple(fold, ROOT=self.root, EVENTS=self.root / 'events', NOW=self.now),
                  patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_LEDGER_FILE='L-executor-0001.jsonl'),
                  patch('branch_refresh.config', side_effect=lambda: self.cfg),
                  patch('branch_refresh.guard_commands', return_value=[('guard', '/usr/bin/python3 guard.py')]),
                  patch('grading_env._project_row', return_value={}),
                  patch('branch_refresh.regate', return_value=0)):
            p.start()
            self.addCleanup(p.stop)
        self.bundle_patch = patch('build_runner.run', side_effect=self.bundle)
        self.bundle_patch.start()
        self.addCleanup(self.bundle_patch.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message, cwd=None):
        self.git('add', '.', cwd=cwd)
        self.git('commit', '-qm', message, cwd=cwd)

    def bundle(self, spec, sha):
        path = self.root / 'bundle.json'
        path.write_text(json.dumps({'spec': spec, 'sha': sha}))
        return path

    def run_refresh(self):
        return br.refresh(self.repo, self.branch, 'main', self.sid, events=self.events, now=self.now)

    def real_gate(self, repo, branch, main, spec):
        import merge_gate
        previous = Path.cwd()
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir'))
        with (common / ('branch-refresh-' + spec + '.lock')).open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.chdir(repo)
            with patch('merge_gate.secret_scan', return_value=('clean', [])):
                return merge_gate.main_([branch, main, '--spec', spec])
        finally:
            os.chdir(previous)

    def read(self):
        return fold.read_events(use_cache=False)

    def invariant(self, untouched=False):
        self.assertEqual(self.git('rev-parse', 'HEAD', 'HEAD^{tree}'), self.snapshot)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('diff', '--cached', '--raw'), '')
        self.assertEqual(len(self.git('worktree', 'list', '--porcelain').split('worktree ')) - 1, 2)
        if untouched:
            self.assertEqual(self.git('rev-parse', self.branch), self.start)

    def signed(self, ac, **body):
        # Reports bind the complete artifact to its digest, without provider I/O.
        data = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url='fixture://branch-refresh/' + ac, status=0,
                             body_sha256=hashlib.sha256(data).hexdigest(), body_excerpt=body)))


class TestAC1(Fixture):
    def test_adoption_metadata_and_persisted_event(self):
        import build_runner
        self.bundle_patch.stop()
        (self.root / 'events').mkdir(exist_ok=True)
        build = dict(v=1, type='build-done', subject=self.sid, project='do-it-v2', status='DONE',
                     ts=(self.now - timedelta(hours=1)).isoformat(), ready_sha=self.start, base_sha=self.old)
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(build) + '\n')
        with patch('branch_refresh.git', wraps=br.git) as trace, \
             patch('grading_env._project_checkout', return_value=self.repo), \
             patch('build_runner.SHORT_TMP_ROOT', self.root), \
             patch('build_runner.SHORT_TMP_BACKING_ROOT', self.root / 'runner-backing'), \
             patch('scratch.sub', side_effect=lambda name: self.root / name), \
             patch('build_runner.run', wraps=build_runner.run) as bundle, \
             patch('branch_refresh.regate', side_effect=self.real_gate) as gate:
            (self.root / 'runner').mkdir()
            (self.root / 'runner-backing').mkdir()
            result = self.run_refresh()
        bundle.assert_called_once()
        self.assertEqual(result['state'], 'refreshed')
        sha = self.git('rev-parse', self.branch)
        self.assertNotEqual(sha, self.start)
        self.assertEqual(self.git('rev-list', '--count', 'main..' + self.branch), '1')
        self.assertEqual(self.git('show', '-s', '--format=%an%n%ae%n%B', sha),
                         self.git('show', '-s', '--format=%an%n%ae%n%B', self.start))
        self.assertEqual(self.git('show', '-s', '--format=%cn', sha), 'Recheck Bot')
        bundle_body = json.loads(Path(result['bundle']).read_text())
        self.assertEqual(bundle_body['sha'], sha)
        self.assertTrue(bundle_body['commands'])
        self.assertTrue(all(r['exit'] == 0 for r in bundle_body['commands']))
        rows = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json, fold; print(json.dumps(fold.read_events(use_cache=False)))'],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)), text=True))
        event = next(e for e in rows if e['type'] == 'branch-refreshed')
        self.assertEqual((event['from_sha'], event['to_sha'], event['base_sha'], event['checks']),
                         (self.start, sha, self.main, 'pass'))
        gate.assert_called_once()
        self.assertTrue(any(e['type'] == 'merge-gate-clean' for e in rows))
        commands = [list(c.args[1:]) for c in trace.call_args_list]
        self.assertTrue(any('rebase' in c and '--no-autostash' in c and '--no-update-refs' in c
                            and '--rebase-merges' in c and 'rerere.enabled=true' in c for c in commands))
        self.assertFalse(any(e['type'] in ('conflict-rework', 'spawn-started') for e in rows))
        self.assertEqual(self.git('config', '--get', 'user.name'), 'Original Author')
        self.invariant()
        self.signed('AC1', event=event, commands=commands, bundle=True, gate=0, metadata=True)


class TestAC2(Fixture):
    def test_conflict_cleanup(self):
        (self.tree / 'base').write_text('conflicting feature\n')
        self.git('add', '.', cwd=self.tree)
        self.git('commit', '--amend', '-qm', 'feature message', cwd=self.tree)
        self.start = self.git('rev-parse', self.branch)
        r = self.run_refresh()
        self.assertEqual(r['state'], 'conflict')
        self.assertIn('base', r['paths'])
        event = self.read()[-1]
        self.assertEqual(event['type'], 'conflict-rework')
        self.assertTrue(event['attempt'])
        self.assertTrue(event['verdict'])
        self.invariant(True)
        self.signed('AC2', event=event, cleanup=True)


class TestAC3(Fixture):
    def test_verification_and_guard_failures(self):
        for guard in (False, True):
            with self.subTest(guard=guard):
                self.spec.write_text('Writes:\n- feature.py\n\n## Verification\n```bash\n' +
                                     ('true' if guard else '/usr/bin/python3 -c "assert False"') + '\n```\n')
                with patch('branch_refresh.guard_commands', return_value=[('guard-fixture', 'false' if guard else 'true')]):
                    r = self.run_refresh()
                self.assertEqual(r['state'], 'checks-failed')
                event = next(e for e in reversed(self.read()) if e['type'] == 'conflict-rework')
                self.assertEqual(event['type'], 'conflict-rework')
                self.assertTrue(event['failing_ids'])
                self.assertIn('guard-fixture' if guard else 'verification-step-1', r['failing_ids'])
                self.assertEqual(sum(e['type'] == 'conflict-rework' for e in self.read()), 1)
                self.assertEqual(self.git('cat-file', '-t', event['rebased_sha']), 'commit')
                self.assertFalse(any(e['type'] == 'branch-refreshed' for e in self.read()))
                self.invariant(True)
                self.signed('AC3-guard' if guard else 'AC3-verification', event=event, resolvable=True)


class TestAC4(Fixture):
    def test_refusal_routing_and_packet_authority(self):
        import dispatch_builder as db
        import attempts
        runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec)
        a = SimpleNamespace(project='do-it-v2', subject=self.sid, role='builder', pinned_base_sha=self.old)
        for reason in br.REFUSALS:
            self.assertTrue(attempts._refusal(dict(type='spawn-failed', role='builder', reason=reason)))
            with patch('branch_refresh.refresh', return_value={'state': 'refreshed'}) as refresh:
                br.before_refusal(runtime, a, reason)
                refresh.assert_called_once()
        a.effective_base_sha = self.main
        with patch('packet._effective_base', return_value=self.main):
            for reason in sorted(br.REFUSALS):
                self.git('reset', '--hard', self.start, cwd=self.tree)
                outcome = br.before_refusal(runtime, a, reason)
                self.assertEqual(outcome['state'], 'refreshed')
                self.assertEqual(a.pinned_base_sha, self.old)
                db.branch_preflight(runtime, a, self.tree, self.events, lambda *a, **k: self.fail('unexpected refusal'))
        class Refused(Exception):
            pass
        def fail(message, **kw):
            raise Refused(kw['reason'])
        with patch('packet._effective_base', return_value=self.main), patch('branch_refresh.before_refusal') as hook:
            with self.assertRaisesRegex(Refused, 'stale-base'):
                db.effective_base(runtime, a, fail)
            hook.assert_called_once()
            self.assertEqual(a.pinned_base_sha, self.old)
            a.pinned_base_sha = self.main
            self.assertEqual(db.effective_base(runtime, a, fail), self.main)
        self.git('reset', '--hard', self.start, cwd=self.tree)
        with patch('branch_refresh.before_refusal') as before, \
             patch('branch_refresh.refresh') as refresh, \
             patch('branch_refresh.recut', return_value={'state': 'recut'}) as recut:
            with self.assertRaisesRegex(Refused, 'branch-recut'):
                db.branch_preflight(runtime, a, self.tree, self.events, fail)
            before.assert_not_called()
            refresh.assert_not_called()
            self.assertIsInstance(recut.call_args.kwargs['now'], datetime)
        runtime.pending_conflict_rework = lambda *_: True
        with patch('branch_refresh.before_refusal', return_value={'state': 'conflict'}), \
             patch('branch_refresh.recut', return_value={'state': 'conflict'}):
            db.branch_preflight(runtime, a, self.tree, self.events, fail)
        self.signed('AC4', reasons=sorted(br.REFUSALS), pin_preserved=True, current_packet_admitted=True,
                    no_builder_on_refresh=True, authorized_recovery_preserved=True)


class TestAC5(Fixture):
    def test_refusal_inventory_and_persistent_limit(self):
        for path in ('api/alembic_supabase/versions/new.py', 'genesis_schema_fingerprint.txt',
                     'api/alembic_supabase/branch_isolation.json', '.secrets.baseline'):
            self.spec.write_text('Writes:\n- ' + path + '\n')
            self.assertEqual(self.run_refresh()['reason'], 'migration-or-fingerprint')
        self.spec.write_text('Writes:\n- feature.py\n\n## Verification\n```bash\nfalse\n```\n')
        (self.tree / 'dirty').write_text('preserve')
        self.assertEqual(self.run_refresh()['reason'], 'uncommitted-state')
        (self.tree / 'dirty').unlink()
        self.events.append(dict(type='spawn-started', subject=self.sid, role='builder', spawn='L-builder-9999', ts=self.now.isoformat()))
        self.assertEqual(self.run_refresh()['reason'], 'open-builder-dispatch')
        self.events.pop()
        for _ in range(3):
            self.assertEqual(self.run_refresh()['state'], 'checks-failed')
        self.assertEqual(self.run_refresh()['reason'], 'daily-limit')
        self.now += timedelta(days=1)
        self.assertEqual(self.run_refresh()['state'], 'checks-failed')
        self.cfg['merge_queue']['branch_refresh_per_day'] = 1
        self.assertEqual(self.run_refresh()['reason'], 'daily-limit')
        (self.tree / 'second').write_text('second')
        self.commit('second', self.tree)
        self.assertEqual(self.run_refresh()['reason'], 'multiple-commits')
        self.signed('AC5', events=self.read(), fresh_accounting=True, default=3, configured=1)


class TestAC6(Fixture):
    def test_holding_worktree_synchronized(self):
        r = self.run_refresh()
        self.assertEqual(r['state'], 'refreshed')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.tree), self.git('rev-parse', self.branch))
        self.assertEqual(self.git('status', '--porcelain', cwd=self.tree), '')
        self.invariant()
        self.signed('AC6', clean_idle_reset=True, main_head_index_unchanged=True, cleanup=True)


class TestAC7(Fixture):
    def test_presentation_and_utc_day(self):
        import look_omens
        event = dict(type='branch-refreshed', subject=self.sid, base_sha=self.main, ts=self.now.isoformat())
        done = dict(type='build-done', subject=self.sid, ts=(self.now - timedelta(hours=1)).isoformat())
        self.assertIn('refreshed onto ' + self.main, br.refresh_note([done, event], self.sid))
        self.assertEqual(br.refresh_note([event, dict(done, ts=(self.now + timedelta(hours=1)).isoformat())], self.sid), '')
        self.assertEqual(br.refresh_note([done], self.sid), '')
        yesterday = dict(event, ts='2026-11-03T00:30:00+01:00')
        runner = SimpleNamespace(clock=lambda: 0, git=lambda *args: '0')
        with patch('autodispatch.candidates', return_value=[]):
            rows = look_omens.run([event, yesterday], {}, runner, self.now, {})
        row = next(r for r in rows if r['rule'] == 'branch-refreshes')
        self.assertIn('1 branch refresh', row['detail'])
        import nextstep
        step = nextstep.Step(item=self.sid, item_kind='spec', stage='checking', state='hold', action=None,
                             owner='executor', trigger=None, reason='fixture', clears='fixture', since='', attempt=0, retry_at=None)
        captured = io.StringIO()
        with patch('ledger_files.assert_readable'), patch('fold.read_events', return_value=[done, event]), \
             patch('fold.fold', return_value=({}, {}, {}, {})), \
             patch('nextstep.next_steps', return_value=[step]), patch('nextstep.flow_counts', return_value={}), \
             contextlib.redirect_stdout(captured):
            self.assertEqual(nextstep.main([]), 0)
        self.assertIn(br.refresh_note([done, event], self.sid), captured.getvalue())
        self.signed('AC7', note=captured.getvalue(), count=row)


class RecutFixture(Fixture):
    def setUp(self):
        super().setUp()
        (self.tree / 'second.txt').write_text('second\n')
        self.commit('second feature', self.tree)
        self.start = self.git('rev-parse', self.branch)
        self.spec.write_text(self.spec.read_text().replace('- feature.py', '- feature.py\n- second.txt\n- side.txt'))

    def run_recut(self):
        return br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)

    def snapshots(self):
        return dict(branch=self.git('rev-parse', self.branch), head=self.git('rev-parse', 'HEAD', cwd=self.tree),
                    status=self.git('status', '--porcelain', cwd=self.tree),
                    diff=self.git('diff', 'HEAD', cwd=self.tree), tags=self.git('show-ref', '--tags')
                    if self.git('tag', '--list') else '', worktrees=self.git('worktree', 'list', '--porcelain'))

    def refused(self, expected):
        before = self.snapshots()
        result = self.run_recut()
        self.assertEqual(result['state'], expected)
        self.assertEqual(self.snapshots(), before)
        self.invariant(True)
        self.signed('recut-' + self._testMethodName, before=before, after=self.snapshots(),
                    result=result, event=self.read()[-1], cleanup=True)
        return result


class TestRecut(RecutFixture):
    def test_clean_two_commit_dispatch_and_fresh_ledger(self):
        import build_runner
        import dispatch_builder
        self.bundle_patch.stop()
        (self.root / 'events').mkdir(exist_ok=True)
        event = dict(self.events[0], v=1, ready_sha=self.start, base_sha=self.old)
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(event) + '\n')
        (self.root / 'runner').mkdir()
        (self.root / 'runner-backing').mkdir()
        runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec,
                                  pending_conflict_rework=lambda *_: False)
        a = SimpleNamespace(project='do-it-v2', subject=self.sid, role='builder', effective_base_sha=self.main)
        def fail(message, **facts):
            raise RuntimeError(facts['reason'])
        with patch('branch_refresh.git', wraps=br.git) as trace, \
             patch('packet_base._effective_branch', return_value='main'), \
             patch('grading_env._project_checkout', return_value=self.repo), \
             patch('build_runner.SHORT_TMP_ROOT', self.root), \
             patch('build_runner.SHORT_TMP_BACKING_ROOT', self.root / 'runner-backing'), \
             patch('scratch.sub', side_effect=lambda name: self.root / name), \
             patch('build_runner.run', wraps=build_runner.run) as bundle, \
             patch('branch_refresh.regate', side_effect=self.real_gate) as gate:
            with self.assertRaisesRegex(RuntimeError, 'branch-recut'):
                dispatch_builder.branch_preflight(runtime, a, self.tree, self.events, fail)
        bundle.assert_called_once()
        gate.assert_called_once()
        scratch_adds = [call.args for call in trace.call_args_list if 'worktree' in call.args and 'add' in call.args]
        self.assertTrue(any(str(args[-2]).startswith('/var/tmp/branch-recut-') for args in scratch_adds))
        rows = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json, fold; print(json.dumps(fold.read_events(use_cache=False)))'],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())), text=True))
        recut = next(e for e in rows if e['type'] == 'branch-recut')
        after = self.git('rev-parse', self.branch)
        self.assertEqual((recut['spec'], recut['from_sha'], recut['to_sha'], recut['tag'], recut['commit_count']),
                         (self.sid, self.start, after, 'rework/9774-prev', 2))
        self.assertEqual(recut['actor'], 'executor')
        self.assertEqual(self.git('rev-parse', recut['tag']), self.start)
        self.assertEqual(self.git('merge-base', 'main', after), self.main)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.tree), after)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.tree), '')
        self.assertEqual((self.tree / 'feature.py').read_text(), 'feature\n')
        self.assertEqual((self.tree / 'second.txt').read_text(), 'second\n')
        self.assertTrue(any(e['type'] == 'merge-gate-clean' for e in rows))
        self.assertFalse(any(e['type'] == 'conflict-rework' for e in rows))
        self.invariant()
        self.signed('recut-clean-two-commit-routing-fresh-ledger', event=recut, snapshots=self.snapshots(),
                    bundle=True, fresh_gate=True, cleanup=True, main_unchanged=True)

    def test_merge_empty_and_occupied_tags(self):
        self.git('switch', '-qc', 'side', cwd=self.tree)
        (self.tree / 'side.txt').write_text('side\n')
        self.commit('side feature', self.tree)
        self.git('switch', self.branch, cwd=self.tree)
        self.git('merge', '--no-ff', '-qm', 'merge side', 'side', cwd=self.tree)
        self.git('commit', '--allow-empty', '-qm', 'empty', cwd=self.tree)
        self.start = self.git('rev-parse', self.branch)
        for tag in ('rework/9774-prev', 'rework/9774-prev-2'):
            self.git('tag', tag, self.old)
        result = self.run_recut()
        self.assertEqual(result['state'], 'recut')
        self.assertEqual(result['commit_count'], 3)
        self.assertEqual(result['tag'], 'rework/9774-prev-3')
        self.assertEqual((result['skipped_merges'], result['dropped_empty']), (1, 1))
        for tag in ('rework/9774-prev', 'rework/9774-prev-2'):
            self.assertEqual(self.git('rev-parse', tag), self.old)
        self.assertEqual(self.git('rev-parse', result['tag']), self.start)
        self.assertEqual(self.git('rev-list', '--count', '--merges', 'main..' + self.branch), '0')
        self.assertEqual((self.tree / 'side.txt').read_text(), 'side\n')
        self.invariant()
        self.signed('recut-merge-empty-occupied-tags', result=result, snapshots=self.snapshots(), cleanup=True)

    def test_patch_already_on_main_is_dropped(self):
        self.git('cherry-pick', self.start)
        self.main = self.git('rev-parse', 'HEAD')
        self.snapshot = self.git('rev-parse', 'HEAD', 'HEAD^{tree}')
        result = self.run_recut()
        self.assertEqual((result['state'], result['commit_count'], result['dropped_empty']), ('recut', 1, 1))
        self.assertEqual((self.tree / 'second.txt').read_text(), 'second\n')
        self.invariant()
        self.signed('recut-redundant-patch', result=result, cleanup=True)

    def test_verification_and_guard_failures(self):
        for guard in (False, True):
            with self.subTest(guard=guard):
                self.spec.write_text('Writes:\n- feature.py\n- second.txt\n\n## Verification\n```bash\n' +
                                     ('true' if guard else 'false') + '\n```\n')
                with patch('branch_refresh.guard_commands', return_value=[('recut-guard', 'false' if guard else 'true')]):
                    result = self.refused('checks-failed')
                self.assertIn('recut-guard' if guard else 'verification-step-1', result['failing_ids'])

    def test_fresh_gate_failure_restores_old_tip_and_tag_set(self):
        before = self.snapshots()
        with patch('branch_refresh.regate', return_value=1):
            result = self.run_recut()
        self.assertIn('merge-gate', result['failing_ids'])
        after = self.snapshots()
        self.assertEqual(self.git('rev-parse', 'rework/9774-prev'), self.start)
        self.assertEqual({k: v for k, v in after.items() if k != 'tags'},
                         {k: v for k, v in before.items() if k != 'tags'})
        self.invariant(True)

    def test_no_fallback_in_other_refresh_callers(self):
        runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec)
        a = SimpleNamespace(project='do-it-v2', subject=self.sid, role='builder')
        with patch('packet._effective_base', return_value=self.main), patch('branch_refresh.recut') as recut:
            for reason in ('stale-base', 'pinned-base-mismatch'):
                br.before_refusal(runtime, a, reason)
            br.gate_refresh(self.repo, self.branch, 'main', self.sid)
            br.dispatch_preflight(runtime, a, self.tree, [], self.fail)
            recut.assert_not_called()


class TestRecutConflict(RecutFixture):
    def test_conflict_preserves_state_and_names_paths_in_recovery(self):
        (self.tree / 'base').write_text('feature conflict\n')
        self.commit('conflicting feature', self.tree)
        self.start = self.git('rev-parse', self.branch)
        self.git('tag', 'rework/9774-prev', self.old)
        result = self.refused('conflict')
        self.assertEqual(result['paths'], ['base'])
        event = self.read()[-1]
        self.assertEqual((event['type'], event['paths']), ('conflict-rework', ['base']))


class TestRecutDirty(RecutFixture):
    def test_tracked_edits(self):
        (self.tree / 'second.txt').write_text('preserve tracked\n')
        self.assertEqual(self.refused('skipped')['reason'], 'uncommitted-state')
        self.assertEqual((self.tree / 'second.txt').read_text(), 'preserve tracked\n')

    def test_untracked_files(self):
        (self.tree / 'untracked').write_text('preserve untracked\n')
        self.assertEqual(self.refused('skipped')['reason'], 'uncommitted-state')
        self.assertEqual((self.tree / 'untracked').read_text(), 'preserve untracked\n')

    def test_existing_operations(self):
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
            with self.subTest(operation=name):
                path = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', name, cwd=self.tree))
                if name.startswith('rebase-'):
                    path.mkdir()
                else:
                    path.write_text(self.start + '\n')
                self.assertEqual(self.refused('skipped')['reason'], 'uncommitted-state')
                self.assertTrue(path.exists())
                self.signed('recut-existing-operation', operation=name, preserved=True, snapshots=self.snapshots())
                if path.is_dir():
                    path.rmdir()
                else:
                    self.assertEqual(path.read_text(), self.start + '\n')
                    path.unlink()

    def test_dirtiness_appearing_during_checks(self):
        check_tree = br.check_tree
        def checks(*args):
            result = check_tree(*args)
            (self.tree / 'second.txt').write_text('arrived during checks\n')
            return result
        with patch('branch_refresh.check_tree', side_effect=checks):
            result = self.run_recut()
        self.assertEqual((result['state'], result['reason']), ('skipped', 'adoption-state-changed'))
        self.assertEqual((self.tree / 'second.txt').read_text(), 'arrived during checks\n')
        self.assertEqual(self.git('tag', '--list'), '')
        self.invariant(True)
        self.assertFalse(any(e['type'] == 'conflict-rework' for e in self.read()))
        self.signed('recut-post-check-dirty', result=result, snapshots=self.snapshots(), cleanup=True)


class TestAC8(unittest.TestCase):
    def test_structure_and_admission(self):
        import fold_emits
        repo = Path(__file__).resolve().parent.parent
        rows = {}
        for name in ('merge_gate', 'dispatch_builder', 'attempts'):
            introduced = subprocess.check_output(['git', 'log', '--diff-filter=A', '--format=%H', '--',
                                                   'src/branch_refresh.py'], cwd=repo, text=True).splitlines()
            self.assertTrue(introduced, 'branch_refresh.py must have an introducing commit')
            diff = subprocess.check_output(['git', 'diff', '--numstat', introduced[0] + '^',
                                            introduced[0], '--', 'src/' + name + '.py'], cwd=repo, text=True).split()
            added, removed = map(int, diff[:2]) if diff else (0, 0)
            self.assertLessEqual(added, 20)
            rows[name] = dict(added=added, net=added-removed)
        for kind in ('branch-refreshed', 'branch-refresh-skipped'):
            self.assertEqual(fold_emits.EMITS[kind], {'executor'})
        self.assertIn('branch-refresh', (repo / 'docs/DESIGN.md').read_text())
        print(json.dumps(dict(url='fixture://branch-refresh/AC8', status=0, body_excerpt=rows,
                             body_sha256=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())))


if __name__ == '__main__':
    unittest.main()


class TestReworkPendingPreflight(unittest.TestCase):
    """A rework newer than the last DONE hand-in is never swallowed by a recut (Thinker 2026-11-10)."""
    SID = 'L-spec-9790'

    def rows(self, *items):
        return [dict(subject=self.SID, ts=ts, type=t, **extra) for ts, t, extra in items]

    def test_rework_pending_ordering(self):
        done = ('2026-11-10T10:00:00+00:00', 'build-done', dict(status='DONE'))
        rework = ('2026-11-10T11:00:00Z', 'decision', dict(rework='yes'))
        regrade = ('2026-11-10T12:00:00+00:00', 'decision', dict(regrade='yes'))
        later_done = ('2026-11-10T13:00:00+00:00', 'build-done', dict(status='DONE'))
        self.assertTrue(br.rework_pending(self.rows(done, rework), self.SID))
        self.assertTrue(br.rework_pending(self.rows(done, rework, regrade), self.SID))
        self.assertFalse(br.rework_pending(self.rows(done, rework, later_done), self.SID))
        self.assertFalse(br.rework_pending(self.rows(done, regrade), self.SID))
        self.assertFalse(br.rework_pending(self.rows(rework)[:0], self.SID))
        other = [dict(r, subject='L-spec-1') for r in self.rows(done, rework)]
        self.assertFalse(br.rework_pending(other, self.SID))

    def test_preflight_dispatches_builder_instead_of_recut(self):
        events = self.rows(('2026-11-10T10:00:00+00:00', 'build-done', dict(status='DONE')),
                           ('2026-11-10T11:00:00+00:00', 'decision', dict(rework='yes')))
        runtime = SimpleNamespace(ROOT=Path('/nonexistent'), spec_path=lambda _: Path('/nonexistent'))
        a = SimpleNamespace(project='do-it-v2', subject=self.SID, role='builder', effective_base_sha='0' * 40)
        failures = []
        with patch('branch_refresh.recut') as recut, patch('branch_refresh.git') as git:
            self.assertFalse(br.dispatch_preflight(runtime, a, Path('/nonexistent'), events,
                                                   lambda *x, **k: failures.append((x, k))))
        recut.assert_not_called()
        git.assert_not_called()
        self.assertEqual(failures, [])
