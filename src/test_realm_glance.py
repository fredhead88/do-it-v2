"""What matters: actual windows, intentional holds, and plain collapsed copy."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import realm_glance as g

NOW = dt.datetime(2026, 10, 23, 8, tzinfo=dt.timezone.utc)
GROUPS = g.load_priorities(Path(__file__).resolve().parents[1] / 'realm_priorities.json')


def event(kind, subject='L-spec-1430', **kw):
    return dict(type=kind, subject=subject, actor='executor', ts='2026-10-23T07:00:00Z', **kw)


def project(events=(), states=(), rules=(), classified=(), **kw):
    return g.shape(events, states, rules, classified, GROUPS, NOW, **kw)


def test_real_open_window_beats_ledger_queue_position():
    events = [event('queue-add', 'L-spec-1430', branch='l-spec-1430'),
              event('window-opened', 'solo', specs=['L-spec-1430']),
              event('window-candidate', 'solo', specs=['L-spec-1430'])]
    d = project(events)
    assert d['groups'][1]['status'] == 'merging now'
    assert d['collapsed'].startswith("Dina's tools merging now · ")
    assert 'position' not in json.dumps(d) and 'next' not in d and not d['queue_available']
    assert not project([events[-1]])['merging']  # candidate alone is insufficient
    assert not project([dict(events[1], actor='builder')])['merging']
    for terminal in ['window-verified', 'window-cancelled', 'escalation-blocking']:
        assert not project([*events, event(terminal, 'solo')])['merging']
    for kind in ['shipped', 'window-dropped', 'window-deferred', 'merge-conflict']:
        assert not project([*events, event(kind, window='solo')])['merging']
    assert not project([*events, event('shipped') | {'actor': 'tick'}])['merging']
    reopened = [*events, event('window-verified', 'solo'), events[1]]
    assert project(reopened)['merging'] == ['L-spec-1430']


def test_partial_drop_and_tree_checks_are_not_window_completion():
    opened = event('window-opened', 'solo', specs=['L-spec-1430', 'L-spec-1417'])
    d = project([opened, event('window-dropped', window='solo')])
    assert d['merging'] == ['L-spec-1417']
    for green in [False, True]:
        d = project([opened, event('window-tree-verified', 'solo', green=green)])
        assert d['merging'] == ['L-spec-1417', 'L-spec-1430']
        assert d['groups'][1]['status'] == ('merging now' if green else 'checks failed; waiting for repair')


def test_ac7_hold_facts_use_sd2():
    import dispatch
    import fold
    from test_realm_stuck import fixture
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix='realm-glance-ac7-') as tmp:
        root = Path(tmp)
        (root / 'events').mkdir()
        (root / 'content').mkdir()
        tick = root / 'events' / 'L-tick-local.jsonl'
        assert not tick.exists()
        # Seat diagnostics also use fold.NOW to age active runs. Keep that clock
        # on the fixture snapshot so a live rework cannot emit a stale tick.
        with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'), \
                patch.object(fold, 'NOW', NOW), patch.object(dispatch, 'CONTENT', root / 'content'):
            events, folded, classified = fixture(NOW)
            # Include queued and waiting-on-human, completing the enumerated SD2 matrix.
            events += [dict(type='spec-written',subject='L-spec-9014',actor='planner',ts=(NOW-dt.timedelta(minutes=10)).isoformat()),
                       dict(type='spec-written',subject='L-spec-9015',actor='planner',ts=(NOW-dt.timedelta(minutes=100)).isoformat()),
                       dict(type='question',subject='L-spec-9015',actor='builder',ts=(NOW-dt.timedelta(minutes=90)).isoformat(),deadline=(NOW+dt.timedelta(hours=1)).isoformat())]
            for i,e in enumerate(events): e.setdefault('_src',f'fixture:{i+1}')
            specs,_,_,by=fold.fold(events)
            classified=fold.stall_lane_rows(events,specs,NOW,by_subject=by)
            assert {r['bucket'] for r in classified}==__import__('fold_stalls').BUCKETS
            facts=g.hold_facts(events,[],[dict(spec='L-spec-9990',rule='wave-held')],classified,NOW)
            assert facts['held']=={r['spec'] for r in classified if r['bucket']=='held'}
            groups={'Running':['L-spec-9008']}
            assert 'owns active dispatch' not in next(r for r in classified if r['spec']=='L-spec-9008')['reason']
            shaped=g.shape(events,[(sid,s['state']) for sid,s in specs.items()],[],classified,groups,NOW)
            assert shaped['groups'][0]['status']=='building' and 'L-spec-9008' not in shaped['held']
            release=dict(type='unblocked',subject='L-spec-9003',actor='operator',ts=NOW.isoformat())
            events.append(release)
            specs,_,_,by=fold.fold(events)
            released=fold.stall_lane_rows(events,specs,NOW,by_subject=by)
            assert 'L-spec-9003' not in g.hold_facts(events,[],[],released,NOW)['held']
            assert not tick.exists(), 'classification must not create a tick ledger'
            print('AC7 classifier held set:',sorted(facts['held']),'active rework building; released operator hold absent; tick ledger absent')


def test_deployment_requires_exact_evidence_and_never_equates_acceptance_to_live():
    ship = event('shipped', sha='abc')
    assert project([ship])['groups'][1]['status'] == 'shipped'
    assert project([ship, event('deploy-landed', 'other', sha='xyz')])['groups'][1]['status'] == 'shipped'
    assert project([ship, event('deploy-landed', 'other', sha='abc')])['groups'][1]['status'] == 'live'
    assert project(states=[('L-spec-1430','accepted')])['groups'][1]['status'] == 'shipped'


def test_only_unanswered_operator_owned_escalations_need_you():
    needs = [event('escalation-blocking', owner=o, why='Approve the access change', _src=o) for o in ['operator', 'thinker', 'executor', 'you']]
    d = project(needs=needs, answered={'you'})
    assert len(d['needs_you']) == 1
    assert '1 thing needs you' in d['collapsed']
    assert d['needs_you'][0]['ask'] == 'Approve the access change'
    assert d['collapsed'].startswith('1 thing needs you')
    assert 'No operator decisions waiting' in project(needs=needs, answered={'operator', 'you'})['collapsed']


def test_collapsed_and_config():
    d = project()
    assert len(d['groups']) == 5 and all(name in d['collapsed'] for name in list(GROUPS)[:3])
    assert '\n' not in d['collapsed'] and '<a' not in d['collapsed']
    assert GROUPS['Listings'] == ['L-spec-1409','L-spec-1410','L-spec-1466','L-spec-1412']
    assert GROUPS['Data connector V1'] == ['L-spec-1417','L-spec-1418','L-spec-1421','L-spec-1422','L-spec-1423']
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'priorities.json'
        path.write_text('{"Listings": ["garbage"]}')
        try:
            g.load_priorities(path)
            raise AssertionError('bad config accepted')
        except ValueError:
            pass
    assert not project([event('window-opened','future',specs=['L-spec-1430']) | {'ts':'2027-01-01T00:00:00Z'}])['merging']




def test_terminal_check_failure_is_visible_until_repair_or_new_window():
    opened = event('window-opened', 'solo', specs=['L-spec-1430'])
    failed = event('escalation-blocking', 'solo', outcome='undetermined')
    red = event('window-tree-verified', 'solo', green=False)
    d = project([opened, failed, red])
    assert not d['merging']
    assert d['groups'][1]['status'] == 'checks need repair'
    assert d['check_issues'] == ['L-spec-1430']
    assert 'checks need attention' in d['attention']
    assert not project([opened, failed, red], states=[('L-spec-1430','accepted')])['check_issues']
    assert not project([opened, failed, red], classified=[dict(spec='L-spec-1430',bucket='held')])['check_issues']
    for progress in [event('build-started'), opened, event('shipped')]:
        assert not project([opened, failed, red, progress])['check_issues']


def test_ready_for_review_and_ready_to_merge_are_not_active_review():
    assert project(states=[('L-spec-1430','graded')])['groups'][1]['status'] == 'ready for review'
    assert project([event('review',n_blocking=0)], states=[('L-spec-1430','graded')])['groups'][1]['status'] == 'ready to merge'
    assert project([event('review',n_blocking=1)])['groups'][1]['status'] == 'changes requested'
    active = [dict(spec='L-spec-1430',owner='builder',bucket='in-flight',reason='builder has a live assigned run')]
    assert project(states=[('L-spec-1430','graded')],classified=active)['groups'][1]['status'] == 'building'


def test_server_cache_and_unavailable_answer():
    import contextlib
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    import realm
    fake_rules = SimpleNamespace(due=lambda *a: [], load=lambda: None)
    fake_fold = SimpleNamespace(stall_lane_rows=lambda *a, **k: [], stall_rows=lambda *a, **k: [], admits=lambda *a: None,
                                open_escalations=lambda *a: [], answered=lambda *a: set())
    @contextlib.contextmanager
    def snapshot(*a):
        yield fake_fold, [], ({}, {}, [], {})
    with patch.dict(sys.modules, {'lane_rules':fake_rules}), patch.object(realm, 'ledger_snapshot', snapshot), \
         patch.dict(realm._GLANCE, {'key':None, 'value':None}), patch.dict(realm.LEDGER.stalls, clear=True):
        first = realm.glance_payload(NOW)
        assert first['error'] is None and len(first['groups']) == 5
        with patch.object(fake_fold, 'stall_rows', side_effect=AssertionError('cached call recomputed stalls')):
            assert realm.glance_payload(NOW + dt.timedelta(seconds=1)) is first
        with patch.object(realm, 'ledger_snapshot', side_effect=OSError('fixture read failure')):
            failed = realm.glance_payload(NOW + dt.timedelta(seconds=61))
            assert failed['error'] and 'collapsed' not in failed


def test_ac5_one_payload_one_number_no_new_read():
    import contextlib
    import subprocess
    from unittest.mock import patch
    from types import SimpleNamespace
    import realm
    import tempfile
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp); (path/'state').mkdir()
        (path/'state/owed-digest.json').write_text(json.dumps(dict(at=NOW.isoformat(),needs_you=[],health=[])))
        fold = SimpleNamespace(stall_rows=lambda *a,**k: [],stall_lane_rows=lambda *a,**k: [],admits=lambda *a: None,
                               open_escalations=lambda *a: [],answered=lambda *a: set())
        @contextlib.contextmanager
        def snapshot(*a):
            yield fold, [], ({}, {}, [], {})
        with patch.object(realm,'ROOT',path), patch.object(realm,'ledger_snapshot',snapshot), patch.dict(realm._GLANCE,key=None,value=None), patch.dict(realm.LEDGER.stalls, clear=True):
            first = realm.glance_payload(NOW)
            assert first['stuck']['error'] is None and 'overdue' not in first
            assert 'specs are stuck' not in first['attention'] and 'overdue' not in first['attention']
            with patch.object(fold,'stall_rows',side_effect=AssertionError('cache recomputed')):
                assert realm.glance_payload(NOW+dt.timedelta(seconds=1)) is first
            with patch.object(realm,'codex_payload',return_value={}),patch.object(realm,'sys_signals',return_value={}),patch.object(realm,'read_all',return_value=([],[],{})):
                with patch.object(fold,'stall_rows',side_effect=AssertionError('same-generation bootstrap recomputed')):
                    for _ in range(2):
                        assert realm.bootstrap(24,states=[],goals=[],proving=[],now=NOW)['glance']['stuck'] == first['stuck']
            with patch.object(realm,'ledger_snapshot',side_effect=OSError('unavailable')):
                failed = realm.glance_payload(NOW+dt.timedelta(seconds=61))
                assert failed['error'] and 'stuck' not in failed
    for count in [0,1,3]:
        d = project(stuck=dict(count=count,error=None))
        clause = f'{count} '+('spec is' if count==1 else 'specs are')+' stuck'
        assert (clause in d['attention']) == bool(count)
    assert 'stuck count unavailable' in project(stuck=dict(count=0,error='unreadable'))['attention']
    parent = subprocess.check_output(['git','show',subprocess.check_output(['git','merge-base','HEAD','main'],text=True).strip()+':src/realm.py'],text=True)
    current = (root/'src/realm.py').read_text()
    import re
    routes = lambda text: re.findall(r'(?:path|p) == ["\']([^"\']+)', text[text.index('    def do_GET('):])
    assert routes(parent) == routes(current)
    base_html = subprocess.check_output(['git','show',subprocess.check_output(['git','merge-base','HEAD','main'],text=True).strip()+':src/realm.html'],text=True)
    html = (root/'src/realm.html').read_text()
    for call in ['setInterval(', 'EventSource(', 'fetch(']:
        assert html.count(call) == base_html.count(call)


def test_ac6_stuck_rows_equal_the_lane():
    import contextlib
    import time
    from unittest.mock import patch
    import fold
    import realm
    import realm_stuck
    events = []
    def ev(kind, sid, minutes, actor='executor', **kw):
        events.append(dict(type=kind,subject=sid,actor=actor,ts=(NOW-dt.timedelta(minutes=minutes)).isoformat(),_src=f'fixture:{len(events)+1}',**kw))
    for sid, minutes in [('L-spec-1',20),('L-spec-2',3000),('L-spec-3',3000),('L-spec-4',3000)]:
        ev('spec-written',sid,minutes,actor='planner',project='fixture')
    ev('blocked','L-spec-3',280,actor='builder',blocker='L-spec-5',reason='wait for L-spec-5')
    ev('shipped','L-spec-5',60)
    ev('decision','lane-stale:L-spec-4',30,actor='thinker',why='Held on purpose: dependency order')
    events.sort(key=lambda e:e['ts'])
    specs = {sid:dict(state=state,ts=next(e['ts'] for e in events if e['subject']==sid)) for sid,state in [('L-spec-1','written'),('L-spec-2','written'),('L-spec-3','blocked'),('L-spec-4','written'),('L-spec-5','shipped')]}
    by = {sid:[e for e in events if e['subject']==sid] for sid in specs}
    for sid in specs:
        specs[sid].update(id=sid,evs=by[sid])
    @contextlib.contextmanager
    def snapshot(*a):
        yield fold, events, (specs,{},[],by)
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp);(path/'state').mkdir();(path/'state/owed-digest.json').write_text(json.dumps(dict(at=NOW.isoformat(),needs_you=[],health=[])))
        with patch.object(realm,'ROOT',path),patch.object(realm,'ledger_snapshot',snapshot),patch.dict(realm._GLANCE,key=None,value=None), patch.dict(realm.LEDGER.stalls, clear=True):
            start=time.perf_counter();glance=realm.glance_payload(NOW);elapsed=time.perf_counter()-start
            assert not glance['error'], glance
            lane=fold.stall_lane_rows(events,specs,NOW,by_subject=by)
            facts=g.hold_facts(events,[],[],[],NOW)
            expected={r['spec'] for r in lane if r['bucket']=='STUCK' and not realm_stuck.hold_evidence(r,facts['rulings'])}
            actual={r['spec'] for r in glance['stuck']['stuck'] if r['kind']=='lane'}
            assert actual == expected and 'L-spec-2' in actual, (lane,glance)
            assert 'L-spec-1' not in actual and 'L-spec-3' in actual
            with patch.object(realm_stuck,'shape',return_value=dict(count=0,error=None,stuck=[],held=[])),patch.dict(realm._GLANCE,key=None,value=None), patch.dict(realm.LEDGER.stalls, clear=True):
                start=time.perf_counter();realm.glance_payload(NOW);without=time.perf_counter()-start
            print(f'fixture glance with stuck {elapsed:.6f}s; without {without:.6f}s; delta {elapsed-without:.6f}s')


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn(); print('ok', name)
