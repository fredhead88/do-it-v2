"""Automatic base-red policy through real Git objects and the normal window gate."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
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
