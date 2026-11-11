#!/usr/bin/env python3
"""Scratch-only R4 lifecycle, persistence and governed tick regressions."""
import atexit
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_BOOT = tempfile.TemporaryDirectory(prefix='rework-wait-import-')
atexit.register(_BOOT.cleanup)
os.environ.update(DOIT_ROOT=_BOOT.name, DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=str(Path(_BOOT.name) / 'v4-ledger'),
                  V4_INBOX_DIR=str(Path(_BOOT.name) / 'v4-inbox'),
                  V4_STAGING_DIR=str(Path(_BOOT.name) / 'v4-staging'))
os.environ.pop('DOIT_PROJECT', None)
os.environ.pop('DOIT_LEDGER_FILE', None)
import autodispatch
import dispatch
import fold
import lane_driver
import lane_rules
import look
import nextstep
import tick

NOW = datetime(2026, 11, 5, 20, tzinfo=timezone.utc)
SID = 'L-spec-9901'


def row(kind='conflict-rework', seconds=-1801, subject=SID, **fields):
    return dict(type=kind, subject=subject,
                ts=(NOW + timedelta(seconds=seconds)).isoformat(), **fields)


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='rework-wait-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('events', 'content', 'seat'):
            (self.root / name).mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module, names in ((fold, ('ROOT', 'EVENTS')),
                              (dispatch, ('ROOT', 'EVENTS', 'CONTENT', 'SEAT'))):
            for name in names:
                self.mock(module, name, self.root if name == 'ROOT' else self.root / name.lower())
        self.mock(fold, 'NOW', NOW)
        self.mock(fold, 'PROJECT', None)
        self.mock(dispatch, 'now', return_value=NOW.isoformat())
        self.mock(look, '_DIGEST', None)
        self.mock(look, '_DRY_RUN', False)
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1'))

    def mock(self, obj, name, *args, **kw):
        return self.stack.enter_context(patch.object(obj, name, *args, **kw))

    def report(self, name, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://rework-wait/' + name, status=0,
                              body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                              body_excerpt=payload)), flush=True)

    def waits(self, events, seats=1, now=NOW):
        return tick.rework_waits(events, now, seats)

    def read(self, cached=False):
        return fold.read_events(use_cache=cached)

    def fresh_files(self):
        # Independent file-opening process, after the production writer closes.
        code = '''import json, pathlib, sys
r = pathlib.Path(sys.argv[1])
events = [json.loads(s) for p in sorted((r/'events').glob('*.jsonl')) for s in p.read_text().splitlines()]
digests = {p.stem: json.loads(p.read_text()) for p in (r/'look/digests').glob('*.json')}
print(json.dumps(dict(events=events, digests=digests)))'''
        proc = subprocess.run([sys.executable, '-c', code, str(self.root)],
                              capture_output=True, text=True, check=True, timeout=20)
        return json.loads(proc.stdout)

    def observe(self, events, seats=1, now=NOW, steps=(), outcomes=()):
        with patch.object(fold, 'NOW', now), patch.object(dispatch, 'now', return_value=now.isoformat()):
            return tick._record_rework_waits(events, steps, now, seats, outcomes)

    def alarms(self):
        return [e for e in self.fresh_files()['events']
                if e['type'] == 'brief' and e.get('condition', '').startswith('rework-wait:')]

    def seed(self, events):
        # Input lifecycle fixtures only; alarm rows always use production emit_once.
        path = self.root / 'events/L-operator-local.jsonl'
        path.write_text(''.join(json.dumps(dict(v=1, **e)) + '\n' for e in events))


