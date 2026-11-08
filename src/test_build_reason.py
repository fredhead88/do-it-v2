"""Unfinished builds carry an actionable reason (L-spec-2279 R6).

Every test writes only to disposable scratch roots and never launches a backend.
Select a class by name: FoldReasonTests, SchemaReasonTests, ReasonRoundTripTests,
WrapperRefusalTests, CompatibilityTests.
"""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_import_root = tempfile.TemporaryDirectory(prefix='build-reason-import-')
os.environ['DOIT_ROOT'] = _import_root.name
os.environ.pop('DOIT_LEDGER_FILE', None)
os.environ['DOIT_NO_POKE'] = '1'
os.environ['DOIT_TEST_PROCESS'] = '1'

import build_readiness as readiness  # noqa: E402 -- isolation precedes imports
import dispatch  # noqa: E402
import fold  # noqa: E402
import grading_env  # noqa: E402
import models  # noqa: E402

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
SHA = 'a' * 40
UNFINISHED = ('BLOCKED', 'NEEDS_CONTEXT')
INVALID = [('absent', fold_absent := object()), ('null', None), ('empty', ''), ('spaces', '   '),
           ('tab-newline', '\t\n'), ('integer', 7), ('boolean', True), ('list', ['why']),
           ('object', {'why': 'x'})]


def event(status, reason=fold_absent, **extra):
    row = dict(type='build-done', subject='L-spec-9279', project='fixture', status=status, **extra)
    if reason is not fold_absent:
        row['reason'] = reason
    return row


def card(status='DONE', reason=None, sha=SHA, subject='L-spec-9279'):
    out = dict(
        status=status,
        identity=dict(spec_id=subject, built_by='L-builder-9279', branch=subject.lower(),
                      base_sha=sha, ready_sha=sha),
        acs=[dict(id='AC1', criterion_type='backend', evidence='fixture', evidence_type='none',
                  check='[UNVERIFIED]', disposition='not-done')],
        verify=dict(command='true', exit_code=0, result='fixture'),
        stubs=[], deviations=[], tests=dict(added=True), not_built=[], unknowns=[],
        built_against=[], escalations=[], declarations=[])
    if reason is not None:
        out['reason'] = reason
    return out


def schema_errors(obj):
    import jsonschema
    schema = json.loads((REPO / 'agents/builder.schema.json').read_text())
    return list(jsonschema.Draft202012Validator(schema).iter_errors(obj))


@contextlib.contextmanager
def scratch_ledger(filename='L-builder-9279.jsonl'):
    """Disposable roots for the writers; yields (root, events directory, ledger path)."""
    with tempfile.TemporaryDirectory(prefix='build-reason-') as name:
        root = Path(name).resolve()
        events = root / 'events'
        events.mkdir()
        with patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_LEDGER_FILE=filename), \
                patch.multiple(fold, ROOT=root, EVENTS=events), \
                patch.multiple(dispatch, ROOT=root, EVENTS=events, CONTENT=root / 'content'):
            (root / 'content').mkdir()
            yield root, events, events / filename


class FoldReasonTests(unittest.TestCase):
    def expected(self, status):
        return f'build-done status={status} requires a non-empty reason'

    def test_check_append_matrix(self):
        for status in UNFINISHED:
            for label, value in INVALID:
                with self.subTest(status=status, reason=label):
                    row = event(status, value)
                    self.assertEqual(fold.check_append(row, 'builder'), self.expected(status))
                    self.assertEqual(fold.required_reason(row), self.expected(status))
            self.assertIsNone(fold.check_append(event(status, 'needs the base interface'), 'builder'))
            self.assertIsNone(fold.check_append(event(status, '  padded  '), 'builder'))

    def test_done_and_actor_admission_unchanged(self):
        self.assertIsNone(fold.check_append(event('DONE'), 'builder'))
        self.assertIsNone(fold.check_append(event('DONE', ''), 'builder'))
        self.assertIsNone(fold.check_append(event('BLOCKED', 'real reason'), 'builder'))
        cancelled = dict(type='spawn-failed', subject='L-spec-9279', reason='cancelled')
        self.assertIn('may not emit', fold.check_append(cancelled, 'builder'))

    def test_fold_append_refuses_in_both_modes_without_a_row(self):
        for strict in (True, False):
            for status in UNFINISHED:
                for label, value in INVALID:
                    with self.subTest(strict=strict, status=status, reason=label), \
                            scratch_ledger() as (root, events, ledger):
                        argv = ['build-done', 'L-spec-9279', f'status={status}', 'project=fixture']
                        if value is not fold_absent:
                            argv.append('reason:=' + json.dumps(value))
                        with self.assertRaises(SystemExit) as refused:
                            fold.append(argv, strict=strict)
                        self.assertIn(self.expected(status), str(refused.exception))
                        self.assertFalse(ledger.exists())

    def test_fold_append_accepts_a_reason(self):
        for status in UNFINISHED:
            with scratch_ledger() as (root, events, ledger):
                row = fold.append(['build-done', 'L-spec-9279', f'status={status}', 'project=fixture',
                                   'reason=blocked on the missing base interface'])
                self.assertEqual(row['reason'], 'blocked on the missing base interface')
                self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_dispatch_emit_refuses_in_both_modes_without_a_row(self):
        base = dict(subject='L-spec-9279', project='fixture', spawn='L-builder-9279')
        for strict in (True, False):
            for status in UNFINISHED:
                for label, value in INVALID:
                    with self.subTest(strict=strict, status=status, reason=label), \
                            scratch_ledger() as (root, events, ledger):
                        kv = dict(status=status)
                        if value is not fold_absent:
                            kv['reason'] = value
                        self.assertEqual(dispatch.emit(ledger, base, 'build-done', strict=strict, **kv),
                                         self.expected(status))
                        self.assertFalse(ledger.exists())
                with scratch_ledger() as (root, events, ledger):
                    self.assertIsNone(dispatch.emit(ledger, base, 'build-done', strict=strict,
                                                    status=status, reason='real reason'))
                    self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_legacy_unfinished_rows_still_fold(self):
        legacy = [dict(v=1, ts='2026-10-01T00:00:00+00:00', type='build-done', subject='L-spec-9001',
                       project='fixture', status=status, actor='builder', _src='L-builder-0001.jsonl:1')
                  for status in UNFINISHED]
        for row in legacy:
            self.assertIsNone(fold.admits(row, 'builder'))
        fold.fold([copy.deepcopy(r) for r in legacy])


