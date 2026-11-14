#!/usr/bin/env python3
"""Isolated assertion-bearing clock evidence; AC1/AC2/AC3/AC5 selectors."""
import atexit
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

_BOOT = tempfile.TemporaryDirectory(prefix='stall-alarm-import-')
atexit.register(_BOOT.cleanup)
os.environ['DOIT_ROOT'] = _BOOT.name
os.environ.pop('DOIT_PROJECT', None)
os.environ.pop('DOIT_LEDGER_FILE', None)

import close_reviews
import fold
import dispatch
import owed
import merge_queue
import relay
import stall_alarm as alarm

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CID = 'L-charter-9901'
SID = 'L-spec-9901'
REPO = Path(__file__).resolve().parent.parent


def event(kind, age=0, subject=CID, actor='planner', **kw):
    return dict(type=kind, subject=subject, actor=actor,
                ts=(NOW - timedelta(seconds=age)).isoformat(), **kw)


def reading(value):
    return dict(value=value, error=None)


class Clocks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='stall-alarm-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'logs').mkdir()
        (self.root / 'events').mkdir()
        (self.root / 'repos/fixture').mkdir(parents=True)
        self.thresholds = tomllib.loads((REPO / 'stall-thresholds.toml').read_text())
        self.state = {}
        self.events = [event('spawn-started', role='builder')]
        self.readings = dict(ready=reading([]), due=reading([]), queue=reading([]),
                             specs=reading({}), stalls=reading([]), owed=reading([]), autodispatch=reading([
                                 dict(ts=(NOW - timedelta(seconds=i)).isoformat(), elapsed_s=1)
                                 for i in (3, 2, 1)]))
        self.evidence = []

    def check(self, label, step, status, *, now=NOW, events=None):
        before = deepcopy((self.events, self.readings, self.thresholds))
        row = alarm.clocks(self.events if events is None else events, now,
                           state=self.state, thresholds=self.thresholds,
                           readings=self.readings)[step]
        self.assertEqual(row['status'], status, label + ': ' + str(row))
        self.assertTrue({'last_moved', 'pending', 'threshold_s', 'reason'} <= row.keys())
        self.assertEqual(before, (self.events, self.readings, self.thresholds))
        self.evidence.append(dict(label=label, step=step, clock=row))
        return row

    def test_AC1_starts_and_planning(self):
        self.readings['ready'] = reading([CID])
        self.events += [event('plan-written', 10800), event('planner-started', 21600)]
        self.check('plan exactly 3h/6h', 'plan', 'healthy')
        self.check('plan over 3h/6h', 'plan', 'alarm', now=NOW + timedelta(seconds=1))
        self.events = [event('planner-started', 21600), event('plan-written', 1, subject='L-charter-9902')]
        self.check('active planning exactly 6h', 'plan', 'healthy')
        self.check('active planning over 6h', 'plan', 'alarm', now=NOW + timedelta(seconds=1))
        self.check('planning alarm proven with no idle history', 'plan', 'alarm',
                   events=[event('planner-started', 21601)])
        for terminal in ('plan-written', 'l1-complete', 'charter-retracted', 'planner-ended'):
            self.events = [event('planner-started', 21601), event(terminal, 10), event('cut-written', 1)]
            self.check('ended planning: ' + terminal, 'plan', 'healthy')
        self.events = [event('planner-started', 21601), event('plan-written', 1, subject='L-charter-9902')]
        self.check('old planning despite recent other plan', 'plan', 'alarm')
        self.readings['ready'] = reading([])
        self.check('historical planning but no ready work', 'plan', 'healthy')
        self.readings['ready'] = reading([CID])
        self.events = []
        self.check('no prior plan movement', 'plan', 'undetermined')
        self.events = [event('plan-written', 10801), event('plan-written', -100)]
        self.check('future plan does not reset', 'plan', 'alarm')
        self.events += [dict(event('plan-written'), ts='invalid')]
        self.check('invalid relevant timestamp', 'plan', 'undetermined')
        self.readings['ready'] = dict(value=None, error='ready reader failed')
        self.check('failed ready reading', 'plan', 'undetermined')

        self.readings['ready'] = reading([])
        for kind in ('spawn-started', 'build-started'):
            for actor in ('builder', 'executor', 'planner', 'tick', 'operator', 'research'):
                rows = [event(kind, 2700, actor=actor),
                        event('spawn-done', 1), event('spawn-refused', 1),
                        event('spawn-heartbeat', 1), event('build-done', 1)]
                # Actual fold admission, including actors with no start-specific policy.
                _, _, ignored, admitted = fold.fold(rows)
                self.assertIn(rows[0], admitted[CID])
                accepted = sorted((e for evs in admitted.values() for e in evs), key=lambda e: e['ts'])
                self.check(f'{kind}/{actor} exactly 45m', 'dispatch', 'healthy', events=accepted)
                self.check(f'{kind}/{actor} over 45m', 'dispatch', 'alarm',
                           events=accepted, now=NOW + timedelta(seconds=1))
        self.check('no admitted start', 'dispatch', 'undetermined', events=[])
        self.check('future start ignored', 'dispatch', 'alarm', events=[
            event('build-started', 2701), event('spawn-started', -1)])
        self.check('bad start timestamp', 'dispatch', 'undetermined', events=[
            dict(event('spawn-started'), ts='bad')])

    def test_AC1_real_readers(self):
        rows = [event('charter-filed', 200000, actor='operator', project='fixture', covers='none'),
                event('l1-complete', 100000),
                event('spec-written', 90000, subject=SID, charter=CID, project='fixture'),
                event('shipped', 80000, subject=SID, actor='executor'),
                event('spawn-started', 86400, subject='L-charter-9902', role='charter-reviewer')]
        specs, _, _, by_subject = fold.fold(sorted(rows, key=lambda e: e['ts']))
        admitted = sorted((e for evs in by_subject.values() for e in evs), key=lambda e: e['ts'])
        due = close_reviews.due(admitted, specs, NOW)
        self.assertEqual(due, [CID])
        self.readings['due'] = reading(due)
        self.check('real due, exactly 24h', 'close', 'healthy', events=admitted)
        self.check('real due, over 24h', 'close', 'alarm', events=admitted,
                   now=NOW + timedelta(seconds=1))
        self.check('due but no prior reviewer start', 'close', 'undetermined', events=[])
        # SD4: not-complete remains owed, but has no new shipment to justify a retry.
        admitted.append(event('charter-review-not-complete', 10, actor='charter-reviewer'))
        self.assertTrue(fold.charter_review_owed([e for e in admitted if e['subject'] == CID]))
        self.assertEqual(close_reviews.due(admitted, specs, NOW), [])
        self.readings['due'] = reading(close_reviews.due(admitted, specs, NOW))
        self.check('SD4 owed but due-empty wait', 'close', 'healthy', events=admitted)
        self.readings['due'] = dict(value=None, error='due reader failed')
        self.check('failed due reading', 'close', 'undetermined')
        planner_rows = [event('charter-filed', 30000, actor='operator', project='fixture', covers='none'),
                        event('planner-started', 21601)]
        ready, _ = relay.plannable(planner_rows, root=self.root)
        self.assertEqual(ready, [CID])
        self.readings['ready'] = reading(ready)
        self.check('real relay eligibility', 'plan', 'alarm', events=planner_rows + [
            event('plan-written', 1, subject='L-charter-9902')])
        planner_rows.append(event('charter-retracted', 1, actor='operator'))
        ready, _ = relay.plannable(planner_rows, root=self.root)
        self.assertEqual(ready, [])
        self.readings['ready'] = reading(ready)
        self.check('real relay retraction', 'plan', 'healthy', events=planner_rows)

    def passes(self, durations, age=1):
        return [dict(ts=(NOW - timedelta(seconds=age + len(durations) - i - 1)).isoformat(), elapsed_s=n)
                for i, n in enumerate(durations)]

    def test_AC1_autodispatch(self):
        for values, age, status in [
                ([901, 901, 901], 1, 'alarm'), ([901, 900, 901], 1, 'healthy'),
                ([900, 900, 900], 1800, 'healthy'), ([1, 1, 1], 1801, 'alarm'),
                ([1], 1801, 'alarm'), ([1, 1], 1801, 'alarm'),
                ([901], 1, 'undetermined'), ([1, 1], 1, 'undetermined'),
                ([], 1, 'undetermined')]:
            self.readings['autodispatch'] = reading(self.passes(values, age))
            self.check(f'passes={values}, newest age={age}', 'autodispatch', status)
        self.readings['autodispatch'] = reading(self.passes([901, 901, 901]) + self.passes([1], -1))
        self.check('future fast completion ignored', 'autodispatch', 'alarm')
        self.readings['autodispatch'] = reading([dict(ts='bad', elapsed_s=1)])
        self.check('invalid completion timestamp', 'autodispatch', 'undetermined')
        self.readings['autodispatch'] = dict(value=None, error='log unavailable')
        self.check('failed log reader', 'autodispatch', 'undetermined')

        live = self.root / 'logs/lane-driver.log'
        rotated = live.with_suffix('.log.1')
        def line(age, duration):
            return f'{(NOW - timedelta(seconds=age)).isoformat()} snapshot=2000-01-01T00:00:00Z finished autodispatch elapsed_s={duration}\n'
        old, middle, latest = line(3, 901), line(2, 901), line(1, 901)
        rotated.write_text(middle + old)
        live.write_text(middle + latest + f'{NOW.isoformat()} snapshot=ignored started autodispatch\n')
        self.readings['autodispatch'] = alarm.read_autodispatch(self.root, NOW)
        self.assertEqual(len(self.readings['autodispatch']['value']), 3)
        self.check('rotation-spanning deduplicated chronological triple', 'autodispatch', 'alarm')
        live.write_text(line(1801, 1) + f'{NOW.isoformat()} snapshot=x finished autodispatch elapsed_s=')
        rotated.unlink()
        self.readings['autodispatch'] = alarm.read_autodispatch(self.root, NOW)
        self.check('fresh file mtime/partial line cannot reset stale pass', 'autodispatch', 'alarm')
        rotated.write_text(old)
        rotated.chmod(0)
        try:
            self.assertIsNotNone(alarm.read_autodispatch(self.root, NOW)['error'])
        finally:
            rotated.chmod(0o600)
        with patch.object(Path, 'read_text', side_effect=OSError('read failed')):
            self.assertIsNotNone(alarm.read_autodispatch(self.root, NOW)['error'])
        live.unlink()
        self.assertIsNotNone(alarm.read_autodispatch(self.root, NOW)['error'])

    def test_AC1_configuration(self):
        expected = dict(plan_idle=10800, plan_in_planning=21600, dispatch=2700, close=86400,
                        autodispatch=900, autodispatch_idle=1800, merge=7200, build=21600,
                        grade=21600, review=21600, deploy=86400, owed_checks=86400, realert=10800)
        self.assertEqual(self.thresholds, dict(expected, deploy_targets={'albert-scott': 'albert-scott/droplet'}))
        for key in expected:
            for value in (None, 0, -1, True, '900', float('inf'), float('nan')):
                self.thresholds = dict(expected, **{key: value})
                if value is None:
                    del self.thresholds[key]
                # Do not compare NaN through check's mutation assertion.
                rows = alarm.clocks(self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)
                self.assertTrue(all(r['status'] == 'undetermined' for r in rows.values()), (key, value))
        self.thresholds = dict(expected, dispatch=1)
        self.check('independently configured dispatch threshold', 'dispatch', 'alarm',
                   events=[event('build-started', 2)])
        self.thresholds = expected

    def test_AC3_continuity(self):
        self.readings['specs'] = reading({SID: dict(state='building')})
        self.check('first sample initializes', 'merge', 'healthy')
        for minutes in range(15, 121, 15):
            self.check(f'continuous empty +{minutes}m', 'merge', 'healthy', now=NOW + timedelta(minutes=minutes))
        self.check('strictly over 2h', 'merge', 'alarm', now=NOW + timedelta(seconds=7201))
        # Test-owned persistence, then a new interpreter consuming the public pure seam.
        path = self.root / 'state.json'
        path.write_text(json.dumps(self.state))
        code = '''import json,sys
from datetime import datetime
import stall_alarm
state=json.load(open(sys.argv[1]))
print(json.dumps(stall_alarm.clocks([],datetime.fromisoformat(sys.argv[2]),state=state,thresholds=json.loads(sys.argv[3]),readings=json.loads(sys.argv[4]))['merge']))
'''
        result = subprocess.run(['/usr/bin/python3', '-c', code, str(path),
                                 (NOW + timedelta(seconds=7202)).isoformat(), json.dumps(self.thresholds),
                                 json.dumps(self.readings)], cwd=REPO / 'src', env=dict(os.environ),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'alarm')
        self.evidence.append(dict(label='fresh process reads saved samples', clock=json.loads(result.stdout)))
        rows = [event('queue-add', subject=SID, actor='executor', branch='fixture', project='fixture')]
        self.readings['queue'] = reading(merge_queue.queue(rows))
        self.assertEqual([row['spec'] for row in self.readings['queue']['value']], [SID])
        self.check('real nonempty queue recovers', 'merge', 'healthy', now=NOW + timedelta(seconds=7203))
        self.assertEqual(self.state['merge_samples'], [])
        self.readings['queue'] = reading([])
        self.check('empty restarts after nonempty', 'merge', 'healthy', now=NOW + timedelta(seconds=7204))

    def test_AC3_breaks(self):
        self.readings['specs'] = reading({SID: dict(state='building')})
        self.check('initial empty', 'merge', 'healthy')
        self.check('exactly two periods preserves continuity', 'merge', 'healthy', now=NOW + timedelta(seconds=1800))
        self.assertEqual(len(self.state['merge_samples']), 2)
        self.check('gap over two periods restarts', 'merge', 'healthy', now=NOW + timedelta(seconds=3601))
        self.assertEqual(self.state['merge_samples'], [(NOW + timedelta(seconds=3601)).isoformat()])
        for name in ('queue', 'specs'):
            saved = self.readings[name]
            self.readings[name] = dict(value=None, error=name + ' failed')
            self.check(name + ' failure breaks continuity', 'merge', 'undetermined', now=NOW + timedelta(seconds=3602))
            self.assertEqual(self.state['merge_samples'], [])
            self.readings[name] = saved
            self.check(name + ' recovery starts again', 'merge', 'healthy', now=NOW + timedelta(seconds=3603))
        self.readings['specs'] = reading({SID: dict(state='graded')})
        self.check('no building work breaks continuity', 'merge', 'healthy', now=NOW + timedelta(seconds=3604))
        self.assertEqual(self.state['merge_samples'], [])
        self.readings['specs'] = reading({SID: dict(state='building')})
        self.state['merge_samples'] = ['bad']
        self.check('malformed saved sample', 'merge', 'undetermined')
        self.assertEqual(self.state['merge_samples'], [])
        self.state['merge_samples'] = [(NOW + timedelta(seconds=1)).isoformat()]
        self.check('future sample cannot manufacture continuity', 'merge', 'healthy')
        self.assertEqual(self.state['merge_samples'], [NOW.isoformat()])


    def consumers(self, rows):
        """Write scratch actor files, independently normalize/admit, then read consumers."""
        grouped = {}
        for row in rows:
            row = dict(row)
            actor = row.pop('actor')
            grouped.setdefault(actor, []).append(row)
        for path in (self.root / 'events').glob('*.jsonl'):
            path.unlink()
        for actor, evs in grouped.items():
            (self.root / 'events' / f'L-{actor}-fixture.jsonl').write_text(
                ''.join(json.dumps(e) + '\n' for e in evs))
        with patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', self.root / 'events'), \
             patch.object(fold, 'PROJECT', None), patch.object(fold, 'NOW', NOW), \
             patch.object(fold, 'SESSIONS', self.root / 'sessions'), \
             patch.object(dispatch, 'CONTENT', self.root / 'content'):
            events = fold.read_events(use_cache=False)
            by = fold.fold(events)[3]
            self.events = sorted((e for evs in by.values() for e in evs), key=lambda e: e['ts'])
            self.readings.update(alarm.read_consumers(self.events, NOW))
        return self.readings['stalls']['value']

    def test_AC2_real_census_states_roles_and_telemetry(self):
        rows = []
        cases = [([], 'written', 'build'),
                 ([('build-started', 'builder', {})], 'building', 'build'),
                 ([('build-done', 'builder', {'status': 'DONE'})], 'graded', 'grade'),
                 ([('verdict', 'grader', {'confirmed': True})], 'reviewing', 'review'),
                 ([('build-done', 'builder', {'status': 'BLOCKED'}),
                   ('decision', 'thinker', {'rework': 'yes'})], 'blocked', 'build'),
                 ([('build-done', 'builder', {'status': 'BLOCKED'}),
                   ('decision', 'thinker', {'regrade': 'yes'})], 'blocked', 'grade'),
                 ([('build-done', 'builder', {'status': 'BLOCKED'}),
                   ('cannot-assess', 'grader', {})], 'blocked', 'review'),
                 ([('verdict', 'grader', {'confirmed': True}),
                   ('decision', 'thinker', {'regrade': 'yes'})], 'reviewing', 'grade'),
                 ([('verdict', 'grader', {'confirmed': True}),
                   ('decision', 'thinker', {'rework': 'yes'})], 'reviewing', 'build')]
        for n, (history, state, stage) in enumerate(cases):
            sid = f'L-spec-{9910+n}'
            rows.append(event('spec-written', 23000, sid))
            for i, (kind, actor, fields) in enumerate(history):
                rows.append(event(kind, 22000-i, sid, actor, **fields))
            rows.append(event('build-stub', 1, sid, 'builder', path='diagnostic', why='telemetry'))
        census = self.consumers(rows)
        self.assertIsNotNone(census, self.readings)
        self.assertEqual({r['state'] for r in census}, {'written', 'building', 'graded', 'reviewing', 'blocked'})
        for n, (_, state, stage) in enumerate(cases):
            sid = f'L-spec-{9910+n}'
            self.assertEqual(self.readings['specs']['value'][sid]['state'], state)
            clock = self.check(f'real {state}/{stage}/{sid} with fresh diagnostic', stage, 'alarm')
            self.assertIn(sid, clock['pending'])
            self.assertLess(datetime.fromisoformat(clock['last_moved']), NOW-timedelta(hours=6))
        before = alarm.clocks(self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)
        self.thresholds['grade'] = 30000
        after = alarm.clocks(self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)
        self.assertEqual(after['grade']['status'], 'healthy')
        for step in ('build', 'review', 'deploy', 'owed_checks'):
            self.assertEqual(before[step], after[step])

    def test_AC2_real_census_buckets_and_merge_waits(self):
        rows = []
        buckets = ('STUCK', 'held', 'audit-wait', 'retry-wait', 'in-flight', 'queued', 'waiting-on-human')
        for n in range(7):
            rows.append(event('spec-written', 2400 if n != 5 else 600,
                              f'L-spec-{9940+n}', 'spec-writer' if n == 2 else 'planner'))
        rows += [event('decision', 300, 'L-spec-9941', 'operator', why='Held on purpose: operator hold'),
                 event('spec-written', 1740, 'L-spec-9942', 'spec-writer'),
                 event('spawn-failed', 180, 'L-spec-9943', 'executor', spawn='L-builder-9943', role='builder', reason='capacity'),
                 event('spawn-started', 180, 'L-spec-9944', 'executor', spawn='L-builder-9944', role='builder'),
                 event('question', 120, 'L-spec-9946', 'builder', asks='access', deadline=(NOW+timedelta(hours=1)).isoformat())]
        census = self.consumers(rows)
        self.assertEqual({r['spec']: r['bucket'] for r in census},
                         {f'L-spec-{9940+n}': bucket for n, bucket in enumerate(buckets)})
        self.thresholds['build'] = 60
        row = self.check('all seven real buckets retained with reasons', 'build', 'alarm')
        self.assertEqual(set(row['pending']), {r['spec'] for r in census})
        self.assertEqual({r['bucket'] for r in row['items']}, set(buckets))
        for item in row['items']:
            self.assertTrue(item['reason'])
            self.assertIn('blocker', item)
            self.assertIn('deadline', item)
        rows = [event('spec-written', 40000, SID), event('build-done', 39000, SID, 'builder', status='DONE'),
                event('verdict', 38000, SID, 'grader', confirmed=True),
                event('review', 37000, SID, 'reviewer', n_blocking=0)]
        for extra in ([], [event('window-opened', 36000, 'win-fixture', 'executor', specs=[SID])]):
            self.consumers(rows + extra)
            for stage in ('build', 'grade', 'review'):
                self.assertEqual(self.check('merge-ready excluded from '+stage, stage, 'healthy')['pending'], [])
            self.assertEqual(self.check('merge-ready diagnostics retained', 'merge', 'healthy')['waiting'][0]['spec'], SID)
        for kind, actor in [('shipped', 'executor'), ('spec-killed', 'spec-writer'), ('spec-closed', 'operator')]:
            self.assertEqual(self.consumers(rows+[event(kind, 10, SID, actor)]), [])
            self.check('terminal '+kind, 'build', 'healthy')

    def test_AC2_deploy_targets_boundaries_and_resets(self):
        self.events = [event('shipped', 86400, SID, 'executor', project='albert-scott', sha='merge-head'),
                       event('shipped', 90000, 'L-spec-9902', 'executor', project='do-it-v2')]
        row = self.check('deploy exactly 24h', 'deploy', 'healthy')
        self.assertEqual(row['projects']['do-it-v2']['status'], 'not-applicable')
        self.check('deploy strictly over 24h, no landed history', 'deploy', 'alarm', now=NOW+timedelta(seconds=1))
        for age, status in [(86401, 'alarm'), (86400, 'healthy'), (86399, 'healthy')]:
            self.check('target landed earlier/equal/later '+str(age), 'deploy', status,
                       now=NOW+timedelta(seconds=1), events=self.events+[
                           event('deploy-landed', age, 'auto-deploy', 'deployer', target='albert-scott/droplet',
                                 project='hashed-project', sha='different-deploy-head')])
        self.events[0]['ts'] = (NOW-timedelta(seconds=86401)).isoformat()
        self.events += [event('deploy-landed', 1, 'auto-deploy', 'deployer', target='elsewhere'),
                        event('deploy-started', 1, actor='deployer', target='albert-scott/droplet'),
                        event('deploy-failed', 1, actor='deployer', target='albert-scott/droplet')]
        self.check('wrong target/start/failure do not clear', 'deploy', 'alarm')
        self.events.append(event('shipped', 1, 'L-spec-9903', 'executor', project='albert-scott'))
        row = self.check('newer shipment resets project clock', 'deploy', 'healthy')
        self.assertEqual(row['pending'], [SID, 'L-spec-9903'])
        self.thresholds['deploy_targets']['fixture'] = 'fixture/production'
        self.events.append(event('shipped', 86401, 'L-spec-9904', 'executor', project='fixture'))
        row = self.check('independent configured project target', 'deploy', 'alarm')
        self.assertEqual(row['projects']['albert-scott']['status'], 'healthy')
        self.thresholds['deploy_targets'] = {}
        self.check('known production route missing association', 'deploy', 'undetermined')

    def test_AC2_real_owed_isolation_and_movement(self):
        rows = []
        for n in range(2):
            sid = f'L-spec-{9970+n}'
            rows += [event('spec-written', 200000, sid, project='fixture'),
                     event('owed-ac', 190000, sid, 'spec-writer', criterion='AC1', wake_at=(NOW-timedelta(seconds=190000)).isoformat()),
                     event('shipped', 86400, sid, 'executor'),
                     event('sweep-run', 1, sid, 'executor', outcome='checked')]
        self.consumers(rows)
        self.assertEqual(len(self.readings['owed']['value']), 2)
        row = self.check('owed exactly 24h', 'owed_checks', 'healthy')
        self.assertEqual(row['last_moved'], (NOW-timedelta(seconds=86400)).isoformat())
        row = self.check('owed strictly over 24h', 'owed_checks', 'alarm', now=NOW+timedelta(seconds=1))
        self.assertEqual(row['pending'], ['L-spec-9970', 'L-spec-9971'])
        rows.append(event('owed-met', 10, 'L-spec-9970', 'executor', criterion='AC1', evidence='scratch'))
        self.consumers(rows)
        row = self.check('one AC1 met does not clear other spec AC1', 'owed_checks', 'alarm', now=NOW+timedelta(seconds=1))
        self.assertEqual(row['pending'], ['L-spec-9971'])
        self.assertEqual(row['excluded_counts'], {'met': 1})
        rows.append(event('owed-ac', 5, 'L-spec-9971', 'executor', criterion='AC1', wake_at=(NOW+timedelta(days=2)).isoformat()))
        self.consumers(rows)
        self.check('re-dated future owed check is not pending', 'owed_checks', 'healthy')

    def test_AC5_named_reading_fault_matrix(self):
        dependencies = dict(ready=('plan',), due=('close',), queue=('merge',), specs=('merge','build','grade','review'),
                            stalls=('build','grade','review'), owed=('owed_checks',), autodispatch=('autodispatch',),
                            state=('merge',), config=alarm.STEPS, ledger=tuple(s for s in alarm.STEPS if s != 'autodispatch'))
        baseline = deepcopy(self.readings)
        for source, steps in dependencies.items():
            self.readings = dict(baseline, **{source: dict(value=None, error='private untrusted diagnostic')})
            decisions = alarm.clocks(self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)
            for step in steps:
                self.assertEqual(decisions[step]['status'], 'undetermined', (source, step, decisions))
            faults = alarm.failed_readings(self.readings, decisions)
            self.assertIn(source, faults)
            self.assertNotIn('private untrusted diagnostic', json.dumps((decisions, faults)))
            if source not in ('ledger', 'config'):
                self.assertEqual(decisions['dispatch']['status'], 'healthy')
            self.evidence.append(dict(label='failed '+source, steps=list(steps), failures=faults))
        self.readings = baseline
        self.assertEqual(alarm.failed_readings(self.readings, alarm.clocks(
            self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)), {})
        for source in ('stalls', 'owed'):
            for bad in (None, {}, [None]):
                self.readings = dict(baseline, **{source: reading(bad)})
                self.check('malformed '+source, 'build' if source=='stalls' else 'owed_checks', 'undetermined')
        self.readings = baseline
        self.events.append(dict(event('shipped', project='albert-scott'), ts='bad'))
        self.check('invalid relevant shipment timestamp', 'deploy', 'undetermined')
        self.consumers([event('spec-written', 25000, SID)])
        self.readings['stalls']['value'][0]['since'] = None
        self.check('missing census since cannot look healthy', 'build', 'undetermined')
        self.readings['stalls']['value'][0]['since'] = (NOW+timedelta(seconds=1)).isoformat()
        self.check('future census since cannot reset', 'build', 'undetermined')
        self.readings['stalls']['value'][0].update(since=(NOW-timedelta(hours=7)).isoformat(),
                                                   bucket='in-flight', owner='executor')
        self.check('generic executor is not an active role', 'build', 'undetermined')
        self.readings['stalls']['value'][0]['owner'] = 'spec-auditor'
        self.check('active spec auditor belongs to build', 'build', 'alarm')
        self.readings['owed'] = reading([dict(status='due', spec=SID, criterion='AC1', project='fixture',
                                            due_at='bad', last_moved=NOW.isoformat())])
        self.check('invalid due timestamp cannot look healthy', 'owed_checks', 'undetermined')

    def test_AC5_log_state_and_configuration_faults(self):
        self.readings['autodispatch'] = alarm.read_autodispatch(self.root, NOW)
        self.check('missing live log, including fresh state', 'autodispatch', 'undetermined')
        live = self.root / 'logs/lane-driver.log'
        rotated = live.with_suffix('.log.1')
        live.write_text('invalid snapshot=x finished autodispatch elapsed_s=1\n')
        self.readings['autodispatch'] = alarm.read_autodispatch(self.root, NOW)
        self.check('malformed completed log timestamp', 'autodispatch', 'undetermined')
        live.write_text(f'{NOW.isoformat()} snapshot=x finished autodispatch elapsed_s=1\n')
        rotated.write_text('old log')
        rotated.chmod(0)
        try:
            self.readings['autodispatch'] = alarm.read_autodispatch(self.root, NOW)
            self.check('unreadable rotated log', 'autodispatch', 'undetermined')
        finally:
            rotated.chmod(0o600)
        self.readings['specs'] = reading({SID: dict(state='building')})
        self.state['merge_samples'] = ['bad']
        self.check('malformed saved merge state', 'merge', 'undetermined')
        self.assertEqual(self.state['merge_samples'], [])
        original = deepcopy(self.thresholds)
        for duration in alarm.DURATIONS:
            for invalid in (None, 0, -1, True, '1'):
                self.thresholds = dict(original, **{duration: invalid})
                if invalid is None:
                    del self.thresholds[duration]
                decisions = alarm.clocks(self.events, NOW, state={}, thresholds=self.thresholds, readings=self.readings)
                self.assertEqual(set(decisions), set(alarm.STEPS))
                self.assertTrue(all(r['status'] == 'undetermined' for r in decisions.values()))
            self.evidence.append(dict(label='missing/nonpositive/wrong-type duration '+duration,
                                      affected=list(alarm.STEPS)))
        self.thresholds = original
        self.thresholds['deploy_targets'] = {'albert-scott': None}
        self.check('malformed configured target', 'deploy', 'undetermined')

    def test_AC5_consumer_errors_and_future_controls(self):
        rows = [event('spec-written', 25000, SID)]
        self.consumers(rows)
        with patch.object(fold, 'stall_rows', side_effect=OSError('untrusted detail')):
            readings = alarm.read_consumers(self.events, NOW)
        self.assertIsNotNone(readings['stalls']['error'])
        self.assertIsNone(readings['owed']['error'])
        with patch.object(owed, 'checks', side_effect=RuntimeError('untrusted detail')):
            readings = alarm.read_consumers(self.events, NOW)
        self.assertIsNotNone(readings['owed']['error'])
        self.assertIsNone(readings['stalls']['error'])
        self.consumers(rows+[event('shipped', -1, SID, 'executor')])
        self.check('future shipment cannot hide old build wait', 'build', 'alarm')
        for row in (dict(event('spec-written', subject=SID), ts='bad'),
                    dict(event('owed-ac', subject=SID, criterion='AC1'), ts='bad')):
            readings = alarm.read_consumers([row], NOW)
            self.assertIsNotNone(readings['specs']['error'])
            self.assertIsNotNone(readings['owed']['error'])
        self.evidence.append(dict(label='real consumer exceptions isolated, future movement ignored, bad timestamps fail closed'))


def main():
    groups = sys.argv[1:] or ['AC1', 'AC2', 'AC3', 'AC5']
    if any(group not in ('AC1', 'AC2', 'AC3', 'AC5') for group in groups):
        return 2
    for group in groups:
        names = [n for n in unittest.defaultTestLoader.getTestCaseNames(Clocks) if n.startswith('test_' + group + '_')]
        if not names:
            return 2
        tests = [Clocks(name) for name in names]
        result = unittest.TextTestRunner().run(unittest.TestSuite(tests))
        if not result.wasSuccessful():
            return 1
        evidence = [r for test in tests for r in test.evidence]
        body = json.dumps(evidence, sort_keys=True)
        print(f'L-spec-2424 {group} ok: {len(names)} tests, {len(evidence)} fixture decisions')
        print(json.dumps(dict(url='cli://stall-alarm/' + group, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=evidence)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
