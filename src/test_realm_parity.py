"""Hermetic parity regressions; selectable ACs, no live ledger or network."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/realm_parity.sh'
STAGES = ('idea', 'planned', 'building', 'checking', 'merging', 'live')
STEP = dict(item='L-spec-9001', item_kind='spec', stage='building', state='hold',
            action=None, owner='operator', trigger=None, reason='capability unavailable',
            clears='capability restored', since='2026-10-23T12:00:00Z')
PAYLOAD = dict(counts=dict.fromkeys(STAGES, 0) | {'building': 1, 'live': 3}, steps=[STEP])
REALM = '''import json, os
from pathlib import Path
def flow_view(steps, *, live_count=None):
    assert Path(os.environ['DOIT_ROOT'], 'content').is_dir()
    stages = dict.fromkeys(('idea','planned','building','checking','merging','live'), 0)
    for step in steps:
        stages[step['stage']] += 1
    stages['live'] = live_count
    stuck = [dict(s) for s in steps if s['state'] == 'hold']
    # MUTATION
    return dict(stages=stages, stuck=stuck)
'''
DOIT = '''#!/usr/bin/python3
import json, os
from pathlib import Path
import sys
assert sys.argv[1:] == ['next', '--json']
root = Path(os.environ['DOIT_ROOT'])
assert root != Path(os.environ['SOURCE'])
assert (root / 'events/L-operator-fixture.jsonl').is_file()
with Path(os.environ['CALLS']).open('a') as stream:
    stream.write(str(root) + '\\n')
payload = json.loads(os.environ['PAYLOAD'])
# MUTATION
print(json.dumps(payload))
'''


def digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='realm-parity-test-') as name:
        base = Path(name)
        for part in ['source/events', 'source/content', 'repo', 'realm', 'tmp', 'home', 'hooks']:
            (base / part).mkdir(parents=True)
        # Small recorded ledger, always written locally, never read from ~/.do-it.
        (base / 'source/events/L-operator-fixture.jsonl').write_text(
            '{"type":"capability-hold","subject":"grader:live_db",'
            '"capability":"live_db","ts":"2026-10-23T12:00:00Z"}\n')
        (base / 'source/content/L-spec-9001.md').write_text('AC1 [observed-data]: live_db\n')
        (base / 'repo/doit').write_text(DOIT)
        (base / 'repo/doit').chmod(0o755)
        (base / 'realm/realm.py').write_text(REALM)
        env = dict(os.environ, DOIT_LIVE_REPO=str(base / 'repo'),
                   REALM_PARITY_SRC=str(base / 'realm'), TMPDIR=str(base / 'tmp'),
                   HOME=str(base / 'home'), SOURCE=str(base / 'source'),
                   CALLS=str(base / 'calls'), PAYLOAD=json.dumps(PAYLOAD),
                   PYTHONPATH=str(base / 'hooks'), PYTHONDONTWRITEBYTECODE='1')
        yield base, env


def run(base, env, script=SCRIPT, events=None):
    result = subprocess.run([str(script), str(events or base / 'source/events')],
                            env=env, text=True, capture_output=True, timeout=15)
    assert not list((base / 'tmp').iterdir()), 'snapshot leaked'
    assert not list((base / 'home').iterdir()), 'wrote under HOME'
    return result


def expect(result, code, *lines):
    assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
    for line in lines:
        assert line in result.stdout.splitlines(), (line, result.stdout, result.stderr)


def realm_mutation(base, code):
    (base / 'realm/realm.py').write_text(REALM.replace('# MUTATION', code))


def doit_mutation(base, code):
    (base / 'repo/doit').write_text(DOIT.replace('# MUTATION', code))


def hook(base, code):
    (base / 'hooks/sitecustomize.py').write_text(code)


def test_ac1_equal_six_stage_table():
    with fixture() as (base, env):
        before = digest(base / 'source')
        result = run(base, env)
        expect(result, 0, *[f'stage={s} doit={PAYLOAD["counts"][s]} realm={PAYLOAD["counts"][s]} ok'
                            for s in STAGES])
        assert [l.split()[0] for l in result.stdout.splitlines() if l.startswith('stage=')] == [f'stage={s}' for s in STAGES]
        assert digest(base / 'source') == before


def test_ac2_stage_and_every_stuck_field():
    with fixture() as (base, env):
        realm_mutation(base, "stages['building'] -= 1")
        expect(run(base, env), 1, 'DIFF stage=building doit=1 realm=0')
        for field, value in dict(item='L-spec-9002', item_kind='attempt', stage='checking',
                                 state='action', action='build', owner='tick', trigger='ready',
                                 reason='other', clears='other', since='2026-10-24T00:00:00Z').items():
            realm_mutation(base, f'stuck[0][{field!r}] = {value!r}')
            expect(run(base, env), 1, f'DIFF stuck item={STEP["item"]} field={field}')
        realm_mutation(base, 'stuck.clear()')
        expect(run(base, env), 1, f'DIFF stuck item={STEP["item"]} field=missing')
        realm_mutation(base, "stuck.append(dict(stuck[0], item='extra'))")
        expect(run(base, env), 1, 'DIFF stuck item=extra field=missing')


def test_ac3_undetermined_fault_matrix():
    messages = set()
    def fault(setup):
        with fixture() as (base, env):
            setup(base, env)
            result = run(base, env)
            assert result.returncode == 2, (result.stdout, result.stderr)
            lines = result.stdout.splitlines()
            assert len(lines) == 1 and lines[0].startswith('UNDETERMINED: '), lines
            assert lines[0] not in messages, lines
            messages.add(lines[0])
    fault(lambda b, e: (b / 'source/events').rename(b / 'source/absent'))
    fault(lambda b, e: (b / 'source/events').chmod(0))
    fault(lambda b, e: (b / 'source/events/L-operator-fixture.jsonl').unlink())
    fault(lambda b, e: (b / 'source/content').rename(b / 'source/absent'))
    fault(lambda b, e: doit_mutation(b, 'sys.exit(7)'))
    fault(lambda b, e: hook(b, '''import subprocess
original = subprocess.run
def run(*a, **kw):
    assert kw['timeout'] == 60
    raise subprocess.TimeoutExpired(a[0], 60)
subprocess.run = run
'''))
    fault(lambda b, e: doit_mutation(b, "print('not json'); sys.exit(0)"))
    payloads = [[], {k: v for k, v in PAYLOAD.items() if k != 'counts'},
                dict(PAYLOAD, counts=[]), dict(PAYLOAD, counts={}),
                dict(PAYLOAD, counts=PAYLOAD['counts'] | {'extra': 0}),
                dict(PAYLOAD, counts=PAYLOAD['counts'] | {'live': True}),
                dict(PAYLOAD, counts=PAYLOAD['counts'] | {'live': None}),
                {'counts': PAYLOAD['counts']}, dict(PAYLOAD, steps={})]
    for field in STEP:
        p = deepcopy(PAYLOAD)
        del p['steps'][0][field]
        payloads.append(p)
        p = deepcopy(PAYLOAD)
        p['steps'][0][field] = 123
        payloads.append(p)
    payloads.append(dict(PAYLOAD, steps=[dict(STEP, stage='invalid')]))
    payloads.append(dict(PAYLOAD, counts=PAYLOAD['counts'] | {'building': 2}))
    for payload in payloads:
        fault(lambda b, e, p=payload: e.update(PAYLOAD=json.dumps(p)))
    for mutation in ["raise RuntimeError('fixture')", 'return []', "del stages['idea']",
                     "stages['idea'] = '0'", "stages['idea'] = None",
                     "stages['live'] = None", 'stuck = {}']:
        fault(lambda b, e, m=mutation: realm_mutation(b, m))
    # The second canonical invocation must be validated independently too.
    fault(lambda b, e: doit_mutation(b, "if len(Path(os.environ['CALLS']).read_text().splitlines()) == 2: payload = []"))


def test_ac4_pinned_copy_and_churn_cleanup():
    with fixture() as (base, env):
        doit_mutation(base, "Path(os.environ['SOURCE'], 'events/new.jsonl').write_text('{}\\n')")
        before = digest(base / 'source')
        expect(run(base, env), 0)
        after = digest(base / 'source')
        assert after.pop('events/new.jsonl') == hashlib.sha256(b'{}\n').hexdigest()
        assert after == before
        roots = (base / 'calls').read_text().splitlines()
        assert len(roots) == 2 and roots[0] == roots[1]
        assert not Path(roots[0]).exists()
    with fixture() as (base, env):
        hook(base, '''import os, shutil
from pathlib import Path
original = shutil.copytree
def copy(src, dst, *args, **kw):
    result = original(src, dst, *args, **kw)
    if Path(src).name == 'events':
        p = Path(src) / 'L-operator-fixture.jsonl'
        stat = p.stat()
        os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        with Path(os.environ['SOURCE']).parent.joinpath('attempts').open('a') as f:
            f.write('copy\\n')
    return result
shutil.copytree = copy
''')
        before = digest(base / 'source')
        expect(run(base, env), 2, 'UNDETERMINED: source changed during copy')
        assert (base / 'attempts').read_text().splitlines() == ['copy'] * 3
        assert digest(base / 'source') == before


def test_ac5_live_and_independent_invocations():
    with fixture() as (base, env):
        realm_mutation(base, "stages['live'] -= 1")
        expect(run(base, env), 1, 'DIFF stage=live doit=3 realm=2')
        realm_mutation(base, 'pass')
        expect(run(base, env), 0, 'stage=live doit=3 realm=3 ok')
        assert len((base / 'calls').read_text().splitlines()) == 4
        doit_mutation(base, "if len(Path(os.environ['CALLS']).read_text().splitlines()) % 2 == 0: payload['counts']['live'] = 9")
        expect(run(base, env), 1, 'DIFF stage=live doit=3 realm=9')


def test_ac6_content_holds_and_withheld_copy():
    with fixture() as (base, env):
        doit_mutation(base, "held = any(json.loads(line)['type'] == 'capability-hold' for line in (root / 'events/L-operator-fixture.jsonl').read_text().splitlines()); specs = list((root / 'content').glob('L-spec-*.md')); payload['steps'] = payload['steps'] if held and specs else []; payload['counts']['building'] = len(payload['steps'])")
        expect(run(base, env), 0, 'stuck item=L-spec-9001 doit=present realm=present ok')
        hook(base, '''import shutil
from pathlib import Path
original = shutil.copytree
def copy(src, dst, *a, **kw):
    if Path(src).name == 'content':
        return dst
    return original(src, dst, *a, **kw)
shutil.copytree = copy
''')
        expect(run(base, env), 2, 'UNDETERMINED: snapshot copy incomplete')


def assert_gate(base, env, script):
    expect(run(base, env, script), 0, 'stage=live doit=3 realm=3 ok')
    realm_mutation(base, "stages['live'] -= 1")
    expect(run(base, env, script), 1, 'DIFF stage=live doit=3 realm=2')


def test_ac7_assertions_reject_constant_scripts():
    with fixture() as (base, env):
        assert_gate(base, env, SCRIPT)
        for code in [0, 1]:
            standin = base / f'constant-{code}'
            standin.write_text(f'#!/bin/bash\nexit {code}\n')
            standin.chmod(0o755)
            realm_mutation(base, 'pass')
            try:
                assert_gate(base, env, standin)
            except AssertionError:
                pass
            else:
                raise AssertionError(f'constant {code} accepted')


def test_ac7_real_interfaces_on_recorded_fixture():
    with fixture() as (base, env):
        (base / 'source/events/L-operator-fixture.jsonl').write_text(json.dumps(dict(
            type='charter-filed', subject='L-charter-9001', title='Parity fixture',
            ts='2026-10-23T12:00:00Z')) + '\n')
        env.update(DOIT_LIVE_REPO=str(SCRIPT.parents[1]),
                   REALM_PARITY_SRC=str(SCRIPT.parents[1] / 'src'))
        before = digest(base / 'source')
        expect(run(base, env), 0, 'stage=idea doit=1 realm=1 ok')
        assert digest(base / 'source') == before


if __name__ == '__main__':
    prefix = 'test_' + sys.argv[1] if len(sys.argv) > 1 else 'test_'
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith(prefix) and callable(fn))
    assert tests, 'no matching tests'
    for name, fn in tests:
        fn()
        print('ok', name)
