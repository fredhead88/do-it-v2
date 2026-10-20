"""Disposable, cross-process parsed JSONL cache. No fold policy lives here.

Only raw events are saved: corrections, ordering and project filters run in fold.
The single snapshot is replaced atomically under flock. A checksum and restricted
unpickler make damage a cache miss, never executable cache content. Set
DOIT_READ_CACHE=0 (or read_events(use_cache=False)) for the reference reader.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import pickle
import sys
import tempfile

VERSION = 1
MAGIC = b'DOIT-PARSED-LEDGER-1\n'


class _DataOnly(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError('cache may contain data only')


def _load(path):
    with path.open('rb') as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ValueError('cache version')
        digest = fh.read(32)
        actual = hashlib.sha256()
        while chunk := fh.read(1024 * 1024):
            actual.update(chunk)
        if actual.digest() != digest:
            raise ValueError('cache checksum')
        fh.seek(len(MAGIC) + 32)
        return _DataOnly(fh).load()


class _HashWriter:
    def __init__(self, fh):
        self.fh, self.digest = fh, hashlib.sha256()

    def write(self, data):
        self.digest.update(data)
        return self.fh.write(data)


def _save(path, value):
    # A process dying before replace leaves the last complete snapshot usable.
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.ledger-', delete=False) as fh:
            name = fh.name
            fh.write(MAGIC)
            fh.write(b'0' * 32)
            writer = _HashWriter(fh)
            pickle.dump(value, writer, protocol=5)
            fh.seek(len(MAGIC))
            fh.write(writer.digest.digest())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def _meta(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _keys(obj):
    # JSON's decoder shares keys only WITHIN one json.loads call. Sharing keys
    # across lines cuts both snapshot size and the heap needed by every reader.
    return {sys.intern(k): v for k, v in obj.items()}


def _parse(data, name, actor, first_line):
    result = []
    for n, line in enumerate(data.decode('utf-8').splitlines(), first_line):
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_hook=_keys)
        except json.JSONDecodeError:
            continue
        event['actor'], event['_src'] = actor, f'{name}:{n}'
        result.append(event)
    return result


def _file(path, actor, old):
    st = path.stat()
    meta = _meta(st)
    if old and old['meta'] == meta:
        return old
    with path.open('rb') as fh:
        data = fh.read(st.st_size)
        if _meta(os.fstat(fh.fileno())) != meta or _meta(path.stat()) != meta:
            # Never publish metadata for a read that raced a writer/rotation.
            raise OSError('ledger changed during cache read')
    start, first_line, prefix = 0, 1, []
    if (old and meta[:2] == old['meta'][:2] and len(data) > old['meta'][2]
            and hashlib.sha256(data[:old['meta'][2]]).digest() == old['digest']):
        # Verify the entire old prefix, not just its endpoints: historical
        # repairs can rewrite AND grow a file without changing its inode.
        start, first_line, prefix = old['offset'], old['line'], old['events']
    end = data.rfind(b'\n') + 1
    complete = data[start:end]
    events = prefix + _parse(complete, path.name, actor, first_line)
    line = first_line + len(complete.decode('utf-8').splitlines())
    return {'meta': meta, 'digest': hashlib.sha256(data).digest(),
            'offset': end, 'line': line, 'events': events,
            # Reparse an unterminated final line on the next append, even if it
            # was valid JSON today. It may become invalid once bytes arrive.
            'tail': _parse(data[end:], path.name, actor, line)}


def read(events_dir, cache_dir, actor_of):
    """Return glob/line-ordered raw events, or raise for the uncached fallback."""
    events_dir, cache_dir = pathlib.Path(events_dir), pathlib.Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = hashlib.sha256(os.fsencode(events_dir.resolve())).hexdigest()[:16]
    path = cache_dir / f'ledger-{ident}.pickle'
    lock = cache_dir / f'ledger-{ident}.lock'
    with lock.open('a+b') as fh:
        os.chmod(lock, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            saved = _load(path)
            if saved['version'] != VERSION:
                raise ValueError('cache schema')
            old = saved['files']
        except (OSError, EOFError, ValueError, KeyError, TypeError, pickle.UnpicklingError):
            old = {}
        files, changed = {}, False
        for file in sorted(events_dir.glob('*.jsonl')):
            previous = old.get(file.name)
            current = _file(file, actor_of(file.name), previous)
            files[file.name] = current
            changed |= current is not previous
        changed |= files.keys() != old.keys()
        if changed:
            _save(path, {'version': VERSION, 'files': files})
        return [e for entry in files.values() for part in ('events', 'tail') for e in entry[part]]
