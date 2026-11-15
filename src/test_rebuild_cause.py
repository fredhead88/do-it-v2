"""Scratch-only evidence for selected rebuild causes and their persisted transport."""
import contextlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BOOT = tempfile.TemporaryDirectory(prefix='cause-import-')
os.environ.update(DOIT_ROOT=BOOT.name, HOME=BOOT.name, DOIT_NO_POKE='1')
import dispatch
import fold
import lane_rules
import nextstep as ns
import tick
from test_dispatch_cwd import CwdFixture, CapturedLaunch

CAUSES = ('merge-gate', 'rejected-criterion', 'conflict', 'stale-branch', 'launch-failure', 'other')
NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
SID = 'L-spec-9955'


def event(kind, minute=-60, **fields):
    return dict(type=kind, subject=SID, ts=(NOW + timedelta(minutes=minute)).isoformat(),
                actor='operator', _src=f'L-operator-fixture.jsonl:{20000+minute}') | fields


def history():
    return [event('spec-written', -180, project='project'),
            event('spawn-done', -179, actor='spec-auditor', bad_cut=False),
            event('build-started', -170, spawn='L-builder-9000'),
            event('build-done', -160, spawn='L-builder-9000', status='DONE'),
            event('spawn-done', -159, spawn='L-builder-9000', role='builder')]


def signed(ac, body):
    raw = json.dumps(body, sort_keys=True, default=str)
    print(json.dumps(dict(url='scratch://rebuild-cause/' + ac, status=0,
                         body_sha256=hashlib.sha256(raw.encode()).hexdigest(), body_excerpt=body)))


