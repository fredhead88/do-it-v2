#!/usr/bin/env python3
"""One next step per live item. SD1/SD17, plan-L-charter-0061 (no ADR filed).

The caller supplies folded specs, parsed rules, time, and optional spec->wave
context. Only main reads files or the clock. Retires: nothing; reader migration
belongs to the following wave. ACTIONS is the most recently supplied rule table's
owner registry; decisions themselves always use a call-local owner mapping.
"""
import ledger_files
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sys
import unicodedata

import autodispatch
import attempts
import dispatch
import fold
import lane_rules
import merge_queue
import owed
import routing


STAGES = ('idea', 'planned', 'building', 'checking', 'merging', 'live')
OWNERS = {'tick', 'executor', 'thinker', 'operator', 'relay', 'sweeper'}
KINDS = {'charter', 'spec', 'attempt', 'window', 'check'}
ACTION_STAGES = dict(build='building', rebuild='building', grade='checking',
                     regrade='checking', review='checking', gate='merging',
                     enqueue='merging', merge='merging', close='planned')
EVENT_STAGES = {'spec-written': 'planned', 'build-started': 'building',
                'build-done': 'checking', 'verdict': 'checking',
                'review': 'checking', 'queue-add': 'merging'}
PIPELINE = {'written', 'building', 'graded', 'reviewing'}
OWED_STATES = {'shipped-owed-evidence', 'shipped-owed-due', 'shipped-owed-expired'}
FAILURES = {'merge-gate-undetermined', 'merge-gate-rework', 'spawn-failed', 'autodispatch-failed'}
NOT_READY_PREFIXES = ('wave-order', 'bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress')
RETRIES = {'build-started', 'spawn-started', 'queue-add', 'decision', 'spec-written'}
AUDIT_GRACE_MIN = 30
ACTIONS: dict[str, str] = {}


@dataclass(frozen=True)
class Step:
    item: str
    item_kind: str
    stage: str
    state: str
    action: str | None
    owner: str
    trigger: str | None
    reason: str
    clears: str | None
    since: str

    def __post_init__(self):
        if (self.item_kind not in KINDS or self.stage not in STAGES
                or self.state not in {'action', 'hold'} or self.owner not in OWNERS):
            raise ValueError('invalid Step value')
        if self.state == 'action' and self.action is None:
            raise ValueError('action Step needs an action')
        if self.state == 'hold' and (not self.reason or self.clears is None):
            raise ValueError('hold Step needs reason and clears')


# Shared with grading_env.infra_class so retry labels and hold reasoning agree.
# Permission refusals count only when the same physical line names the schema.
SCHEMA_REJECTION_RE = re.compile(
    r'\binvalid_json_schema\b|Invalid schema for response_format|'
    r'\b(?:schema|response_format)\b[^\r\n]*not permitted|'
    r'not permitted[^\r\n]*\b(?:schema|response_format)\b', re.I)
PACKET_INCOMPLETE_RE = re.compile(
    r'^packet: incomplete criterion [A-Za-z][A-Za-z0-9_.-]* from [^\s\r\n][^\r\n]*$', re.M)


BASE_FAILURES = ('stale-base', 'branch-not-descendant', 'pinned-base-mismatch')
BASE_FAILURE_RE = re.compile(
    r'(?:^(?:' + '|'.join(BASE_FAILURES) + r')$|'
    r'(?:\breason=|\bproblem:)(?:' + '|'.join(BASE_FAILURES) + r')(?=$|\s|[,;]))', re.M)


def classify_failure(text, exit_code, rules=None):
    """Conservative SD3 classifier; mixed evidence is always unknown."""
    if exit_code is None:
        return 'unknown'
    text = text or ''
    if not text.strip():
        return 'infra' if exit_code == 124 else 'unknown'
    infra = bool(re.search(
        r'No space left on device|\[Errno 28\]|Out of diskspace|\bENOSPC\b|'
        r'fatal: could not create work tree dir|'
        r'ledger cache: lock not acquired within|'
        r'git worktree add[^\n]*(?:fail|error)|(?:fail|error)[^\n]*git worktree add|'
        r'Connection reset by peer|Connection refused|at capacity|overloaded|\bHTTP\s+429\b',
        text, re.I) or SCHEMA_REJECTION_RE.search(text))
    work = bool(re.search(r'\bFAILED\b|\bAssertionError\b', text))
    defect = bool(PACKET_INCOMPLETE_RE.search(text))
    base = bool(BASE_FAILURE_RE.search(text))
    classes = [name for name, present in (('infra', infra), ('work', work), ('spec-defect', defect), ('base-refresh', base))
               if present]
    return classes[0] if len(classes) == 1 else 'unknown'


def _time(value):
    return lane_rules._time(value)


def _owners(rules):
    if not isinstance(rules, dict) or set(rules) != lane_rules.NAMES:
        raise ValueError('malformed rules: predicate names differ')
    for name, row in rules.items():
        if (not isinstance(row, dict) or set(row) != {'name', 'action', 'actor', 'limit_min'}
                or row.get('name') != name
                or not isinstance(row.get('action'), str) or not row['action']
                or row.get('actor') not in OWNERS | {'none'}
                or type(row.get('limit_min')) is not int or row['limit_min'] <= 0
                or name in ACTION_STAGES and row['actor'] == 'none'
                or name == 'sweep-check' and row['actor'] != 'tick'
                or name not in ACTION_STAGES and name != 'sweep-check' and row['actor'] != 'none'):
            raise ValueError('malformed rule: ' + str(name))
    return {**{n: rules[n]['actor'] for n in ACTION_STAGES}, 'sweep-check': 'sweeper'}


TERMINAL_EVENTS = lane_rules.TERMINAL_EVENTS
JUDGING = lane_rules.JUDGING
DISPATCH_EVENTS = lane_rules.DISPATCH_EVENTS


def _latest(events, kinds):
    return next((e for e in reversed(events) if e.get('type') in kinds), None)



def _after(events, trigger, kinds, role=None):
    return any(e.get('type') in kinds and _time(e['ts']) >= _time(trigger['ts'])
               and (role is None or e.get('role') == role) for e in events)



def _terminal(evs, spec=None):
    return (any(e.get('type') in TERMINAL_EVENTS for e in evs)
            or (spec or {}).get('state') in dispatch.SETTLED_STATES)



def _result(trigger, reason='condition met', **fields):
    _time(trigger['ts'])
    return dict(since=trigger['ts'], reason=reason, **fields)



def _builder_dispatch(e):
    return (e.get('type') in DISPATCH_EVENTS and
            (e.get('role') or '-'.join(str(e.get('spawn') or '').split('-')[1:-1])
             or 'builder') == 'builder')



