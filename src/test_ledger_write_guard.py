#!/usr/bin/env python3
"""Scratch-only regression evidence for test write refusal and queue admission."""
import os
import pathlib
import tempfile
import unittest
import ast
from collections import Counter
import copy
import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='guard-import-')
os.environ['DOIT_ROOT'] = IMPORT_ROOT.name
os.environ.pop('DOIT_PROJECT', None)
import ledger_write_guard as guard
import dispatch
import fold
import ledger_rollup as rollup
import look_omens
import look_wallclock
import project_scope
import relay


def child_env(root, *, marked=True):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DOIT_', 'GIT_', 'SUPABASE_DB_URL'))}
    env.update(HOME=str(root / 'home'), DOIT_ROOT=str(root / 'root'),
               PYTHONPATH=str(REPO / 'src'), PYTHONDONTWRITEBYTECODE='1')
    if marked:
        env['DOIT_TEST_PROCESS'] = '1'
    return env


def python(code, env, *args):
    return subprocess.run([sys.executable, '-B', '-c', code, *map(str, args)],
                          env=env, cwd=REPO, capture_output=True, text=True, timeout=60)


def snapshot(root):
    return {str(p.relative_to(root)): ('link', os.readlink(p)) if p.is_symlink()
            else ('file', p.read_bytes()) if p.is_file() else ('directory',)
            for p in root.rglob('*')}


class ScratchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ledger-guard-')
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.live = self.root / 'surrogate'
        self.live.mkdir()
        self.safe = self.root / 'scratch'
        self.safe.mkdir()
        self.enterContext(patch.object(guard, '_account_root', return_value=self.live))
        self.enterContext(patch.dict(os.environ, DOIT_TEST_PROCESS='1'))
        for key in ('DOIT_TEST_MACHINE_ROOT', 'DOIT_TEST_LIVE_ROOT', 'DOIT_LEDGER_FILE', 'DOIT_PROJECT'):
            os.environ.pop(key, None)
        self.enterContext(patch.multiple(fold, ROOT=self.safe, EVENTS=self.safe / 'events', PROJECT=None))
        self.enterContext(patch.object(dispatch, 'EVENTS', self.safe / 'events'))


