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


def _rows(events, specs, now, table=None):
    """Compatibility row view of tick-owned Steps, with no eligibility table."""
    import nextstep
    rules = lane_rules.load(table)
    steps = (tick._steps(events, specs, now) if table is None else
             nextstep.next_steps(events, specs, now, rules))
    return [fold.step_row(step, rules, now) for step in steps
            if step.owner == 'tick' and step.action in ROLES]


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


def run(events, specs, now, *, runner=None, dry_run=False, handled=frozenset()):
    """Compatibility entry point; tick alone launches routine Steps."""
    return tick.run_steps(events, specs, now, dispatcher=runner or autodispatch._real_runner,
                          dry_run=dry_run, handled=handled)
