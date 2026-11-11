#!/usr/bin/env python3
"""Dependency-table and classifier regressions (synthetic inputs only).

The seed matrix (L-spec-2307) is SeedTests; the source guard (L-spec-2308) is
SourceGuardTests, and IsolationTests holds the isolated-invocation controls.
FixtureContentTests exercise each declared non-ledger input against the
production reader with fixture files of their own; they do not certify closure
completeness or the cross-subject seed matrix.
"""
import ast
import atexit
import collections
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
import importlib
import os
import re
import shutil
from pathlib import Path
import sys
import sysconfig
import tempfile
import unittest
import zoneinfo
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
# Read-only system time-zone data: tg_say builds a ZoneInfo at import.
TZDATA = tuple(Path(p).resolve() for p in zoneinfo.TZPATH)


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
    tzdata = any(path.is_relative_to(t) for t in TZDATA)
    if not writing and (source or fixture or library or tzdata or path in (REPO / 'lane_rules.toml', REPO / 'look.toml')):
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


import json
from datetime import datetime


def _expected_cells(action_class):
    """Every cross-subject dependency a row declares, as route:kind or structural key."""
    row = deps.TABLE[action_class]
    cells = {'global:' + k for k in row.global_kinds} | {'ref:' + k for k in row.ref_kinds}
    cells |= {'spawn:' + k for k in row.spawn_kinds}
    cells |= {'mention:dependency-hold'} if 'dependency-hold' in row.mention_kinds else set()
    cells |= {'sibling:' + k for k in row.mention_kinds if k in ('spec-written', 'spec-carried')}
    cells |= {'membership:' + k for k in row.membership}
    cells |= {'ledger-wide:' + k for k in row.ledger_wide_sets}
    cells |= {'file:' + k for k in row.non_ledger_inputs}
    return cells | ({'same-ts'} if row.same_ts_neighbours else set())


# Every declared cell has a witness. A cell that loses its witness fails the
# matrix test; there is no gap allowlist. Notes on how cells are witnessed:
#   file:content:grader-script / spec-membership are read by fold.held_specs,
#     which merge_queue.queue and autodispatch consult; step_for does not call
#     it, so their controls ask held_specs itself (answer kind held_specs);
#   global:decision is witnessed by a ruling-bearing decision (rulings.project_event
#     projects it onto another spec); global:unblocked / capability-hold by
#     capability-subject events read through the spec-local regrade rule.
GAPS = {}


def materialize_content(directory, content_files):
    """Write synthetic content files beneath a sandbox directory and nowhere else.

    Names are relative POSIX paths. Absolute paths, '..' or empty components,
    backslashes and any symlink on the way (or at the target) are refused before
    anything is written; a refusal leaves the directory exactly as it was.
    """
    root = Path(directory).resolve()
    if not root.is_relative_to(SANDBOX):
        raise ValueError('content directory is outside the temporary root')
    planned = []
    for name, text in content_files.items():
        if text is None:
            continue
        if not isinstance(name, str) or not isinstance(text, str):
            raise ValueError('content entry is not name -> text')
        parts = name.split('/')
        if (not name or name.startswith(('/', '~')) or '\\' in name
                or any(part in ('', '.', '..') for part in parts)):
            raise ValueError('unsafe content path: %r' % (name,))
        walk = root
        for part in parts:
            walk = walk / part
            if walk.is_symlink():
                raise ValueError('content path crosses a symlink: %r' % (name,))
        if not walk.resolve().is_relative_to(root):
            raise ValueError('content path escapes the temporary root: %r' % (name,))
        planned.append((walk, text))
    for target, text in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


class _Evaluator:
    """Normalize a ledger and ask the production answer functions, at a frozen clock."""

    def __init__(self, content_files):
        self.dir = Path(tempfile.mkdtemp(dir=SANDBOX))
        materialize_content(self.dir, content_files)
        self.rules = self.dir / 'lane_rules.toml' if 'lane_rules.toml' in content_files else None

    def close(self):
        shutil.rmtree(self.dir, True)

    def answer(self, kind, raw, item, now):
        import nextstep
        import tick
        events = fold.finish_events([dict(e) for e in raw])
        env = {'DOIT_LANE_RULES': str(self.rules)} if self.rules else {}
        seen = {}
        real_wave, real_next = nextstep._wave, nextstep.next_steps

        def wave(sid, *args):
            seen[sid] = real_wave(sid, *args)
            return seen[sid]

        def next_steps(ev, specs, when, rules, waves=None, mains=None):
            seen['waves'] = dict(waves or {})
            return real_next(ev, specs, when, rules, waves, mains)
        with patch.object(dispatch, 'CONTENT', self.dir), patch.dict(os.environ, env), \
                patch.object(nextstep, '_wave', wave), patch.object(nextstep, 'next_steps', next_steps):
            if kind == 'capability_holds':
                return {s: (r['capability'], sorted(r['specs']))
                        for s, r in sorted(fold.capability_holds(events).items())}
            if kind == 'closable':
                return dict(fold.closable(events, item))
            if kind == 'held_specs':
                return sorted(fold.held_specs(events, content_dir=self.dir))
            specs = fold.fold(events)[0]
            steps = tick._steps(events, specs, datetime.fromisoformat(now.replace('Z', '+00:00')))
            if kind == 'wave':
                return {'blocker': seen.get(item), 'derived_wave': seen['waves'].get(item),
                        'waves': dict(sorted(seen['waves'].items()))}
            return [s for s in steps if s.item == item]


