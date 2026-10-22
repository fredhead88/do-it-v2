"""Tick-owned mechanical lane actions. Children use the existing dispatcher."""
from datetime import timedelta
import json
from pathlib import Path
import re
import tempfile

import autodispatch
import dispatch
import fold
import lane_rules
import tick


ROLES = {'review': 'reviewer', 'rebuild': 'builder',
         'regrade': 'grader', 'close': 'operator'}
# SD16: configuration failure must not stop mechanical dispatch. These are
# the shipped limits, used only when neither configured nor packaged table reads.
DEFAULT_LIMITS = dict(build=30, grade=15, review=15, rebuild=15, regrade=15,
                      close=60, gate=30, enqueue=60, merge=120, idle=60,
                      **{'wave-held': 10, 'dependency-held': 10})


def capacity(root=None):
    try:
        import tomllib
        with (Path(root or fold.ROOT) / 'models.toml').open('rb') as stream:
            return int(tomllib.load(stream)['seats']['reviewer'])
    except (OSError, ValueError, KeyError, TypeError):
        return 4


def _time(e):
    return lane_rules._time(e['ts'])


def _latest(events, types):
    return next((e for e in reversed(events) if e['type'] in types), None)


def _projection(events):
    """Normalize only the two event shapes absent from the shipped predicates.

    Keep dispatch and failure events: a standing refusal stays authoritative.
    This is a read-only view; the input fold and ledger are never changed.
    """
    result = []
    for e in events:
        if e['type'] == 'escalation-blocking' and e.get('kind') == 'capability-hold':
            e = dict(e, type='capability-hold')
        if (e['type'] == 'verdict' and e.get('confirmed') is False
                and e.get('cannot_assess') and not e.get('could_not_run')):
            e = dict(e, type='cannot-assess')
        result.append(e)
    return result


def _rows(events, specs, now, table=None):
    # The driver owns these four actions. Idle/merge diagnostics belong to the
    # watchdog and can otherwise delay every answered build behind a full scan.
    rows = lane_rules.due(_projection(events), specs, now, table=table, only=ROLES)
    # Never turn a malformed fold into an actionable supplemental close row.
    undetermined = {r['spec'] for r in rows if r['reason'].startswith('undetermined:')}
    keys = {(r['spec'], r['rule']) for r in rows}
    rules = lane_rules.load(table)
    by_subject = {}
    for e in events:
        by_subject.setdefault(e.get('subject'), []).append(e)
    for sid in specs:
        if sid in undetermined or (sid, 'close') in keys:
            continue
        evs = by_subject.get(sid, [])
        ruling = next((e for e in reversed(evs) if e['type'] == 'decision'
                       and e.get('actor') in ('thinker', 'operator') and e.get('close')), None)
        if (ruling and any(e['type'] == 'shipped' for e in evs)
                and not any(e['type'] in ('spec-killed', 'spec-closed') for e in evs)):
            rows.append(lane_rules._row(sid, 'close', rules['close'],
                                        dict(since=ruling['ts']), now))
    return rows


def _held(events):
    normalized = [dict(e, type='capability-hold')
                  if e['type'] == 'escalation-blocking' and e.get('kind') == 'capability-hold'
                  else e for e in events]
    return fold.held_specs(normalized)


def _busy(events, now):
    # The shipped pure busy predicate uses the injected clock and, unlike
    # tick.in_flight, cannot write spawn-stale during a dry run.
    spawns, _, escalations = lane_rules._busy(events, now)
    return spawns | escalations


def _in_flight_role(events, now, wanted):
    ended = {e.get('spawn') for e in events
             if e['type'] in ('spawn-done', 'spawn-failed', 'spawn-stale')}
    active = set()
    for e in events:
        if e['type'] not in ('spawn-started', 'build-started') or e.get('spawn') in ended:
            continue
        spawn = e.get('spawn', '')
        role = e.get('role') or '-'.join(spawn.split('-')[1:-1]) or ('builder' if e['type'] == 'build-started' else '')
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        if role == wanted and (now - _time(e)).total_seconds() <= ((e.get('window_min') or cap) + cap) * 60:
            active.add(spawn or e['subject'])
    return len(active)