class RebuildCauseTests(CwdFixture, unittest.TestCase):
    def selected(self, rows, **kw):
        specs = {e['subject']: dict(state='written') for e in rows if e['subject'].startswith('L-spec-')}
        return next(s for s in ns.next_steps(rows, specs, NOW,
                    lane_rules.load(), **kw) if s.item == SID)

    def test_ac1_trigger_causes(self):
        observations = []
        with self.fixture():
            for kind, cause in [('merge-gate-rework', 'merge-gate'),
                    ('build-blocked', 'merge-gate'), ('rejected-criterion', 'rejected-criterion'),
                    ('must-fix', 'rejected-criterion'), ('conflict-rework', 'conflict'),
                    ('merge-conflict', 'conflict'), ('branch-recut', 'stale-branch'),
                    ('branch-recut-refused', 'stale-branch'), ('lesson', 'other')]:
                with self.subTest(kind=kind):
                    source = event(kind, -90, reason='handin-check:shape')
                    decision = event('decision', -60, rework='yes', ref=source['_src'])
                    rows = history() + [source, decision]
                    step = self.selected(rows)
                    self.assertEqual((step.action, step.cause), ('rebuild', cause), step)
                    self.assertEqual((step.trigger, step.since), (decision['_src'], decision['ts']))
                    observations.append(asdict(step))
            for variant in ('missing', 'future', 'other-subject', 'recursive', 'unrecognized'):
                source = event('merge-gate-rework', -90)
                if variant == 'future':
                    source = event('must-fix', -30)
                elif variant == 'other-subject':
                    source['subject'] = 'L-spec-9956'
                elif variant == 'recursive':
                    source.update(type='decision', ref='earlier:1')
                elif variant == 'unrecognized':
                    source['type'] = 'lesson'
                decision = event('decision', -60, rework='yes',
                                 ref='missing:1' if variant == 'missing' else source['_src'])
                rows = sorted(history() + [source, decision], key=lambda e: e['ts'])
                step = self.selected(rows)
                self.assertEqual(step.cause, 'other', (variant, step))
                observations.append(dict(variant=variant, step=asdict(step)))
            for kind, fields, cause in [
                    ('merge-conflict', {}, 'conflict'), ('window-dropped', dict(reason='owner'), 'other'),
                    ('build-blocked', dict(reason='handin-check:secret-scan'), 'merge-gate'),
                    ('review', dict(n_blocking=1, spawn='L-reviewer-9001'), 'rejected-criterion')]:
                rows = history() + [event(kind, -60, **fields)]
                if kind == 'review':
                    rows.append(event('must-fix', -59, spawn='L-reviewer-9001', criterion='AC1'))
                step = self.selected(rows)
                self.assertEqual((step.action, step.cause), ('rebuild', cause), step)
                observations.append(asdict(step))
            for reason in ('preflight:runner', 'crash:runner', 'vanished', 'timeout'):
                for repeat in (False, True):
                    rows = history() if repeat else history()[:2]
                    if repeat:
                        rows.append(event('decision', -90, rework='yes'))
                    rows += [event('build-started', -80, spawn='L-builder-9002'),
                             event('spawn-failed', -60, spawn='L-builder-9002', role='builder',
                                   reason=reason, exit_code=124 if reason == 'timeout' else 1)]
                    step = self.selected(rows)
                    self.assertEqual((step.action, step.cause),
                                     ('rebuild' if repeat else 'build', 'launch-failure'), step)
                    observations.append(asdict(step))
            for reason in ('stale-base', 'branch-not-descendant'):
                rows = history() + [event('spawn-failed', -60, role='builder', reason=reason, exit_code=1)]
                step = self.selected(rows)
                self.assertEqual((step.action, step.cause), ('build', 'stale-branch'), step)
                observations.append(asdict(step))
            # A later incidental signal must not replace the selected decision's ref.
            source = event('conflict-rework', -90)
            rows = history() + [source, event('decision', -60, rework='yes', ref=source['_src']),
                    event('must-fix', -50, subject='L-spec-9956'),
                    event('spawn-failed', -40, role='research', reason='timeout', subject='L-spec-9956')]
            self.assertEqual(self.selected(rows).cause, 'conflict')
        signed('AC1', observations)

    def test_ac2_step_transport(self):
        observations = []
        for source, cause in [('merge-gate-rework', 'merge-gate'), ('must-fix', 'rejected-criterion'),
                ('conflict-rework', 'conflict'), ('branch-recut', 'stale-branch'), ('lesson', 'other'),
                ('spawn-failed', 'launch-failure'), ('stale-base', 'stale-branch'),
                ('branch-not-descendant', 'stale-branch'), ('first-build', None)]:
            with self.fixture():
                rows = history()
                if source == 'first-build':
                    rows = rows[:2]
                elif source in ('stale-base', 'branch-not-descendant'):
                    rows += [event('spawn-failed', -60, role='builder', reason=source, exit_code=1)]
                elif source == 'spawn-failed':
                    rows = rows[:2] + [event('build-started', -80, spawn='L-builder-9002'),
                        event(source, -60, role='builder', spawn='L-builder-9002', reason='timeout', exit_code=124)]
                else:
                    trigger = event(source, -90)
                    rows += [trigger, event('decision', -60, rework='yes', action='rework', ref=trigger['_src'])]
                step = self.selected(rows)
                calls = []
                def runner(argv):
                    calls.append(argv)
                    return dict(code=0, stdout=json.dumps(dict(detached=12345))
                                if argv[:2] == ['doit', 'dispatch'] else
                                '0' if 'rev-list' in argv else '/packet.md')
                runner.events, runner.now = rows, NOW
                self.assertEqual(tick.launch(step, dispatcher=runner), '12345')
                argv = next(a for a in calls if a[:2] == ['doit', 'dispatch'])
                if cause is None:
                    self.assertNotIn('--cause', argv)
                else:
                    self.assertEqual(argv[argv.index('--cause')+1], cause)
                self.assertEqual(json.loads(json.dumps(asdict(step)))['cause'], cause)
                observations.append(dict(step=asdict(step), argv=argv))
        with self.fixture():
            step = self.selected(history()[:2])
            self.assertEqual((step.action, step.cause), ('build', None))
            self.assertIsNone(ns.Step(SID, 'spec', 'checking', 'action', 'grade', 'tick', None,
                                     'fixture', None, NOW.isoformat()).cause)
        signed('AC2', observations)

    def cli(self, argv):
        with patch.object(sys, 'argv', [dispatch.__file__, *argv]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                runpy.run_path(dispatch.__file__, run_name='__main__')
            except CapturedLaunch:
                return 'launched'
            except SystemExit as exc:
                return exc.code

    def test_ac3_dispatch_arguments(self):
        observations = []
        for cause in (*CAUSES, None):
            with self.fixture():
                argv = ['builder', SID, '--packet', str(self.packet)]
                if cause is not None:
                    argv += ['--cause', cause]
                self.assertEqual(self.cli(argv), 'launched')
                self.assertEqual(self.recorded_start()['cause'], cause or 'other')
                observations.append(dict(cause=cause, route='foreground'))
            with self.fixture():
                with patch.object(subprocess, 'Popen') as fork:
                    fork.return_value.pid = 12345
                    self.assertEqual(self.cli(argv[:2] + ['--packet', str(self.packet), '--detach'] +
                        (['--cause', cause] if cause else [])), 0)
                    child = fork.call_args.args[0]
                    self.assertNotIn('--detach', child)
                    self.assertEqual('--cause' in child, cause is not None)
                    if cause:
                        self.assertEqual(child[child.index('--cause')+1], cause)
                # Execute the actual forwarded argv after restoring real Git subprocesses.
                self.assertEqual(self.cli(child[2:]), 'launched')
                self.assertEqual(self.recorded_start()['cause'], cause or 'other')
                observations.append(dict(cause=cause, route='detached-child'))
        for role, cause in [('builder', 'arbitrary'), ('reviewer', 'merge-gate')]:
            with self.fixture(), patch.object(subprocess, 'Popen') as fork:
                before = {p.name: p.read_bytes() for p in (self.root/'events').glob('*.jsonl')}
                self.assertNotEqual(self.cli([role, SID, '--cause', cause, '--detach',
                    '--packet', str(self.packet)]), 'launched')
                fork.assert_not_called()
                self.assertEqual(self.launches, [])
                self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root/'events').glob('*.jsonl')})
                args = self.args(role)
                args.cause = cause
                self.assertIsInstance(self.invoke(args, self.callers[0]), str)
                self.assertEqual(self.launches, [])
        for issuer, explicit in [('tick', 'conflict'), ('executor', 'rejected-criterion'), ('foreman', 'merge-gate')]:
            for repeat, cause in [(False, None), (True, None), (True, explicit)]:
                with self.fixture():
                    os.environ['DOIT_LEDGER_FILE'] = f'L-{issuer}-fixture.jsonl'
                    if repeat:
                        self.seed('build-started', actor='builder', spawn='L-builder-9000')
                        self.seed('build-done', actor='builder', spawn='L-builder-9000', status='DONE')
                        self.seed('spawn-done', actor='builder', spawn='L-builder-9000', role='builder')
                    argv = ['builder', SID, '--packet', str(self.packet)] + (['--cause', cause] if cause else [])
                    self.assertEqual(self.cli(argv), 'launched')
                    row = next(e for e in fold.read_events(use_cache=False) if e['type'] == 'build-started'
                               and e.get('spawn') != 'L-builder-9000')
                    self.assertEqual(row.get('cause'), cause or ('other' if repeat else None))
                    self.assertEqual('cause' in row, repeat)
                    observations.append(dict(issuer=issuer, repeat=repeat, explicit=cause, cause=row.get('cause')))
        signed('AC3', observations)

    def test_ac4_start_event_round_trip(self):
        observations = []
        cases = [('operator', False, c, c) for c in CAUSES]
        cases += [('operator', False, None, 'other'), ('unknown', False, None, 'other')]
        for issuer, explicit in [('tick', 'conflict'), ('executor', 'rejected-criterion'), ('foreman', 'merge-gate')]:
            cases += [(issuer, False, None, None), (issuer, True, None, 'other'),
                      (issuer, True, explicit, explicit)]
        for issuer, repeat, cause, expected in cases:
            with self.fixture():
                os.environ['DOIT_LEDGER_FILE'] = f'L-{issuer}-fixture.jsonl'
                if repeat:
                    self.seed('build-started', actor='builder', spawn='L-builder-9000', worktree=str(self.tree))
                    self.seed('build-done', actor='builder', spawn='L-builder-9000', status='DONE')
                    self.seed('spawn-done', actor='builder', spawn='L-builder-9000', role='builder')
                before = {p: p.read_bytes() for p in (self.root/'events').glob('*.jsonl')}
                args = self.args()
                if cause is not None:
                    args.cause = cause
                # args without cause exercises old direct callers as well.
                with patch.object(dispatch, 'emit', wraps=dispatch.emit) as writer:
                    self.assertEqual(self.invoke(args, self.callers[0]), 'launched', self.stderr.getvalue())
                    self.assertTrue(any(c.args[2] == 'build-started' for c in writer.call_args_list))
                for p, raw in before.items():
                    self.assertTrue(p.read_bytes().startswith(raw))
                code = 'import fold,json; print(json.dumps(fold.read_events(use_cache=False)))'
                reader = subprocess.Popen([sys.executable, '-B', '-c', code], cwd=Path(dispatch.__file__).parent,
                    env=dict(os.environ), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = reader.communicate(timeout=30)
                self.assertEqual(reader.returncode, 0, stderr)
                row = next(e for e in json.loads(stdout) if e['type'] == 'build-started'
                           and e.get('spawn') != 'L-builder-9000')
                self.assertEqual(row.get('cause'), expected, (issuer, repeat, cause, row))
                self.assertEqual('cause' in row, expected is not None)
                for key in ('subject', 'spawn', 'project', 'worktree', 'base_sha', 'issued_by',
                            'backend', 'window_min', 'waiter_pid', 'waiter_host', 'waiter_proc_start'):
                    self.assertIn(key, row)
                self.assertEqual((row['subject'], row['project'], row['worktree'], row['base_sha']),
                                 (SID, 'project', str(self.tree), self.pin))
                observations.append(dict(issuer=issuer, repeat=repeat, explicit=cause,
                                         cause=row.get('cause'), persisted=True, legacy_unchanged=True))
        with self.fixture():
            self.assertEqual(self.invoke(self.args('spec-auditor'), self.callers[0]), 'launched')
            self.assertNotIn('cause', self.recorded_start())
        signed('AC4', observations)

    def test_ac5_routing_invariants(self):
        observations = []
        with self.fixture():
            base = history() + [event('decision', -60, rework='yes')]
            for kind, fields in [('spec-killed', {}), ('dependency-hold', dict(waits_for='L-spec-9956')),
                    ('spec-shape-failed', {}),
                    ('branch-recut-refused', dict(reason='conflict', tip='fixture')),
                    ('build-started', dict(spawn='L-builder-9002'))]:
                rows = base + [event(kind, -30, **fields)]
                step = self.selected(rows)
                self.assertNotEqual(step.action, 'rebuild', step)
                self.assertIsNone(step.cause)
                self.assertEqual(self.selected(rows), step)
                observations.append(asdict(step))
            wave_rows = [event('spec-written', -180, subject=sid, charter='L-charter-9955',
                               project='project', footprint=[sid+'.py']) for sid in ('L-spec-9956', SID)]
            wave_rows.append(event('spawn-done', -179, actor='spec-auditor', bad_cut=False))
            wave = self.selected(wave_rows, waves={'L-spec-9956': 1, SID: 2})
            self.assertIsNone(wave.action)
            self.assertIn('L-spec-9956', wave.clears)
            observations.append(asdict(wave))
            for reason, expected in [('handin-check:secret-scan', 'rebuild'), ('handin-check:undetermined', None)]:
                rows = history() + [event('build-blocked', -60, reason=reason, spawn='L-builder-9000')]
                step = self.selected(rows)
                self.assertEqual(step.action, expected, step)
                self.assertEqual(step.cause, 'merge-gate' if expected else None)
                observations.append(asdict(step))
            rows = history()[:2] + [event('build-started', -10, spawn='L-builder-9002'),
                event('spawn-failed', -1, role='builder', spawn='L-builder-9002', reason='timeout', exit_code=124)]
            waiting = self.selected(rows)
            self.assertIsNone(waiting.action)
            self.assertIn('retry-wait', waiting.reason)
            observations.append(asdict(waiting))
            exhausted = history() + [event('decision', -100, rework='yes')]
            for n in range(3):
                exhausted += [event('build-started', -90+n*10, spawn=f'L-builder-{9010+n}'),
                    event('spawn-failed', -89+n*10, spawn=f'L-builder-{9010+n}', role='builder', reason='timeout', exit_code=124)]
            step = self.selected(exhausted)
            self.assertIsNone(step.action)
            self.assertIn('retries-exhausted', step.reason)
            observations.append(asdict(step))
        results = self.regressions()
        signed('AC5', dict(holds=observations, regressions=results))

    def regressions(self):
        repo = Path(__file__).resolve().parent.parent
        base = subprocess.check_output(['git', 'merge-base', 'HEAD', 'origin/main'], cwd=repo, text=True).strip()
        files = ['nextstep', 'lane_driver', 'tick', 'dispatch', 'nextstep_base_refresh', 'tick_held',
                 'tick_retry1851', 'tick_wait_notify', 'lane_driver_log', 'dispatch_cwd',
                 'dispatch_failure_detail', 'dispatch_item_isolation', 'dispatch_manual_gate']
        allowed = {'test_retry1851_AC1_detached_marker_preserves_trigger_and_request_window',
                   'test_retry1851_AC1_silent_checker_cap_late_evidence_and_resets',
                   'test_retry1851_AC1_build_freshness_and_real_gate',
                   'test_retry1851_AC1_undated_failure_keeps_wall_cap_hold'}
        def run(tree, name, home):
            env = {k: v for k, v in os.environ.items() if not k.startswith(('SUPABASE_DB_URL', 'GIT_'))}
            env.update(HOME=str(home), DOIT_ROOT=str(home/'root'), DOIT_NO_POKE='1',
                       PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(tree/'src'))
            result = subprocess.run(['/usr/bin/python3', str(tree/'src'/('test_'+name+'.py'))],
                        cwd=tree, env=env, capture_output=True, text=True, timeout=300)
            text = result.stdout + result.stderr
            # Compare failure identities and diagnostics, ignoring scratch paths, elapsed times and line offsets.
            blocks = re.findall(r'(?:FAIL|ERROR): .*?(?=\n={10}|\n-{10}\nRan |\Z)', text, re.S)
            def normalized(value):
                value = re.sub(r'File ".*?", line \d+', 'File <scratch>, line N', value)
                value = re.sub(r'/[^\s\"\']+', '<path>', value)
                return value.strip()
            return result.returncode, text, [normalized(b) for b in blocks]
        rows = []
        with tempfile.TemporaryDirectory(prefix='cause-regressions-', dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            scratch = root/'base'
            subprocess.run(['git', 'worktree', 'add', '--detach', str(scratch), base], cwd=repo,
                           check=True, capture_output=True)
            try:
                for name in files:
                    bc, bt, bf = run(scratch, name, root/('base-'+name))
                    hc, ht, hf = run(repo, name, root/('head-'+name))
                    if bc or hc:
                        self.assertEqual(hc, bc, (name, ht[-5000:], bt[-5000:]))
                        if name == 'tick' and 'L0486-AC7' in bt and 'L0486-AC7' in ht:
                            self.assertIn('worktree-reaped', bt)
                            self.assertIn('worktree-reaped', ht)
                            coverage = 'incomplete: import abort at L0486-AC7'
                        elif name == 'tick':
                            diagnostic = ('AssertionError: a changed key updates the daily digest '
                                          'without another brief')
                            for output in (bt, ht):
                                self.assertRegex(output, r'File "[^"\n]*/test_tick\.py", line 2759, in <module>')
                                self.assertEqual(output.strip().splitlines()[-1], diagnostic)
                            coverage = 'incomplete: module abort at test_tick.py:2759: ' + diagnostic
                        else:
                            self.assertEqual(name, 'nextstep', (name, ht[-5000:], bt[-5000:]))
                            self.assertTrue(bf, bt[-5000:])
                            identities = re.findall(r'^(?:FAIL|ERROR): (\w+)', bt, re.M)
                            self.assertTrue(set(identities) <= allowed, identities)
                            self.assertEqual(hf, bf, (name, hf, bf))
                            coverage = 'named baseline failures only'
                    else:
                        coverage = 'pass'
                    rows.append(dict(file='src/test_'+name+'.py', base=bc, branch=hc,
                                     coverage=coverage, base_failures=bf, failures=hf,
                                     base_diagnostic=bt[-1500:] if bc else '',
                                     branch_diagnostic=ht[-1500:] if hc else ''))
            finally:
                subprocess.run(['git', 'worktree', 'remove', '--force', str(scratch)], cwd=repo, check=True)
        return rows


if __name__ == '__main__':
    unittest.main()
