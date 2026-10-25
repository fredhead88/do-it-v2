"""Disposable cross-process parsed JSONL cache, stored as small SQLite chunks.

Only raw events are saved; fold owns ordering, corrections and project filters.
Transactions publish whole reads atomically under flock. Checksummed, data-only
pickles avoid JSON reparsing without allowing executable cache content. Any
cache failure falls back to fold's independent reference parser.
"""
import fcntl
import hashlib
import io
import json
import os
import pathlib
import pickle
import sqlite3
import sys

import ledger_compact

VERSION = 3
CHUNK_EVENTS = 256
PREFIX_CHUNK = 8 * 1024 * 1024


class Invalid(ValueError):
    pass


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


def _connect(path):
    db = sqlite3.connect(path, timeout=5)
    try:
        # Keep SQLite's page heap bounded. Rollback journal + FULL synchronous
        # protects commits; an interrupted build leaves a valid older cache.
        db.execute('PRAGMA cache_size=-2048')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, VERSION):
            raise Invalid('cache version')
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


def _read(db, directory, actor_of):
    rows = db.execute('SELECT * FROM files').fetchall()
    if any(len(row) != 13 or _record_digest(row) != row[12] for row in rows):
        raise Invalid('cache metadata checksum')
    old = {r[0]: dict(meta=tuple(r[1:6]), offset=r[6], line=r[7], digest=r[8],
                       chunks=r[9], tail=r[10], tail_sha=r[11]) for r in rows}
    out, seen = [], set()
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
                entry = dict(chunks=seq, tail=tail, tail_sha=digest)
                # Do not retain parsed chunks while deserializing their saved
                # counterparts, especially on the first, large cache build.
                del current
            else:
                entry = previous
            count = 0
            for seq, data, digest in db.execute('SELECT seq,data,sha FROM chunks WHERE name=? ORDER BY seq', (name,)):
                if seq != count:
                    raise Invalid('cache chunk sequence')
                out.extend(_unpack(data, digest))
                count += 1
            if count != entry['chunks']:
                raise Invalid('cache missing chunks')
            out.extend(_unpack(entry['tail'], entry['tail_sha']))
        for name in old.keys() - seen:
            db.execute('DELETE FROM chunks WHERE name=?', (name,))
            db.execute('DELETE FROM files WHERE name=?', (name,))
    return out


def read(events_dir, cache_dir, actor_of):
    directory, cache_dir = pathlib.Path(events_dir), pathlib.Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = hashlib.sha256(os.fsencode(directory.resolve())).hexdigest()[:16]
    path, lock = cache_dir / f'ledger-{ident}.sqlite3', cache_dir / f'ledger-{ident}.lock'
    with lock.open('a+b') as fh:
        os.chmod(lock, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)
        # Permission is private from creation, including a first writer's DB.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        for attempt in range(2):
            db = None
            try:
                db = _connect(path)
                return _read(db, directory, actor_of)
            except (sqlite3.DatabaseError, Invalid) as exc:
                if db is not None:
                    db.close()
                    db = None
                if attempt:
                    raise OSError('ledger cache unavailable') from exc
                # Protected by our process lock; no reader can observe a
                # partially rebuilt DB. The ledger is never touched.
                path.unlink(missing_ok=True)
                pathlib.Path(str(path) + '-journal').unlink(missing_ok=True)
            finally:
                if db is not None:
                    db.close()
