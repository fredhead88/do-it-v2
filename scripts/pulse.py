#!/usr/bin/python3
"""Read-only system picture. Delivery belongs to the invoking poller."""
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
os.environ['DOIT_ROOT'] = str(ROOT)
START_NOW = os.environ.get('DOIT_NOW')


def clock():
    try:
        now = (datetime.fromisoformat(START_NOW.replace('Z', '+00:00'))
               if START_NOW is not None else datetime.now(timezone.utc))
        if now.utcoffset() is None:
            raise ValueError
        return now.astimezone(ZoneInfo('Asia/Jerusalem'))
    except (ValueError, OverflowError):
        raise SystemExit('pulse: invalid DOIT_NOW') from None


NOW = clock()
sys.path.insert(0, str(REPO / 'src'))
import fold

CHARTERS = [('0043', 'read model'), ('0044', 'DB capacity'), ('0045', 'front end'),
            ('0046', 'Proving status'), ('0047', 'dossiers')]
LOCAL_LINK = re.compile(
    r'file://[^\s]+|https?://(?:localhost|127\.0\.0\.1|\[::1\])(?=[:/\s]|$)(?::\d+)?(?:/[^\s]*)?|(?<![\w:/])/(?!/)[^\s]+',
    re.IGNORECASE)
SECRETS = re.compile(r'\b(token|password|secret)\s*[:=]\s*[^\s]+|\bBearer\s+[^\s]+', re.IGNORECASE)
REMOTE = ('python3 -c \'import json; print(json.load(open("/opt/albert-scott/.version.json"))["sha"][:8])\'; '
          'journalctl -u bluedot-webhook --since "20 min ago" | grep -c " 500 "')


def safe(text):
    text = LOCAL_LINK.sub('[local link omitted]', text)
    text = re.sub(r'PULSE_SECRET_[A-Z0-9_]+', '[redacted]', text)
    return SECRETS.sub('[redacted]', text)


def sh(cmd, t=25, *, env=None, cwd=None):
    """Successful empty output is an observation; failed partial output is not."""
    try:
        result = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                                text=True, timeout=t, env=env, cwd=cwd)
        if result.returncode == 0:
            return 'ok', result.stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return 'unread', ''


def child_env():
    env = {**os.environ, 'DOIT_ROOT': str(ROOT), 'DOIT_READ_CACHE': '0'}
    if START_NOW is None:
        env.pop('DOIT_NOW', None)
    else:
        env['DOIT_NOW'] = START_NOW
    return env


def asks_lines():
    try:
        text = (ROOT / 'state/operator-asks.txt').read_text(encoding='utf-8')
        asks = [safe(line.strip()) for line in text.splitlines() if line.strip() and not line.startswith('#')]
    except (OSError, UnicodeError):
        return ['WAITING ON YOU: unread (asks file unreadable)']
    if not asks:
        return ['WAITING ON YOU: 0 (nothing; anything not listed here is not waiting on you)']
    return [f'WAITING ON YOU: {len(asks)}'] + [f' {i}. {ask[:200]}' for i, ask in enumerate(asks, 1)]


def board_lines():
    status, text = sh([str(REPO / 'doit'), 'board'], 120, env=child_env(), cwd=REPO)
    match = re.search(r'^## NEEDS YOU \([0-9]+\)[ \t]*$', text, re.MULTILINE)
    if status != 'ok' or match is None:
        return ['(Thinker escalation count: unread; not yours.)']
    section = text[match.end():].split('\n## ', 1)[0]
    count = sum(bool(line.strip()) for line in section.splitlines())
    return [f'(Thinker is handling {count} other escalation(s); not yours.)'] if count else []


def omen_lines():
    status, text = sh([sys.executable, str(ROOT / 'omens.py')], 60, env=child_env())
    records = [tuple(part.strip() for part in line.split('|', 2))
               for line in text.splitlines() if line.strip()]
    if status != 'ok' or any(len(row) != 3 or not all(row) for row in records):
        return ['STUCK: unread', 'WATCH: unread']
    stuck = [detail for severity, name, detail in records if severity == 'WARN' and name == 'shelf-stuck']
    watch = [detail for severity in ('CRIT', 'WARN') for sev, name, detail in records
             if sev == severity and not (sev == 'WARN' and name == 'shelf-stuck')]
    lines = [f'STUCK: {len(stuck)}'] + [' - ' + safe(detail)[:110] for detail in stuck]
    if watch:
        lines += ['WATCH:'] + [' - ' + safe(detail)[:110] for detail in watch[:5]]
    return lines


def portal_lines():
    try:
        # fold treats a missing events directory as empty; pulse must distinguish it.
        with os.scandir(ROOT / 'events') as entries:
            for entry in entries:
                if entry.name.endswith('.jsonl') and entry.is_file():
                    with open(entry.path, encoding='utf-8') as source:
                        for _ in source:
                            pass
        specs = fold.fold(fold.read_events(use_cache=False))[0]
    except Exception:
        specs = None
    lines = ['PORTAL SPEED (goal: every page < 2s)']
    for number, name in CHARTERS:
        tail = 'unread'
        if specs is not None:
            matched = [s for s in specs.values() if Path(str(next(
                (e['charter'] for e in reversed(s['evs']) if e.get('charter')), ''))).stem == 'L-charter-' + number]
            shipped = sum(s['state'].startswith(('shipped', 'accepted')) for s in matched)
            working = sum(not s['state'].startswith(('shipped', 'accepted', 'written', 'killed',
                                                    'void', 'dropped', 'closed')) for s in matched)
            tail = f'{shipped}/{len(matched)} shipped, {working} in work'
        lines.append(f' {number} {name:<15} {tail}')
    return lines


def box_lines():
    status, text = sh(['ssh', '-o', 'ConnectTimeout=8', 'root@167.71.46.51', REMOTE], 30)
    fields = text.split() if status == 'ok' else []
    build = fields[0] if fields and re.fullmatch(r'[0-9a-fA-F]{8}', fields[0]) else 'unread'
    errors = fields[1] if len(fields) > 1 and re.fullmatch(r'[0-9]+', fields[1]) else 'unread'
    status, text = sh('df / --output=pcent | tail -1')
    disk = text if status == 'ok' and re.fullmatch(r'[0-9]+%', text) else 'unread'
    try:
        value = os.getloadavg()[0]
        load = f'{value:.1f}' if math.isfinite(value) else 'unread'
    except (OSError, ValueError, TypeError, IndexError):
        load = 'unread'
    return [f'PRODUCTION: build {build}, errors(20m) {errors}', f'BOX: disk {disk}, load {load}']


def lane_lines():
    path = ROOT / 'state/pulse-lanes.txt'
    try:
        text = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        return ['', 'LANES: unread']
    return ['', safe(text)]


def main():
    lines = [f'PULSE {NOW:%H:%M} {NOW:%a %d %b}', '']
    # Keep the visible picture order independent of the order of observations.
    omens = omen_lines()
    lines += asks_lines() + board_lines() + omens
    lines += [''] + portal_lines() + [''] + box_lines() + lane_lines()
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
