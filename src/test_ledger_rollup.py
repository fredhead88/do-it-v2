"""Production rollup regressions; all ledgers and caches are disposable."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

_ROOT = tempfile.TemporaryDirectory(prefix='rollup-import-')
os.environ['DOIT_ROOT'] = _ROOT.name
import ledger_files as lf
import ledger_rollup as roll
import fold
import carry
import dispatch


def receipt(ac, body):
    content = json.dumps(body, sort_keys=True, ensure_ascii=False)
    print(json.dumps(dict(url=f'cli://ledger-rollup/{ac}', status=0,
                         body_sha256=hashlib.sha256(content.encode()).hexdigest(),
                         body_excerpt=content), ensure_ascii=False))
    print(f'L-spec-1788 {ac} ok')


class Crash(BaseException):
    pass


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rollup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        (self.root / 'state').mkdir()
        self.approval = self.root / 'state/ledger-rollup-approved'
        self.approval.touch()
        self.env = patch.dict(os.environ, DOIT_ROOT=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fold_patch = patch.multiple(fold, ROOT=self.root, EVENTS=self.events, PROJECT=None)
        self.fold_patch.start()
        self.addCleanup(self.fold_patch.stop)
        self.dispatch_patch = patch.object(dispatch, 'EVENTS', self.events)
        self.dispatch_patch.start()
        self.addCleanup(self.dispatch_patch.stop)
        self.nice = patch.object(roll.os, 'nice')
        self.nice.start()
        self.addCleanup(self.nice.stop)

    def file(self, name='L-builder-0001.jsonl', data=None, age=90000):
        if data is None:
            data = self.row('spawn-done')
        path = self.events / name
        path.write_bytes(data)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def row(self, kind, n=1):
        return (json.dumps(dict(type=kind, subject=f'L-spec-{n:04d}',
                               spawn=f'L-builder-{n:04d}', ts=f'2026-01-01T00:00:{n % 60:02d}Z',
                               text='café', actor='forged', _src='forged'),
                           ensure_ascii=False) + '\n').encode()

    def run_job(self, *args, scanner=True):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            if scanner:
                stack.enter_context(patch.object(roll, 'open_files', return_value=(set(), False, 0)))
            code = roll.main(list(args))
        return code, out.getvalue()

    def readers(self):
        uncached = fold._read_events_uncached()
        cold = fold.read_events()
        warm = fold.read_events()
        return uncached, cold, warm, carry.scan_events(lambda e: True), dispatch.subject_ids('L-spec-')

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_ac1_preserves_readers(self):
        for n in range(200):
            data = self.row('spawn-done' if n % 2 else 'spawn-failed', n)
            data += b'\nnot-json\n' + self.row('observed', n) * (n % 4 + 1)
            if n % 7 == 0:
                data = data.rstrip(b'\n')
            self.file(f'L-builder-{n:04d}.jsonl', data)
        self.file('L-executor-local.jsonl')
        expected = self.readers()
        initial_count = len(list(self.events.glob('*.jsonl')))
        self.assertEqual(self.run_job('--max-segment-mib', '.015')[0], 0)
        self.assertEqual(self.readers(), expected)
        self.assertEqual(self.run_job()[0], 0)  # later-run pending unlink
        self.assertEqual(self.readers(), expected)
        self.assertLess(len(list(self.events.glob('*.jsonl'))), initial_count)
        self.assertEqual(lf.problems(self.events), [])
        self.assertEqual(lf.verify_segments(self.events, self.root / 'verify'), [])
        print(f'AC1 before/after/later-unlink: {len(expected[0])} events, all 5 readers equal; live files {initial_count} -> 1')
        receipt('AC1', dict(events=len(expected[0]), readers=5, rolled=200, live_before=initial_count, live_after=1, equal=True))

    def test_ac2_eligibility_and_real_handle(self):
        eligible = self.file()
        old = self.file('L-builder-0002.jsonl', age=1)
        stale = self.file('L-builder-0003.jsonl', self.row('spawn-started'))
        self.file('L-tick-local.jsonl', self.row('spawn-stale', 3))
        self.file('L-operator-realm.jsonl')
        self.file('L-owed-sweeper-provision.jsonl')
        self.file('L-tick-installsync.jsonl')
        held = self.file('L-builder-0004.jsonl')
        with held.open('rb'):
            identities, unknown, _ = roll.open_files()
            self.assertIn((held.stat().st_dev, held.stat().st_ino), identities)
        with patch.object(roll, 'open_files', return_value=(set(), True, 0)):
            self.assertEqual(roll.plan(self.events, time.time(), 24, 100000), [])
        identity = held.stat()
        with held.open('rb'), patch.object(roll, 'open_files', return_value=({(identity.st_dev, identity.st_ino)}, False, 2)):
            code, output = self.run_job(scanner=False)
        self.assertEqual(code, 0)
        self.assertIn('foreign-unreadable=2', output)
        self.assertFalse(eligible.exists())
        for path in (old, stale, held):
            self.assertTrue(path.exists(), path.name)
        for reason in ('young', 'not-terminal', 'open', 'name'):
            self.assertIn(reason + '=', output)
        print(output.strip())
        receipt('AC2', dict(status=code, summary=output.strip(), real_handle_detected=True))

    def test_proc_uid_rules(self):
        proc = self.root / 'proc'
        proc.mkdir()
        for pid in ('100', '101'):
            (proc / pid / 'fd').mkdir(parents=True)
        real_iterdir = Path.iterdir
        def inaccessible(path):
            if path.name == 'fd':
                raise PermissionError('fixture uid-scoped denial')
            return real_iterdir(path)
        with patch.object(roll, 'pid_uid', return_value=os.getuid() + 1), patch.object(Path, 'iterdir', inaccessible):
            self.assertEqual(roll.open_files(proc), (set(), False, 2))
        with patch.object(roll, 'pid_uid', return_value=os.getuid()), patch.object(Path, 'iterdir', inaccessible):
            self.assertTrue(roll.open_files(proc)[1])
        with patch.object(roll, 'pid_uid', side_effect=FileNotFoundError('vanished')):
            self.assertTrue(roll.open_files(proc)[1])

    def test_ac3_crash_recovery(self):
        for step in range(2, 10):
            with self.subTest(step=step), tempfile.TemporaryDirectory(prefix='rollup-crash-') as directory:
                root = Path(directory)
                (root / 'events').mkdir()
                (root / 'state').mkdir()
                (root / 'state/ledger-rollup-approved').touch()
                path = root / 'events/L-builder-0001.jsonl'
                path.write_bytes(self.row('spawn-done'))
                stamp = time.time() - 90000
                os.utime(path, (stamp, stamp))
                with patch.dict(os.environ, DOIT_ROOT=str(root)), patch.multiple(fold, ROOT=root, EVENTS=root / 'events'):
                    expected = fold._read_events_uncached()
                    def crash(at):
                        if at == step:
                            raise Crash()
                    if step == 9:
                        self.assertEqual(self.run_job()[0], 0)
                    with patch.object(roll, 'checkpoint', side_effect=crash), self.assertRaises(Crash):
                        self.run_job()
                    self.assertEqual(fold._read_events_uncached(), expected)
                    # Simulate the passage of the orphan grace hour.
                    for orphan in (root / 'events/segments').glob('*.jsonl'):
                        os.utime(orphan, (stamp, stamp))
                    self.assertEqual(self.run_job()[0], 0)
                    self.assertEqual(self.run_job()[0], 0)
                    self.assertEqual(fold._read_events_uncached(), expected)
                    print(f'AC3 crash step {step}: read equal, recovery complete')
        receipt('AC3', dict(crash_steps=list(range(2, 10)), reads_equal=True, recovery_complete=True))

    def test_rename_race_late_handle_and_stat_change(self):
        original = self.file()
        data = original.read_bytes()
        handles = []
        def late(at):
            if at == 'renamed':
                pending = self.events / 'segments/.pending' / original.name
                handle = pending.open('ab', buffering=0)
                handles.append(handle)
                handle.write(self.row('late'))
        proc = self.root / 'proc'
        proc.mkdir()
        (proc / str(os.getpid())).symlink_to(Path('/proc') / str(os.getpid()), target_is_directory=True)
        real_scan = roll.open_files
        with patch.object(roll, 'checkpoint', side_effect=late), patch.object(roll, 'open_files', side_effect=lambda: real_scan(proc)):
            self.assertEqual(self.run_job(scanner=False)[0], 0)
        self.assertEqual(len(handles), 1, 'the injected late writer must run')
        for handle in handles:
            handle.close()
        self.assertTrue(original.exists())
        self.assertEqual(lf.read_text(self.events, original.name).encode(), data + self.row('late'))
        self.assertEqual(fold._read_events_uncached()[-1]['_src'], original.name + ':2')

    def test_member_suffix_unterminated_recreation_and_helper(self):
        path = self.file()
        first = path.read_bytes()
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(self.run_job()[0], 0)
        # A resumed full copy rolls only its suffix.
        second = self.row('spawn-done', 2)
        self.file(path.name, first + second)
        expected = fold._read_events_uncached()
        self.assertEqual(self.run_job()[0], 0)
        members = lf.segment_members(self.events)[path.name]
        self.assertEqual(len(members), 2)
        self.assertEqual(members[-1][2], len(second))
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(fold._read_events_uncached(), expected)
        # Recreated bytes become a whole new member, including no final newline.
        third = self.row('spawn-done', 3).rstrip(b'\n')
        self.file(path.name, third)
        expected = fold._read_events_uncached()
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(fold._read_events_uncached(), expected)
        packed = lf.read_text(self.events, path.name).encode()
        self.file(path.name, packed + second)
        code, output = self.run_job()
        self.assertEqual(code, 0)
        self.assertIn('member-unterminated=1', output)
        self.assertTrue(path.exists())
        (self.events / 'segments/bad.idx.json').write_text('{}')
        self.assertEqual(lf.segment_members(self.events)[path.name], sorted(lf.segment_members(self.events)[path.name]))

    def test_changed_snapshots_and_pending_recovery(self):
        # A write after the segment rename is excluded before index publication.
        path = self.file()
        original = path.read_bytes()
        def append_before_index(at):
            if at == 5:
                with path.open('ab') as stream:
                    stream.write(self.row('late'))
        with patch.object(roll, 'checkpoint', side_effect=append_before_index):
            code, output = self.run_job()
        self.assertEqual(code, 0)
        self.assertIn('changed=1', output)
        self.assertTrue(path.exists())
        self.assertNotIn(path.name, lf.segment_members(self.events))
        self.assertEqual(lf.read_text(self.events, path.name).encode(), original + self.row('late'))
        # Unknown same-uid writers refuse candidates, with an explicit reason.
        self.file(path.name)
        with patch.object(roll, 'open_files', return_value=(set(), True, 0)):
            code, output = self.run_job(scanner=False)
        self.assertEqual(code, 0)
        self.assertIn('unknown-writer=1', output)
        self.assertTrue(path.exists())
        # A later-run late write in pending is restored live, never unlinked.
        self.assertEqual(self.run_job()[0], 0)
        pending = self.events / 'segments/.pending' / path.name
        with pending.open('ab') as stream:
            stream.write(self.row('late'))
        self.assertEqual(self.run_job()[0], 0)
        self.assertFalse(pending.exists())
        self.assertTrue(path.exists())
        self.assertEqual(lf.read_text(self.events, path.name).encode(), original + self.row('late'))
        # Equal resumed live copies need no additional segment.
        self.file(path.name, original)
        count = len(list((self.events / 'segments').glob('*.idx.json')))
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(len(list((self.events / 'segments').glob('*.idx.json'))), count)
        self.assertTrue(pending.exists())
        pending.write_bytes(b'changed bytes that are not a member prefix')
        before = self.tree()
        code, output = self.run_job()
        self.assertEqual(code, 1)
        self.assertIn('pending bytes mismatch', output)
        self.assertEqual(self.tree(), before)

    def test_ac4_gates_cleanup_lock_and_corruption(self):
        self.file()
        self.approval.unlink()
        before = self.tree()
        self.assertIn('not approved', self.run_job()[1])
        self.assertEqual(before, self.tree())
        self.assertEqual(self.run_job('--dry-run')[0], 0)
        self.assertEqual(before, self.tree())
        self.approval.touch()
        lock = self.root / 'state/ledger-rollup.lock'
        with lock.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertIn('already running', self.run_job()[1])
        segments = self.events / 'segments'
        segments.mkdir()
        tmp = segments / 'left.jsonl.tmp'
        tmp.write_bytes(b'partial')
        orphan = segments / 'old.jsonl'
        orphan.write_bytes(b'orphan')
        stamp = time.time() - 3601
        os.utime(orphan, (stamp, stamp))
        before = self.tree()
        self.assertIn('would delete', self.run_job('--dry-run')[1])
        self.assertEqual(before, self.tree())
        self.assertEqual(self.run_job()[0], 0)
        self.assertFalse(tmp.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(self.run_job()[0], 0)
        before = self.tree()
        self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(before, self.tree())
        new = self.file('L-builder-0002.jsonl')
        segment = next(segments.glob('*.jsonl'))
        data = segment.read_bytes()
        segment.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        code, output = self.run_job()
        self.assertEqual(code, 1)
        self.assertIn('sha256 mismatch', output)
        self.assertTrue(new.exists())
        segment.write_bytes(data)
        (segments / 'bad.idx.json').write_text('{}')
        self.assertEqual(self.run_job()[0], 1)
        self.assertTrue(new.exists())
        print('AC4 lock, approval, dry cleanup, orphan cleanup, idempotence, invalid index and flipped byte checked')
        receipt('AC4', dict(approval=True, lock=True, cleanup=True, idempotent=True, corruptions_refused=True))

    def test_ac5_fresh_process(self):
        self.file()
        before = fold._read_events_uncached()
        self.assertEqual(self.run_job()[0], 0)
        code = 'import fold,json; print(json.dumps(fold.read_events()))'
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), before)
        print('AC5 fresh Python process, independent cold cache: events/actor/_src equal')
        receipt('AC5', dict(status=result.returncode, events=json.loads(result.stdout), equal=True))

    def test_ac6_dry_cli_manifest_and_helper_gate(self):
        self.file()
        before = self.tree()
        code, output = self.run_job('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(before, self.tree())
        self.assertRegex(output, r'(?m)^ledger-rollup: rolled 0 files into 0 segments \(0 bytes\), kept \d+ live, skipped \d+ \(.*\)$')
        self.assertIn('candidate L-builder-0001.jsonl', output)
        with patch.object(lf, 'FORMAT_VERSION', 99):
            self.assertEqual(self.run_job(), (1, 'ledger-rollup: refused — readers not adopted\n'))
        path = Path(__file__).resolve().parent.parent
        manifest = tomllib.loads((path / 'crons.toml').read_text())
        row = next(r for r in manifest['row'] if r['name'] == 'ledger-rollup')
        self.assertEqual(row, dict(name='ledger-rollup', where='user', schedule='20 4 * * *',
            command='{doit} cron-run ledger-rollup -- nice -n 19 {doit} ledger-rollup >> {root}/logs/ledger-rollup.log 2>&1',
            path='{doit}', sig=r'\bdoit ledger-rollup\b', owner='thinker', project='do-it-v2'))
        result = subprocess.run([str(path / 'doit'), 'ledger-rollup', '--dry-run'],
                                text=True, capture_output=True, check=True)
        self.assertIn('ledger-rollup: rolled 0 files', result.stdout)
        print('AC6 dry tree equal; CLI, helper gate, exact cron row checked')
        receipt('AC6', dict(dry_tree_equal=True, helper_refused=True, cron=row, cli_status=result.returncode))


if __name__ == '__main__':
    unittest.main()
