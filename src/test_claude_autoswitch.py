#!/usr/bin/env python3
"""Autoswitch regressions with scratch credentials, stub HTTP and no live panes."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('autoswitch', Path(__file__).resolve().parents[1] / 'scripts/claude-autoswitch.py')
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def credentials(access='expired', refresh='fixture-refresh'):
    return dict(claudeAiOauth=dict(accessToken=access, refreshToken=refresh, scopes=['user:profile']))


class AutoswitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='autoswitch-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, relative in (('DIR', 'accounts'), ('LIVE', 'live.json'),
                               ('STATE', 'state/auto.json'), ('LOG', 'logs/auto.log')):
            p = patch.object(auto, name, root / relative)
            p.start()
            self.addCleanup(p.stop)
        auto.DIR.mkdir()
        (auto.DIR / 'current').write_text('1')
        for n in ('1', '2'):
            path = auto.DIR / (n + '.json')
            path.write_text(json.dumps(credentials()))
            path.chmod(0o600)
        self.http = patch.object(auto.urllib.request, 'urlopen', side_effect=AssertionError('unexpected HTTP'))
        self.http.start()
        self.addCleanup(self.http.stop)
        self.proc = patch.object(auto.subprocess, 'Popen')
        self.popen = self.proc.start()
        self.addCleanup(self.proc.stop)
        p = patch.object(auto.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '200\n', ''))
        self.run = p.start()
        self.addCleanup(p.stop)

    def test_saved_refresh_and_rotated_credentials(self):
        calls = []
        def http(req, **kwargs):
            calls.append(req)
            if req.full_url.endswith('/token'):
                self.assertEqual(json.loads(req.data)['grant_type'], 'refresh_token')
                return response(dict(access_token='fresh-fixture', refresh_token='rotated-fixture', expires_in=3600))
            if req.get_header('Authorization') == 'Bearer expired':
                raise HTTPError(req.full_url, 401, 'expired', {}, None)
            return response(dict(seven_day=dict(utilization=20, resets_at='2026-11-08T00:00:00Z')))
        with patch.object(auto.urllib.request, 'urlopen', side_effect=http):
            self.assertEqual(auto.usage('2')['weekly'], 20)
        saved = auto.DIR / '2.json'
        self.assertEqual(json.loads(saved.read_text())['claudeAiOauth']['refreshToken'], 'rotated-fixture')
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(calls), 3)

    def test_active_live_token_and_matching_saved_account(self):
        auto.save_json(auto.LIVE, credentials('fresh-live'))
        with patch.object(auto, 'read_usage', return_value=dict(session=1, weekly=2)) as read:
            self.assertEqual(auto.usage('1', live=True)['weekly'], 2)
        self.assertEqual(read.call_args.args[0]['claudeAiOauth']['accessToken'], 'fresh-live')
        self.assertEqual(json.loads((auto.DIR / '1.json').read_text())['claudeAiOauth']['accessToken'], 'fresh-live')
        auto.save_json(auto.LIVE, credentials('other-live', 'different-account'))
        with patch.object(auto, 'read_usage', return_value=dict(session=1, weekly=2)):
            auto.usage('1', live=True)
        self.assertEqual(json.loads((auto.DIR / '1.json').read_text())['claudeAiOauth']['accessToken'], 'fresh-live')

    def test_active_unreadable_never_rotates_the_cli_refresh_token(self):
        auto.save_json(auto.LIVE, credentials('live-fixture'))
        before = (auto.DIR / '1.json').read_bytes()
        with patch.object(auto, 'read_usage', side_effect=OSError('unreadable')), \
             patch.object(auto, 'refresh', side_effect=AssertionError('active refresh token must remain owned by CLI')):
            self.assertIsNone(auto.usage('1', live=True))
        self.assertEqual((auto.DIR / '1.json').read_bytes(), before)

    def test_two_blind_passes_log_once_and_rearm(self):
        with patch.object(auto, 'usage', return_value=None), patch.object(auto, 'usage_paused', return_value=False):
            for _ in range(5):
                auto.main()
        self.assertEqual(self.run.call_count, 0)
        self.assertEqual(auto.LOG.read_text().count('auto-switch blind'), 1)
        with patch.object(auto, 'usage', return_value=dict(session=0, weekly=0)):
            auto.main()
        with patch.object(auto, 'usage', return_value=None):
            auto.main()
            auto.main()
        self.assertEqual(self.run.call_count, 0)
        self.assertEqual(auto.LOG.read_text().count('auto-switch blind'), 2)
        self.popen.assert_not_called()

    def test_unreadable_active_paused_switches_to_readable_account(self):
        with patch.object(auto, 'usage', side_effect=lambda n, **kw: None if n == '1' else dict(session=1, weekly=2)), \
             patch.object(auto, 'usage_paused', return_value=True):
            auto.main()
            auto.main()  # cooldown prevents launching another switch
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(self.popen.call_args.args[0][-2:], ['switch', '2'])

    def test_unreadable_or_exhausted_alternate_does_not_switch(self):
        for other in (None, dict(session=100, weekly=2)):
            with patch.object(auto, 'usage', side_effect=[None, other]), patch.object(auto, 'usage_paused', return_value=True):
                auto.main()
        self.popen.assert_not_called()

    def test_notification_failure_preserves_blind_state(self):
        self.run.side_effect = subprocess.TimeoutExpired('fixture', 60)
        with patch.object(auto, 'usage', return_value=None):
            for _ in range(3):
                auto.main()
        self.assertEqual(json.loads(auto.STATE.read_text())['blind_runs'], 3)
        self.assertEqual(self.run.call_count, 0)

    def test_pause_probe(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0, '%1\n', ''),
                               subprocess.CompletedProcess([], 0, "You've hit your limit · resets soon", '')]
        self.assertTrue(auto.usage_paused())
        self.run.side_effect = OSError('no tmux')
        self.assertFalse(auto.usage_paused())


if __name__ == '__main__':
    unittest.main()
