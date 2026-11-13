"""Tick-owned evidence requests; projection remains read-only."""
import os
from pathlib import Path
import re
import subprocess
import time

import evidence_revision
import grader_packet
import project_scope


# Like pass_memo, retain one caller list so identities cannot be recycled.
_main_snapshot = None


def project_main(project, *, root=None):
    """Resolve the local main used by the runner, without fetching."""
    import build_runner
    root = Path(root or os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
    if not project_scope.registered(root, project):
        return None
    repo = root / 'repos' / project
    if not (repo / '.git').exists():
        return None
    env, deadline = dict(os.environ), time.monotonic() + 10
    try:
        branch = build_runner._main_branch(repo, project, env, deadline)
        for ref in ('origin/' + branch, branch):
            code, value = build_runner._process(
                ['git', '-C', str(repo), 'rev-parse', '--verify', ref + '^{commit}'],
                env=env, deadline=deadline)
            sha = value.decode().strip()
            if code == 0 and re.fullmatch(r'[0-9a-f]{40}', sha):
                return sha
        return None
    except (OSError, ValueError, build_runner.Undetermined):
        return None


def project_mains(events, specs, *, root=None):
    global _main_snapshot
    projects = {project_scope.subject_project(events, sid) for sid in specs}
    mains = {p: project_main(p, root=root) for p in projects if p}
    _main_snapshot = (events, len(events), mains)
    return mains


def cached_mains(events):
    """Reuse only this caller snapshot's main identity; never resolve another ref."""
    snapshot = _main_snapshot
    if snapshot is None or snapshot[0] is not events or snapshot[1] != len(events):
        return {}
    return snapshot[2]


def request_for(events, subject, sha):
    return next((e for e in reversed(events)
                 if e.get('subject') == subject and e.get('type') == 'bundle-requested'
                 and e.get('sha') == sha and evidence_revision.current(events, subject, e)
                 and (Path(str(e['_src']).split(':')[0]).name.startswith('L-tick-')
                      if e.get('_src') else e.get('actor') == 'tick')), None)


def clears(events, subject, main_sha, text):
    state = grader_packet.evidence_state(events, subject, main_sha=main_sha)
    request = request_for(events, subject, state.ready_sha)
    detail = f'ready_sha={state.ready_sha}; current key={state.key}; main sha={main_sha}'
    if request:
        detail += f"; request key={request.get('key')}"
        if request.get('key') != state.key:
            detail += '; stale-main: operator must request current-main checks'
    return text + '; ' + detail


def base_hold(events, subject, mains):
    """Only already-marked base-only blocks with a concrete proof tuple qualify."""
    if mains is None:
        return None
    own = [e for e in events if e.get('subject') == subject]
    latest = next((e for e in reversed(own) if e.get('type') in
                   {'blocked', 'unblocked', 'build-blocked', 'build-started', 'build-done'}), None)
    if not (latest and latest.get('base_only') is True and latest.get('ready_sha')
            and isinstance(latest.get('cited'), list) and latest['cited']):
        return None
    # evidence_state selects build-done; the marked block supplies this identity.
    view = build_view(events, subject)
    main = mains.get(project_scope.subject_project(events, subject))
    return latest, view, main


def build_view(events, subject):
    """Preserve the marked base-only recovery identity for tick acquisition."""
    own = [e for e in events if e.get('subject') == subject]
    latest = next((e for e in reversed(own) if e.get('type') in
                   {'blocked', 'unblocked', 'build-blocked', 'build-started', 'build-done'}), None)
    if (latest and latest.get('base_only') is True and latest.get('ready_sha')
            and isinstance(latest.get('cited'), list) and latest['cited']):
        return [*events, dict(latest, type='build-done', status='DONE')]
    return events


def run(step, events, now, *, root, dst):
    """Compatibility entry: pending holds use the tick's single acquisition seam."""
    if step.reason != 'not-ready:bundle-pending' or step.owner != 'tick':
        return
    import tick
    from types import SimpleNamespace
    # Re-evaluate with the full evidence reader; a landed bundle releases the
    # hold, and all launch admission/retry bookkeeping belongs to tick.
    from dataclasses import replace
    current = replace(step, state='action', action='bundle')
    return tick.launch(current, dispatcher=SimpleNamespace(
        now=now, mains=cached_mains(events)))
