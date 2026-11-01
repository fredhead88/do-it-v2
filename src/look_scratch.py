"""Name scratch Git residue and refused sweep access as machine health."""
import os
from pathlib import Path


def git_entry(path):
    path = Path(path).resolve()
    return next((str(p / '.git') for p in (path, *path.parents)
                 if (p / '.git').exists() or (p / '.git').is_symlink()), None)


def check(events, root, briefs, answered):
    import look
    path = Path(os.environ.get('DOIT_SCRATCH') or Path.home() / 'doit-scratch')
    entry = path / '.git'
    marker = str(entry) if entry.exists() or entry.is_symlink() else None
    look._settle('scratch-git-entry', 'scratch-root', 'thinker', bool(marker),
                 {'why': '.git at ' + marker if marker else 'scratch root has no .git entry'},
                 events, root, briefs, answered)
    latest = {}
    for event in events:
        if (event.get('type') == 'spawn-started' and event.get('dsn_role') in {'refused', 'readonly'}
                and event.get('role') in {'owed-sweeper', 'owed-verifier'}):
            latest[event.get('project', 'unknown')] = event
    for project, event in latest.items():
        bad = event['dsn_role'] == 'refused'
        marker = git_entry(event.get('cwd') or path) if bad else None
        why = ('.git at ' + marker if marker else
               'read-only sweep access refused; credential collision or destination changed' if bad else
               'read-only sweep access restored')
        look._settle('sweep-access-refused', project, 'thinker', bad,
                     {'why': why, 'spawn': event.get('spawn', '')}, events, root, briefs, answered)
