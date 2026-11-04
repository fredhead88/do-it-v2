"""Bounded, isolated refresh of completed branches before builder recovery."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import fold
import ledger_cache

REFUSALS = frozenset({'stale-base', 'branch-not-descendant', 'pinned-base-mismatch'})


def config():
    import tomllib
    return tomllib.loads((Path(__file__).resolve().parent.parent / 'look.toml').read_text())


def git(repo, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True,
                          text=True, check=check, timeout=120)


def value(repo, *args):
    return git(repo, *args).stdout.strip()


def emit(kind, subject, **fields):
    previous = os.environ.get('DOIT_LEDGER_FILE')
    os.environ['DOIT_LEDGER_FILE'] = 'L-executor-0001.jsonl'
    try:
        return fold.append([kind, subject, *[f'{k}:={json.dumps(v)}' for k, v in fields.items()]], strict=True)
    finally:
        if previous is None:
            os.environ.pop('DOIT_LEDGER_FILE', None)
        else:
            os.environ['DOIT_LEDGER_FILE'] = previous


def utc_day(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc).date()


def refresh_note(events, spec):
    own = [e for e in events if e.get('subject') == spec]
    refreshed = max((e for e in own if e.get('type') == 'branch-refreshed'),
                    key=lambda e: e.get('ts', ''), default=None)
    done = max((e.get('ts', '') for e in own if e.get('type') == 'build-done'), default='')
    if refreshed and refreshed.get('ts', '') > done:
        return f"refreshed onto {refreshed.get('base_sha', '?')} at {refreshed['ts']}"
    return ''


def open_builder(events, spec):
    active = set()
    for e in events:
        if e.get('subject') != spec:
            continue
        spawn = e.get('spawn')
        builder = e.get('role') == 'builder' or str(spawn).startswith('L-builder-')
        if e.get('type') in {'spawn-started', 'autodispatched', 'build-started'} and builder:
            active.add(spawn)
        elif e.get('type') in {'spawn-done', 'spawn-failed', 'spawn-stale', 'build-done', 'build-blocked'}:
            active.discard(spawn)
    return bool(active)


def holders(repo, ref):
    found = []
    for block in value(repo, 'worktree', 'list', '--porcelain').split('\n\n'):
        fields = dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
        if fields.get('branch') == ref:
            found.append(Path(fields['worktree']))
    return found


def dirty(tree):
    if value(tree, 'status', '--porcelain=v1', '--untracked-files=all'):
        return True
    return any(Path(value(tree, 'rev-parse', '--path-format=absolute', '--git-path', name)).exists()
               for name in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'))


def guard_commands(project, tree, row):
    if project == 'do-it-v2':
        return [('project-guards', './doit test')]
    if project == 'albert-scott':
        import shlex
        import window_gate
        python = str(Path(row['venv']) / 'bin/python')
        nodes = [n.removesuffix('::') for n in window_gate.tree_guards(os.environ)]
        return [('project-guards', ' '.join(map(shlex.quote, [python, '-m', 'pytest', '-q', *nodes])))]
    raise ValueError('project guards unavailable')


def check_tree(spec, project, repo, tree, auxiliary, text):
    """Use build_runner's extraction, rewrite, provisioning and execution policy."""
    import base_red
    import build_runner as runner
    import cpu_share
    import grading_env
    commands, _ = runner._commands(text, project, None)
    row = grading_env._project_row(project)
    commands += guard_commands(project, tree, row)
    env = cpu_share.launch_env({k: v for k, v in grading_env._clean_env().items()
                               if not k.startswith('GIT_')}, fold.ROOT)
    for name in ('DOIT_LEDGER_FILE', 'DOIT_SUPERVISED', 'PYTHONPATH'):
        env.pop(name, None)
    for name, directory in (('HOME', 'home'), ('DOIT_ROOT', 'root'), ('TMPDIR', 'tmp')):
        target = auxiliary / directory
        target.mkdir()
        env[name] = str(target)
    if row.get('venv'):
        env['PATH'] = str(Path(row['venv']) / 'bin') + os.pathsep + env.get('PATH', os.defpath)
    deadline = time.monotonic() + row.get('runner_wall_cap_s', runner.WALL_CAP)
    failures, reports = [], []
    try:
        runner._provision(tree, auxiliary, row, env, deadline)
        with cpu_share.claim(fold.ROOT):
            for index, (criterion, command) in enumerate(commands):
                command, _ = runner._rewrite(command, repo, tree, row.get('venv'))
                code, output = runner._process(['/bin/bash', '-c', command], env=env,
                                               deadline=deadline, cwd=tree, lower=True)
                parsed = base_red.parse_log(output.decode('utf-8', 'replace'))
                ids = sorted({node for kind, nodes in parsed.items() if kind != 'undetermined' for node in nodes})
                if code:
                    failures.extend(ids or [criterion or f'verification-step-{index + 1}'])
                reports.append(dict(criterion=criterion, exit=code, failing_ids=ids))
    finally:
        grading_env.teardown(auxiliary)
    return failures, reports


