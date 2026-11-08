#!/usr/bin/env python3
"""Write-time size and carry regressions; selectors are the acceptance groups."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

# Isolate before importing any module that can resolve the operator ledger.
_SANDBOX = tempfile.TemporaryDirectory(prefix='spec-size-')
os.environ.update(HOME=_SANDBOX.name, DOIT_ROOT=_SANDBOX.name,
                  DOIT_LEDGER_FILE='L-operator-0001.jsonl', DOIT_PROJECT='fixture')
import carry
import dispatch
import fold
import shape
import validate

REPO = Path(__file__).resolve().parent.parent
VALID = ('# fixture\nIn plain English: Check a complete spec.\n'
         '## Acceptance Criteria\nAC1 [backend]: x.\n  review_path: y\n'
         'Writes: a.py\n## Verification\n```bash\ntrue\n```\n## Notes\n')
BROKEN = VALID.replace('true\n', 'python wrong.py\n')
MECHANICAL = VALID.replace('Writes: a.py', 'Writes: a.py (annotation)')


def sized(text, length):
    return text + 'x' * (length - len(text.encode('utf-8')))


class Scratch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='revision-', dir=_SANDBOX.name)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for folder in ('events', 'content'):
            (self.root / folder).mkdir()
        self.subject = 'L-spec-7001'
        self.path = self.root / 'content' / f'{self.subject}.md'
        self.tick = 0
        self.calls = []
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root)))
        for module in (dispatch, fold):
            for key, value in (('ROOT', self.root), ('EVENTS', self.root / 'events')):
                self.patches.enter_context(patch.object(module, key, value))
        self.patches.enter_context(patch.object(fold, 'PROJECT', None))
        self.patches.enter_context(patch.object(carry, 'subprocess', SimpleNamespace(run=self.run_process)))

    def events(self, text, status='written', **extra):
        self.path.write_text(text, encoding='utf-8')
        out = dict(status=status, spec_id=self.subject, ac_count=1,
                   ac_types=['backend'], requirement_ids=['R7'], owed=0,
                   unknowns=0, footprint=['a.py'], declarations=[], escalations=[])
        out.update(extra)
        return dispatch.events_for('spec-writer', out,
                                   SimpleNamespace(path=str(self.path), subject=self.subject), {})

    def persist(self, events, *, subject=None, actor='spec-writer', name=None):
        self.tick += 1
        dst = self.root / 'events' / (name or f'L-{actor}-{self.tick:04d}.jsonl')
        base = dict(subject=subject or self.subject, project='another-project')
        # A registration and its failure have the same second, in append order.
        with patch.object(dispatch, 'now', return_value=f'2026-11-08T12:00:{self.tick:02d}+00:00'):
            for kind, fields in events:
                self.assertIsNone(dispatch.emit(dst, base, kind, strict=True, **fields))
        return dst

    def read(self):
        return [e for e in fold.read_events(use_cache=False) if e['subject'] == self.subject]

    def run_process(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[1] == 'dispatch':
            self.persist(self.events(self.drafts.pop(0)))
            return SimpleNamespace(returncode=0, stdout='{"spawn":"L-spec-writer-7001"}', stderr='')
        if cmd[1] == 'packet':
            return SimpleNamespace(returncode=0, stdout=str(self.slot), stderr='')
        if cmd[1] == 'append':
            fields = {}
            for kv in cmd[4:]:
                key, value = kv.split('=', 1)
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
                fields[key] = value
            self.persist([(cmd[2], fields)], actor='operator')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(f'Unexpected external process: {cmd}')

    def prepare_carry(self, *drafts):
        self.drafts = list(drafts)
        self.slot = self.root / 'slot.md'
        self.slot.write_text('carry source')
        result = (self.slot, self.subject, '1454', None, 'absent')
        self.patches.enter_context(patch.object(carry, 'resolve_target', return_value=('fixture', REPO)))
        self.patches.enter_context(patch.object(carry, 'carried_slot', return_value=result))
        self.patches.enter_context(patch.object(carry, 'pr_slot', return_value=result))
        self.patches.enter_context(patch.object(carry, 'resolve_record', return_value=(None, {})))
        self.patches.enter_context(patch.object(carry, '_title_body_from_ledger', return_value=('title', 'body')))


class SizeBoundary(unittest.TestCase):
    def test_bytes_and_exact_message(self):
        self.assertEqual(validate.SPEC_SIZE_CEILING, 51000)
        for text in ('', 'x' * 50999, 'x' * 51000, 'x' * 51001,
                     'é' * 25500, 'é' * 25500 + '\n', 'x' * 50999 + '\n\n'):
            with self.subTest(length=len(text.encode('utf-8'))):
                size = len(text.encode('utf-8'))
                expected = None if size <= 51000 else f'oversize: {size} > 51000'
                self.assertEqual(validate.spec_size(text), expected)

    def test_pure_predicate_and_durable_derivation(self):
        source = (REPO / 'src/validate.py').read_text()
        tree = ast.parse(source)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'spec_size')
        calls = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertEqual(calls, ['len', "spec_text.encode"])
        for fact in ('2026-11-01', '2026-11-08T05:54:01', '35/227',
                     '37000:17/67', '40000:14/54', '51000:7/24',
                     '60000:5/11', '73000:4/4', 'correlation', 'rewrites', '225'):
            self.assertIn(fact, source)


class WriteTimeEvents(Scratch):
    def test_findings_registration_warnings_and_declarations(self):
        for text in (sized(VALID, 50999), sized(VALID, 51000), sized(VALID, 51001),
                     BROKEN, sized(BROKEN, 51001)):
            with self.subTest(size=len(text.encode())):
                events = self.events(text)
                self.assertEqual(events[0][0], 'spec-written')
                registration = events[0][1]
                self.assertEqual(registration, dict(spec=self.subject, path=str(self.path),
                    ac_count=1, ac_types=['backend'], requirement_ids=['R7'],
                    owed_ac_count=0, unknown_count=0, footprint=['a.py']))
                expected = validate.spec_shape(text)
                finding = validate.spec_size(text)
                if finding:
                    expected.append(finding)
                failures = [fields['findings'] for kind, fields in events if kind == 'spec-shape-failed']
                self.assertEqual(failures, [expected] if expected else [])
                self.assertNotIn('spawn-failed', [kind for kind, _ in events])
                self.assertEqual([f['finding'] for k, f in events if k == 'spec-lint-warning'],
                                 validate.spec_shape_warnings(text))
        events = self.events(VALID, declarations=[dict(term='spec-unbuildable', line='split')],
                             escalations=[dict(asks='re-carve', blocks=['AC1'])])
        self.assertIn(('spec-unbuildable', dict(line='split')), events)
        self.assertIn(('question', dict(asks='re-carve', blocks=['AC1'])), events)

    def test_production_emit_read_pending_and_correction(self):
        self.persist(self.events(sized(VALID, 51001)))
        self.assertTrue(fold.spec_shape_pending(self.read()))
        self.persist(self.events(VALID))
        self.assertFalse(fold.spec_shape_pending(self.read()))


class ExistingSpecCompatibility(Scratch):
    def test_legacy_shape_unchanged(self):
        for text in (VALID, BROKEN):
            large = sized(text, 52000)
            self.assertEqual(validate.spec_shape(text), validate.spec_shape(large))
            self.assertEqual(validate.spec_shape_warnings(text), validate.spec_shape_warnings(large))
            self.assertEqual(shape.check(text), shape.check(large))

    def test_only_written_writer_measures(self):
        with patch.object(validate, 'spec_size', side_effect=AssertionError('unexpected size call')):
            self.events('', status='killed', killed_by_check=1)
            self.events('', status='split-proposed')
            dispatch.events_for('spec-auditor', dict(findings=[], rejected=[]), None, {})
            dispatch.events_for('research', dict(path='research.md', answered=True), None, {})
        sites = []
        for path in (REPO / 'src').glob('*.py'):
            if path.name.startswith('test_'):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Attribute) and node.func.attr == 'spec_size' or
                    isinstance(node.func, ast.Name) and node.func.id == 'spec_size'):
                    sites.append(path.name)
        self.assertEqual(sites, ['dispatch.py'])
        print('Production spec_size call sites:', sites)


class ContractSizing(unittest.TestCase):
    def test_complete_goal_sizing_and_schema_supported_handback(self):
        planner = (REPO / 'agents/planner.md').read_text().split('### ① ')[1].split('### ② ')[0]
        writer = (REPO / 'agents/spec-writer.md').read_text()
        for document in (planner, writer):
            for marker in ('validate.SPEC_SIZE_CEILING', 'UTF-8', 'headings', 'criteria',
                           'Verification', 'split', 'complete'):
                self.assertIn(marker, document)
        for marker in ('before final write', 'spec-unbuildable', 'split-proposed',
                       'Planner', 'CarryFailed', 'shape-only', 'without dropping scope'):
            self.assertIn(marker, writer)
        schema = json.loads((REPO / 'agents/spec-writer.schema.json').read_text())
        self.assertIn('split-proposed', schema['properties']['status']['enum'])
        self.assertIn('split', schema['properties'])
        self.assertIn('spec-unbuildable', json.dumps(schema['properties']['declarations']))
        self.assertIsNone(validate.spec_size(sized(VALID, 51000)))
        self.assertEqual(validate.spec_size(sized(VALID, 51001)), 'oversize: 51001 > 51000')


class CarryOversize(Scratch):
    def assert_failed(self, initial, rework=None):
        self.prepare_carry(initial, *([rework] if rework else []))
        with self.assertRaisesRegex(carry.CarryFailed,
                                    self.subject + '.*oversize: 51001 > 51000.*Planner.*re-carv'):
            carry.carry('1454', repo=REPO, project='fixture')
        events = self.read()
        self.assertTrue(fold.spec_shape_pending(events))
        self.assertNotIn('spec-carried', [e['type'] for e in events])
        self.assertEqual(sum(e['type'] == 'spec-written' for e in events), 2 if rework else 1)
        self.assertEqual(sum(c[1] == 'dispatch' for c in self.calls), 2 if rework else 1)
        self.assertEqual(sum(c[1] == 'packet' for c in self.calls), 1 if rework else 0)
        self.assertFalse(any(c[1:3] == ['append', 'spec-written'] for c in self.calls))

    def test_initial_oversize_and_cli_failure(self):
        self.assert_failed(sized(VALID, 51001))
        self.drafts = [sized(VALID, 51001)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(carry.main(['1454', '--repo', str(REPO), '--project', 'fixture']), 1)
        self.assertIn('oversize: 51001 > 51000', err.getvalue())

    def test_malformed_oversize_cannot_mechanically_restamp(self):
        self.assertTrue(shape.check(MECHANICAL)['block'])
        self.assertFalse(shape.check(shape.mechanical_fix(MECHANICAL)[0])['block'])
        self.assert_failed(sized(MECHANICAL, 51001))

    def test_shape_rework_oversize_fails(self):
        self.assert_failed(BROKEN, sized(VALID, 51001))

    def test_fitting_clean_mechanical_and_writer_repairs(self):
        for initial, rework in ((VALID, None), (MECHANICAL, None), (BROKEN, VALID)):
            with self.subTest(initial=initial):
                self.prepare_carry(initial, *([rework] if rework else []))
                self.assertEqual(carry.carry('1454', repo=REPO, project='fixture', force=True), self.subject)
                self.assertFalse(fold.spec_shape_pending(self.read()))
                self.assertEqual(self.read()[-1]['type'], 'spec-carried')

    def test_pr_and_ledger_pr_boundaries(self):
        for from_ledger in (False, True):
            self.prepare_carry(sized(VALID, 51001))
            with self.assertRaises(carry.CarryFailed):
                carry._do_carry('https://example.test/pull/1', repo=REPO, project='fixture',
                                force=True, from_ledger=from_ledger)
            self.assertTrue(fold.spec_shape_pending(self.read()))

    def test_revision_chronology_and_same_spawn_order(self):
        self.persist(self.events(sized(VALID, 51001)), name='L-spec-writer-9000.jsonl')
        self.persist(self.events(VALID), name='L-spec-writer-0001.jsonl')
        original = carry.ledger_files.names
        with patch.object(carry.ledger_files, 'names', side_effect=lambda directory: sorted(original(directory))):
            carry._shape_gate(self.subject, self.path, None, None, REPO, 'fixture',
                              carry.spec_written(self.subject))
            self.assertEqual(carry.spec_written(self.subject)['ts'], self.read()[-1]['ts'])
        # Even the reverse enumeration cannot change a current same-spawn pair.
        self.persist(self.events(sized(VALID, 51001)), name='L-spec-writer-0002.jsonl')
        with patch.object(carry.ledger_files, 'names', side_effect=lambda directory: sorted(original(directory), reverse=True)):
            with self.assertRaises(carry.CarryFailed):
                carry._shape_gate(self.subject, self.path, None, None, REPO, 'fixture',
                                  carry.spec_written(self.subject))
        self.assertTrue(fold.spec_shape_pending(self.read()))

    def test_other_subject_and_legacy_text_are_not_size_gated(self):
        self.persist(self.events(sized(VALID, 51001)), subject='L-spec-7002')
        # Legacy registration has no recorded size finding; never remeasure.
        self.persist([('spec-written', dict(path=str(self.path), footprint=['a.py']))])
        with patch.object(validate, 'spec_size', side_effect=AssertionError('retroactive size gate')):
            self.assertEqual(carry._shape_gate(self.subject, self.path, None, None, REPO,
                                              'fixture', carry.spec_written(self.subject)), [])


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        _SANDBOX.cleanup()