def _builder_question(evs):
    """The latest builder attempt must still end at this question."""
    activity = [e for e in evs if e.get('type') in {'build-started', 'build-blocked', 'build-done'}
                or e.get('type') == 'question' and e.get('actor') == 'builder'
                or _builder_dispatch(e)]
    question = _latest(activity, {'question', 'build-blocked', 'build-done'})
    if not question or question['type'] != 'question' or not question.get('_src'):
        return None
    attempt = _latest(activity, DISPATCH_EVENTS | {'build-started', 'build-blocked'})
    if attempt and (_time(attempt['ts']) >= _time(question['ts'])
                    or attempt.get('spawn') and attempt['spawn'] != question.get('spawn')):
        return None
    return question



def _vanished(e):
    """Retry authority comes from the ledger filename, never from prose or JSON actor."""
    source = _source(e)
    return (e.get('type') == 'spawn-failed' and e.get('reason') == 'vanished'
            and bool(source) and fold.actor_of(source.split(':')[0]) == 'tick')


def _attempt_role(e):
    return e.get('role') or fold.actor_of(e.get('spawn') or '')


def _refusal(e):
    return (e.get('type') == 'spawn-failed'
            and (str(e.get('reason') or '').startswith(NOT_READY_PREFIXES)
                 or str(e.get('reason') or '') in BASE_FAILURES))


def _history_events(evs):
    return [e for e in evs if not _refusal(e)
            or str(e.get('reason') or '').startswith('bundle-pending')]


def _retry_result(c, trigger, role, *, dispatched=False):
    """Use the original trigger's history; a vanished end never resets its budget."""
    evs = c['evs']
    vanished = any(_vanished(e) and _attempt_role(e) == role
                   and _time(e['ts']) >= _time(trigger['ts']) for e in evs)
    if dispatched and not vanished:
        return None
    # SD1's transport refusals are not attempts. Bundle readiness retains its
    # existing history treatment; other refusals simply re-evaluate next tick.
    h = attempts.history(_history_events(evs), dict(trigger, subject=c['sid']), role, now=c['now'])
    if h.consumed or h.live:
        return None
    fields = {}
    reason = 'condition met'
    if h.exhausted:
        reason = f'retry exhausted: {h.n_failed} failed attempts; ' + '; '.join(h.reasons)
        fields = dict(retry_hold=True, retry_owner='executor',
                      clears='new authorized trigger or operator decision')
    elif h.not_ready:
        reason = h.not_ready
        fields = dict(retry_hold=True, retry_owner='tick', clears='evidence-bundle ready')
    elif h.next_retry_at and c['now'] < h.next_retry_at:
        reason = f'retry backoff: {h.n_failed} failed attempts; next_retry_at={h.next_retry_at.isoformat()}'
        fields = dict(retry_hold=True, retry_owner='tick', clears='next_retry_at=' + h.next_retry_at.isoformat())
    elif h.n_failed:
        reason = f'retry due: {h.n_failed} failed attempts; ' + '; '.join(h.reasons)
    return _result(trigger, reason, **fields)


def _rebuild(c):
    evs = c['evs']
    # Settled fold states include shipped (and all shipped-owed-* states),
    # accepted, closed-shipped, closed-unbuilt, dropped, killed and void.
    if _terminal(evs, c['spec']) or c['dependency']:
        return None
    blocked = _latest(evs, {'build-blocked'})
    outcome = _latest(evs, {'build-done', 'build-blocked'})
    # thinker-answer refs the builder's question, not the build-blocked row.
    # A spawn ties that question to this blocked build; legacy rows without a
    # spawn must at least belong to the current block's time interval.
    questions = [e for e in evs if blocked and e.get('type') == 'question'
                 and e.get('actor') == 'builder' and e.get('_src')
                 and (e.get('spawn') == blocked['spawn'] if blocked.get('spawn')
                      else _time(e['ts']) >= _time(blocked['ts']))]
    question = _builder_question(evs)
    if question and question not in questions:
        questions.append(question)
    decisions = [e for e in evs if e.get('type') == 'decision' and e.get('actor') in ('thinker', 'operator')
                 and not e.get('close') and e.get('regrade') != 'yes' and not e.get('wave_waive')
                 # Explicit rework belongs to the latest completed builder attempt,
                 # even when its ref also answers a historical blocked build.
                 and (e.get('rework') != 'yes' or not outcome or _time(e['ts']) > _time(outcome['ts']))
                 and (blocked and _time(e['ts']) > _time(blocked['ts']) and e.get('ref')
                      and e['ref'] == blocked.get('_src')
                      or any(e.get('ref') == q['_src'] and _time(e['ts']) > _time(q['ts'])
                             for q in questions)
                      or e.get('rework') == 'yes')]
    rewrites = [e for e in evs if blocked and e.get('type') == 'spec-written'
                and _time(e['ts']) > _time(blocked['ts'])]
    drops = [e for e in evs if (e.get('type') == 'window-dropped' and e.get('reason') == 'owner')
             or e.get('type') in {'merge-conflict', 'precheck-red'}]
    review = _latest(evs, JUDGING | {'build-done', 'build-blocked'})
    blocking = ([review] if review and review['type'] == 'review'
                and type(review.get('n_blocking')) is int and review['n_blocking'] > 0 else [])
    trigger = max(decisions + rewrites + drops + blocking, key=lambda e: _time(e['ts']), default=None)
    dispatched = [e for e in evs if _builder_dispatch(e) and not _refusal(e)]
    if trigger and (not blocked or _time(trigger['ts']) > _time(blocked['ts'])):
        result = _retry_result(c, trigger, 'builder', dispatched=(
            _after(dispatched, trigger, DISPATCH_EVENTS) or _after(evs, trigger, {'build-started'})))
        return dict(result, trigger=_source(trigger)) if result else None



def _close(c):
    trigger = next((e for e in reversed(c['evs']) if e.get('type') == 'decision'
                    and e.get('actor') in ('thinker', 'operator') and e.get('close')), None)
    return _result(trigger) if trigger else None



def _regrade(c):
    if c['dependency']:
        return None
    triggers = []
    for evs in c['capabilities'].get(c['sid'], []):
        hold = _latest(evs, {'capability-hold'})
        unblocked = _latest(evs, {'unblocked'})
        if (hold and unblocked and unblocked.get('actor') in ('thinker', 'operator')
                and unblocked.get('regrade') != 'no'
                and _time(unblocked['ts']) > _time(hold['ts'])
                and not routing.hold_ruled(c['evs'], hold, c['sid'])):
            triggers.append(unblocked)
    decision, kind = autodispatch._grader_trigger(c['evs'])
    if decision and kind == 'regrade':
        triggers.append(decision)
    trigger = max(triggers, key=lambda e: _time(e['ts']), default=None)
    if trigger:
        dispatched = [e for e in c['evs'] if not _refusal(e) and _attempt_role(e) == 'grader']
        return _retry_result(c, trigger, 'grader', dispatched=_after(dispatched, trigger, DISPATCH_EVENTS))



