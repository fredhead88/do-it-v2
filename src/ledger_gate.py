#!/usr/bin/env python3
"""Read-only ledger equality and bookkeeping meter (stdlib only).

compare(before_sha, subjects, *, root=None, repo=None, now=None) returns a JSON
object with source identities, captured dataset digest, fixed UTC instant,
auxiliary_context, requested/selected/completed counts, frozen subject IDs,
ineligible rows, per-surface results, differences, uncomparable and success.
Optional current=DIR permits an explicitly identified local source materialization.
The default current source includes uncommitted tracked source edits.

meter(start, end, *, samples=DEFAULT_SAMPLES) returns the requested window,
validated sample count, CPU/wall sums, unrounded rate and signed baseline change,
bar decision, coverage/gap/overlap figures, complete_window/day and reasons.
Sample ts is its interval END; boundaries select inclusively. Sparse by objects
contribute zero bookkeeping CPU and their entire wall interval. All selected
rows count; malformed input anywhere fails rather than disappearing silently.

CLI stdout is local JSON; exit 0 requires complete equal answers or a complete
meter window at/below the bar. All inability to measure/compare exits 1. Neither
API modifies the source ledger. Evaluation runs bounded local Python children,
never a resident process. Private bootstrap also controls descendant clocks and
records forbidden filesystem access even when revision code catches the error.
"""
import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

DEFAULT_SAMPLES = '/var/tmp/push/compute/samples.jsonl'
SUBJECT_SURFACES = ('brief', 'events', 'folded')
SURFACES = ('brief', 'events', 'states', 'board', 'folded', 'counts')
LIMIT = 64 * 1024 * 1024
AUXILIARY = {
    'DOIT_PROJECT': None, 'DOIT_READ_CACHE': '0', 'DOIT_K': '0',
    'DOIT_TICK_MIN': '5', 'DOIT_SEAT_CLAIM_SEC': '300',
    'home': 'private', 'config': 'private; backup.toml absent',
    'sessions': 'empty private', 'v4': 'empty private ledger/inbox/staging',
    'absent': ['backup-state.json', 'state/' + '-'.join(('master', 'frozen', 'for', 'codex')),
               'state/build-priority', 'seat', 'relay.heartbeat', 'models.toml'],
    'clock': 'fixed UTC datetime.now/utcnow/today and time.time in all Python children',
    'locale': 'C.UTF-8', 'encoding': 'utf-8',
    'optional_backend_version': 'claude --version pinned unavailable; no backend process',
    'registrations': 'captured project names replayed as empty private directories',
}


def utc(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be an aware ISO string')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('invalid ISO timestamp') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp must include a timezone')
    return result.astimezone(dt.timezone.utc)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')


