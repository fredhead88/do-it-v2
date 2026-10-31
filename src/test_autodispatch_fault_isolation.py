#!/usr/bin/env python3
"""Scratch-only regressions for malformed holds and stalled dispatch passes."""
import atexit
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack

# Isolate write-capable imports, including caches, sessions and carry inputs.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='fault-isolation-import-')
atexit.register(_IMPORT_ROOT.cleanup)
os.environ.update(DOIT_ROOT=_IMPORT_ROOT.name, DOIT_NO_POKE='1',
                  DOIT_READ_CACHE='0',
                  V4_LEDGER_DIR=_IMPORT_ROOT.name + '/v4',
                  V4_INBOX_DIR=_IMPORT_ROOT.name + '/inbox',
                  V4_STAGING_DIR=_IMPORT_ROOT.name + '/staging')
os.environ.pop('DOIT_PROJECT', None)
import autodispatch as ad
import dispatch
import fold
import fold_stalls
import lane_rules
import look
import nextstep
import tick

NOW = dt.datetime(2026, 10, 31, 18, tzinfo=dt.timezone.utc)
SID = 'L-spec-1895'
INVALID = [None, '', '   ', 'L-spec-1\n', ' L-spec-1890', 'L-spec-1890 ', 'other', 'L-spec-x',
           [], ['L-spec-1890'], ['L-spec-1890', 'L-spec-1892'], {},
           {'id': 'L-spec-1890'}, True, False, 7, 1.5]
PREFIX = 'held: invalid waits_for; unresolvable dependency-hold'
CLEARS = ('thinker/operator append a newer dependency-hold with '
          'waits_for=L-spec-<digits> or explicitly release the hold')


def event(kind, subject=SID, minute=-120, actor='thinker', **kv):
    return dict(type=kind, subject=subject, actor=actor,
                ts=(NOW + dt.timedelta(minutes=minute)).isoformat(),
                _src='L-' + actor + '-fixture.jsonl:1', **kv)


def signed(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='scratch://autodispatch-fault-isolation/' + ac,
                         status=0, body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=payload)))


class Scratch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='fault-isolation-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for part in ('events', 'content', 'seat', 'sessions', 'projects'):
            (self.root / part).mkdir()
        for module, names in ((fold, ('ROOT', 'EVENTS')),
                              (dispatch, ('ROOT', 'EVENTS', 'CONTENT', 'SEAT'))):
            for name in names:
                self.swap(module, name, self.root if name == 'ROOT' else self.root / name.lower())
        self.swap(fold, 'PROJECT', None)
        self.swap(fold, 'NOW', NOW)
        self.swap(dispatch, 'now', lambda: NOW.isoformat())
        self.swap(look.panes, 'SESSIONS', self.root / 'sessions')
        self.swap(look.pane_resume, 'PROJECTS', self.root / 'projects')
        env = patch.dict(os.environ, DOIT_ROOT=str(self.root),
                         DOIT_LEDGER_FILE='L-thinker-fixture.jsonl')
        env.start()
        self.addCleanup(env.stop)
        self.rules = lane_rules.load()

    def swap(self, module, name, value):
        p = patch.object(module, name, value)
        p.start()
        self.addCleanup(p.stop)

    def read(self, snapshot=None):
        return fold.read_events(use_cache=False) if snapshot is None else fold.read_events(snapshot=snapshot)

    def raw(self, rows, filename='L-thinker-fixture.jsonl'):
        path = self.root / 'events' / filename
        with path.open('a') as handle:
            for row in rows:
                handle.write(json.dumps({k: v for k, v in row.items() if k not in ('actor', '_src')}) + '\n')
        return path

    def specs(self, rows):
        return {e['subject']: dict(id=e['subject'], state='written',
                                  evs=[r for r in rows if r['subject'] == e['subject']])
                for e in rows if e['type'] == 'spec-written'}

    def steps(self, rows):
        return nextstep.next_steps(rows, self.specs(rows), NOW, self.rules)

    def written(self, sid=SID):
        return event('spec-written', subject=sid, actor='operator', project='fixture')


