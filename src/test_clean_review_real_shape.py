#!/usr/bin/env python3
"""Real reviewer events and bounded, oldest-first automatic gate attempts."""
import atexit
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

# Isolate imports as well as ledger writes from the operator's root.
_root = tempfile.TemporaryDirectory(prefix='clean-review-root-')
atexit.register(_root.cleanup)
os.environ['DOIT_ROOT'] = _root.name

import dispatch
import fold
import lane_rules
import merge_queue
import nextstep
import tick


class CleanReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='clean-review-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        self.now = datetime.now(timezone.utc)
        for module, values in (
                (fold, dict(ROOT=self.root, EVENTS=self.root / 'events', NOW=self.now)),
                (dispatch, dict(ROOT=self.root, EVENTS=self.root / 'events',
                                CONTENT=self.root / 'content', now=lambda: self.now.isoformat()))):
            for key, value in values.items():
                p = patch.object(module, key, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        p.start()
        self.addCleanup(p.stop)
        self.sid = 'L-spec-9001'
        self.events = [self.event('spec-written', 20, project='fixture', footprint=['allowed.txt']),
                       self.event('build-done', 15),
                       self.event('review', 10, depth='full', round=1, n_blocking=0)]
        self.calls = []

    def event(self, kind, age=0, **fields):
        return dict(type=kind, subject=fields.pop('subject', self.sid),
                    ts=(self.now - timedelta(minutes=age)).isoformat(), **fields)

    def context(self, events):
        return dict(evs=events, spec=dict(state='reviewing'), dependency=False,
                    capabilities={}, sid=self.sid)

    def mark(self, ac, body):
        print(f'L-spec-1785 AC{ac} ok')
        print(json.dumps(dict(url=f'cli://doit-v2/clean-review-real-shape/AC{ac}', status=0,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              body_excerpt=body)))

    def test_ac1_real_review(self):
        review = self.events[-1]
        self.assertNotIn('confirmed', review)
        c = self.context(self.events)
        self.assertIs(nextstep._clean_review(c), review)
        self.assertEqual(nextstep._gate(c)['since'], review['ts'])
        self.mark(1, 'real review recognised; gate since equals review timestamp')

    def test_ac2_blockers_and_enqueue(self):
        review = self.events[-1]
        cases = [self.events[:-1] + [dict(review, n_blocking=1)],
                 self.events + [self.event('rejected-criterion', 5, criterion='AC1')],
                 self.events + [self.event('must-fix', 5, criterion='AC1')],
                 self.events + [self.event('build-done', 5)],
                 self.events + [self.event('build-blocked', 5)]]
        for events in cases:
            with self.subTest(events=events):
                self.assertIsNone(nextstep._clean_review(self.context(events)))
                self.assertIsNone(nextstep._gate(self.context(events)))
        for invalid in (False, '0', 0.0, None):
            self.assertIsNone(nextstep._clean_review(self.context(
                self.events[:-1] + [dict(review, n_blocking=invalid)])))
        clean = self.events + [self.event('merge-gate-clean', 5)]
        self.assertIsNone(nextstep._gate(self.context(clean)))
        self.assertEqual(nextstep._enqueue(self.context(clean))['since'], review['ts'])
        self.mark(2, 'blocking/reject/must-fix/later build blocked; integer check retained; '
                  'clean gate suppresses gate and permits enqueue')

    def queue_fixture(self):
        repo = self.root / 'repos' / 'fixture'
        repo.mkdir(parents=True)
        result = subprocess.run(['git', 'init', '-q', '-b', 'fixture-head'], cwd=repo,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows, events = [], []
        # Equal oldest timestamps test stable tie ordering as well.
        for number, age in enumerate((5, 20, 10, 20, 15), 1):
            sid = f'L-spec-{9000 + number}'
            since = self.event('review', age)['ts']
            rows.append(dict(spec=sid, rule='gate', since=since, limit_min=30,
                             overdue=False, reason='condition met', reason_live=False))
            events.append(self.event('spec-written', 25, subject=sid, project='fixture',
                                     footprint=['allowed.txt']))
            (self.root / 'content' / f'{sid}.md').write_text('Writes: allowed.txt\n')
        return rows, events

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        return dict(code=0, stdout='', stderr='')

    def run_queue(self, rows, events, **kwargs):
        with patch.object(lane_rules, 'due', return_value=rows):
            return merge_queue.enqueue_due(events, self.now, runner=self.runner, **kwargs)

    def landed(self):
        path = tick.tick_path()
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_ac3_oldest_and_skipped(self):
        rows, events = self.queue_fixture()
        out = self.run_queue(rows, events)
        expected = ['L-spec-9002', 'L-spec-9004']
        self.assertEqual([call[5] for call in self.calls], expected)
        self.assertTrue(all(call[:2] == ['doit', 'gate'] for call in self.calls))
        summary = [line for line in out if line.startswith('gate deferred ')]
        self.assertEqual(summary, ['gate deferred 3 (per-tick cap 2): '
                                   'L-spec-9005, L-spec-9003, L-spec-9001...'])
        self.assertEqual([e['subject'] for e in self.landed()], expected)
        # The previously dispatched oldest row must leave both slots available.
        self.calls.clear()
        skipped = self.event('autodispatched', 1, subject='L-spec-9002', rule='gate',
                             since=rows[1]['since'], role='gate', spawn='none')
        out = self.run_queue(rows, events + [skipped])
        self.assertEqual([call[5] for call in self.calls], ['L-spec-9004', 'L-spec-9005'])
        self.assertEqual(len([line for line in out if line.startswith('gate deferred 2 ')]), 1)
        self.mark(3, 'gates L-spec-9002,L-spec-9004; ' + summary[0] +
                  '; already-dispatched oldest skipped; next two eligible gated')

    def test_failed_attempts_dry_run_and_retry(self):
        rows, events = self.queue_fixture()
        out = self.run_queue(rows, events, dry_run=True)
        self.assertEqual(len([s for s in out if s.startswith('would gate ')]), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.landed(), [])
        def fail(argv, **kwargs):
            self.calls.append(argv)
            raise RuntimeError('scratch gate failure')
        with patch.object(self, 'runner', side_effect=fail):
            out = self.run_queue(rows, events)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len([s for s in out if s.startswith('gate failed ')]), 2)
        self.assertEqual(len([s for s in out if s.startswith('gate deferred 3 ')]), 1)
        self.calls.clear()
        later = self.now + timedelta(minutes=40)
        rows = [dict(row, overdue=True) for row in rows]
        with patch.object(lane_rules, 'due', return_value=rows):
            merge_queue.enqueue_due(events + self.landed(), later, runner=self.runner)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len([e for e in self.landed() if e.get('trigger') == 'retry']), 2)

    def test_other_skips_leave_slots(self):
        rows, events = self.queue_fixture()
        oldest = rows[1]
        for skip in ('undetermined', 'busy', 'ladder', 'retry'):
            self.calls.clear()
            due = [dict(row) for row in rows]
            extra = []
            if skip == 'undetermined':
                due[1]['reason'] = 'undetermined: scratch fixture'
                due[1]['since'] = 'undetermined'
            elif skip == 'retry':
                extra = [self.event('autodispatched', 1, subject=oldest['spec'], rule='gate',
                                    role='gate', since=oldest['since'], trigger='retry', spawn='none')]
            ladder = lane_rules.ladder
            with patch.object(lane_rules, '_busy', return_value=(
                    {oldest['spec']} if skip == 'busy' else set(), set(), set())), \
                    patch.object(lane_rules, 'ladder', side_effect=lambda evs, row, now:
                                 'inbox' if skip == 'ladder' and row['spec'] == oldest['spec']
                                 else ladder(evs, row, now)):
                self.run_queue(due, events + extra)
            self.assertEqual([call[5] for call in self.calls], ['L-spec-9004', 'L-spec-9005'], skip)

    def test_enqueues_keep_order_and_are_uncapped(self):
        rows, events = self.queue_fixture()
        enqueues = [dict(row, rule='enqueue') for row in rows]
        for row in enqueues:
            events.append(self.event('merge-gate-clean', 1, subject=row['spec'], project='fixture',
                                     branch=row['spec'].lower()))
        mixed = [row for pair in zip(rows, enqueues) for row in pair]
        out = self.run_queue(mixed, events)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([line for line in out if line.startswith('enqueued ')],
                         [f"enqueued {row['spec']}" for row in enqueues])
        self.assertEqual(len([e for e in self.landed() if e['type'] == 'queue-add']), 5)


if __name__ == '__main__':
    unittest.main()
