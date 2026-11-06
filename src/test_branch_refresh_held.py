#!/usr/bin/env python3
"""Pure ledger refusal-reader regressions, with external state forbidden."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import branch_refresh as br
from test_branch_refresh import Fixture
import fold_emits
import fold
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


class PinFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.verify = self.root / 'content' / ('verify-' + self.sid + '-builder.sh')
        self.absent = self.root / 'content' / ('verify-' + self.sid + '-grader.sh')
        self.spec.write_text('---\nbase_sha: ' + self.old + '\n---\nBase: ' + self.old[:10] + '\n' +
            'Unrelated: ' + self.start + '\nWrites:\n- feature.py\n\n## Verification\n```bash\n' +
            '/usr/bin/python3 -c "assert True"\n```\n')
        self.verify.write_text('#!/bin/bash\n# Base: ' + self.old[:12] + '\n# literal ' + self.old + '\n')
        self.verify.chmod(0o751)
        self.spec.chmod(0o640)
        self.images = [(p, p.read_bytes(), p.stat().st_mode) for p in (self.spec, self.verify)]

    def recut(self):
        return br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)

    def original_artifacts(self):
        for path, data, mode in self.images:
            self.assertEqual((path.read_bytes(), path.stat().st_mode), (data, mode))
        self.assertFalse(self.absent.exists())


class TestRecutPins(PinFixture):
    def test_one_commit_dispatch_pins_and_real_bundle(self):
        import build_runner
        import evidence_revision
        import sweep_owed
        self.bundle_patch.stop()
        (self.root / 'events').mkdir(exist_ok=True)
        build = dict(self.events[0], v=1, ready_sha=self.start, base_sha=self.old)
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(build) + '\n')
        with patch.dict(os.environ, DOIT_LEDGER_FILE='L-tick-local.jsonl'):
            fold.append(['autodispatched', self.sid, 'role=builder', 'spawn=123',
                         'project=do-it-v2', 'since=' + self.now.isoformat()], strict=True)
        (self.root / 'runner').mkdir()
        (self.root / 'runner-backing').mkdir()
        check = br.check_tree
        def checks(*args):
            self.assertIn(self.main, args[-1])
            self.assertNotIn(self.old, args[-1])
            self.assertEqual(self.git('rev-parse', self.branch), self.start)
            return check(*args)
        runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec,
                                  pending_conflict_rework=lambda *_: False)
        args = SimpleNamespace(project='do-it-v2', subject=self.sid, effective_base_sha=self.main)
        def fail(message, **fields):
            raise RuntimeError(fields['reason'])
        with patch('branch_refresh.before_refusal') as before, patch('branch_refresh.refresh') as refresh, \
             patch('packet_base._effective_branch', return_value='main'), \
             patch('grading_env._project_checkout', return_value=self.repo), \
             patch('build_runner.SHORT_TMP_ROOT', self.root), \
             patch('build_runner.SHORT_TMP_BACKING_ROOT', self.root / 'runner-backing'), \
             patch('scratch.sub', side_effect=lambda name: self.root / name), \
             patch('branch_refresh.check_tree', side_effect=checks), \
             patch('branch_refresh.regate', side_effect=self.real_gate), \
             patch('branch_refresh.git', wraps=br.git) as commands:
            with self.assertRaisesRegex(RuntimeError, 'branch-recut'):
                br.dispatch_preflight(runtime, args, self.tree, self.events, fail)
        before.assert_not_called()
        refresh.assert_not_called()
        rows = self.read()
        receipt = next(e for e in rows if e['type'] == 'branch-recut')
        bundle_event = next(e for e in rows if e['type'] == 'evidence-bundle')
        bundle = json.loads(Path(bundle_event['path']).read_text())
        digest = evidence_revision.digest(self.spec.read_text())
        self.assertEqual(bundle['spec_sha256'], digest)
        self.assertEqual(sweep_owed._spec_hash(dict(spec=self.sid, spec_path=str(self.spec))), digest)
        self.assertEqual((receipt['old_base_sha'], receipt['base_sha']), (self.old, self.main))
        self.assertEqual(self.git('rev-parse', receipt['tag']), self.start)
        trace = [c.args[1:] for c in commands.call_args_list]
        tag_index = next(i for i, c in enumerate(trace) if c[:1] == ('update-ref',) and c[1].startswith('refs/tags/'))
        adopt_index = next(i for i, c in enumerate(trace) if c[:2] == ('reset', '--hard'))
        self.assertLess(tag_index, adopt_index)
        self.assertIn('Unrelated: ' + self.start, self.spec.read_text())
        self.assertEqual(self.verify.read_text().count(self.main), 2)
        self.assertEqual(self.verify.stat().st_mode & 0o777, 0o751)
        self.assertFalse(self.absent.exists())
        runtime.pending_conflict_rework = lambda *_: True
        self.assertTrue(br.dispatch_preflight(runtime, args, self.tree, rows, fail))
        self.assertEqual(self.git('status', '--porcelain', cwd=self.tree), '')
        self.invariant()
        self.signed('AC2', receipt=receipt, spec_sha256=digest, bundle_spec_sha256=bundle['spec_sha256'],
                    old_tag_reachable=True, checks_before_adoption=True, current_branch_admitted=True)

    def test_zero_count_receipt(self):
        self.git('cherry-pick', self.start)
        self.main = self.git('rev-parse', 'HEAD')
        result = self.recut()
        self.assertEqual(result['state'], 'recut')
        row = next(e for e in self.read() if e['type'] == 'branch-recut')
        for key in ('commit_count', 'skipped_merges', 'dropped_empty'):
            self.assertIn(key, row)
        self.assertEqual(row['commit_count'], 0)
        self.assertEqual(row['skipped_merges'], 0)
        self.signed('AC4-zero-count', receipt=row)


class TestRecutRollback(PinFixture):
    def test_failed_gate_restores_images_retains_tag(self):
        with patch('branch_refresh.regate', return_value=1):
            result = self.recut()
        self.assertEqual(result['failing_ids'], ['merge-gate'])
        self.original_artifacts()
        self.assertEqual(self.git('rev-parse', self.branch), self.start)
        self.assertEqual(self.git('rev-parse', 'rework/9774-prev'), self.start)
        self.invariant(True)
        self.signed('AC3-gate', artifacts_restored=True, modes_restored=True, retained_tag=True)

    def test_check_failure_and_undetermined_restore(self):
        for outcome in ('failed', 'undetermined'):
            with self.subTest(outcome=outcome):
                with patch('branch_refresh.check_tree', **(
                        dict(return_value=(['fixture'], [])) if outcome == 'failed' else
                        dict(side_effect=RuntimeError('fixture unavailable')))):
                    self.assertEqual(self.recut()['state'], 'checks-failed')
                self.original_artifacts()
                self.invariant(True)
                self.assertEqual(self.git('tag', '--list'), '')
                self.signed('AC3-' + outcome, artifacts_restored=True, branch_unchanged=True)

    def test_adoption_races_preserve_competitors(self):
        check = br.check_tree
        for race in ('main', 'tip', 'builder', 'dirty', 'artifact'):
            with self.subTest(race=race):
                # Each race has its own disposable Fixture, without reusing moved refs.
                case = PinFixture()
                case.setUp()
                try:
                    def checks(*args):
                        result = check(*args)
                        if race in ('main', 'tip'):
                            target = case.repo if race == 'main' else case.tree
                            (target / 'competitor').write_text(race)
                            case.commit('concurrent work', target)
                        elif race == 'builder':
                            br.emit('build-started', case.sid, spawn='L-builder-race', ts=case.now.isoformat())
                        elif race == 'dirty':
                            (case.tree / 'competitor').write_text('dirty')
                        else:
                            case.verify.write_text('concurrent artifact')
                        return result
                    with patch('branch_refresh.check_tree', side_effect=checks):
                        result = case.recut()
                    self.assertEqual(result.get('reason'), 'adoption-state-changed')
                    if race == 'artifact':
                        self.assertEqual(case.verify.read_text(), 'concurrent artifact')
                        self.assertEqual(case.spec.read_bytes(), case.images[0][1])
                    else:
                        case.original_artifacts()
                    if race in ('main', 'tip'):
                        self.assertEqual(((case.repo if race == 'main' else case.tree) / 'competitor').read_text(), race)
                    if race == 'dirty':
                        self.assertEqual((case.tree / 'competitor').read_text(), 'dirty')
                    self.signed('AC3-race-' + race, preserved=True, result=result)
                finally:
                    case.doCleanups()

    def test_failed_gate_does_not_overwrite_new_builder_or_artifact(self):
        def gate(*args):
            br.emit('build-started', self.sid, spawn='L-builder-race', ts=self.now.isoformat())
            self.verify.write_text('competing pin edit')
            return 1
        with patch('branch_refresh.regate', side_effect=gate):
            self.recut()
        self.assertNotEqual(self.git('rev-parse', self.branch), self.start)
        self.assertEqual(self.verify.read_text(), 'competing pin edit')
        self.assertIn(self.main, self.spec.read_text())
        self.assertEqual(self.git('rev-parse', 'rework/9774-prev'), self.start)
        self.signed('AC3-gate-race', new_builder_preserved=True, artifact_preserved=True)


class TestRecutRefusals(Fixture):
    def recut(self):
        return br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)

    def test_standing_inventory_real_emitter(self):
        reasons = ('migration-or-fingerprint', 'uncommitted-state', 'conflict', 'checks-failed',
                   'merge-gate', 'cherry-pick-undetermined', 'checks-undetermined', 'invalid-branch',
                   'main-branch', 'invalid-daily-limit', 'integrator-holds-branch')
        for reason in reasons:
            with self.subTest(reason=reason):
                for _ in range(2):
                    br.recovery_receipt(self.sid, self.branch, self.read(), 'do-it-v2', reason,
                                        self.start, self.now, paths=[], failing_ids=['fixture'])
                rows = [e for e in self.read() if e.get('reason') == reason]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['owner'], 'thinker')
                self.assertIsNone(rows[0]['retry_at'])
                rework = [e for e in self.read() if e.get('recut_reason') == reason]
                self.assertEqual(len(rework), int(reason in br.REWORK_REASONS))
                for row in rework:
                    self.assertNotIn('reason', row)
                    self.assertEqual(row['tip'], self.start)
                    self.assertTrue(row['attempt'])
                    self.assertTrue(row['verdict'])
        rows = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json, fold; print(json.dumps(fold.read_events(use_cache=False)))'],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())), text=True))
        self.assertEqual(len([r for r in rows if r['type'] == 'branch-recut-refused']), 11)
        br.recovery_receipt(self.sid, self.branch, self.read(), 'do-it-v2', 'conflict',
                            self.start, self.now, paths=[])
        self.assertEqual(len(self.read()), len(rows))
        br.emit('build-done', self.sid, status='DONE', ts=self.now.isoformat())
        br.recovery_receipt(self.sid, self.branch, self.read(), 'do-it-v2', 'conflict',
                            self.start, self.now, paths=[])
        self.assertEqual(sum(e.get('reason') == 'conflict' for e in self.read()), 2)
        self.assertEqual(sum(e.get('recut_reason') == 'conflict' for e in self.read()), 2)
        self.signed('AC4-inventory', reasons=list(reasons), fresh_process=True, rows=rows)

    def test_shared_conflict_and_checks_rework(self):
        for reason in ('conflict', 'checks-failed'):
            case = Fixture()
            case.setUp()
            try:
                if reason == 'conflict':
                    (case.tree / 'base').write_text('conflict')
                    case.git('add', '.', cwd=case.tree)
                    case.git('commit', '--amend', '-qm', 'conflict', cwd=case.tree)
                    case.start = case.git('rev-parse', case.branch)
                else:
                    case.spec.write_text(case.spec.read_text().replace('assert True', 'assert False'))
                for _ in range(2):
                    self.assertEqual(case.run_refresh()['state'], reason)
                result = br.recut(case.repo, case.branch, 'main', case.sid, now=case.now)
                self.assertEqual(result['state'], reason)
                rows = case.read()
                reworks = [r for r in rows if r['type'] == 'conflict-rework']
                self.assertEqual(len(reworks), 1)
                self.assertNotIn('reason', reworks[0])
                self.assertEqual(reworks[0]['recut_reason'], reason)
                self.assertEqual(br.attempt_count(rows, case.sid, case.now), 3)
                self.assertEqual(br.recut(case.repo, case.branch, 'main', case.sid, now=case.now)['reason'], 'daily-limit')
                case.invariant(True)
                self.signed('AC4-shared-' + reason, reworks=reworks, shared_attempts=3)
            finally:
                case.doCleanups()

    def test_daily_midnight_fresh_process_and_invalid_limit(self):
        self.now = datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc)
        self.spec.write_text(self.spec.read_text().replace('assert True', 'assert False'))
        self.assertEqual(self.run_refresh()['state'], 'checks-failed')
        self.assertEqual(self.recut()['state'], 'checks-failed')
        script = ('import branch_refresh as br; from datetime import datetime; import sys; '
                  'br.config=lambda: {}; '
                  'br.guard_commands=lambda *args: []; '
                  'import grading_env; grading_env._project_row=lambda project: {}; '
                  'print(br.recut(sys.argv[1], sys.argv[2], "main", sys.argv[3], '
                  'now=datetime.fromisoformat(sys.argv[4]))["state"])')
        # The existing production verification text is a local false command; no provider.
        result = subprocess.check_output([sys.executable, '-c', script, str(self.repo), self.branch,
            self.sid, self.now.isoformat()], env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())), text=True)
        self.assertIn('checks-failed', result)
        self.assertEqual(self.recut()['reason'], 'daily-limit')
        hold = br.recut_refusal(self.read(), self.sid, self.now)
        self.assertEqual(hold['retry_at'], '2026-11-07T00:00:00Z')
        self.now += timedelta(seconds=1)
        self.assertIsNone(br.recut_refusal(self.read(), self.sid, self.now))
        self.assertEqual(self.recut()['state'], 'checks-failed')
        self.cfg['merge_queue']['branch_refresh_per_day'] = True
        self.assertEqual(self.recut()['reason'], 'invalid-daily-limit')
        self.assertEqual(self.run_refresh()['reason'], 'invalid-daily-limit')
        self.signed('AC4-midnight', retry_at=hold['retry_at'], new_day_attempt=True, persisted_count=3)

    def test_transients_and_completed_conflicting_dispatch(self):
        br.emit('build-started', self.sid, spawn='L-builder-test', ts=self.now.isoformat())
        self.assertEqual(self.recut()['reason'], 'open-builder-dispatch')
        self.assertFalse(any(e['type'] in {'branch-recut-refused', 'conflict-rework'} for e in self.read()))
        br.emit('build-done', self.sid, status='DONE', ts=self.now.isoformat())
        (self.tree / 'base').write_text('conflict')
        self.git('add', '.', cwd=self.tree)
        self.git('commit', '--amend', '-qm', 'conflicting feature', cwd=self.tree)
        runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec, pending_conflict_rework=lambda *_: True)
        args = SimpleNamespace(project='do-it-v2', subject=self.sid, effective_base_sha=self.main)
        with patch('branch_refresh.recut', wraps=br.recut) as recut, \
             patch('branch_refresh.refresh') as refresh, \
             patch('packet_base._effective_branch', return_value='main'):
            self.assertFalse(br.dispatch_preflight(runtime, args, self.tree, self.events, self.fail))
        recut.assert_called_once()
        refresh.assert_not_called()
        rows = self.read()
        self.assertEqual(sum(e.get('reason') == 'attempt-started' for e in rows), 1)
        self.assertEqual(sum(e['type'] == 'conflict-rework' for e in rows), 1)
        self.signed('AC4-completed-conflict', reservation_count=1, rework_count=1, refresh_calls=0)

    def test_nullable_receipt_and_committed_readback(self):
        self.assertEqual(fold_emits.REQUIRED_ROWS['branch-recut-refused'],
                         ('reason', 'owner', 'project'))
        result = br.recut(self.repo, 'missing', 'main', self.sid, now=self.now)
        self.assertEqual(result['reason'], 'invalid-branch')
        row = self.read()[-1]
        self.assertEqual(row['type'], 'branch-recut-refused')
        self.assertIsNone(row['tip'])
        self.assertIsNone(row['retry_at'])
        self.assertEqual(row['owner'], 'thinker')
        br.recut(self.repo, 'missing', 'main', self.sid, now=self.now)
        self.assertEqual(len(self.read()), 1)
        self.signed('AC4', receipt=row, duplicate_count=0)


class TestRecutRefusalReader(unittest.TestCase):
    def test_ledger_order_and_midnight(self):
        now = datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc)
        sid = 'L-spec-9774'
        def row(kind, **fields):
            return dict(type=kind, subject=sid, ts=now.isoformat(), **fields)
        refusal = row('branch-recut-refused', reason='conflict', tip='fixture-tip', retry_at=None)
        expected = {key: refusal[key] for key in ('reason', 'tip', 'retry_at')}
        cases = [('empty', [], None), ('standing', [refusal], expected)]
        invalidators = ('branch-recut', 'build-started', 'build-done', 'spec-written', 'decision', 'unblocked')
        for kind in invalidators:
            invalidator = row(kind)
            cases.extend([
                (kind+'-after', [refusal, invalidator], None),
                (kind+'-before', [invalidator, refusal], expected),
                (kind+'-other-subject', [refusal, dict(invalidator, subject='L-spec-9999')], expected),
                (kind+'-new-refusal', [refusal, invalidator, refusal], expected),
            ])
        newer = dict(refusal, reason='checks-failed', tip='fixture-new-tip')
        cases.append(('newest-ledger-row', [refusal, newer],
                      {key: newer[key] for key in ('reason', 'tip', 'retry_at')}))
        for reason in ('open-builder-dispatch', 'adoption-state-changed', 'already-current', 'attempt-started'):
            transient = row('branch-refresh-skipped', reason=reason)
            cases.extend([(reason, [transient], None),
                          (reason+'-does-not-clear', [refusal, transient], expected)])
        cases.append(('other-event', [refusal, row('autodispatched', spawn='123')], expected))
        daily = dict(refusal, reason='daily-limit', retry_at='2026-11-07T00:00:00Z')
        with patch('branch_refresh.git', side_effect=AssertionError('Git forbidden')), \
             patch('pathlib.Path.open', side_effect=AssertionError('filesystem forbidden')), \
             patch('pathlib.Path.exists', side_effect=AssertionError('filesystem forbidden')), \
             patch('os.stat', side_effect=AssertionError('filesystem forbidden')):
            for name, events, result in cases:
                with self.subTest(name=name):
                    self.assertEqual(br.recut_refusal(events, sid, now), result)
            self.assertEqual(br.recut_refusal([daily], sid, now)['retry_at'], daily['retry_at'])
            self.assertIsNone(br.recut_refusal([daily], sid, now + timedelta(seconds=1)))
            self.assertIsNone(br.recut_refusal([daily], sid, now + timedelta(seconds=2)))
            self.assertEqual(br.recut_refusal([daily, refusal], sid, now + timedelta(seconds=1)), expected)
        Fixture.signed(self, 'AC5', cases=[name for name, _, _ in cases],
                       midnight_expiry=True, filesystem_and_git_forbidden=True)


if __name__ == '__main__':
    unittest.main()
