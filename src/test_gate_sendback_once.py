#!/usr/bin/env python3
"""Gate receipt contracts against disposable Git repositories and real ledgers."""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
BOOT = tempfile.TemporaryDirectory(prefix="gate-receipt-import-")
# Workers receive their scratch scope before importing either production module.
if '--worker' not in sys.argv:
    os.environ['DOIT_ROOT'] = str(Path(BOOT.name) / 'ledger')
os.environ['DOIT_TEST_PROCESS'] = '1'
import fold
import merge_gate as mg


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gate-receipt-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'ledger'
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('GIT_', 'DOIT_GATE_', 'SUPABASE_DB_URL'))}
        env.update(DOIT_ROOT=str(self.root), DOIT_PROJECT='do-it-v2',
                   DOIT_SCRATCH=str(self.home / 'scratch'), DOIT_NO_POKE='1',
                   DOIT_GATE_LEDGER_FILE='L-executor-fixture.jsonl',
                   DOIT_LEDGER_FILE='L-executor-fixture.jsonl')
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        for key, value in dict(ROOT=self.root, EVENTS=self.root / 'events',
                               PROJECT='do-it-v2').items():
            self.stack.enter_context(patch.object(fold, key, value))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.stack.enter_context(patch.object(fold, 'NOW', self.now))
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        script = self.repo / mg.DETECT_SECRETS_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/bash\necho run >> "$COUNT_FILE"\n'
                          'if grep -qx FIND fixture; then\n'
                          'echo "  fixture:1 -- Synthetic finding"; exit 1\nfi\n')
        (self.repo / 'fixture').write_text('CLEAN\n')
        self.commit('base')
        self.main = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'work')
        (self.repo / 'fixture').write_text('FIND\n')
        self.commit('finding')
        self.sha = self.git('rev-parse', 'HEAD')
        os.environ['COUNT_FILE'] = str(self.home / 'scans')
        self.stack.enter_context(patch.object(mg, 'ENV', dict(os.environ)))
        self.old_cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.old_cwd)
        self.matrix = []

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '--allow-empty', '-qm', message)

    def seed(self, kind='review', subject='L-spec-fixture', actor='reviewer', **data):
        with patch.dict(os.environ, DOIT_LEDGER_FILE=f'L-{actor}-seed.jsonl'):
            return fold.append([kind, subject] + [f'{k}:={json.dumps(v)}' for k, v in data.items()])

    def review(self, **kw):
        with patch.object(fold, 'NOW', self.now - timedelta(seconds=10)):
            return self.seed(n_blocking=0, **kw)

    def call(self, subject='L-spec-fixture', branch='work', writes=('fixture.txt',)):
        args = [branch, 'main']
        if subject:
            content = self.root / 'content'
            content.mkdir(parents=True, exist_ok=True)
            (content / (subject + '.md')).write_text('Writes: fixture.txt\n')
            args += ['--spec', subject]
        if writes:
            args += ['--writes', ','.join(writes)]
        return mg.main_(args)

    def process(self, subject='L-spec-fixture', branch='work', ledger=None, start=False):
        # Use a valid unrelated grant: scanner findings remain independent of grants.
        if subject:
            content = self.root / 'content'
            content.mkdir(parents=True, exist_ok=True)
            (content / (subject + '.md')).write_text('Writes: fixture.txt\n')
        env = dict(os.environ)
        if ledger:
            env['DOIT_GATE_LEDGER_FILE'] = ledger
        args = [sys.executable, str(Path(__file__).resolve()), '--worker', branch, 'main']
        if subject:
            args += ['--spec', subject]
        args += ['--writes', 'fixture.txt']
        if start:
            p = subprocess.Popen(args, cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: (p.kill(), p.communicate()) if p.poll() is None else None)
            return p
        return subprocess.run(args, cwd=self.repo, env=env, capture_output=True, text=True)

    def rows(self, kind='merge-gate-rework'):
        return [e for e in fold.read_events(use_cache=False) if e['type'] == kind]

    def scans(self):
        p = self.home / 'scans'
        return len(p.read_text().splitlines()) if p.exists() else 0

    def report(self, **data):
        body = dict(case=self._testMethodName, matrix=self.matrix, **data)
        raw = json.dumps(body, sort_keys=True).encode()
        print(json.dumps(dict(url='cli://L-spec-2214/' + self._testMethodName, status=0,
                              body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))


class DedupTests(Fixture):
    def test_serial_checks_rerun(self):
        self.review()
        for _ in range(2):
            self.assertEqual(self.call(writes=('fixture.txt',)), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.scans(), 2)
        row = self.rows()[0]
        self.assertEqual((row['branch_sha'], row['check']), (self.sha, 'secret-scan'))
        self.report(receipts=1, checks=2, exits=[1, 1])

    def test_dimension_matrix_and_cycles(self):
        self.assertEqual(self.call(), 1)  # absent review: empty cycle
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['cycle'], '')
        self.matrix.append(['missing-review', 1])
        review = self.review()
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[-1]['cycle'], review['ts'])
        self.git('branch', 'alias', 'work')
        self.assertEqual(self.call(branch='alias'), 1)
        self.seed('merge-gate-clean', actor='executor', branch_sha=self.sha[:12])
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 2)
        self.matrix.append(['alias-clean-receipt', 2])
        # Main advances without changing branch identity or the selected review.
        self.git('checkout', 'main')
        (self.repo / 'other.txt').write_text('advance\n')
        self.commit('main advances')
        self.git('checkout', 'work')
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 2)
        self.matrix.append(['main-advance', 2])
        self.assertEqual(self.call(subject='L-spec-other'), 1)
        self.assertEqual(len(self.rows()), 3)
        self.matrix.append(['subject', 3])
        self.commit('same branch moves')
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 4)
        self.matrix.append(['commit', 4])
        with patch.object(fold, 'NOW', self.now - timedelta(seconds=5)):
            second = self.seed(n_blocking=0)
        self.assertEqual(self.call(), 1)
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.rows()[-1]['cycle'], second['ts'])
        self.matrix.append(['new-review', 5])
        # An equal-timestamp clean review uses the same existing timestamp cycle.
        with patch.object(fold, 'NOW', self.now - timedelta(seconds=5)):
            self.seed(n_blocking=0)
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.scans(), 11)
        self.report(scans=self.scans(), receipts=5, exits=[1] * 11)

    def test_full_identity_check_and_prose(self):
        self.review()
        a, b = 'a' * 40, 'a' * 12 + 'b' * 28
        for sha, removed, reverts, count in [
                (a, ['old'], [], 1), (a, ['changed prose'], [], 1),
                (b, ['old'], [], 2), (b, [], ['old'], 3),
                (b, ['old'], ['old'], 4)]:
            with patch.object(mg, 'gate', return_value=(removed, reverts, self.main, sha, sha)):
                self.assertEqual(self.call(), 1)
            self.assertEqual(len(self.rows()), count)
            self.matrix.append([sha == a, bool(removed), bool(reverts), count])
        self.assertEqual(self.scans(), 0)
        self.report(receipts=4, scans=0, exits=[1] * 5)

    def test_processes_and_released_lock(self):
        self.review()
        # Hold the receipt lock until both processes have completed their scans.
        key = hashlib.sha256(json.dumps([fold.PROJECT, 'L-spec-fixture'],
                                       separators=(',', ':')).encode()).hexdigest()
        directory = self.root / 'locks'
        directory.mkdir(parents=True)
        lock_path = directory / ('merge-gate-rework-' + key + '.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            processes = []
            for index in range(2):
                processes.append(self.process(start=True, ledger=f'L-executor-{index}.jsonl'))
                until = time.monotonic() + 3
                while self.scans() < index + 1 and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(self.scans(), index + 1)
                # The count is recorded inside the scanner. Wait for its lock to
                # release before launching the next, while receipts still overlap.
                scanlock = self.repo / '.git' / 'doit-secret-scan-locks' / (self.sha + '.lock')
                with scanlock.open('a+b') as scanner:
                    fcntl.flock(scanner, fcntl.LOCK_EX)
            fcntl.flock(lock, fcntl.LOCK_UN)
        for p in processes:
            out, err = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 1, out + err)
        self.assertEqual(len(self.rows()), 1)
        p = self.process()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.scans(), 3)
        self.assertTrue(lock_path.exists())
        self.report(receipts=1, checks=3, exits=[1, 1, 1], overlap=True)

    def test_coordination_failure_matrix(self):
        self.review()
        original_read, original_append, original_open = fold.read_events, fold.append, Path.open
        def read(*a, **kw):
            if kw.get('use_cache') is False:
                raise OSError('fresh read unavailable')
            return original_read(*a, **kw)
        def append(args, *a, **kw):
            if args[0] == 'merge-gate-rework':
                raise OSError('append unavailable')
            return original_append(args, *a, **kw)
        def opened(p, *a, **kw):
            if p.name.startswith('merge-gate-rework-'):
                raise OSError('lock open unavailable')
            return original_open(p, *a, **kw)
        for target, name, replacement in [(fold, 'read_events', read),
                                           (fold, 'append', append), (Path, 'open', opened)]:
            with patch.object(target, name, replacement):
                self.assertEqual(self.call(), 2)
            row = self.rows('merge-gate-undetermined')[-1]
            self.assertEqual((row['reason'], row['hold_reason'], row['retryable']),
                             ('rework-coordination', 'rework-coordination', False))
            self.matrix.append([name, 2])
        import resources
        with patch.object(resources, 'record'), \
             patch.object(fold, 'append', side_effect=OSError('all storage unavailable')):
            self.assertEqual(self.call(), 2)
        self.assertEqual(self.rows(), [])
        # All descriptors from the failed attempts were released.
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.report(exits=[2, 2, 2, 2, 1])

    def test_lock_bound(self):
        self.review()
        key = hashlib.sha256(json.dumps([fold.PROJECT, 'L-spec-fixture'],
                                       separators=(',', ':')).encode()).hexdigest()
        directory = self.root / 'locks'
        directory.mkdir(parents=True)
        path = directory / ('merge-gate-rework-' + key + '.lock')
        real_scan = mg.secret_scan
        entered = []
        def scanned(*args, **kw):
            result = real_scan(*args, **kw)
            entered.append(time.monotonic())
            return result
        with path.open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(mg, 'secret_scan', side_effect=scanned):
                self.assertEqual(self.call(), 2)
            self.assertEqual(len(entered), 1)
            elapsed = time.monotonic() - entered[0]
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 8)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows('merge-gate-undetermined')[-1]['reason'], 'rework-coordination')
        self.assertEqual(self.process().returncode, 1)
        self.report(held_seconds=round(elapsed, 2), exits=[2, 1])

    def test_new_review_reader_action(self):
        import nextstep as ns
        import lane_rules
        self.review()
        self.assertEqual(self.call(), 1)
        first = self.rows()[0]
        with patch.object(fold, 'NOW', self.now + timedelta(seconds=1)):
            review = self.seed(n_blocking=0)
        with patch.object(fold, 'NOW', self.now + timedelta(seconds=2)):
            self.assertEqual(self.call(), 1)
            self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 2)
        rows = fold.read_events(use_cache=False)
        selected = self.rows()[-1]
        self.assertGreaterEqual(selected['ts'], review['ts'])
        self.assertEqual(selected['branch_sha'], first['branch_sha'])
        c = dict(evs=rows, sid='L-spec-fixture', spec=dict(state='reviewing'),
                 dependency=None, capabilities={}, live=set(), now=self.now + timedelta(seconds=3))
        self.assertIsNone(ns._gate(c))
        rules = lane_rules.load()
        failure, classification = ns._failure(rows, rules)
        self.assertEqual(failure, selected)
        self.assertEqual(classification, 'unknown')
        steps = ns.next_steps(rows, {'L-spec-fixture': dict(state='reviewing')}, c['now'], rules)
        step = next(s for s in steps if s.item == 'L-spec-fixture')
        self.assertNotEqual(step.action, 'gate')
        self.assertIn('failure classification', step.reason)
        self.assertEqual(step.trigger, selected['_src'])
        self.report(receipts=2, classification=classification, public_step=step.__dict__, exits=[1, 1, 1])


