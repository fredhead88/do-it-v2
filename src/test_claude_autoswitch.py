#!/usr/bin/env python3
"""Autoswitch regressions with scratch credentials, stub HTTP and no live panes."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tomllib
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


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts/claude-autoswitch.py'
LEGACY_COMMAND = ('/usr/bin/python3 /home/albert/.do-it/bin/claude-autoswitch.py '
                  '>/dev/null 2>&1 # operator 10-30: auto-switch Claude login at 99%, '
                  'back when reset (standing, not temp)')
LEGACY = '*/5 * * * * ' + LEGACY_COMMAND


def evidence(criterion, value):
    """Content-address the scratch backend result, without exposing credentials."""
    body = json.dumps(value, sort_keys=True)
    print(json.dumps(dict(url='cli://claude-autoswitch/' + criterion, status=0,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                          body_excerpt=body)))


class ScratchAutoswitch(unittest.TestCase):
    """Real file operations; only HTTP, tmux and switch process boundaries fake."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='autoswitch-isolated-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.root = Path(self.tmp.name) / 'runtime'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), DOIT_ROOT=str(self.root))
        with patch.dict(os.environ, self.env, clear=True):
            spec = importlib.util.spec_from_file_location('scratch_auto', SCRIPT)
            self.auto = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.auto)
        self.auto.DIR.mkdir(parents=True)
        (self.auto.DIR / 'current').write_text('1')
        for n in ('1', '2'):
            self.auto.save_json(self.auto.DIR / (n + '.json'), credentials('saved-' + n, 'refresh-' + n))
        self.auto.save_json(self.auto.LIVE, credentials('saved-1', 'refresh-1'))
        self.active = 'blind'
        self.rotate = False
        self.events = []
        for obj, name, action in (
                (self.auto.urllib.request, 'urlopen', self.http),
                (self.auto.subprocess, 'run', self.process),
                (self.auto.subprocess, 'Popen', self.switch),
                (os, 'system', AssertionError('unexpected shell operation')),
                (socket, 'create_connection', AssertionError('unexpected network connection'))):
            handle = patch.object(obj, name, side_effect=action)
            handle.start()
            self.addCleanup(handle.stop)

    def http(self, req, **kwargs):
        if req.full_url == 'https://platform.claude.com/v1/oauth/token':
            self.assertTrue(self.rotate)
            self.events.append('inactive refresh')
            return response(dict(access_token='rotated-access', refresh_token='rotated-refresh', expires_in=3600))
        self.assertEqual(req.full_url, 'https://api.anthropic.com/api/oauth/usage', 'outbound notification rejected')
        active = req.get_header('Authorization') == 'Bearer saved-1'
        self.events.append('active usage' if active else 'inactive usage')
        if active and self.active == 'blind':
            raise OSError('scratch unreadable')
        if not active and self.rotate and req.get_header('Authorization') == 'Bearer saved-2':
            raise OSError('scratch expired')
        return response(dict(five_hour=dict(utilization=99 if active and self.active == 'high' else 1),
                             seven_day=dict(utilization=2, resets_at='2099-01-01T00:00:00Z')))

    def process(self, argv, **kwargs):
        self.events.append('process:' + str(argv[0]))
        self.assertEqual(argv, ['tmux', 'list-panes', '-a', '-F', '#{pane_id}'],
                         'unexpected process or notification transport')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def switch(self, argv, **kwargs):
        self.assertEqual(argv, ['nohup', str(self.auto.SWITCH), 'switch', '2'],
                         'unexpected process or notification transport')
        self.events.append('switch:2')
        return object()

    def assert_silent(self):
        self.assertFalse(any('tg-' in e or 'curl' in e or 'telegram' in e.lower() for e in self.events))
        self.assertTrue(all(e in {'inactive refresh', 'active usage', 'inactive usage',
                                 'process:tmux', 'switch:2'} for e in self.events))


