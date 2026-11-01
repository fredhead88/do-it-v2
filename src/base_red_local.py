"""Collect do-it-v2's hermetic suite at an exact main commit, without CI."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

import base_red


def parse(output, expected, code):
    results = {}
    for line in output.splitlines():
        match = re.fullmatch(r"(PASS|FAIL) (test_[\w.]+|retired check|machine check)(?: \(exit \d+\))?", line)
        if match:
            state, name = match.groups()
            if name in results:
                raise ValueError("duplicate suite result")
            results[name] = state
    if set(results) != set(expected) or code not in (0, 1) or (code == 0) != all(v == 'PASS' for v in results.values()):
        raise ValueError("incomplete local suite")
    return sorted('src/' + name if name.startswith('test_') else name
                  for name, state in results.items() if state == 'FAIL')


def nightly():
    project = 'do-it-v2'
    repo = base_red._repo(project)
    sha = base_red.window_gate.git(repo, 'rev-parse', '--verify', 'refs/heads/main^{commit}').stdout.strip()
    directory = base_red.fold.ROOT / 'base-red' / project
    directory.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (directory / 'nightly.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent = Path(tempfile.mkdtemp(prefix='doit-base-'))
        tree = parent / 'tree'
        try:
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(tree), sha],
                           check=True, capture_output=True, timeout=60)
            expected = [p.name for p in (tree / 'src').iterdir()
                        if re.fullmatch(r'test_.+\.(?:py|mjs)', p.name)] + ['retired check', 'machine check']
            env = {**os.environ, 'DOIT_ROOT': str(parent / 'root'), 'HOME': str(parent / 'home'),
                   'XDG_CONFIG_HOME': str(parent / 'xdg')}
            env.pop('DOIT_LEDGER_FILE', None)
            result = subprocess.run([str(tree / 'doit'), 'test'], cwd=tree, env=env,
                                    capture_output=True, text=True, timeout=7200)
            log = directory / (sha + '.suite.log')
            log.write_text(result.stdout + result.stderr)
            ids = parse(result.stdout, expected, result.returncode)
            base_red._append('base-red-nightly', project, sha, complete=True, ids=ids,
                             suite='local-hermetic', sources=[str(log)])
            return sha, ids
        finally:
            subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(tree)],
                           capture_output=True, timeout=60)
            shutil.rmtree(parent)
