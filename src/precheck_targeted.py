"""Change-keyed coupled tests on precheck's disposable combined trees."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import coupled_tests
from merge_gate import Undetermined
import cpu_share
import fold
import test_slots

TARGETED_SECONDS = 600
SCAN_SECONDS = 300
NODE_MODULES = Path('/opt/albert-scott/dashboard/node_modules')
FIELDS = ('targeted', 'targeted_key', 'targeted_red', 'targeted_unattributed', 'targeted_unscanned')


@contextmanager
def bounded(seconds):
    """Bound injected runners too; preserve the enclosing CLI's alarm deadline."""
    if seconds <= 0:
        raise TimeoutError('targeted deadline exceeded')
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    def expired(signum, frame):
        raise TimeoutError('targeted deadline exceeded')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0]) if previous_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


def runner(tree, files, *, dashboard, timeout):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', DOIT_NO_POKE='1', PYTHONPATH='.:api')
    if dashboard:
        if not NODE_MODULES.is_dir():
            raise RuntimeError('dashboard node_modules unavailable')
        cwd = tree / 'dashboard'
        modules = cwd / 'node_modules'
        if not modules.exists():
            modules.symlink_to(NODE_MODULES, target_is_directory=True)
        argv = ['npx', 'vitest', 'run', *[p.removeprefix('dashboard/') for p in files]]
    else:
        cwd = tree
        argv = ['/opt/albert-scott/.venv/bin/python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *files]
    admission = test_slots.acquire('sweep', timeout, root=fold.ROOT) if not dashboard else nullcontext()
    with admission:
        if not dashboard:
            env = cpu_share.launch_env(env, fold.ROOT)
        result = subprocess.run(['nice', '-n', '10', 'timeout', str(min(300, timeout)), *argv], cwd=cwd, env=env,
                                timeout=min(300, timeout), capture_output=True, text=True)
    output = result.stdout + '\n' + result.stderr
    if result.returncode == 0:
        return []
    if dashboard:
        # Vitest's default reporter prints FAIL <relative file> > <test name>.
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        nodes = [f'dashboard/{file}::{name.strip()}' if name else f'dashboard/{file}'
                 for file, name in re.findall(r'^\s*FAIL\s+([^\s]+\.test\.tsx?)(?:\s+>\s+([^\n]+))?', output, re.M)]
    else:
        nodes = re.findall(r'^(?:FAILED|ERROR) ([^\s]+)', output, re.M)
    if result.returncode == 1 and nodes:
        return list(dict.fromkeys(nodes))
    raise RuntimeError('targeted runner undetermined')


def run(repo, master, clean, shas, previous, content, *, chain, check_tree, test_runner=None):
    key = hashlib.sha256(json.dumps([master, [(r['spec'], shas[r['spec']]) for r in clean]],
                                    separators=(',', ':')).encode()).hexdigest()
    result = dict(targeted='skipped-empty', targeted_key=key, targeted_red={},
                  targeted_unattributed={}, targeted_unscanned={})
    if previous and previous.get('targeted_key') == key:
        result.update({f: previous[f] for f in FIELDS if f in previous})
        result['targeted'] = 'unchanged'
        return result
    if not clean:
        return result
    combined = chain(repo, master, clean, shas)[-1]
    owners = {}
    scan_deadline = time.monotonic() + SCAN_SECONDS
    for row in clean:
        sid = row['spec']
        try:
            with bounded(scan_deadline - time.monotonic()):
                text = (content / f'{sid}.md').read_text()
                scanned = coupled_tests.scan(text, repo, rev=combined)
            for tier, source, file, fenced in scanned:
                if tier in ('named', 'referencing'):
                    owners.setdefault(file, [])
                    if sid not in owners[file]:
                        owners[file].append(sid)
        except (coupled_tests.Truncated, ValueError, Undetermined, OSError, RuntimeError,
                subprocess.SubprocessError) as exc:
            result['targeted_unscanned'][sid] = type(exc).__name__
    test_runner = test_runner or runner
    deadline = time.monotonic() + TARGETED_SECONDS
    reruns = 0

    def execute(commit, files, dashboard):
        def extracted(tree):
            present = sorted(f for f in files if (tree / f).is_file())
            if not present:
                return [], False
            remaining = deadline - time.monotonic()
            with bounded(remaining):
                nodes = test_runner(tree, present, dashboard=dashboard, timeout=remaining)
            if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
                raise RuntimeError('invalid targeted runner response')
            return list(dict.fromkeys(nodes)), True
        with bounded(deadline - time.monotonic()):
            return check_tree(repo, commit, extracted)

    ran, undetermined = False, False
    for dashboard in (False, True):
        files = [f for f in owners if (f.startswith('dashboard/') and
                 f.endswith(('.test.ts', '.test.tsx'))) == dashboard and
                 (dashboard or f.endswith('.py'))]
        if not files:
            continue
        try:
            nodes, did_run = execute(combined, files, dashboard)
            ran |= did_run
            if not nodes:
                continue
            # One master-only run per language, restricted to failing files.
            failing_files = list(dict.fromkeys(n.split('::', 1)[0] for n in nodes))
            baseline, _ = execute(master, [f for f in failing_files if f in files], dashboard)
            nodes = [n for n in nodes if n not in baseline]
            for file in dict.fromkeys(n.split('::', 1)[0] for n in nodes):
                failures = [n for n in nodes if n.split('::', 1)[0] == file]
                candidates = owners.get(file, [])
                culprit = candidates[0] if len(candidates) == 1 else None
                if len(candidates) > 1 and reruns + len(candidates) <= 3:
                    green_without = []
                    for sid in candidates:
                        rest = [r for r in clean if r['spec'] != sid]
                        tip = chain(repo, master, rest, shas)[-1] if rest else master
                        reruns += 1
                        probe, _ = execute(tip, [file], dashboard)
                        if not [n for n in probe if n not in baseline]:
                            green_without.append(sid)
                    if len(green_without) == 1:
                        culprit = green_without[0]
                if culprit:
                    result['targeted_red'].setdefault(culprit, []).extend(failures)
                else:
                    result['targeted_unattributed'].update({n: candidates for n in failures})
        except Exception:
            # Runner crashes, including injected runner errors, preserve selection.
            undetermined = True
    if undetermined:
        result['targeted_red'] = {}
    result['targeted'] = ('undetermined' if undetermined else 'red' if result['targeted_red'] or
                          result['targeted_unattributed'] else 'green' if ran else 'skipped-empty')
    return result