def audit_state(evs, now):
    """Evaluate SD3's one audit and SD1's bounded attempts on a subject slice."""
    state = dict(phase='undetermined', since=None, due_at=None, retry_at=None, reason='')
    try:
        evs = list(evs)
        trigger = _latest(evs, {'spec-written'})
        if trigger:
            state['since'] = trigger['ts']
            due_at = _time(trigger['ts']) + timedelta(minutes=AUDIT_GRACE_MIN)
            state['due_at'] = due_at.isoformat()
        reason = dispatch.unaudited(evs)
        if reason is None:
            first = next((e for e in evs if e.get('type') == 'spec-written'), {})
            exempt = (not first or first.get('actor') != 'spec-writer'
                      or any(e.get('type') in {'spec-carried', 'build-started'} for e in evs))
            state.update(phase='exempt' if exempt else 'done', reason='audit exempt' if exempt else 'audit complete')
            return state
        state['reason'] = reason
        if reason.startswith('unaudited: undetermined'):
            return state
        if 'audit reported bad_cut' in reason:
            state['phase'] = 'bad-cut'
            return state
        if now < due_at:
            state.update(phase='wait', reason='30 minute audit grace')
            return state
        history = attempts.history(_history_events(evs), trigger, 'spec-auditor', now=now,
            dead=frozenset(e['spawn'] for e in autodispatch.dead_spawns(evs, now)))
        if history.live:
            state.update(phase='live', reason='spec-auditor in flight')
        elif history.not_ready:
            state.update(phase='not-ready', reason=history.not_ready)
        elif history.exhausted:
            state.update(phase='exhausted', reason=f'{history.n_failed} failed attempts; ' + '; '.join(history.reasons))
        elif history.consumed:
            # A consumed trigger without a C1 audit cannot authorize a build.
            state.update(phase='not-ready', reason='audit trigger already consumed')
        elif history.next_retry_at and now < history.next_retry_at:
            state.update(phase='retry-wait', retry_at=history.next_retry_at.isoformat(),
                         reason=f'{history.n_failed} failed attempts; ' + '; '.join(history.reasons))
        else:
            state.update(phase='due', reason='one blind spec audit due', attempt=history.n_failed + 1)
        return state
    except Exception as exc:
        state.update(phase='undetermined', reason='undetermined: ' + str(exc))
        return state


def audit_due(evs, now):
    state = audit_state(evs, now)
    if state['phase'] == 'due':
        return {key: state[key] for key in ('since', 'reason', 'attempt', 'retry_at')}
    return None


def _build(c):
    if dispatch.unaudited(c['evs']) is not None:
        return None
    state = c['spec'].get('state')
    vanished = any(_vanished(e) and _attempt_role(e) == 'builder' for e in c['evs'])
    if (state != 'written' and not (state == 'building' and vanished)
            or c['sid'] in c['busy'] or c['dependency'] or c['wave']):
        return None
    if _rebuild(c) or _close(c) or _regrade(c):
        return None
    # Hand-authored later pipeline events are still authoritative if a fold
    # rejects their envelope; never dispatch another first build over them.
    if any(e.get('type') in JUDGING | {'build-done', 'queue-add', 'merge-gate-clean', 'build-blocked'} for e in c['evs']):
        return None
    trigger = _latest(c['evs'], {'spec-written'})
    return _retry_result(c, trigger, 'builder') if trigger else None



def _grade(c):
    if c['dependency']:
        return None
    trigger, kind = autodispatch._grader_trigger(c['evs'])
    if not trigger or kind == 'regrade' or _rebuild(c) or _regrade(c) or _close(c):
        return None
    if _after(c['evs'], trigger, JUDGING | {'queue-add', 'merge-gate-clean'}):
        return None
    dispatched = [e for e in c['evs'] if not _refusal(e) and _attempt_role(e) == 'grader']
    return _retry_result(c, trigger, 'grader', dispatched=_after(dispatched, trigger, DISPATCH_EVENTS))



def _review(c):
    if c['dependency']:
        return None
    trigger = _latest(c['evs'], JUDGING | {'build-done', 'build-blocked'})
    if not trigger or not (trigger['type'] == 'cannot-assess' or
                           trigger['type'] == 'verdict' and trigger.get('confirmed') is True):
        return None
    if fold.standing_rejects(c['evs']) or _rebuild(c) or _close(c) or _regrade(c):
        return None
    if _after(c['evs'], trigger, {'queue-add', 'merge-gate-clean'}):
        return None
    dispatched = [e for e in c['evs'] if not _refusal(e) and _attempt_role(e) == 'reviewer']
    return _retry_result(c, trigger, 'reviewer', dispatched=_after(dispatched, trigger, DISPATCH_EVENTS))



def _clean_review(c):
    trigger = _latest(c['evs'], JUDGING | {'build-done', 'build-blocked'})
    if (trigger and trigger['type'] == 'review'
            and type(trigger.get('n_blocking')) is int and trigger['n_blocking'] == 0
            and not fold.standing_rejects(c['evs'])):
        return trigger



def _gate_attempts(evs, event):
    """Count persisted acquired failures once, within one review/SHA/repo."""
    identity = tuple(event.get(k) for k in ('subject', 'repo', 'branch_sha', 'cycle'))
    unblocked = _latest([e for e in evs if e.get('type') == 'unblocked'
                         and _admitted_actor(e) in {'operator', 'thinker'}], {'unblocked'})
    reset = _time(unblocked['ts']) if unblocked else None
    acquired = {}
    for e in evs:
        if (e.get('type') == 'merge-gate-undetermined'
                and tuple(e.get(k) for k in ('subject', 'repo', 'branch_sha', 'cycle')) == identity
                and e.get('retryable') is True and e.get('attempt')
                and (reset is None or _time(e['ts']) > reset)):
            acquired.setdefault(e['attempt'], e)
    return len(acquired), reset


def _gate_tooling(c):
    evs = c['evs']
    event = _latest(evs, {'merge-gate-undetermined', 'merge-gate-clean', 'merge-gate-rework',
                          'conflict-rework', 'shipped', 'spec-killed', 'spec-closed'} | JUDGING)
    if not event or event['type'] != 'merge-gate-undetermined':
        return None
    review = _clean_review(c)
    if review and _time(review['ts']) > _time(event['ts']):
        return None  # A new clean review starts its own cycle at the same SHA.
    if not review or event.get('cycle') != review['ts']:
        return dict(event=event, hold='review-required', due=None)
    count, reset = _gate_attempts(evs, event)
    if reset is not None and reset > _time(event['ts']):
        return dict(event=event, hold=None, due=None)
    # A refusal cannot clear an outstanding acquired failure's hold or backoff.
    policy = event
    if event.get('reason') == 'lock-busy':
        identity = tuple(event.get(k) for k in ('repo', 'branch_sha', 'cycle'))
        policy = _latest([e for e in evs if e.get('type') == 'merge-gate-undetermined'
                          and e.get('attempt') and tuple(e.get(k) for k in
                          ('repo', 'branch_sha', 'cycle')) == identity
                          and (reset is None or _time(e['ts']) > reset)], {'merge-gate-undetermined'}) or event
    hold = policy.get('hold_reason')
    if policy.get('retryable') is not True:
        hold = hold or policy.get('reason') or 'gate-tooling'
    if count >= 3:
        hold = 'retry-cap-exhausted'
    due = policy.get('retry_at')
    return dict(event=event, hold=hold, due=due)


