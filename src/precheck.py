"""Precheck the queued merge set using dangling git objects and tree guards."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import dispatch
import fold
import merge_queue
import window_gate
import precheck_targeted
import cpu_share
import test_slots
import rechain

DOIT = str(Path(__file__).resolve().parent.parent / 'doit')


def git(repo, *args, check=True, inp=None):
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True,
                          input=inp, check=check, timeout=60)


def project_name(project):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..'):
        raise ValueError('invalid precheck project')
    return project


@contextmanager
def lock(project):
    path = fold.ROOT / 'state' / f'precheck.{project_name(project)}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def master_sha(repo, master):
    return git(repo, 'rev-parse', '--verify', f'{master}^{{commit}}').stdout.strip()


def latest(events, project):
    return next((e for e in reversed(merge_queue._ordered(events))
                 if e.get('type') == 'precheck' and e.get('project') == project), None)


def fresh(event, master, now, minutes):
    try:
        stamp = datetime.fromisoformat(event['ts'].replace('Z', '+00:00'))
        age = (now - stamp).total_seconds()
        return event['master'] == master and 0 <= age <= minutes * 60
    except (TypeError, KeyError, ValueError):
        return False


def merge(repo, left, right, *, entry=None):
    result = git(repo, 'merge-tree', '--write-tree', left, right, check=False)
    if result.returncode == 1:
        # Stage records precede the human-readable conflict diagnostics.
        paths = re.findall(r'^\d+ [0-9a-f]+ [123]\t(.+)$', result.stdout, re.M)
        if not paths:
            raise RuntimeError('merge conflict path unavailable')
        if entry is not None and os.environ.get("DOIT_WINDOW_RECHAIN", "on") == "on":
            try:
                commit, _ = rechain.resolve(repo, left, entry, "precheck", ev=lambda *a, **kw: None)
                if commit:
                    return commit, None
            except Exception:
                pass
        return None, paths[0]
    if result.returncode:
        raise RuntimeError('merge-tree undetermined')
    tree = result.stdout.splitlines()[0]
    if not re.fullmatch(r'[0-9a-f]{40,64}', tree):
        raise RuntimeError('merge tree unavailable')
    commit = git(repo, *window_gate.IDENT, 'commit-tree', tree, '-p', left, '-p', right,
                 inp='precheck candidate\n').stdout.strip()
    return commit, None


def chain(repo, master, rows, shas):
    tip, prefixes = master, []
    for row in rows:
        tip, conflict = merge(repo, tip, shas[row['spec']],
                              entry=(row['spec'], row['branch'], shas[row['spec']]))
        if conflict:
            raise RuntimeError('precheck remainder conflicts')
        prefixes.append(tip)
    return prefixes


def guard_runner(repo):
    python = repo / '.venv/bin/python'
    if not python.is_file():
        raise RuntimeError('project venv unavailable')
    guards = window_gate.tree_guards(os.environ)
    def run_tree(tree):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', DOIT_NO_POKE='1')
        env.pop('PYTHONPATH', None)
        with test_slots.acquire('sweep', 300, root=fold.ROOT):
            env = cpu_share.launch_env(env, fold.ROOT)
            result = subprocess.run(['nice', '-n', '10', 'timeout', '300', str(python), '-m', 'pytest', '-q',
                                     *[g.removesuffix('::') for g in guards]],
                                    cwd=tree, env=env, text=True, capture_output=True, timeout=300)
        output = result.stdout + '\n' + result.stderr
        nodes = list(dict.fromkeys(re.findall(r'^(?:FAILED|ERROR) ([^\s]+)', output, re.M)))
        if result.returncode == 0:
            return []
        if result.returncode == 1 and nodes:
            return nodes
        raise RuntimeError('tree guards undetermined')
    return run_tree


def check_tree(repo, commit, runner):
    # Every extraction, including bisection probes, has its own cleanup scope.
    with tempfile.TemporaryDirectory(prefix='doit-precheck-', dir='/var/tmp') as name:
        tree = Path(name) / 'tree'
        tree.mkdir()
        with tempfile.TemporaryFile(dir=name) as stream:
            subprocess.run(['git', 'archive', '--format=tar', commit], cwd=repo,
                           stdout=stream, stderr=subprocess.PIPE, check=True, timeout=60)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as bundle:
                bundle.extractall(tree, filter='data')
        return runner(tree)


def emitter(project, ledger_file):
    if Path(ledger_file).name != ledger_file or fold.actor_of(ledger_file) not in ('tick', 'executor', 'operator'):
        raise ValueError('unsupported precheck ledger actor')
    dst = fold.ROOT / 'events' / ledger_file
    dst.parent.mkdir(parents=True, exist_ok=True)
    def emit(subject, kind, **fields):
        reason = dispatch.emit(dst, {'subject': subject}, kind, strict=True, project=project, **fields)
        if reason:
            raise RuntimeError(reason)
        return json.loads(dst.read_text().splitlines()[-1])
    return emit


def run(project, *, events=None, guard_runner=None, emit=None, now=None):
    project_name(project)
    with lock(project) as owned:
        if not owned:
            raise RuntimeError('precheck lock held')
        started = time.monotonic()
        now = now or datetime.now(timezone.utc)
        repo, main = merge_queue._repository(project)
        master = master_sha(repo, main)
        events = window_gate.train_events() if events is None else events
        rows = merge_queue.queue(events, project)
        emit = emit or emitter(project, os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl'))
        result = dict(master=master, clean=[], conflict_master={}, conflict_set={},
                      guard_red={}, guards='skipped-empty')
        shas, clean = {}, []
        tip = master
        for row in rows:
            sid = row['spec']
            sha = shas[sid] = master_sha(repo, row['branch'])
            _, path = merge(repo, master, sha, entry=(sid, row['branch'], sha))
            if path:
                result['conflict_master'][sid] = path
                continue
            candidate, path = merge(repo, tip, sha, entry=(sid, row['branch'], sha))
            if path:
                result['conflict_set'][sid] = path
            else:
                tip = candidate
                clean.append(row)
        if clean:
            runner = guard_runner or globals()['guard_runner'](repo)
            attributions = 0
            while clean:
                prefixes = chain(repo, master, clean, shas)
                nodes = check_tree(repo, prefixes[-1], runner)
                if not nodes:
                    result['guards'] = 'green'
                    break
                result['guards'] = 'red-unattributed'
                if attributions >= 3 or check_tree(repo, master, runner):
                    clean = []
                    break
                lo, hi = 0, len(clean) - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    if check_tree(repo, prefixes[mid], runner):
                        hi = mid
                    else:
                        lo = mid + 1
                culprit_nodes = check_tree(repo, prefixes[lo], runner)
                if not culprit_nodes:
                    clean = []
                    break
                result['guard_red'][clean[lo]['spec']] = culprit_nodes
                clean.pop(lo)
                attributions += 1
                if not clean:
                    result['guards'] = 'skipped-empty'
        result['clean'] = [r['spec'] for r in clean]
        result['seconds'] = round(time.monotonic() - started, 3)
        for kind, field in (('merge-conflict', 'conflict_master'), ('precheck-red', 'guard_red')):
            for sid, detail in result[field].items():
                if any(e.get('type') == kind and e.get('subject') == sid
                       and e.get('branch_sha') == shas[sid] and e.get('master') == master for e in events):
                    continue
                fields = dict(source='precheck', path=detail) if kind == 'merge-conflict' else dict(tests=detail)
                emit(sid, kind, master=master, branch_sha=shas[sid], **fields)
        event = emit('precheck:' + project, 'precheck', **result)
        return dict(event, project=project)


PREGRADE_MAX = 5


@contextmanager
def pregrade_lock():
    path = fold.ROOT / 'state/precheck-pregrade.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def pregrade(project, *, events=None, guard_runner=None, targeted_runner=None, emit=None, now=None):
    """Check newest builds solo; absence or uncertainty never blocks grading."""
    project_name(project)
    with pregrade_lock() as owned:
        if not owned:
            return []
        events = window_gate.train_events() if events is None else list(events)
        repo, main = merge_queue._repository(project)
        master = master_sha(repo, main)
        emit = emit or emitter(project, os.environ.get('DOIT_LEDGER_FILE', 'L-executor-0001.jsonl'))
        latest_build = {}
        for event in merge_queue._ordered(events):
            if event.get('type') == 'build-done' and event.get('project') == project and event.get('ready_sha'):
                latest_build[event['subject']] = event
        seen = {(e.get('subject'), e.get('branch_sha'), e.get('master')) for e in events
                if e.get('type') == 'precheck-pregrade'}
        builds = sorted(latest_build.values(), key=lambda e: (e['ts'], e['subject']))
        results = []
        with precheck_targeted.bounded(1200):
            for build in [b for b in builds if (b['subject'], b['ready_sha'], master) not in seen][:PREGRADE_MAX]:
                started = time.monotonic()
                sid, sha = build['subject'], build['ready_sha']
                verdict, detail = 'undetermined', {}
                try:
                    candidate, path = merge(repo, master, sha)
                    if path:
                        verdict, detail = 'conflict', dict(path=path)
                        if os.environ.get("DOIT_WINDOW_RECHAIN", "on") == "on":
                            try:
                                rechained, _ = rechain.resolve(repo, master, (sid, build.get('branch', sha), sha),
                                                               "precheck", ev=lambda *a, **kw: None)
                                if rechained:
                                    verdict, detail = 'undetermined', {}
                            except Exception:
                                pass
                    else:
                        runner = guard_runner or globals()['guard_runner'](repo)
                        with precheck_targeted.bounded(300):
                            failures = check_tree(repo, candidate, runner)
                        if failures:
                            with precheck_targeted.bounded(300):
                                before = check_tree(repo, master, runner)
                            failures = [n for n in failures if n not in before]
                        row = dict(spec=sid, branch=sha)
                        target = precheck_targeted.run(repo, master, [row], {sid:sha}, None,
                            fold.ROOT / 'content', chain=lambda *args:[candidate],
                            check_tree=check_tree, test_runner=targeted_runner)
                        if (target['targeted'] == 'undetermined' or target['targeted_unscanned']
                                or target['targeted_unattributed']):
                            raise RuntimeError('coupled checks undetermined')
                        failures = list(dict.fromkeys(failures + target['targeted_red'].get(sid, [])))
                        verdict, detail = ('red', dict(tests=failures)) if failures else ('clean', {})
                except (OSError, RuntimeError, ValueError, TimeoutError, KeyError,
                        subprocess.SubprocessError, tarfile.TarError):
                    verdict, detail = 'undetermined', {}
                if verdict == 'conflict':
                    emit(sid, 'merge-conflict', master=master, branch_sha=sha,
                         source='precheck-pregrade', **detail)
                elif verdict == 'red':
                    emit(sid, 'precheck-red', master=master, branch_sha=sha, layer='pregrade', **detail)
                results.append(emit(sid, 'precheck-pregrade', master=master, branch_sha=sha,
                                    verdict=verdict, seconds=round(time.monotonic()-started,3), **detail))
        return results


def _pregrade_history(events, subject):
    # Unrelated legacy evidence may be undated; it cannot invalidate absence of
    # a pregrade result or turn an advisory check into a dispatch crash.
    rows = [e for e in events if e.get('subject') == subject
            and e.get('type') in {'build-done','precheck-pregrade','precheck-red','merge-conflict'}]
    try:
        return merge_queue._ordered(rows)
    except (ValueError, TypeError, KeyError):
        return []


def grader_master(events, subject, project=None):
    """An unavailable current master cannot turn advisory results into a refusal."""
    checked = next((e for e in reversed(_pregrade_history(events, subject))
                    if e.get('type') == 'precheck-pregrade'), None)
    if not checked or checked.get('verdict') not in {'red', 'conflict'}:
        return None
    try:
        project = project or next(e['project'] for e in reversed(merge_queue._ordered(events))
                                  if e.get('subject') == subject and e.get('project'))
        repo, main = merge_queue._repository(project)
        return master_sha(repo, main)
    except (OSError, RuntimeError, ValueError, StopIteration, subprocess.SubprocessError):
        return None


def pregrade_refusal(events, subject, current_master):
    own = _pregrade_history(events, subject)
    build = next((e for e in reversed(own) if e.get('type') == 'build-done'), None)
    checked = next((e for e in reversed(own) if e.get('type') == 'precheck-pregrade'), None)
    if (not current_master or not build or not checked or checked.get('branch_sha') != build.get('ready_sha')
            or checked.get('master') != current_master or checked.get('verdict') not in ('red', 'conflict')):
        return None
    evidence = next((e for e in reversed(own) if e.get('type') in ('precheck-red','merge-conflict')
                     and e.get('branch_sha') == checked['branch_sha'] and e.get('master') == current_master), {})
    detail = dict(evidence, **checked)
    return 'precheck ' + checked['verdict'] + ': ' + (str(detail.get('path', 'merge conflict'))
        if checked['verdict'] == 'conflict' else ', '.join(detail.get('tests', ['coupled checks failed'])))


def stats(events, since=None, project=None):
    days = {}
    for event in events:
        day = str(event.get('ts', ''))[:10]
        if not day or since and day < since or project and event.get('project') != project:
            continue
        kind = event.get('type')
        if kind not in ('precheck-pregrade', 'window-run-minutes'):
            continue
        row = days.setdefault(day, dict(clean=0,conflict=0,red=0,undetermined=0,windows=set()))
        if kind == 'precheck-pregrade' and event.get('verdict') in ('clean','conflict','red','undetermined'):
            row[event['verdict']] += 1
        elif kind == 'window-run-minutes' and event.get('state') == 'red' and event.get('window'):
            row['windows'].add(event['window'])
    for day, row in sorted(days.items()):
        print(day, *(f'{v}={row[v]}' for v in ('clean','conflict','red','undetermined')),
              f"red_windows={len(row['windows'])}")


def synchronous(project):
    result = subprocess.run([DOIT, 'precheck', '--project', project, '--json'],
                            text=True, capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError('precheck undetermined')
    return json.loads(result.stdout)


def selection(events, project, repo, main, now, runner=None):
    """A failed probe preserves the existing selection with a visible explanation."""
    try:
        master = master_sha(repo, main)
        event = latest(events, project)
        if not fresh(event, master, now, 30):
            event = (runner or synchronous)(project)
        if not fresh(event, master, now, 30) or not isinstance(event.get('clean'), list):
            raise RuntimeError('precheck result stale or unavailable')
        return event
    except Exception:
        print('window-run: precheck undetermined; using existing selection')
        return None


def launch_due(events, now, *, project='albert-scott', launcher=None):
    """The child takes the same flock as synchronous and manual prechecks."""
    try:
        if not merge_queue.queue(events, project):
            return False
        with lock(project) as owned:
            if not owned:
                return False
            repo, main = merge_queue._repository(project)
            if fresh(latest(events, project), master_sha(repo, main), now, 20):
                return False
            log = fold.ROOT / 'state' / f'precheck.{project}.log'
            with log.open('ab') as stream:
                (launcher or subprocess.Popen)(['nice', DOIT, 'precheck', '--project', project],
                                               env=dict(os.environ, DOIT_LEDGER_FILE='L-tick-local.jsonl'),
                                               start_new_session=True, stdout=stream, stderr=subprocess.STDOUT)
            return True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog='doit precheck', description=__doc__)
    parser.add_argument('--project', type=project_name)
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--since', type=lambda s: datetime.strptime(s, '%Y-%m-%d').date().isoformat())
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.stats:
        stats(window_gate.train_events(), args.since, args.project)
        return 0
    if not args.project:
        parser.error('--project is required for checks')
    def expired(signum, frame):
        raise RuntimeError('precheck deadline exceeded')
    # Unwind extraction and subprocess scopes before the synchronous caller's
    # 15-minute hard deadline, including when bisection needs many guard runs.
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(890)
    try:
        result = run(args.project)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, tarfile.TarError):
        print('precheck undetermined', file=sys.stderr)
        return 3
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"precheck: clean {len(result['clean'])}; master conflicts {len(result['conflict_master'])}; "
              f"set conflicts {len(result['conflict_set'])}; guard red {len(result['guard_red'])}; "
              f"guards={result['guards']}")
    if not args.json:
        try:
            pregrade(args.project)
        except (OSError, RuntimeError, ValueError, TimeoutError, subprocess.SubprocessError):
            print('pregrade undetermined', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
