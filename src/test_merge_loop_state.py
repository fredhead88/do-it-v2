"""Synthetic source fixtures; no running loop or release service is required."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import merge_loop_state as m

NOW = datetime(2026, 11, 6, 18, tzinfo=timezone.utc)
BASE = 'a' * 40


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='loop-reader-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.q = self.root / 'push/rq'
        self.q.mkdir(parents=True)
        self.put('push/rq/main.pid', '1234\n')
        self.put('push/rq/attention.txt', '')
        self.put('logs/releases.jsonl', '')
        self.proc = patch.object(m, '_process_status', return_value='running')
        self.proc.start()
        self.addCleanup(self.proc.stop)

    def put(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def batch(self, name='001.batch', specs='2017 2099'):
        return self.put('push/rq/' + name, BASE + ' ' + specs + '\n')

    def state(self, now=NOW):
        local = now.astimezone(m.IL)
        slots = {}
        for back in (1, 0):
            day = local.date() - timedelta(days=back)
            for label, hour in m.SLOT_HOURS.items():
                if datetime.combine(day, datetime.min.time(), m.IL).replace(hour=hour) <= now:
                    slots[f'{day}-{label}'] = {'status': 'done'}
        result = {'last_tick_utc': now.isoformat(), 'slots': slots, 'shas': {},
                  'builder_crons_pending': None}
        self.save_state(result)
        return result

    def save_state(self, value):
        self.put('logs/release-state.json', json.dumps(value))

    def read(self):
        return m.read(self.root, now=NOW)

    def git(self, *args, when=NOW, author=None):
        repo = self.root / 'repos/albert-scott'
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
                   GIT_AUTHOR_DATE=(author or when).isoformat(), GIT_COMMITTER_DATE=when.isoformat())
        return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def repo(self):
        (self.root / 'repos/albert-scott').mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        self.git('commit', '-q', '--allow-empty', '-m', 'initial', when=NOW - timedelta(days=3))


class LoopReadTests(Fixture):
    def test_queue_grammar_order_and_read_time(self):
        self.batch('002.batch', '2099 2018')
        self.batch('001.batch', '2017 2099')
        self.batch('current.bat', '2000 2017')
        self.put('push/rq/current', 'win-20261106-r1500\n')
        self.put('push/rq/win-20261106-r1500.log', 'running CI\n')
        state = self.read()
        self.assertEqual(state.status, 'running')
        self.assertEqual(state.queued, ['L-spec-2000', 'L-spec-2017', 'L-spec-2099', 'L-spec-2018'])
        self.assertEqual(state.read_at, NOW)
        self.assertEqual(state.failures, [])

    def test_malformed_batch_and_attention_preserve_known_failure(self):
        self.put('push/rq/attention.txt', '2017 13:58 CONFLICT with master\nbad row\n')
        self.put('push/rq/001.batch', 'not-a-base 2099\n')
        state = self.read()
        self.assertEqual(state.status, 'undetermined')
        self.assertEqual(state.failures[0].spec, 'L-spec-2017')
        for row in (BASE, BASE + ' 2099\n' + BASE + ' 2017', BASE + ' L-spec-2017', BASE + ' 12345'):
            with self.subTest(row=row):
                self.put('push/rq/001.batch', row)
                self.assertEqual(self.read().status, 'undetermined')

    def test_current_is_only_window_selector(self):
        self.batch('current.bat', '2017')
        self.assertEqual(self.read().status, 'undetermined')
        self.put('push/rq/current', 'win-20261106-r1500')
        self.assertEqual(self.read().status, 'undetermined')
        self.put('push/rq/win-20261106-r1500.log', 'CI still running')
        self.put('push/rq/win-20261106-r1900.log', 'exit=1\n')
        self.assertEqual(self.read().status, 'running')
        self.put('push/rq/current', '../escape')
        self.assertEqual(self.read().status, 'undetermined')

    def test_empty_queue_and_stop_request(self):
        self.assertEqual(self.read().queued, [])
        self.assertEqual(self.read().status, 'running')
        self.put('push/rq/stop', '')
        self.assertEqual(self.read().status, 'running')
        with patch.object(m, '_process_status', return_value='stopped'):
            self.assertEqual(self.read().status, 'stopped')
        for name in ('main.pid',):
            (self.q / name).unlink()
        self.assertEqual(self.read().status, 'undetermined')

    def test_rejecting_ci_and_cancellation(self):
        self.batch(specs='2017 2099')
        self.put('push/rq/001.batch.ci', 'ATTRIBUTED L-spec-2017\nexit=1\n')
        state = self.read()
        self.assertEqual(state.status, 'rejecting')
        self.assertEqual([f.spec for f in state.failures], ['L-spec-2017'])
        self.put('push/rq/001.batch.ci', 'exit=143\n')
        self.assertEqual(self.read().failures, [])

    def test_invalid_terminal_and_pid_records_are_unknown(self):
        self.batch()
        for terminal in ('exit=oops\n', 'exit=999999999999\n'):
            self.put('push/rq/001.batch.ci', terminal)
            self.assertEqual(self.read().status, 'undetermined')
        (self.q / '001.batch.ci').unlink()
        self.put('push/rq/main.pid', '9' * 5000)
        self.assertEqual(self.read().status, 'undetermined')

    def test_process_identity(self):
        self.proc.stop()
        for script in ('./rloop.run.sh', '/var/tmp/push/rloop.sh'):
            with patch.object(m.os, 'kill') as kill, patch.object(m, '_process_argv', return_value=['/bin/bash', script]):
                self.assertEqual(m._process_status(1234), 'running')
                kill.assert_called_once_with(1234, 0)
        with patch.object(m.os, 'kill', side_effect=ProcessLookupError):
            self.assertEqual(m._process_status(1234), 'stopped')
        with patch.object(m.os, 'kill'), patch.object(m, '_process_argv', return_value=['python', 'other.py']):
            self.assertEqual(m._process_status(1234), 'undetermined')
        for error in (PermissionError(), FileNotFoundError()):
            with patch.object(m.os, 'kill'), patch.object(m, '_process_argv', side_effect=error):
                self.assertEqual(m._process_status(1234), 'undetermined')

    def test_unread_sources_and_file_races(self):
        self.put('push/rq/attention.txt', '2017 13:58 CONFLICT\n')
        original = m._Snapshot.text
        for target in (self.q, self.q / 'attention.txt', self.q / 'main.pid'):
            with self.subTest(target=target):
                if target == self.q:
                    with patch.object(m._Snapshot, 'names', side_effect=PermissionError):
                        self.assertEqual(self.read().status, 'undetermined')
                else:
                    def unread(snapshot, path, **kwargs):
                        if path == target:
                            raise PermissionError
                        return original(snapshot, path, **kwargs)
                    with patch.object(m._Snapshot, 'text', unread):
                        self.assertEqual(self.read().status, 'undetermined')
        self.batch()
        def replace(snapshot, path, **kwargs):
            value = original(snapshot, path, **kwargs)
            if path.name == '001.batch':
                path.unlink()
                path.write_text(BASE + ' 2001\n')
            return value
        with patch.object(m._Snapshot, 'text', replace):
            self.assertEqual(self.read().status, 'undetermined')
        self.put('push/rq/current', 'win-20261106-r1500')
        def disappear(snapshot, path, **kwargs):
            if path.name == 'current':
                raise PermissionError
            return original(snapshot, path, **kwargs)
        with patch.object(m._Snapshot, 'text', disappear):
            self.assertEqual(self.read().status, 'undetermined')

    def test_last_merge_uses_committer_subject_and_first_parent(self):
        self.repo()
        landed = NOW - timedelta(hours=2)
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2017): window win-20261106-r1500', when=landed)
        self.git('checkout', '-q', '-b', 'side')
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2099): side')
        self.git('checkout', '-q', 'master')
        self.git('commit', '-q', '--allow-empty', '-m', 'ci: reconcile automatic base-red quarantine')
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2000): future', when=NOW + timedelta(hours=1))
        self.assertEqual(self.read().last_merge, landed)

    def test_naive_now_is_rejected(self):
        for fn in (lambda: m.read(self.root, now=NOW.replace(tzinfo=None)),
                   lambda: m.next_release(NOW.replace(tzinfo=None), self.root),
                   lambda: m.day_lists(NOW.replace(tzinfo=None), self.root)):
            with self.assertRaises(ValueError):
                fn()


class PlainLineTests(Fixture):
    def test_only_goal_sentence_and_hosted_link(self):
        self.put('content/L-spec-2017.md', '# Title\nIn plain English: Outside.\n\n## Goal\nOther text.\nIn plain English: Merges wait for review. Another sentence.\ncontinued text\n## Notes\nsecret elsewhere\n')
        self.assertEqual(m.plain_line('L-spec-2017', self.root), 'Merges wait for review.')
        self.put('content/L-spec-2017.md', '## Goal\nIn plain English: Check https://example.org/status.\n')
        self.assertEqual(m.plain_line('L-spec-2017', self.root), 'Check https://example.org/status.')

    def test_unsafe_and_missing_text(self):
        fallback = 'Plain-English description unavailable for L-spec-2017.'
        for text in ('No requested line.', 'In plain English: <b>Injected</b>.',
                     'In plain English: [click](file:///private).', 'In plain English: See http://localhost/path.',
                     'In plain English: See http://127.0.0.1/path.', 'In plain English: See /var/log/private.',
                     'In plain English: Token=INVENTED_SECRET_SENTINEL.',
                     'In plain English: Diagnostic sk-INVENTED000000000000000000.',
                     'In plain English: Hidden\u202etext.',
                     'In plain English: **Bold**.', 'In plain English: Bad\x01text.'):
            self.put('content/L-spec-2017.md', '## Goal\n' + text + '\n')
            with self.subTest(text=text):
                self.assertEqual(m.plain_line('L-spec-2017', self.root), fallback)
        for sid in ('../../private', '/etc/passwd', 'L-spec-2017\n', '2017', None):
            self.assertEqual(m.plain_line(sid, self.root), 'Plain-English description unavailable for unknown spec.')
        (self.root / 'content/L-spec-2017.md').unlink()
        self.assertEqual(m.plain_line('L-spec-2017', self.root), fallback)
        outside = self.put('outside', '## Goal\nIn plain English: Outside content.\n')
        (self.root / 'content/L-spec-2017.md').symlink_to(outside)
        self.assertEqual(m.plain_line('L-spec-2017', self.root), fallback)


class ReleaseClockTests(Fixture):
    def test_real_schema_and_pending_capture(self):
        state = self.state()
        self.assertEqual(m.next_release(NOW, self.root), '2026-11-07 03:00')
        state['slots']['2026-11-06-afternoon']['status'] = 'pending'
        self.save_state(state)
        self.assertIsNone(m.next_release(NOW, self.root))

    def test_freshness_inclusive_and_future(self):
        for minutes, expected in ((30, True), (30.001, False), (-1, False)):
            state = self.state()
            state['last_tick_utc'] = (NOW - timedelta(minutes=minutes)).isoformat()
            self.save_state(state)
            self.assertEqual(m.next_release(NOW, self.root) is not None, expected)

    def test_hold_missing_due_and_old_pending(self):
        state = self.state()
        self.put('state/release-hold', '')
        self.assertIsNone(m.next_release(NOW, self.root))
        (self.root / 'state/release-hold').unlink()
        with patch.object(m, '_hold', return_value=True):
            self.assertIsNone(m.next_release(NOW, self.root))
        state['slots'].pop('2026-11-05-afternoon')
        self.save_state(state)
        self.assertIsNone(m.next_release(NOW, self.root))
        state = self.state()
        state['slots']['2026-10-01-afternoon'] = {'status': 'pending'}
        self.save_state(state)
        self.assertIsNone(m.next_release(NOW, self.root))

    def test_invalid_state_matrix(self):
        for change in ({'slots': []}, {'slots': {'bad-key': {'status': 'done'}}},
                       {'slots': {'2026-11-06-afternoon': {'status': 'bad'}}},
                       {'last_tick_utc': '2026-11-06T18:00:00'}, {'last_tick_utc': 'garbage'}, {'shas': []}):
            state = self.state()
            state.update(change)
            self.save_state(state)
            self.assertIsNone(m.next_release(NOW, self.root))
        self.put('logs/release-state.json', '{')
        self.assertIsNone(m.next_release(NOW, self.root))
        (self.root / 'logs/release-state.json').unlink()
        self.assertIsNone(m.next_release(NOW, self.root))

    def test_slot_boundaries_rollover_and_dst(self):
        for now, expected in ((datetime(2026, 11, 6, 1, tzinfo=timezone.utc), '2026-11-06 15:00'),
                              (datetime(2026, 7, 6, 0, tzinfo=timezone.utc), '2026-07-06 15:00'),
                              (datetime(2026, 11, 6, 23, tzinfo=timezone.utc), '2026-11-07 03:00')):
            self.state(now)
            self.assertEqual(m.next_release(now, self.root), expected)
        state = self.state()
        state['slots']['2026-11-07-overnight'] = {'status': 'done'}
        self.save_state(state)
        self.assertEqual(m.next_release(NOW, self.root), '2026-11-07 15:00')


class DayListsTests(Fixture):
    def test_real_history_committer_day_and_resolved_failures(self):
        self.repo()
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2000): yesterday', when=NOW - timedelta(days=1))
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2017): window win-20261106-r1500',
                 when=NOW - timedelta(hours=2), author=NOW - timedelta(days=5))
        self.git('checkout', '-q', '-b', 'side')
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2099): unmerged')
        self.git('checkout', '-q', 'master')
        self.git('commit', '-q', '--allow-empty', '-m', 'ci: reconcile automatic base-red quarantine')
        self.put('push/rq/attention.txt', '2017 13:00 CI failed\n2099 13:10 CONFLICT\n2099 13:11 CONFLICT\n')
        for n in range(7):
            self.batch(f'{n:03}.batch.red', '2017')
            self.put(f'push/rq/{n:03}.batch.red.ci', 'exit=1\n')
        rows = m.day_lists(NOW, self.root)
        self.assertEqual([r.spec for r in rows.merged], ['L-spec-2017'])
        self.assertEqual([r.spec for r in rows.held], ['L-spec-2099'])
        self.assertIsNone(rows.held[0].age)
        self.assertEqual([r.spec for r in self.read().failures], ['L-spec-2099'])
        self.assertEqual(rows.merged[0].time.tzinfo, m.IL)
        self.put('push/rq/attention.txt', '')
        self.assertEqual(m.day_lists(NOW, self.root).held, [])

    def test_new_green_evidence_supersedes_red(self):
        self.put('push/rq/attention.txt', '2017 13:00 CI failed\n')
        self.batch('001.batch.red', '2017')
        self.put('push/rq/001.batch.red.ci', 'exit=1\n')
        self.batch('002.batch', '2017')
        self.put('push/rq/002.batch.ci', 'exit=0\n')
        self.assertEqual(self.read().failures, [])

    def test_older_green_does_not_resolve_newer_red_and_cancel_is_not_attempt(self):
        self.batch('001.batch', '2017')
        green = self.put('push/rq/001.batch.ci', 'exit=0\n')
        os.utime(green, (1, 1))
        self.put('push/rq/attention.txt', '2017 13:00 CI failed\n')
        self.batch('002.batch.red', '2017')
        self.put('push/rq/002.batch.red.ci', 'exit=1\n')
        self.batch('003.batch.red', '2017')
        self.put('push/rq/003.batch.red.ci', 'exit=143\n')
        failures = self.read().failures
        self.assertEqual([f.spec for f in failures], ['L-spec-2017'])
        self.assertIsNone(failures[0].attempts)
        self.put('push/rq/current', 'win-20261106-r1500')
        self.batch('current.bat', '2017')
        self.put('push/rq/win-20261106-r1500.log', 'gate-window: advanced master shipped=[L-spec-2017]\nexit=0\n')
        self.assertEqual(self.read().failures, [])

    def test_release_activity_outcomes_date_only_and_versions(self):
        outcomes = ['released', 'api-failed', 'api-rolled-back', 'site-failed', 'skipped', 'delayed', 'paused']
        rows = [{'date': '2026-11-06', 'outcome': outcome, 'sha': ''} for outcome in outcomes]
        rows[0]['tag'] = 'v2.3'
        rows[1]['sha'] = 'observed-sha'
        self.put('logs/releases.jsonl', '\n'.join(map(json.dumps, rows)) + '\n')
        result = m.day_lists(NOW, self.root).released
        self.assertEqual([r.outcome for r in result], outcomes)
        self.assertEqual([r.version for r in result[:3]], ['v2.3', 'observed-sha', 'unknown'])
        self.assertTrue(all(r.time is None and r.date == '2026-11-06' for r in result))
        capture = [dict(date='2026-11-06', outcome=o) for o in ['released'] + ['api-failed'] * 6]
        self.put('logs/releases.jsonl', '\n'.join(map(json.dumps, capture)))
        self.assertEqual(len(m.day_lists(NOW, self.root).released), 7)

    def test_independent_unread_and_empty_lists(self):
        self.repo()
        rows = m.day_lists(NOW, self.root)
        self.assertEqual((rows.merged, rows.released, rows.held), ([], [], []))
        self.put('logs/releases.jsonl', '{broken')
        rows = m.day_lists(NOW, self.root)
        self.assertIsNone(rows.released)
        self.assertEqual(rows.merged, [])
        self.assertEqual(rows.held, [])
        self.put('logs/releases.jsonl', json.dumps({'date': '2026-11-06', 'outcome': 'invented'}))
        self.assertIsNone(m.day_lists(NOW, self.root).released)
        self.put('logs/releases.jsonl', '')
        self.put('push/rq/attention.txt', 'malformed')
        self.assertIsNone(m.day_lists(NOW, self.root).held)
        self.assertEqual(m.day_lists(NOW, self.root).released, [])
        with patch.object(m, '_history', return_value=None):
            self.assertIsNone(m.day_lists(NOW, self.root).merged)
        with patch.object(m.Path, 'lstat', side_effect=PermissionError):
            self.assertTrue(m._hold(self.root / 'state/release-hold'))

    def test_future_instants_and_israel_day(self):
        rows = [dict(date='2026-11-06', ts=(NOW + timedelta(seconds=1)).isoformat(), outcome='released'),
                dict(date='2026-11-06', ts=NOW.isoformat(), outcome='api-failed'),
                dict(date='2026-11-05', outcome='released')]
        self.put('logs/releases.jsonl', '\n'.join(map(json.dumps, rows)))
        result = m.day_lists(NOW, self.root).released
        self.assertEqual([r.outcome for r in result], ['api-failed'])
        self.assertEqual(result[0].time.tzinfo, m.IL)

    def test_local_day_boundary_uses_committer_not_author(self):
        self.repo()
        # 22:30 UTC on November 5 belongs to November 6 in Israel.
        local_today = datetime(2026, 11, 5, 22, 30, tzinfo=timezone.utc)
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2018): midnight',
                 when=local_today, author=local_today - timedelta(days=1))
        self.git('commit', '-q', '--allow-empty', '-m', 'merge(L-spec-2099): future',
                 when=NOW + timedelta(seconds=1))
        self.assertEqual([row.spec for row in m.day_lists(NOW, self.root).merged], ['L-spec-2018'])


class ReadOnlyTests(Fixture):
    def test_repeated_reads_no_mutation_network_sends_or_fixture_escape(self):
        self.state()
        self.batch()
        self.put('content/L-spec-2017.md', '## Goal\nIn plain English: Review the work.\n')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(socket, 'socket', side_effect=AssertionError('network')), \
             patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 10)) as run:
            for _ in range(3):
                m.read(self.root, now=NOW)
                m.plain_line('L-spec-2017', self.root)
                m.next_release(NOW, self.root)
                m.day_lists(NOW, self.root)
            for call in run.call_args_list:
                argv = call.args[0]
                self.assertEqual(argv[:3], ['git', '-C', str(self.root / 'repos/albert-scott')])
                self.assertEqual(argv[3], 'log')
                self.assertLessEqual(call.kwargs['timeout'], 10)
                self.assertFalse(call.kwargs.get('shell', False))
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_default_paths_and_push_override(self):
        with patch.dict(os.environ, {'DOIT_ROOT': str(self.root)}):
            paths = m._paths(None, None)
            self.assertEqual(paths.content, self.root / 'content')
            self.assertEqual(paths.push, Path('/var/tmp/push'))
            self.assertEqual(paths.logs, Path('/var/log/albert-scott'))
        paths = m._paths(self.root, self.root / 'override')
        self.assertEqual(paths.push, self.root / 'override')
        self.assertEqual(paths.logs, self.root / 'logs')


if __name__ == '__main__':
    unittest.main()
