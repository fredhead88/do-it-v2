"""AST guard for raw event-directory I/O, with exact documented exceptions."""
import ast
from pathlib import Path
import unittest
import ledger_files

METHODS = {'glob', 'rglob', 'iterdir', 'listdir', 'scandir', 'open', 'read_text', 'read_bytes'}


def scan(source):
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

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

    # Assignment and same-file caller propagation reach a fixed point.
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
    hits = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else None
        if method not in METHODS:
            continue
        scope = owner(node)
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if marked(receiver, scope) or (method in {'open', 'listdir', 'scandir', 'glob'} and any(marked(a, scope) for a in node.args)):
            hits.add(scope.name if scope is not tree else '<module>')
    return hits


# Every exception is an exact (file, function), with its own reason.
ALLOW = {
    ('base_red.py', '_nightly_rows'): 'Only reads L-base-red-local.jsonl; local files never roll up (additional classified hit).',
    ('base_red.py', '_rows'): 'Only reads L-base-red-local.jsonl; local files never roll up.',
    ('dispatch.py', 'alloc'): 'Raw glob applies only to generic directories; event allocations use ledger_files.names.',
    ('flow_health.py', 'main'): 'Requires a listable events directory with a regular jsonl file before judging; decoded events use fold.read_events.',
    ('ledger_files.py', '_accessible'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', '_indexes'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', '_open_binary'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', '_read_bytes'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', '_read_range'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', 'assert_readable'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', 'names'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_files.py', 'read_text'): 'The virtual file layer itself; live Path.read_text preserves existing failures.',
    ('ledger_files.py', 'problems'): 'The virtual file layer itself; raw file access is confined here.',
    ('ledger_rollup.py', 'candidates'): 'The rollup reads live per-spawn file bytes in order to segment them; candidate selection and byte snapshot.',
    ('ledger_rollup.py', 'leftovers'): 'The rollup reads live per-spawn file bytes in order to segment them; enumerates only its segment leftovers.',
    ('ledger_rollup.py', 'member_bytes'): 'The rollup reads live per-spawn file bytes in order to segment them; reads the already indexed segment prefix.',
    ('ledger_rollup.py', 'next_segment'): 'The rollup reads live per-spawn file bytes in order to segment them; enumerates segment names for monotonic publication.',
    ('ledger_rollup.py', 'pending_cleanup'): 'The rollup reads live per-spawn file bytes in order to segment them; re-verifies originals before later-run unlink.',
    ('ledger_rollup.py', 'sync_directory'): 'The rollup reads live per-spawn file bytes in order to segment them; opens directories only for durability fsync.',
    ('look.py', '_read_jsonl'): 'Own local watchdog ledger (never rolled); other callers read transcript files.',
    ('look.py', '_write_brief'): 'Counts lines only in its own never-rolled local ledger.',
    ('pace.py', '_json_lines'): 'Binding R3 LIVE-ONLY exception for pace telemetry; local files never roll up (part B R3.1).',
    ('pace.py', '_origins'): 'Binding R3 LIVE-ONLY exception for pace telemetry; local files never roll up (part B R3.1).',
    ('realm.py', 'tail_forever'): 'Live append watcher; full snapshot reads use the helper.',
    ('relay.py', 'ledger_changed'): 'Live append change detector; rollup causes one extra refresh.',
    ('tick.py', '_events_state'): 'Live append change detector; rollup causes one extra refresh.',
}


class ReaderGuard(unittest.TestCase):
    def test_controls(self):
        forms = [
            'd = fold.EVENTS; d.glob("*.jsonl")',
            'directory = ROOT / "events"; directory.glob("*.jsonl")',
            'ledger.EVENTS.iterdir()',
            'pathlib.Path(events_dir).glob("*.jsonl")',
            '(root() / "events").glob("*.jsonl")',
            'os.listdir(EVENTS)',
            'glob.glob(str(EVENTS / "*.jsonl"))',
            '(fold.EVENTS / f"{sid}.jsonl").read_text()',
            'd = fold.EVENTS; d.rglob("*")',
            'open(EVENTS / "x.jsonl")',
            'path = EVENTS / "x.jsonl"; path.open()',
        ]
        for form in forms:
            self.assertEqual(scan('def control():\n    ' + form), {'control'}, form)
        self.assertEqual(scan('def caller():\n    read(ROOT / "events")\ndef read(src_dir):\n    src_dir.glob("*")'), {'read'})
        print('negative controls: all direct, alias, and caller forms flagged')

    def test_tree(self):
        actual = {(p.name, fn) for p in Path(__file__).parent.glob('*.py')
                  if not p.name.startswith('test_') and '_checks_' not in p.name
                  for fn in scan(p.read_text())}
        self.assertEqual(actual - ALLOW.keys(), set(), 'unclassified ledger readers')
        self.assertEqual(ALLOW.keys() - actual, set(), 'stale exceptions')
        self.assertTrue(all(ALLOW.values()))
        self.assertEqual(ledger_files.FORMAT_VERSION, 1)
        print('L-spec-1787 AC4 ok')


if __name__ == '__main__':
    unittest.main()
