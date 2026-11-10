"""Builder timeout preservation and resume (L-spec-2284, L-adr-0479 D11).

A builder that reaches its time limit keeps its work: after the writer can no
longer change the worktree, the wrapper commits every dirty path on the
builder's own branch (hooks off, wrapper only), flags paths outside the
effective grant, and appends `build-timeout`. The next packet names that
checkpoint. Nothing here resets, removes, recuts or touches main.
"""
import os
import pathlib
import signal
import subprocess
import time

TIMEOUT_CONFLICT = 'timeout-resume conflict: Executor authorization required'
TERM_GRACE = 5      # seconds between SIGTERM and SIGKILL of a builder process group
SEAT_GRACE = 30     # seconds a claimed seat gets to stamp completion after the marker
_clock, _sleep = time.monotonic, time.sleep
GIT_OPERATIONS = ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD',
                  'REVERT_HEAD', 'BISECT_LOG', 'index.lock')


class PreserveError(Exception):
    """A bounded, actionable reason the work could not be saved."""


def applicable_timeout(events, subject):
    """Newest subject timeout after the newest spec-written, unless a later DONE superseded it."""
    found = None
    for e in events:
        if e.get('subject') != subject:
            continue
        t = e.get('type')
        if t == 'spec-written' or (t == 'build-done' and e.get('status') == 'DONE'):
            found = None
        elif t == 'build-timeout':
            found = e
    return found


def packet_lines(events, subject):
    """The resume line and flagged-path instruction, or nothing."""
    e = applicable_timeout(events, subject)
    if not e:
        return []
    n = len(e.get('wip_commits') or [])
    out = ['', f"Resume from {e.get('last_commit')} on {e.get('branch')}: {n} WIP commits from the timed-out build"]
    flagged = e.get('uncommitted_out_of_scope') or []
    if flagged:
        out += ['These saved paths lie outside the effective grant and are already committed; '
                'remove each from the branch or justify it in the card before hand-in:',
                *[f'   {p}' for p in flagged]]
    return out


def git(cwd, *args, timeout=60):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=timeout)


def _git_ok(cwd, *args):
    r = git(cwd, *args)
    if r.returncode:
        raise PreserveError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-120:]}")
    return r.stdout


