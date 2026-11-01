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

    def check(self, dry_run=True):
        with patch.object(readiness.grading_env, '_project_row', return_value={}):
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
