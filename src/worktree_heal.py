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


def fresh_trigger(step, evs):
    """Consume the selected rebuild; never create eligibility or another budget."""
    import nextstep
    if step.action != 'rebuild' or step.state != 'action' or step.owner != 'tick':
        return False
    outcome = next((e for e in reversed(evs) if e.get('type') in {'build-done','build-blocked'}), None)
    if not outcome:
        return False
    newer = lambda e: nextstep._time(e['ts']) > nextstep._time(outcome['ts'])
    questions = {e.get('_src') for e in evs if e.get('type') == 'question' and e.get('actor') == 'builder'}
    if any(e.get('type') == 'decision' and nextstep._admitted_actor(e) in {'thinker','operator'}
           and newer(e) and (e.get('rework') == 'yes' or e.get('ref') in questions) for e in evs):
        return False
    trigger = next((e for e in evs if nextstep._source(e) == step.trigger), {})
    drops = [e for e in evs if e.get('type') == 'window-dropped' and newer(e)]
    blocked = outcome.get('type') == 'build-blocked' or outcome.get('status') != 'DONE'
    return (trigger.get('type') == 'window-dropped' and newer(trigger)
            or trigger.get('type') in {'unblocked','spec-amended'} and newer(trigger) and (blocked or bool(drops)))


