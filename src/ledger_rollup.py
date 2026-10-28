"""Pack finished per-spawn files; publication uses the FORMAT_VERSION 1 reader rule.

Cron installation follows shipping automatically. The command remains inert until
an Executor creates state/ledger-rollup-approved after the external-script review,
pre-image backup and live dry run. No ledger lock or fold read is used here.
"""
import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

try:
    import ledger_files
except ImportError:
    ledger_files = None

SPAWN = re.compile(r'^L-[a-z0-9-]+-[0-9]+\.jsonl$')


@dataclass
class Candidate:
    path: Path
    data: bytes
    stat: os.stat_result
    original_sha: str


def checkpoint(step):
    """Named crash boundaries, replaceable by scratch-only regression tests."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pid_uid(pid):
    return pid.stat().st_uid


def open_files(proc=Path('/proc')):
    """Return open (device,inode) identities, same-uid uncertainty, foreign count."""
    opened, unknown, foreign = set(), False, 0
    for pid in proc.iterdir():
        if not pid.name.isdigit():
            continue
        try:
            uid = pid_uid(pid)
        except OSError:
            unknown = True
            continue
        try:
            descriptors = list((pid / 'fd').iterdir())
        except OSError:
            if uid == os.getuid():
                unknown = True
            else:
                foreign += 1
            continue
        if uid != os.getuid():
            continue
        for descriptor in descriptors:
            try:
                stat = descriptor.stat()
                opened.add((stat.st_dev, stat.st_ino))
            except FileNotFoundError:
                # An fd closed mid-scan, including our own listdir fd. A vanished
                # same-uid process is checked independently below and is unknown.
                pass
            except OSError:
                unknown = True
        try:
            pid.stat()
        except OSError:
            unknown = True
    return opened, unknown, foreign


def unchanged(path, stat):
    try:
        now = path.stat()
        return (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) == (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return False


def blocked(stat, scan):
    opened, unknown, _ = scan
    return 'unknown-writer' if unknown else 'open' if (stat.st_dev, stat.st_ino) in opened else None


def member_bytes(events_dir):
    """Only indexed segment slices, never the live duplicate or continuation."""
    result = {}
    for name, slices in ledger_files.segment_members(events_dir).items():
        parts, last_nl, raw = bytearray(), True, []
        for segment, offset, length in slices:
            with (Path(events_dir) / ledger_files.SEGMENT_DIR / segment).open('rb') as stream:
                stream.seek(offset)
                data = stream.read(length)
            if len(data) != length:
                raise OSError('segment changed during rollup')
            if parts and not last_nl:
                parts.extend(b'\n')
            parts.extend(data)
            raw.append(data)
            last_nl = not data or data.endswith(b'\n')
        result[name] = (bytes(parts), last_nl, raw)
    return result


def candidates(events_dir, now, min_age_hours, members=None):
    """Read each live candidate once, retain bytes and the pre-read stat."""
    members = member_bytes(events_dir) if members is None else members
    selected, reasons = [], Counter()
    scan = open_files()
    if scan[2]:
        reasons['foreign-unreadable'] = scan[2]
    paths = sorted(Path(events_dir).glob('*.jsonl'))
    for path in paths:
        reason = None
        if not SPAWN.fullmatch(path.name):
            reason = 'name'
        else:
            try:
                stat = path.stat()
                if path.is_symlink() or not path.is_file():
                    reason = 'not-regular'
                elif now - stat.st_mtime <= min_age_hours * 3600:
                    reason = 'young'
                elif blocked(stat, scan):
                    reason = blocked(stat, scan)
                else:
                    data = path.read_bytes()
                    terminal = False
                    for line in data.splitlines():
                        try:
                            event = json.loads(line)
                            terminal |= isinstance(event, dict) and event.get('type') in ('spawn-done', 'spawn-failed')
                        except (ValueError, UnicodeError):
                            pass
                    if not terminal:
                        reason = 'not-terminal'
                    elif not unchanged(path, stat) or len(data) != stat.st_size:
                        reason = 'changed'
                    else:
                        payload = data
                        if path.name in members:
                            packed, last_nl, _ = members[path.name]
                            if data.startswith(packed):
                                if data != packed and not last_nl:
                                    reason = 'member-unterminated'
                                else:
                                    payload = data[len(packed):]
                        if not reason:
                            selected.append(Candidate(path, payload, stat, digest(data)))
            except OSError:
                reason = 'unreadable'
        if reason:
            reasons[reason] += 1
    return selected, reasons, len(paths)


def batches(selected, max_bytes):
    result, batch, size = [], [], 0
    for candidate in selected:
        if not candidate.data:  # equal resumed copy: just rename it aside
            continue
        if batch and size + len(candidate.data) > max_bytes:
            result.append(batch)
            batch, size = [], 0
        batch.append(candidate)
        size += len(candidate.data)
    if batch:
        result.append(batch)
    return result


def plan(events_dir, now, min_age_hours, max_bytes):
    selected, _, _ = candidates(events_dir, now, min_age_hours)
    return [[c.path.name for c in batch] for batch in batches(selected, max_bytes)]


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_sync(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def next_segment(directory, date):
    # Fixed-width sequences keep segment-name order equal to publication order.
    # Refuse exhaustion instead of wrapping and reordering continuations.
    used = [int(match.group(1)) for path in directory.iterdir()
            if (match := re.match(rf'^{re.escape(date)}-([0-9]{{2}})\.', path.name))]
    for k in range(max(used, default=0) + 1, 100):
        stem = f'{date}-{k:02d}'
        if not any((directory / (stem + suffix)).exists() for suffix in (
                '.jsonl', '.idx.json', '.jsonl.tmp', '.idx.json.tmp')):
            return stem
    raise OSError('daily segment sequence exhausted (99); retry on the next UTC day')


def set_aside(candidate, directory, reasons):
    if not unchanged(candidate.path, candidate.stat):
        reasons['changed'] += 1
        return False
    scan = open_files()
    reason = blocked(candidate.stat, scan)
    if reason:
        reasons[reason] += 1
        return False
    pending = directory / '.pending'
    pending.mkdir(exist_ok=True)
    target = pending / candidate.path.name
    if target.exists():
        raise OSError(f'pending collision: {target.name}')
    os.rename(candidate.path, target)
    checkpoint('renamed')
    reason = blocked(candidate.stat, open_files())
    if not unchanged(target, candidate.stat) or reason:
        if candidate.path.exists():
            raise OSError(f'late writer and recreated live name: {target.name}; pending retained')
        os.rename(target, candidate.path)
        reasons[reason or 'changed'] += 1
        return False
    checkpoint(7)
    sync_directory(candidate.path.parent)
    sync_directory(pending)
    checkpoint(8)
    return True


def publish(batch, directory, date, reasons):
    stem = next_segment(directory, date)
    segment = directory / (stem + '.jsonl')
    index = directory / (stem + '.idx.json')
    segment_tmp, index_tmp = Path(str(segment) + '.tmp'), Path(str(index) + '.tmp')
    rows, offset, whole_hash = [], 0, hashlib.sha256()
    # No concatenated second copy of a segment in memory.
    with segment_tmp.open('xb') as stream:
        for candidate in batch:
            data = candidate.data
            rows.append(dict(name=candidate.path.name, offset=offset, length=len(data),
                             lines=len(data.decode('utf-8', errors='replace').splitlines()),
                             sha256=digest(data), ends_nl=data.endswith(b'\n')))
            stream.write(data)
            whole_hash.update(data)
            offset += len(data)
        stream.flush()
        os.fsync(stream.fileno())
    checkpoint(2)
    obj = dict(version=1, segment=segment.name, bytes=offset,
               sha256=whole_hash.hexdigest(), members=rows)
    write_sync(index_tmp, json.dumps(obj, sort_keys=True).encode())
    checkpoint(3)
    with segment_tmp.open('rb') as stream:
        total = hashlib.sha256()
        for row in rows:
            data = stream.read(row['length'])
            if (len(data) != row['length'] or digest(data) != row['sha256'] or
                    len(data.decode('utf-8', errors='replace').splitlines()) != row['lines']):
                raise OSError('temporary segment member verification failed')
            total.update(data)
        if stream.read(1) or total.hexdigest() != obj['sha256']:
            raise OSError('temporary segment verification failed')
    checkpoint(4)
    os.rename(segment_tmp, segment)
    checkpoint(5)
    scan = open_files()
    valid = []
    for candidate, row in zip(batch, rows):
        reason = blocked(candidate.stat, scan)
        if not unchanged(candidate.path, candidate.stat):
            reason = 'changed'
        if reason:
            reasons[reason] += 1
        else:
            valid.append((candidate, row))
    obj['members'] = [row for _, row in valid]
    # Still private: rewrite the temp index after the last pre-publication check.
    with index_tmp.open('wb') as stream:
        stream.write(json.dumps(obj, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(index_tmp, index)
    sync_directory(directory)
    checkpoint(6)
    rolled = sum(set_aside(candidate, directory, reasons) for candidate, _ in valid)
    return rolled, offset


def leftovers(events_dir, now):
    directory = Path(events_dir) / ledger_files.SEGMENT_DIR
    result = sorted(p for p in directory.glob('*.tmp') if p.is_file())
    # problems() defines precisely which final-named orphans belong to this job.
    result.extend(Path(path) for path, reason in ledger_files.problems(events_dir)
                  if reason == 'orphan segment older than one hour')
    return result


def pending_cleanup(events_dir, members):
    pending = Path(events_dir) / ledger_files.SEGMENT_DIR / '.pending'
    for path in sorted(pending.glob('*.jsonl')):
        if path.name not in members:
            continue  # the health gate already refuses unindexed pending files
        stat = path.stat()
        if blocked(stat, open_files()):
            raise OSError(f'pending writer unknown or open: {path.name}')
        data = path.read_bytes()
        packed, _, slices = members[path.name]
        expected = [packed, *slices]
        equal = any(len(data) == len(value) and digest(data) == digest(value) for value in expected)
        if not unchanged(path, stat):
            raise OSError(f'pending changed: {path.name}')
        if equal:
            path.unlink()
            sync_directory(pending)
            checkpoint(9)
        elif any(data.startswith(value) and len(data) > len(value) for value in expected):
            live = Path(events_dir) / path.name
            if live.exists():
                raise OSError(f'pending late write and live name exists: {path.name}')
            os.rename(path, live)
            sync_directory(pending)
            sync_directory(events_dir)
        else:
            raise OSError(f'pending bytes mismatch: {path.name}')


def summary(files=0, segments=0, size=0, live=0, reasons=None):
    reasons = reasons or Counter()
    skipped = sum(n for reason, n in reasons.items() if reason != 'foreign-unreadable')
    explanation = ', '.join(f'{key}={value}' for key, value in sorted(reasons.items())) or 'none'
    print(f'ledger-rollup: rolled {files} files into {segments} segments ({size} bytes), '
          f'kept {live} live, skipped {skipped} ({explanation})')


def run(root, args):
    events_dir, state = root / 'events', root / 'state'
    now = time.time()
    cleanup = leftovers(events_dir, now)
    if args.dry_run:
        for path in cleanup:
            print(f'ledger-rollup: would delete {path}')
    else:
        for path in cleanup:
            path.unlink()
        if cleanup:
            sync_directory(events_dir / ledger_files.SEGMENT_DIR)
    issues = ledger_files.problems(events_dir)
    if args.dry_run:
        issues = [(path, reason) for path, reason in issues if Path(path) not in cleanup]
    # Fresh disposable verification state detects corruption even when an old
    # index-mtime cache says the segment was good; dry-run leaves no cache behind.
    with tempfile.TemporaryDirectory(prefix='rollup-verify-') as verification:
        issues += ledger_files.verify_segments(events_dir, Path(verification))
    if issues:
        print('ledger-rollup: refused — ' + '; '.join(f'{path}: {reason}' for path, reason in issues[:5]))
        return 1
    members = member_bytes(events_dir)
    if not args.dry_run:
        pending_cleanup(events_dir, members)
    selected, reasons, count = candidates(events_dir, now, args.min_age_hours, members)
    groups = batches(selected, int(args.max_segment_mib * 1024 * 1024))
    if args.dry_run:
        for candidate in selected:
            print(f'ledger-rollup: candidate {candidate.path.name} ({len(candidate.data)} bytes)')
        for number, group in enumerate(groups, 1):
            print(f'ledger-rollup: planned segment {number}: ' + ', '.join(c.path.name for c in group))
        summary(live=count, reasons=reasons)
        return 0
    if not selected:
        summary(live=count, reasons=reasons)
        return 0
    directory = events_dir / ledger_files.SEGMENT_DIR
    directory.mkdir(exist_ok=True)
    sync_directory(events_dir)
    date = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    rolled, size = 0, 0
    for candidate in selected:
        if not candidate.data:
            rolled += set_aside(candidate, directory, reasons)
    for group in groups:
        files, written = publish(group, directory, date, reasons)
        rolled += files
        size += written
    summary(rolled, len(groups), size, count - rolled, reasons)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-segment-mib', type=float, default=64)
    parser.add_argument('--min-age-hours', type=float, default=24)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.max_segment_mib) or args.max_segment_mib <= 0 or
            not math.isfinite(args.min_age_hours) or args.min_age_hours < 0):
        print('ledger-rollup: refused — invalid size or age')
        return 1
    try:
        os.nice(10)
        if (ledger_files is None or getattr(ledger_files, 'FORMAT_VERSION', None) != 1 or
                not Path(__file__).with_name('test_ledger_readers.py').is_file()):
            print('ledger-rollup: refused — readers not adopted')
            return 1
        root = Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
        if not args.dry_run and not (root / 'state/ledger-rollup-approved').is_file():
            print('ledger-rollup: not approved (state/ledger-rollup-approved absent)')
            return 0
        if args.dry_run:
            # Do not create a lock or state directory in a read-only preview.
            return run(root, args)
        state = root / 'state'
        with (state / 'ledger-rollup.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('ledger-rollup: already running')
                return 0
            return run(root, args)
    except (OSError, ValueError) as exc:
        print(f'ledger-rollup: failed — {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
