"""Automatic base-red policy through real Git objects and the normal window gate."""
from datetime import datetime, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

_ROOT = tempfile.TemporaryDirectory(prefix='auto-quarantine-import-')
os.environ['DOIT_ROOT'] = _ROOT.name
os.environ['DOIT_NO_POKE'] = '1'
import fold
import merge_queue
import quarantine as q
import quarantine_file as policy
import rechain
import window_gate as gate

PROJECT = 'albert-scott'
NODE = 'tests/test_clock.py::test_boundary'


# Real job 126438799694, run 41789236664; all 26 recorded step names/conclusions.
PG_STEPS = [
    {'name': 'Set up job', 'conclusion': 'success'},
    {'name': 'Set up runner', 'conclusion': 'success'},
    {'name': 'Run actions/checkout@v4.4.0', 'conclusion': 'success'},
    {'name': 'Run actions/setup-python@v5', 'conclusion': 'success'},
    {'name': 'Install uv', 'conclusion': 'success'},
    {'name': 'Install dependencies (pipelines lock)', 'conclusion': 'success'},
    {'name': 'Install web-serving stack (spec 378 parity)', 'conclusion': 'success'},
    {'name': 'Install API package (editable, dev extra)', 'conclusion': 'success'},
    {'name': 'Install dev/test-only dependencies', 'conclusion': 'success'},
    {'name': 'Verify test dependencies importable', 'conclusion': 'success'},
    {'name': 'Start hosted PG18 service', 'conclusion': 'success'},
    {'name': 'Start private socket-only PG18', 'conclusion': 'skipped'},
    {'name': 'Bootstrap the PG-parity schema (alembic upgrade head, from empty)', 'conclusion': 'success'},
    {'name': 'Run the spec-202 PG-parity data-layer suite against real Postgres', 'conclusion': 'success'},
    {'name': 'Upload pg-parity junit.xml', 'conclusion': 'success'},
    {'name': 'Seed agency_settings row for spec-1158 ingest tests (albert_scott)', 'conclusion': 'success'},
    {'name': 'Run spec-1158 all-orders/ingestion-pipeline/MCF-channel ingest tests', 'conclusion': 'success'},
    {'name': 'Install PG18 server and vector for ephemeral scratch fixtures', 'conclusion': 'success'},
    {'name': 'Run spec-839/895/926/949 scratch-database tests (pg_scratch)', 'conclusion': 'failure'},
    {'name': 'Stop private socket-only PG18', 'conclusion': 'skipped'},
    {'name': 'Report test-execution status to job summary', 'conclusion': 'success'},
    {'name': 'Post Install uv', 'conclusion': 'skipped'},
    {'name': 'Post Run actions/setup-python@v5', 'conclusion': 'skipped'},
    {'name': 'Post Run actions/checkout@v4.4.0', 'conclusion': 'success'},
    {'name': 'Complete runner', 'conclusion': 'success'},
    {'name': 'Complete job', 'conclusion': 'success'},
]
SPEC_202 = PG_STEPS[13]['name']
SPEC_1158 = PG_STEPS[16]['name']
PG_SCRATCH = PG_STEPS[18]['name']
PG_FAILED = 'api/tests/test_claude_ro_exposure_live.py::test_AC04_peer_suites_run_without_skips'
PG_ERROR = 'api/tests/test_049_verifier_ro_grants.py::test_select_policy_and_guarded_round_trip'
# FAILED/ERROR lines and summary, verbatim
# Reconstructed from the supplied raw log: 7 data lines, not the missing 15-line sample.
# Only the step column is rewritten to UNKNOWN STEP.
PG_LOG = (
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:21:58.3198318Z api/tests/test_049_verifier_ro_grants.py::test_select_policy_and_guarded_round_trip ERROR [ 84%]\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:22:25.3471498Z api/tests/test_claude_ro_exposure_live.py::test_AC04_peer_suites_run_without_skips FAILED [ 90%]\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:36.2224154Z _________ ERROR at setup of test_select_policy_and_guarded_round_trip __________\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:36.2227792Z E   AssertionError: leftover cluster-global role doit_verifier_ro: no-role coverage requires its absence\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:36.2235318Z E   AssertionError: api/tests/test_562_genesis_drift.py: 1 failed, 1 passed, 22 deselected in 15.29s\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:38.8477076Z FAILED api/tests/test_claude_ro_exposure_live.py::test_AC04_peer_suites_run_without_skips - AssertionError: api/tests/test_562_genesis_drift.py: 1 failed, 1 passed, 22 deselected in 15.29s\n'
    'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:38.8504969Z ERROR api/tests/test_049_verifier_ro_grants.py::test_select_policy_and_guarded_round_trip - AssertionError: leftover cluster-global role doit_verifier_ro: no-role coverage requires its absence\n'
)
PG_SUMMARY_LOG = '\n'.join(line for line in PG_LOG.splitlines()
                           if 'Z FAILED ' in line or 'Z ERROR ' in line)
