"""Trusted rulings reach existing gate/Executor paths with no hand-edited files."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_IMPORT = tempfile.TemporaryDirectory(prefix='ruling-import-')
os.environ['DOIT_ROOT'] = _IMPORT.name
os.environ['DOIT_NO_POKE'] = '1'
import autodispatch
import fold
import merge_gate
import nextstep
import quarantine as q
import rulings
import thinker_answer
import window_gate as gate

PROJECT = 'albert-scott'
SPEC = 'L-spec-1234'
NODE = 'tests/test_clock.py::test_boundary'


class RulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ruling-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        for obj, name, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root/'events')):
            change = patch.object(obj, name, value)
            change.start(); self.addCleanup(change.stop)
        change = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1', THINKER_LEDGER='L-thinker-local.jsonl')
        change.start(); self.addCleanup(change.stop)
        self.question = q._append('question', PROJECT, self.now, _subject=SPEC, asks='Decide',
                                  default='hold', deadline=(self.now+timedelta(days=1)).isoformat())

    def answer(self, value):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return thinker_answer.main([self.question['_src'], value])

    def test_plain_prose_and_all_canonical_kinds_emit_structured_fields(self):
        self.assertEqual(self.answer("it's okay; keep the recorded scope"), 0)
        self.assertNotIn('ruling', fold.read_events()[-1])
        for ordinary in ('{"choice":"keep the existing route"}', '{draft answer'):
            self.assertEqual(self.answer(ordinary), 0)
            self.assertNotIn('ruling', fold.read_events()[-1])
        for answer, kind in [('base-red-accepted '+NODE, 'base-red-accepted'),
                             ('drop-from-window '+SPEC+' obsolete --window win-1', 'drop-from-window'),
                             ('regrade '+SPEC, 'regrade'), ('rebuild '+SPEC, 'rebuild'),
                             ('grant-extend '+SPEC+' src/exact.py', 'grant-extend')]:
            self.assertEqual(self.answer(answer), 0, answer)
            rows = [e for e in fold.read_events() if e['type'] == 'decision']
            self.assertEqual(rows[-1]['ruling']['kind'], kind)
            self.assertEqual(rows[-1]['ref'], self.question['_src'])
        self.assertIn(self.question['_src'], fold.answered(fold.read_events()))

    def test_base_red_ruling_feeds_policy_and_next_pg_gate_releases(self):
        self.assertEqual(self.answer('base-red-accepted '+NODE), 0)
        q.apply(PROJECT, now=datetime.now(timezone.utc))
        current = q.active(fold.read_events(), PROJECT, datetime.now(timezone.utc))
        self.assertIn(NODE, current)
        result = dict(state='red', pg_failures={NODE: 'FAILED clock'}, other_nodes=[], nodes=[NODE], doc={})
        with patch.object(gate, 'verify', return_value=result):
            baseline, _ = gate.base_pg(self.root, 'a'*40, [gate.PG_CHECK], 'win-1', fold.read_events())
        self.assertEqual(baseline['state'], 'green')
        until = q._time(current[NODE]['until'])
        q.apply(PROJECT, now=until)
        q.apply(PROJECT, now=until+timedelta(seconds=1))
        self.assertNotIn(NODE, q.active(fold.read_events(), PROJECT, until+timedelta(seconds=1)))
        self.assertEqual(len([e for e in fold.read_events() if e['type'] == 'test-quarantined']), 1)

    def test_structured_regrade_and_rebuild_reach_real_executor_selectors(self):
        self.assertEqual(self.answer('regrade '+SPEC), 0)
        event, kind = autodispatch._grader_trigger(fold.read_events())
        self.assertEqual(kind, 'regrade')
        self.assertEqual(event['subject'], SPEC)
        self.assertEqual(self.answer('rebuild '+SPEC), 0)
        context = dict(evs=fold.read_events(), spec={'state': 'built'}, dependency=None)
        with patch.object(nextstep, '_terminal', return_value=False), \
             patch.object(nextstep, '_retry_result', return_value={'reason': 'ruling'}):
            result = nextstep._rebuild(context)
        self.assertEqual(result['trigger'], [e for e in fold.read_events() if e['type'] == 'decision'][-1]['_src'])

    def test_drop_is_consumed_once_before_window_construction(self):
        self.assertEqual(self.answer('drop-from-window '+SPEC+' obsolete --window win-1'), 0)
        (self.root/'repos'/PROJECT).mkdir(parents=True)
        with patch.object(gate, 'resolve_entries', return_value=[(SPEC, 'branch', 'a'*40)]), \
             patch.object(gate, 'required_checks', side_effect=AssertionError('dropped branch must never run')):
            code = gate._run_attempt('win-1', PROJECT, [SPEC], 'master', False,
                                     'b'*40, 'b'*40, None, 0, None)
        self.assertEqual(code, 1)
        self.assertEqual(rulings.drops(fold.read_events(), PROJECT, 'win-1'), [])
        drops = [e for e in fold.read_events() if e['type'] == 'window-dropped']
        self.assertEqual([(e['subject'], e['reason']) for e in drops], [(SPEC, 'thinker-ruling')])

    def test_grants_use_existing_path_validation_and_untrusted_rulings_are_ignored(self):
        self.assertEqual(self.answer('grant-extend '+SPEC+' src/exact.py'), 0)
        with patch.object(merge_gate, 'writes_grant', return_value=['src/original.py']):
            self.assertEqual(merge_gate.grant_for(SPEC, []), ['src/exact.py', 'src/original.py'])
        self.assertEqual(self.answer('grant-extend '+SPEC+' **/*'), 2)
        forged = dict(type='decision', actor='builder', subject=SPEC, project=PROJECT,
                      ruling={'kind': 'base-red-accepted', 'nodes': [NODE]})
        self.assertIsNone(rulings.ruling(forged))
        for bad in ({'kind': 'base-red-accepted', 'nodes': []}, {'kind': []},
                    {'kind': 'rebuild', 'spec': 'wrong'}, {'kind': 'regrade', 'spec': SPEC, 'extra': True}):
            self.assertEqual(self.answer(json.dumps(bad)), 2)


if __name__ == '__main__':
    unittest.main()
