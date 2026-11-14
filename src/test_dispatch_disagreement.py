#!/usr/bin/env python3
"""Scratch regressions for original-Step dispatch disagreement telemetry."""
import dataclasses
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import nextstep
import dispatch_disagreement as disagreement


def signed(name, body):
    print(json.dumps(dict(url='cli://dispatch-disagreement/' + name, status=0,
                         body_sha256=hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                         body_excerpt=body)), flush=True)


def step(action='build', **changes):
    values = dict(item='L-spec-1001', item_kind='spec', stage='building', state='action',
                  action=action, owner='tick', trigger='spec-written', reason='ready',
                  clears=None, since='2026-11-14T12:00:00+00:00', attempt=2)
    return nextstep.Step(**dict(values, **changes))


class ApprovalTests(unittest.TestCase):
    def test_actual_tick_launch_and_legacy_argv(self):
        from test_lane_driver import DriverTests
        fixture = DriverTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        import lane_driver
        for action in disagreement.ROLES:
            fixture.calls.clear()
            original = fixture.step(action)
            fixture.run_steps([original])
            launched = [args for args in fixture.calls if args[:2] == ['doit', 'dispatch']]
            self.assertEqual(len(launched), 1, action)
            argv = launched[0]
            self.assertEqual(json.loads(argv[argv.index('--nextstep-approval') + 1]),
                             disagreement.from_step(original))
            signed('AC1-' + action, argv)
            # Clear the tick record so a sequential launch tests a fresh selection.
            import tick
            tick.tick_path().unlink(missing_ok=True)
        for original, dry in ((fixture.step(state='hold', clears='decision'), False),
                              (fixture.step(owner='executor'), False), (fixture.step(), True)):
            fixture.calls.clear()
            fixture.run_steps([original], dry_run=dry)
            self.assertFalse(any('--nextstep-approval' in argv for argv in fixture.calls))
        fixture.calls.clear()
        lane_driver._dispatch('reviewer', fixture.sid, 'proj', None, fixture.runner)
        self.assertNotIn('--nextstep-approval', fixture.calls[-1])

    def test_original_answer_and_receiver_binding(self):
        for action, role in disagreement.ROLES.items():
            original = step(action)
            approval = disagreement.from_step(original)
            self.assertEqual(approval, {k: getattr(original, k) for k in disagreement.FIELDS
                                        if k != 'role'} | {'role': role})
            self.assertEqual(disagreement.parse(json.dumps(approval), original.item, role), approval)
            self.assertIsNone(disagreement.parse(approval, 'L-spec-1002', role))
            self.assertIsNone(disagreement.parse(approval, original.item, 'operator'))

    def test_no_approval_for_unselected_answers(self):
        self.assertIsNone(disagreement.from_step(step(state='hold', clears='decision')))
        self.assertIsNone(disagreement.from_step(step(owner='executor')))
        for value in ('{', 'null', '[]', '{}', None):
            self.assertIsNone(disagreement.parse(value, 'L-spec-1001', 'builder'))

    def test_two_selected_subjects_keep_their_original_answers(self):
        from test_lane_driver import DriverTests
        fixture = DriverTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        second = 'L-spec-1002'
        (fixture.root / 'worktrees/proj' / second.lower()).mkdir()
        (fixture.root / 'content' / (second + '.md')).write_text('AC1 [backend]: scratch\n')
        extra = [dict(e, subject=second) for e in fixture.events]
        fixture.events += extra
        fixture.specs[second] = dict(id=second, state='graded', evs=extra)
        originals = [fixture.step(), fixture.step(item=second, trigger='second-selection', attempt=3)]
        fixture.run_steps(originals)
        launches = [argv for argv in fixture.calls if argv[:2] == ['doit', 'dispatch']]
        self.assertEqual(len(launches), 2)
        self.assertEqual([json.loads(argv[argv.index('--nextstep-approval') + 1]) for argv in launches],
                         [disagreement.from_step(s) for s in originals])
        signed('AC1-sequential', launches)

    def test_detached_parent_forwards_original_json_to_real_child_parser(self):
        import runpy
        import start_check
        from types import SimpleNamespace
        from test_dispatch import R7DispatchTests
        fixture = R7DispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.packet.write_text('scratch without a pinned base\n')
        script = str(pathlib.Path(fixture.d.__file__))
        approval = json.dumps(disagreement.from_step(step(item=fixture.sid)), indent=1)
        argv = [script, 'builder', fixture.sid, '--packet', str(fixture.packet),
                '--cwd', str(fixture.tree), '--project', 'do-it-v2', '--detach',
                '--nextstep-approval', approval]
        def version_only(command, **kwargs):
            self.assertEqual(command, ['claude', '--version'], 'unexpected execution')
            return SimpleNamespace(stdout='fixture', returncode=0)
        with patch.object(start_check, 'refusal', return_value=None), \
                patch.object(subprocess, 'run', side_effect=version_only), \
                patch.object(subprocess, 'Popen', return_value=SimpleNamespace(pid=123)) as child, \
                patch.dict(os.environ, DOIT_LEDGER_FILE='L-tick-9999.jsonl'), \
                patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_path(script, run_name='__main__')
            self.assertEqual(exit.exception.code, 0)
            child_argv = child.call_args.args[0]
            self.assertNotIn('--detach', child_argv)
            self.assertEqual(child_argv[child_argv.index('--nextstep-approval') + 1], approval)
            child.reset_mock()
            with patch.object(sys, 'argv', child_argv[1:]), self.assertRaises(SystemExit) as exit:
                runpy.run_path(script, run_name='__main__')
            self.assertEqual(exit.exception.code, 1)
            child.assert_not_called()
        comparisons = [e for e in fixture.rows() if e['type'] == 'dispatch-disagreed']
        self.assertEqual([e['nextstep_answer'] for e in comparisons], [json.loads(approval)])
        self.assertEqual([e['rule'] for e in comparisons], ['no-pinned-base'])
        signed('AC1-detached-child', dict(argv=child_argv, comparisons=comparisons))


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        from test_dispatch import R7DispatchTests
        self.fixture = R7DispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def invoke(self, *, action='build', approval=True, packet=True, pin=None):
        f = self.fixture
        answer = disagreement.from_step(step(action, item=f.sid)) if approval is True else approval
        f.packet.write_text('scratch\n' + ('PINNED_BASE_SHA: ' + (pin or f.old) + '\n' if packet else ''))
        args = argparse.Namespace(role=disagreement.ROLES[action], subject=f.sid, packet=str(f.packet),
                                  path=None, cwd=str(f.tree), charter=None, project='do-it-v2',
                                  mcp_config=None, timeout=None, max_usd=None, seat=False,
                                  nextstep_approval=json.dumps(answer) if answer is not None else None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                f.d.main(args)
            except SystemExit:
                pass
            except f.BackendBoundary:
                pass
        rows = f.rows()
        return rows, [e for e in rows if e['type'] == 'dispatch-disagreed']

    def test_actual_receiver_guards_and_separate_attempts(self):
        f = self.fixture
        for module, name, value, rule in (
                (f.d, 'open_spec_writer_spawn', True, 'rework-open'),
                (f.f, 'spec_shape_pending', True, 'spec-shape'),
                (f.d, 'subject_project', 'other', 'wrong-project'),
                (f.d, 'unaudited', 'fixture audit pending', 'unaudited'),
                (f.f, 'spec_state', 'killed', 'receiver-refused')):
            with self.subTest(rule=rule), patch.object(module, name, return_value=value):
                f.clear_events()
                rows, comparisons = self.invoke()
                self.assertEqual([e['rule'] for e in comparisons], [rule])
                self.assertEqual(sum(e['type'] == 'spawn-failed' for e in rows), 1)
                f.backend.assert_not_called()
                signed('AC2-' + rule, rows)
        f.clear_events()
        for _ in range(2):
            self.invoke(packet=False)
        rows = f.rows()
        comparisons = [e for e in rows if e['type'] == 'dispatch-disagreed']
        self.assertEqual(len(comparisons), 2)
        self.assertEqual(len({e['spawn'] for e in comparisons}), 2)
        self.assertEqual({e['rule'] for e in comparisons}, {'no-pinned-base'})

    def test_spawns_and_grader_gates_keep_their_refusals(self):
        import spawns
        import grader_packet
        f = self.fixture
        # The real acquire path refuses when the lock directory is a file.
        (f.root / 'state').write_text('unreadable lock directory')
        rows, comparisons = self.invoke()
        self.assertEqual([e['rule'] for e in comparisons], ['dispatch-in-progress'])
        f.backend.assert_not_called()
        f.clear_events()
        (f.root / 'state').unlink()
        rows, comparisons = self.invoke(action='grade')
        self.assertEqual([e['rule'] for e in comparisons], ['bundle-pending'])
        f.backend.assert_not_called()
        signed('AC2-gates', rows)

    def test_nested_callback_emits_once(self):
        import dispatch
        f = self.fixture
        ledger = f.root / 'events' / 'L-builder-9991.jsonl'
        base = dict(subject=f.sid, project='do-it-v2', spawn=ledger.stem)
        attempt = disagreement.Attempt(disagreement.from_step(step(item=f.sid)), f.sid, 'builder')
        for _ in range(3):
            attempt.refuse(dispatch.emit, ledger, base, 'disk')
        self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_remaining_receiver_rules(self):
        import spawns
        import grader_packet
        import grading_env
        import precheck
        from types import SimpleNamespace
        f = self.fixture
        cases = (
            ('duplicate-dispatch', 'build', [(spawns, 'conflict', 'fixture duplicate')]),
            ('disk', 'build', [(f.d, 'ensure_room', False)]),
            ('commit-shape', 'grade', [(f.f, 'standing_rejects', {'COMMIT-SHAPE'})]),
            ('precheck-red', 'grade', [(precheck, 'pregrade_refusal', 'fixture precheck refusal')]),
            ('preflight:runner', 'grade', [(grader_packet, 'evidence_state', SimpleNamespace(
                ready_sha=f.old, bundle_event=None, _failed={'reason': 'fixture runner failed'}))]),
            ('held:browser', 'review', [(f.d, 'held_capability', 'capability:browser')]),
            ('preflight:tools', 'review', [(grading_env, 'preflight', [('tools', 'fixture tools missing')])]),
            ('wave-order', 'build', [(f.d, 'resolve_charter', 'L-charter-1001'),
                                   (f.d, 'spec_wave', 2), (f.d, 'wave_blocker', 'L-spec-1000')]),
        )
        for rule, action, replacements in cases:
            with self.subTest(rule=rule), contextlib.ExitStack() as stack:
                f.clear_events()
                if rule == 'wave-order':
                    f.record('spec-written', actor='spec-writer', footprint=['src/fixture.py'])
                for module, name, value in replacements:
                    stack.enter_context(patch.object(module, name, return_value=value))
                rows, comparisons = self.invoke(action=action)
                self.assertEqual([e['rule'] for e in comparisons], [rule])
                self.assertEqual(sum(e['type'] == 'spawn-failed' for e in rows), 1)
                f.backend.assert_not_called()
                signed('AC2-' + rule, comparisons)

    def test_locked_dispatch_and_branch_ancestry_refusals(self):
        import fcntl
        import spawns
        f = self.fixture
        state = f.root / 'state'
        state.mkdir(exist_ok=True)
        with (state / spawns.lock_name(f.sid)).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rows, comparisons = self.invoke()
            self.assertEqual([e['rule'] for e in comparisons], ['dispatch-in-progress'])
            f.backend.assert_not_called()
        f.clear_events()
        f.patch_commit()
        (f.tree / 'builder').write_text('dirty scratch worktree\n')
        before = f.snapshot()
        rows, comparisons = self.invoke(pin=f.new)
        self.assertEqual([e['rule'] for e in comparisons], ['branch-not-descendant'])
        self.assertEqual(f.snapshot(), before)
        f.backend.assert_not_called()
        signed('AC2-branch-not-descendant', comparisons)


class LedgerTests(unittest.TestCase):
    def test_production_append_and_separate_process_admission(self):
        import dispatch
        with tempfile.TemporaryDirectory(prefix='disagreement-ledger-') as temporary:
            root = pathlib.Path(temporary)
            events = root / 'events'
            events.mkdir()
            with patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE='1'), \
                    patch.object(dispatch, 'EVENTS', events):
                for role in ('builder', 'grader', 'reviewer', 'operator'):
                    action = next(k for k, v in disagreement.ROLES.items() if v == role) if role != 'operator' else 'build'
                    answer = disagreement.from_step(step(action))
                    event = dict(role=answer['role'], rule='fixture-rule', nextstep_answer=answer,
                                 receiver_answer=dict(state='refused', rule='fixture-rule'))
                    base = dict(subject=answer['item'], project='fixture', spawn=f'L-{role}-9001')
                    path = events / (base['spawn'] + '.jsonl')
                    self.assertIsNone(dispatch.emit(path, base, 'dispatch-disagreed', **event))
                    if role == 'builder':
                        del event['receiver_answer']
                        self.assertIsNotNone(dispatch.emit(path, base, 'dispatch-disagreed', **event))
            script = ('import json,fold,realm; events=fold.read_events(use_cache=False); '
                      'print(json.dumps([realm.compact(e,e["actor"]) for e in events '
                      'if e["type"]=="dispatch-disagreed" and not fold.check_append(e,e["actor"])]))')
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONPATH=str(pathlib.Path(__file__).parent))
            env.pop('DOIT_LEDGER_FILE', None)
            result = subprocess.run([sys.executable, '-B', '-c', script], env=env,
                                    text=True, capture_output=True, check=True)
            rows = json.loads(result.stdout)
            self.assertEqual({r['a'] for r in rows}, {'builder', 'grader', 'reviewer'})
            self.assertTrue(all(r['rule'] == 'fixture-rule' and r['nextstep_answer'] and r['receiver_answer'] for r in rows))
            signed('AC3', dict(bytes={p.name: p.read_text() for p in events.glob('*.jsonl')}, rows=rows))