class AppendValidationTests(Scratch):
    def test_matrix(self):
        dst = self.root / 'events/L-thinker-fixture.jsonl'
        dst.write_bytes(b'')
        rejected = []
        for value in INVALID:
            for strict in (False, True):
                for key in ('waits_for=', 'waits_for:='):
                    before = dst.read_bytes()
                    args = ['dependency-hold', SID, key + json.dumps(value), 'why=fixture']
                    with self.assertRaisesRegex(SystemExit, 'waits_for'):
                        fold.append(args, strict=strict)
                    self.assertEqual(dst.read_bytes(), before)
                before = dst.read_bytes()
                reason = dispatch.emit(dst, {}, 'dependency-hold', strict=strict,
                                       subject=SID, waits_for=value, why='fixture')
                self.assertIn('waits_for', reason)
                self.assertEqual(dst.read_bytes(), before)
                self.assertIn('waits_for', fold.check_append(dict(type='dependency-hold',
                    subject=SID, waits_for=value, why='fixture'), 'thinker'))
            rejected.append(value)
        for strict in (False, True):
            before = dst.read_bytes()
            with self.assertRaisesRegex(SystemExit, 'waits_for'):
                fold.append(['dependency-hold', SID, 'why=fixture'], strict=strict)
            self.assertEqual(before, dst.read_bytes())
        for strict in (False, True):
            before = dst.read_bytes()
            self.assertIn('waits_for', dispatch.emit(dst, {}, 'dependency-hold',
                          strict=strict, subject=SID, why='fixture'))
            self.assertEqual(before, dst.read_bytes())
        for dependency in ('L-spec-0', 'L-spec-1890', 'L-spec-12345'):
            before = dst.read_bytes()
            fold.append(['dependency-hold', SID, 'waits_for=' + dependency, 'why=fixture'], strict=True)
            with dst.open() as handle:
                self.assertEqual(json.loads(handle.readlines()[-1])['waits_for'], dependency)
            self.assertIsNone(dispatch.emit(dst, {}, 'dependency-hold', strict=True,
                                           subject=SID, waits_for=dependency, why='fixture'))
            with dst.open() as handle:
                self.assertEqual(json.loads(handle.readlines()[-1])['waits_for'], dependency)
            self.assertTrue(dst.read_bytes().startswith(before))
        before = dst.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'why'):
            fold.append(['dependency-hold', SID, 'waits_for=L-spec-1'], strict=True)
        with patch.dict(os.environ, DOIT_LEDGER_FILE='L-builder-fixture.jsonl'):
            with self.assertRaises(SystemExit):
                fold.append(['dependency-hold', SID, 'waits_for=L-spec-1', 'why=fixture'], strict=True)
        self.assertEqual(before, dst.read_bytes())
        signed('AC1', dict(rejected=rejected, missing=True, strict_modes=[False, True],
                          coercions=['k=v', 'k:=JSON'], fresh_handle_roundtrips=6,
                          actor_and_why_preserved=True, append_only=True))