# Synthetic no-id log, modelled on the reported uncaptured failure.
PG_NO_IDS_LOG = '\n'.join(line for line in PG_LOG.splitlines()
                          if 'Z FAILED ' not in line and 'Z ERROR ' not in line)
PG_NO_IDS_LOG += '\n' + 'pg-parity (Postgres)\tUNKNOWN STEP\t2026-11-04T07:27:38.8509195Z = 1 failed, 1855 passed, 47 skipped, 1493 deselected, 1 xfailed, 110 warnings, 1 error in 1289.97s (0:21:29) =\n'


class AutoQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='auto-quarantine-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        for obj, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root/'events')):
            change = patch.object(obj, name, value)
            change.start(); self.addCleanup(change.stop)
        change = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        change.start(); self.addCleanup(change.stop)

    def observation(self, key, nodes=(NODE,), minutes=0):
        q._append('base-red', PROJECT, self.now+timedelta(minutes=minutes),
                  sha='a'*40, complete=True, ids={'pytest': list(nodes)}, sources=['https://ci.invalid/runs/'+key])

    def repo(self):
        repo = self.root/'repos'/PROJECT
        (repo/Path(policy.PATH).parent).mkdir(parents=True)
        (repo/policy.PATH).write_text('# quarantine\n')
        rechain.out(repo, 'init', '-q', '-b', 'master')
        rechain.out(repo, 'add', '.')
        rechain.out(repo, *rechain.IDENT, 'commit', '-qm', 'base')
        return repo

    def test_distinct_consecutive_runs_and_green_reset(self):
        self.observation('1')
        q.apply(PROJECT, now=self.now)
        self.assertEqual(q.active(fold.read_events(), PROJECT, self.now), {})
        self.observation('1')
        q.apply(PROJECT, now=self.now)
        self.assertEqual(q.active(fold.read_events(), PROJECT, self.now), {})
        self.observation('2', [])
        self.observation('3')
        q.apply(PROJECT, now=self.now)
        self.assertFalse(q.active(fold.read_events(), PROJECT, self.now))
        self.observation('4')
        q.apply(PROJECT, now=self.now)
        row = q.active(fold.read_events(), PROJECT, self.now)[NODE]
        self.assertEqual(row['owner'], 'auto-'+self.now.date().isoformat())
        self.assertEqual(q._time(row['until'])-self.now, timedelta(days=7))
        brief = next(e for e in fold.read_events() if e['_src'] == row['fix'])
        self.assertEqual(brief['type'], 'brief')
        self.assertIn(NODE, brief['line'])

    def test_overlapping_snapshots_use_distinct_runs_and_green_resets_old_union(self):
        def observation(run, nodes):
            return dict(run_id=str(run), attempt=1, complete=True,
                        timestamp=(self.now-timedelta(minutes=10-run)).isoformat(),
                        ids={'pytest': nodes})
        def snapshot(rows):
            q._append('base-red', PROJECT, self.now, sha='a'*40, complete=True,
                      ids={'pytest': [NODE]}, sources=['same-workflow-set'], observations=rows)
        snapshot([observation(1, [NODE]), observation(2, [])])
        q.apply(PROJECT, now=self.now)
        self.assertFalse(q.active(fold.read_events(), PROJECT, self.now))
        snapshot([observation(2, []), observation(3, [NODE])])
        q.apply(PROJECT, now=self.now)
        self.assertFalse(q.active(fold.read_events(), PROJECT, self.now))
        snapshot([observation(3, [NODE]), observation(4, [NODE])])
        q.apply(PROJECT, now=self.now)
        self.assertIn(NODE, q.active(fold.read_events(), PROJECT, self.now))

    def test_two_windows_do_not_count_bisection_twice(self):
        for _ in range(2):
            q._append('window-pre-existing', PROJECT, self.now, _subject='w1', window='w1', ids=[NODE])
        q.apply(PROJECT, now=self.now)
        self.assertFalse(q.active(fold.read_events(), PROJECT, self.now))
        q._append('window-pre-existing', PROJECT, self.now, _subject='w2', window='w2', ids=[NODE])
        q.apply(PROJECT, now=self.now)
        self.assertIn(NODE, q.active(fold.read_events(), PROJECT, self.now))

    def test_queued_one_file_commit_and_next_window_releases_both_jobs(self):
        repo = self.repo()
        base = rechain.out(repo, 'rev-parse', 'HEAD')
        self.observation('1'); self.observation('2')
        q.apply(PROJECT, now=self.now)
        events = fold.read_events()
        queued = merge_queue.queue(events, PROJECT)
        self.assertEqual(len(queued), 1)
        branch = queued[0]['branch']
        self.assertEqual(rechain.out(repo, 'diff', '--name-only', base, branch), policy.PATH)
        row = rechain.text(repo, branch, policy.PATH).splitlines()[-1].split('\t')
        self.assertEqual(row[:3], [NODE, 'auto-'+self.now.date().isoformat(),
                                  (self.now+timedelta(days=7)).date().isoformat()])
        self.assertIn('runs/1', row[3]); self.assertIn('runs/2', row[3])
        self.assertEqual((repo/policy.PATH).read_text(), '# quarantine\n')
        q.apply(PROJECT, now=self.now)
        self.assertEqual(len(merge_queue.queue(fold.read_events(), PROJECT)), 1)
        result = dict(state='red', nodes=[NODE], url='', doc={}, pg_failed=True,
                      pg_failures={NODE: 'FAILED clock'}, other_nodes=[NODE], output='', why='gate exit 1')
        failures = dict(pytest={NODE}, **{'pg-parity': {NODE}, 'vitest': set(), 'suite-error': set(),
                                        'undetermined': [], 'pg_files': {}})
        with patch.object(gate, 'required_checks', return_value=[gate.PYTEST_CHECK, gate.PG_CHECK]), \
             patch.object(gate, 'scope', return_value={'checks': [gate.PYTEST_CHECK, gate.PG_CHECK], 'reason': 'fixture'}), \
             patch.object(gate, 'base_red_snapshot', return_value={}), \
             patch.object(gate, 'verify', side_effect=lambda *a: dict(result)), \
             patch.object(gate, 'candidate_failures', return_value=failures):
            rc = gate._run_attempt('auto-policy', PROJECT, [queued[0]['spec']], 'master', False,
                                   base, base, None, 0, None)
        self.assertEqual(rc, 0)
        self.assertIn(NODE, rechain.text(repo, 'master', policy.PATH))
        self.assertTrue(any(e['type'] == 'window-verified' for e in fold.read_events()))
        self.assertFalse(any(e['type'] == 'window-dropped' for e in fold.read_events()))

    def test_expiry_removes_row_escalates_fix_and_does_not_reuse_old_evidence(self):
        repo = self.repo()
        self.observation('1'); self.observation('2')
        q.apply(PROJECT, now=self.now)
        events = fold.read_events()
        branch = merge_queue.queue(events, PROJECT)[0]['branch']
        text = rechain.text(repo, branch, policy.PATH)
        self.observation('3', minutes=6*24*60)
        self.observation('4', minutes=6*24*60+1)
        future = self.now+timedelta(days=7)
        q.apply(PROJECT, now=future)
        self.assertNotIn(NODE, policy.render(text, fold.read_events(), PROJECT, future))
        self.assertFalse(q.active(fold.read_events(), PROJECT, future))
        alarms = [e for e in fold.read_events() if e['type'] == 'escalation-blocking']
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]['node'], NODE)
        q.apply(PROJECT, now=future)
        self.assertEqual(len([e for e in fold.read_events() if e['type'] == 'escalation-blocking']), 1)

    def test_fix_briefs_and_expiry_escalations_have_independent_node_subjects(self):
        other = 'tests/test_other.py::test_boundary'
        self.observation('1', [NODE, other]); self.observation('2', [NODE, other])
        q.apply(PROJECT, now=self.now)
        briefs = [e for e in fold.read_events() if e['type'] == 'brief']
        self.assertEqual(len({e['subject'] for e in briefs}), 2)
        q.apply(PROJECT, now=self.now+timedelta(days=7))
        alarms = [e for e in fold.read_events() if e['type'] == 'escalation-blocking']
        self.assertEqual({e['subject'] for e in alarms}, {e['subject'] for e in briefs})
        self.assertEqual({e['ref'] for e in alarms}, {e['_src'] for e in briefs})

    def test_pg_infrastructure_step_is_never_excused_by_named_quarantine(self):
        import json
        import subprocess
        metadata = {'jobs': [{'name': gate.PG_CHECK, 'databaseId': 123, 'conclusion': 'failure',
                             'steps': [{'name': 'Apply migrations', 'conclusion': 'failure'}]}]}
        completed = subprocess.CompletedProcess([], 0, json.dumps(metadata), '')
        with patch.object(gate.subprocess, 'run', return_value=completed), self.assertRaisesRegex(RuntimeError, 'non-test step'):
            gate.pg_result(self.root, {'checks': {gate.PG_CHECK: {'run_id': '123'}}})

    def pg_job(self, failed=None):
        steps = copy.deepcopy(PG_STEPS)
        if failed is not None:
            for step in steps:
                if step['conclusion'] != 'skipped':
                    step['conclusion'] = 'failure' if step['name'] in failed else 'success'
        return dict(name=gate.PG_CHECK, databaseId=126438799694, conclusion='failure', steps=steps)

    def pg_call(self, job, log=PG_LOG, **kwargs):
        def evidence(args, **unused):
            if args == ['gh', 'run', 'view', '123', '--json', 'jobs']:
                return subprocess.CompletedProcess(args, 0, json.dumps({'jobs': [job]}), '')
            self.assertEqual(args, ['gh', 'run', 'view', '--job', '126438799694', '--log-failed'])
            return subprocess.CompletedProcess(args, 0, log, '')
        with patch.object(gate.subprocess, 'run', side_effect=evidence) as run:
            result = gate.pg_result(self.root, {'checks': {gate.PG_CHECK: {'run_id': '123'}}}, **kwargs)
        return result, run.call_args_list

    def assert_pg_ids(self, result):
        failed, ids = result
        self.assertTrue(failed)
        self.assertEqual(set(ids), {PG_FAILED, PG_ERROR})
        self.assertTrue(ids[PG_FAILED].startswith('FAILED'))
        self.assertIn('AssertionError', ids[PG_FAILED])
        self.assertTrue(ids[PG_ERROR].startswith('ERROR'))
        self.assertIn('leftover cluster-global role doit_verifier_ro', ids[PG_ERROR])

    def test_pg_scratch_step_failure_is_parsed_as_test_failure(self):
        self.assertEqual(len(PG_STEPS), 26)
        self.assertEqual(len(PG_LOG.splitlines()), 7)
        self.assert_pg_ids(self.pg_call(self.pg_job(), rehearsal=True)[0])

    def test_spec_1158_step_failure_is_parsed_as_test_failure(self):
        # Synthetic log: real summary lines reused under the same tab prefix.
        self.assert_pg_ids(self.pg_call(self.pg_job([SPEC_1158]), PG_SUMMARY_LOG, rehearsal=True)[0])

    def test_failed_test_step_with_no_parseable_ids_is_undetermined(self):
        for step in (PG_SCRATCH, SPEC_1158):
            with self.subTest(step=step), self.assertRaisesRegex(RuntimeError, 'no failing test id'):
                self.pg_call(self.pg_job([step]), PG_NO_IDS_LOG, rehearsal=True)
        self.assertEqual(self.pg_call(self.pg_job([SPEC_202]), PG_NO_IDS_LOG)[0], (True, {}))

    def test_default_mode_keeps_only_spec_202_as_test_step(self):
        for step in (PG_SCRATCH, SPEC_1158):
            with self.subTest(step=step), self.assertRaisesRegex(RuntimeError, 'non-test step'):
                self.pg_call(self.pg_job([step]))
        self.assert_pg_ids(self.pg_call(self.pg_job([SPEC_202]))[0])

    def test_other_failed_steps_still_make_pg_result_raise(self):
        infrastructure = [PG_STEPS[17]['name'], PG_STEPS[15]['name'], PG_STEPS[12]['name']]
        for failed in ([name] for name in infrastructure):
            with self.subTest(failed=failed), self.assertRaisesRegex(RuntimeError, 'non-test step'):
                self.pg_call(self.pg_job(failed), rehearsal=True)
        for name in (infrastructure[0], 'Run a new unlisted suite'):
            job = self.pg_job([PG_SCRATCH, name])
            if name == 'Run a new unlisted suite':
                job['steps'].append(dict(name=name, conclusion='failure'))
            with self.subTest(name=name), patch.object(gate.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, json.dumps({'jobs': [job]}), '')) as run:
                with self.assertRaisesRegex(RuntimeError, 'non-test step'):
                    gate.pg_result(self.root, {'checks': {gate.PG_CHECK: {'run_id': '123'}}}, rehearsal=True)
                self.assertFalse(any('--log-failed' in call.args[0] for call in run.call_args_list))

    def test_successful_pg_parity_job_reads_no_log(self):
        for rehearsal in (False, True):
            job = self.pg_job([])
            job['conclusion'] = 'success'
            result, calls = self.pg_call(job, rehearsal=rehearsal)
            self.assertEqual(result, (False, {}))
            self.assertFalse(any('--log-failed' in call.args[0] for call in calls))

    def test_default_mode_spec_202_only_failure_without_ids_returns_empty(self):
        self.assertEqual(self.pg_call(self.pg_job([SPEC_202]), PG_NO_IDS_LOG)[0], (True, {}))

    def test_rehearsal_mode_spec_202_only_failure_without_ids_is_unchanged(self):
        self.assertEqual(self.pg_call(self.pg_job([SPEC_202]), PG_NO_IDS_LOG, rehearsal=True)[0], (True, {}))

    def test_failed_job_with_no_failed_step_list_reads_log(self):
        for rehearsal in (False, True):
            job = self.pg_job()
            del job['steps']
            result, calls = self.pg_call(job, rehearsal=rehearsal)
            self.assert_pg_ids(result)
            self.assertTrue(any('--log-failed' in call.args[0] for call in calls))

    def test_named_quarantine_unfreezes_nightly_but_unknown_failures_still_hold(self):
        self.observation('1'); self.observation('2')
        q.apply(PROJECT, now=self.now)
        events = fold.read_events()
        with patch.object(fold, 'nightly_unowned', return_value=[NODE]):
            self.assertIsNone(merge_queue.merge_freeze(events, PROJECT))
        with patch.object(fold, 'nightly_unowned', return_value=[NODE, 'unidentified suite failure']):
            self.assertIn('unidentified suite failure', merge_queue.merge_freeze(events, PROJECT))


if __name__ == '__main__':
    unittest.main()