def _suppressed(evs, row):
    since = lane_rules._time(row['since'])
    role = ROLES[row['rule']]
    for e in evs:
        if _time(e) < since:
            continue
        if e['type'] == 'autodispatched' and e.get('rule') == row['rule']:
            return True
        event_role = (e.get('role') or '-'.join(str(e.get('spawn') or '').split('-')[1:-1])
                      or ('builder' if e['type'] in lane_rules.DISPATCH_EVENTS | {'build-started'} else None))
        if e['type'] in lane_rules.DISPATCH_EVENTS | {'build-started'} and event_role == role:
            return True
    return False


def _review_options(evs, sid):
    rounds = [e for e in evs if e['type'] == 'review'
              and (e.get('confirmed') is False or e.get('n_blocking', 0) > 0)]
    round_n = max([1] + [int(e.get('round', 1)) + 1 for e in rounds])
    if round_n > 2:
        return None
    judging = _latest(evs, lane_rules.JUDGING)
    if judging and judging['type'] == 'cannot-assess' or (
            judging and judging.get('confirmed') is False and judging.get('cannot_assess')):
        return 'gates-only', round_n
    text = dispatch.spec_path(sid).read_text()
    ui = bool(re.search(r'\[ui\]', text, re.I))
    if ui and not (fold.ROOT / 'review-mcp.json').is_file():
        return 'needs-browser', round_n
    carried = _latest(evs, {'spec-carried'})
    depth = carried['tier'] if carried else ('full' if ui else 'gates-only')
    return depth, round_n


def _dispatch(role, sid, project, charter, runner, options=None):
    wt = fold.ROOT / 'worktrees' / project / sid.lower()
    if not wt.is_dir():
        raise ValueError(f'worktree {wt} missing')
    argv = ['doit', 'packet', role, sid, '--worktree', str(wt)]
    if role == 'builder':
        argv += ['--repo', str(fold.ROOT / 'repos' / project)]
    if options:
        argv += ['--depth', options[0], '--round', str(options[1])]
    packet = runner(argv)
    if packet.get('code', 1):
        raise ValueError(packet.get('stderr') or packet.get('stdout') or 'packet failed')
    argv = ['doit', 'dispatch', '--detach', role, sid, '--packet', packet.get('stdout', '').strip(),
            '--cwd', str(wt), '--charter', charter or '', '--project', project]
    if role == 'grader':
        argv += ['--timeout', '30']
    result = runner(argv)
    if result.get('code', 1):
        raise ValueError(result.get('stderr') or result.get('stdout') or 'dispatch failed')
    return json.loads(result.get('stdout') or '')['detached']


