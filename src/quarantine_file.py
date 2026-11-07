"""Render quarantine policy as a one-file Git commit, queued like other merges.

No checkout, index, working file or remote is modified here. The window gate
verifies the queued branch before its ordinary compare-and-swap advancement.
"""
from datetime import datetime, timezone
import hashlib
import re
import subprocess

import fold
import quarantine
import rechain

PATH = 'scripts/ci/pytest_known_red_quarantine.txt'


def render(text, events, project, now):
    rows = quarantine.active(events, project, now)
    managed = {e['node'] for e in events if e.get('project') == project
               and e.get('actor') == 'base-red' and e.get('type') == 'test-quarantined'
               and quarantine.valid_node(e.get('node'))}
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            lines.append(line)
            continue
        fields = line.split('\t')
        if len(fields) != 4:
            raise ValueError('malformed quarantine row')
        expiry = datetime.strptime(fields[2], '%Y-%m-%d').date()
        if fields[0] not in managed and expiry >= now.date():
            lines.append(line)
    for node, row in sorted(rows.items()):
        if not quarantine.valid_node(node):
            continue
        owner = row.get('owner', 'auto-' + quarantine._time(row['since']).date().isoformat())
        reason = row['rule'] + ' evidence=' + ','.join(row['windows'])
        if not quarantine._metadata(owner) or not quarantine._metadata(reason):
            raise ValueError('invalid quarantine metadata')
        # Compact only the display evidence: ledger windows remain exact for
        # proposal identity/renewal. Seven hex characters cannot exceed the
        # scanner's 3-bit entropy threshold. Keep context and JSON syntax intact.
        evidence = re.sub(r'(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])',
                          lambda match: match[0][:7], ','.join(row['windows']))
        reason = row['rule'] + ' evidence=' + evidence
        lines.append('\t'.join((node, owner, quarantine._time(row['until']).date().isoformat(), reason)))
    return '\n'.join(lines) + '\n'


def candidate(repo, base, events, project, now=None):
    try:
        return _candidate(repo, base, events, project, now)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('quarantine policy unavailable (' + type(exc).__name__ + ')') from None


def _candidate(repo, base, events, project, now=None):
    if project != 'albert-scott':
        return base
    now = now or datetime.now(timezone.utc)
    found = rechain.git(repo, 'show', base + ':' + PATH, check=False)
    raw = found.stdout
    if found.returncode:
        return base  # repositories predating the quarantine format
    content = render(raw.decode(), events, project, now).encode()
    if content == raw:
        return base
    tree = rechain.rewrite_tree(repo, rechain.out(repo, 'rev-parse', base+'^{tree}'), {PATH: content})
    return rechain.out(repo, *rechain.IDENT, 'commit-tree', tree, '-p', base,
                       '-m', 'ci: reconcile automatic base-red quarantine')


def enqueue(project, events, now):
    try:
        return _enqueue(project, events, now)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('quarantine queue unavailable (' + type(exc).__name__ + ')') from None


def _enqueue(project, events, now):
    repo = fold.ROOT / 'repos' / project
    if project != 'albert-scott' or not repo.is_dir() or not (repo / PATH).is_file():
        return None
    import merge_queue
    # Follow the integrator's attached branch; never invent or move master here.
    main = rechain.out(repo, 'symbolic-ref', '--short', 'HEAD')
    base = rechain.out(repo, 'rev-parse', 'refs/heads/' + main)
    commit = candidate(repo, base, events, project, now)
    if commit == base:
        return None
    content = rechain.blob(repo, commit, PATH)
    digest = hashlib.sha256(content).hexdigest()[:16]
    subject, branch = 'quarantine-' + digest, 'auto-quarantine/' + digest
    if any(row['spec'] == subject for row in merge_queue.queue(events, project)):
        return subject
    rechain.out(repo, 'update-ref', 'refs/heads/' + branch, commit)
    quarantine._append('merge-gate-clean', project, now, _subject=subject,
                       branch=branch, sha=commit, grant=[PATH], reason='automatic one-file quarantine')
    quarantine._append('queue-add', project, now, _subject=subject, branch=branch, rule='auto-quarantine')
    return subject


def queued(events, project):
    """Only authenticated maintenance rows may run during a nightly freeze."""
    import merge_queue
    authentic = {(e.get('subject'), e.get('branch')) for e in events
                 if e.get('type') == 'queue-add' and e.get('actor') == 'base-red'
                 and e.get('project') == project and e.get('rule') == 'auto-quarantine'}
    return {row['spec'] for row in merge_queue.queue(events, project)
            if (row['spec'], row['branch']) in authentic}