class SeedTests(unittest.TestCase):
    """AC4: committed single-event mutations change the answer production gives."""
    CLASSES = deps.ACTION_CLASSES
    results = []

    def run_row(self, row, seed, content=None):
        """(baseline, included, omitted-selection result) for one seed."""
        ev = _Evaluator(seed.content_files if content is None else content)
        self.addCleanup(ev.close)
        base = list(seed.base_events)
        full = base + [seed.event]
        item, kind, now = seed.item_id, row['answer'], seed.now
        baseline = ev.answer(kind, base, item, now)
        mutated = ev.answer(kind, full, item, now)
        # The dependency-selected input: drop another subject's own-route lines,
        # keep every line the table gives a cross-subject role and every unknown.
        cls = row['classes'][0]
        picked = [e for e in full if e is seed.event or e.get('subject') == item
                  or deps.classify(e, cls) != 'own'
                  or e.get('charter') or e.get('subject') in ('fixture',)]
        selected = ev.answer(kind, picked, item, now)
        return baseline, mutated, selected

    def test_interface(self):
        with self.assertRaises(ValueError):
            deps.seeds('nope')
        self.assertEqual([f.name for f in fields(deps.Seed)], [
            'name', 'item_id', 'dependency_kind', 'base_events', 'event', 'now', 'content_files'])
        self.assertNotIn('waves', [f.name for f in fields(deps.Seed)])
        for action in deps.ACTION_CLASSES:
            first, second = deps.seeds(action), deps.seeds(action)
            self.assertTrue(first, action)
            self.assertEqual(first, second)
            for a, b in zip(first, second):
                self.assertIsNot(a.event, b.event)
                self.assertIsNot(a.content_files, b.content_files)
                for x, y in zip(a.base_events, b.base_events):
                    self.assertIsNot(x, y)
                self.assertNotIn(a.event['_src'], {e['_src'] for e in a.base_events})
                datetime.fromisoformat(a.now.replace('Z', '+00:00'))
                self.assertEqual(a.event['actor'], fold.actor_of(a.event['_src'].split(':')[0]))
                self.assertTrue(a.dependency_kind and a.item_id)
            first[0].base_events[0]['type'] = 'tampered'      # contaminate one call
            first[0].content_files['x.md'] = 'tampered'
            self.assertNotEqual(deps.seeds(action)[0].base_events[0]['type'], 'tampered')
            self.assertNotIn('x.md', deps.seeds(action)[0].content_files)

    def test_fixture_loader_refuses_unsafe_input(self):
        for bad in ('../x', 'a/b', '', '.hidden', 'x..y'):
            with self.assertRaises(ValueError):
                deps.scenario_ledgers(bad)
        with self.assertRaises(ValueError):
            deps.make_event({}, 'L-thinker-1.jsonl', dict(type='x', actor='operator'))
        with self.assertRaises(ValueError):
            deps.make_event({}, '../L-thinker-1.jsonl', dict(type='x'))

    def test_baseline_mutated_omitted(self):
        print('class | seed | dependency key | derived waves | baseline | mutated | omitted')
        for action in deps.ACTION_CLASSES:
            for row, seed in zip(deps.seed_rows(action), deps.seeds(action)):
                with self.subTest(action=action, seed=seed.name):
                    baseline, mutated, selected = self.run_row(row, seed)
                    omitted = baseline
                    brief = lambda v: ([(s.state, s.action, s.reason[:44]) for s in v] if isinstance(v, list) and v and hasattr(v[0], 'state') else v)
                    print(action, '|', seed.name, '|', seed.dependency_kind, '|', 'baseline', brief(baseline),
                          '| mutated', brief(mutated), '| omitted', brief(omitted))
                    if row['expect'] == 'same':
                        self.assertEqual(mutated, omitted, 'control must not move the answer')
                    else:
                        self.assertNotEqual(mutated, omitted, 'omitting the mutation must change the answer')
                    self.assertEqual(selected, mutated, 'dependency-selected input must agree with the full fixture')


    def evaluate(self, kind, seed, item, content, spy=None):
        ev = _Evaluator(content)
        self.addCleanup(ev.close)
        return ev.answer(kind, list(seed.base_events), item, seed.now)

    def test_waits_for_text_hold(self):
        seed = next(s for s in deps.seeds('hold-release') if s.name == 'text-shipped')
        declared = dict(seed.content_files)
        self.assertIn('Dependency-hold: waits_for=L-spec-9003', declared['L-spec-9001.md'])
        held = self.evaluate('step', seed, seed.item_id, declared)[0]
        self.assertEqual(held.state, 'hold')
        self.assertIn('L-spec-9003', held.clears)
        released = _Evaluator(declared)
        self.addCleanup(released.close)
        after = released.answer('step', list(seed.base_events) + [seed.event], seed.item_id, seed.now)[0]
        self.assertEqual((after.state, after.action), ('action', 'build'))
        self.assertNotEqual(held, after)
        # With the declaration removed the baseline is unheld: the file mattered.
        stripped = {k: v.replace('Dependency-hold: waits_for=L-spec-9003\n', '') for k, v in declared.items()}
        unheld = self.evaluate('step', seed, seed.item_id, stripped)[0]
        self.assertEqual((unheld.state, unheld.action), ('action', 'build'))
        self.assertEqual(unheld, after.__class__(**{**after.__dict__, 'trigger': unheld.trigger,
                                                    'since': unheld.since, 'stage': unheld.stage}))
        print('waits_for: held', held.reason, '| released by shipped ->', after.action,
              '| declaration removed ->', unheld.action)

    def test_production_derived_waves(self):
        import nextstep
        calls = []
        real = dispatch.spec_wave

        def spy(charter, footprint):
            calls.append((charter, tuple(footprint or ())))
            return real(charter, footprint)
        rows = {r['name']: r for r in deps.seed_rows('wave-order')}
        by_name = {s.name: s for s in deps.seeds('wave-order')}
        with patch.object(dispatch, 'spec_wave', spy):
            for name in ('wave-footprint-corr-wave', 'wave-charter-corr-wave', 'wave-sibling-written-wave'):
                seed, calls[:] = by_name[name], []
                ev = _Evaluator(seed.content_files)
                self.addCleanup(ev.close)
                before = ev.answer('wave', list(seed.base_events), seed.item_id, seed.now)
                during = len(calls)
                after = ev.answer('wave', list(seed.base_events) + [seed.event], seed.item_id, seed.now)
                self.assertGreater(during, 0, 'baseline did not call dispatch.spec_wave')
                self.assertGreater(len(calls), during, 'mutation did not re-derive waves')
                self.assertNotEqual(before, after)
                print('waves', name, 'baseline', before, 'mutated', after)
            # Plan text only, ledger fixed: the derived wave moves.
            control = next(c for c in deps.file_controls() if c['name'] == 'ctl-plan-text')
            seed = by_name['wave-footprint-corr-wave']
            plan = dict(seed.content_files)
            ev = _Evaluator(plan)
            self.addCleanup(ev.close)
            first = ev.answer('wave', list(seed.base_events), seed.item_id, seed.now)
            plan[control['path']] = control['text']
            ev = _Evaluator(plan)
            self.addCleanup(ev.close)
            second = ev.answer('wave', list(seed.base_events), seed.item_id, seed.now)
            self.assertNotEqual(first['waves'], second['waves'])
            self.assertIsNone(first['waves'] and second['blocker'], 'later sibling wave releases the target')
            print('plan-only control: waves', first['waves'], '->', second['waves'])
        self.assertNotIn('waves', [f.name for f in fields(deps.Seed)])

    def test_file_controls(self):
        for control in deps.file_controls():
            with self.subTest(control=control['name']):
                files = deps.scenario_content(control['scenario'])
                base = deps.scenario_ledgers(control['scenario'])
                raw = [e for rows in base.values() for e in rows]
                text = control['text']
                if control.get('derive') == 'lane-rules-owner':
                    text = lane_rules.DEFAULT_TABLE.read_text().replace('actor = "tick"', 'actor = "executor"', 1)
                    self.assertNotEqual(text, lane_rules.DEFAULT_TABLE.read_text())
                changed = dict(files)
                changed[control['path']] = text
                if text is None:
                    changed.pop(control['path'])
                results = []
                for content in (files, changed):
                    ev = _Evaluator(content)
                    self.addCleanup(ev.close)
                    results.append(ev.answer(control['answer'], raw, control['item_id'], '2026-01-02T00:00:00Z'))
                print('file control', control['key'], control['name'], '|', results[0], '->', results[1])
                self.assertNotEqual(results[0], results[1], 'the file edit must move the answer')

    def test_wave_row_omits_sibling_terminals(self):
        """Finding: a sibling's terminal event changes nextstep._wave yet the
        wave-order row classifies it as 'own' (not selected by a target-id
        closure). Recorded so the table owner decides; the Door must read fully."""
        for name in ('wave-shipped-wave', 'wave-killed-wave', 'wave-closed-wave'):
            seed = next(s for s in deps.seeds('wave-order') if s.name == name)
            self.assertEqual(deps.classify(seed.event, 'wave-order'), 'own')
            self.assertNotIn(seed.event['type'], deps.TABLE['wave-order'].global_kinds)

    def test_coverage_matrix(self):
        covered, matrix = {}, {}
        for action in deps.ACTION_CLASSES:
            expected = _expected_cells(action)
            witnessed = {}
            for row in deps.seed_rows(action):
                if row['expect'] != 'changed':
                    continue
                for cell in row['cells']:
                    witnessed.setdefault(cell, []).append(row['name'])
            for control in deps.file_controls():
                if action in control['classes']:
                    witnessed.setdefault('file:' + control['key'], []).append(control['name'])
            self.assertFalse(set(witnessed) - expected, (action, sorted(set(witnessed) - expected)))
            covered[action] = witnessed
            gaps = sorted(expected - set(witnessed))
            matrix[action] = gaps
            print('matrix', action, 'cells', len(expected), 'witnessed', len(witnessed), 'gaps', ','.join(gaps) or '-')
            for cell, names in sorted(witnessed.items()):
                print('  ', cell, '<-', ','.join(sorted(names)))
        self.assertEqual(matrix, {a: sorted(GAPS.get(a, ())) for a in deps.ACTION_CLASSES},
                         'an unwitnessed cell must be listed in GAPS, and a listed gap must still be a gap')



# ---------------------------------------------------------------------------
# Source guard (L-spec-2308): conservative AST read analysis over non-test src.
# Reads syntax only: nothing scanned is imported or executed.
# ---------------------------------------------------------------------------
import builtins as _builtins

FIXTURES = deps.FIXTURES

SEED_PARAMS = frozenset(('events', 'evs', 'all_ev', 'all_evs', 'all_events',
                         'spec_events', 'spec_evs'))
TARGET_FORMALS = frozenset(('item', 'item_id', 'sid', 'subject', 'spec'))
READERS = frozenset(('fold.read_events', 'fold.finish_events'))
JOIN_FIELDS = frozenset(('ref', 'spawn'))
_RECORD_METHODS = frozenset((
    'get', 'items', 'values', 'keys', 'append', 'extend', 'add', 'update', 'setdefault',
    'insert', 'pop', 'popitem', 'remove', 'discard', 'clear', 'copy', 'sort', 'reverse',
    'index', 'count', 'join', 'split', 'rsplit', 'strip', 'lstrip', 'rstrip', 'lower',
    'upper', 'startswith', 'endswith', 'format', 'difference', 'union', 'intersection',
    'issubset', 'issuperset', 'isdisjoint', 'symmetric_difference', 'replace', 'encode',
    'decode', 'splitlines', 'isoformat', 'timestamp', 'total_seconds'))
_PURE_METHODS = frozenset((
    'fullmatch', 'search', 'match', 'findall', 'finditer', 'sub', 'subn', 'group', 'groups',
    'groupdict', 'resolve', 'relative_to', 'is_relative_to', 'with_suffix', 'utcoffset',
    'astimezone', 'date', 'time', 'strftime', 'weekday', 'communicate', 'wait', 'poll',
    'kill', 'terminate', 'rpartition', 'partition', 'zfill', 'title', 'casefold',
    'removeprefix', 'removesuffix', 'hexdigest', 'digest', 'expandvars', 'expanduser'))
_BUILDERS = frozenset(('append', 'extend', 'add', 'update', 'setdefault', 'insert'))
_TRANSPARENT = frozenset(('list', 'tuple', 'set', 'frozenset', 'dict', 'sorted', 'reversed',
                          'filter', 'map', 'enumerate', 'zip', 'min', 'max', 'next', 'iter',
                          'deepcopy', 'copy', 'chain', 'islice', 'groupby'))
_FILE_READS = frozenset(('is_symlink', 'stat', 'lstat', 'open', 'read_text', 'read_bytes', 'glob', 'rglob', 'iterdir',
                         'listdir', 'scandir', 'exists', 'is_file', 'is_dir', 'walk'))
_BUILTIN_NAMES = frozenset(dir(_builtins))


def _is_test_path(rel):
    parts = Path(rel).parts
    name = parts[-1]
    return (name.startswith('test_') or name.endswith('_test.py') or '_checks_' in name
            or any(p in ('test', 'tests') for p in parts[:-1]))


def source_domain(root):
    """Sorted non-test Python paths beneath root, recursive."""
    root = Path(root)
    return sorted(str(p.relative_to(root)) for p in root.rglob('*.py')
                  if p.is_file() and not _is_test_path(p.relative_to(root)))


def _modname(rel):
    return '.'.join(Path(rel).with_suffix('').parts)


def _names(target):
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _root(e):
    while isinstance(e, (ast.Subscript, ast.Attribute, ast.Call)):
        e = e.func if isinstance(e, ast.Call) else e.value
    return e.id if isinstance(e, ast.Name) else None


def _join_key(key):
    return isinstance(key, ast.Constant) and key.value in JOIN_FIELDS


def _subject_of(e):
    """Name whose 'subject' field e reads, else None."""
    if isinstance(e, ast.Subscript) and isinstance(e.value, ast.Name) \
            and isinstance(e.slice, ast.Constant) and e.slice.value == 'subject':
        return e.value.id
    if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == 'get' \
            and isinstance(e.func.value, ast.Name) and e.args and len(e.args) <= 2 \
            and isinstance(e.args[0], ast.Constant) and e.args[0].value == 'subject':
        return e.func.value.id
    return None


