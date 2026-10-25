"""Age-limited disk cleanup; uncertain filesystem/process answers retain data."""
import contextlib
import contextvars
import json
import os
import pathlib
import stat
import tempfile
from datetime import datetime, timezone

KEEP_VAR_TMP = ('dt',)
VAR_TMP_MAX_AGE_S = 3 * 86400
SESSION_LOG_MAX_AGE_S = 14 * 86400
ROOT_DISK_HIGH_PCT = 90
_WORKTREE_PASS = contextvars.ContextVar('disk_reaper_worktrees', default=None)


@contextlib.contextmanager
def worktree_pass(proc_root='/proc', *, stats=None):
    """Carry injectable process reads and accounting through the existing API."""
    rows = []
    stats = {'other_uid_skipped': 0} if stats is None else stats
    token = _WORKTREE_PASS.set((proc_root, rows, stats))
    try:
        yield rows
    finally:
        _WORKTREE_PASS.reset(token)


def process_refs(proc_root=None, *, stats=None):
    import reap_tmp
    context = _WORKTREE_PASS.get()
    proc_root = proc_root if proc_root is not None else context[0] if context else '/proc'
    stats = stats if stats is not None else context[2] if context and len(context) > 2 else {}
    stats['other_uid_skipped'] = 0
    refs = []
    for name in reap_tmp._pid_dir_names(proc_root):
        # Other users' proc links are unreadable by design. Establish ownership
        # first; uncertainty about our own processes still retains all data.
        if os.lstat(pathlib.Path(proc_root) / name).st_uid != os.getuid():
            stats['other_uid_skipped'] += 1
            continue
        if reap_tmp.panes.alive(int(name)):
            refs.extend(r.removesuffix(' (deleted)') for r in reap_tmp._pid_refs(proc_root, int(name)))
    return refs


