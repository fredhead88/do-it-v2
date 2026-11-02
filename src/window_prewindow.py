"""Cheap window admission using Git objects and spec metadata only.

No checkout, refs, CI, migration execution, or database access. Prefix commits
are unreachable Git objects so merge-tree can inspect the cumulative tree.
"""
import ast
import re
import subprocess

import dependency_holds
import merge_gate

VERSIONS = 'api/alembic_supabase/versions/'
CENSUS = 'dashboard/src/lib/server-identity.test.ts'
CALLER = re.compile(r'BACKEND_URL|process\.env\.API_KEY|\bbackendFetch\b|\bidentityHeaders\b')


class Deferred(ValueError):
    """An otherwise usable branch needs a later window, without rework."""


def git(repo, *args, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, timeout=60)
    if check and result.returncode:
        raise ValueError('cannot determine Git ' + args[0])
    return result


class Trees:
    def __init__(self, repo):
        self.repo, self.cache, self.blobs = repo, {}, {}

    def read(self, ref):
        tree = git(self.repo, 'rev-parse', ref + '^{tree}').stdout.strip()
        if tree in self.cache:
            return self.cache[tree]
        paths = {}
        for record in git(self.repo, 'ls-tree', '-r', '-z', tree).stdout.split('\0'):
            if record:
                meta, path = record.split('\t', 1)
                mode, kind, oid = meta.split()
                if kind == 'blob':
                    paths[path] = (mode, oid)
        wanted = {oid for path, (_, oid) in paths.items()
                  if path.startswith(VERSIONS) or dashboard_source(path)} - self.blobs.keys()
        if wanted:
            result = subprocess.run(['git', '-C', str(self.repo), 'cat-file', '--batch'],
                                    input=('\n'.join(sorted(wanted)) + '\n').encode(),
                                    capture_output=True, timeout=60)
            if result.returncode:
                raise ValueError('cannot read candidate blobs')
            data, offset = result.stdout, 0
            for oid in sorted(wanted):
                end = data.index(b'\n', offset)
                header = data[offset:end].split()
                if len(header) != 3 or header[1] != b'blob':
                    raise ValueError('cannot read candidate blob')
                size = int(header[2])
                self.blobs[oid] = data[end + 1:end + 1 + size].decode('utf-8')
                offset = end + size + 2
        self.cache[tree] = paths
        return paths

    def source(self, paths, path):
        mode, oid = paths[path]
        if mode not in ('100644', '100755'):
            raise ValueError('unsupported source mode: ' + path)
        return self.blobs[oid]


def dashboard_source(path):
    return (path.startswith('dashboard/src/') and path.endswith(('.ts', '.tsx'))
            and not re.search(r'\.(test|spec)\.', path))


def callers(reader, paths):
    # Keep the same inventory predicate as server-identity.test.ts.
    return {path for path in paths if dashboard_source(path)
            and CALLER.search(reader.source(paths, path))}


