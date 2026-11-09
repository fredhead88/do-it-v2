"""Bounded, isolated refresh of completed branches before builder recovery."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import fnmatch
import json
import math
import os
from pathlib import Path
import resource
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


def open_builder(events, spec, now=None):
    import dispatch
    now = now or datetime.now(timezone.utc)
    active = {}
    for e in events:
        if e.get('subject') != spec:
            continue
        spawn = e.get('spawn')
        kind = e.get('type')
        role = (e.get('role') or '-'.join(str(spawn or '').split('-')[1:-1])
                or ('builder' if kind == 'build-started' else ''))
        if kind in {'spawn-started', 'build-started'} and role == 'builder':
            cap = dispatch.ROLES['builder'][1]
            stamp = datetime.fromisoformat(e['ts'].replace('Z', '+00:00'))
            if (now - stamp).total_seconds() <= ((e.get('window_min') or cap) + cap) * 60:
                active[spawn or spec] = e
        elif e.get('type') in {'spawn-done', 'spawn-failed', 'spawn-stale', 'build-done', 'build-blocked'}:
            if not spawn and kind in {'build-done', 'build-blocked'}:
                active.clear()
            else:
                active.pop(spawn or spec, None)
    return bool(active)


def recut_refusal(events, spec, now):
    """Return the newest standing refusal using ledger order and UTC time only."""
    invalidators = {'branch-recut', 'build-started', 'build-done',
                    'spec-written', 'decision', 'unblocked'}
    for event in reversed(events):
        if event.get('subject') != spec:
            continue
        if event.get('type') in invalidators:
            return None
        if event.get('type') != 'branch-recut-refused':
            continue
        retry = event.get('retry_at')
        if retry and now >= datetime.fromisoformat(retry.replace('Z', '+00:00')):
            return None
        return {key: event.get(key) for key in ('reason', 'tip', 'retry_at')}
    return None


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


INVALIDATORS = frozenset({'branch-recut', 'build-started', 'build-done',
                          'spec-written', 'decision', 'unblocked'})
REWORK_REASONS = frozenset({'conflict', 'checks-failed', 'merge-gate'})


def rework(spec, branch, events, *, paths, tip, recut_reason, **facts):
    import conflict
    for event in reversed(events):
        if event.get('subject') != spec:
            continue
        if event.get('type') in INVALIDATORS:
            break
        if event.get('type') in {'branch-recut-refused', 'conflict-rework'} and event.get('tip') != tip:
            break
        if event.get('type') == 'conflict-rework' and event.get('tip') == tip and event.get('recut_reason') == recut_reason:
            return
    attempt = conflict.conflict_attempts(events, spec)
    emit('conflict-rework', spec, branch=branch, attempt=attempt.attempt,
         verdict=attempt.verdict, paths=paths, tip=tip, recut_reason=recut_reason, **facts)


def recovery_receipt(spec, branch, events, project, reason, tip, now, **facts):
    """Standing refusals and genuine rework have independent interval deduplication."""
    retry_at = None
    if reason == 'daily-limit':
        tomorrow = now.astimezone(timezone.utc).date() + timedelta(days=1)
        retry_at = datetime.combine(tomorrow, datetime.min.time(), timezone.utc).isoformat().replace('+00:00', 'Z')
    if reason not in {'open-builder-dispatch', 'adoption-state-changed', 'already-current'}:
        duplicate = False
        for event in reversed(events):
            if event.get('subject') != spec:
                continue
            if event.get('type') in INVALIDATORS:
                break
            if event.get('type') == 'branch-recut-refused':
                if event.get('tip') != tip:
                    break
                if event.get('reason') == reason and event.get('retry_at') == retry_at:
                    duplicate = True
                    break
        if not duplicate:
            emit('branch-recut-refused', spec, reason=reason, tip=tip, retry_at=retry_at,
                 owner='thinker', project=project, ts=now.isoformat())
    if reason in REWORK_REASONS:
        rework(spec, branch, events, tip=tip, recut_reason=reason, **facts)


def attempt_count(events, spec, now):
    day = str(now.astimezone(timezone.utc).date())
    return sum(e.get('type') == 'branch-refresh-skipped' and e.get('reason') == 'attempt-started'
               and e.get('subject') == spec
               and (e.get('day') or str(utc_day(e['ts']))) == day for e in events)


def pin_artifacts(repo, spec, old_base, base):
    """Rewrite only this subject's existing artifacts; retain byte/mode pre-images."""
    import re
    content = (fold.ROOT / 'content').resolve()
    paths = [content / (spec + '.md'), *sorted(content.glob('verify-' + spec + '-*.sh'))]
    snapshots = []
    try:
        for path in paths:
            if path.is_symlink() or path.resolve().parent != content:
                raise ValueError('artifact escapes subject content')
            if not path.is_file():
                continue
            original, mode = path.read_bytes(), path.stat().st_mode & 0o7777
            def replace(match):
                token = match.group()
                resolved = git(repo, 'rev-parse', '--verify', '--end-of-options', token + '^{commit}', check=False)
                return base if resolved.returncode == 0 and resolved.stdout.strip() == old_base else token
            written = re.sub(r'(?<![A-Za-z0-9_])[0-9a-fA-F]{7,40}(?![A-Za-z0-9_])',
                             replace, original.decode()).encode()
            snapshots.append((path, original, mode, written))
        for path, original, mode, written in snapshots:
            if path.is_symlink() or path.read_bytes() != original or path.stat().st_mode & 0o7777 != mode:
                raise ValueError('artifact changed before repin')
            path.write_bytes(written)
        return snapshots
    except Exception:
        restore_artifacts(snapshots)
        raise


