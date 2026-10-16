#!/usr/bin/env python3
"""Worker budget = min(affinity, every visible ancestor's cgroup CPU quota)."""
import os
from pathlib import Path


def ancestors(root, relative):
    # Some container runtimes expose a cgroup path outside the mounted subtree;
    # the mount root still carries the effective container quota in that case.
    relative = Path(relative.lstrip('/'))
    leaf = root / relative if '..' not in relative.parts else root
    while True:
        yield leaf
        if leaf == root:
            break
        leaf = leaf.parent


def mounted_relative(proc, root, relative):
    try:
        mounts = (proc / 'self/mountinfo').read_text().splitlines()
    except FileNotFoundError:
        return relative
    for line in mounts:
        before, _, after = line.partition(' - ')
        fields = before.split()
        if after.split()[0] in ('cgroup2', 'cgroup') and Path(fields[4]) == root:
            try:
                return str(Path(relative).relative_to(fields[3]))
            except ValueError:
                return '.'
    return relative


def workers(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup'), affinity=None):
    budget = affinity if affinity is not None else len(os.sched_getaffinity(0))
    memberships = (proc / 'self/cgroup').read_text().splitlines()
    for line in memberships:
        _, controllers, relative = line.split(':', 2)
        if controllers == '':
            roots, names = [cgroup], ('cpu.max',)
        elif 'cpu' in controllers.split(','):
            roots = [r for r in (cgroup / 'cpu', cgroup / 'cpu,cpuacct', cgroup) if r.is_dir()]
            names = ('cpu.cfs_quota_us', 'cpu.cfs_period_us')
        else:
            continue
        for root in roots:
            for folder in ancestors(root, mounted_relative(proc, root, relative)):
                try:
                    if len(names) == 1:
                        quota, period = (folder / names[0]).read_text().split()
                        if quota == 'max':
                            continue
                    else:
                        quota, period = ((folder / name).read_text().strip() for name in names)
                        if int(quota) == -1:
                            continue
                    quota, period = int(quota), int(period)
                    if quota <= 0 or period <= 0:
                        raise ValueError('invalid cgroup CPU quota')
                    budget = min(budget, max(1, quota // period))
                except FileNotFoundError:
                    continue
    return max(1, budget)


if __name__ == '__main__':
    print(workers())
