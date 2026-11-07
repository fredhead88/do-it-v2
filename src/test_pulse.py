"""Scratch-only pulse contracts; expected pictures are independent fixture literals."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
NOW = '2026-11-07T12:00:00+00:00'
ASKS = 'Approve the release window.\nChoose the next lane.\n'
BOARD = '## NEEDS YOU (2)\nfirst escalation\nsecond escalation\n\n## NEXT\n'
OMENS = 'WARN | shelf-stuck | Spec A needs review\nCRIT | pressure | Queue busy\nWARN | health | Disk watch\n'
GOLDEN = '''PULSE 14:00 Sat 07 Nov

WAITING ON YOU: 2
 1. Approve the release window.
 2. Choose the next lane.
(Thinker is handling 2 other escalation(s); not yours.)
STUCK: 1
 - Spec A needs review
WATCH:
 - Queue busy
 - Disk watch

PORTAL SPEED (goal: every page < 2s)
 0043 read model      0/0 shipped, 0 in work
 0044 DB capacity     0/0 shipped, 0 in work
 0045 front end       0/0 shipped, 0 in work
 0046 Proving status  0/0 shipped, 0 in work
 0047 dossiers        0/0 shipped, 0 in work

PRODUCTION: build abcdef12, errors(20m) 3
BOX: disk 42%, load 1.2

ATLAS: steady
'''
REMOTE = ('python3 -c \'import json; print(json.load(open("/opt/albert-scott/.version.json"))["sha"][:8])\'; '
          'journalctl -u bluedot-webhook --since "20 min ago" | grep -c " 500 "')
SSH = ['ssh', '-o', 'ConnectTimeout=8', 'root@167.71.46.51', REMOTE]
DISK = 'df / --output=pcent | tail -1'


def populate(root):
    (root / 'events').mkdir(parents=True)
    (root / 'state').mkdir()
    (root / 'state/operator-asks.txt').write_text(ASKS)
    (root / 'state/pulse-lanes.txt').write_text('ATLAS: steady\n')
    (root / 'events/L-operator-fixture.jsonl').write_text(
        json.dumps({'type': 'tick', 'subject': 'fixture', 'ts': NOW}) + '\n')


def manifest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.root = self.folder / 'root'
        populate(self.root)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NOW=NOW,
                                               PYTHONDONTWRITEBYTECODE='1', LC_ALL='C'))
        # A fresh production import selects the scratch root before fold import.
        prior = sys.modules.pop('fold', None)
        self.addCleanup(lambda: sys.modules.__setitem__('fold', prior) if prior is not None
                        else sys.modules.pop('fold', None))
        spec = importlib.util.spec_from_file_location('pulse_under_test', REPO / 'scripts/pulse.py')
        self.pulse = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pulse)
        self.responses = {'omens': (0, OMENS), 'board': (0, BOARD),
                          'ssh': (0, 'abcdef12\n3\n'), 'disk': (0, '42%\n')}
        self.calls = []
        self.real_run = subprocess.run
        self.stack.enter_context(mock.patch.object(self.pulse.subprocess, 'run', side_effect=self.boundary))
        self.stack.enter_context(mock.patch.object(self.pulse.os, 'getloadavg', return_value=(1.25, 0, 0)))
        self.stack.enter_context(mock.patch('socket.socket', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(mock.patch('urllib.request.urlopen', side_effect=AssertionError('send forbidden')))

    def boundary(self, argv, **kw):
        if argv == [sys.executable, str(self.root / 'omens.py')]:
            key = 'omens'
        elif argv == [str(REPO / 'doit'), 'board']:
            key = 'board'
        elif argv == SSH:
            key = 'ssh'
        elif argv == DISK:
            key = 'disk'
        else:
            self.fail('Unexpected external command')
        self.calls.append((key, argv, kw))
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return subprocess.CompletedProcess(argv, response[0], response[1], 'PULSE_SECRET_STDERR')

    def picture(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.pulse.main()
        return output.getvalue()


class PulseTests(Fixture):
    def test_golden(self):
        # G explicitly uses an existing, empty ledger directory.
        (self.root / 'events/L-operator-fixture.jsonl').unlink()
        self.assertEqual(self.picture(), GOLDEN)

    def test_empty_is_observed(self):
        (self.root / 'state/operator-asks.txt').write_text('# comment\n\n')
        (self.root / 'state/pulse-lanes.txt').unlink()
        self.responses.update(omens=(0, ''), board=(0, '## NEEDS YOU (0)\n\n## NEXT\n'))
        out = self.picture()
        self.assertIn('WAITING ON YOU: 0 (nothing; anything not listed here is not waiting on you)', out)
        self.assertIn('STUCK: 0\n', out)
        self.assertNotIn('WATCH:', out)
        self.assertNotIn('Thinker', out)
        self.assertTrue(out.endswith('BOX: disk 42%, load 1.2\n'))

    def test_failed_commands_discard_partial_output(self):
        failures = [(1, ''), (1, 'PULSE_SECRET_FAILED plausible healthy output'),
                    subprocess.TimeoutExpired('fixture', 1, output='PULSE_SECRET_PARTIAL'),
                    FileNotFoundError('PULSE_SECRET_MISSING'),
                    OSError('PULSE_SECRET_EXCEPTION'), UnicodeError('PULSE_SECRET_DECODE')]
        for key, fragment in [('omens', 'STUCK: unread\nWATCH: unread'),
                              ('board', '(Thinker escalation count: unread; not yours.)'),
                              ('ssh', 'PRODUCTION: build unread, errors(20m) unread'),
                              ('disk', 'BOX: disk unread, load 1.2')]:
            original = self.responses[key]
            for failure in failures:
                with self.subTest(source=key, failure=type(failure).__name__):
                    self.responses[key] = failure
                    out = self.picture()
                    self.assertIn(fragment, out)
                    self.assertNotIn('PULSE_SECRET_', out)
                    self.assertIn('PORTAL SPEED', out)
            self.responses[key] = original

    def test_malformed_successes(self):
        cases = [('omens', 'WARN | missing', 'STUCK: unread\nWATCH: unread'),
                 ('omens', OMENS + 'bad\n', 'STUCK: unread\nWATCH: unread'),
                 ('board', '', 'escalation count: unread'),
                 ('board', '## NEEDS YOU nope\n', 'escalation count: unread'),
                 ('board', '## NEXT\n', 'escalation count: unread'),
                 ('ssh', '', 'build unread, errors(20m) unread'),
                 ('ssh', 'bad 3', 'build unread, errors(20m) 3'),
                 ('ssh', 'abcdef12 -1', 'build abcdef12, errors(20m) unread'),
                 ('ssh', 'abcdef12', 'build abcdef12, errors(20m) unread'),
                 ('disk', '', 'BOX: disk unread'), ('disk', '42% PULSE_SECRET_X', 'BOX: disk unread')]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                old = self.responses[key]
                self.responses[key] = (0, value)
                self.assertIn(expected, self.picture())
                self.responses[key] = old
        self.responses['omens'] = (0, 'INFO | note | safe\n')
        self.assertIn('STUCK: 0\n', self.picture())

    def test_file_and_ledger_unread(self):
        asks = self.root / 'state/operator-asks.txt'
        lanes = self.root / 'state/pulse-lanes.txt'
        asks.unlink()
        self.assertIn('WAITING ON YOU: unread (asks file unreadable)', self.picture())
        asks.write_bytes(b'\xff')
        lanes.write_bytes(b'\xff')
        out = self.picture()
        self.assertIn('WAITING ON YOU: unread', out)
        self.assertIn('\n\nLANES: unread\n', out)
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('PULSE_SECRET_PERMISSION')):
            out = self.picture()
            self.assertIn('WAITING ON YOU: unread', out)
            self.assertIn('LANES: unread', out)
            self.assertNotIn('PULSE_SECRET_', out)
        for target in ('read_events', 'fold'):
            with mock.patch.object(self.pulse.fold, target, side_effect=OSError('PULSE_SECRET_LEDGER')):
                out = self.picture()
                self.assertEqual(out.count(' unread\n'), 6)  # five portal rows plus lanes
                self.assertIn(' 0043 read model      unread', out)
        shutil.rmtree(self.root / 'events')
        self.assertIn(' 0047 dossiers        unread', self.picture())
        with mock.patch.object(os, 'scandir', side_effect=PermissionError('fixture')):
            self.assertIn(' 0043 read model      unread', self.picture())

    def test_limits_and_source_order(self):
        (self.root / 'state/operator-asks.txt').write_text('#ignored\n #kept\n' + 'a' * 201 + '\n')
        self.responses['omens'] = (0, '\n'.join(
            ['WARN | health | warn first', 'WARN | shelf-stuck | ' + 's' * 111] +
            ['CRIT | pressure | crit' + str(i) for i in range(6)]))
        out = self.picture()
        self.assertIn('WAITING ON YOU: 2\n 1. #kept\n 2. ' + 'a' * 200 + '\n', out)
        self.assertNotIn('a' * 201, out)
        self.assertIn(' - ' + 's' * 110 + '\n', out)
        self.assertNotIn('s' * 111, out)
        self.assertIn('WATCH:\n' + ''.join(' - crit' + str(i) + '\n' for i in range(5)), out)
        self.assertNotIn('crit5', out)
        self.assertNotIn('warn first', out)

    def test_safe_output_all_text_sections(self):
        hostile = ('Open /tmp/report file:///tmp/x http://localhost:8080/x http://127.0.0.1/x '
                   'https://[::1]/x PULSE_SECRET_FIXTURE token=fixture-value PASSWORD:fixture '
                   'secret=fixture Bearer fixture https://example.org/report $(never-run); `never-run`')
        (self.root / 'state/operator-asks.txt').write_text(hostile)
        (self.root / 'state/pulse-lanes.txt').write_text(hostile)
        self.responses['board'] = (0, '## NEEDS YOU (1)\n' + hostile + '\n## NEXT\n')
        self.responses['omens'] = (0, 'WARN | shelf-stuck | ' + hostile + '\nCRIT | health | ' + hostile)
        out = self.picture()
        self.assertNotIn('PULSE_SECRET_', out)
        self.assertNotIn('fixture-value', out)
        for raw in ('/tmp/', 'file://', 'localhost', '127.0.0.1', '[::1]', 'PASSWORD:', 'Bearer fixture'):
            self.assertNotIn(raw, out)
        # Shorter positive control ensures redaction and hosted links survive detail truncation.
        short = 'token=fixture PULSE_SECRET_X https://example.org/report'
        (self.root / 'state/operator-asks.txt').write_text(short)
        (self.root / 'state/pulse-lanes.txt').write_text(short)
        self.responses['omens'] = (0, 'WARN | shelf-stuck | ' + short + '\nWARN | health | ' + short)
        out = self.picture()
        self.assertEqual(out.count('[redacted] [redacted] https://example.org/report'), 4)
        self.assertEqual([x[0] for x in self.calls], ['omens', 'board', 'ssh', 'disk'] * 2)

    def test_load_failures(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with mock.patch.object(os, 'getloadavg', return_value=(value, 0, 0)):
                self.assertIn('BOX: disk 42%, load unread', self.picture())
        with mock.patch.object(os, 'getloadavg', side_effect=OSError('PULSE_SECRET_LOAD')):
            self.responses['disk'] = (1, '42%')
            self.assertIn('BOX: disk unread, load unread', self.picture())

    def test_declared_state_prefixes(self):
        # Supplemental rows test all specified state prefixes, independently of fold's transitions.
        states = ['shipped-owed-due', 'accepted', 'written', 'killed', 'void', 'dropped', 'closed-unbuilt', 'building']
        rows = {str(i): {'state': state, 'evs': [{'charter': 'L-charter-9999'}, {'charter': ''},
                                                {'charter': '/fixture/L-charter-0043.md'}, {'charter': ''}]}
                for i, state in enumerate(states)}
        rows['other'] = {'state': 'building', 'evs': [{'charter': 'L-charter-9999'}]}
        with mock.patch.object(self.pulse.fold, 'fold', return_value=(rows, {})):
            self.assertIn(' 0043 read model      2/8 shipped, 1 in work', self.picture())
        # The normal path still folds genuine synthetic ledger events.
        (self.root / 'events/L-spec-writer-fixture.jsonl').write_text(json.dumps({
            'type': 'spec-written', 'subject': 'L-spec-9999', 'charter': 'L-charter-0044', 'ts': NOW}) + '\n')
        self.assertIn(' 0044 DB capacity     0/1 shipped, 0 in work', self.picture())


class ReadOnlyTests(Fixture):
    def test_real_child_default_read_is_uncached(self):
        folder = self.folder / 'child-launch'
        folder.mkdir()
        with mock.patch.object(subprocess, 'run', self.real_run):
            VersionedEntryTests.launch(self, REPO, folder, True, False)

    def test_real_fold_uncached_and_manifests(self):
        for cache in (None, '1', '0'):
            for existing in (False, True):
                with self.subTest(parent_cache=cache, existing=existing), mock.patch.dict(os.environ):
                    if cache is None:
                        os.environ.pop('DOIT_READ_CACHE', None)
                    else:
                        os.environ['DOIT_READ_CACHE'] = cache
                    cache_dir = self.root / 'cache'
                    if existing:
                        cache_dir.mkdir(exist_ok=True)
                        (cache_dir / 'sentinel').write_bytes(b'cache sentinel')
                    before = manifest(self.root)
                    before_bytes = {p: (self.root / p).read_bytes() for p in before}
                    self.calls.clear()
                    with mock.patch.object(self.pulse.fold, 'read_events', wraps=self.pulse.fold.read_events) as read:
                        self.picture()
                        read.assert_called_once_with(use_cache=False)
                    self.assertEqual(manifest(self.root), before)
                    self.assertEqual({p: (self.root / p).read_bytes() for p in before}, before_bytes)
                    self.assertEqual(cache_dir.exists(), existing)
                    self.assertEqual([k for k, _, _ in self.calls], ['omens', 'board', 'ssh', 'disk'])
                    for key, argv, kw in self.calls:
                        if key in ('omens', 'board'):
                            self.assertEqual(kw['env']['DOIT_ROOT'], str(self.root))
                            self.assertEqual(kw['env']['DOIT_READ_CACHE'], '0')
                            self.assertEqual(kw['env']['DOIT_NOW'], NOW)
                            self.assertEqual(kw['timeout'], 60 if key == 'omens' else 120)
                        if key == 'board':
                            self.assertEqual(Path(kw['cwd']), REPO)
                        if key == 'ssh':
                            self.assertEqual(kw['timeout'], 30)
                        if key == 'disk':
                            self.assertEqual(kw['timeout'], 25)
                    # Unread and hostile observations retain the same immutable manifest.
                    self.responses['omens'] = (1, 'PULSE_SECRET_FAIL')
                    self.responses['board'] = (1, 'PULSE_SECRET_FAIL')
                    self.picture()
                    for key, _, kw in self.calls[-4:-2]:
                        self.assertEqual(kw['env']['DOIT_READ_CACHE'], '0')
                    self.assertEqual(manifest(self.root), before)
                    self.responses.update(omens=(0, OMENS), board=(0, BOARD))
                    if existing:
                        shutil.rmtree(cache_dir)

    def test_hostile_inputs_do_not_execute_or_write(self):
        value = '$(touch SHOULD_NOT_EXIST); `touch SHOULD_NOT_EXIST` PULSE_SECRET_DATA token=fixture'
        (self.root / 'state/operator-asks.txt').write_text(value)
        (self.root / 'state/pulse-lanes.txt').write_text(value)
        self.responses['omens'] = (0, 'CRIT | health | ' + value)
        before = manifest(self.root)
        out = self.picture()
        self.assertNotIn('PULSE_SECRET_', out)
        self.assertEqual(manifest(self.root), before)
        self.assertEqual(len(self.calls), 4)


# Only external boundaries are faked at process startup. Production path selection,
# imports, real ledger reads and rendering execute normally via the actual entry file.
HOOK = '''import atexit, json, os, pathlib, subprocess, sys
original = subprocess.run
def capture(argv, **kw):
    with open(os.environ['PULSE_TRACE'], 'a') as f:
        f.write(json.dumps({'argv': argv, 'cwd': str(kw.get('cwd', '')), 'timeout': kw.get('timeout'),
                           'env': {k: (kw.get('env') or os.environ).get(k) for k in ('DOIT_ROOT', 'DOIT_READ_CACHE', 'DOIT_NOW')}}) + '\\n')
    if argv == [sys.executable, str(pathlib.Path(os.environ['DOIT_ROOT']) / 'omens.py')]:
        return original(argv, **kw)
    if isinstance(argv, list) and len(argv) == 2 and argv[1] == 'board':
        output = BOARD
    elif argv == SSH:
        output = 'abcdef12\\n3\\n'
    elif argv == DISK:
        output = '42%\\n'
    else:
        raise AssertionError('unexpected command')
    return subprocess.CompletedProcess(argv, 0, output, '')
subprocess.run = capture
os.getloadavg = lambda: (1.25, 0, 0)
def report():
    if 'fold' in sys.modules:
        with open(os.environ['PULSE_IMPORT_TRACE'], 'a') as f:
            f.write(str(pathlib.Path(sys.modules['fold'].__file__).resolve()) + '\\n')
atexit.register(report)
'''


class VersionedEntryTests(unittest.TestCase):
    def launch(self, repo, folder, explicit, symlink, now=NOW):
        home = folder / 'home'
        home.mkdir()
        default = home / '.do-it'
        selected = folder / 'selected' if explicit else default
        decoy = default if explicit else folder / 'selected'
        populate(selected)
        populate(decoy)
        (decoy / 'state/operator-asks.txt').write_text('DECOY MUST NOT PRINT')
        (decoy / 'state/pulse-lanes.txt').write_text('DECOY MUST NOT PRINT')
        (decoy / 'events/L-spec-writer-decoy.jsonl').write_text(json.dumps({
            'type': 'spec-written', 'subject': 'L-spec-9999', 'charter': 'L-charter-0043', 'ts': NOW}) + '\n')
        trace = folder / 'invocations.jsonl'
        imports = folder / 'imports.txt'
        helper_trace = folder / 'helper.json'
        # The child exercises the real default-cache API under the inherited disabled env.
        (selected / 'omens.py').write_text(
            'import json, os, pathlib, sys\n'
            f'sys.path.insert(0, {str(repo / "src")!r})\nimport fold\n'
            'assert os.environ["DOIT_READ_CACHE"] == "0"\nfold.read_events()\n'
            f'pathlib.Path({str(helper_trace)!r}).write_text(json.dumps(dict(root=str(fold.ROOT), cache=os.environ["DOIT_READ_CACHE"])))\n'
            f'print({OMENS!r}, end="")\n')
        hook_dir = folder / 'hooks'
        hook_dir.mkdir()
        (hook_dir / 'sitecustomize.py').write_text(
            f'BOARD={BOARD!r}\nSSH={SSH!r}\nDISK={DISK!r}\n' + HOOK)
        entry = repo / 'scripts/pulse.py'
        if symlink:
            entry = folder / 'installed-pulse.py'
            entry.symlink_to(repo / 'scripts/pulse.py')
        cwd = folder / 'elsewhere'
        cwd.mkdir()
        env = {**os.environ, 'HOME': str(home), 'DOIT_NOW': now, 'PYTHONDONTWRITEBYTECODE': '1',
               'DOIT_READ_CACHE': '1', 'PYTHONPATH': str(hook_dir), 'LC_ALL': 'C',
               'PULSE_TRACE': str(trace), 'PULSE_IMPORT_TRACE': str(imports)}
        env.pop('DOIT_ROOT', None)
        if explicit:
            env['DOIT_ROOT'] = str(selected)
        before = manifest(selected)
        result = subprocess.run(['/usr/bin/python3', str(entry)], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(manifest(selected), before)
        if now == NOW:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, GOLDEN)
            calls = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([c['argv'] for c in calls], [
                ['/usr/bin/python3', str(selected / 'omens.py')], [str(repo / 'doit'), 'board'], SSH, DISK])
            self.assertEqual([c['timeout'] for c in calls], [60, 120, 30, 25])
            self.assertEqual(calls[1]['cwd'], str(repo))
            for call in calls[:2]:
                self.assertEqual(call['env'], {'DOIT_ROOT': str(selected), 'DOIT_READ_CACHE': '0', 'DOIT_NOW': NOW})
            self.assertEqual(json.loads(helper_trace.read_text()), {'root': str(selected), 'cache': '0'})
            self.assertEqual(set(imports.read_text().splitlines()), {str(repo / 'src/fold.py')})
            self.assertFalse((selected / 'cache').exists())
            if repo != REPO:
                self.assertNotIn('/home/albert/.do-it', trace.read_text())
                self.assertNotIn('/home/albert/do-it-v2', trace.read_text())
        return result

    def test_fresh_direct_symlink_and_relocated_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            relocated = base / 'repo'
            (relocated / 'scripts').mkdir(parents=True)
            shutil.copy2(REPO / 'scripts/pulse.py', relocated / 'scripts/pulse.py')
            shutil.copy2(REPO / 'doit', relocated / 'doit')
            shutil.copytree(REPO / 'src', relocated / 'src', ignore=shutil.ignore_patterns('__pycache__', 'node_modules'))
            for repo in (REPO, relocated):
                for explicit in (True, False):
                    for symlink in (False, True):
                        with self.subTest(relocated=repo != REPO, explicit=explicit, symlink=symlink):
                            folder = base / ('case' + str(len(list(base.iterdir()))))
                            folder.mkdir()
                            self.launch(repo, folder, explicit, symlink)

    def test_clock_dst_and_invalid(self):
        cases = [('2026-10-24T21:00:00+00:00', 'PULSE 00:00 Sun 25 Oct'),
                 ('2026-10-25T01:00:00Z', 'PULSE 03:00 Sun 25 Oct'),
                 ('2026-11-07T14:00:00+02:00', 'PULSE 14:00 Sat 07 Nov'),
                 ('PULSE_SECRET_INVALID', None), ('2026-11-07T12:00:00', None)]
        for now, header in cases:
            with self.subTest(now=now), tempfile.TemporaryDirectory() as tmp:
                result = self.launch(REPO, Path(tmp), True, False, now)
                if header:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines()[0], header)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, 'pulse: invalid DOIT_NOW\n')


if __name__ == '__main__':
    unittest.main()
