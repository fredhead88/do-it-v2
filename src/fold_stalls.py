"""Read-only explanations of aged specs. Public entry points live in fold.py.

Age selects candidates; it is never proof of a stall. Explicit holds/questions
win over readiness. A clean result is invalidated by later rework or failure.
All inputs are fold-admitted facts; no dispatch, ledger append or pane probe.
"""
from collections import defaultdict
from datetime import timedelta
import re

import fold

BUCKETS = ('queued', 'held', 'waiting-on-prereq', 'waiting-on-human', 'STUCK')
RESETS = {'spec-written', 'build-started', 'build-done', 'build-blocked',
          'conflict-rework', 'merge-conflict', 'window-dropped',
          'evidence-bundle-failed', 'grading-preflight-failed',
          'rejected-criterion', 'must-fix', 'merge-gate-failed', 'verdict', 'cannot-assess'}


def text(value):
    import unicodedata
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('C') else c
                            for c in str(value)).split())[:300]


def latest(evs, kinds):
    return next((e for e in reversed(evs) if e.get('type') in kinds), None)


def prepare(by_subject, now):
    import dispatch
    events = sorted((e for evs in by_subject.values() for e in evs),
                    key=lambda e: str(e.get('ts', '')))
    ended = fold.terminal_spawns(events)
    active = defaultdict(list)
    for e in events:
        if e.get('type') not in ('spawn-started', 'build-started'):
            continue
        spawn = e.get('spawn')
        if not spawn or spawn in ended:
            continue
        role = e.get('role') or ('builder' if e['type'] == 'build-started'
                                  else fold.actor_of(spawn + '.jsonl'))
        cap = dispatch.ROLES.get(role, (None, 60, 0))[1]
        deadline = fold.ts(e['ts']) + timedelta(minutes=(e.get('window_min') or cap) + cap)
        if fold.ts(e['ts']) <= now < deadline:
            active[e.get('subject')].append((e, role, deadline))
    caps = fold.capability_holds(events)
    held = {}
    for subject in caps:
        for sid in fold.held_specs(by_subject[subject]):
            held.setdefault(sid, []).append(subject)
    windows = defaultdict(list)
    for subject, rows in by_subject.items():
        window = latest(rows, {'window-opened', 'window-candidate', 'window-verified',
                               'window-dropped', 'escalation-blocking'})
        if window and window['type'] in ('window-opened', 'window-candidate'):
            for sid in window.get('specs') or []:
                windows[sid].append(subject)
    return dict(events=events, answered=fold.answered(events), active=active,
                capabilities=caps, capability_specs=held, windows=windows,
                shipped={e.get('subject') for e in events if e.get('type') == 'shipped'})