def revisions(reader, paths, *, require_single_head=True):
    graph = {}
    for path in paths:
        if not path.startswith(VERSIONS) or not path.endswith('.py'):
            continue
        relative = path[len(VERSIONS):]
        if '/' in relative or relative == '__init__.py':
            continue
        try:
            values = {}
            for node in ast.parse(reader.source(paths, path)).body:
                targets = node.targets if isinstance(node, ast.Assign) else (
                    [node.target] if isinstance(node, ast.AnnAssign) else [])
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in ('revision', 'down_revision'):
                        if target.id in values:
                            raise ValueError('repeated revision declaration')
                        values[target.id] = ast.literal_eval(node.value)
            revision, parent = values['revision'], values['down_revision']
            if not isinstance(revision, str) or not revision or revision in graph:
                raise ValueError('invalid or duplicate revision')
            parents = () if parent is None else (parent,) if isinstance(parent, str) else parent
            if not isinstance(parents, (tuple, list)) or not all(isinstance(p, str) and p for p in parents):
                raise ValueError('invalid down_revision')
            graph[revision] = tuple(parents)
        except (SyntaxError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('Alembic invalid revision metadata: ' + path) from exc
    if not graph:
        if any(path.startswith(VERSIONS) for path in paths):
            raise ValueError('Alembic has no readable revisions')
        return graph
    parents = {p for ps in graph.values() for p in ps}
    if parents - graph.keys():
        raise ValueError('Alembic references a missing parent')
    visiting, visited = set(), set()

    def visit(revision):
        if revision in visiting:
            raise ValueError('Alembic revision cycle')
        if revision in visited:
            return
        visiting.add(revision)
        for parent in graph[revision]:
            visit(parent)
        visiting.remove(revision)
        visited.add(revision)

    for revision in graph:
        visit(revision)
    if require_single_head and len(graph.keys() - parents) != 1:
        raise ValueError('Alembic candidate must have exactly one head')
    return graph


def candidate_revisions(reader, paths, required):
    graph = revisions(reader, paths)
    if required and not graph:
        raise ValueError('Alembic candidate must have exactly one head (found zero)')
    return graph


def prerequisite(spec, text, events):
    # Use the dispatcher's canonical newest-hold/release semantics.
    import autodispatch
    own = [e for e in events if e.get('subject') == spec]
    holds = [e for e in own if e.get('type') == 'dependency-hold']
    written = [e for e in own if e.get('type') in ('spec-written', 'spec-amended')]
    hold = max(holds, key=autodispatch._hold_order, default=None)
    latest = max(written, key=autodispatch._hold_order, default=None)
    if hold and (not latest or autodispatch._hold_order(hold)[0] >= autodispatch._hold_order(latest)[0]):
        if autodispatch._hold_released(hold, own):
            return None
        dependency = hold.get('waits_for')
    else:
        dependency = dependency_holds.from_spec_text(text)
        if dependency and latest:
            declaration = dict(latest, type='dependency-hold', _src='spec-text:' + spec)
            if autodispatch._hold_released(declaration, own):
                return None
    if dependency is not None and (not dependency_holds.valid_dependency(dependency) or dependency == spec):
        raise ValueError('invalid dependency-hold waits_for')
    if hold and (not latest or autodispatch._hold_order(hold)[0] >= autodispatch._hold_order(latest)[0]) and dependency is None:
        raise ValueError('invalid dependency-hold waits_for')
    return dependency


def overlap(left, right):
    """Prove disjointness conservatively, including paths not created yet.

    Native grants only use * and ? wildcards. Distinct literal prefixes cannot
    intersect; ambiguous glob intersections are serialized rather than guessed.
    """
    for a in left:
        for b in right:
            if merge_gate.granted(a.rstrip('/'), [b]) or merge_gate.granted(b.rstrip('/'), [a]):
                return a, b
            pa, pb = re.split(r'[*?]', a, maxsplit=1)[0], re.split(r'[*?]', b, maxsplit=1)[0]
            if any(c in a + b for c in '*?') and (pa.startswith(pb) or pb.startswith(pa)):
                return a, b
    return None


def merge_tree(repo, left, right):
    result = git(repo, 'merge-tree', '--write-tree', '--name-only', left, right, check=False)
    if result.returncode:
        raise ValueError('textual merge is not clean' if result.returncode == 1
                         else 'cannot determine textual merge')
    tree = result.stdout.splitlines()[0]
    if not re.fullmatch(r'[0-9a-f]{40,64}', tree):
        raise ValueError('cannot determine merged tree')
    return tree


def merged_dependency(repo, dependency, master, events):
    # A shipped marker alone cannot prove it is on today's master. Resolve the
    # recorded merge SHA (or the gate-clean branch for older shipped records).
    shipped = [e for e in events if e.get('subject') == dependency and e.get('type') == 'shipped']
    for event in shipped:
        refs = [event.get('sha')]
        if not refs[0]:
            refs = [e.get('branch') for e in events if e.get('subject') == dependency
                    and e.get('type') == 'merge-gate-clean']
        for ref in refs:
            if isinstance(ref, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', ref):
                if git(repo, 'merge-base', '--is-ancestor', ref, master, check=False).returncode == 0:
                    return True
    return False


def metadata(repo, master, entry, events, content):
    spec, branch, sha = entry
    # Automatic quarantine commits are genuine queue entries without spec
    # content. Their producer already records a grant; trust only its pinned,
    # authenticated one-file artifact, never arbitrary gate-clean metadata.
    import quarantine_file
    path = quarantine_file.PATH
    if re.fullmatch(r'quarantine-[0-9a-f]{16}', spec) and branch == 'auto-quarantine/' + spec[11:]:
        authentic = any(e.get('type') == 'merge-gate-clean' and e.get('actor') == 'base-red'
                        and e.get('project') == 'albert-scott' and e.get('subject') == spec
                        and e.get('branch') == branch and e.get('sha') == sha
                        and e.get('grant') == [path] for e in events)
        fork = git(repo, 'merge-base', master, sha).stdout.strip()
        changed = git(repo, 'diff', '--name-only', '-z', fork, sha).stdout.split('\0')
        if authentic and changed == [path, '']:
            return '', [path]
        raise ValueError('invalid automatic quarantine metadata')
    try:
        text = (content / (spec + '.md')).read_text()
        return text, merge_gate.grant_from_text(text)
    except (OSError, merge_gate.Undetermined) as exc:
        raise ValueError('spec metadata missing or invalid Writes grant') from exc


def select(repo, base, main, entries, events, content, *, on=None):
    """Return admitted entries and rework drops; log deferrals without events.

    Unreadable master leaves admission to CI. A window-local clash stays queued,
    while a branch defect against master alone needs a rebuild.
    """
    reader, kept, drops, grants, dependencies, migration_specs = Trees(repo), [], [], {}, {}, set()
    prefix = base
    try:
        master = on or git(repo, 'rev-parse', '--verify', 'refs/heads/' + main + '^{commit}').stdout.strip()
        master_paths = reader.read(master)
        master_callers = callers(reader, master_paths)
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f'gate-window: prewindow cannot read master: {exc}; retaining entries for CI')
        return entries, []
    for entry in entries:
        spec, branch, sha = entry
        try:
            if not re.fullmatch(r'[A-Za-z0-9._-]+', spec):
                raise ValueError('invalid spec metadata id')
            text, grant = metadata(repo, master, entry, events, content)
            # Establish intrinsic defects before considering other members.
            alone = merge_tree(repo, master, sha)
            alone_paths = reader.read(alone)
            master_has_versions = any(p.startswith(VERSIONS) for p in master_paths)
            candidate_revisions(reader, alone_paths, master_has_versions)
            previous = reader.read(prefix)
            added_callers = callers(reader, alone_paths) - master_callers - callers(reader, previous)
            fork = git(repo, 'merge-base', prefix, sha).stdout.strip()
            changed = set(git(repo, 'diff', '--name-only', '-z', fork, sha).stdout.split('\0'))
            if added_callers and CENSUS not in changed:
                raise ValueError('backend caller census must be updated: ' + ', '.join(sorted(added_callers)))
            dependency = prerequisite(spec, text, events)
            if dependency and dependency not in grants and not merged_dependency(repo, dependency, master, events):
                raise Deferred('dependency-hold waits_for=' + dependency + ' is not merged or earlier in this window')
            for owner, other in grants.items():
                clash = overlap(grant, other)
                if clash:
                    raise Deferred('Writes overlaps ' + owner + ': ' + clash[0] + ' / ' + clash[1])
            try:
                tree = alone if prefix == master else merge_tree(repo, prefix, sha)
            except ValueError as exc:
                raise Deferred('merge with earlier window members: ' + str(exc)) from exc
            paths = reader.read(tree)
            try:
                graph = candidate_revisions(reader, paths, master_has_versions
                                            or any(p.startswith(VERSIONS) for p in previous))
            except ValueError as exc:
                raise Deferred('Alembic window combination: ' + str(exc)) from exc
            prior_graph = revisions(reader, previous, require_single_head=False)
            added_migrations = graph.keys() - prior_graph.keys()
            if added_migrations and migration_specs:
                ancestors, current = set(), dependency
                while current and current not in ancestors:
                    ancestors.add(current)
                    current = dependencies.get(current)
                if not migration_specs <= ancestors:
                    raise Deferred('Alembic migrations must be ordered by waits_for and re-chained')
            # Use merge parents only as ancestry inputs to the next merge-tree.
            prefix = git(repo, '-c', 'user.name=doit-prewindow', '-c',
                         'user.email=doit-prewindow@localhost', 'commit-tree', tree,
                         '-p', prefix, '-p', sha, '-m', 'prewindow admission').stdout.strip()
            kept.append(entry)
            grants[spec], dependencies[spec] = grant, dependency
            if added_migrations:
                migration_specs.add(spec)
        except Deferred as exc:
            print(f'gate-window: prewindow deferred {spec}: {exc}')
        except (ValueError, OSError, UnicodeError, RecursionError, subprocess.SubprocessError) as exc:
            drops.append((entry, str(exc)))
    return kept, drops
