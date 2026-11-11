"""Ledger-owned merge queue and the tick's gate/enqueue actions."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

import dispatch
import fold
import lane_rules
from pass_memo import once_per_list
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


_ordered_once = once_per_list(_ordered)   # read-only use: queue() runs per spec within a pass


def _latest(events, kinds):
    return next((e for e in reversed(events) if e.get('type') in kinds), None)


def queue(events, project=None, *, _include_terminal=False):
    """Fold corrections without consulting a mutable script or the wall clock."""
    live, returned = {}, set()
    for pos, e in enumerate(_ordered_once(events)):
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
                if spec in live and live[spec][1]['project'] == e.get('project'):
                    live[spec][1]['batch'] = sid
        elif kind == 'window-redcut' and e.get('window') == sid:
            # Return an existing row, retaining its branch, timestamp and
            # position. An old completion cannot release a later reservation.
            for spec in e.get('returned', []):
                key = (sid, spec)
                if key in returned:
                    continue
                returned.add(key)
                if spec in live:
                    row = live[spec][1]
                    if row['project'] == e.get('project') and row['batch'] == sid:
                        row['batch'] = None
    return [dict(row) for sid, (_, row) in
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
    events = _ordered(events)
    terminal = {e['subject'] for e in events if e['type'] in
                ('window-verified', 'escalation-blocking', 'window-redcut')
                or e['type'] == 'window-cancelled' and e.get('reason') == 'escalation-default'}
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


def _cleanup_pending(project, window):
    # The lifetime lock protects cleanup even after terminal evidence commits.
    with window_gate.chain_lock(window, project, root=fold.ROOT) as holder:
        if holder is not None:
            return True
    for path in (fold.ROOT / 'state').glob('window-gate.*.lock'):
        slot = path.name.split('.')[1]
        if slot.isdecimal() and window_gate.lock_holder(int(slot), fold.ROOT) is not None:
            return True
    return False


def _successor_admitted(events, window):
    if any(e.get('subject') == window and e.get('type') in
           ('window-opened', 'window-candidate', 'window-verified', 'window-redcut',
            'escalation-blocking') for e in events):
        return True
    path = fold.ROOT / 'state' / f'window-run.{window}.pid'
    try:
        return int(path.read_text()) > 0
    except FileNotFoundError:
        return False


def _redcut_link(events, project):
    """Choose only committed dispositions; retry a refused launch's same ID."""
    closures = [e for e in events if e.get('type') == 'window-redcut'
                and e.get('project') == project and e.get('outcome') == 'handled-red'
                and e.get('window') == e.get('subject') and not e.get('residual')]
    for closure in reversed(closures):
        predecessor = closure['subject']
        joins = [e for e in events if e.get('type') == 'queue-batch'
                 and e.get('project') == project and e.get('redcut_of') == predecessor]
        if any(_successor_admitted(events, e['subject']) for e in joins):
            continue
        return predecessor, joins[-1]['subject'] if joins else None
    return None, None


def redcut_handoff(project, window, *, launcher=None, now=None, precheck_runner=None):
    """One bounded attempt through the ordinary serialized queue launcher."""
    try:
        events = _ordered(window_gate.train_events())
        closed = any(e.get('type') == 'window-redcut' and e.get('subject') == window
                     and e.get('window') == window and e.get('project') == project
                     and e.get('outcome') == 'handled-red' and not e.get('residual')
                     for e in events)
        if not closed or _cleanup_pending(project, window):
            print('redcut-handoff: pending; disposition unresolved or cleanup lock held')
            return 3
        joins = [e for e in events if e.get('type') == 'queue-batch'
                 and e.get('project') == project and e.get('redcut_of') == window]
        if any(_successor_admitted(events, e['subject']) for e in joins):
            print('redcut-handoff: successor already admitted')
            return 0
        if _redcut_link(events, project)[0] != window:
            print('redcut-handoff: pending; another disposition owns next admission')
            return 3
        code = window_run(project, detach=True, launcher=launcher, now=now,
                          precheck_runner=precheck_runner)
        events = _ordered(window_gate.train_events())
        admitted = any(e.get('type') == 'queue-batch' and e.get('project') == project
                       and e.get('redcut_of') == window
                       and _successor_admitted(events, e['subject']) for e in events)
        return 0 if admitted else code if code else 3
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'redcut-handoff: pending; {exc}')
        return 3