def classify(spec, specs, by_subject, context, now, dwell):
    sid, evs = spec['id'], spec.get('evs', [])
    if spec['state'] not in dwell or spec['age'] <= dwell[spec['state']]:
        return None

    def result(bucket, reason, owner='executor', event=None, blocker=None, deadline=None):
        anchor = event or (evs[-1] if evs else {})
        return dict(spec=sid, state=spec['state'], bucket=bucket,
                    reason=text(reason), owner=text(owner), blocker=blocker,
                    deadline=deadline, since=anchor.get('ts'), src=anchor.get('_src'),
                    age_days=spec['age'], limit_min=dwell[spec['state']] * 1440)

    if not evs:
        return result('STUCK', 'no ledger evidence for the next action')
    import autodispatch
    dependency = autodispatch.held_by_dependency(evs, context['events'], shipped=context['shipped'])
    if dependency:
        hold = latest(evs, {'dependency-hold'})
        if 'unresolvable' in dependency:
            return result('STUCK', dependency, event=hold)
        return result('held', dependency, hold.get('owner') or hold['actor'],
                      hold, blocker=hold.get('waits_for'))
    if sid in context['capability_specs']:
        subject = context['capability_specs'][sid][0]
        row = context['capabilities'][subject]
        hold = latest(by_subject[subject], {'capability-hold'})
        return result('held', 'capability-hold: ' + str(row['capability']),
                      hold.get('owner') or hold['actor'], hold, blocker=subject)

    # Older ledgers express a few explicit dependency holds as decisions. Read
    # only a named, authorized dependency-held ruling, never arbitrary "hold"
    # prose or an entire free-text mention of another spec.
    ruling = latest(evs, {'decision', 'unblocked'})
    if ruling and ruling['type'] == 'decision' and ruling.get('actor') in ('thinker', 'operator', 'executor'):
        why = str(ruling.get('why', ''))
        named = ruling.get('waits_for')
        if not named and why.lower().startswith('dependency-held'):
            match = re.search(r'waits? for (L-spec-\d+[a-z]?)\b', why)
            named = match.group(1) if match else None
        if named and named != sid and not any(e['type'] == 'shipped'
                                              for e in by_subject.get(named, [])):
            return result('held', 'dependency-held ruling: waits for ' + named,
                          ruling.get('owner') or ruling['actor'], ruling, blocker=named)

    pending = [e for e in evs if e.get('type') in ('question', 'escalation-blocking')
               and e.get('_src') not in context['answered']]
    if pending:
        question = pending[-1]
        owner = question.get('owner') or ('operator' if question.get('actor') == 'operator' else 'thinker')
        deadline = question.get('deadline')
        if str(deadline).strip().lower() in ('before merge', 'before deploy'):
            passed = fold.deadline_passed(deadline, context['events'], sid)
            return result('STUCK' if passed else 'waiting-on-human',
                          ('human deadline passed: ' if passed else 'open question due ' + str(deadline) + ': ')
                          + str(question.get('asks') or question.get('why')),
                          owner, question, deadline=deadline)
        try:
            from datetime import datetime, timezone
            due = datetime.fromisoformat(str(deadline).replace('Z', '+00:00'))
            due = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return result('STUCK', 'open question has no resolvable deadline; obtain a ruling',
                          owner, question, deadline=deadline)
        if due <= now:
            return result('STUCK', 'human deadline passed: ' + str(question.get('asks') or question.get('why')),
                          owner, question, deadline=deadline)
        return result('waiting-on-human', question.get('asks') or question.get('why') or 'open question',
                      owner, question, deadline=deadline)

    if context['active'].get(sid):
        start, role, deadline = context['active'][sid][-1]
        return result('queued', f'{role} owns active dispatch {start["spawn"]}', role,
                      start, deadline=deadline.isoformat())

    last_reset = max((i for i, e in enumerate(evs) if e['type'] in RESETS or (
        e['type'] == 'decision' and e.get('actor') in ('thinker', 'operator', 'executor')
        and (e.get('rework') == 'yes' or e.get('regrade') == 'yes' or e.get('close')))), default=-1)
    reviews = [(i, e) for i, e in enumerate(evs) if e['type'] == 'review']
    clean = [(i, e) for i, e in enumerate(evs)
             if e['type'] == 'merge-gate-clean'
             or (e['type'] == 'review' and type(e.get('n_blocking')) is int and e['n_blocking'] == 0)]
    blocking_review = bool(reviews and reviews[-1][1].get('n_blocking') != 0)
    if clean and clean[-1][0] > last_reset and not blocking_review and not fold.standing_rejects(evs):
        ready = clean[-1][1]
        windows = context['windows'].get(sid, [])
        reason = ('in merge window ' + ', '.join(sorted(windows)) if windows
                  else 'awaiting merge window after ' + ready['type'])
        return result('queued', reason, event=ready)

    if spec['state'] == 'written':
        import dispatch
        charter = dispatch.resolve_charter(context['events'], sid, None)
        written = latest(evs, {'spec-written'})
        wave = dispatch.spec_wave(charter, written.get('footprint')) if charter and written else None
        blocker = dispatch.wave_blocker(context['events'], charter, sid, wave)
        if blocker:
            return result('waiting-on-prereq', 'earlier wave has not shipped: ' + blocker,
                          event=written, blocker=blocker)
    blocked = latest(evs, {'blocked', 'unblocked', 'decision'})
    if blocked and blocked['type'] == 'blocked':
        blocker = blocked.get('waits_for') or blocked.get('blocker') or blocked.get('blocked_by')
        if blocker:
            import dispatch
            if blocker == sid or blocker not in specs or specs[blocker]['state'] == 'unknown':
                return result('STUCK', 'unresolvable prerequisite: ' + str(blocker), event=blocked)
            if specs[blocker]['state'] not in dispatch.SETTLED_STATES:
                return result('waiting-on-prereq', 'footprint/dependency blocker: ' + str(blocker),
                              blocked.get('owner') or 'executor', blocked, blocker=blocker)

    failure = latest(evs, {'evidence-bundle-failed', 'evidence-bundle', 'build-done', 'review'})
    if failure and failure['type'] == 'evidence-bundle-failed':
        return result('STUCK', 'evidence bundle failed and not rerun: ' + str(failure.get('reason')),
                      event=failure)
    conflict = latest(evs, {'conflict-rework', 'merge-conflict', 'build-done', 'merge-gate-clean', 'review'})
    if conflict and conflict['type'] in ('conflict-rework', 'merge-conflict'):
        return result('STUCK', 'conflict-rework has no active builder dispatch', event=conflict)
    if ruling and ruling['type'] == 'decision':
        later = evs[evs.index(ruling) + 1:]
        if not any(e['type'] in ('build-started', 'spawn-started', 'spec-written', 'spec-killed',
                                 'spec-closed', 'build-done', 'verdict', 'review') for e in later):
            return result('STUCK', 'decision not carried out: ' + str(ruling.get('why')),
                          'planner' if 're-cut' in str(ruling.get('why')).lower() else 'executor', ruling)
    if fold.standing_rejects(evs):
        return result('STUCK', 'standing rejected criteria remain: '
                      + ', '.join(sorted(fold.standing_rejects(evs)))
                      + '; reconcile verdict/review before advancing')
    if spec['state'] == 'written':
        return result('STUCK', 'spec written with no prerequisite blocker and no builder dispatched')
    if spec['state'] == 'building':
        return result('STUCK', 'build has no live dispatch within its recorded window/cap')
    verdict = latest(evs, {'build-done', 'verdict', 'review', 'build-blocked'})
    if verdict and verdict['type'] == 'build-blocked':
        return result('STUCK', 'build blocked with no active next action: ' + str(verdict.get('reason')),
                      event=verdict)
    role = 'reviewer' if verdict and verdict['type'] == 'verdict' else 'grader'
    return result('STUCK', f'{spec["state"]} with no active {role} dispatched')


def rows(specs, by_subject, now, dwell):
    candidates = [s for s in specs.values() if s['state'] in dwell and s['age'] > dwell[s['state']]]
    if not candidates:
        return []
    # Unavailable evidence is not proof of a real stall. Propagate to the
    # board/realm's explicit unavailable state instead of inventing WEDGEs.
    context = prepare(by_subject, now)
    out = []
    for spec in sorted(candidates, key=lambda s: (-s['age'], s['id'])):
        try:
            out.append(classify(spec, specs, by_subject, context, now, dwell))
        except Exception as exc:
            raise ValueError(f'classification unavailable for {spec["id"]}: {exc}') from exc
    return out