class HistoricalHoldTests(Scratch):
    def test_historical_matrix(self):
        results = []
        for value in INVALID:
            hold = event('dependency-hold', waits_for=value)
            for kwargs in ({}, dict(shipped=set())):
                reason = ad.held_by_dependency([hold], [hold], **kwargs)
                self.assertTrue(reason.startswith(PREFIX), reason)
                self.assertIn('unresolvable', reason)
            results.append(dict(value=value, reason=reason))
        missing = event('dependency-hold')
        self.assertTrue(ad.held_by_dependency([missing], []).startswith(PREFIX))
        incident = event('dependency-hold', waits_for=['L-spec-1890', 'L-spec-1892'])
        incident['_src'] = 'L-thinker-0004.jsonl:10979'
        self.raw([event('dependency-hold', waits_for=value) for value in INVALID] + [incident])
        with patch.dict(os.environ, DOIT_READ_CACHE='1'):
            immutable_rows = fold.read_events(snapshot=fold.ReaderSnapshot())
        for immutable in immutable_rows:
            for kwargs in ({}, dict(shipped=set())):
                self.assertTrue(ad.held_by_dependency([immutable], [], **kwargs).startswith(PREFIX))
        corrected = event('dependency-hold', minute=-119, waits_for='L-spec-1890')
        corrected['_src'] = 'L-thinker-0004.jsonl:11028'
        for rows in ([incident, corrected], [corrected, incident]):
            self.assertIn('waits for L-spec-1890', ad.held_by_dependency(rows, []))
            self.assertIsNone(ad.held_by_dependency(rows, [], shipped={'L-spec-1890'}))
            self.assertIsNone(ad.held_by_dependency(rows, [event('shipped', subject='L-spec-1890', minute=-130)]))
        self.assertIn('unresolvable', ad.held_by_dependency([event('dependency-hold', waits_for=SID)], []))
        for kind in ('decision', 'unblocked'):
            release = event(kind, minute=-119, ref=incident['_src'])
            self.assertIsNone(ad.held_by_dependency([incident, release], []))
            release['actor'] = 'builder'
            self.assertTrue(ad.held_by_dependency([incident, release], []).startswith(PREFIX))
        unrelated = event('decision', minute=-118, ref='elsewhere:1')
        self.assertTrue(ad.held_by_dependency([incident, unrelated], []).startswith(PREFIX))
        rows = [self.written(), incident]
        spec = dict(id=SID, state='written', age=3, evs=rows)
        context = dict(events=rows, shipped=set(), capability_specs={}, capabilities={},
                       answered=set(), windows={})
        result = fold_stalls.classify(spec, {SID: spec}, {SID: rows}, context, NOW, {'written': 1})
        self.assertEqual(result['bucket'], 'STUCK')
        signed('AC2', dict(matrix=results, immutable_snapshot=True,
                          incident_source=incident['_src'], corrected_newer=True,
                          authorized_release=True, overdue=result))


