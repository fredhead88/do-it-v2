"""Best-effort, host-wide CPU shares for heavy jobs; never an admission gate."""
import contextlib
import errno
import fcntl
import os
from pathlib import Path
import secrets
import threading
import time

NICE_INCREMENT = 10
_local = threading.local()


def _root(root):
    return Path(root if root is not None else os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))


def _key(root):
    return os.getpid(), str(_root(root).resolve())


def _active(root):
    return getattr(_local, 'claims', {}).get(_key(root))


def workers(held, ncpu=None, parent=None):
    if ncpu is None:
        try:
            ncpu = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            ncpu = os.cpu_count() or 1
    n = max(1, ncpu // ((held if held is not None else 1) + 1))
    if parent is None:
        parent = os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS')
    try:
        cap = int(parent)
        if cap > 0:
            n = min(n, cap)
    except (TypeError, ValueError):
        pass
    return n


def count(root=None):
    directory = _root(root) / 'state' / 'cpu-share'
    try:
        # Missing state is empty; a non-directory state is an unknown count.
        paths = list(directory.iterdir())
        held = 0
        for path in paths:
            if not path.name.endswith('.claim'):
                continue
            try:
                with path.open('r') as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        held += 1
                    else:
                        path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
        return held
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def share(root=None):
    held = count(root)
    return workers(None if held is None else held + (0 if _active(root) else 1))


@contextlib.contextmanager
def claim(root=None, enabled=True):
    active = _active(root)
    if not enabled or active is not None:
        yield active if active is not None else share(root)
        return
    directory = _root(root) / 'state' / 'cpu-share'
    handle = temporary = final = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = f'{os.getpid()}-{secrets.token_hex(12)}'
        temporary, final = directory / f'.{name}.tmp', directory / f'{name}.claim'
        handle = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        temporary.rename(final)
    except OSError:
        if handle is not None:
            os.close(handle)
            handle = None
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
    claims = getattr(_local, 'claims', None)
    if claims is None:
        claims = _local.claims = {}
    key = _key(root)
    # Mark ownership before counting, so our own claim is not charged twice.
    claims[key] = workers(None)
    try:
        claims[key] = share(root) if handle is not None else workers(None)
        yield claims[key]
    finally:
        claims.pop(key, None)
        if handle is not None:
            with contextlib.suppress(OSError):
                final.unlink(missing_ok=True)
            os.close(handle)


def codex_claim(role, root=None):
    return claim(root, enabled=role == 'builder')


def child_env(env, n):
    return dict(env, PYTEST_XDIST_AUTO_NUM_WORKERS=str(n), CI_WORKERS=str(n))


def launch_env(env, root=None):
    held = _active(root)
    return child_env(env, held if held is not None else share(root))


def niced(argv):
    return (['/usr/bin/nice', '-n', str(NICE_INCREMENT)] if os.access('/usr/bin/nice', os.X_OK)
            else []) + list(argv)


def lower_autogroup(pid):
    if pid == os.getpid():
        return
    for attempt in range(11):
        try:
            Path(f'/proc/{pid}/autogroup').write_text(str(NICE_INCREMENT))
            return
        except OSError as exc:
            # Linux throttles these writes per uid, including other runners.
            # The child is already running; never turn this into admission.
            if exc.errno != errno.EAGAIN or attempt == 10:
                return
            time.sleep(.1)
