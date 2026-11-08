#!/usr/bin/env python3
"""Pure route vocabulary and readiness checks for L-spec-1003."""
import ast
from datetime import datetime, timedelta, timezone
import pathlib
import sys

import grading_env
import owed_routes

# Observation evidence is criterion-local; the reporting seat is not authority.
NEED_CASES = [
    ('sql', 'Read production with read-only SQL', 'no browser here', 'ro-dsn'),
    ('repository', 'Read repository test output', 'no review account', 'repo'),
    ('saved-playwright', 'Read saved Playwright output', 'no browser here', 'repo'),
    ('droplet', 'Read droplet logs through SSH', '', 'droplet'),
    ('node', 'Run repository tests', 'missing node_modules', 'environment'),
    ('postgres', 'Run local Postgres tests', 'missing Postgres', 'environment'),
    ('venv', 'Run repository tests', 'missing venv', 'environment'),
    ('account', 'Log in as staff at https://fixture.invalid', 'no review account', 'credential'),
    ('account-in-criterion', 'Log in as staff; missing review account', '', 'credential'),
    ('credential-in-criterion', 'Requires an approved API token to observe the endpoint', '', 'credential'),
    ('tool-only', 'Run Playwright against an anonymous local fixture', 'no browser executable', 'environment'),
    ('permission', 'Read production SQL', 'Operator: grant SELECT on fixture', 'credential'),
    ('history', 'Observe a historical observation that was never recorded', '', 'unobservable'),
    ('boilerplate', 'review_path: log in as local checkout reader; read test output', 'no browser here', 'repo'),
    ('anonymous', 'Run Playwright against an anonymous local fixture', 'missing chromium', 'environment'),
    ('browser', 'Log in as staff at https://fixture.invalid', 'browser unavailable', 'environment'),
    ('mixed', 'Read repository output and log in as staff at https://fixture.invalid', '', 'unobservable'),
    ('unknown', '', '', 'unobservable'),
    ('window', 'Observe after the next observation window', 'not yet due', 'environment'),
]
for case, observation, why, expected in NEED_CASES:
    text = 'AC1 [backend]: ' + observation + '\nAC2 [ui]: log in as administrator'
    row = dict(criterion='AC1', capability='unknown', why=why)
    actual = owed_routes.need(row, text)
    print('need-case', case, actual)
    assert actual == expected, (case, actual, expected)
assert owed_routes.need(dict(criterion='AC9', capability='browser'), 'malformed') == 'unobservable'

assert owed_routes.MAX_ATTEMPTS == 2
assert owed_routes.ROUTES == {"ro-dsn": "plain", "not-live": "plain", "droplet": "plain",
                              "elapsed": "plain", "repo": "repo", "browser": "browser",
                              "operator-action": None, "ro-dsn-grant": None}
for capability in owed_routes.ROUTES:
    assert owed_routes.route_for(capability) == capability
assert owed_routes.route_for("other") is None
assert owed_routes.route_for("zzz") is None
assert owed_routes.route_for(None) is None
for value, expected in (("git", "repo"), ("view-paths", "repo"), ("node_modules", "repo"),
                        ("unknown", "unknown"), ("browser", "browser"), ("db", "ro-dsn"),
                        ("deploy", "not-live"), (None, "unknown"), ("new-capability", "new-capability")):
    assert owed_routes.from_grader(value) == expected
assert all(owed_routes.from_grader(v) in owed_routes.ROUTES for v in grading_env.GRADER_NEVER)
assert owed_routes.from_grader("browser") == "browser"

now = datetime(2026, 10, 15, tzinfo=timezone.utc)
last = now - timedelta(hours=12)
for cap in ("ro-dsn", "not-live"):
    for timestamp in (last, last.isoformat()):
        row = {"capability": cap, "last_unobservable_at": timestamp}
        assert owed_routes.ready(row, now, {"deploy": now.isoformat()})
        for epochs in ({}, {"deploy": None}, {"deploy": last.isoformat()},
                       {"deploy": (last - timedelta(seconds=1)).isoformat()}, {"deploy": "bad"}):
            assert not owed_routes.ready(row, now, epochs), (row, epochs)
    assert not owed_routes.ready({"capability": cap}, now, {"deploy": now.isoformat()})
