"""Hermetic decision, freshness, bounded-work and timing regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import disk_reaper as dr
import reap_tmp as rt

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
UID = os.getuid()
QUOTA = dict(used_bytes=1, limit_bytes=1000, limit_source='configured')
KEYS = set('total quota settings ref_index proc_snapshot tmp nested scratch recorded '
           'servers worktrees var_tmp session_logs grade_views clusters databases'.split())


class PassCost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pass-cost-')
        self.addCleanup(self.tmp.cleanup)
        # Runner TMPDIR may be a short symlink; anchored reapers reject links.
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'tmp'
        self.proc = self.base / 'proc'
        self.root.mkdir()
        (self.proc / 'net').mkdir(parents=True)
        (self.proc / 'net/unix').write_text('header\n')
        self.enter = contextlib.ExitStack()
        self.addCleanup(self.enter.close)
        for p in (patch.object(rt, 'user_quota', return_value=QUOTA),
                  patch.object(rt.panes, 'alive', return_value=True),
                  patch.object(dr, 'LOOK_TOML_PATH', self.base / 'look.toml'),
                  patch.object(rt.fold, 'ROOT', self.base / 'doit'),
                  patch.dict(os.environ, {}, clear=False)):
            self.enter.enter_context(p)
        os.environ.pop('DOIT_REAP_HOLD', None)

    def old(self, path, hours=100):
        for p in [*path.rglob('*'), path] if path.is_dir() else [path]:
            os.utime(p, (NOW.timestamp() - hours * 3600,) * 2, follow_symlinks=False)
        return path

    def directory(self, name):
        p = self.root / name
        p.mkdir(parents=True)
        return self.old(p)

    def fixture(self):
        self.directory('O')
        r = self.directory('R')
        (r / 'a').mkdir()
        (r / 'b').mkdir()
        self.old(r)
        (self.root / 'S').write_text('socket')
        self.old(self.root / 'S')
        (self.root / 'Y').write_text('young')
        os.utime(self.root / 'Y', (NOW.timestamp(),) * 2)
        (self.proc / '123/fd').mkdir(parents=True)
        (self.proc / '123/cwd').symlink_to(r / 'a')
        (self.proc / 'net/unix').write_text(f'header\n0 0 0 0 0 0 {self.root / "S"}\n')
        return r

    def run_pass(self, **kw):
        return rt.run(root=self.root, proc_root=self.proc, uid=UID, now=NOW, **kw)

    def reasons(self, result):
        return {Path(row['path']).name: row['reason'] for row in result['kept']}

    def test_ac1_ac3_shared_sockets_and_parent_pidfiles(self):
        r = self.fixture()
        with (patch.object(rt, '_bound_sockets', wraps=rt._bound_sockets) as sockets,
              patch.object(rt, '_pidfiles_under', wraps=rt._pidfiles_under) as walks):
            result = self.run_pass()
        self.assertEqual(set(result['removed']), {str(self.root / 'O'), str(r / 'b')})
        why = self.reasons(result)
        self.assertTrue(why['S'].startswith('a bound socket or live pidfile at'))
        self.assertEqual(why['Y'], 'too young')
        self.assertTrue(why['a'].startswith('a live process references'))
        self.assertEqual(sockets.call_count, 3)
        paths = [Path(c.args[0]) for c in walks.call_args_list]
        self.assertEqual(paths.count(r), 1)
        self.assertEqual(paths.count(r / 'a'), 0)
        self.assertEqual(paths.count(r / 'b'), 1)

    def test_ac2_retry_failed_cache_and_fresh_confirmations(self):
        for name in ('a', 'b', 'c'):
            self.directory(name)
        with patch.object(rt, '_bound_sockets', side_effect=[OSError(), [], [], []]) as sockets:
            result = self.run_pass(dry_run=True)
        self.assertEqual(self.reasons(result)['a'], 'undetermined')
        self.assertEqual(len(result['removed']), 2)
        self.assertEqual(sockets.call_count, 4)
        # A successful cache is not permission to remove against stale sockets.
        for dry in (False, True):
            with patch.object(rt, '_bound_sockets', side_effect=[[], *[[str(self.root / name)] for name in ('a', 'b', 'c')]]) as sockets:
                result = self.run_pass(dry_run=dry)
            self.assertEqual(result['removed'], [])
            self.assertTrue(all(reason.startswith('a bound socket') for reason in self.reasons(result).values()))
            self.assertEqual(sockets.call_count, 4)
        with patch.object(rt, '_bound_sockets', side_effect=[[], OSError(), OSError(), OSError()]):
            result = self.run_pass()
        self.assertEqual(result['removed'], [])
        self.assertEqual(set(self.reasons(result).values()), {'undetermined'})
        self.assertEqual(len(list(self.root.iterdir())), 3)
        with patch.object(rt, '_bound_sockets', side_effect=AssertionError('other uid socket read')):
            result = rt.run(root=self.root, proc_root=self.proc, uid=UID + 1, now=NOW)
        self.assertEqual(result['removed'], [])

    def test_ac3_live_pidfile_and_non_directory_children(self):
        r = self.fixture()
        pf = r / 'b/server.pid'
        pf.write_text('123\n')
        (r / 'c.pid').write_text('123\n')
        (r / 'l.pid').symlink_to(r / 'c.pid')
        self.old(r)
        with patch.object(rt, '_pidfiles_under', wraps=rt._pidfiles_under) as walks:
            result = self.run_pass()
        self.assertTrue(self.reasons(result)['b'].endswith(str(pf)))
        self.assertNotIn(r / 'b', [Path(c.args[0]) for c in walks.call_args_list])
        self.assertTrue({str(r / 'c.pid'), str(r / 'l.pid')} <= set(result['removed']))

    def test_ac3_fresh_child_walk_and_unreadable_parent(self):
        r = self.fixture()
        pf = r / 'b/server.pid'
        pf.write_text('123\n')
        self.old(r)
        original = rt._pidfiles_under
        def walk(path):
            return [] if Path(path) == r else original(path)
        with patch.object(rt, '_pidfiles_under', side_effect=walk):
            result = self.run_pass(dry_run=True)
        self.assertTrue(self.reasons(result)['b'].endswith(str(pf)))
        def denied(path):
            if Path(path) == r:
                raise PermissionError()
            return original(path)
        with patch.object(rt, '_pidfiles_under', side_effect=denied):
            result = self.run_pass(dry_run=True)
        self.assertEqual(self.reasons(result)['R'], 'undetermined')
        self.assertNotIn('a', self.reasons(result))

    def test_ac4_equivalence_and_depth_bound(self):
        link = self.base / 'link'
        link.symlink_to('/x/a')
        paths = [Path(p) for p in ('/', '/x/a', '/x/a/b', '/x/ab', '/x/z')] + [link]
        class CountSet(set):
            count = 0
            def __contains__(self, value):
                self.count += 1
                return super().__contains__(value)
        for ref in paths[:-1]:
            index = rt._recorded_index([ref])
            index.refs, index.ancestors = CountSet(index.refs), CountSet(index.ancestors)
            for p in paths:
                resolved = p.resolve()
                expected = resolved == ref or resolved in ref.parents or ref in resolved.parents
                before = index.refs.count + index.ancestors.count
                self.assertEqual(rt._recorded_overlap(p, index), expected)
                self.assertEqual(rt._recorded_overlap(p, (ref,)), expected)
                self.assertLessEqual(index.refs.count + index.ancestors.count - before,
                                     len(resolved.parts) + 1)

    def test_ac4_scans_use_constant_memberships(self):
        entries = [self.directory(f'e{i}') for i in range(40)]
        nested = [self.directory(f'claude-{UID}/p/s/scratchpad/pgdata-{i}') for i in range(40)]
        totals = []
        class CountSet(set):
            count = 0
            def __contains__(self, value):
                self.count += 1
                return super().__contains__(value)
        for count in (1, 500):
            refs = tuple(Path(f'/unrelated/{i}/leaf') for i in range(count))
            index = rt._recorded_index(refs)
            index.refs, index.ancestors = CountSet(index.refs), CountSet(index.ancestors)
            def scans(refs):
                rt._scan_dir(str(self.root), UID, 120, NOW, self.proc, True, [], refs)
                rt._scan_nested_pgdata(nested, UID, NOW, self.proc, True, [], refs)
                dr.reap_var_tmp(self.root, uid=UID, now=NOW, proc_root=self.proc,
                                recorded_refs=refs, dry_run=True)
            with patch.object(rt, '_overlaps_ref', wraps=rt._overlaps_ref) as overlaps:
                scans(index)
                self.assertEqual(overlaps.call_count, 0)
                scans(refs)
                self.assertGreaterEqual(overlaps.call_count, count * 120)
            totals.append(index.refs.count + index.ancestors.count)
        self.assertEqual(*totals)

    def test_ac4_exactly_two_shared_indexes(self):
        nested = self.directory(f'claude-{UID}/p/s/scratchpad/pgdata-a')
        scr = self.base / 'scratch'
        scr.mkdir()
        refs = [Path(f'/elsewhere/{i}/leaf') for i in range(500)]
        rows = [SimpleNamespace(ref=str(p)) for p in refs]
        original = rt._recorded_index
        built = []
        def build(refs):
            index = original(refs)
            built.append(index)
            return index
        with (patch('resources.live', return_value=rows),
              patch.object(rt, '_recorded_index', side_effect=build),
              patch.object(rt, '_scan_dir', return_value=([], [])) as generic,
              patch.object(rt, '_scan_nested_pgdata', return_value=([], [])) as ns,
              patch.object(rt, '_scan_scratch', return_value=([], [])) as ss,
              patch.object(dr, 'reap_var_tmp', return_value={'removed': [], 'kept': []}) as vs):
            self.run_pass(scratch_root=scr, var_tmp_root=self.root)
        self.assertEqual(len(built), 2)
        a, b = built
        self.assertEqual(a.refs, set(refs))
        self.assertEqual(b.refs, {*refs, nested.resolve()})
        self.assertIs(generic.call_args.args[7], b)
        self.assertIs(ns.call_args.args[6], a)
        self.assertIs(ss.call_args.args[5], a)
        self.assertIs(vs.call_args.kwargs['recorded_refs'], a)

    def test_ac5_caps_hold_dry_run_and_failed_inventory(self):
        for i in range(30):
            p = self.directory(f'e{i:02}')
            (p / 'data').write_bytes(b'x' * 2000)
            self.old(p)
        hold = rt.fold.ROOT / 'state/reap-hold'
        hold.parent.mkdir(parents=True)
        original = dr._inventory
        def inventory(path, uid):
            if Path(path).name == 'e29':
                raise OSError('entry changed during eligibility check')
            return original(path, uid)
        for held, dry, count in ((False, True, 25), (True, False, 0), (True, True, 25)):
            if held:
                hold.touch()
            else:
                hold.unlink(missing_ok=True)
            with patch.object(dr, '_inventory', side_effect=inventory) as inv:
                result = dr.reap_var_tmp(self.root, uid=UID, now=NOW,
                                          proc_root=self.proc, dry_run=dry)
            self.assertEqual(inv.call_count, count)
            self.assertEqual(len(result['removed']), count)
            self.assertEqual(result['held'], 30 if held and not dry else 0)
            self.assertEqual(result['deferred'], 0 if held and not dry else 5)
            self.assertTrue(all(p.exists() for p in self.root.iterdir()))
        hold.unlink()
        (self.base / 'look.toml').write_text('[thresholds]\nreap_max_gb_per_pass=0.000001\n')
        with patch.object(dr, '_inventory', wraps=original) as inv:
            result = dr.reap_var_tmp(self.root, uid=UID, now=NOW, proc_root=self.proc)
        self.assertEqual(inv.call_count, 1)
        self.assertEqual(result['deferred'], 29)

    def test_ac4_ac5_aliased_temporary_root(self):
        alias = self.base / 'tmp-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             'PassCost.test_ac4_scans_use_constant_memberships',
             'PassCost.test_ac5_caps_hold_dry_run_and_failed_inventory'],
            env={**os.environ, 'TMPDIR': str(alias)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ac5_non_directory_scan_roots_fail_closed(self):
        regular = self.root / 'file'
        regular.write_text('retain')
        linked = self.base / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        for root in (regular, regular / 'child', linked, self.base / 'missing'):
            with self.subTest(root=root), patch.object(dr, '_inventory') as inventory:
                result = dr.reap_var_tmp(root, uid=UID, now=NOW, proc_root=self.proc)
                self.assertEqual(result['removed'], [])
                self.assertEqual(len(result['kept']), 1)
                self.assertEqual(result['kept'][0]['path'], str(root))
                self.assertTrue(result['kept'][0]['reason'].startswith('undetermined:'))
                inventory.assert_not_called()
                self.assertEqual(regular.read_text(), 'retain')

    def test_ac5_mixed_entry_types_and_root_listing_failure(self):
        regular = self.root / 'file'
        regular.write_text('old file')
        target = self.base / 'outside'
        target.write_text('retain')
        link = self.root / 'link'
        link.symlink_to(target)
        directory = self.directory('directory')
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        for entry in (regular, link, fifo):
            self.old(entry)
        self.assertEqual(len(list(self.root.iterdir())), 4)
        with patch.object(os, 'listdir', side_effect=NotADirectoryError('root replaced')):
            result = dr.reap_var_tmp(self.root, uid=UID, now=NOW, proc_root=self.proc)
        self.assertEqual(result['removed'], [])
        self.assertEqual(result['kept'][0]['reason'], 'undetermined: root replaced')
        for dry in (True, False):
            with self.subTest(dry=dry):
                result = dr.reap_var_tmp(self.root, uid=UID, now=NOW,
                                         proc_root=self.proc, dry_run=dry)
                self.assertEqual(set(result['removed']), {str(regular), str(link), str(directory)})
                self.assertEqual(self.reasons(result), {'fifo': 'unsupported entry type'})
                self.assertEqual(target.read_text(), 'retain')
                self.assertEqual(len(list(self.root.iterdir())), 4 if dry else 1)

    def test_ac6_timings_log_resolver_and_stdout(self):
        var = self.base / 'var'
        var.mkdir()
        ticks = []
        def times():
            ticks.append(1)
            n = len(ticks)
            return SimpleNamespace(user=n*.5, system=n*.25, children_user=0.,
                                   children_system=0., elapsed=float(n))
        with patch.object(rt, '_times', side_effect=times):
            result = self.run_pass(events=[], var_tmp_root=var, session_roots=[],
                                   log_path=self.base / 'log')
        self.assertEqual(set(result['timings_s']), KEYS)
        self.assertEqual(set(result['cpu_s']), KEYS)
        active = set('quota settings ref_index proc_snapshot tmp nested recorded worktrees var_tmp session_logs'.split())
        for key in KEYS - {'total'}:
            self.assertIsInstance(result['timings_s'][key], float)
            self.assertGreaterEqual(result['timings_s'][key], 0.)
            self.assertEqual(result['cpu_s'][key], [1., .5] if key == 'nested' else
                             [.5, .25] if key in active else [0., 0.])
        self.assertEqual(len(ticks), 24)
        self.assertGreater(result['cpu_s']['total'][0], 1.)
        real = self.run_pass()
        self.assertTrue(all(len(v) == 2 and all(isinstance(n, float) and n >= 0 for n in v)
                            for v in real['cpu_s'].values()))
        lines = (self.base / 'log').read_text().splitlines()
        self.assertEqual(len(lines), 6)
        prefix, cpu = lines[5].rsplit(' cpu_s=', 1)
        self.assertEqual(json.loads(cpu), result['cpu_s'])
        self.assertEqual(json.loads(prefix.rsplit(' timings_s=', 1)[1]), result['timings_s'])
        self.assertTrue(all(len(line.encode()) < 2048 for line in lines[4:]))
        with (patch.object(rt, 'run', return_value=result), patch.object(rt.fold, 'read_events', return_value=[]),
              patch.object(rt.scratch, 'root', return_value=self.base),
              patch.object(rt, '_psql_pg_fn', return_value=None), patch.object(rt, '_times', side_effect=times)):
            resolved = rt.reap_now()
        self.assertEqual(resolved['cpu_s']['read_events'], [.5, .25])
        bare = dict(removed=[], kept=[], before_pct=42, after_pct=10)
        with (patch.object(rt, 'run', return_value=bare), patch.object(rt.fold, 'read_events', return_value=[]),
              patch.object(rt.scratch, 'root', return_value=self.base), patch.object(rt, '_psql_pg_fn', return_value=None)):
            self.assertEqual(rt.reap_now(), bare)
        for payload in (resolved, bare):
            output = io.StringIO()
            with (patch.object(rt, 'reap_now', return_value=payload), patch.object(rt, 'ROOT', self.base),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(rt.main([]), 0)
            self.assertEqual('timings_s' in json.loads(output.getvalue()), payload is resolved)

    def test_ac7_nested_cached_and_fresh_evidence(self):
        for dry in (True, False):
            with self.subTest(dry=dry):
                o = self.directory('O') if not (self.root / 'O').exists() else self.root / 'O'
                paths = [self.directory(f'claude-{UID}/p/s/scratchpad/pgdata-{s}')
                         for s in ('a', 'b', 'c', 'd', 'e')]
                (paths[4] / 'postmaster.pid').write_text('123\n')
                self.old(paths[4])
                self.old(paths[3], 47)
                (self.proc / 'net/unix').write_text(f'header\n0 0 0 0 0 0 {paths[2] / "sock"}\n')
                with (patch.object(rt, '_bound_sockets', wraps=rt._bound_sockets) as sockets,
                      patch.object(rt, '_pidfiles_under', wraps=rt._pidfiles_under) as walks,
                      patch.object(rt, '_nested_pgdata', wraps=rt._nested_pgdata) as discovery):
                    result = self.run_pass(dry_run=dry)
                self.assertEqual(set(result['removed']), {str(o), str(paths[0]), str(paths[1])})
                self.assertEqual(sockets.call_count, 4)
                self.assertEqual(discovery.call_count, 1)
                self.assertEqual(walks.call_count, 6)  # O and five nested candidates
                self.assertEqual(self.reasons(result)['pgdata-d'], 'too young')
                self.assertEqual(self.reasons(result)[f'claude-{UID}'], 'recorded resource; registry owns cleanup')
                if dry:
                    for p in paths:
                        p.rmdir() if not list(p.iterdir()) else (p / 'postmaster.pid').unlink()
                    paths[4].rmdir()

    def test_ac7_fresh_nested_checks_and_selection(self):
        o = self.directory('O')
        a, b = [self.directory(f'claude-{UID}/p/s/scratchpad/pgdata-{s}') for s in ('a', 'b')]
        for dry in (True, False):
            def fresh(_):
                return [] if sockets.call_count == 1 else [str(a / 'sock')]
            with patch.object(rt, '_bound_sockets', side_effect=fresh) as sockets:
                result = self.run_pass(dry_run=dry)
            self.assertEqual(set(result['removed']), {str(o), str(b)})
            self.assertTrue(self.reasons(result)['pgdata-a'].startswith('a bound socket'))
            self.assertEqual(sockets.call_count, 4)
            self.assertTrue(a.exists())
            if dry:
                self.assertTrue(o.exists() and b.exists())
        o.mkdir()
        b.mkdir()
        self.old(o)
        self.old(b)
        def failed(_):
            if sockets.call_count == 1:
                return []
            raise OSError('fresh read failed')
        with patch.object(rt, '_bound_sockets', side_effect=failed) as sockets:
            result = self.run_pass()
        self.assertEqual(result['removed'], [])
        for name in ('O', 'pgdata-a', 'pgdata-b'):
            self.assertEqual(self.reasons(result)[name], 'undetermined')
        o.rmdir()
        with patch.object(rt, '_bound_sockets', wraps=rt._bound_sockets) as sockets:
            self.assertEqual(len(self.run_pass(dry_run=True)['removed']), 2)
        self.assertEqual(sockets.call_count, 3)
        outside = self.directory('pgdata-outside')
        link = a.parent / 'pgdata-symlink'
        link.symlink_to(outside)
        selected = rt._nested_pgdata(self.root, UID)
        self.assertNotIn(outside, selected)
        self.assertNotIn(link, selected)


if __name__ == '__main__':
    unittest.main()
