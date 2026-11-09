#!/usr/bin/env python3
"""The tick owns the do-it-v2 merge-direct Step: owner, one merge per pass, refusals, failure item."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_ISO = tempfile.TemporaryDirectory(prefix='merge-direct-tick-import-')
os.environ['DOIT_ROOT'] = _ISO.name
os.environ['DOIT_NO_POKE'] = '1'
os.environ.pop('DOIT_PROJECT', None)
sys.path.insert(0, str(Path(__file__).parent))
import direct_merge, dispatch, empty_grade, fold, lane_rules, nextstep, tick  # noqa: E402

NOW = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)


def receipt(case, body):
    payload = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url=f'cli://merge-direct-tick/{case}', status=0,
                          body_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                          body_excerpt=payload)))


def queued(sid, project='do-it-v2', age=180):
    return dict(v=1, type='queue-add', subject=sid, project=project, branch=sid.lower(),
                ts=(NOW - timedelta(minutes=age)).isoformat())


class OwnerTests(unittest.TestCase):
    def test_owner_is_tick_and_albert_scott_unchanged(self):
        events = [dict(queued('L-spec-9001'), actor='executor', _src='L-executor-1.jsonl:1'),
                  dict(queued('L-spec-9002', 'albert-scott'), actor='executor', _src='L-executor-1.jsonl:2')]
        specs = {e['subject']: dict(state='reviewing', evs=[e]) for e in events}
        own = {s.item: s for s in nextstep.next_steps(events, specs, NOW, lane_rules.load())}
        direct = own['L-spec-9001']
        self.assertEqual((direct.action, direct.owner), ('merge-direct', 'tick'))
        self.assertTrue(direct.reason.startswith('Tick merge-direct'))
        self.assertEqual((own['L-spec-9002'].action, own['L-spec-9002'].owner), ('merge', 'tick'))
        self.assertEqual(nextstep.ACTIONS['merge-direct'], 'tick')
        self.assertIn('merge-direct', tick.LAUNCH_ACTIONS)
        receipt('AC1', dict(owner=direct.owner, actions=nextstep.ACTIONS['merge-direct']))


class PassTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                   (dispatch, 'ROOT', self.root), (dispatch, 'EVENTS', self.root / 'events'),
                                   (dispatch, 'CONTENT', self.root / 'content')):
            swap = patch.object(module, key, value)
            swap.start()
            self.addCleanup(swap.stop)
        (self.root / 'events').mkdir()
        (self.root / 'content').mkdir()
        self.repo = self.root / 'repos' / 'do-it-v2'
        self.repo.mkdir(parents=True)
        self.git('init', '-b', 'main')
        for key, value in (('user.email', 'f@example.invalid'), ('user.name', 'Fixture'),
                           ('commit.gpgsign', 'false'), ('merge.gpgsign', 'false')):
            self.git('config', key, value)
        self.write('shared', 'base\n')
        self.commit('base')
        self.specs = ['L-spec-9001', 'L-spec-9002']
        for sid in self.specs:
            self.git('switch', '-c', sid.lower(), 'main')
            self.write(sid.lower() + '.txt', 'x\n')
            self.commit(f'{sid} tip')
            self.git('switch', 'main')
        self.gate_calls = []
        self.gate = lambda branch, main, spec: (self.gate_calls.append(spec) or (0, ''))
        patcher = patch.object(direct_merge, 'cli_gate', lambda repo: (lambda *a: self.gate(*a)))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / 'events' / 'L-executor-0001.jsonl').write_text(
            ''.join(json.dumps(queued(sid, age=200 - i)) + '\n' for i, sid in enumerate(self.specs)))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, check=True).stdout.decode()

    def write(self, name, body):
        (self.repo / name).write_text(body)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)

    def steps(self, events):
        specs = {e['subject']: dict(state='reviewing', evs=[e]) for e in events if e['type'] == 'queue-add'}
        return [s for s in nextstep.next_steps(events, specs, NOW, lane_rules.load()) if s.action == 'merge-direct']

    def run_pass(self, **kw):
        events = fold.read_events(use_cache=False)
        return tick.run_steps(events, {}, NOW, steps=self.steps(events), **kw)

    def ledger(self, type_):
        return [e for e in fold.read_events(use_cache=False) if e.get('type') == type_]

    def merges(self):
        return self.git('log', '--merges', '--format=%s').splitlines()

    def test_one_merge_per_pass(self):
        first = self.run_pass()
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual([e['subject'] for e in self.ledger('shipped')], ['L-spec-9001'])
        self.assertTrue(any(l.startswith('acted L-spec-9001 merge-direct shipped') for l in first), first)
        self.assertIn('merge-wait L-spec-9002', first)
        second = self.run_pass()
        self.assertEqual(len(self.merges()), 2)
        self.assertEqual([e['subject'] for e in self.ledger('shipped')], ['L-spec-9001', 'L-spec-9002'])
        self.assertEqual({e['actor'] for e in self.ledger('shipped')}, {'tick'})
        receipt('AC2-one-per-pass', dict(first=first, second=second, merges=self.merges()))

    def test_regate_dirty_and_dry_run_merge_nothing(self):
        def move_main(branch, main, spec):
            self.write('other.txt', 'moved\n')
            self.commit('main moved')
            return 0, ''
        self.gate = move_main
        out = self.run_pass()
        self.assertIn('hold L-spec-9001: merge-direct regate', out)
        self.assertEqual(self.merges(), [])
        self.gate = lambda *a: (0, '')
        self.git('switch', '-c', 'l-spec-9003', 'main')
        self.write('shared', 'branch\n')
        self.commit('touch shared')
        self.git('switch', 'main')
        self.write('shared', 'dirty\n')
        with (self.root / 'events' / 'L-executor-0001.jsonl').open('a') as fh:
            fh.write(json.dumps(queued('L-spec-9003', age=300)) + '\n')
        out_dirty = self.run_pass()
        self.assertIn('hold L-spec-9003: merge-direct refused-dirty', out_dirty)
        self.assertEqual([m for m in self.merges() if '9003' in m], [])
        before, shipped_before = self.merges(), self.ledger('shipped')
        dry = self.run_pass(dry_run=True)
        self.assertEqual(self.merges(), before)
        self.assertTrue(any(l.startswith('would act L-spec-') and l.endswith('merge-direct') for l in dry), dry)
        self.assertEqual(self.ledger('shipped'), shipped_before)
        receipt('AC2-refusals', dict(regate=out, dirty=out_dirty, dry=dry))

    def test_failure_after_merge_records_one_problem(self):
        real = empty_grade.complete

        def boom(step, events, **kw):
            if step.item == 'L-spec-9002' or step.item == 'L-spec-9003':
                raise RuntimeError('injected after merge')
            return real(step, events, **kw)
        with patch.object(empty_grade, 'complete', boom):
            out = self.run_pass()
            self.assertTrue(any('failed L-spec-9002' in l for l in out), out)
            first = self.ledger('problem-occurred')
            self.assertEqual(len(first), 1)
            self.assertEqual((first[0]['subject'], first[0]['problem']),
                             ('L-spec-9001', 'merge-direct-pass-failed'))
            self.assertEqual(first[0]['sha'], self.ledger('shipped')[0]['sha'])
            self.assertEqual(first[0]['actor'], 'tick')
            # Second pass: the already-reported spec is not reported again.
            tick._report_merge_pass_failure([('L-spec-9001', first[0]['sha'])], RuntimeError('again'))
            self.assertEqual(len(self.ledger('problem-occurred')), 1)
        receipt('AC3', dict(problem_events=len(self.ledger('problem-occurred'))))

    def test_no_problem_without_failure(self):
        self.run_pass()
        self.assertEqual(self.ledger('problem-occurred'), [])


if __name__ == '__main__':
    unittest.main()
