"""Hermetic project inference regressions."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import fold

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / 'repos/demo').mkdir(parents=True)
    events = [dict(subject='L-spec-1', type='dependency-hold', project='.do-it'),
              dict(subject='L-spec-1', type='spec-written', project='demo')]
    with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'), \
            patch.object(fold, 'PROJECT', None), patch.object(fold, 'read_events', return_value=events):
        assert fold.subject_project('L-spec-1') == 'demo', 'pre-spec event poisoned project'
        with patch.object(Path, 'cwd', return_value=root / '.do-it'):
            try:
                fold.append(['dependency-hold', 'L-spec-2', 'waits_for=L-spec-3', 'why=fixture'])
            except SystemExit as exc:
                assert 'unregistered project' in str(exc), exc
            else:
                raise AssertionError('unregistered inferred project admitted')
        assert not (root / 'events').exists()
        event = fold.append(['observed', 'L-spec-1'])
        assert event['project'] == 'demo'
print('PASS factory spec-authoritative project and unregistered inference refusal')

# The runner must use the same authority rule, or an early hold still poisons evidence.
import json
import test_build_runner as fixtures
fixture = fixtures.BuildRunnerTests(methodName='runTest')
fixture.setUp()
fixture.env.pop('DOIT_PROJECT', None)
try:
    rows = [dict(v=1, type='dependency-hold', subject=fixture.sid, project='.do-it',
                 ts='2026-10-17T00:00:00Z', waits_for='L-spec-8999', why='fixture'),
            dict(v=1, type='spec-written', subject=fixture.sid, project='demo',
                 ts='2026-10-17T01:00:00Z', path=str(fixture.root / 'content' / (fixture.sid + '.md')))]
    (fixture.root / 'events/L-planner-fixture.jsonl').write_text(json.dumps(rows[0]) + '\n')
    (fixture.root / 'events/L-spec-writer-fixture.jsonl').write_text(json.dumps(rows[1]) + '\n')
    bundle, _ = fixture.bundle()
    assert bundle['project'] == 'demo'
finally:
    fixture.doCleanups()
print('PASS factory runner uses registered spec project over provisional hold')