class ToolingTests(Fixture):
    @contextlib.contextmanager
    def scanner_process(self, outcome):
        original = subprocess.Popen
        attempts = []
        class Process:
            pid = 99999999
            returncode = 1
            def communicate(proc, timeout=None):
                if outcome == 'timeout' and timeout is not None:
                    raise subprocess.TimeoutExpired('fixture scanner', timeout)
                return b'', b'fixture tool unavailable'
        def popen(argv, *a, **kw):
            if argv[0] == 'bash':
                attempts.append(argv)
                return Process()
            return original(argv, *a, **kw)
        with patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.object(mg.os, 'killpg') as kill:
            yield attempts, kill

    def test_tooling_matrix_and_prior_rework(self):
        self.review()
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 1)
        for reason in ['timeout', 'worktree', 'tool', 'lock-busy', 'deadline']:
            with contextlib.ExitStack() as stack:
                if reason in ('timeout', 'tool'):
                    attempts, killed = stack.enter_context(self.scanner_process(reason))
                elif reason == 'worktree':
                    original = mg.git
                    def git(*args, **kw):
                        if args[:2] == ('worktree', 'add'):
                            raise mg.Undetermined('worktree already exists')
                        return original(*args, **kw)
                    stack.enter_context(patch.object(mg, 'git', side_effect=git))
                elif reason == 'lock-busy':
                    scanlock = self.repo / '.git' / 'doit-secret-scan-locks' / (self.sha + '.lock')
                    lock = stack.enter_context(scanlock.open('a+b'))
                    fcntl.flock(lock, fcntl.LOCK_EX)
                else:
                    import resources
                    original = resources.record
                    def record(*a, **kw):
                        result = original(*a, **kw)
                        mg._START = time.monotonic() - 1000
                        return result
                    stack.enter_context(patch.object(resources, 'record', side_effect=record))
                before = len(self.rows('merge-gate-undetermined'))
                self.assertEqual(self.call(), 2)
                rows = self.rows('merge-gate-undetermined')
                self.assertEqual(len(rows), before + 1)
                row = rows[-1]
                self.assertEqual(row['reason'], reason)
                self.assertEqual(row['branch_sha'], self.sha)
                self.assertEqual(row['cycle'], self.review()['ts'])
                if reason == 'timeout':
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(killed.call_count, 2)
                if reason == 'lock-busy':
                    self.assertEqual(row['attempt'], '')
                self.assertEqual(len(self.rows()), 1)
                self.assertEqual(self.rows('merge-gate-clean'), [])
                self.matrix.append([reason, row['retryable'], row['hold_reason'], 2])
        self.report(receipts=1, matrix_checks=5)

    def test_metadata_policy_and_retry_caps(self):
        review = self.review()
        malformed = [('undetermined', ['timeout']), ('undetermined', ['deadline',
                     'budget=nan', 'scan-exhausted=false']), ('rework', ['worktree already exists']),
                     ('clean', ['extra']), ['clean', []]]
        for outcome in malformed:
            self.assertEqual(mg._scan_outcome(outcome), ('tool', False, 'scan-metadata'))
        self.assertEqual(mg._scan_outcome(('undetermined', ['timeout', 'budget=600.0',
                         'scan-exhausted=true'])), ('timeout', False, 'scan-cap-exhausted'))
        # Corrupt the transport metadata AFTER executing the production scanner.
        real_scan = mg.secret_scan
        def corrupted(*a, **kw):
            verdict, details = real_scan(*a, **kw)
            return verdict, tuple(details)
        with patch.object(mg, 'secret_scan', side_effect=corrupted):
            self.assertEqual(self.call(), 2)
        self.assertEqual(self.rows('merge-gate-undetermined')[-1]['hold_reason'], 'scan-metadata')
        for index in range(3):
            with self.scanner_process('tool'):
                self.assertEqual(self.call(), 2)
            row = self.rows('merge-gate-undetermined')[-1]
            self.assertEqual(row['cycle'], review['ts'])
            if index < 2:
                self.assertEqual(row['retry_at'], (self.now + timedelta(minutes=5 if index == 0 else 15)).isoformat())
            else:
                self.assertEqual((row['retryable'], row['hold_reason'], row['retry_at']),
                                 (False, 'retry-cap-exhausted', None))
        with patch.object(fold, 'NOW', self.now + timedelta(seconds=1)):
            self.seed('build-done', actor='builder', status='DONE')
        with self.scanner_process('tool'):
            self.assertEqual(self.call(), 2)
        self.assertEqual(self.rows('merge-gate-undetermined')[-1]['hold_reason'], 'review-required')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows('merge-gate-clean'), [])
        self.report(malformed=len(malformed), acquired_attempts=3, missing_review=True)

    def test_findings_distinct_commit_and_clean(self):
        self.assertEqual(self.call(), 1)
        self.commit('distinct commit')
        self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(self.rows()[0]['branch_sha'], self.rows()[1]['branch_sha'])
        (self.repo / 'fixture').write_text('CLEAN\n')
        self.commit('clean')
        self.assertEqual(self.call(), 0)
        self.assertEqual(len(self.rows('merge-gate-clean')), 1)
        self.report(exits=[1, 1, 0], reworks=2, clean=1)



