"""One bounded live-checkout module bundle and ledger snapshot for Realm.

Bare imports in the live checkout must never resolve to Realm's older siblings.
Keep one private bundle, temporarily install its aliases under Python's import
lock, then restore the registry. Reimport only when that checkout changes.
"""
import _imp
import codecs
import contextlib
import gc
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading


def fingerprint(directory, pattern='*.jsonl'):
    return tuple((p.name, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                 for p in sorted(Path(directory).glob(pattern)) for s in [p.stat()])


class Ledger:
    def __init__(self):
        self.lock = threading.RLock()
        self.key = None
        self.modules = {}
        self.events = self.folded = self.rows = self.offsets = None
        self.codex_sessions = None
        self.generation = None
        self.clock_minute = None
        self.watch = None
        self.file_offsets = {}
        self.raw_events = []
        self._raw_revision = 0
        self.events_dir = None
        self.event_key = None
        self._metadata = None
        self._rescan = True
        self._force = True
        self._last_fold = None
        self._snapshot_offsets = {}
        self.folds = self.reads = self.loads = 0

    @contextlib.contextmanager
    def live(self, src, root):
        src, root = Path(src).resolve(), Path(root).resolve()
        code = tuple(row for row in fingerprint(src, '*.py')
                     if not row[0].startswith(('test_', 'dispatch_checks_')))
        key = (src, root, os.environ.get('DOIT_PROJECT'), code)
        with self.lock:
            if key != self.key:
                replacing = self.key is not None
                self.key, self.modules = key, {}
                self._force = True
                if replacing:
                    # Modules contain function/global cycles. Collect a retired
                    # bundle before allocating the next ledger, not hours later
                    # after a series of merges has inflated the process heap.
                    gc.collect()
            _imp.acquire_lock()
            before_path, before = sys.path[:], sys.modules.copy()
            try:
                sys.path.insert(0, str(src))
                names = {p.stem for p in src.glob('*.py')}
                packages = {p.name for p in src.iterdir() if p.is_dir() and (p / '__init__.py').is_file()}
                for name in list(sys.modules):
                    if name in names or name.split('.')[0] in packages:
                        sys.modules.pop(name, None)
                sys.modules.update(self.modules)
                if 'fold' not in self.modules:
                    spec = importlib.util.spec_from_file_location('_realm_live_fold', src / 'fold.py')
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    sys.modules['fold'] = module
                    self.loads += 1
                yield sys.modules['fold']
            finally:
                # Retain only this bundle, not successive copies of imported
                # code objects/cycles at every refresh. Dependencies imported
                # lazily by the classifier are retained too.
                self.modules = {name: module for name, module in sys.modules.items()
                                if before.get(name) is not module}
                sys.path[:] = before_path
                for name in list(sys.modules):
                    if name not in before:
                        del sys.modules[name]
                sys.modules.update(before)
                _imp.release_lock()

    def refresh(self, fold, events_dir, now):
        """Read new bytes transactionally, then publish a consistent bounded snapshot."""
        with self.lock:
            directory = Path(events_dir).resolve()
            new_directory = directory != self.events_dir
            if new_directory:
                if self.watch is not None:
                    self.watch.close()
                self.watch = Changes(directory)
            changed = self.watch.poll()
            scan = new_directory or self._rescan or changed is None or bool(changed)
            if scan:
                # A failure must trigger a retry even after poll consumed notifications.
                self._rescan = True
                metadata = fingerprint(directory)
                previous = {} if new_directory else self.file_offsets
                staged = {}
                for meta in metadata:
                    name, dev, inode, size = meta[:4]
                    old = previous.get(name)
                    if old and old['identity'] == (dev, inode) and size >= old['size']:
                        if size == old['size']:
                            staged[name] = old
                            continue
                        start, pending, line, events = old['size'], old['pending'], old['line'], old['events']
                    else:
                        start, pending, line, events = 0, b'', 0, []
                    with (directory / name).open('rb') as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino) != (dev, inode) or opened.st_size < size:
                            raise OSError(f'ledger changed before read: {name}')
                        stream.seek(start)
                        data = stream.read(size - start)
                    if len(data) != size - start:
                        raise OSError(f'ledger changed during read: {name}')
                    data = pending + data
                    end = data.rfind(b'\n') + 1
                    complete, pending = data[:end], data[end:]
                    codecs.getincrementaldecoder('utf-8')().decode(pending, final=False)
                    parsed = []
                    # splitlines matches the reference's CRLF and Unicode line numbering.
                    for text in complete.decode('utf-8').splitlines():
                        line += 1
                        if not text.strip():
                            continue
                        try:
                            event = fold.ledger_compact.compact(json.loads(text))
                        except json.JSONDecodeError:
                            continue
                        event['actor'], event['_src'] = fold.actor_of(name), f'{name}:{line}'
                        parsed.append(event)
                    # Strictly validate completed bytes; a torn UTF-8 suffix stays pending.
                    staged[name] = dict(identity=(dev, inode), size=size, pending=pending,
                                        line=line, events=events + parsed, offset=size-len(pending))
                if new_directory or metadata != self._metadata:
                    # Preserve retained objects on a mere code/project change or idle poll.
                    if new_directory or staged != previous:
                        self.file_offsets = staged
                        self.raw_events = [e for state in staged.values() for e in state['events']]
                        self._raw_revision += 1
                        self.reads += 1
                    self._metadata = metadata
                self.events_dir = directory
                self._rescan = False
            minute = int(now.timestamp() // 60)
            source_key = (self.key, self._raw_revision)
            event_change = self.event_key != source_key
            exceptional = self.events is None or self._force or new_directory
            due = exceptional or self._last_fold is None or (now - self._last_fold).total_seconds() >= 10
            if due and (exceptional or event_change or self.clock_minute != minute):
                events = fold.finish_events(self.raw_events) if event_change or exceptional else self.events
                fold.NOW = now
                folded = fold.fold(events)
                if not isinstance(folded, tuple) or len(folded) != 4 or not isinstance(folded[0], dict):
                    raise ValueError('fold returned an invalid four-part spec map')
                self.folds += 1
                # Never mutate a pair a previous caller may still hold.
                self.events, self.folded = events, folded
                self.generation = object()
                if event_change or exceptional:
                    self.rows = self.codex_sessions = None
                    self._snapshot_offsets = {name: state['offset'] for name, state in self.file_offsets.items()}
                self.offsets = dict(self._snapshot_offsets)
                self.event_key = source_key
                self.clock_minute, self._last_fold, self._force = minute, now, False
            return self.events, self.folded


class Changes:
    """Linux directory notifications; a full directory scan on any uncertainty.

    Watching the directory covers new actor files and rotation without 10,000
    per-file watches or 10,000 stat calls every idle second. No ledger writes.
    """
    def __init__(self, directory):
        self.fd = None
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            self.fd = fd
            # MODIFY, ATTRIB, CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE,
            # DELETE_SELF, MOVE_SELF. Overflow and IGNORED are always sent.
            if libc.inotify_add_watch(fd, os.fsencode(directory), 0xFCE) < 0:
                self.close()
        except (AttributeError, OSError):
            self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def poll(self):
        if self.fd is None:
            return None
        import struct
        names = set()
        try:
            while True:
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    return names
                if not data:
                    self.close()
                    return None
                offset = 0
                while offset < len(data):
                    _watch, mask, _cookie, length = struct.unpack_from('iIII', data, offset)
                    name = os.fsdecode(data[offset + 16:offset + 16 + length].rstrip(b'\0'))
                    offset += 16 + length
                    if mask & (0x4000 | 0x8000 | 0x400 | 0x800):
                        # Overflow, removed watch or replaced directory: scan.
                        self.close()
                        return None
                    if name.endswith('.jsonl'):
                        names.add(name)
        except (OSError, ValueError, struct.error):
            self.close()
            return None
