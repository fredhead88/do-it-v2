#!/usr/bin/env python3
"""Disposable main-comparison and adoption CPU regressions (round one)."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from datetime import timedelta
import unittest
from unittest.mock import patch

import branch_refresh as br
import fold
import spec_preflight
from test_branch_refresh import Fixture


def signed(tag, body):
    raw = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(url='fixture://refresh-before-grade/' + tag, status=0,
                         body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))


class RedOnMasterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='red-on-main-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'do-it-v2'
        self.repo.mkdir()
        self.env = spec_preflight._environment(self.root)
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Scratch')
        self.git('config', 'user.email', 'scratch@example.invalid')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/test_red.py').write_text('assert False\n')
        (self.repo / 'src/test_green.py').write_text('assert True\n')
        (self.repo / 'src/test_runner_error.py').write_text('raise SystemExit(2)\n')
        (self.repo / 'src/test_import_error.py').write_text('import unavailable_fixture_module\n')
        (self.repo / 'src/test_timeout.py').write_text('import time\ntime.sleep(30)\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'main tests')
        self.main = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'feature')
        (self.repo / 'src/test_red.py').write_text('assert True\n')
        (self.repo / 'src/test_green.py').write_text('assert False\n')
        self.git('commit', '-qam', 'opposite branch results')
        self.before = self.image()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       env=self.env, text=True).strip()

    def image(self):
        return (self.git('rev-parse', 'HEAD'), self.git('status', '--porcelain'),
                self.git('worktree', 'list', '--porcelain'),
                {p.name: p.read_bytes() for p in (self.repo / 'src').iterdir()})

    def compare(self, ids, main='main'):
        seen = []
        actual = spec_preflight._process
        def process(argv, **kwargs):
            self.assertIsInstance(argv, list)
            self.assertFalse(kwargs.get('shell'))
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], spec_preflight.TEST_CAP_S)
            if argv[0] == '/usr/bin/python3':
                tree = Path(kwargs['cwd'])
                seen.append(tree)
                self.assertNotEqual(tree, self.repo)
                self.assertEqual(subprocess.check_output(
                    ['git', '-C', str(tree), 'rev-parse', 'HEAD'],
                    env=kwargs['env'], text=True).strip(), self.main)
                self.assertNotIn('SUPABASE_DB_URL', kwargs['env'])
                self.assertNotIn('GIT_DIR', kwargs['env'])
                self.assertNotIn('PYTHONPATH', kwargs['env'])
            return actual(argv, **kwargs)
        with patch('spec_preflight._process', side_effect=process), \
             patch.dict(os.environ, SUPABASE_DB_URL='redacted-fixture', GIT_DIR='invalid-fixture'):
            result = br.red_on_master(self.repo, main, ids)
        self.assertEqual(self.image(), self.before)
        self.assertTrue(all(not tree.exists() for tree in seen))
        return result

    def test_main_bytes_red_green_and_mixed(self):
        for ids, expected in ((['src/test_red.py'], ['src/test_red.py']),
                              (['src/test_green.py'], []),
                              (['src/test_green.py', 'src/test_red.py'], ['src/test_red.py']),
                              ([], [])):
            with self.subTest(ids=ids):
                self.assertEqual(self.compare(ids), expected)
        signed('AC3-main-bytes', dict(red=['src/test_red.py'], green=['src/test_green.py'],
                                     branch_unchanged=True, cleanup=True))

    def test_missing_non_file_and_unsafe_ids_are_undetermined(self):
        for identifier in ('src/test_missing.py', 'src', '../test_escape.py',
                           '/src/test_red.py', '-test_red.py', 'src/test_red.py::case',
                           'src/test_red.py;touch owned', None):
            with self.subTest(identifier=identifier):
                self.assertIsNone(self.compare([identifier]))
        self.assertIsNone(self.compare(['src/test_red.py'], main='missing'))
        self.assertIsNone(self.compare(['src/test_red.py'], main='--help'))

    def test_symlink_cannot_escape_clone(self):
        outside = self.root / 'test_outside.py'
        outside.write_text('raise AssertionError("must not execute")\n')
        (self.repo / 'src/test_link.py').symlink_to(outside)
        self.git('add', '.')
        self.git('commit', '-qm', 'unsafe link')
        self.git('branch', '-f', 'main', 'HEAD')
        self.main = self.git('rev-parse', 'HEAD')
        self.before = self.image()
        self.assertIsNone(self.compare(['src/test_link.py']))

    def test_runner_error_and_timeout_are_undetermined_and_cleaned(self):
        self.assertIsNone(self.compare(['src/test_runner_error.py']))
        self.assertIsNone(self.compare(['src/test_import_error.py']))
        actual = spec_preflight._process
        for failure in (OSError('runner unavailable'), subprocess.TimeoutExpired('runner', 1)):
            seen = []
            def process(argv, **kwargs):
                if argv[0] == '/usr/bin/python3':
                    seen.append(Path(kwargs['cwd']))
                    raise failure
                return actual(argv, **kwargs)
            with self.subTest(failure=type(failure).__name__), \
                 patch('spec_preflight._process', side_effect=process):
                self.assertIsNone(br.red_on_master(self.repo, 'main', ['src/test_red.py']))
            self.assertTrue(seen)
            self.assertTrue(all(not p.exists() for p in seen))
            self.assertEqual(self.image(), self.before)
        signed('AC3-undetermined', dict(missing=True, unsafe=True, runner_error=True,
                                       timeout=True, cleanup=True))

    def test_real_timeout_kills_runner_and_cleans_clone(self):
        with patch('spec_preflight.TEST_CAP_S', 1):
            self.assertIsNone(self.compare(['src/test_timeout.py']))


class LedgerAndCpuTests(Fixture):
    def fresh_rows(self):
        return json.loads(subprocess.check_output([sys.executable, '-c',
            'import json, fold; print(json.dumps(fold.read_events(use_cache=False)))'],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent)), text=True))

    def adoption(self):
        rows = [e for e in self.fresh_rows() if e['type'] == 'branch-refreshed']
        self.assertEqual(len(rows), 1)
        event = rows[0]
        self.assertEqual(event['subject'], self.sid)
        self.assertEqual((event['from_sha'], event['to_sha'], event['base_sha']),
                         (self.start, self.git('rev-parse', self.branch), self.main))
        self.assertIsInstance(event['cpu_s'], (int, float))
        self.assertTrue(math.isfinite(event['cpu_s']))
        self.assertGreaterEqual(event['cpu_s'], 0)
        return event

    def test_real_child_cpu_persisted_and_repeated_call_does_not_adopt(self):
        actual = br.check_tree
        def measured(*args):
            subprocess.run([sys.executable, '-c', 'sum(i * i for i in range(800000))'], check=True)
            return actual(*args)
        with patch('branch_refresh.check_tree', side_effect=measured):
            self.assertEqual(self.run_refresh()['state'], 'refreshed')
        event = self.adoption()
        self.assertGreater(event['cpu_s'], 0)
        self.assertEqual(self.run_refresh()['reason'], 'already-current')
        self.assertEqual(self.adoption()['cpu_s'], event['cpu_s'])
        signed('AC8-refresh-cpu', dict(cpu_s=event['cpu_s'], adoption_count=1, fresh_reader=True))

    def test_counter_delta_uses_children_user_plus_system(self):
        counters = iter((SimpleNamespace(ru_utime=12.0, ru_stime=3.0),
                         SimpleNamespace(ru_utime=14.5, ru_stime=4.25)))
        with patch('branch_refresh.resource.getrusage', side_effect=lambda who: next(counters)) as usage:
            self.assertEqual(self.run_refresh()['state'], 'refreshed')
        self.assertEqual([c.args for c in usage.call_args_list], [(br.resource.RUSAGE_CHILDREN,)] * 2)
        self.assertEqual(self.adoption()['cpu_s'], 3.75)

    def test_recut_emits_one_cpu_receipt_at_adoption(self):
        self.assertEqual(br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)['state'], 'recut')
        event = self.adoption()
        self.assertEqual(br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)['reason'], 'already-current')
        self.assertEqual(self.adoption()['cpu_s'], event['cpu_s'])
        self.assertEqual(sum(e['type'] == 'branch-recut' for e in self.fresh_rows()), 1)
        signed('AC8-recut-cpu', dict(cpu_s=event['cpu_s'], adoption_count=1, fresh_reader=True))

    def test_failed_gate_retains_adoption_cpu(self):
        with patch('branch_refresh.regate', return_value=1):
            result = self.run_refresh()
        self.assertEqual(result['gate'], 1)
        self.adoption()
        self.assertEqual(self.run_refresh()['reason'], 'already-current')
        self.adoption()
        reworks = [e for e in self.fresh_rows() if e['type'] == 'conflict-rework']
        self.assertEqual(len(reworks), 1)
        self.assertEqual(reworks[0]['recut_reason'], 'merge-gate')

    def test_failed_recut_gate_retains_cpu_receipt_after_guarded_rollback(self):
        with patch('branch_refresh.regate', return_value=1):
            result = br.recut(self.repo, self.branch, 'main', self.sid, now=self.now)
        self.assertEqual(result['state'], 'checks-failed')
        self.assertEqual(self.git('rev-parse', self.branch), self.start)
        rows = self.fresh_rows()
        receipts = [e for e in rows if e['type'] == 'branch-refreshed']
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual((receipt['subject'], receipt['from_sha'], receipt['base_sha']),
                         (self.sid, self.start, self.main))
        self.assertNotEqual(receipt['to_sha'], self.start)
        self.assertTrue(math.isfinite(receipt['cpu_s']))
        self.assertGreaterEqual(receipt['cpu_s'], 0)
        self.assertEqual(sum(e['type'] == 'conflict-rework' for e in rows), 1)
        self.assertFalse(any(e['type'] == 'branch-recut' for e in rows))

    def test_held_reason_survives_fresh_reader(self):
        self.cfg['merge_queue']['branch_refresh_per_day'] = 0
        result = br.refresh_before_grade(self.repo, self.branch, 'main', self.sid,
                                         events=self.events, now=self.now)
        self.assertEqual(result['state'], 'hold')
        rows = self.fresh_rows()
        self.assertTrue(any(e.get('subject') == self.sid
                            and e.get('reason') == result['reason'] for e in rows))
        signed('AC8-hold', dict(reason=result['reason'], fresh_reader=True))


class CleanGradeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bundle_patch.stop()
        self.spec.write_text('Writes:\n- feature.py\n\n## Acceptance criteria\n'
                             'AC1 [backend]: retain the feature.\n\n## Verification\n'
                             '```bash\n/usr/bin/python3 -c "assert True"\n```\n')
        self.build = dict(type='build-done', subject=self.sid, project='do-it-v2',
                          status='DONE', ready_sha=self.start, base_sha=self.old,
                          ts=(self.now - timedelta(hours=1)).isoformat())
        (self.root / 'events').mkdir(exist_ok=True)
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        self.events = [self.build]
        (self.root / 'runner').mkdir()
        (self.root / 'runner-backing').mkdir()
        for context in (patch('grading_env._project_checkout', return_value=self.repo),
                        patch('build_runner.SHORT_TMP_ROOT', self.root),
                        patch('build_runner.SHORT_TMP_BACKING_ROOT', self.root / 'runner-backing'),
                        patch('scratch.sub', side_effect=lambda name: self.root / name),
                        patch('build_runner._main_branch', return_value='main')):
            context.start()
            self.addCleanup(context.stop)

    def packet_context(self, rows):
        import packet
        card = self.root / 'content/card-fixture.md'
        card.write_text('AC1 [backend] done · signed-response · scratch\nverify exit 0\n')
        context = packet.Ctx.__new__(packet.Ctx)
        context.a = SimpleNamespace(role='grader', subject=self.sid, base_sha=None,
                                    project='do-it-v2', repo=str(self.repo),
                                    worktree=str(self.tree), charter=None)
        context.evs, context.by, context.specs = rows, {self.sid: rows}, {}
        context.spec_file = lambda: self.spec
        context.card_file = lambda: card
        return context

    def real_packet(self, rows):
        import packet
        context = self.packet_context(rows)
        with patch.multiple(packet, ROOT=self.root, CONTENT=self.root / 'content'), \
             patch('packet.models.resolve', return_value={'backend': 'codex'}):
            text = '\n'.join(packet.p_grader(context))
        return context, text

    def before_grade(self):
        return br.refresh_before_grade(self.repo, self.branch, 'main', self.sid,
                                       events=self.events, now=self.now)

    def test_clean_refresh_publishes_new_identity_without_changing_build(self):
        import grader_packet
        with patch('branch_refresh.regate', side_effect=self.real_gate):
            result = self.before_grade()
        self.assertEqual(result['state'], 'refreshed')
        self.assertEqual(result['gate'], 0)
        tip = self.git('rev-parse', self.branch)
        self.assertEqual(self.git('merge-base', tip, self.main), self.main)
        self.assertEqual((self.tree / 'feature.py').read_text(), 'feature\n')
        rows = self.read()
        self.assertEqual([{k: v for k, v in e.items() if k not in {'actor', '_src'}}
                          for e in rows if e['type'] == 'build-done'], [self.build])
        identity = grader_packet.grading_build(rows, self.sid)
        self.assertEqual((identity['ready_sha'], identity['base_sha']), (tip, self.main))
        bundle = grader_packet.bundle_for(rows, self.sid, tip)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle['bundle']['base_sha'], self.main)
        import packet
        context, packet_text = self.real_packet(rows)
        self.assertEqual(packet.verify_base_sha(context), self.main)
        self.assertIn(bundle['event']['bundle_sha256'], packet_text)
        def fail(message, **facts):
            raise ValueError(message)
        text = 'bundle_sha256: `' + bundle['event']['bundle_sha256'] + '`'
        self.assertEqual(grader_packet.gate(rows, self.sid, text, lambda *a, **k: None, fail), bundle)
        before = self.git('rev-parse', self.branch)
        self.assertEqual(self.before_grade()['state'], 'current')
        self.assertEqual(self.git('rev-parse', self.branch), before)
        signed('AC1-identity', dict(real_gate=True, retained_feature=True, historical_build_unchanged=True))

    def test_build_and_adoption_timestamp_tie_uses_matching_build_authority(self):
        import grader_packet
        self.build['ts'] = fold.NOW.isoformat(timespec='seconds')
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        result = self.before_grade()
        self.assertEqual(result['state'], 'refreshed')
        self.assertEqual(grader_packet.grading_build(self.read(), self.sid)['ready_sha'], result['to_sha'])

    def launch_grade(self, expected):
        import dispatch
        import grader_packet
        import lane_driver
        import look
        import nextstep
        import tick
        import worktree_heal
        location = self.root / 'worktrees/do-it-v2' / self.branch
        location.parent.mkdir(parents=True)
        self.git('worktree', 'move', str(self.tree), str(location))
        self.tree = location
        calls = []
        actual = br.refresh_before_grade
        def refresh(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.assertEqual(result['state'], expected)
            calls.append('refresh-complete')
            return result
        def fail(message, **facts):
            raise AssertionError(message)
        def runner(argv):
            if argv[:3] == ['doit', 'packet', 'grader']:
                self.assertEqual(calls, ['refresh-complete'])
                context, text = self.real_packet(self.read())
                path = self.root / 'grader-packet.md'
                path.write_text(text)
                calls.append('packet')
                return dict(code=0, stdout=str(path))
            self.assertEqual(argv[:4], ['doit', 'dispatch', '--detach', 'grader'])
            packet = Path(argv[argv.index('--packet') + 1]).read_text()
            bundle = grader_packet.gate(self.read(), self.sid, packet, lambda *a, **k: None, fail)
            self.assertEqual(bundle['bundle']['sha'], self.git('rev-parse', self.branch))
            calls.extend(['real-dispatch-gate', 'grader-backend'])
            return dict(code=0, stdout=json.dumps(dict(detached='L-grader-9999')))
        runner.events = self.events
        runner.now = self.now
        runner.mains = {'do-it-v2': self.main}
        step = nextstep.Step(self.sid, 'spec', 'checking', 'action', 'grade', 'tick',
                             'fixture:1', 'condition met', None, self.build['ts'])
        config = look.load_toml()
        config = {**config, 'push': {**config.get('push', {}),
                  'main': {**config.get('push', {}).get('main', {}), 'do-it-v2': 'main'}}}
        with patch.multiple(dispatch, ROOT=self.root, CONTENT=self.root / 'content', EVENTS=self.root / 'events'), \
             patch('look.load_toml', return_value=config), \
             patch.object(worktree_heal, 'enabled', return_value=False), \
             patch('branch_refresh.refresh_before_grade', side_effect=refresh), \
             patch('branch_refresh.regate', side_effect=self.real_gate):
            self.assertEqual(tick.launch(step, dispatcher=runner), 'L-grader-9999')
        self.assertEqual(calls, ['refresh-complete', 'packet', 'real-dispatch-gate', 'grader-backend'])
        signed('AC1-launch-' + expected, dict(order=calls, graders=1, builders=0))

    def test_tick_launch_refreshes_before_real_packet_and_dispatch_gate(self):
        import build_runner
        # The launch preflight only starts a grader whose evidence bundle names the
        # current main; the stale branch tip is refreshed after that check passes.
        build_runner.run(self.sid, self.start)
        self.launch_grade('refreshed')

    def test_tick_launch_current_build_dispatches_one_grader(self):
        import build_runner
        self.git('reset', '--hard', self.old)
        self.main = self.old
        build_runner.run(self.sid, self.start)
        self.launch_grade('current')

    def test_current_build_keeps_original_identity_and_tip(self):
        import build_runner
        import grader_packet
        self.git('reset', '--hard', self.old)
        self.main = self.old
        build_runner.run(self.sid, self.start)
        with patch('branch_refresh.refresh', side_effect=AssertionError('current branch rewritten')):
            self.assertEqual(self.before_grade()['state'], 'current')
        rows = self.read()
        self.assertEqual(grader_packet.grading_build(rows, self.sid)['ready_sha'], self.start)
        self.assertEqual(self.git('rev-parse', self.branch), self.start)
        self.assertFalse(any(e['type'] == 'branch-refreshed' for e in rows))
        _, text = self.real_packet(rows)
        self.assertIsNotNone(grader_packet.gate(rows, self.sid, text,
                                               lambda *a, **k: None,
                                               lambda *a, **k: self.fail('current bundle refused')))
        signed('AC1-current', dict(original_identity=True, tip_unchanged=True, gate=0))

    def test_invalid_receipts_and_evidence_fail_closed(self):
        import copy
        import grader_packet
        self.assertEqual(self.before_grade()['state'], 'refreshed')
        rows = self.read()
        identity = grader_packet.grading_build(rows, self.sid)
        ready = identity['ready_sha']
        def refused(damaged):
            def fail(message, **facts):
                raise ValueError(message)
            with patch('grader_packet.subprocess.Popen'), self.assertRaises(ValueError):
                grader_packet.gate(damaged, self.sid, '', lambda *a, **k: None, fail)
        for field, value in (('build_key', 'unrelated'), ('spec_sha256', 'invalidated'),
                             ('from_sha', self.main), ('to_sha', self.start)):
            damaged = copy.deepcopy(rows)
            event = next(e for e in damaged if e['type'] == 'branch-refreshed')
            event[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                grader_packet.grading_build(damaged, self.sid)
            refused(damaged)
        for field in ('base_sha', 'bundle_sha256', 'bundle_key'):
            damaged = copy.deepcopy(rows)
            event = next(e for e in damaged if e['type'] == 'evidence-bundle')
            event[field] = 'invalid'
            with self.subTest(field=field):
                self.assertIsNone(grader_packet.bundle_for(damaged, self.sid, ready))
            refused(damaged)
        path = Path(next(e['path'] for e in rows if e['type'] == 'evidence-bundle'))
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        self.assertIsNone(grader_packet.bundle_for(rows, self.sid, ready))
        refused(rows)
        path.write_bytes(original)
        for reason in ('grade-refresh-failed', None):
            damaged = copy.deepcopy(rows)
            terminal = next(e for e in damaged if e.get('reason') == 'grade-refresh-success')
            if reason:
                terminal.update(reason=reason, gate=1)
            else:
                damaged.remove(terminal)
            with self.subTest(terminal=reason), self.assertRaises(ValueError):
                grader_packet.grading_build(damaged, self.sid)
        (self.tree / 'extra').write_text('new work\n')
        self.commit('tip moved', self.tree)
        self.assertIsNone(grader_packet.bundle_for(rows, self.sid, ready))
        refused(rows)
        signed('AC1-fail-closed', dict(stale_tip=True, wrong_base=True, wrong_digest=True,
                                      invalid_receipts=True, pending_and_failed_gate=True))

    def test_nonzero_gate_never_returns_ready(self):
        import grader_packet
        with patch('branch_refresh.regate', return_value=1):
            self.assertEqual(self.before_grade()['state'], 'conflict')
        with self.assertRaises(ValueError):
            grader_packet.grading_build(self.read(), self.sid)
        self.assertNotIn(self.before_grade()['state'], {'current', 'refreshed'})
        rows = LedgerAndCpuTests.fresh_rows(self)
        self.assertEqual(sum(e['type'] == 'branch-refreshed' for e in rows), 1)
        self.assertEqual(sum(e.get('reason') == 'grade-refresh-failed' for e in rows), 1)
        code = ('import fold, grader_packet; '
                'events=fold.read_events(use_cache=False); '
                '\ntry: grader_packet.grading_build(events, ' + repr(self.sid) + ')'
                '\nexcept ValueError: print("failed-gate-held")'
                '\nelse: raise AssertionError("failed adoption became ready")')
        self.assertEqual(subprocess.check_output([sys.executable, '-c', code], text=True,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))).strip(), 'failed-gate-held')
        signed('AC8-failed-gate', dict(adoptions=1, terminal_failures=1, independent_reader_held=True))

    def test_normalized_recut_reports_one_adoption(self):
        (self.tree / 'feature.py').write_text('feature\nsecond\n')
        self.commit('second feature commit', self.tree)
        self.start = self.git('rev-parse', self.branch)
        self.build['ready_sha'] = self.start
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        result = self.before_grade()
        self.assertEqual(result['state'], 'refreshed')
        self.assertEqual(self.before_grade()['state'], 'current')
        rows = LedgerAndCpuTests.fresh_rows(self)
        adoptions = [e for e in rows if e['type'] == 'branch-refreshed']
        self.assertEqual(len(adoptions), 1)
        self.assertTrue(math.isfinite(adoptions[0]['cpu_s']))
        self.assertGreaterEqual(adoptions[0]['cpu_s'], 0)
        self.assertEqual(sum(e['type'] == 'branch-recut' for e in rows), 1)
        signed('AC8-normalized-recut', dict(adoptions=1, repeated_state='current', cpu_s=adoptions[0]['cpu_s']))



class GradeBase(CleanGradeTests):
    """Clean-grade fixture state without re-running CleanGradeTests' cases."""
    fresh_rows = LedgerAndCpuTests.fresh_rows

    def count(self, kind, **match):
        return sum(e['type'] == kind and all(e.get(k) == v for k, v in match.items())
                   for e in self.fresh_rows())

    def conflict_branch(self):
        (self.tree / 'base').write_text('conflict\n')
        self.commit('conflicting feature', self.tree)
        self.git('reset', '--soft', 'HEAD~1', cwd=self.tree)
        self.git('commit', '--amend', '-qm', 'conflicting feature', cwd=self.tree)
        self.start = self.git('rev-parse', self.branch)
        self.build['ready_sha'] = self.start
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        self.events = [self.build]


