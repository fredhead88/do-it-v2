"""Read-only, ordered SD2 explanations of lifecycle waits.

Age selects rows; the next action's trigger sets its deadline. Incidental
observations and refreshed seat diagnostics never renew that deadline.
"""
from collections import defaultdict
from datetime import timedelta
import re

import fold

BUCKET_ORDER = ('STUCK', 'held', 'audit-wait', 'retry-wait', 'in-flight', 'queued', 'waiting-on-human')
BUCKETS = frozenset(BUCKET_ORDER)
RESETS = {'spec-written', 'spec-amended', 'build-started', 'build-done', 'build-blocked',
          'conflict-rework', 'merge-conflict', 'window-dropped', 'evidence-bundle-failed',
          'grading-preflight-failed', 'rejected-criterion', 'must-fix', 'merge-gate-failed',
          'verdict', 'cannot-assess'}


def text(value):
    import unicodedata
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('C') else c
                            for c in str(value)).split())[:300]


def latest(evs, kinds):
    return next((e for e in reversed(evs) if e.get('type') in kinds), None)


def role_of(e):
    return e.get('role') or fold.actor_of(str(e.get('spawn', '')) + '.jsonl')


def prepare(by_subject, now):
    events = sorted((e for evs in by_subject.values() for e in evs
                     if fold.ts(e.get('ts')) <= now), key=lambda e: fold.ts(e['ts']))
    caps = fold.capability_holds(events)
    held = {}
    for subject in caps:
        for sid in fold.held_specs(by_subject[subject]):
            held.setdefault(sid, []).append(subject)
    opened = {}
    for e in events:
        kind, subject = e.get('type'), e.get('subject')
        if e.get('actor') != 'executor':
            continue
        if kind == 'window-opened':
            opened[subject] = set(e.get('specs') or [])
        elif kind == 'window-candidate' and subject in opened:
            opened[subject] = set(e.get('specs') or [])
        elif kind in {'window-verified', 'window-cancelled', 'escalation-blocking'}:
            opened.pop(subject, None)
        elif kind == 'window-dropped' and subject in opened:
            opened.pop(subject)
        elif kind in {'window-dropped', 'window-deferred', 'merge-conflict', 'shipped'}:
            for name, members in opened.items():
                if kind == 'shipped' or name == e.get('window'):
                    members.discard(subject)
    windows = defaultdict(list)
    for name, members in opened.items():
        for sid in members:
            windows[sid].append(name)
    return dict(events=events, answered=fold.answered(events), capabilities=caps,
                capability_specs=held, windows=windows,
                shipped={e.get('subject') for e in events if e.get('type') == 'shipped'})


def action(evs):
    """Only lifecycle actions/explicit new instructions establish a trigger."""
    trigger = latest(evs, {'spec-written', 'spec-amended', 'unblocked', 'decision',
                           'build-done', 'build-blocked', 'verdict', 'cannot-assess',
                           'review', 'merge-gate-clean', 'conflict-rework', 'merge-conflict',
                           'window-dropped', 'must-fix', 'rejected-criterion',
                           'evidence-bundle-failed', 'evidence-bundle'})
    if not trigger:
        return None, 'builder'
    kind = trigger['type']
    if kind in {'build-done', 'evidence-bundle', 'evidence-bundle-failed'}:
        role = 'grader'
    elif kind in {'verdict', 'cannot-assess'}:
        role = 'reviewer'
    else:
        role = 'builder'
    if kind == 'decision' and trigger.get('regrade') == 'yes':
        role = 'grader'
    return trigger, role


