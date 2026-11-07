"""Hermetic Telegram poller regressions; no installed poller or real credentials."""
import ast
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / 'scripts/tg-poll.py'


def load():
    spec = importlib.util.spec_from_file_location('scratch_tg_poll', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(source):
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == 'api':
            assert isinstance(node.args[1], ast.Constant)
            assert node.args[1].value in ('getUpdates', 'getFile')
            calls.append(('api', node.args[1].value, node.lineno))
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'run':
            argv = node.args[0] if node.args else None
            if isinstance(argv, ast.List) and isinstance(argv.elts[0], ast.Call) and ast.unparse(argv.elts[0]) == 'str(SENDER)':
                env = next(k.value for k in node.keywords if k.arg == 'env')
                assert isinstance(env, ast.Dict)
                values = {k.value: v for k, v in zip(env.keys, env.values) if isinstance(k, ast.Constant)}
                assert ast.literal_eval(values['TG_SENDER']) == 'tg-poll'
                assert ast.literal_eval(values['TG_CLASS']) == 'reply'
                assert ast.unparse(values['TG_REPLY_TO']) == 'str(update_id)'
                calls.append(('sender', 'tg-poll/reply/update_id', node.lineno))
    assert {c[1] for c in calls if c[0] == 'api'} == {'getUpdates', 'getFile'}
    assert sum(c[0] == 'sender' for c in calls) == 1
    downloads = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and '/file/bot' in ast.unparse(n)]
    assert len(downloads) == 1
    fetch = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'fetch_attachment')
    assert downloads[0] in list(ast.walk(fetch))
    assert "api(tok, 'getFile'" in ast.unparse(fetch)
    return calls


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='poll-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.root = self.base / 'application'
        self.home.mkdir()
        self.root.mkdir()
        self.env = patch.dict(os.environ, HOME=str(self.home), DOIT_ROOT=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.p = load()
        sleep_patch = patch.object(self.p.time, 'sleep')
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        # Synthetic input remains private and is never included in traces.
        self.token = 'synthetic-private-input'
        self.p.private_write(self.root / 'telegram.token', self.token.encode())
        self.updates = []
        self.calls = []
        self.processes = []
        self.run_result = subprocess.CompletedProcess([], 0, '200\n', '')
        self.api_patch = patch.object(self.p, 'api', side_effect=self.fake_api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.run_patch = patch.object(self.p.subprocess, 'run', side_effect=self.fake_run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.popen_patch = patch.object(self.p.subprocess, 'Popen', side_effect=lambda cmd, **kw: self.processes.append(cmd))
        self.popen_patch.start()
        self.addCleanup(self.popen_patch.stop)

    def fake_api(self, tok, method, **params):
        self.assertEqual(tok, self.token)
        self.calls.append((method, params))
        if method == 'getUpdates':
            return {'ok': True, 'result': self.updates}
        self.assertEqual(method, 'getFile')
        return {'ok': True, 'result': {'file_path': 'documents/file.bin'}}

    def fake_run(self, cmd, **kw):
        self.processes.append((cmd, kw))
        if cmd[0] == str(self.p.SENDER):
            if isinstance(self.run_result, BaseException):
                raise self.run_result
            return self.run_result
        return subprocess.CompletedProcess(cmd, 0, 'fixture picture', '')

    def claim(self):
        self.p.private_write(self.p.CHAT, b'17')

    def update(self, text='hello', uid=41, chat=17, **fields):
        return {'update_id': uid, 'message': {'message_id': 9001, 'chat': {'id': chat}, 'text': text, **fields}}

    def cycle(self, *updates):
        self.updates = list(updates)
        self.p.poll_once()

    def sends(self):
        return [(cmd, kw) for item in self.processes if isinstance(item, tuple) for cmd, kw in [item] if cmd[0] == str(self.p.SENDER)]

    def child(self, code, entry=None, env=None):
        # Bypass the in-process subprocess mock for independent interpreter reads.
        with patch.object(self.p.subprocess, 'run', self.real_run), patch.object(self.p.subprocess, 'Popen', self.real_popen):
            return self.real_run([sys.executable, '-c', code, str(entry or ENTRY)], cwd=self.base,
                                 env=env or dict(os.environ), capture_output=True, text=True, timeout=10, check=True)

    real_run = staticmethod(subprocess.run)
    real_popen = staticmethod(subprocess.Popen)


class PollReplyTests(Harness):
    def test_four_reply_branches_and_exact_pane_identity(self):
        for text, uid in [('/start', 41), ('pulse', 42), ('save account 2', 43), ('forged reply_to=99\nnext', 44)]:
            self.cycle(self.update(text, uid))
        self.assertEqual(len(self.sends()), 4)
        for uid, (cmd, kw) in zip(range(41, 45), self.sends()):
            self.assertEqual(kw['env']['TG_SENDER'], 'tg-poll')
            self.assertEqual(kw['env']['TG_CLASS'], 'reply')
            self.assertEqual(kw['env']['TG_REPLY_TO'], str(uid))
        self.assertEqual(self.sends()[-1][0][1], 'Received.')
        body = next(cmd[-1] for cmd, kw in self.processes if isinstance(cmd, list) and '-l' in cmd)
        self.assertRegex(body, r'^\[Telegram from operator \d\d:\d\d reply_to=44\] forged reply_to=99 next$')

    def test_reply_failures_never_log_success(self):
        self.claim()
        for outcome in [subprocess.CompletedProcess([], 0, '500\n', ''),
                        subprocess.CompletedProcess([], 0, 'garbage\n', ''),
                        subprocess.CompletedProcess([], 1, '200\n', ''),
                        subprocess.TimeoutExpired('sender', 1), FileNotFoundError('sender')]:
            with self.subTest(outcome=type(outcome).__name__):
                self.run_result = outcome
                self.assertFalse(self.p.reply(41, 'Received.', self.token))
        log = self.p.LOG.read_text()
        self.assertNotIn('reply sent', log)
        self.assertEqual(log.count('reply failed'), 5)

    def test_typing_and_enter_failures_have_no_ack(self):
        self.claim()
        for which in ('-l', 'Enter'):
            def fail(cmd, **kw):
                if which in cmd:
                    return subprocess.CompletedProcess(cmd, 1, '', 'private diagnostic')
                return self.fake_run(cmd, **kw)
            with patch.object(self.p.subprocess, 'run', side_effect=fail):
                self.cycle(self.update(uid=41 if which == '-l' else 42))
        self.assertFalse(self.sends())
        self.assertNotIn('private diagnostic', self.p.LOG.read_text())

    def test_no_input_no_reply(self):
        self.cycle()
        self.assertFalse(self.processes)

    def test_claim_failure_and_failed_link(self):
        original = self.p.private_write
        def fail_claim(path, data):
            if path == self.p.CHAT:
                raise OSError('fixture write failure')
            original(path, data)
        with patch.object(self.p, 'private_write', side_effect=fail_claim):
            with self.assertRaises(OSError):
                self.cycle(self.update('/start'))
        self.assertFalse(self.sends())
        self.run_result = subprocess.CompletedProcess([], 0, '503\n', '')
        self.cycle(self.update('/start', 42))
        self.assertEqual(self.p.CHAT.read_text(), '17')
        self.assertEqual(stat.S_IMODE(self.p.CHAT.stat().st_mode), 0o600)
        self.assertNotIn('reply sent', self.p.LOG.read_text())

    def test_actual_shared_sender_bootstrap(self):
        bindir = self.base / 'bin'
        bindir.mkdir()
        curl = bindir / 'curl'
        curl.write_text('#!/bin/sh\ncat >/dev/null\nprintf "%s\\n" "${FIXTURE_HTTP:-200}"\n')
        curl.chmod(0o700)
        for code, uid in [('200', 41), ('503', 42)]:
            self.p.CHAT.unlink(missing_ok=True)
            with patch.dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'], FIXTURE_HTTP=code):
                with patch.object(self.p.subprocess, 'run', self.real_run), patch.object(self.p.subprocess, 'Popen', self.real_popen):
                    self.cycle(self.update('/start', uid))
            self.assertEqual(self.p.CHAT.read_text(), '17')
        rows = [json.loads(line) for line in (self.root / 'logs/tg-send.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in rows], ['attempt', 'result', 'attempt', 'result'])
        self.assertEqual([r['http'] for r in rows if r['event'] == 'result'], [200, 503])
        self.assertEqual([r['reply_to'] for r in rows], ['41', '41', '42', '42'])
        self.assertTrue(all(r['sender'] == 'tg-poll' and r['class'] == 'reply' and r['label'] is None for r in rows))
        self.assertEqual(self.p.LOG.read_text().count('reply sent'), 1)
        print('shared sender bootstrap: committed private claim; HTTP 200 sent; HTTP 503 failed; reply ids 41/42')

    def test_source_shape_and_adversarial_mutations(self):
        source = ENTRY.read_text()
        print('call-site inventory:', json.dumps(inventory(source)))
        for mutated in [source.replace("api(tok, 'getUpdates'", "api(tok, method"),
                        source.replace("'TG_SENDER': 'tg-poll'", "'TG_SENDER': sender")]:
            with self.assertRaises((AssertionError, ValueError)):
                inventory(mutated)


class PollInputTests(Harness):
    def test_real_inbound_api_query_and_error_response(self):
        self.api_patch.stop()
        response = {'ok': True, 'result': []}
        with patch.object(self.p.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as http:
            self.assertEqual(self.p.api(self.token, 'getUpdates', offset=42, timeout=50), response)
            request_url = http.call_args.args[0]
            self.assertIn('/getUpdates?offset=42&timeout=50', request_url)
        with patch.object(self.p.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaises(ValueError):
                self.p.api(self.token, 'getUpdates', offset=42)

    def test_foreign_chat_isolation_all_input_kinds(self):
        self.claim()
        for i, kind in enumerate(('text', 'caption', 'photo', 'document', 'video', 'voice', 'audio')):
            data = {} if kind == 'text' else {kind: [{'file_id': 'f'}] if kind == 'photo' else {'file_id': 'f'}}
            if kind == 'caption':
                data = {'caption': 'pulse'}
            self.cycle(self.update('switch 1', 41 + i, chat=99, **data))
        self.assertFalse(self.processes)
        self.assertTrue(all(call[0] == 'getUpdates' for call in self.calls))

    def test_pulse_aliases_empty_failure_and_home_boundary(self):
        self.claim()
        for uid, alias in enumerate(('pulse', '/status', 'p!'), 41):
            self.cycle(self.update(alias, uid))
        helper = str(self.home / '.do-it/telegram/pulse.py')
        self.assertEqual(sum(isinstance(p, tuple) and helper in p[0] for p in self.processes), 3)
        for output, status, expected in [('', 0, 'pulse: no output'), ('unsafe', 1, 'Pulse failed.')]:
            def run(cmd, **kw):
                if helper in cmd:
                    return subprocess.CompletedProcess(cmd, status, output, 'secret error')
                return self.fake_run(cmd, **kw)
            with patch.object(self.p.subprocess, 'run', side_effect=run):
                self.cycle(self.update('pulse', 50 + status))
            self.assertEqual(self.sends()[-1][0][1], expected)
        with patch.object(self.p.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 1)):
            self.cycle(self.update('pulse', 52))
        self.assertNotIn('secret error', self.p.LOG.read_text())

    def test_actual_scratch_pulse_executable_stdout_only(self):
        self.claim()
        self.p.PULSE.parent.mkdir(parents=True)
        self.p.PULSE.write_text("print('Fixture picture at 14:30')\n")
        def run(cmd, **kw):
            if str(self.p.PULSE) in cmd:
                return self.real_run(cmd, **kw)
            return self.fake_run(cmd, **kw)
        with patch.object(self.p.subprocess, 'Popen', self.real_popen), patch.object(self.p.subprocess, 'run', side_effect=run):
            self.cycle(self.update('pulse'))
        self.assertEqual(self.sends()[0][0][1], 'Fixture picture at 14:30')
        self.assertFalse((self.home / '.do-it/telegram/chat_id').exists())

    def test_bounded_command_grammar_literal_text(self):
        self.claim()
        self.cycle(self.update('switch account 1'))
        self.assertIn([str(self.home / '.do-it/bin/claude-switch.sh'), 'switch', '1'], self.processes)
        for uid, text in enumerate(('save2', 'save account 1', 'switch 3', 'save 2; touch /sentinel', '$(touch /sentinel)'), 42):
            self.cycle(self.update(text, uid))
        bodies = [item[0][-1] for item in self.processes if isinstance(item, tuple) and '-l' in item[0]]
        self.assertTrue(any('$(touch /sentinel)' in b for b in bodies))
        self.assertTrue(any('switch 3' in b for b in bodies))
        self.assertTrue(all(not kw.get('shell') for p in self.processes if isinstance(p, tuple) for cmd, kw in [p]))

    def test_untrusted_helper_output_and_safe_diagnostics(self):
        self.claim()
        for value in (self.token, 'https://api.telegram.org/botprivate/sendMessage', '/home/private/key',
                      '2026-11-07T12:00:00Z', '2026-11-07T12:00:00+00:00', 'password=sentinel', 'token: sentinel',
                      'failed:/private/key', 'postgresql://user:sentinel@host/db', 'Bearer sentinel',
                      'https://user:sentinel@example.org/status',
                      '{"api_key": "synthetic-private-value"}', "{'password': 'synthetic-private-value'}"):
            safe = self.p.safe_reply(value, self.token)
            self.assertNotIn(value, safe)
        self.assertEqual(self.p.safe_reply('See https://example.org/status', self.token), 'See https://example.org/status')
        def unsafe(cmd, **kw):
            if cmd[0] == str(self.p.SENDER):
                return self.fake_run(cmd, **kw)
            return subprocess.CompletedProcess(cmd, 0, self.token + ' /home/private/key 12:30Z', '')
        with patch.object(self.p.subprocess, 'run', side_effect=unsafe):
            self.cycle(self.update('pulse'))
            self.cycle(self.update('save 1', 42))
        for cmd, kw in self.sends():
            self.assertNotIn(self.token, cmd[1])
            self.assertNotIn('/home/private', cmd[1])
            self.assertNotIn('12:30Z', cmd[1])
        self.assertNotIn(self.token, self.p.LOG.read_text())

    def test_long_output_reports_omitted_count(self):
        text = '\U0001f600' * 5000
        rendered = self.p.safe_reply(text, self.token)
        self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 4096)
        self.assertIn('3100 characters omitted: reply length limit', rendered)

    def test_resume_lookup_and_fallback(self):
        self.assertEqual(self.p.thinker_pane(), 'flow:2.0')
        session = self.home / '.do-it/state/flow-sessions.env'
        session.parent.mkdir(parents=True)
        session.write_text('THINKER_ID=fixture-session\n')
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, '%9 123\n' if cmd[0] == 'tmux' else 'claude --resume fixture-session', '')
        with patch.object(self.p.subprocess, 'run', side_effect=run):
            self.assertEqual(self.p.thinker_pane(), '%9')
        with patch.object(self.p.subprocess, 'run', side_effect=OSError('unreadable')):
            self.assertEqual(self.p.thinker_pane(), 'flow:2.0')

    def test_israel_dst_boundary(self):
        before = datetime.datetime(2026, 10, 24, 22, 30, tzinfo=datetime.timezone.utc)
        after = datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.p.stamp(before), '01:30')
        self.assertEqual(self.p.stamp(after), '02:30')

    def test_all_attachment_kinds_caption_and_private_paths(self):
        self.claim()
        for uid, kind in enumerate(('photo', 'document', 'video', 'voice', 'audio'), 41):
            f = {'file_id': 'fixture', 'file_name': '../../x;$(bad).csv', 'file_size': 3}
            with patch.object(self.p.urllib.request, 'urlopen', return_value=io.BytesIO(b'csv')):
                self.cycle(self.update('', uid, caption='caption', **{kind: [f] if kind == 'photo' else f}))
        paths = list(self.p.INBOX.iterdir())
        self.assertEqual(len(paths), 5)
        self.assertTrue(all(p.parent == self.p.INBOX and p.read_bytes() == b'csv' and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths))
        self.assertEqual(stat.S_IMODE(self.p.INBOX.stat().st_mode), 0o700)
        self.assertEqual(len([c for c in self.calls if c[0] == 'getFile']), 5)
        bodies = [cmd[-1] for cmd, kw in self.processes if isinstance(cmd, list) and '-l' in cmd]
        self.assertTrue(all('caption [attached file saved:' in b for b in bodies))
        self.assertTrue(all(cmd[1] == 'Received.' for cmd, kw in self.sends()))

    def test_actual_limit_advertised_limit_and_network_failure(self):
        message = {'document': {'file_id': 'f', 'file_size': 1}}
        for count, saved in [(self.p.MAX_BYTES + 1, False), (self.p.MAX_BYTES, True)]:
            with patch.object(self.p.urllib.request, 'urlopen', return_value=io.BytesIO(b'x' * count)):
                note = self.p.fetch_attachment(self.token, message)
            self.assertEqual('not saved' not in note, saved)
        self.assertEqual(len(list(self.p.INBOX.iterdir())), 1)
        self.assertIn('not saved', self.p.fetch_attachment(self.token, {'document': {'file_id': 'f', 'file_size': self.p.MAX_BYTES + 1}}))
        self.claim()
        with patch.object(self.p.urllib.request, 'urlopen', side_effect=OSError(self.token + ' /private/secret')):
            self.cycle(self.update('', document={'file_id': 'f'}))
        self.assertEqual(len(list(self.p.INBOX.iterdir())), 1)
        self.assertNotIn(self.token, self.p.LOG.read_text())
        for path in ('../private', 'https://elsewhere/secret', 'file?query=secret'):
            with patch.object(self.p, 'api', return_value={'ok': True, 'result': {'file_path': path}}):
                with self.assertRaises(ValueError):
                    self.p.fetch_attachment(self.token, message)


READBACK = '''
import importlib.util, json, pathlib, sys
s=importlib.util.spec_from_file_location('poll',sys.argv[1]); p=importlib.util.module_from_spec(s); s.loader.exec_module(p)
print(json.dumps({'chat':p.CHAT.read_text(), 'offset':p.OFF.read_text(), 'files':[(f.name,f.read_bytes().hex(),f.stat().st_mode & 511) for f in p.INBOX.iterdir()]}))
'''


class PollStateRoundTripTests(Harness):
    def test_real_writes_independent_read_restart_and_consumed_offsets(self):
        self.cycle(self.update('/start'))
        with patch.object(self.p.urllib.request, 'urlopen', return_value=io.BytesIO(b'\x00private\xff')):
            self.cycle(self.update('caption', 42, document={'file_id': 'f'}), self.update('foreign', 43, chat=99))
        read = json.loads(self.child(READBACK).stdout)
        self.assertEqual((read['chat'], read['offset']), ('17', '44'))
        self.assertEqual([(r[1], r[2]) for r in read['files']], [(b'\x00private\xff'.hex(), 0o600)])
        code = '''
import importlib.util, json, sys
s=importlib.util.spec_from_file_location('poll',sys.argv[1]);p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
calls=[]
def api(tok, method, **kw):
    calls.append(kw);return {'ok':True,'result':[{'update_id':42,'message':{'chat':{'id':17},'text':'old'}},{'update_id':44,'message':{'chat':{'id':99},'text':'foreign'}}]}
p.api=api
p.reply=lambda *a: (_ for _ in ()).throw(AssertionError('unexpected reply'))
p.subprocess.run=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('unexpected process'))
p.poll_once()
print(json.dumps({'offset':p.OFF.read_text(),'requested':calls[0]['offset'],'chat':p.CHAT.read_text()}))
'''
        self.assertEqual(json.loads(self.child(code).stdout), {'offset': '45', 'requested': 44, 'chat': '17'})
        print('independent restart: private claim 17; consumed cursor 44 -> 45; attachment bytes/mode verified')

    def test_plain_file_compatibility_and_failed_offset_write(self):
        self.p.CHAT.parent.mkdir(exist_ok=True)
        self.p.CHAT.write_text('17\n')
        self.p.OFF.write_text('40\n')
        self.cycle(self.update(uid=41))
        self.assertEqual(self.calls[0][1]['offset'], 40)
        with patch.object(self.p, 'private_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.cycle(self.update(uid=42))
        self.assertEqual(self.p.OFF.read_text(), '42')
        self.assertEqual(len(self.sends()), 1)

    def test_failed_atomic_replace_leaves_previous_state(self):
        self.claim()
        with patch.object(self.p.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                self.p.private_write(self.p.CHAT, b'99')
        self.assertEqual(self.p.CHAT.read_text(), '17')
        self.assertEqual(sorted(p.name for p in self.p.CHAT.parent.iterdir()), ['chat_id'])


ENTRY_RUN = '''
import json, runpy, sys, urllib.request, subprocess
from unittest.mock import patch
from pathlib import Path
class Stop(BaseException): pass
calls=[]; launches=[]
def http(url, **kw):
    import io
    assert 'synthetic-private-input' in url
    calls.append(url.rsplit('/',1)[-1].split('?')[0])
    if len(calls)>1: raise Stop()
    return io.BytesIO(json.dumps({'ok':True,'result':[{'update_id':51,'message':{'chat':{'id':17},'text':'/start'}}]}).encode())
def run(cmd,**kw):
    launches.append([cmd, {k:kw['env'][k] for k in ('DOIT_ROOT','TG_SENDER','TG_CLASS','TG_REPLY_TO')}])
    return subprocess.CompletedProcess(cmd,0,'200\\n','')
try:
    with patch.object(urllib.request,'urlopen',side_effect=http),patch.object(subprocess,'run',side_effect=run):
        runpy.run_path(sys.argv[1],run_name='__main__')
except Stop: pass
s=runpy.run_path(sys.argv[1])
print(json.dumps({'sender':str(s['SENDER']),'root':str(s['D']),'pulse':str(s['PULSE']),'switch':str(s['SWITCH']),'session':str(s['SESSION']),'launches':launches,'calls':calls}))
'''


class PollEntryTests(Harness):
    def test_fresh_main_repo_and_symlink_from_foreign_cwd(self):
        link = self.base / 'installed-poll.py'
        link.symlink_to(ENTRY)
        for entry in (ENTRY, link):
            self.p.CHAT.unlink(missing_ok=True)
            self.p.OFF.unlink(missing_ok=True)
            row = json.loads(self.child(ENTRY_RUN, entry=entry).stdout)
            self.assertEqual(row['sender'], str(REPO / 'scripts/tg-send.sh'))
            self.assertEqual(row['root'], str(self.root))
            self.assertEqual(row['pulse'], str(self.home / '.do-it/telegram/pulse.py'))
            self.assertEqual(row['switch'], str(self.home / '.do-it/bin/claude-switch.sh'))
            self.assertEqual(row['session'], str(self.home / '.do-it/state/flow-sessions.env'))
            self.assertEqual(row['launches'][0][1]['TG_REPLY_TO'], '51')
            self.assertEqual(row['calls'], ['getUpdates', 'getUpdates'])
            self.assertEqual(self.p.OFF.read_text(), '52')
            self.assertEqual(self.p.CHAT.read_text(), '17')
            self.assertTrue(self.p.LOG.exists())
        self.assertFalse((self.home / '.do-it').exists())
        print('bounded main launches: repository and resolved symlink; foreign cwd; separate HOME and DOIT_ROOT')

    def test_default_root_without_doit_root(self):
        env = dict(os.environ)
        env.pop('DOIT_ROOT', None)
        root = self.home / '.do-it'
        root.mkdir()
        token = root / 'telegram.token'
        token.write_text(self.token)
        token.chmod(0o600)
        row = json.loads(self.child(ENTRY_RUN, env=env).stdout)
        self.assertEqual(row['root'], str(root))
        self.assertEqual((root / 'telegram/chat_id').read_text(), '17')
        self.assertEqual((root / 'telegram/offset').read_text(), '52')


if __name__ == '__main__':
    result = unittest.main(exit=False).result
    if result.wasSuccessful():
        for group, proof in (
            ('PollReplyTests', 'attributed shared replies, committed bootstrap, exact pane identity, failure refusal and AST inventory'),
            ('PollInputTests', 'accepted input matrix, HOME helpers, private bounded attachments and safe Israel-time output'),
            ('PollStateRoundTripTests', 'real private state and attachment writes, independent restart read-back and consumed cursor'),
            ('PollEntryTests', 'bounded repository and symlink launches from foreign cwd with distinct HOME/DOIT_ROOT and default root'),
        ):
            if len(sys.argv) == 1 or group in sys.argv[1:]:
                body = group + ': ' + proof + ' passed'
                print(json.dumps({'url': 'cli://test_tg_poll.py/' + group, 'status': 0,
                                  'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'body_excerpt': body}))
    sys.exit(0 if result.wasSuccessful() else 1)
