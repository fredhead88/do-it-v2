#!/usr/bin/env python3
"""Isolated transcript parity, decision, memory and fallback regressions."""
import contextlib
import hashlib
import io
import json
import locale
import os
import pathlib
import pwd
import subprocess
import sys
import tempfile
import tracemalloc
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_TEMP = tempfile.TemporaryDirectory(prefix="transcript-scan-")
BASE = pathlib.Path(_TEMP.name)
os.environ["HOME"] = str(BASE / "home")
os.environ["DOIT_ROOT"] = str(BASE / "root")
pathlib.Path(os.environ["HOME"]).mkdir()
pathlib.Path(os.environ["DOIT_ROOT"]).mkdir()
assert pathlib.Path(os.environ["HOME"]) != pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
assert pathlib.Path(os.environ["DOIT_ROOT"]) != pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".do-it"

import fold
import look
import look_wallclock as wc
import pane_resume
import panes
import transcript_scan


def signed(ac, body):
    text = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url=f"cli://look/L-spec-1652/{ac}", status=0,
                         body_sha256=hashlib.sha256(text.encode()).hexdigest(), body_excerpt=text)))


def outcome(fn):
    try:
        return ("ok", fn())
    except Exception as exc:
        return (type(exc).__name__, None)


def parity():
    fixtures = [b'', b'\n\r\n\n', b' \n\t\n{}\n', b'bad\n[]\n"str"\n{}',
                b'{}\r\n{}\r{}', b'{"x":"\xff"}\n', b'{}\n\xe2\x82']
    for sep in '\u2028\u2029\u0085\v\f\x1c\x1d\x1e':
        fixtures.append(('{"x":"a' + sep + 'b"}\n{"ok":1}\n').encode())
    fixtures.append('{"x":"\u00e9\U0001f600"}\n{}'.encode())
    fixtures.append(('{"x":"' + 'x' * 80000 + '"}\r\n{}').encode())
    fixtures.append((' ' * 65535 + '\r\n{}\u2028{}').encode())
    for i, data in enumerate(fixtures):
        p = BASE / f'parity-{i}.jsonl'
        p.write_bytes(data)
        old = outcome(lambda: wc._parse_jsonl(p.read_text()))
        new = outcome(lambda: list(transcript_scan.iter_jsonl(p)))
        assert old == new, (i, old, new)
    assert outcome(lambda: list(transcript_scan.iter_jsonl(BASE / 'missing')))[0] == 'FileNotFoundError'
    print('encoding=' + locale.getpreferredencoding(False))
    return len(fixtures)


