"""Read-only virtual ledger files (FORMAT_VERSION 1).

segments/<segment>.jsonl contains member bytes back to back. Its matching
<segment>.idx.json is {"version": 1, "segment": "<segment>.jsonl", "bytes": int,
"sha256": "<whole segment hex>", "members": [{"name": "original.jsonl",
"offset": int, "length": int, "lines": int, "sha256": "<member hex>",
"ends_nl": bool}]}. Empty members have ends_nl=true. Indexes are authoritative;
structurally damaged indexes are skipped and exposed by problems(). Hashing is
reserved for verify_segments(), never a read path. Reads create nothing and
never lock. Only verify_segments writes its disposable, atomic state cache.
"""
import io
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

FORMAT_VERSION = 1
SEGMENT_DIR = "segments"


def _name(name):
    if not isinstance(name, str) or not name or '/' in name or '..' in name or '\\' in name:
        raise ValueError('name must be a basename without ..')
    return name


def _integer(value):
    return type(value) is int and value >= 0


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _index(path):
    """Validate structure and boundary bytes, without reading event content."""
    try:
        obj = json.loads(path.read_text())
        if not isinstance(obj, dict) or type(obj.get('version')) is not int or obj.get('version') != FORMAT_VERSION:
            raise ValueError('version')
        segment = _name(obj.get('segment'))
        if segment != path.name[:-9] + '.jsonl':
            raise ValueError('segment name must match index')
        if not _integer(obj.get('bytes')) or not _hash(obj.get('sha256')):
            raise ValueError('bytes or sha256')
        members = obj.get('members')
        if not isinstance(members, list):
            raise ValueError('members list')
        file = path.parent / segment
        if not file.is_file():
            raise ValueError('segment file missing')
        if file.stat().st_size != obj['bytes']:
            raise ValueError('segment bytes size')
        seen = set()
        with file.open('rb') as stream:
            for member in members:
                if not isinstance(member, dict):
                    raise ValueError('member object')
                name = _name(member.get('name'))
                if not name.endswith('.jsonl') or name in seen:
                    raise ValueError('member name or duplicate member')
                seen.add(name)
                if not all(_integer(member.get(k)) for k in ('offset', 'length', 'lines')):
                    raise ValueError('member offset, length or lines')
                if not _hash(member.get('sha256')) or type(member.get('ends_nl')) is not bool:
                    raise ValueError('member sha256 or ends_nl')
                offset, length = member['offset'], member['length']
                if offset + length > obj['bytes']:
                    raise ValueError('member slice outside segment')
                if length:
                    stream.seek(offset + length - 1)
                    if (stream.read(1) == b'\n') != member['ends_nl']:
                        raise ValueError('member ends_nl disagrees with segment')
                elif not member['ends_nl']:
                    raise ValueError('empty member ends_nl')
        return obj, None
    except (OSError, ValueError, TypeError) as exc:
        return None, str(exc)


def _indexes(events_dir):
    directory = Path(events_dir) / SEGMENT_DIR
    return [(path, *_index(path)) for path in sorted(directory.glob('*.idx.json'))]


def _members(events_dir, name):
    return [(path, obj, member) for path, obj, _ in _indexes(events_dir) if obj
            for member in obj['members'] if member['name'] == name]


def segment_members(events_dir) -> dict[str, list[tuple[str, int, int]]]:
    """Member slices in sorted valid-index order; invalid indexes are skipped."""
    result = {}
    for _, obj, _ in _indexes(events_dir):
        if obj:
            for member in obj['members']:
                result.setdefault(member['name'], []).append(
                    (obj['segment'], member['offset'], member['length']))
    return result


def names(events_dir):
    directory = Path(events_dir)
    # iterdir, unlike glob, refuses a missing directory.
    result = {_name(p.name) for p in directory.iterdir() if p.suffix == '.jsonl'}
    for _, obj, _ in _indexes(directory):
        if obj:
            result.update(member['name'] for member in obj['members'])
    return sorted(result)


def exists(events_dir, name):
    name = _name(name)
    return (Path(events_dir) / name).is_file() or bool(_members(events_dir, name))


def _read_bytes(events_dir, name):
    """The shared byte composition rule, also used by the incremental cache."""
    name = _name(name)
    members = _members(events_dir, name)
    parts = bytearray()
    for path, obj, member in members:
        if parts and not parts.endswith(b'\n'):
            parts.extend(b'\n')
        with (path.parent / obj['segment']).open('rb') as stream:
            stream.seek(member['offset'])
            data = stream.read(member['length'])
            if len(data) != member['length']:
                raise OSError('segment changed during read')
            parts.extend(data)
    try:
        live = (Path(events_dir) / name).read_bytes()
    except FileNotFoundError:
        if not members:
            raise
        return bytes(parts)
    packed = bytes(parts)
    if len(live) == len(packed) and hashlib.sha256(live).digest() == hashlib.sha256(packed).digest():
        return packed
    if live.startswith(packed):
        return packed + live[len(packed):]
    return packed + (b'\n' if packed and not packed.endswith(b'\n') else b'') + live


