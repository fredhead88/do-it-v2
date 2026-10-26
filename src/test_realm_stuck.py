"""Recorded holds and all five stuck entry points, without live data."""
import datetime as dt
import json
from pathlib import Path
import re
import tempfile
import realm_stuck as s
import realm_omens as om

NOW = dt.datetime(2026, 10, 25, 6, tzinfo=dt.timezone.utc)
def ago(minutes):
    return (NOW - dt.timedelta(minutes=minutes)).isoformat()
def lane(n, **kw):
    return dict(spec=f'L-spec-{n}', bucket='STUCK', since=ago(180), src=f'fixture:{n}',
                reason='spec written with no prerequisite blocker and no builder dispatched', owner='executor', **kw)
def project(rows=(), **kw):
    args = dict(lane_rows=rows, escalations=[], answered=set(), held=set(), rulings={},
                open_spawns=[], windows=[], owed_overdue=[], titles={}, projects={}, now=NOW)
    args.update(kw)
    d = s.shape(**args)
    assert d['count'] == len(d['stuck'])
    return d

def test_ac1_held_is_never_counted_and_1417_is_held_by_its_ruling():
    rows = [lane(n) | {'reason':'Binding dependency hold honored'} for n in [1295,1296,1297,1304]]
    rows += [lane(1417) | {'reason':'conflict-rework has no active builder dispatch','since':'2026-10-22T20:26:29Z'},
             lane(1507), lane(1508) | {'since':ago(200)}]
    rows += [lane(n) | {'bucket':'waiting-on-prereq'} for n in range(1600,1605)]
    rows += [lane(1605) | {'bucket':'held'}]
    ruling = dict(type='decision', actor='thinker', subject='lane-stale:L-spec-1417',
                  ts='2026-10-22T21:35:13Z', why='Held on purpose: migration solo order', _src='L-thinker-0004.jsonl:6411')
    escalation = dict(subject='capability:unknown:L-spec-1523', owner='thinker', ts=ago(30),
                      why='Identify the capability', _src='L-grader-3095.jsonl:10')
    d = project(rows, rulings={'L-spec-1417':ruling}, escalations=[escalation])
    assert d['count'] == 3
    assert [r['spec'] for r in d['stuck']] == ['L-spec-1508','L-spec-1507','L-spec-1523']
    held = {r['spec']:r for r in d['held']}
    assert set(held) == {f'L-spec-{n}' for n in [1295,1296,1297,1304,1417,*range(1600,1606)]}
    assert all(r['src'] for r in held.values())
    assert held['L-spec-1417']['src'] == ruling['_src']
    assert not set(held) & {r['spec'] for r in d['stuck']}
    assert project(rows, escalations=[escalation])['count'] == 4
    assert project([lane(9)], held={'L-spec-9'})['count'] == 1
    assert project([lane(9)], rulings={'L-spec-9':ruling | {'ts':ago(300)}})['count'] == 1
    rework = {'L-spec-9':ruling | {'rework':'yes'}}
    assert project([lane(9) | {'bucket':'queued','owner':'builder'}], rulings=rework)['held'][0]['kind'] == 'held-rework'
    assert not project([], rulings=rework)['held']
    assert project([lane(1523) | {'reason':'capability-hold: unknown'}], escalations=[escalation])['count'] == 1

def test_ac2_counting_merge_and_escalation_mapping():
    for subject in ['capability:x:L-spec-9','lane-stale:L-spec-9','L-charter-0064','a-slug']:
        e = dict(subject=subject, ts=ago(240), why='Please decide access', owner='thinker', _src='fixture:2')
        d = project(escalations=[e]); row = d['stuck'][0]
        assert row['spec'] == ('L-spec-9' if subject.endswith('L-spec-9') else None)
        assert row['title'] == '(no title on record)' and row['project'] == 'unknown' and row['src']
        assert row['subject'] == (row['spec'] or subject)
        assert not project(escalations=[e], answered={'fixture:2'})['stuck']
    e['subject'] = 'L-spec-9'
    row = project([lane(9)], escalations=[e])['stuck'][0]
    assert row['since'] == ago(240) and row['src'] == 'fixture:2' and row['kind'] == 'escalation'
    assert 'Please decide access' in row['reason'] and 'builder' in row['reason'] and len(row['reason']) <= 140
    assert project(None, escalations=[e])['error']
    d = project([lane(9)], owed_overdue=None)
    assert d['error'] and d['count'] == 1