class AC1Tests(Scratch):
    def test_trigger_boundary_matrix(self):
        cases = []
        triggers = [row(), row('conflict-rework', verdict='re-dispatch'),
                    row('verdict', confirmed=False), row('spawn-failed', reason='stale-base')]
        for trigger in triggers:
            for age in (1799, 1800, 1801):
                for seats in (-1, 0, 1):
                    event = dict(trigger, ts=(NOW - timedelta(seconds=age)).isoformat())
                    actual = bool(self.waits([event], seats))
                    expected = age > 1800 and seats > 0
                    self.assertEqual(actual, expected)
                    cases.append(dict(type=event['type'], verdict=event.get('verdict'),
                                      age=age, seats=seats, expected=expected, actual=actual))
        for event in [row(reason='skip'), row(verdict='escalate'), row('verdict', confirmed=True),
                      row('spawn-failed', reason='unrelated'), row(seconds=1),
                      row(subject=None), row(ts_bad=True, seconds=0), dict(row(), ts='bad'),
                      dict(row(), ts='2026-11-01T00:00:00')]:
            self.assertEqual(self.waits([event]), [])
        self.report('AC1-boundaries', cases)

    def test_authoritative_failure_cross_product_and_real_shapes(self):
        results = []
        reasons = ['stale-base', 'branch-not-descendant', 'pinned-base-mismatch',
                   'unrelated', 'reason=stale-base\nAssertionError',
                   'reason=pinned-base-mismatch', 'problem:pinned-base-mismatch']
        for kind in ('spawn-failed', 'autodispatch-failed', 'merge-gate-undetermined',
                     'merge-gate-rework', 'build-blocked'):
            for reason in reasons:
                for exit_code in ('absent', 1, 0, None):
                    event = row(kind, reason=reason)
                    if exit_code != 'absent':
                        event['exit_code'] = exit_code
                    expected = nextstep._failure([event], lane_rules.load())[1] == 'base-refresh'
                    actual = bool(self.waits([event]))
                    self.assertEqual(actual, expected, (kind, reason, exit_code))
                    results.append(dict(type=kind, reason=reason, exit_code=exit_code,
                                        expected=expected, actual=actual))
        for sid, spawn, ts in [('L-spec-1807', 'L-builder-4648', '2026-10-29T23:13:10+00:00'),
                               ('L-spec-1808', 'L-builder-4649', '2026-10-29T23:14:00+00:00')]:
            event = dict(type='spawn-failed', subject=sid, spawn=spawn, ts=ts,
                         reason='branch-not-descendant')
            self.assertEqual(len(self.waits([event])), 1)
            self.assertNotEqual(nextstep.classify_failure(event['reason'], None), 'base-refresh')
            results.append(dict(real_shape=event, expected=True, actual=True))
        self.report('AC1-base-matrix', results)


