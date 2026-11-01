#!/usr/bin/env python3
"""Reap only allowlisted scratch databases on the local port-5432 cluster.
No FORCE, roles, remote connections, ledger writes, or connection strings in logs.
"""
import argparse
import json
import math
import os
import pathlib
import re
import subprocess
from datetime import datetime, timezone

import fold

# Probe q3: stage databases, numbered scratch/test databases and fingerprint runs.
# DSN-shaped names and ambiguous singletons deliberately remain outside this list.
PATTERNS = tuple(re.compile(p) for p in (
    r"as_(?:authority_parallel|authority_start_v3|executor_release_v1|execution_definition_v2|authority_runtime_v3|listing_nonsuper)_[0-9]+(?:_[0-9a-f]+)?",
    r"as_[a-z0-9_]+_test_[0-9]+(?:_[0-9a-f]+)?",
    r"as[0-9]+_scratch_[0-9]+(?:_[0-9a-f]+)?",
    r"genesis_fp_[0-9]+_[0-9]+",
))
DATA_ROOTS = (pathlib.Path('/var/lib/postgresql'),)


def candidate(name):
    return any(p.fullmatch(name) for p in PATTERNS)


class Runner:
    def command(self, args):
        # Do not inherit libpq service/password/connection overrides.
        env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
        return subprocess.run(['sudo', '-n', '-u', 'postgres', *args], env=env,
                              capture_output=True, text=True, check=True, timeout=30).stdout

    def sql(self, sql):
        return self.command(['psql', '-X', '-w', '-h', '/var/run/postgresql', '-p', '5432',
                             '-d', 'postgres', '-At', '-v', 'ON_ERROR_STOP=1', '-c', sql])

    def catalog(self):
        data = self.sql('SHOW data_directory').strip()
        data = self.command(['readlink', '-f', '--', data]).strip()
        rows = json.loads(self.sql("SELECT coalesce(json_agg(row_to_json(d)), '[]') FROM "
            "(SELECT oid, datname AS name, datistemplate AS template, dattablespace AS tablespace "
            "FROM pg_database ORDER BY datname) d"))
        return data, rows

    def newest(self, path):
        # No symlink traversal; an inaccessible file fails the entire candidate closed.
        if self.command(['readlink', '-f', '--', str(path)]).strip() != str(path):
            raise OSError('data path redirected')
        values = self.command(['find', '-P', str(path), '-exec', 'stat', '--format=%Y', '--', '{}', '+'])
        return max(float(v) for v in values.splitlines())

    def connections(self, name):
        return int(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname = '" + name + "'").strip())

    def drop(self, name):
        # Names have already passed the closed identifier patterns; never FORCE.
        self.sql('DROP DATABASE "' + name + '"')


def live_databases(events):
    ended = fold.terminal_spawns(events)
    live = {e.get('subject') for e in events if e.get('spawn') and e['spawn'] not in ended
            and (e.get('type') == 'build-started' or (e.get('type') == 'spawn-started'
                 and (e.get('role') == 'builder' or '-builder-' in e['spawn'])))}
    names = set()
    for e in events:
        if e.get('subject') in live:
            for key in ('database', 'dbname', 'scratch_db'):
                if candidate(str(e.get(key, ''))):
                    names.add(e[key])
        if e.get('type') == 'resource-created' and e.get('kind') == 'scratch-db' and e.get('subject') in live:
            names.add(e.get('ref'))
    return names


def run(events, *, runner=None, now=None, min_idle_h=48, dry_run=False):
    if not math.isfinite(min_idle_h) or min_idle_h < 48:
        raise ValueError('minimum idle retention is 48 hours')
    runner = runner or Runner()
    now = now or datetime.now(timezone.utc)
    data, databases = runner.catalog()  # failure here means cluster unreachable (CLI exit 2)
    data = pathlib.Path(data)
    safe = data.is_absolute() and '..' not in data.parts and any(r in data.parents for r in DATA_ROOTS)
    protected = live_databases(events)
    rows = []
    for db in databases:
        name = db['name']
        # Redact arbitrary names: the probe found literal connection strings in the catalog.
        label = name if re.fullmatch(r'[a-zA-Z0-9_]+', name) else '[non-identifier]'
        action, reason = 'kept', None
        if db.get('template') or not candidate(name):
            action, reason = 'skipped', 'outside scratch name allowlist'
        elif not safe or not str(db['oid']).isdigit() or db.get('tablespace') != 1663:
            reason = 'outside data root/default tablespace allowlist'
        elif name in protected:
            reason = 'live builder database'
        else:
            try:
                if runner.connections(name):
                    reason = 'active connections'
                elif now.timestamp() - runner.newest(data / 'base' / str(db['oid'])) <= min_idle_h * 3600:
                    reason = 'too recent'
                elif runner.connections(name):
                    reason = 'active connections'
                else:
                    if not dry_run:
                        runner.drop(name)
                    action, reason = ('would-drop' if dry_run else 'dropped'), 'idle beyond retention'
            except Exception:
                reason = 'undetermined or drop refused'
        rows.append(dict(database=label, action=action, reason=reason))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--min-idle-h', type=float, default=48)
    args = p.parse_args(argv)
    try:
        rows = run(fold.read_events(), min_idle_h=args.min_idle_h, dry_run=args.dry_run)
        lines = [f"{r['action']} {r['database']}: {r['reason']}" for r in rows]
    except Exception:
        lines = ['skipped cluster: unreachable or unsafe configuration']
        rc = 2
    else:
        rc = 0
    print('\n'.join(lines))
    if not args.dry_run:
        log = fold.ROOT / 'logs' / 'scratch-db-reaper.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as f:
            for line in lines:
                f.write(f'{datetime.now(timezone.utc).isoformat()} {line}\n')
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