class RoundTripTests(Fixture):
    def fresh_rows(self):
        code = "import fold,json; print(json.dumps(fold.read_events(use_cache=False)))"
        env = dict(os.environ, PYTHONPATH=str(SRC))
        done = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_real_store_independent_read_and_legacy_prefix(self):
        self.review()
        self.seed('merge-gate-rework', actor='executor', branch_sha=self.sha[:12], reason='secret-scan')
        path = self.root / 'events' / 'L-executor-fixture.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = (json.dumps(dict(v=1, ts=self.now.isoformat(), type='merge-gate-rework',
                   subject='L-spec-fixture', project=fold.PROJECT, branch_sha=self.sha[:12],
                   reason='secret-scan')) + '\n').encode()
        path.write_bytes(legacy)
        for subject in ['L-spec-fixture', 'L-spec-other']:
            if subject.endswith('other'):
                self.commit('different subject commit')
            sha = self.git('rev-parse', 'HEAD')
            first = self.process(subject=subject)
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            rows = [e for e in self.fresh_rows() if e['subject'] == subject
                    and e['type'] == 'merge-gate-rework' and e.get('check') == 'secret-scan']
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row['branch_sha'], sha)
            self.assertEqual(row['reason'], 'secret-scan')
            self.assertEqual(row['findings'], ['  fixture:1 -- Synthetic finding'])
            self.assertEqual(row['branch'], 'work')
            self.assertEqual(row['cycle'], self.review()['ts'] if subject.endswith('fixture') else '')
            self.assertEqual(self.process(subject=subject).returncode, 1)
            repeated = [e for e in self.fresh_rows() if e['subject'] == subject
                        and e['type'] == 'merge-gate-rework' and e.get('check') == 'secret-scan']
            self.assertEqual(len(repeated), 1)
            self.matrix.append([subject, True, 1, [1, 1]])
        self.assertTrue(path.read_bytes().startswith(legacy))
        self.report(historical_prefix_unchanged=True, subjects=2)

    def test_source_authorization(self):
        review = self.review()
        # Missing provenance cannot be created by the production reader; exercise
        # that defensive case separately while keeping actual store reads intact.
        candidate = dict(type='merge-gate-rework', subject='L-spec-fixture',
                         branch_sha=self.sha, check='secret-scan', cycle=review['ts'],
                         project=fold.PROJECT, ts=self.now.isoformat())
        for actor in ['executor', 'tick', 'builder', 'forged-builder', 'missing']:
            subject = 'L-spec-' + actor
            self.review(subject=subject)
            if actor == 'missing':
                real = fold.read_events
                def read(*a, **kw):
                    return real(*a, **kw) + [dict(candidate, subject=subject)]
                with patch.object(fold, 'read_events', side_effect=read):
                    self.assertEqual(self.call(subject=subject), 1)
            else:
                source_actor = 'builder' if actor == 'forged-builder' else actor
                path = self.root / 'events' / f'L-{source_actor}-source.jsonl'
                row = dict(candidate, subject=subject)
                if actor == 'forged-builder':
                    row.update(actor='executor', role='executor')
                with path.open('a') as stream:
                    stream.write(json.dumps(row) + '\n')
                self.assertEqual(self.call(subject=subject), 1)
            written = [r for r in self.rows() if r['subject'] == subject
                       and r['_src'].startswith('L-executor-fixture.jsonl:')]
            count = 0 if actor in ('executor', 'tick') else 1
            self.assertEqual(len(written), count)
            self.matrix.append([actor, count])
        self.report(authorized=['executor', 'tick'])

    def test_project_scope_and_legacy_cycle(self):
        review = self.review()
        self.seed('merge-gate-rework', actor='executor', branch_sha=self.sha,
                  check='secret-scan', cycle=review['ts'], project='other-project')
        self.seed('merge-gate-rework', actor='executor', branch_sha=self.sha,
                  check='secret-scan')
        self.assertEqual(self.call(), 1)
        rows = [r for r in self.rows() if r.get('cycle') == review['ts']]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['project'], fold.PROJECT)
        self.report(other_project_and_cycleless_cannot_suppress=True)



