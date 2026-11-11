#!/usr/bin/env python3
"""Isolated ledger evidence for tick's post-build observation."""
import atexit
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Bind disposable paths before importing any production module.
_BOOT = tempfile.TemporaryDirectory(prefix='tick-held-import-')
atexit.register(_BOOT.cleanup)
os.environ.update(DOIT_ROOT=_BOOT.name, DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=str(Path(_BOOT.name) / 'v4-ledger'),
                  V4_INBOX_DIR=str(Path(_BOOT.name) / 'v4-inbox'),
                  V4_STAGING_DIR=str(Path(_BOOT.name) / 'v4-staging'))
os.environ.pop('DOIT_PROJECT', None)
import dispatch
import fold
import ledger_cache
import nextstep
import spawns
import tick

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SID = 'L-spec-9901'


class HeldWithoutRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='tick-held-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('events', 'content', 'seat'):
            (self.root / name).mkdir()
        for module, attrs in ((fold, ('ROOT', 'EVENTS')), (dispatch, ('ROOT', 'EVENTS', 'CONTENT', 'SEAT'))):
            for attr in attrs:
                self.mock(module, attr, self.root if attr == 'ROOT' else self.root / attr.lower())
        self.mock(fold, 'PROJECT', None)
        self.mock(fold, 'NOW', NOW)
        self.mock(dispatch, 'now', return_value=NOW.isoformat())
        env = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        env.start()
        self.addCleanup(env.stop)

    def mock(self, module, attr, value=None, **kwargs):
        p = patch.object(module, attr, **kwargs) if kwargs else patch.object(module, attr, value)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def emit(self, kind, seconds=-1800, sid=SID, actor='operator', **fields):
        with patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=seconds)).isoformat()):
            error = dispatch.emit(self.root / 'events' / ('L-' + actor + '-local.jsonl'),
                                  {'subject': sid}, kind, **fields)
        self.assertIsNone(error)

    def seed(self, seconds=-1800, sid=SID, status='BLOCKED'):
        self.emit('spec-written', seconds - 120, sid, project='fixture')
        self.emit('build-done', seconds, sid, actor='builder', status=status,
                  ready_sha='a' * 40)
        if status == 'DONE':
            self.emit('evidence-bundle-failed', seconds + 1, sid, actor='build-runner',
                      sha='a' * 40, reason='fixture runner failure')

    def read(self):
        return fold.read_events(use_cache=False)

    def steps(self, events=None):
        events = self.read() if events is None else events
        specs = fold.fold(events)[0]
        return specs, tick._steps(events, specs, NOW)

    def held(self):
        return [e for e in self.read() if e['type'] == 'next-step-held']

    def observe(self, steps=None):
        events = self.read()
        specs, actual = self.steps(events)
        tick._record_next_step_held(events, specs, actual if steps is None else steps, NOW)

    def signed(self, ac, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://tick-held/' + ac, status=0,
                              body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                              body_excerpt=payload)))

    def test_ac1_threshold(self):
        checks = []
        for status in ('DONE', 'BLOCKED'):
            for age in (899, 900, 901):
                sid = f'L-spec-{9900 + len(checks)}'
                self.seed(-age, sid, status)
                if status == 'DONE':
                    self.emit('verdict', -age + 1, sid, actor='grader', confirmed=False)
                specs, steps = self.steps()
                if status == 'BLOCKED':
                    self.assertEqual(specs[sid]['state'], 'blocked')
                step = next(s for s in steps if s.item == sid)
                self.observe()
                held = [e for e in self.held() if e['spec'] == sid]
                self.assertEqual(len(held), int(age >= 900))
                if held:
                    row = held[0]
                    self.assertEqual((row['subject'], row['spec'], row['reason'], row['owner']),
                                     ('held:' + sid, sid, step.reason, step.owner))
                    self.assertEqual(row['ts'], NOW.isoformat())
                    self.assertEqual(datetime.fromisoformat(row['since']), NOW - timedelta(seconds=age))
                    self.assertIsNotNone(datetime.fromisoformat(row['since']).tzinfo)
                checks.append(dict(status=status, age=age, count=len(held)))
        self.emit('spec-written', -2000, 'L-spec-9997', project='fixture')
        self.seed(-2000, 'L-spec-9998')
        self.emit('spec-killed', -1900, 'L-spec-9998')
        self.observe()
        self.assertFalse(any(e['spec'] in {'L-spec-9997', 'L-spec-9998'} for e in self.held()))
        self.signed('AC1', checks)

    def invariant(self):
        events = self.read()
        specs, steps = self.steps(events)
        stalls = fold.stall_rows(events, specs, now=NOW, dwell={'blocked': .01, 'graded': .01})
        body = dict(specs={sid: dict(age=s['age'], evs=s['evs']) for sid, s in specs.items()},
                    stalls=stalls,
                    steps=[dict(item=s.item, since=s.since, state=s.state, action=s.action,
                                owner=s.owner, trigger=s.trigger, reason=s.reason) for s in steps])
        return json.dumps(body, sort_keys=True, separators=(',', ':')).encode(), stalls

    def fresh_process(self, code):
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent),
                                     'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_ac2_dedup(self):
        def observe(steps):
            before = self.invariant()[0]
            self.observe(steps)
            self.assertEqual(self.invariant()[0], before)
        self.seed(-3 * 86400)
        specs, steps = self.steps()
        original = next(s for s in steps if s.item == SID)
        self.assertTrue(original.reason.startswith('unknown: no rule matched after'))
        before, stalls = self.invariant()
        self.assertTrue(any(r['spec'] == SID and r['bucket'] == 'STUCK' for r in stalls))
        reasons = ['A', 'A', 'B', 'B', 'A']
        for i, reason in enumerate(reasons):
            observe([replace(original, reason=reason, owner='operator' if i % 2 else 'tick',
                             since=(NOW - timedelta(seconds=i)).isoformat())])
            self.assertEqual(self.invariant()[0], before)
        self.assertEqual([e['reason'] for e in self.held()], ['A', 'B', 'A'])
        self.assertEqual(len({e['since'] for e in self.held()}), 1)
        count = len(self.held())
        self.fresh_process('''
from datetime import datetime
from dataclasses import replace
import fold, tick
now = datetime.fromisoformat('2026-11-01T12:00:00+00:00')
fold.NOW = now
events = fold.read_events(use_cache=False)
specs = fold.fold(events)[0]
steps = [replace(s, reason='A') for s in tick._steps(events, specs, now)]
tick._record_next_step_held(events, specs, steps, now)
''')
        self.assertEqual(len(self.held()), count)
        self.seed(-1800, 'L-spec-9902')
        _, steps = self.steps()
        other = next(s for s in steps if s.item == 'L-spec-9902')
        observe([replace(other, reason='A')])
        self.assertEqual(len(self.held()), count + 1)
        # A real intervening assigned run ends the interval; refusals cannot.
        self.emit('spawn-failed', -1200, spawn='L-grader-refused', actor='tick', reason='capacity')
        observe([replace(original, reason='A')])
        self.assertEqual(len(self.held()), count + 1)
        self.emit('spawn-started', -1200, spawn='L-grader-9901', role='grader')
        observe([replace(original, reason='A')])
        self.assertEqual(len(self.held()), count + 1)
        self.emit('spawn-done', -900, spawn='L-grader-9901')
        observe([replace(original, reason='A')])
        self.assertEqual(len(self.held()), count + 2)
        self.assertEqual(self.held()[-1]['since'], (NOW - timedelta(seconds=900)).isoformat())
        # An elapsed/stale assignment ceases to suppress without a new launch.
        self.seed(-86400, 'L-spec-9910')
        self.emit('spawn-started', -80000, 'L-spec-9910', spawn='L-grader-9910', role='grader')
        _, steps = self.steps()
        stale_step = next(s for s in steps if s.item == 'L-spec-9910')
        observe([stale_step])
        self.assertTrue(any(e['spec'] == 'L-spec-9910' for e in self.held()))
        self.signed('AC2', dict(reasons=[e['reason'] for e in self.held()],
                                byte_invariance=True, fresh_process=True, stuck_rows=stalls))

    def isolate_record(self):
        """Only unrelated services/transport are replaced; lane evaluation is real."""
        services = {
            'intake.run': {}, 'notes.run': [], 'pr_closeout.run': [],
            'install_sync.run': [], 'push_origin.run': [], 'carry.uncarried': [],
            'carry.sync_v4': [], 'pane_resume.run': [], 'crons.check': [],
            'look._read_state': {'last_pass': NOW.isoformat()}, 'look.heartbeat_missed': None,
            'look.emit_once': None,
            'up.ensure_relay': 'healthy', 'resources.manifest_counts': {},
            'resources.not_reapable_counts': {}, 'reap_tmp._reap_recorded': [],
            'tree_cleanup.reap_worktrees': ([], []), 'loadgate.begin': None,
            'autodispatch.redispatch_dead': [], 'merge_queue.enqueue_due': [],
            'tick.launch_precheck': None,
        }
        mocks = {}
        for name, result in services.items():
            module, attr = name.rsplit('.', 1)
            mocks[name] = self.mock(importlib.import_module(module), attr, return_value=result)
        self.mock(importlib.import_module('autodispatch'), '_real_runner',
                  side_effect=AssertionError('external dispatch forbidden'))
        return mocks

    def heartbeat(self):
        return [e for e in self.read() if e['type'] == 'tick'][-1]

    def test_ac3_record_integration(self):
        from types import SimpleNamespace
        import autodispatch
        import lane_driver
        import loadgate
        mocks = self.isolate_record()
        # L-spec-2176: request the bounded fresh observer view with a real
        # dispatch-only append, rather than requiring a read on no-write passes.
        def dispatch_append(*args, **kwargs):
            refusal = tick._committed(dispatch.emit(tick.tick_path(), {'subject': SID},
                                 'autodispatch-skipped', strict=True,
                                 reason='scratch queue observation'))
            self.assertIsNone(refusal)
            return []
        mocks['merge_queue.enqueue_due'].side_effect = dispatch_append
        # The sole tail refresh must follow the whole pass and hold the lock.
        reader = ledger_cache.tail_refresh
        refreshes = []
        def ordered_read():
            import fcntl
            with (self.root / 'tick.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertGreaterEqual(mocks['autodispatch.redispatch_dead'].call_count, 1)
            self.assertGreaterEqual(mocks['merge_queue.enqueue_due'].call_count, 1)
            refreshes.append(True)
            return reader()
        self.seed()
        self.seed(-1800, 'L-spec-9902')
        self.emit('spawn-failed', -1700, 'L-spec-9902', actor='tick', reason='unclassified', exit_code=9)
        _, steps = self.steps()
        unknown = next(s for s in steps if s.item == 'L-spec-9902')
        self.assertTrue(unknown.reason.startswith('undetermined:'))
        with patch.object(ledger_cache, 'tail_refresh', side_effect=ordered_read):
            tick._record()
        self.assertEqual(len(refreshes), 1)
        self.assertEqual({e['spec'] for e in self.held()}, {SID, 'L-spec-9902'})
        self.assertNotIn('held_error', self.heartbeat())
        self.assertEqual(mocks['merge_queue.enqueue_due'].call_count, 1)
        self.emit('escalation-blocking', -1600, 'L-spec-9902', actor='operator', asks='classify failure',
                  default='wait', deadline=(NOW + timedelta(days=1)).isoformat(), revert='decision')
        before = len(self.held())
        tick._record()
        # The named idle reason carries the last event type; the pass's own event renames it once.
        self.assertEqual(len(self.held()), before + 1)
        self.assertTrue(self.held()[-1]['reason'].startswith('unknown: no rule matched after '), self.held()[-1])
        tick._record()
        self.assertEqual(len(self.held()), before + 1)  # same reason again, no new assignment
        # Real grade action, refusal and same-pass assignment all share _record.
        self.seed(-1800, 'L-spec-9903', 'DONE')
        _, steps = self.steps()
        action = next(s for s in steps if s.item == 'L-spec-9903')
        self.assertEqual((action.state, action.action), ('action', 'grade'))
        launches = []
        def launch(step, **kwargs):
            launches.append(step.item)
            if step.item == 'L-spec-9903':
                self.emit('spawn-started', 0, step.item, role='grader', spawn='L-grader-9903')
                return 'L-grader-9903'
            return None
        with patch.object(tick, 'launch', side_effect=launch):
            tick._record()
        self.assertEqual(launches, ['L-spec-9903'])
        self.assertFalse(any(e['spec'] == 'L-spec-9903' for e in self.held()))
        self.seed(-1800, 'L-spec-9904', 'DONE')
        _, refusal_steps = self.steps()
        refusal_step = next(s for s in refusal_steps if s.item == 'L-spec-9904')
        def refusal(step, **kwargs):
            self.emit('spawn-failed', 0, step.item, actor='tick', spawn='L-grader-refused',
                      role='grader', reason='bundle-pending')
            return None
        with patch.object(tick, 'launch', side_effect=refusal) as refused:
            tick._record()
        self.assertTrue(refused.called)
        self.assertTrue(any(e['spec'] == 'L-spec-9904' for e in self.held()))
        self.assertEqual(next(e['reason'] for e in self.held() if e['spec'] == 'L-spec-9904'),
                         refusal_step.reason)
        # The earlier assignment remains live on subsequent ticks too.
        self.assertFalse(any(e['spec'] == 'L-spec-9903' for e in self.held()))
        # Redispatch text contains no subject. Only the fresh ledger links it.
        self.seed(-1800, 'L-spec-9905')
        self.emit('spawn-started', -1500, 'L-spec-9905', role='grader', spawn='L-grader-dead')
        self.emit('spawn-stale', -1200, 'L-spec-9905', actor='tick', spawn='L-grader-dead')
        old = self.read()
        def replay(events, specs, now, **kwargs):
            self.assertFalse(any(e.get('spawn') == 'L-grader-replay' for e in events))
            # The spawn exists, but its subject link arrives on autodispatched.
            self.emit('spawn-started', 0, '', spawn='L-grader-replay', role='grader')
            self.emit('autodispatched', 0, 'L-spec-9905', actor='tick',
                      spawn='L-grader-replay', replaces='L-grader-dead', rule='grade',
                      since=(NOW - timedelta(seconds=1800)).isoformat())
            return ['replayed L-grader-dead as L-grader-replay']
        mocks['autodispatch.redispatch_dead'].side_effect = replay
        with patch.object(tick, 'launch', return_value=None):
            tick._record()
        self.assertFalse(any(e.get('spawn') == 'L-grader-replay' for e in old))
        self.assertTrue(any(e.get('subject') == 'L-spec-9905' and e.get('spawn') == 'L-grader-replay'
                            for e in self.read()))
        self.assertFalse(any(e['spec'] == 'L-spec-9905' for e in self.held()))
        mocks['autodispatch.redispatch_dead'].side_effect = None
        # Capacity/resource gates remain inside the original dispatcher.
        self.seed(-1800, 'L-spec-9906', 'DONE')
        self.emit('verdict', -1700, 'L-spec-9906', actor='grader', confirmed=True)
        self.mock(lane_driver, 'capacity', return_value=0)
        with patch.object(tick, 'launch', return_value=None) as launcher:
            tick._record()
        self.assertFalse(any(c.args[0].item == 'L-spec-9906' for c in launcher.call_args_list))
        self.assertTrue(any(e['spec'] == 'L-spec-9906' for e in self.held()))
        self.seed(-1800, 'L-spec-9907', 'DONE')
        gate = SimpleNamespace(grades_first=False, check=lambda *a, **k: SimpleNamespace(hold=True),
                               finish=lambda: {'admission_mode': 'enforce'})
        mocks['loadgate.begin'].return_value = gate
        with patch.object(tick, 'launch', side_effect=AssertionError('resource hold bypassed')):
            tick._record()
        self.assertTrue(any(e['spec'] == 'L-spec-9907' for e in self.held()))
        self.assertEqual(self.heartbeat()['admission_mode'], 'enforce')
        mocks['loadgate.begin'].return_value = None
        # Lock refusal cannot append any event.
        import fcntl
        with (self.root / 'tick.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = tick.tick_path().read_bytes()
            self.assertIsNone(tick._record())
            self.assertEqual(tick.tick_path().read_bytes(), before)
        # Both a partial pass and observer errors preserve the heartbeat.
        with patch.object(tick, 'run_steps', side_effect=RuntimeError('partial dispatch')), \
             patch.object(tick, '_record_next_step_held', side_effect=RuntimeError('observer failed')):
            tick._record()
        self.assertEqual(self.heartbeat()['held_error'], 'observer failed')
        self.assertTrue(self.heartbeat()['autodispatch_pass_failed'])
        with patch.object(ledger_cache, 'tail_refresh', side_effect=RuntimeError('refresh failed')), \
             patch.object(tick, 'launch', return_value=None):
            tick._record()
        self.assertEqual(self.heartbeat()['held_error'], 'refresh failed')
        self.seed(-1800, 'L-spec-9908')
        writer = dispatch.emit
        def refuse(dst, base, kind, **fields):
            return 'append refused' if kind == 'next-step-held' else writer(dst, base, kind, **fields)
        with patch.object(dispatch, 'emit', side_effect=refuse), \
             patch.object(tick, 'launch', return_value=None):
            tick._record()
        self.assertEqual(self.heartbeat()['held_error'], 'append refused')
        with patch.object(tick, 'launch', return_value=None):
            tick._record()
        self.assertNotIn('held_error', self.heartbeat())
        # A successful dispatch still completes when observation itself raises.
        self.seed(-1800, 'L-spec-9911', 'DONE')
        dispatched = []
        def successful(step, **kwargs):
            dispatched.append(step.item)
            self.emit('spawn-started', 0, step.item, role='grader', spawn='L-grader-9911')
            return 'L-grader-9911'
        with patch.object(tick, 'launch', side_effect=successful), \
             patch.object(tick, '_record_next_step_held', side_effect=RuntimeError('observer failed')):
            tick._record()
        self.assertIn('L-spec-9911', dispatched)
        self.assertEqual(self.heartbeat()['held_error'], 'observer failed')
        # Advance the fixture clock beyond completion and the new dwell.
        self.emit('spawn-done', 900, 'L-spec-9903', spawn='L-grader-9903')
        with patch.object(fold, 'NOW', NOW + timedelta(seconds=1800)), \
             patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=1800)).isoformat()), \
             patch.object(tick, 'launch', return_value=None):
            tick._record()
        self.assertTrue(any(e['spec'] == 'L-spec-9903' for e in self.held()))
        self.signed('AC3', dict(same_pass_assignment=True, replay_subject_join=True,
                                lock_refusal=True, heartbeat_errors=['observer failed', 'refresh failed',
                                                                   'append refused']))

    def test_ac4_ledger_roundtrip(self):
        self.seed()
        self.isolate_record()
        _, steps = self.steps()
        before = fold.fold(self.read())[0][SID]
        marker = self.root / 'must-not-exist'
        reason = 'quote "\n$(touch ' + str(marker) + '); `false` & echo inert'
        selected = [replace(s, reason=reason) for s in steps]
        with patch.object(tick, '_steps', return_value=selected):
            tick._record()
        self.assertFalse(marker.exists())
        output = self.fresh_process('''
import json, fold
events = fold.read_events(use_cache=False)
specs, _, ignored, _ = fold.fold(events)
held = [e for e in events if e['type'] == 'next-step-held']
print(json.dumps(dict(held=held, ignored=ignored, state=specs['L-spec-9901']['state'],
                     evs=specs['L-spec-9901']['evs'])))
''')
        result = json.loads(output)
        self.assertEqual(len(result['held']), 1)
        row = result['held'][0]
        self.assertEqual((row['actor'], row['subject'], row['spec'], row['reason']),
                         ('tick', 'held:' + SID, SID, reason))
        self.assertEqual(row['owner'], selected[0].owner)
        self.assertEqual(row['since'], (NOW - timedelta(seconds=1800)).isoformat())
        self.assertFalse(result['ignored'])
        self.assertEqual(result['state'], before['state'])
        self.assertEqual(result['evs'], before['evs'])
        fields = {k: row[k] for k in ('subject', 'spec', 'reason', 'owner', 'since')}
        for missing in fields:
            error = dispatch.emit(tick.tick_path(), {}, 'next-step-held',
                                  **{k: v for k, v in fields.items() if k != missing})
            self.assertIsNotNone(error)
        unauthorized = dict(fields, type='next-step-held', ts=NOW.isoformat(), v=1)
        (self.root / 'events/L-builder-untrusted.jsonl').write_text(json.dumps(unauthorized) + '\n')
        ignored = fold.fold(self.read())[2]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]['actor'], 'builder')
        self.assertEqual(len(self.held()), 2)  # raw reader includes ignored authorship
        # The unauthorized row cannot suppress a legitimate reason change.
        self.observe([replace(selected[0], reason='new reason')])
        self.assertEqual(self.held()[-1]['actor'], 'tick')
        self.signed('AC4', dict(roundtrip=row, independent_process=True, missing_fields_refused=5,
                                unauthorized_ignored=True, state=result['state'], lifecycle_unchanged=True))


if __name__ == '__main__':
    unittest.main()