def redcut_metrics(events, *, project, since, until):
    """Read admitted evidence as of until; never infer an open from launch time."""
    def stamp(value):
        value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('aware timestamp required')
        return value

    since, until = stamp(since), stamp(until)
    if since >= until:
        raise ValueError('empty metric interval')
    rows, bad_time = [], []
    for event in events:
        if 'actor' in event and fold.admits(event, event['actor']) is not None:
            continue
        try:
            if stamp(event.get('ts')) < until:
                rows.append(event)
        except (ValueError, TypeError):
            bad_time.append(event)
    windows, invalid = {}, set()
    for event in rows:
        if event.get('type') in ('queue-batch', 'window-opened', 'window-redcut'):
            windows.setdefault(event.get('subject'), []).append(event)
    members, projects = {}, {}
    for name, evidence in windows.items():
        projects[name] = {e.get('project') for e in evidence}
        members[name] = {sid for e in evidence if e['type'] in ('queue-batch', 'window-opened')
                         for sid in str(e.get('specs', '')).split(',') if sid}
    closures = {}
    for name, evidence in windows.items():
        if project not in projects[name]:
            continue
        redcuts = [e for e in evidence if e['type'] == 'window-redcut'
                   and e.get('outcome') != 'rehearsal-reformed']
        if not redcuts:
            continue
        try:
            if projects[name] != {project}:
                raise ValueError('contradictory project')
            signatures = {json.dumps({k: v for k, v in e.items()
                                      if k not in ('ts', 'actor', '_src', 'v')}, sort_keys=True)
                          for e in redcuts}
            if len(signatures) != 1:
                raise ValueError('contradictory closure')
            e = redcuts[0]
            first, closed = stamp(e.get('first_red_at')), stamp(e.get('closed_at'))
            if (e.get('window') != name or e.get('outcome') != 'handled-red'
                    or e.get('automatic') is not True or closed < first
                    or closed > stamp(e['ts'])
                    or any(not isinstance(e.get(k), list) for k in
                           ('dropped', 'returned', 'flakes', 'residual'))):
                raise ValueError('invalid closure')
            if since <= closed < until:
                closures[name] = (closed, first)
        except (ValueError, TypeError):
            invalid.add(name)
    for e in bad_time:
        name = e.get('window') if e.get('type') == 'window-dropped' else e.get('subject')
        if e.get('type') in ('window-redcut', 'window-opened', 'queue-batch', 'window-dropped'):
            if e.get('project') == project or project in projects.get(name, set()):
                invalid.add(name)
    legacy = {}
    for e in rows:
        name = e.get('window')
        if (e.get('type') != 'window-dropped' or not e.get('nodes') or
                name not in windows or e.get('subject') not in members[name] or
                any(r['type'] == 'window-redcut' for r in windows[name])):
            continue
        if project not in projects[name]:
            continue
        if projects[name] != {project} or e.get('project', project) != project:
            invalid.add(name)
            continue
        closed = stamp(e['ts'])
        # Membership must precede the drop, not merely exist later in the file.
        if not any(r['type'] in ('queue-batch', 'window-opened')
                   and stamp(r['ts']) <= closed and e['subject'] in str(r.get('specs', '')).split(',')
                   for r in windows[name]):
            invalid.add(name)
            continue
        legacy[name] = max(closed, legacy.get(name, closed))
    durations, pending, automatic, without = [], [], 0, 0
    for name, (closed, first) in sorted(closures.items(), key=lambda item: (item[1][0], item[0])):
        try:
            joins = [e for e in rows if e.get('type') == 'queue-batch'
                     and e.get('redcut_of') == name and e.get('project') == project]
            successors = {e['subject'] for e in joins}
            if len(successors) > 1:
                raise ValueError('conflicting successors')
            successor = next(iter(successors), None)
            opens = [e for e in rows if e.get('type') == 'window-opened'
                     and e.get('subject') == successor and successor is not None]
            if any(stamp(e['ts']) < closed for e in joins):
                raise ValueError('batch before closure')
            if any(e.get('project') != project or e.get('redcut_of', name) != name
                   or stamp(e['ts']) < closed for e in opens):
                raise ValueError('contradictory open')
            open_times = {stamp(e['ts']) for e in opens}
            if len(open_times) > 1:
                raise ValueError('conflicting opens')
            if name in invalid:
                continue
            automatic += 1
            involved = members.get(name, set()) | {name}
            decisions = [e for e in rows if e.get('type') == 'decision'
                         and e.get('actor') in ('thinker', 'operator')
                         and e.get('subject') in involved and first <= stamp(e['ts']) <= closed
                         and e.get('project', project) == project
                         and (e.get('subject') == name or
                              len({p for w, ids in members.items() if e['subject'] in ids
                                   for p in projects[w]}) == 1)]
            without += not bool(decisions)
            if open_times:
                durations.append(dict(window=name, next_window=successor,
                                      minutes=(next(iter(open_times)) - first).total_seconds() / 60))
            else:
                pending.append(name)
        except (ValueError, TypeError):
            invalid.add(name)
    return dict(automatic_closed=automatic,
                red_closed=automatic + sum(since <= closed < until and name not in invalid
                                           for name, closed in legacy.items()),
                without_thinker_decision=without, red_to_next_open_minutes=durations,
                pending_next_open=pending, excluded_invalid=sorted(invalid))


