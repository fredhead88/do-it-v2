"""Pure journey clocks, unit one: plan, dispatch, close, autodispatch and merge.

``clocks`` consumes an admitted event snapshot and named reading envelopes. Its
only mutation is prospective merge continuity in ``state['merge_samples']``;
the eventual runner owns atomic persistence and alarm delivery. Plan groups
its two predicates under one clock, retaining each predicate's threshold and
movement. No clock uses file mtime, process uptime or observation as movement.
"""
from datetime import datetime, timezone
import math
from pathlib import Path
import re

import spawns

UTC = timezone.utc
DURATIONS = ('plan_idle', 'plan_in_planning', 'dispatch', 'close', 'autodispatch',
             'autodispatch_idle', 'merge', 'build', 'grade', 'review', 'deploy',
             'owed_checks', 'realert')
STEPS = ('plan', 'dispatch', 'close', 'autodispatch', 'merge')
MERGE_MAX_GAP_S = 1800  # Two scheduled 15-minute observations.


def _time(value):
    try:
        point = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if point.tzinfo is None or point.utcoffset() is None:
            return None
        return point.astimezone(UTC)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _row(status, threshold, reason, *, moved=None, pending=(), **extra):
    return dict(status=status, last_moved=moved.isoformat() if moved else None,
                pending=sorted(set(pending)), threshold_s=threshold, reason=reason, **extra)


def _reading(readings, name, expected):
    row = readings.get(name) if isinstance(readings, dict) else None
    if not isinstance(row, dict) or 'value' not in row or 'error' not in row:
        raise ValueError(name + ': missing reading envelope')
    if row['error'] is not None:
        # Do not echo arbitrary reader diagnostics (which can contain log text).
        raise ValueError(name + ': reading failed')
    value = row['value']
    if not isinstance(value, expected):
        raise ValueError(name + ': invalid reading shape')
    return value


def _ids(readings, name):
    value = _reading(readings, name, list)
    if any(not isinstance(v, str) or not v for v in value):
        raise ValueError(name + ': invalid pending ids')
    return sorted(set(value))


def _movement(events, now, predicate):
    points = []
    for event in events:
        if not predicate(event):
            continue
        point = _time(event.get('ts'))
        if point is None:
            raise ValueError('invalid relevant event timestamp')
        if point <= now:
            points.append((point, event))
    return sorted(points, key=lambda pair: pair[0])


def _age_clock(points, now, threshold, pending, reason):
    moved = points[-1][0] if points else None
    if moved is None:
        return _row('undetermined', threshold, 'no prior movement', pending=pending)
    return _row('alarm' if (now - moved).total_seconds() > threshold else 'healthy',
                threshold, reason, moved=moved, pending=pending)


def _plan(events, now, thresholds, readings):
    ready = _ids(readings, 'ready')
    idle_s, planning_s = thresholds['plan_idle'], thresholds['plan_in_planning']
    if not ready:
        idle = _row('healthy', idle_s, 'no ready charters')
        planning = _row('healthy', planning_s, 'no ready charters')
    else:
        movements = _movement(events, now, lambda e: e.get('type') in ('plan-written', 'cut-written'))
        idle = _age_clock(movements, now, idle_s, ready, 'newest admitted plan/cut')
        sequences = _movement(events, now, lambda e: e.get('subject') in ready and e.get('type') in (
            'planner-started', 'planner-ended', 'plan-written', 'l1-complete', 'charter-retracted'))
        active = {}
        for at, event in sequences:
            cid = event['subject']
            if event['type'] == 'planner-started':
                active[cid] = at
            else:
                active.pop(cid, None)
        candidates = [dict(charter=cid, last_moved=at.isoformat(),
                           status='alarm' if (now - at).total_seconds() > planning_s else 'healthy')
                      for cid, at in sorted(active.items())]
        planning = _row('alarm' if any(c['status'] == 'alarm' for c in candidates) else 'healthy',
                        planning_s, 'active ready planning' if active else 'no active ready planning',
                        moved=min(active.values()) if active else None, pending=active,
                        candidates=candidates)
    statuses = (idle['status'], planning['status'])
    # Preserve both predicates even if an alarm is already proven by the other.
    status = 'alarm' if 'alarm' in statuses else 'undetermined' if 'undetermined' in statuses else 'healthy'
    return dict(status=status, last_moved=idle['last_moved'], pending=ready,
                threshold_s=dict(idle=idle_s, in_planning=planning_s),
                reason='grouped planner predicates', predicates=dict(idle=idle, in_planning=planning))


