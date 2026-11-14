#!/usr/bin/env python3
"""Close-review regression reports and read-only production observation routes."""
import atexit
import contextlib
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

OBSERVING = '--live-l2-replay' in sys.argv or '--observe-tick-launch' in sys.argv
if not OBSERVING:
    _BOOT = tempfile.TemporaryDirectory(prefix='close-reviews-import-')
    atexit.register(_BOOT.cleanup)
    os.environ.update(DOIT_ROOT=_BOOT.name, HOME=str(Path(_BOOT.name) / 'home'),
                      DOIT_NO_POKE='1', V4_LEDGER_DIR=_BOOT.name + '/inbound-ledger',
                      V4_INBOX_DIR=_BOOT.name + '/inbox', V4_STAGING_DIR=_BOOT.name + '/staging')
    os.environ.pop('DOIT_PROJECT', None)
    os.environ.pop('DOIT_LEDGER_FILE', None)

import fold
import spawns
import close_reviews as cr

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CID = 'L-charter-9901'
SID = 'L-spec-9901'


def event(kind, seconds=-60, subject=CID, **fields):
    actor = ('charter-reviewer' if kind.startswith('charter-review-') else 'operator')
    return dict(type=kind, ts=(NOW + timedelta(seconds=seconds)).isoformat(),
                subject=subject, actor=actor, **fields)


def report(group, label, expected, actual):
    body = dict(label=label, expected=expected, actual=actual, paid_runner_calls=0)
    payload = json.dumps(body, sort_keys=True, default=str)
    print(json.dumps(dict(url='cli://close-reviews/' + group, status=0,
                         body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         body_excerpt=payload)))


