#!/usr/bin/env python3
"""Driver regressions: real rule predicates and append-only ledger round trips."""
import datetime as dt
import contextlib
import hashlib
import subprocess
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
        # The synthetic runner now crosses the real effective-base resolver.
        import look
        repo = self.root / 'repos/proj'
        subprocess.run(['git', '-C', str(repo), 'init', '-q', '-b', 'master'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-q',
                        '--allow-empty', '-m', 'fixture'], check=True)
        self.base = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        config = look.load_toml()
        config = dict(config, push={**config.get('push', {}),
                      'main': {**config.get('push', {}).get('main', {}), 'proj': 'master'}})
        config_patch = patch.object(look, 'load_toml', return_value=config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
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
                if role == 'builder':
                    self.assertEqual(self.calls.pop(0)[:2], ['doit', 'readiness'])
                self.assertEqual(self.calls[0][:4], ['doit', 'packet', role, self.sid])
                self.assertEqual(self.calls[1][:5], ['doit', 'dispatch', '--detach', role, self.sid])
                if role == 'grader':
                    self.assertEqual(self.calls[1][-2:], ['--timeout', '30'])
                else:
                    self.assertEqual(self.calls[0][-4:], ['--repo', str(self.root / 'repos/proj'),
                                                        '--base-sha', self.base])
                self.assertEqual(self.appended()[0]['rule'], action)

    def test_tick_build_creates_then_records_worktree(self):
        self.worktree.rmdir()
        def runner(argv):
            if 'worktree' in argv and 'add' in argv:
                self.worktree.mkdir()
            return self.runner(argv)
        tick.run_steps(self.events, self.specs, self.now, dispatcher=runner, steps=[self.step('build')])
        self.assertEqual([a[0] for a in self.calls], ['git', 'git', 'doit', 'doit', 'doit'])
        self.assertEqual(self.calls[1][-5:], ['add', str(self.worktree), '-b', self.sid.lower(), self.base])
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
        (self.root / 'content' / 'L-spec-1002.md').write_text('AC1 [backend]: fixture.\n')
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
                self.browser_config()
                original = self.events[:]
                self.events.extend(extra)
                self.calls.clear()
                self.run_steps([self.step()])
                self.assertEqual(self.calls[0][-4:], ['--depth', depth, '--round', round_n])
                self.events = original

    def browser_config(self):
        path = self.root / 'review-mcp.json'
        path.write_text(json.dumps({'mcpServers': {'chrome-devtools': {
            'command': sys.executable, 'args': ['fixture-mcp.py']}}}))
        return path

    def test_review_browser_readiness_holds_once_without_spending_attempts(self):
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [ui]: fixture.\n')
        for _ in range(3):
            out = self.run_steps([self.step()])
            self.assertEqual(out, [f'hold {self.sid}: review-readiness: missing review-mcp.json'])
        self.assertEqual(self.calls, [])
        self.assertEqual([e['type'] for e in self.appended()], ['autodispatch-skipped'])
        self.assertNotIn('actor', self.appended()[0])
        # Restoration releases this readiness hold without an operator reset.
        config = self.browser_config()
        self.assertTrue(self.run_steps([self.step()])[0].startswith('acted'))
        self.assertEqual(self.calls[-1][-2:], ['--mcp-config', str(config)])
        self.assertFalse(any(e['type'] == 'autodispatch-failed' for e in self.appended()))

    def test_review_browser_invalid_configs_and_direct_launch_are_pre_dispatch(self):
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [ui]: fixture.\n')
        config = self.root / 'review-mcp.json'
        for body in ('private-fixture-value', '{}', '[]',
                     '{"mcpServers":{"other":{"command":"npx"}}}',
                     '{"mcpServers":{"chrome-devtools":{"args":[]}}}'):
            with self.subTest(body=body):
                config.write_text(body)
                self.assertIn('review-readiness: invalid review-mcp.json',
                              self.run_steps([self.step()])[0])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.appended()), 1)
        self.assertNotIn('private-fixture-value', json.dumps(self.appended()))
        config.unlink()
        runner = tick._PassDispatcher(self.runner, self.events, self.specs, self.now, False)
        self.assertIsNone(tick.launch(self.step(), dispatcher=runner))
        self.assertEqual(self.calls, [])
        self.assertTrue(all(e['type'] == 'autodispatch-skipped' for e in self.appended()))

    def test_review_browser_readiness_dry_run_is_read_only(self):
        (self.root / 'content' / (self.sid + '.md')).write_text('AC1 [ui]: fixture.\n')
        self.assertIn('review-readiness: missing review-mcp.json',
                      self.run_steps([self.step()], dry_run=True)[0])
        self.assertEqual(self.appended(), [])
        self.assertEqual(self.calls, [])