def _gate(c):
    tooling = _gate_tooling(c)
    if tooling:
        if (tooling['hold'] or c['sid'] in c['live'] or _close(c) or _rebuild(c) or _regrade(c)
                or tooling['due'] and c['now'] < _time(tooling['due'])):
            return None
        return _result(tooling['event'], 'gate tooling retry due', retry_at=tooling['due'])
    trigger = _clean_review(c)
    if not trigger or _close(c) or _rebuild(c) or _regrade(c):
        return None
    if any(str(e.get('type', '')).startswith('merge-gate-')
           and _time(e['ts']) >= _time(trigger['ts']) for e in c['evs']):
        return None
    if _after(c['evs'], trigger, {'queue-add'}):
        return None
    return _result(trigger)



def _enqueue(c):
    trigger = _latest(c['evs'], {'merge-gate-clean', 'merge-gate-undetermined', 'merge-gate-rework', 'conflict-rework'})
    if not trigger or trigger['type'] != 'merge-gate-clean':
        return None
    judgment = _latest(c['evs'], {'verdict', 'cannot-assess'})
    if judgment and _time(trigger['ts']) < _time(judgment['ts']):
        return None
    if _after(c['evs'], trigger, {'queue-add', 'shipped', 'window-dropped', 'merge-conflict', 'precheck-red'}):
        return None
    if _close(c) or _rebuild(c) or _regrade(c):
        return None
    review = _clean_review(c)
    if review is None:
        raise ValueError('clean gate has no clean review timestamp')
    if _time(trigger['ts']) < _time(review['ts']):
        return None
    return _result(review)



def _merge(c):
    trigger = _latest(c['evs'], {'queue-add'})
    if not trigger or _after(c['evs'], trigger, {'queue-remove', 'shipped', 'window-dropped', 'merge-conflict', 'precheck-red'}):
        return None
    from merge_queue import queue
    live = next((r for r in queue(c['events']) if r.get('spec') == c['sid']), None)
    if live is not None:
        if live.get('project') != merge_queue.window_gate.PROJECT:
            return _result(dict(ts=live['added_at']), 'undetermined: project')
        return _result(dict(ts=live['added_at']))



def _held_result(c, blocker, kind):
    evs = c['by_subject'].get(blocker, [])
    terminal = _terminal(evs, c['specs'].get(blocker))
    if kind == 'dependency-held' and (not evs or terminal or c['specs'].get(blocker, {}).get('state', 'unknown') == 'unknown'):
        hold = _dependency_hold(c['evs'])
        return _result(hold, 'blocker-dead: ' + str(blocker), blocker=blocker, overdue=True)
    owned = next((r for r in c['acting_rows'] if r['spec'] == blocker
                  and not r['reason'].startswith('undetermined: ')), None)
    if owned:
        return dict(since=owned['since'], limit_min=owned['limit_min'], blocker=blocker,
                    reason='blocker-open: ' + owned['rule'], reason_live=not owned['overdue'])
    trigger = _latest(evs, TERMINAL_EVENTS) if terminal else (evs[-1] if evs else None)
    trigger = trigger or _latest(c['evs'], {'dependency-hold', 'spec-written'})
    return _result(trigger, 'blocker-terminal: ' + str(blocker) if terminal else 'blocker-open: no-row',
                   blocker=blocker, reason_live=not terminal and blocker in c['live'])



def _wave_held(c):
    return _held_result(c, c['wave'], 'wave-held') if c['wave'] else None



def _dependency_hold(evs):
    return max((e for e in evs if e.get('type') == 'dependency-hold'),
               key=autodispatch._hold_order, default=None)


def _dependency_held(c):
    if not c['dependency']:
        return None
    hold = _dependency_hold(c['evs'])
    if not fold.valid_dependency(hold.get('waits_for')):
        return _result(hold, c['dependency'], invalid_dependency=True)
    return _held_result(c, hold.get('waits_for'), 'dependency-held')



def _idle(c):
    if (not re.fullmatch(r'L-spec-\d{4}', c['sid']) or c['spec'].get('state') == 'unknown'
            or not any(e.get('type') == 'spec-written' for e in c['evs'])
            or c['wave'] or c['dependency'] or c['sid'] in c['busy']
            or any(r['spec'] == c['sid'] for r in c['acting_rows'])):
        return None
    # A standing dispatch failure and capability hold have another owner.
    fail = _latest(c['evs'], {'autodispatch-failed'})
    marker = _latest(c['evs'], {'spec-written', 'decision', 'unblocked', 'dependency-hold'})
    if fail and (not marker or _time(fail['ts']) > _time(marker['ts'])):
        return None
    for evs in c['capabilities'].get(c['sid'], []):
        last = _latest(evs, {'capability-hold', 'unblocked'})
        if last and last['type'] == 'capability-hold':
            return None
    # This is a ledger-wide query, including cross-subject refs. Share it for
    # this one due() call rather than rescanning the ledger for every idle spec.
    if 'answered' not in c['memo']:
        c['memo']['answered'] = fold.answered(c['events'])
    answered = c['memo']['answered']
    if any(e.get('type') == 'question' and e.get('_src') not in answered for e in c['evs']):
        return None
    trigger = c['evs'][-1] if c['evs'] else None
    if trigger and (c['now'] - _time(trigger['ts'])).total_seconds() >= c['table']['idle']['limit_min'] * 60:
        return _result(trigger)



PREDICATES = {'build': _build, 'grade': _grade, 'review': _review, 'gate': _gate,
              'enqueue': _enqueue, 'merge': _merge, 'idle': _idle, 'rebuild': _rebuild,
              'regrade': _regrade, 'close': _close, 'wave-held': _wave_held,
              'dependency-held': _dependency_held}


def _source(e):
    return e.get('_src') or e.get('src')


def _admitted_actor(e):
    """Rulings and their evidence use ledger provenance, never payload identity."""
    source = _source(e)
    actor = fold.actor_of(source.rsplit(':', 1)[0]) if source else e.get('actor')
    return actor if fold.admits(e, actor) is None else None