for previous, epoch, expected in ((None, "new", True), ("old", "new", True),
                                   ("same", "same", False), ("old", None, False)):
    assert owed_routes.ready({"capability": "droplet", "last_route_epoch": previous}, now,
                             {"droplet-wrapper": epoch}) == expected
assert not owed_routes.ready({"capability": "droplet"}, now, {})
for age, expected in ((timedelta(hours=12), True), (timedelta(hours=13), True),
                      (timedelta(hours=12) - timedelta(seconds=1), False)):
    assert owed_routes.ready({"capability": "elapsed", "last_unobservable_at": now - age}, now, {}) == expected
assert not owed_routes.ready({"capability": "elapsed", "last_unobservable_at": None}, now, {})
assert not owed_routes.ready({"capability": "elapsed"}, now, {})
for cap, expected in (("repo", True), ("other", False), ("browser", True),
                      ("operator-action", False), ("zzz", False), (None, False)):
    assert owed_routes.ready({"capability": cap}, now, {}) == expected
print("owed-lifecycle AC4 ok")

module = ast.parse(pathlib.Path(owed_routes.__file__).read_text())
for node in ast.walk(module):
    if isinstance(node, ast.Import):
        assert all(n.name.split(".")[0] in sys.stdlib_module_names | {'grading_env', 'dispatch', 'fold', 'owed'} for n in node.names)
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0 and node.module.split(".")[0] in sys.stdlib_module_names
print("owed-lifecycle AC5 ok")

# A short grant is a permanent blocked route, never an expired unknown route.
import owed
import sweep_owed
for clock in (now, now + timedelta(days=3650)):
    for epochs in ({}, {"deploy": clock, "droplet-wrapper": "new"}):
        assert owed_routes.route_for("ro-dsn-grant") == "ro-dsn-grant"
        assert not owed_routes.ready({"capability": "ro-dsn-grant"}, clock, epochs)
events = [{"subject": "L-spec-grant", "type": "shipped", "ts": last.isoformat()},
          {"subject": "L-spec-grant", "type": "owed-ac", "criterion": "AC1",
           "wake_at": last.isoformat(), "ts": last.isoformat()},
          {"subject": "L-spec-grant", "type": "owed-unobservable", "criterion": "AC1",
           "capability": "ro-dsn-grant", "why": "permission denied", "ts": now.isoformat()}]
assert owed.checks(events, now)[0]["status"] == "blocked"
assert not sweep_owed.due_candidates({"L-spec-grant": {"evs": events}}, now)
print("AC3 ok")


