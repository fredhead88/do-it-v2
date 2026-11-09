"""Tick-owned evidence requests; projection remains read-only."""
import os
from pathlib import Path
import re
import subprocess
import time

import evidence_revision
import grader_packet
import project_scope


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
    projects = {project_scope.subject_project(events, sid) for sid in specs}
    return {p: project_main(p, root=root) for p in projects if p}


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
    view = [*events, dict(latest, type='build-done')]
    main = mains.get(project_scope.subject_project(events, subject))
    state = grader_packet.evidence_state(view, subject, main_sha=main)
    return (latest, view, main) if state.state == 'pending' else None


def run(step, events, now, *, root, dst):
    """Act only on the projected pending hold. No grader or packet is created."""
    if step.reason != 'not-ready:bundle-pending' or step.owner != 'tick':
        return
    import dispatch
    import look
    subject = step.item
    own = [e for e in events if e.get('subject') == subject]
    build = next((e for e in reversed(own) if e.get('type') == 'build-done'), {})
    block = next((e for e in reversed(own) if e.get('base_only') is True
                  and e.get('ready_sha') and e.get('cited')), None)
    if block and (not build or evidence_revision._time(block['ts']) > evidence_revision._time(build['ts'])):
        build = block
        events = [*events, dict(block, type='build-done')]
    sha = build.get('ready_sha')
    if not re.fullmatch(r'L-spec-\d+', str(subject)) or not re.fullmatch(r'[0-9a-fA-F]{7,40}', str(sha or '')):
        return
    request = request_for(events, subject, sha)
    if request is None:
        # The projection supplies the exact read-only selection key; no second
        # per-subject main resolution is necessary during the acting pass.
        match = re.search(r'current key=([0-9a-f]{64})', step.clears or '')
        if not match:
            return
        try:
            subprocess.Popen(['doit', 'run-checks', subject, '--sha', sha],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             cwd=root)
        except OSError:
            return
        dispatch.emit(dst, dict(subject=subject, project=project_scope.subject_project(events, subject)),
                      'bundle-requested', sha=sha, key=match[1], strict=True)
        return
    age = (now - evidence_revision._time(request['ts'])).total_seconds()
    alarm_key = f'{subject}:{sha}'
    alarmed = any(e.get('type') == 'brief' and e.get('condition') == 'bundle-wait'
                  and (e.get('key') == alarm_key
                       or any(i.get('key') == alarm_key for i in e.get('items', [])))
                  for e in events)
    if age > 7200 and not alarmed:
        look.emit_once('bundle-wait', f'{subject}:{sha}', 'executor',
                       dict(subject=subject, ready_sha=sha, waited_s=int(age)), events, root=root)
