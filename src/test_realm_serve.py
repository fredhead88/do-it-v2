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


def test_realm_serve_parts_read_only():
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
        env = {**os.environ, 'DOIT_ROOT': str(root), 'DOIT_LIVE_REPO': str(REPO),
               'HOME': str(Path(tmp) / 'home'), 'XDG_CONFIG_HOME': str(Path(tmp) / 'config')}
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
                for path,part in [('/api/bootstrap','first'),('/api/bootstrap?part=rest','rest'),('/api/glance',None)]:
                    with urllib.request.urlopen(base + path, timeout=180) as response:
                        body = response.read(); data = json.loads(body)
                        assert response.status == 200 and isinstance(data, dict)
                        if part: assert data['part'] == part
                        if part=='first': assert data['n_events'] >= 3, 'Scratch ledger was not loaded'
                        signatures.append(dict(url=base + path, status=response.status,
                                               body_sha256=hashlib.sha256(body).hexdigest(),
                                               body_excerpt=json.dumps({'part':data.get('part'),'error':data.get('error')})))
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
    assert_no_ledger_writes()
    print('PASS realm serve: page, bootstrap and read-only scratch ledger')
    print('PASS realm serve parts read-only')


def ledger_writes(source):
    import ast
    tree=ast.parse(source);tainted={'EVENTS','events_dir'}
    def derived(node):
        if isinstance(node,ast.Name): return node.id in tainted
        if isinstance(node,ast.Constant): return isinstance(node.value,str) and (node.value=='events' or node.value.startswith('events/'))
        if isinstance(node,ast.Attribute): return node.attr=='EVENTS' or derived(node.value)
        if isinstance(node,ast.BinOp): return derived(node.left) or derived(node.right)
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Attribute) and node.func.attr in {'glob','rglob','iterdir','resolve','absolute','joinpath','with_name','with_suffix'}:
                return derived(node.func.value)
            if isinstance(node.func,ast.Name) and node.func.id in {'Path','str'} or isinstance(node.func,ast.Attribute) and node.func.attr=='Path':
                return any(derived(a) for a in node.args)
        return False
    def bind(target,value):
        if isinstance(target,ast.Name) and derived(value): tainted.add(target.id)
        if isinstance(target,(ast.Tuple,ast.List)) and isinstance(value,(ast.Tuple,ast.List)):
            for t,v in zip(target.elts,value.elts): bind(t,v)
    for _ in range(len(list(ast.walk(tree)))):
        before=set(tainted)
        for node in ast.walk(tree):
            if isinstance(node,ast.Assign):
                for target in node.targets:bind(target,node.value)
            if isinstance(node,(ast.For,ast.comprehension)) and derived(node.iter):
                for name in ast.walk(node.target):
                    if isinstance(name,ast.Name):tainted.add(name.id)
        if before==tainted:break
    bad=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call): continue
        name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
        target=None
        if name=='open':
            modes=node.args[1:2]+[k.value for k in node.keywords if k.arg=='mode']
            writing=any(isinstance(m,ast.Constant) and isinstance(m.value,str) and any(c in m.value for c in 'wax+') for m in modes)
            if writing:target=node.func.value if isinstance(node.func,ast.Attribute) else node.args[0] if node.args else None
        elif name in {'write_text','write_bytes'} and isinstance(node.func,ast.Attribute):target=node.func.value
        elif name in {'rename','replace','remove','unlink','move'}:
            if isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id in {'os','shutil'}:
                if any(derived(a) for a in node.args):bad.append(node.lineno)
            elif isinstance(node.func,ast.Attribute):target=node.func.value
        if target is not None and derived(target):bad.append(node.lineno)
    return bad


def assert_no_ledger_writes():
    for source in ["p=EVENTS/'x';q=p;q.write_text('bad')", "p=ROOT/'events'/'x';open(p,'a')", "os.replace('x',EVENTS/'y')", "for f in EVENTS.glob('*.jsonl'): f.unlink()"]:
        assert ledger_writes(source),source
    for name in ('realm.py','realm_ledger.py','realm_glance.py'):
        assert not ledger_writes((REPO/'src'/name).read_text()),name


if __name__ == '__main__':
    test_realm_serve_parts_read_only()
