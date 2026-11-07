"""Scratch evidence for tick-owned base recovery; no provider or production writes."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

# This module is loaded first by the combined regression command. Imports must
# resolve their default roots inside scratch, including dispatch's seat path.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='base-recovery-import-')
os.environ['DOIT_ROOT'] = _IMPORT_ROOT.name
os.environ['DOIT_NO_POKE'] = '1'

import autodispatch
import branch_refresh
import dispatch
import fold
import lane_rules
import nextstep as ns
import tick

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SID = 'L-spec-9901'


def event(kind, minute=-10, **fields):
    return dict(type=kind, subject=SID, ts=(NOW + timedelta(minutes=minute)).isoformat(),
                actor='operator', _src=f'L-operator-fixture.jsonl:{20000+minute}') | fields


def history(kind='spawn-failed', diagnostic='stale-base', count=1, role='builder'):
    rows = [event('spec-written', -120, project='proj'),
            event('spawn-done', -119, role='spec-auditor', bad_cut=False)]
    for n in range(count):
        extra = dict(reason=diagnostic, exit_code=1, spawn=f'L-builder-{9900+n}')
        if role:
            extra['role'] = role
        else:
            extra['spawn'] = str(3000+n)
        rows.append(event(kind, -count+n, **extra))
    return rows


def specs(rows):
    return {e['subject']: dict(state='written') for e in rows}


def selected(rows, now=NOW, **kwargs):
    return ns.step_for(rows, specs(rows), SID, now, lane_rules.load(), **kwargs)


def signed(name, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://base-refresh/'+name, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=body)))


class TestBaseRefreshActions(unittest.TestCase):
    def test_ingress_and_histories(self):
        checked = []
        for token in ns.BASE_FAILURES:
            for diagnostic in (token, 'reason='+token, 'problem:'+token+' — refresh'):
                for kind, role in [('spawn-failed', 'builder'), ('autodispatch-failed', 'builder'),
                                   ('autodispatch-failed', None)]:
                    for count in (1, 2, 3):
                        for prior in ('none', 'done', 'rework'):
                            rows = history(kind, diagnostic, count, role)
                            if prior != 'none':
                                rows.insert(2, event('build-done', -90, status='DONE'))
                            if prior == 'rework':
                                rows.insert(3, event('conflict-rework', -80))
                            step = selected(rows)
                            self.assertEqual((step.owner, step.state, step.action, step.stage),
                                             ('tick', 'action', 'build', 'building'))
                            self.assertIsNone(step.clears)
                            self.assertIsNone(step.retry_at)
                            self.assertEqual((step.trigger, step.since), (rows[-1]['_src'], rows[-1]['ts']))
                            self.assertEqual(ns.next_steps(rows, specs(rows), NOW, lane_rules.load()), [step])
                            checked.append([kind, diagnostic, role, count, prior, step.owner, step.action, step.since])
        for diagnostic in ('pinned-base-mismatch', 'reason=pinned-base-mismatch',
                           'problem:pinned-base-mismatch — refresh'):
            self.assertEqual(selected(history('build-blocked', diagnostic)).action, 'build')
        signed('AC1', checked)


REFUSALS = ('migration-or-fingerprint', 'uncommitted-state', 'conflict', 'checks-failed',
            'merge-gate', 'cherry-pick-undetermined', 'checks-undetermined', 'invalid-branch',
            'main-branch', 'invalid-daily-limit', 'integrator-holds-branch')


class TestBaseRefreshRefusals(unittest.TestCase):
    def test_real_reader_and_pure_repeat(self):
        results = []
        for reason in REFUSALS:
            tip = None if reason == 'invalid-branch' else 'fixture-tip'
            rows = history()+[event('branch-recut-refused', 0, reason=reason, tip=tip)]
            original = deepcopy(rows)
            with patch.object(dispatch, 'emit', side_effect=AssertionError('snapshot writes')):
                first, second = selected(rows), selected(rows)
            self.assertEqual(first, second)
            self.assertEqual(rows, original)
            self.assertEqual((first.owner, first.state, first.action), ('thinker', 'hold', None))
            for detail in ('base-refresh:', reason, tip or 'unknown-tip'):
                self.assertIn(detail, first.reason)
            self.assertTrue(first.clears)
            self.assertIsNone(first.retry_at)
            self.assertEqual(branch_refresh.recut_refusal(rows, SID, NOW)['reason'], reason)
            results.append(asdict(first))
        signed('AC2', results)


class TestBaseRefreshRetryTimes(unittest.TestCase):
    def test_midnight_and_transients(self):
        midnight = datetime(2026, 11, 8, tzinfo=timezone.utc)
        rows = history()+[event('branch-recut-refused', 0, reason='daily-limit',
                                tip='fixture-tip', retry_at=midnight.isoformat())]
        snapshots = []
        for delta in (-1, 0, 1):
            step = selected(rows, midnight+timedelta(microseconds=delta))
            self.assertEqual(step.state, 'hold' if delta < 0 else 'action')
            self.assertEqual(step.retry_at, midnight.isoformat() if delta < 0 else None)
            snapshots.append(asdict(step))
        # Transient outcomes are branch-refresh-skipped, never refusal receipts.
        for reason in ('open-builder-dispatch', 'adoption-state-changed', 'already-current'):
            events = history()+[event('branch-refresh-skipped', 0, reason=reason)]
            self.assertEqual(selected(events).action, 'build')
        signed('AC3', snapshots)


class TestBaseRefreshRelease(unittest.TestCase):
    def test_ledger_reader_release_and_supersession(self):
        snapshots = []
        for kind in ('branch-recut', 'build-started', 'build-done', 'spec-written', 'decision', 'unblocked'):
            rows = history()+[event('branch-recut-refused', -0.5, reason='conflict', tip='old-tip')]
            for release in (event(kind, 0, spawn='L-builder-9999'),
                            event(kind, -2), event(kind, 0, subject='L-spec-9902'), event('lesson', 0)):
                events = sorted(rows+[release], key=lambda e: e['ts'])
                refusal = branch_refresh.recut_refusal(events, SID, NOW)
                step = selected(events)
                self.assertEqual('old-tip' in step.reason, refusal is not None)
                if kind == 'build-started' and refusal is None:
                    self.assertIsNone(step.action)
                snapshots.append(asdict(step))
            newer = rows+[event(kind, -0.25), event('branch-recut-refused', 0,
                        reason='checks-failed', tip='new-tip')]
            self.assertEqual(branch_refresh.recut_refusal(newer, SID, NOW)['tip'], 'new-tip')
            self.assertEqual(selected(newer).owner, 'thinker')
            self.assertIn('new-tip', selected(newer).reason)
        signed('AC4', snapshots)


class ScratchLaunch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='base-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repos/proj'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo/'base').write_text('base')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.wt = self.root/'worktrees/proj'/SID.lower()
        self.wt.parent.mkdir(parents=True)
        self.git('worktree', 'add', '-qb', SID.lower(), str(self.wt))
        (self.wt/'subject').write_text('subject')
        self.git('add', '.', cwd=self.wt)
        self.git('commit', '-qm', 'subject', cwd=self.wt)
        self.tip = self.git('rev-parse', 'HEAD', cwd=self.wt)
        (self.repo/'master').write_text('advanced')
        self.git('add', '.')
        self.git('commit', '-qm', 'master advanced')
        self.main = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-list', '--count', self.base+'..HEAD', cwd=self.wt), '1')
        self.assertEqual(self.git('merge-base', self.main, self.tip), self.base)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.wt), '')
        (self.root/'content').mkdir()
        (self.root/'content'/f'{SID}.md').write_text(f'base_sha: {self.base}\nAC1 [backend]: fixture\n')
        (self.root/'events').mkdir()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root/'events'),
                (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root/'events'),
                (dispatch, 'CONTENT', self.root/'content')):
            stack.enter_context(patch.object(module, key, value))
        import look
        config = look.load_toml()
        config = dict(config, push={**config.get('push', {}),
                      'main': {**config.get('push', {}).get('main', {}), 'proj': 'master'}})
        stack.enter_context(patch.object(look, 'load_toml', return_value=config))
        self.calls = []

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-C', str(cwd or self.repo), *args], text=True).strip()

    def runner(self, argv):
        self.calls.append(argv)
        if argv[0] == 'git':
            return dict(code=0, stdout=subprocess.check_output(argv, text=True), stderr='')
        self.assertIn(argv[:2], (['doit', 'readiness'], ['doit', 'packet'], ['doit', 'dispatch']))
        return dict(code=0, stdout=json.dumps(dict(detached=4321)) if argv[1] == 'dispatch'
                    else '/scratch/packet.md' if argv[1] == 'packet' else '', stderr='')

    def launch(self, rows):
        step = selected(rows)
        dispatcher = tick._PassDispatcher(self.runner, rows, specs(rows), NOW, False)
        return tick.launch(step, dispatcher=dispatcher)


class TestBaseRefreshLaunch(ScratchLaunch):
    def test_real_launch_matrix(self):
        results = []
        for kind, role in [('spawn-failed', 'builder'), ('autodispatch-failed', 'builder'),
                           ('autodispatch-failed', None)]:
            for token in ns.BASE_FAILURES:
                for diagnostic in (token, 'reason='+token, 'problem:'+token+' — refresh'):
                    for count, prior in product((1, 2, 3), ('none', 'done', 'rework')):
                        self.calls.clear()
                        rows = history(kind, diagnostic, count, role)
                        if prior != 'none':
                            rows.insert(2, event('build-done', -90, status='DONE'))
                        if prior == 'rework':
                            rows.insert(3, event('conflict-rework', -80))
                        outcome = tick.run_steps(rows, specs(rows), NOW, dispatcher=self.runner)
                        self.assertTrue(any(line.startswith('acted '+SID+' build') for line in outcome), outcome)
                        commands = [a[1] for a in self.calls if a[0] == 'doit']
                        self.assertEqual(commands, ['readiness', 'packet', 'dispatch'])
                        argv = self.calls[-1]
                        self.assertEqual(argv[:5], ['doit', 'dispatch', '--detach', 'builder', SID])
                        self.assertEqual(argv[argv.index('--cwd')+1], str(self.wt))
                        self.assertEqual(self.calls[-2][-2:], ['--base-sha', self.main])
                        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.wt), self.tip)
                        self.assertFalse(any(a[0] == 'git' for a in self.calls))
                        results.append([kind, role, diagnostic, count, prior, self.calls[:]])
        signed('AC5-launch', results)

    def test_admission_and_fabricated_authority(self):
        rows = history()
        for extra in (event('branch-recut-refused', 0, reason='conflict', tip='tip'),
                      event('dependency-hold', 0, waits_for='L-spec-9902'),
                      event('decision', 0, actor='thinker', action='hold'),
                      event('build-started', 0, spawn='L-builder-9999', role='builder')):
            self.calls.clear()
            self.assertIsNone(self.launch(rows+[extra]))
            self.assertEqual(self.calls, [])
        ordinary = history(diagnostic='FAILED fixture')
        forged = replace(selected(rows), reason='base-refresh: fabricated')
        dispatcher = tick._PassDispatcher(self.runner, ordinary, specs(ordinary), NOW, False)
        self.assertIsNone(tick.launch(forged, dispatcher=dispatcher))
        self.assertEqual(self.calls, [])
        # Genuine non-base failure remains a veto even when followed by base transport receipts.
        rows.insert(2, event('spawn-failed', -2, role='builder', spawn='L-builder-9800', reason='FAILED fixture'))
        self.assertIsNone(self.launch(rows))
        signed('AC5-admission', dict(held=True, forged=False, unrelated_retry_preserved=True))

    def test_capacity_governor_wave_and_one_receipt(self):
        import loadgate
        rows = history()
        with patch.object(autodispatch, 'capacity', return_value=0):
            result = tick.run_steps(rows, specs(rows), NOW, dispatcher=self.runner)
        self.assertTrue(any(line.startswith('seat-wait') for line in result), result)
        self.assertEqual(self.calls, [])
        gate = SimpleNamespace(mode='enforce', grades_first=False,
            check=lambda *a, **k: SimpleNamespace(hold=True), finish=lambda: {},
            note_launch=lambda *a, **k: None)
        with patch.object(loadgate, 'begin', return_value=gate):
            result = tick.run_steps(rows, specs(rows), NOW, dispatcher=self.runner, admission={})
        self.assertTrue(any(line.startswith('resource-wait') for line in result), result)
        self.assertEqual(self.calls, [])
        rows[0]['charter'] = 'L-charter-9900'
        rows += [event('spec-written', -120, subject='L-spec-9902', charter='L-charter-9900')]
        step = selected(rows, waves={SID: 2, 'L-spec-9902': 1})
        self.assertIsNone(step.action)
        result = tick.run_steps(rows, specs(rows), NOW, dispatcher=self.runner, steps=[step])
        self.assertFalse(any(line.startswith('acted') for line in result), result)
        self.assertEqual(self.calls, [])
        signed('AC5-capacity', dict(zero_slot=True, governor=True, earlier_wave=True))


    def test_wait_alarm_survives_thinker_refusal(self):
        rows = history()
        rows[-1]['ts'] = (NOW-timedelta(minutes=31)).isoformat()
        rows.append(event('branch-recut-refused', 0, reason='daily-limit', tip='tip',
                          retry_at=(NOW+timedelta(days=1)).isoformat()))
        step = selected(rows)
        first = tick._record_rework_waits(rows, [step], NOW, 1)
        second = tick._record_rework_waits(rows, [step], NOW, 1)
        self.assertEqual(first['observed'], 1)
        self.assertEqual(second['observed'], 0)
        signed('AC6-alarm', dict(first=first, second=second))


class TestBaseRefreshPurity(unittest.TestCase):
    def test_pure_and_conservative(self):
        rows = history()
        rules = lane_rules.load()
        originals = deepcopy((rows, rules))
        with patch.object(subprocess, 'run', side_effect=AssertionError('process')), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('provider/process')), \
                patch.object(Path, 'open', side_effect=AssertionError('filesystem')), \
                patch.object(branch_refresh, 'recut', side_effect=AssertionError('recut')), \
                patch.object(dispatch, 'emit', side_effect=AssertionError('write')):
            one = ns.next_steps(rows, specs(rows), NOW, rules)
            self.assertEqual(one, ns.next_steps(rows, specs(rows), NOW, rules))
            self.assertEqual(one[0], ns.step_for(rows, specs(rows), SID, NOW, rules))
        self.assertEqual((rows, rules), originals)
        for text in ('question stale-base', 'prefix-stale-base', 'stale-base-suffix',
                     'reason=stale-base-suffix', 'FAILED reason=stale-base',
                     'No space left on device reason=stale-base',
                     'packet: incomplete criterion AC1 from fixture', 'unknown'):
            self.assertNotEqual(selected(history(diagnostic=text)).action, 'build')
        signed('AC6', dict(pure=True, conservative=True, step=asdict(one[0])))


class TestBaseRefreshLedgerRoundTrip(ScratchLaunch):
    def test_real_writer_fresh_reader(self):
        rows = history('autodispatch-failed', 'problem:stale-base', 3, None)
        self.assertEqual(self.launch(rows), '4321')
        ledger = tick.tick_path()
        self.assertTrue(ledger.name.startswith('L-tick-'))
        code = """import json,fold
rows=fold.read_events(use_cache=False)
print(json.dumps([e for e in rows if e.get('subject')=='L-spec-9901']))
"""
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
                   PYTHONPATH=str(Path(__file__).resolve().parent))
        env.pop('DOIT_PROJECT', None)
        result = subprocess.check_output([sys.executable, '-c', code], env=env, text=True)
        receipts = json.loads(result)
        self.assertEqual(len(receipts), 1, result)
        receipt = receipts[0]
        self.assertNotIn('autodispatch-failed', [e['type'] for e in receipts])
        self.assertIsNone(self.launch(rows+receipts))
        self.assertEqual((receipt['type'], receipt['role'], receipt['rule'], receipt['spawn'], receipt['since']),
                         ('autodispatched', 'builder', 'build', '4321', rows[-1]['ts']))
        signed('AC7', dict(result='4321', receipt=receipt, fresh_process=True))


if __name__ == '__main__':
    unittest.main()
