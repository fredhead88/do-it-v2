#!/usr/bin/env python3
"""W28: fixture ledgers distinguish an aged wait from a real missing action."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix='doit-stalls-')
os.environ['DOIT_ROOT'] = TMP.name
os.environ['DOIT_SESSIONS_DIR'] = str(Path(TMP.name) / 'sessions')
os.environ['V4_LEDGER_DIR'] = str(Path(TMP.name) / 'v4-absent')
os.environ['V4_INBOX_DIR'] = str(Path(TMP.name) / 'inbox-absent')
import dispatch
import fold
from fold_stalls import BUCKETS

NOW = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)
SID = 'L-spec-9000'


class Stalls(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TMP.name))
        self.events = self.root / 'events'
        self.events.mkdir()
        self.content = self.root / 'content'
        self.content.mkdir()
        for obj, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.events),
                                (fold, 'BOARD', self.root / 'board.md'), (fold, 'NOW', NOW),
                                (fold, 'SESSIONS', self.root / 'sessions'),
                                (dispatch, 'CONTENT', self.content)):
            fix = patch.object(obj, key, value)
            fix.start()
            self.addCleanup(fix.stop)
        self.files = {}

    def add(self, kind, sid=SID, minutes=-3000, role='planner', **fields):
        # Actors live in filenames, never in event bodies.
        row = dict(type=kind, subject=sid, ts=(NOW + timedelta(minutes=minutes)).isoformat(), **fields)
        self.files.setdefault(role, []).append(row)
        return row

    def written(self, sid=SID, **fields):
        self.add('spec-written', sid, **fields)

    def clean(self, sid=SID):
        self.written(sid)
        self.add('build-done', sid, -2900, 'builder', status='DONE')
        self.add('verdict', sid, -2800, 'grader', confirmed=True)
        self.add('review', sid, -2700, 'reviewer', n_blocking=0)

    def read(self):
        for role, rows in self.files.items():
            (self.events / f'L-{role}-fixture.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in rows))
        events = fold.read_events()
        specs, charters, ignored, by = fold.fold(events)
        rows = fold.stall_rows(events, specs, now=NOW, dwell=fold.DWELL_DAYS, by_subject=by)
        return events, specs, charters, ignored, by, {r['spec']: r for r in rows}

    def one(self):
        return self.read()[-1][SID]

    def test_clean_review_and_gate_are_queued_without_a_queue_add(self):
        self.clean()
        self.assertEqual(self.one()['bucket'], 'queued')
        self.add('merge-gate-clean', minutes=-2600, role='executor')
        row = self.one()
        self.assertEqual(row['bucket'], 'queued')
        self.assertIn('merge-gate-clean', row['reason'])
        self.assertEqual(row['owner'], 'executor')

    def test_real_window_is_named(self):
        self.clean()
        self.add('window-opened', 'win-fixture', -2600, 'executor', specs=[SID])
        self.assertIn('win-fixture', self.one()['reason'])

    def test_window_index_preserves_latest_window_state(self):
        self.clean()
        self.add('window-opened', 'win-active', -2600, 'executor', specs=[SID])
        self.add('window-opened', 'win-dropped', -2600, 'executor', specs=[SID])
        self.add('window-dropped', 'win-dropped', -2500, 'executor', specs=[SID])
        self.assertEqual(self.one()['reason'], 'in merge window win-active')
        self.add('window-verified', 'win-active', -2400, 'executor', specs=[SID])
        self.assertEqual(self.one()['reason'], 'awaiting merge window after review')

    def test_new_conflict_invalidates_old_clean_review(self):
        self.clean()
        self.add('conflict-rework', minutes=-2600, role='executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.assertIn('conflict-rework', self.one()['reason'])
        self.add('review', minutes=-2500, role='reviewer', n_blocking=0)
        self.assertEqual(self.one()['bucket'], 'queued')

    def test_failed_bundle_must_be_rerun(self):
        self.clean()
        self.add('evidence-bundle-failed', minutes=-2600, role='build-runner', sha='fixture', reason='wall cap')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.assertIn('bundle failed', self.one()['reason'])
        self.add('evidence-bundle', minutes=-2500, role='build-runner', path='fixture', sha='fixture', bundle_sha256='fixture')
        self.assertNotIn('bundle failed', self.one()['reason'])

    def test_new_grade_or_rework_ruling_invalidates_old_readiness(self):
        self.clean()
        self.add('verdict', minutes=-2600, role='grader', confirmed=False)
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.add('review', minutes=-2500, role='reviewer', n_blocking=0)
        self.assertEqual(self.one()['bucket'], 'queued')
        self.add('decision', minutes=-2400, role='thinker', rework='yes', why='repair')
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_held_dependency_closes_only_when_dependency_ships_or_is_released(self):
        self.written()
        self.add('dependency-hold', role='thinker', waits_for='L-spec-9001', why='needs producer')
        self.assertEqual(self.one()['bucket'], 'held')
        self.assertEqual(self.one()['blocker'], 'L-spec-9001')
        self.add('shipped', 'L-spec-9001', -2000, 'executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_self_hold_is_not_a_real_wait(self):
        self.written()
        self.add('dependency-hold', role='thinker', waits_for=SID, why='broken')
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_0829_executor_records_named_thinker_dependency_hold(self):
        self.clean('L-spec-0829')
        self.add('decision', 'L-spec-0829', -2500, 'executor',
                 why='dependency-held (Thinker stuck-spec triage): waits for L-spec-0828b, from the approved split.')
        row = self.read()[-1]['L-spec-0829']
        self.assertEqual((row['bucket'], row['blocker'], row['owner']),
                         ('held', 'L-spec-0828b', 'executor'))

    def test_capability_hold_is_shared_with_fold_helper(self):
        self.written()
        self.add('capability-hold', 'capability:pg-scratch:' + SID, role='grader',
                 spec=SID, capability='pg-scratch')
        self.assertEqual(self.one()['bucket'], 'held')
        self.add('unblocked', 'capability:pg-scratch:' + SID, -2000, 'thinker')
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_open_human_question_requires_owner_and_live_deadline(self):
        self.written()
        self.add('question', role='builder', asks='grant?', deadline='2026-10-20T00:00:00Z', owner='operator')
        row = self.one()
        self.assertEqual((row['bucket'], row['owner']), ('waiting-on-human', 'operator'))
        self.assertEqual(row['deadline'], '2026-10-20T00:00:00Z')
        self.add('decision', minutes=-2000, role='operator', why='granted', revert='remove grant')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.assertIn('decision not carried out', self.one()['reason'])

    def test_expired_or_unresolvable_question_does_not_hide_stall(self):
        self.written()
        question = self.add('question', role='builder', asks='grant?', deadline='2026-10-18T00:00:00Z')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        question['deadline'] = 'when convenient'
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_existing_named_deadline_anchors_remain_real_human_waits(self):
        self.written()
        self.add('question', role='builder', asks='grant?', deadline='before merge')
        self.assertEqual(self.one()['bucket'], 'waiting-on-human')

    def test_active_dispatch_has_owner_but_stale_dispatch_does_not(self):
        self.written()
        self.add('build-started', minutes=-30, role='builder', spawn='L-builder-9000', window_min=90)
        # Use measured dwell shorter than the dispatch window, as on October 19.
        evs, specs, _, _, by, _ = self.read()
        row = fold.stall_rows(evs, specs, now=NOW, dwell={'building': .01}, by_subject=by)[0]
        self.assertEqual((row['bucket'], row['owner']), ('queued', 'builder'))
        self.add('spawn-done', minutes=-25, role='builder', spawn='L-builder-9000')
        evs, specs, _, _, by, _ = self.read()
        row = fold.stall_rows(evs, specs, now=NOW, dwell={'building': .01}, by_subject=by)[0]
        self.assertEqual(row['bucket'], 'STUCK')

    def wave(self, sid, index):
        charter = 'L-charter-9000'
        self.written(sid, charter=charter, footprint=[f'later-{index}.py'])
        plan = self.content / f'plan-{charter}.md'
        if not plan.exists():
            self.written('L-spec-8100', charter=charter, footprint=['first.py'])
            self.add('build-started', 'L-spec-8100', -1, 'builder')
            plan.write_text('## First\nFootprint:\n- first.py\nWave: 1\n')
        with plan.open('a') as f:
            f.write(f'## Later {index}\nFootprint:\n- later-{index}.py\nWave: 2\n')

    def test_wave_wait_names_unshipped_sibling_and_clears(self):
        self.wave(SID, 0)
        row = self.one()
        self.assertEqual((row['bucket'], row['blocker']), ('waiting-on-prereq', 'L-spec-8100'))
        self.add('shipped', 'L-spec-8100', -1, 'executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_unauthorized_clean_review_cannot_hide_a_stall(self):
        self.written()
        self.add('review', role='builder', n_blocking=0)
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.assertEqual(len(self.read()[3]), 1)

    def test_terminal_and_young_specs_are_not_candidates(self):
        self.written()
        self.add('shipped', role='executor')
        self.written('L-spec-9001', minutes=-1)
        self.assertEqual(self.read()[-1], {})

    def test_classifier_unavailable_is_not_invented_stalls_or_a_healthy_zero(self):
        self.written()
        evs, specs, charters, ignored, by, _ = self.read()
        with patch('fold_stalls.prepare', side_effect=RuntimeError('cannot read prerequisites')):
            with self.assertRaises(RuntimeError):
                fold.stall_lane_rows(evs, specs, NOW, by_subject=by)
            board = fold.render(evs, specs, charters, ignored, by)
        self.assertNotIn('⚠ WEDGE', board)
        self.assertIn('aged specs: unavailable', board)
        self.assertIn('classification unavailable · owner executor', board)

    def test_footprint_wait_requires_a_real_unshipped_blocker(self):
        self.written()
        self.written('L-spec-9001', minutes=-1)
        hold = self.add('blocked', minutes=-2600, role='executor', blocker='L-spec-9001', why='overlap')
        self.assertEqual(self.one()['bucket'], 'waiting-on-prereq')
        self.add('shipped', 'L-spec-9001', -1, 'executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        hold['blocker'] = 'L-spec-missing'
        self.assertEqual(self.one()['bucket'], 'STUCK')

    def test_october_19_triage_fixture_and_board_and_realm_agree(self):
        for n in range(25):
            self.clean(f'L-spec-{9200+n:04d}')
        for sid in ('L-spec-0662', 'L-spec-1173'):
            self.clean(sid)
            self.add('merge-gate-clean', sid, -2600, 'executor')
        for sid, dependency in (('L-spec-1138', 'L-spec-1137'), ('L-spec-1185', 'L-spec-1183'),
                                 ('L-spec-1186', 'L-spec-1185'), ('L-spec-1187', 'L-spec-1186')):
            self.written(sid)
            self.add('dependency-hold', sid, -2600, 'thinker', waits_for=dependency, why='deliberate hold')
        for n, sid in enumerate(('L-spec-1150', 'L-spec-1153', 'L-spec-1154', 'L-spec-1193',
                                  'L-spec-1194', 'L-spec-1195', 'L-spec-1196')):
            self.wave(sid, n)
        self.clean('L-spec-1170')
        self.add('conflict-rework', 'L-spec-1170', -2600, 'executor')
        self.written('L-spec-1189')
        self.add('build-done', 'L-spec-1189', -2900, 'builder', status='DONE')
        self.add('evidence-bundle-failed', 'L-spec-1189', -2600, 'build-runner', sha='fixture', reason='wall cap')
        self.written('L-spec-0828')
        self.add('decision', 'L-spec-0828', -2600, 'thinker', why='Re-cut into two; planner registers replacements')
        self.written('L-spec-9009')
        self.add('verdict', 'L-spec-9009', -2600, 'grader', confirmed=True)
        evs, specs, charters, ignored, by, rows = self.read()
        self.assertEqual(Counter(r['bucket'] for r in rows.values()),
                         {'queued': 27, 'held': 4, 'waiting-on-prereq': 7, 'STUCK': 4})
        lane = fold.stall_lane_rows(evs, specs, NOW, by_subject=by)
        self.assertEqual({r['spec'] for r in lane if r['overdue'] and not r['reason_live']},
                         {sid for sid, r in rows.items() if r['bucket'] == 'STUCK'})
        for sid, row in rows.items():
            self.assertEqual(fold.wedged(specs[sid], fold.DWELL_DAYS, events=evs,
                                        specs=specs, by_subject=by, now=NOW),
                             row['bucket'] == 'STUCK')
        before = {p: p.read_bytes() for p in self.events.iterdir()}
        with patch.object(dispatch, 'emit', side_effect=AssertionError('classifier wrote ledger')):
            board = fold.render(evs, specs, charters, ignored, by)
        self.assertEqual(board.count('⚠ WEDGE'), 4)
        self.assertIn('queued=27', board)
        self.assertIn('waiting-on-prereq=7', board)
        self.assertIn('owner executor', board)
        self.assertEqual(before, {p: p.read_bytes() for p in self.events.iterdir()})
        self.assertTrue(set(r['bucket'] for r in lane) <= set(BUCKETS))


if __name__ == '__main__':
    unittest.main()
