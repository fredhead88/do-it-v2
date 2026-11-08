"""Hermetic Git regression fixtures for L-spec-0932, including merge-history replay."""
import ast
import atexit
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Standalone verification must never inherit the live resource ledger.
_ledger_root = tempfile.TemporaryDirectory(prefix="rechain-ledger-")
atexit.register(_ledger_root.cleanup)
os.environ["DOIT_ROOT"] = _ledger_root.name
os.environ["DOIT_NO_POKE"] = "1"
_grading = pathlib.Path(_ledger_root.name) / "grading.toml"
_grading.write_text("")
os.environ["DOIT_GRADING_TOML"] = str(_grading)
import rechain
import window_gate


def response(criterion, body, status=0):
    """CLI response evidence: hash the exact canonical response body."""
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(criterion=criterion, url="cli://rechain/" + criterion, status=status,
                          body_sha256=hashlib.sha256(encoded).hexdigest(), body_excerpt=body)))


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *args],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def replay_semantics(repo, cur, changes=None):
    """Inspect an R1 overlay without writing objects to the historical repo."""
    changes = changes or {}

    def source(path):
        value = changes[path] if path in changes else rechain.blob(repo, cur, path)
        return value.decode() if isinstance(value, bytes) else value

    _, head = rechain.policy(source(rechain.MANIFEST))
    paths = set(git(repo, "ls-tree", "-r", "--name-only", cur, rechain.VERSIONS).splitlines())
    paths.update(p for p in changes if p.startswith(rechain.VERSIONS))
    chain = {}
    for path in sorted(paths):
        if not path.endswith(".py") or "/_archive/" in path or path.endswith("/__init__.py"):
            continue
        revision, parent = rechain.revision(source(path))
        if revision in chain:
            raise ValueError("R1: duplicate migration revision")
        # Compare the complete inherited graph, including old Alembic merge
        # revisions. R1 adds one linear successor to its single current head.
        chain[revision] = () if parent is None else (parent,) if isinstance(parent, str) else tuple(parent)
    routed = head["revision"]
    ancestors = {p for parents in chain.values() for p in parents}
    if set(chain) - ancestors != {routed}:
        raise ValueError("R1: merged versions must have the single routed head")
    seen, todo = set(), [routed]
    while todo:
        current = todo.pop()
        if current not in chain:
            raise ValueError("R1: merged versions must have connected ancestry")
        if current not in seen:
            seen.add(current)
            todo.extend(chain[current])
    if seen != set(chain):
        raise ValueError("R1: merged versions must have connected ancestry")

    authority = None
    if rechain.AUTHORITY in changes or rechain.git(repo, "cat-file", "-e", f"{cur}:{rechain.AUTHORITY}", check=False).returncode == 0:
        for node in ast.parse(source(rechain.AUTHORITY)).body:
            if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == "AUTHORITY_REVISIONS" for n in node.targets):
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
                    value, = value.args
                authority = set(ast.literal_eval(value))
                break
        if authority is None:
            raise ValueError("AUTHORITY_REVISIONS literal not found")

    literals = []
    for fn in ast.parse(source(rechain.TEST)).body:
        if not isinstance(fn, ast.FunctionDef) or not re.search(r"isolated_heads|scratch|cli|route", fn.name):
            continue
        strings = [n for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        literals.extend((fn.name, i, n.value) for i, n in enumerate(strings)
                        if n.value in (routed, routed + "\n", routed + " (head)\n"))
    # Historical reasons are prose entries separated by sentence boundaries;
    # hand merges prepend entries while R1 appends them.
    reasons = {entry.strip() for entry in re.split(r"(?<=[.!?])\s*", head["reason"]) if entry.strip()}
    return dict(head=routed, chain=chain, authority=authority, reasons=reasons, head_literals=literals)


class Fixture:
    def __init__(self, root, *, repo=None):
        self.repo = pathlib.Path(repo) if repo is not None else pathlib.Path(root) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        git(self.repo, "init", "-b", "master")
        self.write(rechain.VERSIONS + "h0.py", "revision = 'h0'\ndown_revision = None\n")
        self.write(rechain.MANIFEST, json.dumps({"heads": [{"revision": "h0", "deployment": "routed",
                   "allowedCurrentRevisions": ["h0"], "reason": "Initial."}]}, indent=2) + "\n")
        self.write(rechain.TEST, '''def test_real_isolated_heads_are_exactly_manifested_and_routable():
    assert declared_heads == actual_heads == {"h0"}
    assert resolve_scratch_target(MANIFEST, VERSIONS) == "h0"
    for current in (
        "h0 (head)\\n",
    ):
        assert resolve_target(current, MANIFEST, VERSIONS) == "h0"

def test_scratch_union_current():
    current = "h0 (head)\\n"
    assert resolve_scratch_union_heads(MANIFEST, VERSIONS) == ("h0",)
''')
        self.write(rechain.AUTHORITY, 'AUTHORITY_REVISIONS = frozenset(\n    {\n        "h0",\n    }\n)\n')
        self.write(rechain.FINGERPRINT, "initial\n")
        self.write("outside.txt", "initial\n")
        self.base = self.commit("base")
        self.events = []

    def write(self, path, content):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def commit(self, msg):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def branch(self, name, parent=None, mutation=None):
        start = parent or self.base
        git(self.repo, "checkout", "-B", "br-" + name, start)
        h = rechain.routed_head(self.repo, start)
        self.write(rechain.VERSIONS + name + ".py", f"revision = '{name}'\ndown_revision = '{h}'\n")
        self.write(rechain.MANIFEST, rechain.manifest_edit(rechain.text(self.repo, start, rechain.MANIFEST), h, name,
                                                        f" {name} advances from {h}."))
        self.write(rechain.TEST, rechain.test_edit(rechain.text(self.repo, start, rechain.TEST), h, name))
        authority = rechain.text(self.repo, start, rechain.AUTHORITY)
        self.write(rechain.AUTHORITY, authority.replace('        "h0",', f'        "{name}",\n        "h0",'))
        self.write(rechain.FINGERPRINT, name + "\n")
        if mutation:
            mutation(self)
        sha = self.commit(name)
        git(self.repo, "checkout", "master")
        return name, "br-" + name, sha

    def ev(self, typ, subject, **kw):
        self.events.append(dict(type=typ, subject=subject, **kw))

    def scripts(self, root, fail=False, identical=False):
        root = pathlib.Path(root)
        pg, fp = root / "pg.py", root / "fp.py"
        pg.write_text('''import os, pathlib, sys
assert not any(k in os.environ for k in ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS", "DATABASE_URL", "AS_LOCAL_PG_ADMIN"))
p = pathlib.Path(sys.argv[1])
with open(os.environ["RECHAIN_TEST_LOG"], "a") as f: f.write(sys.argv[2] + " " + str(p) + "\\n")
if sys.argv[2] == "loopback":
    (p / "running").touch()
    print("ADMIN_DSN='postgresql://fixture@127.0.0.1:23456/postgres'")
else: (p / "running").unlink(missing_ok=True)
''')
        fp.write_text('''import os, pathlib, subprocess, sys
assert not any(k in os.environ for k in ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS", "DATABASE_URL"))
assert os.environ["AS_LOCAL_PG_ADMIN"].startswith("postgresql://fixture@127.0.0.1:")
with open(os.environ["RECHAIN_TEST_LOG"], "a") as f: f.write("fingerprint " + str(pathlib.Path.cwd()) + "\\n")
''' + ("sys.exit(1)\n" if fail else
             'pathlib.Path(sys.argv[-1]).write_bytes(subprocess.check_output(["git", "show", "HEAD:' + rechain.FINGERPRINT + '"]))\n' if identical else
             'pathlib.Path(sys.argv[-1]).write_text("final:" + subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "HEAD", "' + rechain.VERSIONS + '"]).decode())\n'))
        return {"DOIT_RECHAIN_PG_CMD": "/usr/bin/python3 " + str(pg),
                "DOIT_RECHAIN_FINGERPRINT_CMD": "/usr/bin/python3 " + str(fp),
                "RECHAIN_TEST_LOG": str(root / "calls")}


class RechainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rechain-test-")
        self.addCleanup(self.tmp.cleanup)
        self.fx = Fixture(self.tmp.name)

    def stacked(self):
        a, b = self.fx.branch("a"), self.fx.branch("b")
        cur, _ = window_gate.merge_into(self.fx.repo, self.fx.base, a[2], "a")
        return cur, b

    ISOLATION = '''def test_route():
    assert routed == "h0"
    assert is_ancestor(graph, "h0", ROUTED_HEAD)

def test_cli():
    for current in (
        "h0 (head)\\n",
    ):
        assert target == "h0"
'''
    HANDWRITTEN = '''def test_route():
    assert routed == 'b'
    assert is_ancestor(graph, "h0", ROUTED_HEAD)
    assert is_ancestor( graph, 'b', ROUTED_HEAD )

def test_cli():
    for current in (
        'h0 (head)\\n',
        'b (head)\\n',
    ):
        assert target == 'b'
'''
    ASSERTION_ONLY = '''def test_graph():
    assert is_ancestor(graph, "h0", ROUTED_HEAD)
'''
    WITH_ASSERTION = '''def test_graph():
    assert is_ancestor(graph, "h0", ROUTED_HEAD)
    assert is_ancestor(graph, "b", ROUTED_HEAD)
'''
    ARENA = "tests/pipelines/amazon/test_arena.py"
    HISTORY = 'VALUE = 0\nHISTORY = "h0"\nANCESTOR = "listing_arena_001"\n'

    def coverage_case(self, shape, *, already=False, combined=False):
        f = self.fx
        f.write(rechain.TEST, self.ISOLATION if shape == "handwritten" else self.ASSERTION_ONLY)
        f.write(rechain.VERSIONS + "h0.py", "revision='h0'\ndown_revision='listing_arena_001'\n")
        f.write(rechain.VERSIONS + "history.py", "revision='listing_arena_001'\ndown_revision=None\n")
        f.write(self.ARENA, self.HISTORY)
        f.write("executable", "#!/bin/sh\nexit 0\n")
        (f.repo / "executable").chmod(0o755)
        f.base = f.commit("coverage base")
        def current(f):
            source = (f.repo / rechain.TEST).read_text()
            if already:
                source = source.replace('    assert is_ancestor(graph, "h0", ROUTED_HEAD)',
                                        '    assert is_ancestor(graph, "h0", ROUTED_HEAD)\n'
                                        '    assert is_ancestor(graph, "b", ROUTED_HEAD)')
            f.write(rechain.TEST, source + '\ndef test_current_history():\n    assert retained == "a"\n')
            f.write(self.ARENA, self.HISTORY.replace('VALUE = 0', 'VALUE = 7'))
        a = f.branch("a", mutation=current)
        def branch(f):
            f.write(rechain.TEST, self.HANDWRITTEN if shape == "handwritten" else self.WITH_ASSERTION)
            if shape == "arena" or combined:
                f.write(self.ARENA, self.HISTORY + 'PARENT = "h0"\nREVISION = "b"\n')
            f.write(rechain.VERSIONS + "b.py", "revision='b'\ndown_revision='h0'\n"
                    "def upgrade():\n    op.execute('upgrade body')\n"
                    "def downgrade():\n    op.execute('downgrade body')\n")
        b = f.branch("b", mutation=branch)
        if shape != "handwritten":
            self.assertEqual(rechain.test_edit(self.ASSERTION_ONLY, "h0", "b"), self.ASSERTION_ONLY)
        return a[2], b

    def test_2253_isolation_entries(self):
        rows = []
        for shape, already, combined in (("handwritten", False, False), ("2189", False, False),
                                         ("2189", True, False), ("2189", False, True)):
            with self.subTest(shape=shape, already=already, combined=combined), tempfile.TemporaryDirectory() as root:
                self.fx = Fixture(root)
                cur, b = self.coverage_case(shape, already=already, combined=combined)
                changes, _, _, _, allowed = rechain.mechanical(self.fx.repo, cur, b[2])
                candidate = changes[rechain.TEST]
                tree = ast.parse(candidate)
                name = "test_route" if shape == "handwritten" else "test_graph"
                fn, = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
                self.assertEqual(sum(rechain.ancestry(n, "b") for n in fn.body), 1)
                self.assertEqual(sum(rechain.ancestry(n, "h0") for n in fn.body), 1)
                self.assertIn('assert retained == "a"', candidate)
                if shape == "handwritten":
                    self.assertIn('assert routed == "b"', candidate)
                    self.assertIn('"a\\n",', candidate)
                    self.assertIn('"b (head)\\n",', candidate)
                if combined:
                    self.assertIn('PARENT = "a"', changes[self.ARENA])
                    self.assertIn(self.ARENA, allowed)
                rows.append(dict(shape=shape, already_present=already, combined=combined, candidate=candidate))
        # The same entry point also proves arbitrary extra expressions decline.
        for expression in ('assert is_ancestor(graph, "b", OTHER)',
                           'assert is_ancestor(graph, "b", ROUTED_HEAD), "message"',
                           'assert is_ancestor(graph, "b", ROUTED_HEAD, extra)',
                           'helper()'):
            with self.assertRaisesRegex(ValueError, '^unrecognised isolation-test branch change$'):
                rechain.isolation_edit(self.ASSERTION_ONLY, self.ASSERTION_ONLY + '    ' + expression + '\n',
                                       self.ASSERTION_ONLY, 'h0', 'b', 'a')
            rows.append(dict(rejected=expression))
        response("2253-AC1", rows)

    def test_2253_committed_roundtrip(self):
        rows = []
        for shape, combined in (("handwritten", False), ("2189", False), ("arena", False), ("2189", True)):
            with self.subTest(shape=shape, combined=combined), tempfile.TemporaryDirectory() as root:
                self.fx = Fixture(root)
                cur, b = self.coverage_case(shape, combined=combined)
                repo = self.fx.repo
                def snapshot():
                    return (git(repo, "show-ref"), git(repo, "status", "--porcelain=v1"),
                            git(repo, "diff"), git(repo, "ls-files", "-s"))
                before = snapshot()
                sha, why = rechain.resolve(repo, cur, b, "w", ev=self.fx.ev)
                self.assertIsNone(why)
                self.assertIsNotNone(sha)
                # Each read is a fresh subprocess against the committed object.
                def read(path):
                    return git(repo, "show", sha + ":" + path) + "\n"
                diff = git(repo, "diff", "--name-only", cur, sha).splitlines()
                print("2253 committed diff", shape, combined, diff)
                expected = {rechain.VERSIONS + "b.py", rechain.MANIFEST, rechain.TEST, rechain.AUTHORITY}
                if shape == "arena" or combined:
                    expected.add(self.ARENA)
                self.assertEqual(set(diff), expected)
                self.assertEqual(git(repo, "show", "-s", "--format=%P", sha).split(), [cur, b[2]])
                self.assertEqual(rechain.routed_head(repo, sha), "b")
                migration = read(rechain.VERSIONS + "b.py")
                self.assertEqual(rechain.revision(migration), ("b", "a"))
                original = rechain.text(repo, b[2], rechain.VERSIONS + "b.py")
                self.assertEqual(migration, original.replace("down_revision='h0'", "down_revision='a'"))
                doc, head = rechain.policy(read(rechain.MANIFEST))
                self.assertEqual(head['allowedCurrentRevisions'], ['h0', 'a', 'b'])
                self.assertIn('b advances from a.', head['reason'])
                self.assertIn('"a",\n        "b",', read(rechain.AUTHORITY))
                self.assertEqual(read(rechain.FINGERPRINT), rechain.text(repo, cur, rechain.FINGERPRINT))
                test = read(rechain.TEST)
                fn_name = "test_route" if shape == "handwritten" else "test_graph"
                fn, = [n for n in ast.parse(test).body if isinstance(n, ast.FunctionDef) and n.name == fn_name]
                self.assertEqual(sum(rechain.ancestry(n, 'b') for n in fn.body), 1)
                self.assertEqual(sum(rechain.ancestry(n, 'h0') for n in fn.body), 1)
                self.assertIn('assert retained == "a"', test)
                arena = read(self.ARENA)
                expected_arena = self.HISTORY.replace('VALUE = 0', 'VALUE = 7')
                if shape == "arena" or combined:
                    expected_arena += 'PARENT = "a"\nREVISION = "b"\n'
                self.assertEqual(arena, expected_arena)
                for path in git(repo, "ls-tree", "-r", "--name-only", cur).splitlines():
                    if path not in expected:
                        self.assertEqual(git(repo, "ls-tree", cur, path), git(repo, "ls-tree", sha, path))
                self.assertEqual(snapshot(), before)
                event, = self.fx.events
                self.assertEqual(event['type'], 'rechained')
                self.assertEqual((event['old_parent'], event['new_parent'], event['revision'], event['mechanical']),
                                 ('h0', 'a', 'b', True))
                self.assertEqual(event['files'], sorted(expected | {rechain.FINGERPRINT}))
                rows.append(dict(shape=shape, combined=combined, diff=diff, head='b', parent='a',
                                 ancestry_count=1, arena=arena, refs_and_worktree_unchanged=True,
                                 inherited_files_and_modes_preserved=True, migration_bodies_preserved=True))
        response("2253-AC4", rows)

    def test_2253_decline_matrix(self):
        iso_rule = "unrecognised isolation-test branch change"
        pin_rule = "old parent literal in another changed file"
        conflict_rule = "conflict outside re-chain allowlist"
        cases = [
            ("isolation operator", iso_rule), ("isolation helper", iso_rule),
            ("isolation unrelated revision", iso_rule), ("ancestry argument", iso_rule),
            ("ancestry message", iso_rule), ("ancestry extra argument", iso_rule),
            ("ancestry deletion", iso_rule), ("ambiguous function", iso_rule),
            ("new unrelated revision", pin_rule), ("changed historical revision", pin_rule),
            ("body conflict", conflict_rule), ("comment", pin_rule), ("retyped comment", pin_rule), ("substring", pin_rule),
            ("computed", pin_rule), ("concatenated", pin_rule), ("ambiguous provenance", pin_rule),
            ("conftest", pin_rule), ("helpers", pin_rule), ("tools", pin_rule), ("non-test", pin_rule),
            ("two migrations", "branch must add exactly one active migration"),
            ("metadata", "allowed revisions must be unique with head last"), ("outside conflict", conflict_rule),
        ]
        rows = []
        for case, rule in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                self.fx = f = Fixture(root)
                f.write(rechain.TEST, self.ISOLATION)
                original = 'VALUE = "ordinary"\nHISTORY = "h0"\n'
                if case == "ambiguous provenance":
                    original = 'PIN = "h0"\n'
                if case == "retyped comment":
                    original = '\'return h0\'\n'
                if case == "changed historical revision":
                    original += 'ANCESTOR = "h0"\n'
                f.write('tests/test_parent.py', original)
                f.base = f.commit("negative base")
                def current(f):
                    if case == "body conflict":
                        f.write('tests/test_parent.py', original.replace('"ordinary"', '2'))
                    if case == "outside conflict":
                        f.write('outside.txt', 'current body\n')
                    if case == "ambiguous function":
                        f.write(rechain.TEST, self.ISOLATION + self.ISOLATION)
                a = f.branch("a", mutation=current)
                def branch(f):
                    source = (f.repo / rechain.TEST).read_text()
                    added = '    assert is_ancestor(graph, "b", ROUTED_HEAD)\n'
                    if case == "isolation operator":
                        source = source.replace('routed ==', 'routed !=')
                    elif case == "isolation helper":
                        source += '\ndef helper():\n    return True\n'
                    elif case == "isolation unrelated revision":
                        source = source.replace('routed == "b"', 'routed == "a"')
                    elif case == "ancestry argument":
                        source = source.replace('\ndef test_cli', added.replace('ROUTED_HEAD', 'OTHER') + '\ndef test_cli')
                    elif case == "ancestry message":
                        source = source.replace('\ndef test_cli', added.rstrip() + ', "message"\n\ndef test_cli')
                    elif case == "ancestry extra argument":
                        source = source.replace('\ndef test_cli', added.replace('ROUTED_HEAD)', 'ROUTED_HEAD, extra)') + '\ndef test_cli')
                    elif case == "ancestry deletion":
                        source = source.replace('    assert is_ancestor(graph, "h0", ROUTED_HEAD)\n', '')
                    elif case == "ambiguous function":
                        source = source.replace('\ndef test_cli', added + '\ndef test_cli')
                    f.write(rechain.TEST, source)
                    pin = 'PARENT = "h0"\n'
                    path = 'tests/test_parent.py'
                    if case == "new unrelated revision":
                        f.write(path, original + 'UNRELATED = "a"\n')
                    elif case == "changed historical revision":
                        f.write(path, original.replace('ANCESTOR = "h0"', 'ANCESTOR = "a"'))
                    elif case == "body conflict":
                        f.write(path, original.replace('"ordinary"', '1') + pin)
                    elif case in ("comment", "substring", "computed", "concatenated"):
                        values = dict(comment='# parent h0\n', substring='PIN = "prefix-h0"\n',
                                      computed='PIN = "h0" + suffix\n', concatenated='PIN = "h0" ""\n')
                        f.write(path, original + values[case])
                    elif case == "retyped comment":
                        f.write(path, '#return h0\n')
                    elif case == "ambiguous provenance":
                        f.write(path, original + original)
                    elif case in ("conftest", "helpers", "tools", "non-test"):
                        paths = dict(conftest='api/tests/conftest.py', helpers='api/tests/helpers.py',
                                     tools='tools/test_parent.py', **{'non-test': 'outside.txt'})
                        f.write(paths[case], pin)
                    elif case == "two migrations":
                        f.write(rechain.VERSIONS + 'extra.py', "revision='extra'\ndown_revision='h0'\n")
                    elif case == "metadata":
                        doc = (f.repo / rechain.MANIFEST).read_text().replace('"revision": "b"', '"revision": "wrong"')
                        f.write(rechain.MANIFEST, doc)
                    elif case == "outside conflict":
                        f.write('outside.txt', 'branch body\n')
                b = f.branch("b", mutation=branch)
                refs_before = git(f.repo, 'show-ref')
                status_before = git(f.repo, 'status', '--porcelain=v1')
                self.assert_rules_decline(a[2], b, rule)
                self.assertEqual(git(f.repo, 'show-ref'), refs_before)
                self.assertEqual(git(f.repo, 'status', '--porcelain=v1'), status_before)
                rows.append(dict(case=case, rule=rule, result='declined', events=1, refs_unchanged=True))
        response("2253-AC3", rows)

    def test_2253_parent_pins(self):
        rows = []
        for path in ("tests/test_parent.py", "api/tests/test_parent.py",
                     "tests/pipelines/amazon/test_arena.py", "api/tests/nested/test_parent.py"):
            for existing in (False, True):
                with self.subTest(path=path, existing=existing), tempfile.TemporaryDirectory() as root:
                    self.fx = Fixture(root)
                    historical = 'INDEPENDENT = 0\nHISTORY = "h0"\nANCESTOR = "listing_arena_001"\n'
                    if existing:
                        self.fx.write(rechain.VERSIONS + "h0.py",
                                      "revision='h0'\ndown_revision='listing_arena_001'\n")
                        self.fx.write(rechain.VERSIONS + "history.py",
                                      "revision='listing_arena_001'\ndown_revision=None\n")
                        self.fx.write(path, historical)
                        self.fx.base = self.fx.commit("historical pins")
                    before = historical if existing else ""
                    branch = before + 'PARENT = "h0"\nOTHER = \'h0\'\nREVISION = "b"\n'
                    current = {path: before.replace('INDEPENDENT = 0', 'INDEPENDENT = 17')} if existing else {}
                    cur, b = self.parent_case(files={path: branch}, current_files=current)
                    sha, why = rechain.resolve(self.fx.repo, cur, b, "w", ev=self.fx.ev)
                    self.assertIsNone(why)
                    candidate = rechain.text(self.fx.repo, sha, path)
                    self.assertIn(before.replace('INDEPENDENT = 0', 'INDEPENDENT = 17') + 'PARENT = "a"\nOTHER = \'a\'\nREVISION = "b"\n', candidate)
                    if existing:
                        self.assertIn('INDEPENDENT = 17', candidate)
                    self.assertIn(path, self.fx.events[-1]["files"])
                    rows.append(dict(path=path, base=before, branch=branch, candidate=candidate))
        for scenario in ("historical only", "changed pin", "quote conflict"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                self.fx = Fixture(root)
                path = "tests/test_existing.py"
                base = 'VALUE = 0\nHISTORY = "h0"\nPIN = "ordinary"\n'
                self.fx.write(path, base)
                self.fx.base = self.fx.commit("existing parent fixture")
                branch = base.replace('VALUE = 0', 'VALUE = 1') if scenario == "historical only" else base.replace('PIN = "ordinary"', "PIN = 'h0'")
                current = {path: base.replace('PIN = "ordinary"', 'PIN = "a"')} if scenario == "quote conflict" else {}
                cur, b = self.parent_case(files={path: branch}, current_files=current)
                sha, why = rechain.resolve(self.fx.repo, cur, b, "w", ev=self.fx.ev)
                self.assertIsNone(why)
                candidate = rechain.text(self.fx.repo, sha, path)
                expected = branch if scenario == "historical only" else branch.replace("PIN = 'h0'", "PIN = 'a'")
                self.assertEqual(candidate, expected)
                self.assertEqual(path in self.fx.events[-1]['files'], scenario != "historical only")
                rows.append(dict(case=scenario, path=path, base=base, branch=branch, candidate=candidate))
        response("2253-AC2", rows)

    def test_2111_ac1(self):
        base = 'AUTHORITY_REVISIONS = frozenset(\n    {\n        "h0",\n    }\n)\n'
        current = base.replace('        "h0",', '        "m1",\n        "h0",')
        for line in ('        "r1",\n', '        "r1",  # Index-only migration; no authority changes.\n'):
            branch = base.replace('    }', line + '    }')
            result = rechain.authority_edit(current, base, branch, "r1")
            self.assertEqual(result, current.replace('    }', line + '    }'))
        response("2111-AC1", dict(comment_preserved=True, plain_preserved=True))

    def test_2111_ac2(self):
        base = 'AUTHORITY_REVISIONS = frozenset(\n    {\n        "h0",\n    }\n)\n'
        line = '        "r1",  # note\n'
        branch = base.replace('    }', line + '    }')
        for changed in (branch.replace('"h0"', '"other"'),
                        base.replace('    }', '        "r1",\n        "r1",\n    }'),
                        base.replace('    }', '        "r1", x = 1\n    }')):
            with self.assertRaisesRegex(ValueError, "^unrecognised authority change$"):
                rechain.authority_edit(base, base, changed, "r1")
        with self.assertRaisesRegex(ValueError, "^authority insertion outside revision set$"):
            rechain.authority_edit(base, base, line + base, "r1")
        response("2111-AC2", dict(strict_rejections=4))

    def parent_case(self, *, comment=False, files=None, current_files=None):
        a = self.fx.branch("a", mutation=lambda f: [
            f.write(path, value) for path, value in (current_files or {}).items()])
        def mutate(f):
            if comment:
                authority = (f.repo / rechain.AUTHORITY).read_text()
                f.write(rechain.AUTHORITY, authority.replace('"b",', '"b",  # Index-only migration; no authority changes.'))
            for path, value in (files or {}).items():
                f.write(path, value)
        b = self.fx.branch("b", mutation=mutate)
        cur, _ = window_gate.merge_into(self.fx.repo, self.fx.base, a[2], "a")
        self.fx.events.clear()
        return cur, b

    def assert_parent_resolves(self, *, comment=False, files=None, expected=None):
        cur, b = self.parent_case(comment=comment, files=files)
        sha, why = rechain.resolve(self.fx.repo, cur, b, "w", ev=self.fx.ev)
        self.assertIsNone(why)
        self.assertIsNotNone(sha)
        self.assertEqual(rechain.revision(rechain.text(self.fx.repo, sha, rechain.VERSIONS + "b.py")), ("b", "a"))
        self.assertEqual(rechain.routed_head(self.fx.repo, sha), "b")
        event = self.fx.events[-1]
        self.assertEqual(event["type"], "rechained")
        for path, value in (expected or {}).items():
            self.assertEqual(rechain.text(self.fx.repo, sha, path), value)
            self.assertIn(path, event["files"])
        if comment:
            self.assertIn('"b",  # Index-only migration; no authority changes.',
                          rechain.text(self.fx.repo, sha, rechain.AUTHORITY))
        return sha

    def test_2111_ac3(self):
        self.assert_parent_resolves(comment=True)
        response("2111-AC3", dict(head="b", parent="a", comment_preserved=True, event="rechained"))

    def test_2111_ac4(self):
        path = "api/tests/test_b_parent.py"
        self.assert_parent_resolves(files={path: 'PARENT = "h0"\nOTHER = \'h0\'\n'},
                                    expected={path: 'PARENT = "a"\nOTHER = \'a\'\n'})
        response("2111-AC4", dict(two_quote_styles_rewritten=True, event_includes_test=True))

    def test_2111_ac5(self):
        path = "api/tests/test_b_parent.py"
        self.assert_parent_resolves(comment=True, files={path: 'PARENT = "h0"\nOTHER = \'h0\'\n'},
                                    expected={path: 'PARENT = "a"\nOTHER = \'a\'\n'})
        response("2111-AC5", dict(comment_and_parent_pins=True))

    def assert_rules_decline(self, cur, entry, rule):
        before = len(self.fx.events)
        sha, why = rechain.resolve(self.fx.repo, cur, entry, "w", ev=self.fx.ev)
        self.assertIsNone(sha)
        self.assertEqual(why, "re-chain rules could not be applied safely")
        self.assertEqual(len(self.fx.events), before + 1)
        event = self.fx.events[-1]
        self.assertEqual(event["type"], "rechain-declined")
        self.assertEqual(event["why"], why)
        self.assertEqual(event["rule"], rule)
        self.assertNotIn("stage", event)
        self.assertNotIn("stderr_tail", event)

    def test_2111_ac6(self):
        path = "api/tests/test_b_parent.py"
        cases = [
            ({path: "# from h0\n"}, None, "old parent literal in another changed file"),
            ({path: 'PARENT = "h0"\n# from h0\n'}, None, "old parent literal in another changed file"),
            ({"outside.txt": '"h0"\n'}, None, "old parent literal in another changed file"),
            ({"tools/helper_b.py": '"h0"\n'}, None, "old parent literal in another changed file"),
            ({path: '"h0"\n'}, {path: "other\n"}, "old parent literal in another changed file"),
            ({path: '"zz"\n'}, {path: "other\n"}, "conflict outside re-chain allowlist"),
        ]
        for i, (files, current, rule) in enumerate(cases):
            with self.subTest(case=i), tempfile.TemporaryDirectory() as root:
                self.fx = Fixture(root)
                if i == 2:
                    self.fx.write("api/tests/test_old.py", "before\n")
                    self.fx.base = self.fx.commit("pre-existing test")
                    files = {"api/tests/test_old.py": '"a"\n'}
                cur, b = self.parent_case(files=files, current_files=current)
                self.assert_rules_decline(cur, b, rule)
        for value, expected in [('PARENT = "h0"\nOTHER = "zz"\n', 'PARENT = "a"\nOTHER = "zz"\n'),
                                ('ANCESTOR = "h0"\n', 'ANCESTOR = "a"\n')]:
            with tempfile.TemporaryDirectory() as root:
                self.fx = Fixture(root)
                self.assert_parent_resolves(files={path: value}, expected={path: expected})
        response("2111-AC6", dict(narrow_rejections=6, accepted_rewrites=2))

    def test_2111_ac7(self):
        self.test_rules_decline_fields_unchanged()
        cur, b = self.parent_case(files={"api/tests/test_b_parent.py": "# from h0\n"})
        self.assert_rules_decline(cur, b, "old parent literal in another changed file")
        for exc, rule in [(KeyError("k"), "KeyError"), (ValueError("x" * 500), "x" * 200),
                          (UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "bad"), "UnicodeDecodeError"),
                          (ValueError(""), "ValueError")]:
            with patch.object(rechain, "mechanical", side_effect=exc):
                self.assert_rules_decline(cur, b, rule)
        response("2111-AC7", dict(rules_bounded=True, subclass_uses_class=True, legacy_fields_preserved=True))

    def ordinary_branch(self):
        git(self.fx.repo, "checkout", "-b", "br-c", self.fx.base)
        self.fx.write("outside.txt", "c\n")
        sha = self.fx.commit("ordinary conflict")
        git(self.fx.repo, "checkout", "master")
        return ("c", "br-c", sha)

    def test_2111_ac8(self):
        a = self.fx.branch("a", mutation=lambda f: f.write("outside.txt", "a\n"))
        c = self.ordinary_branch()
        cur, _ = window_gate.merge_into(self.fx.repo, self.fx.base, a[2], "a")
        before = list(self.fx.events)
        self.assertEqual(rechain.resolve(self.fx.repo, cur, c, "w", ev=self.fx.ev),
                         (None, "no added migration; ordinary conflict"))
        sha, why = rechain.resolve(self.fx.repo, self.fx.base, c, "w", ev=self.fx.ev)
        self.assertIsNotNone(sha)
        self.assertIsNone(why)
        self.assertEqual(self.fx.events, before)
        response("2111-AC8", dict(ordinary_conflict=True, clean_merge=True, events=0))

    def test_2111_ac9(self):
        a = self.fx.branch("a", mutation=lambda f: f.write("outside.txt", "a\n"))
        b = self.fx.branch("b", mutation=lambda f: f.write(rechain.VERSIONS + "extra.py",
                                                          "revision='extra'\ndown_revision='b'\n"))
        cur, _ = window_gate.merge_into(self.fx.repo, self.fx.base, a[2], "a")
        self.assert_rules_decline(cur, b, "branch must add exactly one active migration")
        c = self.ordinary_branch()
        with self.assertRaisesRegex(ValueError, "^branch must add exactly one active migration$"):
            rechain.mechanical(self.fx.repo, cur, c[2])
        with patch.object(rechain, "added_versions", side_effect=subprocess.CalledProcessError(1, "git")):
            self.assert_rules_decline(cur, c, "CalledProcessError")
        response("2111-AC9", dict(two_migrations_declined=True, mechanical_unchanged=True, failed_scan_closed=True))

    def fingerprint_env(self):
        root = pathlib.Path(self.tmp.name)
        env = self.fx.scripts(root)
        config = root / "grading.toml"
        venv = root / "venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin/python"
        python.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        python.chmod(0o755)
        config.write_text('[project.albert-scott]\nvenv = ' + json.dumps(str(venv)) + '\n')
        bare = root / "bare"
        bare.mkdir()
        (bare / "git").symlink_to("/usr/bin/git")
        env.update(DOIT_GRADING_TOML=str(config), PATH=str(bare))
        return root, env, venv

    def test_ac1_fingerprint_finds_project_python(self):
        branch = self.fx.branch("b")
        root, env, _ = self.fingerprint_env()
        script = root / "fingerprint.sh"
        script.write_text('python -c \'import pathlib, sys; pathlib.Path(sys.argv[1]).write_text("project python\\n")\' "$2" || exit 1\n'
                          'echo "fingerprint $PWD" >> "$RECHAIN_TEST_LOG"\n')
        env["DOIT_RECHAIN_FINGERPRINT_CMD"] = "/bin/bash " + str(script)
        with patch.dict(os.environ, env):
            sha = rechain.fingerprint(self.fx.repo, branch[2], "w", project="albert-scott", ev=self.fx.ev)
            self.assertNotEqual(sha, branch[2])
            self.assertEqual(rechain.blob(self.fx.repo, sha, rechain.FINGERPRINT), b"project python\n")
            calls = pathlib.Path(env["RECHAIN_TEST_LOG"]).read_text().splitlines()
            self.assertEqual([line.split()[0] for line in calls], ["loopback", "fingerprint", "stop"])
            with self.assertRaises(rechain.FingerprintFailed):
                rechain.fingerprint(self.fx.repo, branch[2], "w", project=None, ev=self.fx.ev)
        response("1534-AC1", dict(project_python=True, no_project_declined=True, calls=3))

    def test_ac2_venv_first_and_prod_vars_stripped(self):
        branch = self.fx.branch("b")
        root, env, venv = self.fingerprint_env()
        script = root / "fp.py"
        original = script.read_text()
        keys = ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "DATABASE_URL", "PGPASSWORD", "SCRATCH_PG_URL")
        env.update({k: "forbidden-fixture" for k in keys})
        for project, configured in (("albert-scott", True), ("unconfigured", False), (None, False)):
            expected = str(venv / "bin") + os.pathsep + env["PATH"] if configured else env["PATH"]
            script.write_text(original + '\nassert os.environ["PATH"] == ' + repr(expected) + '\n'
                              + 'assert not any(k in os.environ for k in ' + repr(keys) + ')\n'
                              + 'assert os.environ["AS_LOCAL_PG_ADMIN"] == "postgresql://fixture@127.0.0.1:23456/postgres"\n')
            with self.subTest(project=project), patch.dict(os.environ, env):
                rechain.fingerprint(self.fx.repo, branch[2], "w", project=project, ev=self.fx.ev)
        for name in ("rechain.py", "window_gate.py"):
            self.assertNotIn("/opt/" + "albert-scott", pathlib.Path(__file__).with_name(name).read_text())
        response("1534-AC2", dict(cases=3, venv_first=True, production_variables_stripped=True))

    def test_ac3_failure_carries_redacted_stderr_tail(self):
        branch = self.fx.branch("b")
        root, env, _ = self.fingerprint_env()
        secret = "test-" + "credential"
        raw = ["line1", "line2", "line3", "line4", "", "postgresql://u:" + secret + "@127.0.0.1:5432/x",
               "password=" + secret, "x" * 400, "line8"]
        script = root / "fp.py"
        script.write_text('import sys\nsys.stderr.write(' + repr("\n".join(raw)) + ')\nsys.exit(2)\n')
        with patch.dict(os.environ, env):
            with self.assertRaises(rechain.FingerprintFailed) as caught:
                rechain.fingerprint(self.fx.repo, branch[2], "w", ev=self.fx.ev)
        exc = caught.exception
        self.assertEqual(exc.stage, "fingerprint")
        self.assertTrue(str(exc).startswith("fingerprint regeneration failed"))
        self.assertEqual(exc.stderr_tail, ["line4", "[redacted database URL]", "password=[redacted]", "x" * 300, "line8"])
        self.assertTrue(all(len(line) <= 300 and secret not in line and "postgresql://" not in line for line in exc.stderr_tail))
        for pg_failure in (False, True):
            (root / ("pg.py" if pg_failure else "fp.py")).write_text(
                'import sys\n' + ('sys.stderr.write("PG fixture failed\\n")\n' if pg_failure else '') + 'sys.exit(1)\n')
            with patch.dict(os.environ, env), self.assertRaises(rechain.FingerprintFailed) as caught:
                rechain.fingerprint(self.fx.repo, branch[2], "w", ev=self.fx.ev)
            self.assertEqual(caught.exception.stage, "pg-start" if pg_failure else "fingerprint")
            self.assertEqual(caught.exception.stderr_tail, ["PG fixture failed"] if pg_failure else [])
        response("1534-AC3", dict(cases=3, redacted=True, bounded=True, empty_stderr_declined=True))

    def test_rules_decline_fields_unchanged(self):
        a = self.fx.branch("a", mutation=lambda f: f.write("outside.txt", "a\n"))
        b = self.fx.branch("b", mutation=lambda f: f.write("outside.txt", "b\n"))
        cur, _ = window_gate.merge_into(self.fx.repo, self.fx.base, a[2], "a")
        sha, why = rechain.resolve(self.fx.repo, cur, b, "w", ev=self.fx.ev)
        self.assertIsNone(sha)
        self.assertEqual(why, "re-chain rules could not be applied safely")
        event = self.fx.events[-1]
        self.assertEqual(event["why"], why)
        self.assertEqual(event["rule"], "conflict outside re-chain allowlist")
        self.assertNotIn("stderr_tail", event)
        self.assertNotIn("stage", event)
        response("1534-AC5-rules", dict(declined=True, why=why, no_stderr_tail=True))

    def test_missing_project_environment_declines(self):
        branch = self.fx.branch("b")
        root, env, venv = self.fingerprint_env()
        config = root / "grading.toml"
        for contents in ('[project.albert-scott]\nvenv = ' + json.dumps(str(venv / "missing")),
                         'invalid = [', None):
            if contents is None:
                config.unlink()
            else:
                config.write_text(contents)
            with patch.dict(os.environ, env), self.assertRaises(rechain.FingerprintFailed) as caught:
                rechain.fingerprint(self.fx.repo, branch[2], "w", project="albert-scott", ev=self.fx.ev)
            self.assertEqual(caught.exception.stage, "worktree")
        self.assertFalse((root / "calls").exists())

    def test_ac1_ac7(self):
        cur, b = self.stacked()
        repo = self.fx.repo
        new, conflicts = window_gate.merge_into(repo, cur, b[2], "plain")
        self.assertIsNone(new)
        self.assertIn(rechain.MANIFEST, conflicts)
        sha, why = rechain.resolve(repo, cur, b, "w", ev=self.fx.ev)
        self.assertIsNone(why)
        self.assertEqual(rechain.routed_head(repo, sha), "b")
        self.assertEqual(rechain.revision(rechain.text(repo, sha, rechain.VERSIONS + "b.py")), ("b", "a"))
        _, h = rechain.policy(rechain.text(repo, sha, rechain.MANIFEST))
        self.assertEqual(h["allowedCurrentRevisions"], ["h0", "a", "b"])
        self.assertIn("b advances from a", h["reason"])
        self.assertIn('"a\\n",\n        "b (head)\\n",', rechain.text(repo, sha, rechain.TEST))
        self.assertTrue(rechain.is_pure(repo, sha))
        tree = rechain.rewrite_tree(repo, git(repo, "rev-parse", sha + "^{tree}"), {"outside.txt": "extra"})
        bad = git(repo, "commit-tree", tree, "-p", cur, "-p", b[2], "-m", "bad")
        self.assertFalse(rechain.is_pure(repo, bad))
        self.assertEqual(len(self.fx.events), 1)
        self.assertTrue(self.fx.events[0]["mechanical"])
        response("AC1", dict(head=rechain.routed_head(repo, sha), allowed=h["allowedCurrentRevisions"],
                             parent=rechain.revision(rechain.text(repo, sha, rechain.VERSIONS + "b.py"))[1],
                             pure=rechain.is_pure(repo, sha)))
        response("AC7", dict(pure=rechain.is_pure(repo, sha), extra_file_pure=rechain.is_pure(repo, bad)))
        print("ac1 ac7 OK")

    def test_merge_simple_migration_order(self):
        import dispatch, fold, merge_queue
        from datetime import datetime, timedelta, timezone
        a, b = self.fx.branch("a"), self.fx.branch("b")
        root = self.fx.repo.parent / "queue-root"
        events_dir = root / "events"
        events_dir.mkdir(parents=True)
        ledger = events_dir / "L-executor-local.jsonl"
        now = datetime.now(timezone.utc)
        launches = []
        (root / "repos").mkdir()
        (root / "repos/albert-scott").symlink_to(self.fx.repo, target_is_directory=True)
        def emit(kind, sid, **fields):
            reason = dispatch.emit(ledger, {"subject": sid}, kind, **fields)
            self.assertIsNone(reason)
        def runner(argv):
            selected = argv[argv.index('--specs') + 1].split(',')
            launches.append(selected)
            self.assertEqual(len(selected), 1)
            entry = {"a": a, "b": b}[selected[0]]
            old = git(self.fx.repo, 'rev-parse', 'refs/heads/master')
            candidate, kept, clashes = window_gate.build_candidate(self.fx.repo, argv[2], old, [entry])
            self.assertEqual(kept, selected)
            self.assertFalse(clashes)
            window_gate.advance(self.fx.repo, 'master', old, candidate, argv[2], kept, [], 1, [], [entry], False)
            return dict(code=0)
        self.assertEqual(merge_queue.MIGRATION_ORDER_SOURCE, 'queue')
        with patch.object(fold, 'ROOT', root), patch.object(fold, 'EVENTS', events_dir), \
             patch.dict(os.environ, DOIT_ROOT=str(root), DOIT_LEDGER_FILE=ledger.name), \
             patch.object(window_gate, 'ev', side_effect=emit):
            os.environ.pop('DOIT_WINDOW_RECHAIN', None)
            for i, entry in enumerate((a, b)):
                with patch.object(dispatch, 'now', return_value=(now + timedelta(seconds=i-10)).isoformat()):
                    emit('queue-add', entry[0], project='albert-scott', branch=entry[1], rule='operator')
            self.assertEqual(merge_queue.next_candidate(fold.read_events(use_cache=False)), 'a')
            self.assertEqual(merge_queue.window_run('albert-scott', runner=runner,
                precheck_runner=lambda project: dict(master=git(self.fx.repo, 'rev-parse', 'master'),
                    ts=datetime.now(timezone.utc).isoformat(), clean=[])), 0)
            first = git(self.fx.repo, 'rev-parse', 'master')
            # The explicit opt-out leaves the conflicting second migration unmerged.
            with patch.dict(os.environ, DOIT_WINDOW_RECHAIN='off'):
                unchanged, kept, clashes = window_gate.build_candidate(self.fx.repo, 'off', first, [b])
            self.assertEqual(unchanged, first)
            self.assertFalse(kept)
            self.assertIn('b', clashes)
            self.assertNotIn('DOIT_WINDOW_RECHAIN', os.environ)
            self.assertEqual(merge_queue.next_candidate(fold.read_events(use_cache=False)), 'b')
            self.assertEqual(merge_queue.window_run('albert-scott', runner=runner,
                precheck_runner=lambda project: dict(master=git(self.fx.repo, 'rev-parse', 'master'),
                    ts=datetime.now(timezone.utc).isoformat(), clean=[])), 0)
            final = git(self.fx.repo, 'rev-parse', 'master')
            self.assertNotEqual(first, final)
            self.assertEqual(rechain.routed_head(self.fx.repo, final), 'b')
            self.assertEqual(rechain.revision(rechain.text(self.fx.repo, final, rechain.VERSIONS + 'b.py')), ('b', 'a'))
            self.assertEqual(launches, [['a'], ['b']])
            self.assertEqual([e['subject'] for e in fold.read_events(use_cache=False) if e['type'] == 'shipped'], ['a', 'b'])
        response('merge-simple/AC6', dict(launches=launches, single_head='b', parent='a',
                                         unset_default=True, off_skips_rechain=True))
        print('merge-simple AC6 status=0')

    def test_ac2(self):
        b = self.fx.branch("b")
        with patch.dict(os.environ, {"DOIT_WINDOW_RECHAIN": "on"}), patch.object(window_gate, "ev", self.fx.ev):
            sha, kept, clashes = window_gate.build_candidate(self.fx.repo, "w", self.fx.base, [b])
        self.assertEqual(kept, ["b"])
        self.assertFalse(clashes or self.fx.events)
        self.assertEqual(git(self.fx.repo, "rev-parse", sha + "^{tree}"), git(self.fx.repo, "rev-parse", b[2] + "^{tree}"))
        response("AC2", dict(kept=kept, conflicts=clashes, events=self.fx.events, identical_tree=True))
        print("ac2 OK")

    def test_ac3_declines(self):
        mutations = [
            lambda f: f.write("outside.txt", "b\n"),
            lambda f: f.write(rechain.VERSIONS + "extra.py", "revision='extra'\ndown_revision='h0'\n"),
            lambda f: f.write("own_test.py", "parent = 'h0'\n"),
            lambda f: f.write(rechain.TEST, (f.repo / rechain.TEST).read_text() + "new_assertion = True\n"),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(i=i), tempfile.TemporaryDirectory() as root:
                fx = Fixture(root)
                a = fx.branch("a", mutation=(lambda f: f.write("outside.txt", "a\n")) if i == 0 else None)
                b = fx.branch("b", mutation=mutate)
                cur, _ = window_gate.merge_into(fx.repo, fx.base, a[2], "a")
                with patch.dict(os.environ, {"DOIT_WINDOW_RECHAIN": "on"}), patch.object(window_gate, "ev", fx.ev):
                    sha, kept, clashes = window_gate.build_candidate(fx.repo, "w", cur, [b])
                self.assertEqual(sha, cur)
                self.assertEqual(kept, [])
                self.assertIn("b", clashes)
                self.assertTrue(fx.events[0]["why"])
        cur, b = self.stacked()
        doc = json.loads(rechain.text(self.fx.repo, cur, rechain.MANIFEST))
        doc["heads"].append(dict(doc["heads"][0]))
        tree = rechain.rewrite_tree(self.fx.repo, git(self.fx.repo, "rev-parse", cur + "^{tree}"), {rechain.MANIFEST: json.dumps(doc)})
        bad = git(self.fx.repo, "commit-tree", tree, "-p", cur, "-m", "two routed heads")
        self.assertIsNone(rechain.resolve(self.fx.repo, bad, b, "w", ev=self.fx.ev)[0])
        response("AC3", dict(decline_cases=["outside-conflict", "two-versions", "other-parent-literal", "two-routed-heads", "unknown-test-line"], unchanged=True))
        print("ac3 a-e OK")

    def test_ac8_fingerprint(self):
        b = self.fx.branch("b")
        repo = self.fx.repo
        refs = git(repo, "show-ref")
        before = git(repo, "status", "--porcelain")
        self.assertEqual(before, "")
        for fail, identical in [(False, False), (True, False), (False, True)]:
            with tempfile.TemporaryDirectory() as root:
                env = self.fx.scripts(root, fail, identical)
                env.update({k: "forbidden-fixture" for k in ("SUPABASE_DB_URL", "SUPABASE_DB_URL_DIRECT", "SUPABASE_DB_URL_RO", "SUPABASE_DB_URL_MIGRATIONS", "DATABASE_URL")})
                with patch.dict(os.environ, env):
                    if fail:
                        with self.assertRaises(ValueError):
                            rechain.fingerprint(repo, b[2], "w", ev=self.fx.ev)
                    else:
                        sha = rechain.fingerprint(repo, b[2], "w", ev=self.fx.ev)
                        self.assertEqual(sha == b[2], identical)
                        if not identical:
                            self.assertEqual(self.fx.events[-1]["fingerprint_sha256"], hashlib.sha256(rechain.blob(repo, sha, rechain.FINGERPRINT)).hexdigest())
                calls = pathlib.Path(env["RECHAIN_TEST_LOG"]).read_text().splitlines()
                self.assertEqual([c.split()[0] for c in calls], ["loopback", "fingerprint", "stop"])
                self.assertTrue(all(not pathlib.Path(c.split(" ", 1)[1]).exists() for c in calls))
            self.assertEqual(git(repo, "status", "--porcelain"), before)
            self.assertEqual(git(repo, "show-ref"), refs)
            self.assertEqual(len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) - 1, 1)
        print("ac7 fingerprint hash ac8 OK")
        response("AC8", dict(builder_refs_unchanged=True, porcelain=before, cleanup_cases=["success", "fingerprint-failure", "identical"], prod_variables_absent=True))

    def test_ac11_semantic_comparison(self):
        a = self.fx.branch("a")
        repo, cur = self.fx.repo, a[2]
        expected = replay_semantics(repo, cur)
        doc = json.loads(rechain.text(repo, cur, rechain.MANIFEST))
        doc["heads"][0]["reason"] = "a advances from h0.Initial."
        equivalent = {
            rechain.MANIFEST: json.dumps(doc),
            rechain.AUTHORITY: "AUTHORITY_REVISIONS = frozenset({'h0', 'a'})\n",
            rechain.TEST: rechain.text(repo, cur, rechain.TEST).replace('"a"', "'a'"),
            rechain.FINGERPRINT: "different rebuild\n",
        }
        self.assertEqual(replay_semantics(repo, cur, equivalent), expected)
        for path, value in (
            (rechain.AUTHORITY, "AUTHORITY_REVISIONS = frozenset({'h0'})\n"),
            (rechain.MANIFEST, rechain.text(repo, cur, rechain.MANIFEST).replace("a advances", "a changes")),
            (rechain.TEST, rechain.text(repo, cur, rechain.TEST).replace('== "a"', '== "h0"')),
        ):
            with self.subTest(path=path):
                self.assertNotEqual(replay_semantics(repo, cur, {path: value}), expected)
        with self.assertRaises(ValueError):
            replay_semantics(repo, cur, {rechain.VERSIONS + "a.py": "revision='a'\ndown_revision=None\n"})
        with self.assertRaises(ValueError):
            replay_semantics(repo, cur, {rechain.MANIFEST: rechain.text(repo, cur, rechain.MANIFEST).replace('"revision": "a"', '"revision": "h0"')})

    def test_ac11_real_replay(self):
        # Four hand-authored historical merges, each from two real Git parents.
        # The expected trees do not call mechanical()/resolve(): replay must
        # reproduce independently authored parent, manifest, tests and authority.
        repo = self.fx.repo
        branches = [self.fx.branch(name) for name in ("a", "b", "c", "d")]
        branches.append(self.fx.branch("e", mutation=lambda fx: fx.write(
            rechain.TEST, (fx.repo / rechain.TEST).read_text() + "# hand-edited isolation test\n")))
        cur = branches[0][2]
        history = []
        allowed = ["h0", "a"]
        reason = "Initial. a advances from h0."
        for name, branch, sha in branches[1:]:
            old = allowed[-1]
            allowed.append(name)
            reason += f" {name} advances from {old}."
            manifest = {"heads": [{"revision": name, "deployment": "routed",
                        "allowedCurrentRevisions": list(allowed), "reason": reason}]}
            tests = rechain.text(repo, cur, rechain.TEST)
            # Independently spell out the literal updates, including the old
            # current-head entry retained by the CLI's accepted-input loop.
            tests = tests.replace(f'"{old} (head)\\n",',
                                  f'"{old}\\n",\n        "{name} (head)\\n",')
            tests = tests.replace(f'"{old}"', f'"{name}"')
            tests = tests.replace(f'"{old} (head)\\n"', f'"{name} (head)\\n"')
            authority = 'AUTHORITY_REVISIONS = frozenset(\n    {\n' + "".join(
                f'        "{r}",\n' for r in [*allowed[1:], "h0"]) + '    }\n)\n'
            changes = {rechain.VERSIONS + name + ".py": f"revision = '{name}'\ndown_revision = '{old}'\n",
                       rechain.MANIFEST: json.dumps(manifest, indent=2) + "\n",
                       rechain.TEST: tests, rechain.AUTHORITY: authority}
            tree = rechain.rewrite_tree(repo, git(repo, "rev-parse", cur + "^{tree}"), changes)
            merge = git(repo, "commit-tree", tree, "-p", cur, "-p", sha, "-m", "hand merge " + name)
            history.append(merge)
            cur = merge
        git(repo, "update-ref", "refs/heads/master", cur)
        # Discover the fixtures through the same Git history query as a replay
        # of a live checkout; never depend on that checkout being installed.
        history = git(repo, "log", "--merges", "--first-parent", "--full-history", "--format=%H",
                      "master", "--", rechain.VERSIONS).splitlines()
        outcomes = []
        decline_rules = {
            "unrecognised isolation-test branch change": "R1(c): unrecognised branch-added isolation-test line",
            "old parent literal in another changed file": "R1 decline: literal P in another changed file",
            "branch must add exactly one active migration": "R1: exactly one added active migration",
            "migration requires a single literal parent": "R1(a): single literal down_revision",
            "branch manifest and migration disagree": "R1(b): manifest revision must match migration",
            "manifest does not have exactly one routed head": "R1 decline: exactly one routed head",
            "manifest head disagrees with migration graph": "R1(a): routed head cross-check",
            "disconnected migration graph": "R1 decline: single linear head",
            "allowed revisions must be unique with head last": "R1(b): unique allowed revisions with head last",
            "manifest head or successor is inconsistent": "R1(b): consistent successor revision",
            "ambiguous manifest value position": "R1(b): preserve unambiguous manifest formatting",
            "reason addition is not unambiguous": "R1(b): branch reason minus base reason",
            "unrecognised down_revision assignment": "R1(a): literal down_revision assignment",
            "authority file is missing": "R1(d): authority insertion requires existing revision set",
            "unrecognised authority change": "R1(d): only the branch revision insertion",
            "authority insertion outside revision set": "R1(d): insertion in AUTHORITY_REVISIONS",
            "ambiguous authority insertion point": "R1(d): branch insertion point",
        }
        for merge in history:
            parents = git(repo, "show", "-s", "--format=%P", merge).split()
            if len(parents) != 2 or len(rechain.added_versions(repo, parents[0], parents[1])) != 1:
                continue
            try:
                changes, *_ = rechain.mechanical(repo, parents[0], parents[1])
            except ValueError as exc:
                self.assertIn(str(exc), decline_rules, "AC11 cannot classify an unexpected failure as an R1 decline")
                why = decline_rules[str(exc)]
                outcomes.append(dict(outcome="DECLINED", event="rechain-declined", why=why))
                print("ac11 replay", len(outcomes), "DECLINED rechain-declined:", why)
            else:
                actual = replay_semantics(repo, parents[0], changes)
                self.assertEqual(actual, replay_semantics(repo, merge), "AC11 resolved replay differs semantically from historical merge")
                outcomes.append(dict(outcome="RESOLVED", head=actual["head"], semantic_equal=True))
                print("ac11 replay", len(outcomes), "RESOLVED", actual["head"], "semantically equal")
            if len(outcomes) == 4:
                break
        self.assertEqual(len(outcomes), 4)
        self.assertTrue(any(o["outcome"] == "RESOLVED" for o in outcomes), "AC11 requires at least one resolved replay")
        self.assertEqual([o["outcome"] for o in outcomes], ["DECLINED", "RESOLVED", "RESOLVED", "RESOLVED"])
        response("AC11", dict(replays=outcomes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