class DailyTests(unittest.TestCase):
    @staticmethod
    def population(ledger=None):
        import realm
        now = dt.datetime(2026, 11, 14, 1, tzinfo=dt.timezone.utc)
        rows = []
        def persist(event, actor):
            if ledger is not None:
                with (ledger / ('L-' + actor + '-9998.jsonl')).open('a') as stream:
                    stream.write(json.dumps(event) + '\n')
        for i, (age, actor, why, reason) in enumerate((
                (1, 'builder', 'refused before any spend', 'disk'),
                (3, 'grader', 'x' * 200 + ' refusing', 'bundle-pending'),
                (5, 'reviewer', 'backend started and failed', 'timeout'),
                (23, 'builder', 'refused', 'disk'),
                (24, 'builder', 'refused', 'excluded-boundary'),
                (-1, 'builder', 'refused', 'excluded-future'),
                (1, 'relay', 'refused', 'excluded-relay'))):
            event = dict(type='spawn-failed', ts=(now - dt.timedelta(hours=age)).isoformat(),
                         subject='L-spec-1001', spawn=f'L-{actor}-{i:04d}', why=why, reason=reason)
            rows.append(realm.compact(event, actor))
            persist(event, actor)
        for age in (1, 23, 24, -1):
            answer = disagreement.from_step(step())
            event = dict(type='dispatch-disagreed', ts=(now - dt.timedelta(hours=age)).isoformat(),
                         subject=answer['item'], project='fixture', spawn=f'L-builder-{age:04d}',
                         role='builder', rule='disk', nextstep_answer=answer,
                         receiver_answer=dict(state='refused', rule='disk'))
            rows.append(realm.compact(event, 'builder'))
            persist(event, 'builder')
            persist(event, 'operator')
            persist(event, 'relay')
            assert realm.compact(event, 'operator') is None
            assert realm.compact(event, 'relay') is None
        return now, rows

    def test_real_ledger_bootstrap_counts_match_fixture_inventory(self):
        import realm
        from test_realm import parts_fixture
        with parts_fixture() as (root, _, _):
            now, rows = self.population(root / 'events')
            payload = realm.bootstrap(24, states=[], goals=[], proving=[], now=now)
            self.assertEqual(payload['dispatch_readiness'], disagreement.daily(rows, now))
            signed('AC4-bootstrap', payload['dispatch_readiness'])

    def test_window_boundaries_and_pretruncation_classification(self):
        now, rows = self.population()
        report = disagreement.daily(rows, now)
        self.assertEqual(report['disagreements'], 2)
        self.assertEqual(report['disagreements_by_rule'], {'disk': 2})
        self.assertEqual(report['refusals_by_reason'], {'disk': 2, 'bundle-pending': 1})
        self.assertEqual(disagreement.daily([], now)['disagreements'], 0)
        signed('AC4', dict(rows=rows, report=report))

    def test_precise_interval_and_equivalent_utc_timestamps(self):
        now = dt.datetime(2026, 11, 14, 1, 0, 0, 500000, tzinfo=dt.timezone.utc)
        rows = [dict(t='2026-11-14T01:00:00.250000Z', ty='dispatch-disagreed', a='builder', rule='disk'),
                dict(t='2026-11-13T01:00:00.500000Z', ty='dispatch-disagreed', a='builder', rule='lower')]
        report = disagreement.daily(rows, now)
        self.assertEqual(report['disagreements_by_rule'], {'disk': 1})
        self.assertEqual(disagreement.stamp(report['end']), now)
        self.assertEqual(disagreement.stamp(report['start']), now - dt.timedelta(hours=24))


