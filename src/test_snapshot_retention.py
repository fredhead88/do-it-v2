#!/usr/bin/env python3
"""Snapshot regression fixtures: all writes stay in a TemporaryDirectory."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='snapshot-retention-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'source'
        self.dst = self.root / 'backup'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.dst.mkdir()
        self.write('events/ledger', 'event')
        self.script = self.root / 'new-snapshot.sh'
        shutil.copy2(REPO / 'scripts/snapshot.sh', self.script)
        self.clock = dt.datetime(2026, 10, 1, tzinfo=UTC)
        self.env = {**os.environ, 'SNAPSHOT_SRC': str(self.src),
                    'SNAPSHOT_DST': str(self.dst), 'SNAPSHOT_TEST': '1',
                    'SNAPSHOT_TEST_FREE_BYTES': str(100 * 2**30),
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        for name in ('SNAPSHOT_NOW', 'SNAPSHOT_TEST_PAUSE_FILE'):
            self.env.pop(name, None)

    def write(self, path, body='data', base=None):
        target = (base or self.src) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def run_snapshot(self, rc=0):
        result = subprocess.run([str(self.script)], env={**self.env,
                                'SNAPSHOT_NOW': self.clock.isoformat()},
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, rc, result.stderr + self.log())
        self.assertEqual(result.stdout, '')
        if rc == 0:
            self.assertEqual(result.stderr, '')
        else:
            self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        return result

    def log(self):
        p = self.dst / 'snapshot.log'
        return p.read_text()[-2000:] if p.exists() else ''

    def stub(self, tool, body):
        p = self.bin / tool
        p.write_text('#!/bin/bash\n' + body + '\n')
        p.chmod(0o755)
        return p

    def snapshots(self):
        return sorted(p.name for p in (self.dst / 'snap').iterdir()
                      if len(p.name) == 13 and p.is_dir())

    def legacy(self):
        return self.dst / 'mirror'

    def test_ac1_exact_deletion_history_and_filters(self):
        self.stub('rsync', 'printf "%s\\n" "$@" >> "$SNAPSHOT_DST/rsync.args"\n'
                  'exec /usr/bin/rsync "$@"')
        for name in ('keep', 'gone', 'tmp-x.sh', 'content/probe-x/finding.md',
                     'burndown/reports/a.md', 'burndown/brief.md'):
            self.write(name, 'old')
        for name in ('build-tmp/a', 'tmp-01/a', 'tmp/a', 'cache/a', '.git/a',
                     'nested/scratch/a', 'burndown/junk.txt', 'burndown/wt/bad.md'):
            self.write(name)
        self.run_snapshot()
        current = self.dst / 'snap/current'
        files = sorted(str(p.relative_to(current)) for p in current.rglob('*') if p.is_file())
        self.assertEqual(files, sorted(['events/ledger', 'keep', 'gone', 'tmp-x.sh',
                         'content/probe-x/finding.md', 'burndown/reports/a.md',
                         'burndown/brief.md']))
        old = self.dst / 'snap/2026-10-01T00'
        self.assertEqual((current / 'keep').stat().st_ino, (old / 'keep').stat().st_ino)
        self.write('keep', 'a much newer value')
        (self.src / 'gone').unlink()
        self.clock += dt.timedelta(minutes=10)
        self.run_snapshot()
        self.assertFalse((current / 'gone').exists())
        self.assertEqual((old / 'gone').read_text(), 'old')
        self.assertEqual((old / 'keep').read_text(), 'old')
        self.assertEqual((current / 'keep').read_text(), 'a much newer value')
        self.assertTrue((current / 'burndown/brief.md').exists())
        shutil.rmtree(self.src / 'burndown')
        self.run_snapshot()
        self.assertFalse((current / 'burndown').exists())
        self.assertNotIn('--inplace', (self.dst / 'rsync.args').read_text())
        self.assertNotIn('--inplace', self.script.read_text())

    def test_ac2_ten_days_retention_and_calendar_gaps(self):
        first = self.clock
        self.write('daily-file')
        self.write('hourly-file')
        history = []
        for hour in range(10 * 24):
            self.clock = first + dt.timedelta(hours=hour)
            if self.clock.day == 3 or (self.clock.day == 4 and self.clock.hour < 3):
                continue
            # Seed intervening freezes cheaply; exercise the real script at
            # midnight, either side of the 24-hour boundary, and after the gap.
            checkpoint = self.clock.hour in (0, 1, 23) or hour == 75
            if checkpoint:
                self.run_snapshot()
            else:
                shutil.copytree(self.src, self.dst / 'snap' / self.clock.strftime('%Y-%m-%dT%H'),
                                copy_function=os.link)
            history.append(self.clock)
            if hour == 0:
                (self.src / 'daily-file').unlink()
                self.run_snapshot()
                self.assertEqual(len(self.snapshots()), 1)
            if hour == 1:
                (self.src / 'hourly-file').unlink()
            if hour == 7 * 24 + 23:
                self.assertTrue((self.dst / 'snap/2026-10-01T00/daily-file').exists())
            if hour == 8 * 24:
                self.assertFalse((self.dst / 'snap/2026-10-01T00').exists())
            if hour == 25:
                self.assertTrue((self.dst / 'snap/2026-10-01T01/hourly-file').exists())
            if not checkpoint:
                continue
            cutoff = self.clock - dt.timedelta(hours=24)
            daily = {}
            for stamp in history:
                daily.setdefault(stamp.date(), stamp)
            keep = {t for t in history if t >= cutoff}
            keep.update(t for day, t in daily.items()
                        if 0 <= (self.clock.date() - day).days <= 7)
            keep.update(history[-2:])
            self.assertEqual(self.snapshots(), sorted(t.strftime('%Y-%m-%dT%H') for t in keep))
        self.assertNotIn('2026-10-03T00', self.snapshots())
        self.assertTrue((self.dst / 'snap/2026-10-04T03').is_dir())

    def test_ac2_clock_future_parts_and_names(self):
        self.run_snapshot()
        snap = self.dst / 'snap'
        (snap / '2026-10-01T01.part').mkdir()
        (snap / '2026-09-29T00.part').mkdir()
        (snap / 'notes').mkdir()
        (snap / '2026-11-10T03').mkdir()
        self.clock = dt.datetime(2026, 11, 10, tzinfo=UTC)
        self.run_snapshot()
        self.assertEqual(self.snapshots(), ['2026-10-01T00', '2026-11-10T00', '2026-11-10T03'])
        self.assertEqual(os.readlink(snap / 'latest'), '2026-11-10T00')
        self.assertTrue((snap / 'notes').exists())
        self.assertFalse(list(snap.glob('*.part')))
        self.assertIn('future', self.log())

    def test_ac3_rescue_tarballs_retire_and_expire(self):
        for name in ('private/old', 'content/other/a', 'build-tmp/no',
                     'tmp-0001/no', 'content/probe-x/no', 'private/.name.ab12CD'):
            self.write(name, base=self.legacy())
        self.write('events/ledger', 'older', self.legacy())
        link = self.legacy() / 'private/link'
        link.symlink_to('missing')
        (self.src / 'dangling').symlink_to('missing')
        self.write('dangling', base=self.legacy())
        for name, contents in [('daily-covered.tar.gz', ['private/old', 'build-tmp/no']),
                               ('daily-missing.tar.gz', ['absent'])]:
            with tarfile.open(self.dst / name, 'w:gz') as archive:
                for path in contents:
                    fixture = self.write(path, base=self.root / 'archive/mirror')
                    archive.add(fixture, arcname='mirror/' + path)
        self.write('daily-unfinished.tar.gz.part', base=self.dst)
        self.stub('rsync', 'test ! -e "$SNAPSHOT_DST/daily-covered.tar.gz" || exit 91\n'
                  'exec /usr/bin/rsync "$@"')
        self.run_snapshot()
        rescue = self.dst / 'rescued/legacy-2026-10-01'
        legacy = self.dst / 'mirror.legacy'
        self.assertFalse(self.legacy().exists())
        self.assertEqual((rescue / 'private/old').stat().st_ino,
                         (legacy / 'private/old').stat().st_ino)
        self.assertTrue((rescue / 'private/link').is_symlink())
        self.assertEqual(os.readlink(rescue / 'private/link'), 'missing')
        for path in ('build-tmp', 'tmp-0001', 'content/probe-x', 'private/.name.ab12CD', 'dangling'):
            self.assertFalse((rescue / path).exists())
        self.assertTrue((self.dst / 'daily-missing.tar.gz').exists())
        self.assertFalse((self.dst / 'daily-unfinished.tar.gz.part').exists())
        for day in range(1, 6):
            self.clock += dt.timedelta(days=1)
            self.run_snapshot()
            self.assertTrue(legacy.exists())
        (rescue / 'private/old').unlink()
        self.clock += dt.timedelta(days=1)
        self.run_snapshot()
        self.assertTrue(legacy.exists())
        self.assertTrue((rescue / 'private/old').exists())
        self.run_snapshot()
        self.assertFalse(legacy.exists())
        self.assertFalse((self.dst / 'daily-missing.tar.gz').exists())
        self.clock = dt.datetime(2026, 10, 31, tzinfo=UTC)
        self.run_snapshot()
        self.assertTrue(rescue.exists())
        self.clock += dt.timedelta(days=1)
        self.run_snapshot()
        self.assertFalse((self.dst / 'rescued').exists())

    def test_ac3_rescue_retry_failure_and_late_writer(self):
        self.write('one', base=self.legacy())
        self.write('two', base=self.legacy())
        self.stub('ln', 'if [[ "$1" == -P && "$3" == */two ]]; then exit 8; fi\n'
                  'exec /usr/bin/ln "$@"')
        result = self.run_snapshot(4)
        self.assertIn('rescue failed', result.stderr)
        self.assertTrue((self.dst / 'snap/2026-10-01T00').exists())
        self.assertFalse((self.dst / '.rescue-done').exists())
        self.assertTrue((self.dst / 'rescued/legacy-2026-10-01/one').exists())
        (self.bin / 'ln').unlink()
        self.clock += dt.timedelta(days=1)
        self.run_snapshot()
        self.assertTrue((self.dst / 'rescued/legacy-2026-10-01/two').exists())
        self.assertFalse((self.dst / 'rescued/legacy-2026-10-02').exists())
        self.write('late', base=self.legacy())
        self.run_snapshot()
        self.assertFalse(self.legacy().exists())
        self.assertTrue(list((self.dst / 'rescued').glob('legacy-late-*/late')))
        self.assertFalse(list(self.dst.glob('mirror.legacy.late-*')))
        # A repeated missing-link verification must alarm, and never retire.
        for day in range(4):
            self.clock += dt.timedelta(days=1)
            self.run_snapshot()
        self.clock += dt.timedelta(days=1)
        rescue = self.dst / 'rescued/legacy-2026-10-01/one'
        rescue.unlink()
        self.run_snapshot()
        rescue.unlink()
        self.assertIn('rescue verification', self.run_snapshot(4).stderr)
        self.assertTrue((self.dst / 'mirror.legacy').exists())

    def test_ac3_first_build_partial_transfer(self):
        self.stub('rsync', '/usr/bin/rsync "$@"\nexit 23')
        self.assertIn('rsync failed rc=23', self.run_snapshot(4).stderr)
        self.assertTrue((self.dst / 'snap/2026-10-01T00/events/ledger').exists())

    def test_ac3_missing_space_and_step_errors(self):
        shutil.rmtree(self.src / 'events')
        before = list(self.dst.iterdir())
        self.assertIn('source events/ missing', self.run_snapshot(7).stderr)
        self.assertEqual(list(self.dst.iterdir()), before)
        self.write('events/ledger')
        self.env['SNAPSHOT_TEST_FREE_BYTES'] = str(3 * 2**30)
        self.assertIn('insufficient space', self.run_snapshot(4).stderr)
        self.assertFalse((self.dst / 'snap/2026-10-01T00').exists())
        self.write('snap/current/already', 'x', self.dst)
        self.write('snap/.current-building', '', self.dst)
        self.stub('rsync', 'if [[ "$*" == *--stats* ]]; then '
                  'echo "Total file size: 2,147,483,648 bytes"; exit 0; fi\n'
                  'exec /usr/bin/rsync "$@"')
        self.stub('du', 'echo "2147483648 fixture"')
        self.env['SNAPSHOT_TEST_FREE_BYTES'] = str(5 * 2**30)
        self.run_snapshot()
        (self.bin / 'du').unlink()
        (self.bin / 'rsync').unlink()
        self.env['SNAPSHOT_TEST_FREE_BYTES'] = '100'
        self.clock += dt.timedelta(days=9)
        for day in range(2, 5):
            (self.dst / f'snap/2026-10-0{day}T00').mkdir()
        self.assertIn('low space (100 bytes free)', self.run_snapshot(4).stderr)
        self.assertFalse((self.dst / 'snap/2026-10-02T00').exists())
        self.assertFalse((self.dst / 'snap/2026-10-10T00').exists())
        self.env['SNAPSHOT_TEST_FREE_BYTES'] = str(100 * 2**30)
        self.stub('rsync', '/usr/bin/rsync "$@"\nexit 23')
        self.assertIn('rsync failed rc=23', self.run_snapshot(4).stderr)
        self.assertTrue((self.dst / 'snap/2026-10-10T00').exists())
        self.stub('rsync', '/usr/bin/rsync "$@"\nexit 24')
        self.clock += dt.timedelta(hours=1)
        self.run_snapshot()
        self.stub('rsync', 'exit 9')
        self.clock += dt.timedelta(hours=1)
        self.assertIn('rsync failed rc=9', self.run_snapshot(4).stderr)
        self.assertFalse((self.dst / 'snap/2026-10-10T02').exists())
        (self.bin / 'rsync').unlink()
        self.stub('cp', 'mkdir -p "${@: -1}"\nexit 11')
        self.assertIn('cp failed rc=11', self.run_snapshot(4).stderr)
        self.assertFalse((self.dst / 'snap/2026-10-10T02').exists())
        self.assertTrue((self.dst / 'snap/2026-10-10T02.part').exists())
        (self.bin / 'cp').unlink()
        self.run_snapshot()
        self.assertFalse((self.dst / 'snap/2026-10-10T02.part').exists())
        self.write('snapshot.log', 'x' * (11 * 1024**2), self.dst)
        self.run_snapshot()
        self.assertLessEqual((self.dst / 'snapshot.log').stat().st_size, 1024**2 + 1000)
        self.stub('rsync', '/usr/bin/rsync "$@"\nhead -c 11534336 /dev/zero')
        self.run_snapshot()
        self.assertLessEqual((self.dst / 'snapshot.log').stat().st_size, 1024**2 + 1000)

    def holder(self, birth=None, age=3601):
        pid = os.getpid()
        ticks = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        stamp = (self.clock - dt.timedelta(seconds=age)).isoformat()
        self.write('snapshot.running', f'{pid} {birth or ticks} {stamp}\n', self.dst)

    def test_ac3_lock_stuck_orphan_and_race(self):
        with (self.dst / 'snapshot.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_snapshot()
            self.holder()
            self.assertIn('previous run stuck', self.run_snapshot(6).stderr)
            self.write('snap/.current-building', '', self.dst)
            self.run_snapshot()
            self.holder(age=14401)
            self.run_snapshot(6)
            (self.dst / 'snap/.current-building').unlink()
            self.holder(birth='0')
            self.assertIn('lock orphaned', self.run_snapshot(6).stderr)
            # The 2-second reread must recognize the replacement holder.
            self.stub('sleep', f'printf "%s\\n" "{os.getpid()} 0 {self.clock.isoformat()}" '
                      '> "$SNAPSHOT_DST/snapshot.running"')
            self.run_snapshot()
            (self.bin / 'sleep').unlink()
        self.write('snapshot.running', '999999999 0 2000-01-01T00:00:00Z', self.dst)
        self.run_snapshot()
        self.assertFalse((self.dst / 'snapshot.running').exists())

    def test_ac3_old_run_pause_and_parsed_body(self):
        old = self.write('snapshot.sh', '#!/bin/bash\nsleep 30\n', self.dst)
        process = subprocess.Popen(['bash', str(old)])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.write('valuable', base=self.legacy())
        try:
            time.sleep(.1)
            self.run_snapshot()
            self.assertTrue(self.legacy().exists())
            self.assertFalse((self.dst / '.cutover-renamed').exists())
        finally:
            process.terminate()
            process.wait()
        self.run_snapshot()
        pause = self.write('pause', '', self.root)
        self.env['SNAPSHOT_TEST_PAUSE_FILE'] = str(pause)
        self.clock += dt.timedelta(hours=1)
        process = subprocess.Popen([str(self.script)], env={**self.env,
                                   'SNAPSHOT_NOW': self.clock.isoformat()},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.dst / 'snapshot.running').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.run_snapshot()
            self.script.write_text('#!/bin/bash\nexit 99\n')
            pause.unlink()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((process.returncode, stdout, stderr), (0, b'', b''))
            self.assertTrue((self.dst / 'snap/2026-10-01T01').exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_ac3_hooks_require_test_flag(self):
        self.env.pop('SNAPSHOT_TEST')
        self.env['SNAPSHOT_TEST_FREE_BYTES'] = '0'
        pause = self.write('pause', '', self.root)
        self.env['SNAPSHOT_TEST_PAUSE_FILE'] = str(pause)
        # Exercise the ordinary df/date paths without depending on host space
        # or crossing a real hour boundary while the subprocess runs.
        self.stub('df', 'printf "Avail\\n107374182400\\n"')
        actual = dt.datetime(2030, 1, 2, 3, tzinfo=UTC)
        self.stub('date', 'if [[ "$*" == "-u +%s" ]]; then echo '
                  f'{int(actual.timestamp())}; else exec /usr/bin/date "$@"; fi')
        self.run_snapshot()
        self.assertEqual(self.snapshots(), [actual.strftime('%Y-%m-%dT%H')])
        self.assertTrue(pause.exists())

    def test_ac4_executable(self):
        self.assertTrue(os.access(REPO / 'scripts/snapshot.sh', os.X_OK))
        mode = subprocess.check_output(['git', 'ls-files', '--stage', 'scripts/snapshot.sh'],
                                       cwd=REPO, text=True)
        self.assertTrue(mode.startswith('100755 '), mode)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SnapshotTests)
    group = sys.argv[sys.argv.index('-k') + 1].lower() if '-k' in sys.argv else None
    if group:
        suite = unittest.TestSuite(t for t in suite if group in t.id().lower())
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        if group is None:
            print('L-spec-1708 AC2 ok')
        for number in range(1, 5):
            if group is None or group == f'ac{number}':
                body = f'snapshot-retention AC{number} status=0'
                print(json.dumps({'url': f'cli://snapshot-retention/AC{number}',
                                  'status': 0,
                                  'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                                  'body_excerpt': body}, sort_keys=True))
                print(body)
    sys.exit(0 if result.wasSuccessful() else 1)
