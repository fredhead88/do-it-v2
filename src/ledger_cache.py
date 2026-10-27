"""Disposable cross-process parsed JSONL cache, stored as small SQLite chunks.

Only raw events are saved; fold owns ordering, corrections and project filters.
Transactions publish whole reads atomically under flock. Checksummed, data-only
pickles avoid JSON reparsing without allowing executable cache content. Cache
corruption falls back to fold's reference parser; lock failures propagate.
"""
from contextlib import contextmanager
from typing import NamedTuple
import fcntl
import hashlib
import io
import json
import math
import os
import pathlib
import pickle
import sqlite3
import sys
import threading
import time

import ledger_compact

VERSION = 3
CHUNK_EVENTS = 256
PREFIX_CHUNK = 8 * 1024 * 1024
# Callers may patch the shared time module for their own timeout tests.
_monotonic = time.monotonic


class Invalid(ValueError):
    pass


class LockTimeout(RuntimeError):
    pass


class ReentrantRead(LockTimeout):
    pass


# Include pid and thread: neither a fork nor a concurrent thread is recursion.
_HELD = set()


def lock_wait_s():
    try:
        value = float(os.environ.get('DOIT_LEDGER_LOCK_WAIT_S', '120'))
        return value if math.isfinite(value) and value >= 0 else 120.0
    except ValueError:
        return 120.0


@contextmanager
def _locked(lock, mode):
    claim = (str(lock), os.getpid(), threading.get_ident())
    if claim in _HELD:
        raise ReentrantRead(f'ledger cache: reentrant read ({lock})')
    try:
        fh = lock.open('rb')
    except FileNotFoundError:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fh = os.fdopen(fd, 'rb')
    with fh:
        wait = lock_wait_s()
        deadline = _monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    raise LockTimeout(f'ledger cache: lock not acquired within {wait:g}s ({lock})')
                time.sleep(min(.05, remaining))
        _HELD.add(claim)
        try:
            yield
        finally:
            _HELD.discard(claim)
            fcntl.flock(fh, fcntl.LOCK_UN)