def _accepted_start(e, spawn_events):
    if (e.get('type') not in {'build-started', 'spawn-started', 'autodispatched'}
            or (e.get('role') or fold.actor_of(str(e.get('spawn') or ''))
                or 'builder') != 'builder'
            or _admitted_actor(e) is None or not e.get('spawn')):
        return False
    history = spawn_events.get(e['spawn'], [])
    positive = any((x['type'] == 'seat-claimed' and _admitted_actor(x) == 'relay'
                    or x['type'] == 'spawn-done' and _admitted_actor(x) == 'builder'
                    and x.get('role', 'builder') == 'builder') for x in history)
    later = history[history.index(e) + 1:] if e in history else history
    return positive or not any(x['type'] == 'spawn-failed'
                              and _admitted_actor(x) is not None for x in later)


def _ruling_step(c, spawn_events, owners):
    """SD10's executor obligation; completion is read from this snapshot only."""
    sid, evs = c['sid'], c['evs']
    if not re.fullmatch(r'L-spec-\d{4}', sid) or _terminal(evs, c['spec']):
        return None
    ruling = next((e for e in reversed(evs) if e['type'] == 'decision'
                   and e.get('action') in ('close', 'rework', 'hold', 'recut')
                   and _admitted_actor(e) in {'thinker', 'operator'}), None)
    if ruling is None:
        return None
    action, source = ruling['action'], _source(ruling)
    if action in ('hold', 'recut') and (autodispatch._hold_released(ruling, evs)
            or any(e['type'] == 'build-started' and _time(e['ts']) >= _time(ruling['ts']) for e in evs)):
        return None
    if not source or action == 'hold' and not fold.valid_dependency(ruling.get('waits_for')):
        return _hold(sid, 'spec', _stage(evs), ruling,
                     'undetermined: ruling needs ' + ('source' if not source else 'waits_for'),
                     'valid action-bearing decision')
    for e in evs[evs.index(ruling) + 1:]:
        if ('ref' in e and e['ref'] != source) or _admitted_actor(e) is None:
            continue
        if (action in {'close', 'recut'} and e['type'] == 'spec-killed'
                or action == 'hold' and e['type'] == 'dependency-hold'
                and e.get('waits_for') == ruling['waits_for']
                or action == 'rework' and _accepted_start(e, spawn_events)):
            return None
    if (c['now'] - _time(ruling['ts'])).total_seconds() < 1800:
        return None
    # Preserve only a launch that is eligible now and executes this exact ruling.
    # An action field alone never supplies a legacy rebuild trigger.
    if action == 'rework' and sid not in c['busy'] and not c['wave']:
        rebuild = _rebuild(c)
        failure, classification = _failure(evs, c['table'])
        if (rebuild and rebuild.get('trigger') == source and owners['rebuild'] == 'tick'
                and dispatch.unaudited(evs) is None and not _unanswered(evs, c['events'])
                and not (failure and classification != 'work')):
            return Step(sid, 'spec', 'building', 'action', 'rebuild', 'tick', source,
                        rebuild['reason'], None, rebuild['since'])
    clears = {'close': 'spec-killed', 'rework': 'rework dispatch',
              'hold': 'dependency-hold waits_for=' + str(ruling.get('waits_for')),
              'recut': 'recut resolution recorded as spec-killed'}[action]
    return _hold(sid, 'spec', _stage(evs), ruling,
                 f'ruling-unexecuted: {action} ({source})', clears)


def _snapshot(events, specs):
    """Validate by subject before passing anything to clock-fallback helpers."""
    events = list(events)
    present = {e.get('subject') for e in events}
    for sid, spec in specs.items():
        if sid not in present:
            events.extend(spec.get('evs', []))
    valid, bad = [], {}
    for original in events:
        e = dict(original)
        e['_src'] = _source(e)
        if 'actor' not in e and e['_src']:
            e['actor'] = fold.actor_of(e['_src'].split(':')[0])
        try:
            _time(e.get('ts'))
        except (ValueError, TypeError) as exc:
            bad[e.get('subject')] = str(exc)
            if e.get('spawn'):
                bad[e['spawn']] = str(exc)
        else:
            valid.append(e)
    # Restore append order within a source before the fold's content tie-break.
    def key(e):
        src = str(e.get('_src') or '')
        file, _, line = src.rpartition(':')
        return (_time(e['ts']), file, int(line) if line.isdigit() else 0,
                fold.event_content_key(e))
    valid.sort(key=key)
    ordered = []
    for e in valid:
        if ordered and _time(ordered[-1][0]['ts']) == _time(e['ts']):
            ordered[-1].append(e)
        else:
            ordered.append([e])
    valid = [e for group in ordered for e in fold._merge_same_ts(group)]
    by = {}
    for e in valid:
        by.setdefault(e.get('subject'), []).append(e)
    return valid, by, bad


def _hold(item, kind, stage, event, reason, clears, owner='executor', since=None):
    return Step(item, kind, stage, 'hold', None, owner, _source(event or {}),
                reason, clears, since if since is not None else (event or {}).get('ts', ''))


def _stage(evs):
    return next((EVENT_STAGES[e['type']] for e in reversed(evs)
                 if e['type'] in EVENT_STAGES), 'planned')


def _unanswered(evs, events):
    return [e for e in evs if e['type'] in {'question', 'escalation-blocking'}
            and not any(a['type'] in {'decision', 'unblocked'}
                        and (_source(e) and a.get('ref') == _source(e)
                             or a.get('subject') == e.get('subject')
                             and _time(a['ts']) > _time(e['ts'])) for a in events)]


def _wave(sid, evs, by, specs, waves):
    if any(e['type'] in {'build-done', 'shipped'} for e in evs):
        return None
    waiver = next((e for e in reversed(evs) if e['type'] == 'decision' and e.get('wave_waive')), {})
    if waiver.get('wave_waive') == 'yes' or waves.get(sid) is None:
        return None
    charter = dispatch.resolve_charter(evs, sid, None)
    if not charter:
        return None
    for other in sorted(specs):
        if (other != sid and waves.get(other) is not None and waves[other] < waves[sid]
                and dispatch.resolve_charter(by.get(other, []), other, None) == charter
                and specs[other].get('state') not in dispatch.SETTLED_STATES
                and not any(e['type'] in lane_rules.TERMINAL_EVENTS for e in by.get(other, []))):
            return other
    return None


def _failure_text(event):
    return '\n'.join(str(event.get(k) or '') for k in ('findings', 'why', 'error', 'reason'))


def _gate_refusal(e):
    return (e.get('type') == 'spawn-failed' and e.get('role') == 'gate'
            and e.get('reason') in {'lock', 'duplicate', 'dispatch-in-progress'})


