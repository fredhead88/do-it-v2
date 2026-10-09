#!/usr/bin/env python3
"""Executable evidence and per-spec capability hold regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dispatch
import fold
import grading_env


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


if __name__ == '__main__':
    unittest.main()