def regate(repo, branch, main, spec):
    # A fresh process clears the gate's per-run ref cache after adoption.
    script = Path(__file__).with_name('merge_gate.py')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['/usr/bin/python3', str(script), branch, main, '--spec', spec],
                          cwd=repo, env=env, timeout=600).returncode


@contextmanager
def lock(repo, spec):
    common = Path(value(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    with (common / ('branch-refresh-' + spec + '.lock')).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield stream


def rework(spec, branch, events, *, paths, **facts):
    import conflict
    attempt = conflict.conflict_attempts(events, spec)
    emit('conflict-rework', spec, branch=branch, attempt=attempt.attempt,
         verdict=attempt.verdict, paths=paths, **facts)


def refresh(repo, branch, main, spec, *, events=None, now=None):
    """Return refreshed/conflict/checks-failed/skipped; never dispatch a role."""
    import re
    if not re.fullmatch(r'L-spec-\d+', spec):
        raise ValueError('invalid refresh spec')
    repo = Path(repo).resolve()
    import merge_gate
    merge_gate.begin_run(repo)
    now = now or datetime.now(timezone.utc)
    # Refresh under reservation/adoption locks and after git actions: never
    # decide from the caller snapshot, but reuse validated cached history.
    with lock(repo, spec) as reservation:
        persisted = ledger_cache.tail_refresh()
        events = persisted if events is None else [*events, *persisted]
        project = next((e['project'] for e in reversed(events) if e.get('subject') == spec and e.get('project')), repo.name)
        def skip(reason):
            emit('branch-refresh-skipped', spec, reason=reason, project=project)
            return dict(state='skipped', reason=reason)
        ref = branch if branch.startswith('refs/heads/') else 'refs/heads/' + branch
        if git(repo, 'check-ref-format', ref, check=False).returncode:
            return skip('invalid-branch')
        before = value(repo, 'rev-parse', '--verify', ref + '^{commit}')
        base = value(repo, 'rev-parse', '--verify', main + '^{commit}')
        common = value(repo, 'merge-base', before, base)
        if value(repo, 'rev-list', '--count', common + '..' + before) != '1':
            return skip('multiple-commits')
        if git(repo, 'merge-base', '--is-ancestor', base, before, check=False).returncode == 0:
            return skip('already-current')
        text = (fold.ROOT / 'content' / (spec + '.md')).read_text()
        import merge_gate
        writes = merge_gate.grant_from_text(text)
        protected = ('api/alembic_supabase/versions/*', 'genesis_schema_fingerprint.txt',
                     '*/genesis_schema_fingerprint.txt', 'branch_isolation.json', '*/branch_isolation.json', '.secrets.baseline', '*/.secrets.baseline')
        if any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(pattern, path)
               for path in writes for pattern in protected):
            return skip('migration-or-fingerprint')
        if open_builder(events, spec):
            return skip('open-builder-dispatch')
        holding = holders(repo, ref)
        main_tree = Path(value(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).parent.resolve()
        if any(t.resolve() == main_tree for t in holding):
            return skip('integrator-holds-branch')
        if any(dirty(t) for t in holding):
            return skip('uncommitted-state')
        limit = config().get('merge_queue', {}).get('branch_refresh_per_day', 3)
        if type(limit) is not int or limit < 1:
            return skip('invalid-daily-limit')
        day = now.astimezone(timezone.utc).date()
        count = sum(e.get('type') == 'branch-refresh-skipped' and e.get('reason') == 'attempt-started'
                    and e.get('subject') == spec and e.get('day', str(utc_day(e['ts']))) == str(day) for e in persisted)
        if count >= limit:
            return skip('daily-limit')
        # A committed ledger reservation counts conflicts/crashes as attempts too.
        emit('branch-refresh-skipped', spec, reason='attempt-started', project=project, day=str(day), ts=now.isoformat())
        with tempfile.TemporaryDirectory(prefix='branch-refresh-') as name:
            scratch = Path(name)
            tree, auxiliary = scratch / 'tree', scratch / 'checks'
            auxiliary.mkdir()
            try:
                git(repo, 'worktree', 'add', '--detach', str(tree), before)
                result = git(tree, '-c', 'rerere.enabled=true', '-c', 'rerere.autoupdate=false',
                             '-c', 'user.name=Recheck Bot', '-c', 'user.email=fredhead88@users.noreply.github.com',
                             'rebase', '--no-autostash', '--no-update-refs', '--rebase-merges', base, check=False)
                if result.returncode:
                    paths = git(tree, 'diff', '--name-only', '--diff-filter=U', '-z').stdout.rstrip('\0').split('\0')
                    git(tree, 'rebase', '--abort', check=False)
                    if not paths or paths == ['']:
                        return skip('rebase-undetermined')
                    rework(spec, branch, events, paths=paths)
                    return dict(state='conflict', paths=paths)
                after = value(tree, 'rev-parse', 'HEAD')
                metadata = '%an%x00%ae%x00%aI%x00%B'
                if (value(tree, 'rev-list', '--count', base + '..HEAD') != '1'
                        or value(tree, 'rev-parse', 'HEAD^') != base
                        or value(tree, 'show', '-s', '--format=' + metadata, after)
                        != value(repo, 'show', '-s', '--format=' + metadata, before)
                        or value(tree, 'show', '-s', '--format=%cn', after) != 'Recheck Bot'):
                    return skip('commit-shape-or-metadata')
                # Pin the diagnostic commit even when the detached worktree is removed.
                git(repo, 'update-ref', f'refs/doit/branch-refresh/{spec}/{after}', after)
                try:
                    failures, reports = check_tree(spec, project, repo, tree, auxiliary, text)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, __import__('build_runner').Undetermined):
                    failures, reports = ['checks-undetermined'], []
                if (value(tree, 'rev-parse', 'HEAD') != after
                        or git(tree, 'diff', '--quiet', after, check=False).returncode):
                    failures.append('checks-modified-tree')
                if failures:
                    rework(spec, branch, events, paths=[], failing_ids=failures, rebased_sha=after)
                    return dict(state='checks-failed', failing_ids=failures, rebased_sha=after, reports=reports)
                # Recheck every adoption precondition after potentially long checks.
                if (value(repo, 'rev-parse', ref) != before or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec)
                        or holders(repo, ref) != holding or any(dirty(t) for t in holding)):
                    return skip('adoption-state-changed')
                for t in holding:
                    if value(t, 'rev-parse', 'HEAD') != before:
                        return skip('adoption-state-changed')
                if holding:
                    git(holding[0], 'reset', '--hard', after)
                else:
                    git(repo, 'update-ref', ref, after, before)
                emit('branch-refreshed', spec, spec=spec, project=project, from_sha=before,
                     to_sha=after, base_sha=base, checks='pass')
                import build_runner
                bundle = build_runner.run(spec, after)
                # Re-gating may itself refresh after another master move.
                fcntl.flock(reservation, fcntl.LOCK_UN)
                gate = regate(repo, branch, main, spec)
                return dict(state='refreshed', from_sha=before, to_sha=after, base_sha=base,
                            bundle=str(bundle), gate=gate, reports=reports)
            finally:
                if tree.exists():
                    git(tree, 'rebase', '--abort', check=False)
                    git(repo, 'worktree', 'remove', '--force', str(tree))
                git(repo, 'worktree', 'prune')