class SchemaReasonTests(unittest.TestCase):
    def test_unfinished_statuses_require_a_nonblank_string(self):
        for status in UNFINISHED:
            self.assertEqual(schema_errors(card(status, 'missing context; supply the interface')), [])
            self.assertTrue(schema_errors(card(status)), (status, 'absent'))
            for label, value in INVALID[1:]:
                with self.subTest(status=status, reason=label):
                    self.assertTrue(schema_errors(card(status) | {'reason': value}), (status, label))

    def test_length_cap_and_done(self):
        for status in UNFINISHED:
            self.assertEqual(schema_errors(card(status, 'x' * 1000)), [])
            self.assertTrue(schema_errors(card(status, 'x' * 1001)))
        self.assertEqual(schema_errors(card('DONE')), [])
        self.assertEqual(schema_errors(card('DONE', 'optional note')), [])

    def test_schema_has_the_two_status_conditional(self):
        schema = json.loads((REPO / 'agents/builder.schema.json').read_text())
        self.assertEqual(set(schema['if']['properties']['status']['enum']), set(UNFINISHED))
        self.assertEqual(schema['then'], {'required': ['reason']})
        self.assertEqual(schema['properties']['reason']['minLength'], 1)

    def test_instructions_require_reason_for_both_statuses(self):
        text = ' '.join((REPO / 'agents/builder.md').read_text().split())
        self.assertIn('For BLOCKED and NEEDS_CONTEXT, return a nonblank top-level `reason`', text)
        self.assertIn('what would unblock it', text)
        self.assertIn('refused, recorded as a failed spawn, and your card is kept', text)


class ReasonRoundTripTests(unittest.TestCase):
    def test_helper_semantics(self):
        for status in UNFINISHED:
            self.assertEqual(readiness.build_done_fields({'status': status, 'reason': '  why and fix  '}),
                             {'reason': 'why and fix'})
            for label, value in INVALID:
                out = {'status': status}
                if value is not fold_absent:
                    out['reason'] = value
                with self.subTest(status=status, reason=label), self.assertRaises(readiness.NotReady):
                    readiness.build_done_fields(out)
        self.assertEqual(readiness.build_done_fields({'status': 'DONE'}), {})
        self.assertEqual(readiness.build_done_fields({'status': 'DONE', 'reason': 'note'}), {})
        self.assertTrue(issubclass(readiness.NotReady, ValueError))

    def test_persisted_reason_read_by_a_separate_process(self):
        for status in UNFINISHED:
            with self.subTest(status=status), scratch_ledger() as (root, events, ledger):
                out = card(status, '  nothing to build against; supply the interface  ')
                args = argparse.Namespace(subject='L-spec-9279', cwd=str(root), pinned_base_sha=SHA)
                base = dict(subject='L-spec-9279', project='fixture', spawn='L-builder-9279')
                rows = dispatch.events_for('builder', out, args, base)
                for kind, fields in rows:
                    self.assertIsNone(dispatch.emit(ledger, base, kind, strict=True, **fields))
                reader = ('import fold, json; print(json.dumps([e for e in fold.read_events(use_cache=False)'
                          ' if e["type"] == "build-done"]))')
                env = dict(os.environ, DOIT_ROOT=str(root), PYTHONPATH=str(SRC), DOIT_NO_POKE='1')
                done = subprocess.run([sys.executable, '-B', '-c', reader], capture_output=True,
                                      text=True, env=env, cwd=str(root), check=True)
                stored = json.loads(done.stdout)
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0]['reason'], 'nothing to build against; supply the interface')
                self.assertEqual(stored[0]['status'], status)
                self.assertEqual(stored[0]['base_sha'], SHA)
                persisted = json.loads((root / 'content/L-card-9279.json').read_text())
                self.assertEqual(persisted['status'], status)
                self.assertEqual(persisted['identity'], out['identity'])
                self.assertTrue((root / 'content/L-card-9279.md').is_file())
                if status == 'BLOCKED':
                    self.assertEqual(sum(t == 'build-blocked' for t, _ in rows), 1)


