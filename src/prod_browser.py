#!/usr/bin/env python3
"""Committed browser observations in disposable clones; no ledger writes."""
from datetime import timedelta
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import fold
import grading_env
import scratch

SOURCE = pathlib.Path(__file__).resolve().parent.parent
SPEC = re.compile(r'(?<![A-Za-z0-9_./-])dashboard/e2e/[A-Za-z0-9._-]+\.spec\.ts(?![A-Za-z0-9_./-])')


class Unobservable(Exception):
    pass


class Runner:
    def run(self, argv, **kw):
        return subprocess.run(argv, capture_output=True, text=True, **kw)


def _env():
    return {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}


def _command(runner, argv, *, cwd=None, env=None, timeout=60, phase='command'):
    try:
        result = runner.run(argv, cwd=cwd, env=env if env is not None else _env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Unobservable(phase + ' timed out') from None
    except (OSError, subprocess.SubprocessError):
        raise Unobservable(phase + ' unavailable') from None
    if result.returncode:
        raise Unobservable(phase + ' failed')
    return result.stdout or ''


def _target(row, runner):
    project = row.get('project')
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project):
        return None
    try:
        config = grading_env._look_toml(SOURCE)
        entry = next((p for p in config.get('prod', []) if p.get('project') == project), None)
        if not entry or not entry.get('repo'):
            return None
        repo = pathlib.Path(entry['repo']).expanduser().resolve()
        events = [e for e in fold.read_events() if e.get('type') == 'deploy-landed' and e.get('target') == project]
        event = max(events, key=lambda e: fold.ts(e.get('ts'))) if events else {}
        sha = event.get('sha', '')
        if not re.fullmatch(r'[0-9a-fA-F]{7,40}', sha):
            return None
        _command(runner, ['git', '-C', str(repo), 'cat-file', '-e', sha + '^{commit}'])
        return repo, sha
    except (OSError, ValueError, TypeError, Unobservable):
        return None


def _paths(row, repo, sha, runner):
    try:
        text = pathlib.Path(row['spec_path']).read_text()
        block = grading_env._criterion_blocks(text).get(row['criterion'], '')
    except (KeyError, OSError, UnicodeError, TypeError):
        return []
    paths = []
    for path in dict.fromkeys(SPEC.findall(block)):
        if '..' in pathlib.PurePosixPath(path).name:
            continue
        try:
            _command(runner, ['git', '-C', str(repo), 'cat-file', '-e', sha + ':' + path])
        except Unobservable:
            continue
        paths.append(path)
    return paths


def _partition(rows, runner):
    e2e, seats = [], []
    for row in rows:
        target = _target(row, runner)
        paths = _paths(row, *target, runner) if target else []
        if paths:
            e2e.append((row, *target, paths))
        else:
            seats.append(row)
    return e2e, seats


def split(rows, *, runner=None):
    e2e, seats = _partition(rows, runner or Runner())
    return [entry[0] for entry in e2e], seats


def _safe(path):
    resolved = pathlib.Path(path).resolve()
    forbidden = (pathlib.Path('/opt/albert-scott'), pathlib.Path.home() / '.do-it/portal-e2e-runner')
    if any(resolved.is_relative_to(p.resolve()) for p in forbidden):
        raise Unobservable('forbidden browser run directory')
    return resolved


def _credentials(project):
    path = pathlib.Path.home() / '.do-it' / ('review-creds-' + project + '.env')
    values = {}
    try:
        with path.open() as handle:
            for line in handle:
                key, sep, value = line.strip().partition('=')
                if sep and key in ('VERIFIER_CLIENT_USER', 'VERIFIER_CLIENT_PASS'):
                    values[key] = value.strip().strip('\"\'')
    except (OSError, UnicodeError):
        raise Unobservable('missing review credentials file') from None
    if len(values) != 2 or not all(values.values()):
        raise Unobservable('missing review credentials')
    return values


