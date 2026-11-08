#!/usr/bin/env python3
"""Dependency-table and classifier regressions (synthetic inputs only).

The source guard and seed matrix (L-spec-2307/2308) are not supplied here.
FixtureContentTests exercise each declared non-ledger input against the
production reader with fixture files of their own; they do not certify closure
completeness or the cross-subject seed matrix.
"""
import ast
import atexit
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
import importlib
import os
import shutil
from pathlib import Path
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

# Establish the data boundary before importing any production module. Inherited
# locations are not fixture inputs, including an inherited lane-rule table.
for _key in tuple(os.environ):
    if _key.startswith(('DOIT_', 'SUPABASE_DB_URL')) or _key in (
            'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
        os.environ.pop(_key)
_sandbox = tempfile.TemporaryDirectory(prefix='door-deps-')
atexit.register(_sandbox.cleanup)
SANDBOX = Path(_sandbox.name).resolve()
os.environ.update(HOME=str(SANDBOX / 'home'), DOIT_ROOT=str(SANDBOX / 'data'),
                  DOIT_SESSIONS_DIR=str(SANDBOX / 'sessions'))
for _directory in ('home', 'data/content', 'sessions'):
    (SANDBOX / _directory).mkdir(parents=True)
sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parent
STDLIB = Path(sysconfig.get_path('stdlib')).resolve()


def _audit(operation, arguments):
    if operation in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'):
        raise PermissionError('forbidden-operation: subprocess')
    if operation.startswith('socket.'):
        raise PermissionError('forbidden-operation: network')
    if operation != 'open' or isinstance(arguments[0], int):
        return
    path = Path(os.fsdecode(arguments[0])).resolve()
    if path.is_relative_to(SANDBOX):
        return
    mode, flags = arguments[1:3]
    writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    source = path.is_relative_to(SOURCE) and path.suffix in ('.py', '.pyc')
    fixture = path.is_relative_to(SOURCE / 'fixtures' / 'door_seed')
    library = (path.is_relative_to(STDLIB)
               and not {'site-packages', 'dist-packages'} & set(path.parts)
               and path.suffix in ('.py', '.pyc', '.so'))
    if not writing and (source or fixture or library or path in (REPO / 'lane_rules.toml', REPO / 'look.toml')):
        return
    raise PermissionError('forbidden-operation: out-of-root file')


sys.addaudithook(_audit)

import fold
import dispatch
import ledger_files


def _forbidden(operation):
    def reject(*args, **kwargs):
        raise PermissionError('forbidden-operation: ' + operation)
    return reject


_guards = ExitStack()
atexit.register(_guards.close)
for _module, _names in ((fold, ('read_events', 'append')),
                       (ledger_files, ('iter_sources', 'read_text')),
                       (dispatch, ('main', 'emit', 'run_claude', 'run_codex_exec',
                                   'run_codex', 'run_seat', 'poke'))):
    for _name in _names:
        _guards.enter_context(patch.object(_module, _name,
                              _forbidden(_module.__name__ + '.' + _name)))

import dependency_holds
import door_deps as deps
import lane_rules


class ContractTests(unittest.TestCase):
    def test_classes_and_record(self):
        self.assertEqual(deps.ACTION_CLASSES, (
            'next-step', 'build-start', 'grade', 'review', 'merge', 'close',
            'hold-release', 'capability-release', 'wave-order', 'charter-close'))
        self.assertEqual(set(deps.TABLE), set(deps.ACTION_CLASSES))
        self.assertEqual([f.name for f in fields(deps.Deps)], [
            'own_kinds', 'mention_kinds', 'global_kinds', 'ref_kinds',
            'spawn_kinds', 'ledger_wide_sets', 'membership',
            'same_ts_neighbours', 'answer', 'non_ledger_inputs'])
        for action, row in deps.TABLE.items():
            with self.assertRaises(FrozenInstanceError):
                row.answer = 'other'
            for field in fields(row):
                value = getattr(row, field.name)
                if isinstance(value, tuple):
                    self.assertEqual(len(value), len(set(value)), (action, field.name))
                    self.assertTrue(all(isinstance(k, str) and k and '*' not in k
                                        for k in value))
                    print(action, field.name, ','.join(value))
            self.assertTrue(row.same_ts_neighbours)

    def test_source_symbols_and_selectors(self):
        root = Path(__file__).resolve().parent
        for action, row in deps.TABLE.items():
            expected = ('fold.capability_holds' if action == 'capability-release'
                        else 'nextstep._wave' if action == 'wave-order'
                        else 'fold.closable' if action == 'charter-close'
                        else 'nextstep.step_for')
            self.assertEqual(row.answer, expected)
            module, name = row.answer.split('.')
            answer = getattr(importlib.import_module(module), name)
            self.assertTrue(callable(answer))
            self.assertEqual((answer.__module__, answer.__name__), (module, name))
            print(action, 'answer', row.answer, 'callable')
            for symbol in (row.answer, *deps.SOURCE_SYMBOLS[action]):
                module, name = symbol.split('.')
                tree = ast.parse((root / (module + '.py')).read_text())
                self.assertIn(name, {node.name for node in tree.body
                                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))})
            for key in row.non_ledger_inputs:
                selector, symbols = deps.NON_LEDGER_INPUTS[key]
                self.assertTrue(selector)
                for symbol in symbols:
                    module, name = symbol.split('.')
                    tree = ast.parse((root / (module + '.py')).read_text())
                    self.assertIn(name, {node.name for node in tree.body
                                        if isinstance(node, ast.FunctionDef)})
        self.assertEqual(set(deps.NON_LEDGER_INPUTS), {
            'content:spec-text', 'content:spec-membership', 'content:grader-script',
            'content:plan', 'repo:lane-rules'})

    def test_shared_item_answer_union(self):
        for action in deps.ACTION_CLASSES[:7]:
            self.assertEqual(deps.TABLE[action], deps.TABLE['next-step'])
            row = deps.TABLE[action]
            self.assertTrue({'spawn-started', 'build-started', 'spawn-done',
                             'spawn-failed', 'spawn-stale', 'seat-claimed'}
                            <= set(row.spawn_kinds))
            self.assertEqual(row.ref_kinds, ('decision', 'unblocked'))
            self.assertIn('shipped', row.global_kinds)
            self.assertIn('shipped', row.ledger_wide_sets)
            self.assertEqual(row.membership, ('charter', 'wave'))
        self.assertIn('content:plan', deps.TABLE['wave-order'].non_ledger_inputs)
        self.assertIn('spec-written', deps.TABLE['wave-order'].mention_kinds)

    def test_declared_kinds_are_existing_source_literals(self):
        # A vocabulary check, not the pending source-read dependency guard.
        literals = set()
        for path in Path(__file__).resolve().parent.rglob('*.py'):
            if (path.name == 'door_deps.py' or path.name.startswith('test_')
                    or path.name.endswith('_test.py') or '_checks_' in path.name
                    or {'test', 'tests'} & set(path.parts)):
                continue
            tree = ast.parse(path.read_text())
            literals.update(node.value for node in ast.walk(tree)
                            if isinstance(node, ast.Constant)
                            and isinstance(node.value, str))
        for action, row in deps.TABLE.items():
            kinds = set(row.own_kinds + row.mention_kinds + row.global_kinds
                        + row.ref_kinds + row.spawn_kinds)
            self.assertEqual(kinds - literals, set(), action)