def _act(events, specs, now, rows, runner, dry_run, handled):
    out, handled = [], set(handled)
    undetermined = {r['spec'] for r in rows if r['reason'].startswith('undetermined:')}
    out.extend(f"undetermined {r['spec']} {r['rule']}: {r['reason']}"
               for r in rows if r['spec'] not in handled and r['reason'].startswith('undetermined:'))
    rows = [r for r in rows if r['spec'] not in undetermined]
    if not rows:
        return out
    by_subject = {}
    for e in events:
        by_subject.setdefault(e.get('subject'), []).append(e)
    busy, held = _busy(events, now), _held(events)
    slots = max(0, capacity() - _in_flight_role(events, now, 'reviewer'))
    build_slots = max(0, autodispatch.capacity() - _in_flight_role(events, now, 'builder')
                      - sum(specs.get(sid, {}).get('state') == 'written' for sid in handled))
    for row in sorted(rows, key=lambda r: (r['since'], r['spec'])):
        sid, rule = row['spec'], row['rule']
        if sid in handled:
            continue
        if rule not in ROLES:
            continue
        evs = by_subject.get(sid, [])
        if sid in busy:
            out.append(f'busy {sid} {rule}')
            continue
        if sid in held:
            out.append(f'held {sid} {rule}: capability hold')
            continue
        if _suppressed(evs, row):
            out.append(f'suppressed {sid} {rule}: already attempted since {row["since"]}')
            continue
        dependency = autodispatch.held_by_dependency(evs, events)
        if dependency:
            out.append(f'held {sid} {rule}: {dependency}')
            continue
        if rule != 'close' and any(e['type'] in lane_rules.TERMINAL_EVENTS for e in evs):
            out.append(f'suppressed {sid} {rule}: terminal')
            continue
        role = ROLES[rule]
        if role == 'builder' and build_slots <= 0:
            out.append(f'seat-wait {sid} {rule}')
            continue
        if rule == 'review' and slots <= 0:
            out.append(f'seat-wait {sid} review')
            continue
        if rule == 'close' and any(e['type'] in ('spec-killed', 'spec-closed') or
                e['type'] == 'escalation-blocking' and e.get('kind') == 'close-needs-operator'
                and _time(e) >= lane_rules._time(row['since']) for e in evs):
            out.append(f'suppressed {sid} close: already closed or escalated')
            continue
        if dry_run:
            if rule == 'review' and _review_options(evs, sid) is None:
                out.append(f'suppressed {sid} review: round limit')
                continue
            out.append(f'would act {sid} {rule}')
            handled.add(sid)
            if rule == 'review':
                slots -= 1
            if rule == 'rebuild':
                build_slots -= 1
            continue
        handled.add(sid)
        base = {'subject': sid}

        def fail(reason):
            refusal = dispatch.emit(tick.tick_path(), base, 'autodispatch-failed',
                                    role=role, rule=rule, reason=str(reason))
            out.append(f'failed {sid} {rule}: {reason}' + (f'; failure record refused: {refusal}' if refusal is not None else ''))

        def record(event_type, **fields):
            refusal = dispatch.emit(tick.tick_path(), base, event_type, **fields)
            if refusal is not None:
                fail(f'{event_type} record refused: {refusal}')
                return False
            return True

        try:
            if rule == 'close':
                ruling = next(e for e in evs if e['type'] == 'decision' and e['ts'] == row['since']
                              and e.get('actor') in ('thinker', 'operator') and e.get('close'))
                if any(e['type'] == 'shipped' for e in evs):
                    landed = record('escalation-blocking', kind='close-needs-operator', owner='operator',
                                    why=f"ruling {ruling.get('_src')}: {ruling['close']}",
                                    default='leave the spec open', deadline=(now + timedelta(hours=72)).isoformat(),
                                    revert='nothing changed')
                else:
                    landed = record('spec-killed', why=f"ruling {ruling.get('_src')}: {ruling['close']}")
                if landed:
                    out.append(f'closed ruling {sid}')
                continue
            project = next((e['project'] for e in reversed(evs) if e.get('project')), 'unknown')
            charter = dispatch.resolve_charter(evs, sid, None)
            spawn = 'none'
            if rule == 'review':
                options = _review_options(evs, sid)
                if options and options[0] == 'needs-browser':
                    if record('escalation-blocking', kind='review-needs-browser', owner='operator',
                              why='ui review requires review-mcp.json', default='leave review pending',
                              deadline=(now + timedelta(hours=72)).isoformat(), revert='nothing changed'):
                        out.append(f'escalated {sid} review needs browser')
                    continue
                if options:
                    spawn = _dispatch(role, sid, project, charter, runner, options)
                    slots -= 1
            else:
                spawn = _dispatch(role, sid, project, charter, runner)
            if spawn == 'none':
                out.append(f'suppressed {sid} {rule}: round limit')
                continue
            fields = dict(role=role, rule=rule, since=row['since'], spawn=str(spawn))
            if record('autodispatched', **fields):
                out.append(f'acted {sid} {rule} spawn={spawn}')
            if role == 'builder' and spawn != 'none':
                build_slots -= 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            fail(exc)
    return out


def run(events, specs, now, *, runner=None, dry_run=False, handled=frozenset()):
    events = sorted(events, key=lambda e: e.get('ts', ''))
    runner = runner or autodispatch._real_runner
    try:
        lane_rules.load()
    except ValueError as exc:
        warning = ['lane-rules unreadable: ' + str(exc).replace('\n', ' ')]
        # due(table=...) accepts a path, not a dict. A private transient table
        # preserves its predicate implementation even if the packaged file broke.
        with tempfile.TemporaryDirectory(prefix='lane-driver-rules-') as directory:
            table = Path(directory) / 'rules.toml'
            table.write_text('\n'.join(f'[[rules]]\nname = "{name}"\naction = "none"\nactor = "tick"\nlimit_min = {limit}\n'
                                       for name, limit in DEFAULT_LIMITS.items()))
            return warning + _act(events, specs, now, _rows(events, specs, now, table), runner, dry_run, handled)
    return _act(events, specs, now, _rows(events, specs, now), runner, dry_run, handled)
