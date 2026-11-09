"""Scratch ledger regressions for tick-owned bundle waits; no paid boundary."""
import os
from pathlib import Path
import tempfile

# Bind the scratch root before any product module is imported.
_BOOT = tempfile.TemporaryDirectory(prefix='bundle-wait-')
os.environ['DOIT_ROOT'] = _BOOT.name
os.environ['DOIT_NO_POKE'] = '1'
os.environ.pop('DOIT_PROJECT', None)

from datetime import datetime, timedelta, timezone
import json
import hashlib
import unittest
import subprocess
import sys
from unittest.mock import patch

import bundle_wait
import dispatch
import fold
import grader_packet
import lane_rules
import look
import nextstep
import tick

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SID = 'L-spec-9901'
SHA = 'a' * 40
MAIN = 'b' * 40


class BundleWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bundle-wait-case-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content/evidence' / SID).mkdir(parents=True)
        self.scope = patch.dict(os.environ, DOIT_ROOT=str(self.root))
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.patches = [patch.object(fold, 'ROOT', self.root),
                        patch.object(fold, 'EVENTS', self.root / 'events'),
                        patch.object(dispatch, 'CONTENT', self.root / 'content'),
                        patch.object(fold, 'NOW', NOW)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.emit('spec-written', 'spec-writer', seconds=-10000,
                  path=str(self.root / 'content' / (SID + '.md')))
        self.emit('spawn-done', 'spec-auditor', seconds=-9000, bad_cut=False)
        self.emit('build-done', 'builder', seconds=-8000, status='DONE', ready_sha=SHA)

    def emit(self, kind, actor='operator', seconds=0, subject=SID, **fields):
        with patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=seconds)).isoformat()):
            error = dispatch.emit(self.root / 'events' / f'L-{actor}-local.jsonl',
                                  dict(subject=subject, project='demo'), kind, **fields)
        self.assertIsNone(error)

    def read(self):
        return fold.read_events(use_cache=False)

    def specs(self, events):
        return fold.fold(events)[0]

    def step(self, now=NOW, mains=None):
        events = self.read()
        return nextstep.step_for(events, self.specs(events), SID, now, lane_rules.load(),
                                 mains={'demo': MAIN} if mains is None else mains)

    def publish(self, *, sha=SHA, seconds=-100, main=MAIN, **fields):
        path = self.root / 'content/evidence' / SID / 'bundle.json'
        path.write_text(json.dumps(dict(master_sha=main, local_master_sha=main)))
        self.emit('evidence-bundle', 'build-runner', seconds=seconds,
                  sha=sha, path=str(path), bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **fields)

    def run_tick(self, now=NOW, dry_run=False):
        events = self.read()
        # Ledger timestamps must share the injected tick clock, including the
        # first request that a later amendment is expected to invalidate.
        with patch.object(bundle_wait, 'project_main', return_value=MAIN), \
             patch.object(dispatch, 'now', return_value=now.isoformat()):
            return tick.run_steps(events, self.specs(events), now, dry_run=dry_run,
                                  dispatcher=lambda argv: self.fail(f'unexpected grader start: {argv}'))

    def test_ac1_pending_cases_and_legacy_receipts(self):
        for action in ('grade', 'regrade'):
            if action == 'regrade':
                self.emit('decision', seconds=-7000, regrade='yes')
            for count in range(4):
                for age in (60, 600):
                    with self.subTest(action=action, receipts=count, age=age):
                        for n in range(count):
                            self.emit('spawn-failed', 'grader', seconds=-age + n,
                                      spawn=f'L-grader-{count}{age}{n}', role='grader', reason='bundle-pending')
                        self.assertEqual(self.step().reason, 'not-ready:bundle-pending')
            for case in ('no-bundle', 'wrong-sha', 'restricted', 'pre-build', 'pre-amendment', 'stale-main', 'missing-main'):
                with self.subTest(action=action, case=case):
                    saved = (self.root / 'events/L-build-runner-local.jsonl')
                    if saved.exists():
                        saved.unlink()
                    if case == 'wrong-sha': self.publish(sha='c' * 40)
                    elif case == 'restricted': self.publish(criteria=['AC1'])
                    elif case == 'pre-build': self.publish(seconds=-8500)
                    elif case == 'pre-amendment':
                        self.publish(seconds=-200)
                        self.emit('spec-amended', seconds=-150)
                    elif case == 'stale-main': self.publish(main='c' * 40)
                    elif case == 'missing-main': self.publish()
                    step = self.step(mains={} if case == 'missing-main' else None)
                    self.assertEqual((step.state, step.action, step.owner, step.reason),
                                     ('hold', None, 'tick', 'not-ready:bundle-pending'))
                    self.assertIsNotNone(step.trigger)
                    print(action, case, 'hold without attempt')
            self.publish(seconds=-1)
            with patch.object(grader_packet, 'bundle_for', side_effect=AssertionError('full validation during precheck')):
                self.assertEqual(self.step().action, action)
            (self.root / 'events/L-build-runner-local.jsonl').unlink()

    def test_ac1_main_resolved_once_and_duplicate_holds(self):
        events = self.read()
        specs = self.specs(events)
        specs['L-spec-9902'] = dict(specs[SID])
        events += [dict(e, subject='L-spec-9902') for e in events]
        with patch.object(bundle_wait, 'project_main', return_value=MAIN) as resolve:
            tick._steps(events, specs, NOW)
        self.assertEqual(resolve.call_count, 1)
        import spawns
        with patch.object(spawns, 'lock_held', return_value=True):
            self.assertEqual(self.step().reason, 'not-ready:dispatch-in-progress')
        with patch.object(spawns, 'conflict', return_value='open grader'):
            self.assertEqual(self.step().reason, 'not-ready:duplicate-dispatch')

    def test_ac2_persisted_request_restart_and_actor_admission(self):
        with patch.object(bundle_wait.subprocess, 'Popen') as start:
            self.run_tick()
            self.run_tick()
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0], ['doit', 'run-checks', SID, '--sha', SHA])
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertNotIn('shell', start.call_args.kwargs)
        requests = [e for e in self.read() if e['type'] == 'bundle-requested']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['actor'], 'tick')
        self.assertIsNone(fold.admits(requests[0], 'tick'))
        self.assertIsNotNone(fold.admits(requests[0], 'operator'))
        self.emit('bundle-requested', sha='c' * 40, key='forged')
        self.assertIsNone(bundle_wait.request_for(self.read(), SID, 'c' * 40))
        self.emit('spec-amended', seconds=1)
        with patch.object(bundle_wait.subprocess, 'Popen') as start, \
             patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=2)).isoformat()):
            self.run_tick(NOW + timedelta(seconds=2))
        self.assertEqual(start.call_count, 1)
        self.assertEqual(len([e for e in self.read() if e['type'] == 'bundle-requested' and e['actor'] == 'tick']), 2)
        self.emit('build-done', 'builder', seconds=3, status='DONE', ready_sha='c' * 40)
        with patch.object(bundle_wait.subprocess, 'Popen') as start:
            self.run_tick(NOW + timedelta(seconds=4))
        self.assertEqual(start.call_count, 1)
        print('fresh ledger reads: request survives repeat pass, amendment and SHA get fresh requests; forged actor rejected')

    def test_ac2_main_only_change_and_base_only(self):
        with patch.object(bundle_wait.subprocess, 'Popen'):
            self.run_tick()
        changed = self.step(mains={'demo': 'd' * 40})
        self.assertIn('stale-main: operator must request current-main checks', changed.clears)
        with patch.object(bundle_wait.subprocess, 'Popen') as start:
            bundle_wait.run(changed, self.read(), NOW, root=self.root, dst=tick.tick_path())
        start.assert_not_called()
        self.publish(main='d' * 40)
        self.assertEqual(self.step(mains={'demo': 'd' * 40}).action, 'grade')
        for fields, expect in [(dict(base_only=True), False),
                               (dict(base_only=True, ready_sha='e' * 40), False),
                               (dict(base_only=True, ready_sha='e' * 40, cited=['AC1']), True)]:
            self.emit('blocked', seconds=1, **fields)
            self.assertEqual(self.step(mains={'demo': 'd' * 40}).reason == 'not-ready:bundle-pending', expect)
        with patch.object(bundle_wait.subprocess, 'Popen') as start:
            self.run_tick(NOW + timedelta(seconds=2))
        self.assertEqual(start.call_args.args[0][-1], 'e' * 40)

    def test_ac3_alarm_boundary_and_lane_stale(self):
        self.emit('bundle-requested', 'tick', sha=SHA, key='original')
        for age in (3600, 7199, 7200, 7201, 8000):
            cut = NOW + timedelta(seconds=age)
            with patch.object(look, '_send_operator'), patch.object(fold, 'NOW', cut):
                self.run_tick(cut)
            alarms = [e for e in self.read() if e['type'] == 'brief']
            self.assertEqual(len(alarms), int(age > 7200))
            if age < 7200:
                events = self.read()
                step = self.step(now=cut)
                row = fold.step_row(step, lane_rules.load(), cut)
                self.assertTrue(row['reason_live'])
                self.assertEqual(lane_rules.ladder(events, row, cut), 'act')
                briefs = []
                with patch.object(bundle_wait, 'project_main', return_value=MAIN):
                    look._check_lane_stale(events, cut, self.root, briefs)
                self.assertEqual(briefs, [])
                self.assertFalse(any(e.get('kind') == 'lane-stale' for e in self.read()))
        alarms = [e for e in self.read() if e['type'] == 'brief']
        self.assertIn('executor', json.dumps(alarms))
        self.assertIn(SID + ':' + SHA, json.dumps(alarms))
        print('7199 and 7200: no alarm; 7201 and repeat: exactly one durable executor alarm')

    def test_ac3_fresh_clocks_and_non_pending_candidates(self):
        self.emit('bundle-requested', 'tick', seconds=-10000, sha='c' * 40, key='older-sha')
        self.emit('bundle-requested', 'tick', seconds=-9000, sha=SHA, key='older-amendment')
        self.emit('spec-amended', seconds=-100)
        self.emit('bundle-requested', 'tick', seconds=-50, sha=SHA, key='new-amendment')
        with patch.object(look, '_send_operator'):
            self.run_tick()
        self.assertFalse(any(e['type'] == 'brief' for e in self.read()))
        for kind, actor, fields in [
                ('evidence-bundle-failed', 'build-runner', dict(sha=SHA, reason='runner-broken')),
                ('spec-killed', 'operator', {})]:
            self.emit(kind, actor, seconds=1, **fields)
            with patch.object(look, '_send_operator'):
                self.run_tick(NOW + timedelta(seconds=8000))
            self.assertFalse(any(e['type'] == 'brief' for e in self.read()))

    def test_ac3_base_only_operator_wait_lane_path(self):
        self.emit('blocked', seconds=-5000, base_only=True, ready_sha=SHA, cited=['AC1'])
        self.emit('bundle-requested', 'tick', seconds=-4000, sha=SHA, key='old-main')
        for age in (3600, 7199):
            cut = NOW + timedelta(seconds=age - 4000)
            events = self.read()
            step = self.step(now=cut)
            self.assertIn('stale-main', step.clears)
            self.assertIsNotNone(step.trigger)
            row = fold.step_row(step, lane_rules.load(), cut)
            self.assertTrue(row['reason_live'])
            self.assertEqual(lane_rules.ladder(events, row, cut), 'act')
            with patch.object(bundle_wait, 'project_main', return_value=MAIN):
                look._check_lane_stale(events, cut, self.root, [])
            self.assertFalse(any(e.get('kind') == 'lane-stale' for e in self.read()))
            with patch.object(look, '_send_operator'):
                self.run_tick(cut)
            self.assertFalse(any(e['type'] == 'brief' for e in self.read()))
        with patch.object(look, '_send_operator'):
            self.run_tick(NOW + timedelta(seconds=3201))
        self.assertEqual(len([e for e in self.read() if e['type'] == 'brief']), 1)

    def test_ac1_real_project_main_and_read_only_cli(self):
        repo = self.root / 'repos/demo'
        repo.mkdir(parents=True)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('empty repo must stay read-only')):
            self.assertIsNone(bundle_wait.project_main('demo', root=self.root))
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q', '-b', 'fixture')
        git('-c', 'user.name=fixture', '-c', 'user.email=fixture@localhost',
            'commit', '--allow-empty', '-qm', 'fixture')
        with patch.dict(os.environ, DOIT_MAIN='fixture'):
            self.assertEqual(bundle_wait.project_main('demo', root=self.root), git('rev-parse', 'HEAD'))
        before = {p.name: p.read_bytes() for p in (self.root / 'events').iterdir()}
        cli = subprocess.run([sys.executable, str(Path(nextstep.__file__)), '--json', SID],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        row = json.loads(cli.stdout)['steps'][0]
        self.assertEqual(row['reason'], 'not-ready:bundle-pending')
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'events').iterdir()})
        self.assertFalse((self.root / 'look-state.json').exists())

    def test_ac4_failed_evidence_start_failure_and_pure_projection(self):
        with patch.object(bundle_wait.subprocess, 'Popen', side_effect=OSError('not started')) as start:
            self.run_tick()
            self.run_tick()
        self.assertEqual(start.call_count, 2)
        self.assertFalse(any(e['type'] == 'bundle-requested' for e in self.read()))
        with patch.object(bundle_wait.subprocess, 'Popen', side_effect=AssertionError('pure path launched')):
            self.step()
            self.run_tick(dry_run=True)
        self.emit('evidence-bundle-failed', 'build-runner', seconds=-1, sha=SHA, reason='runner-broken')
        self.assertEqual(self.step().action, 'grade')
        class Refused(Exception): pass
        recorded = []
        def fail(why, reason=None):
            recorded.append(reason)
            raise Refused()
        with self.assertRaises(Refused):
            grader_packet.gate(self.read(), SID, '', lambda *a, **kw: None, fail)
        self.assertEqual(recorded, ['preflight:runner'])
        print('failed evidence keeps preflight:runner; failed checker starts retry; pure and dry-run paths write nothing')


if __name__ == '__main__':
    unittest.main()