class WrapperRefusalTests(unittest.TestCase):
    def run_handin(self, status, reason=None, inject=False):
        """The real dispatch.main for a builder with a synthetic backend; returns (exit code, rows, root)."""
        with tempfile.TemporaryDirectory(prefix='build-reason-wrapper-') as name:
            root = Path(name).resolve()
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-qb', 'fixture'], cwd=repo, check=True)
            packet = root / 'packet.md'
            packet.write_text('fixture\nPINNED_BASE_SHA: ' + SHA + '\n')
            result = card(status, reason)
            response = subprocess.CompletedProcess(
                [], 0, json.dumps(dict(is_error=False, structured_output=result)), '')
            mapping = dict(backend='codex', model='fixture', source='fixture')
            real = fold.required_reason

            def refusing(row):
                if inject and row.get('type') == 'build-done' and row.get('status') in UNFINISHED:
                    return f"build-done status={row['status']} requires a non-empty reason"
                return real(row)

            with patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NO_POKE='1'), \
                    patch.multiple(dispatch, ROOT=root, EVENTS=root / 'events', CONTENT=root / 'content'), \
                    patch.multiple(fold, ROOT=root, EVENTS=root / 'events'), \
                    patch.object(models, 'resolve', return_value=mapping), \
                    patch.object(dispatch, 'ensure_room', return_value=True), \
                    patch.object(dispatch, 'builder_worktree_preflight'), \
                    patch.object(dispatch, 'provision_worktree_env', return_value='absent'), \
                    patch.object(dispatch.spawns, 'gate', return_value=None), \
                    patch.object(dispatch.atexit, 'register'), \
                    patch.object(grading_env, 'failure_kind', return_value='work'), \
                    patch.object(fold, 'required_reason', side_effect=refusing), \
                    patch.object(dispatch, 'run_codex', return_value=response), \
                    patch.object(dispatch, 'run_seat', side_effect=AssertionError('unexpected seat')):
                os.environ.pop('DOIT_SEAT', None)
                os.environ.pop('DOIT_LEDGER_FILE', None)
                args = argparse.Namespace(role='builder', subject='L-spec-9279', packet=str(packet),
                                          path=None, cwd=str(repo), charter=None, project='fixture',
                                          mcp_config=None, timeout=None, max_usd=None, seat=False)
                code = 0
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        dispatch.main(args)
                    except SystemExit as exc:
                        code = exc.code
                rows = fold.read_events(use_cache=False)
                cards = sorted((root / 'content').glob('L-card-*'))
                return code, rows, [json.loads(p.read_text()) for p in cards if p.suffix == '.json']

    def test_refusal_records_spawn_failed_and_keeps_the_card(self):
        for status in UNFINISHED:
            with self.subTest(status=status):
                code, rows, cards = self.run_handin(status, 'a real reason', inject=True)
                self.assertEqual(code, 1)
                failed = [e for e in rows if e['type'] == 'spawn-failed']
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0]['reason'], 'build-done-refused:no-reason')
                self.assertFalse(any(e['type'] in ('build-done', 'spawn-done') for e in rows))
                self.assertFalse(any(e['type'] in ('grade', 'graded', 'grading-started') for e in rows))
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]['status'], status)

    def test_valid_unfinished_and_done_hand_ins_complete(self):
        for status, reason in (('BLOCKED', 'waiting on the base interface'),
                               ('NEEDS_CONTEXT', 'need the sibling signature'), ('DONE', None)):
            with self.subTest(status=status):
                code, rows, cards = self.run_handin(status, reason)
                self.assertEqual(code, 0, rows)
                done = [e for e in rows if e['type'] == 'build-done']
                self.assertEqual(len(done), 1)
                self.assertEqual(done[0].get('reason'), reason)
                self.assertEqual(sum(e['type'] == 'spawn-done' for e in rows), 1)
                self.assertFalse(any(e['type'] == 'spawn-failed' for e in rows))

    def test_unrelated_emit_failures_keep_their_classification(self):
        with scratch_ledger() as (root, events, ledger):
            base = dict(subject='L-spec-9279', project='fixture', spawn='L-builder-9279')
            refused = dispatch.emit(ledger, base, 'autodispatch-failed', strict=True)
            self.assertIn('missing required field', refused)
            self.assertNotIn('build-done', refused)


class CompatibilityTests(unittest.TestCase):
    def test_build_readiness_suite_passes_unchanged(self):
        env = dict(os.environ, PYTHONPATH=str(SRC), DOIT_NO_POKE='1')
        done = subprocess.run([sys.executable, '-B', str(SRC / 'test_build_readiness.py')],
                              capture_output=True, text=True, env=env, cwd=str(REPO))
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn('requires a nonempty reason', (SRC / 'test_build_readiness.py').read_text())


if __name__ == '__main__':
    unittest.main()
