"""Bounded synthetic ledger regressions for L-spec-1711; no external work."""
from contextlib import ExitStack, closing, contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import ast
import fcntl
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Every repository import sees scratch roots, even when invoked directly.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='reader-snapshot-import-')
os.environ.update(DOIT_ROOT=_IMPORT_ROOT.name,
                  V4_LEDGER_DIR=str(Path(_IMPORT_ROOT.name) / 'v4-ledger'),
                  V4_INBOX_DIR=str(Path(_IMPORT_ROOT.name) / 'v4-inbox'))
import fold
import ledger_cache
import look
import sweep_owed
import tick

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
SID = 'L-spec-9901'


def event(kind='spec-written', **fields):
    return {**dict(ts='2026-01-01T00:00:00+00:00', type=kind, subject=SID,
                   project='demo'), **fields}


class CountedRows(tuple):
    """Count the actual canonical traversal and shallow copies on each return."""
    def __iter__(self):
        self.passes += 1
        for row in super().__iter__():
            self.rows += 1
            yield row


@contextmanager
def copy_counter():
    counts = []
    original = fold.ReaderSnapshot._copy
    def copy(snapshot):
        rows = CountedRows(snapshot._events)
        rows.passes = rows.rows = 0
        snapshot._events = rows
        result = original(snapshot)
        assert rows.passes == 1, rows.passes
        assert rows.rows == len(rows), (rows.rows, len(rows))
        counts.append((rows.passes, rows.rows))
        return result
    with patch.object(fold.ReaderSnapshot, '_copy', new=copy):
        yield counts


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reader-snapshot-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(fold, ROOT=self.root, EVENTS=self.events,
                                               PROJECT=None, NOW=NOW))
        self.stack.enter_context(patch.dict(os.environ, DOIT_READ_CACHE='1'))
        # A stray external boundary is a test failure, not a live operation.
        def forbidden(*args, **kwargs):
            raise AssertionError('external process attempted')
        self.stack.enter_context(patch.object(subprocess, 'Popen', side_effect=forbidden))
        self.original_finish = fold.finish_events
        self.snapshot = fold.ReaderSnapshot()

    def write(self, name, rows, mode='w'):
        path = self.events / name
        with path.open(mode) as stream:
            for row in rows:
                stream.write(json.dumps(row) + '\n')
        # Bound every fixture independently of its input generator.
        files = list(self.events.glob('*.jsonl'))
        self.assertLessEqual(len(files), 12)
        self.assertLessEqual(sum(len(p.read_text().splitlines()) for p in files), 60)
        return path

    def reference(self):
        return self.original_finish(fold._read_events_uncached())

    @contextmanager
    def counted(self):
        with patch.object(ledger_cache, '_read', wraps=ledger_cache._read) as read, \
                patch.object(ledger_cache, '_unpack', wraps=ledger_cache._unpack) as unpack, \
                patch.object(fold, 'finish_events', wraps=self.original_finish) as finish, \
                copy_counter() as copies:
            yield read, unpack, finish, copies

    def read(self):
        return fold.read_events(snapshot=self.snapshot)

    def test_ac1_reuse_isolation_and_bypass(self):
        self.write('L-spec-writer-fixture.jsonl', [event(payload={'rows': [{'n': 1}]})])
        self.write('L-operator-fixture.jsonl', [event('correction',
            ref='L-spec-writer-fixture.jsonl:1', set={'payload': {'rows': [{'n': 2}]}})])
        reference = self.reference()
        applied = json.dumps(fold.APPLIED)
        with self.counted() as (read, unpack, finish, copies):
            first = self.read()
            self.assertEqual(first, reference)
            self.assertEqual(json.dumps(fold.APPLIED), applied)
            loads = unpack.call_count
            # Canonical nested containers preserve JSON types, equality and encoding.
            self.assertEqual(json.dumps(first), json.dumps(reference))
            mutable = next(e for e in first if e['actor'] == 'spec-writer')
            with self.assertRaises(TypeError):
                mutable['payload']['rows'][0]['n'] = 90
            with self.assertRaises(TypeError):
                mutable['payload']['rows'].append({})
            mutable['payload'] = {'rows': ['caller replacement']}
            fold.APPLIED[0]['set'] = {'project': 'poison'}
            open_original = Path.open
            def no_body(path, *args, **kwargs):
                if path.suffix == '.jsonl':
                    raise AssertionError('unchanged hit read a JSONL body')
                return open_original(path, *args, **kwargs)
            with patch.object(Path, 'open', new=no_body), \
                    patch.object(ledger_cache, '_generation', wraps=ledger_cache._generation) as generation, \
                    patch.object(fold, '_immutable', side_effect=AssertionError('hit froze nested data')):
                second = self.read()
            self.assertEqual(generation.call_count, 1)
            self.assertEqual(second, reference)
            self.assertEqual(json.dumps(fold.APPLIED), applied)
            self.assertEqual((read.call_count, finish.call_count, unpack.call_count), (1, 1, loads))
            self.assertEqual([p for p, _ in copies], [1, 1])
            # No-snapshot still materializes and normalizes on every call.
            self.assertEqual(fold.read_events(), reference)
            self.assertEqual(fold.read_events(), reference)
            self.assertEqual((read.call_count, finish.call_count), (3, 3))
            with patch.object(ledger_cache, 'read_conditional', side_effect=AssertionError('bypass used cache')):
                self.assertEqual(fold.read_events(use_cache=False, snapshot=self.snapshot), reference)
                with patch.dict(os.environ, DOIT_READ_CACHE='0'):
                    self.assertEqual(self.read(), reference)
            self.assertIsNone(self.snapshot._generation)
        # Explicitly distinguish an unchanged empty ledger from a loaded empty one.
        for path in self.events.glob('*.jsonl'):
            path.unlink()
        result = ledger_cache.read_conditional(self.events, self.root / 'cache', fold.actor_of)
        self.assertEqual(result.events, [])
        self.assertIsNone(ledger_cache.read_conditional(self.events, self.root / 'cache',
            fold.actor_of, previous=result.generation).events)
        print('AC1 counters: initial _read=1 finish_events=1; hit body/unpack/normalize=0; copy passes=1')
        print('L-spec-1711 AC1 ok')

    def test_ac2_generations_failures_and_recovery(self):
        path = self.write('L-spec-writer-fixture.jsonl', [event(title='before')])
        with self.counted() as (read, unpack, finish, copies):
            expected_loads = 0
            def changed():
                nonlocal expected_loads
                expected_loads += 1
                current = self.read()
                self.assertEqual(current, self.reference())
                self.assertEqual((read.call_count, finish.call_count), (expected_loads, expected_loads))
                unpacked = unpack.call_count
                self.assertEqual(self.read(), current)
                self.assertEqual((read.call_count, finish.call_count, unpack.call_count),
                                 (expected_loads, expected_loads, unpacked))
                return current
            first = changed()
            directory_mtime = self.events.stat().st_mtime_ns
            self.write(path.name, [event('build-started')], 'a')
            os.utime(self.events, ns=(directory_mtime, directory_mtime))
            appended = changed()
            self.assertEqual(len(appended), len(first) + 1)
            self.assertEqual(first[0]['title'], 'before')
            self.assertEqual(len(first), 1)
            extra = self.write('L-executor-other.jsonl', [event('shipped')])
            changed()
            extra.unlink()
            changed()
            path = self.write(path.name, [event(title='before')])  # truncate
            changed()
            old = path.stat()
            path.write_text(path.read_text().replace('before', 'repair'))  # same size
            self.assertEqual(path.stat().st_size, old.st_size)
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
            self.assertEqual(changed()[0]['title'], 'repair')
            replacement = self.events / 'replacement.part'
            replacement.write_bytes(path.read_bytes())
            old_inode = path.stat().st_ino
            replacement.replace(path)
            self.assertNotEqual(old_inode, path.stat().st_ino)
            changed()
            # A torn tail is reparsed from its start when completed.
            with path.open('a') as stream:
                stream.write('{"ts":"2026-01-03","type":"build-done",')
            changed()
            with path.open('a') as stream:
                stream.write('"subject":"L-spec-9901","project":"demo"}\n')
            self.assertEqual(len(changed()), 2)
            with patch.object(fold, 'PROJECT', 'elsewhere'):
                self.assertEqual(changed(), [])
            changed()
            other_root = self.root / 'other'
            with patch.object(fold, 'ROOT', other_root):
                changed()
            changed()
            other_events = self.root / 'elsewhere'
            other_events.mkdir()
            with patch.object(fold, 'EVENTS', other_events):
                self.assertEqual(changed(), [])
            changed()
        # Actual bounded lock acquisition failure must invalidate, never serve stale.
        ident = hashlib.sha256(os.fsencode(self.events.resolve())).hexdigest()[:16]
        lock = self.root / 'cache' / f'ledger-{ident}.lock'
        with lock.open('rb') as held, patch.dict(os.environ, DOIT_LEDGER_LOCK_WAIT_S='0'):
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ledger_cache.LockTimeout):
                self.read()
            self.assertIsNone(self.snapshot._generation)
        self.assertEqual(self.read(), self.reference())
        # A failed cache read preserves reference fallback but cannot publish it.
        with patch.object(ledger_cache, 'read_conditional', side_effect=OSError('fixture failure')):
            self.assertEqual(self.read(), self.reference())
            self.assertIsNone(self.snapshot._generation)
        with patch.object(ledger_cache, '_read', wraps=ledger_cache._read) as read:
            self.assertEqual(self.read(), self.reference())
            self.assertEqual(read.call_count, 1)
        with patch.object(ledger_cache, 'read_conditional', side_effect=OSError('cache failure')), \
                patch.object(fold, '_read_events_uncached', side_effect=OSError('source failure')):
            with self.assertRaises(OSError):
                self.read()
            self.assertIsNone(self.snapshot._generation)
        # Payload corruption still rebuilds on a fresh load (no reusable snapshot).
        cache = self.root / 'cache' / f'ledger-{ident}.sqlite3'
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute("UPDATE chunks SET data=X'00'")
        self.assertEqual(self.read(), self.reference())
        # Associate events with their validated metadata, even if a writer races
        # immediately AFTER _read: the following read must see the append.
        self.snapshot = fold.ReaderSnapshot()
        original_read = ledger_cache._read
        def racing_read(db):
            result = original_read(db)
            self.write(path.name, [event('build-started', title='raced')], 'a')
            return result
        with patch.object(ledger_cache, '_read', side_effect=racing_read):
            before_race = self.read()
        after_race = self.read()
        self.assertEqual(len(after_race), len(before_race) + 1)
        self.assertEqual(len(before_race), 2)
        print('AC2 append/create/delete/truncate/repair/replace/tail/scope/lock/fallback/corruption/race ok')
        print('L-spec-1711 AC2 ok')

    def test_ac3_fresh_derivation_and_semantics(self):
        self.write('L-spec-writer-fixture.jsonl', [
            event(project_wrong='unused'),
            event('owed-ac', criterion='AC1', line='fixture', wake_at=NOW.isoformat()),
            event('build-started'), event('build-done')])
        self.write('L-executor-fixture.jsonl', [event('shipped')])
        self.write('L-intruder-fixture.jsonl', [event('shipped', actor='executor')])
        self.write('L-operator-fixture.jsonl', [event('correction', project='other',
            ref='L-spec-writer-fixture.jsonl:1',
            set={'project': 'demo', 'title': 'corrected', 'actor': 'intruder', '_src': 'forged'})])
        # Repair the initial wrong project, making correction-before-filter observable.
        path = self.events / 'L-spec-writer-fixture.jsonl'
        path.write_text(path.read_text().replace('"project": "demo"', '"project": "wrong"', 1))
        with patch.object(fold, 'PROJECT', 'demo'), self.counted() as (read, unpack, finish, _):
            states = []
            for clock in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)):
                with patch.object(fold, 'NOW', clock):
                    current = self.read()
                    applied = list(fold.APPLIED)
                    reference = self.reference()
                    self.assertEqual(current, reference)
                    self.assertEqual(applied, fold.APPLIED)
                    actual = fold.fold(current)
                    self.assertEqual(actual, fold.fold(reference))
                    states.append(actual[0][SID]['state'])
                    self.assertEqual(actual[0][SID]['age'], fold.age_days(actual[0][SID]['evs'][-1]))
                    self.assertTrue(any(e['actor'] == 'intruder' for e in actual[2]))
                    source_rows = [e for e in current if e['actor'] == 'spec-writer']
                    self.assertEqual([e['_src'] for e in source_rows],
                        [f'L-spec-writer-fixture.jsonl:{i}' for i in range(1, 5)])
                    self.assertEqual(source_rows[0]['title'], 'corrected')
            self.assertEqual(states, ['shipped-owed-evidence', 'shipped-owed-due'])
            self.assertEqual((read.call_count, finish.call_count), (1, 1))
            self.assertEqual(set(vars(self.snapshot)),
                {'_events', '_visible', '_applied', '_scope', '_generation'})
        print('AC3 controlled-clock waiting/due reference parity; actor/correction/ordering ok; loads=1 normalizations=1')
        print('L-spec-1711 AC3 ok')

    def patch_call(self, stack, module, name, value=None, side_effect=None):
        return stack.enter_context(patch.object(module, name, return_value=value, side_effect=side_effect))

    def stub_tick(self, stack):
        for module_name, function, value in [
            ('intake', 'run', {}), ('notes', 'run', None), ('pr_closeout', 'run', None),
            ('install_sync', 'run', []), ('push_origin', 'run', []),
            ('carry', 'uncarried', []), ('carry', 'sync_v4', None),
            ('pane_resume', 'run', None), ('crons', 'check', []),
            ('look', '_read_state', {'last_pass': NOW.isoformat()}),
            ('look', 'heartbeat_missed', None), ('look', 'emit_once', None),
            ('up', 'ensure_relay', 'ok'), ('autodispatch', 'redispatch_dead', []),
            ('merge_queue', 'enqueue_due', []), ('resources', 'manifest_counts', {}),
            ('resources', 'not_reapable_counts', {}), ('reap_tmp', '_reap_recorded', []),
            ('tree_cleanup', 'reap_worktrees', ([], [])), ('dispatch', 'emit', None),
        ]:
            self.patch_call(stack, importlib.import_module(module_name), function, value)
        self.patch_call(stack, tick, '_steps', [])
        self.patch_call(stack, tick, 'run_steps', [])
        self.patch_call(stack, tick, '_log_lane_driver')
        self.patch_call(stack, tick, 'in_flight', set())
        self.patch_call(stack, tick, '_carry_packet_busy', set())

    def stub_sweep(self, stack):
        stack.enter_context(patch.object(sweep_owed.dispatch, 'ROOT', self.root))
        for module, name, value in [
            (sweep_owed, 'reap_repo_cwds', []), (sweep_owed, 'sweep_expired', []),
            (sweep_owed.owed_failures, 'escalate_failures', []),
            (sweep_owed, 'close_unverifiable', []), (sweep_owed, 'readmitted', []),
            (sweep_owed, 'due_candidates', []), (sweep_owed, '_batch_for', (None, [])),
            (sweep_owed.provision, 'script_rows', ([], [])),
            (sweep_owed.prod_runner, 'batch_for_browser', []),
            (sweep_owed.prod_browser, 'split', ([], [])),
            (sweep_owed, 'judgment_rows', ([], [])), (sweep_owed, 'open_batch', None),
            (sweep_owed, '_emit', None), (sweep_owed.verifier, 'run', []),
        ]:
            self.patch_call(stack, module, name, value)
        self.patch_call(stack, sweep_owed, 'dispatch_batch',
                        side_effect=AssertionError('dispatch attempted'))
        self.patch_call(stack, sweep_owed.prod_runner, 'run',
                        {'lines': [], 'held': set(), 'bundle_rows': {}, 'to_judgment': set()})

    def test_ac4_drivers_and_look(self):
        self.write('L-spec-writer-fixture.jsonl', [event()])
        for driver in ('tick', 'sweep'):
            for append in (False, True):
                with ExitStack() as stack, self.counted() as (read, unpack, finish, copies):
                    calls, retained = [], []
                    original = fold.read_events
                    def spy(*args, **kwargs):
                        calls.append(kwargs.get('snapshot'))
                        result = original(*args, **kwargs)
                        retained.append(result)
                        return result
                    stack.enter_context(patch.object(fold, 'read_events', side_effect=spy))
                    if driver == 'tick':
                        self.stub_tick(stack)
                        if append:
                            self.patch_call(stack, tick.intake, 'run', side_effect=lambda ev:
                                self.write('L-builder-appended.jsonl', [event('build-started')], 'a') and {})
                        tick._record()
                        self.assertEqual(len(calls), 2)
                    else:
                        self.stub_sweep(stack)
                        if append:
                            # An escalation is observed before the runner; a runner
                            # append is observed before resolution; a quarantine is
                            # observed at _resolve_candidates' existing reread.
                            def escalate(*args, **kwargs):
                                self.write('L-builder-appended.jsonl', [event('build-started')], 'a')
                                return ['sweep-owed: escalated fixture']
                            self.patch_call(stack, sweep_owed.owed_failures, 'escalate_failures', side_effect=escalate)
                            def observe(*args, **kwargs):
                                self.assertEqual(len(kwargs['events']), len(retained[0]) + 1)
                                self.write('L-builder-appended.jsonl', [event('build-done')], 'a')
                                return {'lines': [], 'held': set(), 'bundle_rows': {}, 'to_judgment': set()}
                            self.patch_call(stack, sweep_owed.prod_runner, 'run', side_effect=observe)
                            def close(*args, **kwargs):
                                self.write('L-operator-quarantine.jsonl', [event('owed-quarantined',
                                    criterion='AC1', reason='fixture', spec_sha256='fixture')], 'a')
                                return []
                            self.patch_call(stack, sweep_owed, 'close_unverifiable', side_effect=close)
                        # Assert the retry view stays owned by the pre-observation fold.
                        with patch.object(sweep_owed, 'readmitted', return_value=[]) as retry, redirect_stdout(io.StringIO()):
                            sweep_owed.run(now=NOW, runner=SimpleNamespace())
                        self.assertIs(retry.call_args_list[0].args[0], retry.call_args_list[-1].args[0])
                        self.assertEqual(len(calls), 4 if append else 3)
                    self.assertTrue(all(isinstance(s, fold.ReaderSnapshot) for s in calls))
                    self.assertTrue(all(s is calls[0] for s in calls))
                    expected = (len(calls) if append else 1)
                    self.assertEqual((read.call_count, finish.call_count), (expected, expected))
                    self.assertEqual(len(copies), len(calls))
                    self.assertTrue(all(passes == 1 for passes, _ in copies))
                    if append:
                        self.assertGreater(len(retained[-1]), len(retained[0]))
                        self.assertEqual(len(retained[0]), len(self.reference()) - (len(calls) - 1))
        self.look_evaluations()
        # Protect the pinned read-site count, without importing/running up.
        source_root = Path(__file__).parent
        count = 0
        for name in ('tick.py', 'up.py'):
            tree = ast.parse((source_root / name).read_text())
            count += sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'fold'
                and n.func.attr == 'read_events' for n in ast.walk(tree))
        self.assertEqual(count, 8)
        print('AC4 tick reads=2; sweep reads=3/4; materializations=generations; copy passes per read=1; look folds=1/evaluation')
        print('L-spec-1711 AC4 ok')

    def look_evaluations(self):
        consumers = {'_check_dispatchable_stale', '_check_grades_per_shipped',
                     '_check_spec_idle', '_check_lane_stale'}
        import autodispatch
        import lane_rules
        with ExitStack() as stack:
            # Every unrelated check is an explicit stub, including production,
            # proc/session scans, cleanup, cron, network and subprocess work.
            for name in vars(look):
                if name.startswith('_check_') and name not in consumers:
                    self.patch_call(stack, look, name)
            self.patch_call(stack, look, '_clear')
            settle = self.patch_call(stack, look, '_settle')
            undetermined = self.patch_call(stack, look, '_fire_undetermined')
            self.patch_call(stack, look, 'load_toml', {'thresholds': look.DEFAULTS['thresholds'], 'prod': []})
            self.patch_call(stack, look, '_call', ('', None))
            self.patch_call(stack, lane_rules, 'load', {'build': {'limit_min': 10}})
            self.patch_call(stack, autodispatch, 'candidates', [])
            self.patch_call(stack, fold, 'held_specs', set())
            self.patch_call(stack, look, '_spec_idle_bad', False)
            self.patch_call(stack, tick, '_steps', [])
            runner = SimpleNamespace(clock=lambda: 0)
            inputs = []
            for name in consumers:
                original = getattr(look, name)
                def consumer(*args, _original=original, **kwargs):
                    if 'folded' in kwargs:
                        inputs.append(kwargs['folded'])
                    return _original(*args, **kwargs)
                stack.enter_context(patch.object(look, name, side_effect=consumer))
            observed_clocks = []
            original_fold = fold.fold
            def derive(events):
                observed_clocks.append(fold.NOW)
                return original_fold(events)
            events = self.reference()
            with patch.object(fold, 'fold', side_effect=derive) as derivation:
                for clock in (NOW, NOW + timedelta(hours=1)):
                    look.run(events, now=clock, runner=runner, root=self.root, dry_run=True)
                    self.assertEqual(derivation.call_count, len(observed_clocks))
                self.assertEqual(derivation.call_count, 2)
            self.assertEqual(observed_clocks, [NOW, NOW + timedelta(hours=1)])
            self.assertEqual(len(inputs), 8)
            self.assertTrue(all(value is inputs[0] for value in inputs[:4]))
            self.assertTrue(all(value is inputs[4] for value in inputs[4:]))
            self.assertIsNot(inputs[0], inputs[4])
            self.assertEqual(fold.NOW, NOW)
            self.assertEqual(undetermined.call_count, 0)
            self.assertTrue(settle.called)
            # Direct helpers still derive when no optional input is supplied.
            with patch.object(fold, 'fold', wraps=original_fold) as derive:
                look._check_dispatchable_stale([], NOW, self.root, [])
                look._check_spec_idle([], NOW, self.root, [], [])
                look._check_lane_stale([], NOW, self.root, [])
                look._check_grades_per_shipped([], NOW, self.root, [], [], look.DEFAULTS['thresholds'])
                fold.grades_per_shipped([], NOW)
                self.assertEqual(derive.call_count, 5)
            # The three guarded helpers keep their undetermined result; grades
            # retains its existing propagated error rather than fabricating data.
            failure = ValueError('fixture derivation failed')
            for name, args in [
                ('_check_dispatchable_stale', ([], NOW, self.root, [])),
                ('_check_spec_idle', ([], NOW, self.root, [], [])),
                ('_check_lane_stale', ([], NOW, self.root, [])),
            ]:
                getattr(look, name)(*args, folded=failure)
            self.assertEqual([call.args[0] for call in undetermined.call_args_list],
                             ['dispatch-stale', 'spec-idle', 'lane-stale'])
            with patch.object(fold, 'fold', side_effect=failure) as derive:
                with self.assertRaisesRegex(ValueError, 'fixture derivation failed'):
                    look.run([], now=NOW, root=self.root, runner=runner, dry_run=True)
                self.assertEqual(derive.call_count, 1)
            self.assertEqual(fold.NOW, NOW)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _IMPORT_ROOT.cleanup()