class ClassifyTests(unittest.TestCase):
    def event(self, kind, **fields):
        return dict(type=kind, subject='L-spec-9901', actor='operator',
                    ts='2026-01-01T00:00:00Z', project='fixture',
                    _src='L-operator-9901.jsonl:1', **fields)

    def supported(self, kind):
        event = self.event(kind)
        selectors = {
            'correction': dict(ref='fixture.jsonl:1', set={'type': 'voided'}),
            'capability-hold': dict(subject='capability:fixture'),
            'dependency-hold': dict(waits_for='L-spec-9902'),
            'queue-add': dict(branch='fixture'),
            'queue-batch': dict(specs='L-spec-9902,L-spec-9903'),
            'window-redcut': dict(window='fixture', returned=['L-spec-9902']),
        }
        event.update(selectors.get(kind, {}))
        return event

    def test_routes_and_precedence(self):
        cases = [
            (self.event('spec-amended'), 'own'),
            (self.event('spec-written', charter='L-charter-9901', footprint=['src/a.py']), 'sibling'),
            (self.event('dependency-hold', waits_for='L-spec-9902'), 'mention'),
            (self.event('decision', ref='L-builder-9901.jsonl:1'), 'ref'),
            (self.event('decision', ref='L-builder-9901.jsonl:1',
                        ruling={'kind': 'rebuild', 'spec': 'L-spec-9902'}), 'global'),
            (self.event('shipped'), 'global'),
            (self.event('queue-add', branch='fixture'), 'global'),
            (self.event('spawn-done', spawn='L-builder-9901'), 'spawn'),
            (self.event('correction', ref='L-builder-9901.jsonl:1',
                        set={'type': 'shipped', 'subject': 'L-spec-9902',
                             'charter': 'L-charter-9902'}), 'global'),
        ]
        for action in deps.ACTION_CLASSES[:7]:
            for event, role in cases:
                with self.subTest(action=action, event=event):
                    before = deepcopy(event)
                    self.assertEqual(deps.classify(event, action), role)
                    self.assertEqual(event, before)
                    print(action, event['type'], role)
            self.assertEqual(deps.classify(dict(type='spawn-stale', spawn='L-builder-9901'), action), 'spawn')
            self.assertEqual(deps.classify(dict(type='seat-claimed', subject='L-builder-9901'), action), 'spawn')
            capability = dict(type='decision', subject='capability:fixture',
                              actor='thinker', ref='elsewhere.jsonl:1')
            self.assertEqual(deps.classify(capability, action), 'global')
            self.assertEqual(deps.classify(dict(type='capability-hold',
                                               subject='capability:fixture'), action), 'global')

    def test_unknown_is_conservative(self):
        bad = [None, [], {}, dict(type=[]), dict(type='new-kind', subject='L-spec-9901'),
               dict(type='spec-amended'), dict(type='spec-amended', subject=''),
               self.event('decision', ref=[]), self.event('decision', ruling={}),
               self.event('unblocked', ruling={'kind': 'rebuild', 'spec': 'L-spec-9902'}),
               self.event('correction', ref='file:1', set=[]),
               self.event('correction', ref='file:1', set={'actor': 'thinker'}),
               self.event('spawn-done', spawn=[]),
               self.event('seat-claimed'),
               self.event('spec-written', footprint='src/a.py'),
               self.event('dependency-hold', waits_for=['L-spec-9902']),
               self.event('capability-hold')]
        for action in deps.ACTION_CLASSES:
            for event in bad:
                with self.subTest(action=action, event=event):
                    self.assertEqual(deps.classify(event, action), 'unknown')
        for action in ('new-action', None, [], {}):
            self.assertEqual(deps.classify(self.event('spec-written'), action), 'unknown')

    def test_every_declared_kind_has_a_supported_shape(self):
        for action, row in deps.TABLE.items():
            kinds = set(row.own_kinds + row.mention_kinds + row.global_kinds
                        + row.ref_kinds + row.spawn_kinds)
            for kind in sorted(kinds):
                event = self.supported(kind)
                if kind == 'correction':
                    event.update(ref='fixture.jsonl:1', set={'type': 'voided'})
                elif kind == 'capability-hold':
                    event.update(subject='capability:fixture')
                elif kind in row.spawn_kinds:
                    event.update(spawn='L-builder-9901')
                elif kind in ('decision', 'unblocked') and kind not in row.own_kinds:
                    event.update(subject='capability:fixture')
                elif kind in row.mention_kinds and kind not in row.own_kinds:
                    event.update(charter='L-charter-9901')
                self.assertIn(deps.classify(event, action),
                              ('own', 'mention', 'global', 'ref', 'spawn', 'sibling'),
                              (action, event))

    def test_each_route_declaration_and_precedence(self):
        # Exercise each route entry, retaining overlaps in the declarations.
        # Expected results follow the documented selectors and precedence,
        # independently of the classifier implementation.
        roles = ('own', 'mention', 'global', 'ref', 'spawn')
        for action, row in deps.TABLE.items():
            for declared_role in roles:
                for kind in getattr(row, declared_role + '_kinds'):
                    event = self.supported(kind)
                    if kind in row.spawn_kinds:
                        event['spawn'] = 'L-builder-9901'
                    if declared_role == 'ref':
                        event['ref'] = 'elsewhere.jsonl:1'
                    if declared_role == 'mention' and kind in ('spec-written', 'spec-carried'):
                        event['charter'] = 'L-charter-9901'
                    if declared_role == 'global' and kind in ('decision', 'unblocked'):
                        event['subject'] = 'capability:fixture'
                    if kind in row.global_kinds and (kind not in ('decision', 'unblocked')
                                                    or event['subject'].startswith('capability:')):
                        expected = 'global'
                    elif declared_role == 'ref':
                        expected = 'ref'
                    elif kind in row.spawn_kinds:
                        expected = 'spawn'
                    elif 'charter' in event and row.membership:
                        expected = 'sibling'
                    elif kind == 'dependency-hold':
                        expected = 'mention' if kind in row.mention_kinds else 'own'
                    else:
                        expected = 'own'
                    with self.subTest(action=action, route=declared_role, kind=kind):
                        original = deepcopy(event)
                        self.assertEqual(deps.classify(event, action), expected)
                        self.assertEqual(event, original)
                        print(action, declared_role + ':' + kind, expected)

    def test_structural_routes_for_every_class(self):
        for action, row in deps.TABLE.items():
            cases = [
                (dict(type='decision', actor='thinker',
                      ruling={'kind': 'rebuild', 'spec': 'L-spec-9902'},
                      ref='foreign.jsonl:1'),
                 'global' if 'decision' in row.global_kinds else 'unknown'),
                (dict(type='capability-hold', subject='capability:fixture'),
                 'global' if 'capability-hold' in row.global_kinds else 'unknown'),
                (dict(type='unblocked', subject='capability:fixture', ref='foreign.jsonl:1'),
                 'global' if 'unblocked' in row.global_kinds else 'unknown'),
                (dict(type='spawn-stale', spawn='L-builder-9901', subject=''),
                 'spawn' if 'spawn-stale' in row.spawn_kinds else 'unknown'),
                (dict(type='seat-claimed', subject='L-builder-9901'),
                 'spawn' if 'seat-claimed' in row.spawn_kinds else 'unknown'),
                (self.event('spec-written', charter='L-charter-9901', footprint=['src/a.py']),
                 'sibling' if row.membership else 'unknown'),
            ]
            for event, expected in cases:
                with self.subTest(action=action, event=event):
                    self.assertEqual(deps.classify(event, action), expected)
            # An ordinary answer is ref-scoped even with a spawn selector; a
            # ruling/capability shape wins first. The raw tuples keep both routes.
            if 'decision' in row.ref_kinds:
                self.assertEqual(deps.classify(self.event('decision', ref='foreign.jsonl:1',
                                                         spawn='L-builder-9901'), action), 'ref')
            if 'shipped' in row.global_kinds:
                self.assertEqual(deps.classify(self.event('shipped', ref='foreign.jsonl:1',
                                                        spawn='L-builder-9901'), action), 'global')
            if row.membership:
                # Footprint-only and resolved-charter-only membership selectors.
                for fields in (dict(footprint=['src/a.py']), dict(charter='L-charter-9901')):
                    self.assertEqual(deps.classify(self.event('spec-written', **fields), action), 'sibling')
                # The folded spec's charter can come from a later event even
                # when that event is not the wave resolver's spec-written.
                self.assertEqual(deps.classify(self.event('spec-amended',
                    charter='L-charter-9902'), action), 'sibling')

    def test_malformed_dependency_selectors(self):
        for action in deps.ACTION_CLASSES:
            for event in (
                    self.event('dependency-hold', waits_for='unresolved'),
                    self.event('dependency-hold', waits_for='L-spec-9902,L-spec-9903'),
                    self.event('queue-add', branch=[]),
                    self.event('queue-batch', specs=['L-spec-9902']),
                    self.event('queue-batch', specs='L-spec-9902,,L-spec-9903'),
                    self.event('window-redcut', returned='L-spec-9902'),
                    self.event('window-redcut', returned=[{}]),
                    self.event('spawn-done', spawn='L-builder-9901') | {'subject': []},
                    self.event('spec-amended', charter={}),
                    self.event('spec-written', footprint=[42])):
                with self.subTest(action=action, event=event):
                    self.assertEqual(deps.classify(event, action), 'unknown')

    def test_missing_dependency_selectors(self):
        for action in deps.ACTION_CLASSES:
            for kind in ('dependency-hold', 'queue-add', 'queue-batch', 'window-redcut'):
                self.assertEqual(deps.classify(self.event(kind), action), 'unknown')


