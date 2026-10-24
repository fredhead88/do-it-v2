#!/usr/bin/env python3
"""L-spec-1144 regression checks; fixture writes stay in disposable ledgers."""
import copy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import throughput

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent
KEYS = set('queue_depth in_flight median_wait_min p90_wait_min merged_per_hour busy_hours shipped stale_merges unproven_merges minutes_per_spec selfhosted_minutes_per_spec reason'.split())


def event(kind, subject='L-spec-1', minutes=-60, **fields):
    return dict(type=kind, subject=subject, ts=(NOW + timedelta(minutes=minutes)).isoformat(), **fields)


def answer(events, **kwargs):
    return throughput.summary(events, NOW, **kwargs)


def ok(ac, case):
    print(f'1144-AC{ac} {case} ok')


def check_summary():
    empty = answer([])
    assert set(empty) == KEYS
    ok(1, 'keys')
    assert empty == dict(queue_depth=None, in_flight=[], median_wait_min=None, p90_wait_min=None,
                         merged_per_hour=None, busy_hours=0, shipped=0, stale_merges=None,
                         unproven_merges=0, minutes_per_spec=None, selfhosted_minutes_per_spec=None,
                         reason='no events')
    ok(1, 'empty')
    import ast
    tree = ast.parse(Path(throughput.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not {a.name for a in node.names} & {'fold', 'window_gate'}
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {'fold', 'window_gate'}
    ok(1, 'no-fold-import')
    rows = [event('review', n_blocking=0), event('shipped', minutes=-5)]
    before = copy.deepcopy(rows)
    class Frozen(datetime):
        @classmethod
        def now(cls, *args, **kwargs):
            raise AssertionError('summary read the clock')
    with patch.object(throughput, 'datetime', Frozen):
        answer(rows)
    assert rows == before
    ok(1, 'pure')

    clean = event('review', n_blocking=0)
    assert answer([clean])['queue_depth'] == 1
    assert answer([event('review')])['queue_depth'] == 0
    assert answer([event('review', n_blocking='0')])['queue_depth'] == 0
    ok(2, 'queued')
    assert answer([clean, event('review', minutes=-30, n_blocking=2)])['queue_depth'] == 0
    ok(2, 'rework leaves')
    gate = event('merge-gate-clean')
    assert answer([gate])['queue_depth'] == 1
    assert answer([gate, event('review', minutes=-30)])['queue_depth'] == 0
    ok(2, 'gate-clean fallback')
    for kind in ('spec-killed', 'spec-closed'):
        assert answer([clean, event(kind, minutes=-30)])['queue_depth'] == 0
    ok(2, 'killed closed')
    # Copied from the real ledger's field shape: subject is the spec, window separate.
    dropped = dict(type='window-dropped', ts='2026-10-09T21:01:32+00:00',
                   subject='L-spec-0790', project='albert-scott', window='win-20261009-2101')
    old = event('review', dropped['subject'], minutes=-20000, n_blocking=0)
    assert answer([old, dropped])['queue_depth'] == 0
    assert answer([old, dropped])['busy_hours'] == 0
    for kind, fields in [('review', {'n_blocking': 0}), ('merge-gate-clean', {})]:
        assert answer([old, dropped, event(kind, dropped['subject'], **fields)])['queue_depth'] == 1
    ok(2, 'dropped leaves')
    waits = []
    for i, wait in enumerate((10, 40, 100)):
        waits += [event('review', f'L-spec-{i}', minutes=-120, n_blocking=0),
                  event('shipped', f'L-spec-{i}', minutes=-120+wait)]
    stats = answer(list(reversed(waits)))
    assert (stats['median_wait_min'], stats['p90_wait_min']) == (40.0, 100.0)
    newest = [event('review', minutes=-100, n_blocking=0),
              event('review', minutes=-50, n_blocking=0),
              event('merge-gate-clean', minutes=-40), event('shipped', minutes=-10)]
    assert answer(newest)['median_wait_min'] == 40
    ok(2, 'p50 p90')
    assert answer(waits + [event('review', 'old', minutes=-1600, n_blocking=0),
                           event('shipped', 'old', minutes=-1500)])['shipped'] == 3
    assert answer(waits + [event('shipped', 'future', minutes=1)])['shipped'] == 3
    ok(2, 'span')
    assert answer(waits + [event('shipped', f'L-spec-{i}', minutes=-1) for i in range(3)])['shipped'] == 3
    ok(2, 'first shipped')

    assert empty['merged_per_hour'] is None and empty['busy_hours'] == 0
    idle = answer([event('spec-killed')])
    assert idle['busy_hours'] == 0 and idle['merged_per_hour'] is None
    ok(3, 'idle none')
    backlog = []
    for i in range(30):
        backlog += [event('review', str(i), minutes=-180, n_blocking=0), event('shipped', str(i), minutes=-1)]
    stats = answer(backlog)
    assert stats['busy_hours'] == 3 and stats['merged_per_hour'] == 10.0
    ok(3, 'backlog rate')
    partial_now = NOW - timedelta(minutes=30)
    stats = throughput.summary([event('review', minutes=-100, n_blocking=0), event('shipped', minutes=-80)], partial_now, hours=1)
    assert stats['busy_hours'] == 1 and stats['merged_per_hour'] == 1
    ok(3, 'partial hour')

    candidate = event('window-candidate', 'win-a', specs=['L-spec-1'])
    assert answer([candidate])['in_flight'] == [dict(window='win-a', specs=['L-spec-1'], since=candidate['ts'])]
    ok(4, 'candidate')
    # Real window-opened shape, including its 12-character base.
    opened = dict(type='window-opened', subject='win-20261009-2101',
                  ts='2026-10-09T21:01:32+00:00', specs=['L-spec-0790', 'L-spec-0796', 'L-spec-0793'],
                  base='e906f4d46e7d', project='albert-scott')
    opened['ts'] = candidate['ts']
    assert answer([opened])['in_flight'][0]['specs'] == opened['specs']
    ok(4, 'opened fallback')
    cancelled = event('window-cancelled', 'win-a', minutes=-30)
    assert len(answer([candidate, cancelled, event('window-candidate', 'win-a', minutes=-10)])['in_flight']) == 1
    assert len(answer([candidate, cancelled])['in_flight']) == 1
    ok(4, 'cancelled stays')
    for kind in ('window-verified', 'escalation-blocking'):
        assert answer([candidate, event(kind, 'win-a', minutes=-10)])['in_flight'] == []
    assert len(answer([candidate, event('escalation-blocking', 'unrelated', minutes=-1)])['in_flight']) == 1
    ok(4, 'terminal leaves')
    stuck = answer([event('window-opened', 'win-dead\n\x1b', minutes=-721)])
    assert stuck['in_flight'] == [] and 'stuck win-dead' in stuck['reason']
    assert '\n' not in stuck['reason'] and '\x1b' not in stuck['reason']
    boundary = answer([event('window-candidate', 'boundary', minutes=-720)])
    assert len(boundary['in_flight']) == 1
    malicious = answer([event('window-candidate', 'win\n\x1b', specs=['spec\n\x1b'])])
    assert malicious['in_flight'][0]['window'] == 'win  '
    assert malicious['in_flight'][0]['specs'] == ['spec  ']
    ok(4, 'stuck named')

    # Real window-verified shape: subject is the window, sha is full length.
    verified = dict(type='window-verified', subject='win-20261010-0301',
                    ts='2026-10-18T11:50:00+00:00', project='albert-scott',
                    sha='df6e0786e3635c7019f927a4bd9f6b1db4f6edea')
    match = dict(verified, verified_on='a'*40, replaced='a'*40)
    assert answer([match])['stale_merges'] == 0
    ok(5, 'match')
    mismatch = dict(match, verified_on='b'*40)
    assert answer([mismatch])['stale_merges'] == 1
    ok(5, 'mismatch')
    assert answer([mismatch, verified])['unproven_merges'] == 1
    assert answer([mismatch, verified])['stale_merges'] == 1
    ok(5, 'unproven')
    assert answer([verified])['stale_merges'] is None
    ok(5, 'none is none')
    assert answer([dict(verified, verified_on='a'*40)])['stale_merges'] == 0
    ok(5, 'first window')
    interleaved = [event('window-verified', 'a1', minutes=-1500, project='a', sha='a'*40),
                   event('window-verified', 'b1', minutes=-1490, project='b', sha='b'*40),
                   event('window-verified', 'a2', project='a', verified_on='a'*40),
                   event('window-verified', 'b2', project='b', verified_on='b'*40)]
    assert answer(interleaved)['stale_merges'] == 0
    ok(5, 'per project')
    base = event('window-opened', verified['subject'], minutes=-20, base='b'*12)
    assert answer([base, match])['stale_merges'] == 0
    ok(5, 'replaced wins')
    base['base'] = 'a'*12
    assert answer([event('window-verified', 'previous', minutes=-30, project='albert-scott', sha='b'*40),
                   base, dict(verified, verified_on='a'*40)])['stale_merges'] == 0
    ok(5, 'non-window commit')

    shipments = [event('review', str(i), minutes=-120, n_blocking=0) for i in range(2)]
    shipments += [event('shipped', str(i), minutes=-30) for i in range(2)]
    costs = [dict(verified, actions_minutes=12, selfhosted_minutes=8, minutes_source='measured'),
             dict(verified, subject='win-other', actions_minutes=8, selfhosted_minutes=4, minutes_source='estimate')]
    assert answer(shipments + costs)['minutes_per_spec'] == 10
    ok(6, 'ratio')
    assert answer(shipments + costs)['selfhosted_minutes_per_spec'] == 6
    ok(6, 'selfhosted')
    assert answer(costs)['minutes_per_spec'] is None
    assert answer(shipments)['selfhosted_minutes_per_spec'] is None
    ok(6, 'none')
    assert 'minutes: 1 of 2 windows estimated' in answer(shipments + costs)['reason']
    del costs[1]['minutes_source']
    assert 'minutes: 1 of 2 windows estimated' in answer(shipments + costs)['reason']
    ok(6, 'estimate label')
    assert answer(shipments + costs + [event('window-verified', minutes=-1500, actions_minutes=100)])['minutes_per_spec'] == 10
    ok(6, 'span')
    stats = answer(shipments + [dict(verified, actions_minutes='bad')])
    assert stats['minutes_per_spec'] is None and 'actions_minutes' in stats['reason'] and 'win-20261010-0301' in stats['reason']
    assert 'invalid timestamps: 1' in answer([{'type': 'review', 'ts': 'bad'}])['reason']
    ok(6, 'bad minutes named')


def run(args, env, **kwargs):
    return subprocess.run(args, cwd=REPO, env=env, text=True, capture_output=True, **kwargs)


def check_cli():
    with tempfile.TemporaryDirectory(prefix='throughput-') as scratch:
        env = dict(os.environ, DOIT_ROOT=scratch, DOIT_NOW=NOW.isoformat(), DOIT_NO_POKE='1')
        env.pop('DOIT_PROJECT', None)
        writer = '''import fold,json,os,sys
for filename,rows in json.loads(sys.argv[1]):
 os.environ['DOIT_LEDGER_FILE']=filename
 for row in rows:
  fold.append([row['type'],row['subject']]+[k+':='+json.dumps(v) for k,v in row.items() if k not in ('type','subject')])
'''
        fixtures = [('L-reviewer-fixture.jsonl', [event('review', n_blocking=0, minutes=-180)]),
                    ('L-executor-fixture.jsonl', [event('shipped', minutes=-120)]),
                    ('L-builder-fixture.jsonl', [event('review', 'L-spec-forged', n_blocking=0)])]
        written = run([sys.executable, '-c', writer, json.dumps(fixtures)], env | {'PYTHONPATH': str(REPO/'src')})
        assert written.returncode == 0, written.stderr
        cli = run(['./doit', 'throughput', '--json'], env)
        assert cli.returncode == 0, cli.stderr
        reader = '''import fold,throughput,json,os
from datetime import datetime
events=fold.read_events(); ignored={id(x) for x in fold.fold(events)[2]}
print(json.dumps(throughput.summary([e for e in events if id(e) not in ignored],datetime.fromisoformat(os.environ['DOIT_NOW']))))
'''
        computed = run([sys.executable, '-c', reader], env | {'PYTHONPATH': str(REPO/'src')})
        assert computed.returncode == 0, computed.stderr
        stats = json.loads(cli.stdout)
        assert stats == json.loads(computed.stdout) and set(stats) == KEYS
        ok(7, 'roundtrip')
        narrow = run(['./doit', 'throughput', '--hours', '1', '--json'], env)
        assert narrow.returncode == 0 and json.loads(narrow.stdout)['shipped'] == 0 and stats['shipped'] == 1
        ok(7, 'hours')
        for hours in ('abc', '0', '721'):
            bad = run(['./doit', 'throughput', '--hours', hours], env)
            assert bad.returncode != 0 and 'usage:' in bad.stderr
        ok(7, 'bad hours')
        missing = run(['./doit', 'throughput'], env | {'DOIT_ROOT': str(Path(scratch)/'missing')})
        assert missing.returncode == 2 and 'unavailable' in missing.stderr
        invalid_now = run(['./doit', 'throughput'], env | {'DOIT_NOW': 'invalid'})
        assert invalid_now.returncode == 2
        ok(7, 'unreadable')
        assert stats['queue_depth'] == 0
        ok(7, 'forged review ignored')


def check_board():
    # This import happens only inside an isolated root, never on the live ledger.
    import fold
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix='throughput-board-') as scratch:
        rows = [dict(event('review', n_blocking=0), actor='reviewer', _src='L-reviewer-fixture.jsonl:1'),
                dict(event('review', 'L-spec-forged', n_blocking=0), actor='builder', _src='L-builder-fixture.jsonl:1')]
        def render():
            return fold.render(rows, *fold.fold(rows))
        with patch.multiple(fold, ROOT=Path(scratch), EVENTS=Path(scratch)/'events', NOW=NOW):
            board = render()
            lines = board.splitlines()
            merges = [x for x in lines if x.startswith('  merge:')]
            assert len(merges) == 1
            assert merges[0] == '  merge: queue 1 · in flight 0 · wait p50 ?m (24h) · 0.0/h merged · stale ?'
            assert lines[lines.index(merges[0])-1].startswith('  mirror:')
            with patch.object(throughput, 'summary', return_value=dict(answer([]), median_wait_min=40.4, merged_per_hour=1/3)):
                assert 'wait p50 40m (24h) · 0.3/h merged' in render()
            ok(8, 'line')
            with patch.object(throughput, 'summary', return_value=answer([])):
                unknown = render()
            assert 'queue ?' in unknown and 'wait p50 ?m' in unknown and '?/h merged' in unknown and 'stale ?' in unknown
            ok(8, 'none prints ?')
            with patch.object(fold, '_throughput', return_value=(None, 'why')):
                unavailable = render()
            assert '  merge: unavailable — why' in unavailable
            ok(8, 'unavailable')
            def boom(*args, **kwargs):
                raise ValueError('bad\n\x1b input')
            with patch.object(fold, '_throughput', return_value=(SimpleNamespace(summary=boom), None)):
                raised = render()
            assert 'merge: unavailable — throughput.summary raised ValueError: bad   input' in raised
            ok(8, 'raises')
            def strip_merge(text):
                return '\n'.join(x for x in text.splitlines() if not x.startswith('  merge:'))
            assert strip_merge(board) == strip_merge(unavailable) == strip_merge(raised)
            ok(8, 'rest unchanged')
            assert '\nimport throughput' not in inspect.getsource(fold)
            ok(8, 'lazy')


def test_throughput_regressions():
    check_summary()
    check_cli()
    check_board()



def tokens_per_live_spec():
    def tokens(vendor, i, cached, output):
        return dict(vendor=vendor, input=i, cached_input=cached, output=output, reasoning=None)
    rows = [event('shipped', spec) for spec in ('A', 'B', 'C')]
    rows += [event('shipped', 'D', minutes=-8*24*60),
             event('spawn-failed', 'A', reason='preflight:no-pinned-base', tokens=None),
             event('spawn-stale', 'A'),
             event('spawn-done', 'A', tokens=tokens('codex', 100, 80, 10)),
             event('spawn-failed', 'A', backend_ran=True, tokens=tokens('claude', 50, 10, 10)),
             event('spawn-done', 'B', tokens=tokens('codex', 200, 100, 20)),
             event('spawn-done', 'C', tokens=None)]
    expected = dict(codex=75, claude=25, specs=3, unmeasured_specs=1)
    assert throughput.tokens_per_live_spec(rows, NOW) == expected
    assert throughput.tokens_per_live_spec([], NOW) == dict(codex=None, claude=None, specs=0, unmeasured_specs=0)
    boundary = [event('shipped', 'past', minutes=-7*24*60), event('shipped', 'future', minutes=1)]
    assert throughput.tokens_per_live_spec(boundary, NOW)['specs'] == 0
    assert throughput.tokens_per_live_spec([event('shipped')], NOW)['unmeasured_specs'] == 1
    assert throughput.tokens_per_live_spec(rows + [event('spawn-failed', 'B', backend_ran=True)], NOW) == dict(
        codex=30, claude=50, specs=3, unmeasured_specs=2)
    for bad in (None, {}, dict(vendor='codex', input=100, cached_input=None, output=1),
                tokens('codex', True, 0, 1), tokens('codex', 1, 2, 1), tokens('unknown', 2, 1, 1)):
        result = throughput.tokens_per_live_spec([event('shipped'), event('spawn-done', tokens=bad)], NOW)
        assert result == dict(codex=None, claude=None, specs=1, unmeasured_specs=1), result
    assert throughput.tokens_per_live_spec(rows + [event('spawn-done', 'B', minutes=1, tokens=None)], NOW) == expected


def health():
    meters = dict(codex=dict(used=None, limit=100), claude=dict(used=25, limit=100))
    rows = [event('spec-written', 'A', minutes=-1500), event('spec-written', 'A'),
            event('spec-written', 'B'), event('spec-written', 'B'), event('spec-written', 'future', minutes=1),
            event('review', n_blocking=0), event('shipped')]
    got = throughput.health_view(rows, NOW, usage=meters, disk_used_fraction=None)
    assert got == dict(finishing_per_day=1, intake_per_day=1, usage=meters, disk_used_fraction=None,
                       tokens_per_live_spec=dict(codex=None, claude=None, specs=1, unmeasured_specs=1)), got
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'events').mkdir()
        env = dict(os.environ, HOME=str(root), DOIT_ROOT=str(root), DOIT_NOW=NOW.isoformat())
        def cli(*args, **extra):
            return subprocess.run([str(REPO / 'doit'), 'health', *args], env=env | extra,
                                  capture_output=True, text=True)
        result = cli('--json')
        assert result.returncode == 0, result.stderr
        view = json.loads(result.stdout)
        assert set(view) == set(got) and view['tokens_per_live_spec']['codex'] is None
        text = cli()
        assert text.returncode == 0 and len(text.stdout.splitlines()) == 5 and 'unknown' in text.stdout
        for key in got:
            assert key + ': ' in text.stdout
        missing = cli('--json', DOIT_ROOT=str(root / 'missing'))
        assert missing.returncode == 2 and missing.stdout == ''
        before = subprocess.run([str(REPO / 'doit'), 'throughput', '--json'], env=env, capture_output=True)
        cli('--json')
        after = subprocess.run([str(REPO / 'doit'), 'throughput', '--json'], env=env, capture_output=True)
        assert before.stdout == after.stdout and before.returncode == after.returncode == 0


def run_groups():
    import argparse, hashlib
    parser = argparse.ArgumentParser()
    parser.add_argument('-k')
    args = parser.parse_args()
    groups = dict(legacy=test_throughput_regressions, tokens_per_live_spec=tokens_per_live_spec, health=health)
    selected = [name for name in groups if args.k is None or args.k in name]
    if not selected:
        parser.exit(1, 'no group matches: ' + args.k + '\n')
    for name in selected:
        groups[name]()
        body = name + ': passed'
        print(json.dumps(dict(url='cli://throughput/' + name, status=0,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


if __name__ == '__main__':
    run_groups()
