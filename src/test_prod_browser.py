#!/usr/bin/env python3
"""Disposable git fixtures and recorded browser execution; no network or login."""
from datetime import datetime, timezone
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
from unittest.mock import patch

import prod_browser as browser
import prod_runner
import owed_routes

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
SOURCE = pathlib.Path(__file__).resolve().parent.parent


def signed(criterion, body):
    data = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='fixture://prod-browser/' + criterion, status=0,
                         body_sha256=hashlib.sha256(data.encode()).hexdigest(), body_excerpt=body)))
    print(criterion + ' ok')


class Runner:
    def __init__(self, repo):
        self.repo, self.calls, self.fault = repo, [], None
        self.report = {'suites': [{'specs': [
            {'file': 'e2e/one.spec.ts', 'title': 'one', 'tests': [{'results': [{'status': 'passed'}]}]},
            {'file': 'e2e/two.spec.ts', 'title': 'second failing test', 'tests': [{'results': [{'status': 'failed'}]}]}
        ]}]}

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.fault and self.fault in argv:
            if self.fault == 'test':
                raise subprocess.TimeoutExpired(argv, 600)
            raise OSError('fixture fault')
        if argv[:2] == ['git', 'clone']:
            shutil.copytree(self.repo, pathlib.Path(argv[-1]), dirs_exist_ok=True)
        if argv[0] == 'npm':
            (pathlib.Path(kw['cwd']) / 'node_modules').mkdir()
        output = json.dumps(self.report) if 'test' in argv else ''
        if 'cat-file' in argv:
            return subprocess.CompletedProcess(argv, int('missing.spec.ts' in argv[-1]), '', '')
        return subprocess.CompletedProcess(argv, 0, output, '')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        top = pathlib.Path(tmp)
        repo = top / 'repo'
        dashboard = repo / 'dashboard'
        (dashboard / 'e2e').mkdir(parents=True)
        for name in ('one', 'two'):
            (dashboard / 'e2e' / (name + '.spec.ts')).write_text('// fixture\n')
        (dashboard / 'package-lock.json').write_text('{}')
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
        git('init', '-q'); git('add', '.')
        git('-c', 'user.name=fixture', '-c', 'user.email=fixture@localhost', 'commit', '-qm', 'fixture')
        sha = git('rev-parse', 'HEAD')
        spec = top / 'spec.md'
        spec.write_text('## Acceptance criteria\n' + '\n'.join([
            'AC1 [backend]: dashboard/e2e/one.spec.ts', 'AC2 [backend]: dashboard/e2e/two.spec.ts',
            'AC3 [backend]: dashboard/e2e/missing.spec.ts', 'AC4 [backend]: ../../etc/x.spec.ts',
            'AC5 [backend]: no file', 'AC6 [backend]: dashboard/e2e/../one.spec.ts']))
        rows = [dict(spec='L-spec-9000', criterion='AC' + str(i), project='demo', spec_path=str(spec)) for i in range(1, 7)]
        rows.append(dict(rows[0], criterion='AC7', spec_path=str(top / 'absent')))
        config = {'prod': [dict(project='demo', repo=str(repo))]}
        events = [dict(type='deploy-landed', target='demo', project='hash-pid-123', sha=sha, ts=NOW.isoformat())]
        root = top / 'scratch'
        def sub(name):
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            return path
        home = top / 'home'
        creds = home / '.do-it' / 'review-creds-demo.env'
        creds.parent.mkdir(parents=True)
        # Generated fixture values are never included in output.
        secrets = ['fixture-user-' + 'sentinel', 'fixture-pass-' + 'sentinel']
        creds.write_text('VERIFIER_CLIENT_USER=' + secrets[0] + '\nVERIFIER_CLIENT_PASS=' + secrets[1] + '\n')
        fake = Runner(repo)
        with patch.object(browser.grading_env, '_look_toml', return_value=config), \
                patch.object(browser.fold, 'read_events', return_value=events), \
                patch.object(browser.scratch, 'sub', side_effect=sub), \
                patch.dict(os.environ, {'HOME': str(home)}, clear=False):
            e2e, seat = browser.split(rows)
            assert e2e == rows[:2] and seat == rows[2:]
            for event in (dict(events[0], target='other', project='demo'), dict(events[0], sha='0' * 40)):
                with patch.object(browser.fold, 'read_events', return_value=[event]):
                    assert browser.split(rows) == ([], rows)
            signed('AC1', {'e2e': len(e2e), 'seat': len(seat), 'real_target_and_commit_checked': True})
            with patch.object(socket, 'socket', side_effect=AssertionError('network')), \
                    patch.object(os, 'system', side_effect=AssertionError('shell')), \
                    patch.object(subprocess, 'run', side_effect=AssertionError('process')), \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('process')):
                result = browser.run_batch(rows, NOW, runner=fake)
                assert len(result) == 2
                assert browser.run_batch(rows[2:], NOW, runner=fake) == []
            clones = [a for a, k in fake.calls if a[:2] == ['git', 'clone']]
            plays = [(a, k) for a, k in fake.calls if 'test' in a]
            assert len(clones) == len(plays) == 1
            clone = pathlib.Path(clones[0][-1])
            assert clones[0][2] == '--shared' and str(repo) in clones[0]
            assert plays[0][1]['cwd'] == clone / 'dashboard'
            assert not clone.exists() and clone.parent.parent == sub('prod-browser')
            for argv, kw in fake.calls:
                assert kw.get('cwd') != repo
                assert not any('/opt/albert-scott' in str(v) or 'portal-e2e-runner' in str(v) for v in [*argv, *kw.get('env', {}).values()])
            for fault in ('clone', 'checkout', 'ci', 'test'):
                fake.fault = fault
                # Force a cache miss for the npm fault.
                if fault == 'ci':
                    shutil.rmtree(sub('node-cache'))
                obs = browser.run_batch(rows[:2], NOW, runner=fake)
                assert all(o['outcome'] == 'unobservable' for o in obs)
                assert all(owed_routes.route_for(o['capability']) for o in obs)
                assert not list(sub('prod-browser').iterdir())
            fake.fault = None
            signed('AC2', {'scratch_removed_on_success_and_faults': True, 'recorded_clone': True})
            assert all('e2e/' + name + '.spec.ts' in plays[0][0] for name in ('one', 'two'))
            assert '--reporter=json' in plays[0][0]
            assert not (sub('node-cache') / hashlib.sha256(b'{}').hexdigest()[:16] / 'node_modules').is_symlink()
            # Different deployed revisions stay in separate execution groups.
            with patch.object(browser, '_target', side_effect=[(repo, sha), (repo, '1' * 40)]):
                grouped = Runner(repo)
                assert len(browser.run_batch(rows[:2], NOW, runner=grouped)) == 2
                assert sum('test' in a for a, k in grouped.calls) == 2
                assert sum(a[:2] == ['git', 'clone'] for a, k in grouped.calls) == 2
            signed('AC3', {'clones': 1, 'invocations': 1, 'private_node_copy': True})
            assert [o['outcome'] for o in result] == ['met', 'failed']
            assert 'second failing test' in result[1]['evidence']
            for obs in result:
                assert set(obs) == {'spec', 'criterion', 'outcome', 'evidence', 'observed_at', 'valid_until'}
                assert obs['evidence'].startswith('clone=' + str(clone)) and len(obs['evidence']) <= 600
                assert obs['observed_at'] == NOW.isoformat() and obs['valid_until'].startswith('2026-10-19')
            fake.report['suites'][0]['specs'][0]['tests'][0]['results'][0].update(
                status='failed', errors=[{'message': "browserType.launch: Executable doesn't exist"}])
            assert browser.run_batch(rows[:1], NOW, runner=fake)[0]['outcome'] == 'unobservable'
            fake.report = {'suites': []}
            assert all(o['outcome'] == 'unobservable' for o in browser.run_batch(rows, NOW, runner=fake))
            signed('AC4', {'outcomes': ['met', 'failed', 'unobservable'], 'shape_and_expiry_checked': True})
            allowed = {'VERIFIER_CLIENT_USER', 'VERIFIER_CLIENT_PASS', 'PATH', 'HOME', 'E2E_BASE_URL', 'PLAYWRIGHT_BROWSERS_PATH'}
            env = plays[0][1]['env']
            assert set(env) <= allowed and env['VERIFIER_CLIENT_USER'] == secrets[0] and env['VERIFIER_CLIENT_PASS'] == secrets[1]
            assert not any(s in json.dumps(result) for s in secrets)
            assert not any(s in json.dumps(a) for a, k in fake.calls for s in secrets)
            fake.report = {'suites': [{'specs': [{'file': 'e2e/one.spec.ts',
                'title': secrets[0] + secrets[1], 'tests': [{'results': [{'status': 'failed'}]}]}]}]}
            redacted = browser.run_batch([dict(rows[0], validity_h=2)], NOW, runner=fake)[0]
            assert '[redacted]' in redacted['evidence'] and redacted['valid_until'].startswith('2026-10-18T02:')
            assert not any(s in json.dumps(redacted) for s in secrets)
            creds.unlink()
            missing = browser.run_batch(rows, NOW, runner=fake)
            assert all(o['outcome'] == 'unobservable' and o['capability'] == 'browser' for o in missing)
            assert not any(s.encode() in p.read_bytes() for p in root.rglob('*') if p.is_file() for s in secrets)
            signed('AC5', {'allowlisted_environment': True, 'missing_credentials_blocked': True})
        mcp = json.loads((SOURCE / 'mcp/chrome-devtools.json').read_text())
        assert set(mcp['mcpServers']) == {'chrome-devtools'}
        server = mcp['mcpServers']['chrome-devtools']
        assert set(server) == {'command', 'args'} and server['command'] == 'npx'
        assert 'chrome-devtools-mcp@1.10.1' in server['args']
        assert '--headless' in server['args'] and '--isolated' in server['args']
        signed('AC6', {'version': '1.10.1', 'one_server': True})
        for name in ('prod_browser.py', 'test_prod_browser.py', 'sweep_owed.py', 'test_sweep_owed.py', 'prod_runner.py'):
            assert len((SOURCE / 'src' / name).read_text().splitlines()) <= 1500
        assert len((SOURCE / 'src/packet.py').read_text().splitlines()) <= 1578
        # An import must not open the operator's root or create scratch files.
        import importlib
        original_open = pathlib.Path.open
        def guarded_open(path, *args, **kw):
            assert '.do-it' not in str(path) and str(root) not in str(path)
            return original_open(path, *args, **kw)
        with patch.object(pathlib.Path, 'open', guarded_open):
            importlib.reload(browser)
        signed('AC10', {'injected_execution': True, 'size_bounds': True})


if __name__ == '__main__':
    main()
