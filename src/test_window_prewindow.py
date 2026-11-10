"""Prewindow admission: real Git trees; no CI, database, or real ledger."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

_root = tempfile.TemporaryDirectory(prefix='prewindow-import-')
os.environ['DOIT_ROOT'] = _root.name
os.environ['DOIT_NO_POKE'] = '1'
import fold
import window_gate

VERSIONS = 'api/alembic_supabase/versions/'
CENSUS = 'dashboard/src/lib/server-identity.test.ts'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='prewindow-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / 'repos/albert-scott'
        self.repo.mkdir(parents=True)
        self.git('init', '-b', 'master')
        self.write('base.txt', 'base\n')
        self.write(VERSIONS + 'root.py', "revision = 'root'\ndown_revision = None\n")
        self.write(CENSUS, '// initial census\n')
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.events = []
        self.entries = []
        self.patch_root = patch.object(fold, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def git(self, *args):
        p = subprocess.run(['git', '-c', 'user.name=fixture', '-c',
                            'user.email=fixture@localhost', *args], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-m', 'fixture')

    def branch(self, number, files, *, parent='master', writes=None, dependency=None):
        sid = f'L-spec-{number}'
        branch = f'br-{number}'
        self.git('checkout', '-b', branch, parent)
        for path, text in files.items():
            self.write(path, text)
        self.commit()
        content = self.root / 'content'
        content.mkdir(exist_ok=True)
        text = 'Writes: ' + ', '.join(writes or files) + '\n'
        if dependency:
            text += f'Dependency-hold: waits_for={dependency}\n'
        (content / (sid + '.md')).write_text(text)
        entry = (sid, branch, self.git('rev-parse', 'HEAD'))
        self.entries.append(entry)
        return entry

    def select(self, entries=None):
        import window_prewindow
        return window_prewindow.select(self.repo, self.base, 'master',
                                      self.entries if entries is None else entries,
                                      self.events, self.root / 'content')


class PrewindowIntegrationTests(Fixture):
    def test_deferred_spec_stays_merge_ready_after_window_closes(self):
        import lane_rules
        import merge_queue
        import nextstep
        a = self.branch(1820, {'one.txt': 'one'}, writes=['one.txt', 'shared.txt'])
        b = self.branch(1824, {'two.txt': 'two'}, writes=['two.txt', 'shared.txt'])
        events = self.root / 'events'
        events.mkdir()
        now = datetime.now(timezone.utc)
        output = io.StringIO()
        with patch.object(fold, 'EVENTS', events), \
             patch.dict(os.environ, DOIT_ROOT=str(self.root),
                        DOIT_GATE_LEDGER_FILE='L-executor-prewindow.jsonl'), \
             patch.object(window_gate.quarantine, 'apply'), \
             patch.object(window_gate, 'required_checks', return_value=[window_gate.PYTEST_CHECK]), \
             patch.object(window_gate, 'base_red_snapshot', return_value={}), \
             patch.object(window_gate, 'scope', return_value=dict(checks=[window_gate.PYTEST_CHECK], reason='fixture')), \
             patch.object(window_gate, 'verify', return_value=dict(state='green', nodes=[], url='', doc={})), \
             contextlib.redirect_stdout(output):
            for entry in (a, b):
                for minutes, ledger, kind, fields in [
                    (3, 'reviewer', 'review', dict(n_blocking=0)),
                    (2, 'executor', 'merge-gate-clean', dict(branch=entry[1])),
                    (1, 'operator', 'queue-add', dict(branch=entry[1], rule='operator')),
                ]:
                    with patch.dict(os.environ, DOIT_LEDGER_FILE='L-' + ledger + '-prewindow.jsonl'), \
                         patch.object(fold, 'NOW', now - timedelta(minutes=minutes)):
                        fold.append([kind, entry[0], 'project=albert-scott'] +
                                    [key + ':=' + json.dumps(value) for key, value in fields.items()])
            rc = window_gate._run_attempt('fixture', 'albert-scott', [a[0], b[0]], 'master',
                                         False, self.base, self.base, None, 0, False)
            rows = fold.read_events()
            step = nextstep.step_for(rows, {b[0]: dict(state='graded')}, b[0],
                                     datetime.now(timezone.utc), lane_rules.load())
            later_base = self.git('rev-parse', 'master')
            later_rc = window_gate._run_attempt('later', 'albert-scott', [b[0]], 'master',
                                                False, later_base, later_base, None, 0, False)
            later_rows = fold.read_events()
        self.assertEqual(step.action, 'merge', step)
        self.assertEqual([row['spec'] for row in merge_queue.queue(rows, 'albert-scott')], [b[0]])
        verified = next(row for row in rows if row['type'] == 'window-verified')
        self.assertEqual(verified['kept'], [a[0]])
        self.assertEqual(verified['dropped'], [])
        self.assertFalse(any(row['type'] == 'window-dropped' for row in rows))
        self.assertIn('deferred ' + b[0], output.getvalue())
        self.assertEqual(output.getvalue().count('deferred ' + b[0]), 1)
        self.assertEqual(rc, 0)
        self.assertEqual(later_rc, 0)
        self.assertFalse(merge_queue.queue(later_rows, 'albert-scott'))
        self.assertTrue(any(row['type'] == 'shipped' and row['subject'] == b[0] for row in later_rows))

    def test_targeted_probe_cannot_build_child_without_its_parent(self):
        a = self.branch(1, {'one.txt': 'one'})
        b = self.branch(2, {'two.txt': 'two'}, dependency=a[0])
        node = 'api/tests/absent.py::test_bad'
        failures = dict(pytest={node}, **{'pg-parity': set(), 'vitest': set(),
                        'suite-error': set(), 'undetermined': [], 'pg_files': {}})
        base_red = types.SimpleNamespace(
            classify=lambda *a, **kw: dict(complete=True, pre_existing=[], new=[node]),
            recheck=lambda *a: dict(green=[node]))
        emitted, inspected, output = [], [], io.StringIO()

        def attribute(repo, base, entries, ids, *, prepare):
            # A diagnostic single must pass the same dependency admission as a
            # shipping candidate. It must remain unknown if its parent is absent.
            candidate = prepare([b[0]])
            inspected.append(candidate)
            self.assertIsNone(candidate)
            pair = prepare([a[0], b[0]])
            self.assertIsNotNone(pair)
            self.assertIn('one.txt', self.git('ls-tree', '--name-only', pair))
            self.assertIn('two.txt', self.git('ls-tree', '--name-only', pair))
            return dict(drop={}, proven={}, environment=[], unresolved=[node],
                        need_pairs=[], mode='singles', base_sha=base, evidence={})

        with patch.dict(sys.modules, base_red=base_red), \
             patch.object(window_gate.quarantine, 'apply'), \
             patch.object(window_gate, 'train_events', return_value=[]), \
             patch.object(window_gate, 'resolve_entries', return_value=[a, b]), \
             patch.object(window_gate, 'required_checks', return_value=[window_gate.PYTEST_CHECK]), \
             patch.object(window_gate, 'base_red_snapshot', return_value=dict(complete=True)), \
             patch.object(window_gate, 'scope', return_value=dict(checks=[window_gate.PYTEST_CHECK], reason='fixture')), \
             patch.object(window_gate, 'verify', return_value=dict(state='red', nodes=[node], url='', doc={})), \
             patch.object(window_gate, 'candidate_failures', return_value=failures), \
             patch('window_attribution.attribute', side_effect=attribute), \
             patch.object(window_gate, 'ev', side_effect=lambda typ, subject, **kw:
                          emitted.append(dict(type=typ, subject=subject, **kw))), \
             contextlib.redirect_stdout(output):
            self.assertEqual(window_gate._run_attempt('fixture', 'albert-scott', [], 'master',
                             False, self.base, self.base, None, 0, False), 2)
        self.assertEqual(inspected, [None])
        drops = [e for e in emitted if e['type'] == 'window-dropped']
        self.assertEqual(drops, [])
        self.assertIn('deferred ' + b[0], output.getvalue())
        self.assertIn('waits_for', output.getvalue())

    def test_drop_round_trips_through_isolated_executor_ledger(self):
        entry = self.branch(1927, {'dashboard/src/lib/new-caller.ts': 'backendFetch("/x")'})
        events = self.root / 'events'
        events.mkdir()
        append = fold.append
        with patch.object(fold, 'EVENTS', events), \
             patch.dict(os.environ, DOIT_ROOT=str(self.root),
                        DOIT_GATE_LEDGER_FILE='L-executor-prewindow.jsonl'), \
             patch.object(fold, 'append', side_effect=lambda args: append(args, strict=True)), \
             patch.object(window_gate.quarantine, 'apply'), \
             patch.object(window_gate, 'resolve_entries', return_value=[entry]), \
             patch.object(window_gate, 'required_checks') as ci, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = window_gate._run_attempt('fixture', 'albert-scott', [], 'master',
                                         False, self.base, self.base, None, 0, False)
            rows = fold.read_events()
        self.assertEqual(rc, 1)
        self.assertFalse(ci.called)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['actor'], 'executor')
        self.assertEqual(rows[0]['reason'], 'prewindow')
        self.assertEqual(rows[0]['nodes'], [])
        self.assertIn('census', rows[0]['why'])

    def test_unsafe_branch_never_reaches_ci_or_candidate(self):
        entry = self.branch(1927, {'dashboard/src/lib/new-caller.ts':
                                  "import { backendFetch } from './backend';\n"})
        emitted = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(window_gate.quarantine, 'apply'))
            stack.enter_context(patch.object(window_gate, 'train_events', return_value=[]))
            stack.enter_context(patch.object(window_gate, 'resolve_entries', return_value=[entry]))
            stack.enter_context(patch.object(window_gate, 'ev', side_effect=
                               lambda typ, subject, **kw: emitted.append(dict(type=typ, subject=subject, **kw))))
            barriers = [stack.enter_context(patch.object(window_gate, name,
                        side_effect=AssertionError(name + ' ran before admission')))
                        for name in ('required_checks', 'base_red_snapshot', 'build_candidate')]
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            rc = window_gate._run_attempt('fixture', 'albert-scott', [], 'master',
                                         False, self.base, self.base, None, 0, False)
        self.assertEqual(rc, 1)
        self.assertTrue(all(not barrier.called for barrier in barriers))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]['reason'], 'prewindow')
        self.assertIn('census', emitted[0]['why'])
        self.assertEqual(emitted[0]['branch'], entry[1])
        self.assertFalse(self.git('for-each-ref', 'refs/heads/window/'))

    def test_good_sibling_reaches_ci_setup_after_bad_branch_is_dropped(self):
        bad = self.branch(1, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'})
        good = self.branch(2, {'safe.txt': 'safe'})
        emitted = []
        with patch.object(window_gate.quarantine, 'apply'), \
             patch.object(window_gate, 'train_events', return_value=[]), \
             patch.object(window_gate, 'resolve_entries', return_value=[bad, good]), \
             patch.object(window_gate, 'ev', side_effect=lambda *args, **kw: emitted.append((args, kw))), \
             patch.object(window_gate, 'required_checks', side_effect=LookupError('CI setup reached')) as ci:
            with self.assertRaisesRegex(LookupError, 'CI setup reached'):
                window_gate._run_attempt('fixture', 'albert-scott', [], 'master',
                                         False, self.base, self.base, None, 0, False)
        self.assertEqual(ci.call_count, 1)
        self.assertEqual(emitted[0][0], ('window-dropped', bad[0]))
        self.assertEqual(emitted[0][1]['reason'], 'prewindow')

    def test_good_sibling_ships_and_preflight_drop_survives_final_result(self):
        bad = self.branch(1, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'})
        good = self.branch(2, {'safe.txt': 'safe'})
        events = self.root / 'events'
        events.mkdir()
        with patch.object(fold, 'EVENTS', events), \
             patch.dict(os.environ, DOIT_ROOT=str(self.root),
                        DOIT_GATE_LEDGER_FILE='L-executor-prewindow.jsonl'), \
             patch.object(window_gate.quarantine, 'apply'), \
             patch.object(window_gate, 'resolve_entries', return_value=[bad, good]), \
             patch.object(window_gate, 'required_checks', return_value=[window_gate.PYTEST_CHECK]), \
             patch.object(window_gate, 'base_red_snapshot', return_value={}), \
             patch.object(window_gate, 'scope', return_value=dict(checks=[window_gate.PYTEST_CHECK], reason='fixture')), \
             patch.object(window_gate, 'verify', return_value=dict(state='green', nodes=[], url='', doc={})) as ci, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = window_gate._run_attempt('fixture', 'albert-scott', [], 'master',
                                         False, self.base, self.base, None, 0, False)
            rows = fold.read_events()
        self.assertEqual(rc, 1)
        self.assertEqual(ci.call_count, 1)
        verified = next(row for row in rows if row['type'] == 'window-verified')
        self.assertEqual(verified['kept'], [good[0]])
        self.assertEqual(verified['dropped'], [bad[0]])
        self.assertEqual([row['subject'] for row in rows if row['type'] == 'shipped'], [good[0]])
        self.assertEqual(self.git('show', 'master:safe.txt'), 'safe')
        self.assertNotIn('dashboard/src/lib/caller.ts', self.git('ls-tree', '-r', '--name-only', 'master'))


class PrewindowTests(Fixture):
    def test_2111_ac14(self):
        import rechain
        import window_prewindow
        with patch.dict(os.environ):
            from test_rechain import Fixture as MigrationFixture, response
        with tempfile.TemporaryDirectory() as root:
            fx = MigrationFixture(root)
            self.repo = fx.repo
            self.base = fx.base
            a = fx.branch('a', mutation=lambda f: f.write('outside.txt', 'master edit\n'))
            b = fx.branch('b')
            c = fx.branch('c', mutation=lambda f: f.write('outside.txt', 'branch edit\n'))
            content = self.root / 'content'
            content.mkdir(exist_ok=True)
            for entry in (a, b, c):
                paths = self.git('diff', '--name-only', fx.base, entry[2]).splitlines()
                (content / (entry[0] + '.md')).write_text('Writes: ' + ', '.join(paths) + '\n')
            self.git('merge', '--no-ff', '-m', 'land a', a[2])
            master = self.git('rev-parse', 'master')
            self.base = master
            observed = []
            real_git = window_prewindow.git
            def record(repo, *args, **kw):
                result = real_git(repo, *args, **kw)
                if 'commit-tree' in args:
                    observed.append(rechain.routed_head(repo, result.stdout.strip()))
                return result
            with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='on'), \
                 patch.object(window_prewindow, 'git', side_effect=record):
                self.assertEqual(self.select([b]), ([b], []))
            self.assertEqual(observed, ['b'])
            with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='off'), \
                 patch.object(rechain, 'resolve') as resolve:
                self.assertEqual(self.select([b]), ([], [(b, 'textual merge is not clean')]))
                resolve.assert_not_called()
            with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='on'):
                self.assertEqual(self.select([c]), ([], [(c, 'textual merge is not clean')]))
                with patch.object(rechain, 'resolve', side_effect=OSError):
                    self.assertEqual(self.select([b]), ([], [(b, 'textual merge is not clean')]))
                # Peer migrations off one base clash on bookkeeping; the resolver
                # (reached through the cumulative merge's entry) admits both.
                peer_root = pathlib.Path(root) / 'peer'
                peer = MigrationFixture(peer_root)
                first, second = peer.branch('L-spec-9101'), peer.branch('L-spec-9102')
                for entry in (first, second):
                    (content / (entry[0] + '.md')).write_text('Writes: ' + entry[0] + '.py\n')
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    kept, drops = window_prewindow.select(peer.repo, peer.base, 'master',
                                                          [first, second], [], content)
                self.assertEqual((kept, drops), ([first, second], []))
                self.assertNotIn('deferred', output.getvalue())
            self.assertEqual(fx.events, [])
        response('2111-AC14', dict(prefix_head='b', off_and_error_drop=True,
                                  unrelated_drop=True, peer_rechained=True, events=0))

    def assertDeferred(self, fragment, entries=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kept, drops = self.select(entries)
        self.assertEqual(drops, [])
        self.assertIn('deferred', output.getvalue())
        self.assertIn(fragment, output.getvalue())
        return kept, drops

    def test_peer_text_conflict_is_deferred(self):
        self.branch(1, {'base.txt': 'first\n'}, writes=['one.txt'])
        self.branch(2, {'base.txt': 'second\n'}, writes=['two.txt'])
        kept, _ = self.assertDeferred('merge with earlier window members')
        self.assertEqual(kept, self.entries[:1])

    def test_unreadable_master_retains_all_entries_for_ci(self):
        self.branch(1, {'one.txt': 'one'})
        self.branch(2, {'two.txt': 'two'})
        self.git('branch', '-D', 'master')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.select(), (self.entries, []))
        self.assertIn('master', output.getvalue())
        self.assertIn('cannot determine Git', output.getvalue())

    def test_master_blob_read_error_retains_all_entries_for_ci(self):
        import window_prewindow
        self.branch(1, {'one.txt': 'one'})
        output = io.StringIO()
        with patch.object(window_prewindow.Trees, 'read', side_effect=OSError('fixture Git read error')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(self.select(), (self.entries, []))
        self.assertIn('fixture Git read error', output.getvalue())

    def test_intrinsic_multiple_heads_are_dropped_when_alone(self):
        self.branch(1, {VERSIONS + 'a.py': "revision='a'\ndown_revision='root'\n",
                        VERSIONS + 'b.py': "revision='b'\ndown_revision='root'\n"})
        self.assertDropped('Alembic')

    def test_intrinsic_caller_defect_is_not_hidden_by_peer_overlap(self):
        self.branch(1, {'one.txt': 'one'}, writes=['shared.txt'])
        self.branch(2, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'}, writes=['shared.txt'])
        kept, _ = self.assertDropped('census')
        self.assertEqual(kept, self.entries[:1])

    def assertDropped(self, fragment, entries=None):
        kept, drops = self.select(entries)
        self.assertTrue(drops)
        self.assertIn(fragment, drops[-1][1])
        return kept, drops

    def test_clean_disjoint_branches_leave_checkout_and_refs_untouched(self):
        self.branch(1, {'one.txt': 'one'})
        self.branch(2, {'two.txt': 'two'})
        refs = self.git('show-ref')
        self.write('operator-dirty.txt', 'keep')
        status = self.git('status', '--porcelain')
        self.assertEqual(self.select(), (self.entries, []))
        self.assertEqual(self.git('show-ref'), refs)
        self.assertEqual(self.git('status', '--porcelain'), status)

    def test_text_conflict_against_current_master(self):
        self.branch(1, {'base.txt': 'branch\n'})
        self.git('checkout', 'master')
        self.write('base.txt', 'master moved\n')
        self.commit()
        self.assertDropped('merge')

    def test_siblings_cannot_create_two_alembic_heads(self):
        self.branch(1, {VERSIONS + 'a.py': "revision='a'\ndown_revision='root'\n"})
        self.branch(2, {VERSIONS + 'b.py': "revision='b'\ndown_revision='root'\n"})
        kept, _ = self.assertDeferred('Alembic')
        self.assertEqual(kept, self.entries[:1])

    def test_removing_all_migrations_is_zero_heads_and_is_dropped(self):
        entry = self.branch(1, {'one.txt': 'one'}, writes=['one.txt', VERSIONS + 'root.py'])
        self.git('rm', VERSIONS + 'root.py')
        self.commit()
        self.entries = [(entry[0], entry[1], self.git('rev-parse', 'HEAD'))]
        self.assertDropped('Alembic')

    def test_ordered_rechained_migrations_can_share_window(self):
        self.branch(1, {VERSIONS + 'a.py': "revision='a'\ndown_revision='root'\n"})
        self.branch(2, {VERSIONS + 'b.py': "revision='b'\ndown_revision='a'\n"},
                    parent='br-1', dependency='L-spec-1')
        self.assertEqual(self.select(), (self.entries, []))

    def test_branch_repairing_existing_multiple_heads_is_admitted(self):
        self.write(VERSIONS + 'a.py', "revision='a'\ndown_revision='root'\n")
        self.write(VERSIONS + 'b.py', "revision='b'\ndown_revision='root'\n")
        self.commit()
        self.base = self.git('rev-parse', 'master')
        self.branch(1, {VERSIONS + 'merge.py': "revision='merge'\ndown_revision=('a', 'b')\n"})
        self.assertEqual(self.select(), (self.entries, []))

    def test_malformed_missing_duplicate_and_cyclic_revisions_fail_closed(self):
        cases = ["revision='a'\ndown_revision=call()\n",
                 "revision='a'\ndown_revision='missing'\n",
                 "revision='root'\ndown_revision=None\n",
                 "revision='a'\ndown_revision='a'\n"]
        for i, source in enumerate(cases):
            entry = self.branch(10 + i, {VERSIONS + f'bad{i}.py': source})
            self.assertDropped('Alembic', [entry])

    def test_caller_requires_own_census_edit(self):
        self.branch(1, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'})
        self.assertDropped('census')
        entry = self.branch(2, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")',
                                CENSUS: '// updated census\n'})
        self.assertEqual(self.select([entry]), ([entry], []))

    def test_existing_caller_edits_and_test_sources_do_not_expand_census(self):
        self.git('checkout', 'master')
        self.write('dashboard/src/lib/caller.ts', 'backendFetch("/old")')
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.branch(1, {'dashboard/src/lib/caller.ts': 'backendFetch("/new")',
                       'dashboard/src/lib/new.test.ts': 'backendFetch("/test")'})
        self.assertEqual(self.select(), (self.entries, []))

    def test_dependency_must_be_merged_or_admitted_earlier(self):
        a = self.branch(1, {'one.txt': 'one'})
        b = self.branch(2, {'two.txt': 'two'}, dependency='L-spec-1')
        self.assertEqual(self.select(), (self.entries, []))
        kept, _ = self.assertDeferred('waits_for', [b, a])
        self.assertEqual(kept, [a])
        self.assertDeferred('waits_for', [b])
        self.git('checkout', 'master')
        self.git('merge', '--no-ff', 'br-1', '-m', 'merge dependency')
        self.events = [dict(type='shipped', subject='L-spec-1', sha=a[2])]
        self.assertEqual(self.select([b]), ([b], []))

    def test_active_ledger_hold_and_rejected_parent(self):
        self.branch(1, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'})
        self.branch(2, {'two.txt': 'two'})
        self.events = [dict(type='dependency-hold', subject='L-spec-2',
                           waits_for='L-spec-1', ts='2026-11-01T01:00:00Z', _src='hold:1')]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kept, drops = self.select()
        self.assertEqual(kept, [])
        self.assertEqual([entry[0] for entry, why in drops], ['L-spec-1'])
        self.assertIn('deferred L-spec-2', output.getvalue())
        self.assertIn('waits_for', output.getvalue())

    def test_release_newest_hold_and_malformed_dependency(self):
        entry = self.branch(2, {'two.txt': 'two'}, dependency='L-spec-1')
        hold = dict(type='dependency-hold', subject=entry[0], waits_for='L-spec-1',
                    ts='2026-11-01T01:00:00Z', _src='hold:1')
        self.events = [hold, dict(type='unblocked', subject=entry[0], actor='builder',
                                 ts='2026-11-01T02:00:00Z')]
        self.assertDeferred('waits_for')
        self.events[-1]['actor'] = 'thinker'
        self.assertEqual(self.select(), (self.entries, []))
        self.events.append(dict(hold, waits_for=['L-spec-1'], ts='2026-11-01T03:00:00Z'))
        self.assertDropped('invalid dependency')

    def test_shipped_marker_off_master_is_not_merged(self):
        a = self.branch(1, {'one.txt': 'one'})
        b = self.branch(2, {'two.txt': 'two'}, dependency='L-spec-1')
        self.events = [dict(type='shipped', subject=a[0], sha=a[2])]
        self.assertDeferred('waits_for', [b])

    def test_another_branches_census_edit_does_not_cover_new_caller(self):
        self.branch(1, {'one.txt': 'one', CENSUS: '// changed for first branch\n'})
        self.branch(2, {'dashboard/src/lib/caller.ts': 'backendFetch("/x")'},
                    parent='br-1', dependency='L-spec-1')
        kept, _ = self.assertDropped('census')
        self.assertEqual(kept, self.entries[:1])

    def test_unordered_migrations_are_admitted_in_canonical_order(self):
        self.branch(1, {VERSIONS + 'a.py': "revision='a'\ndown_revision='root'\n"})
        self.branch(2, {VERSIONS + 'b.py': "revision='b'\ndown_revision='a'\n"}, parent='br-1',
                    dependency='L-spec-1')
        # Shuffled input: migration_order.order puts the prerequisite first.
        shuffled = [self.entries[1], self.entries[0]]
        self.assertEqual(self.select(shuffled), (self.entries, []))

    def test_dependent_migration_without_its_prerequisite_is_held(self):
        self.branch(1, {VERSIONS + 'a.py': "revision='a'\ndown_revision='root'\n"})
        self.branch(2, {VERSIONS + 'b.py': "revision='b'\ndown_revision='a'\n"}, parent='br-1',
                    dependency='L-spec-77')
        self.assertDeferred('migration order', self.entries)

    def test_declared_write_overlap_including_uncreated_globs(self):
        self.branch(1, {'one.txt': 'one'}, writes=['future/*.py'])
        self.branch(2, {'two.txt': 'two'}, writes=['future/test_*.py'])
        kept, _ = self.assertDeferred('Writes')
        self.assertEqual(kept, self.entries[:1])

    def test_missing_writes_is_not_clean(self):
        entry = self.branch(1, {'one.txt': 'one'})
        (self.root / 'content' / (entry[0] + '.md')).unlink()
        self.assertDropped('metadata')

    def test_forged_maintenance_grant_cannot_bypass_metadata(self):
        import quarantine_file
        entry = self.branch(1, {quarantine_file.PATH: '# quarantine\n'})
        fake = ('quarantine-' + 'a' * 16, 'auto-quarantine/' + 'a' * 16, entry[2])
        self.events = [dict(type='merge-gate-clean', actor='builder', project='albert-scott',
                            subject=fake[0], branch=fake[1], sha=fake[2], grant=[quarantine_file.PATH])]
        self.assertDropped('quarantine metadata', [fake])


if __name__ == '__main__':
    unittest.main()
