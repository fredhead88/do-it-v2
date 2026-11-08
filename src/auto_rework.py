"""Pure admission of a current grade's first, actionable rejection for rework."""
from lane_rules import _time


def _rejections(evs, verdict):
    clears = {}
    for e in evs:
        if e.get('type') == 'criterion-cleared':
            ac, timestamp = e.get('criterion'), _time(e['ts'])
            if ac not in clears or timestamp > clears[ac]:
                clears[ac] = timestamp
    return [e for e in evs if e.get('type') == 'rejected-criterion'
            and e.get('actor') == 'grader' and e.get('criterion')
            and e.get('spawn') == verdict.get('spawn')
            and _time(e['ts']) >= _time(verdict['ts'])
            and (e['criterion'] not in clears or _time(e['ts']) > clears[e['criterion']])]


def reason(evs, verdict):
    return 'auto-rework: ' + ', '.join(sorted({e['criterion'] for e in _rejections(evs, verdict)}))


def trigger(evs: list, caps: list) -> dict | None:
    """Return the newest verdict only while it authorizes automatic rework.

    caps contains the subject's capability event streams, as used by _regrade.
    Retry timing and budget remain the caller's existing attempts policy.
    """
    verdict = max((e for e in evs if e.get('type') == 'verdict'),
                  key=lambda e: _time(e['ts']), default=None)
    if not verdict or verdict.get('confirmed') is not False or verdict.get('missing_capability'):
        return None
    if any(_time(e['ts']) > _time(verdict['ts']) and (
            e.get('type') in {'build-started', 'build-done', 'spec-written'}
            or e.get('type') == 'decision' and e.get('actor') in {'thinker', 'operator'})
           for e in evs):
        return None
    for stream in caps:
        last = max((e for e in stream if e.get('type') in {'capability-hold', 'unblocked'}),
                   key=lambda e: _time(e['ts']), default=None)
        if last and last['type'] == 'capability-hold':
            return None
    rejected = _rejections(evs, verdict)
    if not rejected:
        return None
    for ac in {e['criterion'] for e in rejected}:
        clear = max((_time(e['ts']) for e in evs
                     if e.get('type') == 'criterion-cleared' and e.get('criterion') == ac), default=None)
        count = sum(e.get('type') == 'rejected-criterion' and e.get('actor') == 'grader'
                    and e.get('criterion') == ac and (clear is None or _time(e['ts']) > clear)
                    for e in evs)
        if count != 1:
            return None
    pre_existing = set(verdict.get('pre_existing') or [])
    for rejection in rejected:
        nodes = {token.rstrip(',;.)') for token in rejection.get('why', '').split() if '::' in token}
        if not nodes or not nodes <= pre_existing:
            return verdict
    return None