class AutoswitchNotificationTests(ScratchAutoswitch):
    def test_real_notifications_blind_rearming_and_switch_stay_local(self):
        self.auto.notify('scratch direct maintenance notice')
        for _ in range(4):
            self.auto.main()
        self.assertEqual(self.auto.LOG.read_text().count('auto-switch blind'), 1)
        self.active = 'low'
        self.auto.main()
        self.active = 'blind'
        self.auto.main()
        self.auto.main()
        self.assertEqual(self.auto.LOG.read_text().count('auto-switch blind'), 2)
        self.active = 'high'
        self.auto.main()
        self.auto.main()
        text = self.auto.LOG.read_text()
        self.assertIn('scratch direct maintenance notice', text)
        self.assertIn('login switched automatically', text)
        self.assertIn('cooling down', text)
        self.assertEqual(self.events.count('switch:2'), 1)
        self.assert_silent()
        evidence('AC1', dict(operations=self.events, blind_episodes=text.count('auto-switch blind'),
                             switches=self.events.count('switch:2'), cooldown='cooling down' in text,
                             direct_notice='scratch direct maintenance notice' in text))

    def test_transport_negative_assertion_is_sensitive(self):
        original = self.auto.notify
        with patch.object(self.auto, 'notify', side_effect=lambda message: self.auto.subprocess.run(['tg-send.sh', message])):
            with self.assertRaisesRegex(AssertionError, 'unexpected process or notification transport'):
                self.auto.notify('sensitivity probe')
        self.assertIs(self.auto.notify, original)
        self.events.clear()
        self.auto.notify('restored local logging')
        self.assert_silent()
        evidence('AC1-sensitivity', dict(sender_attempt_rejected=True, real_notify_restored=True))


