#!/usr/bin/env python3
"""Hermetic local-cluster boundary, retention, allowlist and failure regressions."""
import pathlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import reap_scratch_db as r

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
NAME = 'as_authority_parallel_123456_0'

class Fake:
    def __init__(self):
        self.data = '/var/lib/postgresql/18/main'
        self.rows = [dict(oid=123, name=NAME, template=False, tablespace=1663)]
        self.age, self.active, self.dropped = 49, 0, []
    def catalog(self): return self.data, self.rows
    def newest(self, path):
        assert path == pathlib.Path(self.data) / 'base/123'
        return NOW.timestamp() - self.age * 3600
    def connections(self, name): return self.active
    def drop(self, name): self.dropped.append(name)

class Tests(unittest.TestCase):
    def run_reaper(self, f, **kw): return r.run([], runner=f, now=NOW, **kw)[0]
    def test_drop_dry_age_and_connections(self):
        f = Fake()
        self.assertEqual(self.run_reaper(f, dry_run=True)['action'], 'would-drop')
        self.assertFalse(f.dropped)
        for age in (0, 47, 48):
            f.age = age
            self.assertEqual(self.run_reaper(f)['reason'], 'too recent')
        f.age, f.active = 49, 1
        self.assertEqual(self.run_reaper(f)['reason'], 'active connections')
        f.active = 0
        self.assertEqual(self.run_reaper(f)['action'], 'dropped')
        self.assertEqual(f.dropped, [NAME])
    def test_invalid_retention_fails_closed(self):
        for hours in (0, -1, 47, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                self.run_reaper(Fake(), min_idle_h=hours)

    def test_closed_patterns_and_roots(self):
        for name in ('postgres', 'template0', 'as_prod', 'as0395_fp_manual', 'pg_parity_test',
                     'postgresql://secret@localhost/db', 'as394_scratch_1"; DROP DATABASE postgres;'):
            f = Fake(); f.rows[0]['name'] = name
            self.assertEqual(self.run_reaper(f)['action'], 'skipped')
            self.assertFalse(f.dropped)
            self.assertNotIn('secret', str(self.run_reaper(f)))
        for name in (NAME, 'as394_scratch_12', 'genesis_fp_12_123', 'as_1058_test_12_abcd'):
            self.assertTrue(r.candidate(name), name)
        for path in ('/opt/albert-scott', '/home/albert/.do-it/repos', '/var/lib/postgresql/../other'):
            f = Fake(); f.data = path
            self.assertEqual(self.run_reaper(f)['action'], 'kept')
        f = Fake(); f.rows[0]['tablespace'] = 42
        self.assertEqual(self.run_reaper(f)['action'], 'kept')
    def test_live_builder_and_undetermined(self):
        f = Fake()
        events = [dict(type='build-started', spawn='L-builder-1', subject='L-spec-1234', database=NAME)]
        self.assertEqual(r.run(events, runner=f, now=NOW)[0]['reason'], 'live builder database')
        events.append(dict(type='spawn-done', spawn='L-builder-1'))
        self.assertEqual(r.run(events, runner=f, now=NOW)[0]['action'], 'dropped')
        with patch.object(f, 'newest', side_effect=OSError):
            self.assertEqual(self.run_reaper(f)['action'], 'kept')
        with patch.object(f, 'catalog', side_effect=OSError), patch.object(r, 'Runner', return_value=f), \
             patch.object(r.fold, 'read_events', return_value=[]):
            self.assertEqual(r.main(['--dry-run']), 2)
    def test_dispatch_help(self):
        import subprocess
        doit = pathlib.Path(__file__).resolve().parent.parent / 'doit'
        result = subprocess.run([str(doit), 'reap-scratch-db', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--min-idle-h', result.stdout)

    def test_cron(self):
        import tomllib
        doc = tomllib.loads((pathlib.Path(__file__).resolve().parent.parent / 'crons.toml').read_text())
        row = next(r for r in doc['row'] if r['name'] == 'scratch-db-reaper')
        self.assertEqual(row['schedule'], '17 * * * *')
        self.assertIn('cron-run scratch-db-reaper -- {doit} reap-scratch-db', row['command'])

    def test_local_peer_no_force(self):
        with patch.object(r.Runner, 'command', return_value='') as call:
            r.Runner().drop(NAME)
        args = call.call_args.args[0]
        self.assertIn('/var/run/postgresql', args)
        self.assertIn('5432', args)
        self.assertNotIn('FORCE', args[-1])

if __name__ == '__main__': unittest.main()