def _node(dashboard, runner, env):
    lock = dashboard / 'package-lock.json'
    if not lock.is_file():
        raise Unobservable('missing node lockfile')
    key = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    root = _safe(scratch.sub('node-cache'))
    cache = _safe(root / key)
    with (root / (key + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not (cache / 'node_modules').is_dir():
            # Use the clone as npm's working directory; publish only modules.
            _command(runner, ['npm', 'ci', '--prefer-offline'], cwd=dashboard,
                     env=env, timeout=600, phase='npm ci')
            if not (dashboard / 'node_modules').is_dir():
                raise Unobservable('npm ci produced no node_modules')
            with tempfile.TemporaryDirectory(dir=root, prefix=key + '-') as tmp:
                ready = pathlib.Path(tmp) / 'ready'
                ready.mkdir()
                shutil.copytree(dashboard / 'node_modules', ready / 'node_modules')
                if cache.exists():
                    shutil.rmtree(cache)
                ready.rename(cache)
        target = dashboard / 'node_modules'
        if target.is_symlink():
            raise Unobservable('unsafe node_modules symlink')
        if not target.exists():
            shutil.copytree(cache / 'node_modules', target)
        # Reject symlinks that escape either private dependency tree.
        for base in (cache / 'node_modules', target):
            for link in base.rglob('*'):
                if link.is_symlink() and not link.resolve().is_relative_to(base.resolve()):
                    raise Unobservable('node_modules symlink escapes private tree')


def _report_specs(report):
    found = {}
    def walk(suite):
        for spec in suite.get('specs', []):
            file = spec.get('file', suite.get('file', ''))
            found.setdefault(file, []).append(spec)
        for child in suite.get('suites', []):
            walk(child)
    for suite in report['suites']:
        walk(suite)
    return found


def _answer(report, paths, dashboard):
    if report.get('errors'):
        raise Unobservable('Playwright infrastructure error (possibly missing chromium)')
    specs = _report_specs(report)
    failure = None
    for path in paths:
        relative = str(pathlib.PurePosixPath(path).relative_to('dashboard'))
        items = specs.get(relative) or specs.get(str(dashboard / relative)) or specs.get(path)
        if not items:
            raise Unobservable('Playwright report lacks ' + path)
        for item in items:
            tests = item.get('tests', [])
            if not tests:
                raise Unobservable('Playwright report has no tests for ' + path)
            for test in tests:
                results = test.get('results', [])
                errors = json.dumps([r.get('errors', r.get('error', [])) for r in results]).lower()
                if any(word in errors for word in ("executable doesn't exist", 'browsertype.launch',
                                                   'browser was not found', 'missing chromium')):
                    raise Unobservable('chromium unavailable')
                statuses = [r.get('status') for r in results]
                if any(s in ('failed', 'timedOut') for s in statuses):
                    failure = failure or (path, item.get('title', 'unnamed test'))
                elif not statuses or any(s != 'passed' for s in statuses):
                    raise Unobservable('Playwright test undetermined for ' + path)
    return ('failed', failure[0] + ': ' + failure[1]) if failure else ('met', ', '.join(paths) + ': passed')


def run_batch(rows, now, *, runner=None):
    runner = runner or Runner()
    entries, _ = _partition(rows, runner)
    groups = {}
    for row, repo, sha, paths in entries:
        groups.setdefault((row['project'], repo, sha), []).append((row, paths))
    observations = []
    for (project, repo, sha), group in groups.items():
        clone, credentials, report, fault = None, {}, None, None
        try:
            credentials = _credentials(project)
            root = _safe(scratch.sub('prod-browser'))
            with tempfile.TemporaryDirectory(dir=root, prefix=project + '-') as tmp:
                clone = _safe(pathlib.Path(tmp) / 'clone')
                if len(str(clone)) > 500:
                    raise Unobservable('scratch clone path exceeds evidence bound')
                home = _safe(pathlib.Path(tmp) / 'home')
                home.mkdir()
                env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home)}
                _command(runner, ['git', 'clone', '--shared', '--no-checkout', str(repo), str(clone)],
                         cwd=root, env=env, phase='clone')
                _command(runner, ['git', 'checkout', '--detach', sha], cwd=clone, env=env, phase='checkout')
                dashboard = _safe(clone / 'dashboard')
                _node(dashboard, runner, env)
                play_env = {**env, **credentials, 'PLAYWRIGHT_BROWSERS_PATH': str(
                    _safe(pathlib.Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or
                                            pathlib.Path.home() / '.cache/ms-playwright')))}
                if 'E2E_BASE_URL' in os.environ:
                    play_env['E2E_BASE_URL'] = os.environ['E2E_BASE_URL']
                paths = list(dict.fromkeys(p for _, paths in group for p in paths))
                argv = [str(dashboard / 'node_modules/.bin/playwright'), 'test',
                        *[str(pathlib.PurePosixPath(p).relative_to('dashboard')) for p in paths], '--reporter=json']
                try:
                    process = runner.run(argv, cwd=dashboard, env=play_env, timeout=600)
                except subprocess.TimeoutExpired:
                    raise Unobservable('Playwright killed at 600-second cap') from None
                except (OSError, subprocess.SubprocessError):
                    raise Unobservable('Playwright or chromium unavailable') from None
                try:
                    report = json.loads(process.stdout)
                    if not isinstance(report, dict) or not isinstance(report.get('suites'), list):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise Unobservable('Playwright report does not parse (possibly missing chromium)') from None
                # Read the report while all directory context still exists.
                answers = []
                for row, row_paths in group:
                    try:
                        answers.append(_answer(report, row_paths, dashboard))
                    except (Unobservable, KeyError, TypeError, AttributeError) as exc:
                        answers.append(('unobservable', str(exc) if isinstance(exc, Unobservable) else 'malformed Playwright report'))
                if process.returncode and all(answer[0] == 'met' for answer in answers):
                    raise Unobservable('Playwright process failed without a test failure')
        except (Unobservable, OSError, ValueError, scratch.ScratchRootError) as exc:
            fault = str(exc) if isinstance(exc, Unobservable) else 'browser scratch provisioning unavailable'
        for index, (row, _) in enumerate(group):
            outcome, detail = ('unobservable', fault) if fault else answers[index]
            obs = {'spec': row['spec'], 'criterion': row['criterion'], 'outcome': outcome,
                   'observed_at': now.isoformat(),
                   'valid_until': (now + timedelta(hours=row.get('validity_h', 24))).isoformat(),
                   'evidence': ''}
            for secret in credentials.values():
                detail = detail.replace(secret, '[redacted]')
            if outcome == 'unobservable':
                obs.update(capability='browser', why=detail[:600])
            else:
                prefix = 'clone=' + str(clone)
                obs['evidence'] = (prefix + ' ' + detail)[:600]
            observations.append(obs)
    return observations
