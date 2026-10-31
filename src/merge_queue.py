"""Ledger-owned merge queue and the tick's gate/enqueue actions."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import dispatch
import fold
import lane_rules
import tick
import window_gate

DOIT = str(Path(__file__).resolve().parent.parent / 'doit')
OUTCOMES = {'shipped', 'window-dropped', 'merge-conflict'}
TERMINALS = OUTCOMES | {'spec-killed', 'spec-closed'}
BATCH_MIN_QUEUE = 2
GATE_PER_TICK = 2
MIGRATION_ORDER_SOURCE = 'queue'


def _ordered(events):
    # Validate even events that admission would subsequently exclude.
    for event in events:
        try:
            lane_rules._time(event.get('ts'))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('event timestamp undetermined') from exc
    # Stable sorting preserves ledger position at equal timestamps.
    return sorted((e for e in events if 'actor' not in e or
                   fold.admits(e, e['actor']) is None), key=lambda e: lane_rules._time(e['ts']))


def _latest(events, kinds):
    return next((e for e in reversed(events) if e.get('type') in kinds), None)


def queue(events, project=None, *, _include_terminal=False):
    """Fold corrections without consulting a mutable script or the wall clock."""
    live, batches = {}, {}
    for pos, e in enumerate(_ordered(events)):
        sid, kind = e.get('subject'), e.get('type')
        if kind == 'queue-add':
            live.pop(sid, None)
            if e.get('branch'):
                live[sid] = (pos, dict(spec=sid, project=e.get('project'), branch=e['branch'],
                                      added_at=e['ts'], batch=None))
        elif kind == 'queue-remove' or (kind in TERMINALS and not _include_terminal):
            live.pop(sid, None)
        elif kind == 'queue-batch':
            for spec in e.get('specs', '').split(','):
                batches[spec] = sid
    return [dict(row, batch=batches.get(sid)) for sid, (_, row) in
            sorted(live.items(), key=lambda item: item[1][0])
            if project is None or row['project'] == project]


def next_candidate(events):
    """The head of the sole ready fold, with one outstanding window at most."""
    events = _ordered(events)
    if _launching(events, window_gate.PROJECT, window_gate.in_flight(events, root=fold.ROOT)):
        return None
    rows = queue(events)
    return rows[0]['spec'] if rows else None


def batching_allowed(events, queue_len):
    """Orderable ready work can batch immediately, without historical permission."""
    _ordered(events)
    return queue_len >= BATCH_MIN_QUEUE


def _emit(sid, kind, **fields):
    dst = tick.tick_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    reason = dispatch.emit(dst, {'subject': sid}, kind, **fields)
    if reason:
        raise ValueError(reason)
    # dispatch.emit verifies bytes; confirm the requested event independently.
    landed = [json.loads(line) for line in dst.read_text().splitlines()]
    if not any(e.get('type') == kind and e.get('subject') == sid and
               all(e.get(k) == v for k, v in fields.items()) for e in landed):
        raise ValueError(f'{kind} append not confirmed')


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)


def _repository(project):
    if not project or Path(project).name != project or project in ('.', '..'):
        raise ValueError('repository project unresolved')
    repo = fold.ROOT / 'repos' / project
    top = _git(repo, 'rev-parse', '--show-toplevel')
    if top.returncode or Path(top.stdout.strip()).resolve() != repo.resolve():
        raise ValueError(f'repository unresolved: {repo}')
    head = _git(repo, 'symbolic-ref', '--short', 'HEAD')
    if head.returncode or not head.stdout.strip():
        raise ValueError(f'HEAD branch undetermined: {repo}')
    return repo, head.stdout.strip()


def _base_blocks(written, sid, repo, head):
    path = Path(written.get('path') or fold.ROOT / 'content' / f'{sid}.md')
    if not path.is_absolute():
        path = fold.ROOT / path
    text = path.read_text()
    for capture in re.findall(r'^Base: ([A-Za-z0-9._/-]+)$', text, re.M):
        result = _git(repo, 'rev-parse', '--verify', 'refs/heads/' + capture)
        if result.returncode:
            # rev-parse conflates a missing ref with operational errors. A
            # quiet show-ref distinguishes absent local branches from failure.
            check = _git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + capture)
            if check.returncode == 1:
                continue
            raise ValueError(f'Base branch check failed: {capture}')
        if capture != head:
            return capture
    return None


def _reconcile(events, *, dry_run=False, ledger_file=None):
    out = []
    for row in queue(events, _include_terminal=True):
        evs = [e for e in events if e.get('subject') == row['spec']]
        added = _latest(evs, {'queue-add'})
        outcome = _latest(evs, TERMINALS if ledger_file is None else OUTCOMES)
        if not outcome or evs.index(outcome) <= evs.index(added):
            continue
        reason = outcome['type'] if outcome['type'] in OUTCOMES else 'killed'
        out.append(f"{'would remove' if dry_run else 'removed'} 1 spec {row['spec']} reason={reason}")
        if not dry_run:
            if ledger_file is None:
                _emit(row['spec'], 'queue-remove', project=row['project'], reason=reason)
            else:
                _train_emit(ledger_file, row['spec'], 'queue-remove', project=row['project'], reason=reason)
    return out


def enqueue_due(events, now, *, runner=None, dry_run=False):
    """Own gate/enqueue actions and their one retry; limits belong to due rows."""
    from autodispatch import _real_runner
    runner = runner or _real_runner
    out = []
    try:
        events = _ordered(events)
        out.extend(_reconcile(events, dry_run=dry_run))
        specs = fold.fold(events)[0]
        rows = lane_rules.due(events, specs, now)
        # Same busy subjects as tick.in_flight, with no wall clock or stale
        # event writes (also used by lane_driver's pure dispatch predicate).
        spawns, _, escalations = lane_rules._busy(events, now)
        busy = spawns | escalations | fold.held_specs(events)
    except Exception as exc:
        if not dry_run:
            _emit('queue', 'autodispatch-failed', role='queue', reason=str(exc))
        return out + [f'queue failed: {exc}']
    eligible = []
    for row in rows:
        rule, sid = row.get('rule'), row.get('spec')
        if str(row.get('reason', '')).startswith('undetermined:'):
            out.append(f"{sid} {rule}: {row['reason']}")
            continue
        if rule not in ('gate', 'enqueue'):
            continue
        try:
            if sid in busy:
                continue
            evs = [e for e in events if e.get('subject') == sid]
            # An outcome can share its second with the preceding dispatch. Only
            # bookkeeping persisted after that outcome consumes its fresh trigger.
            anchor = next((i for i in range(len(evs) - 1, -1, -1)
                           if evs[i].get('type') == 'merge-gate-undetermined'
                           and evs[i]['ts'] == row['since']), None) if rule == 'gate' else None
            preceding = {id(e) for e in evs[:anchor + 1]
                         if e.get('type') == 'autodispatched' and e.get('rule') == 'gate'} if anchor is not None else set()
            newer = evs[anchor + 1:] if anchor is not None else [
                e for e in evs if lane_rules._time(e['ts']) >= lane_rules._time(row['since'])]
            if rule == 'gate':
                from nextstep import _gate_refusal
                refused = {id(e) for i, e in enumerate(newer)
                           if e.get('type') == 'autodispatched' and e.get('rule') == 'gate'
                           and any(_gate_refusal(f) for f in newer[i+1:])}
                preceding |= refused
                newer = [e for e in newer if id(e) not in refused and not _gate_refusal(e)]
            if anchor is not None and any(
                    e.get('type') == 'autodispatched' and e.get('rule') == 'gate'
                    and e.get('since') == row['since'] for e in newer):
                continue  # Only a new outcome can provide another gate trigger.
            rung = lane_rules.ladder([e for e in events if id(e) not in preceding], row, now)
            if rung not in ('act', 'retry'):
                continue
            if any(e.get('type') == 'autodispatched' and e.get('rule') == rule and
                   e.get('since') == row['since'] and e.get('trigger') == 'retry' for e in newer):
                continue
            if rung != 'retry' and any(
                    (e.get('type') == 'autodispatched' and e.get('rule') == rule) or
                    (e.get('type') in ('spawn-started', 'spawn-failed', 'autodispatch-failed') and
                     e.get('role') == row.get('role', rule)) for e in newer):
                continue
            eligible.append((row, rung, evs))
        except Exception as exc:
            if not dry_run:
                _emit(sid or 'queue', 'autodispatch-failed', role=rule, rule=rule, reason=str(exc))
            out.append(f'{rule} failed {sid}: {exc}')
    # Choose only after all existing skips, so skipped rows take no slot.
    # Replace gate positions only: enqueue rows retain their relative order.
    gates = sorted((item for item in eligible if item[0]['rule'] == 'gate'),
                   key=lambda item: lane_rules._time(item[0]['since']))
    permitted = {item[0]['spec'] for item in gates[:GATE_PER_TICK]}
    ordered_gates = iter(gates)
    pending = [next(ordered_gates) if item[0]['rule'] == 'gate' else item for item in eligible]
    deferred = []
    for row, rung, evs in pending:
        rule, sid = row['rule'], row['spec']
        if rule == 'gate' and sid not in permitted:
            deferred.append(sid)
            continue
        try:
            if dry_run:
                out.append(f'would {rule} {sid} ({rung})')
                continue
            _emit(sid, 'autodispatched', rule=rule, role=rule, since=row['since'],
                  spawn='none', **({'trigger': 'retry'} if rung == 'retry' else {}))
            written = _latest(evs, {'spec-written'})
            if written is None:
                raise ValueError('missing spec-written grant')
            if rule == 'gate':
                grant = written.get('footprint')
                if not isinstance(grant, list) or not grant or not all(isinstance(p, str) and p for p in grant):
                    raise ValueError('missing spec-written footprint grant')
                repo, head = _repository(written.get('project'))
                env = dict(os.environ, DOIT_ROOT=str(fold.ROOT),
                           DOIT_GATE_LEDGER_FILE=tick.tick_path().name,
                           DOIT_PROJECT=written['project'])
                result = runner(['doit', 'gate', sid.lower(), head, '--spec', sid, '--writes', *grant],
                                cwd=str(repo), env=env)
                if result.get('code') == 2:
                    out.append(f"gate tooling hold {sid}: " +
                               (result.get('stdout') or result.get('stderr') or 'undetermined').strip())
                    continue
                if result.get('code', 1):
                    lines = (result.get('stderr') or result.get('stdout') or 'gate failed').strip().splitlines()
                    raise ValueError(lines[-1])
                out.append(f'gated {sid}')
            else:
                clean = _latest(evs, {'merge-gate-clean'})
                previous = _latest(evs, {'queue-add'} | OUTCOMES)
                if not clean or (previous and evs.index(previous) >= evs.index(clean)):
                    continue
                if not clean.get('branch') or not clean.get('project'):
                    raise ValueError('clean gate missing branch or project')
                repo, head = _repository(clean['project'])
                blocked = _base_blocks(written, sid, repo, head)
                if blocked:
                    out.append(f'{sid} enqueue blocked: Base: {blocked}')
                    continue
                _emit(sid, 'queue-add', project=clean['project'], branch=clean['branch'], rule='enqueue')
                out.append(f'enqueued {sid}')
        except Exception as exc:
            if not dry_run:
                _emit(sid or 'queue', 'autodispatch-failed', role=rule, rule=rule, reason=str(exc))
            out.append(f'{rule} failed {sid}: {exc}')
    if deferred:
        out.append(f'gate deferred {len(deferred)} (per-tick cap {GATE_PER_TICK}): '
                   + ', '.join(deferred[:3]) + '...')
    return out


def _append(sid, kind, **fields):
    """CLI corrections take the same strict admission path as doit append."""
    result = subprocess.run([DOIT, 'append', kind, sid, *[f'{k}:={json.dumps(v)}' for k, v in fields.items()]],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())


def form_batch(queue_rows, footprints, cap, risky):
    """Group exact-path connected specs, preserving queue order within the group."""
    ids = list(dict.fromkeys(row['spec'] for row in queue_rows))
    if not ids:
        return []
    head, cap = ids[0], max(1, cap)
    if head in risky or footprints.get(head) is None:
        return [head]
    eligible = [sid for sid in ids if sid not in risky and footprints.get(sid) is not None]
    group, paths = {head}, set(footprints[head])
    while True:
        before = len(group)
        for sid in eligible:
            if sid not in group and paths.intersection(footprints[sid]):
                group.add(sid)
                paths.update(footprints[sid])
        if len(group) == before:
            break
    return ([sid for sid in eligible if sid in group] +
            [sid for sid in eligible if sid not in group])[:cap]


def spec_paths(repo, master, branch, runner=None):
    """No rename detection: both names remain in the footprint, including odd bytes."""
    try:
        result = (runner or subprocess.run)(
            ['git', 'diff', '--name-status', '--no-renames', '-z', f'{master}...{branch}'],
            cwd=repo, capture_output=True)
        if result.returncode:
            return None
        raw = result.stdout
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'surrogateescape')
        if not raw:
            return []
        parts = raw.split('\0')
        if parts.pop() != '' or len(parts) % 2:
            return None
        return list(zip(parts[::2], parts[1::2]))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def is_risky(paths):
    if paths is None:
        return True
    for status, path in paths:
        parts = path.split('/')
        if (path in ('deploy.sh', 'scripts/release_kit.sh') or
                path.startswith('scripts/release/') or
                status == 'A' and any(part.startswith('alembic') and
                                     parts[i + 1] == 'versions'
                                     for i, part in enumerate(parts[:-2]))):
            return True
    return False


def _look_toml():
    return Path(DOIT).parent / 'look.toml'


def effective_cap(events, now, env=None, toml_path=None):
    events = _ordered(events)
    configured, floor, unreadable = 10, 6, False
    try:
        import tomllib
        with open(_look_toml() if toml_path is None else toml_path, 'rb') as stream:
            config = tomllib.load(stream).get('merge_queue', {})
        def positive(key, default):
            value = config.get(key)
            return value if type(value) is int and value >= 1 else default
        configured, floor = positive('batch_cap', 10), positive('batch_cap_floor', 6)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        unreadable = True
    floor = min(floor, configured)
    try:
        rate = window_gate.red_rate(events, now)
        red, windows = rate['rate'], rate['windows']
    except (OSError, ValueError, TypeError, KeyError):
        red, windows, unreadable = None, 0, True
    previous = _latest(events, {'batch-cap'})
    if unreadable or red is None and windows >= 5:
        cap, reason = floor, 'undetermined'
    elif windows < 5:
        cap, reason = previous.get('cap', configured) if previous else configured, 'few-windows'
    elif red > .40:
        cap, reason = floor, 'red-rate-over-0.40'
    else:
        cap, reason = configured, 'configured'
    if type(cap) is not int or cap < 1:
        cap, reason = floor, 'undetermined'
    cap = max(1, min(cap, window_gate.max_merges(os.environ if env is None else env)))
    return dict(cap=cap, configured=configured, floor=floor, red_rate=red,
                windows=windows, reason=reason)


def batch_cap(events, now, toml_path=None):
    return effective_cap(events, now, toml_path=toml_path)


def _train_emit(ledger_file, subject, kind, **fields):
    if Path(ledger_file).name != ledger_file:
        raise ValueError('ledger filename required')
    actor = fold.actor_of(ledger_file)
    allowed = ('executor', 'tick', 'operator') if kind in ('batch-cap', 'merge-frozen') else ('executor', 'tick')
    if actor not in allowed:
        raise ValueError('unsupported train ledger actor')
    dst = fold.ROOT / 'events' / ledger_file
    dst.parent.mkdir(parents=True, exist_ok=True)
    reason = dispatch.emit(dst, {'subject': subject}, kind, **fields)
    if reason:
        raise ValueError(reason)


def record_cap(result, events, ledger_file):
    previous = _latest(_ordered(events), {'batch-cap'})
    old = previous['cap'] if previous else result['configured']
    if result['cap'] != old:
        _train_emit(ledger_file, 'merge-train', 'batch-cap',
                    **{key: result[key] for key in ('cap', 'red_rate', 'windows', 'reason')},
                    **{'from': old})


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def _run_lock(project):
    state = fold.ROOT / 'state'
    state.mkdir(parents=True, exist_ok=True)
    path = state / f'window-run.{project}.lock'
    # Serialize stale takeover and the tiny O_EXCL/write interval. The directory
    # mutex is unrelated to gate slots and is never held while running a gate.
    directory = os.open(state, os.O_RDONLY)
    owned = False
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    pid = int(path.read_text().strip())
                except (ValueError, OSError):
                    pid = None
                if pid is None or _alive(pid):
                    pass  # unreadable ownership is never a free slot
                else:
                    path.unlink()
            if not path.exists():
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(str(os.getpid()))
                owned = True
        finally:
            fcntl.flock(directory, fcntl.LOCK_UN)
        yield owned
    finally:
        if owned:
            fcntl.flock(directory, fcntl.LOCK_EX)
            try:
                path.unlink(missing_ok=True)
            finally:
                fcntl.flock(directory, fcntl.LOCK_UN)
        os.close(directory)


def _launching(events, project, flying):
    """Reserve a launched process before its first candidate reaches the ledger."""
    rows = {row['window']: row for row in flying}
    terminal = {e['subject'] for e in events if e['type'] in ('window-verified', 'escalation-blocking')}
    for event in events:
        if event['type'] != 'queue-batch' or event.get('project') != project:
            continue
        name = event['subject']
        if name in terminal or name in rows:
            continue
        path = fold.ROOT / 'state' / f'window-run.{name}.pid'
        try:
            pid = int(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise ValueError('launch state unreadable')
        if _alive(pid):
            rows[name] = dict(window=name, specs=event['specs'])
        else:
            path.unlink(missing_ok=True)
    return list(rows.values())


def _window_reconcile(events, ledger_file):
    removed = _reconcile(events, ledger_file=ledger_file)
    print(f"window-run: removed {len(removed)} spec(s); outcomes={' | '.join(removed) or 'none'}")


def merge_freeze(events, project):
    ids = fold.nightly_unowned(events, project)
    return 'unowned nightly red: ' + ', '.join(ids) if ids else None


def window_run(project, *, runner=None, launcher=None, detach=False, now=None, precheck_runner=None):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..'):
        return 2
    ledger_file = os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl')
    now = now if now is not None else datetime.now(timezone.utc)
    process = None
    with _run_lock(project) as owned:
        if not owned:
            print('window-run: lock held; no batch formed')
            return 3
        try:
            events = _ordered(window_gate.train_events())
            _window_reconcile(events, ledger_file)
            events = _ordered(window_gate.train_events())
            head = next_candidate(events)
            if head is None:
                print('window-run: no candidate; queue empty or one window in flight')
                return 3
            waiting = queue(events)
            if waiting[0]['project'] != project:
                raise ValueError('queue head project undetermined or differs from launch')
            try:
                reason = merge_freeze(events, project)
            except Exception as exc:
                raise ValueError('nightly freeze undetermined') from exc
            if reason is not None:
                print('window-run: ' + reason)
                previous = _latest([e for e in events if e.get('project') == project], {'merge-frozen'})
                if previous is None or previous.get('reason') != reason:
                    _train_emit(ledger_file, 'nightly:' + project, 'merge-frozen',
                                project=project, reason=reason)
                return 3
            # Selection uses the configured capacity, independent of red-rate tuning.
            import tomllib
            try:
                with open(_look_toml(), 'rb') as stream:
                    configured = tomllib.load(stream).get('merge_queue', {}).get('batch_cap')
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise ValueError('configuration batch_cap undetermined') from exc
            if type(configured) is not int or configured < 1:
                raise ValueError('configuration batch_cap must be a positive integer')
            if configured > window_gate.max_merges(os.environ):
                raise ValueError('configuration batch_cap exceeds gate max_merges')

            def refuse(reason, specs):
                attempt = 'window-' + uuid.uuid4().hex
                recovery = ('Operator: repair the repository/branch then retry; for an invalid branch use '
                            'doit queue remove SPEC --why WHY (' + ', '.join(specs) + ')')
                try:
                    _train_emit(ledger_file, attempt, 'window-refused', window=attempt,
                                project=project, holder_window='<unknown>', holder_pid=-1,
                                reason=reason, specs=specs, recovery=recovery)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError(f'{reason}: refusal persistence failed: {exc}') from exc
                print(f"window-run: {reason}: {', '.join(specs)}; {recovery}", file=sys.stderr)
                return 3

            entries = queue(events, project)
            tiers = {'L-charter-0072': 0, 'L-charter-0074': 1}
            entries.sort(key=lambda row: tiers.get(
                dispatch.resolve_charter(events, row['spec'], None), 2))
            try:
                repo, master = _repository(project)
            except (OSError, ValueError):
                return refuse('repository-undetermined', [r['spec'] for r in entries])
            import precheck
            checked = precheck.selection(events, project, repo, master, now, precheck_runner)
            # A synchronous check may have appended terminal outcomes or red evidence.
            events = _ordered(window_gate.train_events())
            _window_reconcile(events, ledger_file)
            events = _ordered(window_gate.train_events())
            entries = queue(events, project)
            entries.sort(key=lambda row: tiers.get(
                dispatch.resolve_charter(events, row['spec'], None), 2))

            # Queue membership is retained for rework. Only a later queue-add
            # clears a red result, including ledger ties at equal timestamps.
            red = set()
            for event in events:
                sid = event.get('subject')
                if event['type'] == 'queue-add':
                    red.discard(sid)
                elif event['type'] == 'precheck-red':
                    red.add(sid)
            readiness = {}
            for row in entries:
                sid = row['spec']
                if sid in red:
                    readiness[sid] = 'precheck-red'
                if checked is not None and lane_rules._time(checked['ts']) >= lane_rules._time(row['added_at']):
                    if sid in checked.get('guard_red', {}):
                        readiness[sid] = 'precheck-red'
                    elif sid in checked.get('conflict_master', {}) and sid not in readiness:
                        readiness[sid] = 'conflict-master'
            footprints = {}
            for row in entries:
                sid = row['spec']
                if sid in readiness:
                    continue
                paths = spec_paths(repo, master, row['branch'])
                footprints[sid] = None if paths is None else {path for _, path in paths}
            unknown = [sid for sid, paths in footprints.items() if paths is None]
            if unknown:
                return refuse('footprint-undetermined', unknown)
            specs, skipped = [], []
            for row in entries:
                sid = row['spec']
                blockers, shared = [], set()
                reason = readiness.get(sid)
                if reason is None:
                    for selected in specs:
                        overlap = footprints[sid] & footprints[selected]
                        if overlap:
                            blockers.append(selected)
                            shared.update(overlap)
                    if blockers:
                        reason = 'footprint-conflict'
                    elif len(specs) >= configured:
                        reason = 'cap-reached'
                if reason is not None:
                    skipped.append(dict(spec=sid, reason=reason, conflicts_with=blockers, paths=sorted(shared)))
                else:
                    specs.append(sid)
            print(f'window-run: selected {len(specs)}; skipped {len(skipped)}; '
                  f'exclusions={json.dumps(skipped)}')
            if not specs:
                print('window-run: no merge-ready candidates')
                return 3
            reason = 'selection-exclusions' if skipped else 'sole-eligible' if len(specs) == 1 else 'all-ready'
            window = 'window-' + uuid.uuid4().hex
            _train_emit(ledger_file, window, 'queue-batch', project=project, specs=','.join(specs),
                        reason=reason, cap=configured, queue_len=len(entries), taken=specs, skipped=skipped,
                        precheck=checked['ts'] if checked is not None else 'none')
            argv = ['doit', 'gate-window', window, '--project', project, '--specs', ','.join(specs)]
            if runner is not None:
                if detach:
                    raise ValueError('detached calls require a process launcher')
                result = runner(argv)
            else:
                log = fold.ROOT / 'state' / f'window-run.{window}.log'
                with log.open('ab') as stream:
                    process = (launcher or subprocess.Popen)(
                        [DOIT, *argv[1:]], start_new_session=True,
                        stdout=stream, stderr=subprocess.STDOUT)
                (fold.ROOT / 'state' / f'window-run.{window}.pid').write_text(str(process.pid))
                print(f'window-run: launched {window}; log={log}')
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration) as exc:
            print(f'window-run: undetermined; {exc}', file=sys.stderr)
            return 3
    if detach:
        return 0
    code = process.wait() if process is not None else result.get('code', 1)
    # Reconcile once more for synchronous callers; detached outcomes land next pass.
    with _run_lock(project) as owned:
        if owned:
            try:
                _window_reconcile(_ordered(window_gate.train_events()), ledger_file)
            except (OSError, ValueError) as exc:
                print(f'window-run: outcome reconciliation deferred; {exc}', file=sys.stderr)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('queue', 'window-run'))
    args, rest = parser.parse_known_args(argv)
    try:
        if args.command == 'window-run':
            p = argparse.ArgumentParser()
            p.add_argument('--project', required=True)
            p.add_argument('--detach', action='store_true')
            a = p.parse_args(rest)
            return window_run(a.project, detach=a.detach)
        p = argparse.ArgumentParser()
        p.add_argument('action', choices=('list', 'add', 'remove'))
        p.add_argument('spec', nargs='?')
        p.add_argument('--project')
        p.add_argument('--json', action='store_true')
        p.add_argument('--why')
        a = p.parse_args(rest)
        events = _ordered(fold.read_events())
        entries = queue(events, a.project if a.action == 'list' else None)
        if a.action == 'list':
            omitted = [row['spec'] for row in queue(events, a.project, _include_terminal=True)
                       if row['spec'] not in {entry['spec'] for entry in entries}]
            if omitted:
                reasons = {sid: _latest([e for e in events if e.get('subject') == sid], TERMINALS)['type']
                           for sid in omitted}
                print(f"queue: omitted {len(omitted)} spec(s); reason=terminal outcome; specs={reasons}",
                      file=sys.stderr)
            corrections = {}
            for event in events:
                if event.get('type') in ('queue-add', 'queue-remove'):
                    corrections[event['subject']] = event
            missing = [sid for sid, event in corrections.items()
                       if event['type'] == 'queue-add' and not event.get('branch') and
                       (a.project is None or event.get('project') == a.project)]
            if missing:
                print(f"queue: omitted {len(missing)} spec(s); reason=queue-add missing branch; "
                      f"specs={','.join(missing)}", file=sys.stderr)
            if a.json:
                print(json.dumps(entries))
            else:
                for n, row in enumerate(entries, 1):
                    print(n, *[row[k] for k in ('spec', 'project', 'branch', 'added_at', 'batch')])
            return 0
        if not a.spec or not a.why or not a.why.strip():
            raise ValueError('spec and nonempty --why required')
        live = next((r for r in entries if r['spec'] == a.spec), None)
        if a.action == 'add':
            clean = _latest([e for e in events if e.get('subject') == a.spec], {'merge-gate-clean'})
            if live or not clean or not clean.get('branch') or not clean.get('project'):
                raise ValueError('already queued or no complete merge-gate-clean')
            _append(a.spec, 'queue-add', project=clean['project'], branch=clean['branch'], rule='operator', why=a.why)
        else:
            if not live:
                raise ValueError('spec not queued')
            _append(a.spec, 'queue-remove', project=live['project'], reason='operator', why=a.why)
        return 0
    except (OSError, ValueError) as exc:
        print(f'{args.command}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
