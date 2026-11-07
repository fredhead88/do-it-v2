#!/usr/bin/env python3
"""Scratch-only deadman contracts and guarded, full-entry-point look suites."""
from datetime import datetime, timedelta, timezone
import ast
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import merge_deadman as deadman
import merge_loop_state
import notification_policy
import tg_say

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)


def event(kind, age=90, **fields):
    return dict(type=kind, subject='L-spec-9001', project='do-it-v2',
                ts=(NOW - timedelta(minutes=age)).isoformat(), **fields)


def state(status='running', queued=True, failures=(), last_merge=NOW - deadman.WINDOW):
    return merge_loop_state.LoopState(status, ['L-spec-9001'] if queued else [],
                                     list(failures), last_merge, NOW)


def evidence(ac, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://merge-deadman/' + ac, status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)))


def hostile_failure():
    # Entirely synthetic payload, never a configured value or a live read.
    value = 'sk-' + 'aB09' * 16
    return merge_loop_state.Failure('L-spec-9001',
        'test_merge.py::test_gate failed\n\x00<b>token=' + value +
        '</b> /home/fixture/private.log <script>markup</script>', 4), value


class DeadmanContractTests(unittest.TestCase):
    def test_loop_state_trigger_and_clock(self):
        table = []
        with tempfile.TemporaryDirectory(prefix='deadman-clock-') as tmp:
            for status in ('running', 'rejecting', 'stopped', 'undetermined'):
                for queued in (False, True):
                    for ready in (False, True):
                        events = [event('queue-add', 60 * 24 * 60)] if ready else []
                        with patch.object(merge_loop_state, 'read', return_value=state(status, queued)) as read:
                            alarm = deadman.run(events, NOW, tmp, dry_run=True)
                        read.assert_called_once_with(root=Path(tmp), now=NOW)
                        due = queued or status == 'undetermined'
                        self.assertEqual(alarm is not None, due)
                        row = [status, 'nonempty' if queued else 'empty',
                               'present' if ready else 'absent', 'due' if alarm else 'suppress']
                        table.append(row)
                        print(','.join(row))
            for seconds, due in ((5399, False), (5400, True)):
                s = state(last_merge=NOW - timedelta(seconds=seconds))
                self.assertEqual(deadman.evaluate([], NOW, state=s) is not None, due)
            for project in deadman.PROJECTS:
                overrides = {p: NOW - timedelta(hours=4) for p in deadman.PROJECTS}
                overrides[project] = NOW - timedelta(minutes=89)
                self.assertIsNone(deadman.evaluate([], NOW, state=state(), merges=overrides))
            for value in (None, 'invalid', NOW + timedelta(days=1), datetime(2026, 11, 1)):
                alarm = deadman.evaluate([], NOW, state=state(last_merge=value))
                self.assertIsNone(alarm['since'])
                self.assertIn('duration is unknown', deadman.message(alarm, NOW))
                self.assertNotIn('90 minutes', deadman.message(alarm, NOW))
            for terminal in ('spec-written', 'build-started', 'spec-killed', 'queue-remove'):
                events = [event('queue-add'), event(terminal, 1)]
                self.assertIsNone(deadman.evaluate(events, NOW, state=state(queued=False)))
                self.assertIsNotNone(deadman.evaluate(events, NOW, state=state()))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        evidence('AC1', dict(table=table, window_seconds=5400, both_projects=True,
                             unknown_and_future='due, duration unknown', scratch_only=True))

    def test_cause_and_standard_message(self):
        failure, private = hostile_failure()
        failures = [failure] + [merge_loop_state.Failure(f'L-spec-{9002+i}',
                    f'test_gate_{i}.py failed', i + 1) for i in range(4)]
        rendered = []
        for status in ('rejecting', 'running', 'stopped', 'undetermined'):
            s = state(status, status != 'undetermined', failures if status == 'rejecting' else [])
            alarm = deadman.evaluate([event('tick', 1, error='unrelated ledger lock timeout')], NOW, state=s)
            text = deadman.message(alarm, NOW)
            self.assertEqual(notification_policy.validate_message(text), '[DOWN]')
            self.assertIn('status: ' + status, alarm['cause'])
            self.assertIn('You: No operator action is required. The Thinker owns investigation.', text)
            self.assertLessEqual(len(text), 3500)
            for forbidden in (private, '/home/', '<', '>', '\x00', 'ready=', 'UTC', '+00:00', 'ledger lock'):
                self.assertNotIn(forbidden, text)
                self.assertNotIn(forbidden, alarm['cause'])
            if status == 'rejecting':
                for expected in ('test_merge.py::test_gate', 'L-spec-9001', 'attempts: 4', '2 more failures'):
                    self.assertIn(expected, text)
                self.assertLess(text.index('L-spec-9001'), text.index('L-spec-9002'))
                self.assertNotIn('L-spec-9004', text)
            else:
                self.assertIn('No failure reason is available', text)
            if status == 'undetermined':
                self.assertIn('cause cannot be confirmed', text)
            rendered.append(text)
        for now, local in ((NOW, '2026-11-01 07:00'),
                           (datetime(2026, 7, 1, 5, tzinfo=timezone.utc), '2026-07-01 08:00')):
            self.assertIn('By: ' + local, deadman.message(alarm, now))
        giant = state(failures=[merge_loop_state.Failure('S' * 9000, 'test_safe.py ' + 'x ' * 9000, 2)] * 9)
        self.assertLessEqual(len(deadman.message(deadman.evaluate([], NOW, state=giant), NOW)), 3500)
        for diagnostic in ('test_safe.py You:', 'test_safe.py UTC', 'test_safe.py /var/log/fixture'):
            unusual = state(failures=[merge_loop_state.Failure('L-spec-9001', diagnostic, None)])
            predicted = deadman.evaluate([], NOW, state=unusual)
            notification_policy.validate_message(deadman.message(predicted, NOW))
        evidence('AC2', dict(messages=rendered, winter='07:00', summer='08:00', safe_and_bounded=True))

    def test_committed_alarm_and_repeat(self):
        import dispatch
        failure, _ = hostile_failure()
        roundtrips = []
        for reason in (failure.reason, 'quota exceeded', 'token unavailable', 'payment declined'):
            s = state('rejecting', failures=[merge_loop_state.Failure('L-spec-9001', reason, 3)])
            with tempfile.TemporaryDirectory(prefix='deadman-ledger-') as tmp, \
                 patch.object(merge_loop_state, 'read', return_value=s), \
                 patch.object(dispatch, 'now', return_value=NOW.isoformat()):
                root = Path(tmp)
                def delivered(*args, **kwargs):
                    self.assertTrue((root / 'events' / deadman.LEDGER).is_file())
                    return tg_say.Delivery('sent', 200)
                with patch.object(tg_say, 'deliver', side_effect=delivered) as send:
                    alarm = deadman.run([], NOW, root)
                    self.assertEqual(alarm['delivery'], dict(state='sent', http=200))
                    for later in (NOW, NOW + deadman.REPEAT - timedelta(seconds=1)):
                        self.assertIsNone(deadman.run([], later, root))
                    self.assertEqual(send.call_count, 1)
                    child = subprocess.run([sys.executable, '-B', '-c', FRESH_READER, str(root),
                                            str(Path(__file__).with_name('realm.html')), NOW.isoformat()],
                                           env={**os.environ, 'DOIT_ROOT': str(root),
                                                'PYTHONPATH': str(Path(__file__).parent.resolve())},
                                           text=True, capture_output=True, check=True)
                    result = json.loads(child.stdout)
                    self.assertEqual(result['owner'], 'thinker')
                    self.assertEqual(result['realm_owner'], 'thinker')
                    row = result['row']
                    self.assertEqual(row['type'], 'escalation-blocking')
                    self.assertEqual(row['subject'], deadman.SUBJECT)
                    self.assertEqual(row['kind'], deadman.KIND)
                    self.assertEqual(row['owner'], 'thinker')
                    for key in ('ready', 'asks', 'text', 'irreversible'):
                        self.assertNotIn(key, row)
                    roundtrips.append(result)
                    with patch.object(dispatch, 'now', return_value=(NOW + deadman.REPEAT).isoformat()):
                        self.assertIsNotNone(deadman.run([], NOW + deadman.REPEAT, root))
                    self.assertEqual(send.call_count, 2)
                    self.assertEqual(len((root / 'events' / deadman.LEDGER).read_text().splitlines()), 2)
        with tempfile.TemporaryDirectory(prefix='deadman-refusal-') as tmp, \
             patch.object(merge_loop_state, 'read', return_value=state()), \
             patch.object(dispatch, 'emit', return_value='fixture refusal'), \
             patch.object(tg_say, 'deliver') as send:
            with self.assertRaisesRegex(ValueError, 'refused'):
                deadman.run([], NOW, tmp)
            send.assert_not_called()
        evidence('AC3', dict(roundtrips=roundtrips, repeat_seconds=10800,
                             attempts_at_boundaries=[1, 1, 2], refused_append='no delivery'))

    def test_delivery_states(self):
        import dispatch
        outcomes = []
        for result in (tg_say.Delivery('sent', 200), tg_say.Delivery('held'),
                       tg_say.Delivery('failed', 503), tg_say.Delivery('refused', 403),
                       OSError('synthetic transport failure'), 0):
            with tempfile.TemporaryDirectory(prefix='deadman-delivery-') as tmp, \
                 patch.object(merge_loop_state, 'read', return_value=state()), \
                 patch.object(dispatch, 'now', return_value=NOW.isoformat()), \
                 patch.object(tg_say, 'deliver') as send:
                if isinstance(result, Exception):
                    send.side_effect = result
                else:
                    send.return_value = result
                alarm = deadman.run([], NOW, tmp, sender=lambda *a: self.fail('legacy sender used'))
                send.assert_called_once_with('merge-deadman', deadman.message(alarm, NOW),
                                             label='[DOWN]', root=Path(tmp))
                expected = result.state if isinstance(result, tg_say.Delivery) else 'failed'
                self.assertEqual(alarm['delivery']['state'], expected)
                self.assertEqual(alarm['delivery']['http'], result.http if isinstance(result, tg_say.Delivery) else None)
                self.assertTrue((Path(tmp) / 'events' / deadman.LEDGER).is_file())
                self.assertIsNone(deadman.run([], NOW, tmp))
                send.assert_called_once()
                outcomes.append(alarm['delivery'])
        # Real formatter and delivery consumer, with only the transport replaced.
        with tempfile.TemporaryDirectory(prefix='deadman-real-deliver-') as tmp, \
             patch.object(merge_loop_state, 'read', return_value=state()), \
             patch.object(dispatch, 'now', return_value=NOW.isoformat()), \
             patch.object(tg_say, '_transport', return_value=tg_say.Delivery('sent', 200)) as transport:
            self.assertEqual(deadman.run([], NOW, tmp)['delivery']['state'], 'sent')
            transport.assert_called_once()
            self.assertFalse((Path(tmp) / 'state').exists())
        evidence('AC4', dict(sender='merge-deadman', label='[DOWN]', outcomes=outcomes,
                             centralized_attempts=1, real_transport_calls=0))

    def test_dry_run_and_stamp(self):
        with tempfile.TemporaryDirectory(prefix='deadman-dry-') as tmp, \
             patch.object(merge_loop_state, 'read', return_value=state()), \
             patch.object(tg_say, 'deliver') as send:
            root = Path(tmp) / 'absent'
            alarm = deadman.run([], NOW, root, dry_run=True)
            self.assertIsNotNone(alarm)
            self.assertFalse(root.exists())
            send.assert_not_called()
        for value in (NOW, NOW.isoformat(), '2026-11-01T07:00:00+02:00', '2026-11-01T05:00:00Z'):
            self.assertEqual(deadman.stamp(value), NOW)
        for value in (None, 'invalid', '2026-11-01T05:00:00', datetime(2026, 11, 1), 4):
            self.assertIsNone(deadman.stamp(value))
        evidence('AC5', dict(dry_run='predicted; no writes or sends', stamp='UTC-aware preserved'))


# This process has closed the producer's ledger; read with the actual segmented reader.
FRESH_READER = r"""
import json, sys, re
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
import ledger_files, thinker_inbox as inbox
import merge_deadman, merge_loop_state, tg_say
rows = [json.loads(line) for line in ledger_files.read_text(Path(sys.argv[1]) / 'events', 'L-look-local.jsonl').splitlines()]
assert len(rows) == 1
row = rows[0]
row['_src'] = 'L-look-local.jsonl:1'
text = str(row['why']) + ' ' + str(row['default'])
assert inbox.OPERATOR_RE.search(text) is None
assert inbox.OPERATOR_RE.search(inbox.ask_of(row) + ' ' + str(row.get('default') or '')) is None
pattern = re.search(r'const OPERATOR_RE=/(.*)/i;', Path(sys.argv[2]).read_text()).group(1)
assert re.search(pattern, text, re.I) is None
with patch.object(inbox.fold, 'read_events', return_value=rows), patch.object(inbox, 'now', return_value=datetime.fromisoformat(sys.argv[3])):
    items = inbox.open_items()
assert len(items) == 1 and items[0]['owner'] == 'thinker'
clock = datetime.fromisoformat(sys.argv[3])
snapshot = merge_loop_state.LoopState('undetermined', [], [], None, clock)
with patch.object(merge_loop_state, 'read', return_value=snapshot), patch.object(tg_say, 'deliver', side_effect=AssertionError('restarted caller sent')) as send:
    assert merge_deadman.run([], clock, sys.argv[1]) is None
    send.assert_not_called()
assert len(ledger_files.read_text(Path(sys.argv[1]) / 'events', 'L-look-local.jsonl').splitlines()) == 1
print(json.dumps(dict(row=row, owner=items[0]['owner'], realm_owner='thinker')))
"""


class DeadmanTests(unittest.TestCase):
    def test_probe_uses_only_landed_first_parent_merges(self):
        with tempfile.TemporaryDirectory(prefix='deadman-git-') as tmp:
            repo = Path(tmp) / 'repos/do-it-v2'
            repo.mkdir(parents=True)
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                   'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
            def git(*args, when=NOW):
                return subprocess.check_output(['git', '-C', str(repo), *args],
                    env={**env, 'GIT_AUTHOR_DATE': when.isoformat(), 'GIT_COMMITTER_DATE': when.isoformat()}, stderr=subprocess.DEVNULL)
            git('init', '-q', '-b', 'main')
            old = NOW - timedelta(hours=3)
            git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-9001): landed', when=old)
            git('checkout', '-q', '-b', 'unmerged')
            git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-9002): not landed')
            self.assertEqual(deadman.stamp(deadman.probe_merges(tmp)['do-it-v2']), old)
            git('checkout', '-q', 'main')
            git('commit', '-q', '--allow-empty', '-m', 'factory(outage): landed direct fix')
            self.assertEqual(deadman.stamp(deadman.probe_merges(tmp)['do-it-v2']), NOW)

    def test_look_calls_alarm_outside_dispatch(self):
        import dispatch
        import look
        class StopAfterAlarm(Exception):
            pass
        with tempfile.TemporaryDirectory(prefix='deadman-look-') as tmp, \
             patch.object(look, 'load_toml', return_value={'thresholds': {'pass_budget_s': 90}}), \
             patch.object(merge_loop_state, 'read', return_value=state('rejecting')), \
             patch.object(dispatch, 'now', return_value=NOW.isoformat()), \
             patch.object(tg_say, 'deliver', return_value=tg_say.Delivery('sent', 200)) as send, \
             patch.object(look, '_send_operator', side_effect=AssertionError('legacy send')):
            test = self
            class Runner:
                def clock(self):
                    test.assertTrue(look._PASS_LOCKED)
                    row = json.loads((Path(tmp) / 'events' / deadman.LEDGER).read_text())
                    test.assertEqual(row['kind'], deadman.KIND)
                    send.assert_called_once()
                    raise StopAfterAlarm()
            with self.assertRaises(StopAfterAlarm):
                look.run([event('tick', 1, autodispatch_pass_failed=True)],
                         now=NOW, root=tmp, runner=Runner())
            evidence('AC5-look', dict(pass_lock=True, committed_before_budget=True,
                                      tick_failed=True, centralized_attempts=send.call_count))