class Fixtures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='close-reviews-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'repos/one').mkdir(parents=True)
        (self.root / 'repos/two').mkdir()
        self.charter = self.root / 'charter.md'
        self.charter.write_text('fixture charter\n')
        p = patch.object(fold, 'ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.events = []
        self.specs = {}
        self.calls = []
        self.candidate()

    def candidate(self, cid=CID, sid=SID, state='accepted', shipped=True, covers='none',
                  marker='l1-complete', filed=-600, project='one'):
        self.events.append(event('charter-filed', filed, cid, covers=covers,
                                 project=project, path=str(self.charter)))
        if marker:
            self.events.append(event(marker, -500, cid))
        evs = [event('spec-written', -550, sid, charter=cid)]
        if shipped:
            evs.append(event('shipped', -120, sid))
        self.events.extend(evs)
        self.specs[sid] = dict(id=sid, charter=cid, state=state, evs=evs)

    def start(self, cid=CID, spawn='L-charter-reviewer-9901', pid=123, seconds=-10,
              role='charter-reviewer'):
        e = event('spawn-started', seconds, cid, spawn=spawn, role=role, waiter_pid=pid)
        self.events.append(e)
        return e

    def receipt(self, lines, seconds=0):
        self.events.append(event('tick', seconds, 'tick', close_reviews_outcomes=lines))

    def runner(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['doit', 'packet']:
            return dict(code=0, stdout='/fixture/packet.md', stderr='')
        return dict(code=0, stdout=json.dumps(dict(detached=1000 + len(self.calls),
                    role='charter-reviewer', subject=argv[4], log='/fixture/log')), stderr='')

    def run_reviews(self, **kw):
        with patch.object(cr.os, 'getloadavg', return_value=(19.9, 0, 0)):
            return cr.run(self.events, self.specs, NOW, runner=self.runner, **kw)

    def check_due(self, label, expected, now=NOW):
        actual = cr.due(self.events, self.specs, now)
        self.assertEqual(actual, expected, label)
        report('due', label, expected, actual)


class DueTests(Fixtures):
    def test_finished_matrix(self):
        for state, shipped, wanted in [
                ('accepted', True, True), ('shipped-owed-evidence', True, True),
                ('shipped-owed-due', True, True), ('shipped-owed-expired', True, True),
                ('accepted', False, False), ('building', False, False),
                ('killed', False, True), ('dropped', False, True),
                ('closed-unbuilt', False, True), ('void', True, False)]:
            self.specs[SID]['state'] = state
            self.specs[SID]['evs'] = [e for e in self.specs[SID]['evs'] if e['type'] != 'shipped']
            self.events = [e for e in self.events if e['type'] != 'shipped']
            if shipped:
                e = event('shipped', -120, SID)
                self.specs[SID]['evs'].append(e)
                self.events.append(e)
            self.check_due(state + '-shipped=' + str(shipped), [CID] if wanted else [])
        self.specs.clear()
        self.check_due('empty', [])

    def test_markers_and_replan(self):
        for marker in (None, 'plan-written', 'cut-written', 'l1-complete'):
            for replan in (None, -501, -500, -499):
                self.events = [e for e in self.events if e['type'] not in
                               ('plan-written', 'cut-written', 'l1-complete', 'charter-replan')]
                if marker:
                    self.events.append(event(marker, -500))
                if replan is not None:
                    self.events.append(dict(event('charter-replan', replan), actor='tick',
                                            verdict_src='L-charter-reviewer-9901.jsonl:1',
                                            findings=[], uncovered=[]))
                wanted = marker in ('cut-written', 'l1-complete') and (replan is None or replan < -500)
                self.check_due(f'{marker}-replan={replan}', [CID] if wanted else [])

    def test_unauthorized_or_malformed_replans_cannot_block_review(self):
        valid = replan()
        for actor in ('builder', 'reviewer', 'executor', 'operator'):
            self.events.append(dict(valid, actor=actor))
            self.check_due('unauthorized-replan-'+actor, [CID])
            self.events.pop()
        malformed = dict(valid)
        malformed.pop('findings')
        self.events.append(malformed)
        self.check_due('malformed-replan-cannot-block-review', [CID])

    def test_close_reopen_and_verdicts(self):
        self.events.append(event('charter-closed', -50))
        for reopened, wanted in ((None, False), (-51, False), (-50, False), (-49, True)):
            self.events = [e for e in self.events if e['type'] != 'charter-reopened']
            if reopened is not None:
                self.events.append(event('charter-reopened', reopened))
            self.check_due('reopen=' + str(reopened), [CID] if wanted else [])
        self.events.append(event('charter-retracted', -1))
        self.check_due('retracted', [])

    def test_order_projects_void_mixture(self):
        self.candidate('L-charter-9900', 'L-spec-9900', state='dropped', shipped=False,
                       filed=-600, project='two', covers='G1')
        self.candidate('L-charter-9899', 'L-spec-9899', filed=-700)
        self.specs['void'] = dict(id='void', charter=CID, state='void', evs=[])
        self.check_due('oldest-tie-projects-shipped-plus-void', ['L-charter-9899', 'L-charter-9900', CID])
        self.specs['L-spec-9900']['charter'] = CID
        self.check_due('mixed-terminal-shipped', ['L-charter-9899', CID])

    def test_open_terminal_expired_paired(self):
        e = self.start()
        self.events.append(dict(e, type='build-started'))
        self.check_due('paired-open-same', [])
        self.events.append(event('spawn-done', spawn=e['spawn']))
        self.check_due('terminal', [CID])
        self.events = [r for r in self.events if r['type'] not in spawns.TERMINAL]
        for r in self.events:
            if r['type'] in spawns.START_TYPES:
                r['ts'] = (NOW - timedelta(hours=3)).isoformat()
        self.check_due('expired', [CID])
        self.start('L-charter-9902', spawn='L-charter-reviewer-9902')
        self.check_due('open-other-charter', [CID])
        with patch.object(cr.os, 'getloadavg', side_effect=AssertionError('pure due')):
            self.check_due('no-load-in-due', [CID])


class RetryTests(Fixtures):
    def test_not_complete_shipments(self):
        self.events += [event('charter-review-complete', -400),
                        event('charter-review-not-complete', -100)]
        for seconds, own, expected in [(-101, True, []), (-100, True, []),
                                        (-99, False, []), (-99, True, [CID])]:
            e = event('shipped', seconds, SID if own else 'L-spec-other')
            self.events.append(e)
            if own:
                self.specs[SID]['evs'].append(e)
            self.check_due(f'newest-not-complete-own={own}-shipment={seconds}', expected)
            self.events.remove(e)
            if own:
                self.specs[SID]['evs'].remove(e)
        self.events.append(event('l1-complete', -1))
        self.check_due('l1-only', [])

    def test_failures_and_boundaries(self):
        e = self.start(seconds=-30)
        # Terminal in the spawn's own file can omit the charter; join to start.
        failed = event('spawn-failed', -20, e['spawn'], spawn=e['spawn'])
        self.events.append(failed)
        for offset in (-1, 0, 1):
            now = fold.ts(failed['ts']) + timedelta(hours=24, seconds=offset)
            self.check_due('failure-24h' + str(offset), [] if offset < 0 else [CID], now)
        self.events.append(event('spawn-failed', -10, spawn=e['spawn']))
        self.check_due('newer-failure-resets', [], fold.ts(failed['ts']) + timedelta(hours=24))
        self.events = self.events[:-2]
        self.start('L-charter-other', 'L-charter-reviewer-9903')
        self.events.append(event('spawn-failed', -1, 'L-charter-other', spawn='L-charter-reviewer-9903'))
        self.events.append(event('spawn-failed', -1, spawn='L-builder-9904', role='builder'))
        self.check_due('unrelated-failures', [])  # original reviewer remains open
        self.events.append(event('spawn-done', spawn=e['spawn']))
        self.check_due('unrelated-failures-after-terminal', [CID])

    def test_100_passes_no_restart(self):
        for mode in ('child-failed', 'no-start', 'refusal', 'packet-failed', 'malformed'):
            initial_events = list(self.events)
            self.calls = []
            calls = self.runner
            if mode in ('refusal', 'packet-failed', 'malformed'):
                def calls(argv, mode=mode):
                    self.calls.append(argv)
                    if mode == 'packet-failed' or argv[:2] == ['doit', 'dispatch']:
                        return dict(code=0 if mode == 'malformed' else 1, stdout='{}', stderr='private')
                    return dict(code=0, stdout='/packet.md', stderr='')
            with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
                lines = cr.run(self.events, self.specs, NOW, runner=calls)
            self.assertEqual(sum(line.startswith('forked ') for line in lines),
                             int(mode in ('child-failed', 'no-start')), lines)
            self.assertEqual(len(self.calls), 1 if mode == 'packet-failed' else 2)
            self.receipt(lines)
            if mode == 'child-failed':
                self.events.append(event('spawn-failed', 1, spawn='L-charter-reviewer-9988'))
            count = len(self.calls)
            for n in range(100):
                at = NOW + timedelta(seconds=n + 2)
                self.check_due(f'{mode}-pass={n}-anchor={NOW.isoformat()}-forks='
                               f'{int(mode in ("child-failed", "no-start"))}-failures='
                               f'{sum(e["type"] == "spawn-failed" for e in self.events)}', [], at)
                lines = cr.run(self.events, self.specs, at, runner=calls)
                self.receipt(lines, n + 2)
                self.assertEqual(len(self.calls), count)
                self.assertFalse(any(e['type'].startswith('charter-review-') for e in self.events))
            report('retry', mode, dict(forks=mode in ('child-failed', 'no-start'), extra_calls=0),
                   dict(calls=count, failure_events=sum(e['type'] == 'spawn-failed' for e in self.events)))
            self.events = initial_events

    def test_delayed_start_and_cooldown_gate(self):
        lines = self.run_reviews()
        self.receipt(lines)
        self.check_due('pending', [], NOW + timedelta(seconds=1))
        pid = cr.receipts(self.events)[0]['pid']
        e = self.start(pid=pid, seconds=1)
        with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
            out = cr.run(self.events, self.specs, NOW + timedelta(seconds=2), runner=self.runner)
        self.assertTrue(any(line.startswith('observed-start ') for line in out), out)
        self.events.append(event('spawn-done', 2, spawn=e['spawn']))
        self.check_due('delayed-matched-terminal-replaces-no-start', [CID], NOW + timedelta(seconds=3))
        self.events.append(event('spawn-failed', 3, spawn=e['spawn']))
        self.events.append(event('shipped', 4, SID))
        self.check_due('shipment-cannot-bypass-cooldown', [], NOW + timedelta(seconds=5))
        self.check_due('expiry-no-new-shipment-required', [CID], NOW + timedelta(days=1, seconds=3))
        self.events.append(event('charter-review-not-complete', 5))
        self.check_due('expiry-still-needs-shipment-after-verdict', [], NOW + timedelta(days=1, seconds=3))


class RunTests(Fixtures):
    def test_capacity(self):
        original = list(self.events)
        self.candidate('L-charter-9902', 'L-spec-9902', filed=-500)
        both = list(self.events)
        for n in range(4):
            self.events = list(both)
            self.calls = []
            for i in range(n):
                self.start('L-charter-98' + str(i), spawn=f'L-charter-reviewer-98{i}')
            out = self.run_reviews()
            forked = [line.split()[1] for line in out if line.startswith('forked ')]
            self.assertEqual(forked, [CID, 'L-charter-9902'][:max(0, 2 - n)])
            self.assertEqual(len(self.calls), 2 * len(forked))
            report('run', 'capacity=' + str(n), max(0, 2 - n), forked)
        self.events = original

    def test_load_and_dry_run(self):
        for load in (19.9, 20, 20.1, float('nan'), float('inf'), OSError('unreadable')):
            self.calls = []
            kw = dict(side_effect=load) if isinstance(load, Exception) else dict(return_value=(load, 0, 0))
            with patch.object(cr.os, 'getloadavg', **kw):
                out = cr.run(self.events, self.specs, NOW, runner=self.runner)
            expected = isinstance(load, float) and load <= 20 or load == 20
            self.assertEqual(any(x.startswith('forked ') for x in out), expected, out)
            self.assertEqual(len(self.calls), 2 if expected else 0)
            report('run', 'load=' + str(load), expected, out)
        self.calls = []
        self.assertTrue(self.run_reviews(dry_run=True)[0].startswith('would-dispatch '))
        self.assertEqual(self.calls, [])

    def test_metadata_and_cli(self):
        for project in (None, '../escape', 'absent', 'two'):
            self.events[0]['project'] = project
            self.calls = []
            out = self.run_reviews()
            self.assertEqual(out[0].startswith('forked '), project == 'two', out)
            if project == 'two':
                self.assertEqual(self.calls[0], ['doit', 'packet', 'charter-reviewer', CID,
                                                '--charter', str(self.charter)])
                self.assertEqual(self.calls[1], ['doit', 'dispatch', '--detach', 'charter-reviewer',
                                  CID, '--packet', '/fixture/packet.md', '--project', 'two', '--charter', CID])
            else:
                self.assertEqual(self.calls, [])
            report('run', 'project=' + str(project), project == 'two', out)
        self.events[0]['path'] = str(self.root / 'missing')
        self.calls = []
        self.assertTrue(self.run_reviews()[0].startswith('failed '))
        self.assertEqual(self.calls, [])

    def test_invalid_responses_and_partial_forks(self):
        for packet, detached in [((1, ''), (0, '{}')), ((0, ''), (0, '{}')),
                ((0, '/p'), (1, '{}')), ((0, '/p'), (0, '{}')),
                ((0, '/p'), (0, '{"detached":true}')), ((0, '/p'), (0, 'private text'))]:
            def runner(argv):
                code, stdout = packet if argv[1] == 'packet' else detached
                return dict(code=code, stdout=stdout, stderr='private data')
            with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
                out = cr.run(self.events, self.specs, NOW, runner=runner)
            self.assertTrue(out[0].startswith('failed '), out)
            self.assertNotIn('private', str(out))
            report('run', 'invalid-response', 'failed', out)
        self.candidate('L-charter-9902', 'L-spec-9902', filed=-500)
        def partial(argv):
            if 'L-charter-9902' in argv:
                raise RuntimeError('private')
            return self.runner(argv)
        with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
            out = cr.run(self.events, self.specs, NOW, runner=partial)
        self.assertTrue(out[0].startswith('forked '), out)
        self.assertTrue(out[1].startswith('failed '), out)

    def test_reconcile_refused_and_pending_capacity(self):
        out = self.run_reviews()
        self.assertTrue(out[0].startswith('forked '), out)
        self.assertNotIn('dispatched', str(out))
        self.receipt(out)
        self.candidate('L-charter-9902', 'L-spec-9902', filed=-500)
        self.candidate('L-charter-9903', 'L-spec-9903', filed=-400)
        before = len(self.calls)
        out = self.run_reviews()
        self.assertEqual(sum(x.startswith('forked ') for x in out), 1, out)
        self.assertEqual(len(self.calls) - before, 2)
        self.events.append(event('spawn-failed', 1, spawn='L-charter-reviewer-9999'))
        self.calls = []
        with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
            out = cr.run(self.events, self.specs, NOW + timedelta(seconds=2), runner=self.runner)
        self.assertFalse(any(x.startswith('forked ' + CID + ' ') for x in out))

    def test_deduplicated_start_other_roles_and_first_failure(self):
        fork = self.run_reviews()
        self.receipt(fork)
        pid = cr.receipts(self.events)[0]['pid']
        # The writer's seconds clock may precede the receipt's microseconds.
        self.start(pid=pid, seconds=0)
        self.start('L-spec-98', 'L-builder-98', role='builder')
        self.candidate('L-charter-9902', 'L-spec-9902', filed=-500)
        self.calls = []
        out = self.run_reviews()
        self.assertEqual(sum(x.startswith('forked ') for x in out), 1, out)
        self.assertIn('open=1', next(x for x in out if x.startswith('forked ')))
        self.assertEqual(cr.occupied(self.events, NOW), 1)
        self.events = [e for e in self.events if e['type'] not in spawns.START_TYPES and e['type'] != 'tick']
        def first_fails(argv):
            if CID in argv:
                return dict(code=1, stdout='', stderr='private')
            return self.runner(argv)
        with patch.object(cr.os, 'getloadavg', return_value=(1, 0, 0)):
            out = cr.run(self.events, self.specs, NOW, runner=first_fails)
        self.assertTrue(out[0].startswith('failed ' + CID), out)
        self.assertTrue(out[1].startswith('forked L-charter-9902'), out)
        report('run', 'first-failure-does-not-reserve-seat', ['failed', 'forked'], out)

    def test_unexpected_later_error_retains_fork(self):
        self.candidate('L-charter-9902', 'L-spec-9902', filed=-500)
        with patch.object(cr.os, 'getloadavg', side_effect=[(1, 0, 0), RuntimeError('private')]):
            with self.assertRaises(RuntimeError) as raised:
                cr.run(self.events, self.specs, NOW, runner=self.runner)
        self.assertEqual(len(raised.exception.outcomes), 1)
        self.assertTrue(raised.exception.outcomes[0].startswith('forked ' + CID))
        self.assertNotIn('private', str(raised.exception))


class RoundTripTests(Fixtures):
    def test_frozen_replay_and_launch_observer(self):
        # The observer refuses a fork alone and joins a real own-ledger start.
        for e in self.events:
            if e['type'] == 'spec-written':
                e['charter'] = CID
        self.events.append(event('shipped', -10, 'L-spec-2157'))
        fork = self.run_reviews()
        self.receipt(fork, 3)
        self.events[-1]['_src'] = 'L-tick-local.jsonl:1'
        with self.assertRaisesRegex(ValueError, 'cannot-observe'):
            observe_launch(self.events, 'L-spec-2157')
        pid = cr.receipts(self.events)[0]['pid']
        start = self.start(pid=pid, seconds=1)
        start['_src'] = start['spawn'] + '.jsonl:1'
        observed = observe_launch(self.events, 'L-spec-2157')
        self.assertEqual((observed['subject'], observed['pid']), (CID, pid))
        start['waiter_pid'] += 1
        with self.assertRaisesRegex(ValueError, 'cannot-observe'):
            observe_launch(self.events, 'L-spec-2157')
        closed = [event('charter-filed', -600, covers='none'),
                  event('l1-complete', -500), event('sweep-fixpoint', -400),
                  event('charter-closed', -300, reason='historical')]
        for e in closed:
            e['actor'] = sorted(fold.EMITS[e['type']])[0]
        result = replay(closed)
        self.assertEqual(result['baseline_L2'], [CID])
        self.assertEqual(result['difference'], [])
        self.assertEqual(result['event_count'], len(closed))
        self.assertEqual(cr.due(self.events, self.specs, NOW), [])  # mismatched pid is pending
        report('observers', 'frozen-closed-replay-and-pid-join', [], result)

    def test_tick_real_append_and_fresh_reader(self):
        import tick
        import dispatch
        import carry
        import intake
        import notes
        import pr_closeout
        import install_sync
        import push_origin
        import pane_resume
        import look
        import up
        import autodispatch
        import merge_queue
        import tree_cleanup
        import reap_tmp
        marker = 'close-review-unique-roundtrip-marker'
        fork = self.run_reviews()
        with contextlib.ExitStack() as stack:
            for mod, attr, value in ((fold, 'EVENTS', self.root / 'events'),
                    (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root / 'events'),
                    (dispatch, 'CONTENT', self.root / 'content'), (fold, 'NOW', NOW)):
                stack.enter_context(patch.object(mod, attr, value))
            # Leave the production reader, fold, heartbeat writer and storage intact.
            for mod, attr, value in ((intake, 'run', {}), (notes, 'run', None),
                    (pr_closeout, 'run', None), (install_sync, 'run', []),
                    (push_origin, 'run', None), (carry, 'uncarried', []), (carry, 'sync_v4', []),
                    (pane_resume, 'run', None), (up, 'ensure_relay', 'fixture'), (up, 'ensure_planner', 'idle'),
                    (look, 'emit_once', None), (look, 'heartbeat_missed', None),
                    (tick, 'run_steps', []), (tick, 'launch_precheck', None),
                    (autodispatch, 'redispatch_dead', []), (merge_queue, 'enqueue_due', []),
                    (tree_cleanup, 'reap_worktrees', ([], [])), (reap_tmp, '_reap_recorded', [])):
                stack.enter_context(patch.object(mod, attr, return_value=value))
            hook = stack.enter_context(patch.object(cr, 'run', side_effect=RuntimeError(marker)))
            tick._record()
            self.assertEqual(hook.call_count, 1)
            self.assertEqual(hook.call_args.args[2], NOW)
            hook.side_effect = None
            hook.return_value = fork
            tick._record()
            hook.return_value = ['held ' + CID + ' reason=cooldown']
            tick._record()
            partial = RuntimeError('close-review-partial-roundtrip')
            partial.outcomes = fork + ['failed L-charter-9902 at=' + NOW.isoformat() + ' reason=dispatch']
            hook.side_effect = partial
            tick._record()
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONPATH=str(Path(__file__).parent))
        script = ('import fold,json; e=fold.read_events(use_cache=False); '
                  'print(json.dumps([x for x in e if x["type"]=="tick"]))')
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 4, rows)
        self.assertEqual(rows[0]['close_reviews_error'], marker)
        self.assertEqual(rows[1]['close_reviews_outcomes'], fork)
        self.assertNotIn('close_reviews_error', rows[2])
        self.assertEqual(rows[3]['close_reviews_outcomes'], partial.outcomes)
        self.assertEqual(rows[3]['close_reviews_error'], str(partial))
        self.assertTrue(all('lane' in r and 'relay' in r for r in rows))
        self.events += rows
        self.check_due('fresh-process-no-start-hold', [], NOW + timedelta(seconds=1))
        report('roundtrip', 'real-append-fresh-reader', 4, rows)


