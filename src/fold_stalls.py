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
    block = block_evidence(evs) if spec['state'] == 'blocked' else None

    def result(bucket, reason, owner='executor', event=None, blocker=None, deadline=None,
               hold_kind=None, due=None):
        human = event and event.get('type') in {'question', 'escalation-blocking'}
        if block and bucket == 'STUCK' and not human:
            reason, owner = block
        anchor = event or trigger or (evs[0] if evs else {})
        if bucket == 'STUCK' and due is None:
            due = fold.ts(anchor['ts']) if anchor else now
        return dict(spec=sid, state=spec['state'], bucket=bucket, hold_kind=hold_kind,
                    due_since=due.isoformat() if bucket == 'STUCK' else None,
                    reason=text(reason), why=block[0] if block else text(reason),
                    owner=text(owner), blocker=blocker, deadline=deadline,
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


def dwell_days(by_subject):
    """Expected dwell per state, measured from the log's own stage crossings — the
    fallback stands until fold.DWELL_MIN_N specs have crossed. Twice the MEDIAN crossing,
    not twice the longest: one spec that sat over a weekend would otherwise raise
    the bar for every spec after it, and a wedge alarm nothing can trip is worse
    than no alarm."""
    import statistics
    out = dict(fold.DWELL_DAYS)
    for state, (enter, leave) in fold.STAGES.items():
        obs = []
        for evs in by_subject.values():
            a = next((e for e in evs if e["type"] == enter), None)
            b = a and next((e for e in evs if e["type"] == leave
                            and fold.ts(e["ts"]) >= fold.ts(a["ts"])), None)
            if b:
                obs.append((fold.ts(b["ts"]) - fold.ts(a["ts"])).total_seconds() / 86400)
        if len(obs) >= fold.DWELL_MIN_N:
            out[state] = 2 * statistics.median(obs)
    return out


def stall_rows(events=(), specs=None, *, now=None, dwell=None, by_subject=None):
    """One classified row per aged spec, shared by board and realm.

    Supply the already-folded by_subject to avoid folding twice. This API only
    reads ledger facts and prerequisite plans; it never ticks or dispatches.
    """
    if by_subject is None:
        folded = fold.fold(events)
        specs = folded[0] if specs is None else specs
        by_subject = folded[3]
    clock = now or fold.NOW
    specs = {sid: {**s, 'age': (clock - fold.ts(s['evs'][-1]['ts'])).total_seconds() / 86400}
             for sid, s in (specs or {}).items()}
    return rows(specs, by_subject, clock, dwell or fold.dwell_days(by_subject))


def wedged(spec, dwell=None, *, events=None, specs=None, by_subject=None, now=None):
    """Only classified STUCK is a WEDGE; pass ledger context for external holds.

    The two-argument form retains standalone spec checks. Cross-spec wave and
    capability facts require the same admitted context the board/realm use.
    """
    if dwell is None:
        import dispatch
        selected = {sid: s for sid, s in (specs or {spec['id']: spec}).items()
                    if s['state'] != 'unknown' and s['state'] not in dispatch.SETTLED_STATES}
        specs = selected
        dwell = {s['state']: -1 for s in selected.values()}
        if spec['id'] not in selected:
            return False
    if events is not None or by_subject is not None:
        found = fold.stall_rows(events or (), specs or {spec['id']: spec}, now=now,
                           dwell=dwell, by_subject=by_subject)
        return any(r['spec'] == spec['id'] and r['bucket'] == 'STUCK' for r in found)
    found = rows({spec['id']: spec}, {spec['id']: spec.get('evs', [])}, now or fold.NOW,
                 dwell or fold.DWELL_DAYS)
    return bool(found and found[0]['bucket'] == 'STUCK')


def stall_lane_rows(events, specs=None, now=None, *, by_subject=None):
    """Realm's lane-compatible view; reason_live is never guessed from age."""
    import dispatch
    if specs is None or by_subject is None:
        folded = fold.fold(events)
        specs = folded[0] if specs is None else specs
        by_subject = folded[3] if by_subject is None else by_subject
    unfinished = {sid: s for sid, s in specs.items()
                  if s['state'] != 'unknown' and s['state'] not in dispatch.SETTLED_STATES}
    return [dict(r, rule='stalls', action=r['reason'], actor=r['owner'],
                 overdue=r['bucket'] == 'STUCK', reason_live=r['bucket'] != 'STUCK')
            for r in fold.stall_rows(events, unfinished, now=now, by_subject=by_subject,
                                dwell={s['state']: -1 for s in unfinished.values()})]


def step_row(step, limits, now):
    """Map a Step to the legacy dwell row; an unknown age is always overdue."""
    import lane_rules
    reason = step.reason
    if step.state == 'hold':
        rule = ('undetermined' if reason.startswith('undetermined:') else
                'dependency-held' if reason.startswith('held:') else
                'wave-held' if reason.startswith('wave') else 'idle')
    else:
        rule = 'gate' if step.action == 'merge-gate' else step.action
    limit = 0 if rule == 'undetermined' else limits[rule]['limit_min']
    try:
        overdue = (now - lane_rules._time(step.since)).total_seconds() > limit * 60
    except (ValueError, TypeError, OverflowError):
        rule, limit, overdue = 'undetermined', 0, True
    return dict(spec=step.item, rule=rule, since=step.since, reason=reason,
                action=step.action or 'none', limit_min=limit, overdue=overdue,
                reason_live=step.owner in {'tick', 'relay', 'sweeper'} and step.trigger is not None)


def _step_line(value):
    """Ledger text cannot add a physical board line or a field separator."""
    import unicodedata
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('C') else c
                            for c in str(value)).replace(' · ', ' / ').split())


