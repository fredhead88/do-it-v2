"""Bounded, scratch-only regression driver for Realm's four child readers."""
import ast
import contextlib
import datetime as dt
import fcntl
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest import mock


SRC = pathlib.Path(__file__).resolve().parent
NOW = dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc)
SITES = ('fires_register', 'tick_omens', 'throughput_payload', 'derive_dossier')
DEGRADED = 'WARN | shelf | shelf check unavailable (dispatcher error)'


def regression_failures(realm):
    """Nonzero empty output must never overwrite an unavailable read with success."""
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 75, '', 'locked')):
        assert realm.fires_register() is None
        assert realm._FIRES['v'] is None
        before = dict(realm._OMENS_SCRIPT)
        assert realm.tick_omens() is None
        assert realm._OMENS_SCRIPT == before


@contextlib.contextmanager
def fixture(realm, sandbox):
    with tempfile.TemporaryDirectory(dir=sandbox) as folder, contextlib.ExitStack() as stack:
        root = pathlib.Path(folder)
        events = root / 'events'
        events.mkdir()
        actor = events / 'L-executor-fixture.jsonl'
        actor.write_text(json.dumps(dict(type='tick', ts=NOW.isoformat(), subject='one')) + '\n')
        for name in ('fires.py', 'omens.py'):
            (root / name).touch()
        stack.enter_context(mock.patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_NOW=NOW.isoformat()))
        stack.enter_context(mock.patch.multiple(realm, ROOT=root, EVENTS=events, BRIEF=SRC/'brief.py',
                                               LIVE_REPO=SRC.parent, HUB=realm.Hub()))
        stack.enter_context(mock.patch.dict(realm._FIRES, at=0, v=None))
        stack.enter_context(mock.patch.dict(realm._OMENS_SCRIPT, at=None, lines=[]))
        yield root, actor


def calls(realm):
    return (realm.fires_register, realm.tick_omens, lambda: realm.throughput_payload(cached=False),
            lambda: realm.derive_dossier('L-spec-1'))


def environment_matrix(realm, existing):
    for cache in (None, '1', '0'):
        for wait in (None, '0', '999'):
            with mock.patch.dict(os.environ):
                for key, value in [('DOIT_READ_CACHE', cache), ('DOIT_LEDGER_LOCK_WAIT_S', wait)]:
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')) as run:
                    for name, call, timeout in zip(SITES, calls(realm), (180, 180, 60, 120)):
                        realm._FIRES.update(at=0, v=None)
                        call()
                        kw = run.call_args.kwargs
                        env = kw['env']
                        assert env.get('DOIT_READ_CACHE') == cache, name
                        assert env['DOIT_ROOT'] == str(realm.ROOT)
                        assert env['DOIT_LEDGER_LOCK_WAIT_S'] == (wait if wait is not None else '30')
                        assert kw['timeout'] == timeout
                        assert kw['capture_output'] and kw['text']
                        if name == 'throughput_payload':
                            assert run.call_args.args[0] == [str(SRC.parent/'doit'), 'throughput', '--json']
                            assert kw['cwd'] == SRC.parent and kw['stdin'] == subprocess.DEVNULL
                            assert dt.datetime.fromisoformat(env['DOIT_NOW']).tzinfo is not None
                        else:
                            assert env['DOIT_NOW'] == NOW.isoformat()
                    realm.ask_thinker('stuck', [])
                    assert run.call_args.kwargs['env'].get('DOIT_READ_CACHE') == cache
                    assert run.call_args.kwargs['env'].get('DOIT_LEDGER_LOCK_WAIT_S') == wait
    existing.test_subprocess_sites_enumerated_and_cached_child_reads()
    print('four-site environment matrix: absent/enabled/disabled; default/explicit wait; selected inventory passed')