def _window_reconcile(events, ledger_file):
    removed = _reconcile(events, ledger_file=ledger_file)
    print(f"window-run: removed {len(removed)} spec(s); outcomes={' | '.join(removed) or 'none'}")


def merge_freeze(events, project):
    import quarantine
    current = quarantine.active(events, project, datetime.now(timezone.utc))
    ids = [node for node in fold.nightly_unowned(events, project) if node not in current]
    return 'unowned nightly red: ' + ', '.join(ids) if ids else None


def _rehearsal_batches(events, project):
    """Project-scoped reservations, including the interval before a worker starts."""
    batches = {}
    for event in _ordered(events):
        if event.get('project') != project:
            continue
        ident, kind = event.get('subject'), event.get('type')
        if kind == 'queue-batch' and event.get('rehearsal') is True:
            batches[ident] = dict(event, receipt=None)
        elif ident in batches:
            if kind == 'window-rehearsed':
                batches[ident]['receipt'] = event
            elif kind in ('window-verified', 'window-redcut', 'escalation-blocking') or (
                    kind == 'window-cancelled' and event.get('reason') == 'escalation-default'):
                del batches[ident]
    return list(batches.values())


def _canonical_rows(repo, main, base, rows, events):
    """Rows in migration_order.order's order for migration specs; (rows, {spec: hold reason})."""
    import window_migrations
    try:
        entries = [window_gate.resolve_entries(repo, [row['spec'] + ':' + row['branch']], events)[0]
                   for row in rows]
    except RuntimeError:
        return rows, {}
    ordered, held = window_migrations.canonical(repo, base, entries, events, fold.ROOT / 'content')
    by_spec = {row['spec']: row for row in rows}
    return ([by_spec[e[0]] for e in ordered] + [by_spec[s] for s in held if s in by_spec],
            {s: 'migration-order: ' + why for s, why in held.items()})


