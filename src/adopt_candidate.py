"""Adopt a hand-repaired branch without claiming that Verification ran.

doit adopt-candidate L-spec-N SHA --by thinker --reason 'resolved conflicts'
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import fold
import packet_base
import spawns


LEDGER = 'L-adopt-candidate-local.jsonl'


@contextmanager
def git_environment():
    """Nested base resolvers must use the same Git routing as our own commands."""
    inherited = {k: v for k, v in os.environ.items() if k.startswith('GIT_')}
    for key in inherited:
        del os.environ[key]
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith('GIT_'):
                del os.environ[key]
        os.environ.update(inherited)


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        # Do not print repository/config error text that may contain credentials.
        raise ValueError('git ' + args[0] + ' failed; adoption not recorded')
    return result.stdout.strip()


def content_path(root, value):
    path = Path(value)
    for candidate in (path if path.is_absolute() else root / path,
                      root / 'content' / path.name):
        if candidate.is_file():
            return candidate
    raise ValueError('required spec or build card is missing')


def owned_worktree(repo, worktree, branch):
    """A conventional path alone is not authority to reset a checkout."""
    if worktree.is_symlink() or not worktree.is_dir() or worktree.resolve() == repo.resolve():
        raise ValueError('expected spec worktree is missing or redirected')
    top = Path(git(worktree, 'rev-parse', '--show-toplevel')).resolve()
    common = lambda p: Path(git(p, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if top != worktree.resolve() or common(worktree) != common(repo):
        raise ValueError('spec worktree does not belong to the project repository')
    listing = git(repo, 'worktree', 'list', '--porcelain')
    if not any(('worktree ' + str(worktree.resolve())) in block.splitlines()
               and ('branch refs/heads/' + branch) in block.splitlines()
               for block in listing.split('\n\n')):
        raise ValueError('worktree is not registered on the expected branch')
    if git(worktree, 'symbolic-ref', '--short', 'HEAD') != branch:
        raise ValueError('spec worktree is not on the expected branch')
    for name in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'):
        path = Path(git(worktree, 'rev-parse', '--git-path', name))
        if not path.is_absolute():
            path = worktree / path
        if path.exists():
            raise ValueError('finish the existing git operation before adoption')


def candidate_base(repo, project, spec, subject, sha):
    branch = subject.lower()
    ready = git(repo, 'rev-parse', '--verify', sha + '^{commit}')
    tip = git(repo, 'rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}')
    if ready != tip:
        raise ValueError('candidate is not the spec branch tip')
    base = packet_base._effective_base(repo, project, spec, subject)
    common = git(repo, 'merge-base', '--all', ready, base)
    if common != base:
        raise ValueError('candidate is not a descendant of current master/main or its configured window base')
    count = git(repo, 'rev-list', '--count', base + '..' + ready)
    if count != '1':
        raise ValueError(f'candidate must carry exactly one commit above its merge-base (found {count})')
    return ready, common


def adopt(subject, sha, *, by, reason):
    if not re.fullmatch(r'L-spec-\d+', subject):
        raise ValueError('expected L-spec-N subject')
    if not re.fullmatch(r'[0-9a-fA-F]{7,40}', sha):
        raise ValueError('expected a full or abbreviated commit SHA')
    if by not in {'thinker', 'operator', 'executor'} or not reason.strip():
        raise ValueError('adoption needs --by thinker/operator/executor and a nonempty --reason')
    root = fold.ROOT
    state = root / 'state'
    state.mkdir(parents=True, exist_ok=True)
    with (state / spawns.lock_name(subject)).open('a') as lock, git_environment():
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('another dispatch holds the subject lock') from exc
        # Read after locking: never adopt over a build/grader already in progress.
        events = fold.read_events()
        own = [e for e in events if e.get('subject') == subject]
        written = next((e for e in reversed(own) if e['type'] == 'spec-written'), None)
        if not written:
            raise ValueError('subject has no spec-written event')
        project = written.get('project')
        if (not isinstance(project, str) or project in {'.', '..'}
                or not re.fullmatch(r'[A-Za-z0-9._-]+', project)):
            raise ValueError('spec has no valid project')
        repo = root / 'repos' / project
        worktree = root / 'worktrees' / project / subject.lower()
        conflict = spawns.conflict('builder', subject, str(worktree),
                                  spawns.open_spawns(events, datetime.now(timezone.utc)))
        if conflict:
            raise ValueError(conflict)
        spec = content_path(root, written.get('path') or f'content/{subject}.md')
        previous = next((e for e in reversed(own) if e['type'] == 'build-done'), {})
        card = content_path(root, previous.get('card') or
                            f'content/L-card-{subject.removeprefix("L-spec-")}.md')
        ready, base = candidate_base(repo, project, spec, subject, sha)
        owned_worktree(repo, worktree, subject.lower())
        fields = dict(project=project, branch=subject.lower(), card=str(card), status='DONE',
                      ready_sha=ready, base_sha=base, base_sha_explicit=True,
                      adopted_by=by, reason=reason, verify_exit=None)
        # Admission precedes the destructive step; append rechecks it normally.
        refusal = fold.check_append(dict(type='build-done', subject=subject, **fields),
                                    fold.actor_of(LEDGER))
        if refusal:
            raise ValueError('ledger refused adoption: ' + refusal)
        # Intentionally reconcile stale indexes after a hand-moved branch ref.
        # No checkout/switch/clean and no reset in the integrator or other worktrees.
        git(worktree, 'reset', '--hard', ready)
        if candidate_base(repo, project, spec, subject, sha) != (ready, base):
            raise ValueError('branch/base changed during adoption; no event recorded')
        if git(worktree, 'rev-parse', 'HEAD') != ready or git(worktree, 'status', '--porcelain', '--untracked-files=no'):
            raise ValueError('worktree reconciliation failed; no event recorded')
        ledger_before, clock_before = os.environ.get('DOIT_LEDGER_FILE'), fold.NOW
        try:
            os.environ['DOIT_LEDGER_FILE'] = LEDGER
            fold.NOW = datetime.now(timezone.utc)
            event = fold.append(['build-done', subject] +
                                [k + ':=' + json.dumps(v) for k, v in fields.items()], strict=True)
        finally:
            fold.NOW = clock_before
            if ledger_before is None:
                os.environ.pop('DOIT_LEDGER_FILE', None)
            else:
                os.environ['DOIT_LEDGER_FILE'] = ledger_before
        return event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('subject')
    parser.add_argument('sha')
    parser.add_argument('--by', required=True, choices=['thinker', 'operator', 'executor'])
    parser.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        event = adopt(args.subject, args.sha, by=args.by, reason=args.reason)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('adopt-candidate: refused: ' + str(exc), file=sys.stderr)
        return 1
    print(f'adopted {args.subject} {event["ready_sha"]}; Verification still required')
    return 0


if __name__ == '__main__':
    sys.exit(main())
