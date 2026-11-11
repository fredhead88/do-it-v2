#!/usr/bin/env python3
"""Known-red dispatch guard and dispatch-verdict receipts (L-spec-2323).

Hermetic: real git in temp repos, an isolated DOIT_ROOT/ledger per fixture, production
`base_red.classify` over a seeded local base snapshot, and spies on every dispatch seam.
No network, no GitHub call, no paid dispatch.
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import test_window_rehearse as rehearse_tests  # sets an isolated DOIT_ROOT before fold is used
import base_red
import fold
import window_gate as wg
import window_known_red as wk

NODE = rehearse_tests.NODE
FLAKE = rehearse_tests.FLAKE
SRC = str(Path(__file__).resolve().parent)
VERDICT = 'window-dispatch-verdict'
LATER = datetime(2026, 11, 9, tzinfo=timezone.utc)
P = 'albert-scott'
GATE_PLAN = dict(drop={}, proven={}, environment=[], unresolved=[], mode='singles')


def sha(c):
    return c * 40


def at(minutes):
    return (LATER + timedelta(minutes=minutes)).isoformat()


def verdict(window, s, tree, state, *, failing=(), new=(), minutes=0, required=('pytest',), ref=None, **extra):
    return dict(type=VERDICT, subject=window, project=P, window=window, sha=s, tree=tree, base_sha=sha('b'),
                state=state, required=sorted(required), failing_ids=sorted(failing), new_ids=sorted(new),
                pre_existing=[], flakes=[], quarantined=[], completed_at=at(minutes),
                source_ref=ref or f'{window}:{s[:4]}:{minutes}', **extra)


def candidate(window, s, specs):
    return dict(type='window-candidate', subject=window, sha=s, specs=list(specs))


def read_by_fresh_process(root, *types):
    """Production fold.read_events in a separate interpreter, ledger cache off."""
    code = ("import json, sys, fold; print(json.dumps([e for e in fold.read_events(use_cache=False) "
            "if e.get('type') in sys.argv[1:]]))")
    out = subprocess.run([sys.executable, '-c', code, *types], capture_output=True, text=True, check=True,
                         env={**os.environ, 'DOIT_ROOT': str(root), 'PYTHONPATH': SRC})
    return json.loads(out.stdout)


class KnownRed(unittest.TestCase):
    # ---- fixtures -------------------------------------------------------------
    def fixture(self):
        rh = rehearse_tests.Rehearsal('test_rehearsal_calls_verify_in_rehearsal_mode')
        rh.setUp()
        self.addCleanup(rh.doCleanups)
        return rh

    def seed_base(self, rh, ids, base=None):
        """A complete local master snapshot: classify answers offline."""
        base = base or rh.base
        directory = fold.ROOT / 'base-red' / P
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (base + '.json')).write_text(json.dumps(dict(
            sha=base, project=P, complete=True, undetermined=[], runs_seen=[],
            ids={k: (sorted(ids) if k == 'pytest' else []) for k in base_red.KINDS},
            workflows=sorted(base_red.WORKFLOWS), kinds_covered=list(base_red.KINDS))))

    def red_rehearsal(self, rh, ident='rh-red', ids=(NODE,), rewrite=None):
        out = ctx = contextlib.ExitStack()
        with out:
            if rewrite:
                out.enter_context(self.rewritten(rewrite))
            code = rh.rehearse(dict(state='red', nodes=list(ids), url='', doc={}),
                               probe={'states': {i: 'red' for i in ids}}, attribute=GATE_PLAN,
                               args=rh.args(ident=ident, base=rh.base))[0]
        self.assertEqual(code, 1)

    @contextlib.contextmanager
    def rewritten(self, change):
        real = wg.ev

        def ev(typ, subject, **kv):
            if typ == VERDICT:
                change(kv)
            return real(typ, subject, **kv)
        with patch.object(wg, 'ev', side_effect=ev):
            yield

    @contextlib.contextmanager
    def spies(self, classify=None):
        """Every way to start paid work records itself; the child is an undetermined gate."""
        launched, popen, git = [], subprocess.Popen, wg.git

        def local_git(repo, *args, **kwargs):
            if args and args[0] == 'push':
                launched.append(('push', args))
                return subprocess.CompletedProcess(args, 0, '', '')
            return git(repo, *args, **kwargs)

        def launch(args, **kwargs):
            if isinstance(args, list) and args[:1] == ['bash'] and str(args[1]).endswith(wg.PREDEPLOY):
                launched.append(('popen', args))
                return Mock(pid=987654, returncode=2, args=args, communicate=Mock(return_value=('', None)))
            return popen(args, **kwargs)
        with patch.object(wg, 'git', side_effect=local_git), \
             patch.object(wg.subprocess, 'Popen', side_effect=launch), \
             patch.object(wg.os, 'killpg', side_effect=ProcessLookupError):
            if classify is None:
                yield launched
            else:
                with patch.object(base_red, 'classify', side_effect=classify):
                    yield launched

    def next_rehearsal(self, rh, ident, **kw):
        with self.spies(**kw) as launched, patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('base')), \
             patch.object(wg, 'base_pg', side_effect=AssertionError('base pg')), \
             patch('window_attribution.probe', return_value={'states': {}}), \
             patch.object(wg, 'targeted_attribution', return_value={}):
            code = wg.main_(rh.args(ident=ident, base=rh.base))
        return code, launched

    def next_window(self, rh, ident, **kw):
        with self.spies(**kw) as launched, patch.object(wg, 'base_red_snapshot', side_effect=AssertionError('base')), \
             patch.object(wg, 'base_pg', side_effect=AssertionError('base pg')):
            code = wg.main_([ident, '--project', P, '--main', 'master', '--specs', 'A:A,B:B'])
        return code, launched

    def count(self, kind, **match):
        return [e for e in fold.read_events() if e['type'] == kind and all(e.get(k) == v for k, v in match.items())]

    # ---- AC1 ------------------------------------------------------------------
    def test_ac1_quick_ids(self):
        t = {n: sha(n) for n in '1234567'}
        events = [
            candidate('w1', sha('a'), ['A', 'B']),
            verdict('w1', sha('a'), t['1'], 'red', failing=['x1', 'x2', 'x3'], new=['x2', 'x3'], minutes=10),
            # A again, later completion: only this one contributes for A. Duplicate id x3.
            candidate('w2', sha('c'), ['A']),
            verdict('w2', sha('c'), t['2'], 'red', failing=['x3', 'x4'], new=['x3', 'x4'], minutes=20),
            # newer unfinished candidate for A: no verdict, so no effect
            candidate('w3', sha('d'), ['A']),
            dict(type='window-tree-verified', subject='w3', sha=sha('d'), tree=t['3'], green=False, new_ids=['u1']),
            # unrelated spec C
            candidate('w4', sha('e'), ['C']),
            verdict('w4', sha('e'), t['4'], 'red', failing=['z1'], new=['z1'], minutes=5),
            # one window id, two candidates with different shas: membership follows its receipt
            candidate('wh', sha('f'), ['D', 'E']),
            verdict('wh', sha('f'), t['5'], 'red', failing=['h1', 'h2'], new=['h1'], minutes=30),
            candidate('wh', sha('9'), ['E']),
            verdict('wh', sha('9'), t['6'], 'green', failing=['h2'], minutes=40),
            # not completions: unnamed failure, incomplete base, raw red without a verdict
            candidate('wu', sha('8'), ['F']),
            verdict('wu', sha('8'), t['7'], 'red', failing=[], new=[], minutes=50),
            candidate('wi', sha('7'), ['G']),
            verdict('wi', sha('7'), sha('0'), 'undetermined', failing=['g1'], minutes=50),
            candidate('wr', sha('6'), ['H']),
            dict(type='window-tree-verified', subject='wr', sha=sha('6'), tree=sha('5'), green=False, new_ids=['r1']),
            # mixed flake / pre-existing / new: only the new ids
            candidate('wm', sha('5'), ['I']),
            dict(verdict('wm', sha('5'), sha('4'), 'red', failing=['m1', 'm2', 'm3', 'm4'], new=['m4'], minutes=60),
                 flakes=['m1'], pre_existing=['m2'], quarantined=['m3']),
        ]
        spies = [patch.object(base_red, 'classify', side_effect=AssertionError('classify')),
                 patch.object(wg, 'verify', side_effect=AssertionError('verify')),
                 patch.object(wg.window_dispatch, 'verify', side_effect=AssertionError('dispatch')),
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('network')),
                 patch.object(wg.subprocess, 'Popen', side_effect=AssertionError('process'))]
        with contextlib.ExitStack() as stack:
            for spy in spies:
                stack.enter_context(spy)
            self.assertEqual(wk.quick_ids(events, ['A']), ['x3', 'x4'])
            self.assertEqual(wk.quick_ids(events, ['B']), ['x2', 'x3'])
            self.assertEqual(wk.quick_ids(events, ['A', 'B']), ['x2', 'x3', 'x4'])
            self.assertEqual(wk.quick_ids(events, ['C']), ['z1'])
            self.assertEqual(wk.quick_ids(events, ['A', 'B', 'C']), ['x2', 'x3', 'x4', 'z1'])
            self.assertEqual(wk.quick_ids(events, ['D']), ['h1'])     # still red for D on the first sha
            self.assertEqual(wk.quick_ids(events, ['E']), [])         # a later green candidate supersedes
            self.assertEqual(wk.quick_ids(events, ['F', 'G', 'H']), [])
            self.assertEqual(wk.quick_ids(events, ['I']), ['m4'])
            self.assertEqual(wk.quick_ids(events, ['nobody']), [])
            self.assertEqual(wk.quick_ids(list(reversed(events)), ['A', 'B', 'C']), ['x2', 'x3', 'x4', 'z1'])
            self.assertEqual(wg.quick_ids(events, ['A', 'B']), ['x2', 'x3', 'x4'])
            self.assertEqual(wk.quick_ids(events, 'A'), [])
            self.assertEqual(wk.quick_ids(events, [None, 3]), [])
            clean = wk.diagnostics(events)
            self.assertEqual(clean, dict(missing_green=0, invalid_new_ids=0, missing_membership=0,
                                         ambiguous_membership=0, conflicting_receipts=0,
                                         unproven_completion=3))  # w3 and wr raw reds, wu unnamed failure
            noisy = events + [
                dict(type='window-tree-verified', subject='n1', sha=sha('1'), tree=sha('1')),          # no green flag
                dict(type='window-tree-verified', subject='n2', sha=sha('2'), tree=sha('2'), green=False,
                     new_ids='not-a-list'),
                dict(verdict('n3', sha('3'), sha('3'), 'red', failing=['q'], new=['q'], minutes=70),
                     new_ids='not-a-list'),
                verdict('n4', sha('4'), sha('4'), 'red', failing=['q'], new=['q'], minutes=70),         # no membership
                candidate('n5', sha('5'), ['J']), candidate('n5', sha('5'), ['J', 'K']),
                verdict('n5', sha('5'), sha('6'), 'red', failing=['q'], new=['q'], minutes=70),         # ambiguous
                candidate('n6', sha('6'), ['L']),
                verdict('n6', sha('6'), sha('7'), 'red', failing=['q'], new=['q'], minutes=70),
                verdict('n6', sha('6'), sha('7'), 'red', failing=['q', 'r'], new=['q', 'r'], minutes=71),  # conflict
            ]
            self.assertEqual(wk.diagnostics(noisy), dict(missing_green=1, invalid_new_ids=2, missing_membership=1,
                                                         ambiguous_membership=1, conflicting_receipts=1,
                                                         unproven_completion=3))
            self.assertEqual(wk.quick_ids(noisy, ['J', 'K', 'L']), [])

    # ---- AC2 ------------------------------------------------------------------
    def check_receipts(self, rh, expected):
        rows = read_by_fresh_process(rh.root, VERDICT, 'window-tree-verified')
        verdicts = [e for e in rows if e['type'] == VERDICT]
        self.assertEqual(len(verdicts), 1, verdicts)
        row = verdicts[0]
        for key, value in expected.items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(sorted(row), sorted(['type', 'subject', 'project', 'window', 'sha', 'tree', 'base_sha', 'state',
                                              'required', 'failing_ids', 'new_ids', 'pre_existing', 'flakes',
                                              'quarantined', 'completed_at', 'source_ref', 'v', 'ts', 'actor', '_src']))
        for key in ('failing_ids', 'new_ids', 'pre_existing', 'flakes', 'quarantined', 'required'):
            self.assertEqual(row[key], sorted(set(row[key])))
        self.assertTrue(wk._verdict(row) is not None or row['state'] == 'red' and not row['new_ids'])
        for tree_row in (e for e in rows if e['type'] == 'window-tree-verified'):
            self.assertFalse({'completed_at', 'source_ref', 'failing_ids', 'state'} & set(tree_row))
        return row

    def test_ac2_verdict_roundtrip(self):
        unresolved = dict(drop={}, proven={}, environment=[], unresolved=[NODE], mode='singles')
        # production rehearsal: _run -> verify seam -> diagnose -> receipt
        cases = [
            ('green', dict(state='green', nodes=[], url='', doc={}), {}, GATE_PLAN,
             dict(state='green', failing_ids=[], new_ids=[], flakes=[])),
            ('named red', dict(state='red', nodes=[NODE, FLAKE], url='', doc={}),
             {'states': {NODE: 'red', FLAKE: 'green'}}, GATE_PLAN,
             dict(state='red', failing_ids=sorted([NODE, FLAKE]), new_ids=[NODE], flakes=[FLAKE])),
            ('flake only', dict(state='red', nodes=[FLAKE], url='', doc={}), {'states': {FLAKE: 'green'}}, GATE_PLAN,
             dict(state='green', failing_ids=[FLAKE], new_ids=[], flakes=[FLAKE])),
            ('early undetermined', dict(state='undetermined', nodes=[], url='', doc={}, why='stopped early'), {},
             GATE_PLAN, dict(state='undetermined', failing_ids=[], new_ids=[])),
            ('unresolved red', dict(state='red', nodes=[NODE], url='', doc={}), {'states': {}}, unresolved,
             dict(state='undetermined', new_ids=[])),
        ]
        for name, result, probe, plan, expected in cases:
            with self.subTest(path='rehearsal', case=name):
                rh = self.fixture()
                code = rh.rehearse(result, probe=probe, attribute=plan, args=rh.args(ident='rh-' + name.split()[0],
                                                                                    base=rh.base))[0]
                self.assertIn(code, (0, 1, 2))
                row = self.check_receipts(rh, dict(expected, project=P, base_sha=rh.base,
                                                   window='rh-' + name.split()[0]))
                self.assertEqual(row['tree'], wg.tree_of(rh.repo, row['sha']))
        # production judging through _run_attempt
        known = {NODE}

        def classify(project, base, ids, **kw):
            return dict(complete=True, undetermined=False, covered=list(base_red.KINDS), uncovered=[], needed=[],
                        pre_existing=[i for i in ids if i in known], new=[i for i in ids if i not in known])
        ordinary = [
            ('green', dict(state='green', nodes=[], url='', doc={}), {}, {},
             dict(state='green', failing_ids=[], new_ids=[])),
            ('named red', dict(state='red', nodes=[FLAKE], url='', doc={}), {'states': {FLAKE: 'red'}}, {},
             dict(state='red', failing_ids=[FLAKE], new_ids=[FLAKE], pre_existing=[])),
            ('pre-existing only', dict(state='red', nodes=[NODE], url='', doc={}), {'states': {NODE: 'red'}}, {},
             dict(state='green', failing_ids=[NODE], new_ids=[], pre_existing=[NODE], flakes=[])),
            ('flake only', dict(state='red', nodes=[FLAKE], url='', doc={}), {'states': {FLAKE: 'green'}}, {},
             dict(state='green', failing_ids=[FLAKE], new_ids=[], flakes=[FLAKE])),
            ('early undetermined', dict(state='undetermined', nodes=[], url='', doc={}, why='stopped early'), {}, {},
             dict(state='undetermined', failing_ids=[], new_ids=[])),
        ]
        for name, result, probe, _, expected in ordinary:
            with self.subTest(path='window', case=name):
                rh = self.fixture()
                window = 'win-' + name.split()[0]
                with patch.object(wg, 'verify', side_effect=lambda repo, cand, req, w, **k: (
                         dict(state='green', nodes=[], url='', doc={}) if cand == rh.base else result)), \
                     patch.object(wg, 'base_red_snapshot', return_value={'complete': True}), \
                     patch('window_attribution.probe', return_value=probe or {'states': {}}), \
                     patch.object(base_red, 'classify', side_effect=classify), \
                     patch.object(base_red, 'recheck', return_value={}), \
                     patch.object(wg, 'targeted_attribution', return_value=dict(
                         drop={}, proven={}, environment=[], unresolved=[NODE], mode='singles', base_sha=rh.base)), \
                     contextlib.redirect_stdout(open(os.devnull, 'w')):
                    wg.main_([window, '--project', P, '--main', 'master', '--specs', 'A:A,B:B'])
                row = self.check_receipts(rh, dict(expected, project=P, window=window))
                self.assertEqual(row['base_sha'], rh.base)
                self.assertEqual(row['tree'], wg.tree_of(rh.repo, row['sha']))

    # ---- AC3 ------------------------------------------------------------------
    def assert_skip(self, rh, code, launched, ident, *, kind):
        self.assertEqual(code, 2)
        self.assertEqual(launched, [])
        skips = self.count('window-skipped-known')
        self.assertEqual(len(skips), 1)
        skip = skips[0]
        self.assertEqual(skip['subject'], ident)
        self.assertEqual(skip['reason'], 'unchanged-tree-master-known-red')
        self.assertEqual((skip['ids'], skip['specs'], skip['project']), ([NODE], ['A', 'B'], P))
        self.assertEqual((skip['base_sha'], skip['tree'] == skip['source_tree']), (rh.base, True))
        self.assertEqual(skip['source_subject'], 'rh-red')
        self.assertTrue(skip['source_ts'])
        self.assertEqual(self.count(VERDICT, subject=ident), [])       # a suppression is no completion
        self.assertEqual([e for e in fold.read_events() if e['type'] in ('shipped', 'window-verified')], [])
        self.assertEqual(rh.git('rev-parse', 'master'), rh.base)       # master unmoved
        self.assertFalse(any(e.get('green') is True for e in fold.read_events() if e['type'] == 'window-tree-verified'))

    def test_ac3_skip_decision(self):
        # positive, rehearsal path
        rh = self.fixture()
        self.red_rehearsal(rh)
        self.seed_base(rh, [NODE])
        before = len(fold.read_events())
        code, launched = self.next_rehearsal(rh, 'rh-again')
        self.assert_skip(rh, code, launched, 'rh-again', kind='rehearsal')
        row = self.count('window-rehearsed', subject='rh-again')[-1]
        self.assertEqual(row['state'], 'undetermined')
        self.assertIn('known-red', row['why'])
        self.assertGreater(len(fold.read_events()), before)
        # positive, ordinary path: also before any baseline ensure (base_red_snapshot / base_pg raise if touched)
        rh = self.fixture()
        self.red_rehearsal(rh)
        self.seed_base(rh, [NODE])
        with contextlib.redirect_stdout(open(os.devnull, 'w')):
            code, launched = self.next_window(rh, 'win-again')
        self.assert_skip(rh, code, launched, 'win-again', kind='window')
        # the verify seam alone also refuses to launch (the guard sits before the push)
        rh = self.fixture()
        self.red_rehearsal(rh)
        self.seed_base(rh, [NODE])
        source = self.count(VERDICT)[-1]
        cand = source['sha']
        with self.spies() as launched, wg.window_dispatch.managed(
                source['tree'], known=dict(project=P, base_sha=rh.base, specs=['A', 'B'])):
            result = wg.verify(rh.repo, cand, source['required'], 'seam-1')
        self.assertEqual((result['state'], launched), ('undetermined', []))
        self.assertEqual(len(self.count('window-skipped-known')), 1)

        # negative cases each dispatch normally and emit no skip
        def edit(**fields):
            return lambda kv: kv.update(fields)

        def drop(name):
            return lambda kv: kv.pop(name)
        classify_error = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('classify unavailable'))
        negatives = {
            'new failing id': dict(base=[]),
            'errored classification': dict(base=[NODE], classify=classify_error),
            'incomplete classification': dict(base=None),
            'missing field': dict(base=[NODE], rewrite=drop('base_sha')),
            'empty ids': dict(base=[NODE], rewrite=edit(new_ids=[], failing_ids=[])),
            'absent membership': dict(base=[NODE], rewrite=edit(sha=sha('a'))),
            'partial required set': dict(base=[NODE], rewrite=edit(required=[wg.PYTEST_CHECK])),
        }
        for name, case in negatives.items():
            with self.subTest(case=name):
                rh = self.fixture()
                self.red_rehearsal(rh, rewrite=case.get('rewrite'))
                if case['base'] is not None:
                    self.seed_base(rh, case['base'])
                code, launched = self.next_rehearsal(rh, 'rh-neg', classify=case.get('classify'))
                self.assertEqual(code, 2)
                self.assertGreaterEqual(len(launched), 1)
                self.assertEqual(self.count('window-skipped-known'), [])
        with self.subTest(case='ambiguous membership'):
            rh = self.fixture()
            self.red_rehearsal(rh)
            self.seed_base(rh, [NODE])
            sha_ = self.count(VERDICT)[-1]['sha']
            wg.ev('window-candidate', 'rh-red', sha=sha_, specs=['A'], verified_on=rh.base)
            code, launched = self.next_rehearsal(rh, 'rh-neg')
            self.assertGreaterEqual(len(launched), 1)
            self.assertEqual(self.count('window-skipped-known'), [])
        with self.subTest(case='changed tree'):
            rh = self.fixture()
            self.red_rehearsal(rh)
            self.seed_base(rh, [NODE])
            with self.spies() as launched, patch.object(wg, 'base_red_snapshot', side_effect=AssertionError), \
                 patch.object(wg, 'base_pg', side_effect=AssertionError):
                wg.main_(rh.args(ident='rh-other', base=rh.base, specs=['A']))
            self.assertGreaterEqual(len(launched), 1)
            self.assertEqual(self.count('window-skipped-known'), [])
        with self.subTest(case='newer fresh covered green'):
            rh = self.fixture()
            self.red_rehearsal(rh)
            self.seed_base(rh, [NODE])
            source = self.count(VERDICT)[-1]
            wg.ev('window-tree-verified', 'rh-green', window='rh-green', sha=source['sha'], tree=source['tree'],
                  green=True, required=source['required'], new_ids=[], rehearsal=True, base_sha=rh.base, project=P)
            code, launched = self.next_rehearsal(rh, 'rh-reuse')
            self.assertEqual((code, launched), (0, []))                # the existing green-reuse path
            self.assertEqual(self.count('window-skipped-known'), [])

    # ---- AC4 ------------------------------------------------------------------
    def test_ac4_dedup_escalation(self):
        rh = self.fixture()
        self.red_rehearsal(rh)
        self.seed_base(rh, [NODE])
        code, launched = self.next_rehearsal(rh, 'rh-1')
        self.assertEqual((code, launched), (2, []))
        code, launched = self.next_rehearsal(rh, 'rh-2')                          # second window id
        self.assertEqual((code, launched), (2, []))
        with contextlib.redirect_stdout(open(os.devnull, 'w')):
            code, launched = self.next_window(rh, 'win-3')                        # ordinary path, same key
        self.assertEqual((code, launched), (2, []))

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=2)
        with patch.object(wk, 'datetime', Later):                                  # after the deadline
            code, launched = self.next_rehearsal(rh, 'rh-4')
        self.assertEqual((code, launched), (2, []))
        # a fresh process, through production readers, sees exactly one pair
        skips = read_by_fresh_process(rh.root, 'window-skipped-known')
        blocks = [e for e in read_by_fresh_process(rh.root, 'escalation-blocking') if e.get('known_red_key')]
        self.assertEqual((len(skips), len(blocks)), (1, 1))
        self.assertEqual(skips[0]['subject'], 'rh-1')
        block = blocks[0]
        self.assertEqual(block['default'], wk.DEFAULT)
        self.assertFalse(block['default'].startswith('after 30 minutes drop'))
        self.assertEqual(block['known_red_key'], skips[0]['known_red_key'])
        first = datetime.fromisoformat(skips[0]['ts'])
        due = datetime.fromisoformat(block['deadline'])
        self.assertLessEqual(abs((due - first - timedelta(minutes=30)).total_seconds()), 2)
        self.assertEqual([e for e in fold.read_events() if e['type'] == 'window-dropped'], [])
        # changed base: the same tree is re-evaluated and gets its own pair
        other = rh.git('rev-parse', 'future')
        self.seed_base(rh, [NODE], base=other)
        source = self.count(VERDICT)[-1]
        with self.spies() as launched:
            result = wk.guard(wg, rh.repo, source['sha'], source['tree'], source['required'], 'rh-5',
                              project=P, base_sha=other, specs=['A', 'B'])
        self.assertEqual(result['state'], 'undetermined')
        self.assertEqual(launched, [])
        self.assertEqual(len(read_by_fresh_process(rh.root, 'window-skipped-known')), 2)
        self.assertEqual(len([e for e in read_by_fresh_process(rh.root, 'escalation-blocking')
                              if e.get('known_red_key')]), 2)
        # an operator resolution lifts the suppression for that key
        key = skips[0]['known_red_key']
        wg.ev('unblocked', 'rh-1', why='operator resolved')
        code, launched = self.next_rehearsal(rh, 'rh-6')
        self.assertGreaterEqual(len(launched), 1)
        self.assertEqual(len([e for e in self.count('window-skipped-known') if e['known_red_key'] == key]), 1)

    # ---- AC5 ------------------------------------------------------------------
    def window_verdicts(self, rh, window, result, *, probe=None, known=()):
        def classify(project, base, ids, **kw):
            return dict(complete=True, undetermined=False, covered=list(base_red.KINDS), uncovered=[], needed=[],
                        pre_existing=[i for i in ids if i in known], new=[i for i in ids if i not in known])
        with patch.object(wg, 'verify', side_effect=lambda repo, cand, req, w, **k: (
                 dict(state='green', nodes=[], url='', doc={}) if cand == rh.base else result)), \
             patch.object(wg, 'base_red_snapshot', return_value={'complete': True}), \
             patch('window_attribution.probe', return_value=probe or {'states': {}}), \
             patch.object(base_red, 'classify', side_effect=classify), patch.object(base_red, 'recheck', return_value={}), \
             patch.object(wg, 'targeted_attribution', return_value=dict(
                 drop={}, proven={}, environment=[], unresolved=[NODE], mode='singles', base_sha=rh.base)), \
             contextlib.redirect_stdout(open(os.devnull, 'w')):
            code = wg.main_([window, '--project', P, '--main', 'master', '--specs', 'A:A,B:B'])
        return code, self.count(VERDICT, subject=window)

    def test_ac5_fail_closed(self):
        # a named new early-stop failure stays red with those ids
        rh = self.fixture()
        code, rows = self.window_verdicts(rh, 'w-named', dict(state='red', nodes=[NODE], url='', doc={}),
                                          probe={'states': {NODE: 'red'}})
        self.assertEqual((len(rows), rows[0]['state'], rows[0]['new_ids'], rows[0]['failing_ids']),
                         (1, 'red', [NODE], [NODE]))
        # an unnamed early stop is undetermined, never red or green
        rh = self.fixture()
        code, rows = self.window_verdicts(rh, 'w-unnamed', dict(state='red', nodes=[], url='', doc={}))
        self.assertEqual((code, len(rows), rows[0]['state'], rows[0]['new_ids']), (2, 1, 'undetermined', []))
        # known-red-only failure: no new-failure stop; the evidenced pre-existing-to-green policy applies
        rh = self.fixture()
        code, rows = self.window_verdicts(rh, 'w-known', dict(state='red', nodes=[NODE], url='', doc={}),
                                          probe={'states': {NODE: 'red'}}, known=[NODE])
        self.assertEqual((rows[0]['state'], rows[0]['new_ids'], rows[0]['pre_existing']), ('green', [], [NODE]))
        self.assertEqual(code, 0)
        # an exception inside a guard that is making a positive decision: undetermined, no skip, no dispatch
        rh = self.fixture()
        self.red_rehearsal(rh)
        self.seed_base(rh, [NODE])
        real = wg.ev

        def failing_ev(typ, subject, **kv):
            if typ == 'window-skipped-known':
                raise OSError('ledger unavailable')
            return real(typ, subject, **kv)
        with patch.object(wg, 'ev', side_effect=failing_ev):
            code, launched = self.next_rehearsal(rh, 'rh-fault')
        self.assertEqual((code, launched), (2, []))
        self.assertEqual(self.count('window-skipped-known'), [])
        self.assertEqual(self.count('window-rehearsed', subject='rh-fault')[-1]['state'], 'undetermined')
        # a guard that cannot even read history dispatches ordinarily; so does absent history
        rh = self.fixture()
        code, launched = self.next_rehearsal(rh, 'rh-fresh')
        self.assertEqual(code, 2)
        self.assertGreaterEqual(len(launched), 1)
        with patch.object(fold, 'read_events', side_effect=OSError('unreadable')):
            self.assertIsNone(wk.guard(wg, rh.repo, sha('a'), sha('b'), ['pytest'], 'w', project=P,
                                       base_sha=rh.base, specs=['A']))
        # a skipped required check never counts as ran
        rh = self.fixture()
        out = json.dumps(dict(verdict='green', checks={c: ('ran' if c != wg.PG_CHECK else dict(state='skipped'))
                                                       for c in rehearse_tests.CHECKS}, new_failures=[]))
        process = Mock(pid=987654, returncode=0, args=['x'], communicate=Mock(return_value=(out, None)))
        tree = wg.tree_of(rh.repo, rh.base)
        with patch.object(wg, 'git', side_effect=lambda repo, *a, **k: subprocess.CompletedProcess(a, 0, '', '')
                          if a[:1] == ('push',) else subprocess.run(['git', '-C', str(repo), *a], capture_output=True,
                                                                    text=True)), \
             patch.object(wg.subprocess, 'Popen', return_value=process):
            result = wg.window_dispatch.verify(wg, rh.repo, rh.base, rehearse_tests.CHECKS, 'w-skipped', tree=tree)
        self.assertNotEqual(result['state'], 'green')
        # window_gate stays within its line cap
        self.assertLessEqual(len((Path(wg.__file__)).read_text().splitlines()), 2099)


if __name__ == '__main__':
    unittest.main()
