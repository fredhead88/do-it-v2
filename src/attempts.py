"""Pure dispatch history for one subject, role and trigger (SD1)."""
from dataclasses import dataclass
from datetime import datetime, timedelta


BACKOFF_MIN = (5, 15, 45)
MAX_ATTEMPTS = 3
# Value contracts with lane_rules; deliberately no dispatch-stack import.
DISPATCH_EVENTS = frozenset({'spawn-started', 'autodispatched', 'spawn-failed', 'autodispatch-failed'})
TERMINAL_EVENTS = frozenset({'shipped', 'spec-killed', 'spec-closed'})
_OUTPUTS = {'grader': ('verdict', 'cannot-assess'), 'reviewer': ('review',),
            'builder': ('build-done', 'build-blocked'), 'spec-auditor': ('spawn-done',)}
_STARTS = frozenset({'spawn-started', 'build-started'})
_ATTEMPT_EVENTS = _STARTS | {'spawn-failed', 'spawn-done', 'spawn-stale'}
# Dispatch role defaults, kept here to preserve the pure helper import direction.
_MARKER_CAP_MIN = {'builder': 90, 'grader': 30, 'reviewer': 30, 'spec-auditor': 15}


@dataclass(frozen=True)
class Attempts:
    consumed: bool
    live: bool
    n_failed: int
    reasons: tuple[str, ...]
    next_retry_at: datetime | None
    exhausted: bool
    not_ready: str | None


def _timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a timezone-aware ISO-8601 string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone missing')
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{field} must be timezone-aware ISO-8601') from exc
    return parsed


def _role(event):
    explicit = event.get('role')
    if explicit:
        return explicit
    spawn = event.get('spawn')
    if isinstance(spawn, str) and spawn.startswith('L-'):
        middle, separator, number = spawn[2:].rpartition('-')
        if middle and separator and number.isdigit():
            return middle
    return 'builder' if event.get('type') == 'build-started' else None


def _reason(event):
    return event.get('reason') or event.get('why') or f"{event['type']} (no reason recorded)"


def _refusal(event):
    reason = str(_reason(event))
    return (event.get('type') == 'spawn-failed'
            and (reason.startswith(('bundle-pending', 'duplicate-dispatch', 'wave-order'))
                 or reason == 'dispatch-in-progress'
                 and (_role(event) != 'grader' or 'another dispatch holds the subject lock' in str(event.get('why') or ''))
                 or reason == 'branch-refreshed' or reason in __import__('branch_refresh').REFUSALS))


def _marker_groups(rows, role):
    """Join parent PID receipts to children before evaluating time or input order.

    Pre-start failures do not carry the parent's PID. Attribute only the next
    same-role receipt before another dispatch/start, as the launcher records it.
    """
    groups = {}
    for row in rows:
        e = row[2]
        if _role(e) == role and e.get('spawn') and e.get('type') in _ATTEMPT_EVENTS:
            groups.setdefault(e['spawn'], []).append(row)
    for row in rows:
        e = row[2]
        if e.get('type') == 'autodispatch-failed' and _role(e) == role and e.get('spawn') in groups:
            groups[e['spawn']].append(row)
    associated = set()
    markers = [r for r in rows if r[2].get('type') == 'autodispatched'
               and _role(r[2]) == role and r[2].get('spawn')]
    for marker in markers:
        pid = str(marker[2]['spawn'])
        match = next((spawn for spawn, group in groups.items()
                      if spawn == marker[2]['spawn'] or pid.isdecimal() and any(
                          r[2].get('type') in _STARTS and str(r[2].get('waiter_pid')) == pid
                          for r in group)), None)
        if match is None:
            following = [r for r in rows if (r[0], r[1]) > (marker[0], marker[1])
                         and _role(r[2]) == role and r[2].get('type') in
                         _STARTS | {'autodispatched', 'spawn-failed', 'autodispatch-failed'}]
            receipt = following[0] if following else None
            if (receipt and receipt[2]['type'] in {'spawn-failed', 'autodispatch-failed'}
                    and receipt[1] not in associated):
                associated.add(receipt[1])
                match = receipt[2].get('spawn') or ('receipt', receipt[1])
                if receipt[2]['type'] == 'autodispatch-failed':
                    groups.setdefault(match, []).append(receipt)
        if match is None:
            match = marker[2]['spawn']
        groups.setdefault(match, []).append(marker)
    for group in groups.values():
        group.sort(key=lambda r: (r[0], r[1]))
    return groups, associated


