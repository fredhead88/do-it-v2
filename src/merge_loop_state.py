"""Read-only merge and release evidence for local reporting consumers.

Missing, changing or malformed evidence stays unknown. No ledger readiness, log
mtime guessing, delivery, process control or persistent cache participates.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Literal
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

IL = ZoneInfo('Asia/Jerusalem')
SLOT_HOURS = {'overnight': 3, 'afternoon': 15}
SPEC = re.compile(r'L-spec-[0-9]{4}')
MERGE = re.compile(r'^merge\(L-spec-([0-9]{4})\):')
OUTCOMES = {'released', 'api-failed', 'api-rolled-back', 'site-failed', 'skipped', 'delayed', 'paused'}
MAX_SOURCE_BYTES = 4 * 1024 * 1024


@dataclass
class Failure:
    spec: str
    reason: str
    attempts: int | None


@dataclass
class LoopState:
    status: Literal['running', 'rejecting', 'stopped', 'undetermined']
    queued: list[str]
    failures: list[Failure]
    last_merge: datetime | None
    read_at: datetime


@dataclass
class Merged:
    spec: str
    plain_line: str
    time: datetime


@dataclass
class Released:
    version: str
    time: datetime | None
    outcome: str
    date: str


@dataclass
class Held:
    spec: str
    reason: str
    age: timedelta | None


@dataclass
class DayLists:
    merged: list[Merged] | None
    released: list[Released] | None
    held: list[Held] | None
    release_hold: bool


@dataclass(frozen=True)
class _Paths:
    content: Path
    repo: Path
    push: Path
    logs: Path
    hold: Path


def _paths(root, push_dir):
    explicit = root is not None
    root = Path(root) if explicit else Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
    return _Paths(root / 'content', root / 'repos/albert-scott',
                  Path(push_dir) if push_dir is not None else root / 'push' if explicit else Path('/var/tmp/push'),
                  root / 'logs' if explicit else Path('/var/log/albert-scott'), root / 'state/release-hold')


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('now must be an aware datetime')
    return value


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be text')
    return _now(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _identity(path):
    try:
        s = path.lstat()
    except FileNotFoundError:
        return None
    return s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns


class _Snapshot:
    """Track absence and identity as well as successful reads; fail on replacement."""
    def __init__(self):
        self.seen = {}

    def remember(self, path):
        identity = _identity(path)
        if path in self.seen and self.seen[path] != identity:
            raise OSError('source changed')
        self.seen[path] = identity
        return identity

    def names(self, path):
        self.remember(path)
        return sorted(p.name for p in path.iterdir())

    def text(self, path, *, optional=False):
        identity = self.remember(path)
        if identity is None:
            if optional:
                return None
            raise FileNotFoundError(path)
        if not stat.S_ISREG(identity[2]):
            raise OSError('source is not a regular file')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            actual = os.fstat(source.fileno())
            if (actual.st_dev, actual.st_ino) != identity[:2]:
                raise OSError('source replaced')
            data = source.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError('source too large')
        self.remember(path)
        return data.decode('utf-8')

    def stable(self):
        try:
            return all(_identity(path) == identity for path, identity in self.seen.items())
        except OSError:
            return False


def _process_argv(pid):
    data = (Path('/proc') / str(pid) / 'cmdline').read_bytes()
    return [word.decode('utf-8') for word in data.split(b'\0') if word]


def _process_status(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'stopped'
    except OSError:
        return 'undetermined'
    try:
        argv = _process_argv(pid)
        # A shell's script argument identifies this driver, not an arbitrary
        # substring in another process's command or an argument passed via -c.
        shell = argv and Path(argv[0]).name in {'bash', 'sh', 'dash'}
        script = next((arg for arg in argv[1:] if not arg.startswith('-')), '')
        if shell and Path(script).name in {'rloop.sh', 'rloop.run.sh'} and '-c' not in argv[1:]:
            if _process_argv(pid) == argv:
                return 'running'
    except (OSError, UnicodeError):
        pass
    return 'undetermined'


def _batch(text):
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if (len(lines) != 1 or len(lines[0]) < 2 or
            re.fullmatch(r'[0-9a-fA-F]{40}', lines[0][0]) is None or
            any(re.fullmatch(r'[0-9]{4}', token) is None for token in lines[0][1:])):
        raise ValueError('invalid batch')
    return ['L-spec-' + token for token in lines[0][1:]]


def _reason(text):
    """Return observed categories, never arbitrary diagnostics or credentials."""
    text = text.lower()
    if 'conflict' in text:
        return 'Merge conflict needs review.'
    if 'attributed' in text or 'attr:' in text:
        return 'Attributed test failure needs review.'
    if 'ci' in text or 'test' in text or 'failure' in text or 'red' in text:
        return 'Observed check failure needs review.'
    if 'lock' in text or 'cap' in text or 'base-not-releasable' in text:
        return 'Merge window refused to proceed.'
    return 'Work is held for review.'


def _ci(text, specs):
    """Return (failed specs, completed green) from explicit terminal evidence."""
    exits = re.findall(r'^exit=([^\n]*)$', text, re.M)
    if not exits:
        return [], False
    if not re.fullmatch(r'[0-9]{1,3}', exits[-1].strip()) or int(exits[-1]) > 255:
        raise ValueError('invalid terminal status')
    code = int(exits[-1])
    if code == 0:
        return [], True
    attributed = set(re.findall(r'^(?:ATTRIBUTED|FALLBACK) (L-spec-[0-9]{4})\b', text, re.M))
    if code == 143 and not attributed:
        return [], False
    return [sid for sid in specs if not attributed or sid in attributed], False


def _history(paths, now):
    if not paths.repo.is_dir():
        return None
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0')
    try:
        result = subprocess.run(['git', '-C', str(paths.repo), 'log', '--first-parent',
                                 '--format=%cI%x09%s', 'master', '--'],
                                capture_output=True, text=True, timeout=10, env=env)
        if result.returncode or len(result.stdout) > MAX_SOURCE_BYTES:
            return None
        rows = []
        for line in result.stdout.splitlines():
            timestamp, subject = line.split('\t', 1)
            match = MERGE.match(subject)
            if match:
                when = _stamp(timestamp)
                if when <= now:
                    rows.append(('L-spec-' + match[1], when.astimezone(IL)))
        return rows
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return None


@dataclass
class _QueueRead:
    queued: list[str]
    failures: list[Failure]
    known: bool
    held_known: bool
    process: str


def _queue(paths, history):
    q = paths.push / 'rq'
    snapshot = _Snapshot()
    queued, batches, attention, active, green, archives = [], {}, {}, {}, {}, {}
    known = held_known = True
    process = 'undetermined'
    try:
        names = snapshot.names(q)
    except (OSError, ValueError):
        return _QueueRead([], [], False, False, process)

    def read_text(name, optional=False):
        nonlocal known, held_known
        try:
            return snapshot.text(q / name, optional=optional)
        except (OSError, ValueError, UnicodeError):
            known = held_known = False
            return None

    # Active batches include the in-flight copy, never their base SHA.
    for name in ['current.bat'] + [n for n in names if n.endswith('.batch')]:
        text = read_text(name, optional=name == 'current.bat' and name not in names)
        if text is None:
            continue
        try:
            specs = _batch(text)
            batches[name] = specs
            queued.extend(specs)
        except ValueError:
            known = held_known = False
    text = read_text('attention.txt')
    if text is not None:
        for line in text.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r'([0-9]{4})\s+([0-9]{2}):([0-9]{2})\s+(.+)', line)
            if not match or int(match[2]) > 23 or int(match[3]) > 59:
                known = held_known = False
                continue
            sid = 'L-spec-' + match[1]
            reason, count = attention.get(sid, ('', 0))
            attention[sid] = (_reason(match[4]), count + 1)

    # Archives corroborate current holds; they cannot resurrect removed holds.
    for name in [n for n in names if n.endswith('.batch.red')]:
        batch = read_text(name)
        ci = read_text(name + '.ci')
        if batch is None or ci is None:
            continue
        try:
            specs = _batch(batch)
            failures, _ = _ci(ci, specs)
            for sid in failures:
                archives.setdefault(sid, []).append(snapshot.seen[q / (name + '.ci')][4])
        except ValueError:
            known = held_known = False

    for name, specs in batches.items():
        if name == 'current.bat':
            continue
        ci = read_text(name + '.ci', optional=True)
        if ci is not None:
            try:
                failures, success = _ci(ci, specs)
            except ValueError:
                known = held_known = False
                continue
            if success:
                for sid in specs:
                    green[sid] = max(green.get(sid, 0), snapshot.seen[q / (name + '.ci')][4])
            for sid in failures:
                active[sid] = _reason(ci)

    current = read_text('current', optional=True)
    if current is None:
        if 'current.bat' in names:
            known = False
    elif re.fullmatch(r'win-[0-9]{8}-[A-Za-z0-9_-]+', current.strip()) is None:
        known = False
    else:
        log = read_text(current.strip() + '.log')
        if log is not None:
            specs = batches.get('current.bat', [])
            try:
                failures, success = _ci(log, specs)
            except ValueError:
                failures, success = [], False
                known = held_known = False
            if success and 'advanced master' in log:
                for sid in specs:
                    green[sid] = max(green.get(sid, 0), snapshot.seen[q / (current.strip() + '.log')][4])
            for sid in failures:
                active[sid] = _reason(log)

    pid = read_text('main.pid')
    if pid is not None and re.fullmatch(r'[1-9][0-9]{0,9}\s*', pid) and int(pid) <= 2**31 - 1:
        process = _process_status(int(pid))
    else:
        known = False
    landed = {sid for sid, _ in history or []}
    failures = []
    for sid in dict.fromkeys([*attention, *active]):
        observed_red = archives.get(sid, [])
        attention_identity = snapshot.seen.get(q / 'attention.txt')
        latest_red = max(observed_red, default=attention_identity[4] if attention_identity else 0)
        if sid in landed or (green.get(sid, 0) > latest_red and sid not in active):
            continue
        reason, count = attention.get(sid, (active.get(sid), 0))
        observations = max(count, len(archives.get(sid, [])))
        failures.append(Failure(sid, active.get(sid, reason), observations if observations > 1 else None))
    if not snapshot.stable():
        known = held_known = False
    return _QueueRead(list(dict.fromkeys(queued)), failures, known, held_known, process)


def read(root=None, push_dir=None, now=None):
    now = _now(now)
    paths = _paths(root, push_dir)
    history = _history(paths, now)
    evidence = _queue(paths, history)
    status = evidence.process
    if not evidence.known or status == 'undetermined':
        status = 'undetermined'
    elif status == 'running' and evidence.failures:
        status = 'rejecting'
    last = max((when for _, when in history or []), default=None)
    return LoopState(status, evidence.queued, evidence.failures, last, now)


def _safe_sentence(text):
    if not text or len(text) > 500 or any(unicodedata.category(c).startswith('C') for c in text):
        return None
    if re.search(r'[<>\[\]`*_\\|]|(?:token|password|secret|credential|dsn|traceback|exception|sentinel)\b', text, re.I):
        return None
    if re.search(r'\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|bearer\s+)', text, re.I):
        return None
    # Hosted HTTP(S) links may remain, but local paths and diagnostic fragments
    # are unavailable instead of being partially scrubbed into a false summary.
    urls = re.findall(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', text)
    rest = text
    for url in urls:
        parsed = urlsplit(url.rstrip('.,!?'))
        host = (parsed.hostname or '').lower()
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            return None
        if not host or '.' not in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        rest = rest.replace(url, '')
    if re.search(r'(?:^|\s)(?:/|~/|[A-Za-z]:\\)|\b(?:https?|file|ssh):|\b(?:exit=|NEW:|ERROR:)', rest, re.I):
        return None
    sentence = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text.strip(), maxsplit=1)[0]
    return sentence if sentence.endswith(('.', '!', '?')) else sentence + '.'


def plain_line(spec_id, root=None):
    valid = isinstance(spec_id, str) and SPEC.fullmatch(spec_id) is not None
    fallback = f'Plain-English description unavailable for {spec_id if valid else "unknown spec"}.'
    if not valid:
        return fallback
    paths = _paths(root, None)
    try:
        file = paths.content / (spec_id + '.md')
        file.resolve().relative_to(paths.content.resolve())
        snapshot = _Snapshot()
        text = snapshot.text(file)
        goal = False
        for line in text.split('\n'):
            if re.match(r'^##\s+Goal\s*$', line):
                goal = True
                continue
            if goal and re.match(r'^#{1,2}\s', line):
                break
            if goal and line.startswith('In plain English:'):
                sentence = _safe_sentence(line[len('In plain English:'):].strip())
                return sentence if sentence and snapshot.stable() else fallback
    except (OSError, ValueError, UnicodeError):
        pass
    return fallback


def _hold(path):
    try:
        return path.lstat() is not None
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _slot(key):
    match = re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2})-(overnight|afternoon)', key)
    if not match:
        raise ValueError('invalid slot key')
    return datetime.combine(date.fromisoformat(match[1]), time(SLOT_HOURS[match[2]]), IL)


def next_release(now, root=None):
    now = _now(now)
    paths = _paths(root, None)
    if _hold(paths.hold):
        return None
    snapshot = _Snapshot()
    try:
        state = json.loads(snapshot.text(paths.logs / 'release-state.json'))
        if not isinstance(state, dict) or not isinstance(state['slots'], dict) or not isinstance(state['shas'], dict):
            return None
        elapsed = now - _stamp(state['last_tick_utc'])
        if not timedelta(0) <= elapsed <= timedelta(minutes=30):
            return None
        slots = state['slots']
        for key, value in slots.items():
            when = _slot(key)
            if not isinstance(value, dict) or value.get('status') not in {'pending', 'done'}:
                return None
            if value['status'] == 'pending' and when <= now:
                return None
        local = now.astimezone(IL)
        for back in (1, 0):
            day = local.date() - timedelta(days=back)
            for label, hour in SLOT_HOURS.items():
                due = datetime.combine(day, time(hour), IL)
                if due <= now and slots.get(f'{day}-{label}', {}).get('status') != 'done':
                    return None
        # Skip future slots explicitly completed early. There are finitely many
        # such entries, so this bound always includes an unrecorded future day.
        for offset in range(len(slots) + 2):
            day = local.date() + timedelta(days=offset)
            for label, hour in SLOT_HOURS.items():
                candidate = datetime.combine(day, time(hour), IL)
                if candidate > now and slots.get(f'{day}-{label}', {}).get('status') != 'done':
                    if not snapshot.stable() or _hold(paths.hold):
                        return None
                    return candidate.strftime('%Y-%m-%d %H:%M')
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    return None


def _released(paths, now):
    snapshot = _Snapshot()
    rows = []
    try:
        text = snapshot.text(paths.logs / 'releases.jsonl')
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('outcome') not in OUTCOMES:
                return None
            day = date.fromisoformat(row['date'])
            instant = None
            for key in ('ts', 'time', 'timestamp', 'released_at'):
                if row.get(key) is not None:
                    instant = _stamp(row[key]).astimezone(IL)
                    break
            observed_day = instant.date() if instant is not None else day
            if observed_day != now.astimezone(IL).date() or (instant is not None and instant > now):
                continue
            # Version labels are identifiers, not sentences; avoid raw log text.
            raw = next((row[k].strip() for k in ('version', 'tag', 'sha')
                        if isinstance(row.get(k), str) and row[k].strip()), 'unknown')
            version = raw if _safe_sentence(raw) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}', raw) else 'unknown'
            rows.append(Released(version, instant, row['outcome'], observed_day.isoformat()))
        return rows if snapshot.stable() else None
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def day_lists(now, root=None, push_dir=None):
    now = _now(now)
    paths = _paths(root, push_dir)
    history = _history(paths, now)
    merged = None if history is None else [Merged(sid, plain_line(sid, root), when)
              for sid, when in history if when.date() == now.astimezone(IL).date()]
    queue = _queue(paths, history)
    held = [Held(f.spec, f.reason, None) for f in queue.failures] if queue.held_known else None
    return DayLists(merged, _released(paths, now), held, _hold(paths.hold))
