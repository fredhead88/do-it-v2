#!/usr/bin/env python3
"""Retained host resource and local allowance samples (SD6)."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile

import seat_usage

SAFE_CEILING = {'disk': 85, 'ram': 90}
WINDOWS = {'claude_5h': timedelta(hours=5), 'claude_7d': timedelta(days=7),
           'codex_7d': timedelta(days=7)}
MEMINFO = Path('/proc/meminfo')
DISK_PATH = '/'
RETENTION = timedelta(days=10)
_VALUES = ('disk_used_pct', 'disk_total_gb', 'ram_used_pct', 'swap_used_pct',
           'claude_5h_pct', 'claude_5h_reset', 'claude_7d_pct', 'claude_7d_reset',
           'codex_7d_pct', 'codex_7d_reset')


def _root(root=None):
    return Path(root if root is not None else os.environ.get('DOIT_ROOT', Path.home()/'.do-it'))


def _time(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pct(value):
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _reset(value):
    try:
        return _iso(value)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _empty(now, source='live'):
    return dict(ts=_iso(now), **dict.fromkeys(_VALUES), source=source)


def pace_pct(reset_at, window, at):
    if window <= timedelta(0):
        raise ValueError('window must be positive')
    return 100 * (_time(at) - (_time(reset_at)-window)) / window


def _memory():
    try:
        return {parts[0].rstrip(':'): float(parts[1]) for line in MEMINFO.read_text().splitlines()
                if len(parts := line.split()) >= 2}
    except (OSError, ValueError):
        return {}


def _used(total, free):
    if total is None or free is None or total <= 0:
        return None
    return _pct(100 * (1-free/total))


def _json_lines(path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except (ValueError, TypeError):
            continue
    return rows


def _rollouts():
    directory = Path.home()/'.codex/sessions'
    # Missing is an absent origin; other traversal errors must reach backfill.
    def scan(folder):
        try:
            entries = list(folder.iterdir())
        except FileNotFoundError:
            return []
        paths = []
        for entry in entries:
            if entry.is_dir():
                paths.extend(scan(entry))
            elif entry.name.startswith('rollout-') and entry.suffix == '.jsonl':
                paths.append(entry)
        return paths
    return scan(directory)


def _weekly(row):
    """Return a weekly block, preserving an invalid latest reading as unknown."""
    try:
        payload = row['payload']
        if row['type'] != 'event_msg' or payload['type'] != 'token_count':
            return None
        for name in ('primary', 'secondary'):
            block = payload['rate_limits'].get(name)
            if isinstance(block, dict) and block.get('window_minutes') == 10080:
                return _time(row['timestamp']), block
    except (KeyError, TypeError, AttributeError, ValueError):
        pass
    return None


def _codex_fields(block, at):
    try:
        reset = datetime.fromtimestamp(float(block['resets_at']), timezone.utc)
        if reset <= at:
            return dict(codex_7d_pct=None, codex_7d_reset=None)
        return dict(codex_7d_pct=_pct(block.get('used_percent')), codex_7d_reset=_iso(reset))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return dict(codex_7d_pct=None, codex_7d_reset=None)


def _claude_fields(reading):
    return {f'claude_{short}_{suffix}': convert(reading.get(long+field))
            for short, long in (('5h', 'five_hour'), ('7d', 'seven_day'))
            for suffix, field, convert in (('pct', '_pct', _pct), ('reset', '_resets_at', _reset))}


def sample(now, *, root=None):
    root = _root(root)
    now = _time(now)
    row = _empty(now)
    try:
        total, used, free = shutil.disk_usage(DISK_PATH)
        row.update(disk_used_pct=_pct(100*used/(used+free)) if used+free else None,
                   disk_total_gb=round(total/2**30, 1))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    memory = _memory()
    row['ram_used_pct'] = _used(memory.get('MemTotal'), memory.get('MemAvailable'))
    row['swap_used_pct'] = _used(memory.get('SwapTotal'), memory.get('SwapFree'))
    reading = seat_usage.latest(root=root, now=now)
    if reading is not None:
        row.update(_claude_fields(reading))
    try:
        paths = sorted(_rollouts(), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        readings = [weekly for path in paths for record in _json_lines(path)
                    if (weekly := _weekly(record)) is not None]
        if readings:
            row.update(_codex_fields(max(readings, key=lambda pair: pair[0])[1], now))
    except OSError:
        pass
    return row


def _read_samples(path, since):
    rows = []
    for row in _json_lines(path):
        try:
            if _time(row['ts']) >= since:
                rows.append(row)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return sorted(rows, key=lambda row: _time(row['ts']))


def samples(since, *, root=None):
    return _read_samples(_root(root)/'state/pace-samples.jsonl', _time(since))


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _replace(path, rows):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name+'.', delete=False) as file:
            name = file.name
            for row in rows:
                file.write(json.dumps(row, allow_nan=False, separators=(',', ':'))+'\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def _record(path, row, now):
    with _locked(path):
        # Remove a previous torn tail before the single append write.
        _replace(path, _read_samples(path, now-RETENTION))
        line = (json.dumps(row, allow_nan=False, separators=(',', ':'))+'\n').encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError('short sample write')
        finally:
            os.close(fd)
    return row


def record(now=None, *, root=None):
    now = _time(now if now is not None else datetime.now(timezone.utc))
    root = _root(root)
    return _record(root/'state/pace-samples.jsonl', sample(now, root=root), now)


def _slot(at):
    return at.replace(minute=at.minute//10*10, second=0, microsecond=0)


def _origins(root):
    """Yield (origin, timestamp, fields); malformed source records are absent."""
    for row in _json_lines(root/'events/L-seat-usage-local.jsonl'):
        if row.get('type') == 'seat-usage':
            try:
                yield 'seat-usage', _time(row['observed_at']), _claude_fields(row)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    for path in _rollouts():
        for row in _json_lines(path):
            weekly = _weekly(row)
            if weekly is not None:
                at, block = weekly
                yield 'codex-rollout', at, _codex_fields(block, at)
    history = root/'cache/realm-history.json'
    if not history.exists():
        history = root/'realm-history.json'
    try:
        disk = json.loads(history.read_text()).get('disk', [])
    except (FileNotFoundError, ValueError, AttributeError):
        disk = []
    try:
        total, used, free = shutil.disk_usage(DISK_PATH)
        basis = (used+free)/total
    except (OSError, ValueError, ZeroDivisionError):
        basis = None
    for point in disk:
        try:
            at, pct = point
            number = _pct(pct)
            yield 'realm-history', _time(at), dict(disk_used_pct=_pct(number/basis)
                if number is not None and basis else None)
        except (TypeError, ValueError, AttributeError):
            continue
    memory = _memory()
    for path in sorted((root/'events').glob('*.jsonl')):
        for row in _json_lines(path):
            if row.get('type') != 'tick' or not isinstance(row.get('admission_readings'), dict):
                continue
            try:
                at = _time(row['admission_sampled_at'])
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            values = {}
            for key, total_key, free_key in (('ram_used_pct','MemTotal','mem_avail_gb'),
                                             ('swap_used_pct','SwapTotal','swap_free_gb')):
                total = memory.get(total_key)
                free = _number(row['admission_readings'].get(free_key))
                values[key] = _used(total/2**20 if total is not None else None, free)
            yield 'admission', at, values


def backfill(*, root=None, now=None):
    root = _root(root)
    now = _time(now if now is not None else datetime.now(timezone.utc))
    path = root/'state/pace-samples.jsonl'
    with _locked(path):
        existing = _read_samples(path, now-RETENTION)
        boundary = min((_time(r['ts']) for r in existing if r.get('source') == 'live'), default=_slot(now))
        slots = {}
        for origin, at, fields in _origins(root):
            slot = _slot(at)
            if slot < now-RETENTION or slot >= boundary:
                continue
            bucket = slots.setdefault(slot, {})
            if origin not in bucket or at >= bucket[origin][0]:
                bucket[origin] = (at, fields)
        occupied = {_time(row['ts']) for row in existing}
        added, origins = [], set()
        for slot, readings in sorted(slots.items()):
            if slot in occupied:
                continue
            row = _empty(slot, 'backfill:'+'+'.join(sorted(readings)))
            for origin, (_, fields) in readings.items():
                row.update(fields)
                origins.add(origin)
            added.append(row)
        _replace(path, sorted(existing+added, key=lambda row: _time(row['ts'])))
        _replace(root/'state/pace-backfill.json', [dict(ts=_iso(now), rows_added=len(added), origins=sorted(origins))])
    return dict(rows_added=len(added), origins=sorted(origins))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('sample', 'backfill'))
    args = parser.parse_args(argv)
    if args.command == 'backfill':
        try:
            print(json.dumps(backfill()))
            return 0
        except Exception as error:
            print(f'backfill: {error}', file=sys.stderr)
            return 1
    failed = False
    if not (_root()/'state/pace-backfill.json').exists():
        try:
            backfill()
        except Exception as error:
            failed = True
            print(f'backfill: {error}', file=sys.stderr)
    try:
        print(json.dumps(record()))
    except Exception as error:
        print(f'sample: {error}', file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
