#!/usr/bin/env python3
"""L-spec-2316: tick.launch re-reads the ledger and re-checks refusals before preparing.

Real appends and real fresh reads in a scratch root; only the process runner is
intercepted. No model or API call.
"""
import atexit
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

# Bind disposable paths before importing any production module.
_BOOT = tempfile.TemporaryDirectory(prefix='tick-start-check-import-')
atexit.register(_BOOT.cleanup)
os.environ.update(DOIT_ROOT=_BOOT.name, DOIT_NO_POKE='1',
                  V4_LEDGER_DIR=str(Path(_BOOT.name) / 'v4-ledger'),
                  V4_INBOX_DIR=str(Path(_BOOT.name) / 'v4-inbox'),
                  V4_STAGING_DIR=str(Path(_BOOT.name) / 'v4-staging'))
os.environ.pop('DOIT_PROJECT', None)
os.environ.pop('DOIT_LEDGER_FILE', None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch
import fold
import ledger_cache
import nextstep
import spawns
import tick

SID = 'L-spec-8801'
ACTIONS = (('build', 'builder'), ('rebuild', 'builder'), ('grade', 'grader'),
           ('regrade', 'grader'), ('review', 'reviewer'))
START_KINDS = ('spawn-started', 'autodispatched', 'autodispatch-failed', 'spawn-failed')


class StartCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='tick-start-check-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('events', 'content', 'repos/proj', 'worktrees/proj/' + SID.lower(), 'state'):
            (self.root / name).mkdir(parents=True)
        for module, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root / 'events'),
                                    (dispatch, 'CONTENT', self.root / 'content')):
            self.patch(module, name, value)
        self.now = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
        self.patch(fold, 'NOW', self.now)
        self.patch(dispatch, 'now', lambda: self.now.isoformat(timespec='seconds'))
        env = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        env.start()
        self.addCleanup(env.stop)
        import look
        repo = self.root / 'repos/proj'
        subprocess.run(['git', '-C', str(repo), 'init', '-q', '-b', 'master'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-q',
                        '--allow-empty', '-m', 'fixture'], check=True)
        self.base = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                            text=True).strip()
        config = look.load_toml()
        config = dict(config, push={**config.get('push', {}),
                      'main': {**config.get('push', {}).get('main', {}), 'proj': 'master'}})
        self.patch(look, 'load_toml', return_value=config)
        (self.root / 'content' / (SID + '.md')).write_text('AC1 [backend]: fixture.\n')
        self.since = (self.now - dt.timedelta(hours=1)).isoformat()
        self.append('spec-written', -90, actor='operator', project='proj', charter='L-charter-0052')
        self.calls = []
        self.detached = lambda argv: json.dumps({'detached': 'L-%s-7001' % argv[3]})
        self.on_dispatch = None

    def patch(self, module, name, value=None, **kw):
        p = patch.object(module, name, **kw) if kw else patch.object(module, name, value)
        p.start()
        self.addCleanup(p.stop)

    def append(self, kind, minutes=0, actor='tick', sid=SID, **fields):
        when = (self.now + dt.timedelta(minutes=minutes)).isoformat(timespec='seconds')
        name = 'L-%s-local.jsonl' % actor
        with patch.object(dispatch, 'now', return_value=when):
            error = dispatch.emit(self.root / 'events' / name, {'subject': sid}, kind, **fields)
        self.assertIsNone(error)

    def runner(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ['doit', 'packet']:
            return dict(code=0, stdout='/packet.md', stderr='')
        if argv[:2] == ['doit', 'dispatch']:
            if self.on_dispatch:
                return self.on_dispatch(argv)
            return dict(code=0, stdout=self.detached(argv), stderr='')
        return dict(code=0, stdout='0' if 'rev-list' in argv else 'master', stderr='')

    def step(self, action):
        return replace(nextstep.Step(SID, 'spec', 'checking', 'action', action, 'tick', 'fixture:1',
                                     'condition met', None, self.since), action=action)

    def stale_dispatcher(self):
        events = fold.read_events(use_cache=False)
        return tick._PassDispatcher(self.runner, events, {}, self.now, False)

    def rows(self, *kinds):
        return [e for e in fold.read_events(use_cache=False)
                if e.get('subject') == SID and (not kinds or e.get('type') in kinds)]

    def assertNothingPrepared(self):
        for argv in self.calls:
            self.assertNotEqual(argv[:2], ['git', 'worktree'])
            self.assertNotIn(argv[:2], (['doit', 'packet'], ['doit', 'readiness'], ['doit', 'dispatch']))
            self.assertFalse('worktree' in argv, argv)

    # AC1 ---------------------------------------------------------------
    def test_ac1_later_same_role_start_blocks_preparation_in_three_roles(self):
        for action, role in ACTIONS:
            with self.subTest(action=action):
                self.calls.clear()
                sid = SID
                dispatcher = self.stale_dispatcher()
                spawn = 'L-%s-%d' % (role, 6000 + len(self.rows()))
                self.append('spawn-started', -5, actor=role, role=role, spawn=spawn, worktree='w')
                before = self.rows(*START_KINDS)
                self.assertIsNone(tick.launch(self.step(action), dispatcher=dispatcher))
                self.assertEqual(self.calls, [])
                self.assertNothingPrepared()
                self.assertEqual(self.rows(*START_KINDS), before)
                out = tick.run_steps(dispatcher.events, {}, self.now, dispatcher=self.runner,
                                     steps=[self.step(action)])
                self.assertIn('hold %s: not-ready:duplicate-dispatch' % sid, out)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.rows(*START_KINDS), before)
                self.append('spawn-done', -4, actor=role, role=role, spawn=spawn)

    def test_ac1_failed_fresh_read_never_uses_the_stale_snapshot(self):
        dispatcher = self.stale_dispatcher()
        err = io.StringIO()
        with patch.object(fold, 'read_events', side_effect=ledger_cache.LockTimeout('ledger busy')), \
             contextlib.redirect_stderr(err):
            self.assertIsNone(tick.launch(self.step('rebuild'), dispatcher=dispatcher))
        self.assertEqual(self.calls, [])
        self.assertIn('fresh ledger read failed', err.getvalue())
        self.assertEqual(self.rows(*START_KINDS), [])

    # AC2 ---------------------------------------------------------------
    def test_ac2_held_flock_blocks_with_dispatch_in_progress(self):
        dispatcher = self.stale_dispatcher()
        lock = open(self.root / 'state' / spawns.lock_name(SID), 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertIsNone(tick.launch(self.step('rebuild'), dispatcher=dispatcher))
        self.assertEqual(tick.HOLD_REASONS.get(SID), 'not-ready:dispatch-in-progress')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(*START_KINDS), [])

    def test_ac2_unpaired_marker_inside_window_blocks(self):
        self.append('autodispatched', -2, role='builder', rule='rebuild', since=self.since, spawn='45678')
        dispatcher = self.stale_dispatcher()
        self.assertIsNone(tick.launch(self.step('rebuild'), dispatcher=dispatcher))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.rows('autodispatched')), 1)

    def test_ac2_two_more_passes_after_one_launch_start_nothing(self):
        for paired in (False, True):
            with self.subTest(start_row_written=paired):
                sid = 'L-spec-88%02d' % (10 + paired)
                wt = self.root / 'worktrees/proj' / sid.lower()
                wt.mkdir()
                (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
                self.append('spec-written', -90, actor='operator', sid=sid, project='proj')
                step = replace(self.step('rebuild'), item=sid)

                def on_dispatch(argv, sid=sid, paired=paired):
                    if paired:
                        self.append('spawn-started', 0, actor='builder', sid=sid, role='builder',
                                    spawn='L-builder-7001', worktree=str(wt))
                    return dict(code=0, stdout=json.dumps({'detached': 7001}), stderr='')
                self.on_dispatch = on_dispatch
                self.calls.clear()
                events = fold.read_events(use_cache=False)
                first = tick.launch(step, dispatcher=tick._PassDispatcher(self.runner, events, {}, self.now, False))
                self.assertEqual(first, '7001')
                started = len([c for c in self.calls if c[:2] == ['doit', 'dispatch']])
                for _ in range(2):
                    # Lock released (never held here); only the ledger prevents a second start.
                    self.assertIsNone(tick.launch(step, dispatcher=tick._PassDispatcher(
                        self.runner, events, {}, self.now, False)))
                def own(*k, sid=sid):
                    return [e for e in fold.read_events(use_cache=False)
                            if e.get('subject') == sid and e.get('type') in k]
                self.assertEqual(len([c for c in self.calls if c[:2] == ['doit', 'dispatch']]), started)
                self.assertEqual(len(own('autodispatched')), 1)
                self.assertEqual(len(own('spawn-started')), 1 if paired else 0)

    # AC3 ---------------------------------------------------------------
    def test_ac3_marker_needs_a_positive_pid(self):
        cases = (('zero', lambda a: json.dumps({'detached': 0})),
                 ('false', lambda a: json.dumps({'detached': False})),
                 ('true', lambda a: json.dumps({'detached': True})),
                 ('missing', lambda a: json.dumps({})),
                 ('empty', lambda a: json.dumps({'detached': ''})))
        for name, body in cases:
            with self.subTest(pid=name):
                self.detached = body
                self.calls.clear()
                before = len(self.rows('autodispatch-failed'))
                with self.assertRaises(ValueError):
                    tick.launch(self.step('rebuild'), dispatcher=self.stale_dispatcher())
                self.assertEqual(self.rows('autodispatched'), [])
                self.assertEqual(len(self.rows('autodispatch-failed')), before + 1)
                # The ordinary failure is a standing hold; clear it for the next case.
                self.append('verdict', 1, actor='operator', confirmed=True)
                self.since = (self.now + dt.timedelta(minutes=1)).isoformat()
        self.detached = lambda argv: json.dumps({'detached': 7002})
        self.since = (self.now + dt.timedelta(minutes=2)).isoformat()
        self.assertEqual(tick.launch(self.step('rebuild'), dispatcher=self.stale_dispatcher()), '7002')
        self.assertEqual(len(self.rows('autodispatched')), 1)

    def test_ac3_command_failure_writes_no_marker(self):
        self.on_dispatch = lambda argv: dict(code=1, stdout='', stderr='boom')
        with self.assertRaises(ValueError):
            tick.launch(self.step('rebuild'), dispatcher=self.stale_dispatcher())
        self.assertEqual(self.rows('autodispatched'), [])
        self.assertEqual(len(self.rows('autodispatch-failed')), 1)

    def test_ac3_dispatch_refusal_after_a_passing_check_keeps_the_real_spawn_failed(self):
        written = {}

        def refuse(argv):
            self.append('spawn-failed', 0, actor='grader', role='grader', spawn='L-grader-7003',
                        reason='bundle-pending', why='evidence bundle pending')
            written.update(self.rows('spawn-failed')[-1])
            return dict(code=1, stdout='', stderr='bundle-pending')
        self.on_dispatch = refuse
        with patch('grader_packet.evidence_state', return_value=type('S', (), {'state': 'ready'})()):
            with self.assertRaises(ValueError):
                tick.launch(self.step('grade'), dispatcher=self.stale_dispatcher())
        after = self.rows('spawn-failed')
        self.assertEqual(after, [written])
        self.assertEqual(self.rows('autodispatched'), [])
        self.assertEqual([e['type'] for e in self.rows('autodispatch-skipped')], [])


if __name__ == '__main__':
    unittest.main()
