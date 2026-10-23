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


def test_holds_and_active_rework_never_become_overdue():
    due = [dict(spec=s, rule='merge', overdue=True) for s in ['L-spec-1417','L-spec-1418','L-spec-1421','L-spec-1422','L-spec-1423','L-spec-1454']]
    ruling = event('decision', 'lane-stale:L-spec-1417', why='Held on purpose: migration solo order; after 1418 ships.') | {'actor':'thinker'}
    classified = [dict(spec='L-spec-1418', bucket='held'), dict(spec='L-spec-1421', bucket='waiting-on-prereq'),
                  dict(spec='L-spec-1422', bucket='queued', reason='builder owns active rework')]
    d = project([ruling], rules=[*due, dict(spec='L-spec-1423', rule='wave-held', overdue=True)], classified=classified)
    assert d['overdue'] == ['L-spec-1454']
    assert 'L-spec-1417' in d['held']
    assert 'held on purpose' in d['groups'][3]['text']
    unrelated = ruling | {'subject':'realm-operator-panel'}
    assert 'L-spec-1417' not in project([unrelated])['held']
    release = event('unblocked', 'L-spec-1417') | {'ts':'2026-10-23T07:01:00Z'}
    assert 'L-spec-1417' not in project([ruling, release])['held']


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



def test_server_cache_and_unavailable_answer():
    import contextlib
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    import realm
    fake_rules = SimpleNamespace(due=lambda *a: [])
    fake_fold = SimpleNamespace(stall_rows=lambda *a, **k: [], admits=lambda *a: None,
                                open_escalations=lambda *a: [], answered=lambda *a: set())
    @contextlib.contextmanager
    def snapshot(*a):
        yield fake_fold, [], ({}, {}, [], {})
    with patch.dict(sys.modules, {'lane_rules':fake_rules}), patch.object(realm, 'ledger_snapshot', snapshot), \
         patch.dict(realm._GLANCE, {'key':None, 'value':None}):
        first = realm.glance_payload(NOW)
        assert first['error'] is None and len(first['groups']) == 5
        with patch.object(realm, 'ledger_snapshot', side_effect=AssertionError('cached call read ledger')):
            assert realm.glance_payload(NOW + dt.timedelta(seconds=1)) is first
        with patch.object(realm, 'ledger_snapshot', side_effect=OSError('fixture read failure')):
            failed = realm.glance_payload(NOW + dt.timedelta(seconds=31))
            assert failed['error'] and 'collapsed' not in failed


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn(); print('ok', name)