def previous_predicate(charter_evs):
    """Exact pre-change oracle; also used by the one-read production replay."""
    filed = [e for e in charter_evs if e['type'] == 'charter-filed']
    if filed:
        covers = filed[-1].get('covers')
        spelling = '' if covers is None else str(covers)
        if spelling.strip().lower() in ('none', 'null'):
            return False
    return fold.charter_review(charter_evs) != 'charter-review-complete'


def replay(events):
    candidate = fold.charter_review_owed
    try:
        fold.charter_review_owed = previous_predicate
        baseline = fold.fold(events)[1]
    finally:
        fold.charter_review_owed = candidate
    after = fold.fold(events)[1]
    ids = sorted(cid for cid, c in baseline.items() if c['state'] == 'L2-complete')
    return dict(event_count=len(events), input_digest=hashlib.sha256(
                json.dumps(events, sort_keys=True, default=str).encode()).hexdigest(), baseline_L2=ids,
                candidate_states={cid: after.get(cid, {}).get('state') for cid in ids},
                difference=[cid for cid in ids if after.get(cid, {}).get('state') != 'L2-complete'])


def observe_launch(events, spec):
    shipped = [e for e in events if e.get('type') == 'shipped' and e.get('subject') == spec]
    if not shipped:
        raise ValueError('cannot-observe: spec shipment unavailable')
    shipped_at = max(fold.ts(e['ts']) for e in shipped)
    for index, tick in enumerate(events):
        if tick['type'] != 'tick' or fold.ts(tick['ts']) <= shipped_at:
            continue
        prior = []
        for receipt in cr.receipts([tick]):
            if receipt['kind'] != 'forked' or receipt['at'] <= shipped_at:
                continue
            starts = [e for e in events if e['type'] == 'spawn-started'
                      and e.get('waiter_pid') == receipt['pid'] and e.get('subject') == receipt['subject']
                      and spawns._role(e) == 'charter-reviewer' and e.get('_src')
                      and fold.ts(e['ts']) > shipped_at]
            before = [e for e in events[:index] if fold.ts(e['ts']) <= receipt['at']]
            if prior:
                before.append(dict(type='tick', ts=tick['ts'], close_reviews_outcomes=prior))
            specs = fold.fold(before)[0]
            occupied = cr.occupied(before, receipt['at'])
            if (starts and receipt['subject'] in cr.due(before, specs, receipt['at'])
                    and receipt['load'] <= 20 and receipt['open'] < 2
                    and occupied == receipt['open']):
                return dict(subject=receipt['subject'], pid=receipt['pid'],
                            at=receipt['at'].isoformat(), load=receipt['load'], open=receipt['open'],
                            spawn=starts[0]['spawn'], start_src=starts[0]['_src'], tick_src=tick['_src'])
            prior.append(f"forked {receipt['subject']} pid={receipt['pid']} load={receipt['load']} "
                         f"open={receipt['open']} at={receipt['at'].isoformat()}")
    raise ValueError('cannot-observe: no attributable due tick launch')