def _failure(evs, rules):
    latest = next((e for e in reversed(evs) if e['type'] in FAILURES | RETRIES
                   and not _gate_refusal(e)
                   and not (e['type'] == 'spawn-failed'
                            and str(e.get('reason') or '').startswith(NOT_READY_PREFIXES))
                   or e['type'] == 'build-blocked' and _base_blocked(e)), None)
    base = next((e for e in reversed(evs) if _base_blocked(e)
                 or e['type'] == 'spawn-failed' and e.get('reason') in BASE_FAILURES), None)
    if base and latest and latest['type'] in RETRIES and _time(latest['ts']) == _time(base['ts']):
        latest = base
    if latest and latest['type'] == 'merge-gate-undetermined':
        return latest, 'gate-tooling'
    if latest and (latest['type'] in FAILURES or _base_blocked(latest)):
        if _vanished(latest):
            return latest, 'retry'
        text = latest['reason'] if _base_blocked(latest) else _failure_text(latest)
        classification = classify_failure(text, latest.get('exit_code', 1), rules)
        if classification == 'base-refresh':
            # Actor files can tie at second resolution. Builder ids are allocated
            # monotonically; a newer start discharges an older refused dispatch.
            def order(e):
                spawn = re.fullmatch(r'L-builder-(\d+)', str(e.get('spawn') or ''))
                return (_time(e['ts']), int(spawn[1]) if spawn else -1)
            if any(e['type'] == 'build-started' and order(e) > order(latest) for e in evs):
                return None, None
        return latest, classification
    return None, None


def _base_blocked(event):
    reason = str(event.get('reason') or '')
    return (event.get('type') == 'build-blocked'
            and bool(BASE_FAILURE_RE.search(reason))
            and ('pinned-base-mismatch' == reason
                 or re.search(r'(?:\breason=|\bproblem:)pinned-base-mismatch(?=$|\s|[,;])', reason)))


def _base_refresh_hold(c, failure):
    evs = c['evs']
    trigger = _latest(evs, {'decision', 'spec-written', 'window-dropped', 'verdict', 'conflict-rework', 'merge-conflict', 'precheck-red'})
    history = attempts.history(_history_events(evs), trigger or failure, 'builder', now=c['now'])
    detail = str(failure.get('why') or failure.get('reason') or '')
    reason = ('base-refresh: regenerate the packet with the current effective base and redispatch; '
              'dispatch performs safe auto-rebase of a clean ordinary branch')
    if 'dirty work' in detail:
        reason += '; builder/operator preserves and cleans dirty work first'
    elif 'rebase conflict' in detail:
        reason += '; Executor uses existing subject-authorized conflict-rework to send the builder to resolve the conflict'
    if history.exhausted:
        reason += f'; retry exhausted: {history.n_failed} failed attempts; operator authorization required'
    return _hold(c['sid'], 'spec', _stage(evs), failure, reason,
                 'refreshed effective base/branch and regenerated packet; successful redispatch'
                 + (' after new authorized trigger or operator decision' if history.exhausted else ''))


def _choose(c, rows, owners):
    sid, evs = c['sid'], c['evs']
    event, stage = evs[-1] if evs else {}, _stage(evs)
    unknown = next((r for r in rows if str(r.get('reason', '')).startswith('undetermined')), None)
    if unknown:
        return _hold(sid, 'spec', stage, event, unknown['reason'], 'valid lane input')
    acting = [r for r in rows if c['table'][r['name']]['actor'] != 'none']
    if len({c['table'][r['name']]['action'] for r in acting}) > 1:
        return _hold(sid, 'spec', stage, event, 'undetermined: conflicting actions', 'one lane action')
    row = next(iter(acting), None)
    if row:
        if row.get('retry_hold'):
            trigger = next((e for e in reversed(evs) if e['ts'] == row['since']), {})
            return _hold(sid, 'spec', stage, trigger, row['reason'], row['clears'],
                         row['retry_owner'], row['since'])
        name = row['name']
        trigger = next((e for e in reversed(evs)
                        if (_source(e) == row['trigger'] if row.get('trigger')
                            else e['ts'] == row['since'])), {})
        return Step(sid, 'spec', ACTION_STAGES[name], 'action', name, owners[name],
                    _source(trigger), row.get('reason', 'condition met'), None, row['since'])
    for row in rows:
        if row['name'] in {'wave-held', 'dependency-held'}:
            label = 'dependency' if row['name'] == 'dependency-held' else 'wave'
            return _hold(sid, 'spec', stage, event,
                         label + ': ' + row.get('reason', 'held'),
                         str(row.get('blocker')) + ' shipped', 'tick', row['since'])
    return _hold(sid, 'spec', stage, event, 'idle: awaiting lane condition', 'lane condition or decision')


def _spec_exception(sid, evs, exc):
    # Exception messages may contain event payloads or credentials. Class and
    # subject identify the repair without copying that untrusted text.
    reason = f'undetermined: {sid} evaluation failed: {type(exc).__name__}'[:300]
    stage = next((EVENT_STAGES[e['type']] for e in reversed(evs)
                  if e.get('type') in EVENT_STAGES), 'planned')
    return _hold(sid, 'spec', stage, evs[-1] if evs else {}, reason,
                 'correct affected spec input and re-evaluate ' + sid)


def _invalid_dependency_step(sid, evs, reason):
    return _hold(sid, 'spec', _stage(evs), _dependency_hold(evs), reason,
                 'thinker/operator append a newer dependency-hold with '
                 'waits_for=L-spec-<digits> or explicitly release the hold')


