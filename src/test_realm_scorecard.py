"""Scorecard scratch regressions; production and diff checks are explicit opt-ins."""
import builtins
import contextlib
import datetime as dt
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import realm

UTC = dt.timezone.utc
BANDS = {'green', 'amber', 'red', 'not sampled', 'no data'}


def fixture(root, now):
    """Real SD6 rows, including one unsampled day and a null gap."""
    (root / 'events').mkdir(exist_ok=True)
    (root / 'seat').mkdir(exist_ok=True)
    (root / 'state').mkdir(exist_ok=True)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events, samples = [], []
    for ago in (1, 3, 5):
        at = midnight - dt.timedelta(days=ago) + dt.timedelta(hours=1)
        for kind, hour in [('spec-written', 0), ('build-done', 1), ('shipped', 2)]:
            events.append(dict(ts=(at + dt.timedelta(hours=hour)).isoformat(), type=kind, subject=f'L-spec-{ago}'))
    for kind in ('window-opened', 'window-verified', 'window-dropped', 'build-blocked'):
        events.append(dict(ts=(now-dt.timedelta(hours=1)).isoformat(), type=kind,
                           subject='L-spec-9' if kind == 'build-blocked' else 'W-fixture',
                           window='W-fixture', specs=['L-spec-9'], reason='fixture failure'))
    for ago in range(8, -1, -1):
        if ago == 4:
            continue
        at = midnight - dt.timedelta(days=ago) + dt.timedelta(minutes=1)
        # Avoid future samples even when this is run at midnight UTC.
        if at > now:
            at = now
        for offset in (0, 1):
            ts = at + dt.timedelta(seconds=offset)
            if ts > now:
                continue
            row = dict(ts=ts.isoformat(), disk_used_pct=40+ago, disk_total_gb=100,
                       ram_used_pct=55, swap_used_pct=2, claude_5h_pct=25,
                       claude_7d_pct=10, codex_7d_pct=12, source='fixture')
            for key, window in [('claude_5h', dt.timedelta(hours=5)), ('claude_7d', dt.timedelta(days=7)), ('codex_7d', dt.timedelta(days=7))]:
                row[key+'_reset'] = (ts + window/2).isoformat()
            if ago == 2 and offset == 0:
                row.update({key: None for key in ('disk_used_pct', 'ram_used_pct', 'swap_used_pct', 'claude_5h_pct', 'claude_7d_pct', 'codex_7d_pct')})
            samples.append(row)
    (root/'events/L-builder-1.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    (root/'state/pace-samples.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in samples))
    return events, samples


@contextlib.contextmanager
def scratch():
    with tempfile.TemporaryDirectory(prefix='realm-scorecard-') as directory:
        root = Path(directory)
        now = dt.datetime.now(UTC)
        fixture(root, now)
        with patch.multiple(realm, ROOT=root, EVENTS=root/'events', SEAT=root/'seat',
                            LIVE_REPO=HERE.parent, LEDGER=realm.realm_ledger.Ledger(),
                            _SCORECARD_CACHE=None), patch.dict(os.environ, {'DOIT_ROOT': str(root), 'DOIT_LIVE_REPO': str(HERE.parent)}):
            yield root, now


@contextlib.contextmanager
def server(module=realm):
    handler = type('ScorecardHandler', (module.Handler,), {'key': None})
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def get(base, path='/api/scorecard', headers=None):
    with urllib.request.urlopen(urllib.request.Request(base+path, headers=headers or {}), timeout=30) as response:
        body = response.read()
        # Local signed response record: independently reproducible body digest.
        print(json.dumps(dict(url=base+path, status=response.status,
                              body_sha256=hashlib.sha256(body).hexdigest(),
                              body_excerpt=body[:180].decode())))
        assert response.status == 200
        return json.loads(body)


