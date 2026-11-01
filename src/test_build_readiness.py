"""First builds spend no seat when their exact base is not ready."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import build_readiness as readiness
import dispatch
import fold
import lane_driver


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='readiness-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repos/demo'
        self.repo.mkdir(parents=True)
        (self.root / 'content').mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source.py').write_text('def existing():\n    return 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.sid = 'L-spec-9000'
        self.events = [dict(type='spec-written', subject=self.sid, project='demo', ts='2026-10-18T00:00:00Z')]
        self.spec()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def spec(self, consumes='', command='python3 -c "print(1)"', writes='source.py'):
        text = ('## Interfaces\nConsumes:\n' + consumes + '\nProduces:\n- new behavior\n'
                'Writes: ' + writes + '\n## Acceptance\nAC1 [backend]: fixture.\n'
                '## Verification\n```bash\n' + command + '\n```\n')
        (self.root / 'content' / (self.sid + '.md')).write_text(text)
        return text

    def check(self, dry_run=True, project_row=None):
        with patch.object(readiness.grading_env, '_project_row', return_value=project_row or {}):
            return readiness.check(self.sid, 'demo', self.base, self.events, root=self.root, dry_run=dry_run)

    def test_exact_base_and_successful_dry_run(self):
        self.spec('- `def existing()` from `source.py`')
        self.assertEqual(self.check()['status'], 'ready')
        (self.repo / 'source.py').write_text('def future(): pass\n')
        self.spec('- `def future()` from `source.py`')
        self.assertEqual(self.check()['status'], 'bounced')
        self.spec('Consumes: source.future()')
        self.assertEqual(self.check()['status'], 'bounced')

    def test_missing_symbol_records_single_valid_producer_hold(self):
        self.spec('- `def future()` from `source.py`')
        producer = self.root / 'content/L-spec-9001.md'
        producer.write_text('Produces:\n- `def future()`\nWrites: source.py\n')
        self.events.append(dict(type='spec-written', subject='L-spec-9001', project='demo', path=str(producer)))
        result = self.check()
        self.assertEqual((result['status'], result['waits_for']), ('held', 'L-spec-9001'))
        with patch.object(fold, 'ROOT', self.root):
            readiness.record(result, 'demo', root=self.root)
        rows = [json.loads(line) for line in (self.root / 'events/L-tick-local.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['type'], 'dependency-hold')
        self.assertTrue(fold.valid_dependency(rows[-1]['waits_for']))
        self.assertNotIn('actor', rows[-1])
        self.events.append(dict(type='spec-killed', subject='L-spec-9001'))
        self.assertEqual(self.check()['status'], 'bounced')

    def test_external_packages_and_runtime_paths_are_not_git_inputs(self):
        (self.repo / 'package.json').write_text('{"dependencies":{"next":"1.0"}}')
        self.git('add', '.')
        self.git('commit', '-qm', 'package declaration')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec('- Link from `next/link`\n- runtime `$R/base-red/<project>/<sha>.dispatched`')
        self.assertEqual(self.check()['status'], 'ready')

    def test_missing_path_without_producer_bounces(self):
        self.spec('- `missing/path.py`')
        self.assertIn('missing inputs', self.check()['findings'][0])

    def test_unquoted_slash_prose_is_not_a_path_but_explicit_inputs_still_are(self):
        target = self.repo / 'api/existing.py'
        target.parent.mkdir()
        target.write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'existing top-level directory')
        self.base = self.git('rev-parse', 'HEAD')
        prose = ('pass/waived missing/unreadable creates/chmods Scorecard/run pending/stale '
                 'P/neighbour method/type numbers/kinds/verdicts finalize/contract '
                 'routes/synonyms exact-relation/book-total metric/relation')
        self.spec('- ' + prose)
        self.assertEqual(self.check(dry_run=False)['status'], 'ready')
        self.spec('- api/existing.py')
        self.assertEqual(self.check(dry_run=False)['status'], 'ready')
        for token in ('api/missing_module', './api/missing_module', 'absent/module.py',
                      '`missing/unreadable`', '`unknown/path`'):
            with self.subTest(token=token):
                self.spec('- ' + token)
                result = self.check(dry_run=False)
                self.assertEqual(result['status'], 'bounced')
                self.assertIn('missing inputs', result['findings'][0])

    def test_coupled_tests_and_companion_grants_block(self):
        (self.repo / 'test_source.py').write_text('import source\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'coupled test')
        self.base = self.git('rev-parse', 'HEAD')
        result = self.check()
        self.assertIn('test_source.py', result['findings'][0])
        self.spec(writes='source.py test_source.py')
        self.assertEqual(self.check()['status'], 'ready')

    def test_known_generated_companion_blocks_even_without_import(self):
        (self.repo / 'crons.toml').write_text('# cron fixture')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/test_crons.py').write_text('# generated manifest assertion fixture')
        self.git('add', '.')
        self.git('commit', '-qm', 'cron companion')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec(writes='crons.toml')
        self.assertIn('src/test_crons.py', self.check()['findings'][0])
        self.spec(writes='crons.toml src/test_crons.py')
        self.assertEqual(self.check()['status'], 'ready')

    def test_text_references_are_run_only_and_reach_the_builder_packet(self):
        import coupled_tests
        import packet
        sources = ['pkg/service.py', 'scripts/task.sh', 'api/migrations/new_revision.py']
        named = 'pkg/test_service.py'
        fingerprint = 'api/genesis_schema_fingerprint.txt'
        run_tests = [f'tests/test_importer_{i:02d}.py' for i in range(35)]
        files = {path: '# fixture\n' for path in sources + [fingerprint]}
        files[named] = 'from pkg import service\n'
        files.update({path: 'from pkg import service\n# scripts/task.sh\n' for path in run_tests})
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'run-only references and blocking companions')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec(writes=' '.join(sources))
        result = self.check(dry_run=False)
        self.assertEqual(result['findings'], [
            'Writes omits coupled tests/companions: ' + ', '.join(sorted([named, fingerprint]))])
        self.assertEqual(result['run_tests'], run_tests)
        self.spec(writes=' '.join(sources + [named, fingerprint]))
        result = self.check(dry_run=False)
        self.assertEqual((result['status'], result['run_tests']), ('ready', run_tests))
        with patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', self.root / 'events'), \
                patch.object(fold, 'PROJECT', None):
            readiness.record(result, 'demo', root=self.root)
            normalized = next(e for e in fold.read_events(use_cache=False) if e['type'] == 'build-readiness')
        stored = json.loads((self.root / 'events/L-tick-local.jsonl').read_text().splitlines()[-1])
        self.assertEqual(stored['run_tests'], run_tests)
        events = self.events + [normalized]
        context = SimpleNamespace(
            a=SimpleNamespace(subject=self.sid, role='builder', base_sha=self.base,
                              repo=str(self.repo), produces=None, conventions=None, hint=None),
            evs=events, spec_file=lambda: self.root / 'content' / (self.sid + '.md'),
            all_of=lambda kind: [e for e in events if e['type'] == kind],
            last=lambda kind: next((e for e in reversed(events) if e['type'] == kind), None),
            charter_file=lambda: None, charter_id=lambda: None, standing=lambda: ([], []),
            footprint=lambda: sources, project=lambda: 'demo', worktree=lambda: str(self.repo))
        with patch.object(packet, 'verify_base_sha', return_value=self.base), \
                patch.object(packet, 'write_verify_script', return_value=None), \
                patch.object(packet, 'conflicts', return_value=[]), \
                patch.object(packet, 'preflight_lines', return_value=[]):
            lines = packet.p_builder(context)
        self.assertIn('no edit grants', '\n'.join(lines))
        for path in run_tests:
            self.assertTrue(any(line.startswith('   ' + path + ':') for line in lines), path)
        import shlex
        command = shlex.split(next(line for line in lines if line.startswith('   Run: '))[8:])
        self.assertEqual(json.loads(command[command.index('--tests') + 1]), run_tests)
        with patch.object(coupled_tests, 'scan', side_effect=AssertionError('explicit list rescanned')), \
                patch.object(coupled_tests, 'run', return_value=[]) as run:
            self.assertEqual(coupled_tests.main(command[2:]), 0)
        self.assertEqual(run.call_args.args[0], run_tests)
        self.assertIsNone(coupled_tests.readiness_tests(events, '0' * 40))
        events.append(dict(type='spec-written', subject=self.sid))
        self.assertIsNone(coupled_tests.readiness_tests(events, self.base))

    def test_shared_registries_require_named_tests_and_migration_fingerprint(self):
        registries = ['api/alembic_supabase/branch_isolation.json',
                      'scratch_listing_authority_cleanup.py']
        named = ['api/alembic_supabase/test_branch_isolation_contract.py',
                 'tests/test_scratch_listing_authority_cleanup.py']
        fingerprint = 'api/alembic_supabase/genesis_schema_fingerprint.txt'
        files = {path: '# registry fixture\n' for path in registries + named + [fingerprint]}
        files['tests/test_registry_reader.py'] = '\n'.join(registries) + '\nbranch_isolation\n'
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'shared registry readers')
        self.base = self.git('rev-parse', 'HEAD')
        writes = registries + ['api/alembic_supabase/migrations/new_revision.py']
        self.spec(writes=' '.join(writes))
        config = Path(readiness.__file__).resolve().parent.parent / 'grading.toml'
        with patch.dict(os.environ, DOIT_GRADING_TOML=str(config)):
            row = readiness.grading_env._project_row('albert-scott')
        expected = sorted(named + [fingerprint])
        self.assertEqual(self.check(dry_run=False, project_row=row)['findings'],
                         ['Writes omits coupled tests/companions: ' + ', '.join(expected)])
        self.spec(writes=' '.join(writes + expected))
        self.assertEqual(self.check(dry_run=False, project_row=row)['status'], 'ready')
        # Without this project policy the readers become run-only references.
        ordinary = self.check(dry_run=False)
        self.assertEqual(ordinary['status'], 'ready')
        self.assertEqual(ordinary['run_tests'], ['tests/test_registry_reader.py'])

    def test_non_python_sources_couple_full_paths_and_names_without_bare_stems(self):
        sources = ['dashboard/src/app/api/auth/login/route.ts', 'scripts/deploy.sh', 'api/service.py']
        expected = ['tests/test_explicit_reference.py', 'scripts/tests/test_deploy_mode.py',
                    'dashboard/src/app/api/auth/login/route.test.ts', 'tests/test_python_import.py']
        files = {path: '# source fixture\n' for path in sources}
        files.update({path: '# named fixture\n' for path in expected})
        files['tests/test_explicit_reference.py'] = '\n'.join(sources[:2])
        files['tests/test_python_import.py'] = 'import api.service\n'
        files['tests/test_generic_words.py'] = '# route and deploy are common words\n'
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'path and stem references')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec(writes=' '.join(sources))
        blocking = ['scripts/tests/test_deploy_mode.py', 'dashboard/src/app/api/auth/login/route.test.ts']
        run_tests = ['tests/test_explicit_reference.py', 'tests/test_python_import.py']
        result = self.check(dry_run=False)
        self.assertEqual(result['findings'],
                         ['Writes omits coupled tests/companions: ' + ', '.join(sorted(blocking))])
        self.assertEqual(result['run_tests'], run_tests)
        self.spec(writes=' '.join(sources + blocking))
        result = self.check(dry_run=False)
        self.assertEqual((result['status'], result['run_tests']), ('ready', run_tests))

    def test_broken_chain_and_missing_environment_bounce(self):
        for command, reason in [('false\ntrue', 'exit 1'),
                                ('python3 -c "raise SystemExit(5)"', 'exit 5'),
                                ('no_such_fixture_command', 'exit 127'),
                                ('.venv/bin/python -c "print(1)"', 'environment missing')]:
            self.spec(command=command)
            result = self.check()
            self.assertEqual(result['status'], 'bounced', result)
            self.assertIn(reason, result['findings'][0])
            readiness.record(result, 'demo', root=self.root)
        self.assertIn('spec-shape-failed', (self.root / 'events/L-tick-local.jsonl').read_text())

    def test_companions_exclude_global_names_fixture_data_and_python_path_prose(self):
        files = {
            'pkg/route.ts': '// source\n',
            'pkg/__tests__/route.test.ts': '// local named test\n',
            'unrelated/route.test.ts': '// unrelated basename\n',
            'pkg/worker.py': 'value = 1\n',
            'tests/test_import.py': 'from\tpkg\timport\tworker\n',
            'tests/test_path_prose.py': '# historical pkg/worker.py path\n',
            'tests/fixtures/test_reader.py': 'import pkg.worker\n',
            'tests/manifest.json': 'pkg.worker\npkg/route.ts\n',
            'tests/manifest.txt': 'pkg.worker\npkg/route.ts\n',
        }
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'round two false-positive fixtures')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec(writes='pkg/route.ts pkg/worker.py')
        result = self.check(dry_run=False)
        self.assertEqual(result['findings'], [
            'Writes omits coupled tests/companions: pkg/__tests__/route.test.ts'])
        self.assertEqual(result['run_tests'], ['tests/test_import.py'])
        tree = self.git('ls-tree', '-r', '--name-only', self.base).splitlines()
        rows = readiness.companion_matches('', ['pkg/route.ts', 'pkg/worker.py'],
                                            self.repo, self.base, tree)
        self.assertEqual([(r['test'], r['source'], r['kind']) for r in rows], [
            ('pkg/__tests__/route.test.ts', 'pkg/route.ts', 'named'),
            ('tests/test_import.py', 'pkg/worker.py', 'reference')])
        self.assertEqual(rows[1]['line'], 1)
        self.assertIn('import', rows[1]['pattern'])

    def test_package_import_root_is_preserved_without_bare_path_matching(self):
        files = {'api/app/__init__.py': '', 'api/app/worker.py': 'value = 1\n',
                 'tests/test_import.py': 'from app import worker\n',
                 'tests/test_prose.py': '# api/app/worker.py and worker alone\n'}
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'package-root import fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.spec(writes='api/app/worker.py')
        result = self.check(dry_run=False)
        self.assertEqual((result['status'], result['run_tests']), ('ready', ['tests/test_import.py']))

    def test_rebuilds_bypass_first_build_gate(self):
        self.events.append(dict(type='build-done', subject=self.sid))
        with patch.object(readiness, 'verify_base', side_effect=AssertionError('rebuild checked')):
            self.assertEqual(self.check()['status'], 'already-built')

    def test_needs_context_requires_reason_and_exposes_ledger_fields(self):
        with self.assertRaisesRegex(ValueError, 'requires a nonempty reason'):
            readiness.build_done_fields({'status': 'NEEDS_CONTEXT'})
        self.assertEqual(readiness.build_done_fields({'status': 'NEEDS_CONTEXT', 'reason': 'missing interface'}),
                         {'reason': 'missing interface'})
        import jsonschema
        schema = json.loads((Path(__file__).resolve().parent.parent / 'agents/builder.schema.json').read_text())
        obj = {'status': 'NEEDS_CONTEXT'}
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(obj))
        self.assertTrue(any('reason' in error.message for error in errors))

    def test_lane_driver_refuses_dispatch_after_readiness_failure(self):
        wt = self.root / 'worktrees/demo' / self.sid.lower()
        wt.mkdir(parents=True)
        calls = []
        def runner(argv):
            calls.append(argv)
            if argv[:2] == ['doit', 'readiness']:
                return dict(code=2, stdout='', stderr='readiness fixture blocked')
            return dict(code=0, stdout='/fixture.packet', stderr='')
        with patch.object(fold, 'ROOT', self.root), patch.object(dispatch, 'CONTENT', self.root / 'content'), \
                patch('packet._effective_base', return_value=self.base):
            with self.assertRaisesRegex(ValueError, 'readiness fixture blocked'):
                lane_driver._dispatch('builder', self.sid, 'demo', None, runner)
        self.assertFalse(any(c[:2] == ['doit', 'dispatch'] for c in calls))

    def test_dispatch_persists_needs_context_reason(self):
        output = dict(status='NEEDS_CONTEXT', reason='missing base interface',
                      identity=dict(branch=self.sid.lower(), base_sha=self.base, ready_sha=self.base),
                      verify=dict(exit_code=0), tests=dict(added=0), deviations=[], stubs=[])
        args = SimpleNamespace(subject=self.sid, cwd=str(self.repo), pinned_base_sha=self.base)
        base = dict(subject=self.sid, spawn='L-builder-9000', project='demo')
        events = self.root / 'events'
        events.mkdir()
        with patch.object(dispatch, 'write_card', return_value=self.root / 'card.md'), \
                patch.object(dispatch, 'commit_shape_events', return_value=[]), \
                patch.object(fold, 'ROOT', self.root), patch.object(fold, 'EVENTS', events):
            rows = dispatch.events_for('builder', output, args, base)
            for kind, fields in rows:
                self.assertFalse(dispatch.emit(events / 'L-builder-9000.jsonl', base, kind, strict=True, **fields))
            stored = next(e for e in fold.read_events(use_cache=False) if e['type'] == 'build-done')
            self.assertEqual(stored['reason'], 'missing base interface')
            for reason in ('', None):
                with self.assertRaisesRegex(ValueError, 'requires a nonempty reason'):
                    dispatch.events_for('builder', dict(output, reason=reason), args, base)


if __name__ == '__main__':
    unittest.main()