def manifest(root):
    """Metadata plus bytes, without following aliases; does not change source."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(directory) / name
            info = p.lstat()
            row = [info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            if stat.S_ISREG(info.st_mode):
                row.append(digest(p.read_bytes()))
            elif stat.S_ISLNK(info.st_mode):
                row.append(os.readlink(p))
            result[str(p.relative_to(root))] = row
    return result


def capture(root):
    """Capture only ledger JSONL/indexes and inert Markdown content, once.

    Virtual-source names/line provenance are provided by the existing reader.
    Physical segments and live tails are retained, so each revision also sees
    exactly the captured virtual-source representation. Two metadata/content
    inventories bracket the read; detected instability is an explicit failure.
    No .env, configuration, auxiliary state or credential file is copied.
    """
    import ledger_files
    from relay import registered_projects
    root = Path(root)
    if root.is_symlink():
        raise ValueError('source root symlink is not supported')
    for name in ('events', 'content'):
        if not (root / name).is_dir() or (root / name).is_symlink():
            raise ValueError('missing or aliased required ' + name)
    before = {name: manifest(root / name) for name in ('events', 'content')}
    projects = sorted(registered_projects(root))
    files = {}
    for name, tree in before.items():
        for relative, meta in tree.items():
            p = root / name / relative
            if name == 'events' and stat.S_ISLNK(meta[0]):
                raise ValueError('ledger symlink is not supported')
            wanted = (p.suffix == '.md' if name == 'content'
                      else p.suffix == '.jsonl' or p.name.endswith('.idx.json'))
            if wanted and stat.S_ISREG(meta[0]):
                # Never traverse a symlink directory to acquire a file.
                if any(parent.is_symlink() for parent in p.parents if parent != root.parent):
                    raise ValueError('aliased capture path')
                files[f'{name}/{relative}'] = p.read_bytes()
    if ledger_files.problems(root / 'events'):
        raise ValueError('invalid or incomplete virtual ledger sources')
    sources = dict(ledger_files.iter_sources(root / 'events'))
    if not sources:
        raise ValueError('missing ledger events')
    after = {name: manifest(root / name) for name in ('events', 'content')}
    if before != after or projects != sorted(registered_projects(root)):
        raise ValueError('unstable source capture')
    return {'files': files, 'sources': sources,
            'registered_projects': projects,
            'identity': digest(encoded({'files': {k: digest(v) for k, v in sorted(files.items())},
                                       'registered_projects': projects})),
            'content_policy': 'regular non-symlink Markdown files; no config/env files'}


def select_subjects(snapshot, count):
    if type(count) is not int or count <= 0:
        raise ValueError('subjects must be a positive integer')
    ranked, ineligible = {}, []
    for lines in snapshot['sources'].values():
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                raise ValueError('malformed captured ledger row') from None
            if not isinstance(event, dict):
                raise ValueError('captured event must be an object')
            subject = event.get('subject')
            if not isinstance(subject, str) or not re.fullmatch(r'L-(spec|charter)-[0-9]+', subject):
                continue
            timestamp = utc(event.get('ts'))
            ranked[subject] = max(ranked.get(subject, timestamp), timestamp)
    eligible = []
    for subject in ranked:
        if f'content/{subject}.md' not in snapshot['files']:
            ineligible.append({'subject': subject, 'reason': 'missing regular non-symlink content'})
        else:
            eligible.append(subject)
    eligible.sort(key=lambda subject: (-ranked[subject].timestamp(), subject))
    if len(eligible) < count:
        return eligible, sorted(ineligible, key=lambda row: row['subject'])
    return eligible[:count], sorted(ineligible, key=lambda row: row['subject'])


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                       env=env, timeout=30)
    if p.returncode:
        raise ValueError('local revision unavailable or materialization failed')
    return p.stdout


def materialize(repo, revision, destination):
    """Export trusted LOCAL revision files, refusing traversal/alias members."""
    data = git(repo, 'archive', '--format=tar', revision)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('unsafe revision path')
            target = destination / path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError('unsupported revision alias or special file')


def current_source(repo, destination):
    """Export tracked files from the current worktree, including local edits."""
    destination.mkdir(parents=True)
    identities = {}
    for raw in git(repo, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        relative = Path(name)
        if any(part == '.env' or part.startswith('.env.') for part in relative.parts):
            continue
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe tracked path')
        source = repo / name
        if not source.exists():
            identities[name] = None
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError('unsupported tracked alias')
        content = source.read_bytes()
        identities[name] = digest(content)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return digest(encoded(identities))


def run_child(argv, *, cwd, env, timeout=90):
    """Bound elapsed time and stored output, including all descendants."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=output, stderr=errors,
                                   start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('revision execution timeout')
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > LIMIT:
                    raise RuntimeError('revision output exceeded limit')
                time.sleep(.01)
            if process.returncode:
                raise RuntimeError('revision child nonzero exit')
            if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > LIMIT:
                raise RuntimeError('revision output exceeded limit')
            output.seek(0)
            try:
                return json.load(output)
            except (ValueError, UnicodeError):
                raise RuntimeError('malformed revision output') from None
        finally:
            # Kill descendants too, even if the immediate child already ended.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def activate():
    """sitecustomize bootstrap for evaluator and each Python descendant.

    These checks isolate trusted revision Python code; they are not an OS sandbox
    for hostile code. Syscall wrappers include metadata reads (not audit events).
    Violations survive caught exceptions through a shared append-only scratch log.
    """
    import datetime
    import builtins
    instant = utc(os.environ['DOIT_NOW'])
    original_datetime = datetime.datetime

    class FrozenDatetime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            value = original_datetime.fromtimestamp(instant.timestamp(), tz=tz)
            return cls.fromisoformat(value.isoformat())

        @classmethod
        def utcnow(cls):
            return cls.now(datetime.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    datetime.datetime = FrozenDatetime
    time.time = lambda: instant.timestamp()
    if hasattr(time, 'time_ns'):
        time.time_ns = lambda: int(instant.timestamp() * 1_000_000_000)
    scratch = os.environ['LEDGER_GATE_SPACE']
    revision = os.environ['LEDGER_GATE_REVISION']
    harness = str(Path(__file__).parent)
    allowed = (scratch, revision, harness, sys.base_prefix + '/lib', '/usr/lib',
               '/usr/local/lib', '/usr/share/locale', '/usr/share/locale-langpack',
               '/lib', '/lib64')
    raw_open = builtins.open
    raw_realpath = os.path.realpath
    log = os.environ['LEDGER_GATE_VIOLATIONS']
    inside = False

    def violation(kind, path=None, write=False):
        nonlocal inside
        previous = inside
        inside = True
        try:
            with raw_open(log, 'a') as stream:
                stream.write(json.dumps({'kind': kind, 'path': path, 'write': write}) + '\n')
        finally:
            inside = previous
        raise PermissionError('ledger gate isolation: ' + kind)

    def check(path, write=False, metadata=False):
        nonlocal inside
        if inside or isinstance(path, int) or path is None:
            return
        inside = True
        try:
            path = os.path.abspath(os.fsdecode(path))
            real = raw_realpath(path)
            permitted = any(real == prefix or real.startswith(prefix + '/') for prefix in allowed)
            if metadata:
                permitted = permitted or any(prefix.startswith(real.rstrip('/') + '/') for prefix in allowed)
            if real in ('/dev/null', '/etc/localtime') and not write:
                permitted = True
            if write:
                permitted = real == scratch or real.startswith(scratch + '/')
            if not permitted:
                violation('forbidden filesystem access', path, write)
        finally:
            inside = False

    def audit(event, args):
        if event == 'open':
            flags = args[2] if len(args) > 2 else 0
            check(args[0], bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        elif event in ('os.listdir', 'os.scandir', 'os.chdir'):
            check(args[0])
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime'):
            check(args[0], write=True)
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            check(args[0], write=True)
            check(args[1], write=True)
        elif event.startswith('socket.'):
            violation('network forbidden')
        elif event == 'subprocess.Popen':
            executable, argv, cwd, env = args
            # dispatch's import-time optional version probe catches OSError.
            # Keep that executable absent in the private runtime; never launch
            # a backend even if the invoking machine has it installed.
            if list(argv) == ['claude', '--version']:
                raise FileNotFoundError('optional backend executable absent in private context')
            if Path(executable).name not in ('python', 'python3', Path(sys.executable).name):
                violation('only Python answer children allowed')
            if any(flag in argv for flag in ('-S', '-I', '-E')):
                violation('child bootstrap disabled')
            if env is not None and (env.get('DOIT_ROOT') != os.environ['DOIT_ROOT'] or
                                    env.get('PYTHONPATH') != os.environ['PYTHONPATH']):
                violation('child environment changed')

    for name in ('stat', 'lstat', 'access', 'readlink'):
        original = getattr(os, name)
        def guarded(path, *args, _original=original, **kwargs):
            check(path, metadata=True)
            return _original(path, *args, **kwargs)
        setattr(os, name, guarded)
    sys.addaudithook(audit)


def _cli(script, args):
    output = io.StringIO()
    old = sys.argv
    try:
        sys.argv = [str(script), *args]
        with contextlib.redirect_stdout(output):
            try:
                runpy.run_path(str(script), run_name='__main__')
            except SystemExit as exc:
                if exc.code not in (0, None):
                    raise RuntimeError('answer CLI failed') from None
    finally:
        sys.argv = old
    return output.getvalue()


def _evaluate_worker(revision, ids):
    # Imported only in a bootstrapped private child, never against the live root.
    sys.path.insert(0, str(Path(revision) / 'src'))
    import fold
    import brief
    signature = inspect.signature(fold.read_events)
    kwargs = {'with_reaped': True} if 'with_reaped' in signature.parameters else {}
    history = fold.read_events(**kwargs)  # Internal TypeError is never retried.
    default = fold.read_events()
    specs, charters, _, _ = fold.fold(default)
    result = {'brief': {}, 'events': {}, 'folded': {},
              'states': _cli(Path(revision) / 'src/fold.py', ['states']),
              'board': _cli(Path(revision) / 'src/fold.py', []),
              'counts': dict(collections.Counter(e['type'] for e in history))}
    for subject in ids:
        record = {**specs, **charters}.get(subject)
        if record is None:
            raise ValueError('selected subject has no folded record: ' + subject)
        result['folded'][subject] = {k: v for k, v in record.items() if k != 'evs'}
        result['brief'][subject] = {
            'rendered': _cli(Path(revision) / 'src/brief.py', [subject]),
            'dossier': brief.dossier(subject, default, fold.ROOT, utc(os.environ['DOIT_NOW']))}
        display = _cli(Path(revision) / 'src/fold.py', ['events', subject])
        result['events'][subject] = {
            'history': [e for e in history if e.get('subject') == subject],
            'display': [json.loads(line) for line in display.splitlines()]}
        if not result['events'][subject]['history']:
            raise ValueError('missing selected subject history: ' + subject)
    return result


def evaluate(revision, snapshot, ids, now, workspace, *, timeout=90):
    """Reset the SAME pathname before either revision or repeat evaluation."""
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, mode=0o700)
    root = workspace / 'root'
    for folder in ('events', 'content'):
        (root / folder).mkdir(parents=True)
    for project in snapshot['registered_projects']:
        (root / 'repos' / project).mkdir(parents=True)
    for relative, data in snapshot['files'].items():
        p = root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for relative in ('home/.claude/sessions', 'home/.config/do-it', 'sessions',
                     'v4/ledger', 'v4/inbox', 'v4/staging', 'tmp', 'cwd', 'bootstrap'):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    bootstrap = workspace / 'bootstrap'
    shutil.copyfile(__file__, bootstrap / 'gate_runtime.py')
    (bootstrap / 'sitecustomize.py').write_text('from gate_runtime import activate\nactivate()\n')
    (bootstrap / 'worker.py').write_text(
        'import json,sys\nfrom gate_runtime import _evaluate_worker\n'
        'print(json.dumps(_evaluate_worker(sys.argv[1], json.loads(sys.argv[2])), '
        'sort_keys=True, ensure_ascii=False, allow_nan=False))\n')
    env = {'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
           'HOME': str(workspace / 'home'), 'XDG_CONFIG_HOME': str(workspace / 'home/.config'),
           'TMPDIR': str(workspace / 'tmp'), 'DOIT_ROOT': str(root), 'DOIT_NOW': now,
           'DOIT_READ_CACHE': '0', 'DOIT_SESSIONS_DIR': str(workspace / 'sessions'),
           'DOIT_K': '0', 'DOIT_TICK_MIN': '5', 'DOIT_SEAT_CLAIM_SEC': '300',
           'V4_LEDGER_DIR': str(workspace / 'v4/ledger'),
           'V4_INBOX_DIR': str(workspace / 'v4/inbox'),
           'V4_STAGING_DIR': str(workspace / 'v4/staging'),
           'PYTHONPATH': str(bootstrap) + os.pathsep + str(revision / 'src'),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
           'PYTHONUTF8': '1', 'TZ': 'UTC', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'LEDGER_GATE_SPACE': str(workspace), 'LEDGER_GATE_REVISION': str(revision),
           'LEDGER_GATE_VIOLATIONS': str(workspace / 'violations.jsonl')}
    try:
        result = run_child([sys.executable, str(bootstrap / 'worker.py'), str(revision),
                            json.dumps(ids)], cwd=workspace / 'cwd', env=env, timeout=timeout)
    finally:
        violations = workspace / 'violations.jsonl'
        if violations.exists() and violations.stat().st_size:
            raise RuntimeError('revision isolation violation (including answer descendants)')
    validate_answers(result, ids)
    return result


def validate_answers(result, ids):
    if not isinstance(result, dict) or set(result) != set(SURFACES):
        raise ValueError('incomplete surface comparison')
    for surface in SUBJECT_SURFACES:
        if not isinstance(result[surface], dict) or set(result[surface]) != set(ids):
            raise ValueError('partial selected subject comparison: ' + surface)
    for surface in ('board', 'states'):
        if not isinstance(result[surface], str) or not result[surface]:
            raise ValueError('missing rendered surface: ' + surface)
    if not isinstance(result['counts'], dict) or not result['counts']:
        raise ValueError('missing full history counts')
    for subject in ids:
        brief = result['brief'][subject]
        events = result['events'][subject]
        if (not isinstance(brief, dict) or set(brief) != {'rendered', 'dossier'}
                or not isinstance(brief['rendered'], str) or not brief['rendered']
                or not isinstance(brief['dossier'], dict) or not brief['dossier']):
            raise ValueError('missing brief/dossier')
        if (not isinstance(events, dict) or set(events) != {'history', 'display'}
                or not isinstance(events['history'], list) or not events['history']
                or not isinstance(events['display'], list)):
            raise ValueError('missing event history/presentation')
        if not isinstance(result['folded'][subject], dict) or not result['folded'][subject]:
            raise ValueError('missing folded record')


def differences(before, current):
    rows = []
    for surface in sorted(set(before) | set(current)):
        if surface in SUBJECT_SURFACES:
            left, right = before.get(surface, {}), current.get(surface, {})
            for subject in sorted(set(left) | set(right)):
                if left.get(subject) != right.get(subject):
                    rows.append({'surface': surface, 'subject': subject})
        elif before.get(surface) != current.get(surface):
            rows.append({'surface': surface, 'subject': None})
    return rows


def compare(before_sha, subjects, *, root=None, repo=None, now=None, current=None):
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    root = Path(root or os.environ.get('DOIT_ROOT') or Path.home() / '.do-it')
    result = {'before_source': {'revision': before_sha}, 'current_source': {},
              'dataset_identity': None, 'fixed_instant': None,
              'auxiliary_context': AUXILIARY.copy(), 'requested_count': subjects,
              'selected_count': 0, 'completed_count': 0, 'selected_subjects': [],
              'ineligible': [], 'surfaces': [], 'differences': [],
              'uncomparable': [], 'success': False}
    try:
        if type(subjects) is not int or subjects <= 0:
            raise ValueError('subjects must be a positive integer')
        if not isinstance(before_sha, str) or not before_sha or before_sha.startswith('-'):
            raise ValueError('invalid local before revision')
        revision = git(repo, 'rev-parse', '--verify', before_sha + '^{commit}').decode().strip()
        result['before_source']['resolved'] = revision
        instant = utc(now) if now else dt.datetime.now(dt.timezone.utc)
        result['fixed_instant'] = instant.isoformat()
        snapshot = capture(root)
        result['dataset_identity'] = snapshot['identity']
        ids, excluded = select_subjects(snapshot, subjects)
        result.update(selected_subjects=ids, selected_count=len(ids), ineligible=excluded)
        if len(ids) != subjects:
            raise ValueError('insufficient eligible subjects; no replacement or partial comparison')
        with tempfile.TemporaryDirectory(prefix='ledger-gate-') as temporary:
            space = Path(temporary).resolve()
            before = space / 'before'
            after = space / 'current'
            materialize(repo, revision, before)
            current_repo = Path(current).resolve() if current else repo
            identity = current_source(current_repo, after) if not current else _copy_local(current_repo, after)
            result['current_source'] = {'kind': 'local materialization' if current else 'current worktree',
                                        'path': str(current_repo), 'content_sha256': identity}
            values, errors = [], []
            for label, source in (('before', before), ('current', after)):
                try:
                    value = evaluate(source, snapshot, ids, result['fixed_instant'], space / 'evaluation')
                    validate_answers(value, ids)
                    values.append(value)
                except Exception as exc:
                    errors.append({'source': label, 'reason': f'{type(exc).__name__}: {exc}'})
            if errors:
                result['uncomparable'] = errors
                return result
            result['completed_count'] = len(ids)
            result['differences'] = differences(*values)
            result['surfaces'] = [{'surface': name,
                                    'equal': not any(row['surface'] == name for row in result['differences']),
                                    'before_sha256': digest(encoded(values[0][name])),
                                    'current_sha256': digest(encoded(values[1][name]))}
                                   for name in SURFACES]
            result['success'] = not result['differences']
    except Exception as exc:
        result['uncomparable'].append({'reason': f'{type(exc).__name__}: {exc}'})
    return result


def _copy_local(source, destination):
    # Used for deliberately local/synthetic source materializations. Git metadata
    # and Python caches are not runtime inputs; aliases are never followed.
    identities = {}
    destination.mkdir()
    for folder, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__')]
        for name in dirs + files:
            if (Path(folder) / name).is_symlink():
                raise ValueError('local revision alias')
        for name in files:
            p = Path(folder) / name
            relative = p.relative_to(source)
            data = p.read_bytes()
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            identities[str(relative)] = digest(data)
    return digest(encoded(identities))


def number(value, *, positive=False):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('measurement must be a finite number, not boolean')
    if value < 0 or (positive and value == 0):
        raise ValueError('invalid measurement sign')
    return value


def meter(start, end, *, samples=DEFAULT_SAMPLES):
    result = {'window': {'from': start, 'to': end}, 'requested_span_seconds': None,
              'sample_count': 0, 'bookkeeping_cpu_seconds': None, 'summed_wall_seconds': None,
              'cpu_s_per_hour': None, 'baseline': 7645, 'percent_change': None,
              'bar': 5350, 'at_or_under_bar': None, 'observed_coverage_start': None,
              'observed_coverage_end': None, 'coverage_tolerance_seconds': None,
              'maximum_interior_uncovered_gap': None, 'total_uncovered_seconds': None,
              'overlap_seconds': None, 'complete_window': False, 'complete_day': False,
              'reasons': [], 'success': False}
    try:
        begin, finish = utc(start), utc(end)
        span = (finish - begin).total_seconds()
        if span <= 0:
            raise ValueError('meter window must have positive width')
        result['requested_span_seconds'] = span
        selected = []
        previous = None
        with Path(samples).open() as stream:
            for line in stream:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('sample must be an object')
                stamp = utc(row.get('ts'))
                wall = number(row.get('wall'), positive=True)
                by = row.get('by')
                if not isinstance(by, dict):
                    raise ValueError('sample by must be an object')
                for value in by.values():
                    number(value)
                if begin <= stamp <= finish:
                    if previous is not None and stamp <= previous:
                        raise ValueError('selected timestamps must strictly increase')
                    selected.append((stamp, wall, by.get('ledger-bookkeeping', 0.0)))
                    previous = stamp
        if not selected:
            raise ValueError('no samples in requested window')
        walls = [r[1] for r in selected]
        cpu = math.fsum(r[2] for r in selected)
        wall = math.fsum(walls)
        rate = 3600 * cpu / wall
        number(rate)
        tolerance = statistics.median(walls)
        coverage_start = selected[0][0] - dt.timedelta(seconds=selected[0][1])
        coverage_end = selected[-1][0]
        leading = max(0.0, (coverage_start - begin).total_seconds())
        trailing = max(0.0, (finish - coverage_end).total_seconds())
        signed_gaps = [(b[0] - a[0]).total_seconds() - b[1] for a, b in zip(selected, selected[1:])]
        gaps = [max(0.0, gap) for gap in signed_gaps]
        maximum = max(gaps, default=0.0)
        complete = leading <= tolerance and trailing <= tolerance and maximum <= tolerance
        result.update(sample_count=len(selected), bookkeeping_cpu_seconds=cpu,
                      summed_wall_seconds=wall, cpu_s_per_hour=rate,
                      percent_change=100 * (rate / 7645 - 1), at_or_under_bar=rate <= 5350,
                      observed_coverage_start=coverage_start.isoformat(),
                      observed_coverage_end=coverage_end.isoformat(),
                      coverage_tolerance_seconds=tolerance, maximum_interior_uncovered_gap=maximum,
                      total_uncovered_seconds=leading + trailing + math.fsum(gaps),
                      overlap_seconds=math.fsum(max(0.0, -gap) for gap in signed_gaps),
                      complete_window=complete, complete_day=complete and span == 86400,
                      leading_uncovered_seconds=leading, trailing_uncovered_seconds=trailing)
        if not complete:
            result['reasons'].append('incomplete coverage: boundary or interior gap exceeds median wall')
        if rate > 5350:
            result['reasons'].append('bookkeeping CPU rate above approved bar')
        result['success'] = complete and rate <= 5350
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        result['reasons'].append(f'{type(exc).__name__}: {exc}')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest='verb', required=True)
    comparing = verbs.add_parser('compare')
    comparing.add_argument('--before', required=True)
    comparing.add_argument('--subjects', type=int, required=True)
    comparing.add_argument('--root', help='local events/content root (default DOIT_ROOT or ~/.do-it)')
    comparing.add_argument('--now', help='fixed aware instant (default capture time)')
    measuring = verbs.add_parser('meter')
    measuring.add_argument('--from', dest='start', required=True)
    measuring.add_argument('--to', dest='end', required=True)
    measuring.add_argument('--samples', default=DEFAULT_SAMPLES, help='local JSONL override for scratch measurements')
    args = parser.parse_args(argv)
    if args.verb == 'compare':
        result = compare(args.before, args.subjects, root=args.root, now=args.now)
    else:
        result = meter(args.start, args.end, samples=args.samples)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
