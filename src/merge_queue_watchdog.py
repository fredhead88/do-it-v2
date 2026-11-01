"""Escalate queued work that has no resolvable owner/action for two hours."""
from datetime import timedelta
from pathlib import Path

import merge_deadman
import merge_queue


def _owned_step(step):
    reason = step.reason.removeprefix('undetermined:').strip().split(':', 1)[0]
    return step.state == 'action' or step.state == 'hold' and (
        not step.reason.startswith('undetermined:') or
        reason in {'base-not-releasable', 'window-in-progress'})


def _window_owners(events):
    """Open windows own their members across candidate rebuilds, until closure."""
    windows = {}
    closed = {'window-verified', 'window-merged', 'window-done', 'window-dropped',
              'window-cancelled', 'window-redcut', 'shipped'}
    for event in merge_queue._ordered(events):
        key = (event.get('project'), event.get('subject'))
        kind = event.get('type')
        if kind in closed:
            windows.pop(key, None)
        elif kind in {'window-opened', 'window-candidate'}:
            specs = event.get('specs', [])
            if isinstance(specs, str):
                specs = specs.split(',')
            windows.setdefault(key, set()).update(specs)
    return {(project, spec) for (project, _), specs in windows.items() for spec in specs}


def due(events, steps, now):
    owned = {s.item for s in steps if _owned_step(s)}
    window_owned = _window_owners(events)
    result = []
    for row in merge_queue.queue(events):
        since = merge_deadman.stamp(row['added_at'])
        if (since is not None and now - since > timedelta(hours=2)
                and row['spec'] not in owned
                and (row['project'], row['spec']) not in window_owned):
            result.append(row)
    return result


def check(events, specs, now, root, *, dry_run=False, steps=None):
    if not merge_queue.queue(events):
        return []
    import lane_rules
    import look
    import nextstep
    if steps is None:
        steps = nextstep.next_steps(events, specs, now, lane_rules.load())
    dst = Path(root) / 'events' / look.LEDGER_NAME
    seen = list(events) + look._read_jsonl(dst)
    keys = {(e.get('spec'), e.get('since')) for e in seen
            if e.get('type') == 'escalation-blocking' and e.get('kind') == 'queue-no-step'}
    pending = [r for r in due(events, steps, now) if (r['spec'], r['added_at']) not in keys]
    if not dry_run:
        for row in pending:
            look._append_watchdog(root, dst, 'escalation-blocking', subject='merge-routing:' + row['spec'],
                kind='queue-no-step', owner='executor', spec=row['spec'], since=row['added_at'],
                why='Queued spec has no resolvable next step for over 2 hours',
                default='Keep the queued branch; Executor repairs merge routing',
                deadline=now.isoformat(), revert='Resume the existing queue after routing is repaired')
    return pending
