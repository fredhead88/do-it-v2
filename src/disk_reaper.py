"""Age-limited disk cleanup; uncertain filesystem/process answers retain data."""
import contextlib
import contextvars
import json
import math
import os
import pathlib
import re
import stat
import tempfile
import tomllib
from datetime import datetime, timezone

KEEP_VAR_TMP = ('dt',)
VAR_TMP_MAX_AGE_S = 3 * 86400
SESSION_LOG_MAX_AGE_S = 14 * 86400
ROOT_DISK_HIGH_PCT = 90
LOOK_TOML_PATH = pathlib.Path(__file__).resolve().parent.parent / 'look.toml'
_WORKTREE_PASS = contextvars.ContextVar('disk_reaper_worktrees', default=None)
_SETTINGS_REPORT = contextvars.ContextVar('disk_reaper_settings_report', default=None)
UNREADABLE_EXEMPT_NAMES = ('systemd', '(sd-pam)', 'sshd-session', 'ssh-agent')


@contextlib.contextmanager
def _settings_pass():
    token = _SETTINGS_REPORT.set(dict(config_invalid=set(), hold=False))
    try:
        yield
    finally:
        _SETTINGS_REPORT.reset(token)


def _unreadable_pid(proc_root, pid):
    """Classify only permission denials; a live pid's missing files retain."""
    import reap_tmp
    path = pathlib.Path(proc_root) / str(pid)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return 'exited', None
    if not reap_tmp.panes.alive(pid):
        return 'exited', None
    try:
        status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
        if [int(v) for v in status.get('Uid', '').split()] != [os.getuid()] * 4:
            return None, None
        token = (path / 'cmdline').read_bytes().split(b'\0', 1)[0].decode()
        name = pathlib.Path(re.split(r'[:\s]', token, maxsplit=1)[0]).name
        if not token or name not in UNREADABLE_EXEMPT_NAMES:
            return None, None
        try:
            root_owned = os.lstat(path / 'cwd').st_uid == 0
        except OSError:
            root_owned = False
        if root_owned:
            return 'root-owned cwd', name
        own = dict(line.split(':', 1) for line in
                   (pathlib.Path(proc_root) / 'self/status').read_text().splitlines() if ':' in line)
        if int(status['CapPrm'], 16) & ~int(own['CapEff'], 16):
            return 'permitted capability outside effective set', name
    except (OSError, ValueError, KeyError, UnicodeError):
        pass
    return None, None


def _budget(settings=None):
    settings = _settings() if settings is None else settings
    return dict(settings=settings, count=0, freed_bytes=0)


def _budget_reason(budget, dry_run, *, directory=True):
    settings = budget['settings']
    if settings['hold'] and not dry_run:
        return 'held: reap hold set'
    if directory and (budget['count'] >= settings['reap_max_entries_per_pass'] or
                      budget['freed_bytes'] >= settings['reap_max_gb_per_pass'] * 2**30):
        return f"deferred: pass cap {settings['reap_max_entries_per_pass']} reached"
    return None


def _surface_stats(settings):
    return dict(hold=settings['hold'], config_invalid=settings['config_invalid'],
                deferred=0, held=0, unreadable_exempt=0, other_uid_skipped=0, exempt=[])


def _count_reason(stats, reason):
    for action in ('held', 'deferred'):
        if reason and reason.startswith(action + ':'):
            stats[action] = stats.get(action, 0) + 1