def helper_source(kind, trace):
    """Spy inside the real tiny child: use production cache and parser, never user scripts."""
    return f'''import atexit, datetime, json, os, pathlib, sys
sys.path.insert(0, {str(SRC)!r})
import fold, ledger_cache
assert pathlib.Path(fold.__file__).resolve() == pathlib.Path({str(SRC/'fold.py')!r})
counts = {{'read': 0, 'generation': 0, 'parse': 0, 'refresh': 0, 'uncached': 0}}
def spy(module, name, key):
    original = getattr(module, name)
    def wrapped(*args, **kwargs):
        counts[key] += 1
        return original(*args, **kwargs)
    setattr(module, name, wrapped)
spy(ledger_cache, 'read', 'read')
spy(ledger_cache, '_generation', 'generation')
spy(ledger_cache, '_parse', 'parse')
spy(ledger_cache, '_refresh', 'refresh')
spy(fold, '_read_events_uncached', 'uncached')
def record():
    with open({str(trace)!r}, 'a') as out:
        out.write(json.dumps(counts)+'\\n')
atexit.register(record)
try:
    events = fold.read_events()
except ledger_cache.LockTimeout:
    # Exit normally through atexit so the no-retry evidence survives the failure.
    sys.exit(75)
clock = os.environ.get('DOIT_NOW', datetime.datetime.now(datetime.timezone.utc).isoformat())
n = len(events)
if {kind!r} == 'fires':
    print(json.dumps([{{'class': 'ledger-read', 'n': n, 'last': clock}}]))
else:
    print('info|ledger-read|'+str(n)+':'+clock)
'''


def real_cache_and_clocks(realm, root, actor):
    trace = root/'reads.jsonl'
    for kind in ('fires', 'omens'):
        (root/(kind+'.py')).write_text(helper_source(kind, trace))
    def rows():
        return [json.loads(line) for line in trace.read_text().splitlines()]
    with mock.patch.dict(os.environ):
        os.environ.pop('DOIT_READ_CACHE', None)
        os.environ.pop('DOIT_LEDGER_LOCK_WAIT_S', None)
        with mock.patch.object(realm.time, 'monotonic', return_value=1000) as clock:
            first = realm.fires_register()
            assert first[0]['n'] == 1 and first[0]['last'] == NOW.isoformat()
            assert rows()[-1]['read'] == 1 and rows()[-1]['refresh'] == 1
            assert list((root/'cache').glob('ledger-*.sqlite3'))
            directory_stamp = actor.parent.stat().st_mtime_ns
            with actor.open('a') as out:
                out.write(json.dumps(dict(type='tick', ts=NOW.isoformat(), subject='two'))+'\n')
            assert actor.parent.stat().st_mtime_ns == directory_stamp
            clock.return_value = 1599
            assert realm.fires_register() is first and len(rows()) == 1
            clock.return_value = 1600
            second = realm.fires_register()
            assert second[0]['n'] == 2 and len(rows()) == 2
            assert rows()[-1]['refresh'] == 1
            clock.return_value = 2200
            os.environ['DOIT_NOW'] = (NOW+dt.timedelta(minutes=10)).isoformat()
            third = realm.fires_register()
            assert third[0]['n'] == 2 and third[0]['last'] != first[0]['last']
            assert rows()[-1]['parse'] == rows()[-1]['refresh'] == 0
        for offset, expected in ((20, 2), (30, 3), (40, 3)):
            if offset == 30:
                directory_stamp = actor.parent.stat().st_mtime_ns
                with actor.open('a') as out:
                    out.write(json.dumps(dict(type='tick', ts=NOW.isoformat(), subject='three'))+'\n')
                assert actor.parent.stat().st_mtime_ns == directory_stamp
            if offset == 40:
                os.environ['DOIT_READ_CACHE'] = '1'
            os.environ['DOIT_NOW'] = (NOW+dt.timedelta(minutes=offset)).isoformat()
            assert realm.tick_omens()['lines'][0]['detail'] == str(expected)+':'+os.environ['DOIT_NOW']
            assert rows()[-1]['read'] == 1 and rows()[-1]['generation'] >= 1
            if offset == 30:
                assert rows()[-1]['refresh'] == 1
            else:
                assert rows()[-1]['parse'] == rows()[-1]['refresh'] == 0
        assert all(row['read'] == 1 and row['generation'] >= 1 and row['uncached'] == 0 for row in rows())
        # The held lock is immediate, with production cache/fold and no uncached retry.
        os.environ['DOIT_LEDGER_LOCK_WAIT_S'] = '0'
        lock, = (root/'cache').glob('ledger-*.lock')
        with lock.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            realm._FIRES.update(at=0, v=None)
            before = len(rows())
            assert realm.fires_register() is None
            old = dict(realm._OMENS_SCRIPT)
            assert realm.tick_omens() is None and realm._OMENS_SCRIPT == old
            failed = rows()[before:]
            assert len(failed) == 2
            assert all(row['read'] == 1 and row['uncached'] == row['refresh'] == 0 for row in failed)
    print('real cache: initial populate, unchanged reuse, same-file append, 599/600s memo, current clocks; immediate held-lock no retry')