def _eq_pair(node, targets, op):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], op):
        return None
    left, right = node.left, node.comparators[0]
    for a, b in ((left, right), (right, left)):
        v = _subject_of(a)
        if v and isinstance(b, ast.Name) and b.id in targets:
            return v
    return None


def _conjuncts(test):
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for v in test.values:
            yield from _conjuncts(v)
    else:
        yield test


def _proof(test, targets):
    return {v for c in _conjuncts(test) for v in [_eq_pair(c, targets, ast.Eq)] if v}


def _proof_nodes(test, targets):
    return {id(c) for c in _conjuncts(test) if _eq_pair(c, targets, ast.Eq)}


def _guard_continue(st, targets):
    if isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 \
            and isinstance(st.body[0], (ast.Continue, ast.Return)):
        v = _eq_pair(st.test, targets, ast.NotEq)
        return {v} if v else set()
    return set()


def _for_filter(st, targets):
    """Loop variable proved same-subject for the whole body, or None."""
    if not isinstance(st.target, ast.Name) or not st.body:
        return None
    var, first = st.target.id, st.body[0]
    if _guard_continue(first, targets) == {var}:
        return var
    if len(st.body) == 1 and isinstance(first, ast.If) and not first.orelse \
            and var in _proof(first.test, targets):
        return var
    return None


def _comp_filter(gen, targets):
    if isinstance(gen.target, ast.Name):
        for cond in gen.ifs:
            if gen.target.id in _proof(cond, targets):
                return gen.target.id
    return None


