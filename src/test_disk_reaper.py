#!/usr/bin/env python3
"""Destructive disk reapers: every path and process fixture is disposable."""
import atexit, hashlib, json, os, pathlib, subprocess, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='doit-disk-reaper-')
atexit.register(_IMPORT_ROOT.cleanup)
for key, name in (('HOME', 'home'), ('DOIT_ROOT', 'root')):
    path = pathlib.Path(_IMPORT_ROOT.name) / name
    path.mkdir()
    os.environ[key] = str(path)
import disk_reaper as dr
import reap_tmp
import fold

_DEFAULT_LOOK_TOML_PATH = dr.LOOK_TOML_PATH
dr.LOOK_TOML_PATH = pathlib.Path(_IMPORT_ROOT.name) / 'look.toml'

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


class DiskReaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        for module, name, value in (
                (dr, 'LOOK_TOML_PATH', self.base / 'look.toml'),
                (fold, 'ROOT', self.base / 'doit-root')):
            setting = patch.object(module, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.root = self.base / 'var-tmp'
        self.root.mkdir()
        self.proc = self.base / 'proc'
        self.proc.mkdir()

    def file(self, path, days=20, data='bytes'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        self.old(path, days)
        return path

    def old(self, path, days=20):
        t = (NOW - timedelta(days=days)).timestamp()
        os.utime(path, (t, t), follow_symlinks=False)

    def ref(self, name, path):
        pid = self.proc / str(os.getpid())
        (pid / 'fd').mkdir(parents=True, exist_ok=True)
        (pid / name).symlink_to(path)

    def var(self, **kw):
        return dr.reap_var_tmp(self.root, now=NOW, proc_root=self.proc, **kw)

    def settings(self, text=None, env=''):
        dr.LOOK_TOML_PATH.unlink(missing_ok=True)
        if text is not None:
            dr.LOOK_TOML_PATH.write_text(text)
        with patch.dict(os.environ, DOIT_REAP_HOLD=env):
            return dr._settings()

    def test_1800_zombie_pid_is_exited(self):
        def before_change(proc_root, pid):
            """Embedded pre-change _unreadable_pid for the same fixtures."""
            import re
            path = pathlib.Path(proc_root) / str(pid)
            try:
                os.lstat(path)
            except FileNotFoundError:
                return 'exited', None
            if not reap_tmp.panes.alive(pid):
                return 'exited', None
            try:
                status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
                if [int(v) for v in status.get('Uid', '').split()] != [os.getuid()] * 4:
                    return None, None
                token = (path / 'cmdline').read_bytes().split(b'\0', 1)[0].decode()
                name = pathlib.Path(re.split(r'[:\s]', token, maxsplit=1)[0]).name
                if not token or name not in dr.UNREADABLE_EXEMPT_NAMES:
                    return None, None
                try:
                    root_owned = os.lstat(path / 'cwd').st_uid == 0
                except OSError:
                    root_owned = False
                if root_owned:
                    return 'root-owned cwd', name
                own = dict(line.split(':', 1) for line in
                           (pathlib.Path(proc_root) / 'self/status').read_text().splitlines() if ':' in line)
                if int(status['CapPrm'], 16) & ~int(own['CapEff'], 16):
                    return 'permitted capability outside effective set', name
            except (OSError, ValueError, KeyError, UnicodeError):
                pass
            return None, None

        outcomes = []
        for label, pid_number, content, zombie in (
                ('zombie', 305865, '305865 (codex) Z 2246 ...', True),
                ('command parentheses', 9, '9 (a) b) Z 1 ...', True),
                ('sleeping', 305865, '305865 (codex) S 2246 ...', False),
                ('missing', 305865, None, False),
                ('empty', 305865, '', False),
                ('no delimiter', 305865, '305865 codex Z 2246 ...', False),
                ('no state', 305865, '305865 (codex)', False)):
            with self.subTest(case=label):
                pid = self.proc / str(pid_number)
                pid.mkdir(exist_ok=True)
                uid = os.getuid()
                (pid / 'status').write_text(f'Uid:\t{uid} {uid} {uid} {uid}\n')
                (pid / 'cmdline').write_bytes(b'')
                stat_path = pid / 'stat'
                if content is None:
                    stat_path.unlink(missing_ok=True)
                else:
                    stat_path.write_text(content)
                for version, classifier in (('before', before_change), ('after', dr._unreadable_pid)):
                    with (self.subTest(version=version),
                          patch.object(reap_tmp.panes, 'alive', return_value=True),
                          patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('fd denied')) as refs,
                          patch.object(dr, '_unreadable_pid', classifier)):
                        exited = zombie and version == 'after'
                        self.assertEqual(classifier(self.proc, pid_number),
                                         ('exited', None) if exited else (None, None))
                        stats = {}
                        if exited:
                            self.assertEqual(dr.process_refs(self.proc, stats=stats), [])
                        else:
                            with self.assertRaises(PermissionError):
                                dr.process_refs(self.proc, stats=stats)
                        refs.assert_called_once_with(self.proc, pid_number)
                        self.assertEqual(stats['unreadable_exempt'], 0)
                        self.assertEqual(stats['exempt'], [])
                        outcomes.append(dict(case=label, version=version,
                            result='exited' if exited else 'PermissionError', unreadable_exempt=0))
                # Keep exactly one fake pid visible to the next process scan.
                self.assertEqual(len(list(pid.iterdir())), 2 if content is None else 3)
                for child in pid.iterdir():
                    child.unlink()
                pid.rmdir()
        self.assertEqual(len(outcomes), 14)
        body = json.dumps(outcomes, sort_keys=True)
        print(json.dumps(dict(url='cli://disk-reaper/L-spec-1800/AC1', status=0,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
        print('L-spec-1800 AC1 ok')

    def test_unreadable_classifier_matrix(self):
        child = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        pid = self.proc / str(child.pid)
        pid.mkdir()
        (self.proc / 'self').mkdir()
        own = self.proc / 'self/status'
        own.write_text('CapEff:\t0000000000000000\n')
        uid = os.getuid()
        old = self.file(self.root / 'old')
        log = self.file(self.base / 'sessions/old.jsonl')
        cwd = pid / 'cwd'
        cwd.symlink_to(self.base)
        real = os.lstat
        def root_cwd(path, *args, **kw):
            st = real(path, *args, **kw)
            if pathlib.Path(path) == cwd:
                fields = list(st)
                fields[4] = 0
                return os.stat_result(fields)
            return st
        def denied_cwd(path, *args, **kw):
            if pathlib.Path(path) == cwd:
                raise PermissionError('cwd')
            return real(path, *args, **kw)
        cases = [
            ('systemd', 'cap', True), ('sshd-session: albert@notty', 'root', True),
            ('python3', 'cap', False), ('systemd', 'missing', False),
            ('systemd', 'uid', False), ('systemd', 'plain', False),
            ('systemd', 'denied', False), ('sshd: albert@notty', 'cap', False),
            ('', 'cap', False), ('systemd', 'effective', False),
        ]
        outcomes = []
        for name, mechanism, exempt in cases:
            with self.subTest(name=name, mechanism=mechanism):
                status = pid / 'status'
                status.write_text(f'Uid:\t{uid} {uid} {uid + (mechanism == "uid")} {uid}\n'
                                  f'CapPrm:\t{8 if mechanism in ("cap", "effective", "uid") else 0:x}\n')
                if mechanism == 'missing':
                    status.unlink()
                (pid / 'cmdline').write_bytes(('/usr/lib/systemd/' + name + '\0--user\0').encode() if name else b'')
                own.write_text(f'CapEff:\t{8 if mechanism == "effective" else 0:x}\n')
                stats = {}
                with (patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('fixture pid')),
                      patch.object(os, 'lstat', side_effect=root_cwd if mechanism == 'root' else
                                   denied_cwd if mechanism == 'denied' else real)):
                    if exempt:
                        self.assertEqual(dr.process_refs(self.proc, stats=stats), [])
                        self.assertEqual(stats['unreadable_exempt'], 1)
                        self.assertEqual(stats['other_uid_skipped'], 0)
                        self.assertEqual(stats['exempt'][0]['name'], name.split(':')[0])
                        # Each deletion has a counted, disposable pre-image.
                        self.assertEqual(sum(p.exists() for p in (old, log)), 2)
                        self.assertEqual(len(self.var()['removed']), 1)
                        self.assertEqual(len(dr.reap_session_logs([log.parent], now=NOW,
                            proc_root=self.proc, dry_run=True)['removed']), 1)
                        self.file(old)
                    else:
                        with self.assertRaises(PermissionError):
                            dr.process_refs(self.proc, stats=stats)
                        self.assertIn('undetermined', self.var()['kept'][0]['reason'])
                        self.assertTrue(old.exists())
                        self.assertTrue(log.exists())
                outcomes.append(dict(name=name, mechanism=mechanism,
                    unreadable_exempt=stats['unreadable_exempt'], other_uid_skipped=stats['other_uid_skipped']))
        with (patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('exited')),
              patch.object(reap_tmp.panes, 'alive', side_effect=[True, False])):
            stats = {}
            self.assertEqual(dr.process_refs(self.proc, stats=stats), [])
            self.assertEqual(stats['unreadable_exempt'], 0)
        def vanished(path, *args, **kw):
            if pathlib.Path(path) == pid:
                raise FileNotFoundError('gone')
            return real(path, *args, **kw)
        with patch.object(os, 'lstat', side_effect=vanished):
            self.assertEqual(dr.process_refs(self.proc), [])
        body = json.dumps(outcomes, sort_keys=True)
        print(json.dumps(dict(url='cli://disk-reaper/L1654-AC1', status=0,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_caps_hold_and_dry_preview(self):
        for i in range(30):
            self.file(self.root / f'{i:02}')
        self.assertEqual(len(list(self.root.iterdir())), 30)
        preview = self.var(dry_run=True)
        self.assertEqual((len(preview['removed']), len(preview['kept'])), (25, 5))
        self.assertEqual(len(list(self.root.iterdir())), 30)
        with patch.dict(os.environ, DOIT_REAP_HOLD='1'):
            held = self.var()
        self.assertEqual(len(held['kept']), 30)
        self.assertEqual(held['held'], 30)
        self.assertEqual(len(self.var()['removed']), 25)
        self.assertEqual(len(self.var()['removed']), 5)
        for i in range(3):
            self.file(self.root / str(i), data='x' * (600 * 1024))
        dr.LOOK_TOML_PATH.write_text('[thresholds]\nreap_max_gb_per_pass=0.001\n')
        self.assertEqual(len(self.var()['removed']), 2)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_readable_child_cwd_retains_old_entry(self):
        active = self.root / 'active'
        active.mkdir()
        self.file(active / 'payload')
        self.old(active)
        child = subprocess.Popen(['/bin/sleep', '60'], cwd=active)
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        (self.proc / str(child.pid)).symlink_to(pathlib.Path('/proc') / str(child.pid))
        out = self.var()
        self.assertEqual(out['removed'], [])
        self.assertEqual(out['kept'][0]['reason'], 'live process references entry')
        self.assertTrue(active.exists())

    def test_session_cap_and_hold_preserve_marker_and_directories(self):
        logs = self.base / 'sessions'
        (logs / 'empty').mkdir(parents=True)
        for i in range(3):
            self.file(logs / f'{i}.jsonl')
        marker = self.base / 'marker.json'
        dr.LOOK_TOML_PATH.write_text('[thresholds]\nreap_max_entries_per_pass=2\n')
        def run(**kw):
            return dr.reap_session_logs([logs], now=NOW, proc_root=self.proc,
                                        state_path=marker, **kw)
        with patch.dict(os.environ, DOIT_REAP_HOLD='1'):
            self.assertEqual(run()['held'], 3)
        self.assertTrue((logs / 'empty').exists())
        self.assertFalse(marker.exists())
        self.assertEqual(len(run()['removed']), 2)
        self.assertFalse(marker.exists())
        self.assertEqual(len(run()['removed']), 1)
        self.assertTrue(marker.exists())

    def test_settings_defaults_and_checkout_path(self):
        self.assertEqual(_DEFAULT_LOOK_TOML_PATH, reap_tmp.look.DEFAULT_TOML_PATH)
        out = self.settings()
        self.assertEqual(out, {
            'reap_max_entries_per_pass': 25, 'reap_max_gb_per_pass': 10,
            'reap_retire_max_per_pass': 200, 'reap_gone_grace_h': 6,
            'hold': False, 'config_invalid': set(),
        })
        self.assertEqual(self.settings('[thresholds]\nreap_max_gb_per_pass=0.001\n')
                         ['reap_max_gb_per_pass'], 0.001)
        for key in ('reap_max_entries_per_pass', 'reap_retire_max_per_pass',
                    'reap_max_gb_per_pass', 'reap_gone_grace_h'):
            with self.subTest(key=key):
                out = self.settings(f'[thresholds]\n{key}=0\n')
                self.assertEqual(out[key], 0)
                self.assertEqual(out['config_invalid'], set())

    def test_settings_numeric_validation(self):
        defaults = {'reap_max_entries_per_pass': 25, 'reap_max_gb_per_pass': 10,
                    'reap_retire_max_per_pass': 200, 'reap_gone_grace_h': 6}
        for key, default in defaults.items():
            values = ['"lots"', 'true', '-1', 'nan', 'inf', '-inf']
            if key in ('reap_max_entries_per_pass', 'reap_retire_max_per_pass'):
                values += ['2.5', '25.0']
            for value in values:
                with self.subTest(key=key, value=value):
                    out = self.settings(f'[thresholds]\n{key}={value}\n')
                    self.assertEqual(out[key], default)
                    self.assertEqual(out['config_invalid'], {key})
                    self.assertFalse(out['hold'])
        for key in ('reap_max_gb_per_pass', 'reap_gone_grace_h'):
            out = self.settings(f'[thresholds]\n{key}=2.5\n')
            self.assertEqual(out[key], 2.5)
            self.assertEqual(out['config_invalid'], set())

    def test_settings_toml_and_environment_hold(self):
        for value, expected, invalid in (
                ('true', True, set()), ('false', False, set()),
                ('"true"', True, {'reap_hold'}), ('0', True, {'reap_hold'})):
            with self.subTest(value=value):
                text = f'[thresholds]\nreap_hold={value}\n'
                out = self.settings(text)
                self.assertEqual(out['hold'], expected)
                self.assertEqual(out['config_invalid'], invalid)
                self.assertFalse(self.settings(text, env='0')['hold'])
                self.assertTrue(self.settings(text, env='1')['hold'])
        self.assertTrue(self.settings(env='true')['hold'])
        self.assertEqual(self.settings(env='true')['config_invalid'], {'DOIT_REAP_HOLD'})
        with patch.dict(os.environ):
            os.environ.pop('DOIT_REAP_HOLD', None)
            self.assertFalse(dr._settings()['hold'])
        for value in ('true', 'false'):
            text = f'[thresholds]\nreap_hold={value}\nbroken = [\n'
            out = self.settings(text)
            self.assertTrue(out['hold'])
            self.assertEqual(out['config_invalid'], {'look.toml'})
            self.assertEqual(out['reap_max_entries_per_pass'], 25)
            self.assertFalse(self.settings(text, env='0')['hold'])
        self.assertFalse(self.settings('[thresholds]\nreap_hld=true\n')['hold'])

    def test_settings_unreadable_toml_and_invalid_table(self):
        with patch.object(pathlib.Path, 'open', side_effect=PermissionError('unreadable')):
            out = dr._settings()
        self.assertTrue(out['hold'])
        self.assertEqual(out['config_invalid'], {'look.toml'})
        out = self.settings('thresholds = "not a table"\n')
        self.assertTrue(out['hold'])
        self.assertEqual(out['config_invalid'], {'look.toml'})

    def test_settings_hold_file_precedence_and_errors(self):
        hold = fold.ROOT / 'state/reap-hold'
        self.assertFalse(self.settings()['hold'])
        hold.parent.mkdir(parents=True)
        hold.touch()
        for env in ('', '0', '1', 'true'):
            with self.subTest(env=env):
                self.assertTrue(self.settings(env=env)['hold'])
        hold.unlink()
        hold.symlink_to(self.base / 'missing')
        self.assertTrue(self.settings(env='0')['hold'])
        hold.unlink()
        hold.mkdir()
        self.assertTrue(self.settings(env='0')['hold'])
        hold.rmdir()
        hold.parent.rmdir()
        hold.parent.write_text('not a directory')
        out = self.settings()
        self.assertFalse(out['hold'])
        self.assertEqual(out['config_invalid'], {'reap-hold'})

    def test_settings_reread_and_distinct_invalid_names(self):
        text = '[thresholds]\nreap_max_entries_per_pass="lots"\nreap_hold="yes"\n'
        first = self.settings(text)
        second = dr._settings()
        self.assertEqual(first['config_invalid'] | second['config_invalid'],
                         {'reap_max_entries_per_pass', 'reap_hold'})
        self.assertTrue(first['hold'])
        self.assertFalse(self.settings('[thresholds]\nreap_hold=false\n')['hold'])

    def test_var_tmp(self):
        tree = self.root / 'tree'
        self.file(tree / 'nested' / 'data', data='12345')
        for p in (tree / 'nested', tree):
            self.old(p)
        old = self.file(self.root / 'old', data='123')
        target = self.file(self.base / 'outside')
        link = self.root / 'link'
        link.symlink_to(target)
        self.old(link)
        young = self.file(self.root / 'young', 2)
        dt = self.file(self.root / 'dt')
        cwd = self.file(self.root / 'cwd')
        fd = self.file(self.root / 'fd')
        recorded = self.file(self.root / 'recorded')
        alien = self.file(self.root / 'alien')
        self.ref('cwd', cwd)
        self.ref('fd/7', fd)
        real = os.lstat
        def lstat(path, *args, **kwargs):
            st = real(path, *args, **kwargs)
            if str(path) == str(alien):
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        with patch.object(os, 'lstat', side_effect=lstat):
            dry = self.var(recorded_refs=(recorded,), dry_run=True)
            self.assertEqual(len(list(self.root.iterdir())), 9)
            self.assertEqual(set(dry['removed']), {str(tree), str(old), str(link)})
            out = self.var(recorded_refs=(recorded,))
        self.assertEqual(out['freed_bytes'], 8)
        self.assertEqual(set(out['removed']), set(dry['removed']))
        self.assertTrue(target.exists())
        for p in (young, dt, cwd, fd, recorded, alien):
            self.assertTrue(p.exists(), str(p))
        self.assertEqual(len(out['kept']), 6)

    def test_var_fail_closed(self):
        old = self.file(self.root / 'old')
        self.ref('cwd', self.base)
        with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')):
            out = self.var()
        self.assertTrue(old.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        with patch.object(reap_tmp, '_newest_mtime', side_effect=PermissionError('mtime')):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        with patch.object(os, 'lstat', side_effect=PermissionError('lstat')):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])

    def test_other_uid_unreadable_process_does_not_block_cleanup(self):
        old = self.file(self.root / 'old')
        log = self.file(self.base / 'sessions/old.jsonl')
        pid = self.proc / str(os.getpid())
        pid.mkdir()
        real_lstat = os.lstat
        def other_uid(path, *args, **kwargs):
            st = real_lstat(path, *args, **kwargs)
            if pathlib.Path(path) == pid:
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        self.assertEqual(sum(p.exists() for p in (old, log)), 2)
        with (patch.object(os, 'lstat', side_effect=other_uid),
              patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('other uid')) as refs):
            var = self.var()
            sessions = self.sessions()
        refs.assert_not_called()
        self.assertEqual(var['removed'], [str(old)])
        self.assertEqual(sessions['removed'], [str(log)])
        self.assertEqual(var['other_uid_skipped'], 1)
        self.assertEqual(sessions['other_uid_skipped'], 1)
        self.assertFalse(old.exists())
        self.assertFalse(log.exists())

    def test_process_listing_and_pid_ownership_fail_closed(self):
        old = self.file(self.root / 'old')
        log = self.file(self.base / 'sessions/old.jsonl')
        pid = self.proc / str(os.getpid())
        pid.mkdir()
        real_lstat = os.lstat
        def unreadable_pid(path, *args, **kwargs):
            if pathlib.Path(path) == pid:
                raise PermissionError('pid ownership')
            return real_lstat(path, *args, **kwargs)
        for failure in (patch.object(reap_tmp, '_pid_dir_names', side_effect=PermissionError('proc listing')),
                        patch.object(os, 'lstat', side_effect=unreadable_pid)):
            with failure:
                var = self.var()
                sessions = self.sessions()
            self.assertTrue(old.exists())
            self.assertTrue(log.exists())
            self.assertIn('undetermined', var['kept'][0]['reason'])
            self.assertIn('undetermined', sessions['kept'][0]['reason'])
            self.assertFalse((self.base / 'state').exists())

    def test_var_nested_and_scan_safety(self):
        tree = self.root / 'tree'
        child = self.file(tree / 'child', 1)
        self.old(tree)
        self.assertEqual(self.var()['kept'][0]['reason'], 'too young')
        self.old(child)
        real_inventory = dr._inventory
        with patch.object(dr, '_inventory', side_effect=PermissionError('unreadable child')):
            out = self.var()
        self.assertTrue(child.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        with patch.object(os.path, 'ismount', side_effect=lambda p: pathlib.Path(p) == tree):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(child.exists())
        def touch_child(path, *args, **kw):
            self.old(child, 1)
            return real_inventory(path, *args, **kw)
        with patch.object(dr, '_inventory', side_effect=touch_child):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(child.exists())
        self.old(child)
        outside = self.file(self.base / 'outside')
        # A replacement after inventory cannot redirect deletion through a link.
        def replace(path, *args, **kw):
            records = real_inventory(path, *args, **kw)
            child.unlink()
            tree.rmdir()
            tree.symlink_to(outside.parent, target_is_directory=True)
            return records
        with patch.object(dr, '_inventory', side_effect=replace):
            self.assertIn('undetermined', self.var()['kept'][0]['reason'])
        self.assertTrue(outside.exists())

    def sessions(self, **kw):
        return dr.reap_session_logs([self.root, self.base / 'sessions'], now=kw.pop('now', NOW),
                                   proc_root=self.proc, state_path=self.base / 'state/date.json', **kw)

    def test_sessions(self):
        (self.base / 'sessions').mkdir()
        old = self.file(self.root / 'project' / 'old.jsonl', data='1234')
        young = self.file(self.root / 'young.jsonl', 13)
        opened = self.file(self.root / 'open.jsonl')
        notes = self.file(self.root / 'memory/notes.md')
        settings = self.file(self.root / 'settings.json')
        outside = self.file(self.base / 'outside.jsonl')
        (self.root / 'alias.jsonl').symlink_to(outside)
        (self.root / 'linked').symlink_to(self.base, target_is_directory=True)
        self.ref('fd/3', opened)
        self.assertTrue(self.sessions(dry_run=True)['ran'])
        self.assertTrue(old.exists())
        self.assertFalse((self.base / 'state').exists())
        out = self.sessions()
        self.assertEqual(out['removed'], [str(old)])
        self.assertEqual(out['freed_bytes'], 4)
        self.assertFalse(old.parent.exists())
        self.assertTrue(self.root.is_dir())
        for p in (young, opened, notes, settings, outside):
            self.assertTrue(p.exists())
        self.assertFalse(self.sessions()['ran'])
        self.assertTrue(self.sessions(now=NOW + timedelta(days=1))['ran'])

    def test_session_scan_failure_does_not_mark(self):
        (self.base / 'sessions').mkdir()
        old = self.file(self.root / 'old.jsonl')
        self.ref('cwd', self.base)
        with patch.object(reap_tmp, '_pid_refs', side_effect=PermissionError('proc')):
            out = self.sessions()
        self.assertTrue(old.exists())
        self.assertIn('undetermined', out['kept'][0]['reason'])
        self.assertFalse((self.base / 'state').exists())
        real = dr._inventory
        def fail(path, *args, **kwargs):
            if pathlib.Path(path) == self.base / 'sessions':
                raise PermissionError('scan')
            return real(path, *args, **kwargs)
        with patch.object(dr, '_inventory', side_effect=fail):
            out = self.sessions()
        self.assertTrue(out['errors'])
        self.assertFalse((self.base / 'state').exists())


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiskReaperTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    body = json.dumps({'tests': result.testsRun, 'success': result.wasSuccessful()}, sort_keys=True)
    print(json.dumps(dict(url='cli://disk-reaper/AC3-AC4-AC7', status=0 if result.wasSuccessful() else 1,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    raise SystemExit(not result.wasSuccessful())