class PanelTests(unittest.TestCase):
    def test_real_page_functions_escape_answers_and_show_window(self):
        import realm
        from test_realm import _page_function
        now, rows = DailyTests.population()
        payload = disagreement.daily(rows, now)
        hostile = '<img src=x onerror="globalThis.executed=true">'
        for row in rows:
            if row['ty'] == 'dispatch-disagreed':
                row['rule'] = hostile
                row['receiver_answer']['rule'] = hostile
                row['nextstep_answer']['trigger'] = hostile
        page = realm.PAGE.read_text()
        esc = next(line for line in page.splitlines() if line.startswith('const esc ='))
        functions = '\n'.join(_page_function(page, name) for name in
                              ('readinessWindow', 'readinessTurnedBack', 'readinessHtml'))
        script = ("const assert=require('node:assert/strict');const input=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "let DATA={dispatch_readiness:input.payload};const EVENTS=input.rows,S={now:Date.now()};\n"
                  + esc + '\n' + functions + '\n' +
                  "const html=readinessHtml();assert(html.includes('trailing UTC 24 hours'));"
                  "assert(html.includes('2 disagreements'));assert(html.includes('3 turned back'));"
                  "assert(html.includes('&lt;img'));assert(!html.includes('<img'));"
                  "assert(!globalThis.executed);assert.equal(readinessTurnedBack(),3);"
                  "assert.equal((html.match(/Nextstep:/g)||[]).length,2);"
                  "assert.equal((html.match(/Receiver:/g)||[]).length,2);"
                  "DATA.dispatch_readiness={...input.payload,disagreements:0,disagreements_by_rule:{},refusals_by_reason:{}};"
                  "assert(readinessHtml().includes('0 disagreements'));console.log(html);")
        result = subprocess.run(['node', '-e', script], input=json.dumps(dict(payload=payload, rows=rows)),
                                text=True, capture_output=True, check=True)
        signed('AC5-render-trace', dict(rendered=result.stdout, executed=False))