class AC2Tests(Scratch):
    def test_lifecycle_resets_oldest_and_causal_ties(self):
        cases = []
        for kind, fields in [('build-started', {}), ('branch-recut', {}), ('shipped', {}),
                             ('spec-killed', {}), ('spec-closed', {}),
                             ('verdict', dict(confirmed=True)), ('escalation-blocking', {}),
                             ('conflict-rework', dict(verdict='escalate'))]:
            trigger = row(seconds=-4000)
            reset = row(kind, seconds=-3000, **fields)
            self.assertEqual(self.waits([trigger, reset]), [])
            self.assertEqual(len(self.waits([trigger, dict(reset, subject='L-spec-9999')])), 1)
            tied = dict(reset, ts=trigger['ts'])
            self.assertEqual(self.waits([trigger, tied]), [])
            reopened = [reset]
            if kind in ('shipped', 'spec-killed', 'spec-closed'):
                reopened += [row('spec-written', seconds=-2500)]
            if kind in ('escalation-blocking',) or fields.get('verdict') == 'escalate':
                reopened += [row('unblocked', seconds=-2500)]
            new = row(seconds=-2000)
            self.assertEqual(self.waits([trigger, *reopened]), [])
            expired = NOW + timedelta(days=1)
            self.assertEqual(self.waits([trigger, *reopened, new], now=expired)[0]['since'], new['ts'])
            self.assertEqual(self.waits([*reopened, dict(new, ts=reset['ts'])], now=expired)[0]['since'], reset['ts'])
            cases.append(dict(reset=kind, fields=fields, resolved=0, new_wait=1))
        oldest, later = row(seconds=-4000), row('verdict', seconds=-100, confirmed=False)
        self.assertEqual(self.waits([oldest, later])[0]['since'], oldest['ts'])
        for kind in ('shipped', 'spec-killed', 'spec-closed', 'escalation-blocking'):
            self.assertEqual(self.waits([oldest, row(kind, seconds=-3000), row(seconds=-2000)]), [])
        self.report('AC2-lifecycle', cases)

    def test_named_builder_projection_and_expiry(self):
        trigger = row(seconds=-30000)
        cap = dispatch.ROLES['builder'][1]
        results = []
        for kind in ('spawn-started', 'build-started'):
            # build-started discharges the old trigger; a new failure follows it.
            start = row(kind, seconds=-120, spawn='L-builder-9911', role='builder', window_min=5)
            failure = row('spawn-failed', seconds=-100, reason='stale-base', spawn='L-builder-9922')
            events = [trigger, start, dict(start), failure]
            expected = lane_driver._in_flight_role([start, dict(start)], NOW, 'builder')
            self.assertEqual(expected, 1)
            observation = NOW + timedelta(seconds=2000)
            self.assertEqual(self.waits(events, now=observation), [])
            for terminal_kind in ('spawn-done', 'spawn-failed', 'spawn-stale'):
                for terminal_subject in (SID, 'L-builder-9911', None):
                    terminal = row(terminal_kind, seconds=-50, subject=terminal_subject, spawn=start['spawn'])
                    isolated = [start, dict(start), terminal]
                    self.assertEqual(lane_driver._in_flight_role(isolated, observation, 'builder'), 0)
                    self.assertEqual(len(self.waits([*events, terminal], now=observation)), 1)
                    results.append(dict(start=kind, terminal=terminal_kind,
                                        terminal_subject=terminal_subject, actual=1))
            expiry = datetime.fromisoformat(start['ts']) + timedelta(minutes=5 + cap)
            self.assertEqual(self.waits(events, now=expiry), [])
            self.assertEqual(len(self.waits(events, now=expiry + timedelta(seconds=1))), 1)
        for start in [row('spawn-started', seconds=-10, spawn='L-reviewer-9911', role='reviewer'),
                      row('spawn-started', seconds=-10, subject='L-spec-9999', spawn='L-builder-9911'),
                      row('autodispatched', seconds=-10, spawn=17, role='builder')]:
            self.assertEqual(len(self.waits([trigger, start])), 1)
        assigned = row('autodispatched', seconds=-20, spawn='L-builder-9911')
        missing = row('spawn-started', seconds=-10, subject=None, spawn='L-builder-9911', role='builder')
        self.assertEqual(self.waits([trigger, assigned, missing]), [])
        ended = row('spawn-done', seconds=-1, subject=missing['spawn'], spawn=missing['spawn'])
        self.assertEqual(len(self.waits([trigger, assigned, missing, ended])), 1)
        self.assertEqual(self.waits([trigger, assigned, dict(missing, type='build-started'), ended]), [])
        with patch.object(lane_driver, '_in_flight_role', wraps=lane_driver._in_flight_role) as helper:
            self.waits([trigger, assigned, missing, ended,
                        row('spawn-started', subject='L-spec-9999', spawn='L-builder-9999')])
            projection = helper.call_args.args[0]
        equivalent = [dict(missing, subject=SID), ended]
        self.assertEqual(projection, equivalent)
        self.assertEqual(lane_driver._in_flight_role(projection, NOW, 'builder'),
                         lane_driver._in_flight_role(equivalent, NOW, 'builder'))
        # Bad rows cannot suppress a different valid subject or crash the helper.
        self.assertEqual(len(self.waits([trigger, dict(missing, ts='bad')])), 1)
        self.report('AC2-liveness', results)


