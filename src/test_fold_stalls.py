"""Admitted scratch-ledger regressions for unsuccessful builder outcomes."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fold

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SID = 'L-spec-91909'
ABSENT = object()


def row(kind, actor='builder', minute=0, subject=SID, **fields):
    return actor, dict(type=kind, subject=subject,
                       ts=(NOW - timedelta(days=3) + timedelta(minutes=minute)).isoformat(), **fields)


def outcome(status=ABSENT, **fields):
    return row('build-done', **fields, **({} if status is ABSENT else {'status': status}))


@contextmanager
def admitted(rows):
    """Use the real reader: actors come from filenames, including timestamp ties."""
    import dispatch
    with tempfile.TemporaryDirectory(prefix='doit-blocked-') as directory:
        root = Path(directory)
        events_dir = root / 'events'
        events_dir.mkdir()
        (root / 'content').mkdir()
        grouped = {}
        for actor, event in rows:
            grouped.setdefault(actor, []).append(event)
        for actor, events in grouped.items():
            (events_dir / f'L-{actor}-fixture.jsonl').write_text(
                ''.join(json.dumps(e) + '\n' for e in events))
        with patch.object(fold, 'PROJECT', None), patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', events_dir), \
             patch.object(fold, 'NOW', NOW), patch.object(fold, 'SESSIONS', root / 'sessions'), \
             patch.object(dispatch, 'CONTENT', root / 'content'), \
             patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE='1'):
            events = fold.read_events(use_cache=False)
            yield root, events, fold.fold(events)


def evidence(name, body):
    body = json.dumps(body, sort_keys=True, default=str)
    print(json.dumps(dict(url='scratch://blocked-build/' + name, status=0,
                         body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


class BlockedBuildSpec(unittest.TestCase):
    def fixture(self, history):
        return admitted([row('spec-written', 'planner', minute=-10), *history])

    def test_ac1_non_done_state(self):
        statuses = ['DONE', 'BLOCKED', 'NEEDS_CONTEXT', 'OTHER', '', None, ABSENT]
        checked = []
        for status in statuses:
            for history, expected in [([outcome(status)], 'graded' if status == 'DONE' else 'blocked'),
                                      ([outcome('DONE'), outcome(status, minute=1)], 'graded' if status == 'DONE' else 'blocked')]:
                with self.subTest(status=str(status)), self.fixture(history) as (_, events, folded):
                    self.assertEqual(folded[0][SID]['state'], expected)
                    # A warmed reader snapshot must retain the same admitted authority.
                    self.assertEqual(fold.fold(fold.read_events())[0][SID]['state'], expected)
                    checked.append([str(status) if status is not ABSENT else 'absent', expected])
        for status in statuses:
            with self.fixture([outcome('DONE'), outcome(status)]) as (_, _, folded):
                self.assertEqual(folded[0][SID]['state'], 'graded' if status == 'DONE' else 'blocked')
        for tied in (False, True):
            minute = 0 if tied else 1
            for first, second, expected in [('DONE', 'BLOCKED', 'blocked'), ('BLOCKED', 'DONE', 'graded')]:
                with self.fixture([outcome(first), outcome(second, minute=minute)]) as (_, _, folded):
                    self.assertEqual(folded[0][SID]['state'], expected)
        controls = [([outcome(actor='executor')], 'graded'),
                    ([outcome('DONE', actor='executor', actor_claim='builder')], 'graded'),
                    ([outcome('BLOCKED'), outcome('DONE', actor='executor', minute=1)], 'blocked'),
                    ([outcome('DONE'), outcome('BLOCKED', actor='executor', minute=1)], 'graded'),
                    ([outcome('DONE'), row('verdict', 'grader', minute=1, confirmed=True), outcome('BLOCKED', minute=2)], 'blocked'),
                    ([outcome('BLOCKED'), row('verdict', 'grader', minute=1, confirmed=True)], 'reviewing')]
        for history, expected in controls:
            with self.fixture(history) as (_, _, folded):
                self.assertEqual(folded[0][SID]['state'], expected)
        for kind, actor, expected in [('spec-killed', 'spec-writer', 'killed'),
                                      ('spec-closed', 'operator', 'closed-unbuilt'),
                                      ('shipped', 'executor', 'shipped')]:
            with self.fixture([row(kind, actor, minute=1), outcome('BLOCKED', minute=2)]) as (_, _, folded):
                self.assertEqual(folded[0][SID]['state'], expected)
        # The append route cannot grant builder authority through an event body.
        with self.fixture([]) as (_, _, _):
            with patch.dict(os.environ, DOIT_LEDGER_FILE='L-executor-local.jsonl'):
                with self.assertRaises(SystemExit):
                    fold.append(['build-done', SID, 'status=DONE', 'actor=builder'])
                fold.append(['build-done', SID, 'status=DONE'])
            events = fold.read_events(use_cache=False)
            event = next(e for e in events if e['type'] == 'build-done')
            self.assertEqual(event['actor'], 'executor')
            self.assertEqual(fold.fold(events)[0][SID]['state'], 'graded')
        evidence('AC1', checked)

    def test_ac2_block_evidence(self):
        histories = [
            ([outcome('BLOCKED', spawn='L-builder-new'),
              row('build-blocked', minute=1, spawn='L-builder-new', reason='late current block'),
              row('build-blocked', minute=2, spawn='L-builder-old', reason='old block', owner='old owner')], 'late current block', 'executor'),
            ([row('build-blocked', reason='x' * 400, owner='y' * 400), outcome('BLOCKED', minute=1)], 'x' * 300, 'y' * 300),
            ([row('build-started', minute=-4, spawn='L-builder-old'),
              row('build-blocked', minute=-3, spawn='L-builder-old', reason='old block', owner='old owner'),
              outcome('BLOCKED', minute=-2, spawn='L-builder-old'),
              row('build-started', minute=-1, spawn='L-builder-new'),
              row('build-blocked', spawn='L-builder-new', reason='repair\ncurrent\x00 fault', owner='human\nowner'),
              outcome('NEEDS_CONTEXT', minute=1, spawn='L-builder-new'),
              row('evidence-bundle', 'build-runner', minute=2),
              row('spawn-done', 'executor', minute=3, spawn='L-builder-new', role='builder')], 'repair current fault', 'human owner'),
            ([row('build-blocked', reason='legacy block'), outcome('BLOCKED', minute=1)], 'legacy block', 'executor'),
            ([row('build-blocked', minute=-2, reason='old block'), outcome('DONE', minute=-1),
              outcome('OTHER')], 'no recorded block line', 'executor'),
            ([row('build-blocked', subject='L-spec-91910', reason='wrong subject'), outcome(None)], 'no recorded block line', 'executor')]
        checked = []
        for history, why, owner in histories:
            with self.fixture(history) as (_, events, folded):
                rows = fold.stall_rows(events, folded[0], by_subject=folded[3])
                self.assertEqual(len(rows), 1)
                r = rows[0]
                self.assertIn(why, r['why'])
                self.assertEqual(r['reason'], r['why'])
                self.assertEqual(r['owner'], owner)
                self.assertNotIn('old block', r['why'])
                if why == 'no recorded block line':
                    self.assertIn(str(history[-1][1].get('status')), r['why'])
                checked.append(r)
        evidence('AC2', checked)

    def test_ac3_shared_views(self):
        import lane_rules
        import nextstep
        history = [row('build-blocked', reason='needs repair', owner='operator'), outcome('BLOCKED')]
        with self.fixture(history) as (_, events, folded):
            specs, charters, ignored, by = folded
            r = fold.stall_rows(events, specs, by_subject=by)[0]
            with patch.object(fold, 'stall_rows', wraps=fold.stall_rows) as public_rows:
                lane = fold.stall_lane_rows(events, specs, by_subject=by)[0]
                self.assertEqual(public_rows.call_count, 1)
            # Realm registers its temporary fold alias only after module execution.
            from realm_ledger import Ledger
            import sys
            with Ledger().live(Path(fold.__file__).parent, fold.ROOT) as live_fold:
                with patch.object(live_fold, 'stall_rows', wraps=live_fold.stall_rows) as calls:
                    live_fold.stall_lane_rows(events, specs, NOW, by_subject=by)
                    self.assertEqual(calls.call_count, 1)
                    self.assertIs(sys.modules['fold_stalls'].fold, live_fold)
            self.assertEqual((lane['action'], lane['actor']), (r['reason'], r['owner']))
            board = fold.render(events, *folded)
            expected = SID + ' · blocked · 3.0d · needs repair · owner operator'
            from fold_stalls import in_flight_lines
            self.assertEqual(in_flight_lines(events, specs, {SID: r}, lambda s: ''), [expected])
            section = board.split('## IN FLIGHT (1)\n', 1)[1].split('\n\n', 1)[0]
            self.assertEqual(section, '  ' + expected + '  ⚠ WEDGE · STUCK · owner operator · needs repair')
            self.assertIn('## AWAITING VERIFICATION (0)', board)
            self.assertIn('## BLOCKED (0)', board)
            step = next(s for s in nextstep.next_steps(events, specs, NOW, lane_rules.load()) if s.item == SID)
            self.assertNotIn('unknown spec state', step.reason)
            self.assertNotEqual(step.action, 'grade')
            evidence('AC3', dict(row=r, lane=lane, board=board))
        for hold in [row('decision', 'operator', minute=1, why='held on purpose: repair approval'),
                     row('question', 'builder', minute=1, asks='choose repair', deadline=(NOW + timedelta(days=1)).isoformat(), owner='thinker')]:
            with self.fixture([*history, hold]) as (_, events, folded):
                r = fold.stall_rows(events, folded[0], by_subject=folded[3])[0]
                self.assertIn(r['bucket'], ('held', 'waiting-on-human'))
                self.assertEqual(r['why'], 'needs repair')
                self.assertNotEqual(r['reason'], r['why'])
        for recovery, expected in [(outcome('DONE', minute=2), 'graded'),
                                    (row('verdict', 'grader', minute=2, confirmed=True), 'reviewing')]:
            with self.fixture([*history, recovery]) as (_, events, folded):
                self.assertEqual(folded[0][SID]['state'], expected)
                r = fold.stall_rows(events, folded[0], by_subject=folded[3])[0]
                self.assertNotIn('needs repair', r.get('why', ''))


if __name__ == '__main__':
    unittest.main()
