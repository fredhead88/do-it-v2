#!/usr/bin/env python3
"""L-spec-2261: several migrations through the versioned window path.

Scratch Git repositories, real rechain.resolve / rechain_check.stack /
migration_order.order and a real file ledger. Only the hosted-CI seam
(window_gate.verify), the Alembic CLI and the private Postgres are doubled; the
fingerprint command rebuilds from the candidate tree, it never returns a canned
digest.  Run one class:  test_multi_migration_window.py AC3Tests
"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_rechain as T  # sets an isolated DOIT_ROOT before any gate import
import dispatch
import fold
import merge_queue as mq
import migration_order
import precheck
import rechain
import rechain_check
import window_gate as wg
import window_migrations as wm
import window_prewindow
import window_rehearse as wr

PROJECT = 'albert-scott'
LEDGER = 'L-executor-0001.jsonl'
GREEN = dict(state='green', nodes=[], url='fixture://ci', doc={})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='multi-migration-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for name in ('events', 'content', 'repos', 'state'):
            (self.root / name).mkdir()
        self.repo = self.root / 'repos' / PROJECT
        self.fx = T.Fixture(self.root, repo=self.repo)
        checks = [wg.PYTEST_CHECK, wg.PG_CHECK, wg.DASHBOARD_CHECK]
        self.fx.write(wg.PREDEPLOY, 'REQUIRED_CHECKS=("' + '" "'.join(checks) + '")\n')
        self.fx.write('scripts/ci/actions_changes_gate.sh', "printf '%s\\n' '" + json.dumps(
            dict(checks=checks, everything=True, unclassified=[], reason='fixture')) + "'\n")
        self.fx.write('scripts/ci/pytest_known_red_quarantine.txt', '# policy\n')
        self.fx.base = self.fx.commit('ci files')
        self.base = self.fx.base
        self.content = self.root / 'content'
        self.now = datetime.now(timezone.utc)
        grading = self.root / 'grading.toml'
        grading.write_text('')
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for obj, key, value in ((fold, 'ROOT', self.root), (fold, 'EVENTS', self.root / 'events'),
                                (dispatch, 'ROOT', self.root), (dispatch, 'CONTENT', self.content),
                                (dispatch, 'EVENTS', self.root / 'events'), (fold, 'NOW', self.now),
                                (dispatch, 'now', lambda: self.now.isoformat())):
            stack.enter_context(patch.object(obj, key, value))
        env = dict(DOIT_ROOT=str(self.root), DOIT_NO_POKE='1', DOIT_LEDGER_FILE=LEDGER,
                   DOIT_GATE_LEDGER_FILE=LEDGER, DOIT_GRADING_TOML=str(grading), DOIT_MAIN='master',
                   DOIT_READ_CACHE='0', DOIT_REHEARSAL_LAUNCHER='on', DOIT_REHEARSAL_BATCHES='3',
                   DOIT_WINDOW_RECHAIN='on')
        env.update(self.fx.scripts(self.root))
        stack.enter_context(patch.dict(os.environ, env))
        os.environ.pop('DOIT_WINDOW_MAX_MERGES', None)
        self.heads = []
        self.alembic = stack.enter_context(patch.object(wr, 'alembic_heads', side_effect=self.single_head))

    # -- fixtures ---------------------------------------------------------
    def single_head(self, repo, candidate, project):
        """The Alembic CLI is external; the graph rule it enforces is checked for real."""
        reader = window_prewindow.Trees(repo)
        graph = window_prewindow.candidate_revisions(reader, reader.read(candidate), True)
        self.heads.append(sorted(graph))

    def git(self, *args):
        return T.git(self.repo, *args)

    def spec(self, number, parent=None, dependency=None, mutation=None):
        name = f'm{number}'
        start = parent or self.base
        entry = self.fx.branch(name, parent=start, mutation=mutation)
        sid = f'L-spec-{number}'
        paths = self.git('diff', '--name-only', start, entry[2]).split()
        text = 'Writes: ' + ', '.join(paths) + '\n'
        if dependency:
            text += f'Dependency-hold: waits_for={dependency}\n'
        (self.content / (sid + '.md')).write_text(text)
        return (sid, entry[1], entry[2])

    def queue(self, entries):
        rows = [dict(v=1, type='queue-add', subject=s, actor='executor', ts=self.now.isoformat(),
                     project=PROJECT, branch=b) for s, b, _ in entries]
        path = self.root / 'events' / LEDGER
        with path.open('a') as stream:
            stream.write(''.join(json.dumps(r) + '\n' for r in rows))

    def ledger(self, *kinds):
        """Fresh-process read of the file ledger, as a separate session would see it."""
        code = ('import json, fold; print(json.dumps(fold.read_events(use_cache=False)))')
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent)))
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = json.loads(out.stdout)
        return [r for r in rows if not kinds or r['type'] in kinds]

    def expected_order(self, entries):
        texts = {s: (self.content / (s + '.md')).read_text() for s, _, _ in entries}
        return migration_order.order([s for s, _, _ in entries], [], texts=texts).ordered

    def prepare(self, entries, record=None):
        return wr.prepare(self.repo, 'w-fixture', self.base, entries, PROJECT, 'master', [], record)

    def chain_specs(self, candidate):
        log = self.git('log', '--first-parent', '--format=%s', f'{self.base}..{candidate}').splitlines()
        return [line.split(')')[0][len('merge('):] for line in reversed(log) if line.startswith('merge(')]

    def refill(self, cap=6, runner=None):
        launches = []
        with patch.object(mq.window_gate, 'required_checks', return_value=['fixture']), \
             patch.object(mq.window_gate, 'scope', return_value=dict(checks=['fixture'])):
            started = mq._refill_rehearsals(PROJECT, runner=runner or launches.append, cap=cap)
        return started, launches

    def window(self, ident, specs, verify=None, **env):
        verify = verify or (lambda *a, **k: dict(GREEN))
        out = io.StringIO()
        with patch.object(wg, 'verify', side_effect=verify) as verifier, \
             patch.object(wg, 'base_red_snapshot', return_value={'complete': True}), \
             patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = wg.main_([ident, '--project', PROJECT, '--main', 'master',
                             '--specs', ','.join(f'{s}:{b}' for s, b, _ in specs)])
        return code, verifier, out.getvalue()


class AC1Tests(Base):
    def five(self):
        a, b, c = self.spec(9101), self.spec(9102), self.spec(9103)
        d1 = self.spec(9104)
        d2 = self.spec(9105, parent=d1[1], dependency='L-spec-9104')
        return [d2, b, d1, c, a], d1, d2

    def test_five_compatible_specs_one_canonical_batch_everywhere(self):
        shuffled, d1, d2 = self.five()
        expected = self.expected_order(shuffled)
        self.assertEqual(len(expected), 5)
        self.assertLess(expected.index(d1[0]), expected.index(d2[0]))
        # prewindow admission
        kept, drops = window_prewindow.select(self.repo, self.base, 'master', shuffled, [], self.content, on=self.base)
        self.assertEqual(([e[0] for e in kept], drops), (expected, []))
        # rehearsal preparation: one candidate, merged in producer order, one head
        record = {}
        candidate = self.prepare(shuffled, record)
        self.assertEqual(self.chain_specs(candidate), expected)
        self.assertEqual(len(self.heads), 1)
        self.assertEqual(len(self.heads[0]), 1 + 5)
        # queue refill: one batch rehearsal for the five, cap 6
        self.queue(shuffled)
        with patch.object(wr, 'prepare', wraps=wr.prepare) as prepared:
            started, launches = self.refill(cap=6)
        self.assertEqual((len(started), len(launches), prepared.call_count), (1, 1, 1))
        argv = launches[0]
        self.assertEqual([p.split(':')[0] for p in argv[argv.index('--specs') + 1].split(',')], expected)
        # real window: shipped in the same order on one landing
        code, verifier, _ = self.window('w-real', shuffled, DOIT_WINDOW_MAX_MERGES='6')
        # the existing baseline PG probe runs on the base; the batch has ONE candidate verification
        candidates = [c for c in verifier.call_args_list if c.args[1] != self.base]
        self.assertEqual((code, len(candidates)), (0, 1))
        shipped = self.ledger('shipped')
        self.assertEqual([e['subject'] for e in shipped], expected)
        self.assertEqual(len({e['sha'] for e in shipped}), 1)
        print('AC1 order', expected, 'batches', len(launches), 'candidate verifications', len(candidates))

    def test_default_cap_still_applies_without_an_override(self):
        shuffled, _, _ = self.five()
        self.assertEqual(wg.max_merges(os.environ), 4)
        with patch.object(wg, 'verify', side_effect=AssertionError('dispatched')):
            code = wr.run('rh-cap', PROJECT, [f'{s}:{b}' for s, b, _ in shuffled], self.base, 'master')
        self.assertEqual(code, 2)
        self.assertFalse(self.alembic.called)

    def test_dependency_hold_and_interfaces_come_from_the_ordering_producer(self):
        x, y = self.spec(9111), self.spec(9112)
        (self.content / 'L-spec-9111.md').write_text(
            (self.content / 'L-spec-9111.md').read_text() + '\n## Interfaces\n\nConsumes:\n- `thing.make() -> int`\n')
        (self.content / 'L-spec-9112.md').write_text(
            (self.content / 'L-spec-9112.md').read_text() + '\n## Interfaces\n\nProduces:\n- `thing.make() -> int`\n')
        ordered, held = wm.canonical(self.repo, self.base, [x, y], [], self.content)
        self.assertEqual(([e[0] for e in ordered], held), (['L-spec-9112', 'L-spec-9111'], {}))
        self.assertEqual([e[0] for e in ordered], self.expected_order([x, y]))


class AC2Tests(Base):
    def pair(self):
        return self.spec(9201), self.spec(9202)

    def assert_final(self, candidate, first, second):
        repo = self.repo
        self.assertEqual(rechain.revision(rechain.text(repo, candidate, rechain.VERSIONS + 'm9202.py')),
                         ('m9202', 'm9201'))
        self.assertEqual(rechain.routed_head(repo, candidate), 'm9202')
        reader = window_prewindow.Trees(repo)
        graph = window_prewindow.candidate_revisions(reader, reader.read(candidate), True)
        self.assertEqual(len(graph.keys() - {p for ps in graph.values() for p in ps}), 1)
        # the production fingerprint path, run again on the final tree, reproduces its bytes
        before = pathlib.Path(os.environ['RECHAIN_TEST_LOG']).read_text().count('fingerprint ')
        self.assertEqual(rechain.fingerprint(repo, candidate, 'w-again', ev=lambda *a, **k: None, project=PROJECT),
                         candidate)
        after = pathlib.Path(os.environ['RECHAIN_TEST_LOG']).read_text().count('fingerprint ')
        self.assertGreater(after, before)
        self.assertTrue(rechain.text(repo, candidate, rechain.FINGERPRINT).startswith('final:'))

    def test_bookkeeping_clash_is_assembled_by_every_entry_point(self):
        first, second = self.pair()
        steps = rechain_check.stack(self.repo, self.base, [first, second])
        self.assertEqual([s.outcome for s in steps], ['clean', 'rechained'])
        # prewindow solo + cumulative merge: the cumulative merge carries the exact entry
        seen = []
        real = window_prewindow.rechain.resolve
        def spy(repo, cur, entry, window, **kw):
            seen.append(entry)
            return real(repo, cur, entry, window, **kw)
        with patch.object(window_prewindow.rechain, 'resolve', side_effect=spy):
            kept, drops = window_prewindow.select(self.repo, self.base, 'master', [first, second], [],
                                                  self.content, on=self.base)
        self.assertEqual((kept, drops), ([first, second], []))
        self.assertIn(second, seen)
        # precheck run/chain
        master = self.git('rev-parse', 'master')
        rows = [dict(spec=s, branch=b) for s, b, _ in (first, second)]
        prefixes = precheck.chain(self.repo, master, rows, {s: sha for s, _, sha in (first, second)})
        self.assertEqual(rechain.routed_head(self.repo, prefixes[-1]), 'm9202')
        # candidate builder + fingerprint through prepare, with the receipt record
        record = {}
        candidate = self.prepare([first, second], record)
        self.assert_final(candidate, first, second)
        self.assertEqual(list(record['rechain']), [second[0]])
        self.assertEqual(record['rechain'][second[0]], dict(old_parent='h0', new_parent='m9201'))
        self.assertEqual(record['rechained_specs'], [second[0]])
        # queue refill
        self.queue([first, second])
        started, launches = self.refill()
        self.assertEqual((len(started), len(launches)), (1, 1))
        receipt = self.ledger('window-rehearsed')[-1]
        self.assertEqual(receipt['rechain'], {second[0]: dict(old_parent='h0', new_parent='m9201')})
        self.assertFalse(self.ledger('merge-conflict', 'conflict-rework', 'build-started', 'rechain-declined'))
        print('AC2 final head m9202 parent m9201 events', [e['type'] for e in self.ledger()])

    def test_isolation_coverage_shapes_a_and_b(self):
        for label, combined in (('a', False), ('b', True)):
            with self.subTest(shape=label), tempfile.TemporaryDirectory() as root:
                fx = T.Fixture(root)
                case = T.RechainTests('test_2253_isolation_entries')
                case.fx = fx
                _, b = case.coverage_case('2189', combined=combined)
                a_sha = fx.repo and rechain.out(fx.repo, 'rev-parse', 'br-a')
                a = ('L-spec-9301', 'br-a', a_sha)
                b = ('L-spec-9302', b[1], b[2])
                steps = rechain_check.stack(fx.repo, fx.base, [a, b])
                self.assertEqual([s.outcome for s in steps], ['clean', 'rechained'])
                events = []
                tip, why = rechain.resolve(fx.repo, steps[0].tip, b, 'w-shape', ev=lambda k, s, **kw: events.append(kw))
                self.assertIsNone(why)
                self.assertEqual((events[0]['old_parent'], events[0]['new_parent']), ('h0', 'a'))
                if combined:
                    self.assertIn(case.ARENA, events[0]['files'])

    def test_pregrade_has_no_entry_and_stays_undetermined_without_guards_or_events(self):
        first, second = self.pair()
        self.git('merge', '--no-ff', '-m', 'land first', first[2])
        calls = []
        real = precheck.merge
        def merge(repo, left, right, *, entry=None):
            calls.append(entry)
            return real(repo, left, right, entry=entry)
        guards = []
        events = [dict(v=1, type='build-done', subject=second[0], project=PROJECT, ready_sha=second[2],
                       branch=second[1], ts=self.now.isoformat(), actor='builder')]
        emitted = []
        with patch.object(precheck, 'merge', side_effect=merge):
            results = precheck.pregrade(PROJECT, events=events, guard_runner=lambda tree: guards.append(tree) or [],
                                        targeted_runner=lambda *a, **k: guards.append('targeted'),
                                        emit=lambda *a, **k: emitted.append((a, k)))
        self.assertEqual(calls, [None])
        self.assertEqual(guards, [])
        self.assertEqual([a[1] for a, _ in emitted], ['precheck-pregrade'])
        self.assertEqual(emitted[0][1]['verdict'], 'undetermined')
        self.assertFalse([e for e in self.ledger() if e['type'] in ('merge-conflict', 'conflict-rework')])


class AC3Tests(Base):
    def test_resolver_approved_overlap_is_admitted_with_overlapping_grants(self):
        first, second = self.spec(9401), self.spec(9402)
        g1 = (self.content / 'L-spec-9401.md').read_text()
        self.assertTrue(window_prewindow.overlap(
            [p.strip() for p in g1.split(':', 1)[1].split(',')],
            [p.strip() for p in (self.content / 'L-spec-9402.md').read_text().split(':', 1)[1].split(',')]))
        kept, drops = window_prewindow.select(self.repo, self.base, 'master', [first, second], [], self.content, on=self.base)
        self.assertEqual((kept, drops), ([first, second], []))

    def test_refusal_and_hold_matrix(self):
        rows = []
        first = self.spec(9411)
        # two-version branch needing re-chain: declined by the resolver
        def two_versions(f):
            f.write(rechain.VERSIONS + 'extra.py', "revision = 'extra'\ndown_revision = 'm9412'\n")
        two = self.spec(9412, mutation=two_versions)
        steps = rechain_check.stack(self.repo, self.base, [first, two])
        rows.append(('two-version', steps[-1].outcome))
        self.assertEqual(steps[-1].outcome, 'declined')
        # ordinary nonmigration overlap stays conservative
        n1, n2 = self.base, self.base
        self.git('checkout', '-q', '-B', 'plain-1', self.base)
        self.fx.write('outside.txt', 'one\n'); one = (self.fx.commit('one'), 'plain-1')
        self.git('checkout', '-q', '-B', 'plain-2', self.base)
        self.fx.write('outside.txt', 'two\n'); two_sha = self.fx.commit('two')
        self.git('checkout', '-q', 'master')
        (self.content / 'L-spec-9413.md').write_text('Writes: outside.txt\n')
        (self.content / 'L-spec-9414.md').write_text('Writes: outside.txt\n')
        kept, drops = self._select([('L-spec-9413', 'plain-1', one[0]), ('L-spec-9414', 'plain-2', two_sha)])
        self.assertEqual([k[0] for k in kept], ['L-spec-9413'])
        rows.append(('nonmigration-overlap', 'deferred'))
        # malformed metadata, cycle, absent deferred dependency, uncovered path
        cyc_a = self.spec(9421, dependency='L-spec-9422')
        cyc_b = self.spec(9422, dependency='L-spec-9421')
        ordered, held = wm.canonical(self.repo, self.base, [cyc_a, cyc_b], [], self.content)
        self.assertEqual((ordered, sorted(held)), ([], ['L-spec-9421', 'L-spec-9422']))
        rows.append(('cycle', 'held'))
        absent = self.spec(9423, dependency='L-spec-9499')
        buddy = self.spec(9424)
        ordered, held = wm.canonical(self.repo, self.base, [absent, buddy], [], self.content)
        self.assertEqual((list(held), [e[0] for e in ordered]), (['L-spec-9423'], ['L-spec-9424']))
        rows.append(('absent-dependency', 'held'))
        (self.content / 'L-spec-9424.md').write_text('no writes grant here\n')
        kept, drops = self._select([buddy])
        self.assertEqual((kept, len(drops)), ([], 1))
        rows.append(('malformed-metadata', 'dropped'))
        # stack exception / missing step / unknown outcome are undetermined, never admission
        x, y = self.spec(9431), self.spec(9432)
        with patch.object(rechain_check, 'stack', side_effect=RuntimeError('boom')):
            self.assertIn('undetermined', wm.stack_check(self.repo, self.base, [x, y])[0])
        with patch.object(rechain_check, 'stack', return_value=[]):
            self.assertIn('undetermined', wm.stack_check(self.repo, self.base, [x, y])[0])
        bogus = rechain_check.StackStep('L-spec-9431', 'surprising', [], None, self.base)
        with patch.object(rechain_check, 'stack', return_value=[bogus, bogus]):
            self.assertTrue(wm.stack_check(self.repo, self.base, [x, y])[0])
        rows.append(('stack-undetermined', 'held'))
        # zero / multiple heads
        double = self.spec(9441, mutation=lambda f: f.write(rechain.VERSIONS + 'side.py',
                                                               "revision = 'side'\ndown_revision = 'h0'\n"))
        with self.assertRaises(ValueError):
            self.prepare([double])
        # none of the rejected combinations dispatched CI or touched master
        self.assertEqual(self.git('rev-parse', 'master'), self.base)
        print('AC3 matrix', rows)

    def _select(self, entries):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return window_prewindow.select(self.repo, self.base, 'master', entries, [], self.content, on=self.base)

    def test_re_chain_disabled_never_lets_a_conflict_through(self):
        first, second = self.spec(9451), self.spec(9452)
        with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='off'):
            self.assertIn('disabled', wm.stack_check(self.repo, self.base, [first, second])[0])
            with self.assertRaises(ValueError):
                self.prepare([first, second])
            kept, _ = self._select([first, second])
        self.assertEqual(kept, [first])

    def test_clean_multi_version_branch_keeps_existing_policy(self):
        def two(f):
            f.write(rechain.VERSIONS + 'extra.py', "revision = 'extra'\ndown_revision = 'm9461'\n")
        solo = self.spec(9461, mutation=two)
        self.assertEqual(len(rechain.added_versions(self.repo, self.base, solo[2])), 2)
        steps = rechain_check.stack(self.repo, self.base, [solo])
        self.assertEqual(steps[0].outcome, 'clean')
        self.assertEqual(wm.stack_check(self.repo, self.base, [solo]), (None, set()))


class AC4Tests(Base):
    def shape_abc(self):
        a, b, c = self.spec(9501), self.spec(9502), self.spec(9503)
        return a, b, c

    def test_receipts_and_shipped_records_round_trip_through_the_ledger(self):
        a, b, c = self.shape_abc()
        entries = [a, b, c]
        stale = dict(type='rechained', subject='L-spec-9502', window='other-window', branch='x', merge_sha='0' * 40,
                     old_parent='zz', new_parent='yy')
        wg.ev('rechained', stale['subject'], **{k: v for k, v in stale.items() if k not in ('type', 'subject')})
        # launcher handoff: refill prepares, the real worker consumes the receipt
        self.queue(entries)
        def runner(argv):
            with patch.object(wr, 'prepare', side_effect=AssertionError('candidate rebuilt')), \
                 patch.object(wg, 'verify', return_value=dict(GREEN)), \
                 patch.object(wg, 'base_red_snapshot', return_value={'complete': True}):
                self.assertEqual(wg.main_(argv[2:]), 0)
        started, _ = self.refill(runner=runner)
        self.assertEqual(len(started), 1)
        want = {b[0]: dict(old_parent='h0', new_parent='m9501'), c[0]: dict(old_parent='h0', new_parent='m9502')}
        receipts = [e for e in self.ledger('window-rehearsed') if e['subject'] == started[0]]
        self.assertEqual([e['state'] for e in receipts][:1], ['prepared'])
        self.assertTrue(receipts[0]['launcher_prepared'])
        for e in receipts:
            self.assertEqual(e['rechained_specs'], [b[0], c[0]])
            self.assertEqual(e['rechain'], want)
            self.assertIsInstance(e['rechained_specs'], list)
        self.assertEqual(receipts[-1]['state'], 'green')
        candidate = receipts[-1]['sha']
        self.assertEqual(receipts[-1]['base_sha'], self.base)
        self.assertEqual(wg.tree_of(self.repo, candidate), receipts[-1]['tree'])
        # direct preparation (no launcher receipt) carries the same parents
        with patch.object(wg, 'verify', return_value=dict(GREEN)):
            self.assertEqual(wr.run('rh-direct', PROJECT, [f'{s}:{br}' for s, br, _ in entries], self.base, 'master'), 0)
        direct = [e for e in self.ledger('window-rehearsed') if e['subject'] == 'rh-direct']
        self.assertTrue(direct and all(e['rechain'] == want for e in direct[1:]))
        self.assertNotIn('launcher_prepared', direct[0])
        # real window: shipped records carry the surviving batch; only b and c are truthy rechained
        code, _, _ = self.window('w-ship', entries)
        self.assertEqual(code, 0)
        shipped = {e['subject']: e for e in self.ledger('shipped')}
        for sid in (a[0], b[0], c[0]):
            self.assertEqual(shipped[sid]['rechained_specs'], [b[0], c[0]])
            self.assertEqual(shipped[sid]['rechain'], want)
            self.assertEqual(shipped[sid]['window'], 'w-ship')
        self.assertEqual({s for s, e in shipped.items() if e.get('rechained')}, {b[0], c[0]})
        self.assertTrue(all(e['rechained'] is True for s, e in shipped.items() if s != a[0]))
        self.assertNotIn(a[0], [s for s, e in shipped.items() if e.get('rechained')])
        print('AC4 shipped', {s: e.get('rechained', False) for s, e in shipped.items()})

    def test_single_spec_stale_parent_landing_through_advance(self):
        first, second = self.spec(9511), self.spec(9512)
        code, _, _ = self.window('w-one', [first])
        self.assertEqual(code, 0)
        code, verifier, _ = self.window('w-two', [second])
        self.assertEqual((code, verifier.call_count), (0, 1))
        landed = [e for e in self.ledger('shipped') if e['subject'] == second[0]][0]
        self.assertIs(landed['rechained'], True)
        self.assertEqual(landed['rechained_specs'], [second[0]])
        self.assertEqual(landed['rechain'], {second[0]: dict(old_parent='h0', new_parent='m9511')})
        first_rec = [e for e in self.ledger('shipped') if e['subject'] == first[0]][0]
        self.assertEqual((first_rec['rechained_specs'], first_rec['rechain']), ([], {}))
        self.assertNotIn('rechained', first_rec)
        self.assertTrue(self.ledger('rechain-fingerprint'))

    def test_bound_to_the_final_candidate_and_window(self):
        a, b, _ = self.shape_abc()
        events = []
        cand, kept, _ = wg.build_candidate(self.repo, 'w-x', self.base, [a, b], publish=False, emit=lambda k, s, **kw: events.append(dict(kw, type=k, subject=s)))
        self.assertEqual(kept, [a[0], b[0]])
        names, mapping = wm.parents(self.repo, self.base, cand, 'w-x', [a[0], b[0]], events)
        self.assertEqual((names, list(mapping)), ([b[0]], [b[0]]))
        # a dropped member, another window and an abandoned candidate cannot leak
        self.assertEqual(wm.parents(self.repo, self.base, cand, 'w-x', [a[0]], events), ([], {}))
        self.assertEqual(wm.parents(self.repo, self.base, cand, 'other', [a[0], b[0]], events), ([], {}))
        stale = [dict(e, merge_sha='1' * 40) for e in events]
        self.assertEqual(wm.parents(self.repo, self.base, cand, 'w-x', [a[0], b[0]], stale), ([], {}))
        self.assertEqual(wm.receipt_fields({}), dict(rechained_specs=[], rechain={}))
        self.assertEqual(wm.receipt_fields(dict(rechained_specs=['x'], rechain={})), dict(rechained_specs=[], rechain={}))

    def test_historical_bool_readers_and_predecessor_cancellation(self):
        verified = dict(type='window-verified', subject='W', window='W', sha='s', dropped=[], kept=['a', 'b'])
        old = [verified, dict(type='shipped', subject='b', window='W', rechained=True)]
        self.assertEqual(wg.predecessor_state(self.repo, 'master', 'W', 's', events=old), 'predecessor-rechained')
        listed = [verified, dict(type='shipped', subject='a', window='W', rechained_specs=['b'],
                                 rechain={'b': dict(old_parent='h0', new_parent='a')})]
        self.assertNotEqual(wg.predecessor_state(self.repo, 'master', 'W', 's', events=listed), 'predecessor-rechained')


class AC5Tests(Base):
    def test_moved_base_rebuilds_and_never_reuses_the_old_green(self):
        first, second = self.spec(9601), self.spec(9602)
        trees = []
        def verify(repo, cand, required, window, **kw):
            if cand != self.git('rev-parse', 'master'):      # not the baseline probe of the moved base
                trees.append(wg.tree_of(repo, cand))
            return dict(GREEN)
        # a rehearsal is green on the base, then master moves before the window
        old = self.prepare([second])
        old_tree = wg.tree_of(self.repo, old)
        self.git('merge', '--no-ff', '-m', 'master moves', first[2])
        moved = self.git('rev-parse', 'master')
        code, verifier, _ = self.window('w-moved', [second], verify=verify)
        self.assertEqual(code, 0)
        landed = self.git('rev-parse', 'master')
        self.assertNotEqual(wg.tree_of(self.repo, landed), old_tree)
        self.assertEqual(trees, [wg.tree_of(self.repo, landed)])
        self.assertIn(moved, self.git('rev-list', '--first-parent', landed).split())   # advanced from the moved main
        self.assertNotEqual(landed, moved)
        self.assertEqual(rechain.routed_head(self.repo, landed), 'm9602')

    def test_red_and_undetermined_never_land_and_failed_fingerprint_is_not_retried(self):
        first, second = self.spec(9611), self.spec(9612)
        for state in ('red', 'undetermined'):
            code, _, _ = self.window('w-' + state, [first],
                                     verify=lambda *a, **k: dict(state=state, nodes=[], url='', doc={}))
            self.assertNotEqual(code, 0)
            self.assertEqual(self.git('rev-parse', 'master'), self.base)
        log = pathlib.Path(os.environ['RECHAIN_TEST_LOG'])
        failing = self.fx.scripts(self.root / 'state', fail=True)
        (self.root / 'state' / 'calls').write_text('')
        with patch.dict(os.environ, failing):
            code, verifier, _ = self.window('w-fp', [first, second])
        self.assertNotEqual(code, 0)
        self.assertEqual(verifier.call_count, 0)
        self.assertEqual(self.git('rev-parse', 'master'), self.base)
        calls = (self.root / 'state' / 'calls').read_text().count('fingerprint ')
        self.assertLessEqual(calls, 2)


class AC6Tests(Base):
    def test_cap_four_selects_four_and_cap_six_admits_five(self):
        entries = [self.spec(9700 + i) for i in range(5)]
        self.queue(entries)
        started, launches = self.refill(cap=4)
        first = [p.split(':')[0] for p in launches[0][launches[0].index('--specs') + 1].split(',')]
        self.assertEqual(len(first), 4)
        self.assertLessEqual(len(started), 3)
        # fresh ledger and queue for cap 6
        for name in ('events',):
            for path in (self.root / name).iterdir():
                path.unlink()
        self.queue(entries)
        started, launches = self.refill(cap=6)
        second = [p.split(':')[0] for p in launches[0][launches[0].index('--specs') + 1].split(',')]
        self.assertEqual((len(started), len(second)), (1, 5))

    def test_successor_builds_only_on_the_in_flight_candidate_and_in_flight_is_capped(self):
        entries = [self.spec(9720 + i) for i in range(7)]
        self.queue(entries)
        started, launches = self.refill(cap=2)
        self.assertEqual(len(started), 3)                      # existing maximum of three in flight
        receipts = {e['subject']: e for e in self.ledger('window-rehearsed') if e['state'] == 'prepared'}
        bases = [receipts[i]['base_sha'] for i in started]
        shas = [receipts[i]['sha'] for i in started]
        self.assertEqual(bases, [self.base] + shas[:2])
        for earlier, later in zip(shas, shas[1:]):
            self.assertEqual(self.git('merge-base', '--is-ancestor', earlier, later) or 'ok', 'ok')

    def test_unavailable_or_red_predecessor_refuses_and_stack_rejects_before_dispatch(self):
        entries = [self.spec(9740), self.spec(9741)]
        self.queue(entries)
        started, _ = self.refill(cap=1)
        batch = started[0]
        wg.ev('window-rehearsed', batch, project=PROJECT, state='red', rehearsal=True, base_sha=self.base,
              specs=[entries[0][0]], entries=[list(entries[0])], sha=self.git('rev-parse', 'master'))
        again, launches = self.refill(cap=1)
        self.assertEqual((again, launches), ([], []))
        # a stack rejection happens before any hosted verification call
        with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='off'), patch.object(wg, 'verify', side_effect=AssertionError('paid')):
            code = wr.run('rh-off', PROJECT, [f'{s}:{b}' for s, b, _ in entries], self.base, 'master')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