def adapter_clocks(realm):
    with mock.patch.object(realm.dt, 'datetime', wraps=dt.datetime) as clock:
        clock.now.return_value = NOW
        with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"clock":1}', '')) as run:
            first = realm.throughput_payload(cached=False)
            assert first['throughput'] == {'clock': 1} and run.call_count == 1
            clock.now.return_value = NOW+dt.timedelta(seconds=599)
            assert realm.throughput_payload(cached=True) is first and run.call_count == 1
            clock.now.return_value = NOW+dt.timedelta(seconds=600)
            realm.throughput_payload(cached=True)
            assert run.call_count == 2
            clock.now.return_value = NOW+dt.timedelta(seconds=601)
            realm.throughput_payload(cached=False)
            assert run.call_count == 3
            assert run.call_args.kwargs['env']['DOIT_NOW'] == clock.now.return_value.isoformat()
            assert run.call_args.kwargs['env'].get('DOIT_READ_CACHE') != '0'
            for _ in range(2):
                assert realm.derive_dossier('L-spec-1') == {'clock': 1}
            assert run.call_count == 5
    print('adapter clocks: throughput TTL/fresh evaluation and dossier per-request reads preserved')


def failure_matrix(realm):
    regression_failures(realm)
    for failure in (subprocess.CompletedProcess([], 75, '', 'lock timeout'),
                    subprocess.TimeoutExpired(['fixture'], 0), OSError('fixture')):
        kw = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
        with mock.patch.object(realm.subprocess, 'run', **kw) as run:
            for name, call in zip(SITES, calls(realm)):
                realm._FIRES.update(at=0, v=[{'cls': 'last good'}])
                realm.HUB.throughput = {'throughput': {'old': True}, 'throughput_at': NOW.isoformat()}
                before = dict(realm._OMENS_SCRIPT)
                count = run.call_count
                value = call()
                assert run.call_count == count+1, name
                if name == 'throughput_payload':
                    assert value['throughput'] is None and value['throughput_error']
                else:
                    assert value is None, name
                if name == 'fires_register':
                    assert realm._FIRES['v'] is None
                assert realm._OMENS_SCRIPT == before
    for output in ('not-json', '{}'):
        with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
            realm._FIRES.update(at=0, v=None)
            assert realm.fires_register() is None
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, DEGRADED, '')):
        old = dict(realm._OMENS_SCRIPT)
        assert realm.tick_omens() is None and realm._OMENS_SCRIPT == old
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'info|shelf|shelf check unavailable (dispatcher error) elsewhere', '')):
        assert realm.tick_omens()['lines'][0]['sev'] == 'info'  # exact tuple, not substring
    for empty in ('', '[]'):
        with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, empty, '')):
            realm._FIRES.update(at=0, v=None)
            assert realm.fires_register() == []
            assert realm.tick_omens()['lines'] == []
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'malformed omen line', '')):
        assert realm.tick_omens()['lines'] == []  # retain the helper's existing ignored-line behavior
    for value in ('not-json', '[]'):
        with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, value, '')):
            assert realm.throughput_payload()['throughput'] is None
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'not-json', '')):
        assert realm.derive_dossier('L-spec-1') is None
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', '')) as run:
        assert realm.derive_dossier('L-spec-unknown') == {'error': 'unknown'}
        n = run.call_count
        for item in ('', None, '--help'):
            assert realm.derive_dossier(item) == {'error': 'invalid'}
        assert run.call_count == n
    with mock.patch.object(realm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '{}', '')):
        assert realm.derive_dossier('L-spec-1') is None
    with mock.patch.object(realm, 'BRIEF', realm.ROOT/'missing.py'), mock.patch.object(realm.subprocess, 'run') as run:
        assert realm.derive_dossier('L-spec-1') is None
        run.assert_not_called()
    print('failure matrix: nonzero/timeout/OS error, malformed JSON, exact degraded omen, empty success, unknown/invalid IDs')


