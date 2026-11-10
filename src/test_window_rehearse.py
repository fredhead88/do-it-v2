"""Real Git rehearsal trees and ledger receipts; all CI seams are local doubles."""
import contextlib
import hashlib
import io
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

_import_root = tempfile.TemporaryDirectory(prefix='rehearse-import-')
os.environ['DOIT_ROOT'] = _import_root.name
os.environ['DOIT_NO_POKE'] = '1'
import fold
import window_gate as wg

CHECKS = [wg.PYTEST_CHECK, wg.PG_CHECK, wg.DASHBOARD_CHECK]
NODE = 'api/tests/test_total.py::test_total'
FLAKE = 'api/tests/test_total.py::test_clock'
UNKNOWN = 'api/tests/test_total.py::test_unknown'


class Rehearsal(unittest.TestCase):
    def test_local_red_cancels_only_the_matching_ci_process(self):
        import merge_queue
        child_pid, run_id = 987654, '731'
        cancelled = []
        process = Mock(pid=child_pid, returncode=143, args=['fake-ci', run_id])
        git = wg.git
        popen = subprocess.Popen
        def launch(args, **kwargs):
            return process if args[:2] == ['bash', str(self.repo / wg.PREDEPLOY)] else popen(args, **kwargs)
        def local_git(repo, *args, **kwargs):
            if args[0] == 'push':
                return subprocess.CompletedProcess(args, 0, '', '')
            return git(repo, *args, **kwargs)
        def kill_group(pid, sig):
            self.assertEqual(sig, signal.SIGTERM)
            if pid == os.getpid():
                signal.raise_signal(sig)
            else:
                self.assertEqual(pid, child_pid)
                cancelled.append(run_id)
        def communicate(**kwargs):
            if not cancelled:
                merge_queue._cancel_rehearsal(dict(batch='rh-local', project='albert-scott',
                                                  pid=os.getpid()))
            return ('', None)
        process.communicate.side_effect = communicate
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(wg, 'git', side_effect=local_git), \
             patch.object(wg.subprocess, 'Popen', side_effect=launch), \
             patch.object(wg.os, 'killpg', side_effect=kill_group):
            code = wg.main_(self.args(ident='rh-local'))
        self.assertEqual(code, 2)
        self.assertEqual(cancelled, [run_id])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(fold.read_events()[-1]['state'], 'undetermined')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='window-rehearse-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repos/albert-scott'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        self.write('base.txt', 'base\n')
        self.write(wg.PREDEPLOY, 'REQUIRED_CHECKS=("' + '" "'.join(CHECKS) + '")\n')
        self.write('scripts/ci/actions_changes_gate.sh', "printf '%s\\n' '" + json.dumps(dict(
            checks=CHECKS, everything=True, unclassified=[], reason='fixture')) + "'\n")
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'future')
        self.write('previous-window.txt', 'future base\n')
        self.commit()
        self.future = self.git('rev-parse', 'HEAD')
        self.entries = []
        for spec in 'AB':
            self.branch(spec, {spec + '.txt': spec})
        self.git('switch', '-qc', 'operator', 'master')
        self.write('base.txt', 'operator dirty\n')
        config = self.root / 'grading.toml'
        config.write_text('')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for obj, key, value in [(fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events')]:
            self.stack.enter_context(patch.object(obj, key, value))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1',
            DOIT_GRADING_TOML=str(config), DOIT_MAIN='master', DOIT_WINDOW_RECHAIN='off',
            DOIT_GATE_LEDGER_FILE='L-executor-rehearse.jsonl'))

    def git(self, *args):
        p = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=fixture',
                            '-c', 'user.email=fixture@localhost', *args], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def branch(self, spec, files, *, parent=None, dependency=None):
        self.git('switch', '-qc', spec, parent or self.base)
        for path, value in files.items():
            self.write(path, value)
        self.commit()
        content = self.root / 'content'
        content.mkdir(exist_ok=True)
        text = 'Writes: ' + ', '.join(files) + '\n'
        if dependency:
            text += 'Dependency-hold: waits_for=' + dependency + '\n'
        (content / (spec + '.md')).write_text(text)
        self.entries = [e for e in self.entries if e[0] != spec]
        self.entries.append((spec, spec, self.git('rev-parse', 'HEAD')))

    def args(self, ident='rh-fixture', base=None, specs=None):
        selected = self.entries if specs is None else [e for e in self.entries if e[0] in specs]
        return [ident, '--rehearse', '--on', base or self.future, '--project', 'albert-scott',
                '--main', 'master', '--specs', ','.join(s + ':' + b for s, b, _ in selected)]

    def rehearse(self, verdict=None, *, probe=None, attribute=None, args=None):
        verdict = verdict or dict(state='green', nodes=[], url='', doc={})
        output = io.StringIO()
        def verify_session(*unused, **kwargs):
            import test_slots
            self.assertEqual(test_slots.held_count(self.root), 1)
            records = [json.loads(line) for line in
                       (self.root / 'state/test-slots/queue.jsonl').read_text().splitlines()]
            self.assertEqual(records[-1]['role'], 'rehearsal')
            self.assertEqual(records[-1]['event'], 'acquired')
            return verdict
        with patch.object(wg, 'verify', side_effect=verify_session) as verify, \
             patch.object(wg, 'escalate', side_effect=AssertionError('rehearsal escalated')), \
             patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('rehearsal base dispatch')), \
             patch.object(wg, 'base_pg', side_effect=AssertionError('rehearsal base PG dispatch')), \
             patch('window_attribution.probe', return_value=probe or {'states': {}}) as retry, \
             patch.object(wg, 'targeted_attribution', return_value=attribute or {}) as attribution, \
             contextlib.redirect_stdout(output):
            code = wg.main_(args or self.args())
        import test_slots
        self.assertEqual(test_slots.held_count(self.root), 0)
        return code, verify.call_args_list, retry.call_args_list, attribution.call_args_list, output.getvalue()

    def test_undetermined_rehearsal_event_carries_why(self):
        why = 'pg-parity has a failed non-test step'
        cases = [why, 'x' * 800, None, 'token=fixture-value']
        for index, reason in enumerate(cases):
            verdict = dict(state='undetermined', nodes=[], url='https://example.invalid/run', doc={})
            if reason is not None:
                verdict['why'] = reason
            code = self.rehearse(verdict, args=self.args(ident='rh-why-' + str(index)))[0]
            self.assertEqual(code, 2)
            row = next(e for e in reversed(fold.read_events()) if e['type'] == 'window-rehearsed')
            self.assertEqual(row['state'], 'undetermined')
            if reason is None:
                self.assertNotIn('why', row)
                continue
            import prod_runner
            expected = prod_runner.redact(reason)[:500]
            self.assertEqual(row['why'], expected)
            self.assertLessEqual(len(row['why']), 500)
            if index == 3:
                self.assertNotIn(reason, row['why'])
            # Fresh process reads the actual production append, without fold's cache.
            reader = subprocess.run([__import__('sys').executable, '-c',
                "import json, fold; print(json.dumps(next(e for e in reversed(fold.read_events(use_cache=False)) "
                "if e['type'] == 'window-rehearsed')))"],
                env={**os.environ, 'PYTHONPATH': str(Path(wg.__file__).parent)},
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(reader.stdout)['why'], expected)
        red = dict(state='red', nodes=[NODE], url='', doc={})
        self.assertEqual(self.rehearse(red, probe={'states': {NODE: 'red'}}, attribute=dict(
            drop={}, proven={}, environment=[], unresolved=[NODE], mode='singles'))[0], 1)
        row = next(e for e in reversed(fold.read_events()) if e['type'] == 'window-rehearsed')
        self.assertNotIn('why', row)
        self.assertEqual(self.rehearse()[0], 0)
        row = next(e for e in reversed(fold.read_events()) if e['type'] == 'window-rehearsed')
        self.assertNotIn('why', row)

    def test_rehearsal_calls_verify_in_rehearsal_mode(self):
        _, calls, _, _, _ = self.rehearse()
        self.assertEqual(calls[0].kwargs, {'rehearsal': True})

    def test_red_pg_only_rehearsal_is_never_promoted_when_probe_is_red_or_undetermined(self):
        pg1 = 'api/tests/test_049_verifier_ro_grants.py::test_select_policy_and_guarded_round_trip'
        pg2 = 'api/tests/test_claude_ro_exposure_live.py::test_AC04_peer_suites_run_without_skips'
        verdict = dict(state='red', nodes=[pg1, pg2], url='https://example.invalid/run', doc={},
                       pg_failed=True, pg_failures={pg1: 'ERROR x', pg2: 'ERROR y'}, other_nodes=[])
        code, _, _, _, output = self.rehearse(verdict,
            probe={'states': {pg1: 'red', pg2: 'undetermined'}},
            attribute=dict(drop={}, proven={}, environment=[pg1], unresolved=[pg2], mode='singles'))
        self.assertEqual(code, 1)
        rows = fold.read_events()
        row = next(e for e in reversed(rows) if e['type'] == 'window-rehearsed')
        self.assertEqual(row['state'], 'red')
        self.assertEqual(row['ids'], [pg1, pg2])
        self.assertEqual(row['attribution']['environment'], [pg1])
        self.assertEqual(row['attribution']['unresolved'], [pg2])
        self.assertEqual(row['run_url'], 'https://example.invalid/run')
        self.assertNotIn('ATTRIBUTED', output)
        self.assertFalse(any(e['type'] in ('window-dropped', 'escalation-blocking', 'window-base-observed')
                             for e in rows))

    def test_identical_tree_reuses_all_checks_and_skips_base_ci_after_master_reaches_on(self):
        before_refs = self.git('show-ref')
        with wg.chain_lock('window-in-progress', 'albert-scott'):
            with patch.object(wg, 'chain_lock', side_effect=AssertionError('rehearsal chain lock')):
                code, calls, _, _, _ = self.rehearse()
        self.assertEqual(code, 0)
        self.assertEqual(self.git('show-ref'), before_refs)
        candidate = calls[0].args[1]
        self.assertEqual(calls[0].args[2], CHECKS)
        rows = fold.read_events()
        self.assertEqual({e['type'] for e in rows}, {'window-tree-verified', 'window-rehearsed'})
        receipt = next(e for e in rows if e['type'] == 'window-tree-verified')
        self.assertEqual((receipt['rehearsal'], receipt['base_sha']), (True, self.future))
        self.assertFalse(wg.in_flight(rows))
        self.assertEqual(wg.red_rate(rows, wg.datetime.now(wg.timezone.utc))['windows'], 0)
        self.assertFalse(fold.open_escalations(rows))
        self.git('update-ref', 'refs/heads/master', self.future, self.base)
        with patch.object(wg, 'verify', side_effect=AssertionError('cached tree redispatched')), \
             patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('cached tree baseline refresh')), \
             patch.object(wg, 'base_pg', side_effect=AssertionError('cached tree base PG')):
            rc = wg.main_(['win-real', '--project', 'albert-scott', '--main', 'master',
                           '--specs', 'A:A,B:B'])
        self.assertEqual(rc, 0)
        self.assertEqual(wg.tree_of(self.repo, self.git('rev-parse', 'master')), wg.tree_of(self.repo, candidate))
        self.assertEqual(self.git('branch', '--show-current'), 'operator')
        self.assertEqual((self.repo / 'base.txt').read_text(), 'operator dirty\n')

    def test_changed_base_or_missing_check_falls_back_to_normal_verification(self):
        for missing_check in (False, True):
            with self.subTest(missing_check=missing_check):
                self.rehearse(args=self.args(ident='rh-' + str(missing_check)))
                if missing_check:
                    self.git('update-ref', 'refs/heads/master', self.future)
                    events = fold.read_events()
                    receipt = next(e for e in reversed(events)
                                   if e['type'] == 'window-tree-verified' and e.get('rehearsal') is True)
                    wg.ev('window-tree-verified', 'rh-partial', **{k: v for k, v in receipt.items()
                        if k in ('window', 'sha', 'tree', 'green', 'rehearsal', 'base_sha')}, required=[wg.PYTEST_CHECK])
                with patch.object(wg, 'base_red_snapshot', return_value={'complete': True}), \
                     patch.object(wg, 'verify', return_value=dict(state='green', nodes=[], url='', doc={})) as verify:
                    rc = wg.main_(['win-fallback-' + str(missing_check), '--project', 'albert-scott',
                                   '--main', 'master', '--specs', 'A:A,B:B'])
                self.assertEqual(rc, 0)
                self.assertTrue(any(c.args[2] == CHECKS for c in verify.call_args_list))

    def test_base_pg_also_uses_rehearsed_tree_receipt(self):
        _, calls, _, _, _ = self.rehearse()
        candidate = calls[0].args[1]
        with patch.object(wg, 'verify', side_effect=AssertionError('cached baseline redispatched')):
            baseline, docs = wg.base_pg(self.repo, candidate, CHECKS, 'win-base', fold.read_events())
        self.assertEqual(baseline['state'], 'green')
        self.assertEqual(docs, [])

    def test_repeated_rehearsal_reuses_without_extending_ci_receipt_age(self):
        self.assertEqual(self.rehearse()[0], 0)
        before = [e for e in fold.read_events() if e['type'] == 'window-tree-verified']
        code, calls, _, _, _ = self.rehearse(args=self.args(ident='rh-again'))
        self.assertEqual((code, calls), (0, []))
        after = [e for e in fold.read_events() if e['type'] == 'window-tree-verified']
        self.assertEqual(after, before)

    def test_cached_candidate_still_waits_for_predecessor_and_cannot_ship_on_cancellation(self):
        self.rehearse()
        train = dict(repo=self.repo, main='master', after='win-predecessor', verified_on=self.future)
        with patch.object(wg, 'verify', side_effect=AssertionError('cache miss')), \
             patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('base dispatch')), \
             patch.object(wg, 'wait_predecessor', side_effect=wg.TrainCancelled('predecessor-red')) as wait:
            with self.assertRaises(wg.TrainCancelled):
                wg._run_attempt('win-child', 'albert-scott', ['A:A', 'B:B'], 'master', False,
                                self.future, self.future, 'win-predecessor', 1, train)
        wait.assert_called_once_with(train)
        self.assertEqual(self.git('rev-parse', 'master'), self.base)
        self.assertFalse(any(e['type'] == 'shipped' for e in fold.read_events()))

    def test_base_pg_does_not_reuse_incomplete_coverage_or_newer_red(self):
        _, calls, _, _, _ = self.rehearse()
        candidate = calls[0].args[1]
        receipt = next(e for e in fold.read_events() if e['type'] == 'window-tree-verified')
        for green, required in [(True, [wg.PYTEST_CHECK]), (False, CHECKS)]:
            wg.ev('window-tree-verified', 'rh-no-pg-cache', window='rh-no-pg-cache', sha=candidate,
                  tree=receipt['tree'], green=green, required=required, rehearsal=True, base_sha=self.future)
            with patch.object(wg, 'verify', return_value=dict(state='green', nodes=[], doc={})) as verify:
                wg.base_pg(self.repo, candidate, CHECKS, 'win-base', fold.read_events())
            verify.assert_called_once_with(self.repo, candidate, [wg.PG_CHECK], 'win-base')

    def test_red_reruns_only_failed_ids_then_attributes_only_reproduced_ids(self):
        verdict = dict(state='red', nodes=[FLAKE, NODE, UNKNOWN], url='', doc={})
        plan = dict(proven={'B': [NODE]}, drop={'B': [NODE]}, environment=[], unresolved=[], mode='singles')
        probe = {'states': {FLAKE: 'green', NODE: 'red', UNKNOWN: 'undetermined'}}
        code, calls, retries, attrs, output = self.rehearse(verdict, probe=probe, attribute=plan)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].args[1], calls[0].args[1])
        self.assertCountEqual(retries[0].args[2], [FLAKE, NODE, UNKNOWN])
        self.assertEqual(attrs[0].args[1], self.future)
        self.assertEqual(attrs[0].args[3], [NODE])
        self.assertEqual(attrs[0].kwargs['on'], self.future)
        self.assertIn('FLAKE ' + FLAKE, output)
        self.assertIn('ATTRIBUTED B ' + NODE, output)
        self.assertIn('UNRESOLVED ' + UNKNOWN, output)
        rows = fold.read_events()
        self.assertEqual({e['type'] for e in rows}, {'window-tree-verified', 'window-rehearsed', 'window-flake'})
        event = next(e for e in reversed(rows) if e['type'] == 'window-rehearsed')
        self.assertEqual(event['state'], 'red')
        self.assertEqual(event['flakes'], [FLAKE])
        self.assertEqual(event['attribution']['proven'], {'B': [NODE]})
        self.assertFalse(next(e['green'] for e in rows if e['type'] == 'window-tree-verified'))
        self.assertEqual(self.git('rev-parse', 'master'), self.base)

    def test_unknown_or_absent_retry_is_never_a_flake_or_spec_blame(self):
        for state in ('absent', 'undetermined'):
            code, _, _, attrs, output = self.rehearse(dict(state='red', nodes=[NODE], url='', doc={}),
                                                     probe={'states': {NODE: state}})
            self.assertEqual(code, 1)
            self.assertEqual(attrs, [])
            self.assertNotIn('FLAKE ', output)
            self.assertIn('UNRESOLVED ' + NODE, output)

    def test_undetermined_does_not_create_a_determined_cache_result(self):
        code, _, _, attrs, _ = self.rehearse(dict(state='undetermined', nodes=[], url='', doc={}))
        self.assertEqual(code, 2)
        self.assertEqual(attrs, [])
        self.assertFalse(any(e['type'] == 'window-tree-verified' for e in fold.read_events()))

    def test_two_slots_per_project_and_refusal_before_dispatch(self):
        import window_rehearse as wr
        with patch.dict(os.environ, DOIT_REHEARSAL_BATCHES='2'), \
             wr.slot(self.root, 'albert-scott') as first, wr.slot(self.root, 'albert-scott') as second:
            self.assertNotEqual(first, second)
            code, calls, _, _, _ = self.rehearse()
            self.assertEqual((code, calls), (3, []))
            with wr.slot(self.root, 'other-project') as other:
                self.assertIsNotNone(other)
        self.assertEqual(self.rehearse()[0], 0)

    def test_default_three_slots_and_configured_capacity_fail_closed(self):
        import window_rehearse as wr
        with patch.dict(os.environ):
            os.environ.pop('DOIT_REHEARSAL_BATCHES', None)
            self.assertEqual(wr.capacity(), 3)
            with contextlib.ExitStack() as stack:
                slots = [stack.enter_context(wr.slot(self.root, 'albert-scott')) for _ in range(3)]
                self.assertEqual(len(set(slots)), 3)
                self.assertNotIn(None, slots)
                self.assertEqual(self.rehearse()[:2], (3, []))
            self.assertEqual(self.rehearse()[0], 0)
        for value in ('0', '-1', 'unknown', '1.5'):
            with self.subTest(value=value), patch.dict(os.environ, DOIT_REHEARSAL_BATCHES=value):
                code, calls, _, _, _ = self.rehearse()
                self.assertEqual((code, calls), (2, []))
                receipt = [e for e in fold.read_events() if e['type'] == 'window-rehearsed'][-1]
                self.assertEqual(receipt['state'], 'undetermined')

    def test_candidate_is_persisted_before_session_wait_and_ci_dispatch(self):
        import test_slots
        timeline = []
        real_acquire = test_slots.acquire

        def read_fresh():
            env = dict(os.environ, DOIT_READ_CACHE='0')
            result = subprocess.run([
                os.sys.executable, '-c',
                'import json,sys;sys.path.insert(0,sys.argv[1]);import fold;'
                'print(json.dumps(fold.read_events()))', str(Path(__file__).parent)],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [e for e in json.loads(result.stdout) if e['type'] == 'window-rehearsed'][-1]

        @contextlib.contextmanager
        def acquire(*args, **kwargs):
            receipt = read_fresh()
            self.assertEqual(receipt['state'], 'prepared')
            self.assertEqual(receipt['base_sha'], self.future)
            self.assertEqual(receipt['specs'], ['A', 'B'])
            self.assertEqual(self.git('rev-parse', receipt['sha'] + '^{tree}'), receipt['tree'])
            timeline.append(dict(stage='session-wait', receipt=receipt))
            with real_acquire(*args, **kwargs) as held:
                yield held

        def verify(repo, candidate, required, ident, *, rehearsal=False):
            self.assertTrue(rehearsal)
            receipt = read_fresh()
            self.assertEqual(receipt['state'], 'running')
            self.assertEqual((receipt['sha'], receipt['required']), (candidate, required))
            self.assertEqual(receipt['sha'], timeline[0]['receipt']['sha'])
            timeline.append(dict(stage='ci-dispatch', receipt=receipt))
            return dict(state='green', nodes=[], url='', doc={})

        with patch.object(test_slots, 'acquire', side_effect=acquire), \
             patch.object(wg, 'verify', side_effect=verify):
            self.assertEqual(wg.main_(self.args()), 0)
        terminal = read_fresh()
        self.assertEqual(terminal['state'], 'green')
        self.assertEqual(terminal['sha'], timeline[0]['receipt']['sha'])
        self.assertEqual([row['stage'] for row in timeline], ['session-wait', 'ci-dispatch'])
        body = json.dumps(dict(timeline=timeline, terminal=terminal), sort_keys=True)
        print(json.dumps(dict(url='scratch://rehearsal/early-candidate', status=200,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_unavailable_capacity_state_fails_closed_before_dispatch(self):
        with patch('window_rehearse.slot', side_effect=OSError('fixture capacity state unavailable')):
            code, calls, _, _, _ = self.rehearse()
        self.assertEqual((code, calls), (2, []))

    def test_shared_files_competing_migration_roots_and_conflicts_fail_before_ci(self):
        import window_rehearse as wr
        # Peer migrations are no longer refused for being migrations (test_multi_migration_window);
        # two unrelated roots still leave two heads and fail before CI.
        for mode in ('shared', 'competing-roots', 'conflict'):
            with self.subTest(mode=mode):
                c, d = 'C-' + mode, 'D-' + mode
                if mode == 'shared':
                    self.branch(c, {'shared.txt': 'C'})
                    self.branch(d, {'shared.txt': 'D'})
                elif mode == 'competing-roots':
                    self.branch(c, {wg.rechain.VERSIONS + 'c.py': "revision='c'\ndown_revision=None\n"})
                    self.branch(d, {wg.rechain.VERSIONS + 'd.py': "revision='d'\ndown_revision=None\n"})
                else:
                    self.branch(c, {'base.txt': 'C'})
                    self.git('switch', '-q', 'future')
                    self.write('base.txt', 'future conflict')
                    self.commit()
                    self.future = self.git('rev-parse', 'HEAD')
                args = self.args(specs=[c] if mode == 'conflict' else [c, d])
                with patch.object(wr, 'alembic_heads', side_effect=AssertionError('expensive check after cheap failure')):
                    code, calls, _, _, _ = self.rehearse(args=args)
                self.assertEqual((code, calls), (2, []))
        self.assertFalse(any(e['type'] == 'window-tree-verified' for e in fold.read_events()))

    def migration(self):
        self.branch('H', {wg.rechain.VERSIONS + 'root.py': "revision='root'\ndown_revision=None\n",
                         'api/alembic_supabase.ini': '[alembic]\nscript_location=alembic_supabase\n',
                         wg.rechain.FINGERPRINT: 'before\n',
                         'scripts/ci/pytest_known_red_quarantine.txt': '# policy'})

    def interpreter(self, output, code=0):
        venv = self.root / 'venv'
        (venv / 'bin').mkdir(parents=True, exist_ok=True)
        python = venv / 'bin/python'
        python.write_text('#!/usr/bin/python3\nimport os,sys,pathlib,json\n'
                          'assert pathlib.Path.cwd().name == "api"\n'
                          'assert sys.argv[1:] == ["-m","alembic","-c","alembic_supabase.ini","heads"]\n'
                          'assert "SUPABASE_DB_URL" not in os.environ\n'
                          'print(' + repr(output) + ')\nsys.exit(' + str(code) + ')\n')
        python.chmod(0o755)
        return str(venv)

    def test_alembic_uses_candidate_api_directory_and_project_python_before_ci(self):
        self.migration()
        venv = self.interpreter('root (head)')
        before = self.git('worktree', 'list', '--porcelain')
        with patch('grading_env.project_venv', return_value=venv):
            code, calls, _, _, _ = self.rehearse(args=self.args(specs=['H']))
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_multiple_or_zero_cli_heads_and_cli_error_never_dispatch(self):
        self.migration()
        for output, status in [('root (head)\nother (head)', 0), ('', 0), ('root (head)', 1)]:
            with self.subTest(output=output, status=status):
                venv = self.interpreter(output, status)
                with patch('grading_env.project_venv', return_value=venv):
                    code, calls, _, _, _ = self.rehearse(args=self.args(specs=['H']))
                self.assertEqual((code, calls), (2, []))

    def test_one_migration_uses_same_fingerprint_and_quarantine_tree_as_real_window(self):
        import window_rehearse as wr
        self.migration()
        def fingerprint(repo, candidate, ident, **kwargs):
            tree = wg.rechain.rewrite_tree(repo, wg.tree_of(repo, candidate),
                                           {wg.rechain.FINGERPRINT: b'stable fingerprint\n'})
            return self.git(*wg.IDENT, 'commit-tree', tree, '-p', candidate, '-m', 'fixture fingerprint')
        with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='on'), \
             patch.object(wr, 'alembic_heads'), \
             patch.object(wg.rechain, 'fingerprint', side_effect=fingerprint) as fp:
            code, calls, _, _, _ = self.rehearse(args=self.args(specs=['H']))
            self.assertEqual(code, 0)
            candidate = calls[0].args[1]
            self.assertEqual(self.git('show', candidate + ':scripts/ci/pytest_known_red_quarantine.txt'), '# policy')
            self.git('update-ref', 'refs/heads/master', self.future)
            with patch.object(wg, 'verify', side_effect=AssertionError('fingerprinted tree redispatched')), \
                 patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('unnecessary base CI')):
                rc = wg.main_(['win-migration', '--project', 'albert-scott', '--main', 'master', '--specs', 'H:H'])
        self.assertEqual(rc, 0)
        self.assertEqual(fp.call_count, 2)
        self.assertEqual(wg.tree_of(self.repo, self.git('rev-parse', 'master')), wg.tree_of(self.repo, candidate))

    def test_future_base_satisfies_dependency_not_yet_on_live_master(self):
        self.branch('C', {'c.txt': 'child'}, dependency='L-spec-1999')
        wg.ev('shipped', 'L-spec-1999', sha=self.future, project='albert-scott')
        code, calls, _, _, _ = self.rehearse(args=self.args(specs=['C']))
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertEqual(self.git('rev-parse', 'master'), self.base)

    def test_all_rerun_passes_are_flakes_with_a_green_tree_receipt(self):
        code, _, retries, attrs, output = self.rehearse(dict(state='red', nodes=[FLAKE], url='', doc={}),
                                                      probe={'states': {FLAKE: 'green'}})
        self.assertEqual((code, len(retries), attrs), (0, 1, []))
        self.assertIn('FLAKE ' + FLAKE, output)
        event = next(e for e in reversed(fold.read_events()) if e['type'] == 'window-rehearsed')
        self.assertEqual((event['state'], event['flake_only'], event['flakes']), ('green', True, [FLAKE]))
        receipt = next(e for e in fold.read_events() if e['type'] == 'window-tree-verified')
        self.assertTrue(receipt['green'])
        self.assertEqual(receipt['flakes'], [FLAKE])

    def flake_receipts(self, criterion, code, output, ident='rh-fixture'):
        # A new process reads the ledger written by the production ev/append path.
        reader = subprocess.run(['python3', '-c',
            'import json,fold,sys; print(json.dumps([e for e in fold.read_events(use_cache=False) '
            'if e.get("window",e.get("subject")) == sys.argv[1]]))', ident],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent)),
            capture_output=True, text=True, check=True)
        rows = json.loads(reader.stdout)
        event = next(e for e in reversed(rows) if e['type'] == 'window-rehearsed')
        records = [e for e in rows if e['type'] == 'window-flake']
        self.assertEqual(sorted(e['node'] for e in records), event['flakes'])
        for record in records:
            self.assertEqual(record['candidate'], event['sha'])
            self.assertEqual(record['project'], 'albert-scott')
            self.assertEqual(record['isolated_state'], 'green')
            self.assertEqual(record['evidence']['snapshot'], event['sha'])
            self.assertEqual(record['evidence']['selector'], record['node'])
            self.assertEqual(record['evidence']['report'], 'window_attribution.probe')
        body = dict(exit_code=code, output=output, receipts=rows)
        raw = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='scratch://rehearsal/' + criterion, status=200,
            body_sha256=hashlib.sha256(raw.encode()).hexdigest(), body_excerpt=body)))
        return rows

    def test_flake_only_rehearsal_is_green(self):
        ids = sorted([FLAKE, NODE])
        code, calls, retries, attrs, output = self.rehearse(
            dict(state='red', nodes=ids, url='', doc={}),
            probe={'states': dict.fromkeys(ids, 'green'), 'url': 'scratch://exact-tree'})
        self.assertEqual(code, 0)
        self.assertEqual(attrs, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(retries[0].args[1:], (calls[0].args[1], ids))
        self.assertIn('green (flakes: 2)', output)
        rows = self.flake_receipts('AC1', code, output)
        event = next(e for e in reversed(rows) if e['type'] == 'window-rehearsed')
        self.assertEqual((event['state'], event['flake_only'], event['flakes']), ('green', True, ids))
        receipt = next(e for e in rows if e['type'] == 'window-tree-verified')
        self.assertEqual(receipt['sha'], calls[0].args[1])
        self.assertEqual(receipt['tree'], self.git('rev-parse', receipt['sha'] + '^{tree}'))
        self.assertEqual(receipt['flakes'], ids)
        self.assertEqual(receipt['required'], CHECKS)
        self.assertTrue(wg.covers(wg.prior_result(rows, receipt['tree']), CHECKS))
        self.assertEqual(self.rehearse()[1], [])

    def test_mixed_and_unresolved_flakes_persist_without_promotion(self):
        for state in ('red', 'undetermined', 'absent', None):
            with self.subTest(state=state):
                # Each subcase gets a separate ledger, including the fresh reader.
                with patch.dict(os.environ, DOIT_GATE_LEDGER_FILE='L-executor-' + str(state) + '.jsonl'):
                    ident = 'rh-' + str(state)
                    states = {FLAKE: 'green'}
                    if state is not None:
                        states[NODE] = state
                    code, calls, _, attrs, output = self.rehearse(
                        dict(state='red', nodes=[FLAKE, NODE], url='', doc={}),
                        probe={'states': states}, args=self.args(ident))
                self.assertEqual(code, 1)
                self.assertEqual(bool(attrs), state == 'red')
                reader_rows = self.flake_receipts('AC2/' + str(state), code, output, ident)
                event = next(e for e in reversed(reader_rows) if e['type'] == 'window-rehearsed')
                self.assertEqual((event['state'], event['flakes']), ('red', [FLAKE]))
                self.assertFalse(event.get('flake_only', False))
                records = [e for e in reader_rows if e['type'] == 'window-flake']
                self.assertEqual([e['node'] for e in records], [FLAKE])
                self.assertEqual(records[0]['candidate'], calls[0].args[1])

    def test_unnamed_or_unavailable_red_evidence_cannot_be_promoted(self):
        for nodes, unavailable in (([], []), ([FLAKE], ['logs unavailable'])):
            failures = dict.fromkeys(('pytest', 'pg-parity', 'vitest', 'suite-error'), set())
            failures.update(pytest=set(nodes), undetermined=unavailable)
            with patch.object(wg, 'candidate_failures', return_value=failures):
                code, _, retries, _, _ = self.rehearse(dict(state='red', nodes=nodes, url='', doc={}),
                    probe={'states': {FLAKE: 'green'}})
            self.assertEqual(code, 1)
            self.assertEqual(retries, [])

    def test_kernel_slot_is_released_when_rehearsal_process_dies(self):
        import select
        import sys
        import window_rehearse as wr
        code = ('import sys,time;sys.path.insert(0,sys.argv[1]);import window_rehearse as w;'
                '\nwith w.slot(sys.argv[2],"albert-scott") as held:\n print(held,flush=True);time.sleep(30)')
        with patch.dict(os.environ, DOIT_REHEARSAL_BATCHES='2'), wr.slot(self.root, 'albert-scott'):
            child = subprocess.Popen([sys.executable, '-c', code, str(Path(__file__).parent), str(self.root)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                self.assertEqual(child.stdout.readline().strip(), '1')
                self.assertEqual(self.rehearse()[0], 3)
            finally:
                child.terminate()
                child.communicate(timeout=5)
            self.assertEqual(self.rehearse()[0], 0)

    def test_cli_requires_rehearsal_base_and_rejects_incompatible_window_options(self):
        for tail in [['--on', self.base], ['--rehearse'],
                     ['--rehearse', '--on', self.base, '--after', 'win-prior'],
                     ['--rehearse', '--on', self.base, '--fixes-base']]:
            with self.subTest(tail=tail), patch.object(wg, 'chain_lock', side_effect=AssertionError('chain lock')):
                rc = wg.main_(['rh-invalid', '--project', 'albert-scott', '--specs', 'A:A'] + tail)
                self.assertEqual(rc, 2)

    def test_old_window_bypass_does_not_skip_rehearsal_ci(self):
        with patch.dict(os.environ, DOIT_WINDOW_GATE='off'):
            code, calls, _, _, _ = self.rehearse()
        self.assertEqual((code, len(calls)), (0, 1))


if __name__ == '__main__':
    unittest.main()
