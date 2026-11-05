"""Mechanical grader result when every real AC is delegated elsewhere."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re

import fold
import ledger_cache
import routing

MARKER = 'zero-grader-criteria'
EMPTY = re.compile(r'packet: empty grader criteria for L-spec-\d+: all criteria routed or absent\s*\Z')


def omitted(events, subject):
    own = [e for e in events if e.get('subject') == subject]
    return ({e['criterion'] for e in own if e['type'] == 'owed-ac'}
            | set(fold.routed_criteria(events, subject))
            | set(routing.ruled_criteria(events, subject))) - fold.standing_rejects(own)


def proof(events, subject):
    import autodispatch
    import packet
    own = [e for e in events if e.get('subject') == subject]
    build = next((e for e in reversed(own) if e['type'] == 'build-done'), {})
    trigger, _ = autodispatch._grader_trigger(own)
    if (not trigger or build.get('status') != 'DONE'
            or not re.fullmatch(r'[0-9a-f]{40}', build.get('ready_sha') or '')
            or fold.standing_rejects(own)
            or any(e['type'] in {'shipped', 'spec-killed', 'spec-closed'} for e in own)
            or autodispatch._automatic_hold(own, events)):
        return None
    excluded = omitted(events, subject)
    if not excluded:
        return None
    written = next((e for e in reversed(own) if e['type'] == 'spec-written'), {})
    registered = written.get('path')
    if not isinstance(registered, str) or not registered:
        return None
    try:
        path = Path(registered)
        path = path if path.is_absolute() else fold.ROOT / path
        text = packet.amends.checked_spec(own, subject, path, packet.die).read_text()
        criteria = '\n'.join(packet.criteria(text))
    except (OSError, SystemExit):
        return None
    ids = {cid for cid, _, _ in packet._criterion_spans(criteria, allow_bullets=True)}
    if not ids or not ids <= excluded:
        return None
    routes = fold.routed_criteria(events, subject)
    ruled = routing.ruled_criteria(events, subject)
    delegated = {cid: ('ruled-met' if cid in ruled else routes.get(cid, 'owed')) for cid in sorted(ids)}
    if any(to not in ('reviewer', 'owed', 'ruled-met') for to in delegated.values()):
        return None
    return dict(automatic=MARKER, confirmed=True, n=0, grader_criteria=[],
                ready_sha=build['ready_sha'], trigger_ts=trigger['ts'],
                spec_sha256=hashlib.sha256(text.encode()).hexdigest(), delegated_criteria=delegated)


def is_result(event):
    return (event.get('type') == 'verdict' and event.get('actor') == 'grader'
            and event.get('automatic') == MARKER and event.get('n') == 0
            and event.get('confirmed') is True and bool(event.get('delegated_criteria')))


def packet_failure(event):
    return bool(event.get('type') == 'autodispatch-failed'
                and (event.get('role') == 'grader' or event.get('rule') in ('grade', 'regrade'))
                and EMPTY.fullmatch(str(event.get('reason') or '')))


def clears_failure(failure, event):
    return packet_failure(failure) and is_result(event)


def _candidate(step, events):
    if step.item_kind != 'spec':
        return False
    if step.state == 'action' and step.owner == 'tick' and step.action in ('grade', 'regrade'):
        return True
    if step.state != 'hold' or not step.reason.startswith(('exhausted:', 'retries-exhausted:', 'retry-wait:')):
        return False
    own = [e for e in events if e.get('subject') == step.item]
    import autodispatch
    import nextstep
    own, _ = nextstep._grader_history(own)
    trigger, _ = autodispatch._grader_trigger(own)
    failures = [e for e in own if trigger and fold.ts(e['ts']) >= fold.ts(trigger['ts'])
                and e['type'] in ('spawn-failed', 'autodispatch-failed')]
    return bool(failures) and all(packet_failure(e) for e in failures)


def complete(step, events, *, dry_run=False):
    """No seat or retry: record one scoped verdict, never clear delegated checks."""
    if not _candidate(step, events):
        return False
    evidence = proof(events, step.item)
    if evidence is None:
        return False
    if dry_run:
        return True
    # Tick is already serialized; this also protects an explicit recovery CLI
    # from racing tick; tail refresh observes appends after the first read.
    lock_path = fold.ROOT / 'state' / 'empty-grade.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fresh = ledger_cache.tail_refresh()
        evidence = proof(fresh, step.item)
        if evidence is None:
            return False
        own = [e for e in fresh if e.get('subject') == step.item]
        judged = [e for e in own if e['type'] in ('verdict', 'cannot-assess', 'review')
                  and fold.ts(e['ts']) >= fold.ts(evidence['trigger_ts'])]
        if judged:
            return any(is_result(e) and all(e.get(k) == value for k, value in evidence.items()) for e in judged)
        import dispatch
        destination = fold.EVENTS / 'L-grader-auto.jsonl'
        project = __import__('project_scope').subject_project(own, step.item)
        error = dispatch.emit(destination, dict(subject=step.item, project=project), 'verdict', strict=True, **evidence)
        if error:
            raise ValueError('automatic grade refused: ' + error)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('subject')
    args = parser.parse_args(argv)
    import nextstep
    import lane_rules
    from datetime import datetime, timezone
    events = fold.read_events()
    spec = fold.fold(events)[0].get(args.subject)
    steps = nextstep.next_steps(events, {args.subject: spec} if spec else {},
                                datetime.now(timezone.utc), lane_rules.load())
    for step in steps:
        if step.item == args.subject and complete(step, events):
            print(json.dumps(dict(subject=args.subject, automatic=MARKER, n=0, confirmed=True)))
            return 0
    print('no automatic grade: not an eligible zero-criterion grade')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
