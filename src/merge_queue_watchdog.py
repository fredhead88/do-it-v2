"""Escalate queued work that has no resolvable owner/action for two hours."""
from datetime import timedelta
from pathlib import Path

import merge_deadman
import merge_queue


def due(events, steps, now):
    owned = {s.item for s in steps if s.state == 'action' or
             s.state == 'hold' and not s.reason.startswith('undetermined:')}
    result = []
    for row in merge_queue.queue(events):
        since = merge_deadman.stamp(row['added_at'])
        if since is not None and now - since > timedelta(hours=2) and row['spec'] not in owned:
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
