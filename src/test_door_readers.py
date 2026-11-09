"""Source-only Door reader ratchet. Never imports the application being scanned."""
import ast
import collections
import fnmatch
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_ledger_readers import ALLOW, METHODS, scan


ROOT = Path(__file__).resolve().parent.parent
INVENTORY = 'src/door_readers.txt'
INTEGRATION_REFS = ('refs/heads/main', 'refs/heads/master', 'refs/remotes/origin/main')
NO_REF = 'reader-ratchet: no integration ref; seed ceiling only'
# Frozen probe ceilings, including the six independently enumerated fold.py calls.
SEED = dict((path, int(count)) for path, count in (line.split() for line in '''
scripts/problem_backfill.py 1
scripts/pulse.py 1
scripts/seat/backfill_tokens.py 1
src/adopt_candidate.py 1
src/audit.py 1
src/balance.py 2
src/brief.py 1
src/build_readiness.py 1
src/build_runner.py 2
src/cancel.py 1
src/ci_runner.py 1
src/ci_shadow.py 3
src/dispatch.py 19
src/dispatch_builder.py 1
src/empty_grade.py 1
src/flow_health.py 1
src/fold.py 6
src/gh_bill.py 1
src/health_alarms.py 2
src/lane_rules.py 1
src/ledger_cache.py 1
src/ledger_gate.py 2
src/look.py 1
src/merge_gate.py 3
src/merge_queue.py 1
src/nextstep.py 1
src/owed_asks.py 2
src/owed_backfill.py 1
src/owed_digest.py 1
src/owed_failure_snapshot.py 1
src/owed_metrics.py 1
src/packet.py 1
src/pane_end.py 3
src/plain_backfill.py 3
src/problems.py 1
src/prod_browser.py 1
src/prod_runner.py 2
src/proving.py 2
src/provision.py 1
src/push_origin.py 1
src/quarantine.py 4
src/reap_scratch_db.py 1
src/reap_tmp.py 1
src/reap_worktrees.py 1
src/scorecard.py 1
src/seat_usage.py 1
src/spawns.py 1
src/spec_preflight.py 1
src/stall_sample.py 1
src/sweep_owed.py 5
src/think.py 3
src/thinker_answer.py 1
src/thinker_inbox.py 1
src/throughput.py 2
src/tick.py 3
src/tree_cleanup.py 1
src/up.py 5
src/verifier.py 3
src/window_gate.py 12
src/window_members.py 3
src/window_policy.py 2
src/window_rehearse.py 1
src/worktree_heal.py 1
'''.splitlines() if line))
CONTENT_APIS = {'read_text', 'iter_sources', '_read_bytes', '_read_range', '_open_binary', '_members'}
# Each exception is existing virtual-source/segment access predating the Door.
# Keep identities and counts frozen; migrations may remove calls, never add any.
VIRTUAL = {
    'src/autodispatch.py': {'read_text': 1},
    'src/carry.py': {'read_text': 1},
    'src/dispatch.py': {'iter_sources': 1},
    'src/dispatch_isolation.py': {'read_text': 1},
    'src/fold.py': {'iter_sources': 1},
    'src/grading_env.py': {'iter_sources': 1},
    'src/guards_live.py': {'read_text': 1},
    'src/ledger_cache.py': {'_members': 1, '_open_binary': 1},
    'src/ledger_gate.py': {'iter_sources': 1},
    'src/look.py': {'read_text': 1},  # L-spec-2176: raw own-file ledger input.
    'src/merge_deadman.py': {'read_text': 1},
    'src/pane_resume.py': {'read_text': 1},
    'src/realm.py': {'read_text': 2},
    'src/realm_ledger.py': {'_members': 1, '_read_bytes': 1, '_read_range': 1},
    'src/realm_omens.py': {'_members': 1, '_read_bytes': 1},
    'src/seat_usage.py': {'read_text': 1},  # L-spec-2179: fixed own-file prior lookup.
    'src/tg_check.py': {'iter_sources': 1, 'read_text': 1},
    'src/tick.py': {'read_text': 3},
    'src/up.py': {'read_text': 2},
    'src/window_gate.py': {'iter_sources': 1},
}
# Thinker 2026-11-08: exact raw-call ceilings measured on the pinned base.
# Reason for each: pre-Door legacy reader; migration separately owned.
LEGACY_RAW = {
    ('scripts/lessons_digest.py', 'events'): 2,
    ('scripts/pulse.py', 'portal_lines'): 1,
    ('scripts/seat/mkpacket.py', '<module>'): 1,
}