def gap(cid=CID, seconds=-100, source='L-charter-reviewer-9901.jsonl:1', **fields):
    return event('charter-review-not-complete', seconds, cid, _src=source,
                 uncovered_requirement_ids=['R5'], **fields)


def replan(cid=CID, seconds=-90, source='L-charter-reviewer-9901.jsonl:1'):
    return dict(event('charter-replan', seconds, cid), actor='tick',
                verdict_src=source, findings=[], uncovered=['R5'])


class ReplanSelectionTests(Fixtures):
    def check_selection(self, label, expected):
        before = json.dumps(self.events, sort_keys=True)
        actual = cr.replans_due(self.events, NOW)
        self.assertEqual([e['subject'] for e in actual], expected, label)
        self.assertEqual(json.dumps(self.events, sort_keys=True), before)
        report('AC1', label, expected, actual)
        return actual

    def test_backlog_and_slots(self):
        self.events = []
        for n in range(18):
            cid = f'L-charter-{9800+n}'
            self.candidate(cid, f'L-spec-{9800+n}', filed=-900)
            self.events.append(gap(cid, -300+n, f'L-charter-reviewer-{9800+n}.jsonl:1'))
        self.check_selection('eighteen-backlog-zero-active', ['L-charter-9800', 'L-charter-9801'])
        self.events.append(replan('L-charter-9800', -90, 'L-charter-reviewer-9800.jsonl:1'))
        self.check_selection('one-active', ['L-charter-9801'])
        self.events.append(replan('L-charter-9801', -89, 'L-charter-reviewer-9801.jsonl:1'))
        self.check_selection('two-active', [])

    def test_ties_payload_and_open_spawn(self):
        self.candidate('L-charter-9900', 'L-spec-9900')
        self.events += [gap(CID, spawn='L-charter-reviewer-9901'),
                        gap('L-charter-9900', source='L-charter-reviewer-9900.jsonl:1')]
        self.start(seconds=-110)
        self.check_selection('open-reviewer-deferred', ['L-charter-9900'])
        self.events += [event('spawn-done', -80, spawn='L-charter-reviewer-9901'),
                        dict(event('audit-finding', -79, spawn='L-charter-reviewer-9901',
                                   text='same review'), actor='charter-reviewer'),
                        dict(event('audit-finding', -78, spawn='L-charter-reviewer-9999',
                                   text='unrelated'), actor='charter-reviewer')]
        rows = self.check_selection('closed-reviewer-complete-findings-and-charter-tie',
                                    ['L-charter-9900', CID])
        self.assertEqual(rows[1]['verdict_src'], 'L-charter-reviewer-9901.jsonl:1')
        self.assertEqual(rows[1]['uncovered'], ['R5'])
        self.assertEqual([f['text'] for f in rows[1]['findings']], ['same review'])
        self.events = [e for e in self.events if e.get('spawn') != 'L-charter-reviewer-9901']
        self.events += [gap(source='L-charter-reviewer-9901.jsonl:8'),
                        dict(event('audit-finding', -79, text='legacy',
                                   _src='L-charter-reviewer-9901.jsonl:9'), actor='charter-reviewer')]
        rows = self.check_selection('legacy-file-findings', ['L-charter-9900', CID])
        self.assertEqual(rows[1]['findings'][0]['text'], 'legacy')

    def test_exclusions(self):
        original = list(self.events)
        cases = [('future', [gap(seconds=1)]),
                 ('serviced', [gap(), replan(), event('l1-complete', -20)]),
                 ('superseded-complete', [gap(), event('charter-review-complete', -90)]),
                 ('held', [gap(), event('charter-replan-held', -90, why='waiting')]),
                 ('retracted', [gap(), event('charter-retracted', -90)]),
                 ('closed', [gap(), dict(event('charter-closed', -90), actor='tick')]),
                 ('unregistered-tied-filing', [gap(), event('charter-filed', -600, project='missing')]),
                 ('unregistered-newest-filing', [gap(), event('charter-filed', -90, project='missing')])]
        for label, rows in cases:
            self.events = original + rows
            self.check_selection(label, [])
        self.events = original + [dict(gap(), _src='')]
        with self.assertRaisesRegex(ValueError, 'verdict identity'):
            cr.replans_due(self.events, NOW)
        report('AC1', 'missing-verdict-identity-diagnostic', 'ValueError', 'ValueError')

    def test_waiting_release_competes_with_new_verdict(self):
        # A released younger verdict must not steal the next free slot from
        # an older verdict that has not received its event yet.
        self.events = []
        for n in range(4):
            cid = f'L-charter-{9800+n}'
            self.candidate(cid, f'L-spec-{9800+n}')
            self.events.append(gap(cid, -200+n, f'L-charter-reviewer-{9800+n}.jsonl:1'))
        for n in (0, 1, 3):
            self.events.append(replan(f'L-charter-{9800+n}', -100+n,
                                      f'L-charter-reviewer-{9800+n}.jsonl:1'))
        self.events += [event('charter-replan-held', -90, 'L-charter-9803', why='wait'),
                        event('charter-replan-released', -80, 'L-charter-9803', why='resume'),
                        event('l1-complete', -70, 'L-charter-9800')]
        self.check_selection('older-new-verdict-before-waiting-release', ['L-charter-9802'])
        self.events.append(replan('L-charter-9802', -60, 'L-charter-reviewer-9802.jsonl:1'))
        self.check_selection('new-event-keeps-slot-ahead-of-younger-release', [])
        state = cr.replan_state(self.events, NOW)
        self.assertTrue(state['L-charter-9802']['active'])
        self.assertFalse(state['L-charter-9803']['active'])


if __name__ == '__main__':
    if OBSERVING:
        try:
            if not fold.EVENTS.is_dir():
                raise ValueError('cannot-observe: production ledger unavailable')
            fold.PROJECT = None
            events = fold.read_events(use_cache=False)
            if not events:
                raise ValueError('cannot-observe: ledger empty')
            if '--live-l2-replay' in sys.argv:
                result = replay(events)
                print(json.dumps(result))
                sys.exit(1 if result['difference'] else 0)
            spec = sys.argv[sys.argv.index('--spec') + 1]
            print(json.dumps(observe_launch(events, spec)))
        except (ValueError, IndexError, OSError) as exc:
            print(str(exc))
            sys.exit(2)
    else:
        groups = {'AC1': ReplanSelectionTests}
        if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
            selector = sys.argv[1]
            if selector not in groups or len(sys.argv) != 2:
                print('unknown selector: ' + selector, file=sys.stderr)
                sys.exit(2)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(groups[selector])
            print(selector + ' criterion group')
            if not suite.countTestCases():
                sys.exit(2)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            sys.exit(0 if result.wasSuccessful() else 1)
        unittest.main()