class Func:
    def __init__(self, qual, module, rel, node, cls, parent):
        self.qual, self.module, self.rel, self.node = qual, module, rel, node
        self.cls, self.parent = cls, parent
        a = node.args
        self.params = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
        if a.vararg:
            self.params.append(a.vararg.arg)
        if a.kwarg:
            self.params.append(a.kwarg.arg)
        self.locals = set(self.params) | {n.id for n in ast.walk(node)
                                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


class Report:
    def __init__(self):
        self.returns, self.calls, self.refs, self.candidates = False, [], set(), []
        self.env = set()


class Analysis:
    """Parse, resolve and taint-propagate one source tree."""

    def __init__(self, root, entry_symbols):
        self.root, self.problems = Path(root), []
        self.entry_symbols = list(entry_symbols)
        self.domain = source_domain(root)
        self.trees, self.funcs, self.imports, self.classes = {}, {}, {}, {}
        self.pending_tables, self.tables, self.pending_calls, self.top = {}, {}, {}, {}
        self.attr_taint, self.attr_changed = set(), set()
        for rel in self.domain:
            try:
                self.trees[rel] = ast.parse((self.root / rel).read_text(), rel)
            except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
                self.problems.append('parse-error %s: %s' % (rel, type(exc).__name__))
        self.modules = {_modname(r): r for r in self.trees}
        for rel, tree in self.trees.items():
            self._index(rel, tree)
        self._validate_imports()
        for key, (module, names) in self.pending_tables.items():
            hit = [self.resolve_name(module, n)[1] for n in names if self.resolve_name(module, n)[0] == 'func']
            if len(hit) == len(names):
                self.tables[key] = hit
        for key, (module, names) in self.pending_calls.items():
            hit = [self.resolve_name(module, n)[1] for n in names if self.resolve_name(module, n)[0] == 'func']
            if hit:
                self.tables[key] = hit
        self.methods = {}
        for q, f in self.funcs.items():
            if f.cls:
                self.methods.setdefault(q.rsplit('.', 1)[1], []).append(q)
        self.taint_params = {q: set(f.params) & SEED_PARAMS for q, f in self.funcs.items()}
        self.returns = {q: False for q in self.funcs}
        self.reports = {}
        self._solve()
        self._edges()

    def _index(self, rel, tree):
        module, imports = _modname(rel), {}
        self.imports[module] = imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        imports[a.asname] = ('module', a.name)
                    else:
                        imports[a.name.split('.')[0]] = ('module', a.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.problems.append('import-resolution %s: relative import' % rel)
                for a in node.names:
                    imports[a.asname or a.name] = ('from', node.module or '', a.name)

        def walk(body, prefix, cls, parent):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = prefix + '.' + node.name
                    f = self.funcs[q] = Func(q, module, rel, node, cls, parent)
                    walk(node.body, q, None, f)
                elif isinstance(node, ast.ClassDef):
                    q = prefix + '.' + node.name
                    self.classes[q] = module
                    walk(node.body, q, q, parent)
                else:
                    for field in ('body', 'orelse', 'finalbody'):
                        sub = getattr(node, field, None)
                        if isinstance(sub, list):
                            walk(sub, prefix, cls, parent)
                    for h in getattr(node, 'handlers', []):
                        walk(h.body, prefix, cls, parent)
        walk(tree.body, module, None, None)
        top = self.top.setdefault(module, set())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tg in node.targets:
                    top |= _names(tg)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                top |= _names(node.target)
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call):
                names = [v.id for v in node.value.args if isinstance(v, ast.Name)]
                if names:
                    self.pending_calls[module + '.' + node.targets[0].id] = (module, names)
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, (ast.Dict, ast.List, ast.Tuple)):
                vals = node.value.values if isinstance(node.value, ast.Dict) else node.value.elts
                names = [v.id for v in vals if isinstance(v, ast.Name)]
                if names and len(names) == len(vals):
                    self.pending_tables[module + '.' + node.targets[0].id] = (module, names)

    def _validate_imports(self):
        for module, imports in sorted(self.imports.items()):
            for name, imp in sorted(imports.items()):
                if imp[0] != 'from' or imp[1] not in self.modules:
                    continue
                base, sym = imp[1], imp[2]
                if sym == '*' or base + '.' + sym in self.funcs or base + '.' + sym in self.classes \
                        or base + '.' + sym in self.modules or sym in self.top.get(base, ()) \
                        or sym in self.imports.get(base, {}):
                    continue
                self.problems.append('import-resolution %s: %s not defined in %s'
                                     % (self.modules[module], sym, base))

    # -- name resolution -----------------------------------------------------
    def resolve_name(self, module, name, func=None):
        scope = func
        while scope is not None:
            if scope.qual + '.' + name in self.funcs:
                return 'func', scope.qual + '.' + name
            if name in scope.locals:
                return None, None
            scope = scope.parent
        if module + '.' + name in self.funcs:
            return 'func', module + '.' + name
        if module + '.' + name in self.classes:
            return 'class', module + '.' + name
        imp = self.imports.get(module, {}).get(name)
        if imp:
            if imp[0] == 'module':
                return ('module', imp[1]) if imp[1] in self.modules else ('external', imp[1])
            _, base, sym = imp
            if base in self.modules:
                again = self.imports.get(base, {}).get(sym)
                if again and base + '.' + sym not in self.funcs and base + '.' + sym not in self.classes \
                        and not (again[0] == 'from' and again[1] == base and again[2] == sym):
                    seen = getattr(self, '_follow', set())
                    if (base, sym) not in seen:
                        self._follow = seen | {(base, sym)}
                        try:
                            return self.resolve_name(base, sym)
                        finally:
                            self._follow = seen
                q = base + '.' + sym
                if sym in self.top.get(base, ()):
                    return 'external', q
                if q in self.funcs:
                    return 'func', q
                if q in self.classes:
                    return 'class', q
                if q in self.modules:
                    return 'module', q
                if sym.isupper() or not sym.isidentifier():
                    return 'external', q       # a constant, not a callable
                self.problems.append('import-resolution %s: %s not defined in %s'
                                     % (self.modules[module], sym, base))
                return 'external', q
            if base + '.' + sym in self.modules:
                return 'module', base + '.' + sym
            return 'external', base + '.' + sym
        if name in _BUILTIN_NAMES:
            return 'builtin', name
        return None, None

    def resolve_call(self, module, func, node):
        """-> (kind, target); kind in func/class/module/external/builtin/None."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == '__import__' and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            name = node.args[0].value
            return ('module', name) if name in self.modules else ('external', name)
        if isinstance(node, ast.Name):
            if node.id == 'cls' and func is not None:
                owner = func
                while owner is not None and not owner.cls:
                    owner = owner.parent
                if owner is not None:
                    init = owner.cls + '.__init__'
                    return ('func', init) if init in self.funcs else ('class', owner.cls)
            kind, target = self.resolve_name(module, node.id, func)
            if kind == 'class':
                init = target + '.__init__'
                return ('func', init) if init in self.funcs else ('class', target)
            return kind, target
        if isinstance(node, ast.Attribute):
            base = node.value
            if isinstance(base, ast.Call):
                kind, target = self.resolve_call(module, func, base)
                if kind == 'module':
                    q = target + '.' + node.attr
                    return ('func', q) if q in self.funcs else ('external', q)
                return None, None
            if isinstance(base, ast.Name) and base.id in ('self', 'cls') and func is not None:
                owner = func
                while owner is not None and not owner.cls:
                    owner = owner.parent
                q = (owner.cls if owner else '') + '.' + node.attr
                return ('func', q) if q in self.funcs else (None, None)
            if isinstance(base, (ast.Name, ast.Attribute)):
                kind, target = self.resolve_call(module, func, base)
                if kind == 'module':
                    q = target + '.' + node.attr
                    if q in self.funcs:
                        return 'func', q
                    if q in self.classes:
                        init = q + '.__init__'
                        return ('func', init) if init in self.funcs else ('class', q)
                    if q in self.modules:
                        return 'module', q
                    if node.attr in self.imports.get(target, {}):
                        return self.resolve_name(target, node.attr)
                    if node.attr in self.top.get(target, ()) or node.attr.isupper():
                        return 'external', q
                    self.problems.append('import-resolution %s: %s has no %s'
                                         % (self.modules.get(module, module), target, node.attr))
                    return 'external', q
                if kind == 'class':
                    q = target + '.' + node.attr
                    return ('func', q) if q in self.funcs else (None, None)
                if kind in ('external', 'builtin'):
                    return 'external', target + '.' + node.attr
        return None, None

    # -- fixed point ---------------------------------------------------------
    def _solve(self):
        callers, dirty = {}, set(self.funcs)
        nested = {}
        for q, f in self.funcs.items():
            if f.parent is not None:
                nested.setdefault(f.parent.qual, []).append(q)
        while dirty:
            q = min(dirty, key=lambda x: (x.count('.'), x))
            dirty.discard(q)
            old_env = self.reports[q].env if q in self.reports else None
            rep = _FuncPass(self, self.funcs[q]).run()
            self.reports[q] = rep
            for cls in self.attr_changed:
                dirty.update(m for m, mf in self.funcs.items() if mf.cls == cls)
            self.attr_changed.clear()
            if old_env is None or rep.env != old_env:
                dirty.update(nested.get(q, ()))
            if rep.returns and not self.returns[q]:
                self.returns[q] = True
                dirty.update(callers.get(q, ()))
            for callee, bound in rep.calls:
                callers.setdefault(callee, set()).add(q)
                new = set(bound) - self.taint_params[callee]
                if new:
                    self.taint_params[callee] |= new
                    dirty.add(callee)
        self.problems = sorted(set(self.problems))

    def _edges(self):
        self.edges = {q: set(r.refs) for q, r in self.reports.items()}
        for q in self.funcs:
            for c in list(self.edges[q]):
                if c in self.classes:
                    self.edges[q] |= {m for m, f in self.funcs.items() if f.cls == c}
        for q, f in self.funcs.items():
            if f.parent is not None:
                self.edges[f.parent.qual].add(q)

    def reachable(self, symbols):
        seen, todo = set(), [s for s in symbols if s in self.funcs]
        while todo:
            q = todo.pop()
            if q not in seen:
                seen.add(q)
                todo.extend(c for c in self.edges.get(q, ()) if c in self.funcs)
        return seen


class _FuncPass:
    """Flow-insensitive pass over one function at its current taint."""

    def __init__(self, analysis, func):
        self.a, self.f = analysis, func
        self.env = set(analysis.taint_params[func.qual])
        if func.parent is not None and func.parent.qual in analysis.reports:
            self.env |= analysis.reports[func.parent.qual].env
        self.targets = {p for p in func.params if p in TARGET_FORMALS}
        owner = func
        while owner is not None and not owner.cls:
            owner = owner.parent
        self.owner = owner.cls if owner else None
        self.grouped, self.rep, self.call_tainted = set(), Report(), False
        self.lambdas, self.in_lambda_assign = set(), False
        self.rep.env = self.env

    def run(self):
        for _ in range(6):
            before = set(self.env), set(self.targets), set(self.grouped)
            self.rep.candidates, self.rep.calls, self.rep.refs = [], [], set()
            self.rep.returns = False
            self.block(self.f.node.body, frozenset())
            if before == (self.env, self.targets, self.grouped):
                break
        return self.rep

    # -- taint of an expression ---------------------------------------------
    def tx(self, e, env=None):
        env = self.env if env is None else env
        if e is None or isinstance(e, (ast.Constant, ast.Lambda, ast.JoinedStr, ast.Compare)):
            return False
        if isinstance(e, ast.Name):
            return e.id in env
        if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) \
                and e.value.id in ('self', 'cls') and (self.owner, e.attr) in self.a.attr_taint:
            return True
        if isinstance(e, (ast.Attribute, ast.Starred, ast.Subscript)):
            return self.tx(e.value, env)
        if isinstance(e, ast.Call):
            return self.call_taint(e, env)
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = set(env)
            for g in e.generators:
                if self.tx(g.iter, inner):
                    inner |= _names(g.target)
            elts = [e.key, e.value] if isinstance(e, ast.DictComp) else [e.elt]
            return any(self.tx(x, inner) for x in elts) or any(self.tx(g.iter, inner) for g in e.generators)
        return any(self.tx(c, env) for c in ast.iter_child_nodes(e) if isinstance(c, ast.expr))

    def call_taint(self, c, env):
        kind, target = self.a.resolve_call(self.f.module, self.f, c.func)
        args = list(c.args) + [k.value for k in c.keywords]
        any_arg = any(self.tx(x, env) for x in args)
        if target in READERS:
            return True
        if kind == 'func':
            return self.a.returns.get(target, False) or (target.endswith('.__init__') and any_arg)
        if kind in ('external', 'builtin'):
            return target.rsplit('.', 1)[-1] in _TRANSPARENT and any_arg
        if isinstance(c.func, ast.Attribute):
            if self.tx(c.func.value, env) or any_arg:
                return c.func.attr in _RECORD_METHODS
            return False
        return any_arg

    # -- statements ----------------------------------------------------------
    def assign(self, target, tainted):
        if not tainted:
            return
        self.env |= _names(target)
        if isinstance(target, (ast.Subscript, ast.Attribute)):
            root = _root(target)
            if root:
                self.env.add(root)
            if isinstance(target, ast.Attribute) and root in ('self', 'cls'):
                if isinstance(target.value, ast.Name) and self.owner:
                    key = (self.owner, target.attr)
                    if key not in self.a.attr_taint:
                        self.a.attr_taint.add(key)
                        self.a.attr_changed.add(self.owner)
                else:
                    self.cand('unresolved', target, 'event data stored on a nested object attribute')
            elif isinstance(target, ast.Attribute) and root not in self.env_roots():
                pass

    def env_roots(self):
        return self.env

    def block(self, stmts, proved):
        proved = frozenset(proved)
        for st in stmts:
            self.stmt(st, proved)
            proved = proved | _guard_continue(st, self.targets)

    def stmt(self, st, proved):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(st, ast.Assign):
            self.in_lambda_assign = isinstance(st.value, ast.Lambda)
            self.expr(st.value, proved)
            self.in_lambda_assign = False
            t = self.tx(st.value)
            if isinstance(st.value, ast.Lambda):
                self.lambdas |= {n for tg in st.targets for n in _names(tg)}
            for tg in st.targets:
                self.expr(tg, proved, store=True)
                self.assign(tg, t)
                if isinstance(tg, ast.Name) and isinstance(st.value, ast.Name) and st.value.id in self.targets:
                    self.targets.add(tg.id)
                if isinstance(tg, ast.Subscript) and _subject_of(tg.slice) and _root(tg):
                    self.grouped.add(_root(tg))
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
            if st.value is not None:
                self.expr(st.value, proved)
                self.assign(st.target, self.tx(st.value))
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self.expr(st.iter, proved)
            mine = _for_filter(st, self.targets)
            if self.tx(st.iter):
                self.assign(st.target, True)
                if mine is None:
                    self.cand('iter', st.iter)
            self.block(st.body, proved | ({mine} if mine else set()))
            self.block(st.orelse, proved)
        elif isinstance(st, ast.If):
            skip = _proof_nodes(st.test, self.targets)
            if _guard_continue(st, self.targets):
                skip = skip | {id(st.test)}
            self.expr(st.test, proved, skip=skip)
            self.block(st.body, proved | _proof(st.test, self.targets))
            self.block(st.orelse, proved)
        elif isinstance(st, ast.While):
            self.expr(st.test, proved)
            self.block(st.body, proved)
            self.block(st.orelse, proved)
        elif isinstance(st, ast.Return):
            if st.value is not None:
                self.expr(st.value, proved)
                if self.tx(st.value):
                    self.rep.returns = True
        elif isinstance(st, ast.Expr):
            self.expr(st.value, proved)
            v = st.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in _BUILDERS:
                if any(self.tx(x) for x in v.args) and _root(v.func.value):
                    self.env.add(_root(v.func.value))
            for n in ast.walk(v):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'setdefault' \
                        and n.args and _subject_of(n.args[0]) and _root(n.func.value):
                    self.grouped.add(_root(n.func.value))
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for it in st.items:
                self.expr(it.context_expr, proved)
                if it.optional_vars is not None:
                    self.assign(it.optional_vars, self.tx(it.context_expr))
            self.block(st.body, proved)
        elif isinstance(st, ast.Try):
            self.block(st.body, proved)
            for h in st.handlers:
                self.block(h.body, proved)
            self.block(st.orelse, proved)
            self.block(st.finalbody, proved)
        else:
            for c in ast.iter_child_nodes(st):
                if isinstance(c, ast.expr):
                    self.expr(c, proved)
                elif isinstance(c, ast.match_case):
                    self.block(c.body, proved)

    # -- expressions ---------------------------------------------------------
    def cand(self, op, node, detail=''):
        self.rep.candidates.append((op, ast.unparse(node), detail))

    def expr(self, e, proved, store=False, skip=frozenset(), env=None):
        env = self.env if env is None else env
        if e is None or id(e) in skip:
            return
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner, prov = set(env), set(proved)
            for g in e.generators:
                self.expr(g.iter, prov, env=inner)
                mine = _comp_filter(g, self.targets)
                if self.tx(g.iter, inner):
                    inner |= _names(g.target)
                    if mine is None:
                        self.cand('iter', g.iter)
                if mine:
                    prov.add(mine)
                for cond in g.ifs:
                    self.expr(cond, prov, env=inner, skip=_proof_nodes(cond, self.targets))
            for x in ([e.key, e.value] if isinstance(e, ast.DictComp) else [e.elt]):
                self.expr(x, prov, env=inner)
            return
        if isinstance(e, ast.Lambda):
            inner = set(env)
            if self.call_tainted or self.in_lambda_assign:
                inner |= {a.arg for a in e.args.args}
            self.expr(e.body, proved, env=inner)
            return
        if isinstance(e, ast.Call):
            self.call(e, proved, env)
            return
        if isinstance(e, ast.Subscript) and not store and self.tx(e.value, env):
            base, key = e.value, e.slice
            same = isinstance(base, ast.Name) and base.id in proved and not _join_key(key)
            grouped = isinstance(base, ast.Name) and base.id in self.grouped \
                and isinstance(key, ast.Name) and key.id in self.targets
            if not (same or grouped):
                self.cand('subscript', e)
        elif isinstance(e, ast.Compare) and any(
                self.tx(x, env - proved) for x in [e.left] + e.comparators):
            self.cand('compare', e)
        for c in ast.iter_child_nodes(e):
            if isinstance(c, ast.expr):
                self.expr(c, proved, env=env, skip=skip)

    def bind(self, targets, c, env):
        for target in targets:
            cf = self.a.funcs[target]
            off = 1 if cf.params and cf.params[0] in ('self', 'cls') and cf.cls else 0
            bound = [cf.params[i + off] for i, x in enumerate(c.args)
                     if i + off < len(cf.params) and self.tx(x, env)]
            bound += [k.arg for k in c.keywords if k.arg and self.tx(k.value, env)]
            self.rep.calls.append((target, bound))

    def fan_out(self, c):
        """Project functions a call may reach when its target is not a plain name."""
        f = c.func
        if isinstance(f, ast.Subscript):
            return self.table_of(f) or []
        if isinstance(f, ast.Name):
            key = self.f.module + '.' + f.id
            imp = self.a.imports.get(self.f.module, {}).get(f.id)
            if key in self.a.tables:
                return self.a.tables[key]
            if imp and imp[0] == 'from' and imp[1] + '.' + imp[2] in self.a.tables:
                return self.a.tables[imp[1] + '.' + imp[2]]
        if isinstance(f, ast.Attribute) and f.attr not in _RECORD_METHODS \
                and f.attr not in _FILE_READS and f.attr not in _PURE_METHODS:
            hits = self.a.methods.get(f.attr, [])
            return hits if len(hits) == 1 else []
        return []

    def call(self, c, proved, env):
        kind, target = self.a.resolve_call(self.f.module, self.f, c.func)
        args = list(c.args) + [k.value for k in c.keywords]
        targs = [self.tx(x, env - proved) for x in args]
        recv = isinstance(c.func, ast.Attribute) and self.tx(c.func.value, env - proved)
        recv_all = isinstance(c.func, ast.Attribute) and self.tx(c.func.value, env)
        for x in [c.func] + args:
            self.refs_of(x)
        prev, self.call_tainted = self.call_tainted, any(targs) or recv
        fan = self.fan_out(c) if kind in (None,) else []
        if fan:
            self.rep.refs.update(fan)
        if kind == 'func':
            self.bind([target], c, env)
            if any(targs):
                self.cand('call-arg', c)
            if target in READERS:
                self.cand('ledger-read', c)
        elif target in READERS:
            self.cand('ledger-read', c)
        elif fan:
            self.bind(fan, c, env)
            if any(targs) or recv:
                self.cand('call-arg', c)
        elif isinstance(c.func, ast.Attribute):
            attr = c.func.attr
            if recv_all and attr in ('get', 'items', 'values', 'keys'):
                skip = attr == 'get' and isinstance(c.func.value, ast.Name) and (
                    (c.func.value.id in proved and not (c.args and _join_key(c.args[0])))
                    or (c.func.value.id in self.grouped and c.args and isinstance(c.args[0], ast.Name)
                        and c.args[0].id in self.targets))
                if not skip and not (attr == 'get' and _subject_of(c) in proved):
                    self.cand(attr, c)
            elif kind in ('external', 'builtin') and any(targs):
                self.cand('call-arg', c)
            elif (recv or any(targs)) and attr not in _RECORD_METHODS and attr not in _FILE_READS \
                    and attr not in _PURE_METHODS:
                self.cand('unresolved', c, 'method on an unresolved receiver')
            elif any(targs) and attr not in _BUILDERS:
                self.cand('call-arg', c)
            if attr in _FILE_READS:
                self.cand('file-read', c)
        elif isinstance(c.func, ast.Name):
            if kind == 'class' and any(targs):
                self.cand('call-arg', c)
            elif kind is None and c.func.id in self.lambdas:
                if any(targs):
                    self.cand('call-arg', c)
            elif kind is None and any(targs):
                self.cand('unresolved', c, 'call target not resolved')
            elif kind in ('builtin', 'external') and any(targs):
                self.cand('call-arg', c)
            if c.func.id in _FILE_READS:
                self.cand('file-read', c)
        elif isinstance(c.func, ast.Subscript) and not fan and any(targs):
            self.cand('unresolved', c, 'dynamic call target')
        elif any(targs):
            self.cand('unresolved', c, 'dynamic call target')
        if isinstance(c.func, ast.Attribute):
            self.expr(c.func.value, proved, env=env)
        for x in args:
            self.expr(x, proved, env=env)
        self.call_tainted = prev

    def table_of(self, node):
        if isinstance(node.value, ast.Name):
            kind, target = self.a.resolve_name(self.f.module, node.value.id, self.f)
            key = self.f.module + '.' + node.value.id
            if key in self.a.tables:
                return self.a.tables[key]
            imp = self.a.imports.get(self.f.module, {}).get(node.value.id)
            if imp and imp[0] == 'from' and imp[1] + '.' + imp[2] in self.a.tables:
                return self.a.tables[imp[1] + '.' + imp[2]]
        return None

    def refs_of(self, e):
        for n in ast.walk(e):
            if isinstance(n, ast.Subscript) and self.table_of(n):
                self.rep.refs.update(self.table_of(n))
            if isinstance(n, ast.Name):
                key = self.f.module + '.' + n.id
                imp = self.a.imports.get(self.f.module, {}).get(n.id)
                if key in self.a.tables:
                    self.rep.refs.update(self.a.tables[key])
                elif imp and imp[0] == 'from' and imp[1] + '.' + imp[2] in self.a.tables:
                    self.rep.refs.update(self.a.tables[imp[1] + '.' + imp[2]])
            if isinstance(n, (ast.Name, ast.Attribute)):
                k, t = self.a.resolve_call(self.f.module, self.f, n)
                if k in ('func', 'class'):
                    self.rep.refs.add(t)


# ---------------------------------------------------------------------------
# Inventory construction and comparison
# ---------------------------------------------------------------------------
INPUT_ROOTS = ('tick._steps', 'fold.fold', 'fold.finish_events', 'fold.held_specs', 'lane_rules.load')
GAP_KEYS = {'ledger_files': 'ledger-store', 'grading_env': 'config-look-toml'}
INVENTORY = FIXTURES / 'source_reads.json'
EXCLUSIONS = {}      # (symbol, op, expression) -> proof of non-effect; none are needed today


def class_roots():
    return {a: (deps.TABLE[a].answer,) + tuple(deps.SOURCE_SYMBOLS[a]) for a in deps.ACTION_CLASSES}


def _kinds():
    out = set()
    for row in deps.TABLE.values():
        out |= set(row.own_kinds + row.mention_kinds + row.global_kinds + row.ref_kinds + row.spawn_kinds)
    return out


def _global_kinds():
    out = set()
    for row in deps.TABLE.values():
        out |= set(row.global_kinds)
    return out | set(deps._QUEUE)


def _literals(expr):
    """(record fields read, string constants) of one normalized expression."""
    fields, text = set(), set()
    for n in ast.walk(ast.parse(expr, mode='eval')):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
            fields.add(n.slice.value)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'get' \
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            fields.add(n.args[0].value)
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            text.add(n.value)
    return sorted(fields), sorted(text)


def _route(op, expr, symbol, module, near, declared):
    fields, text = _literals(expr)
    kinds = sorted(set(text) & _kinds())
    if op == 'file-read':
        hits = [k for k in sorted(declared) if symbol in near[k]]
        if len(hits) > 1:
            hits = [k for k in hits if ('verify' in expr) == (k == 'content:grader-script')] or hits
        key = hits[0] if hits else 'gap:' + GAP_KEYS.get(module, 'runtime-state')
        return 'non-ledger:' + key, fields, kinds
    if op == 'ledger-read':
        route = 'full-read'
    elif op == 'unresolved':
        route = 'unresolved'
    elif 'ref' in fields:
        route = 'ref'
    elif 'spawn' in fields:
        route = 'spawn'
    elif set(kinds) & _global_kinds():
        route = 'global'
    elif 'waits_for' in fields or 'dependency-hold' in kinds:
        route = 'mention'
    elif {'charter', 'footprint', 'wave'} & set(fields):
        route = 'sibling'
    elif kinds:
        route = 'own'
    else:
        route = 'ledger-wide'
    return route, fields, kinds


def build_inventory(analysis, roots, inputs, surfaces, exclusions=None):
    """The complete inventory implied by one analysis, and what is missing."""
    exclusions = exclusions or {}
    reach = {a: analysis.reachable(r) for a, r in roots.items()}
    graph = set().union(*reach.values()) | analysis.reachable(inputs)
    declared = deps.NON_LEDGER_INPUTS
    near = {}
    for key, (_, symbols) in declared.items():
        near[key] = set(symbols) | {c for s in symbols for c in analysis.edges.get(s, ())}
    out = {'graph': [], 'outside': []}
    missing = []
    for q in sorted(analysis.funcs):
        rep = analysis.reports[q]
        if not rep.candidates:
            continue
        f = analysis.funcs[q]
        counts = collections.Counter((op, ex) for op, ex, _ in rep.candidates)
        if q in graph:
            reads = []
            for (op, ex), n in sorted(counts.items()):
                route, fields, kinds = _route(op, ex, q, f.module, near, declared)
                row = dict(op=op, expr=ex, count=n, route=route, fields=fields, kinds=kinds)
                if (q, op, ex) in exclusions:
                    row['excluded'] = exclusions[(q, op, ex)]
                reads.append(row)
            out['graph'].append(dict(
                path=f.rel, symbol=q, classes=sorted(a for a in reach if q in reach[a]) or ['input-construction'],
                reads=reads))
        else:
            surface = surfaces.get(f.module)
            if not surface:
                missing.append((f.rel, q, f.module))
            out['outside'].append(dict(
                path=f.rel, symbol=q, surface=surface or '', reads=[
                    [op, ex, n] for (op, ex), n in sorted(counts.items())]))
    return out, sorted(graph), missing


def _index(rows, key):
    return {(r['path'], r['symbol']): r for r in rows}


def check_inventory(analysis, roots, inputs, surfaces, committed, exclusions=None):
    """Diagnostics (path/symbol first); an empty list means the guard is satisfied."""
    problems = ['analysis: ' + p for p in analysis.problems]
    entry = sorted({s for r in roots.values() for s in r} | set(inputs))
    problems += ['missing entry symbol: ' + s for s in entry if s not in analysis.funcs]
    current, graph, missing = build_inventory(analysis, roots, inputs, surfaces, exclusions)
    problems += ['no non-action reason for module %s (%s %s)' % (m, p, q) for p, q, m in missing]
    for row in current['graph']:
        for r in row['reads']:
            if r['op'] == 'unresolved':
                problems.append('unresolved flow in reachable graph: %s %s: %s' % (row['path'], row['symbol'], r['expr']))
            if r['route'] == 'unresolved' or r.get('excluded') == '':
                problems.append('read without a route or proof: %s %s: %s' % (row['path'], row['symbol'], r['expr']))
    for part in ('graph', 'outside'):
        have, want = _index(committed.get(part, []), part), _index(current[part], part)
        for k in sorted(set(have) | set(want)):
            if k not in have:
                problems.append('new %s read with no disposition: %s %s' % (
                    part, k[0], k[1]) + ''.join('\n    + %s %s' % (r['op'] if part == 'graph' else r[0],
                                                   r['expr'] if part == 'graph' else r[1])
                                               for r in want[k]['reads']))
            elif k not in want:
                problems.append('stale %s disposition (symbol gone or no longer matches): %s %s' % (part, k[0], k[1]))
            elif have[k] != want[k]:
                a_ = {(r['op'], r['expr']): r for r in have[k]['reads']} if part == 'graph' else {(r[0], r[1]): r for r in have[k]['reads']}
                b_ = {(r['op'], r['expr']): r for r in want[k]['reads']} if part == 'graph' else {(r[0], r[1]): r for r in want[k]['reads']}
                detail = ['\n    + %s %s' % x for x in sorted(set(b_) - set(a_))] + \
                         ['\n    - %s %s' % x for x in sorted(set(a_) - set(b_))] + \
                         ['\n    ~ %s %s' % x for x in sorted(set(a_) & set(b_)) if a_[x] != b_[x]]
                if have[k] != want[k] and not detail:
                    detail = ['\n    ~ metadata (classes/surface) differs']
                problems.append('changed %s reads: %s %s%s' % (part, k[0], k[1], ''.join(detail)))
    return problems, current, graph


# Caller surface for code outside the reachable graph. First matching group wins.
# A reason names who calls the module and why its reads cannot move a Step or
# any other answer root; the guard re-derives reachability, so a function that
# an answer root starts to call loses this exemption.
SURFACE_GROUPS = (
    (r'(fold|nextstep|tick|dispatch|autodispatch|merge_queue|proving|rulings|dependency_holds|'
     r'lane_rules|attempts|bundle_wait|routing|spawns|start_check|window_policy|window_prewindow|'
     r'grader_packet|grading_env|checks|owed|owed_failures|owed_routes|owed_verdicts|panes|'
     r'project_scope|ledger_files|fold_emits|fold_stalls|evidence_revision|health_alarms|build_runner)',
     'answer-module function that no answer root reaches: CLI main, report, ledger writer or executor '
     'entry living beside a reachable helper'),
    (r'(brief|daily_summary|digest_actions|flow_health|look|look_.*|realm|realm_.*|scorecard|stuck_digest|'
     r'throughput|problems|owed_digest|owed_metrics|usage|seat_usage|howto|plain|plain_backfill|notes|'
     r'notification_policy|merge_news|merge_lane_board|ubicloud_bill|gh_bill|transcript_scan|harvest|'
     r'rounds|spec_history|pace|models|shape|stall_sample|validate|verifier|window_attribution|retired|'
     r'amends|audit|door_deps|think|thinker_.*|planner_bar|planner_run)',
     'report and digest surface: read-only display or metrics invoked from the doit CLI or a report cron, '
     'never from tick._steps or an answer root'),
    (r'(tg_.*|up|relay|intake|launch|machine|install_sync|cli_path|ctx_guard|cron_run|crons|'
     r'balance|pane_.*|wip_resume|worktree_heal|resources|cpu_share|loadgate|ghlimit|paid_call|dispatch_isolation)',
     'operator and session plumbing (Telegram, relay, panes, launch, crons, load gates): runs beside the '
     'tick and decides nothing about a spec step'),
    (r'(build_.*|builder_deps|ci_.*|grader_.*|empty_grade|runner_evidence|suite_run|prod_.*|review_.*|'
     r'precheck|precheck_targeted|dispatch_builder|coupled_.*|effective_writes|packet|packet_.*|'
     r'spec_preflight|preflight_sweep|base_red.*|base_refresh|branch_refresh|adopt_candidate|'
     r'node_modules|provision|scratch|push_origin|deploy|direct_merge|master_full|migration_order|'
     r'guard|guards_live|freeze|cancel|carry|handin_check|close_reviews|conflict|sweep_.*|check_backfill|'
     r'owed_.*|window_.*|merge_.*|rechain.*|quarantine.*|lane_driver|pass_memo|prequeue|reap_.*|'
     r'disk_reaper|tree_cleanup|backup|restore|ledger_.*|stall.*|overlap_view|fixtures\..*)',
     'executor, gate and maintenance surface: runs after a step is chosen (build, grade, review, merge, '
     'sweep, repair) or on its own cron, and is not called by an answer root'),
)


def surface_for(module):
    for pattern, reason in SURFACE_GROUPS:
        if re.fullmatch(pattern, module):
            return reason
    return None



MINI_BASE = {
    'fold.py': ("def read_events():\n    return []\n\n\n"
                "def fold(events):\n    return list(events)\n"),
    'nextstep.py': ("import fold\nimport helper\n\n\n"
                    "def step_for(events, specs, item, now, rules):\n"
                    "    own = [e for e in events if e.get('subject') == item]\n"
                    "    return helper.latest(own, item)\n"),
    'helper.py': ("def latest(evs, item):\n    best = None\n    for e in evs:\n"
                  "        if e['subject'] != item:\n            continue\n"
                  "        best = e['type']\n    return best\n"),
    'unrelated.py': "def util(x):\n    return x + 1\n",
}
MINI_ROOTS = {'next-step': ('nextstep.step_for',)}
MINI_INPUTS = ('fold.fold',)
MINI_SURFACES = {'unrelated': 'control: arithmetic helper, no event data'}


class SourceGuardTests(unittest.TestCase):
    """AC3: conservative AST source guard over every non-test module beneath src/."""
    analysis = None

    @classmethod
    def setUpClass(cls):
        roots = class_roots()
        entries = sorted({s for r in roots.values() for s in r} | set(INPUT_ROOTS))
        cls.analysis = Analysis(SOURCE, entries)
        cls.surfaces = {f.module: surface_for(f.module) for f in cls.analysis.funcs.values()
                        if surface_for(f.module)}
        cls.committed = json.loads(INVENTORY.read_text())
        cls.problems, cls.current, cls.graph = check_inventory(
            cls.analysis, roots, INPUT_ROOTS, cls.surfaces, cls.committed, EXCLUSIONS)

    # -- helpers for the temporary trees --------------------------------------
    def tree(self, files):
        root = Path(tempfile.mkdtemp(dir=SANDBOX))
        self.addCleanup(shutil.rmtree, root, True)
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return root

    def mini(self, edits=None, drop=()):
        files = {k: v for k, v in MINI_BASE.items() if k not in drop}
        files.update(edits or {})
        return files

    def run_guard(self, files, committed, surfaces=None, exclusions=None):
        entries = sorted({s for r in MINI_ROOTS.values() for s in r} | set(MINI_INPUTS))
        analysis = Analysis(self.tree(files), entries)
        return check_inventory(analysis, MINI_ROOTS, MINI_INPUTS,
                               MINI_SURFACES if surfaces is None else surfaces, committed, exclusions)[0]

    def baseline(self):
        entries = sorted({s for r in MINI_ROOTS.values() for s in r} | set(MINI_INPUTS))
        analysis = Analysis(self.tree(MINI_BASE), entries)
        inventory = build_inventory(analysis, MINI_ROOTS, MINI_INPUTS, MINI_SURFACES)[0]
        self.assertEqual(check_inventory(analysis, MINI_ROOTS, MINI_INPUTS, MINI_SURFACES, inventory)[0], [])
        return inventory

    def regenerated(self, files, surfaces=None, exclusions=None):
        entries = sorted({s for r in MINI_ROOTS.values() for s in r} | set(MINI_INPUTS))
        analysis = Analysis(self.tree(files), entries)
        return build_inventory(analysis, MINI_ROOTS, MINI_INPUTS,
                               MINI_SURFACES if surfaces is None else surfaces, exclusions)[0]

    # -- the real source ------------------------------------------------------
    def test_real_source_matches_the_committed_inventory(self):
        a = self.analysis
        print('SOURCE DOMAIN (%d non-test python modules, recursive)' % len(a.domain))
        for rel in a.domain:
            print('  ', rel)
        print('REACHABLE SYMBOLS (%d)' % len(self.graph))
        for q in self.graph:
            print('  ', q)
        print('ACTION-READ INVENTORY (path | symbol | op | route | count | expression)')
        for row in self.current['graph']:
            for r in row['reads']:
                print('  ', row['path'], '|', row['symbol'], '|', r['op'], '|', r['route'], '|', r['count'],
                      '|', r['expr'])
        print('OUTSIDE-GRAPH DISPOSITIONS (each symbol: matched reads) grouped by caller surface')
        for surface in sorted({row['surface'] for row in self.current['outside']}):
            rows = [row for row in self.current['outside'] if row['surface'] == surface]
            print('  SURFACE:', surface)
            for row in rows:
                print('    ', row['symbol'], sum(n for _, _, n in row['reads']))
        self.assertEqual(self.problems, [], '\n'.join(self.problems))

    def test_domain_is_recursive_and_excludes_tests(self):
        domain = set(self.analysis.domain)
        self.assertIn('fold.py', domain)
        self.assertIn('fixtures/owner_drop_rework_before.py', domain)
        self.assertIn('fixtures/build_runner/driver.py', domain)
        self.assertFalse([p for p in domain if Path(p).name.startswith('test_')])
        self.assertFalse([p for p in domain if '_checks_' in Path(p).name])
        self.assertEqual(sorted(domain), self.analysis.domain)
        tree = self.tree({'a.py': '', 'test_a.py': '', 'a_test.py': '', 'x_checks_y.py': '',
                          'pkg/b.py': '', 'pkg/tests/c.py': '', 'test/d.py': '', 'pkg/sub/e.py': ''})
        self.assertEqual(source_domain(tree), ['a.py', 'pkg/b.py', 'pkg/sub/e.py'])

    def test_every_answer_function_and_input_root_is_in_the_graph(self):
        for action, row in deps.TABLE.items():
            self.assertIn(row.answer, self.graph, action)
            for symbol in deps.SOURCE_SYMBOLS[action]:
                self.assertIn(symbol, self.graph, (action, symbol))
        for symbol in INPUT_ROOTS:
            self.assertIn(symbol, self.graph)
        modules = {s.split('.')[0] for s in self.graph}
        for module in ('autodispatch', 'dependency_holds', 'dispatch', 'tick', 'merge_queue',
                       'proving', 'rulings', 'fold', 'nextstep'):
            self.assertIn(module, modules)

    def test_real_reads_are_represented(self):
        graph = {(r['symbol'], x['op'], x['expr']): x for r in self.current['graph'] for x in r['reads']}
        routes = {x['route'] for x in graph.values()}
        self.assertTrue({'ref', 'spawn', 'global', 'sibling', 'mention', 'own', 'ledger-wide'} <= routes, routes)
        symbols = {k[0] for k in graph}
        for symbol in ('fold.answered', 'fold.capability_holds', 'fold.terminal_spawns', 'fold.closable',
                       'nextstep._wave', 'merge_queue.queue', 'proving.unproven_checks'):
            self.assertIn(symbol, symbols)
        keys = {x['route'].split(':', 1)[1] for x in graph.values() if x['route'].startswith('non-ledger:')}
        self.assertTrue(set(deps.NON_LEDGER_INPUTS) <= keys, set(deps.NON_LEDGER_INPUTS) - keys)
        self.assertIn(('dependency_holds._text_declaration', 'file-read', 'dispatch.spec_path(sid).read_text()'),
                      graph)
        self.assertIn(('dispatch._plan_units', 'file-read', 'plan_path.read_text()'), graph)
        self.assertEqual(graph[('dispatch._plan_units', 'file-read', 'plan_path.read_text()')]['route'],
                         'non-ledger:content:plan')
        self.assertEqual(graph[('lane_rules.load', 'file-read', "path.open('rb')")]['route'],
                         'non-ledger:repo:lane-rules')
        gaps = sorted({(k[0], x['route']) for k, x in graph.items() if ':gap:' in x['route']})
        print('NON-LEDGER READS REACHABLE FROM AN ANSWER ROOT THAT door_deps.NON_LEDGER_INPUTS DOES NOT LIST')
        for symbol, route in gaps:
            print('  ', symbol, route)
        self.assertTrue(gaps, 'a recorded table gap vanished; update the inventory')

    def test_every_graph_read_has_a_route_or_a_proof(self):
        for row in self.current['graph']:
            for r in row['reads']:
                self.assertNotEqual(r['op'], 'unresolved', (row['symbol'], r['expr']))
                self.assertTrue(r['route'] and r['route'] != 'unresolved')
        for row in self.current['outside']:
            self.assertTrue(row['surface'], row['symbol'])

    # -- the detection contract -----------------------------------------------
    def test_same_subject_reads_and_lookalikes_add_no_cross_subject_read(self):
        src = '''
def comp(events, item):
    """for e in events: e['ref']  # docstring lookalike"""
    text = "for e in events: e['ref']"  # for e in events: e['spawn']
    mine = [e['type'] for e in events if e.get('subject') == item]
    other = [e.get('ts') for e in events if item == e['subject']]
    return mine, other, text