def _refill_rehearsals(project, *, runner=None, launcher=None, cap=6, local=None, cancel=None):
    """Prepare and dispatch stacked CI work without acquiring ordinary gate slots.

    Every reservation is durable before dispatch. The caller may poll results on
    a later tick; a missing/undetermined candidate is never treated as free room.
    """
    import precheck
    import window_migrations
    import window_prewindow
    import window_rehearse

    if (not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..')
            or type(cap) is not int or not 1 <= cap <= 6):
        raise ValueError('invalid rehearsal admission configuration')
    ledger_file = os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl')
    started, locals_ = [], []
    local = local if local is not None else (_local_prefilter if local_prefilter_enabled() else None)
    cancel = cancel or _cancel_rehearsal
    with _run_lock(project) as owned:
        if not owned:
            return started
        events = _ordered(window_gate.train_events())
        _window_reconcile(events, ledger_file)
        events = _ordered(window_gate.train_events())
        if _cleanup_pending(project, 'admission'):
            print('window-run: rehearsal refill pending cleanup')
            return started
        reason = merge_freeze(events, project)
        if reason:
            print('window-run: rehearsal refill held; ' + reason)
            return started
        repo, main = _repository(project)
        active = _rehearsal_batches(events, project)
        capacity = window_rehearse.capacity()
        base = window_gate.git(repo, 'rev-parse', '--verify', 'refs/heads/' + main).stdout.strip()
        for batch in active:
            receipt = batch['receipt'] or {}
            if receipt.get('state') not in ('prepared', 'running', 'green'):
                print('window-run: rehearsal refill held; predecessor verdict undetermined or red')
                return started
            candidate = receipt.get('sha', '')
            if not re.fullmatch(r'[0-9a-f]{40}', candidate):
                raise ValueError('in-flight rehearsal candidate unavailable')
            window_gate.tree_of(repo, candidate)
            base = candidate
        red = set()
        for event in events:
            if event.get('type') == 'queue-add':
                red.discard(event.get('subject'))
            elif event.get('type') == 'precheck-red':
                red.add(event.get('subject'))
        remaining = [row for row in queue(events, project) if row['batch'] is None]
        tiers = {'L-charter-0072': 0, 'L-charter-0074': 1}
        remaining.sort(key=lambda row: tiers.get(
            dispatch.resolve_charter(events, row['spec'], None), 2))
        while remaining and len(active) < capacity:
            entries, selected, skipped, paths, migration = [], [], [], set(), False
            tip = base
            remaining, held = _canonical_rows(repo, main, base, remaining, events)
            record = {}
            for row in remaining:
                sid = row['spec']
                reason = ('precheck-red' if sid in red else 'cap-reached' if len(entries) >= cap
                          else held.get(sid))
                changes = spec_paths(repo, main, row['branch'])
                if changes is None:
                    raise ValueError('rehearsal footprint undetermined: ' + sid)
                footprint = {path for _, path in changes}
                migrations = any(path.startswith(window_gate.rechain.VERSIONS) and path.endswith('.py')
                                 and '/_archive/' not in path for path in footprint)
                # Peer migrations share bookkeeping files; only the resolver may
                # admit that overlap (prewindow and the stack below prove it).
                if reason is None and paths & footprint and not (migration and migrations):
                    reason = 'footprint-conflict'
                if reason is None:
                    entry = window_gate.resolve_entries(repo, [sid + ':' + row['branch']], events)[0]
                    admitted, rejected = window_prewindow.select(
                        repo, base, main, entries + [entry], events, fold.ROOT / 'content', on=base)
                    # select reorders migrations canonically; the set must match, not the order.
                    if rejected or {e[0] for e in admitted} != {e[0] for e in entries + [entry]}:
                        reason = ('prewindow: ' + '; '.join(why for _, why in rejected)
                                  if rejected else 'prewindow-deferred')
                    else:
                        new_tip, conflict = precheck.merge(repo, tip, entry[2], entry=entry)
                        if conflict:
                            reason = 'merge-conflict: ' + conflict
                        else:
                            stack, _ = window_migrations.stack_check(repo, base, admitted)
                            reason = ('migration-stack: ' + stack) if stack else None
                if reason:
                    skipped.append(dict(spec=sid, reason=reason))
                    continue
                tip = new_tip
                entries[:] = admitted
                selected[:] = [e[0] for e in entries]
                paths.update(footprint)
                migration = migration or migrations
            print(f'window-run: rehearsal selected {len(selected)}; skipped {len(skipped)}; '
                  f'exclusions={json.dumps(skipped)}')
            if not entries:
                break
            ident = 'window-' + uuid.uuid4().hex
            candidate = window_rehearse.prepare(repo, ident, base, entries, project, main, events, record)
            tree = window_gate.tree_of(repo, candidate)
            _train_emit(ledger_file, ident, 'queue-batch', project=project, specs=','.join(selected),
                        rehearsal=True, base_sha=base, sha=candidate, cap=cap, queue_len=len(remaining),
                        taken=selected, skipped=skipped, reason='rehearsal-refill')
            _train_emit(ledger_file, ident, 'window-rehearsed', project=project, specs=selected,
                        rehearsal=True, base_sha=base, sha=candidate, tree=tree, state='prepared',
                        launcher_prepared=True, entries=[list(entry) for entry in entries],
                        **window_migrations.receipt_fields(record))
            argv = ['doit', 'gate-window', ident, '--project', project, '--main', main,
                    '--rehearse', '--on', base,
                    '--specs', ','.join(spec + ':' + branch for spec, branch, _ in entries)]
            run = dict(batch=ident, project=project, pid=None)
            # CI is dispatched first; the optional local pass only overlaps it.
            if runner is not None:
                runner(argv)
            else:
                log = fold.ROOT / 'state' / f'rehearsal.{ident}.log'
                with log.open('ab') as stream:
                    process = (launcher or subprocess.Popen)([DOIT, *argv[1:]], start_new_session=True,
                                                            stdout=stream, stderr=subprocess.STDOUT)
                (fold.ROOT / 'state' / f'rehearsal.{ident}.pid').write_text(str(process.pid))
                run['pid'] = process.pid
            started.append(ident)
            if local is not None:
                locals_.append(_start_local(local, repo, run, candidate, selected))
            events = _ordered(window_gate.train_events())
            active = _rehearsal_batches(events, project)
            current = next((batch for batch in active if batch['subject'] == ident), None)
            if current is None or (current['receipt'] or {}).get('state') not in ('prepared', 'running', 'green'):
                print('window-run: rehearsal refill stopped; current batch has no usable candidate verdict')
                break
            remaining = [row for row in remaining if row['spec'] not in selected]
            base = candidate
        _consume_local(project, ledger_file, locals_, started, cancel)
    return started


