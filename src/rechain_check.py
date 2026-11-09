"""Cumulative, object-only stack admission through the existing re-chain resolver."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import sys
import threading

import cpu_share
import fold
import project_scope
import rechain


class Undetermined(RuntimeError):
    pass


@dataclass
class StackStep:
    spec: str
    outcome: str
    paths: list
    reason: str | None
    tip: str
    metadata: dict = field(default_factory=dict)


def git(repo, *args, inp=None, check=True):
    try:
        p = subprocess.run(cpu_share.niced(['git', '-C', str(repo), *args]),
                           input=inp, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise Undetermined('Git object operation unavailable') from None
    if check and p.returncode:
        raise Undetermined('Git object operation failed: '+args[0])
    return p


def out(repo, *args):
    return git(repo, *args).stdout.decode().strip()


def object_id(repo, ref, kind='commit'):
    if not isinstance(ref, str) or not ref or ref.startswith('-') or any(ord(c) < 33 for c in ref):
        raise Undetermined('invalid Git object input')
    return out(repo, 'rev-parse', '--verify', '--end-of-options', ref+'^{'+kind+'}')


# resolve's helpers share module globals. Serialize this narrow adapter so all
# its Git work receives the same fail-closed runner, including tool failures
# that resolve would otherwise mistake for a rules decline.
_resolver_lock = threading.RLock()


@contextmanager
def resolver_io():
    with _resolver_lock:
        original = rechain.git
        rechain.git = git
        try:
            yield
        finally:
            rechain.git = original


def stack(repo, base, entries):
    cur = object_id(repo, base)
    steps = []
    with resolver_io():
        for spec, branch, sha in entries:
            sha = object_id(repo, sha)
            probe = git(repo, 'merge-tree', '--write-tree', '--name-only', cur, sha, check=False)
            if probe.returncode not in (0, 1):
                raise Undetermined('merge-tree could not assess candidate')
            lines = probe.stdout.decode().splitlines()
            if not lines:
                raise Undetermined('merge-tree returned no tree')
            paths = lines[1:lines.index('', 1)] if probe.returncode == 1 and '' in lines[1:] else []
            events = []
            tip, reason = rechain.resolve(repo, cur, (spec, branch, sha), 'preflight',
                                         ev=lambda kind, subject, **kw: events.append(dict(type=kind, **kw)))
            meta = next((e for e in events if e['type'] == 'rechained'), {})
            if tip:
                outcome = 'rechained' if meta else 'clean'
                cur = tip
            else:
                mb = out(repo, 'merge-base', cur, sha)
                outcome = 'declined' if rechain.added_versions(repo, mb, sha) else 'content-conflict'
                if events:
                    reason = events[-1].get('rule', reason)
            steps.append(StackStep(spec, outcome, paths, reason, cur, meta))
            if tip is None:
                break
    return steps


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) or value in ('.', '..'):
        raise Undetermined('invalid project/spec identifier')
    return value


def resolve_specs(repo, specs):
    if not specs or len(set(specs)) != len(specs):
        raise Undetermined('distinct registered spec ids required')
    for spec in specs:
        identifier(spec)
    events = fold.read_events()
    project = project_for(repo)
    registered = {e['subject'] for e in events if e.get('type') == 'spec-written' and e.get('project') == project}
    branches = {e['subject']: e['branch'] for e in events if e.get('type') == 'merge-gate-clean'
                and e.get('project') == project and e.get('branch')}
    entries = []
    for spec in specs:
        if spec not in registered or spec not in branches:
            raise Undetermined('spec registration or merge-gate-clean branch unavailable: '+spec)
        branch = branches[spec]
        if not isinstance(branch, str) or git(repo, 'check-ref-format', '--branch', branch, check=False).returncode:
            raise Undetermined('invalid registered branch')
        entries.append((spec, branch, object_id(repo, 'refs/heads/'+branch)))
    return entries


def project_for(repo):
    """Resolve a registered checkout or any of its linked worktrees."""
    directory = fold.ROOT / 'repos'
    common = out(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    for registered in sorted(directory.iterdir()):
        if not project_scope.registered(fold.ROOT, registered.name):
            continue
        if registered.resolve() == Path(repo).resolve():
            return registered.name
        other = git(registered, 'rev-parse', '--path-format=absolute', '--git-common-dir', check=False)
        if other.returncode == 0 and Path(other.stdout.decode().strip()).resolve() == Path(common).resolve():
            return registered.name
    raise Undetermined('repository is not registered')


def arguments(argv, *, preflight=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--on', required=True)
    parser.add_argument('--specs', required=True)
    if preflight:
        parser.add_argument('--tree')
    a = parser.parse_args(argv)
    identifier(a.project)
    if not project_scope.registered(fold.ROOT, a.project):
        raise Undetermined('project is not registered')
    a.repo = fold.ROOT / 'repos' / a.project
    a.specs = a.specs.split(',')
    for spec in a.specs:
        identifier(spec)
    return a


def diagnostic(value):
    # Never forward raw child output or environment values to the terminal.
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def main(argv=None):
    try:
        a = arguments(sys.argv[1:] if argv is None else argv)
        steps = stack(a.repo, a.on, resolve_specs(a.repo, a.specs))
        for step in steps:
            print(step.spec, step.outcome, diagnostic(','.join(step.paths) or step.reason or ''))
        return int(any(s.outcome in ('content-conflict', 'declined') for s in steps))
    except (Undetermined, OSError, ValueError):
        print('RECHAIN-UNDETERMINED stack repository or inputs unavailable')
        return 2


if __name__ == '__main__':
    sys.exit(main())
