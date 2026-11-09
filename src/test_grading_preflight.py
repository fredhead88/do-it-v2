#!/usr/bin/env python3
"""Executable evidence and per-spec capability hold regressions."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dispatch
import fold
import grading_env
import grader_view
import routing


class PreflightTests(unittest.TestCase):
    SPEC = '''# Example
## Background
pytest and npm were used before; node_modules is irrelevant here.
## Acceptance criteria
AC1 [backend]: Historically pytest and vitest were unreliable.
review_path: run python3 check.py
  and bash check.sh
rollback_path: use pytest to undo this
AC2 [backend]: Check a fixture.
review_path: run node checks.js
## Constraints
pytest must not become a preflight dependency.
## Verification
```sh
python3 verify.py
```
'''

    def test_only_executable_evidence_counts(self):
        caps = grading_env.criterion_caps(self.SPEC, '', 'unknown')
        self.assertIn('tools', caps['AC1'])
        self.assertNotIn('node_modules', caps['AC1'])
        self.assertIn('node_modules', caps['AC2'])
        text = '\n'.join(grading_env._tool_texts(self.SPEC, '').values())
        self.assertNotIn('pytest', text)
        self.assertNotIn('vitest', text)
        self.assertNotIn('npm', text)
        self.assertIn('python3 verify.py', text)
        description_only = 'AC1 [backend]: pytest and npm support exists.\nreview_path: inspect result'
        self.assertEqual(grading_env.required('grader', description_only, '', 'unknown'), [])
        self.assertIn('node_modules', grading_env.required(
            'grader', '## Verification\n```\nnpm test\n```', '', 'unknown'))

    def test_preflight_proves_only_active_review_and_verify_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = Path(tmp)
            (view / 'tree').mkdir()
            commands = []
            def prove(view, project, repo, argv):
                commands.append(argv)
                return True, ''
            with patch.object(grading_env, '_spec_verify_text', return_value=(self.SPEC, 'python3 verify.py')), \
                    patch.object(grading_env, '_project_row', return_value={}), \
                    patch.object(grading_env, '_look_toml', return_value={}), \
                    patch.object(grading_env, '_sandboxed_run', side_effect=prove):
                failures = grading_env.preflight('grader', 'L-spec-A', view, 'unknown',
                                                repo=tmp, routed={'AC2'})
            self.assertEqual(failures, [])
            self.assertEqual([(Path(argv[0]).name, argv[1:]) for argv in commands],
                             [('bash', ['--version']), ('python3', ['--version'])])

    def test_genuine_tool_and_node_requirements_still_fail(self):
        spec = 'AC1 [backend]: Run checks.\nreview_path: pytest checks.py and npm test'
        with tempfile.TemporaryDirectory() as tmp:
            view = Path(tmp)
            (view / 'tree').mkdir()
            with patch.object(grading_env, '_spec_verify_text', return_value=(spec, '')), \
                    patch.object(grading_env, '_project_row', return_value={}), \
                    patch.object(grading_env, '_look_toml', return_value={}), \
                    patch.object(grading_env, '_sandboxed_run', return_value=(False, 'missing pytest')):
                failures = grading_env.preflight('grader', 'L-spec-A', view, 'unknown', repo=tmp)
            self.assertEqual([cap for cap, _ in failures], ['tools', 'node_modules'])

    def test_each_capability_hold_is_spec_local_for_callers(self):
        self.assertEqual(grading_env.SPEC_LOCAL, set(grading_env.CAPABILITIES))
        for cap in grading_env.CAPABILITIES:
            events = [{'type': 'capability-hold', 'subject': f'capability:{cap}:L-spec-A',
                       'capability': cap, 'spec': 'L-spec-A', 'ts': '2026-10-09T00:00:00Z'}]
            self.assertEqual(dispatch.held_capability(events, 'L-spec-A', [cap]),
                             f'capability:{cap}:L-spec-A')
            self.assertIsNone(dispatch.held_capability(events, 'L-spec-B', [cap]))
            self.assertEqual(fold.capability_holds(events)[f'capability:{cap}:L-spec-A']['specs'],
                             {'L-spec-A'})

    def test_git_routes_by_default_without_hiding_other_failures(self):
        git = 'AC1 [backend]: History.\nreview_path: run git log -1\n'
        for extra, expected in [('', ['git']), (
                'AC2 [backend]: Dependencies.\nreview_path: npm test', ['node_modules', 'git']), (
                'AC1 [backend]: History and dependencies.\nreview_path: git log -1 && npm test',
                ['node_modules', 'git'])]:
            spec = git + extra
            with tempfile.TemporaryDirectory() as tmp:
                view = Path(tmp)
                (view / 'tree').mkdir()
                with patch.object(grading_env, '_spec_verify_text', return_value=(spec, '')), \
                        patch.object(grading_env, '_project_row', return_value={}), \
                        patch.object(grading_env, '_look_toml', return_value={}):
                    self.assertIn('git', grading_env.required('grader', spec, '', 'unknown'))
                    failures = grading_env.preflight('grader', 'L-spec-A', view, 'unknown', repo=tmp)
                    self.assertEqual([cap for cap, _ in failures], expected)
                    self.assertTrue(dict(failures)['git'])


class BundledPreflightTests(unittest.TestCase):
    def test_bundle_skips_env_config_validation_but_legacy_still_checks_it(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(grading_env, '_spec_verify_text', return_value=('', '')), \
                patch.object(grading_env, '_project_row', return_value={'env_vars': ['API_TOKEN']}), \
                patch.object(grading_env, '_look_toml', return_value={}):
            failures = grading_env.preflight('grader', 'L-spec-1109', tmp, 'unknown', repo=tmp)
            self.assertEqual([cap for cap, _ in failures], ['env-file'])
            self.assertEqual(grading_env.preflight('grader', 'L-spec-1109', tmp, 'unknown',
                                                  repo=tmp, bundled=True), [])

    def test_bundle_skips_all_execution_proofs_including_admin_db(self):
        spec = "AC1 [backend]: fixture\nreview_path: run git diff HEAD && npm test && python3 -m pytest /opt/checkout/tests && cat .env\n"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(grading_env, '_spec_verify_text', return_value=(spec, 'python3 -m pytest')), \
                patch.object(grading_env, '_project_row', return_value={'admin_env': 'fixture'}), \
                patch.object(grading_env, '_prove_db', side_effect=AssertionError('DB proof attempted')), \
                patch.object(grading_env, '_prove_view_paths', side_effect=AssertionError('path proof attempted')), \
                patch.object(grading_env, '_prove_tools', return_value=(False, 'missing')):
            self.assertEqual(grading_env.preflight('grader', 'L-spec-1109', tmp, 'unknown',
                                                  repo=tmp, bundled=True), [])
        self.assertTrue(grading_env.required('grader', spec, '', 'unknown'))
        self.assertEqual(grading_env.required('grader', spec, '', 'unknown', bundled=True), [])


class PinnedHistoryTests(unittest.TestCase):
    def test_real_view_facade_checker_and_fail_closed_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, content = root / 'repo', root / 'content'
            content.mkdir()
            subprocess.run(['/usr/bin/git', 'init', '-q', '-b', 'master', str(repo)], check=True)
            def git(*args):
                return subprocess.check_output(['/usr/bin/git', '-C', str(repo), *args], text=True).strip()
            git('config', 'user.name', 'fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'fixture').write_text('ancestor\n')
            git('add', '.')
            git('commit', '-qm', 'ancestor')
            base = git('rev-parse', 'HEAD')
            git('checkout', '-qb', 'build')
            (repo / 'fixture').write_text('build\n')
            git('commit', '-qam', 'build')
            ready = git('rev-parse', 'HEAD')
            spec = 'AC1 [backend]: History.\nreview_path: git log -1 && git show HEAD:fixture\n'
            (content / 'L-spec-fixture.md').write_text(spec)
            (content / 'verify-L-spec-fixture-grader.sh').write_text(
                '#!/bin/bash\nset -eu\ncd "$(git rev-parse --show-toplevel)"\n'
                f'test "$(git rev-parse HEAD)" = "{ready}"\n'
                'test "$(git show HEAD:fixture)" = build\ngit log -1 --format=%H\n')
            with patch.dict(os.environ, HOME=str(root / 'home'), DOIT_ROOT=str(root),
                            DOIT_SCRATCH=str(root / 'scratch')), \
                    patch.object(grader_view, 'CONTENT', content), \
                    patch.object(grader_view, 'SEAT', root / 'seat'), \
                    patch.object(grading_env, 'CONTENT', content), \
                    patch.object(grading_env, '_project_row', return_value={}), \
                    patch.object(grading_env, '_look_toml', return_value={}):
                view = grader_view.build('L-spec-fixture', repo, base, ready, 'fixture')
                self.assertEqual(grading_env.route(spec, 'git show HEAD:fixture', 'fixture'), {})
                self.assertEqual(grading_env.preflight('grader', 'L-spec-fixture', view,
                                                     'fixture', repo=repo), [])
                checker = subprocess.run(grader_view.bwrap_argv(view, doit_src=grading_env.DOIT_SRC) +
                                         ['--', '/bin/bash', str(view / 'verify.sh')],
                                         capture_output=True, text=True)
                self.assertEqual(checker.returncode, 0, checker.stderr)
                self.assertEqual(checker.stdout.strip(), ready)
                with patch.object(grading_env, '_bwrap_available', return_value=False):
                    with self.assertRaises(grading_env.InfraFailure):
                        grading_env._prove_git(view, 'fixture')
                with patch.object(grading_env, '_run', return_value=subprocess.CompletedProcess(
                        [], 1, '', 'bwrap: No permissions to create a new namespace')):
                    with self.assertRaises(grading_env.InfraFailure):
                        grading_env._prove_git(view, 'fixture')
                pack = next((view / 'history').rglob('*.pack'))
                saved_pack = pack.with_suffix('.saved')
                pack.rename(saved_pack)
                pack.write_bytes(b'corrupt pack')
                self.assertIn('git', dict(grading_env.preflight(
                    'grader', 'L-spec-fixture', view, 'fixture', repo=repo)))
                pack.unlink()
                saved_pack.rename(pack)
                manifest_path = view / 'view.json'
                manifest = manifest_path.read_text()
                for damage in ('mismatch', 'corrupt', 'missing'):
                    if damage == 'mismatch':
                        manifest_path.write_text(json.dumps(dict(json.loads(manifest), ready_sha=base)))
                    elif damage == 'corrupt':
                        manifest_path.write_text(manifest)
                        (view / 'history/HEAD').write_text('not-a-commit\n')
                    else:
                        (view / 'history').rename(view / 'missing-history')
                    failures = grading_env.preflight('grader', 'L-spec-fixture', view,
                                                      'fixture', repo=repo)
                    self.assertIn('git', dict(failures), damage)
                    self.assertTrue(dict(failures)['git'])
                # Exclusions must not attempt history or re-execute bundled checks.
                with patch.object(grading_env, '_prove_git', side_effect=AssertionError('unexpected git proof')):
                    for options in ({'owed': {'AC1', 'verify'}}, {'routed': {'AC1', 'verify'}},
                                    {'bundled': True}):
                        self.assertEqual(grading_env.preflight('grader', 'L-spec-fixture', view,
                                                              'fixture', repo=repo, **options), [])
                # Infrastructure failures retain the existing pre-spend exception.
                with patch.object(grading_env, '_prove_git', return_value=(False, 'No space left on device')):
                    with self.assertRaises(grading_env.InfraFailure) as raised:
                        grading_env.preflight('grader', 'L-spec-fixture', view, 'fixture', repo=repo)
                    self.assertEqual(raised.exception.category, 'no-space')
            print('AC1 real facade/checker: pinned history passed; missing/corrupt/mismatch failed; exclusions preserved')

    def test_prior_git_route_is_revoked_once_without_revoking_explicit_owed(self):
        subject = 'L-spec-history'
        events = [dict(type='criterion-routed', actor='grader', subject=subject, criterion='AC1',
                       capability='git', to='owed', ts='2026-11-09T00:00:00Z'),
                  dict(type='owed-ac', subject=subject, criterion='AC2', actor='spec-writer')]
        spec = 'AC1 [backend]: History.\nreview_path: git log -1\n'
        self.assertEqual(fold.verdict_owed_criteria(events, subject), {'AC1', 'AC2'})
        with patch.object(grading_env, '_look_toml', return_value={}):
            computed = grading_env.route(spec, '', 'fixture')
        rows = routing.diff_routes(computed, routing.latest_routed_events(events, subject))
        self.assertEqual(rows, [('AC1', 'git', 'none')])
        for cid, cap, to in rows:
            events.append(dict(type='criterion-routed', actor='grader', subject=subject, criterion=cid,
                               capability=cap, to=to, ts='2026-11-09T00:01:00Z'))
        self.assertEqual(routing.owed_rows_for_routed(events, subject), [])
        self.assertEqual(fold.verdict_owed_criteria(events, subject), {'AC2'})
        self.assertEqual(routing.explicit_owed_ids(events, subject), {'AC2'})
        self.assertEqual(routing.diff_routes(computed, routing.latest_routed_events(events, subject)), [])
        print('AC1 route revocation: to=none once; derived owed removed; explicit owed preserved')


if __name__ == '__main__':
    unittest.main()