def recut(repo, branch, main, spec):
    """Replay non-merge patches on main, preserving the old tip before adoption."""
    import re
    import build_runner
    import merge_gate
    if not re.fullmatch(r'L-spec-\d+', spec):
        raise ValueError('invalid recut spec')
    repo = Path(repo).resolve()
    merge_gate.begin_run(repo)
    # Refresh under reservation/adoption locks and after git actions: never
    # decide from the caller snapshot, but reuse validated cached history.
    with lock(repo, spec) as reservation:
        events = ledger_cache.tail_refresh()
        project = next((e['project'] for e in reversed(events) if e.get('subject') == spec and e.get('project')), repo.name)
        def skip(reason):
            rework(spec, branch, events, paths=[], reason=reason)
            return dict(state='skipped', reason=reason)
        ref = branch if branch.startswith('refs/heads/') else 'refs/heads/' + branch
        if git(repo, 'check-ref-format', ref, check=False).returncode:
            return skip('invalid-branch')
        if ref in {'refs/heads/master', 'refs/heads/main'} or ref == value(repo, 'rev-parse', '--symbolic-full-name', '--end-of-options', main):
            return skip('main-branch')
        before = value(repo, 'rev-parse', '--verify', ref + '^{commit}')
        base = value(repo, 'rev-parse', '--verify', '--end-of-options', main + '^{commit}')
        if git(repo, 'merge-base', '--is-ancestor', base, before, check=False).returncode == 0:
            return skip('already-current')
        text = (fold.ROOT / 'content' / (spec + '.md')).read_text()
        protected = ('api/alembic_supabase/versions/*', '*genesis_schema_fingerprint.txt',
                     '*branch_isolation.json', '*.secrets.baseline')
        if any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(g, p) for p in merge_gate.grant_from_text(text) for g in protected):
            return skip('migration-or-fingerprint')
        holding = holders(repo, ref)
        main_tree = Path(value(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).parent.resolve()
        if open_builder(events, spec):
            return skip('open-builder-dispatch')
        if any(t.resolve() == main_tree for t in holding) or len(holding) > 1:
            return skip('integrator-holds-branch')
        if any(dirty(t) for t in holding):
            return skip('uncommitted-state')
        day = str(datetime.now(timezone.utc).date())
        limit = config().get('merge_queue', {}).get('branch_refresh_per_day', 3)
        if type(limit) is not int or limit < 1:
            return skip('invalid-daily-limit')
        if sum(e.get('type') == 'branch-refresh-skipped' and e.get('reason') == 'attempt-started'
               and e.get('subject') == spec and e.get('day', str(utc_day(e['ts']))) == day for e in events) >= limit:
            return skip('daily-limit')
        commits = value(repo, 'rev-list', '--reverse', '--topo-order', '--no-merges', base + '..' + before).splitlines()
        merges = int(value(repo, 'rev-list', '--count', '--merges', base + '..' + before))
        emit('branch-refresh-skipped', spec, reason='attempt-started', project=project, day=day)
        with tempfile.TemporaryDirectory(prefix='branch-recut-', dir='/var/tmp') as name:
            tree, auxiliary = Path(name) / 'tree', Path(name) / 'checks'
            auxiliary.mkdir()
            tag = None
            try:
                git(repo, 'worktree', 'add', '--detach', str(tree), base)
                for commit in commits:
                    if git(tree, 'diff-tree', '--quiet', '--root', commit, check=False).returncode == 0:
                        continue
                    result = git(tree, '-c', 'user.name=Recheck Bot', '-c', 'user.email=fredhead88@users.noreply.github.com',
                                 'cherry-pick', '--empty=drop', commit, check=False)
                    if result.returncode:
                        paths = git(tree, 'diff', '--name-only', '--diff-filter=U', '-z').stdout.rstrip('\0').split('\0')
                        git(tree, 'cherry-pick', '--abort', check=False)
                        if paths == ['']:
                            return skip('cherry-pick-undetermined')
                        rework(spec, branch, events, paths=paths)
                        return dict(state='conflict', paths=paths)
                after = value(tree, 'rev-parse', 'HEAD')
                count = int(value(tree, 'rev-list', '--count', base + '..HEAD'))
                try:
                    failures, reports = check_tree(spec, project, repo, tree, auxiliary, text)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, build_runner.Undetermined):
                    failures, reports = ['checks-undetermined'], []
                if (value(tree, 'rev-parse', 'HEAD') != after
                        or git(tree, 'diff', '--quiet', after, check=False).returncode):
                    failures.append('checks-modified-tree')
                if failures:
                    rework(spec, branch, events, paths=[], failing_ids=failures)
                    return dict(state='checks-failed', failing_ids=failures, reports=reports)
                if (value(repo, 'rev-parse', ref) != before or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec) or holders(repo, ref) != holding
                        or any(dirty(t) or value(t, 'rev-parse', 'HEAD') != before for t in holding)):
                    return skip('adoption-state-changed')
                tag, suffix = 'rework/' + spec.removeprefix('L-spec-') + '-prev', 1
                stem = tag
                while git(repo, 'show-ref', '--verify', '--quiet', 'refs/tags/' + tag, check=False).returncode == 0:
                    suffix += 1
                    tag = stem + '-' + str(suffix)
                git(repo, 'update-ref', 'refs/tags/' + tag, before, '')
                try:
                    if holding:
                        git(holding[0], 'reset', '--hard', after)
                    else:
                        git(repo, 'update-ref', ref, after, before)
                except subprocess.SubprocessError:
                    git(repo, 'update-ref', '-d', 'refs/tags/' + tag, before)
                    raise
                try:
                    bundle = build_runner.run(spec, after)
                    fcntl.flock(reservation, fcntl.LOCK_UN)
                    gate = regate(repo, branch, main, spec)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, build_runner.Undetermined):
                    gate = 1
                if gate:
                    fcntl.flock(reservation, fcntl.LOCK_EX)
                    if (value(repo, 'rev-parse', ref) == after and holders(repo, ref) == holding
                            and not any(dirty(t) or value(t, 'rev-parse', 'HEAD') != after for t in holding)):
                        if holding:
                            git(holding[0], 'reset', '--hard', before)
                        else:
                            git(repo, 'update-ref', ref, before, after)
                        git(repo, 'update-ref', '-d', 'refs/tags/' + tag, before)
                    rework(spec, branch, ledger_cache.tail_refresh(), paths=[], failing_ids=['merge-gate'])
                    return dict(state='checks-failed', failing_ids=['merge-gate'])
                facts = dict(from_sha=before, to_sha=after, tag=tag, commit_count=count,
                             skipped_merges=merges, dropped_empty=len(commits) - count)
                emit('branch-recut', spec, spec=spec, project=project, **facts)
                return dict(state='recut', bundle=str(bundle), gate=gate, reports=reports, **facts)
            finally:
                if tree.exists():
                    git(tree, 'cherry-pick', '--abort', check=False)
                    git(repo, 'worktree', 'remove', '--force', str(tree))
                git(repo, 'worktree', 'prune')


