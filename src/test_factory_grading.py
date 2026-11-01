"""Hermetic regressions for grader capability routing."""
import os
import pathlib
import tempfile
from unittest.mock import patch

with tempfile.TemporaryDirectory() as tmp:
    os.environ['DOIT_ROOT'] = tmp
    import grading_env
    text = '''AC1 [backend]: the command reports all rows.
  review_path: log in as repository reviewer / go to Verification AC1 output
    / worked if every row appears / failed if any is missing.
AC2 [backend]: another command works.
  review_path: log in as repository reviewer / go to Verification AC2 output
'''
    with patch.object(grading_env, '_look_toml', return_value={}):
        assert grading_env.route(text, 'python3 check.py # AC1', 'fixture') == {}, 'repository review instructions routed to browser'
        assert grading_env.route('AC1 [backend]: check\nreview_path: log in as repository reviewer', '', 'fixture') == {}
        assert grading_env.route('AC1 [ui]: check\nreview_path: log in as repository reviewer at https://example.test', '', 'fixture')['AC1'] == ('browser', 'owed')
        real = 'AC1 [ui]: check dashboard\nreview_path: log in as administrator / go to /dashboard'
        assert grading_env.route(real, '', 'fixture') == {'AC1': ('browser', 'owed')}
        mixed = text + '  review_path: log in as repository reviewer / go to Verification AC2 output; then log in as admin at https://example.test\n'
        assert grading_env.route(mixed, '', 'fixture')['AC2'] == ('browser', 'owed')
print('PASS factory grader repository-review routing')
