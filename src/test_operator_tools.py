#!/usr/bin/env python3
"""R9 operator scripts: fake tmux/curl, fixture state; never send a message."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import thinker_inbox as inbox

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'

TMUX = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) [ "${FAKE_EXISTING:-1}" = 1 ]; exit ;;
  display)
    case "${*: -1}" in
      '#{pane_id}') echo "${FAKE_PANE:-%84}" ;;
      '#{pane_dead}') echo "${FAKE_DEAD:-0}" ;;
      '#{pane_pid}') echo 999999999 ;;
      '#{window_width}') echo 120 ;;
    esac ;;
esac
'''


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='operator-tools-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'tmux-calls'
        self.executable('tmux', TMUX)
        self.executable('pgrep', '#!/bin/sh\nexit 1\n')
        # The only sender used by these tests: no networking, no real token.
        self.executable('curl', '#!/bin/sh\necho called >> "$FAKE_CURL_LOG"\nprintf "%s\\n" "${FAKE_HTTP:-200}"\n')
        self.env = dict(os.environ, PATH=str(self.bin) + ':/usr/bin:/bin', DOIT_ROOT=str(self.root),
                        FAKE_TMUX_LOG=str(self.log), TG_SENDER='fixture-sender',
                        FAKE_CURL_LOG=str(self.root / 'curl-calls'))

    def executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def run_script(self, name, *args, **env):
        result = subprocess.run(['bash', str(SCRIPTS / name), *args], env=self.env | env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_flow_bootstrap_and_reattach_persist_identity_and_keys(self):
        for exists in ('0', '1'):
            self.run_script('flow.sh', '--no-attach', FAKE_EXISTING=exists)
            self.assertEqual((self.root / 'state/thinker-pane').read_text(), '%84\n')
            calls = self.log.read_text()
            self.assertIn('bind-key -n S-Right select-pane -t :.+', calls)
            self.assertIn('bind-key -n S-Left select-pane -t :.-', calls)
        self.assertEqual((self.root / 'state/thinker-pane').stat().st_mode & 0o777, 0o600)

    def test_snapshot_refreshes_pane_without_session_resume_id(self):
        self.run_script('flow-snapshot.sh', FAKE_PANE='%91')
        self.assertEqual((self.root / 'state/thinker-pane').read_text(), '%91\n')
        self.assertTrue((self.root / 'state/flow-sessions.env').exists())
        # Dead or invalid observations cannot replace a proven identity.
        for env in ({'FAKE_DEAD': '1'}, {'FAKE_PANE': 'invalid'}):
            self.run_script('remember-thinker-pane.sh', **env)
            self.assertEqual((self.root / 'state/thinker-pane').read_text(), '%91\n')

    def test_sender_logs_first_line_time_sender_on_every_attempt(self):
        (self.root / 'telegram').mkdir()
        (self.root / 'telegram.token').write_text('fixture-token\n')
        (self.root / 'telegram/chat_id').write_text('fixture-chat\n')
        for code in ('200', '429'):
            result = self.run_script('tg-send.sh', 'NEEDS YOU: fixture\nprivate second line', FAKE_HTTP=code)
            self.assertEqual(result.stdout.strip(), code)
        log = self.root / 'logs/tg-send.jsonl'
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['first_line'], 'NEEDS YOU: fixture')
            self.assertEqual(row['sender'], 'fixture-sender')
            self.assertRegex(row['time'], r'^\d{4}-\d\d-\d\dT.*\+00:00$')
        self.assertNotIn('fixture-token', log.read_text())
        self.assertNotIn('fixture-chat', log.read_text())
        self.assertNotIn('second line', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_log_failure_prevents_transport_and_tokens_without_newline_work(self):
        (self.root / 'telegram').mkdir()
        (self.root / 'telegram.token').write_text('fixture-token')
        (self.root / 'telegram/chat_id').write_text('fixture-chat')
        self.run_script('tg-send.sh', 'fixture')
        calls = self.root / 'curl-calls'
        self.assertEqual(calls.read_text().splitlines(), ['called'])
        (self.root / 'logs/tg-send.jsonl').unlink()
        (self.root / 'logs/tg-send.jsonl').symlink_to(self.root / 'do-not-follow')
        result = subprocess.run(['bash', str(SCRIPTS / 'tg-send.sh'), 'fixture'], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text().splitlines(), ['called'])
        self.assertFalse((self.root / 'do-not-follow').exists())


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='thinker-inbox-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for key, value in {'STATE': str(self.root / 'state/inbox.json'),
                           'PANE_FILE': str(self.root / 'state/thinker-pane'),
                           'LOG': str(self.root / 'logs/inbox.log')}.items():
            p = patch.object(inbox, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.item = dict(src='question:1', subject='L-spec-fixture', kind=None, type='question',
                         ask='fixture question', owner='thinker')
        self.sent = []
        for key, value in {'open_items': lambda: [self.item], 'pane_idle': lambda p: True,
                           'type_into': lambda *a: True,
                           'telegram': lambda msg: self.sent.append(msg) or True}.items():
            p = patch.object(inbox, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(inbox.sys, 'argv', ['thinker-inbox'])
        p.start()
        self.addCleanup(p.stop)

    def test_missing_three_passes_once_and_recovery_resets(self):
        with patch.object(inbox, 'pane', return_value=None):
            inbox.main()
            inbox.main()
            self.assertEqual(self.sent, [])
            inbox.main()
            inbox.main()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('NEEDS YOU:', self.sent[0])
        self.assertIn('Recommendation:', self.sent[0])
        with patch.object(inbox, 'pane', return_value='%84'):
            inbox.main()
        self.assertEqual(inbox.load_state()['pane_misses'], 0)
        self.item = dict(self.item, src='question:2')
        with patch.object(inbox, 'pane', return_value=None):
            inbox.main()
            inbox.main()
            self.assertEqual(len(self.sent), 1)
            inbox.main()
        self.assertEqual(len(self.sent), 2)

    def test_failed_send_remains_eligible(self):
        with patch.object(inbox, 'pane', return_value=None), \
                patch.object(inbox, 'telegram', side_effect=[False, True]) as send:
            for _ in range(3):
                inbox.main()
            self.assertEqual(inbox.load_state()['tg'], {})
            inbox.main()
            self.assertIn('question:1', inbox.load_state()['tg'])
            self.assertEqual(send.call_count, 2)

    def test_stale_pointer_falls_back_and_resaves(self):
        Path(inbox.PANE_FILE).parent.mkdir()
        Path(inbox.PANE_FILE).write_text('%14\n')
        with patch.object(inbox, '_alive', side_effect=lambda p: p == '%84'), \
                patch.object(inbox.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '%84\n', '')):
            self.assertEqual(inbox.pane(), '%84')
        self.assertEqual(Path(inbox.PANE_FILE).read_text(), '%84\n')
        # A stale delivered item does not bypass the missing-pane threshold.
        old = (inbox.now() - inbox.dt.timedelta(hours=2)).isoformat()
        inbox.save_state({'delivered': {'question:1': old}, 'tg': {}})
        with patch.object(inbox, 'pane', return_value=None):
            inbox.main()
            inbox.main()
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
