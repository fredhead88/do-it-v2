"""SD2 fixture events shared by backend and browser regressions; no live data."""
import datetime as dt
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
import realm_stuck as s
import realm_omens as om

NOW = dt.datetime(2026, 10, 25, 6, tzinfo=dt.timezone.utc)
EVIL = '<img src=x onerror="window.__x=1"><script>window.__x=1</script>&amp;'

def fixture_events(now=NOW, scenario='base'):
    if '+' in scenario:
        return sorted([e for part in scenario.split('+') for e in fixture_events(now,part)],key=lambda e:e['ts'])
    events = []
    def ev(kind, n, age, actor='executor', **kw):
        events.append(dict(type=kind,subject=f'L-spec-{n}',actor=actor,
                      ts=(now-dt.timedelta(minutes=age)).isoformat(),
                      _src=f'L-{actor}-fixture.jsonl:{len(events)+1}', **kw))
    def written(n, age=180, actor='planner'):
        ev('spec-written',n,age,actor,title='Fixture '+str(n)+' '+EVIL,project='fixture-project')
    def review(n, age=121):
        written(n,age+60); ev('build-done',n,age+30,'builder',status='DONE')
        ev('verdict',n,age+20,'grader',confirmed=True); ev('review',n,age,'reviewer',n_blocking=0)
    if scenario in {'base','owed-unavailable','lane-unavailable'}:
        review(9001)
        written(9002); ev('question',9002,120,'builder',asks='Decide '+EVIL,deadline=(now-dt.timedelta(minutes=1)).isoformat())
        for n,actor in [(9003,'operator'),(9004,'thinker')]:
            written(n); ev('decision',n,65,actor,why='Held on purpose: '+EVIL)
        written(9005); ev('dependency-hold',9005,60,'thinker',waits_for='L-spec-9999',why=EVIL)
        written(9006,20,'spec-writer')
        written(9007); ev('spawn-failed',9007,3,spawn='L-builder-9007',role='builder',reason='capacity')
        written(9008); ev('decision',9008,10,'thinker',rework='yes',why='Repair is assigned')
        ev('spawn-started',9008,5,spawn='L-builder-9008',role='builder')
    elif scenario in {'escalation','held-escalation','expired-decision'}:
        written(9010)
        ev('escalation-blocking',9010,120,why='Decide '+EVIL,default='wait',revert='none',
           deadline=(now+dt.timedelta(minutes=-1 if scenario=='expired-decision' else 60)).isoformat())
        if scenario=='held-escalation':
            ev('decision',9010,60,'operator',why='Held on purpose: '+EVIL)
    elif scenario=='window':
        review(9011,180)
        ev('window-opened','window',90,specs=['L-spec-9011'])
    elif scenario=='dead-attempt':
        written(9012); ev('spawn-started',9012,100,role='builder',spawn='L-builder-9012')
        ev('spawn-failed',9012,10,role='builder',spawn='L-builder-9012',reason='capacity')
    elif scenario!='owed':
        raise ValueError(scenario)
    return sorted(events,key=lambda e:e['ts'])

def fixture(now=NOW, scenario='base'):
    import fold
    events = fixture_events(now,scenario)
    with patch.object(fold,'NOW',now):
        folded=fold.fold(events)
    lane=fold.stall_lane_rows(events,now=now,specs=folded[0],by_subject=folded[3])
    return events,folded,lane

def project(rows=(), now=NOW, **kw):
    args=dict(lane_rows=rows,escalations=[],answered=set(),held=set(),rulings={},open_spawns=[],
              windows=[],owed_overdue=[],titles={},projects={},now=now)
    args.update(kw)
    d=s.shape(**args)
    assert d['count']==len(d['stuck'])
    return d

def fixture_payload(now=NOW, scenario='base'):
    import fold,realm_glance as g
    events,folded,lane=fixture(now,scenario)
    facts=g.hold_facts(events,[],[],lane,now)
    titles={e['subject']:e['title'] for e in events if e.get('title')}
    owed=[dict(spec='L-spec-9990',criterion='AC1',since=now.isoformat(),why='overdue')]
    payload=project(None if scenario=='lane-unavailable' else lane,now,
            escalations=fold.open_escalations(events),held=facts['held'],rulings=facts['rulings'],
            open_spawns=s.vanished_spawns(events,[],now),windows=facts['windows'],
            owed_overdue=None if scenario=='owed-unavailable' else owed,titles=titles,
            projects={sid:'fixture-project' for sid in titles})
    return events,folded,lane,payload

