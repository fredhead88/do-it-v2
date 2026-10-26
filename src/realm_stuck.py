"""Read-only stuck facts for the Realm's existing glance cycle."""
import datetime as dt
import json
from pathlib import Path
import re

from realm_glance import HELD_RULES, recorded_hold, utc
from realm_omens import CAP_MIN, _gone

WORDS = [
    ('no ledger evidence for the next action', 'The next action has no recorded evidence.'),
    ('unresolvable prerequisite:', 'A prerequisite cannot be found or resolved.'),
    ('human deadline passed:', 'The deadline for a human decision has passed.'),
    ('open question has no resolvable deadline', 'A question needs a decision and a deadline.'),
    ('evidence bundle failed and not rerun:', 'The evidence checks failed and need another run.'),
    ('conflict-rework has no active builder dispatch', 'A merge conflict needs a builder to repair it.'),
    ('decision not carried out:', 'A recorded decision still needs to be carried out.'),
    ('standing rejected criteria remain:', 'Rejected acceptance checks still need repair.'),
    ('spec written with no prerequisite blocker and no builder dispatched', 'Ready to build; no builder has been assigned.'),
    ('build has no live dispatch within its recorded window/cap', 'The build has no active assigned run.'),
    ('build blocked with no active next action:', 'The build needs a next action to resolve its blocker.'),
    ('capability-hold:', 'The Thinker needs to resolve a capability question.'),
]
JARGON = re.compile(r'\b(stalls|STUCK|bucket|dependency-held|wave-held|migration-sequenced|conflict-rework|waiting-on-prereq|capability-hold)\b', re.I)


def words(reason):
    text = ' '.join(str(reason or '').split())
    for stem, sentence in WORDS:
        if text.lower().startswith(stem):
            return sentence
    if re.match(r'\S+ with no active \S+ dispatched', text):
        return 'The next stage needs an assigned run.'
    return ('Action is waiting: ' + JARGON.sub('wait', text or 'the next action needs confirmation') + '.')[:140]


def who(owner):
    owner = str(owner or '').strip().lower()
    if owner in {'operator', 'you'}:
        return 'operator'
    if owner in {'thinker', 'planner'}:
        return owner
    return 'executor' if owner in {'executor', 'tick', '-', ''} else 'process'


def subject_spec(subject):
    subject = str(subject or '')
    if re.fullmatch(r'L-spec-\d+', subject):
        return subject
    if subject.startswith(('capability:', 'lane-stale:')):
        m = re.search(r'(L-spec-\d+)$', subject)
        return m[1] if m else None
    return None


def vanished_spawns(events, states, now):
    """Per-spawn counterpart of vanished_item, additionally excluding redispatch."""
    events = [e for e in events if utc(e.get('ts')) and utc(e['ts']) <= now]
    ended = {e.get('spawn') for e in events if e.get('type') in {'spawn-done','spawn-failed','spawn-stale'}}
    shipped, latest = {}, {}
    for e in events:
        if e.get('type') == 'shipped':
            shipped[e.get('subject')] = max(shipped.get(e.get('subject'), dt.datetime.min.replace(tzinfo=dt.timezone.utc)), utc(e['ts']))
        if e.get('type') in {'build-started','spawn-started'}:
            sid = e.get('subject')
            latest[sid] = max(latest.get(sid, dt.datetime.min.replace(tzinfo=dt.timezone.utc)), utc(e['ts']))
    states = dict(states)
    result = []
    for e in events:
        if e.get('type') not in {'build-started','spawn-started'} or not e.get('spawn') or e['spawn'] in ended:
            continue
        since, sid = utc(e['ts']), e.get('subject')
        role = e.get('role') or ('builder' if e['type'] == 'build-started' else '')
        age = (now - since).total_seconds() / 60
        if age > 1440 or age <= 2 * CAP_MIN.get(role, 30) or _gone(states.get(sid)):
            continue
        if shipped.get(sid, since) > since or latest.get(sid, since) > since:
            continue
        result.append(dict(spawn=e['spawn'],subject=sid or e['spawn'],role=role,since=e['ts']))
    return result


