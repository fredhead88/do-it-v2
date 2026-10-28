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
from fold_stalls import BUCKETS, BUCKET_ORDER

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

    def test_old_clean_review_and_gate_need_a_window(self):
        self.clean()
        # SD2: a clean review/gate over 120 minutes old is overdue.
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.add('merge-gate-clean', minutes=-2600, role='executor')
        row = self.one()
        self.assertEqual(row['bucket'], 'STUCK')
        self.assertIn('over 2 h', row['reason'])
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
        self.assertEqual(self.one()['reason'], 'waited over 2 h for a merge window')

    def test_new_conflict_invalidates_old_clean_review(self):
        self.clean()
        self.add('conflict-rework', minutes=-2600, role='executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        self.assertIn('conflict-rework', self.one()['reason'])
        self.add('review', minutes=-2500, role='reviewer', n_blocking=0)
        # SD2: a clean review/gate over 120 minutes old is overdue.
        self.assertEqual(self.one()['bucket'], 'STUCK')

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
        # SD2: a clean review/gate over 120 minutes old is overdue.
        self.assertEqual(self.one()['bucket'], 'STUCK')
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
        self.assertEqual((row['bucket'], row['owner']), ('in-flight', 'builder'))
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
        self.assertEqual((row['bucket'], row['blocker']), ('held', 'L-spec-8100'))
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
        # SD2: named prerequisites, including missing ones, are deliberate holds.
        self.assertEqual(self.one()['bucket'], 'held')
        self.add('shipped', 'L-spec-9001', -1, 'executor')
        self.assertEqual(self.one()['bucket'], 'STUCK')
        hold['blocker'] = 'L-spec-missing'
        self.assertEqual(self.one()['bucket'], 'held')
        self.assertEqual(self.one()['hold_kind'], 'prerequisite')

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
                         {'STUCK': 31, 'held': 11})
        lane = fold.stall_lane_rows(evs, specs, NOW, by_subject=by)
        self.assertEqual({r['spec'] for r in lane if r['overdue'] and not r['reason_live']},
                         {sid for sid, r in rows.items() if r['bucket'] == 'STUCK'} | {'L-spec-8100'})
        for sid, row in rows.items():
            self.assertEqual(fold.wedged(specs[sid], fold.DWELL_DAYS, events=evs,
                                        specs=specs, by_subject=by, now=NOW),
                             row['bucket'] == 'STUCK')
        before = {p: p.read_bytes() for p in self.events.iterdir()}
        with patch.object(dispatch, 'emit', side_effect=AssertionError('classifier wrote ledger')):
            board = fold.render(evs, specs, charters, ignored, by)
        # SD2: default consumers also include the sub-day prerequisite's overdue action.
        self.assertEqual(board.count('⚠ WEDGE'), 32)
        self.assertIn('queued=0', board)
        self.assertIn('held=11', board)
        self.assertIn('owner executor', board)
        self.assertEqual(before, {p: p.read_bytes() for p in self.events.iterdir()})
        self.assertTrue(set(r['bucket'] for r in lane) <= set(BUCKETS))



    def sd(self, sid=SID, now=NOW):
        evs, specs, _, _, by, _ = self.read()
        rows = fold.stall_rows(evs, specs, now=now,
                               dwell={s['state']: -1 for s in specs.values()}, by_subject=by)
        return {r['spec']: r for r in rows}[sid]

    def test_ac1_buckets_and_row_keys(self):
        expected = ('STUCK', 'held', 'audit-wait', 'retry-wait', 'in-flight', 'queued', 'waiting-on-human')
        self.assertIsInstance(BUCKETS, frozenset)
        self.assertEqual(BUCKETS, frozenset(expected))
        self.assertEqual(BUCKET_ORDER, expected)
        for n in range(7):
            self.written(f'L-spec-{9500+n}', minutes=-40 if n != 5 else -10,
                         role='spec-writer' if n == 2 else 'planner')
        self.add('decision', 'L-spec-9501', -5, 'operator', why='Held on purpose: operator hold')
        self.add('spec-written', 'L-spec-9502', -29, 'spec-writer')
        self.add('spawn-failed', 'L-spec-9503', -3, 'executor', spawn='L-builder-9503', reason='capacity')
        self.files['executor'][-1]['role']='builder'
        self.add('spawn-started', 'L-spec-9504', -3, 'executor', spawn='L-builder-9504')
        self.files['executor'][-1]['role']='builder'
        self.add('question', 'L-spec-9506', -2, 'builder', asks='access', deadline=(NOW+timedelta(hours=1)).isoformat())
        for n, bucket in enumerate(expected):
            r=self.sd(f'L-spec-{9500+n}')
            self.assertEqual(r['bucket'], bucket, r)
            self.assertTrue({'bucket','hold_kind','due_since','reason'} <= r.keys())
            self.assertEqual(r['state'], 'written')
            self.assertEqual(r['due_since'] is not None, bucket == 'STUCK')
            self.assertEqual(r['hold_kind'] is not None, bucket == 'held')
            print(r['spec'],r['bucket'],r['hold_kind'],r['due_since'],r['state'])

    def test_ac6_board_and_lane_consumers(self):
        import subprocess, sys
        for age, bucket in [(119, 'queued'), (120, 'queued'), (121, 'STUCK')]:
            self.files = {role: [] for role in self.files}
            self.clean()
            self.files['reviewer'][-1]['ts'] = (NOW-timedelta(minutes=age)).isoformat()
            self.written('L-spec-9901')
            self.add('shipped', 'L-spec-9901', -5, 'executor')
            self.written('L-spec-9902')
            self.add('spec-killed', 'L-spec-9902', -5, 'thinker')
            self.add('observed', 'L-spec-9903', -5, 'thinker')
            evs, specs, charters, ignored, by, _ = self.read()
            lane = fold.stall_lane_rows(evs, now=NOW)
            self.assertEqual([(r['spec'], r['bucket']) for r in lane], [(SID, bucket)])
            self.assertEqual(fold.wedged(specs[SID], events=evs, now=NOW), bucket == 'STUCK')
            board = fold.render(evs, specs, charters, ignored, by)
            self.assertEqual(board.count('⚠ WEDGE'), int(bucket == 'STUCK'))
            self.assertIn('STUCK='+str(int(bucket == 'STUCK')), board)
            print('default population', age, [(r['spec'],r['bucket']) for r in lane], board)
        with patch('fold_stalls.rows', side_effect=RuntimeError('fixture unavailable')):
            self.assertIn('aged specs: unavailable', fold.render(evs, specs, charters, ignored, by))
        # Reuse the seven-bucket fixture; production board/lane classification.
        self.files = {role: [] for role in self.files}
        self.test_ac1_buckets_and_row_keys()
        evs, specs, charters, ignored, by, _ = self.read()
        lane = fold.stall_lane_rows(evs, now=NOW)
        self.assertEqual({r['bucket'] for r in lane}, BUCKETS)
        self.assertTrue(all(r['overdue'] == (r['bucket']=='STUCK') and
                            r['reason_live'] == (r['bucket']!='STUCK') for r in lane))
        code = ('import fold,json; from datetime import datetime; '
                'fold.NOW=datetime.fromisoformat('+repr(NOW.isoformat())+'); '
                'e=fold.read_events(use_cache=False); print(fold.render(e,*fold.fold(e)))')
        outputs = [subprocess.check_output([sys.executable,'-c',code], env={**os.environ,
                   'PYTHONPATH':'src','DOIT_ROOT':str(self.root),'PYTHONHASHSEED':seed}, text=True)
                   for seed in ['1','234']]
        self.assertEqual(*outputs)
        health = next(line for line in outputs[0].splitlines() if 'aged specs:' in line)
        self.assertEqual([x.split('=')[0] for x in health.split('aged specs: ')[1].split(' · ')], list(BUCKET_ORDER))
        self.assertEqual(outputs[0].count('⚠ WEDGE'), 1)
        self.assertIn('L-spec-9500', next(line for line in outputs[0].splitlines() if '⚠ WEDGE' in line))
        for seed, output in zip(['1','234'],outputs):
            print('seven-bucket board, hash seed',seed,output)
        print('hash-seed stable:',health)

    def test_ac2_recorded_holds_are_held_with_kind(self):
        for n, (actor, why, kind, revisit) in enumerate([
            ('operator','Held on purpose: maintenance','operator',None),
            ('thinker','Held on purpose: decision','decision',1),
            ('thinker','Held on purpose: decision','decision',-1),
            ('thinker','Held on purpose: decision','decision',None)]):
            sid=f'L-spec-{9600+n}'; self.clean(sid)
            self.files['reviewer'][-1]['ts']=(NOW-timedelta(days=5)).isoformat()
            # Keep the lifecycle ordering valid for the five-day review.
            for rows in self.files.values():
                for event in rows:
                    if event.get('subject') == sid and event['type'] in {'spec-written','build-done','verdict'}:
                        event['ts']=(NOW-timedelta(days=6)).isoformat()
            fields={} if revisit is None else {'revisit_at':(NOW+timedelta(days=revisit)).isoformat()}
            self.add('decision',sid,-200,actor,why=why,**fields)
            self.add('escalation-blocking',sid,-180,'executor',why='access',default='hold',revert='release',deadline=(NOW+timedelta(days=1)).isoformat())
            r=self.sd(sid); self.assertEqual((r['bucket'],r['hold_kind']),('held',kind)); print(sid,kind,r['bucket'])
        self.written('L-spec-9610'); self.add('dependency-hold','L-spec-9610',-200,'thinker',waits_for='L-spec-9699',why='producer')
        self.assertEqual(self.sd('L-spec-9610')['hold_kind'],'dependency')
        for n, blocker in [(9611,'L-spec-9698'),(9612,'L-spec-9697')]:
            self.written(f'L-spec-{n}')
            self.add('blocked',f'L-spec-{n}',-200,'executor',blocker=blocker)
            if n==9611: self.written(blocker)
            self.assertEqual(self.sd(f'L-spec-{n}')['hold_kind'],'prerequisite')
        self.wave('L-spec-9613',0); self.assertEqual(self.sd('L-spec-9613')['hold_kind'],'wave')
        self.written('L-spec-9614'); self.add('capability-hold','capability:x:L-spec-9614',-200,'grader',spec='L-spec-9614',capability='x')
        self.assertEqual(self.sd('L-spec-9614')['hold_kind'],'capability')
        self.written('L-spec-9615'); self.add('decision','lane-stale:L-spec-9615',-200,'thinker',why='Held on purpose: sequence')
        self.assertEqual(self.sd('L-spec-9615')['hold_kind'],'decision')
        self.add('unblocked','L-spec-9615',-40,'thinker'); self.assertNotEqual(self.sd('L-spec-9615')['bucket'],'held')
        self.add('shipped','L-spec-9698',-40,'executor'); self.assertNotEqual(self.sd('L-spec-9611')['bucket'],'held')
        self.written('L-spec-9616'); self.add('decision','L-spec-9616',-40,'thinker',why='We discussed a hold yesterday')
        self.assertNotEqual(self.sd('L-spec-9616')['bucket'],'held')
        for sid in sorted(self.read()[1]):
            r=self.sd(sid); print(sid,r['bucket'],r['hold_kind'])

    def test_ac3_in_flight_retry_wait_and_exhausted(self):
        self.written(); failure=self.add('spawn-failed',minutes=-10,role='executor',spawn='L-builder-9001',reason='capacity')
        failure['role']='builder'
        threshold=fold.ts(failure['ts'])+timedelta(minutes=5)
        for seconds,bucket in [(180,'retry-wait'),(300,'retry-wait'),(301,'STUCK'),(600,'STUCK')]:
            r=self.sd(now=fold.ts(failure['ts'])+timedelta(seconds=seconds))
            self.assertEqual(r['bucket'],bucket,r)
            if bucket=='STUCK': self.assertEqual(r['due_since'],threshold.isoformat())
            print('failure',failure['ts'],threshold.isoformat(),r['bucket'],r['reason'])
        self.add('spawn-failed',minutes=-9,role='executor',spawn='L-builder-9002',reason='capacity')['role']='builder'
        self.add('spawn-failed',minutes=-8,role='executor',spawn='L-builder-9003',reason='capacity')['role']='builder'
        self.assertEqual(self.sd()['bucket'],'STUCK')
        exhausted_at=self.sd()['due_since']
        self.assertEqual(self.sd(now=NOW+timedelta(hours=1))['due_since'],exhausted_at)
        self.add('spec-amended',minutes=-2,role='thinker')
        self.assertEqual(self.sd()['bucket'],'queued')
        self.add('unblocked',minutes=-1,role='thinker'); self.assertEqual(self.sd()['bucket'],'queued')
        self.add('spawn-started',minutes=-.5,role='executor',spawn='L-builder-9004')['role']='builder'
        self.assertEqual(self.sd()['bucket'],'in-flight')
        self.written('L-spec-9701',minutes=-29)
        self.add('spawn-failed','L-spec-9701',-28,'executor',spawn='L-builder-9701',reason='bundle-pending: wait')['role']='builder'
        self.assertEqual(self.sd('L-spec-9701')['bucket'],'queued')
        import attempts
        with patch.object(attempts,'history',side_effect=RuntimeError('missing attempts')):
            self.assertIn('attempt',self.sd('L-spec-9701')['reason'])
            self.assertEqual(self.sd('L-spec-9701')['bucket'],'STUCK')
        for status in ['seat-wait','dispatchable']:
            self.written('L-spec-9702',minutes=-29,role='spec-writer')
            self.add('spawn-done','L-spec-9702',-29,'executor',spawn='L-spec-auditor-9702')['role']='spec-auditor'
            with patch('autodispatch.candidates',return_value=[dict(spec='L-spec-9702',status=status,since=NOW.isoformat())]):
                r=self.sd('L-spec-9702'); self.assertEqual(r['bucket'],'queued')
                self.assertIn('waiting for a seat' if status=='seat-wait' else 'awaiting assignment',r['reason'])
                self.assertEqual(self.sd('L-spec-9702',NOW+timedelta(minutes=2))['bucket'],'STUCK')
                print(status,r['bucket'],r['reason'],'31m STUCK; fresh observation does not reset trigger')

    def test_ac4_window_wait_boundary(self):
        self.clean(); self.add('review',minutes=-121,role='reviewer',n_blocking=0)
        due=(NOW-timedelta(minutes=1)).isoformat()
        for age,bucket in [(119,'queued'),(120,'queued'),(121,'STUCK'),(7200,'STUCK')]:
            r=self.sd(now=NOW+timedelta(minutes=age-121)); self.assertEqual(r['bucket'],bucket,r)
            if bucket=='STUCK':
                self.assertEqual(r['due_since'],due); self.assertIn('over 2 h',r['reason'])
            print(age,r['bucket'],r['due_since'],r['reason'])
        self.add('window-opened','window-fixture',-120,'executor',specs=[SID])
        self.assertEqual(self.sd(now=NOW+timedelta(days=5))['bucket'],'queued')
        for n,kind in enumerate(['conflict-rework','window-dropped','must-fix','rejected-criterion']):
            sid=f'L-spec-{9750+n}'; self.clean(sid)
            self.add(kind,sid,-40,'reviewer' if kind in ('must-fix','rejected-criterion') else 'executor',criterion='AC1',why='repair')
            r=self.sd(sid); self.assertEqual(r['bucket'],'STUCK'); self.assertNotIn('merge window',r['reason'])

    def test_ac5_audit_wait_and_carried_exempt(self):
        self.written(role='spec-writer',minutes=-30)
        for minute,bucket in [(29,'audit-wait'),(30,'audit-wait'),(31,'STUCK')]:
            r=self.sd(now=NOW+timedelta(minutes=minute-30)); self.assertEqual(r['bucket'],bucket,r)
            print('audit',minute,r['bucket'],r['due_since'],r['reason'])
        self.add('spawn-started',minutes=14,role='executor',spawn='L-spec-auditor-9001')['role']='spec-auditor'
        r=self.sd(now=NOW+timedelta(minutes=15))
        self.assertEqual(r['bucket'],'in-flight'); print('audit 45 live',r['bucket'],r['reason'])
        self.add('spawn-failed',minutes=14,role='executor',spawn='L-spec-auditor-9001',reason='capacity')['role']='spec-auditor'
        r=self.sd(now=NOW+timedelta(minutes=15))
        self.assertEqual(r['bucket'],'audit-wait'); print('audit 45 backoff',r['bucket'],r['reason'])
        self.add('spawn-done',minutes=15,role='executor',spawn='L-spec-auditor-9002')['role']='spec-auditor'
        r=self.sd(now=NOW+timedelta(minutes=15))
        self.assertEqual(r['bucket'],'queued'); print('audit completed, builder allowance',r['bucket'],r['reason'])
        self.add('spec-written',minutes=20,role='spec-writer')
        r=self.sd(now=NOW+timedelta(minutes=21))
        self.assertEqual(r['bucket'],'queued'); print('rewrite, no second audit',r['bucket'],r['reason'])
        self.written('L-spec-9801',minutes=-29,role='spec-writer')
        self.add('spec-carried','L-spec-9801',-28,'executor',source='fixture',tier='trusted',audited_at=NOW.isoformat())
        self.written('L-spec-9802',minutes=-29,role='operator')
        for sid in ['L-spec-9801','L-spec-9802']:
            r=self.sd(sid); self.assertEqual(r['bucket'],'queued'); print('exempt',sid,r['bucket'])


if __name__ == '__main__':
    unittest.main()
