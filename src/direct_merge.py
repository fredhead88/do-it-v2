"""Merge one do-it-v2 queue row; selection and follow-up belong to the caller."""
from pathlib import Path
import re
import subprocess
from types import FunctionType

import merge_gate

DOIT = str(Path(__file__).resolve().parent.parent / 'doit')


def cli_gate(repo):
    """Return a gate(branch, main, spec) using the repository's existing CLI."""
    def gate(branch, main, spec):
        result = subprocess.run([DOIT, 'gate', branch, main, '--spec', spec],
                                cwd=repo, capture_output=True)
        return result.returncode, (result.stdout + result.stderr).decode(errors='replace')
    return gate


def merge_one(spec, row, *, repo, gate, run, emit, now):
    """Return status/sha/detail. `run` follows subprocess.run's bytes interface.

    `now` is reserved for callers' clock injection; shipped uses emit's clock.
    The caller supplies an integrator already on main. Never switch its branch.
    """
    def result(status, detail='', sha=None):
        return dict(status=status, sha=sha, detail=detail)

    branch = row.get('branch')
    if (not isinstance(spec, str) or not re.fullmatch(r'L-spec-\d+', spec)
            or not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9._/-]+', branch)
            or branch.startswith('-') or row.get('spec') != spec
            or row.get('project') != 'do-it-v2'):
        return result('gate-red', 'invalid do-it-v2 queue identifiers')

    def git(args):
        return run(['git', '-C', str(repo), *merge_gate.PINNED, *args],
                   capture_output=True, env=merge_gate.ENV)

    def checked(*args):
        process = git(args)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args,
                                                process.stdout, process.stderr)
        return process.stdout

    # Reuse the gate's ambiguity-safe resolver with a per-call runner, without
    # mutating merge_gate's process-global repo, deadline or resolution cache.
    resolve = FunctionType(merge_gate.resolve.__code__,
                           dict(merge_gate.resolve.__globals__, run=git, _RESOLVED={}))
    main_sha, branch_sha = resolve('main'), resolve(branch)
    if checked('symbolic-ref', '--quiet', 'HEAD').strip() != b'refs/heads/main':
        return result('regate', 'integrator must be on main')
    changed = set(checked('diff', '--name-only', '--no-renames', '-z',
                          f'{main_sha}...{branch_sha}').split(b'\0')) - {b''}
    entries = iter(checked('status', '--porcelain=v1', '-z', '--untracked-files=all').split(b'\0'))
    dirty = set()
    for entry in entries:
        if not entry:
            continue
        dirty.add(entry[3:])
        if b'R' in entry[:2] or b'C' in entry[:2]:
            dirty.add(next(entries))
    overlap = sorted(changed & dirty)
    if overlap:
        return result('refused-dirty', f'{len(overlap)} overlapping dirty path(s): ' +
                      ', '.join(path.decode(errors='replace') for path in overlap))
    code, output = gate(branch, 'main', spec)
    if code:
        return result('gate-red', output)
    try:
        fresh = resolve('main'), resolve(branch)
    except merge_gate.Undetermined:
        return result('regate', 'main or branch no longer resolves after gate')
    if fresh != (main_sha, branch_sha):
        return result('regate', 'main or branch moved during gate')
    if checked('symbolic-ref', '--quiet', 'HEAD').strip() != b'refs/heads/main':
        return result('regate', 'integrator moved off main during gate')
    subject = checked('log', '-1', '--format=%B', branch_sha).decode(errors='replace').splitlines()[0]
    try:
        merged = git(('merge', '--no-ff', branch_sha, '-m', f'merge({spec}): {subject}'))
        if merged.returncode:
            detail = (merged.stdout + merged.stderr).decode(errors='replace')
        else:
            sha = checked('rev-parse', 'HEAD').decode().strip()
            emit('shipped', spec, branch=branch, project='do-it-v2', sha=sha)
            return result('shipped', sha=sha)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
    try:
        aborted = git(('merge', '--abort'))
        if aborted.returncode:
            detail += '\nmerge --abort: ' + aborted.stderr.decode(errors='replace')
    except (OSError, subprocess.SubprocessError) as exc:
        detail += '\nmerge --abort: ' + str(exc)
    return result('merge-failed', detail)
