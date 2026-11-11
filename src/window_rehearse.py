"""Bounded CI rehearsals on Git objects; no window ownership or disposition."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

import fold
import grading_env
import quarantine_file
import window_attribution
import window_gate as gate
import window_prewindow
import window_dispatch
import window_known_red
import test_slots


def capacity(env=None):
    """Batch capacity is separate from the test-session scheduler's capacity."""
    value = (os.environ if env is None else env).get('DOIT_REHEARSAL_BATCHES', '3')
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('DOIT_REHEARSAL_BATCHES must be a positive integer')
    return int(value)


@contextmanager
def slot(root, project):
    """Configured independent kernel-held slots; process death releases ownership."""
    if not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..'):
        raise ValueError('invalid rehearsal project')
    directory = Path(root) / 'state'
    directory.mkdir(parents=True, exist_ok=True)
    fd, selected = None, None
    try:
        for index in range(capacity()):
            handle = os.open(directory / f'rehearsal.{project}.{index}.lock',
                             os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(handle)
                continue
            fd, selected = handle, index
            break
        yield selected
    finally:
        if fd is not None:
            os.close(fd)


def alembic_heads(repo, candidate, project):
    """Inspect the exact candidate from api/, with the project's interpreter."""
    reader = window_prewindow.Trees(repo)
    paths = reader.read(candidate)
    if not any(p.startswith(gate.rechain.VERSIONS) for p in paths):
        return
    window_prewindow.candidate_revisions(reader, paths, True)
    if 'api/alembic_supabase.ini' not in paths:
        raise ValueError('Alembic configuration missing at api/alembic_supabase.ini')
    venv = grading_env.project_venv(project)
    interpreter = Path(venv) / 'bin/python' if venv else Path(sys.executable)
    if not interpreter.is_absolute():
        interpreter = Path(repo) / interpreter
    with tempfile.TemporaryDirectory(prefix='window-rehearse-heads-') as directory:
        root = Path(directory)
        tree = root / 'tree'
        added = False
        try:
            gate.git(repo, 'worktree', 'add', '--detach', str(tree), candidate)
            added = True
            env = gate.rechain.child_env()
            env.update(HOME=str(root), XDG_CONFIG_HOME=str(root), TMPDIR=str(root),
                       PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(tree / 'api') + os.pathsep + str(tree))
            proc = subprocess.Popen([str(interpreter), '-m', 'alembic', '-c', 'alembic_supabase.ini', 'heads'],
                                    cwd=tree / 'api', env=env, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                out, _ = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise ValueError('Alembic heads timed out') from None
            heads = [line.strip() for line in out.splitlines() if line.strip()]
            if proc.returncode or len(heads) != 1 or '(head)' not in heads[0]:
                raise ValueError('Alembic must report exactly one head')
        finally:
            if added:
                gate.git(repo, 'worktree', 'remove', '--force', str(tree))


def prepare(repo, ident, base, entries, project, main, events):
    """Share the real merge/fingerprint/policy builders without their ref writes."""
    seen, migrations = {}, []
    for spec, _, sha in entries:
        changed = gate.git(repo, 'diff', '--name-only', '-z', f'{base}...{sha}').stdout.split('\0')
        for path in filter(None, changed):
            if path in seen:
                raise ValueError(f'shared file between {seen[path]} and {spec}: {path}')
            seen[path] = spec
        if any(p.startswith(gate.rechain.VERSIONS) and p.endswith('.py')
               and '/_archive/' not in p for p in changed):
            migrations.append(spec)
    if len(migrations) > 1:
        raise ValueError('at most one migration spec per rehearsal')
    admitted, rejected = window_prewindow.select(repo, base, main, entries, events,
                                                fold.ROOT / 'content', on=base)
    if rejected or admitted != entries:
        reasons = '; '.join(spec[0] + ': ' + why for spec, why in rejected)
        raise ValueError('prewindow admission incomplete' + (': ' + reasons if reasons else ' (deferred specs)'))
    silent = lambda *args, **kwargs: None
    candidate, kept, conflicts = gate.build_candidate(repo, ident, base, entries, publish=False, emit=silent)
    if conflicts or kept != [s for s, _, _ in entries]:
        raise ValueError('merge conflict in rehearsal: ' + ', '.join(conflicts))
    alembic_heads(repo, candidate, project)
    if (os.environ.get('DOIT_WINDOW_RECHAIN', 'on') == 'on'
            and any(len(gate.rechain.added_versions(repo, base, sha)) == 1 for _, _, sha in entries)):
        candidate = gate.fingerprint_candidate(repo, candidate, ident, project, publish=False, emit=silent)
    return quarantine_file.candidate(repo, candidate, events, project)


def diagnose(repo, candidate, base, entries, result, main):
    failures = gate.candidate_failures(repo, result)
    ids = sorted(set().union(*(failures[k] for k in ('pytest', 'pg-parity', 'vitest', 'suite-error'))))
    if failures['undetermined'] or not ids:
        return dict(ids=ids, flakes=[], unresolved=ids, why='unnamed or unavailable CI evidence')
    # One exact-tree rerun of only F. Absent/unknown is never a passing rerun.
    retry = window_attribution.probe(repo, candidate, ids)
    states = retry.get('states', {})
    flakes = [node for node in ids if states.get(node) == 'green']
    reproduced = [node for node in ids if states.get(node) == 'red']
    unresolved = [node for node in ids if states.get(node) not in ('green', 'red')]
    for node in flakes:
        print('FLAKE ' + node)
    plan = {}
    if reproduced:
        plan = gate.targeted_attribution(repo, base, entries, reproduced, main=main, on=base)
        for spec, nodes in plan.get('proven', {}).items():
            for node in nodes:
                print('ATTRIBUTED ' + spec + ' ' + node)
        for spec, nodes in plan.get('drop', {}).items():
            for node in nodes:
                if node not in plan.get('proven', {}).get(spec, []):
                    print('FALLBACK ' + spec + ' ' + node)
        for node in plan.get('environment', []):
            print('ENVIRONMENT ' + node)
        unresolved = sorted(set(unresolved + plan.get('unresolved', [])))
    for node in unresolved:
        print('UNRESOLVED ' + node)
    return dict(ids=ids, flakes=flakes, unresolved=unresolved, retry=retry, attribution=plan)


def prepared_candidate(repo, ident, project, base, entries, events):
    """Consume only a matching admitted launcher preparation, never a verdict.

    Branch identities bind the handoff to the queue version the launcher saw.
    The worker still computes scope, checks reuse and acquires a test session.
    """
    receipts = [event for event in events if event.get('type') == 'window-rehearsed'
                and event.get('subject') == ident and event.get('project') == project
                and event.get('launcher_prepared') is True
                and ('actor' not in event or fold.admits(event, event['actor']) is None)]
    if not receipts:
        return None
    receipt = receipts[-1]
    expected = [list(entry) for entry in entries]
    if (receipt.get('base_sha') != base or receipt.get('entries') != expected
            or receipt.get('specs') != [entry[0] for entry in entries]
            or receipt.get('state') != 'prepared'):
        raise ValueError('launcher preparation no longer matches rehearsal')
    candidate = receipt.get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', candidate):
        raise ValueError('launcher candidate unavailable')
    if gate.tree_of(repo, candidate) != receipt.get('tree'):
        raise ValueError('launcher candidate tree differs')
    if gate.git(repo, 'merge-base', '--is-ancestor', base, candidate, check=False).returncode:
        raise ValueError('launcher candidate does not descend from intended base')
    return candidate


def _cancel_verification(signum, frame):
    # gate.verify catches this exception and stops its separately sessioned
    # predeploy process, whose trap cancels only its bound hosted run IDs.
    raise gate.TrainCancelled('rehearsal worker cancelled')


def run(ident, project, specs, on, main):
    try:
        return _run(ident, project, specs, on, main)
    except (OSError, ValueError):
        print('gate-window: rehearsal capacity state unavailable')
        gate.ev('window-rehearsed', ident, base_sha='unresolved', specs=[s.partition(':')[0] for s in specs],
                state='undetermined', rehearsal=True, project=project, why='rehearsal capacity state unavailable')
        return 2


def _run(ident, project, specs, on, main):
    repo = fold.ROOT / 'repos' / project
    base, members = 'unresolved', [s.partition(':')[0] for s in specs]
    entries = []
    def emit(state, **fields):
        gate.ev('window-rehearsed', ident, base_sha=base, specs=members, state=state,
                rehearsal=True, project=project, entries=[list(entry) for entry in entries], **fields)
    with slot(fold.ROOT, project) as held:
        if held is None:
            print('gate-window: rehearsal capacity reached for ' + project)
            emit('refused', reason='rehearsal-cap')
            return 3
        try:
            if not re.fullmatch(r'[0-9a-fA-F]{7,40}', on):
                raise ValueError('--on must identify a commit SHA')
            base = gate.git(repo, 'rev-parse', '--verify', '--end-of-options', on + '^{commit}').stdout.strip()
            if len(set(members)) != len(members) or not members:
                raise ValueError('rehearsal requires distinct specs')
            if len(members) > gate.max_merges(os.environ):
                raise ValueError('rehearsal exceeds the current window cap')
            events = fold.read_events()
            entries = gate.resolve_entries(repo, specs, events)
            candidate = prepared_candidate(repo, ident, project, base, entries, events)
            if candidate is None:
                candidate = prepare(repo, ident, base, entries, project, main, events)
            required = gate.scope(repo, base, candidate, gate.required_checks(repo, base))['checks']
            tree = gate.tree_of(repo, candidate)
            prior = gate.prior_result(events, tree)
            # Publish the immutable identity before waiting for a test session.
            # Successors can form against it without waiting for CI to finish.
            emit('prepared', sha=candidate, tree=tree, required=required)
            if gate.covers(prior, required):
                result = dict(state='green', nodes=[], url='', doc={})
                reused = True
            else:
                with test_slots.acquire('rehearsal', None, root=fold.ROOT, spec=ident):
                    emit('running', sha=candidate, tree=tree, required=required)
                    previous = signal.signal(signal.SIGTERM, _cancel_verification)
                    try:
                        with window_dispatch.managed(tree, known=dict(project=project, base_sha=base, specs=members)):
                            result = gate.verify(repo, candidate, required, ident, rehearsal=True)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
                reused = False
            state = result['state']
            if state not in ('green', 'red', 'undetermined'):
                raise ValueError('invalid rehearsal verdict')
            diagnosis = {}
            if state == 'red':
                diagnosis = diagnose(repo, candidate, base, entries, result, main)
                gate.record_flakes(ident, project, candidate, diagnosis['flakes'],
                                   diagnosis.get('retry', {}), 'rehearsal')
                if (diagnosis['ids'] and diagnosis['flakes'] == diagnosis['ids']
                        and not diagnosis['unresolved']):
                    state = 'green'
                    diagnosis['flake_only'] = True
            elif reused and prior.get('flakes'):
                diagnosis = dict(flakes=prior['flakes'], flake_only=prior.get('flake_only', False))
            if state != 'undetermined' and not reused:
                gate.ev('window-tree-verified', ident, window=ident, sha=candidate, tree=tree,
                        green=state == 'green', required=required, new_ids=result.get('nodes', []),
                        rehearsal=True, base_sha=base, project=project,
                        **({k: diagnosis[k] for k in ('flakes', 'flake_only') if k in diagnosis}))
            if not result.get('skipped_known'):
                ids, flakes = diagnosis.get('ids', []), diagnosis.get('flakes', [])
                unresolved = state == 'red' and bool(diagnosis.get('unresolved'))
                window_known_red.receipt(gate, ident, project=project, base_sha=base, sha=candidate, tree=tree,
                    required=required, state='undetermined' if unresolved else state, failing_ids=ids, flakes=flakes,
                    new_ids=[i for i in ids if i not in flakes] if state == 'red' else [])
            if state == 'undetermined' and 'why' in result:
                import prod_runner
                diagnosis['why'] = prod_runner.redact(result['why'])[:500]
            emit(state, sha=candidate, tree=tree, required=required, reused=reused,
                 run_url=result.get('url', ''), **diagnosis)
            verdict = state + (f" (flakes: {len(diagnosis['flakes'])})" if diagnosis.get('flake_only') else '')
            print(f'REHEARSED {ident} {verdict} base={base} candidate={candidate} tree={tree}')
            return {'green': 0, 'red': 1, 'undetermined': 2}[state]
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, gate.TrainCancelled) as exc:
            # Keep diagnostics bounded and credential-free even for external tools.
            import prod_runner
            why = prod_runner.redact(str(exc))[:500]
            print('gate-window: rehearsal undetermined: ' + why)
            emit('undetermined', why=why)
            return 2