class CompatibilityTests(Fixture):
    def test_origins_and_unresolved_identity(self):
        self.review()
        for check, removed, reverts in [('removed', ['old.txt'], []),
                ('reverted', [], ['old.txt']), ('removed+reverted', ['old.txt'], ['old.txt'])]:
            for _ in range(2):
                with patch.object(mg, 'gate', return_value=(removed, reverts, self.main, self.sha, self.sha)), \
                     patch.object(mg, 'secret_scan', side_effect=AssertionError('guard scanned')):
                    self.assertEqual(self.call(), 1)
            rows = [e for e in self.rows() if e.get('check') == check]
            self.assertEqual(len(rows), 1)
            if removed:
                self.assertEqual(rows[0]['removed'], removed)
            if reverts:
                self.assertEqual(rows[0]['reverted'], reverts)
            self.matrix.append([check, 1, [1, 1]])
        for _ in range(2):
            with patch.object(mg, 'secret_scan', side_effect=AssertionError('guard scanned')):
                self.assertEqual(self.call(writes=('English',)), 1)
        self.assertEqual(len([r for r in self.rows() if r['check'] == 'grant']), 1)
        self.matrix.append(['grant', 1, [1, 1]])
        for _ in range(2):
            with patch.object(mg, 'secret_scan', side_effect=AssertionError('guard scanned')):
                self.assertEqual(self.call(branch='nonexistent', writes=('English',)), 1)
        unresolved = [r for r in self.rows() if r['branch_sha'] == '']
        self.assertEqual(len(unresolved), 2)
        self.assertTrue(all(r['check'] == 'grant' for r in unresolved))
        self.assertEqual(self.call(), 1)
        self.assertEqual(self.call(), 1)
        self.assertEqual(len([r for r in self.rows() if r['check'] == 'secret-scan']), 1)
        self.matrix.append(['secret-scan', 1, [1, 1]])
        self.commit('new finding')
        self.assertEqual(self.call(), 1)
        self.assertEqual(len([r for r in self.rows() if r['check'] == 'secret-scan']), 2)
        self.report(unresolved=2, distinct_commit=True)

    def test_conflict_routes_and_preverdict_refusals(self):
        import conflict
        import branch_refresh
        # Create a real three-way conflict from the fixture's original base.
        self.git('checkout', 'main')
        (self.repo / 'fixture').write_text('MAIN CONFLICT\n')
        self.commit('main conflict')
        self.git('checkout', 'work')
        for _ in range(2):
            self.assertEqual(self.call(subject=None), 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['check'] == 'conflict-diagnostic' and r['branch_sha'] == self.sha for r in rows))
        # Production diagnosis and receipt, without branch recutting in the fixture.
        with patch.object(branch_refresh, 'gate_refresh', return_value=None):
            self.assertEqual(self.call(), 1)
        self.assertEqual(len(self.rows('conflict-rework')), 1)
        with patch.object(branch_refresh, 'gate_refresh', return_value=2):
            self.assertEqual(self.call(), 2)
        with patch.object(conflict, 'conflicted', side_effect=mg.Undetermined('diagnosis unavailable')):
            self.assertEqual(self.call(), 2)
        self.assertEqual(self.rows('merge-gate-undetermined')[-1]['reason'], 'conflict-diagnostic')
        before = len(fold.read_events(use_cache=False))
        for args in [['--help'], ['work', '--bad-option']]:
            with self.assertRaises(SystemExit):
                mg.main_(args)
        with patch.object(mg.freeze, 'state', return_value={'text': 'fixture freeze'}), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='albert-scott', stderr='')):
            with self.assertRaises(SystemExit):
                mg.main_(['work', 'main'])
        self.assertEqual(len(fold.read_events(use_cache=False)), before)
        self.report(specless_conflict_receipts=2, spec_conflict=1, diagnosis_hold=2, preverdict=True)

    def test_entry_points(self):
        import branch_refresh
        import merge_queue
        self.review()
        subject = 'L-spec-fixture'
        self.git('branch', subject.lower(), 'work')
        self.call()  # Also writes the scratch spec consumed by subprocesses.
        content = self.root / 'content' / (subject + '.md')
        content.write_text('Writes: fixture.txt\n')
        for spec in [True, False]:
            args = [str(SRC.parent / 'doit'), 'gate', 'work', 'main']
            if spec:
                args += ['--spec', subject]
            for _ in range(2):
                done = subprocess.run(args, cwd=self.repo, capture_output=True, text=True)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for _ in range(2):
            self.assertEqual(branch_refresh.regate(self.repo, 'work', 'main', subject), 1)
        self.seed('handin-checked', actor='builder', sha=self.sha, checks=['secret-scan'], result='clean')
        written = dict(type='spec-written', subject=subject, project=fold.PROJECT,
                       ts=(self.now - timedelta(minutes=10)).isoformat(), footprint=['fixture.txt'])
        row = dict(rule='gate', spec=subject, since=self.review()['ts'], role='gate')
        def runner(argv, **kw):
            self.assertEqual(argv[:2], ['doit', 'gate'])
            done = subprocess.run([str(SRC.parent / 'doit')] + argv[1:], capture_output=True, text=True, **kw)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            return dict(code=done.returncode, stdout=done.stdout, stderr=done.stderr)
        for _ in range(2):
            with patch.object(merge_queue, '_reconcile', return_value=[]), \
                 patch.object(merge_queue.lane_rules, 'due', return_value=[row]), \
                 patch.object(merge_queue.lane_rules, 'ladder', return_value='act'), \
                 patch.object(merge_queue, '_repository', return_value=(self.repo, 'main')):
                result = merge_queue.enqueue_due([written], self.now, runner=runner)
            self.assertTrue(any('gate failed' in value for value in result), result)
        for who in [subject, 'work']:
            self.assertEqual(len([r for r in self.rows() if r['subject'] == who]), 1)
        self.report(routes=['imported main_', 'doit gate --spec', 'doit gate', 'branch_refresh.regate',
                            'merge_queue.enqueue_due'], receipt_counts=[1, 1], finding_after_handin=True)

    def test_unchanged_gate_suite(self):
        env = dict(os.environ)
        # Its legacy-store fixture deliberately includes project-less history.
        # Run its own isolation contract without this suite's project filter.
        env.pop('DOIT_PROJECT', None)
        done = subprocess.run([sys.executable, str(SRC / 'test_merge_gate.py')],
                              cwd=SRC.parent, env=env, capture_output=True, text=True)
        # Print the immutable suite's failures verbatim; never edit expectations.
        print(done.stdout)
        print(done.stderr, file=sys.stderr)
        self.assertEqual(done.returncode, 0)
        self.report(unchanged_gate_suite=True)



if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        sys.exit(mg.main_(sys.argv[2:]))
    names = sys.argv[1:] or ['DedupTests', 'ToolingTests', 'RoundTripTests', 'CompatibilityTests']
    allowed = {c.__name__: c for c in (DedupTests, ToolingTests, RoundTripTests, CompatibilityTests)}
    if any(n not in allowed for n in names):
        sys.exit('unknown selector')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(allowed[n]) for n in names)
    if not suite.countTestCases():
        sys.exit('empty selector')
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
