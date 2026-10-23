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
import nextstep
from dataclasses import replace


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
                    type=kind, subject=self.sid, _src='L-fixture.jsonl:' + str(minutes), **fields)


    def runner(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'packet']:
            return dict(code=0, stdout='/packet.md', stderr='')
        if argv[:2] == ['doit', 'dispatch']:
            return dict(code=0, stdout=json.dumps({'detached': 'L-' + argv[3] + '-9999'}), stderr='')
        return dict(code=0, stdout='0' if 'rev-list' in argv else 'master', stderr='')


    def appended(self):
        path = tick.tick_path()
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


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
        self.assertEqual(lines, ['hold L-reviewer-8888: in flight',
                                 'seat-wait ' + self.sid + ' review'])
        self.assertEqual(self.calls, [])


    def test_real_runner_inherits_tick_ledger(self):
        from types import SimpleNamespace
        with patch.object(autodispatch.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as child:
            autodispatch._real_runner(['doit', 'dispatch', '--detach', 'reviewer', self.sid])
            self.assertEqual(child.call_args.kwargs['env']['DOIT_LEDGER_FILE'], str(tick.tick_path()))


    def step(self, action='review', **kw):
        return replace(nextstep.Step(self.sid, 'spec', 'checking', 'action', action or 'review',
                                     'tick', 'fixture:1', 'condition met', None,
                                     self.events[-1]['ts']), action=action, **kw)

    def run_steps(self, steps, **kw):
        return tick.run_steps(self.events, self.specs, self.now,
                              dispatcher=self.runner, steps=steps, **kw)

    def test_compatibility_entries_delegate_to_tick(self):
        for entry in (lane_driver.run, autodispatch.run):
            with self.subTest(entry=entry.__module__), \
                 patch.object(tick, 'run_steps', return_value=['tick outcome']) as run, \
                 patch.object(lane_driver, '_rows', side_effect=AssertionError('legacy eligibility')), \
                 patch.object(autodispatch, 'candidates', side_effect=AssertionError('legacy candidates')):
                self.assertEqual(entry(self.events, self.specs, self.now, runner=self.runner,
                                       dry_run=True), ['tick outcome'])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args, (self.events, self.specs, self.now))
                self.assertEqual(run.call_args.kwargs['dispatcher'], self.runner)
                self.assertTrue(run.call_args.kwargs['dry_run'])

    def test_tick_review_exact_argv_and_real_readback(self):
        self.run_steps([self.step()])
        self.assertEqual(self.calls, [
            ['doit', 'packet', 'reviewer', self.sid, '--worktree', str(self.worktree),
             '--depth', 'gates-only', '--round', '1'],
            ['doit', 'dispatch', '--detach', 'reviewer', self.sid, '--packet', '/packet.md',
             '--cwd', str(self.worktree), '--charter', 'L-charter-0052', '--project', 'proj']])
        rows = fold.read_events(use_cache=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['type'], rows[0]['rule'], rows[0]['actor']),
                         ('autodispatched', 'review', 'tick'))
        self.assertNotIn('actor', self.appended()[0])

    def test_tick_rebuild_and_regrade_transport(self):
        for action, role in (('rebuild', 'builder'), ('grade', 'grader'), ('regrade', 'grader')):
            with self.subTest(action=action):
                self.calls.clear()
                tick.tick_path().unlink(missing_ok=True)
                self.run_steps([self.step(action)])
                self.assertEqual(self.calls[0][:4], ['doit', 'packet', role, self.sid])
                self.assertEqual(self.calls[1][:5], ['doit', 'dispatch', '--detach', role, self.sid])
                if role == 'grader':
                    self.assertEqual(self.calls[1][-2:], ['--timeout', '30'])
                else:
                    self.assertEqual(self.calls[0][-2:], ['--repo', str(self.root / 'repos/proj')])
                self.assertEqual(self.appended()[0]['rule'], action)

    def test_tick_build_creates_then_records_worktree(self):
        self.worktree.rmdir()
        def runner(argv):
            if 'worktree' in argv and 'add' in argv:
                self.worktree.mkdir()
            return self.runner(argv)
        tick.run_steps(self.events, self.specs, self.now, dispatcher=runner, steps=[self.step('build')])
        self.assertEqual([a[0] for a in self.calls], ['git', 'git', 'git', 'doit', 'doit'])
        self.assertEqual(self.calls[2][-5:], ['add', str(self.worktree), '-b', self.sid.lower(), 'master'])
        rows = fold.read_events(use_cache=False)
        self.assertCountEqual([e['type'] for e in rows], ['resource-created', 'autodispatched'])
        self.assertEqual(rows[0]['subject'], self.sid)

    def test_tick_build_reuses_only_clean_worktree(self):
        self.run_steps([self.step('build')])
        self.assertTrue(any('merge' in a and '--ff-only' in a for a in self.calls))
        self.assertFalse(any(e['type'] == 'resource-created' for e in self.appended()))
        self.calls.clear()
        tick.tick_path().unlink()
        def runner(argv):
            if 'rev-list' in argv:
                self.calls.append(argv)
                return dict(code=0, stdout='1', stderr='')
            return self.runner(argv)
        out = tick.run_steps(self.events, self.specs, self.now, dispatcher=runner,
                             steps=[self.step('build')])
        self.assertIn('not reusing', out[0])
        self.assertFalse(any(a[:2] == ['doit', 'dispatch'] for a in self.calls))
        self.assertEqual(self.appended()[0]['type'], 'autodispatch-failed')

    def test_tick_respects_step_holds_owners_and_handled(self):
        for step in (self.step(state='hold', action=None, clears='decision'),
                     self.step(reason='undetermined: fixture'), self.step(owner='executor'),
                     self.step('close', owner='operator'), self.step('gate'),
                     self.step('enqueue'), self.step('merge')):
            with self.subTest(step=step):
                self.run_steps([step])
        self.run_steps([self.step()], handled={self.sid})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])

    def test_tick_capacity_and_dry_run(self):
        other = replace(self.step(), item='L-spec-1002')
        with patch.object(lane_driver, 'capacity', return_value=1):
            out = self.run_steps([self.step(), other], dry_run=True)
        self.assertEqual(out, [f'would act {self.sid} review', 'seat-wait L-spec-1002 review'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.appended(), [])
        with patch.object(autodispatch, 'capacity', return_value=0):
            self.assertEqual(self.run_steps([self.step('rebuild')]), [f'seat-wait {self.sid} rebuild'])

    def test_tick_failed_transport_stands_until_new_decision(self):
        for result in (dict(code=1, stdout='', stderr='packet refused'),
                       dict(code=0, stdout='invalid-json', stderr='')):
            tick.tick_path().unlink(missing_ok=True)
            with patch.object(self, 'runner', return_value=result):
                out = self.run_steps([self.step()])
            self.assertTrue(out[0].startswith('failed '))
            rows = fold.read_events(use_cache=False)
            self.assertEqual([e['type'] for e in rows], ['autodispatch-failed'])
            original = self.events[:]
            self.events.extend(rows)
            self.calls.clear()
            self.run_steps([self.step()])
            self.assertEqual(self.calls, [])
            self.events.append(self.event('decision', -1, actor='thinker'))
            self.run_steps([self.step()])
            self.assertEqual(self.calls[-1][:2], ['doit', 'dispatch'])
            self.events = original

    def test_tick_missing_worktree_and_refused_record(self):
        self.worktree.rmdir()
        self.assertIn('missing', self.run_steps([self.step()])[0])
        self.assertEqual(self.calls, [])
        self.worktree.mkdir()
        with patch.object(dispatch, 'emit', return_value='fixture refusal'):
            self.assertIn('record refused', self.run_steps([self.step()])[0])

    def test_tick_review_options_preserved(self):
        for text, extra, depth, round_n in (
                ('AC1 [backend]: fixture.', [], 'gates-only', '1'),
                ('AC1 [ui]: fixture.', [], 'full', '1'),
                ('AC1 [backend]: fixture.', [self.event('review', 0, confirmed=False, round=1)], 'gates-only', '2'),
                ('AC1 [backend]: fixture.', [self.event('spec-carried', 0, tier='full')], 'full', '1')):
            with self.subTest(depth=depth, round=round_n):
                (self.root / 'content' / (self.sid + '.md')).write_text(text)
                (self.root / 'review-mcp.json').write_text('{}')
                original = self.events[:]
                self.events.extend(extra)
                self.calls.clear()
                self.run_steps([self.step()])
                self.assertEqual(self.calls[0][-4:], ['--depth', depth, '--round', round_n])
                self.events = original


if __name__ == '__main__':
    unittest.main()
