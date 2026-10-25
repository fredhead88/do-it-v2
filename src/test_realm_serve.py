#!/usr/bin/env python3
"""Serve the real Realm entry point over a disposable ledger without mutating it."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


REPO = Path(__file__).resolve().parents[1]


def digests(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_realm_serve():
    with tempfile.TemporaryDirectory(prefix='realm-serve-') as tmp:
        root = Path(tmp) / 'root'
        (root / 'events').mkdir(parents=True)
        (root / 'seat').mkdir()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = [dict(ts=now, type='tick', subject='tick', lane=i) for i in range(3)]
        (root / 'events' / 'L-executor-fixture.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows))
        (root / 'realm-history.json').write_text('{}')  # legacy history remains untouched
        before = digests(root)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'DOIT_ROOT': str(root), 'DOIT_LIVE_REPO': str(REPO)}
        env.pop('DOIT_LEDGER_FILE', None)
        signatures = []
        with tempfile.TemporaryFile() as stderr:
            child = subprocess.Popen([str(REPO / 'doit'), 'realm', '--port', str(port)],
                                     cwd=REPO, env=env, stdout=subprocess.DEVNULL,
                                     stderr=stderr, start_new_session=True)
            try:
                deadline = time.monotonic() + 60
                while True:
                    assert child.poll() is None, 'Realm exited before serving'
                    try:
                        with urllib.request.urlopen(base + '/', timeout=2) as response:
                            page = response.read()
                            assert response.status == 200 and b'realm' in page.lower()
                            signatures.append(dict(url=base + '/', status=response.status,
                                                   body_sha256=hashlib.sha256(page).hexdigest(),
                                                   body_excerpt='realm'))
                        break
                    except (urllib.error.URLError, TimeoutError):
                        assert time.monotonic() < deadline, 'Realm startup timed out'
                        time.sleep(0.1)
                with urllib.request.urlopen(base + '/api/bootstrap', timeout=180) as response:
                    body = response.read()
                    data = json.loads(body)
                    assert response.status == 200 and isinstance(data, dict)
                    assert data['n_events'] >= 3, 'Scratch ledger was not loaded'
                    signatures.append(dict(url=base + '/api/bootstrap', status=response.status,
                                           body_sha256=hashlib.sha256(body).hexdigest(),
                                           body_excerpt=json.dumps({'n_events': data['n_events']})))
            except Exception as exc:
                stderr.seek(0)
                raise AssertionError(f'{exc}\nRealm stderr:\n{stderr.read().decode(errors="replace")}') from exc
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
        after = digests(root)
        assert all(after.get(path) == digest for path, digest in before.items()), \
            'Realm changed an existing scratch-root file'
        added = set(after) - set(before)
        assert all(path.startswith('cache/') for path in added), f'Realm created files: {sorted(added)}'
        print(json.dumps({'responses': signatures, 'ledger_rows': len(rows),
                          'original_files_unchanged': True, 'cache_files_created': sorted(added)}))
    print('PASS realm serve: page, bootstrap and read-only scratch ledger')


if __name__ == '__main__':
    test_realm_serve()