def before_refusal(runtime, a, reason):
    """Attempt refresh, but leave an authoritative stale packet refused."""
    if reason not in REFUSALS or getattr(a, 'role', 'builder') != 'builder':
        return None
    repo = runtime.ROOT / 'repos' / (a.project or '')
    if not repo.is_dir():
        return None
    try:
        import packet
        base = packet._effective_base(repo, a.project, runtime.spec_path(a.subject), a.subject)
        return refresh(repo, a.subject.lower(), base, a.subject)
    except (OSError, ValueError, subprocess.SubprocessError, __import__('merge_gate').Undetermined):
        return None


def dispatch_preflight(runtime, a, cwd, events, fail):
    # Initial, unbuilt branches retain the existing builder admission contract.
    completed = any(e.get('subject') == a.subject and e.get('type') == 'build-done'
                    and e.get('status') == 'DONE' for e in events)
    if not completed:
        return False
    if git(cwd, 'merge-base', '--is-ancestor', a.effective_base_sha, 'HEAD', check=False).returncode == 0:
        return True
    outcome = before_refusal(runtime, a, 'branch-not-descendant')
    if outcome and outcome['state'] == 'refreshed':
        fail('branch-refreshed: branch checked, adopted and re-gated; no builder needed', reason='branch-refreshed')
    try:
        import packet_base
        repo = runtime.ROOT / 'repos' / a.project
        main = packet_base._effective_branch(repo, a.project, runtime.spec_path(a.subject), a.subject)
        outcome = recut(repo, a.subject.lower(), main, a.subject)
    except (OSError, ValueError, subprocess.SubprocessError, __import__('merge_gate').Undetermined):
        outcome = None
    if outcome and outcome['state'] == 'recut':
        fail('branch-recut: branch checked, adopted and re-gated; no builder needed', reason='branch-recut')
    if getattr(runtime, 'pending_conflict_rework', lambda *_: False)(events, a.subject):
        return False
    fail('branch-not-descendant: isolated refresh/recut did not pass; preserve the branch for builder recovery',
         reason='branch-not-descendant')


def gate_refresh(repo, branch, main, spec):
    try:
        outcome = refresh(repo, branch, main, spec)
    except (OSError, ValueError, subprocess.SubprocessError, __import__('merge_gate').Undetermined):
        return None
    if outcome['state'] == 'refreshed':
        return outcome['gate']
    if outcome['state'] in {'conflict', 'checks-failed'}:
        return 1
    return None