def local_prefilter_enabled(env=None):
    """The optional local pass is off unless explicitly switched on."""
    value = str((os.environ if env is None else env).get('DOIT_LOCAL_PREFILTER', 'off')).lower()
    if value not in ('on', 'off', '1', '0', 'true', 'false'):
        raise ValueError('DOIT_LOCAL_PREFILTER must be on or off')
    return value in ('on', '1', 'true')


def _local_prefilter(repo, run, candidate, members):
    """Run the configured local command on the exact candidate; anything else is no verdict."""
    import shlex
    import tempfile
    command = shlex.split(os.environ.get('DOIT_LOCAL_PREFILTER_CMD', ''))
    if not command:
        return 'undetermined'
    with tempfile.TemporaryDirectory(prefix='local-prefilter-') as directory:
        tree = Path(directory) / 'tree'
        window_gate.git(repo, 'worktree', 'add', '--detach', str(tree), candidate)
        try:
            done = subprocess.run(command, cwd=tree, capture_output=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            return 'undetermined'
        finally:
            window_gate.git(repo, 'worktree', 'remove', '--force', str(tree))
    return 'green' if done.returncode == 0 else 'red'


def _start_local(local, repo, run, candidate, members):
    import threading
    box = dict(run=run, candidate=candidate, members=list(members), verdict='undetermined')

    def work():
        try:
            box['verdict'] = local(repo, run, candidate, list(members))
        except Exception:
            box['verdict'] = 'undetermined'
    thread = threading.Thread(target=work, daemon=True)
    box['thread'] = thread
    thread.start()
    return box


def _cancel_rehearsal(run):
    """Stop only the worker this launcher started for the batch; its trap cancels CI."""
    pid = run.get('pid')
    if type(pid) is int and pid > 1 and _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _release_batch(ledger_file, project, ident, members, parked, reason, *, candidate='', base=''):
    """Park attributed members and return the rest of the batch to the ready queue."""
    stamp = datetime.now(timezone.utc).isoformat()
    for spec in sorted(parked):
        _train_emit(ledger_file, spec, 'queue-remove', project=project, window=ident, reason=reason)
    _train_emit(ledger_file, ident, 'window-redcut', window=ident, project=project, candidate=candidate,
                base=base or '0' * 40, dropped=sorted(parked), returned=[m for m in members if m not in parked],
                flakes=[], residual=[], first_red_at=stamp, closed_at=stamp,
                outcome='rehearsal-reformed', automatic=True)


def _consume_local(project, ledger_file, locals_, started, cancel):
    """A local red cancels its own CI run, parks its batch and voids stacked successors."""
    for box in locals_:
        box['thread'].join(timeout=3600)
    events = _ordered(window_gate.train_events())
    for index, box in enumerate(locals_):
        if box['verdict'] != 'red':
            continue
        active = {batch['subject']: batch for batch in _rehearsal_batches(events, project)}
        names = [box['run']['batch']] + [b['run']['batch'] for b in locals_[index + 1:]]
        for name in names:
            batch = active.get(name)
            if batch is None:
                continue
            run = next(b['run'] for b in locals_ if b['run']['batch'] == name)
            cancel(run)
            members = batch['specs'].split(',')
            first = name == box['run']['batch']
            _release_batch(ledger_file, project, name, members, members if first else [],
                           'local pre-filter red' if first else 'stacked on locally red batch',
                           candidate=batch.get('sha', ''), base=batch.get('base_sha', ''))
        break


def rehearsal_launcher_enabled(env=None):
    value = str((os.environ if env is None else env).get('DOIT_REHEARSAL_LAUNCHER', 'off')).lower()
    if value not in ('on', 'off'):
        raise ValueError('DOIT_REHEARSAL_LAUNCHER must be on or off')
    return value == 'on'


def refill_candidate(events, project=None):
    """First unbatched ready spec while fewer than N rehearsal batches are in flight."""
    if not rehearsal_launcher_enabled():
        return None
    import window_rehearse
    events = _ordered(events)
    rows = queue(events, project)
    if not rows:
        return None
    active = _rehearsal_batches(events, rows[0]['project'])
    if any((batch['receipt'] or {}).get('state') in ('green', 'red') for batch in active):
        return active[0]['specs'].split(',')[0]
    free = [row for row in rows if row['batch'] is None]
    return free[0]['spec'] if free and len(active) < window_rehearse.capacity() else None


def _settle_rehearsals(project, ledger_file, *, runner=None, launcher=None, cancel=None):
    """Park attributed reds, void their stacked successors and launch the oldest green batch."""
    cancel = cancel or _cancel_rehearsal
    launched = []
    events = _ordered(window_gate.train_events())
    active = _rehearsal_batches(events, project)
    for index, batch in enumerate(active):
        receipt = batch['receipt'] or {}
        if receipt.get('state') != 'red':
            continue
        members = batch['specs'].split(',')
        attribution = receipt.get('attribution') or {}
        parked = set()
        if isinstance(attribution, dict):
            for field in ('proven', 'drop'):
                blamed = attribution.get(field) or {}
                if isinstance(blamed, dict):
                    parked.update(spec for spec in blamed if spec in members)
        if not parked:
            print('window-run: rehearsal red without attribution; ' + batch['subject'] + ' held')
            break
        reason = 'attributed rehearsal red'
        for later in active[index:]:
            run = dict(batch=later['subject'], project=project, pid=_pid_of(later['subject']))
            names = later['specs'].split(',')
            if later is batch:
                _release_batch(ledger_file, project, later['subject'], names, parked, reason,
                               candidate=later.get('sha', ''), base=later.get('base_sha', ''))
            else:
                cancel(run)
                _release_batch(ledger_file, project, later['subject'], names, [],
                               'stacked on red rehearsal ' + batch['subject'],
                               candidate=later.get('sha', ''), base=later.get('base_sha', ''))
        events = _ordered(window_gate.train_events())
        active = _rehearsal_batches(events, project)
        break
    flying = window_gate.in_flight(events, root=fold.ROOT)
    if active and not _launching(events, project, flying) and not flying:
        head = active[0]
        receipt = head['receipt'] or {}
        started = any(e.get('subject') == head['subject'] and
                      e.get('type') in ('window-opened', 'window-candidate') for e in events)
        if receipt.get('state') == 'green' and not started:
            specs = ','.join(entry[0] + ':' + entry[1] for entry in receipt.get('entries', []))
            if not specs:
                print('window-run: green rehearsal has no member entries; not launched')
                return launched
            argv = ['doit', 'gate-window', head['subject'], '--project', project, '--specs', specs]
            if runner is not None:
                runner(argv)
            else:
                log = fold.ROOT / 'state' / f"window-run.{head['subject']}.log"
                with log.open('ab') as stream:
                    process = (launcher or subprocess.Popen)([DOIT, *argv[1:]], start_new_session=True,
                                                            stdout=stream, stderr=subprocess.STDOUT)
                (fold.ROOT / 'state' / f"window-run.{head['subject']}.pid").write_text(str(process.pid))
            print(f"window-run: launched green rehearsal {head['subject']}")
            launched.append(head['subject'])
    return launched


def _pid_of(ident):
    try:
        return int((fold.ROOT / 'state' / f'rehearsal.{ident}.pid').read_text())
    except (OSError, ValueError):
        return None


def _rehearsal_run(project, *, runner=None, launcher=None, now=None, cancel=None, local=None):
    ledger_file = os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl')
    now = now if now is not None else datetime.now(timezone.utc)
    cap = window_gate.max_merges(os.environ)
    try:
        with _run_lock(project) as owned:
            if not owned:
                print('window-run: lock held; no batch formed')
                return 3
            import quarantine
            quarantine.apply(project, now=now)
            launched = _settle_rehearsals(project, ledger_file, runner=runner, launcher=launcher,
                                          cancel=cancel)
        started = _refill_rehearsals(project, runner=runner, launcher=launcher, cap=min(cap, 6),
                                     local=local, cancel=cancel)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f'window-run: undetermined; {exc}', file=sys.stderr)
        return 3
    return 0 if launched or started else 3


