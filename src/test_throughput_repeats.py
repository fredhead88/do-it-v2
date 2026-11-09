#!/usr/bin/env python3
"""Repeat-report acceptance fixtures; subprocesses use disposable ledgers only."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import throughput

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent
CAUSES = ('merge-gate', 'rejected-criterion', 'conflict', 'stale-branch',
          'launch-failure', 'other', 'unrecorded')
REASONS = ('bundle-pending', 'duplicate-dispatch', 'dispatch-in-progress', 'wave-order')
NEW = ('repeat_rate', 'repeats_by_cause', 'one_build_share', 'median_builds',
       'gate_sendbacks_per_merged', 'failed_starts_by_reason_per_day', 'handin_cpu_s_per_day')
PROOFS = []


def event(kind, subject='L-spec-1', hours=-1, **fields):
    actor = {'build-started': 'builder', 'shipped': 'executor',
             'merge-gate-rework': 'executor', 'spawn-failed': 'grader'}.get(kind, 'builder')
    return dict(type=kind, subject=subject, ts=(NOW + timedelta(hours=hours)).isoformat(),
                actor=actor) | fields


def report(rows, **kwargs):
    return throughput.summary(rows, NOW, **kwargs)


def metrics(rows, **kwargs):
    stats = report(rows, **kwargs)
    return {key: stats[key] for key in NEW}


def prove(name, actual, expected):
    assert actual == expected, (name, actual, expected)
    PROOFS.append({name: actual})


def empty_metrics():
    return dict(repeat_rate=None, one_build_share=None, median_builds=None,
                gate_sendbacks_per_merged=None,
                repeats_by_cause={key: dict(count=0, share=None) for key in CAUSES},
                failed_starts_by_reason_per_day={day: dict.fromkeys(REASONS, 0)
                                               for day in ('2026-11-07', '2026-11-08')},
                handin_cpu_s_per_day={'2026-11-07': 0, '2026-11-08': 0})


def rounds(stats):
    return [stats[key] for key in ('repeat_rate', 'one_build_share', 'median_builds')]


def ac1():
    prove('empty', metrics([]), empty_metrics())
    prove('unrelated', metrics([event('unrelated')]), empty_metrics())
    rows = []
    for n in (1, 2, 3):
        subject = f'L-spec-{n}'
        rows += [event('build-started', subject, hours=-48+i) for i in range(n)]
        rows += [event('shipped', subject, hours=-2), event('shipped', subject)]
        rows += [event('build-started', subject, hours=-.5)]  # After latest shipment.
    rows += [event('build-started', 'L-spec-unmerged'), event('shipped', 'not-a-spec')]
    prove('one_two_three', rounds(report(rows)), [.5, 1/3, 2])
    continued = rows + [event('build-started', f'L-spec-{n}', hours=-3,
                              continuation=True, cause='conflict') for n in (1, 2, 3)]
    prove('boolean_continuations', metrics(continued), metrics(rows))
    prove('zero_round_shipment', rounds(report(rows + [event('shipped', 'L-spec-zero')])),
          [.5, .25, 1.5])
    values = [{}, {'continuation': 'true'}, {'continuation': 1},
              {'continuation': False}, {'continuation': True}]
    mixed = [event('build-started', hours=-10+i, **fields) for i, fields in enumerate(values)]
    prove('strict_boolean', rounds(report(mixed + [event('shipped')])), [.75, 0, 4])
    boundary = [event('shipped', 'L-spec-left', hours=-24),
                event('shipped', 'L-spec-right', hours=0),
                event('shipped', 'L-spec-future', hours=1)]
    prove('cohort_boundaries', rounds(report(boundary)), [None, 0, 0])
    # The latest shipment, not the first, clips lifetime starts.
    prove('latest_cutoff', rounds(report([event('build-started', hours=-40),
          event('shipped', hours=-4), event('build-started', hours=-3), event('shipped')])), [.5, 0, 2])


def ac2():
    rows = [event('build-started', hours=-20, cause='conflict')]
    rows += [event('build-started', hours=-19+i, **({} if key == 'unrecorded' else {'cause': key}))
             for i, key in enumerate(CAUSES)]
    rows += [event('shipped'), event('merge-gate-rework', hours=-10),
             event('rejected-criterion', hours=-10),
             event('build-started', hours=-2, continuation=True, cause='conflict')]
    prove('seven_causes', report(rows)['repeats_by_cause'],
          {key: dict(count=1, share=1/7) for key in CAUSES})
    malformed = [None, [], {}, 1, True, 'unexpected', 'conflict\n\x1b[31m']
    rows += [event('build-started', hours=-3, cause=value) for value in malformed]
    rows += [event('build-started', hours=-3, continuation='true', cause='other'),
             event('build-started', hours=-3, continuation=1, cause='launch-failure')]
    buckets = report(rows)['repeats_by_cause']
    counts = dict.fromkeys(CAUSES, 1)
    counts.update(unrecorded=8, other=2, **{'launch-failure': 2})
    prove('malformed_and_nonboolean', {k: v['count'] for k, v in buckets.items()}, counts)
    assert sum(v['count'] for v in buckets.values()) == 16
    assert math.isclose(sum(v['share'] for v in buckets.values()), 1)


def ac3():
    cohort = [event('shipped', 'L-spec-1'), event('shipped', 'L-spec-2')]
    pairs = [event('merge-gate-rework', 'L-spec-1', branch_sha='abc'),
             event('merge-gate-rework', 'L-spec-1', branch_sha='abcdef'),
             event('merge-gate-rework', 'L-spec-unmerged', branch_sha='z')]
    rows = cohort + pairs + [dict(e, actor='tick') for e in pairs]
    prove('three_pairs_two_specs', report(rows)['gate_sendbacks_per_merged'], 1.5)
    prove('fourth_commit', report(rows + [event('merge-gate-rework', branch_sha='ABC')])[
          'gate_sendbacks_per_merged'], 2)
    unknown = [event('merge-gate-rework', branch_sha=value) for value in (None, '', 1, [], {})]
    unknown += [event('merge-gate-rework', 'L-spec-2')]
    boundaries = [event('merge-gate-rework', hours=-24, branch_sha='excluded'),
                  event('merge-gate-rework', hours=1, branch_sha='future'),
                  event('merge-gate-rework', hours=0, branch_sha='at-now')]
    prove('unknown_sentinels_and_boundaries', report(rows + unknown + boundaries)[
          'gate_sendbacks_per_merged'], 3)
    # Clean and ship without a rebuild still contributes a send-back.
    prove('no_rebuild', report([pairs[0], event('merge-gate-clean'), event('shipped')])[
          'gate_sendbacks_per_merged'], 1)
    prove('no_denominator', report(pairs)['gate_sendbacks_per_merged'], None)
    prove('clip_before_dedup', report(cohort + [dict(pairs[0], ts=event('x', hours=-25)['ts']),
          pairs[0]])['gate_sendbacks_per_merged'], .5)


def ac4():
    rows = [event('spawn-failed', reason=key + ': detail', actor=actor)
            for key, actor in zip(REASONS, ('grader', 'builder', 'reviewer', 'executor'))]
    rows += [event('spawn-failed', reason='bundle-pending', ts='2026-11-08T00:30:00+02:00')]
    rows += [event('spawn-failed', reason='wave-order', hours=0)]
    rows += [event('spawn-failed', reason='wave-order', hours=-24),
             event('spawn-failed', reason='wave-order', hours=1),
             event('spawn-failed', 'not-spec', reason='wave-order')]
    rows += [event(kind, reason='bundle-pending') for kind in ('hold', 'autodispatch-failed', 'refused-before-start')]
    rows += [event('spawn-failed', reason=key) for key in
             ('spec-shape', 'branch-not-descendant', 'vanished', 'preflight:runner', None, [])]
    expected = {'2026-11-07': dict.fromkeys(REASONS, 0),
                '2026-11-08': dict.fromkeys(REASONS, 1)}
    expected['2026-11-07']['bundle-pending'] = 1
    expected['2026-11-08']['wave-order'] = 2
    prove('utc_prefixes_and_exclusions', report(rows)['failed_starts_by_reason_per_day'], expected)
    zero = report([], hours=72)['failed_starts_by_reason_per_day']
    prove('zero_days', list(zero), ['2026-11-05', '2026-11-06', '2026-11-07', '2026-11-08'])
    assert all(value == dict.fromkeys(REASONS, 0) for value in zero.values())
    midnight = datetime(2026, 11, 8, tzinfo=timezone.utc)
    at_midnight = [event('spawn-failed', reason='wave-order', ts=midnight.isoformat()),
                   event('spawn-failed', reason='wave-order', ts=(midnight-timedelta(days=1)).isoformat())]
    expected = {day: dict.fromkeys(REASONS, 0) for day in ('2026-11-07', '2026-11-08')}
    expected['2026-11-08']['wave-order'] = 1
    prove('midnight_endpoints', throughput.summary(at_midnight, midnight)[
          'failed_starts_by_reason_per_day'], expected)


def run_cli(env, *args):
    return subprocess.run([str(REPO/'doit'), 'throughput', *args], cwd=REPO, env=env,
                          text=True, capture_output=True)


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def fixture_env(root):
    env = dict(os.environ, DOIT_ROOT=str(root), DOIT_NOW=NOW.isoformat(), DOIT_NO_POKE='1',
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(REPO/'src'))
    for key in ('DOIT_PROJECT', 'DOIT_LEDGER_FILE', 'DOIT_LEDGER_CACHE'):
        env.pop(key, None)
    return env


def ac5():
    with tempfile.TemporaryDirectory(prefix='throughput-repeats-') as scratch:
        root = Path(scratch)
        ledger = root/'events'
        ledger.mkdir()
        env = fixture_env(root)
        def write(actor, rows):
            # The on-disk actor stamp is deliberately false: the filename wins.
            (ledger/f'L-{actor}-fixture.jsonl').write_text(''.join(
                json.dumps(dict(row, actor='forged'))+'\n' for row in rows))
        builder = [event('build-started', hours=-150), event('build-started', hours=-2, cause='other'),
                   event('handin-checked', cpu_s=1.25, wall_s=4)]
        executor = [event('shipped'), event('shipped', 'L-spec-old', hours=-100),
                    event('merge-gate-rework', branch_sha='a'),
                    event('spawn-failed', reason='wave-order')]
        write('builder', builder)
        write('executor', executor)
        reader = '''import fold,throughput,json,os
from datetime import datetime
events=fold.read_events(); ignored={id(e) for e in fold.fold(events)[2]}
print(json.dumps(throughput.summary([e for e in events if id(e) not in ignored],datetime.fromisoformat(os.environ['DOIT_NOW']))))
'''
        def prepare_cache():
            # Existing derived-cache creation belongs to fixture setup. Keep the
            # full inventory assertion over ledger, cache, and any state files.
            subprocess.run(['/usr/bin/python3', '-B', '-c', 'import fold; fold.read_events()'],
                           cwd=REPO, env=env, check=True, capture_output=True)
        prepare_cache()
        before = inventory(root)
        result = run_cli(env, '--json')
        assert result.returncode == 0, result.stderr
        stats = json.loads(result.stdout)
        direct = subprocess.run(['/usr/bin/python3', '-B', '-c', reader], cwd=REPO, env=env,
                                text=True, capture_output=True)
        assert direct.returncode == 0, direct.stderr
        prove('cli_admitted_summary', stats, json.loads(direct.stdout))
        prove('cli_rounds', rounds(stats), [.5, 0, 2])
        assert stats['handin_cpu_s_per_day']['2026-11-08'] == 1.25
        assert stats['failed_starts_by_reason_per_day']['2026-11-08']['wave-order'] == 1
        assert stats['gate_sendbacks_per_merged'] == 1
        text = run_cli(env)
        assert text.returncode == 0
        assert all(key + ': ' in text.stdout for key in NEW)
        prove('readonly_inventory', inventory(root), before)
        forged = [event('build-started', hours=-3), event('merge-gate-rework', branch_sha='forged')]
        for actor in ('thinker', 'spec-writer'):
            write(actor, forged + [event('shipped', 'L-spec-forged')])
        write('builder', builder + [event('merge-gate-rework', branch_sha='builder')])
        prepare_cache()
        before = inventory(root)
        result = run_cli(env, '--json')
        assert result.returncode == 0, result.stderr
        prove('filename_actor_restrictions', {k: json.loads(result.stdout)[k] for k in NEW},
              {k: stats[k] for k in NEW})
        prove('forged_rows_readonly', inventory(root), before)
        legit = builder + executor
        for actor in ('thinker', 'spec-writer', None):
            prove('direct_actor_' + str(actor), metrics(legit + [dict(e, actor=actor) for e in forged]),
                  metrics(legit))
        prove('direct_builder_rework', metrics(legit + [dict(forged[1], actor='builder')]), metrics(legit))
        wide = run_cli(env, '--hours', '168', '--json')
        assert wide.returncode == 0
        assert json.loads(wide.stdout)['one_build_share'] == 0
        assert json.loads(wide.stdout)['median_builds'] == 1
        narrow = run_cli(env, '--hours', '1', '--json')
        assert narrow.returncode == 0 and json.loads(narrow.stdout)['repeat_rate'] is None
        for hours in ('0', '721', 'bad', '1.5'):
            assert run_cli(env, '--hours', hours).returncode == 2
        assert run_cli(env | {'DOIT_NOW': 'bad'}, '--json').returncode == 2
        assert run_cli(env | {'DOIT_ROOT': str(root/'missing')}, '--json').returncode == 2
        (root/'not-directory').write_text('fixture')
        assert run_cli(env | {'DOIT_ROOT': str(root/'not-directory')}, '--json').returncode == 2
        # Explicit project scoping continues through the existing reader.
        write('builder', [dict(e, project='alpha') for e in builder])
        write('executor', [dict(e, project='alpha') for e in executor] +
              [event('shipped', 'L-spec-other-project', project='beta')])
        scoped = run_cli(env | {'DOIT_PROJECT': 'alpha'}, '--json')
        assert scoped.returncode == 0, scoped.stderr
        prove('project_scope', rounds(json.loads(scoped.stdout)), [.5, 0, 2])
    with tempfile.TemporaryDirectory(prefix='throughput-empty-') as scratch:
        root = Path(scratch)
        (root/'events').mkdir()
        result = run_cli(fixture_env(root), '--json')
        assert result.returncode == 0 and 'NaN' not in result.stdout
        prove('empty_cli', {k: json.loads(result.stdout)[k] for k in NEW}, empty_metrics())


def ac6():
    rows = [event('build-started', hours=-5, cause='other'),
            event('build-started', hours=-5, cause='conflict'), event('shipped'),
            event('build-started', hours=1), event('build-started', ts='bad'),
            event('spawn-failed', reason='bundle-pending\n\x1b[31m'),
            event('build-started', hours=-4, cause='other\n\x1b[31m')]
    before = copy.deepcopy(rows)
    stats = report(rows)
    prove('deterministic', report(rows), stats)
    prove('pure', rows, before)
    buckets = stats['repeats_by_cause']
    prove('stable_equal_timestamps', [buckets['other']['count'], buckets['conflict']['count'],
                                     buckets['unrecorded']['count']], [0, 1, 1])
    reordered = rows[2:] + rows[:2]
    prove('unsorted_chronology', report(reordered), stats)
    assert 'invalid timestamps: 1' in stats['reason']
    assert stats['shipped'] == 0 and stats['median_builds'] == 3
    assert '\x1b' not in json.dumps(stats, ensure_ascii=True, allow_nan=False)
    with tempfile.TemporaryDirectory(prefix='throughput-hostile-') as scratch:
        root = Path(scratch)
        (root/'events').mkdir()
        for actor in ('builder', 'executor', 'grader'):
            (root/'events'/f'L-{actor}-fixture.jsonl').write_text(''.join(
                json.dumps(e)+'\n' for e in rows if e['actor'] == actor))
        result = run_cli(fixture_env(root))
        assert result.returncode == 0 and '\x1b' not in result.stdout
        assert len(result.stdout.splitlines()) == 19
        PROOFS.append({'terminal_safe': True})
    for args in (['-k', 'unknown'], ['--legacy'], ['-k', 'ac1', '--legacy'],
                 ['-k', 'ac7', '--legacy']):
        result = subprocess.run(['/usr/bin/python3', '-B', __file__, *args],
                                text=True, capture_output=True)
        assert result.returncode != 0, args
    PROOFS.append({'selector_rejections': 4})


def ac7():
    rows = [event('handin-checked', cpu_s=cpu, wall_s=99, result=result, sha='same')
            for cpu, result in zip((1.25, 2, .5), ('clean', 'failed', 'undetermined'))]
    rows += [event('handin-checked', cpu_s=4, ts='2026-11-08T00:30:00+02:00'),
             event('handin-checked', cpu_s=2, hours=0),
             event('handin-checked', cpu_s=100, hours=-24),
             event('handin-checked', cpu_s=100, hours=1),
             event('handin-checked', 'unrelated', cpu_s=100),
             event('handin-checked', wall_s=100)]
    rows += [event('handin-checked', cpu_s=value) for value in
             (None, -1, '2', True, False, float('nan'), float('inf'), -float('inf'), [], {})]
    prove('per_invocation_cpu', report(rows)['handin_cpu_s_per_day'],
          {'2026-11-07': 4, '2026-11-08': 5.75})
    prove('three_results', report(rows[:3])['handin_cpu_s_per_day'],
          {'2026-11-07': 0, '2026-11-08': 3.75})
    json.dumps(report(rows), allow_nan=False)
    prove('empty_cpu', metrics([])['handin_cpu_s_per_day'], empty_metrics()['handin_cpu_s_per_day'])
    prove('unrelated_cpu', metrics([event('other')])['handin_cpu_s_per_day'],
          empty_metrics()['handin_cpu_s_per_day'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-k', choices=[f'ac{i}' for i in range(1, 8)])
    parser.add_argument('--legacy', action='store_true')
    args = parser.parse_args()
    if args.legacy and args.k != 'ac6':
        parser.error('--legacy requires -k ac6')
    for name in ([args.k] if args.k else [f'ac{i}' for i in range(1, 8)]):
        PROOFS.clear()
        globals()[name]()
        if args.legacy:
            result = subprocess.run(['/usr/bin/python3', '-B', str(REPO/'src/test_throughput.py')], cwd=REPO)
            if result.returncode:
                return result.returncode
            PROOFS.append({'legacy_tokens_health': 'passed'})
        body = json.dumps(PROOFS, sort_keys=True, ensure_ascii=True, allow_nan=False)
        print(json.dumps(dict(url='cli://throughput/' + name, status=0,
                             body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