def _settings():
    """Read one surface's settings and the machine-local stop controls."""
    import fold

    defaults = {
        'reap_max_entries_per_pass': 25,
        'reap_max_gb_per_pass': 10,
        'reap_retire_max_per_pass': 200,
        'reap_gone_grace_h': 6,
    }
    invalid = set()
    thresholds = {}
    broken = False
    try:
        with pathlib.Path(LOOK_TOML_PATH).open('rb') as stream:
            document = tomllib.load(stream)
        thresholds = document.get('thresholds', {})
        if not isinstance(thresholds, dict):
            raise ValueError('thresholds is not a table')
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        thresholds = {}
        invalid.add('look.toml')
        broken = True

    settings = {}
    for key, default in defaults.items():
        value = thresholds.get(key, default)
        count = key in ('reap_max_entries_per_pass', 'reap_retire_max_per_pass')
        # Avoid coercion: booleans and integral floats are not count values.
        valid = type(value) in (int, float) and value >= 0
        if valid and isinstance(value, float):
            valid = math.isfinite(value) and not count
        if not valid:
            invalid.add(key)
            value = default
        settings[key] = value

    toml_hold = thresholds.get('reap_hold', False)
    if type(toml_hold) is not bool:
        invalid.add('reap_hold')
        toml_hold = True
    env_hold = os.environ.get('DOIT_REAP_HOLD', '')
    if env_hold not in ('', '0', '1'):
        invalid.add('DOIT_REAP_HOLD')
    hold = (broken or toml_hold) if not env_hold else env_hold != '0'
    try:
        os.lstat(fold.ROOT / 'state' / 'reap-hold')
        hold = True
    except FileNotFoundError:
        pass
    except OSError:
        invalid.add('reap-hold')
    settings['hold'] = hold
    settings['config_invalid'] = invalid
    report = _SETTINGS_REPORT.get()
    if report is not None:
        report['config_invalid'].update(invalid)
        report['hold'] = report['hold'] or hold
    return settings


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
    stats['unreadable_exempt'] = 0
    stats['exempt'] = []
    refs = []
    for name in reap_tmp._pid_dir_names(proc_root):
        # Other users' proc links are unreadable by design. Establish ownership
        # first; uncertainty about our own processes still retains all data.
        try:
            uid = os.lstat(pathlib.Path(proc_root) / name).st_uid
        except FileNotFoundError:
            continue
        if uid != os.getuid():
            stats['other_uid_skipped'] += 1
            continue
        if reap_tmp.panes.alive(int(name)):
            try:
                refs.extend(r.removesuffix(' (deleted)') for r in reap_tmp._pid_refs(proc_root, int(name)))
            except PermissionError:
                why, process_name = _unreadable_pid(proc_root, int(name))
                if why is None:
                    raise
                if why != 'exited':
                    stats['unreadable_exempt'] += 1
                    if len(stats['exempt']) < 5:
                        stats['exempt'].append(dict(pid=int(name), name=process_name, why=why))
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
    budget = _budget()
    result = dict(removed=[], kept=[], freed_bytes=0, **_surface_stats(budget['settings']))
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
                    reason = _budget_reason(budget, dry_run)
                    if reason:
                        _count_reason(result, reason)
                        result['kept'].append(dict(path=str(path), reason=reason))
                        continue
                    _remove(path, records, dry_run=True)
                    if not dry_run:
                        _remove(path, records)
                    result['removed'].append(str(path))
                    result['freed_bytes'] += size
                    budget['count'] += 1
                    budget['freed_bytes'] += size
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
    budget = _budget()
    result = dict(removed=[], freed_bytes=0, ran=False, kept=[], errors={}, **_surface_stats(budget['settings']))
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
                    reason = _budget_reason(budget, dry_run)
                    if reason:
                        _count_reason(result, reason)
                    else:
                        _unlink_checked(path, st, dry_run=dry_run)
                        result['removed'].append(str(path))
                        result['freed_bytes'] += st.st_size
                        budget['count'] += 1
                        budget['freed_bytes'] += st.st_size
                except OSError as exc:
                    reason = 'undetermined: ' + str(exc)
                    result['errors'][str(path)] = reason
            if reason:
                result['kept'].append(dict(path=str(path), reason=reason))
        if not dry_run and not budget['settings']['hold']:
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
    if not dry_run and not result['errors'] and not result['held'] and not result['deferred'] and not budget['settings']['hold']:
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
