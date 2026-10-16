#!/usr/bin/env python3
"""Capture toolchain/test evidence and compare the same SHA on both runners."""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def version(argv):
    # Fresh cwd and isolated Python prevent checkout/user imports from masking
    # polluted system interpreters. Never capture environment values.
    with tempfile.TemporaryDirectory(prefix='ci-probe-') as cwd:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def toolchain():
    return {'system_python': version(['/usr/bin/python3', '-I', '-c',
                                     'import platform; print(platform.python_version())']),
            'bare_python': version(['python3', '-I', '-c', 'import platform; print(platform.python_version())']),
            'system_imports': json.loads(version(['/usr/bin/python3', '-I', '-c',
                'import importlib.util,json; print(json.dumps({m: importlib.util.find_spec(m) is not None '
                'for m in ("fastapi", "sqlalchemy", "api")}))'])),
            'node': version(['node', '--version']), 'git': version(['git', '--version']),
            'os': {k: v.strip('"') for k, v in (line.split('=', 1) for line in
                    Path('/etc/os-release').read_text().splitlines() if '=' in line) if k in ('ID', 'VERSION_ID')}}


def probe_namespace():
    expected = os.environ.get('CI_HOST_NETNS')
    if not expected or os.readlink('/proc/self/ns/net') == expected:
        raise ValueError('private job network namespace is not proven')
    # This check executes ONLY after the namespace identity check. No host
    # service is probed. A private job must support its own loopback servers.
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=1):
            peer, _ = server.accept()
            peer.close()
    for address in ('127.0.0.1', '::1'):
        try:
            connection = socket.create_connection((address, 5432), timeout=1)
        except ConnectionRefusedError:
            continue
        except OSError as exc:
            # IPv6 may be disabled; timeout/unreachable IPv4 isn't a refusal.
            if address == '::1' and exc.errno in (97, 99, 101):
                continue
            raise
        else:
            connection.close()
            raise ValueError('unexpected PostgreSQL service in fresh job namespace')
    return True


def results(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if not cases:
        raise ValueError('test report is empty')
    outcomes = {}
    for case in cases:
        key = case.get('classname', '') + '::' + case.attrib['name']
        if key in outcomes:
            raise ValueError('duplicate test case: ' + key)
        outcomes[key] = ('failed' if case.find('failure') is not None or case.find('error') is not None else
                         'skipped' if case.find('skipped') is not None else 'passed')
    return outcomes


def compare(hosted, candidate, max_seconds):
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('window budget must be positive and finite')
    for doc in (hosted, candidate):
        wall = doc['wall_seconds']
        if isinstance(wall, bool) or not math.isfinite(wall) or wall <= 0:
            raise ValueError('wall time must be positive and finite')
        if not doc['sha'] or any(v not in ('failed', 'skipped', 'passed') for v in doc['tests'].values()):
            raise ValueError('invalid SHA or test outcome')
        if not doc['tests']:
            raise ValueError('test report is empty')
    issues = []
    for field in ('sha', 'toolchain', 'tests'):
        if hosted[field] != candidate[field]:
            issues.append(field + ' differs')
    if any(v == 'failed' for v in candidate['tests'].values()):
        issues.append('candidate has failures')
    if candidate['wall_seconds'] >= max_seconds:
        issues.append('candidate exceeds window budget')
    if candidate.get('private_netns') is not True:
        issues.append('candidate isolation not proven')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    capture = subs.add_parser('capture')
    capture.add_argument('--sha', required=True)
    capture.add_argument('--junit', type=Path, required=True)
    capture.add_argument('--wall-seconds', type=float, required=True)
    capture.add_argument('--private-netns', action='store_true')
    diff = subs.add_parser('compare')
    diff.add_argument('hosted', type=Path)
    diff.add_argument('candidate', type=Path)
    diff.add_argument('--max-seconds', type=float, default=4200)
    args = parser.parse_args()
    if args.command == 'capture':
        print(json.dumps(dict(sha=args.sha, wall_seconds=args.wall_seconds, tests=results(args.junit),
                              toolchain=toolchain(), private_netns=probe_namespace() if args.private_netns else False),
                         sort_keys=True))
        return 0
    issues = compare(json.loads(args.hosted.read_text()), json.loads(args.candidate.read_text()), args.max_seconds)
    print(json.dumps(dict(equivalent=not issues, issues=issues)))
    return int(bool(issues))


if __name__ == '__main__':
    raise SystemExit(main())
