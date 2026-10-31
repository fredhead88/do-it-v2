"""Scratch-only regression coverage for queued-branch prechecks."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fold
import lane_rules
import merge_queue
import nextstep
import tick

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='precheck-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repos/proj'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'core.hooksPath', '/dev/null')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        self.commit({'shared': 'base\n', 'master-only': 'base\n'})
        self.base = self.git('rev-parse', 'HEAD')
        self.branch('A', {'shared': 'A\n'})
        self.branch('B', {'master-only': 'B\n'})
        self.branch('C', {'shared': 'C\n'})
        self.branch('D', {'D': 'clean\n'})
        self.branch('E', {'E': 'bad guard\n'})
        self.git('checkout', '-q', 'master')
        self.commit({'master-only': 'master\n'})
        self.master = self.git('rev-parse', 'HEAD')
        self.events = [dict(type='queue-add', subject=s, project='proj', branch=s,
                            rule='fixture', ts=(NOW-timedelta(minutes=10-i)).isoformat())
                       for i, s in enumerate('ABCD')]
        self.emitted = []
        for module, name, value in ((fold, 'ROOT', self.root),
                                    (fold, 'EVENTS', self.root / 'events')):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True,
                              text=True, capture_output=True).stdout.strip()

    def commit(self, files):
        for name, text in files.items():
            (self.repo / name).write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def branch(self, name, files):
        self.git('checkout', '-qb', name, self.base)
        self.commit(files)

    def emit(self, subject, kind, **fields):
        event = dict(subject=subject, type=kind, ts=NOW.isoformat(), **fields)
        self.emitted.append(event)
        return event

    def run_check(self, guards=None):
        import precheck
        return precheck.run('proj', events=self.events + self.emitted,
                            guard_runner=guards or (lambda tree: []), emit=self.emit, now=NOW)

    def evidence(self, criterion, body):
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url='cli://precheck/' + criterion, status=0,
                              body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                              body_excerpt=payload)))

    def test_conflicts_classified(self):
        result = self.run_check()
        self.assertEqual(result['clean'], ['A', 'D'])
        self.assertEqual(result['conflict_master'], {'B': 'master-only'})
        self.assertEqual(result['conflict_set'], {'C': 'shared'})
        self.evidence('AC1', result)

    def add_e(self):
        self.events.append(dict(type='queue-add', subject='E', project='proj', branch='E',
                                rule='fixture', ts=(NOW-timedelta(minutes=1)).isoformat()))

    @staticmethod
    def bad_e(tree):
        return ['tests/test_rule.py::test_e'] if (tree / 'E').exists() else []

    def test_guard_red_attributed(self):
        self.add_e()
        result = self.run_check(self.bad_e)
        self.assertEqual(result['guard_red'], {'E': ['tests/test_rule.py::test_e']})
        self.assertEqual(result['clean'], ['A', 'D'])
        self.assertEqual(result['guards'], 'green')
        self.assertEqual([e['subject'] for e in self.emitted if e['type'] == 'precheck-red'], ['E'])
        self.evidence('AC2-attributed', result)

    def test_guard_red_unattributed(self):
        self.add_e()
        result = self.run_check(lambda tree: ['tests/test_rule.py::test_base'])
        self.assertEqual(result['guards'], 'red-unattributed')
        self.assertEqual(result['clean'], [])
        self.assertLessEqual(len(result['guard_red']), 3)
        self.evidence('AC2-unattributed', result)

    def test_refs_untouched(self):
        import precheck
        before = (self.git('for-each-ref'), (self.repo / '.git/HEAD').read_bytes(),
                  (self.repo / '.git/index').read_bytes())
        paths, commands = [], []
        real_git = precheck.git
        def recording_git(repo, *args, **kw):
            commands.append(args)
            return real_git(repo, *args, **kw)
        with patch.object(precheck, 'git', side_effect=recording_git):
            self.run_check(lambda path: paths.append(path) or [])
        self.assertEqual(before, (self.git('for-each-ref'), (self.repo / '.git/HEAD').read_bytes(),
                                 (self.repo / '.git/index').read_bytes()))
        self.assertTrue(paths)
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertFalse({c[0] for c in commands} &
                         {'checkout', 'switch', 'reset', 'update-ref', 'branch', 'worktree'})
        self.evidence('AC3', dict(refs_head_index_unchanged=True, temporary_trees_removed=True))

    def test_per_spec_events_once(self):
        self.add_e()
        self.run_check(self.bad_e)
        self.run_check(self.bad_e)
        for kind, spec in [('merge-conflict', 'B'), ('precheck-red', 'E')]:
            self.assertEqual(len([e for e in self.emitted if e['type'] == kind and e['subject'] == spec]), 1)
        self.git('checkout', '-q', 'B')
        self.commit({'extra': 'new tip\n'})
        self.git('checkout', '-q', 'master')
        self.events.append(dict(type='queue-add', subject='B', project='proj', branch='B',
                                rule='fixture', ts=(NOW+timedelta(minutes=1)).isoformat()))
        self.run_check(self.bad_e)
        conflicts = [e for e in self.emitted if e['type'] == 'merge-conflict']
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(e['source'] == 'precheck' for e in conflicts))
        self.assertNotEqual(conflicts[0]['branch_sha'], conflicts[1]['branch_sha'])
        self.evidence('AC4-dedup', dict(master_conflicts=2, guard_red_events=1))

    def test_precheck_red_routes_rework(self):
        outputs = []
        for kind in ('merge-conflict', 'precheck-red'):
            evs = [dict(type='build-done', subject='L-spec-9001', status='DONE',
                        ts=(NOW-timedelta(hours=1)).isoformat(), actor='builder'),
                   dict(type=kind, subject='L-spec-9001', actor='tick',
                        ts=(NOW-timedelta(minutes=1)).isoformat(), _src='L-tick-local.jsonl:1')]
            steps = nextstep.next_steps(evs, {'L-spec-9001': {'state': 'graded'}}, NOW, lane_rules.load())
            step = next(s for s in steps if s.item == 'L-spec-9001')
            self.assertEqual((step.action, step.owner), ('rebuild', 'tick'))
            outputs.append(step.action)
        self.evidence('AC4-rework', dict(actions=outputs))

    def test_window_run_uses_precheck(self):
        fresh = dict(type='precheck', subject='precheck:proj', project='proj', master=self.master,
                     clean=['D', 'A'], guards='green', ts=NOW.isoformat())
        for scenario in ('fresh', 'stale', 'error'):
            with self.subTest(scenario=scenario):
                events = self.events + [dict(fresh, ts=(NOW-timedelta(hours=1)).isoformat())
                                        if scenario != 'fresh' else fresh]
                batches, checks, launches = [], [], []
                def check(project):
                    checks.append(project)
                    if scenario == 'error':
                        raise RuntimeError('fixture error')
                    return fresh
                def launch(argv):
                    launches.append(argv)
                    return {'code': 0}
                out = io.StringIO()
                with patch.object(merge_queue.window_gate, 'train_events', return_value=events), \
                     patch.object(merge_queue, 'next_candidate', return_value='A'), \
                     patch.object(merge_queue, '_train_emit', side_effect=lambda *a, **kw: batches.append((a, kw))), \
                     patch.object(merge_queue, 'spec_paths', side_effect=lambda r, m, b: [('M', b)]), \
                     patch.object(merge_queue, 'record_cap'), redirect_stdout(out):
                    rc = merge_queue.window_run('proj', runner=launch, now=NOW, precheck_runner=check)
                self.assertEqual(rc, 0)
                batch = next(kw for args, kw in batches if args[2] == 'queue-batch')
                if scenario == 'error':
                    self.assertEqual(batch['specs'], 'A,B,C,D')
                    self.assertEqual(batch['precheck'], 'none')
                    self.assertIn('precheck undetermined', out.getvalue())
                else:
                    self.assertEqual(batch['specs'], 'A,B,C,D')
                    self.assertEqual(batch['precheck'], fresh['ts'])
                    self.assertEqual(batch['skipped'], [])
                    self.assertIn('selected 4', out.getvalue())
                self.assertEqual(checks, [] if scenario == 'fresh' else ['proj'])
        self.evidence('AC5', dict(fresh=True, stale_refreshed=True, error_fallback=True))

    def test_cli_json_and_event_writer(self):
        import precheck
        self.add_e()
        # A scratch executable stands in for the project's pytest environment.
        # The actual CLI, archive, guard command, and ledger writer run normally.
        python = self.repo / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text("#!/usr/bin/python3\nimport pathlib, sys\n"
                          "assert sys.argv[1:4] == ['-m', 'pytest', '-q']\n"
                          "assert any('test_' in a for a in sys.argv[4:])\n"
                          "if pathlib.Path('E').exists():\n"
                          " print('FAILED tests/test_rule.py::test_e - fixture')\n"
                          " sys.exit(1)\n")
        python.chmod(0o755)
        events_dir = self.root / 'events'
        events_dir.mkdir()
        (events_dir / 'L-executor-fixture.jsonl').write_text(
            ''.join(json.dumps(e) + '\n' for e in self.events))
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_LEDGER_FILE='L-executor-precheck.jsonl',
                   DOIT_NO_POKE='1')
        env.pop('DOIT_PROJECT', None)
        response = subprocess.run([precheck.DOIT, 'precheck', '--project', 'proj', '--json'],
                                  env=env, capture_output=True, text=True)
        self.assertEqual(response.returncode, 0, response.stderr)
        result = json.loads(response.stdout)
        self.assertEqual(result['clean'], ['A', 'D'])
        self.assertEqual(result['conflict_master'], {'B': 'master-only'})
        self.assertEqual(result['conflict_set'], {'C': 'shared'})
        self.assertEqual(result['guard_red'], {'E': ['tests/test_rule.py::test_e']})
        written = [json.loads(line) for line in
                   (events_dir / 'L-executor-precheck.jsonl').read_text().splitlines()]
        self.assertEqual([e['type'] for e in written], ['merge-conflict', 'precheck-red', 'precheck'])
        self.assertEqual(result, written[-1])
        for e in written:
            self.assertIsNone(fold.required_reason(e))
            self.assertIsNone(fold.admits(e, 'executor'))
        self.evidence('CLI', dict(clean=result['clean'], guards=result['guards'],
                                  emitted=[e['type'] for e in written]))

    def test_tick_launches_precheck(self):
        import precheck
        fresh = dict(type='precheck', subject='precheck:proj', project='proj', master=self.master,
                     ts=NOW.isoformat(), clean=['A', 'D'])
        with patch.object(precheck, 'subprocess') as process:
            process.Popen.return_value.pid = 123
            for previous, expected in ((None, True), (fresh, False),
                                       (dict(fresh, master=self.base), True),
                                       (dict(fresh, ts=(NOW-timedelta(minutes=21)).isoformat()), True)):
                process.Popen.reset_mock()
                events = self.events + ([previous] if previous else [])
                # Only mock the launch: git probes must remain real.
                with patch.object(precheck, 'git', side_effect=lambda repo, *args, **kw:
                                  subprocess.run(['git', *args], cwd=repo, text=True,
                                                 capture_output=True, check=kw.get('check', True))):
                    self.assertEqual(tick.launch_precheck(events, NOW, project='proj'), expected)
                self.assertEqual(process.Popen.call_count, int(expected))
        lock = self.root / 'state/precheck.proj.lock'
        with lock.open('a') as stream, patch.object(precheck.subprocess, 'Popen') as launch:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(tick.launch_precheck(self.events, NOW, project='proj'))
            self.assertFalse(tick.launch_precheck([], NOW, project='proj'))
            launch.assert_not_called()
        self.evidence('AC6', dict(stale=True, changed_master=True, fresh_skipped=True,
                                 lock_skipped=True, empty_skipped=True))


if __name__ == '__main__':
    unittest.main()