def _dispatch(events, now, threshold):
    starts = _movement(events, now, lambda e: e.get('type') in spawns.START_TYPES)
    return _age_clock(starts, now, threshold, (), 'newest admitted spawn-started/build-started')


def _close(events, now, threshold, readings):
    due = _ids(readings, 'due')
    if not due:
        return _row('healthy', threshold, 'no close reviews due')
    starts = _movement(events, now, lambda e: e.get('type') in spawns.START_TYPES
                       and spawns._role(e) == 'charter-reviewer')
    return _age_clock(starts, now, threshold, due, 'newest charter-reviewer start')


def _autodispatch(now, thresholds, readings):
    rows = _reading(readings, 'autodispatch', list)
    completed = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('autodispatch: invalid completion')
        at, elapsed = _time(row.get('ts')), row.get('elapsed_s')
        if at is None or type(elapsed) not in (float, int) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('autodispatch: invalid completion')
        if at <= now:
            completed.append((at, elapsed))
    completed.sort(key=lambda pair: pair[0])
    newest = completed[-3:]
    slow_s, idle_s = thresholds['autodispatch'], thresholds['autodispatch_idle']
    threshold = dict(slow=slow_s, idle=idle_s)
    if not newest:
        return _row('undetermined', threshold, 'no completed passes')
    moved = newest[-1][0]
    stale = (now - moved).total_seconds() > idle_s
    slow = len(newest) == 3 and all(elapsed > slow_s for _, elapsed in newest)
    if stale or slow:
        status, reason = 'alarm', 'stale completion' if stale else 'three consecutive slow completed passes'
    elif len(newest) < 3:
        status, reason = 'undetermined', 'insufficient three-pass history'
    else:
        status, reason = 'healthy', 'recent completion and no slow triple'
    return _row(status, threshold, reason, moved=moved,
                ignored_future_completions=sum(_time(row['ts']) > now for row in rows),
                completed=[dict(ts=at.isoformat(), elapsed_s=s) for at, s in newest])


def _merge(now, threshold, state, readings):
    try:
        queue = _reading(readings, 'queue', list)
        specs = _reading(readings, 'specs', dict)
        if any(not isinstance(sid, str) or not isinstance(spec, dict) or not isinstance(spec.get('state'), str)
               for sid, spec in specs.items()):
            raise ValueError('specs: invalid folded spec')
        if any(not isinstance(row, dict) or not isinstance(row.get('spec'), str) for row in queue):
            raise ValueError('queue: invalid queue row')
        building = sorted(sid for sid, spec in specs.items() if spec['state'] == 'building')
        if queue or not building:
            state['merge_samples'] = []
            return _row('healthy', threshold, 'nonempty queue' if queue else 'no building work', pending=building)
        raw = state.get('merge_samples', [])
        if not isinstance(raw, list) or any(_time(value) is None for value in raw):
            raise ValueError('state: invalid merge samples')
        points = sorted(set(_time(value) for value in raw if _time(value) <= now))
        # Only the suffix of consecutive samples proves current continuity.
        start = 0
        for index in range(1, len(points)):
            if (points[index] - points[index - 1]).total_seconds() > MERGE_MAX_GAP_S:
                start = index
        points = points[start:]
        if points and (now - points[-1]).total_seconds() > MERGE_MAX_GAP_S:
            points = []
        if not points or points[-1] != now:
            points.append(now)
        state['merge_samples'] = [at.isoformat() for at in points]
        return _row('alarm' if (now - points[0]).total_seconds() > threshold else 'healthy',
                    threshold, 'continuous empty queue observations', moved=points[0], pending=building,
                    ignored_future_samples=sum(_time(value) > now for value in raw),
                    samples=list(state['merge_samples']))
    except ValueError:
        state['merge_samples'] = []
        raise


