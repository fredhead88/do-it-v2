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
             '', '## Roles', f'Source: `agents/*.md` and `{model_source}`.']
    lines.extend('- ' + backend for backend in backends)
    lines.extend(['', 'SD10 records `tokens = {vendor, input, cached_input, output, reasoning}` on terminal events.',
                  'Vendors are `codex` or `claude`; input includes cached input; absent usage is unknown.',
                  '`doit health [--json]`: finishing, intake, usage, disk, tokens_per_live_spec (7-day mean;',
                  'unmeasured_specs excluded). Authors: Codex; judges: Claude. Executor: claude-sonnet-5-5;',
                  'judgment: claude-opus-5-5 seat. Sonnet trial: log health at start and 14 days later; axis=K.',
                  'SD10 migration cuts: audit reports migration-not-cut; merge the migration-only predecessor first.'])
    lines.extend(['## Lane rules', 'Source: `lane_rules.toml`. Name | action | actor | limit_min.'])
    # Keep each lane tuple intact while leaving room for new cron rows within
    # the page's existing line budget.
    current = ''
    for row in rules:
        item = '- ' + ' | '.join(str(row[key]) for key in ('name', 'action', 'actor', 'limit_min'))
        if current and len(current) + 2 + len(item) > 100:
            lines.append(current)
            current = ''
        current += ('; ' if current else '') + item
    lines.extend(_wrap(current, initial='', subsequent='  '))
    lines.extend(['', '## Cron manifest',
                  'Source: `crons.toml`. Name | schedule | where | path, then command.'])
    # Pack rows together, keeping each metadata tuple intact where it fits.
    # Commands retain two spaces after metadata and ordinary word wrapping.
    current = '- '
    for index, row in enumerate(crons):
        metadata = ' | '.join(row[key] for key in ('name', 'schedule', 'where', 'path'))
        if row.get('paused') is True:
            metadata += ' (paused)'
        chunks = textwrap.wrap(metadata, width=98, break_long_words=False, break_on_hyphens=False)
        tokens = [('' if index == 0 and j == 0 else '; ' if j == 0 else ' ', chunk)
                  for j, chunk in enumerate(chunks)]
        tokens += [('  ' if j == 0 else ' ', word) for j, word in enumerate(row['command'].split())]
        for separator, chunk in tokens:
            if len(current) + len(separator) + len(chunk) > 100:
                lines.append(current.rstrip())
                current = '  ' + chunk
            else:
                current += separator + chunk
    lines.append(current)
    lines.extend(['## Tick event types',
                  'Source: literal `dispatch.emit`/`look.emit_once` calls in `src/tick.py`, `lane_rules.toml` actions.'])
    lines.extend(_wrap('; '.join(sorted(events))))
    lines.extend(['`src/bundle_wait.py`: tick grade/regrade, marked base-only checks; once/subject/ready SHA/amendment.',
                  '`bundle-requested`: tick-only; `bundle-wait`: one executor alarm/subject/SHA after >2h.',
                  '`DONE+notes`; `BLOCKED+base_only/cited`: current proof with `n_new=0` → grade; existing gates apply.'])
    lines.extend(['`src/loadgate.py` begin_pass: one scratch disk sample, before optional load admission.',
                  'Builder/grader disk holds survive load off/errors; receiver thresholds default to 5/2 GiB.',
                  'Holds use admission.json holding; dry runs never save. Receiver alone reclaims and rechecks.'])
    queue = _toml(repo, 'look.toml')['merge_queue']
    parallel, fresh = queue['member_verify_parallel'], queue['verdict_fresh_hours']
    if type(parallel) is not int or parallel <= 0 or isinstance(fresh, bool) or not isinstance(fresh, (int, float)) or fresh <= 0:
        raise ValueError('merge_queue: expected positive member verdict settings')
    lines.extend(['## Member verdicts',
                  'Source: `look.toml` `[merge_queue]`, `src/window_members.py`, `src/prequeue.py` (`doit prequeue`).'])
    lines.extend(_wrap('After a red window with unnamed owners, each remaining member is verified alone on the '
                       f'window base, at most {parallel} CI runs at once (member_verify_parallel).'))
    lines.extend(_wrap('Each run appends `member-verdict {spec, window, tree, state, url, nodes}`; red members drop.'))
    lines.extend(_wrap('Green member-verdict or window-tree-verified for required checks on the exact tree is '
                       f'reused for {fresh} hours (verdict_fresh_hours). Reject undetermined, malformed, '
                       'stale or future-dated evidence.'))
    lines.extend(_wrap('`src/window_dispatch.py`: predeploy tree, window, suite, quick ids; legacy tree/quick empty.'))
    lines.extend(_wrap('`src/window_known_red.py`: `window-dispatch-verdict` completion receipts; '
                       'master-known-red retries stay pending (`window-skipped-known`, never green).'))
    lines.extend(_wrap('`src/window_cancel.py`: confirm owned cancels before replacement; tombstones veto verdicts.'))
    lines.extend(['## Branch refresh',
                  'Before rework: isolated rebase, Verification/guards, adoption, bundle, re-gate; '
                  f'max {queue.get("branch_refresh_per_day", 3)}/spec/day.'])
    lines.extend(_wrap('`src/direct_merge.py`: merge_one handles one do-it-v2 queue row: refuse overlapping '
                       'dirty paths; gate with unchanged main/branch shas; merge --no-ff, emit shipped. '
                       'A moved ref needs re-gating; failed merge is aborted. cli_gate uses doit gate; '
                       'caller owns queue selection and tick wiring.'))
    lines.extend(_wrap('`src/coupled_files.py`: spec pre-flight grants or asks for merge-gate-pinned non-test files.'))
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
