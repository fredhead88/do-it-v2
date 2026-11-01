"""Provision dashboard dependencies before handing a worktree to a builder."""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil

import grading_env
import provision


PACKET_NOTE = ("   Dashboard node_modules is provisioned before dispatch and satisfies an npm ci prerequisite "
               "in a carried spec. Never run npm ci or another dependency install; verify with the supplied tree.")


def prepare(tree, root, checkout, *, subject=None):
    tree, root, checkout = Path(tree), Path(root), Path(checkout)
    lock = tree / 'dashboard/package-lock.json'
    if not lock.is_file():
        return 'none'
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    state = root / 'state/builder-deps'
    state.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(tree.resolve()).encode()).hexdigest()
    marker = state / (key + '.json')
    target = tree / 'dashboard/node_modules'
    with (state / (key + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            previous = json.loads(marker.read_text())
        except (OSError, ValueError):
            previous = {}
        if target.is_symlink():
            # Unlink only the old shared link, never anything it points at.
            target.unlink()
        elif target.is_dir():
            if previous.get('lock') == digest:
                return 'private:' + digest
            if not previous:
                return 'existing'  # preserve an operator's unmanaged installation
            shutil.rmtree(target)  # only a copy this module previously created
        try:
            import checks
            record = checks.CheckRecord('builder:dependencies', subject or tree.name, 'setup', [], '',
                'scratch', False, 'repo-venv', 'pre-merge', 'model')
            with provision.context(tree=tree, dashboard=(root, checkout / 'dashboard'),
                                   private_node_modules=True):
                prepared = provision.prepare(record, lane='build')
            used = prepared.summary['node_modules']
        except (OSError, RuntimeError, ValueError) as exc:
            raise grading_env.InfraFailure('node-modules') from exc
        marker.write_text(json.dumps({'lock': digest, 'source': used}))
        return used