def window_run(project, *, runner=None, launcher=None, detach=False, now=None, precheck_runner=None,
               cancel=None, local=None):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..'):
        return 2
    if rehearsal_launcher_enabled():
        return _rehearsal_run(project, runner=runner, launcher=launcher, now=now, cancel=cancel, local=local)
    ledger_file = os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl')
    now = now if now is not None else datetime.now(timezone.utc)
    process = None
    with _run_lock(project) as owned:
        if not owned:
            print('window-run: lock held; no batch formed')
            return 3
        try:
            import quarantine
            quarantine.apply(project, now=now)
            events = _ordered(window_gate.train_events())
            _window_reconcile(events, ledger_file)
            events = _ordered(window_gate.train_events())
            redcut_of, retry_window = _redcut_link(events, project)
            if _cleanup_pending(project, redcut_of or 'admission'):
                print('window-run: pending; cleanup/chain lock held')
                return 3
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
            maintenance_only = set()
            if reason is not None:
                import quarantine_file
                maintenance_only = quarantine_file.queued(events, project)
            if reason is not None and not maintenance_only:
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
            configured = min(configured, window_gate.max_merges(os.environ))

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

            entries = [row for row in queue(events, project)
                       if not maintenance_only or row['spec'] in maintenance_only]
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
            entries = [row for row in queue(events, project)
                       if not maintenance_only or row['spec'] in maintenance_only]
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
                if row['batch'] != retry_window and any(
                        e.get('type') == 'window-redcut' and e.get('subject') == row['batch']
                        for e in events):
                    readiness[sid] = 'reserved-window'
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
            window = retry_window or 'window-' + uuid.uuid4().hex
            _train_emit(ledger_file, window, 'queue-batch', project=project, specs=','.join(specs),
                        reason=reason, cap=configured, queue_len=len(entries), taken=specs, skipped=skipped,
                        precheck=checked['ts'] if checked is not None else 'none',
                        **({'redcut_of': redcut_of} if redcut_of else {}))
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
    if code == 5:
        redcut_handoff(project, window, launcher=launcher, now=now,
                       precheck_runner=precheck_runner)
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