class _DataOnly(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError('cache may contain data only')


def _pack(value):
    data = pickle.dumps(value, protocol=5)
    return data, hashlib.sha256(data).digest()


def _unpack(data, digest):
    if not isinstance(data, bytes) or hashlib.sha256(data).digest() != digest:
        raise Invalid('cache checksum')
    try:
        value = _DataOnly(io.BytesIO(data)).load()
        if not isinstance(value, list) or any(not isinstance(e, dict) for e in value):
            raise Invalid('cache schema')
        # Pickle shares strings within a chunk, but not across chunks. Reuse
        # JSON keys and repeated labels without changing order or values.
        labels = {'type', 'subject', 'project', 'role', 'actor'}
        for n, event in enumerate(value):
            value[n] = {sys.intern(k) if isinstance(k, str) else k:
                        sys.intern(v) if k in labels and isinstance(v, str) else v
                        for k, v in event.items()}
        return value
    except (EOFError, ValueError, TypeError, pickle.UnpicklingError) as exc:
        raise Invalid('cache payload') from exc


def _record_digest(row):
    return hashlib.sha256(pickle.dumps(row[:10] + (row[11],), protocol=5)).digest()


def _meta(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _keys(obj):
    return {sys.intern(k): v for k, v in obj.items()}


def _parse(data, name, actor, first_line, *, with_count=False):
    result, next_line = [], first_line
    # Match read_text().splitlines(), including CRLF and Unicode separators,
    # without retaining a second ledger-sized list of line strings.
    physical_lines = io.TextIOWrapper(io.BytesIO(data), encoding='utf-8', newline=None)
    lines = (line for physical in physical_lines for line in physical.splitlines())
    for n, line in enumerate(lines, first_line):
        next_line = n + 1
        if not line.strip():
            continue
        try:
            event = ledger_compact.compact(json.loads(line, object_hook=_keys))
        except json.JSONDecodeError:
            continue
        event['actor'], event['_src'] = actor, f'{name}:{n}'
        result.append(event)
    return (result, next_line) if with_count else result


def _file(path, actor, old):
    st = path.stat()
    meta = _meta(st)
    append = False
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        if old is not None and meta[:2] == old['meta'][:2] and st.st_size > old['meta'][2]:
            remaining = old['meta'][2]
            while remaining:
                chunk = fh.read(min(PREFIX_CHUNK, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            append = remaining == 0 and digest.digest() == old['digest']
        # Verify every old byte, including an unterminated tail, before using
        # the suffix. A rewrite-and-grow must take the full reparse path.
        start, first_line = (old['offset'], old['line']) if append else (0, 1)
        fh.seek(start)
        data = fh.read(st.st_size - start)
        if append:
            digest.update(memoryview(data)[old['meta'][2] - start:])
        else:
            digest = hashlib.sha256(data)
        if _meta(os.fstat(fh.fileno())) != meta or _meta(path.stat()) != meta:
            raise OSError('ledger changed during cache read')
    # Verify the ENTIRE old prefix: a historical repair may rewrite AND grow
    # a file in place. Offset/line exclude any unterminated final line, even
    # if that line happened to parse as valid JSON on the previous read.
    end = data.rfind(b'\n') + 1
    complete = data[:end]
    events, line = _parse(complete, path.name, actor, first_line, with_count=True)
    return dict(meta=meta, digest=digest.digest(), offset=start + end, line=line,
                events=events, tail=_parse(data[end:], path.name, actor, line), append=append)


def _connect(path, *, writable=False):
    # mode=ro refuses absent files and cannot create a journal or run DDL.
    db = (sqlite3.connect(path, timeout=5) if writable else
          sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
    try:
        # Keep SQLite's page heap bounded. Rollback journal + FULL synchronous
        # protects commits; an interrupted build leaves a valid older cache.
        db.execute('PRAGMA cache_size=-2048')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != VERSION and not (writable and version == 0):
            raise Invalid('cache version')
        if not writable:
            return db
        db.execute('CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, '
                   'dev INTEGER, ino INTEGER, size INTEGER, mtime INTEGER, ctime INTEGER, '
                   'offset INTEGER, line INTEGER, digest BLOB, chunks INTEGER, tail BLOB, tail_sha BLOB, record_sha BLOB)')
        db.execute('CREATE TABLE IF NOT EXISTS chunks (name TEXT, seq INTEGER, data BLOB, sha BLOB, '
                   'PRIMARY KEY (name, seq))')
        if version == 0:
            db.execute(f'PRAGMA user_version={VERSION}')
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def _metadata(db):
    rows = db.execute('SELECT * FROM files').fetchall()
    if any(len(row) != 13 or _record_digest(row) != row[12] for row in rows):
        raise Invalid('cache metadata checksum')
    old = {r[0]: dict(meta=tuple(r[1:6]), offset=r[6], line=r[7], digest=r[8],
                       chunks=r[9], tail=r[10], tail_sha=r[11]) for r in rows}
    return old


def _generation(db, directory):
    """Return the metadata validated by this scan, never a later fingerprint."""
    old = _metadata(db)
    recorded = {name: row['meta'] for name, row in old.items()}
    current = {file.name: _meta(file.stat()) for file in directory.glob('*.jsonl')}
    return tuple(sorted(recorded.items())) if recorded == current else None


def _current(db, directory):
    return _generation(db, directory) is not None


def _update(db, directory, actor_of):
    old = _metadata(db)
    seen = set()
    with db:  # All metadata, prefix chunks and torn tails publish together.
        for file in sorted(directory.glob('*.jsonl')):
            name = file.name
            seen.add(name)
            previous = old.get(name)
            if previous is None or previous['meta'] != _meta(file.stat()):
                current = _file(file, actor_of(name), previous)
                if not current['append']:
                    db.execute('DELETE FROM chunks WHERE name=?', (name,))
                seq = previous['chunks'] if current['append'] else 0
                for start in range(0, len(current['events']), CHUNK_EVENTS):
                    data, digest = _pack(current['events'][start:start + CHUNK_EVENTS])
                    db.execute('INSERT INTO chunks VALUES (?,?,?,?)', (name, seq, data, digest))
                    seq += 1
                tail, digest = _pack(current['tail'])
                record = (name, *current['meta'], current['offset'], current['line'],
                          current['digest'], seq, tail, digest)
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (*record, _record_digest(record)))
                del current
        for name in old.keys() - seen:
            db.execute('DELETE FROM chunks WHERE name=?', (name,))
            db.execute('DELETE FROM files WHERE name=?', (name,))


def _read(db):
    out = []
    for name, entry in sorted(_metadata(db).items()):
        count = 0
        for seq, data, digest in db.execute(
                'SELECT seq,data,sha FROM chunks WHERE name=? ORDER BY seq', (name,)):
            if seq != count:
                raise Invalid('cache chunk sequence')
            out.extend(_unpack(data, digest))
            count += 1
        if count != entry['chunks']:
            raise Invalid('cache missing chunks')
        out.extend(_unpack(entry['tail'], entry['tail_sha']))
    return out


def _refresh(path, directory, actor_of):
    # Called only under LOCK_EX. Re-check after waiting: another writer may
    # already have published exactly the state this reader needs.
    for attempt in range(2):
        db = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            db = _connect(path, writable=True)
            if not _current(db, directory):
                _update(db, directory, actor_of)
            return
        except (sqlite3.DatabaseError, Invalid) as exc:
            if attempt:
                raise OSError('ledger cache unavailable') from exc
            if db is not None:
                db.close()
                db = None
            path.unlink(missing_ok=True)
            pathlib.Path(str(path) + '-journal').unlink(missing_ok=True)
        finally:
            if db is not None:
                db.close()


class ConditionalRead(NamedTuple):
    generation: tuple
    events: list | None  # None means unchanged; [] is a newly loaded empty ledger.


def read(events_dir, cache_dir, actor_of):
    return read_conditional(events_dir, cache_dir, actor_of).events


def read_conditional(events_dir, cache_dir, actor_of, *, previous=None):
    directory, cache_dir = pathlib.Path(events_dir).resolve(), pathlib.Path(cache_dir).resolve()
    ident = hashlib.sha256(os.fsencode(directory.resolve())).hexdigest()[:16]
    path, lock = cache_dir / f'ledger-{ident}.sqlite3', cache_dir / f'ledger-{ident}.lock'
    claim = (str(lock), os.getpid(), threading.get_ident())
    if claim in _HELD:
        raise ReentrantRead(f'ledger cache: reentrant read ({lock})')
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # No lock conversion: drop SH before requesting EX, and drop EX before
    # unpacking. Each acquisition has its own bounded wait.
    for attempt in range(3):
        corrupt = False
        broken_meta = None
        with _locked(lock, fcntl.LOCK_SH):
            db = None
            try:
                db = _connect(path)
                validated = _generation(db, directory)
                if validated is not None:
                    generation = (str(directory), str(cache_dir), validated)
                    if generation == previous:
                        return ConditionalRead(generation, None)
                    return ConditionalRead(generation, _read(db))
            except (sqlite3.DatabaseError, Invalid):
                corrupt = True
                if path.exists():
                    broken_meta = _meta(path.stat())
            finally:
                if db is not None:
                    db.close()
        if attempt == 2:
            raise OSError('ledger cache unavailable')
        with _locked(lock, fcntl.LOCK_EX):
            # A damaged payload can have current metadata. Validate on the
            # shared read; if it failed, rebuild rather than trust that cache.
            db = None
            needs_refresh = True
            try:
                db = _connect(path)
                current = _current(db, directory)
                if current and corrupt and broken_meta == _meta(path.stat()):
                    # Only needed after a corrupt shared read, never on a
                    # healthy stale path. Do not unpack under exclusive lock.
                    db.close()
                    db = None
                    path.unlink(missing_ok=True)
                    pathlib.Path(str(path) + '-journal').unlink(missing_ok=True)
                elif current:
                    needs_refresh = False
            except (sqlite3.DatabaseError, Invalid):
                pass
            finally:
                if db is not None:
                    db.close()
            if needs_refresh:
                _refresh(path, directory, actor_of)