class RecognitionTests(ScratchTests):
    def test_operator_payload_is_not_test_identity(self):
        env = child_env(self.root, marked=False)
        for value in ('test_fixture.py', 'dispatch_checks_fixture.py', 'pytest', 'unittest'):
            with self.subTest(project=value):
                code = ("import sys; sys.argv=['dispatch.py','builder','L-spec-9999',"
                        f"'--project',{value!r}]; "
                        "sys.orig_argv=['python3',*sys.argv]; "
                        "import ledger_write_guard as g; assert not g.mark_test_process()")
                proc = python(code, env)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_entry_routes_and_ordinary_invocation(self):
        env = child_env(self.root, marked=False)
        probe = "import ledger_write_guard as g,os; print(g.mark_test_process(),os.getenv('DOIT_TEST_PROCESS'))"
        cases = [('operator', probe, 'False None'),
                 ('pytest invocation', "import sys; sys.argv=['pytest']; " + probe, 'True 1'),
                 ('unittest invocation', "import sys; sys.argv=['unittest']; " + probe, 'True 1'),
                 ('project is not identity', "import os; os.environ['DOIT_PROJECT']='test_fixture'; " + probe, 'False None')]
        for label, code, expected in cases:
            with self.subTest(route=label):
                proc = python(code, env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)
        for filename in ('test_direct.py', 'dispatch_checks_fixture.py'):
            script = self.root / filename
            script.write_text(probe + "\nos.environ['HOME']='changed'; os.environ['DOIT_ROOT']='changed'\n"
                              "import subprocess,sys\nsubprocess.run([sys.executable,'-c',"
                              "\"import os; assert os.getenv('DOIT_TEST_PROCESS')=='1'\"],check=True)\n")
            proc = subprocess.run([sys.executable, '-B', str(script)], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), 'True 1')
        harness = self.root / 'test_harness_import.py'
        harness.write_text("import harness,os; assert os.getenv('DOIT_TEST_PROCESS')=='1'")
        self.assertEqual(subprocess.run([sys.executable, str(harness)], env=env).returncode, 0)

    def test_real_unittest_and_active_module(self):
        script = self.root / 'runner_case.py'
        script.write_text("import unittest\nclass Probe(unittest.TestCase):\n"
                          " def test_marker(self):\n  import ledger_write_guard as g\n"
                          "  self.assertTrue(g.mark_test_process())\n")
        env = child_env(self.root, marked=False)
        env['PYTHONPATH'] += os.pathsep + str(self.root)
        proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'runner_case'],
                              cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (self.root / 'test_active.py').write_text(
            "import ledger_write_guard as g\ndef recognize(): return g.mark_test_process()\n")
        proc = python("import test_active; assert test_active.recognize()", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_inherited_python_and_cli(self):
        env = child_env(self.root)
        proc = python("import os; assert os.getenv('DOIT_TEST_PROCESS')=='1'", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run([str(REPO / 'doit'), 'append', 'observed', 'L-spec-9999', 'project=fixture'],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len((self.root / 'root/events/L-operator-local.jsonl').read_text().splitlines()), 1)

    def test_import_marks_before_home_rewrite_and_child_launch(self):
        for module in ('fold', 'dispatch'):
            script = self.root / 'test_early_import.py'
            script.write_text(f'import {module}\nimport os,subprocess,sys\n'
                              "assert os.environ['DOIT_TEST_PROCESS']=='1'\n"
                              "os.environ['HOME']='rewritten'; os.environ['DOIT_ROOT']='rewritten'\n"
                              "subprocess.run([sys.executable,'-c',\"import os; "
                              "assert os.getenv('DOIT_TEST_PROCESS')=='1'\"],check=True)\n")
            proc = subprocess.run([sys.executable, '-B', str(script)], env=child_env(self.root, marked=False),
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class RefusalTests(ScratchTests):
    def test_appenders_refuse_before_any_side_effect(self):
        import ledger_write_guard as guard
        import fold
        import dispatch
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            events = root / 'events'
            with patch.object(guard, '_account_root', return_value=root), \
                    patch.dict(os.environ, DOIT_TEST_PROCESS='1'), \
                    patch.multiple(fold, ROOT=root, EVENTS=events):
                for write in (lambda: fold.append(['charter-filed', 'L-charter-9999']),
                              lambda: dispatch.emit(events / 'L-operator-0001.jsonl',
                                                    {'subject': 'L-charter-9999'}, 'charter-filed')):
                    with self.assertRaisesRegex(SystemExit, '^refused: test process cannot write live ledger'):
                        write()
                    self.assertFalse(events.exists())

    def test_alias_routing_matrix_and_unchanged_inventory(self):
        events = self.live / 'events'
        events.mkdir()
        dst = events / 'L-operator-local.jsonl'
        dst.write_bytes(b'original bytes\n')
        (self.root / 'alias').symlink_to(self.live, target_is_directory=True)
        (self.safe / 'parent').symlink_to(events, target_is_directory=True)
        (self.safe / 'file.jsonl').symlink_to(dst)
        destinations = [dst, self.root / 'alias/events/new.jsonl', self.safe / 'parent/new.jsonl',
                        self.safe / 'file.jsonl', events / 'missing/nested.jsonl']
        for path in destinations:
            with self.subTest(destination=str(path)):
                before = snapshot(self.root)
                with patch.dict(os.environ, DOIT_LEDGER_FILE=str(path), HOME=str(self.safe)):
                    for writer in (lambda: fold.append(['observed', 'L-spec-9999', 'project=fixture']),
                                   lambda: dispatch.emit(path, {'subject': 'L-spec-9999'}, 'observed')):
                        with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
                            writer()
                self.assertEqual(snapshot(self.root), before)
        before = snapshot(self.root)
        with patch.object(fold, 'EVENTS', events):
            with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
                fold.append(['observed', 'L-spec-9999', '--inbox'])
        with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
            dispatch.alloc(events, 'L-builder-', '.jsonl')
        self.assertEqual(snapshot(self.root), before)

    def test_captured_roots_are_additive_and_resolution_fails_closed(self):
        for name in ('DOIT_TEST_LIVE_ROOT', 'DOIT_TEST_MACHINE_ROOT'):
            root = self.root / name
            root.mkdir()
            before = snapshot(self.root)
            with patch.dict(os.environ, {name: str(root)}):
                for target in (root / 'events/missing.jsonl', self.live / 'events/missing.jsonl'):
                    with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
                        guard.check(target)
            self.assertEqual(snapshot(self.root), before)
        loop = self.safe / 'loop'
        loop.symlink_to(loop)
        with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
            guard.check(loop / 'missing.jsonl')
        with patch.object(pathlib.Path, 'resolve', side_effect=PermissionError):
            with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
                guard.check(self.safe / 'missing.jsonl')

    def test_rollup_all_mutators_and_read_only_dry_run(self):
        events = self.live / 'events'
        events.mkdir()
        file = events / 'L-builder-0001.jsonl'
        file.write_bytes(b'original\n')
        candidate = rollup.Candidate(file, file.read_bytes(), file.stat(), rollup.digest(file.read_bytes()))
        args = SimpleNamespace(dry_run=False, min_age_hours=24, max_segment_mib=64)
        before = snapshot(self.root)
        for writer in (lambda: rollup.run(self.live, args),
                       lambda: rollup.set_aside(candidate, events / '.segments', Counter()),
                       lambda: rollup.publish([candidate], events / '.segments', '2026-11-07', Counter()),
                       lambda: rollup.pending_cleanup(events, {})):
            with self.assertRaisesRegex(SystemExit, '^' + guard.REFUSAL):
                writer()
            self.assertEqual(snapshot(self.root), before)
        # A clean empty events tree permits an actual read-only preview.
        file.unlink()
        before = snapshot(self.root)
        args.dry_run = True
        self.assertEqual(rollup.run(self.live, args), 0)
        self.assertEqual(snapshot(self.root), before)


class RoundTripTests(ScratchTests):
    def test_append_emit_inbox_and_absolute_file_read_in_fresh_process(self):
        for route in ('default', 'inbox', 'absolute', 'dispatch'):
            with self.subTest(route=route):
                root = self.safe / route
                events = root / 'events'
                events.mkdir(parents=True)
                file = events / ('L-inbox-local.jsonl' if route == 'inbox' else 'L-operator-local.jsonl')
                env = child_env(self.root)
                env.update(DOIT_ROOT=str(root))
                with patch.multiple(fold, ROOT=root, EVENTS=events), patch.dict(os.environ, DOIT_ROOT=str(root)):
                    args = ['observed', 'L-spec-9999', 'project=fixture']
                    if route == 'inbox':
                        args += ['--inbox']
                    if route == 'absolute':
                        file = events / 'L-operator-absolute.jsonl'
                        with patch.dict(os.environ, DOIT_LEDGER_FILE=str(file)):
                            fold.append(args)
                    elif route == 'dispatch':
                        self.assertIsNone(dispatch.emit(file, {'subject': 'L-spec-9999', 'project': 'fixture'}, 'observed'))
                    else:
                        fold.append(args)
                proc = python("import fold,json; print(json.dumps(fold.read_events(use_cache=False)))", env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                rows = json.loads(proc.stdout)
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual((rows[0]['subject'], rows[0]['type'], rows[0]['project']),
                                 ('L-spec-9999', 'observed', 'fixture'))
                self.assertEqual(rows[0]['_src'], file.name + ':1')
                self.assertEqual(len(file.read_text().splitlines()), 1)

    def test_ordinary_append_to_surrogate_and_guarded_scratch_rollup(self):
        env = child_env(self.root, marked=False)
        env['DOIT_ROOT'] = str(self.live)
        proc = python("import fold,ledger_write_guard as g; assert not g.mark_test_process(); "
                      "fold.append(['observed','L-spec-9999','project=fixture'])", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len((self.live / 'events/L-operator-local.jsonl').read_text().splitlines()), 1)
        events = self.safe / 'events'
        events.mkdir()
        file = events / 'L-builder-9999.jsonl'
        file.write_text('{}\n')
        candidate = rollup.Candidate(file, file.read_bytes(), file.stat(), rollup.digest(file.read_bytes()))
        directory = events / '.segments'
        directory.mkdir()
        # The real /proc scanner may conservatively retain the open source;
        # publication itself still writes and verifies the segment/index.
        rollup.publish([candidate], directory, '2026-11-07', Counter())
        self.assertTrue(list(directory.glob('*.idx.json')))
        rollup.pending_cleanup(events, rollup.member_bytes(events))


def charter(project, cid='L-charter-9991', **extra):
    return {'type': 'charter-filed', 'subject': cid, 'actor': 'operator',
            'ts': '2026-11-07T00:00:00Z', 'project': project, **extra}


class QueueTests(ScratchTests):
    def test_registration_agrees_with_inference(self):
        repos = self.safe / 'repos'
        repos.mkdir()
        (repos / 'plain').mkdir()
        (repos / 'alias').symlink_to(repos / 'plain', target_is_directory=True)
        (repos / 'dangling').symlink_to(repos / 'absent')
        (repos / 'file').write_text('x')
        for name in ('plain', 'alias', 'dangling', 'file', '', '../plain', '/plain', '.', None, 42, 'bad space'):
            with self.subTest(project=name):
                expected = name in ('plain', 'alias')
                self.assertEqual(project_scope.registered(self.safe, name), expected)
                self.assertEqual(isinstance(name, str) and name in relay.registered_projects(self.safe), expected)
                if expected:
                    project_scope.check_inferred(self.safe, {'type': 'spec-written', 'project': name}, None, [])
                else:
                    with self.assertRaises(SystemExit):
                        project_scope.check_inferred(self.safe, {'type': 'spec-written', 'project': name}, None, [])
        for configured, arguments in (('bootstrap', []), (None, ['project=bootstrap'])):
            project_scope.check_inferred(self.safe, {'type': 'spec-written', 'project': 'bootstrap'}, configured, arguments)
        self.assertEqual(relay.registered_projects(self.live), set())
        with patch.object(pathlib.Path, 'iterdir', side_effect=PermissionError):
            self.assertEqual(relay.registered_projects(self.safe), set())

    def test_newest_filing_explicit_roots_notes_lifecycle_and_input(self):
        other = self.root / 'other'
        (other / 'repos/other').mkdir(parents=True)
        (self.safe / 'repos/fixture').mkdir(parents=True)
        cases = [('fixture',), ('unknown',), (None,), ('',), (42,), ([],),
                 ('fixture', 'unknown'), ('unknown', 'fixture')]
        rows, eligible, excluded = [], [], []
        for number, projects in enumerate(cases, 1):
            cid = f'L-charter-{number:04d}'
            rows.extend(charter(project, cid) for project in projects)
            (eligible if projects[-1] == 'fixture' else excluded).append(cid)
        rows.append({k: v for k, v in charter('fixture', 'L-charter-0010').items() if k != 'project'})
        excluded.append('L-charter-0010')
        original = copy.deepcopy(rows)
        with patch.object(fold, 'ROOT', other):
            by_subject = fold.fold(rows)[3]
            for mapping in (None, by_subject):
                self.assertEqual(relay.open_charters(rows, mapping, root=self.safe), eligible)
                self.assertEqual(relay.open_charters(rows, mapping), [])
            ready, waiting = relay.plannable(rows, self.safe)
            self.assertEqual(ready, eligible)
            self.assertEqual(waiting, [])
            self.assertEqual(relay.unmeasured_footprints(rows, self.safe), eligible)
            notes = '\n'.join(relay.waiting_lines(rows, self.safe))
            self.assertIn(f'excluded {len(excluded)} open charter(s)', notes)
            for cid in excluded:
                self.assertNotIn(cid, notes)
            for cid in eligible:
                self.assertIn(cid, notes)
            for kind in ('cut-written', 'l1-complete', 'charter-retracted'):
                closed = rows + [{'type': kind, 'subject': eligible[0], 'actor': 'planner' if kind == 'cut-written' else 'operator'}]
                self.assertNotIn(eligible[0], relay.open_charters(closed, root=self.safe))
        self.assertEqual(rows, original)

    def test_corrective_readers_and_planner_stuck_use_effective_root(self):
        (self.safe / 'repos/fixture').mkdir(parents=True)
        (self.live / 'repos/other').mkdir(parents=True)
        (self.safe / 'content').mkdir()
        cid = 'L-charter-9991'
        condition, cls = 'quota-pause', 'quota'
        self.enterContext(patch.object(look_wallclock, '_CLASSES', {condition: cls}))
        (self.safe / 'content' / (cid + '.md')).write_text(f'corrects: {cls}, omen-demo\n')
        for project, expected in (('fixture', cid), ('other', None)):
            rows = [charter(project)]
            with patch.object(fold, 'ROOT', self.live):
                self.assertEqual(look_wallclock.fix_for(condition, self.safe, rows), expected)
                self.assertEqual(look_omens.fix_for('demo', self.safe, rows, {}), expected)
                rows += [{'type': 'planner-ended', 'subject': cid, 'ts': '2026-11-07T01:00:00Z'},
                         {'type': 'decision', 'subject': cid, 'ts': '2026-11-07T02:00:00Z'}]
                with patch.object(relay, 'planner_attempts', return_value={'next': 'escalate'}):
                    briefs = []
                    look_wallclock._check_planner_stuck(rows, self.safe, lambda *a, **k: cid, briefs)
                    self.assertEqual(briefs, [cid] if expected else [])
        rows = [charter('fixture'), charter('other')]
        self.assertIsNone(look_wallclock.fix_for(condition, self.safe, rows))
        self.assertIsNone(look_omens.fix_for('demo', self.safe, rows, {}))

    def test_unrelated_rows_survive_production_reads(self):
        events = self.safe / 'events'
        events.mkdir()
        rows = [dict(type='deploy-landed', subject='L-spec-9999', project='unknown'),
                dict(type='health-alarm', subject='L-spec-9999', project='unknown', alarm='demo', severity='info', line='demo'),
                dict(type='resource-created', subject='L-spec-9999', project='unknown', kind='demo', ref='demo', lane='demo'),
                dict(type='observed', subject='L-spec-9999')]
        for index, row in enumerate(rows):
            actor = 'health' if row['type'] == 'health-alarm' else 'executor' if row['type'] == 'deploy-landed' else 'operator'
            (events / f'L-{actor}-{index:04d}.jsonl').write_text(json.dumps(dict(v=1, ts='2026-11-07T00:00:00Z', **row)) + '\n')
        read = fold.read_events(use_cache=False)
        self.assertEqual({e['type'] for e in read}, {e['type'] for e in rows})
        self.assertEqual(len(read), len(rows))


REGRESSION_SUITES = tuple('src/test_' + name + '.py' for name in (
    'fold', 'dispatch', 'relay', 'packet', 'ledger_rollup', 'doit_test', 'up',
    'look_wallclock', 'look_omens', 'dispatch_cwd', 'dispatch_failure_detail',
    'dispatch_item_isolation', 'dispatch_manual_gate', 'fold_stalls', 'harness',
    'relay_builder', 'relay_supervision', 'routing'))


def git(*args, cwd=REPO):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True).strip()


def build_base():
    # The introduction commit's parent is immutable even after this branch moves.
    introduced = git('log', '--diff-filter=A', '--format=%H', '--', 'src/ledger_write_guard.py').splitlines()
    return git('rev-parse', introduced[-1] + '^') if introduced else git('rev-parse', 'HEAD')


def source_sizes(repo):
    return {name: len((repo / 'src' / name).read_text().splitlines()) for name in ('fold.py', 'dispatch.py')}


def failing_ids(file, proc, repo):
    output = proc.stdout + '\n' + proc.stderr
    failures = set()
    if file.endswith('test_routing.py'):
        constants = next(node.value for node in ast.walk(ast.parse((repo / file).read_text()))
                         if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASELINES' for t in node.targets))
        for name, ceiling in ast.literal_eval(constants).items():
            if len((repo / 'src' / name).read_text().splitlines()) > ceiling:
                failures.add(file + '::AC13::' + name)
    for kind, test_id in re.findall(r'^(FAIL|ERROR): (.+)$', output, re.MULTILINE):
        failures.add(file + '::' + kind + '::' + test_id)
    if proc.returncode and not failures:
        assertion = re.findall(r'^AssertionError(?:: (.*))?$', output, re.MULTILINE)
        if assertion:
            # Use the assertion's source expression, independent of temp paths
            # or changed fixture line numbers, for top-level script suites.
            frames = re.findall(r'File "([^"]+)", line (\d+)', output)
            for path, line in reversed(frames):
                if pathlib.Path(path).name == pathlib.Path(file).name:
                    tree = ast.parse((repo / file).read_text())
                    node = next((n for n in ast.walk(tree) if isinstance(n, ast.Assert)
                                 and n.lineno <= int(line) <= n.end_lineno), None)
                    if node:
                        label = ast.unparse(node.test)
                        # ok()/check() helpers report the caller's assertion label.
                        if label in ('cond', 'condition'):
                            label = assertion[-1].split(':', 1)[0]
                        failures.add(file + '::assert::' + label)
                        break
        if not failures:
            failures.add(file + '::ERROR::incomplete-script')
    if re.search(r'^(?:OK .*skipped=|\s*SKIP(?:PED)?\b|.*\.\.\. skipped )', output, re.MULTILINE):
        failures.add(file + '::SKIP')
    return sorted(failures)


class RegressionTests(unittest.TestCase):
    def test_named_scripts_against_immutable_base(self):
        baseline = build_base()
        evidence = pathlib.Path(tempfile.mkdtemp(prefix='ledger-guard-evidence-'))
        manifests = {}
        with tempfile.TemporaryDirectory(prefix='ledger-guard-comparison-') as directory:
            for label, revision in (('base', baseline), ('candidate', git('rev-parse', 'HEAD'))):
                repo = pathlib.Path(directory) / label
                git('worktree', 'add', '--quiet', '--detach', str(repo), revision)
                try:
                    if label == 'candidate':
                        # Includes the candidate's uncommitted edits during development.
                        for name in [*git('diff', '--name-only', baseline).splitlines(), 'src/ledger_write_guard.py']:
                            source = REPO / name
                            if source.is_file():
                                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(source, repo / name)
                    manifest = dict(files={}, sizes=source_sizes(repo), revision=revision)
                    for file in REGRESSION_SUITES:
                        with tempfile.TemporaryDirectory(dir=directory, prefix='script-') as scratch:
                            root = pathlib.Path(scratch)
                            env = child_env(root)
                            for name in ('home', 'root', 'tmp'):
                                (root / name).mkdir()
                            env.update(PYTHONPATH=str(repo / 'src'), TMPDIR=str(root / 'tmp'),
                                       TMP=str(root / 'tmp'), TEMP=str(root / 'tmp'),
                                       DOIT_TEST_MACHINE_ROOT=str(root / 'machine'))
                            try:
                                proc = subprocess.run([sys.executable, '-B', file], cwd=repo, env=env,
                                                      capture_output=True, text=True, timeout=900)
                                failures = failing_ids(file, proc, repo)
                                transcript = proc.stdout + '\n' + proc.stderr
                                result = dict(exit_code=proc.returncode, failing_ids=failures)
                            except subprocess.TimeoutExpired as exc:
                                transcript = str(exc)
                                result = dict(exit_code=124, failing_ids=[file + '::TIMEOUT'])
                            (evidence / (label + '-' + pathlib.Path(file).name + '.log')).write_text(transcript)
                            manifest['files'][file] = result
                            print(label, file, result, flush=True)
                    manifests[label] = manifest
                    (evidence / (label + '.json')).write_text(json.dumps(manifest, indent=2))
                finally:
                    git('worktree', 'remove', '--force', str(repo))
        print('AC5 transcripts and immutable-base manifests:', evidence, flush=True)
        self.assertEqual(set(manifests['base']['files']), set(REGRESSION_SUITES))
        self.assertEqual(set(manifests['candidate']['files']), set(REGRESSION_SUITES))
        for file in REGRESSION_SUITES:
            base = set(manifests['base']['files'][file]['failing_ids'])
            candidate = set(manifests['candidate']['files'][file]['failing_ids'])
            self.assertLessEqual(candidate, base, file)
            self.assertFalse(any('::SKIP' in f or '::TIMEOUT' in f for f in candidate), file)
        for name, count in manifests['candidate']['sizes'].items():
            self.assertLessEqual(count, manifests['base']['sizes'][name], name)


class InventoryTests(unittest.TestCase):
    def test_writer_inventory_and_guard_precedes_effects(self):
        writers = [(fold.append, 'path'), (dispatch.emit, 'dst'), (dispatch.alloc, 'd'),
                   (rollup.set_aside, 'candidate.path'), (rollup.publish, 'directory'),
                   (rollup.pending_cleanup, 'events_dir'), (rollup.run, 'events_dir')]
        for writer, destination in writers:
            source = inspect.getsource(writer)
            self.assertIn('.check(' + destination + ')', source, writer.__name__)
            tree = ast.parse(source)
            check = min(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == 'check')
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('mkdir', 'open', 'unlink', 'rename')):
                    self.assertLess(check, node.lineno, writer.__name__)
        self.assertEqual(inspect.signature(fold.append).parameters['strict'].default, False)
        self.assertEqual(inspect.signature(relay.open_charters).parameters['root'].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        self.assertIn('guard', guard.__file__)
        for file in ('relay.py', 'look_wallclock.py', 'look_omens.py'):
            tree = ast.parse((REPO / 'src' / file).read_text())
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and ((isinstance(n.func, ast.Name) and n.func.id == 'open_charters')
                          or (isinstance(n.func, ast.Attribute) and n.func.attr == 'open_charters'))]
            self.assertTrue(calls)
            self.assertTrue(all(any(k.arg == 'root' for k in call.keywords) for call in calls))

    def test_size_debt_constants_and_no_growth(self):
        baseline = build_base()
        baseline_tree = ast.parse(git('show', baseline + ':src/test_routing.py'))
        candidate_tree = ast.parse((REPO / 'src/test_routing.py').read_text())
        baseline_tests = {node.name for node in ast.walk(baseline_tree)
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.name.startswith('test_')}
        candidate_tests = {node.name for node in ast.walk(candidate_tree)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name.startswith('test_')}
        self.assertLessEqual(baseline_tests, candidate_tests)
        for name, size in source_sizes(REPO).items():
            base_size = {'fold.py': 2511, 'dispatch.py': 1890}[name]
            print(f'AC6 {name}: base={base_size}, candidate={size}; ceilings retained')
            self.assertLessEqual(size, base_size)


if __name__ == '__main__':
    unittest.main(verbosity=2)