class PerSpecIsolationTests(Scratch):
    def test_invalid_hold_and_ruling(self):
        for value in INVALID:
            for ruling in (False, True):
                rows = [self.written(), event('dependency-hold', waits_for=value)]
                if ruling:
                    rows.append(event('decision', action='hold', waits_for='L-spec-1890'))
                with patch.object(nextstep, '_held_result', side_effect=AssertionError('invalid blocker lookup')):
                    step, = self.steps(rows)
                self.assertEqual((step.item, step.item_kind, step.state, step.action, step.owner),
                                 (SID, 'spec', 'hold', None, 'executor'))
                self.assertTrue(step.reason.startswith(PREFIX), step)
                self.assertEqual(step.clears, CLEARS)
                # Direct predicate path must also avoid blocker lookup.
                c = dict(dependency=PREFIX, evs=rows)
                with patch.object(nextstep, '_held_result', side_effect=AssertionError('lookup')):
                    result = nextstep._dependency_held(c)
                self.assertEqual(result['reason'], PREFIX)
            step, = self.steps([self.written(), event('decision', action='hold', waits_for=value)])
            self.assertEqual(step.reason, 'undetermined: ruling needs waits_for')
            self.assertEqual(step.clears, 'valid action-bearing decision')
        rows = [self.written(), event('decision', action='hold', waits_for='L-spec-1890')]
        self.assertIn('ruling-unexecuted', self.steps(rows)[0].reason)
        rows.append(event('dependency-hold', minute=-110, waits_for='L-spec-1890'))
        self.assertNotIn('ruling-unexecuted', self.steps(rows)[0].reason)
        incident = event('dependency-hold', waits_for=['L-spec-1890', 'L-spec-1892'])
        incident['_src'] = 'L-thinker-0004.jsonl:10979'
        step, = self.steps([self.written(), incident])
        self.assertEqual(step.trigger, incident['_src'])
        self.assertEqual(step.since, incident['ts'])
        self.assertEqual(step.stage, 'planned')
        self.assertTrue(step.reason.startswith(PREFIX))
        self.raw([self.written(), incident])
        for value in ([], ['L-spec-1890'], ['L-spec-1890', 'L-spec-1892']):
            self.raw([event('dependency-hold', waits_for=value)])
            with patch.dict(os.environ, DOIT_READ_CACHE='1'):
                immutable_rows = fold.read_events(snapshot=fold.ReaderSnapshot())
            step, = self.steps(immutable_rows)
            self.assertTrue(step.reason.startswith(PREFIX))
            self.assertEqual(step.clears, CLEARS)
        with patch.dict(os.environ, DOIT_READ_CACHE='1'):
            rows = fold.read_events(snapshot=fold.ReaderSnapshot())
        step, = self.steps(rows)
        self.assertTrue(step.reason.startswith(PREFIX))
        # A corrected hold at the same timestamp wins by numeric source line,
        # even when snapshot content ordering places an invalid row last.
        old = event('dependency-hold', waits_for=['L-spec-1890', 'L-spec-1892'])
        old['_src'] = 'L-thinker-fixture.jsonl:9'
        new = dict(old, waits_for='L-spec-1890', _src='L-thinker-fixture.jsonl:10')
        ruling = event('decision', action='hold', waits_for='L-spec-1890')
        for holds in ([old, new], [new, old]):
            chosen, = self.steps([self.written(), ruling, *holds])
            self.assertIn('waits for L-spec-1890', chosen.reason)
            self.assertEqual(chosen.trigger, new['_src'])
        signed('AC3-invalid', dict(values=INVALID, both_ruling_branches=True,
                                  immutable_snapshot=True, step=step.__dict__))

    def test_exception_isolation(self):
        rows = [self.written(s) for s in ('L-spec-1894', SID, 'L-spec-1896')]
        # Operator-written fixtures are exempt from the initial audit.
        original = json.dumps(rows, sort_keys=True)
        healthy_rows = [r for r in rows if r['subject'] != SID]
        expected = self.steps(healthy_rows)
        cases = []
        for phase in ('context', 'predicate', 'choice'):
            name = {'context': '_wave', 'choice': '_choose'}.get(phase)
            target = getattr(nextstep, name) if name else nextstep.PREDICATES['build']
            for cls in (RuntimeError, AttributeError):
                def broken(*args, **kwargs):
                    sid = args[0] if phase == 'context' else args[0]['sid']
                    if sid == SID:
                        raise cls('do not expose event payload')
                    return target(*args, **kwargs)
                p = patch.object(nextstep, name, side_effect=broken) if name else patch.dict(nextstep.PREDICATES, build=broken)
                with p:
                    steps = self.steps(rows)
                failed, = [s for s in steps if s.item == SID]
                self.assertEqual((failed.state, failed.action, failed.owner), ('hold', None, 'executor'))
                self.assertTrue(failed.reason.startswith('undetermined:'))
                self.assertIn(SID, failed.reason)
                self.assertIn(cls.__name__, failed.reason)
                self.assertLessEqual(len(failed.reason), 300)
                self.assertTrue(failed.clears)
                self.assertEqual([s for s in steps if s.item != SID], expected)
                cases.append(dict(phase=phase, exception=cls.__name__, step=failed.__dict__))
        # Consume the actual computed Steps through the real launch loop.
        import loadgate
        launches = []
        for sid in ('L-spec-1894', 'L-spec-1896'):
            (self.root / 'worktrees/fixture' / sid.lower()).mkdir(parents=True)
            (self.root / 'content' / (sid + '.md')).write_text('AC1 [backend]: fixture.\n')
        def transport(argv):
            if argv[:2] == ['doit', 'dispatch']:
                launches.append(argv[4])
                return dict(code=0, stdout=json.dumps(dict(detached='L-builder-9009')), stderr='')
            return dict(code=0, stdout='/packet.md' if argv[:2] == ['doit', 'packet'] else
                        '0' if 'rev-list' in argv else 'master', stderr='')
        with patch.object(loadgate, 'begin', return_value=None), patch.object(ad, 'capacity', return_value=2):
            outcomes = tick.run_steps(rows, self.specs(rows), NOW, steps=steps, dispatcher=transport)
        self.assertEqual(launches, ['L-spec-1894', 'L-spec-1896'], outcomes)
        self.assertEqual(original, json.dumps(rows, sort_keys=True))
        for exc in (KeyboardInterrupt, SystemExit):
            with patch.object(nextstep, '_wave', side_effect=exc), self.assertRaises(exc):
                self.steps(rows)
        signed('AC3-exceptions', dict(cases=cases, healthy_neighbors= [s.__dict__ for s in expected],
                                     snapshot_pure=True, process_control_propagates=True,
                                     fake_launches=launches, outcomes=outcomes))