def artifacts_unchanged(snapshots):
    return all(not path.is_symlink() and path.is_file() and path.read_bytes() == written
               and path.stat().st_mode & 0o7777 == mode for path, _, mode, written in snapshots)


def restore_artifacts(snapshots):
    for path, original, mode, written in snapshots:
        if artifacts_unchanged([(path, original, mode, written)]):
            path.write_bytes(original)
            path.chmod(mode)


def red_on_master(repo, main, tests):
    """Compare test files with main's bytes in an isolated, detached clone.

    A missing file, unsupported identifier or unavailable runner is absence of
    evidence, never a green baseline. Reuse preflight's runner/environment
    policy, including its total test budget and process-group timeout cleanup.
    """
    import re
    import spec_preflight as preflight
    repo = Path(repo).resolve()
    project = repo.name
    if (project not in {'do-it-v2', 'albert-scott'}
            or not isinstance(main, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', main)
            or not isinstance(tests, (list, tuple, set, frozenset))):
        return None
    if any(not isinstance(identifier, str) or not preflight.SAFE_TEST.fullmatch(identifier)
           or '::' in identifier for identifier in tests):
        return None
    identifiers = list(dict.fromkeys(tests))
    deadline = time.monotonic() + preflight.TEST_CAP_S
    try:
        with tempfile.TemporaryDirectory(prefix='red-on-master-') as name:
            allocation = Path(name)
            env = preflight._environment(allocation)
            tree = allocation / 'checkout'
            def run(argv, *, cwd=None, cap=120):
                remaining = min(cap, deadline - time.monotonic())
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, 0)
                return preflight._process(argv, cwd=cwd, env=env, timeout=remaining)
            resolved = run(['git', '-C', str(repo), 'rev-parse', '--verify',
                            '--end-of-options', main + '^{commit}'])
            if resolved.returncode or not re.fullmatch(r'[0-9a-f]{40}', resolved.stdout.strip()):
                return None
            base = resolved.stdout.strip()
            cloned = run(['git', '-c', 'core.hooksPath=' + os.devnull, 'clone',
                          '--local', '--no-hardlinks', '--no-checkout', str(repo), str(tree)])
            if cloned.returncode:
                return None
            detached = run(['git', '-c', 'core.hooksPath=' + os.devnull, 'checkout',
                            '--detach', base], cwd=tree)
            if detached.returncode:
                return None
            for identifier in identifiers:
                path = (tree / identifier).resolve()
                if not path.is_relative_to(tree.resolve()) or not path.is_file():
                    return None
            failures = []
            for identifier in identifiers:
                argv = (['/usr/bin/python3', identifier] if project == 'do-it-v2' else
                        ['/opt/albert-scott/.venv/bin/python', '-m', 'pytest', '-q',
                         '-p', 'no:cacheprovider', identifier])
                result = run(argv, cwd=tree, cap=preflight.TEST_CAP_S)
                # pytest reserves non-1 exits for collection/runner failures;
                # signals and script setup failures are also undetermined.
                if (result.returncode not in (0, 1)
                        or result.returncode and re.search(
                            r'(?m)^(?:ModuleNotFoundError|ImportError|SyntaxError):',
                            result.stdout + result.stderr)):
                    return None
                if result.returncode:
                    failures.append(identifier)
            return failures
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def children_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def adoption_cpu(started):
    delta = children_cpu() - started
    if not math.isfinite(delta) or delta < 0:
        raise ValueError('invalid child CPU counter delta')
    return delta