def classify(spec, specs, by_subject, context, now, dwell):
    sid = spec['id']
    evs = [e for e in spec.get('evs', []) if fold.ts(e['ts']) <= now]
    if spec['state'] not in dwell or spec['age'] <= dwell[spec['state']]:
        return None
    trigger, role = action(evs)

    def result(bucket, reason, owner='executor', event=None, blocker=None, deadline=None,
               hold_kind=None, due=None):
        anchor = event or trigger or (evs[0] if evs else {})
        if bucket == 'STUCK' and due is None:
            due = fold.ts(anchor['ts']) if anchor else now
        return dict(spec=sid, state=spec['state'], bucket=bucket, hold_kind=hold_kind,
                    due_since=due.isoformat() if bucket == 'STUCK' else None,
                    reason=text(reason), owner=text(owner), blocker=blocker, deadline=deadline,
                    since=anchor.get('ts'), src=anchor.get('_src'), age_days=spec['age'],
                    limit_min=dwell[spec['state']] * 1440)

    import autodispatch
    import dispatch
    from realm_glance import recorded_hold
    # Rule 1: only recorded holds; neither active rework nor arbitrary prose.
    dependency = autodispatch.held_by_dependency(evs, context['events'], shipped=context['shipped'])
    if dependency and 'unresolvable' not in dependency:
        hold = latest(evs, {'dependency-hold'})
        return result('held', dependency, hold.get('owner') or hold.get('actor'), hold,
                      blocker=hold.get('waits_for'), hold_kind='dependency')
    if sid in context['capability_specs']:
        subject = context['capability_specs'][sid][0]
        hold = latest(by_subject[subject], {'capability-hold'})
        return result('held', 'capability-hold: ' + str(context['capabilities'][subject]['capability']),
                      hold.get('owner') or hold.get('actor'), hold, blocker=subject, hold_kind='capability')
    decisions = sorted([e for subject in (sid, 'lane-stale:' + sid)
                        for e in by_subject.get(subject, []) if fold.ts(e['ts']) <= now
                        and e.get('type') in {'decision', 'unblocked'}
                        and e.get('actor') in {'thinker', 'operator', 'executor'}], key=lambda e: fold.ts(e['ts']))
    ruling = decisions[-1] if decisions else None
    if ruling and ruling['type'] == 'decision':
        why = str(ruling.get('why', ''))
        named = ruling.get('waits_for')
        if not named and why.lower().startswith('dependency-held'):
            match = re.search(r'waits? for (L-spec-\d+[a-z]?)\b', why)
            named = match.group(1) if match else None
        if named and named != sid and named not in context['shipped']:
            return result('held', 'dependency-held ruling: waits for ' + named,
                          ruling.get('owner') or ruling['actor'], ruling, blocker=named, hold_kind='dependency')
        if recorded_hold(ruling) and not named:
            kind = 'operator' if ruling['actor'] == 'operator' else ('wave' if why.lower().startswith('wave-held') else 'decision')
            return result('held', why, ruling.get('owner') or ruling['actor'], ruling, hold_kind=kind)
    written = latest(evs, {'spec-written'})
    charter = dispatch.resolve_charter(context['events'], sid, None)
    wave = dispatch.spec_wave(charter, written.get('footprint')) if charter and written else None
    blocker = dispatch.wave_blocker(context['events'], charter, sid, wave)
    if blocker:
        return result('held', 'earlier wave has not shipped: ' + blocker, event=written,
                      blocker=blocker, hold_kind='wave')
    blocked = latest(evs, {'blocked', 'unblocked', 'decision'})
    if blocked and blocked['type'] == 'blocked':
        blocker = blocked.get('waits_for') or blocked.get('blocker') or blocked.get('blocked_by')
        if blocker and blocker != sid and fold.spec_state(by_subject.get(blocker, []), set()) not in dispatch.SETTLED_STATES:
            return result('held', 'prerequisite has not shipped: ' + str(blocker),
                          blocked.get('owner') or 'executor', blocked, blocker=blocker, hold_kind='prerequisite')
    if not trigger:
        return result('STUCK', 'next action trigger unavailable: no ledger evidence for the next action')
    # SD1 owns live/dead/failure accounting, including bundle-pending refusals.
    import attempts
    first_written = next((e for e in evs if e.get('type') == 'spec-written'), None)
    audits = [e for e in evs if e.get('type') == 'spawn-done' and role_of(e) == 'spec-auditor']
    audit_needed = (first_written and first_written.get('actor') == 'spec-writer'
                    and not any(e.get('type') == 'spec-carried' for e in evs) and not audits)
    audit_trigger = written if audit_needed else None
    if not audit_needed and role == 'builder' and audits:
        trigger = max([trigger, *audits], key=lambda e: fold.ts(e['ts']))
    try:
        attempt = attempts.history(evs, audit_trigger or trigger,
                                   'spec-auditor' if audit_needed else role, now=now)
    except Exception as exc:
        return result('STUCK', 'attempt history unavailable: ' + str(exc))
    if attempt.live:
        owner = 'spec-auditor' if audit_needed else role
        return result('in-flight', f'{owner} has a live assigned run', owner, audit_trigger or trigger)
    if audit_needed:
        due = fold.ts(written['ts']) + timedelta(minutes=30)
        if not attempt.exhausted and (now <= due or attempt.next_retry_at and now <= attempt.next_retry_at):
            return result('audit-wait', 'waiting for the first blind spec audit', event=written)
        failed = latest(evs, {'spawn-failed', 'spawn-stale', 'autodispatch-failed'})
        exhausted_at = fold.ts(failed['ts']) if failed else fold.ts(written['ts'])
        return result('STUCK', 'first blind spec audit is overdue', event=written,
                      due=exhausted_at if attempt.exhausted else attempt.next_retry_at or due)
    if attempt.next_retry_at and now <= attempt.next_retry_at:
        return result('retry-wait', 'retry scheduled for ' + attempt.next_retry_at.isoformat(), event=trigger)
    # Rules 5 and 6 still precede an overdue retry's final STUCK result.
    pending = [e for e in evs if e.get('type') in ('question', 'escalation-blocking')
               and e.get('_src') not in context['answered']]
    if pending:
        q = pending[-1]; deadline = q.get('deadline'); due = None
        if str(deadline).strip().lower() in {'before merge', 'before deploy'}:
            if not fold.deadline_passed(deadline, context['events'], sid):
                return result('waiting-on-human', 'open question due ' + str(deadline), q.get('owner') or 'thinker', q, deadline=deadline)
            due = fold.ts(q['ts'])
        else:
            from realm_glance import utc
            due = utc(deadline)
        if due and now < due:
            return result('waiting-on-human', q.get('asks') or q.get('why') or 'open question', q.get('owner') or 'thinker', q, deadline=deadline)
        return result('STUCK', 'human deadline passed: ' + str(q.get('asks') or q.get('why')) if due else
                      'open question has no resolvable deadline; obtain a ruling',
                      q.get('owner') or 'thinker', q, deadline=deadline, due=due)
    windows = context['windows'].get(sid, [])
    if windows:
        return result('queued', 'in merge window ' + ', '.join(sorted(windows)))
    last_reset = max((i for i, e in enumerate(evs) if e['type'] in RESETS or (
        e['type'] == 'decision' and (e.get('rework') == 'yes' or e.get('regrade') == 'yes' or e.get('close')))), default=-1)
    reviews = [(i, e) for i, e in enumerate(evs) if e['type'] == 'review']
    clean = [(i, e) for i, e in enumerate(evs) if e['type'] == 'merge-gate-clean'
             or e['type'] == 'review' and type(e.get('n_blocking')) is int and e['n_blocking'] == 0]
    if clean and clean[-1][0] > last_reset and not (reviews and reviews[-1][1].get('n_blocking') != 0) and not fold.standing_rejects(evs):
        ready = clean[-1][1]; due = fold.ts(ready['ts']) + timedelta(minutes=120)
        if now <= due:
            return result('queued', 'awaiting merge window after ' + ready['type'], event=ready)
        return result('STUCK', 'waited over 2 h for a merge window', event=ready, due=due)
    if attempt.exhausted:
        failed = latest(evs, {'spawn-failed', 'spawn-stale', 'autodispatch-failed', 'spawn-done'})
        return result('STUCK', 'retry attempts exhausted: ' + '; '.join(attempt.reasons), event=failed)
    if attempt.next_retry_at:
        return result('STUCK', 'scheduled retry is overdue', due=attempt.next_retry_at)
    due = fold.ts(trigger['ts']) + timedelta(minutes=30)
    if now <= due:
        seat = False
        if spec['state'] == 'written' and not attempt.n_failed:
            if 'candidates' not in context:
                context['candidates'] = {r['spec']: r for r in autodispatch.candidates(context['events'], specs, now)}
            seat = context['candidates'].get(sid, {}).get('status') == 'seat-wait'
        return result('queued', 'waiting for a seat' if seat else 'awaiting assignment')
    reason = 'ordinary next action has no assigned run'
    if dependency:
        reason = dependency
    elif trigger['type'] == 'evidence-bundle-failed':
        reason = 'evidence bundle failed and not rerun: ' + str(trigger.get('reason'))
    elif trigger['type'] in {'conflict-rework', 'merge-conflict'}:
        reason = 'conflict-rework has no active builder dispatch'
    elif trigger['type'] == 'decision':
        reason = 'decision not carried out: ' + str(trigger.get('why'))
    elif fold.standing_rejects(evs):
        reason = 'standing rejected criteria remain: ' + ', '.join(sorted(fold.standing_rejects(evs)))
    return result('STUCK', reason, due=due)


def rows(specs, by_subject, now, dwell):
    candidates = [s for s in specs.values() if s['state'] in dwell and s['age'] > dwell[s['state']]]
    if not candidates:
        return []
    context = prepare(by_subject, now)
    out = []
    for spec in sorted(candidates, key=lambda s: (-s['age'], s['id'])):
        try:
            out.append(classify(spec, specs, by_subject, context, now, dwell))
        except Exception as exc:
            raise ValueError(f'classification unavailable for {spec["id"]}: {exc}') from exc
    return out