def _open_dir(path):
    """Anchor every ancestor, refusing symlinks even above the named root."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in pathlib.Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size, st.st_mtime_ns


def _inventory(path, uid=None):
    """Full lstat inventory before deletion; never follow links or cross mounts."""
    path = pathlib.Path(path)
    records = {}
    parent = _open_dir(path.parent)
    try:
        device = os.fstat(parent).st_dev
        def visit(fd, name, rel):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if st.st_dev != device or os.path.ismount(path / rel) or (uid is not None and st.st_uid != uid):
                raise OSError('mount boundary or owned by another uid')
            records[rel] = st
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(st):
                        raise OSError('directory changed during scan')
                    for entry in sorted(os.listdir(child)):
                        visit(child, entry, rel / entry)
                finally:
                    os.close(child)
        visit(parent, path.name, pathlib.Path('.'))
    finally:
        os.close(parent)
    return records


def entry_bytes(path):
    return sum(st.st_size for st in _inventory(path, os.getuid()).values() if stat.S_ISREG(st.st_mode))


def _remove(path, records, dry_run=False):
    """Remove an inventoried entry using anchored fds and identity checks."""
    path = pathlib.Path(path)
    children = {}
    for rel in records:
        if rel != pathlib.Path('.'):
            children.setdefault(rel.parent, set()).add(rel.name)
    parent = _open_dir(path.parent)
    try:
        def remove(fd, name, rel):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            expected = records[rel]
            if _identity(st) != _identity(expected):
                raise OSError('entry changed since scan')
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(expected):
                        raise OSError('directory changed since scan')
                    names = sorted(os.listdir(child))
                    if set(names) != children.get(rel, set()):
                        raise OSError('directory contents changed since scan')
                    for entry in names:
                        remove(child, entry, rel / entry)
                finally:
                    os.close(child)
                if not dry_run:
                    os.rmdir(name, dir_fd=fd)
            elif not dry_run:
                os.unlink(name, dir_fd=fd)
        remove(parent, path.name, pathlib.Path('.'))
    finally:
        os.close(parent)


def reap_var_tmp(root='/var/tmp', uid=None, now=None, proc_root='/proc', recorded_refs=(), dry_run=False):
    import reap_tmp
    root = pathlib.Path(root)
    uid = os.getuid() if uid is None else uid
    now = now or datetime.now(timezone.utc)
    result = dict(removed=[], kept=[], freed_bytes=0, other_uid_skipped=0)
    fd = _open_dir(root)
    try:
        names = sorted(os.listdir(fd))
    finally:
        os.close(fd)
    refs, ref_error = None, None
    for name in names:
        path = root / name
        reason = None
        try:
            st = os.lstat(path)
            if st.st_uid != uid:
                reason = 'owned by another uid'
            elif name in KEEP_VAR_TMP:
                reason = 'keep-list'
            elif reap_tmp._recorded_overlap(path, recorded_refs):
                reason = 'recorded live resource'
            elif now.timestamp() - reap_tmp._newest_mtime(path, 3) <= VAR_TMP_MAX_AGE_S:
                reason = 'too young'
            elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                reason = 'unsupported entry type'
            else:
                if refs is None and ref_error is None:
                    try:
                        refs = process_refs(proc_root, stats=result)
                    except OSError as exc:
                        ref_error = exc
                if ref_error is not None:
                    raise ref_error
                if any(reap_tmp._under(ref, path) for ref in refs):
                    reason = 'live process references entry'
                else:
                    records = _inventory(path, uid)
                    if _identity(records[pathlib.Path('.')]) != _identity(st):
                        raise OSError('entry changed during eligibility check')
                    if now.timestamp() - max(s.st_mtime for rel, s in records.items()
                                             if len(rel.parts) <= 3) <= VAR_TMP_MAX_AGE_S:
                        raise OSError('entry became too young during scan')
                    size = sum(s.st_size for s in records.values() if stat.S_ISREG(s.st_mode))
                    _remove(path, records, dry_run=True)
                    if not dry_run:
                        _remove(path, records)
                    result['removed'].append(str(path))
                    result['freed_bytes'] += size
        except OSError as exc:
            reason = 'undetermined: ' + str(exc)
        if reason:
            result['kept'].append(dict(path=str(path), reason=reason))
    return result


def _unlink_checked(path, expected, directory=False, dry_run=False):
    parent = _open_dir(path.parent)
    try:
        actual = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        # Directory mtimes change when their children are removed.
        same = _identity(actual)[:4] == _identity(expected)[:4] if directory else _identity(actual) == _identity(expected)
        if not same or actual.st_uid != os.getuid():
            raise OSError('entry changed or owned by another uid')
        if not dry_run:
            (os.rmdir if directory else os.unlink)(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def reap_session_logs(roots=None, now=None, proc_root='/proc', dry_run=False, state_path=None):
    import reap_tmp
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date().isoformat()
    roots = [pathlib.Path.home() / '.claude/projects', pathlib.Path.home() / '.codex/sessions'] if roots is None else roots
    state_path = pathlib.Path(state_path) if state_path is not None else reap_tmp.ROOT / 'state/disk-reaper-session-logs.json'
    result = dict(removed=[], freed_bytes=0, ran=False, kept=[], errors={}, other_uid_skipped=0)
    try:
        if json.loads(state_path.read_text()).get('date') == today:
            return result
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        result['errors']['state'] = 'undetermined: ' + str(exc)
        return result
    result['ran'] = True
    try:
        refs = process_refs(proc_root, stats=result)
    except OSError as exc:
        result['errors']['proc'] = 'undetermined: ' + str(exc)
        result['kept'] = [dict(path=str(root), reason=result['errors']['proc']) for root in roots]
        return result
    for root in map(pathlib.Path, roots):
        try:
            records = _inventory(root)
            if not stat.S_ISDIR(records[pathlib.Path('.')].st_mode):
                raise OSError('session root is not a directory')
        except FileNotFoundError:
            # Missing roots are normal for a backend not used on this machine.
            if not root.exists() and not root.is_symlink():
                continue
            result['errors'][str(root)] = 'undetermined: entry disappeared during scan'
            continue
        except OSError as exc:
            result['errors'][str(root)] = 'undetermined: ' + str(exc)
            continue
        for rel, st in records.items():
            path = root / rel
            if not stat.S_ISREG(st.st_mode) or not path.name.endswith('.jsonl'):
                continue
            reason = None
            if st.st_uid != os.getuid():
                reason = 'owned by another uid'
            elif now.timestamp() - st.st_mtime <= SESSION_LOG_MAX_AGE_S:
                reason = 'too young'
            elif any(reap_tmp._same(ref, path) for ref in refs):
                reason = 'live process holds log open'
            else:
                try:
                    _unlink_checked(path, st, dry_run=dry_run)
                    result['removed'].append(str(path))
                    result['freed_bytes'] += st.st_size
                except OSError as exc:
                    reason = 'undetermined: ' + str(exc)
                    result['errors'][str(path)] = reason
            if reason:
                result['kept'].append(dict(path=str(path), reason=reason))
        if not dry_run:
            for rel in sorted(records, key=lambda p: len(p.parts), reverse=True):
                st = records[rel]
                if rel == pathlib.Path('.') or not stat.S_ISDIR(st.st_mode):
                    continue
                path = root / rel
                try:
                    fd = _open_dir(path)
                    try:
                        empty = not os.listdir(fd)
                    finally:
                        os.close(fd)
                    if empty and not any(reap_tmp._under(ref, path) for ref in refs):
                        _unlink_checked(path, st, directory=True)
                except OSError as exc:
                    result['errors'][str(path)] = 'undetermined: ' + str(exc)
    if not dry_run and not result['errors']:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=state_path.parent, delete=False) as f:
            marker = pathlib.Path(f.name)
            try:
                json.dump({'date': today}, f)
                f.close()
                marker.replace(state_path)
            finally:
                marker.unlink(missing_ok=True)
    return result
