#!/usr/bin/env python3
"""Hermetic coverage of all eight migrated omen groups and their boundaries."""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from contextlib import ExitStack
import fold
import look
import look_omens as o

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SID = 'L-spec-1001'

def ev(kind, age=0, subject=SID, **kw):
    return dict(type=kind, subject=subject, ts=(NOW-timedelta(hours=age)).isoformat(), **kw)

class Runner:
    def __init__(self): self.processes = ''; self.behind = '0'; self.calls = []
    def clock(self): return 0
    def git(self, args, cwd, timeout):
        self.calls.append(('git', args, cwd, timeout)); return self.behind
    def dispatch_processes(self, role, subject, timeout):
        self.calls.append(('processes', role, subject, timeout)); return self.processes

class Tests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch('autodispatch.candidates', return_value=[]),
                        patch.object(o.fold, 'open_questions', return_value=[]),
                        patch.object(o.fold, 'owed_due', return_value=[]),
                        patch('sweep_owed.due_candidates', return_value=[])]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches: self.addCleanup(p.stop)
        self.runner = Runner()
    def run_rules(self, events=(), specs=None, **cfg):
        return o.run(list(events), specs or {}, self.runner, NOW, cfg)
    def rules(self, *args, **kw): return {r['rule'] for r in self.run_rules(*args, **kw)}
    def test_import_order_does_not_cycle_through_build_runner(self):
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as name:
            src = str(pathlib.Path(__file__).resolve().parent)
            env = dict(os.environ, HOME=name, DOIT_ROOT=name, PYTHONPATH=src)
            result = subprocess.run([sys.executable, '-c', 'import build_runner; import look; import look_omens'],
                                    cwd=name, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_caps_vanished_endings_and_override(self):
        def start(minutes): return ev('spawn-started', minutes/60, spawn='L-grader-1001')
        self.assertFalse(self.rules([start(12)]))
        rows = self.run_rules([start(13)])
        self.assertEqual(rows[0]['rule'], 'near-cap'); self.assertEqual(rows[0]['severity'], 'WARN')
        self.assertEqual(self.run_rules([start(16)])[0]['severity'], 'CRIT')
        self.assertEqual(self.rules([start(46)]), {'vanished'})
        self.assertFalse(self.rules([start(48*60)]))
        self.assertFalse(self.rules([start(16), ev('spawn-done', spawn='L-grader-1001')]))
        self.runner.processes = 'dispatch.py grader L-spec-1001 --timeout 30'
        self.assertFalse(self.rules([start(16)]))
        self.assertEqual(self.rules([start(25)]), {'near-cap'})
        self.assertFalse(self.rules([start(13)], near_cap_fraction=0.95))
    def test_shelf_follows_chain_and_recent_head(self):
        self.mocks[0].return_value = [dict(spec=SID, status='blocked', reason='wave-blocked by L-spec-1002'),
                                    dict(spec='L-spec-1002', status='blocked', reason='wave-blocked by L-spec-1003')]
        rows = self.run_rules([ev('build-started', 2, subject='L-spec-1003')])
        self.assertEqual(rows[0]['rule'], 'shelf-stuck')
        self.assertEqual(rows[0]['key'], 'L-spec-1003')
        self.assertIn('holding 2 spec(s)', rows[0]['detail'])
        self.assertEqual(self.rules([ev('build-started', .5, subject='L-spec-1003')]), {'shelf'})
        self.mocks[0].return_value = [dict(spec=SID, status='dispatchable')]
        self.assertEqual(self.rules([ev('spec-written', 2)]), {'shelf-stuck'})
        self.mocks[0].side_effect = RuntimeError()
        self.assertIn('shelf', self.rules())
    def test_ravens_thresholds_known_exception(self):
        self.mocks[1].return_value = [dict(subject=SID, src='q:1', kind='question', asks='help')]
        for age, want in ((.5, None), (.6, 'WARN'), (2, 'WARN'), (2.1, 'CRIT')):
            rows = self.run_rules([ev('question', age, _src='q:1')])
            self.assertEqual(rows[0]['severity'] if rows else None, want)
        self.mocks[1].return_value[0]['subject'] = 'L-charter-0034'
        self.assertEqual(self.rules([ev('question', 3, _src='q:1')]), {'known-bug'})
    def test_contamination_and_regrade_loop(self):
        specs = {SID: dict(state='reviewing')}
        bad = ev('spawn-failed', 2, why='contamination in grader')
        self.assertEqual(self.rules([bad], specs), {'contamination'})
        fixed = ev('spec-written', 1, reasserted=True)
        self.assertEqual(self.rules([bad, fixed], specs), {'contamination-handled'})
        self.assertFalse(self.rules([bad, ev('verdict', 1)], specs))
        self.assertFalse(self.rules([ev('spawn-failed', 2, why='contamination identical packet')], specs))
        grades = [ev('verdict', age) for age in (3, 2, 1)]
        self.assertEqual(self.rules(grades, specs), {'regrade-loop'})
        self.assertFalse(self.rules(grades, {SID: dict(state='shipped-owed-due')}))
        self.assertFalse(self.rules(grades, specs, regrade_count=4))
    def test_owed_uses_fold_and_current_sweeper(self):
        specs = {SID: dict(state='shipped-owed-due')}
        due = [dict(spec=SID, criterion='AC1', due_at=NOW-timedelta(days=1))]
        self.mocks[2].return_value = due
        self.mocks[3].return_value = due
        self.assertEqual(self.rules(specs=specs), {'owed-due'})
        self.mocks[2].assert_called_with(specs)
        self.mocks[3].return_value = []
        self.assertEqual(self.rules(specs=specs), {'owed-parked'})
        self.mocks[3].side_effect = RuntimeError()
        self.assertEqual(self.rules(specs=specs), {'owed-due'})
        self.assertNotEqual(fold.NOW, NOW)  # the injected clock must not leak
    def test_member_verdict_rows_show_waiting_and_undetermined(self):
        tree = lambda c: c * 40
        green = ev('member-verdict', 3, spec=SID, window='w', tree=tree('a'), state='green', url='', nodes=[])
        rows = self.run_rules([green], {SID: dict(state='written')})
        self.assertEqual([(r['rule'], r['key'], r['severity']) for r in rows], [('member-verdict', 'waiting', 'INFO')])
        self.assertIn('verified alone, waiting for a window', rows[0]['detail'])
        self.assertIn(f'{SID} aaaaaaaa 3h ago', rows[0]['detail'])
        undetermined = ev('member-verdict', 1, spec=SID, window='w2', tree=tree('b'), state='undetermined', url='', nodes=[])
        rows = self.run_rules([green, undetermined], {SID: dict(state='written')})
        self.assertEqual([(r['key'], r['severity']) for r in rows], [('undetermined', 'WARN')])
        self.assertIn('UNDETERMINED', rows[0]['detail'])
        self.assertNotIn('waiting for a window', rows[0]['detail'])
        # Settled specs and specs with no member evidence add nothing.
        self.assertFalse(self.rules([green], {SID: dict(state='shipped')}))
        self.assertFalse(self.rules([], {SID: dict(state='written')}))
        self.assertEqual(o.OWNERS['member-verdict'], 'executor')
    def test_inbound_carried_and_boundary(self):
        source = 'https://example.test/pull/123'
        registration = ev('inbound-registered', 3, source=source)
        self.assertEqual(self.rules([registration]), {'inbound-uncarried'})
        self.assertFalse(self.rules([ev('inbound-registered', 2, source=source)]))
        for kind in ('spec-carried', 'inbound-covered', 'inbound-closed'):
            self.assertFalse(self.rules([registration, ev(kind, source=source)]))
    def test_not_live_deadline_no_fetch_or_disk(self):
        self.runner.behind = '2'
        self.assertEqual(self.rules(), {'doit-not-live'})
        self.assertFalse(any('fetch' in str(c) or 'disk' in str(c) for c in self.runner.calls))
        self.runner.calls.clear()
        rows = self.run_rules(_deadline=-1)
        self.assertEqual(rows[0]['rule'], 'doit-not-live')
        self.assertEqual(rows[0]['severity'], 'WARN')
        self.assertFalse(self.runner.calls)
    def test_config_partial_caps_and_fix(self):
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name); (root / 'content').mkdir()
            path = root / 'look.toml'; path.write_text('[omens]\nregrade_count = 4\n[omens.caps]\ngrader = 20\n')
            cfg = look.load_toml(path)['omens']
            self.assertEqual(cfg['regrade_count'], 4)
            self.assertEqual(cfg['vanished_max_h'], 48)
            (root / 'content/L-charter-9000.md').write_text('corrects: owed, other\n')
            with patch('relay.open_charters', return_value=['L-charter-9000']):
                self.assertEqual(o.fix_for('owed-due', root, [], {}), 'L-charter-9000')
                self.assertIsNone(o.fix_for('raven', root, [], {}))
                (root / 'content/L-charter-9000.md').write_text('corrects: dispatch-cap\n')
                self.assertEqual(o.fix_for('near-cap', root, [], {'omen-near-cap': 'dispatch-cap'}), 'L-charter-9000')
    def test_look_pass_calls_omens_once_with_shared_fold_and_budget(self):
        with tempfile.TemporaryDirectory() as name, ExitStack() as stack:
            root = pathlib.Path(name)
            specs = {SID: dict(state='reviewing')}
            rows = [dict(rule='regrade-loop', key=SID, detail='graded 3x', severity='WARN')]
            run = stack.enter_context(patch.object(o, 'run', return_value=rows))
            folded = stack.enter_context(patch.object(fold, 'fold', return_value=(specs, {}, [], {})))
            for key in dir(look):
                if key.startswith('_check_') and key != '_check_omens':
                    stack.enter_context(patch.object(look, key, return_value=[]))
            stack.enter_context(patch('merge_deadman.run'))
            stack.enter_context(patch('look_scratch.check'))
            stack.enter_context(patch('merge_queue_watchdog.check'))
            stack.enter_context(patch.object(self.runner, 'crontab_text', return_value='', create=True))
            result = look.run([], runner=self.runner, root=root, now=NOW, dry_run=True)
            run.assert_called_once()
            self.assertIs(run.call_args.args[1], specs)
            self.assertEqual(run.call_args.args[3], NOW)
            self.assertEqual(run.call_args.args[4]['_deadline'], 90)
            folded.assert_called_once()
            self.assertEqual(result['briefs'][0]['subject'], f'look:omen-regrade-loop:{SID}')
            self.assertFalse((root / 'events').exists())

    def test_unavailable_shelf_does_not_clear_standing_stuck_reading(self):
        state = {'open': {f'omen-shelf-stuck:{SID}|{SID}': 'old:1'}}
        rows = [dict(rule='shelf', key='unavailable', detail='dispatcher error', severity='WARN')]
        with patch.object(o, 'run', return_value=rows), patch.object(look, '_read_state', return_value=state), \
             patch.object(look, '_write_brief', return_value=None), patch.object(look, '_clear') as clear:
            look._check_omens([], {}, self.runner, NOW, pathlib.Path('/unused'), dict(omens={}), 90, [], [])
        clear.assert_not_called()

    def test_digest_per_key_clear_recovery_and_dry_run(self):
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name)
            rows = [dict(rule='near-cap', key=f'L-grader-{i}', detail='near cap', severity='WARN') for i in (1, 2)]
            events = []
            with patch.object(o, 'run', return_value=rows), patch.object(fold, 'NOW', NOW):
                def check(dry=False):
                    briefs, answered = [], []
                    look._DIGEST = look.look_digest.Digest(root, events, NOW, dry_run=dry)
                    look._DRY_RUN = dry
                    try:
                        look._check_omens(events, {}, self.runner, NOW, root, dict(omens={}), 90, briefs, answered)
                        look._finish_digest(look._DIGEST)
                    finally:
                        look._DIGEST = None; look._DRY_RUN = False
                    return briefs, answered
                briefs, _ = check(True)
                self.assertEqual(len(briefs), 2)
                self.assertFalse((root / 'events').exists())
                briefs, _ = check()
                self.assertEqual({b['subject'] for b in briefs}, {'look:omen-near-cap:L-grader-1', 'look:omen-near-cap:L-grader-2'})
                self.assertTrue(all(b['owner'] == 'executor' for b in briefs))
                events = [dict(json.loads(line), _src=f'{look.LEDGER_NAME}:{i}') for i, line in enumerate(
                    (root / 'events' / look.LEDGER_NAME).read_text().splitlines(), 1)]
                self.assertFalse(check()[0])
                # A single disappearing key clears only its own brief, after state loss too.
                (root / 'look/state.json').unlink()
                rows.pop()
                _, answers = check()
                self.assertEqual(len(answers), 1)
                self.assertEqual(answers[0]['ref'], events[1]['_src'])
                with patch.object(o, 'run', side_effect=RuntimeError):
                    self.assertFalse(check()[1])

if __name__ == '__main__': unittest.main()