def grade_adoption(events, spec, branch):
    import grader_packet
    import evidence_revision
    build = grader_packet._latest(events, spec, 'build-done') or {}
    return dict(refresh_id=evidence_revision.new_invocation_id(), branch=branch.removeprefix('refs/heads/'),
                build_key=grader_packet.build_key(build),
                spec_sha256=evidence_revision.digest((fold.ROOT / 'content' / f'{spec}.md').read_text()))


def grade_terminal(spec, project, authority, after, base, bundle, gate):
    import hashlib
    facts = dict(**authority, to_sha=after, base_sha=base, gate=gate, project=project)
    if gate == 0:
        raw = Path(bundle).read_bytes()
        facts.update(bundle_sha256=hashlib.sha256(raw).hexdigest(),
                     bundle_key=json.loads(raw).get('bundle_key'))
    emit('branch-refresh-skipped', spec,
         reason='grade-refresh-failed' if gate else 'grade-refresh-success', **facts)


def _grade_build(spec):
    """Read completed-build admission with refresh's existing ledger reader."""
    import grader_packet
    return grader_packet._latest(ledger_cache.tail_refresh(), spec, 'build-done') or {}


MARKERS = ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD')
HOLD_REASONS = frozenset({'uncommitted-state', 'daily-limit', 'invalid-daily-limit', 'open-builder-dispatch',
                          'integrator-holds-branch', 'adoption-state-changed', 'invalid-branch', 'main-branch',
                          'rebase-undetermined', 'cherry-pick-undetermined', 'commit-shape-or-metadata'})
CONFLICT_REASONS = frozenset({'migration-or-fingerprint'})
PENDING_WAIT_S = 120
OUTCOME_WAIT_S = 10
_TASK_TERMINAL = ('grade-refresh-failed', 'grade-refresh-success')