def owed_overdue(root, now):
    """Unreadable is distinct from empty. Old digest rows remain useful."""
    try:
        data = json.loads((Path(root) / 'state/owed-digest.json').read_text())
        if not isinstance(data, dict) or not utc(data.get('at')) or any(not isinstance(data.get(k), list) for k in ('needs_you','health')):
            return None
        result = []
        for key in ('needs_you','health'):
            for row in data[key]:
                if not isinstance(row, dict):
                    return None
                if key == 'needs_you' and row.get('decided') or key == 'health' and row.get('kind') != 'expired':
                    continue
                if not row.get('spec') or not utc(row.get('since')):
                    return None
                why = row.get('ask') or row.get('reason') or row.get('text') or 'Check deadline passed without an observation'
                owner = row.get('owner') if key == 'needs_you' else ('operator' if 'Operator' in why else 'executor')
                result.append(dict(spec=row['spec'],criterion=row.get('criterion'),since=row['since'],
                                   why=why,who=owner,src=row.get('declared_src') or 'state/owed-digest.json'))
        return result
    except (OSError, ValueError, TypeError):
        return None


def hold_evidence(row, rulings):
    if row.get('bucket') in {'held','waiting-on-prereq'}:
        return 'held-bucket', row.get('src')
    if row.get('rule') in HELD_RULES:
        return 'held-rule', row.get('src')
    ruling = rulings.get(row.get('spec'), {})
    if recorded_hold(ruling) and utc(ruling.get('ts')) and utc(row.get('since')) and utc(ruling['ts']) >= utc(row['since']):
        return 'held-ruling', ruling.get('_src') or ruling.get('src')
    if re.search(r'dependency hold honored|held on purpose', str(row.get('reason',''))+' '+str(row.get('blocker','')), re.I):
        return 'held-report', row.get('src')
    if row.get('bucket') == 'queued' and row.get('owner') == 'builder' and ruling.get('rework') == 'yes':
        return 'held-rework', row.get('src')
    return None


def shape(lane_rows, escalations, answered, held, rulings, open_spawns, windows, owed_overdue, titles, projects, now):
    errors = []
    if lane_rows is None:
        errors.append('Lane unavailable')
    if owed_overdue is None:
        errors.append('Owed digest unavailable')
    stuck, waiting = {}, {}

    def add(target, subject, since, reason, owner, kind, src):
        spec = subject_spec(subject)
        subject = spec or str(subject or '(unknown subject)')
        at = utc(since)
        if at is None:
            errors.append('A stuck item has no recorded start time')
            at = now
        row = dict(spec=spec, subject=subject, title=titles.get(subject) or '(no title on record)',
                   project=projects.get(subject) or 'unknown', since=at.isoformat(),
                   stuck_for_s=max(0,int((now-at).total_seconds())), reason=reason[:140],
                   who=who(owner), kind=kind, src=src or 'ledger (source unavailable)')
        old = target.get(subject)
        if old:
            first, second = (row, old) if at < utc(old['since']) else (old, row)
            row = dict(first, reason=(first['reason']+'; '+second['reason'])[:140])
        target[subject] = row

    for row in lane_rows or []:
        evidence = hold_evidence(row, rulings)
        if evidence:
            kind, src = evidence
            reason = 'Rework is actively assigned.' if kind == 'held-rework' else 'Waiting on purpose for a recorded prerequisite or decision.'
            add(waiting,row['spec'],row.get('since'),reason,row.get('owner') or row.get('actor'),kind,src)
        elif row.get('bucket') == 'STUCK':
            add(stuck,row['spec'],row.get('since'),words(row.get('reason')),row.get('owner') or row.get('actor'),'lane',row.get('src'))
    for e in escalations:
        if e.get('_src') not in answered:
            add(stuck,e.get('subject'),e.get('ts'),words(e.get('why') or e.get('asks') or 'A decision is needed'),e.get('owner'),'escalation',e.get('_src'))
    for e in open_spawns:
        add(stuck,e['subject'],e['since'],'An assigned run has not reported an outcome.','process','spawn',e.get('src') or e['spawn'])
    for e in windows:
        since = utc(e.get('since'))
        if since and (now-since).total_seconds() > 3600:
            for sid in e['specs']:
                add(stuck,sid,e['since'],'The merge window has been open for over an hour.','executor','window',e.get('src') or e['window'])
    for e in owed_overdue or []:
        add(stuck,e['spec'],e['since'],('Owed check: '+str(e.get('criterion') or '')+' '+words(e.get('why')))[:140],e.get('who'),'owed',e.get('src'))
    # An independently open question or missing observation still needs action.
    # Its spec appears once, in the actionable list, with no duplicate held row.
    for sid in stuck:
        waiting.pop(sid, None)
    order = lambda row: (utc(row['since']),row['subject'])
    return dict(at=now.isoformat(),error='; '.join(dict.fromkeys(errors)) or None,count=len(stuck),
                stuck=sorted(stuck.values(),key=order),held=sorted(waiting.values(),key=order))
