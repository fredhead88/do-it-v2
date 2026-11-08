#!/usr/bin/env python3
"""Scratch-only contract tests for prerequisite ordering and its dispatcher."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import migration_order as mo
import autodispatch
import dependency_holds
import dispatch
import lane_rules

REPO = Path(__file__).resolve().parent.parent
A, B, C, D, E = [f'L-spec-{n}' for n in range(9001, 9006)]


def text(dep=None, consumes='', produces='', writes='src/example.py'):
    return (f'Dependency-hold: waits_for={dep}\n' if dep else '') + (
        f'## Interfaces\nConsumes:\n{consumes}\nProduces:\n{produces}\n\nWrites:\n- {writes}\n')


def ev(kind, sid=A, day=1, **kw):
    return dict(type=kind, subject=sid, ts=f'2026-11-{day:02}T00:00:00Z',
                actor='operator', _src='L-operator-0001.jsonl:1', **kw)


class OrderGraphTests(unittest.TestCase):
    def test_signature_span_suffixes_and_local_module_mismatch(self):
        signature = 'migration_order.order(specs, events, *, texts) -> OrderResult'
        # This is the spec's Produces bullet, including its trailing prose.
        provider = ('- `migration_order.order(specs, events, *, texts) -> OrderResult`; '
                    'fields `ordered: list[str]`, `deferred: dict[str, str]`, '
                    '`cycles: list[list[str]]`. specs is an iterable of requested ids. '
                    'texts maps project spec ids to text and includes open, not-ready waiters '
                    'for weight and interface resolution. Terminal/shipped prerequisite '
                    'evidence comes from events. Neither input is mutated; the API performs '
                    'no filesystem reads. Missing requested texts are deferred. Invalid '
                    'metadata in a potential waiter/provider must not silently reduce '
                    'weight or erase a dependency: return an explicit undetermined '
                    'deferral for affected requested work.')
        for declaration in (provider, f'- `{signature}` (ordered, deferred, cycles)',
                            f'- {signature} (fields ordered, deferred, cycles)'):
            texts = {A: text(consumes=f'- `{signature}`'), B: text(produces=declaration)}
            self.assertEqual(mo.order([A, B], [], texts=texts).ordered, [B, A])
        for returned in ('str | None', 'dict[str, list[int]]', 'tuple[int, str] | None'):
            signature = f'local.read() -> {returned}'
            texts = {A: text(consumes=f'- `{signature}`'),
                     B: text(produces=f'- {signature} (fields optional)')}
            self.assertEqual(mo.order([A, B], [], texts=texts).ordered, [B, A])
        texts = {A: text(consumes='- `migration_order.missing() -> int`'),
                 B: text(produces=provider), C: text(A)}
        result = mo.order([A, B, C], [], texts=texts)
        self.assertEqual(result.ordered, [B])
        self.assertIn('undetermined', result.deferred[A])
        self.assertIn('migration_order.missing() -> int', result.deferred[A])
        self.assertIn('migration_order', result.deferred[C])
        self.assertEqual(mo.order([A], [], texts={A: texts[A]}).ordered, [A])

    def test_five_migrations_and_mixed_code_are_uncapped(self):
        texts = {A: text(writes='api/migrations/001.py'), B: text(), C: text(),
                 D: text(), E: text(D)}
        for ids in ([E, D, C, B, A], [A, B, C, E, D]):
            result = mo.order(ids, [], texts=texts)
            self.assertEqual(set(result.ordered), set(texts))
            self.assertEqual(len(result.ordered), 5)
            self.assertLess(result.ordered.index(D), result.ordered.index(E))
            self.assertEqual((result.deferred, result.cycles), ({}, []))
        migrations = {sid: value.replace('src/example.py', f'api/migrations/{sid}.py')
                      for sid, value in texts.items()}
        self.assertEqual(len(mo.order(migrations, [], texts=migrations).ordered), 5)

    def test_all_dependency_sources_and_no_writes_edge(self):
        cases = [({A: text(B), B: text()}, []),
                 ({A: text(), B: text()}, [ev('dependency-hold', waits_for=B)]),
                 ({A: text(consumes=f'- `{B}`'), B: text()}, []),
                 ({A: text(consumes='- `Some API`'), B: text(produces='- Some   API')}, [])]
        for texts, events in cases:
            with self.subTest(texts=texts, events=events):
                self.assertEqual(mo.order([A, B], events, texts=texts).ordered, [B, A])
        events = [ev('dependency-hold', waits_for=B), ev('unblocked', day=2)]
        self.assertEqual(mo.order([A, B], events, texts={A: text(), B: text()}).ordered, [A, B])
        self.assertEqual(mo.order([A], [ev('shipped', B)], texts={A: text(B)}).ordered, [A])
        # Same Writes grants and unmatched external signatures introduce no edges.
        self.assertEqual(mo.order([B, A], [], texts={A: text(consumes='external'), B: text()}).ordered, [A, B])
        self.assertEqual(mo.order([A], [], texts={A: text(), B: text()}).ordered, [A])


class WeightTests(unittest.TestCase):
    def test_all_terminal_waiters_and_cross_charter_open_waiter(self):
        cases = [[ev('shipped', B)], [ev('spec-killed', B)],
                 [ev('spawn-started', B), ev('spawn-failed', B)],
                 [ev('spec-written', B, charter='L-charter-9999'),
                  ev('charter-retracted', 'L-charter-9999')],
                 [ev('spec-closed', B)], [ev('spec-closed', B), ev('shipped', B)]]
        for rows in cases:
            if rows[0]['type'] == 'spawn-started':
                for row in rows:
                    row['actor'] = 'spec-writer'
            with self.subTest(rows=rows):
                texts = {A: text(), B: text(A), C: text()}
                self.assertEqual(mo.chain_weight(A, rows, texts), 1)
                self.assertEqual(mo.order([C, A], rows, texts=texts).ordered, [A, C])
        rows = [ev('spec-written', B, charter='L-charter-9998')]
        self.assertEqual(mo.chain_weight(A, rows, {A: text(), B: text(A)}), 2)

    def test_direct_transitive_diamond_and_interface_exclusion(self):
        texts = {A: text(), B: text(A), C: text(A), D: text(B, consumes=f'- {C}'), E: text(D)}
        self.assertEqual(mo.chain_weight(A, [], texts), 5)
        self.assertEqual(mo.chain_weight(C, [], texts), 1)
        # A cycle must count distinct waiters, never its root.
        texts[A] = text(E)
        self.assertEqual(mo.chain_weight(A, [], texts), 5)
        texts = {A: text(), B: text(A), C: text(B), D: text(C)}
        self.assertEqual(mo.chain_weight(A, [ev('shipped', B), ev('spec-killed', C)], texts), 1)
        self.assertEqual(mo.chain_weight(A, [ev('shipped', A)], {A: text(), B: text(A)}), 1)
        events = [ev('dependency-hold', B, waits_for=A), ev('unblocked', B, day=2)]
        self.assertEqual(mo.chain_weight(A, events, {A: text(), B: text()}), 1)

    def test_weight_grade_and_id_ties_with_open_nonready_waiters(self):
        texts = {A: text(), B: text(), C: text(B), D: text(), E: text()}
        events = [ev('verdict', A, day=1, confirmed=True),
                  ev('verdict', A, day=4, confirmed=True),
                  ev('verdict', D, day=2, confirmed=True),
                  ev('verdict', E, day=1, confirmed=False),
                  ev('spec-written', C, charter='another-charter')]
        self.assertEqual(mo.chain_weight(B, events, texts), 2)
        for ids in itertools.permutations([A, B, D, E]):
            for values, rows in [(texts, events), (dict(reversed(list(texts.items()))), list(reversed(events)))]:
                self.assertEqual(mo.order(ids, rows, texts=values).ordered, [B, D, A, E])
        self.assertEqual(mo.order([D, A], [], texts={D: text(A), A: text()}).ordered, [A, D])
        self.assertEqual(mo.order([B, A], [], texts={A: text(), B: text()}).ordered, [A, B])

    def test_owed_confirmation_uses_fold_authority(self):
        rows = [ev('owed-ac', B, criterion='AC1', actor_override='unused')]
        rows[0]['actor'] = 'spec-writer'
        rows += [ev('verdict', B, day=1, cannot_assess=['AC1'], matches_intent='yes', card_ok='yes'),
                 ev('verdict', A, day=2, confirmed=True)]
        rows[1]['confirmed'] = False
        self.assertEqual(mo.order([A, B], rows, texts={A: text(), B: text()}).ordered, [B, A])
        self.assertEqual(mo.order([A, B], rows[1:], texts={A: text(), B: text()}).ordered, [A, B])


class BlockedTests(unittest.TestCase):
    def test_unresolved_killed_missing_and_propagation(self):
        self.assertEqual(mo.order([A], [], texts={A: text(B)}).deferred,
                         {A: f'prerequisite {B} is not requested or shipped'})
        self.assertEqual(mo.order([A], [ev('spec-killed', B)], texts={A: text(B)}).deferred,
                         {A: f'prerequisite {B} is killed'})
        result = mo.order([A, B, C], [], texts={A: text(B), C: text()})
        self.assertEqual(result.ordered, [C])
        self.assertEqual(result.deferred[B], f'metadata unavailable for {B}')
        self.assertIn(B, result.deferred[A])
        self.assertEqual(mo.order([], [], texts={}), mo.OrderResult([], {}, []))
        self.assertEqual(mo.order([A, A], [], texts={A: text()}).ordered, [A])
        self.assertEqual(mo.order([A], [ev('spec-closed', B)], texts={A: text(B)}).deferred,
                         {A: f'prerequisite {B} is closed and unshipped'})

    def test_invalid_metadata_is_explicit_even_in_potential_waiter(self):
        for bad in [text('bad\nCYCLE forged'), text(A) + f'Dependency-hold: waits_for={B}\n',
                    text(writes='invalid prose'), None]:
            with self.subTest(bad=bad):
                result = mo.order([A], [], texts={A: text(), B: bad})
                self.assertEqual(result.ordered, [])
                self.assertIn(B, result.deferred[A])
                self.assertNotIn('\n', result.deferred[A])
                with self.assertRaises(ValueError):
                    mo.chain_weight(A, [], {A: text(), B: bad})
        invalid = mo.order([A], [], texts={A: text(), 'bad\nCYCLE forged': text()})
        self.assertEqual(invalid.ordered, [])
        self.assertNotIn('\n', invalid.deferred[A])
        self.assertIn('ambiguous', mo.order([A, B, C], [], texts={
            A: text(consumes='api'), B: text(produces='api'), C: text(produces='api')}).deferred[A])
        self.assertIn('invalid', mo.order([A], [ev('dependency-hold', B, waits_for=[])],
                                         texts={A: text(), B: text()}).deferred[A])

    def test_cycles_only_contain_actual_members(self):
        texts = {A: text(B), B: text(A), C: text(A), D: text(E), E: text(D)}
        result = mo.order(reversed(list(texts)), [], texts=texts)
        self.assertEqual(result.cycles, [[A, B], [D, E]])
        self.assertEqual(result.ordered, [])
        self.assertIn('cycle', result.deferred[C])
        result = mo.order([A, B, C], [], texts={A: text(A), B: text(), C: text(consumes=f'- {A}')})
        self.assertEqual(result.cycles, [[A]])
        self.assertEqual(result.ordered, [B])
        self.assertIn('cycle', result.deferred[C])
        self.assertEqual(mo.order([A, B], [], texts={A: text(consumes=B), B: text(consumes=A)}).cycles, [[A, B]])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='migration-order-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('content', 'events', 'repos/fixture', 'repos/foreign'):
            (self.root / directory).mkdir(parents=True)
        self.rows = []

    def spec(self, sid, body=None, project='fixture'):
        (self.root / 'content' / f'{sid}.md').write_text(body or text())
        self.rows.append(ev('spec-written', sid, project=project))

    def run_cli(self, ids, project='fixture'):
        actors = {row['actor'] for row in self.rows} | {'operator'}
        for actor in actors:
            path = self.root / 'events' / f'L-{actor}-0001.jsonl'
            payload = ''.join(json.dumps(row) + '\n' for row in self.rows if row['actor'] == actor)
            # Preserve unchanged fixture evidence, including its cache generation.
            if not path.exists() or path.read_text() != payload:
                path.write_text(payload)
        env = dict(os.environ, DOIT_ROOT=str(self.root), DOIT_PROJECT='foreign',
                   PYTHONDONTWRITEBYTECODE='1', DOIT_NO_POKE='1')
        return subprocess.run([str(REPO / 'doit'), 'migration-order', '--project', project,
                               '--specs', ids], env=env, text=True, capture_output=True, timeout=20)


class CliTests(Fixture):
    def test_terminal_waiters_and_project_charter_discovery(self):
        self.spec(A)
        self.spec(B)
        self.spec(C, text(B))
        self.rows[-1]['charter'] = 'L-charter-9998'
        self.assertEqual((p := self.run_cli(f'{A},{B}')).stdout, f'{B}\n{A}\n', p.stderr)
        cases = [[ev('shipped', C)], [ev('spec-killed', C)],
                 [ev('spec-closed', C)], [ev('shipped', C), ev('spec-closed', C)],
                 [ev('charter-retracted', 'L-charter-9998')]]
        open_rows = copy.deepcopy(self.rows)
        for terminal in cases:
            self.rows = open_rows + terminal
            (self.root / 'content' / f'{C}.md').unlink(missing_ok=True)
            p = self.run_cli(f'{A},{B}')
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, f'{A}\n{B}\n', ''))
        self.rows = open_rows[:-1]
        for kind in ('spawn-started', 'spawn-failed'):
            self.rows.append(dict(ev(kind, C, project='fixture'), actor='spec-writer'))
        p = self.run_cli(f'{A},{B}')
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, f'{A}\n{B}\n', ''))

    def test_protocol_and_project_isolation(self):
        self.spec(A)
        self.spec(B)
        self.spec(C, text(B), project='foreign')
        self.assertEqual((p := self.run_cli(f'{B},{A}')).returncode, 0, p.stderr)
        self.assertEqual(p.stdout, f'{A}\n{B}\n')
        self.assertEqual(p.stderr, '')
        self.spec(D, text(E))
        p = self.run_cli(f'{A},{D}')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, f'{A}\nDEFERRED {D} prerequisite {E} is not requested or shipped\n')
        self.spec(E, text(D))
        p = self.run_cli(f'{A},{D},{E}')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, f'{A}\nDEFERRED {D} cycle: {D} {E}\nDEFERRED {E} cycle: {D} {E}\nCYCLE {D} {E}\n')
        p = self.run_cli(C)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, '')

    def test_invalid_project_ids_and_missing_metadata(self):
        self.spec(A)
        for ids, project, status in [(A, 'unknown', 2), ('../escape', 'fixture', 2),
                                      ('L-spec-1\nCYCLE forged', 'fixture', 2),
                                      (B, 'fixture', 2)]:
            p = self.run_cli(ids, project)
            self.assertEqual(p.returncode, status)
            if status == 2:
                self.assertEqual(p.stdout, '')
                self.assertIn('undetermined', p.stderr)
        self.spec(B)
        (self.root / 'content' / f'{B}.md').unlink()
        p = self.run_cli(A)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, '')
        self.assertIn('undetermined', p.stderr)

    def test_corrupt_and_unavailable_evidence(self):
        self.spec(A)
        self.run_cli(A)
        env = dict(os.environ, DOIT_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        for payload in ['{bad\n', '[]\n', '{"type":"spec-written"}\n']:
            (self.root / 'events' / 'L-operator-0001.jsonl').write_text(payload)
            p = subprocess.run([str(REPO / 'doit'), 'migration-order', '--project', 'fixture', '--specs', A],
                               env=env, capture_output=True, text=True)
            self.assertEqual((p.returncode, p.stdout), (2, ''))
            self.assertIn('undetermined', p.stderr)
        (self.root / 'events' / 'L-operator-0001.jsonl').unlink()
        (self.root / 'events').rmdir()
        p = subprocess.run([str(REPO / 'doit'), 'migration-order', '--project', 'fixture', '--specs', A],
                           env=env, capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (2, ''))


class MetadataSafetyTests(Fixture):
    def test_lane_handoff_uses_one_default_reader_and_supplied_snapshot(self):
        rows = [ev('spec-written', project='fixture')]
        argv = ['lane_rules.py', '--migration-order', '--project', 'fixture', '--specs', A]
        with patch('sys.argv', argv), \
             patch.object(mo.fold, 'PROJECT', 'foreign'), \
             patch.object(mo.fold, 'read_events', return_value=rows) as reader, \
             patch.object(mo, 'cli', return_value=2) as cli:
            self.assertEqual(lane_rules.main(), 2)
            reader.assert_called_once_with()
            cli.assert_called_once_with(argv[2:], rows)
            self.assertIs(cli.call_args.args[1], rows)
            self.assertIsNone(mo.fold.PROJECT)
        with patch('sys.argv', argv), \
             patch.object(mo.fold, 'PROJECT', None), \
             patch.object(mo.fold, 'read_events', side_effect=OSError('unreadable')), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(lane_rules.main(), 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('undetermined', stderr.getvalue())

    def test_default_reader_works_without_writable_cache(self):
        self.spec(A, text(B))
        self.spec(B)
        self.run_cli(f'{A},{B}')
        snapshot = {str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob('*') if p.is_file()}
        with patch.object(mo.fold, 'ROOT', self.root), \
             patch.object(mo.fold, 'EVENTS', self.root / 'events'), \
             patch.object(mo.fold, 'PROJECT', None), \
             patch.dict(os.environ, {'DOIT_READ_CACHE': '1'}), \
             patch('ledger_cache.read', side_effect=PermissionError('read-only cache')), \
             patch('sys.argv', ['lane_rules.py', '--migration-order', '--project', 'fixture',
                                '--specs', f'{A},{B}']), \
             patch.object(mo.fold, 'read_events', wraps=mo.fold.read_events) as reader, \
             redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(lane_rules.main(), 0)
            reader.assert_called_once_with()
            self.assertEqual(stdout.getvalue(), f'{B}\n{A}\n')
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes()
                          for p in self.root.rglob('*') if p.is_file()}, snapshot)

    def test_supplied_text_is_authoritative_and_missing_never_falls_back(self):
        self.spec(A, text(C))
        self.spec(B)
        rows = [ev('spec-written')]
        with patch.object(dispatch, 'spec_path', side_effect=AssertionError('live content')), \
             patch.object(dependency_holds, 'text_hold_reason', side_effect=AssertionError('disk wrapper')), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('disk read')):
            self.assertEqual(mo.order([A, B], rows, texts={A: text(B), B: text()}).ordered, [B, A])
            self.assertEqual(mo.chain_weight(B, rows, {A: text(B), B: text()}), 2)
            self.assertEqual(mo.order([A], rows, texts={}).deferred,
                             {A: f'metadata unavailable for {A}'})

    def test_release_and_text_precedence_matches_authorities(self):
        hold = ev('dependency-hold', day=2, waits_for=B)
        for release, cleared in [(ev('unblocked', day=3), True),
                                 (ev('decision', day=3, ref=hold['_src']), True),
                                 (ev('unblocked', day=2), False),
                                 (ev('decision', day=3, ref='wrong:1'), False),
                                 (dict(ev('unblocked', day=3), actor='builder'), False)]:
            events = [hold, release]
            self.assertEqual(autodispatch._hold_released(hold, events), cleared)
            self.assertEqual(mo.chain_weight(B, events, {A: text(), B: text()}), 1 if cleared else 2)
        # Ledger after/equal amendment supersedes text; newer text replaces stale ledger.
        for written_day, expected in [(1, 1), (2, 1), (4, 2)]:
            events = [ev('spec-amended', day=written_day), hold, ev('unblocked', day=3)]
            self.spec(A, text(B))
            with patch.object(dispatch, 'spec_path', return_value=self.root / 'content' / f'{A}.md'):
                reason = dependency_holds.text_hold_reason(events, events)
            self.assertEqual(bool(reason), expected == 2)
            self.assertEqual(mo.chain_weight(B, events, {A: text(B), B: text()}), expected)
        holds = [dict(hold, waits_for=C, _src='z:2'), dict(hold, waits_for=B, _src='z:10')]
        self.assertEqual(max(holds, key=autodispatch._hold_order)['waits_for'], B)
        self.assertEqual(mo.order([A, B, C], holds, texts={s: text() for s in [A, B, C]}).ordered, [B, A, C])

    def test_cli_release_and_authoritative_ledger(self):
        self.spec(A, text(B))
        self.spec(B)
        self.rows += [ev('dependency-hold', day=2, waits_for=B), ev('unblocked', day=3)]
        p = self.run_cli(f'{A},{B}')
        self.assertEqual((p.returncode, p.stdout), (0, f'{A}\n{B}\n'))
        self.rows += [ev('spec-amended', day=4)]
        p = self.run_cli(f'{A},{B}')
        self.assertEqual((p.returncode, p.stdout), (0, f'{B}\n{A}\n'))
        self.spec(C, text() + f'Dependency-hold: waits_for={A},{B}\n')
        p = self.run_cli(A)
        self.assertEqual((p.returncode, p.stdout), (2, ''))
        self.assertIn('undetermined', p.stderr)

    def test_fences_repeated_declarations_and_exact_interfaces(self):
        body = text() + f'\n```\nDependency-hold: waits_for={B}\n```\n'
        self.assertIsNone(dependency_holds.from_spec_text(body))
        self.assertEqual(mo.order([A], [], texts={A: body}).ordered, [A])
        for decl in [f'Dependency-hold: waits_for={B},{C}\n',
                     f'Dependency-hold: waits_for={B}\nDependency-hold: waits_for={C}\n']:
            with self.assertRaises(ValueError):
                dependency_holds.from_spec_text(decl)
            self.assertIn('invalid', mo.order([A], [], texts={A: decl + text()}).deferred[A])
        self.assertEqual(mo.order([A, B], [], texts={A: text(consumes='- API extra'), B: text(produces='- API')}).ordered, [A, B])
        body = '## Interfaces\nConsumes:\n- `Some\n  API`\nProduces:\n\nWrites:\n- src/example.py\n'
        self.assertEqual(mo.order([A, B], [], texts={A: body, B: text(produces='- Some API')}).ordered, [B, A])
        self.assertIn(B, mo.order([A], [], texts={A: text(consumes=f'{B} external')}).deferred[A])

    def test_no_mutation_or_forbidden_calls_and_persistent_bytes(self):
        texts = {A: text(B), B: text()}
        events = [ev('spec-written')]
        before = copy.deepcopy((texts, events))
        with patch('pathlib.Path.open', side_effect=AssertionError('filesystem API')), \
             patch('subprocess.Popen', side_effect=AssertionError('subprocess API')), \
             patch('socket.socket', side_effect=AssertionError('network API')):
            self.assertEqual(mo.order([A, B], events, texts=texts).ordered, [B, A])
            self.assertEqual(mo.chain_weight(B, events, texts), 2)
        self.assertEqual((texts, events), before)
        self.spec(A, text(B))
        self.spec(B)
        self.run_cli(f'{A},{B}')  # materialize the synthetic ledger before snapshot
        snapshot = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        p = self.run_cli(f'{A},{B}')
        self.assertEqual((p.returncode, p.stdout), (0, f'{B}\n{A}\n'))
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, snapshot)


class DispatcherTests(unittest.TestCase):
    def test_bash_version_help_and_discovery(self):
        self.assertEqual(subprocess.run(['/bin/bash', '-n', str(REPO / 'doit')]).returncode, 0)
        p = subprocess.run([str(REPO / 'doit'), 'version'], capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, (REPO / 'VERSION').read_text()))
        p = subprocess.run([str(REPO / 'doit'), '--help'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        for name in ('migration-order', 'gate-window', 'precheck'):
            self.assertIn('doit ' + name, p.stdout)
        self.assertIn(Path(__file__), list((REPO / 'src').glob('test_*.py')))


if __name__ == '__main__':
    unittest.main()