def source_contract():
    repo = SRC.parent
    base = subprocess.check_output(['git', 'merge-base', 'HEAD', 'main'], cwd=repo, text=True).strip()
    def source_at(path):
        return subprocess.check_output(['git', 'show', base+':'+path], cwd=repo, text=True)
    def definitions(source):
        return {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(source).body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    old = definitions(source_at('src/realm.py'))
    current_source = (SRC/'realm.py').read_text()
    new = definitions(current_source)
    assert {name for name in old if old[name] != new[name]} == set(SITES)
    assert set(new)-set(old) == {'_ledger_child_env'}
    for node in ast.parse(current_source).body:
        if isinstance(node, ast.FunctionDef) and node.name in SITES:
            text = ast.get_source_segment(current_source, node)
            assert '"DOIT_READ_CACHE": "0"' not in text
            assert text.count('subprocess.run(') == 1 and '_ledger_child_env()' in text
    old_tests = definitions(source_at('src/test_realm.py'))
    new_tests = definitions((SRC/'test_realm.py').read_text())
    assert old_tests['test_realm_never_opens_ledger_lock'] == new_tests['test_realm_never_opens_ledger_lock']
    removed = set(old_tests)-set(new_tests)
    added = set(new_tests)-set(old_tests)
    assert removed == {'test_subprocess_sites_enumerated_and_lock_free'}
    assert added == {'test_subprocess_sites_enumerated_and_cached_child_reads'}
    assert all(old_tests[name] == new_tests[name] for name in old_tests.keys() & new_tests.keys())
    print('source contract: exactly four adapters; parent reader, append and unrelated tests unchanged')


def marker(number):
    print(f'L-spec-1729 AC{number} ok')


def main():
    # Configure all roots before importing repository modules, including the existing test definitions.
    with tempfile.TemporaryDirectory(prefix='realm-read-cache-') as sandbox, mock.patch.dict(os.environ):
        root = pathlib.Path(sandbox)/'root'
        os.environ.update(DOIT_ROOT=str(root), DOIT_LIVE_REPO=str(SRC.parent),
                          DOIT_SESSIONS_DIR=str(root/'sessions'),
                          V4_LEDGER_DIR=str(root/'v4-ledger'), V4_INBOX_DIR=str(root/'v4-inbox'))
        for key in ('DOIT_PROJECT', 'DOIT_READ_CACHE', 'DOIT_LEDGER_LOCK_WAIT_S'):
            os.environ.pop(key, None)
        import realm
        # test_realm's import creates a root; keep it under our managed scratch directory.
        imported_root = pathlib.Path(sandbox)/'existing-test'
        imported_root.mkdir()
        with mock.patch.object(tempfile, 'mkdtemp', return_value=str(imported_root)):
            existing = importlib.import_module('test_realm')
        with fixture(realm, sandbox):
            environment_matrix(realm, existing)
        marker(1)
        with fixture(realm, sandbox) as (root, actor):
            real_cache_and_clocks(realm, root, actor)
            adapter_clocks(realm)
        marker(2)
        with fixture(realm, sandbox):
            failure_matrix(realm)
        marker(3)
        source_contract()
        marker(4)


if __name__ == '__main__':
    main()