def test_ac1_scorecard_route_payload():
    with scratch() as (root, _), server() as (base, handler):
        data = get(base)
        now = dt.datetime.fromisoformat(data['generated_at'].replace('Z', '+00:00'))
        assert now.utcoffset() == dt.timedelta(0)
        assert set(data) == {'days', 'pace', 'generated_at', 'ceiling'}
        samples = realm.pace.samples(now-dt.timedelta(days=7), root=root)
        with realm.ledger_snapshot() as (_, events, __):
            expected = realm.scorecard.day_rows(events, now, days=7, samples=samples)
        assert data['days'] == [dict(row, copy_text=realm.scorecard.copy_text(row)) for row in expected]
        assert data['pace'] == samples
        assert all(dt.datetime.fromisoformat(s['ts']) >= now-dt.timedelta(days=7) for s in samples)
        assert data['ceiling'] == realm.pace.SAFE_CEILING
        handler.key = 'scratch-only-key'
        try:
            get(base)
            raise AssertionError('missing authorization accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        assert get(base, headers={'X-Realm-Key': handler.key}) == data
    print('AC1 PASS')


def test_ac2_scorecard_failures_are_shown():
    with scratch() as (root, _), server() as (base, handler):
        path = root/'state/pace-samples.jsonl'
        path.unlink()
        data = get(base)
        assert data['pace'] == [] and len(data['days']) == 7
        row = dict(ts=dt.datetime.now(UTC).isoformat(), disk_used_pct=None, claude_7d_pct=None)
        path.write_text(json.dumps(row)+'\n')
        realm._SCORECARD_CACHE = None
        original = realm.scorecard.day_rows
        with patch.object(realm.scorecard, 'day_rows', wraps=original) as wrapped:
            data = get(base)
            assert data['pace'][0]['claude_7d_pct'] is None
            assert wrapped.call_args.kwargs['samples'] == [row]
        for module, attribute in [(realm.scorecard, 'day_rows'), (realm.scorecard, 'copy_text'), (realm.pace, 'samples')]:
            realm._SCORECARD_CACHE = None
            with patch.object(module, attribute, side_effect=RuntimeError('fixture')) as raiser:
                data = get(base)
                assert data['error'] == 'scorecard unavailable (RuntimeError)' and 'days' not in data
                raiser.assert_called_once()
            realm._SCORECARD_CACHE = None
            assert len(get(base)['days']) == 7
        for module in (realm.scorecard, realm.pace):
            assert Path(module.__file__).resolve().parent == HERE
        original_import = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name == 'scorecard':
                raise ImportError('fixture blocked import')
            return original_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('realm_scorecard_import_failure', HERE/'realm.py')
        fresh = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=blocked):
            spec.loader.exec_module(fresh)
        with server(fresh) as (other, _):
            assert get(other)['error'] == 'scorecard unavailable (ImportError)'
    print('AC2 PASS')


def test_ac3_scorecard_is_cached_read_only_and_free():
    def listing(root):
        return [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob('*'))]
    with scratch() as (root, _), server() as (base, handler):
        before = listing(root)
        ledger = (root/'events/L-builder-1.jsonl').read_bytes()
        with patch.object(realm.scorecard, 'day_rows', wraps=realm.scorecard.day_rows) as calls:
            # Fixed minute avoids a timing-boundary flake.
            now = dt.datetime.now(UTC)
            with patch.object(realm, '_scorecard_now', return_value=now):
                assert get(base) == get(base)
                assert calls.call_count == 1
                path = root/'state/pace-samples.jsonl'
                original = path.stat()
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns+1))
                get(base)
                assert calls.call_count == 2
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                with patch.object(realm, 'bootstrap', return_value={}), patch.object(realm, 'glance_payload', return_value={}):
                    assert 'scorecard' not in get(base, '/api/bootstrap')
                    assert 'scorecard' not in get(base, '/api/glance')
                assert calls.call_count == 2
        after = listing(root)
        print('AC3 before', before, 'after', after)
        assert before == after and ledger == (root/'events/L-builder-1.jsonl').read_bytes()
    print('AC3 PASS')


def guarded():
    assert os.environ.get('REALM_SCORECARD_VERIFY') == '1', 'explicit verification required'


def verify_ac3_diff(base):
    guarded()
    diff = subprocess.check_output(['git', 'diff', '-U0', base, '--', 'src/realm.py'], text=True)
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    print(added)
    assert not re.search(r'subprocess|urllib|socket|http\.client|claude -p|anthropic', added)
    print('AC3 diff PASS')