class FixtureContentTests(unittest.TestCase):
    """AC1: content creation, absence, edits and removal move the answer.

    Each case drives the production reader named by NON_LEDGER_INPUTS against a
    fixture directory under the sandbox, so the declared key is an input in
    fact, not a relabelled ledger line.
    """
    SID, DEP, OTHER = 'L-spec-9901', 'L-spec-9902', 'L-spec-9903'

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=SANDBOX))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def ev(self, kind, **extra):
        return dict(type=kind, subject=self.SID, actor='operator', project='fixture',
                    ts='2026-01-01T00:00:00Z', _src='L-operator-9901.jsonl:1', **extra)

    def test_inputs_are_declared_for_these_rows(self):
        for action in ('next-step', 'build-start', 'grade', 'review', 'merge',
                       'close', 'hold-release'):
            self.assertEqual(set(deps.TABLE[action].non_ledger_inputs),
                             set(deps.NON_LEDGER_INPUTS))
        self.assertEqual(deps.TABLE['wave-order'].non_ledger_inputs, ('content:plan',))
        for action in ('capability-release', 'charter-close'):
            self.assertEqual(deps.TABLE[action].non_ledger_inputs, ())

    def test_spec_text(self):
        path = self.dir / (self.SID + '.md')
        events = [self.ev('spec-written')]

        def reason():
            with patch.object(dispatch, 'CONTENT', self.dir):
                return dependency_holds.text_hold_reason(events, events)
        self.assertIsNone(reason())                                  # absent
        path.write_text('# spec\n')
        self.assertIsNone(reason())                                  # no declaration
        path.write_text('Dependency-hold: waits_for=%s\n' % self.DEP)
        created = reason()                                           # created/edited
        self.assertIn(self.DEP, created)
        path.write_text('Dependency-hold: waits_for=%s\n' % self.OTHER)
        changed = reason()                                           # text changed
        self.assertIn(self.OTHER, changed)
        self.assertNotEqual(created, changed)
        path.write_text('Dependency-hold: waits_for=nonsense\n')
        self.assertIn('invalid', reason())                           # malformed is held
        path.unlink()
        self.assertIsNone(reason())                                  # removed

    def held(self):
        events = [self.ev('spec-written'),
                  dict(self.ev('spec-written'), subject=self.OTHER),
                  dict(self.ev('capability-hold', capability='git'),
                       subject='capability:git')]
        return fold.held_specs(events, content_dir=self.dir)

    def test_spec_membership(self):
        path = self.dir / (self.SID + '.md')
        self.assertEqual(self.held(), set())                         # absent
        path.write_text('AC1 [backend]: x\ncheck: y\nreview_path: do git log\n')
        self.assertEqual(self.held(), {self.SID})                    # created
        path.write_text('AC1 [backend]: x\ncheck: y\nreview_path: do read\n')
        self.assertEqual(self.held(), set())                         # text changed
        path.write_text('AC1 [backend]: x\ncheck: y\nreview_path: do git log\n')
        (self.dir / (self.OTHER + '.md')).write_text(path.read_text())
        self.assertEqual(self.held(), {self.SID, self.OTHER})        # listing grows
        path.unlink()
        self.assertEqual(self.held(), {self.OTHER})                  # removed

    def test_grader_script(self):
        (self.dir / (self.SID + '.md')).write_text('AC1 [backend]: x\n')
        script = self.dir / ('verify-%s-grader.sh' % self.SID)
        self.assertEqual(self.held(), set())                         # absent
        script.write_text('git log\n')
        self.assertEqual(self.held(), {self.SID})                    # created
        script.write_text('true\n')
        self.assertEqual(self.held(), set())                         # text changed
        script.write_text('git log\n')
        script.unlink()
        self.assertEqual(self.held(), set())                         # removed

    def test_plan(self):
        plan = self.dir / 'plan-L-charter-9901.md'
        footprint = ['src/a.py']

        def wave(charter='L-charter-9901'):
            with patch.object(dispatch, 'CONTENT', self.dir):
                return dispatch.spec_wave(charter, footprint)
        unit = '## unit\nFootprint:\n- src/a.py\nWave: %d\n'
        self.assertIsNone(wave())                                    # absent
        plan.write_text(unit % 1)
        self.assertEqual(wave(), 1)                                  # created
        plan.write_text(unit % 2)
        self.assertEqual(wave(), 2)                                  # text changed
        self.assertIsNone(wave('L-charter-9902'))                    # keyed by resolved charter
        plan.unlink()
        self.assertIsNone(wave())                                    # removed

    def test_lane_rules(self):
        path = self.dir / 'lane_rules.toml'
        with self.assertRaises(ValueError):
            lane_rules.load(path)                                    # absent
        shutil.copy(lane_rules.DEFAULT_TABLE, path)
        created = lane_rules.load(path)                              # created
        self.assertEqual(created, lane_rules.load(lane_rules.DEFAULT_TABLE))
        path.write_text(path.read_text().replace('limit_min = 30', 'limit_min = 31', 1))
        self.assertNotEqual(lane_rules.load(path), created)          # text changed
        path.unlink()
        with self.assertRaises(ValueError):
            lane_rules.load(path)                                    # removed


