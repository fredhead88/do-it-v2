#!/usr/bin/env python3
"""Remove clean terminal spec worktrees only under $R/worktrees/<project>/l-spec-NNNN.
Branches are retained. Repository checkouts are only queried, never switched.
"""
import argparse
import pathlib
import re

import fold
import tree_cleanup
import disk_reaper


def terminal(state):
    return state in ('accepted', 'killed', 'void', 'shipped') or state.startswith('shipped-owed-')


def git(path, *args):
    return tree_cleanup.git(path, *args).stdout


def run(events, *, root=None, dry_run=False, proc_refs=()):
    # Explicit fixture roots do not read machine-local hold/budget configuration.
    budget = disk_reaper._budget(disk_reaper._settings()) if root is None else None
    root = pathlib.Path(root) if root is not None else fold.ROOT
    allowed = root / 'worktrees'
    repos = root / 'repos'
    # Refuse redirected cleanup roots, even when their spelling is allowlisted.
    if allowed.is_symlink() or root.resolve() != root.absolute():
        return [dict(path=str(allowed), action='kept', reason='outside root allowlist')]
    by = {}
    for e in events:
        subject = e.get('subject')
        if fold.admits(e, e.get('actor', ''), by.get(subject, [])) is None:
            by.setdefault(subject, []).append(e)
    retracted = {sid for sid, evs in by.items() if any(e.get('type') == 'charter-retracted' for e in evs)}
    ended = fold.terminal_spawns(events)
    live_builders = {e.get('subject') for e in events if e.get('spawn') and e['spawn'] not in ended
                     and (e.get('type') == 'build-started' or (e.get('type') == 'spawn-started'
                          and (e.get('role') == 'builder' or '-builder-' in e['spawn'])))}
    rows = []
    for repo in sorted(repos.iterdir()) if repos.is_dir() else []:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', repo.name):
            continue
        try:
            trees = tree_cleanup.worktrees(repo)
        except Exception:
            rows.append(dict(path=str(repo), action='kept', reason='worktree listing undetermined'))
            continue
        for branch, name in sorted(trees.items(), key=lambda x: x[1]):
            path = pathlib.Path(name)
            row = dict(path=str(path), branch=branch, action='kept')
            # Exactly two levels under the allowlisted root. Never the primary repo.
            try:
                rel = path.relative_to(allowed)
            except ValueError:
                row['reason'] = 'outside root allowlist'
            else:
                if len(rel.parts) != 2 or rel.parts[0] != repo.name or not re.fullmatch(r'l-spec-[0-9]{4,}', rel.name):
                    row['reason'] = 'outside spec path allowlist'
                elif path.resolve() != path or path == repo.resolve():
                    row['reason'] = 'symlink or primary checkout'
                else:
                    sid = 'L-' + path.name[2:]
                    state = fold.spec_state(by.get(sid, []), retracted)
                    try:
                        if sid in live_builders:
                            row['reason'] = 'live builder'
                        elif proc_refs is None:
                            row['reason'] = 'undetermined process snapshot'
                        elif any(str(ref) == str(path) or str(ref).startswith(str(path) + '/') for ref in proc_refs):
                            row['reason'] = 'live process references worktree'
                        elif not terminal(state):
                            row['reason'] = f'nonterminal spec ({state})'
                        elif git(path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching', '--ignore-submodules=none').strip():
                            row['reason'] = 'dirty worktree (uncommitted or untracked files)'
                        elif budget is not None and (why := disk_reaper._budget_reason(budget, dry_run)):
                            row['reason'] = why
                        else:
                            size = disk_reaper.entry_bytes(path)
                            # Re-check registry and cleanliness directly before removal; no --force.
                            if tree_cleanup.worktrees(repo).get(branch) != str(path):
                                raise RuntimeError('registration changed')
                            if git(path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching', '--ignore-submodules=none').strip():
                                raise RuntimeError('worktree changed')
                            if not dry_run:
                                git(repo, 'worktree', 'remove', str(path))
                            row.update(action='would-remove' if dry_run else 'removed',
                                       reason=f'terminal spec ({state}); branch kept', freed_bytes=size)
                            if budget is not None:
                                budget['count'] += 1
                                budget['freed_bytes'] += size
                    except Exception:
                        row['reason'] = 'undetermined or removal refused'
            rows.append(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    try:
        refs = disk_reaper.process_refs()
    except OSError:
        refs = None
    for row in run(fold.read_events(), dry_run=args.dry_run, proc_refs=refs):
        print(f"{row['action']} {row['path']}: {row['reason']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