class SafetyTests(ReceiverTests):
    def test_approval_parser_bounds_and_hostile_types(self):
        good = disagreement.from_step(step())
        for value in ('[' * 1200 + ']' * 1200, 'x' * 8193, dict(good, trigger='x' * 8193),
                      dict(good, action=[]), dict(good, attempt=True), dict(good, since=1)):
            self.assertIsNone(disagreement.parse(value, good['item'], good['role']))

    def test_real_receiver_after_backend_and_success_never_compare(self):
        import grading_env
        import spawns
        from types import SimpleNamespace
        f = self.fixture
        valid = dict(depth='gates-only', round=1, blocking=[], recommendations=[], reverify=[],
                     offer=[], unverifiable=[], cleared=[], escalations=[], declarations=[])
        for name, result in (
                ('timeout', subprocess.TimeoutExpired('scratch', 1)),
                ('cancelled', spawns.Cancelled('L-reviewer-scratch')),
                ('quota', dict(is_error=True, result='codex quota precheck')),
                ('output-validation', dict(structured_output={})),
                ('accepted', dict(structured_output=valid))):
            with self.subTest(name=name), \
                    patch.object(grading_env, 'required', return_value=set()), \
                    patch.object(grading_env, 'preflight', return_value=[]):
                f.clear_events()
                f.backend.reset_mock()
                f.backend.side_effect = result if isinstance(result, BaseException) else None
                if not isinstance(result, BaseException):
                    f.backend.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
                rows, comparisons = self.invoke(action='review')
                self.assertEqual(comparisons, [])
                f.backend.assert_called_once()
                if name == 'accepted':
                    self.assertTrue(any(e['type'] == 'spawn-done' for e in rows))
                signed('AC6-backend-' + name, rows)

    def test_existing_scripts_match_unmodified_base(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        base = subprocess.check_output(['git', 'rev-parse', 'HEAD^'], cwd=repo, text=True).strip()
        names = ('dispatch', 'spawns', 'tick', 'realm', 'routing', 'lane_driver')
        with tempfile.TemporaryDirectory(prefix='disagreement-regressions-') as temporary:
            temporary = pathlib.Path(temporary)
            tree = temporary / 'base'
            subprocess.run(['git', 'worktree', 'add', '--detach', str(tree), base], cwd=repo,
                           capture_output=True, check=True)
            try:
                results = {}
                env = {k: v for k, v in os.environ.items() if not k.startswith('SUPABASE_DB_URL')
                       and k not in ('PYTHONPATH', 'DOIT_PROJECT', 'DOIT_LEDGER_FILE', 'DOIT_ROOT',
                                     'V4_LEDGER_DIR', 'V4_INBOX_DIR', 'V4_STAGING_DIR')}
                for name in ('home', 'tmp', 'root'):
                    (temporary / name).mkdir()
                env.update(HOME=str(temporary / 'home'), TMPDIR=str(temporary / 'tmp'),
                           DOIT_ROOT=str(temporary / 'root'), DOIT_NO_POKE='1',
                           DOIT_TEST_PROCESS='1', PYTHONDONTWRITEBYTECODE='1')
                for side, cwd in (('base', tree), ('candidate', repo)):
                    for name in names:
                        argv = ['/usr/bin/python3', 'src/test_' + name + '.py']
                        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                                text=True, timeout=180)
                        transcript = result.stdout + result.stderr
                        # Capture test identifiers plus assertion text, independent of line shifts.
                        failures = re.findall(r'^(?:FAIL|ERROR): .+|^AssertionError: .+', transcript, re.M)
                        self.assertTrue(result.returncode == 0 or failures,
                                        f'{side} {argv}: unclassified failure\n{transcript}')
                        results[side, name] = dict(command=' '.join(argv), exit_code=result.returncode,
                                                   failures=failures, transcript=transcript)
                for name in names:
                    original, candidate = results['base', name], results['candidate', name]
                    self.assertEqual(candidate['exit_code'], original['exit_code'], name)
                    self.assertEqual(candidate['failures'], original['failures'], name)
                    signed('AC6-regression-' + name, dict(base_revision=base, base=original, candidate=candidate))
            finally:
                subprocess.run(['git', 'worktree', 'remove', '--force', str(tree)], cwd=repo,
                               capture_output=True, check=True)

    def test_observer_contract_and_unobservable_populations(self):
        import dispatch
        import fold
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        root = self.fixture.root
        events = root / 'events'
        ship_at = (now - dt.timedelta(hours=25)).isoformat()
        ship = events / 'L-executor-9999.jsonl'
        with patch.object(dispatch, 'now', return_value=ship_at):
            self.assertIsNone(dispatch.emit(ship, dict(subject='L-spec-2156'), 'shipped'))
        answer = disagreement.from_step(step())
        payload = dict(role='builder', rule='disk', nextstep_answer=answer,
                       receiver_answer=dict(state='refused', rule='disk'))
        ledger = events / 'L-builder-9999.jsonl'
        base = dict(subject=answer['item'], project='fixture', spawn=ledger.stem)
        before = (fold.ROOT, fold.EVENTS, fold.PROJECT)
        report = disagreement.observe(root, 'L-spec-2156', now)
        self.assertEqual(report['state'], 'met')
        self.assertEqual(report['ship_at'], ship_at)
        self.assertEqual(report['disagreements'], 0)
        self.assertEqual((fold.ROOT, fold.EVENTS, fold.PROJECT), before)
        for _ in range(4):
            self.assertIsNone(dispatch.emit(ledger, base, 'dispatch-disagreed', **payload))
        report = disagreement.observe(root, 'L-spec-2156', now + dt.timedelta(seconds=2))
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['disagreements_by_rule'], {'disk': 4})
        # A malformed authorized comparison must prevent a green zero.
        invalid = dict(base, type='dispatch-disagreed', ts=now.isoformat(), **payload)
        invalid.pop('rule')
        with ledger.open('a') as stream:
            stream.write(json.dumps(invalid) + '\n')
        self.assertEqual(disagreement.observe(root, 'L-spec-2156', now)['state'], 'cannot-observe')
        self.assertEqual(disagreement.observe(root, 'L-spec-2156', now - dt.timedelta(hours=2))['state'],
                         'cannot-observe')
        self.assertEqual(disagreement.observe(root, 'missing', now)['state'], 'cannot-observe')
        self.assertEqual(disagreement.observe(root / 'missing', 'L-spec-2156', now)['state'],
                         'cannot-observe')
        with patch.object(fold, 'read_events', side_effect=PermissionError('scratch')):
            self.assertEqual(disagreement.observe(root, 'L-spec-2156', now)['state'], 'cannot-observe')
        # The real CLI must carry every field and distinguish cannot-observe.
        script = pathlib.Path(disagreement.__file__)
        for subject, expected in (('L-spec-2156', 2), ('missing', 2)):
            result = subprocess.run([sys.executable, str(script), '--observe-m4', '--root', str(root),
                                     '--spec', subject], text=True, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(set(json.loads(result.stdout)), {'state', 'ship_at', 'start', 'end',
                             'disagreements', 'disagreements_by_rule', 'refusals_by_reason', 'reason'})
        ledger.unlink()
        for count, expected in ((0, 0), (4, 1)):
            for _ in range(count):
                self.assertIsNone(dispatch.emit(ledger, base, 'dispatch-disagreed', **payload))
            result = subprocess.run([sys.executable, str(script), '--observe-m4', '--root', str(root),
                                     '--spec', 'L-spec-2156'], text=True, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            signed('AC6-observer-' + str(count), json.loads(result.stdout))
        self.assertFalse((root / 'cache').exists())

    def test_manual_malformed_and_previous_attempt_controls(self):
        f = self.fixture
        good = disagreement.from_step(step(item=f.sid))
        for answer in (None, '{', [], {}, dict(good, item='L-spec-other'),
                       dict(good, role='grader'), dict(good, state='hold')):
            f.clear_events()
            rows, comparisons = self.invoke(approval=answer, packet=False)
            self.assertEqual(comparisons, [])
            self.assertTrue(any(e['type'] == 'spawn-failed' for e in rows))
            f.backend.assert_not_called()
        f.clear_events()
        self.invoke(packet=False)
        f.clear_events()
        self.assertEqual(self.invoke(approval=None, packet=False)[1], [])
        signed('AC6-negative-controls', f.rows())

    def test_backend_boundary_cannot_emit_comparisons(self):
        import dispatch
        f = self.fixture
        attempt = disagreement.Attempt(disagreement.from_step(step(item=f.sid)), f.sid, 'builder')
        path = f.root / 'events' / 'L-builder-9999.jsonl'
        for rule in ('timeout', 'cancelled', 'quota', 'output-validation'):
            attempt.refuse(dispatch.emit, path, dict(subject=f.sid, project='fixture', spawn=path.stem),
                           rule, backend_ran=True)
        self.assertFalse(path.exists())
        self.assertLessEqual(len(pathlib.Path(dispatch.__file__).read_text().splitlines()), 1890)


if __name__ == '__main__':
    unittest.main()
