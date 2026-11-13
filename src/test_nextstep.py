"""Regression tests for the pure next-step snapshot and read-only CLI."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import nextstep as ns
import dispatch
import fold
import lane_rules as lr


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
SID = 'L-spec-9001'
KEYS = {'item', 'item_kind', 'stage', 'state', 'action', 'owner',
        'trigger', 'reason', 'clears', 'since', 'attempt', 'retry_at'}
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'src/fixtures/nextstep'


def event(kind, minutes=-30, subject=SID, **extra):
    return dict(type=kind, ts=(NOW + timedelta(minutes=minutes)).isoformat(),
                subject=subject, _src=f'L-operator-fixture.jsonl:{minutes + 20000}', actor='operator') | extra


def specs_for(events):
    return {sid: dict(state='written') for sid in {e['subject'] for e in events}
            if sid.startswith('L-spec-')}


def replay(name):
    directory = FIXTURES / name
    events = [json.loads(l) for l in (directory / 'events.jsonl').read_text().splitlines()]
    lines = (directory / 'source.txt').read_text().splitlines()
    now = datetime.fromisoformat(lines[0].split('cut: ')[1])
    # The caller supplies fold state; replay time never comes from fold.NOW.
    events += [dict(type='spawn-done', subject=e['subject'], ts=e['ts'],
                    actor='spec-auditor', bad_cut=False, _src='L-spec-auditor-fixture.jsonl:1')
               for e in list(events) if e['type'] == 'spec-written']
    normalized = ns._snapshot(events, {})[0]
    with patch.object(fold, 'NOW', now):
        specs = fold.fold(normalized)[0]
    waves = json.loads((directory / 'waves.json').read_text())
    return events, specs, now, waves


_REAL_OPEN = open


def allow_dependency_reads(stack):
    """Permit only the read-only dependency lookup and start-refusal lock read under the scratch root."""
    content = dispatch.CONTENT
    def read_only(file, mode='r', *args, **kwargs):
        # The shared start refusal observes the subject lock under state/.
        if (not (Path(file).is_relative_to(content) or Path(file).is_relative_to(Path(fold.ROOT) / 'state')
                    or str(file) == '/proc/locks')
                or mode not in ('r', 'rb')):
            raise AssertionError('unexpected filesystem access')
        return _REAL_OPEN(file, mode, *args, **kwargs)
    for hook in ('builtins.open', 'io.open', 'pathlib.Path.open'):
        stack.enter_context(patch(hook, new=read_only))


class NextStepTests(unittest.TestCase):
    def test_current_blocked_build_never_grades_stale_success(self):
        from test_fold_stalls import admitted, row, outcome, NOW as cut, SID as sid, evidence
        checked = []
        for tied in (False, True):
            minute = 0 if tied else 1
            for history, expected_grade in [([outcome('BLOCKED')], False),
                    ([outcome('DONE'), outcome('BLOCKED', minute=minute)], False),
                    ([outcome('DONE'), outcome('BLOCKED', minute=minute), outcome('DONE', minute=minute + 1)], True)]:
                with admitted([row('spec-written', 'planner', minute=-10), *history]) as (_, events, folded):
                    step = next(s for s in self.steps(events, folded[0], now=cut) if s.item == sid)
                    self.assertNotIn('unknown spec state', step.reason)
                    self.assertEqual(step.action == 'grade', expected_grade, step)
                    checked.append(asdict(step))
        evidence('AC6', checked)

    def setUp(self):
        self.rules = lr.load()
        temporary = tempfile.TemporaryDirectory(prefix='nextstep-content-')
        self.addCleanup(temporary.cleanup)
        self.content = Path(temporary.name)
        content_patch = patch.object(dispatch, 'CONTENT', self.content)
        content_patch.start()
        self.addCleanup(content_patch.stop)

    def steps(self, events, specs=None, now=NOW, rules=None, waves=None):
        return ns.next_steps(events, specs if specs is not None else specs_for(events),
                             now, self.rules if rules is None else rules, waves)

    def one(self, events, specs=None, **kwargs):
        return next(s for s in self.steps(events, specs, **kwargs) if s.item == SID)

    def test_vanished_first_build_keeps_original_trigger_and_backoff(self):
        written = event('spec-written', -90)
        start = event('build-started', -10, spawn='L-builder-9861', role='builder')
        failed = event('spawn-failed', -5, spawn=start['spawn'], role='builder',
                       reason='vanished', actor='tick') | {'_src': 'L-tick-local.jsonl:1'}
        evs = [written, start, failed]
        self.assertEqual(ns._failure(evs, self.rules)[1], 'retry')
        step = self.one(evs, {SID: dict(state='building')})
        self.assertEqual((step.action, step.since), ('build', written['ts']))
        waiting = self.one(evs, {SID: dict(state='building')}, now=NOW-timedelta(seconds=1))
        self.assertIsNone(waiting.action)
        self.assertIn('next_retry_at', waiting.reason)

    def test_ac1_frozen_validated_shape(self):
        step = ns.Step(SID, 'spec', 'planned', 'hold', None, 'executor',
                       None, 'waiting', 'decision', NOW.isoformat())
        self.assertEqual({f.name for f in fields(step)}, KEYS)
        self.assertIsNot(replace(step), step)
        with self.assertRaises(FrozenInstanceError):
            step.owner = 'tick'
        for change in ({'state': 'action'}, {'reason': ''}, {'clears': None},
                       {'owner': 'nobody'}, {'item_kind': 'row'},
                       {'stage': 'done'}, {'state': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(step, **change)

    def test_ac2_live_item_identity_and_order(self):
        events = [event('charter-filed', subject='L-charter-9001')]
        states = ['written', 'building', 'graded', 'reviewing', 'unknown', *dispatch.SETTLED_STATES]
        specs = {}
        expected = {('charter', 'L-charter-9001'), ('attempt', 'L-builder-9001'),
                    ('window', 'win-fixture')}
        for i, state in enumerate(states):
            sid = f'L-spec-{9100 + i}'
            specs[sid] = dict(state=state)
            events.append(event('spec-written', subject=sid))
            if state not in dispatch.SETTLED_STATES:
                expected.add(('spec', sid))
            if state in ns.OWED_STATES:
                events += [event('owed-ac', -25, subject=sid, criterion='AC1', wake_at=event('x', -24)['ts']),
                           event('shipped', -20, subject=sid)]
                expected.add(('check', sid + ':AC1'))
        events += [event('build-started', -2, subject='L-spec-9101', spawn='L-builder-9001'),
                   event('window-opened', -3, subject='win-fixture')]
        baseline = self.steps(events, specs)
        identities = [(s.item_kind, s.item) for s in baseline]
        self.assertEqual(set(identities), expected)
        self.assertEqual(len(identities), len(expected))
        self.assertEqual(identities, sorted(identities))
        self.assertEqual(baseline, self.steps(events, specs))
        random.Random(41).shuffle(events)
        self.assertEqual(baseline, self.steps(events, specs))

    def test_charter_reviewer_spawn_is_never_a_charter(self):
        charter = 'L-charter-9001'
        for subject in ('L-charter-reviewer-0008', 'L-charter-008', 'L-charter-00008',
                        'L-charter-0008-extra', 'L-charter-0008\n'):
            for timestamp in (NOW.isoformat(), 'invalid-time'):
                with self.subTest(subject=subject, timestamp=timestamp):
                    events = [event('charter-filed', subject=charter),
                              event('spawn-done', subject=subject, spawn=subject, ts=timestamp)]
                    steps = self.steps(events, {})
                    self.assertEqual([(s.item_kind, s.item) for s in steps], [('charter', charter)])
                    self.assertEqual(steps[0].reason, 'plan not written')

    def test_cancelled_window_is_terminal(self):
        cancelled, waiting = 'win-cancelled', 'win-waiting'
        events = [event('window-opened', -30, subject=window)
                  for window in (cancelled, waiting)]
        events.append(event('window-cancelled', -20, subject=cancelled,
                            after='win-successor', reason='superseded'))

        steps = self.steps(events)

        self.assertFalse(any(step.item == cancelled for step in steps))
        remaining = [step for step in steps if step.item == waiting]
        self.assertEqual(len(remaining), 1)
        self.assertEqual((remaining[0].item_kind, remaining[0].owner, remaining[0].reason),
                         ('window', 'executor', 'window waiting; elapsed_min=30'))

    def selected(self, results, rules=None):
        predicates = {n: (lambda c, n=n: results.get(n)) for n in ns.PREDICATES}
        with patch.dict(ns.PREDICATES, predicates, clear=True):
            return self.one([event('spec-written')], rules=rules)

    def test_ac3_predicate_precedence_and_conflicts(self):
        row = dict(since=event('x')['ts'], reason='ready')
        for actions, expected in [(['build'], 'build'), (['build', 'rebuild'], 'build'),
                                  (['grade', 'regrade'], 'grade')]:
            results = {n: dict(row) for n in actions}
            results.update({'wave-held': dict(row, blocker='L-spec-9002'), 'idle': row})
            step = self.selected(results)
            self.assertEqual((step.state, step.action), ('action', expected))
        conflict = self.selected({'build': row, 'grade': row})
        self.assertTrue(conflict.reason.startswith('undetermined: conflicting actions'))
        self.assertEqual((conflict.state, conflict.owner), ('hold', 'executor'))
        old, new = dict(row, since='2026-01-01T00:00:00+00:00'), dict(row, since='2026-01-02T00:00:00+00:00')
        self.assertEqual(self.selected({'merge': old, 'grade': new}).action, 'grade')
        self.assertTrue(self.selected({'merge': new, 'grade': old}).reason
                        .startswith('undetermined: conflicting actions'))
        for name in ('wave-held', 'dependency-held'):
            held = self.selected({name: dict(row, blocker='L-spec-9002'), 'idle': row})
            self.assertEqual((held.state, held.owner), ('hold', 'tick'))
            self.assertIn('L-spec-9002', held.clears)
        self.assertEqual(self.selected({'idle': row}).owner, 'thinker')
        held = self.selected({'build': dict(row, reason='undetermined: missing timestamp')})
        self.assertEqual((held.state, held.owner), ('hold', 'executor'))
        # Table order, rather than predicate registry order, wins ties.
        reversed_rules = dict(reversed(list(self.rules.items())))
        self.assertEqual(self.selected({'build': row, 'rebuild': row}, reversed_rules).action, 'rebuild')

    def queued_clean(self, project='do-it-v2'):
        return NoSilentUndeterminedTests().built() + [
            event('verdict', -70, confirmed=True),
            event('review', -60, n_blocking=0),
            event('merge-gate-clean', -50, branch_sha='built', main_sha='old-main'),
            event('queue-add', -40, project=project, branch='l-spec-9001')]

    def queued_action(self, events, action, **kwargs):
        steps = [s for s in self.steps(events, **kwargs) if s.item == SID]
        self.assertEqual(len(steps), 1, steps)
        step = steps[0]
        self.assertEqual((step.state, step.action), ('action', action), step)
        self.assertFalse(step.reason.startswith('undetermined'), step)
        AuditStepTests().signed('L-spec-2116/' + self._testMethodName, asdict(step))
        return step

    def test_queued_no_conflict_reclean_do_it_v2(self):
        events = self.queued_clean()
        baseline = self.queued_action(events, 'merge-direct')
        reclean = event('merge-gate-clean', -30, branch_sha='built', main_sha='new-main')
        self.assertEqual(self.queued_action(events + [reclean], 'merge-direct'), baseline)

    def test_queued_no_conflict_reclean_window_gate(self):
        events = self.queued_clean(ns.merge_queue.window_gate.PROJECT)
        baseline = self.queued_action(events, 'merge')
        reclean = event('merge-gate-clean', -30, branch_sha='built', main_sha='new-main')
        self.assertEqual(self.queued_action(events + [reclean], 'merge'), baseline)

    def test_queued_no_conflict_rebuild(self):
        events = AttemptsRebuild1855Tests().fixture('rebuild')
        events.append(event('queue-add', -190, project='do-it-v2', branch='l-spec-9001'))
        self.queued_action(events, 'rebuild')

    def test_queued_no_conflict_close(self):
        events = AttemptsRebuild1855Tests().fixture('grade')
        events += [event('queue-add', -170, project='do-it-v2', branch='l-spec-9001'),
                   event('decision', -20, close='superseded')]
        self.queued_action(events, 'close')

    def test_queued_no_conflict_regrade(self):
        events = AttemptsRebuild1855Tests().fixture('regrade')
        events.append(event('queue-add', -190, project='do-it-v2', branch='l-spec-9001'))
        self.queued_action(events, 'regrade')

    def test_queued_no_conflict_rebuild_retry_due(self):
        fixture = VanishedRetryTests()
        events, role, _ = fixture.fixture('rebuild')
        fixture.failure(events, role, 1, -120)
        now = NOW - timedelta(minutes=115)
        baseline = self.queued_action(events, 'rebuild', now=now)
        self.assertTrue(baseline.reason.startswith('retry due'), baseline)
        events.append(event('queue-add', -225, project='do-it-v2', branch='l-spec-9001'))
        self.assertEqual(self.queued_action(events, 'rebuild', now=now), baseline)

    def queued_rebuilt(self):
        return self.queued_clean() + [
            event('build-started', -30, actor='builder'),
            event('build-done', -25, actor='builder', status='DONE')]

    def test_queued_stale_row_after_rebuild_awaiting_grade(self):
        events = self.queued_rebuilt()
        self.queued_action(events, 'grade')
        self.assertIsNone(ns._merge(AttemptsRebuild1855Tests().context(events)))

    def test_queued_stale_row_after_rebuild_verdict(self):
        self.queued_action(self.queued_rebuilt() + [
            event('verdict', -20, confirmed=True)], 'review')

    def test_queued_stale_row_after_rebuild_clean_review(self):
        self.queued_action(self.queued_rebuilt() + [
            event('verdict', -20, confirmed=True),
            event('review', -10, n_blocking=0)], 'gate')

    def test_queued_stale_row_regate_after_refresh(self):
        self.queued_action(self.queued_clean() + [
            event('merge-gate-clean', -30, branch_sha='refreshed', main_sha='new-main'),
            event('verdict', -20, confirmed=True)], 'review')

    def test_queued_no_conflict_precedence(self):
        events = AttemptsRebuild1855Tests().fixture('rebuild') + [
            event('decision', -20, close='superseded'),
            event('queue-add', -190, project='do-it-v2', branch='l-spec-9001')]
        step = ns.step_for(events, {SID: dict(state='building')}, SID, NOW, lr.load())
        self.assertEqual((step.state, step.action), ('action', 'close'), step)
        self.assertFalse(step.reason.startswith('undetermined'), step)
        self.assertEqual(self.queued_action(events, 'close',
                                          specs={SID: dict(state='building')}), step)

    def test_queued_no_conflict_genuine_hold_kept(self):
        row = dict(since=event('x')['ts'], reason='ready')
        conflict = self.selected({'build': row, 'grade': row})
        self.assertTrue(conflict.reason.startswith('undetermined: conflicting actions'))
        self.assertEqual((conflict.state, conflict.owner, conflict.clears),
                         ('hold', 'executor', 'one lane action'))
        AuditStepTests().signed('L-spec-2116/genuine-conflict', asdict(conflict))
        self.queued_action(self.queued_clean()[:-1], 'enqueue')

    def test_queued_no_conflict_not_live_still_enqueues(self):
        for kind in ('queue-remove', 'window-dropped', 'branch-less'):
            with self.subTest(kind=kind):
                events = self.queued_clean()
                if kind == 'branch-less':
                    del events[-1]['branch']
                else:
                    events.append(event(kind, -35, reason='conflict'))
                events.append(event('merge-gate-clean', -30))
                baseline = [e for e in events if e['type'] not in
                            {'queue-add', 'queue-remove', 'window-dropped'}]
                self.assertEqual(self.queued_action(events, 'enqueue'), self.one(baseline))
        events = self.queued_clean() + [
            event('precheck-red', -35, tests=['fixture'], master='main', branch_sha='built'),
            event('build-started', -30, actor='builder'),
            event('build-done', -25, actor='builder', status='DONE'),
            event('verdict', -20, confirmed=True),
            event('review', -15, n_blocking=0),
            event('merge-gate-clean', -10)]
        self.assertTrue(any(r['spec'] == SID for r in ns.merge_queue.queue(events)))
        self.queued_action(events, 'enqueue')

    def test_ac4_table_owners_and_judgment(self):
        row = dict(since=event('x')['ts'], reason='ready')
        for action in ns.ACTION_STAGES:
            step = self.selected({action: row})
            self.assertEqual(step.owner, self.rules[action]['actor'])
            self.assertEqual(step.owner, ns.ACTIONS[step.action])
        self.assertEqual(ns.ACTIONS, {**{n: self.rules[n]['actor'] for n in ns.ACTION_STAGES},
                                     'sweep-check': 'sweeper'})
        changed = deepcopy(self.rules)
        changed['build']['actor'] = 'executor'
        self.assertEqual(self.selected({'build': row}, changed).owner, 'executor')
        events = [event('spec-written'), event('question', -20)]
        self.assertEqual(self.one(events).owner, 'executor')
        events[-1] = event('escalation-blocking', -20)
        self.assertEqual(self.one(events).owner, 'executor')
        events = [event('spec-written'), event('owed-ac', -25, criterion='AC1', wake_at=event('x', -24)['ts']),
                  event('shipped', -20), event('owed-unobservable', -10, criterion='AC1', capability='zzz')]
        check = self.steps(events, {SID: dict(state='shipped-owed-expired')})[0]
        self.assertEqual((check.state, check.owner, check.action), ('hold', 'thinker', None))
        self.assertTrue(check.reason.startswith('undetermined:'))
        self.assertIn('observation tool, access and owner', check.clears)
        events[-1] = event('owed-unobservable', -10, criterion='AC1', capability='browser', why='No browser available')
        events += [event('owed-routed', -9, criterion='AC1', route='browser', epoch='one'),
                   event('owed-routed', -8, criterion='AC1', route='browser', epoch='two')]
        check = self.steps(events, {SID: dict(state='shipped-owed-due')})[0]
        self.assertEqual((check.state, check.owner, check.action), ('hold', 'thinker', None))
        self.assertIn('exhausted',check.reason)

    def test_ac5_pure_inherited_predicates_and_clock(self):
        events, specs, now, waves = replay('w42')
        events += [event('window-opened', subject='win-fixture')]
        # Carry a footprint to catch accidental reuse of the file-reading wave helper.
        events[0]['footprint'] = ['fixture.py']
        originals = deepcopy((events, specs, self.rules, waves))
        def calls():
            return (ns.next_steps(events, specs, now, self.rules, waves),
                    ns.step_for(events, specs, 'L-spec-1459', now, self.rules, waves),
                    ns.classify_failure('ENOSPC', 1, self.rules),
                    ns.flow_counts(events, specs, now, self.rules, waves))
        expected = calls()
        import health_alarms, start_check  # lazy import of the lock reader reads the environment
        class NoEnvironment:
            def fail(self, *args, **kwargs):
                raise AssertionError('environment read')
            get = __getitem__ = __iter__ = __contains__ = fail
        with ExitStack() as stack:
            for name in ('io.open', 'builtins.open', 'pathlib.Path.open', 'subprocess.run',
                         'time.time', 'lane_rules.load', 'lane_rules.due',
                         'fold.spec_state', 'fold.owed_due'):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            # The shared start refusal calls spec_wave; it stays call-through, and the
            # file-open hooks above still fail any file read it would make.
            stack.enter_context(patch('dispatch.spec_wave', wraps=dispatch.spec_wave))
            allow_dependency_reads(stack)
            stack.enter_context(patch.object(os, 'environ', NoEnvironment()))
            stack.enter_context(patch.object(fold, 'NOW', NOW.replace(year=2040)))
            self.assertEqual(calls(), expected)
        self.assertEqual((events, specs, self.rules, waves), originals)
        later = ns.next_steps(events, specs, now + timedelta(days=1), self.rules, waves)
        self.assertNotEqual(later, expected[0])
        a, b = 'L-spec-9002', SID
        wave_events = [event('spec-written', subject=s, charter='L-charter-9001', footprint=['file.py'])
                       for s in (a, b)]
        step = self.one(wave_events, waves={a: 1, b: 2})
        self.assertEqual((step.state, step.owner), ('hold', 'tick'))
        self.assertIn(a, step.clears)

    def test_ac6_undetermined_is_scoped_and_never_action(self):
        events = [event('spec-written'), event('spec-written', subject='L-spec-9002')]
        baseline = self.steps(events)
        for bad in ({'type': 'decision', 'subject': SID},
                    event('decision', ts_override=True)):
            if 'ts_override' in bad:
                bad.pop('ts_override')
                bad['ts'] = '2026-10-23T11:00:00'
            result = self.steps(events + [bad])
            self.assertEqual([s for s in result if s.item != SID], [s for s in baseline if s.item != SID])
            step = next(s for s in result if s.item == SID)
            self.assertEqual((step.state, step.owner), ('hold', 'executor'))
            self.assertTrue(step.reason.startswith('undetermined:'))
        for state in ('unknown', 'invented'):
            self.assertTrue(self.one(events[:1], {SID: dict(state=state)}).reason.startswith('undetermined:'))
        terminals = events[:1] + [event('shipped', -10), event('spec-killed', -5)]
        self.assertTrue(self.one(terminals, {SID: dict(state='killed')}).reason.startswith('undetermined:'))
        for rules in (None, {}, [], 1, {'build': {}}, dict(self.rules, build={'name': 'build'})):
            steps = ns.next_steps(events, specs_for(events), NOW, rules)
            self.assertEqual(len(steps), 2)
            self.assertTrue(all(s.state == 'hold' and s.owner == 'executor'
                                and s.reason.startswith('undetermined:') for s in steps))
        missing = ns.next_steps([], {SID: dict(state='written')}, NOW, self.rules)[0]
        self.assertTrue(missing.reason.startswith('undetermined:'))
        missing = self.selected({'build': dict(since='not-a-time', reason='ready')})
        self.assertTrue(missing.reason.startswith('undetermined:'))
        malformed = [{'type': 'build-started', 'subject': SID, 'spawn': 'L-builder-9001'},
                     {'type': 'charter-filed', 'subject': 'L-charter-9001'},
                     {'type': 'window-opened', 'subject': 'win-bad'}]
        steps = self.steps(malformed)
        self.assertEqual(len(steps), 4)
        self.assertTrue(all(s.state == 'hold' and s.owner == 'executor'
                            and s.reason.startswith('undetermined:') for s in steps))

    def test_ac7_failure_classifier(self):
        infra = ['No space left on device', '[Errno 28]', 'Out of diskspace', 'ENOSPC',
                 'fatal: could not create work tree dir', 'git worktree add failed',
                 'failed git worktree add', 'Connection reset by peer', 'Connection refused',
                 'at capacity', 'overloaded', 'HTTP 429',
                 'ledger cache: lock not acquired within 120s (/home/x/.do-it/cache/ledger-abc.lock)']
        for text in infra:
            with self.subTest(text=text):
                self.assertEqual(ns.classify_failure(text, 1), 'infra')
                self.assertEqual(ns.classify_failure(text + '\nFAILED', 1), 'unknown')
                self.assertEqual(ns.classify_failure('AssertionError\n' + text, 1), 'unknown')
        for text in ['FAILED', 'AssertionError', 'Ran 12 tests\nFAILED']:
            self.assertEqual(ns.classify_failure(text, 1), 'work')
        for text in ['', None]:
            self.assertEqual(ns.classify_failure(text, 124), 'infra')
            for code in (None, 0, 1, 127):
                self.assertEqual(ns.classify_failure(text, code), 'unknown')
        for text in ['14290', 'all green', 'HTTP 14290', 'ledger cache: lock acquired']:
            self.assertEqual(ns.classify_failure(text, 1), 'unknown')
        self.assertEqual(ns.classify_failure('ENOSPC', None), 'unknown')

    def test_ac8_stage_counts_and_seven_day_boundary(self):
        events = [event('spec-written'), event('build-started', -20, spawn='L-builder-9001'),
                  event('window-opened', -10, subject='win-fixture'),
                  event('charter-filed', subject='L-charter-9001')]
        specs = {SID: dict(state='building')}
        for i, age in enumerate((timedelta(days=6, hours=23), timedelta(days=7, hours=1))):
            sid = f'L-spec-{9100+i}'
            events.append(dict(event('shipped', subject=sid), ts=(NOW-age).isoformat()))
            specs[sid] = dict(state='shipped')
        sid = 'L-spec-9100'
        events.append(event('owed-ac', -10, subject=sid, criterion='AC1', wake_at=event('x', -9)['ts']))
        specs[sid]['state'] = 'shipped-owed-due'
        steps = self.steps(events, specs)
        counts = ns.flow_counts(events, specs, NOW, self.rules)
        self.assertEqual(set(counts), set(ns.STAGES))
        self.assertTrue(all(type(v) is int for v in counts.values()))
        self.assertEqual(counts['live'], 1)
        for stage in ns.STAGES[:-1]:
            self.assertEqual(counts[stage], sum(s.stage == stage for s in steps))
        self.assertTrue(all(s.stage == 'live' for s in steps if s.item_kind == 'check'))
        row = dict(since=event('x')['ts'], reason='ready')
        for action, stage in ns.ACTION_STAGES.items():
            self.assertEqual(self.selected({action: row}).stage, stage)
        for kind, stage in [('spec-written', 'planned'), ('build-started', 'building'),
                            ('build-done', 'checking'), ('verdict', 'checking'), ('queue-add', 'merging')]:
            with patch.dict(ns.PREDICATES, {n: lambda c: None for n in ns.PREDICATES}):
                self.assertEqual(self.one([event('spec-written', -60), event(kind)]).stage, stage)

    def test_ac9_real_replay_shapes(self):
        actions = {'w41': ('L-spec-1451', 'L-thinker-0004.jsonl:6272', '2026-10-22T13:52:25+00:00'),
                   'w50': ('L-spec-1418', 'L-thinker-0004.jsonl:6139', '2026-10-22T07:55:19+00:00'),
                   'w51': ('L-spec-1417', 'L-thinker-0004.jsonl:6210', '2026-10-22T11:21:20+00:00')}
        for name, (sid, trigger, since) in actions.items():
            events, specs, now, waves = replay(name)
            steps = ns.next_steps(events, specs, now, self.rules, waves)
            matching = [s for s in steps if s.item == sid]
            self.assertEqual(len(matching), 1)
            expected = ns.Step(sid, 'spec', 'building', 'action', 'rebuild', 'tick', trigger,
                               'condition met', None, since, attempt=1)
            for key in KEYS:
                self.assertEqual(asdict(matching[0])[key], asdict(expected)[key], (name, key))
            if name == 'w41':
                blocked = next(e for e in events if e['type'] == 'build-blocked')
                decision = next(e for e in events if e['_src'] == trigger)
                self.assertEqual(decision['ref'], blocked['_src'])
                ship = json.loads((FIXTURES/name/'shipped.json').read_text())
                specs[sid]['state'] = 'shipped'
                self.assertFalse(any(s.item == sid for s in ns.next_steps(events+[ship], specs, now, self.rules, waves)))
            print(name, json.dumps(asdict(expected), sort_keys=True))
        events, specs, now, waves = replay('w42')
        step = ns.step_for(events, specs, 'L-spec-1459', now, self.rules, waves)
        expected = ns.Step('L-spec-1459', 'spec', 'checking', 'hold', None, 'tick',
                           'L-thinker-0004.jsonl:6353', 'dependency: blocker-open: build',
                           'L-spec-1457 shipped', '2026-10-22T16:42:29+00:00')
        self.assertEqual(asdict(step), asdict(expected))
        self.assertFalse(any(e['type'] == 'shipped' and e['subject'] == 'L-spec-1457' for e in events))
        print('w42', json.dumps(asdict(step), sort_keys=True))
        events, specs, now, waves = replay('w56')
        for item, kind, reason in [('L-builder-3018', 'attempt', 'in flight'),
                                   ('L-spec-1417', 'spec', 'in flight: L-builder-3018')]:
            expected = ns.Step(item, kind, 'building', 'hold', None, 'tick', 'L-builder-3018.jsonl:1',
                               reason, 'L-builder-3018 spawn-done/spawn-failed', '2026-10-22T10:14:59+00:00')
            self.assertEqual(ns.step_for(events, specs, item, now, self.rules, waves), expected)
        self.assertEqual(next(e for e in events if e['_src'] == 'L-builder-3018.jsonl:1')['issued_by'], 'executor')
        print('w56', json.dumps(asdict(expected), sort_keys=True))
        events, specs, now, waves = replay('disk')
        expected = ns.Step('L-spec-1186', 'spec', 'checking', 'hold', None, 'executor',
                           'L-executor-0001.jsonl:21570', 'infra: retry required',
                           'infra-retry backoff or freed disk', '2026-10-23T08:11:56+00:00')
        self.assertEqual(ns.step_for(events, specs, expected.item, now, self.rules, waves), expected)
        window = ns.step_for(events, specs, 'win-proj-a', now, self.rules, waves)
        self.assertEqual(window, ns.Step('win-proj-a', 'window', 'merging', 'hold', None, 'executor',
                                        'L-executor-0001.jsonl:21568', 'window waiting; elapsed_min=0',
                                        'window resolution', '2026-10-23T08:11:22+00:00'))
        print('disk', json.dumps(asdict(expected), sort_keys=True))

    def test_ac10_fixture_redaction_and_field_minimum(self):
        forbidden = ['albert-scott', 'fredhead88', 'qurlife', 'amazon', '/opt/', '/home/',
                     'docs/sessions', 'tests/pipelines', 'dashboard/', '.env']
        allowed = set('v ts type subject spawn role issued_by backend branch reason findings why error ref '
                      'rework regrade close status blocker charter src _src waits_for'.split())
        for path in FIXTURES.rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text()
            for fragment in forbidden:
                self.assertNotIn(fragment, text, str(path))
            if path.name in {'events.jsonl', 'shipped.json'}:
                for line in text.splitlines():
                    self.assertLessEqual(set(json.loads(line)), allowed)

    def test_ac9_failure_first_and_newer_retry(self):
        events = [event('spec-written', -60), event('build-blocked', -50),
                  event('decision', -40, actor='thinker', rework='yes')]
        for kind in ('merge-gate-rework', 'spawn-failed', 'autodispatch-failed'):
            for text, prefix in [('ENOSPC', 'infra:'), ('unmatched', 'undetermined:')]:
                step = self.one(events + [event(kind, -30, findings=text)])
                self.assertEqual((step.state, step.owner), ('hold', 'executor'))
                self.assertTrue(step.reason.startswith(prefix))
            failure = event(kind, -30, findings='FAILED')
            with patch.dict(ns.PREDICATES, {'rebuild': lambda c: dict(
                    since=events[-1]['ts'], reason='table evaluated')}):
                self.assertEqual(self.one(events + [failure]).action, 'rebuild')
            retry = event('decision', -20, actor='thinker', rework='yes')
            self.assertEqual(self.one(events + [event(kind, -30, findings='ENOSPC'), retry]).action, 'rebuild')

    def test_ac11_cli_read_only_json_text_and_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'events').mkdir()
            events = [event('spec-written'), event('question', -5, reason='line\nbreak\x1b')]
            (root/'events/L-operator-fixture.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            for key in ('DOIT_LEDGER_FILE', 'DOIT_LANE_RULES', 'DOIT_PROJECT'):
                env.pop(key, None)
            def tree():
                return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.rglob('*') if p.is_file()}
            def run(*args, environment=None):
                return subprocess.run([str(ROOT/'doit'), 'next', *args], env=environment or env,
                                      text=True, capture_output=True)
            before = tree()
            result = run('--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            obj = json.loads(result.stdout)
            self.assertEqual(set(obj), {'counts', 'steps'})
            self.assertTrue(obj['steps'])
            self.assertTrue(all(set(s) == KEYS for s in obj['steps']))
            for args in [(), (SID,)]:
                result = run(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn(SID+' | hold | executor | ', result.stdout)
            self.assertEqual(run('absent').returncode, 1)
            for environment in [dict(env, DOIT_LANE_RULES=str(root/'absent.toml')),
                                dict(env, DOIT_ROOT=str(root/'absent'))]:
                result = run(environment=environment)
                self.assertEqual((result.returncode, result.stdout), (2, ''))
                self.assertTrue(result.stderr.strip())
            (root/'bad.toml').write_text('invalid = [')
            result = run(environment=dict(env, DOIT_LANE_RULES=str(root/'bad.toml')))
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            (root/'bad.toml').unlink()
            after = tree()
            self.assertEqual(before, {name: value for name, value in after.items() if name in before})
            self.assertTrue(set(after) - set(before))
            self.assertTrue(all(name.startswith('cache/ledger-') for name in set(after) - set(before)))
            self.assertEqual(run('--json').returncode, 0)
            self.assertEqual(after, tree())  # unchanged cache is reused without writes
            # Default invocation still renders the existing board entry point.
            result = subprocess.run([str(ROOT/'doit')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ns._clean('a\nb\rc\x1b\u0085d'), 'a b c  d')
        with patch.object(fold, 'read_events', return_value=[]), \
             patch.object(fold, 'fold', return_value=({}, {})), \
             patch.object(ns, 'datetime') as clock, \
             tempfile.TemporaryDirectory() as temporary, \
             patch.object(fold, 'EVENTS', Path(temporary)), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            clock.now.side_effect = ValueError('clock unavailable')
            self.assertEqual(ns.main([]), 2)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('clock unavailable', err.getvalue())


class MemberVerdictCliTests(unittest.TestCase):
    """L-spec-2012 AC6: `doit next <spec>` shows the latest member-verdict, and only then."""

    def run_cli(self, rows, *args):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'events').mkdir()
            (root/'events/L-executor-0001.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            for key in ('DOIT_LEDGER_FILE', 'DOIT_LANE_RULES', 'DOIT_PROJECT'):
                env.pop(key, None)
            result = subprocess.run([str(ROOT/'doit'), 'next', *args], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rows(self, *verdicts):
        now = datetime.now(timezone.utc)
        out = [dict(v=1, type='spec-written', subject=SID, ts=(now - timedelta(hours=9)).isoformat(timespec='seconds'))]
        for hours, tree, state in verdicts:
            out.append(dict(v=1, type='member-verdict', subject=SID, spec=SID, window='w', tree=tree*40, state=state,
                            url='', nodes=[], ts=(now - timedelta(hours=hours)).isoformat(timespec='seconds')))
        return out

    def test_latest_green_undetermined_and_absent(self):
        absent = self.run_cli(self.rows(), SID)
        self.assertNotIn('member-verdict', absent)
        green = self.run_cli(self.rows((5, 'a', 'green')), SID)
        self.assertEqual(len(green.splitlines()), 1)
        self.assertIn('[member-verdict aaaaaaaa green (verified alone, waiting for a window), 5h ago]', green)
        # The latest event wins; an older green is not shown once undetermined arrived.
        latest = self.run_cli(self.rows((5, 'a', 'green'), (2, 'b', 'undetermined')), SID)
        self.assertIn('member-verdict bbbbbbbb undetermined (UNDETERMINED, not verified', latest)
        self.assertIn('2h ago', latest)
        self.assertNotIn('aaaaaaaa', latest)
        self.assertNotIn('waiting for a window', latest)
        # Output for a spec without member evidence is exactly what it was.
        self.assertEqual(absent.split(' | ')[:3], green.split(' | ')[:3])
        self.assertTrue(absent.rstrip('\n').endswith(absent.split(' | ', 3)[3].rstrip('\n')))
        obj = json.loads(self.run_cli(self.rows((2, 'c', 'green')), '--json')) 
        self.assertIn('cccccccc green', obj['steps'][0]['member_verdict'])
        plain = json.loads(self.run_cli(self.rows(), '--json'))
        self.assertTrue(all(set(s) == KEYS for s in plain['steps']))


class AuditStepTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one
    # Use the inherited snapshot helpers without repeating the historical cases.
    def planner(self, minutes=-31, **kw):
        return event('spec-written', minutes, actor='spec-writer', **kw)

    def signed(self, ac, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://audit-nextstep/' + ac, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))

    def test_ac2_audit_grace_and_precedence(self):
        written = self.planner(-30)
        evs = [written]
        for offset, expected in ((-1, None), (0, 'audit')):
            now = NOW + timedelta(seconds=offset)
            step = self.one(evs, now=now)
            self.assertEqual(step.action, expected)
            if expected:
                self.assertEqual((step.owner, step.stage), ('tick', 'planned'))
                self.assertEqual(ns.audit_due(evs, now)['since'], written['ts'])
            else:
                self.assertTrue(step.reason.startswith('audit-wait:'))
            self.signed('AC2-boundary', asdict(step))
        self.assertTrue(self.one(evs + [self.planner(-10)]).reason.startswith('audit-wait:'))
        clean = event('spawn-done', -1, actor='spec-auditor', bad_cut=False)
        self.assertEqual(self.one(evs + [clean]).action, 'build')
        for exempt in ([event('spec-written')], evs + [event('spec-carried')]):
            self.assertEqual(self.one(exempt).action, 'build')
        for blocked in (event('question'), event('spawn-started', -1, spawn='L-spec-auditor-9001', role='spec-auditor')):
            self.assertIsNone(self.one(evs + [blocked]).action)
        dependency = event('spec-written', -40, subject='L-spec-9002')
        with patch.object(ns.autodispatch, 'held_by_dependency', return_value='L-spec-9002'):
            self.assertEqual(self.one(evs, specs={SID: dict(state='written')}).action, 'audit')
        bad = event('spawn-done', -2, actor='spec-auditor', bad_cut=True)
        step = self.one(evs + [bad])
        self.assertEqual(step.owner, 'executor')
        self.assertTrue(step.reason.startswith('audit-bad-cut:'))
        self.assertEqual(self.one(evs + [bad, self.planner(-1)]).action, 'build')
        self.assertIsNone(ns._build(dict(evs=evs)))
        self.signed('AC2-bad-cut', asdict(step))

    def test_ac3_real_attempts_and_failure_hold_order(self):
        import attempts
        evs = [self.planner(-90)]
        start = event('spawn-started', -10, spawn='L-spec-auditor-9001', role='spec-auditor')
        self.assertEqual(ns.audit_state(evs + [start], NOW)['phase'], 'live')
        self.assertIsNone(ns.audit_due(evs + [start], NOW))
        refusal = event('spawn-failed', -20, role='builder', spawn='L-builder-9001', reason='unaudited')
        self.assertEqual(ns.classify_failure('unaudited', 1), 'unknown')
        self.assertEqual(self.one(evs + [refusal]).action, 'audit')
        fail = event('spawn-failed', -5, role='spec-auditor', spawn='L-spec-auditor-9001', reason='audit fixture failed')
        retry = evs + [refusal, start, fail]
        for seconds, phase in ((-1, 'retry-wait'), (0, 'due'), (1, 'due')):
            now = NOW + timedelta(seconds=seconds)
            state = ns.audit_state(retry, now)
            self.assertEqual(state['phase'], phase)
            step = self.one(retry, now=now)
            if phase == 'due':
                self.assertEqual(ns.audit_due(retry, now)['attempt'], 2)
                self.assertEqual(step.action, 'audit')
            else:
                self.assertIsNone(ns.audit_due(retry, now))
                self.assertTrue(step.reason.startswith('audit-wait:'))
            self.signed('AC3-backoff', dict(state=state, step=asdict(step)))
        stale = event('spawn-stale', -5, role='spec-auditor', spawn='L-spec-auditor-9001')
        self.assertEqual(ns.audit_due(evs + [start, stale], NOW)['attempt'], 2)
        failures = [event('spawn-failed', -30 + n, role='spec-auditor',
                         spawn=f'L-spec-auditor-{9100+n}', reason=f'audit failure {n}')
                    for n in range(attempts.MAX_ATTEMPTS)]
        exhausted = evs + failures
        self.assertEqual(ns.audit_state(exhausted, NOW)['phase'], 'exhausted')
        self.assertIsNone(ns.audit_due(exhausted, NOW))
        step = self.one(exhausted)
        self.assertTrue(step.reason.startswith('audit-exhausted:'))
        for n in range(attempts.MAX_ATTEMPTS):
            self.assertIn(f'audit failure {n}', step.reason)
        self.assertEqual(ns.audit_state(exhausted + [self.planner(-10)], NOW + timedelta(minutes=20))['phase'], 'due')
        with patch.object(attempts, 'history', side_effect=OSError('ledger unavailable')):
            self.assertEqual(ns.audit_state(evs, NOW)['phase'], 'undetermined')
            self.assertIsNone(ns.audit_due(evs, NOW))
        def unreadable():
            raise OSError('slice unavailable')
            yield
        self.assertEqual(ns.audit_state(unreadable(), NOW)['phase'], 'undetermined')
        self.signed('AC3-exhausted', asdict(step))


class VanishedRetryTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def fixture(self, action):
        evs = [event('spec-written', -240, actor='spec-writer' if action == 'audit' else 'operator')]
        if action == 'rebuild':
            evs += [event('build-blocked', -230), event('decision', -220, rework='yes')]
        elif action in ('grade', 'regrade', 'review'):
            evs += [event('build-done', -230, actor='builder', status='DONE')]
            if action == 'regrade':
                evs += [event('decision', -220, regrade='yes')]
            elif action == 'review':
                evs += [event('verdict', -220, actor='grader', confirmed=True)]
        role = dict(build='builder', rebuild='builder', grade='grader',
                    regrade='grader', review='reviewer', audit='spec-auditor')[action]
        return evs, role, evs[-1]

    def failure(self, evs, role, number, minute):
        spawn = f'L-{role}-{9860+number}'
        start = event('spawn-started', minute-1, spawn=spawn, role=role)
        evs.append(start)
        if role == 'builder':
            evs.append(dict(start, type='build-started'))
        evs += [event('spawn-stale', minute-.5, spawn=spawn, role=role),
                event('spawn-failed', minute, spawn=spawn, role=role, reason='vanished', actor='tick')
                | {'_src': f'L-tick-local.jsonl:{number}'}]

    def report(self, label, step, evs, trigger, role, now):
        import attempts
        h = asdict(attempts.history(ns._history_events(evs), trigger, role, now=now))
        h['next_retry_at'] = str(h['next_retry_at'])
        body = json.dumps(dict(case=label, step=asdict(step), history=h), sort_keys=True)
        print(json.dumps(dict(url='cli://vanished-nextstep/' + label, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_six_actions_all_retry_boundaries_and_new_trigger(self):
        import attempts
        self.assertEqual((attempts.MAX_ATTEMPTS, attempts.BACKOFF_MIN), (3, (5, 15, 45)))
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            with self.subTest(action=action):
                evs, role, trigger = self.fixture(action)
                zero = self.one(evs)
                self.assertEqual(zero.action, action)
                self.assertEqual(zero.owner, 'tick')
                self.report(action+'-zero', zero, evs, trigger, role, NOW)
                for n, minute in ((1, -120), (2, -60), (3, -10)):
                    self.failure(evs, role, n, minute)
                    deadline = NOW + timedelta(minutes=minute + (5, 15, 45)[n-1])
                    for offset in (-1, 0):
                        now = deadline + timedelta(seconds=offset)
                        step = self.one(evs, now=now)
                        if n == 3:
                            self.assertIsNone(step.action)
                            self.assertEqual(step.owner, 'executor')
                            self.assertIn('exhaust', step.reason)
                            self.assertIn('operator decision', step.clears)
                        elif offset == -1:
                            self.assertIsNone(step.action)
                            if role == 'grader':
                                self.assertTrue(step.reason.startswith('retry-wait:')); self.assertEqual(step.retry_at, deadline.isoformat())
                            else:
                                self.assertIn('next_retry_at', step.reason)
                        else:
                            self.assertEqual((step.action, step.owner, step.since), (action, 'tick', trigger['ts']))
                            self.assertEqual(step.trigger, trigger['_src'])
                        self.report(action+f'-{n}-{offset}', step, evs, trigger, role, now)
                new = dict(trigger, ts=event('x', 1)['ts'], _src='L-operator-new.jsonl:1')
                renewed = self.one(evs+[new], now=NOW+timedelta(minutes=32))
                self.assertEqual(renewed.action, action)
                self.assertEqual(renewed.since, new['ts'])
                # A late reconciliation belonging to an older attempt cannot charge the new trigger.
                late = dict(evs[-1], ts=event('x', 2)['ts'])
                renewed = self.one(evs+[new, late], now=NOW+timedelta(minutes=32))
                self.assertEqual(renewed.action, action)
                self.report(action+'-new-trigger-late-end', renewed, evs+[new, late], new, role, NOW+timedelta(minutes=32))

    def test_stronger_lifecycle_predicates_and_refusals(self):
        import attempts
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            with self.subTest(action=action):
                evs, role, trigger = self.fixture(action)
                self.failure(evs, role, 1, -20)
                live = event('spawn-started', -1, spawn=f'L-{role}-9899', role=role)
                self.assertIn('in flight', self.one(evs+[live]).reason)
                output = dict(builder='build-done', grader='verdict', reviewer='review',
                              **{'spec-auditor': 'spawn-done'})[role]
                done = event(output, -10, actor=role, spawn=evs[-1]['spawn'], role=role,
                             status='DONE', confirmed=True, n_blocking=0, bad_cut=False)
                self.assertNotEqual(self.one(evs+[done]).action, action)
                late = dict(evs[-1], ts=event('x', -1)['ts'])
                self.assertNotEqual(self.one(evs+[done, late]).action, action)
                for terminal in ('queue-add', 'shipped', 'spec-killed'):
                    result = self.steps(evs+[event(terminal, -1)])
                    self.assertFalse(any(s.item == SID and s.action == action for s in result))
                question = event('escalation-blocking', -1)
                self.assertIn('judgment', self.one(evs+[question]).reason)
                if action != 'audit':
                    hold = event('dependency-hold', -1, waits_for='L-spec-9898')
                    with patch.object(ns.autodispatch, 'held_by_dependency', return_value='L-spec-9898'):
                        self.assertIsNone(self.one(evs+[hold]).action)
                clean, _, _ = self.fixture(action)
                for reason in ('bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress'):
                    refusal = event('spawn-failed', -2, spawn=f'L-{role}-9898', role=role, reason=reason)
                    if role == 'grader' and reason == 'dispatch-in-progress':
                        refusal['why'] = 'another dispatch holds the subject lock'
                    history = attempts.history(ns._history_events(clean+[refusal]), trigger, role, now=NOW)
                    self.assertEqual((history.consumed, history.n_failed), (False, 0))
                    refused = self.one(clean+[refusal])
                    if reason == 'bundle-pending':
                        self.assertIsNone(refused.action)
                    else:
                        self.assertEqual(refused.action, action)
                    self.report(action+'-refusal-'+reason, refused, clean+[refusal], trigger, role, NOW)

    def test_structured_authority_only_and_text_classifier_unchanged(self):
        import grading_env
        base = event('spawn-failed', reason='vanished', actor='tick') | {'_src': 'L-tick-local.jsonl:1'}
        self.assertEqual(ns._failure([base], self.rules)[1], 'retry')
        for change in (dict(reason='vanished later'), dict(reason='prose vanished'),
                       dict(type='autodispatch-failed'), dict(_src='L-builder-9861.jsonl:1'),
                       dict(_src=None)):
            self.assertNotEqual(ns._failure([dict(base, **change)], self.rules)[1], 'retry')
        self.assertEqual(ns.classify_failure('vanished', 1), 'unknown')
        with patch.object(ns, 'classify_failure', return_value='retry'):
            self.assertEqual(grading_env.failure_kind('vanished', 1), 'unknown')
        self.assertEqual(ns._attempt_role(dict(spawn='L-spec-auditor-9861')), 'spec-auditor')
        self.assertEqual(ns._attempt_role(dict(spawn='L-spec-writer-9861')), 'spec-writer')

    def test_same_timestamp_start_and_reconciliation_has_no_live_hold(self):
        for action in ('build', 'rebuild', 'grade', 'regrade', 'review', 'audit'):
            evs, role, _ = self.fixture(action)
            self.failure(evs, role, 1, -5)
            # Second-resolution appends from different sources can sort an end
            # before a start. Terminal identity still wins over that ordering.
            for e in evs[-4:] if role == 'builder' else evs[-3:]:
                e['ts'] = event('x', -5)['ts']
            steps = self.steps(evs)
            self.assertFalse(any(s.item_kind == 'attempt' for s in steps))
            self.assertEqual(next(s for s in steps if s.item == SID).action, action)


class RulingMixin:
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def ruling(self, action='close', **kw):
        return event('decision', action=action, **kw)

    def due(self, events, **kw):
        return [s for s in self.steps(events, **kw)
                if s.item_kind == 'spec' and s.reason.startswith('ruling-unexecuted:')]

    def signed(self, ac, body):
        payload = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://ruling-nextstep/' + ac, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


class RulingTimingTests(RulingMixin, unittest.TestCase):
    def test_boundary_matrix(self):
        results = []
        for action in ('close', 'rework', 'hold', 'recut'):
            for legacy in ({}, {'rework': 'yes'}, {'ref': 'L-operator-fixture.jsonl:19950'}) if action == 'rework' else ({},):
                evs = [event('spec-written', -60), event('build-blocked', -50),
                       self.ruling(action, **legacy, **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))]
                ruling = evs[-1]
                for seconds in (-1, 0, 1):
                    step = self.one(evs, now=NOW + timedelta(seconds=seconds))
                    self.assertEqual(len([s for s in self.steps(evs) if s.item == SID]), 1)
                    if legacy:
                        self.assertEqual((step.action, step.owner, step.trigger), ('rebuild', 'tick', ruling['_src']))
                    elif seconds < 0:
                        self.assertFalse(step.reason.startswith('ruling-unexecuted:'))
                    else:
                        self.assertEqual((step.state, step.action, step.owner, step.trigger, step.since),
                                         ('hold', None, 'executor', ruling['_src'], ruling['ts']))
                        self.assertEqual(step.reason, f'ruling-unexecuted: {action} ({ruling["_src"]})')
                        self.assertEqual(step.clears, {'close': 'spec-killed', 'rework': 'rework dispatch',
                            'hold': 'dependency-hold waits_for=L-spec-1642',
                            'recut': 'recut resolution recorded as spec-killed'}[action])
                    results.append(dict(action=action, legacy=legacy, age_seconds=1800+seconds, step=asdict(step)))
        self.signed('AC1-timing', results)

    def test_precedence_and_scoped_errors(self):
        ruling = self.ruling()
        written = event('spec-written', -60)
        variants = [[], [event('build-started', -10, spawn='L-builder-9001')],
                    [event('merge-gate-rework', -10, findings='ENOSPC')],
                    [event('dependency-hold', -40, waits_for='L-spec-1642')],
                    [event('question', -10)], [event('verdict', -10, confirmed=True)],
                    [event('review', -10, n_blocking=0)]]
        for extra in variants:
            for actor in ('operator', 'spec-writer'):
                evs = [dict(written, actor=actor), ruling, *extra]
                self.assertEqual(len(self.due(evs)), 1)
        for changed in [dict(ruling, ts='invalid'), dict(ruling, action='hold')]:
            self.assertTrue(self.one([written, changed]).reason.startswith('undetermined:'))
        malformed = deepcopy(self.rules)
        malformed.pop('build')
        self.assertTrue(self.one([written, ruling], rules=malformed).reason.startswith('undetermined:'))
        conflict = [written, ruling, event('spec-killed', -20), event('shipped', -10)]
        self.assertTrue(self.one(conflict).reason.startswith('undetermined:'))
        self.signed('AC1-precedence', dict(variants=len(variants)*2, scoped_errors=4))


class RulingExecutionTests(RulingMixin, unittest.TestCase):
    def test_execution_matrix(self):
        results = []
        for action, kind in [('close', 'spec-killed'), ('recut', 'spec-killed'),
                             ('hold', 'dependency-hold'), ('rework', 'build-started'),
                             ('rework', 'spawn-started'), ('rework', 'autodispatched')]:
            ruling = self.ruling(action, **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))
            execution = event(kind, -20, spawn='L-builder-9001', role='builder', waits_for='L-spec-1642')
            if kind == 'autodispatched':
                execution.update(actor='tick', _src='L-tick-fixture.jsonl:1')
            evs = [event('spec-written', -60), ruling]
            for change, clears in [({}, True), ({'ref': ruling['_src']}, True),
                                   ({'ref': 'elsewhere:1'}, False),
                                   ({'subject': 'L-spec-9999'}, False),
                                   ({'ts': event('x', -40)['ts']}, False)]:
                # A terminal subject stays terminal even with an unrelated ref.
                if kind == 'spec-killed' and (change.get('ref') == 'elsewhere:1' or 'ts' in change):
                    continue
                sample = evs + [dict(execution, **change)]
                if action == 'hold' and kind == 'dependency-hold' and sample[-1]['subject'] == SID:
                    # Dependency explanation takes precedence even when this
                    # event does not execute the older ruling's own obligation.
                    step = self.one(sample)
                    self.assertEqual(step.state, 'hold')
                    self.assertIn('L-spec-1642', step.reason)
                    context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                                   spec=dict(state='written'), now=NOW)
                    self.assertEqual(ns._ruling_step(context, {}, {}) is not None, not clears)
                    continue
                self.assertEqual(bool(self.due(sample)), not clears, (action, change))
                results.append(dict(action=action, change=change, cleared=clears))
            if action == 'hold':
                sample = evs + [dict(execution, waits_for='L-spec-9003')]
                self.assertIn('waits for L-spec-9003', self.one(sample).reason)
                context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                               spec=dict(state='written'), now=NOW)
                self.assertIsNotNone(ns._ruling_step(context, {}, {}))
            if action == 'rework':
                for role in ('grader', 'reviewer'):
                    self.assertTrue(self.due(evs + [dict(execution, role=role)]))
                self.assertTrue(self.due(evs + [dict(execution, spawn=None)]))
        self.signed('AC2-execution', results)

    def test_acceptance_correlated_across_complete_snapshot(self):
        results = []
        ruling = self.ruling('rework')
        base = [event('spec-written', -60), ruling]
        start = event('build-started', -20, spawn='L-builder-9001', role='builder')
        failure = event('spawn-failed', -10, spawn='L-builder-9001')
        queued = event('seat-queued', -19, subject='L-builder-9001', spawn='L-builder-9001', actor='relay')
        claim = event('seat-claimed', -18, subject='L-builder-9001', spawn='L-builder-9001',
                      actor='relay', _src='L-relay-fixture.jsonl:1')
        done = event('spawn-done', -17, spawn='L-builder-9001', role='builder',
                     actor='builder', _src='L-builder-fixture.jsonl:1')
        for tail, cleared in [([], True), ([failure], False), ([queued, failure], False),
                              ([claim, failure], True), ([done, failure], True),
                              ([{k: v for k, v in claim.items() if k != 'spawn'}, failure], True),
                              ([dict(claim, spawn='L-builder-9999'), failure], False),
                              ([dict(done, spawn='L-builder-9999'), failure], False),
                              ([dict(claim, _src='L-builder-forged.jsonl:1'), failure], False)]:
            sample = base + [start] + tail
            self.assertEqual(bool(self.due(sample)), not cleared)
            shuffled = list(sample)
            random.Random(72).shuffle(shuffled)
            self.assertEqual(self.steps(sample), self.steps(shuffled))
            results.append(dict(tail=tail, cleared=cleared))
        for kind in ('spawn-failed', 'autodispatch-failed', 'decision'):
            self.assertTrue(self.due(base + [event(kind, -20)]))
        self.signed('AC2-acceptance', results)

    def test_same_act_rebuild_and_refused_launch_at_threshold(self):
        results = []
        for legacy in ({'rework': 'yes'}, {'ref': 'L-operator-fixture.jsonl:19950'}):
            base = [event('spec-written', -60), event('build-blocked', -50),
                    self.ruling('rework', **legacy)]
            refused = [event('build-started', -20, spawn='L-builder-9001'),
                       event('seat-queued', -19, spawn='L-builder-9001'),
                       event('spawn-failed', -18, spawn='L-builder-9001', reason='bundle-pending')]
            for seconds in (-1, 0, 1):
                now = NOW + timedelta(seconds=seconds)
                eligible = self.one(base, now=now)
                self.assertEqual((eligible.action, eligible.owner, eligible.trigger),
                                 ('rebuild', 'tick', base[-1]['_src']))
                rejected = self.one(base + refused, now=now)
                self.assertEqual(rejected.reason.startswith('ruling-unexecuted:'), seconds >= 0)
                results.append(dict(legacy=legacy, age_seconds=1800+seconds,
                                    eligible=asdict(eligible), refused=asdict(rejected)))
        self.signed('AC2-same-act-refusal', results)

    def test_newest_actor_legacy_terminal_and_order(self):
        base = [event('spec-written', -60), self.ruling()]
        fresh = self.ruling('recut', minutes=-5)
        self.assertFalse(self.due(base + [fresh]))
        self.assertIn('recut', self.due(base + [fresh], now=NOW + timedelta(minutes=25))[0].reason)
        self.assertTrue(self.due(base + [event('decision', -5, why='rework it')]))
        for state in dispatch.SETTLED_STATES:
            self.assertFalse(self.due(base, specs={SID: dict(state=state)}))
        self.assertFalse(self.due([base[0], event('spec-killed', -50), base[1]]))
        for changed in [dict(base[1], action='unknown'), dict(base[1], action=None),
                        dict(base[1], _src='L-builder-forged.jsonl:1', actor='thinker')]:
            self.assertFalse(self.due([base[0], changed]))
        # Same source append order decides equal timestamps, independent of input order.
        ruling = self.ruling('hold', waits_for='L-spec-1642', _src='L-operator-fixture.jsonl:2')
        for line, cleared in [(1, False), (3, True)]:
            done = event('dependency-hold', waits_for='L-spec-1642', _src=f'L-operator-fixture.jsonl:{line}')
            sample = [base[0], done, ruling]
            step = self.one(sample)
            self.assertEqual(step.state, 'hold')
            self.assertIn('L-spec-1642', step.reason)
            context = dict(sid=SID, evs=ns._snapshot(sample, {})[1][SID],
                           spec=dict(state='written'), now=NOW)
            self.assertEqual(ns._ruling_step(context, {}, {}) is not None, not cleared)
            self.assertEqual(self.steps(sample), self.steps(list(reversed(sample))))
        self.signed('AC2-order', dict(newest_clock=True, provenance=True, canonical_order=True))


class RulingReplayTests(RulingMixin, unittest.TestCase):
    def test_six_redacted_replays_and_adapters(self):
        import tick
        replayed = []
        for sid, line, action in [('1555', 9044, 'close'), ('1557', 9093, 'close'),
                                  ('1660', 9140, 'close'), ('1745', 9640, 'rework'),
                                  ('1743', 9684, 'rework'), ('1641', 9848, 'hold')]:
            subject = 'L-spec-' + sid
            ruling = self.ruling(action, subject=subject, actor='thinker',
                _src=f'L-thinker-0004.jsonl:{line}', **({'waits_for': 'L-spec-1642'} if action == 'hold' else {}))
            evs = [event('spec-written', -60, subject=subject), ruling]
            specs = specs_for(evs)
            original = deepcopy((evs, specs, self.rules))
            step = self.steps(evs)[0]
            self.assertEqual(step.owner, 'executor')
            self.assertEqual(ns.step_for(evs, specs, subject, NOW, self.rules), step)
            self.assertEqual(ns.flow_counts(evs, specs, NOW, self.rules)['planned'], 1)
            self.assertEqual(fold.step_row(step, self.rules, NOW)['spec'], subject)
            self.assertEqual(tick._steps(evs, specs, NOW), [step])
            self.assertFalse([s for s in tick._steps(evs, specs, NOW) if s.state == 'action' and s.owner == 'tick'])
            execution = event({'close': 'spec-killed', 'hold': 'dependency-hold', 'rework': 'build-started'}[action],
                              -20, subject=subject, waits_for='L-spec-1642', spawn='L-builder-9001', role='builder')
            after = self.steps(evs + [execution], specs={subject: dict(
                state='killed' if action == 'close' else 'written')})
            self.assertFalse(any(s.reason.startswith('ruling-unexecuted:') for s in after))
            if action == 'close':
                self.assertFalse(any(s.item == subject for s in after))
            self.assertEqual((evs, specs, self.rules), original)
            replayed.append(dict(subject=subject, original_ref=ruling['_src'], action=action,
                                 before=asdict(step), after=[asdict(s) for s in after]))
        self.signed('AC3-replays', replayed)

    def test_cli_readonly(self):
        with tempfile.TemporaryDirectory(prefix='ruling-cli-') as directory:
            root = Path(directory)
            (root/'events').mkdir()
            events = [event('spec-written', -60), self.ruling()]
            path = root/'events/L-operator-fixture.jsonl'
            path.write_text(''.join(json.dumps(e)+'\n' for e in events))
            before = {p.name: p.read_bytes() for p in (root/'events').iterdir()}
            env = dict(os.environ, DOIT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            for key in ('DOIT_LEDGER_FILE', 'DOIT_LANE_RULES', 'DOIT_PROJECT'):
                env.pop(key, None)
            outputs = []
            for args in [('--json',), (SID,), ()]:
                result = subprocess.run([str(ROOT/'doit'), 'next', *args], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if '--json' in args:
                    obj = json.loads(result.stdout)
                    self.assertEqual(set(obj), {'counts', 'steps'})
                    self.assertEqual(set(obj['steps'][0]), KEYS)
                    self.assertEqual(obj['steps'][0]['owner'], 'executor')
                self.assertIn('ruling-unexecuted: close', result.stdout)
                outputs.append(result.stdout)
            after = {p.name: p.read_bytes() for p in (root/'events').iterdir()}
            self.assertEqual(before, after)
            self.signed('AC3-cli', dict(outputs=outputs, unchanged_bytes=True,
                event_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))


class RulingConventionTests(RulingMixin, unittest.TestCase):
    def test_sd10_contract(self):
        text = (ROOT/'agents/thinker.md').read_text()
        base = (ROOT/'agents/relay.md').read_text()
        marker = '## Lessons'
        self.assertEqual(text[text.index(marker):], base[base.index(marker):])
        headings = [line for line in text.splitlines() if line.startswith('## ')]
        self.assertTrue(headings[-1].startswith(marker))
        convention = text[text.index('## SD10'):text.index(marker)]
        for phrase in ['action=close|rework|hold|recut', 'waits_for=', '30 minutes', 'executor',
                       'spec-killed', 'dependency-hold', 'rework dispatch', 'same-act',
                       'action-less', 'filename', 'original', 'still open', 'settled']:
            self.assertIn(phrase, convention)
        for number in ('1555', '1557', '1660', '1745', '1743', '1641', '1560', '1730', '1672', '1768'):
            self.assertIn('L-spec-' + number, convention)
        for phrase in ('doit alloc charter', 'doit think --land', 'doit think --discard', 'review_path',
                       'Covers:', 'You spawn nothing', 'NEVER deletes', 'blocked_me'):
            self.assertIn(phrase, text)
        self.signed('AC4-convention', dict(shared_tail_unchanged=True, legacy_subjects=10, spawns_nothing=True))


class NoSilentUndeterminedTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one
    signed = AuditStepTests.signed
    diagnostic = 'packet: incomplete criterion AC6 from /home/albert/.do-it/content/L-spec-1706.md\n'

    def built(self):
        return [event('spec-written', -90), event('build-done', -80, actor='builder', status='DONE')]

    def context(self, events):
        return dict(evs=events, spec=dict(state='graded'), dependency=False,
                    capabilities={}, sid=SID)

    def test_packet_classification(self):
        positives = [self.diagnostic, self.diagnostic.rstrip('\n'),
                     'packet: incomplete criterion AC6 from spec.md\n',
                     'packet: incomplete criterion AC6 from spec.md',
                     self.diagnostic.replace('AC6', 'AC42')]
        for text in positives:
            with self.subTest(text=text):
                self.assertEqual(ns.classify_failure(text, 1), 'spec-defect')
                self.assertEqual(ns.classify_failure(text, None), 'unknown')
                for other in ('ENOSPC', 'FAILED', 'ENOSPC\nAssertionError'):
                    self.assertEqual(ns.classify_failure(text + '\n' + other, 1), 'unknown')
        for text in ('incomplete criterion', 'packet: unrelated error', '',
                     'mentioned packet: incomplete criterion AC6 from spec.md',
                     'packet: incomplete criterion AC6',
                     'packet: incomplete criterion AC6 from '):
            self.assertEqual(ns.classify_failure(text, 1), 'unknown')
        for text, code, expected in (('ENOSPC', 1, 'infra'), ('FAILED', 1, 'work'),
                                     ('ENOSPC\nFAILED', 1, 'unknown'), ('', 124, 'infra'),
                                     ('invalid_json_schema', 1, 'infra'),
                                     ('ledger cache: lock not acquired within 120s', 1, 'infra')):
            self.assertEqual(ns.classify_failure(text, code), expected)
        self.signed('L-spec-1853/AC1', dict(positive_classes=[ns.classify_failure(t, 1) for t in positives],
                                         conservative_mixed_evidence=True))

    def test_spec_defect_hold_lifecycle(self):
        for field in ('findings', 'why', 'error', 'reason'):
            failure = event('autodispatch-failed', -60, **{field: self.diagnostic})
            events = self.built() + [failure]
            with self.subTest(field=field):
                self.assertEqual(ns._failure(events, self.rules), (failure, 'spec-defect'))
                step = ns.step_for(events, specs_for(events), SID, NOW, self.rules)
                self.assertEqual(self.steps(events), [step])
                self.assertEqual((step.state, step.action, step.owner), ('hold', None, 'executor'))
                self.assertTrue(step.reason.startswith('spec-defect:'))
                self.assertIn('incomplete criterion AC6', step.reason)
                self.assertRegex(step.clears, 'spec.*(amend|rewrite)')
                self.assertIn('executor', step.clears)
                self.assertIn('reconsider', step.clears)
                self.assertEqual((step.trigger, step.since), (failure['_src'], failure['ts']))
                self.assertEqual(step, self.one(events, now=NOW + timedelta(days=1)))
                self.assertEqual(step, self.one(events + [event('evidence-bundle', -50)]))
                for kind in ('spec-written', 'decision'):
                    cleared = events + [event(kind, -40, actor='executor')]
                    self.assertEqual(ns._failure(cleared, self.rules), (None, None))
                    self.assertEqual(self.one(cleared).action, 'grade')
                # Diagnostic holds precede questions, busy attempts, and rework.
                for extra in (event('question', -55),
                              event('autodispatched', -55, role='builder'),
                              event('spawn-started', -70, spawn='L-builder-9001', role='builder')):
                    self.assertEqual(self.one(events + [extra]), step)
                for prefix in ns.NOT_READY_PREFIXES:
                    refused = event('spawn-failed', -50, reason=prefix + ': fixture')
                    self.assertEqual(self.one(events + [refused]), step)
        self.signed('L-spec-1853/AC2', asdict(step))

    def test_stale_gate_review_and_controls(self):
        original = self.built() + [event('verdict', -70, confirmed=True),
                                  event('review', -60, n_blocking=0),
                                  event('merge-gate-clean', -50)]
        for kind in ('verdict', 'cannot-assess'):
            judgment = event(kind, -40, **({'confirmed': True} if kind == 'verdict' else {}))
            events = original + [judgment]
            with self.subTest(kind=kind):
                self.assertIsNone(ns._enqueue(self.context(events)))
                step = ns.step_for(events, specs_for(events), SID, NOW, self.rules)
                self.assertEqual(self.steps(events), [step])
                self.assertEqual((step.state, step.action, step.owner), ('action', 'review', 'tick'))
                self.assertEqual((step.trigger, step.since), (judgment['_src'], judgment['ts']))
                no_gate = [e for e in events if e['type'] != 'merge-gate-clean']
                self.assertEqual(self.one(no_gate).action, 'review')
                # No intervening build: the old gate cannot authorize this review.
                reviewed = events + [event('autodispatched', -30, role='reviewer'),
                                     event('review', -20, n_blocking=0)]
                self.assertNotIn('confirmed', reviewed[-1])
                self.assertIsNone(ns._enqueue(self.context(reviewed)))
                self.assertEqual(ns._gate(self.context(reviewed))['since'], reviewed[-1]['ts'])
                gate_step = self.one(reviewed)
                self.assertEqual(gate_step.action, 'gate')
                self.assertEqual((gate_step.trigger, gate_step.since),
                                 (reviewed[-1]['_src'], reviewed[-1]['ts']))
                current = reviewed + [event('merge-gate-clean', -10)]
                self.assertEqual(ns._enqueue(self.context(current))['since'], reviewed[-1]['ts'])
                self.assertEqual(self.one(current).action, 'enqueue')
                for terminal in ('queue-add', 'window-dropped', 'merge-conflict', 'shipped'):
                    suppressed = current + [event(terminal, -5, project='fixture', branch='fixture')]
                    self.assertIsNone(ns._enqueue(self.context(suppressed)))
                    self.assertFalse(any(s.action == 'enqueue' for s in self.steps(suppressed)))
                self.signed('L-spec-1853/AC3-' + kind, dict(review=asdict(step), gate=asdict(gate_step),
                                                         enqueue=asdict(self.one(current))))
        negative = original + [event('verdict', -40, confirmed=False)]
        self.assertIsNone(ns._enqueue(self.context(negative)))
        self.assertNotEqual(self.one(negative).action, 'enqueue')
        for gate_time in (-40, -30):
            malformed = self.built() + [event('verdict', -40, confirmed=True),
                                        event('merge-gate-clean', gate_time)]
            with self.subTest(gate_time=gate_time):
                # Equal-time gates remain current; both inputs lack a clean review.
                with self.assertRaisesRegex(ValueError, 'clean review timestamp'):
                    ns._enqueue(self.context(malformed))
                self.assertTrue(self.one(malformed).reason.startswith('undetermined:'))
        # A gate that predates its clean review cannot authorize that review.
        late_review = original + [event('review', -20, n_blocking=0)]
        self.assertIsNone(ns._enqueue(self.context(late_review)))

    def test_compatibility_and_purity(self):
        import grading_env
        import tick
        rules = self.rules
        events = self.built() + [event('autodispatch-failed', -60, error=self.diagnostic + '\x1b\n')]
        review_events = self.built() + [event('verdict', -70, confirmed=True),
                                       event('review', -60, n_blocking=0),
                                       event('merge-gate-clean', -50),
                                       event('cannot-assess', -40)]
        specs = specs_for(events)
        originals = deepcopy((events, review_events, specs, rules))
        with ExitStack() as stack:
            for hook in ('builtins.open', 'io.open', 'pathlib.Path.open', 'pathlib.Path.write_text',
                         'subprocess.run', 'subprocess.Popen', 'dispatch.emit', 'fold.read_events',
                         'time.time'):
                stack.enter_context(patch(hook, side_effect=AssertionError(hook)))
            allow_dependency_reads(stack)
            stack.enter_context(patch.object(tick, 'launch', side_effect=AssertionError('launch')))
            self.assertEqual(grading_env.failure_kind(self.diagnostic), 'unknown')
            held = ns.step_for(events, specs, SID, NOW, rules)
            review = ns.step_for(review_events, specs, SID, NOW, rules)
            self.assertEqual((held.owner, held.action), ('executor', None))
            self.assertEqual(review.action, 'review')
            self.assertEqual(set(asdict(held)), KEYS)
            for invalid in (False, '0', 0.0, None):
                malformed = review_events + [event('review', -20, n_blocking=invalid)]
                self.assertIsNone(ns._clean_review(self.context(malformed)))
            for kind in ('rejected-criterion', 'must-fix'):
                rejected = review_events + [event('review', -20, n_blocking=0),
                                             event(kind, -10, criterion='AC1')]
                self.assertIsNone(ns._clean_review(self.context(rejected)))
            self.assertNotIn('\x1b', ns._clean(held.reason))
            self.assertNotIn('\n', ns._clean(held.reason))
            self.assertEqual(json.loads(json.dumps(asdict(held))), asdict(held))
        # The real launch guard must return before any dispatcher or ledger access.
        with patch.object(dispatch, 'emit', side_effect=AssertionError('emit')), \
                patch.object(fold, 'read_events', side_effect=AssertionError('read')):
            self.assertIsNone(tick.launch(held, dispatcher=lambda argv: self.fail('dispatch')))
        self.assertEqual((events, review_events, specs, rules), originals)
        self.signed('L-spec-1853/AC4', dict(grading_failure_kind='unknown', pure=True,
                                         hold=asdict(held), review=asdict(review)))


class R7BaseRefreshTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one

    def signed(self, name, body):
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://r7/' + name, status=0,
            body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=body)))

    def test_failure_class_and_step(self):
        import grading_env
        for name in ('stale-base', 'branch-not-descendant', 'pinned-base-mismatch'):
            for diagnostic in (name, 'reason=' + name, 'problem:' + name + ' — refresh'):
                self.assertEqual(ns.classify_failure(diagnostic, 1), 'base-refresh', diagnostic)
                self.assertEqual(grading_env.failure_kind(diagnostic), 'unknown')
            for diagnostic in ('prefix-' + name, name + '-suffix', 'question about ' + name,
                               'problem:' + name + '-suffix', 'reason=' + name + '-suffix'):
                self.assertEqual(ns.classify_failure(diagnostic, 1), 'unknown', diagnostic)
            kind = 'build-blocked' if name == 'pinned-base-mismatch' else 'spawn-failed'
            events = [event('spec-written', -60, actor='operator'),
                      event(kind, -20, actor='builder', spawn='L-builder-9774', reason='problem:' + name + ' — refresh')]
            step = self.one(events)
            self.assertEqual((step.owner, step.state, step.action), ('tick', 'action', 'build'))
            for word in ('base-refresh', 'regenerate', 'redispatch', 'safe auto-rebase'):
                self.assertIn(word, step.reason)
            self.assertIsNone(step.clears)
            self.assertEqual(self.one(events), step)
            self.signed('R7-' + name, asdict(step))
            started = events + [event('build-started', -10, spawn='L-builder-9775')]
            self.assertNotIn('base-refresh', self.one(started).reason)
        self.assertNotIn('build-blocked', ns.FAILURES)
        for text, expected in (('FAILED fixture', 'work'), ('No space left on device', 'infra'),
                               ('FAILED No space left on device', 'unknown'), ('other', 'unknown')):
            self.assertEqual(ns.classify_failure(text, 1), expected)
            self.assertEqual(grading_env.failure_kind(text), expected)
        baseline = [event('spec-written', -90, actor='operator'),
                    event('build-blocked', -40, actor='builder', spawn='L-builder-9774', reason='needs judgment'),
                    event('question', -30, actor='builder', spawn='L-builder-9774', asks='Which option?')]
        original = self.one(baseline)
        incidental = deepcopy(baseline)
        incidental[-1]['asks'] += ' stale-base pinned-base-mismatch'
        incidental[-2]['why'] = 'reason=pinned-base-mismatch'
        self.assertEqual(self.one(incidental), original)
        answered = baseline + [event('decision', -10, actor='operator', ref=baseline[-1]['_src'])]
        self.assertEqual(self.one(answered).action, 'rebuild')
        self.signed('R7-unrelated-blocked', dict(original=asdict(original), answered=asdict(self.one(answered))))
        exhausted = [event('spec-written', -90, actor='operator')]
        exhausted += [event('spawn-failed', -60 + n * 10, actor='tick', role='builder',
                            spawn=f'L-builder-{9800 + n}', reason='vanished') for n in range(3)]
        exhausted += [event('spawn-failed', -10, actor='builder', spawn='L-builder-9803', reason='stale-base')]
        held = self.one(exhausted)
        self.assertIn('retry exhausted: 3', held.reason)
        self.assertEqual((held.owner, held.action), ('executor', None))
        self.signed('R7-bounded-exhaustion', asdict(held))


class GateToolingTests(unittest.TestCase):
    setUp = NextStepTests.setUp
    steps = NextStepTests.steps
    one = NextStepTests.one
    signed = AuditStepTests.signed

    def baseline(self):
        return [event('spec-written', -120), event('build-done', -110, status='DONE'),
                event('review', -100, n_blocking=0)]

    def failure(self, minutes=-30, attempt='one', **extra):
        return event('merge-gate-undetermined', minutes, actor='executor',
                     _src=f'L-executor-fixture.jsonl:{20000+minutes}', reason='tool',
                     repo='/repo/.git', sha='a'*40, branch_sha='a'*40, main_sha='b'*40,
                     branch='work', cycle=self.baseline()[-1]['ts'], attempt=attempt,
                     retryable=True, retry_at=(NOW+timedelta(minutes=minutes+5)).isoformat(),
                     hold_reason=None, findings=[]) | extra

    def test_due_boundaries_anchor_and_ladder(self):
        base = self.baseline()
        first = self.failure()
        dispatch_row = event('autodispatched', -80, rule='gate', since=base[-1]['ts'], trigger='retry')
        evs = base + [dispatch_row, first]
        due = ns._time(first['retry_at'])
        for seconds in (-1, 0, 1):
            clock = due + timedelta(seconds=seconds)
            step = self.one(evs, now=clock)
            self.assertEqual(step.action, 'gate' if seconds >= 0 else None)
            if seconds >= 0:
                self.assertEqual((step.owner, step.since, step.trigger), ('tick',first['ts'],first['_src']))
                rows = [r for r in lr.due(evs, specs_for(evs), clock) if r['rule']=='gate']
                self.assertEqual(len(rows), 1)
                self.assertEqual(lr.ladder(evs, rows[0], clock), 'act')
        self.assertEqual(self.one(evs), self.one(evs))
        self.signed('L-spec-1869/AC2-boundaries', dict(since=first['ts'], retry_at=first['retry_at'], boundaries=[-1,0,1]))

    def test_identity_counts_caps_unblock(self):
        base = self.baseline()
        first, second = self.failure(-40), self.failure(-20,'two', retry_at=(NOW-timedelta(minutes=5)).isoformat())
        third = self.failure(-3,'three', retryable=False, retry_at=None, hold_reason='retry-cap-exhausted')
        for tail, count in [([first],1), ([first,first],1), ([first,second],2)]:
            evs = base + tail
            self.assertEqual(ns._gate_attempts(evs, tail[-1])[0], count)
            self.assertEqual(self.one(evs).action, 'gate')
        evs = base + [first,second,third]
        held = self.one(evs)
        self.assertEqual((held.action, held.owner, held.reason), (None,'executor','retry-cap-exhausted'))
        unrelated = event('decision', -1, text='unrelated')
        self.assertEqual(self.one(evs+[unrelated]).reason, 'retry-cap-exhausted')
        reset = event('unblocked', -1)
        self.assertEqual(self.one(evs+[reset]).action, 'gate')
        self.assertEqual(ns._gate_attempts(evs+[reset], third)[0], 0)
        fresh = self.failure(-1,'new-sha', sha='c'*40, branch_sha='c'*40,
                             retry_at=(NOW-timedelta(seconds=1)).isoformat())
        self.assertEqual(self.one(evs+[fresh]).action, 'gate')
        self.assertEqual(ns._gate_attempts(evs+[fresh], fresh)[0], 1)
        self.signed('L-spec-1869/AC2-identities', dict(duplicate_count=1, cap=asdict(held), reset=True))

    def test_refusals_live_owner_and_lock_boundary(self):
        first = self.failure(-4)
        lock = self.failure(-2,'', reason='lock-busy', retry_at=None)
        evs = self.baseline()+[first,lock]
        due = ns._time(first['retry_at'])
        self.assertIsNone(self.one(evs, now=due-timedelta(seconds=1)).action)
        self.assertEqual(self.one(evs, now=due).action, 'gate')
        self.assertEqual(self.one(evs, now=due).since, lock['ts'])
        for refusal in ('lock','duplicate','dispatch-in-progress'):
            refused = event('spawn-failed', -1, role='gate', reason=refusal)
            self.assertEqual(self.one(evs+[refused], now=due).action, 'gate')
        live = event('spawn-started', 0, role='executor', spawn='L-executor-9002')
        self.assertIsNone(self.one(evs+[live], now=due).action)
        released = event('spawn-done', 0, spawn='L-executor-9002')
        self.assertEqual(self.one(evs+[live,released], now=due).action, 'gate')

    def test_cycles_cancellation_and_permanent_holds(self):
        failure = self.failure()
        base = self.baseline()+[failure]
        for hold in ('configuration','ref-resolution','scan-cap-exhausted','scan-metadata'):
            evs = self.baseline()+[failure | dict(retryable=False,hold_reason=hold,retry_at=None)]
            self.assertEqual((self.one(evs).action, self.one(evs).reason), (None,hold))
        newer_review = event('review', -1, n_blocking=0)
        self.assertEqual(self.one(base+[newer_review]).action, 'gate')
        self.assertEqual(self.one(base+[newer_review]).since, newer_review['ts'])
        built = event('build-done', -1)
        self.assertEqual(self.one(base+[built]).reason, 'review-required')
        clean = event('merge-gate-clean', -1)
        self.assertEqual(self.one(base+[clean]).action, 'enqueue')
        stale_clean = self.baseline()+[event('merge-gate-clean', -50), failure]
        self.assertEqual(self.one(stale_clean).action, 'gate')
        rework = event('merge-gate-rework', -1, reason='actual guard')
        self.assertNotEqual(self.one(stale_clean+[rework]).action, 'enqueue')
        terminal = event('shipped', -1)
        self.assertEqual(self.steps(base+[terminal], {SID:dict(state='shipped')}), [])

    def test_later_pipeline_outcomes_supersede_tooling_hold(self):
        for failure in (self.failure(), self.failure(retryable=False, retry_at=None,
                                                    hold_reason='configuration')):
            base = self.baseline() + [failure]
            blocked = event('review', -3, n_blocking=1)
            built = event('build-done', -2, status='DONE', actor='builder')
            verdict = event('verdict', -1, confirmed=True, actor='grader')
            for tail, action in (([blocked], 'rebuild'), ([built, verdict], 'review')):
                with self.subTest(hold=failure['hold_reason'], action=action):
                    evs = base + tail
                    step = self.one(evs)
                    self.assertEqual((step.state, step.action, step.owner), ('action', action, 'tick'))
                    self.assertEqual((step.trigger, step.since), (tail[-1]['_src'], tail[-1]['ts']))
                    self.assertFalse(any(row['rule'] in {'gate', 'enqueue'}
                                         for row in lr.due(evs, specs_for(evs), NOW)))
                    self.signed('L-spec-1873/AC2-later-' + action, asdict(step))
            # A build alone still requires a new clean review before another gate.
            self.assertEqual(self.one(base + [built]).reason, 'review-required')
            self.assertNotIn(self.one(base + [blocked, built]).action, {'rebuild', 'gate', 'enqueue'})


class BundleStepTests(unittest.TestCase):
    """Snapshot selection never buys a grader to discover missing evidence."""

    steps = NextStepTests.steps
    one = NextStepTests.one

    def setUp(self):
        from test_factory_evidence import ReadOnlyBundleTests
        self.fixture = ReadOnlyBundleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.rules = lr.load()
        self.now = datetime(2026, 11, 6, 1, tzinfo=timezone.utc)
        self.rows = [event('spec-written', -120),
                     dict(self.fixture.build, status='DONE', actor='builder', _src='L-builder-fixture.jsonl:1')]

    def selected(self, rows):
        return self.one(rows, now=self.now)

    def signed(self, name, matrix):
        body = json.dumps(matrix, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://bundle/' + name, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_selection(self):
        matrix = {}
        for name, extra, expected in [
                ('done', [], 'bundle'),
                ('worked', [], 'bundle'),
                ('regrade', [event('decision', regrade='yes')], 'bundle'),
                ('blocked', [event('build-done', status='BLOCKED')], None),
                ('missing-sha', [event('build-done', status='DONE')], 'grade'),
                ('close', [event('decision', close='yes')], 'close'),
                ('dependency', [event('dependency-hold', waits_for='L-spec-9999')], None),
                ('terminal', [event('spec-closed', disposition='closed-unbuilt')], None),
                ('rebuild', [event('decision', rework='yes')], 'rebuild'),
                ('stale-queue', [event('queue-add', -110, project='do-it-v2', sha='c'*40)], 'bundle')]:
            with self.subTest(name=name):
                rows = self.rows + extra
                if name == 'stale-queue':
                    rows = [self.rows[0], event('build-done', -115, actor='builder', status='DONE',
                                               ready_sha='c'*40), extra[0], self.rows[-1]]
                if name == 'worked':
                    rows.append(event('worked', role='builder', ts='2026-11-06T00:00:01Z'))
                if name in {'blocked', 'missing-sha'}:
                    rows[-1]['ts'] = self.rows[-1]['ts']
                    rows[-1]['actor'] = 'builder'
                if name in {'close', 'dependency', 'terminal', 'rebuild', 'regrade'}:
                    rows[-1]['ts'] = '2026-11-06T00:00:01Z'
                steps = [s for s in self.steps(rows, now=self.now) if s.item == SID]
                if name == 'terminal':
                    self.assertFalse(any(s.action in {'grade', 'regrade', 'bundle'} for s in steps))
                    matrix[name] = [asdict(s) for s in steps]
                    continue
                self.assertEqual(len(steps), 1)
                step = steps[0]
                self.assertEqual(step.action, expected, step)
                if expected == 'bundle':
                    self.assertEqual((step.stage, step.owner, step.state), ('checking', 'tick', 'action'))
                matrix[name] = asdict(step)
        self.signed('selection', matrix)

    def test_evidence_gate(self):
        from copy import deepcopy
        f = self.fixture
        matrix = {}
        cases = ['valid', 'abbreviated', 'other-subject', 'old-sha', 'subset', 'missing',
                 'bad-digest', 'escape', 'amended', 'rebuild', 'newer-failure',
                 'missing-identity', 'inconsistent-identity', 'cached']
        original = deepcopy(f.data)
        for name in cases:
            with self.subTest(name=name):
                f.publish(original)
                evidence = dict(f.event)
                rows = deepcopy(self.rows)
                expected = 'bundle'
                if name in {'valid', 'abbreviated', 'cached'}:
                    expected = 'grade'
                if name == 'abbreviated': rows[-1]['ready_sha'] = f.sha[:9]
                if name == 'other-subject': evidence['subject'] = 'L-spec-9002'
                if name == 'old-sha': evidence['sha'] = 'd'*40
                if name == 'subset': evidence['criteria'] = ['AC1']
                if name == 'missing': evidence['path'] += '.absent'
                if name == 'bad-digest': evidence['bundle_sha256'] = 'bad'
                if name == 'escape': evidence['path'] = str(f.root / 'outside.json')
                if name == 'rebuild': rows[-1]['ts'] = '2026-11-06T00:02:00Z'
                if name in {'missing-identity', 'inconsistent-identity'}:
                    data = deepcopy(original)
                    if name == 'missing-identity': data.pop('master_sha')
                    else: data['base_sha'] = 'e'*40
                    f.publish(data)
                    evidence = dict(f.event)
                    expected = None
                rows.append(evidence)
                if name == 'amended':
                    rows.append(event('spec-amended', ts='2026-11-06T00:03:00Z'))
                if name == 'newer-failure':
                    rows.append(event('evidence-bundle-failed', sha=f.sha, reason='checks failed',
                                      ts='2026-11-06T00:03:00Z'))
                before = f.snapshot()
                with f.no_effects(), patch.dict(os.environ, DOIT_ROOT=str(f.root)):
                    for _ in range(2):
                        step = self.selected(rows)
                        self.assertEqual(step.action, expected, step)
                        if expected is None: self.assertEqual(step.reason, 'undetermined: bundle identity')
                    import tick
                    tick.run_steps(rows, specs_for(rows), self.now, dry_run=True, steps=[step])
                self.assertEqual(before, f.snapshot())
                matrix[name] = asdict(step)
        f.publish(original)
        f.test_default_lookup_keeps_live_validation_and_reuse_receipt()
        with f.no_effects():
            regrade = self.selected(self.rows + [f.event, event('decision', regrade='yes',
                                                              ts='2026-11-06T00:03:00Z')])
        self.assertEqual(regrade.action, 'regrade')
        matrix['regrade'] = asdict(regrade)
        self.signed('evidence', matrix)

    def test_retry_boundaries(self):
        import attempts
        matrix = []
        start = ns._time(self.rows[-1]['ts']) + timedelta(seconds=2)
        for kind in ('checks failed', 'wall cap exceeded', 'launch', 'silent', 'dead', 'paired'):
            for refusal in ('bundle-pending', 'lock', 'duplicate-dispatch', 'dispatch-in-progress'):
                rows = deepcopy(self.rows)
                identity = ns._bundle_context(rows, SID, rows[-1]['_src'])
                for count in range(attempts.MAX_ATTEMPTS + 1):
                    with self.subTest(kind=kind, refusal=refusal, count=count):
                        h = ns._bundle_history(rows, identity, self.now)
                        self.assertEqual(h.n_failed, count)
                        if count:
                            instant = ns._time(rows[-1]['ts'])
                            self.assertEqual(ns._bundle_history(rows, identity, instant).n_failed, count)
                        ref = event('spawn-failed', ts=(start-timedelta(seconds=1)).isoformat(),
                                    role='grader', spawn='L-grader-9990', reason=refusal,
                                    why='another dispatch holds the subject lock')
                        # A lock refusal uses the existing dispatch-in-progress vocabulary.
                        if refusal == 'lock': ref['reason'] = 'dispatch-in-progress'
                        test_rows = rows + [ref]
                        due = h.next_retry_at or (failed+timedelta(seconds=1) if count else start)
                        moments = (due-timedelta(microseconds=1), due) if count and not h.exhausted else (due,)
                        for moment in moments:
                            step = self.one(test_rows, now=moment)
                            exhausted = count == attempts.MAX_ATTEMPTS
                            expected = None if exhausted or moment < due else 'bundle'
                            self.assertEqual(step.action, expected, step)
                            self.assertEqual(step.trigger, identity['ref'])
                            self.assertEqual(step.attempt, min(count+1, attempts.MAX_ATTEMPTS))
                            if exhausted:
                                self.assertIn('retries-exhausted: bundle', step.reason)
                                for reason in h.reasons: self.assertIn(reason, step.reason)
                            paid = attempts.history(ns._grader_history(test_rows)[0], rows[1], 'grader', now=moment)
                            self.assertEqual((paid.n_failed, paid.consumed), (0, False))
                            matrix.append(dict(case=kind, refusal=refusal, failures=count,
                                               due=str(h.next_retry_at), now=str(moment),
                                               attempt=step.attempt, action=step.action, reason=step.reason))
                        if count == attempts.MAX_ATTEMPTS: break
                        claimed = max(due, start) + timedelta(seconds=1)
                        failed = claimed + timedelta(seconds=1)
                        common = dict(identity, attempt=count+1, claimed_at=claimed.isoformat(),
                                      expires_at=(claimed+timedelta(seconds=60)).isoformat())
                        if kind in ('launch', 'silent', 'dead', 'paired'):
                            rows.append(event('autodispatch-skipped', ts=claimed.isoformat(),
                                              reason='bundle-claim', action='bundle', **common))
                            detail = {'launch':'FileNotFoundError', 'silent':'checker-receipt-timeout',
                                      'dead':'checker-vanished', 'paired':'checker-vanished'}[kind]
                            rows.append(event('autodispatch-skipped', ts=failed.isoformat(),
                                              reason='bundle-launch-failed', action='bundle', detail=detail, **common))
                        if kind in ('checks failed', 'wall cap exceeded', 'paired'):
                            rows.append(event('evidence-bundle-failed', ts=failed.isoformat(),
                                              sha=self.fixture.sha, reason=kind,
                                              reason_code='wall-cap' if kind == 'wall cap exceeded' else 'checks'))
                # A new build and a revision each own a new acquisition identity.
                for renewed in (dict(rows[1], ts=(failed+timedelta(seconds=1)).isoformat(),
                                     _src='L-builder-renewed.jsonl:1'),
                                event('spec-amended', ts=(failed+timedelta(seconds=1)).isoformat())):
                    spec_path = self.fixture.root/'content'/f'{SID}.md'
                    if renewed['type'] == 'spec-amended': spec_path.write_text(self.fixture.text+'Revised.\n')
                    reset = self.one(rows+[renewed], now=failed+timedelta(minutes=1))
                    spec_path.write_text(self.fixture.text)
                    self.assertEqual((reset.action, reset.attempt), ('bundle', 1), reset)
                # Late usable success releases acquisition even after the retry cap.
                success = dict(self.fixture.event, ts=(failed+timedelta(seconds=1)).isoformat())
                self.assertEqual(self.one(rows+[success], now=failed+timedelta(minutes=1)).action, 'grade')
        self.signed('retry-boundaries', matrix)

    def test_receiver_safety(self):
        import grader_packet as gp
        class Refused(Exception): pass
        def fail(message, **fields): raise Refused(fields['reason'])
        matrix = {}
        f = self.fixture
        gp_bundle = gp.bundle_for
        with patch.object(gp.subprocess, 'Popen', side_effect=AssertionError('receiver checker launch')), \
                patch.object(dispatch, 'run_codex', side_effect=AssertionError('paid entry')), \
                patch.object(dispatch, 'run_seat', side_effect=AssertionError('paid entry')), \
                patch.object(gp, 'bundle_for', wraps=lambda *a, **kw: gp_bundle(*a, **kw, identity=(f.base, 'c'*40, None))), \
                patch.object(__import__('build_runner'), '_event'):
            for mode in ('attached', 'detached'):
                for name in ('missing', 'stale', 'invalid', 'failed', 'wall-cap', 'wall-cap-retried', 'digest', 'usable'):
                    rows = deepcopy(self.rows)
                    emitted = []
                    if name in ('stale', 'invalid', 'digest', 'usable'):
                        evidence = dict(f.event)
                        if name == 'stale': evidence['ts'] = rows[1]['ts']
                        if name == 'invalid': evidence['bundle_sha256'] = 'bad'
                        rows.append(evidence)
                    if name in ('failed', 'wall-cap', 'wall-cap-retried'):
                        rows.append(event('evidence-bundle-failed', sha=f.sha, ts='2026-11-06T00:02:00Z',
                                          reason='wall cap exceeded' if name.startswith('wall-cap') else 'checks failed'))
                    if name == 'wall-cap-retried':
                        rows.append(event('infra-failure', role='grader', ts='2026-11-06T00:03:00Z'))
                    packet = 'bundle_sha256: `' + (f.event['bundle_sha256'] if name == 'usable' else 'wrong') + '`'
                    expected = ('wall-cap-retry' if name == 'wall-cap-retried' else 'wall-cap' if name == 'wall-cap'
                                else 'preflight:runner' if name == 'failed' else 'bundle-pending')
                    if name == 'usable':
                        self.assertIsNotNone(gp.gate(rows, SID, packet, lambda *a, **kw: emitted.append(kw), fail))
                    else:
                        with self.assertRaisesRegex(Refused, expected):
                            gp.gate(rows, SID, packet, lambda *a, **kw: emitted.append(kw), fail)
                    self.assertFalse(any(e.get('reason') == 'wall-cap-retry' for e in emitted))
                    matrix[mode+'/'+name] = dict(refusal=expected if name != 'usable' else None, metadata=emitted)
        self.signed('receiver-safety', matrix)

    def test_empty_grade_preserved(self):
        from test_empty_grade import EmptyGradeTests
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(EmptyGradeTests).run(result)
        self.assertEqual(result.testsRun, 7)
        self.assertTrue(result.wasSuccessful(), (result.failures, result.errors))


class GraderRetryTests(unittest.TestCase):
    """Grading has distinct transport, checker-request and genuine budgets."""

    READY_SHA = 'a' * 40

    def setUp(self):
        import grader_packet
        self.rules = lr.load()
        temporary = tempfile.TemporaryDirectory(prefix='grader-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.content = self.root/'content'
        self.content.mkdir()
        (self.content/f'{SID}.md').write_text('AC1 [backend]: retry fixture.\n')
        self.addCleanup(patch.stopall)
        patch.object(dispatch, 'CONTENT', self.content).start()
        patch.object(grader_packet, 'root', return_value=self.root).start()

    steps = NextStepTests.steps
    one = NextStepTests.one

    def fixture(self, action='grade', *, ready=False):
        rows = [event('spec-written', -120),
                event('build-done', -100, actor='builder', status='DONE', **(dict(ready_sha='a'*40) if ready else {}))]
        if action == 'regrade':
            rows.append(event('decision', -90, actor='thinker', regrade='yes'))
        return rows

    def refusal(self, minute=-4, reason='bundle-pending', number=1, **extra):
        return event('spawn-failed', minute, spawn=f'L-grader-{9100+number}',
                     reason=reason, **extra)

    def signed(self, case, data):
        body = json.dumps(data, sort_keys=True, default=str)
        print(json.dumps(dict(url='cli://grader-retry/' + case, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_retry1851_AC1_detached_marker_preserves_trigger_and_request_window(self):
        for action in ('grade', 'regrade'):
            rows = self.fixture(action, ready=True)
            trigger = rows[-1]
            rows += [event('autodispatched', -5, role='grader', spawn='12345'), self.refusal()]
            for seconds in (0, 59, 301):
                step = self.one(rows, now=NOW + timedelta(seconds=seconds))
                self.assertEqual((step.action, step.attempt), ('bundle', 1))
                self.assertEqual((step.trigger, step.since), (trigger['_src'], trigger['ts']))
                self.assertIn('evidence required', step.reason)
        self.signed('AC1-detached', asdict(step))

    def test_retry1851_AC1_silent_checker_cap_late_evidence_and_resets(self):
        import attempts
        for action in ('grade', 'regrade'):
            rows = self.fixture(action, ready=True)
            trigger = rows[-1]
            for n in range(1, 4):
                minute = -20 + 5*n
                rows += [event('autodispatched', minute, role='grader', spawn=str(12340+n)),
                         self.refusal(minute, number=n)]
                step = self.one(rows)
                self.assertEqual((step.action, step.attempt), ('bundle', 1))
                self.assertEqual(step.trigger, trigger['_src'])
                h = attempts.history(ns._grader_history(rows)[0], trigger, 'grader', now=NOW)
                self.assertEqual((h.n_failed, h.reasons), (0, ()))
            # Legacy grader request receipts do not become checker attempts.
            self.assertEqual(self.one(rows, now=NOW+timedelta(days=100)).action, 'bundle')
            new_build = event('build-done', 1, actor='builder', status='DONE', ready_sha='b'*40)
            self.assertEqual(self.one(rows+[new_build], now=NOW+timedelta(minutes=2)).action, 'bundle')
            decision = event('decision', 1, regrade='yes', actor='operator')
            self.assertEqual(self.one(rows+[decision], now=NOW+timedelta(minutes=2)).action, 'bundle')
        self.signed('AC1-cap', dict(legacy_requests=3, checker_attempts=0, history=asdict(h)))

    def test_retry1851_AC1_build_freshness_and_real_gate(self):
        import grader_packet as gp
        class Refused(Exception):
            pass
        def fail(why, reason=None):
            raise Refused(reason)
        with patch.dict(os.environ, DOIT_ROOT=str(self.root)):
            directory = self.root
            path = Path(directory)/'content'/'evidence'/SID/'bundle.json'
            path.parent.mkdir(parents=True)
            raw = json.dumps(dict(spec=SID, sha='a'*40, commands=[], comparison={})).encode()
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for action in ('grade', 'regrade'):
                rows = self.fixture(action, ready=True)+[self.refusal()]
                success = event('evidence-bundle', -10, sha='a'*40, path=str(path), bundle_sha256=digest)
                packet = 'bundle_sha256: `' + digest + '`'
                self.assertEqual(self.one(rows+[success]).action, action)
                with patch.object(gp.subprocess, 'Popen') as checker:
                    with self.assertRaisesRegex(Refused, 'bundle-pending'):
                        gp.gate(rows+[success], SID, 'stale packet without digest', lambda *a, **k: None, fail)
                    checker.assert_not_called()
                    self.assertIsNotNone(gp.gate(rows+[success], SID, packet, lambda *a, **k: None, fail))
                failed = event('evidence-bundle-failed', -4, sha='a'*40, reason='checker failed')
                self.assertTrue(self.one(rows+[failed]).reason.startswith('retry-wait: bundle'))
                with self.assertRaisesRegex(Refused, 'preflight:runner'):
                    gp.gate(rows+[failed], SID, packet, lambda *a, **k: None, fail)
                for evidence in (dict(success, ts=rows[1]['ts']), dict(success, sha='c'*40),
                                 dict(success, subject='L-spec-9999'),
                                 dict(success, ts=event('x', -101)['ts'])):
                    self.assertEqual(self.one(rows+[evidence]).action, 'bundle')
                    with patch.object(gp.subprocess, 'Popen'), self.assertRaisesRegex(Refused, 'bundle-pending'):
                        gp.gate(rows+[evidence], SID, packet, lambda *a, **k: None, fail)
                # Same-second result order is ledger order, including failures before refusal.
                tied = dict(failed, ts=success['ts'])
                for tail, expect_failure in (([success, tied], True), ([tied, success], False)):
                    if expect_failure:
                        with self.assertRaisesRegex(Refused, 'preflight:runner'):
                            gp.gate(rows+tail, SID, packet, lambda *a, **k: None, fail)
                    else:
                        self.assertIsNotNone(gp.gate(rows+tail, SID, packet, lambda *a, **k: None, fail))
                for mutation in (dict(path=str(path.with_name('missing'))), dict(bundle_sha256='bad')):
                    corrupt = dict(success, **mutation)
                    self.assertEqual(self.one(rows+[corrupt]).action, 'bundle')
                    with self.assertRaisesRegex(Refused, 'preflight:runner'):
                        gp.gate(rows+[corrupt], SID, packet, lambda *a, **k: None, fail)
        self.signed('AC1-freshness', dict(pre_refusal=True, same_second_failure=True, digest_checked=True))

    def test_retry1851_AC2_only_proven_contention_is_free(self):
        for action in ('grade', 'regrade'):
            rows = self.fixture(action)
            for n in range(3):
                rows += [event('autodispatched', -20+n, role='grader', spawn=str(13000+n)),
                         self.refusal(-20+n, 'dispatch-in-progress', n,
                                      why='another dispatch holds the subject lock')]
            self.assertEqual(self.one(rows).action, action)
            rows.append(self.refusal(-4, 'dispatch-in-progress', 9,
                                     why='cannot open the dispatch lock ([Errno 13] Permission denied)'))
            step = self.one(rows)
            self.assertTrue(step.reason.startswith('retry-wait:')); self.assertEqual(step.retry_at, (NOW+timedelta(minutes=1)).isoformat())
            self.assertEqual(self.one(rows, now=NOW+timedelta(minutes=1)).action, action)
        self.signed('AC2-contention', asdict(step))

    def test_retry1851_AC2_live_start_without_spawn_stays_suppressed(self):
        for action in ('grade', 'regrade'):
            rows = self.fixture(action)
            rows.append(event('spawn-started', -1, role='grader'))
            self.assertFalse(any(step.action == action for step in self.steps(rows)))

    def test_retry1851_AC1_undated_failure_keeps_wall_cap_hold(self):
        import grader_packet as gp
        class Refused(Exception):
            pass
        def fail(why, reason=None):
            raise Refused(reason)
        rows = [dict(type='build-done', subject=SID, ready_sha='a'*40),
                dict(type='evidence-bundle-failed', subject=SID, sha='a'*40,
                     reason='wall cap exceeded')]
        with patch.object(gp.subprocess, 'Popen') as checker:
            with self.assertRaisesRegex(Refused, '^wall-cap$'):
                gp.gate(rows, SID, 'no digest', lambda *a, **k: None, fail)
            checker.assert_not_called()

    def test_retry1851_AC2_genuine_ladder_and_adjacent_roles(self):
        import attempts
        for action in ('grade', 'regrade'):
            for kind in ('spawn-failed', 'autodispatch-failed'):
                rows = self.fixture(action)
                trigger = rows[-1]
                for n, minute in ((1, -40), (2, -30), (3, -10)):
                    rows.append(event(kind, minute, role='grader', spawn=f'L-grader-{9200+n}',
                                      reason='preparation failed'))
                    deadline = NOW+timedelta(minutes=minute+(5, 15, 45)[n-1])
                    for second in (-1, 0):
                        step = self.one(rows, now=deadline+timedelta(seconds=second))
                        if n == 3:
                            self.assertTrue(step.reason.startswith('retries-exhausted: grader; 3 failed attempts; preparation failed'))
                        elif second == -1:
                            self.assertTrue(step.reason.startswith('retry-wait:')); self.assertEqual(step.retry_at, deadline.isoformat())
                        else:
                            self.assertEqual(step.action, action)
                        self.assertEqual((step.trigger, step.since), (trigger['_src'], trigger['ts']))
                    h = attempts.history(ns._grader_history(rows)[0], trigger, 'grader', now=deadline)
                    self.assertEqual(h.n_failed, n)
                late = event('evidence-bundle', -1, sha='a'*40)
                self.assertTrue(self.one(rows+[late], now=NOW+timedelta(days=1)).reason.startswith('retries-exhausted:'))
        for action in ('build', 'rebuild', 'review'):
            rows, role, trigger = VanishedRetryTests.fixture(self, action)
            for n, minute in ((1, -4), (2, -3), (3, -2)):
                VanishedRetryTests.failure(self, rows, role, n, minute)
                step = self.one(rows)
                self.assertTrue(step.reason.startswith('retries-exhausted:' if n == 3 else 'retry-wait:'))
                if n < 3:
                    due = NOW+timedelta(minutes=minute+(5, 15)[n-1])
                    self.assertTrue(self.one(rows, now=due).reason.startswith('retry due:'))
        self.signed('AC2-budgets', dict(max_attempts=3, backoff=attempts.BACKOFF_MIN,
                                      adjacent_roles_unchanged=True))

    def test_retry1851_AC2_association_and_visible_conflict(self):
        rows = self.fixture()
        marker = event('autodispatched', -6, role='grader', spawn='12345')
        refusal = self.refusal(reason='duplicate-dispatch')
        live = event('spawn-started', -5, role='grader', spawn='L-grader-9400')
        held = self.one(rows+[marker, live, refusal])
        self.assertTrue(held.reason.startswith('grade-wait: conflict'))
        self.assertIn(live['spawn']+' spawn-done/spawn-failed', held.clears)
        # The intervening start prevents pairing away the earlier parent.
        normalized, receipts = ns._grader_history(rows+[marker, live, refusal])
        self.assertIn(marker, normalized)
        self.assertEqual(receipts, [refusal])
        for terminal in ('spawn-done', 'spawn-failed'):
            clean = rows+[live, refusal, event(terminal, -3, role='grader', spawn=live['spawn'],
                                              reason='duplicate-dispatch')]
            # A completed real attempt without a verdict follows genuine history;
            # a duplicate refusal with no visible start simply re-offers.
            no_start = [e for e in clean if e is not live]
            if terminal == 'spawn-failed':
                self.assertEqual(self.one(no_start).action, 'grade')
        open_builder = event('spawn-started', -5, role='builder', spawn='L-builder-9400')
        before = self.one(rows+[open_builder, refusal])
        self.assertTrue(before.reason.startswith('grade-wait: conflict'))
        after = self.one(rows+[open_builder, refusal,
                              event('spawn-done', -3, role='builder', spawn=open_builder['spawn'])])
        self.assertEqual(after.action, 'grade')
        for change in (dict(role='reviewer', spawn='L-reviewer-9101'),
                       dict(subject='L-spec-9999'), dict(spawn=None),
                       dict(reason='dispatch-in-progress', why='cannot open lock')):
            foreign = dict(refusal, **change)
            normalized, _ = ns._grader_history(rows+[marker, foreign])
            self.assertIn(marker, normalized)
        self.signed('AC2-conflict', dict(before=asdict(before), after=asdict(after)))


class AttemptsRebuild1855Tests(unittest.TestCase):
    def fixture(self, action):
        written = event('spec-written', -240)
        evs = [written, event('spawn-done', -240, role='spec-auditor', actor='spec-auditor', bad_cut=False)]
        if action == 'rebuild':
            evs += [event('build-blocked', -200), event('decision', -180, rework='yes')]
        elif action == 'grade':
            evs += [event('build-done', -180, actor='builder', status='DONE')]
        elif action == 'regrade':
            evs += [event('build-done', -200, actor='builder', status='DONE'),
                    event('decision', -180, regrade='yes')]
        elif action == 'review':
            evs += [event('verdict', -180, actor='grader', confirmed=True)]
        return evs

    def one(self, evs, now=NOW, specs=None):
        specs = specs or {SID: dict(state='written' if len(evs) == 2 else 'building')}
        return ns.step_for(evs, specs, SID, now, lr.load())

    def context(self, evs, now=NOW):
        return dict(evs=evs, events=evs, sid=SID, now=now, dependency=None, wave=None,
                    spec=dict(state='written'), busy=set(), capabilities={})

    def evidence(self, name, body):
        body = json.dumps(body, sort_keys=True, default=str)
        print(json.dumps(dict(url='scratch://attempts-rebuild-1855/'+name, status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_AC1(self):
        import attempts
        results = []
        outputs = dict(builder=('build-done', 'build-blocked'), grader=('verdict', 'cannot-assess'), reviewer=('review',))
        for action, role in [('build', 'builder'), ('rebuild', 'builder'), ('grade', 'grader'),
                             ('regrade', 'grader'), ('review', 'reviewer')]:
            base = self.fixture(action)
            trigger = base[0] if action == 'build' else base[-1]
            predicate = ns.PREDICATES[action]
            variants = [('bare', [], True),
                        ('marker-live', [event('autodispatched', -1, role=role, spawn='123')], False),
                        ('marker-dead-due', [event('autodispatched', -100, role=role, spawn='123')], True),
                        ('start', [event('spawn-started', -1, role=role, spawn='L-'+role+'-9191')], False),
                        ('failed', [event('spawn-failed', -10, role=role, spawn='L-'+role+'-9191', reason='mystery')], True),
                        ('parent-failed', [event('autodispatch-failed', -10, role=role, reason='mystery')], True),
                        ('done-no-output', [event('spawn-done', -10, role=role, spawn='L-'+role+'-9191')], True),
                        ('stale', [event('spawn-stale', -10, role=role, spawn='L-'+role+'-9191')], True),
                        ('foreign-role', [event('autodispatched', -1, role='relay', spawn='123')], True),
                        ('older', [event('spawn-started', -250, role=role, spawn='L-'+role+'-9191'),
                                   event('spawn-failed', -10, role=role, spawn='L-'+role+'-9191', reason='older attempt')], True)]
            variants += [(kind, [event(kind, -10)], False) for kind in outputs[role]]
            variants += [(kind, [event(kind, -10)], False) for kind in ('queue-add', 'merge-gate-clean', 'shipped')]
            for label, tail, expected in variants:
                with self.subTest(action=action, variant=label):
                    with patch.object(attempts, 'history', wraps=attempts.history) as spy:
                        result = predicate(self.context(sorted(base + tail, key=lambda e: e['ts'])))
                    self.assertEqual(bool(result), expected, result)
                    if label in ('bare', 'marker-live', 'failed'):
                        self.assertTrue(any(c.args[2] == role and c.args[1]['subject'] == SID
                                            and c.args[1]['ts'] == trigger['ts'] for c in spy.call_args_list))
                    results.append([action, label, bool(result)])
            # The public reader must not strand an unknown worker failure.
            step = self.one(base + [event('spawn-failed', -10, role=role,
                                         spawn='L-'+role+'-9191', reason='unclassified worker exit')],
                            specs={SID: dict(state='written' if action == 'build' else 'building')})
            self.assertEqual(step.action, action, step)
        for module in ('test_attempts', 'test_owner_drop_rework', 'test_lane_rules'):
            run = subprocess.run(['/usr/bin/python3', '-m', 'unittest', module],
                                 env=dict(os.environ, PYTHONPATH='src', PYTHONDONTWRITEBYTECODE='1'),
                                 text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(run.returncode, 0, run.stdout[-1000:]+run.stderr[-5000:])
        self.evidence('AC1', results)

    def test_AC2(self):
        import attempts
        self.assertEqual((attempts.BACKOFF_MIN, attempts.MAX_ATTEMPTS), ((5, 15, 45), 3))
        checked = []
        for action, role in [('build', 'builder'), ('rebuild', 'builder'), ('grade', 'grader'),
                             ('regrade', 'grader'), ('review', 'reviewer')]:
            base = self.fixture(action)
            trigger = base[0] if action == 'build' else base[-1]
            fails = []
            for n, minute, delay in [(1, -60, 5), (2, -40, 15), (3, -20, None)]:
                fails.append(event('spawn-failed', minute, role=role, spawn='L-'+role+'-'+str(9190+n), reason='failure-'+str(n)))
                if delay is not None:
                    due = NOW + timedelta(minutes=minute+delay)
                    before = self.one(base+fails, due-timedelta(microseconds=1))
                    self.assertIsNone(before.action)
                    self.assertIn('retry-wait', before.reason)
                    self.assertIn(str(n), before.reason)
                    self.assertIn(due.isoformat(), before.reason + str(before.clears))
                    at = self.one(base+fails, due)
                    self.assertEqual(at.action, action, at)
                    later = self.one(base+fails+[event('note', -1)], NOW+timedelta(hours=1))
                    self.assertEqual((at.trigger, at.since), (trigger['_src'], trigger['ts']))
                    self.assertEqual((later.trigger, later.since), (at.trigger, at.since))
                    checked.append(asdict(before))
                else:
                    stop = self.one(base+fails)
                    self.assertIsNone(stop.action)
                    self.assertIn('retries-exhausted', stop.reason)
                    self.assertLess(stop.reason.index('failure-1'), stop.reason.index('failure-2'))
                    self.assertLess(stop.reason.index('failure-2'), stop.reason.index('failure-3'))
                    checked.append(asdict(stop))
        self.evidence('AC2', checked)

    def test_AC3(self):
        from test_fold_stalls import admitted, row, outcome, SID as real_sid, NOW as cut
        checked = []
        for legacy in (False, True):
            block = row('build-blocked', minute=0, spawn='L-builder-91909') if legacy else outcome('BLOCKED')
            prefix = [row('spec-written', 'planner', minute=-20), row('build-started', minute=-10, spawn='L-builder-91909'),
                      block, row('spawn-done', minute=1, role='builder', spawn='L-builder-91909')]
            with admitted(prefix) as (_, events, folded):
                self.assertEqual(folded[0][real_sid]['state'], 'building' if legacy else 'blocked')
                block_src = next(e['_src'] for e in events if e['type'] == block[1]['type'])
            for actor, kind, ref, expected in [
                    ('thinker', 'unblocked', None, True), ('operator', 'unblocked', None, True),
                    ('thinker', 'spec-amended', None, True), ('operator', 'spec-amended', None, True),
                    ('executor', 'unblocked', block_src, True), ('executor', 'unblocked', None, False),
                    ('executor', 'unblocked', 'stale:1', False), ('executor', 'spec-amended', block_src, False),
                    ('builder', 'unblocked', block_src, False), ('grader', 'unblocked', block_src, False),
                    ('reviewer', 'unblocked', block_src, False)]:
                release = row(kind, actor, minute=2, **({'ref': ref} if ref else {}))
                with self.subTest(legacy=legacy, actor=actor, kind=kind, ref=ref), admitted(prefix+[release]) as (_, events, folded):
                    self.assertEqual(folded[0][real_sid]['state'], 'building' if legacy else 'blocked')
                    steps = ns.next_steps(events, folded[0], cut, lr.load())
                    step = ns.step_for(events, folded[0], real_sid, cut, lr.load())
                    self.assertIn(step, steps)
                    self.assertEqual(step.action == 'rebuild', expected, step)
                    if expected:
                        source = next(e['_src'] for e in events if e['type'] == kind)
                        self.assertEqual((step.trigger, step.since, step.owner, step.stage),
                                         (source, release[1]['ts'], 'tick', 'building'))
                    checked.append(asdict(step))
            for tail in ([row('unblocked', 'operator', minute=0)],
                         [row('unblocked', 'operator', minute=-1)],
                         [row('unblocked', 'operator', minute=2), outcome('DONE', minute=3)],
                         [outcome('DONE', minute=2), row('unblocked', 'operator', minute=3)],
                         [row('unblocked', 'operator', minute=2), row('spec-killed', 'operator', minute=3)]):
                with admitted(prefix+tail) as (_, events, folded):
                    self.assertFalse(any(s.item == real_sid and s.action == 'rebuild'
                                         for s in ns.next_steps(events, folded[0], cut, lr.load())))
        self.evidence('AC3', checked)


class RetryRegressions1857Tests(unittest.TestCase):
    def fixture(self, role):
        helper=AttemptsRebuild1855Tests()
        action={'builder':'build','grader':'grade','reviewer':'review'}[role]
        evs=helper.fixture(action)
        trigger=evs[0] if role=='builder' else evs[-1]
        for i,minute in enumerate((-90,-70,-50)):
            evs.append(event('spawn-failed',minute,role=role,spawn=f'L-{role}-{9900+i}',reason=f'failure-{i}'))
        hold=event('escalation-blocking',-30,actor='tick',role=role,ref=trigger['_src'],
                   owner='thinker',default='wait',deadline='2026-10-24T12:00:00Z',revert='nothing to revert')
        hold.update(kind='retries-exhausted',_src='L-tick-retries.jsonl:1');evs.append(hold)
        return evs,hold

    def test_retry1857_AC2_authorized_release_matrix(self):
        for role in ('builder','grader','reviewer'):
            for actor in ('thinker','operator'):
                for kind in ('decision','unblocked'):
                    with self.subTest(role=role,actor=actor,kind=kind):
                        evs,hold=self.fixture(role)
                        release=event(kind,-20,actor=actor);release['_src']=f'L-{actor}-release.jsonl:1'
                        evs.append(release)
                        specs={SID:dict(state='written' if role=='builder' else 'building')}
                        s=ns.step_for(evs,specs,SID,NOW,lr.load())
                        self.assertEqual(s.state,'action',s)
                        self.assertEqual((s.state,s.action,s.trigger,s.since,s.attempt),
                            ('action',{'builder':'build','grader':'grade','reviewer':'review'}[role],release['_src'],release['ts'],1),s)
                        self.assertEqual(s,next(x for x in ns.next_steps(evs,specs,NOW,lr.load()) if x.item==SID))
                        failed=event('autodispatch-failed',-1,role=role,reason='fresh failure')
                        again=ns.step_for(evs+[failed],specs,SID,NOW,lr.load())
                        self.assertEqual((again.state,again.attempt),('hold',2));self.assertIn('retry-wait',again.reason)

    def test_retry1857_AC2_unauthorized_and_historical_holds(self):
        import tick
        for kind in ('retries-exhausted','spawn-died-twice','spawn-redispatch-lapsed','spawn-redispatch-failed'):
            for actor,minute,subject in [('builder',-20,SID),(None,-20,SID),('operator',-30,SID),('thinker',-40,SID),('operator',-20,'L-spec-9998')]:
                with self.subTest(kind=kind,actor=actor,minute=minute,subject=subject):
                    evs,hold=self.fixture('builder');hold.update(kind=kind,dead_spawn='L-builder-9902')
                    release=event('unblocked',minute,subject=subject)
                    release.pop('actor',None)
                    if actor:
                        release['actor']=actor;release['_src']=f'L-{actor}-release.jsonl:1'
                    else: release.pop('_src',None)
                    evs.append(release)
                    s=ns.step_for(evs,{SID:dict(state='written')},SID,NOW,lr.load())
                    self.assertEqual(s.state,'hold');self.assertIn(SID,tick.in_flight(evs))
                    self.assertIn(SID,tick.handled_items([s]))

    def test_retry1857_AC2_explicit_directives_keep_priority(self):
        for action, role in [('rebuild','builder'),('regrade','grader')]:
            for fields, expected in [(dict(close='superseded'),'close'),(dict(rework='yes'),'rebuild'),(dict(regrade='yes'),'regrade')]:
                with self.subTest(action=action,fields=fields):
                    evs=AttemptsRebuild1855Tests().fixture(action)
                    trigger=evs[-1]
                    for i,minute in enumerate((-90,-70,-50)):
                        evs.append(event('spawn-failed',minute,role=role,spawn=f'L-{role}-{9900+i}',reason='crash'))
                    hold=event('escalation-blocking',-30,actor='tick',role=role,ref=trigger['_src'])
                    hold.update(kind='retries-exhausted',_src='L-tick-retries.jsonl:1');evs.append(hold)
                    evs.append(event('decision',-20,**fields))
                    step=ns.step_for(evs,{SID:dict(state='building')},SID,NOW,lr.load())
                    self.assertEqual(step.action,expected,step)

    def test_retry1857_AC1_ruling_exhaustion_stays_durable(self):
        evs=AttemptsRebuild1855Tests().fixture('rebuild')
        evs[-1]['action']='rework'
        for i,minute in enumerate((-90,-70,-50)):
            evs.append(event('spawn-failed',minute,role='builder',spawn=f'L-builder-{9900+i}',reason='crash'))
        step=ns.step_for(evs,{SID:dict(state='building')},SID,NOW,lr.load())
        self.assertTrue(step.reason.startswith('retries-exhausted:'),step)
        self.assertEqual((step.state,step.attempt),('hold',3))

    def test_retry1857_AC1_pregrade_preserves_retry_hold_reason(self):
        for kind in ('precheck-red','merge-conflict'):
            for minutes,prefix,attempt in [((-4,),'retry-wait:',2),
                                          ((-90,-70,-50),'retries-exhausted:',3)]:
                with self.subTest(kind=kind,prefix=prefix):
                    evs=[event('spec-written',-240),
                         event('build-done',-230,status='DONE',ready_sha='built'),
                         event(kind,-220,layer='pregrade',branch_sha='built',
                               tests=['tests/a.py::test_a'],path='a.py')]
                    for i,minute in enumerate(minutes):
                        evs.append(event('spawn-failed',minute,role='builder',
                                         spawn=f'L-builder-{9900+i}',reason='crash'))
                    step=ns.step_for(evs,{SID:dict(state='building')},SID,NOW,lr.load())
                    self.assertTrue(step.reason.startswith(prefix),step)
                    self.assertIn('a.py',step.reason)
                    self.assertEqual((step.state,step.attempt),('hold',attempt))

    def test_retry1857_AC3_step_metadata_and_coverage(self):
        for role,action in [('builder','build'),('grader','grade'),('reviewer','review')]:
            helper=AttemptsRebuild1855Tests();evs=helper.fixture(action)
            first=helper.one(evs)
            self.assertEqual((first.attempt,first.retry_at),(1,None))
            evs.append(event('spawn-failed',-4,role=role,spawn=f'L-{role}-9991',reason='crash'))
            waiting=helper.one(evs)
            self.assertEqual(waiting.state,'hold');self.assertEqual(waiting.attempt,2)
            self.assertEqual(waiting.retry_at,(NOW+timedelta(minutes=1)).isoformat())
        self.assertGreater(unittest.defaultTestLoader.loadTestsFromTestCase(AttemptsRebuild1855Tests).countTestCases(),0)
        for module, pattern in [('test_attempts','*retry1672_AC9*'), ('test_worktree_heal','*AC1*'), ('test_lane_driver','*worktree*')]:
            script = ('import importlib,unittest,sys; unittest.defaultTestLoader.testNamePatterns=[sys.argv[2]]; '
                      's=unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(sys.argv[1])); '
                      'assert s.countTestCases()>0; print(sys.argv[1],s.countTestCases()); '
                      'sys.exit(not unittest.TextTestRunner().run(s).wasSuccessful())')
            result=subprocess.run([sys.executable,'-c',script,module,pattern],env=dict(os.environ,PYTHONPATH=str(ROOT/'src')),
                                  capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--spec-1869-ac2']:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(GateToolingTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print(f'L-spec-1869 AC2 nextstep: {result.testsRun} executed; success={result.wasSuccessful()}')
        sys.exit(0 if result.testsRun and result.wasSuccessful() else 1)
    unittest.main()
