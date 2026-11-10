#!/usr/bin/env python3
"""Run the box check on a detached merge and record one prequeue verdict.

    doit prequeue <spec> --project <project> [--main master]
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tomllib

import base_red
import fold
import window_gate

CONFIG = Path(__file__).resolve().parent.parent / 'look.toml'
NODE_MODULES = Path('/opt/albert-scott/dashboard/node_modules')
DEFAULTS = dict(enforce=False, wall_cap_s=900, load_max=15)
git = window_gate.git


def settings():
    """Read the checkout's prequeue settings, with validated safe defaults."""
    try:
        with CONFIG.open('rb') as stream:
            table = tomllib.load(stream).get('prequeue', {})
        result = {key: table.get(key, default) for key, default in DEFAULTS.items()}
        if type(result['enforce']) is not bool:
            raise ValueError('enforce')
        for key in ('wall_cap_s', 'load_max'):
            value = result[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(key)
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULTS.copy()


def run_child(tree, base, cap):
    env = os.environ.copy()
    env.pop('ANTHROPIC_API_KEY', None)
    command = [env.get('DOIT_PREQUEUE_PYTHON', '/opt/albert-scott/.venv/bin/python'),
               'scripts/ci/prequeue_check.py', '--base', base, '--json', '--wall-cap', str(cap)]
    # Kill descendants too: no timed-out checks may keep using a removed tree.
    with subprocess.Popen(command, cwd=tree, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as child:
        try:
            stdout, _ = child.communicate(timeout=cap)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise
        return child.returncode, stdout


def parse_result(code, stdout):
    """Only a complete, consistent response can certify a determined result."""
    try:
        result = json.loads(stdout.splitlines()[-1])
        verdict = result['verdict']
        if code != {'green': 0, 'red': 1, 'undetermined': 2}[verdict]:
            raise ValueError('exit mismatch')
        if verdict == 'undetermined':
            if not isinstance(result['why'], str) or not result['why']:
                raise ValueError('why')
            return result
        wall = result['wall_s']
        if type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
            raise ValueError('wall_s')
        if verdict == 'green':
            if result['scope'] not in ('full', 'selected', 'partial'):
                raise ValueError('scope')
        elif (not isinstance(result['checks'], dict) or not isinstance(result['failing'], list)
              or not all(isinstance(node, str) and node for node in result['failing'])):
            raise ValueError('red fields')
        return result
    except (ValueError, KeyError, TypeError, IndexError):
        return dict(verdict='undetermined', why='bad-output')


def check_tree(repo, tree, branch, base, cap):
    """Own only the newly created scratch directory, and always remove it."""
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.mkdir(mode=0o700)  # An existing attempt's directory is never ours to remove.
    try:
        git(repo, 'worktree', 'add', '--detach', str(tree), base)
        merge = git(tree, '-c', 'user.name=doit-prequeue', '-c',
                    'user.email=doit-prequeue@localhost', 'merge', '--no-ff', '--no-edit', branch,
                    check=False, env={**os.environ, 'GIT_COMMITTER_NAME': 'doit-prequeue',
                                      'GIT_COMMITTER_EMAIL': 'doit-prequeue@localhost'})
        if merge.returncode:
            git(tree, 'merge', '--abort', check=False)
            return dict(verdict='undetermined', why='conflict')
        parents = git(tree, 'show', '-s', '--format=%P', 'HEAD').stdout.split()
        if parents != [base, branch]:
            return dict(verdict='undetermined', why='no-merge')
        if not (tree / 'scripts/ci/prequeue_check.py').is_file():
            return dict(verdict='undetermined', why='no-script')
        modules = tree / 'dashboard/node_modules'
        if NODE_MODULES.is_dir() and not modules.exists() and not modules.is_symlink():
            modules.parent.mkdir(parents=True, exist_ok=True)
            modules.symlink_to(NODE_MODULES, target_is_directory=True)
        try:
            code, stdout = run_child(tree, base, cap)
        except subprocess.TimeoutExpired:
            return dict(verdict='undetermined', why='timeout')
        return parse_result(code, stdout)
    finally:
        try:
            git(repo, 'worktree', 'remove', '--force', str(tree))
        finally:
            if tree.exists():
                shutil.rmtree(tree)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec')
    parser.add_argument('--project', required=True)
    parser.add_argument('--main', default='master')
    args = parser.parse_args(argv)
    for value in (args.spec, args.project):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value):
            parser.error('spec and project must be simple names')
    config = settings()
    repo = fold.ROOT / 'repos' / args.project
    branch, base = 'unknown', None
    excluded = []
    result = dict(verdict='undetermined', why='no-branch')
    try:
        entries = window_gate.resolve_entries(repo, [args.spec], window_gate.train_events())
        _, _, branch = entries[0]
    except (OSError, RuntimeError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    else:
        try:
            load = os.getloadavg()[0]
            if not math.isfinite(load) or load >= config['load_max']:
                result = dict(verdict='undetermined', why='load')
            else:
                base = git(repo, 'rev-parse', '--verify', f'refs/heads/{args.main}^{{commit}}').stdout.strip()
                tree = fold.ROOT / 'scratch' / 'prequeue' / f'{args.spec}-{branch[:12]}'
                result = check_tree(repo, tree, branch, base, config['wall_cap_s'])
                if result['verdict'] == 'red' and result['failing']:
                    known = base_red.known_red(args.project)
                    excluded = [node for node in result['failing'] if node in known]
                    result['failing'] = [node for node in result['failing'] if node not in known]
                    if not result['failing']:
                        result.update(verdict='green', scope='partial')
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            result = dict(verdict='undetermined', why='execution')
    verdict = result['verdict']
    fields = dict(spec=args.spec, branch_sha=branch)
    if verdict == 'undetermined':
        fields['why'] = result['why']
    else:
        fields.update(base_sha=base, wall_s=result['wall_s'])
        if verdict == 'green':
            fields['scope'] = result['scope']
        else:
            fields.update(checks=result['checks'], failing=result['failing'])
    window_gate.ev('prequeue-' + verdict, args.spec, project=args.project, **fields)
    print(json.dumps(dict(verdict=verdict, **fields, excluded_count=len(excluded),
                          excluded=excluded,
                          excluded_reason='known-red-on-master' if excluded else None)))
    return {'green': 0, 'red': 1, 'undetermined': 2}[verdict]


if __name__ == '__main__':
    sys.exit(main())