class AC3Tests(Scratch):
    def test_stable_identity_sources_cache_restart_answer_and_new_wait(self):
        event = row(spawn='L-builder-9911', _src='L-operator-local.jsonl:1')
        expected = hashlib.sha256(json.dumps(['rework-wait-v1', SID, event['type'], event['ts'],
                                             event['spawn'], '', '', None, None],
                                            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        first = self.waits([event])[0]
        self.assertEqual(first['trigger'], expected)
        renamed = dict(reversed(list(dict(event, _src='renamed:999', src='elsewhere', actor='other').items())))
        self.assertEqual(self.waits([renamed, renamed])[0]['trigger'], expected)
        self.seed([event])
        uncached, cached = self.read(False), self.read(True)
        self.assertEqual(self.waits(uncached)[0]['trigger'], self.waits(cached)[0]['trigger'])
        self.observe(uncached)
        self.assertEqual(len(self.alarms()), 1)
        self.observe(uncached)  # deliberately stale same-process snapshot
        self.assertEqual(len(self.alarms()), 1)
        alarm = self.alarms()[0]
        self.assertEqual((alarm['condition'], alarm['key'], alarm['owner']),
                         ('rework-wait:' + expected, SID, 'thinker'))
        self.assertEqual(alarm['reading']['trigger_source'], event['_src'])
        self.assertIsNone(dispatch.emit(self.root / 'events/L-operator-local.jsonl', {},
                                       'brief-answered', ref='L-look-local.jsonl:1', why='fixture'))
        self.observe([renamed, *self.read()], now=NOW + timedelta(days=1))
        self.assertEqual(len(self.alarms()), 1)
        new = row(seconds=1, spawn='L-builder-9912')
        self.observe([row('build-started', seconds=0), new], now=NOW + timedelta(days=2))
        self.assertEqual(len(self.alarms()), 2)
        self.assertNotEqual(self.alarms()[1]['reading']['trigger'], expected)
        self.observe([row(subject='L-spec-9902')])
        self.assertEqual(len(self.alarms()), 3)
        self.report('AC3-identity', self.alarms())

    def test_real_cap_defer_restart_next_day_and_resolution(self):
        for i in range(50):
            look.emit_once('fixture-' + str(i), 'key', 'thinker', {}, self.read(), root=self.root)
        event = row()
        condition = 'rework-wait:' + self.waits([event])[0]['trigger']
        self.observe([event])
        files = self.fresh_files()
        digest = files['digests'][NOW.date().isoformat()]
        self.assertEqual(digest['emitted'], 50)
        self.assertIn(SID, digest['conditions'][condition]['items'])
        self.assertFalse(digest['conditions'][condition].get('ref'))
        self.assertEqual(self.alarms(), [])
        self.observe([event, *self.read()])
        self.assertEqual(self.alarms(), [])
        # A fresh interpreter replays input and calls the same production observer.
        code = '''import json, os, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
import fold, dispatch, tick
fold.NOW = datetime.fromisoformat(sys.argv[2])
dispatch.now = lambda: fold.NOW.isoformat()
event = json.loads(sys.argv[3])
tick._record_rework_waits([event, *fold.read_events(use_cache=False)], [], fold.NOW, 1, [])'''
        tomorrow = NOW + timedelta(days=1)
        subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parent),
                        tomorrow.isoformat(), json.dumps(event)], check=True, timeout=30)
        self.assertEqual(len(self.alarms()), 1)
        self.assertIsNone(dispatch.emit(self.root / 'events/L-operator-local.jsonl', {},
                                       'brief-answered', ref='L-look-local.jsonl:51', why='fixture'))
        self.observe([event], now=NOW + timedelta(days=2))
        self.assertEqual(len(self.alarms()), 1)
        self.report('AC3-cap', dict(initial_emitted=50, waiting_condition=condition,
                                   eventual_alarms=self.alarms()))

    def test_resolved_deferred_wait_never_emits(self):
        for i in range(50):
            look.emit_once('fixture-' + str(i), 'key', 'thinker', {}, [], root=self.root)
        event = row()
        self.observe([event])
        self.observe([event, row('shipped', seconds=-1)], now=NOW + timedelta(days=1))
        self.assertEqual(self.alarms(), [])
        self.report('AC3-resolved-deferred', dict(alarms=0))


