"""Ledger-derived window sizing and bounded escalation recovery."""
from datetime import timedelta
import json
import re

import fold
import lane_rules
import rulings

RESULT = 'window-verification-result'
DEFAULT_MINUTES = 30
CLOSED = {'window-verified', 'window-cancelled', 'window-merged', 'window-done'}


def is_window(subject):
    return isinstance(subject, str) and bool(re.fullmatch(r'(?:win|window)-[A-Za-z0-9._-]+', subject))


def adaptive_cap(events, project='albert-scott'):
    """New result events persist the controller; historical rows do not seed it."""
    cap, streak, seen = 4, 0, set()
    for event in events:
        if (event.get('type') != RESULT or event.get('project') != project
                or event.get('actor') not in ('executor', 'operator', 'tick')
                or event.get('state') not in ('green', 'red', 'undetermined')):
            continue
        window = event.get('subject')
        if not isinstance(window, str):
            continue
        first = window not in seen and event.get('first_run') is True
        seen.add(window)
        if event['state'] == 'red':
            cap, streak = max(2, cap // 2), 0
        elif event['state'] == 'green' and first:
            streak += 1
            if streak == 2:
                cap, streak = min(8, cap + 1), 0
        else:
            streak = 0
    return cap


def pending(events, window, now, *, own=None):
    """Legacy prose is not a machine ruling; only an exact trusted ref stops default."""
    if not is_window(window):
        return None
    own = [e for e in events if e.get('subject') == window] if own is None else own
    escalation = next((e for e in reversed(own) if e.get('type') == 'escalation-blocking'), None)
    if not escalation or not escalation.get('_src'):
        return None
    position = own.index(escalation)
    if any(e.get('type') in CLOSED | {'window-opened', 'queue-batch'}
           for e in own[position + 1:]):
        return None
    if now - lane_rules._time(escalation['ts']) < timedelta(minutes=DEFAULT_MINUTES):
        return None
    if any(e.get('ref') == escalation['_src'] and rulings.ruling(e) is not None for e in events):
        return None
    return escalation


def apply_default(window, ref, now, *, dry_run=False):
    """Revalidate under the gate's lifetime lock; replay incomplete writes safely."""
    import merge_queue
    import window_gate
    events = merge_queue._ordered(fold.read_events())
    escalation = pending(events, window, now)
    if escalation is None or escalation['_src'] != ref:
        return False
    if dry_run:
        return True
    project = escalation.get('project')
    with window_gate.chain_lock(window, project, root=fold.ROOT) as holder:
        if holder is not None:
            return False
        events = merge_queue._ordered(fold.read_events())
        escalation = pending(events, window, now)
        if escalation is None or escalation['_src'] != ref:
            return False
        related = [e for e in events if e.get('project') == project and
                   (e.get('subject') == window or e.get('window') == window)]
        members, nodes, attributed = [], set(), set()
        for e in related:
            specs = e.get('specs', [])
            specs = specs.split(',') if isinstance(specs, str) else specs
            if isinstance(specs, list):
                members.extend(s for s in specs if isinstance(s, str) and s not in members)
            for key in ('nodes', 'new_ids'):
                if isinstance(e.get(key), list):
                    nodes.update(n for n in e[key] if isinstance(n, str))
            if e.get('type') == 'window-dropped' and (e.get('owned_nodes')
                    or e.get('reason') in ('owner', 'bisect-owner', 'bisect-single')):
                attributed.add(e['subject'])
        # Snapshot branch metadata before any drop removes its queue membership.
        branches = {e['subject']: e['branch'] for e in events if e.get('project') == project
                    and e.get('type') in ('queue-add', 'merge-gate-clean') and e.get('branch')}
        terminal = {e['subject'] for e in related if e.get('type') in ('shipped', 'merge-conflict')
                    or e.get('type') == 'window-dropped' and e['subject'] in attributed
                    or e.get('type') == 'window-dropped' and e.get('reason') == 'thinker-ruling'}
        terminal.update(e['subject'] for e in events if e.get('project') == project
                        and e.get('type') in ('shipped', 'spec-killed', 'spec-closed'))
        reserved_elsewhere = {r['spec'] for r in merge_queue.queue(events, project)
                              if r.get('batch') and r['batch'] != window}
        targets = [s for s in members if s not in terminal | reserved_elsewhere]
        if any(s not in branches for s in targets if s not in attributed):
            raise ValueError('window default cannot establish requeue branches')
        for spec in targets:
            disposition = 'drop' if spec in attributed else 'requeue'
            done = [e for e in events if e.get('ref') == ref and e.get('subject') == spec]
            if not any(e.get('type') == 'window-dropped' for e in done):
                merge_queue._emit(spec, 'window-dropped', project=project, window=window,
                                  reason='escalation-default', nodes=sorted(nodes),
                                  disposition=disposition, ref=ref)
            if disposition == 'requeue' and not any(e.get('type') == 'queue-add' for e in done):
                merge_queue._emit(spec, 'queue-add', project=project, branch=branches[spec],
                                  rule='escalation-default', nodes=sorted(nodes), ref=ref)
        if not any(e.get('type') == 'unblocked' and e.get('ref') == ref for e in events):
            merge_queue._emit(window, 'unblocked', project=project, ref=ref, reason='escalation-default')
        merge_queue._emit(window, 'window-cancelled', project=project, ref=ref, after='none',
                          reason='escalation-default', nodes=sorted(nodes), specs=members)
        return True


def check_decision(event, events):
    if event.get('type') != 'decision' or not event.get('ref'):
        return
    target = next((e for e in events if e.get('_src') == event['ref']), None)
    if not target or target.get('type') != 'escalation-blocking' or not is_window(target.get('subject')):
        return
    try:
        rulings.validate(event.get('ruling'))
    except (ValueError, TypeError) as exc:
        template = "ruling:='" + json.dumps(dict(kind='drop-from-window', spec='L-spec-NNNN',
                                               window=target['subject'], reason='...')) + "'"
        raise SystemExit('refused: window escalation decision requires a valid ruling: ' + str(exc) + '\n' + template) from exc