def clocks(events, now, *, state, thresholds, readings) -> dict[str, dict]:
    """Evaluate one UTC snapshot without IO; unknown readings never turn healthy.

    Events must already be normalized and admitted by the caller. Reader
    envelopes are ``{value: result, error: None}`` or ``{value: None, error: diagnostic}``.
    Future movement is ignored. Unit two extends this mapping with its clocks;
    unit three consumes the result and persists prospective state after appends.
    """
    now = _time(now)
    invalid = [key for key in DURATIONS if not isinstance(thresholds, dict) or not _positive(thresholds.get(key))]
    if now is None or invalid or not isinstance(state, dict):
        if isinstance(state, dict):
            state['merge_samples'] = []
        reason = 'invalid now/state' if now is None or not isinstance(state, dict) else 'invalid duration: ' + ', '.join(invalid)
        return {step: _row('undetermined', None, reason) for step in STEPS}
    evaluators = dict(plan=lambda: _plan(events, now, thresholds, readings),
                      dispatch=lambda: _dispatch(events, now, thresholds['dispatch']),
                      close=lambda: _close(events, now, thresholds['close'], readings),
                      autodispatch=lambda: _autodispatch(now, thresholds, readings),
                      merge=lambda: _merge(now, thresholds['merge'], state, readings))
    result = {}
    clock_thresholds = dict(plan=dict(idle=thresholds['plan_idle'], in_planning=thresholds['plan_in_planning']),
                            autodispatch=dict(slow=thresholds['autodispatch'], idle=thresholds['autodispatch_idle']),
                            **{key: thresholds[key] for key in ('dispatch', 'close', 'merge')})
    for step, evaluator in evaluators.items():
        try:
            result[step] = evaluator()
        except ValueError as exc:
            result[step] = _row('undetermined', clock_thresholds[step], str(exc))
    future = sum(_time(e.get('ts')) is not None and _time(e['ts']) > now for e in events)
    for row in result.values():
        row['ignored_future_events'] = future
        row['ignored_future_reason'] = 'future timestamps cannot reset movement'
    return result


def read_autodispatch(root, now):
    """Read a coherent rotated/live completion snapshot into a named envelope.

    Rotation or append during the read fails this observation; a later scheduled
    pass retries. Missing .1 is normal; missing live and unreadable .1 are not.
    Identical completed lines are deduplicated across the rotation boundary.
    """
    live = Path(root) / 'logs/lane-driver.log'
    rotated = live.with_suffix('.log.1')

    def stamp(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not info.st_mode & 0o444:
            raise OSError('unreadable log')
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    try:
        now = _time(now)
        if now is None:
            raise ValueError('invalid observation time')
        before = [stamp(rotated), stamp(live)]
        if before[1] is None:
            raise OSError('missing live log')
        texts = [path.read_text() if signature is not None else ''
                 for path, signature in zip((rotated, live), before)]
        if before != [stamp(rotated), stamp(live)]:
            raise OSError('log changed during snapshot')
        completed, seen = [], set()
        ignored = dict(partial=0, not_completed=0, duplicate=0, future=0, older_history=0)
        for text in texts:
            for line in text.splitlines(keepends=True):
                if not line.endswith('\n'):
                    ignored['partial'] += 1
                    continue
                if 'finished autodispatch elapsed_s=' not in line:
                    ignored['not_completed'] += 1
                    continue
                if line in seen:
                    ignored['duplicate'] += 1
                    continue
                seen.add(line)
                match = re.fullmatch(r'(\S+) snapshot=\S+ finished autodispatch elapsed_s=(\S+)\n', line)
                if match is None:
                    raise ValueError('invalid completed line')
                at, duration = _time(match[1]), float(match[2])
                if at is None or not math.isfinite(duration) or duration < 0:
                    raise ValueError('invalid completed line')
                if at <= now:
                    completed.append(dict(ts=at.isoformat(), elapsed_s=duration))
                else:
                    ignored['future'] += 1
        completed.sort(key=lambda row: row['ts'])
        ignored['older_history'] = max(0, len(completed) - 3)
        return dict(value=completed[-3:], error=None, ignored=ignored)
    except (OSError, ValueError, UnicodeError, OverflowError) as exc:
        return dict(value=None, error='autodispatch log snapshot unavailable: ' + type(exc).__name__)