def r7_evidence(name, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://r7/' + name, status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)), flush=True)


class R7Fixture:
    """Local packet/git/ledger fixture shared by the caller and tick proofs."""
    sid = 'L-spec-9001'
    project = 'proj'

    def __init__(self, stack, default='main', case='default'):
        import packet
        import look
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='r7-pin-'))).resolve()
        self.repo = self.root / 'repos/proj'
        self.wt = self.root / 'worktrees/proj' / self.sid.lower()
        self.spec = self.root / 'content' / (self.sid + '.md')
        self.seat = self.root / 'seat'
        self.calls = []
        self.packets = []
        self.dispatched = []
        self.default, self.case = default, case
        self.branch = 'realm' if case == 'realm' else default
        self.now = dt.datetime.now(dt.timezone.utc)
        for directory in ('events', 'content', 'seat', 'repos/proj', 'worktrees/proj'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for module, key, value in (
                (fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                (fold, 'PROJECT', None), (dispatch, 'ROOT', self.root),
                (dispatch, 'EVENTS', self.root / 'events'),
                (dispatch, 'CONTENT', self.root / 'content'), (dispatch, 'SEAT', self.seat),
                (packet, 'ROOT', self.root), (packet, 'CONTENT', self.root / 'content'),
                (packet, 'PACKETS', self.root / 'packets')):
            stack.enter_context(patch.object(module, key, value))
        stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1'))
        for key in ('DOIT_LEDGER_FILE', 'DOIT_REAP_LEDGER_FILE', 'DOIT_PROJECT'):
            stack.enter_context(patch.dict(os.environ))
            os.environ.pop(key, None)
        original_config = look.load_toml()
        config = dict(original_config, push={**original_config.get('push', {}),
                      'main': {**original_config.get('push', {}).get('main', {}), 'proj': default}})
        stack.enter_context(patch.object(look, 'load_toml', return_value=config))
        self.git('init', '-q', '-b', default)
        self.commit(self.repo, 'seed.txt')
        self.old = self.git('rev-parse', 'HEAD')
        if case == 'realm':
            self.git('branch', 'realm')
        self.advance()
        if case == 'other-head':
            self.git('checkout', '-qb', 'other', self.old)
            self.commit(self.repo, 'other')
        self.spec.write_text('# Goal\nCaller fixture.\n' +
            ('Base: realm\n' if case == 'realm' else '') +
            'Writes:\n- seed.txt\n## Acceptance criteria\nAC1 [backend]: caller pin.\n'
            'review_path: run the fixture / worked if pinned / failed otherwise.\n'
            '## Verification\n```bash\ntrue\n```\n')
        self.event('L-spec-writer-9001.jsonl', 'spec-written',
                   ts=(self.now - dt.timedelta(hours=2)).isoformat(), project='proj',
                   path=str(self.spec), footprint=['seed.txt'], base_sha=self.old)
        self.event('L-builder-8000.jsonl', 'build-done',
                   ts=(self.now - dt.timedelta(hours=1)).isoformat(), project='proj',
                   status='DONE', base_sha=self.old, ready_sha=self.old,
                   card=str(self.root / 'content/old-card.md'))
        (self.root / 'content/old-card.md').write_text('Previous build.\n')

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', '-C', str(cwd or self.repo), *args],
                                capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def commit(self, cwd, name):
        (cwd / name).write_text(name + '\n')
        self.git('add', name, cwd=cwd)
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', name, cwd=cwd)
        return self.git('rev-parse', 'HEAD', cwd=cwd)

    def advance(self):
        # Use a separate checkout: the repository's symbolic HEAD may differ.
        with tempfile.TemporaryDirectory(prefix='r7-advance-') as directory:
            tree = Path(directory) / 'tree'
            self.git('worktree', 'add', '-q', '--detach', str(tree), self.branch)
            try:
                self.commit(tree, 'advance-' + str(len(self.git('rev-list', self.branch).splitlines())))
                pin = self.git('rev-parse', 'HEAD', cwd=tree)
                self.git('update-ref', 'refs/heads/' + self.branch, pin)
            finally:
                self.git('worktree', 'remove', str(tree))
        # Refresh only a checked-out effective branch in this synthetic repo.
        if self.git('symbolic-ref', '--short', 'HEAD') == self.branch:
            self.git('reset', '--hard', self.branch)
        return pin

    def worktree(self, pin=None):
        self.git('worktree', 'add', '-q', '-b', self.sid.lower(), str(self.wt), pin or self.branch)

    def event(self, filename, kind, **fields):
        path = self.root / 'events' / filename
        with path.open('a') as stream:
            stream.write(json.dumps(dict(v=1, type=kind, subject=self.sid,
                              ts=fields.pop('ts', self.now.isoformat()), **fields)) + '\n')

    def events(self):
        return fold.read_events(use_cache=False)

    def runner(self, argv):
        import packet
        self.calls.append(list(argv))
        if argv[0] == 'git':
            result = subprocess.run(argv, capture_output=True, text=True)
            return dict(code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if argv[:2] == ['doit', 'readiness']:
            return dict(code=0, stdout='readiness fixture accepted', stderr='')
        if argv[:2] == ['doit', 'packet']:
            try:
                path = packet.main(argv[2:])
            except (ValueError, SystemExit) as exc:
                return dict(code=1, stdout='', stderr=str(exc))
            self.packets.append(path)
            return dict(code=0, stdout=str(path), stderr='')
        if argv[:2] == ['doit', 'dispatch']:
            path = Path(argv[argv.index('--packet') + 1])
            self.dispatched.append(path)
            return dict(code=0, stdout=json.dumps(dict(detached='L-builder-9002')), stderr='')
        raise AssertionError(argv)

    def pin(self):
        return self.git('rev-parse', self.branch)


class R7PinCallerTests(unittest.TestCase):
    def test_effective_base_transport(self):
        import packet
        for default, case in (('main', 'default'), ('master', 'default'),
                              ('main', 'other-head'), ('main', 'realm')):
            with self.subTest(default=default, case=case), contextlib.ExitStack() as stack:
                f = R7Fixture(stack, default, case)
                f.worktree()
                for attempt in range(2):
                    if attempt:
                        f.advance()
                    pin = f.pin()
                    self.assertEqual(lane_driver._dispatch('builder', f.sid, 'proj', None, f.runner),
                                     'L-builder-9002')
                    argv = [a for a in f.calls if a[:3] == ['doit', 'packet', 'builder']][-1]
                    self.assertEqual(argv[-2:], ['--base-sha', pin])
                    text = f.dispatched[-1].read_text()
                    self.assertEqual(dispatch.pinned_base(f.dispatched[-1]), pin)
                    self.assertIn('PINNED_BASE_SHA_EXPLICIT: true', text)
                    r7_evidence('AC1', dict(case=case, default=default, attempt=attempt,
                        effective_ref=f.branch, effective_sha=pin, argv=argv,
                        packet_pin=dispatch.pinned_base(f.dispatched[-1]), explicit=True,
                        dispatch_count=len(f.dispatched)))
                f.calls.clear()
                with patch.object(packet, '_effective_base', side_effect=ValueError('unresolved base')):
                    with self.assertRaisesRegex(ValueError, 'unresolved base'):
                        lane_driver._dispatch('builder', f.sid, 'proj', None, f.runner)
                self.assertEqual(f.calls, [])
                calls = []
                def failed(argv):
                    calls.append(argv)
                    return dict(code=1, stderr='packet refused')
                with self.assertRaisesRegex(ValueError, 'packet refused'):
                    lane_driver._dispatch('builder', f.sid, 'proj', None, failed)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:2], ['doit', 'readiness'])
                r7_evidence('AC1-failures', dict(case=case, resolver_dispatches=0, packet_dispatches=0))


class FreshWorktree1856PacketTests(unittest.TestCase):
    def fixture(self, stack, kind='unblocked', default='main', dirty=False, missing=False):
        f=R7Fixture(stack,default)
        f.worktree(f.old)
        retained=f.commit(f.wt,'builder-work')
        f.event('L-builder-1856.jsonl','build-done',status='BLOCKED',project='proj',
                base_sha=f.old,ready_sha=retained,branch=f.sid.lower(),card=str(f.root/'content/old-card.md'),
                ts=(f.now-dt.timedelta(seconds=30)).isoformat())
        f.event('L-builder-1856.jsonl','build-blocked',reason='fixture blocked',
                ts=(f.now-dt.timedelta(seconds=29)).isoformat())
        if kind=='window-dropped':
            f.event('L-executor-1856.jsonl',kind,reason='owner',window='fixture-window',
                    ts=(f.now-dt.timedelta(seconds=10)).isoformat())
        elif kind=='rework':
            f.event('L-operator-1856.jsonl','decision',rework='yes',
                    ts=(f.now-dt.timedelta(seconds=10)).isoformat())
        else:
            f.event('L-operator-1856.jsonl',kind,ts=(f.now-dt.timedelta(seconds=10)).isoformat())
        if dirty:
            (f.wt/'builder-work').write_text('uncommitted\n');(f.wt/'untracked').write_text('untracked\n')
        if missing:
            import shutil
            shutil.rmtree(f.wt)
        return f,retained

    def run_tick(self,f,**kw):
        events=f.events()
        return tick.run_steps(events,fold.fold(events)[0],f.now,dispatcher=kw.pop('runner',f.runner),**kw)

    def test_AC1_trigger_scope_and_rework_precedence(self):
        import worktree_heal
        for kind in ('unblocked','spec-amended','window-dropped','rework'):
            with self.subTest(kind=kind),contextlib.ExitStack() as stack:
                f,retained=self.fixture(stack,kind)
                events=f.events();s=nextstep.step_for(events,fold.fold(events)[0],f.sid,f.now,lane_rules.load())
                self.assertEqual(s.action,'rebuild',s)
                self.assertEqual(worktree_heal.fresh_trigger(s,events),kind!='rework')
                if kind=='rework':
                    self.run_tick(f)
                    self.assertEqual(f.git('rev-parse','HEAD',cwd=f.wt),retained)
                    self.assertEqual(list(f.wt.parent.glob('*-pre-*')),[])
                else:
                    before=f.git('show-ref')
                    self.run_tick(f,dry_run=True)
                    self.assertEqual(f.git('show-ref'),before)
                    self.assertFalse(f.dispatched)
        for control in ('hold','disabled','older-amendment','newer-DONE','mixed-rework'):
            with self.subTest(control=control),contextlib.ExitStack() as stack:
                f,retained=self.fixture(stack)
                if control=='hold': f.event('L-operator-1856.jsonl','escalation-blocking',why='fixture hold',default='wait',deadline='2099-01-01',revert='none')
                if control=='disabled':
                    (f.root/'state').mkdir();(f.root/'state/worktree-heal.off').touch()
                if control=='older-amendment':
                    (f.root/'events/L-operator-1856.jsonl').unlink()
                    f.event('L-operator-1856.jsonl','spec-amended',ts=(f.now-dt.timedelta(seconds=60)).isoformat())
                if control=='newer-DONE': f.event('L-builder-1856.jsonl','build-done',status='DONE',ready_sha=retained,base_sha=f.old,card=str(f.root/'content/old-card.md'))
                if control=='mixed-rework': f.event('L-operator-1856.jsonl','decision',rework='yes',ts=(f.now-dt.timedelta(seconds=20)).isoformat())
                self.run_tick(f)
                self.assertEqual(f.git('rev-parse','HEAD',cwd=f.wt),retained)
                self.assertEqual(list(f.wt.parent.glob('*-pre-*')),[])

    def test_AC2_lossless_archival_matrix(self):
        for default in ('main','master'):
            for case in ('clean','dirty','missing','collision','no-ahead'):
                with self.subTest(default=default,case=case),contextlib.ExitStack() as stack:
                    f,retained=self.fixture(stack,default=default,dirty=case=='dirty',missing=case=='missing')
                    if case=='collision':f.git('branch',f.sid.lower()+'-pre-1',f.old)
                    if case=='no-ahead':f.git('reset','--hard',f.old,cwd=f.wt)
                    pin=f.pin();out=self.run_tick(f)
                    self.assertTrue(any(line.startswith('acted') for line in out),out)
                    archive=f.sid.lower()+'-pre-'+('2' if case=='collision' else '1')
                    if case=='no-ahead':
                        self.assertEqual(f.git('rev-parse','HEAD',cwd=f.wt),pin)
                        self.assertFalse(list(f.wt.parent.glob('*-pre-*')))
                        continue
                    self.assertEqual(f.git('rev-parse',archive),retained)
                    self.assertEqual(f.git('rev-parse','HEAD',cwd=f.wt),pin)
                    self.assertEqual(f.git('symbolic-ref','--short','HEAD',cwd=f.wt),f.sid.lower())
                    if case!='missing':
                        archived=f.wt.with_name(archive)
                        self.assertEqual(f.git('rev-parse','HEAD',cwd=archived),retained)
                        self.assertEqual((archived/'builder-work').read_text(),'uncommitted\n' if case=='dirty' else 'builder-work\n')
                        if case=='dirty':self.assertEqual((archived/'untracked').read_text(),'untracked\n')
                    self.assertFalse(any('reset' in a or '--force' in a or '-f' in a for a in f.calls))
                    print(json.dumps(dict(criterion='AC2',default=default,case=case,retained=retained,fresh=pin,archive=archive)))

    def test_AC3_packet_preflight_and_base_movement(self):
        import argparse
        import packet
        for move in (False,True):
            with self.subTest(move=move),contextlib.ExitStack() as stack:
                f,retained=self.fixture(stack)
                f.event('L-operator-1856.jsonl','dependency-hold',waits_for='L-spec-9000',ts=(f.now-dt.timedelta(seconds=20)).isoformat())
                pin=f.pin();moved=[]
                def runner(argv):
                    answer=f.runner(argv)
                    if move and argv[:2]==['git','-C'] and 'worktree' in argv and 'add' in argv and argv[-1]==pin and not moved:
                        moved.append(f.advance())
                    return answer
                out=self.run_tick(f,runner=runner)
                if move:
                    self.assertFalse(f.dispatched);self.assertTrue(any('moved' in line or 'does not match' in line for line in out),out)
                    continue
                self.assertEqual(dispatch.pinned_base(f.dispatched[-1]),pin)
                self.assertIn('PINNED_BASE_SHA_EXPLICIT: true',f.dispatched[-1].read_text())
                argv=next(a for a in f.calls if a[:3]==['doit','packet','builder'])
                self.assertEqual(argv[-2:],['--base-sha',pin])
                args=argparse.Namespace(subject=f.sid,project='proj',pinned_base_sha=pin,_dispatch_builder=True)
                def fail(message,**kw):raise AssertionError((message,kw))
                dispatch.builder_worktree_preflight(args,str(f.wt),f.root/'events/L-builder-1856.jsonl',{'subject':f.sid},fail)
                commit=f.commit(f.wt,'fresh-builder')
                self.assertEqual(f.git('rev-parse',commit+'^'),pin)
                self.assertEqual(f.git('rev-list','--count',pin+'..'+commit),'1')

    def test_AC4_refusals_and_partial_archive_resume(self):
        for fault in ('locked','other-head','explicit-base','move-failure','leftover','wrong-add','wrong-branch','record','readback','bad-recorded-base'):
            with self.subTest(fault=fault),contextlib.ExitStack() as stack:
                f,retained=self.fixture(stack,dirty=fault=='move-failure')
                if fault=='locked':f.git('worktree','lock',str(f.wt))
                if fault=='other-head':f.git('checkout','-qb','other')
                if fault=='explicit-base':
                    f.git('branch','realm');f.spec.write_text('Base: realm\n'+f.spec.read_text())
                if fault=='bad-recorded-base':
                    path=f.root/'events/L-builder-1856.jsonl'
                    rows=[json.loads(line) for line in path.read_text().splitlines()]
                    rows[0]['base_sha']='f'*40
                    path.write_text(''.join(json.dumps(e)+'\n' for e in rows))
                failed=[]
                def runner(argv):
                    if fault=='move-failure' and 'move' in argv and not failed:
                        failed.append(True);return dict(code=1,stdout='',stderr='fixture move failed')
                    if fault=='leftover' and 'move' in argv:
                        answer=f.runner(argv);f.wt.mkdir();return answer
                    if fault=='wrong-add' and 'add' in argv:
                        argv=list(argv);argv[-1]=f.old
                    if fault=='wrong-branch' and 'add' in argv:
                        argv=list(argv);argv[argv.index('-b')+1]='wrong-branch'
                    return f.runner(argv)
                if fault in ('record','readback'):
                    import resources
                    target='record' if fault=='record' else '_readback'
                    stack.enter_context(patch.object(resources,target,side_effect=resources.RecordReadbackError('record refused')))
                out=self.run_tick(f,runner=runner)
                self.assertFalse(f.dispatched,out)
                self.assertTrue(any(line.startswith('failed') for line in out),out)
                self.assertIn(retained,f.git('show-ref'))
                if fault in ('locked','other-head','explicit-base'):
                    self.assertEqual(f.git('rev-parse','HEAD',cwd=f.wt),retained)
                    self.assertFalse(list(f.wt.parent.glob('*-pre-*')))
                if fault=='explicit-base':self.assertTrue(any('the Executor cuts this worktree by hand' in line for line in out))
                if fault=='move-failure':
                    # Resume the same journal at the next due tick, without a second archive.
                    f.now+=dt.timedelta(minutes=6)
                    self.run_tick(f)
                    self.assertEqual(len(f.dispatched),1)
                    self.assertEqual([p.name for p in f.wt.parent.glob('*-pre-*')],[f.sid.lower()+'-pre-1'])
                    self.assertEqual((f.wt.with_name(f.sid.lower()+'-pre-1')/'builder-work').read_text(),'uncommitted\n')

    def test_AC4_path_and_git_failures_preserve_foreign_content(self):
        import worktree_heal
        for fault in ('unsafe-project','unsafe-subject','symlink','foreign','unreadable'):
            with self.subTest(fault=fault),contextlib.ExitStack() as stack:
                f,retained=self.fixture(stack)
                events=f.events();step=nextstep.step_for(events,fold.fold(events)[0],f.sid,f.now,lane_rules.load())
                project='../../outside' if fault=='unsafe-project' else 'proj'
                if fault=='unsafe-subject':step=replace(step,item='L-spec-../../outside')
                if fault in ('symlink','foreign'):
                    retained_path=f.wt.with_name('retained-foreign')
                    f.git('worktree','move',str(f.wt),str(retained_path))
                    if fault=='symlink':f.wt.symlink_to(f.root/'absent')
                    else:f.wt.mkdir();(f.wt/'sentinel').write_text('foreign')
                def runner(argv):
                    if fault=='unreadable':return dict(code=1,stdout='',stderr='unreadable git')
                    return f.runner(argv)
                with self.assertRaises(ValueError):worktree_heal.prepare_fresh(step,events,f.root,project,f.wt,runner)
                self.assertFalse(f.dispatched)
                self.assertEqual(f.git('rev-parse',f.sid.lower()),retained)
                if fault=='foreign':self.assertEqual((f.wt/'sentinel').read_text(),'foreign')

    def test_AC4_foreign_registered_branch_is_not_moved(self):
        import worktree_heal
        with contextlib.ExitStack() as stack:
            f,retained=self.fixture(stack)
            f.git('checkout','-b','foreign-builder',cwd=f.wt)
            (f.wt/'foreign-sentinel').write_text('preserve here')
            events=f.events();step=nextstep.step_for(events,fold.fold(events)[0],f.sid,f.now,lane_rules.load())
            with self.assertRaisesRegex(ValueError,'branch mismatch'):
                worktree_heal.prepare_fresh(step,events,f.root,'proj',f.wt,f.runner)
            self.assertEqual((f.wt/'foreign-sentinel').read_text(),'preserve here')
            self.assertEqual(f.git('symbolic-ref','--short','HEAD',cwd=f.wt),'foreign-builder')
            self.assertFalse(list(f.wt.parent.glob('*-pre-*')))
            self.assertEqual(f.git('rev-parse',f.sid.lower()),retained)

    def test_AC5_resource_round_trip_and_reaper_retention(self):
        import resources
        import tree_cleanup
        with contextlib.ExitStack() as stack:
            f,retained=self.fixture(stack)
            self.run_tick(f)
            env=dict(os.environ,DOIT_ROOT=str(f.root),PYTHONPATH=str(Path(__file__).parent.resolve()))
            read=subprocess.run([sys.executable,'-c','import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'],env=env,capture_output=True,text=True,check=True)
            events=json.loads(read.stdout)
            created=[e for e in events if e['type']=='resource-created']
            archived=f.wt.with_name(f.sid.lower()+'-pre-1')
            self.assertEqual({e['ref'] for e in created},{str(f.wt),str(archived)})
            self.assertTrue(all(e['subject']==f.sid and e['lane']=='build' for e in created))
            self.assertEqual(tree_cleanup._spec_worktree_paths(f.sid,events),[f.wt])
            # Remove the synthetic parent receipt from reaper input; no worker ran.
            evidence=[e for e in events if e['type']!='autodispatched']
            f.commit(f.wt,'uncovered')
            f.event('L-operator-1856.jsonl','spec-killed',why='terminal fixture')
            evidence=[e for e in f.events() if e['type']!='autodispatched']
            reasons=[]
            for resource in resources.live(evidence):
                try:reasons.append(resources._worktree(resource,evidence)[3])
                except tree_cleanup.Undetermined as exc:reasons.append('undetermined: '+str(exc))
            self.assertIn('undetermined: owner branch does not match recorded path',reasons)
            self.assertTrue(any('holds work' in why or 'holds-work' in why for why in reasons),reasons)
            self.assertEqual(f.git('rev-parse',f.sid.lower()+'-pre-1'),retained)


if __name__ == '__main__':
    unittest.main()
