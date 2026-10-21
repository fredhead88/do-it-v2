"""Ledger-owned merge queue and the tick's gate/enqueue actions."""
import argparse
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

DOIT = str(Path(__file__).resolve().parent.parent / 'doit')
OUTCOMES = {'shipped', 'window-dropped', 'merge-conflict'}
TERMINALS = OUTCOMES | {'spec-killed', 'spec-closed'}


def _ordered(events):
    # Stable sorting preserves ledger position at equal timestamps.
    return sorted((e for e in events if 'actor' not in e or
                   fold.admits(e, e['actor']) is None), key=lambda e: lane_rules._time(e['ts']))


def _latest(events, kinds):
    return next((e for e in reversed(events) if e.get('type') in kinds), None)


def queue(events, project=None):
    """Fold corrections without consulting a mutable script or the wall clock."""
    live, batches = {}, {}
    for pos, e in enumerate(_ordered(events)):
        sid, kind = e.get('subject'), e.get('type')
        if kind == 'queue-add':
            live.pop(sid, None)
            if e.get('branch'):
                live[sid] = (pos, dict(spec=sid, project=e.get('project'), branch=e['branch'],
                                      added_at=e['ts'], batch=None))
        elif kind == 'queue-remove':
            live.pop(sid, None)
        elif kind == 'queue-batch':
            for spec in e.get('specs', '').split(','):
                batches[spec] = sid
    return [dict(row, batch=batches.get(sid)) for sid, (_, row) in
            sorted(live.items(), key=lambda item: item[1][0])
            if project is None or row['project'] == project]


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


def _reconcile(events, *, dry_run=False):
    out = []
    for row in queue(events):
        evs = [e for e in events if e.get('subject') == row['spec']]
        added = _latest(evs, {'queue-add'})
        outcome = _latest(evs, TERMINALS)
        if not outcome or evs.index(outcome) <= evs.index(added):
            continue
        reason = outcome['type'] if outcome['type'] in OUTCOMES else 'killed'
        out.append(f"{'would remove' if dry_run else 'removed'} 1 spec {row['spec']} reason={reason}")
        if not dry_run:
            _emit(row['spec'], 'queue-remove', project=row['project'], reason=reason)
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
    for row in rows:
        rule, sid = row.get('rule'), row.get('spec')
        if str(row.get('reason', '')).startswith('undetermined:'):
            out.append(f"{sid} {rule}: {row['reason']}")
            continue
        if rule not in ('gate', 'enqueue'):
            continue
        try:
            rung = lane_rules.ladder(events, row, now)
            if rung not in ('act', 'retry') or sid in busy:
                continue
            evs = [e for e in events if e.get('subject') == sid]
            newer = [e for e in evs if lane_rules._time(e['ts']) >= lane_rules._time(row['since'])]
            if any(e.get('type') == 'autodispatched' and e.get('rule') == rule and
                   e.get('since') == row['since'] and e.get('trigger') == 'retry' for e in newer):
                continue
            if rung != 'retry' and any(
                    (e.get('type') == 'autodispatched' and e.get('rule') == rule) or
                    (e.get('type') in ('spawn-started', 'spawn-failed', 'autodispatch-failed') and
                     e.get('role') == row.get('role', rule)) for e in newer):
                continue
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
    return out


def _append(sid, kind, **fields):
    """CLI corrections take the same strict admission path as doit append."""
    result = subprocess.run([DOIT, 'append', kind, sid, *[f'{k}:={json.dumps(v)}' for k, v in fields.items()]],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())


def window_run(project, maximum=6, *, runner=None):
    if project != 'albert-scott' or not 1 <= maximum <= 6:
        print('window-run: only albert-scott is supported; --max must be 1..6')
        return 1
    entries = queue(_ordered(fold.read_events()), project)[:maximum]
    if not entries:
        print('window-run: queue empty')
        return 0
    window = 'window-' + uuid.uuid4().hex
    specs = ','.join(row['spec'] for row in entries)
    _append(window, 'queue-batch', project=project, specs=specs)
    # Read back the batch timestamp; wall-clock guesses cannot delimit outcomes.
    batch = _latest([e for e in _ordered(fold.read_events()) if e.get('subject') == window], {'queue-batch'})
    if batch is None:
        raise ValueError('queue-batch append not admitted')
    if runner is None:
        from autodispatch import _real_runner
        runner = _real_runner
    result = runner(['doit', 'gate-window', window, '--project', project, '--specs', specs])
    code = result.get('code', 1)
    if result.get('stdout'):
        print(result['stdout'].rstrip())
    if result.get('stderr'):
        print(result['stderr'].rstrip(), file=sys.stderr)
    meanings = {0: 'advanced; every spec shipped', 1: 'advanced with dropped specs',
                2: 'window red or unverified', 3: 'another window holds the lock',
                4: 'operator-declared gate bypass'}
    print(f"window-run: {meanings.get(code, f'gate-window exited {code}')}")
    if code != 3:
        fresh = _ordered(fold.read_events())
        still_queued = {r['spec'] for r in queue(fresh)}
        removed = []
        for row in entries:
            if row['spec'] not in still_queued:
                continue
            outcomes = [e for e in fresh if e.get('subject') == row['spec'] and
                        e.get('type') in OUTCOMES and lane_rules._time(e['ts']) > lane_rules._time(batch['ts'])]
            if outcomes:
                reason = outcomes[-1]['type']
                _append(row['spec'], 'queue-remove', project=project, reason=reason, window=window)
                removed.append(f"{row['spec']}={reason}")
        print(f"window-run: removed {len(removed)} spec(s); outcomes={','.join(removed) or 'none'}")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('queue', 'window-run'))
    args, rest = parser.parse_known_args(argv)
    try:
        if args.command == 'window-run':
            p = argparse.ArgumentParser()
            p.add_argument('--project', required=True)
            p.add_argument('--max', type=int, default=6)
            a = p.parse_args(rest)
            return window_run(a.project, a.max)
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