def _open_binary(events_dir, name):
    """Internal stream used by the bounded cache prefix reader."""
    name = _name(name)
    if _members(events_dir, name):
        return io.BytesIO(_read_bytes(events_dir, name))
    return (Path(events_dir) / name).open('rb')


def _read_range(events_dir, name, start, size, identity):
    """Live-only incremental read for Realm, with the existing race checks."""
    with (Path(events_dir) / _name(name)).open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != identity or opened.st_size < size:
            raise OSError(f'ledger changed before read: {name}')
        stream.seek(start)
        data = stream.read(size - start)
    if len(data) != size - start:
        raise OSError(f'ledger changed during read: {name}')
    return data


def read_text(events_dir, name, errors='strict'):
    name = _name(name)
    if not _members(events_dir, name):
        return (Path(events_dir) / name).read_text(errors=errors)
    # Match Path.read_text's universal newline translation.
    return _read_bytes(events_dir, name).decode('utf-8', errors).replace('\r\n', '\n').replace('\r', '\n')


def iter_sources(events_dir):
    for name in names(events_dir):
        yield name, read_text(events_dir, name).splitlines()


def stat_key(events_dir, name):
    name = _name(name)
    members = _members(events_dir, name)
    try:
        live = (Path(events_dir) / name).stat()
    except FileNotFoundError:
        if not members:
            raise
        live = None
    if not members:
        return live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns, live.st_ctime_ns
    stats = [path.stat() for path, _, _ in members]
    if live is not None:
        stats.append(live)
    first = live if live is not None else stats[0]
    total = sum(member['length'] for _, _, member in members)
    return (first.st_dev, first.st_ino, total + (live.st_size if live else 0),
            max(st.st_mtime_ns for st in stats), max(st.st_ctime_ns for st in stats))


def _accessible(path, directory=False):
    mode = path.stat().st_mode
    if not mode & 0o444 or (directory and not mode & 0o111):
        raise PermissionError(str(path))
    if not os.access(path, os.R_OK | (os.X_OK if directory else 0)):
        raise PermissionError(str(path))
    if not directory:
        with path.open('rb'):
            pass


def assert_readable(events_dir):
    directory = Path(events_dir)
    _accessible(directory, True)
    for path in directory.iterdir():
        if path.suffix == '.jsonl':
            _accessible(path)
    segments = directory / SEGMENT_DIR
    if segments.exists():
        _accessible(segments, True)
        for path in segments.iterdir():
            if path.name.endswith(('.jsonl', '.idx.json')):
                _accessible(path)


def problems(events_dir):
    directory = Path(events_dir) / SEGMENT_DIR
    if not directory.exists():
        return []
    result, indexed, segments = [], set(), set()
    for path, obj, reason in _indexes(events_dir):
        if obj is None:
            result.append((str(path), reason))
        else:
            segments.add(obj['segment'])
            indexed.update(member['name'] for member in obj['members'])
    for path in sorted(directory.glob('*.jsonl')):
        if path.name not in segments and not path.with_suffix('.idx.json').exists() and time.time() - path.stat().st_mtime > 3600:
            result.append((str(path), 'orphan segment older than one hour'))
    pending = directory / '.pending'
    if pending.exists():
        result.extend((str(path), 'pending file not indexed as member')
                      for path in sorted(pending.iterdir()) if path.is_file() and path.name not in indexed)
    return result


def verify_segments(events_dir, state_dir):
    indexes = [(path, obj) for path, obj, _ in _indexes(events_dir) if obj]
    if not indexes:
        return []
    state_dir = Path(state_dir)
    destination = state_dir / 'ledger-segments-verified.json'
    try:
        cached = json.loads(destination.read_text())
        if not isinstance(cached, dict):
            cached = {}
    except (OSError, ValueError):
        cached = {}
    result, state = [], {}
    for path, obj in indexes:
        mtime = path.stat().st_mtime_ns
        entry = cached.get(path.name)
        if not isinstance(entry, dict) or entry.get('mtime_ns') != mtime or type(entry.get('ok')) is not bool:
            digest = hashlib.sha256()
            with (path.parent / obj['segment']).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            entry = dict(mtime_ns=mtime, ok=digest.hexdigest() == obj['sha256'])
        state[path.name] = entry
        if not entry['ok']:
            result.append((str(path.parent / obj['segment']), 'sha256 mismatch'))
    if state != cached:
        state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.ledger-segments-', dir=state_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return result