def dirty_paths(cwd):
    """Every staged, unstaged and nonignored untracked path, rename sources included."""
    r = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise PreserveError('git status failed: ' + r.stderr.strip()[-120:])
    parts, paths, i = r.stdout.split('\0'), [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in 'RC' or entry[1] in 'RC':
            if i < len(parts) and parts[i]:
                paths.append(parts[i])
            i += 1
    return list(dict.fromkeys(paths))


def stop_group(pgid, grace=None):
    """SIGTERM the process group, SIGKILL after the grace. True only when no member remains."""
    grace = TERM_GRACE if grace is None else grace

    def alive():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait(until):
        while alive() and _clock() < until:
            _sleep(0.05)
        return not alive()
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not wait(_clock() + grace):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wait(_clock() + 2)
    return not alive()


def run_group(cmd, packet, cwd, env, timeout):
    """subprocess.run in its own session and process group (the seam tests intercept). On a timeout the
    group is terminated, escalating to SIGKILL, and TimeoutExpired carries `.quiescent` (True only when
    every member is gone; unknown group means False). The child announces its pid, which is its pgid."""
    read, write = os.pipe()
    os.set_blocking(read, False)

    def announce():
        os.write(write, str(os.getpid()).encode())
    try:
        return subprocess.run(cmd, input=packet, capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=timeout, start_new_session=True, preexec_fn=announce)
    except subprocess.TimeoutExpired as exc:
        try:
            pgid = int(os.read(read, 32) or 0) or None
        except (OSError, ValueError):
            pgid = None
        exc.quiescent = bool(pgid) and stop_group(pgid)
        raise
    finally:
        os.close(read)
        os.close(write)


def seat_quiesce(seat_dir, spawn, grace=None):
    """Write the cancellation marker, then wait for the pane's completion stamp. A marker alone proves nothing."""
    grace = SEAT_GRACE if grace is None else grace
    seat_dir = pathlib.Path(seat_dir)
    marker = seat_dir / f'{spawn}.cancelled'
    if not marker.exists():
        marker.write_text('timeout-preservation: stop requested by the dispatch wrapper\n')
    deadline = _clock() + grace
    while True:
        if (seat_dir / f'{spawn}.result.json').is_file() or (seat_dir / f'{spawn}.meta.json').is_file():
            marker.unlink(missing_ok=True)
            return True
        if _clock() >= deadline:
            return False
        _sleep(0.5)


def _guard(cwd, subject, repo):
    """Refuse a missing worktree, detached HEAD, wrong branch, shared checkout or active git operation."""
    cwd = pathlib.Path(cwd)
    if not cwd.is_dir():
        raise PreserveError('worktree missing')
    top = git(cwd, 'rev-parse', '--show-toplevel')
    if top.returncode or pathlib.Path(top.stdout.strip()).resolve() != cwd.resolve():
        raise PreserveError('cwd is not a worktree root')
    if repo and pathlib.Path(repo).resolve() == cwd.resolve():
        raise PreserveError('refusing the shared main checkout')
    branch = git(cwd, 'symbolic-ref', '--short', 'HEAD')
    if branch.returncode:
        raise PreserveError('detached HEAD')
    name = branch.stdout.strip()
    if name != subject.lower() or name in ('main', 'master'):
        raise PreserveError(f'branch {name!r} is not the spec branch')
    for part in GIT_OPERATIONS:
        state = git(cwd, 'rev-parse', '--git-path', part)
        path = pathlib.Path(state.stdout.strip())
        if not path.is_absolute():
            path = cwd / path
        if state.returncode or path.exists():
            raise PreserveError('git operation in progress: ' + part)
    return name


def head_of(cwd):
    r = git(cwd, 'rev-parse', '--verify', 'HEAD')
    return r.stdout.strip() if r.returncode == 0 else None


def preserve(cwd, subject, start_head, grant, repo=None):
    """Commit dirty paths as `wip(<subject>): timeout`; returns (branch, last_commit, wip_commits, out_of_scope)."""
    branch = _guard(cwd, subject, repo)
    if not start_head:
        raise PreserveError('attempt start commit unknown')
    paths = dirty_paths(cwd)
    flagged = []
    if paths:
        import merge_gate
        flagged = [p for p in paths if grant is None or not merge_gate.granted(p, grant)]
        _git_ok(cwd, 'add', '-A')
        who = [] if git(cwd, 'config', 'user.email').stdout.strip() else \
            ['-c', 'user.name=DO-IT timeout', '-c', 'user.email=timeout@do-it.invalid']
        _git_ok(cwd, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *who,
                'commit', '--no-verify', '-q', '-m', f'wip({subject}): timeout')
        if dirty_paths(cwd):
            raise PreserveError('worktree not clean after the checkpoint commit')
    last = head_of(cwd)
    if not last:
        raise PreserveError('HEAD unresolved')
    rows = _git_ok(cwd, 'log', '--reverse', '--format=%H%x00%s', f'{start_head}..HEAD').splitlines()
    wip = [r.split('\0')[0] for r in rows if r.split('\0', 1)[1].startswith(f'wip({subject}):')]
    return branch, last, wip, flagged


def on_timeout(runtime, a, cwd, ledger, base, spawn, start_head, exc, seat):
    """Save the timed-out builder's work and append build-timeout. None on success, else a bounded diagnostic."""
    try:
        if seat:
            quiet = seat_quiesce(runtime.SEAT, spawn)
        else:
            quiet = getattr(exc, 'quiescent', False)
        if not quiet:
            return 'timeout-preservation:writer-not-quiescent'
        import fold
        import effective_writes
        import merge_gate
        events = fold.read_events()
        try:
            grant = effective_writes.effective_grant(a.subject, runtime.spec_path(a.subject).read_text(), events)
        except (merge_gate.Undetermined, OSError, ValueError):
            grant = None        # undetermined is never clean: every saved path is flagged
        repo = runtime.ROOT / 'repos' / (a.project or '')
        branch, last, wip, flagged = preserve(cwd, a.subject, start_head, grant, repo)
        refused = runtime.emit(ledger, base, 'build-timeout', strict=True, branch=branch, last_commit=last,
                               wip_commits=wip, uncommitted_out_of_scope=flagged)
        if refused:
            return 'timeout-preservation:event-refused: ' + str(refused)[:120]
        return None
    except PreserveError as e:
        return 'timeout-preservation:' + str(e)[:160]
    except Exception as e:      # the terminal failure must stay visible, never masked
        return f'timeout-preservation:{type(e).__name__}: {str(e)[:100]}'