def raw_call_counts(source):
    """Count scanner-recognized calls without changing its read-only set API.

    Retain scan's assignment/caller propagation and marking rules; unlike its
    owner set, this result bounds multiple calls inside each legacy function.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def owner(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return tree

    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    aliases = {scope: {'EVENTS', 'events_dir'} for scope in scopes}
    functions = {s.name: s for s in scopes if s is not tree}

    def marked(expr, scope):
        if expr is None:
            return False
        if isinstance(expr, ast.Name):
            return expr.id in aliases[scope]
        if isinstance(expr, ast.Attribute):
            return expr.attr == 'EVENTS' or marked(expr.value, scope)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            return (marked(expr.left, scope) or marked(expr.right, scope) or
                    isinstance(expr.right, ast.Constant) and expr.right.value == 'events')
        if isinstance(expr, (ast.Tuple, ast.List)):
            return any(marked(e, scope) for e in expr.elts)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Attribute) and isinstance(expr.func.value, ast.Name) and expr.func.value.id == 'ledger_files':
                return False
            name = expr.func.attr if isinstance(expr.func, ast.Attribute) else expr.func.id if isinstance(expr.func, ast.Name) else None
            if name in {'Path', 'str', 'resolve', 'absolute', 'expanduser', 'with_name', 'with_suffix', 'glob', 'rglob', 'iterdir', 'sorted', 'max', 'next', 'iter'}:
                return ((isinstance(expr.func, ast.Attribute) and marked(expr.func.value, scope)) or
                        any(marked(a, scope) for a in expr.args))
        return False

    changed = True
    while changed:
        before = sum(map(len, aliases.values()))
        for node in ast.walk(tree):
            scope = owner(node)
            if isinstance(node, ast.For) and marked(node.iter, scope):
                aliases[scope].update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    pairs = zip(target.elts, node.value.elts) if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)) else [(target, node.value)]
                    for dest, value in pairs:
                        if marked(value, scope):
                            aliases[scope].update(n.id for n in ast.walk(dest) if isinstance(n, ast.Name))
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                target = functions.get(name)
                if target:
                    args = target.args.args
                    if isinstance(node.func, ast.Attribute) and args and args[0].arg == 'self':
                        args = args[1:]
                    for param, value in zip(args, node.args):
                        if marked(value, scope):
                            aliases[target].add(param.arg)
                    for kw in node.keywords:
                        if marked(kw.value, scope):
                            aliases[target].add(kw.arg)
        changed = before != sum(map(len, aliases.values()))
    counts = collections.Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else None
        if method not in METHODS:
            continue
        scope = owner(node)
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if marked(receiver, scope) or (method in {'open', 'listdir', 'scandir', 'glob'} and any(marked(a, scope) for a in node.args)):
            counts[scope.name if scope is not tree else '<module>'] += 1
    return counts


def parse_inventory(text):
    records = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split(maxsplit=2)
        if len(fields) < 2:
            raise ValueError(f'inventory line {number}: expected path and positive count')
        path, count = fields[:2]
        pure = PurePosixPath(path)
        if (pure.is_absolute() or '..' in pure.parts or str(pure) != path or
                '\\' in path or pure.suffix != '.py' or not count.isascii() or
                not count.isdecimal() or int(count) <= 0):
            raise ValueError(f'inventory line {number}: unsafe path or malformed count: {line}')
        if path in records:
            raise ValueError(f'inventory line {number}: duplicate {path}')
        records[path] = (int(count), fields[2] if len(fields) == 3 else '')
    if list(records) != sorted(records):
        raise ValueError('inventory records must be path-sorted')
    return records


def read_inventory(path):
    try:
        return parse_inventory(path.read_text())
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'inventory {path}: {exc}') from exc


def validate_ceiling(records, ceiling, label):
    for path, (count, _) in records.items():
        if path not in ceiling or count > ceiling[path]:
            raise ValueError(f'{label}: {path}: {count} > {ceiling.get(path, 0)} (new reader or increased ceiling)')


def clean_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], env=clean_environment(),
                                capture_output=True, text=True)
    except OSError as exc:
        raise ValueError(f'git {args[0]}: {exc}') from exc
    if result.returncode:
        raise ValueError(f'git {args[0]} failed: {result.stderr.strip()}')
    return result.stdout


def committed_inventory(root, revision):
    # ls-tree distinguishes a genuinely absent entry from a failed show/inspection.
    entry = git(root, 'ls-tree', revision, '--', INVENTORY)
    if not entry:
        return None
    return parse_inventory(git(root, 'show', f'{revision}:{INVENTORY}'))


def ratchet(root, records):
    validate_ceiling(records, SEED, 'seed')
    git(root, 'rev-parse', '--verify', 'HEAD')
    refs = set(git(root, 'for-each-ref', '--format=%(refname)', *INTEGRATION_REFS).splitlines())
    ref = next((ref for ref in INTEGRATION_REFS if ref in refs), None)
    if ref is None:
        return [NO_REF]
    base = git(root, 'merge-base', 'HEAD', ref).strip()
    base_records = committed_inventory(root, base)
    if base_records is not None:
        validate_ceiling(records, {p: row[0] for p, row in base_records.items()}, 'integration base')
    # Examine every revision and parent edge, including reductions later restored.
    revisions = git(root, 'rev-list', '--reverse', '--topo-order', f'{base}..HEAD').splitlines()
    history = {base: base_records}
    def at(rev):
        if rev not in history:
            history[rev] = committed_inventory(root, rev)
        return history[rev]
    for revision in revisions:
        current = committed_inventory(root, revision)
        if current is not None:
            validate_ceiling(current, SEED, 'seed branch revision')
        parents = []
        for parent in git(root, 'rev-list', '--parents', '-n', '1', revision).split()[1:]:
            previous = at(parent)
            parents.append(previous)
            if previous is not None:
                validate_ceiling(current or {}, {p: r[0] for p, r in previous.items()}, 'branch revision')
        # Absence before introduction is different from deletion after introduction.
        # Carry an empty ceiling across deletion, including into the working tree.
        history[revision] = current if current is not None else ({} if any(p is not None for p in parents) else None)
    head_records = at(git(root, 'rev-parse', 'HEAD').strip())
    if head_records is not None:
        validate_ceiling(records, {p: r[0] for p, r in head_records.items()}, 'branch revision working tree')
    return []


def census(source, path):
    tree = ast.parse(source, filename=path)
    modules = {'fold': {'fold'}, 'ledger_files': {'ledger_files'}, 'importlib': {'importlib'}}
    functions = collections.defaultdict(set)
    if path == 'src/fold.py':
        functions['fold', 'read_events'].add('read_events')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    modules[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                functions[node.module, alias.name].add(alias.asname or alias.name)

    def module(expr, name):
        if ast.unparse(expr) in modules[name]:
            return True
        if name == 'fold' and isinstance(expr, ast.Attribute) and expr.attr == 'fold':
            return True
        if isinstance(expr, ast.Call) and expr.args and isinstance(expr.args[0], ast.Constant) and expr.args[0].value == name:
            fn = expr.func
            return (isinstance(fn, ast.Name) and (fn.id == '__import__' or fn.id in functions['importlib', 'import_module']) or
                    isinstance(fn, ast.Attribute) and fn.attr == 'import_module' and module(fn.value, 'importlib'))
        return False

    def function(expr, name, api):
        return (ast.unparse(expr) in functions[name, api] or
                isinstance(expr, ast.Attribute) and expr.attr == api and module(expr.value, name))

    # Monotone alias propagation is conservative across branches and assignments.
    # Calls are counted below once per AST node, regardless of overlapping forms.
    changed = True
    identities = [('fold', 'read_events')] + [('ledger_files', api) for api in sorted(CONTENT_APIS)]
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            def pairs(dest, value):
                if isinstance(dest, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
                    for d, v in zip(dest.elts, value.elts):
                        yield from pairs(d, v)
                else:
                    yield dest, value
            for target in targets:
                for dest, value in pairs(target, node.value):
                    destination = ast.unparse(dest)
                    for name, aliases in modules.items():
                        if module(value, name) and destination not in aliases:
                            aliases.add(destination)
                            changed = True
                    for name, api in identities:
                        aliases = functions[name, api]
                        if function(value, name, api) and destination not in aliases:
                            aliases.add(destination)
                            changed = True
    calls, virtual = [], collections.Counter()
    def visit(node, internal=False):
        if path == 'src/fold.py' and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            internal = internal or node.name in {'read_events', '_read_events_uncached'}
        if isinstance(node, ast.Call):
            if not internal and function(node.func, 'fold', 'read_events'):
                calls.append(node.lineno)
            for api in CONTENT_APIS:
                if function(node.func, 'ledger_files', api):
                    virtual[api] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, internal)
    visit(tree)
    return sorted(calls), virtual


def is_test(path):
    return any(part.startswith('test_') or part == 'tests' or '_checks_' in part or
               part.endswith('_test.py') for part in PurePosixPath(path).parts)


def source_paths(root):
    def fail(error):
        raise error
    for directory in ('src', 'scripts'):
        start = root / directory
        if not start.is_dir() or start.is_symlink():
            raise ValueError(f'incomplete discovery: {directory} missing or symlink')
        for current, dirs, files in os.walk(start, onerror=fail):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in {'node_modules', '__pycache__'} and not is_test(d))
            for name in dirs:
                if (Path(current) / name).is_symlink():
                    raise ValueError(f'incomplete discovery: symlink {Path(current) / name}')
            for name in sorted(files):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                if name.startswith('.') or path.suffix != '.py' or is_test(relative):
                    continue
                if path.is_symlink():
                    raise ValueError(f'incomplete discovery: symlink {relative}')
                yield relative, path


def check_tree(root, records):
    errors, actual = [], {}
    try:
        for relative, path in source_paths(root):
            try:
                source = path.read_text()
                calls, virtual = census(source, relative)
                if relative != 'src/door.py' and calls:
                    actual[relative] = len(calls)
                    limit = records.get(relative, (0, ''))[0]
                    if len(calls) > limit:
                        errors.append(f'{relative}: {len(calls)} > {limit} full-reader calls at {calls}')
                if relative != 'src/door.py':
                    legacy_counts = raw_call_counts(source) if any(p == relative for p, _ in LEGACY_RAW) else {}
                    for owner in sorted(scan(source)):
                        legacy_limit = LEGACY_RAW.get((relative, owner))
                        if legacy_limit is not None:
                            count = legacy_counts.get(owner, 0)
                            if count == 0 or count > legacy_limit:
                                errors.append(f'{relative}:{owner}: legacy raw calls: {count} > {legacy_limit} (or scanner mismatch)')
                            continue
                        identity = (Path(relative).name, owner)
                        if relative != 'src/' + identity[0] or identity not in ALLOW:
                            errors.append(f'{relative}:{owner}: raw I/O outside exact ALLOW')
                if relative not in {'src/door.py', 'src/ledger_files.py'}:
                    for api, count in sorted(virtual.items()):
                        limit = VIRTUAL.get(relative, {}).get(api, 0)
                        if count > limit:
                            errors.append(f'{relative}: virtual {api}: {count} > {limit}')
            except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
                errors.append(f'{relative}: unreadable/unparseable source: {exc}')
    except (OSError, ValueError) as exc:
        errors.append(f'incomplete discovery: {exc}')
    for path in sorted(records.keys() - actual.keys()):
        errors.append(f'{path}: stale inventory row (no full-reader call remains)')
    return errors


def gate(root):
    try:
        records = read_inventory(root / INVENTORY)
        for diagnostic in ratchet(root, records):
            print(diagnostic)
        return check_tree(root, records)
    except ValueError as exc:
        return [str(exc)]


class ScratchTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='door-ratchet-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'scripts').mkdir()

    def put(self, path, text):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return dest

    def init_repo(self, records='src/tick.py 3\n', ref='refs/remotes/origin/main'):
        git(self.root, 'init', '-q', '-b', 'fixture')
        if records is not None:
            self.put(INVENTORY, records)
        self.put('anchor', 'fixture\n')
        self.commit()
        if ref:
            git(self.root, 'update-ref', ref, 'HEAD')

    def commit(self):
        git(self.root, 'add', '.')
        git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'fixture', '--allow-empty')

    def error(self, fragment):
        return self.assertRaisesRegex(ValueError, fragment)


class InventoryTests(ScratchTree):
    def test_integration_ref_order(self):
        self.assertEqual(INTEGRATION_REFS, ('refs/heads/main', 'refs/heads/master',
                                           'refs/remotes/origin/main'))

    def test_format(self):
        rows = parse_inventory('# metadata\nsrc/a.py 2\nsrc/b.py 1 full: several words\n')
        self.assertEqual(rows, {'src/a.py': (2, ''), 'src/b.py': (1, 'full: several words')})
        for text in ('src/a.py 1\nsrc/a.py 1', 'src/a.py 0', 'src/a.py -1',
                     'src/a.py one', 'src/a.py 1.0', '/src/a.py 1', '../a.py 1',
                     'src/../a.py 1', 'src//a.py 1', 'src/./a.py 1', 'src/a.txt 1',
                     'src\\a.py 1', 'src/a.py', 'src/b.py 1\nsrc/a.py 1'):
            with self.subTest(text=text), self.error('inventory'):
                parse_inventory(text)
        with self.error('inventory'):
            read_inventory(self.root / 'missing')

    def test_seed_and_reductions(self):
        self.assertEqual((len(SEED), sum(SEED.values())), (63, 133))
        validate_ceiling({'src/tick.py': (1, 'changed reason')}, SEED, 'seed')
        for records in ({'src/new.py': (1, '')}, {'src/tick.py': (4, '')}):
            with self.error('seed'):
                validate_ceiling(records, SEED, 'seed')

    def test_each_integration_ref(self):
        for ref in INTEGRATION_REFS:
            with self.subTest(ref=ref):
                with tempfile.TemporaryDirectory(prefix='door-ref-') as d:
                    root = Path(d)
                    git(root, 'init', '-q', '-b', 'fixture')
                    (root / 'src').mkdir()
                    (root / INVENTORY).write_text('src/tick.py 2\n')
                    git(root, 'add', '.')
                    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                        'commit', '-qm', 'base')
                    git(root, 'update-ref', ref, 'HEAD')
                    with self.error('integration base'):
                        ratchet(root, {'src/tick.py': (3, '')})
                    ratchet(root, {'src/tick.py': (1, '')})

    def test_committed_divergence_and_readdition(self):
        for before, after in [('src/tick.py 2\n', 'src/tick.py 3\n'),
                              ('', 'src/tick.py 1\n')]:
            with self.subTest(before=before):
                # Each case gets independent history, including a diverged integration tip.
                with tempfile.TemporaryDirectory(prefix='door-diverged-') as d:
                    root = Path(d)
                    git(root, 'init', '-q', '-b', 'fixture')
                    (root / 'src').mkdir()
                    (root / INVENTORY).write_text(before)
                    git(root, 'add', '.')
                    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                        'commit', '-qm', 'base')
                    base = git(root, 'rev-parse', 'HEAD').strip()
                    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                        'commit', '-qm', 'integration', '--allow-empty')
                    git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
                    git(root, 'reset', '--soft', base)
                    (root / INVENTORY).write_text(after)
                    git(root, 'add', '.')
                    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                        'commit', '-qm', 'candidate')
                    with self.error('integration base'):
                        ratchet(root, parse_inventory(after))

    def test_uncommitted_growth_and_shrinking_baseline(self):
        self.init_repo('src/tick.py 1\n')
        self.put(INVENTORY, 'src/tick.py 2\n')
        with self.error('integration base'):
            ratchet(self.root, read_inventory(self.root / INVENTORY))
        ratchet(self.root, {})

    def test_intermediate_reduction_cannot_be_restored(self):
        self.init_repo()
        self.put(INVENTORY, 'src/tick.py 1\n')
        self.commit()
        self.put(INVENTORY, 'src/tick.py 2\n')
        self.commit()
        with self.error('branch revision'):
            ratchet(self.root, read_inventory(self.root / INVENTORY))

    def test_initial_introduction(self):
        self.init_repo(None)
        self.commit()  # An unrelated commit before first introduction is legal.
        self.put(INVENTORY, 'src/tick.py 3\n')
        self.commit()
        ratchet(self.root, read_inventory(self.root / INVENTORY))
        with self.error('seed'):
            ratchet(self.root, {'src/tick.py': (4, '')})

    def test_removing_inventory_cannot_reset_branch_ceiling(self):
        self.init_repo(None)
        self.put(INVENTORY, 'src/tick.py 1\n')
        self.commit()
        (self.root / INVENTORY).unlink()
        self.commit()
        self.put(INVENTORY, 'src/tick.py 2\n')
        with self.error('branch revision'):
            ratchet(self.root, read_inventory(self.root / INVENTORY))
        self.commit()
        with self.error('branch revision'):
            ratchet(self.root, read_inventory(self.root / INVENTORY))

    def test_no_ref_fallback_and_missing_head(self):
        self.init_repo(ref=None)
        self.assertEqual(ratchet(self.root, {'src/tick.py': (3, '')}), [NO_REF])
        with self.error('seed'):
            ratchet(self.root, {'src/tick.py': (4, '')})
        with tempfile.TemporaryDirectory(prefix='door-no-head-') as d:
            with self.error('git'):
                ratchet(Path(d), {})

    def test_present_ref_inspection_errors_fail(self):
        self.init_repo()
        real_git = git
        for failing in ('merge-base', 'ls-tree', 'show'):
            def broken(root, *args):
                if args[0] == failing:
                    raise ValueError('git inspection failed')
                return real_git(root, *args)
            with self.subTest(command=failing), mock.patch(__name__ + '.git', side_effect=broken):
                with self.error('git inspection'):
                    ratchet(self.root, {'src/tick.py': (1, '')})
        with mock.patch('subprocess.run', side_effect=FileNotFoundError('git')):
            with self.error('git'):
                ratchet(self.root, {})


class CallCensusTests(ScratchTree):
    def test_positive_forms(self):
        forms = [
            'fold.read_events()', 'import fold as f; f.read_events()',
            'from fold import read_events as read; read()',
            'import fold as f; g=f; g.read_events()',
            'from fold import read_events as r; s=r; s()',
            'a=fold.read_events; b=a; b()',
            'f,r=fold,fold.read_events; f.read_events(); r()',
            'package.fold.read_events()',
            "import importlib as il; il.import_module('fold').read_events()",
            "__import__('fold').read_events()",
            "f=__import__('fold'); r=f.read_events; r()",
            'holder.reader=fold.read_events; holder.reader()',
        ]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(len(census(form, 'src/example.py')[0]), 2 if 'f,r=' in form else 1)
        self.assertEqual(len(census('fold.read_events(); fold.read_events()', 'src/a.py')[0]), 2)

    def test_negative_forms(self):
        source = '''# fold.read_events()
x = "fold.read_events()"
other.read_events()
door.full()
door.lines_of_item('x')
'''
        self.assertEqual(census(source, 'src/a.py')[0], [])

    def test_fold_bare_calls_and_internal_exclusions(self):
        source = '''read_events()
def subject_project(): return read_events()
def append():
    read_events(); read_events(); read_events()
def decide_overdue(): return read_events()
def read_events():
    read_events(); fold.read_events()
def _read_events_uncached():
    alias = read_events
    alias()
'''
        self.assertEqual(len(census(source, 'src/fold.py')[0]), 6)
        self.assertEqual(len(census(source, 'src/ordinary.py')[0]), 1)
        self.put('src/fold.py', source)
        rows = {'src/fold.py': (6, '')}
        self.assertEqual(check_tree(self.root, rows), [])
        for extra in ('\ndef seventh(): return read_events()\n',
                      '\na=read_events\ndef seventh(): return a()\n',
                      '\nfrom fold import read_events as r\nr()\n'):
            self.put('src/fold.py', source + extra)
            self.assertIn('src/fold.py: 7', '\n'.join(check_tree(self.root, rows)))

    def test_test_path_exclusions(self):
        for path in ('src/test_fake.py', 'scripts/tests/a.py', 'src/a_test.py',
                     'src/x_checks_misc/a.py', 'scripts/test_helpers/a.py'):
            self.put(path, 'fold.read_events()')
        self.assertEqual(check_tree(self.root, {}), [])

    def test_new_nested_readers_and_stale_rows(self):
        for path in ('src/new/nested.py', 'scripts/nested/new.py'):
            self.put(path, 'fold.read_events(); fold.read_events()')
        errors = '\n'.join(check_tree(self.root, {}))
        for path in ('src/new/nested.py', 'scripts/nested/new.py'):
            self.assertIn(path + ': 2', errors)
        self.assertIn('stale', '\n'.join(check_tree(self.root, {'src/gone.py': (1, 'full: old')})))
        self.put('src/tick.py', 'fold.read_events()')
        self.assertNotIn('src/tick.py', '\n'.join(check_tree(self.root, {'src/tick.py': (3, '')})))

    def test_bad_sources_and_incomplete_discovery(self):
        self.put('src/bad.py', 'def invalid(')
        self.assertIn('src/bad.py', '\n'.join(check_tree(self.root, {})))
        self.put('src/bad.py', 'fold.read_events()')
        real_read = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path.name == 'bad.py':
                raise PermissionError('denied fixture')
            return real_read(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', unreadable):
            self.assertIn('denied fixture', '\n'.join(check_tree(self.root, {})))
        with mock.patch('os.scandir', side_effect=PermissionError('discovery fixture')):
            self.assertIn('discovery fixture', '\n'.join(check_tree(self.root, {})))
        (self.root / 'src/link.py').symlink_to('/nonexistent/outside.py')
        self.assertIn('symlink', '\n'.join(check_tree(self.root, {})))


class BoundaryTests(ScratchTree):
    def test_raw_counter_retains_scanner_alias_and_caller_contract(self):
        sources = [
            'def control():\n    d = fold.EVENTS; d.glob("*"); d.iterdir()\n',
            'def caller():\n    read(ROOT / "events")\ndef read(d):\n    d.glob("*"); open(d / "x.jsonl")\n',
            'def ordinary():\n    pathlib.Path("ordinary.txt").read_text()\n',
        ]
        for source, expected in zip(sources, ({'control': 2}, {'read': 2}, {})):
            self.assertEqual(raw_call_counts(source), expected)
            self.assertEqual(set(raw_call_counts(source)), scan(source))

    def test_legacy_scripts_are_count_and_function_bounded(self):
        sources = {
            ('scripts/lessons_digest.py', 'events'): 'def events():\n    for f in glob.glob(str(ROOT / "events" / "*.jsonl")):\n        open(f)\n',
            ('scripts/pulse.py', 'portal_lines'): 'def portal_lines():\n    os.scandir(ROOT / "events")\n',
            ('scripts/seat/mkpacket.py', '<module>'): 'glob.glob(str(R / "events" / "*.jsonl"))\n',
        }
        for (path, owner), source in sources.items():
            with self.subTest(path=path):
                self.put(path, source)
                self.assertEqual(check_tree(self.root, {}), [])
                extra = '\nEVENTS.glob("*")\n' if owner == '<module>' else '\n    EVENTS.glob("*")\n'
                self.put(path, source + extra)
                self.assertIn('legacy raw calls', '\n'.join(check_tree(self.root, {})))
                self.put(path, source + '\ndef unrelated():\n    EVENTS.glob("*")\n')
                self.assertIn(path + ':unrelated: raw I/O', '\n'.join(check_tree(self.root, {})))
                self.put(path, '')

    def test_raw_controls_and_exact_function_exceptions(self):
        forms = ['d=fold.EVENTS; d.glob("*")', 'os.listdir(EVENTS)',
                 'directory=ROOT/"events"; directory.iterdir()',
                 '(fold.EVENTS/"a.jsonl").read_text()']
        for form in forms:
            self.put('src/look.py', 'def _read_jsonl():\n    ' + form)
            self.assertEqual(check_tree(self.root, {}), [])
            self.put('src/look.py', 'def unrelated():\n    ' + form)
            self.assertIn('raw I/O', '\n'.join(check_tree(self.root, {})))
        self.put('src/look.py', 'def caller():\n    read(ROOT/"events")\ndef read(d):\n    d.glob("*")')
        self.assertIn('look.py:read', '\n'.join(check_tree(self.root, {})))
        self.put('scripts/look.py', 'def _read_jsonl():\n    EVENTS.glob("*")')
        self.assertIn('scripts/look.py', '\n'.join(check_tree(self.root, {})))

    def test_virtual_aliases_and_all_content_apis(self):
        for api in CONTENT_APIS:
            forms = [f'ledger_files.{api}()',
                     f'import ledger_files as lf; lf.{api}()',
                     f'import ledger_files as lf; x=lf; x.{api}()',
                     f'from ledger_files import {api} as read; read()',
                     f'from ledger_files import {api} as read; other=read; other()',
                     f'x,y=ledger_files,ledger_files.{api}; y()',
                     f"__import__('ledger_files').{api}()"]
            for form in forms:
                with self.subTest(form=form):
                    self.put('src/new.py', form)
                    self.assertIn('virtual ' + api, '\n'.join(check_tree(self.root, {})))

    def test_virtual_negative_controls_and_frozen_counts(self):
        self.put('src/new.py', 'pathlib.Path("ordinary.txt").read_text()\nother.read_text()\nledger_files.names()\nledger_files.stat_keys()\n')
        self.assertEqual(check_tree(self.root, {}), [])
        self.put('src/tick.py', 'ledger_files.read_text()\n' * 3)
        self.assertEqual(check_tree(self.root, {}), [])
        self.put('src/tick.py', 'ledger_files.read_text()\n' * 4)
        self.assertIn('virtual read_text: 4 > 3', '\n'.join(check_tree(self.root, {})))
        self.put('src/tick.py', 'ledger_files.iter_sources()')
        self.assertIn('virtual iter_sources: 1 > 0', '\n'.join(check_tree(self.root, {})))
        self.put('src/tick.py', '')
        self.assertEqual(check_tree(self.root, {}), [])

    def test_seat_usage_own_file_reader_ceiling(self):
        self.put('src/seat_usage.py',
                 'ledger_files.read_text(fold.EVENTS, "L-seat-usage-local.jsonl")')
        self.assertEqual(check_tree(self.root, {}), [])
        self.put('src/seat_usage.py', 'ledger_files.read_text()\n' * 2)
        self.assertIn('seat_usage.py: virtual read_text: 2 > 1',
                      '\n'.join(check_tree(self.root, {})))
        self.put('src/seat_usage.py', 'ledger_files.iter_sources()')
        self.assertIn('seat_usage.py: virtual iter_sources: 1 > 0',
                      '\n'.join(check_tree(self.root, {})))

    def test_infrastructure_is_exact(self):
        self.put('src/door.py', 'def content():\n    EVENTS.glob("*")\n    ledger_files.read_text()')
        self.put('src/ledger_files.py', 'def _read_bytes():\n    EVENTS.glob("*")\n    ledger_files.read_text()')
        self.assertEqual(check_tree(self.root, {}), [])
        self.put('src/ledger_files.py', 'def unrelated():\n    EVENTS.glob("*")')
        self.assertIn('raw I/O', '\n'.join(check_tree(self.root, {})))
        self.put('scripts/door.py', 'ledger_files.read_text()\nfold.read_events()')
        self.assertIn('scripts/door.py', '\n'.join(check_tree(self.root, {})))
        self.put('src/ledger_files.py', 'fold.read_events()')
        self.assertIn('src/ledger_files.py', '\n'.join(check_tree(self.root, {})))


class RepositoryGateTests(ScratchTree):
    def test_candidate_and_runner_discovery(self):
        self.assertTrue(fnmatch.fnmatch(Path(__file__).name, 'test_*.py'))
        self.assertIn('for f in test_*.py test_*.mjs; do', (ROOT / 'doit').read_text())
        self.assertEqual(gate(ROOT), [])

    def test_door_can_appear_without_inventory_entry(self):
        self.init_repo('')
        self.assertEqual(gate(self.root), [])
        self.put('src/door.py', 'import fold\nfold.read_events()\nEVENTS.glob("*")')
        self.assertEqual(gate(self.root), [])

    def test_executable_from_root_and_src_under_isolation(self):
        # Explicit scratch copy uses the identical executable and raw scanner.
        for name in ('test_door_readers.py', 'test_ledger_readers.py', 'ledger_files.py'):
            shutil.copy2(ROOT / 'src' / name, self.root / 'src' / name)
        self.init_repo('')
        for inject in (False, True):
            if inject:
                self.put('scripts/deep/new.py', 'fold.read_events()')
            for cwd in (self.root, self.root / 'src'):
                with self.subTest(inject=inject, cwd=str(cwd)), tempfile.TemporaryDirectory(prefix='door-isolation-') as d:
                    env = clean_environment()
                    for key in ('HOME', 'DOIT_ROOT', 'TMPDIR', 'XDG_CONFIG_HOME'):
                        dest = Path(d) / key.lower()
                        dest.mkdir()
                        env[key] = str(dest)
                    env.pop('DOIT_LEDGER_FILE', None)
                    env['PYTHONDONTWRITEBYTECODE'] = '1'
                    result = subprocess.run([sys.executable, str(self.root / 'src/test_door_readers.py'),
                                             '--check'], cwd=cwd, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1 if inject else 0, result.stdout + result.stderr)
                    if inject:
                        self.assertIn('scripts/deep/new.py: 1', result.stderr)
        # Candidate is selected from __file__ even with deliberately misleading process state.
        with mock.patch.dict(os.environ, {'HOME': str(self.root), 'DOIT_ROOT': str(self.root),
                                          'GIT_WORK_TREE': str(self.root), 'GIT_DIR': '/missing'}):
            self.assertEqual(gate(ROOT), [])


if __name__ == '__main__':
    if sys.argv[1:] == ['--check']:
        problems = gate(ROOT)
        for problem in problems:
            print(problem, file=sys.stderr)
        sys.exit(bool(problems))
    unittest.main()