class IsolationTests(unittest.TestCase):
    def test_roots(self):
        self.assertTrue(fold.ROOT.is_relative_to(SANDBOX))
        self.assertTrue(dispatch.ROOT.is_relative_to(SANDBOX))
        self.assertTrue(dispatch.CONTENT.is_relative_to(SANDBOX))
        self.assertFalse(any(dispatch.CONTENT.iterdir()))
        self.assertNotIn('DOIT_LANE_RULES', os.environ)

    def test_forbidden_operations(self):
        import socket
        import subprocess
        operations = {
            'fold.read_events': fold.read_events,
            'fold.append': lambda: fold.append('spec-written', 'L-spec-9901'),
            'ledger_files.iter_sources': lambda: ledger_files.iter_sources(fold.EVENTS),
            'ledger_files.read_text': lambda: ledger_files.read_text(fold.EVENTS, 'synthetic.jsonl'),
            'dispatch.main': lambda: dispatch.main([]),
            'out-of-root config': lambda: (REPO / 'synthetic-forbidden.toml').read_text(),
            'network': socket.socket,
            'subprocess': lambda: subprocess.run(['true'], check=True),
        }
        for operation, call in operations.items():
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(PermissionError, 'forbidden-operation'):
                    call()
                print(operation, 'caught forbidden-operation')


if __name__ == '__main__':
    unittest.main()