class StateRoundTripTests(ScratchAutoswitch):
    def test_private_commits_independent_reader_and_restart_cooldown(self):
        a = self.auto
        self.rotate = True
        live_before = a.LIVE.read_bytes()
        # Compatible prior state must load, retain old readings and then update.
        a.save_json(a.STATE, dict(blind_account='1', blind_runs=0,
                                last_1=dict(session=10, weekly=20, weekly_reset='2099-01-01T00:00:00Z')))
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            a.main()
            a.main()
        blind = json.loads(a.STATE.read_text())
        self.assertEqual((blind['blind_account'], blind['blind_runs']), ('1', 2))
        self.assertEqual(blind['last_1']['weekly'], 20)
        self.assertIn('last_2', blind)
        # Independent interpreter opens committed state and credential files.
        reader = """
import json, os, pathlib, sys
home, root = pathlib.Path(os.environ['HOME']), pathlib.Path(os.environ['DOIT_ROOT'])
state_path = root / 'state/claude-autoswitch.json'
saved_path = home / '.do-it/private/claude-accounts/2.json'
state, saved = json.loads(state_path.read_text()), json.loads(saved_path.read_text())
expected_blind = int(sys.argv[1])
assert set(('blind_account','blind_runs','last_1','last_2')) <= state.keys()
assert state['blind_account'] == '1' and state['blind_runs'] == expected_blind
assert state['last_1']['session'] == (10 if expected_blind else 99)
assert state['last_2']['weekly'] == 2
if not expected_blind:
    assert state['last_switch']
assert saved['claudeAiOauth']['refreshToken'] == 'rotated-refresh'
assert all(p.stat().st_mode & 0o777 == 0o600 for p in (state_path, saved_path))
print('AC2 independent read: compatible keys, usage, rotation and 0600 verified')
"""
        # Use the original subprocess implementation: all child work has its own fakes.
        self.run_child(reader, '2')
        self.active = 'high'
        a.main()
        self.assertEqual(self.events.count('switch:2'), 1)
        self.run_child(reader, '0')
        restart = """
import importlib.util, os, pathlib, subprocess, sys
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('fixture', sys.argv[1])
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
def http(req, **kw):
    import io, json
    assert req.full_url == 'https://api.anthropic.com/api/oauth/usage'
    active = req.get_header('Authorization') == 'Bearer saved-1'
    return io.BytesIO(json.dumps(dict(five_hour=dict(utilization=99 if active else 1), seven_day=dict(utilization=2, resets_at='2099-01-01T00:00:00Z'))).encode())
with patch.object(a.urllib.request, 'urlopen', side_effect=http), patch.object(a.subprocess, 'Popen', side_effect=AssertionError('duplicate switch')), patch.object(a.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')):
    a.main()
assert 'cooling down' in a.LOG.read_text()
print('AC2 fresh production main: disk cooldown prevented duplicate switch')
"""
        self.run_child(restart, str(SCRIPT))
        self.assertEqual(a.LIVE.read_bytes(), live_before)
        self.assertEqual(a.STATE, self.root / 'state/claude-autoswitch.json')
        self.assertEqual(a.LOG, self.root / 'logs/claude-autoswitch.log')
        text = a.LOG.read_text() + capture.getvalue()
        for secret in ('saved-1', 'saved-2', 'refresh-1', 'refresh-2', 'rotated-access', 'rotated-refresh'):
            self.assertNotIn(secret, text)
        self.assert_silent()
        evidence('AC2', dict(independent_readers=2, fresh_main_cooldown=True,
                             private_modes='0600', live_unchanged=a.LIVE.read_bytes() == live_before,
                             blind_runs_committed=blind['blind_runs'], secrets_in_output=False))

    def run_child(self, code, *args):
        # subprocess is a shared module; temporarily restore only the parent runner.
        with patch.object(subprocess, 'run', ORIGINAL_RUN), patch.object(subprocess, 'Popen', ORIGINAL_POPEN):
            result = subprocess.run(['/usr/bin/python3', '-c', code, *args], env=self.env,
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        print(result.stdout.strip())


ORIGINAL_RUN, ORIGINAL_POPEN = subprocess.run, subprocess.Popen


class VersionedEntryTests(unittest.TestCase):
    def test_import_paths_and_unrelated_cwd_launches(self):
        with tempfile.TemporaryDirectory(prefix='autoswitch-launch-') as temp:
            temp = Path(temp)
            home, root, cwd = temp / 'home', temp / 'root', temp / 'cwd'
            cwd.mkdir()
            accounts = home / '.do-it/private/claude-accounts'
            accounts.mkdir(parents=True)
            (accounts / 'current').write_text('?')
            env = dict(os.environ, HOME=str(home), DOIT_ROOT=str(root))
            code = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('fixture', sys.argv[1])
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
home, root = pathlib.Path.home(), pathlib.Path(sys.argv[2])
assert a.STATE == root / 'state/claude-autoswitch.json'
assert a.LOG == root / 'logs/claude-autoswitch.log'
assert a.DIR == home / '.do-it/private/claude-accounts'
assert a.SWITCH == home / '.do-it/bin/claude-switch.sh'
assert a.LIVE == home / '.claude/.credentials.json'
"""
            result = subprocess.run(['/usr/bin/python3', '-c', code, str(SCRIPT), str(root)],
                                    cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            default_env = dict(env)
            del default_env['DOIT_ROOT']
            result = subprocess.run(['/usr/bin/python3', '-c', code, str(SCRIPT), str(home / '.do-it')],
                                    cwd=cwd, env=default_env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            link = temp / 'claude-autoswitch.py'
            link.symlink_to(SCRIPT)
            for target in (SCRIPT, link):
                result = subprocess.run(['/usr/bin/python3', str(target)], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertEqual((root / 'logs/claude-autoswitch.log').read_text().count('current account undetermined'), 2)
            self.assertFalse((root / 'state/claude-autoswitch.json').exists())
            self.assertFalse((home / '.do-it/logs').exists())
        evidence('AC3-launches', dict(repository_launch=True, symlink_launch=True,
                                      unrelated_cwd=True, home_root_split=True, default_root=True))

    def test_manifest_and_idempotent_complete_legacy_migration(self):
        import crons
        rows = tomllib.loads((REPO / 'crons.toml').read_text())['row']
        matches = [r for r in rows if r['name'] == 'claude-autoswitch']
        self.assertEqual(len(matches), 1)
        row = matches[0]
        self.assertEqual({k: row[k] for k in ('name','where','schedule','owner','project','path','command')},
                         dict(name='claude-autoswitch', where='user', schedule='*/5 * * * *',
                              owner='thinker', project='do-it-v2', path='/usr/bin/python3',
                              command='DOIT_ROOT={root} {doit} cron-run claude-autoswitch -- /usr/bin/python3 {repo}/scripts/claude-autoswitch.py >/dev/null 2>&1'))
        sig = re.compile(row['sig'])
        for line in (LEGACY, LEGACY_COMMAND, '/usr/bin/python3 /home/albert/.do-it/bin/claude-autoswitch.py',
                     f'/usr/bin/python3 {SCRIPT}', crons.print_line('claude-autoswitch')):
            self.assertIsNotNone(sig.search(line), line)
        for line in ('#' + LEGACY, '  #' + LEGACY, '# mention claude-autoswitch.py',
                     '/usr/bin/python3 /scratch/claude-autoswitch-helper.py', '/scratch/other.py'):
            self.assertIsNone(sig.search(line), line)
        with tempfile.TemporaryDirectory(prefix='autoswitch-cron-') as temp:
            temp = Path(temp)
            exe = temp / 'python'
            exe.write_text('#!/bin/sh\nexit 0\n')
            exe.chmod(0o755)
            fixture = dict(row, path=str(exe), command=f'{exe} cron-run claude-autoswitch -- {temp}/scripts/claude-autoswitch.py')
            manifest = temp / 'manifest.toml'
            manifest.write_text('[[row]]\n' + '\n'.join(k + ' = ' + json.dumps(v) for k,v in fixture.items()) + '\n')
            target = crons.print_line('claude-autoswitch', manifest)
            preserved = ['MAILTO=fixture', '#'+LEGACY, '  #'+LEGACY,
                         '1 * * * * /scratch/claude-autoswitch-helper.py',
                         '2 * * * * /scratch/unrelated  # preserve spacing  ']
            for already_tagged in (False, True):
                table = temp / 'crontab'
                table.write_text('\n'.join(preserved + [LEGACY] + ([target] if already_tagged else [])) + '\n')
                # Never fall back to a live crontab, even accidentally.
                with patch.object(crons, '_RUN', side_effect=AssertionError('real crontab forbidden')):
                    for _ in range(2):
                        crons.install('claude-autoswitch', manifest=manifest, crontab_path=table)
                        lines = table.read_text().splitlines()
                        active = [line for line in lines if not line.lstrip().startswith('#') and
                                  re.search(r'claude-autoswitch(?:\.py|\s+--)', line)]
                        self.assertEqual(active, [target], 'leftover active legacy launch')
                        self.assertEqual([line for line in lines if line != target], preserved)
                        self.assertEqual(table.read_text(), '\n'.join(preserved + [target]) + '\n')
                        evidence('AC3-migration', dict(active_lines=active, legacy_active=0,
                                                       preserved_lines=len(preserved), preexisting_tag=already_tagged))


class AutoswitchSourceTests(unittest.TestCase):
    def test_source_and_docstring_describe_local_logs(self):
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        doc = ast.get_docstring(tree).lower()
        self.assertNotRegex(source.lower(), r'tg-send|telegram')
        self.assertFalse(any(isinstance(node, ast.Name) and node.id == 'TG' and
                             isinstance(node.ctx, ast.Store) for node in ast.walk(tree)))
        self.assertIn('decisions', doc)
        self.assertIn('switch', doc)
        self.assertIn('blind', doc)
        self.assertRegex(doc, r'log(?:ged|s|ging)? locally|local logs')
        evidence('AC4', dict(local_log_docstring=True, stale_sender_references=0, tg_binding=False))


if __name__ == '__main__':
    unittest.main()