class FakeRunner(look.Runner):
    def clock(self): return 0
    def sleep(self, seconds): pass
    def ssh(self, *args): return '{}'
    def git(self, *args): return 'fixture'
    def tmux_capture(self, *args): return ''
    def crontab_text(self, *args): return ''
    def disk_usage(self, *args): return dict(total=100, used=10, free=90)
    def gh_run_list(self, *args): return '[]'


class RepeatFailureAlarmTests(Scratch):
    def look_pass(self, rows=None, now=NOW, dry_run=False):
        config = self.root / 'look.toml'
        config.write_text('ci = []\n[push]\nprojects = ["fixture"]\n')
        with patch.object(look.look_wallclock, 'run', return_value=dict(briefs=[], events=[])), \
             patch.object(look, '_send_operator', return_value=0), \
             patch.object(fold, 'NOW', now):
            return look.run(self.read() if rows is None else rows, now=now,
                            runner=FakeRunner(), root=self.root, toml_path=config,
                            crons_manifest=[], crond_dir=str(self.root / 'cron'),
                            repo=self.root, dry_run=dry_run)

    def ticks(self, markers, start=0, filename='L-tick-fixture.jsonl'):
        rows = []
        for i, marker in enumerate(markers, start):
            row = event('tick', subject='tick', actor='tick', minute=i,
                        autodispatch_error='different failure ' + str(i))
            if marker != 'legacy':
                row['autodispatch_pass_failed'] = marker
            rows.append(row)
        self.raw(rows, filename)

    def alarms(self, result):
        return [b for b in result['briefs'] if b['condition'] == 'autodispatch-repeat-failure']

    def test_threshold_reset_and_untrusted(self):
        cases = [[], [True], [True, True], [True, True, False, True],
                 [False, False, False], [True, True, 'legacy', True],
                 [True, True, 1, True], [True, True, 'true', True], [True, True, True]]
        results = []
        for markers in cases:
            with self.subTest(markers=markers), tempfile.TemporaryDirectory() as directory:
                saved = self.root
                self.root = Path(directory)
                (self.root / 'events').mkdir()
                with patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', self.root / 'events'):
                    self.ticks(markers)
                    # Forged actor on an unauthorized filename cannot add a tick.
                    forged = event('tick', actor='tick', minute=100, autodispatch_pass_failed=True)
                    forged['_src'] = 'L-builder-forged.jsonl:1'
                    self.raw([forged], 'L-builder-forged.jsonl')
                    rows = self.read()
                    alarms = self.alarms(self.look_pass(rows))
                    self.assertEqual(len(alarms), int(markers == [True, True, True]))
                    results.append(dict(markers=markers, alarms=len(alarms)))
                self.root = saved
        signed('AC4-threshold', dict(cases=results, filename_authority=True))

    def test_persistence_recovery_rearm_and_dry_run(self):
        self.ticks([True, True, True])
        self.raw([event('decision', minute=1)], 'L-operator-fixture.jsonl')
        rows = self.read()
        # Reversed input exercises timestamp/source ordering independent of input order.
        preview = self.alarms(self.look_pass(list(reversed(rows)), dry_run=True))
        self.assertEqual(len(preview), 1)
        self.assertFalse((self.root / 'events' / look.LEDGER_NAME).exists())
        self.assertFalse((self.root / 'look/state.json').exists())
        alarm, = self.alarms(self.look_pass())
        self.assertEqual((alarm['key'], alarm['owner']), ('autodispatch', 'executor'))
        self.assertEqual(alarm['reading']['consecutive'], 3)
        self.assertEqual(len(alarm['reading']['sources']), 3)
        with (self.root / 'look/state.json').open() as handle:
            self.assertIn('autodispatch-repeat-failure|autodispatch', json.load(handle)['open'])
        self.assertEqual(len([e for e in self.read() if e.get('condition') == 'autodispatch-repeat-failure']), 1)
        self.assertEqual(self.alarms(self.look_pass()), [])
        self.ticks([True], start=3)
        self.assertEqual(self.alarms(self.look_pass()), [])
        for marker in ('legacy', 1):
            self.ticks([marker], start=4)
            out = self.look_pass()
            self.assertFalse(any(a['ref'] == alarm['_src'] for a in out['answered']))
        self.ticks([False], start=5)
        out = self.look_pass()
        self.assertTrue(any(a['ref'] == alarm['_src'] for a in out['answered']))
        with (self.root / 'look/state.json').open() as handle:
            self.assertNotIn('autodispatch-repeat-failure|autodispatch', json.load(handle)['open'])
        self.assertTrue(any(e['type'] == 'brief-answered' and e['ref'] == alarm['_src'] for e in self.read()))
        self.ticks([True, True, True], start=6)
        self.assertEqual(self.alarms(self.look_pass()), [])  # existing cooldown
        rearmed, = self.alarms(self.look_pass(now=NOW + dt.timedelta(minutes=60)))
        self.assertNotEqual(rearmed['_src'], alarm['_src'])
        signed('AC4-lifecycle', dict(alarm=alarm, rearmed=rearmed, separate_handle_state=True,
                                    legacy_does_not_clear=True, dry_run_no_persistence=True,
                                    fourth_and_reloaded_pass_deduplicated=True))

    def test_timestamp_source_ties(self):
        rows = [event('tick', minute=0, actor='tick', autodispatch_pass_failed=True) for _ in range(3)]
        for i, row in enumerate(rows, 1):
            row['_src'] = 'L-tick-a.jsonl:' + str(i)
        recovery = dict(rows[0], _src='L-tick-z.jsonl:1', autodispatch_pass_failed=False)
        for sample in (rows + [recovery], [recovery] + list(reversed(rows))):
            self.assertEqual(self.alarms(self.look_pass(sample, dry_run=True)), [])
        # Numeric source line order must put line 10 after line 9.
        rows[-1]['_src'] = 'L-tick-a.jsonl:9'
        recovery['_src'] = 'L-tick-a.jsonl:10'
        self.assertEqual(self.alarms(self.look_pass([recovery] + rows, dry_run=True)), [])
        signed('AC4-order', dict(timestamp_ties=True, source_tie_break=True, numeric_lines=True))

    def test_real_tick_pass_marker(self):
        import carry, crons, install_sync, intake, notes, pane_resume, pr_closeout
        import push_origin, resources, reap_tmp, tree_cleanup, up, merge_queue, loadgate
        calls = []
        real_run = tick.run_steps
        def fake_transport(argv):
            calls.append(argv)
            if argv[:2] == ['doit', 'dispatch']:
                return dict(code=0, stdout=json.dumps(dict(detached='L-reviewer-9009')), stderr='')
            return dict(code=0, stdout='/packet.md' if argv[:2] == ['doit', 'packet'] else 'master', stderr='')
        steps = [nextstep.Step(sid, 'spec', 'checking', 'action', 'review', 'tick',
                              'fixture:1', 'condition met', None, NOW.isoformat())
                 for sid in ('L-spec-9001', 'L-spec-9002')]
        for step in steps:
            (self.root / 'worktrees/fixture' / step.item.lower()).mkdir(parents=True)
            (self.root / 'content' / (step.item + '.md')).write_text('AC1 [backend]: fixture.\n')
            self.raw([self.written(step.item)], 'L-operator-fixture.jsonl')
        def transport_with_item_failure(argv):
            if argv[:2] == ['doit', 'packet'] and 'L-spec-9001' in argv:
                return dict(code=1, stdout='', stderr='fixture packet failure')
            return fake_transport(argv)
        logs, records = [], []
        with ExitStack() as stack:
            for module, name, result in (
                (intake, 'run', {}), (notes, 'run', None), (pr_closeout, 'run', None),
                (install_sync, 'run', []), (push_origin, 'run', None),
                (carry, 'uncarried', []), (carry, 'sync_v4', []),
                (pane_resume, 'run', None), (crons, 'check', []),
                (look, 'heartbeat_missed', None), (look, 'emit_once', None),
                (up, 'ensure_relay', None), (merge_queue, 'enqueue_due', []),
                (ad, 'redispatch_dead', []), (resources, 'manifest_counts', {}),
                (resources, 'not_reapable_counts', {}), (reap_tmp, '_reap_recorded', []),
                (tree_cleanup, 'reap_worktrees', ([], [])), (loadgate, 'begin', None),
                (ad, 'capacity', 2), (tick, 'launch_precheck', None)):
                stack.enter_context(patch.object(module, name, return_value=result))
            stack.enter_context(patch.object(tick, '_log_lane_driver', side_effect=lambda lines, now: logs.extend(lines)))
            stack.enter_context(patch.object(tick, '_steps', return_value=steps))
            for exception in (RuntimeError('whole pass one'), AttributeError('whole pass two')):
                with patch.object(tick, 'run_steps', side_effect=exception):
                    tick._record()
                with tick.tick_path().open() as handle:
                    row = [json.loads(line) for line in handle if json.loads(line)['type'] == 'tick'][-1]
                self.assertIs(row['autodispatch_pass_failed'], True)
                records.append(row)
            with patch.object(tick, 'run_steps', return_value=[]):
                tick._record()
            with tick.tick_path().open() as handle:
                row = [json.loads(line) for line in handle if json.loads(line)['type'] == 'tick'][-1]
            self.assertIs(row['autodispatch_pass_failed'], False)
            records.append(row)
            def run_with_fake_transport(*args, **kwargs):
                return real_run(*args, dispatcher=transport_with_item_failure, **kwargs)
            with patch.object(tick, 'run_steps', side_effect=run_with_fake_transport):
                tick._record()
            with tick.tick_path().open() as handle:
                row = [json.loads(line) for line in handle if json.loads(line)['type'] == 'tick'][-1]
            self.assertIs(row['autodispatch_pass_failed'], False)
            self.assertIn('failed L-spec-9001 review:', row['autodispatch_error'])
            self.assertTrue(any(a[:2] == ['doit', 'dispatch'] and 'L-spec-9002' in a for a in calls), calls)
            records.append(row)
        self.assertEqual(sum(line.startswith('failed autodispatch:') for line in logs), 2)
        signed('AC4-tick-persistence', dict(ticks=records, fake_launches=calls, logs=logs))


if __name__ == '__main__':
    unittest.main()