def history(evs, trigger, role, *, now, dead=frozenset()):
    """Return bounded retry state; chronological ownership is independent of input order."""
    if not isinstance(role, str) or role not in _OUTPUTS:
        raise ValueError('role must be grader, reviewer, builder or spec-auditor')
    if not isinstance(trigger, dict):
        raise ValueError('trigger must contain subject and ts')
    subject = trigger.get('subject')
    if not isinstance(subject, str) or not subject:
        raise ValueError('trigger.subject must be a non-empty string')
    threshold = _timestamp(trigger.get('ts'), 'trigger.ts')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')

    # Keep original indices solely for equal-time failure ordering. Never inspect
    # another subject's timestamp or any other fields beyond its subject.
    rows = [(_timestamp(e.get('ts'), 'event.ts'), i, e)
            for i, e in enumerate(evs) if e.get('subject') == subject]
    rows.sort(key=lambda row: (row[0], row[1]))
    groups, associated = _marker_groups(rows, role)
    older = {spawn for spawn, group in groups.items() if group[0][0] < threshold}
    groups = {spawn: group for spawn, group in groups.items() if spawn not in older}
    recent = [row for row in rows if row[0] >= threshold]
    outputs = _OUTPUTS[role]
    terminal_rows = [row for row in recent if row[2].get('type') in outputs
                     and row[2].get('spawn') not in older
                     and (role != 'spec-auditor' or _role(row[2]) == role)]
    consumed = bool(terminal_rows) or any(
        e.get('type') in TERMINAL_EVENTS | {'queue-add', 'merge-gate-clean'}
        for _, _, e in recent)
    output_spawns = {e.get('spawn') for _, _, e in terminal_rows}

    dead_ids = set(dead)
    stale_rows = {}
    for row in recent:
        e = row[2]
        kind = e.get('type')
        if kind == 'spawn-stale' and e.get('spawn'):
            dead_ids.add(e['spawn'])
            stale_rows.setdefault(e['spawn'], row)
        elif kind == 'autodispatched' and e.get('replaces'):
            dead_ids.add(e['replaces'])
        elif kind == 'escalation-blocking' and e.get('dead_spawn'):
            dead_ids.add(e['dead_spawn'])

    failures = []
    refusals = []
    starts = []
    for row in recent:
        _, _, e = row
        if _role(e) != role:
            continue
        if e.get('spawn') in older:
            continue
        if (e.get('type') == 'autodispatch-failed' and row[1] not in associated
                and e.get('spawn') not in groups):
            failures.append((row[0], row[1], _reason(e)))
        if e.get('type') in _STARTS:
            starts.append(row)
        if e.get('type') == 'spawn-failed' and _reason(e).startswith('bundle-pending'):
            refusals.append(row)

    live = False
    for spawn, group in groups.items():
        spawned = [r for r in group if r[2]['type'] in _STARTS]
        failed = [r for r in group if r[2]['type'] in {'spawn-failed', 'autodispatch-failed'}]
        real_failed = [r for r in failed if not _refusal(r[2])]
        markers = [r for r in group if r[2]['type'] == 'autodispatched']
        done = [r for r in group if r[2]['type'] == 'spawn-done']
        if real_failed:
            f = real_failed[0]
            failures.append((f[0], f[1], _reason(f[2])))
        elif spawn in dead_ids:
            f = stale_rows.get(spawn) or (spawned[0] if spawned else group[0])
            failures.append((f[0], f[1], f'dead: {spawn}'))
        elif done and spawn not in output_spawns and role != 'spec-auditor':
            f = done[0]
            failures.append((f[0], f[1], 'spawn-done without ' + '/'.join(outputs)))
        elif spawned and spawn not in output_spawns and not failed and not done:
            live = True
        elif markers and not spawned and not failed and not done and not consumed:
            marker = markers[0]
            deadline = marker[0] + timedelta(minutes=_MARKER_CAP_MIN[role])
            if now < deadline:
                live = True
            else:
                failures.append((deadline, marker[1], f'dead: {marker[2]["spawn"]}'))

    failures.sort(key=lambda row: (row[0], row[1]))
    n_failed = len(failures)
    live = live and not consumed
    exhausted = not consumed and not live and n_failed >= MAX_ATTEMPTS
    next_retry_at = None
    if not consumed and not live and 0 < n_failed < MAX_ATTEMPTS:
        next_retry_at = failures[-1][0] + timedelta(minutes=BACKOFF_MIN[n_failed - 1])
    not_ready = None
    if not consumed and not live and refusals:
        refused_at = refusals[-1][0]
        cleared = any(ts > refused_at and e.get('type') in {'evidence-bundle', 'evidence-bundle-failed'}
                      for ts, _, e in recent) or any(ts > refused_at for ts, _, _ in starts)
        if not cleared:
            not_ready = 'bundle-pending'
    return Attempts(consumed, live, n_failed, tuple(row[2] for row in failures),
                    next_retry_at, exhausted, not_ready)