ISOLATED_SUITES = {
    'test_look.py', 'test_tick.py', 'test_look_wallclock.py', 'test_reader_snapshot.py',
    'test_transcript_scan.py', 'test_autodispatch_fault_isolation.py',
}


def _install_look_guard():
    """sitecustomize entry: in every child, before the suite imports anything."""
    guard_dir = os.environ['DEADMAN_GUARD_DIR']
    count_path = Path(os.environ['DEADMAN_GUARD_COUNTS'])
    suite = Path(os.environ['DEADMAN_GUARD_SUITE']).resolve()
    assertions = {node.lineno for node in ast.walk(ast.parse(suite.read_text(encoding='utf-8')))
                  if isinstance(node, ast.Assert)}
    counts = dict(checks=0, transport_calls=0)
    original_start = unittest.TestResult.startTest
    def start_test(result, test):
        counts['checks'] += 1
        return original_start(result, test)
    unittest.TestResult.startTest = start_test
    def forbidden(*args, **kwargs):
        # Record synchronously: even a caught exception cannot hide the call.
        with count_path.open('a') as out:
            out.write(json.dumps(dict(checks=0, transport_calls=1)) + '\n')
        raise AssertionError('isolated look suite reached Telegram transport')
    merge_loop_state.read = lambda root=None, push_dir=None, now=None: state(queued=False, last_merge=None)
    tg_say.deliver = forbidden
    tg_say._transport = forbidden
    original_popen = subprocess.Popen
    class GuardedPopen(original_popen):
        def __init__(self, *args, **kwargs):
            env = dict(kwargs.get('env') if kwargs.get('env') is not None else os.environ)
            env.update({key: os.environ[key] for key in (
                'DEADMAN_GUARD_DIR', 'DEADMAN_GUARD_COUNTS', 'DEADMAN_GUARD_SUITE')})
            env.setdefault('DOIT_ROOT', os.environ['DOIT_ROOT'])
            env['PYTHONPATH'] = guard_dir + os.pathsep + env.get('PYTHONPATH', '')
            kwargs['env'] = env
            super().__init__(*args, **kwargs)
    subprocess.Popen = GuardedPopen
    def trace(frame, action, arg):
        if frame.f_code.co_filename != str(suite):
            return None
        if action == 'line' and frame.f_lineno in assertions:
            counts['checks'] += 1
        return trace
    sys.settrace(trace)
    def report():
        with count_path.open('a') as out:
            out.write(json.dumps(counts) + '\n')
    atexit.register(report)


