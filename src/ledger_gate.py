#!/usr/bin/env python3
"""Read-only ledger equality and bookkeeping meter (stdlib only).

compare(before_sha, subjects, *, root=None, repo=None, now=None,
        surfaces=SURFACES, timeout=90) returns a JSON
object with source identities, captured dataset digest, fixed UTC instant,
auxiliary_context, requested/selected/completed counts, frozen subject IDs,
ineligible rows, per-surface results, differences, uncomparable and success.
Only requested surfaces are executed and compared.
Optional current=DIR permits an explicitly identified local source materialization.
The default current source includes uncommitted tracked source edits.

meter(start, end, *, samples=DEFAULT_SAMPLES) returns the requested window,
validated sample count, CPU/wall sums, unrounded rate and signed baseline change,
bar decision, coverage/gap/overlap figures, complete_window/day and reasons.
Sample ts is its interval END; boundaries select inclusively. Sparse by objects
contribute zero bookkeeping CPU and their entire wall interval. All selected
rows count; malformed input anywhere fails rather than disappearing silently.

job_meter(start, end, jobs) reports current/baseline/control command-matched
CPU lower bounds, completion evidence and canonical merge provenance. CPU
lower bounds alone never establish a definitive charter saving. Optional
LEDGER_GATE_SAMPLES, LEDGER_GATE_CRON_RUNS_DIR and LEDGER_GATE_REPO select
local evidence. Preserved cron JSONL may carry ev=coverage records with aware
start/end, continuous=true and source provenance. A ring without such evidence
cannot establish complete-window passes. LEDGER_GATE_CADENCE optionally names
JSONL effective schedule evidence (job/start/end/cadence_s/anchor/source and
effective=true); its counts remain estimates, separate from observed passes.

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


def surface_selection(surfaces):
    if (not isinstance(surfaces, (tuple, list)) or not surfaces
            or any(not isinstance(s, str) or s not in SURFACES for s in surfaces)
            or len(set(surfaces)) != len(surfaces)):
        raise ValueError('surfaces must be a nonempty duplicate-free subset of SURFACES')
    return tuple(surfaces)


def _read_canonical(*, full_history=False, uncached=False):
    """Shared reader for selected revision answers and admitted merge provenance."""
    import fold
    kwargs = {'use_cache': False} if uncached else {}
    if full_history and 'with_reaped' in inspect.signature(fold.read_events).parameters:
        kwargs['with_reaped'] = True
    return fold.read_events(**kwargs)  # Internal TypeError is never retried.


def _evaluate_worker(revision, ids, surfaces=SURFACES):
    # Imported only in a bootstrapped private child, never against the live root.
    surfaces = surface_selection(surfaces)
    sys.path.insert(0, str(Path(revision) / 'src'))
    import fold
    import brief
    result = {s: {} for s in surfaces}
    history = None
    if set(surfaces) & {'events', 'counts'}:
        history = _read_canonical(full_history=True)
    default = _read_canonical() if set(surfaces) & {'brief', 'folded'} else None
    if 'folded' in surfaces:
        specs, charters, _, _ = fold.fold(default)
    for name, args in (('states', ['states']), ('board', [])):
        if name in surfaces:
            result[name] = _cli(Path(revision) / 'src/fold.py', args)
    if 'counts' in surfaces:
        result['counts'] = dict(collections.Counter(e['type'] for e in history))
    for subject in ids:
        if 'folded' in surfaces:
            record = {**specs, **charters}.get(subject)
            if record is None:
                raise ValueError('selected subject has no folded record: ' + subject)
            result['folded'][subject] = {k: v for k, v in record.items() if k != 'evs'}
        if 'brief' in surfaces:
            dossier = brief.dossier(subject, default, fold.ROOT, utc(os.environ['DOIT_NOW']))
            # brief.main's exact text presentation, from the revision's own
            # renderer and dossier, with a shared canonical read only.
            result['brief'][subject] = {'rendered': brief.render(dossier) + '\n',
                                         'dossier': dossier}
        if 'events' in surfaces:
            display = _cli(Path(revision) / 'src/fold.py', ['events', subject])
            result['events'][subject] = {
                'history': [e for e in history if e.get('subject') == subject],
                'display': [json.loads(line) for line in display.splitlines()]}
    return result


def evaluate(revision, snapshot, ids, now, workspace, *, surfaces=SURFACES, timeout=90):
    """Reset the SAME pathname before either revision or repeat evaluation."""
    surfaces = surface_selection(surfaces)
    number(timeout, positive=True)
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
        'print(json.dumps(_evaluate_worker(sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])), '
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
                            json.dumps(ids), json.dumps(surfaces)], cwd=workspace / 'cwd', env=env, timeout=timeout)
    finally:
        violations = workspace / 'violations.jsonl'
        if violations.exists() and violations.stat().st_size:
            raise RuntimeError('revision isolation violation (including answer descendants)')
    validate_answers(result, ids, surfaces=surfaces)
    return result


def validate_answers(result, ids, *, surfaces=SURFACES):
    surfaces = surface_selection(surfaces)
    if not isinstance(result, dict) or set(result) != set(surfaces):
        raise ValueError('incomplete surface comparison')
    for surface in set(SUBJECT_SURFACES) & set(surfaces):
        if not isinstance(result[surface], dict) or set(result[surface]) != set(ids):
            raise ValueError('partial selected subject comparison: ' + surface)
    for surface in {'board', 'states'} & set(surfaces):
        if not isinstance(result[surface], str) or not result[surface]:
            raise ValueError('missing rendered surface: ' + surface)
    if 'counts' in surfaces and (not isinstance(result['counts'], dict) or not result['counts']):
        raise ValueError('missing full history counts')
    for subject in ids:
        if 'brief' in surfaces:
            brief = result['brief'][subject]
            if (not isinstance(brief, dict) or set(brief) != {'rendered', 'dossier'}
                    or not isinstance(brief['rendered'], str) or not brief['rendered']
                    or not isinstance(brief['dossier'], dict) or not brief['dossier']):
                raise ValueError('missing brief/dossier')
        if 'events' in surfaces:
            events = result['events'][subject]
            if (not isinstance(events, dict) or set(events) != {'history', 'display'}
                    or not isinstance(events['history'], list) or not events['history']
                    or not isinstance(events['display'], list)):
                raise ValueError('missing event history/presentation')
        if 'folded' in surfaces and (not isinstance(result['folded'][subject], dict)
                                    or not result['folded'][subject]):
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


def compare(before_sha, subjects, *, root=None, repo=None, now=None, current=None,
            surfaces=SURFACES, timeout=90):
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    root = Path(root or os.environ.get('DOIT_ROOT') or Path.home() / '.do-it')
    result = {'before_source': {'revision': before_sha}, 'current_source': {},
              'dataset_identity': None, 'fixed_instant': None,
              'auxiliary_context': AUXILIARY.copy(), 'requested_count': subjects,
              'selected_count': 0, 'completed_count': 0, 'selected_subjects': [],
              'ineligible': [], 'surfaces': [], 'differences': [],
              'uncomparable': [], 'success': False}
    try:
        surfaces = surface_selection(surfaces)
        number(timeout, positive=True)
        result['requested_surfaces'] = list(surfaces)
        result['timeout'] = timeout
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
                    value = evaluate(source, snapshot, ids, result['fixed_instant'], space / 'evaluation',
                                     surfaces=surfaces, timeout=timeout)
                    validate_answers(value, ids, surfaces=surfaces)
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
                                   for name in surfaces]
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


JOBS = ('tick', 'spec_preflight')
BASELINE = ('2026-11-05T00:00:00Z', '2026-11-06T00:00:00Z')
CPU_EXCLUSIONS = ['descendants whose own command does not match',
                  'processes that exit between samples',
                  'no pid/ppid: category membership cannot reconstruct a job process tree',
                  'top may be truncated; a full ledger list is still command-matched only']


def _jsonl(path):
    data = Path(path).read_bytes()
    rows = []
    for line in data.decode('utf-8').splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('JSONL row must be an object')
        rows.append(row)
    return rows, {'path': str(path), 'sha256': digest(data), 'rows': len(rows)}


def _job_match(command, job):
    patterns = {'tick': r'(?:^|[ /])tick\.py(?:\s|$)|(?:^|\s)cron-run\s+tick(?:\s|$)',
                'spec_preflight': r'(?:^|[ /])spec_preflight\.py(?:\s|$)|(?:^|\s)cron-run\s+spec-preflight(?:\s|$)'}
    return re.search(patterns[job], command) is not None


def _job_samples(path):
    rows, identity = _jsonl(path)
    result, previous = [], None
    for row in rows:
        stamp = utc(row.get('ts'))
        wall = number(row.get('wall'), positive=True)
        if previous is not None and stamp <= previous:
            raise ValueError('sample timestamps must strictly increase')
        previous = stamp
        if not isinstance(row.get('by'), dict):
            raise ValueError('sample by must be an object')
        for value in row['by'].values():
            number(value)
        lists = {}
        for key in ('ledger', 'top'):
            entries = row.get(key, [])
            if not isinstance(entries, list):
                raise ValueError('sample observation list required')
            lists[key] = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('cmd'), str):
                    raise ValueError('sample observation requires cmd and cpu')
                lists[key].append((entry['cmd'], number(entry.get('cpu'))))
        # Entries have no process identity, and cmd lengths can differ between
        # the two lists. Retain the largest multiplicity, conservatively treating
        # equal CPU/prefix commands as overlapping observations, not extra CPU.
        union = list(lists['ledger'])
        available = list(lists['ledger'])
        for cmd, cpu in lists['top']:
            match = next((i for i, (other, cost) in enumerate(available)
                          if cost == cpu and (other.startswith(cmd) or cmd.startswith(other))), None)
            if match is None:
                union.append((cmd, cpu))
            else:
                i_cmd, i_cpu = available.pop(match)
                if len(cmd) > len(i_cmd):
                    union[union.index((i_cmd, i_cpu))] = (cmd, cpu)
        cpu = {}
        for job in JOBS:
            matches = [cost for cmd, cost in union if _job_match(cmd, job)]
            cpu[job] = math.fsum(matches) if matches else None
        result.append((stamp, wall, cpu))
    identity['cutoff'] = previous.isoformat() if previous else None
    identity['schema_evidence'] = 'own cmd/cpu only; category totals are not job CPU'
    return result, identity


def _coverage(selected, begin, finish):
    if not selected:
        return {'complete_window': False, 'reasons': ['no samples'],
                'observed_start': None, 'observed_end': None, 'overlap_s': None}
    tolerance = statistics.median(row[1] for row in selected)
    first = selected[0][0] - dt.timedelta(seconds=selected[0][1])
    last = selected[-1][0]
    leading = max(0., (first - begin).total_seconds())
    trailing = max(0., (finish - last).total_seconds())
    gaps = [(b[0] - a[0]).total_seconds() - b[1] for a, b in zip(selected, selected[1:])]
    maximum = max((max(0., gap) for gap in gaps), default=0.)
    complete = max(leading, trailing, maximum) <= tolerance
    return {'complete_window': complete, 'observed_start': first.isoformat(),
            'observed_end': last.isoformat(), 'tolerance_s': tolerance,
            'leading_gap_s': leading, 'trailing_gap_s': trailing,
            'maximum_interior_gap_s': maximum,
            'uncovered_s': leading + trailing + math.fsum(max(0., g) for g in gaps),
            'overlap_s': math.fsum(max(0., -g) for g in gaps),
            'complete_day': complete and (finish-begin).total_seconds() == 86400,
            'reasons': [] if complete else ['boundary or interior gap exceeds median sampled wall']}


def _cron_evidence(path):
    if not path.exists():
        return {'source': {'path': str(path), 'sha256': None}, 'starts': {}, 'ends': {},
                'coverage': [], 'reasons': ['missing completion log']}
    rows, identity = _jsonl(path)
    starts, ends, coverage, previous = {}, {}, [], None
    for row in rows:
        ev = row.get('ev')
        if ev == 'coverage':
            a, b = utc(row.get('start')), utc(row.get('end'))
            if b <= a or row.get('continuous') is not True or not row.get('source'):
                raise ValueError('coverage needs ordered aware bounds, continuous=true and provenance')
            coverage.append((a, b, row['source']))
            continue
        stamp = utc(row.get('ts'))
        if previous is not None and stamp < previous:
            raise ValueError('completion timestamps must not reverse')
        previous = stamp
        if ev == 'start':
            key = stamp.isoformat()
            if key in starts:
                raise ValueError('duplicate invocation start timestamp')
            starts[key] = stamp
        elif ev == 'end':
            began = utc(row.get('start_ts'))
            number(row.get('duration_s'))
            if type(row.get('rc')) is not int or began > stamp:
                raise ValueError('invalid completion rc/start timestamp')
            key = began.isoformat()
            value = {'stamp': stamp, 'start': began, 'duration_s': row['duration_s'], 'rc': row['rc']}
            if key in ends and ends[key] != value:
                raise ValueError('conflicting duplicate completion')
            ends[key] = value
        else:
            raise ValueError('unknown cron event')
    identity['cutoff'] = previous.isoformat() if previous else None
    return {'source': identity, 'starts': starts, 'ends': ends,
            'coverage': coverage, 'reasons': []}


def _passes(evidence, begin, finish):
    starts, ends = evidence['starts'], evidence['ends']
    selected = [e for e in ends.values() if begin < e['stamp'] <= finish]
    # Full-window evidence is affirmative operator preservation, never inferred
    # from a ring's first/last rows or from the configured cron schedule.
    bounds = sorted(evidence['coverage'])
    cursor = begin
    for a, b, _ in bounds:
        if a <= cursor < b:
            cursor = max(cursor, b)
    complete = cursor >= finish
    reasons = list(evidence['reasons'])
    if not complete:
        reasons.append('no affirmative continuous completion coverage for the requested window')
    first = min(starts.values(), default=None)
    if first and first > begin and not complete:
        reasons.append('earliest retained invocation is after window start (ring truncated)')
    unmatched = sum(e['start'].isoformat() not in starts for e in selected)
    if unmatched:
        reasons.append('completed invocations without retained starts')
        complete = False
    known = len(selected)
    duration = math.fsum(e['duration_s'] for e in selected)
    observed = sorted([*starts.values(), *(e['stamp'] for e in ends.values())])
    return {'passes': known if complete else None, 'known_completions': known,
            'failed_completions': sum(e['rc'] != 0 for e in selected),
            'unmatched_ends': unmatched,
            'unfinished_invocations': sum(a <= finish and (key not in ends or ends[key]['stamp'] > finish)
                                          for key, a in starts.items() if a >= begin),
            'boundary_crossing_completions': sum(e['start'] <= begin for e in selected),
            'pass_quality': 'observed-complete' if complete else 'incomplete',
            'pass_reasons': reasons, 'pass_source': evidence['source'],
            'wall_fallback': {'observed_wall_s': duration,
                'known_completions': known,
                'wall_s_per_known_completed_pass': duration/known if known else None,
                'observation_start': observed[0].isoformat() if observed else None,
                'observation_end': observed[-1].isoformat() if observed else None,
                'complete_window': complete,
                'coverage': [{'from': a.isoformat(), 'to': b.isoformat(), 'source': source}
                             for a, b, source in bounds],
                'quality': 'wall duration of known completed invocations; never CPU or a full-window mean'}}


def _merge_provenance(root, repo):
    import fold
    # Reuse the canonical reader's virtual sources, corrections and authorization.
    # Disable the disk read cache; restore module scope for library callers.
    old = fold.ROOT, fold.EVENTS, fold.PROJECT
    try:
        fold.ROOT, fold.EVENTS, fold.PROJECT = root, root / 'events', None
        events = _read_canonical(uncached=True)
        specs, _, _, _ = fold.fold(events)
    finally:
        fold.ROOT, fold.EVENTS, fold.PROJECT = old
    merges = []
    for subject, spec in specs.items():
        for event in spec['evs']:
            if event.get('type') != 'shipped':
                continue
            sha = event.get('sha')
            if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{7,40}', sha):
                raise ValueError('canonical shipped merge lacks a valid sha')
            merges.append({'subject': subject, 'charter': spec.get('charter'),
                           'sha': sha, 'ts': utc(event.get('ts')).isoformat()})
    merges.sort(key=lambda m: (utc(m['ts']), m['subject'], m['sha']))
    own = [m for m in merges if m['charter'] == 'L-charter-0086']
    first, last = dict(own[0]) if own else None, dict(own[-1]) if own else None
    reasons = []
    if first:
        try:
            first['parent_sha'] = git(repo, 'rev-parse', '--verify', first['sha']+'^1').decode().strip()
        except ValueError:
            first['parent_sha'] = None
            reasons.append('first merge parent unavailable locally')
    else:
        reasons.append('no canonical shipped L-charter-0086 merge anchor')
    return {'first_merge': first, 'last_merge': last, 'merges': merges, 'reasons': reasons,
            'source': {'events': str(root / 'events'),
                       'sha256': digest(encoded(merges)), 'authorization': 'canonical fold admitted shipped events'}}


def _scheduled_passes(path, job, begin, finish):
    if not path:
        return {'scheduled_passes': None, 'scheduled_quality': 'unknown',
                'schedule_reasons': ['no operator-confirmed effective cadence evidence']}
    rows, identity = _jsonl(path)
    matches = []
    for row in rows:
        if row.get('job') not in JOBS:
            raise ValueError('invalid cadence job')
        a, b = utc(row.get('start')), utc(row.get('end'))
        cadence = number(row.get('cadence_s'), positive=True)
        if b <= a or row.get('effective') is not True or not row.get('source'):
            raise ValueError('cadence requires effective bounds and operator provenance')
        if row['job'] == job and a <= begin and b >= finish:
            # Anchor is an effective scheduled occurrence; count in (begin,end].
            anchor = utc(row.get('anchor'))
            matches.append((math.floor((finish-anchor).total_seconds()/cadence)
                            - math.floor((begin-anchor).total_seconds()/cadence),
                            {k: row[k] for k in ('job', 'start', 'end', 'cadence_s',
                                                 'anchor', 'source', 'effective')}))
    if len(matches) > 1:
        raise ValueError('ambiguous effective cadence evidence')
    return {'scheduled_passes': matches[0][0] if matches else None,
            'scheduled_quality': 'estimate' if matches else 'unknown',
            'schedule_source': identity, 'effective_cadence': matches[0][1] if matches else None,
            'schedule_reasons': [] if matches else ['effective cadence does not cover window']}


def job_meter(start, end, jobs):
    result = {'windows': {}, 'provenance': {}, 'changes': {}, 'charter_bar_met': None,
              'reasons': [], 'success': False}
    try:
        if (not isinstance(jobs, (tuple, list)) or not jobs
                or any(not isinstance(j, str) or j not in JOBS for j in jobs)
                or len(set(jobs)) != len(jobs)):
            raise ValueError('jobs must be a nonempty duplicate-free subset of tick/spec_preflight')
        begin, finish = utc(start), utc(end)
        if finish <= begin:
            raise ValueError('job-meter window must have positive width')
        root = Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
        repo = Path(os.environ.get('LEDGER_GATE_REPO', Path(__file__).resolve().parents[1]))
        samples, identity = _job_samples(os.environ.get('LEDGER_GATE_SAMPLES', DEFAULT_SAMPLES))
        cron = Path(os.environ.get('LEDGER_GATE_CRON_RUNS_DIR', root / 'state/cron-runs'))
        evidence = {j: _cron_evidence(cron / ('spec-preflight.jsonl' if j == 'spec_preflight' else 'tick.jsonl'))
                    for j in jobs}
        provenance = _merge_provenance(root, repo)
        result['provenance'] = provenance
        first = provenance['first_merge']
        control_end = utc(first['ts']) if first else None
        windows = {'current': (begin, finish), 'baseline': tuple(map(utc, BASELINE)),
                   'control': (control_end-dt.timedelta(days=1), control_end) if first else None}
        for label, window in windows.items():
            if window is None:
                result['windows'][label] = None
                continue
            a, b = window
            selected = [r for r in samples if a < r[0] <= b]
            wall = math.fsum(r[1] for r in selected)
            coverage = _coverage(selected, a, b)
            entry = {'from': a.isoformat(), 'to': b.isoformat(), 'source': identity,
                     'sample_count': len(selected), 'sampled_wall_s': wall, 'coverage': coverage,
                     'merges': [m for m in provenance['merges'] if a < utc(m['ts']) <= b], 'jobs': {}}
            for job in jobs:
                values = [r[2][job] for r in selected if r[2][job] is not None]
                cpu = math.fsum(values) if values else None
                rate = 86400*cpu/wall if cpu is not None and wall else None
                if rate is not None:
                    number(rate)
                passes = _passes(evidence[job], a, b)
                scheduled = _scheduled_passes(os.environ.get('LEDGER_GATE_CADENCE'), job, a, b)
                row = {'observed_cpu_s': cpu, 'cpu_s_per_86400_sampled_wall': rate,
                       'cpu_quality': 'lower-bound' if cpu is not None else 'unknown',
                       'cpu_reasons': CPU_EXCLUSIONS + (['no own-command matches; missing CPU is unknown']
                                                       if cpu is None else []),
                       'matched_sample_count': len(values), 'missing_match_sample_count': len(selected)-len(values),
                       'cpu_s_per_pass': cpu/passes['passes'] if cpu is not None and passes['passes'] else None,
                       **passes, **scheduled}
                expected = scheduled['scheduled_passes']
                row['cpu_s_per_scheduled_pass'] = cpu/expected if cpu is not None and expected else None
                entry['jobs'][job] = row
            result['windows'][label] = entry
        for job in jobs:
            changes = {}
            current = result['windows']['current']['jobs'][job]['cpu_s_per_86400_sampled_wall']
            for label in ('baseline', 'control'):
                other = result['windows'][label]
                old = other['jobs'][job]['cpu_s_per_86400_sampled_wall'] if other else None
                defined = current is not None and old is not None
                changes[label] = {'delta': current-old if defined else None,
                    'percent': 100*(current/old-1) if defined and old else None,
                    'quality': 'lower-bound-derived; cannot establish definitive savings',
                    'comparison_complete': bool(other and other['coverage']['complete_window']
                                                and result['windows']['current']['coverage']['complete_window'])}
            result['changes'][job] = changes
        result['reasons'] = list(provenance['reasons'])
        current = result['windows']['current']
        if not current['coverage']['complete_window']:
            result['reasons'].append('incomplete current sample coverage')
        for job, row in current['jobs'].items():
            if row['observed_cpu_s'] is None:
                result['reasons'].append(job+': CPU unknown')
            if row['passes'] is None:
                result['reasons'].append(job+': incomplete pass evidence')
        result['bar_reasons'] = ['command-matched lower bounds cannot establish a definitive decrease',
                                 'baseline/control coverage, confounding merges and paired causal evidence must be reviewed']
        result['success'] = not result['reasons']
    except (ValueError, TypeError, OSError, OverflowError, KeyError, UnicodeError) as exc:
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
    comparing.add_argument('--surfaces', nargs='+', default=SURFACES)
    comparing.add_argument('--timeout', type=float, default=90)
    measuring = verbs.add_parser('meter')
    measuring.add_argument('--from', dest='start', required=True)
    measuring.add_argument('--to', dest='end', required=True)
    measuring.add_argument('--samples', default=DEFAULT_SAMPLES, help='local JSONL override for scratch measurements')
    jobs = verbs.add_parser('job-meter')
    jobs.add_argument('--from', dest='start', required=True)
    jobs.add_argument('--to', dest='end', required=True)
    jobs.add_argument('--jobs', nargs='+', required=True)
    args = parser.parse_args(argv)
    if args.verb == 'compare':
        result = compare(args.before, args.subjects, root=args.root, now=args.now,
                         surfaces=args.surfaces, timeout=args.timeout)
    elif args.verb == 'job-meter':
        result = job_meter(args.start, args.end, args.jobs)
    else:
        result = meter(args.start, args.end, samples=args.samples)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