def verify_ac4_real_ledger_payload():
    guarded()
    root = Path('/home/albert/.do-it')
    assert (root/'events').is_dir() and any((root/'events').glob('*.jsonl'))
    started = time.monotonic()
    with patch.multiple(realm, ROOT=root, EVENTS=root/'events', SEAT=root/'seat',
                        LEDGER=realm.realm_ledger.Ledger(), _SCORECARD_CACHE=None):
        data = realm.scorecard_payload()
    assert 'error' not in data, data
    today = dt.datetime.fromisoformat(data['generated_at'].replace('Z', '+00:00')).date()
    assert [r['date'] for r in data['days']] == [str(today-dt.timedelta(days=d)) for d in reversed(range(7))]
    for row in data['days']:
        bands = [cell['band'] for cell in row['cells']]
        assert set(bands) <= BANDS
        for cell in row['cells']:
            if cell['value'] is None:
                assert cell['band'] != 'green'
        print(row['date'], ','.join(bands))
    if not (root/'state/pace-samples.jsonl').exists():
        assert data['pace'] == []
        assert all(c['band'] == 'not sampled' for r in data['days'] for c in r['cells'] if c['key'] in ('disk_used_pct','ram_used_pct','swap_used_pct','claude_5h_pct','claude_7d','codex_7d'))
    print(f'AC4 elapsed_seconds={time.monotonic()-started:.3f}')
    print('AC4 count=7 PASS')


def test_ac6_copy_path_is_text_only():
    page = (HERE/'realm.html').read_text()
    code = page.split('async function copyScorecardDay', 1)[1].split('\n}', 1)[0]
    assert 'navigator.clipboard.writeText(row.copy_text)' in code
    assert not re.search(r'ClipboardItem|toBlob|canvas|html2', code)
    print('AC6 text-only PASS')


def test_ac7_page_pace_formula_matches_pace_pct():
    page = (HERE/'realm.html').read_text()
    function = re.search(r'function scorecardPacePct\([^\n]+', page)[0]
    reset = dt.datetime(2026, 10, 28, tzinfo=UTC)
    cases = [(hours, fraction) for hours in (5, 168) for fraction in (-.5, 0, .5, 1, 1.5)]
    args = [[(reset-dt.timedelta(hours=h)*(1-f)).timestamp()*1000, reset.timestamp()*1000, h*3600000] for h,f in cases]
    script = function+'\nconsole.log(JSON.stringify('+json.dumps(args)+'.map(a=>scorecardPacePct(...a))));'
    actual = json.loads(subprocess.check_output(['/usr/bin/node', '-e', script], text=True))
    for value, (hours, fraction) in zip(actual, cases):
        at = reset-dt.timedelta(hours=hours)*(1-fraction)
        assert abs(value-realm.pace.pace_pct(reset, dt.timedelta(hours=hours), at)) < .01
    print('AC7 formula: 10 cases PASS')


def test_ac9_no_unescaped_payload_in_innerhtml():
    page = (HERE/'realm.html').read_text()
    code = page.split('// Scorecard: server-owned', 1)[1].split('// End scorecard', 1)[0]
    assert 'innerHTML' not in code
    assert 'textContent' in code
    print('AC9 text-only DOM PASS')


def verify_ac10_routes_and_footprint(base):
    guarded()
    paths = subprocess.check_output(['git', 'diff', '--name-only', base], text=True).splitlines()
    assert set(paths) <= {'src/realm.py', 'src/realm.html', 'src/test_realm_scorecard.py', 'e2e/realm-scorecard.spec.ts', 'src/test_realm_glance.py'}, paths
    # Existing route branches must remain byte-identical.
    old = subprocess.check_output(['git', 'show', base+':src/realm.py'], text=True)
    current = (HERE/'realm.py').read_text()
    addition = '        elif u.path == "/api/scorecard":\n            self._json(scorecard_payload())\n'
    assert current.split('    def do_GET(self):',1)[1].replace(addition, '') == old.split('    def do_GET(self):',1)[1]
    print('AC10 footprint and existing routes PASS')


if __name__ == '__main__':
    for test in [test_ac1_scorecard_route_payload, test_ac2_scorecard_failures_are_shown,
                 test_ac3_scorecard_is_cached_read_only_and_free, test_ac6_copy_path_is_text_only,
                 test_ac7_page_pace_formula_matches_pace_pct, test_ac9_no_unescaped_payload_in_innerhtml]:
        test()
    print('realm scorecard: 6 scratch tests PASS')