def test_ac3_spawns_windows_owed_and_vanished_item_parity():
    def ev(kind, spawn, sid, minutes, **kw):
        return dict(type=kind, spawn=spawn, subject=sid, ts=ago(minutes), **kw)
    events = [ev('build-started','builder','L-spec-1',181), ev('spawn-started','research','L-spec-2',11,role='research'),
              ev('build-started','ended','L-spec-3',181), ev('spawn-done','ended','L-spec-3',1),
              ev('build-started','old','L-spec-4',1500), ev('build-started','shipped','L-spec-5',181),
              ev('shipped',None,'L-spec-5',1), ev('build-started','redispatched','L-spec-6',181),
              ev('build-started','new','L-spec-6',2)]
    reduced = [r for e in events if (r := om.reduce_event(e))]
    item = om.vanished_item(reduced, [], NOW)
    found = s.vanished_spawns(events, [], NOW)
    assert {r['spawn'] for r in found} == {'builder','research'}
    assert all(name+' (' in item['detail'] for name in ['builder','research','redispatched'])
    assert {name for name in ['builder','research','ended','old','shipped','redispatched','new'] if name+' (' in item['detail']} == {'builder','research','redispatched'}
    assert project(open_spawns=found)['count'] == 2
    assert project(windows=[dict(window='w',specs=['L-spec-1'],since=ago(61))])['stuck'][0]['kind'] == 'window'
    assert not project(windows=[dict(window='w',specs=['L-spec-1'],since=ago(59))])['stuck']
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); (root/'state').mkdir(); path = root/'state/owed-digest.json'
        assert project(owed_overdue=s.owed_overdue(root, NOW))['error']
        path.write_text('{'); assert s.owed_overdue(root, NOW) is None
        path.write_text(json.dumps(dict(at=ago(500), needs_you=[dict(spec='L-spec-7',criterion='AC1',decided=False,owner='thinker',since=ago(40),ask='Decide access'),dict(spec='L-spec-8',decided=True)], health=[dict(spec='L-spec-9',kind=k,since=ago(50),reason='Expired') for k in ['expired','failed','quarantined']])))
        rows = project(owed_overdue=s.owed_overdue(root, NOW))['stuck']
        assert {r['spec'] for r in rows} == {'L-spec-7','L-spec-9'}
        assert {r['spec']:r['who'] for r in rows} == {'L-spec-7':'thinker','L-spec-9':'executor'}
        assert all(r['kind']=='owed' and r['src']=='state/owed-digest.json' for r in rows)

def test_ac4_plain_words_who_and_order():
    stems = ['no ledger evidence for the next action','unresolvable prerequisite: X','human deadline passed: access',
             'open question has no resolvable deadline; obtain a ruling','evidence bundle failed and not rerun: red',
             'conflict-rework has no active builder dispatch','decision not carried out: dispatch',
             'standing rejected criteria remain: AC1','spec written with no prerequisite blocker and no builder dispatched',
             'build has no live dispatch within its recorded window/cap','build blocked with no active next action: access',
             'graded with no active reviewer dispatched','unexpected bucket STUCK stalls dependency-held']
    forbidden = r'\b(stalls|STUCK|bucket|dependency-held|wave-held|migration-sequenced|conflict-rework|waiting-on-prereq|capability-hold)\b'
    for stem in stems:
        row = project([lane(9) | {'reason':stem}])['stuck'][0]
        assert len(row['reason']) <= 140 and row['reason'] != stem and not re.search(forbidden,row['reason'])
    for owner, who in [('tick','executor'),('-','executor'),('','executor'),('you','operator'),('grader','process')]:
        assert project([lane(9) | {'owner':owner}])['stuck'][0]['who'] == who
    rows = project([lane(3),lane(2),lane(1) | {'since':ago(-1)}])['stuck']
    assert [r['spec'] for r in rows] == ['L-spec-2','L-spec-3','L-spec-1']
    for row in rows:
        assert row['stuck_for_s'] == max(0,int((NOW-dt.datetime.fromisoformat(row['since'])).total_seconds()))

if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn(); print('ok', name)