class AC4Tests(Scratch):
    def test_real_append_and_independent_fresh_read(self):
        self.seed([row()])
        result = self.observe(self.read(), seats=3)
        fresh = self.fresh_files()
        alarms = self.alarms()
        self.assertEqual(len(alarms), 1)
        alarm = alarms[0]
        self.assertEqual(alarm['reading']['free_slots'], 3)
        self.assertEqual(alarm['reading']['wait_seconds'], 1801)
        digest = fresh['digests'][NOW.date().isoformat()]
        item = digest['conditions'][alarm['condition']]['items'][SID]
        self.assertEqual(item['reading'], alarm['reading'])
        self.assertEqual(digest['emitted'], 1)
        self.observe(self.read())
        self.assertEqual(len(self.alarms()), 1)
        self.report('AC4-roundtrip', dict(result=result, alarm=alarm, digest=digest))


class AC5Tests(Scratch):
    def isolate(self):
        import install_sync, merge_queue, pane_resume, pr_closeout, push_origin, up, crons
        import reap_tmp, resources, loadgate
        self.mock(tick.intake, 'run', return_value={})
        self.mock(tick.notes, 'run', return_value=None)
        self.mock(pr_closeout, 'run', return_value=None)
        self.mock(install_sync, 'run', return_value=[])
        self.mock(push_origin, 'run', return_value=[])
        self.mock(tick.carry, 'uncarried', return_value=[])
        self.mock(tick.carry, 'sync_v4', return_value=None)
        self.mock(pane_resume, 'run', return_value={})
        self.mock(up, 'ensure_relay', return_value='healthy')
        self.mock(up, 'ensure_planner', return_value='idle')
        self.mock(crons, 'check', return_value=[])
        self.mock(look, '_read_state', return_value={'last_pass': NOW.isoformat()})
        self.mock(look, 'heartbeat_missed', return_value=None)
        self.mock(tick, '_spawn_busy', return_value=set())
        self.mock(tick.tree_cleanup, 'reap_worktrees', return_value=([], []))
        self.mock(reap_tmp, '_reap_recorded', return_value=[])
        self.mock(resources, 'manifest_counts', return_value={})
        self.mock(resources, 'not_reapable_counts', return_value={})
        self.redispatch = self.mock(autodispatch, 'redispatch_dead', return_value=[])
        self.mock(merge_queue, 'enqueue_due', return_value=[])
        self.mock(tick, 'launch_precheck', return_value=[])
        self.mock(loadgate, 'begin', return_value=None)
        self.mock(autodispatch, 'capacity', return_value=2)
        # Avoid the unrelated look cadence alerts; R4 persistence remains real.
        cron = self.root / 'state/cron-runs/look.jsonl'
        cron.parent.mkdir(parents=True)
        cron.write_text(json.dumps(dict(ev='start', ts=NOW.isoformat(), pid=1)) + '\n')

    def step(self, **kw):
        return replace(nextstep.Step(SID, 'spec', 'building', 'action', 'rebuild', 'tick',
                                     'fixture:1', 'fixture', None, row()['ts']), **kw)

    def heartbeat(self):
        return [e for e in self.read() if e['type'] == 'tick'][-1]

    def test_governed_pass_holds_remaining_seats_and_delayed_starts(self):
        self.isolate()
        cases = []
        # One ordinary _record per isolated lifecycle subject. Existing run_steps is real.
        scenarios = [('eligible', {}, None, None, 1),
                     ('dependency-held', {}, 'dependency fixture', None, 0),
                     ('standing-held', {}, 'held: ruling escalate', None, 0),
                     ('merges-first', dict(state='hold', action=None, clears='merge',
                                          reason='merge first'), None, None, 0),
                     ('base-refresh-executor', dict(state='hold', action=None, owner='executor',
                                                   clears='recut', reason='base-refresh: fixture'), None, None, 1),
                     ('delayed-start', {}, None, 'L-builder-9911', 0)]
        for index, (name, changes, hold, spawned, expected) in enumerate(scenarios):
            sid = 'L-spec-' + str(9920 + index)
            event = row(subject=sid)
            fixture = [row('spec-written', seconds=-4000, subject=sid), event]
            if name == 'dependency-held':
                fixture += [row('dependency-hold', seconds=-1700, subject=sid,
                                waits_for='L-spec-9999', why='fixture dependency')]
            if name == 'standing-held':
                fixture += [row('decision', seconds=-1700, subject=sid, action='hold', why='fixture ruling')]
            self.seed(fixture)
            step = replace(self.step(**changes), item=sid)
            with patch.object(tick, '_steps', return_value=[step]), \
                 patch.object(tick, 'launch', return_value=spawned):
                # A None launch is an intentional dispatch refusal. Eligible control
                # has executor ownership so it is not offered to a paid launcher.
                if name == 'eligible':
                    step = replace(step, owner='executor')
                    with patch.object(tick, '_steps', return_value=[step]):
                        tick._record()
                else:
                    tick._record()
            actual = len([e for e in self.alarms() if e['key'] == sid])
            self.assertEqual(actual, expected, name)
            self.assertNotIn('rework_wait_error', self.heartbeat())
            cases.append(dict(case=name, expected=expected, actual=actual,
                              heartbeat=self.heartbeat()))
            if hold:
                released = [*self.read(), row('unblocked', seconds=-1, subject=sid, actor='operator')]
                self.observe(released, steps=[replace(step, owner='executor')])
                alarm = next(e for e in self.alarms() if e['key'] == sid)
                self.assertEqual(alarm['reading']['since'], event['ts'])
        self.assertEqual(self.redispatch.call_count, len(scenarios))
        self.report('AC5-governed', cases)

    def test_resource_hold_saturation_and_slot_reservations(self):
        self.isolate()
        event = row()
        self.seed([event])
        step = self.step()
        class Gate:
            grades_first = False
            def check(self, *a, **kw):
                from types import SimpleNamespace
                return SimpleNamespace(hold=True)
            def finish(self):
                return {}
        import loadgate
        with patch.object(tick, '_steps', return_value=[step]), patch.object(loadgate, 'begin', return_value=Gate()):
            tick._record()
        self.assertEqual(self.alarms(), [])
        with patch.object(tick, '_steps', return_value=[step]), patch.object(autodispatch, 'capacity', return_value=0):
            tick._record()
        self.assertEqual(self.alarms(), [])
        second = row(subject='L-spec-9902')
        self.seed([event, second])
        steps = [step, replace(step, item='L-spec-9902', owner='executor')]
        with patch.object(tick, '_steps', return_value=steps), \
             patch.object(autodispatch, 'capacity', return_value=1), \
             patch.object(tick, 'launch', return_value='L-builder-9911'):
            tick._record()
        self.assertEqual(self.alarms(), [])  # reserved seat, no visible start yet
        self.seed([event])
        def start(*a, **kw):
            with patch.object(dispatch, 'now', return_value=(NOW + timedelta(seconds=1)).isoformat()):
                dispatch.emit(self.root / 'events/L-builder-9911.jsonl', {'subject': SID},
                              'build-started', spawn='L-builder-9911')
            return 'L-builder-9911'
        with patch.object(tick, '_steps', return_value=[step]), patch.object(tick, 'launch', side_effect=start):
            tick._record()
        self.assertEqual(self.alarms(), [])
        # The fresh start alone discharges the wait even without a launch result.
        self.assertEqual(self.waits(self.read()), [])
        self.report('AC5-seats', dict(alarms=0, heartbeat=self.heartbeat()))

    def test_observer_error_and_incomplete_pass_are_visible(self):
        self.isolate()
        self.seed([row()])
        step = self.step(owner='executor')
        with patch.object(tick, '_steps', return_value=[step]), \
             patch.object(look, 'emit_once', side_effect=RuntimeError('alarm door fixture')):
            tick._record()
        self.assertIn('alarm door fixture', self.heartbeat()['rework_wait_error'])
        self.assertEqual(self.redispatch.call_count, 1)
        self.assertFalse(self.heartbeat()['autodispatch_pass_failed'])
        with patch.object(tick, 'run_steps', side_effect=RuntimeError('before accounting')):
            tick._record()
        self.assertIn('remaining builder seats unavailable', self.heartbeat()['rework_wait_error'])
        self.assertTrue(self.heartbeat()['autodispatch_pass_failed'])
        self.assertEqual(self.alarms(), [])
        self.report('AC5-errors', self.heartbeat())