def dirty_report(tree):
    """Read-only tracked/untracked path list and operation markers; never alters the tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    def read(*args):
        done = subprocess.run(['git', '--no-optional-locks', '-C', str(tree), *args], env=env,
                              capture_output=True, text=True, timeout=120)
        if done.returncode:
            raise subprocess.SubprocessError('git ' + args[0] + ' failed')
        return done.stdout
    fields = read('status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
    paths, skip = [], False
    for field in fields:
        if skip:
            skip = False
            continue
        if len(field) < 4:
            continue
        paths.append(field[3:])
        skip = field[0] in 'RC' or field[1] in 'RC'
    markers = []
    for name in MARKERS:
        path = Path(read('rev-parse', '--path-format=absolute', '--git-path', name).strip())
        if path.exists():
            markers.append(name)
    return dict(tree=str(tree), paths=paths, markers=markers)


def gate_outcome(events, spec, tip):
    """pending/failed/success for the refresh adoption of this exact tip; None if none."""
    receipt = None
    status = None
    for event in events:
        if event.get('subject') != spec:
            continue
        kind = event.get('type')
        if kind in INVALIDATORS:
            receipt = status = None
        elif kind == 'branch-refreshed' and event.get('to_sha') == tip and event.get('refresh_id'):
            receipt, status = event, 'pending'
        elif (receipt and kind == 'branch-refresh-skipped' and event.get('reason') in _TASK_TERMINAL
              and event.get('refresh_id') == receipt['refresh_id'] and event.get('to_sha') == tip):
            failed = event['reason'] == 'grade-refresh-failed' or event.get('gate')
            status = 'failed' if failed else ('failed' if status == 'failed' else 'success')
    return status


def _busy(repo, spec):
    common = Path(value(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    with (common / ('branch-refresh-' + spec + '.lock')).open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _concurrent_outcome(events, spec, tip):
    """What the attempt we waited on persisted for this tip, or None."""
    own = [e for e in events if e.get('subject') == spec]
    start = max((i for i, e in enumerate(own) if e.get('reason') == 'attempt-started' and e.get('tip') == tip),
                default=-1)
    for event in reversed(own[start + 1:]):
        if event.get('type') in INVALIDATORS:
            return None
        if event.get('type') == 'conflict-rework' and event.get('tip') == tip:
            return dict(state='conflict', reason=event.get('recut_reason') or 'conflict',
                        paths=event.get('paths') or [], to_sha=tip)
        if event.get('hold_state') and event.get('tip') == tip:
            return dict(state=event['hold_state'], reason=event.get('reason'), to_sha=tip)
    return None


def _file_ids(ids):
    import spec_preflight
    files = []
    for identifier in ids:
        name = str(identifier).split('::')[0]
        if not spec_preflight.SAFE_TEST.fullmatch(name):
            return None
        files.append(name)
    return files


def refresh_before_grade(repo, branch, main, spec, *, events=None, now=None, _again=False, _recut_first=False):
    """Checked refresh at the launch boundary; never launch a backend here."""
    import re
    import grader_packet
    repo = Path(repo).resolve()
    tip = None
    def stopped(reason, state='undetermined', **facts):
        extra = {k: facts[k] for k in ('failing_ids', 'red_on_master') if k in facts}
        emit('branch-refresh-skipped', spec, reason=reason, hold_state=state, tip=tip, **extra)
        return dict(state=state, reason=reason, **facts)
    def conflict(reason, **facts):
        return dict(state='conflict', reason=reason, to_sha=tip, **facts)
    if not isinstance(spec, str) or not re.fullmatch(r'L-spec-\d+', spec):
        raise ValueError('invalid refresh spec')
    try:
        if (not isinstance(branch, str) or not isinstance(main, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', main)):
            return stopped('invalid-branch')
        ref = branch if branch.startswith('refs/heads/') else 'refs/heads/' + branch
        if git(repo, 'check-ref-format', ref, check=False).returncode:
            return stopped('invalid-branch')
        # A caller arriving during another attempt waits for it and consumes
        # its persisted outcome; the reservation alone is never a verdict.
        contended = _busy(repo, spec)
        if contended:
            with lock(repo, spec):
                pass
        def read_history():
            fresh = ledger_cache.tail_refresh()
            return [*events, *fresh] if events is not None else fresh
        history = read_history()
        tip = value(repo, 'rev-parse', '--verify', ref + '^{commit}')
        base = value(repo, 'rev-parse', '--verify', '--end-of-options', main + '^{commit}')
        status = gate_outcome(history, spec, tip)
        deadline = time.monotonic() + PENDING_WAIT_S
        while status == 'pending' and time.monotonic() < deadline:
            contended = True
            time.sleep(0.2)
            history = read_history()
            status = gate_outcome(history, spec, tip)
        if status == 'pending':
            return stopped('gate-pending')
        if status == 'failed':
            return conflict('merge-gate')
        identity = None
        if git(repo, 'merge-base', '--is-ancestor', base, tip, check=False).returncode == 0:
            identity = grader_packet.grading_build(history, spec)
            if not grader_packet._same_sha(identity.get('ready_sha'), tip):
                return stopped('build-tip-mismatch')
            return dict(state='refreshed' if contended and status == 'success' else 'current',
                        to_sha=tip, base_sha=identity.get('base_sha'))
        if contended:
            # The first caller classifies after releasing the reservation:
            # give it a bounded moment to persist, never grade on silence.
            end = time.monotonic() + OUTCOME_WAIT_S
            found = _concurrent_outcome(history, spec, tip)
            while not found and time.monotonic() < end:
                time.sleep(0.2)
                history = read_history()
                found = _concurrent_outcome(history, spec, tip)
            return found or stopped('attempt-no-outcome')
        if _recut_first:
            # Completed-builder redispatch keeps the established recut (pins, backup tag).
            outcome = recut(repo, branch, main, spec, now=now, defer_checks=True)
        else:
            outcome = refresh(repo, branch, main, spec, events=events, now=now, defer_checks=True)
        if not _recut_first and outcome.get('state') == 'skipped' and outcome.get('reason') == 'multiple-commits':
            outcome = recut(repo, branch, main, spec, now=now, defer_checks=True)
        state, reason = outcome.get('state'), outcome.get('reason')
        if state in {'refreshed', 'recut'}:
            if outcome.get('gate') != 0:
                return conflict('merge-gate')
            if _recut_first:
                return {**outcome, 'state': 'refreshed'}
            fresh = ledger_cache.tail_refresh()
            ready = grader_packet.grading_build(fresh, spec)
            if grader_packet.bundle_for(fresh, spec, ready.get('ready_sha')) is None:
                return stopped('refreshed-evidence-unavailable')
            return {**outcome, 'state': 'refreshed'}
        if state == 'conflict':
            return conflict(reason or 'conflict', paths=outcome.get('paths') or [])
        if state == 'checks-failed':
            ids = list(outcome.get('failing_ids') or [])
            if ids == ['merge-gate']:
                return conflict('merge-gate')
            files = _file_ids(ids) if ids else None
            if files is None or {'checks-undetermined', 'checks-modified-tree'} & set(ids):
                return stopped('checks-undetermined', state='hold', failing_ids=ids)
            red = red_on_master(repo, main, sorted(set(files)))
            if red is None:
                return stopped('red-on-master-undetermined', state='hold', failing_ids=ids)
            if set(files) <= set(red):
                return stopped('failing-on-master', state='hold', failing_ids=ids, red_on_master=red)
            branch_only = [i for i, f in zip(ids, files) if f not in red]
            rework(spec, branch, history, paths=[], tip=tip, recut_reason='checks-failed',
                   failing_ids=branch_only, rebased_sha=outcome.get('rebased_sha'))
            return conflict('checks-failed', failing_ids=branch_only)
        if state == 'skipped' and reason in CONFLICT_REASONS:
            rework(spec, branch, history, paths=[], tip=tip, recut_reason='conflict', reason=reason)
            return conflict(reason)
        if state == 'skipped' and reason == 'already-current' and not _again:
            return refresh_before_grade(repo, branch, main, spec, events=events, now=now, _again=True,
                                        _recut_first=_recut_first)
        if state == 'skipped' and reason in HOLD_REASONS:
            facts = {}
            if reason == 'uncommitted-state':
                facts['dirty'] = [dirty_report(t) for t in holders(repo, ref)]
            return stopped(reason, state='hold', **facts)
        return stopped(reason or state or 'missing-outcome')
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        return stopped('refresh-undetermined')


def refresh(repo, branch, main, spec, *, events=None, now=None, defer_checks=False):
    """Return refreshed/conflict/checks-failed/skipped; never dispatch a role."""
    cpu_started = children_cpu()
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
        if open_builder(events, spec, now=now):
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
        count = attempt_count(persisted, spec, now)
        if count >= limit:
            return skip('daily-limit')
        # A committed ledger reservation counts conflicts/crashes as attempts too.
        emit('branch-refresh-skipped', spec, reason='attempt-started', project=project, day=str(day),
             tip=before, ts=now.isoformat())
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
                    rework(spec, branch, events, paths=paths, tip=before, recut_reason='conflict')
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
                    if failures != ['checks-undetermined'] and not defer_checks:
                        rework(spec, branch, events, paths=[], tip=before, recut_reason='checks-failed',
                               failing_ids=failures, rebased_sha=after)
                    return dict(state='checks-failed', failing_ids=failures, rebased_sha=after, reports=reports)
                # Recheck every adoption precondition after potentially long checks.
                if (value(repo, 'rev-parse', ref) != before or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec, now=now)
                        or holders(repo, ref) != holding or any(dirty(t) for t in holding)):
                    return skip('adoption-state-changed')
                for t in holding:
                    if value(t, 'rev-parse', 'HEAD') != before:
                        return skip('adoption-state-changed')
                if holding:
                    git(holding[0], 'reset', '--hard', after)
                else:
                    git(repo, 'update-ref', ref, after, before)
                authority = grade_adoption(events, spec, branch)
                emit('branch-refreshed', spec, spec=spec, project=project, from_sha=before,
                     to_sha=after, base_sha=base, checks='pass', cpu_s=adoption_cpu(cpu_started), **authority)
                import build_runner
                bundle = build_runner.run(spec, after)
                # Re-gating may itself refresh after another master move.
                fcntl.flock(reservation, fcntl.LOCK_UN)
                gate = regate(repo, branch, main, spec)
                if gate:
                    rework(spec, branch, ledger_cache.tail_refresh(), paths=[], tip=before,
                           recut_reason='merge-gate', failing_ids=['merge-gate'])
                grade_terminal(spec, project, authority, after, base, bundle, gate)
                return dict(state='refreshed', from_sha=before, to_sha=after, base_sha=base,
                            bundle=str(bundle), gate=gate, reports=reports)
            finally:
                if tree.exists():
                    git(tree, 'rebase', '--abort', check=False)
                    git(repo, 'worktree', 'remove', '--force', str(tree))
                git(repo, 'worktree', 'prune')


def recut(repo, branch, main, spec, *, now=None, defer_checks=False):
    """Replay non-merge patches on main, preserving the old tip before adoption."""
    cpu_started = children_cpu()
    import re
    import build_runner
    import merge_gate
    if not re.fullmatch(r'L-spec-\d+', spec):
        raise ValueError('invalid recut spec')
    now = now or datetime.now(timezone.utc)
    repo = Path(repo).resolve()
    merge_gate.begin_run(repo)
    # Refresh under reservation/adoption locks and after git actions: never
    # decide from the caller snapshot, but reuse validated cached history.
    with lock(repo, spec) as reservation:
        events = ledger_cache.tail_refresh()
        project = next((e['project'] for e in reversed(events) if e.get('subject') == spec and e.get('project')), repo.name)
        before = None
        def skip(reason, **facts):
            recovery_receipt(spec, branch, events, project, reason, before, now, paths=[], **facts)
            return dict(state='skipped', reason=reason)
        ref = branch if branch.startswith('refs/heads/') else 'refs/heads/' + branch
        if git(repo, 'check-ref-format', ref, check=False).returncode:
            return skip('invalid-branch')
        resolved = git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}', check=False)
        if resolved.returncode:
            return skip('invalid-branch')
        before = resolved.stdout.strip()
        if ref in {'refs/heads/master', 'refs/heads/main'} or ref == value(repo, 'rev-parse', '--symbolic-full-name', '--end-of-options', main):
            return skip('main-branch')
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
        if open_builder(events, spec, now=now):
            return skip('open-builder-dispatch')
        if any(t.resolve() == main_tree for t in holding) or len(holding) > 1:
            return skip('integrator-holds-branch')
        if any(dirty(t) for t in holding):
            return skip('uncommitted-state')
        day = str(now.astimezone(timezone.utc).date())
        limit = config().get('merge_queue', {}).get('branch_refresh_per_day', 3)
        if type(limit) is not int or limit < 1:
            return skip('invalid-daily-limit')
        if attempt_count(events, spec, now) >= limit:
            return skip('daily-limit')
        commits = value(repo, 'rev-list', '--reverse', '--topo-order', '--no-merges', base + '..' + before).splitlines()
        merges = int(value(repo, 'rev-list', '--count', '--merges', base + '..' + before))
        emit('branch-refresh-skipped', spec, reason='attempt-started', project=project, day=day,
             tip=before, ts=now.isoformat())
        with tempfile.TemporaryDirectory(prefix='branch-recut-', dir='/var/tmp') as name:
            tree, auxiliary = Path(name) / 'tree', Path(name) / 'checks'
            auxiliary.mkdir()
            tag = None
            snapshots, adopted = [], False
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
                        recovery_receipt(spec, branch, events, project, 'conflict', before, now, paths=paths)
                        return dict(state='conflict', paths=paths)
                after = value(tree, 'rev-parse', 'HEAD')
                count = int(value(tree, 'rev-list', '--count', base + '..HEAD'))
                old_base = value(repo, 'merge-base', before, base)
                pin = re.search(r'(?m)^(?:base_sha:|Base:)\s*[`\"\']?([0-9a-fA-F]{7,40})\b', text)
                if pin:
                    old_base = value(repo, 'rev-parse', '--verify', '--end-of-options', pin[1] + '^{commit}')
                try:
                    snapshots = pin_artifacts(repo, spec, old_base, base)
                    text = (fold.ROOT / 'content' / (spec + '.md')).read_text()
                    failures, reports = check_tree(spec, project, repo, tree, auxiliary, text)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, build_runner.Undetermined):
                    failures, reports = ['checks-undetermined'], []
                if (value(tree, 'rev-parse', 'HEAD') != after
                        or git(tree, 'diff', '--quiet', after, check=False).returncode):
                    failures.append('checks-modified-tree')
                if failures:
                    reason = 'checks-undetermined' if failures == ['checks-undetermined'] else 'checks-failed'
                    if not (defer_checks and reason == 'checks-failed'):
                        recovery_receipt(spec, branch, events, project, reason, before, now, paths=[], failing_ids=failures)
                    return dict(state='checks-failed', failing_ids=failures, reports=reports)
                if (value(repo, 'rev-parse', ref) != before or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec, now=now) or holders(repo, ref) != holding
                        or any(dirty(t) or value(t, 'rev-parse', 'HEAD') != before for t in holding)
                        or not artifacts_unchanged(snapshots)):
                    return skip('adoption-state-changed')
                tag, suffix = 'rework/' + spec.removeprefix('L-spec-') + '-prev', 1
                stem = tag
                while git(repo, 'show-ref', '--verify', '--quiet', 'refs/tags/' + tag, check=False).returncode == 0:
                    suffix += 1
                    tag = stem + '-' + str(suffix)
                git(repo, 'update-ref', 'refs/tags/' + tag, before, '')
                if (value(repo, 'rev-parse', ref) != before or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec, now=now)
                        or holders(repo, ref) != holding or not artifacts_unchanged(snapshots)
                        or any(dirty(t) or value(t, 'rev-parse', 'HEAD') != before for t in holding)):
                    return skip('adoption-state-changed')
                try:
                    if holding:
                        git(holding[0], 'reset', '--hard', after)
                    else:
                        git(repo, 'update-ref', ref, after, before)
                except subprocess.SubprocessError:
                    return skip('adoption-state-changed')
                adopted = True
                # Adoption accounting precedes the terminal gate: even a later
                # rollback consumed this CPU and must retain its audit receipt.
                authority = grade_adoption(events, spec, branch)
                emit('branch-refreshed', spec, spec=spec, project=project, from_sha=before,
                     to_sha=after, base_sha=base, checks='pass', cpu_s=adoption_cpu(cpu_started), **authority)
                try:
                    bundle = build_runner.run(spec, after)
                    fcntl.flock(reservation, fcntl.LOCK_UN)
                    gate = regate(repo, branch, main, spec)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, build_runner.Undetermined):
                    gate = 1
                if gate:
                    grade_terminal(spec, project, authority, after, base, None, gate)
                    fcntl.flock(reservation, fcntl.LOCK_EX)
                    fresh = ledger_cache.tail_refresh()
                    if (value(repo, 'rev-parse', ref) == after and holders(repo, ref) == holding
                            and not open_builder(fresh, spec, now=now)
                            and not any(dirty(t) or value(t, 'rev-parse', 'HEAD') != after for t in holding)):
                        if holding:
                            git(holding[0], 'reset', '--hard', before)
                        else:
                            git(repo, 'update-ref', ref, before, after)
                        adopted = False
                    recovery_receipt(spec, branch, fresh, project, 'merge-gate', before, now, paths=[], failing_ids=['merge-gate'])
                    return dict(state='checks-failed', failing_ids=['merge-gate'])
                fcntl.flock(reservation, fcntl.LOCK_EX)
                if (value(repo, 'rev-parse', ref) != after or value(repo, 'rev-parse', main) != base
                        or open_builder(ledger_cache.tail_refresh(), spec, now=now)
                        or holders(repo, ref) != holding or not artifacts_unchanged(snapshots)
                        or any(dirty(t) or value(t, 'rev-parse', 'HEAD') != after for t in holding)):
                    return skip('adoption-state-changed')
                facts = dict(from_sha=before, to_sha=after, tag=tag, commit_count=count,
                             skipped_merges=merges, dropped_empty=len(commits) - count,
                             old_base_sha=old_base, base_sha=base)
                emit('branch-recut', spec, spec=spec, project=project, **facts)
                grade_terminal(spec, project, authority, after, base, bundle, gate)
                return dict(state='recut', bundle=str(bundle), gate=gate, reports=reports, **facts)
            finally:
                if not adopted:
                    restore_artifacts(snapshots)
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
    operation_time = datetime.now(timezone.utc)
    # Initial, unbuilt branches retain the existing builder admission contract.
    completed = any(e.get('subject') == a.subject and e.get('type') == 'build-done'
                    and e.get('status') == 'DONE' for e in events)
    # An applicable build-timeout is incomplete work: never adopted, recut or re-gated.
    if not completed or __import__('wip_resume').applicable_timeout(events, a.subject):
        return False
    if git(cwd, 'merge-base', '--is-ancestor', a.effective_base_sha, 'HEAD', check=False).returncode == 0:
        return True
    try:
        import packet_base
        repo = runtime.ROOT / 'repos' / a.project
        main = packet_base._effective_branch(repo, a.project, runtime.spec_path(a.subject), a.subject)
        outcome = refresh_before_grade(repo, a.subject.lower(), main, a.subject, now=operation_time,
                                       _recut_first=True)
    except (OSError, ValueError, subprocess.SubprocessError, __import__('merge_gate').Undetermined):
        outcome = None
    state = (outcome or {}).get('state')
    if state == 'current':
        return True
    if state == 'refreshed':
        fail('branch-recut: branch checked, adopted and re-gated; no builder needed', reason='branch-recut')
    if state in {'hold', 'undetermined'}:
        fail('branch-not-descendant: held, not a conflict (' + str(outcome.get('reason')) + '); no builder dispatched',
             reason='branch-not-descendant')
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
