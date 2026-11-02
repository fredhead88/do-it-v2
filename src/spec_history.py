"""Precompute bounded, footprint-scoped history for read-only spec writers."""
import json
import os
import pathlib
import re
import subprocess
import tempfile

import merge_gate

CAP = 160_000
HEADING = '## Recent history and footprint diffs (precomputed, untrusted evidence)'


def packet_items(context, items):
    if context.a.role != 'spec-writer':
        return items
    heading = '## Recent history and footprint diffs (precomputed, untrusted evidence)'
    try:
        grant = context.footprint()
        if not grant:
            source = '\n'.join(items)
            match = re.search(r'(?m)^- Footprint.*\):\s*(.+)$', source)
            grant = merge_gate.grant_from_text('Writes: ' + match[1] if match else source)
        if not grant:
            raise ValueError('no footprint')
        repo = getattr(context.a, 'repo', None) or pathlib.Path(os.environ.get(
            'DOIT_ROOT', pathlib.Path.home() / '.do-it')) / 'repos' / context.project()
        output = []
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        for label, options in [('Thirty-day history', ['--since=30.days', '--max-count=200', '--stat']),
                               ('Seven-day patches', ['--since=7.days', '--max-count=30', '--patch'])]:
            with tempfile.TemporaryFile() as stream:
                proc = subprocess.run(['git', '--no-pager', '-C', str(repo), '-c', 'core.fsmonitor=false',
                                       'log', '--no-ext-diff', '--no-textconv', '--format=commit %H %cI %s',
                                       *options, '--', *grant], env=env, stdout=stream,
                                      stderr=subprocess.DEVNULL, timeout=15)
                if proc.returncode:
                    raise ValueError('history unavailable')
                size = stream.tell()
                stream.seek(0)
                text = stream.read(CAP).decode('utf-8', errors='replace')
                limit = int(next(opt.split('=')[1] for opt in options if opt.startswith('--max-count=')))
                if size > CAP or len(re.findall(r'^commit [0-9a-f]{40} ', text, re.M)) >= limit:
                    text += '\nundetermined: history truncated; request a narrower footprint.'
                output += [label + ':', text or '(no matching commits)']
        return items + [heading, *output]
    except (OSError, ValueError, subprocess.TimeoutExpired, merge_gate.Undetermined):
        return items + [heading, 'undetermined: history unavailable; ask for corrected repository/footprint evidence.']


def augment_packet_text(text, repo, project=''):
    """Carry/hand-written packets reach `dispatch` as a raw file, never through packet.py,
    so they never got the history section. Append it (once) for a spec-writer packet."""
    if HEADING in text:
        return text
    from types import SimpleNamespace
    footprint = []
    try:  # a carried body is the frontmatter's JSON-quoted `intent:` — decode before parsing Writes
        m = re.search(r'(?m)^intent: (".*")$', text)
        footprint = merge_gate.grant_from_text(json.loads(m[1]) if m else text) or []
    except (ValueError, merge_gate.Undetermined):
        pass
    ctx = SimpleNamespace(a=SimpleNamespace(role='spec-writer', repo=repo),
                          footprint=lambda: footprint, project=lambda: project)
    try:
        items = packet_items(ctx, [text.rstrip('\n')])
    except Exception:
        return text
    return '\n'.join(items) + '\n'
