"""Conservative same-tick recovery of a missing, previously built checkout."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile


class RepairRetry(ValueError):
    """A counted transient failure: launch must not write a standing hold."""


class _Definitive(ValueError):
    pass


class _Passing(Exception):
    pass


def enabled(root):
    return not os.path.lexists(root / 'state/worktree-heal.off')


def project_of(evs, root):
    witness = next((e['project'] for e in reversed(evs)
                    if e.get('type') in ('spec-written', 'build-done') and e.get('project')), None)
    if (isinstance(witness, str) and witness not in ('.', '..')
            and '/' not in witness and '\x00' not in witness
            and (root / 'repos' / witness).is_dir()):
        return witness
    return next((e['project'] for e in reversed(evs) if e.get('project')), 'unknown')


def _read_state(path):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.worktree-heal.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(state, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _clear(path, key):
    try:
        state = _read_state(path)
        if key in state:
            del state[key]
            _write_state(path, state)
    except OSError as exc:
        print(f'worktree-heal: state file: {exc}', file=sys.stderr)


def _retry(path, key, prefix, cause):
    cause = str(cause)[:200]
    try:
        state = _read_state(path)
        count = state.get(key, {}).get('count', 0) + 1
        if count >= 6:
            _clear(path, key)
            raise ValueError(prefix + 'not recreated: repair failed 6 times in a row: ' + cause)
        state[key] = dict(count=count, last_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                          last_error=cause)
        while len(state) > 200:
            oldest = min(state, key=lambda k: state[k].get('last_ts', ''))
            del state[oldest]
        _write_state(path, state)
    except OSError as exc:
        _clear(path, key)
        raise ValueError(prefix + f'not recreated: retry not counted: {cause}; state file: {exc}') from exc
    raise RepairRetry(prefix + 'repair failed, retrying next tick: ' + cause)


def _cause(result):
    return (result.get('stderr') or result.get('stdout') or 'git command failed').strip()


def _wrapped(runner):
    def run(argv):
        try:
            return runner(argv)
        except Exception as exc:
            return dict(code=-1, stdout='', stderr=f'{type(exc).__name__}: {exc}', raised=True)
    return run


def _checked(result):
    if result.get('raised') or result.get('code', 1):
        raise _Passing(_cause(result))
    return result.get('stdout', '').strip()


def _registrations(text):
    rows, row = [], {}
    for line in text.splitlines() + ['']:
        if not line:
            if row:
                rows.append(row)
            row = {}
        else:
            key, _, value = line.partition(' ')
            row[key] = value
    return rows


def _repair(item, action, evs, root, project, wt, runner):
    import autodispatch
    import resources

    branch = item.lower()
    repo = root / 'repos' / project
    if os.path.lexists(wt):
        raise _Definitive('path is occupied by a non-directory')
    if not repo.is_dir():
        raise _Definitive(f'repo {repo} is not a directory')
    build = next((e for e in reversed(evs) if e.get('type') == 'build-done'), {})
    if build.get('branch') and build['branch'] != branch:
        raise _Definitive(f'newest build-done names branch {build["branch"]}, not {branch}')
    ready = build.get('ready_sha')
    if action != 'rebuild' and not ready:
        raise _Definitive('no build-done with a ready_sha')
    first = next((e['project'] for e in evs if e.get('project')), None)
    if action == 'rebuild' and first and project != first:
        raise _Definitive(f"project {project} is not the subject's first recorded project {first}")

    run = _wrapped(runner)
    prefix = ['git', '-C', str(repo)]
    result = run(prefix + ['rev-parse', '--show-toplevel'])
    if not result.get('raised') and result.get('code', 1) and 'not a git repository' in _cause(result):
        raise _Definitive(f'repo {repo} is not the top of a git work tree')
    top = _checked(result)
    if Path(top).resolve() != repo.resolve():
        raise _Definitive(f'repo {repo} is not the top of a git work tree')
    result = run(prefix + ['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'])
    if not result.get('raised') and result.get('code') == 1:
        raise _Definitive(f'branch {branch} does not exist')
    _checked(result)
    if action != 'rebuild':
        result = run(prefix + ['rev-parse', '--verify', '--quiet', f'{ready}^{{commit}}'])
        if not result.get('raised') and result.get('code') == 1:
            raise _Definitive(f'ready_sha {ready} is not a commit in {repo}')
        expected = _checked(result)
        tip = _checked(run(prefix + ['rev-parse', '--verify', f'refs/heads/{branch}^{{commit}}']))
        if tip != expected:
            raise _Definitive(f'branch {branch} tip {tip} is not ready_sha {ready}')
    rows = _registrations(_checked(run(prefix + ['worktree', 'list', '--porcelain'])))
    stale = []
    for row in rows:
        path = Path(row['worktree']).resolve()
        if path == wt.resolve():
            if 'locked' in row:
                raise _Definitive(f'registration of {wt} is locked')
        elif row.get('branch') == f'refs/heads/{branch}':
            if path.is_dir() or 'locked' in row or 'prunable' not in row:
                raise _Definitive(f'branch {branch} is checked out at {path}')
            stale.append(path)
    # Only registrations proven stale by the complete read-only pass are removed.
    for path in stale:
        _checked(run(prefix + ['worktree', 'remove', str(path)]))
    try:
        result = autodispatch.add_builder_worktree(repo, wt, branch, branch, runner=run)
    except resources.RecordReadbackError as exc:
        raise _Passing(str(exc)) from exc
    except Exception as exc:
        removed = run(prefix + ['worktree', 'remove', str(wt)])
        if removed.get('raised') or removed.get('code', 1):
            raise _Definitive(f'record failed: {exc}; removing the new tree failed: {_cause(removed)}') from exc
        raise _Passing(str(exc)) from exc
    _checked(result)


def repair(item, action, evs, root, project, wt, runner):
    """Repair only an absent checkout; return whether this launch recreated it."""
    if wt.is_dir():
        return False
    state, key = root / 'state/worktree-heal.json', f'{item} {action}'
    prefix = f'worktree {wt} missing; '
    try:
        _repair(item, action, evs, root, project, wt, runner)
    except _Passing as exc:
        try:
            _retry(state, key, prefix, str(exc))
        except (RepairRetry, ValueError):
            raise
        except Exception as unexpected:
            _clear(state, key)
            raise ValueError(prefix + f'not recreated: unexpected error: {type(unexpected).__name__}: {unexpected}') from unexpected
    except Exception as exc:
        _clear(state, key)
        cause = str(exc) if isinstance(exc, _Definitive) else f'unexpected error: {type(exc).__name__}: {exc}'
        raise ValueError(prefix + 'not recreated: ' + cause) from exc
    _clear(state, key)
    return True