def ac1():
    count = parity()
    for extra in ({}, {'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}):
        env = dict(HOME=os.environ['HOME'], DOIT_ROOT=os.environ['DOIT_ROOT'], PATH='/usr/bin:/bin', **extra)
        result = subprocess.run(['/usr/bin/python3', __file__, '--parity'], env=env,
                                capture_output=True, text=True, check=True)
        print(result.stdout.strip())
        if extra and 'encoding=UTF-8' in result.stdout:
            print('ASCII locale check skipped: platform still resolves UTF-8')
    signed('AC1', dict(fixtures=count, locales=3, isolation=True))
    print('L-spec-1652 AC1 ok')


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
PANE = 'L-thinker-1652'


def assistant(**kw):
    return dict(type='assistant', timestamp=NOW.isoformat(), uuid='question', **kw)


QUESTION = assistant(message={'content': [{'type': 'tool_use', 'name': 'AskUserQuestion', 'id': 'q'}]})
QUESTION['timestamp'] = (NOW - timedelta(minutes=2)).isoformat()
RESULT = dict(type='user', message={'content': [{'type': 'tool_result', 'tool_use_id': 'q'}]})
NEEDLE = dict(type='user', message={'content': [{'type': 'text', 'text': 'needle'}]})
BAD_MESSAGE = dict(type='system', message='bad')
BAD_USER = dict(type='user', message='bad')
BAD_CLASS = assistant(isApiErrorMessage=True, quotaLimits='bad')
BAD_QUOTA = dict(type='system', quotaLimits='bad')


def hit(**kw):
    return dict(type='system', subtype='informational', timestamp=(NOW - timedelta(minutes=1)).isoformat(),
                content='Usage limit reached continuing automatically at 4pm · stop', **kw)


class Runner:
    def clock(self): return 0
    def sleep(self, seconds): pass
    def ssh(self, *args): return '200'
    def git(self, *args): return ''
    def crontab_text(self, *args): return ''
    def disk_usage(self, *args): return dict(total=100, used=10, free=90)
    def gh_run_list(self, *args): return '[]'
    def tmux_capture(self, *args): return ''


def fixture(rows, *, idle=True, raw=None, state=None):
    root = pathlib.Path(tempfile.mkdtemp(dir=BASE))
    for name in ('events', 'look', 'projects/p', 'sessions', 'crond'):
        (root / name).mkdir(parents=True, exist_ok=True)
    path = root / 'projects/p/SID.jsonl'
    path.write_bytes(raw if raw is not None else ''.join(json.dumps(row) + '\n' for row in rows).encode())
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    meta = dict(name=PANE, status='idle' if idle else 'busy', sessionId='SID', tmux='fixture:1.1')
    (root / 'sessions' / f'{os.getpid()}.json').write_text(json.dumps(meta))
    (root / 'look.toml').write_text('ci = []\n[push]\nprojects = []\n')
    (root / 'crons.toml').write_text('')
    if state is not None:
        (root / 'look/state.json').write_text(json.dumps(state))
    events = [dict(type='message-sent', actor='sender', to=PANE, text='needle',
                   ts=(NOW - timedelta(minutes=3)).isoformat(), _src='L-sender-local.jsonl:1')]
    return root, path, events


@contextlib.contextmanager
def environment(root, mode):
    env = os.environ.copy()
    if mode == 'legacy':
        os.environ['DOIT_LOOK_TRANSCRIPT_READ'] = mode
    else:
        os.environ.pop('DOIT_LOOK_TRANSCRIPT_READ', None)
    with patch.object(pane_resume, 'PROJECTS', root / 'projects'), patch.object(panes, 'SESSIONS', root / 'sessions'), \
         patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', root / 'events'):
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(env)


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k not in ('ts', 'measured_at', 'last_pass')}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def run_fixture(root, events, mode):
    with environment(root, mode):
        result = look.run(events, now=NOW, root=root, runner=Runner(), toml_path=root / 'look.toml',
                          crons_manifest=root / 'crons.toml', crond_dir=root / 'crond', repo=root)
    ledger = root / 'events/L-look-local.jsonl'
    persisted = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
    state = json.loads((root / 'look/state.json').read_text())
    compared = normalize(dict(result={k: result[k] for k in ('briefs', 'answered', 'wallclock')},
                              ledger=persisted, state=state))
    assert str(root) not in json.dumps(compared)
    return compared, result


def direct(root, path, events, mode):
    outputs = []
    with environment(root, mode):
        for check in ('quota', 'message', 'menu'):
            rows, und, state, tally = [], [], {}, wc._new_tally()
            def emit(type_, kv):
                row = dict(type=type_, _src=f'fixture:{len(rows) + 1}', **kv)
                rows.append(row)
                return row
            def invoke():
                if check == 'quota':
                    wc._check_quota(events, NOW, state, False, emit, tally=tally)
                elif check == 'message':
                    wc._check_message_answered(events, emit, und.append, tally=tally)
                else:
                    look._check_pane_at_menu_claude(events, root, rows, tally=tally)
            # Brief creation is real; start menu from a fresh store in both modes.
            if check == 'menu':
                for p in (root / 'events/L-look-local.jsonl', root / 'look/state.json'):
                    p.unlink(missing_ok=True)
            status = outcome(invoke)[0]
            outputs.append(normalize([status, rows, und, state, tally]))
    return outputs


CASES = [
    ('structured', [hit(), dict(type='system', quotaLimits={'resetsAt': 'structured'})], True),
    ('parsed', [hit()], True),
    ('last-hit', [hit(quotaLimits={'resetsAt': 'old'}), hit(), dict(quotaLimits={'resetsAt': 'new'})], True),
    ('resolved', [hit(quotaLimits={'resetsAt': 'done'}), assistant()], False),
    ('unresolved', [hit(quotaLimits={'resetsAt': 'done'}), assistant(isApiErrorMessage=True,
                       quotaLimits={'status': 'rejected'})], False),
    ('reply', [NEEDLE, assistant()], False),
    ('no-reply', [NEEDLE], False),
    ('auth', [assistant(isApiErrorMessage=True, error='authentication_failed')], True),
    ('question', [QUESTION], True),
    ('answered-question', [QUESTION, RESULT], True),
    ('replaced-question', [QUESTION, BAD_MESSAGE, assistant()], True),
    ('after-result', [QUESTION, RESULT, BAD_MESSAGE], True),
    ('ignored-resets', [hit(quotaLimits={'resetsAt': 'first'}), BAD_QUOTA], False),
    ('ignored-text', [NEEDLE, BAD_USER, assistant()], False),
    ('ignored-class', [hit(quotaLimits={'resetsAt': 'first'}), assistant(), BAD_CLASS], False),
    ('bad-resets', [hit(), BAD_QUOTA, dict(quotaLimits={'resetsAt': 'late'})], False),
    ('bad-text', [BAD_USER, NEEDLE, assistant()], False),
    ('bad-class', [hit(quotaLimits={'resetsAt': 'first'}), BAD_CLASS, assistant()], False),
    ('menu-error', [QUESTION, BAD_MESSAGE], True),
    ('discard-resets', [hit(), BAD_QUOTA, hit(quotaLimits={'resetsAt': 'later'})], False),
    ('discard-question', [assistant(message='bad'), assistant()], True),
    ('decode-first', [], False), ('decode-last', [], False), ('mode000', [], False),
]


@contextlib.contextmanager
def live_fixture_pids():
    children = []
    try:
        # Actual live local fixture processes, one per session; no mocked join.
        for _ in range(15):
            children.append(subprocess.Popen(['/usr/bin/python3', '-c', 'import sys; sys.stdin.read()'],
                                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL))
        yield [child.pid for child in children]
    finally:
        for child in children:
            child.stdin.close()
            child.wait(timeout=10)


def main_fixture(pids):
    root, old_path, _ = fixture([])
    old_path.unlink()
    for session in (root / 'sessions').glob('*.json'): session.unlink()
    events, paths = [], []
    quota = {}
    for i, (name, entries, idle) in enumerate(CASES[:15]):
        sid, pane = f'SID-{i:02d}', f'L-thinker-{16520 + i}'
        path = root / f'projects/p/{sid}.jsonl'
        # A mixture of <=3-row and >=4-row transcripts exercises exact fallback counts.
        if name in ('structured', 'question', 'ignored-class'):
            entries = entries + [dict(type='system'), dict(type='system'), dict(type='system')]
        path.write_text(''.join(json.dumps(row) + '\n' for row in entries))
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        paths.append(path)
        meta = dict(name=pane, status='idle' if idle else 'busy', sessionId=sid, tmux=f'fixture:{i}.1')
        (root / 'sessions' / f'{pids[i]}.json').write_text(json.dumps(meta))
        events.append(dict(type='message-sent', actor='sender', to=pane, text='needle',
                           ts=(NOW - timedelta(minutes=3)).isoformat(), _src=f'L-sender-local.jsonl:{i + 1}'))
        if name in ('resolved', 'unresolved', 'ignored-class'):
            quota[sid] = dict(pane=pane, src=f'quota:{i}', hit_ts=None)
    (root / 'look/state.json').write_text(json.dumps({'wallclock': {'quota': quota}}))
    return root, paths, events


def make_case(case):
    name, rows, idle = case
    raw = None
    if name == 'decode-first': raw = b'\xff\n{}\n'
    if name == 'decode-last': raw = (json.dumps(QUESTION) + '\n' + json.dumps(NEEDLE) + '\n').encode() + b'\xe2\x82'
    state = None
    if name in ('bad-class', 'ignored-class', 'unresolved'):
        # Existing entry with no hit_ts exercises the optional second scan.
        state = {'wallclock': {'quota': {'SID': {'pane': PANE, 'src': 'fixture:1', 'hit_ts': None}}}}
    root, path, events = fixture(rows, idle=idle, raw=raw, state=state)
    if name == 'mode000': path.chmod(0)
    return root, path, events


def ac2():
    tested = []
    with live_fixture_pids() as pids:
        old_root, _, old_events = main_fixture(pids)
        new_root, _, new_events = main_fixture(pids)
        old, old_result = run_fixture(old_root, old_events, 'legacy')
        new, new_result = run_fixture(new_root, new_events, 'scan')
        assert old == new
        assert old_result['transcripts_read'] == new_result['transcripts_read']
        assert any(row.get('type') == 'pane-paused' for row in new['result']['wallclock'])
        assert any(row.get('type') == 'message-answered' for row in new['result']['wallclock'])
        assert any(row.get('reading', {}).get('class') == 'menu' for row in new['result']['briefs'])
    for case in CASES:
        if case[0] == 'mode000' and os.geteuid() == 0:
            print('mode 000 skipped: euid 0 can read it')
            continue
        roots = [make_case(case) for _ in range(4)]
        # An idle pane's decode/predicate errors intentionally escape run.
        if case[0] != 'menu-error':
            old, old_result = run_fixture(roots[0][0], roots[0][2], 'legacy')
            new, new_result = run_fixture(roots[1][0], roots[1][2], 'scan')
            assert old == new, (case[0], old, new)
            assert old_result['transcripts_read'] == new_result['transcripts_read']
            assert new_result['transcript_fallbacks'] == {'count': 0, 'classes': []}
            assert new_result['transcripts_skipped'] == 0
        assert direct(*roots[2], 'legacy') == direct(*roots[3], 'scan'), case[0]
        tested.append(case[0])
    for mode in ('legacy', 'scan'):
        root, path, events = fixture([BAD_USER], idle=False)
        events[0]['text'] = '   '
        with environment(root, mode):
            assert outcome(lambda: wc._check_message_answered(events, lambda *a: None,
                                                               lambda *a: None))[0] == 'ok'
    # A later decoder failure wins over a predicate recorded in an earlier chunk.
    for mode in ('legacy', 'scan'):
        root, path, events = fixture([], idle=False, raw=(json.dumps(BAD_USER) + '\n' + ' ' * 70000).encode() + b'\xff')
        with environment(root, mode):
            assert outcome(lambda: wc._check_message_answered(events, lambda *a: None,
                                                               lambda *a: None))[0] == 'UnicodeDecodeError'
    for data in (b'\xff', (json.dumps(QUESTION) + '\n').encode() + b'\xff'):
        values = []
        for mode in ('legacy', 'scan'):
            root, path, events = fixture([], raw=data)
            values.append(direct(root, path, events, mode)[2])
        assert values[0] == values[1] and values[0][0] == 'UnicodeDecodeError'
    signed('AC2', dict(cases=tested, direct_checks=3, store_roundtrip=True))
    print('L-spec-1652 AC2 ok')


def ac3():
    peaks = {}
    for check in ('quota', 'menu', 'message'):
        results, measured = [], {}
        for mode in ('scan', 'legacy'):
            root, path, events = fixture([QUESTION, NEEDLE, hit()])
            padding = json.dumps(dict(type='user', message={'content': [{'type': 'text', 'text': 'x' * 2100 + '\U0001f600'}]}), ensure_ascii=False)
            with path.open('a') as f:
                for _ in range(12000): f.write(padding + '\n')
                f.write(json.dumps(dict(type='system', quotaLimits={'resetsAt': 'end'})) + '\n')
            assert path.stat().st_size >= 24 * 1024 * 1024
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            rows, und, state = [], [], {}
            def emit(type_, kv):
                row = dict(type=type_, _src='fixture:1', **kv)
                rows.append(row)
                return row
            with environment(root, mode):
                tracemalloc.start()
                if check == 'quota': wc._check_quota(events, NOW, state, False, emit)
                elif check == 'menu': look._check_pane_at_menu_claude(events, root, rows)
                else: wc._check_message_answered(events, emit, und.append)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            measured[mode] = peak
            results.append(normalize([rows, und, state]))
        assert results[0] == results[1], check
        if check == 'quota':
            assert len(results[0][0]) == 1 and results[0][0][0]['resets_at'] == 'end'
            assert results[0][0][0]['evidence'] == 'SID.jsonl:3'
        elif check == 'menu': assert results[0][0][0]['reading'] == {'class': 'menu'}
        else: assert results[0] == [[], [], {}]
        assert measured['scan'] < 8 * 1024 * 1024, measured
        assert measured['legacy'] > 48 * 1024 * 1024, measured
        peaks[check] = measured
    signed('AC3', peaks)
    print('L-spec-1652 AC3 ok')


def fail_fourth(path):
    for i, row in enumerate(REAL_ITER(path), 1):
        if i == 4: raise RuntimeError('fixture scan failure')
        yield row


REAL_ITER = transcript_scan.iter_jsonl


def ac4():
    rows = [QUESTION, NEEDLE, hit(), dict(quotaLimits={'resetsAt': 'end'}), assistant()]
    runs = {}
    for mode in ('legacy', 'scan', 'fault', 'cap', 'import'):
        root, path, events = fixture(rows)
        calls = []
        def spy(fn):
            def wrapped(p):
                if pathlib.Path(p) == path: calls.append(str(p))
                return fn(p)
            return wrapped
        with contextlib.ExitStack() as stack:
            for module, attr in ((wc, '_read_jsonl_lines'), (wc, '_read_jsonl_lines_strict'), (look, '_read_jsonl')):
                stack.enter_context(patch.object(module, attr, spy(getattr(module, attr))))
            it = stack.enter_context(patch.object(transcript_scan, 'iter_jsonl', side_effect=fail_fourth if mode in ('fault', 'cap') else REAL_ITER))
            if mode == 'cap': stack.enter_context(patch.object(wc, 'TRANSCRIPT_FALLBACK_MAX_BYTES', 1))
            if mode == 'import': stack.enter_context(patch.dict(sys.modules, {'transcript_scan': None}))
            compared, tally = run_fixture(root, events, mode)
        reads = tally['transcripts_read']
        if mode == 'legacy':
            assert len(calls) == reads and it.call_count == 0
        elif mode == 'scan':
            assert calls == [] and tally['transcript_fallbacks']['count'] == 0
        elif mode in ('fault', 'import'):
            assert len(calls) == reads == tally['transcript_fallbacks']['count']
            assert tally['transcripts_skipped'] == 0
            assert tally['transcript_fallbacks']['classes'] == ['RuntimeError' if mode == 'fault' else 'ModuleNotFoundError']
        else:
            assert not calls and tally['transcripts_skipped'] == reads
            assert tally['transcript_fallbacks'] == {'count': 0, 'classes': ['RuntimeError']}
            assert not compared['result']['wallclock']
            assert any(b['key'] == 'undetermined:message-answered' for b in compared['result']['briefs'])
        runs[mode] = (compared, tally)
    assert runs['legacy'][0] == runs['scan'][0] == runs['fault'][0] == runs['import'][0]
    # Normal read/decode/predicate errors never invoke the legacy readers.
    for case in CASES:
        if case[0] not in ('decode-first', 'decode-last', 'bad-resets', 'bad-text', 'bad-class', 'menu-error', 'mode000'):
            continue
        if case[0] == 'mode000' and os.geteuid() == 0: continue
        root, path, events = make_case(case)
        with patch.object(wc, '_read_jsonl_lines', side_effect=AssertionError('unexpected legacy')), \
             patch.object(wc, '_read_jsonl_lines_strict', side_effect=AssertionError('unexpected legacy')), \
             patch.object(look, '_read_jsonl', side_effect=AssertionError('unexpected legacy')):
            for result in direct(root, path, events, 'scan'):
                assert result[-1]['transcript_fallbacks'] == {'count': 0, 'classes': []}
                assert result[-1]['transcripts_skipped'] == 0
                assert result[0] != 'AssertionError'
    mixed = {}
    with live_fixture_pids() as pids:
        legacy_calls = []
        for mode in ('legacy', 'scan', 'fault', 'cap', 'import'):
            root, paths, events = main_fixture(pids)
            calls = []
            def spy_main(fn):
                def wrapped(p):
                    if pathlib.Path(p) in paths: calls.append(pathlib.Path(p).name)
                    return fn(p)
                return wrapped
            with contextlib.ExitStack() as stack:
                for module, attr in ((wc, '_read_jsonl_lines'), (wc, '_read_jsonl_lines_strict'), (look, '_read_jsonl')):
                    stack.enter_context(patch.object(module, attr, spy_main(getattr(module, attr))))
                iterator = stack.enter_context(patch.object(transcript_scan, 'iter_jsonl', side_effect=fail_fourth if mode in ('fault', 'cap') else REAL_ITER))
                if mode == 'cap':
                    # Cap only the largest padded transcript; smaller failures still fall back.
                    cap = max(path.stat().st_size for path in paths) - 1
                    stack.enter_context(patch.object(wc, 'TRANSCRIPT_FALLBACK_MAX_BYTES', cap))
                if mode == 'import': stack.enter_context(patch.dict(sys.modules, {'transcript_scan': None}))
                compared, result = run_fixture(root, events, mode)
            if mode == 'legacy':
                assert len(calls) == result['transcripts_read'] and iterator.call_count == 0
                legacy_calls = calls
                baseline = compared
            if mode in ('legacy', 'scan', 'fault', 'import'): assert compared == baseline, mode
            by_name = {p.name: p for p in paths}
            raisers = [name for name in legacy_calls if len(wc._parse_jsonl(by_name[name].read_text())) >= 4]
            if mode == 'fault':
                assert calls == raisers
                assert result['transcript_fallbacks']['count'] == len(raisers)
                assert result['transcripts_skipped'] == 0
            elif mode == 'cap':
                skipped = [name for name in raisers if by_name[name].stat().st_size > cap]
                assert calls == [name for name in raisers if name not in skipped]
                assert result['transcripts_skipped'] == len(skipped) > 0
                assert result['transcript_fallbacks']['count'] == len(raisers) - len(skipped)
                assert result['transcript_fallbacks']['classes'] == ['RuntimeError']
                # Exact output equivalence to an unreadable read of the capped path.
                error_root, error_paths, error_events = main_fixture(pids)
                for p in error_paths:
                    if p.name in skipped:
                        p.unlink()
                        p.mkdir()  # stat/glob/find still reach it; read raises OSError.
                error_output, _ = run_fixture(error_root, error_events, 'legacy')
                assert compared == error_output
            elif mode == 'import':
                assert calls == legacy_calls
                assert result['transcript_fallbacks']['count'] == result['transcripts_read']
            elif mode == 'scan': assert not calls
            mixed[mode] = {k: result[k] for k in wc._new_tally()}
    signed('AC4', dict(single={mode: {k: value[1][k] for k in wc._new_tally()} for mode, value in runs.items()}, mixed=mixed))
    print('L-spec-1652 AC4 ok')


if __name__ == '__main__':
    if '--parity' in sys.argv:
        parity()
    else:
        ac1()
        ac2()
        ac3()
        ac4()