def newest_builder_outcome(evs):
    """Admitted order breaks timestamp ties; body actor claims have no authority."""
    return next((e for e in reversed(evs) if e.get('type') == 'build-done'
                 and e.get('actor') == 'builder'), None)


def unsuccessful_build(evs):
    outcome = newest_builder_outcome(evs)
    if outcome is None or outcome.get('status') == 'DONE':
        return None
    index = next(i for i, e in enumerate(evs) if e is outcome)
    if any(e.get('type') == 'verdict' for e in evs[index + 1:]):
        return None
    return outcome


def block_evidence(evs):
    """A block belongs to the current spawn, or its legacy build interval."""
    outcome = unsuccessful_build(evs)
    if outcome is None:
        return None
    index = next(i for i, e in enumerate(evs) if e is outcome)
    boundary = lambda e: e.get('actor') == 'builder' and e.get('type') in {'build-started', 'build-done'}
    start = max((i for i, e in enumerate(evs[:index]) if boundary(e)), default=-1)
    end = next((i for i in range(index + 1, len(evs)) if boundary(evs[i])), len(evs))
    spawn = outcome.get('spawn')
    blocks = [e for i, e in enumerate(evs) if e.get('type') == 'build-blocked'
              and e.get('actor') == 'builder' and e.get('subject') == outcome.get('subject')
              and (e.get('spawn') == spawn if spawn and e.get('spawn')
                   else start < i < end)]
    block = blocks[-1] if blocks else None
    if block and block.get('reason'):
        return text(block['reason']), text(block.get('owner') or 'executor')
    status = repr(outcome['status']) if 'status' in outcome else 'absent'
    return text(f'builder returned status {status}; no recorded block line'), 'executor'


def in_flight_lines(events, specs, stalls, flag):
    rows = []
    for s in specs.values():
        if s['state'] == 'building':
            line = f"{s['id']} · {s['age']:.1f}d"
        elif s['state'] == 'blocked':
            why, owner = block_evidence(s['evs'])
            r = stalls.get(s['id'], {})
            line = (f"{s['id']} · blocked · {s['age']:.1f}d · {r.get('why', why)}"
                    f" · owner {r.get('owner', owner)}")
        else:
            continue
        rows.append((fold._spec_row_sort_key(s), line + flag(s)))
    rows += [((-fold.age_days(e), e.get('subject', '?'), fold.raw_content_json(e)),
              f"deploy in flight · {e.get('subject','?')} · {str(e.get('sha',''))[:7]} · "
              f"{fold.age_days(e):.1f}d") for e in fold.in_flight_deploys(events)]
    return [line for _, line in sorted(rows, key=lambda r: r[0])]


def verification_lines(specs, flag):
    return [f"{s['id']} · {s['state']} · {s['age']:.1f}d"
            + (f"  ⚠ {s['rejects']} REJECTED, needs rework" if s['rejects'] else '') + flag(s)
            for s in sorted((s for s in specs.values() if s['state'] in {'graded', 'reviewing', 'shipped'}),
                            key=fold._spec_row_sort_key)]