def test_need_replay():
    import json
    import os
    import subprocess
    import tempfile
    from unittest.mock import patch
    import dispatch
    import fold
    import owed_asks
    import owed_backfill
    clock = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix='need-replay-') as tmp:
        root = pathlib.Path(tmp)
        (root / 'events').mkdir()
        (root / 'content').mkdir()
        sid = 'L-spec-9920'
        (root / 'content' / (sid + '.md')).write_text(
            'AC1 [backend]: Read repository test output\n'
            'AC2 [backend]: Historical observation was never recorded\n')
        def ev(kind, seconds, actor, **fields):
            return dict(type=kind, ts=(clock + timedelta(seconds=seconds)).isoformat(),
                        subject=sid, actor=actor, **fields)
        events = [ev('owed-ac', -100, 'spec-writer', criterion=c, wake_at=(clock-timedelta(days=1)).isoformat())
                  for c in ('AC1', 'AC2')]
        events += [ev('shipped', -90, 'executor')]
        events += [ev('owed-unobservable', -80, 'owed-sweeper', criterion=c,
                      capability='browser', why='browser unavailable') for c in ('AC1', 'AC2')]
        events += [ev('owed-routed', -70+i, 'sweep', criterion='AC1', route='browser') for i in range(2)]
        events += [ev('owed-routed', -50, 'operator', criterion='AC1', route='relabel', capability='browser')]
        for e in events:
            data = dict(e)
            actor = data.pop('actor')
            with (root / 'events' / ('L-' + actor + '-fixture.jsonl')).open('a') as f:
                f.write(json.dumps(data) + '\n')
        with patch.dict(os.environ, DOIT_ROOT=str(root)), patch.object(dispatch, 'CONTENT', root/'content'):
            before = owed.checks(events, clock)[0]
            proposals = owed_routes.relabel(events, clock)
            assert len(proposals) == 3, proposals
            for e in proposals:
                print('relabel-proposal', e['subject'], e['criterion'], e['type'], e.get('need'), e['capability'])
                data = dict(e)
                kind = data.pop('type')
                subject = data.pop('subject')
                assert dispatch.emit(root/'events/L-sweep-local.jsonl', dict(subject=subject), kind,
                                     strict=True, **data) is None
            env = dict(os.environ, DOIT_ROOT=str(root), DOIT_PROJECT='', DOIT_READ_CACHE='0')
            code = '''import fold,owed,owed_routes,owed_asks,json
events=fold.read_events()
specs,_,ignored,_=fold.fold(events)
assert not ignored, ignored
clock=fold.NOW
rows=owed.checks(specs['L-spec-9920']['evs'],clock)
rel=[e for e in events if e.get('need')]
assert {(e['criterion'],e['need'],e['capability']) for e in rel} == {('AC1','repo','repo'),('AC2','unobservable','other')}
unobs=[e for e in events if e['type']=='owed-unobservable' and e.get('capability')=='other']
assert len(unobs)==1 and all(e['why'] and e['evidence'] for e in unobs)
assert owed_routes.relabel(events,clock)==[]
assert not owed_asks.queue(events,clock) or all(r['criterion']!='AC2' for r in owed_asks.queue(events,clock))
row=rows[0]
assert row['route_attempts']=={'browser':2} and row['routing_state']=='pending'
assert owed_routes.ready(row,clock,{}) and row['route_attempts'].get('repo',0)<owed_routes.MAX_ATTEMPTS
print(json.dumps({'admitted_relabels':len(rel),'unobservable':len(unobs)}))
'''
            result = subprocess.run(['/usr/bin/python3', '-c', code], env=env,
                                    cwd=pathlib.Path(__file__).parent, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            print(result.stdout.strip())
            replay = events + [dict(e, actor='sweep', ts=clock.isoformat()) for e in proposals]
            after = owed.checks(replay, clock)[0]
            for key in ('due_at', 'governing_at', 'route_attempts'):
                assert after[key] == before[key], key
            assert owed_routes.relabel(replay, clock) == []
            assert not [a for a in owed_backfill.plan({sid: replay}, [], clock) if a.get('event')]
            # Opposite order: revised backfill targets cannot displace need.
            back = [a['event'] for a in owed_backfill.plan({sid:events}, [], clock) if a.get('event')]
            combined = events + [dict(e, actor='operator', ts=clock.isoformat()) for e in back]
            combined += [dict(e, actor='sweep', ts=(clock+timedelta(seconds=1)).isoformat())
                         for e in owed_routes.relabel(combined, clock)]
            assert owed_routes.relabel(combined, clock) == []
            assert not [a for a in owed_backfill.plan({sid:combined}, [], clock) if a.get('event')]
            returned = replay + [ev('owed-routed', 1, 'sweep', criterion='AC1', route='relabel', capability='browser')]
            exhausted = owed.checks(returned, clock)[0]
            assert exhausted['route_attempts']['browser'] == 2 and exhausted['routing_state'] == 'exhausted'
            assert not (owed_routes.ready(exhausted,clock,{}) and exhausted['route_attempts']['browser'] < 2)
            for suffix in ([ev('owed-met', 2, 'owed-sweeper', criterion='AC1', evidence='fixture'),
                            ev('owed-met', 2, 'owed-sweeper', criterion='AC2', evidence='fixture')],
                           [ev('spec-killed', 2, 'thinker')]):
                assert owed_routes.relabel(replay+suffix, clock) == []
            assert owed_routes.relabel([e for e in events if e['type'] != 'shipped'], clock) == []
            assert owed_routes.relabel([dict(e,actor='builder') for e in events], clock) == []
    print('need replay: real strict append, fresh acquisition, clocks, attempts, both producer orders ok')


test_need_replay()
