#!/usr/bin/env python3
"""Append read budgets, writer clocks, fixed-revision equality and store receipts."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Imports cannot see the operator's configuration or event store.
with tempfile.TemporaryDirectory(prefix='append-import-') as directory:
    with patch.dict(os.environ, DOIT_ROOT=directory):
        import fold
        import merge_gate
        import nextstep
        import problems
        import project_scope
        import window_policy

REPO = Path(__file__).resolve().parent.parent
CLOCK = datetime(2026, 1, 2, tzinfo=timezone.utc)
SID = 'L-spec-9901'


def row(kind='observed', **fields):
    return dict(v=1, ts=CLOCK.isoformat(timespec='seconds'), type=kind,
                subject=SID, **fields)


def receipt(criterion, body):
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(url='cli://append/' + criterion, status=0,
                          body_sha256=hashlib.sha256(encoded).hexdigest(),
                          body_excerpt=body), sort_keys=True))


class ScratchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='append-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            fold, ROOT=self.root, EVENTS=self.events, BOARD=self.root / 'board.md',
            SESSIONS=self.root / 'sessions', PROJECT=None))
        self.stack.enter_context(patch.dict(os.environ, DOIT_ROOT=str(self.root),
            DOIT_LEDGER_FILE='L-operator-0001.jsonl', DOIT_PROJECT='',
            DOIT_READ_CACHE='0', V4_LEDGER_DIR=str(self.root / 'v4-ledger'),
            V4_INBOX_DIR=str(self.root / 'v4-inbox')))

    def seed(self, name, rows):
        path = self.events / name
        path.write_text(''.join(json.dumps(e, sort_keys=True) + '\n' for e in rows))
        return path

    def append(self, *args, **kwargs):
        return fold.append(list(args), **kwargs)


class ReadBudgetTests(ScratchTests):
    def counted(self, project, kind, snapshot=None):
        history = [row('spec-written', project='registered', actor='spec-writer',
                       _src='L-spec-writer-0001.jsonl:1'),
                   row('owed-ac', criterion='AC1', actor='spec-writer',
                       _src='L-spec-writer-0001.jsonl:2')]
        if project == 'cwd':
            history = [e for e in history if e['type'] != 'spec-written']
        if snapshot is not None:
            history = snapshot
        args = ['observed', SID]
        filename = 'L-operator-0001.jsonl'
        if kind.startswith('decision'):
            args = ['decision', SID]
            if kind == 'decision-ref':
                args += ['ref=not-a-window:1']
        elif kind.startswith('owed'):
            args = ['owed-ac', SID, 'criterion=AC1']
            filename = ('L-executor-0001.jsonl' if kind == 'owed-executor'
                        else 'L-spec-writer-0001.jsonl')
        elif kind == 'lesson':
            args = ['lesson', SID, 'problem=read-budget', 'text=shared snapshot']
        if project == 'explicit':
            args += ['project=do-it-v2']
        with patch.object(fold, 'PROJECT', 'do-it-v2' if project == 'configured' else None), \
                patch.dict(os.environ, DOIT_LEDGER_FILE=filename), \
                patch.object(fold, 'read_events', return_value=history) as reads, \
                patch.object(project_scope, 'subject_project', wraps=project_scope.subject_project) as infer, \
                patch.object(window_policy, 'check_decision', wraps=window_policy.check_decision) as decision, \
                patch.object(fold, 'fold', wraps=fold.fold) as folded, \
                patch.object(problems, 'occurrence_count', wraps=problems.occurrence_count) as count:
            result = fold.append(args, strict=True)
        target = int(project in ('history', 'cwd') or
                     kind in ('decision-ref', 'owed-executor', 'lesson'))
        self.assertEqual(reads.call_count, target, (project, kind))
        if project in ('history', 'cwd'):
            self.assertIs(infer.call_args.args[0], history)
        else:
            infer.assert_not_called()
        if kind == 'decision-ref':
            self.assertIs(decision.call_args.args[1], history)
        elif kind == 'owed-executor':
            self.assertIs(folded.call_args.args[0], history)
        elif kind == 'lesson':
            self.assertIs(count.call_args.args[0], history)
        else:
            folded.assert_not_called()
        print(f'{project} / {kind}: measured {reads.call_count}, target {target}')
        return result

    def test_matrix(self):
        cases = []
        for project in ('explicit', 'configured', 'history', 'cwd'):
            for kind in ('ordinary', 'decision-ref', 'decision-no-ref',
                         'owed-executor', 'owed-declarer', 'lesson'):
                with self.subTest(project=project, kind=kind):
                    self.counted(project, kind)
                    cases.append(project + '/' + kind)
        self.assertEqual(len(cases), 24)
        receipt('AC1', {'matrix': cases, 'targets_checked': 24})

    def test_missing_project_lesson_reads_once(self):
        self.counted('history', 'lesson')

    def test_missing_project_decision_reads_once(self):
        self.counted('history', 'decision-ref')

    def test_missing_project_owed_reads_once(self):
        self.counted('history', 'owed-executor')

    def test_known_project_ordinary_reads_zero(self):
        self.counted('explicit', 'ordinary')
        self.counted('configured', 'ordinary')

    def test_empty_snapshot_is_shared(self):
        self.counted('cwd', 'lesson', [])
        self.counted('cwd', 'decision-ref', [])

    def test_second_append_refreshes_history(self):
        snapshots = [[], []]
        with patch.object(fold, 'read_events', side_effect=snapshots) as reads, \
                patch.object(project_scope, 'subject_project', wraps=project_scope.subject_project) as infer, \
                patch.object(problems, 'occurrence_count', wraps=problems.occurrence_count) as count:
            for _ in range(2):
                self.append('lesson', SID, 'text=refresh')
        self.assertEqual(reads.call_count, 2)
        for index in range(2):
            self.assertIs(infer.call_args_list[index].args[0], snapshots[index])
            self.assertIs(count.call_args_list[index].args[0], snapshots[index])

    def test_refusals_do_not_write(self):
        escalation = row('escalation-blocking',
                         actor='builder', _src='L-builder-0001.jsonl:1')
        escalation['subject'] = 'window-fixture'
        for args, filename, history in (
            (['decision', SID, 'ref=L-builder-0001.jsonl:1'], 'L-operator-0001.jsonl', [escalation]),
            (['owed-ac', SID, 'criterion=AC1'], 'L-executor-0001.jsonl', []),
            (['observed', SID, 'actor=operator'], 'L-operator-0001.jsonl', [])):
            with self.subTest(args=args), patch.dict(os.environ, DOIT_LEDGER_FILE=filename), \
                    patch.object(fold, 'read_events', return_value=history) as reads:
                destination = self.events / filename
                before = destination.read_bytes() if destination.exists() else None
                with self.assertRaises(SystemExit):
                    fold.append(args, strict=True)
                self.assertLessEqual(reads.call_count, 1)
                self.assertEqual(destination.read_bytes() if destination.exists() else None, before)


class ClockTests(ScratchTests):
    def generated_clock(self):
        # Identity, not equality, selects the default; make import time deliberately old.
        self.stack.enter_context(patch.object(fold, '_IMPORT_NOW', CLOCK, create=True))
        self.stack.enter_context(patch.object(fold, 'NOW', CLOCK))
        return self.stack.enter_context(patch.object(fold, 'datetime', wraps=datetime))

    def persisted(self):
        return [json.loads(line) for line in (self.events / os.environ['DOIT_LEDGER_FILE']).read_text().splitlines()]

    def test_default_append_two_seconds_apart(self):
        clock = self.generated_clock()
        wall = datetime(2031, 2, 3, tzinfo=timezone.utc)
        clock.now.side_effect = [wall, wall + timedelta(seconds=2)]
        first = self.append('observed', SID, 'project=do-it-v2')
        second = self.append('observed', SID, 'project=do-it-v2')
        self.assertEqual(datetime.fromisoformat(second['ts']) - datetime.fromisoformat(first['ts']), timedelta(seconds=2))
        self.assertEqual(self.persisted(), [first, second])
        self.assertIs(fold.NOW, fold._IMPORT_NOW)
        clock.now.assert_any_call(timezone.utc)
        receipt('AC2', {'timestamp_pair': [first['ts'], second['ts']], 'delta_seconds': 2})

    def test_real_clock_receipt_bracket(self):
        self.assertIs(fold.NOW, fold._IMPORT_NOW)
        before = datetime.now(timezone.utc).replace(microsecond=0)
        result = self.append('observed', SID, 'project=do-it-v2')
        after = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertLessEqual(before, datetime.fromisoformat(result['ts']))
        self.assertLessEqual(datetime.fromisoformat(result['ts']), after)
        self.assertEqual(self.persisted()[-1], result)
        print('receipt bracket:', before.isoformat(), result['ts'], after.isoformat())

    def test_rebound_clock_identity(self):
        original = fold._IMPORT_NOW
        self.assertIs(fold.NOW, original)
        equal = datetime.fromisoformat(original.isoformat())
        self.assertEqual(equal, original)
        self.assertIsNot(equal, original)
        with patch.object(fold, 'NOW', equal), patch.object(fold, 'datetime', wraps=datetime) as clock:
            for _ in range(2):
                self.assertEqual(self.append('observed', SID, 'project=do-it-v2')['ts'], equal.isoformat(timespec='seconds'))
            clock.now.assert_not_called()
        self.assertIs(fold.NOW, original)
        with patch.object(fold, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = CLOCK + timedelta(days=800)
            self.assertEqual(self.append('observed', SID, 'project=do-it-v2')['ts'], clock.now.return_value.isoformat(timespec='seconds'))
        with patch.object(fold, 'NOW', CLOCK):
            for _ in range(3):
                self.assertEqual(self.append('observed', SID, 'project=do-it-v2')['ts'], CLOCK.isoformat(timespec='seconds'))
            supplied = '1999-01-01T00:00:00.123456+00:00'
            self.assertEqual(self.append('observed', SID, 'project=do-it-v2', 'ts=' + supplied)['ts'], supplied)

    def test_merge_gate_tooling_clock(self):
        clock = self.generated_clock()
        wall = datetime(2031, 2, 3, tzinfo=timezone.utc)
        clock.now.return_value = wall
        earlier = self.append('observed', SID, 'project=do-it-v2')
        review = dict(ts=CLOCK.isoformat(timespec='seconds'))
        with patch.object(merge_gate, '_COMMON', '/scratch/repo'), \
                patch.dict(os.environ, DOIT_LEDGER_FILE='L-executor-0001.jsonl'), \
                patch.object(nextstep, '_gate_attempts', wraps=nextstep._gate_attempts) as attempts:
            history = []
            for index, minutes in ((1, 5), (2, 15)):
                now = wall + timedelta(seconds=index * 2)
                clock.now.return_value = now
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(merge_gate._emit_tooling(SID, 'feature', 'a' * 40, 'b' * 40,
                                     history, review, 'timeout', True), 2)
                stored = self.persisted()[-1]
                local = attempts.call_args.args[1]
                self.assertEqual(local['ts'], stored['ts'])
                self.assertEqual(stored['ts'], now.isoformat(timespec='seconds'))
                self.assertGreaterEqual(stored['ts'], earlier['ts'])
                self.assertEqual(datetime.fromisoformat(stored['retry_at']) - now, timedelta(minutes=minutes))
                history.append(stored)
            with patch.object(fold, 'NOW', CLOCK + timedelta(days=1)):
                with contextlib.redirect_stdout(io.StringIO()):
                    merge_gate._emit_tooling(SID, 'feature', 'a' * 40, 'b' * 40,
                                            [], review, 'timeout', True)
                stored = self.persisted()[-1]
                self.assertEqual(stored['ts'], fold.NOW.isoformat(timespec='seconds'))
                self.assertEqual(datetime.fromisoformat(stored['retry_at']) - fold.NOW, timedelta(minutes=5))
                self.assertEqual(attempts.call_args.args[1]['ts'], stored['ts'])


class RoundTripTests(ScratchTests):
    def test_committed_record_fresh_handle(self):
        prefix = self.seed('L-operator-0001.jsonl', [row(text='old prefix')]).read_bytes()
        result = self.append('observed', SID, 'project=do-it-v2', 'message=π\nframing')
        path = self.events / 'L-operator-0001.jsonl'
        with path.open('rb') as fresh:
            data = fresh.read()
        self.assertTrue(data.startswith(prefix))
        self.assertEqual(data[-1:], b'\n')
        self.assertEqual(len(data.splitlines()), 2)
        self.assertEqual(json.loads(data.splitlines()[-1]), result)
        inbox = self.append('observed', SID, 'project=do-it-v2', '--inbox')
        with (self.events / 'L-inbox-local.jsonl').open() as fresh:
            self.assertEqual(json.load(fresh), inbox)
        self.assertEqual(inbox['project'], 'do-it-v2')
        self.assertTrue(datetime.fromisoformat(inbox['ts']).tzinfo)
        receipt('AC4', {'fresh_handle': True, 'prefix_bytes': len(prefix), 'routing': 'L-inbox-local.jsonl'})

    def test_second_append_reads_first_commit(self):
        self.seed('L-operator-seed.jsonl', [row('lesson', project='do-it-v2',
                  problem='append-shared', statement='shared history loses records')])
        prefix = (self.events / 'L-operator-seed.jsonl').read_bytes()
        stderr = []
        for _ in range(2):
            message = io.StringIO()
            with contextlib.redirect_stderr(message):
                self.append('lesson', SID, 'problem=append-share', 'statement=shared history loses records')
            stderr.append(message.getvalue())
        self.assertIn('mint:', stderr[0])
        self.assertEqual(stderr[1], '')
        self.assertEqual(problems.occurrence_count(fold.read_events(), 'append-share'), 2)
        self.assertEqual((self.events / 'L-operator-seed.jsonl').read_bytes(), prefix)
        self.assertEqual(len((self.events / 'L-operator-0001.jsonl').read_text().splitlines()), 2)

    def test_strict_refusal_preserves_store(self):
        path = self.events / 'L-executor-0001.jsonl'
        with patch.dict(os.environ, DOIT_LEDGER_FILE=path.name):
            for initial in (None, b'{"ts":"2026-01-01","type":"observed","subject":"old"}\n'):
                if initial is not None:
                    path.write_bytes(initial)
                with self.assertRaises(SystemExit):
                    self.append('owed-ac', SID, 'criterion=AC1', strict=True)
                self.assertEqual(path.read_bytes() if path.exists() else None, initial)


# This driver is executed by BOTH revisions in fresh processes. Only the module
# search path changes: the oracle imports the original files from Git, unedited.
ORACLE_DRIVER = r'''
import contextlib, io, json, os, pathlib, runpy, subprocess, sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import fold
fold.NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
case = json.loads(sys.stdin.read())
fold.PROJECT = case.get('configured')
stdout, stderr = io.StringIO(), io.StringIO()
result, status = None, 0
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    try:
        if case.get('entry') == 'wrapper':
            run = subprocess.run([str(pathlib.Path(fold.__file__).parent.parent / 'doit'),
                                  'append'] + case['argv'], capture_output=True, text=True, timeout=60)
            stdout.write(run.stdout)
            stderr.write(run.stderr)
            status = run.returncode
        elif case.get('entry') == 'cli':
            sys.argv = ['fold.py', 'append'] + case['argv']
            runpy.run_path(str(pathlib.Path(fold.__file__)), run_name='__main__',
                           init_globals={})
        else:
            result = fold.append(case['argv'], strict=case.get('strict', False))
    except SystemExit as exc:
        status = exc.code
# CLI run_path imports its own clock. CLI equality uses explicit ts; board's
# header alone uses the process import clock, pinned by a datetime module shim
# in the parent-provided bootstrap (see run_case).
events = fold.read_events()
import problems
slugs = sorted({e.get('problem', 'unassigned') for e in events if e.get('type') == 'lesson'})
print(json.dumps(dict(result=result, status=status, stdout=stdout.getvalue(), stderr=stderr.getvalue(),
    files={p.name:p.read_bytes().hex() for p in sorted(fold.EVENTS.glob('*.jsonl'))},
    events=events, folded=fold.fold(events), problems=problems.register(events),
    counts={slug:problems.occurrence_count(events, slug) for slug in slugs},
    unassigned=problems.unassigned_summary(events)), sort_keys=True, default=str))
'''


@contextlib.contextmanager
def original_checkout(revision):
    # The checkout is always removed; retained reports contain no clone/baseline.
    with tempfile.TemporaryDirectory(prefix='append-oracle-') as directory:
        checkout = Path(directory) / 'repo'
        subprocess.run(['git', '-C', str(REPO), 'worktree', 'add', '--quiet', '--detach',
                        str(checkout), revision], check=True, capture_output=True, text=True)
        try:
            yield checkout
        finally:
            subprocess.run(['git', '-C', str(REPO), 'worktree', 'remove', '--force', str(checkout)],
                           check=True, capture_output=True, text=True)


class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = original_checkout(os.environ.get('APPEND_ORACLE_BASE', 'f2459fd'))
        cls.base = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def run_case(self, repo, case, files):
        with tempfile.TemporaryDirectory(prefix='append-equality-') as directory:
            root = Path(directory)
            (root / 'events').mkdir()
            for name, rows in files.items():
                (root / 'events' / name).write_text(''.join(json.dumps(e, sort_keys=True) + '\n' for e in rows))
            env = dict(os.environ, DOIT_ROOT=str(root), DOIT_LEDGER_FILE=case.get('filename', 'L-operator-0001.jsonl'),
                       DOIT_PROJECT='', DOIT_READ_CACHE='0', V4_LEDGER_DIR=str(root / 'v4-ledger'),
                       V4_INBOX_DIR=str(root / 'v4-inbox'), DOIT_SESSIONS_DIR=str(root / 'sessions'),
                       HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'xdg'),
                       PYTHONPATH=os.pathsep.join((str(root), str(repo / 'src'))),
                       PATH='/usr/bin:' + os.environ.get('PATH', ''))
            # Pin imports too: both the real CLI __main__ and its dependencies
            # see the same deliberate clock. No candidate module is an oracle.
            bootstrap = '''import datetime as _dt
class Fixed(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, tzinfo=_dt.timezone.utc)
_dt.datetime = Fixed
'''
            (root / 'sitecustomize.py').write_text(bootstrap)
            run = subprocess.run(['/usr/bin/python3', '-B', '-c', bootstrap + ORACLE_DRIVER, str(repo / 'src')],
                                 input=json.dumps(case), capture_output=True, text=True, env=env,
                                 cwd=REPO, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            return json.loads(run.stdout)

    def compare(self, case, files=None):
        files = files or {}
        expected = self.run_case(self.base, case, files)
        actual = self.run_case(REPO, case, files)
        self.assertEqual(actual, expected, case)
        return actual

    def test_project_priority_and_parser(self):
        history = {'L-spec-writer-0001.jsonl': [row('observed', project='provisional'),
                    row('spec-written', project='registered')]}
        for configured, explicit in ((None, None), ('configured', None), ('configured', 'explicit')):
            args = ['observed', SID, 'sha=628758778891', 'n:=12', 'payload:={"x":[1,true]}', 'bad:={oops']
            if explicit:
                args += ['project=' + explicit]
            answer = self.compare(dict(argv=args, configured=configured), history)
            self.assertEqual(answer['result']['project'], explicit or configured or 'registered')
        self.assertEqual(self.compare(dict(argv=['observed', SID]),
            {'L-operator-seed.jsonl': [row(project='provisional')]})['result']['project'], 'provisional')
        self.assertEqual(self.compare(dict(argv=['observed', SID]))['result']['project'], REPO.name)

    def test_decisions_owed_lessons_and_refusals(self):
        escalation = row('escalation-blocking')
        escalation['subject'] = 'window-fixture'
        history = {'L-builder-0001.jsonl': [escalation], 'L-spec-writer-0001.jsonl': [row('owed-ac', criterion='AC1')],
                   'L-operator-seed.jsonl': [row('lesson', problem='history-gap', statement='shared history loses records')]}
        ruling = json.dumps(dict(kind='drop-from-window', spec=SID, window='fixture', reason='fixture'))
        cases = [['decision', SID, 'ref=L-builder-0001.jsonl:1', 'ruling:=' + ruling],
                 ['decision', SID, 'ref=L-builder-0001.jsonl:1'],
                 ['decision', SID, 'ref=unknown:1'], ['decision', SID, 'ref=L-operator-seed.jsonl:1'],
                 ['observed', SID, 'actor=operator'], ['observed', SID, '--inbox'],
                 ['lesson', SID, 'problem=history-gap'], ['lesson', SID, 'problem=history-gaps', 'statement=shared history loses records'],
                 ['lesson', SID, 'text=unassigned']]
        for args in cases:
            for strict in (False, True):
                with self.subTest(args=args, strict=strict):
                    self.compare(dict(argv=args, strict=strict), history)
        for filename in ('L-spec-writer-0001.jsonl', 'L-executor-0001.jsonl'):
            for criterion in ('AC1', 'AC2'):
                for strict in (False, True):
                    with self.subTest(filename=filename, criterion=criterion, strict=strict):
                        self.compare(dict(argv=['owed-ac', SID, 'criterion=' + criterion],
                                          filename=filename, strict=strict), history)
        receipt('AC3', {'oracle': 'fixed Git revision', 'records_bytes_diagnostics_fold': 'equal'})

    def test_cli_modes(self):
        for entry in ('cli', 'wrapper'):
            for flag in ('--quiet', '--json', '--board'):
                with self.subTest(entry=entry, flag=flag):
                    self.compare(dict(entry=entry, argv=['observed', SID, 'project=do-it-v2',
                        'ts=' + CLOCK.isoformat(timespec='seconds'), flag]))
        # The doit wrapper dispatches to this same CLI with its supplied Python.
        with tempfile.TemporaryDirectory(prefix='append-wrapper-') as directory:
            env = dict(os.environ, DOIT_ROOT=directory, DOIT_PROJECT='do-it-v2',
                       DOIT_LEDGER_FILE='L-operator-local.jsonl', PATH='/usr/bin:' + os.environ.get('PATH', ''))
            run = subprocess.run([str(REPO / 'doit'), 'append', 'observed', SID, '--json',
                                  'ts=' + CLOCK.isoformat(timespec='seconds')], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['subject'], SID)

    def test_mixed_clock_ordering(self):
        supplied = row()
        supplied['ts'] = (CLOCK - timedelta(seconds=2)).isoformat(timespec='seconds')
        answer = self.compare(dict(argv=['observed', SID]), {'L-operator-seed.jsonl': [supplied]})
        self.assertEqual([e['ts'] for e in answer['events']], [supplied['ts'], CLOCK.isoformat(timespec='seconds')])


def failure_fingerprint(output, name, repo, writes):
    section = output.split('· ' + name + '\n', 1)[1].split('\n· ', 1)[0]
    identities = re.findall(r'^(?:ERROR|FAIL): (.+)$', section, re.M)
    exceptions = re.findall(r'^(?:[\w.<>]*Error|[\w.<>]*Exception|[\w.<>]*Refused)(?:: .*)?$', section, re.M)
    frames = []
    for path, number in re.findall(r'  File "([^"]+)", line (\d+)', section):
        try:
            relative = Path(path).relative_to(repo).as_posix()
        except ValueError:
            continue
        if relative in writes:
            return None
        source = (repo / relative).read_text().splitlines()[int(number) - 1]
        # Earlier unrelated additions can shift a traceback's line number.
        # Both logs retain the numbers; match the source text.
        frames.append((relative, source))
    # Stdlib script checks have no unittest heading. Their matching exception
    # and untouched traceback source lines still establish the failure.
    return (identities, exceptions, frames) if exceptions and frames else None


class SuiteComparisonTests(unittest.TestCase):
    def test_bare_assertion_requires_source_evidence(self):
        with tempfile.TemporaryDirectory(prefix='append-bare-assertion-') as directory:
            root = Path(directory)
            (root / 'test_fixture.py').write_text('assert answer == expected\n')
            output = ('· test_fixture.py\nFAIL test_fixture.py (exit 1)\n'
                      f'  File "{root / "test_fixture.py"}", line 1, in <module>\n'
                      'AssertionError\n')
            self.assertIsNotNone(failure_fingerprint(output, 'test_fixture.py', root, set()))
            self.assertIsNone(failure_fingerprint(output, 'test_fixture.py', root, {'test_fixture.py'}))
            self.assertIsNone(failure_fingerprint(output.replace('AssertionError\n', ''),
                                                  'test_fixture.py', root, set()))

    def test_plain_assertion_base_red_requires_untouched_matching_source(self):
        with tempfile.TemporaryDirectory(prefix='append-fingerprint-') as directory:
            roots = [Path(directory) / label for label in ('base', 'candidate')]
            fingerprints = []
            for root in roots:
                root.mkdir()
                (root / 'test_fixture.py').write_text('assert answer == expected\n')
                output = ('· test_fixture.py\nFAIL test_fixture.py (exit 1)\n'
                          f'  File "{root / "test_fixture.py"}", line 1, in <module>\n'
                          'AssertionError: equal answers required\n')
                fingerprints.append(failure_fingerprint(output, 'test_fixture.py', root, set()))
                self.assertIsNone(failure_fingerprint(output, 'test_fixture.py', root, {'test_fixture.py'}))
            self.assertIsNotNone(fingerprints[0])
            self.assertEqual(*fingerprints)
            (roots[1] / 'test_fixture.py').write_text('assert different == expected\n')
            self.assertNotEqual(fingerprints[0], failure_fingerprint(output, 'test_fixture.py', roots[1], set()))


def suite_regression(revision):
    report = Path(tempfile.mkdtemp(prefix='append-suite-report-'))
    # The fixed historical revision remains the compatibility oracle. Suite
    # comparisons use this one-commit candidate's dispatch parent instead,
    # with the CURRENT tests on both sides (including the new regressions).
    suite_base = subprocess.check_output(
        ['git', '-C', str(REPO), 'rev-parse', 'HEAD^'], text=True).strip()
    current_tests = sorted(list((REPO / 'src').glob('test_*.py')) +
                           list((REPO / 'src').glob('test_*.mjs')))
    def run(repo, label):
        env = dict(os.environ, PATH='/usr/bin:' + os.environ.get('PATH', ''),
                   DOIT_ROOT=str(report / 'machine-root'), DOIT_PROJECT='',
                   V4_LEDGER_DIR=str(report / 'v4-ledger'), V4_INBOX_DIR=str(report / 'v4-inbox'))
        for name in list(env):
            if name.startswith('SUPABASE_DB_URL'):
                del env[name]
        path = report / (label + '.log')
        with path.open('w') as output:
            result = subprocess.run([str(repo / 'doit'), 'test'], cwd=repo, env=env,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=3600)
        text = path.read_text()
        # Test bodies also print PASS messages. Only a suite header followed by
        # that file's verdict identifies a completed suite check.
        rows = [(status, name) for name, status in re.findall(
            r'^· (test_[^\n]+)\n(PASS|FAIL) \1(?: \(exit \d+\))?$', text, re.M)]
        rows += re.findall(r'^(PASS|FAIL) (retired check|machine check)(?: \(exit \d+\))?$', text, re.M)
        identities = {name: status for status, name in rows}
        expected = {p.name for p in (repo / 'src').glob('test_*.py')} | {p.name for p in (repo / 'src').glob('test_*.mjs')} | {'retired check', 'machine check'}
        if set(identities) != expected or len(rows) != len(identities):
            raise AssertionError(f'{label}: missing/duplicate/unknown checks: {expected ^ set(identities)}; {path}')
        failures = sorted(name for name, status in identities.items() if status == 'FAIL')
        if bool(result.returncode) != bool(failures):
            raise AssertionError(f'{label}: undetermined suite exit {result.returncode}; {path}')
        print(json.dumps(dict(suite=label, exit_code=result.returncode, failures=failures, checks=identities, log=str(path))), flush=True)
        return identities, failures, text
    with original_checkout(suite_base) as base:
        for source in current_tests:
            shutil.copy2(source, base / 'src' / source.name)
        base_names = {p.name for p in (base / 'src').glob('test_*.py')} | {
            p.name for p in (base / 'src').glob('test_*.mjs')}
        if base_names != {p.name for p in current_tests}:
            raise AssertionError('current/base suite test identities differ')
        print(json.dumps(dict(oracle_revision=subprocess.check_output(
            ['git', '-C', str(REPO), 'rev-parse', revision], text=True).strip(),
            suite_base_revision=suite_base, current_suite_files={
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in current_tests})), flush=True)
        # Both unmodified suite commands get independent hermetic roots from
        # doit test. Running them together avoids serial timeout-test waits.
        with ThreadPoolExecutor(max_workers=2) as executor:
            baseline = executor.submit(run, base, 'base')
            candidate = executor.submit(run, REPO, 'candidate')
            base_checks, base_failures, base_output = baseline.result()
            candidate_checks, candidate_failures, candidate_output = candidate.result()
        writes = {'src/fold.py', 'src/merge_gate.py', 'src/test_append_reads.py',
                  'src/test_balance.py', 'src/test_packet.py', 'src/test_fold.py',
                  'src/test_resources.py'}

        matched = []
        for name in set(candidate_failures) & set(base_failures):
            expected = failure_fingerprint(base_output, name, base, writes)
            actual = failure_fingerprint(candidate_output, name, REPO, writes)
            if expected is not None and actual == expected:
                matched.append(name)
    new = sorted(set(candidate_failures) - set(base_failures))
    missing = sorted(set(base_checks) ^ set(candidate_checks))
    unmatched = sorted(set(candidate_failures) - set(matched))
    summary = dict(new_failures=new, missing=missing, base_failures=base_failures,
                   candidate_failures=candidate_failures, matched_base_red=sorted(matched),
                   unmatched_failures=unmatched, report=str(report))
    (report / 'summary.json').write_text(json.dumps(summary, sort_keys=True))
    print(json.dumps(summary), flush=True)
    # Baseline-only failures are reported, not attributed to the candidate.
    # Shared failures need matching identities, exceptions and untouched source
    # lines outside the write grant; names alone never establish base-red.
    return not new and not missing and not unmatched


if __name__ == '__main__':
    if '--suite-regression' in sys.argv:
        revision = sys.argv[sys.argv.index('--base') + 1]
        os.environ['APPEND_ORACLE_BASE'] = revision
        tests = unittest.defaultTestLoader.loadTestsFromTestCase(CompatibilityTests)
        passed = unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful()
        # The 2026-11-08 ruling assigns the full-suite comparison to the
        # evidence bundle. Builder Verification retains the fixed oracle;
        # the bundle can request the complete comparison with --full-suite.
        if passed and '--full-suite' in sys.argv:
            passed = suite_regression(revision)
        sys.exit(0 if passed else 1)
    unittest.main(verbosity=2)
