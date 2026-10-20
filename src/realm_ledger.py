"""One bounded live-checkout module bundle and ledger snapshot for Realm.

Bare imports in the live checkout must never resolve to Realm's older siblings.
Keep one private bundle, temporarily install its aliases under Python's import
lock, then restore the registry. Reimport only when that checkout changes.
"""
import _imp
import contextlib
import gc
import importlib.util
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
        self.generation = None
        self.clock_minute = None
        self.watch = None
        self.file_offsets = {}
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
                if self.watch is not None:
                    self.watch.close()
                self.watch = None
                self.file_offsets = {}
                self.key, self.modules = key, {}
                self.events = self.folded = self.rows = self.offsets = None
                self.generation = self.clock_minute = None
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
        if self.watch is None:
            self.watch = Changes(events_dir)
        changed = self.watch.poll()
        generation = (fingerprint(events_dir) if changed is None or changed or self.generation is None
                      else self.generation)
        if generation != self.generation or self.events is None:
            # Drop this owner's old snapshot before allocating the next one.
            # Request threads may finish with the previous immutable snapshot.
            self.events = self.folded = self.rows = self.offsets = None
            events = fold.read_events()
            self.reads += 1
            fold.NOW = now
            folded = fold.fold(events)
            self.folds += 1
            if not isinstance(folded, tuple) or len(folded) != 4 or not isinstance(folded[0], dict):
                raise ValueError('fold returned an invalid four-part spec map')
            self.events, self.folded, self.generation = events, folded, generation
            self.clock_minute = int(now.timestamp() // 60)
        elif self.clock_minute != int(now.timestamp() // 60):
            # Owed deadlines/age may cross without any ledger event. Reuse the
            # parsed events, but never freeze time-dependent derived states.
            fold.NOW = now
            self.folded = fold.fold(self.events)
            self.folds += 1
            self.clock_minute = int(now.timestamp() // 60)
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
