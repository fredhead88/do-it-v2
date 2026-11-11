"""Cross-process admission and exact companion-ref ownership for managed candidates.

Per-invocation records are immutable in identity; cleanup never unlinks records or
supersession tombstones. The project lock orders admission, launch publication and
verdict acceptance. Auxiliary/base/member requests do not enter this protocol.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import uuid

import fold

POLL_SECS = 2
MAX_POLLS = 20
QUIET_POLLS = 3
ADMISSION_SECS = 600
_DEADLINE = ContextVar('window_cancel_deadline', default=None)
_NEXT_INVOCATION = ContextVar('window_cancel_replacement', default=None)
MAX_RUNS = 100  # gh's --limit bounds discovery to at most two API pages.
FIELDS = 'databaseId,headBranch,headSha,status,conclusion'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def directory(project):
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', project):
        raise ValueError('invalid cancellation project')
    path = fold.ROOT / 'state' / 'window-cancel' / project
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def paths(row):
    ident = row['invocation_id']
    if not re.fullmatch(r'[0-9a-f]{32}', ident):
        raise ValueError('invalid invocation id')
    root = directory(row['project'])
    return root / (ident + '.json'), root / (ident + '.tombstone')


@contextmanager
def lock(project):
    fd = os.open(directory(project) / 'admission.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _DEADLINE.get() is not None and time.monotonic() >= _DEADLINE.get():
                    raise RuntimeError('cancellation admission lock deadline exceeded')
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def start_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('invalid companion pid')
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot + ':' + stat[19]
    except FileNotFoundError:
        return None


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


def repository(gate, repo):
    remote = gate.git(repo, 'remote', 'get-url', 'origin', check=False).stdout.strip()
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:|ssh://git@github.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    # Local fixture remotes and legacy repositories can run; they cannot establish
    # ownership for a subsequent remote cancellation.
    return match[1] if match else ''


def related(old, window, specs):
    return old['window'] == window or bool(set(old['specs']) & set(specs))


def active(project, window, specs):
    rows = []
    for path in sorted(directory(project).glob('*.json')):
        row = json.loads(path.read_text())
        receipt, tombstone = paths(row)
        if receipt != path or row['project'] != project:
            raise ValueError('receipt identity mismatch')
        if related(row, window, specs) and row['status'] != 'confirmed' and (
                not row.get('finished_at') or tombstone.exists()):
            rows.append(row)
    return rows


def gh(row, *args):
    deadline = _DEADLINE.get()
    left = deadline - time.monotonic() if deadline is not None else 15
    if left <= 0:
        raise RuntimeError('cancellation deadline exceeded')
    proc = subprocess.run(['gh', 'run', *args, '-R', row['repository']],
                          capture_output=True, text=True, timeout=min(15, left))
    if proc.returncode:
        raise RuntimeError('gh cancellation operation failed')
    return json.loads(proc.stdout) if args[0] in ('list', 'view') and proc.stdout.strip() else None


def owned(row, run):
    if (not isinstance(run, dict) or run.get('headBranch') != row['ref'] or run.get('headSha') != row['sha']
            or type(run.get('databaseId')) is not int or run['databaseId'] <= 0
            or run.get('status') not in ('queued', 'in_progress', 'waiting', 'pending', 'requested', 'completed')):
        raise ValueError('run ownership or status unestablished')
    return str(run['databaseId'])


def holder_record(window):
    return dict(window=window, pid=os.getpid(), host=socket.gethostname(),
                start_identity=start_identity(os.getpid()))


def validate_companion(row):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', row['repository'])
            or not re.fullmatch(r'[0-9a-f]{40}', row['sha'])
            or row['holder']['host'] != socket.gethostname()
            or not row.get('start_identity')
            or row['ref'] != f"ci-verify/{row['sha'][:12]}-{row['pid']}"):
        raise ValueError('companion ownership unestablished')
    identity = start_identity(row['pid'])
    if identity is not None and identity != row['start_identity']:
        raise ValueError('stale companion pid')


class RemoteCompanion:
    """The existing stop boundary, for an identified companion owned by another gate."""
    def __init__(self, row):
        self.row = row
        self.args = ['owned-companion']

    @property
    def pid(self):
        validate_companion(self.row)
        return self.row['pid']

    def communicate(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 30)
        while alive(self.row['pid']):
            validate_companion(self.row)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.02)
        return '', ''


def generation(known):
    entries = known.get('entries', []) if known else []
    return dict(base=known.get('base_sha') if known else None,
                members={s: sha for s, branch, sha in entries})


def newer(gate, repo, old, entries, specs):
    before = old['generation']
    after = dict(base=gate.git(repo, 'rev-parse', 'refs/heads/' + gate.main_branch(None)).stdout.strip(),
                 members={s: sha for s, branch, sha in entries})
    shared = set(old['specs']) & set(specs)
    if not shared or not before['members'] or before == after:
        return False
    pairs = [(before['base'], after['base'])] + [(before['members'][s], after['members'][s]) for s in shared]
    return all(re.fullmatch(r'[0-9a-f]{40}', a or '') and re.fullmatch(r'[0-9a-f]{40}', b or '')
               and not gate.git(repo, 'merge-base', '--is-ancestor', a, b, check=False).returncode
               for a, b in pairs)


def lock_released(project):
    path = fold.ROOT / 'state' / f'window-chain.{project}.lock'
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(fd)


@contextmanager
def try_supersede(refusal, window, project, specs, events):
    """Keep the lifetime lock for one retry, only after proven newer owned work stops."""
    if refusal is None:
        yield None
        return
    import window_gate as gate
    reason = 'supersession ownership unestablished'
    admitted = False
    deadline = time.monotonic() + ADMISSION_SECS
    token = _DEADLINE.set(deadline)
    try:
        metadata = json.loads((fold.ROOT / 'state' / f'window-chain.{project}.lock').read_text())
        pid = metadata['pid']
        if (metadata['host'] != socket.gethostname() or pid != refusal['holder_pid']
                or metadata['window'] != refusal['holder_window'] or not alive(pid)
                or start_identity(pid) != metadata['start_identity']):
            raise ValueError('holder identity mismatch')
        repo = fold.ROOT / 'repos' / project
        names = [s.partition(':')[0] for s in specs]
        with lock(project):
            rows = [r for r in active(project, metadata['window'], [])
                    if r['window'] == metadata['window'] and r.get('holder') == metadata]
            if len(rows) != 1:
                raise ValueError('holder receipt missing or ambiguous')
            row = rows[0]
            # Resolve only after the cheap receipt check: ordinary contention has no side effects.
            entries = gate.resolve_entries(repo, specs, events)
            if not newer(gate, repo, row, entries, names):
                raise ValueError('candidate is not a newer overlapping generation')
            validate_companion(row)
            runs = gh(row, 'list', '--branch', row['ref'], '--limit', str(MAX_RUNS + 1), '--json', FIELDS)
            if not isinstance(runs, list) or not runs or len(runs) > MAX_RUNS:
                raise ValueError('holder runs missing or discovery unbounded')
            ids = {owned(row, run) for run in runs}
            row['run_ids'] = sorted(set(row['run_ids']) | ids, key=int)
            for rid in row['run_ids']:
                if owned(row, gh(row, 'view', rid, '--json', FIELDS)) != rid:
                    raise ValueError('holder run identity mismatch')
            # Recheck immediately before the tombstone and process action.
            if (json.loads((fold.ROOT / 'state' / f'window-chain.{project}.lock').read_text()) != metadata
                    or start_identity(pid) != metadata['start_identity'] or not alive(pid)):
                raise ValueError('holder changed before cancellation')
            atomic(paths(row)[0], row)
            replacement = uuid.uuid4().hex
            cancel_owned(row, replacement)
        while time.monotonic() < deadline:
            if lock_released(project):
                admitted = True
                break
            time.sleep(POLL_SECS)
        if not admitted:
            raise RuntimeError('holder lock did not release within ten minutes')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        reason = str(exc)[:160]
    finally:
        _DEADLINE.reset(token)
    if admitted:
        with gate.chain_lock(window, project) as retried:
            token = _NEXT_INVOCATION.set(replacement)
            try:
                yield retried
            finally:
                _NEXT_INVOCATION.reset(token)
    else:
        # Keep the existing refusal shape; the reason is separate diagnostic evidence.
        gate.ev('window-supersession-refused', window, window=window, why=reason, project=project)
        yield refusal


def cancel_owned(row, replacement):
    path, tombstone = paths(row)
    validate_companion(row)
    # Publish before stopping: even a late green/partial failure is unusable.
    row.update(replacement_invocation=replacement, requested_at=stamp(), status='requested')
    atomic(tombstone, dict(invocation_id=row['invocation_id'], replacement_invocation=replacement,
                           requested_at=row['requested_at']))
    atomic(path, row)
    import window_gate
    if _DEADLINE.get() is not None and _DEADLINE.get() - time.monotonic() < 65:
        raise RuntimeError('insufficient cancellation time remaining')
    if start_identity(row['pid']) is not None and alive(row['pid']):
        window_gate.stop_verification(RemoteCompanion(row))
    seen, cancelled, quiet = set(row['run_ids']), [], 0
    for _ in range(MAX_POLLS):
        runs = gh(row, 'list', '--branch', row['ref'], '--limit', str(MAX_RUNS + 1), '--json', FIELDS)
        if not isinstance(runs, list) or len(runs) > MAX_RUNS:
            raise ValueError('run discovery exceeded bound')
        discovered = {owned(row, run) for run in runs}
        if len(seen | discovered) > MAX_RUNS:
            raise ValueError('run discovery exceeded bound')
        quiet = quiet + 1 if discovered <= seen else 0
        seen.update(discovered)
        row['run_ids'] = sorted(seen, key=int)
        atomic(path, row)  # ownership survives a crash during cancellation
        complete = True
        for rid in sorted(seen, key=int):
            run = gh(row, 'view', rid, '--json', FIELDS)
            if owned(row, run) != rid:
                raise ValueError('run id changed')
            if run['status'] != 'completed':
                if rid not in cancelled:
                    gh(row, 'cancel', rid)
                    cancelled.append(rid)
                run = gh(row, 'view', rid, '--json', FIELDS)
                if owned(row, run) != rid:
                    raise ValueError('run id changed')
            complete = complete and run['status'] == 'completed'
        if complete and quiet >= QUIET_POLLS:
            row.update(confirmed_at=stamp(), status='confirmed')
            atomic(path, row)
            return cancelled
        time.sleep(POLL_SECS)
    raise RuntimeError('cancellation confirmation unavailable within bound')


def _supersede(project, window, specs, tree, invocation):
    cancelled, confirmed = [], []
    try:
        for row in active(project, window, specs):
            # Distinct scopes on one explicit tree are peers. The admission loop
            # waits for them, then re-evaluates cached coverage instead of killing.
            if row['tree'] == tree and any(row[k] != invocation[k] for k in ('required', 'quick', 'suite')):
                return dict(status='undetermined', cancelled=cancelled, confirmed=confirmed, error='peer still active')
            cancelled.extend(cancel_owned(row, invocation['invocation_id']))
            confirmed.append(row['invocation_id'])
        return dict(status='confirmed' if confirmed else 'none-owned', cancelled=cancelled,
                    confirmed=confirmed, error='')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
        return dict(status='undetermined', cancelled=cancelled, confirmed=confirmed,
                    error='owned cancellation could not be confirmed')


def supersede(project, window, specs, tree, invocation):
    """Cancel only exact-ref work from older related managed candidate invocations."""
    with lock(project):
        return _supersede(project, window, specs, tree, invocation)


class Admission:
    def __init__(self, gate, repo, sha, tree, required, window, quick, suite, known):
        self.row = dict(project=known['project'] if known else repo.name,
                        repository=repository(gate, repo), window=window,
                        specs=sorted(set(known['specs'])) if known else [], sha=sha, tree=tree,
                        required=sorted(required), quick=sorted(quick), suite=suite,
                        invocation_id=_NEXT_INVOCATION.get() or uuid.uuid4().hex, ref='', run_ids=[], replacement_invocation=None,
                        requested_at=None, confirmed_at=None, status='admitted',
                        holder=holder_record(window), generation=generation(known))
        _NEXT_INVOCATION.set(None)
        self.error = ''
        self.waited = False
        self.held = None

    def enter(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.held = lock(self.row['project'])
            self.held.__enter__()
            rows = active(self.row['project'], self.row['window'], self.row['specs'])
            peers = [r for r in rows if r['tree'] == self.row['tree'] and
                     any(r[k] != self.row[k] for k in ('required', 'quick', 'suite'))]
            if not peers:
                result = _supersede(self.row['project'], self.row['window'], self.row['specs'], self.row['tree'], self.row)
                self.error = result['error']
                return
            self.release()
            self.waited = True
            if time.monotonic() >= deadline:
                self.error = 'nonduplicate managed peer did not finish'
                return
            time.sleep(0.1)

    def bind(self, proc):
        self.row.update(pid=proc.pid, start_identity=start_identity(proc.pid),
                        ref=f"ci-verify/{self.row['sha'][:12]}-{proc.pid}", status='running')
        atomic(paths(self.row)[0], self.row)
        self.release()

    def release(self):
        if self.held is not None:
            held, self.held = self.held, None
            held.__exit__(None, None, None)

    def finish(self, result):
        self.release()
        with lock(self.row['project']):
            path, tombstone = paths(self.row)
            if tombstone.exists():
                return dict(state='undetermined', nodes=[], url='', doc={}, superseded=True,
                            why='verification superseded; no chargeable outcome')
            if path.exists():
                self.row.update(status='finished', finished_at=stamp())
                atomic(path, self.row)
            return result
