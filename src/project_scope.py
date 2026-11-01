"""Project inference for append callers; spec registration owns the label."""
import re


def subject_project(events, subject):
    rows = [e for e in events if e.get('subject') == subject and e.get('project')]
    registered = next((e['project'] for e in reversed(rows) if e.get('type') == 'spec-written'), None)
    return registered or next((e['project'] for e in rows), None)


def check_inferred(root, event, configured, arguments):
    # Explicit project/DOIT_PROJECT supports repository bootstrap. Inference
    # must resolve to a registered repo, never a planning directory (~/.do-it).
    if event['type'] not in ('spec-written', 'dependency-hold') or configured or any(
            arg.partition('=')[0].rstrip(':') == 'project' for arg in arguments):
        return
    project = event.get('project')
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project) or not (root / 'repos' / project).is_dir():
        raise SystemExit(f'refused: unregistered project {project!r}; name a registered project explicitly')