def isolated_look_suite(path):
    suite = Path(path).resolve()
    if suite.parent != Path(__file__).resolve().parent or suite.name not in ISOLATED_SUITES:
        raise ValueError('unrecognized isolated look suite')
    with tempfile.TemporaryDirectory(prefix='deadman-isolated-') as tmp:
        root = Path(tmp)
        counts = root / 'counts.jsonl'
        counts.touch()
        (root / 'sitecustomize.py').write_text(
            'import sys\nsys.path.insert(0, ' + repr(str(suite.parent)) + ')\n'
            'try:\n from test_merge_deadman import _install_look_guard\n _install_look_guard()\n'
            'except BaseException:\n import os\n os._exit(98)\n')
        env = dict(os.environ, DOIT_ROOT=str(root / 'doit'), TMPDIR=str(root),
                   DEADMAN_GUARD_DIR=str(root), DEADMAN_GUARD_COUNTS=str(counts),
                   DEADMAN_GUARD_SUITE=str(suite), PYTHONPATH=str(root), PYTHONUNBUFFERED='1')
        result = subprocess.run([sys.executable, '-B', str(suite)], env=env)
        rows = [json.loads(line) for line in counts.read_text().splitlines()]
        checks = sum(row['checks'] for row in rows)
        calls = sum(row['transport_calls'] for row in rows)
        print(json.dumps(dict(suite=path, checks=checks, transport_calls=calls,
                              exit_code=result.returncode)), flush=True)
        return result.returncode or (1 if checks == 0 or calls else 0)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--isolated-look-suite':
        sys.exit(isolated_look_suite(sys.argv[2]))
    unittest.main()
