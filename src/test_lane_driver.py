#!/usr/bin/env python3
"""Driver regressions: real rule predicates and append-only ledger round trips."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autodispatch
import dispatch
import fold
import lane_driver
import lane_rules
import tick


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lane-driver-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('events', 'content', 'repos/proj', 'worktrees/proj/l-spec-1001'):
            (self.root / name).mkdir(parents=True)
        for module, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'CONTENT', self.root / 'content')):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.now = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)
        p = patch.object(dispatch, 'now', lambda: self.now.isoformat(timespec='seconds'))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(fold, 'NOW', self.now)
        p.start()
        self.addCleanup(p.stop)
        self.sid = 'L-spec-1001'
        self.calls = []
        self.events = [self.event('spec-written', 40, project='proj', charter='L-charter-0052'),
                       self.event('verdict', 1, confirmed=True)]
        self.specs = {self.sid: {'id': self.sid, 'state': 'graded', 'evs': self.events}}
        self.worktree = self.root / 'worktrees/proj' / self.sid.lower()
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [backend]: fixture.\n')

    def event(self, kind, minutes=0, **fields):
        return dict(v=1, ts=(self.now - dt.timedelta(minutes=minutes)).isoformat(),
                    type=kind, subject=self.sid, **fields)

    def runner(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'packet']:
            return dict(code=0, stdout='/packet.md', stderr='')
        if argv[:2] == ['doit', 'dispatch']:
            return dict(code=0, stdout=json.dumps({'detached': 'L-' + argv[3] + '-9999'}), stderr='')
        return dict(code=0, stdout='0' if 'rev-list' in argv else 'master', stderr='')

    def run_driver(self, **kw):
        return lane_driver.run(self.events, self.specs, self.now, runner=self.runner, **kw)

    def appended(self):
        path = tick.tick_path()
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_review_exact_argv_and_repeat(self):
        out = self.run_driver()
        self.assertEqual(self.calls, [
            ['doit', 'packet', 'reviewer', self.sid, '--worktree', str(self.worktree), '--depth', 'gates-only', '--round', '1'],
            ['doit', 'dispatch', '--detach', 'reviewer', self.sid, '--packet', '/packet.md',
             '--cwd', str(self.worktree), '--charter', 'L-charter-0052', '--project', 'proj']])
        self.assertEqual(len(out), 1)
        written = self.appended()
        self.assertEqual(len(written), 1)
        self.assertEqual({k: written[0][k] for k in ('type', 'rule', 'role', 'spawn', 'since')},
                         dict(type='autodispatched', rule='review', role='reviewer',
                              spawn='L-reviewer-9999', since=self.events[-1]['ts']))
        self.assertNotIn('actor', written[0])
        self.events.extend(fold.read_events())
        self.calls.clear()
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(self.calls, [])

    def test_failed_review_stays_suppressed_when_overdue(self):
        self.worktree.rmdir()
        self.assertIn('failed', self.run_driver()[0])
        self.assertEqual([e['type'] for e in self.appended()], ['autodispatch-failed'])
        self.events.extend(fold.read_events())
        self.now += dt.timedelta(hours=2)
        self.assertEqual(self.run_driver(), [])


    def test_overdue_first_review_is_action_not_retry(self):
        self.events[-1] = self.event('verdict', 60, confirmed=True)
        self.assertEqual(len(self.run_driver()), 1)
        self.assertNotIn('trigger', self.appended()[0])

    def test_review_suppressors_and_handled(self):
        for kind in ('autodispatched', 'spawn-started', 'spawn-failed', 'autodispatch-failed'):
            with self.subTest(kind=kind):
                e = self.event(kind, 0, role='reviewer', spawn='L-reviewer-9999', reason='refused')
                self.assertEqual(lane_driver.run(self.events + [e], self.specs, self.now, runner=self.runner), [])
        self.assertEqual(self.run_driver(handled={self.sid}), [])
        self.assertEqual(self.calls, [])

    def test_review_ineligible_conditions(self):
        for kind, fields in (
                ('review', dict(confirmed=True, n_blocking=0)),
                ('verdict', dict(confirmed=False, could_not_run=['AC1'])),
                ('rejected-criterion', dict(criterion='AC1', why='standing')),
                ('spec-killed', dict(why='closed')), ('spec-closed', {}),
                ('shipped', {}), ('dependency-hold', dict(waits_for='L-spec-9998')),
                ('capability-hold', dict(spec=self.sid, capability='db'))):
            with self.subTest(kind=kind):
                event = self.event(kind, **fields)
                if kind == 'capability-hold':
                    event['subject'] = 'capability:db:' + self.sid
                self.assertEqual(lane_driver.run(self.events + [event], self.specs,
                                                 self.now, runner=self.runner), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_each_success_and_failure_survives_real_fold_validation(self):
        self.run_driver()
        for event in self.appended():
            self.assertIsNone(fold.required_reason(event))
            self.assertNotIn('actor', event)
        read = fold.read_events(use_cache=False)
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0]['actor'], 'tick')
        self.assertNotIn('trigger', read[0])

    def test_unanswered_block_and_older_capability_unblock(self):
        self.events = [self.events[0], self.event('build-blocked', 20)]
        self.assertEqual(self.run_driver(), [])
        cap = 'capability:db:' + self.sid
        self.events = [self.events[0], dict(self.event('capability-hold', 5), subject=cap),
                       dict(self.event('unblocked', 10, actor='operator'), subject=cap)]
        self.assertEqual(self.run_driver(), [])
        self.events[-1] = dict(self.event('unblocked', 1, actor='builder'), subject=cap)
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_rebuild_answer_and_wrong_actor(self):
        self.events = [self.events[0], self.event('build-blocked', 20),
                       self.event('decision', 1, actor='builder', why='not binding')]
        self.assertEqual(self.run_driver(), [])
        self.events[-1] = self.event('decision', 1, actor='thinker', why='Use the smaller scope')
        self.run_driver()
        self.assertEqual(self.calls[0][:4], ['doit', 'packet', 'builder', self.sid])
        self.assertEqual(self.appended()[0]['rule'], 'rebuild')

    def test_regrade_capability_forms_and_subject(self):
        for kind in ('capability-hold', 'escalation-blocking'):
            with self.subTest(kind=kind):
                cap = 'capability:db:' + self.sid
                self.events = [self.events[0], dict(self.event(kind, 20), kind='capability-hold')]
                self.events[-1]['subject'] = cap
                self.events += [dict(self.event('unblocked', 1, actor='operator'), subject='capability:other:' + self.sid)]
                self.assertEqual(self.run_driver(), [])
                self.events[-1]['subject'] = cap
                self.run_driver()
                self.assertEqual(self.calls[-1][-2:], ['--timeout', '30'])
                self.assertEqual(self.appended()[-1]['rule'], 'regrade')
                self.calls.clear()

    def test_close_unshipped_real_readback(self):
        self.events += [self.event('decision', 0, actor='thinker', close='superseded', _src='ruling:1')]
        self.run_driver()
        e = fold.read_events()[0]
        self.assertEqual((e['type'], e['why']), ('spec-killed', 'ruling ruling:1: superseded'))
        self.assertEqual(e['actor'], 'tick')
        self.assertNotIn('actor', self.appended()[0])
        self.assertEqual(self.calls, [])

    def test_shipped_close_stays_suppressed_after_answer(self):
        self.events += [self.event('shipped', 10),
                        self.event('decision', 1, actor='operator', close='archive', _src='ruling:2')]
        self.run_driver()
        e = fold.read_events()[0]
        self.assertEqual((e['kind'], e['owner']), ('close-needs-operator', 'operator'))
        self.assertEqual(e['default'], 'leave the spec open')
        self.assertEqual(dt.datetime.fromisoformat(e['deadline']), self.now + dt.timedelta(hours=72))
        self.events += [e, self.event('decision', 0, actor='operator', ref=e['_src'])]
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(len(self.appended()), 1)

    def test_invalid_close_actors_prose_and_terminal(self):
        for ruling in (self.event('decision', 0, actor='builder', close='invalid'),
                       self.event('decision', 0, actor='thinker', why='close this')):
            self.assertEqual(lane_driver.run([self.events[0], ruling], self.specs, self.now, runner=self.runner), [])
        self.events += [self.event('spec-killed', 2), self.event('decision', 1, actor='operator', close='again')]
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(self.appended(), [])

    def test_unreadable_table_falls_back_without_erasing_failure(self):
        with patch.dict(os.environ, {'DOIT_LANE_RULES': str(self.root / 'missing.toml')}):
            lines = self.run_driver()
            self.assertTrue(lines[0].startswith('lane-rules unreadable'))
            self.assertEqual(self.appended()[0]['rule'], 'review')
            self.events += [self.event('autodispatch-failed', 0, role='reviewer', reason='refused')]
            self.assertEqual(len(self.run_driver()), 1)  # warning only

    def test_malformed_table_and_fold_do_not_dispatch(self):
        table = self.root / 'bad-rules.toml'
        table.write_text('rules = [')
        with patch.dict(os.environ, {'DOIT_LANE_RULES': str(table)}):
            self.events[0]['ts'] = 'bad-time'
            lines = self.run_driver()
            self.assertTrue(lines[0].startswith('lane-rules unreadable:'))
            self.assertTrue(all(line.startswith('undetermined ') for line in lines[1:]))
            self.assertEqual(autodispatch._lane_limit('build'), 30)
            self.assertEqual(autodispatch._lane_limit('grade'), 15)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_failed_action_immediate_and_overdue_then_new_trigger(self):
        def failing(argv):
            self.calls.append(argv)
            return dict(code=1, stderr='packet refused')
        lane_driver.run(self.events, self.specs, self.now, runner=failing)
        self.events += fold.read_events(use_cache=False)
        for minutes in (0, 120):
            self.assertEqual(lane_driver.run(self.events, self.specs,
                                            self.now + dt.timedelta(minutes=minutes),
                                            runner=failing), [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.appended()), 1)
        self.now += dt.timedelta(minutes=121)
        self.events.append(self.event('verdict', 1, confirmed=True))
        self.run_driver()
        self.assertEqual(len(self.appended()), 2)

    def test_refused_failure_record_is_reported(self):
        with patch.object(dispatch, 'emit', return_value='refused') as emit:
            lines = self.run_driver()
        self.assertEqual(emit.call_count, 2)
        self.assertIn('failure record refused: refused', lines[0])
        self.assertEqual(self.appended(), [])

    def test_malformed_fold_is_undetermined_including_shipped_close(self):
        self.events += [self.event('shipped', 5), self.event('decision', 1, actor='operator', close='archive')]
        self.events[0]['ts'] = 'bad-time'
        lines = self.run_driver()
        self.assertTrue(all(line.startswith('undetermined ') for line in lines))
        self.assertTrue(lines)
        self.assertEqual(self.appended(), [])
        self.assertEqual(self.calls, [])

    def test_malformed_busy_spawn_is_undetermined(self):
        self.events.append(dict(self.event('spawn-started', role='reviewer',
                                           spawn='L-reviewer-8888'), ts='bad-time'))
        lines = self.run_driver()
        self.assertTrue(lines and all(line.startswith('undetermined ') for line in lines))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_legacy_builder_records_suppress_answered_rebuild(self):
        self.events = [self.events[0], self.event('build-blocked', 20),
                       self.event('decision', 10, actor='operator', why='answer')]
        for kind in ('spawn-started', 'spawn-failed', 'autodispatched',
                     'autodispatch-failed', 'build-started'):
            with self.subTest(kind=kind):
                events = self.events + [self.event(kind, 1, spawn='L-builder-8888')]
                self.assertEqual(lane_driver.run(events, self.specs, self.now,
                                                 runner=self.runner), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_cannot_assess_and_could_not_run(self):
        self.events[-1] = self.event('verdict', 1, confirmed=False, cannot_assess=['AC1'], could_not_run=['AC2'])
        self.assertEqual(self.run_driver(), [])
        self.events[-1].pop('could_not_run')
        self.run_driver()
        self.assertEqual(self.calls[0][-4:], ['--depth', 'gates-only', '--round', '1'])

    def test_round_two_and_round_three(self):
        self.events.insert(1, self.event('review', 20, confirmed=False, round=1))
        self.run_driver()
        self.assertEqual(self.calls[0][-2:], ['--round', '2'])
        self.events.insert(2, self.event('review', 10, confirmed=False, round=2))
        self.calls.clear()
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.appended()), 1)

    def test_ui_without_browser_escalates(self):
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [ui]: fixture.\n')
        self.run_driver()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended()[0]['kind'], 'review-needs-browser')

    def test_reviewer_capacity_defaults_and_in_flight(self):
        self.assertEqual(lane_driver.capacity(), 4)
        config = self.root / 'models.toml'
        for body in ('[seats]\nbuilder = 2', 'bad = ['):
            config.write_text(body)
            self.assertEqual(lane_driver.capacity(), 4)
        config.write_text('[seats]\nreviewer = 1')
        other = self.event('spawn-started', 1, role='reviewer', spawn='L-reviewer-8888')
        other['subject'] = 'L-spec-1002'
        lines = lane_driver.run(self.events + [other], self.specs, self.now, runner=self.runner)
        self.assertEqual(lines, ['seat-wait ' + self.sid + ' review'])
        self.assertEqual(self.calls, [])

    def test_seat_capacity_limits_missed_reviewers(self):
        self.events[-1] = self.event('verdict', 30, confirmed=True)
        for i in range(1002, 1022):
            sid = f'L-spec-{i}'
            self.specs[sid] = dict(id=sid, state='graded')
            self.events += [dict(e, subject=sid) for e in self.events[:2]]
            (self.root / 'worktrees/proj' / sid.lower()).mkdir()
            (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        lines = self.run_driver()
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(sum(line.startswith('seat-wait ') for line in lines), 17)
        self.assertEqual(len(self.appended()), 4)

    def test_emit_refusal_reports_failure_and_no_success(self):
        real_emit = dispatch.emit
        def refuse(dst, base, kind, **fields):
            if kind == 'autodispatched':
                return 'fixture refusal'
            return real_emit(dst, base, kind, **fields)
        with patch.object(dispatch, 'emit', side_effect=refuse):
            lines = self.run_driver()
        self.assertIn('record refused', lines[0])
        self.assertEqual([e['type'] for e in self.appended()], ['autodispatch-failed'])

    def test_dry_run_no_runner_or_write(self):
        lines = self.run_driver(dry_run=True)
        self.assertTrue(all(line.startswith('would ') for line in lines))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_non_driver_rows_are_ignored(self):
        rows = [dict(spec=self.sid, rule=rule, since=self.events[-1]['ts'], reason='condition met')
                for rule in ('build', 'grade', 'wave-held', 'dependency-held', 'merge', 'idle', 'gate', 'enqueue')]
        self.assertEqual(lane_driver._act(self.events, self.specs, self.now, rows, self.runner, False, set()), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_integration_preserves_standing_grader_failure(self):
        self.events = [self.events[0], self.event('build-done', 60, actor='builder', status='DONE'),
                       self.event('autodispatch-failed', 1, role='grader', reason='boom-packet')]
        self.specs[self.sid]['state'] = 'built'
        self.specs[self.sid]['evs'] = self.events
        self.assertEqual(autodispatch.run(self.events, self.specs, self.now, runner=self.runner), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_rebuild_rewrite_missing_worktree_and_failure(self):
        self.events = [self.events[0], self.event('build-blocked', 20),
                       self.event('spec-written', 1, project='proj')]
        self.worktree.rmdir()
        self.run_driver()
        e = self.appended()[0]
        self.assertEqual((e['type'], e['rule'], e['role']), ('autodispatch-failed', 'rebuild', 'builder'))
        self.events += fold.read_events()
        self.assertEqual(self.run_driver(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.appended()), 1)

    def test_ui_with_browser_is_full(self):
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [ui]: fixture.\n')
        (self.root / 'review-mcp.json').write_text('{}')
        self.run_driver()
        self.assertEqual(self.calls[0][-4:], ['--depth', 'full', '--round', '1'])

    def test_rules_limits_change_but_grader_timeout_does_not(self):
        table = self.root / 'limits.toml'
        table.write_text(lane_rules.DEFAULT_TABLE.read_text().replace('limit_min = 30', 'limit_min = 45'))
        self.events = [self.events[0], self.event('build-done', 1, actor='builder', status='DONE')]
        self.specs[self.sid]['state'] = 'built'
        with patch.dict(os.environ, {'DOIT_LANE_RULES': str(table)}):
            self.assertEqual(autodispatch._lane_limit('build'), 45)
            self.assertEqual(autodispatch.grader_candidates(self.events, self.specs, self.now)[0]['limit_min'], 15)
            autodispatch.run(self.events, self.specs, self.now, runner=self.runner)
        self.assertEqual(self.calls[-1][-2:], ['--timeout', '30'])

    def test_integration_driver_called_once_after_replay_with_handled(self):
        self.events = [self.events[0], self.event('build-done', 60, actor='builder', status='DONE')]
        self.specs[self.sid]['state'] = 'built'
        order = []
        def no_replays(events, now):
            order.append('replay')
            return []
        def driver(events, specs, now, **kw):
            order.append('driver')
            self.assertEqual(kw['handled'], {self.sid})
            self.assertIs(kw['runner'], fake)
            self.assertTrue(kw['dry_run'])
            return ['would driver fixture']
        fake = self.runner
        with patch.object(autodispatch, 'dead_spawns', side_effect=no_replays), patch.object(lane_driver, 'run', side_effect=driver) as called:
            lines = autodispatch.run(self.events, self.specs, self.now, runner=fake, dry_run=True)
        self.assertEqual(order, ['replay', 'driver'])
        called.assert_called_once()
        self.assertEqual(lines[-1], 'would driver fixture')
        self.assertEqual(self.calls, [])

    def test_real_runner_inherits_tick_ledger(self):
        from types import SimpleNamespace
        with patch.object(autodispatch.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as child:
            autodispatch._real_runner(['doit', 'dispatch', '--detach', 'reviewer', self.sid])
            self.assertEqual(child.call_args.kwargs['env']['DOIT_LEDGER_FILE'], str(tick.tick_path()))

    def test_carried_tier_from_copied_real_ledger_row(self):
        # Copied verbatim from /home/albert/.do-it/events/L-executor-0001.jsonl:499.
        carried = {'audited_at': '2026-09-22T18:45:38Z', 'footprint': ['api/app/lib/profit_v2/services/provenance_trail.py', 'api/tests/profit_v2/test_provenance_trail.py', 'docs/do-it/evidence/settlement-leaf-report-name-fix/'], 'project': 'albert-scott', 'source': '1476', 'subject': 'L-spec-0217', 'tier': 'full', 'ts': '2026-09-23T13:03:51+00:00', 'type': 'spec-carried', 'v': 1}
        sid = carried['subject']
        self.events = [dict(e, subject=sid) for e in self.events] + [carried]
        self.specs = {sid: dict(id=sid, state='graded')}
        (self.root / 'worktrees/proj' / sid.lower()).mkdir()
        (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        self.run_driver()
        self.assertEqual(self.calls[0][-4:], ['--depth', 'full', '--round', '1'])


if __name__ == '__main__':
    unittest.main()