def test_ac8_lane_authority_and_named_scenarios():
    from collections import Counter
    events,folded,lane,payload=fixture_payload()
    assert Counter(r['bucket'] for r in lane)=={'STUCK':2,'held':3,'audit-wait':1,'retry-wait':1,'in-flight':1},lane
    assert payload['count']==2 and len(payload['held'])==3
    assert {r['kind'] for r in payload['held']}=={'operator','decision','dependency'}
    for scenario,bucket,count,held in [('escalation','waiting-on-human',0,0),('window','queued',0,0),
          ('held-escalation','held',0,1),('expired-decision','STUCK',1,0),('dead-attempt','STUCK',1,0),
          ('owed',None,0,0),('owed-unavailable',None,2,3)]:
        _,_,rows,d=fixture_payload(scenario=scenario)
        if bucket: assert rows[0]['bucket']==bucket,(scenario,rows)
        assert d['count']==count and len(d['held'])==held and not d['error'],(scenario,d)
        print(scenario,[(r['spec'],r['bucket']) for r in rows],d['count'],len(d['held']))
    assert fixture_payload(scenario='lane-unavailable')[-1]['error']
    for scenario,held in [('escalation',3),('window',3),('held-escalation',4)]:
        _,_,_,combined=fixture_payload(scenario='base+'+scenario)
        assert combined['count']==2 and len(combined['held'])==held
        print('base plus',scenario,combined['count'],len(combined['held']))
    # Independent facts cannot override a lane, even a later prose hold ruling.
    row=next(r for r in lane if r['bucket']=='STUCK')
    ruling=dict(type='decision',actor='thinker',ts=NOW.isoformat(),why='Held on purpose: incidental')
    assert project([row],held={row['spec']},rulings={row['spec']:ruling})['count']==1
    assert not project([r for r in lane if r['spec']=='L-spec-9008'])['held']
    held_events=events+[dict(type='decision',subject='L-spec-9008',actor='operator',
                            ts=NOW.isoformat(),why='Held on purpose: maintenance',_src='fixture:hold')]
    import fold
    with patch.object(fold,'NOW',NOW): specs,_,_,by=fold.fold(held_events)
    held_lane=fold.stall_lane_rows(held_events,specs,NOW,by_subject=by)
    assert next(r for r in held_lane if r['spec']=='L-spec-9008')['bucket']=='held'
    assert 'L-spec-9008' in {r['spec'] for r in project(held_lane)['held']}
    for r in lane: print('provenance',r['spec'],r['state'],r['bucket'],r['hold_kind'],r['due_since'],r['reason'])

def test_vanished_and_owed_readers_remain_available():
    def ev(kind,spawn,sid,minutes,**kw):
        return dict(type=kind,spawn=spawn,subject=sid,ts=(NOW-dt.timedelta(minutes=minutes)).isoformat(),**kw)
    events=[ev('build-started','builder','L-spec-1',181),ev('spawn-started','research','L-spec-2',11,role='research'),
            ev('build-started','ended','L-spec-3',181),ev('spawn-done','ended','L-spec-3',1),
            ev('build-started','old','L-spec-4',1500),ev('build-started','shipped','L-spec-5',181),
            ev('shipped',None,'L-spec-5',1),ev('build-started','redispatched','L-spec-6',181),
            ev('build-started','new','L-spec-6',2)]
    reduced=[r for e in events if (r:=om.reduce_event(e))]
    item=om.vanished_item(reduced,[],NOW)
    found=s.vanished_spawns(events,[],NOW)
    assert {r['spawn'] for r in found}=={'builder','research'}
    assert all(name+' (' in item['detail'] for name in ['builder','research','redispatched'])
    assert {name for name in ['builder','research','ended','old','shipped','redispatched','new'] if name+' (' in item['detail']}=={'builder','research','redispatched'}
    assert project(open_spawns=found)['count']==0  # SD2: no independent additions.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'state').mkdir();path=root/'state/owed-digest.json'
        assert s.owed_overdue(root,NOW) is None
        path.write_text('{');assert s.owed_overdue(root,NOW) is None
        path.write_text(json.dumps(dict(at=NOW.isoformat(),needs_you=[dict(spec='L-spec-7',criterion='AC1',decided=False,owner='thinker',since=NOW.isoformat(),ask='Decide access'),dict(spec='L-spec-8',decided=True)],health=[dict(spec='L-spec-9',kind=kind,since=NOW.isoformat(),reason='Expired') for kind in ['expired','failed','quarantined']])))
        owed=s.owed_overdue(root,NOW)
        assert {r['spec']:r['who'] for r in owed}=={'L-spec-7':'thinker','L-spec-9':'executor'}
        assert all(r['src']=='state/owed-digest.json' for r in owed)
        assert project(owed_overdue=owed)['count']==0

def test_plain_words_who_order_and_jargon():
    _,_,lane,_=fixture_payload()
    row=next(r for r in lane if r['bucket']=='STUCK')
    stems=['no ledger evidence for the next action','unresolvable prerequisite: X','human deadline passed: access',
           'open question has no resolvable deadline; obtain a ruling','evidence bundle failed and not rerun: red',
           'conflict-rework has no active builder dispatch','decision not carried out: dispatch',
           'standing rejected criteria remain: AC1','spec written with no prerequisite blocker and no builder dispatched',
           'build has no live dispatch within its recorded window/cap','build blocked with no active next action: access',
           'graded with no active reviewer dispatched','unexpected bucket STUCK stalls dependency-held']
    for stem in stems:
        reason=project([row|{'reason':stem}])['stuck'][0]['reason']
        assert len(reason)<=140 and reason!=stem and not s.JARGON.search(reason)
    for token in ['STUCK','held','audit-wait','retry-wait','in-flight','bucket','dependency-held','wave-held','migration-sequenced','conflict-rework','waiting-on-prereq','capability-hold']:
        reason=s.words('unexpected '+token)
        assert not re.search(r'\b'+re.escape(token)+r'\b',reason,re.I),(token,reason)
        assert len(reason)<=140
    for owner,who in [('tick','executor'),('-','executor'),('','executor'),('you','operator'),('grader','process')]:
        assert project([row|{'owner':owner}])['stuck'][0]['who']==who
    d=project([row|{'spec':'L-spec-3'},row|{'spec':'L-spec-2'}])
    assert [r['spec'] for r in d['stuck']]==['L-spec-2','L-spec-3']
    assert all(r['stuck_for_s']>=0 and r['src'] for r in d['stuck'])
    future=(NOW+dt.timedelta(minutes=1)).isoformat()
    future_row=project([row|{'since':future,'due_since':future}])['stuck'][0]
    assert future_row['stuck_for_s']==0

if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'): fn();print('ok',name)