for _name in [n for n in dir(CleanGradeTests) if n.startswith('test_')]:
    setattr(GradeBase, _name, None)


class RedispatchTests(Fixture):
    def setUp(self):
        super().setUp()
        import dispatch_builder
        self.db = dispatch_builder
        self.events = [dict(type='build-done', subject=self.sid, status='DONE', project='do-it-v2',
                            ts=self.now.isoformat())]
        self.runtime = SimpleNamespace(ROOT=self.root, spec_path=lambda _: self.spec,
                                       pending_conflict_rework=lambda *_: True)
        self.args = SimpleNamespace(project='do-it-v2', subject=self.sid, role='builder',
                                    effective_base_sha=self.main)

    class Refused(Exception):
        pass

    def fail(self, message, **facts):
        raise self.Refused(facts['reason'])

    def preflight(self, events=None):
        with patch('packet_base._effective_branch', return_value='main'):
            return self.db.branch_preflight(self.runtime, self.args, self.tree,
                                            self.events if events is None else events, self.fail)

    def test_completed_clean_branch_needs_no_builder_and_current_is_admitted(self):
        with self.assertRaisesRegex(self.Refused, 'branch-recut'):
            self.preflight()
        tip = self.git('rev-parse', self.branch)
        self.assertEqual(self.git('merge-base', tip, self.main), self.main)
        self.assertEqual((self.tree / 'feature.py').read_text(), 'feature\n')
        self.assertEqual(sum(e['type'] == 'conflict-rework' for e in self.read()), 0)
        self.assertIsNone(self.preflight())
        self.assertEqual(self.git('rev-parse', self.branch), tip)
        signed('AC2-clean', dict(builder_dispatches=0, current_admitted=True, tip_unchanged=True))

    def test_real_conflict_keeps_one_authorized_recovery(self):
        (self.tree / 'base').write_text('conflict\n')
        self.git('add', '.', cwd=self.tree)
        self.git('commit', '--amend', '-qm', 'conflicting feature', cwd=self.tree)
        start = self.git('rev-parse', self.branch)
        self.assertFalse(self.preflight())
        self.assertFalse(self.preflight())
        self.assertEqual(self.git('rev-parse', self.branch), start)
        reworks = [e for e in self.read() if e['type'] == 'conflict-rework']
        self.assertEqual(len(reworks), 1)
        self.assertEqual(reworks[0]['recut_reason'], 'conflict')
        signed('AC2-conflict', dict(reworks=1, branch_unchanged=True))

    def test_multi_commit_completed_branch_uses_recut_with_backup_tag(self):
        (self.tree / 'second.py').write_text('second\n')
        self.commit('second feature', self.tree)
        start = self.git('rev-parse', self.branch)
        with self.assertRaisesRegex(self.Refused, 'branch-recut'):
            self.preflight()
        rows = self.read()
        receipt = next(e for e in rows if e['type'] == 'branch-recut')
        self.assertEqual(self.git('rev-parse', receipt['tag']), start)
        self.assertEqual(self.git('merge-base', self.branch, self.main), self.main)

    def test_first_build_never_enters_completed_recut(self):
        with patch('branch_refresh.refresh_before_grade') as classify, \
             patch('branch_refresh.recut') as recut, patch('branch_refresh.refresh') as refresh:
            self.assertFalse(br.dispatch_preflight(self.runtime, self.args, self.tree, [], self.fail))
        for spy in (classify, recut, refresh):
            spy.assert_not_called()

    def test_hold_refuses_without_builder_or_rework(self):
        br.emit('spawn-started', self.sid, role='builder', spawn='L-builder-9999',
                ts=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
        start = self.git('rev-parse', self.branch)
        with self.assertRaisesRegex(self.Refused, 'branch-not-descendant'):
            self.preflight()
        self.assertEqual(self.git('rev-parse', self.branch), start)
        self.assertEqual(sum(e['type'] == 'conflict-rework' for e in self.read()), 0)


class FailureRoutingTests(GradeBase):
    def checks(self, ids):
        return patch('branch_refresh.check_tree', return_value=(ids, []))

    def forbidden(self):
        return [patch('conflict.conflict_attempts', wraps=__import__('conflict').conflict_attempts)]

    def test_shared_master_failure_holds_without_rework(self):
        start = self.git('rev-parse', self.branch)
        with self.checks(['src/test_red.py::T::a']), \
             patch('branch_refresh.red_on_master', return_value=['src/test_red.py']):
            result = self.before_grade()
        self.assertEqual((result['state'], result['reason']), ('hold', 'failing-on-master'))
        self.assertEqual(self.count('conflict-rework'), 0)
        row = next(e for e in self.fresh_rows() if e.get('hold_state') == 'hold')
        self.assertEqual((row['reason'], row['failing_ids']), ('failing-on-master', ['src/test_red.py::T::a']))
        self.assertEqual(self.git('rev-parse', self.branch), start)

    def test_branch_only_failure_reaches_repair_once(self):
        with self.checks(['src/test_red.py', 'src/test_new.py']), \
             patch('branch_refresh.red_on_master', return_value=['src/test_red.py']):
            result = self.before_grade()
            self.assertEqual(self.before_grade()['state'], 'conflict')
        self.assertEqual((result['state'], result['failing_ids']), ('conflict', ['src/test_new.py']))
        self.assertEqual(self.count('conflict-rework', recut_reason='checks-failed'), 1)

    def test_uncomparable_results_hold(self):
        for ids, red in ((['src/test_x.py'], None), (['checks-undetermined'], []),
                         (['verification-step-1'], [])):
            with self.subTest(ids=ids), self.checks(ids), \
                 patch('branch_refresh.red_on_master', return_value=red):
                self.assertEqual(self.before_grade()['state'], 'hold')
        self.assertEqual(self.count('conflict-rework'), 0)
        self.assertEqual(self.count('branch-refreshed'), 0)

    def test_adopted_gate_failure_conflicts_once_for_second_reader(self):
        with patch('branch_refresh.regate', return_value=1):
            first = self.before_grade()
        second = self.before_grade()
        for result in (first, second):
            self.assertEqual((result['state'], result['reason']), ('conflict', 'merge-gate'))
        self.assertEqual(self.count('conflict-rework', recut_reason='merge-gate'), 1)
        self.assertEqual(self.count('branch-refreshed'), 1)
        signed('AC4-gate', dict(reworks=1, adoptions=1, second_reader='conflict'))

    def test_recut_gate_failure_conflicts_with_rollback_and_tag(self):
        (self.tree / 'second.py').write_text('second\n')
        self.commit('second', self.tree)
        start = self.git('rev-parse', self.branch)
        self.build['ready_sha'] = start
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        with patch('branch_refresh.regate', return_value=1):
            result = self.before_grade()
        self.assertEqual((result['state'], result['reason']), ('conflict', 'merge-gate'))
        self.assertEqual(self.git('rev-parse', self.branch), start)
        self.assertEqual(self.git('rev-parse', 'rework/' + self.sid.removeprefix('L-spec-') + '-prev'), start)
        self.assertEqual(self.count('conflict-rework'), 1)

    def test_launch_boundary_runs_nothing_for_hold(self):
        import lane_driver
        import look
        location = self.root / 'worktrees/do-it-v2' / self.branch
        location.parent.mkdir(parents=True)
        self.git('worktree', 'move', str(self.tree), str(location))
        calls = []
        config = look.load_toml()
        for state in ('hold', 'undetermined'):
            with self.subTest(state=state), \
                 patch('look.load_toml', return_value=config), \
                 patch('packet_base._effective_branch', return_value='main'), \
                 patch('branch_refresh.refresh_before_grade',
                       return_value=dict(state=state, reason='fixture')):
                with self.assertRaisesRegex(ValueError, 'refresh before grade: fixture'):
                    lane_driver._dispatch('grader', self.sid, 'do-it-v2', None, calls.append)
        self.assertEqual(calls, [])
        self.assertEqual(self.count('conflict-rework'), 0)


class RefusalTests(GradeBase):
    def test_enumerated_routing_table(self):
        table = {}
        for reason in sorted(br.HOLD_REASONS):
            with patch('branch_refresh.refresh', return_value=dict(state='skipped', reason=reason)):
                if reason == 'uncommitted-state':
                    continue
                result = self.before_grade()
            table[reason] = result['state']
            self.assertEqual((result['state'], result['reason']), ('hold', reason))
        with patch('branch_refresh.refresh', return_value=dict(state='skipped', reason='migration-or-fingerprint')):
            result = self.before_grade()
            self.assertEqual(self.before_grade()['state'], 'conflict')
        table['migration-or-fingerprint'] = result['state']
        self.assertEqual(result['state'], 'conflict')
        self.assertEqual(self.count('conflict-rework'), 1)
        for outcome in (dict(state='skipped', reason='surprise'), dict(state='skipped'), dict(), dict(state='weird')):
            with patch('branch_refresh.refresh', return_value=outcome):
                self.assertEqual(self.before_grade()['state'], 'undetermined')
        with patch('branch_refresh.refresh', side_effect=OSError('injected')):
            self.assertEqual(self.before_grade()['state'], 'undetermined')
        with patch('branch_refresh.check_tree', side_effect=OSError('runner')):
            self.assertEqual(self.before_grade()['state'], 'hold')
        self.assertEqual(self.count('branch-refreshed'), 0)
        signed('AC5-table', table)

    def test_dirty_paths_markers_and_before_images_preserved(self):
        (self.tree / 'feature.py').write_text('changed\n')
        (self.tree / 'untracked.txt').write_text('keep\n')
        def image():
            return ({p.name: p.read_bytes() for p in self.tree.iterdir() if p.is_file()},
                    self.git('status', '--porcelain=v1', '-uall', cwd=self.tree),
                    (Path(self.git('rev-parse', '--absolute-git-dir', cwd=self.tree)) / 'index').read_bytes())
        before = image()
        result = self.before_grade()
        self.assertEqual((result['state'], result['reason']), ('hold', 'uncommitted-state'))
        self.assertEqual(sorted(result['dirty'][0]['paths']), ['feature.py', 'untracked.txt'])
        self.assertEqual(result['dirty'][0]['markers'], [])
        self.assertEqual(image(), before)
        self.assertEqual(self.count('branch-refreshed') + self.count('conflict-rework'), 0)

    def test_marker_only_dirty_case(self):
        marker = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD', cwd=self.tree))
        marker.write_text(self.main + '\n')
        before = (self.git('status', '--porcelain=v1', '-uall', cwd=self.tree), marker.read_bytes())
        result = self.before_grade()
        self.assertEqual(result['reason'], 'uncommitted-state')
        self.assertEqual((result['dirty'][0]['paths'], result['dirty'][0]['markers']), ([], ['MERGE_HEAD']))
        self.assertEqual((self.git('status', '--porcelain=v1', '-uall', cwd=self.tree), marker.read_bytes()), before)

    def test_moving_main_holds_and_leaves_branch(self):
        start = self.git('rev-parse', self.branch)
        actual = br.check_tree
        def moving(*args):
            (self.repo / 'late').write_text('late\n')
            self.commit('main moved again')
            return actual(*args)
        with patch('branch_refresh.check_tree', side_effect=moving):
            result = self.before_grade()
        self.assertEqual((result['state'], result['reason']), ('hold', 'adoption-state-changed'))
        self.assertEqual(self.git('rev-parse', self.branch), start)

    def test_stale_multi_commit_recuts_and_current_multi_commit_is_untouched(self):
        (self.tree / 'second.py').write_text('second\n')
        self.commit('second', self.tree)
        start = self.git('rev-parse', self.branch)
        self.build['ready_sha'] = start
        (self.root / 'events/L-builder-fixture.jsonl').write_text(json.dumps(self.build) + '\n')
        with patch('branch_refresh.recut', wraps=br.recut) as recut:
            self.assertEqual(self.before_grade()['state'], 'refreshed')
        recut.assert_called_once()
        tip = self.git('rev-parse', self.branch)
        self.assertEqual(self.before_grade()['state'], 'current')
        self.assertEqual(self.git('rev-parse', self.branch), tip)


WORKER = r"""
import json, os, sys, time
from pathlib import Path
from unittest.mock import patch
cfg = json.loads(sys.argv[1])
os.environ.update(HOME=cfg['root'] + '/home', DOIT_ROOT=cfg['root'], DOIT_LEDGER_FILE='L-executor-0001.jsonl')
sys.path.insert(0, cfg['src'])
from datetime import datetime
import fold, branch_refresh as br
root = Path(cfg['root'])
now = datetime.fromisoformat(cfg['now'])
def wait(name):
    while not (root / name).exists():
        time.sleep(0.05)
def mark(name):
    (root / name).write_text('x')
real_emit, real_check, real_run = br.emit, br.check_tree, None
import build_runner
real_run = build_runner.run
def emit(kind, subject, **fields):
    row = real_emit(kind, subject, **fields)
    if fields.get('reason') == 'attempt-started' and cfg['hook'] == 'attempt':
        mark('A-attempt'); wait('release-attempt')
    if fields.get('reason') == 'attempt-started' and cfg['hook'] == 'die':
        mark('A-attempt'); os._exit(3)
    return row
def regate(*args):
    if cfg['hook'] == 'gate':
        mark('A-gate'); wait('release-gate')
    return cfg.get('gate', 0)
patches = [patch.multiple(fold, ROOT=root, EVENTS=root / 'events', NOW=now),
           patch('branch_refresh.config', return_value={'merge_queue': {'branch_refresh_per_day': 3}}),
           patch('branch_refresh.guard_commands', return_value=[('guard', '/usr/bin/python3 guard.py')]),
           patch('grading_env._project_row', return_value={}),
           patch('branch_refresh.regate', side_effect=regate),
           patch('branch_refresh.emit', side_effect=emit),
           patch('branch_refresh.PENDING_WAIT_S', 30), patch('branch_refresh.OUTCOME_WAIT_S', 3),
           patch('grading_env._project_checkout', return_value=Path(cfg['repo'])),
           patch('build_runner.SHORT_TMP_ROOT', root), patch('build_runner.SHORT_TMP_BACKING_ROOT', root / 'runner-backing'),
           patch('scratch.sub', side_effect=lambda name: root / name),
           patch('build_runner._main_branch', return_value='main')]
if cfg.get('hold'):
    patches += [patch('branch_refresh.check_tree', return_value=(['src/test_red.py'], [])),
                patch('branch_refresh.red_on_master', return_value=['src/test_red.py'])]
real_busy = br._busy
def busy(*args):
    found = real_busy(*args)
    if found and cfg['hook'] == 'none':
        mark('B-blocked')
    return found
patches.append(patch('branch_refresh._busy', side_effect=busy))
for p in patches:
    p.start()
result = br.refresh_before_grade(cfg['repo'], cfg['branch'], 'main', cfg['sid'], now=now)
print(json.dumps({k: v for k, v in result.items() if k in {'state', 'reason'}}))
"""


class ConcurrentAttemptTests(GradeBase):
    def spawn(self, hook, **extra):
        cfg = dict(root=str(self.root), src=str(Path(__file__).resolve().parent), repo=str(self.repo),
                   branch=self.branch, sid=self.sid, now=self.now.isoformat(), hook=hook, **extra)
        return subprocess.Popen([sys.executable, '-I', '-c', WORKER, json.dumps(cfg)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})

    def wait_for(self, name, process):
        import time
        deadline = time.monotonic() + 60
        while not (self.root / name).exists():
            if process.poll() is not None and not (self.root / name).exists():
                self.fail(process.communicate())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    def finish(self, process):
        out, err = process.communicate(timeout=90)
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def race(self, first_hook, release, second_extra=None, **extra):
        first = self.spawn(first_hook, **extra)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.wait_for('A-attempt' if first_hook == 'attempt' else 'A-gate', first)
        second = self.spawn('none', **(second_extra or {}))
        self.addCleanup(lambda: second.poll() is None and second.kill())
        self.wait_for('B-blocked', second)
        self.assertIsNone(second.poll(), 'second caller must wait, not decide')
        (self.root / release).write_text('go')
        return self.finish(first), self.finish(second)

    def test_success_during_pre_adoption_checks(self):
        first, second = self.race('attempt', 'release-attempt')
        self.assertEqual((first['state'], second['state']), ('refreshed', 'refreshed'))
        self.assertEqual(self.count('branch-refresh-skipped', reason='attempt-started'), 1)
        self.assertEqual(self.count('branch-refreshed'), 1)
        self.assertEqual(self.count('conflict-rework'), 0)

    def test_success_with_second_caller_while_gate_pending(self):
        first = self.spawn('gate')
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.wait_for('A-gate', first)
        second = self.spawn('none')
        self.addCleanup(lambda: second.poll() is None and second.kill())
        import time
        time.sleep(2)
        self.assertIsNone(second.poll())
        self.assertEqual(self.count('branch-refreshed'), 1)
        (self.root / 'release-gate').write_text('go')
        self.assertEqual(self.finish(first)['state'], 'refreshed')
        self.assertEqual(self.finish(second)['state'], 'refreshed')
        self.assertEqual(self.count('branch-refresh-skipped', reason='attempt-started'), 1)
        self.assertEqual(self.count('branch-refreshed'), 1)

    def test_gate_failure_is_consumed_as_one_conflict(self):
        first = self.spawn('gate', gate=1)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.wait_for('A-gate', first)
        second = self.spawn('none', gate=1)
        self.addCleanup(lambda: second.poll() is None and second.kill())
        import time
        time.sleep(2)
        (self.root / 'release-gate').write_text('go')
        self.assertEqual(self.finish(first)['state'], 'conflict')
        self.assertEqual(self.finish(second)['state'], 'conflict')
        self.assertEqual(self.count('conflict-rework', recut_reason='merge-gate'), 1)
        self.assertEqual(self.count('branch-refreshed'), 1)

    def test_true_conflict_is_consumed_once(self):
        self.conflict_branch()
        first, second = self.race('attempt', 'release-attempt')
        self.assertEqual((first['state'], second['state']), ('conflict', 'conflict'))
        self.assertEqual(self.count('branch-refresh-skipped', reason='attempt-started'), 1)
        self.assertEqual(self.count('conflict-rework'), 1)

    def test_held_check_result_is_consumed_without_second_attempt(self):
        first, second = self.race('attempt', 'release-attempt', hold=True, second_extra=dict(hold=True))
        self.assertEqual((first['state'], second['state']), ('hold', 'hold'))
        self.assertEqual(self.count('branch-refresh-skipped', reason='attempt-started'), 1)
        self.assertEqual(self.count('conflict-rework'), 0)

    def test_terminated_attempt_leaves_undetermined_never_grades(self):
        import time
        first = self.spawn('attempt')
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.wait_for('A-attempt', first)
        second = self.spawn('none')
        self.addCleanup(lambda: second.poll() is None and second.kill())
        self.wait_for('B-blocked', second)
        self.assertIsNone(second.poll())
        first.kill()
        first.wait(timeout=30)
        result = self.finish(second)
        self.assertEqual(result['state'], 'undetermined')
        self.assertEqual(result['reason'], 'attempt-no-outcome')
        self.assertEqual(self.count('branch-refresh-skipped', reason='attempt-started'), 1)
        self.assertEqual(self.count('branch-refreshed'), 0)
        self.assertTrue(any(e.get('reason') == 'attempt-no-outcome' for e in self.fresh_rows()))


class SafetyAndCompatibilityTests(GradeBase):
    SCRIPTS = ('test_branch_refresh', 'test_branch_refresh_held', 'test_branch_refresh_open_builder',
               'test_lane_driver', 'test_lane_driver_log', 'test_packet',
               'test_packet_redaction_completeness')

    def test_existing_regression_scripts_pass_in_separate_processes(self):
        here = Path(__file__).resolve().parent
        codes = {}
        for name in self.SCRIPTS:
            done = subprocess.run([sys.executable, str(here / (name + '.py'))], cwd=here.parent,
                                  capture_output=True, text=True, timeout=300,
                                  env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
            codes[name] = done.returncode
            self.assertEqual(done.returncode, 0, name + done.stderr[-2000:])
        signed('AC7-compat', codes)

    def test_untrusted_refs_and_ids_are_inert_argv_data(self):
        start = self.git('rev-parse', self.branch)
        for branch, main in (('--help', 'main'), ('bad..ref', 'main'), (self.branch, '--help'),
                             (self.branch, 'main; touch owned'), (self.branch, '../escape')):
            with self.subTest(branch=branch, main=main):
                result = br.refresh_before_grade(self.repo, branch, main, self.sid, now=self.now)
                self.assertIn(result['state'], {'hold', 'undetermined'})
        for spec in ('../etc', 'L-spec-1; touch owned', '--help'):
            with self.assertRaises(ValueError):
                br.refresh_before_grade(self.repo, self.branch, 'main', spec)
        self.assertFalse((self.repo / 'owned').exists() or Path('owned').exists())
        self.assertEqual(self.git('rev-parse', self.branch), start)
        self.assertEqual(self.count('branch-refreshed'), 0)

    def test_test_ids_cannot_escape_clone_and_fixture_is_isolated(self):
        for identifier in ('../x.py', '/etc/passwd', '-x', 'src/../../x.py'):
            self.assertIsNone(br.red_on_master(self.repo, 'main', [identifier]))
        self.assertEqual(fold.ROOT, self.root)
        self.assertTrue(str(self.root).startswith(tempfile.gettempdir()) or 'scratch' in str(self.root))
        self.assertEqual(os.environ['DOIT_ROOT'], str(self.root))

    def test_ordinary_routes_gain_no_bypass(self):
        import lane_driver
        calls = []
        with patch('branch_refresh.refresh_before_grade') as classify:
            for role in ('builder', 'reviewer'):
                try:
                    lane_driver._dispatch(role, self.sid, 'do-it-v2', None, calls.append)
                except (ValueError, OSError, KeyError, AttributeError):
                    pass
            classify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
