#!/usr/bin/env python3
"""Hermetic merge silence boundaries, causes, delivery and landed-git probes."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import merge_deadman as deadman

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)


def event(kind, age=90, **fields):
    return dict(type=kind, subject='L-spec-9001', project='do-it-v2',
                ts=(NOW - timedelta(minutes=age)).isoformat(), **fields)


class DeadmanTests(unittest.TestCase):
    def test_ninety_minutes_one_alert_and_no_work_no_alert(self):
        self.assertIsNone(deadman.evaluate([], NOW, merges={}))
        self.assertIsNone(deadman.evaluate([event('queue-add', 89)], NOW, merges={}))
        ready = [event('queue-add'), event('tick', 2, autodispatch_pass_failed=True)]
        alarm = deadman.evaluate(ready, NOW, merges={})
        self.assertIn('dispatch loop erroring', alarm['why'])
        with tempfile.TemporaryDirectory(prefix='deadman-') as tmp:
            import dispatch
            with patch.object(dispatch, 'now', return_value=NOW.isoformat()):
                sends = []
                for _ in range(3):
                    deadman.run(ready, NOW, tmp, merges={'do-it-v2': None},
                                sender=lambda *args: sends.append(args))
                rows = [json.loads(line) for line in (Path(tmp) / 'events' / deadman.LEDGER).read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['type'], 'escalation-blocking')
                self.assertEqual(rows[0]['subject'], deadman.SUBJECT)
                self.assertEqual(len(sends), 1)
                self.assertIn('dispatch loop erroring', sends[0][0])
                self.assertIsNotNone(deadman.evaluate(ready + rows, NOW + timedelta(hours=3), merges={}))
                self.assertIsNone(deadman.evaluate(ready + rows, NOW + timedelta(hours=3, seconds=-1), merges={}))

    def test_either_project_merging_suppresses_alarm(self):
        for project in deadman.PROJECTS:
            self.assertIsNone(deadman.evaluate([event('queue-add', 120)], NOW,
                merges={project: (NOW - timedelta(minutes=89)).isoformat()}))
        self.assertIsNone(deadman.evaluate([event('queue-add', 120), event('shipped', 5)], NOW, merges={}))
        self.assertIsNone(deadman.evaluate([event('queue-add', 120), event('build-started', 5)], NOW, merges={}))

    def test_causes_only_recent_diagnostics(self):
        for diagnostic, label in [(event('tick', 1, autodispatch_error='poisoned'), 'dispatch loop erroring'),
                (event('pane-stalled', 1, reason_class='quota'), 'seats paused for quota'),
                (event('base-red', 1, ids=['test_routing.py'], complete=True), 'base not releasable'),
                (event('window-refused', 1, reason='chain_lock busy'), 'chain_lock held'),
                (event('tick', 1, error='ledger lock timeout'), 'ledger lock')]:
            self.assertIn(label, deadman.evaluate([event('merge-gate-clean', 120), diagnostic], NOW, merges={})['cause'])
        stale = event('tick', 91, autodispatch_pass_failed=True)
        self.assertNotIn('dispatch loop erroring', deadman.evaluate([event('queue-add', 120), stale], NOW, merges={})['cause'])

    def test_dry_run_and_sender_failure_are_bounded(self):
        with tempfile.TemporaryDirectory(prefix='deadman-') as tmp:
            import dispatch
            ready = [event('queue-add', 120)]
            with patch.object(dispatch, 'now', return_value=NOW.isoformat()):
                alarm = deadman.run(ready, NOW, tmp, merges={'do-it-v2': None}, dry_run=True,
                                    sender=lambda *args: self.fail('dry-run sent'))
                self.assertIsNotNone(alarm)
                self.assertFalse((Path(tmp) / 'events').exists())
                with patch.object(deadman.subprocess, 'run', side_effect=OSError('sender unavailable')) as send:
                    deadman.run(ready, NOW, tmp, merges={'do-it-v2': None})
                    deadman.run(ready, NOW, tmp, merges={'do-it-v2': None})
                    self.assertEqual(send.call_count, 1)

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
             patch.object(deadman, 'probe_merges', return_value={'do-it-v2': None}), \
             patch.object(dispatch, 'now', return_value=NOW.isoformat()), \
             patch.object(look, '_send_operator') as send:
            runner = type('Runner', (), {'clock': lambda self: (_ for _ in ()).throw(StopAfterAlarm())})()
            with self.assertRaises(StopAfterAlarm):
                look.run([event('queue-add', 120), event('tick', 1, autodispatch_pass_failed=True)],
                         now=NOW, root=tmp, runner=runner)
            send.assert_called_once()
            self.assertIn('dispatch loop erroring', send.call_args.args[0])
            self.assertTrue((Path(tmp) / 'events' / deadman.LEDGER).is_file())


if __name__ == '__main__':
    unittest.main()
