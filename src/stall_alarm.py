"""Pure journey clocks and named consumer readings for all ten journey steps.

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
STEPS = ('plan', 'dispatch', 'build', 'grade', 'review', 'merge', 'deploy',
         'owed_checks', 'close', 'autodispatch')
CENSUS_DWELL = dict.fromkeys(('written', 'building', 'blocked', 'graded', 'reviewing'), -1)
# Production route inventory: crons.toml auto-deploy. Target values live in TOML.
DEPLOY_PROJECTS = frozenset(('albert-scott',))
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


def read_consumers(events, now):
    """Read the shared census and owed checks from one admitted nonfuture fold.

    Reader exceptions are isolated and never replaced by healthy empty values.
    This adapter is outside the pure clocks seam; the census can read plans.
    Validate timestamps before readers whose timestamp helpers default to now.
    """
    import fold
    import owed

    def failure(name, exc):
        return dict(value=None, error=name + ': unavailable (' + type(exc).__name__ + ')')

    try:
        now = _time(now)
        if now is None or any(not isinstance(e, dict) or _time(e.get('ts')) is None for e in events):
            raise ValueError('invalid snapshot timestamp')
        snapshot = [e for e in events if _time(e['ts']) <= now]
        specs, _, _, by_subject = fold.fold(snapshot)
        admitted = sorted((e for evs in by_subject.values() for e in evs), key=lambda e: _time(e['ts']))
    except Exception as exc:
        return {name: failure(name, exc) for name in ('specs', 'stalls', 'owed')}
    result = dict(specs=dict(value=specs, error=None))
    try:
        result['stalls'] = dict(value=fold.stall_rows(admitted, specs, now=now,
                                      dwell=dict(CENSUS_DWELL), by_subject=by_subject), error=None)
    except Exception as exc:
        result['stalls'] = failure('stalls', exc)
    try:
        rows = []
        for spec in specs.values():
            # ONE spec at a time, even when several specs use the same AC id.
            for row in owed.checks(spec['evs'], now):
                movements = [e for e in spec['evs'] if e.get('type') == 'shipped' or
                             (e.get('criterion') == row['criterion'] and
                              (e.get('type', '').startswith('owed-') or e.get('type') == 'criterion-routed'))]
                moved = max((_time(e['ts']) for e in movements), default=None)
                rows.append(dict(row, last_moved=moved.isoformat() if moved else None))
        result['owed'] = dict(value=rows, error=None)
    except Exception as exc:
        result['owed'] = failure('owed', exc)
    return result


def _merge_wait(row):
    reason = row.get('reason', '')
    return isinstance(reason, str) and (reason.startswith('in merge window ') or
        reason.startswith('awaiting merge window after ') or reason == 'waited over 2 h for a merge window')


def _census(now, thresholds, readings):
    import fold_stalls
    rows = _reading(readings, 'stalls', list)
    specs = _reading(readings, 'specs', dict)
    items = {stage: [] for stage in ('build', 'grade', 'review')}
    excluded = dict(terminal=0, merge_waiting=0)
    defaults = dict(written='build', building='build', graded='grade', reviewing='review')
    roles = dict(builder='build', grader='grade', reviewer='review', **{'spec-auditor': 'build'})
    explicit = {'build-done', 'build-blocked', 'evidence-bundle', 'evidence-bundle-failed',
                'verdict', 'cannot-assess', 'conflict-rework', 'merge-conflict', 'window-dropped',
                'must-fix', 'rejected-criterion'}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('spec'), str):
            raise ValueError('stalls: invalid census row')
        if not isinstance(row.get('state'), str):
            raise ValueError('stalls: invalid lifecycle state')
        if row['state'] not in CENSUS_DWELL:
            excluded['terminal'] += 1
            continue
        if _merge_wait(row):
            excluded['merge_waiting'] += 1
            continue
        spec = specs.get(row['spec'])
        if not isinstance(spec, dict) or not isinstance(spec.get('evs'), list):
            raise ValueError('specs: missing census history')
        evs = spec['evs']
        if any(not isinstance(e, dict) or _time(e.get('ts')) is None for e in evs):
            raise ValueError('specs: invalid lifecycle timestamp')
        evs = sorted((e for e in evs if _time(e['ts']) <= now), key=lambda e: _time(e['ts']))
        trigger, role = fold_stalls.action(evs)
        stage = defaults.get(row['state'])
        if row.get('bucket') == 'in-flight':
            stage = roles.get(row.get('owner'))
        elif row['state'] == 'blocked' or trigger and (trigger['type'] in explicit or
                trigger['type'] == 'decision' and (trigger.get('regrade') == 'yes' or trigger.get('rework') == 'yes')):
            stage = roles.get(role)
        since = _time(row.get('since'))
        if stage is None or since is None or since > now:
            raise ValueError('stalls: unresolved stage or movement')
        item = dict(row, last_moved=since.isoformat(),
                    status='alarm' if (now-since).total_seconds() > thresholds[stage] else 'healthy')
        items[stage].append(item)
    return {stage: _row('alarm' if any(r['status'] == 'alarm' for r in group) else 'healthy',
                        thresholds[stage], 'shared census waits' if group else 'no pending stage work',
                        moved=min((_time(r['since']) for r in group), default=None),
                        pending=[r['spec'] for r in group], items=group, excluded_counts=dict(excluded),
                        excluded_reason='terminal states have no stage work; merge waits appear in merge.waiting')
            for stage, group in items.items()}


def _deploy(events, now, threshold, thresholds):
    targets = thresholds.get('deploy_targets')
    if not isinstance(targets, dict) or not DEPLOY_PROJECTS <= targets.keys() or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
            for k, v in targets.items()):
        raise ValueError('config: missing or invalid deploy target association')
    projects = {}
    for project, target in sorted(targets.items()):
        ships = _movement(events, now, lambda e: e.get('type') == 'shipped' and e.get('project') == project)
        lands = _movement(events, now, lambda e: e.get('type') == 'deploy-landed' and e.get('target') == target)
        landed = lands[-1][0] if lands else None
        pending = [(at, e) for at, e in ships if landed is None or at > landed]
        if any(not isinstance(e.get('subject'), str) or not e['subject'] for _, e in pending):
            raise ValueError('deploy: invalid shipped subject')
        projects[project] = (_age_clock(pending, now, threshold, [e['subject'] for _, e in pending],
                                        'newest shipment after target landing') if pending else
                             _row('healthy', threshold, 'no undeployed shipments', moved=landed))
        projects[project].update(target=target, landed_at=landed.isoformat() if landed else None)
    # Unconfigured projects have no production target; expose why their ships do not alarm.
    for _, event in _movement(events, now, lambda e: e.get('type') == 'shipped'):
        project = event.get('project')
        if not isinstance(project, str) or not project:
            raise ValueError('deploy: shipped project unavailable')
        if project not in projects:
            projects[project] = _row('not-applicable', threshold, 'no configured production deploy target')
    pending = [sid for row in projects.values() for sid in row['pending']]
    active = [row for row in projects.values() if row['pending']]
    return _row('alarm' if any(row['status'] == 'alarm' for row in active) else 'healthy',
                threshold, 'project/target deployment clocks', pending=pending,
                moved=min((_time(row['last_moved']) for row in active), default=None), projects=projects)


def _owed(now, threshold, readings):
    import owed
    rows = _reading(readings, 'owed', list)
    pending = []
    excluded = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('status') not in owed.STATUSES:
            raise ValueError('owed: invalid check row')
        if row['status'] != 'due':
            excluded[row['status']] = excluded.get(row['status'], 0) + 1
            continue
        due, moved = _time(row.get('due_at')), _time(row.get('last_moved'))
        if due is None or moved is None or moved > now or due > now or any(
                not isinstance(row.get(k), str) or not row[k] for k in ('spec', 'criterion', 'project')):
            raise ValueError('owed: invalid due check identity or timestamp')
        pending.append(dict(spec=row['spec'], criterion=row['criterion'], project=row['project'],
                            due_at=due.isoformat(), last_moved=moved.isoformat(),
                            status='alarm' if (now-due).total_seconds() > threshold else 'healthy'))
    return _row('alarm' if any(r['status'] == 'alarm' for r in pending) else 'healthy', threshold,
                'due owed checks' if pending else 'no due owed checks', pending=[r['spec'] for r in pending],
                moved=min((_time(r['last_moved']) for r in pending), default=None), items=pending, excluded_counts=excluded,
                excluded_reason='only status=due has an overdue check clock')


def failed_readings(readings, decisions):
    """Safe named diagnostics for unit three's grouped stall:undetermined body.

    This is prospective input only: it sends nothing and cannot clear an alarm.
    Arbitrary exception strings from envelopes are never copied into output.
    """
    failures = {}
    for name in ('ledger', 'config', 'state', 'ready', 'due', 'queue', 'stalls', 'specs', 'owed', 'autodispatch'):
        if name in readings:
            row = readings[name]
            if not isinstance(row, dict) or row.get('error') is not None or 'value' not in row:
                failures[name] = name + ': reading unavailable or malformed'
    for step, row in decisions.items():
        if row['status'] == 'undetermined':
            failures.setdefault(step, row['reason'])
    return failures


def clocks(events, now, *, state, thresholds, readings) -> dict[str, dict]:
    """Evaluate one UTC snapshot without IO; unknown readings never turn healthy.

    Events must already be normalized and admitted by the caller. Reader
    envelopes are ``{value: result, error: None}`` or ``{value: None, error: diagnostic}``.
    Future movement is ignored. All ten steps return decisions only;
    unit three consumes the result and persists prospective state after appends.
    """
    now = _time(now)
    invalid = [key for key in DURATIONS if not isinstance(thresholds, dict) or not _positive(thresholds.get(key))]
    if now is None or invalid or not isinstance(state, dict):
        if isinstance(state, dict):
            state['merge_samples'] = []
        reason = 'invalid now/state' if now is None or not isinstance(state, dict) else 'invalid duration: ' + ', '.join(invalid)
        return {step: _row('undetermined', None, reason) for step in STEPS}
    try:
        census = _census(now, thresholds, readings)
    except ValueError as exc:
        census = {stage: _row('undetermined', thresholds[stage], str(exc))
                  for stage in ('build', 'grade', 'review')}
    evaluators = dict(plan=lambda: _plan(events, now, thresholds, readings),
                      dispatch=lambda: _dispatch(events, now, thresholds['dispatch']),
                      close=lambda: _close(events, now, thresholds['close'], readings),
                      autodispatch=lambda: _autodispatch(now, thresholds, readings),
                      merge=lambda: _merge(now, thresholds['merge'], state, readings),
                      deploy=lambda: _deploy(events, now, thresholds['deploy'], thresholds),
                      owed_checks=lambda: _owed(now, thresholds['owed_checks'], readings),
                      **{stage: (lambda stage=stage: census[stage]) for stage in census})
    result = {}
    clock_thresholds = dict(plan=dict(idle=thresholds['plan_idle'], in_planning=thresholds['plan_in_planning']),
                            autodispatch=dict(slow=thresholds['autodispatch'], idle=thresholds['autodispatch_idle']),
                            **{key: thresholds[key] for key in ('dispatch', 'close', 'merge', 'build', 'grade', 'review', 'deploy', 'owed_checks')})
    for step, evaluator in evaluators.items():
        try:
            if isinstance(readings, dict):
                for source in ('config', 'ledger', 'state'):
                    if source == 'state' and step != 'merge' or source == 'ledger' and step == 'autodispatch':
                        continue
                    if source in readings:
                        _reading(readings, source, list if source == 'ledger' else dict)
            result[step] = evaluator()
        except ValueError as exc:
            if step == 'merge':
                state['merge_samples'] = []
            result[step] = _row('undetermined', clock_thresholds[step], str(exc))
    if isinstance(readings, dict) and isinstance(readings.get('stalls'), dict) and isinstance(readings['stalls'].get('value'), list):
        result['merge']['waiting'] = [dict(row) for row in readings['stalls']['value']
                                      if isinstance(row, dict) and _merge_wait(row)]
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