def prepare_fresh(step, evs, root, project, wt, runner):
    """Archive old work losslessly, then prove the conventional fresh checkout.

    The existing heal state journals partial preparation by governing trigger.
    Retry timing/exhaustion remains entirely in tick/attempts.
    """
    if not fresh_trigger(step, evs):
        return None
    import re
    import packet
    import packet_base
    import resources
    import fold
    import autodispatch
    item, branch = step.item, step.item.lower()
    if (not re.fullmatch(r'L-spec-\d+',item) or not isinstance(project,str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',project) or project in ('.','..')):
        raise ValueError('unsafe fresh recovery subject/project')
    conventional = root / 'worktrees' / project / branch
    if (wt != conventional or wt.is_symlink() or not wt.parent.resolve().is_relative_to(root.resolve())):
        raise ValueError('unsafe fresh recovery worktree path')
    repo = root / 'repos' / project
    def git(*args, cwd=repo, missing=False):
        result = runner(['git','-C',str(cwd),*args])
        if missing and result.get('code') == 1:
            return None
        if result.get('code',1):
            raise ValueError('fresh recovery: '+_cause(result))
        return result.get('stdout','').strip()
    configured = packet_base._project_main(project)
    main = git('symbolic-ref','--short','HEAD')
    if main != configured:
        raise ValueError('fresh recovery: symbolic HEAD disagrees with configured default')
    spec = fold.ROOT / 'content' / (item+'.md')
    effective = packet_base._effective_branch(repo,project,spec,item)
    if effective != main:
        raise ValueError(f'base {effective}: the Executor cuts this worktree by hand')
    pin = packet._effective_base(repo,project,spec,item)
    if git('rev-parse','--verify',f'refs/heads/{main}^{{commit}}') != pin:
        raise ValueError('fresh recovery: default moved during resolution')
    rows = _registrations(git('worktree','list','--porcelain'))
    registered = next((r for r in rows if Path(r['worktree']).resolve() == wt.resolve()),None)
    if registered and 'locked' in registered:
        raise ValueError('fresh recovery: worktree registration is locked')
    if os.path.lexists(wt) and (not registered or not wt.is_dir()):
        raise ValueError('fresh recovery: conventional path is occupied')
    if any(r.get('branch') == 'refs/heads/'+branch and Path(r['worktree']).resolve() != wt.resolve() for r in rows):
        raise ValueError('fresh recovery: subject branch is registered elsewhere')
    state_path = root / 'state/worktree-heal.json'
    state = _read_state(state_path)
    key = 'fresh '+item+' '+str(step.trigger)
    journal = state.get(key)
    if journal is not None and (not isinstance(journal,dict)
            or re.fullmatch(re.escape(branch)+r'-pre-[1-9][0-9]*',str(journal.get('archive',''))) is None
            or journal.get('phase') not in {'planned','archived','complete'}
            or any(re.fullmatch(r'[0-9a-f]{40}',str(journal.get(k,''))) is None for k in ('tip','base'))
            or type(journal.get('had_tree')) is not bool):
        raise ValueError('fresh recovery: invalid preparation journal')
    allowed = {'refs/heads/'+branch}
    if journal and journal['phase'] == 'planned':
        allowed.add('refs/heads/'+journal['archive'])
    if registered and registered.get('branch') not in allowed:
        raise ValueError('fresh recovery: conventional checkout branch mismatch')
    def save():
        state[key] = journal
        _write_state(state_path,state)
    def record(path):
        events = fold.read_events(use_cache=False)
        if not any(e.get('type') == 'resource-created' and e.get('subject') == item
                   and e.get('kind') == 'worktree' and e.get('ref') == str(path) for e in events):
            resources.record('worktree',str(path),lane='build',subject=item)
    def prove():
        if (git('rev-parse','HEAD',cwd=wt) != pin or git('symbolic-ref','--short','HEAD',cwd=wt) != branch):
            raise ValueError('fresh recovery: checkout HEAD/branch does not match creation base')
        entries = _registrations(git('worktree','list','--porcelain'))
        if not any(Path(r['worktree']).resolve() == wt.resolve() and r.get('branch') == 'refs/heads/'+branch
                   and r.get('HEAD') == pin for r in entries):
            raise ValueError('fresh recovery: fresh registration not proved')
    if journal is None:
        build = next((e for e in reversed(evs) if e.get('type') == 'build-done'), {})
        old_base = build.get('base_sha')
        if not old_base or not re.fullmatch(r'[0-9a-f]{40}',old_base):
            raise ValueError('fresh recovery: recorded base is unavailable')
        git('rev-parse','--verify',old_base+'^{commit}')
        tip = git('rev-parse','--verify','refs/heads/'+branch+'^{commit}')
        if git('rev-list','--count',old_base+'..'+tip) == '0':
            if not wt.is_dir():
                _repair(item,'rebuild',evs,root,project,wt,runner)
            if git('symbolic-ref','--short','HEAD',cwd=wt) != branch:
                raise ValueError('fresh recovery: reusable branch mismatch')
            git('merge','--ff-only',pin,cwd=wt)
            prove()
            return pin
        n = 1
        while True:
            archive = branch+'-pre-'+str(n)
            path = wt.with_name(archive)
            if not os.path.lexists(path) and git('show-ref','--verify','--quiet','refs/heads/'+archive,missing=True) is None:
                break
            n += 1
            if n > 1000:
                raise ValueError('fresh recovery: archival names exhausted')
        journal = dict(archive=archive,tip=tip,base=pin,phase='planned',had_tree=wt.is_dir())
        save()
    archive = journal['archive']; archived = wt.with_name(archive)
    if journal['phase'] == 'planned':
        existing = git('show-ref','--verify','--quiet','refs/heads/'+archive,missing=True)
        if existing is None:
            if git('rev-parse','refs/heads/'+branch) != journal['tip']:
                raise ValueError('fresh recovery: original branch moved')
            git('branch','-m',branch,archive)
        if git('rev-parse','refs/heads/'+archive) != journal['tip']:
            raise ValueError('fresh recovery: archival tip changed')
        if journal['had_tree']:
            if not archived.is_dir():
                git('worktree','move',str(wt),str(archived))
            if os.path.lexists(wt):
                raise ValueError('fresh recovery: conventional path remains after archive move')
            if git('rev-parse','HEAD',cwd=archived) != journal['tip']:
                raise ValueError('fresh recovery: archival tree not proved')
            record(archived)
        elif registered:
            # Only this proven-missing registration may be removed.
            git('worktree','remove',str(wt))
        journal['phase'] = 'archived'; save()
    if not wt.is_dir():
        if os.path.lexists(wt):
            raise ValueError('fresh recovery: conventional path remains occupied')
        journal['base'] = pin; save()
        existing = git('show-ref','--verify','--quiet','refs/heads/'+branch,missing=True)
        if existing is None:
            result = autodispatch.add_builder_worktree(repo,wt,branch,pin,runner=runner,reuse_branch=False)
            if result.get('code',1):
                raise ValueError('fresh recovery: '+_cause(result))
        else:
            if git('rev-parse','refs/heads/'+branch) != journal['base']:
                raise ValueError('fresh recovery: incomplete fresh branch changed')
            git('worktree','add',str(wt),branch)
    prove()
    record(wt)
    journal['phase'] = 'complete'; save()
    return pin
