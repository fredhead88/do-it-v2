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
        temp = tempfile.TemporaryDirectory(prefix='branch-refresh-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
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
                event = self.read()[-1]
                self.assertEqual(event['type'], 'conflict-rework')
                self.assertTrue(event['failing_ids'])
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
        with patch('branch_refresh.before_refusal', return_value={'state': 'refreshed'}):
            with self.assertRaisesRegex(Refused, 'branch-refreshed'):
                db.branch_preflight(runtime, a, self.tree, self.events, fail)
        runtime.pending_conflict_rework = lambda *_: True
        with patch('branch_refresh.before_refusal', return_value={'state': 'conflict'}):
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
        self.events.append(dict(type='spawn-started', subject=self.sid, role='builder', spawn='L-builder-9999'))
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


class TestAC8(unittest.TestCase):
    def test_structure_and_admission(self):
        import fold_emits
        repo = Path(__file__).resolve().parent.parent
        rows = {}
        for name in ('merge_gate', 'dispatch_builder', 'attempts'):
            introduced = subprocess.check_output(['git', 'log', '--diff-filter=A', '--format=%H', '--',
                                                   'src/branch_refresh.py'], cwd=repo, text=True).splitlines()
            base = os.environ.get('BRANCH_REFRESH_TEST_BASE') or (introduced[0] + '^' if introduced else 'HEAD')
            diff = subprocess.check_output(['git', 'diff', '--numstat', base, '--', 'src/' + name + '.py'], cwd=repo, text=True).split()
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
