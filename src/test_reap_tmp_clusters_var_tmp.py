#!/usr/bin/env python3
"""L-spec-1968: stop-only var/tmp clusters, using fake proc and private children."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import reap_tmp


KEY = 'reap_var_tmp_cluster_min_age_h'


class VarTmpClustersTests(unittest.TestCase):
    def setUp(self):
        # Literal /var/tmp is intentional: /tmp would exercise the old admission.
        self.v = self.temp('/var/tmp')
        self.s = self.temp('/var/tmp')
        self.base = self.temp('/var/tmp')
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        (self.proc / 'uptime').write_text('1000000 0\n')
        self.root = self.base / 'tmp'
        self.root.mkdir()
        self.config = self.base / 'look.toml'
        self.state = self.base / 'clusters.json'
        self.stops = []
        self.now = datetime.now(timezone.utc)
        self.enterContext(patch.object(reap_tmp.disk_reaper, 'LOOK_TOML_PATH', self.config))
        self.enterContext(patch.object(reap_tmp, 'ROOT', self.base / 'doit'))
        self.enterContext(patch.object(reap_tmp.fold, 'ROOT', self.base / 'doit'))
        self.enterContext(patch.dict(os.environ, {'DOIT_REAP_HOLD': '0'}))
        self.enterContext(patch.object(reap_tmp, 'user_quota', return_value={
            'used_bytes': 0, 'limit_bytes': 1000, 'limit_source': 'fixture'}))
        for allowed in ('/tmp', '/dev/shm', Path.home(), self.s,
                        reap_tmp.ROOT / 'worktrees'):
            self.assertFalse(reap_tmp._under(os.path.realpath(self.v), allowed))
        self.child = subprocess.Popen(['/bin/sleep', '300'])
        self.addCleanup(self.end_child)
        self.d = self.v / 'l9999.AbCdEf/pg/data'
        self.cluster(self.d, 25)
        self.addCleanup(self.no_default_state)

    def temp(self, parent):
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def no_default_state(self):
        self.assertFalse(reap_tmp._CLUSTER_STATE_DEFAULT().exists())

    def end_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()

    def stat(self, pid, ppid, age):
        ticks = int((1000000 - age * 3600) * os.sysconf('SC_CLK_TCK'))
        fields = ['S', str(ppid)] + ['0'] * 17 + [str(ticks)]
        (self.proc / str(pid) / 'stat').write_text(f'{pid} (postgres) ' + ' '.join(fields))

    def cluster(self, d, age):
        self.d = d
        d.mkdir(parents=True, exist_ok=True)
        (d / 'postmaster.pid').write_text(str(self.child.pid) + '\n')
        p = self.proc / str(self.child.pid)
        p.mkdir(exist_ok=True)
        (p / 'cmdline').write_bytes(
            b'/usr/lib/postgresql/18/bin/postgres\0-D\0' + os.fsencode(d))
        self.stat(self.child.pid, 1, age)

    def client(self, present=True):
        p = self.proc / '999999998'
        if present:
            p.mkdir(exist_ok=True)
            (p / 'cmdline').write_text('postgres: t postgres [local] idle')
            self.stat(999999998, self.child.pid, 1)
        elif p.exists():
            for f in p.iterdir():
                f.unlink()
            p.rmdir()

    def read_state(self):
        # Independent reopen of the production-written JSON on every assertion.
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def seed(self, entries=None):
        self.state.write_text(json.dumps(entries if entries is not None else {
            str(self.d): {'idle_passes': 1}}))

    def call(self, *, opted=True, through_run=False, **kwargs):
        opts = dict(proc_root=self.proc, uid=os.getuid(), scratch_root=self.s,
                    events=None, now=self.now, state_path=self.state,
                    stop_fn=self.stops.append, kill_fn=lambda *_: None,
                    wait_s=0, dry_run=False)
        if opted:
            opts['var_tmp_root'] = self.v
        opts.update(kwargs)
        if through_run:
            opts.update(root=self.root, events=[])
            # All disk-sweep entries are newly created, never three days old.
            opts['now'] = datetime.now(timezone.utc)
            result = reap_tmp.run(**opts)
            self.assertEqual(result['errors'], {})
            self.assertTrue(self.d.exists())
            return result
        return reap_tmp._reap_clusters(**opts)

    def assert_count(self, n):
        self.assertEqual(self.read_state().get(str(self.d)),
                         None if n is None else {'idle_passes': n})

    def two_passes(self, stopped, **kwargs):
        self.call(**kwargs)
        self.assert_count(1 if stopped else None)
        rows = self.call(**kwargs)
        self.assertEqual(self.stops, [str(self.d)] if stopped else [])
        return rows

    def reset(self, age=25):
        self.stops.clear()
        self.seed({})
        self.client(False)
        self.cluster(self.d, age)

    def test_ac1_stop_retains_directory_and_persists_passes(self):
        self.assertEqual(self.call(), [])
        self.assertEqual(self.stops, [])
        self.assert_count(1)
        self.assertEqual(self.call(), [dict(dir=str(self.d), owner='unresolved', why='stopping')])
        self.assertEqual(self.stops, [str(self.d)])
        self.end_child()
        rows = self.call()
        self.assertEqual(self.stops, [str(self.d)] * 2)
        self.assertEqual(rows[0]['owner'], 'unresolved')
        self.assertIn('not removed', rows[0]['why'])
        self.assertTrue(self.d.is_dir())

    def test_ac1_young_opt_out_and_client_reset(self):
        self.cluster(self.d, 23)
        self.two_passes(False)
        self.reset()
        self.two_passes(False, opted=False)
        self.reset()
        self.call()
        self.client()
        self.call()
        self.assert_count(None)
        self.client(False)
        self.call()
        self.assert_count(1)
        self.assertEqual(self.stops, [])

    def test_ac1_symlink_root(self):
        # This alias is outside /var/tmp and every original allowed base.
        outside = self.temp('/run/lock')
        link = outside / 'alias'
        link.symlink_to(self.v, target_is_directory=True)
        self.cluster(link / 'l9999.AbCdEf/pg/data', 25)
        self.assertFalse(reap_tmp._under(link, '/var/tmp'))
        self.two_passes(True, var_tmp_root=link)

    def test_ac2_thresholds_and_reports(self):
        cases = [(None, 23, False, False), (None, 25, True, False),
                 ('30', 25, False, False), ('2', 5, False, True),
                 ('2', 7, True, True)]
        for value in ('"x"', 'true', '-1', 'nan', 'inf', '-inf'):
            cases += [(value, 23, False, True), (value, 25, True, True)]
        for value, age, stopped, invalid in cases:
            with self.subTest(value=value, age=age):
                self.reset(age)
                self.config.write_text('[thresholds]\n' + (
                    '' if value is None else f'{KEY} = {value}\n'))
                with reap_tmp.disk_reaper._settings_pass():
                    self.two_passes(stopped)
                    report = reap_tmp.disk_reaper._SETTINGS_REPORT.get()
                    self.assertEqual(KEY in report['config_invalid'], invalid)
        self.config.write_text(f'[thresholds]\n{KEY} = 2\n')
        self.reset(7)
        self.assertIsNone(reap_tmp.disk_reaper._SETTINGS_REPORT.get())
        self.two_passes(True)
        # Report even on an empty proc pass, and parse once per opted-in pass.
        (self.proc / str(self.child.pid) / 'cmdline').write_bytes(b'')
        with reap_tmp.disk_reaper._settings_pass():
            with patch.object(reap_tmp, '_var_tmp_cluster_min_age_s',
                              wraps=reap_tmp._var_tmp_cluster_min_age_s) as helper:
                self.call()
            helper.assert_called_once_with()
            self.assertIn(KEY, reap_tmp.disk_reaper._SETTINGS_REPORT.get()['config_invalid'])

    def test_ac2_missing_unreadable_and_broken_config(self):
        self.two_passes(True)
        for content in ('not toml [', 'thresholds = "bad"'):
            self.config.write_text(content)
            with reap_tmp.disk_reaper._settings_pass():
                self.assertEqual(reap_tmp._var_tmp_cluster_min_age_s(), 86400)
                self.assertEqual(reap_tmp.disk_reaper._SETTINGS_REPORT.get()['config_invalid'], set())
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertEqual(reap_tmp._var_tmp_cluster_min_age_s(), 86400)

    def test_ac3_skips_and_component_prefix(self):
        for relative in ('dt/pg/data', 'closeout-pgdata-abc/data',
                         'l9999.AbCdEf/closeout-pgdata-x/pg'):
            with self.subTest(relative=relative):
                self.reset()
                self.cluster(self.v / relative, 25)
                self.seed()
                for _ in range(3):
                    self.assertEqual(self.call(), [])
                    self.assert_count(None)
                self.assertEqual(self.stops, [])
        self.reset()
        self.cluster(self.v / 'my-closeout-pgdata-x/data', 25)
        self.two_passes(True)

    def test_ac3_client_and_original_base_symlink(self):
        self.client()
        self.seed()
        for _ in range(3):
            self.assertEqual(self.call(), [])
            self.assert_count(None)
        self.assertEqual(self.stops, [])
        self.reset(7)
        (self.s / 'link').symlink_to(self.v, target_is_directory=True)
        self.cluster(self.s / 'link/pg/data', 7)
        self.two_passes(True)

    def test_ac3_realpath_keep_and_closeout_skips(self):
        for relative in ('dt', 'closeout-pgdata-real'):
            self.reset()
            target = self.v / relative
            target.mkdir(exist_ok=True)
            alias = self.v / 'alias'
            if alias.is_symlink():
                alias.unlink()
            alias.symlink_to(target, target_is_directory=True)
            self.cluster(alias / 'pg/data', 25)
            self.two_passes(False)

    def test_ac4_dry_run_never_changes_running_state(self):
        self.two_passes(False, dry_run=True)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.call(), [])
        self.assert_count(1)
        self.assertEqual(self.call(dry_run=True), [
            dict(dir=str(self.d), owner='unresolved', why='stopping')])
        self.assert_count(1)
        self.client()
        self.assertEqual(self.call(dry_run=True), [])
        self.assert_count(1)
        self.client(False)
        absent = str(self.v / 'gone/data')
        self.seed({str(self.d): {'idle_passes': 1}, absent: {'idle_passes': 7}})
        self.call(dry_run=True)
        self.assertEqual(self.read_state(), {str(self.d): {'idle_passes': 1}})
        self.assertEqual(self.stops, [])
        # Kept-by-name entries also survive dry runs unchanged.
        self.cluster(self.v / 'dt/data', 25)
        self.seed()
        self.call(dry_run=True)
        self.assert_count(1)

    def test_ac4_hold_and_release(self):
        for _ in range(3):
            self.assertEqual(self.call(hold=True), [])
            self.assert_count(None)
        self.two_passes(True, hold=False)
        self.stops.clear()
        self.seed()
        self.assertEqual(self.call(hold=True), [])
        self.assert_count(1)
        self.assertEqual(self.call(hold=True, dry_run=True), [])
        self.assert_count(1)
        self.client()
        self.call(hold=True)
        self.assert_count(None)
        self.assertEqual(self.stops, [])

    def test_ac4_ac5_original_paths_keep_six_hour_rule(self):
        for opted in (True, False):
            for name in ('pgx/data', 'closeout-pgdata-z/data'):
                with self.subTest(opted=opted, name=name):
                    self.cluster(self.s / name, 7)
                    self.reset(7)
                    self.two_passes(True, opted=opted)
        self.reset(7)
        self.call(dry_run=True, hold=True)
        self.assert_count(1)
        self.call(hold=True)
        self.assertEqual(self.stops, [str(self.d)])
        self.reset(5)
        with patch.object(reap_tmp, '_has_client_backend',
                          wraps=reap_tmp._has_client_backend) as client:
            self.call()
        client.assert_called_once_with(self.proc, self.child.pid)
        self.assert_count(None)

    def test_ac6_run_wiring_opt_out_and_original_regressions(self):
        self.call(through_run=True)
        self.assert_count(1)
        result = self.call(through_run=True)
        self.assertEqual(result['stopped_clusters'], [
            dict(dir=str(self.d), owner='unresolved', why='stopping')])
        self.assertEqual(self.stops, [str(self.d)])
        self.assertTrue(all(row['reason'] == 'too young' for row in result['var_tmp']['kept']))
        self.reset()
        self.two_passes(False, opted=False, through_run=True)
        self.cluster(self.s / 'pgx/data', 7)
        self.reset(7)
        self.two_passes(True, opted=False, through_run=True)
        self.reset(5)
        self.two_passes(False, opted=False, through_run=True)
        self.reset(7)
        self.client()
        self.two_passes(False, opted=False, through_run=True)

    def test_ac6_run_config_invalid_and_hold(self):
        baseline = self.call(through_run=True)['surface_summary']['config_invalid']
        self.config.write_text(f'[thresholds]\n{KEY} = 2\n')
        result = self.call(through_run=True)
        self.assertEqual(result['surface_summary']['config_invalid'], baseline + 1)
        self.reset()
        # Honour the TOML hold rather than the environment override in this case.
        with patch.dict(os.environ, {'DOIT_REAP_HOLD': ''}):
            self.config.write_text('[thresholds]\nreap_hold = true\n')
            for _ in range(3):
                result = self.call(through_run=True)
                self.assertEqual(result['stopped_clusters'], [])
                self.assert_count(None)
            self.assertEqual(self.stops, [])
            var_d = self.d
            self.cluster(self.s / 'pgx/data', 7)
            self.two_passes(True, through_run=True)
            self.cluster(var_d, 25)
            self.stops.clear()
            self.config.write_text('[thresholds]\n')
            self.two_passes(True, through_run=True)


if __name__ == '__main__':
    if not Path('/var/tmp').is_dir():
        sys.exit('/var/tmp does not exist')
    if not os.access('/var/tmp', os.W_OK | os.X_OK):
        sys.exit('/var/tmp is not writable')
    unittest.main(verbosity=2)