# Minimal supplied real lifecycle shapes. No live ledger read is involved.
HISTORY = [
    dict(type='verdict', subject='L-spec-0152', confirmed=False, ts='2026-09-22T08:45:45+00:00'),
    dict(type='shipped', subject='L-spec-0152', ts='2026-09-22T08:51:12+00:00'),
    dict(type='verdict', subject='L-spec-0146', confirmed=False, ts='2026-09-22T08:57:30+00:00'),
    dict(type='escalation-blocking', subject='L-spec-0146', ts='2026-09-22T09:03:30+00:00'),
    dict(type='conflict-rework', subject='L-spec-0275', verdict='escalate', ts='2026-09-24T10:11:35+00:00'),
    dict(type='verdict', subject='L-spec-0173', confirmed=False, ts='2026-09-27T01:06:00+00:00'),
    dict(type='spec-killed', subject='L-spec-0173', ts='2026-09-27T02:42:23+00:00'),
]


def historical_fixture():
    events = list(HISTORY)
    # Synthetic cap-pressure extensions of the supplied shapes, explicitly labelled.
    for n in range(51):
        sid = 'L-spec-' + str(8000 + n)
        start = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=n)
        def event(kind, minutes, **fields):
            return dict(type=kind, subject=sid, ts=(start + timedelta(minutes=minutes)).isoformat(), **fields)
        events += [event('verdict', 0, confirmed=False),
                   event(('shipped', 'spec-killed', 'spec-closed', 'verdict', 'escalation-blocking')[n % 5],
                         5, **(dict(confirmed=True) if n % 5 == 3 else {}))]
        if n % 5 in (0, 1, 2, 4):
            events += [event('conflict-rework', 10)]  # late receipt cannot reopen
        if n % 5 == 4:
            events += [event('unblocked', 15), event('verdict', 20, confirmed=False), event('shipped', 25)]
        if n % 5 == 2:
            events += [event('spec-written', 15), event('verdict', 20, confirmed=False), event('spec-closed', 25)]
    return sorted(events, key=lambda e: e['ts'])


class AC6Tests(Scratch):
    def test_redacted_history_first_observation_zero_budget_then_live_control(self):
        events = historical_fixture()
        subjects = sorted({e['subject'] for e in events})
        self.assertGreaterEqual(len(subjects), 51)
        self.seed(events)
        self.assertEqual(self.waits(events), [])
        self.observe(self.read())
        self.assertEqual(self.alarms(), [])
        files = self.fresh_files()
        self.assertEqual(sum(d['emitted'] for d in files['digests'].values()), 0)
        live = row()
        self.observe([*self.read(), live])
        self.assertEqual(len(self.alarms()), 1)
        self.observe([*self.read(), live])
        self.assertEqual(len(self.alarms()), 1)
        self.report('AC6-history', dict(fixture_only=True, subjects=len(subjects),
                                       enumeration={sid: [e for e in events if e['subject'] == sid]
                                                    for sid in subjects},
                                       historical_alarms=0, historical_budget=0, live_alarms=1))


if __name__ == '__main__':
    unittest.main()
