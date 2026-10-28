#!/bin/bash
# Read-only parity gate. Python owns the snapshot lifetime, including faults.
exec /usr/bin/python3 -B - "$@" "${REALM_PARITY_SRC:-$(dirname "$0")/../src}" <<'PY'
import collections
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

STAGES = ('idea', 'planned', 'building', 'checking', 'merging', 'live')
FIELDS = ('item', 'item_kind', 'stage', 'state', 'action', 'owner', 'trigger',
          'reason', 'clears', 'since')
NULLABLE = {'action', 'trigger', 'clears'}


class Undetermined(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Undetermined(message)


def fingerprint(directory):
    """Include directory entries too, so additions/removals during copy show up."""
    result = []
    for path in [directory, *sorted(directory.rglob('*'))]:
        stat = path.stat()
        require(not path.is_symlink(), 'snapshot source contains symlink')
        require(bool(stat.st_mode & 0o444) and os.access(path, os.R_OK),
                'snapshot source unreadable')
        require(path.is_dir() or path.is_file(), 'snapshot source is not a regular file')
        result.append((str(path.relative_to(directory)), stat.st_size if path.is_file() else None,
                       stat.st_mtime_ns))
    return result


def pinned_copy(events, target):
    content = events.parent / 'content'
    require(events.is_dir(), 'events-dir missing')
    require(bool(events.stat().st_mode & 0o444) and os.access(events, os.R_OK | os.X_OK),
            'events-dir unreadable')
    require(any(events.glob('*.jsonl')), 'events-dir has no *.jsonl')
    require(content.is_dir(), 'sibling content directory missing')
    for attempt in range(3):
        for name in ('events', 'content'):
            if (target / name).exists():
                shutil.rmtree(target / name)
        try:
            before = (fingerprint(events), fingerprint(content))
            shutil.copytree(events, target / 'events')
            shutil.copytree(content, target / 'content')
            after = (fingerprint(events), fingerprint(content))
        except (FileNotFoundError, shutil.Error):
            # A disappearing entry may be a concurrent append/rotation; retry.
            continue
        if before != after:
            continue
        require((target / 'events').is_dir() and (target / 'content').is_dir(),
                'snapshot copy incomplete')
        require(before == (fingerprint(target / 'events'), fingerprint(target / 'content')),
                'snapshot copy incomplete')
        return
    raise Undetermined('source changed during copy')


def validate_step(step, side, index):
    label = f'{side} Step[{index}]'
    require(isinstance(step, dict), label + ' is not an object')
    for field in FIELDS:
        require(field in step, label + ' missing ' + field)
        require(isinstance(step[field], str) or field in NULLABLE and step[field] is None,
                label + ' ill-typed ' + field)
    require(step['stage'] in STAGES, label + ' invalid stage')
    require(step['state'] in ('hold', 'action'), label + ' invalid state')
    require(step['item_kind'] in ('charter', 'spec', 'attempt', 'window', 'check'),
            label + ' invalid item_kind')
    require(step['owner'] in ('tick', 'executor', 'thinker', 'operator', 'relay', 'sweeper'),
            label + ' invalid owner')


def canonical(repo, env, side):
    try:
        proc = subprocess.run([str(repo / 'doit'), 'next', '--json'], cwd=repo,
                              env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise Undetermined(side + ' doit timed out (60 s)')
    except (OSError, UnicodeError):
        raise Undetermined(side + ' doit could not run or decode output')
    require(proc.returncode == 0, side + ' doit exited nonzero')
    try:
        value = json.loads(proc.stdout)
    except ValueError:
        raise Undetermined(side + ' doit printed non-JSON')
    require(isinstance(value, dict), side + ' doit output is not an object')
    require('counts' in value, side + ' doit counts missing')
    counts = value['counts']
    require(isinstance(counts, dict), side + ' doit counts is not a dict')
    require(set(counts) == set(STAGES), side + ' doit counts keys differ: ' + ','.join(sorted(counts)))
    for stage in STAGES:
        require(type(counts[stage]) is int, side + ' doit count ill-typed ' + stage + ': ' + type(counts[stage]).__name__)
    require('steps' in value, side + ' doit steps missing')
    require(isinstance(value['steps'], list), side + ' doit steps is not a list')
    for i, step in enumerate(value['steps']):
        validate_step(step, side + ' doit', i)
    tally = collections.Counter(step['stage'] for step in value['steps'])
    for stage in STAGES[:-1]:
        require(counts[stage] == tally[stage], side + ' doit count/Steps tally mismatch ' + stage)
    return value


def compare(expected, actual):
    lines = []
    different = False
    for stage in STAGES:
        a, b = expected['counts'][stage], actual['stages'][stage]
        if a == b:
            lines.append(f'stage={stage} doit={a} realm={b} ok')
        else:
            lines.append(f'DIFF stage={stage} doit={a} realm={b}')
            different = True
    left = [s for s in expected['steps'] if s['state'] == 'hold']
    right = list(actual['stuck'])
    # Match item first. A sole item-field mutation can still be diagnosed by
    # the other nine fields, without mistaking unrelated one-sided Steps.
    for a in left:
        match = next((b for b in right if a['item'] == b['item']), None)
        if match is None:
            matches = [b for b in right if all(a[f] == b[f] for f in FIELDS if f != 'item')]
            if len(matches) == 1:
                match = matches[0]
        if match is None:
            lines.append(f'DIFF stuck item={a["item"]} field=missing')
            different = True
            continue
        right.remove(match)
        fields = [f for f in FIELDS if a[f] != match[f]]
        if not fields:
            lines.append(f'stuck item={a["item"]} doit=present realm=present ok')
        for field in fields:
            lines.append(f'DIFF stuck item={a["item"]} field={field}')
            different = True
    for b in right:
        lines.append(f'DIFF stuck item={b["item"]} field=missing')
        different = True
    print('\n'.join(lines))
    return int(different)


def main():
    require(len(sys.argv) == 3, 'usage: realm_parity.sh <events-dir>')
    events = Path(sys.argv[1]).absolute()
    repo = Path(os.environ.get('DOIT_LIVE_REPO', '/home/albert/do-it-v2')).absolute()
    source = str(Path(sys.argv[2]).absolute())
    with tempfile.TemporaryDirectory(prefix='realm-parity-') as name:
        target = Path(name)
        pinned_copy(events, target)
        env = dict(os.environ, DOIT_ROOT=name, DOIT_READ_CACHE='0', PYTHONDONTWRITEBYTECODE='1')
        # Both calls and the pure realm import see only the frozen root.
        os.environ.update(DOIT_ROOT=name, DOIT_READ_CACHE='0', PYTHONDONTWRITEBYTECODE='1')
        expected = canonical(repo, env, 'expected')
        second = canonical(repo, env, 'realm')
        sys.path.insert(0, source)
        try:
            actual = importlib.import_module('realm').flow_view(second['steps'], live_count=second['counts']['live'])
        except Exception:
            raise Undetermined('realm flow_view raised or could not import')
        require(isinstance(actual, dict), 'realm flow_view is not a dict')
        require(isinstance(actual.get('stages'), dict), 'realm stages missing or not a dict')
        for stage in STAGES:
            require(stage in actual['stages'], 'realm missing stage ' + stage)
            require(actual['stages'][stage] is not None, 'realm null count ' + stage)
            require(type(actual['stages'][stage]) is int, 'realm non-int count ' + stage)
        require(isinstance(actual.get('stuck'), list), 'realm stuck is not a list')
        for i, step in enumerate(actual['stuck']):
            validate_step(step, 'realm stuck', i)
        return compare(expected, actual)


def interrupted(signum, frame):
    raise Undetermined('interrupted')


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
try:
    sys.exit(main())
except Undetermined as exc:
    print('UNDETERMINED: ' + str(exc))
    sys.exit(2)
except Exception:
    # Unexpected I/O/import faults are unknown, never evidence of parity.
    print('UNDETERMINED: snapshot or comparison could not be established')
    sys.exit(2)
PY