def loop(events, sid):
    for e in events:
        if e.get('subject') == sid:
            print(e['type'], e.get('ts'))

def guard(events, item_id):
    for e in events:
        if e['subject'] != item_id:
            continue
        print(e['type'], e.get('payload'))

def alias(events, item):
    target = item
    return [e['type'] for e in events if e['subject'] == target and e['type'] == 'x']

def grouped(events, item):
    by = {}
    for e in events:
        by.setdefault(e['subject'], []).append(e)
    return by.get(item), by[item]
'''
        a = Analysis(self.tree({'m.py': src}), ())
        for name in ('comp', 'loop', 'guard', 'alias', 'grouped'):
            cands = a.reports['m.' + name].candidates
            if name == 'grouped':
                # the build loop reads every event; the lookups by the proved target are not reads
                self.assertFalse({'by[item]', 'by.get(item)'} & {c[1] for c in cands}, cands)
            else:
                self.assertEqual(cands, [], (name, cands))
        self.assertEqual(a.reports['m.comp'].candidates, [])
        print('same-subject control: no candidate read in', 'comp loop guard alias grouped')

    def test_unproved_filters_are_candidates(self):
        cases = {
            'inequality': "def f(events, item):\n    return [e['type'] for e in events if e.get('subject') != item]\n",
            'prefix': "def f(events, item):\n    return [e['type'] for e in events if e['subject'].startswith(item)]\n",
            'type-filter': "def f(events, item):\n    return [e for e in events if e['type'] == 'shipped']\n",
            'charter-equality': "def f(events, item):\n    return [e for e in events if e.get('charter') == item]\n",
            'ref-equality': "def f(events, item):\n    return [e for e in events if e.get('ref') == item]\n",
            'spawn-equality': "def f(events, item):\n    return [e for e in events if e.get('spawn') == item]\n",
            'unproved-key': "def f(events, item):\n    by = {e['ref']: e for e in events}\n    return by[item]\n",
            'unfiltered-lookup': "def f(events, item):\n    for e in events:\n        print(e['subject'])\n",
            'else-branch': ("def f(events, item):\n    for e in events:\n        if e.get('subject') == item:\n"
                            "            pass\n        else:\n            print(e['type'])\n"),
        }
        for name, src in cases.items():
            with self.subTest(case=name):
                self.assertTrue(Analysis(self.tree({'m.py': src}), ()).reports['m.f'].candidates, name)

    def test_join_through_own_subject_filter_still_fails(self):
        src = ("def f(events, item):\n    by_spawn = {e['spawn']: e for e in events}\n"
               "    out = []\n    for e in events:\n        if e.get('subject') == item:\n"
               "            out.append(by_spawn[e['spawn']])\n            out.append(e.get('ref'))\n"
               "    return out\n")
        cands = Analysis(self.tree({'m.py': src}), ()).reports['m.f'].candidates
        text = [c[1] for c in cands]
        self.assertIn("by_spawn[e['spawn']]", text)
        self.assertIn("e['spawn']", text)
        self.assertIn("e.get('ref')", text)

    def test_negative_controls(self):
        base = self.baseline()
        controls = {
            'new unrelated module': ({'extra.py': "def leak(events):\n    return [e['ref'] for e in events]\n"},
                                     'extra', 'extra.leak'),
            'nested module': ({'pkg/deep.py': "def leak(events):\n    return [e['ref'] for e in events]\n"},
                              'pkg.deep', 'pkg/deep.py'),
        }
        for name, (edit, module, needle) in controls.items():
            with self.subTest(control=name):
                files = self.mini(edit)
                failure = self.run_guard(files, base)
                self.assertTrue(any('no non-action reason for module ' + module in p for p in failure), failure)
                supplied = dict(MINI_SURFACES, **{module: 'control: caller surface named'})
                failure = self.run_guard(files, base, supplied)
                self.assertTrue(any('new outside read with no disposition' in p and needle in p for p in failure), failure)
                self.assertEqual(self.run_guard(files, self.regenerated(files, supplied), supplied), [])
                print('control', name, 'failed:', failure[0].splitlines()[0])

        helper = MINI_BASE['helper.py']
        graph_controls = {
            'existing reachable helper': (
                {'helper.py': helper.replace("    return best", "    seen = {x['type'] for x in evs}\n    return best")},
                'helper.latest'),
            'renamed and aliased parameter': (
                {'helper.py': helper + "\n\ndef pick(rows):\n    return rows[0]['type']\n",
                 'nextstep.py': MINI_BASE['nextstep.py'].replace(
                     "    return helper", "    alias = events\n    helper.pick(alias)\n    return helper")},
                'helper.pick'),
            'derived map': (
                {'helper.py': helper + ("\n\ndef index(evs):\n    by = {}\n    for e in evs:\n"
                                        "        by.setdefault(e['ref'], []).append(e)\n    return by\n"),
                 'nextstep.py': MINI_BASE['nextstep.py'].replace(
                     "    return helper", "    by = helper.index(events)\n    by[item]\n    return helper")},
                'by[item]'),
            'transitive helper call': (
                {'helper.py': helper + ("\n\ndef a(evs):\n    return b(evs)\n\n\ndef b(rows):\n"
                                        "    return [r['spawn'] for r in rows]\n"),
                 'nextstep.py': MINI_BASE['nextstep.py'].replace(
                     "    return helper", "    helper.a(events)\n    return helper")},
                "helper.b"),
            'changed expression, same names': (
                {'fold.py': MINI_BASE['fold.py'].replace('list(events)', 'sorted(events)')}, 'fold.fold'),
        }
        for name, (edit, needle) in graph_controls.items():
            with self.subTest(control=name):
                files = self.mini(edit)
                failure = self.run_guard(files, base)
                self.assertTrue(any(needle in p for p in failure), (needle, failure))
                self.assertEqual(self.run_guard(files, self.regenerated(files)), [])
                print('control', name, 'failed:', failure[0].splitlines()[0])

    def test_comment_and_string_lookalikes_leave_the_inventory_unchanged(self):
        base = self.baseline()
        files = self.mini({'helper.py': '"""for e in events: e["ref"]"""\n' + MINI_BASE['helper.py']
                           + "\n# for e in evs: e['spawn'] == 1\nNOTE = \"events[0]['ref']\"\n"})
        self.assertEqual(self.run_guard(files, base), [])

    def test_unresolved_reachable_flow_never_passes(self):
        step = MINI_BASE['nextstep.py'].replace("    return helper", "    rules.process(events)\n    return helper")
        files = self.mini({'nextstep.py': step})
        for committed in (self.baseline(), self.regenerated(files)):
            failure = self.run_guard(files, committed)
            self.assertTrue(any('unresolved flow in reachable graph: nextstep.py nextstep.step_for' in p
                                for p in failure), failure)
        outside = self.mini({'extra.py': "def f(events, thing):\n    thing.process(events)\n"})
        supplied = dict(MINI_SURFACES, extra='control: named surface')
        self.assertEqual(self.run_guard(outside, self.regenerated(outside, supplied), supplied), [])

    def test_helper_called_by_an_answer_loses_its_exemption(self):
        extra = {'util2.py': "def scan(events):\n    return [e['type'] for e in events]\n"}
        surfaces = dict(MINI_SURFACES, util2='control: report helper')
        files = self.mini(extra)
        committed = self.regenerated(files, surfaces)
        self.assertEqual(self.run_guard(files, committed, surfaces), [])
        called = self.mini(dict(extra, **{'nextstep.py': MINI_BASE['nextstep.py'].replace(
            "    return helper", "    import util2\n    util2.scan(events)\n    return helper")}))
        failure = self.run_guard(called, committed, surfaces)
        self.assertTrue(any('stale outside' in p and 'util2.scan' in p for p in failure), failure)
        self.assertTrue(any('new graph read' in p and 'util2.scan' in p for p in failure), failure)

    def test_syntax_and_import_errors_fail_with_path_and_symbol(self):
        base = self.baseline()
        failure = self.run_guard(self.mini({'broken.py': 'def x(:\n'}), base)
        self.assertTrue(any(p.startswith('analysis: parse-error broken.py') for p in failure), failure)
        failure = self.run_guard(self.mini({'nextstep.py': 'from helper import nothing\n'
                                            + MINI_BASE['nextstep.py']}), base)
        self.assertTrue(any('import-resolution nextstep.py: nothing not defined in helper' in p for p in failure),
                        failure)
        failure = self.run_guard(self.mini({'nextstep.py': MINI_BASE['nextstep.py'].replace(
            'step_for', 'step_two')}), base)
        self.assertTrue(any('missing entry symbol: nextstep.step_for' in p for p in failure), failure)

    def test_exclusion_is_per_expression_and_needs_a_reason(self):
        edit = {'helper.py': MINI_BASE['helper.py'] + "\n\ndef note(evs):\n    return len(evs)\n",
                'nextstep.py': MINI_BASE['nextstep.py'].replace("    return helper", "    helper.note(events)\n    return helper")}
        files = self.mini(edit)
        key = ('helper.note', 'call-arg', 'len(evs)')
        excluded = {key: 'the count is logged and never reaches the returned step'}
        committed = self.regenerated(files, exclusions=excluded)
        self.assertEqual(self.run_guard(files, committed, exclusions=excluded), [])
        self.assertTrue(self.run_guard(files, committed))                         # no waiver without it
        blank = {key: ''}
        self.assertTrue(any('without a route or proof' in p for p in self.run_guard(
            files, self.regenerated(files, exclusions=blank), exclusions=blank)))
        other = self.mini({'helper.py': edit['helper.py'].replace('len(evs)', 'len(list(evs))'),
                           'nextstep.py': edit['nextstep.py']})
        self.assertTrue(self.run_guard(other, committed, exclusions=excluded))    # changed expression


class IsolationTests(unittest.TestCase):
    def test_roots(self):
        self.assertTrue(fold.ROOT.is_relative_to(SANDBOX))
        self.assertTrue(dispatch.ROOT.is_relative_to(SANDBOX))
        self.assertTrue(dispatch.CONTENT.is_relative_to(SANDBOX))
        self.assertFalse(any(dispatch.CONTENT.iterdir()))
        self.assertNotIn('DOIT_LANE_RULES', os.environ)

    def test_content_materialization_refuses_escapes(self):
        base = Path(tempfile.mkdtemp(dir=SANDBOX))
        outside = Path(tempfile.mkdtemp(dir=SANDBOX))
        self.addCleanup(shutil.rmtree, base, True)
        self.addCleanup(shutil.rmtree, outside, True)
        (base / 'link').symlink_to(outside, target_is_directory=True)
        (base / 'file.md').symlink_to(outside / 'target.md')
        for bad in ('/abs.md', '../x.md', 'a/../../x.md', 'link/x.md', 'file.md', '', '.', 'a//b.md',
                    '~/x.md', 'a\\b.md', './x.md'):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    materialize_content(base, {'ok.md': 'fine', bad: 'x'})
                print('content path', repr(bad), 'refused')
        self.assertEqual(sorted(p.name for p in base.iterdir()), ['file.md', 'link'])   # nothing written
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(ValueError):
            materialize_content(REPO, {'x.md': 'x'})                                   # not a sandbox directory
        with self.assertRaises(ValueError):
            materialize_content(base, {'n.md': b'bytes'})
        materialize_content(base, {'good.md': 'a', 'sub/n.md': 'b', 'gone.md': None})
        self.assertEqual((base / 'sub' / 'n.md').read_text(), 'b')
        self.assertFalse((base / 'gone.md').exists())

    def test_seed_lists_share_no_mutable_state(self):
        def digest():
            h = __import__('hashlib').sha256()
            for p in sorted(deps.FIXTURES.rglob('*')):
                if p.is_file():
                    h.update(str(p.relative_to(deps.FIXTURES)).encode() + p.read_bytes())
            return h.hexdigest()
        table_before, files_before = repr(sorted(deps.TABLE.items())), digest()
        pristine = {a: deepcopy(deps.seeds(a)) for a in deps.ACTION_CLASSES}
        rows_before = {a: deepcopy(deps.seed_rows(a)) for a in deps.ACTION_CLASSES}
        controls_before = deepcopy(deps.file_controls())
        for action in deps.ACTION_CLASSES:
            for seed in deps.seeds(action):
                for event in (*seed.base_events, seed.event):
                    event['type'] = 'tampered'
                    event.clear()
                seed.content_files.clear()
                seed.content_files['../escape.md'] = 'tampered'
            for row in deps.seed_rows(action):
                row.clear()
        for control in deps.file_controls():
            control.clear()
        deps.scenario_ledgers('wave')['L-thinker-9001.jsonl'].clear()
        deps.scenario_content('wave').clear()
        self.assertEqual(repr(sorted(deps.TABLE.items())), table_before)
        self.assertEqual(digest(), files_before)
        for action in deps.ACTION_CLASSES:
            self.assertEqual(deps.seeds(action), pristine[action])
            self.assertEqual(deps.seed_rows(action), rows_before[action])
        self.assertEqual(deps.file_controls(), controls_before)
        for bad in ('nope', '', None, 7):
            with self.assertRaises(ValueError):
                deps.seeds(bad)
            with self.assertRaises(ValueError):
                deps.seed_rows(bad)
        print('seed lists, rows and controls: mutation left TABLE and fixtures unchanged')

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


def print_source_reads():
    """Regenerate the committed inventory: python3 -B src/test_door_deps.py --print-source-reads > src/fixtures/door_seed/source_reads.json

    Only the mechanical part (path, symbol, expression, route, classes, caller
    surface) is regenerated; run it, read the diff, and commit it deliberately.
    """
    roots = class_roots()
    entries = sorted({s for r in roots.values() for s in r} | set(INPUT_ROOTS))
    analysis = Analysis(SOURCE, entries)
    surfaces = {f.module: surface_for(f.module) for f in analysis.funcs.values() if surface_for(f.module)}
    inventory, graph, missing = build_inventory(analysis, roots, INPUT_ROOTS, surfaces, EXCLUSIONS)
    if analysis.problems or missing:
        raise SystemExit('refusing to write: %s %s' % (analysis.problems, missing))
    rows = lambda part: ',\n'.join('  ' + json.dumps(r, sort_keys=True) for r in inventory[part])
    print('{\n "graph": [\n%s\n ],\n "outside": [\n%s\n ]\n}' % (rows('graph'), rows('outside')))


if __name__ == '__main__':
    if sys.argv[1:] == ['--print-source-reads']:
        print_source_reads()
    else:
        unittest.main()