def next_steps(events, specs, now, rules, waves=None):
    """Pure snapshot evaluation; waves is caller-built spec_id -> wave context."""
    raw_events = list(events)
    events, by, bad = _snapshot(events, specs)
    waves = waves or {}
    rule_error = None
    try:
        owners = _owners(rules)
        # The window launcher owns albert-scott merges. Other projects retain
        # their table owner and direct merge path; there is no global table edit.
        if any(row.get('project') == merge_queue.window_gate.PROJECT
               for row in merge_queue.queue(events)):
            owners['merge'] = 'tick'
        # Export effective owners, including the supported window launcher.
        ACTIONS.clear()
        ACTIONS.update(owners)
    except (ValueError, TypeError, KeyError) as exc:
        rule_error, owners = str(exc), {}
    steps, attempts, spawn_events = [], {}, {}
    for e in events:
        spawn = e.get('spawn') or (e.get('subject') if e['type'] == 'seat-claimed' else None)
        if not spawn:
            continue
        spawn_events.setdefault(spawn, []).append(e)
        if e['type'] in {'spawn-started', 'build-started'}:
            attempts.setdefault(spawn, e)
        elif e['type'] in {'spawn-done', 'spawn-failed', 'spawn-stale'}:
            attempts.pop(spawn, None)
    ended = {e.get('spawn') for e in events if e['type'] in {'spawn-done', 'spawn-failed', 'spawn-stale'}}
    # Equal-time cross-file ordering must not resurrect a terminal attempt.
    attempts = {spawn: e for spawn, e in attempts.items() if spawn not in ended}
    for e in raw_events:
        if (e.get('spawn') in bad and e.get('spawn') not in ended
                and e.get('type') in {'spawn-started', 'build-started'}):
            attempts.setdefault(e['spawn'], dict(e, ts='', _src=_source(e)))
    busy = {e.get('subject') for e in attempts.values()}
    for spawn, e in attempts.items():
        role = e.get('role') or fold.actor_of(spawn)
        stage = 'building' if role == 'builder' else 'checking' if role in {'grader', 'reviewer'} else 'planned'
        reason = 'undetermined: ' + bad[spawn] if spawn in bad else 'in flight'
        steps.append(_hold(spawn, 'attempt', stage, e, reason,
                           spawn + ' spawn-done/spawn-failed', 'executor' if spawn in bad else 'tick'))
    capabilities = {}
    for subject, evs in by.items():
        if str(subject).startswith('capability:'):
            capabilities.setdefault(subject.split(':', 2)[-1], []).append(evs)
    contexts, rows = [], []
    shipped = {e.get('subject') for e in events if e['type'] == 'shipped'}
    for sid, spec in sorted(specs.items()):
        evs = by.get(sid, [])
        first_step = len(steps)
        try:
            event, stage = evs[-1] if evs else {}, _stage(evs)
            terminals = {e['type'] for e in evs if e['type'] in lane_rules.TERMINAL_EVENTS}
            conflict = 'spec-killed' in terminals and bool(terminals - {'spec-killed'})
            error = bad.get(sid) or ('terminal events disagree' if conflict else None)
            state = spec.get('state')
            if not error and state in dispatch.SETTLED_STATES and state not in OWED_STATES:
                continue
            error = error or rule_error
            if state not in PIPELINE | set(dispatch.SETTLED_STATES):
                error = error or 'unknown spec state: ' + str(state)
            if not evs:
                error = error or 'since cannot be established'
            if error:
                steps.append(_hold(sid, 'spec', stage, event, 'undetermined: ' + error, 'valid input'))
                continue
            if state in OWED_STATES:
                for check in owed.checks(evs, now):
                    if check['status'] in {'met', 'waived', 'unverifiable'}:
                        continue
                    item = sid + ':' + check['criterion']
                    since = check['governing_at'].isoformat()
                    if check['status'] == 'expired':
                        step = _hold(item, 'check', 'live', event, 'owed-expired', 'decision', since=since)
                    elif check['due_at'] is not None and check['due_at'] <= now:
                        step = Step(item, 'check', 'live', 'action', 'sweep-check', owners['sweep-check'],
                                    check['declared_src'], 'owed check due', None, since)
                    else:
                        step = _hold(item, 'check', 'live', event, 'owed check waiting',
                                     str(check['due_at'] or 'dated owed check'), 'sweeper', since)
                    steps.append(step)
                continue
            c = dict(sid=sid, spec=spec, specs=specs, events=events, evs=evs,
                     by_subject=by, now=now, table=rules, busy=busy, live=busy,
                     capabilities=capabilities, memo={}, acting_rows=rows)
            c['dependency'] = autodispatch.held_by_dependency(evs, events, shipped=shipped)
            hold = _dependency_hold(evs)
            if c['dependency'] and hold is not None and not fold.valid_dependency(hold.get('waits_for')):
                steps.append(_invalid_dependency_step(sid, evs, c['dependency']))
                continue
            c['wave'] = _wave(sid, evs, by, specs, waves)
            ruling_step = _ruling_step(c, spawn_events, owners)
            ruling = autodispatch._standing_ruling(evs)
            if ruling is not None and c['dependency']:
                hold = _dependency_hold(evs)
                steps.append(_hold(sid, 'spec', stage, hold, c['dependency'],
                                   str(hold.get('waits_for')) + ' shipped', 'tick'))
                continue
            if ruling_step is not None:
                steps.append(ruling_step)
                continue
            if ruling is not None:
                steps.append(_hold(sid, 'spec', stage, ruling,
                                   autodispatch._automatic_hold(evs, events, shipped=shipped),
                                   'thinker/operator unblocked or decision referring to ruling', 'tick'))
                continue
            if state == 'written' and dispatch.unaudited(evs) is not None:
                questions = _unanswered(evs, events)
                if questions:
                    steps.append(_hold(sid, 'spec', stage, questions[-1],
                                       'judgment: open question/escalation', 'decision'))
                    continue
                if sid in busy:
                    spawn = next(s for s, e in attempts.items() if e.get('subject') == sid)
                    steps.append(_hold(sid, 'spec', stage, attempts[spawn], 'in flight: ' + spawn,
                                       spawn + ' spawn-done/spawn-failed', 'tick'))
                    continue
                audit = audit_state(evs, now)
                trigger = _latest(evs, {'spec-written'})
                phase = audit['phase']
                if phase == 'due':
                    steps.append(Step(sid, 'spec', 'planned', 'action', 'audit', 'tick',
                                      _source(trigger), audit['reason'], None, audit['since']))
                elif phase == 'bad-cut':
                    steps.append(_hold(sid, 'spec', 'planned', trigger,
                                       f'audit-bad-cut: {sid} audit reported bad_cut',
                                       'spec-written rewrite or operator decision', 'executor', audit['since']))
                else:
                    prefix = 'audit-exhausted: ' if phase == 'exhausted' else 'audit-wait: '
                    reason = prefix + phase + ': ' + audit['reason']
                    if audit['retry_at']:
                        reason += '; next_retry_at=' + audit['retry_at']
                    exhausted = phase == 'exhausted'
                    clears = ('new authorized trigger or operator decision' if exhausted
                              else 'next_retry_at=' + audit['retry_at'] if audit['retry_at']
                              else 'spec-auditor spawn-done')
                    steps.append(_hold(sid, 'spec', 'planned', trigger, reason, clears,
                                       'executor' if exhausted else 'tick', audit['since']))
                continue
            tooling = _gate_tooling(c)
            if tooling and (tooling['hold'] or tooling['due'] and now < _time(tooling['due'])):
                reason = tooling['hold'] or ('gate-tooling: waiting; retry_at=' + tooling['due'])
                steps.append(_hold(sid, 'spec', stage, tooling['event'], reason,
                                   'thinker/operator unblocked or repaired gate' if tooling['hold']
                                   else 'retry_at=' + tooling['due'],
                                   'executor' if tooling['hold'] else owners['gate']))
                continue
            failure, classification = _failure(evs, rules)
            if classification == 'base-refresh':
                steps.append(_base_refresh_hold(c, failure))
                continue
            if classification == 'spec-defect':
                diagnostic = PACKET_INCOMPLETE_RE.search(_failure_text(failure)).group(0)
                steps.append(_hold(sid, 'spec', stage, failure, 'spec-defect: ' + diagnostic,
                                   'spec amendment/rewrite and executor reconsideration'))
                continue
            # Structured reconciliation retries continue through original role/trigger
            # predicates, which own bounds, completion and stronger lifecycle holds.
            if failure and classification not in {'work', 'retry', 'gate-tooling'}:
                reason = 'infra: retry required' if classification == 'infra' else 'undetermined: failure classification'
                steps.append(_hold(sid, 'spec', stage, failure, reason, 'infra-retry backoff or freed disk'
                                   if classification == 'infra' else 'classified failure'))
                continue
            questions = _unanswered(evs, events)
            if questions:
                steps.append(_hold(sid, 'spec', stage, questions[-1], 'judgment: open question/escalation', 'decision'))
                continue
            if sid in busy:
                spawn = next(s for s, e in attempts.items() if e.get('subject') == sid)
                steps.append(_hold(sid, 'spec', stage, attempts[spawn], 'in flight: ' + spawn,
                                   spawn + ' spawn-done/spawn-failed', 'tick'))
                continue
            contexts.append(c)
        except Exception as exc:
            del steps[first_step:]
            steps.append(_spec_exception(sid, evs, exc))
    failed = set()
    for diagnostic in (False, True):
        for c in contexts:
            if c['sid'] in failed:
                continue
            try:
                for name, rule in rules.items():
                    if name == 'sweep-check':
                        continue
                    if (name in {'wave-held', 'dependency-held', 'idle'}) != diagnostic:
                        continue
                    result = PREDICATES[name](c)
                    if result is not None:
                        _time(result.get('since'))
                        rows.append(dict(result, name=name, spec=c['sid'], rule=name,
                                         limit_min=result.get('limit_min', rule['limit_min']),
                                         overdue=(now - _time(result['since'])).total_seconds() > rule['limit_min'] * 60))
            except Exception as exc:
                failed.add(c['sid'])
                rows[:] = [r for r in rows if r['spec'] != c['sid']]
                steps.append(_spec_exception(c['sid'], c['evs'], exc))
    order = {name: i for i, name in enumerate(rules)} if not rule_error else {}
    for c in contexts:
        if c['sid'] in failed:
            continue
        try:
            own = sorted((r for r in rows if r['spec'] == c['sid']), key=lambda r: order[r['name']])
            steps.append(_choose(c, own, owners))
        except Exception as exc:
            steps.append(_spec_exception(c['sid'], c['evs'], exc))
    windows = {r['batch'] for r in merge_queue.queue(events) if r.get('batch')}
    windows.update(s for s, evs in by.items() if any(e['type'] == 'window-opened' for e in evs))
    for window in sorted(windows):
        evs = by.get(window, [])
        if any(e['type'] in {'window-dropped', 'window-merged', 'window-done', 'window-verified', 'shipped'} for e in evs):
            continue
        event = evs[-1] if evs else {}
        questions = _unanswered(evs, events)
        if questions:
            event = questions[-1]
        age = int((now - _time(event['ts'])).total_seconds() // 60) if event else None
        error = bad.get(window) or (None if event else 'since cannot be established')
        steps.append(_hold(window, 'window', 'merging', event,
                           'undetermined: ' + error if error else 'window waiting; elapsed_min=' + str(age),
                           'window resolution', 'executor'))
    for charter, evs in by.items():
        if not str(charter).startswith('L-charter-') or any(e['type'] in
                {'plan-written', 'charter-closed', 'charter-retracted'} for e in evs):
            continue
        steps.append(_hold(charter, 'charter', 'idea', evs[-1],
                           'undetermined: ' + bad[charter] if charter in bad else 'plan not written', 'plan-written'))
    seen = {(s.item_kind, s.item) for s in steps}
    for e in raw_events:
        item = e.get('subject')
        if item not in bad:
            continue
        kind = 'charter' if str(item).startswith('L-charter-') else 'window' if e.get('type') == 'window-opened' else None
        if kind and (kind, item) not in seen:
            steps.append(_hold(item, kind, 'idea' if kind == 'charter' else 'merging',
                               dict(e, ts=''), 'undetermined: ' + bad[item], 'valid timestamp'))
            seen.add((kind, item))
    return sorted(steps, key=lambda s: (s.item_kind, s.item))


def step_for(events, specs, item, now, rules, waves=None):
    matches = [s for s in next_steps(events, specs, now, rules, waves) if s.item == item]
    if len(matches) != 1:
        raise KeyError(item)
    return matches[0]


def flow_counts(events, specs, now, rules, waves=None):
    counts = dict.fromkeys(STAGES, 0)
    for step in next_steps(events, specs, now, rules, waves):
        if step.stage != 'live':
            counts[step.stage] += 1
    valid, _, _ = _snapshot(events, specs)
    counts['live'] = len({e.get('subject') for e in valid if e['type'] == 'shipped'
                          and now - timedelta(days=7) <= _time(e['ts']) <= now})
    return counts


def _clean(text):
    return ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in str(text))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('item', nargs='?')
    args = parser.parse_args(argv)
    try:
        # Missing/unreadable directories cannot silently become an empty ledger.
        ledger_files.assert_readable(fold.EVENTS)
        events = fold.read_events(use_cache=False)
        rules = lane_rules.load()
        now = datetime.now(timezone.utc)
        specs = fold.fold(events)[0]
        waves = {}
        for sid, spec in specs.items():
            evs = spec.get('evs', [])
            written = next((e for e in reversed(evs) if e['type'] == 'spec-written' and e.get('footprint')), None)
            charter = dispatch.resolve_charter(evs, sid, None)
            if written and charter:
                waves[sid] = dispatch.spec_wave(charter, written['footprint'])
        steps = [step_for(events, specs, args.item, now, rules, waves)] if args.item else next_steps(events, specs, now, rules, waves)
        result = dict(counts=flow_counts(events, specs, now, rules, waves), steps=[asdict(s) for s in steps])
    except KeyError as exc:
        # A missing internal ledger/rule field is undetermined, not an unknown ID.
        if args.item and exc.args == (args.item,):
            print('next: unknown item ' + _clean(args.item), file=sys.stderr)
            return 1
        print('next: ' + _clean(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print('next: ' + _clean(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result))
    else:
        for step in steps:
            print(' | '.join(_clean(v) for v in (step.item, step.action or 'hold', step.owner, step.reason)))
    return 0




def manual_routine_dispatches(events, since):
    """Return routine spawn ids with missing, malformed, or manual provenance."""
    return [e.get('spawn') for e in events
            if (e.get('type') == 'build-started' or
                e.get('type') == 'spawn-started' and e.get('role') in {'grader', 'reviewer'})
            and _time(e.get('ts')) >= since and e.get('issued_by') != 'tick']


if __name__ == '__main__':
    sys.exit(main())
