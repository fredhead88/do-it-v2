#!/usr/bin/env python3
"""Render a deterministic system description from repository sources only."""
import argparse
import ast
import difflib
from pathlib import Path
import sys
import textwrap
import tomllib


def event_types(path):
    """Literal tick events from both supported emit call signatures."""
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    events = set()
    forms = {('dispatch', 'emit'): 2, ('look', 'emit_once'): 0}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        index = forms.get((node.func.value.id, node.func.attr))
        if index is not None and len(node.args) > index:
            value = node.args[index]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                events.add(value.value)
    return events


def _toml(repo, name):
    with (repo / name).open('rb') as stream:
        return tomllib.load(stream)


def _rows(doc, key, fields):
    rows = doc[key]
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'{key}: expected nonempty rows')
    for row in rows:
        for field in fields:
            value = row[field]
            if field == 'limit_min':
                if type(value) is not int or value <= 0:
                    raise ValueError('limit_min: expected a positive integer')
            elif not isinstance(value, str) or not value.strip() or '\n' in value:
                raise ValueError(f'{field}: expected a nonempty single-line string')
    return rows


def _wrap(text, initial='- ', subsequent='  '):
    return textwrap.wrap(text, width=100, initial_indent=initial,
                         subsequent_indent=subsequent, break_long_words=False,
                         break_on_hyphens=False, replace_whitespace=False)


def render(repo: str) -> str:
    """Return the current page; missing or malformed sources raise, never render partially."""
    repo = Path(repo)
    roles = sorted(path.stem for path in (repo / 'agents').iterdir()
                   if path.is_file() and path.suffix == '.md')
    if not roles:
        raise ValueError('agents: no contracts')
    model_source = ('machine/models.toml' if (repo / 'machine/models.toml').exists()
                    else 'models.example.toml')
    models = _toml(repo, model_source)
    default = models['defaults']['backend']
    contracts = models.get('contracts', {})
    backends = []
    for role in roles:
        contract = contracts.get(role, {})
        # models.backend_of semantics, without importing machine-local model state.
        backend = contract.get('backend', default)
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError('models: expected backend string')
        suffix = '' if 'backend' in contract else ' (default)'
        backends.append(f'{role}: {backend}{suffix}')
    rules = _rows(_toml(repo, 'lane_rules.toml'), 'rules',
                  ('name', 'action', 'actor', 'limit_min'))
    crons = _rows(_toml(repo, 'crons.toml'), 'row',
                  ('name', 'schedule', 'where', 'command', 'path'))
    events = event_types(repo / 'src/tick.py')
    if not events:
        raise ValueError('tick: no literal events')
    events.update(row['action'] for row in rules)

    lines = ['# How DO-IT works', '',
             'Generated from repository code and config by `doit howto`; regenerate when sources change.',
             '', '## Roles', '', f'Source: `agents/*.md` and `{model_source}`.']
    lines.extend('- ' + backend for backend in backends)
    lines.extend(['', 'SD10 records `tokens = {vendor, input, cached_input, output, reasoning}` on terminal events.',
                  'Vendors are `codex` or `claude`; input includes cached input; absent usage is unknown.',
                  '`doit health [--json]` reports finishing, intake, usage percentages, disk and tokens_per_live_spec.',
                  'Tokens per live spec average uncached input plus output for measured specs shipped in seven days.',
                  'unmeasured_specs counts excluded specs; spawn-stale has no usage and understates spend.',
                  'The vendor routing above assigns authors to Codex and judging roles to Claude seats.',
                  'The Executor uses claude-sonnet-5-5; judgment calls go to a claude-opus-5-5 seat sub-agent.',
                  'Sonnet trial: log tokens_per_live_spec.claude via doit health --json at start and 14 days later;',
                  'record both readings and judgment outcomes in lesson lines (axis=K); a measured rise ends it.'])
    lines.extend(['', '## Lane rules', '', 'Source: `lane_rules.toml`. Name | action | actor | limit_min.'])
    for row in rules:
        lines.extend(_wrap(' | '.join(str(row[key]) for key in ('name', 'action', 'actor', 'limit_min'))))
    lines.extend(['', '## Cron manifest', '',
                  'Source: `crons.toml`. Name | schedule | where | path, followed by command.'])
    for row in crons:
        lines.extend(_wrap(' | '.join(row[key] for key in ('name', 'schedule', 'where', 'path'))
                           + '  ' + row['command']))
    lines.extend(['', '## Tick event types', '',
                  'Source: literal `dispatch.emit` and `look.emit_once` calls in `src/tick.py`,',
                  'plus every action in `lane_rules.toml`.'])
    lines.extend(_wrap('; '.join(sorted(events))))
    return '\n'.join(lines) + '\n'


def main(argv=None, *, repo=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only comparison; exit 1 on drift')
    args = parser.parse_args(argv)
    repo = Path(repo) if repo is not None else Path(__file__).resolve().parent.parent
    target = repo / 'docs/HOW-IT-WORKS.md'
    try:
        page = render(str(repo))
        if args.check:
            existing = target.read_text(encoding='utf-8')
            if existing == page:
                return 0
            sys.stdout.writelines(difflib.unified_diff(
                existing.splitlines(keepends=True), page.splitlines(keepends=True),
                fromfile='docs/HOW-IT-WORKS.md (committed)',
                tofile='docs/HOW-IT-WORKS.md (regenerated)'))
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding='utf-8')
        return 0
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, AttributeError) as exc:
        print(f'howto: undetermined ({type(exc).__name__})', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
